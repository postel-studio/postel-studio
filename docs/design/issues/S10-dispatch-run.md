# S10 · 派活 → 状态变绿，磁盘上真有改动

- **里程碑** M2 · **预计** 4h · **依赖** S7,S8,S9 · **优先级** P0
- **用户路径**：选中 Bot → 输入一句话 → 点「派活」→ 状态灰→绿 → 去 worktree 里能看见新文件

## 背景

这是整个产品的核心闭环第一段。前四刀都是准备，这一刀才真正让 Bot 干活。

## 验收标准

1. 输入简短指令（如「创建一个 hello.txt」），任务从 `queued` → `running` → `succeeded`。
2. Agent 的 `cwd` 是被分配的那个 worktree（断言子进程工作目录）。
3. 任务结束后 worktree 中 `git status` 非空。
4. 失败时显示稳定错误码，不静默 `failed`。
5. 同一 Bot 已有任务在跑时返回 `BUSY`，不并发。
6. 页面实时显示状态变化（不需刷新）。

## 涉及文件

- `src/postel_studio/domain/models.py`（加 `Task` 与状态机）
- `src/postel_studio/agent/acp_client.py`（加 `session/new` / `prompt` / `cancel`）
- `src/postel_studio/orchestrator.py`（新建，派活流程，≤200 行）
- `src/postel_studio/api/app.py`（加 `task.dispatch` / `task.status`）
- `src/postel_studio/web/assets.py`（派活输入 + 状态灯）
- `tests/test_dispatch.py`（新建）

## 测试要求

- **真实集成**：调用 `opencode acp` 执行写文件指令，断言文件出现（未装则 skip）。
- 队列：并发派两个任务，断言第二个拿到 `BUSY`（或串行执行，二者选一并测）。
- 状态机：非法迁移（`queued → succeeded`）必须被拒绝。

## 禁止事项

- 不得让 Agent 在主仓库执行（必须 worktree）。
- 不得把 stderr 当成功结果。
- 不得跳过权限判定（本刀即便全允许，也必须走 `permission.check` 路径）。
- 本刀不产出候选变更（那是 S11）。
