# S17 · 派活前能看到「注入了哪些上下文」

- **里程碑** M3 · **预计** 3h · **依赖** S8 · **优先级** P2
- **用户路径**：点 Bot → 看到「本次会注入：AGENTS.md (根) / README.md / charter」→ 派活时这些真的被注入

## 背景

上下文注入如果不可见，用户无法判断 Agent 为什么那么做。这是可审计的前提。

## 验收标准

1. 显示本次将注入的上下文来源清单（文件路径 + 层级 + 字节数）。
2. 仓库内 `AGENTS.md` 实时读取，**不复制**到记忆目录。
3. 仓库层与 Bot charter 冲突时**阻断**任务（`CONTEXT_CONFLICT`），不静默取一个。
4. Bot 级只能加严：放宽权限的条目被拒绝并报错。
5. 注入总长度有上限，超限截断并标记。

## 涉及文件

- `src/postel_studio/memory/inject.py`（新建）
- `src/postel_studio/orchestrator.py`（注入接入）
- `src/postel_studio/api/app.py`（加 `memory.context.preview`）
- `src/postel_studio/web/assets.py`（注入清单展示）
- `tests/test_injection.py`（新建）

## 测试要求

- 三层 AGENTS.md 构造（根/子目录/charter），断言全部检索到且层级正确。
- 冲突用例：断言阻断且错误码稳定。
- 断言记忆目录中**没有** AGENTS.md 副本。
- 超长截断用例。

## 禁止事项

- 不得把 AGENTS.md 复制进记忆目录。
- 不得在冲突时静默选一方。
- 不得允许 Bot 级放宽权限。
- 不得把注入内容当指令执行。
