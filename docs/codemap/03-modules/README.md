# 03 · 模块索引

> 上游：[README.md](../README.md) · 相关：[02-layers](../02-layers.md)

按依赖方向从下往上读。

| 层 | 模块页 | 源文件 | 行数 | 一句话 |
|---|---|---|---|---|
| L0 | [entry](entry.md) | `src/postel_studio/__init__.py` | 55 | CLI 入口，解析 `serve` / `version` |
| L0 | [paths](paths.md) | `src/postel_studio/storage/paths.py` | 322 | 状态根、锁、原子写、权限、Bot 记忆空间（S6）、工作区目录（S7） |
| L0 | [domain](domain.md) | `src/postel_studio/domain/models.py` | 478 | `Repo` / `Bot` / `Task` 实体、origin 归一化、`repo_id`、权限阶梯、任务状态机与 ULID（S8/S10）（**无 IO**） |
| L0 | [gitops](gitops.md) | `src/postel_studio/gitops/repo.py` | 500 | git **只读**探测（子进程，含 S4 分支列表 + S5 diff） |
| L0 | [gitops](gitops.md) | `src/postel_studio/gitops/worktrees.py` | 269 | git **写**操作（S7）：`worktree add` / `remove` / `prune`——写面只有这一处 |
| L0 | [agent](agent.md) | `src/postel_studio/agent/acp_client.py` | 427 | ACP over stdio（S9 握手 + S10 会话）：`initialize` / `session/new` / `prompt` / `cancel`、跳过噪声行、无条件回收 |
| L0 | [agent](agent.md) | `src/postel_studio/agent/registry.py` | 78 | 通道登记（S9）：`acp-stdio` 一条，命令是字面数组 + 进程级覆盖 |
| L0 | [storage-db](storage-db.md) | `src/postel_studio/storage/db.py` | 863 | `state.db`、schema 迁移（v5）、`RepoStore` / `BotStore` / `TaskStore` / `LeaseStore` / `CandidateStore` |
| L2 | [server](server.md) | `src/postel_studio/api/server.py` | 261 | 进程生命周期、信号、日志 |
| L3 | [app](app.md) | `src/postel_studio/api/app.py` | 157 | 路由表（3 个 HTTP 路由 + 网关挂载）+ 构造 store/service/编排器并注入 |
| L3 | [gateway](gateway.md) | `src/postel_studio/api/gateway.py` | 321 | WS 认证与帧分派（19 个方法）+ `subscribe` 特判与事件泵（S10） |
| L3 | [repository](repository.md) | `src/postel_studio/api/repository.py` | 378 | `repo.bind` / `repo.list` / `repo.branches` / `repo.diff` 的业务编排（**不碰 fastapi**） |
| L3 | [bots](bots.md) | `src/postel_studio/api/bots.py` | 267 | `bot.list` / `bot.create` / `bot.set_permission` / `memory.charter.get` 的业务编排（判完再写 + charter 摘要/rev，**不碰 fastapi**） |
| L3 | [worktrees](worktrees.md) | `src/postel_studio/api/worktrees.py` | 300 | `worktree.acquire` / `list` / `release` 的业务编排（复用/回收状态机，**不落库**） |
| L3 | [agents](agents.md) | `src/postel_studio/api/agents.py` | 108 | `agent.list` / `agent.capabilities` 的业务编排（只读登记表 vs 真握手） |
| L3 | [candidates](candidates.md) | `src/postel_studio/candidates.py` | 205 | 候选固化（commit/分支/落库）与 `candidate.list`（S11，**不 import 同层 handler**） |
| L3 | [orchestrator](orchestrator.md) | `src/postel_studio/orchestrator.py` | 460 | `task.dispatch` / `status` / `cancel` 的编排：worker 线程一任务一线程、`EventHub` 事件桥（S10，**不 import 同层 handler**） |
| L1 | [frames](frames.md) | `src/postel_studio/api/frames.py` | 126 | 帧编解码 + topic 白名单，纯函数 |
| L4 | [assets](assets.md) | `src/postel_studio/web/assets.py` | 2404 | 页面 HTML + 内联 JS（S3 仓库区 + S4 分支 + S5 差异 + S6 Bots/charter + S7 工作区 + S8 新建表单 + S9 Agent 卡片 + S10 派活卡片 + S11 候选详情卡） |

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