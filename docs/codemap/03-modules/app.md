# 03 · app — `src/postel_studio/api/app.py`

> 层 L3 · 79 行 · 上游：[模块索引](README.md)

## 职责

路由表。构造 FastAPI 应用，挂 3 个 HTTP 路由，并把网关挂上去。

## 符号表

| 符号 | 行 | 做什么 | 谁在用 |
|---|---|---|---|
| `API_VERSION` | 22 | `"1"`，协议版本 | `server._run` 写进 server.json；`test_server_boot.py` |
| `health()` | 25 | 内核存活应答 dict | `/api/health`、网关 `system.health`、测试 |
| `create_app(ws_path, host, port)` | 36 | 构造应用 | `server._run`、`test_gateway_inprocess.py` |

### 三个路由

| 路由 | 行 | 认证 | 返回 |
|---|---|---|---|
| `GET /` | 46 | 无 | HTML（每次渲染签发新 ticket） |
| `GET /api/health` | 60 | 无 | `{status, version, apiVersion, stateRoot, pid}` |
| `GET /api/where` | 73 | 无 | `{host, port, wsPath, pid}` |
| `WS /gateway/<sha16>` | 78（由 `gateway.install` 挂） | ticket | 帧 |

## 关键决策与理由

### 为什么 `/` 每次请求都重新渲染

S1 时页面是**常量**，只构造一次，避免每请求拼接。S2 起必须变：每次渲染要签发一个
**一次性 handshake ticket**。所以 `index()` 现在是函数而不是预渲染字符串。

**代价**：每个 `GET /` 会往 `TicketRegistry` 里塞一个 ticket（60 秒后过期）。
**不要**把这个渲染结果缓存起来——缓存会让所有客户端共用同一个 ticket，第二次连就 403。

### `health()` 是同步函数，且被两处共用

- `/api/health`（HTTP）
- 网关 `system.health`（帧）

网关要调用它，但 `gateway.py` **不能 import `app.py`**（会成环）。解决方式：
`app.py` 把 `health` 作为 `Providers(health=health)` **注入**给网关。

> S2 初版是让 `gateway.py` 在函数体内 import `app.health`。实测那个环确实存在，
> 但**环是因为分层错了**，正确做法是注入。见 [02-layers §3](../02-layers.md#3-同层-import-的唯一例外)。

### 为什么 `docs_url=None, redoc_url=None, openapi_url=None`

不暴露 `/docs`、`/redoc`、`/openapi.json`。本地工具不需要，且少三条未受保护的路径。
**加路由时不要顺手把 openapi 打开。**

### `/api/where` 为什么存在，以及为什么不读 `server.json`

浏览器读不到文件系统，所以内核必须**自己说出来**它绑在哪。
但**不能读文件**——`server.json` 可能已被下一个内核实例覆盖、或已删除。
所以 `host`/`port` 由 `server._run` 在 `create_app(ws_path, host=host, port=port)` 时**注入**。

`test_where_reports_this_kernels_real_binding` 专门**删掉 server.json 之后**再问一次，
两次结果相等——这是「不读文件」的证据，不是注释。

### 响应形状不一致（有意）

| 路由 | 形状 |
|---|---|
| `/api/health`、`/api/where` | **裸对象** `{...}` |
| WS 帧 `res` | 包络 `{data, seq}`（07 §1.2） |

**HTTP 路由用 HTTP 形状，WS 帧用帧形状。** 这个边界曾被写错：S2 初版前端按
`frame.data` 读 `/api/where`，导致**同端口重连也失败**，浏览器实测才发现。

> 结论：看到「不一致」先确认是不是有意的边界，别急着统一。**但新增 HTTP 路由时，
> 保持裸对象，别再引入第三种形状。**

## 修改须知

| 如果你要… | 还必须 |
|---|---|
| 加 HTTP 路由 | 需要认证吗？本地无认证是**有意的**（05 §1.3），但别回显敏感值 |
| 加 WS 方法 | **不要在 app.py 加**，去 `gateway.dispatch()` |
| 改 `health()` 字段 | `07-api-contract §2.1` + `test_gateway.py::test_health_roundtrip_matches_http_health` |
| 改 `API_VERSION` | 只有**破坏性**协议变更才改（07 §5）；改前先看客户端不匹配的处理 |
| 让 app 依赖更多 gateway 内部 | 走注入，别反向 |

## 测试

| 测试 | 守什么 |
|---|---|
| `test_page_shows_version_and_state_root` | `/` 内容 |
| `test_health_payload_contains_version_and_state_root` | `health()` 字段 |
| `test_gateway_inprocess.py::test_where_reports_this_kernels_real_binding` | `/api/where` 不读文件 |
| `test_gateway.py::test_page_carries_handshake_ticket_meta` | 页面里有 ticket meta |
| `test_gateway.py::test_page_does_not_carry_the_long_term_token` | 令牌不进页面 |