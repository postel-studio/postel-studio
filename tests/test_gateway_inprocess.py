"""S2 · 网关进程内集成：真监听端口、真 WebSocket 连接（§11.6 允许的唯一例外）。

为什么单独一个文件：`active_connection_count()` 是**进程级**的表，子进程黑盒测不到它。
所以这里在一个线程里起真 uvicorn（真 `listen(0)`、真端口），再用真 `websockets` 客户端连
过去、断开、断言计数变化。**没有用 ASGI 直调绕过网络**，也没有 mock。

子进程黑盒测试在 `test_gateway.py`；两边共用 `gateway_util.py`。
"""

from __future__ import annotations

import asyncio
import json
import socket
import threading
import time
from collections.abc import Iterator

import pytest
import uvicorn
import websockets
from websockets.exceptions import InvalidStatus

from postel_studio.api.app import create_app
from postel_studio.api.gateway import active_connection_count
from postel_studio.api.server import log_config
from postel_studio.web.assets import render_index

from .gateway_util import decode_frame, frame_result


class InProcessKernel:
    """在后台线程里跑一个真 uvicorn（真 socket、真端口），测试完收摊。"""

    def __init__(self, ws_path: str) -> None:
        # 监听 socket 的**所有权交给 uvicorn**（它负责关）：这里不能用 `closing()`，
        # 否则 socket 在 uvicorn 拿走之前就被关掉（实测：`Bad file descriptor`）。
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 0))
        sock.listen(2048)
        self.port = int(sock.getsockname()[1])
        config = uvicorn.Config(
            create_app(ws_path),
            log_config=log_config(),
            log_level="warning",  # 别把 uvicorn 的日志灌进 pytest 输出
            access_log=False,
            lifespan="off",
            timeout_graceful_shutdown=5,
        )
        self.server = uvicorn.Server(config)
        self._thread = threading.Thread(target=lambda: self.server.run(sockets=[sock]), daemon=True)
        self._thread.start()
        self._wait_until_listening()

    def _wait_until_listening(self) -> None:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if getattr(self.server, "started", False):
                return
            time.sleep(0.02)
        raise AssertionError("进程内 uvicorn 10 秒内没有起来")

    def ticket(self) -> str:
        """不是从 HTTP 拿的？—— 也走 HTTP：`create_app` 的 `/` 每次渲染签发一个 ticket。"""
        import httpx

        page = httpx.get(f"http://127.0.0.1:{self.port}/", timeout=5).text
        for line in page.splitlines():
            if 'name="postel-handshake"' in line:
                return line.split('content="', 1)[1].split('"', 1)[0]
        raise AssertionError("页面里没有 ticket")

    def stop(self) -> None:
        self.server.should_exit = True
        self._thread.join(timeout=10)


@pytest.fixture
def kernel() -> Iterator[InProcessKernel]:
    k = InProcessKernel("/gateway/" + "a" * 64)
    try:
        yield k
    finally:
        k.stop()


async def _wait_count(expected: int, timeout: float = 3.0) -> int:
    """等 `active_connection_count()` 变成 `expected`；超时就返回当时的值（由断言报错）。"""
    deadline = time.monotonic() + timeout
    count = active_connection_count()
    while time.monotonic() < deadline:
        count = active_connection_count()
        if count == expected:
            return count
        await asyncio.sleep(0.01)
    return count


# ---------------------------------------------------------------------------
# D5 · 断开后服务端清理
# ---------------------------------------------------------------------------


def test_closing_one_of_two_connections_drops_the_count(kernel: InProcessKernel):
    """§7.3 原话：开 2 个连接，关 1 个，断言计数从 2 → 1。"""

    async def scenario() -> tuple[int, int]:
        url = f"ws://127.0.0.1:{kernel.port}/gateway/" + "a" * 64
        ws_a = await websockets.connect(f"{url}?ticket={kernel.ticket()}")
        ws_b = await websockets.connect(f"{url}?ticket={kernel.ticket()}")
        at_two = await _wait_count(2)
        await ws_a.close()
        at_one = await _wait_count(1)
        await ws_b.close()
        return at_two, at_one

    at_two, at_one = asyncio.run(scenario())

    assert at_two == 2
    assert at_one == 1


def test_count_returns_to_zero_after_all_disconnect(kernel: InProcessKernel):
    async def scenario() -> int:
        url = f"ws://127.0.0.1:{kernel.port}/gateway/" + "a" * 64
        for _ in range(3):  # 反复连断，证明不会漏登记
            async with websockets.connect(f"{url}?ticket={kernel.ticket()}"):
                pass
        return await _wait_count(0)

    assert asyncio.run(scenario()) == 0


def test_count_returns_to_zero_after_abrupt_close(kernel: InProcessKernel):
    """不是优雅 close，而是直接断开传输层：`finally` 走的是异常路径，也必须清干净（D5）。"""

    async def scenario() -> int:
        url = f"ws://127.0.0.1:{kernel.port}/gateway/" + "a" * 64
        ws = await websockets.connect(f"{url}?ticket={kernel.ticket()}")
        assert await _wait_count(1) == 1
        ws.transport.abort()  # 拔线：不给任何 close 帧
        return await _wait_count(0)

    assert asyncio.run(scenario()) == 0


def test_frame_roundtrip_in_process(kernel: InProcessKernel):
    """进程内也跑一次 A5，确保子进程用例与进程内用例两条路都真的能往返。"""

    async def scenario() -> dict[str, object]:
        url = f"ws://127.0.0.1:{kernel.port}/gateway/" + "a" * 64
        async with websockets.connect(f"{url}?ticket={kernel.ticket()}") as ws:
            await ws.send(
                json.dumps({"type": "req", "id": "c1", "method": "system.echo", "params": {"a": 1}})
            )
            return decode_frame(await ws.recv())

    frame = asyncio.run(scenario())
    assert frame["ok"] is True
    assert frame_result(frame)["data"] == {"a": 1}


# ---------------------------------------------------------------------------
# §7.3 · 无 ticket / 错 ticket → 握手被拒（HTTP 403，连接根本没建立）
# ---------------------------------------------------------------------------


def test_rejected_handshake_does_not_touch_active_count(kernel: InProcessKernel):
    """被拒的连接**不该**被登记：`accept()` 之前就关门了，所以计数始终是 0。"""
    url = f"ws://127.0.0.1:{kernel.port}/gateway/" + "a" * 64

    async def scenario() -> list[int]:
        counts: list[int] = []
        for ticket in ("", "nope", "0" * 64):
            try:
                async with websockets.connect(f"{url}?ticket={ticket}"):
                    raise AssertionError("错误 ticket 居然连上了")
            except InvalidStatus as exc:
                assert exc.response.status_code == 403
            counts.append(active_connection_count())
        return counts

    assert asyncio.run(scenario()) == [0, 0, 0]


def test_rejection_is_http_403_not_a_closed_websocket(kernel: InProcessKernel):
    """放宽一点：不只断言 `InvalidStatus`，还要确认它**不是**「连上再被关」。

    `websockets` 用 `InvalidStatus` 表示「握手阶段就失败」；如果实现是先 `accept()` 再
    `close()`，客户端拿到的是**连接成功 + close 帧**，根本不抛 `InvalidStatus`。
    """
    url = f"ws://127.0.0.1:{kernel.port}/gateway/" + "a" * 64

    async def scenario() -> tuple[int, bool]:
        try:
            async with websockets.connect(f"{url}?ticket=bad") as ws:
                await ws.send(json.dumps({"type": "req", "id": "x", "method": "system.echo"}))
                await ws.recv()
        except InvalidStatus as exc:
            return exc.response.status_code, False
        return 0, True  # 走到这里说明握手成功了

    status, accepted = asyncio.run(scenario())
    assert accepted is False
    assert status == 403


# ---------------------------------------------------------------------------
# ticket 语义（进程内版：不必重启内核就能验证「用后即删」与「过期作废」）
# ---------------------------------------------------------------------------


def test_ticket_registry_is_single_use_and_expires():
    """`TicketRegistry` 的语义单测（不碰网络）：一次性 + 到期即失效 + 不泄漏条目。"""
    from postel_studio.api.gateway import TicketRegistry

    registry = TicketRegistry(ttl=0.05)
    ticket = registry.issue()

    assert registry.consume(ticket) is True  # 第一次成功
    assert registry.consume(ticket) is False  # 用后即删

    stale = registry.issue()
    time.sleep(0.08)
    assert registry.consume(stale) is False  # 过期作废（哪怕一次没用过）

    registry.issue()
    assert registry.issue() != ticket  # 每次都不同随机值


def test_page_renders_a_fresh_ticket_each_time():
    """`/` 每渲染一次就签发一个新 ticket（前端「重连时重新 fetch 页面」的前提）。"""
    page_a = render_index("0.1.2", "1", "/tmp/x", "ticket-a", "/gateway")
    page_b = render_index("0.1.2", "1", "/tmp/x", "ticket-b", "/gateway")

    assert "ticket-a" in page_a
    assert "ticket-b" in page_b
    assert "ticket-b" not in page_a
