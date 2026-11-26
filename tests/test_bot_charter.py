"""S6 · Bot 与 charter（`bot.list` / `memory.charter.get`）：真 SQLite + 真文件 + 真 WS 帧。

一条用例对应 `docs/design/issues/S6-bot-charter.md` 的 **D11 测试矩阵**第 1–14 行
（行号写在每个用例的 docstring 里；15 在 `tests/test_accept_m1.py`，16 在
`tests/test_reverse_checks.py`）。这里**不 mock**：SQLite 是真文件、记忆空间是真目录、
端到端那条起真内核子进程 + 真 WebSocket。

D3 的 umask 陷阱（T38）是权限用例的核心：`mkdir(mode=0o700)` 的 mode 只作用于**叶子**，
父级按 umask 给 0755；`Path.write_text` 建出来是 0644。所以权限用例显式把 umask 设成
文档实测的 `0o022` 再断言真实 `stat` mode——不设的话，"本机 umask 恰好是 077"会让
逆向注入（去掉 chmod）意外变绿。
"""

from __future__ import annotations

import os
import sqlite3
import stat
from collections.abc import Iterator
from pathlib import Path

import pytest

from postel_studio.domain.models import BOT_NAME_RE, bot_id, bot_name_ok
from postel_studio.storage import db as state_db
from postel_studio.storage import paths

# ---------------------------------------------------------------------------
# 脚手架：真 SQLite（`state_home` 由 conftest 指到 tmp_path，绝不出仓库）
# ---------------------------------------------------------------------------


@pytest.fixture
def umask_022() -> Iterator[None]:
    """在 D3 实测的 umask 下跑（`mkdir` 的默认 mode 才是 0755，T38 才复现得出来）。"""
    previous = os.umask(0o022)
    try:
        yield None
    finally:
        os.umask(previous)


def _state_home(state_home: Path) -> Path:
    """`conftest.state_home` 已经把 `POSTEL_HOME` 指到这里；本文件只需要路径本身。"""
    return state_home


# ---------------------------------------------------------------------------
# D11 第 11 行：名字白名单（06 §2.2 / D7 的 L0 纯函数）
# ---------------------------------------------------------------------------


def test_bot_name_whitelist() -> None:
    """**D11 第 11 行**：`../x` / `Builder` / `a_b` / 33 字符 → 拒绝；`builder` / `qa-1` → 接受。"""
    assert BOT_NAME_RE.pattern == r"^[a-z][a-z0-9-]{0,31}$"
    for bad in ("../x", "Builder", "a_b", "x" * 33, "", "-a", "a b", "a.b", "a/b", ".", ".."):
        assert bot_name_ok(bad) is False, bad
    for good in ("builder", "qa-1", "a", "a" * 32):
        assert bot_name_ok(good) is True, good
    assert bot_id("adec6540a725", "builder") == "adec6540a725/builder"


# ---------------------------------------------------------------------------
# D11 第 12 行：v2 → v3 迁移（只加表、不动 repos；D7 要求老库升级后 repos 原样还在）
# ---------------------------------------------------------------------------


def test_v2_database_upgrades_to_v3_and_keeps_repos(tmp_path: Path) -> None:
    """**D11 第 12 行**：造一个有 `repos` 行的 v2 库 → 升级后 repos 行还在、bots 表可用。"""
    db_path = tmp_path / "state.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(state_db._MIGRATIONS[0])  # v1：repos
    conn.executescript(state_db._MIGRATIONS[1])  # v2：origin_hash
    conn.execute(
        "INSERT INTO repos (repo_id, path, origin_url, default_branch, added_at, origin_hash)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        ("deadbeefcafe", str(tmp_path / "repo"), None, "main", 1.5, None),
    )
    conn.execute("PRAGMA user_version=2")
    conn.commit()
    conn.close()

    opened = state_db.open_state_db(db_path)
    try:
        assert int(opened.execute("PRAGMA user_version").fetchone()[0]) == 3
        assert state_db.SCHEMA_VERSION == 3
        row = opened.execute("SELECT repo_id, path, added_at FROM repos").fetchone()
        assert row is not None
        assert (row[0], row[1], row[2]) == ("deadbeefcafe", str(tmp_path / "repo"), 1.5)
        # bots 表可用（v3 迁移真的建了表，不是只改了个版本号）
        opened.execute(
            "INSERT INTO bots (bot_id, repo_id, name, permission, agent_kind,"
            " charter_rev, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("deadbeefcafe/builder", "deadbeefcafe", "builder", "L1", None, 0, 2.0),
        )
        assert opened.execute("SELECT COUNT(*) FROM bots").fetchone()[0] == 1
    finally:
        opened.close()


# ---------------------------------------------------------------------------
# D11 第 4/5/2 行（L0 侧）：记忆空间的布局、权限、幂等
# ---------------------------------------------------------------------------


def test_ensure_memory_space_layout_and_permissions(state_home: Path, umask_022: None) -> None:
    """**D11 第 4/5 行**：目录含 charter.md/memory/sessions；目录链全 0700、charter 0600。

    第 5 行明确要求「用 `stat` 断言真实 mode」，所以断言的是 `stat.S_IMODE`，不是建目录时
    传的 `mode=` 参数（T38：那个参数只管叶子）。
    """
    home = _state_home(state_home)
    memory = paths.ensure_memory_space("adec6540a725", "builder")
    assert memory == paths.bots_root() / "adec6540a725" / "builder"
    assert memory.is_relative_to(paths.bots_root())
    for name in (paths.CHARTER_FILE, paths.MEMORY_DIR, paths.SESSIONS_DIR):
        assert (memory / name).exists(), name
    for directory in (
        paths.bots_root(),
        paths.bots_root() / "adec6540a725",
        memory,
        memory / paths.MEMORY_DIR,
        memory / paths.SESSIONS_DIR,
    ):
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700, directory
    charter = memory / paths.CHARTER_FILE
    assert stat.S_IMODE(charter.stat().st_mode) == 0o600
    # D4：骨架空白待填，只有标题；末尾一个换行。
    assert charter.read_text(encoding="utf-8") == paths.CHARTER_TEMPLATE.format(name="builder")
    assert charter.read_text(encoding="utf-8").endswith("\n")
    assert home == paths.state_root()


def test_charter_template_has_no_business_duties() -> None:
    """D4 的禁止事项：模板只有标题骨架，**不预设任何业务职责**。"""
    body = paths.CHARTER_TEMPLATE.format(name="builder")
    headings = [line.strip() for line in body.splitlines() if line.startswith("## ")]
    assert headings == ["## 身份", "## 职责", "## 边界", "## 工作方式"]
    others = [line for line in body.splitlines() if line.strip() and not line.startswith("#")]
    assert others == []


def test_ensure_memory_space_never_overwrites_an_existing_charter(
    state_home: Path, umask_022: None
) -> None:
    """**D11 第 2 行（L0 侧）**：charter 已存在就只读不写，第二次调用逐字不变。"""
    _state_home(state_home)
    memory = paths.ensure_memory_space("adec6540a725", "builder")
    charter = memory / paths.CHARTER_FILE
    edited = "# builder\n\n## 职责\n\n审查每一行改动。\n"
    paths.atomic_write(charter, edited.encode("utf-8"))

    again = paths.ensure_memory_space("adec6540a725", "builder")
    assert again == memory
    assert charter.read_text(encoding="utf-8") == edited


# ---------------------------------------------------------------------------
# D11 第 10 行（L0 侧）：名字进路径前的白名单 + 越界检查
# ---------------------------------------------------------------------------


def test_ensure_memory_space_rejects_names_that_escape_bots_root(state_home: Path) -> None:
    """**D11 第 10 行（L0 侧）**：坏名字一律 `ValueError`，且**一个文件都不创建**。

    `..` 是这条用例里最关键的负载：它 `resolve()` 之后**仍在** `bots_root()` 之下（等于
    bots 根本身），所以纵深防御的越界断言拦不住它——挡住它的是白名单（D12 #4 注入点）。
    """
    home = _state_home(state_home)
    bad_names = ("../../etc/passwd", "..", ".", "Builder", "a_b", "x" * 33, "a/b", "")
    for name in bad_names:
        with pytest.raises(ValueError):
            paths.ensure_memory_space("adec6540a725", name)
    # 越界企图没有在状态根内外创建任何东西（状态根本身都不该被建出来）
    assert home.exists() is False
    assert (home.parent / "etc").exists() is False
    with pytest.raises(ValueError):
        paths.bot_memory_dir("adec6540a725", "../../etc/passwd")
