# 01 · 系统全景

> 上游：[README.md](README.md) · 相关：[02-layers](02-layers.md) · [03/server](03-modules/server.md)

---

## 1. 现在系统里有什么

截至 S3（`v0.1.2`），Postel Studio 只有一个进程、一个页面、一条 WebSocket，
外加**一个 SQLite 文件（`state.db`）**与**只读的 git 子进程**。仍然**没有 Agent、没有任务调度**。
S3 的主题是「用户绑一个本地仓库，左栏认得它」。

```
┌──────────────────────────────────────────────────────────────────┐
│  单进程：postel-studio serve                    （前台，Ctrl+C 停） │
│                                                                  │
│  主线程 ────────────────────────────────────────────────────────  │
│   ⓪ 删掉上一轮 SIGKILL 残留的 server.json（看见文件=内核在听）    │
│   ① 抢锁 kernel.lock（O_EXCL 原子）                              │
│   ② bind(127.0.0.1, 0) → 内核自行选端口                          │
│   ③ 装信号处理器（SIGTERM/SIGINT）  ← 必须早于 ④                │
│   ④ 写 token 文件 + server.json                                  │
│   ⑤ uvicorn.run(sockets=[已监听的 sock])                        │
│        └─ asyncio 事件循环                                       │
│             ├─ HTTP: GET /            → 渲染页面（含一次性 ticket）│
│             ├─ HTTP: GET /api/health  → 版本/状态根                │
│             ├─ HTTP: GET /api/where   → 本进程真实绑定             │
│             └─ WS  : /gateway/<sha16> → 帧往返                   │
│                   ├─ repo.bind → git 子进程（只读探测）           │
│                   └─ repo.list → state.db（SQLite）               │
│   ⑥ finally：删 server.json、删 token、关 socket、删自己的锁      │
└──────────────────────────────────────────────────────────────────┘
                              ▲
                              │ 用户点开终端里打印的 http://127.0.0.1:PORT
                              │
              ┌───────────────┴────────────────┐
              │ 浏览器（页面内联 JS，无构建链）  │
              │  · 取页面 → meta 里拿到 ticket  │
              │  · new WebSocket(...?ticket=)  │
              │  · 心跳 system.echo 判活       │
              │  · 断开 → 变红 + 灰化 + 按钮   │
              │  · 左栏仓库区：绑定 / 列表    │
              └────────────────────────────────┘
```

## 2. 一次完整请求的路径

### 2.1 HTTP `GET /`（页面）

```
浏览器 → uvicorn → app.index()                        api/app.py:58
                      └─ gateway.TicketRegistry.issue()   api/gateway.py:52
                      └─ render_index(ticket=..., ws_path=...)  web/assets.py:1198
                   ← HTML（含 <meta name="postel-handshake">）
```

**每次渲染都签发一个新 ticket**，旧的不会立即作废（60 秒后过期）。

### 2.2 WebSocket 首连

```
浏览器 → new WebSocket("ws://127.0.0.1:PORT/gateway/<sha16>?ticket=XXX")
       → uvicorn WS 握手 → gateway.handler(ws)                api/gateway.py:136
            ① tickets.consume(ticket)  ── 失败 → ws.close(1008)（未 accept，客户端见 HTTP 403）
            ② ws.accept()
            ③ 登记到 _ACTIVE → finally 里删                （D5，可观测点）
            ④ 循环 receive_text()
```

### 2.3 一次帧往返（`system.echo`）

```
浏览器  socket.send({type:req, id:"c1", method:"system.echo", params:{...}})
   │
   ▼
gateway.handler
   ├─ len(text.encode()) > 1MiB ?        → close(4002)，不回首包     gateway.py:41
   ├─ frames.decode_incoming(text)       → ReqFrame 或 FrameError    frames.py:56
   │     · FrameError 且 reply_id is None → 记日志 + 忽略，不断连
   │     · FrameError 且有 reply_id      → 回 ok:false 帧
   ├─ gateway.dispatch(method, params, providers)                     gateway.py:111
   │     · "system.echo"   → 原样返回 params
   │     · "system.health" → providers.health()（由 app 注入）
   │     · "repo.bind"     → providers.repo_bind(params)
   │     · "repo.list"     → providers.repo_list()      ← 这两个也由 app 注入
   │     · 其它            → raise FrameError(UNKNOWN_METHOD)
   └─ state.take_seq()  → frames.encode_response(id, seq, data)
   ▼
浏览器  socket.onmessage → JSON.parse → 若 id === awaitingBeatId → 认为还活着
```

### 2.4 断连与重连

```
内核被杀 → 浏览器心跳失败（HEARTBEAT_INTERVAL_MS 内没回）
   → fail() → setState("disconnected","已断开")
             → 显示「重连」按钮
             → #content 加 .stale（灰化 + "数据已过期" 角标）
   → 用户点按钮 → reconnect()                                  assets.py:1163
       → freshTicket():
           fetch("/api/where") → 若 port != location.port → throw PORT_CHANGED
           fetch("/")         → 抠 meta ticket
       → connect(ticket) → ws.onopen → setState("connected")
```

### 2.5 `repo.bind` 的完整路径（S3）

```
浏览器 左栏「绑定」按钮 → request("repo.bind", {path})           assets.py:1074
   │
   ▼
gateway.dispatch → providers.repo_bind = RepositoryService.bind   repository.py:143
   ├─ _require_path(params)              非字符串/空 → INVALID_PARAMS
   ├─ Path(raw).is_absolute()            相对 / ~ → BAD_CWD
   ├─ Path(raw).resolve()                消 .. 与符号链接（D4）
   ├─ _bindable_root(target)             目标/.git? 裸仓库? 逐级向上找根? 否则 NOT_A_REPO
   │     └─ 子目录校正：/x/proj/src → /x/proj（D2）
   ├─ gitops.origin_url(root)            git config --get（无 origin → None）
   ├─ gitops.default_branch(root)        git symbolic-ref --short HEAD
   ├─ domain.repo_id(path)                sha256(绝对路径)[:12]，写后永不更新（S3.5 D1）
   ├─ domain.origin_hash(origin)          归一化 URL 的哈希；只识别"同一远端"（S3.5 D2）
   └─ RepoStore.upsert(repo)             同路径不产生第二行；**不改写已有 repo_id**（验收 4 / D1）
   ▼
浏览器  重读 repo.list → renderRepos() → 左栏一行「名称 / 默认分支」  assets.py:497
```

**这条路径上有三处"看起来对"**：git 的 discover（T25）、`ON CONFLICT` 的次级唯一键（T26）、
断连时的空列表（T27）——都记在 06，都是实测踩出来的。

### 2.6 断连时左栏怎么变（D1 的可观测后果）

```
fail()（唯一向下出口）→ setState("disconnected","已断开")
   ├─ 右上角灯变红、#content 灰化（S2）
   └─ #repos[data-state="disconnected"]
         ├─ #repo-state：「已断开：内核没有连着，仓库列表未知」
         ├─ #repo-list 与 #repo-empty 都 **隐藏**（空 ≠ 没连上）
         └─ 输入框与按钮禁用
重连成功 → setState("connected") → #repos[data-state="ready"] → loadRepos()
```

## 3. 关键数字（改这些要连带看别处）

| 常量 | 值 | 位置 | 为什么 |
|---|---|---|---|
| `MAX_FRAME_BYTES` | 1 MiB | `frames.py:24` | 02b §5 / 05 §3.1 |
| `MAX_ID_LENGTH` | 128 | `frames.py:25` | 05 §3.1 |
| `TICKET_TTL_SECONDS` | 60.0 | `gateway.py:39` | 一次性握手票有效期 |
| `CLOSE_FRAME_INVALID` | 4002 | `gateway.py:41` | 帧层违规的关闭码 |
| `GRACE_SECONDS` | 5 | `server.py:55` | uvicorn 优雅关闭上限 |
| `BUILD_SETTLE_SECONDS` | 0.2 | `server.py:56` | 建设期收到停止信号的等待 |
| `LISTEN_BACKLOG` | 2048 | `server.py:57` | |
| `HEARTBEAT_INTERVAL_MS` | 1000 | `assets.py:43` | 心跳间隔 |
| `RECONNECT_DELAY_MS` | 500 | `assets.py:44` | 点重连后取票的延迟 |
| `CONNECT_TIMEOUT_MS` | 2000 | `assets.py:363`（JS 内硬编码） | 见 [03/assets](03-modules/assets.md) |
| `DIR_MODE` / `FILE_MODE` | `0700` / `0600` | `paths.py:30-31` | 05 §4.1 |
| `GIT_TIMEOUT_SECONDS` | 5.0 | `gitops/repo.py:35` | D5：git 卡住不能拖住网关 |
| `SCHEMA_VERSION` | 3 | `storage/db.py:32` | 06 §7：`PRAGMA user_version`；v1 = S3，v2 = S3.5 加 `origin_hash`，v3 = S6 加 `bots` |
| `BOT_NAME_RE` | `^[a-z][a-z0-9-]{0,31}$` | `domain/models.py:139` | 06 §2.2 的 Bot 名字白名单（进路径前必须过，S6 D3） |
| `SUMMARY_MAX_CHARS` | 80 | `api/bots.py:44` | S6 D5：charter 摘要按字符截断（加 `…`） |
| `REPO_ID_LENGTH` | 12 | `domain/models.py:22` | 06 §2.1 / 07 §4.1 |

> `HEARTBEAT_INTERVAL_MS + RECONNECT_DELAY_MS < 3000` 是被测试断言的关系
> （S2 验收：断连 ≤3 秒可见）。改心跳间隔会连带打破这个预算。

## 4. 状态根长什么样

`$POSTEL_HOME`（缺省 `~/.postel`），目录 `0700`：

| 文件 | 所有者 | 生命周期 | 权限 | 内容 |
|---|---|---|---|---|
| `kernel.lock` | 内核 | 进程存活期间 | 0600 | `{pid, version}` |
| `state.db` | 内核 | **长期**（用户数据，进程退出不删） | 0600 | SQLite：`repos` + `bots` 表（v3，S6；另有 WAL 的 `state.db-wal` / `state.db-shm`） |
| `bots/` | 内核 | **长期**（用户数据） | 目录链 0700 / `${repo_id}/${name}/charter.md` 0600 | Bot 的记忆空间：`<repo_id>/<name>/{charter.md, memory/, sessions/}`。**只在状态根下，绝不写进用户仓库**（S6 D3 / T38） |
| `server.json` | 内核 | 进程存活期间 | 0600 | 见下 |
| `token` | 内核 | 进程存活期间 | 0600 | 32 字节 hex 明文 |

**`server.json` 字段**（`server.py:_run` 写入）：

```jsonc
{
  "host": "127.0.0.1",
  "port": 43813,          // 内核自行分配，每次启动可能不同
  "pid": 12345,
  "apiVersion": "1",      // 破坏性协议变更时递增
  "version": "0.1.2",
  "stateRoot": "/home/u/.postel",
  "wsPath": "/gateway/aeddb9231080161f",   // 由状态根 sha256 派生，重启不变（D7）
  "tokenFingerprint": "<sha256 of token>"  // 只放指纹，不放令牌本体
}
```

**S1/S2 的三个文件在正常退出时全部删除。** SIGKILL 时全部残留，下次启动会覆盖锁与 server.json。

**`state.db` 例外：它不删。** 它装的是用户数据（"我绑过哪些仓库"），删掉等于清空用户的绑定。
S3 也没有退出收场（连接不显式 close，靠进程退出回收；WAL 已 fsync，不丢数据），见
[06 §已知未决 U10](06-pitfalls.md#已知未决问题)。

## 5. 现在还没有的东西（别去猜）

| 没有 | 出现的切片 | 届时影响 |
|---|---|---|
| 其余业务方法（bot/task/candidate/memory/agent、`repo.unbind`） | S4~S13 | `dispatch()` 已从 4 个分支长到 11 个（S7 结束）→ 到十几个时应考虑改注册表 |
| Bot / Task / Candidate / WorktreeLease 表 | 后续切片 | 现在只有 `repos` 一张表；新表 append 一版迁移 |
| 订阅/topic 机制 | S10 前后 | `encode_event` 已存在但**从未被调用** |
| `seq` 的跨连接语义 | — | 现在每连接独立从 1 开始（D4） |
| ~~数据库~~ | **S3 已有**（`state.db`，只有 `repos`） | 后续加表要 append 一版迁移 |
| Agent 子进程/ACP | S9/S10 | 会引入第二个进程模型，本页的「单进程」图将失效 |
| 权限判定 | S16 | |
| 前端构建链 | 明确**不做** | `assets.py` 会持续变长，见 06 §前端 |

## 6. 读代码的最短路径

建议按这个顺序读，大约 20 分钟能通：

1. `src/postel_studio/__init__.py` — 55 行，看清 CLI 形状
2. `src/postel_studio/storage/paths.py` — 195 行，所有文件 IO 都在这里
3. `src/postel_studio/api/server.py` — 256 行，生命周期；**先读模块 docstring**
4. `src/postel_studio/api/app.py` — 79 行，路由表
5. `src/postel_studio/api/frames.py` — 121 行，纯函数，最好读
6. `src/postel_studio/api/gateway.py` — 150 行，网关
7. `src/postel_studio/domain/models.py` — 131 行，纯函数，S3.5 的 id / origin_hash 规则
8. `src/postel_studio/gitops/repo.py` — 197 行，**先读模块 docstring 的三条硬约束**
9. `src/postel_studio/storage/db.py` — 261 行，schema v2 + upsert
10. `src/postel_studio/api/repository.py` — 196 行，S3 的业务编排
11. `src/postel_studio/web/assets.py` — 465 行，页面 + 内联 JS（JS 从 `<script>` 开始看）
12. `tests/test_bind.py` — 一条真实的端到端：真实 git + 真实 SQLite + 真实 WebSocket
13. `tests/test_gateway_inprocess.py` — 看一个「真端口真连接」的测试怎么写
14. `tests/test_reverse_checks.py` — 反向验证（注入缺陷 → 已存在测试必须红），**先读文件头**