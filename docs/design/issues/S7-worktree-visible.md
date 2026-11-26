# S7 · 点「分配工作区」→ 磁盘上真有 worktree

- **里程碑** M2 · **预计** 3h · **依赖** S3 · **优先级** P0
- **用户路径**：选中仓库 → 点「分配工作区」→ 页面显示路径 → 在文件管理器里能看到该目录

## 背景

Bot 的隔离靠 worktree。这一刀只做**看得见**的最小版本：能分、能看、能收，不接 Agent。

## 验收标准

1. 点击后创建 `~/.postel/worktrees/<repo-id>/<bot>/`，基于选定分支。
2. 页面显示该路径与分支名。
3. 重复点击不重复创建（复用已有 worktree）。
4. 点「回收」后目录消失，原仓库 `git worktree list` 中不再出现。
5. 基础分支不存在时报 `BAD_BASE_REF`，不留半个目录。

## 涉及文件

- `src/postel_studio/gitops/worktrees.py`（新建，≤150 行）
- `src/postel_studio/api/app.py`（加 `worktree.acquire` / `release`）
- `src/postel_studio/web/assets.py`（工作区卡片）
- `tests/test_worktree.py`（新建）

## 测试要求

- 真实 `git worktree add`，不得 mock。
- 复用：两次 acquire 返回同一路径且只创建一次。
- 回收：断言 `worktree list` 已无该项。
- 失败：不存在的基础分支 → `BAD_BASE_REF` 且无残留目录。

## 禁止事项

- 不得在仓库工作目录内建 worktree。
- 不得在回收时对主仓库执行 `reset --hard`。
- 本刀不接 Agent，不得顺手写派活逻辑。
