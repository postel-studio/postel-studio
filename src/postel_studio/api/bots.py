"""`bot.list` / `bot.create` / `bot.set_permission` / `memory.charter.get` 的业务编排
（S6 D1–D6，S8 D1–D7）。

与 `api/repository.py` 同一层、同一纪律：**不碰 fastapi**，只吃 `dict`、返回 `dict`/`list[dict]`，
错误一律翻成 07 §1.3 的码。

四处裁决的落点：

- **S8 D7 起 `bot.list` 是纯读**：S6 的惰性播种（一个读方法会落一行）已删除——创建是**人的动作**
  （`create`），一个 Bot 都没有的仓库就回空数组（U17 的第一半在这里结案）。
- **S8 D6 先判后写**：`create` 的前五步（repoId / 名字 / 权限 / agentKind / 重名）都不碰磁盘，
  所以被拒绝的请求**什么都没留下**；第六步建记忆空间，第七步落行。
- **D5 文件是事实源**：`charterRev` 现场按 charter 内容算、摘要按五条规则提取；DB 的
  `charter_rev` 列只在创建时写一次（U17 的第二半到期 S22 的 `memory.charter.set`）。
- **D9 路径显示**：`memoryPath` 给**绝对路径**原样，不伪装成 `~`——状态根可被 `POSTEL_HOME`
  覆盖，把临时路径显示成 `~/.postel/...` 是假话。
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
import time
from typing import Final

from postel_studio.api import frames
from postel_studio.domain.models import (
    BOT_PERMISSION_DEFAULT,
    UNASSIGNABLE_PERMISSIONS,
    Bot,
    Permission,
    Repo,
    bot_id,
    bot_name_ok,
    permission_parse,
)
from postel_studio.memory import inject as context_inject
from postel_studio.storage.db import BotStore, RepoStore
from postel_studio.storage.paths import bot_memory_dir, ensure_memory_space, read_charter

# 07 §1.3 已登记的码。`REPO_NOT_BOUND` 在 frames 里没有定义（那是纯帧层）。
INVALID_PARAMS: Final = frames.INVALID_PARAMS
INTERNAL: Final = frames.INTERNAL
NOT_SUPPORTED: Final = "NOT_SUPPORTED"
REPO_NOT_BOUND: Final = "REPO_NOT_BOUND"
# S8 新增（07 §1.3 同表）：创建撞名。为什么不是 INVALID_PARAMS——页面按**码**选文案，
# 把一个合法的名字说成"名字不合法"就是假话（S8 D5）。
ALREADY_EXISTS: Final = "ALREADY_EXISTS"

# 错误文案：**可读、面向人、不含任何用户输入**（05 §4.2）。改文案不需要改码。
MSG_REPO_ID_INVALID: Final = "repoId 参数缺失或不是字符串"
MSG_REPO_NOT_BOUND: Final = "仓库未绑定"
MSG_BOT_ID_INVALID: Final = "botId 参数缺失或不是字符串"
MSG_BOT_NOT_FOUND: Final = "找不到这个 Bot"
MSG_MEMORY_FAILED: Final = "记忆目录或 charter 读写失败"
# 名字被拒时把**规则**说清楚：人不该为了一条白名单去翻文档。规则是固定的，不回显输入。
MSG_NAME_INVALID: Final = "名字不合法：1–32 位，小写字母开头，只能用小写字母、数字和连接符"
MSG_PERMISSION_INVALID: Final = "权限级别不合法"
MSG_PERMISSION_UNSUPPORTED: Final = "第一版不支持这个权限级别（L3 / L4）"
MSG_AGENT_KIND_UNSUPPORTED: Final = "第一版还不能指定 Agent 通道（S9 才会验证）"
MSG_AGENT_KIND_INVALID: Final = "agentKind 只能缺省或为 null"
MSG_ALREADY_EXISTS: Final = "这个仓库已经有叫这个名字的 Bot"

# D5 第 5 条：没有正文时的摘要——不是空串，也不编造职责。
DEFAULT_SUMMARY: Final = "尚未填写职责"
SUMMARY_MAX_CHARS: Final = 80

# `BotService` 里有个方法叫 `list`，类作用域的 `list[...]` 会被 mypy 解析成那个方法
# （`valid-type`）。所以列表载荷的类型用模块级别名（与 `repository.py` 同一处教训）。
type BotPayloads = list[dict[str, object]]

_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)  # D5 第 1 条：整段 HTML 注释
_EMPHASIS = re.compile(r"\*\*|[*_`]")  # D5 第 3 条：行内强调标记


def _fail(code: str, message: str) -> frames.FrameError:
    """帧层错误（`FrameError` 由网关原样翻成 `ok:false` 帧）。"""
    return frames.FrameError(code, message)


def charter_rev(content: str) -> int:
    """D5：`int(sha256(content.encode("utf-8")).hexdigest()[:8], 16)`——随内容变、无状态。"""
    return int(hashlib.sha256(content.encode("utf-8")).hexdigest()[:8], 16)


def charter_summary(content: str) -> str:
    """D5 的摘要五条（顺序即契约，`tests/test_bot_charter.py` 逐条覆盖）。

    跳过标题行 / 空行 / HTML 注释 → 取第一个正文段落（多行拼成一段）→ 去强调标记 →
    超过 80 字符截断加 `…` → 没有正文回 `DEFAULT_SUMMARY`。
    """
    body: list[str] = []
    for line in _COMMENT.sub("", content).splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            if body:  # 段落结束：下一个空行或标题行
                break
            continue
        body.append(stripped)
    if not body:
        return DEFAULT_SUMMARY
    summary = _EMPHASIS.sub("", " ".join(body))
    if len(summary) <= SUMMARY_MAX_CHARS:
        return summary
    return summary[:SUMMARY_MAX_CHARS] + "…"


class BotService:
    """S6 的两个读方法 + S8 的两个写方法。构造时接收两个 store（依赖注入，测试用临时库）。

    `RepoStore` 只用来回答「repoId 查不查得到」（D6 的 `REPO_NOT_BOUND`）；Bot 自己的表在
    `BotStore`。两个 store 各开一条 SQLite 连接，与 `app.py` 里既有的 `RepoStore` 同款。
    """

    def __init__(self, store: BotStore, repos: RepoStore) -> None:
        self._store = store
        self._repos = repos

    def list(self, params: dict[str, object]) -> BotPayloads:
        """`bot.list`：列出仓库下的 Bot。**纯读**——一个都没有就回 `[]`（S8 D7）。"""
        repo = self._require_repo(params)
        return [self._split(bot) for bot in self._store.list_for_repo(repo.repo_id)]

    def create(self, params: dict[str, object]) -> dict[str, object]:
        """`bot.create`：判完再写（S8 D6 的七步，前五步不碰磁盘）。"""
        repo = self._require_repo(params)
        name = self._require_name(params)
        permission = self._permission(params, default=BOT_PERMISSION_DEFAULT)
        self._refuse_agent_kind(params)
        composite = bot_id(repo.repo_id, name)
        if self._store.get(composite) is not None:
            raise _fail(ALREADY_EXISTS, MSG_ALREADY_EXISTS)
        try:
            # 缺什么建什么；已有 charter 绝不覆盖（S6 D3）——所以同名重试是安全的。
            ensure_memory_space(repo.repo_id, name)
            content = read_charter(repo.repo_id, name)
        except OSError as exc:  # 权限 / 磁盘满：D6 的 INTERNAL
            raise _fail(INTERNAL, MSG_MEMORY_FAILED) from exc
        try:
            bot = self._store.insert(
                Bot(
                    bot_id=composite,
                    repo_id=repo.repo_id,
                    name=name,
                    permission=str(permission),
                    agent_kind=None,  # D4：S9 之前没有任何通道被验证过
                    charter_rev=charter_rev(content),
                    created_at=time.time(),
                )
            )
        except sqlite3.IntegrityError as exc:
            # 今天只有一种现实成因：两个并发的同名创建都过了第 5 步（各持自己的锁）。
            # 仓库行刚被 `_require_repo` 证明存在，而 `repo.unbind` 还没实现——所以这里不会是
            # 外键错。S22 做 unbind 时必须回来重看这一行（S8 D6）。
            raise _fail(ALREADY_EXISTS, MSG_ALREADY_EXISTS) from exc
        return self._split(bot)

    def set_permission(self, params: dict[str, object]) -> dict[str, object]:
        """`bot.set_permission`：改权限并回载荷（S8 D1/D2）。同值不写库。"""
        bot = self._require_bot(params)
        permission = self._permission(params, default=None)
        if bot.permission == permission:  # 幂等：不制造一次无意义的写
            return self._split(bot)
        updated = self._store.set_permission(bot.bot_id, str(permission))
        if updated is None:  # 竞态：刚读到、现在没了。07 §1.3 没有更贴的码（与 D6 一致）
            raise _fail(INVALID_PARAMS, MSG_BOT_NOT_FOUND)
        return self._split(updated)

    def charter_get(self, params: dict[str, object]) -> dict[str, object]:
        """`memory.charter.get`：`{content, rev}`，正文与 rev 都来自 charter 文件（D5）。"""
        bot = self._require_bot(params)
        try:
            # 缺目录/缺 charter 时自愈（缺什么建什么）；已有 charter 绝不覆盖（D3）。
            ensure_memory_space(bot.repo_id, bot.name)
            content = read_charter(bot.repo_id, bot.name)
        except OSError as exc:  # 权限 / 磁盘满：D6 的 INTERNAL
            raise _fail(INTERNAL, MSG_MEMORY_FAILED) from exc
        return {"content": content, "rev": charter_rev(content)}

    def context_preview(self, params: dict[str, object]) -> dict[str, object]:
        """`memory.context.preview`（S17）：只读来源清单，冲突只标 `conflict` 不抛错。

        S17 D1/D4：AGENTS.md/README.md 从绑定仓库**实时读取**，不写盘；Bot charter 用
        `inject.py` 的只读路径。仓库查不到 → `REPO_NOT_BOUND`（07 §1.3）。
        """
        bot = self._require_bot(params)
        repo = self._repos.get(bot.repo_id)
        if repo is None:
            raise _fail(REPO_NOT_BOUND, MSG_REPO_NOT_BOUND)
        try:
            return context_inject.preview(repo.path, bot.repo_id, bot.name)
        except OSError as exc:  # 磁盘读失败：既有 INTERNAL，不新增码
            raise _fail(INTERNAL, MSG_MEMORY_FAILED) from exc

    # -- 内部 ----------------------------------------------------------------
    def _require_repo(self, params: dict[str, object]) -> Repo:
        """`repoId` 形状 → `INVALID_PARAMS`；查不到 → `REPO_NOT_BOUND`（D6 前两行）。"""
        value = params.get("repoId")
        if not isinstance(value, str) or not value:
            raise _fail(INVALID_PARAMS, MSG_REPO_ID_INVALID)
        repo = self._repos.get(value)
        if repo is None:
            raise _fail(REPO_NOT_BOUND, MSG_REPO_NOT_BOUND)
        return repo

    def _require_bot(self, params: dict[str, object]) -> Bot:
        """`botId` 缺失/非字符串/查不到（含路径穿越企图）一律 `INVALID_PARAMS`（D6）。

        穿越企图不必单独判格式：不合法的 `bot_id` 在库里必然查不到，查不到就拒绝，**一个
        文件都不会碰**。名字进路径前的白名单在 `storage.paths.bot_memory_dir`（D3）。
        """
        value = params.get("botId")
        if not isinstance(value, str) or not value:
            raise _fail(INVALID_PARAMS, MSG_BOT_ID_INVALID)
        bot = self._store.get(value)
        if bot is None:
            raise _fail(INVALID_PARAMS, MSG_BOT_NOT_FOUND)
        return bot

    def _require_name(self, params: dict[str, object]) -> str:
        """新名字的形状（06 §2.2 白名单，D3）。非 `str` 走同一句错误，不抛 TypeError。"""
        value = params.get("name")
        if not isinstance(value, str) or not bot_name_ok(value):
            raise _fail(INVALID_PARAMS, MSG_NAME_INVALID)
        return value

    def _permission(self, params: dict[str, object], *, default: Permission | None) -> Permission:
        """入站 `permission` → `Permission`。三种拒绝各有各的码（D2）：

        | 入站 | 结果 |
        |---|---|
        | 缺字段 / `null` | `default`（创建时是 L1；改权限时没有默认 → `INVALID_PARAMS`） |
        | `L3` / `L4` | `NOT_SUPPORTED`——"你说得清，但第一版不做" |
        | 其它（`L9` / `l1` / `42` / `""`） | `INVALID_PARAMS`——"根本没有这个级别" |
        """
        raw = params.get("permission")
        if raw is None:  # 缺字段与显式 null 同义（07 的 `permission?`）
            if default is None:
                raise _fail(INVALID_PARAMS, MSG_PERMISSION_INVALID)
            return default
        if isinstance(raw, str) and raw in UNASSIGNABLE_PERMISSIONS:
            raise _fail(NOT_SUPPORTED, MSG_PERMISSION_UNSUPPORTED)
        parsed = permission_parse(raw)
        if parsed is None:
            raise _fail(INVALID_PARAMS, MSG_PERMISSION_INVALID)
        return parsed

    def _refuse_agent_kind(self, params: dict[str, object]) -> None:
        """D4：`agentKind` 只收缺省或 `null`。

        给了名字 → `NOT_SUPPORTED`；给了别的类型 → `INVALID_PARAMS`（与 `_permission` 同一条
        分界线：说得清的拒绝，与根本不合法的参数，不是一回事）。
        """
        raw = params.get("agentKind")
        if raw is None:
            return
        if isinstance(raw, str):
            raise _fail(NOT_SUPPORTED, MSG_AGENT_KIND_UNSUPPORTED)
        raise _fail(INVALID_PARAMS, MSG_AGENT_KIND_INVALID)

    def _split(self, bot: Bot) -> dict[str, object]:
        """实体 → 帧载荷（D1：**恰好 8 个字段**，camelCase 转换只在这个边界发生）。"""
        content = self._charter(bot)
        return {
            "botId": bot.bot_id,
            "repoId": bot.repo_id,
            "name": bot.name,
            "permission": bot.permission,
            "agentKind": bot.agent_kind,
            "charterRev": charter_rev(content),
            "charterSummary": charter_summary(content),
            "memoryPath": str(bot_memory_dir(bot.repo_id, bot.name)),
        }

    def _charter(self, bot: Bot) -> str:
        """读 charter；目录被用户删掉时按空正文处理（不编造、不在这里重建目录）。"""
        try:
            return read_charter(bot.repo_id, bot.name)
        except FileNotFoundError:
            return ""
        except OSError as exc:
            raise _fail(INTERNAL, MSG_MEMORY_FAILED) from exc
