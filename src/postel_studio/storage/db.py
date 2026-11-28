"""`state.db`：内核持有的本地状态（S3 第一次出现用户数据）。

三件事在这里落地：

1. **位置**：只在状态根下（`~/.postel/state.db`，`POSTEL_HOME` 覆盖），**绝不**写进用户仓库
   （S3 禁止事项）。路径由 `storage/paths.py` 的唯一事实源 `state_root()` 决定，本模块不自己拼。
2. **schema 版本**：`PRAGMA user_version`（06-data-model §7），迁移函数**只增不减**、可重入。
   本刀 schema 只有一张 `repos` 表——Bot / Task / Candidate / WorktreeLease 是后续切片的事，
   **不要提前建表**：建错的表比没有表更难改回来。
3. **权限**：文件 `0600`、所在目录 `0700`（05-security §4.1 与 paths 的约定一致）。

为什么用标准库 `sqlite3` 而不是 ORM / 迁移框架：S3 的禁止事项明确不许新增依赖
（DoD：「SQLite 用标准库 `sqlite3`，不引入 SQLAlchemy」）。表结构只有一张，手写 DDL 比
引入一套 alembic 式的机器更可审计。
"""

from __future__ import annotations

import sqlite3
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Final

from postel_studio.domain.models import (
    TASK_TERMINAL,
    Bot,
    Candidate,
    Repo,
    Task,
    WorktreeLease,
    task_transition_ok,
)
from postel_studio.storage.paths import _chmod, ensure_state_root, state_db_file

# 06-data-model §7：user_version 是 schema 版本。
# v1 = S3 的初始 schema（只有 repos）；v2 = S3.5 加的 `origin_hash`（D2）；
# v3 = S6 加的 `bots` 表（D7：只加表、不动 repos）；v4 = S10 加 `tasks` + `worktree_leases`
# （06 §2.3 / §2.5，S7 裁决的"到期一起建"）；v5 = S11 加 `candidates`
# （06 §2.4：`tasks.candidate_id` 列已在 v4 建好，只需补表）；
# v6 = S13 给 `candidates` 加 `reject_reason` 列（拒绝理由落库，D2）。
SCHEMA_VERSION: Final = 6

# ---------------------------------------------------------------------------
# 迁移（每个版本一个函数，索引即版本号；新增版本=往后 append，不改历史）
# ---------------------------------------------------------------------------

# v1：只有 repos（06-data-model §2.1）。`repo_id` 是主键，`path` 唯一（同一路径不能绑两次），
# `default_branch` / `origin_url` 允许 NULL（裸仓库的 HEAD 可能探测不到；没有 origin 是常态）。
_MIGRATIONS: Final[tuple[str, ...]] = (
    """
    CREATE TABLE repos (
        repo_id        TEXT PRIMARY KEY,
        path           TEXT NOT NULL UNIQUE,
        origin_url     TEXT,
        default_branch TEXT,
        added_at       REAL NOT NULL
    );
    """,
    # v2（S3.5 D2/D3）：**只加一列**，不重写任何已有 `repo_id`。`origin_hash` 允许 NULL
    # （没 origin 的行就是 NULL；它不参与主键，老行没有它不影响任何查询）。
    # 老行**不回填**：D2 理论上允许从 `origin_url` 回填，但那要在迁移里再实现一遍 URL 归一化，
    # 收益只是让"去重提示"多覆盖几条历史记录，风险是迁移本身出错。要做请单独一刀，
    # 且**只碰 `origin_hash`，不碰 `repo_id`**（D3）。
    """
    ALTER TABLE repos ADD COLUMN origin_hash TEXT;
    """,
    # v3（S6 D7）：**只加 `bots` 表，不动 `repos`**。`charter_rev` 先建列但 M1 不维护它
    # （D5：charter 正文的事实源是文件；写路径是 S22 的 charter.set）。外键照 06 §2.2：
    # Bot 随仓库删除级联（记忆目录不删，保留可审计——06 §2.2 的不变量）。
    """
    CREATE TABLE bots (
        bot_id      TEXT PRIMARY KEY,
        repo_id     TEXT NOT NULL REFERENCES repos(repo_id) ON DELETE CASCADE,
        name        TEXT NOT NULL,
        permission  TEXT NOT NULL DEFAULT 'L1',
        agent_kind  TEXT,
        charter_rev INTEGER NOT NULL DEFAULT 0,
        created_at  REAL NOT NULL,
        UNIQUE (repo_id, name)
    );
    """,
    # v4（S10 D5）：`tasks`（06 §2.3）与 `worktree_leases`（06 §2.5）一次建齐。
    # `candidate_id` 列先建、S10 恒 NULL（S11 才有值）；`trigger_key` 的唯一约束以部分
    # 索引表达（06 §2.3 不变量："同一事件的重复触发在此去重"）。
    # **两个 BUSY 后备锁**（S10 D5）：tasks 的部分唯一索引把"活跃任务每 Bot 至多一条"
    # 钉在数据层——检查-再插入的竞态即使发生在 orchestrator 的检查之后也会被这条索引拦住；
    # leases 的部分唯一索引照 06 §2.5 原文。SQLite 的部分索引 WHERE 支持任意表达式。
    """
    CREATE TABLE tasks (
        task_id      TEXT PRIMARY KEY,
        bot_id       TEXT NOT NULL REFERENCES bots(bot_id) ON DELETE CASCADE,
        trigger      TEXT NOT NULL,
        trigger_key  TEXT,
        prompt       TEXT NOT NULL,
        base_ref     TEXT,
        state        TEXT NOT NULL,
        candidate_id TEXT,
        error_code   TEXT,
        created_at   REAL NOT NULL,
        started_at   REAL,
        ended_at     REAL
    );
    CREATE UNIQUE INDEX tasks_active_per_bot ON tasks (bot_id) WHERE state IN ('queued', 'running');
    CREATE UNIQUE INDEX tasks_trigger_key ON tasks (bot_id, trigger, trigger_key)
        WHERE trigger_key IS NOT NULL;
    CREATE TABLE worktree_leases (
        lease_id    TEXT PRIMARY KEY,
        bot_id      TEXT NOT NULL REFERENCES bots(bot_id) ON DELETE CASCADE,
        task_id     TEXT NOT NULL UNIQUE REFERENCES tasks(task_id) ON DELETE CASCADE,
        path        TEXT NOT NULL,
        created_at  REAL NOT NULL,
        released_at REAL
    );
    CREATE UNIQUE INDEX leases_active_per_bot ON worktree_leases (bot_id) WHERE released_at IS NULL;
    """,
    # v5（S11 D7）：候选表。`task_id` UNIQUE = 一任务至多一条候选（06 §4）；
    # `branch` UNIQUE 是"候选身份"的数据级保护（ULID 前缀撞车时宁可拒绝，不静默覆盖）。
    # `repo_id` **不冗余存**：列表按 tasks→bots join 出来（06 §2.4 的字段表没有这一列）。
    # diff 全文/stat 文本不进库（06 §8）：这里的三个数字只是冗余的排序/显示快照。
    """
    CREATE TABLE candidates (
        candidate_id  TEXT PRIMARY KEY,
        task_id       TEXT NOT NULL UNIQUE REFERENCES tasks(task_id) ON DELETE CASCADE,
        branch        TEXT NOT NULL UNIQUE,
        base_sha      TEXT NOT NULL,
        head_sha      TEXT NOT NULL,
        files_changed INTEGER NOT NULL,
        insertions    INTEGER NOT NULL,
        deletions     INTEGER NOT NULL,
        review_state  TEXT NOT NULL,
        remote        TEXT,
        target        TEXT,
        created_at    REAL NOT NULL
    );
    CREATE INDEX candidates_review_created ON candidates (review_state, created_at DESC);
    """,
    # v6（S13 D2）：拒绝理由落库。只加一列，不动任何历史数据。
    """
    ALTER TABLE candidates ADD COLUMN reject_reason TEXT;
    """,
)


# 读路径的列清单只写一份：`repos` 的列一变（v2 加了 `origin_hash`），四处 SELECT 一起改
# 才不会有"某处少读一列、静默变成 None"的漏洞。`_row_to_repo` 依赖这里面每一列都存在。
_COLUMNS: Final = "repo_id, path, origin_url, default_branch, added_at, origin_hash"


def state_db_path() -> Path:
    """`state.db` 的路径（**不创建**）。文件名的事实源是 `storage/paths.py`。"""
    return state_db_file()


def _connect(path: Path) -> sqlite3.Connection:
    """打开连接并把库放进已知的安全姿态。**调用方负责关闭。**

    `check_same_thread=False` + `RepositoryStore` 里的锁：网关每个请求都可能在线程池里跑，
    连接跨线程用是 sqlite3 允许的（`threadsafety=3`），但**并发**访问仍要串行化。
    """
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")  # 06 §4：外键强制（本刀的表还没有外键，先立规矩）
    conn.execute("PRAGMA journal_mode=WAL")  # 读不阻塞写；单机本地库收益最大
    return conn


def migrate(conn: sqlite3.Connection, *, path_for_chmod: Path | None = None) -> None:
    """把库升到 `SCHEMA_VERSION`，每一步一个事务。**可重入**：版本够高就什么都不做。

    文件权限在**第一次**创建后收紧一次（`0600`）：`sqlite3.connect` 建出来的文件受 umask
    影响，可能是 `0644`；而 `state.db` 里将来会有用户仓库路径，不该让同组可读。
    """
    if path_for_chmod is not None and path_for_chmod.exists():
        _chmod(path_for_chmod, 0o600)
    current = int(conn.execute("PRAGMA user_version").fetchone()[0])
    for version in range(current, len(_MIGRATIONS)):
        with conn:  # 一个 with = 一个事务：迁移失败整体回滚，不会留下半张表
            try:
                conn.executescript(_MIGRATIONS[version])
            except sqlite3.OperationalError as exc:
                # 内核 SIGKILL 落在 executescript（隐式 commit）与 user_version 写入之间，
                # 会让下一任连接看到「这一步的对象已在、版本号还停在这一步」。对象已存在
                # 说明这一步其实做完了：推进版本、继续下一步，而不是撞「already exists」
                # 让任务失败（CI 实测的 table candidates already exists flake）。
                # 「对象已存在」有 table / column / index 三种形状；其它错误照常抛。
                message = str(exc).lower()
                already = (
                    "already exists" in message
                    or "duplicate column name" in message
                    or "duplicate index" in message
                )
                advanced = int(conn.execute("PRAGMA user_version").fetchone()[0])
                if not already or advanced < version:
                    raise
            # 正常迁移与「对象已在」两条路径都在这里推进版本。
            conn.execute(f"PRAGMA user_version={version + 1}")


def open_state_db(path: Path | None = None) -> sqlite3.Connection:
    """创建目录、打开（必要时创建+迁移）`state.db`，返回连接。"""
    target = path or state_db_file()
    if path is None:
        ensure_state_root()  # 0700；只在用默认位置时管，测试传入的路径由调用方负责
    else:
        target.parent.mkdir(parents=True, exist_ok=True)
    conn = _connect(target)
    try:
        migrate(conn, path_for_chmod=target)
    except BaseException:
        conn.close()
        raise
    return conn


class RepoStore:
    """`repos` 表的读写（S3 唯一一张表）。

    只暴露两个动作：`upsert`（绑定，**重复绑定不产生第二行**）与 `list`。
    解绑（`repo.unbind`）与删除数据不在本刀范围内——09 §5「永不自动删除用户文件」的
    精神同样适用于绑定关系，且 S3 的方法清单里没有它。
    """

    def __init__(self, path: Path | None = None) -> None:
        self._path = path
        self._conn = open_state_db(path)
        self._lock = threading.Lock()

    @property
    def path(self) -> Path:
        """这份 store 打开的库文件（只读；测试与将来的 `doctor` 要指给人看）。"""
        return state_db_path() if self._path is None else self._path

    # -- 生命周期 ----------------------------------------------------------
    def close(self) -> None:
        """关闭连接（`ContextManager` 退出时也走这里）。幂等。"""
        with self._lock:
            self._conn.close()

    def __enter__(self) -> RepoStore:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # -- 读 -----------------------------------------------------------------
    def list(self) -> list[Repo]:
        """全部已绑定仓库，按绑定时间稳定排序（同毫秒时按 `repo_id`，保证输出确定）。"""
        with self._lock:
            rows = self._conn.execute(
                f"SELECT {_COLUMNS} FROM repos ORDER BY added_at, repo_id"
            ).fetchall()
        return [_row_to_repo(row) for row in rows]

    def get(self, repo_id: str) -> Repo | None:
        """按 id 取；不存在返回 `None`（帧层翻成 `REPO_NOT_BOUND`）。"""
        with self._lock:
            row = self._conn.execute(
                f"SELECT {_COLUMNS} FROM repos WHERE repo_id = ?",
                (repo_id,),
            ).fetchone()
        return None if row is None else _row_to_repo(row)

    # -- 写 -----------------------------------------------------------------
    def upsert(self, repo: Repo) -> Repo:
        """写入一条绑定并返回**落库后的**记录。

        「同一路径不产生第二行」是验收 4。三条规则都在这里，**不要**换回单条
        `INSERT … ON CONFLICT`——见下面被推翻的方案。

        1. 按 `path`（唯一键）找一行：有就 **UPDATE**——但 **`repo_id` 不在 SET 里**（S3.5 D1）。
        2. 没有 `path` 行，但 `repo_id` 已存在（仓库被移动/改名）：**UPDATE** 那行的 `path`
           与 origin 字段；`repo_id` 同样是**身份**，不是可变字段，所以也不改。
        3. 都没有：INSERT——这是 `repo_id` **唯一**会被写入的时刻（D1 的"一次性计算"）。

        **为什么规则 1 必须去掉 `repo_id`**（06-pitfalls T26 的续集）：S3 时代这里会更新
        `repo_id`，于是"给仓库加个 remote 再重绑"就让同一行换了一个主键——`bot_id`、
        `~/.postel/bots/<旧id>/` 之类的引用全部失效。现在 `repo_id` 由**路径**决定且写后不变，
        origin 的变化只落在 `origin_url` / `origin_hash`（都是展示/识别字段）。
        """
        with self._lock, self._conn:  # `with conn` = 一个事务：UPDATE + 回读不会读到别人的中间态
            row = self._conn.execute(
                "SELECT repo_id FROM repos WHERE path = ?", (repo.path,)
            ).fetchone()
            if row is not None:
                # D1：**不更新 `repo_id`**。原 id 保留，即使调用方算出的 id 与它不同
                # （v1 时代绑定的老行，其 id 可能是 URL 哈希——D3 明确不回写）。
                self._conn.execute(
                    "UPDATE repos SET origin_url = ?, origin_hash = ?, default_branch = ?,"
                    " added_at = ? WHERE path = ?",
                    (
                        repo.origin_url,
                        repo.origin_hash,
                        repo.default_branch,
                        repo.added_at,
                        repo.path,
                    ),
                )
            elif self._conn.execute(
                "SELECT 1 FROM repos WHERE repo_id = ?", (repo.repo_id,)
            ).fetchone():
                self._conn.execute(
                    "UPDATE repos SET path = ?, origin_url = ?, origin_hash = ?,"
                    " default_branch = ?, added_at = ? WHERE repo_id = ?",
                    (
                        repo.path,
                        repo.origin_url,
                        repo.origin_hash,
                        repo.default_branch,
                        repo.added_at,
                        repo.repo_id,
                    ),
                )
            else:
                self._conn.execute(
                    "INSERT INTO repos"
                    " (repo_id, path, origin_url, default_branch, added_at, origin_hash)"
                    " VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        repo.repo_id,
                        repo.path,
                        repo.origin_url,
                        repo.default_branch,
                        repo.added_at,
                        repo.origin_hash,
                    ),
                )
            stored = self._conn.execute(
                f"SELECT {_COLUMNS} FROM repos WHERE path = ?",
                (repo.path,),
            ).fetchone()
        if stored is None:  # pragma: no cover - 只有 SQL 写错才走得到
            raise RuntimeError("upsert 之后读不回这一行")
        return _row_to_repo(stored)


def _row_to_repo(row: sqlite3.Row) -> Repo:
    """`sqlite3.Row` → `Repo`。类型收敛集中在这里，别处不再直接碰 Row。"""
    raw_id = str(row["repo_id"])
    raw_path = str(row["path"])
    raw_origin = row["origin_url"]
    raw_branch = row["default_branch"]
    raw_hash = row["origin_hash"]
    raw_added = row["added_at"]
    assert isinstance(raw_added, int | float) and not isinstance(raw_added, bool)
    return Repo(
        repo_id=raw_id,
        path=raw_path,
        origin_url=str(raw_origin) if isinstance(raw_origin, str) else None,
        default_branch=str(raw_branch) if isinstance(raw_branch, str) else None,
        added_at=float(raw_added),
        origin_hash=str(raw_hash) if isinstance(raw_hash, str) else None,
    )


@contextmanager
def open_store(path: Path | None = None) -> Iterator[RepoStore]:
    """`with open_store() as store:` —— 异常路径也会关连接。"""
    store = RepoStore(path)
    try:
        yield store
    finally:
        store.close()


# `bots` 的列清单只写一份（同 `_COLUMNS` 的理由：一处改、处处一致）。
_BOT_COLUMNS: Final = "bot_id, repo_id, name, permission, agent_kind, charter_rev, created_at"


class BotStore:
    """`bots` 表的读写（S6 v3 新增）。与 `RepoStore` 同款：自开关连接、一把锁串行化。

    `insert` 不吞 `sqlite3.IntegrityError`：重复主键 / 重名是**调用方**该显式处理的错，
    静默覆盖会让"D2 只播种一次"失去证据。
    """

    def __init__(self, path: Path | None = None) -> None:
        self._conn = open_state_db(path)
        self._lock = threading.Lock()

    def close(self) -> None:
        """关闭连接（`with` 退出时也走这里）。幂等。"""
        with self._lock:
            self._conn.close()

    def __enter__(self) -> BotStore:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def list_for_repo(self, repo_id: str) -> list[Bot]:
        """某个仓库的全部 Bot，按 `created_at, bot_id` 稳定排序（确定性输出）。"""
        with self._lock:
            rows = self._conn.execute(
                f"SELECT {_BOT_COLUMNS} FROM bots WHERE repo_id = ? ORDER BY created_at, bot_id",
                (repo_id,),
            ).fetchall()
        return [_row_to_bot(row) for row in rows]

    def get(self, bot_id: str) -> Bot | None:
        """按复合主键取；不存在返回 `None`（帧层翻成 `INVALID_PARAMS`，D6）。"""
        with self._lock:
            row = self._conn.execute(
                f"SELECT {_BOT_COLUMNS} FROM bots WHERE bot_id = ?", (bot_id,)
            ).fetchone()
        return None if row is None else _row_to_bot(row)

    def insert(self, bot: Bot) -> Bot:
        """插入一个新 Bot 并返回落库后的记录（成功返回入参；失败抛异常）。"""
        with self._lock, self._conn:
            self._conn.execute(
                f"INSERT INTO bots ({_BOT_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    bot.bot_id,
                    bot.repo_id,
                    bot.name,
                    bot.permission,
                    bot.agent_kind,
                    bot.charter_rev,
                    bot.created_at,
                ),
            )
        return bot

    def set_permission(self, bot_id: str, permission: str) -> Bot | None:
        """改一个 Bot 的权限并返回落库后的记录；`bot_id` 不存在返回 `None`（S8 D1）。

        只动 `permission` 一列：`bot_id` / `repo_id` / `name` 是身份，`charter_rev` 属于 charter
        的写路径（S22）。"行不存在"由**调用方**翻成码——store 不认识 07 §1.3。

        `rowcount` 在 SQLite 里是"匹配到的行数"，不是"值真的变了"：写成同一个值也返回 1。
        调用方自己在写之前挡掉 no-op（`api/bots.py` 的做法），这里不做无谓的判断。
        """
        with self._lock, self._conn:
            updated = self._conn.execute(
                "UPDATE bots SET permission = ? WHERE bot_id = ?", (permission, bot_id)
            ).rowcount
            if updated == 0:
                return None
            row = self._conn.execute(
                f"SELECT {_BOT_COLUMNS} FROM bots WHERE bot_id = ?", (bot_id,)
            ).fetchone()
        return None if row is None else _row_to_bot(row)


def _row_to_bot(row: sqlite3.Row) -> Bot:
    """`sqlite3.Row` → `Bot`。类型收敛集中在这里，别处不再直接碰 Row。"""
    raw_rev = row["charter_rev"]
    raw_created = row["created_at"]
    raw_agent = row["agent_kind"]
    assert isinstance(raw_rev, int) and not isinstance(raw_rev, bool)
    assert isinstance(raw_created, int | float) and not isinstance(raw_created, bool)
    return Bot(
        bot_id=str(row["bot_id"]),
        repo_id=str(row["repo_id"]),
        name=str(row["name"]),
        permission=str(row["permission"]),
        agent_kind=str(raw_agent) if isinstance(raw_agent, str) else None,
        charter_rev=raw_rev,
        created_at=float(raw_created),
    )


# `tasks` 的列清单只写一份（同 `_COLUMNS` 的理由）。
_TASK_COLUMNS: Final = (
    "task_id, bot_id, trigger, trigger_key, prompt, base_ref, state,"
    " candidate_id, error_code, created_at, started_at, ended_at"
)
# `worktree_leases` 同上。
_LEASE_COLUMNS: Final = "lease_id, bot_id, task_id, path, created_at, released_at"


class TaskStore:
    """`tasks` 表的读写（S10 v4 新增）。锁与连接纪律同 `RepoStore` / `BotStore`。

    状态机的**唯一写入口**是 `transition`（S10 D6）：它做一次原子的
    `UPDATE ... WHERE state = <from>`，迁移表校验交给 domain（`task_transition_ok`）——
    store 不重复排布允许边，但它保证"读-判-写"三步不会被打断。
    """

    def __init__(self, path: Path | None = None) -> None:
        self._conn = open_state_db(path)
        self._lock = threading.Lock()

    def close(self) -> None:
        """关闭连接（`with` 退出时也走这里）。幂等。"""
        with self._lock:
            self._conn.close()

    def __enter__(self) -> TaskStore:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def insert(self, task: Task) -> None:
        """插入一个 `queued` 任务。活跃索引撞上（`BUSY` 竞态的后备锁）抛
        `sqlite3.IntegrityError`，由调用方翻成码——store 不认识 07 §1.3。"""
        with self._lock, self._conn:
            self._conn.execute(
                f"INSERT INTO tasks ({_TASK_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    task.task_id,
                    task.bot_id,
                    task.trigger,
                    task.trigger_key,
                    task.prompt,
                    task.base_ref,
                    task.state,
                    task.candidate_id,
                    task.error_code,
                    task.created_at,
                    task.started_at,
                    task.ended_at,
                ),
            )

    def get(self, task_id: str) -> Task | None:
        """按主键取；不存在返回 `None`（帧层翻码）。"""
        with self._lock:
            row = self._conn.execute(
                f"SELECT {_TASK_COLUMNS} FROM tasks WHERE task_id = ?", (task_id,)
            ).fetchone()
        return None if row is None else _row_to_task(row)

    def active_for_bot(self, bot_id: str) -> Task | None:
        """这个 Bot 的活跃任务（`queued` / `running`）；没有就是 `None`（`BUSY` 的判据）。"""
        with self._lock:
            row = self._conn.execute(
                f"SELECT {_TASK_COLUMNS} FROM tasks WHERE bot_id = ?"
                " AND state IN ('queued', 'running')",
                (bot_id,),
            ).fetchone()
        return None if row is None else _row_to_task(row)

    def latest_for_bot(self, bot_id: str) -> Task | None:
        """S15：这个 Bot 最近一条任务（含终态），供页面重连后恢复中断态。

        `created_at DESC, task_id DESC`：两条任务落在同一毫秒时也有确定顺序（id 作第二排序键）。
        """
        with self._lock:
            row = self._conn.execute(
                f"SELECT {_TASK_COLUMNS} FROM tasks WHERE bot_id = ?"
                " ORDER BY created_at DESC, task_id DESC LIMIT 1",
                (bot_id,),
            ).fetchone()
        return None if row is None else _row_to_task(row)

    def transition(
        self,
        task_id: str,
        to_state: str,
        *,
        error_code: str | None = None,
        candidate_id: str | None = None,
    ) -> Task | None:
        """`current → to_state` 的一次**原子**迁移（S10 D6）。

        读出当前行 → domain 校验迁移边 → `UPDATE ... WHERE state = <当前值>`。行不存在、
        迁移非法、或 `WHERE` 没命中（竞态里别人先动了）都返回 `None`——调用方必须把
        `None` 当作"已是终态"处理，而不是重试。时间戳在这里落：`started_at` 只在
        `queued→running` 写，`ended_at` 只在进终态写，调用方不用管。

        `with self._lock, self._conn`：**必须**把连接也放进 with——sqlite3 的隐式事务
        只会由显式 commit / `with conn` 退出收尾；只拿锁不拿连接，UPDATE 开着的写事务
        会一直堵住其它连接的写（实测复现：下一个 store 的任何写都 `database is locked`）。
        """
        with self._lock, self._conn:
            row = self._conn.execute(
                f"SELECT {_TASK_COLUMNS} FROM tasks WHERE task_id = ?", (task_id,)
            ).fetchone()
            if row is None:
                return None
            task = _row_to_task(row)
            if not task_transition_ok(task.state, to_state):
                return None
            now = time.time()
            started = now if (task.state, to_state) == ("queued", "running") else task.started_at
            ended = now if to_state in TASK_TERMINAL else task.ended_at
            candidate = candidate_id if candidate_id is not None else task.candidate_id
            updated = self._conn.execute(
                "UPDATE tasks SET state = ?, error_code = ?, candidate_id = ?,"
                " started_at = ?, ended_at = ? WHERE task_id = ? AND state = ?",
                (to_state, error_code, candidate, started, ended, task_id, task.state),
            )
            if updated.rowcount == 0:  # pragma: no cover - 锁内的竞态在单进程里不可达
                return None
            row2 = self._conn.execute(
                f"SELECT {_TASK_COLUMNS} FROM tasks WHERE task_id = ?", (task_id,)
            ).fetchone()
        return None if row2 is None else _row_to_task(row2)

    def mark_interrupted(self) -> int:
        """内核重启的记账（S10 D7）：`queued` / `running` 全部标 `interrupted`。

        返回标掉的行数（可观测）。这是**唯一**绕开 `transition` 的写路径——重启时读-
        判-写逐行做会在锁外留窗口，而"全部标掉"本身是一条原子的批量 UPDATE，
        语义等价于对每行跑一次 `transition`（两条边都在迁移表里）。
        """
        with self._lock, self._conn:
            now = time.time()
            cursor = self._conn.execute(
                "UPDATE tasks SET state = 'interrupted', ended_at = ?"
                " WHERE state IN ('queued', 'running')",
                (now,),
            )
        return cursor.rowcount


class LeaseStore:
    """`worktree_leases` 表的读写（S10 v4 新增，06 §2.5）。

    每个任务恰好一条租约（`task_id UNIQUE`）；活跃租约每 Bot 至多一条（部分唯一索引，
    违反抛 `IntegrityError`）。租约**不**代替 orchestrator 持有路径——路径的事实源仍是
    git 与文件系统（S7 D4 的原则不变），租约回答的只是"这个 Bot 现在占用着哪个工作区"。
    """

    def __init__(self, path: Path | None = None) -> None:
        self._conn = open_state_db(path)
        self._lock = threading.Lock()

    def close(self) -> None:
        """关闭连接（`with` 退出时也走这里）。幂等。"""
        with self._lock:
            self._conn.close()

    def __enter__(self) -> LeaseStore:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def insert(self, lease: WorktreeLease) -> None:
        """登记一条租约。活跃索引撞上（同 Bot 双任务竞态）抛 `IntegrityError`。"""
        with self._lock, self._conn:
            self._conn.execute(
                f"INSERT INTO worktree_leases ({_LEASE_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    lease.lease_id,
                    lease.bot_id,
                    lease.task_id,
                    lease.path,
                    lease.created_at,
                    lease.released_at,
                ),
            )

    def active_for_task(self, task_id: str) -> WorktreeLease | None:
        """这个任务的**活跃**租约；已释放或没有就是 `None`（只读，观测用）。"""
        with self._lock:
            row = self._conn.execute(
                f"SELECT {_LEASE_COLUMNS} FROM worktree_leases"
                " WHERE task_id = ? AND released_at IS NULL",
                (task_id,),
            ).fetchone()
        return None if row is None else _row_to_lease(row)

    def release(self, task_id: str) -> bool:
        """按任务释放活跃租约（`released_at` 落当前时刻）。没有活跃租约回 `False`。"""
        with self._lock, self._conn:
            updated = self._conn.execute(
                "UPDATE worktree_leases SET released_at = ?"
                " WHERE task_id = ? AND released_at IS NULL",
                (time.time(), task_id),
            )
        return updated.rowcount > 0


def _row_to_task(row: sqlite3.Row) -> Task:
    """`sqlite3.Row` → `Task`。类型收敛集中在这里。"""
    raw_key = row["trigger_key"]
    raw_base = row["base_ref"]
    raw_candidate = row["candidate_id"]
    raw_error = row["error_code"]
    raw_created = row["created_at"]
    raw_started = row["started_at"]
    raw_ended = row["ended_at"]
    assert isinstance(raw_created, int | float) and not isinstance(raw_created, bool)
    return Task(
        task_id=str(row["task_id"]),
        bot_id=str(row["bot_id"]),
        trigger=str(row["trigger"]),
        trigger_key=str(raw_key) if isinstance(raw_key, str) else None,
        prompt=str(row["prompt"]),
        base_ref=str(raw_base) if isinstance(raw_base, str) else None,
        state=str(row["state"]),
        candidate_id=str(raw_candidate) if isinstance(raw_candidate, str) else None,
        error_code=str(raw_error) if isinstance(raw_error, str) else None,
        created_at=float(raw_created),
        started_at=float(raw_started) if isinstance(raw_started, int | float) else None,
        ended_at=float(raw_ended) if isinstance(raw_ended, int | float) else None,
    )


def _row_to_lease(row: sqlite3.Row) -> WorktreeLease:
    """`sqlite3.Row` → `WorktreeLease`。类型收敛集中在这里（同 `_row_to_task`）。"""
    raw_created = row["created_at"]
    raw_released = row["released_at"]
    assert isinstance(raw_created, int | float) and not isinstance(raw_created, bool)
    return WorktreeLease(
        lease_id=str(row["lease_id"]),
        bot_id=str(row["bot_id"]),
        task_id=str(row["task_id"]),
        path=str(row["path"]),
        created_at=float(raw_created),
        released_at=float(raw_released) if isinstance(raw_released, int | float) else None,
    )


# 候选的读路径列清单（同 `_COLUMNS` 的理由：只写一份）。join 查询用 `c.` 前缀，
# 由 `_candidate_join_columns()` 从这份清单生成，避免两处漂移。
_CANDIDATE_COLUMNS: Final = (
    "candidate_id, task_id, branch, base_sha, head_sha, files_changed,"
    " insertions, deletions, review_state, remote, target, created_at, reject_reason"
)


def _candidate_join_columns() -> str:
    return ", ".join(f"c.{column}" for column in _CANDIDATE_COLUMNS.split(", "))


class CandidateStore:
    """`candidates` 表的读写门面（S11，带锁）。

    与 `TaskStore` 一样：读只拿 `_lock`，写**必须** `with self._lock, self._conn`
    （T44：只拿锁会让隐式写事务永不提交，堵死其它连接）。
    """

    def __init__(self, path: Path | None = None) -> None:
        self._conn = open_state_db(path)
        self._lock = threading.Lock()

    @property
    def path(self) -> Path:
        return Path(self._conn.execute("PRAGMA database_list").fetchone()[2])

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def __enter__(self) -> CandidateStore:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def insert(self, candidate: Candidate) -> None:
        """插入一行；重复 `candidate_id` / `task_id` / `branch` 抛 `IntegrityError`。"""
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO candidates ("
                f"{_CANDIDATE_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    candidate.candidate_id,
                    candidate.task_id,
                    candidate.branch,
                    candidate.base_sha,
                    candidate.head_sha,
                    candidate.files_changed,
                    candidate.insertions,
                    candidate.deletions,
                    candidate.review_state,
                    candidate.remote,
                    candidate.target,
                    candidate.created_at,
                    candidate.reject_reason,
                ),
            )

    def get(self, candidate_id: str) -> Candidate | None:
        with self._lock:
            row = self._conn.execute(
                f"SELECT {_CANDIDATE_COLUMNS} FROM candidates WHERE candidate_id = ?",
                (candidate_id,),
            ).fetchone()
        return None if row is None else _row_to_candidate(row)

    def for_task(self, task_id: str) -> Candidate | None:
        with self._lock:
            row = self._conn.execute(
                f"SELECT {_CANDIDATE_COLUMNS} FROM candidates WHERE task_id = ?", (task_id,)
            ).fetchone()
        return None if row is None else _row_to_candidate(row)

    def list_for_repo(
        self,
        repo_id: str,
        *,
        review_state: str | None = None,
        task_id: str | None = None,
        limit: int = 100,
    ) -> list[Candidate]:
        """这个仓库的候选，按 `created_at DESC`（S12 收件箱按这个顺序）。

        `repo_id` 经 `tasks → bots` join 得到——候选表不冗余存 repo_id（06 §2.4）。
        `task_id` 是 S11 单条详情卡的窄读；`review_state` 是 06 §3.2 的筛选项。
        """
        sql = (
            f"SELECT {_candidate_join_columns()} FROM candidates c"
            " JOIN tasks t ON t.task_id = c.task_id"
            " JOIN bots b ON b.bot_id = t.bot_id"
            " WHERE b.repo_id = ?"
        )
        params: list[object] = [repo_id]
        if review_state is not None:
            sql += " AND c.review_state = ?"
            params.append(review_state)
        if task_id is not None:
            sql += " AND c.task_id = ?"
            params.append(task_id)
        sql += " ORDER BY c.created_at DESC, c.candidate_id LIMIT ?"
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [_row_to_candidate(row) for row in rows]

    def transition(
        self, candidate_id: str, target: str, *, reject_reason: str | None = None
    ) -> Candidate | None:
        """S13 D3：原子迁移 `review_state`，输掉竞态回 `None`。

        `UPDATE … WHERE review_state = <from>`：from 由内存里的当前行给出，但**真正的
        门是 WHERE**——两个 approve 同时到时只有一个 UPDATE 命中（T44：锁 + 连接必须
        一起拿，否则隐式事务不提交）。理由只在 reject 时写，随状态一起提交。
        """
        from postel_studio.domain.models import review_transition_ok

        with self._lock:
            row = self._conn.execute(
                "SELECT review_state FROM candidates WHERE candidate_id = ?", (candidate_id,)
            ).fetchone()
        if row is None:
            return None
        current = str(row["review_state"])
        if not review_transition_ok(current, target):
            return None
        with self._lock, self._conn:
            cursor = self._conn.execute(
                "UPDATE candidates SET review_state = ?, reject_reason = ?"
                " WHERE candidate_id = ? AND review_state = ?",
                (target, reject_reason, candidate_id, current),
            )
        if cursor.rowcount == 0:
            return None  # 竞态输家：别人先改了状态
        return self.get(candidate_id)

    def discard(self, candidate_id: str) -> bool:
        """补偿删除**本 worker 刚插入**的候选（S11 D6：transition 输掉 cancel 竞态时）。

        不做通用删除 API：候选一旦被人看见就不该被内核删掉（06 §2.4：拒绝也留痕）。
        """
        with self._lock, self._conn:
            cursor = self._conn.execute(
                "DELETE FROM candidates WHERE candidate_id = ?", (candidate_id,)
            )
        return cursor.rowcount > 0


def _row_to_candidate(row: sqlite3.Row) -> Candidate:
    """`sqlite3.Row` → `Candidate`。类型收敛集中在这里（同 `_row_to_task`）。"""
    raw_created = row["created_at"]
    assert isinstance(raw_created, int | float) and not isinstance(raw_created, bool)
    return Candidate(
        candidate_id=str(row["candidate_id"]),
        task_id=str(row["task_id"]),
        branch=str(row["branch"]),
        base_sha=str(row["base_sha"]),
        head_sha=str(row["head_sha"]),
        files_changed=int(row["files_changed"]),
        insertions=int(row["insertions"]),
        deletions=int(row["deletions"]),
        review_state=str(row["review_state"]),
        remote=str(row["remote"]) if row["remote"] is not None else None,
        target=str(row["target"]) if row["target"] is not None else None,
        created_at=float(raw_created),
        reject_reason=(str(row["reject_reason"]) if row["reject_reason"] is not None else None),
    )
