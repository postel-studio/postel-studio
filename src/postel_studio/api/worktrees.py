"""`worktree.acquire` / `worktree.list` / `worktree.release` 的业务编排（S7 D1–D8）。

与 `api/repository.py` / `api/bots.py` 同一层、同一纪律：**不碰 fastapi**，只吃 `dict`、返回
`dict` / `list[dict]`，错误一律翻成 07 §1.3 的码；这一层负责"路径 → 校验 → git → 载荷"，
以及把 `gitops/worktrees.py` 的三种异常翻成稳定码。

三处裁决的落点：

- **D4 不落库**：工作区的事实源是 `git worktree list` 加目录是否存在。本模块**不写** state.db，
  也不缓存任何东西——`list` 每次重新问 git，`acquire`/`release` 成功后再读一次事实，
  而不是自己拼一个"应该是什么"的载荷。
- **D7 状态机**：两个布尔量（是否被 git 注册、目录是否存在）决定全部五种情形；判定只用文件系统
  与 `--porcelain` 的字段前缀，**不解析本地化文案**（`gitops/worktrees.py` 纪律 3）。
- **D6 错误码**：`WORKTREE_CONFLICT` 是本刀**唯一**新增的码，覆盖三种"正常用户处境"（目录被占、
  有未提交改动、分支被别的 worktree 占着）。翻成 `INTERNAL` 会让用户去"报障"，那是假话。

`WORKTREE_CONFLICT` 的文案只有一句：页面按**码**映射（05 §4.2 / 07 §1.3），不看 message。
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

from postel_studio.api import frames
from postel_studio.domain.models import Bot, Repo, bot_id, bot_name_ok, ref_name_ok
from postel_studio.gitops import repo as gitops
from postel_studio.gitops import worktrees as wt
from postel_studio.storage.db import BotStore, RepoStore
from postel_studio.storage.paths import ensure_worktree_parent, worktree_dir, worktrees_root

# 07 §1.3 已登记的码（`INVALID_PARAMS` / `INTERNAL` 在 frames 里，其余是业务码）。
INVALID_PARAMS: Final = frames.INVALID_PARAMS
INTERNAL: Final = frames.INTERNAL
REPO_NOT_BOUND: Final = "REPO_NOT_BOUND"
BAD_BASE_REF: Final = "BAD_BASE_REF"
ADAPTER_UNAVAILABLE: Final = "ADAPTER_UNAVAILABLE"
NOT_SUPPORTED: Final = "NOT_SUPPORTED"
# S7 唯一新增的码（D6）：409，人工处理后可重试。PR 体里单独标出，07 的表同一次改。
WORKTREE_CONFLICT: Final = "WORKTREE_CONFLICT"

# 错误文案：**可读、面向人、不含任何用户输入**（05 §4.2）。改文案不需要改码。
MSG_REPO_ID_INVALID: Final = "repoId 参数缺失或不是字符串"
MSG_REPO_NOT_BOUND: Final = "仓库未绑定"
MSG_REPO_PATH_MISSING: Final = "仓库路径已不存在"
MSG_BOT_ID_INVALID: Final = "botId 参数缺失或不是字符串"
MSG_BOT_NOT_FOUND: Final = "找不到这个 Bot"
MSG_BASE_REF_INVALID: Final = "baseRef 参数不合法"
MSG_NO_DEFAULT_BRANCH: Final = "仓库没有默认分支（HEAD 未指向分支），请指定 baseRef"
MSG_BAD_BASE_REF: Final = "baseRef 不存在或不是提交"
MSG_CONFLICT: Final = "工作区目录被占用，或里面有未提交的改动"
MSG_GIT_REFUSED: Final = "工作区命令被 git 拒绝"
MSG_ADAPTER_UNAVAILABLE: Final = "git 命令不可用"
MSG_GIT_TIMEOUT: Final = "git 命令超时"  # 同 S3 D6：先按 INTERNAL，S19 的 doctor 再细化
MSG_NOT_SUPPORTED_REFNAME: Final = "仓库含非 UTF-8 的名称，第一版不支持"

# `WorktreeService` 里有个方法叫 `list`，类作用域的 `list[...]` 会被 mypy 解析成那个方法
# （`valid-type`）。列表载荷的类型用模块级别名（与 repository.py / bots.py 同一处教训）。
type WorktreePayloads = list[dict[str, object]]


def _fail(code: str, message: str) -> frames.FrameError:
    """帧层错误（`FrameError` 由网关原样翻成 `ok:false` 帧）。"""
    return frames.FrameError(code, message)


def _same_path(candidate: str, target: Path) -> bool:
    """git 报的路径与我们要的路径是不是同一个（两边都 `resolve()`：符号链接/`..` 不算差异）。"""
    return Path(candidate).resolve() == target.resolve()


def _find(entries: list[wt.WorktreeEntry], target: Path) -> wt.WorktreeEntry | None:
    """在 git 的记录里找"我们的那个路径"。找不到 = 没注册（D7 下半张表）。"""
    return next((entry for entry in entries if _same_path(entry.path, target)), None)


class WorktreeService:
    """S7 的三个帧方法。构造时接收两个 store（依赖注入，测试用临时库）。"""

    def __init__(self, repos: RepoStore, bots: BotStore) -> None:
        self._repos = repos
        self._bots = bots

    # -- 方法表 --------------------------------------------------------------
    def list(self, params: dict[str, object]) -> WorktreePayloads:
        """`worktree.list`：本仓库下属于本内核的工作区（0 或 1 条，多 Bot 时才是多条）。

        过滤三条，缺一不可（D4/D7）：git 里注册了、目录真的在、路径在本仓库的 worktrees 子树下
        且末段是合法 bot 名。`prunable`（目录已删）**不出现在列表里**——它不是"一个工作区"，
        而是一条待清理的元数据，由 acquire/release 顺手修（D7 第 2 行）。
        """
        repo = self._require_repo(params)
        try:
            # T25：`git -C` 会向父目录 discover，先判"这个路径还是不是仓库"再问。
            if not gitops.is_usable_repo(Path(repo.path)):
                raise _fail(REPO_NOT_BOUND, MSG_REPO_PATH_MISSING)
            entries = wt.list_worktrees(Path(repo.path))
        except gitops.GitUnavailableError:
            raise _fail(ADAPTER_UNAVAILABLE, MSG_ADAPTER_UNAVAILABLE) from None
        except gitops.GitTimeoutError:
            raise _fail(INTERNAL, MSG_GIT_TIMEOUT) from None
        except wt.GitRefusedError:
            # 问不出来 = 仓库不可用（路径被删 / `.git` 被删）。正常用户处境，不是"需报障"。
            raise _fail(REPO_NOT_BOUND, MSG_REPO_PATH_MISSING) from None
        return [
            self._payload(repo.repo_id, entry, reused=None)
            for entry in entries
            if self._is_ours(repo, entry)
        ]

    def acquire(self, params: dict[str, object]) -> dict[str, object]:
        """`worktree.acquire`：需要时创建工作区，已有则复用（D7 上半张表）。

        | 情形 | 结果 |
        |---|---|
        | 已注册 + 目录在 | 复用，`reused: true`，**一次 git 写操作都不做** |
        | 已注册 + 目录不在 | `prune` 后重建（修掉那条 stale 元数据） |
        | 未注册 + 目录不存在 / 空目录 | 重建（空目录 git 也接受，实测） |
        | 未注册 + 目录非空 / 是文件 | `WORKTREE_CONFLICT`（不删用户的东西） |
        | 分支被别的 worktree 占着 | `WORKTREE_CONFLICT`（不解析 git 文案，按记录判） |

        基础分支先解析成 commit（`resolve_commit`，S5 D2 的唯一入口），再交给 `worktree add`；
        于是"用户字符串进 git"仍然只有这一条命令。分支不存在就 `-b` 新建（D3：不检出基础分支
        本身——它几乎总被主工作区占着，实测 git 会拒绝）。
        """
        repo = self._require_repo(params)
        bot = self._require_bot(params, repo)
        base_ref = self._base_ref(params, repo)
        target = worktree_dir(repo.repo_id, bot.name)
        branch = f"{wt.BRANCH_PREFIX}/{bot.name}/{wt.WORKSPACE_LEAF}"
        root = Path(repo.path)
        # 失败时是否允许清掉空目录：缺省**不允许**（只有确认"这一次不是它建出来的"才改成 True）
        preexisting = True
        try:
            # 先于一切 git 调用（T25：别在别人的仓库里建工作区）
            if not gitops.is_usable_repo(Path(repo.path)):
                raise _fail(REPO_NOT_BOUND, MSG_REPO_PATH_MISSING)
            base_sha = gitops.resolve_commit(root, base_ref)
            if base_sha is None:  # 含空仓库：0 提交时没有可用的 commit
                raise _fail(BAD_BASE_REF, MSG_BAD_BASE_REF)
            entries = wt.list_worktrees(root)
            registered = _find(entries, target)
            if registered is not None and not registered.prunable and target.is_dir():
                return self._payload(repo.repo_id, registered, reused=True)
            if registered is not None:  # 目录被手删了：先修掉 stale 元数据，再当"没建过"处理
                wt.prune_worktrees(root)
                entries = wt.list_worktrees(root)
            for entry in entries:  # 分支被别的 worktree 占着：按**记录**判，不解析文案
                if entry.branch == branch and not _same_path(entry.path, target):
                    raise _fail(WORKTREE_CONFLICT, MSG_CONFLICT)
            if target.exists() and (not target.is_dir() or any(target.iterdir())):
                raise _fail(WORKTREE_CONFLICT, MSG_CONFLICT)  # 非空目录 / 是文件：不动它
            ensure_worktree_parent(repo.repo_id, bot.name)
            preexisting = target.is_dir()  # 失败时只清"这一次不是它建出来"的空目录
            wt.add_worktree(
                root,
                target,
                branch=branch,
                start_point=base_sha,
                create_branch=not wt.branch_exists(root, branch),
            )
            # D4：成功之后**重新读事实**，而不是自己拼"应该是"的 HEAD（复用的分支可能已经不是
            # base_sha 了：`-b` 只在分支不存在时用，已存在时检出的是它自己的 tip）。
            stored = _find(wt.list_worktrees(root), target)
        except gitops.GitUnavailableError:
            raise _fail(ADAPTER_UNAVAILABLE, MSG_ADAPTER_UNAVAILABLE) from None
        except gitops.GitTimeoutError:
            raise _fail(INTERNAL, MSG_GIT_TIMEOUT) from None
        except gitops.GitOutputUndecodableError:  # 非 UTF-8 refname（S5 D3.2 / U15 的裁决）
            raise _fail(NOT_SUPPORTED, MSG_NOT_SUPPORTED_REFNAME) from None
        except wt.GitRefusedError as exc:
            if not preexisting:
                self._drop_empty_dir(target)  # 只删"空的"：绝不删用户的东西
            raise _fail(INTERNAL, MSG_GIT_REFUSED) from exc
        if stored is None:  # pragma: no cover - git 建完却读不回，只可能是 git 坏了
            raise _fail(INTERNAL, MSG_GIT_REFUSED)
        return self._payload(repo.repo_id, stored, reused=False)

    def release(self, params: dict[str, object]) -> dict[str, object]:
        """`worktree.release`：回收工作区。**不删分支**（D3）、**不 `--force`**（D5）。

        | 情形 | 结果 |
        |---|---|
        | 已注册 + 目录在 + 干净 | `worktree remove` → `{removed: true}` |
        | 已注册 + 目录在 + 脏 | `WORKTREE_CONFLICT`，**目录原样保留** |
        | 已注册 + 目录不在 | `prune` → `{removed: true}`（它其实早就没了） |
        | 未注册 + 目录不在 | `{removed: false}`——重复点"回收"不是错误 |
        | 未注册 + 目录在 | `WORKTREE_CONFLICT`（不是本内核的工作区，拒绝碰它） |
        """
        repo = self._require_repo(params)
        bot = self._require_bot(params, repo)
        target = worktree_dir(repo.repo_id, bot.name)
        root = Path(repo.path)
        try:
            if not gitops.is_usable_repo(Path(repo.path)):
                raise _fail(REPO_NOT_BOUND, MSG_REPO_PATH_MISSING)
            entry = _find(wt.list_worktrees(root), target)
            if entry is None:
                if target.exists():
                    raise _fail(WORKTREE_CONFLICT, MSG_CONFLICT)
                return {"removed": False, "path": str(target)}
            if entry.prunable or not target.is_dir():
                wt.prune_worktrees(root)
                return {"removed": True, "path": str(target)}
            try:
                wt.remove_worktree(root, target)
            except wt.GitRefusedError as exc:
                # git 拒绝了：是"里面有你的东西"（正常处境 → 409）还是别的原因（→ 500）？
                # 判据是 `git status --porcelain`，不是 git 的那句本地化文案（D7）。
                if wt.is_dirty(target):
                    raise _fail(WORKTREE_CONFLICT, MSG_CONFLICT) from None
                raise _fail(INTERNAL, MSG_GIT_REFUSED) from exc
        except gitops.GitUnavailableError:
            raise _fail(ADAPTER_UNAVAILABLE, MSG_ADAPTER_UNAVAILABLE) from None
        except gitops.GitTimeoutError:
            raise _fail(INTERNAL, MSG_GIT_TIMEOUT) from None
        except gitops.GitOutputUndecodableError:
            raise _fail(NOT_SUPPORTED, MSG_NOT_SUPPORTED_REFNAME) from None
        return {"removed": True, "path": str(target)}

    # -- 内部 ----------------------------------------------------------------
    def _require_repo(self, params: dict[str, object]) -> Repo:
        """`repoId` 形状 → `INVALID_PARAMS`；查不到 → `REPO_NOT_BOUND`（与 S4/S5/S6 同一条）。"""
        value = params.get("repoId")
        if not isinstance(value, str) or not value:
            raise _fail(INVALID_PARAMS, MSG_REPO_ID_INVALID)
        repo = self._repos.get(value)
        if repo is None:
            raise _fail(REPO_NOT_BOUND, MSG_REPO_NOT_BOUND)
        return repo

    def _require_bot(self, params: dict[str, object], repo: Repo) -> Bot:
        """`botId` 形状/存在性/归属：三者任一不成立都 `INVALID_PARAMS`（S6 D6 的先例）。

        **归属必须查**：`botId` 是 `{repo_id}/{name}` 的复合键，跨仓库传进来时若不拦，工作区
        会按 A 仓库的路径建、却挂着 B 仓库的 Bot——路径里的 `name` 是合法的，所以只有"这个 Bot
        是不是这个仓库的"这一条能拦住它。穿越企图不必单独判：库里查不到就拒绝，一个文件都不碰。
        """
        value = params.get("botId")
        if not isinstance(value, str) or not value:
            raise _fail(INVALID_PARAMS, MSG_BOT_ID_INVALID)
        bot = self._bots.get(value)
        if bot is None or bot.repo_id != repo.repo_id:
            raise _fail(INVALID_PARAMS, MSG_BOT_NOT_FOUND)
        return bot

    def _base_ref(self, params: dict[str, object], repo: Repo) -> str:
        """`baseRef`：缺省用仓库的默认分支；给了就过 S5 D3.1 的形状（与 `repo.diff` 同一份）。

        游离 HEAD（`default_branch is None`）且没给 `baseRef` → `INVALID_PARAMS`：
        这不是"分支无效"，是**调用方少给了一个参数**（S5 D9 对 `defaultBranch=null` 的同一裁决）。
        """
        value = params.get("baseRef")
        if value is None:
            if repo.default_branch is None:
                raise _fail(INVALID_PARAMS, MSG_NO_DEFAULT_BRANCH)
            return repo.default_branch
        if not isinstance(value, str) or not ref_name_ok(value):
            raise _fail(INVALID_PARAMS, MSG_BASE_REF_INVALID)
        return value

    def _is_ours(self, repo: Repo, entry: wt.WorktreeEntry) -> bool:
        """这条 git 记录是不是**本内核**在这个仓库下的工作区（D4 的过滤，三条缺一不可）。"""
        path = Path(entry.path)
        if entry.prunable or not path.is_dir():
            return False
        if not bot_name_ok(path.name):
            return False
        return path.resolve().is_relative_to((worktrees_root() / repo.repo_id).resolve())

    def _payload(
        self, repo_id: str, entry: wt.WorktreeEntry, *, reused: bool | None
    ) -> dict[str, object]:
        """实体 → 帧载荷（D1：**恰好七个字段**；`list` 的条目没有 `reused`，故传 `None`）。"""
        name = Path(entry.path).name
        data: dict[str, object] = {
            "repoId": repo_id,
            "botId": bot_id(repo_id, name),
            "botName": name,
            "path": entry.path,
            "branch": entry.branch,
            "headSha": (entry.head_sha or "")[:8],  # D1：8 位小写 hex（边界格式化）
        }
        if reused is not None:
            data["reused"] = reused
        return data

    @staticmethod
    def _drop_empty_dir(target: Path) -> None:
        """创建失败后只清掉**空**目录（`worktree add` 有时会先建目录再失败）。

        非空就不动：那可能是 git 建了一半的检出，也可能是用户的东西——两种都不该由这里删
        （06 §5「永不自动删除用户文件」）。下一次 acquire 会把它判成 `WORKTREE_CONFLICT` 并
        让人处理，这比悄悄删掉安全。
        """
        try:
            if target.is_dir() and not any(target.iterdir()):
                target.rmdir()
        except OSError:  # 删不掉不是新错误：原始失败已经在上抛了
            pass
