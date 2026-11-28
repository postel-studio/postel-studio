# 03 · memory — `src/postel_studio/memory/inject.py`

> 层 L0 · 263 行 · 上游：[模块索引](README.md) · 规格：[S17-context-visible](../../design/issues/S17-context-visible.md)

## 职责

S17 · 上下文注入的**只读**装配层：实时读取仓库里的 `AGENTS.md`（根 + 各级子目录）与
`README.md`，加上 Bot 的 `charter.md`，产出来源清单（路径 + 层级 + 字节数 + 截断标记）
与带边界标记的文本。**本模块没有任何写调用**——AGENTS.md 绝不复制进记忆目录。

它被两层调用：`api/bots.py` 的 `context_preview`（只读预览）与 `orchestrator._run`
（派活前装配 + 冲突终态化）。它自己不 import api / web / gitops / agent（见
[02-layers](../02-layers.md) 的可执行边表）。

## 符号表

| 符号 | 行 | 做什么 |
|---|---|---|
| `INJECT_BUDGET_BYTES` | 27 | 64 KiB 注入预算（UTF-8 字节，与 `PROMPT_MAX_BYTES` 同口径） |
| `CONTEXT_CONFLICT` | 29 | 07 §1.3 已登记的错误码，S17 首次使用（不新增码） |
| `_PERMISSION_LINE` | 32 | 结构化权限上限声明的正则；**先试 `L2+` 再试 `L2`**（spec 探针测过顺序坑） |
| `ContextSource` | 39 | 一条来源：`path` / `level` / `bytes` / `text` / `truncated` / `injected_bytes` / `skipped` |
| `InjectionPlan` | 65 | 一次注入的完整结果：`sources` / `total_bytes` / `omitted_bytes` / `conflict` / `effective_permission` / `prompt_text` |
| `ContextSource.as_row` | 50 | 预览载荷里的一条来源（07 §2.6 字段） |
| `InjectionPlan.as_row` | 75 | `memory.context.preview` 的返回体 |
| `declared_permission(text)` | 85 | 纯函数：取最严格的结构化权限声明；无 / L3/L4 → `None` |
| `_read_source` | 100 | 单文件只读（UTF-8 解码失败记 `skipped`） |
| `_safe_source` | 113 | **05 §7**：符号链接 / 解析后越出 repo 根的来源不读内容，只记 `skipped`，绝不进 prompt |
| `_repo_sources` / `_charter_source` | 131/150 | 仓库层级排序（实时读）/ charter（永远最后） |
| `_begin` / `_end` | 166/170 | 来源边界标记（含 `DATA, NOT INSTRUCTIONS`） |
| `build_plan(repo_root, repo_id, bot_name, budget=)` | 174 | 只读装配：来源 + 冲突判定 + 整条跳过 + prompt 文本 |
| `preview(...)` | 261 | `memory.context.preview` 的薄包装（冲突只标 `conflict`，不抛） |

## 关键决策与理由

### 为什么「不复制」是一条可执行约束

02 §5 / 01 §5 要求规范层随仓库变化、实时读取。`tests/test_injection.py::test_agents_md_read_live_and_no_copy_in_memory_dir`
改一次根 AGENTS.md 后断言 preview 立刻变，并 `rglob("AGENTS.md")` 断言记忆目录为零副本。

### 为什么来源必须先 `resolve()` 再判 containment

05 §7 要求路径解析后仍在允许的根内。`_safe_source` 对每个仓库来源先排除符号链接，
再 `resolve(strict=True)` 并要求 `is_relative_to(repo_root.resolve())`；越界来源只记
`skipped`（预览可见），正文绝不读、绝不进 prompt。审查实测的根 `AGENTS.md` → 仓库外
secret 泄漏由 `tests/test_injection.py::test_symlinks_outside_repo_never_enter_prompt` 钉住，
组合反向注入 S17 #5 同时拆掉两道守卫证明测试会红。

### 冲突为什么用结构化声明而不是 LLM 判断

S17 D3：`permission: L0|L1|L2|L2+`（或中文 `权限：`）声明该层允许的上限；仓库层取最严，
Bot 级只能加严（`bot.rank <= repo.rank`），放宽即 `CONTEXT_CONFLICT`。多处声明取最严，
防后面一行覆盖前面的放宽。L3/L4 解析为 `None`（不在可授予阶梯内）。

### 截断为什么整条跳过

S17 D5：超限跳过整条来源（不切半个文件），在清单里标 `truncated=true` /
`injectedBytes=0`，顶层 `omittedBytes` 累计被跳过的字节。`totalBytes` 始终是实际注入量。

## 修改须知

| 如果你要… | 还波及 |
|---|---|
| 改层级标签 `level` | S17 spec D2 + `assets.py` 的 `loadContext` 展示 + `tests/test_injection.py` 的字节/层级断言 |
| 改权限声明正则 | S17 spec D3 的 `L2+` 顺序坑 + `test_permission_l2_plus_is_not_truncated` |
| 改预算 | S17 spec D5 + `test_budget_truncation_is_marked` |
| 改预览载荷字段 | 07 §2.6 + S17 spec D7 + 前端 `loadContext` |
| 给本模块加 import | `scripts/gate-layers.py` 的 L0 禁止边 + `02-layers.md` |

## 测试

| 测试 | 守什么 |
|---|---|
| `tests/test_injection.py::test_preview_lists_three_layers_with_bytes` | 三层 + 字节数 + 排序 |
| `...::test_agents_md_read_live_and_no_copy_in_memory_dir` | 实时读取 + 零副本 |
| `...::test_symlinks_outside_repo_never_enter_prompt` | 越根符号链接 / `..` 不泄漏（组合反向注入 S17 #5） |
| `...::test_repo_bot_permission_conflict_blocks_dispatch` / `...::test_bot_only_tightens` | 冲突 / 加严 |
| `...::test_budget_truncation_is_marked` | 64 KiB 整条跳过 + 标记 |
| `...::test_dispatch_with_context_conflict_fails_without_session` | 端到端：冲突 → `failed` + `CONTEXT_CONFLICT`，会话不创建 |
