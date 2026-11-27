"""worktree 的**写**操作（S7 三条 + S11 三条）：内核第一次往用户的仓库里写东西。

本模块只做六件事（S7 D5 的允许清单 + S11 D5 的显式扩展，改这个文件时当红线）：

1. `git worktree add` —— 写 `.git/worktrees/<name>/`，带 `-b` 时再写
   `refs/heads/bot/<bot>/workspace`，以及 reflog；
2. `git worktree remove` —— 删掉那条注册记录与那个（**干净的**）目录；
3. `git worktree prune` —— 只删"目录已经没了"的那些 worktree 元数据；
4. `git add -A` + `git commit` —— 在 **linked worktree** 里固化 Bot 的改动（S11）；
5. `git branch <candidate> <sha>` —— 在**主仓库 refs** 里建候选分支（S11）；
6. 读取 `HEAD`（只读，但复用本模块的 30s 外壳与同一环境）。

五条纪律：

1. **不加 `--force`**：宁可失败也不删用户没提交的东西（实测：脏 worktree 的 `remove` 退出 128，
   加 `--force` 就删）。脏由调用方翻成 `WORKTREE_CONFLICT`（S7 D6）。
2. **主仓库操作永远 `-C <已绑定的 repo.path>`**（S3 D4 的允许根）；S11 的
   `commit_all` / `head_commit` 例外，它们的 `-C` 是**租约里记下的 Bot 工作区路径**
   （由 `worktree_dir()` 在 worktrees 根之下断言过，S10 D13）。两条路径都不接受用户现编。
3. **不解析本地化的 git 文案**：判定一律来自文件系统 + `--porcelain` 的**字段前缀**。
   实测 `worktree list` 的 `prunable` 行是 `prunable gitdir 文件指向一个不存在的位置`——
   中文！按文案匹配的代码在别的语言环境里会静默失效（T39 的同一族陷阱）。
4. **30 秒超时**：`worktree add` 要检出整棵树，读操作的 5 秒（S3 D5）会误报；超时 = 失败，不重试。
5. **环境与只读探测共用一份**（`repo.git_env`）：`GIT_TERMINAL_PROMPT=0` / `GIT_CONFIG_NOSYSTEM=1`
   是 05 §3.3 的硬约束，不是可以各写一遍的细节。

为什么单独成模块而不是塞进 `repo.py`：那边的契约是"只读探测"，`run_git` 的 docstring 与
codemap 都按这条索引。写面只有这一处，读面只有那一处，审的时候一眼能看完。
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from postel_studio.gitops.repo import (
    GitTimeoutError,
    GitUnavailableError,
    git_env,
    git_executable,
)

# S7 D5：写操作的上限。读操作是 5s（S3 D5），因为它们是毫秒级；检出整棵树不是。
WORKTREE_TIMEOUT_SECONDS: Final = 30.0
# 06 §2.4 的分支命名空间前缀。S7 只有占位分支 `bot/<bot>/workspace`（D3）。
BRANCH_PREFIX: Final = "bot"
WORKSPACE_LEAF: Final = "workspace"
_PRUNED_PREFIX: Final = "prunable"


class GitRefusedError(RuntimeError):
    """git 非零退出（"我拒绝"）。

    **不带 git 的输出**：那是本地化文案（实测中文），拿它做判断会在别的 locale 里失效（纪律 3），
    而且它可能含用户路径（05 §4.2）。调用方按**文件系统状态**决定翻成哪个稳定码。
    """


@dataclass(frozen=True)
class WorktreeEntry:
    """`git worktree list --porcelain` 的一条记录（S7 D4 的事实源）。

    `branch` 已是短名（`refs/heads/` 去掉了）；detached / bare 时为 `None`。
    `prunable` = git 记录了它，但目录已经不在了（实测：删掉目录后 git 仍列出这一条）。
    """

    path: str
    branch: str | None
    head_sha: str | None
    prunable: bool


def _run(args: list[str], *, root: Path, timeout: float = WORKTREE_TIMEOUT_SECONDS) -> str:
    """在 `root` 下跑一条 git 命令，返回 stdout；非零退出抛 `GitRefusedError`。

    与 `repo.run_git` 同款外壳（字面数组、`shell=False`、timeout、共用环境），差别只在这里
    **只服务写操作**：非零退出是"拒绝"而不是"问不出来的 None"，因为对 `worktree add` 来说
    "没有答案"不是一个有意义的状态。

    `errors="replace"`：输出里只有路径（`worktree list`），只进 DOM、永不回传 git；
    仓库位于非 UTF-8 路径时让它显示成 `�`，比为一条路径让整次操作失败合理（S5 D3.3 的同一裁决）。
    """
    command = [git_executable(), "-C", str(root), *args]
    try:
        result = subprocess.run(
            command,
            shell=False,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout,
            env=git_env(),
            stdin=subprocess.DEVNULL,
            check=False,
        )
    except FileNotFoundError:
        raise GitUnavailableError("找不到 git 命令") from None
    except subprocess.TimeoutExpired:
        raise GitTimeoutError(f"git 超时（>{timeout}s）") from None
    except OSError as exc:
        raise GitUnavailableError("git 命令无法启动") from exc
    if result.returncode != 0:
        raise GitRefusedError(f"git {args[0]} 退出码 {result.returncode}")
    return result.stdout


def list_worktrees(root: Path) -> list[WorktreeEntry]:
    """这个仓库的全部 worktree（含主工作区与 bare 记录），按 git 的输出顺序。

    解析只认**字段前缀**（`worktree ` / `HEAD ` / `branch ` / `prunable`），理由见纪律 3。
    路径按整行其余部分取（实测：路径可以带空格，`worktree /tmp/wt bare` 只切第一个空格就对了）。
    段数/字段异常时**不猜**：没有 `worktree ` 行的段直接跳过（那是空行或 git 的新字段）。
    """
    output = _run(["worktree", "list", "--porcelain"], root=root)
    entries: list[WorktreeEntry] = []
    for block in output.split("\n\n"):
        path: str | None = None
        branch: str | None = None
        head: str | None = None
        prunable = False
        for line in block.splitlines():
            if line.startswith("worktree "):
                path = line[len("worktree ") :]
            elif line.startswith("HEAD "):
                head = line[len("HEAD ") :]
            elif line.startswith("branch "):
                ref = line[len("branch ") :]
                branch = ref[len("refs/heads/") :] if ref.startswith("refs/heads/") else ref
            elif line.startswith(_PRUNED_PREFIX):
                prunable = True
        if path:
            entries.append(
                WorktreeEntry(path=path, branch=branch, head_sha=head, prunable=prunable)
            )
    return entries


def add_worktree(
    root: Path, target: Path, *, branch: str, start_point: str, create_branch: bool
) -> None:
    """`git worktree add`：建 `branch` 并检出到 `target`，或检出**已存在**的 `branch`。

    两种形态（差别不只是 `-b`，**检出对象不同**）：

    | `create_branch` | 命令 | 检出的东西 |
    |---|---|---|
    | `True` | `add -b <branch> <path> <start_point>` | `start_point`（**已解析的 commit id**） |
    | `False` | `add <path> <branch>` | 分支自己的 tip |

    第二行是实测踩出来的：分支已存在时若仍把 `start_point`（基础提交的 id）当检出对象，
    git 会**静默检出成游离 HEAD**——分支上已有的提交看不见了，而且 `worktree list` 里
    `branch` 字段消失（`branch: null`）。S7 D3 的"复用不重置"就是靠这一行成立的。

    `start_point` 永远不是用户原样输入：调用方先用 `resolve_commit` 解析（S5 D2），
    注入面收敛在那一条命令里。
    """
    args = ["worktree", "add"]
    if create_branch:
        args += ["-b", branch, str(target), start_point]
    else:
        args += [str(target), branch]
    _run(args, root=root)


def remove_worktree(root: Path, target: Path) -> None:
    """`git worktree remove`：删注册记录与目录。**没有 `--force`**（纪律 1）。

    脏 worktree 会被 git 拒绝（退出 128），抛 `GitRefusedError`；调用方用 `is_dirty` 判断
    这次拒绝是不是"里面有你的东西"，再决定翻成 `WORKTREE_CONFLICT` 还是 `INTERNAL`。
    """
    _run(["worktree", "remove", str(target)], root=root)


def prune_worktrees(root: Path) -> None:
    """`git worktree prune`：只删"目录已经不在"的元数据（实测：活着的记录不受影响）。

    这是本刀唯一的一处"修复"写操作，触发条件严格：**已注册但目录不存在**（S7 D7 第 2 行）。
    """
    _run(["worktree", "prune"], root=root)


def branch_exists(root: Path, branch: str) -> bool:
    """本地分支 `branch` 是否存在。`branch` 由本模块拼（`bot/<bot>/workspace`），不是用户输入。"""
    try:
        _run(["rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"], root=root)
    except GitRefusedError:
        return False
    return True


def is_dirty(worktree: Path) -> bool:
    """工作区有没有**会被回收丢掉的东西**（未提交改动 / 未跟踪文件）。

    三个方向都是"脏"，宁可拒绝回收也不删数据：
    - `status` 非零退出（worktree 坏掉 / `.git` 指丢）→ 脏；
    - 有任何一行输出（` M a.txt` / `?? x`）→ 脏；
    - 空输出 → 干净。

    这是**判断**而不是判定来源：`worktree remove` 自己也会拒绝（纪律 1），这里的价值只是
    把"git 拒绝"翻译成用户能懂的那句话。
    """
    try:
        output = _run(["status", "--porcelain"], root=worktree)
    except (GitRefusedError, GitUnavailableError, GitTimeoutError):
        return True
    return bool(output.strip())


# S11 D4：内核自己的提交身份，绝不读用户的 git config（T34：配置不能改断言结果）。
_COMMIT_IDENTITY: Final = ("-c", "user.email=kernel@localhost", "-c", "user.name=postel")


def head_commit(worktree: Path) -> str:
    """工作区 HEAD 的 40 位 commit id（S11 D2 的 base/head 快照）。

    只读，但走本模块的 30s 外壳与同一份环境——`repo.resolve_commit(root, "HEAD")`
    用的是 5s 读超时与另一个执行器；这里要与写操作共享超时/错误语义。
    """
    return _run(["rev-parse", "HEAD"], root=worktree).strip()


def commit_all(worktree: Path, message: str) -> str:
    """把 Bot 工作区里的改动固化成一个 commit，返回新 HEAD（S11 D4/D5）。

    只接受内核生成的 `message`（`candidate <task_id>`，无用户文本）；`git add -A` 在
    **linked worktree 内**执行，绝不写用户的主工作区。没有 `--force`/`reset`/`clean`。
    空改动时 git 会拒绝提交（退出 1）——调用方先用 `is_dirty` / HEAD 快照判断，这里
    把失败如实抛成 `GitRefusedError`，不吞成"成功但没有 commit"。
    """
    _run(["add", "-A"], root=worktree)
    _run([*_COMMIT_IDENTITY, "commit", "-m", message], root=worktree)
    return head_commit(worktree)


def create_branch(root: Path, branch: str, start_sha: str) -> None:
    """在**主仓库**建一个指向 `start_sha` 的候选分支（S11 D5）。

    `root` 必须是已绑定的仓库根（`repo.path`），`branch` 由 `candidate_branch()` 派生，
    `start_sha` 是 `head_commit()` 的产出——三者都不是用户直接输入的原样文本。
    分支已存在时 git 会拒绝（`GitRefusedError`），不静默覆盖。
    """
    _run(["branch", branch, start_sha], root=root)


GIT_WRITE_ERRORS: Final[tuple[type[Exception], ...]] = (
    GitUnavailableError,
    GitTimeoutError,
    GitRefusedError,
)

__all__ = [
    "BRANCH_PREFIX",
    "GIT_WRITE_ERRORS",
    "WORKSPACE_LEAF",
    "WORKTREE_TIMEOUT_SECONDS",
    "GitRefusedError",
    "WorktreeEntry",
    "add_worktree",
    "branch_exists",
    "commit_all",
    "create_branch",
    "head_commit",
    "is_dirty",
    "list_worktrees",
    "prune_worktrees",
    "remove_worktree",
]
