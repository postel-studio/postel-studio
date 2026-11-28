# 03 · assets — `src/postel_studio/web/assets.py`

> 层 L4 · 2466 行 · 上游：[模块索引](README.md) · 相关：[03-ui-spec §2/§4.4](../../design/03-ui-spec.md) · [06-pitfalls T8/T9/T27](../06-pitfalls.md)
> ⚠️ **本模块是 Code Map 里覆盖最薄的一块**：JS 的 DOM 行为没有自动化测试。见 §测试。

## 职责

一个 Python 字符串常量（HTML + CSS + 内联 JS），加一个填占位符的函数。
**不引入前端构建链**（S1 禁止事项，D6 不变）。

S4 起主区有「分支」面板（D6 的 DOM 契约）：左栏仓库行可选中（鼠标 + 键盘）→ 发
`repo.branches` → 渲染分支列表 / 空态 / 错误 / 断连回 idle。

S5 起主区还有「差异」面板（D9 的 DOM 契约）：点分支行（鼠标 + 键盘）或手填两个 ref →
发 `repo.diff` → 统计串 + 逐行着色 patch / 截断提示 / 按码映射的错误 / 断连回 idle。
**diff 是不可信文本**（文件名可以是 `<script>`）：进 DOM 一律 `textContent`，全页零 `innerHTML`。

S6 起左栏还有「Bots」区、主区还有只读的「Charter」面板（D8 的 DOM 契约）：点仓库行 →
`bot.list` → 一行一个 Bot（名字 / 权限徽标 / charter 摘要）→ 点 Bot 行（鼠标或 `Enter`）→
`memory.charter.get` → 全文 `<pre>` + 记忆目录绝对路径。**charter 是用户可编辑文本 = 不可信**：
只进 `textContent`，同样零 `innerHTML`。

## 符号表

| 符号 | 行 | 做什么 |
|---|---|---|
| `HEARTBEAT_INTERVAL_MS` | 43 | `1000`，心跳间隔 |
| `RECONNECT_DELAY_MS` | 44 | `500`，点重连到发起的延迟 |
| `REQUIRED_PARAMS` | 47 | 全部占位符元组（9 个；S4/S5 都没加新占位符） |
| `ERROR_TEXT` | 61 | 07 §1.3 错误码 → 用户可读中文（S3 左栏 + S4 分支面板 + S5 差异面板共用；S4 补 `REPO_NOT_BOUND`，S5 补 `BAD_REF` / `NOT_SUPPORTED`） |
| `ERROR_TEXT_FALLBACK` | 79 | 表外的码走这句通用文案（**只给绑定表单**；分支/差异面板各有自己那句） |
| `INDEX_HTML` | 81 | 页面本体 |
| `error_text_json()` | 1629 | 把 `ERROR_TEXT` 序列化成 JS 对象字面量 |
| `render_index(...)` | 1638 | 填占位符 |

页面内的 DOM / JS（S3 左栏仓库区）：

| 元素/函数 | 行 | 做什么 |
|---|---|---|
| `<section id="repos" data-state="loading">` | 401 | 仓库区；**初始 `loading`** 而不是 ready（D1） |
| `#bind-form` / `#bind-path` / `#bind-button` | 346-349 | 输入框 + 「绑定」按钮（真 `<form>`，回车也能提交） |
| `#repos-error` / `#repo-state` / `#repo-list` / `#repo-empty` | 351-354 | 错误 / 连接状态 / 列表 / 空态 |
| `setState(state, text)` | 598 | 状态灯 + `#content.stale` + 转给 `setRepoState` |
| `setRepoState(state, text)` | 609 | 连接状态 → `#repos` 的 `data-state`（**一次改属性，显隐全交给 CSS**）；非 connected 时把分支/差异面板重置回 idle |
| `errorText(code)` | 641 | 错误码 → 文案（**不看 message**；绑定表单用） |
| `renderRepos(list)` | 649 | 一行 = 名称 + 「默认分支 X」；`li` 可选中（`tabindex` / `role=button` / `data-selected`），重建后恢复选中标记；同时把 `defaultBranch` 存进 `repoDefaultBranches` 供差异面板用 |
| `loadRepos()` | 708 | 连上后发 `repo.list` |
| `submitBind()` | 1442 | 发 `repo.bind`，成功/失败各自收口 |
| `request(method, params, done)` | 1490 | 带回调的帧请求（回调拿 `data` 或**错误码**） |

S4 的分支面板（D6）：

| 元素/函数 | 行 | 做什么 |
|---|---|---|
| `<section id="branches" data-state="idle">` | 339 | 主区分支面板，位置在 `#content` 之后；初始 idle |
| `#branches-title` / `#branches-state` / `#branches-error` / `#branch-list` / `#branch-empty` | 302-306 | 标题 / 状态文案 / 错误 / 列表 / 空态（DOM 契约，id 不许改） |
| `BRANCH_ERROR_FALLBACK` | 515 | 分支面板**自己的**兜底「读取分支列表失败」（不用 `ERROR_TEXT_FALLBACK`） |
| `branchErrorText(code)` | 724 | 错误码 → 中文；表外走分支自己的兜底 |
| `resetBranches(state, text)` | 732 | 清空列表 + 回指定 state；断连时 `idle` + 「已断开：分支列表未知」 |
| `applyRepoSelection()` | 743 | 左边行 `data-selected="true"`（选中标记只在这一个函数里打/清） |
| `renderBranches(list)` | 758 | 一行 = `.branch-name` / `.branch-sha` / `.branch-upstream`（有才建）/ `.branch-current`（仅当前）；**S5 起每行可点/键盘可达**（`tabIndex` / `role=button` / `data-selected`），重建后恢复选中 |
| `loadBranches()` | 802 | 发 `repo.branches`；用 `branchRequestToken` 丢弃过期回包 |
| `selectRepo(repoId, name)` | 1422 | 选中仓库：更新选中态 + 标题 + **清空差异面板** + 取分支 |

S5 的差异面板（D9）：

| 元素/函数 | 行 | 做什么 |
|---|---|---|
| `<section id="diff" data-state="idle">` | 348 | 主区差异面板，位置在 `#branches` 之后；初始 idle |
| `#diff-title` / `#diff-base` / `#diff-head` / `#diff-compare` / `#diff-state` / `#diff-error` / `#diff-truncated` / `#diff-stat` / `#diff-patch` | 311-322 | 标题 / 两个 ref 输入 + 对比按钮 / 状态文案 / 错误 / 截断提示 / 统计 / patch（DOM 契约，id 不许改） |
| `DIFF_ERROR_FALLBACK` / `DIFF_HINT` | 419-420 | 差异面板自己的兜底「读取差异失败」/ 初始提示文案 |
| `repoDefaultBranches` | 534 | `repoId → defaultBranch`（点分支行时 base 的来源；`null` = HEAD 游离） |
| `selectedBranchName` | 535 | 当前点选的分支行（列表重建后恢复 `data-selected`） |
| `diffErrorText(code)` | 840 | 错误码 → 中文；表外走差异面板自己的兜底 |
| `diffTitleText()` | 844 | `差异` / `差异 · <repoName>`（`textContent`，不拼 HTML） |
| `clearDiffContent(state, text)` | 849 | 清空统计/patch/错误/截断 + 回指定 state；`diffRequestToken += 1` 让在途回包作废 |
| `applyBranchSelection()` | 863 | 分支行 `data-selected` 的唯一打/清点（重建后恢复） |
| `resetDiff(state, text)` | 876 | 切仓库 / 断连的收口：清分支选中 + `clearDiffContent` |
| `diffLineKind(line)` | 884 | **按 D9 的顺序**分类：meta（`diff --git`/`index`/mode/`\\`）→ hunk（`@@`）→ meta（`+++ `/`--- `）→ add → del → ctx |
| `renderDiffPatch(patch)` | 896 | 一行一个 `<div class="diff-line diff-<kind>">`，文本 `textContent`（不可信） |
| `diffStatText(stat)` | 908 | `<N> 个文件 · +<X> −<Y>`（U+00B7 / U+2212） |
| `requestDiff(base, head)` | 913 | 发 `repo.diff`；`diffRequestToken` 丢弃过期回包；处理 truncated / 空 patch |
| `selectBranch(name)` | 952 | 点分支行：填两个输入框；默认分支本身 / 无默认分支 → idle + 文案、**不发请求** |
| `submitDiff()` | 972 | 手填表单：trim 后为空 → `#diff-error`「请填 base 和 head」、state 不变 |

S6 的 Bots 区与 charter 面板（D8）：

| 元素/函数 | 行 | 做什么 |
|---|---|---|
| `<section id="charter" data-state="idle">` | 364 | 主区 charter 面板，位置在 `#diff` 之后；初始 idle |
| `#charter-title` / `#charter-state` / `#charter-error` / `#charter-memory` / `#charter-content` | 327-332 | 标题（`Charter` / `Charter · <bot>`）/ 状态文案 / 错误 / `记忆目录：<绝对路径>` / 全文（**一个 `<pre>` + `textContent`**） |
| `<section id="bots" data-state="idle">` | 392 | 左栏 Bots 区，位置在 `#repos` **之前**（03-ui-spec §2：左栏上 Bot / 下仓库） |
| `#bots-title` / `#bots-state` / `#bots-error` / `#bot-list` / `#bot-empty` | 336-340 | 标题 / 状态文案 / 错误 / 列表 / 空态（DOM 契约，id 不许改） |
| `botsEl` … `charterContent` | 426-435 | 两个面板的元素引用 |
| `BOT_HINT` / `CHARTER_HINT` / `BOT_ERROR_FALLBACK` / `CHARTER_ERROR_FALLBACK` | 437-441 | 提示文案与**各自**的兜底（不用绑定表单那句「绑定失败」） |
| `selectedBotId` / `selectedBotName` / `botRequestToken` / `charterRequestToken` | 442-446 | 选中的 Bot（与 repo 强绑定）+ 两个过期回包拦截器 |
| `botErrorText(code)` / `charterErrorText(code)` | 840/844 | 按码映射；`charterErrorText` 把 `INVALID_PARAMS` 翻成「找不到这个 Bot」（D6） |
| `charterTitleText()` | 1023 | `Charter` / `Charter · <botName>` |
| `resetBots(state, text)` | 1027 | 清列表 + 回 state；`botRequestToken += 1` 让在途回包作废 |
| `applyBotSelection()` | 1038 | Bot 行 `data-selected` 的唯一打/清点（列表重建后恢复） |
| `renderBots(list)` | 1053 | 一行 = `.bot-name` / `.bot-permission` / `.bot-charter-summary`；`li` 键盘可达；选中的 Bot 消失 → charter 回 idle |
| `loadBots()` | 1119 | 发 `bot.list`；`botRequestToken` 丢弃过期回包 |
| `resetCharter(state, text)` | 1205 | 清记忆目录/全文/错误 + 回 state（切仓库、断连） |
| `loadCharter(botId, botName)` | 1227 | 发 `memory.charter.get`；全文与路径只进 `textContent`（D8/D9） |
| `selectBot(botId, botName)` | 1414 | 选中 Bot → 取 charter |
| `setRepoState` / `renderRepos` / `selectRepo` | 457/495/1006 | S6 钩子：断连重置两个面板、仓库列表重建后重取 `bot.list`、切仓库清 charter |

S8 的新建 Bot 表单与 charter 面板的权限下拉（D8）：

| 元素/函数 | 行 | 做什么 |
|---|---|---|
| `#bot-new` | 438 | 「新建 Bot」按钮；**只在 `#bots[data-state=ready]` 出现**（CSS 管） |
| `#bot-create-form` / `#bot-create-name` / `#bot-create-permission` | 439-447 | 表单（`hidden` 开关 + `[hidden]{display:none}` 显式规则）与四个选项的下拉 |
| `#bot-create-submit` / `#bot-create-cancel` / `#bot-create-error` | 449-452 | 提交（飞行中 `disabled`）/ 取消 / `role=alert` 的错误行 |
| `#charter-permission-row` / `#charter-permission` | 400-405 | charter 面板里的权限下拉；同样四个选项，`change` 即提交 |
| `botCreateErrorText(code)` / `permissionErrorText(code)` | 1002/1011 | 面板**自己的**码→文案（`ALREADY_EXISTS` / `NOT_SUPPORTED` 不进全局表） |
| `syncBotCreateButton()` | 1150 | 提交按钮的 `disabled` 只看「忙不忙 + 表单开没开」 |
| `openBotCreate()` / `closeBotCreate(silent)` | 1154/1165 | 开：清空 + 权限回 L1 + 聚焦名字；关：清空 + **焦点回按钮**（`silent` 不抢焦点） |
| `submitBotCreate()` | 1177 | 发 `bot.create`；成功 → 关表单 + `loadBots()`；失败 → 错误行 + 焦点回名字框 |
| `renderCharterPermission(botId)` | 1223 | 下拉的值只来自 `botPermissions`（`bot.list` 的同一份事实） |
| `submitPermissionChange()` | 1260 | 发 `bot.set_permission`；成功 → 更新徽标（`loadBots`）；失败 → **回滚下拉** + 错误行 |

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

### S5：主区差异面板（D9）——「点分支 → 看到 diff」

DOM 契约（**id / `data-state` 值 / 类名都是契约，浏览器验收按这些点**，见
[03-ui-spec §4.4](../../design/03-ui-spec.md)）：

```html
<section id="diff" data-state="idle" aria-live="polite">
  <h2 id="diff-title">差异</h2>
  <form id="diff-form" autocomplete="off">… #diff-base → #diff-head → #diff-compare …</form>
  <p id="diff-state">点左侧分支查看它与默认分支的差异，或在上方填两个 ref</p>
  <p id="diff-error" role="alert" hidden></p>
  <p id="diff-truncated" hidden>已截断：差异太大，只显示前一部分</p>
  <p id="diff-stat" hidden></p>
  <pre id="diff-patch" hidden></pre>
</section>
```

| 规则 | 实现 |
|---|---|
| `data-state` 四值 | `idle`（未选/断开）/ `loading` / `ready` / `error`；**显隐只由 CSS 的 `#diff[data-state=…]` 决定**（T8），JS 只改属性与 `hidden` |
| 标题 | `#diff-title.textContent`：未选仓库「差异」，选中后「差异 · <repoName>」 |
| 统计 | `ready` 时显示 `<N> 个文件 · +<X> −<Y>`（`·` 是 U+00B7、`−` 是 U+2212；**不是** ASCII `-`） |
| patch | `ready` 时显示；`patch === ""` 时 `hidden`（空差异只显示统计）；每行 `<div class="diff-line diff-<kind>">` |
| 行分类顺序 | **D9 的顺序不许改**：先 meta（文件头/元信息）→ hunk → `+++ `/`--- ` 仍算 meta → `+` 算 add → `-` 算 del → 其它 ctx。`+++ b/` 若先判 `+` 会变成绿色新增行（浏览器验收第 2 条专门钉它） |
| 着色 | `.diff-add{color:#3fb950}` / `.diff-del{color:#f85149}` / `.diff-hunk{color:#58a6ff}` / `.diff-meta{color:#8b949e}` / `.diff-ctx{color:inherit}`；`#diff-patch` `max-height:60vh; overflow:auto; white-space:pre-wrap`，**不设固定宽度、不假设中文宽度** |
| 截断 | `#diff-truncated` 由 `truncated === true` 打开；文案就是 HTML 里那句（**不动态拼数字**）；`#diff-patch` 仍是 `patch[:maxChars]`（可能切在行中间） |
| 错误 | `#diff-error` 按**码**映射（复用 `ERROR_TEXT`；兜底是差异面板自己的「读取差异失败」）；`BAD_REF` →「分支或提交不存在」 |
| 点分支 | `selectBranch()`：填 base=仓库 `defaultBranch`、head=该分支；**默认分支本身** → idle +「这是默认分支，选择其它分支查看差异」；`defaultBranch` 为 `null`（游离 HEAD）→ idle +「不知道默认分支（HEAD 未指向分支）…」；**这两种都不发请求** |
| 手填表单 | trim 后任一为空 → `#diff-error`「请填 base 和 head」、`data-state` 保持原样；否则发 `repo.diff` |
| 切仓库 | idle + 清空 patch/stat/error/**分支行选中**，标题跟着仓库名变（`selectRepo` → `resetDiff`） |
| 断连 | idle + 「已断开：差异未知」+ 清空（同 S4 分支面板纪律）；**重连不自动重发 diff**（diff 是查询历史动作，重放会覆盖用户正在看的对比） |
| 快速切换 | `diffRequestToken` 拦住过期回包（同 `branchRequestToken`） |
| 键盘 | 分支行 `tabindex="0"` + `role="button"`，`Enter`/`Space` 与 click 同一条路径（`preventDefault` 防 Space 滚动） |
| 安全 | 全文件 `innerHTML` 零使用（diff / charter 都是不可信文本）；静态测试钉住 |

**为什么 `repoDefaultBranches` 是「按 repoId 的 map」而不是 `selectRepo` 的参数**：S4 的
`renderRepos` → `selectRepo(repoId, repoName)` 回调形状与页面契约断言是既有资产，S5
**不改它**；默认分支在 `renderRepos` 里顺手记进 map，`selectBranch` 再按 `selectedRepoId` 取。

### S6：charter 是不可信文本，两个面板与分支/差异同源

- **全文只进 `<pre>` 的 `textContent`**：charter 是用户可编辑的 Markdown，可能包含任何字符
  （含 `<script>`）。S6 的浏览器验收专门写一段 `<script>` 进 charter 再点开，断言 DOM 里
  **没有 script 元素**。
- **显隐只由 `data-state` + `hidden`**：`#bots` / `#charter` 与 `#branches` / `#diff` 同一套
  CSS 规则，JS 不逐个 `style.display`（T8）。
- **断连清空、重连重放**（D8）：断连时两个面板回 idle 并清空；重连后若 repo 仍选中则重取
  `bot.list`，选中的 Bot 仍存在时把 charter 重放回来（charter 是静态内容，重放不会覆盖
  用户正在看的对比——这一条与 diff 的「不重放」不同）。
- **两个 token**：`botRequestToken` / `charterRequestToken` 各自拦住过期回包（快速切仓库）。

### S8：新建 Bot —— 表单只是一个动作，列表仍是事实源

- **创建成功后不在本地拼列表**：`submitBotCreate` 成功只做两件事——关表单、`loadBots()`。
  排序（`created_at, bot_id`）与徽标的事实源都在服务端，页面只负责显示。
- **改权限也一样**：`bot.set_permission` 成功后 `loadBots()` 刷新徽标，而不是直接改 DOM 文本。
  下拉的值来自 `botPermissions`（`renderBots` 时从 `bot.list` 填），所以两处永远不会说两句话。
- **失败要回滚控件**：`set_permission` 被拒时把下拉拨回服务端仍持有的值。一个"显示成改了、
  其实没改"的控件比一行错误文案更糟。
- **Escape 绑在 `document` 上**（T41）：提交时按钮会被 `disabled`（防连点），"聚焦元素被禁用"
  会把焦点丢回 `body`，绑在 `<form>` 上的 keydown 就再也收不到——真实浏览器验收当场抓到。
- **四个选项就是全部**：`L3`/`L4` 不出现，**也不灰着放**（灰掉会让人以为"以后能开"）。
  `test_page_permission_select_lists_exactly_four_levels` 静态守着两个下拉。

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
| 改 `#diff` 的 DOM 结构 / `data-state` 语义 / 行分类顺序 | **D9 是契约**；`node tools/acceptance/s5_diff.mjs` 与 `test_diff.py` 的页面契约用例会红 |
| 让 diff 文本走 `innerHTML` | **禁止**：diff 是不可信文本（文件名可以是 `<script>`）；`test_page_never_uses_inner_html` 会红（D12 #5 的反向注入靶子） |
| 改 `#diff` 的显隐 | 只许改 CSS 的 `#diff[data-state=…]` 规则与 `hidden` 属性；**禁止**逐个 `style.display`（T8） |
| 改 `ERROR_TEXT` | `render_index` 会重新序列化；真实浏览器里验一遍（T9）。S5 补了 `BAD_REF` / `NOT_SUPPORTED` |
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
| `test_page_declares_diff_panel_dom_contract` | S5 D9 的 `#diff` DOM 契约（id / 初始 state / 条件元素） |
| `test_page_diff_panel_visibility_is_css_driven` | 显隐由 `#diff[data-state=…]` 决定、无 `style.display`（T8） |
| `test_page_diff_error_text_reuses_error_text_with_its_own_fallback` | 按码映射 + 面板自己的兜底（`BAD_REF` / `NOT_SUPPORTED`） |
| `test_page_diff_rows_use_text_content_and_contract_order` | 行分类顺序 + `textContent` |
| `test_page_diff_stat_and_truncation_have_exact_copy` | 统计串的 U+00B7/U+2212 与截断文案 |
| `test_page_diff_branch_rows_are_keyboard_selectable` | 分支行 `tabindex` / `role=button` / Enter-Space 同路径 |
| `test_page_never_uses_inner_html` | 全文件 `innerHTML` 零使用（D10 第 18 行，反向注入 D12 #5 的靶子） |
| `tests/test_bot_charter.py`（页面契约 3 条，S6） | 两个面板的 id/文案/顺序、行结构、charter 只经 `textContent`、零 `innerHTML` |

S3 的页面行为**没有**进 pytest（T9 的老问题，S3 也没解决）。验收 1–4 与 D1 的断连可区分
由 **`tools/acceptance/s3_bind.mjs`** 验：真 Chrome + 真内核 + 读真 DOM，**跳过而不失败**
（没 Chrome/puppeteer 时退出码 0 并说明原因）。它不是 pytest、不进依赖、不跑 CI，
用法见 [tools/acceptance/README.md](../../../tools/acceptance/README.md)。

S5 的差异面板同样只有文本断言；D11 的 10 条（点分支 → ready/stat 逐字/着色行/默认分支/手填/
BAD_REF/截断/XSS/键盘/断连）由 **`tools/acceptance/s5_diff.mjs`** 在真实 Chrome 里跑。

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


### 候选详情卡（S11）

`#candidate`（`idle/loading/done/error`）位于 `#task` 之后：分支、`+X −Y`、`仅本地`，
点「查看差异」**复用 `#diff` 面板与 `renderDiffPatch`**，没有第二个 diff 渲染件。
`candidate.proposed` 事件只带 `{candidateId, taskId}`，细节走 `candidate.list`；
选仓库/重连时 `loadLatestCandidate()` 取最近一条，所以刷新后详情还在（页面状态本身不落盘）。
断连、换 Bot、换仓库都重置卡片（同 S10 的 `resetTask` 纪律）。


### 变更收件箱（S12）

`#inbox`（idle/loading/ready/empty/error）位于 `#candidate` 之后；入口是左栏仓库行里的
`.repo-inbox-link`（Q1 D1：不新增第 8 个 tab，v2 的 7 tab 逐字不变）。
筛选 `<select id="inbox-filter">` 的五个值对齐 `REVIEW_STATES` + `all`；筛选走服务端
`candidate.list({repoId, reviewState})`，`inboxRequestToken` 丢弃竞态回包。
相对时间是纯函数 `relativeTime(epochSeconds, nowMs)`（刚刚/分钟/小时/天）。
点行把 `taskId` 交给 S11 的 `loadCandidate()`，**不画第二个详情或 diff 渲染件**。


### 审核按钮（S13）

`#candidate-actions` 含 `#candidate-approve` / `#candidate-reject` / `#candidate-reason`；
终态由 CSS 隐藏，服务端状态机才是真正守门人。按钮按 `botPermissions[selectedBotId]`
给禁用提示（D4：UX hint，不是安全边界；S16 才做服务端强制）。**没有「合并」按钮**。
`#candidate[data-state]` 现在直接取 `reviewState`（local_only/…/rejected），
不再是 S11/S12 的 `done`。订阅 `candidate.updated` 并刷新详情与收件箱。

### S16 · 权限审计面板

| 元素/函数 | 行 | 做什么 |
|---|---|---|
| `<section id="audit">` | 652 | 审计面板（只读） |
| `badge.dataset.unavailable` | 1399 / 1890-1891 | ① `renderBots` 里不可授予的级别标「（不可用）」；② `markPermissionUnavailable` 里 FALLBACK 路径同样标 |
| `markPermissionUnavailable(botId)` | 1885 | **D6**：审计里的 `FALLBACK` 把该 Bot 徽标标「（不可用）」（判定在 1890，调用在 1893） |
| `loadAudit(botId)` | 1897 | 发 `audit.list`，逐行 `textContent` 渲染（不用 `innerHTML`）；见 FALLBACK → 标不可用 |

### S17 · 上下文来源清单

| 元素/函数 | 行 | 做什么 |
|---|---|---|
| `<section id="context">` | 658 | 注入清单面板（只读，来源 + 层级 + 字节数） |
| `resetContext(state, text)` | 1849 | 断连 / 换 Bot 时清空上下文清单 |
| `loadContext(botId)` | 1855 | 发 `memory.context.preview`，`textContent` 渲染；`truncated` 单列标红，`conflict` 警告 |
