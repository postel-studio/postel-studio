# 07 · 接口契约

> 状态：定稿 v1 · 2026-11-26
> 上游：[02b-protocol.md](02b-protocol.md)（帧层）、[06-data-model.md](06-data-model.md)（实体）
> 分工：**02b 定义帧长什么样，本文定义每个方法收什么发什么。**

---

## 1. 通用约定

### 1.1 调用形式（回顾 02b §2）

```json
{"type":"req","id":"c1","method":"repo.branches","params":{"repoId":"adec6540a725"}}
{"type":"res","id":"c1","ok":true,"result":{...}}
{"type":"res","id":"c1","ok":false,"error":{"code":"REPO_NOT_BOUND","message":"...","field":"repoId"}}
```

### 1.2 响应包络

所有 `result` 统一为：

| 字段 | 类型 | 说明 |
|---|---|---|
| `data` | object \| array | 实际载荷 |
| `seq` | number | 该响应对应的内核状态序号，客户端用于判断是否已过期 |

### 1.3 错误码

**错误码是稳定契约**，文案可以改，码不能改。`message` 只用于日志与调试，**不得**作为条件判断依据。

| 码 | HTTP 类比 | 含义 | 是否可重试 |
|---|---|---|---|
| `INVALID_PARAMS` | 400 | 参数缺失、类型错、越界 | 修正后可 |
| `UNKNOWN_METHOD` | 404 | 方法不存在 | 否 |
| `NOT_A_REPO` | 400 | 路径不是 git 仓库 | 否 |
| `REPO_NOT_BOUND` | 404 | repoId 不存在 | 否 |
| `BAD_REF` | 422 | 分支/提交不存在 | 否 |
| `BAD_BASE_REF` | 422 | worktree 基础分支无效 | 否 |
| `BAD_CWD` | 400 | 相对路径（要求绝对） | 修正后可 |
| `PATH_OUTSIDE_ROOT` | 403 | 路径越出允许根 | 否 |
| `PERMISSION_DENIED` | 403 | 权限不足 | 否 |
| `NOT_SUPPORTED` | 400 | 第一版不支持（如 L3/L4；S5 起也用于「仓库含非 UTF-8 的 ref 名」） | 否 |
| `WORKTREE_CONFLICT` | 409 | 工作区目录被占用 / 里面有未提交的改动 / 分支被别的 worktree 占着（S7 新增） | 人工处理后可 |
| `ALREADY_EXISTS` | 409 | 要创建的东西已经存在（S8 新增：同一个仓库里已有同名 Bot） | 换个名字后可 |
| `ADAPTER_UNAVAILABLE` | 503 | Agent 命令不存在/起不来 | 可 |
| `ADAPTER_TIMEOUT` | 504 | 握手超时 | 可 |
| `CONTEXT_CONFLICT` | 409 | 规范冲突，阻断任务 | 人工解决后 |
| `SCOPE_VIOLATION` | 409 | 会话越界（切分支/越界写） | 人工评估后 |
| `BUSY` | 429 | 该 Bot 已有任务在跑 | 稍后 |
| `INTERNAL` | 500 | 未预期错误 | 可，需报障 |

#### 进程启动错误码（非协议帧，由 CLI 输出到 stderr）

这些错误发生在内核尚未监听时，因此不经过 WebSocket 帧，而由 `serve` 子命令打印到 stderr 并以退出码
`1`（参数类错误为 `2`）结束。**码名同样稳定**。

| 码 | 退出码 | 含义 | 是否可重试 |
|---|---|---|---|
| `KERNEL_ALREADY_RUNNING` | 1 | 已有存活内核持有锁；需 `--force` 才能接管 | 先停旧内核 |
| `LISTEN_FAILED` | 1 | 指定端口无法监听 | 换端口 |
| `PORT_INVALID` | 1 | `--port` 超出 `0..65535` | 修正参数 |
| `HOST_NOT_ALLOWED` | 2 | `--host` 不是 `127.0.0.1`（硬约束，见 05 §1.1） | 不可绕过 |

**禁止**新增未登记的错误码；新增必须同时更新本表与 `errors` 映射。

### 1.4 命名

`<模块>.<动作>`。**禁止**方法名含具体 Agent 名（`opencode.*` 是错的）。

---

## 2. 方法清单

### 2.1 系统

| 方法 | 说明 |
|---|---|
| `system.health` | 内核存活、版本、`apiVersion`、状态根 |
| `subscribe` | **连接级**方法（S10）：订阅 topic、随连接注销；实现名**没有** `system.` 前缀（02b §2.3：连接级与请求级不同类） |

### 2.2 仓库

| 方法 | 参数 | 返回 |
|---|---|---|
| `repo.bind` | `path` | `repo` |
| `repo.unbind` | `repoId` | `{removed:bool}` |
| `repo.list` | — | `repo[]` |
| `repo.branches` | `repoId` | `branch[]` |
| `repo.diff` | `repoId, base, head, maxChars?` | `{stat, patch, truncated}` |

### 2.3 Bot

| 方法 | 参数 | 返回 |
|---|---|---|
| `bot.list` | `repoId` | `bot[]` |
| `bot.create` | `repoId, name, permission?, agentKind?` | `bot`（S8：`agentKind` 只收缺省/`null`，见 §4.10） |
| `bot.set_permission` | `botId, permission` | `bot` |
| `bot.delete` | `botId` | `{removed:bool}` |

### 2.4 任务

> **S10 实现了前三行**（`task.dispatch` / `task.cancel` / `task.status`）；S15 加了
> `task.latest`（单条、只读，供重连恢复中断态）；`task.list` 仍是后续切片（页面只跟
> 「选中 Bot 的最近一次任务」，不需要全列表）。

| 方法 | 参数 | 返回 | 状态 |
|---|---|---|---|
| `task.dispatch` | `botId, prompt, baseRef?` | `task`（queued 载荷） | ✅ §4.3（S10） |
| `task.cancel` | `taskId` | `task` | ✅ §4.3（S10，running 时异步生效） |
| `task.status` | `taskId` | `task` | ✅ §4.3（S10） |
| `task.latest` | `botId` | `task` 或 `null` | ✅ S15（D8：重连恢复中断态；只查最近一条，非全列表） |
| `task.list` | `botId?, state?` | `task[]` | 未实现（`UNKNOWN_METHOD`） |

### 2.6 权限审计（S16）

| 方法 | 参数 | 返回 | 说明 |
|---|---|---|---|
| `audit.list` | `botId?` | `audit[]` | 只读 `<状态根>/audit.jsonl`；每行 `{at, botId, permission, action, resource, effect, source}`，不含 key / prompt |


### 2.5 候选变更

| 方法 | 参数 | 返回 |
|---|---|---|
| `candidate.list` | `repoId, taskId?, reviewState?` | `candidate[]`（S11：repoId 必需，见 §4.12） |
| `candidate.approve` | `candidateId` | `candidate` |
| `candidate.reject` | `candidateId, reason?` | `candidate` |
| `candidate.push` | `candidateId` | `candidate` |
| `candidate.open_pr` | `candidateId` | `candidate` |

### 2.6 记忆

| 方法 | 参数 | 返回 |
|---|---|---|
| `memory.charter.get` | `botId` | `{content, rev}` |
| `memory.charter.set` | `botId, content, expectedRev` | `{rev}` |
| `memory.context.preview` | `botId` | `{sources:[{path,level,bytes}], totalBytes}` |

### 2.7 接入

| 方法 | 参数 | 返回 |
|---|---|---|
| `agent.list` | — | `agent[]`（已注册通道 + 能力） |
| `agent.capabilities` | `agentKind, target?` | `{flags, unsupported}` |
| `permission.check` | `botId, need` | `{allowed, reason?}` |

### 2.8 工作区

> S7 新增。工作区 = 一个真实的 `git worktree`，目录在 `<状态根>/worktrees/<repoId>/<botName>/`。
> **不落库**：事实源是 `git worktree list` 与目录本身（06 §2.5 的裁决）。

| 方法 | 参数 | 返回 |
|---|---|---|
| `worktree.acquire` | `repoId, botId, baseRef?` | `worktree`（幂等：已有则复用，`reused: true`） |
| `worktree.list` | `repoId` | `worktree[]`（不含 `reused`） |
| `worktree.release` | `repoId, botId` | `{removed: bool, path}`（**不删分支**） |

`worktree` 载荷（恰好七个字段）：

| 字段 | 类型 | 说明 |
|---|---|---|
| `repoId` | string | 12 位 hex |
| `botId` | string | `{repoId}/{name}` |
| `botName` | string | 目录末段 |
| `path` | string | 绝对路径，在 `worktrees` 根之下 |
| `branch` | string | `bot/<botName>/workspace` |
| `headSha` | string | 8 位小写 hex（边界格式化，同 §4.5） |
| `reused` | bool | 仅 `acquire` 有；`true` = 这次没有做任何 git 写操作 |

---

## 3. 事件（订阅推送）

| topic | 触发 | 载荷 |
|---|---|---|
| `repo.updated` | 绑定/解绑 | `{repoId}` |
| `task.updated` | 状态迁移 | `{taskId, state, errorCode?}` |
| `task.output` | 会话流式 | `{taskId, kind, chunk}` |
| `candidate.proposed` | 新候选 | `{candidateId, taskId}` |
| `candidate.updated` | 审核状态变化 | `{candidateId, reviewState}` |
| `agent.status` | 通道上下线 | `{agentKind, available}` |

**节流**：`task.output` 按 100ms 或 4 KiB 聚合推送，**不逐 token 转发**（02b §5）。

---

## 4. 关键请求/响应示例

### 4.1 `repo.bind`

```json
// 请求
{"type":"req","id":"c1","method":"repo.bind","params":{"path":"/home/u/proj"}}
// 成功
{"type":"res","id":"c1","ok":true,
 "result":{"data":{"repoId":"adec6540a725","name":"proj",
                   "defaultBranch":"main","originUrl":"https://github.com/o/proj","isDirty":true},
           "seq":12}}
// 失败
{"type":"res","id":"c1","ok":false,
 "error":{"code":"NOT_A_REPO","message":"路径不是 git 仓库","field":"path"}}
```

> **`repoId` 是稳定的**（S3.5 D1，见 [08-repo-id-stability.md](08-repo-id-stability.md)）：
> 仍是 12 位十六进制，由**第一次绑定的绝对路径**决定，写入后**永不改变**——加/改/删 remote、
> 重绑同一路径都不会让它变。客户端可以把它当长期缓存的身份用（`bot_id`、分支列表都挂在它上面）。
> 唯一会变的情形是仓库被移动到新路径。

### 4.2 `repo.diff`（简例；完整契约见 §4.6）

```json
{"type":"req","id":"c2","method":"repo.diff",
 "params":{"repoId":"adec6540a725","base":"main","head":"feature"}}
{"type":"res","id":"c2","ok":true,
 "result":{"data":{"stat":{"filesChanged":1,"insertions":2,"deletions":1},
                   "patch":"diff --git ...","truncated":false},"seq":12}}
```

**注意**：字段名用 `camelCase`（帧层统一），数据模型文档用 `snake_case`（落盘统一）。**转换发生在边界，内部不混用。**

### 4.3 `task.dispatch` / `task.status` / `task.cancel`（S10：派活 → 状态变绿 → 磁盘上真有改动）

`task.dispatch` **立即应答**：帧回 `queued`，全部慢活（建工作区、起 Agent 会话、等 prompt）
在内核侧的 worker 线程里跑。状态变化经 `subscribe` 订阅的 `task.updated` 事件推送（本节末尾）。

```json
{"type":"req","id":"c3","method":"task.dispatch",
 "params":{"botId":"adec6540a725/builder","prompt":"补 README 英文版"}}
{"type":"res","id":"c3","ok":true,
 "result":{"data":{"taskId":"01JC5...","state":"queued","botId":"adec6540a725/builder"},"seq":15}}
```

`task.status` / `task.cancel` 回**任务载荷**（S10 的 `orchestrator._payload`，恰好 7+3 字段）：

| 字段 | 类型 | 说明 |
|---|---|---|
| `taskId` | string | ULID（26 字符，时间有序） |
| `botId` | string | 复合键 |
| `state` | string | `queued` / `running` / `succeeded` / `failed` / `cancelled` / `interrupted` |
| `prompt` | string | 原文回显 |
| `baseRef` | string \| null | 派活时给的基分支 |
| `createdAt` | number | epoch 秒 |
| `startedAt` / `endedAt` / `errorCode` | number / number / string | 各自非 `null` 时才出现（**不给 null 占位**） |

生命周期（06 §3.1 状态机，`TaskStore.transition` 是唯一迁移入口）：

```
queued ──worker──▶ running ──stopReason──▶ succeeded | failed | cancelled
   │                  │
   └──cancel──▶ cancelled        内核重启：活跃行全部标 interrupted
```

**`stopReason` → 状态的映射 fail-closed（D9）**：`end_turn` → `succeeded`（**不代表磁盘上有
改动**——候选变更从磁盘看，S11 的事）、`cancelled` → `cancelled`、其余一律 `failed` +
`INTERNAL`。

取消的两条路径形状不同（实测 ACP 语义）：

| 任务状态 | `task.cancel` 做什么 | 返回 |
|---|---|---|
| `queued` | 直接迁移到 `cancelled`，worker 稍后醒来发现终态就退出（**零副作用**） | `cancelled` 载荷 |
| `running` | 给会话发 `session/cancel` 通知；迁移**交给** prompt 的应答（实测 ~0.3s 内带 `stopReason: "cancelled"` 回来） | `running` 载荷（取消是异步完成的） |
| 终态 | 不做任何事 | `INVALID_PARAMS`「任务已结束，不能再取消」 |

错误码（S10 的完整表）：

| 情形 | 码 |
|---|---|
| `botId` / `taskId` 缺失、非字符串、查无此行 | `INVALID_PARAMS` |
| `prompt` 空 / 超 32 KiB（**按 UTF-8 字节**） | `INVALID_PARAMS` |
| `baseRef` 不合法（`ref_name_ok` 的六条形状） | `INVALID_PARAMS` |
| Bot 是 L0 | `PERMISSION_DENIED`（**判定先于一切执行**，D4） |
| 该 Bot 已有活跃任务（queued/running） | `BUSY`（数据级兜底：`tasks_active_per_bot` 部分唯一索引） |
| Agent 命令不可用 / 超时 / 协议错 | `ADAPTER_UNAVAILABLE` / `ADAPTER_TIMEOUT` / `INTERNAL`（落 `failed` + `errorCode`，不回帧——任务已经发生了） |
| 未知 `stopReason` | `failed` + `INTERNAL`（fail-closed，不猜） |

> **设计取舍**：`BUSY` 而不是自动排队。自动排队会让调用方无法知道何时真正开始；
> 排队由客户端重试或 UI 明确显示「已排队」实现。**两条路都可以，但必须选一条并保持一致**——MVP 选 `BUSY`。

**订阅与事件（S10 D11）**：`subscribe` 是**连接级**方法（不在方法表里，重复调用 = 整体替换）：

```json
{"type":"req","id":"c9","method":"subscribe","params":{"topics":["task.updated"]}}
{"type":"res","id":"c9","ok":true,"result":{"data":{"topics":["task.updated"]},"seq":9}}
{"type":"evt","seq":10,"topic":"task.updated","data":{"taskId":"01JC5...","state":"running"}}
```

- topic 白名单 = `task.updated`（S10 只有一个；07 §3 的其它 topic 仍是后续切片）；
- 白名单外 / 空数组 → `INVALID_PARAMS`（拼错名字当场报错，不静默吞掉）；
- `evt` 的 `seq` 与 `res` 共用同一连接计数器（客户端丢帧检测跨帧连续）；
- 订阅**随连接死**：断开即注销，内核不保存任何订阅状态（02b §2.3）；
- `task.updated` 载荷恰好 `{taskId, state, errorCode?}`——**不是**完整任务载荷，
  要细节就发 `task.status`（07 §3 表的既有约定）。

### 4.4 `permission.check`

```json
{"type":"req","id":"c4","method":"permission.check",
 "params":{"botId":"adec6540a725/builder","need":"push"}}
{"type":"res","id":"c4","ok":true,
 "result":{"data":{"allowed":false,"reason":"需要 L2，当前 L1"},"seq":15}}
```

### 4.5 `repo.branches`（S4）

```json
// 请求：参数只有 repoId（没有 ref / pathspec；切分支与 diff 是之后的切片）
{"type":"req","id":"c5","method":"repo.branches","params":{"repoId":"adec6540a725"}}
// 成功：data 是**数组**，按 refname 字典序；空数组 = 空仓库，是成功
{"type":"res","id":"c5","ok":true,
 "result":{"data":[{"name":"feature","sha":"c7a21ad7","isCurrent":false,"upstream":null},
                   {"name":"main","sha":"c7a21ad7","isCurrent":true,"upstream":"origin/main"}],
           "seq":16}}
// 失败：repoId 查不到 / 绑定的路径已不是 git 仓库（目录或 .git 被删）
{"type":"res","id":"c5","ok":false,
 "error":{"code":"REPO_NOT_BOUND","message":"仓库路径已不存在"}}
```

`branch` 载荷字段（**不多不少四个**）：

| 字段 | 类型 | 规则 |
|---|---|---|
| `name` | string | 去掉 `refs/heads/` 的短名；带 `/` 的原样保留（`feat/x`） |
| `sha` | string | **8 位**小写十六进制（git 给 40 位；截断发生在内核的边界层，客户端只拿 8 位） |
| `isCurrent` | boolean | `HEAD` 指向它才为 `true`；**游离 HEAD 时全部 `false`** |
| `upstream` | string \| null | `%(upstream:short)`；没有 upstream 时是 **`null`**，不是 `""` |

顺序与范围：

- 顺序固定为 refname 字典序（`for-each-ref` 的缺省），**不**把当前分支提到最前——稳定顺序才能被客户端/测试断言。
- 只列**本地**分支（`refs/heads/`）。remote-tracking（`origin/main`）**不会**出现：它是别名不是分支，混进来会让"共几个分支"与"当前分支"同时失真。

错误码（**不许新增码**，07 §1.3 的既有六码之内）：

| 情形 | 码 | 可重试 |
|---|---|---|
| `repoId` 缺失 / 不是非空字符串 | `INVALID_PARAMS` | 修正后 |
| `repoId` 查不到 | `REPO_NOT_BOUND` | 否 |
| 绑定的路径已不是 git 仓库（目录 / `.git` 被删） | `REPO_NOT_BOUND` | 否 |
| `git` 不存在 | `ADAPTER_UNAVAILABLE` | 是 |
| `git` 超时 | `INTERNAL` | 是 |
| 空仓库（0 提交） | **成功**，`data: []` | — |

> 「路径已不存在」用 `REPO_NOT_BOUND` 而不是 `INTERNAL`：码表不许扩，而 `INTERNAL` 会把
> 「用户删了仓库」这种**正常处境**报成"未预期错误需报障"（S4 D5 的裁决）。
>
> 分支列表**不落库**（[06-data-model §8](06-data-model.md)）：每次按需从 git 读，
> 所以用户 `git switch` 之后刷新页面就能看到，不存在"缓存过期"这一类问题。

---

### 4.6 `repo.diff`（S5：点分支 → 看到 diff）

```json
// 请求：base / head 是任意可解析成 commit 的 ref 名（分支、tag、提交号都行）
{"type":"req","id":"c6","method":"repo.diff",
 "params":{"repoId":"adec6540a725","base":"main","head":"feature","maxChars":100000}}
// 成功：data 是**对象**；stat 描述完整差异，patch 可能被 maxChars 截断
{"type":"res","id":"c6","ok":true,
 "result":{"data":{"stat":{"filesChanged":2,"insertions":3,"deletions":1},
                   "patch":"diff --git a/f.txt b/f.txt\nindex 4cb29ea..6addb9b 100644\n...",
                   "truncated":false},
           "seq":17}}
// 失败：ref 不存在 / 不是 commit / 两个 ref 没有共同祖先
{"type":"res","id":"c6","ok":false,
 "error":{"code":"BAD_REF","message":"分支或提交不存在","field":"head"}}
```

**语义一句话**：三点 `base...head`（共同祖先 → head）——报的是"head 相对共同祖先改了什么"，
**不**把 base 在分支点之后自己的提交算进来（两点语义会；S5 D1）。

`data` 载荷（**不多不少三个字段**）：

| 字段 | 类型 | 规则 |
|---|---|---|
| `stat` | object | `{filesChanged, insertions, deletions}`，三个非负整数。`filesChanged` 是 git numstat **记录数**（含二进制、gitlink、纯 mode 变更）；插入/删除是各行数值求和（二进制的 `-` 记 0）。**不受截断影响**——统计描述完整差异 |
| `patch` | string | 完整 patch 文本（可能为 `""`：base 与 head 同一 commit 是**成功**，不是错误）。超过 `maxChars` 时按**字符**切（允许切在行中间），不静默丢内容：`truncated` 会告诉客户端 |
| `truncated` | boolean | `len(完整 patch) > maxChars`；只影响 `patch`，**不影响** `stat` |

`maxChars` 规则（S5 D6）：

| 项 | 值 / 规则 |
|---|---|
| 缺省 | `100000` |
| 合法区间 | `[1000, 200000]` |
| 类型 | 必须是 `int` 且**不是 `bool`**（`True` 是 `int` 子类，`isinstance(True, int)` 为真） |
| 越界 | `INVALID_PARAMS`（**不夹紧**：夹紧会让调用方以为拿到了它要的长度） |

错误码（**不许新增码**，07 §1.3 的既有码之内；顺序 = 参数形状 → 资源存在 → ref → 语义）：

| 情形 | 码 |
|---|---|
| `repoId` 缺失 / 非字符串 / 空 | `INVALID_PARAMS` |
| `repoId` 查不到 | `REPO_NOT_BOUND` |
| `base`/`head` 缺失 / 非字符串 / 空 / 超长 / 控制字符 / lone surrogate / `-` 开头 | `INVALID_PARAMS` |
| 仓库路径已不存在（目录或 `.git` 被删）**先于 ref 判** | `REPO_NOT_BOUND` |
| ref 解析不到（不存在 / 不是 commit，例如 tree id） | `BAD_REF` |
| 两个 ref 没有共同祖先（三点没有 merge base） | `BAD_REF`（文案「两个 ref 没有共同祖先，无法比较」） |
| `git` 不存在 | `ADAPTER_UNAVAILABLE` |
| `git` 超时 | `INTERNAL`（同 S3 D6，S19 的 doctor 再细化） |
| 仓库含非 UTF-8 的 ref 名（S5 D3.2 / U15） | `NOT_SUPPORTED` |
| 成功（含空差异） | `ok: true` |

> **不落库、不缓存**（[06-data-model §8](06-data-model.md)）：每次按需从 git 读。
> 缓存会让「用户刚 `git commit` 完」看到旧 diff，比慢更糟。
>
> **只到 `<a>...<b>`**：不支持 pathspec（`-- path`）、不开 rename 检测（重命名 = 一删一增）、
> 不做语法高亮（只按行着色，前端的事）。


### 4.7 `bot.list`（S6：左栏的 Bot）

```json
// 请求：repoId 是已绑定仓库的 12 位 id
{"type":"req","id":"c7","method":"bot.list","params":{"repoId":"adec6540a725"}}
// 成功：data 是**数组**；M1 每个仓库恰好一个预置 Bot（第一次 bot.list 时惰性播种）
{"type":"res","id":"c7","ok":true,
 "result":{"data":[{"botId":"adec6540a725/builder","repoId":"adec6540a725","name":"builder",
                    "permission":"L1","agentKind":null,"charterRev":3254720643,
                    "charterSummary":"尚未填写职责",
                    "memoryPath":"/home/u/.postel/bots/adec6540a725/builder"}],
           "seq":18}}
```

`bot` 载荷（**恰好 8 个字段，不多不少**，S6 D1）：

| 字段 | 类型 | 规则 |
|---|---|---|
| `botId` | string | `{repoId}/{name}`（06 §2.2 的复合主键） |
| `repoId` | string | 12 位 hex |
| `name` | string | 06 §2.2 白名单 `^[a-z][a-z0-9-]{0,31}$` |
| `permission` | string | `L0` / `L1` / `L2` / `L2+` 之一；新建默认 `L1`（S8 D2） |
| `agentKind` | string \| **null** | M1 是 **`null`（未选择）**：S9 之前没有任何通道被验证过，编一个适配器名等于宣称"已支持"（04 §6 禁止） |
| `charterRev` | number | 见 §4.8：`int(sha256(charter 内容)[:8], 16)` |
| `charterSummary` | string | charter 第一个正文段落的摘要（>80 字符截断加 `…`）；没有正文时 `"尚未填写职责"` |
| `memoryPath` | string | **绝对路径**：`<状态根>/bots/<repoId>/<name>`（不伪装 `~`，S6 D9） |

**S8 起这是纯读**：M1 的"惰性播种"（一个读方法会落一行）已删除（S8 D7，U17 的第一半）。
一个 Bot 都没有的仓库回空数组 `[]`，**不落行、不建目录**——创建是人的动作（§4.10 的
`bot.create`）。页面据此显示「这个仓库还没有 Bot。」+「新建 Bot」。

错误码（**不许新增码**，07 §1.3 之内）：

| 情形 | 码 |
|---|---|
| `repoId` 缺失 / 非字符串 / 空 | `INVALID_PARAMS` |
| `repoId` 查不到 | `REPO_NOT_BOUND` |
| 记忆目录 / charter 读失败（权限、磁盘满） | `INTERNAL` |

> **不探测仓库路径是否还在**：Bot 元数据在 `state.db`、charter 在状态根，与仓库目录是否存在
> 无关——仓库被删了，仍应能读到它给 Bot 写的 charter（审计材料）。只有 `repoId` 查不到才报
> `REPO_NOT_BOUND`。

### 4.8 `memory.charter.get`（S6：charter 全文）

```json
{"type":"req","id":"c8","method":"memory.charter.get","params":{"botId":"adec6540a725/builder"}}
{"type":"res","id":"c8","ok":true,
 "result":{"data":{"content":"# builder\n\n## 身份\n\n…","rev":3254720643},"seq":19}}
```

`data` 载荷（**恰好两个字段**）：

| 字段 | 类型 | 规则 |
|---|---|---|
| `content` | string | charter 全文（可能是空串；M1 的播种会先写入空白骨架） |
| `rev` | number | `int(sha256(content.encode("utf-8")).hexdigest()[:8], 16)`——整数、随内容变、无状态 |

**内容的事实源是文件**（`<状态根>/bots/<repoId>/<name>/charter.md`，权限 `0600`），**不落库**
（06 §8）。DB 的 `bots.charter_rev` 列先建但 M1 不维护（写路径是 S22 的 `charter.set`，06 U17）。
目录缺失时自愈（缺什么建什么）；**已存在的 charter 绝不覆盖**（用户编辑不可丢）。读失败
（权限 / 磁盘）→ `INTERNAL`。

错误码：

| 情形 | 码 |
|---|---|
| `botId` 缺失 / 非字符串 | `INVALID_PARAMS` |
| `botId` 格式合法但查不到（含路径穿越企图） | `INVALID_PARAMS`（文案「找不到这个 Bot」；07 §1.3 没有更贴的码，M1 不擅自扩码表） |
| charter 读失败（权限、磁盘满） | `INTERNAL` |

### 4.9 `worktree.acquire` / `list` / `release`（S7：分配工作区）

```json
{"type":"req","id":"c9","method":"worktree.acquire",
 "params":{"repoId":"adec6540a725","botId":"adec6540a725/builder","baseRef":"main"}}
{"type":"res","id":"c9","ok":true,"result":{"seq":9,"data":{
  "repoId":"adec6540a725","botId":"adec6540a725/builder","botName":"builder",
  "path":"/home/u/.postel/worktrees/adec6540a725/builder",
  "branch":"bot/builder/workspace","headSha":"91a5baa7","reused":false}}}
```

`baseRef` 缺省用 `repo.defaultBranch`；`reused: true` 表示这次只读、没有任何写操作。回收：

```json
{"type":"req","id":"c10","method":"worktree.release",
 "params":{"repoId":"adec6540a725","botId":"adec6540a725/builder"}}
{"type":"res","id":"c10","ok":true,"result":{"seq":10,"data":{
  "removed":true,"path":"/home/u/.postel/worktrees/adec6540a725/builder"}}}
```

`removed: false` 只出现在"本来就没有工作区"（重复回收不是错误）。错误码：

| 情形 | 码 |
|---|---|
| `repoId` / `botId` 缺失或类型错 / `botId` 查不到或属于别的仓库 | `INVALID_PARAMS` |
| `repoId` 查不到；仓库路径被删 / `.git` 被删 | `REPO_NOT_BOUND` |
| `baseRef` 形状不合法；没有默认分支又没给 `baseRef` | `INVALID_PARAMS` |
| `baseRef` 解析不到（含 0 提交的空仓库） | `BAD_BASE_REF` |
| 目录被占用 / 工作区脏（回收会丢东西）/ 分支被别的 worktree 占着 | `WORKTREE_CONFLICT`（**S7 新增码**） |
| git 不存在 | `ADAPTER_UNAVAILABLE` |
| git 超时 / `worktree add` 或 `remove` 因其它原因被拒 | `INTERNAL` |
| 仓库含非 UTF-8 的 ref 名 | `NOT_SUPPORTED` |

---

### 4.10 `bot.create` / `bot.set_permission`（S8：人建 Bot、人改权限）

```json
{"type":"req","id":"c11","method":"bot.create",
 "params":{"repoId":"adec6540a725","name":"qa-1","permission":"L2"}}
{"type":"res","id":"c11","ok":true,"result":{"seq":11,"data":{
  "botId":"adec6540a725/qa-1","repoId":"adec6540a725","name":"qa-1",
  "permission":"L2","agentKind":null,"charterRev":1525472935,
  "charterSummary":"尚未填写职责",
  "memoryPath":"/home/u/.postel/bots/adec6540a725/qa-1"}}}
```

返回的就是 §4.7 的 `bot` 载荷（**同样八个字段**）。建 Bot 同时建出记忆空间
（`<状态根>/bots/<repoId>/<name>/` + `charter.md` 模板 + `memory/` + `sessions/`，0700/0600）；
已有 `charter.md` 绝不覆盖。

`permission` 五条规则（S8 D2）：

| 入站 | 结果 |
|---|---|
| 缺字段 / `null` | 默认 **`L1`**（05 §2.2「新建 Bot」） |
| `L0` / `L1` / `L2` / `L2+` | 接受，落库并回载荷 |
| `L3` / `L4` | `NOT_SUPPORTED`——认识这两个名字，第一版不授予 |
| 其它（`L9` / `l1` / `""` / 数字） | `INVALID_PARAMS` |

`L3`（跨仓库 PR）与 `L4`（合并）**不在** `L0 < L1 < L2 < L2+` 这根梯子上（01 §4：不同维度），
所以"持 `L2+`"不蕴含"能对上游开 PR"。

改权限：

```json
{"type":"req","id":"c12","method":"bot.set_permission",
 "params":{"botId":"adec6540a725/qa-1","permission":"L1"}}
{"type":"res","id":"c12","ok":true,"result":{"seq":12,"data":{ …同上八字段，permission 为 "L1" }}}
```

`permission` 是**必填**（改权限没有"默认值"）；写成当前值也成功（幂等，不产生无意义的写）。

错误码：

| 情形 | 码 |
|---|---|
| `repoId` 缺失 / 非字符串 / 空 | `INVALID_PARAMS` |
| `repoId` 查不到 | `REPO_NOT_BOUND` |
| `name` 不在 06 §2.2 白名单（`^[a-z][a-z0-9-]{0,31}$`）或不是字符串 | `INVALID_PARAMS` |
| 同一仓库已有同名 Bot | `ALREADY_EXISTS`（**S8 新增码**，409） |
| `permission` 是 `L3` / `L4` | `NOT_SUPPORTED` |
| `permission` 是别的字符串或非字符串（**改权限时**缺字段 / `null` 也在此列） | `INVALID_PARAMS` |
| `agentKind` 给了字符串（S9 之前没有任何通道被验证过） | `NOT_SUPPORTED` |
| `agentKind` 给了非字符串的非 null 值 | `INVALID_PARAMS` |
| `botId` 缺失 / 非字符串 / 查不到 | `INVALID_PARAMS` |
| 记忆目录 / charter 读写失败（权限、磁盘满） | `INTERNAL` |

两条如实说明：

1. **权限只被存下来、显示出来，尚未被强制执行**——执行在 S16（权限门）。在此之前，
   `permission` 的价值是"人第一次能看见并修改它"，不是"它挡住了什么"。
2. **`bot.create` 不检查仓库路径是否还在**：它只写状态根、不读 git（S8 D9）。仓库回到原路径
   时 `repo_id` 不变，这一行就能重新用。

### 4.11 `agent.list` / `agent.capabilities`（S9：内核认识哪条通道、真的握手一次）

`agent.list` **不启动进程**：它只读登记表并回答「可执行文件在不在 `PATH` 上」。真正的握手是
`agent.capabilities`，它起一个子进程、发一次 `initialize`、读一行、把进程回收（S9 D4）。

```json
{"type":"req","id":"c13","method":"agent.list","params":{}}
{"type":"res","id":"c13","ok":true,"result":{"seq":13,"data":[
  {"agentKind":"acp-stdio","command":["opencode","acp"],"available":true}]}}
```

```json
{"type":"req","id":"c14","method":"agent.capabilities","params":{"agentKind":"acp-stdio"}}
{"type":"res","id":"c14","ok":true,"result":{"seq":14,"data":{
  "agentKind":"acp-stdio","available":true,"protocolVersion":1,
  "agentName":"OpenCode","agentVersion":"1.19.6",
  "flags":["loadSession","mcp.http","mcp.sse","prompt.embeddedContext","prompt.image",
           "session.close","session.fork","session.list","session.resume"],
  "unsupported":[],"elapsedMs":705}}}
```

`agent.list` 的条目：

| 字段 | 类型 | 说明 |
|---|---|---|
| `agentKind` | string | 通道 id，描述**传输**不描述产品（S9 D1） |
| `command` | string[] | 字面命令数组；可被进程级的 `POSTEL_ACP_COMMAND` 覆盖（D2） |
| `available` | bool | `shutil.which` 的静态结果，**不代表**握手会成功 |

`agent.capabilities` 的载荷：

| 字段 | 类型 | 说明 |
|---|---|---|
| `agentKind` / `available` | string / bool | 成功时 `available` 恒为 `true` |
| `protocolVersion` | int | 顶层字段，来自应答 |
| `agentName` / `agentVersion` | string \| null | 来自 `agentInfo`；Agent 不给就是 `null` |
| `flags` | string[] | **从 `agentCapabilities` 算出来的**，已排序（D3） |
| `unsupported` | string[] | S9 恒为 `[]`（参数里没有「请求某能力」的入口） |
| `elapsedMs` | int | 本次握手的墙钟耗时 |

`flags` 的命名规则（D3，实现见 `agent/acp_client.py` 的 `flatten_capabilities`）：

| 应答形状 | flag |
|---|---|
| `{"loadSession": true}` | `loadSession` |
| `{"mcpCapabilities": {"http": true}}` | `mcp.http` |
| `{"sessionCapabilities": {"close": {}}}` | `session.close` ← **空对象也表示支持** |
| `{"x": false}` 或键不存在 | 不产出 |

错误码：

| 情形 | 码 |
|---|---|
| `agentKind` 缺失 / 非字符串 / 空 / 未登记 | `INVALID_PARAMS` |
| `target` 非 null（S9 保留字） | `NOT_SUPPORTED` |
| 命令不存在、不可执行、没应答就退出 | `ADAPTER_UNAVAILABLE` |
| 30 秒内没有匹配 `id` 的应答 | `ADAPTER_TIMEOUT` |
| 应答不是合法 `initialize` 结果（没有 `protocolVersion`） | `INTERNAL` |
| `POSTEL_ACP_COMMAND` 不是合法的非空字符串数组 | `ADAPTER_UNAVAILABLE`（**不回退默认**） |

三条如实说明：

1. **能力值来自 Agent，不来自内核**：`flags` 是应答的摊平结果，不是白名单。Agent 升级后
   同一个方法可能回不同的 `flags`，这是设计意图，不是不稳定。
2. **`agent.list` 的 `available` 会撒谎一次**：它说「文件在」，但握手仍可能超时或协议不兼容。
   页面必须在点了检测之后才说「可用」。
3. **`bot.agentKind` 仍不可写**：S9 没有选通道的 UI，`bot.create` 继续拒绝非 null 的
   `agentKind`（U18，到期 S10）。

---

### 4.12 `candidate.list`（S11：任务成功后的一条候选详情）

`candidate.list` 是 S11 唯一新增的帧方法；S12 的收件箱列表也用它（按 `repoId` 取多条，
后端按 `created_at DESC` 排）。`taskId` 是详情卡的单行读，`reviewState` 是 06 §3.2 的筛选。

```json
{"type":"req","id":"c20","method":"candidate.list",
 "params":{"repoId":"adec6540a725","taskId":"01J..."}}
{"type":"res","id":"c20","ok":true,"result":{"seq":20,"data":[{
  "candidateId":"cand_01J...","taskId":"01J...","botName":"builder",
  "branch":"bot/builder/mym4tc1e","baseSha":"02c9...","headSha":"96fb...",
  "filesChanged":1,"insertions":1,"deletions":0,"reviewState":"local_only",
  "remote":null,"target":null,"createdAt":1795760000.0}]}}
```

| 字段 | 类型 | 说明 |
|---|---|---|
| `candidateId` | string | `cand_` + ULID |
| `taskId` / `botName` | string | 派生自任务/分支，候选表不冗余存 bot/repo |
| `branch` | string | `bot/<bot>/<ULID 随机尾部 8 位>`（S11 D1，见 issue §8 的撞车实测） |
| `baseSha` / `headSha` | string | 40 位 commit id；base 是**任务开始时的工作区 HEAD**，不是派活的 baseRef（D2） |
| `filesChanged` / `insertions` / `deletions` | int | 来自 `git diff --numstat`，与 S5 的 `repo.diff.stat` 同口径；**不是**缓存的 diff 文本 |
| `reviewState` | string | S11 恒 `local_only`（06 §3.2 的其余状态由 S13 写入） |
| `remote` / `target` | string \| null | `local_only` 时为 `null` |
| `createdAt` | number | epoch 秒 |

错误码：`repoId` 缺失/非字符串 → `INVALID_PARAMS`；仓库未绑定 → `REPO_NOT_BOUND`；
`taskId` / `reviewState` 形状不对 → `INVALID_PARAMS`。

**diff 从哪来**：载荷里没有 patch。页面拿 `baseSha` / `headSha` 再发 `repo.diff`（§4.6），
因此 diff 与 S5 的渲染件逐字一致，也遵守 06 §8「diff 不落库、不缓存」。

**事件**：任务成功后内核发 `candidate.proposed`（`{candidateId, taskId}`），细节由客户端
再发一次 `candidate.list` 取（03 §3 的载荷约定）。



---

### 4.13 `candidate.approve` / `candidate.reject`（S13：人的决定）

```json
{"type":"req","id":"r1","method":"candidate.approve","params":{"candidateId":"cand_01J..."}}
{"type":"res","id":"r1","ok":true,"result":{"seq":22,"data":{/* candidate，reviewState=approved */}}}

{"type":"req","id":"r2","method":"candidate.reject",
 "params":{"candidateId":"cand_01J...","reason":"改动没过测试"}}
{"type":"res","id":"r2","ok":true,"result":{"seq":23,"data":{/* candidate，reviewState=rejected，rejectReason 已填 */}}}
```

| 规则 | 说明 |
|---|---|
| 状态机 | `REVIEW_TRANSITIONS`（06 §3.2）：`local_only → approved` 合法；`rejected → approved`、`approved → *`、`→ merged` 均非法 |
| 理由 | `reason` 可选，trim 后 UTF-8 字节上限 2000（S13 D2）；落 `candidates.reject_reason` 列，随状态原子更新 |
| 竞态 | 两个请求同时改一行：`UPDATE … WHERE review_state = <from>` 只有一个命中，输家 `INVALID_PARAMS`（不覆盖赢家理由） |
| 事件 | 成功后 `candidate.updated`，载荷 `{candidateId, reviewState}`（07 §3 早已登记） |
| 错误码 | `candidateId` 缺失/不存在/状态非法/理由过长 → `INVALID_PARAMS`（既有码，无新增） |
| 权限 | 服务端在 S16 才强制；S13 的 UI 禁用只是提示（03 §4.7），不能当安全边界 |
| 不做 | `push` / `open_pr` 属 S16；**永不提供合并** |

---

## 5. 兼容与演进

| 变更 | 要求 |
|---|---|
| 新增字段 | 允许，客户端忽略未知字段 |
| 新增方法 | 允许 |
| 改字段名/类型 | **破坏性**：必须升 `apiVersion` |
| 删除方法 | **破坏性** |
| 新增错误码 | 允许但要登记本表 |
| 改错误码文案 | 允许 |

`server.json` 中携带 `apiVersion`；客户端不匹配时拒绝连接并提示升级（02b §6）。

---

## 6. 禁止事项

- **禁止**在帧中传凭据（02b §7）。
- **禁止**用错误文案判断分支逻辑。
- **禁止**方法名含 Agent 名。
- **禁止**返回解析后的绝对路径给越权请求（05 §3.2）。
- **禁止**让 Agent 直接连接本接口（只能走代理通道）。