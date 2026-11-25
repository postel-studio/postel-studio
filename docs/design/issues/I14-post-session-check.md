# I14 · Agent 会话后的一致性检查

- **里程碑**：M2　**预计**：3h　**依赖**：I11　**优先级**：P1

## 背景

ACP 没有「我要提交了」语义（需求 §6.5）。Agent 可能私自切分支、自己 commit、或写出 worktree。必须在会话结束后检查。

## 验收标准

1. 会话结束后检查三项并报告：是否切换了分支、是否出现多余 commit、是否越界写。
2. 任一项异常时任务标为 `failed`，错误码 `SCOPE_VIOLATION`，并在结果中列出具体异常。
3. 越界写检测覆盖：worktree 之外被修改的文件（通过 mtime 或内容哈希抽样）。
4. 最终 commit 由内核在受控分支上做**唯一一次**，不由 Agent 做。
5. 检查本身不修改 worktree（只读）。

## 涉及文件

- `src/postel_studio/gitops/audit.py`（新建：会话后检查）
- `src/postel_studio/orchestrator.py`（接入检查）
- `tests/test_audit.py`（新建）

## 测试要求

- 构造三种异常：切分支、多余 commit、越界写（写 worktree 外文件），逐一断言被检出。
- 正常路径：无异常时任务状态不变。
- 断言检查前后 worktree 的 `git status` 不变（只读）。

## 禁止事项

- 不得在检查阶段自动修复（发现即报告，交给人决定）。
- 不得允许 Agent 完成最终 commit。
- 不得只看 `git status` 就判「无异常」（必须核分支与提交数）。
