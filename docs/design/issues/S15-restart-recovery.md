# S15 · 杀掉内核再启动 → 中断的任务不撒谎

- **里程碑** M3 · **预计** 3h · **依赖** S11 · **优先级** P2
- **用户路径**：任务跑到一半杀进程 → 重启 → 收件箱里那条显示「已中断」并可选择重跑

## 背景

常驻进程会重启（升级、崩溃）。最危险的不是丢任务，而是把"半截任务"显示成"成功"。

## 验收标准

1. 重启后发现 `running` 状态的任务，标记为 `interrupted` 并在 UI 明示。
2. **不得**把 `interrupted` 改成 `succeeded`。
3. 已完成任务与候选不受影响。
4. 中断任务可一键重跑（新建任务，不复用旧 id）。
5. 数据库 schema 有版本号；从空库与旧版本库各迁移一次成功。

## 涉及文件

- `src/postel_studio/storage/db.py`（schema 版本 + 迁移）
- `src/postel_studio/orchestrator.py`（启动时恢复逻辑）
- `src/postel_studio/web/assets.py`（中断标记与重跑按钮）
- `tests/test_recovery.py`（新建）

## 测试要求

- 写入 `running` → 模拟重启 → 断言变 `interrupted`。
- 断言已完成任务不变。
- 迁移：空库 → 当前版本；旧版本 → 当前版本，各断言成功且可重入。

## 禁止事项

- 不得把 `running` 自动改成 `succeeded`。
- 不得自动重跑（交给人决定）。
- 不得丢失候选与任务的外键关系。
