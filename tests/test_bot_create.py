"""S8 · 建一个 Bot（`bot.create` / `bot.set_permission`）：真 SQLite + 真文件 + 真 WS 帧。

一条用例对应 `docs/design/issues/S8-create-bot.md` §4 的验收 → 测试映射；S8 D6 的顺序
（判完再写）由 `test_create_rejects_illegal_names_and_writes_nothing` 与
`test_list_on_a_repo_with_no_bots_writes_nothing` 两条守着。

这里**不 mock**：SQLite 是真文件、记忆空间是真目录、端到端那条起真内核子进程 + 真 WebSocket。
权限用例沿用 S6 的 `umask_022` 纪律（T38：`mkdir(mode=)` 只作用于叶子，不显式收紧就会漏）。
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import stat
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import websockets

from postel_studio.api import errors, frames
from postel_studio.api.bots import (
    ALREADY_EXISTS,
    MSG_ALREADY_EXISTS,
    MSG_NAME_INVALID,
    BotService,
)
from postel_studio.domain.models import (
    BOT_PERMISSION_DEFAULT,
    UNASSIGNABLE_PERMISSIONS,
    Permission,
    Repo,
    permission_covers,
    permission_parse,
    repo_id,
)
from postel_studio.storage import db as state_db
from postel_studio.storage import paths
from postel_studio.web import assets

from .gateway_util import Kernel, decode_frame, frame_result

# ---------------------------------------------------------------------------
# 脚手架：真 SQLite（`state_home` 由 conftest 指到 tmp_path，绝不出仓库）
# ---------------------------------------------------------------------------


@pytest.fixture
def umask_022() -> Iterator[None]:
    """在 D3 实测的 umask 下跑（`mkdir` 的默认 mode 才是 0755，T38 才复现得出来）。"""
    previous = os.umask(0o022)
    try:
        yield None
    finally:
        os.umask(previous)


@pytest.fixture
def stores(state_home: Path) -> Iterator[tuple[state_db.BotStore, state_db.RepoStore]]:
    """两个真 store 共用同一份 `state.db`（`state_home` 已把 `POSTEL_HOME` 指到 tmp）。"""
    db_path = state_home / "state.db"
    repos = state_db.RepoStore(db_path)
    bots = state_db.BotStore(db_path)
    try:
        yield bots, repos
    finally:
        bots.close()
        repos.close()


@pytest.fixture
def repos(stores: tuple[state_db.BotStore, state_db.RepoStore]) -> state_db.RepoStore:
    return stores[1]


@pytest.fixture
def bots(stores: tuple[state_db.BotStore, state_db.RepoStore]) -> state_db.BotStore:
    return stores[0]


@pytest.fixture
def service(stores: tuple[state_db.BotStore, state_db.RepoStore]) -> BotService:
    bot_store, repo_store = stores
    return BotService(bot_store, repo_store)


def _bind(repos: state_db.RepoStore, path: Path, *, name: str = "proj") -> Repo:
    """往库里放一条已绑定仓库。`bot.create` 不读 git，所以路径不必真的存在（D9）。"""
    return repos.upsert(
        Repo(
            repo_id=repo_id(str(path)),
            path=str(path),
            origin_url=None,
            default_branch="main",
            added_at=1.0,
        )
    )


def _code(callable_: Any, params: dict[str, object]) -> str:
    """跑一个服务方法，返回 `FrameError.code`；没有抛就返回 `"ok"`。"""
    try:
        callable_(params)
    except frames.FrameError as exc:
        return exc.code
    return "ok"


# ---------------------------------------------------------------------------
# 验收 1 / 4：创建成功、载荷形状、默认 L1
# ---------------------------------------------------------------------------


def test_create_returns_the_eight_field_payload(
    service: BotService, repos: state_db.RepoStore, tmp_path: Path
) -> None:
    """**验收 1**：载荷仍是 S6 的八个字段（多一个少一个都算破坏契约），权限是**选的那个**。"""
    repo = _bind(repos, tmp_path / "proj")

    payload = service.create({"repoId": repo.repo_id, "name": "builder", "permission": "L2"})

    assert set(payload) == {
        "botId",
        "repoId",
        "name",
        "permission",
        "agentKind",
        "charterRev",
        "charterSummary",
        "memoryPath",
    }
    assert payload["botId"] == f"{repo.repo_id}/builder"
    assert payload["repoId"] == repo.repo_id
    assert payload["name"] == "builder"
    assert payload["permission"] == "L2"
    assert payload["agentKind"] is None
    assert payload["charterSummary"] == "尚未填写职责"
    assert isinstance(payload["charterRev"], int) and payload["charterRev"] > 0
    assert payload["memoryPath"] == str(paths.bots_root() / repo.repo_id / "builder")


def test_create_then_list_shows_it(
    service: BotService, repos: state_db.RepoStore, tmp_path: Path
) -> None:
    """**验收 1**：创建出来的东西**从列表里读得到**（不是只在返回值里存在）。"""
    repo = _bind(repos, tmp_path / "proj")
    created = service.create({"repoId": repo.repo_id, "name": "builder"})
    assert service.list({"repoId": repo.repo_id}) == [created]


def test_create_defaults_to_l1(
    service: BotService, repos: state_db.RepoStore, tmp_path: Path
) -> None:
    """**验收 4**：缺字段与显式 `null` 都落 L1（05 §2.2「新建 Bot」行；S8 D2 的裁决）。"""
    repo = _bind(repos, tmp_path / "proj")
    assert service.create({"repoId": repo.repo_id, "name": "bare"})["permission"] == "L1"
    assert (
        service.create({"repoId": repo.repo_id, "name": "nulled", "permission": None})["permission"]
        == "L1"
    )
    assert BOT_PERMISSION_DEFAULT == "L1"


# ---------------------------------------------------------------------------
# 验收 2：非法名字 —— 一句可读的话，且**什么都没创建**
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_name",
    ["Builder", "b uilder", "a_b", "1abc", "-abc", "a" * 33, "", " ", "../x", "a/b"],
)
def test_create_rejects_illegal_names(
    service: BotService, repos: state_db.RepoStore, tmp_path: Path, bad_name: str
) -> None:
    """**验收 2**（06 §2.2 白名单）：形状不对 → `INVALID_PARAMS`，文案**说出规则**。"""
    repo = _bind(repos, tmp_path / "proj")
    with pytest.raises(frames.FrameError) as caught:
        service.create({"repoId": repo.repo_id, "name": bad_name})
    assert caught.value.code == "INVALID_PARAMS"
    # 文案是**常量**：等于常量就证明了它没有被拼进任何输入（05 §4.2）
    assert caught.value.message == MSG_NAME_INVALID


@pytest.mark.parametrize("bad_name", [None, 42, [], {"x": 1}])
def test_create_rejects_non_string_names(
    service: BotService, repos: state_db.RepoStore, tmp_path: Path, bad_name: object
) -> None:
    """非 `str` 走同一句错误——不是 `TypeError`，也不是另一句话。"""
    repo = _bind(repos, tmp_path / "proj")
    assert _code(service.create, {"repoId": repo.repo_id, "name": bad_name}) == "INVALID_PARAMS"


def test_create_rejects_illegal_names_and_writes_nothing(
    service: BotService, repos: state_db.RepoStore, bots: state_db.BotStore, tmp_path: Path
) -> None:
    """**验收 2 / D6 前五步**：一整串非法名字跑完，**行数与目录都还是零**。

    这条是"S8 D6 的写序"唯一的证据：判在写之前，拒绝就不该留下痕迹。
    """
    repo = _bind(repos, tmp_path / "proj")
    for bad_name in ("Builder", "b uilder", "a_b", "1abc", "-abc", "a" * 33, "", "../x"):
        assert _code(service.create, {"repoId": repo.repo_id, "name": bad_name}) != "ok"
    assert bots.list_for_repo(repo.repo_id) == []
    assert not (paths.bots_root() / repo.repo_id).exists()


# ---------------------------------------------------------------------------
# 验收 3：权限矩阵（四个能授予、两个按名字拒绝、其余是参数错）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("value", ["L0", "L1", "L2", "L2+"])
def test_create_accepts_the_four_assignable_levels(
    service: BotService, repos: state_db.RepoStore, tmp_path: Path, value: str
) -> None:
    """**验收 3 的反面**：这四个都能建（`L2+` 里的 `+` 是普通字符，别被 URL 编码咬到）。"""
    repo = _bind(repos, tmp_path / "proj")
    name = f"bot-{value.lower().replace('+', 'p')}"
    assert (
        service.create({"repoId": repo.repo_id, "name": name, "permission": value})["permission"]
        == value
    )


@pytest.mark.parametrize("value", ["L3", "L4"])
def test_create_rejects_l3_and_l4(
    service: BotService, repos: state_db.RepoStore, tmp_path: Path, value: str
) -> None:
    """**验收 3 / 06 §2.2**：L3/L4 **认识但不收**——`NOT_SUPPORTED`，且不创建。"""
    repo = _bind(repos, tmp_path / "proj")
    with pytest.raises(frames.FrameError) as caught:
        service.create({"repoId": repo.repo_id, "name": "builder", "permission": value})
    assert caught.value.code == "NOT_SUPPORTED"
    assert service.list({"repoId": repo.repo_id}) == []


@pytest.mark.parametrize("value", ["L9", "l1", "L2 +", "", 42, [], {"level": "L1"}])
def test_create_rejects_unknown_permission_values(
    service: BotService, repos: state_db.RepoStore, tmp_path: Path, value: object
) -> None:
    """其它一律 `INVALID_PARAMS`——"根本没有这个级别"与"有但不给"是两句不同的话（D2）。"""
    repo = _bind(repos, tmp_path / "proj")
    assert (
        _code(service.create, {"repoId": repo.repo_id, "name": "builder", "permission": value})
        == "INVALID_PARAMS"
    )


def test_unassignable_permissions_are_exactly_l3_and_l4() -> None:
    """拒绝清单只有两个名字（禁止事项：不得把 L3/L4 合并成一级，也不得悄悄加第三个）。"""
    assert frozenset({"L3", "L4"}) == UNASSIGNABLE_PERMISSIONS
    for value in ("L3", "L4"):
        assert permission_parse(value) is None  # 不是枚举成员：不参与任何大小比较


def test_permission_parse_table() -> None:
    """`permission_parse`：只有四个成员解析得出来；其余（含 lone surrogate）都是 `None`。"""
    for member in Permission:
        assert permission_parse(member.value) is member
    values: tuple[object, ...] = (None, "", "l1", "L2 +", "L3", "L4", "L9", 42, [], "\udcff")
    for value in values:
        assert permission_parse(value) is None, value


def test_permission_covers_is_a_ladder() -> None:
    """**D2 的包含关系**：持 L2 具备 L1；反向不成立；L3/L4/垃圾输入一律 `False`（失败即收紧）。"""
    assert permission_covers("L2", "L1") is True
    assert permission_covers("L2+", "L0") is True
    assert permission_covers("L1", "L1") is True
    assert permission_covers("L0", "L1") is False
    assert permission_covers("L1", "L2") is False
    assert permission_covers("L2+", "L3") is False  # 另一根轴，不假装有序
    assert permission_covers("L3", "L0") is False
    assert permission_covers(None, "L0") is False
    assert permission_covers("L0", 42) is False


# ---------------------------------------------------------------------------
# D3 / D4：agentKind 与撞名
# ---------------------------------------------------------------------------


def test_create_accepts_null_agent_kind_and_refuses_a_string(
    service: BotService, repos: state_db.RepoStore, tmp_path: Path
) -> None:
    """**D4**：`agentKind` 只收缺省/`null`；给名字 → `NOT_SUPPORTED`（S9 之前没有验证过的通道）。"""
    repo = _bind(repos, tmp_path / "proj")
    assert service.create({"repoId": repo.repo_id, "name": "a"})["agentKind"] is None
    explicit_null = service.create({"repoId": repo.repo_id, "name": "b", "agentKind": None})
    assert explicit_null["agentKind"] is None
    assert _code(service.create, {"repoId": repo.repo_id, "name": "c", "agentKind": "codex"}) == (
        "NOT_SUPPORTED"
    )
    assert _code(service.create, {"repoId": repo.repo_id, "name": "d", "agentKind": 42}) == (
        "INVALID_PARAMS"
    )
    assert [row["name"] for row in service.list({"repoId": repo.repo_id})] == ["a", "b"]


def test_create_duplicate_name_is_already_exists(
    service: BotService, repos: state_db.RepoStore, bots: state_db.BotStore, tmp_path: Path
) -> None:
    """**验收 1 的反面**：同一个仓库同名 → `ALREADY_EXISTS`，且**不产生第二行**。"""
    repo = _bind(repos, tmp_path / "proj")
    service.create({"repoId": repo.repo_id, "name": "builder"})
    with pytest.raises(frames.FrameError) as caught:
        service.create({"repoId": repo.repo_id, "name": "builder"})
    assert caught.value.code == ALREADY_EXISTS
    assert caught.value.message == MSG_ALREADY_EXISTS
    assert len(bots.list_for_repo(repo.repo_id)) == 1


def test_same_name_in_two_repos_is_allowed(
    service: BotService, repos: state_db.RepoStore, tmp_path: Path
) -> None:
    """复合主键 `{repo_id}/{name}`：两个仓库各有 `builder` 是正常的，不是撞名。"""
    one = _bind(repos, tmp_path / "one", name="one")
    two = _bind(repos, tmp_path / "two", name="two")
    a = service.create({"repoId": one.repo_id, "name": "builder"})
    b = service.create({"repoId": two.repo_id, "name": "builder"})
    assert a["botId"] != b["botId"]
    assert len(service.list({"repoId": one.repo_id})) == 1
    assert len(service.list({"repoId": two.repo_id})) == 1


# ---------------------------------------------------------------------------
# 验收 5：记忆空间与 charter 模板
# ---------------------------------------------------------------------------


def test_create_makes_memory_space_with_template_charter(
    umask_022: None, service: BotService, repos: state_db.RepoStore, tmp_path: Path
) -> None:
    """**验收 5**：建 Bot 就建出 `charter.md` / `memory/` / `sessions/`，权限 0700 / 0600。"""
    repo = _bind(repos, tmp_path / "proj")
    memory = Path(str(service.create({"repoId": repo.repo_id, "name": "builder"})["memoryPath"]))
    for name in (paths.CHARTER_FILE, paths.MEMORY_DIR, paths.SESSIONS_DIR):
        assert (memory / name).exists(), name
    assert (memory / paths.CHARTER_FILE).read_text(encoding="utf-8") == (
        paths.CHARTER_TEMPLATE.format(name="builder")
    )
    for directory in (
        paths.bots_root(),
        memory.parent,
        memory,
        memory / paths.MEMORY_DIR,
        memory / paths.SESSIONS_DIR,
    ):
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700, directory
    assert stat.S_IMODE((memory / paths.CHARTER_FILE).stat().st_mode) == 0o600


def test_create_does_not_overwrite_an_existing_charter(
    service: BotService, repos: state_db.RepoStore, state_home: Path, tmp_path: Path
) -> None:
    """先手写 charter、再删掉行、再用同名建一次：**文件仍是我写的那份**（S6 D3 不覆盖）。

    这条同时钉住 D6 第 6 步的"幂等"：`ensure_memory_space` 从不覆盖已有 charter。
    """
    repo = _bind(repos, tmp_path / "proj")
    payload = service.create({"repoId": repo.repo_id, "name": "builder"})
    charter = Path(str(payload["memoryPath"])) / paths.CHARTER_FILE
    edited = "# 我自己写的\n\n## 职责\n\n别动我。\n"
    paths.atomic_write(charter, edited.encode("utf-8"))
    # 把行删掉（模拟"上次创建到一半失败了"），再建同名。单独开一条连接：只制造中间态，
    # 不碰被测代码的 store（那会把"锁"也一起测进去）。
    conn = sqlite3.connect(state_home / "state.db")
    try:
        with conn:
            conn.execute("DELETE FROM bots WHERE bot_id = ?", (payload["botId"],))
    finally:
        conn.close()
    service.create({"repoId": repo.repo_id, "name": "builder"})
    assert charter.read_text(encoding="utf-8") == edited


# ---------------------------------------------------------------------------
# U17 第一半：bot.list 是纯读
# ---------------------------------------------------------------------------


def test_list_on_a_repo_with_no_bots_writes_nothing(
    service: BotService, repos: state_db.RepoStore, bots: state_db.BotStore, tmp_path: Path
) -> None:
    """**U17 / S8 D7**：空仓库 `bot.list` → `[]`，**不落行、不建目录**（S6 的惰性播种已删）。"""
    repo = _bind(repos, tmp_path / "proj")
    assert service.list({"repoId": repo.repo_id}) == []
    assert service.list({"repoId": repo.repo_id}) == []
    assert bots.list_for_repo(repo.repo_id) == []
    assert not (paths.bots_root() / repo.repo_id).exists()


# ---------------------------------------------------------------------------
# 验收 7：改权限
# ---------------------------------------------------------------------------


def test_set_permission_changes_the_value(
    service: BotService, repos: state_db.RepoStore, bots: state_db.BotStore, tmp_path: Path
) -> None:
    """**验收 7**：改完载荷与列表都是新值，库里也真的变了。"""
    repo = _bind(repos, tmp_path / "proj")
    created = service.create({"repoId": repo.repo_id, "name": "builder"})
    changed = service.set_permission({"botId": created["botId"], "permission": "L2"})
    assert changed["permission"] == "L2"
    assert service.list({"repoId": repo.repo_id})[0]["permission"] == "L2"
    row = bots.get(str(created["botId"]))
    assert row is not None and row.permission == "L2"
    assert changed["botId"] == created["botId"]
    assert set(changed) == set(created)  # 同一个 8 字段载荷


def test_set_permission_is_idempotent(
    service: BotService, repos: state_db.RepoStore, tmp_path: Path
) -> None:
    """同值再设一次：成功、载荷不变（不制造一次无意义的写）。"""
    repo = _bind(repos, tmp_path / "proj")
    created = service.create({"repoId": repo.repo_id, "name": "builder"})
    assert service.set_permission({"botId": created["botId"], "permission": "L1"}) == created


@pytest.mark.parametrize("value", ["L3", "L4"])
def test_set_permission_rejects_l3_and_l4_without_writing(
    service: BotService,
    repos: state_db.RepoStore,
    bots: state_db.BotStore,
    tmp_path: Path,
    value: str,
) -> None:
    """**验收 7 的反面**：`NOT_SUPPORTED`，**库里的值一位都没变**。"""
    repo = _bind(repos, tmp_path / "proj")
    created = service.create({"repoId": repo.repo_id, "name": "builder", "permission": "L2"})
    with pytest.raises(frames.FrameError) as caught:
        service.set_permission({"botId": created["botId"], "permission": value})
    assert caught.value.code == "NOT_SUPPORTED"
    row = bots.get(str(created["botId"]))
    assert row is not None and row.permission == "L2"


def test_set_permission_on_unknown_bot_is_invalid_params(
    service: BotService, repos: state_db.RepoStore, tmp_path: Path
) -> None:
    """查不到的 `botId` → `INVALID_PARAMS`（S6 D6 的同一句话，不新增码）。"""
    repo = _bind(repos, tmp_path / "proj")
    assert (
        _code(service.set_permission, {"botId": f"{repo.repo_id}/nope", "permission": "L1"})
        == "INVALID_PARAMS"
    )
    assert _code(service.set_permission, {"permission": "L1"}) == "INVALID_PARAMS"
    assert _code(service.set_permission, {"botId": 42, "permission": "L1"}) == "INVALID_PARAMS"


def test_set_permission_requires_a_value(
    service: BotService, repos: state_db.RepoStore, tmp_path: Path
) -> None:
    """`permission` 是必填：缺省 / `null` 都不"默认成 L1"——升级/降级不能靠猜。"""
    repo = _bind(repos, tmp_path / "proj")
    created = service.create({"repoId": repo.repo_id, "name": "builder"})
    for params in (
        {"botId": created["botId"]},
        {"botId": created["botId"], "permission": None},
    ):
        assert _code(service.set_permission, params) == "INVALID_PARAMS"


# ---------------------------------------------------------------------------
# D9 / 帧层错误：仓库状态与 repoId
# ---------------------------------------------------------------------------


def test_create_on_unknown_repo_is_repo_not_bound(service: BotService, tmp_path: Path) -> None:
    """`repoId` 查不到 → `REPO_NOT_BOUND`（S6 D6，不变）。"""
    assert _code(service.create, {"repoId": "0" * 12, "name": "builder"}) == "REPO_NOT_BOUND"


def test_create_rejects_a_bad_repo_id(service: BotService) -> None:
    """`repoId` 缺失 / 非字符串 / 空 → `INVALID_PARAMS`。"""
    for params in ({}, {"repoId": None}, {"repoId": 42}, {"repoId": ""}):
        assert _code(service.create, {**params, "name": "builder"}) == "INVALID_PARAMS"


@pytest.mark.parametrize("shape", ["normal", "bare", "gone"])
def test_create_does_not_care_about_the_repo_shape(
    service: BotService, repos: state_db.RepoStore, tmp_path: Path, shape: str
) -> None:
    """**D9**：`bot.create` 只写状态根，不读 git——普通 / 裸仓库 / 路径已删，都能建。

    理由写在 D9：`repo_id` 由路径决定，仓库回到原路径时这一行就能重新用，不是垃圾数据。
    """
    target = tmp_path / shape
    if shape == "normal":
        target.mkdir()
        _git_init(target, bare=False)
    elif shape == "bare":
        target.mkdir()
        _git_init(target, bare=True)
    repo = _bind(repos, target)
    payload = service.create({"repoId": repo.repo_id, "name": "builder"})
    assert payload["repoId"] == repo.repo_id
    assert Path(str(payload["memoryPath"])).is_dir()


def _git_init(path: Path, *, bare: bool) -> None:
    """建一个真仓库（`create` 不读它，但这几行让"裸仓库"这个状态是真的）。"""
    args = ["git", "init", "-q"] + (["--bare"] if bare else ["--initial-branch=main"]) + [str(path)]
    result = subprocess.run(args, capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr


# ---------------------------------------------------------------------------
# 验收 3 / 6：页面契约（**文本断言**；真实 DOM 与刷新由 s8_create_bot.mjs 验）
# ---------------------------------------------------------------------------


def _select_block(page: str, element_id: str) -> str:
    """抠出某个 `<select …>…</select>` 的整段（用于断言"选项里没有 L3/L4"）。"""
    start = page.index(f'id="{element_id}"')
    open_tag = page.rindex("<select", 0, start)
    end = page.index("</select>", start)
    return page[open_tag:end]


def test_page_permission_select_lists_exactly_four_levels() -> None:
    """**验收 3**：两个下拉都恰好四个选项、顺序固定、默认选中 L1，且**没有 L3/L4**。"""
    page = assets.INDEX_HTML
    expected = [
        ("L0", "L0 · 只读"),
        ("L1", "L1 · 改工作区（默认）"),
        ("L2", "L2 · 推分支"),
        ("L2+", "L2+ · 开本仓库 PR"),
    ]
    for element_id in ("bot-create-permission", "charter-permission"):
        block = _select_block(page, element_id)
        assert block.count("<option") == 4, element_id
        for value, label in expected:
            assert f'value="{value}"' in block, (element_id, value)
            assert label in block, (element_id, label)
        assert "L3" not in block and "L4" not in block, element_id  # 连灰掉都不许出现
        positions = [block.index(f'value="{value}"') for value, _ in expected]
        assert positions == sorted(positions), "顺序即契约（rank 递增）"
    # 只有新建表单需要 selected：charter 面板的值来自 bot.list（D8）
    assert 'value="L1" selected' in _select_block(page, "bot-create-permission")


def test_page_has_the_create_form_dom_contract() -> None:
    """**验收 1 / D8**：按钮、表单、输入、两个按钮、错误行都在，且表单初始是收起的。"""
    page = assets.INDEX_HTML
    for fragment in (
        '<button id="bot-new" type="button">新建 Bot</button>',
        '<form id="bot-create-form" autocomplete="off" hidden>',
        'id="bot-create-name"',
        '<button id="bot-create-submit" type="submit" disabled>创建</button>',
        '<button id="bot-create-cancel" type="button">取消</button>',
        '<p id="bot-create-error" role="alert" hidden></p>',
        '<p id="charter-permission-row">',
        'id="charter-permission"',
    ):
        assert fragment in page, fragment
    assert page.index('id="bot-new"') < page.index('id="bot-create-form"')  # 按钮在表单之前


def test_page_js_wires_the_form_without_inner_html() -> None:
    """D8 的行为接线：提交走 form、Escape 收表单、改权限挂在 change 上、文案只进 textContent。"""
    page = assets.INDEX_HTML
    for fragment in (
        'botNew.addEventListener("click", openBotCreate);',
        'botCreateForm.addEventListener("submit", function (ev) {',
        'botCreateCancel.addEventListener("click", function () { closeBotCreate(); });',
        # Escape 必须绑在 document 上（T41：提交时按钮 disabled 会把焦点丢给 body）
        'document.addEventListener("keydown", function (ev) {',
        'charterPermission.addEventListener("change", submitPermissionChange);',
        'request("bot.create", {',
        'request("bot.set_permission", { botId: botId, permission: wanted }',
        "botCreateError.textContent = botCreateErrorText(err);",
        "loadBots();   // 左栏徽标由 bot.list 渲染：一处事实源，两处一起更新",
    ):
        assert fragment in page, fragment
    assert "innerHTML" not in page


def test_error_text_table_knows_the_new_code() -> None:
    """`ALREADY_EXISTS` 进的是**面板自己的**映射，不是全局 `ERROR_TEXT`（后者属仓库/ref 场景）。"""
    page = assets.INDEX_HTML
    assert '"ALREADY_EXISTS": "这个仓库已经有叫这个名字的 Bot"' not in page  # 不在全局表里
    assert 'if (code === "ALREADY_EXISTS") { return "这个仓库已经有叫这个名字的 Bot"; }' in page
    assert "第一版不支持这个权限级别（L3 / L4）" in page
    assert errors.ERROR_TEXT["NOT_SUPPORTED"] == "仓库含非 UTF-8 的名称，第一版不支持"


# ---------------------------------------------------------------------------
# 端到端：真内核 + 真 WS 帧（零 mock）
# ---------------------------------------------------------------------------


@pytest.fixture
def gw(state_home: Path) -> Iterator[Kernel]:
    with Kernel(state_home) as kernel:
        yield kernel


def test_ws_bot_create_and_set_permission_frames(gw: Kernel, tmp_path: Path) -> None:
    """**验收 1/7 的帧层**：两个新方法真的挂在网关上，错误以 `ok:false` + 码回来。"""
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    _git_init(repo_path, bare=False)

    async def run() -> dict[str, Any]:
        ticket = gw.fetch_page()[0]

        async def call(
            ws: Any, rid: str, method: str, params: dict[str, object] | None
        ) -> dict[str, object]:
            message: dict[str, object] = {"type": "req", "id": rid, "method": method}
            if params is not None:
                message["params"] = params
            await ws.send(json.dumps(message))
            return decode_frame(await ws.recv())

        async with websockets.connect(gw.ws_url(ticket)) as ws:
            bound = frame_result(await call(ws, "c1", "repo.bind", {"path": str(repo_path)}))
            bound_data = bound["data"]
            assert isinstance(bound_data, dict)
            repo_id_value = bound_data["repoId"]
            assert isinstance(repo_id_value, str)
            created = frame_result(
                await call(
                    ws,
                    "c2",
                    "bot.create",
                    {"repoId": repo_id_value, "name": "builder", "permission": "L2"},
                )
            )
            created_data = created["data"]
            assert isinstance(created_data, dict)
            created_bot_id = created_data["botId"]
            assert isinstance(created_bot_id, str)
            duplicate = await call(
                ws, "c3", "bot.create", {"repoId": repo_id_value, "name": "builder"}
            )
            unsupported = await call(
                ws,
                "c4",
                "bot.set_permission",
                {"botId": created_bot_id, "permission": "L3"},
            )
            changed = frame_result(
                await call(
                    ws,
                    "c5",
                    "bot.set_permission",
                    {"botId": created_bot_id, "permission": "L1"},
                )
            )
            return {
                "repo_id": repo_id_value,
                "created": created,
                "duplicate": duplicate,
                "unsupported": unsupported,
                "changed": changed,
            }

    result = asyncio.run(run())
    created_data = result["created"]["data"]
    changed_data = result["changed"]["data"]
    assert isinstance(created_data, dict) and isinstance(changed_data, dict)
    assert created_data["permission"] == "L2"
    assert result["duplicate"]["ok"] is False
    assert result["duplicate"]["error"]["code"] == "ALREADY_EXISTS"
    assert result["unsupported"]["ok"] is False
    assert result["unsupported"]["error"]["code"] == "NOT_SUPPORTED"
    assert changed_data["permission"] == "L1"
