"""S5 · U15 结案：非 UTF-8 refname 不再把 `repo.branches` / `repo.bind` 变成 `INTERNAL`。

S4 的已知洞（06-pitfalls U15）：`run_git(text=True)` 严格解码，仓库里有非 UTF-8 的 refname
时抛未捕获的 `UnicodeDecodeError`——它不在 `GIT_ERRORS` 里，一路逃到网关的
`except Exception` → `INTERNAL`（用户只看到「内部错误」）。S5 D3.2 的裁决：**严格解码 +
显式异常 + 稳定码 `NOT_SUPPORTED`**，绝不 mangled 回传（mangled 名字回传 git 会变成
`BAD_REF`，甚至指向别的 ref——比报错更糟）。

本文件随后会在 S5 的 `repo.diff` 切片里长成完整验收（D10 的 24 行映射）。
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from postel_studio.api import frames
from postel_studio.api.repository import MSG_NOT_SUPPORTED_REFNAME, RepositoryService
from postel_studio.storage import db as state_db

# 07 §1.3 的码（断言只按码，不看文案）
NOT_SUPPORTED = "NOT_SUPPORTED"


# ---------------------------------------------------------------------------
# 脚手架：真 git（身份显式给，别依赖跑测试机器的全局配置）
# ---------------------------------------------------------------------------


def _git(*args: str, cwd: Path) -> str:
    """真 `git`（测试自己的脚手架，不是被测代码）。失败即测试失败。"""
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


def _git_bytes(*args: str | bytes, cwd: Path) -> subprocess.CompletedProcess[bytes]:
    """真 `git`，**字节**模式：造 / 读非 UTF-8 的 refname 时不能走 text=True。"""
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, check=False)


def _init_repo(path: Path) -> Path:
    path.mkdir(parents=True)
    _git("init", "-q", "--initial-branch=main", str(path), cwd=path.parent)
    _git("commit", "-q", "--allow-empty", "-m", "init", cwd=path)
    return path


def _bind(service: RepositoryService, path: Path) -> str:
    """绑定一个真仓库，返回 `repoId`。"""
    payload = service.bind({"path": str(path)})
    repo_id = payload["repoId"]
    assert isinstance(repo_id, str)
    return repo_id


def _create_non_utf8_branch(repo: Path, name: bytes = b"bad-\xff") -> None:
    """用**字节**建一个非 UTF-8 的分支名（POSIX 上 argv 可以是 bytes）。"""
    result = _git_bytes("branch", name, cwd=repo)
    assert result.returncode == 0, result.stderr


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
    """`make(name) -> Path`：在 `tmp_path/repos/` 下建真仓库（带一个初始提交）。"""
    root = tmp_path / "repos"
    root.mkdir(exist_ok=True)

    def make(name: str = "proj") -> Path:
        return _init_repo(root / name)

    return make


# ---------------------------------------------------------------------------
# U15：两条路径（列分支 / 绑定）都回 NOT_SUPPORTED，不再是 INTERNAL
# ---------------------------------------------------------------------------


def test_branches_non_utf8_refname_is_not_supported(
    service: RepositoryService, repo_factory: Callable[..., Path]
) -> None:
    """**D10 第 19 行**（D3.2 / U15 结案）：仓库有非 UTF-8 分支名 → `NOT_SUPPORTED`。

    前提：`for-each-ref` 的原始字节里**真的**有那个 `0xFF`（否则这条测试没验到解码路径）。
    """
    repo = repo_factory("non-utf8-ref")
    _create_non_utf8_branch(repo)
    raw = _git_bytes("-C", str(repo), "for-each-ref", "refs/heads/", cwd=repo.parent)
    assert b"\xff" in raw.stdout, "前提：分支名里真的存在非 UTF-8 字节"

    repo_id = _bind(service, repo)  # 绑定本身不列分支，应当成功
    with pytest.raises(frames.FrameError) as excinfo:
        service.branches({"repoId": repo_id})
    assert excinfo.value.code == NOT_SUPPORTED
    assert excinfo.value.message == MSG_NOT_SUPPORTED_REFNAME


def test_bind_non_utf8_branch_is_not_supported(
    service: RepositoryService, repo_factory: Callable[..., Path]
) -> None:
    """**D10 第 20 行**（D3.2 / U15 结案）：HEAD 指向非 UTF-8 分支 →
    `repo.bind` 回 `NOT_SUPPORTED`。

    并且 **HEAD 正常时该仓库仍能绑**——这条同时证明"拒绝"只发生在真的解不开名字时。
    HEAD 用 `symbolic-ref` 直接指过去（不 checkout，避免工作区噪声）。
    """
    repo = repo_factory("non-utf8-head")
    bound = _bind(service, repo)  # HEAD 正常（main）→ 能绑
    assert bound

    _create_non_utf8_branch(repo)
    result = _git_bytes("symbolic-ref", "HEAD", b"refs/heads/bad-\xff", cwd=repo)
    assert result.returncode == 0, result.stderr

    with pytest.raises(frames.FrameError) as excinfo:
        service.bind({"path": str(repo)})
    assert excinfo.value.code == NOT_SUPPORTED
    assert excinfo.value.message == MSG_NOT_SUPPORTED_REFNAME
