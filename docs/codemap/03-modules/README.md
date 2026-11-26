# 03 · 模块索引

> 上游：[README.md](../README.md) · 相关：[02-layers](../02-layers.md)

按依赖方向从下往上读。

| 层 | 模块页 | 源文件 | 行数 | 一句话 |
|---|---|---|---|---|
| L0 | [entry](entry.md) | `src/postel_studio/__init__.py` | 55 | CLI 入口，解析 `serve` / `version` |
| L0 | [paths](paths.md) | `src/postel_studio/storage/paths.py` | 203 | 状态根、锁、原子写、权限 |
| L0 | [domain](domain.md) | `src/postel_studio/domain/models.py` | 131 | `Repo` 实体、origin 归一化、`repo_id`（**无 IO**） |
| L0 | [gitops](gitops.md) | `src/postel_studio/gitops/repo.py` | 280 | git 只读探测（子进程，含 S4 分支列表）——**唯一起 git 的地方** |
| L0 | [storage-db](storage-db.md) | `src/postel_studio/storage/db.py` | 261 | `state.db`、schema 迁移、`RepoStore` |
| L2 | [server](server.md) | `src/postel_studio/api/server.py` | 256 | 进程生命周期、信号、日志 |
| L3 | [app](app.md) | `src/postel_studio/api/app.py` | 95 | 路由表（3 个 HTTP 路由 + 网关挂载）+ 构造 store 并注入 |
| L3 | [gateway](gateway.md) | `src/postel_studio/api/gateway.py` | 169 | WS 认证与帧分派（5 个方法） |
| L3 | [repository](repository.md) | `src/postel_studio/api/repository.py` | 256 | `repo.bind` / `repo.list` / `repo.branches` 的业务编排（**不碰 fastapi**） |
| L1 | [frames](frames.md) | `src/postel_studio/api/frames.py` | 121 | 帧编解码，纯函数 |
| L4 | [assets](assets.md) | `src/postel_studio/web/assets.py` | 683 | 页面 HTML + 内联 JS（S3 左栏仓库区 + S4 主区分支面板） |

**必须有但我还没单列的**（内容已在别处）：

| 项 | 在哪 | 说明 |
|---|---|---|
| `scripts/check.sh` | [05-change-impact §门禁](../05-change-impact.md#门禁) | 唯一质量门禁入口 |
| `tests/` 各文件 | [04-capabilities](../04-capabilities.md) 的「由谁守住」列 | 按能力反查测试 |
| `pyproject.toml` | [CONTRIBUTING.md](../../../CONTRIBUTING.md) | 依赖与全部工具配置 |

每个模块页的统一结构：

1. **职责** — 一句话
2. **符号表** — 公开函数/类/常量：做什么、谁在用
3. **关键决策与理由** — 为什么这么写（含被推翻的方案）
4. **修改须知** — 改它会波及什么
5. **测试** — 哪个测试文件守着它