# 02 · 架构

> 状态：讨论定稿 v1 · 2026-11-26
> 上游：[01-requirements.md](01-requirements.md)
> 通信契约见 [02b-protocol.md](02b-protocol.md)

---

## 1. 进程模型

```
┌────────────────────────────────────────────────┐
│ GUI（Web UI，后补 Tauri 壳）                     │
└───────────────┬────────────────────────────────┘
                │ WebSocket（gateway）
┌───────────────▼────────────────────────────────┐
│ 编排内核（常驻进程）                              │
│  · 仓库 / worktree / 候选变更 / 收件箱 / 权限     │
│  · 通用 ACP 通道                                │
│  · 记忆空间（仓库外）                             │
└───┬───────────────────────────┬────────────────┘
    │ ACP over stdio            │ socket
┌───▼──────────────┐   ┌────────▼─────────────┐
│ OpenCode（外部）  │   │ Node PI 运行时        │
│ opencode acp      │   │ pi-coding-agent SDK  │
└──────────────────┘   └──────────────────────┘
```

**为什么内核必须常驻独立进程**：若 GUI 持有 worktree 与 Agent 子进程，窗口一关 Bot 就死，「常驻」不成立。GUI 只是内核的一个客户端；将来 CLI / 远程客户端接同一套接口。

## 2. 模块划分（Cordis 风格）

内核按 Cordis 的 **Service / Context / 生命周期**组织：注册项属于某个 context，卸载即回收。

| 模块 | 公开 Service | 职责 | 依赖 |
|---|---|---|---|
| `repo` | `repo.bind`, `repo.list`, `repo.branches` | 本地仓库绑定与 git 只读访问 | git |
| `worktree` | `worktree.acquire`, `worktree.release`, `worktree.list` | 每 Bot 一个常驻 worktree + 串行队列 | repo |
| `agent` | `agent.register`, `agent.capabilities`, `agent.run` | 接入通道登记与统一运行时接口 | — |
| `agent-acp` | （注册进 `agent`） | ACP stdio 通道：拉起、握手、会话、取消 | agent |
| `agent-pi` | （注册进 `agent`） | Node PI 运行时代理 | agent |
| `task` | `task.dispatch`, `task.cancel`, `task.status` | 派活、状态机、触发源去重 | agent, worktree |
| `candidate` | `candidate.propose`, `candidate.push`, `candidate.review` | 候选变更与渐进出口 | worktree, repo |
| `memory` | `memory.charter`, `memory.experience`, `memory.injectContext` | 记忆四层与上下文注入 | — |
| `permission` | `permission.check` | 权限分级判定 | memory, candidate |
| `gitapi` | （可选）`push`, `pr.open` | 远端出口；第一版仅本仓库 | candidate |

**约束**：

- 模块之间只通过 Service 调用，不直接 import 彼此内部。
- 每个注册项持有 disposer，卸载模块时回收全部副作用。
- `agent` 的统一接口是**唯一**的接入抽象；内核不得出现 `if kind == "acp"` 这类分支。

## 3. 统一 Bot 运行时接口

两条通道（ACP、PI）对内核暴露同一形状：

```
run(task) -> event stream:
    started(taskId)
    tool_call(name, args, blocked?)     # 权限闸门的判定点
    message(delta)
    turn_end(turnId, outcome)
    finished(diff | nothing, error?)
cancel(taskId)
capabilities() -> { flags: {...}, unsupported: [...] }
```

- `capabilities()` **必须来自运行时真实响应**（ACP 的 `initialize` 结果、PI 的实际注册），不得在配置里硬编码。
- 缺失能力必须显式出现在 `unsupported`，编排据此降级（例如不支持 fork 就改用上下文移交），而不是假装支持。

## 4. 工作区隔离

**策略：每 Bot 常驻 worktree + 内部串行队列。** 同一 Bot 同时只能跑一个任务。

- worktree 从默认分支创建，路径 `~/.postel/worktrees/<repo-id>/<bot>/`。
- Agent 的 `cwd` 被强制为该 worktree。
- Agent 会话结束后必须检查（见 01 §6.5）：是否切了分支、是否有多余 commit、是否有越界写。
- 最终 commit 由内核在受控分支上做**唯一一次**。

## 5. 记忆与上下文注入

```
~/.postel/bots/<repo-id>/<bot>/
  charter.md        ① 宪法层（人编辑）
  memory/           ③ 经验层
  sessions/         ④ 短期层
```

② 规范层**不复制**：任务启动时从仓库实时读取 `AGENTS.md` / `README.md` 及各层 `AGENTS.md`，拼成分层上下文。

**注入可见性**：GUI 必须能展示本次任务注入了哪些上下文（来源文件 + 命中层级），这是可审计的前提。

**冲突规则**：仓库层优先；Bot 级只能加严；冲突即阻断。

## 6. 接入层

### 6.1 ACP 通道

- 拉起任意 stdio ACP 命令（`command` + 字面参数列表，**不展开 shell 表达式**）。
- 生命周期：`initialize` → `session/new` → `prompt` → `session/cancel` → `session/close`。
- 能力以 `initialize` 响应为准。

### 6.2 PI 运行时（路线 A）

- `@earendil-works/pi-coding-agent` 以库形式嵌入 Node 侧进程（**钉死版本**）。
- 二次开发落点：`ExtensionAPI` 的 `registerTool` / `registerMcpServer` / `registerProvider` / `setActiveTools` / `appendEntry`。
- **权限闸门落在 `tool_call` 事件的阻断上**，不靠 Prompt 约束（Pi 无默认 bash 沙箱）。
- Node 进程与内核之间走 socket，复用 gateway 设计。
- 已知待验证：`execute` 的真实调用签名、`tool_call` 阻断的实际生效路径。**验证通过前不得在文档中宣称已支持。**

## 7. 数据落盘

| 数据 | 位置 | 说明 |
|---|---|---|
| 仓库绑定 / 任务 / 候选 | `state.db`（SQLite） | 只存绑定与元数据 |
| worktree | `~/.postel/worktrees/` | 真正的文件，人可直接查看 |
| Bot 记忆 | `~/.postel/bots/` | 与仓库完全分离 |
| 服务器地址 | `~/.postel/server.json` | 供客户端发现端口，权限 600 |

## 8. 范围外（MVP 不做）

- 自研 Agent 内核与模型；
- 远程沙箱实现（仅留接口）；
- 跨仓库 PR、合并；
- 多 Git 平台。

## 9. 已知技术债务与风险

| 项 | 说明 |
|---|---|
| Pi 上游变动 | 包已迁移 scope，API 面可能再变；必须钉版本并保留升级闸门 |
| 双运行时 | Node + 内核两套语言，构建与调试复杂度上升 |
| ACP 语义缺失 | 无「提交」语义，产出边界必须由内核兜底 |
| 拦截有效性 | `tool_call` 阻断需实测；无效则 PI 不得获得写权限 |