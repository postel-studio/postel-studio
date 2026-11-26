"""S5 · 差异视图：`repo.diff`（真实 git + 真 SQLite + 真 WS 帧，**不 mock git**）。

一条用例对应 `docs/design/issues/S5-diff-view.md` 的「D10 验收 → 测试映射」第 1–21 行
（用例名与表里一致；行号写在 docstring 里）。页面契约那几条是**文本断言**——它们只证明
"这句话/这个 id 在页面里"，不证明"浏览器里真的这样渲染"（06-pitfalls T9 的老问题）；
真实 DOM 行为由 `tools/acceptance/s5_diff.mjs` 验（D11）。

夹具的边界：
- `store` / `service`：**真 SQLite 文件**（`tmp_path` 下），不碰状态根；
- `repo_factory`：真 `git init`。身份与 `init.defaultBranch` 显式给，不依赖测试机的全局配置；
- `gw`：真内核子进程 + 真 WebSocket（复用 S2 的 `tests/gateway_util.py`）。它跑的是
  **已安装**的包（editable 安装 → 与本文件同一份源码），所以端到端那条验的是真实链路。

D1 的三点语义是这整个文件的骨架：`main...feature` 只报 feature 自己的变化，不把 main 在
分支点之后的提交算进来。D4 的 flag 钉死各有**反向验证**：`tests/test_reverse_checks.py`
把 `--no-color` / `--no-ext-diff` 从 `DIFF_FLAGS` 里删掉，第 12 / 13 行必须变红（D12）。
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
from postel_studio.api.repository import (
    DEFAULT_MAX_CHARS,
    MAX_MAX_CHARS,
    MIN_MAX_CHARS,
    MSG_BAD_REF,
    MSG_NO_MERGE_BASE,
    MSG_NOT_SUPPORTED_REFNAME,
    MSG_REPO_PATH_MISSING,
    RepositoryService,
)
from postel_studio.gitops import repo as gitops
from postel_studio.storage import db as state_db

from .gateway_util import Kernel, decode_frame, frame_result

# 07 §1.3 的码（断言只按码，不看文案）
BAD_REF = "BAD_REF"
INVALID_PARAMS = "INVALID_PARAMS"
REPO_NOT_BOUND = "REPO_NOT_BOUND"
ADAPTER_UNAVAILABLE = "ADAPTER_UNAVAILABLE"
NOT_SUPPORTED = "NOT_SUPPORTED"
INTERNAL = "INTERNAL"


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


def _commit(repo: Path, message: str) -> None:
    _git("add", "-A", cwd=repo)
    _git("commit", "-q", "-m", message, cwd=repo)


def _bind(service: RepositoryService, path: Path) -> str:
    """绑定一个真仓库，返回 `repoId`（`repo.diff` 的第一个参数）。"""
    payload = service.bind({"path": str(path)})
    repo_id = payload["repoId"]
    assert isinstance(repo_id, str)
    return repo_id


def _diff(
    service: RepositoryService, repo_id: str, base: str, head: str, **extra: object
) -> dict[str, object]:
    return service.diff({"repoId": repo_id, "base": base, "head": head, **extra})


def _standard_repo(repo_factory: Callable[..., Path]) -> Path:
    """D10 第 1/2/7/17 行共用的 fixture（三个数字与 D1 的示例一致：2 文件 / +3 / −1）。

    时间线：

    ```text
    main:    init ── base（f.txt）────────────── later（m.txt；三点语义**不该**算进来）
               └── feature（f.txt 改 2 加 1 删 + 新增 中文.txt）
    ```
    """
    repo = repo_factory("standard")
    (repo / "f.txt").write_text("one\ntwo\nthree\n", encoding="utf-8")
    _commit(repo, "base")
    _git("checkout", "-q", "-b", "feature", cwd=repo)
    (repo / "f.txt").write_text("one\nTWO\nthree\nfour\n", encoding="utf-8")
    (repo / "中文.txt").write_text("中文内容\n", encoding="utf-8")
    _commit(repo, "feature change")
    _git("checkout", "-q", "main", cwd=repo)
    (repo / "m.txt").write_text("main only\n", encoding="utf-8")
    _commit(repo, "main later")
    return repo


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
# D10 第 1 行：stat 精确
# ---------------------------------------------------------------------------


def test_stat_exact_numbers(service: RepositoryService, repo_factory: Callable[..., Path]) -> None:
    """**D10 第 1 行**（D5）：三个数字都断言，期望值写死（禁止 `a or b` 式断言）。

    这条 fixture 的三个数字同时覆盖「普通文本改动」与「新增中文文件」：
    `f.txt` 是 2 加 1 删，`中文.txt` 是 1 加 0 删 → 2 文件 / +3 / −1。
    """
    repo = _standard_repo(repo_factory)

    payload = _diff(service, _bind(service, repo), "main", "feature")

    assert payload["stat"] == {"filesChanged": 2, "insertions": 3, "deletions": 1}
    # 再逐个断言一次：数据形状被改坏时上面那条会红，但读报告的人需要看到"哪个数是错的"
    stat = payload["stat"]
    assert isinstance(stat, dict)
    assert stat["filesChanged"] == 2
    assert stat["insertions"] == 3
    assert stat["deletions"] == 1


# ---------------------------------------------------------------------------
# D10 第 2 行：三点语义（D1）
# ---------------------------------------------------------------------------


def test_three_dot_ignores_later_base_commits(
    service: RepositoryService, repo_factory: Callable[..., Path]
) -> None:
    """**D10 第 2 行**（D1）：main 在分支点后又提交了 `m.txt`，diff **不得**把它算进去。

    两点语义 `main feature` 会把 main 的追加报成删除（`0 1 m.txt`，实测见 S5 D1）；
    三点语义取共同祖先 → head，所以 `m.txt` 根本不出现、`filesChanged` 仍是 2。
    """
    repo = _standard_repo(repo_factory)

    payload = _diff(service, _bind(service, repo), "main", "feature")

    patch = payload["patch"]
    assert isinstance(patch, str)
    assert "m.txt" not in patch, "三点语义：main 在分支点之后的提交不能出现在 diff 里"
    assert payload["stat"] == {"filesChanged": 2, "insertions": 3, "deletions": 1}


# ---------------------------------------------------------------------------
# D10 第 3 行：二进制文件
# ---------------------------------------------------------------------------


def test_stat_counts_binary_file_without_line_counts(
    service: RepositoryService, repo_factory: Callable[..., Path]
) -> None:
    """**D10 第 3 行**（D5）：二进制文件计 **1 个文件**、加/减行按 0 计（`-` 记 0）。"""
    repo = repo_factory("binary")
    _git("checkout", "-q", "-b", "feature", cwd=repo)
    (repo / "b.bin").write_bytes(b"\x00\x01\x02binary")
    _commit(repo, "add binary")

    payload = _diff(service, _bind(service, repo), "main", "feature")

    assert payload["stat"] == {"filesChanged": 1, "insertions": 0, "deletions": 0}
    patch = payload["patch"]
    assert isinstance(patch, str)
    assert "b.bin" in patch


# ---------------------------------------------------------------------------
# D10 第 4 行：纯 mode 变更
# ---------------------------------------------------------------------------


def test_stat_counts_mode_only_change(
    service: RepositoryService, repo_factory: Callable[..., Path]
) -> None:
    """**D10 第 4 行**（D5）：chmod +x 是 `0 0` 记录——计 1 个文件、0 加 0 减。"""
    repo = repo_factory("mode")
    (repo / "run.sh").write_text("#!/bin/sh\necho hi\n", encoding="utf-8")
    _commit(repo, "add script")
    _git("checkout", "-q", "-b", "feature", cwd=repo)
    _git("update-index", "--chmod=+x", "run.sh", cwd=repo)
    _git("commit", "-q", "-m", "chmod +x", cwd=repo)

    payload = _diff(service, _bind(service, repo), "main", "feature")

    assert payload["stat"] == {"filesChanged": 1, "insertions": 0, "deletions": 0}
    patch = payload["patch"]
    assert isinstance(patch, str)
    assert "old mode 100644" in patch
    assert "new mode 100755" in patch


# ---------------------------------------------------------------------------
# D10 第 5 行：重命名 = 一删一增（`--no-renames` 的可见后果，D4）
# ---------------------------------------------------------------------------


def test_rename_counts_as_delete_plus_add(
    service: RepositoryService, repo_factory: Callable[..., Path]
) -> None:
    """**D10 第 5 行**（D4）：rename 检测**关闭**——重命名按「一删一增」计 2 个文件。

    前提必须成立：仓库显式设了 `diff.renames=true`（git 缺省就是 true，这里写明让它不依赖
    版本），否则"没检测到重命名"可能只是捡来的。MVP 裁决见 D4：不开 rename 感知视图。
    """
    repo = repo_factory("rename")
    (repo / "old-name.txt").write_text("a\nb\n", encoding="utf-8")
    _commit(repo, "base")
    _git("config", "diff.renames", "true", cwd=repo)
    _git("checkout", "-q", "-b", "feature", cwd=repo)
    _git("mv", "old-name.txt", "new-name.txt", cwd=repo)
    _commit(repo, "rename")

    payload = _diff(service, _bind(service, repo), "main", "feature")

    assert payload["stat"] == {"filesChanged": 2, "insertions": 2, "deletions": 2}
    patch = payload["patch"]
    assert isinstance(patch, str)
    assert "deleted file mode" in patch
    assert "new file mode" in patch
    assert "=>" not in patch, "--no-renames：不许出现 rename 语法"


# ---------------------------------------------------------------------------
# D10 第 6 行：空差异（成功，不是错误）
# ---------------------------------------------------------------------------


def test_same_ref_is_empty_diff_not_error(
    service: RepositoryService, repo_factory: Callable[..., Path]
) -> None:
    """**D10 第 6 行**（D6）：base 与 head 是同一个 commit → `0/0/0` + `patch==""` + 不截断。"""
    payload = _diff(service, _bind(service, _standard_repo(repo_factory)), "main", "main")

    assert payload["stat"] == {"filesChanged": 0, "insertions": 0, "deletions": 0}
    assert payload["patch"] == ""
    assert payload["truncated"] is False


# ---------------------------------------------------------------------------
# D10 第 7 行：patch 逐行种类
# ---------------------------------------------------------------------------

# D9 的行分类顺序（**必须按这个顺序判**）。测试里独立实现一份，防止实现和测试共用同一段
# 错误逻辑却互相"证明"。
_META_PREFIXES = (
    "diff --git ",
    "index ",
    "new file mode ",
    "deleted file mode ",
    "old mode ",
    "new mode ",
    "\\ No newline at end of file",
)


def _line_kind(line: str) -> str:
    if line.startswith(_META_PREFIXES):
        return "meta"
    if line.startswith("@@"):
        return "hunk"
    if line.startswith(("+++ ", "--- ")):
        return "meta"
    if line.startswith("+"):
        return "add"
    if line.startswith("-"):
        return "del"
    return "ctx"


def test_patch_contains_expected_line_kinds(
    service: RepositoryService, repo_factory: Callable[..., Path]
) -> None:
    """**D10 第 7 行**（D9）：服务端返回的 patch 里 add / del / hunk / meta / ctx 各至少一行。

    同时钉住分类顺序的两个易错点：`+++ b/` 是 **meta 不是 add**；`@@` 是 hunk。
    """
    repo = _standard_repo(repo_factory)
    patch = _diff(service, _bind(service, repo), "main", "feature")["patch"]
    assert isinstance(patch, str)

    kinds = [_line_kind(line) for line in patch.splitlines()]
    for expected in ("add", "del", "hunk", "meta", "ctx"):
        assert expected in kinds, f"patch 里没有 {expected} 行：{kinds}"

    plus_plus_plus = [line for line in patch.splitlines() if line.startswith("+++ ")]
    assert plus_plus_plus, "fixture 必须产生文件头 +++ 行"
    assert all(_line_kind(line) == "meta" for line in plus_plus_plus), "+++ 是文件头，不是 add"
    assert any(line.startswith("+") and not line.startswith("+++ ") for line in patch.splitlines())


# ---------------------------------------------------------------------------
# D10 第 8 行：截断边界两侧
# ---------------------------------------------------------------------------


def _big_diff_repo(repo_factory: Callable[..., Path]) -> Path:
    """造一个完整 patch 长度落在 `[MIN_MAX_CHARS, MAX_MAX_CHARS]` 之间的仓库。"""
    repo = repo_factory("big")
    _git("checkout", "-q", "-b", "feature", cwd=repo)
    lines = [f"line-{index:04d}-padding\n" for index in range(3000)]
    (repo / "big.txt").write_text("".join(lines), encoding="utf-8")
    _commit(repo, "big file")
    return repo


def test_truncation_boundary_both_sides(
    service: RepositoryService, repo_factory: Callable[..., Path]
) -> None:
    """**D10 第 8 行**（D6）：长度恰好 `maxChars` → 不截断；`maxChars + 1` → 截断且长度精确。

    先用上限 `MAX_MAX_CHARS` 拿到**完整** patch（前提断言：它确实没被截断、且长度 ≥ 下限），
    再拿它的真实长度当边界——这样"边界"是实测值，不是拍脑袋的数字。
    """
    repo_id = _bind(service, _big_diff_repo(repo_factory))

    full = _diff(service, repo_id, "main", "feature", maxChars=MAX_MAX_CHARS)
    assert full["truncated"] is False, "前提：MAX_MAX_CHARS 必须能装下完整 patch"
    full_patch = full["patch"]
    assert isinstance(full_patch, str)
    length = len(full_patch)
    assert length >= MIN_MAX_CHARS, "前提：patch 要够长，边界才能落在合法区间里"

    exact = _diff(service, repo_id, "main", "feature", maxChars=length)
    assert exact["truncated"] is False
    exact_patch = exact["patch"]
    assert isinstance(exact_patch, str)
    assert exact_patch == full_patch
    assert len(exact_patch) == length

    one_less = _diff(service, repo_id, "main", "feature", maxChars=length - 1)
    assert one_less["truncated"] is True
    shorter_patch = one_less["patch"]
    assert isinstance(shorter_patch, str)
    assert len(shorter_patch) == length - 1
    assert shorter_patch == full_patch[:-1]


# ---------------------------------------------------------------------------
# D10 第 9 行：maxChars 非法
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad_value", [0, 999, 200_001, "1000", True, 1.5])
def test_max_chars_rejects_out_of_range_and_bool(
    service: RepositoryService, repo_factory: Callable[..., Path], bad_value: object
) -> None:
    """**D10 第 9 行**（D6）：越界 / 类型错 / `bool` → `INVALID_PARAMS`（**不夹紧**）。

    `True` 是 `int` 的子类（`isinstance(True, int)` 为真），必须先排除 `bool`
    ——否则 `maxChars: true` 会被当成 `1`。
    """
    repo_id = _bind(service, _standard_repo(repo_factory))
    with pytest.raises(frames.FrameError) as excinfo:
        _diff(service, repo_id, "main", "feature", maxChars=bad_value)
    assert excinfo.value.code == INVALID_PARAMS


def test_max_chars_defaults_and_limits_match_the_contract() -> None:
    """D6 的常量是**契约值**（改动 = 破坏契约），在这里钉死。"""
    assert DEFAULT_MAX_CHARS == 100_000
    assert MIN_MAX_CHARS == 1_000
    assert MAX_MAX_CHARS == 200_000


# ---------------------------------------------------------------------------
# D10 第 10/11 行：非法 ref
# ---------------------------------------------------------------------------


def test_bad_refs_are_bad_ref(
    service: RepositoryService, repo_factory: Callable[..., Path]
) -> None:
    """**D10 第 10 行**（D2/D7 #5）：不存在的名字、tree id → `BAD_REF` + 「分支或提交不存在」。

    tree id 是这一条的关键：`resolve_commit` 必须用 `^{commit}` 强制 commit 类型，
    并在 L0 就返回 `None`（反向注入 D12 #4 去掉 `^{commit}` 后，这条断言必须变红）。
    服务层的文案也必须是 ref 解析那句，**不是** no-merge-base 那句——否则"tree id 被拒"
    与"无共同祖先"在测试里分不清。
    """
    repo = _standard_repo(repo_factory)
    repo_id = _bind(service, repo)

    with pytest.raises(frames.FrameError) as missing:
        _diff(service, repo_id, "no-such-ref", "feature")
    assert missing.value.code == BAD_REF
    assert missing.value.message == MSG_BAD_REF

    tree = _git("rev-parse", "main^{tree}", cwd=repo).strip()
    assert re.fullmatch(r"[0-9a-f]{40}", tree), tree
    assert gitops.resolve_commit(repo, tree) is None, "D2：tree id 不能被当成 commit"

    with pytest.raises(frames.FrameError) as tree_error:
        _diff(service, repo_id, "main", tree)
    assert tree_error.value.code == BAD_REF
    assert tree_error.value.message == MSG_BAD_REF


def test_ref_starting_with_dash_is_rejected(
    service: RepositoryService, repo_factory: Callable[..., Path]
) -> None:
    """**D10 第 11 行**（D3.1）：`-` 开头的 ref 不得被当成 git 选项 → `INVALID_PARAMS`。

    这一条守的是注入面：`--no-index` / `--output=/tmp/x` 这类参数在 `--end-of-options`
    之前会被 git 当选项。入站直接拒绝，用户字符串根本到不了 git。
    """
    repo_id = _bind(service, _standard_repo(repo_factory))
    for bad in ("--no-index", "-", "--no-index^{commit}", "--output=/tmp/x"):
        with pytest.raises(frames.FrameError) as excinfo:
            _diff(service, repo_id, bad, "feature")
        assert excinfo.value.code == INVALID_PARAMS, bad


@pytest.mark.parametrize(
    "bad_ref",
    ["", "   ", "main\nhead", "a\x00b", "x\x7fy", "a" * 201, 123, None],
)
def test_ref_with_invalid_shape_is_invalid_params(
    service: RepositoryService, repo_factory: Callable[..., Path], bad_ref: object
) -> None:
    """**D10 第 10 行**（D3.1）：空 / 控制字符 / 超长 / 非字符串 → `INVALID_PARAMS`。"""
    repo_id = _bind(service, _standard_repo(repo_factory))
    with pytest.raises(frames.FrameError) as excinfo:
        _diff(service, repo_id, bad_ref, "feature")  # type: ignore[arg-type]
    assert excinfo.value.code == INVALID_PARAMS


def test_ref_with_lone_surrogate_is_invalid_params(
    service: RepositoryService, repo_factory: Callable[..., Path]
) -> None:
    """**D10 第 10 行**（D3.1 的实测依据）：JSON 帧里能带进来的 lone surrogate 必须挡掉。

    `json.loads('{"base": "\\udcff"}')` 是合法 JSON，得到的是一个 lone surrogate 的 `str`；
    Python 给子进程编码 argv 时用 `surrogateescape`，它在**字节层**变成原始 `0xFF`——
    正好是 git 里那个非 UTF-8 ref 名的字节（实测 `rev-parse --verify` 真的解析成功）。
    不挡就是「一侧拒绝展示、另一侧接受解析」的漏洞。
    """
    repo_id = _bind(service, _standard_repo(repo_factory))
    lone_surrogate = json.loads('"\\udcff"')
    with pytest.raises(frames.FrameError) as excinfo:
        _diff(service, repo_id, lone_surrogate, "feature")
    assert excinfo.value.code == INVALID_PARAMS


# ---------------------------------------------------------------------------
# D10 第 12/13/14 行：用户配置隔离与非 UTF-8 内容
# ---------------------------------------------------------------------------


def test_patch_has_no_ansi_when_color_ui_always(
    service: RepositoryService, repo_factory: Callable[..., Path]
) -> None:
    """**D10 第 12 行**（D4）：仓库设 `color.ui=always` → patch 内**没有** `\\x1b`。

    先证明前提：同一对 ref、不带 `--no-color` 的真 git 输出里**确实**有 ANSI 转义；
    否则"没有 ANSI"可能只是这个 git 版本默认不上色（测试就空转了）。
    """
    repo = _standard_repo(repo_factory)
    _git("config", "color.ui", "always", cwd=repo)
    raw = _git("diff", "--no-ext-diff", "main...feature", cwd=repo)
    assert "\x1b[" in raw, "前提：color.ui=always 时真 git 输出带 ANSI"

    payload = _diff(service, _bind(service, repo), "main", "feature")
    patch = payload["patch"]
    assert isinstance(patch, str)
    assert "\x1b" not in patch, "--no-color 必须把 ANSI 全部去掉"


def test_patch_ignores_external_diff_driver(
    service: RepositoryService, repo_factory: Callable[..., Path]
) -> None:
    """**D10 第 13 行**（D4）：`diff.external` 指向假命令 → 仍然成功（`--no-ext-diff`）。

    前提：不带 `--no-ext-diff` 时真 git **失败**（`fatal: 外部 diff 退出，停止在 f.txt`，
    exit 128）——否则这条测试证明不了 flag 起了作用。
    """
    repo = _standard_repo(repo_factory)
    _git("config", "diff.external", "false", cwd=repo)
    raw = _git_bytes("diff", "main...feature", cwd=repo)
    assert raw.returncode != 0, "前提：外部 diff 命令退出码非零时真 git 必须失败"

    payload = _diff(service, _bind(service, repo), "main", "feature")
    patch = payload["patch"]
    assert isinstance(patch, str)
    assert patch.startswith("diff --git "), "--no-ext-diff 之后 patch 是 git 自己的输出"


def test_non_utf8_content_is_replaced_not_fatal(
    service: RepositoryService, repo_factory: Callable[..., Path]
) -> None:
    """**D10 第 14 行**（D3.3）：文件内容是非法 UTF-8 → 成功、含 U+FFFD、**不 500**。

    仓库里有 latin-1 / 历史遗留的非 UTF-8 内容是常态；为它回 500 是错的。patch 只经
    `textContent` 进 DOM、永不回传 git，所以宽松解码是正确的（D3.3 的裁决）。
    """
    repo = repo_factory("latin1")
    _git("checkout", "-q", "-b", "feature", cwd=repo)
    (repo / "latin1.txt").write_bytes(b"\xff\xfe latin1 bad\n")
    _commit(repo, "latin1")

    payload = _diff(service, _bind(service, repo), "main", "feature")

    patch = payload["patch"]
    assert isinstance(patch, str)
    assert "\ufffd" in patch, "非法字节应变成 U+FFFD，而不是让整条 diff 失败"
    assert "latin1.txt" in patch


# ---------------------------------------------------------------------------
# D10 第 15 行：仓库路径没了（D7 #4，先于 ref 判）
# ---------------------------------------------------------------------------


def test_deleted_repo_path_is_repo_not_bound(
    service: RepositoryService, repo_factory: Callable[..., Path]
) -> None:
    """**D10 第 15 行**（D7 #4）：目录被删 + **任意** ref → `REPO_NOT_BOUND`（不是 `BAD_REF`）。

    `rev-parse` 在没了的仓库上同样失败；不先探测路径就会把"仓库没了"报成"ref 不存在"，
    用户会去改 ref，而真正的问题是仓库被删 / 搬走了（D7 #4 点名的顺序陷阱）。
    """
    repo = repo_factory("gone")
    repo_id = _bind(service, repo)
    shutil.rmtree(repo)

    with pytest.raises(frames.FrameError) as excinfo:
        _diff(service, repo_id, "main", "main")  # ref 本身合法，但仓库没了
    assert excinfo.value.code == REPO_NOT_BOUND
    assert excinfo.value.message == MSG_REPO_PATH_MISSING
    assert str(repo) not in excinfo.value.message  # 05 §4.2：错误文案不回显用户路径


def test_repo_whose_dot_git_was_removed_is_repo_not_bound(
    service: RepositoryService, repo_factory: Callable[..., Path]
) -> None:
    """**D7 #4** 的另一半：目录还在但 `.git` 被删 → `REPO_NOT_BOUND`，不许被 `BAD_REF` 盖住。"""
    repo = repo_factory("no-git-dir")
    repo_id = _bind(service, repo)
    shutil.rmtree(repo / ".git")

    with pytest.raises(frames.FrameError) as excinfo:
        _diff(service, repo_id, "main", "feature")
    assert excinfo.value.code == REPO_NOT_BOUND
    assert excinfo.value.message == MSG_REPO_PATH_MISSING


# ---------------------------------------------------------------------------
# D10 第 16 行：无共同祖先
# ---------------------------------------------------------------------------


def test_unrelated_histories_are_bad_ref(
    service: RepositoryService, repo_factory: Callable[..., Path]
) -> None:
    """**D10 第 16 行**（D1）：两个不相干历史 → `BAD_REF` +「没有共同祖先」文案。

    实测：`git diff --numstat main...other` 在无 merge base 时回 exit 128、stdout 空
    （`fatal: main...other：无合并基线`）。码表里最贴的是 `BAD_REF`（**不新增码**）。
    """
    repo = repo_factory("orphan")
    _git("checkout", "-q", "--orphan", "other", cwd=repo)
    (repo / "other.txt").write_text("other\n", encoding="utf-8")
    _commit(repo, "orphan root")  # `git checkout --orphan` 之后第一次提交是全新历史

    with pytest.raises(frames.FrameError) as excinfo:
        _diff(service, _bind(service, repo), "main", "other")
    assert excinfo.value.code == BAD_REF
    assert excinfo.value.message == MSG_NO_MERGE_BASE


# ---------------------------------------------------------------------------
# D10 第 17 行：非 ASCII 文件名可读
# ---------------------------------------------------------------------------


def test_non_ascii_path_is_readable_not_escaped(
    service: RepositoryService, repo_factory: Callable[..., Path]
) -> None:
    """**D10 第 17 行**（D4）：中文文件名在 patch 里是**字面**中文，不是 `\\346` 八进制转义。"""
    repo = _standard_repo(repo_factory)
    patch = _diff(service, _bind(service, repo), "main", "feature")["patch"]
    assert isinstance(patch, str)
    assert "中文.txt" in patch
    assert "\\346" not in patch, "core.quotePath=false 之后不该出现八进制转义"
    assert "中文内容" in patch


# ---------------------------------------------------------------------------
# D10 第 19/20 行：U15 结案（非 UTF-8 refname）
# ---------------------------------------------------------------------------


def _create_non_utf8_branch(repo: Path, name: bytes = b"bad-\xff") -> None:
    """用**字节**建一个非 UTF-8 的分支名（POSIX 上 argv 可以是 bytes）。"""
    result = _git_bytes("branch", name, cwd=repo)
    assert result.returncode == 0, result.stderr


def test_branches_non_utf8_refname_is_not_supported(
    service: RepositoryService, repo_factory: Callable[..., Path]
) -> None:
    """**D10 第 19 行**（D3.2 / U15 结案）：仓库有非 UTF-8 分支名 → `NOT_SUPPORTED`。

    S4 时代这里抛未捕获的 `UnicodeDecodeError` → 网关 `except Exception` 回
    `INTERNAL`（「内部错误，请查看终端」）。本刀改成稳定的 `NOT_SUPPORTED` + 一句人话。
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

    并且 **HEAD 正常时该仓库仍能绑**——这条同时证明"拒绝"只发生在真的解不开名字时，
    不是把整个仓库类别误杀。HEAD 用 `symbolic-ref` 直接指过去（不 checkout，避免工作区噪声）。
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


# ---------------------------------------------------------------------------
# D7 第 7/8/9 行的映射（码的映射本身；git 的真实失败由其它用例用真仓库守）
# ---------------------------------------------------------------------------


def test_missing_git_maps_to_adapter_unavailable(
    service: RepositoryService, repo_factory: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """D7 #7：`git` 不存在 → `ADAPTER_UNAVAILABLE`（可重试）。

    先把仓库绑好（那时 git 还在），再清空 `PATH`：`diff` 会走到 `git_executable()` 抛
    `GitUnavailableError`——**不 mock 解析**，只是让"找不到 git"真实发生。
    """
    repo = _standard_repo(repo_factory)
    repo_id = _bind(service, repo)

    monkeypatch.setenv("PATH", "")
    with pytest.raises(frames.FrameError) as excinfo:
        _diff(service, repo_id, "main", "feature")
    assert excinfo.value.code == ADAPTER_UNAVAILABLE


def test_git_timeout_maps_to_internal(
    service: RepositoryService, repo_factory: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """D7 #8：`git` 超时 → `INTERNAL`（同 S3 D6；只在边界注入异常，钉住码映射）。"""
    repo_id = _bind(service, _standard_repo(repo_factory))

    def _raise_timeout(_root: Path, _ref: str) -> str | None:
        raise gitops.GitTimeoutError("模拟超时")

    monkeypatch.setattr(gitops, "resolve_commit", _raise_timeout)
    with pytest.raises(frames.FrameError) as excinfo:
        _diff(service, repo_id, "main", "feature")
    assert excinfo.value.code == INTERNAL


def test_undecodable_output_maps_to_not_supported(
    service: RepositoryService, repo_factory: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """D7 #9：L0 抛 `GitOutputUndecodableError` → 边界一律 `NOT_SUPPORTED`（D3.2）。"""
    repo_id = _bind(service, _standard_repo(repo_factory))

    def _raise_undecodable(_root: Path, _ref: str) -> str | None:
        raise gitops.GitOutputUndecodableError("模拟非 UTF-8")

    monkeypatch.setattr(gitops, "resolve_commit", _raise_undecodable)
    with pytest.raises(frames.FrameError) as excinfo:
        _diff(service, repo_id, "main", "feature")
    assert excinfo.value.code == NOT_SUPPORTED
    assert excinfo.value.message == MSG_NOT_SUPPORTED_REFNAME


# ---------------------------------------------------------------------------
# D10 第 21 行：端到端（真 WS 帧）
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


def test_ws_repo_diff_matches_service(
    gw: Kernel, repo_factory: Callable[..., Path], tmp_path: Path
) -> None:
    """**D10 第 21 行**：真 WS 帧 `repo.diff` 回 `{data:{stat,patch,truncated}, seq}`，与直调一致。

    直调那一路另开一个真 SQLite、绑**同一个** git 仓库：`repoId` 由路径决定（S3.5 D1），
    所以两边的载荷应当逐字段相同——这正是"帧层只是转手"的证据。
    """
    repo = _standard_repo(repo_factory)

    with state_db.RepoStore(tmp_path / "direct.db") as direct_store:
        direct = RepositoryService(direct_store)
        direct_repo_id = _bind(direct, repo)
        expected = _diff(direct, direct_repo_id, "main", "feature")

    bound, diffed = _ws_calls(
        gw,
        [
            ("repo.bind", {"path": str(repo)}),
            ("repo.diff", {"repoId": direct_repo_id, "base": "main", "head": "feature"}),
        ],
    )

    assert bound["ok"] is True, bound
    assert diffed["ok"] is True, diffed
    assert frame_result(bound)["seq"] == 1
    assert frame_result(diffed)["seq"] == 2

    data = frame_result(diffed)["data"]
    assert isinstance(data, dict)
    assert set(data) == {"stat", "patch", "truncated"}
    assert data == expected, "帧层载荷必须与直调服务层逐字段一致"
    assert data["stat"] == {"filesChanged": 2, "insertions": 3, "deletions": 1}


def test_ws_repo_diff_error_frame_carries_stable_code(gw: Kernel) -> None:
    """D7 的帧形态：坏 ref → `ok:false` + `BAD_REF`（不是 500、不是 `INTERNAL`）。"""
    (frame,) = _ws_calls(gw, [("repo.diff", {"repoId": "000000000000", "base": "a", "head": "b"})])

    assert frame["ok"] is False
    error = frame["error"]
    assert isinstance(error, dict)
    assert error["code"] == REPO_NOT_BOUND
