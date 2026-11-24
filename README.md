# Postel Studio

> 以仓库为第一视图的 Agent 工作台。
> 状态：`v0.1` 骨架（仅项目脚手架，尚无业务代码）

不是又一个 AI IDE，也不是又一个 Git 客户端，而是 **Agent 时代的 Git 工作台**：
人在仓库全景里派活、看 diff、做验收，Bot 在仓库里常驻、认领工作、产出候选变更。

## 目录结构

```text
postel-studio/
├── .env                 # 本地密钥（GITHUB_TOKEN），已被 .gitignore 排除
├── .python-version      # 3.12
├── pyproject.toml       # 包名 postel-studio，入口脚本 postel-studio
├── uv.lock
├── README.md
└── src/postel_studio/
    └── __init__.py      # main()
```

## 开发

```bash
uv sync                # 创建 .venv 并安装（editable）
uv run postel-studio   # 运行入口
```

依赖统一用 `uv add <pkg>` 管理，不要手改 `pyproject.toml` 再去 `pip install`。

## 约定

- **包名**：安装名 `postel-studio`（连字符），导入名 `postel_studio`（下划线）。
- **分支**：默认 `main`，已跟踪 `origin/main`。
- **密钥**：只放 `.env` 或本机凭据里，绝不提交；`.env` 已在 `.gitignore`。

## 仓库

- https://github.com/MaoZhiqin/postel-studio （私有）

Product positioning doc lives outside this repo; this README only guarantees
build/run correctness.