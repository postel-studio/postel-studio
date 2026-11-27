"""S7 · 工作区（`worktree.acquire` / `list` / `release`）：真 git + 真 SQLite + 真目录，不 mock。

一条用例对应 `docs/design/issues/S7-worktree-visible.md` §4 的验收映射与 §5 的边界矩阵
（用例名与文档里的名字一一对应；反向注入在 `tests/test_reverse_checks.py`）。

三条纪律（S7 的硬约束，测试是它们的证据）：

1. **写面**：本刀是内核第一次往用户仓库里写东西。每个用例结束都断言用户仓库的工作区**零改动**
   （`git status --porcelain` 空），只有 `.git/worktrees/` 与 `refs/heads/bot/*` 会变。
2. **不删用户的东西**：脏工作区回收必须失败（`WORKTREE_CONFLICT`）且文件还在；占用的目录不碰。
3. **git 是事实源**：复用/回收的判定都从 `git worktree list --porcelain` 回读，不信内存里的对象。

夹具：`service` 用**真 SQLite 文件**（`tmp_path`），`state_home` 把状态根指到 `tmp_path`
（工作区目录落在那里，绝不落到真实 `~/.postel`，也绝不落进被绑定的仓库）。
"""

from __future__ import annotations

import os
import stat
import subprocess
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from postel_studio.api import frames, gateway
from postel_studio.api.bots import BotService
from postel_studio.api.repository import RepositoryService
from postel_studio.api.worktrees import WorktreeService
from postel_studio.domain.models import Bot, bot_id
from postel_studio.storage import db as state_db
from postel_studio.storage import paths
from postel_studio.web import assets

# 07 §1.3 的码（断言只按码，不看文案）
INVALID_PARAMS = "INVALID_PARAMS"
REPO_NOT_BOUND = "REPO_NOT_BOUND"
BAD_BASE_REF = "BAD_BASE_REF"
WORKTREE_CONFLICT = "WORKTREE_CONFLICT"

PRESET = "builder"  # 本文件用的 Bot 名字（S8 起由 `bot.create` 建，不再有预置播种）


# ---------------------------------------------------------------------------
# 脚手架：真 git（身份显式给，不依赖跑测试机器的全局配置）
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


def _worktree_records(repo: Path) -> list[dict[str, str]]:
    """`git worktree list --porcelain` 解析成记录（测试自己的解析，故意与被测代码分开写）。"""
    records: list[dict[str, str]] = []
    for block in _git("worktree", "list", "--porcelain", cwd=repo).split("\n\n"):
        if not block.strip():
            continue
        record: dict[str, str] = {}
        for line in block.splitlines():
            key, _, value = line.partition(" ")
            record[key] = value
        records.append(record)
    return records


def _record_for(repo: Path, target: Path) -> dict[str, str] | None:
    wanted = target.resolve()
    return next(
        (r for r in _worktree_records(repo) if Path(r.get("worktree", "")).resolve() == wanted),
        None,
    )


def _error_code(exc: BaseException) -> str:
    assert isinstance(exc, frames.FrameError), f"要的是帧层错误，拿到 {type(exc).__name__}"
    return exc.code


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------


@pytest.fixture
def umask_022() -> Iterator[None]:
    """D2 的权限断言要在实测的 umask 下（同 S6 T38：不设的话 umask 077 会让注入意外变绿）。"""
    previous = os.umask(0o022)
    try:
        yield None
    finally:
        os.umask(previous)


@pytest.fixture
def stores(state_home: Path) -> Iterator[tuple[state_db.RepoStore, state_db.BotStore]]:
    """两个 store 共用一份**真** `state.db`（`state_home` 已把 `POSTEL_HOME` 指到 tmp）。"""
    assert paths.state_root() == state_home.resolve(), "夹具没生效：工作区会落进真实状态根"
    with state_db.RepoStore() as repos, state_db.BotStore() as bots:
        yield repos, bots


@pytest.fixture
def service(
    stores: tuple[state_db.RepoStore, state_db.BotStore],
) -> WorktreeService:
    repos, bots = stores
    return WorktreeService(repos, bots)


@pytest.fixture
def bind(
    stores: tuple[state_db.RepoStore, state_db.BotStore],
) -> Callable[[Path], str]:
    """`bind(path) -> repoId`，并顺手建出 `builder`（工作区必须挂在一个 Bot 上）。

    S8 删掉了 `bot.list` 的惰性播种，所以这里显式 `bot.create`——S7 的断言点（路径 / 分支 /
    复用 / 回收）一个都没变，只是"Bot 从哪来"变成了人的动作。
    """
    repos, bots = stores
    repository = RepositoryService(repos)

    def bind_repo(path: Path, *, seed: bool = True) -> str:
        payload = repository.bind({"path": str(path)})
        repo_id = payload["repoId"]
        assert isinstance(repo_id, str)
        if seed:
            BotService(bots, repos).create({"repoId": repo_id, "name": PRESET})
        return repo_id

    return bind_repo


@pytest.fixture
def repo_factory(tmp_path: Path) -> Callable[..., Path]:
    """`make(name, *, commit=True) -> Path`：在 `tmp_path/repos/` 下建真仓库。"""
    root = tmp_path / "repos"
    root.mkdir(exist_ok=True)

    def make(name: str = "proj", *, commit: bool = True) -> Path:
        return _init_repo(root / name, commit=commit)

    return make


def _params(repo_id: str, *, bot: str = PRESET, **extra: object) -> dict[str, object]:
    """一次 acquire/release 的参数（`botId` 是复合键：`{repo_id}/{name}`）。"""
    return {"repoId": repo_id, "botId": bot_id(repo_id, bot), **extra}


# ---------------------------------------------------------------------------
# 验收 1：分配 → 目录真的在，且 git 认为它是这个仓库的 worktree
# ---------------------------------------------------------------------------


def test_acquire_creates_directory_registered_by_git(
    service: WorktreeService,
    bind: Callable[..., str],
    repo_factory: Callable[..., Path],
    umask_022: None,
) -> None:
    """**验收 1**（D1/D2/D3）：目录在状态根下、分支是 `bot/builder/workspace`、HEAD = 基础提交。

    顺带钉住 D2 的权限：`worktrees/` 与 `worktrees/<repo_id>/` 显式 0700（T38：umask 022 下
    `mkdir` 的父级是 0755）。
    """
    repo = repo_factory("proj")
    repo_id = bind(repo)
    base_sha = _git("rev-parse", "HEAD", cwd=repo).strip()

    payload = service.acquire(_params(repo_id))

    target = paths.worktree_dir(repo_id, PRESET)
    assert payload == {
        "repoId": repo_id,
        "botId": bot_id(repo_id, PRESET),
        "botName": PRESET,
        "path": str(target),
        "branch": f"bot/{PRESET}/workspace",
        "headSha": base_sha[:8],
        "reused": False,
    }, "D1：载荷恰好七个字段，一个不多一个不少"
    assert target.is_dir()
    assert target == paths.worktrees_root() / repo_id / PRESET
    assert paths.worktrees_root().is_relative_to(paths.state_root()), "工作区只在状态根下（D2）"
    assert stat.S_IMODE(paths.worktrees_root().stat().st_mode) == 0o700
    assert stat.S_IMODE(target.parent.stat().st_mode) == 0o700

    record = _record_for(repo, target)
    assert record is not None, "git 必须认识这个目录（写面：.git/worktrees/<name>/）"
    assert record["HEAD"] == base_sha
    assert record["branch"] == f"refs/heads/bot/{PRESET}/workspace"
    # 工作区里是基础提交的内容（空树）：只有 .git 文件
    assert (target / ".git").is_file(), "linked worktree 的 .git 是一个文件，不是目录"


# ---------------------------------------------------------------------------
# 验收 3：重复点击不重复创建
# ---------------------------------------------------------------------------


def test_acquire_twice_reuses_the_same_worktree(
    service: WorktreeService, bind: Callable[..., str], repo_factory: Callable[..., Path]
) -> None:
    """**验收 3**（D7 第 1 行）：第二次返回同一路径、`reused: true`、git 里仍只有一条记录。"""
    repo = repo_factory("proj")
    repo_id = bind(repo)
    first = service.acquire(_params(repo_id))
    second = service.acquire(_params(repo_id))

    assert second["path"] == first["path"]
    assert second["reused"] is True
    assert first["headSha"] == second["headSha"]
    target = paths.worktree_dir(repo_id, PRESET)
    assert len([r for r in _worktree_records(repo) if r.get("worktree") == str(target)]) == 1
    assert len(service.list({"repoId": repo_id})) == 1


# ---------------------------------------------------------------------------
# 验收 4：回收 → 目录消失、注册记录消失、**分支保留**
# ---------------------------------------------------------------------------


def test_release_removes_directory_and_registration_but_keeps_the_branch(
    service: WorktreeService, bind: Callable[..., str], repo_factory: Callable[..., Path]
) -> None:
    """**验收 4**（D3/D5）：`removed: true`、目录没了、list 里没有；`bot/*` 分支仍在。"""
    repo = repo_factory("proj")
    repo_id = bind(repo)
    service.acquire(_params(repo_id))
    target = paths.worktree_dir(repo_id, PRESET)

    payload = service.release(_params(repo_id))

    assert payload == {"removed": True, "path": str(target)}
    assert not target.exists()
    assert _record_for(repo, target) is None
    assert _git("branch", "--list", f"bot/{PRESET}/workspace", cwd=repo).strip()
    assert service.list({"repoId": repo_id}) == []


# ---------------------------------------------------------------------------
# 验收 5：基础分支不存在 → BAD_BASE_REF，不留半个目录
# ---------------------------------------------------------------------------


def test_bad_base_ref_leaves_no_directory(
    service: WorktreeService, bind: Callable[..., str], repo_factory: Callable[..., Path]
) -> None:
    """**验收 5**（D6/D8）：`BAD_BASE_REF` 且目标目录没有被留下（半成品）。"""
    repo = repo_factory("proj")
    repo_id = bind(repo)
    target = paths.worktree_dir(repo_id, PRESET)

    with pytest.raises(frames.FrameError) as caught:
        service.acquire(_params(repo_id, baseRef="nosuch"))

    assert _error_code(caught.value) == BAD_BASE_REF
    assert not target.exists()
    assert _record_for(repo, target) is None


# ---------------------------------------------------------------------------
# 边界：基础分支（D8）
# ---------------------------------------------------------------------------


def test_acquire_without_base_ref_uses_default_branch(
    service: WorktreeService, bind: Callable[..., str], repo_factory: Callable[..., Path]
) -> None:
    """D8：缺省用 `repo.default_branch`（这里 `main` 与 `feature` 指向不同提交）。"""
    repo = repo_factory("proj")
    _git("branch", "feature", cwd=repo)
    _git("checkout", "-q", "feature", cwd=repo)
    _git("commit", "-q", "--allow-empty", "-m", "feature-only", cwd=repo)
    _git("checkout", "-q", "main", cwd=repo)
    repo_id = bind(repo)

    payload = service.acquire(_params(repo_id))

    assert payload["headSha"] == _git("rev-parse", "main", cwd=repo).strip()[:8]


def test_acquire_with_explicit_base_ref_uses_that_commit(
    service: WorktreeService, bind: Callable[..., str], repo_factory: Callable[..., Path]
) -> None:
    """D8：`baseRef` 指向 feature 时，工作区停在 feature 的提交上（不是默认分支）。"""
    repo = repo_factory("proj")
    _git("branch", "feature", cwd=repo)
    _git("checkout", "-q", "feature", cwd=repo)
    _git("commit", "-q", "--allow-empty", "-m", "feature-only", cwd=repo)
    feature_sha = _git("rev-parse", "feature", cwd=repo).strip()
    _git("checkout", "-q", "main", cwd=repo)
    repo_id = bind(repo)

    payload = service.acquire(_params(repo_id, baseRef="feature"))

    assert payload["headSha"] == feature_sha[:8]


def test_acquire_after_release_keeps_the_workspace_branch_tip(
    service: WorktreeService, bind: Callable[..., str], repo_factory: Callable[..., Path]
) -> None:
    """D3：复用**不重置**——在工作区分支上提交、回收、再分配，分支 tip 必须还在。

    这是"复用不 reset"的可执行证据：`remove` 不删分支，再分配检出的是分支自己的 tip。
    """
    repo = repo_factory("proj")
    repo_id = bind(repo)
    service.acquire(_params(repo_id))
    target = paths.worktree_dir(repo_id, PRESET)
    (target / "work.txt").write_text("kept\n", encoding="utf-8")
    _git("add", "work.txt", cwd=target)
    _git("commit", "-q", "-m", "work", cwd=target)
    tip = _git("rev-parse", "HEAD", cwd=target).strip()

    service.release(_params(repo_id))
    payload = service.acquire(_params(repo_id))

    assert payload["headSha"] == tip[:8], "再分配不能把分支重置回基础提交（会丢工作）"
    assert (target / "work.txt").read_text(encoding="utf-8") == "kept\n"


def test_acquire_on_detached_repo_requires_base_ref(
    service: WorktreeService, bind: Callable[..., str], repo_factory: Callable[..., Path]
) -> None:
    """D8：游离 HEAD 时没有默认分支 → 少参数是 `INVALID_PARAMS`（不是 `BAD_BASE_REF`）。"""
    repo = repo_factory("proj")
    _git("checkout", "-q", "--detach", cwd=repo)
    repo_id = bind(repo)

    with pytest.raises(frames.FrameError) as caught:
        service.acquire(_params(repo_id))
    assert _error_code(caught.value) == INVALID_PARAMS

    payload = service.acquire(_params(repo_id, baseRef="HEAD"))  # 显式给就没事
    assert payload["branch"] == f"bot/{PRESET}/workspace"


def test_acquire_on_empty_repo_is_bad_base_ref(
    service: WorktreeService, bind: Callable[..., str], repo_factory: Callable[..., Path]
) -> None:
    """D6：0 提交的仓库没有可用的 commit（实测 `worktree add` 报"无效引用"）→ `BAD_BASE_REF`。"""
    repo = repo_factory("empty", commit=False)
    repo_id = bind(repo)

    with pytest.raises(frames.FrameError) as caught:
        service.acquire(_params(repo_id))

    assert _error_code(caught.value) == BAD_BASE_REF
    assert not paths.worktree_dir(repo_id, PRESET).exists()


def test_acquire_on_bare_repo_with_commits_works(
    service: WorktreeService,
    bind: Callable[[Path], str],
    repo_factory: Callable[..., Path],
    tmp_path: Path,
) -> None:
    """边界：S3 接受裸仓库（`git init --bare`），有提交时它也能提供 worktree（实测）。"""
    source = repo_factory("source")
    bare = tmp_path / "bare.git"
    _git("clone", "-q", "--bare", str(source), str(bare), cwd=tmp_path)
    repo_id = bind(bare)
    # 裸仓库没有 .git 目录，`bot.list` 的 charter 目录照建（与工作区无关）
    payload = service.acquire(_params(repo_id))
    assert payload["branch"] == f"bot/{PRESET}/workspace"
    assert _record_for(bare, paths.worktree_dir(repo_id, PRESET)) is not None


@pytest.mark.parametrize(
    "base_ref",
    ["-x", "bad\nname", "x" * 201, "\udcff", ""],
    ids=["leading-dash", "control-char", "too-long", "lone-surrogate", "empty"],
)
def test_acquire_rejects_invalid_base_ref_shape(
    service: WorktreeService,
    bind: Callable[..., str],
    repo_factory: Callable[..., Path],
    base_ref: str,
) -> None:
    """D8（复用 S5 D3.1 的六条）：形状不合法 → `INVALID_PARAMS`，且一次 git 都不跑。"""
    repo = repo_factory("proj")
    repo_id = bind(repo)
    with pytest.raises(frames.FrameError) as caught:
        service.acquire(_params(repo_id, baseRef=base_ref))
    assert _error_code(caught.value) == INVALID_PARAMS
    assert not paths.worktree_dir(repo_id, PRESET).exists()


# ---------------------------------------------------------------------------
# 边界：仓库与 Bot（D6）
# ---------------------------------------------------------------------------


def test_acquire_unknown_repo_is_repo_not_bound(service: WorktreeService) -> None:
    """D6：`repoId` 查不到 → `REPO_NOT_BOUND`；缺失 → `INVALID_PARAMS`。"""
    with pytest.raises(frames.FrameError) as caught:
        service.acquire({"repoId": "000000000000", "botId": "000000000000/builder"})
    assert _error_code(caught.value) == REPO_NOT_BOUND

    with pytest.raises(frames.FrameError) as caught:
        service.acquire({"botId": "x/y"})
    assert _error_code(caught.value) == INVALID_PARAMS


@pytest.mark.parametrize("mode", ["directory-deleted", "git-deleted"])
def test_acquire_when_repo_path_is_gone_is_repo_not_bound(
    service: WorktreeService,
    bind: Callable[..., str],
    repo_factory: Callable[..., Path],
    mode: str,
) -> None:
    """D6 / T25：仓库不可用（目录被删 / `.git` 被删）→ `REPO_NOT_BOUND`，不留目录。

    `.git` 被删那条是 T25 的形状：父目录**恰好也是仓库**，`git -C` 会向上 discover。不先判
    "路径还是不是仓库"，工作区就会被注册到**别人的**仓库里（路径在我们的状态根下，看不出来）。
    """
    parent = repo_factory("parent")
    child = _init_repo(parent / "child")  # 自己是仓库，父目录也是仓库
    repo_id = bind(child)
    target = paths.worktree_dir(repo_id, PRESET)
    if mode == "directory-deleted":
        subprocess.run(["rm", "-rf", str(child)], check=True)
    else:
        subprocess.run(["rm", "-rf", str(child / ".git")], check=True)
        assert child.is_dir(), "目录还在，只是不再是一个仓库"

    for call in (service.acquire, service.release, service.list):
        with pytest.raises(frames.FrameError) as caught:
            call({"repoId": repo_id, "botId": bot_id(repo_id, PRESET)})
        assert _error_code(caught.value) == REPO_NOT_BOUND, mode

    assert not target.exists()
    assert len(_worktree_records(parent)) == 1, "父仓库不许被注册任何工作区（T25）"


def test_acquire_unknown_or_malformed_bot_id_is_invalid_params(
    service: WorktreeService, bind: Callable[..., str], repo_factory: Callable[..., Path]
) -> None:
    """D6：`botId` 缺失 / 不是字符串 / 查不到 / **属于别的仓库** → 一律 `INVALID_PARAMS`。

    归属那一条是必须的：`botId` 是复合键，跨仓库传进来时只有"这个 Bot 是不是这个仓库的"能拦住。
    """
    repo = repo_factory("proj")
    other = repo_factory("other")
    repo_id = bind(repo)
    other_id = bind(other)

    for bad in ({}, {"botId": 7}, {"botId": ""}, {"botId": "nope"}):
        with pytest.raises(frames.FrameError) as caught:
            service.acquire({"repoId": repo_id, **bad})
        assert _error_code(caught.value) == INVALID_PARAMS, bad

    with pytest.raises(frames.FrameError) as caught:
        service.acquire({"repoId": repo_id, "botId": bot_id(other_id, PRESET)})
    assert _error_code(caught.value) == INVALID_PARAMS, "跨仓库的 botId 不能用来建工作区"
    assert not paths.worktree_dir(repo_id, PRESET).exists()


def test_worktree_dir_rejects_names_that_escape_worktrees_root() -> None:
    """D2（纵深防御）：bot 名过白名单、`repo_id` 必须是 12 位 hex，否则**不进路径**。"""
    with pytest.raises(ValueError):
        paths.worktree_dir("adec6540a725", "../x")
    with pytest.raises(ValueError):
        paths.worktree_dir("adec6540a725", "Builder")
    with pytest.raises(ValueError):
        paths.worktree_dir("../../etc", "builder")
    with pytest.raises(ValueError):
        paths.worktree_dir("adec6540a72", "builder")  # 11 位
    assert paths.worktree_dir("adec6540a725", "builder") == (
        paths.worktrees_root() / "adec6540a725" / "builder"
    )


# ---------------------------------------------------------------------------
# 边界：目标路径的五种状态（D7）
# ---------------------------------------------------------------------------


def test_acquire_on_occupied_directory_is_worktree_conflict(
    service: WorktreeService, bind: Callable[..., str], repo_factory: Callable[..., Path]
) -> None:
    """D7 第 5 行：目录在、不是我们的 worktree、**非空** → 冲突且一个字节都不动。"""
    repo = repo_factory("proj")
    repo_id = bind(repo)
    target = paths.worktree_dir(repo_id, PRESET)
    target.mkdir(parents=True)
    keep = target / "keep.txt"
    keep.write_text("mine\n", encoding="utf-8")

    with pytest.raises(frames.FrameError) as caught:
        service.acquire(_params(repo_id))

    assert _error_code(caught.value) == WORKTREE_CONFLICT
    assert keep.read_text(encoding="utf-8") == "mine\n", "不得删除用户的文件"
    assert _record_for(repo, target) is None


def test_acquire_into_an_empty_directory_succeeds(
    service: WorktreeService, bind: Callable[..., str], repo_factory: Callable[..., Path]
) -> None:
    """D7 第 4 行：空目录 git 是接受的（实测）——不为了"干净"去删它。"""
    repo = repo_factory("proj")
    repo_id = bind(repo)
    target = paths.worktree_dir(repo_id, PRESET)
    target.mkdir(parents=True)

    payload = service.acquire(_params(repo_id))

    assert payload["path"] == str(target)
    assert _record_for(repo, target) is not None


def test_acquire_when_the_target_path_is_a_file_is_conflict(
    service: WorktreeService, bind: Callable[..., str], repo_factory: Callable[..., Path]
) -> None:
    """D7 第 5 行（另一种形状）：目标路径是个**文件** → 冲突，文件原样保留。"""
    repo = repo_factory("proj")
    repo_id = bind(repo)
    target = paths.worktree_dir(repo_id, PRESET)
    target.parent.mkdir(parents=True)
    target.write_text("not a dir\n", encoding="utf-8")

    with pytest.raises(frames.FrameError) as caught:
        service.acquire(_params(repo_id))

    assert _error_code(caught.value) == WORKTREE_CONFLICT
    assert target.read_text(encoding="utf-8") == "not a dir\n"


def test_acquire_repairs_a_pruned_worktree_entry(
    service: WorktreeService, bind: Callable[..., str], repo_factory: Callable[..., Path]
) -> None:
    """D7 第 2 行：目录被手删后 git 仍记着它（`prunable`）→ prune 后重建，`reused: false`。"""
    repo = repo_factory("proj")
    repo_id = bind(repo)
    service.acquire(_params(repo_id))
    target = paths.worktree_dir(repo_id, PRESET)
    subprocess.run(["rm", "-rf", str(target)], check=True)
    assert _record_for(repo, target) is not None, "git 仍记着一条目录已消失的记录"
    assert service.list({"repoId": repo_id}) == [], "目录不在的记录不是工作区（D4 的过滤）"

    payload = service.acquire(_params(repo_id))

    assert payload["reused"] is False
    assert target.is_dir()
    assert _record_for(repo, target) is not None


def test_acquire_branch_checked_out_elsewhere_is_conflict(
    service: WorktreeService, bind: Callable[..., str], repo_factory: Callable[..., Path]
) -> None:
    """D7：`bot/<bot>/workspace` 被**别的** worktree 占着 → 冲突（不解析 git 文案，按记录判）。"""
    repo = repo_factory("proj")
    repo_id = bind(repo)
    elsewhere = repo.parent / "manual-wt"
    _git("worktree", "add", "-q", "-b", f"bot/{PRESET}/workspace", str(elsewhere), "main", cwd=repo)

    with pytest.raises(frames.FrameError) as caught:
        service.acquire(_params(repo_id))

    assert _error_code(caught.value) == WORKTREE_CONFLICT
    assert not paths.worktree_dir(repo_id, PRESET).exists()


# ---------------------------------------------------------------------------
# 边界：回收（D5/D6/D7）
# ---------------------------------------------------------------------------


def test_release_dirty_worktree_is_conflict_and_keeps_files(
    service: WorktreeService, bind: Callable[..., str], repo_factory: Callable[..., Path]
) -> None:
    """D5/D6：脏工作区回收 → `WORKTREE_CONFLICT`，目录与未提交的文件**原样保留**（不 --force）。"""
    repo = repo_factory("proj")
    repo_id = bind(repo)
    service.acquire(_params(repo_id))
    target = paths.worktree_dir(repo_id, PRESET)
    work = target / "burn.txt"
    work.write_text("uncommitted\n", encoding="utf-8")

    with pytest.raises(frames.FrameError) as caught:
        service.release(_params(repo_id))

    assert _error_code(caught.value) == WORKTREE_CONFLICT
    assert work.read_text(encoding="utf-8") == "uncommitted\n"
    assert _record_for(repo, target) is not None

    work.unlink()  # 人处理掉之后，回收才成功
    assert service.release(_params(repo_id))["removed"] is True


def test_release_is_idempotent_when_nothing_was_allocated(
    service: WorktreeService, bind: Callable[..., str], repo_factory: Callable[..., Path]
) -> None:
    """D7 第 3 行：从没分配过 → `removed: false`，**不是错误**（重复点"回收"很正常）。"""
    repo = repo_factory("proj")
    repo_id = bind(repo)
    payload = service.release(_params(repo_id))
    assert payload == {"removed": False, "path": str(paths.worktree_dir(repo_id, PRESET))}


def test_release_repairs_a_pruned_worktree_entry(
    service: WorktreeService, bind: Callable[..., str], repo_factory: Callable[..., Path]
) -> None:
    """D7 第 2 行（回收侧）：目录已被手删 → prune 掉元数据并报 `removed: true`。"""
    repo = repo_factory("proj")
    repo_id = bind(repo)
    service.acquire(_params(repo_id))
    target = paths.worktree_dir(repo_id, PRESET)
    subprocess.run(["rm", "-rf", str(target)], check=True)

    payload = service.release(_params(repo_id))

    assert payload["removed"] is True
    assert _record_for(repo, target) is None, "prune 之后 git 里不该再有这条记录"


def test_release_on_a_foreign_directory_is_conflict(
    service: WorktreeService, bind: Callable[..., str], repo_factory: Callable[..., Path]
) -> None:
    """D7 第 5 行：目录在但不是我们的 worktree → 冲突（拒绝碰它，即使回收也没资格删）。"""
    repo = repo_factory("proj")
    repo_id = bind(repo)
    target = paths.worktree_dir(repo_id, PRESET)
    target.mkdir(parents=True)
    (target / "mine.txt").write_text("x\n", encoding="utf-8")

    with pytest.raises(frames.FrameError) as caught:
        service.release(_params(repo_id))

    assert _error_code(caught.value) == WORKTREE_CONFLICT
    assert (target / "mine.txt").exists()


# ---------------------------------------------------------------------------
# 列表（D4）
# ---------------------------------------------------------------------------


def test_list_returns_one_entry_per_bot(
    service: WorktreeService,
    stores: tuple[state_db.RepoStore, state_db.BotStore],
    bind: Callable[..., str],
    repo_factory: Callable[..., Path],
) -> None:
    """D4/D1：每个 Bot 一条，字段与 acquire 同形**但没有 `reused`**；顺序沿用 git 的输出。"""
    _, bots = stores
    repo = repo_factory("proj")
    repo_id = bind(repo)
    bots.insert(
        Bot(
            bot_id=bot_id(repo_id, "reviewer"),
            repo_id=repo_id,
            name="reviewer",
            permission="L0",
            agent_kind=None,
            charter_rev=0,
            created_at=1.0,
        )
    )

    service.acquire(_params(repo_id))
    service.acquire(_params(repo_id, bot="reviewer"))

    rows = service.list({"repoId": repo_id})
    assert sorted(str(row["botName"]) for row in rows) == [PRESET, "reviewer"]
    for row in rows:
        assert set(row) == {"repoId", "botId", "botName", "path", "branch", "headSha"}, (
            "D1：list 的条目没有 reused"
        )
        assert row["path"] == str(paths.worktree_dir(repo_id, str(row["botName"])))


def test_list_ignores_directories_that_are_not_worktrees(
    service: WorktreeService, bind: Callable[..., str], repo_factory: Callable[..., Path]
) -> None:
    """D4 的过滤：目录在、名字合法，但 git 没注册它 → 不是工作区（0 条，不是 1 条）。"""
    repo = repo_factory("proj")
    repo_id = bind(repo)
    stray = paths.worktrees_root() / repo_id / "stray"
    stray.mkdir(parents=True)

    assert service.list({"repoId": repo_id}) == []


def test_list_on_a_gone_repo_is_repo_not_bound(
    service: WorktreeService, bind: Callable[..., str], repo_factory: Callable[..., Path]
) -> None:
    """D6：仓库目录被删 → `REPO_NOT_BOUND`（不是 `INTERNAL`，也不是空列表）。"""
    repo = repo_factory("proj")
    repo_id = bind(repo)
    subprocess.run(["rm", "-rf", str(repo)], check=True)
    with pytest.raises(frames.FrameError) as caught:
        service.list({"repoId": repo_id})
    assert _error_code(caught.value) == REPO_NOT_BOUND


# ---------------------------------------------------------------------------
# 写面（D5）：用户仓库的工作区零改动
# ---------------------------------------------------------------------------


def test_user_repository_has_no_new_files_after_acquire_and_release(
    service: WorktreeService, bind: Callable[..., str], repo_factory: Callable[..., Path]
) -> None:
    """D5：整轮下来用户仓库的**工作区**一点没变；变的只有 `.git/worktrees/` 与 `bot/*` 分支。"""
    repo = repo_factory("proj")
    repo_id = bind(repo)
    before = _git("status", "--porcelain", "--untracked-files=all", cwd=repo)
    head_before = _git("rev-parse", "HEAD", cwd=repo).strip()

    service.acquire(_params(repo_id))
    after_acquire = _git("status", "--porcelain", "--untracked-files=all", cwd=repo)
    service.release(_params(repo_id))
    after_release = _git("status", "--porcelain", "--untracked-files=all", cwd=repo)

    assert before == after_acquire == after_release == ""
    assert _git("rev-parse", "HEAD", cwd=repo).strip() == head_before, "不得动主工作区的 HEAD"
    assert _git("branch", "--list", f"bot/{PRESET}/workspace", cwd=repo).strip(), "分支保留"


# ---------------------------------------------------------------------------
# 方法表与页面契约（07 §4.9 / D9）
# ---------------------------------------------------------------------------


def test_gateway_exposes_the_three_worktree_methods() -> None:
    """07 §4.9：三个方法都在分派表里，且**只**走帧（不新开 HTTP 路由）。"""
    calls: list[str] = []

    def acquire(params: dict[str, object]) -> dict[str, object]:
        calls.append("acquire")
        return params

    def listing(params: dict[str, object]) -> list[dict[str, object]]:
        calls.append("list")
        return [params]

    def release(params: dict[str, object]) -> dict[str, object]:
        calls.append("release")
        return params

    providers = gateway.Providers(
        health=dict,
        repo_bind=lambda params: params,
        repo_list=list,
        repo_branches=lambda params: [params],
        repo_diff=lambda params: params,
        bot_list=lambda params: [params],
        bot_create=lambda params: params,
        bot_set_permission=lambda params: params,
        memory_charter_get=lambda params: params,
        worktree_acquire=acquire,
        worktree_list=listing,
        worktree_release=release,
        agent_list=list,
        agent_capabilities=lambda params: params,
    )
    assert gateway.dispatch("worktree.acquire", {"a": 1}, providers) == {"a": 1}
    assert gateway.dispatch("worktree.list", {"a": 1}, providers) == [{"a": 1}]
    assert gateway.dispatch("worktree.release", {"a": 2}, providers) == {"a": 2}
    assert calls == ["acquire", "list", "release"]


def test_page_declares_workspace_panel() -> None:
    """D9（文本断言）：页面里的 id 与 `data-state` 规则齐全，错误文案按**码**映射到新码。

    这只证明"这些东西在页面源码里"（06-pitfalls T9）；真实 DOM 行为由
    `tools/acceptance/s7_worktree.mjs` 验。
    """
    page = assets.INDEX_HTML
    for element_id in (
        "workspace",
        "workspace-form",
        "workspace-base",
        "workspace-acquire",
        "workspace-state",
        "workspace-error",
        "workspace-empty",
        "workspace-details",
        "workspace-bot",
        "workspace-path",
        "workspace-branch",
        "workspace-release",
    ):
        assert f'id="{element_id}"' in page, element_id
    # 显隐只由 data-state 决定（T8）：四个状态名都要出现在 CSS 规则里
    for state in ("idle", "loading", "ready", "error"):
        assert f'#workspace[data-state="{state}"]' in page, state
    assert assets.ERROR_TEXT["WORKTREE_CONFLICT"] == "工作区目录被占用，或里面有未提交的改动"
    assert assets.ERROR_TEXT["BAD_BASE_REF"] == "基础分支不存在或不是提交"
    assert "worktree.acquire" in page and "worktree.release" in page and "worktree.list" in page
    # 新字段只进 textContent：页面里不得出现给它们赋值 innerHTML 的写法
    for field in ("workspacePath", "workspaceBranch", "workspaceBot"):
        assert f"{field}.innerHTML" not in page
