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
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Final

from postel_studio.domain.models import Repo
from postel_studio.storage.paths import _chmod, ensure_state_root, state_db_file

# 06-data-model §7：user_version 是 schema 版本；本刀只有 v1。
SCHEMA_VERSION: Final = 1

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
)


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
            conn.executescript(_MIGRATIONS[version])
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
                "SELECT repo_id, path, origin_url, default_branch, added_at"
                " FROM repos ORDER BY added_at, repo_id"
            ).fetchall()
        return [_row_to_repo(row) for row in rows]

    def get(self, repo_id: str) -> Repo | None:
        """按 id 取；不存在返回 `None`（帧层翻成 `REPO_NOT_BOUND`）。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT repo_id, path, origin_url, default_branch, added_at"
                " FROM repos WHERE repo_id = ?",
                (repo_id,),
            ).fetchone()
        return None if row is None else _row_to_repo(row)

    # -- 写 -----------------------------------------------------------------
    def upsert(self, repo: Repo) -> Repo:
        """写入一条绑定并返回**落库后的**记录。

        「同一路径不产生第二行」是验收 4。三条规则都在这里，**不要**换回单条
        `INSERT … ON CONFLICT`——见下面被推翻的方案。

        1. 按 `path`（唯一键）找一行：有就 **UPDATE**（`repo_id` 一并更新：仓库加了 origin 之后
           `repo_id` 会从路径哈希变成 URL 哈希，D3 已知未决项）。
        2. 没有 `path` 行，但 `repo_id` 已存在（仓库被移动/改名）：**UPDATE** 那行的 `path`。
        3. 都没有：INSERT。
        """
        with self._lock, self._conn:  # `with conn` = 一个事务：UPDATE + 回读不会读到别人的中间态
            row = self._conn.execute(
                "SELECT repo_id FROM repos WHERE path = ?", (repo.path,)
            ).fetchone()
            if row is not None:
                self._conn.execute(
                    "UPDATE repos SET repo_id = ?, origin_url = ?, default_branch = ?,"
                    " added_at = ? WHERE path = ?",
                    (repo.repo_id, repo.origin_url, repo.default_branch, repo.added_at, repo.path),
                )
            elif self._conn.execute(
                "SELECT 1 FROM repos WHERE repo_id = ?", (repo.repo_id,)
            ).fetchone():
                self._conn.execute(
                    "UPDATE repos SET path = ?, origin_url = ?, default_branch = ?,"
                    " added_at = ? WHERE repo_id = ?",
                    (repo.path, repo.origin_url, repo.default_branch, repo.added_at, repo.repo_id),
                )
            else:
                self._conn.execute(
                    "INSERT INTO repos (repo_id, path, origin_url, default_branch, added_at)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (
                        repo.repo_id,
                        repo.path,
                        repo.origin_url,
                        repo.default_branch,
                        repo.added_at,
                    ),
                )
            stored = self._conn.execute(
                "SELECT repo_id, path, origin_url, default_branch, added_at"
                " FROM repos WHERE path = ?",
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
    raw_added = row["added_at"]
    assert isinstance(raw_added, int | float) and not isinstance(raw_added, bool)
    return Repo(
        repo_id=raw_id,
        path=raw_path,
        origin_url=str(raw_origin) if isinstance(raw_origin, str) else None,
        default_branch=str(raw_branch) if isinstance(raw_branch, str) else None,
        added_at=float(raw_added),
    )


@contextmanager
def open_store(path: Path | None = None) -> Iterator[RepoStore]:
    """`with open_store() as store:` —— 异常路径也会关连接。"""
    store = RepoStore(path)
    try:
        yield store
    finally:
        store.close()
