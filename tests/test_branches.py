"""S4 · 分支列表：`repo.branches`（真实 git + 真 SQLite + 真 WS 帧，**不 mock git**）。

一条用例对应 `docs/design/issues/S4-branch-list.md` 的「D7 测试矩阵」第 1–10 行
（行号写在每个用例的 docstring 里）。解析层的陷阱见 06-pitfalls T33：`%(HEAD)` 对非当前
分支输出**一个空格**、而 `_stdout()` 会把整段输出 `.strip()`，所以**最后一条记录**的
marker 字段可能整个消失——第 3 行（最后一条带 upstream）与第 7 行（color.ui）是这条的回归。

夹具的边界：
- `store` / `service`：**真 SQLite 文件**（`tmp_path` 下），不碰状态根。
- `repo_factory`：真 `git init`。身份与 `init.defaultBranch` 显式给，不依赖测试机的全局配置。
- `gw`：真内核子进程 + 真 WebSocket（复用 S2 的 `tests/gateway_util.py`）。它跑的是
  **已安装**的包、且 `env.pop("PYTHONPATH")`，所以端到端那条只能验"安装版"（T30）；其余
  用例直接调 `RepositoryService` / `gitops`，改源码即时生效。

页面契约的几条是**文本断言**（06-pitfalls T9 的老问题：它们只证明"这句话在页面里"，
不证明"浏览器里真的这样渲染"）。真实 DOM 行为由 `tools/acceptance/s4_branches.mjs` 验。
"""

from __future__ import annotations

import asyncio
import json
import re
import shutil
import subprocess
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
import websockets

from postel_studio.api import frames
from postel_studio.api.repository import RepositoryService
from postel_studio.gitops import repo as gitops
from postel_studio.storage import db as state_db
from postel_studio.web import assets

from .gateway_util import Kernel, decode_frame, frame_result

# 07 §1.3 的码（断言只按码，不看文案）
INVALID_PARAMS = "INVALID_PARAMS"
REPO_NOT_BOUND = "REPO_NOT_BOUND"
ADAPTER_UNAVAILABLE = "ADAPTER_UNAVAILABLE"
INTERNAL = "INTERNAL"

SHA8 = re.compile(r"[0-9a-f]{8}")
SHA40 = re.compile(r"[0-9a-f]{40}")


# ---------------------------------------------------------------------------
# 脚手架：真 git（身份显式给，别依赖跑测试机器的全局配置）
# ---------------------------------------------------------------------------


def _git(*args: str, cwd: Path) -> str:
    """真 `git`（测试自己的脚手架，不是被测代码）。"""
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
    return result.stdout


def _init_repo(path: Path, *, commit: bool = True) -> Path:
    path.mkdir(parents=True)
    _git("init", "-q", "--initial-branch=main", str(path), cwd=path.parent)
    if commit:
        _git("commit", "-q", "--allow-empty", "-m", "init", cwd=path)
    return path


def _bind(service: RepositoryService, path: Path) -> str:
    """绑定一个真仓库，返回 `repoId`（`repo.branches` 的唯一参数）。"""
    payload = service.bind({"path": str(path)})
    repo_id = payload["repoId"]
    assert isinstance(repo_id, str)
    return repo_id


def _branch_rows(service: RepositoryService, repo_id: str) -> list[dict[str, object]]:
    """`repo.branches` 的载荷（数组）。"""
    return service.branches({"repoId": repo_id})


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------


@pytest.fixture
def store(tmp_path: Path) -> Iterator[state_db.RepoStore]:
    """真 SQLite 文件（`tmp_path/state.db`，不碰状态根）。"""
    with state_db.RepoStore(tmp_path / "state.db") as opened:
        yield opened


@pytest.fixture
def service(store: state_db.RepoStore) -> RepositoryService:
    return RepositoryService(store)


@pytest.fixture
def repo_factory(tmp_path: Path) -> Callable[..., Path]:
    """`make(name, *, commit=True) -> Path`：在 `tmp_path/repos/` 下建真仓库。"""
    root = tmp_path / "repos"
    root.mkdir(exist_ok=True)

    def make(name: str = "proj", *, commit: bool = True) -> Path:
        return _init_repo(root / name, commit=commit)

    return make


# ---------------------------------------------------------------------------
# D7 第 1 行：多分支（main + feature + 中文分支）
# ---------------------------------------------------------------------------


def test_branches_returns_names_in_refname_order_with_short_shas(
    service: RepositoryService, repo_factory: Callable[..., Path]
) -> None:
    """**D7 第 1 行**（验收 1/3，D1/D4）：名称集合、顺序稳定、sha 是 8 位小写 hex。

    同时断言 gitops 层拿到的是**完整 40 位**：截 8 位是帧层边界格式化，两层各自可测（D4）。
    """
    repo = repo_factory("multi")
    _git("branch", "feature", cwd=repo)
    _git("branch", "中文分支", cwd=repo)

    full = gitops.list_branches(repo)
    assert full is not None
    assert [branch.name for branch in full] == ["feature", "main", "中文分支"]
    assert all(SHA40.fullmatch(branch.sha) for branch in full), "D4：gitops 层存完整 40 位"

    rows = _branch_rows(service, _bind(service, repo))

    names = [str(row["name"]) for row in rows]
    # D1：按 refname 字典序，不把当前分支提到最前
    assert names == sorted(names) == ["feature", "main", "中文分支"]
    for row in rows:
        assert SHA8.fullmatch(str(row["sha"]))
        # D1：四字段，不多不少
        assert set(row) == {"name", "sha", "isCurrent", "upstream"}


# ---------------------------------------------------------------------------
# D7 第 2 行：当前分支标记
# ---------------------------------------------------------------------------


def test_branch_current_flag_is_only_on_the_head_branch(
    service: RepositoryService, repo_factory: Callable[..., Path]
) -> None:
    """**D7 第 2 行**（验收 2，D1）：只有 HEAD 指向的分支 `isCurrent=True`。"""
    repo = repo_factory("current")
    _git("branch", "feature", cwd=repo)

    rows = _branch_rows(service, _bind(service, repo))

    assert {str(row["name"]): row["isCurrent"] for row in rows} == {
        "feature": False,
        "main": True,
    }


# ---------------------------------------------------------------------------
# D7 第 3 行：upstream（真 --bare remote + push -u）
# ---------------------------------------------------------------------------


def _add_bare_remote(repo: Path, tmp_path: Path) -> Path:
    """给 `repo` 造一个**真的** bare remote 并把 `main` 推上去（`-u` 设置 upstream）。"""
    bare = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True, capture_output=True)
    _git("remote", "add", "origin", str(bare), cwd=repo)
    _git("push", "-q", "-u", "origin", "main", cwd=repo)
    return bare


def test_branch_upstream_is_short_name_and_none_when_missing(
    service: RepositoryService, repo_factory: Callable[..., Path], tmp_path: Path
) -> None:
    """**D7 第 3 行**（D1）：有 upstream 给 `origin/main`，没有给 `None`（不是 `""`）。

    这条同时是 **T33** 的回归：`main` 的 refname 在 `feature` 之后（最后一条记录）且
    **有** upstream——整段 `.strip()` 会把它的 marker 空格连同字段分隔吃掉，解析必须能
    把"缺第 4 字段"读成"不是当前分支"而不是整体失败。
    """
    repo = repo_factory("with-upstream")
    _git("branch", "feature", cwd=repo)
    _add_bare_remote(repo, tmp_path)

    rows = _branch_rows(service, _bind(service, repo))

    by_name = {str(row["name"]): row for row in rows}
    assert by_name["main"]["upstream"] == "origin/main"
    assert by_name["feature"]["upstream"] is None
    assert by_name["main"]["isCurrent"] is True
    assert by_name["feature"]["isCurrent"] is False


# ---------------------------------------------------------------------------
# D7 第 4 行：空仓库（git init 之后不 commit）
# ---------------------------------------------------------------------------


def test_empty_repo_returns_empty_list_as_success(
    service: RepositoryService, repo_factory: Callable[..., Path]
) -> None:
    """**D7 第 4 行**（验收 4，D5）：空仓库 `data==[]`，**成功**而非报错。

    空列表与 `None` 是两件事（D4 的三态不许合并）：前者"git 答了，0 个分支"，
    后者"这不是仓库 / 路径没了"。
    """
    repo = repo_factory("fresh", commit=False)

    assert gitops.list_branches(repo) == []  # gitops 层：空列表，不是 None
    assert _branch_rows(service, _bind(service, repo)) == []


# ---------------------------------------------------------------------------
# D7 第 5 行：游离 HEAD
# ---------------------------------------------------------------------------


def test_detached_head_still_lists_all_branches_with_none_current(
    service: RepositoryService, repo_factory: Callable[..., Path]
) -> None:
    """**D7 第 5 行**（D1）：游离 HEAD 时仍列出全部分支，且**全部** `isCurrent=False`。"""
    repo = repo_factory("detached")
    _git("branch", "feature", cwd=repo)
    _git("checkout", "-q", "--detach", "HEAD", cwd=repo)

    rows = _branch_rows(service, _bind(service, repo))

    assert [str(row["name"]) for row in rows] == ["feature", "main"]
    assert all(row["isCurrent"] is False for row in rows)


# ---------------------------------------------------------------------------
# D7 第 6 行：只列本地分支（不列 remote-tracking）
# ---------------------------------------------------------------------------


def test_only_local_branches_are_listed_after_push(
    service: RepositoryService, repo_factory: Callable[..., Path], tmp_path: Path
) -> None:
    """**D7 第 6 行**（D2）：push 后 `origin/main` 真实存在，但列表里**没有**它。

    remote-tracking 是别名不是分支；混进来会让"共几个分支"与"当前分支"同时失真。
    """
    repo = repo_factory("only-local")
    _add_bare_remote(repo, tmp_path)
    # 前提：远端追踪引用真的存在（否则"没有它"是捡来的，不是这条命令筛掉的）
    remote_refs = _git("for-each-ref", "refs/remotes/", "--format=%(refname:short)", cwd=repo)
    assert "origin/main" in remote_refs

    rows = _branch_rows(service, _bind(service, repo))

    names = [str(row["name"]) for row in rows]
    assert names == ["main"]
    assert "origin/main" not in names


# ---------------------------------------------------------------------------
# D7 第 7 行：不依赖人类可读输出（T33 的回归）
# ---------------------------------------------------------------------------


def test_parsing_does_not_depend_on_human_readable_output(
    service: RepositoryService, repo_factory: Callable[..., Path]
) -> None:
    """**D7 第 7 行**（S4 禁止事项，T33）：`color.ui always` + 非 ASCII 分支名，解析仍对。

    机器可读输出是 `for-each-ref --format=…%00…`；`git branch` 的人类可读输出会随颜色 /
    语言 / 终端宽度变化，**不许**拿它解析。
    """
    repo = repo_factory("colored")
    _git("config", "color.ui", "always", cwd=repo)
    _git("branch", "功能/中文", cwd=repo)

    rows = _branch_rows(service, _bind(service, repo))

    assert [str(row["name"]) for row in rows] == ["main", "功能/中文"]
    assert all(SHA8.fullmatch(str(row["sha"])) for row in rows)
    assert {str(row["name"]): row["isCurrent"] for row in rows} == {
        "main": True,
        "功能/中文": False,
    }


# ---------------------------------------------------------------------------
# D7 第 8 行：服务层错误码
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("params", "expected"),
    [
        ({}, INVALID_PARAMS),  # repoId 缺失
        ({"repoId": 123}, INVALID_PARAMS),  # 不是字符串
        ({"repoId": ""}, INVALID_PARAMS),  # 空串
        ({"repoId": "000000000000"}, REPO_NOT_BOUND),  # 查不到
    ],
)
def test_branch_error_codes_for_bad_params(
    service: RepositoryService, params: dict[str, object], expected: str
) -> None:
    """**D7 第 8 行**（D5）：缺 / 非字符串 / 空串 → `INVALID_PARAMS`；查不到 → `REPO_NOT_BOUND`。"""
    with pytest.raises(frames.FrameError) as excinfo:
        service.branches(params)
    assert excinfo.value.code == expected


def test_deleted_repo_path_maps_to_repo_not_bound(
    service: RepositoryService, repo_factory: Callable[..., Path]
) -> None:
    """**D7 第 8 行**（D5）：绑定的目录被删 → `REPO_NOT_BOUND`，文案「仓库路径已不存在」。

    `INTERNAL` 会把"用户删了仓库"这种正常处境报成"未预期错误需报障"；码表不许扩，
    所以选语义最近的 `REPO_NOT_BOUND`（D5 的裁决）。
    """
    repo = repo_factory("gone")
    repo_id = _bind(service, repo)
    shutil.rmtree(repo)

    with pytest.raises(frames.FrameError) as excinfo:
        service.branches({"repoId": repo_id})
    assert excinfo.value.code == REPO_NOT_BOUND
    assert excinfo.value.message == "仓库路径已不存在"
    # 05 §4.2：错误文案里不许出现用户路径
    assert str(repo) not in excinfo.value.message


def test_repo_whose_dot_git_was_removed_maps_to_repo_not_bound(
    service: RepositoryService, repo_factory: Callable[..., Path]
) -> None:
    """**D7 第 8 行**（D5）：目录还在但 `.git` 被删 → `REPO_NOT_BOUND`（git 非零退出）。"""
    repo = repo_factory("no-git-dir")
    repo_id = _bind(service, repo)
    shutil.rmtree(repo / ".git")

    with pytest.raises(frames.FrameError) as excinfo:
        service.branches({"repoId": repo_id})
    assert excinfo.value.code == REPO_NOT_BOUND


def test_missing_git_executable_maps_to_adapter_unavailable(
    service: RepositoryService, repo_factory: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """D5：`git` 不存在 → `ADAPTER_UNAVAILABLE`（可重试）。

    先把仓库绑好（那时 git 还在），再清空 `PATH`：`branches` 会走到 `git_executable()`
    抛 `GitUnavailableError`——**不 mock 解析**，只是让"找不到 git"真实发生。
    """
    repo = repo_factory("no-git-binary")
    repo_id = _bind(service, repo)

    monkeypatch.setenv("PATH", "")
    with pytest.raises(frames.FrameError) as excinfo:
        service.branches({"repoId": repo_id})
    assert excinfo.value.code == ADAPTER_UNAVAILABLE


def test_git_timeout_maps_to_internal(
    service: RepositoryService, repo_factory: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """D5：`git` 超时 → `INTERNAL`（同 S3 D6，S19 的 doctor 再细化）。

    真超时要 5 秒且依赖机器；这里**只在边界注入异常**，钉住的是"码的映射"，
    解析逻辑本身由其它用例用真 git 守（不 mock git）。
    """
    repo = repo_factory("timeout")
    repo_id = _bind(service, repo)

    def _raise_timeout(_root: Path) -> list[gitops.BranchInfo] | None:
        raise gitops.GitTimeoutError("模拟超时")

    monkeypatch.setattr(gitops, "list_branches", _raise_timeout)
    with pytest.raises(frames.FrameError) as excinfo:
        service.branches({"repoId": repo_id})
    assert excinfo.value.code == INTERNAL


# ---------------------------------------------------------------------------
# D7 第 9 行：端到端（真 WS 帧）
# ---------------------------------------------------------------------------


@pytest.fixture
def gw(state_home: Path) -> Iterator[Kernel]:
    with Kernel(state_home) as kernel:
        yield kernel


def _ws_calls(
    kernel: Kernel, requests: list[tuple[str, dict[str, object] | None]]
) -> list[dict[str, object]]:
    """在**一条**连接上依次发 N 帧，按顺序收回（连接级 `seq` 也因此可断言）。"""

    async def run() -> list[dict[str, object]]:
        ticket = kernel.fetch_page()[0]
        out: list[dict[str, object]] = []
        async with websockets.connect(kernel.ws_url(ticket)) as ws:
            for index, (method, params) in enumerate(requests):
                message: dict[str, object] = {"type": "req", "id": f"c{index}", "method": method}
                if params is not None:
                    message["params"] = params
                await ws.send(json.dumps(message))
                out.append(decode_frame(await ws.recv()))
        return out

    return asyncio.run(run())


def test_websocket_branches_roundtrip_matches_service_layer(
    gw: Kernel, repo_factory: Callable[..., Path], tmp_path: Path
) -> None:
    """**D7 第 9 行**（验收 1/2/3）：真 WS 帧 `repo.branches` 回 `{data, seq}`，与直调一致。

    直调那一路另开一个真 SQLite，绑**同一个** git 仓库：`repoId` 由路径决定（S3.5 D1），
    所以两边的 `repoId` 与分支载荷应当逐字段相同——这正是"帧层只是转手"的证据。
    """
    repo = repo_factory("ws-branches")
    _git("branch", "feature", cwd=repo)

    with state_db.RepoStore(tmp_path / "direct.db") as direct_store:
        direct = RepositoryService(direct_store)
        direct_repo_id = _bind(direct, repo)
        expected = _branch_rows(direct, direct_repo_id)

    bound, branched = _ws_calls(
        gw,
        [
            ("repo.bind", {"path": str(repo)}),
            ("repo.branches", {"repoId": direct_repo_id}),
        ],
    )

    assert bound["ok"] is True, bound
    assert branched["ok"] is True, branched
    assert frame_result(bound)["seq"] == 1
    assert frame_result(branched)["seq"] == 2

    data = frame_result(branched)["data"]
    assert isinstance(data, list), data
    assert data == expected, "帧层载荷必须与直调服务层逐字段一致"
    assert {str(row["name"]): row["isCurrent"] for row in data} == {
        "feature": False,
        "main": True,
    }


def test_websocket_branches_error_frame_carries_stable_code(gw: Kernel) -> None:
    """D5/D7 第 8 行的帧形态：未知 `repoId` → `ok:false` + `REPO_NOT_BOUND`。"""
    (frame,) = _ws_calls(gw, [("repo.branches", {"repoId": "000000000000"})])

    assert frame["ok"] is False
    error = frame["error"]
    assert isinstance(error, dict)
    assert error["code"] == REPO_NOT_BOUND


# ---------------------------------------------------------------------------
# D7 第 10 行：gitops 三态（非仓库 / 路径没了 → None，不抛）
# ---------------------------------------------------------------------------


def test_gitops_list_branches_returns_none_for_non_repo_and_missing_path(
    repo_factory: Callable[..., Path], tmp_path: Path
) -> None:
    """**D7 第 10 行**（D4）：非仓库路径 → `None`（不抛）；路径没了 → `None`。

    这是三态里的第二态：git 非零退出 = "问不出来"。**不许**折成空列表——否则帧层会把
    "这不是仓库"报成"这个仓库没有分支"。
    """
    plain = repo_factory("plain", commit=False)
    shutil.rmtree(plain / ".git")  # T28：显式让"这确实不是仓库"不依赖运气
    assert not (plain / ".git").exists()

    assert gitops.list_branches(plain) is None
    assert gitops.list_branches(tmp_path / "does-not-exist") is None


# ---------------------------------------------------------------------------
# D6 的页面契约（**文本断言**；真实 DOM 行为见 tools/acceptance/s4_branches.mjs）
# ---------------------------------------------------------------------------


def _page() -> str:
    return assets.render_index(
        version="0.1.2",
        api_version="1",
        state_root="/tmp/state",
        handshake_ticket="t" * 64,
        ws_path="/gateway/x",
    )


def test_page_declares_branch_panel_dom_contract() -> None:
    """**D6**：`#branches` 的 id / 初始 state / 空态文案 / error 的 role=alert。

    这些是浏览器验收按着点的契约（`tools/acceptance/s4_branches.mjs`），改 id 会同时打断
    脚本与 codemap；文本断言只是最快的报警器（T9：它不证明浏览器里真的这么渲染）。
    """
    page = _page()
    for snippet in (
        '<section id="branches" data-state="idle" aria-live="polite">',
        '<h2 id="branches-title">分支</h2>',
        '<p id="branches-state">从左侧选择一个仓库</p>',
        '<p id="branches-error" role="alert" hidden></p>',
        '<ul id="branch-list"></ul>',
        '<p id="branch-empty" hidden>这个仓库还没有任何分支（空仓库）。</p>',
    ):
        assert snippet in page, snippet


def test_page_branch_panel_visibility_is_css_driven() -> None:
    """**D6 / T8**：显隐由 `#branches[data-state=…]` 的 CSS 规则决定，JS 只改一个属性。"""
    page = _page()
    for selector in (
        '#branches[data-state="idle"] #branch-list',
        '#branches[data-state="loading"] #branch-list',
        '#branches[data-state="ready"] #branches-state',
        '#branches[data-state="error"] #branches-state',
        '#branches[data-state="error"] #branch-list',
    ):
        assert selector in page, selector
    for assignment in (
        "branchesEl.dataset.state = state;",
        'branchesEl.dataset.state = "loading";',
        'branchesEl.dataset.state = "ready";',
        'branchesEl.dataset.state = "error";',
    ):
        assert assignment in page, assignment


def test_page_branch_error_text_has_its_own_fallback() -> None:
    """**D6**：分支面板按码复用 `ERROR_TEXT`，但兜底是「读取分支列表失败」。

    S4 实施裁决（管理层补的）：`ERROR_TEXT` 补 `REPO_NOT_BOUND`；分支面板**不能**用
    `ERROR_TEXT_FALLBACK`（那是绑定场景的「绑定失败」，在这里答"绑定失败"是错的），
    同时**不许**改 `ERROR_TEXT_FALLBACK` 的值。
    """
    page = _page()
    assert '"REPO_NOT_BOUND": "仓库未绑定或已不存在"' in page
    assert 'var BRANCH_ERROR_FALLBACK = "读取分支列表失败";' in page
    assert "return ERROR_TEXT[code] || BRANCH_ERROR_FALLBACK;" in page
    # 绑定表单的兜底保持不变（S3 的页面断言引用了它）
    assert 'var ERROR_TEXT_FALLBACK = "绑定失败";' in page


def test_page_repo_rows_are_keyboard_selectable() -> None:
    """**D6**：仓库行 `tabindex="0"` + `role="button"`，`click` 与 `Enter` / `Space` 都能选。"""
    page = _page()
    for snippet in (
        "li.tabIndex = 0;",
        'li.setAttribute("role", "button");',
        'if (ev.key === "Enter" || ev.key === " " || ev.key === "Spacebar") {',
        "ev.preventDefault();",
        'li.addEventListener("click", function () { selectRepo(rowId, rowName); });',
        # 重建后恢复选中：否则每次 repo.list 都会把选中样式冲掉
        "applyRepoSelection();",
        "loadBranches();   // 重连且仍有选中仓库",
    ):
        assert snippet in page, snippet


def test_page_never_builds_dom_with_innerhtml() -> None:
    """D6 / 05 §4.2 的同一精神：进 DOM 的文本一律 `textContent`（仓库名是用户数据）。"""
    assert "innerHTML" not in _page()
