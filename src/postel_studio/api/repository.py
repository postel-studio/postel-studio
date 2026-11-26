"""`repo.bind` / `repo.list` / `repo.branches` / `repo.diff` 的业务编排（S3；S4 分支；S5 diff）。

**这一层不碰 fastapi**（S3 涉及文件表的要求）：它只接收 `dict` 参数、返回 `dict`，
把"路径 → 校验 → 探测 → 落库"串起来，并把下游异常翻成 07 §1.3 的**稳定错误码**。
框架怎么把这些接上 WebSocket，是 `api/gateway.py` 的事。

S4 的 `branches` 是 `repo.bind` 的**只读反向**：`repoId` → 库里那条记录 → 已绑定的 `path`
当允许根 → `gitops.list_branches` → 帧载荷。S5 的 `diff` 同一形状：两个 ref 各解析成
object id（`gitops.resolve_commit`）→ `diff_stat` / `diff_patch` → 截断 + camelCase 载荷。
两条都不落库、不缓存（06-data-model §8）。

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
from postel_studio.domain.models import Repo, origin_hash, repo_id
from postel_studio.gitops import repo as gitops
from postel_studio.storage.db import RepoStore

# 07 §1.3 已登记的码（**新建码是禁止事项**）。这里再写一份常量而不是从 frames 里 import：
# `NOT_A_REPO` / `BAD_CWD` / `ADAPTER_UNAVAILABLE` / `REPO_NOT_BOUND` 在 frames 里没有
# 定义（它是 S2 的帧层，只管帧合法性），把它们塞进 frames 会让帧层多出业务语义。
NOT_A_REPO: Final = "NOT_A_REPO"
BAD_CWD: Final = "BAD_CWD"
ADAPTER_UNAVAILABLE: Final = "ADAPTER_UNAVAILABLE"
REPO_NOT_BOUND: Final = "REPO_NOT_BOUND"
# S5：07 §1.3 里**本来就有**的两个码，只是本模块第一次用到（不是新增码）。
BAD_REF: Final = "BAD_REF"
NOT_SUPPORTED: Final = "NOT_SUPPORTED"

# 错误文案：**可读、面向人、不含任何用户输入**（05 §4.2）。改文案不需要改码。
MSG_BAD_CWD: Final = "path 必须是绝对路径"
MSG_NOT_A_REPO: Final = "路径不是 git 仓库"
MSG_INVALID_PARAMS: Final = "path 参数缺失或不是字符串"
MSG_ADAPTER_UNAVAILABLE: Final = "git 命令不可用"
MSG_GIT_TIMEOUT: Final = "git 命令超时"  # D6：先按 INTERNAL，S19 的 doctor 再细化
MSG_REPO_NOT_BOUND: Final = "仓库未绑定"
MSG_REPO_ID_INVALID: Final = "repoId 参数缺失或不是字符串"
# S4 D5：绑定的路径已不是仓库（目录被删 / .git 被删）用 REPO_NOT_BOUND + 这句文案。
# 不夸大字眼：这不是"未预期错误"（INTERNAL），而是用户自己删了仓库——正常处境。
MSG_REPO_PATH_MISSING: Final = "仓库路径已不存在"
# S5 D3.1 / D7：ref 参数形状、ref 解析、三点没有 merge base、非 UTF-8 ref 名各自的文案。
MSG_INVALID_REF: Final = "base / head 参数不合法"
MSG_BAD_REF: Final = "分支或提交不存在"
MSG_NO_MERGE_BASE: Final = "两个 ref 没有共同祖先，无法比较"
MSG_NOT_SUPPORTED_REFNAME: Final = "仓库含非 UTF-8 的名称，第一版不支持"
MSG_MAX_CHARS_INVALID: Final = "maxChars 必须是 1000 到 200000 之间的整数"

# S5 D6：截断上限。缺省 100k 字符；合法区间 **越界报错、不夹紧**——夹紧会让调用方以为
# 拿到了它要的长度。`maxChars` 必须是 `int` 且**不是 `bool`**（`True` 是 `int` 子类）。
DEFAULT_MAX_CHARS: Final = 100_000
MIN_MAX_CHARS: Final = 1_000
MAX_MAX_CHARS: Final = 200_000
# D3.1：ref 名长度上限（git 的 refname 本身远短于这个数；这是入站形状检查的一部分）。
MAX_REF_LENGTH: Final = 200

# `RepositoryService` 里有个方法叫 `list`，**类作用域**里的 `list[...]` 注解会被 mypy 解析成
# 那个方法（`valid-type`）。所以分支载荷的列表类型用模块级别名，别在类体里直接写 `list[...]`。
type BranchPayloads = list[dict[str, object]]


def _fail(code: str, message: str) -> frames.FrameError:
    """帧层错误。`FrameError` 同时带 `code` 与 `message`，网关原样翻成 `ok:false` 帧。"""
    return frames.FrameError(code, message)


def _require_path(params: dict[str, object]) -> str:
    """取出并校验 `path` 的**形状**（绝对路径检查留给 `bind`，那是 D4 而不是参数问题）。"""
    value = params.get("path")
    if not isinstance(value, str) or not value:
        raise _fail(frames.INVALID_PARAMS, MSG_INVALID_PARAMS)
    return value


def _require_ref(params: dict[str, object], field: str) -> str:
    """取出并校验一个 **入站 ref 名** 的形状（S5 D3.1）；不合法 → `INVALID_PARAMS`。

    六条同时满足才算合法：`str`、非空、长度 ≤ `MAX_REF_LENGTH`、不含控制字符
    （`< 0x20` 或 `0x7F`）、`encode("utf-8")` 不抛、`strip()` 后非空、不以 `-` 开头。

    **lone surrogate 必须在这里挡掉**（实测，不是推测）：JSON 允许 `"\\udcff"` 这种转义，
    `json.loads` 会得到一个 lone surrogate 的 `str`；Python 给子进程编码 argv 时用
    `surrogateescape`，于是它在**字节层**变成原始 `0xFF`——正好是 git 里那个非 UTF-8
    ref 名的字节，`rev-parse --verify` 实测**真的解析成功**。不挡的话，`repo.branches`
    拒绝展示的名字，客户端能用 JSON 转义"洗"回 git：一侧拒绝、另一侧接受 = 漏洞。
    `encode` 是唯一可靠的判据（`isinstance(s, str)` / `len()` 都看不出 lone surrogate）。
    """
    value = params.get(field)
    if not isinstance(value, str):  # 缺字段（None）/ 数字等：同一句话
        raise _fail(frames.INVALID_PARAMS, MSG_INVALID_REF)
    if not value or len(value) > MAX_REF_LENGTH or value.startswith("-") or not value.strip():
        raise _fail(frames.INVALID_PARAMS, MSG_INVALID_REF)
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in value):
        raise _fail(frames.INVALID_PARAMS, MSG_INVALID_REF)
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:  # lone surrogate：只有编码才会炸
        raise _fail(frames.INVALID_PARAMS, MSG_INVALID_REF) from None
    return value


def _require_max_chars(params: dict[str, object]) -> int:
    """取出并校验 `maxChars`（S5 D6）：缺省 `DEFAULT_MAX_CHARS`，否则必须合法，否则报错。

    **不夹紧**：越界报 `INVALID_PARAMS` 而不是静默夹到边界——夹紧会让调用方以为拿到了
    它要的长度。类型判据必须先排除 `bool`：`isinstance(True, int)` 为真，`maxChars: true`
    会被当成 `1`（真值陷阱，S5 D6 点名的边界）。
    """
    value = params.get("maxChars", DEFAULT_MAX_CHARS)
    if not isinstance(value, int) or isinstance(value, bool):
        raise _fail(frames.INVALID_PARAMS, MSG_MAX_CHARS_INVALID)
    if not MIN_MAX_CHARS <= value <= MAX_MAX_CHARS:
        raise _fail(frames.INVALID_PARAMS, MSG_MAX_CHARS_INVALID)
    return value


def _inside_git_dir(root: Path, target: Path) -> bool:
    """从已确认的 `root` 走到 `target` 的路上**有 `.git` 段**吗？有就说明用户在粘仓库内部。

    ⚠️ 判据必须**相对于 `root`**，不能看整条绝对路径（S3 复审第二次退回的缺陷，见 T31）。
    反例：`/tmp/edge/.git/workspace` 是一个**合法仓库**，只是它的父目录正好叫 `.git`——
    按"绝对路径任意一段是 `.git` 就拒绝"，它会被误杀；而它自己带 `.git`、有提交，理应能绑。

    | root / target | 相对路径 | 结论 |
    |---|---|---|
    | `target` 自己就是根（相对路径为空）且不叫 `.git` | `""` | 接受 |
    | `<repo>` / `<repo>/.git/objects` | `.git/objects` | **拒绝** |
    | `<repo>` / `<repo>/src` | `src` | 接受（D2 子目录校正） |
    | `/tmp/edge/.git/workspace`（根是它自己） | `""` | **接受**（T31） |
    """
    relative = target.relative_to(root)
    if any(part == ".git" for part in relative.parts):
        return True
    # 根自己就叫 `.git`（例如用户直接粘 `<repo>/.git` 且它碰巧被判定成了根）——仍然拒绝
    return not relative.parts and target.name == ".git"


def _bindable_root(target: Path) -> Path | None:
    """`repo.bind` 的总入口：`target` 可绑定则返回**要落库的仓库根**，否则 `None`。

    五条规则（D2 + D6），顺序有意义。完整来龙去脉见 06-pitfalls T25 / T29 / T31——
    **别把判定改回"`git rev-parse --show-toplevel` 有输出就算仓库"**：那条会让 `/tmp`
    和任何落在别人工作区里的普通目录都"绑定成功"。

    1. **目标自己就是工作区根**（`target/.git` 存在）→ 绑 `target`。
    2. **目标自己就是裸仓库根**（`git init --bare`，没有 `.git` 但 git 说 is-bare）→ 绑 `target`。
    3. 目标是**目录**、且在某个工作区里面 → 绑那个工作区根（子目录校正 `proj/src` → `proj`）。
    4. 其它（不存在 / 是文件 / 普通目录）→ `NOT_A_REPO`。
    5. 求得根之后，若**从根到目标的相对路径里含 `.git` 段** → `NOT_A_REPO`（T29）。

    第 5 条放在**求出根之后**而不是"先看整条路径"：那是 T31 的修法，见 `_inside_git_dir`。
    第 3 条的"在某个工作区里面"用**逐级向上**找最近的带 `.git` 的祖先来确定，而不是问 git：
    git 的 discover 会把"某个祖先恰好是仓库"当成"我在这仓库里"，见 T25。逐级找的答案与
    "最近的祖先"一致，没有歧义；代价是**无法区分** `proj/src`（真子目录）与
    `proj/别的普通目录`（两者都是 `proj` 的子目录），这是本刀接受的近似（见 codemap 未决问题）。
    """
    root: Path | None
    if not target.is_dir():
        return None  # 不存在 / 是文件（05 §3.2 的路径校验在 git 之前先做一次）
    if gitops.is_worktree_root(target) or gitops.bare_root(target) is not None:
        root = target  # 规则 1 / 2：目标自己就是根（工作区根或裸仓库根）
    else:
        # 规则 3：D2 子目录校正——逐级向上找最近的带 `.git` 的祖先
        root = next(
            (ancestor for ancestor in target.parents if gitops.is_worktree_root(ancestor)),
            None,
        )
    if root is None:
        return None
    # T29 + T31：只看"根 → 目标"这一段
    return None if _inside_git_dir(root, target) else root


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


def _repo_path_is_gone(repo: Repo) -> bool:
    """绑定的仓库路径是否已经不能用了（S5 D7 #4）：目录被删，或目录还在但 `.git` 被删。

    **这一步必须先于 ref 解析**：路径没了时 `rev-parse` 同样失败，不先探测就会把
    "仓库没了"报成"ref 不存在"——用户会去改 ref，但真正的问题是仓库被删 / 搬走了。

    判据沿用 `_bindable_root` 的前两条：目录在 **且**（`.git` 存在 **或** git 说是裸仓库）。
    - 不用 `git rev-parse` 判定：它会向父目录 discover，父目录恰好是仓库时会误判"还活着"；
    - `.git` 被删时 `bare_root` 会在非仓库目录里跑一次 git（非零 / `false`）→ 判死；
    - 裸仓库本来就没有 `.git`，所以必须留 `bare_root` 这一条，否则会误杀已绑定的裸仓库。
    """
    path = Path(repo.path)
    if not path.is_dir():
        return True
    if gitops.is_worktree_root(path):
        return False
    return gitops.bare_root(path) is None


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
                # S3.5 D1：**只**用路径算，origin 不参与——否则用户加个 remote 仓库就换了身份
                repo_id=repo_id(str(root)),
                path=str(root),
                origin_url=origin,
                default_branch=gitops.default_branch(root),
                added_at=time.time(),
                # S3.5 D2：origin 的归一化哈希降级成**识别字段**（"是不是同一个远端"），
                # 不参与主键；无 origin 时是 None。
                origin_hash=origin_hash(origin),
            )
        except gitops.GitUnavailableError:
            # D5：git 不存在**不崩内核**，回 503 类码；S19 的 doctor 负责告诉用户怎么装
            raise _fail(ADAPTER_UNAVAILABLE, MSG_ADAPTER_UNAVAILABLE) from None
        except gitops.GitTimeoutError:
            raise _fail(frames.INTERNAL, MSG_GIT_TIMEOUT) from None
        except gitops.GitOutputUndecodableError:
            # S5 D3.2 / U15 结案：HEAD 指向非 UTF-8 分支时 symbolic-ref 解不出来。
            # 严格解码 + NOT_SUPPORTED，绝不把 mangled 名字当默认分支落库 / 回传。
            raise _fail(NOT_SUPPORTED, MSG_NOT_SUPPORTED_REFNAME) from None
        # D6 的 NOT_A_REPO 在 try 里抛：不要被上面的 except 顺手吞掉——FrameError 不是
        # 它们的子类，这里显式说明一次，免得后来者为了"统一处理"而改成 `except Exception`。

        return _split(self._store.upsert(repo))

    def _require_repo(self, params: dict[str, object]) -> Repo:
        """取出并校验 `repoId`，返回**已绑定**的 `Repo`（D5 前两行的落点）。

        `repoId` 缺失 / 不是非空字符串 → `INVALID_PARAMS`；查不到 → `REPO_NOT_BOUND`。
        返回 `Repo` 而不是 `dict`：调用方要拿 `repo.path` 当**允许根**（D4 后半句
        "绑定之后的所有操作必须以已绑定的 path 为允许根"），这样"允许根"只有一个来源。
        """
        value = params.get("repoId")
        if not isinstance(value, str) or not value:
            raise _fail(frames.INVALID_PARAMS, MSG_REPO_ID_INVALID)
        repo = self._store.get(value)
        if repo is None:
            raise _fail(REPO_NOT_BOUND, MSG_REPO_NOT_BOUND)
        return repo

    def get(self, params: dict[str, object]) -> dict[str, object]:
        """按 `repoId` 取一条（S4 起 `branches` 与它共用 `_require_repo`）。"""
        return _split(self._require_repo(params))

    def branches(self, params: dict[str, object]) -> BranchPayloads:
        """`repo.branches`：按 `repoId` 列出已绑定仓库的**本地**分支（S4）。

        D4 的"允许根"落点：`root` 只能是**库里那条记录的 `path`**，不接受任何新路径参数
        （07 §2.2：参数只有 `repoId`）。D4 的"截 8 位是边界格式化"也落在这里——gitops 层
        给完整 40 位，帧里只给 8 位。

        D5 的错误码映射（**一个码都不新增**）：

        | 情形 | 码 |
        |---|---|
        | `repoId` 缺失 / 不是非空字符串 | `INVALID_PARAMS` |
        | `repoId` 查不到 | `REPO_NOT_BOUND` |
        | 绑定的路径已不是仓库（目录 / `.git` 被删） | `REPO_NOT_BOUND` |
        | `git` 不存在 | `ADAPTER_UNAVAILABLE` |
        | `git` 超时 | `INTERNAL`（同 S3 D6，S19 的 doctor 再细化） |
        | 空仓库（0 提交） | **成功**，`data: []` |

        "路径已不是仓库"这条码后的文案是「仓库路径已不存在」：`REPO_NOT_BOUND` 而不是
        `INTERNAL` 的理由见 07 §1.3 与 D5——把用户删了仓库这种正常处境报成"需报障"是错的。

        载荷四字段（D1，不多不少）：`name` / `sha`（前 8 位）/ `isCurrent` / `upstream`。
        顺序沿用 git 的 refname 字典序，**不**把当前分支提到最前（D1：稳定顺序才能被断言）。
        """
        repo = self._require_repo(params)
        try:
            found = gitops.list_branches(Path(repo.path))
        except gitops.GitUnavailableError:
            raise _fail(ADAPTER_UNAVAILABLE, MSG_ADAPTER_UNAVAILABLE) from None
        except gitops.GitTimeoutError:
            raise _fail(frames.INTERNAL, MSG_GIT_TIMEOUT) from None
        except gitops.GitOutputUndecodableError:
            # S5 D3.2 / U15 结案：refname 不是合法 UTF-8 → 稳定的 NOT_SUPPORTED + 人话，
            # 不再是"未捕获 UnicodeDecodeError → INTERNAL（请查看终端）"。
            raise _fail(NOT_SUPPORTED, MSG_NOT_SUPPORTED_REFNAME) from None
        if found is None:
            # D5：路径被删 / .git 被删是**正常的用户处境**，不是"未预期错误需报障"
            raise _fail(REPO_NOT_BOUND, MSG_REPO_PATH_MISSING)
        return [
            {
                "name": branch.name,
                "sha": branch.sha[:8],  # D1：8 位小写 hex；完整 40 位留在 gitops 层（D4）
                "isCurrent": branch.is_current,
                "upstream": branch.upstream,  # None 原样出去，不编造空串
            }
            for branch in found
        ]

    def diff(self, params: dict[str, object]) -> dict[str, object]:
        """`repo.diff`：两个 ref 的三点差异（S5 D1–D8），返回 `{stat, patch, truncated}`。

        **参数形状 → 资源存在 → ref → 语义**（D7 的顺序就是契约）：

        | # | 情形 | 码 |
        |---|---|---|
        | 1 | `repoId` 缺失 / 非字符串 / 空 | `INVALID_PARAMS` |
        | 2 | `repoId` 查不到 | `REPO_NOT_BOUND` |
        | 3 | `base`/`head` 形状不合法（D3.1 六条） | `INVALID_PARAMS` |
        | 4 | 仓库路径已不存在（目录或 `.git` 被删） | `REPO_NOT_BOUND`（**先于 ref 判**） |
        | 5 | ref 解析不到（不存在 / 不是 commit） | `BAD_REF` |
        | 6 | 三点没有共同祖先 | `BAD_REF`（文案不同，码不新增） |
        | 7 | git 不存在 | `ADAPTER_UNAVAILABLE` |
        | 8 | git 超时 | `INTERNAL`（同 S3 D6，S19 的 doctor 再细化） |
        | 9 | 输出含非 UTF-8 的 ref 名 | `NOT_SUPPORTED`（D3.2） |
        | 10 | 成功（含空差异） | `ok: true` |

        三点 `base...head`（D1）是"这个分支改了什么"的语义：取**共同祖先 → head**，
        不把 base 在分支点之后自己的提交算成本次差异。解析后只用两个 object id 跑
        stat / patch（D2），所以统计与 patch 描述的是同一对 commit。

        `stat` **不受截断影响**（D6）：统计描述完整差异；`truncated` 只看 patch，
        `patch` 按**字符**切到 `maxChars`（允许切在行中间，不做行对齐修正）。
        空差异（base 与 head 同一 commit）是**成功**：
        `stat 0/0/0`、`patch: ""`、`truncated: false`。
        """
        repo = self._require_repo(params)  # #1 / #2
        base = _require_ref(params, "base")  # #3
        head = _require_ref(params, "head")
        max_chars = _require_max_chars(params)  # D6：越界报错，不夹紧
        root = Path(repo.path)
        try:
            if _repo_path_is_gone(repo):  # #4：不许被 ref 错误盖住
                raise _fail(REPO_NOT_BOUND, MSG_REPO_PATH_MISSING)
            base_sha = gitops.resolve_commit(root, base)  # #5
            head_sha = gitops.resolve_commit(root, head)
            if base_sha is None or head_sha is None:
                raise _fail(BAD_REF, MSG_BAD_REF)
            stat = gitops.diff_stat(root, base_sha, head_sha)  # #6
            if stat is None:
                raise _fail(BAD_REF, MSG_NO_MERGE_BASE)
            patch = gitops.diff_patch(root, base_sha, head_sha)
        except gitops.GitUnavailableError:  # #7
            raise _fail(ADAPTER_UNAVAILABLE, MSG_ADAPTER_UNAVAILABLE) from None
        except gitops.GitTimeoutError:  # #8
            raise _fail(frames.INTERNAL, MSG_GIT_TIMEOUT) from None
        except gitops.GitOutputUndecodableError:  # #9
            raise _fail(NOT_SUPPORTED, MSG_NOT_SUPPORTED_REFNAME) from None
        if patch is None:  # 同一对 commit：stat 答了 patch 答不出只能是"问不出来"
            raise _fail(BAD_REF, MSG_NO_MERGE_BASE)
        return {
            "stat": {
                "filesChanged": stat.files_changed,
                "insertions": stat.insertions,
                "deletions": stat.deletions,
            },
            "patch": patch[:max_chars],
            "truncated": len(patch) > max_chars,
        }
