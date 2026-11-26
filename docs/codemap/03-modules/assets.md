# 03 · assets — `src/postel_studio/web/assets.py`

> 层 L4 · 267 行 · 上游：[模块索引](README.md)
> ⚠️ **本模块是 Code Map 里覆盖最薄的一块**：JS 的 DOM 行为没有自动化测试。见 §测试。

## 职责

一个 Python 字符串常量（HTML + CSS + 内联 JS），加一个填占位符的函数。
**不引入前端构建链**（S1 禁止事项，D6 不变）。

## 符号表

| 符号 | 行 | 做什么 |
|---|---|---|
| `HEARTBEAT_INTERVAL_MS` | 19 | `1000`，心跳间隔 |
| `RECONNECT_DELAY_MS` | 20 | `500`，点重连到发起的延迟 |
| `REQUIRED_PARAMS` | 23 | 全部占位符元组（7 个） |
| `INDEX_HTML` | 33 | 页面本体 |
| `render_index(version, api_version, state_root, handshake_ticket, ws_path)` | 242 | 填占位符 |

页面里 JS 用的常量（`assets.py:95-97`）：

| JS 常量 | 值 | 来自 |
|---|---|---|
| `HEARTBEAT_INTERVAL_MS` | 1000 | Python 常量注入 |
| `RECONNECT_DELAY_MS` | 500 | Python 常量注入 |
| `CONNECT_TIMEOUT_MS` | 2000 | **JS 内硬编码**（`assets.py:97`） |

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

## 修改须知

| 如果你要… | 还必须 |
|---|---|
| 加占位符 | `REQUIRED_PARAMS` + `render_index` 的 `values` + `app.index()` 传参 |
| 改 `HEARTBEAT_INTERVAL_MS` 或 `RECONNECT_DELAY_MS` | **会动「≤3 秒变红」的承诺**；`test_frontend_disconnect_budget_constants` 会失败 |
| 加 DOM 元素 | 在真实浏览器里验证（见 §测试）；Web 页面无构建链，无法用 JS 单测 |
| 让页面调新的 HTTP 路由 | 注意 `/api/*` 返回**裸对象**，不套 `{data,seq}` 帧包络 |
| 在页面里放凭据 | **禁止**。只有一次性 ticket，且 60 秒失效 |

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