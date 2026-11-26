"""git 只读探测：**起子进程**的那一层（S3 引入，D4/D5；S4 加分支列表；S5 加 diff）。

本模块回答五类问题：这是不是仓库 / 根在哪（`repo_root` / `bare_root` / `is_worktree_root`
/ `is_repo`）、origin 是什么（`origin_url`）、默认分支是谁（`default_branch`）、
本地分支有哪些（`list_branches`，S4）、两个 commit 之间的差异是什么（`diff_stat` /
`diff_patch`，S5）。
**不做任何写操作**。`list_branches` 是"枚举全部本地分支"的整体询问（S4 D2：只列
`refs/heads/`），不接受任何来自网络的引用名；S5 的 `resolve_commit` 是**唯一**把用户给的
ref 字符串送进 git 的入口（D2），它先解析成 object id，之后的 diff 命令只用 id。

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
import re
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
# S5 D4：把**会改变 diff 输出**的用户配置全部用 flag 钉死。少一个，同一个仓库对同一次
# diff 就会给出不同答案（每条都在 06-pitfalls T34 里实测过）：
# - `--no-color`     `color.ui=always` 时 patch 里混入 `\x1b[1m` 等 ANSI；
# - `--no-ext-diff`  `diff.external` 指向外部命令时 patch 直接失败（git exit 128）；
# - `--no-textconv`  `.gitattributes` + `diff.<driver>.textconv` 会把内容换成外部命令的输出；
# - `--no-renames`   `diff.renames` 缺省 **true**：重命名算 1 个文件（`0 0 old => new`），
#                    用户设 false 就变 2 个文件；开着还依赖 `diff.renameLimit`，大 diff 会
#                    静默退化。MVP 裁决：重命名 = 一删一增（D4），不引入第三个配置依赖；
# - `--unified=3`    `diff.context` 会改 hunk 大小（缺省 3，用户可设成 1）。
DIFF_FLAGS: Final = (
    "--no-color",
    "--no-ext-diff",
    "--no-textconv",
    "--no-renames",
    "--unified=3",
)
# `--numstat` 的同一组 flag，**去掉** `--unified`：git 2.53 实测，`--numstat` 与
# `--unified` 同时给会输出「numstat 记录 + 空行 + 完整 patch」——`--unified` 隐含
# `--patch`，把 `--numstat` 隐含的 `--no-patch` 覆盖回来。D5 的命令本来就没有它。
STAT_FLAGS: Final = DIFF_FLAGS[:-1]
# `core.quotePath=false` 必须放在 `diff` 子命令**之前**——它是 `-c` 配置项、不是 diff 的
# flag（`git -C <root> -c core.quotePath=false diff …` 合法）。关掉后非 ASCII 路径是字面
# 中文，而不是 `"\344\270\255\346\226\207.txt"`（T34 实测两种输出）。
_DIFF_CONFIG: Final = ("-c", "core.quotePath=false")
# object id 的形状：SHA-1 40 位 / SHA-256 64 位，小写十六进制。**自己校验**，不信 rev-parse
# 的回显（无 `--verify` 时它会把有歧义的参数原样打到 stdout，见 `resolve_commit`）。
_OBJECT_ID: Final = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})")
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


class GitOutputUndecodableError(RuntimeError):
    """git 的输出不是合法 UTF-8（S5 D3.2，结掉 U15）。

    只在 `decode_errors="strict"` 时出现；边界层一律翻成 `NOT_SUPPORTED`（07 §1.3，不新增码）。
    **不能**用 `errors="replace"` / `surrogateescape` 糊过去：mangled 的 ref 名回传 git 会
    变成 `BAD_REF`，甚至指向别的 ref——比报错更糟（U15 的裁决）。内容类输出（patch / 文件名）
    用 `errors="replace"`，因为它只进 DOM、永不回传 git（D3.3）。
    """


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


@dataclass(frozen=True)
class DiffStat:
    """一次 diff 的**事实**统计（S5 D5）：文件数 / 加行 / 减行。

    纯 snake_case、完整值；截断与 `camelCase` 字段名是 `api/repository.py` 的边界格式化
    （D8 的「格式化在边界」）。`files_changed` 是 numstat **记录数**（含二进制、gitlink、
    纯 mode 变更）；`insertions` / `deletions` 是各记录加/减行求和（`-` 记 0）。
    """

    files_changed: int
    insertions: int
    deletions: int


def _environment() -> dict[str, str]:
    """子进程环境：最小化 + 禁交互 + 不读系统级配置。"""
    env = {k: v for k, v in os.environ.items() if k not in _DROPPED_ENV}
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    # 只读探测不需要可选锁：并发 `git gc` 时缺它会让命令直接失败（与"仓库状态"无关的噪声）
    env["GIT_OPTIONAL_LOCKS"] = "0"
    return env


def git_env() -> dict[str, str]:
    """`_environment()` 的公开别名：**写操作模块**（`gitops/worktrees.py`，S7）也要用它。

    写操作有自己的超时与错误语义（30 秒、非零退出算"拒绝"），所以不复用 `run_git`；但环境
    必须共用一份——`GIT_TERMINAL_PROMPT=0` 与 `GIT_CONFIG_NOSYSTEM=1` 是 05 §3.3 的硬约束，
    复制第二份迟早会漂。`GIT_OPTIONAL_LOCKS=0` 也跟着走：worktree 命令自己会取需要的锁。
    """
    return _environment()


def git_executable() -> str:
    """`git` 的路径；不存在就抛 `GitUnavailableError`。

    用**绝对路径**而不是裸 `"git"`：这样"git 不存在"能被可靠地测出来（把 `PATH` 清空即可），
    而不是被某个 `PATH` 里的影子命令蒙混过去。
    """
    found = shutil.which("git")
    if found is None:
        raise GitUnavailableError("找不到 git 命令")
    return found


def run_git(
    args: Sequence[str], *, root: Path, decode_errors: str = "strict"
) -> subprocess.CompletedProcess[str]:
    """在 `root` 下跑一条**只读** git 命令（字面数组，无 shell，有超时）。

    `root` 是显式参数（D4）：05 §3.2 要求的"允许根"在这里落地成签名的一部分，
    后续切片（S4 的分支列表）只要复用本函数，就天然只能操作已绑定的仓库。
    失败一律抛**不含 `root` 文本**的异常——异常会经 `api/repository.py` 变成帧里的
    `message`，而 05 §4.2 要求错误不回显输入值。

    `decode_errors` 原样传给 `subprocess.run(errors=…)`（S5 D3.2 的裁决），两种取值：

    - `"strict"`（缺省）：**身份类**输出（ref 名 / 分支列表 / 解析结果）。解码失败抛
      `GitOutputUndecodableError` → 帧层 `NOT_SUPPORTED`，**绝不** mangled 回传；
    - `"replace"`：**内容类**输出（patch 正文 / 文件名 / numstat 文件名）。解码失败出现
      `�` 但**不失败**——只进 DOM、永不回传 git。

    旧版本（S3/S4）是严格解码但**不捕获** `UnicodeDecodeError`：它不在 `GIT_ERRORS` 里，
    一路逃到网关变成 `INTERNAL`（U15 的问题）。本刀把它变成显式异常，翻成稳定码。
    """
    command = [git_executable(), "-C", str(root), *args]
    try:
        return subprocess.run(
            command,
            shell=False,
            capture_output=True,
            text=True,
            errors=decode_errors,
            timeout=GIT_TIMEOUT_SECONDS,
            env=_environment(),
            stdin=subprocess.DEVNULL,
            check=False,
        )
    except FileNotFoundError:  # 探测与执行之间 git 被卸掉（或 which 给了坏路径）
        raise GitUnavailableError("找不到 git 命令") from None
    except subprocess.TimeoutExpired:
        raise GitTimeoutError(f"git 超时（>{GIT_TIMEOUT_SECONDS}s）") from None
    except UnicodeDecodeError:  # strict 模式下读到非 UTF-8 字节（U15）
        raise GitOutputUndecodableError("git 输出不是合法 UTF-8") from None
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


def is_usable_repo(path: Path) -> bool:
    """绑定过的路径**现在还能不能当仓库用**（目录在，且自己有 `.git` 或本身就是裸仓库）。

    这是 S5 D7 #4 与 S7 的**同一条**判据，S7 起搬到这里（原先叫
    `api/repository.repo_path_is_gone`）：它只问 git 语义，不认识 `Repo` 实体。

    三个方向的理由（T25 是这条的全部来龙去脉）：

    - **不用 `git rev-parse` 判定**：它会向父目录 discover，父目录恰好是仓库时会误判"还活着"；
      S7 的 `worktree add` 若在这个判据上出错，会把工作区注册到**别人**的仓库里。
    - `.git` 被删时 `bare_root` 会在非仓库目录里跑一次 git（非零 / `false`）→ 判死。
    - 裸仓库本来就没有 `.git`，所以必须留 `bare_root` 这一条，否则会误杀已绑定的裸仓库。
    """
    if not path.is_dir():
        return False
    if is_worktree_root(path):
        return True
    return bare_root(path) is not None


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


def resolve_commit(root: Path, ref: str) -> str | None:
    """把一个 ref（分支名 / 提交号 / ...）解析成 **commit** 的 object id（S5 D2）。

    | 返回 | 含义 |
    |---|---|
    | `"<40 或 64 位小写 hex>"` | 解析成功；形状由**本函数**校验（不信 rev-parse 的回显） |
    | `None` | 问不出来（不存在 / 不是 commit：tree id 会被 `^{commit}` 拒掉） |
    | 抛 `GIT_ERRORS` 里那三个异常 | 起不来 / 卡住 / 非 UTF-8 |

    三条命令形状的理由（都是实测，不是推断）：

    - `--verify` **只能收一个 rev**（多参数回 `fatal: 需要一个单独的版本` + exit 128），
      所以 base/head 各调一次，不要合并成一条；
    - `--end-of-options` 让 `-` 开头的字符串不被当成选项（`--no-index^{commit}` → exit 128）；
    - `^{commit}` 强制 commit 类型：拿 tree id 来问会回
      `expected commit type, but the object dereferences to tree type` + exit 128。

    **形状必须自己校验**：没有 `--verify` 时 rev-parse 会把有歧义的参数原样回显到 stdout
    （实测 `git rev-parse --end-of-options main^{commit} nosuch^{commit}` 输出
    `--end-of-options`、`<sha>`、`nosuch^{commit}` 三行且 exit 128）。"回显"不等于"解析结果"。

    这是**唯一**把用户给的 ref 字符串送进 git 的地方（D2）：调用方拿到 id 后，stat / patch
    只用 `<a>...<b>` 两个 id，于是注入面收敛到这一条命令（且它带 `--end-of-options`），
    并且 base/head 在解析后不再"移动"——统计与 patch 描述同一对 commit。
    """
    sha = _stdout(
        run_git(["rev-parse", "--verify", "--end-of-options", f"{ref}^{{commit}}"], root=root)
    )
    return sha if sha is not None and _OBJECT_ID.fullmatch(sha) else None


def _git_output(result: subprocess.CompletedProcess[str]) -> str | None:
    """成功则返回**原样** stdout（不 strip），非零退出 → `None`。

    diff 的输出是**内容**：首尾空白（补丁的行首空格）有意义，不能用 `_stdout` 的整段 strip
    （`_stdout` 是给"取一个名字/一个 sha"这类单值询问用的）。
    """
    return result.stdout if result.returncode == 0 else None


def diff_stat(root: Path, base: str, head: str) -> DiffStat | None:
    """`base...head` 的统计（S5 D5）：**三点**语义——共同祖先 → head（D1）。

    | 返回 | 含义 |
    |---|---|
    | `DiffStat`（可能全 0） | git 答了；**全 0 = 同一 commit，是成功**（D6） |
    | `None` | 非零退出（无共同祖先等）——"问不出来" |
    | 抛 `GitUnavailableError` / `GitTimeoutError` | 起不来 / 卡住（由调用方翻码） |

    命令固定为 `git -c core.quotePath=false diff --numstat -z --no-renames <a>...<b>`
    （DIFF_FLAGS 见上）。解析规则：

    - 按 `\\x00` 切记录，**每条只取前两段**（`add\\tdel\\tpath`）——`-z` 下路径原样输出，
      可以含 tab 甚至换行（实测 `tab\\tname.txt` 的记录里那个 tab 就是路径的一部分）；
      按行切会把这类路径切坏，这是必须 `-z` 而不是按行解析的原因（T36）；
    - 字段是 `-`（二进制）时按 0 计；纯 mode 变更 git 给 `0 0`，gitlink 也给一行；
    - 记录数就是 `files_changed`（含二进制 / gitlink / mode-only）。

    `decode_errors="replace"`：文件名只出现在被丢弃的第三段，但解析前先解码——仓库里存在
    非 UTF-8 文件名是常态，为此让整条 diff 失败是错的（D3.3；统计是**内容**不是身份）。
    """
    result = run_git(
        [*_DIFF_CONFIG, "diff", "--numstat", "-z", *STAT_FLAGS, f"{base}...{head}"],
        root=root,
        decode_errors="replace",
    )
    if result.returncode != 0:  # 无共同祖先：stdout 空 + exit 128（实测）→ 问不出来
        return None
    files = insertions = deletions = 0
    for record in result.stdout.split("\x00"):
        if not record:
            continue
        fields = record.split("\t", 2)
        if len(fields) != 3:  # format 变了：**不猜**（同 list_branches 的 len(fields) != 4）
            return None
        files += 1
        insertions += 0 if fields[0] == "-" else int(fields[0])
        deletions += 0 if fields[1] == "-" else int(fields[1])
    return DiffStat(files_changed=files, insertions=insertions, deletions=deletions)


def diff_patch(root: Path, base: str, head: str) -> str | None:
    """`base...head` 的完整 patch 文本（S5 D1/D4）：**三点**语义，flag 全部钉死。

    | 返回 | 含义 |
    |---|---|
    | `str`（可能是 `""`） | git 答了；**空串 = 无差异，是成功**（D6） |
    | `None` | 非零退出（无共同祖先等）——"问不出来" |
    | 抛 `GitUnavailableError` / `GitTimeoutError` | 起不来 / 卡住 |

    `decode_errors="replace"`（D3.3）：patch 正文与文件名是**内容**，只经 `textContent`
    进 DOM、**永不回传 git**；仓库里有非 UTF-8 的文件内容（latin-1 源码、历史遗留）是常态，
    为它回 500 是错的。实测：strict 解码在 `\xff` 处抛 `UnicodeDecodeError`，
    replace 得到 `-�� latin1 bad` / `+�� latin1 bad2`，可正常显示。

    返回**原样** stdout（不 strip）：空差异就是 `""`，截断判断（D6）按字符数在边界层做，
    L0 不替调用方决定上限。
    """
    result = run_git(
        [*_DIFF_CONFIG, "diff", *DIFF_FLAGS, f"{base}...{head}"],
        root=root,
        decode_errors="replace",
    )
    return _git_output(result)


GIT_ERRORS: Final[tuple[type[Exception], ...]] = (
    GitUnavailableError,
    GitTimeoutError,
    GitOutputUndecodableError,
)

__all__ = [
    "BRANCH_FORMAT",
    "DIFF_FLAGS",
    "GIT_ERRORS",
    "GIT_TIMEOUT_SECONDS",
    "STAT_FLAGS",
    "BranchInfo",
    "DiffStat",
    "GitOutputUndecodableError",
    "GitTimeoutError",
    "GitUnavailableError",
    "bare_root",
    "default_branch",
    "diff_patch",
    "diff_stat",
    "git_env",
    "git_executable",
    "is_repo",
    "is_usable_repo",
    "is_worktree_root",
    "list_branches",
    "origin_url",
    "repo_root",
    "resolve_commit",
    "run_git",
]
