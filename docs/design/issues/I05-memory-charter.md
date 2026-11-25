# I05 · 记忆目录骨架 + charter 读写

- **里程碑**：M1　**预计**：2h　**依赖**：I02　**优先级**：P0

## 背景

记忆**在仓库外**（D1）。本切片建立目录结构并让 charter 可读可写，不涉及经验层与压缩。

## 验收标准

1. 首次访问某 `(repoId, botName)` 时幂等创建：`charter.md` + `memory/` + `sessions/`。
2. 二次访问**不覆盖**人已编辑过的 `charter.md`。
3. 同名 Bot 在不同 `repoId` 下互不共享（目录不同、内容独立）。
4. 记忆根目录**绝不**位于任何仓库路径之内（有断言）。
5. `memory.charter.get` / `memory.charter.set` 可读写；写入后回读一致。
6. 前端能显示 charter 内容并保存。

## 涉及文件

- `src/postel_studio/storage/paths.py`（增加 `bots_root` / `ensure_memory_space`）
- `src/postel_studio/api/app.py`（`memory.charter.get/set`）
- `src/postel_studio/web/assets.py`（charter 编辑块）
- `tests/test_memory.py`（新建）

## 测试要求

- 幂等性：连续两次 ensure 后 charter 内容不被重置。
- 隔离性：两个 repoId 下同名 Bot 内容独立。
- 路径断言：`repo_path` 不在记忆目录的祖先链中。
- 写入后回读一致（含中文与换行）。

## 禁止事项

- 不得把记忆写进仓库目录（即使只在 `.gitignore` 里）。
- 不得在 charter 模板里预设具体业务职责（骨架必须是空白待填）。
- 不得把 `charter.md` 内容送进日志。
