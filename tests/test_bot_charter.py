"""S6 · Bot 与 charter（`bot.list` / `memory.charter.get`）：真 SQLite + 真文件 + 真 WS 帧。

一条用例对应 `docs/design/issues/S6-bot-charter.md` 的 **D11 测试矩阵**第 1–14 行
（行号写在每个用例的 docstring 里；15 在 `tests/test_accept_m1.py`，16 在
`tests/test_reverse_checks.py`）。这里**不 mock**：SQLite 是真文件、记忆空间是真目录、
端到端那条起真内核子进程 + 真 WebSocket。

D3 的 umask 陷阱（T38）是权限用例的核心：`mkdir(mode=0o700)` 的 mode 只作用于**叶子**，
父级按 umask 给 0755；`Path.write_text` 建出来是 0644。所以权限用例显式把 umask 设成
文档实测的 `0o022` 再断言真实 `stat` mode——不设的话，"本机 umask 恰好是 077"会让
逆向注入（去掉 chmod）意外变绿。
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

from postel_studio.api import frames
from postel_studio.api.bots import DEFAULT_SUMMARY, BotService, charter_summary
from postel_studio.domain.models import (
    BOT_NAME_RE,
    Repo,
    bot_id,
    bot_name_ok,
    repo_id,
)
from postel_studio.storage import db as state_db
from postel_studio.storage import paths
from postel_studio.web import assets

from .gateway_util import Kernel, decode_frame, frame_result, frame_seq

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


def _state_home(state_home: Path) -> Path:
    """`conftest.state_home` 已经把 `POSTEL_HOME` 指到这里；本文件只需要路径本身。"""
    return state_home


# ---------------------------------------------------------------------------
# D11 第 11 行：名字白名单（06 §2.2 / D7 的 L0 纯函数）
# ---------------------------------------------------------------------------


def test_bot_name_whitelist() -> None:
    """**D11 第 11 行**：`../x` / `Builder` / `a_b` / 33 字符 → 拒绝；`builder` / `qa-1` → 接受。"""
    assert BOT_NAME_RE.pattern == r"^[a-z][a-z0-9-]{0,31}$"
    for bad in ("../x", "Builder", "a_b", "x" * 33, "", "-a", "a b", "a.b", "a/b", ".", ".."):
        assert bot_name_ok(bad) is False, bad
    for good in ("builder", "qa-1", "a", "a" * 32):
        assert bot_name_ok(good) is True, good
    assert bot_id("adec6540a725", "builder") == "adec6540a725/builder"


# ---------------------------------------------------------------------------
# D11 第 12 行：v2 → v3 迁移（只加表、不动 repos；D7 要求老库升级后 repos 原样还在）
# ---------------------------------------------------------------------------


def test_v2_database_upgrades_to_v3_and_keeps_repos(tmp_path: Path) -> None:
    """**D11 第 12 行**：造一个有 `repos` 行的 v2 库 → 升级后 repos 行还在、bots 表可用。"""
    db_path = tmp_path / "state.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(state_db._MIGRATIONS[0])  # v1：repos
    conn.executescript(state_db._MIGRATIONS[1])  # v2：origin_hash
    conn.execute(
        "INSERT INTO repos (repo_id, path, origin_url, default_branch, added_at, origin_hash)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        ("deadbeefcafe", str(tmp_path / "repo"), None, "main", 1.5, None),
    )
    conn.execute("PRAGMA user_version=2")
    conn.commit()
    conn.close()

    opened = state_db.open_state_db(db_path)
    try:
        assert int(opened.execute("PRAGMA user_version").fetchone()[0]) == 3
        assert state_db.SCHEMA_VERSION == 3
        row = opened.execute("SELECT repo_id, path, added_at FROM repos").fetchone()
        assert row is not None
        assert (row[0], row[1], row[2]) == ("deadbeefcafe", str(tmp_path / "repo"), 1.5)
        # bots 表可用（v3 迁移真的建了表，不是只改了个版本号）
        opened.execute(
            "INSERT INTO bots (bot_id, repo_id, name, permission, agent_kind,"
            " charter_rev, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("deadbeefcafe/builder", "deadbeefcafe", "builder", "L1", None, 0, 2.0),
        )
        assert opened.execute("SELECT COUNT(*) FROM bots").fetchone()[0] == 1
    finally:
        opened.close()


# ---------------------------------------------------------------------------
# D11 第 4/5/2 行（L0 侧）：记忆空间的布局、权限、幂等
# ---------------------------------------------------------------------------


def test_ensure_memory_space_layout_and_permissions(state_home: Path, umask_022: None) -> None:
    """**D11 第 4/5 行**：目录含 charter.md/memory/sessions；目录链全 0700、charter 0600。

    第 5 行明确要求「用 `stat` 断言真实 mode」，所以断言的是 `stat.S_IMODE`，不是建目录时
    传的 `mode=` 参数（T38：那个参数只管叶子）。
    """
    home = _state_home(state_home)
    memory = paths.ensure_memory_space("adec6540a725", "builder")
    assert memory == paths.bots_root() / "adec6540a725" / "builder"
    assert memory.is_relative_to(paths.bots_root())
    for name in (paths.CHARTER_FILE, paths.MEMORY_DIR, paths.SESSIONS_DIR):
        assert (memory / name).exists(), name
    for directory in (
        paths.bots_root(),
        paths.bots_root() / "adec6540a725",
        memory,
        memory / paths.MEMORY_DIR,
        memory / paths.SESSIONS_DIR,
    ):
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700, directory
    charter = memory / paths.CHARTER_FILE
    assert stat.S_IMODE(charter.stat().st_mode) == 0o600
    # D4：骨架空白待填，只有标题；末尾一个换行。
    assert charter.read_text(encoding="utf-8") == paths.CHARTER_TEMPLATE.format(name="builder")
    assert charter.read_text(encoding="utf-8").endswith("\n")
    assert home == paths.state_root()


def test_charter_template_has_no_business_duties() -> None:
    """D4 的禁止事项：模板只有标题骨架，**不预设任何业务职责**。"""
    body = paths.CHARTER_TEMPLATE.format(name="builder")
    headings = [line.strip() for line in body.splitlines() if line.startswith("## ")]
    assert headings == ["## 身份", "## 职责", "## 边界", "## 工作方式"]
    others = [line for line in body.splitlines() if line.strip() and not line.startswith("#")]
    assert others == []


def test_ensure_memory_space_never_overwrites_an_existing_charter(
    state_home: Path, umask_022: None
) -> None:
    """**D11 第 2 行（L0 侧）**：charter 已存在就只读不写，第二次调用逐字不变。"""
    _state_home(state_home)
    memory = paths.ensure_memory_space("adec6540a725", "builder")
    charter = memory / paths.CHARTER_FILE
    edited = "# builder\n\n## 职责\n\n审查每一行改动。\n"
    paths.atomic_write(charter, edited.encode("utf-8"))

    again = paths.ensure_memory_space("adec6540a725", "builder")
    assert again == memory
    assert charter.read_text(encoding="utf-8") == edited


# ---------------------------------------------------------------------------
# D11 第 10 行（L0 侧）：名字进路径前的白名单 + 越界检查
# ---------------------------------------------------------------------------


def test_ensure_memory_space_rejects_names_that_escape_bots_root(state_home: Path) -> None:
    """**D11 第 10 行（L0 侧）**：坏名字一律 `ValueError`，且**一个文件都不创建**。

    `..` 是这条用例里最关键的负载：它 `resolve()` 之后**仍在** `bots_root()` 之下（等于
    bots 根本身），所以纵深防御的越界断言拦不住它——挡住它的是白名单（D12 #4 注入点）。
    """
    home = _state_home(state_home)
    bad_names = ("../../etc/passwd", "..", ".", "Builder", "a_b", "x" * 33, "a/b", "")
    for name in bad_names:
        with pytest.raises(ValueError):
            paths.ensure_memory_space("adec6540a725", name)
    # 越界企图没有在状态根内外创建任何东西（状态根本身都不该被建出来）
    assert home.exists() is False
    assert (home.parent / "etc").exists() is False
    with pytest.raises(ValueError):
        paths.bot_memory_dir("adec6540a725", "../../etc/passwd")


# ---------------------------------------------------------------------------
# 服务层脚手架：真 SQLite（`state_home` 隔离）+ 真 git（只给端到端那条用）
# ---------------------------------------------------------------------------


@pytest.fixture
def stores(state_home: Path) -> Iterator[tuple[state_db.BotStore, state_db.RepoStore]]:
    """两个真 SQLite store（同一个临时库文件；不碰真实状态根）。"""
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
def service(stores: tuple[state_db.BotStore, state_db.RepoStore]) -> BotService:
    bots, repos = stores
    return BotService(bots, repos)


def _add_repo(repos: state_db.RepoStore, path: Path, *, repo_id_value: str | None = None) -> Repo:
    """往库里放一条已绑定仓库（Bot 只依赖这条元数据，不需要真仓库目录）。"""
    repo = Repo(
        repo_id=repo_id_value or repo_id(str(path)),
        path=str(path),
        origin_url=None,
        default_branch="main",
        added_at=1.0,
    )
    return repos.upsert(repo)


def _payload(service: BotService, repo_id_value: str) -> dict[str, object]:
    payloads = service.list({"repoId": repo_id_value})
    assert len(payloads) == 1
    return payloads[0]


def _seed(repos: state_db.RepoStore, tmp_path: Path, name: str = "repo-a") -> Repo:
    return _add_repo(repos, tmp_path / name)


# ---------------------------------------------------------------------------
# D11 第 1/2/3 行：惰性播种、幂等不覆盖、仓库隔离
# ---------------------------------------------------------------------------


def test_first_bot_list_seeds_preset_builder(
    service: BotService, repos: state_db.RepoStore, tmp_path: Path
) -> None:
    """**D11 第 1 行**：首次 `bot.list` 播种 1 个 `builder`；再调一次仍是 1 个（D2 幂等）。"""
    repo = _seed(repos, tmp_path)
    first = service.list({"repoId": repo.repo_id})
    assert len(first) == 1
    payload = first[0]
    assert payload["name"] == "builder"
    assert payload["permission"] == "L1"
    assert payload["agentKind"] is None
    assert payload["charterSummary"] == DEFAULT_SUMMARY
    assert isinstance(payload["charterRev"], int) and payload["charterRev"] > 0
    assert service.list({"repoId": repo.repo_id}) == first  # 不重复播种、载荷也一致


def test_bot_list_never_overwrites_an_edited_charter(
    service: BotService, repos: state_db.RepoStore, tmp_path: Path
) -> None:
    """**D11 第 2 行**：手写 charter → 再 `bot.list` + `charter.get` → 内容**逐字未变**。"""
    repo = _seed(repos, tmp_path)
    payload = _payload(service, repo.repo_id)
    charter = Path(str(payload["memoryPath"])) / paths.CHARTER_FILE
    edited = "# builder\n\n## 职责\n\n审查每一行改动。\n"
    paths.atomic_write(charter, edited.encode("utf-8"))

    after = service.list({"repoId": repo.repo_id})
    assert len(after) == 1  # 不再新建
    assert after[0]["botId"] == payload["botId"]
    assert after[0]["charterSummary"] == "审查每一行改动。"
    assert service.charter_get({"botId": str(payload["botId"])})["content"] == edited
    assert charter.read_text(encoding="utf-8") == edited


def test_two_repos_have_isolated_builders_and_charters(
    service: BotService, repos: state_db.RepoStore, tmp_path: Path
) -> None:
    """**D11 第 3 行**：两个 repo 各播种 `builder`、各写不同 charter → 各回各的。"""
    repo_a = _seed(repos, tmp_path, "repo-a")
    repo_b = _seed(repos, tmp_path, "repo-b")
    payload_a = _payload(service, repo_a.repo_id)
    payload_b = _payload(service, repo_b.repo_id)
    assert payload_a["botId"] != payload_b["botId"]
    assert payload_a["memoryPath"] != payload_b["memoryPath"]

    for payload, text in (
        (payload_a, "# builder\n\n## 职责\n\nA 的职责。\n"),
        (payload_b, "# builder\n\n## 职责\n\nB 的职责。\n"),
    ):
        charter = Path(str(payload["memoryPath"])) / paths.CHARTER_FILE
        paths.atomic_write(charter, text.encode("utf-8"))
    content_a = service.charter_get({"botId": str(payload_a["botId"])})["content"]
    content_b = service.charter_get({"botId": str(payload_b["botId"])})["content"]
    assert isinstance(content_a, str) and "A 的职责。" in content_a
    assert isinstance(content_b, str) and "B 的职责。" in content_b


# ---------------------------------------------------------------------------
# D11 第 4/5 行：memoryPath 布局 / 权限（D9：绝对路径，不伪装 ~）
# ---------------------------------------------------------------------------


def test_memory_path_is_absolute_outside_the_repo_and_exists(
    service: BotService, repos: state_db.RepoStore, tmp_path: Path
) -> None:
    """**D11 第 4 行**：`memoryPath` 在 `bots_root()` 下、**不在仓库路径内**、目录真实存在。"""
    repo = _seed(repos, tmp_path)
    memory = Path(str(_payload(service, repo.repo_id)["memoryPath"]))
    assert memory.is_absolute()  # D9：显示绝对路径，不伪装成 ~
    assert str(memory).startswith(str(paths.state_root()))
    assert memory.is_relative_to(paths.bots_root())
    assert not memory.is_relative_to(Path(repo.path))
    for name in (paths.CHARTER_FILE, paths.MEMORY_DIR, paths.SESSIONS_DIR):
        assert (memory / name).exists(), name


def test_seeded_bot_permissions_are_0700_and_0600(
    umask_022: None, service: BotService, repos: state_db.RepoStore, tmp_path: Path
) -> None:
    """**D11 第 5 行**：播种出来的目录链全 `0700`、`charter.md` `0600`（真 stat，T38）。"""
    repo = _seed(repos, tmp_path)
    memory = Path(str(_payload(service, repo.repo_id)["memoryPath"]))
    for directory in (
        paths.bots_root(),
        memory.parent,
        memory,
        memory / paths.MEMORY_DIR,
        memory / paths.SESSIONS_DIR,
    ):
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700, directory
    assert stat.S_IMODE((memory / paths.CHARTER_FILE).stat().st_mode) == 0o600


# ---------------------------------------------------------------------------
# D11 第 6/7 行：charterRev 与摘要五条
# ---------------------------------------------------------------------------


def test_charter_rev_changes_with_content_and_is_deterministic(
    service: BotService, repos: state_db.RepoStore, tmp_path: Path
) -> None:
    """**D11 第 6 行**：载荷 `{content, rev}`；改内容 rev 变、不改则 rev 相同（确定性）。"""
    repo = _seed(repos, tmp_path)
    payload = _payload(service, repo.repo_id)
    bot_id_value = str(payload["botId"])
    first = service.charter_get({"botId": bot_id_value})
    assert first == service.charter_get({"botId": bot_id_value})
    rev = first["rev"]
    assert isinstance(rev, int) and not isinstance(rev, bool) and rev > 0
    assert rev == payload["charterRev"]

    charter = Path(str(payload["memoryPath"])) / paths.CHARTER_FILE
    paths.atomic_write(charter, "# builder\n\n## 职责\n\nnew\n".encode())
    changed = service.charter_get({"botId": bot_id_value})["rev"]
    assert isinstance(changed, int)
    assert changed != rev
    assert service.charter_get({"botId": bot_id_value})["rev"] == changed


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        # 5：没有任何正文 → 固定文案（不是空串、不编造）
        ("# builder\n\n## 身份\n\n## 职责\n\n## 边界\n\n## 工作方式\n", DEFAULT_SUMMARY),
        ("", DEFAULT_SUMMARY),
        # 1：跳过标题 / 空行 / HTML 注释（整段，可多行）
        ("<!-- 待填\n职责：不该出现 -->\n\n## 职责\n\n真正的职责\n", "真正的职责"),
        # 2：取第一个正文段落，多行拼成一段（Markdown 软换行 = 空格）
        ("# b\n\n## 职责\n\n第一行\n第二行\n\n下一段不要\n", "第一行 第二行"),
        # 3：去掉行内强调标记 ** / * / _ / `
        ("## 职责\n\n**审查** `代码` _并_ 写测试\n", "审查 代码 并 写测试"),
        # 4：超过 80 字符截断加 …（按字符数）；恰好 80 不截断
        ("## 职责\n\n" + "x" * 100 + "\n", "x" * 80 + "…"),
        ("## 职责\n\n" + "y" * 80 + "\n", "y" * 80),
    ],
)
def test_charter_summary_follows_the_five_rules(content: str, expected: str) -> None:
    """**D11 第 7 行**：D5 摘要五条规则，逐条覆盖。"""
    assert charter_summary(content) == expected


# ---------------------------------------------------------------------------
# D11 第 8/9/10 行：载荷 8 字段、错误码、路径穿越
# ---------------------------------------------------------------------------


def test_bot_payload_has_exactly_eight_fields(
    service: BotService, repos: state_db.RepoStore, tmp_path: Path
) -> None:
    """**D11 第 8 行**：`set(payload)` 恰好是 06/07 的八个字段，`agentKind is None`。"""
    repo = _seed(repos, tmp_path)
    payload = _payload(service, repo.repo_id)
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
    assert payload["agentKind"] is None


def test_bot_error_codes(service: BotService, repos: state_db.RepoStore, tmp_path: Path) -> None:
    """**D11 第 9 行**：repoId 形状错 → INVALID_PARAMS；查不到 → REPO_NOT_BOUND；
    botId 形状错 / 查不到 → INVALID_PARAMS（D6：一个码都不新增）。"""
    repo = _seed(repos, tmp_path)
    bad_repo_params: tuple[dict[str, object], ...] = (
        {},
        {"repoId": None},
        {"repoId": 123},
        {"repoId": ""},
    )
    for params in bad_repo_params:
        with pytest.raises(frames.FrameError) as excinfo:
            service.list(params)
        assert excinfo.value.code == "INVALID_PARAMS", params
    with pytest.raises(frames.FrameError) as excinfo:
        service.list({"repoId": "0" * 12})
    assert excinfo.value.code == "REPO_NOT_BOUND"

    payload = _payload(service, repo.repo_id)
    bad_bot_params: tuple[dict[str, object], ...] = (
        {},
        {"botId": None},
        {"botId": 1},
        {"botId": ""},
    )
    for params in bad_bot_params:
        with pytest.raises(frames.FrameError) as excinfo:
            service.charter_get(params)
        assert excinfo.value.code == "INVALID_PARAMS", params
    for bad in ("abc", f"{repo.repo_id}/nope", "../../etc/passwd", ".."):
        with pytest.raises(frames.FrameError) as excinfo:
            service.charter_get({"botId": bad})
        assert excinfo.value.code == "INVALID_PARAMS", bad
    assert payload["botId"] == f"{repo.repo_id}/builder"


def test_bot_id_traversal_is_rejected_and_creates_nothing(
    service: BotService, repos: state_db.RepoStore, tmp_path: Path
) -> None:
    """**D11 第 10 行**：`botId="../../etc/passwd"` 之类 → `INVALID_PARAMS`，
    且**没有在状态根外创建任何东西**。"""
    repo = _seed(repos, tmp_path)
    _payload(service, repo.repo_id)  # 先播种出真实记忆目录
    before = sorted(p.name for p in tmp_path.iterdir())
    for bad in (
        "../../etc/passwd",
        "../x",
        f"{repo.repo_id}/../x",
        f"{repo.repo_id}/../../etc/passwd",
        "/etc/passwd",
        "..",
    ):
        with pytest.raises(frames.FrameError) as excinfo:
            service.charter_get({"botId": bad})
        assert excinfo.value.code == "INVALID_PARAMS", bad
    assert sorted(p.name for p in tmp_path.iterdir()) == before
    assert (tmp_path / "etc").exists() is False


# ---------------------------------------------------------------------------
# D11 第 13 行：端到端真 WS 帧（真内核子进程 + 真 git 仓库，零 mock）
# ---------------------------------------------------------------------------


@pytest.fixture
def gw(state_home: Path) -> Iterator[Kernel]:
    with Kernel(state_home) as kernel:
        yield kernel


def _git(*args: str, cwd: Path) -> None:
    result = subprocess.run(
        [
            "git",
            "-c",
            "user.email=test@example.invalid",
            "-c",
            "user.name=test",
            "-c",
            "init.defaultBranch=main",
            *args,
        ],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, f"git {args} 失败：{result.stderr}"


def _init_repo(path: Path) -> Path:
    path.mkdir(parents=True)
    _git("init", "-q", "--initial-branch=main", str(path), cwd=path.parent)
    _git("commit", "-q", "--allow-empty", "-m", "init", cwd=path)
    return path


def test_ws_bot_frames_match_service(gw: Kernel, state_home: Path, tmp_path: Path) -> None:
    """**D11 第 13 行**：`bot.list` / `memory.charter.get` 走真 WS 帧，载荷与直调服务层
    一致；同一条连接上 `seq` 严格递增（1,2,3,4）。"""
    repo_path = _init_repo(tmp_path / "repo")

    async def run() -> tuple[str, dict[str, object], dict[str, object], dict[str, object]]:
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
            bound = await call(ws, "c1", "repo.bind", {"path": str(repo_path)})
            bound_data = frame_result(bound)["data"]
            assert isinstance(bound_data, dict)
            repo_id_value = bound_data["repoId"]
            assert isinstance(repo_id_value, str)
            first = await call(ws, "c2", "bot.list", {"repoId": repo_id_value})
            again = await call(ws, "c3", "bot.list", {"repoId": repo_id_value})
            payloads = frame_result(first)["data"]
            assert isinstance(payloads, list) and len(payloads) == 1
            bot_payload = payloads[0]
            assert isinstance(bot_payload, dict)
            bot_id_value = bot_payload["botId"]
            assert isinstance(bot_id_value, str)
            charter = await call(ws, "c4", "memory.charter.get", {"botId": bot_id_value})
            return repo_id_value, first, again, charter

    repo_id_value, first, again, charter = asyncio.run(run())
    assert repo_id_value == repo_id(str(repo_path))
    assert [frame_seq(frame) for frame in (first, again, charter)] == [2, 3, 4]

    payloads = frame_result(first)["data"]
    assert payloads == frame_result(again)["data"]

    # 与直调服务层一致（同一个状态根、同一份 state.db；内核已提交，读得到）
    db_path = state_home / "state.db"
    repos = state_db.RepoStore(db_path)
    bots = state_db.BotStore(db_path)
    try:
        direct = BotService(bots, repos)
        assert direct.list({"repoId": repo_id_value}) == payloads
        bot_payload = payloads[0]
        assert isinstance(bot_payload, dict)
        assert (
            direct.charter_get({"botId": str(bot_payload["botId"])})
            == frame_result(charter)["data"]
        )
    finally:
        bots.close()
        repos.close()


# ---------------------------------------------------------------------------
# D11 第 14 行：页面契约（**文本断言**；真实 DOM 由 tools/acceptance/s6_bots.mjs 验）
# ---------------------------------------------------------------------------


def test_page_has_bots_and_charter_dom_contract() -> None:
    """**D11 第 14 行**：两个面板的 id、文案、DOM 顺序与「一个 `<pre>` 显示全文」（D8）。"""
    page = assets.INDEX_HTML
    for fragment in (
        '<section id="bots" data-state="idle" aria-live="polite">',
        '<h2 id="bots-title">Bots</h2>',
        '<p id="bots-state">选择一个仓库查看它的 Bot</p>',
        '<p id="bots-error" role="alert" hidden></p>',
        '<ul id="bot-list"></ul>',
        '<p id="bot-empty" hidden>这个仓库还没有 Bot。</p>',
        '<section id="charter" data-state="idle" aria-live="polite">',
        '<h2 id="charter-title">Charter</h2>',
        '<p id="charter-state">点左侧的 Bot 查看它的 charter</p>',
        '<p id="charter-error" role="alert" hidden></p>',
        '<p id="charter-memory" hidden></p>',
        '<pre id="charter-content" hidden></pre>',
    ):
        assert fragment in page, fragment
    assert page.index('id="bots"') < page.index('id="repos"')  # D8：左栏上 Bot / 下仓库
    assert page.index('id="charter"') > page.index('id="diff"')  # 主区 charter 在 #diff 之后
    assert "正在读取 Bot…" in page
    assert "已断开：Bot 列表未知" in page
    assert "记忆目录：" in page
    assert "white-space: pre-wrap" in page  # charter 全文可换行、不假设中文宽度


def test_page_renders_bot_rows_and_charter_with_text_content() -> None:
    """**D11 第 14 行**：行结构（`.bot-name` / `.bot-permission` / `.bot-charter-summary`）、
    键盘可达、选中标记、以及 charter 全文只经 `textContent` 进 DOM。"""
    page = assets.INDEX_HTML
    for fragment in (
        "li.dataset.botId = rowId;",
        "li.tabIndex = 0;",
        'li.setAttribute("role", "button");',
        'name.className = "bot-name";',
        'badge.className = "bot-permission";',
        'summary.className = "bot-charter-summary";',
        'rows[i].dataset.selected = "true";',
        'charterTitle.textContent = "Charter · " + botName;',
        'charterMemory.textContent = "记忆目录：" + String(botMemoryPaths[botId] || "");',
        'botMemoryPaths[rowId] = String(bot.memoryPath || "");',
        'charterContent.textContent = String(payload.content == null ? "" : payload.content);',
    ):
        assert fragment in page, fragment
    assert "botRequestToken" in page  # D8：快速切换各自的过期回包拦截
    assert "charterRequestToken" in page


def test_page_never_uses_inner_html_in_bot_panels() -> None:
    """**D11 第 14 行**：全文件 `innerHTML` 零使用。

    charter 是**用户可编辑文本 = 不可信**（05 §4.2 同一精神），进 DOM 一律 `textContent`。
    """
    assert "innerHTML" not in assets.INDEX_HTML
