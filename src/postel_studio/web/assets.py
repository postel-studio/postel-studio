"""静态页面。只是一个字符串常量，不引入前端构建链（S1 的禁止事项，D6 不变）。

S2 起页面里有内联 JS 与一次 WebSocket：`server.json` 里的 `wsPath` 浏览器读不到，所以
网关路径与**一次性 handshake ticket** 都由内核渲染页面时写进来（05 §1.1 的 S2 修订）。
页面里**没有**长期连接令牌。

前端「断线判定」只靠两种信号：socket 的 `close`/`error` 事件，以及 `HEARTBEAT_INTERVAL_MS`
到点还没收到回包（内核被 SIGKILL 时对端不会发 close 帧，只有心跳能发现）。两个参数都是
具名常量，且都远小于验收要求的 3 秒上限。

S3 起左栏出现「仓库」区（03-ui-spec §2：左栏上 Bot / 下 仓库；本刀只有仓库）。三条约束：

- 数据只从 WS 帧拿（`repo.list` / `repo.bind`），前端**不**直接读 git、也不开新 HTTP 路由
  （S3 D1）。
- **「没连上」与「没有仓库」必须可区分**：连上之前仓库区显示「正在加载」/「已断开」，
  只有在连接就绪且 `repo.list` 真的返回空数组时才显示「还没有绑定仓库」（D1 的可观测后果）。
- 错误是**可读中文**（`NOT_A_REPO` → 「不是 git 仓库」），由错误**码**映射，不看 `message`。
  错误码不在表里时显示通用文案 —— 不把服务端 `message` 直接塞进页面（05 §4.2 的同一精神）。

S4 起主区出现「分支」面板（D6 的 DOM 契约）：点左栏仓库行 → `repo.branches` → 分支列表。
四条约束：

- 左栏仓库行**键盘可达**：`tabindex="0"` + `role="button"`，`click` 与 `Enter`/`Space` 都能选中。
- 显隐**只由 CSS** 的 `#branches[data-state=…]` 决定，JS 只改 `data-state`（T8 的教训）。
- 断连回 `idle` 并**清空列表**（不许留旧数据装新鲜）；重连且仍有选中仓库时自动重取。
- 分支面板的错误兜底**不用** `ERROR_TEXT_FALLBACK`（那是绑定场景的「绑定失败」），
  面板自己给「读取分支列表失败」；`ERROR_TEXT` 复用，按错误码映射。
"""

from __future__ import annotations

import html
import json
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
    "%%ERROR_TEXT%%",
    "%%ERROR_TEXT_FALLBACK%%",
)

# 07 §1.3 的错误码 → 用户可读文案。**按码映射，不读 `message`**（07 §1.3：文案可改，码不能改）。
# 每个码有一句人话；表外的码走通用文案，不把服务端原文贴到页面上。
ERROR_TEXT: Final = {
    "NOT_A_REPO": "不是 git 仓库",
    "BAD_CWD": "请输入绝对路径（不能是相对路径或 ~ 开头的路径）",
    "INVALID_PARAMS": "路径参数不合法",
    "ADAPTER_UNAVAILABLE": "内核找不到 git 命令",
    "INTERNAL": "内核处理失败，请查看启动它的终端",
    # S4：repo.branches 会回这个码（repoId 查不到 / 绑定的路径已不是仓库）。
    # S3 的绑定表单用不到它，但码表是全局的，别让分支面板走到「绑定失败」兜底。
    "REPO_NOT_BOUND": "仓库未绑定或已不存在",
    # S5：repo.diff 会回这两个码（07 §1.3 既有码；不新增）。
    "BAD_REF": "分支或提交不存在",
    "NOT_SUPPORTED": "仓库含非 UTF-8 的名称，第一版不支持",
}
ERROR_TEXT_FALLBACK: Final = "绑定失败"

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
  /* 左栏仓库区（03-ui-spec §2）。判定一律走 `data-state`：**不用 JS 逐个开关 display**
     （06-pitfalls T8：`style.display = ""` 会回退到 CSS 的 `none`）。 */
  #repos { margin: 22px 0 0; padding-top: 18px; border-top: 1px solid #262b36; }
  #repos h2 { margin: 0 0 10px; font-size: 13px; font-weight: 600; letter-spacing: .08em;
    color: #8b95a7; text-transform: uppercase; }
  #bind-form { display: grid; grid-template-columns: 1fr auto; gap: 8px; }
  #bind-path { min-width: 0; padding: 6px 9px; font: inherit; font-size: 13px; color: #e6e9ef;
    background: #0f1115; border: 1px solid #39404f; border-radius: 6px; }
  #bind-path:focus { outline: none; border-color: #58627a; }
  #bind-path:disabled { opacity: .45; cursor: not-allowed; }
  #bind-button { padding: 6px 12px; font: inherit; font-size: 13px; color: #0f1115;
    background: #3fb950; border: 1px solid transparent; border-radius: 6px; cursor: pointer; }
  #bind-button:disabled { opacity: .45; cursor: progress; }
  #repos[data-state="disconnected"] #bind-form,
  #repos[data-state="loading"] #bind-form { pointer-events: none; }
  #repos[data-state="disconnected"] #bind-path,
  #repos[data-state="disconnected"] #bind-button,
  #repos[data-state="loading"] #bind-path,
  #repos[data-state="loading"] #bind-button { opacity: .45; }
  #repos-error { margin: 10px 0 0; padding: 7px 10px; font-size: 13px;
    color: #ffb4ab; background: rgb(248 81 73 / 12%); border: 1px solid rgb(248 81 73 / 45%);
    border-radius: 6px; }
  /* 列表与状态栏各管一件事：data-state 决定谁在场，`display` 在 CSS 里写死 */
  #repo-list { list-style: none; margin: 12px 0 0; padding: 0; }
  #repo-list li { padding: 7px 0; font-size: 14px; border-top: 1px solid #1f2430;
    cursor: pointer; }
  #repo-list li:first-child { border-top: none; }
  #repo-list li:hover { background: #1a1f2a; }
  /* S4：仓库行是可操作的按钮（D6），必须能看出"选中了"与"焦点在"。
     选中态只认 data-selected 属性（DOM 契约），样式挂在这个属性上。 */
  #repo-list li[data-selected="true"] { background: #1a1f2a; box-shadow: inset 2px 0 #3fb950; }
  #repo-list li[data-selected="true"] .repo-name { color: #3fb950; }
  #repo-list li:focus-visible { outline: 1px solid #58627a; outline-offset: 2px; }
  .repo-name { display: block; font-family: ui-monospace, "SF Mono", Menlo, monospace; }
  .repo-meta { display: block; margin-top: 2px; font-size: 12px; color: #8b95a7; }
  #repo-empty { margin: 12px 0 0; font-size: 13px; color: #8b95a7; }
  #repo-state { margin: 12px 0 0; font-size: 13px; color: #d29922; }
  #repos[data-state="disconnected"] #repo-state,
  #repos[data-state="loading"] #repo-state { display: block; }
  #repos[data-state="disconnected"] #repo-list,
  #repos[data-state="loading"] #repo-list { display: none; }
  #repos[data-state="disconnected"] #repo-empty,
  #repos[data-state="loading"] #repo-empty { display: none; }
  #repos[data-state="ready"] #repo-state { display: none; }
  /* 空列表是"连接着且真的没有仓库"才能说的话：`#repo-empty` 只在 ready 且无行时由 JS 打开 */
  #repos[data-state="disconnected"] #repos-error,
  #repos[data-state="disconnected"] #note-repos { display: none; }
  #note-repos { margin: 10px 0 0; font-size: 12px; color: #8b95a7; }
  /* 主区分支面板（S4 D6）。与 #repos 同一套机制：JS 只改 data-state / hidden，
     显隐规则全部在这里（06-pitfalls T8：不要逐个 style.display）。 */
  #branches { margin: 22px 0 0; padding-top: 18px; border-top: 1px solid #262b36; }
  #branches h2 { margin: 0 0 10px; font-size: 13px; font-weight: 600; letter-spacing: .08em;
    color: #8b95a7; text-transform: uppercase; }
  #branches-state { margin: 6px 0 0; font-size: 13px; color: #8b95a7; }
  #branches-error { margin: 10px 0 0; padding: 7px 10px; font-size: 13px;
    color: #ffb4ab; background: rgb(248 81 73 / 12%); border: 1px solid rgb(248 81 73 / 45%);
    border-radius: 6px; }
  #branch-list { list-style: none; margin: 12px 0 0; padding: 0; }
  #branch-list li { display: flex; align-items: baseline; gap: 10px; padding: 7px 0;
    font-size: 14px; border-top: 1px solid #1f2430; }
  #branch-list li:first-child { border-top: none; }
  .branch-name { font-family: ui-monospace, "SF Mono", Menlo, monospace; }
  .branch-sha { font-family: ui-monospace, "SF Mono", Menlo, monospace; font-size: 12px;
    color: #8b95a7; }
  .branch-upstream { font-size: 12px; color: #8b95a7; }
  .branch-current { margin-left: auto; padding: 1px 8px; font-size: 12px; color: #0f1115;
    background: #3fb950; border-radius: 999px; }
  #branch-empty { margin: 12px 0 0; font-size: 13px; color: #8b95a7; }
  /* 状态 → 可见性。idle / loading 只显示 #branches-state；error 只显示 #branches-error；
     ready 才允许 #branch-list 与（空仓库时的）#branch-empty 出现（D6）。 */
  #branches[data-state="idle"] #branch-list,
  #branches[data-state="idle"] #branch-empty,
  #branches[data-state="idle"] #branches-error,
  #branches[data-state="loading"] #branch-list,
  #branches[data-state="loading"] #branch-empty,
  #branches[data-state="loading"] #branches-error,
  #branches[data-state="ready"] #branches-state,
  #branches[data-state="error"] #branches-state,
  #branches[data-state="error"] #branch-list,
  #branches[data-state="error"] #branch-empty { display: none; }
  /* 主区差异面板（S5 D9）。与 #branches 同一套机制：JS 只改 data-state 与 hidden，
     显隐规则全部在这里（06-pitfalls T8：不要逐个 style.display）。 */
  #diff { margin: 22px 0 0; padding-top: 18px; border-top: 1px solid #262b36; }
  #diff h2 { margin: 0 0 10px; font-size: 13px; font-weight: 600; letter-spacing: .08em;
    color: #8b95a7; text-transform: uppercase; }
  #diff-form { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; }
  #diff-base, #diff-head { min-width: 0; flex: 1 1 12ch; padding: 6px 9px; font: inherit;
    font-size: 13px; color: #e6e9ef; background: #0f1115; border: 1px solid #39404f;
    border-radius: 6px; }
  #diff-base:focus, #diff-head:focus { outline: none; border-color: #58627a; }
  #diff-compare { padding: 6px 12px; font: inherit; font-size: 13px; color: #0f1115;
    background: #3fb950; border: 1px solid transparent; border-radius: 6px; cursor: pointer; }
  #diff-compare:disabled { opacity: .45; cursor: progress; }
  .diff-arrow { font-size: 13px; color: #8b95a7; }
  #diff-state { margin: 10px 0 0; font-size: 13px; color: #8b95a7; }
  #diff-error { margin: 10px 0 0; padding: 7px 10px; font-size: 13px; color: #ffb4ab;
    background: rgb(248 81 73 / 12%); border: 1px solid rgb(248 81 73 / 45%);
    border-radius: 6px; }
  #diff-truncated { margin: 10px 0 0; padding: 7px 10px; font-size: 13px; color: #0f1115;
    background: #d29922; border-radius: 6px; }
  #diff-stat { margin: 10px 0 0; font-size: 13px;
    font-family: ui-monospace, "SF Mono", Menlo, monospace; }
  /* 不设固定宽度、不假设中文宽度（D9）：等宽字体 + 可换行 + 自身滚动。 */
  #diff-patch { margin: 10px 0 0; max-height: 60vh; overflow: auto; white-space: pre-wrap;
    font-family: ui-monospace, "SF Mono", Menlo, monospace; font-size: 12px; line-height: 1.5; }
  .diff-add { color: #3fb950; }
  .diff-del { color: #f85149; }
  .diff-hunk { color: #58a6ff; }
  .diff-meta { color: #8b949e; }
  .diff-ctx { color: inherit; }
  /* 状态 → 可见性：idle / loading 只显示状态文案；error 只显示错误；ready 显示统计与 patch。
     hidden 属性管「空 patch / 未截断 / 无错误」这几个单元素条件（D9 的表）。 */
  #diff[data-state="idle"] #diff-stat,
  #diff[data-state="idle"] #diff-patch,
  #diff[data-state="idle"] #diff-truncated,
  #diff[data-state="loading"] #diff-stat,
  #diff[data-state="loading"] #diff-patch,
  #diff[data-state="loading"] #diff-truncated,
  #diff[data-state="ready"] #diff-state,
  #diff[data-state="error"] #diff-state,
  #diff[data-state="error"] #diff-stat,
  #diff[data-state="error"] #diff-patch,
  #diff[data-state="error"] #diff-truncated { display: none; }
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
  <!-- 主区分支面板（S4 D6）：位置在 #content 之后，不重排既有布局。
       初始 idle（还没选仓库）；显隐全部由上面的 #branches[data-state=…] 规则决定。 -->
  <section id="branches" data-state="idle" aria-live="polite">
    <h2 id="branches-title">分支</h2>
    <p id="branches-state">从左侧选择一个仓库</p>
    <p id="branches-error" role="alert" hidden></p>
    <ul id="branch-list"></ul>
    <p id="branch-empty" hidden>这个仓库还没有任何分支（空仓库）。</p>
  </section>
  <!-- 主区差异面板（S5 D9）：位置在 #branches 之后，不重排既有布局。
       显隐只由 #diff[data-state=…] 与 hidden 属性决定；JS 不碰 style.display。 -->
  <section id="diff" data-state="idle" aria-live="polite">
    <h2 id="diff-title">差异</h2>
    <form id="diff-form" autocomplete="off">
      <input id="diff-base" type="text" spellcheck="false" placeholder="base（分支或提交）">
      <span class="diff-arrow">→</span>
      <input id="diff-head" type="text" spellcheck="false" placeholder="head（分支或提交）">
      <button id="diff-compare" type="submit">对比</button>
    </form>
    <p id="diff-state">点左侧分支查看它与默认分支的差异，或在上方填两个 ref</p>
    <p id="diff-error" role="alert" hidden></p>
    <p id="diff-truncated" hidden>已截断：差异太大，只显示前一部分</p>
    <p id="diff-stat" hidden></p>
    <pre id="diff-patch" hidden></pre>
  </section>
  <!-- 左栏（03-ui-spec §2）。初始状态是「loading」而不是 ready：连上之前**没有资格**说
       「还没有仓库」（S3 D1：空 与 没连上 必须可区分）。 -->
  <section id="repos" data-state="loading" aria-live="polite">
    <h2>仓库</h2>
    <form id="bind-form" autocomplete="off">
      <input id="bind-path" type="text" spellcheck="false" placeholder="/绝对路径/到/仓库"
             aria-label="仓库路径">
      <button id="bind-button" type="submit">绑定</button>
    </form>
    <p id="repos-error" role="alert" hidden></p>
    <p id="repo-state">正在加载…</p>
    <ul id="repo-list"></ul>
    <p id="repo-empty" hidden>还没有绑定仓库，在上面的输入框里粘贴仓库路径。</p>
    <p id="note-repos">同一路径重复绑定不会产生第二行；粘贴子目录会绑定到仓库根。</p>
  </section>
</main>
<script>
(function () {
  "use strict";
  var HEARTBEAT_INTERVAL_MS = %%HEARTBEAT_INTERVAL_MS%%;  // 到点没回包就判死
  var RECONNECT_DELAY_MS = %%RECONNECT_DELAY_MS%%;        // 点「重连」到发起的间隔
  var CONNECT_TIMEOUT_MS = 2000;                         // 握手超时，也算失败
  // 错误码 → 可读中文（07 §1.3；由 render_index 从 ERROR_TEXT 注入，只含静态文案）
  var ERROR_TEXT = %%ERROR_TEXT%%;
  var ERROR_TEXT_FALLBACK = "%%ERROR_TEXT_FALLBACK%%";  // 静态常量，不来自服务端

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
  var pending = {};       // 请求 id → 回调（仓库区要拿 res 里的数据 / 错误码）
  var bindPending = false;

  // 仓库区元素
  var reposEl = document.getElementById("repos");
  var bindForm = document.getElementById("bind-form");
  var bindInput = document.getElementById("bind-path");
  var bindButton = document.getElementById("bind-button");
  var reposError = document.getElementById("repos-error");
  var repoState = document.getElementById("repo-state");
  var repoList = document.getElementById("repo-list");
  var repoEmpty = document.getElementById("repo-empty");

  // 分支面板元素（S4 D6）
  var branchesEl = document.getElementById("branches");
  var branchesTitle = document.getElementById("branches-title");
  var branchesState = document.getElementById("branches-state");
  var branchesError = document.getElementById("branches-error");
  var branchList = document.getElementById("branch-list");
  var branchEmpty = document.getElementById("branch-empty");
  // 分支面板**自己的**兜底：按码映射之外的码显示这句。**不要**用 ERROR_TEXT_FALLBACK——
  // 那是绑定表单的「绑定失败」，在分支面板里答"绑定失败"是错的。
  var BRANCH_ERROR_FALLBACK = "读取分支列表失败";
  var selectedRepoId = null;      // 当前选中的仓库（断连时保留：重连后自动重取，D6）
  var selectedRepoName = "";
  var branchRequestToken = 0;     // 只认最后一次分支请求的回包（切仓库/断连时让旧回包作废）

  // 差异面板元素（S5 D9）
  var diffEl = document.getElementById("diff");
  var diffTitle = document.getElementById("diff-title");
  var diffForm = document.getElementById("diff-form");
  var diffBase = document.getElementById("diff-base");
  var diffHead = document.getElementById("diff-head");
  var diffState = document.getElementById("diff-state");
  var diffError = document.getElementById("diff-error");
  var diffTruncated = document.getElementById("diff-truncated");
  var diffStat = document.getElementById("diff-stat");
  var diffPatch = document.getElementById("diff-patch");
  // 差异面板**自己的**兜底（同分支面板的纪律：不能答"绑定失败"）。
  var DIFF_ERROR_FALLBACK = "读取差异失败";
  var DIFF_HINT = "点左侧分支查看它与默认分支的差异，或在上方填两个 ref";
  var repoDefaultBranches = {};   // repoId → 默认分支（点分支行时 base 的来源；null = HEAD 游离）
  var selectedBranchName = null;  // 当前点选的分支行（分支列表重建后恢复 data-selected）
  var diffRequestToken = 0;       // 只认最后一次 diff 请求的回包（同 branchRequestToken）

  function setState(state, text) {
    statusEl.dataset.state = state;
    statusText.textContent = text;
    button.style.display = state === "disconnected" ? "inline-block" : "none";
    button.disabled = false;
    content.classList.toggle("stale", state !== "connected");
    setRepoState(state, text);
  }

  // 「没连上」≠「没有仓库」（D1）：连接没就绪时只显示状态文案，**不**显示空列表提示。
  // 具体的显隐由 CSS 的 `#repos[data-state=…]` 决定；这里只改一个属性（T8：别逐个 style.display）。
  function setRepoState(state, text) {
    if (state === "loading") { repoState.textContent = "正在加载…"; }
    else if (state === "reconnecting") { repoState.textContent = "正在连接内核…"; }
    else if (state === "connected") { repoState.textContent = "已连接"; }
    else { repoState.textContent = "已断开：内核没有连着，仓库列表未知"; }
    reposEl.dataset.state = state === "connected" ? "ready" : state;
    if (state === "connected") {
      // 连接恢复：差异面板若还停在 idle（断连时已被清空），把「已断开」文案换回提示。
      // **不**自动重发 diff（D9：diff 是查询历史动作，重放会覆盖用户正在看的对比）。
      if (diffEl.dataset.state === "idle") { diffState.textContent = DIFF_HINT; }
      loadRepos(); return;
    }
    repoEmpty.hidden = true;
    reposError.hidden = true;
    bindInput.disabled = true;
    bindButton.disabled = true;
    // S4 D6：断连/重连中分支面板回 idle 并**清空列表**——不许留着旧数据装新鲜。
    // 选中仓库保留（不清 selectedRepoId）：重连后 renderRepos 会沿 loadRepos 同一条路径重取。
    resetBranches("idle", state === "disconnected"
      ? "已断开：分支列表未知"
      : "正在连接内核…");
    // S5 D9：差异面板同一纪律——断连回 idle + 「已断开：差异未知」+ 清空，不留旧 diff。
    resetDiff("idle", state === "disconnected" ? "已断开：差异未知" : "正在连接内核…");
  }

  // 可读中文错误（07 §1.3 按码映射；表外的码给通用文案，不把服务端 message 贴到页面上）
  function errorText(code) { return ERROR_TEXT[code] || ERROR_TEXT_FALLBACK; }

  function showRepoError(text) { reposError.textContent = text; reposError.hidden = false; }
  function clearRepoError() { reposError.textContent = ""; reposError.hidden = true; }

  // 一行仓库：名称 + 默认分支（验收 1 要求两个都要显示）。
  // S4 起整行可选中（D6）：tabindex + role=button，click 与 Enter/Space 都能选，
  // 选中时 data-selected="true"。文本一律 textContent（仓库名是用户数据）。
  function renderRepos(list) {
    repoList.textContent = "";               // 先清空再重建，避免旧行残留
    for (var i = 0; i < list.length; i++) {
      var repo = list[i] || {};
      var repoId = String(repo.repoId || "");
      var repoName = String(repo.name || "（未命名）");
      // S5 D9：点分支行时 base = 仓库 defaultBranch。存在这里而不是 selectRepo 的参数上，
      // 是为了不改 S4 的既有行结构/回调形状。
      repoDefaultBranches[repoId] = repo.defaultBranch || null;
      var li = document.createElement("li");
      li.dataset.repoId = repoId;
      li.tabIndex = 0;                       // 键盘可达：Tab 能聚焦，Enter/Space 能选
      li.setAttribute("role", "button");
      var name = document.createElement("span");
      name.className = "repo-name";
      name.textContent = repoName;
      var meta = document.createElement("span");
      meta.className = "repo-meta";
      meta.textContent = "默认分支 " + (repo.defaultBranch || "（未知）");
      li.appendChild(name);
      li.appendChild(meta);
      // 闭包捕获当前行的 id/name：回调里不能用循环变量（var 是函数作用域）
      (function (rowId, rowName) {
        li.addEventListener("click", function () { selectRepo(rowId, rowName); });
        li.addEventListener("keydown", function (ev) {
          if (ev.key === "Enter" || ev.key === " " || ev.key === "Spacebar") {
            ev.preventDefault();             // 防 Space 滚动页面
            selectRepo(rowId, rowName);
          }
        });
      }(repoId, repoName));
      repoList.appendChild(li);
    }
    // 只有「真的拿到了一个（可能为空的）列表」时才能说「还没有仓库」
    repoEmpty.hidden = list.length !== 0;
    // 列表是整体重建的：**必须恢复选中标记**，否则每次 repo.list 都会把选中样式冲掉（D6）。
    var stillListed = false;
    for (var j = 0; j < list.length; j++) {
      if (String((list[j] || {}).repoId || "") === selectedRepoId) { stillListed = true; break; }
    }
    if (!stillListed) {
      // 选中的仓库从列表里消失（解绑 / 换了库）：回 idle 并清空（D6）
      selectedRepoId = null;
      selectedRepoName = "";
      resetBranches("idle", "从左侧选择一个仓库");
      resetDiff("idle", DIFF_HINT);   // S5 D9：差异面板一起清空，不留上一个仓库的对比
      return;
    }
    applyRepoSelection();
    loadBranches();   // 重连且仍有选中仓库 → 与 loadRepos 同一条路径自动重取（D6）
  }

  function loadRepos() {
    bindInput.disabled = false;
    bindButton.disabled = false;
    if (!request("repo.list", {}, function (err, data) {
      if (err) { showRepoError("读取仓库列表失败（" + err + "）"); return; }
      renderRepos(Array.isArray(data) ? data : []);
    })) { showRepoError("连接尚未就绪"); }
  }

  // -------------------------------------------------------------------------
  // 分支面板（S4 D6）。三条纪律：
  //   1. 显隐只由 CSS 的 `#branches[data-state=…]` 决定，这里只改 data-state 与 hidden（T8）；
  //   2. 断连时回 idle 并清空（resetBranches），选中仓库保留给重连自动重取；
  //   3. 快速切仓库时旧回包不能覆盖新结果（branchRequestToken）。
  // -------------------------------------------------------------------------

  function branchErrorText(code) {
    return ERROR_TEXT[code] || BRANCH_ERROR_FALLBACK;   // 表外的码不显示"绑定失败"
  }

  function branchesTitleText() {
    return selectedRepoId ? ("分支 · " + selectedRepoName) : "分支";
  }

  function resetBranches(state, text) {
    branchRequestToken += 1;            // 让在途请求的回包作废
    branchesEl.dataset.state = state;
    branchesTitle.textContent = branchesTitleText();
    branchesState.textContent = text;
    branchList.textContent = "";
    branchesError.textContent = "";
    branchesError.hidden = true;
    branchEmpty.hidden = true;
  }

  function applyRepoSelection() {
    var rows = repoList.querySelectorAll("li[data-repo-id]");
    for (var i = 0; i < rows.length; i++) {
      if (selectedRepoId !== null && rows[i].dataset.repoId === selectedRepoId) {
        rows[i].dataset.selected = "true";
        rows[i].setAttribute("aria-current", "true");
      } else {
        delete rows[i].dataset.selected;
        rows[i].removeAttribute("aria-current");
      }
    }
  }

  // 一行分支：.branch-name / .branch-sha（8 位）/ .branch-current（仅当前，文本「当前」）/
  // .branch-upstream（**有** upstream 才创建；null 时连 span 都不建，不放占位符）。
  function renderBranches(list) {
    branchList.textContent = "";
    for (var i = 0; i < list.length; i++) {
      var branch = list[i] || {};
      var branchName = String(branch.name || "");
      var li = document.createElement("li");
      li.dataset.branchName = branchName;
      // S5 D9：分支行键盘可达（与左栏仓库行一致）；click 与 Enter/Space 走同一条路径。
      li.tabIndex = 0;
      li.setAttribute("role", "button");
      (function (rowName) {
        li.addEventListener("click", function () { selectBranch(rowName); });
        li.addEventListener("keydown", function (ev) {
          if (ev.key === "Enter" || ev.key === " " || ev.key === "Spacebar") {
            ev.preventDefault();     // 防 Space 滚动页面
            selectBranch(rowName);
          }
        });
      }(branchName));
      var name = document.createElement("span");
      name.className = "branch-name";
      name.textContent = branchName;
      var sha = document.createElement("span");
      sha.className = "branch-sha";
      sha.textContent = String(branch.sha || "");
      li.appendChild(name);
      li.appendChild(sha);
      if (branch.upstream) {
        var upstream = document.createElement("span");
        upstream.className = "branch-upstream";
        upstream.textContent = "上游 " + String(branch.upstream);
        li.appendChild(upstream);
      }
      if (branch.isCurrent) {
        var current = document.createElement("span");
        current.className = "branch-current";
        current.textContent = "当前";
        li.appendChild(current);
      }
      branchList.appendChild(li);
    }
    applyBranchSelection();   // 列表整体重建：恢复选中标记（同 renderRepos 的纪律）
  }

  function loadBranches() {
    if (selectedRepoId === null) { return; }
    var repoId = selectedRepoId;
    var token = ++branchRequestToken;
    branchesTitle.textContent = branchesTitleText();
    branchesEl.dataset.state = "loading";
    branchesState.textContent = "正在读取分支…";
    branchList.textContent = "";        // 换仓库时先清掉上一个仓库的分支，不混着显示
    branchesError.textContent = "";
    branchesError.hidden = true;
    branchEmpty.hidden = true;
    var id = request("repo.branches", { repoId: repoId }, function (err, data) {
      if (token !== branchRequestToken) { return; }   // 过期回包：切了仓库或已断连
      if (err) {
        branchesEl.dataset.state = "error";
        branchesError.textContent = branchErrorText(err);
        branchesError.hidden = false;
        return;
      }
      var rows = Array.isArray(data) ? data : [];
      renderBranches(rows);
      branchesEl.dataset.state = "ready";
      // 空仓库：ready + 空态（**不是** error、**不是**空白，D6）
      branchEmpty.hidden = rows.length !== 0;
    });
    if (id === null) {                  // 连接没就绪：不装成"这个仓库没有分支"
      if (token === branchRequestToken) { resetBranches("idle", "连接尚未就绪"); }
    }
  }

  // -------------------------------------------------------------------------
  // 差异面板（S5 D9）。三条纪律与分支面板同源：
  //   1. 显隐只由 CSS 的 `#diff[data-state=…]` 与 hidden 属性决定，这里只改这两样（T8）；
  //   2. 断连 / 切仓库回 idle 并清空（resetDiff），不留旧 diff 装新鲜；
  //   3. 快速切换时旧回包不能覆盖新结果（diffRequestToken）。
  // diff 文本一律 textContent：文件名可能是 `<script>`，它是**不可信文本**（05 §4.2 同一精神）。
  // -------------------------------------------------------------------------

  function diffErrorText(code) {
    return ERROR_TEXT[code] || DIFF_ERROR_FALLBACK;   // 表外的码不显示「绑定失败」
  }

  function diffTitleText() {
    return selectedRepoId ? ("差异 · " + selectedRepoName) : "差异";
  }

  // 清内容但**不动**分支行选中：点分支 / 发起请求时用；resetDiff 才连选中一起清。
  function clearDiffContent(state, text) {
    diffRequestToken += 1;            // 让在途请求的回包作废
    diffEl.dataset.state = state;
    diffTitle.textContent = diffTitleText();
    diffState.textContent = text;
    diffError.textContent = "";
    diffError.hidden = true;
    diffTruncated.hidden = true;
    diffStat.textContent = "";
    diffStat.hidden = true;
    diffPatch.textContent = "";
    diffPatch.hidden = true;
  }

  function applyBranchSelection() {
    var rows = branchList.querySelectorAll("li[data-branch-name]");
    for (var i = 0; i < rows.length; i++) {
      if (selectedBranchName !== null && rows[i].dataset.branchName === selectedBranchName) {
        rows[i].dataset.selected = "true";
        rows[i].setAttribute("aria-current", "true");
      } else {
        delete rows[i].dataset.selected;
        rows[i].removeAttribute("aria-current");
      }
    }
  }

  function resetDiff(state, text) {   // 切仓库 / 断连 / 清空：连分支行选中一起清
    selectedBranchName = null;
    clearDiffContent(state, text);
    applyBranchSelection();
  }

  // D9 的行分类顺序**必须照这个顺序判**：`+++ `/`--- ` 是文件头，不能算 add/del；
  // `+`/`-` 前缀不能先判。`\\` 开头是 "\\ No newline at end of file" 这种元信息行。
  function diffLineKind(line) {
    if (line.indexOf("diff --git ") === 0 || line.indexOf("index ") === 0
      || line.indexOf("new file mode ") === 0 || line.indexOf("deleted file mode ") === 0
      || line.indexOf("old mode ") === 0 || line.indexOf("new mode ") === 0
      || line.indexOf("\\\\") === 0) { return "meta"; }
    if (line.indexOf("@@") === 0) { return "hunk"; }
    if (line.indexOf("+++ ") === 0 || line.indexOf("--- ") === 0) { return "meta"; }
    if (line.indexOf("+") === 0) { return "add"; }
    if (line.indexOf("-") === 0) { return "del"; }
    return "ctx";
  }

  function renderDiffPatch(patch) {
    diffPatch.textContent = "";       // 重建前先扔掉旧行
    var lines = patch.split("\\n");
    for (var i = 0; i < lines.length; i++) {
      if (lines[i] === "" && i === lines.length - 1) { continue; }  // 末尾换行不算内容
      var row = document.createElement("div");
      row.className = "diff-line diff-" + diffLineKind(lines[i]);
      row.textContent = lines[i];     // 不可信文本：只进 textContent
      diffPatch.appendChild(row);
    }
  }

  function diffStatText(stat) {
    return String(stat.filesChanged || 0) + " 个文件 · +" + String(stat.insertions || 0)
      + " −" + String(stat.deletions || 0);
  }

  function requestDiff(base, head) {
    if (selectedRepoId === null) { return; }
    var repoId = selectedRepoId;
    var token = ++diffRequestToken;
    diffEl.dataset.state = "loading";
    diffTitle.textContent = diffTitleText();
    diffState.textContent = "正在读取差异…";
    diffError.textContent = "";
    diffError.hidden = true;
    diffTruncated.hidden = true;
    diffStat.textContent = "";
    diffStat.hidden = true;
    diffPatch.textContent = "";
    diffPatch.hidden = true;
    var id = request("repo.diff", { repoId: repoId, base: base, head: head },
      function (err, data) {
        if (token !== diffRequestToken) { return; }   // 过期回包：切了仓库 / 断连 / 换了对比
        if (err) {
          diffEl.dataset.state = "error";
          diffError.textContent = diffErrorText(err);  // 按码映射，不看 message
          diffError.hidden = false;
          return;
        }
        var payload = data || {};
        var stat = payload.stat || {};
        var patch = typeof payload.patch === "string" ? payload.patch : "";
        diffEl.dataset.state = "ready";
        diffStat.textContent = diffStatText(stat);
        diffStat.hidden = false;
        renderDiffPatch(patch);
        diffPatch.hidden = patch === "";               // 空差异只显示统计（D6）
        diffTruncated.hidden = payload.truncated !== true;
      });
    if (id === null) {                                 // 连接没就绪：不装成"正在读"
      if (token === diffRequestToken) { resetDiff("idle", "连接尚未就绪"); }
    }
  }

  // 点分支行（S5 D9）：填两个输入框 → 默认分支本身 / 无默认分支时不发请求。
  function selectBranch(name) {
    if (selectedRepoId === null) { return; }
    var defaultBranch = repoDefaultBranches[selectedRepoId] || null;
    selectedBranchName = name;
    diffBase.value = defaultBranch === null ? "" : defaultBranch;
    diffHead.value = name;
    clearDiffContent("idle", DIFF_HINT);
    applyBranchSelection();
    if (defaultBranch === null) {
      diffState.textContent = "不知道默认分支（HEAD 未指向分支），请在上方填两个 ref";
      return;
    }
    if (defaultBranch === name) {
      diffState.textContent = "这是默认分支，选择其它分支查看差异";
      return;
    }
    requestDiff(defaultBranch, name);
  }

  // 手动提交（S5 D9）：trim 后任一为空 → #diff-error 显示提示、data-state 保持原样。
  function submitDiff() {
    var base = diffBase.value.trim();
    var head = diffHead.value.trim();
    if (!base || !head) {
      diffError.textContent = "请填 base 和 head";
      diffError.hidden = false;
      return;
    }
    if (selectedRepoId === null) {
      diffError.textContent = "先选择一个仓库";
      diffError.hidden = false;
      return;
    }
    requestDiff(base, head);
  }

  function selectRepo(repoId, name) {
    if (!repoId) { return; }
    selectedRepoId = repoId;
    selectedRepoName = name;
    applyRepoSelection();
    // S5 D9：切仓库 → 差异面板回 idle 并清空 patch/stat/error/选中行，标题跟着仓库名变。
    // 显式重选同一个仓库也会清空（用户正在看的对比就此结束，不静默留着旧结果）。
    resetDiff("idle", DIFF_HINT);
    loadBranches();
  }

  function submitBind() {
    var path = bindInput.value.trim();
    clearRepoError();
    if (!path) { showRepoError("请输入仓库路径"); return; }
    if (bindPending) { return; }
    bindPending = true;
    bindButton.disabled = true;
    var id = request("repo.bind", { path: path }, function (err, data) {
      bindPending = false;
      bindButton.disabled = reposEl.dataset.state !== "ready";
      if (err) { showRepoError(errorText(err)); return; }   // 可读中文，不是 console.warn
      bindInput.value = "";
      if (data && typeof data.name === "string") { repoEmpty.hidden = true; }
      loadRepos();                                          // 重读列表：重复绑定靠它去重
    });
    if (id === null) {
      bindPending = false;
      bindButton.disabled = false;
      showRepoError("连接尚未就绪");
    }
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
    pending = {};            // 断连后没人会回这些包了，别让回调永远挂着
    bindPending = false;
    setState("disconnected", "已断开");
  }

  function send(method, params) {
    if (!socket || socket.readyState !== 1) { return null; }
    var id = "c" + (++seq);
    try { socket.send(JSON.stringify({ type: "req", id: id, method: method, params: params })); }
    catch (e) { return null; }
    return id;
  }

  // 一次带回调的请求：成功回调拿 `data`，失败回调拿**错误码**（不拿 message → 07 §1.3）
  function request(method, params, done) {
    var id = send(method, params);
    if (id === null) { return null; }
    pending[id] = done;
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
    if (!frame || frame.type !== "res" || !frame.id || !pending[frame.id]) { return; }
    var done = pending[frame.id];
    delete pending[frame.id];
    if (frame.ok) {
      var result = frame.result || {};
      done(null, result.data);
    } else {
      var code = (frame.error && frame.error.code) || "INTERNAL";
      done(String(code), null);
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
  bindForm.addEventListener("submit", function (ev) { ev.preventDefault(); submitBind(); });
  diffForm.addEventListener("submit", function (ev) { ev.preventDefault(); submitDiff(); });
  // 断连期间**不自动重连**（禁止事项：不得静默重试而不改 UI 状态），只如实显示「已断开」。
  connect(metaTicket ? metaTicket.content : "");
}());
</script>
</body>
</html>
"""


def error_text_json() -> str:
    """把 `ERROR_TEXT` 序列化成 **JS 字面量**（`json.dumps` 的输出就是合法 JS 对象）。

    走占位符而不是把文案硬写在 JS 里：错误码与文案的对照只有一处（Python 侧可被测试读到），
    页面不必在两种语言里各维护一份映射。`ensure_ascii=False` 让中文在源码里可读。
    """
    return json.dumps(ERROR_TEXT, ensure_ascii=False)


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
        "%%ERROR_TEXT%%": error_text_json(),
        # 静态文案（本模块常量），不来自服务端；仍按「外部输入一律转义」的习惯转义
        "%%ERROR_TEXT_FALLBACK%%": html.escape(ERROR_TEXT_FALLBACK),
    }
    assert set(values) == set(REQUIRED_PARAMS), "占位符清单与替换表不一致"
    page = INDEX_HTML
    for placeholder, value in values.items():
        page = page.replace(placeholder, value)
    return page
