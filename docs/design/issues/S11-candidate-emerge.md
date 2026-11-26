# S11 · 任务完成 → 收件箱多出一条候选

- **里程碑** M2 · **预计** 3h · **依赖** S10 · **优先级** P0
- **用户路径**：任务成功后 → 点「收件箱」→ 看到一条候选（标题/分支/+X −Y）→ 点开看到 diff

## 背景

产出的单位是**候选分支**，不是聊天记录。这一刀把 worktree 里的改动固化成可审查的一条。

## 验收标准

1. 任务成功且有改动时生成候选：分支名、`+X −Y`、状态「仅本地」。
2. 分支名可预测且不含用户输入（如 `bot/<bot>/<task短id>`）。
3. **无改动时不生成候选**，任务仍标记 `succeeded`（区分"无事可做"与"失败"）。
4. 点候选能看到 diff（复用 S5 渲染件）。
5. 候选记录落盘，刷新后仍在。

## 涉及文件

- `src/postel_studio/domain/models.py`（加 `Candidate`）
- `src/postel_studio/storage/db.py`（加 `candidates` 表）
- `src/postel_studio/orchestrator.py`（产出候选）
- `src/postel_studio/api/app.py`（加 `candidate.list`）
- `src/postel_studio/web/assets.py`（候选详情视图）
- `tests/test_candidate.py`（新建）

## 测试要求

- 真实仓库：断言 `diff_stat` 与 `git diff --shortstat` 一致。
- 空改动：断言不产生候选且任务仍 `succeeded`。
- 分支名清洗：含非法字符的 Bot 名断言被替换。

## 禁止事项

- 不得把"无事可做"记成失败。
- 不得用用户输入直接作分支名。
- 本刀只做单条候选详情，**不做**列表页（那是 S12）。
