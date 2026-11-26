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
| `system.subscribe` | 订阅 topic（02b §2.3） |

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
| `bot.create` | `repoId, name, agentKind, permission?` | `bot` |
| `bot.set_permission` | `botId, permission` | `bot` |
| `bot.delete` | `botId` | `{removed:bool}` |

### 2.4 任务

| 方法 | 参数 | 返回 |
|---|---|---|
| `task.dispatch` | `botId, prompt, baseRef?` | `task` |
| `task.cancel` | `taskId` | `task` |
| `task.status` | `taskId` | `task` |
| `task.list` | `botId?, state?` | `task[]` |

### 2.5 候选变更

| 方法 | 参数 | 返回 |
|---|---|---|
| `candidate.list` | `repoId?, reviewState?` | `candidate[]` |
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

### 4.3 `task.dispatch`

```json
{"type":"req","id":"c3","method":"task.dispatch",
 "params":{"botId":"adec6540a725/builder","prompt":"补 README 英文版"}}
{"type":"res","id":"c3","ok":true,
 "result":{"data":{"taskId":"01JC5...","state":"queued","botId":"adec6540a725/builder"},"seq":15}}
```

同 Bot 已有任务在跑时：

```json
{"type":"res","id":"c3","ok":false,
 "error":{"code":"BUSY","message":"该 Bot 已有任务在运行","field":"botId"}}
```

> **设计取舍**：`BUSY` 而不是自动排队。自动排队会让调用方无法知道何时真正开始；
> 排队由客户端重试或 UI 明确显示「已排队」实现。**两条路都可以，但必须选一条并保持一致**——MVP 选 `BUSY`。

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