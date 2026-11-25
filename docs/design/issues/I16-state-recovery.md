# I16 · 任务状态机持久化与重启恢复

- **里程碑**：M3　**预计**：3h　**依赖**：I12　**优先级**：P2

## 背景

内核是常驻进程，但会重启（升级、崩溃）。重启后不得丢任务、不得留下无主的 `running` 任务。

## 验收标准

1. 任务与候选状态全部落盘（SQLite），重启后可读。
2. 重启时发现 `running` 状态的任务，标记为 `interrupted` 并在 UI 提示（不静默当成功）。
3. 重启后 worktree 仍可被找回（按 `repoId`+`botName`+`taskId`）。
4. 已完成的任务与候选不受重启影响。
5. 迁移：schema 变更时有版本号与迁移脚本。

## 涉及文件

- `src/postel_studio/storage/db.py`（schema 版本与迁移）
- `src/postel_studio/orchestrator.py`（启动恢复逻辑）
- `tests/test_recovery.py`（新建）

## 测试要求

- 构造：写入 `running` 任务 → 模拟重启 → 断言变 `interrupted`。
- 断言已完成任务状态不变。
- 迁移测试：从空库与从旧版本库各迁移一次。

## 禁止事项

- 不得把 `running` 直接改成 `succeeded`（那是撒谎）。
- 不得在恢复时自动重跑任务（交给人决定）。
- 不得丢失候选与任务之间的外键关系。
