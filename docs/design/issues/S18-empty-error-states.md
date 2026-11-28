# S18 · 每个空态与每个错误都有可读文案

- **里程碑** M3 · **预计** 3h · **依赖** S12 · **优先级** P2
- **用户路径**：故意制造各种空与错 → 都能看到看得懂的提示，而不是白屏或英文堆栈

## 背景

M2 完成后各面板都有了，但空态与错误态往往被忽略。这一刀专门扫一遍——它们是"能用"与"像成品"的差别。

## 验收标准

1. 每个面板都有明确空态（无仓库/无分支/无 Bot/无候选/无审计记录）。
2. 每个已登记错误码都有用户可读文案，且**不泄露**绝对路径与堆栈。
3. 内核不可达时显示「内核不可达」并提供重连，不白屏。
4. 权限不足的操作给出原因，不是静默禁用。
5. 所有状态在浅色与暗色下均可读。

## 涉及文件

- `src/postel_studio/api/errors.py`（新建，错误码 → 文案映射）
- `src/postel_studio/web/assets.py`（各面板空态）
- `tests/test_states.py`（新建）

## 测试要求

- 枚举全量错误码，断言每个在映射表中都有条目。
- 断言文案不含路径参数与 `Traceback` / `File "` 等堆栈特征。
- 空态 DOM 断言。

## 禁止事项

- 不得用空白代替空态。
- 不得把堆栈或绝对路径显示给用户。
- 不得在错误里泄露凭据片段。
- 不得新增未登记的错误码。

---

## 裁决 D1…D9（写完即契约，实现者不得自改）

### D1 文案单一来源：`api/errors.py`，页面靠注入

`src/postel_studio/api/errors.py` 的 `ERROR_TEXT` 是**唯一**的错误码 → 文案表。
`web/assets.py` 不再保留副本：`render_index(..., error_text=...)` 收表并序列化进既有的
`%%ERROR_TEXT%%` 占位符，`api/app.py` 把 `errors.ERROR_TEXT` 传进去。

为什么不是 `web/assets.py` 直接 import：分层门禁里 `web/assets.py` 的禁止项就是
`postel_studio.api`（`02-layers §2`，L4 不许反向 import）。页面本来就有占位符注入通道，
加一个参数比开一条反向边便宜，也不破坏「页面里只有静态文案」的既有性质。

### D2 错误码全集 = 07 §1.3 的 18 个协议码 + `FRAME_TOO_LARGE`

`FRAME_TOO_LARGE` 是 `api/frames.py` 早就在用、但 07 §1.3 漏登记的码。本刀把它**补进
07 §1.3 表**（同 PR，按 09 §2.2 的要求单独标出），并进 `ERROR_TEXT`；不新增其它码。

进程启动错误码（`KERNEL_ALREADY_RUNNING` 等 4 个）**不**进 `ERROR_TEXT`：它们只出现在
CLI stderr，从不经过 WebSocket 帧，页面没有任何机会显示它们。

### D3 表外码的兜底

```
UNKNOWN_CODE_TEXT = "操作失败：内核返回了未登记的错误码，请把启动它的终端输出报给维护者"
```

`errorText(code)` 改为 `ERROR_TEXT[code] || UNKNOWN_CODE_TEXT`。绑定表单的
`ERROR_TEXT_FALLBACK`（「绑定失败」）**保持原值**——S3/S4/S5 的页面断言引用它，且它与
"未登记错误码"是两件事。各面板自己的兜底（`BRANCH_ERROR_FALLBACK` 等）不动。

### D4 帧里的 `message` 统一过一遍脱敏

页面只按码映射（S3 起的纪律），但帧会进 devtools、日志、用户贴的截图。`frames.encode_error`
在编码前调 `errors.message_safe(text)`：

- 绝对路径（`/a/b`、`C:\a\b`、`~/a`）→ `<路径>`；
- `Traceback (most recent call last)` 整段与 `File "..."` 行删除；
- `ghp_…` / `Bearer …` / `token=…` / `password=…` 的取值 → `<凭据>`。

这是**服务端最后一道**，前端映射仍是第一道；两道都不许省。

### D5 空态 DOM 契约（新增三个，其余沿用）

| 面板 | 节点 | 文案 | 出现条件 |
|---|---|---|---|
| 候选 | `#candidate-empty` | 还没有候选变更：派活成功后，Bot 的改动会出现在这里。 | `#candidate[data-state=empty]` |
| 审计 | `#audit-empty` | 这个 Bot 还没有权限裁决记录。 | `#audit[data-state=empty]` |
| 上下文 | `#context-empty` | 本次没有需要注入的上下文。 | `#context[data-state=empty]` |

既有的 `#repo-empty` / `#branch-empty` / `#bot-empty` / `#inbox-empty` / `#workspace-empty`
沿用原判据。`empty` 是本刀给三个新面板用的**显式状态**（沿用 S12 收件箱的先例），
与 `ready`（有数据）/ `idle`（没选）互斥。纪律同 S3 D1：**idle（还没选仓库/Bot）
不显示空态**，显示"选择一个…"，否则「没选」会被说成「没有」。

### D6 内核不可达 = 断线的一种，但有专属文案

`#status` 的 `data-state` 仍是 `connected / disconnected / reconnecting`（S2 契约不扩）；
新增 `#status-hint` 段落承载原因，两种断法文案不同：

- 连接尝试失败（握手超时 / 取不到页面 / 端口变了）→ `#status-text` = `已断开 · 内核不可达`，
  hint = `内核不可达：确认内核进程还在，然后点「重连」。`
- 连上之后掉线（close/error/心跳超时/SIGKILL）→ `#status-text` = `已断开`，
  hint = `连接断了：内核可能已退出，点「重连」再试。`

两种都显示「重连」按钮。`#status-text` 仍以「已断开」开头，S2/S3 的既有断言（含
`STATUS_STATES` 字面量与 `/已断开/` 正则）不变。

### D7 权限不足给原因，不静默禁用

`#task-dispatch` 在选中 L0 Bot 时仍禁用，但同屏出现 `#task-permission-note`：
`这个 Bot 是 L0（只读），派活会被拒绝：先在 Charter 里把权限改到 L1 以上。`
文案与 `ERROR_TEXT.PERMISSION_DENIED` **同源**（JS 直接取表，不复制第二份），
按钮加 `aria-describedby="task-permission-note"`。权限改到 L1 以上后提示消失、按钮恢复。

### D8 浅色/暗色两套 token

颜色收进 `:root` 的 CSS 变量（`--bg / --panel / --text / --muted / --line / --field /
--line-strong / --link / --ok / --warn / --danger / --danger-text`），
`@media (prefers-color-scheme: light)` 覆盖成浅色。判据：正文对比度 ≥ 4.5:1、
次要文字 ≥ 4.5:1；边框不做文字判据。两套各测一遍（验收 5 的浏览器检查）。

### D9 顺手修 S16 遗留：`loadAudit` 的回调形状是错的

`request(method, params, done)` 的回调是 `(err, data)`，`loadAudit` 却写成 `function (frame)`
并把 `frame.ok / frame.data` 当帧读——`err === null` 时 `!frame` 为真，审计**永远**显示
「审计读取失败」，D5 的审计空态也就永远到不了。本刀改回 `(err, data)` 并加反向注入
（S17 的 `request(err, data)` 坑是同一个）。

## 验收 → 测试映射

| 验收 | 测试 | 浏览器 |
|---|---|---|
| 1 每个面板有空态 | `tests/test_states.py::test_every_panel_declares_an_empty_state` | `s18_states.mjs` CHECK 1–5 |
| 2 码表全覆盖 | `test_every_documented_code_has_text` / `test_no_undocumented_code_is_used` | — |
| 2 文案不泄露 | `test_texts_carry_no_path_stack_or_credential` / `test_message_safe_redacts_paths_and_stacks` | `s18_states.mjs` CHECK 6–7 |
| 3 内核不可达 | `test_page_declares_unreachable_hint` | `s18_states.mjs` CHECK 8 |
| 4 权限原因 | `test_page_shows_permission_reason_instead_of_silent_disable` | `s18_states.mjs` CHECK 9 |
| 5 浅/暗可读 | `test_page_declares_light_theme` | `s18_states.mjs` CHECK 10–11 |
| D9 审计回调 | `test_audit_callback_uses_err_data_shape` | `s18_states.mjs` CHECK 12 |

## 边界矩阵

| 维度 | 状态 | 处理 |
|---|---|---|
| 面板 | idle / loading / ready（有数据） / ready（空） / error | 空态只在 ready+空；idle 说"选择一个…" |
| 错误码 | 07 表内 / 表外 / 帧层非法（`FRAME_TOO_LARGE`） | 表内→文案；表外→`UNKNOWN_CODE_TEXT`；帧层→登记进表 |
| 错误内容 | 静态文案 / 含绝对路径 / 含堆栈 / 含凭据 | `message_safe` 脱敏后再进帧 |
| 连接 | 首次失败 / 连上后掉线 / 端口变了 / 重连成功 | 前三种都显示「重连」；文案区分不可达与掉线 |
| 权限 | L0 / L1 / L2 / L2+ | L0 显示原因并禁用；其余可派 |
| 主题 | 浅色 / 暗色 | 两套 token，对比度实测 |
| 列表长度 | 0 条 / 1 条 / 多条 | 0 条走空态；1+ 条不显示空态 |

## U16 的裁决（到期项，必须先裁）

`06-pitfalls U16`（`repo.diff` 全量读进内存再截断）原定"到期 S18"。**本刀明确豁免，到期改
S20（M3 验收）**：S18 是纯展示层的一刀，改 `gitops` 的读取方式要动子进程读取与背压，
与「空态/错误态」不是同一条用户路径（04 §6：一个 PR 不混两条路径），且 5s 超时已经给
内存尖峰封了时间上界。**不解决的后果**：一个几百 MB 的 diff 会让内核先吃下整份 patch
再切 100k，可能造成内存尖峰并拖慢同一进程里的网关请求；S20 必须裁决流式读或硬字节上限。
该豁免同时写进 `05-security` 的已知风险。

## 可执行证据（探针）

```bash
# 1) 码表条数 = 07 表内 18 个 + FRAME_TOO_LARGE
uv run python -c "from postel_studio.api import errors; print(len(errors.ERROR_TEXT))"
# 期望：19

# 2) 07 表 ↔ 码表 ↔ 源码「当码用」的常量，三向对账
uv run pytest -q tests/test_states.py -k "documented_code or undocumented_code_is_used"
# 期望：3 passed（宽口径 grep 会把 GIT_DIR / TOKEN 这类非错误码也算进来，故判定用测试）

# 3) 页面里不再有第二份映射（在 assets.py 里 grep 该行，期望 0）
grep -c '"NOT_A_REPO": "不是 git 仓库"' src/postel_studio/web/assets.py
# 期望：0

# 4) 空态节点数（repo/branch/bot/inbox/workspace/candidate/audit/context）
grep -c 'id="[a-z]*-empty"' src/postel_studio/web/assets.py
# 期望：8
```

## `src/` 预算估计

| 项 | 预计 |
|---|---|
| 文件数 | 4（新增 `errors.py` + `frames.py` / `assets.py` / `app.py`） |
| 行数 | +380 / −80（CSS 变量化多为替换，净增约 300） |

上限 12 文件 / 1000 行，均在限内；`tests/`、`tools/acceptance/`、`docs/` 不计（04 §1.1）。
