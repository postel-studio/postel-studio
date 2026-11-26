# S6 · 左栏出现 Bot 与它的职责

- **里程碑** M1 · **预计** 2h · **依赖** S3 · **优先级** P1
- **用户路径**：左栏有「Bots」区 → 展开看到 Bot 名与职责摘要 → 点开看到 charter 全文

## 背景

Bot 是这个产品与普通 Agent 工具的分界。M1 不需要它能跑任务，只需要它**存在且可读**——证明"Bot 是一等公民"不是空话。

## 验收标准

1. 左栏「Bots」区列出该仓库下的 Bot（至少支持一个预置示例 Bot）。
2. 每个 Bot 显示名字与 charter 第一段（职责）。
3. 点开显示 charter 全文。
4. 记忆目录在仓库外：UI 显示路径 `~/.postel/bots/<repo-id>/<bot>/`。
5. 首次访问幂等创建目录（`charter.md` + `memory/` + `sessions/`），**不覆盖**已编辑内容。
6. **M1 验收**：S1~S6 全走通，且可用一条命令复现（脚本 `scripts/accept_m1.py`）。

## 涉及文件

- `src/postel_studio/domain/models.py`（加 `Bot`）
- `src/postel_studio/storage/paths.py`（加 `bots_root` / `ensure_memory_space`）
- `src/postel_studio/api/app.py`（加 `bot.list` / `memory.charter.get`）
- `src/postel_studio/web/assets.py`（Bots 区 + charter 面板）
- `src/postel_studio/domain/`（bot 抽象最小实现）
- `scripts/accept_m1.py`（新建，M1 全链路验收）
- `tests/test_bot_charter.py`、`tests/test_accept_m1.py`（新建）

## 测试要求

- 幂等：两次 ensure 后 charter 不被重置。
- 隔离：两个 repo 下同名 Bot 内容独立。
- 路径断言：记忆目录**不在**仓库路径内。
- `accept_m1.py` 用临时 `POSTEL_HOME`，结束后无残留。

## 禁止事项

- 不得把记忆写进仓库（即使只在 `.gitignore` 里）。
- 不得在 charter 模板里预设业务职责（骨架必须空白待填）。
- 不得让 `accept_m1.py` 使用真实 `~/.postel`。
