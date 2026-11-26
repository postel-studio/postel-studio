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
import shutil
import sqlite3
import subprocess
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
import websockets

from postel_studio.api import frames
from postel_studio.api.repository import RepositoryService
from postel_studio.storage import db as state_db
from postel_studio.storage import paths

from .gateway_util import Kernel, decode_frame, frame_result

# 07 §1.3 的码（断言只按码，不看文案）
NOT_A_REPO = "NOT_A_REPO"
BAD_CWD = "BAD_CWD"
INVALID_PARAMS = "INVALID_PARAMS"
ADAPTER_UNAVAILABLE = "ADAPTER_UNAVAILABLE"

HEX12 = r"^[0-9a-f]{12}$"


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
    ) -> Path:
        path = root / name
        path.mkdir(parents=True)
        _git("init", "-q", "--bare" if bare else "--initial-branch=main", str(path), cwd=root)
        if not bare and commit:
            # 一次提交要走 index：裸仓库没有工作区，所以这条分支只对非裸仓库成立
            _git("commit", "-q", "--allow-empty", "-m", "init", cwd=path)
        if origin is not None:
            _git("remote", "add", "origin", origin, cwd=path)
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
    """先无 origin 绑（id = 路径哈希），再 `git remote add origin` 重绑：**不产生第二行**。

    D3 的原话是"本刀不做迁移，但要记进 codemap 的未决问题——S4 之前必须解决，否则用户加了
    remote 之后仓库会'变一个'"。这里断言的是**本刀的实际行为**：行数不增（path 唯一），
    但 id 会变成 URL 哈希——把这个已知的不完美钉成一条可执行的事实，而不是口口相传。
    """
    repo = repo_factory("proj")
    _git("remote", "add", "origin", "https://github.com/o/proj.git", cwd=repo)
    from_url = service.bind({"path": str(repo)})
    _git("remote", "remove", "origin", cwd=repo)
    from_path = service.bind({"path": str(repo)})

    assert from_url["originUrl"] == "https://github.com/o/proj.git"
    assert from_path["originUrl"] is None
    assert from_url["repoId"] != from_path["repoId"], "加了 origin 之后 id 本刀就会变（已知未决）"
    assert len(service.list()) == 1, "但绝不能变成两行"
    assert service.list()[0]["repoId"] == from_path["repoId"], "留下的应是最近一次绑定的结果"


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
# 表第 10 行：origin 归一化 —— https 与 ssh 得到同一个 repo_id（D3 / 06 §2.1 不变量）
# ---------------------------------------------------------------------------


def test_https_and_ssh_origins_share_the_same_repo_id(
    repo_factory: Callable[..., Path], tmp_path: Path
) -> None:
    https = repo_factory("https", origin="https://github.com/o/p.git")
    ssh = repo_factory("ssh", origin="git@github.com:o/p.git")

    with state_db.RepoStore(tmp_path / "state.db") as store:
        service = RepositoryService(store)
        first = service.bind({"path": str(https)})
        second = service.bind({"path": str(ssh)})

    assert first["repoId"] == second["repoId"]
    assert len(str(first["repoId"])) == 12


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
