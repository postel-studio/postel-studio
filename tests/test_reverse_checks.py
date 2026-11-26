"""S3 的**反向验证**固化：注入缺陷 → 断言已有的某条测试真的会红。

**为什么要一个文件专门干这件事**：正着跑测试只能证明"现在的代码是绿的"，
不能证明"测试守得住这条约束"。S3 实施期间我在 `/tmp` 里手跑了 15 条反向验证，
其中一条当场暴露了一个假测试（`assert GIT_TIMEOUT_SECONDS > 0` 把超时改成 0 也还是绿）。
手工跑在 `/tmp` 里 = 不可复现、下一个人不会跑 —— 所以固化进来（06-pitfalls T23 的同一教训）。

## 实现方式（为什么不 monkeypatch）

一条反向验证的语义是「**改掉实现文件的那一行**，然后跑**另一条测试**看它红」。要做到这件事，
候选方案只有两种：

- **同进程 monkeypatch**：那些测试要跑真实 git，拆成可 patch 的形态等于改测试本身；
  也 patch 不到"删掉一行代码"这种缺陷。
- **子进程里跑真实 pytest**（采用）：慢一点，但验证的是真的那条测试，注入的也是真的代码改动。

所以这里：把源码文件复制到 `tmp_path`、文本替换掉关键片段、用 `PYTHONPATH` 指向副本跑
真实 pytest、断言退出码非零（测试变红）、最后校验**副本**是不是真的被改了、**原文件**有没有被动。

## 口径与预算

每条是一个真实 pytest 进程，约 0.2–0.8 秒（实测整套 ~4 秒；`gw` 那条要起内核，约 0.5 秒）。
远低于"不许让运行时间翻倍"的红线。如果将来变慢，先合并断言再考虑加 marker ——
**不要**删条目，删了这条防线就没了。

## 一个必须记住的陷阱（T30）

`tests/gateway_util.py:Kernel` 起的是 `.venv/bin/postel-studio`，它装载的是**已安装**的包、
而且显式 `env.pop("PYTHONPATH", None)`。所以：

- 用 `PYTHONPATH` 指向副本**改不到那个内核**；
- 反过来，任何"改网关 → 端到端测试变红"的想法在这里都不成立。

要让**真内核**也读到副本，得用 `python -m pytest`（`-m` 会把 cwd 放进 `sys.path[0]`）。
第二条注入（`repo.bind` 未注册）因此改成跑**进程内 app** 的测试，而不是端到端那条。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = REPO_ROOT / "src"
# 跑被测测试时只给它**一个**测试节点，别让它跑整个套件（会套娃、也会超时）
RUN_TIMEOUT_SECONDS = 120


def _target_test(name: str, *, file: str = "test_bind") -> str:
    """被测测试的节点 id。

    缺省在 `tests/test_bind.py`（S3 的验收文件）；S4 的分支列表注入指向
    `tests/test_branches.py`。
    """
    return f"tests/{file}.py::{name}"


# (注入点文件, 原始片段, 替换成什么, 应当变红的测试, 缺陷说明)
INJECTIONS: tuple[tuple[str, str, str, str, str], ...] = (
    # ---- 05 §3.3 / D5：子进程 ----
    (
        "postel_studio/gitops/repo.py",
        'env["GIT_TERMINAL_PROMPT"] = "0"',
        "pass",
        _target_test("test_bind_uses_git_in_a_way_that_cannot_prompt"),
        "去掉 GIT_TERMINAL_PROMPT=0（git 会挂住等密码）",
    ),
    (
        "postel_studio/gitops/repo.py",
        'env["GIT_CONFIG_NOSYSTEM"] = "1"',
        "pass",
        _target_test("test_bind_uses_git_in_a_way_that_cannot_prompt"),
        "去掉 GIT_CONFIG_NOSYSTEM=1（用户机器配置会影响结果）",
    ),
    (
        "postel_studio/gitops/repo.py",
        "GIT_TIMEOUT_SECONDS: Final = 5.0",
        "GIT_TIMEOUT_SECONDS: Final = 0.0",
        _target_test("test_bind_uses_git_in_a_way_that_cannot_prompt"),
        "超时设成 0（等于没有超时）",
    ),
    (
        "postel_studio/gitops/repo.py",
        "            shell=False,",  # 带缩进：只命中 run_git 里的那处调用
        "            shell=True,",
        _target_test("test_git_subprocess_shell_is_disabled"),
        "把 shell=False 改成 True（05 §3.3 禁止）",
    ),
    # ---- 05 §4.2 / D6：错误不回显用户输入 ----
    (
        "postel_studio/api/repository.py",
        "raise _fail(NOT_A_REPO, MSG_NOT_A_REPO)",
        "raise _fail(NOT_A_REPO, MSG_NOT_A_REPO + str(target))",
        _target_test("test_not_a_repo_message_does_not_echo_any_user_path"),
        "错误 message 里拼上用户路径（05 §4.2 违规）",
    ),
    (
        "postel_studio/api/repository.py",
        "        if not Path(raw).is_absolute():\n            raise _fail(BAD_CWD, MSG_BAD_CWD)",
        "        if False:\n            pass",
        _target_test("test_bind_relative_path_is_bad_cwd"),
        "不拒绝相对路径（D4）",
    ),
    (
        "postel_studio/api/repository.py",
        "    if not target.is_dir():\n        return None",
        "    if not target.is_dir():\n        return target",
        _target_test("test_bind_file_path_is_not_a_repo"),
        "把文件路径当成仓库根",
    ),
    # ---- D2：子目录校正（「用户以为绑了 A 其实绑了 B」）----
    (
        "postel_studio/api/repository.py",
        "        root = next(\n"
        "            (ancestor for ancestor in target.parents"
        " if gitops.is_worktree_root(ancestor)),\n"
        "            None,\n"
        "        )",
        "        root = target",  # 注入：不做校正，直接把子目录当根
        _target_test("test_bind_subdirectory_stores_repo_root_not_the_subdirectory"),
        "去掉子目录校正（D2）",
    ),
    (
        "postel_studio/api/repository.py",
        '    if any(part == ".git" for part in relative.parts):\n        return True',
        '    if target.name == ".git":\n        return True',
        _target_test("test_bind_path_inside_the_git_directory_is_not_a_repo"),
        "T29：只判尾段 .git，`.git/objects` 会逃逸",
    ),
    # ---- D3：repo_id ----
    # T29/T31 是一对孪生缺陷：拒绝得太少 → `.git/objects` 逃逸；拒绝得太多 → 误杀
    # "父目录恰好叫 .git" 的合法仓库。两条都要有锁，只锁一边等于没锁。
    (
        "postel_studio/api/repository.py",
        "    relative = target.relative_to(root)\n"
        '    if any(part == ".git" for part in relative.parts):\n'
        "        return True",
        "    return False",  # 注入：T29 那条判定整体失效（`.git` 内部不再被拒）
        _target_test("test_bind_path_inside_the_git_directory_is_not_a_repo"),
        "T29：删掉 `.git` 判定",
    ),
    (
        "postel_studio/api/repository.py",
        "    relative = target.relative_to(root)\n"
        '    if any(part == ".git" for part in relative.parts):\n'
        "        return True",
        '    return any(part == ".git" for part in target.parts)',  # 注入：看整条绝对路径
        _target_test("test_repo_root_whose_parent_is_named_dot_git_still_binds"),
        "T31：把 `.git` 判定改成「整条绝对路径任意段 = .git」→ 误杀合法仓库",
    ),
    (
        "postel_studio/domain/models.py",
        "REPO_ID_LENGTH: Final = 12",
        "REPO_ID_LENGTH: Final = 16",
        _target_test("test_bind_regular_repo_returns_hex12_id_name_and_main"),
        "repo_id 长度改成 16（06 §2.1 / 07 §4.1 要求 12）",
    ),
    (
        "postel_studio/domain/models.py",
        'return f"{host}/{path}" if path else host',
        "return url",
        _target_test("test_https_and_ssh_origins_share_the_same_repo_id"),
        "origin 不归一化（https 与 ssh 会得不同 id）",
    ),
    # ---- 05 §4.1：state.db 权限 ----
    (
        "postel_studio/storage/db.py",
        "_chmod(path_for_chmod, 0o600)",
        "pass",
        _target_test("test_data_survives_reopening_the_store"),
        "不收紧 state.db 的 0600 权限",
    ),
    # ---- D6：错误码 ----
    (
        "postel_studio/api/repository.py",
        "            # D5：git 不存在**不崩内核**，回 503 类码；S19 的 doctor 负责告诉用户怎么装\n"
        "            raise _fail(ADAPTER_UNAVAILABLE, MSG_ADAPTER_UNAVAILABLE) from None",
        "            raise _fail(frames.INTERNAL, MSG_GIT_TIMEOUT) from None",
        _target_test("test_missing_git_is_adapter_unavailable"),
        "ADAPTER_UNAVAILABLE 翻成 INTERNAL",
    ),
    # ---- D1/S3 方法表：WS 帧 ----
    # 注意跑的是**进程内 app**（`create_app()` + TestClient），不是 `gateway_util.Kernel` 起的
    # 真内核子进程——后者用站点包装载已安装的包、且 `env.pop("PYTHONPATH")`，看不到副本。
    # 见 `test_bind.py::test_kernel_subprocess_ignores_pythonpath_like_gateway_util_does`。
    (
        "postel_studio/api/gateway.py",
        '    if method == "repo.bind":  # 07 §2.2 / 07 §4.1；S3 D1：走帧，不开 HTTP 路由\n'
        "        return providers.repo_bind(params)",
        "    if False:\n        return None",
        _target_test("test_inprocess_dispatch_exposes_repo_bind"),
        "网关不注册 repo.bind",
    ),
    # ---- S4：分支列表（D1/D2/D3/D5 + T33）----
    (
        "postel_studio/api/repository.py",
        "        except gitops.GitUnavailableError:\n"
        "            raise _fail(ADAPTER_UNAVAILABLE, MSG_ADAPTER_UNAVAILABLE) from None",
        "        except gitops.GitUnavailableError:\n"
        "            raise _fail(frames.INTERNAL, MSG_GIT_TIMEOUT) from None",
        _target_test(
            "test_missing_git_executable_maps_to_adapter_unavailable", file="test_branches"
        ),
        "repo.branches 把 ADAPTER_UNAVAILABLE 翻成 INTERNAL",
    ),
    (
        "postel_studio/gitops/repo.py",
        'is_current=marker == "*"',
        "is_current=bool(fields[3])",
        _target_test("test_branch_current_flag_is_only_on_the_head_branch", file="test_branches"),
        "T33：当前判定用未 strip 的 marker 做真值判断（非当前分支的 marker 是空格）",
    ),
    (
        "postel_studio/gitops/repo.py",
        "                upstream=upstream or None,",
        "                upstream=upstream,",
        _target_test(
            "test_branch_upstream_is_short_name_and_none_when_missing", file="test_branches"
        ),
        "D1：没有 upstream 时不再把空串归一成 None",
    ),
    (
        "postel_studio/gitops/repo.py",
        'run_git(["for-each-ref", "refs/heads/", f"--format={BRANCH_FORMAT}"], root=root)',
        'run_git(["for-each-ref", f"--format={BRANCH_FORMAT}"], root=root)',
        _target_test("test_only_local_branches_are_listed_after_push", file="test_branches"),
        "D2：不限定 refs/heads/（remote-tracking 会混进分支列表）",
    ),
)


# S3.5 · U9 的**组合注入**（一次改两处；结构同 INJECTIONS，只是 patches 允许多项）。
#
# U9 要两处同时存在才复现（实测，见 06-pitfalls T32）：(a) id 依赖 origin（S3 的算法）**且**
# (b) `upsert` 按 path 命中时覆盖 `repo_id`。只注入任一处，验收 2/3 都是**绿的**——只有 (b)
# 时新算法算出的 id 本来就相同；只有 (a) 时规则 1 不覆盖 id。单独两半各写成"必须仍然绿"的
# 用例（`_ONLY_*`）。能红的只有 (a)+(b) = S3 的真实代码形态。
#
# 注意 (b) 的参数元组必须一起改：只改 SQL 会红在 `ProgrammingError` 上，那是"红错原因"
# （见 `_RUNTIME_BREAKAGES`）。
_UPSERT_COVERS_ID: tuple[tuple[str, str, str], ...] = (
    (
        "postel_studio/storage/db.py",
        '                    "UPDATE repos SET origin_url = ?, origin_hash = ?,'
        ' default_branch = ?,"',
        '                    "UPDATE repos SET repo_id = ?, origin_url = ?, origin_hash = ?,"\n'
        '                    " default_branch = ?,"',
    ),
    (
        "postel_studio/storage/db.py",
        '                    " added_at = ? WHERE path = ?",\n                    (\n'
        "                        repo.origin_url,",
        '                    " added_at = ? WHERE path = ?",\n                    (\n'
        "                        repo.repo_id,\n"
        "                        repo.origin_url,",
    ),
)


# U9(a)：调用方把 `repo_id()` 换回「有 origin 用 URL 哈希」。import + 调用点两段必须一起注入。
_OLD_ID_ALGORITHM: tuple[tuple[str, str, str], ...] = (
    (
        "postel_studio/api/repository.py",
        "from postel_studio.domain.models import Repo, origin_hash, repo_id",
        "from postel_studio.domain.models import Repo, origin_hash, repo_id, repo_id_for_origin",
    ),
    (
        "postel_studio/api/repository.py",
        "                repo_id=repo_id(str(root)),",
        "                repo_id=repo_id_for_origin(origin) if origin else repo_id(str(root)),",
    ),
)

COMPOSITE_INJECTIONS: tuple[tuple[tuple[tuple[str, str, str], ...], str, str], ...] = (
    (
        _OLD_ID_ALGORITHM + _UPSERT_COVERS_ID,
        _target_test("test_rebinding_after_origin_added_updates_the_row"),
        "U9 完整形态（旧算法 + upsert 覆盖 id）：验收 2 的用例必须红",
    ),
    (
        _OLD_ID_ALGORITHM + _UPSERT_COVERS_ID,
        _target_test("test_unbound_legacy_id_is_still_reachable_after_origin_change"),
        "U9 完整形态：验收 3 的用例必须红",
    ),
    (
        _OLD_ID_ALGORITHM + _UPSERT_COVERS_ID,
        _target_test("test_binding_an_existing_v1_row_keeps_its_legacy_id"),
        "U9 完整形态：v1 老行的 id 在下一次 bind 被覆盖（D3 的留痕被抹掉）",
    ),
)


@pytest.fixture(scope="module")
def patched_source(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """一份可写的 `src` 副本（每次注入都在它上面改，原 `source_root` 只读）。"""
    root = tmp_path_factory.mktemp("reverse-src")
    shutil.copytree(SOURCE_ROOT, root / "src")
    return root / "src"


def _snapshot(source: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(source)): path.read_bytes()
        for path in sorted(source.rglob("*.py"))
        if "__pycache__" not in path.parts
    }


def _run_target_test(source: Path, node: str) -> subprocess.CompletedProcess[str]:
    """用**副本**源码跑一条真实 pytest。`PYTHONPATH` 前置副本，项目根保留在后。"""
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([str(source), str(REPO_ROOT)])
    env["PYTHONDONTWRITEBYTECODE"] = "1"  # 别往副本里写 __pycache__（会污染快照对比）
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "--no-cov",
            "-o",
            "addopts=",
            "-p",
            "no:cacheprovider",
            node,
        ],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=RUN_TIMEOUT_SECONDS,
        check=False,
    )


def test_sanity_the_copy_is_really_what_runs(patched_source: Path) -> None:
    """**前提**：副本源码真的会被 import 到（否则下面所有"变红"都可能来自别的原因）。

    验证办法：在副本里把一个模块常量改成一个哨兵值，读回来必须是哨兵。
    这条要是红了，说明 `PYTHONPATH` 没起作用——那么 INJECTIONS 里的"变红"全部不可信。
    """
    module = patched_source / "postel_studio" / "domain" / "models.py"
    original = module.read_text(encoding="utf-8")
    try:
        module.write_text(
            original.replace("REPO_ID_LENGTH: Final = 12", "REPO_ID_LENGTH: Final = 77"),
            encoding="utf-8",
        )
        probe = subprocess.run(
            [
                sys.executable,
                "-c",
                "from postel_studio.domain.models import REPO_ID_LENGTH;"
                "print(REPO_ID_LENGTH);"
                "import postel_studio;print(postel_studio.__file__)",
            ],
            cwd=REPO_ROOT,
            env={
                **os.environ,
                "PYTHONPATH": os.pathsep.join([str(patched_source), str(REPO_ROOT)]),
            },
            capture_output=True,
            text=True,
            check=False,
        )
        assert probe.returncode == 0, probe.stderr
        first, imported = probe.stdout.split()
        assert first == "77", f"副本没生效：读到的是 {first}"
        assert imported.startswith(str(patched_source)), f"import 到的是 {imported}"
    finally:
        module.write_text(original, encoding="utf-8")


def _apply_patches(source: Path, patches: tuple[tuple[str, str, str], ...]) -> dict[str, str]:
    """把一组补丁打到**副本**上，返回「文件 → 原始内容」（供 `finally` 复原）。

    两条纪律（S3.5 加的第一条）：

    1. **锚点必须恰好命中一次**：`str.count(old)` 不是 1 就当场失败。S3.5 改 `db.py` 时它当场
       抓到过一条旧锚点静默失配（`UPDATE` 里不再有 `repo_id`，那条注入会退化成空操作而永远绿）。
    2. 同一文件的多段补丁**按顺序叠加**（后一段在前一段的结果上找锚点）。
    """
    pristine: dict[str, str] = {}
    for file, old, new in patches:
        path = source / file
        if file not in pristine:
            pristine[file] = path.read_text(encoding="utf-8")
        text = path.read_text(encoding="utf-8")
        hits = text.count(old)
        assert hits == 1, f"{file} 里锚点命中 {hits} 次（应恰好 1 次）：{old[:60]!r}"
        path.write_text(text.replace(old, new, 1), encoding="utf-8")
    return pristine


def _restore_patches(source: Path, pristine: dict[str, str]) -> None:
    """复原副本（**必须**在 `finally` 里调：下一个参数化用例还要用同一份副本）。"""
    for file, text in pristine.items():
        (source / file).write_text(text, encoding="utf-8")


# 注入后目标测试"红"的**成因**不能是这些：它们说明"注入把代码弄坏了"，与"这条约束有没有被
# 守住"无关。实测踩过一次：只改 UPDATE 的 SQL 而忘了补参数元组，测试红在
# `sqlite3.ProgrammingError: Incorrect number of bindings supplied` 上——那次"红"什么也没证明。
_RUNTIME_BREAKAGES: tuple[str, ...] = (
    "ProgrammingError",
    "OperationalError",
    "NameError",
    "AttributeError",
    "ImportError",
    "ModuleNotFoundError",
    "TypeError",
)


def _assert_red_for_the_right_reason(
    result: subprocess.CompletedProcess[str], test_node: str, description: str
) -> None:
    """目标测试必须**红**，且红在断言上（不是注入把代码改坏了）。"""
    assert result.returncode != 0, (
        f"注入「{description}」之后 {test_node} **仍然是绿的**——这条测试守不住这个约束。\n"
        f"stdout:\n{result.stdout[-2000:]}\nstderr:\n{result.stderr[-1000:]}"
    )
    output = result.stdout + result.stderr
    for breakage in _RUNTIME_BREAKAGES:
        assert breakage not in output, (
            f"注入「{description}」之后目标测试红在 {breakage} 上——注入后的代码没跑起来，"
            f"这次变红**不说明**那条约束被守住。\nstdout:\n{result.stdout[-1500:]}"
        )


@pytest.mark.parametrize(
    ("file", "old", "new", "test_node", "description"),
    INJECTIONS,
    ids=[item[4] for item in INJECTIONS],
)
def test_injected_defect_turns_the_target_test_red(
    patched_source: Path,
    file: str,
    old: str,
    new: str,
    test_node: str,
    description: str,
) -> None:
    """注入缺陷 → 目标测试**必须**变红；同时断言副本真的被改了、原文件没被动。"""
    pristine = _apply_patches(patched_source, ((file, old, new),))
    try:
        result = _run_target_test(patched_source, test_node)
        _assert_red_for_the_right_reason(result, test_node, description)
    finally:
        _restore_patches(patched_source, pristine)


@pytest.mark.parametrize(
    ("patches", "test_node", "description"),
    COMPOSITE_INJECTIONS,
    ids=[item[2] for item in COMPOSITE_INJECTIONS],
)
def test_composite_injected_defect_turns_the_target_test_red(
    patched_source: Path,
    patches: tuple[tuple[str, str, str], ...],
    test_node: str,
    description: str,
) -> None:
    """一次改两处（U9 必须如此，见 `COMPOSITE_INJECTIONS` 的注释）→ 目标测试**必须**变红。"""
    pristine = _apply_patches(patched_source, patches)
    try:
        result = _run_target_test(patched_source, test_node)
        _assert_red_for_the_right_reason(result, test_node, description)
    finally:
        _restore_patches(patched_source, pristine)


@pytest.mark.parametrize(
    ("patches", "label"),
    [
        (_OLD_ID_ALGORITHM, "只有 (a)：算法退回「有 origin 用 URL 哈希」"),
        (_UPSERT_COVERS_ID, "只有 (b)：upsert 按 path 命中时覆盖 repo_id"),
    ],
    ids=["only-old-id-algorithm", "only-upsert-covers-id"],
)
def test_each_half_of_u9_alone_keeps_stability_green(
    patched_source: Path, patches: tuple[tuple[str, str, str], ...], label: str
) -> None:
    """单独注入 U9 的任一半时，验收 2/3 的用例**必须仍然绿**——D1 由两道锁共同保证。

    | 注入状态 | 验收 2/3 | 原因 |
    |---|---|---|
    | (a)+(b)（= S3 真实代码） | **红** | U9 复现，见 `COMPOSITE_INJECTIONS` |
    | 只有 (a) | 绿 | 规则 1 不写 `repo_id` |
    | 只有 (b) | 绿 | 同一路径算出的 id 本来就相同 |

    红了说明"另一半"不再独立兜住 D1，先看 `COMPOSITE_INJECTIONS` 里的条目。
    """
    pristine = _apply_patches(patched_source, patches)
    try:
        for test_node in (
            _target_test("test_rebinding_after_origin_added_updates_the_row"),
            _target_test("test_unbound_legacy_id_is_still_reachable_after_origin_change"),
        ):
            result = _run_target_test(patched_source, test_node)
            assert result.returncode == 0, (
                f"{label} 单独存在就把 {test_node} 弄红了——D1 的两道锁不再各自独立。\n"
                f"stdout:\n{result.stdout[-2000:]}\nstderr:\n{result.stderr[-1000:]}"
            )
    finally:
        _restore_patches(patched_source, pristine)


def test_repository_source_is_never_modified(patched_source: Path) -> None:
    """所有反向验证都只改副本；跑完之后**仓库里的 `src/` 必须逐字节没变**。

    这条是这套机制的安全锁：注入逻辑一旦写错（例如忘了用副本），它会当场抓住。
    """
    before = _snapshot(SOURCE_ROOT)
    assert _snapshot(patched_source) == before, "副本与仓库源码不一致（复原有问题）"
    assert _snapshot(patched_source)  # 非空，别让一个空 dict 蒙过去
