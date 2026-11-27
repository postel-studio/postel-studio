"""S15 · 重启恢复的验收：半截任务标 `interrupted`、永不撒谎成 succeeded、显式重跑。

后端恢复（`mark_interrupted` / 状态机 / schema 迁移）在 S10 已实现（S15 spec §0 的探测证据），
本文件把它钉成回归测试，并验证 S15 新增的 UI 中断态与重跑入口的行为契约。

真重启用例起**真内核子进程**（临时 `POSTEL_HOME`），不碰真实数据；不依赖 opencode
（用假 ACP 命令让任务停在 running），确定性。真 Agent 路径由 S14 的 accept_m2.py 覆盖。
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

from postel_studio.api import frames
from postel_studio.domain.models import (
    TASK_ACTIVE,
    TASK_TERMINAL,
    Bot,
    Permission,
    Repo,
    Task,
    task_transition_ok,
)
from postel_studio.orchestrator import EventHub, Orchestrator
from postel_studio.storage import db as state_db
from postel_studio.storage.db import (
    SCHEMA_VERSION,
    BotStore,
    CandidateStore,
    LeaseStore,
    RepoStore,
    TaskStore,
    open_state_db,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
CLI_BIN = Path(sys.executable).with_name("postel-studio")
# 反向注入框架前置 PYTHONPATH 到 src 副本：必须从**实际加载的模块**读源码，
# 硬编码 REPO_ROOT 会让注入改不到断言目标（反向注入永远绿）。
from postel_studio.web import assets as _assets_module  # noqa: E402

ASSETS = Path(_assets_module.__file__)
BOOT_TIMEOUT = 30.0
STOP_TIMEOUT = 15.0


# ---- 夹具：真库 + 一个 repo / 三个 bot（每 bot 至多一条活跃任务，见 S10 D5） --------------


@pytest.fixture
def stores(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("POSTEL_HOME", str(home))
    repos = RepoStore()
    bots = BotStore()
    tasks = TaskStore()
    leases = LeaseStore()
    candidates = CandidateStore()
    repo = Repo(
        repo_id="0123456789ab",
        path=str(tmp_path / "repo"),
        origin_url=None,
        default_branch="main",
        added_at=time.time(),
        origin_hash=None,
    )
    (tmp_path / "repo").mkdir()
    repos.upsert(repo)
    bot_ids: list[str] = []
    for name in ("a", "b", "c"):
        bot = Bot(
            bot_id=f"{repo.repo_id}/{name}",
            repo_id=repo.repo_id,
            name=name,
            permission=Permission.L1,
            agent_kind=None,
            charter_rev=0,
            created_at=time.time(),
        )
        bots.insert(bot)
        bot_ids.append(bot.bot_id)
    yield repos, bots, tasks, leases, candidates, bot_ids
    for store in (tasks, leases, candidates, bots, repos):
        store.close()


def _task(bot_id: str, state: str, tid: str, *, candidate_id: str | None = None) -> Task:
    return Task(
        task_id=tid,
        bot_id=bot_id,
        trigger="manual",
        trigger_key=None,
        prompt="p",
        base_ref=None,
        state=state,
        candidate_id=candidate_id,
        error_code=None,
        created_at=time.time(),
        started_at=None,
        ended_at=None,
    )


def _orchestrator(bots: BotStore, tasks: TaskStore, leases: LeaseStore) -> Orchestrator:
    """构造一个新编排器 = 一次内核重启（mark_interrupted 在 __init__ 里）。"""
    return Orchestrator(bots, tasks, leases, FakeWorktrees(), FakeCandidateMaker(), EventHub())


class FakeWorktrees:
    def acquire(self, params: dict[str, object]) -> dict[str, object]:
        return {"path": "/tmp/never"}


class FakeCandidateMaker:
    def snapshot_base(self, worktree_path: str) -> str:
        return "0" * 40

    def emerge(self, **kwargs: object) -> None:
        return None

    def discard(self, candidate_id: str) -> None:
        return None


# ---- 验收 1/3/5：后端恢复（S10 已实现，S15 钉成回归） -------------------------------------


def test_restart_marks_running_and_queued_interrupted(stores) -> None:
    """验收 1：新编排器构造（= 重启）把 running 与 queued 都标 interrupted。"""
    _repos, bots, tasks, leases, _candidates, bot_ids = stores
    queued = _task(bot_ids[0], "queued", "a" * 26)
    running = _task(bot_ids[1], "queued", "b" * 26)
    tasks.insert(queued)
    tasks.insert(running)
    assert tasks.transition(running.task_id, "running") is not None
    _orchestrator(bots, tasks, leases)
    assert tasks.get(queued.task_id).state == "interrupted"
    after = tasks.get(running.task_id)
    assert after is not None and after.state == "interrupted"
    assert after.ended_at is not None


def test_finished_task_and_candidate_survive_restart(stores) -> None:
    """验收 3：已完成任务与它的 candidate_id 外键在重启后不变。"""
    _repos, bots, tasks, leases, _candidates, bot_ids = stores
    finished = _task(bot_ids[2], "succeeded", "c" * 26, candidate_id="cand-done")
    tasks.insert(finished)
    _orchestrator(bots, tasks, leases)
    after = tasks.get(finished.task_id)
    assert after is not None
    assert after.state == "succeeded"
    assert after.candidate_id == "cand-done"


def test_interrupted_cannot_become_succeeded(stores) -> None:
    """验收 2：状态机 + store 双重断言 interrupted 终态封死。"""
    _repos, _bots, tasks, _leases, _candidates, bot_ids = stores
    assert task_transition_ok("interrupted", "succeeded") is False
    assert "interrupted" in TASK_TERMINAL
    assert "interrupted" not in TASK_ACTIVE
    task = _task(bot_ids[0], "interrupted", "a" * 26)
    tasks.insert(task)
    assert tasks.transition(task.task_id, "succeeded") is None
    assert tasks.get(task.task_id).state == "interrupted"


def test_empty_db_is_current_version(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """验收 5a：空库建出来就是当前 schema 版本。"""
    monkeypatch.setenv("POSTEL_HOME", str(tmp_path))
    conn = open_state_db(tmp_path / "state.db")
    assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    assert len(state_db._MIGRATIONS) == SCHEMA_VERSION
    conn.close()


def test_old_db_migration_is_reentrant(tmp_path: Path) -> None:
    """验收 5b：旧版本库（与生产逐字一致的 v1 DDL）升到当前，且可重入不倒退。"""
    import sqlite3

    from tests.test_bind import _V1_SCHEMA  # 不复制 v1 DDL，复用唯一事实源

    old = tmp_path / "v1.db"
    c = sqlite3.connect(old)
    c.executescript(_V1_SCHEMA)  # 手工建旧库（不是"新建库看它是不是当前版本"）
    c.execute("PRAGMA user_version=1")
    c.commit()
    c.close()

    first = open_state_db(old)
    v1 = first.execute("PRAGMA user_version").fetchone()[0]
    first.close()
    second = open_state_db(old)  # 可重入：再开一次不报错、不倒退
    v2 = second.execute("PRAGMA user_version").fetchone()[0]
    second.close()
    assert v1 == v2 == SCHEMA_VERSION


# ---- 验收 4 + DOM 契约：显式重跑，绝不自动 -----------------------------------------------


def test_rerun_creates_new_task_id(stores) -> None:
    """验收 4：重跑走 task.dispatch → 新 ULID，不复用旧 id。"""
    _repos, bots, tasks, leases, _candidates, bot_ids = stores
    old = _task(bot_ids[0], "interrupted", "a" * 26)
    tasks.insert(old)
    orch = _orchestrator(bots, tasks, leases)
    # 新编排器已把旧任务标 interrupted；dispatch 一条新任务应拿到新 id。
    data = orch.dispatch({"botId": bot_ids[0], "prompt": "重跑"})
    assert isinstance(data["taskId"], str)
    assert data["taskId"] != old.task_id
    assert data["state"] == "queued"
    # T44 纪律：cancel 掉新任务并等 worker 收场，否则 fixture 关库时 worker 还在写。
    orch.cancel({"taskId": data["taskId"]})
    assert orch.wait_idle(timeout=10.0), "worker 没在 10s 内退出"


def test_task_latest_returns_newest_or_none(stores) -> None:
    """D8：task.latest 返回该 Bot 最近一条（含终态），无任务回 None，坏 botId 稳定报错。"""
    _repos, _bots, tasks, leases, _candidates, bot_ids = stores
    orch = _orchestrator(_bots, tasks, leases)
    assert orch.latest({"botId": bot_ids[0]}) is None
    tasks.insert(_task(bot_ids[0], "interrupted", "a" * 26))
    got = orch.latest({"botId": bot_ids[0]})
    assert got is not None and got["taskId"] == "a" * 26
    assert got["state"] == "interrupted"
    with pytest.raises(frames.FrameError) as exc:
        orch.latest({"botId": "不存在"})
    assert exc.value.code == frames.INVALID_PARAMS


def test_reconnect_restores_only_interrupted(stores) -> None:
    """D8：页面重连恢复只认 interrupted，绝不把 succeeded 伪装成当前任务。"""
    source = ASSETS.read_text(encoding="utf-8")
    body = source.split("function restoreInterruptedTask()", 1)[1].split("\n  }", 1)[0]
    assert 'data.state !== "interrupted"' in body
    assert "trackTask(" in body
    assert "submitTask(" not in body  # 恢复只读，绝不重跑


def test_task_interrupted_dom_contract() -> None:
    """DOM 契约：`interrupted` 态 + `#task-rerun` 按钮 + CSS 只在 interrupted 显隐。"""
    source = ASSETS.read_text(encoding="utf-8")
    for token in (
        'id="task-rerun"',
        'data-state="interrupted"',
        '#task[data-state="interrupted"]',
        '#task:not([data-state="interrupted"]) #task-rerun { display: none; }',
        "restoreInterruptedTask()",  # D8：重连后查 task.latest 恢复中断态
        'request("task.latest", { botId: selectedBotId }',
        'taskRerun.addEventListener("click", rerunTask)',
        'interrupted: "被打断（内核重启过，确认后可重跑）"',
    ):
        assert token in source, f"缺 DOM 契约：{token}"
    assert "innerHTML" not in source, "页面不得用 innerHTML"


def test_no_auto_rerun() -> None:
    """禁止事项 #4：重跑只在显式点击时发帧；事件/加载回调不得直接 dispatch。"""
    source = ASSETS.read_text(encoding="utf-8")
    # rerunTask 函数体只在按钮 click 上绑定，onTaskUpdated / setState 里没有调用
    rerun_body = source.split("function rerunTask()", 1)[1].split("\n  }", 1)[0]
    assert "submitTask()" in rerun_body
    for fn in ("function onTaskUpdated", "function setState", "function resetTask"):
        body = source.split(fn, 1)[1].split("\n  }", 1)[0]
        assert "rerunTask" not in body, f"{fn} 不得自动重跑"


# ---- 真重启端到端：SIGKILL 内核 → 重启 → task.status 是 interrupted --------------------


def test_real_kernel_restart_interrupts_half_done_task(tmp_path: Path) -> None:
    """验收 1 端到端：真内核被 SIGKILL → 重启 → 半截任务变 interrupted，已完成任务不变。

    用 `gateway_util.Kernel`（真子进程 + 真 WS）；不依赖 opencode——直接写库造任务行，
    验证的是「重启那一刻的恢复」，派活本身由 S10/S14 覆盖。
    """
    import asyncio

    import websockets

    from tests.gateway_util import Kernel

    home = tmp_path / "home"
    home.mkdir()

    # 第一任内核：起起来，让 state.db 与 schema 建好（然后杀掉，不留收场机会）
    first = Kernel(home)
    first.kill()  # SIGKILL：模拟崩溃，worker/连接都没有优雅退出

    # 直接写库造两行：一个 running（半截）、一个 succeeded（已完成带候选）。
    # 显式传内核的那份 state.db 路径（conftest 把会话 HOME 改到了别处，不能用默认）。
    db_path = home / "state.db"
    repos = RepoStore(db_path)
    bots = BotStore(db_path)
    tasks = TaskStore(db_path)
    repo = Repo(
        repo_id="0123456789ab",
        path=str(tmp_path / "repo"),
        origin_url=None,
        default_branch="main",
        added_at=time.time(),
        origin_hash=None,
    )
    (tmp_path / "repo").mkdir(exist_ok=True)
    repos.upsert(repo)
    for name in ("a", "b"):
        bots.insert(
            Bot(
                bot_id=f"{repo.repo_id}/{name}",
                repo_id=repo.repo_id,
                name=name,
                permission=Permission.L1,
                agent_kind=None,
                charter_rev=0,
                created_at=time.time(),
            )
        )
    tasks.insert(_task(f"{repo.repo_id}/a", "running", "a" * 26))
    tasks.insert(_task(f"{repo.repo_id}/b", "succeeded", "b" * 26, candidate_id="cand-ok"))
    tasks.close()
    bots.close()
    repos.close()

    # 第二任内核：构造编排器时会 mark_interrupted
    with Kernel(home) as second:
        ticket, _ws_path = second.fetch_page()

        async def query() -> dict[str, object]:
            async with websockets.connect(second.ws_url(ticket)) as ws:
                results: dict[str, object] = {}
                for i, tid in enumerate(("a" * 26, "b" * 26)):
                    await ws.send(
                        json.dumps(
                            {
                                "type": "req",
                                "id": f"r{i}",
                                "method": "task.status",
                                "params": {"taskId": tid},
                            }
                        )
                    )
                    raw = json.loads(await ws.recv())
                    assert raw.get("ok") is True, raw
                    results[tid] = raw["result"]["data"]
                return results

        got = asyncio.run(query())
        running_after = got["a" * 26]
        finished_after = got["b" * 26]
        assert isinstance(running_after, dict) and running_after.get("state") == "interrupted"
        assert isinstance(finished_after, dict) and finished_after.get("state") == "succeeded"
        assert finished_after.get("candidateId") == "cand-ok"
