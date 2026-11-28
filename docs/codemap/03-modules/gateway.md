# 03 · gateway — `src/postel_studio/api/gateway.py`

> 层 L3 · 327 行 · 上游：[模块索引](README.md) · 协议：[02b-protocol](../../design/02b-protocol.md) · 分派对象：[repository](repository.md) · [orchestrator](orchestrator.md)

## 职责

WebSocket 网关：**认证 → 接受 → 分派 → 回帧**。

## 符号表

| 符号 | 行 | 做什么 | 谁在用 |
|---|---|---|---|
| `GATEWAY_PATH` | 42 | `"/gateway"`，路径前缀 | `server.gateway_path()` |
| `TICKET_TTL_SECONDS` | 43 | `60.0` | `TicketRegistry` |
| `CLOSE_FRAME_INVALID` | 45 | `4002` 关闭码 | `handler` |
| `SUBSCRIBE_METHOD` | 49 | **S10**：`"subscribe"`——连接级方法，**不进 `dispatch()`**（特判在 `handler` 里，D11） | `handler` |
| `TicketRegistry` | 52 | 一次性 ticket 签发/消费 | `app.create_app` |
| `ConnectionState` | 85 | 每连接的 `seq` 计数器（D4） | `handler` |
| `Providers` | 94 | frozen dataclass，注入依赖（S3~S9 逐刀加的字段 + S10 的 `task_dispatch` / `task_status` / `task_cancel`，共 23 个注入字段（含 `health`）；S15 加 `task_latest`，S16 加 `audit_list`，S17 加 `memory_context_preview`） | `app.create_app` |
| `EventHubLike` | 131 | **S10**：编排器 `EventHub` 的形状（Protocol）；网关不反向 import 编排器，T14 的同一纪律 | `install(..., hub=…)` |
| `active_connection_count()` | 146 | 活动连接数（**仅供测试观测**） | `test_gateway_inprocess.py` |
| `dispatch(method, params, providers)` | 152 | 方法表（**23 个分支**（不含 `system.echo` 自检；含则为 24，可用 `grep -c '    if method == '` 核对）：S10 加了 `task.dispatch` / `task.status` / `task.cancel`，S15 加 `task.latest`，S16 加 `audit.list`，S17 加 `memory.context.preview`） | `handler` |
| `_topics_of(params)` | 205 | **S10**：解析 `subscribe` 的 `topics`（白名单 = `frames.KNOWN_TOPICS`，非空；不合法回 `None`） | `handler` 的 subscribe 分支 |
| `install(app, path, tickets, providers, hub=None)` | 222 | 把 WS 路由挂到 app；**S10 加了 `hub` 参数**（缺省 `None` 时 `subscribe` 回 `UNKNOWN_METHOD`，S2~S9 行为不变） | `app.create_app` |
| `handler` 内的 `pump()` | 256 | **S10**：每连接的事件泵 task——从队列取 evt、经 `send_lock` 发出 | `handler` |
| `handler` 内的 `send_frame()` | 262 | **S10**：经 `send_lock` 发一帧（res 与 evt 串行化，两个协程并发写同一条 WS 不保证原子） | `handler` |
| `_ACTIVE` / `_ACTIVE_LOCK` | 80-81 | 活动连接表（模块级） | 内部 |

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

### `dispatch()` 现在有 23 个分支（不含 `system.echo`；含则为 24），但每一行都是**转手**（S10/S15/S16）

```python
# api/gateway.py:152-202（真实代码的形状，别把业务写进来）
if method == "system.echo":
    return params
if method == "system.health":  # S2
    return providers.health()
if method in {"repo.bind", "repo.list"}:  # S3
    ...
if method == "repo.branches":  # S4
    ...
if method == "repo.diff":  # S5
    ...
if method in {"bot.list", "memory.charter.get"}:  # S6
    ...
if method in {"bot.create", "bot.set_permission"}:  # S8
    ...
if method in {"worktree.acquire", "worktree.list", "worktree.release"}:  # S7
    ...
if method in {"agent.list", "agent.capabilities"}:  # S9
    ...
if method in {"task.dispatch", "task.status", "task.latest", "task.cancel"}:  # S10 / S15
    ...
if method == "audit.list":  # S16
    ...
if method in {"candidate.list", "candidate.approve", "candidate.reject"}:  # S11 / S13
    ...
raise frames.FrameError(frames.UNKNOWN_METHOD, ...)
```

（真实代码里每组是**独立的 `if`**、各做一次 `providers.X(params)` 转手；这里压成组只为读得动。）

`if` 链已经远超当初「6~8 个就换 dict」的阈值，但 S10 的裁决仍然是「本刀只加三行、
不顺手重构」（预算纪律）：23 行**每行只做一次字段访问**，重构的收益是省十几行，
风险是动到全部既有方法的回归面。留给真正需要方法元数据（权限表、参数 schema）的切片。

### `subscribe` 是连接级方法，不进 `dispatch()`（S10 D11）

订阅状态**属于连接**（`sub_handle`），不属于方法表——`dispatch()` 的签名里没有「这条连接」，
塞进去就得为它造全局状态。所以 `handler` 里特判一个 `SUBSCRIBE_METHOD`：

- 不合法的 topics → `INVALID_PARAMS`（白名单 = `frames.KNOWN_TOPICS`，空集合也不合法）；
- 重复 subscribe = **整体替换**（`hub.replace`），不是叠加；
- 应答载荷是 `{"topics": [...已排序...]}`，让客户端知道现在订的是什么。

`hub=None`（`install` 缺省）时 `subscribe` 回 `UNKNOWN_METHOD`——保持 S2~S9 的行为，
测试与旧客户端不受影响；生产装配总传。

### 事件泵 + 发送锁（S10 D11）

`handler` 起一个 `pump()` task：编排器线程经 `EventHub` 把 evt 投进**这条连接**的
`asyncio.Queue`，泵取出来用 `encode_event` 发出。**`send_lock` 串行化所有 `send_text`**：
主循环发 res、泵发 evt，两个协程并发写同一条 WebSocket 在 ASGI 层不保证原子，帧交错
就是协议灾难。evt 的 `seq` 与 res 共用同一个 `ConnectionState` 计数器（客户端的丢帧
检测靠它跨帧连续）。

`finally` 里三条清理缺一不可：`pumper.cancel()`、`hub.unsubscribe(sub_handle)`
（**订阅随连接死**，不保存任何订阅状态——02b §2.3）、活动表删除。

> ⚠️ `test_unknown_method_is_unknown_method_not_subscribe` 的反例清单随实现推进：
> S3 时是 `bot.list` 等，S10 实现三个 task 方法后反例是 `task.list` / `candidate.list`
> （仍属 07 §2.4/§2.5、本刀没做的方法）。**每实现一个方法，都要把它从那条反例清单里
> 拿掉**，否则测试红得莫名其妙。`system.subscribe` 这个 07 §2.1 的**设计名**最终没有
> 落地——实现叫 `subscribe`（无前缀，02b §2.3 说它是连接级方法，与 `system.*` 的
> 请求级方法不同类）。

### `Providers` 注入消掉循环依赖

`gateway` 需要 `health()`，但 `app` import `gateway`，反向 import 会成环。
解法：`app` 把 `health` 作为 `Providers(health=health)` 传进来。

实测：把 `from postel_studio.api.app import health` 挪到 `gateway` 模块级 →
`ImportError: cannot import name 'health' from partially initialized module`。
**环是真的，但该消掉而不是绕开。**

## 修改须知

| 如果你要… | 还必须 |
|---|---|
| 加一个 WS 方法 | `dispatch()` 加分支 + `07-api-contract §2` 登记 + 测试 `UNKNOWN_METHOD` 反例；**它是连接级的**（如订阅类）则走 `handler` 特判，别进方法表 |
| 加一个推送 topic | `frames.KNOWN_TOPICS` append + `_topics_of` 白名单自动生效 + `07-api-contract §3` 表 + 编排器侧 `_publish` |
| 改 ticket 有效期 | `TICKET_TTL_SECONDS` + 05 §1.1.1 的文字 |
| 改错误码 | `frames.py` 常量 + `07-api-contract §1.3` 表 |
| 给网关加状态 | 想清楚是**全局**还是**每连接**；`seq` 是每连接，`_ACTIVE` 是全局，`sub_handle` 是每连接 |
| 让网关调用更多业务 | 新增 handler 模块，**别把业务写进 dispatch**；在 `Providers` 加一个字段 + `app.create_app()` 里注入 |
| 给 `Providers` 加字段 | `app.create_app()` 的 `gateway.Providers(...)` 是**唯一**构造点；`test_gateway_inprocess.py` 若自己构造 `Providers` 也要同步 |
| 动 pump / send_lock | 断言「res 与 evt 不交错」（顺序发帧）仍是硬约束；evt 的 `seq` 必须与 res 共用计数器 |

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
| `tests/test_dispatch.py::test_evt_reaches_subscriber_only` | S10 D11：`task.updated` 只投给订了的连接、未订阅者收不到 |
| `tests/test_dispatch.py::test_subscribe_replaces_and_validates_topics` | subscribe 重复 = 替换；白名单外 / 空集合 → `INVALID_PARAMS` |


### `candidate.list`（S11）

第 20 个方法分支，转手 `providers.candidate_list(params)`；`Providers` 增一个字段，
`app.create_app()` 是唯一构造点。订阅白名单 `KNOWN_TOPICS` 增加第二个 topic
`candidate.proposed`（载荷 `{candidateId, taskId}`，07 §3 早就登记了它）。


### S13：两个审核方法

方法表 19 → 21（`candidate.approve` / `candidate.reject`），`Providers` 增两个字段，
`app.create_app()` 是唯一注入点。`KNOWN_TOPICS` 增 `candidate.updated`（07 §3 早已登记）。
