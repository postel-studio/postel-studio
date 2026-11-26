"""网关：一次性 handshake ticket → WebSocket → 帧分派（S2 D1/D3/D4/D5）。

方法表（`dispatch()`）：S2 的 `system.echo` / `system.health`，S3 加了 `repo.bind` / `repo.list`，
S4 加了 `repo.branches`，S5 加了 `repo.diff`，S6 加了 `bot.list` / `memory.charter.get`，
S7 加了 `worktree.acquire` / `worktree.list` / `worktree.release`
（都**只走 WS 帧**，不新开 HTTP 路由）。
其余一律 `UNKNOWN_METHOD`；订阅 / topic 机制在 S2 **故意不存在**，S3~S7 也没有加——
不要在这里提前长出来。

业务处理函数**不在本模块实现**：它们由 `app` 侧构造后经 `Providers` 注入（S2 R3 的教训，
06-pitfalls T14：网关不反向 import `app`，也不要为了绕环写函数内 import）。

认证走一次性 ticket（05 §1.1 的 S2 修订）：浏览器 `new WebSocket()` 设不了
`Authorization` 头，故浏览器侧不能用 Bearer。ticket 由 `/` 页面注入、60 秒过期、
**用一次即删**，且不能用于任何 HTTP 请求。

两条硬规则：**ticket 不进日志**（05 §4.1，日志只记「有没有」不记「是什么」）；
**先校验、后 `accept()`** —— 拒绝发生在握手阶段（uvicorn 翻成 HTTP 403），那才叫拒绝。
"""

from __future__ import annotations

import logging
import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final

from fastapi import FastAPI, WebSocket
from starlette.websockets import WebSocketDisconnect
from uvicorn.protocols.utils import ClientDisconnected as ClientDisconnect

from postel_studio.api import frames

logger = logging.getLogger(__name__)

GATEWAY_PATH: Final = "/gateway"
TICKET_TTL_SECONDS: Final = 60.0
# 4002 = 帧层判定违规（超大帧 / 无法解码的帧）后的服务端主动关闭
CLOSE_FRAME_INVALID: Final = 4002


class TicketRegistry:
    """一次性 handshake ticket。只存 ticket → 过期时刻（`time.monotonic`）；**不**存任何令牌。"""

    def __init__(self, ttl: float = TICKET_TTL_SECONDS) -> None:
        self._ttl = ttl
        self._lock = threading.Lock()
        self._tickets: dict[str, float] = {}

    def issue(self) -> str:
        """签发 ticket（32 字节 hex），顺手清掉已过期的，避免字典漏气。"""
        ticket = secrets.token_hex(32)
        now = time.monotonic()
        with self._lock:
            self._tickets = {k: v for k, v in self._tickets.items() if v > now}
            self._tickets[ticket] = now + self._ttl
        return ticket

    def consume(self, ticket: str) -> bool:
        """**原子**取出并删除：同一 ticket 只有第一次返回 `True`（一次性）。"""
        if not ticket:
            return False
        with self._lock:
            expiry = self._tickets.pop(ticket, None)
        return expiry is not None and expiry > time.monotonic()


# 活动连接表（D5）：只在 finally 里删；生产路径不读它，仅 `active_connection_count()` 供测试断言。
# `ConnectionState.seq` 是**连接级**计数器（D4），从 1 开始，每发一帧 +1。
_ACTIVE: dict[int, ConnectionState] = {}
_ACTIVE_LOCK = threading.Lock()


@dataclass
class ConnectionState:
    seq: int = 0

    def take_seq(self) -> int:
        self.seq += 1
        return self.seq


@dataclass(frozen=True)
class Providers:
    """从 `app` 侧注入的能力（消环用：`app` import 本模块，本模块不得反向 import `app`）。

    `repo_bind` / `repo_list` 是 S3 加的两个业务入口，`repo_branches` 是 S4 加的第三个，
    `repo_diff` 是 S5 加的第四个，`bot_list` / `memory_charter_get` 是 S6 加的第五、六个，
    S7 又加了 `worktree_acquire` / `worktree_list` / `worktree_release`，S8 加 `bot_create` /
    `bot_set_permission`；
    签名刻意用 `dict → dict`（列表载荷用 `list[dict]`）：闸门只认「帧的参数对象进、载荷对象出」，
    网关不知道仓库、数据库、git、charter 文件、工作区目录的存在。
    抛 `frames.FrameError` 时网关原样转发它的 `code` / `message`（D6 的稳定码）。
    """

    health: Callable[[], dict[str, object]]
    repo_bind: Callable[[dict[str, object]], dict[str, object]]
    repo_list: Callable[[], list[dict[str, object]]]
    repo_branches: Callable[[dict[str, object]], list[dict[str, object]]]
    repo_diff: Callable[[dict[str, object]], dict[str, object]]
    bot_list: Callable[[dict[str, object]], list[dict[str, object]]]
    bot_create: Callable[[dict[str, object]], dict[str, object]]
    bot_set_permission: Callable[[dict[str, object]], dict[str, object]]
    memory_charter_get: Callable[[dict[str, object]], dict[str, object]]
    worktree_acquire: Callable[[dict[str, object]], dict[str, object]]
    worktree_list: Callable[[dict[str, object]], list[dict[str, object]]]
    worktree_release: Callable[[dict[str, object]], dict[str, object]]


def active_connection_count() -> int:
    """当前已 accept 的网关连接数（只读）。断开后必须归零，这是 D5 的可观测点。"""
    with _ACTIVE_LOCK:
        return len(_ACTIVE)


def dispatch(method: str, params: dict[str, object], providers: Providers) -> object:
    """方法表。**新增方法就在这里加一行**，别处不要长出分派逻辑。"""
    if method == "system.echo":  # 通道自检（07 §2.1）：原样回显，不读不写状态，不得有业务语义
        return params
    if method == "system.health":
        return providers.health()
    if method == "repo.bind":  # 07 §2.2 / 07 §4.1；S3 D1：走帧，不开 HTTP 路由
        return providers.repo_bind(params)
    if method == "repo.list":  # 07 §2.2：无参数，返回 repo[]（载荷是**数组**，不是 {data:[...]}）
        return providers.repo_list()
    if method == "repo.branches":  # S4 / 07 §4.5：参数只有 repoId，返回 branch[]（D1 四字段）
        return providers.repo_branches(params)
    if method == "repo.diff":  # S5 / 07 §4.6：两个 ref → 三点 diff 的 stat + patch
        return providers.repo_diff(params)
    if method == "bot.list":  # S6 / 07 §4.7：一个仓库下的 Bot（纯读，没有就回 []，S8 D7）
        return providers.bot_list(params)
    if method == "bot.create":  # S8 / 07 §4.10：建一个 Bot（名字 + 权限由人给）
        return providers.bot_create(params)
    if method == "bot.set_permission":  # S8 / 07 §4.10：人改权限（L3/L4 不收）
        return providers.bot_set_permission(params)
    if method == "memory.charter.get":  # S6 / 07 §4.8：charter 正文 + 内容哈希版本
        return providers.memory_charter_get(params)
    if method == "worktree.acquire":  # S7 / 07 §4.9：分配工作区（幂等，复用返回同一路径）
        return providers.worktree_acquire(params)
    if method == "worktree.list":  # S7 / 07 §4.9：这个仓库下本内核建的工作区（0..n）
        return providers.worktree_list(params)
    if method == "worktree.release":  # S7 / 07 §4.9：回收（不删分支、不加 --force）
        return providers.worktree_release(params)
    raise frames.FrameError(frames.UNKNOWN_METHOD, f"未知方法：{method}")


def install(app: FastAPI, gateway_path: str, tickets: TicketRegistry, providers: Providers) -> None:
    """把网关挂到 app 上；`gateway_path` 形如 `/gateway/<路径哈希>`（`wsPath`）。"""

    @app.websocket(gateway_path)
    async def handler(ws: WebSocket) -> None:
        # 函数名不要叫 `gateway`：那会在函数体内遮蔽本模块名（`gateway.X` 全部失效）。
        if not tickets.consume(ws.query_params.get("ticket", "")):
            logger.info("网关握手被拒（ticket 缺失/已用/过期）")  # 不记录 ticket 内容
            await ws.close(code=1008)  # 先拒后连：不 accept()
            return

        await ws.accept()
        conn_id = id(ws)
        state = ConnectionState()
        with _ACTIVE_LOCK:
            _ACTIVE[conn_id] = state  # 登记（D5），finally 里删
        try:
            while True:
                try:
                    text = await ws.receive_text()
                except (WebSocketDisconnect, ClientDisconnect):
                    break  # 客户端掉线：正常退出路径（ClientDisconnect = 发帧时发现对端已没）
                if len(text.encode("utf-8")) > frames.MAX_FRAME_BYTES:
                    logger.warning("拒绝超大帧，关闭连接")  # 05 §3.1：不回 res，直接关
                    await ws.close(code=CLOSE_FRAME_INVALID)
                    break
                try:
                    frame = frames.decode_incoming(text)
                except frames.FrameError as exc:
                    if exc.reply_id is None:
                        logger.warning("忽略无法归属的坏帧：%s", exc.code)  # 有错没 id：忽略不断连
                        continue
                    await ws.send_text(frames.encode_error(exc.reply_id, exc.code, exc.message))
                    continue
                try:
                    data = dispatch(frame.method, frame.params, providers)
                except frames.FrameError as exc:  # 含 UNKNOWN_METHOD
                    await ws.send_text(frames.encode_error(frame.id, exc.code, exc.message))
                    continue
                except Exception:  # 未预期错误：回稳定码，细节只进本地日志（05 §4.2）
                    logger.exception("方法 %s 执行失败", frame.method)
                    await ws.send_text(frames.encode_error(frame.id, frames.INTERNAL, "内部错误"))
                    continue
                await ws.send_text(
                    frames.encode_response(frame.id, seq=state.take_seq(), data=data)
                )
        finally:  # 掉线 / 异常 / 服务端主动关三条路都走这里（D5），不许只在正常路径删
            with _ACTIVE_LOCK:
                _ACTIVE.pop(conn_id, None)
            logger.info("网关连接关闭，当前活动连接 %d", active_connection_count())
