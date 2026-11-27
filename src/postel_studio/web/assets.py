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

S6 起左栏出现「Bots」区、主区出现只读的「Charter」面板：点仓库行 → `bot.list` 列出该仓库
的 Bot（M1 由内核惰性播种 `builder`）→ 点 Bot 行 → `memory.charter.get` 显示 charter 全文与
记忆目录绝对路径（D8 的 DOM 契约）。charter 是**用户可编辑文本 = 不可信**，全文只进一个
`<pre>` 的 `textContent`。四态与 token 纪律与分支/差异面板同源。

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
    # S7：worktree.acquire / release 会回这两个码。
    # - BAD_BASE_REF 是 07 §1.3 里本来就有的码（"worktree 基础分支无效"），本刀第一次用到；
    # - WORKTREE_CONFLICT 是本刀**新增**的码（三处触发语义相同：需要人先处理目录里的内容）。
    "BAD_BASE_REF": "基础分支不存在或不是提交",
    "WORKTREE_CONFLICT": "工作区目录被占用，或里面有未提交的改动",
    # S10：task.dispatch 会回这两个码（07 §1.3 既有码，本刀第一次用到）。
    "BUSY": "这个 Bot 已有任务在跑，等它结束再派",
    "PERMISSION_DENIED": "这个 Bot 是 L0（只读），先在 Charter 里把权限改到 L1 以上",
}
ERROR_TEXT_FALLBACK: Final = "绑定失败"

INDEX_HTML: Final = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="icon" href="data:,">
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
  /* 左栏 Bots 区与主区 charter 面板（S6 D8）。与 #branches/#diff 同一套机制：显隐只由
     `data-state` 与 hidden 属性决定，JS 不碰 style.display（T8）。charter 全文用 pre-wrap，
     不设固定宽度、不假设中文宽度（等宽 + 自身滚动）。 */
  #bots, #charter { margin: 22px 0 0; padding-top: 18px; border-top: 1px solid #262b36; }
  #bots h2, #charter h2 { margin: 0 0 10px; font-size: 13px; font-weight: 600;
    letter-spacing: .08em; color: #8b95a7; text-transform: uppercase; }
  #bots-state, #charter-state, #bot-empty { margin: 8px 0 0; font-size: 13px; color: #8b95a7; }
  #bots-error, #charter-error { margin: 10px 0 0; padding: 7px 10px; font-size: 13px;
    color: #ffb4ab; background: rgb(248 81 73 / 12%); border: 1px solid rgb(248 81 73 / 45%);
    border-radius: 6px; }
  #bot-list { list-style: none; margin: 12px 0 0; padding: 0; }
  #bot-list li { padding: 7px 0; font-size: 14px; border-top: 1px solid #1f2430; cursor: pointer; }
  #bot-list li:first-child { border-top: none; }
  #bot-list li:hover { background: #1a1f2a; }
  #bot-list li[data-selected="true"] { background: #1a1f2a; box-shadow: inset 2px 0 #3fb950; }
  #bot-list li:focus-visible { outline: 1px solid #58627a; outline-offset: 2px; }
  .bot-name { display: block; font-family: ui-monospace, "SF Mono", Menlo, monospace; }
  .bot-permission { display: inline-block; margin-right: 6px; padding: 0 6px; font-size: 11px;
    color: #8b95a7; border: 1px solid #39404f; border-radius: 999px; }
  .bot-charter-summary { display: block; margin-top: 2px; font-size: 12px; color: #8b95a7; }
  #bots[data-state="idle"] #bot-list,
  #bots[data-state="idle"] #bot-empty,
  #bots[data-state="idle"] #bot-new,
  #bots[data-state="loading"] #bot-list,
  #bots[data-state="loading"] #bot-empty,
  #bots[data-state="loading"] #bot-new,
  #bots[data-state="ready"] #bots-state,
  #bots[data-state="error"] #bots-state,
  #bots[data-state="error"] #bot-list,
  #bots[data-state="error"] #bot-new,
  #bots[data-state="error"] #bot-empty { display: none; }
  /* S8 D8：新建 Bot。按钮只在 ready 出现（没选仓库 / 读取中 / 出错时不装作能建）；表单开关只看
     `hidden`——`display: grid` 会盖掉 UA 的 `[hidden]`，所以这条 `[hidden]` 规则是**必须的**。 */
  #bot-new { margin-top: 12px; padding: 6px 12px; font: inherit; font-size: 13px;
    color: #e6e9ef; background: #1a1f2a; border: 1px solid #39404f; border-radius: 6px;
    cursor: pointer; }
  #bot-new:hover { border-color: #58627a; }
  #bot-new:focus-visible { outline: 1px solid #58627a; outline-offset: 2px; }
  #bot-create-form { margin: 10px 0 0; display: grid; gap: 8px; }
  #bot-create-form[hidden] { display: none; }
  #bot-create-name, #bot-create-permission, #charter-permission {
    min-width: 0; padding: 6px 8px; font: inherit; font-size: 13px; color: #e6e9ef;
    background: #0f1115; border: 1px solid #39404f; border-radius: 6px; }
  #bot-create-name:focus, #bot-create-permission:focus, #charter-permission:focus {
    outline: none; border-color: #58627a; }
  #bot-create-actions { display: flex; gap: 8px; }
  #bot-create-submit, #bot-create-cancel { padding: 6px 12px; font: inherit; font-size: 13px;
    border-radius: 6px; cursor: pointer; }
  #bot-create-submit { color: #0f1115; background: #3fb950; border: 1px solid #3fb950; }
  #bot-create-submit:disabled { opacity: .45; cursor: progress; }
  #bot-create-cancel { color: #e6e9ef; background: transparent; border: 1px solid #39404f; }
  #bot-create-error { margin: 0; padding: 7px 10px; font-size: 13px; color: #ffb4ab;
    background: rgb(248 81 73 / 12%); border: 1px solid rgb(248 81 73 / 45%); border-radius: 6px; }
  #charter-permission-row { margin: 8px 0 0; font-size: 13px; color: #8b95a7; }
  #charter-permission { margin-left: 8px; }
  #charter-memory { margin: 8px 0 0; font-size: 13px; color: #8b95a7;
    font-family: ui-monospace, "SF Mono", Menlo, monospace; word-break: break-all; }
  #charter-content { margin: 10px 0 0; max-height: 60vh; overflow: auto;
    white-space: pre-wrap; font-family: ui-monospace, "SF Mono", Menlo, monospace;
    font-size: 12px; line-height: 1.6; }
  #charter[data-state="idle"] #charter-memory,
  #charter[data-state="idle"] #charter-content,
  #charter[data-state="idle"] #charter-permission-row,
  #charter[data-state="loading"] #charter-memory,
  #charter[data-state="loading"] #charter-content,
  #charter[data-state="loading"] #charter-permission-row,
  #charter[data-state="ready"] #charter-state,
  #charter[data-state="error"] #charter-state,
  #charter[data-state="error"] #charter-memory,
  #charter[data-state="error"] #charter-permission-row,
  #charter[data-state="error"] #charter-content { display: none; }
  /* 工作区卡片（S7 D9）。显隐只由 `#workspace[data-state=…]` 与 `hidden` 决定（T8：不碰
     style.display）；路径 / 分支 / Bot 名都是用户数据，只进 textContent。 */
  #workspace { margin: 22px 0 0; padding-top: 18px; border-top: 1px solid #262b36; }
  #workspace h2 { margin: 0 0 10px; font-size: 13px; font-weight: 600;
    letter-spacing: .08em; color: #8b95a7; text-transform: uppercase; }
  #workspace-form { display: flex; gap: 8px; margin: 10px 0 0; }
  #workspace-base { flex: 1; min-width: 0; padding: 6px 8px; font: inherit; font-size: 13px;
    color: #e6e9ef; background: #0f1115; border: 1px solid #39404f; border-radius: 6px; }
  #workspace-acquire, #workspace-release { padding: 6px 12px; font: inherit; font-size: 13px;
    color: #e6e9ef; background: #1f2430; border: 1px solid #39404f; border-radius: 6px;
    cursor: pointer; }
  #workspace-acquire:hover, #workspace-release:hover { border-color: #58627a; }
  #workspace-acquire:disabled, #workspace-release:disabled { opacity: .5; cursor: progress; }
  #workspace-release { margin: 10px 0 0; }
  #workspace-state, #workspace-empty { margin: 8px 0 0; font-size: 13px; color: #8b95a7; }
  #workspace-error { margin: 10px 0 0; padding: 7px 10px; font-size: 13px; color: #ffb4ab;
    background: rgb(248 81 73 / 12%); border: 1px solid rgb(248 81 73 / 45%); border-radius: 6px; }
  #workspace-bot, #workspace-path, #workspace-branch { margin: 6px 0 0; font-size: 13px;
    font-family: ui-monospace, "SF Mono", Menlo, monospace; word-break: break-all; }
  #workspace[data-state="idle"] #workspace-form,
  #workspace[data-state="idle"] #workspace-details,
  #workspace[data-state="idle"] #workspace-bot,
  #workspace[data-state="idle"] #workspace-empty,
  #workspace[data-state="loading"] #workspace-form,
  #workspace[data-state="loading"] #workspace-details,
  #workspace[data-state="loading"] #workspace-bot,
  #workspace[data-state="loading"] #workspace-empty,
  #workspace[data-state="ready"] #workspace-state,
  #workspace[data-state="error"] #workspace-state,
  #workspace[data-state="error"] #workspace-empty { display: none; }
  /* 注意：error 状态**不隐藏** `#workspace-details`。回收被拒（脏工作区）之后，用户必须还能
     看见路径、并**再点一次**「回收」——把按钮藏起来等于让人去刷新页面（真实浏览器验收抓到
     的第一版缺陷）。没有工作区时 `#workspace-details` 靠 `hidden` 属性隐藏，与这里无关。 */
  /* Agent 通道卡片（S9 D9）。四态只由 `#agent[data-state=…]` 与 `hidden` 决定（T8：不碰
     style.display）；版本号与能力名都是 Agent 自己报的，只进 textContent。 */
  #agent { margin: 22px 0 0; padding-top: 18px; border-top: 1px solid #262b36; }
  #agent h2 { margin: 0 0 10px; font-size: 13px; font-weight: 600;
    letter-spacing: .08em; color: #8b95a7; text-transform: uppercase; }
  #agent-state { margin: 8px 0 8px; font-size: 13px; color: #8b95a7; }
  #agent-detect { padding: 6px 12px; font: inherit; font-size: 13px; color: #e6e9ef;
    background: #1f2430; border: 1px solid #39404f; border-radius: 6px; cursor: pointer; }
  #agent-detect:hover { border-color: #58627a; }
  #agent-detect:disabled { opacity: .5; cursor: progress; }
  #agent-error { margin: 10px 0 0; padding: 7px 10px; font-size: 13px; color: #ffb4ab;
    background: rgb(248 81 73 / 12%); border: 1px solid rgb(248 81 73 / 45%); border-radius: 6px; }
  #agent-detail { margin: 10px 0 0; }
  #agent-kind, #agent-version, #agent-protocol, #agent-elapsed { margin: 4px 0 0;
    font-size: 13px; font-family: ui-monospace, "SF Mono", Menlo, monospace;
    word-break: break-all; }
  #agent-flags-title { margin: 10px 0 0; font-size: 13px; color: #8b95a7; }
  #agent-flags { margin: 4px 0 0; padding-left: 20px; font-size: 13px;
    font-family: ui-monospace, "SF Mono", Menlo, monospace; }
  #agent-flags li { margin: 2px 0; }
  #agent[data-state="idle"] #agent-detail,
  #agent[data-state="loading"] #agent-detail,
  #agent[data-state="error"] #agent-detail,
  #agent[data-state="ready"] #agent-state { display: none; }
  /* 派活卡片（S10）：表单在 idle 不可见（还没选 Bot），running 时禁止再派（BUSY 由内核
     兜底，但页面先不给入口）；取消按钮只在 running 出现；details（taskId + 状态行）只在
     ready/running/done 出现。error 状态**不隐藏** details：失败后用户要能看见是哪个任务
     失败的，并直接再派一次。 */
  #task-form { display: flex; gap: 8px; margin-top: 8px; }
  #task-prompt { flex: 1; padding: 7px 10px; font-size: 14px; color: inherit;
    background: #1f2430; border: 1px solid #39404f; border-radius: 6px; }
  #task-dispatch, #task-cancel { padding: 7px 14px; font-size: 14px; color: inherit;
    background: #1f2430; border: 1px solid #39404f; border-radius: 6px; cursor: pointer; }
  #task-dispatch:hover, #task-cancel:hover { border-color: #58627a; }
  #task-dispatch:disabled { opacity: .5; cursor: progress; }
  #task-cancel { border-color: rgb(248 81 73 / 55%); color: #ffb4ab; }
  #task-error { margin: 10px 0 0; padding: 7px 10px; font-size: 13px; color: #ffb4ab;
    background: rgb(248 81 73 / 12%); border: 1px solid rgb(248 81 73 / 45%); border-radius: 6px; }
  #task-details { margin: 10px 0 0; }
  #task-id, #task-status-line { margin: 4px 0 0; font-size: 13px;
    font-family: ui-monospace, "SF Mono", Menlo, monospace; word-break: break-all; }
  #task[data-state="idle"] #task-form,
  #task[data-state="idle"] #task-details,
  #task[data-state="running"] #task-form,
  #task[data-state="ready"] #task-cancel,
  #task[data-state="done"] #task-cancel,
  #task[data-state="error"] #task-cancel { display: none; }
  /* 候选详情卡（S11）：只跟选中的仓库/最近一次任务，单条详情，不是列表页（S12 才是）。
     状态由 data-state 决定；打开 diff 复用 #diff 面板，不在这里画第二份。 */
  #candidate-detail { margin-top: 8px; }
  #candidate-branch, #candidate-stat { margin: 4px 0 0; font-size: 13px;
    font-family: ui-monospace, "SF Mono", Menlo, monospace; word-break: break-all; }
  #candidate-error { margin: 8px 0 0; padding: 7px 10px; font-size: 13px; color: #ffb4ab;
    background: rgb(248 81 73 / 12%); border: 1px solid rgb(248 81 73 / 45%); border-radius: 6px; }
  #candidate-open { margin-top: 8px; padding: 7px 14px; font-size: 14px; color: inherit;
    background: #1f2430; border: 1px solid #39404f; border-radius: 6px; cursor: pointer; }
  #candidate[data-state="idle"] #candidate-detail,
  #candidate[data-state="idle"] #candidate-error,
  #candidate[data-state="loading"] #candidate-detail { display: none; }
  /* 变更收件箱（S12）：入口在左栏仓库条目下，列表在主区。状态机只有 CSS 决定显隐。 */
  #inbox[data-state="idle"] #inbox-body,
  #inbox[data-state="idle"] #inbox-error,
  #inbox[data-state="loading"] #inbox-body,
  #inbox[data-state="error"] #inbox-body,
  #inbox[data-state="empty"] #inbox-error,
  #inbox[data-state="ready"] #inbox-empty { display: none; }
  #inbox[data-state="idle"] #inbox-filter,
  #inbox[data-state="empty"] #inbox-list { display: none; }
  #inbox-list { margin: 8px 0 0; padding: 0; list-style: none; }
  #inbox-list li { margin: 0 0 6px; padding: 8px 10px; border: 1px solid #39404f;
    border-radius: 6px; cursor: pointer; }
  #inbox-list li:hover, #inbox-list li:focus { background: #1f2430; outline: none; }
  .inbox-title { font-size: 14px; }
  .inbox-meta { display: block; margin-top: 3px; font-size: 12px; color: #aab2c0; }
  .inbox-state { margin-left: 8px; padding: 1px 6px; font-size: 11px;
    border: 1px solid #39404f; border-radius: 9px; }
  #inbox-filter { margin-left: 10px; padding: 4px 8px; font-size: 13px;
    color: inherit; background: #1f2430; border: 1px solid #39404f; border-radius: 6px; }
  #inbox-error { margin: 8px 0 0; padding: 7px 10px; font-size: 13px; color: #ffb4ab;
    background: rgb(248 81 73 / 12%); border: 1px solid rgb(248 81 73 / 45%); border-radius: 6px; }
  .repo-inbox-link { margin-left: 8px; font-size: 11px; color: #58a6ff; cursor: pointer; }
  .repo-inbox-link:hover, .repo-inbox-link:focus { text-decoration: underline; outline: none; }
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
  <!-- Agent 通道卡片（S9 D9）：放在 #content 之后、仓库面板之前——它是**内核级**能力，
       不属于任何选中的仓库。初始 idle；显隐由 #agent[data-state=…] 与 hidden 决定。 -->
  <section id="agent" data-state="idle" aria-live="polite">
    <h2 id="agent-title">Agent 通道</h2>
    <p id="agent-state">点「检测 Agent」看本机有没有可用的 Agent</p>
    <button id="agent-detect" type="button">检测 Agent</button>
    <p id="agent-error" role="alert" hidden></p>
    <div id="agent-detail" hidden>
      <p id="agent-kind"></p>
      <p id="agent-version"></p>
      <p id="agent-protocol"></p>
      <p id="agent-elapsed"></p>
      <p id="agent-flags-title">能力清单（来自这次握手应答）</p>
      <ul id="agent-flags"></ul>
    </div>
  </section>
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
  <!-- 主区 charter 面板（S6 D8）：位置在 #diff 之后，不重排既有布局。charter 是用户可
       编辑文本 = 不可信：全文只进一个 <pre> 的 textContent（绝不拼 HTML）。 -->
  <section id="charter" data-state="idle" aria-live="polite">
    <h2 id="charter-title">Charter</h2>
    <p id="charter-state">点左侧的 Bot 查看它的 charter</p>
    <p id="charter-error" role="alert" hidden></p>
    <!-- S8 D8：权限是人的入口（05 §2.3：Bot 永不能改自己的级别）。四个选项与新建表单同源；
         显隐只由 #charter 的 data-state 决定。 -->
    <p id="charter-permission-row">权限<select id="charter-permission" aria-label="权限">
        <option value="L0">L0 · 只读</option>
        <option value="L1">L1 · 改工作区（默认）</option>
        <option value="L2">L2 · 推分支</option>
        <option value="L2+">L2+ · 开本仓库 PR</option>
      </select></p>
    <p id="charter-memory" hidden></p>
    <pre id="charter-content" hidden></pre>
  </section>
  <!-- 主区工作区卡片（S7 D9）：位置在 #charter 之后，不重排既有布局。分配是**显式动作**：
       选仓库只读 worktree.list，绝不自动建目录。路径 / 分支 / Bot 名都只进 textContent。 -->
  <section id="workspace" data-state="idle" aria-live="polite">
    <h2 id="workspace-title">工作区</h2>
    <p id="workspace-state">选择一个仓库后分配工作区</p>
    <form id="workspace-form" autocomplete="off">
      <input id="workspace-base" type="text" spellcheck="false"
             placeholder="基础分支（缺省用默认分支）" aria-label="基础分支">
      <button id="workspace-acquire" type="submit" disabled>分配工作区</button>
    </form>
    <p id="workspace-error" role="alert" hidden></p>
    <p id="workspace-bot"></p>
    <p id="workspace-empty" hidden>这个仓库还没有分配工作区</p>
    <div id="workspace-details">
      <p id="workspace-path"></p>
      <p id="workspace-branch"></p>
      <button id="workspace-release" type="button">回收工作区</button>
    </div>
  </section>
  <!-- 主区派活卡片（S10）：位置在 #workspace 之后，不重排既有布局。状态灯的形状只有
       五种（idle / ready / running / done / error），显示什么由 data-state 决定；
       taskId / prompt 都是用户数据，只进 textContent。 -->
  <section id="task" data-state="idle" aria-live="polite">
    <h2 id="task-title">派活</h2>
    <p id="task-state">选择一个 Bot 后派活</p>
    <form id="task-form" autocomplete="off">
      <input id="task-prompt" type="text" spellcheck="false" maxlength="512"
             placeholder="给这个 Bot 的一句话指令，如：创建一个 hello.txt" aria-label="派活指令">
      <button id="task-dispatch" type="submit" disabled>派活</button>
    </form>
    <button id="task-cancel" type="button" hidden>取消任务</button>
    <p id="task-error" role="alert" hidden></p>
    <div id="task-details" hidden>
      <p id="task-id"></p>
      <p id="task-status-line"></p>
    </div>
  </section>
  <!-- 候选详情卡（S11）：任务成功后由 candidate.proposed 填进来；选仓库时取最近一条，
       所以刷新后仍看得到。点「查看差异」复用 S5 的 #diff 面板（同一个渲染件）。 -->
  <section id="candidate" data-state="idle" aria-live="polite">
    <h2 id="candidate-title">候选变更</h2>
    <p id="candidate-state">还没有候选</p>
    <div id="candidate-detail">
      <p id="candidate-branch"></p>
      <p id="candidate-stat"></p>
      <button id="candidate-open" type="button">查看差异</button>
    </div>
    <p id="candidate-error" role="alert" hidden></p>
  </section>
  <!-- 变更收件箱（S12 D1）：主区面板，入口在左栏仓库条目下。7 个 tab 保持不变。 -->
  <section id="inbox" data-state="idle" aria-live="polite">
    <h2 id="inbox-title">变更收件箱</h2>
    <p id="inbox-state">选择一个仓库查看它的待审变更</p>
    <label for="inbox-filter" hidden>状态筛选</label>
    <select id="inbox-filter">
      <option value="all">全部状态</option>
      <option value="local_only">仅本地</option>
      <option value="pushed">已推远端</option>
      <option value="pr_open">PR 待审</option>
      <option value="approved">已批准</option>
      <option value="rejected">已拒绝</option>
    </select>
    <div id="inbox-body">
      <p id="inbox-empty">暂无待审变更</p>
      <ul id="inbox-list"></ul>
    </div>
    <p id="inbox-error" role="alert" hidden></p>
  </section>
  <!-- 左栏 Bots 区（S6 D8）：在 #repos 之前（03-ui-spec §2：左栏上 Bot / 下仓库）。
       初始 idle：没选仓库之前不装成「这个仓库没有 Bot」。 -->
  <section id="bots" data-state="idle" aria-live="polite">
    <h2 id="bots-title">Bots</h2>
    <p id="bots-state">选择一个仓库查看它的 Bot</p>
    <p id="bots-error" role="alert" hidden></p>
    <ul id="bot-list"></ul>
    <p id="bot-empty" hidden>这个仓库还没有 Bot。</p>
    <!-- S8 D8：新建 Bot。四个可授予级别**就是全部**——L3/L4 不在这里，也不灰着放
         （灰掉也会让人以为"以后能开"）。表单靠 hidden 开关，JS 不碰 style.display。 -->
    <button id="bot-new" type="button">新建 Bot</button>
    <form id="bot-create-form" autocomplete="off" hidden>
      <input id="bot-create-name" type="text" spellcheck="false" maxlength="32"
             placeholder="名字（如 qa-1）" aria-label="Bot 名字">
      <select id="bot-create-permission" aria-label="权限">
        <option value="L0">L0 · 只读</option>
        <option value="L1" selected>L1 · 改工作区（默认）</option>
        <option value="L2">L2 · 推分支</option>
        <option value="L2+">L2+ · 开本仓库 PR</option>
      </select>
      <div id="bot-create-actions">
        <button id="bot-create-submit" type="submit" disabled>创建</button>
        <button id="bot-create-cancel" type="button">取消</button>
      </div>
      <p id="bot-create-error" role="alert" hidden></p>
    </form>
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

  // Bots 区与 charter 面板（S6 D8）
  var botsEl = document.getElementById("bots");
  var botsState = document.getElementById("bots-state");
  var botsError = document.getElementById("bots-error");
  var botList = document.getElementById("bot-list");
  var botEmpty = document.getElementById("bot-empty");
  var charterEl = document.getElementById("charter");
  var charterTitle = document.getElementById("charter-title");
  var charterState = document.getElementById("charter-state");
  var charterError = document.getElementById("charter-error");
  var charterMemory = document.getElementById("charter-memory");
  var charterContent = document.getElementById("charter-content");
  var charterPermissionRow = document.getElementById("charter-permission-row");
  var charterPermission = document.getElementById("charter-permission");
  var BOT_HINT = "选择一个仓库查看它的 Bot";
  var CHARTER_HINT = "点左侧的 Bot 查看它的 charter";
  // 两个面板**自己的**兜底：表外的码不显示绑定场景的「绑定失败」（同分支/差异面板）。
  var BOT_ERROR_FALLBACK = "读取 Bot 列表失败";
  var CHARTER_ERROR_FALLBACK = "读取 charter 失败";
  var selectedBotId = null;       // 选中的 Bot 与 repo 强绑定（D8）；切仓库/断连时清
  var selectedBotName = "";
  var botMemoryPaths = {};        // botId → memoryPath（来自 bot.list；charter.get 不回它）
  // S8：botId → permission。徽标与 charter 面板的下拉**同源**（都来自 bot.list），
  // 所以改权限成功后只需重拉一次列表，两处一起更新。
  var botPermissions = {};
  var botRequestToken = 0;        // 只认最后一次 bot.list 的回包（快速切仓库）
  var charterRequestToken = 0;    // 只认最后一次 charter 请求的回包

  // 新建 Bot（S8 D8）。表单开关只看 hidden；`botCreateBusy` 防连点。
  var botNew = document.getElementById("bot-new");
  var botCreateForm = document.getElementById("bot-create-form");
  var botCreateName = document.getElementById("bot-create-name");
  var botCreatePermission = document.getElementById("bot-create-permission");
  var botCreateSubmit = document.getElementById("bot-create-submit");
  var botCreateCancel = document.getElementById("bot-create-cancel");
  var botCreateError = document.getElementById("bot-create-error");
  var botCreateBusy = false;

  // 工作区卡片（S7 D9）。三条纪律：显隐只由 data-state / hidden 决定；路径与分支只进
  // textContent；**创建是显式动作**（选仓库只读 worktree.list，不会自动建目录）。
  var workspaceEl = document.getElementById("workspace");
  var workspaceForm = document.getElementById("workspace-form");
  var workspaceBase = document.getElementById("workspace-base");
  var workspaceAcquire = document.getElementById("workspace-acquire");
  var workspaceState = document.getElementById("workspace-state");
  var workspaceError = document.getElementById("workspace-error");
  var workspaceEmpty = document.getElementById("workspace-empty");
  var workspaceDetails = document.getElementById("workspace-details");
  var workspaceBot = document.getElementById("workspace-bot");
  var workspacePath = document.getElementById("workspace-path");
  var workspaceBranch = document.getElementById("workspace-branch");
  var workspaceRelease = document.getElementById("workspace-release");

  // Agent 通道卡片（S9 D9）。检测是**显式动作**：页面加载不动它，不偷偷起进程。
  var agentEl = document.getElementById("agent");
  var agentState = document.getElementById("agent-state");
  var agentDetect = document.getElementById("agent-detect");
  var agentError = document.getElementById("agent-error");
  var agentDetail = document.getElementById("agent-detail");
  var agentKind = document.getElementById("agent-kind");
  var agentVersion = document.getElementById("agent-version");
  var agentProtocol = document.getElementById("agent-protocol");
  var agentElapsed = document.getElementById("agent-elapsed");
  var agentFlags = document.getElementById("agent-flags");
  var agentBusy = false;
  var AGENT_HINT = "点「检测 Agent」看本机有没有可用的 Agent";
  var AGENT_ERROR_FALLBACK = "检测失败";
  var WORKSPACE_HINT = "选择一个仓库后分配工作区";
  // 卡片**自己的**兜底：表外的码不显示绑定场景的「绑定失败」（同分支/差异/Bot 面板）。
  var WORKSPACE_ERROR_FALLBACK = "工作区操作失败";
  var workspaceRequestToken = 0;  // 只认最后一次请求的回包（切仓库 / 断连时让旧回包作废）
  var workspaceBusy = false;      // 分配/回收在进行中：按钮置灰，防连点建两个
  var workspaceBotId = null;      // 卡片作用的 Bot = 这个仓库的第一个 Bot（D9，M1 只有一个）
  var workspaceBotName = "";

  // 派活卡片（S10）。卡片显示的是「选中 Bot 的最近一次任务」：dispatch 的应答与
  // task.updated 事件都往这里写，taskId 不匹配的事件直接忽略（页面只跟一个任务）。
  var taskEl = document.getElementById("task");
  var taskState = document.getElementById("task-state");
  var taskForm = document.getElementById("task-form");
  var taskPrompt = document.getElementById("task-prompt");
  var taskDispatch = document.getElementById("task-dispatch");
  var taskCancel = document.getElementById("task-cancel");
  var taskError = document.getElementById("task-error");
  var taskDetails = document.getElementById("task-details");
  var taskIdLine = document.getElementById("task-id");
  var taskStatusLine = document.getElementById("task-status-line");
  // 候选详情卡（S11）。用户数据（分支/stat）一律 textContent。
  var candidateEl = document.getElementById("candidate");
  var candidateState = document.getElementById("candidate-state");
  var candidateBranch = document.getElementById("candidate-branch");
  var candidateStat = document.getElementById("candidate-stat");
  var candidateError = document.getElementById("candidate-error");
  var candidateOpen = document.getElementById("candidate-open");
  // 变更收件箱（S12）：所有用户数据仍走 textContent，不使用 HTML 注入。
  var inboxEl = document.getElementById("inbox");
  var inboxState = document.getElementById("inbox-state");
  var inboxFilter = document.getElementById("inbox-filter");
  var inboxList = document.getElementById("inbox-list");
  var inboxError = document.getElementById("inbox-error");
  var INBOX_HINT = "选择一个仓库查看它的待审变更";
  var INBOX_ERROR_FALLBACK = "读取收件箱失败";
  var inboxRequestToken = 0;      // 切仓库/筛选竞态：旧回包一律丢弃
  var CANDIDATE_HINT = "还没有候选";
  var currentCandidate = null;    // 当前展示的候选载荷（candidate.list 的条目）
  var TASK_HINT = "选择一个 Bot 后派活";
  var TASK_ERROR_FALLBACK = "派活失败";
  var currentTaskId = null;       // 卡片正在跟踪的任务（evt 只认这个 id）
  var taskBusy = false;           // 派活请求在途：按钮置灰（回包即恢复）
  // 任务状态 → 状态行文案。evt 与应答共用这一张表；表外状态按原样显示（向前兼容）。
  var TASK_STATE_TEXT = {
    queued: "排队中",
    running: "运行中",
    succeeded: "已完成",
    failed: "失败",
    cancelled: "已取消",
    interrupted: "被打断（内核重启过，确认后可重跑）"
  };

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
    // S6 D8：Bots 与 charter 两个面板一起回 idle 并清空（不留旧 charter）。选中的 Bot
    // 与 repo 强绑定，不清 selectedBotId：重连后由 renderBots 重放（D8）。
    resetBots("idle", state === "disconnected" ? "已断开：Bot 列表未知" : "正在连接内核…");
    resetCharter("idle", state === "disconnected" ? "已断开：charter 未知" : "正在连接内核…");
    // S7 D9：工作区卡片同一纪律——断连回 idle + 「已断开：工作区未知」+ 清空路径/分支。
    resetWorkspace("idle", state === "disconnected" ? "已断开：工作区未知" : "正在连接内核…");
    // S9：Agent 检测结果属于上一次连接，断连后不许留着装新鲜（重连要重新点检测）。
    resetAgent("idle", state === "disconnected"
      ? "已断开：Agent 状态未知"
      : AGENT_HINT);
    // S10：派活卡片同一纪律。任务属于上一次连接——内核重启后它的 worker 已经不在了，
    // 留着「运行中」的灯就是撒谎； taskId 也一并清（evt 只认连接内产生的任务）。
    resetTask("idle", state === "disconnected" ? "已断开：任务状态未知" : TASK_HINT);
    resetCandidate("idle", CANDIDATE_HINT);   // S11：候选也属于上一次连接
    resetInbox("idle", INBOX_HINT);           // S12：收件箱也属于上一次连接
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
      // S12 D1：入口在仓库条目下（不新增第 8 个 tab）。stopPropagation 防止冒泡
      // 触发整行的 selectRepo 两次；入口自己选仓库并展开收件箱。
      var inboxLink = document.createElement("span");
      inboxLink.className = "repo-inbox-link";
      inboxLink.tabIndex = 0;
      inboxLink.setAttribute("role", "button");
      inboxLink.textContent = "变更收件箱";
      (function (rowId, rowName) {
        var openInbox = function (ev) {
          ev.stopPropagation();
          if (ev.preventDefault) { ev.preventDefault(); }
          selectRepo(rowId, rowName);   // 内部已 loadInbox()
          inboxEl.scrollIntoView({ block: "nearest" });
        };
        inboxLink.addEventListener("click", openInbox);
        inboxLink.addEventListener("keydown", function (ev) {
          if (ev.key === "Enter" || ev.key === " " || ev.key === "Spacebar") {
            openInbox(ev);
          }
        });
      }(repoId, repoName));
      li.appendChild(inboxLink);
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
      selectedBotId = null;
      selectedBotName = "";
      resetBranches("idle", "从左侧选择一个仓库");
      resetDiff("idle", DIFF_HINT);   // S5 D9：差异面板一起清空，不留上一个仓库的对比
      resetBots("idle", BOT_HINT);    // S6 D8：Bots 与 charter 一起回 idle（选中的 Bot 随仓库消失）
      resetCharter("idle", CHARTER_HINT);
      resetWorkspace("idle", WORKSPACE_HINT);   // S7 D9：工作区同理
      resetTask("idle", TASK_HINT);   // S10：派活卡片跟着 Bot 走，Bot 作废它就作废
      resetCandidate("idle", CANDIDATE_HINT);  // S11：候选跟着仓库走
      resetInbox("idle", INBOX_HINT);           // S12：收件箱跟着仓库走
      return;
    }
    applyRepoSelection();
    loadBranches();   // 重连且仍有选中仓库 → 与 loadRepos 同一条路径自动重取（D6）
    loadBots();       // S6 D8：重连仍选中仓库 → 重取 bot.list（charter 面板不清空）
    loadWorkspace();  // S7 D9：重连仍选中仓库 → 重取 worktree.list（D4：git 是事实源）
    loadLatestCandidate();  // S11：重连后候选也重取（连接不保存订阅状态）
    loadInbox();            // S12：重连后收件箱也重取（筛选保留，列表重读）
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

  // -------------------------------------------------------------------------
  // Bots 与 charter（S6 D8）。三条纪律与分支/差异面板同源：
  //   1. 显隐只由 CSS 的 `#bots[data-state=…]` / `#charter[data-state=…]` 与 hidden 决定；
  //   2. 切仓库 / 断连：两个面板都回 idle 并清空（不留旧 charter 装新鲜）；
  //   3. 快速切换时旧回包不能覆盖新结果（botRequestToken / charterRequestToken）。
  // charter 全文只进 textContent：它是用户可编辑文本，等于不可信输入（05 §4.2 同一精神）。
  // -------------------------------------------------------------------------

  function botErrorText(code) {
    return ERROR_TEXT[code] || BOT_ERROR_FALLBACK;
  }

  // S8 D8：两个面板的码 → 文案。全局 ERROR_TEXT 里 NOT_SUPPORTED 是「仓库含非 UTF-8 的名称」
  // （S5 的语义），在这里显示那句话就是撒谎——所以这几条在面板里各自覆盖。
  function botCreateErrorText(code) {
    if (code === "INVALID_PARAMS") {
      return "名字不合法：1–32 位，小写字母开头，只能用小写字母、数字和连接符";
    }
    if (code === "ALREADY_EXISTS") { return "这个仓库已经有叫这个名字的 Bot"; }
    if (code === "NOT_SUPPORTED") { return "第一版不支持这个权限级别（L3 / L4）"; }
    return ERROR_TEXT[code] || "新建 Bot 失败";
  }

  function permissionErrorText(code) {
    if (code === "INVALID_PARAMS") { return "找不到这个 Bot"; }
    if (code === "NOT_SUPPORTED") { return "第一版不支持这个权限级别（L3 / L4）"; }
    return ERROR_TEXT[code] || "改权限失败";
  }

  function charterErrorText(code) {
    // INVALID_PARAMS 在 charter 场景 = botId 查不到（D6）；不显示 S3 绑定场景的「路径参数不合法」。
    if (code === "INVALID_PARAMS") { return "找不到这个 Bot"; }
    return ERROR_TEXT[code] || CHARTER_ERROR_FALLBACK;
  }

  function charterTitleText() {
    return selectedBotId ? ("Charter · " + selectedBotName) : "Charter";
  }

  function resetBots(state, text) {
    botRequestToken += 1;                 // 让在途请求的回包作废
    botsEl.dataset.state = state;
    botsState.textContent = text;
    botList.textContent = "";
    botsError.textContent = "";
    botsError.hidden = true;
    botEmpty.hidden = true;
    closeBotCreate(true);                 // S8：切仓库 / 断连时收掉半填的表单，别让它悬着
  }

  function applyBotSelection() {
    var rows = botList.querySelectorAll("li[data-bot-id]");
    for (var i = 0; i < rows.length; i++) {
      if (selectedBotId !== null && rows[i].dataset.botId === selectedBotId) {
        rows[i].dataset.selected = "true";
        rows[i].setAttribute("aria-current", "true");
      } else {
        delete rows[i].dataset.selected;
        rows[i].removeAttribute("aria-current");
      }
    }
  }

  // 一行 Bot：名字 + 权限徽标 + 职责摘要（charter 第一段；空 charter 是「尚未填写职责」）。
  // 整行可选中（D8）：tabindex + role=button，click 与 Enter/Space 同一条路径。
  function renderBots(list) {
    botList.textContent = "";
    botPermissions = {};   // 列表是徽标与 charter 下拉的共同事实源：整份重建，不留旧值
    for (var i = 0; i < list.length; i++) {
      var bot = list[i] || {};
      var rowId = String(bot.botId || "");
      var rowName = String(bot.name || "（未命名）");
      var li = document.createElement("li");
      li.dataset.botId = rowId;
      li.tabIndex = 0;
      li.setAttribute("role", "button");
      (function (botId, botName) {
        li.addEventListener("click", function () { selectBot(botId, botName); });
        li.addEventListener("keydown", function (ev) {
          if (ev.key === "Enter" || ev.key === " " || ev.key === "Spacebar") {
            ev.preventDefault();           // 防 Space 滚动页面
            selectBot(botId, botName);
          }
        });
      }(rowId, rowName));
      var name = document.createElement("span");
      name.className = "bot-name";
      name.textContent = rowName;
      var badge = document.createElement("span");
      badge.className = "bot-permission";
      badge.textContent = String(bot.permission || "");
      var summary = document.createElement("span");
      summary.className = "bot-charter-summary";
      summary.textContent = String(bot.charterSummary || "");
      // D8：记忆目录来自 bot.list 的 memoryPath（memory.charter.get 只回 content/rev）。
      botMemoryPaths[rowId] = String(bot.memoryPath || "");
      botPermissions[rowId] = String(bot.permission || "");
      li.appendChild(name);
      li.appendChild(badge);
      li.appendChild(summary);
      botList.appendChild(li);
    }
    // 只有「真的拿到了一个（可能为空的）列表」时才能说「这个仓库还没有 Bot」
    botEmpty.hidden = list.length !== 0;
    // S7 D9：工作区卡片作用在这个仓库的**第一个** Bot 上（M1 只有一个 builder）。拿到它才允许
    // 点「分配工作区」——按钮会一直灰着，而不是点了才报「还没有 Bot」。
    workspaceBotId = list.length > 0 ? (String((list[0] || {}).botId || "") || null) : null;
    workspaceBotName = list.length > 0 ? String((list[0] || {}).name || "") : "";
    if (workspaceEl.dataset.state === "ready") {
      workspaceAcquire.disabled = workspaceBotId === null || workspaceBusy;
      workspaceBot.textContent = workspaceBotText(workspaceBotName);
    }
    var stillListed = false;
    for (var j = 0; j < list.length; j++) {
      if (String((list[j] || {}).botId || "") === selectedBotId) { stillListed = true; break; }
    }
    if (!stillListed) {
      // 选中的 Bot 从列表里消失：回 idle 并清空（D8）；不动 repo 层的选中状态
      selectedBotId = null;
      selectedBotName = "";
      resetCharter("idle", CHARTER_HINT);
      return;
    }
    applyBotSelection();
    // 断连重置过面板（选中 Bot 仍与 repo 绑定）：重连后把 charter 重放回来。**不清空**
    // 已显示的 charter——重放只在 idle 时发生（D8 的「不清空」）。
    if (charterEl.dataset.state === "idle" && selectedBotId !== null) {
      loadCharter(selectedBotId, selectedBotName);
    }
  }

  function loadBots() {
    if (selectedRepoId === null) { resetBots("idle", BOT_HINT); return; }
    var repoId = selectedRepoId;
    var token = ++botRequestToken;
    closeBotCreate(true);   // 列表要重读了：半填的表单不该跨过一次加载留着
    botsEl.dataset.state = "loading";
    botsState.textContent = "正在读取 Bot…";
    botList.textContent = "";
    botsError.textContent = "";
    botsError.hidden = true;
    botEmpty.hidden = true;
    request("bot.list", { repoId: repoId }, function (err, data) {
      if (token !== botRequestToken) { return; }   // 过期回包：切了仓库或已断连
      if (err) {
        botsEl.dataset.state = "error";
        botsError.textContent = botErrorText(err);  // 按码映射，不看 message
        botsError.hidden = false;
        return;
      }
      renderBots(Array.isArray(data) ? data : []);
      botsEl.dataset.state = "ready";
    });
  }

  // -------------------------------------------------------------------------
  // 新建 Bot（S8 D8）。三条纪律：
  //   1. 表单开关只看 hidden；按钮只在 ready 出现（CSS 管，不碰 style.display）；
  //   2. 创建成功后**重新拉一次 bot.list**——排序与徽标的事实源在服务端，页面不拼列表；
  //   3. 错误按**码**映射，不把服务端 message 贴到页面上（05 §4.2）。
  // -------------------------------------------------------------------------

  function syncBotCreateButton() {
    botCreateSubmit.disabled = botCreateBusy || botCreateForm.hidden;
  }

  function openBotCreate() {
    botCreateForm.hidden = false;
    botCreateName.value = "";
    botCreatePermission.value = "L1";   // D2：默认级别，改由人
    botCreateError.textContent = "";
    botCreateError.hidden = true;
    syncBotCreateButton();
    botCreateName.focus();
  }

  // `silent`：别抢焦点（切换仓库 / 重读列表时，用户的眼睛不在这个表单上）。
  function closeBotCreate(silent) {
    var wasOpen = !botCreateForm.hidden;
    botCreateForm.hidden = true;
    botCreateName.value = "";
    botCreatePermission.value = "L1";
    botCreateError.textContent = "";
    botCreateError.hidden = true;
    botCreateBusy = false;
    syncBotCreateButton();
    if (wasOpen && silent !== true) { botNew.focus(); }
  }

  function submitBotCreate() {
    if (botCreateBusy || selectedRepoId === null || botCreateForm.hidden) { return; }
    var repoId = selectedRepoId;
    var name = botCreateName.value.trim();   // 只削空白：名字白名单由内核判（D3）
    botCreateBusy = true;
    botCreateError.textContent = "";
    botCreateError.hidden = true;
    syncBotCreateButton();
    request("bot.create", {
      repoId: repoId,
      name: name,
      permission: botCreatePermission.value
    }, function (err, data) {
      botCreateBusy = false;
      syncBotCreateButton();
      if (selectedRepoId !== repoId) { return; }   // 切了仓库：回包作废
      if (err) {
        botCreateError.textContent = botCreateErrorText(err);
        botCreateError.hidden = false;
        botCreateName.focus();   // 失败后光标回到能改的地方（提交按钮此时刚被重新启用）
        return;
      }
      if (!data) {
        botCreateError.textContent = "新建 Bot 失败";
        botCreateError.hidden = false;
        return;
      }
      closeBotCreate();
      loadBots();
    });
  }

  function resetCharter(state, text) {
    charterRequestToken += 1;
    charterEl.dataset.state = state;
    charterTitle.textContent = charterTitleText();
    charterState.textContent = text;
    charterError.textContent = "";
    charterError.hidden = true;
    charterMemory.textContent = "";
    charterMemory.hidden = true;
    charterContent.textContent = "";
    charterContent.hidden = true;
    // 在途的改权限回包也别再把控件解冻到"已重置"的面板上
    charterPermission.disabled = false;
    charterPermission.value = "";
  }

  // S8 D8：权限下拉的值只来自 botPermissions（bot.list 的同一份事实）。
  // 库里若有选项之外的值（手改过 DB），这里落成未选中——不假装认识它。
  function renderCharterPermission(botId) {
    charterPermission.value = String(botPermissions[botId] || "");
  }

  function loadCharter(botId, botName) {
    var token = ++charterRequestToken;
    charterEl.dataset.state = "loading";
    charterTitle.textContent = "Charter · " + botName;
    charterState.textContent = "正在读取 charter…";
    charterError.textContent = "";
    charterError.hidden = true;
    charterMemory.textContent = "";
    charterMemory.hidden = true;
    charterContent.textContent = "";
    charterContent.hidden = true;
    renderCharterPermission(botId);
    request("memory.charter.get", { botId: botId }, function (err, data) {
      if (token !== charterRequestToken) { return; }  // 过期回包：切了仓库/Bot 或已断连
      if (err) {
        charterEl.dataset.state = "error";
        charterError.textContent = charterErrorText(err);
        charterError.hidden = false;
        return;
      }
      var payload = data || {};
      charterEl.dataset.state = "ready";
      // D9：显示绝对路径原样（状态根可被 POSTEL_HOME 覆盖，伪装 ~ 是假话）。
      charterMemory.textContent = "记忆目录：" + String(botMemoryPaths[botId] || "");
      charterMemory.hidden = false;
      charterContent.textContent = String(payload.content == null ? "" : payload.content);
      charterContent.hidden = false;
      renderCharterPermission(botId);   // 回包可能比 renderBots 晚：再对一次
    });
  }

  // S8 D8：人改权限（05 §2.3：Bot 永不能改自己的级别——本方法只挂在人的入口上）。
  // 失败时把下拉**回滚**到服务端仍持有的值：控件不能声称一个内核拒绝了的改动。
  function submitPermissionChange() {
    if (selectedBotId === null || charterPermission.disabled) { return; }
    var botId = selectedBotId;
    var wanted = charterPermission.value;
    var previous = String(botPermissions[botId] || "");
    if (!wanted || wanted === previous) { return; }
    charterPermission.disabled = true;
    charterError.textContent = "";
    charterError.hidden = true;
    request("bot.set_permission", { botId: botId, permission: wanted }, function (err, data) {
      charterPermission.disabled = false;
      if (selectedBotId !== botId) { return; }   // 切了 Bot：回包作废
      if (err) {
        charterPermission.value = previous;
        charterError.textContent = permissionErrorText(err);
        charterError.hidden = false;
        return;
      }
      botPermissions[botId] = String((data || {}).permission || wanted);
      charterPermission.value = botPermissions[botId];
      loadBots();   // 左栏徽标由 bot.list 渲染：一处事实源，两处一起更新
    });
  }

  // -------------------------------------------------------------------------
  // 工作区卡片（S7 D9）。四条纪律：
  //   1. 显隐只由 `#workspace[data-state=…]` 与 hidden 属性决定（T8）；
  //   2. 路径 / 分支 / Bot 名只进 textContent；
  //   3. 选仓库只**读**（worktree.list），分配与回收才是写；
  //   4. 错误按**码**映射（07 §1.3），不把服务端 message 贴到页面上（05 §4.2）。
  // -------------------------------------------------------------------------

  function workspaceErrorText(code) {
    return ERROR_TEXT[code] || WORKSPACE_ERROR_FALLBACK;   // 表外的码不显示"绑定失败"
  }

  // 卡片作用在哪个 Bot 上：**说出来**，别让人猜（D9）。没有 Bot 时指向那个动作（S8 起可建）。
  function workspaceBotText(name) {
    return name ? ("Bot：" + name) : "Bot：（还没有 Bot，先新建一个）";
  }

  // 本地文案（不是服务端 message）：连接没就绪 / 这个仓库还没有 Bot。
  function showWorkspaceNote(text) {
    workspaceEl.dataset.state = "error";
    workspaceError.textContent = text;
    workspaceError.hidden = false;
  }

  function showWorkspaceError(code) {
    showWorkspaceNote(workspaceErrorText(code));
    workspaceAcquire.disabled = workspaceBotId === null;
    workspaceRelease.disabled = false;
  }

  function resetWorkspace(state, text) {
    workspaceRequestToken += 1;      // 让在途请求的回包作废（切仓库 / 断连）
    workspaceBusy = false;
    workspaceEl.dataset.state = state;
    workspaceState.textContent = text;
    workspaceError.textContent = "";
    workspaceError.hidden = true;
    workspaceEmpty.hidden = true;
    workspaceDetails.hidden = true;
    workspaceBot.textContent = "";
    workspacePath.textContent = "";
    workspaceBranch.textContent = "";
    workspaceAcquire.disabled = true;
    workspaceRelease.disabled = false;
  }

  // 有工作区 → 路径 / 分支；没有 → 「还没有分配」。**空不是错误**（D7 第 3 行）。
  function renderWorkspace(entry) {
    workspaceEl.dataset.state = "ready";
    workspaceAcquire.disabled = workspaceBotId === null || workspaceBusy;
    workspaceRelease.disabled = false;
    workspaceBot.textContent = workspaceBotText(entry ? entry.botName : workspaceBotName);
    if (!entry) {
      workspaceDetails.hidden = true;
      workspaceEmpty.hidden = false;
      workspacePath.textContent = "";
      workspaceBranch.textContent = "";
      return;
    }
    workspacePath.textContent = "路径：" + String(entry.path || "");
    workspaceBranch.textContent = "分支：" + String(entry.branch || "");
    workspaceEmpty.hidden = true;
    workspaceDetails.hidden = false;
  }

  function loadWorkspace() {
    if (selectedRepoId === null) { resetWorkspace("idle", WORKSPACE_HINT); return; }
    resetWorkspace("loading", "正在读取工作区…");
    var token = workspaceRequestToken;
    var id = request("worktree.list", { repoId: selectedRepoId }, function (err, data) {
      if (token !== workspaceRequestToken) { return; }   // 过期回包：切了仓库或已断连
      if (err) { showWorkspaceError(err); return; }
      var rows = Array.isArray(data) ? data : [];
      var mine = null;
      for (var i = 0; i < rows.length; i++) {
        // 卡片只关心它自己的 Bot（D9）；别的 Bot 的工作区不属于这张卡。
        if (workspaceBotId !== null && String((rows[i] || {}).botId || "") === workspaceBotId) {
          mine = rows[i];
          break;
        }
      }
      if (mine === null && rows.length > 0) { mine = rows[0]; }
      renderWorkspace(mine);
    });
    if (id === null) { showWorkspaceNote("连接尚未就绪"); }
  }

  function submitWorkspace() {
    if (workspaceBusy || selectedRepoId === null) { return; }
    if (workspaceBotId === null) { showWorkspaceNote("这个仓库还没有 Bot"); return; }
    var params = { repoId: selectedRepoId, botId: workspaceBotId };
    var base = workspaceBase.value.trim();
    if (base) { params.baseRef = base; }   // 空 = 交给内核用默认分支（D8）
    workspaceBusy = true;
    workspaceAcquire.disabled = true;
    workspaceRelease.disabled = true;
    workspaceError.hidden = true;
    var token = workspaceRequestToken;
    var id = request("worktree.acquire", params, function (err, data) {
      if (token !== workspaceRequestToken) { return; }
      workspaceBusy = false;
      if (err) { showWorkspaceError(err); return; }   // 含 BAD_BASE_REF / WORKTREE_CONFLICT
      renderWorkspace(data && typeof data === "object" ? data : null);
    });
    if (id === null) {
      workspaceBusy = false;
      showWorkspaceNote("连接尚未就绪");
    }
  }

  function releaseWorkspace() {
    if (workspaceBusy || selectedRepoId === null || workspaceBotId === null) { return; }
    workspaceBusy = true;
    workspaceAcquire.disabled = true;
    workspaceRelease.disabled = true;
    workspaceError.hidden = true;
    var token = workspaceRequestToken;
    var id = request("worktree.release", { repoId: selectedRepoId, botId: workspaceBotId },
      function (err) {
        if (token !== workspaceRequestToken) { return; }
        workspaceBusy = false;
        if (err) { showWorkspaceError(err); return; }   // 脏工作区 → WORKTREE_CONFLICT
        loadWorkspace();   // 重新读事实：目录与注册记录都没了（D4，不自己编结果）
      });
    if (id === null) {
      workspaceBusy = false;
      showWorkspaceNote("连接尚未就绪");
    }
  }

  // ---- Agent 通道（S9 D9 / D10）----

  // D10：面板**自己的**码 → 文案。全局 ERROR_TEXT 里 `ADAPTER_UNAVAILABLE` 说的是
  // 「内核找不到 git 命令」——那是 S3 的绑定场景，复用会让用户以为自己的 git 坏了。
  function agentErrorText(code) {
    if (code === "ADAPTER_UNAVAILABLE") {
      return "本机没有可用的 Agent 命令（未安装或不在 PATH 上）";
    }
    if (code === "ADAPTER_TIMEOUT") { return "Agent 握手超时（30 秒），可以重试"; }
    if (code === "NOT_SUPPORTED") { return "第一版不支持这个参数"; }
    return ERROR_TEXT[code] || AGENT_ERROR_FALLBACK;
  }

  // 四态只由 data-state 与 hidden 决定（T8）。错误态**不**禁按钮：它就是重试入口（D9）。
  function resetAgent(state, text) {
    agentEl.dataset.state = state;
    agentState.textContent = text;
    if (state !== "error") { agentError.hidden = true; agentError.textContent = ""; }
    if (state !== "ready") { agentDetail.hidden = true; agentFlags.textContent = ""; }
    agentBusy = state === "loading";
    agentDetect.disabled = agentBusy;
    agentDetect.textContent = state === "error" ? "重试" : "检测 Agent";
  }

  function renderAgent(data) {
    var flags = (data && data.flags) || [];
    var name = data && data.agentName ? String(data.agentName) : "未知";
    var version = data && data.agentVersion ? String(data.agentVersion) : "未知";
    agentKind.textContent = "通道：" + String((data && data.agentKind) || "（未知）");
    agentVersion.textContent = "Agent：" + name + " " + version;
    agentProtocol.textContent = "protocolVersion：" +
      String(data && data.protocolVersion !== undefined ? data.protocolVersion : "未知");
    agentElapsed.textContent = "握手耗时：" + String((data && data.elapsedMs) || 0) + " ms";
    agentFlags.textContent = "";
    for (var i = 0; i < flags.length; i++) {
      var li = document.createElement("li");
      li.textContent = String(flags[i]);   // Agent 报的字符串，只进 textContent
      agentFlags.appendChild(li);
    }
    if (flags.length === 0) {
      var none = document.createElement("li");
      none.textContent = "（这次握手没有声明任何能力）";
      agentFlags.appendChild(none);
    }
    agentDetail.hidden = false;
    agentEl.dataset.state = "ready";
    agentState.textContent = "可用";
    agentBusy = false;
    agentDetect.disabled = false;
    agentDetect.textContent = "重新检测";
    agentError.hidden = true;
  }

  function showAgentError(code) {
    resetAgent("error", "不可用");
    agentError.textContent = agentErrorText(code);
    agentError.hidden = false;
  }

  // 先问 `agent.list` 拿通道 id，再握手：页面**不知道**任何通道名（D1 的禁令同样适用于
  // 前端）。顺带让「一个通道都没登记」和「通道在但握手失败」显示成不同的话。
  function detectAgent() {
    if (agentBusy) { return; }
    resetAgent("loading", "正在读取通道…");
    var id = request("agent.list", {}, function (err, list) {
      if (err) { showAgentError(err); return; }
      var channel = (list && list.length > 0) ? list[0] : null;
      if (channel === null) {
        resetAgent("error", "不可用");
        agentError.textContent = "内核没有登记任何 Agent 通道";
        agentError.hidden = false;
        return;
      }
      resetAgent("loading", "正在握手（最多 30 秒）…");
      var requested = request("agent.capabilities", { agentKind: String(channel.agentKind) },
        function (handshakeError, data) {
          if (handshakeError) { showAgentError(handshakeError); return; }
          renderAgent(data && typeof data === "object" ? data : {});
        });
      if (requested === null) {
        resetAgent("error", "连接尚未就绪");
        agentError.textContent = "连接尚未就绪，先重连内核";
        agentError.hidden = false;
      }
    });
    if (id === null) {
      resetAgent("error", "连接尚未就绪");
      agentError.textContent = "连接尚未就绪，先重连内核";
      agentError.hidden = false;
    }
  }

  function selectBot(botId, botName) {
    if (!botId) { return; }
    selectedBotId = botId;
    selectedBotName = botName;
    applyBotSelection();
    loadCharter(botId, botName);
    // S10：选中的 Bot 变了，卡片跟踪的任务也随之作废——上一个 Bot 的任务不归这个灯管。
    resetTask("ready", TASK_HINT);
    resetCandidate("idle", CANDIDATE_HINT);  // S11：换 Bot 就不跟上一个 Bot 的候选
  }

  // ---- 派活卡片（S10） ------------------------------------------------------
  // 状态灯五种：idle（没选 Bot）/ ready（选了 Bot，没在跟任务）/ running / done / error。
  // 显示什么由 data-state 决定；这里只改属性与文本（T8：别逐个 style.display）。

  function resetTask(state, text) {
    taskBusy = false;
    currentTaskId = null;
    taskEl.dataset.state = state;
    taskState.textContent = text;
    taskError.textContent = "";
    taskError.hidden = true;
    taskDetails.hidden = true;
    taskIdLine.textContent = "";
    taskStatusLine.textContent = "";
    taskPrompt.value = "";
    taskDispatch.disabled = !selectedBotId;
    taskCancel.hidden = true;
  }

  function showTaskError(text) { taskError.textContent = text; taskError.hidden = false; }

  // 把一个任务载荷（dispatch 的应答或 evt 的 data）落到卡片上。
  function trackTask(taskId, state, errorCode) {
    currentTaskId = taskId;
    taskDetails.hidden = false;
    taskIdLine.textContent = "任务 " + taskId;
    var label = TASK_STATE_TEXT[state] || state;
    taskStatusLine.textContent = errorCode ? label + "（" + errorCode + "）" : label;
    if (state === "running" || state === "queued") {
      taskEl.dataset.state = "running";
      taskCancel.hidden = false;
    } else if (state === "succeeded" || state === "cancelled") {
      taskEl.dataset.state = "done";
      taskCancel.hidden = true;
    } else {  // failed / interrupted / 表外状态：不隐藏 details，用户能看见并重派
      taskEl.dataset.state = "error";
      taskCancel.hidden = true;
    }
  }

  function submitTask() {
    var prompt = taskPrompt.value.trim();
    clearTaskError();
    if (!prompt) { showTaskError("请输入一句话指令"); return; }
    if (!selectedBotId) { showTaskError(TASK_HINT); return; }
    taskBusy = true;
    taskDispatch.disabled = true;
    request("task.dispatch", { botId: selectedBotId, prompt: prompt }, function (err, data) {
      taskBusy = false;
      taskDispatch.disabled = false;
      if (err) {
        // BUSY / PERMISSION_DENIED 按码映射（ERROR_TEXT 注入）；其它的码走卡片自己的兜底。
        showTaskError(err === "BUSY" || err === "PERMISSION_DENIED"
          ? errorText(err) : TASK_ERROR_FALLBACK + "（" + err + "）");
        return;
      }
      taskPrompt.value = "";
      // 应答只有 {taskId, state, botId}：以它为起点建卡，后续变化由 task.updated 接管。
      trackTask(String(data.taskId), String(data.state || "queued"), null);
    });
  }

  function cancelTask() {
    if (!currentTaskId) { return; }
    taskCancel.disabled = true;
    request("task.cancel", { taskId: currentTaskId }, function (err, data) {
      taskCancel.disabled = false;
      if (err) {
        showTaskError(err === "INVALID_PARAMS" ? "任务已结束，不能再取消" : TASK_ERROR_FALLBACK);
        return;
      }
      if (data && data.taskId) { trackTask(String(data.taskId), String(data.state), null); }
    });
  }

  function clearTaskError() { taskError.textContent = ""; taskError.hidden = true; }

  // task.updated 事件（S10 D12：载荷恰为 {taskId, state, errorCode?}）。只认卡片正在
  // 跟踪的任务；失败的码也用 ERROR_TEXT 翻译（ADAPTER_UNAVAILABLE 等在表里）。
  function onTaskUpdated(data) {
    if (!data || String(data.taskId) !== currentTaskId) { return; }
    trackTask(String(data.taskId), String(data.state),
      data.errorCode ? String(data.errorCode) : null);
  }

  // ---- 变更收件箱（S12）----------------------------------------------------
  function relativeTime(epochSeconds, nowMs) {
    var delta = (nowMs / 1000) - Number(epochSeconds || 0);
    if (!isFinite(delta) || delta < 60) { return "刚刚"; }
    if (delta < 3600) { return Math.floor(delta / 60) + " 分钟前"; }
    if (delta < 86400) { return Math.floor(delta / 3600) + " 小时前"; }
    return Math.floor(delta / 86400) + " 天前";
  }
  function reviewLabel(state) {
    var labels = { local_only: "仅本地", pushed: "已推远端", pr_open: "PR 待审",
      approved: "已批准", rejected: "已拒绝" };
    return labels[state] || String(state || "");
  }
  function inboxErrorText(code) {
    return code === "REPO_NOT_BOUND" ? "仓库路径已不存在"
      : code === "INVALID_PARAMS" ? "筛选参数不合法" : INBOX_ERROR_FALLBACK;
  }
  function resetInbox(state, text) {
    inboxRequestToken += 1;
    inboxEl.dataset.state = state;
    inboxState.textContent = text;
    inboxList.textContent = "";
    inboxError.textContent = "";
    inboxError.hidden = true;
  }
  function loadInbox() {
    if (!selectedRepoId) { resetInbox("idle", INBOX_HINT); return; }
    var token = ++inboxRequestToken;
    inboxEl.dataset.state = "loading";
    inboxState.textContent = "正在读取收件箱…";
    var filter = inboxFilter.value;
    var params = { repoId: selectedRepoId };
    if (filter !== "all") { params.reviewState = filter; }
    request("candidate.list", params, function (err, data) {
      if (token !== inboxRequestToken) { return; }
      if (err) {
        inboxEl.dataset.state = "error";
        inboxState.textContent = INBOX_ERROR_FALLBACK;
        inboxError.textContent = inboxErrorText(err);
        inboxError.hidden = false;
        return;
      }
      var rows = Array.isArray(data) ? data : [];
      inboxList.textContent = "";
      if (rows.length === 0) {
        inboxEl.dataset.state = "empty";
        inboxState.textContent = "暂无待审变更";
        return;
      }
      var now = Date.now();
      rows.forEach(function (candidate) {
        var li = document.createElement("li");
        li.tabIndex = 0;
        li.setAttribute("role", "button");
        li.dataset.taskId = String(candidate.taskId || "");
        var title = document.createElement("span");
        title.className = "inbox-title";
        title.textContent = "候选 · " + String(candidate.botName || "");
        var badge = document.createElement("span");
        badge.className = "inbox-state";
        badge.textContent = reviewLabel(candidate.reviewState);
        title.appendChild(badge);
        var meta = document.createElement("span");
        meta.className = "inbox-meta";
        meta.textContent = "+" + String(candidate.insertions || 0) + " −"
          + String(candidate.deletions || 0) + " · "
          + String(candidate.filesChanged || 0) + " 个文件 · "
          + relativeTime(candidate.createdAt, now);
        li.appendChild(title);
        li.appendChild(meta);
        var open = function () {
          var taskId = String(candidate.taskId || "");
          if (!taskId) { return; }
          loadCandidate(taskId);
          candidateEl.scrollIntoView({ block: "nearest" });
        };
        li.addEventListener("click", open);
        li.addEventListener("keydown", function (ev) {
          if (ev.key === "Enter" || ev.key === " " || ev.key === "Spacebar") {
            ev.preventDefault();
            open();
          }
        });
        inboxList.appendChild(li);
      });
      inboxEl.dataset.state = "ready";
      inboxState.textContent = "共 " + rows.length + " 条候选";
    });
  }

  // ---- 候选详情卡（S11）----------------------------------------------------
  function resetCandidate(state, text) {
    currentCandidate = null;
    candidateEl.dataset.state = state;
    candidateState.textContent = text;
    candidateError.textContent = "";
    candidateError.hidden = true;
    candidateBranch.textContent = "";
    candidateStat.textContent = "";
  }

  function showCandidateError(text) {
    candidateError.textContent = text;
    candidateError.hidden = false;
    candidateEl.dataset.state = "error";
  }

  function trackCandidate(candidate) {
    currentCandidate = candidate;
    candidateEl.dataset.state = "done";
    candidateState.textContent = "候选 · 仅本地";
    candidateBranch.textContent = "分支 " + String(candidate.branch || "");
    candidateStat.textContent = "变更 +" + String(candidate.insertions || 0)
      + " −" + String(candidate.deletions || 0) + " · " + String(candidate.filesChanged || 0)
      + " 个文件";
    candidateError.textContent = "";
    candidateError.hidden = true;
  }

  // 事件只带 {candidateId, taskId}（07 §3）：细节走 candidate.list 取，不塞进事件载荷。
  function loadCandidate(taskId) {
    if (!selectedRepoId || !taskId) { return; }
    request("candidate.list", { repoId: selectedRepoId, taskId: taskId }, function (err, data) {
      if (err) { showCandidateError("读取候选失败（" + err + "）"); return; }
      if (Array.isArray(data) && data.length > 0) { trackCandidate(data[0]); }
    });
  }

  // 刷新后没有在途事件：选仓库时取该仓库最近一条候选（列表按 createdAt DESC）。
  function loadLatestCandidate() {
    if (!selectedRepoId) { return; }
    request("candidate.list", { repoId: selectedRepoId }, function (err, data) {
      if (err) { return; }  // 列表页的空态/错误由 S12 管，这里静默留 idle
      if (Array.isArray(data) && data.length > 0) { trackCandidate(data[0]); }
    });
  }

  function openCandidateDiff() {
    if (!currentCandidate) { return; }
    // 复用 S5 的渲染件：SHAs 是 git 产出的 40 位 hex，通过 ref_name_ok 的入站校验。
    requestDiff(String(currentCandidate.baseSha), String(currentCandidate.headSha));
  }

  function onCandidateProposed(data) {
    if (!data || String(data.taskId) !== currentTaskId) { return; }
    candidateEl.dataset.state = "loading";
    candidateState.textContent = "正在读取候选…";
    loadCandidate(String(data.taskId));
    loadInbox();   // S12：新候选也要立即出现在收件箱（同一个 repo，筛选保留）
  }

  function subscribeTopics() {
    // 02b §2.3：重连后必须重新订阅（内核不保存订阅状态，订阅随连接死）。
    send("subscribe", { topics: ["task.updated", "candidate.proposed"] });
  }

  function selectRepo(repoId, name) {
    if (!repoId) { return; }
    selectedRepoId = repoId;
    selectedRepoName = name;
    applyRepoSelection();
    // S7 D9：基础分支输入框预填这个仓库的默认分支（用户可以改；游离 HEAD 时留空）。
    workspaceBase.value = repoDefaultBranches[repoId] || "";
    // S5 D9：切仓库 → 差异面板回 idle 并清空 patch/stat/error/选中行，标题跟着仓库名变。
    // 显式重选同一个仓库也会清空（用户正在看的对比就此结束，不静默留着旧结果）。
    resetDiff("idle", DIFF_HINT);
    // S6 D8：切仓库 → 选中的 Bot 作废，charter 面板回 idle 并清空；Bots 区按新仓库重取。
    selectedBotId = null;
    selectedBotName = "";
    resetCharter("idle", CHARTER_HINT);
    loadBots();
    loadBranches();
    // S7 D9：切仓库 → 工作区卡片按新仓库重读（清空旧的路径与分支，不留上一个仓库的结果）。
    loadWorkspace();
    // S11：刷新/换仓库后取这个仓库最近一条候选，详情不会因为刷新就消失。
    resetCandidate("idle", CANDIDATE_HINT);
    loadLatestCandidate();
    loadInbox();            // S12：收件箱跟着仓库走（点整行选仓库也会加载）
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
    // 订阅推送（S10 D11）：evt 不带 id，不进 pending 配对，按 topic 分发。
    if (frame && frame.type === "evt" && frame.topic === "task.updated") {
      onTaskUpdated(frame.data);
      return;
    }
    if (frame && frame.type === "evt" && frame.topic === "candidate.proposed") {
      onCandidateProposed(frame.data);
      return;
    }
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
      subscribeTopics();  // S10 D11：先订阅再干活，否则第一批 evt 会漏
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
  // S9：检测是显式按钮（起进程的事不该被回车或页面加载偷偷触发）。
  agentDetect.addEventListener("click", detectAgent);
  // S10：派活走 <form>（回车 = 点「派活」）；取消是显式按钮（打断会话不该被回车触发）。
  taskForm.addEventListener("submit", function (ev) { ev.preventDefault(); submitTask(); });
  taskCancel.addEventListener("click", cancelTask);
  // S11：查看差异是显式按钮（不是回车提交）；渲染件复用 S5 的 #diff 面板。
  candidateOpen.addEventListener("click", openCandidateDiff);
  // S12：筛选改变 = 重发 candidate.list（服务端筛，不在客户端挑）；竞态靠 token。
  inboxFilter.addEventListener("change", loadInbox);
  // S7：分配走 <form>（回车与点按钮同一条路径）；回收是显式按钮（删目录不该被回车触发）。
  workspaceForm.addEventListener("submit", function (ev) {
    ev.preventDefault();
    submitWorkspace();
  });
  workspaceRelease.addEventListener("click", releaseWorkspace);
  // S8：新建走 <form>（回车 = 点「创建」）；Escape 收表单（焦点回到「新建 Bot」）；
  // 改权限挂在下拉的 change 上——选完即提交，没有第二个按钮可以忘点。
  botNew.addEventListener("click", openBotCreate);
  botCreateForm.addEventListener("submit", function (ev) {
    ev.preventDefault();
    submitBotCreate();
  });
  botCreateCancel.addEventListener("click", function () { closeBotCreate(); });
  // Escape 绑在 **document** 上，不是绑在 form 上：提交时按钮会被 `disabled`（防连点），
  // 而"正在聚焦的元素被禁用"会把焦点丢回 body —— 绑在 form 上的 keydown 就再也收不到了
  // （真实浏览器验收当场抓到这个，见 06-pitfalls T41）。守卫是"表单开着才管"。
  document.addEventListener("keydown", function (ev) {
    if (ev.key === "Escape" && !botCreateForm.hidden) {
      ev.preventDefault();
      closeBotCreate();
    }
  });
  charterPermission.addEventListener("change", submitPermissionChange);
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
