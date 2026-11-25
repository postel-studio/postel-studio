# Postel Studio

> 以仓库为第一视图的 Agent 工作台。
> 状态：`v0.1` 设计定稿，开发中（21 个切片，见 [任务拆解](docs/design/04-task-breakdown.md)）

不是又一个 AI IDE，也不是又一个 Git 客户端，而是 **Agent 时代的 Git 工作台**：
人在仓库全景里派活、看 diff、做验收，Bot 在仓库里常驻、认领工作、产出候选变更。

## 设计文档

设计先于实现，全部在 [`docs/design/`](docs/design/)：

| 文档 | 内容 |
|---|---|
| [01-requirements](docs/design/01-requirements.md) | 定位、15 项已定决定、权限模型、记忆四层、连接能力事实基线 |
| [02-architecture](docs/design/02-architecture.md) | 进程模型、Cordis 风格模块划分、统一 Bot 运行时接口 |
| [02b-protocol](docs/design/02b-protocol.md) | 前端↔内核、内核↔PI 的帧契约 |
| [03-ui-spec](docs/design/03-ui-spec.md) | 仿 GitHub 的仓库页规格 |
| [04-task-breakdown](docs/design/04-task-breakdown.md) | M1~M3 里程碑与 21 个垂直切片 |
| [issues/](docs/design/issues/) | 每个切片一个文件：背景、验收、涉及文件、测试、禁止事项 |
| [prototype/](docs/design/prototype/) | 可点击的 HTML 原型（浏览器直接打开） |

## 目录结构

```text
postel-studio/
├── .env                 # 本地密钥，已被 .gitignore 排除
├── .github/workflows/   # ci.yml（质量门禁）、publish.yml（tag 触发发布）
├── docs/design/         # 设计文档、21 个 Issue、原型、截图
├── scripts/check.sh     # 统一质量门禁（本地与 CI 同一份）
├── src/postel_studio/   # 包源码
├── tests/               # 测试
├── pyproject.toml       # 包名 postel-studio，入口脚本 postel-studio
└── uv.lock
```

## 开发

```bash
uv sync                # 创建 .venv 并安装（editable）
uv run postel-studio   # 运行入口
bash scripts/check.sh  # 质量门禁：lint + 格式 + 类型 + 测试覆盖率 + 凭据扫描
```

依赖统一用 `uv add <pkg>` 管理，不要手改 `pyproject.toml` 再去 `pip install`。

## 约定

- **包名**：安装名 `postel-studio`（连字符），导入名 `postel_studio`（下划线）。
- **分支**：默认 `main`，已跟踪 `origin/main`。
- **密钥**：只放 `.env` 或本机凭据里，绝不提交；`.env` 已在 `.gitignore`。
- **提交前**：`scripts/check.sh` 必须全绿（pre-commit 钩子会拦截 lint/format/凭据问题）。

## 仓库

- https://github.com/postel-studio/postel-studio （私有，完成一轮开发后再考虑开源）