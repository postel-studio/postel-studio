# 03 · storage-db — `src/postel_studio/storage/db.py`

> 层 L0（**碰磁盘**）· 813 行 · 上游：[模块索引](README.md) · 相关：[06-data-model §2.1/§7](../../design/06-data-model.md) · [05-security §4.1](../../design/05-security.md)

## 职责

内核的本地状态库 `state.db`：连接、**schema 版本与迁移**、五张表的读写——
`repos`（S3）、`bots`（S6）、`tasks` 与 `worktree_leases`（S10）、`candidates`（S11）。

- **只写在状态根**（`~/.postel/state.db`，`POSTEL_HOME` 覆盖）。**绝不**写进用户仓库（S3 禁止事项）。
- S10 的 `tasks.candidate_id` 列**先建、恒 NULL**（S11 才有值）——建表时就把列占住，S11 不用再迁移。
- 用**标准库 `sqlite3`**，不引入 ORM / 迁移框架（S3 DoD：不新增依赖）。

文件名的事实源在 `storage/paths.py:28/61`（`STATE_DB_FILE` / `state_db_file()`），本模块只是消费者。

## 符号表

| 符号 | 行 | 做什么 | 谁在用 |
|---|---|---|---|
| `SCHEMA_VERSION` | 44 | `6`（v1 = S3 初始、v2 = S3.5 `origin_hash`、v3 = S6 `bots`、v4 = S10 `tasks` + `worktree_leases`、v5 = S11 `candidates`、v6 = S13 `candidates.reject_reason`） | `migrate`、测试 |
| `_MIGRATIONS` | 52 | 迁移 DDL 元组，**索引 = 版本号**，只往后 append | `migrate` |
| `_COLUMNS` | 149 | 读路径的列清单（**只写一份**，四处 SELECT 共用） | `list` / `get` / `upsert` |
| `state_db_path()` | 152 | `state.db` 的路径（不创建） | `RepoStore.path`、测试 |
| `_connect(path)` | 157 | 打开连接 + `row_factory` + `foreign_keys=ON` + `journal_mode=WAL` | `open_state_db` |
| `migrate(conn, *, path_for_chmod)` | 170 | 按 `PRAGMA user_version` 逐版升级，每版一个事务；顺手收紧 `0600` | `open_state_db` |
| `open_state_db(path=None)` | 185 | 建目录 → 打开 → 迁移；失败关连接再抛 | `RepoStore` |
| `RepoStore` | 201 | `repos` 表的读写门面（带锁） | `api/app.py`、`api/repository.py` |
| `RepoStore.__init__(path=None)` | 209 | 打开库；`path=None` 表示用状态根 | `app.create_app` |
| `RepoStore.path`（property） | 215 | 这份 store 打开的库文件路径（只读） | 测试 |
| `RepoStore.close()` / `__enter__` / `__exit__` | 220/225/228 | 关闭；`with` 支持 | 测试、将来的 server 收场 |
| `RepoStore.list()` | 232 | 全部仓库，`ORDER BY added_at, repo_id`（**输出确定**） | `repo.list` |
| `RepoStore.get(repo_id)` | 240 | 按 id 取，没有给 `None` | `repo.get`（S4 入口） |
| `RepoStore.upsert(repo)` | 250 | 绑定写库：同路径**不产生第二行**，且**不改写已有 `repo_id`**；返回落库后的记录 | `repo.bind` |
| `_row_to_repo(row)` | 322 | `sqlite3.Row` → `Repo`，类型收敛只在这一处 | 内部 |
| `open_store(path=None)` | 342 | `with` 版工厂（异常路径也关连接） | 暂无生产调用方（测试与将来的 CLI） |
| `_BOT_COLUMNS` | 352 | `bots` 的列清单（同 `_COLUMNS` 的理由：只写一份） | `BotStore` |
| `BotStore` | 355 | `bots` 表的读写门面（带锁）：`list_for_repo` / `get` / `insert` / `set_permission`（S8） | `api/app.py`、`api/bots.py` |
| `BotStore.list_for_repo(repo_id)` | 377 | 某仓库全部 Bot，`ORDER BY created_at, bot_id`（确定性） | `bot.list` |
| `BotStore.get(bot_id)` | 386 | 按复合主键取，没有给 `None` | `memory.charter.get`、`orchestrator._channel_for` |
| `BotStore.insert(bot)` | 394 | 插入一行；重复主键抛 `IntegrityError`（**不静默覆盖**） | `api/bots.py:create`（S8 起；S6 的播种已删） |
| `BotStore.set_permission(bot_id, permission)` | 411 | **S8**：只改 `permission` 一列并回读；行不存在 → `None`（码由调用方翻） | `bot.set_permission` |
| `_row_to_bot(row)` | 432 | `sqlite3.Row` → `Bot`（类型收敛只在这一处） | 内部 |
| `TaskStore` | 459 | **S10**：`tasks` 表的读写门面（带锁）：`insert` / `get` / `active_for_bot` / `latest_for_bot` / `transition` / `mark_interrupted` | `orchestrator.py`、`api/app.py` |
| `TaskStore.insert(task)` | 482 | 插入 queued 行；撞 `tasks_active_per_bot` 索引抛 `IntegrityError`（BUSY 的**数据级后备锁**，D5） | `orchestrator.dispatch` |
| `TaskStore.get(task_id)` | 504 | 按主键取，没有给 `None` | `orchestrator.status` / `cancel` / `_finish` |
| `TaskStore.active_for_bot(bot_id)` | 512 | 该 Bot 的活跃任务（`state IN ('queued','running')`），没有给 `None` | `orchestrator.dispatch` 的 BUSY 预检 |
| `TaskStore.latest_for_bot(bot_id)` | 522 | **S15 D8**：该 Bot 最近一条任务（含终态），`ORDER BY created_at DESC, task_id DESC LIMIT 1`；页面重连恢复中断态 | `orchestrator.latest` |
| `TaskStore.transition(task_id, state, error_code=None)` | 535 | **唯一的迁移入口**（D6）：`UPDATE … WHERE state=<from>` 原子完成，竞态输家拿 `None`；非法迁移直接 `ValueError`（状态机是表的守卫，先于 SQL） | `orchestrator` 全部状态变化 |
| `TaskStore.mark_interrupted()` | 579 | **S10 D7**：把库里遗留的活跃任务（queued + running）标 `interrupted`；返回行数。构造 `Orchestrator` 时调一次 | `Orchestrator.__init__`（重启记账） |
| `LeaseStore` | 596 | **S10**：`worktree_leases` 表的读写门面：`insert` / `active_for_task` / `release` | `orchestrator._run` |
| `LeaseStore.insert(lease)` | 619 | 插入租约；撞 `leases_active_per_bot` 索引抛 `IntegrityError`（每 Bot 活跃租约唯一） | `orchestrator._run` |
| `LeaseStore.active_for_task(task_id)` | 634 | 只读：这个任务的租约还活跃吗（测试轮询用） | `orchestrator`、测试 |
| `LeaseStore.release(task_id)` | 644 | `released_at = now`（只对活跃行）；返回是否真的释放了 | `orchestrator._run` 的 `finally` |
| `_row_to_task(row)` / `_row_to_lease(row)` | 655/681 | `sqlite3.Row` → `Task` / `WorktreeLease`（类型收敛只在这一处） | 内部 |

## 关键决策与理由

### schema 演进：v1 → v2（S3.5）→ v3（S6）→ v4（S10）

```sql
-- v1（S3）
CREATE TABLE repos (
    repo_id        TEXT PRIMARY KEY,   -- S3: sha256(origin URL | path)；S3.5 起: sha256(path)
    path           TEXT NOT NULL UNIQUE,
    origin_url     TEXT,
    default_branch TEXT,
    added_at       REAL NOT NULL
);
-- v2（S3.5，只加一列；**不重写任何已有 repo_id**）
ALTER TABLE repos ADD COLUMN origin_hash TEXT;
```

- `path UNIQUE` 是**验收 4**（重复绑定不产生重复行）的落点，不是可选项。
- `origin_url` / `default_branch` 允许 `NULL`：无 origin 是常态；裸仓库的 HEAD 可能探测不到。
- `origin_hash` 允许 `NULL`：没 origin 的行就是 NULL；老行（v1 绑的）迁移**不回填**。
  它**不参与任何主键/外键**，只用于识别"这两个绑定是不是同一个远端仓库"。
- 没有 `name` 列：06 §2.1 就没有，`name` 由 `Repo.name` 从路径派生。

```sql
-- v3（S6 D7，只加表、**不动 repos**）
CREATE TABLE bots (
    bot_id      TEXT PRIMARY KEY,          -- {repo_id}/{name}（06 §2.2）
    repo_id     TEXT NOT NULL REFERENCES repos(repo_id) ON DELETE CASCADE,
    name        TEXT NOT NULL,
    permission  TEXT NOT NULL DEFAULT 'L1',
    agent_kind  TEXT,                      -- NULL = 未选择（M1 就是 NULL）
    charter_rev INTEGER NOT NULL DEFAULT 0,-- 先建列；M1 不维护（D5，S22 才写）
    created_at  REAL NOT NULL,
    UNIQUE (repo_id, name)
);
```

- `charter_rev` **M1 不维护**：charter 正文的事实源是文件（D5），载荷里的 `charterRev` 现场
  由 `api/bots.py` 按内容算；这一列只记播种那一刻的值（U17，到期 S22）。
- `UNIQUE (repo_id, name)` 是 06 §2.2「同一 repo 下 name 唯一」的落点；`bot_id` 是复合主键，
  **天然按仓库隔离**。
- 迁移只 `CREATE TABLE`，老库（v2）升级后 `repos` 数据原样还在（D11 #12 的真库测试）。

```sql
-- v4（S10 D5，一次建齐两表 + 两个部分唯一索引）
CREATE TABLE tasks (
    task_id      TEXT PRIMARY KEY,
    bot_id       TEXT NOT NULL REFERENCES bots(bot_id) ON DELETE CASCADE,
    trigger      TEXT NOT NULL,
    trigger_key  TEXT,
    prompt       TEXT NOT NULL,
    base_ref     TEXT,
    state        TEXT NOT NULL,
    candidate_id TEXT,              -- 先建、S10 恒 NULL（S11 才有值）
    error_code   TEXT,
    created_at   REAL NOT NULL,
    started_at   REAL,
    ended_at     REAL
);
CREATE UNIQUE INDEX tasks_active_per_bot ON tasks (bot_id) WHERE state IN ('queued', 'running');
CREATE UNIQUE INDEX tasks_trigger_key   ON tasks (bot_id, trigger, trigger_key) WHERE trigger_key IS NOT NULL;
CREATE TABLE worktree_leases (
    lease_id    TEXT PRIMARY KEY,
    bot_id      TEXT NOT NULL REFERENCES bots(bot_id) ON DELETE CASCADE,
    task_id     TEXT NOT NULL UNIQUE REFERENCES tasks(task_id) ON DELETE CASCADE,
    path        TEXT NOT NULL,
    created_at  REAL NOT NULL,
    released_at REAL
);
CREATE UNIQUE INDEX leases_active_per_bot ON worktree_leases (bot_id) WHERE released_at IS NULL;
```

- **两个部分唯一索引是 BUSY 的数据级后备锁**（S10 D5）：orchestrator 的「查活跃 → 插入」
  是两步，两个并发 dispatch 都可能通过检查；`tasks_active_per_bot` 让第二个 `INSERT`
  当场抛 `IntegrityError` → `BUSY`。检查只是**提前给出友好错误**，正确性靠索引。
  反向锁：`tests/test_reverse_checks.py` 的 S10 #2（**组合注入**：删掉 orchestrator 的
  活跃检查 + 删掉索引，BUSY 测试必须红——实测只删检查时索引会兜住，测试保持绿，所以注入必须两处一起）。
- `candidate_id` 先建列：S11 加列又是一版迁移，能占住就占住；恒 `NULL` 不撒谎。
- `leases_active_per_bot` 照 06 §2.5 原文：一个 Bot 同时最多一个活跃工作区租约。

### `TaskStore.transition` 必须同时拿锁**和**连接（T44 的实测教训）

第一版只写了 `with self._lock`，忘了 `self._conn`——`UPDATE` 开的写事务**永不提交**，
其它连接的任何写操作都 `database is locked`。症状极隐蔽：测试单跑全绿、真实服务里
第一个任务之后所有写都冻住。修法是 `with self._lock, self._conn:`（连接的 with 语义
= 提交/回滚事务）。**通用**：sqlite3 的 `Connection` 上下文管理器管事务，不管连接的
生命周期；手写锁管并发，也**替代不了**事务边界。

**v2 迁移的措辞**：`_MIGRATIONS` 只 append 一条 `ALTER TABLE`。**不要**在这里加
`UPDATE repos SET repo_id = …`——D3 明确不重写历史 id（详见 08 §3）。

### `upsert` 不用单条 `INSERT … ON CONFLICT`（**被推翻的方案**）

第一版写的是：

```sql
INSERT … ON CONFLICT(repo_id) DO UPDATE SET path=… 
        ON CONFLICT(path)  DO UPDATE SET origin_url=…
```

**实测它不更新 `repo_id`**（06-pitfalls T26）：SQLite 的 upsert 遇到次级唯一键冲突时只执行
`DO UPDATE SET` 里列出的列，让 `repo_id` 保持旧值。后果是"仓库加了 origin 之后重绑"——
id 变成 URL 哈希了，但库里仍是路径哈希，**界面与本刀算出的 id 对不上**（测试
`test_rebinding_after_origin_added_updates_the_row` 当场抓到）。

现版本是一个事务里的三步：按 `path` 命中 → `UPDATE`（**不含 `repo_id`**，S3.5 D1）；
否则按 `repo_id` 命中 → `UPDATE`（含 `path`，仓库移动/改名）；都没有 → `INSERT`。

> **S3.5 改了这一条**：S3 时代"含 `repo_id`"，因为那时 id 依赖 origin、靠这步追平；现在
> id 只看路径、写后不变，**再写它就是 U9 缺陷本身**（加 remote → id 变）。
> 只改 SQL 不改参数元组会红在 `ProgrammingError` 上，见 `tests/test_reverse_checks.py`
> 的 `_RUNTIME_BREAKAGES` 与 06-pitfalls T32。

### `PRAGMA user_version` 迁移：只增不减、可重入

06 §7 的约定。`_MIGRATIONS` 是元组，新增一版就是往尾部 append 一段 DDL；**不要改历史条目**
（已有用户的库不会重跑旧版本，改了等于对老库撒谎）。

### 并发：一把 `threading.Lock` + `check_same_thread=False`

网关的 `dispatch()` 跑在 uvicorn 的线程池里（同步函数），多个 `repo.*` 请求可能并发。
`sqlite3` 的 `threadsafety=3` 允许跨线程用同一个连接，但**并发**访问必须串行化 → 每个公开
方法都在锁里。这把锁只保护**本连接**，不做跨进程互斥（单实例由 `kernel.lock` 保证）。

### 权限：`0600` + 目录 `0700`

05 §4.1。`sqlite3.connect` 建出来的文件受 umask 影响（可能是 `0644`），所以 `migrate()` 在
第一次打开后显式收紧。测试 `test_data_survives_reopening_the_store` 断言 `0600`。
`journal_mode=WAL` 会额外产生 `state.db-wal` / `state.db-shm`——它们是库的一部分，不单独登记。

### 为什么没有 `repo.unbind` 的实现

07 §2.2 有 `repo.unbind` 方法，但 **S3 的验收与涉及文件表里都没有它**，且 06 §5「永不自动删除
用户文件」的精神同样适用于绑定关系（删一行 = 用户要重新粘一次路径）。S3 **不做**，留给需要的切片。
`RepoStore` 因此只有 `list` / `get` / `upsert` 三个动作。

## 修改须知

| 如果你要… | 还波及 |
|---|---|
| 加表 / 改列 | `_MIGRATIONS` **append** 一版 + `SCHEMA_VERSION` +1 + `_COLUMNS`（若新列要读）；**必须**补一条"手工建旧库 → migrate → 断言数据未变"的真库测试（v2/v3/v4 就是这么测的） |
| 改 `bots` 表 | `api/bots.py` 的 8 字段载荷 + `tests/test_bot_charter.py` + 06 §2.2；`charter_rev` 的写路径属于 S22 |
| 改 `tasks` / `worktree_leases` | 06 §2.3 / §2.5 + `orchestrator.py` + `tests/test_dispatch.py`；状态机的事实源在 `domain/models.py` 的 `TASK_TRANSITIONS` |
| 改 `upsert` 的冲突规则 | 验收 4（重复绑定一行）+ `test_rebinding_*` + `test_double_bind_is_idempotent_row_and_id` |
| 把 `repo_id` 加回规则 1 的 `SET` | **禁止**（S3.5 D1 / U9）；反向锁是 `test_each_half_of_u9_alone_*` + 组合注入 |
| 换回 `INSERT … ON CONFLICT` | **不要**，见 T26：它不会更新次级唯一键对应的列 |
| 让 `state.db` 落在别处（例如仓库里） | **禁止**（S3 禁止事项）；文件名的事实源在 `paths.py` |
| 引入 SQLAlchemy / alembic | **禁止**（S3 DoD：不新增依赖） |
| 把 `sqlite3.Row` 漏到本模块以外 | **不要**：类型收敛在 `_row_to_repo`，帧层只该看到 `Repo` |
| 在 `transition` 之外写 `state` 列 | **禁止**（S10 D6）：状态机迁移必须原子，竞态输家拿 `None` 而不是覆盖 |

## 测试

| 测试 | 守什么 |
|---|---|
| `tests/test_bind.py::test_data_survives_reopening_the_store` | 真文件持久化 + `0600` |
| `tests/test_bind.py::test_schema_is_versioned_and_migration_is_reentrant` | `user_version` + 重复 migrate |
| `tests/test_bind.py::test_rebinding_same_path_yields_same_id_and_single_row` | 验收 4（含直接 `SELECT COUNT(*)`） |
| `tests/test_bind.py::test_v1_database_upgrades_to_v2_without_rewriting_any_repo_id` | **真库迁移**（v1 DDL 手工建库 → migrate → id 一个没变、`user_version=SCHEMA_VERSION`；S3.5 时是 2，S6 起 3） |
| `tests/test_bind.py::test_migration_is_reentrant_and_a_second_open_changes_nothing` | 迁移可重入（内容摘要相同） |
| `tests/test_bind.py::test_binding_an_existing_v1_row_keeps_its_legacy_id` | D3 的留痕：老行 id 不被下一次 bind 改写 |
| `tests/test_bind.py::test_rebinding_after_origin_added_updates_the_row` | D1 回归锁（id **不**被更新） |
| `tests/test_bind.py::test_bind_writes_nothing_into_the_user_repository` | 只写状态根 |
| `tests/test_bind.py::test_list_returns_array_payload_with_camel_case_keys` | 落盘 `snake_case` / 帧层 `camelCase` |
| `tests/test_bot_charter.py::test_v2_database_upgrades_to_v3_and_keeps_repos` | S6 D11 #12：真库 v2→v3，repos 行原样 |
| `tests/test_dispatch.py::test_illegal_transitions_are_refused` | S10 D6：状态机表拒绝非法迁移 |
| `tests/test_dispatch.py::test_taskstore_transition_loses_race_returns_none` | T44 的镜像：transition 原子、竞态输家拿 `None` |
| `tests/test_dispatch.py::test_restart_marks_running_and_queued_interrupted` | D7：重启把遗留活跃任务标 `interrupted` |
| `tests/test_dispatch.py::test_second_dispatch_while_active_is_busy` | D5：BUSY（索引兜底，反向注入 S10 #2） |


### v5（S11）：候选表与 CandidateStore

```sql
CREATE TABLE candidates (
    candidate_id TEXT PRIMARY KEY, task_id TEXT NOT NULL UNIQUE REFERENCES tasks(task_id) ON DELETE CASCADE,
    branch TEXT NOT NULL UNIQUE, base_sha TEXT NOT NULL, head_sha TEXT NOT NULL,
    files_changed INTEGER NOT NULL, insertions INTEGER NOT NULL, deletions INTEGER NOT NULL,
    review_state TEXT NOT NULL, remote TEXT, target TEXT, created_at REAL NOT NULL
);
CREATE INDEX candidates_review_created ON candidates (review_state, created_at DESC);
```

- `tasks.candidate_id` **v4 就建好了**，v5 只补表；`task_id` UNIQUE = 一任务至多一条候选。
- `repo_id` 不冗余存：列表按 `tasks → bots` join 得到（06 §2.4 没有这一列）。
- diff 文本与 stat **不落库**（06 §8）：三个数字只是显示/排序快照。
- `CandidateStore.insert` / `get` / `for_task` / `list_for_repo` / `discard`：写路径必须
  `with self._lock, self._conn`（T44）；`discard` 只补偿删本 worker 刚插入的行，
  不做通用删除 API（06 §2.4：候选一旦被人看见就不该被内核删）。


### v6（S13）：拒绝理由 + 原子迁移

```sql
ALTER TABLE candidates ADD COLUMN reject_reason TEXT;
```

`CandidateStore.transition(candidate_id, target, *, reject_reason=None)`：
`UPDATE … SET review_state = ?, reject_reason = ? WHERE candidate_id = ? AND review_state = ?`，
与 `TaskStore.transition` 同一纪律（T44：锁 + 连接一起拿）；竞态输家回 `None`，
不覆盖赢家写入的理由。
