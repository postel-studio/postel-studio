"""`bot.list` / `memory.charter.get` 的业务编排（S6 D1–D6）。

与 `api/repository.py` 同一层、同一纪律：**不碰 fastapi**，只吃 `dict`、返回 `dict`/`list[dict]`，
错误一律翻成 07 §1.3 的**既有码**（D6：`botId` 查不到也用 `INVALID_PARAMS`，不新增码）。

三处裁决的落点：

- **D2 惰性播种**：`bot.list` 是「该 repo 一个 Bot 都没有」时**唯一**播种的地方；已有任何
  Bot 绝不新建。代价：一个读方法会落一行（M1 的临时形态，U17 到期 S8）。
- **D5 文件是事实源**：`charterRev` 现场按 charter 内容算、摘要按五条规则提取；DB 的
  `charter_rev` 列 M1 不维护（写路径是 S22 的 `charter.set`）。
- **D9 路径显示**：`memoryPath` 给**绝对路径**原样，不伪装成 `~`——状态根可被
  `POSTEL_HOME` 覆盖，把临时路径显示成 `~/.postel/...` 是假话。
"""

from __future__ import annotations

import hashlib
import re
import time
from typing import Final

from postel_studio.api import frames
from postel_studio.domain.models import BOT_PERMISSION_DEFAULT, Bot, Repo, bot_id
from postel_studio.storage.db import BotStore, RepoStore
from postel_studio.storage.paths import bot_memory_dir, ensure_memory_space, read_charter

# 07 §1.3 已登记的码（**不新增码**）。`REPO_NOT_BOUND` 在 frames 里没有定义（那是纯帧层）。
INVALID_PARAMS: Final = frames.INVALID_PARAMS
INTERNAL: Final = frames.INTERNAL
REPO_NOT_BOUND: Final = "REPO_NOT_BOUND"

# 错误文案：**可读、面向人、不含任何用户输入**（05 §4.2）。改文案不需要改码。
MSG_REPO_ID_INVALID: Final = "repoId 参数缺失或不是字符串"
MSG_REPO_NOT_BOUND: Final = "仓库未绑定"
MSG_BOT_ID_INVALID: Final = "botId 参数缺失或不是字符串"
MSG_BOT_NOT_FOUND: Final = "找不到这个 Bot"
MSG_MEMORY_FAILED: Final = "记忆目录或 charter 读写失败"

# D2 的预置 Bot（M1 没有创建入口，bot.create 是 S8）。
PRESET_BOT_NAME: Final = "builder"
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
    """S6 的两个帧方法。构造时接收两个 store（依赖注入，测试用临时库）。

    `RepoStore` 只用来回答「repoId 查不查得到」（D6 的 `REPO_NOT_BOUND`）；Bot 自己的表在
    `BotStore`。两个 store 各开一条 SQLite 连接，与 `app.py` 里既有的 `RepoStore` 同款。
    """

    def __init__(self, store: BotStore, repos: RepoStore) -> None:
        self._store = store
        self._repos = repos

    def list(self, params: dict[str, object]) -> BotPayloads:
        """`bot.list`：列出仓库下的 Bot；一个都没有时**惰性播种**（D2，幂等）。"""
        repo = self._require_repo(params)
        bots = self._store.list_for_repo(repo.repo_id)
        if not bots:
            bots = [self._seed(repo.repo_id)]
        return [self._split(bot) for bot in bots]

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

    def _seed(self, repo_id: str) -> Bot:
        """D2 的预置 Bot：`builder` / L1 / agentKind=NULL，并建记忆空间（权限 0700/0600）。"""
        try:
            ensure_memory_space(repo_id, PRESET_BOT_NAME)
            content = read_charter(repo_id, PRESET_BOT_NAME)
        except OSError as exc:
            raise _fail(INTERNAL, MSG_MEMORY_FAILED) from exc
        return self._store.insert(
            Bot(
                bot_id=bot_id(repo_id, PRESET_BOT_NAME),
                repo_id=repo_id,
                name=PRESET_BOT_NAME,
                permission=BOT_PERMISSION_DEFAULT,
                agent_kind=None,  # M1 未选择适配器：S9 之前没有任何通道被验证过（D1）
                charter_rev=charter_rev(content),
                created_at=time.time(),
            )
        )

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
