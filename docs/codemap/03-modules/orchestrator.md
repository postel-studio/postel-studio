# orchestrator.py — 派活编排（S10，L3 纪律）

`src/postel_studio/orchestrator.py`（450 行）· 依赖：`agent/`、`storage/db`、`domain`、`api/frames` —— **不 import 任何同层 handler**（S10 D13）。`WorktreeService` 由 `app.py` 装配时注入（`WorktreeAcquirer` Protocol），环靠构造消掉而不是靠豁免。

## 为什么有这个模块

`02-architecture §2` 的 `task` Service：派活、状态机、去重。它不能是普通 L3 handler，因为
它**活得比一帧久**——dispatch 的应答是毫秒级的"已排队"，真正的工作发生在 worker 线程里
（S10 D1/D2，也是 U19 的收口：ACP 调用从此不在事件循环上）。

## 结构

| 符号 | 行 | 职责 |
|---|---|---|
| `EventHub` | 88 | 线程安全 pub/sub：`subscribe` / `replace` / `unsubscribe` / `publish`。工作线程经 `loop.call_soon_threadsafe` 投递（`asyncio.Queue` 本身不是线程安全的，D11） |
| `Orchestrator` | 148 | 三个帧方法 + worker。构造时 `mark_interrupted()`（D7：重启记账） |
| `dispatch` | 180 | 校验（prompt ≤ 32 KiB、baseRef 形状）→ `permission_covers(bot, L1)` → 活跃检查 → 建行 → 起线程（线程登记进 `_workers`）。`insert` 撞索引是 BUSY 的数据级后备（D5） |
| `wait_idle(timeout)` | 223 | join 全部活着的 worker（测试收场与 S15 优雅停机共用；worker 自己在 finally 末尾把自己从登记表摘除） |
| `status` | 237 | 按 id 取任务载荷 |
| `latest` | 242 | **S15 D8**：某 Bot 最近一条任务（含终态），`task.latest` 帧入口；没有回 `None` |
| `cancel` | 252 | queued 直接迁移；running 发会话取消并把迁移交给 worker（D10）；终态 → `INVALID_PARAMS` |
| `_run` | 274 | 一任务一线程：取工作区 → 建租约 → `queued→running` → 会话 → prompt → D9 映射 → finally（回收会话、释放租约、摘除线程登记） |

## 两条容易改坏的红线

1. **`_run` 的任何异常都要终态化**（D15）。任务已经发生了，把 worker 的异常静默吞掉等于
   任务永远悬在 `running`——重启后被标 `interrupted`，但那是对"进程死了"的记账，不是对
   "worker 崩了"的记账。`except Exception` 在这里是**故意的** fail-closed。
2. **`_publish` 不许在 finally 里做**。publish 自己抛错（loop 已死等）会让正在展开的栈
   再叠一层；终态发布放在 `_run` 的 try 之后（emit 是尽力语义，网关断连的连接由网关自己清理）。

## 与网关的边界

`subscribe` 是连接级方法，**不进本模块**也不进 `dispatch()`——它需要连接自己的队列与事件
循环，所以住在 `api/gateway.py` 里特判；topic 白名单的唯一事实源是 `api/frames.py` 的
`KNOWN_TOPICS`（L1），网关校验入参、本模块发布，两边读同一份。


### S11 候选接缝

构造参数新增 `candidates: CandidateMaker`（Protocol，不 import `candidates.py`）。
worker 在会话**之前**用 `snapshot_base(path)` 记下工作区 HEAD（D2），
`end_turn` 成功后走 `_succeed_with_candidate()`：无改动照旧 `succeeded`；有改动则候选行
先插、终态迁移带 `candidate_id` 后做，输掉 cancel 竞态就 `discard()` 刚插入的行（D6）；
迁移赢了才发 `candidate.proposed`。`task.status` 载荷多一个可选 `candidateId`。
