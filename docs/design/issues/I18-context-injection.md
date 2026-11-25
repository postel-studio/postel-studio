# I18 · 上下文注入与可见性（AGENTS.md 分层）

- **里程碑**：M3　**预计**：3h　**依赖**：I08　**优先级**：P2

## 背景

规范层（②）**不复制**，任务启动时从仓库实时读取（架构 §5）。注入必须可见，否则无法审计。

## 验收标准

1. 任务启动时读取仓库根 `AGENTS.md` / `README.md` 与当前目录路径上的各层 `AGENTS.md`。
2. 注入结果包含来源文件路径与命中层级，可通过接口查询并在 UI 展示。
3. 仓库层与 Bot charter 冲突时**阻断**任务（错误码 `CONTEXT_CONFLICT`），不静默取一个。
4. Bot 级只能**加严**：放宽权限的 charter 条目被拒绝并报错。
5. 注入内容不写入记忆目录（避免两份真相）。

## 涉及文件

- `src/postel_studio/memory/inject.py`（新建）
- `src/postel_studio/orchestrator.py`（注入接入）
- `src/postel_studio/web/assets.py`（注入可见性展示）
- `tests/test_injection.py`（新建）

## 测试要求

- 三层 AGENTS.md 构造（根/子目录/Bot charter），断言全部被检索到且层级正确。
- 冲突用例：断言阻断且错误码稳定。
- 断言记忆目录中**没有** AGENTS.md 副本。

## 禁止事项

- 不得把 AGENTS.md 复制进记忆目录。
- 不得在冲突时静默选一方。
- 不得允许 Bot 级放宽权限。
