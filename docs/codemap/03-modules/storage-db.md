# 03 · storage-db — `src/postel_studio/storage/db.py`

> 层 L0（**碰磁盘**）· 226 行 · 上游：[模块索引](README.md) · 相关：[06-data-model §2.1/§7](../../design/06-data-model.md) · [05-security §4.1](../../design/05-security.md)

## 职责

内核的本地状态库 `state.db`：连接、**schema 版本与迁移**、`repos` 表的读写。

- **只写在状态根**（`~/.postel/state.db`，`POSTEL_HOME` 覆盖）。**绝不**写进用户仓库（S3 禁止事项）。
- 本刀**只有一张表** `repos`。Bot / Task / Candidate / WorktreeLease 是后续切片，**不要提前建表**。
- 用**标准库 `sqlite3`**，不引入 ORM / 迁移框架（S3 DoD：不新增依赖）。

文件名的事实源在 `storage/paths.py:26/59`（`STATE_DB_FILE` / `state_db_file()`），本模块只是消费者。

## 符号表

| 符号 | 行 | 做什么 | 谁在用 |
|---|---|---|---|
| `SCHEMA_VERSION` | 30 | `1`：当前 schema 版本 | `migrate`、测试 |
| `_MIGRATIONS` | 38 | 迁移 DDL 元组，**索引 = 版本号**，只往后 append | `migrate` |
| `state_db_path()` | 51 | `state.db` 的路径（不创建） | `RepoStore.path`、测试 |
| `_connect(path)` | 56 | 打开连接 + `row_factory` + `foreign_keys=ON` + `journal_mode=WAL` | `open_state_db` |
| `migrate(conn, *, path_for_chmod)` | 69 | 按 `PRAGMA user_version` 逐版升级，每版一个事务；顺手收紧 `0600` | `open_state_db` |
| `open_state_db(path=None)` | 84 | 建目录 → 打开 → 迁移；失败关连接再抛 | `RepoStore` |
| `RepoStore` | 100 | `repos` 表的读写门面（带锁） | `api/app.py`、`api/repository.py` |
| `RepoStore.__init__(path=None)` | 108 | 打开库；`path=None` 表示用状态根 | `app.create_app` |
| `RepoStore.path`（property） | 114 | 这份 store 打开的库文件路径（只读） | 测试 |
| `RepoStore.close()` / `__enter__` / `__exit__` | 119/124/127 | 关闭；`with` 支持 | 测试、将来的 server 收场 |
| `RepoStore.list()` | 131 | 全部仓库，`ORDER BY added_at, repo_id`（**输出确定**） | `repo.list` |
| `RepoStore.get(repo_id)` | 140 | 按 id 取，没有给 `None` | `repo.get`（S4 入口） |
| `RepoStore.upsert(repo)` | 151 | 绑定写库：同路径**不产生第二行**；返回落库后的记录 | `repo.bind` |
| `_row_to_repo(row)` | 202 | `sqlite3.Row` → `Repo`，类型收敛只在这一处 | 内部 |
| `open_store(path=None)` | 220 | `with` 版工厂（异常路径也关连接） | 暂无生产调用方（测试与将来的 CLI） |

## 关键决策与理由

### schema v1 只有一张表

```sql
CREATE TABLE repos (
    repo_id        TEXT PRIMARY KEY,
    path           TEXT NOT NULL UNIQUE,
    origin_url     TEXT,
    default_branch TEXT,
    added_at       REAL NOT NULL
);
```

- `path UNIQUE` 是**验收 4**（重复绑定不产生重复行）的落点，不是可选项。
- `origin_url` / `default_branch` 允许 `NULL`：无 origin 是常态；裸仓库的 HEAD 可能探测不到。
- 没有 `name` 列：06 §2.1 就没有，`name` 由 `Repo.name` 从路径派生。

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

现版本是一个事务里的三步：按 `path` 命中 → `UPDATE`（**含 `repo_id`**）；否则按 `repo_id`
命中 → `UPDATE`（含 `path`，仓库移动/改名）；都没有 → `INSERT`。可读、可断言、没有 upsert
的隐式语义。

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
| 加表 / 改列 | `_MIGRATIONS` **append** 一版 + `SCHEMA_VERSION` +1；旧库升级路径要测（本刀只有"重复 migrate 不变"） |
| 改 `upsert` 的冲突规则 | 验收 4（重复绑定一行）+ `test_rebinding_*` 三条 |
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
| `tests/test_bind.py::test_rebinding_after_origin_added_updates_the_row` | T26 的回归锁（`repo_id` 会被更新） |
| `tests/test_bind.py::test_bind_writes_nothing_into_the_user_repository` | 只写状态根 |
| `tests/test_bind.py::test_list_returns_array_payload_with_camel_case_keys` | 落盘 `snake_case` / 帧层 `camelCase` |