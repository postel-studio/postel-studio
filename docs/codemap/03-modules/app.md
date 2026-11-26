# 03 · app — `src/postel_studio/api/app.py`

> 层 L3 · 95 行 · 上游：[模块索引](README.md) · 相关：[repository](repository.md) · [storage-db](storage-db.md)

## 职责

路由表。构造 FastAPI 应用，挂 3 个 HTTP 路由，并把网关挂上去。

S3 起这里还负责**构造并注入**业务能力：`RepoStore` + `RepositoryService` → `gateway.Providers`。
**加了仓库能力，但一个 HTTP 路由都没加**（S3 D1：`repo.*` 只走 WS 帧）。

## 符号表

| 符号 | 行 | 做什么 | 谁在用 |
|---|---|---|---|
| `API_VERSION` | 22 | `"1"`，协议版本 | `server._run` 写进 server.json；`test_server_boot.py` |
| `health()` | 25 | 内核存活应答 dict | `/api/health`、网关 `system.health`、测试 |
| `create_app(ws_path, host, port)` | 38 | 构造应用 + 构造 store/service 并注入网关 | `server._run`、`test_gateway_inprocess.py` |
| （局部）`store = RepoStore()` | 52 | 打开 `state.db`（状态根下） | 注入链的起点 |
| （局部）`repositories = RepositoryService(store)` | 53 | `repo.bind` / `repo.list` / `repo.branches` 的实现 | `gateway.Providers` |
| `gateway.Providers(health=…, repo_bind=…, repo_list=…, repo_branches=…)` | 88-93 | 注入给网关（**唯一**构造点） | `gateway.install` |

### 三个路由

| 路由 | 行 | 认证 | 返回 |
|---|---|---|---|
| `GET /` | 46 | 无 | HTML（每次渲染签发新 ticket） |
| `GET /api/health` | 60 | 无 | `{status, version, apiVersion, stateRoot, pid}` |
| `GET /api/where` | 73 | 无 | `{host, port, wsPath, pid}` |
| `WS /gateway/<sha16>` | 93（由 `gateway.install` 挂） | ticket | 帧（`system.*` / `repo.*`） |

**S3 没有加路由。** `repo.bind` / `repo.list` 走 WS，`/api/repos` 是 404
（`test_bind.py::test_no_http_route_for_repositories` 守着）。

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

### S3：store 与 gitops 在 `create_app` 里构造、经 `Providers` 注入

```python
# api/app.py:52-53 —— 状态根下的库 + 业务对象
store = RepoStore()
repositories = RepositoryService(store)
# api/app.py:88-92 —— 注入给网关（唯一构造 Providers 的地方）
gateway.install(
    app,
    ws_path,
    tickets,
    gateway.Providers(
        health=health,
        repo_bind=repositories.bind,
        repo_list=repositories.list,
        repo_branches=repositories.branches,  # S4
    ),
)
```

- **为什么在这里**：`app` 是"知道全部能力"的装配点。`gateway` 不知道仓库存在，
  `repository` 不知道 WebSocket 存在，`db` 不知道帧存在——三条向下 import 边，没有环。
- **为什么不给网关传 `RepoStore`**：网关只需要"方法 → 函数"，传库会让它认识存储层，
  下一个切片又会往里塞业务。**只注入行为，不注入数据源。**
- **S4 只加了一个注入字段**（`repo_branches=repositories.branches`），没有新路由、没有新 store。
- **已知未决**：`RepoStore` 在这个函数里构造，但**没有地方关它**（进程退出时由 OS 回收；
  `state.db` 不会被删）。S3 不做退出收场，记在 [06 §已知未决](../06-pitfalls.md#已知未决问题) U10。
  `sqlite3` 的连接不关不会丢数据（WAL 会 fsync），但"有开必有关"这条纪律目前是破的。

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
| 加 WS 方法 | **不要在 app.py 加**，去 `gateway.dispatch()` + `Providers` 加字段 + 在这里注入 |
| 加 HTTP 路由 | **先问是不是该走 WS**（S3 D1）。仓库/任务这类业务数据一律走帧，HTTP 只服务页面与 `where`/`health` |
| 改 store 的构造位置 | 它决定了 `state.db` 落在哪个状态根；测试靠 `POSTEL_HOME` 隔离，别把路径写死 |
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
| `tests/test_bind.py::test_websocket_bind_then_list_returns_the_repo` | 注入链真的通了（端到端） |
| `tests/test_bind.py::test_no_http_route_for_repositories` | 没长出 `/api/repos` |