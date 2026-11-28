# 03 · errors — `src/postel_studio/api/errors.py`

> 层 L1 · 90 行 · 上游：[模块索引](README.md) · 契约：[07-api-contract §1.3](../../design/07-api-contract.md) · 相关：[frames](frames.md) · [assets](assets.md)

## 职责

两件事，都只做数据变换：

1. **错误码 → 用户可读文案**的唯一来源（`ERROR_TEXT`）；
2. `message` 进帧前的**脱敏**（`message_safe`：绝对路径、堆栈、凭据）。

页面不再持有第二份映射：`render_index(error_text=...)` 把它序列化进 `%%ERROR_TEXT%%`。
这条边是**注入**而不是 `web → api` 的 import——分层门禁禁止 L4 反向依赖（[02-layers §2](../02-layers.md#2-当前真实的-import-边)）。

## 符号表

| 符号 | 行 | 做什么 | 谁在用 |
|---|---|---|---|
| `ERROR_TEXT` | 21 | 19 个码（07 §1.3 的 18 个协议码 + S18 补登记的 `FRAME_TOO_LARGE`）→ 一句人话 | `api/app.py`（注入页面）、`test_states.py` |
| `UNKNOWN_CODE_TEXT` | 44 | 表外码的兜底；**不是**绑定表单的「绑定失败」 | `app.py` 注入 → 页面 `errorText()` |
| `DOCUMENTED_CODES` | 47 | 码集合（测试对账用，避免再手写一份清单） | `test_states.py` |
| `message_safe(text)` | 66 | 删堆栈尾巴 → 擦 `~/` / 盘符 / POSIX 绝对路径 → 擦凭据取值；幂等 | `frames.encode_error` |
| `user_text(code)` | 88 | 码 → 文案（表外回兜底）；服务端若要在日志里写人话可用 | 目前只有测试 |

## 关键决策与理由

### 表为什么在 `api/` 而不是 `web/`

S3–S17 期间映射就住在 `web/assets.py`。问题不是位置，是**它开始分裂**：S4 给分支面板补
`REPO_NOT_BOUND`、S5 补 `BAD_REF` / `NOT_SUPPORTED`、S7 补 `WORKTREE_CONFLICT`，每次都只改
「页面那一份」，服务端没有任何地方能枚举出"到底登记了哪些码"。S18 把它挪到 L1：

- 服务端与页面**读同一份**（页面靠 `render_index` 注入，见 D1）；
- 测试可以拿 07 §1.3 的表与源码里的码字面量**双向对账**（`test_states.py`）；
- 新增码必须先改 07，否则 `test_no_undocumented_code_has_text` 直接红。

### `FRAME_TOO_LARGE` 是补登记，不是新码

`frames.py` 从 S2 起就在用这个码，但 07 §1.3 一直漏着。S18 按 09 §2.2 的要求在同一个 PR
里补进表（PR 体单独标出），**没有**新增任何码。

### 脱敏为什么放在编码层

`message` 按契约只给日志，但帧会进 devtools、用户贴的截图、issue 附件。把 `message_safe`
挂在 `encode_error` 上，任何 handler 都不可能"忘了脱敏"——这比在每个 `_fail()` 里自觉更可靠。
宁可多擦（比如把普通斜杠句擦成 `<路径>`），也不让一条 `/home/<user>/...` 出去。

## 修改须知

- 加码 = 先改 `docs/design/07-api-contract.md` §1.3，再改 `ERROR_TEXT`，两条测试会对账。
- 改文案不影响契约（07 §1.3：码稳定、文案可改），但 `test_states.py` 的空态断言里也引用了
  几句面板文案，改之前先看它。
- **不要**在页面里重新写一份映射；`test_page_gets_error_text_from_the_single_source` 会拦住
  `assets.ERROR_TEXT` 复活。

## 测试

| 守什么 | 在哪 |
|---|---|
| 07 表 ↔ `ERROR_TEXT` 双向一致 | `tests/test_states.py::test_every_documented_code_has_text` / `::test_no_undocumented_code_has_text` |
| 源码里当码用的常量都在表里 | `::test_no_undocumented_code_is_used` |
| 文案无路径/堆栈/凭据 | `::test_texts_carry_no_path_stack_or_credential` |
| 脱敏规则与幂等 | `::test_message_safe_*` / `::test_encode_error_frames_carry_the_sanitized_message` |
| 反向注入（删码 / 关脱敏） | `tests/test_reverse_checks.py` 的 S18 #1/#2 |
