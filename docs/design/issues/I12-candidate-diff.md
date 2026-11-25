# I12 · 候选变更：从 worktree 生成 diff

- **里程碑**：M2　**预计**：3h　**依赖**：I11　**优先级**：P0

## 背景

产出的单位是**候选分支**（D10）。本切片把 worktree 里的改动固化为候选变更记录，供收件箱展示。

## 验收标准

1. 任务成功后生成 `Candidate`：`base_sha`、`head_sha`、`branch`、`diff_stat`。
2. 候选分支名可预测（如 `bot/<botName>/<taskId 短>`），不以用户输入为分支名。
3. 若 Agent 未产生任何改动，**不产生候选**，任务标记 `succeeded` 但 `candidate=none`（区分「无事可做」与「失败」）。
4. `candidate.list` 按时间倒序列出。
5. 拒绝候选后从收件箱消失（状态 `rejected`），不删分支。

## 涉及文件

- `src/postel_studio/domain/candidate.py`（新建）
- `src/postel_studio/storage/db.py`（candidates 表）
- `src/postel_studio/orchestrator.py`（产出候选）
- `tests/test_candidate.py`（新建）

## 测试要求

- 真实仓库：完成任务 → 断言 `diff_stat` 与 `git diff --shortstat` 一致。
- 空改动：断言不产生候选且任务状态仍是 `succeeded`。
- 状态机：`local_only` → `rejected` 后不再出现在默认列表。

## 禁止事项

- 不得把「无事可做」记成失败。
- 不得用用户输入直接作分支名（需清洗/替换非法字符）。
- 不得在拒绝时删除候选分支（保留可追查）。
