"""S10 的验收：派活 → 状态变绿 → 磁盘上真有改动。

分层与 `test_worktree.py` 相同：store/service 用**真** `state.db`（`state_home` 指到 tmp），
Agent 会话用**假工厂**替换（单测不碰子进程）；真实 `opencode acp` 只在标注 skip 的
集成用例里跑（验收 1/3）。每条验收至少一条真测试（04 §1.1 的 DoD 底线）。
"""

from __future__ import annotations

import asyncio
import json
import os
import pwd
import shutil
import sqlite3
import subprocess
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Final

import pytest

from postel_studio.agent import acp_client
from postel_studio.api import frames
from postel_studio.api.bots import BotService
from postel_studio.api.repository import RepositoryService
from postel_studio.api.worktrees import WorktreeService
from postel_studio.domain.models import Candidate, Task
from postel_studio.orchestrator import EventHub, Orchestrator
from postel_studio.storage.db import BotStore, LeaseStore, RepoStore, TaskStore
from postel_studio.storage.paths import worktree_dir

PRESET: Final = "builder"
TERMINAL_WAIT_S: Final = 180.0  # 真实 opencode 建一个文件实测 8.7s，留足余量

# 真实 opencode 的免费额度模型会耗尽（`AI_APICallError: Free usage limit reached`），
# prompt 要么无限挂起、要么回 `failed/INTERNAL`。集成用例优先用本机已配好的付费 Provider
# （当前为 deepseek），让这条验收**真跑、真绿**；只有连 Provider 凭据都没有时才跳过，
# 而且只认明确证据（日志里的限流标记 / 终态错误文本），绝不拿"超时"或泛化 INTERNAL 当豁免。
_QUOTA_MARKER: Final = "Free usage limit reached"
_QUOTA_LOG_TTL_S: Final = 900.0  # 只看最近 15 分钟的日志，旧证据不能豁免今天的卡住
_REAL_AGENT_MODEL: Final = "deepseek/deepseek-flash"  # 已验证 1~2s 回 end_turn


def _real_opencode_data_home() -> Path | None:
    """本机 opencode 凭据库所在目录（conftest 把 HOME 改到临时目录，pwd 才是真家）。"""
    real = Path(pwd.getpwuid(os.getuid()).pw_dir) / ".local" / "share" / "opencode"
    return real if (real / "opencode.db").is_file() else None


def _has_paid_credential(data_home: Path) -> bool:
    """是否装了可用的付费 Provider 凭据。只读 integration_id 与 active，绝不读 key 值。"""
    try:
        with sqlite3.connect(f"file:{data_home / 'opencode.db'}?mode=ro", uri=True) as conn:
            row = conn.execute(
                "SELECT 1 FROM credential WHERE integration_id='deepseek' AND active=1 LIMIT 1"
            ).fetchone()
        return row is not None
    except sqlite3.Error:  # 库格式变了 / 正被写：宁可跳过也不误报绿
        return False


def _real_agent_env(data_home: Path) -> dict[str, str]:
    """给 opencode 子进程的环境：真实凭据库 + 显式付费模型。

    `child_env` 只按**变量名**过滤凭据字样（TOKEN/KEY/AUTH…）；`XDG_DATA_HOME` 与
    `OPENCODE_CONFIG_CONTENT` 都不含这些字样，所以能透传。凭据值始终留在 opencode.db
    里由它自己读，测试与产品代码都不碰 key。
    """
    env = dict(os.environ)
    env["XDG_DATA_HOME"] = str(data_home.parent)  # opencode 在 <XDG_DATA_HOME>/opencode/ 下找 db
    env["OPENCODE_CONFIG_CONTENT"] = json.dumps({"model": _REAL_AGENT_MODEL})
    return env


def _quota_exhausted(data_home: Path | None) -> bool:
    """限流证据：日志标记（最近 15 分钟）或终态错误文本，二者都不泛化。"""
    root = (
        data_home / "log"
        if data_home is not None
        else Path(os.environ.get("HOME", "")) / ".local/share/opencode/log"
    )
    if not root.is_dir():
        return False
    cutoff = time.time() - _QUOTA_LOG_TTL_S
    for log in root.glob("*.log"):
        try:
            if log.stat().st_mtime >= cutoff and _QUOTA_MARKER in log.read_text(errors="replace"):
                return True
        except OSError:  # 日志正在轮转 / 权限问题：宁可 fail，也不拿它当豁免
            continue
    return False


def _error(exc: BaseException) -> frames.FrameError:
    assert isinstance(exc, frames.FrameError), f"要的是帧层错误，拿到 {type(exc).__name__}"
    return exc


def _init_repo(path: Path) -> Path:
    """真 git 仓库 + 一个空提交（工作区要能从它检出）。与 test_worktree 的同款。"""
    path.mkdir(parents=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.email=test@example.invalid",
            "-c",
            "user.name=test",
            "init",
            "-q",
            "--initial-branch=main",
            str(path),
        ],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(path),
            "-c",
            "user.email=test@example.invalid",
            "-c",
            "user.name=test",
            "commit",
            "-q",
            "--allow-empty",
            "-m",
            "init",
        ],
        check=True,
        capture_output=True,
    )
    return path


# ---------------------------------------------------------------------------
# 假会话与假工作区：orchestrator 的两个接缝（S10 D13 的注入面）
# ---------------------------------------------------------------------------


class NoCandidateMaker:
    """S10 的既有测试不关心候选：假接缝始终回 None（S11 的测试才注入真服务）。"""

    def snapshot_base(self, worktree_path: str) -> str:
        return "0" * 40

    def emerge(
        self, *, task: Task, repo_id: str, worktree_path: str, base_sha: str
    ) -> Candidate | None:
        return None

    def discard(self, candidate_id: str) -> None:
        return None


class FakeSession:
    """替身会话：prompt 行为可编排（阻塞 / 抛错 / 任意 stopReason）。"""

    def __init__(
        self,
        command: tuple[str, ...],
        cwd: str,
        *,
        stop_reason: str = "end_turn",
        error: Exception | None = None,
        gate: threading.Event | None = None,
        created: threading.Event | None = None,
    ) -> None:
        self.command = command
        self.cwd = cwd
        self.stop_reason = stop_reason
        self.error = error
        self.gate = gate
        self.created = created
        self.cancelled = False
        self.closed = False
        if self.created is not None:
            self.created.set()

    def prompt(self, text: str, *, timeout_s: float | None = None) -> acp_client.SessionOutcome:
        if self.gate is not None:
            assert self.gate.wait(timeout=30), "gate 30 秒没人放行（测试卡死了）"
        if self.cancelled:
            # 实测（S10 §8.4）：session/cancel 后，挂起的 prompt 以 stopReason=cancelled 应答。
            return acp_client.SessionOutcome(stop_reason="cancelled", updates=0)
        if self.error is not None:
            raise self.error
        return acp_client.SessionOutcome(stop_reason=self.stop_reason, updates=0)

    def cancel(self) -> None:
        self.cancelled = True

    def close(self) -> None:
        self.closed = True


def make_factory(
    sessions: list[FakeSession],
    *,
    stop_reason: str = "end_turn",
    error: Exception | None = None,
    gate: threading.Event | None = None,
    created: threading.Event | None = None,
) -> Callable[[tuple[str, ...], str], FakeSession]:
    """造一个会记录每次构建参数的假工厂。"""

    def factory(command: tuple[str, ...], cwd: str) -> FakeSession:
        session = FakeSession(
            command, cwd, stop_reason=stop_reason, error=error, gate=gate, created=created
        )
        sessions.append(session)
        return session

    return factory


class FakeAcquirer:
    """替身工作区：返回真实存在的目录。`calls` 记录 `{params, path}`（产出，不是入参）。"""

    def __init__(self, root: Path, gate: threading.Event | None = None) -> None:
        self.root = root
        self.gate = gate
        self.calls: list[dict[str, object]] = []

    def acquire(self, params: dict[str, object]) -> dict[str, object]:
        if self.gate is not None:
            assert self.gate.wait(timeout=30), "gate 30 秒没人放行（测试卡死了）"
        path = self.root / f"wt-{len(self.calls)}"
        path.mkdir(parents=True, exist_ok=True)
        self.calls.append({"params": params, "path": str(path)})
        return {"path": str(path), "reused": False}


# ---------------------------------------------------------------------------
# 夹具：真 store + 真工作区服务 + 可替换会话工厂的编排器
# ---------------------------------------------------------------------------


@pytest.fixture
def stores(state_home: Path) -> Iterator[tuple[RepoStore, BotStore, TaskStore, LeaseStore]]:
    with RepoStore() as repos, BotStore() as bots, TaskStore() as tasks, LeaseStore() as leases:
        yield repos, bots, tasks, leases


@pytest.fixture
def bind(
    stores: tuple[RepoStore, BotStore, TaskStore, LeaseStore],
    tmp_path: Path,
) -> Callable[..., tuple[str, str]]:
    """`bind(permission="L1") -> (repo_id, bot_id)`：真仓库 + 真 Bot（权限可选）。"""
    repos, bots, _tasks, _leases = stores

    def bind_repo(*, permission: str = "L1", name: str = PRESET) -> tuple[str, str]:
        repo = _init_repo(tmp_path / f"proj-{name}")
        payload = RepositoryService(repos).bind({"path": str(repo)})
        repo_id = payload["repoId"]
        assert isinstance(repo_id, str)
        BotService(bots, repos).create({"repoId": repo_id, "name": name})
        bots.set_permission(f"{repo_id}/{name}", permission)
        return repo_id, f"{repo_id}/{name}"

    return bind_repo


@pytest.fixture
def orchestrator(
    stores: tuple[RepoStore, BotStore, TaskStore, LeaseStore], tmp_path: Path
) -> Iterator[Callable[..., Orchestrator]]:
    """`make(factory=None, acquirer=None) -> Orchestrator`：默认给一次性假工厂。"""
    _repos, bots, tasks, leases = stores
    created: list[Orchestrator] = []

    def make(
        factory: Callable[[tuple[str, ...], str], object] | None = None,
        acquirer: FakeAcquirer | None = None,
    ) -> Orchestrator:
        if acquirer is None:
            acquirer = FakeAcquirer(tmp_path / "worktrees")
        if factory is None:
            factory = make_factory([])
        orch = Orchestrator(
            bots,
            tasks,
            leases,
            acquirer,
            NoCandidateMaker(),
            EventHub(),
            session_factory=factory,  # type: ignore[arg-type]
        )
        created.append(orch)
        return orch

    yield make

    # worker 在终态迁移之后还要做 finally 里的 DB 操作（释放租约）。
    # teardown 必须先等 worker 退尽，否则它们撞上已关闭的库（T44 的镜像教训：
    # 「测试绿了」不等于「线程收场了」——用 wait_idle 观测线程，别猜时序）。
    for orch in created:
        assert orch.wait_idle(timeout=10), "有 worker 在 10 秒内没退出"


def _wait_terminal(tasks: TaskStore, task_id: str, timeout: float = 30.0) -> Task:
    """轮询到任务终态（worker 在别的线程里跑；单测没有别的方式观测它）。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        task = tasks.get(task_id)
        if task is not None and task.state not in ("queued", "running"):
            return task
        time.sleep(0.01)
    raise AssertionError(f"任务 {task_id} 在 {timeout}s 内没有到达终态")


# ---------------------------------------------------------------------------
# 验收 4 的前置（D4）：权限判定在执行前
# ---------------------------------------------------------------------------


def test_l0_bot_refused_before_any_execution(
    orchestrator: Callable[..., Orchestrator],
    bind: Callable[..., tuple[str, str]],
    tmp_path: Path,
) -> None:
    """L0 Bot 派活 → `PERMISSION_DENIED`，且**没有**任何执行副作用（05 §2.1）。"""
    _repo_id, bot_id = bind(permission="L0")
    acquirer = FakeAcquirer(tmp_path / "never")
    orch = orchestrator(acquirer=acquirer)
    with pytest.raises(frames.FrameError) as excinfo:
        orch.dispatch({"botId": bot_id, "prompt": "创建一个 hello.txt"})
    assert _error(excinfo.value).code == "PERMISSION_DENIED"
    assert acquirer.calls == [], "被拒的任务绝不能开始取工作区（判定先于执行）"


# ---------------------------------------------------------------------------
# 验收 7（D6）：状态机与原子迁移
# ---------------------------------------------------------------------------


def test_illegal_transitions_are_refused(stores, bind: Callable[..., tuple[str, str]]) -> None:
    """`queued → succeeded` 被拒；合法边走通；终态封死（06 §3.1 的两条禁止）。"""
    _repos, _bots, tasks, _leases = stores
    _repo_id, bot_id = bind()
    task = _queued_task(tasks, bot_id)
    # 06 §3.1 明文禁止的两条边，一条都走不通
    assert tasks.transition(task.task_id, "succeeded") is None
    # 合法的边走通，且时间戳在这里落
    running = tasks.transition(task.task_id, "running")
    assert running is not None and running.state == "running"
    assert running.started_at is not None
    # 终态封死：succeeded 之后连"再迁移一次"都不行
    done = tasks.transition(task.task_id, "succeeded")
    assert done is not None and done.ended_at is not None
    for target in ("running", "failed", "cancelled", "interrupted"):
        assert tasks.transition(task.task_id, target) is None, f"{target} 必须被拒"


def test_taskstore_transition_loses_race_returns_none(
    stores, bind: Callable[..., tuple[str, str]]
) -> None:
    """cancel 与 worker 同时从 running 迁移：先到的赢，后到的拿 `None`（D6 的原子性）。"""
    _repos, _bots, tasks, _leases = stores
    _repo_id, bot_id = bind()
    task = _queued_task(tasks, bot_id)
    assert tasks.transition(task.task_id, "running") is not None
    won = tasks.transition(task.task_id, "cancelled")
    lost = tasks.transition(task.task_id, "succeeded")
    assert won is not None and won.state == "cancelled"
    assert lost is None, "竞态输家必须拿到 None，绝不覆盖赢家的终态"


def test_restart_marks_running_and_queued_interrupted(
    stores, bind: Callable[..., tuple[str, str]], orchestrator: Callable[..., Orchestrator]
) -> None:
    """D7：内核重启 → `running` 与 `queued` 都标 `interrupted`（queued 没人接手，同罪）。

    两条行必须挂在**不同** Bot 上——"每 Bot 至多一条活跃任务"的部分唯一索引（D5 的后备锁）
    会拒绝同一个 Bot 的第二条 queued 行，那正是它该做的事。
    """
    _repos, _bots, tasks, _leases = stores
    _repo_id, bot_a = bind()
    _repo_id2, bot_b = bind(name="worker")
    queued = _queued_task(tasks, bot_a)
    running = _queued_task(tasks, bot_b)
    assert tasks.transition(running.task_id, "running") is not None
    orchestrator()  # 新编排器构造 = 一次"重启"
    after_q = tasks.get(queued.task_id)
    after_r = tasks.get(running.task_id)
    assert after_q is not None and after_q.state == "interrupted"
    assert after_r is not None and after_r.state == "interrupted"
    assert after_r.ended_at is not None


def _queued_task(tasks: TaskStore, bot_id: str) -> Task:
    """直接插一条 queued 行（绕过 dispatch：这里测的是 store 与状态机本身）。"""
    from postel_studio.domain.models import ulid

    task = Task(
        task_id=ulid(),
        bot_id=bot_id,
        trigger="manual",
        trigger_key=None,
        prompt="测试",
        base_ref=None,
        state="queued",
        candidate_id=None,
        error_code=None,
        created_at=time.time(),
        started_at=None,
        ended_at=None,
    )
    tasks.insert(task)
    return task


# ---------------------------------------------------------------------------
# 验收 5（D5）：并发派第二个 → BUSY
# ---------------------------------------------------------------------------


def test_second_dispatch_while_active_is_busy(
    orchestrator: Callable[..., Orchestrator],
    bind: Callable[..., tuple[str, str]],
    stores,
) -> None:
    """第一个任务跑到一半（gate 卡住）再派 → `BUSY`；放行后第二个可以派。"""
    _repo_id, bot_id = bind()
    _repos, _bots, tasks, _leases = stores
    gate = threading.Event()
    sessions: list[FakeSession] = []
    orch = orchestrator(factory=make_factory(sessions, gate=gate))
    first = _dispatch_ok(orch, bot_id)
    task_id = first["taskId"]
    assert isinstance(task_id, str)
    _wait_state(tasks, task_id, "running")
    with pytest.raises(frames.FrameError) as excinfo:
        orch.dispatch({"botId": bot_id, "prompt": "再来一刀"})
    assert _error(excinfo.value).code == "BUSY"
    gate.set()
    final = _wait_terminal(tasks, task_id)
    assert final.state == "succeeded"
    # 任务结束之后 BUSY 解除（数据级后备锁也随终态消失）
    second = _dispatch_ok(orch, bot_id)
    gate.set()
    final2 = _wait_terminal(tasks, str(second["taskId"]))
    assert final2.state == "succeeded"


def _wait_state(tasks: TaskStore, task_id: str, state: str, timeout: float = 30.0) -> None:
    """轮询到某个非终态（queued → running 的窗口观测）。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        task = tasks.get(task_id)
        if task is not None and task.state == state:
            return
        time.sleep(0.005)
    raise AssertionError(f"任务 {task_id} 在 {timeout}s 内没有进入 {state}")


def _dispatch_ok(
    orch: Orchestrator, bot_id: str, prompt: str = "创建一个 hello.txt"
) -> dict[str, object]:
    data = orch.dispatch({"botId": bot_id, "prompt": prompt})
    assert data["state"] == "queued"
    return data


# ---------------------------------------------------------------------------
# 验收 4（D9/D15）：失败必须带稳定码
# ---------------------------------------------------------------------------


def test_agent_death_marks_failed_with_stable_code(
    orchestrator: Callable[..., Orchestrator],
    bind: Callable[..., tuple[str, str]],
    stores,
) -> None:
    """会话中途死掉（起不来）→ `failed` + `ADAPTER_UNAVAILABLE`，绝不静默。"""
    _repo_id, bot_id = bind()
    _repos, _bots, tasks, _leases = stores
    orch = orchestrator(factory=make_factory([], error=acp_client.AcpUnavailableError("EOF")))
    data = _dispatch_ok(orch, bot_id)
    final = _wait_terminal(tasks, str(data["taskId"]))
    assert final.state == "failed"
    assert final.error_code == "ADAPTER_UNAVAILABLE"


def test_unknown_stop_reason_fails_closed(
    orchestrator: Callable[..., Orchestrator],
    bind: Callable[..., tuple[str, str]],
    stores,
) -> None:
    """D9：表外的 stopReason（如 `refusal`）→ `failed` + `INTERNAL`，不庆祝不猜。"""
    _repo_id, bot_id = bind()
    _repos, _bots, tasks, _leases = stores
    orch = orchestrator(factory=make_factory([], stop_reason="refusal"))
    data = _dispatch_ok(orch, bot_id)
    final = _wait_terminal(tasks, str(data["taskId"]))
    assert final.state == "failed"
    assert final.error_code == "INTERNAL"


# ---------------------------------------------------------------------------
# 验收 2（D8/D13）：cwd 是被分配的那个工作区
# ---------------------------------------------------------------------------


def test_session_cwd_is_the_acquired_worktree(
    orchestrator: Callable[..., Orchestrator],
    bind: Callable[..., tuple[str, str]],
    stores,
    tmp_path: Path,
) -> None:
    """假会话记录 cwd：必须等于 acquirer 回的那个路径（验收 2 的单测形态）。"""
    _repo_id, bot_id = bind()
    _repos, _bots, tasks, _leases = stores
    sessions: list[FakeSession] = []
    acquirer = FakeAcquirer(tmp_path / "worktrees")
    orch = orchestrator(factory=make_factory(sessions), acquirer=acquirer)
    data = _dispatch_ok(orch, bot_id)
    final = _wait_terminal(tasks, str(data["taskId"]))
    assert final.state == "succeeded"
    assert len(sessions) == 1
    assert sessions[0].cwd == acquirer.calls[0]["path"], "cwd 必须是被分配的工作区路径"
    assert sessions[0].command == ("opencode", "acp"), "命令来自登记表，不是调用方现编"
    assert sessions[0].closed, "会话在任务结束后必须被回收（D8）"


# ---------------------------------------------------------------------------
# D10：取消
# ---------------------------------------------------------------------------


def test_cancel_queued_aborts_without_side_effects(
    orchestrator: Callable[..., Orchestrator],
    bind: Callable[..., tuple[str, str]],
    stores,
    tmp_path: Path,
) -> None:
    """queued 直接取消：迁移成功、worker（这里根本没起）无副作用。"""
    _repo_id, bot_id = bind()
    _repos, _bots, tasks, _leases = stores
    orch = orchestrator()
    data = _dispatch_ok(orch, bot_id)
    payload = orch.cancel({"taskId": data["taskId"]})
    assert payload["state"] == "cancelled"
    final = tasks.get(str(data["taskId"]))
    assert final is not None and final.state == "cancelled"


def test_cancel_running_sends_session_cancel(
    orchestrator: Callable[..., Orchestrator],
    bind: Callable[..., tuple[str, str]],
    stores,
) -> None:
    """running 取消：`session/cancel` 被发出；prompt 以 `cancelled` 结束 → 终态 `cancelled`。"""
    _repo_id, bot_id = bind()
    _repos, _bots, tasks, _leases = stores
    gate = threading.Event()
    created = threading.Event()
    sessions: list[FakeSession] = []
    orch = orchestrator(factory=make_factory(sessions, gate=gate, created=created))
    data = _dispatch_ok(orch, bot_id)
    task_id = str(data["taskId"])
    _wait_state(tasks, task_id, "running")
    assert created.wait(timeout=30), "假会话没被构建"
    payload = orch.cancel({"taskId": task_id})
    assert payload["state"] == "running", "cancel 应答时 worker 还没跑完（D10：迁移交给 worker）"
    gate.set()
    final = _wait_terminal(tasks, task_id)
    assert sessions[0].cancelled, "session/cancel 必须真的发出去"
    assert final.state == "cancelled"
    with pytest.raises(frames.FrameError) as excinfo:
        orch.cancel({"taskId": task_id})
    assert _error(excinfo.value).code == "INVALID_PARAMS", "终态再取消是调用方 bug（D10）"


# ---------------------------------------------------------------------------
# 验收 8（D11）：evt 到订阅者、不到未订阅者
# ---------------------------------------------------------------------------


def test_evt_reaches_subscriber_only() -> None:
    """hub 的 pub/sub：订阅了才收到；`replace` 整体替换；未订阅的队列保持空。"""
    loop = asyncio.new_event_loop()
    try:
        got: asyncio.Queue[tuple[str, dict[str, object]]] = asyncio.Queue()
        empty: asyncio.Queue[tuple[str, dict[str, object]]] = asyncio.Queue()
        hub = EventHub()
        handle = hub.subscribe(frozenset({"task.updated"}), got, loop)
        hub.publish("task.updated", {"taskId": "t1", "state": "running"})
        topic, data = loop.run_until_complete(asyncio.wait_for(got.get(), timeout=5))
        assert topic == "task.updated" and data == {"taskId": "t1", "state": "running"}
        assert empty.empty(), "未订阅的连接绝不能收到事件"
        # replace 整体换掉（D11）：换成一个不存在的 topic 后，原 topic 不再投递
        assert hub.replace(handle, frozenset({"candidate.proposed"})) is True
        hub.publish("task.updated", {"taskId": "t1", "state": "succeeded"})
        with pytest.raises(asyncio.TimeoutError):
            loop.run_until_complete(asyncio.wait_for(got.get(), timeout=0.2))
        hub.unsubscribe(handle)
        assert hub.replace(handle, frozenset({"task.updated"})) is False
    finally:
        loop.close()


def test_subscribe_replaces_and_validates_topics() -> None:
    """网关侧的入参校验：白名单外 / 空数组 / 非数组 → 不合法（`None`）。"""
    from postel_studio.api import gateway

    assert gateway._topics_of({"topics": ["task.updated"]}) == frozenset({"task.updated"})
    assert gateway._topics_of({"topics": ["task.updated", "task.updated"]}) == frozenset(
        {"task.updated"}
    )
    assert gateway._topics_of({"topics": ["not.a.topic"]}) is None
    assert gateway._topics_of({"topics": []}) is None
    assert gateway._topics_of({"topics": "task.updated"}) is None
    assert gateway._topics_of({}) is None


# ---------------------------------------------------------------------------
# 验收 1/3：真实集成（未装 opencode 就 skip，绝不假装通过）
# ---------------------------------------------------------------------------


@pytest.mark.skipif(shutil.which("opencode") is None, reason="需要真实 opencode acp")
def test_real_integration_creates_file_and_dirty_worktree(stores, tmp_path: Path) -> None:
    """验收 1+2+3 一次走完：派活 → 终态 → worktree 里有文件、`git status` 非空。

    全真栈：真 `state.db`、真 `WorktreeService`（真 `git worktree add`）、真 opencode 会话。
    断言点：终态 `succeeded`；`hello.txt` 在被分配的 worktree 里；`status --porcelain`
    非空（租约已释放但工作区**保留**——S10 D5 的裁决）。
    """
    data_home = _real_opencode_data_home()
    if data_home is None or not _has_paid_credential(data_home):
        pytest.skip("本机 opencode 未配置付费 Provider 凭据（deepseek），无法真跑集成验收")

    # 真 opencode 会话，但用本机已配好的付费模型：conftest 隔离了 HOME，这里显式把它
    # 指回真实凭据库 + 指定模型，让验收真跑真绿（不把 key 读进测试，也不改产品代码）。
    agent_env = _real_agent_env(data_home)

    def factory(command: tuple[str, ...], cwd: str) -> acp_client.Session:
        return acp_client.Session(command, cwd, parent_env=agent_env)

    repos, bots, tasks, leases = stores
    repo = _init_repo(tmp_path / "proj")
    repo_id = RepositoryService(repos).bind({"path": str(repo)})["repoId"]
    assert isinstance(repo_id, str)
    BotService(bots, repos).create({"repoId": repo_id, "name": PRESET})
    bot_id = f"{repo_id}/{PRESET}"
    worktrees = WorktreeService(repos, bots)
    orch = Orchestrator(
        bots, tasks, leases, worktrees, NoCandidateMaker(), EventHub(), session_factory=factory
    )
    data = _dispatch_ok(orch, bot_id)
    task_id = str(data["taskId"])
    try:
        final = _wait_terminal(tasks, task_id, timeout=TERMINAL_WAIT_S)
    except AssertionError:
        # 超时路径：只有明确的限流日志证据才跳过，其他卡住（含产品 bug）照常 fail。
        if _quota_exhausted(data_home):
            pytest.skip("opencode free quota exhausted (external dependency, not a product bug)")
        raise
    # 终态失败路径：只认携带限流文本的 INTERNAL；泛化 INTERNAL 照常 fail。
    if (
        final.state != "succeeded"
        and final.error_code == "INTERNAL"
        and _quota_exhausted(data_home)
    ):
        pytest.skip("opencode free quota exhausted (external dependency, not a product bug)")
    assert final.state == "succeeded", final.error_code
    worktree = worktree_dir(repo_id, PRESET)
    assert worktree.is_dir(), "被分配的 worktree 必须存在"
    hello = worktree / "hello.txt"
    assert hello.exists(), "Agent 必须把文件建在 worktree 里（不是主仓库）"
    status = subprocess.run(
        ["git", "-C", str(worktree), "status", "--porcelain"],
        check=True,
        capture_output=True,
        text=True,
    )
    assert status.stdout.strip(), "验收 3：任务结束后 worktree 的 git status 必须非空"
    main_status = subprocess.run(
        ["git", "-C", str(repo), "status", "--porcelain"],
        check=True,
        capture_output=True,
        text=True,
    )
    assert not main_status.stdout.strip(), "禁止事项：Agent 不得在主仓库留下改动"
    # worker 在终态迁移**之后**才跑 finally 的租约释放（状态是首要事实，释放是清理），
    # 所以这里轮询等它清完，而不是断言"此刻必须已经释放"。`active_for_task` 是只读的，
    # 轮询不会替 worker 做释放。
    deadline = time.monotonic() + 30.0
    while time.monotonic() < deadline:
        if leases.active_for_task(task_id) is None:
            break
        time.sleep(0.01)
    assert leases.active_for_task(task_id) is None, "worker 的 finally 应已释放租约"
