# 03 · assets — `src/postel_studio/web/assets.py`

> 层 L4 · 465 行 · 上游：[模块索引](README.md) · 相关：[03-ui-spec §2](../../design/03-ui-spec.md) · [06-pitfalls T8/T9/T27](../06-pitfalls.md)
> ⚠️ **本模块是 Code Map 里覆盖最薄的一块**：JS 的 DOM 行为没有自动化测试。见 §测试。

## 职责

一个 Python 字符串常量（HTML + CSS + 内联 JS），加一个填占位符的函数。
**不引入前端构建链**（S1 禁止事项，D6 不变）。

## 符号表

| 符号 | 行 | 做什么 |
|---|---|---|
| `HEARTBEAT_INTERVAL_MS` | 29 | `1000`，心跳间隔 |
| `RECONNECT_DELAY_MS` | 30 | `500`，点重连到发起的延迟 |
| `REQUIRED_PARAMS` | 33 | 全部占位符元组（9 个） |
| `ERROR_TEXT` | 47 | 07 §1.3 错误码 → 用户可读中文（S3 左栏用） |
| `ERROR_TEXT_FALLBACK` | 54 | 表外的码走这句通用文案 |
| `INDEX_HTML` | 56 | 页面本体 |
| `error_text_json()` | 428 | 把 `ERROR_TEXT` 序列化成 JS 对象字面量 |
| `render_index(...)` | 437 | 填占位符 |

页面内新增的 DOM / JS（S3 左栏仓库区）：

| 元素/函数 | 行 | 做什么 |
|---|---|---|
| `<section id="repos" data-state="loading">` | 157 | 仓库区；**初始 `loading`** 而不是 ready（D1） |
| `#bind-form` / `#bind-path` / `#bind-button` | 159-162 | 输入框 + 「绑定」按钮（真 `<form>`，回车也能提交） |
| `#repos-error` / `#repo-state` / `#repo-list` / `#repo-empty` | 164-167 | 错误 / 连接状态 / 列表 / 空态 |
| `setRepoState(state, text)` | 217 | 连接状态 → `#repos` 的 `data-state`（**一次改属性，显隐全交给 CSS**） |
| `errorText(code)` | 231 | 错误码 → 文案（**不看 message**） |
| `renderRepos(list)` | 237 | 一行 = 名称 + 「默认分支 X」 |
| `loadRepos()` | 257 | 连上后发 `repo.list` |
| `submitBind()` | 266 | 发 `repo.bind`，成功/失败各自收口 |
| `request(method, params, done)` | 314 | 带回调的帧请求（回调拿 `data` 或**错误码**） |

页面里 JS 用的常量（`assets.py:95-97`）：

| JS 常量 | 值 | 来自 |
|---|---|---|
| `HEARTBEAT_INTERVAL_MS` | 1000 | Python 常量注入 |
| `RECONNECT_DELAY_MS` | 500 | Python 常量注入 |
| `CONNECT_TIMEOUT_MS` | 2000 | **JS 内硬编码**（JS 里 `var CONNECT_TIMEOUT_MS`） |
| `ERROR_TEXT` | 07 §1.3 的码 → 中文 | **Python 常量注入**（`error_text_json()`） |

## 关键决策与理由

### 为什么用 `%%NAME%%` 占位符而不是 `str.format`

页面里全是 CSS/JS 的花括号（`function () { ... }`、`@media { }`）。
`str.format` 要求把所有字面花括号双写成 `{{ }}`，**漏一个就 `KeyError` 或静默错替换**。

用 `str.replace` + `%%NAME%%` 规避。`REQUIRED_PARAMS` + 末尾的 `assert` 保证
「占位符清单」与「替换表」不会漂移。

> `test_render_does_not_confuse_braces_with_placeholders` 与
> `test_parse_page_round_trip` 守着这条。

### 为什么常量在 Python 里定义、再注入 JS

因为测试要断言它们。`%%HEARTBEAT_INTERVAL_MS%%` 让 Python 侧能：

```python
assert HEARTBEAT_INTERVAL_MS + RECONNECT_DELAY_MS < 3000  # 断连预算
```

并且 `test_page_inlines_the_same_constants_it_tests` 断言**页面里出现的数字与 Python 常量一致**
——否则测试测的是「Python 里那个数」，页面里可能是另一个数。

### 断线判定只有两种信号（都必要）

| 信号 | 何时有用 |
|---|---|
| socket 的 `close` / `error` 事件 | 内核优雅退出（SIGTERM）会发关闭帧 |
| 心跳 `system.echo` 到点没回包 | **内核被 SIGKILL 时对端不会发 close 帧**，只有心跳能发现 |

**缺任何一个都会漏判。** 这也是为什么 `system.echo` 这个方法存在——
它是通道自检，不是业务接口（07 §2.1 明确写了「不得给它加业务语义」）。

### 状态机只有一个「向下」出口

```js
function fail() {            // 唯一的「向下」出口：三态里只有已断开是终态
  clearTimers(); socket = null; setState("disconnected", "已断开");
}
```

`setState()` 里统一 `content.classList.toggle("stale", state !== "connected")` ——
**一个出口，不会只加不删**。

### 重连流程（`assets.py:218`）

```
点按钮 → setState("reconnecting","重连中") → 等 RECONNECT_DELAY_MS
   → freshTicket():
        ① fetch("/api/where")
              - 失败 → throw
              - port !== location.port → throw PORT_CHANGED
        ② fetch("/")  → DOMParser 抠 <meta name="postel-handshake">
   → connect(ticket) → ws.onopen → setState("connected")
   catch → fail() + setNote("内核已换端口或未启动，请从启动它的终端重新打开地址")
```

**不 `location.reload()`** ——端口变了的话重载也还是旧地址，只会骗人。
`test_frontend_reports_port_change_and_does_not_guess` 断言页面里**根本没有** `location.reload()`。

### `ws://` 是硬编码的，没有 `wss://` 分支

内核只绑 `127.0.0.1` 且只讲明文 `ws://`（05 §1.1）。
S2 初版有一个 `location.protocol === "https:"` 的病态分支，**从未被执行过**，
已删除（死代码）。

> 将来做远程访问需要 `wss://` 时，**重新写**，别从 git 历史里捞回那个假分支。

### S3：左栏仓库区 —— 「没连上」与「没有仓库」必须可区分（D1）

```
连上之前 / 断连时：#repos[data-state="loading"|"reconnecting"|"disconnected"]
    → #repo-state 显示「正在加载…」/「正在连接内核…」/「已断开：内核没有连着，仓库列表未知」
    → #repo-list 与 #repo-empty 都隐藏；输入框与按钮禁用
连上只后：        #repos[data-state="ready"]
    → 发 repo.list；**拿到一个（可能为空的）数组** 才允许 `#repo-empty` 出现
```

三条实现细节，改的时候别破坏：

1. **显隐全部由 CSS 的 `#repos[data-state=…]` 规则决定**，JS 只改 `data-state` 这一个属性。
   T8 的教训（`style.display = ""` 回退到 CSS 的 `none`）在需要显隐的元素上一律不适用：
   JS 只动 `hidden` 属性（`#repos-error` / `#repo-empty`），或什么都不动。
   `setState()` 里给重连按钮的那句 `style.display = ... ? "inline-block" : "none"` 是**显式值**，
   是 S2 的写法，保留（T8 的正解）。
2. **空态只在拿到数组后才允许出现**：断连时 `#repo-empty` 必须隐藏——否则用户看到
   「还没有绑定仓库」，那是**撒谎**（D1 的原话：空 与 没连上必须可区分）。
3. **错误按码映射**（`errorText`）：`NOT_A_REPO → "不是 git 仓库"`。不把服务端 `message`
   直接贴到页面上（05 §4.2 的同一精神），表外的码给 `ERROR_TEXT_FALLBACK`。

`repo.bind` 成功后**重读** `repo.list`（而不是把返回值插进列表）：重复绑定靠这条路自然去重，
顺序也由服务端决定（`added_at`）。

## 修改须知

| 如果你要… | 还必须 |
|---|---|
| 加占位符 | `REQUIRED_PARAMS` + `render_index` 的 `values` + `app.index()` 传参 |
| 改 `HEARTBEAT_INTERVAL_MS` 或 `RECONNECT_DELAY_MS` | **会动「≤3 秒变红」的承诺**；`test_frontend_disconnect_budget_constants` 会失败 |
| 加 DOM 元素 | 在真实浏览器里验证（见 §测试）；Web 页面无构建链，无法用 JS 单测 |
| 让页面调新的 HTTP 路由 | 注意 `/api/*` 返回**裸对象**，不套 `{data,seq}` 帧包络 |
| 在页面里放凭据 | **禁止**。只有一次性 ticket，且 60 秒失效 |
| 加左栏分区（Bot 在上 / 仓库在下，03-ui-spec §2） | 现在**只有仓库区**，没有 Bot 区（S3 不做 Bot）；加 Bot 区时 `#repos` 的 `data-state` 机制可以复用 |
| 改 `ERROR_TEXT` | `render_index` 会重新序列化；真实浏览器里验一遍（T9） |
| 让前端直接读 git / 开新 HTTP 路由 | **禁止**（S3 D1）：数据一律走 WS 帧 |

## 测试

**本模块只被「文本断言」守着**，不是行为断言：

| 测试 | 守什么 |
|---|---|
| `test_page_declares_status_lamp_and_three_states` | 三个 `data-state` + 三个中文字面量 |
| `test_page_has_reconnect_button` | 元素存在 + CSS 缺省 `display:none` |
| `test_page_has_stale_rule_and_toggles_it_on_disconnect` | CSS 规则 + toggle 语句存在 |
| `test_frontend_reports_port_change_and_does_not_guess` | 有 `/api/where`、有 `PORT_CHANGED`、**无** `location.reload()` |
| `test_frontend_still_uses_same_origin_first` | 先 fetch 同源 |
| `test_frontend_disconnect_budget_constants` | 常量之和 < 3000 |
| `test_page_inlines_the_same_constants_it_tests` | 页面里的数字 == Python 常量 |
| `test_render_does_not_confuse_braces_with_placeholders` | 花括号不被误换 |
| `test_parse_page_round_trip` | 渲染往返 |
| `test_page_does_not_carry_the_long_term_token` | 令牌不进页面 |
| `test_unknown_method_is_unknown_method_not_subscribe` | `repo.bind` 不再是"未实现"的反例（S3 已实现） |

S3 的页面行为**没有**进 pytest（T9 的老问题，S3 也没解决）。验收 1–4 与 D1 的断连可区分
由 **`tools/acceptance/s3_bind.mjs`** 验：真 Chrome + 真内核 + 读真 DOM，**跳过而不失败**
（没 Chrome/puppeteer 时退出码 0 并说明原因）。它不是 pytest、不进依赖、不跑 CI，
用法见 [tools/acceptance/README.md](../../../tools/acceptance/README.md)。

改了左栏的 DOM 结构或数据来源，**必须**重跑它：

```bash
bash scripts/check.sh && node tools/acceptance/s3_bind.mjs
```

### ⚠️ 这些断言证明的是「意图存在」，不是「行为正确」

`'classList.toggle("stale", state !== "connected")' in page` 只能证明**这句话在字符串里**，
不能证明**灯真的会变红**。

**真实行为目前靠人工**：用 Chrome headless + 真内核 + SIGKILL 跑一次，肉眼/脚本读 DOM。
（S2 审核时实测：变红 5~22ms、按钮出现、灰化生效、换端口时文案正确且不跳页。）

**为什么没进 pytest**：需要引入 Node + puppeteer，与「不得引入前端构建链」冲突。
**这是本文件最薄的环节，任何人接手前应知道自己站的地方有多软。**

若将来要自动化，候选路径（未实现，仅供参考）：
1. 允许一个 **dev-only** 的 Node 依赖跑 headless 验收，标记 `@pytest.mark.slow`；
2. 把状态机抽成纯函数（无 DOM 依赖）并在 Python 里用 JS 引擎跑——成本更高；
3. 用 `httpx` + `websockets` 模拟内核被杀，再用静态分析断言状态转移完整——**证明力很弱，不建议**。