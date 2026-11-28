"""网关：一次性 handshake ticket → WebSocket → 帧分派（S2 D1/D3/D4/D5）。

方法表（`dispatch()`）：S2 的 `system.echo` / `system.health`，S3 加了 `repo.bind` / `repo.list`，
S4 加了 `repo.branches`，S5 加了 `repo.diff`，S6 加了 `bot.list` / `memory.charter.get`，
S7 加了 `worktree.acquire` / `worktree.list` / `worktree.release`，S8 加 `bot.create` /
`bot.set_permission`，S9 加 `agent.list` / `agent.capabilities`，S10 加 `task.dispatch` /
`task.status` / `task.cancel`（都**只走 WS 帧**，不新开 HTTP 路由）。
其余一律 `UNKNOWN_METHOD`。`subscribe`（02b §2.3）是**连接级**方法：需要当前连接的队列
与事件循环，所以在 WS 处理器里特判、不进方法表——S2~S9 没有 hub，它一律 `UNKNOWN_METHOD`
（旧行为原样保留）。

业务处理函数**不在本模块实现**：它们由 `app` 侧构造后经 `Providers` 注入（S2 R3 的教训，
06-pitfalls T14：网关不反向 import `app`，也不要为了绕环写函数内 import）。

认证走一次性 ticket（05 §1.1 的 S2 修订）：浏览器 `new WebSocket()` 设不了
`Authorization` 头，故浏览器侧不能用 Bearer。ticket 由 `/` 页面注入、60 秒过期、
**用一次即删**，且不能用于任何 HTTP 请求。

两条硬规则：**ticket 不进日志**（05 §4.1，日志只记「有没有」不记「是什么」）；
**先校验、后 `accept()`** —— 拒绝发生在握手阶段（uvicorn 翻成 HTTP 403），那才叫拒绝。
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final, Protocol

from fastapi import FastAPI, WebSocket
from starlette.websockets import WebSocketDisconnect
from uvicorn.protocols.utils import ClientDisconnected as ClientDisconnect

from postel_studio.api import frames

logger = logging.getLogger(__name__)

GATEWAY_PATH: Final = "/gateway"
TICKET_TTL_SECONDS: Final = 60.0
# 4002 = 帧层判定违规（超大帧 / 无法解码的帧）后的服务端主动关闭
CLOSE_FRAME_INVALID: Final = 4002

# S10 D11：订阅是**连接级**方法（02b §2.3），需要当前连接的队列与事件循环，
# 所以在 WS 处理器里特判，不进 `dispatch()`，也不是 Provider。
SUBSCRIBE_METHOD: Final = "subscribe"


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
    `bot_set_permission`，S9 加 `agent_list` / `agent_capabilities`；
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
    memory_context_preview: Callable[[dict[str, object]], dict[str, object]]  # S17
    worktree_acquire: Callable[[dict[str, object]], dict[str, object]]
    worktree_list: Callable[[dict[str, object]], list[dict[str, object]]]
    worktree_release: Callable[[dict[str, object]], dict[str, object]]
    agent_list: Callable[[], list[dict[str, object]]]
    agent_capabilities: Callable[[dict[str, object]], dict[str, object]]
    task_dispatch: Callable[[dict[str, object]], dict[str, object]]
    task_status: Callable[[dict[str, object]], dict[str, object]]
    task_latest: Callable[[dict[str, object]], dict[str, object] | None]
    audit_list: Callable[[dict[str, object]], list[dict[str, object]]]  # S16
    task_cancel: Callable[[dict[str, object]], dict[str, object]]
    candidate_list: Callable[[dict[str, object]], list[dict[str, object]]]
    candidate_approve: Callable[[dict[str, object]], dict[str, object]]
    candidate_reject: Callable[[dict[str, object]], dict[str, object]]


class EventHubLike(Protocol):
    """编排器 `EventHub` 的形状（注入用；网关不反向 import 编排器，T14 的同一纪律）。"""

    def subscribe(
        self,
        topics: frozenset[str],
        queue: asyncio.Queue[tuple[str, dict[str, object]]],
        loop: asyncio.AbstractEventLoop,
    ) -> int: ...

    def replace(self, handle: int, topics: frozenset[str]) -> bool: ...

    def unsubscribe(self, handle: int) -> None: ...


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
    if method == "memory.context.preview":  # S17 / 07 §2.6：本次注入的只读来源清单
        return providers.memory_context_preview(params)
    if method == "worktree.acquire":  # S7 / 07 §4.9：分配工作区（幂等，复用返回同一路径）
        return providers.worktree_acquire(params)
    if method == "worktree.list":  # S7 / 07 §4.9：这个仓库下本内核建的工作区（0..n）
        return providers.worktree_list(params)
    if method == "worktree.release":  # S7 / 07 §4.9：回收（不删分支、不加 --force）
        return providers.worktree_release(params)
    if method == "agent.list":  # S9 / 07 §4.11：已登记的通道 + 文件在不在（不启进程）
        return providers.agent_list()
    if method == "agent.capabilities":  # S9 / 07 §4.11：真握手，能力来自应答
        return providers.agent_capabilities(params)
    if method == "task.dispatch":  # S10 / 07 §4.3：建 queued 行、起 worker，立即应答
        return providers.task_dispatch(params)
    if method == "task.status":  # S10 / 07 §2.2：按 id 取任务载荷
        return providers.task_status(params)
    if method == "task.latest":  # S15：某 Bot 最近一条任务（页面重连恢复中断态）
        return providers.task_latest(params)
    if method == "audit.list":  # S16：权限裁决审计（只读）
        return providers.audit_list(params)
    if method == "task.cancel":  # S10 / 07 §2.2：queued 直接迁移；running 发会话取消
        return providers.task_cancel(params)
    if method == "candidate.list":  # S11 / 07 §2.5：仓库候选（taskId 单条详情）
        return providers.candidate_list(params)
    if method == "candidate.approve":  # S13 / 07 §2.5：批准
        return providers.candidate_approve(params)
    if method == "candidate.reject":  # S13 / 07 §2.5：拒绝（理由持久化）
        return providers.candidate_reject(params)
    raise frames.FrameError(frames.UNKNOWN_METHOD, f"未知方法：{method}")


def _topics_of(params: dict[str, object]) -> frozenset[str] | None:
    """解析 `subscribe` 的入参（D11）：`{topics: [非空字符串]}`，每个都在白名单里。

    合法回 topic 集合；任何不合法回 `None`（调用方翻 `INVALID_PARAMS`）。空集合也算
    不合法——订阅零个 topic 不是"清空订阅"，那是断开重连的事（02b：不保存订阅状态）。
    """
    raw = params.get("topics")
    if not isinstance(raw, list) or not raw:
        return None
    topics: set[str] = set()
    for item in raw:
        if not isinstance(item, str) or item not in frames.KNOWN_TOPICS:
            return None
        topics.add(item)
    return frozenset(topics)


def install(
    app: FastAPI,
    gateway_path: str,
    tickets: TicketRegistry,
    providers: Providers,
    hub: EventHubLike | None = None,
) -> None:
    """把网关挂到 app 上；`gateway_path` 形如 `/gateway/<路径哈希>`（`wsPath`）。

    `hub`（S10 D11）缺省 `None`：不传时 `subscribe` 一律 `UNKNOWN_METHOD`——保持
    S2~S9 的行为，测试与旧客户端不受影响。生产装配总传。
    """

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
        # 事件泵（S10 D11）：编排器线程经 hub 把 evt 塞进这条连接的队列，这里取出来发。
        # `_send_lock` 串行化**所有** send_text——主循环发 res、泵发 evt，两个协程并发
        # 写同一条 WebSocket 在 ASGI 层不保证原子，帧交错就是协议灾难。
        queue: asyncio.Queue[tuple[str, dict[str, object]]] = asyncio.Queue()
        loop = asyncio.get_running_loop()
        send_lock = asyncio.Lock()
        sub_handle: int | None = None

        async def pump() -> None:
            while True:
                topic, data = await queue.get()
                async with send_lock:
                    await ws.send_text(frames.encode_event(topic, seq=state.take_seq(), data=data))

        async def send_frame(text: str) -> None:
            async with send_lock:
                await ws.send_text(text)

        pumper = asyncio.create_task(pump())
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
                    await send_frame(frames.encode_error(exc.reply_id, exc.code, exc.message))
                    continue
                if frame.method == SUBSCRIBE_METHOD:  # 连接级方法：不进 dispatch（D11）
                    if hub is None:
                        await send_frame(
                            frames.encode_error(frame.id, frames.UNKNOWN_METHOD, "未知方法")
                        )
                        continue
                    topics = _topics_of(frame.params)
                    if topics is None:
                        await send_frame(
                            frames.encode_error(
                                frame.id,
                                frames.INVALID_PARAMS,
                                "topics 必须是已知 topic 的非空数组",
                            )
                        )
                        continue
                    if sub_handle is None:
                        sub_handle = hub.subscribe(topics, queue, loop)
                    else:  # 重复 subscribe = 整体替换（D11），不是叠加
                        hub.replace(sub_handle, topics)
                    await send_frame(
                        frames.encode_response(
                            frame.id, seq=state.take_seq(), data={"topics": sorted(topics)}
                        )
                    )
                    continue
                try:
                    data = dispatch(frame.method, frame.params, providers)
                except frames.FrameError as exc:  # 含 UNKNOWN_METHOD
                    await send_frame(frames.encode_error(frame.id, exc.code, exc.message))
                    continue
                except Exception:  # 未预期错误：回稳定码，细节只进本地日志（05 §4.2）
                    logger.exception("方法 %s 执行失败", frame.method)
                    await send_frame(frames.encode_error(frame.id, frames.INTERNAL, "内部错误"))
                    continue
                await send_frame(frames.encode_response(frame.id, seq=state.take_seq(), data=data))
        finally:  # 掉线 / 异常 / 服务端主动关三条路都走这里（D5），不许只在正常路径删
            pumper.cancel()
            if hub is not None and sub_handle is not None:
                hub.unsubscribe(sub_handle)  # 订阅随连接死（D11），不保存任何订阅状态
            with _ACTIVE_LOCK:
                _ACTIVE.pop(conn_id, None)
            logger.info("网关连接关闭，当前活动连接 %d", active_connection_count())
