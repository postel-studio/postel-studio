# I07 · worktree 池：分配与回收

- **里程碑**：M2　**预计**：3h　**依赖**：I03　**优先级**：P0

## 背景

Bot 的隔离靠 worktree（架构 §4）：每 Bot 一个常驻 worktree，内部串行队列。本切片只做分配与回收。

## 验收标准

1. `worktree.acquire(repoId, botName, baseRef)` 在 `~/.postel/worktrees/<repoId>/<botName>/` 创建 worktree，基于 `baseRef` 的新分支。
2. 同一 Bot 重复 acquire 返回同一个 worktree（复用），不重复创建。
3. `worktree.release` 移除 worktree 目录与 `git worktree prune`，仓库本身不受影响。
4. 同一 Bot 的并发 acquire 必须串行（后到者等待，不并发写同一目录）。
5. 分配失败（如基础 ref 不存在）返回稳定错误码 `BAD_BASE_REF`。

## 涉及文件

- `src/postel_studio/gitops/worktrees.py`（新建）
- `src/postel_studio/api/app.py`（`worktree.acquire/release`）
- `tests/test_worktree.py`（新建）

## 测试要求

- 真实 git 仓库 + 真实 `git worktree add`，不得 mock。
- 复用测试：两次 acquire 返回同一路径且只创建一次。
- 并发测试：两个协程同时 acquire，断言只有一个创建、另一个等待后复用。
- 回收测试：release 后原仓库 `git worktree list` 中不再出现。

## 禁止事项

- 不得在仓库工作目录内创建 worktree（必须在 `~/.postel/worktrees/`）。
- 不得在 release 时执行任何会丢用户改动的 `reset --hard` 到主仓库。
- 不得让并发 acquire 产生两个 worktree（必须串行）。
