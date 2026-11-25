# I11 · 用 OpenCode 跑通一次真实派活

- **里程碑**：M2　**预计**：3h　**依赖**：I07, I08, I10　**优先级**：P0

## 背景

到此为止所有组件都是分开验证的。本切片把它们串起来：给一个 Bot 派活，它在分配的 worktree 里用真实 OpenCode 完成一次写操作。

## 验收标准

1. `task.dispatch(botName, prompt)` 返回 `taskId`，状态从 `queued` → `running` → `succeeded/failed`。
2. OpenCode 的 `cwd` 确实是被分配的 worktree（断言子进程工作目录）。
3. 任务结束后 worktree 里能看到 Agent 产生的文件改动（`git status` 非空）。
4. 任务失败时 `task.status` 带稳定错误码，不是静默 `failed`。
5. 同一 Bot 同时派两个任务，第二个**排队**而不是并发执行。

## 涉及文件

- `src/postel_studio/domain/task.py`（新建：Task 与状态机）
- `src/postel_studio/orchestrator.py`（新建：派活流程）
- `src/postel_studio/api/app.py`（`task.dispatch/cancel/status`）
- `tests/test_real_run_opencode.py`（新建，可标记为慢集成测试）

## 测试要求

- **真实集成测试**：调用本机 `opencode acp`，在临时仓库的 worktree 里执行「创建一个 `hello.txt`」的提示，断言文件出现。
- 队列测试：并发派两个任务，断言串行（比较时间区间不重叠）。
- 需在 opencode 未安装时 skip 而非失败。

## 禁止事项

- 不得让 Agent 在主仓库目录执行（必须 worktree）。
- 不得在任务失败时把 stderr 直接当成功结果。
- 不得跳过排队：同一 Bot 的并发必须被串行化。
