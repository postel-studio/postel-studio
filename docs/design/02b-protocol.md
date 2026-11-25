# 02b · 协议契约

> 状态：讨论定稿 v1 · 2026-11-26
> 上游：[02-architecture.md](02-architecture.md)
> 本文只定义**我们自己**的协议；对 ACP / MCP 只做引用，不重新定义。

---

## 1. 三层协议，不要混为一谈

| 层 | 谁连谁 | 协议 | 是否我们发明 |
|---|---|---|---|
| ① 前端 ↔ 内核 | GUI ↔ 常驻进程 | **WebSocket + JSON 帧** | 是（本文 §2） |
| ② 内核 ↔ Agent（ACP） | 内核 ↔ OpenCode 等 | **ACP over stdio** | 否（用标准） |
| ③ 内核 ↔ PI 运行时 | 内核 ↔ Node 进程 | **WebSocket + JSON 帧（同 §2 形状）** | 是（本文 §3） |
| ④ Agent ↔ 工具 | Agent ↔ MCP Server | **MCP** | 否（用标准） |

**关键约束**：② 必须保持 ACP over stdio，**不要改成 socket**——自己发明协议会丢掉「Registry 里现成 Agent 拿来即用」这个最大好处。

## 2. 层①：前端 ↔ 内核

### 2.1 传输

- WebSocket，默认 `ws://127.0.0.1:<port>/gateway`。
- 端口由内核在启动时写入 `~/.postel/server.json`（`{host, port, pid}`，权限 600），客户端据此发现。
- 单连接多路复用；每帧一个 JSON 对象。

### 2.2 三种帧

```jsonc
// 请求：期待响应
{"type": "req", "id": "c1", "method": "repo.bind", "params": {"path": "/abs/path"}}

// 响应：对应某个请求
{"type": "res", "id": "c1", "ok": true, "result": {"repoId": "adec6540a725"}}
{"type": "res", "id": "c1", "ok": false, "error": {"code": "NOT_A_REPO", "message": "..."}}

// 订阅推送：内核 → 客户端，不需要请求
{"type": "evt", "topic": "task.updated", "seq": 41, "data": {"taskId": "...", "state": "running"}}
```

- `id` 由客户端生成，仅在单连接内唯一。
- `seq` 单调递增，客户端可据此检测丢帧。
- 错误 `code` 是稳定字符串，**不要**把人类可读文案当错误码。

### 2.3 订阅语义

- 客户端发 `{"method": "subscribe", "params": {"topics": ["task.updated", "candidate.proposed"]}}`。
- 内核只向已订阅的 topic 推 `evt`。
- **重连后客户端必须重新订阅**；内核不保存订阅状态。

### 2.4 方法命名

`<模块>.<动作>`，与 [02-architecture.md](02-architecture.md) §2 的 Service 一致：

```
repo.bind / repo.list / repo.branches
worktree.list
task.dispatch / task.cancel / task.status
candidate.list / candidate.push / candidate.approve / candidate.reject
memory.charter.get / memory.charter.set
permission.check
agent.list / agent.capabilities
```

**禁止**在方法名里出现具体 Agent 名（如 `opencode.run`）——那会破坏解耦。

## 3. 层③：内核 ↔ PI 运行时

复用 §2 的帧形状，但**方向相反的主语**：

```jsonc
// 内核 → PI 运行时
{"type": "req", "id": "p1", "method": "runtime.run", "params": {"taskId": "...", "cwd": "...", "prompt": "..."}}
{"type": "req", "id": "p2", "method": "runtime.cancel", "params": {"taskId": "..."}}

// PI 运行时 → 内核（推送）
{"type": "evt", "topic": "runtime.tool_call", "data": {"taskId": "...", "name": "bash", "args": {...}}}
{"type": "evt", "topic": "runtime.turn_end", "data": {"taskId": "...", "outcome": "ok"}}
```

### 3.1 权限判定的往返

工具调用必须**由内核裁决**，PI 运行时不得自行放行：

```
PI 运行时  --runtime.tool_call-->  内核
内核（按 Bot 权限判定）--res ok/blocked-->  PI 运行时
PI 运行时：blocked 则不执行，把拒绝结果作为工具结果返回给 Agent
```

这条往返是 `tool_call` 阻断能成立的前提。**若实测中 PI 无法被阻断，则 PI 运行时不得获得任何写权限（只能 L0 只读）。**

## 4. 层②：ACP 通道（引用标准）

不在此重新定义。使用要点：

| 步骤 | 方法 | 备注 |
|---|---|---|
| 握手 | `initialize` | 返回 `protocolVersion` 与 `agentCapabilities`，**能力以此为准** |
| 建会话 | `session/new` | 需**绝对** `cwd`，可带 `mcpServers` |
| 投喂 | `session/prompt` | 期间接收 `session/update` 通知 |
| 取消 | `session/cancel` | |
| 关闭 | `session/close` | 若 Agent 声明支持 |
| 恢复 | `session/resume` / `session/load` | **两者不同**，按能力选择 |

实测记录（OpenCode 1.19.6）：声明 `sessionCapabilities: {close, fork, list, resume}`、`mcpCapabilities: {http, sse}`、`promptCapabilities: {embeddedContext, image}`。

## 5. 帧大小与流控

- 单帧上限 1 MiB；超出应分片或用附件引用。
- `session/update` 的流式文本按批推送，不做逐 token 转发到客户端（避免前端过载）。
- 客户端节流由前端负责，内核不替客户端做节流。

## 6. 版本与兼容

- 帧格式不设独立版本号；新增字段一律**向后兼容**（客户端忽略未知字段）。
- 破坏性变更必须同时改 `server.json` 里的 `apiVersion`，客户端不匹配时拒绝连接并提示升级。

## 7. 禁止事项

- **不得**把凭据（token / key）放进任何帧。
- **不得**在帧里传本地绝对路径给远程客户端（第一版仅本地，`127.0.0.1` 绑定）。
- **不得**让 Agent 直接连内核 socket（只能连它的代理通道）。