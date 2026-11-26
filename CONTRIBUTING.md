# 贡献指南

## 质量门禁（唯一标准）

本地与 CI 跑同一份脚本，**不允许分叉**：

```bash
bash scripts/check.sh          # 全部
bash scripts/check.sh lint     # 单项：lint | format | types | test | secrets
```

| 门禁 | 工具 | 失败即阻断 |
|---|---|---|
| lint | `ruff check` | 是 |
| format | `ruff format --check` | 是 |
| types | `mypy --strict` | 是 |
| test | `pytest --cov`，覆盖率下限 60% | 是 |
| secrets | 真实凭据形态扫描 + 与 `.env` 逐值比对 | 是 |

- `pre-commit` 钩子只跑快的三项（lint / format / secrets），完整门禁留给 CI。
- 配置全部在 `pyproject.toml`，CI 用 `uv sync --frozen` 保证依赖可复现。

## 覆盖率

当前下限 **60%**（M1 起步）。随 Issue 推进逐步上调；**任何新模块必须自带测试**，不允许先合并空实现。

## 测试约定

- 真实行为优先：git / ACP / worktree 必须用真实临时仓库或真实子进程，**不得只用 mock**。
- 需要真实 Agent（opencode / dsh / pi）的用例用 `@pytest.mark.real_agent`，缺失时 skip 而非失败。
- 慢集成测试用 `@pytest.mark.slow`。
- 断言要精确：禁止 `assert a or b` 这类等价于没断言的写法。

## 代码地图（改代码前先看）

[`docs/codemap/`](docs/codemap/) 是代码的自上而下索引：每个模块为什么长这样、改一处还要连带
检查什么、哪些地方看起来能改其实改了会坏。它**不是**设计文档——`docs/design/` 说「应该是什么」，
代码地图说「现在实际是什么」。

**每个 PR 的硬要求**（详见 [codemap §维护规则](docs/codemap/README.md#维护规则合并前的硬要求)）：

- 新增模块 → 新增 `docs/codemap/03-modules/<name>.md`
- 新增/删除公开符号 → 更新该模块页的符号表
- 改变跨模块调用关系 → 更新 `02-layers.md` 的允许边表
- 修掉一个陷阱 → 在 `06-pitfalls.md` 标「已修」，**不要删**
- 遇到花了 >15 分钟才搞明白的事 → 记进 `06-pitfalls.md`

> 判断标准：如果一个问题让你花了超过 15 分钟，它就是下一个踩坑的人会重蹈的坑。

## 提交约定

- 提交前 `scripts/check.sh` 必须全绿。
- **不得**提交 `.env` 或任何凭据；已有门禁与钩子拦截。
- 一个 Issue 一个提交，提交信息引用 Issue 编号。

## 分支与 PR 流程

**不直接推 `main`。** 每个改动走特性分支 + PR：

```bash
git checkout -b <type>/<short-slug>     # docs/security-data-api, feat/i01-kernel, fix/...
# ... 改动 ...
bash scripts/check.sh                   # 本地门禁
git commit -m "<type>: <summary>"
git push -u origin <branch>             # 推分支，不推 main
# 用 GitHub API 或网页创建 PR 到 main
```

- PR 必须等 `ci` 工作流通过（`pull_request` 触发，与本地同一份门禁）。
- 合并前自查 `docs/design/05-security.md` §7 的验收清单。
- 分支命名：`type/slug`，`type` ∈ `docs` / `feat` / `fix` / `ci` / `chore`。

> **为什么不用直推**：私有仓库当前**不支持分支保护**（需 GitHub Pro 或转公开）。
> 也就是说 `main` 在服务端没有任何强制约束，唯一能防住误操作的就是这个流程约定本身。
> 转公开或升级计划后应补上分支保护：要求 PR + CI 通过才能合并。

## 开发顺序

见 [docs/design/04-task-breakdown.md](docs/design/04-task-breakdown.md)。按 Issue 编号顺序推进，不做横切。