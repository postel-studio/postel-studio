"""`repo.bind` / `repo.list` 的业务编排（S3）。

**这一层不碰 fastapi**（S3 涉及文件表的要求）：它只接收 `dict` 参数、返回 `dict`，
把"路径 → 校验 → 探测 → 落库"串起来，并把下游异常翻成 07 §1.3 的**稳定错误码**。
框架怎么把这两条接上 WebSocket，是 `api/gateway.py` 的事。

为什么校验要在这里而不是在网关：网关只该知道"方法名 → 处理函数"（02-layers 的
`gateway` 里那条"禁止 Agent 特判"是同一精神），字段语义属于业务。D6 的错误码表也在这里
集中成一张小对照，看一眼就知道本刀用了哪几个码。

四条裁决在这一层的落点：

- **D4**：`path` 必须先断言绝对、再 `resolve()`，然后交给 gitops 求**仓库根**。
  "允许根"从这一刻起就是落库的 `path`——`gitops.run_git(root=...)` 的显式参数。
- **D2**：返回的 `path` / `name` 是**仓库根**，不是用户粘的子目录。
- **D3**：`repo_id` 由 `domain.models.repo_id()` 算，这里不另写一份。
- **D6 / 05 §4.2**：所有 `message` 常量里**没有一个字**来自用户输入（没有 `f"{path}"`）。
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Final

from postel_studio.api import frames
from postel_studio.domain.models import Repo, repo_id
from postel_studio.gitops import repo as gitops
from postel_studio.storage.db import RepoStore

# 07 §1.3 已登记的码（**新建码是禁止事项**）。这里再写一份常量而不是从 frames 里 import：
# `NOT_A_REPO` / `BAD_CWD` / `ADAPTER_UNAVAILABLE` / `REPO_NOT_BOUND` 在 frames 里没有
# 定义（它是 S2 的帧层，只管帧合法性），把它们塞进 frames 会让帧层多出业务语义。
NOT_A_REPO: Final = "NOT_A_REPO"
BAD_CWD: Final = "BAD_CWD"
ADAPTER_UNAVAILABLE: Final = "ADAPTER_UNAVAILABLE"
REPO_NOT_BOUND: Final = "REPO_NOT_BOUND"

# 错误文案：**可读、面向人、不含任何用户输入**（05 §4.2）。改文案不需要改码。
MSG_BAD_CWD: Final = "path 必须是绝对路径"
MSG_NOT_A_REPO: Final = "路径不是 git 仓库"
MSG_INVALID_PARAMS: Final = "path 参数缺失或不是字符串"
MSG_ADAPTER_UNAVAILABLE: Final = "git 命令不可用"
MSG_GIT_TIMEOUT: Final = "git 命令超时"  # D6：先按 INTERNAL，S19 的 doctor 再细化
MSG_REPO_NOT_BOUND: Final = "仓库未绑定"


def _fail(code: str, message: str) -> frames.FrameError:
    """帧层错误。`FrameError` 同时带 `code` 与 `message`，网关原样翻成 `ok:false` 帧。"""
    return frames.FrameError(code, message)


def _require_path(params: dict[str, object]) -> str:
    """取出并校验 `path` 的**形状**（绝对路径检查留给 `bind`，那是 D4 而不是参数问题）。"""
    value = params.get("path")
    if not isinstance(value, str) or not value:
        raise _fail(frames.INVALID_PARAMS, MSG_INVALID_PARAMS)
    return value


def _bindable_root(target: Path) -> Path | None:
    """`repo.bind` 的总入口：`target` 可绑定则返回**要落库的仓库根**，否则 `None`。

    四条规则（D2 + D6），顺序有意义。完整来龙去脉见 06-pitfalls T25——
    **别把判定改回"`git rev-parse --show-toplevel` 有输出就算仓库"**：那条会让 `/tmp`
    和任何落在别人工作区里的普通目录都"绑定成功"。

    1. **目标自己就是工作区根**（`target/.git` 存在）→ 绑 `target`。
    2. **目标自己就是裸仓库根**（`git init --bare`，没有 `.git` 但 git 说 is-bare）→ 绑 `target`。
    3. 目标是**目录**、且在某个工作区里面 → 绑那个工作区根（子目录校正 `proj/src` → `proj`）。
    4. 其它（不存在 / 是文件 / 普通目录）→ `NOT_A_REPO`。

    第 3 条的"在某个工作区里面"用**逐级向上**找最近的带 `.git` 的祖先来确定，而不是问 git：
    git 的 discover 会把"某个祖先恰好是仓库"当成"我在这仓库里"，见 T25。逐级找的答案与
    "最近的祖先"一致，没有歧义；代价是**无法区分** `proj/src`（真子目录）与
    `proj/别的普通目录`（两者都是 `proj` 的子目录），这是本刀接受的近似（见 codemap 未决问题）。
    """
    if target.is_dir() and gitops.is_worktree_root(target):
        return target
    if target.is_dir() and gitops.bare_root(target) is not None:
        return target
    if not target.is_dir():
        return None  # 不存在 / 是文件（05 §3.2 的路径校验在 git 之前先做一次）
    for ancestor in target.parents:
        if gitops.is_worktree_root(ancestor):
            return ancestor
    return None


def _split(repo: Repo) -> dict[str, object]:
    """实体 → 帧载荷（07 §4.2：字段名在边界转 `camelCase`）。

    `isDirty` 不在本刀的探测范围（07 §4.1 的示例里有它，但 S4 才读工作区状态），
    **不填**一个假的 `false`——宁可字段缺席让客户端按"未知"处理。
    """
    return {
        "repoId": repo.repo_id,
        "name": repo.name,
        "path": repo.path,
        "originUrl": repo.origin_url,
        "defaultBranch": repo.default_branch,
    }


class RepositoryService:
    """`repo.bind` / `repo.list` 的实现。构造时接收 store（依赖注入，方便测试换临时库）。"""

    def __init__(self, store: RepoStore) -> None:
        self._store = store

    def list(self) -> list[dict[str, object]]:
        """`repo.list`：全部已绑定仓库（数组载荷，07 §1.2 的 `data`）。"""
        return [_split(repo) for repo in self._store.list()]

    def bind(self, params: dict[str, object]) -> dict[str, object]:
        """`repo.bind`：探测仓库 → 落库 → 返回**落库后的**那一条。

        失败一律抛 `FrameError`（07 §1.3 的码）；成功返回实体载荷。
        """
        raw = _require_path(params)

        # D4：相对路径不是"不好的路径"，而是**调用方式错了** → BAD_CWD（可修正后重试）。
        # 注意 `Path("~/x").is_absolute()` 为 False：`~` 不展开（05 §3.3 禁止展开 `~`）。
        if not Path(raw).is_absolute():
            raise _fail(BAD_CWD, MSG_BAD_CWD)

        # D4：`resolve()` 消 `..` 与符号链接后再用；**不**信任原始串
        target = Path(raw).resolve()

        try:
            root = _bindable_root(target)
            if root is None:
                raise _fail(NOT_A_REPO, MSG_NOT_A_REPO)  # 不存在/非目录/非仓库统一这个码（D6）
            origin = gitops.origin_url(root)  # 只问一次：两次答案理论上可能不同（用户正在改配置）
            repo = Repo(
                repo_id=repo_id(str(root), origin),
                path=str(root),
                origin_url=origin,
                default_branch=gitops.default_branch(root),
                added_at=time.time(),
            )
        except gitops.GitUnavailableError:
            # D5：git 不存在**不崩内核**，回 503 类码；S19 的 doctor 负责告诉用户怎么装
            raise _fail(ADAPTER_UNAVAILABLE, MSG_ADAPTER_UNAVAILABLE) from None
        except gitops.GitTimeoutError:
            raise _fail(frames.INTERNAL, MSG_GIT_TIMEOUT) from None
        # D6 的 NOT_A_REPO 在 try 里抛：不要被上面的 except 顺手吞掉——FrameError 不是
        # 它们的子类，这里显式说明一次，免得后来者为了"统一处理"而改成 `except Exception`。

        return _split(self._store.upsert(repo))

    def get(self, params: dict[str, object]) -> dict[str, object]:
        """按 `repoId` 取一条（本刀没有帧方法用到它，但 `REPO_NOT_BOUND` 的语义要立住）。

        S4 的 `repo.branches` 会以它为入口；把它放在这里而不是让 S4 自己查库，
        是为了让"允许根 = 已绑定的 path"（D4 后半句）只有一个实现。
        """
        value = params.get("repoId")
        if not isinstance(value, str) or not value:
            raise _fail(frames.INVALID_PARAMS, "repoId 参数缺失或不是字符串")
        repo = self._store.get(value)
        if repo is None:
            raise _fail(REPO_NOT_BOUND, MSG_REPO_NOT_BOUND)
        return _split(repo)
