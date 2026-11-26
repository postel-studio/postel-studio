# S3 · 填路径 → 左栏出现这个仓库

- **里程碑** M1 · **预计** 3h · **依赖** S2 · **优先级** P0
- **用户路径**：在输入框粘贴仓库路径 → 点「绑定」→ 左栏出现仓库行（名字 + 默认分支）→ 刷新页面仍在

## 背景

第一刀真正碰业务：把"本地有个仓库"变成"工作台认得它"。这一步打通 数据 → 接口 → UI 三层的**最小**形态。

## 验收标准

1. 输入一个真实 git 仓库路径，绑定后左栏出现该仓库（显示名称与默认分支）。
2. 输入非 git 目录 → 显示可读错误「不是 git 仓库」，不崩溃、不留半状态。
3. 刷新页面后仓库仍在（已持久化）。
4. 同一路径重复绑定 → 不产生重复行。
5. 绑定信息落到 `~/.postel/state.db`（或等价的落盘），不写进仓库。

## 涉及文件

- `src/postel_studio/domain/models.py`（新建，仅 `Repo` + `repo_id` 归一化）
- `src/postel_studio/gitops/repo.py`（新建，仅 `is_repo` / `origin_url` / `default_branch`）
- `src/postel_studio/storage/db.py`（新建，仅 `repos` 表）
- `src/postel_studio/api/app.py`（加 `repo.bind` / `repo.list`）
- `src/postel_studio/web/assets.py`（左栏仓库区 + 输入框）
- `tests/test_bind.py`（新建）

## 测试要求

- git 层用**真实临时仓库**（`git init` + 一次提交），不得 mock。
- 覆盖：有 origin / 无 origin / 非 git 目录。
- 断言 `NOT_A_REPO` 错误码稳定。
- 幂等：连续两次绑定同一路径，列表中只有一条。

## 禁止事项

- 不得只写接口不管 UI（那样演示不了）。
- 不得在 git 调用中允许交互式提示（必须 `GIT_TERMINAL_PROMPT=0`）。
- 不得解绑时删除用户仓库里的任何文件。
- 本刀**不做**分支列表 —— 那是 S4，不要顺手写。
