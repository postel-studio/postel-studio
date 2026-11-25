# I15 · M2 验收脚本：派活到批准的完整路径

- **里程碑**：M2　**预计**：3h　**依赖**：I13, I14　**优先级**：P1

## 背景

M2 的核心承诺是「一个 Bot 用真实 Agent 在真实仓库上产出候选分支，人能看 diff 并批准」。需要一条命令证明它。

## 验收标准

1. `uv run python scripts/accept_m2.py <repo-path>` 完成：绑定 → 建 Bot → 派活 → 真实 Agent 执行 → 生成候选 → 查看 diff → 批准。
2. 失败时非零退出并指明失败步。
3. 使用临时 `POSTEL_HOME` 与临时 clone，不污染真实仓库。
4. 结束后清理：worktree 回收、临时目录删除、无残留进程。
5. opencode 未安装时明确 skip 并给出提示，不伪装成功。

## 涉及文件

- `scripts/accept_m2.py`（新建）
- `tests/test_acceptance_m2.py`（新建）

## 测试要求

- 真实集成：断言候选生成且 `diff_stat` 非零。
- 断言批准后 `review_state == approved`。
- 断言清理生效（无残留 worktree 与进程）。

## 禁止事项

- 不得在用户真实仓库上跑（必须先临时 clone）。
- 不得在未装 opencode 时输出「通过」。
- 不得跳过 diff 查看步骤。
