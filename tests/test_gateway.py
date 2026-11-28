"""S2 · 网关黑盒验收：**真子进程 + 真 HTTP + 真 WebSocket**（与 S1 同一种测试风格）。

一条用例对应一条验收标准或 §7 清单里的一点，断言全部走真实网络，没有 mock：

- A1：页面顶部有状态灯元素与三个状态字面量；
- A2：后端侧——客户端断开后服务端 ≤3s 内清理该连接（前端 DOM 无法在 pytest 里跑，
  见 `test_page_declares_frontend_disconnect_budget` 与交付说明的「未验证」一节）；
- A3：断开后重新连接、重新发 `system.echo` 成功（不需要重启内核）；
- A4：页面 CSS 有 `.stale` 规则、JS 在断开时打上它；
- A5：`system.echo` 的 `req/res` 往返，`result` 含 `data` 与 `seq`。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
import websockets
from websockets.exceptions import ConnectionClosed, InvalidStatus

from postel_studio.api import errors
from postel_studio.api.frames import MAX_FRAME_BYTES, MAX_ID_LENGTH
from postel_studio.web import assets

from .gateway_util import (
    STATUS_STATES,
    Kernel,
    decode_frame,
    frame_error_code,
    frame_result,
    frame_seq,
    parse_page,
)

# 验收 A2 的上限：内核被杀后状态必须在 3 秒内变成「已断开」
DISCONNECT_BUDGET_SECONDS = 3.0


@pytest.fixture
def home(state_home: Path) -> Path:
    """本用例专属的临时状态根（`conftest` 的 `state_home` 已把 `POSTEL_HOME` 指过去）。"""
    return state_home


@pytest.fixture
def kernel(home: Path) -> Iterator[Kernel]:
    with Kernel(home) as k:
        yield k


async def _echo(url: str, params: dict[str, object] | None = None) -> dict[str, object]:
    """连上去发一条 `system.echo`，返回解析后的响应帧（连接用完即关）。"""
    async with websockets.connect(url) as ws:
        await ws.send(
            json.dumps({"type": "req", "id": "c1", "method": "system.echo", "params": params or {}})
        )
        return decode_frame(await ws.recv())


# ---------------------------------------------------------------------------
# A1 · 状态灯与三个状态字面量
# ---------------------------------------------------------------------------


def test_page_declares_status_lamp_and_three_states(kernel: Kernel):
    page = kernel.get("/").text

    # 状态灯元素本身（id + 三态各自的 CSS 规则）
    assert 'id="status"' in page
    assert 'data-state="connected"' in page
    assert 'data-state="disconnected"' in page
    assert 'data-state="reconnecting"' in page
    for state in STATUS_STATES:
        assert state in page, f"页面缺少状态字面量：{state}"


def test_page_has_reconnect_button(kernel: Kernel):
    """A2 的「出现「重连」按钮」：元素在页面里，显示由 JS 按状态切换。"""
    page = kernel.get("/").text

    assert 'id="reconnect"' in page
    assert ">重连<" in page
    # 断连时态才显示：CSS 缺省 display:none，JS 只在 disconnected 时放开
    assert re.search(r"#reconnect\s*\{[^}]*display:\s*none", page) is not None
    # 放开时必须显式给一个值：`style.display = ""` 会**回退到 CSS 的 none**（实测踩过）
    assert 'button.style.display = state === "disconnected" ? "inline-block" : "none"' in page


# ---------------------------------------------------------------------------
# A4 · 过期标记
# ---------------------------------------------------------------------------


def test_page_has_stale_rule_and_toggles_it_on_disconnect(kernel: Kernel):
    page = kernel.get("/").text

    # CSS 规则：`.stale` 一上色就灰化，并且有角标（不假装仍是最新）
    assert re.search(r"#content\.stale\s*\{[^}]*opacity", page) is not None
    assert re.search(r"#content\.stale::after\s*\{[^}]*content:", page) is not None
    # JS：状态不是 connected 就加，回到 connected 就去掉（同一个出口函数，不会漏一边）
    assert 'classList.toggle("stale", state !== "connected")' in page


# ---------------------------------------------------------------------------
# A2 · 后端侧可验证的部分：断开 ≤3s 内服务端清理连接
# ---------------------------------------------------------------------------


def test_server_cleans_up_connection_after_client_disconnect(kernel: Kernel):
    """客户端断开后，网关必须在 3 秒内走完 `finally` 并把连接从活动表里删掉（D5 / A2 后端侧）。

    怎么观测：读内核子进程的 **stderr**——`finally` 里打一条带计数的 INFO 日志
    （内核的 logger 被挂到 uvicorn 的 default 流处理器上，写 stderr，见 server.log_config）。
    这条断言依赖的是「真的发生了清理」，不是「代码里 finally 里有清理」。
    """
    asyncio.run(_echo(kernel.ws_url(kernel.fetch_page()[0])))  # 连接在 `async with` 退出时关闭
    assert kernel.proc.stderr is not None

    line = _read_until(kernel, "网关连接关闭", DISCONNECT_BUDGET_SECONDS)

    assert "网关连接关闭" in line, f"断开后 3 秒内没有看到服务端清理日志：{line!r}"
    # 计数必须归零：只剩「日志说清理了」不算，要看到表里真的没人了
    assert "当前活动连接 0" in line, line


def _read_until(kernel: Kernel, needle: str, timeout: float) -> str:
    """从内核 stderr 里读到 `needle`（带超时）。

    不能用 `stream.readline()`：`select` 说「有数据」，`readline` 却会一直等到**换行**才返回，
    中途没有任何输出时就挂死（实测踩过一次）。直接对文件描述符 `os.read`，拿到多少算多少。
    """
    import selectors

    assert kernel.proc.stderr is not None
    fd = kernel.proc.stderr.fileno()
    deadline = time.monotonic() + timeout
    seen = b""
    with selectors.DefaultSelector() as sel:
        sel.register(fd, selectors.EVENT_READ)
        while time.monotonic() < deadline and needle.encode() not in seen:
            if not sel.select(timeout=0.2):
                continue
            chunk = os.read(fd, 65536)  # 阻塞 fd，但 select 已保证可读
            if not chunk:
                break
            seen += chunk
    return seen.decode("utf-8", "replace")


# ---------------------------------------------------------------------------
# A5 · 一次帧往返
# ---------------------------------------------------------------------------


def test_echo_roundtrip_returns_data_and_seq(kernel: Kernel):
    ticket, _ = kernel.fetch_page()

    frame = asyncio.run(_echo(kernel.ws_url(ticket), {"hello": "气泡"}))

    assert frame["type"] == "res"
    assert frame["id"] == "c1"
    assert frame["ok"] is True
    result = frame_result(frame)
    assert result["data"] == {"hello": "气泡"}  # 原样回显，不加工
    assert isinstance(result["seq"], int)
    assert result["seq"] >= 1


def test_health_roundtrip_matches_http_health(kernel: Kernel):
    """`system.health` 复用 `app.health()`：与 `/api/health` 同一份字段。"""
    ticket, _ = kernel.fetch_page()

    async def call() -> dict[str, object]:
        async with websockets.connect(kernel.ws_url(ticket)) as ws:
            await ws.send(json.dumps({"type": "req", "id": "h1", "method": "system.health"}))
            return decode_frame(await ws.recv())

    frame = asyncio.run(call())
    http_payload = kernel.get("/api/health").json()

    assert frame["ok"] is True
    result = frame_result(frame)
    assert result["data"] == http_payload
    assert isinstance(result["seq"], int)


def test_seq_is_strictly_increasing_within_one_connection(kernel: Kernel):
    """D4：`seq` 是连接级计数器，从 1 开始，每发一帧 +1。"""
    ticket, _ = kernel.fetch_page()

    async def three_calls() -> list[int]:
        async with websockets.connect(kernel.ws_url(ticket)) as ws:
            seqs: list[int] = []
            for i in range(3):
                await ws.send(
                    json.dumps(
                        {"type": "req", "id": f"c{i}", "method": "system.echo", "params": {"i": i}}
                    )
                )
                frame = decode_frame(await ws.recv())
                assert frame["id"] == f"c{i}"
                seqs.append(frame_seq(frame))
            return seqs

    assert asyncio.run(three_calls()) == [1, 2, 3]


def test_seq_restarts_for_a_new_connection(kernel: Kernel):
    """计数器是**连接级**的，不是全局的（否则丢帧检测在重连后就不成立了）。"""
    ticket_a, _ = kernel.fetch_page()
    ticket_b, _ = kernel.fetch_page()

    async def first_seq(ticket: str) -> int:
        frame = await _echo(kernel.ws_url(ticket))
        return frame_seq(frame)

    assert asyncio.run(first_seq(ticket_a)) == 1
    assert asyncio.run(first_seq(ticket_b)) == 1


# ---------------------------------------------------------------------------
# 错误码稳定性
# ---------------------------------------------------------------------------


def test_unknown_method_is_unknown_method_not_subscribe(kernel: Kernel):
    """只实现已登记的方法：其它（含 `system.subscribe`）一律 UNKNOWN_METHOD。
    `repo.bind`（S3）/ `repo.branches`（S4）/ `repo.diff`（S5）/ `bot.list`（S6）**都已实现**，
    不再是反例——它们收到空 params 时正确回 `INVALID_PARAMS`（或 `REPO_NOT_BOUND`）。
    反例保留 5 个仍未实现的方法（D7 要求至少 4 个；S6 把 `bot.list` 换成了 `bot.create`，
    S8 又把 `bot.create` 换成了 `bot.delete`——一个方法被实现就要从反例里退出）。
    """
    ticket, _ = kernel.fetch_page()

    async def calls() -> list[dict[str, object]]:
        out = []
        async with websockets.connect(kernel.ws_url(ticket)) as ws:
            for i, method in enumerate(
                [
                    "bot.delete",
                    "task.list",  # S10：task.dispatch/status/cancel 已是真方法，换 S11 的
                    "memory.charter.set",  # S9：agent.list 已变成真方法，换一个还没实现的（S22）
                    "candidate.push",  # S13：approve/reject 已实现，push 属 S16
                    "system.echo.x",
                ]
            ):
                await ws.send(json.dumps({"type": "req", "id": f"m{i}", "method": method}))
                out.append(decode_frame(await ws.recv()))
        return out

    for frame in asyncio.run(calls()):
        assert frame["ok"] is False
        assert frame_error_code(frame) == "UNKNOWN_METHOD"


def test_unknown_method_does_not_close_connection(kernel: Kernel):
    """回错码，**不断连**：后面再发合法请求照样能通。"""
    ticket, _ = kernel.fetch_page()

    async def mixed() -> list[dict[str, object]]:
        async with websockets.connect(kernel.ws_url(ticket)) as ws:
            await ws.send(json.dumps({"type": "req", "id": "bad", "method": "bot.list"}))
            bad = decode_frame(await ws.recv())
            await ws.send(json.dumps({"type": "req", "id": "good", "method": "system.echo"}))
            good = decode_frame(await ws.recv())
            return [bad, good]

    bad, good = asyncio.run(mixed())
    assert bad["ok"] is False
    assert good["ok"] is True


def test_invalid_frame_with_id_gets_invalid_params(kernel: Kernel):
    """`type` 错但有 id：回 `INVALID_PARAMS`（05 §3.1「忽略」在有 id 时升级成可回的错误）。"""
    ticket, _ = kernel.fetch_page()

    async def bad_type() -> dict[str, object]:
        async with websockets.connect(kernel.ws_url(ticket)) as ws:
            await ws.send(json.dumps({"type": "evt", "id": "e1", "topic": "x"}))
            return decode_frame(await ws.recv())

    frame = asyncio.run(bad_type())
    assert frame["ok"] is False
    assert frame_error_code(frame) == "INVALID_PARAMS"
    assert frame["id"] == "e1"


def test_oversized_id_is_rejected_without_echoing_it(kernel: Kernel):
    ticket, _ = kernel.fetch_page()
    long_id = "x" * (MAX_ID_LENGTH + 1)

    async def send_long_id() -> dict[str, object]:
        async with websockets.connect(kernel.ws_url(ticket)) as ws:
            await ws.send(json.dumps({"type": "req", "id": long_id, "method": "system.echo"}))
            # 无 id 可回 → 服务端忽略；连接应当还活着，再发一条合法的能拿到回包
            await ws.send(json.dumps({"type": "req", "id": "ok", "method": "system.echo"}))
            return decode_frame(await ws.recv())

    frame = asyncio.run(send_long_id())
    assert frame["id"] == "ok"
    assert frame["ok"] is True


# ---------------------------------------------------------------------------
# 超大帧：断言的是「服务端主动关闭连接」，不是「回一条错误帧」
# ---------------------------------------------------------------------------


def test_oversized_frame_closes_the_connection(kernel: Kernel):
    """> 1 MiB 的帧：**服务端关连接**，且不为它回 res（05 §3.1「关闭连接并记录」）。

    这里断言的三种可能行为里明确选了哪一种：
    1. 服务端主动关（我们实现的行为，close code 4002 / 或在客户端侧表现为 ConnectionClosed）；
    2. uvicorn 自身 `ws_max_size`（16 MiB）拒绝 —— 1 MiB 的帧碰不到它，所以不是这条路；
    3. 回一条 `FRAME_TOO_LARGE` 的 res —— **不是**我们的行为，也不该是（超限帧不可信）。
    """
    ticket, _ = kernel.fetch_page()
    oversized = json.dumps(
        {
            "type": "req",
            "id": "big",
            "method": "system.echo",
            "params": {"b": "a" * MAX_FRAME_BYTES},
        }
    )
    assert len(oversized.encode("utf-8")) > MAX_FRAME_BYTES

    async def send_big() -> str:
        closed = "NOT CLOSED"
        try:
            async with websockets.connect(kernel.ws_url(ticket), max_size=64 * 1024 * 1024) as ws:
                await ws.send(oversized)
                # 有回包就说明服务端没关它；5 秒内没动静则抛 TimeoutError → 用例失败
                await asyncio.wait_for(ws.recv(), timeout=5)
        except ConnectionClosed as exc:
            closed = f"closed:{exc.rcvd.code if exc.rcvd else None}"
        return closed

    outcome = asyncio.run(send_big())
    assert outcome.startswith("closed:"), f"超大帧之后连接仍在（{outcome}）"


# ---------------------------------------------------------------------------
# 认证：ticket 一次性 与 拒绝握手
# ---------------------------------------------------------------------------


def test_ticket_is_single_use(kernel: Kernel):
    """同一 ticket 用两次：第二次连不上（一次性），且是**握手阶段**被拒（HTTP 403）。"""
    ticket, _ = kernel.fetch_page()
    url = kernel.ws_url(ticket)

    assert asyncio.run(_echo(url))["ok"] is True  # 第一次：用掉了

    with pytest.raises(InvalidStatus) as exc:
        asyncio.run(_echo(url))
    assert exc.value.response.status_code == 403


def test_missing_ticket_is_rejected_with_403(kernel: Kernel):
    _, ws_path = kernel.fetch_page()
    url = f"ws://{kernel.host}:{kernel.port}{ws_path}"  # 不带 ?ticket

    with pytest.raises(InvalidStatus) as exc:
        asyncio.run(_echo(url))
    assert exc.value.response.status_code == 403


def test_forged_ticket_is_rejected_with_403(kernel: Kernel):
    _, ws_path = kernel.fetch_page()

    with pytest.raises(InvalidStatus) as exc:
        asyncio.run(_echo(kernel.ws_url("0" * 64, path=ws_path)))
    assert exc.value.response.status_code == 403


def test_ws_path_is_stable_hash_and_differs_from_token(kernel: Kernel):
    """D7：`wsPath` = `/gateway/<状态根 sha256 前 16 hex>`，**不是**凭据、与 token 无关。"""
    ws_path = str(kernel.info["wsPath"])
    token = (kernel_home(kernel) / "token").read_text(encoding="utf-8")

    assert re.fullmatch(r"/gateway/[0-9a-f]{16}", ws_path), ws_path
    assert token[:16] not in ws_path


def test_ws_path_survives_restart_and_differs_per_state_root(home: Path):
    """D7 的两条硬断言：重启前后 `wsPath` 相同；不同 `POSTEL_HOME` 的 `wsPath` 不同。"""
    with Kernel(home) as first:
        before = str(first.info["wsPath"])
    with Kernel(home) as second:  # 同一状态根，进程换了
        after = str(second.info["wsPath"])

    assert before == after, f"重启后 wsPath 变了：{before} → {after}"

    other_home = home.parent / f"{home.name}-other"
    other_home.mkdir()
    with Kernel(other_home) as elsewhere:
        assert str(elsewhere.info["wsPath"]) != before


def kernel_home(kernel: Kernel) -> Path:
    """子进程的 `POSTEL_HOME`（从 server.json 的 stateRoot 取，别猜）。"""
    return Path(str(kernel.info["stateRoot"]))


def test_page_does_not_carry_the_long_term_token(kernel: Kernel):
    """D1 的硬要求：**长期令牌不进页面**（05 §4.1）。文件里的 token 绝不能出现在 HTML 里。"""
    token = (kernel_home(kernel) / "token").read_text(encoding="utf-8")
    page = kernel.get("/").text

    assert token not in page
    assert "tokenFingerprint" not in page


def test_ticket_never_reaches_kernel_logs(kernel: Kernel):
    """05 §4.1 硬约束：「凭据绝不进入日志」。票虽一次性，但**内核自己**的输出里不能有它。

    这条是 R2 的回归锁。它不是「我们打算这么做」，而是**读内核进程的真实输出**：
    连一次网关（这一刻 uvicorn 会打「WebSocket …?ticket=… [accepted]」），然后把内核
    stdout+stderr 整个读出来，断言 ticket 前 16 个字符**一次都不出现**。

    取前 16 字符而不是全串：任何把 ticket 打出来的地方都会带足够多的前缀，除非有人
    专门按 16 字符截断（那也不是「泄漏整串」，且不现实）。
    """
    ticket, _ = kernel.fetch_page()
    prefix = ticket[:16]
    assert len(prefix) == 16

    asyncio.run(_echo(kernel.ws_url(ticket)))  # 一次真实握手，触发访问日志

    output = _drain_kernel_output(kernel)  # 关掉内核，把两条流读完
    assert prefix not in output, (
        f"内核输出里出现了 ticket 前缀 {prefix}：\n{_leak_lines(output, prefix)}"
    )


def _leak_lines(output: str, needle: str) -> str:
    """把命中的行摘出来（脱敏：只留出现位置的前后 40 字符），便于定位是哪个 logger 干的。"""
    hits = []
    for line in output.splitlines():
        if needle in line:
            idx = line.index(needle)
            hits.append("  …" + line[max(0, idx - 40) : idx + 8] + "…<REDACTED>…")
    return "\n".join(hits)


def _drain_kernel_output(kernel: Kernel) -> str:
    """关掉内核并把 stdout + stderr 读到 EOF（日志是缓冲的，不等退出可能读不全）。"""
    assert kernel.proc.stdout is not None and kernel.proc.stderr is not None
    kernel.stop()
    out = kernel.proc.stdout.read()
    err = kernel.proc.stderr.read()
    return out + err


# ---------------------------------------------------------------------------
# D7 · /api/where：内核自己的位置（前端重连时据此发现「是不是还在同一个地址」）
# ---------------------------------------------------------------------------


def test_where_reports_this_kernels_real_binding(kernel: Kernel):
    """`/api/where` 回的是**本进程真实在听的** host/port 与 `wsPath`，不是 `server.json` 的复述。

    验证「不是读 server.json」的办法：跑起来之后把 `server.json` 删掉，再问 `/api/where`——
    如果它读文件，这里就该 500 或返回空；它回得出来，说明数据来自进程内的绑定。
    """
    payload = kernel.get("/api/where").json()

    assert payload["host"] == "127.0.0.1"
    assert payload["port"] == kernel.port  # 就是 server.json 里那个端口（内核自己分配的）
    assert payload["wsPath"] == kernel.ws_path
    assert payload["pid"] == kernel.proc.pid

    (kernel_home(kernel) / "server.json").unlink()  # 删掉文件，证明不是读它
    again = kernel.get("/api/where").json()
    assert again == payload


def test_where_has_no_credentials(kernel: Kernel):
    """与 `/` 同理：不需要认证，也不回任何凭据（token / ticket）。"""
    token = (kernel_home(kernel) / "token").read_text(encoding="utf-8")
    resp = kernel.get("/api/where")
    body = resp.text

    assert resp.status_code == 200
    assert token not in body
    assert set(resp.json()) == {"host", "port", "wsPath", "pid"}
    assert kernel.get("/api/where").json()["pid"] == kernel.proc.pid


def test_where_path_is_not_under_gateway_prefix(kernel: Kernel):
    """`/api/where` 必须走 HTTP，不受网关路径约束（它就是一个 GET 路由）。"""
    assert kernel.get("/api/where").status_code == 200


# ---------------------------------------------------------------------------
# A3 · 断开后重连（同一内核，客户端重新取 ticket）
# ---------------------------------------------------------------------------


def test_reconnect_after_disconnect_works_without_restart(kernel: Kernel):
    """A3 的网关侧：断开 → 重新取一个 ticket → 再连 → `system.echo` 仍然成功。"""
    first_ticket, _ = kernel.fetch_page()
    assert asyncio.run(_echo(kernel.ws_url(first_ticket)))["ok"] is True

    second_ticket, _ = kernel.fetch_page()  # 重连时前端也是重新 fetch("/") 拿新 ticket
    assert second_ticket != first_ticket
    frame = asyncio.run(_echo(kernel.ws_url(second_ticket)))

    assert frame["ok"] is True
    assert frame_seq(frame) == 1  # 新连接 = 新计数器


# ---------------------------------------------------------------------------
# D7 · 前端「端口变了」的诚实收口
# ---------------------------------------------------------------------------


def test_frontend_reports_port_change_and_does_not_guess(kernel: Kernel):
    """端口变了时前端**不去猜**新地址：不重载页面，直接把状态设为已断开并给明确文案。

    为什么用文本断言：这条行为只能在浏览器里跑，pytest 检查的是「代码里确实这么写了」
    （D7 要求「不要声称任何重启都能重连」，所以这里连 URL 拼接的兜底都不允许有）。
    """
    page = kernel.get("/").text

    assert "/api/where" in page  # 先问内核自己：答话的是不是还是同一个端口
    assert 'throw new Error("PORT_CHANGED")' in page
    assert "内核已换端口或未启动，请从启动它的终端重新打开地址" in page
    # 不许有「整页重载换个地址再试」的猜测式兜底
    assert "location.reload()" not in page


def test_frontend_still_uses_same_origin_first(kernel: Kernel):
    """同源 `fetch("/api/where")` 优先：端口没变时这条链直接成功，不需要任何跨端口手段。"""
    page = kernel.get("/").text

    assert 'fetch("/api/where"' in page


# ---------------------------------------------------------------------------
# 前端参数：A2 里「没法在 pytest 里验 DOM」的那部分的**部分**自动化
# ---------------------------------------------------------------------------


def test_frontend_disconnect_budget_constants():
    """数值意图：心跳判死 + 重连延迟都远小于 3 秒（§12 允许的最小保证，仅证明意图）。"""
    total_ms = assets.HEARTBEAT_INTERVAL_MS + assets.RECONNECT_DELAY_MS

    assert assets.HEARTBEAT_INTERVAL_MS < 1000 * DISCONNECT_BUDGET_SECONDS
    assert assets.RECONNECT_DELAY_MS <= 1000
    assert total_ms < 1000 * DISCONNECT_BUDGET_SECONDS, total_ms


def test_page_inlines_the_same_constants_it_tests():
    """页面里的数字必须就是上面断言的常量（否则测试和页面会各自漂移）。"""
    page = assets.render_index(
        "0.1.2", "1", "/tmp/x", "ticket", "/gateway", errors.ERROR_TEXT, errors.UNKNOWN_CODE_TEXT
    )

    assert f"HEARTBEAT_INTERVAL_MS = {assets.HEARTBEAT_INTERVAL_MS};" in page
    assert f"RECONNECT_DELAY_MS = {assets.RECONNECT_DELAY_MS};" in page
    # 占位符必须全被替换掉（漏一个是页面直接坏掉，不是小事）
    for placeholder in assets.REQUIRED_PARAMS:
        assert placeholder not in page


def test_render_does_not_confuse_braces_with_placeholders():
    """页面里满是 CSS/JS 花括号：渲染必须只替换占位符，不碰别的 `{`。"""
    page = assets.render_index(
        "0.1.2", "1", "/tmp/a&b<c>", "tk", "/gateway", errors.ERROR_TEXT, errors.UNKNOWN_CODE_TEXT
    )

    assert '{ type: "req", id: id' in page  # JS 里的花括号原样保留
    assert "/tmp/a&amp;b&lt;c&gt;" in page  # 路径已转义
    assert "<script>" in page and "</script>" in page


def test_parse_page_round_trip(kernel: Kernel):
    """`parse_page` 是测试自己的工具函数，也得验一次（否则它坏了会把别的用例变成假绿）。"""
    page = kernel.get("/").text
    ticket, ws_path = parse_page(page)

    assert len(ticket) == 64
    assert ws_path.startswith("/gateway/")
