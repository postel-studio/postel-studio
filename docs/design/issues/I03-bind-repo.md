# I03 · 绑定本地仓库并列出分支（全链路）

- **里程碑**：M1　**预计**：3h　**依赖**：I02　**优先级**：P0

## 背景

这是 M1 的「垂直切片」主干：一次打通 数据 → 接口 → 前端。用户路径是「填入路径 → 点绑定 → 看到分支」。

## 验收标准

1. `repo.bind` 传入本地 git 仓库绝对路径，返回 `repoId`（origin URL 归一化哈希，无 origin 时用绝对路径哈希）。
2. 非 git 目录返回 `error.code == "NOT_A_REPO"`，**不是** 500。
3. 同一路径重复绑定幂等，`repoId` 不变，列表中不重复。
4. `repo.branches` 返回分支名、sha、是否当前、upstream；不存在的 `repoId` 返回 `REPO_NOT_BOUND`。
5. 解绑只删绑定，**不动**用户仓库任何文件。
6. 前端页面能：填路径 → 绑定 → 显示仓库行 → 点击显示分支列表。

## 涉及文件

- `src/postel_studio/domain/models.py`（新建：Repo 与 repoId）
- `src/postel_studio/gitops/repo.py`（新建：`is_repo` / `origin_url` / `default_branch` / `list_branches`）
- `src/postel_studio/storage/db.py`（新建：repos 表）
- `src/postel_studio/api/app.py`（新建：方法路由）
- `src/postel_studio/web/assets.py`（新建：最小页面）
- `tests/conftest.py`、`tests/test_git.py`、`tests/test_repo_api.py`（新建）

## 测试要求

- git 层测试必须用**真实临时 git 仓库**（`git init` + 真实提交），不得用 mock。
- 覆盖：无 origin / 有 origin / 分离 HEAD / 空仓库（零提交）。
- 断言 `NOT_A_REPO` 与 `REPO_NOT_BOUND` 的 `error.code` 稳定。
- 一条集成测试：从 WebSocket 帧发起绑定，拿到分支结果。

## 禁止事项

- 不得在 git 调用中允许交互式提示（必须 `GIT_TERMINAL_PROMPT=0`）。
- 不得解析人类可读格式（用 `--porcelain` / `for-each-ref` 的字段分隔输出）。
- 不得在返回里暴露仓库之外的绝对路径（第一版仅返回绑定仓路径）。
