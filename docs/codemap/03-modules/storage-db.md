# 03 · storage-db — `src/postel_studio/storage/db.py`

> 层 L0（**碰磁盘**）· 376 行 · 上游：[模块索引](README.md) · 相关：[06-data-model §2.1/§7](../../design/06-data-model.md) · [05-security §4.1](../../design/05-security.md)

## 职责

内核的本地状态库 `state.db`：连接、**schema 版本与迁移**、`repos` 与 `bots`（S6）表的读写。

- **只写在状态根**（`~/.postel/state.db`，`POSTEL_HOME` 覆盖）。**绝不**写进用户仓库（S3 禁止事项）。
- S3 只有 `repos`；S6 加了第二张表 `bots`（v3）。Task / Candidate / WorktreeLease 仍是后续切片，
  **不要提前建表**（建错的表比没有表更难改回来）。
- 用**标准库 `sqlite3`**，不引入 ORM / 迁移框架（S3 DoD：不新增依赖）。

文件名的事实源在 `storage/paths.py:28/61`（`STATE_DB_FILE` / `state_db_file()`），本模块只是消费者。

## 符号表

| 符号 | 行 | 做什么 | 谁在用 |
|---|---|---|---|
| `SCHEMA_VERSION` | 32 | `3`（v1 = S3 初始、v2 = S3.5 `origin_hash`、v3 = S6 `bots`） | `migrate`、测试 |
| `_MIGRATIONS` | 40 | 迁移 DDL 元组，**索引 = 版本号**，只往后 append | `migrate` |
| `_COLUMNS` | 78 | 读路径的列清单（**只写一份**，四处 SELECT 共用） | `list` / `get` / `upsert` |
| `state_db_path()` | 81 | `state.db` 的路径（不创建） | `RepoStore.path`、测试 |
| `_connect(path)` | 86 | 打开连接 + `row_factory` + `foreign_keys=ON` + `journal_mode=WAL` | `open_state_db` |
| `migrate(conn, *, path_for_chmod)` | 99 | 按 `PRAGMA user_version` 逐版升级，每版一个事务；顺手收紧 `0600` | `open_state_db` |
| `open_state_db(path=None)` | 114 | 建目录 → 打开 → 迁移；失败关连接再抛 | `RepoStore` |
| `RepoStore` | 130 | `repos` 表的读写门面（带锁） | `api/app.py`、`api/repository.py` |
| `RepoStore.__init__(path=None)` | 138 | 打开库；`path=None` 表示用状态根 | `app.create_app` |
| `RepoStore.path`（property） | 128 | 这份 store 打开的库文件路径（只读） | 测试 |
| `RepoStore.close()` / `__enter__` / `__exit__` | 133/138/141 | 关闭；`with` 支持 | 测试、将来的 server 收场 |
| `RepoStore.list()` | 161 | 全部仓库，`ORDER BY added_at, repo_id`（**输出确定**） | `repo.list` |
| `RepoStore.get(repo_id)` | 169 | 按 id 取，没有给 `None` | `repo.get`（S4 入口） |
| `RepoStore.upsert(repo)` | 179 | 绑定写库：同路径**不产生第二行**，且**不改写已有 `repo_id`**；返回落库后的记录 | `repo.bind` |
| `_row_to_repo(row)` | 251 | `sqlite3.Row` → `Repo`，类型收敛只在这一处 | 内部 |
| `_BOT_COLUMNS` | 281 | `bots` 的列清单（同 `_COLUMNS` 的理由：只写一份） | `BotStore` |
| `BotStore` | 284 | `bots` 表的读写门面（带锁）：`list_for_repo` / `get` / `insert` / `set_permission`（S8） | `api/app.py`、`api/bots.py` |
| `BotStore.list_for_repo(repo_id)` | 306 | 某仓库全部 Bot，`ORDER BY created_at, bot_id`（确定性） | `bot.list` |
| `BotStore.get(bot_id)` | 315 | 按复合主键取，没有给 `None` | `memory.charter.get` |
| `BotStore.insert(bot)` | 323 | 插入一行；重复主键抛 `IntegrityError`（**不静默覆盖**） | `api/bots.py:create`（S8 起；S6 的播种已删） |
| `BotStore.set_permission(bot_id, permission)` | 340 | **S8**：只改 `permission` 一列并回读；行不存在 → `None`（码由调用方翻） | `bot.set_permission` |
| `_row_to_bot(row)` | 361 | `sqlite3.Row` → `Bot`（类型收敛只在这一处） | 内部 |
| `open_store(path=None)` | 271 | `with` 版工厂（异常路径也关连接） | 暂无生产调用方（测试与将来的 CLI） |

## 关键决策与理由

### schema 演进：v1 → v2（S3.5）→ v3（S6）

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
| 加表 / 改列 | `_MIGRATIONS` **append** 一版 + `SCHEMA_VERSION` +1 + `_COLUMNS`（若新列要读）；**必须**补一条"手工建旧库 → migrate → 断言数据未变"的真库测试（v2/v3 就是这么测的） |
| 改 `bots` 表 | `api/bots.py` 的 8 字段载荷 + `tests/test_bot_charter.py` + 06 §2.2；`charter_rev` 的写路径属于 S22 |
| 改 `upsert` 的冲突规则 | 验收 4（重复绑定一行）+ `test_rebinding_*` + `test_double_bind_is_idempotent_row_and_id` |
| 把 `repo_id` 加回规则 1 的 `SET` | **禁止**（S3.5 D1 / U9）；反向锁是 `test_each_half_of_u9_alone_*` + 组合注入 |
| 换回 `INSERT … ON CONFLICT` | **不要**，见 T26：它不会更新次级唯一键对应的列 |
| 让 `state.db` 落在别处（例如仓库里） | **禁止**（S3 禁止事项）；文件名的事实源在 `paths.py` |
| 引入 SQLAlchemy / alembic | **禁止**（S3 DoD：不新增依赖） |
| 把 `sqlite3.Row` 漏到本模块以外 | **不要**：类型收敛在 `_row_to_repo`，帧层只该看到 `Repo` |

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
