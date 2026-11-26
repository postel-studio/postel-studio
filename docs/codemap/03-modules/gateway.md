# 03 · gateway — `src/postel_studio/api/gateway.py`

> 层 L3 · 199 行 · 上游：[模块索引](README.md) · 协议：[02b-protocol](../../design/02b-protocol.md) · 分派对象：[repository](repository.md)

## 职责

WebSocket 网关：**认证 → 接受 → 分派 → 回帧**。

## 符号表

| 符号 | 行 | 做什么 | 谁在用 |
|---|---|---|---|
| `GATEWAY_PATH` | 39 | `"/gateway"`，路径前缀 | `server.gateway_path()` |
| `TICKET_TTL_SECONDS` | 40 | `60.0` | `TicketRegistry` |
| `CLOSE_FRAME_INVALID` | 42 | `4002` 关闭码 | `handler` |
| `TicketRegistry` | 45 | 一次性 ticket 签发/消费 | `app.create_app` |
| `ConnectionState` | 78 | 每连接的 `seq` 计数器（D4） | `handler` |
| `Providers` | 87 | frozen dataclass，注入依赖（`health` / `repo_bind` / `repo_list` / `repo_branches` / `repo_diff` / S6 的 `bot_list` / `memory_charter_get` / S7 的 `worktree_acquire` / `worktree_list` / `worktree_release` / S8 的 `bot_create` / `bot_set_permission`） | `app.create_app` |
| `active_connection_count()` | 113 | 活动连接数（**仅供测试观测**） | `test_gateway_inprocess.py` |
| `dispatch(method, params, providers)` | 119 | 方法表（**13 个分支**：S8 加了 `bot.create` / `bot.set_permission`） | `handler` |
| `install(app, path, tickets, providers)` | 150 | 把 WS 路由挂到 app | `app.create_app` |
| `_ACTIVE` / `_ACTIVE_LOCK` | 73-74 | 活动连接表（模块级） | 内部 |

## 关键决策与理由

### 认证：一次性 handshake ticket（D1）

**问题**：05 §1.1 要求 WebSocket 带 `Authorization: Bearer <token>`。
**但浏览器 `new WebSocket()` 无法设置自定义头**——物理上做不到。

两条看似可行的替代路都触碰硬约束：

| 替代路 | 违反 |
|---|---|
| `?token=<长期令牌>` | 令牌进 URL → 进日志（05 §4.1） |
| 渲染页面时把令牌注入 `<script>` | 「令牌不进页面源码」硬约束 |

**解法**：把「认证」与「长期凭据」拆开。

1. 内核另生成 **ticket**（32 字节 hex），只存内存；
2. 页面 `<meta name="postel-handshake">` 下发；
3. `consume()` **原子**取出，用后即删；60 秒过期；
4. `~/.postel/token` 的约束**完全不变**，留给将来的 CLI 客户端。

> **代价**：ticket 有效期内的同机进程若抢先扒走它，可夺一次连接。这需要「同用户 shell 权限」，
> 已被 05 §0 排除在威胁模型外。**若将来要做远程访问，此机制必须重做。**

### `consume()` 必须是原子的

```python
with self._lock:
    expiry = self._tickets.pop(ticket, None)
return expiry is not None and expiry > time.monotonic()
```

`pop` 在锁里完成 → 并发两个连接用同一 ticket，只有一个拿到 `True`。
`test_ticket_is_single_use` 守着这条。

### 先校验、后 `accept()`

```python
if not tickets.consume(...):
    await ws.close(code=1008)  # 不 accept
    return
await ws.accept()
```

**不 accept 直接 close** → uvicorn 把握手翻成 **HTTP 403**，客户端拿到
`websockets.exceptions.InvalidStatus`。

若先 `accept()` 再 `close()`，连接已经建立，**那不算拒绝**。

### 超大帧：关连接，不回首包

```python
if len(text.encode("utf-8")) > frames.MAX_FRAME_BYTES:
    await ws.close(code=CLOSE_FRAME_INVALID)  # 不回复错误帧
```

**按 UTF-8 字节数算，不是字符数**——中文 1 字符 = 3 字节，字符数不超但字节超的帧必须拒。

为什么不回错误帧：超大帧本身不可信，回帧等于把带宽送给攻击者（05 §3.1「关闭连接并记录」）。

### 两种断连异常都必须接住

```python
except (WebSocketDisconnect, ClientDisconnect):
    break
```

| 异常 | 来自 | 何时 |
|---|---|---|
| `starlette.websockets.WebSocketDisconnect` | Starlette | 收到关闭帧 |
| `uvicorn.protocols.utils.ClientDisconnected` | **uvicorn** | 发帧时发现对端已没（RST） |

**`ClientDisconnected` 不是 `WebSocketDisconnect` 的子类**（实测）。只接前者，
每断一次连就打一条 `ERROR: Exception in ASGI application` 栈。

> S2 踩过：一开始只接了 `WebSocketDisconnect`。**别把 `ClientDisconnected` 当冗余删掉。**

### `finally` 里删活动表（D5）

```python
finally:
    with _ACTIVE_LOCK:
        _ACTIVE.pop(conn_id, None)
    logger.info("网关连接关闭，当前活动连接 %d", active_connection_count())
```

三条退出路径（客户端掉线 / 异常 / 服务端主动关）**都走这里**。
不许只在正常路径删——那是「看起来清理了」而不是「真的清理了」。

**那条日志是 S2 验收 A2（断连 ≤3s 服务端清理）的可观测点。**
改措辞会让 `test_server_cleans_up_connection_after_client_disconnect` 失败。

### `seq` 是**连接级**计数器（D4）

`ConnectionState.seq` 每连接一个实例，从 0 开始，每发一帧 +1。
**不是全局的**——02b §2.2 的「检测丢帧」语义就是单连接内。

`system.health` 与 `system.echo` **共用同一个计数器**，不另造。

### `dispatch()` 现在有八个分支（S2 两个 + S3 两个 + S4/S5 各一 + S6 两个）

```python
# api/gateway.py:111-129（真实代码的形状，别把业务写进来）
if method == "system.echo":
    return params  # 通道自检
if method == "system.health":
    return providers.health()
if method == "repo.bind":  # S3
    return providers.repo_bind(params)
if method == "repo.list":  # S3
    return providers.repo_list()
if method == "repo.branches":  # S4：参数只有 repoId
    return providers.repo_branches(params)
if method == "repo.diff":  # S5：repoId + base + head(+ maxChars)
    return providers.repo_diff(params)
if method == "bot.list":  # S6：一个仓库下的 Bot（空则惰性播种，D2）
    return providers.bot_list(params)
if method == "memory.charter.get":  # S6：charter 正文 + 内容哈希版本（D5）
    return providers.memory_charter_get(params)
raise FrameError(UNKNOWN_METHOD, ...)
```

**故意不实现** `system.subscribe` —— 订阅机制属后续切片。
未实现的方法返回 `UNKNOWN_METHOD`，**不要返回空成功**（客户端会以为订阅生效了）。

S3–S6 的六个业务分支都是**转手**：真正的实现在 `api/repository.py`（`RepositoryService`），
由 `app.create_app()` 构造后经 `Providers` 注入。网关不认识"仓库/数据库/git"任何一个词。

> **后续切片注意**：方法多到 6~8 个时，`if` 链应该改成注册表（dict）。
> **S6 已经到 8 个分支**（阈值上沿）；S6 的裁决是"本刀只加两行、不顺手重构"（预算），
> 下一个方法应当把 `if` 链换成 dict 注册表，但**不要**单独为这次重构开一刀。

> ⚠️ `test_unknown_method_is_unknown_method_not_subscribe` 曾把 `repo.bind` 当"未实现"的反例。
> S3 实现它之后反例改成 `bot.list` / `task.dispatch` / `agent.list` / `repo.branches` 等；
> **S4 实现 `repo.branches` 后又拿掉了它**（现在反例是 `bot.list` / `task.dispatch` /
> `agent.list` / `system.subscribe` / `system.echo.x`，5 个；`repo.diff` **不在**清单里，
> S5 没有新增反例）。每实现一个方法，都要把它从
> 那条反例清单里拿掉，否则测试红得莫名其妙。

### `Providers` 注入消掉循环依赖

`gateway` 需要 `health()`，但 `app` import `gateway`，反向 import 会成环。
解法：`app` 把 `health` 作为 `Providers(health=health)` 传进来。

实测：把 `from postel_studio.api.app import health` 挪到 `gateway` 模块级 →
`ImportError: cannot import name 'health' from partially initialized module`。
**环是真的，但该消掉而不是绕开。**

## 修改须知

| 如果你要… | 还必须 |
|---|---|
| 加一个 WS 方法 | `dispatch()` 加分支 + `07-api-contract §2` 登记 + 测试 `UNKNOWN_METHOD` 反例 |
| 改 ticket 有效期 | `TICKET_TTL_SECONDS` + 05 §1.1.1 的文字 |
| 改错误码 | `frames.py` 常量 + `07-api-contract §1.3` 表 |
| 给网关加状态 | 想清楚是**全局**还是**每连接**；`seq` 是每连接，`_ACTIVE` 是全局 |
| 让网关调用更多业务 | 新增 handler 模块（如 `api/repository.py`），**别把业务写进 dispatch**；在 `Providers` 加一个字段 + `app.create_app()` 里注入 |
| 给 `Providers` 加字段 | `app.create_app()` 的 `gateway.Providers(...)` 是**唯一**构造点；`test_gateway_inprocess.py` 若自己构造 `Providers` 也要同步 |

## 测试

| 测试 | 守什么 |
|---|---|
| `test_echo_roundtrip_returns_data_and_seq` | A5 往返 |
| `test_health_roundtrip_matches_http_health` | 两处 health 逐字段相等 |
| `test_seq_is_strictly_increasing_within_one_connection` | 断言恰好 `[1,2,3]` |
| `test_seq_restarts_for_a_new_connection` | 连接级而非全局 |
| `test_unknown_method_is_unknown_method_not_subscribe` | `UNKNOWN_METHOD` |
| `test_unknown_method_does_not_close_connection` | 错误不致命 |
| `test_oversized_frame_closes_the_connection` | >1MiB 关连接 |
| `test_invalid_frame_with_id_gets_invalid_params` | `INVALID_PARAMS` |
| `test_ticket_is_single_use` | 一次性 |
| `test_missing_ticket_is_rejected_with_403` / `test_forged_ticket_is_rejected_with_403` | 拒绝握手 = 403 |
| `test_server_cleans_up_connection_after_client_disconnect` | D5 清理 |
| `test_gateway_inprocess.py::test_closing_one_of_two_connections_drops_the_count` | 计数 2→1 |
| `tests/test_branches.py::test_websocket_branches_roundtrip_matches_service_layer` | S4 D7 第 9 行：`repo.branches` 帧往返（`result.data` 数组 + `seq`），与直调服务层一致 |
| `tests/test_diff.py::test_ws_repo_diff_matches_service` | S5 D10 第 21 行：`repo.diff` 帧往返（`result.data` = `{stat,patch,truncated}` + `seq`），与直调服务层逐字段一致 |
| `tests/test_diff.py::test_ws_repo_diff_error_frame_carries_stable_code` | 坏 `repoId` → `ok:false` + `REPO_NOT_BOUND`（稳定码，不是 500） |
| `tests/test_bot_charter.py::test_ws_bot_frames_match_service` | S6 D11 #13：`bot.list` / `memory.charter.get` 帧往返，与直调服务层一致、`seq` 递增 |
