"""静态页面。只是一个字符串常量，不引入前端构建链（S1 的禁止事项，D6 不变）。

S2 起页面里有内联 JS 与一次 WebSocket：`server.json` 里的 `wsPath` 浏览器读不到，所以
网关路径与**一次性 handshake ticket** 都由内核渲染页面时写进来（05 §1.1 的 S2 修订）。
页面里**没有**长期连接令牌。

前端「断线判定」只靠两种信号：socket 的 `close`/`error` 事件，以及 `HEARTBEAT_INTERVAL_MS`
到点还没收到回包（内核被 SIGKILL 时对端不会发 close 帧，只有心跳能发现）。两个参数都是
具名常量，且都远小于验收要求的 3 秒上限。
"""

from __future__ import annotations

import html
from typing import Final

# 前端行为参数：测试断言 `HEARTBEAT_INTERVAL_MS + RECONNECT_DELAY_MS < 3000`。
# 改这里就等于改「≤3 秒变红」的承诺——别只改数字不改注释，也别把补一次丢帧的余量算漏。
HEARTBEAT_INTERVAL_MS: Final = 1000
RECONNECT_DELAY_MS: Final = 500
# 页面里出现的全部占位符。渲染用 `str.replace` 而不是 `str.format`：页面里全是 CSS/JS 的
# 花括号，双写正则极易漏一个，而漏一个就是运行时 `ValueError` 或静默错替换。
REQUIRED_PARAMS: Final = (
    "%%VERSION%%",
    "%%API_VERSION%%",
    "%%STATE_ROOT%%",
    "%%HANDSHAKE_TICKET%%",
    "%%WS_PATH%%",
    "%%HEARTBEAT_INTERVAL_MS%%",
    "%%RECONNECT_DELAY_MS%%",
)

INDEX_HTML: Final = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="postel-ws-path" content="%%WS_PATH%%">
<meta name="postel-handshake" content="%%HANDSHAKE_TICKET%%">
<title>Postel Studio · 连接状态</title>
<style>
  body { margin: 0; min-height: 100vh; display: grid; place-items: center;
    background: #0f1115; color: #e6e9ef;
    font: 16px/1.7 -apple-system, "Noto Sans CJK SC", "PingFang SC", sans-serif; }
  main { padding: 40px; border: 1px solid #262b36; border-radius: 14px;
    background: #151922; box-shadow: 0 10px 40px rgb(0 0 0 / 45%); }
  h1 { margin: 0; font-size: 26px; font-weight: 600; letter-spacing: .5px; }
  header { display: flex; align-items: center; gap: 12px; margin: 0 0 20px; }
  #status { margin-left: auto; display: inline-flex; align-items: center; gap: 8px;
    font-size: 14px; color: #8b95a7; }
  .dot { display: inline-block; width: 9px; height: 9px; border-radius: 50%;
    background: #3fb950; box-shadow: 0 0 10px #3fb950; }
  /* 状态灯：已连接绿 / 已断开红 / 重连中黄；颜色与文案由 JS 同时改 */
  #status[data-state="connected"] .dot { background: #3fb950; box-shadow: 0 0 10px #3fb950; }
  #status[data-state="disconnected"] .dot { background: #f85149; box-shadow: 0 0 10px #f85149; }
  #status[data-state="reconnecting"] .dot { background: #d29922; box-shadow: 0 0 10px #d29922; }
  #reconnect { display: none; margin-left: 4px; padding: 2px 10px; font: inherit;
    font-size: 13px; color: #e6e9ef; background: #1f2430; border: 1px solid #39404f;
    border-radius: 6px; cursor: pointer; }
  #reconnect:hover { border-color: #58627a; }
  #reconnect:disabled { opacity: .5; cursor: progress; }
  dl { display: grid; grid-template-columns: auto auto; gap: 6px 18px; margin: 0; }
  dt { color: #8b95a7; }
  dd { margin: 0; font-family: ui-monospace, "SF Mono", Menlo, monospace;
    transition: opacity .2s; }
  /* 断连期间已加载的数据是过期的：灰化 + 右上角角标，不假装仍是最新 */
  #content { position: relative; transition: opacity .2s; }
  #content.stale { opacity: .45; }
  #content.stale::after { content: "数据已过期"; position: absolute; top: -14px; right: 0;
    padding: 1px 8px; font-size: 12px; color: #0f1115; background: #d29922;
    border-radius: 999px; }
</style>
</head>
<body>
<main>
  <header>
    <h1>内核存活</h1>
    <span id="status" data-state="reconnecting" role="status" aria-live="polite">
      <span class="dot"></span><span id="status-text">重连中</span>
      <button id="reconnect" type="button">重连</button>
    </span>
  </header>
  <p id="note" style="display:none; margin:-8px 0 18px; font-size:13px; color:#d29922;"></p>
  <div id="content">
    <dl>
      <dt>version</dt><dd>%%VERSION%%</dd>
      <dt>apiVersion</dt><dd>%%API_VERSION%%</dd>
      <dt>stateRoot</dt><dd>%%STATE_ROOT%%</dd>
    </dl>
  </div>
</main>
<script>
(function () {
  "use strict";
  var HEARTBEAT_INTERVAL_MS = %%HEARTBEAT_INTERVAL_MS%%;  // 到点没回包就判死
  var RECONNECT_DELAY_MS = %%RECONNECT_DELAY_MS%%;        // 点「重连」到发起的间隔
  var CONNECT_TIMEOUT_MS = 2000;                         // 握手超时，也算失败

  var metaPath = document.querySelector('meta[name="postel-ws-path"]');
  var metaTicket = document.querySelector('meta[name="postel-handshake"]');
  var content = document.getElementById("content");
  var statusEl = document.getElementById("status");
  var statusText = document.getElementById("status-text");
  var button = document.getElementById("reconnect");

  var socket = null;
  var seq = 0;            // 请求 id 计数，只用于配对 res
  var beatTimer = null;   // 心跳定时器
  var connectTimer = null;
  var awaitingBeatId = null;

  function setState(state, text) {
    statusEl.dataset.state = state;
    statusText.textContent = text;
    button.style.display = state === "disconnected" ? "inline-block" : "none";
    button.disabled = false;
    content.classList.toggle("stale", state !== "connected");
  }

  // 「怎么办」提示：只在需要人工介入时出现（端口变了 / 内核没起来），连上就清掉。
  var noteEl = document.getElementById("note");
  function setNote(text) { noteEl.textContent = text; noteEl.style.display = text ? "" : "none"; }

  function clearTimers() {
    if (beatTimer) { clearInterval(beatTimer); beatTimer = null; }
    if (connectTimer) { clearTimeout(connectTimer); connectTimer = null; }
  }

  function fail() {          // 唯一的「向下」出口：三态里只有已断开是终态
    clearTimers();
    socket = null;
    setState("disconnected", "已断开");
  }

  function send(method, params) {
    if (!socket || socket.readyState !== 1) { return null; }
    var id = "c" + (++seq);
    try { socket.send(JSON.stringify({ type: "req", id: id, method: method, params: params })); }
    catch (e) { return null; }
    return id;
  }

  function onMessage(ev) {
    var frame;
    try { frame = JSON.parse(ev.data); } catch (e) { return; }
    // 心跳回包：证明内核在做帧往返，而不只是 TCP 还开着
    if (frame && frame.id === awaitingBeatId) {
      awaitingBeatId = null;
      if (statusEl.dataset.state !== "connected") { setState("connected", "已连接"); }
      return;
    }
    if (frame && frame.type === "res" && !frame.ok && frame.error) {
      console.warn("网关返回错误", frame.error.code);
    }
  }

  function startHeartbeat() {
    if (beatTimer) { clearInterval(beatTimer); }
    beatTimer = setInterval(function () {
      if (!socket || socket.readyState !== 1) { return; }
      if (awaitingBeatId) { fail(); return; }   // 上一拍没回：判死（内核多半被杀了）
      awaitingBeatId = send("system.echo", { t: Date.now() });
    }, HEARTBEAT_INTERVAL_MS);
  }

  // 页面里内嵌的 ticket 是一次性的：连过一次就作废。重连必须重新拿一个页面（`/` 不需要
  // 凭据，每次渲染签发新 ticket）。取不到说明内核可能还没起来，就保持「已断开」。
  function freshTicket() {
    return fetch("/api/where", { cache: "no-store" })
      .then(function (r) {
        if (!r.ok) { throw new Error("HTTP " + r.status); }
        return r.json();
      })
      .then(function (w) {
        // 这是 HTTP 路由，回**裸对象**（与 `/api/health` 一致，不套帧包络 {data,seq}）
        if (!w || !w.port || !w.wsPath) { throw new Error("where 响应不完整"); }
        // 端口变了 → 现在答话的是**另一个**内核，本页面的旧地址已经没用了。
        // 浏览器没有途径自动发现新端口（那是服务发现的活，S2 不做），所以如实报断。
        if (String(w.port) !== location.port) { throw new Error("PORT_CHANGED"); }
        metaPath.content = w.wsPath;
        return fetch("/", { cache: "no-store" }).then(function (r) {
          if (!r.ok) { throw new Error("HTTP " + r.status); }
          return r.text();
        });
      })
      .then(function (page) {
        var doc = new DOMParser().parseFromString(page, "text/html");
        var t = doc.querySelector('meta[name="postel-handshake"]');
        if (!t || !t.content) { throw new Error("页面缺少 ticket"); }
        return t.content;
      });
  }

  function connect(ticket) {
    clearTimers();
    setState("reconnecting", "重连中");
    // 内核只绑 127.0.0.1 且只讲 ws://（05 §1.1）；https/wss 是将来远程访问隧道的事，
    // 本刀不写那条从未执行过的分支。
    var url = "ws://" + location.host + metaPath.content + "?ticket=" + encodeURIComponent(ticket);
    var ws;
    try { ws = new WebSocket(url); }
    catch (e) { fail(); return; }
    socket = ws;
    connectTimer = setTimeout(function () { if (socket === ws && ws.readyState === 0) { fail(); } },
                              CONNECT_TIMEOUT_MS);
    ws.onopen = function () {
      if (connectTimer) { clearTimeout(connectTimer); connectTimer = null; }
      awaitingBeatId = null;
      setNote("");
      setState("connected", "已连接");
      startHeartbeat();
    };
    ws.onmessage = onMessage;
    ws.onclose = function () { if (socket === ws) { fail(); } };
    ws.onerror = function () { if (socket === ws) { fail(); } };
  }

  function reconnect() {
    if (socket) { return; }   // 已经在连 / 已连上就忽略
    button.disabled = true;
    setState("reconnecting", "重连中");   // 取新页面要时间，别让灯停在红
    clearTimers();
    setTimeout(function () {
      freshTicket().then(connect).catch(function () {
        // 不重载页面：端口变了的话重载也还是旧地址，只会骗人。
        fail();
        setNote("内核已换端口或未启动，请从启动它的终端重新打开地址");
      });
    }, RECONNECT_DELAY_MS);
  }

  button.addEventListener("click", reconnect);
  // 断连期间**不自动重连**（禁止事项：不得静默重试而不改 UI 状态），只如实显示「已断开」。
  connect(metaTicket ? metaTicket.content : "");
}());
</script>
</body>
</html>
"""


def render_index(
    version: str,
    api_version: str,
    state_root: str,
    handshake_ticket: str,
    ws_path: str,
) -> str:
    """填占位符。除两个数值外都 `html.escape`，避免路径里的 `&` / `<>` 破坏页面。

    `handshake_ticket` 是内核自生成的 32 字节 hex（一次性、60 秒失效），仍然转义：
    成本为零，而且「外部输入一律转义」这个习惯不该有例外。
    """
    values: dict[str, str] = {
        "%%VERSION%%": html.escape(version),
        "%%API_VERSION%%": html.escape(api_version),
        "%%STATE_ROOT%%": html.escape(state_root),
        "%%HANDSHAKE_TICKET%%": html.escape(handshake_ticket),
        "%%WS_PATH%%": html.escape(ws_path),
        "%%HEARTBEAT_INTERVAL_MS%%": str(HEARTBEAT_INTERVAL_MS),
        "%%RECONNECT_DELAY_MS%%": str(RECONNECT_DELAY_MS),
    }
    assert set(values) == set(REQUIRED_PARAMS), "占位符清单与替换表不一致"
    page = INDEX_HTML
    for placeholder, value in values.items():
        page = page.replace(placeholder, value)
    return page
