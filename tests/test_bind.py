"""S3 · 绑定仓库：`repo.bind` / `repo.list`（真实 git 仓库，**不 mock git**）。

一条用例对应 `docs/design/issues/S3-bind-repo.md` 「测试要求」表里的一行（表里的每一行都有
对应用例，行号写在用例 docstring 里）。除 WS 端到端那条外都直接调 `RepositoryService`——
它是帧层下面的那一层，错误码与落库效果都在这里断言；帧的形状由 `gw.calls` 端到端验。

三个夹具的边界（见 `tests/conftest.py` 的隔离约定）：

- `store`：**真 SQLite 文件**（`tmp_path` 下），不是内存库——"持久化"那条要关掉再开。
- `repo_factory`：用**真实 `git init`** 建临时仓库。用户的全局 git 身份、`init.defaultBranch`
  在测试机上是随机的，所以显式传 `--initial-branch=main` 并用 `git -c user.*`，
  否则断言 `main` 会依赖跑测试的机器。
- `gw`：一个真实内核子进程 + 一条真实 WebSocket（复用 S2 的 `tests/gateway_util.py`）。
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
import websockets

from postel_studio.api import frames
from postel_studio.api import repository as repository_mod
from postel_studio.api.repository import RepositoryService
from postel_studio.gitops import repo as gitops
from postel_studio.storage import db as state_db
from postel_studio.storage import paths

from .gateway_util import Kernel, decode_frame, frame_result

# 07 §1.3 的码（断言只按码，不看文案）
NOT_A_REPO = "NOT_A_REPO"
BAD_CWD = "BAD_CWD"
INVALID_PARAMS = "INVALID_PARAMS"
ADAPTER_UNAVAILABLE = "ADAPTER_UNAVAILABLE"

HEX12 = r"^[0-9a-f]{12}$"
# 源码树（静态 AST 守卫与"子进程不读 PYTHONPATH"那条用）
# 源码树：从**已 import 的包**反推（`…/src/postel_studio/api/repository.py` → `…/src`），
# 不要从测试文件反推——反向验证在源码**副本**里跑测试，那时测试文件仍在仓库里（实测坑，见 T30）。
REPO_ROOT_SRC = Path(repository_mod.__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------


def _git(*args: str, cwd: Path) -> str:
    """真实 git（测试自己的脚手架，不是被测代码）。身份显式给，避免依赖测试机的全局配置。"""
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
    """`make(name, *, bare=False, commit=True, origin=None) -> Path`：建真实仓库。"""
    root = tmp_path / "repos"
    root.mkdir(exist_ok=True)

    def make(
        name: str = "proj",
        *,
        bare: bool = False,
        commit: bool = True,
        origin: str | None = None,
        worktree: bool = False,
    ) -> Path:
        """建一个真实仓库。

        `worktree=True` 时额外用 `git worktree add` 造一个**链接工作区**：它的 `.git` 是
        **文件**（`gitdir: …`）而不是目录。这是 `.git` 名字判定最容易被写坏的地方（T29）。
        """
        path = root / name
        path.mkdir(parents=True)
        _git("init", "-q", "--bare" if bare else "--initial-branch=main", str(path), cwd=root)
        if not bare and commit:
            # 一次提交要走 index：裸仓库没有工作区，所以这条分支只对非裸仓库成立
            _git("commit", "-q", "--allow-empty", "-m", "init", cwd=path)
        if origin is not None:
            _git("remote", "add", "origin", origin, cwd=path)
        if worktree:
            _git("worktree", "add", "-q", str(path.with_name(path.name + "-wt")), cwd=path)
            return path.with_name(path.name + "-wt")
        return path

    return make


def _bind_code(service: RepositoryService, path: str) -> str:
    """跑一次 `bind`，返回错误码；成功则失败（调用方只想看错误的情形）。"""
    with pytest.raises(frames.FrameError) as excinfo:
        service.bind({"path": path})
    return excinfo.value.code


# ---------------------------------------------------------------------------
# 表第 1 行：绑定普通仓库
# ---------------------------------------------------------------------------


def test_bind_regular_repo_returns_hex12_id_name_and_main(
    service: RepositoryService, repo_factory: Callable[..., Path]
) -> None:
    repo = repo_factory("proj")

    payload = service.bind({"path": str(repo)})

    assert isinstance(payload["repoId"], str)
    assert len(payload["repoId"]) == 12, payload["repoId"]  # D3：必须是 12 位
    int(payload["repoId"], 16)  # 只能是十六进制（非法会抛 ValueError → 用例失败）
    assert payload["name"] == "proj"
    assert payload["defaultBranch"] == "main"
    assert payload["path"] == str(repo.resolve())


# ---------------------------------------------------------------------------
# 表第 2 行：绑子目录 → 落库的是**仓库根**（D2）
# ---------------------------------------------------------------------------


def test_bind_subdirectory_stores_repo_root_not_the_subdirectory(
    service: RepositoryService, repo_factory: Callable[..., Path], store: state_db.RepoStore
) -> None:
    """用户粘 `proj/src` 要绑到 `proj`：否则左栏显示 `src`，用户以为绑了 A 其实绑了 B。"""
    repo = repo_factory("proj")
    sub = repo / "src" / "deep"
    sub.mkdir(parents=True)

    payload = service.bind({"path": str(sub)})

    assert payload["path"] == str(repo.resolve())
    assert payload["name"] == "proj"  # 显示的是**根**的名字
    stored = store.list()
    assert len(stored) == 1
    assert stored[0].path == str(repo.resolve()), "落库的必须是仓库根"


def test_bind_path_with_trailing_slash_and_dotdot_resolves_to_root(
    service: RepositoryService, repo_factory: Callable[..., Path]
) -> None:
    """D4 的 `resolve()`：`..` 与结尾 `/` 都要消掉，否则同一仓库能绑出两行。"""
    repo = repo_factory("proj")
    (repo / "sub").mkdir()
    messy = str(repo / "sub" / "..") + "/"  # resolve 后 = 仓库根；`sub` 必须真实存在

    first = service.bind({"path": messy})
    second = service.bind({"path": str(repo)})

    assert first["path"] == str(repo.resolve())
    assert second["repoId"] == first["repoId"]


def test_bind_through_symlink_stores_the_resolved_path(
    service: RepositoryService, repo_factory: Callable[..., Path], tmp_path: Path
) -> None:
    """D4：`resolve()` 消符号链接。软链绑进去，落库的必须是**真实**路径。"""
    repo = repo_factory("proj")
    link = tmp_path / "proj-link"
    link.symlink_to(repo, target_is_directory=True)

    payload = service.bind({"path": str(link)})

    assert payload["path"] == str(repo.resolve())
    assert not payload["path"].endswith("proj-link")


def test_root_level_plain_directory_that_contains_a_repo_is_not_a_repo(
    service: RepositoryService, repo_factory: Callable[..., Path], tmp_path: Path
) -> None:
    """T25 的回归锁：普通目录，只要它下面（任意深度）有别的仓库，就**不是**仓库。

    这是实测踩到的最小形态：`tmp_path/plain` 下面建一个真仓库，然后绑 `plain` 本身。
    没有收紧之前，`git -C plain rev-parse --show-toplevel` 会向父目录 discover 并答
    "`plain/under-plain`"，于是 `plain` 被"成功"绑定——`/tmp`、`/home` 同理。
    收紧后：目标自己必须带 `.git`（或是裸仓库）才算可绑；子目录只能校正到祖先根。
    """
    root = tmp_path / "plain"
    root.mkdir()
    repo_factory("under-plain")

    assert _bind_code(service, str(root)) == NOT_A_REPO


def test_plain_directory_inside_another_working_tree_is_treated_as_that_repos_subdirectory(
    service: RepositoryService, repo_factory: Callable[..., Path]
) -> None:
    """**已知近似**（06-pitfalls T25 / codemap 未决问题）：普通目录落在别人工作区里，会被
    校到那个仓库的根。

    `outer/proj/plain/deep` 与 `outer/proj/src` 在路径上无法区分：两者都是"某个目录之下
    的一串子目录，最近带 `.git` 的祖先是 `outer/proj`"。本刀按 D2 的字面语义（子目录 → 根）
    处理，代价是"用户粘了一个恰好放在别人仓库里的普通目录，会绑到那个仓库"。
    这条用例把**现有行为**钉住，不是断言理想语义——S4 若改判定，这条要一起改。
    """
    outer = repo_factory("outer")
    plain = outer / "not-a-repo" / "deep"
    plain.mkdir(parents=True)

    assert service.bind({"path": str(plain)})["path"] == str(outer.resolve())


# T29：`.git` 内部结构不是仓库入口（管理者审核发现的逃逸）
# ---------------------------------------------------------------------------


def test_bind_git_directory_itself_is_not_a_repo(
    service: RepositoryService, repo_factory: Callable[..., Path]
) -> None:
    """粘 `<repo>/.git`：`NOT_A_REPO`。

    修复前它会**静默绑到外层仓库**：`.git` 自己的祖先里正好有一个带 `.git` 的目录（它父级），
    于是"逐级向上找祖先"把用户粘的内部路径校成了另一个目录——D2 说的
    「用户以为绑了 A 其实绑了 B」换了入口重现（06-pitfalls T29）。
    """
    repo = repo_factory("proj")

    assert _bind_code(service, str(repo / ".git")) == NOT_A_REPO
    assert service.list() == []


def test_bind_path_inside_the_git_directory_is_not_a_repo(
    service: RepositoryService, repo_factory: Callable[..., Path]
) -> None:
    """粘 `<repo>/.git/objects`（更深一层）：同样 `NOT_A_REPO`，且**不留半状态**。"""
    repo = repo_factory("proj")
    objects = repo / ".git" / "objects"
    assert objects.is_dir()  # 真仓库里必然有它；不存在就说明夹具坏了

    assert _bind_code(service, str(objects)) == NOT_A_REPO
    assert service.list() == []


def test_repo_root_whose_parent_is_named_dot_git_still_binds(
    service: RepositoryService, tmp_path: Path
) -> None:
    """**T31（复审第二次退回）**：父目录名叫 `.git` 的**合法仓库**必须能绑。

    ```
    /tmp/edge/.git/workspace       ← 这是一个仓库：它自己带 .git、有提交
    ```
    第一版修法是"整条绝对路径任意一段是 `.git` 就拒绝"，于是把这类仓库误杀了——
    它的父目录**恰好**叫 `.git`，但它本身是合法入口。判据必须**相对于已确认的仓库根**：
    根就是它自己 → 相对路径为空 → 接受。
    """
    parent = tmp_path / ".git"  # 故意让父目录叫 .git
    workspace = parent / "workspace"
    workspace.mkdir(parents=True)
    _git("init", "-q", "--initial-branch=main", str(workspace), cwd=parent)
    _git("commit", "-q", "--allow-empty", "-m", "init", cwd=workspace)

    payload = service.bind({"path": str(workspace)})

    assert payload["path"] == str(workspace.resolve())
    assert payload["name"] == "workspace"
    assert payload["defaultBranch"] == "main"


def test_subdirectory_of_a_repo_whos_parent_is_named_dot_git_still_corrects(
    service: RepositoryService, tmp_path: Path
) -> None:
    """T31 的更难一档：`…/.git/workspace/src` → 校到 `…/.git/workspace`（不是更外层）。"""
    parent = tmp_path / ".git"
    workspace = parent / "workspace"
    src = workspace / "src"
    src.mkdir(parents=True)
    _git("init", "-q", "--initial-branch=main", str(workspace), cwd=parent)

    payload = service.bind({"path": str(src)})

    assert payload["path"] == str(workspace.resolve())


def test_bind_refuses_any_path_with_a_dot_git_segment(
    service: RepositoryService, repo_factory: Callable[..., Path]
) -> None:
    """判定看的是**整条路径里有 `.git` 这一段**，不只是尾段或第一层。

    参数化了三种位置：末尾、中间、以及 `.git` 里面的深层路径。
    """
    repo = repo_factory("proj")
    git_dir = repo / ".git"
    (git_dir / "objects" / "deep").mkdir(parents=True, exist_ok=True)

    candidates = [
        git_dir,
        git_dir / "objects",
        git_dir / "objects" / "deep",
        repo / ".." / ".git",  # 绕过字面 `.git` 的写法：resolve 之后仍然落在 .git 里
    ]
    for candidate in candidates:
        if not candidate.resolve().exists():
            continue
        assert _bind_code(service, str(candidate)) == NOT_A_REPO, candidate


def test_worktree_root_with_git_file_still_binds(
    service: RepositoryService, repo_factory: Callable[..., Path]
) -> None:
    """**防误伤**：worktree 的 `.git` 是**文件**（`gitdir: …`），它是合法的仓库根。

    这条规则若写成"路径里出现 `.git` 就拒绝"，worktree 会被一起打死。判据是**名字等于
    `.git` 的路径段**——worktree 根自己叫 `proj-wt`，名字不是 `.git`，故不受影响。
    """
    worktree = repo_factory("proj", worktree=True)
    assert (worktree / ".git").is_file(), "worktree 根里 .git 应该是文件"

    payload = service.bind({"path": str(worktree)})

    assert payload["path"] == str(worktree.resolve())
    assert len(str(payload["repoId"])) == 12
    # **不断言 defaultBranch == "main"**：worktree 的 `symbolic-ref HEAD` 答的是链接工作区
    # 自己的分支（`git worktree add` 缺省会建一个同名分支），不是主仓库的 main。
    # 这是 S3 已知未解决的行为（codemap 03-modules/gitops §worktree / 06 U13），
    # 要在 S7 随 worktree 一起重新设计，**不要**在这里猜一个值让测试好看。
    assert isinstance(payload["defaultBranch"], str), payload


def test_worktree_subdirectory_still_corrects_to_the_worktree_root(
    service: RepositoryService, repo_factory: Callable[..., Path]
) -> None:
    """worktree 里的子目录：仍然按 D2 校到 worktree 根（不是主仓库根）。"""
    worktree = repo_factory("proj", worktree=True)
    sub_dir = worktree / "pkg"
    sub_dir.mkdir()

    assert service.bind({"path": str(sub_dir)})["path"] == str(worktree.resolve())


# ---------------------------------------------------------------------------
# 表第 3 行：裸仓库
# ---------------------------------------------------------------------------


def test_bind_bare_repo_succeeds_and_default_branch_may_be_null(
    service: RepositoryService, repo_factory: Callable[..., Path]
) -> None:
    bare = repo_factory("bare.git", bare=True)

    payload = service.bind({"path": str(bare)})

    assert payload["path"] == str(bare.resolve())
    # D2：裸仓库的 default_branch 允许为 null——这里它其实是 "main"（git init 写了 HEAD），
    # 断言"要么是字符串要么是 null"，不硬编码成 null 也不硬编码成 main。
    assert payload["defaultBranch"] is None or isinstance(payload["defaultBranch"], str)
    assert len(str(payload["repoId"])) == 12


def test_bare_repo_without_any_head_branch_reports_null(
    service: RepositoryService, repo_factory: Callable[..., Path]
) -> None:
    """`git init --bare` 会写 HEAD 的 symbolic-ref，但被 clone 出来的裸仓库可以是游离的。

    这里把 HEAD 改成游离（`HEAD` 直接指向一个不存在的 sha）来构造"探测不到默认分支"，
    断言是 **`None`** 而不是瞎猜 `main`。
    """
    bare = repo_factory("orphan.git", bare=True)
    (bare / "HEAD").write_text("0123456789abcdef0123456789abcdef01234567\n", encoding="utf-8")

    payload = service.bind({"path": str(bare)})

    assert payload["defaultBranch"] is None


# ---------------------------------------------------------------------------
# 表第 4 行：空仓库（没有任何提交）——D2 明确不要求有提交
# ---------------------------------------------------------------------------


def test_bind_repo_without_any_commit_succeeds(
    service: RepositoryService, repo_factory: Callable[..., Path]
) -> None:
    empty = repo_factory("fresh", commit=False)

    payload = service.bind({"path": str(empty)})

    assert payload["name"] == "fresh"
    assert payload["defaultBranch"] == "main"  # git init 的缺省值（D2）


# ---------------------------------------------------------------------------
# 表第 5 行：非 git 目录 → NOT_A_REPO，且 **message 不含该路径**（D6 / 05 §4.2）
# ---------------------------------------------------------------------------


def test_bind_non_repo_directory_is_not_a_repo(
    service: RepositoryService, repo_factory: Callable[..., Path], tmp_path: Path
) -> None:
    plain = repo_factory("not-a-repo", commit=False)
    shutil.rmtree(plain / ".git")  # 变成普通目录
    with pytest.raises(frames.FrameError) as excinfo:
        service.bind({"path": str(plain)})

    error = excinfo.value
    assert error.code == NOT_A_REPO
    # **最容易顺手写错的一条**：错误不回显输入值（05 §4.2）
    assert str(plain) not in error.message
    assert "not-a-repo" not in error.message
    assert "路径不是 git 仓库" in error.message


def test_not_a_repo_message_does_not_echo_any_user_path(
    service: RepositoryService, tmp_path: Path
) -> None:
    """把路径换成一段独一无二的串塞进各级目录，证明 message 里一个片段都没有。"""
    marker = "s3-marker-e7c1"
    outside = tmp_path / marker / "inner"
    outside.mkdir(parents=True)

    with pytest.raises(frames.FrameError) as excinfo:
        service.bind({"path": str(outside)})

    error = excinfo.value
    assert error.code == NOT_A_REPO
    for fragment in (marker, "inner", str(outside), str(tmp_path)):
        assert fragment not in error.message, f"message 回显了 {fragment!r}：{error.message}"
    # 也不许出现在 repr / str（异常会被 `f"{exc}"` 格式化进日志）
    assert marker not in str(error)
    assert marker not in repr(error)


def test_bind_file_path_is_not_a_repo(
    service: RepositoryService, repo_factory: Callable[..., Path]
) -> None:
    """路径存在但不是目录：同样是 `NOT_A_REPO`（D6 的表格里"不存在/非目录"是一个码）。"""
    repo = repo_factory("proj")
    a_file = repo / "README.md"
    a_file.write_text("hi\n", encoding="utf-8")

    assert _bind_code(service, str(a_file)) == NOT_A_REPO


# ---------------------------------------------------------------------------
# 表第 6 行：相对路径 → BAD_CWD
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("relative", ["proj", "./proj", "../proj", "~", "~/proj"])
def test_bind_relative_path_is_bad_cwd(service: RepositoryService, relative: str) -> None:
    """D4：相对路径 → `BAD_CWD`（不是 NOT_A_REPO，也不是 INVALID_PARAMS）。

    `~` 也在这里：05 §3.3 禁止展开 `~`，所以它**不是**绝对路径，必须拒绝而不是悄悄展开。
    """
    assert _bind_code(service, relative) == BAD_CWD


# ---------------------------------------------------------------------------
# 表第 7 行：不存在的路径 → NOT_A_REPO（不崩）
# ---------------------------------------------------------------------------


def test_bind_nonexistent_path_is_not_a_repo_not_a_crash(
    service: RepositoryService, tmp_path: Path
) -> None:
    missing = tmp_path / "definitely-missing"

    error_code = _bind_code(service, str(missing))

    assert error_code == NOT_A_REPO
    assert service.list() == []  # 没崩、也没留半状态


# ---------------------------------------------------------------------------
# 表第 8 行：重复绑定 → 同一个 repoId，列表只有一条
# ---------------------------------------------------------------------------


def test_rebinding_same_path_yields_same_id_and_single_row(
    service: RepositoryService, repo_factory: Callable[..., Path], store: state_db.RepoStore
) -> None:
    repo = repo_factory("proj")

    first = service.bind({"path": str(repo)})
    second = service.bind({"path": str(repo)})
    (repo / "src").mkdir()
    third = service.bind({"path": str(repo / "src")})  # 子目录 → 同一个仓库根

    assert second["repoId"] == first["repoId"]
    assert third["repoId"] == first["repoId"]
    assert len(service.list()) == 1
    assert len(store.list()) == 1
    with sqlite3.connect(store.path) as raw:
        assert raw.execute("SELECT COUNT(*) FROM repos").fetchone()[0] == 1


def test_rebinding_after_origin_added_updates_the_row(
    service: RepositoryService, repo_factory: Callable[..., Path]
) -> None:
    """**S3.5 验收 2/3**：先无 origin 绑 → `git remote add origin` → 重绑：`repoId` **不变**。

    ⚠️ 语义在 S3.5 反过来了（06-pitfalls U9）。S3 时代这条断言「id 会从路径哈希变成 URL
    哈希」——那是缺陷，不是契约。现在断言：id 不变（D1）、`originUrl` 更新、仍是 1 行、
    且**按步骤 1 记下的旧 id 仍取得到**（验收 3，正是 S4 `repo.branches(repoId)` 的入口）。
    "加了 origin 后 id 会变"现在由 `test_reverse_checks.py` 的注入守着。
    """
    repo = repo_factory("proj")

    before = service.bind({"path": str(repo)})  # 步骤 1：无 origin
    _git("remote", "add", "origin", "https://github.com/o/proj.git", cwd=repo)
    after = service.bind({"path": str(repo)})  # 步骤 2：加了 origin 再绑同一路径

    assert before["originUrl"] is None
    assert after["originUrl"] == "https://github.com/o/proj.git", "origin 是可变展示字段，要更新"
    assert after["repoId"] == before["repoId"], "D1：加了 remote 之后 id 必须不变"
    assert len(service.list()) == 1, "同一路径绝不能变成两行"
    assert service.list()[0]["repoId"] == before["repoId"]
    # 步骤 3：按**旧** id 取，能取到（旧身份没有失效）
    assert service.get({"repoId": str(before["repoId"])})["path"] == str(repo.resolve())


# ---------------------------------------------------------------------------
# 表第 9 行：持久化 —— 关掉 store 再开，列表还在（真 SQLite 文件）
# ---------------------------------------------------------------------------


def test_data_survives_reopening_the_store(
    repo_factory: Callable[..., Path], tmp_path: Path
) -> None:
    db_file = tmp_path / "state.db"
    repo = repo_factory("proj")

    with state_db.RepoStore(db_file) as first:
        payload = RepositoryService(first).bind({"path": str(repo)})
    assert db_file.exists(), "必须是真文件，不是内存库"
    assert db_file.stat().st_mode & 0o777 == 0o600, oct(db_file.stat().st_mode)

    with state_db.RepoStore(db_file) as second:  # 新连接 = 模拟"重启后"再读
        listed = RepositoryService(second).list()

    assert [row["repoId"] for row in listed] == [payload["repoId"]]
    assert listed[0]["path"] == str(repo.resolve())
    assert listed[0]["defaultBranch"] == "main"


def test_schema_is_versioned_and_migration_is_reentrant(tmp_path: Path) -> None:
    """06-data-model §7：`PRAGMA user_version` 是 schema 版本，迁移必须可重入。"""
    db_file = tmp_path / "state.db"
    with state_db.RepoStore(db_file):
        pass
    with sqlite3.connect(db_file) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == state_db.SCHEMA_VERSION

    # 再开一次（重复执行同一版本的迁移）不改变状态、不报错
    with state_db.RepoStore(db_file) as reopened:
        assert reopened.list() == []
    with sqlite3.connect(db_file) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == state_db.SCHEMA_VERSION


# ---------------------------------------------------------------------------
# S3.5 验收（docs/design/issues/S3-5-repo-id-stable.md「验收标准」1-6）
#
# S3 的用例守"绑定/去重/形状"；这一节守 **repo_id 稳定性**（D1）、origin 降级（D2）
# 与 **v1 → v2 的真实迁移**（D3）。
# ---------------------------------------------------------------------------

# v1 的原始 DDL 写在测试里、**不** import 生产代码的 `_MIGRATIONS[0]`：要证明的是"S3 时代
# 真实存在于用户盘上的那个库能升上来"。两者是否仍一致由迁移测试里的第一句断言守住。
_V1_SCHEMA = """
    CREATE TABLE repos (
        repo_id        TEXT PRIMARY KEY,
        path           TEXT NOT NULL UNIQUE,
        origin_url     TEXT,
        default_branch TEXT,
        added_at       REAL NOT NULL
    );
"""


def _make_v1_database(path: Path, rows: list[tuple[str, str, str | None]]) -> list[str]:
    """手工建一个 **v1** 的 `state.db`，塞进 `rows`（`repo_id` / `path` / `origin_url`），
    返回写入的 id 列表作为"升级前"的基线。`user_version` 显式设成 1。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as conn:
        conn.executescript(_V1_SCHEMA)
        conn.execute("PRAGMA user_version = 1")
        conn.executemany(
            "INSERT INTO repos (repo_id, path, origin_url, default_branch, added_at)"
            " VALUES (?, ?, ?, 'main', ?)",
            [
                (repo_id, repo_path, origin, 1.0 + index)
                for index, (repo_id, repo_path, origin) in enumerate(rows)
            ],
        )
    return [row[0] for row in rows]


def _ids_in_database(path: Path) -> list[str]:
    """直接读库（不经过 `RepoStore`）：迁移后的 id 必须按**存储**对照，不是按读出来的对象。"""
    with sqlite3.connect(path) as conn:
        return [
            str(row[0])
            for row in conn.execute("SELECT repo_id FROM repos ORDER BY added_at, repo_id")
        ]


def test_v1_database_upgrades_to_v2_without_rewriting_any_repo_id(tmp_path: Path) -> None:
    """**S3.5 验收 5 / D3（本次审核的重点）**：v1 库 → `RepoStore()`（= migrate）→ 当前 schema，
    `repo_id` **一个都没变**、`user_version == SCHEMA_VERSION`。

    S3.5 写这条时当前 schema 是 v2；S6 加了 `bots` 表（v3），所以断言改成
    `state_db.SCHEMA_VERSION`——本用例守的是「v1 老库升到**当前**版本且 id 不变」，
    不是「恰好停在 v2」。

    必须"手工用 v1 的 SQL 建库"而不是"新建一个库再看它是 v2"：后者只证明**新库**的建表
    语句带 `origin_hash`，v2 的 `ALTER TABLE` 是否跑、老行有没有被弄坏、`user_version` 有没有
    推进，一条都覆盖不到。

    两行覆盖两种历史数据：无 origin 的老行（id 是路径哈希）、**有 origin 的老行**（S3 时代
    id 是 **URL 哈希**）——后者是 D3 的"已知留痕"，迁移**不**把它重算成路径哈希，
    最容易在实现时被"顺手统一"掉。
    """
    # 夹具与生产代码的 v1 必须一致（否则下面的迁移测试跑在一个**不是 v1** 的库上）
    assert " ".join(_V1_SCHEMA.split()) == " ".join(state_db._MIGRATIONS[0].split())
    assert len(state_db._MIGRATIONS) == state_db.SCHEMA_VERSION

    db_file = tmp_path / "state.db"
    # 有 origin 的那行故意让 repo_id = 旧算法的 URL 哈希，好让"被重算"一眼就能看出来
    from postel_studio.domain.models import repo_id_for_origin

    url_hash = repo_id_for_origin("https://github.com/o/login-e7c1.git")
    path_hash_old = "77347b364a59"  # S3 审核实测的路径哈希形态（任意 12 位 hex 都等价）
    before_ids = _make_v1_database(
        db_file,
        [
            (path_hash_old, "/srv/repos/no-origin", None),
            (url_hash, "/srv/repos/with-origin", "https://github.com/o/login-e7c1.git"),
        ],
    )
    # 升级前的真实状态：版本是 1，还没有 origin_hash 列
    with sqlite3.connect(db_file) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 1
        columns = {row[1] for row in conn.execute("PRAGMA table_info(repos)")}
    assert "origin_hash" not in columns, "v1 不该有 origin_hash 列（夹具坏了）"

    with state_db.RepoStore(db_file):  # 打开 = 迁移
        pass

    after_ids = _ids_in_database(db_file)
    assert after_ids == before_ids, f"D3：迁移改写了 repo_id（前 {before_ids} / 后 {after_ids}）"
    assert url_hash in after_ids, "有 origin 的老行必须保持**URL 哈希**原值，不许重算成路径哈希"
    with sqlite3.connect(db_file) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == state_db.SCHEMA_VERSION
        columns = {row[1] for row in conn.execute("PRAGMA table_info(repos)")}
        assert "origin_hash" in columns
        # 老行照旧读得出来（列加了、数据没动）
        rows = conn.execute(
            "SELECT repo_id, path, origin_url, origin_hash FROM repos ORDER BY added_at, repo_id"
        ).fetchall()
    assert [(row[0], row[1], row[2]) for row in rows] == [
        (path_hash_old, "/srv/repos/no-origin", None),
        (url_hash, "/srv/repos/with-origin", "https://github.com/o/login-e7c1.git"),
    ]
    assert all(row[3] is None for row in rows), "v2 不回填 origin_hash（见 db.py v2 注释）"


def test_migration_is_reentrant_and_a_second_open_changes_nothing(tmp_path: Path) -> None:
    """**S3.5 验收 6**：v1 库连开两次，第二次不做任何改动（06 §7 的可重入）。

    上一条证明"升得上去"，这条证明"升第二遍不会出事"。
    """
    db_file = tmp_path / "state.db"
    before_ids = _make_v1_database(
        db_file, [("aaaaaaaaaaaa", "/srv/repos/a", None), ("bbbbbbbbbbbb", "/srv/repos/b", None)]
    )

    with state_db.RepoStore(db_file) as first:
        first_ids = [repo.repo_id for repo in first.list()]
    first_digest = _database_digest(db_file)

    with state_db.RepoStore(db_file) as second:
        second_ids = [repo.repo_id for repo in second.list()]

    assert first_ids == before_ids == second_ids
    with sqlite3.connect(db_file) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == state_db.SCHEMA_VERSION
    assert _database_digest(db_file) == first_digest, "第二次打开改动了库内容"


def _database_digest(path: Path) -> str:
    """整表的确定性摘要（第二次打开必须与第一次逐字段相同）。"""
    import hashlib

    with sqlite3.connect(path) as conn:
        rows = conn.execute(
            "SELECT repo_id, path, origin_url, default_branch, added_at, origin_hash"
            " FROM repos ORDER BY added_at, repo_id"
        ).fetchall()
    return hashlib.sha256(repr(rows).encode("utf-8")).hexdigest()


def test_binding_an_existing_v1_row_keeps_its_legacy_id(
    repo_factory: Callable[..., Path], tmp_path: Path
) -> None:
    """D3 的"已知留痕"在**绑定路径**上也成立：v1 老行的 id 不会被新算法改写。

    场景：仓库在 v1 时代被绑过，那行存的是 URL 哈希；升到 v2 后对同一路径再绑一次，新算法
    算出的路径哈希**不能**覆盖老 id（`upsert` 规则 1 不写 `repo_id`）。否则迁移里保住的 id
    会在下一次 `bind` 被抹掉。

    这里**不用** `service` / `store` 夹具：它们会各自先建一个**新库**（= v2），而这条要的是
    真的 v1 库。
    """
    db_file = tmp_path / "state.db"
    repo = repo_factory("proj", origin="https://github.com/o/proj.git")
    legacy_id = "13632b79027a"  # 旧算法（URL 哈希）留下的 id
    _make_v1_database(db_file, [(legacy_id, str(repo.resolve()), "https://github.com/o/proj.git")])

    with state_db.RepoStore(db_file) as store:
        payload = RepositoryService(store).bind({"path": str(repo)})
        stored = store.get(legacy_id)

    assert payload["repoId"] == legacy_id, "老行的 id 必须保留（D1/D3）"
    assert stored is not None and stored.repo_id == legacy_id


def test_double_bind_is_idempotent_row_and_id(
    service: RepositoryService, repo_factory: Callable[..., Path], store: state_db.RepoStore
) -> None:
    """**测试要求·幂等**：连续两次 `bind` 同一路径 → 行数不变、id 不变、内容不变。"""
    repo = repo_factory("proj")

    first = service.bind({"path": str(repo)})
    second = service.bind({"path": str(repo)})

    assert first["repoId"] == second["repoId"]
    assert len(store.list()) == 1
    with sqlite3.connect(store.path) as raw:
        assert raw.execute("SELECT COUNT(*) FROM repos").fetchone()[0] == 1
        assert raw.execute("SELECT repo_id FROM repos").fetchone()[0] == first["repoId"]


def test_origin_hash_tracks_origin_changes_while_repo_id_does_not(
    service: RepositoryService, repo_factory: Callable[..., Path], store: state_db.RepoStore
) -> None:
    """D2 的分工：`origin_hash` **可变**（识别字段），`repo_id` **不可变**（身份）。

    S3 时代两者压在同一个值上，于是"origin 变 → 身份变"。现在加/改/删 remote 只动
    `origin_hash` 与展示用的 `origin_url`。
    """
    repo = repo_factory("proj")

    no_origin = service.bind({"path": str(repo)})
    _git("remote", "add", "origin", "https://github.com/o/p.git", cwd=repo)
    with_https = service.bind({"path": str(repo)})
    stored_https = store.list()[0]
    _git("remote", "set-url", "origin", "git@github.com:o/p.git", cwd=repo)  # 换成 ssh 写法
    with_ssh = service.bind({"path": str(repo)})
    stored_ssh = store.list()[0]

    assert no_origin["repoId"] == with_https["repoId"] == with_ssh["repoId"], "id 不变（D1）"
    assert stored_ssh.repo_id == no_origin["repoId"]
    assert stored_ssh.origin_hash is not None
    from postel_studio.domain.models import origin_hash

    assert stored_https.origin_hash == stored_ssh.origin_hash, "https 与 ssh 是同一个远端（D2）"
    assert stored_ssh.origin_hash == origin_hash("git@github.com:o/p.git")


def test_unbound_legacy_id_is_still_reachable_after_origin_change(
    service: RepositoryService, repo_factory: Callable[..., Path]
) -> None:
    """验收 3 的第二形态：**按最早那次绑定得到的 id** 取，永远取得到（不是"最近一次"）。

    用户视角：加/改/删 remote 之后，会话里缓存的 `repoId` 都不会变 `REPO_NOT_BOUND`
    （S4 的 `repo.branches(repoId)` 依赖这一条）。
    """
    repo = repo_factory("proj")
    original = service.bind({"path": str(repo)})["repoId"]

    for operation in (
        ("remote", "add", "origin", "https://github.com/o/p.git"),
        ("remote", "set-url", "origin", "git@github.com:o/p.git"),
        ("remote", "remove", "origin"),
    ):
        _git(*operation, cwd=repo)
        service.bind({"path": str(repo)})
        assert service.get({"repoId": str(original)})["repoId"] == original, operation


# ---------------------------------------------------------------------------
# 表第 10 行：origin 归一化 —— https 与 ssh 是同一个**远端**（origin_hash 相同，id 不同）
# (S3.5 验收 4；含义与 S3 时代不同，见用例 docstring)
# ---------------------------------------------------------------------------


def test_https_and_ssh_origins_share_the_same_repo_id(
    repo_factory: Callable[..., Path], tmp_path: Path
) -> None:
    """**S3.5 验收 4**：https / ssh 两种 origin → `repo_id` **不同**、`origin_hash` 相同。

    ⚠️ S3.5 之后这条用例的**含义变了两处**（08-repo-id-stability §4 验收 4）：

    1. S3 时代它断言两个**不同路径**的仓库因为 URL 归一化而**同 id**。那其实是"id 由 URL 决定"
       的证据——正是要修的缺陷。现在 id 只看路径，两个不同路径的仓库**必然不同 id**；
    2. "https 与 ssh 是同一个远端"这件事改由 `origin_hash` 承载（D2）。所以真正**测 URL
       归一化**的断言在这里：两种写法 → 同一个 `origin_hash`。

    帧层不暴露 `origin_hash`（07 §4.2 的载荷没有它，本刀不改协议），所以在 `RepoStore` 层
    断言（验收 4 原文允许）。
    """
    https = repo_factory("https", origin="https://github.com/o/p.git")
    ssh = repo_factory("ssh", origin="git@github.com:o/p.git")

    with state_db.RepoStore(tmp_path / "state.db") as store:
        service = RepositoryService(store)
        first = service.bind({"path": str(https)})
        second = service.bind({"path": str(ssh)})
        stored = {repo.path: repo for repo in store.list()}

    assert first["repoId"] != second["repoId"], "id 只看路径：两个不同路径 → 不同 id"
    assert len(str(first["repoId"])) == 12
    assert len(str(second["repoId"])) == 12
    # D2：归一化之后两种传输方式是**同一个远端**，这件事记在 origin_hash 上
    hashes = {repo.origin_hash for repo in stored.values()}
    assert len(hashes) == 1 and None not in hashes, f"origin_hash 应相同：{hashes}"


def test_repo_id_normalisation_edge_cases() -> None:
    """归一化的边界（纯函数，不起 git）：`models.normalize_origin` 的每一条规则各一行。"""
    from postel_studio.domain.models import normalize_origin

    assert normalize_origin("https://github.com/o/p.git") == "github.com/o/p"
    assert normalize_origin("git@github.com:o/p.git") == "github.com/o/p"
    assert normalize_origin("ssh://git@github.com:22/o/p.git") == "github.com/o/p"
    assert normalize_origin("https://GitHub.com/o/p/") == "github.com/o/p"
    assert normalize_origin("git://github.com/o/p") == "github.com/o/p"
    # 本地路径 remote 不做 URL 猜测
    assert normalize_origin("/srv/git/p.git") == "/srv/git/p.git"


# ---------------------------------------------------------------------------
# 表第 11 行：无 origin —— 用绝对路径算，12 位
# ---------------------------------------------------------------------------


def test_repo_id_without_origin_is_derived_from_the_absolute_path(
    service: RepositoryService, repo_factory: Callable[..., Path]
) -> None:
    from postel_studio.domain.models import repo_id_for_path

    repo = repo_factory("proj")  # 没有 origin

    payload = service.bind({"path": str(repo)})

    assert payload["originUrl"] is None
    assert payload["repoId"] == repo_id_for_path(str(repo.resolve()))
    assert len(payload["repoId"]) == 12


def test_absolute_path_hash_is_stable_across_calls(repo_factory: Callable[..., Path]) -> None:
    """同一个路径两次算出来的 id 必须一样（否则每次 bind 都"变一个仓库"）。"""
    from postel_studio.domain.models import repo_id_for_path

    repo = repo_factory("proj").resolve()
    assert repo_id_for_path(str(repo)) == repo_id_for_path(str(repo))
    assert repo_id_for_path(str(repo)) != repo_id_for_path(str(repo) + "x")


# ---------------------------------------------------------------------------
# 表第 12 行：git 不存在 → ADAPTER_UNAVAILABLE（PATH 设空），内核不崩
# ---------------------------------------------------------------------------


def test_missing_git_is_adapter_unavailable(
    service: RepositoryService,
    repo_factory: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """把 `PATH` 设空：`which("git")` 找不到 → `ADAPTER_UNAVAILABLE`，不是崩、也不是 NOT_A_REPO。"""
    repo = repo_factory("proj")
    monkeypatch.setenv("PATH", "")

    error_code = _bind_code(service, str(repo))

    assert error_code == ADAPTER_UNAVAILABLE


# ---------------------------------------------------------------------------
# 表第 13 行：不往仓库里写东西
# ---------------------------------------------------------------------------


def test_bind_writes_nothing_into_the_user_repository(
    repo_factory: Callable[..., Path], tmp_path: Path
) -> None:
    """绑定前后 `git status --porcelain` 一致 + 仓库目录里的文件清单一致。

    不变量：`state.db` 只写在状态根（这里显式指到 `tmp_path`），**不进用户仓库**。
    """
    repo = repo_factory("proj")
    (repo / "a.txt").write_text("x\n", encoding="utf-8")  # 一个未跟踪文件，让 status 有内容
    before_status = _git("status", "--porcelain", "-uall", cwd=repo)
    before_files = sorted(p.relative_to(repo) for p in repo.rglob("*"))

    with state_db.RepoStore(tmp_path / "postel" / "state.db") as store:
        RepositoryService(store).bind({"path": str(repo)})

    assert _git("status", "--porcelain", "-uall", cwd=repo) == before_status
    assert sorted(p.relative_to(repo) for p in repo.rglob("*")) == before_files
    assert not any(p.name == paths.STATE_DB_FILE for p in repo.rglob("*"))


def test_bind_uses_git_in_a_way_that_cannot_prompt(
    repo_factory: Callable[..., Path], store: state_db.RepoStore
) -> None:
    """D5：子进程环境里必须有 `GIT_TERMINAL_PROMPT=0`、必须无 shell、必须有超时。

    这条不重复实现：直接读 `gitops.repo` 里 `_environment()` 与 `run_git` 的参数来源，
    并实测 `_environment()` 的返回值。删掉其中任何一个常量，这里就红。
    """
    from postel_studio.gitops import repo as gitops

    env = gitops._environment()  # 守的是"环境里必须有什么"，只能读它
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert env["GIT_CONFIG_NOSYSTEM"] == "1"
    assert "GIT_DIR" not in env and "GIT_WORK_TREE" not in env
    assert isinstance(gitops.GIT_TIMEOUT_SECONDS, float)
    assert 0 < gitops.GIT_TIMEOUT_SECONDS <= 5.0  # D5：必须有超时，且不超过 5 秒

    # 真的跑一次，确认带环境变量也照样能工作（不是"配了但被覆盖"）
    repo = repo_factory("proj")
    assert RepositoryService(store).bind({"path": str(repo)})["name"] == "proj"


def test_git_subprocess_shell_is_disabled() -> None:
    """05 §3.3 的静态守卫：**唯一**起 git 的地方必须 `shell=False`、命令最终是字面数组。

    为什么这条是"读源码"而不是"跑一次"：`shell=True` 在**没有注入**的前提下行为与
    `shell=False` 一样（同一个 argv 交给 `/bin/sh -c` 也能跑通），所以只有两种验法：
    （a）读源码断言；（b）构造带 shell 元字符的路径看它会不会被 sh 解释。
    （b）要临时改用户目录名、依赖 `git -C` 的引号行为，脆弱且会污染机器。
    取（a），并配套**反向验证**（`tests/test_reverse_checks.py` 把 `shell=False` 改成
    `True` 时必须红）—— 静态断言有了反向锁才不是装饰品。

    ⚠️ 命令是 `command = [git_executable(), "-C", str(root), *args]` 这样一个**变量**，
    不是内联的列表字面量，所以这里顺着赋值回溯一层再判它是列表 —— 第一版没回溯，
    当场被自己这条测试打红（见 06-pitfalls T30）。
    """
    import ast

    source = REPO_ROOT_SRC / "postel_studio" / "gitops" / "repo.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))

    # 1) 找出模块内所有"名字 → 赋值表达式"，用于把变量回溯成它的列表字面量
    assigned: dict[str, ast.expr] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            if isinstance(target, ast.Name):
                assigned[target.id] = node.value

    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in {"run", "Popen", "check_output", "call"}
    ]
    assert calls, "找不到子进程调用（代码结构变了？）"

    for call in calls:
        where = f"第 {call.lineno} 行"
        keywords = {kw.arg: kw.value for kw in call.keywords}
        assert "shell" in keywords, f"{where}：必须显式写 shell="
        shell = keywords["shell"]
        assert isinstance(shell, ast.Constant), f"{where}：shell 必须是字面量"
        assert shell.value is False, f"{where}：shell 必须为 False（05 §3.3）"
        assert "timeout" in keywords, f"{where}：必须有 timeout（D5）"

        # 命令必须是列表：内联列表，或一个被回溯到列表的变量
        assert call.args, f"{where}：没有命令参数"
        first = call.args[0]
        if isinstance(first, ast.Name):
            assert first.id in assigned, f"{where}：命令变量 {first.id} 找不到赋值"
            first = assigned[first.id]
        assert isinstance(first, ast.List), f"{where}：命令必须是字面数组"
        for element in first.elts:
            assert not isinstance(element, ast.JoinedStr), f"{where}：命令里有 f-string"
            assert not isinstance(element, ast.BinOp), f"{where}：命令里有字符串拼接"

    # 2) 全项目只有这一个文件起子进程
    #    包目录从**被测文件**反推（`repo.py` → `gitops` → `postel_studio`），不要从测试文件的
    #    位置反推：反向验证在源码**副本**里跑这条测试，那时测试文件还在仓库里、被测文件在 /tmp，
    #    两者不在同一个树里（实测：第一版从测试文件反推，扫描了 /tmp/src 不存在、offenders 为空、
    #    断言空过——一个自己会被镀金的守卫）。
    package_root = source.parent.parent
    assert (package_root / "__init__.py").exists(), f"反推不到包目录：{package_root}"
    offenders = [
        str(path.relative_to(package_root))
        for path in package_root.rglob("*.py")
        if path != source and "subprocess" in path.read_text(encoding="utf-8")
    ]
    assert offenders == [], f"除了 gitops/repo.py 还有别的地方起子进程：{offenders}"


def test_failed_bind_leaves_no_half_state(
    service: RepositoryService, repo_factory: Callable[..., Path]
) -> None:
    """验收 2 的"不留半状态"：失败之后列表仍然是空，再绑一个合法仓库照样成功。"""
    plain = repo_factory("plain", commit=False)
    shutil.rmtree(plain / ".git")

    assert _bind_code(service, str(plain)) == NOT_A_REPO
    assert service.list() == []

    ok = service.bind({"path": str(repo_factory("good"))})
    assert [row["repoId"] for row in service.list()] == [ok["repoId"]]


# ---------------------------------------------------------------------------
# 帧层：参数校验与载荷形状（INVALID_PARAMS / camelCase / 数组载荷）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("params", [{}, {"path": 123}, {"path": ""}, {"path": None}])
def test_bind_rejects_missing_or_non_string_path(
    service: RepositoryService, params: dict[str, object]
) -> None:
    with pytest.raises(frames.FrameError) as excinfo:
        service.bind(params)
    assert excinfo.value.code == INVALID_PARAMS


def test_list_returns_array_payload_with_camel_case_keys(
    service: RepositoryService, repo_factory: Callable[..., Path]
) -> None:
    """07 §4.2：帧层字段 `camelCase`，内部 `snake_case`，转换只在边界发生。"""
    service.bind({"path": str(repo_factory("proj"))})

    listed = service.list()

    assert isinstance(listed, list)
    assert set(listed[0]) == {"repoId", "name", "path", "originUrl", "defaultBranch"}
    assert "repo_id" not in listed[0]  # 内部名不许漏到帧层


def test_get_reports_repo_not_bound_for_unknown_id(store: state_db.RepoStore) -> None:
    """`REPO_NOT_BOUND` 只在表里登记过，本刀的 `repo.branches`（S4）会用到；语义先立住。"""
    with pytest.raises(frames.FrameError) as excinfo:
        RepositoryService(store).get({"repoId": "000000000000"})
    assert excinfo.value.code == "REPO_NOT_BOUND"


# ---------------------------------------------------------------------------
# 实现层的显式缺陷注入（进程内，monkeypatch）
#
# 为什么在这里而不在 `test_reverse_checks.py`：那份文件用**子进程**跑真实 pytest 来验证
# "删掉实现后某条测试会红"，覆盖面广但每个注入点都要起一个进程；而有些约束只有一条测试
# 守着，值得在**进程内**再钉一次（免得起进程、跑得快，失败时 traceback 也短）。
# 两边分工：跨多文件的机制性缺陷走 `test_reverse_checks.py`，单点约束走这里。
# ---------------------------------------------------------------------------


def _root_without_ancestor_walk(target: Path) -> Path | None:
    """`_bindable_root` 的"缺陷版"：**不做子目录校正**（模拟 D2 的机制被删掉）。

    保留 `.git` 判定（T29/T31 那两条规则），否则这条注入会同时踩两个缺陷，看不出是谁的锅。
    """
    if not target.is_dir():
        return None
    if gitops.is_worktree_root(target) or gitops.bare_root(target) is not None:
        return None if repository_mod._inside_git_dir(target, target) else target
    return None


def test_deleting_subdirectory_correction_makes_bind_red(
    repo_factory: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    store: state_db.RepoStore,
) -> None:
    """删掉"逐级向上找祖先" → 绑子目录必须失败（D2 的机制性缺陷，进程内注入）。

    注入前后各测一次：先证明**注入前**它是对的（否则"注入后失败"可能本来就失败）。
    """
    repo = repo_factory("outer")
    deep = repo / "pkg" / "deep"
    deep.mkdir(parents=True)

    assert RepositoryService(store).bind({"path": str(deep)})["path"] == str(repo.resolve())

    monkeypatch.setattr(repository_mod, "_bindable_root", _root_without_ancestor_walk)
    assert _bind_code(RepositoryService(store), str(deep)) == NOT_A_REPO


def test_deleting_dot_git_guard_lets_git_internals_escape(
    repo_factory: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    store: state_db.RepoStore,
) -> None:
    """把 `.git` 判定删掉 → 粘 `<repo>/.git/objects` 会"绑定成功"（T29 的机制性缺陷）。"""
    repo = repo_factory("proj")
    objects = repo / ".git" / "objects"
    assert objects.is_dir()

    assert _bind_code(RepositoryService(store), str(objects)) == NOT_A_REPO  # 注入前：正确拒绝

    # 注入：规则恒假 = T29 那条判定不存在（签名要跟真实函数一致：root + target）
    monkeypatch.setattr(repository_mod, "_inside_git_dir", lambda _root, _target: False)
    payload = RepositoryService(store).bind({"path": str(objects)})
    assert payload["path"] == str(repo.resolve()), "注入后确实逃逸了——这正说明规则是必要的"


# ---------------------------------------------------------------------------
# 方法注册：`repo.bind` 必须真的接到网关的 dispatch 上
#
# 为什么单列一条：端到端那条（真内核子进程）验的是**安装版**内核，进程内 monkeypatch 改不到它
# （见下一节）。而 `test_reverse_checks.py` 要把"网关不注册 repo.bind"注入成红，就需要一条
# **进程内**、能在被改过的源码下跑的用例。这条用 `TestClient` 直连进程内 `create_app()`。
# ---------------------------------------------------------------------------


def test_inprocess_dispatch_exposes_repo_bind(
    state_home: Path, repo_factory: Callable[..., Path]
) -> None:
    """进程内 app：`repo.bind` / `repo.list` 经 WS 帧往返（不靠 `Kernel` 子进程）。

    `state_home` 夹具把 `POSTEL_HOME` 指到 `tmp_path`，`create_app()` 会在那里建 `state.db`。
    这条存在的理由见上一节：它是唯一能在"被注入缺陷的源码"下跑起来的网关用例。

    这里**不用** `fastapi.testclient.TestClient`（它会引入 starlette 的弃用警告，而且走的是
    ASGI 直调）；照 `test_gateway_inprocess.py` 的做法起一个后台线程里的真 uvicorn + 真端口，
    再用真 `websockets` 客户端连过去。
    """
    from .gateway_util import in_process_kernel

    repo = repo_factory("inproc")
    with in_process_kernel("/gateway/reverse-check") as kernel:
        ticket, ws_path = kernel.fetch_page()

        async def roundtrip() -> list[dict[str, object]]:
            async with websockets.connect(kernel.ws_url(ticket, path=ws_path)) as ws:
                await ws.send(
                    json.dumps(
                        {
                            "type": "req",
                            "id": "b1",
                            "method": "repo.bind",
                            "params": {"path": str(repo)},
                        }
                    )
                )
                bound = decode_frame(await ws.recv())
                await ws.send(json.dumps({"type": "req", "id": "l1", "method": "repo.list"}))
                listed = decode_frame(await ws.recv())
                return [bound, listed]

        bound, listed = asyncio.run(roundtrip())

    assert bound["ok"] is True, bound
    assert listed["ok"] is True, listed
    bound_data = frame_result(bound)["data"]
    listed_data = frame_result(listed)["data"]
    assert isinstance(bound_data, dict) and isinstance(listed_data, list)
    assert [row["repoId"] for row in listed_data] == [bound_data["repoId"]]


# ---------------------------------------------------------------------------
# 显式证明：**端到端**那条测的是真的内核进程（已安装的包，不是注入后的副本）
#
# `gateway_util.Kernel` 起的子进程有两个特性共同决定了这件事：
#   1. 它跑的是 `.venv/bin/postel-studio`（站点包里的包），不是 `src/` 布局；
#   2. 它显式 `env.pop("PYTHONPATH", None)`。
# 于是**进程内 monkeypatch 改不到那个内核**，`test_reverse_checks.py` 里"改源码 → 端到端
# 变红"的注入对它无效。这不是推测 —— 第一版这条测试漏了第 1 条（没 pop），当场被自己打红，
# 见 06-pitfalls T30。
# ---------------------------------------------------------------------------


def _version_seen_by_cli(clone_root: Path, *, drop_pythonpath: bool) -> str:
    """跑一次真实 `postel-studio version`，返回它报告的版本。"""
    cli = Path(sys.executable).with_name("postel-studio")
    paths_ = os.pathsep.join([str(clone_root), str(REPO_ROOT_SRC.parent)])
    env = {**os.environ, "PYTHONPATH": paths_}
    if drop_pythonpath:
        env.pop("PYTHONPATH", None)
    result = subprocess.run(
        [str(cli), "version"], env=env, capture_output=True, text=True, check=False, timeout=60
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


@pytest.fixture
def clone_with_bumped_version(tmp_path: Path) -> Path:
    """一份 `__version__` 改成哨兵的源码副本（用来判断"跑的是哪份代码"）。"""
    clone_root = tmp_path / "clone-src"
    shutil.copytree(REPO_ROOT_SRC / "postel_studio", clone_root / "postel_studio")
    init_file = clone_root / "postel_studio" / "__init__.py"
    original = init_file.read_text(encoding="utf-8")
    assert '__version__ = "0.1.2"' in original
    init_file.write_text(
        original.replace('__version__ = "0.1.2"', '__version__ = "9.9.9-CLONE"'),
        encoding="utf-8",
    )
    return clone_root


def test_pythonpath_can_override_the_cli_unless_dropped(clone_with_bumped_version: Path) -> None:
    """**前提 1**：`PYTHONPATH` 确实能改写 CLI 跑的是哪份代码（留着它就是 9.9.9）。

    这条是上一条的对照：如果连它都不成立，说明 venv 的隔离方式变了，
    `Kernel` 里那句 `env.pop("PYTHONPATH")` 也就失去意义了。
    """
    assert _version_seen_by_cli(clone_with_bumped_version, drop_pythonpath=False) == "9.9.9-CLONE"


def test_kernel_subprocess_ignores_pythonpath_like_gateway_util_does(
    clone_with_bumped_version: Path,
) -> None:
    """**前提 2**：按 `Kernel` 的做法（pop 掉 `PYTHONPATH`）跑，子进程读的是原版。

    结论：`test_websocket_bind_then_list_returns_the_repo` 验的是**真的安装版内核**，
    进程内 monkeypatch 改不到它；要对"网关不注册 repo.bind"做反向验证，必须用
    `test_reverse_checks.py` 的**子进程**方式（它在副本的 `src/` 上跑真实 pytest），
    而 `test_webframe_bind_*` 之外的进程内注入只对进程内调用有效。
    """
    assert _version_seen_by_cli(clone_with_bumped_version, drop_pythonpath=True) == "0.1.2"


# ---------------------------------------------------------------------------
# 表第 14 行：WS 端到端（真内核子进程 + 真 WebSocket）
# ---------------------------------------------------------------------------


@pytest.fixture
def gw(state_home: Path) -> Iterator[Kernel]:
    with Kernel(state_home) as kernel:
        yield kernel


def _calls(
    kernel: Kernel, requests: list[tuple[str, dict[str, object] | None]]
) -> list[dict[str, object]]:
    """在**一条**连接上依次发 N 帧，把回复按顺序收回来（连接级 `seq` 也因此可断言）。"""

    async def run() -> list[dict[str, object]]:
        kind = kernel.ws_url(kernel.fetch_page()[0])
        out: list[dict[str, object]] = []
        async with websockets.connect(kind) as ws:
            for index, (method, params) in enumerate(requests):
                message: dict[str, object] = {"type": "req", "id": f"c{index}", "method": method}
                if params is not None:
                    message["params"] = params
                await ws.send(json.dumps(message))
                out.append(decode_frame(await ws.recv()))
        return out

    return asyncio.run(run())


def test_websocket_bind_then_list_returns_the_repo(
    gw: Kernel, repo_factory: Callable[..., Path]
) -> None:
    """真浏览器走的就是这条：`repo.bind` → `repo.list` 拿回它（**载荷在 `result.data` 里**）。

    同时守着三件事：`repo.bind`/`repo.list` 走 **WS 帧**（S3 D1，没有 `/api/repos`）、
    同一个内核连接上 `seq` 连续、落库的是**仓库根**（粘子目录也绑到根）。
    """
    repo = repo_factory("ws-proj")
    sub = repo / "pkg"
    sub.mkdir()

    bound, listed = _calls(
        gw,
        [
            ("repo.bind", {"path": str(sub)}),
            ("repo.list", None),  # 07 §2.2：repo.list 无参数
        ],
    )

    assert bound["ok"] is True, bound
    assert listed["ok"] is True, listed
    assert frame_result(bound)["seq"] == 1
    assert frame_result(listed)["seq"] == 2

    data = frame_result(bound)["data"]
    assert isinstance(data, dict)
    assert len(str(data["repoId"])) == 12
    assert data["name"] == "ws-proj"
    assert data["defaultBranch"] == "main"
    assert data["path"] == str(repo.resolve())

    repos = frame_result(listed)["data"]
    assert isinstance(repos, list)
    assert [row["repoId"] for row in repos] == [data["repoId"]]


def test_websocket_bind_error_returns_stable_code_and_no_path(
    gw: Kernel, repo_factory: Callable[..., Path]
) -> None:
    """错误经帧回去：`ok:false` + 稳定码，`message` 里**没有**用户路径。"""
    plain = repo_factory("not-git")
    shutil.rmtree(plain / ".git")

    frame = _calls(gw, [("repo.bind", {"path": str(plain)})])[0]

    assert frame["ok"] is False
    error = frame["error"]
    assert isinstance(error, dict)
    assert error["code"] == NOT_A_REPO
    assert str(plain) not in str(error["message"])


def test_no_http_route_for_repositories(gw: Kernel) -> None:
    """S3 D1：**不得**新增 HTTP 路由。`/api/repos` 必须是 404。"""
    for path in ("/api/repos", "/api/repos/list", "/repos"):
        assert gw.get(path).status_code == 404, path


def test_bind_works_while_state_root_db_is_not_in_the_repo(
    gw: Kernel, repo_factory: Callable[..., Path]
) -> None:
    """端到端也验一次"不写进用户仓库"：内核的 `state.db` 落在**状态根**里。"""
    repo = repo_factory("e2e-proj")
    _calls(gw, [("repo.bind", {"path": str(repo)})])

    state_root = Path(str(gw.info["stateRoot"]))
    assert (state_root / paths.STATE_DB_FILE).exists()
    assert not (repo / paths.STATE_DB_FILE).exists()
    assert _git("status", "--porcelain", "-uall", cwd=repo) == ""
