"""`Repo` 实体与 id 派生：**纯函数，无 IO**（06-data-model §2.1）。

为什么单独成一模块而不是塞进 `api/repository.py`：这里每个函数都是**被断言过契约**的东西
（S3.5 D1：`repo_id` 只由路径决定、长度 12；`origin_hash` 对 https / ssh 两种写法相同），
拿 `git` 或 DB 才能测它们会让这些不变量变成集成测试的副产物。这里所有函数都只吃字符串。

`repo_id` 的算法照 06 §2.1 + S3.5 **D1**：`sha256(绑定时 resolve 后的绝对路径)[:12]`，
**与 origin 无关**——origin 的哈希降级成 `origin_hash`（D2），只用于识别"同一个远端仓库"。
S3 的算法是"有 origin 用 URL 哈希、没有才用路径"，后果是用户加一个 remote 仓库身份就变了
（`docs/design/08-repo-id-stability.md` §1 的实测）。
"""

from __future__ import annotations

import hashlib
import os
import re
import time
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Final

# 06 §2.1：12 位十六进制（07 §4.1 的示例 `adec6540a725` 就是 12 位）
REPO_ID_LENGTH: Final = 12

# `user@host:path`（scp-like）与 `scheme://[user@]host[:port]/path` 两种写法。
# 前者只认 `host:path` 这一种冒号分隔（避免把 Windows 盘符 `C:\...` 当成 ssh 地址）。
_SCP_LIKE: Final = re.compile(r"^(?:[^@/]+@)?(?P<host>[^:/]+):(?P<path>.+)$")
_URL_LIKE: Final = re.compile(r"^(?P<scheme>[A-Za-z][A-Za-z0-9+.-]*)://(?P<rest>.+)$")


@dataclass(frozen=True, slots=True)
class Repo:
    """一个已绑定的本地仓库（06 §2.1 的 Repo 实体）。

    字段名用 `snake_case`（落盘与内部统一），帧层在 `api/repository.py` 的边界上转成
    `camelCase`（07 §4.2：转换只在边界发生，内部不混用）。
    """

    repo_id: str
    path: str  # 已 resolve 的绝对路径，且**一定是仓库根**（D2）
    origin_url: str | None
    default_branch: str | None
    added_at: float
    # S3.5 D2：归一化 origin 的哈希，**只用于识别"是不是同一个远端仓库"**，不参与主键/外键。
    # 默认 `None` 是为了让不关心它的调用方（测试、将来的重建路径）不必编一个值。
    origin_hash: str | None = None

    @property
    def name(self) -> str:
        """左栏显示名：根目录名（`/home/u/proj/src` 绑定后显示 `proj`，不是 `src`）。"""
        return Path(self.path).name or self.path


def normalize_origin(url: str) -> str:
    """把 origin URL 归一化成一个**与传输方式无关**的标识（D3 / 06 §2.1 不变量）。

    | 输入 | 归一化结果 |
    |---|---|
    | `https://github.com/o/p.git` | `github.com/o/p` |
    | `git@github.com:o/p.git` | `github.com/o/p` |
    | `ssh://git@github.com:22/o/p.git` | `github.com/o/p` |
    | `git://github.com/o/p` | `github.com/o/p` |
    | `/srv/git/p.git`（本地路径 remote） | 原样（`resolve` 都做不到，别猜） |

    host 统一小写、去掉尾部 `.git` 与尾部 `/`、丢弃 scheme / user / 端口 /
    `ssh://` 默认端口。**不做** URL 解码：解码会让两种来源的差异被放大，而不是被抹平。
    """
    text = url.strip()
    if not text:
        return ""
    match = _URL_LIKE.match(text)
    if match is not None:
        rest = match.group("rest")
        # 去掉 `user@`（凭据不进哈希：D3 要的是同一个仓库，不是同一个用户名）
        rest = rest.split("@", 1)[-1]
        host, _, path = rest.partition("/")
        host = host.split(":", 1)[0].lower()  # 端口不参与：22 与 443 是同一个仓库
    else:
        scp = _SCP_LIKE.match(text)
        if scp is not None and not text.startswith("/"):
            host = scp.group("host").lower()
            path = scp.group("path")
        else:
            return text.rstrip("/")
    path = path.strip("/")
    if path.endswith(".git"):
        path = path[: -len(".git")]
    return f"{host}/{path}" if path else host


def repo_id_for_origin(origin_url: str) -> str:
    """`sha256(归一化 origin URL)` 的前 12 个 hex——**只给 `origin_hash` 用**（S3.5 D2）。

    名字里的 `repo_id` 是历史遗留（S3 时代它真的算 `repo_id`）；保留它是因为 S3 的测试与
    codemap 都按这个名字索引。**不要**再用它给 `Repo.repo_id` 赋值：那会退回 D1 之前的缺陷。
    """
    digest = hashlib.sha256(normalize_origin(origin_url).encode("utf-8")).hexdigest()
    return digest[:REPO_ID_LENGTH]


def repo_id_for_path(path: str) -> str:
    """`repo_id` 的唯一算法（S3.5 D1）：`sha256(绝对路径)` 的前 12 个 hex。

    **一次性计算、写后永不更新**（D1）：`RepoStore.upsert` 不会改写已存在行的 `repo_id`。
    所以加/改/删 remote、切分支、fetch 都不影响它；只有仓库**被移动到新路径**时才会变——
    那比"加 remote"罕见得多，且移动仓库本来就需要人确认（S19 的 doctor）。
    """
    digest = hashlib.sha256(path.encode("utf-8")).hexdigest()
    return digest[:REPO_ID_LENGTH]


def origin_hash(origin_url: str | None) -> str | None:
    """`origin_url` → `sha256(归一化 URL)[:12]`；没有 origin 就是 `None`（S3.5 D2）。

    **不再是 `repo_id`**（S3 时代它是）：它只回答"两个绑定是不是同一个远端仓库"——将来做
    去重提示时会用到——**不参与任何主键或外键**。所以它**可变**：改 remote 它就变，这没风险，
    因为没人按它查行。`None` 输入给 `None` 而不是 `sha256("")` 的哈希：凭空创造一个"空 origin"
    的身份会让两个无 origin 的仓库看起来是同一个远端。
    """
    if not origin_url:
        return None
    return repo_id_for_origin(origin_url)


def repo_id(path: str) -> str:
    """`repo_id = sha256(绝对路径)[:12]`（S3.5 D1：**只看路径**，不看 origin）。

    参数里**没有** origin：S3 的签名是 `repo_id(path, origin_url)`，把 origin 传进来就等于
    允许"origin 变了 id 就变"。调用方（`api/repository.py:bind`）拿到 origin 后单独算
    `origin_hash`，那是另一个字段（D2）。
    """
    return repo_id_for_path(path)


# ---------------------------------------------------------------------------
# Bot（S6，06 §2.2）：实体 + 名字白名单 + 复合主键。**纯函数，无 IO**（与 Repo 同一纪律）。
# ---------------------------------------------------------------------------

# 06 §2.2 白名单：首字母小写、其余小写字母/数字/连字符，总长 1..32。
BOT_NAME_RE: Final = re.compile(r"^[a-z][a-z0-9-]{0,31}$")


class Permission(StrEnum):
    """人可以授予一个 Bot 的**四个**级别（S8 D2；01 §4 / 05 §2.2）。

    **这四个才构成阶梯**：`L0 < L1 < L2 < L2+`（`permission_covers` 回答"持有某级是否够用"）。
    `L3`（跨仓库 PR）与 `L4`（合并）**不在**这一维上——01 §4 开头就是这么说的：把「合并」与
    「跨仓库 PR」排成同一根尺子，会让「能往自己仓库开 PR」看起来像「能往别人仓库开 PR」的
    前一步。所以它们不是本枚举的成员，也不参与任何大小比较（`permission_covers` 对它们答
    `False`，失败即收紧）。

    为什么是 `StrEnum`：落库、上网关帧、进页面徽标的都是**字符串**（`"L2+"`）。用字符串字面量
    意味着 `"l1"` / `"L2 +"` 这类拼写错误在运行时才被发现；枚举让"认识的级别"只有一份清单。
    """

    L0 = "L0"
    L1 = "L1"
    L2 = "L2"
    L2_PLUS = "L2+"

    @property
    def rank(self) -> int:
        """在**v1 阶梯**上的位置（0..3）。只对这四个成员有意义。"""
        return _PERMISSION_LADDER.index(self.value)


# 阶梯顺序的唯一事实源：`rank` 与 `permission_covers` 都读它，别在别处再排一次。
_PERMISSION_LADDER: Final = ("L0", "L1", "L2", "L2+")

# 06 §2.2 / 05 §2.3：这两个级别**认识**但**不可写入**（`NOT_SUPPORTED`）。它们不是"拼错的字符串"
# ——拼错的是 `INVALID_PARAMS`，这两个是"你说得清，但第一版不做"。
UNASSIGNABLE_PERMISSIONS: Final[frozenset[str]] = frozenset({"L3", "L4"})

# 新建 Bot 的默认级别（05 §2.2 的"新建 Bot"行；01 §4 的 L0 备注是过时写法，S8 D2 有四个来源）。
BOT_PERMISSION_DEFAULT: Final = Permission.L1


@dataclass(frozen=True, slots=True)
class Bot:
    """一个仓库下的 Bot（06 §2.2）。`bot_id` 是复合主键 `{repo_id}/{name}`。

    `charter_rev` 列先建但 **M1 不维护**（D5：charter 正文的事实源是文件，帧载荷里的
    `charterRev` 由 `api/bots.py` 现场按内容算）。落库时写**创建那一刻**的值，之后允许过期
    （S8 D7：U17 的第二半仍在 S22 到期——改 charter 的写路径是 `memory.charter.set`）。
    """

    bot_id: str
    repo_id: str
    name: str
    permission: str
    agent_kind: str | None
    charter_rev: int
    created_at: float


def bot_name_ok(name: str) -> bool:
    """名字白名单（06 §2.2）。路径安全的第一道锁：`storage.paths` 进路径前必须过它。"""
    return BOT_NAME_RE.fullmatch(name) is not None


def bot_id(repo_id: str, name: str) -> str:
    """复合主键 `{repo_id}/{name}`（06 §2.2）——**唯一的拼接处**，别处不要手写格式。"""
    return f"{repo_id}/{name}"


def permission_parse(value: object) -> Permission | None:
    """入站权限值 → `Permission`；不是那四个之一就 `None`（**纯判定，不抛、不拼文案**）。

    `L3` / `L4` 也走 `None` 这条路（它们不是本枚举的成员）：调用方要先看
    `UNASSIGNABLE_PERMISSIONS` 再调这里，才能把"第一版不支持"（`NOT_SUPPORTED`）与"根本没这个
    级别"（`INVALID_PARAMS`）分开。两个判断分开放，是为了让那个区分是**显式**的，而不是靠
    `None` 的两义性。
    """
    if not isinstance(value, str):
        return None
    try:
        return Permission(value)
    except ValueError:  # 含 lone surrogate：`Permission(...)` 只会 ValueError
        return None


def permission_covers(held: object, needed: object) -> bool:
    """持有 `held` 是否满足 `needed`（S8 D2 的阶梯：`L0 < L1 < L2 < L2+`）。

    **失败即收紧**（05 §2.2）：任何一端解析不出来——`None`、`"L3"`、`"l1"`、`42`——都答 `False`。
    `L3`/`L4` 在另一根轴（01 §4），所以"持 `L2+`"不构成"能对上游开 PR"，这里也不假装有序。
    """
    left = permission_parse(held)
    right = permission_parse(needed)
    if left is None or right is None:
        return False
    return left.rank >= right.rank


# ---------------------------------------------------------------------------
# 入站 ref 名的形状（S5 D3.1）：纯判定，无 IO。
#
# 放在 domain 而不是 `api/repository.py`：S5 的 `repo.diff`（base / head）与 S7 的
# `worktree.acquire`（baseRef）都要用它，而**同层模块之间不互相 import**（02-layers §3）。
# 它只吃字符串、只回答"形状对不对"，正好是这一层的形状。
# ---------------------------------------------------------------------------

# git 的 refname 本身远短于这个数；这是入站形状检查的一部分，不是 git 的限制。
REF_NAME_MAX_LENGTH: Final = 200


def ref_name_ok(value: object) -> bool:
    """一个**入站 ref 名**的形状是否合法（S5 D3.1 的六条）——纯判定，**不抛、不拼文案**。

    六条同时满足才算合法：`str`、非空、长度 ≤ `REF_NAME_MAX_LENGTH`、不含控制字符
    （`< 0x20` 或 `0x7F`）、`encode("utf-8")` 不抛、`strip()` 后非空、不以 `-` 开头。

    **lone surrogate 必须挡掉**（实测，不是推测）：JSON 允许 `"\\udcff"` 这种转义，
    `json.loads` 会得到一个 lone surrogate 的 `str`；Python 给子进程编码 argv 时用
    `surrogateescape`，于是它在**字节层**变成原始 `0xFF`——正好是 git 里那个非 UTF-8
    ref 名的字节，`rev-parse --verify` 实测**真的解析成功**。不挡的话，`repo.branches`
    拒绝展示的名字，客户端能用 JSON 转义"洗"回 git：一侧拒绝、另一侧接受 = 漏洞。
    `encode` 是唯一可靠的判据（`isinstance(s, str)` / `len()` 都看不出 lone surrogate）。

    文案由调用方给（`repo.diff` 说 base/head，`worktree.acquire` 说 baseRef），**码**不变。
    """
    if not isinstance(value, str):
        return False
    if not value or len(value) > REF_NAME_MAX_LENGTH or value.startswith("-") or not value.strip():
        return False
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in value):
        return False
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:  # lone surrogate：只有编码才会炸
        return False
    return True


# ---------------------------------------------------------------------------
# Task（S10，06 §2.3 / §3.1）：实体 + 状态机 + ULID。**纯函数，无 IO**（与 Repo/Bot 同一纪律）。
# ---------------------------------------------------------------------------

# prompt 上限（06 §2.3）。超限是**入参**问题：`INVALID_PARAMS`，不是截断——截断会让 Agent
# 拿到一句它没见过的半截指令，比拒绝危险得多。
PROMPT_MAX_BYTES: Final = 32 * 1024

# 06 §3.1 的允许迁移表（唯一事实源）。**键是 from，值是 to 的集合**；
# `queued → succeeded` 与 `interrupted → succeeded` 不在表里，所以结构上不可能发生。
TASK_TRANSITIONS: Final[dict[str, frozenset[str]]] = {
    "queued": frozenset({"running", "cancelled", "interrupted"}),
    "running": frozenset({"succeeded", "failed", "cancelled", "interrupted"}),
}

# 终态集合：到达后 `transition` 一律拒绝（连"再迁移一次"都不行）。
TASK_TERMINAL: Final[frozenset[str]] = frozenset(
    {"succeeded", "failed", "cancelled", "interrupted"}
)

# 活跃态（BUSY 的判据，D5）：queued 也算"在跑"——worker 尚未接手时再来一刀会争抢工作区。
TASK_ACTIVE: Final[frozenset[str]] = frozenset({"queued", "running"})

# 触发源（06 §2.3）。S10 只产生 `manual`；另两个值先登记，防止调用方拼错时静默入库。
TASK_TRIGGERS: Final[frozenset[str]] = frozenset({"manual", "dialogue", "local_event"})

_ULID_ENCODING: Final = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"  # Crockford base32，无 I/L/O/U


def ulid(now_ms: int | None = None, randomness: bytes | None = None) -> str:
    """26 字符 ULID（D14）：48 位毫秒时间 + 80 位随机，Crockford base32。

    06 §2.3 要求 `task_id` 是 ULID（可排序、无碰撞）。标准库没有它，而 S3 的
    "不新增依赖"仍然有效，所以这里手写生成。`now_ms` / `randomness` 是**测试**注入口
    （确定性断言用）；生产路径两者都传 `None`。

    可排序性来自首段是**大端**时间戳：同一毫秒内的碰撞概率 2^-80，跨毫秒单调——
    不做同毫秒单调递增（那需要进程内计数器，跨进程重启会回退，反而破坏排序假设）。
    """
    if now_ms is None:
        now_ms = int(time.time() * 1000)
    if randomness is None:
        randomness = os.urandom(10)
    if not 0 <= now_ms < (1 << 48):
        raise ValueError(f"时间戳超出 48 位：{now_ms}")
    if len(randomness) != 10:
        raise ValueError("随机段必须是 10 字节")
    value = (now_ms << 80) | int.from_bytes(randomness, "big")
    chars = []
    for shift in range(130 - 5, -1, -5):  # 130 位有效（48+80+2 补齐），从高位到低位
        chars.append(_ULID_ENCODING[(value >> shift) & 0x1F])
    return "".join(chars)


def prompt_ok(value: object) -> bool:
    """派活指令的形状（D5 入参侧）：非空字符串，UTF-8 编码 ≤ 32 KiB（06 §2.3）。

    **按字节**判而不是按字符：JSON 传输是 UTF-8 文本，落库与转发都以字节计。
    lone surrogate 在这里就挡掉（`encode` 会抛）——它进不了 ACP 的 JSON-RPC 帧。
    """
    if not isinstance(value, str) or not value.strip():
        return False
    try:
        return len(value.encode("utf-8")) <= PROMPT_MAX_BYTES
    except UnicodeEncodeError:
        return False


@dataclass(frozen=True, slots=True)
class Task:
    """一次派活（06 §2.3）。字段名 `snake_case`（落盘统一），帧层在边界转 `camelCase`。

    `candidate_id` 本刀恒为 `None`（S11）；`trigger_key` 恒为 `None`（幂等去重是
    `dialogue` / `local_event` 触发源的事）。列先建好——为 S11 改一次 schema 不如一次建对。
    """

    task_id: str
    bot_id: str
    trigger: str
    trigger_key: str | None
    prompt: str
    base_ref: str | None
    state: str
    candidate_id: str | None
    error_code: str | None
    created_at: float
    started_at: float | None
    ended_at: float | None


def task_transition_ok(current: object, target: object) -> bool:
    """一次迁移是否合法（06 §3.1 的表 + 终态封死）——纯判定，**不抛、不拼文案**。

    三个方向都拒绝：不认识的状态（含大小写漂移）、终态再迁移、表里没有的边。
    06 §3.1 的两条**禁止**（`queued → succeeded` / `interrupted → succeeded`）不写在这里，
    写在这里就等于复述——它们由 `TASK_TRANSITIONS` 的**缺失**表达。
    """
    if not isinstance(current, str) or not isinstance(target, str):
        return False
    if current in TASK_TERMINAL:
        return False
    allowed = TASK_TRANSITIONS.get(current)
    return allowed is not None and target in allowed


# ---------------------------------------------------------------------------
# Candidate（S11，06 §2.4 / §3.2）：实体 + 分支名派生。**纯函数，无 IO**。
# ---------------------------------------------------------------------------

CANDIDATE_ID_PREFIX: Final = "cand_"
# 06 §3.2 的审核状态。S11 只生产 local_only，其余先登记（S13 才有写路径）。
REVIEW_STATES: Final[frozenset[str]] = frozenset(
    {"local_only", "pushed", "pr_open", "approved", "rejected"}
)
# 06 §3.2 / S13 D1：审核迁移的**唯一事实源**（纯函数，无 IO）。
# `approved` / `rejected` 是终态；`local_only → approved` 在 MVP 合法（不要求先 push/PR）。
REVIEW_TRANSITIONS: Final[dict[str, frozenset[str]]] = {
    "local_only": frozenset({"pushed", "approved", "rejected"}),
    "pushed": frozenset({"pr_open", "approved", "rejected"}),
    "pr_open": frozenset({"approved", "rejected"}),
    "approved": frozenset(),
    "rejected": frozenset(),
}
REASON_MAX_BYTES: Final = 2000  # S13 D2：拒绝理由的 UTF-8 字节上限


def review_transition_ok(current: object, target: object) -> bool:
    """`(current, target)` 是否是 06 §3.2 允许的审核迁移——纯判定，不抛。

    未知状态一律 `False`（fail closed）：坏数据不能靠"没查到规则就放行"绕过状态机。
    """
    if not isinstance(current, str) or not isinstance(target, str):
        return False
    allowed = REVIEW_TRANSITIONS.get(current)
    return allowed is not None and target in allowed


_TASK_SHORT_LENGTH: Final = 8
# 06 §2.4 的命名空间；gitops/worktrees.BRANCH_PREFIX 是同一常量的另一个副本
# （分层表禁止 gitops → domain 的 import，不能在这里共享一个事实源）。
_BRANCH_PREFIX: Final = "bot"
_BRANCH_SAFE: Final = re.compile(r"[^a-z0-9-]")


def candidate_branch(bot_name: object, task_id: object) -> str:
    """`bot/<bot_name>/<task_id 短>`（06 §2.4 / S11 D1）——**纯**，不碰 git。

    Bot 名已有 `bot_name_ok` 白名单（写入路径强制），这里仍把任何非法字符换成 `-`：
    坏数据可能来自旧库或被手工改过的库，分支名不能因此变成 `bot/../x` 或带空格。

    task_id 取**随机尾部 8 位**，不是前缀：ULID 的前 40 位全是时间戳，同一毫秒派出的
    两个活前缀必然相同（实测撞车：`bot/builder/01m8e3ey` 被第二个任务判为"分支已存在"）。
    尾部 8 位是 80 位随机段的一部分，同毫秒碰撞概率 2^-40；Crockford 字符本来就
    refname-safe。这是规格 D1 的实测返工。
    """
    name = bot_name if isinstance(bot_name, str) else ""
    tid = task_id if isinstance(task_id, str) else ""
    safe_name = _BRANCH_SAFE.sub("-", name.lower()) or "unknown"
    safe_task = _BRANCH_SAFE.sub("-", tid.lower())[-_TASK_SHORT_LENGTH:] or "unknown"
    return f"{_BRANCH_PREFIX}/{safe_name}/{safe_task}"


@dataclass(frozen=True, slots=True)
class Candidate:
    """一条可审查的候选（06 §2.4）。落盘 `snake_case`，帧层在边界转 `camelCase`。

    `base_sha` 是**任务开始时的工作区 HEAD**（S11 D2，不是 task.base_ref），
    `head_sha` 是把改动固化后的 HEAD；两者都是 40 位 hex。`remote` / `target` 在
    `local_only` 时为 `None`（S13 的 push / PR 才有值）。
    """

    candidate_id: str
    task_id: str
    branch: str
    base_sha: str
    head_sha: str
    files_changed: int
    insertions: int
    deletions: int
    review_state: str
    remote: str | None
    target: str | None
    created_at: float
    # S13 D2：拒绝理由；v6 加列，缺省 None 让 S11 的构造点不必改。
    reject_reason: str | None = None


@dataclass(frozen=True, slots=True)
class WorktreeLease:
    """一条工作区租约（06 §2.5，S7 裁决到期 S10，与 Task 一起建）。

    `path` 是 `worktree_dir()` 的产出（已 resolve、在 worktrees 根之内断言过）——本实体
    不再校验形状，那是 `storage.paths` 的职责。`released_at IS NULL` 即活跃租约；
    数据库层用部分唯一索引保证每 Bot 至多一条。
    """

    lease_id: str
    bot_id: str
    task_id: str
    path: str
    created_at: float
    released_at: float | None
