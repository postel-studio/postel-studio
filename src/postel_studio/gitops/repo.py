"""git 只读探测：**起子进程**的那一层（S3 引入，D4/D5；S4 加分支列表）。

本模块只回答四类问题：这是不是仓库 / 根在哪（`repo_root` / `bare_root` / `is_worktree_root`
/ `is_repo`）、origin 是什么（`origin_url`）、默认分支是谁（`default_branch`）、
本地分支有哪些（`list_branches`，S4）。
**不做任何写操作**；`list_branches` 是"枚举全部本地分支"的整体询问（S4 D2：只列
`refs/heads/`），**仍然不接受**任何来自网络的引用名——`repo.branches` 只收 `repoId`，
没有 ref 参数（切分支/看 diff 是 S4 之后的事，见 07 §2.2 的 `repo.diff`）。

⚠️ 这些函数**都不要**被直接用作"用户输入的这个目录能不能绑"的判据：git 的 `-C` 与
`--show-toplevel` 会向父目录 discover。总判定在 `api/repository.py:_bindable_root()`
（06-pitfalls T25 是这条的全部来龙去脉）。

三条硬约束（05-security §3.3 / S3 D5），改这个文件时当作不许动的底线：

1. **字面数组 + `shell=False`**：禁止 `shell=True`、`os.system`、任何字符串拼接出来的命令。
   路径作为**独立参数**交给 `git -C <path>`。
2. **环境变量 `GIT_TERMINAL_PROMPT=0`**：git 缺省会向终端要密码；凭证助手在无 tty 时可能
   挂住整个内核。`GIT_CONFIG_NOSYSTEM=1` 再加一道：不让用户机器的系统级 git 配置影响答案。
3. **必须有 timeout**（`GIT_TIMEOUT_SECONDS`）：坏仓库、锁文件、网络文件系统都会让 git 卡住，
   而这里跑在 WebSocket 处理链路上，卡住就是整个网关无响应。
"""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

# D5：5 秒。本地只读命令正常在毫秒级，5 秒只可能是"卡住了"。
GIT_TIMEOUT_SECONDS: Final = 5.0
# S4 D3：一次 `for-each-ref` 取全字段，字段之间用 NUL（`%00`）分隔。
# **禁止**解析 `git branch` 的人类可读输出：颜色 / 语言 / 终端宽度都会让格式变样
# （S4 禁止事项；D7 第 7 行是这条的回归测试）。
# 末尾的 `%(HEAD)` 是当前分支标记：当前分支给 `*`，**非**当前分支给**一个空格**（T33）。
BRANCH_FORMAT: Final = "%(refname:lstrip=2)%00%(objectname)%00%(upstream:short)%00%(HEAD)"
# 不继承调用方环境里可能存在的 GIT_DIR / GIT_WORK_TREE：它们会把 `git -C <path>` 指到别处
_DROPPED_ENV: Final = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_NAMESPACE",
)


class GitUnavailableError(RuntimeError):
    """`git` 命令不存在或起不来（07 §1.3 `ADAPTER_UNAVAILABLE` 的来源）。"""


class GitTimeoutError(RuntimeError):
    """`git` 超时（S3 D6 裁决：先按 `INTERNAL` 处理，S19 的 doctor 再细化）。"""


@dataclass(frozen=True)
class BranchInfo:
    """一条**本地**分支（S4 D1 / D4）。

    `sha` 存 **完整的 40 位**小写十六进制；截成 8 位是**边界格式化**，发生在
    `api/repository.py`（07 §4.2「字段名/格式在边界转换」同一条纪律）。两层各自可测：
    本层断言"git 给的是完整 sha"，帧层断言"载荷里是 8 位"。

    `upstream` 没有 upstream 时是 `None`（不是 `""`，D1）；`is_current` 只在 HEAD
    直接指向该分支时为 `True`——**游离 HEAD 时全部 `False`**（D1）。
    """

    name: str
    sha: str
    upstream: str | None
    is_current: bool


def _environment() -> dict[str, str]:
    """子进程环境：最小化 + 禁交互 + 不读系统级配置。"""
    env = {k: v for k, v in os.environ.items() if k not in _DROPPED_ENV}
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    # 只读探测不需要可选锁：并发 `git gc` 时缺它会让命令直接失败（与"仓库状态"无关的噪声）
    env["GIT_OPTIONAL_LOCKS"] = "0"
    return env


def git_executable() -> str:
    """`git` 的路径；不存在就抛 `GitUnavailableError`。

    用**绝对路径**而不是裸 `"git"`：这样"git 不存在"能被可靠地测出来（把 `PATH` 清空即可），
    而不是被某个 `PATH` 里的影子命令蒙混过去。
    """
    found = shutil.which("git")
    if found is None:
        raise GitUnavailableError("找不到 git 命令")
    return found


def run_git(args: Sequence[str], *, root: Path) -> subprocess.CompletedProcess[str]:
    """在 `root` 下跑一条**只读** git 命令（字面数组，无 shell，有超时）。

    `root` 是显式参数（D4）：05 §3.2 要求的"允许根"在这里落地成签名的一部分，
    后续切片（S4 的分支列表）只要复用本函数，就天然只能操作已绑定的仓库。
    失败一律抛**不含 `root` 文本**的异常——异常会经 `api/repository.py` 变成帧里的
    `message`，而 05 §4.2 要求错误不回显输入值。

    ⚠️ `text=True` 是**严格 UTF-8 解码**：仓库里有非 UTF-8 的 refname 时，stdout 解码会抛
    `UnicodeDecodeError`（它**不在** `GIT_ERRORS` 里），一路逃到网关变成 `INTERNAL`。
    S3/S4 都受影响，本刀不修——策略与建议见 06-pitfalls **U15**。
    """
    command = [git_executable(), "-C", str(root), *args]
    try:
        return subprocess.run(
            command,
            shell=False,
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_SECONDS,
            env=_environment(),
            stdin=subprocess.DEVNULL,
            check=False,
        )
    except FileNotFoundError:  # 探测与执行之间 git 被卸掉（或 which 给了坏路径）
        raise GitUnavailableError("找不到 git 命令") from None
    except subprocess.TimeoutExpired:
        raise GitTimeoutError(f"git 超时（>{GIT_TIMEOUT_SECONDS}s）") from None
    except OSError as exc:  # 权限、exec 失败等：语义仍是"起不来"
        raise GitUnavailableError("git 命令无法启动") from exc


def _stdout(result: subprocess.CompletedProcess[str]) -> str | None:
    """成功则返回去空白的 stdout，否则 `None`（"问不出来"统一表示成 None）。"""
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def repo_root(path: Path) -> Path | None:
    """`path` 所在**工作区**的根；不是工作区（含裸仓库、普通目录）返回 `None`。

    D2「以真实根为准」的一半实现：`/home/u/proj/src` → `/home/u/proj`。
    **不做任何启发式校正**：放不放在工作区里由调用方（`api/repository.py:_bindable_root`）
    用**允许根断言**决定，见 06-pitfalls T25。本函数只是"问 git"。

    裸仓库不给 `--show-toplevel`（它没有工作区），用 `bare_root`。
    """
    if not path.is_dir():
        return None
    top = _stdout(run_git(["rev-parse", "--show-toplevel"], root=path))
    return Path(top) if top else None


def bare_root(path: Path) -> Path | None:
    """`path` 自己就是裸仓库根时返回它，否则 `None`（`git init --bare`，D2 接受）。"""
    if not path.is_dir():
        return None
    bare = _stdout(run_git(["rev-parse", "--is-bare-repository"], root=path))
    return path if bare == "true" else None


def is_worktree_root(path: Path) -> bool:
    """`path` 是工作区根：`path/.git` 存在（目录，或 worktree 的 `.git` 文件）。

    **不碰 git**：这是一次文件系统检查，目的是"目标自己就是根"这个无条件成立的判据。
    """
    return (path / ".git").exists()


def is_repo(path: Path) -> bool:
    """`path` 是否在一个 git 仓库里（含裸仓库、含"没有任何提交"的空仓库，D2）。

    ⚠️ 沿用 git 的 discover 语义（会向父目录找），因此**不能**单独用来判定"用户输入的目录
    是不是仓库"——判定入口是 `api/repository.py:_bindable_root()`。保留它是给"这里有没有
    git 语义"这类只读询问用的。
    """
    return repo_root(path) is not None or bare_root(path) is not None


def origin_url(root: Path) -> str | None:
    """`origin` 的 URL；**没有 origin 就返回 `None`**（不拿别的 remote 顶替）。

    只在**已确认的仓库根**上调用（不要拿用户的原始输入来问）：`git config --get` 的 -C 取值
    同样会向父目录 discover，问错目录会得到别人的 origin。

    用 `git config --get remote.origin.url` 而不是 `git remote get-url origin`：后者会把
    `insteadOf` 重写后的地址吐出来，前者是配置里的**原始值**（06 §2.1 要展示的就是原始值）。
    两者在无 origin 时退出码都是 1，都按"没有"处理。
    """
    value = _stdout(run_git(["config", "--get", "remote.origin.url"], root=root))
    return value or None


def default_branch(root: Path) -> str | None:
    """默认分支：本地 `HEAD` 指向的分支名，探测不到就是 `None`（D2）。

    三种情形的答案（都实测过，见 codemap 03-modules/gitops）：

    | 仓库 | 答案 |
    |---|---|
    | 正常仓库，HEAD 指向 `main` | `"main"` |
    | 刚 `git init`、**没有任何提交** | `"main"`（git 已写 symbolic-ref，D2 明确不要求有提交） |
    | 裸仓库 `git init --bare` | `"main"`（git init 写了 symbolic-ref） |
    | HEAD 游离（检出了某个提交） | `None`（没有"默认分支"可展示，不能瞎猜 `main`） |

    用 `symbolic-ref`（读 HEAD 的符号引用）而不是 `branch --show-current`：后者依赖"分支列表"
    的概念，而本刀明确不做分支列表（S4）。
    """
    value = _stdout(run_git(["symbolic-ref", "--quiet", "--short", "HEAD"], root=root))
    return value or None


def list_branches(root: Path) -> list[BranchInfo] | None:
    """`root` 的**本地**分支，按 refname 字典序（S4 D1/D2/D3）。

    | 返回 | 含义 |
    |---|---|
    | `list[BranchInfo]`（可能为空） | git 答了；**空列表 = 0 个分支的空仓库，是成功** |
    | `None` | git 说"这不是仓库 / 路径没了"（非零退出码） |
    | 抛 `GitUnavailableError` / `GitTimeoutError` | 起不来 / 卡住（同 S3 D5，由调用方翻码） |

    三态**不许合并**（S4 D4）：空列表与 `None` 在帧层是两个不同的码（`data:[]` vs
    `REPO_NOT_BOUND`），合并掉就会把"空仓库"报成"仓库没了"。

    只列 `refs/heads/`：不带它的话 `refs/remotes/origin/main` 也会出现在结果里，
    "共几个分支"与"当前分支"会同时失真（D2 的实测）。

    ⚠️ **T33：`%(HEAD)` 的空格 + `_stdout()` 的整段 `.strip()`**（06-pitfalls T33）：

    1. `%(HEAD)` 对**非当前**分支输出的不是空串，而是**一个空格**——它是真值；
       当前分支输出 `"*"`，也是真值。所以**不能用真值判断**（`if marker` / `bool(marker)`）
       来判"当前"：那样**除最后一条外全部判反**。
    2. `_stdout()` 对**整段** stdout `.strip()`，只会把**最后一条记录**的 marker 从 `" "`
       变成 `""`（值变了）——`%00` 分隔符是 NUL、**不是空白**，行尾的 NUL 不会被 strip，
       所以 split 后**字段数恒为 4**（末段是空串）。**不要**指望"字段会消失"：实测
       单分支 / 多分支 / 游离 HEAD / 最后一条有 upstream 等布局，段数全是 4。

    因此：字段逐个 `.strip()`，当前判定写 `marker == "*"`。**测试必须断言每一条分支**的
    `is_current`：只测单分支仓库、或只断言最后一条的用例，在"真值判断"的 bug 下会**碰巧
    通过**——最后一条的 marker 恰好被整段 strip 成了空串。

    `len(fields) != 4` 只可能意味着 `--format` 被改写 / git 换了实现：此时**不猜**（把缺字段
    猜成"非当前"等于把坏数据当正常结果），返回 `None`——"问不出来"是 D4 已经定义好的诚实
    答案（帧层翻成 `REPO_NOT_BOUND`）。
    """
    result = run_git(["for-each-ref", "refs/heads/", f"--format={BRANCH_FORMAT}"], root=root)
    output = _stdout(result)
    if output is None:  # 非零退出 = 这不是仓库（或路径已经不存在）
        return None
    branches: list[BranchInfo] = []
    for line in output.splitlines():
        fields = line.split("\x00")
        if len(fields) != 4:
            # 段数不对只可能是 format 被改写 / git 换了实现：**不猜**（猜成"非当前"会把
            # 坏数据当正常结果），返回 None —— 帧层会翻成 REPO_NOT_BOUND（D4 的诚实答案）。
            return None
        name, sha, upstream, marker = (field.strip() for field in fields)
        branches.append(
            BranchInfo(
                name=name,
                sha=sha,
                upstream=upstream or None,  # 无 upstream 时 git 给空串；D1 要求 None
                is_current=marker == "*",  # T33：不能写 `if not marker`
            )
        )
    return branches


GIT_ERRORS: Final[tuple[type[Exception], ...]] = (GitUnavailableError, GitTimeoutError)

__all__ = [
    "BRANCH_FORMAT",
    "GIT_ERRORS",
    "GIT_TIMEOUT_SECONDS",
    "BranchInfo",
    "GitTimeoutError",
    "GitUnavailableError",
    "bare_root",
    "default_branch",
    "git_executable",
    "is_repo",
    "is_worktree_root",
    "list_branches",
    "origin_url",
    "repo_root",
    "run_git",
]
