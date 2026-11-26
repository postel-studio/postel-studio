# 03 · assets — `src/postel_studio/web/assets.py`

> 层 L4 · 683 行 · 上游：[模块索引](README.md) · 相关：[03-ui-spec §2](../../design/03-ui-spec.md) · [06-pitfalls T8/T9/T27](../06-pitfalls.md)
> ⚠️ **本模块是 Code Map 里覆盖最薄的一块**：JS 的 DOM 行为没有自动化测试。见 §测试。

## 职责

一个 Python 字符串常量（HTML + CSS + 内联 JS），加一个填占位符的函数。
**不引入前端构建链**（S1 禁止事项，D6 不变）。

S4 起主区有「分支」面板（D6 的 DOM 契约）：左栏仓库行可选中（鼠标 + 键盘）→ 发
`repo.branches` → 渲染分支列表 / 空态 / 错误 / 断连回 idle。

## 符号表

| 符号 | 行 | 做什么 |
|---|---|---|
| `HEARTBEAT_INTERVAL_MS` | 38 | `1000`，心跳间隔 |
| `RECONNECT_DELAY_MS` | 39 | `500`，点重连到发起的延迟 |
| `REQUIRED_PARAMS` | 42 | 全部占位符元组（9 个；S4 没加新占位符） |
| `ERROR_TEXT` | 56 | 07 §1.3 错误码 → 用户可读中文（S3 左栏 + S4 分支面板共用；S4 补了 `REPO_NOT_BOUND`） |
| `ERROR_TEXT_FALLBACK` | 66 | 表外的码走这句通用文案（**只给绑定表单**；分支面板有自己那句） |
| `INDEX_HTML` | 68 | 页面本体 |
| `error_text_json()` | 646 | 把 `ERROR_TEXT` 序列化成 JS 对象字面量 |
| `render_index(...)` | 655 | 填占位符 |

页面内新增的 DOM / JS（S3 左栏仓库区）：

| 元素/函数 | 行 | 做什么 |
|---|---|---|
| `<section id="repos" data-state="loading">` | 218 | 仓库区；**初始 `loading`** 而不是 ready（D1） |
| `#bind-form` / `#bind-path` / `#bind-button` | 220-223 | 输入框 + 「绑定」按钮（真 `<form>`，回车也能提交） |
| `#repos-error` / `#repo-state` / `#repo-list` / `#repo-empty` | 225-228 | 错误 / 连接状态 / 列表 / 空态 |
| `setRepoState(state, text)` | 291 | 连接状态 → `#repos` 的 `data-state`（**一次改属性，显隐全交给 CSS**）；非 connected 时把分支面板重置回 idle |
| `errorText(code)` | 310 | 错误码 → 文案（**不看 message**；绑定表单用） |
| `renderRepos(list)` | 318 | 一行 = 名称 + 「默认分支 X」；`li` 可选中（`tabindex` / `role=button` / `data-selected`），重建后恢复选中标记 |
| `loadRepos()` | 366 | 连上后发 `repo.list` |
| `submitBind()` | 484 | 发 `repo.bind`，成功/失败各自收口 |
| `request(method, params, done)` | 532 | 带回调的帧请求（回调拿 `data` 或**错误码**） |

S4 新增的分支面板（D6）：

| 元素/函数 | 行 | 做什么 |
|---|---|---|
| `<section id="branches" data-state="idle">` | 208 | 主区分支面板，位置在 `#content` 之后；初始 idle |
| `#branches-title` / `#branches-state` / `#branches-error` / `#branch-list` / `#branch-empty` | 209-213 | 标题 / 状态文案 / 错误 / 列表 / 空态（DOM 契约，id 不许改） |
| `BRANCH_ERROR_FALLBACK` | 275 | 分支面板**自己的**兜底「读取分支列表失败」（不用 `ERROR_TEXT_FALLBACK`） |
| `branchErrorText(code)` | 382 | 错误码 → 中文；表外走分支自己的兜底 |
| `resetBranches(state, text)` | 390 | 清空列表 + 回指定 state；断连时 `idle` + 「已断开：分支列表未知」 |
| `applyRepoSelection()` | 401 | 左边行 `data-selected="true"`（选中标记只在这一个函数里打/清） |
| `renderBranches(list)` | 416 | 一行 = `.branch-name` / `.branch-sha` / `.branch-upstream`（有才建）/ `.branch-current`（仅当前） |
| `loadBranches()` | 446 | 发 `repo.branches`；用 `branchRequestToken` 丢弃过期回包 |
| `selectRepo(repoId, name)` | 476 | 选中仓库：更新选中态 + 更新标题 + 取分支 |

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

### S4：主区分支面板（D6）——「点仓库 → 看到里面有什么」

DOM 契约（**id / `data-state` 值 / 类名都是契约，浏览器验收按这些点**）：

```html
<section id="branches" data-state="idle" aria-live="polite">
  <h2 id="branches-title">分支</h2>
  <p id="branches-state">从左侧选择一个仓库</p>
  <p id="branches-error" role="alert" hidden></p>
  <ul id="branch-list"></ul>
  <p id="branch-empty" hidden>这个仓库还没有任何分支（空仓库）。</p>
</section>
```

| 规则 | 实现 |
|---|---|
| `data-state` 四值 | `idle`（未选/断开）/ `loading` / `ready` / `error`；显隐全由 CSS 规则决定（T8） |
| 标题 | `#branches-title.textContent`：未选「分支」，选中后「分支 · <name>」（**不拼 HTML**） |
| 每行 | `li[data-branch-name]` 含 `.branch-name` / `.branch-sha`（8 位）/ `.branch-current`（文本「当前」，仅当前分支）/ `.branch-upstream`（有 upstream **才创建**，`null` 不建 span） |
| 空仓库 | `ready` + `#branch-empty` 可见 + `#branch-list` 空——不是 error，不是空白 |
| 失败 | `error` + `#branches-error` 显示**按码映射**的中文（复用 `ERROR_TEXT`） |
| 断连 | 回 `idle`、清空列表、`#branches-state` =「已断开：分支列表未知」——不留旧数据装新鲜 |
| 重连 | 选中仓库的 id **保留**；`setState(connected)` → `loadRepos()` → `renderRepos()` 里自动 `loadBranches()` |
| 选中仓库消失 | 回 `idle` 并清空（`renderRepos` 里 `stillListed` 判定） |

四条实现细节，改的时候别破坏：

1. **左栏仓库行是按钮**（D6）：`tabindex="0"` + `role="button"`，`click` 与 `Enter`/`Space`
   都调 `selectRepo()`；`Space` 要 `preventDefault()`（否则页面滚动）。选中标记是
   `data-selected="true"`，样式（绿字 + 左侧竖条）挂在属性上，**不在 JS 里改颜色**。
2. **`renderRepos` 是整体重建的**：重建后必须重新打 `data-selected`（`applyRepoSelection()`），
   否则每次 `repo.list`（绑定/重连）都会把选中样式冲掉——D6 点名要求恢复。
3. **错误兜底分两套**：绑定表单用 `ERROR_TEXT_FALLBACK`（「绑定失败」）；分支面板用
   `BRANCH_ERROR_FALLBACK`（「读取分支列表失败」）。把绑定那句放到分支面板上是错的
   （S4 实施时管理层补的裁决）。两套都**按码**映射，不读服务端 `message`。
4. **过期回包**：`branchRequestToken` 在每次请求 / 重置时 +1，回调里 token 不匹配就丢弃。
   否则"快速点 A→B→A"时 A 的旧回包可能覆盖 B 的结果。

**所有进 DOM 的文本都用 `textContent`**（仓库名/分支名/upstream 是用户数据），全页零
`innerHTML`——`test_page_never_builds_dom_with_innerhtml` 是这条的静态守卫。

## 修改须知

| 如果你要… | 还必须 |
|---|---|
| 加占位符 | `REQUIRED_PARAMS` + `render_index` 的 `values` + `app.index()` 传参 |
| 改 `HEARTBEAT_INTERVAL_MS` 或 `RECONNECT_DELAY_MS` | **会动「≤3 秒变红」的承诺**；`test_frontend_disconnect_budget_constants` 会失败 |
| 加 DOM 元素 | 在真实浏览器里验证（见 §测试）；Web 页面无构建链，无法用 JS 单测 |
| 让页面调新的 HTTP 路由 | 注意 `/api/*` 返回**裸对象**，不套 `{data,seq}` 帧包络 |
| 在页面里放凭据 | **禁止**。只有一次性 ticket，且 60 秒失效 |
| 加左栏分区（Bot 在上 / 仓库在下，03-ui-spec §2） | 现在**只有仓库区**，没有 Bot 区（S3 不做 Bot）；加 Bot 区时 `#repos` 的 `data-state` 机制可以复用 |
| 改 `ERROR_TEXT` | `render_index` 会重新序列化；真实浏览器里验一遍（T9）。S4 补了 `REPO_NOT_BOUND`（S3 漏的），删掉它分支面板会显示「读取分支列表失败」 |
| 改 `#branches` 的 DOM 结构 / `data-state` 语义 / 行内类名 | **D6 是契约**；`node tools/acceptance/s4_branches.mjs` 与 `test_branches.py` 的页面契约用例会红 |
| 改左栏仓库行的交互（选中 / 键盘） | D6：`click` 与 `Enter`/`Space` 都要能选；`renderRepos` 重建后要恢复 `data-selected`；真实浏览器里验一遍 |
| 把分支面板的错误兜底改成 `ERROR_TEXT_FALLBACK` | **禁止**：那句是绑定场景的「绑定失败」（S4 实施裁决） |
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

### S4 的页面契约与真实浏览器验收

新增的 pytest 用例都在 `tests/test_branches.py`（**文本断言**，只能证明"意图存在"，见上面 T9 的警告）：

| 测试 | 守什么 |
|---|---|
| `test_page_declares_branch_panel_dom_contract` | D6 的 7 个 id / `data-state="idle"` / `role="alert"` / 空态文案 |
| `test_page_branch_panel_visibility_is_css_driven` | `#branches[data-state=…]` 的 CSS 规则 + JS 只改 `dataset.state` |
| `test_page_branch_error_text_has_its_own_fallback` | `REPO_NOT_BOUND` 在 `ERROR_TEXT` 里 + 分支面板用 `BRANCH_ERROR_FALLBACK`（不是「绑定失败」） |
| `test_page_repo_rows_are_keyboard_selectable` | `tabIndex = 0` / `role="button"` / `Enter`+`Space` 的 keydown |
| `test_page_never_builds_dom_with_innerhtml` | 全页没有 `innerHTML`（文本一律 `textContent`） |

真实行为由 **`node tools/acceptance/s4_branches.mjs`** 验（真 Chrome + 真内核 + 真 git：
2 个仓库、点行、读 DOM、CDP 断网看重连；**跳过而不失败**，不进依赖、不进 CI）。
改左栏/分支面板的 DOM、状态机或数据来源，**必须**重跑它：

```bash
bash scripts/check.sh
node tools/acceptance/s4_branches.mjs
```
