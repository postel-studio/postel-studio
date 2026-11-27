"""派活编排（S10）：帧回答 `queued`，工作线程拥有全部慢活（D1/D2）。

本模块是 02 §2 `task` Service 的落点。三条纪律：

1. **不 import 任何 `api/*` handler**（D13）：`WorktreeService` 由 `app` 装配时注入，
   环靠构造消掉而不是靠豁免。允许的依赖只有 `agent/`、`storage/`、`domain/`、`frames`。
2. **权限判定在执行前**（D4 / 05 §2.1）：`dispatch` 先查 Bot 权限再建行、再起线程；
   工作线程不再查——它只可能被通过了判定的 dispatch 启动，判定与执行之间没有第二条路。
3. **状态机只经 `TaskStore.transition`**（D6）：并发下（cancel 与 worker 同时迁移）输家
   拿到 `None`，把行当终态处理——绝不重试，绝不覆盖。
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import threading
import time
from typing import Final, Protocol

from postel_studio.agent import acp_client
from postel_studio.agent import registry as agent_registry
from postel_studio.api import frames
from postel_studio.domain.models import (
    TASK_ACTIVE,
    Bot,
    Permission,
    Task,
    WorktreeLease,
    permission_covers,
    prompt_ok,
    ref_name_ok,
    ulid,
)
from postel_studio.storage.db import BotStore, LeaseStore, TaskStore

logger = logging.getLogger(__name__)

INVALID_PARAMS: Final = frames.INVALID_PARAMS
INTERNAL: Final = frames.INTERNAL
BUSY: Final = "BUSY"
PERMISSION_DENIED: Final = "PERMISSION_DENIED"
ADAPTER_UNAVAILABLE: Final = "ADAPTER_UNAVAILABLE"
ADAPTER_TIMEOUT: Final = "ADAPTER_TIMEOUT"

MSG_TASK_ID_INVALID: Final = "taskId 参数缺失或不是字符串"
MSG_TASK_NOT_FOUND: Final = "找不到这个任务"
MSG_TASK_FINISHED: Final = "任务已结束，不能再取消"
MSG_BOT_ID_INVALID: Final = "botId 参数缺失或不是字符串"
MSG_BOT_NOT_FOUND: Final = "找不到这个 Bot"
MSG_PROMPT_INVALID: Final = "prompt 必须是非空文本（≤ 32 KiB）"
MSG_BASE_REF_INVALID: Final = "baseRef 参数不合法"
MSG_PERMISSION_DENIED: Final = "这个 Bot 是 L0（只读），不能派活"
MSG_BUSY: Final = "该 Bot 已有任务在跑"
MSG_PROTOCOL: Final = "Agent 协议失败"
MSG_UNEXPECTED: Final = "任务执行失败"

# 派活需要的最低权限（D4）：任务写工作区 = L1 的授权范围。
DISPATCH_MIN_PERMISSION: Final = Permission.L1


class WorktreeAcquirer(Protocol):
    """`WorktreeService.acquire` 的形状（注入用；orchestrator 不 import 它）。"""

    def acquire(self, params: dict[str, object]) -> dict[str, object]: ...


class SessionFactory(Protocol):
    """`acp_client.Session` 的形状（测试用假会话替换）。"""

    def __call__(self, command: tuple[str, ...], cwd: str) -> acp_client.Session: ...


class EventHub:
    """线程安全的 pub/sub（D11）。

    订阅者 = 一条网关连接（一个 `asyncio.Queue` + topic 集合）。publish 从**工作线程**
    发生，而 `asyncio.Queue` 不是线程安全的——所以投递走 `loop.call_soon_threadsafe`：
    真正的入队发生在连接所属的事件循环里，顺序与帧序一致。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._subs: dict[
            int,
            tuple[
                frozenset[str],
                asyncio.Queue[tuple[str, dict[str, object]]],
                asyncio.AbstractEventLoop,
            ],
        ] = {}
        self._next_handle = 1

    def subscribe(
        self,
        topics: frozenset[str],
        queue: asyncio.Queue[tuple[str, dict[str, object]]],
        loop: asyncio.AbstractEventLoop,
    ) -> int:
        """登记一条连接。返回句柄（`replace` / `unsubscribe` 用）。"""
        with self._lock:
            handle = self._next_handle
            self._next_handle += 1
            self._subs[handle] = (frozenset(topics), queue, loop)
        return handle

    def replace(self, handle: int, topics: frozenset[str]) -> bool:
        """把一条已登记连接的 topic 集合**整个换掉**（D11：重复 subscribe = 替换）。"""
        with self._lock:
            entry = self._subs.get(handle)
            if entry is None:
                return False
            self._subs[handle] = (frozenset(topics), entry[1], entry[2])
        return True

    def unsubscribe(self, handle: int) -> None:
        """注销（连接断开时由网关调用；重复调用无害）。"""
        with self._lock:
            self._subs.pop(handle, None)

    def publish(self, topic: str, data: dict[str, object]) -> None:
        """向所有订了这个 topic 的连接投递。循环已死的连接静默跳过（网关自己会清理）。"""
        with self._lock:
            subscribers = list(self._subs.values())
        for topics, queue, loop in subscribers:
            if topic not in topics:
                continue
            try:
                loop.call_soon_threadsafe(queue.put_nowait, (topic, data))
            except RuntimeError:  # 事件循环已关：连接正在死，网关的 finally 会注销它
                continue


class Orchestrator:
    """`task.dispatch` / `task.status` / `task.cancel` 的业务编排（S10）。

    构造时做两件一次性的事：把上次内核留下的活跃任务标 `interrupted`（D7）；
    之后每个 dispatch 起一个 worker 线程，一任务一线程（D8）。
    """

    def __init__(
        self,
        bots: BotStore,
        tasks: TaskStore,
        leases: LeaseStore,
        worktrees: WorktreeAcquirer,
        hub: EventHub,
        *,
        session_factory: SessionFactory | None = None,
    ) -> None:
        self._bots = bots
        self._tasks = tasks
        self._leases = leases
        self._worktrees = worktrees
        self._hub = hub
        self._factory: SessionFactory = session_factory or self._default_session
        self._sessions: dict[str, acp_client.Session] = {}  # task_id → 活会话（cancel 用）
        self._sessions_lock = threading.Lock()
        self._workers: set[threading.Thread] = set()  # 活着的 worker（wait_idle / S15 收场用）
        self._cancel_requested: set[str] = set()  # 会话建好前到达的 cancel（D10 的窗口）
        self._tasks.mark_interrupted()  # D7：重启记账。queued 也标（同 D7 的理由）

    # -- 帧方法 --------------------------------------------------------------
    def dispatch(self, params: dict[str, object]) -> dict[str, object]:
        """`task.dispatch`（07 §4.3）。校验 → 判权 → BUSY 检查 → 建行 → 起线程 → 应答。

        这里**只做**入参侧的事；任何可能超过毫秒级的工作（git、子进程）都在 worker 里。
        `tasks.insert` 撞活跃索引是 BUSY 的数据级后备（D5）——检查-再插入的竞态由它兜底。
        """
        bot = self._require_bot(params)
        prompt = params.get("prompt")
        if not prompt_ok(prompt):
            raise self._fail(INVALID_PARAMS, MSG_PROMPT_INVALID)
        base_ref = params.get("baseRef")
        if base_ref is not None and (not isinstance(base_ref, str) or not ref_name_ok(base_ref)):
            raise self._fail(INVALID_PARAMS, MSG_BASE_REF_INVALID)
        if not permission_covers(bot.permission, DISPATCH_MIN_PERMISSION):  # D4：先于一切执行
            raise self._fail(PERMISSION_DENIED, MSG_PERMISSION_DENIED)
        if self._tasks.active_for_bot(bot.bot_id) is not None:
            raise self._fail(BUSY, MSG_BUSY)
        task = Task(
            task_id=ulid(),
            bot_id=bot.bot_id,
            trigger="manual",
            trigger_key=None,
            prompt=prompt if isinstance(prompt, str) else "",
            base_ref=base_ref if isinstance(base_ref, str) else None,
            state="queued",
            candidate_id=None,
            error_code=None,
            created_at=time.time(),
            started_at=None,
            ended_at=None,
        )
        try:
            self._tasks.insert(task)
        except sqlite3.IntegrityError:  # 竞态输家：检查之后别人插进了活跃行
            raise self._fail(BUSY, MSG_BUSY) from None
        worker = threading.Thread(
            target=self._run, args=(task, bot.repo_id), daemon=True, name=f"task-{task.task_id[:8]}"
        )
        with self._sessions_lock:
            self._workers.add(worker)
        worker.start()
        return {"taskId": task.task_id, "state": task.state, "botId": task.bot_id}

    def wait_idle(self, timeout: float = 10.0) -> bool:
        """等所有 worker 退出（测试收场与 S15 优雅停机都用它）。超时回 `False`。"""
        deadline = time.monotonic() + timeout
        for worker in self._snapshot_workers():
            remaining = max(0.0, deadline - time.monotonic())
            worker.join(remaining)
            if worker.is_alive():
                return False
        return True

    def _snapshot_workers(self) -> list[threading.Thread]:
        with self._sessions_lock:
            return list(self._workers)

    def status(self, params: dict[str, object]) -> dict[str, object]:
        """`task.status`（07 §2.2）：按 id 取任务载荷。不存在 → `INVALID_PARAMS`。"""
        task = self._require_task(params)
        return self._payload(task)

    def cancel(self, params: dict[str, object]) -> dict[str, object]:
        """`task.cancel`（07 §2.2）。queued 直接迁移；running 发会话取消，迁移交给 worker
        （实测：prompt 应答在 ~0.3s 内带 `stopReason: "cancelled"` 回来）。终态 → `INVALID_PARAMS`。
        """
        task = self._require_task(params)
        if task.state == "queued":
            updated = self._tasks.transition(task.task_id, "cancelled")
            if updated is not None:
                self._publish(updated)
                return self._payload(updated)
            return self._payload(self._require_task(params))  # 输了竞态：行已是终态，如实回它
        if task.state == "running":
            with self._sessions_lock:
                session = self._sessions.get(task.task_id)
                if session is None:  # 会话还没建好（正取工作区）：留记号，worker 建好立刻取消
                    self._cancel_requested.add(task.task_id)
            if session is not None:
                session.cancel()
            return self._payload(task)
        raise self._fail(INVALID_PARAMS, MSG_TASK_FINISHED)

    # -- 工作线程 --------------------------------------------------------------
    def _run(self, task: Task, repo_id: str) -> None:
        """一任务一线程（D1/D8）。全部慢活在这里：工作区 → 会话 → prompt → 记账。

        任何失败都翻成 `failed` + 稳定码（D15）：任务已经发生了，拒绝应答是撒谎。
        `finally` 三件事缺一不可：回收会话进程、释放租约、清登记——publish 在 finally
        之外做（线程栈展开时 publish 自己不许再抛）。
        """
        session: acp_client.Session | None = None
        try:
            # queued 阶段被取消：worker 什么都不做就退出（D10 的"无副作用"）。
            if self._is_terminal(task.task_id):
                return
            acquired = self._worktrees.acquire(
                {"repoId": repo_id, "botId": task.bot_id, "baseRef": task.base_ref}
            )
            path = acquired.get("path")
            if not isinstance(path, str) or not path:
                raise self._fail(INTERNAL, MSG_UNEXPECTED)
            self._leases.insert(
                WorktreeLease(
                    lease_id=ulid(),
                    bot_id=task.bot_id,
                    task_id=task.task_id,
                    path=path,
                    created_at=time.time(),
                    released_at=None,
                )
            )
            running = self._tasks.transition(task.task_id, "running")
            if running is None:  # cancel 赢了竞态
                return
            self._publish(running)
            channel = self._channel_for(task.bot_id)
            session = self._factory(channel.command, path)
            with self._sessions_lock:
                self._sessions[task.task_id] = session
                requested = task.task_id in self._cancel_requested
                self._cancel_requested.discard(task.task_id)
            if requested:  # cancel 在会话建好前到达（D10 的窗口收口）
                session.cancel()
            outcome = session.prompt(task.prompt)
            final = self._map_stop(outcome.stop_reason, task.task_id)
        except frames.FrameError as exc:  # acquire 的稳定码原样透传（REPO_NOT_BOUND 等）
            final = self._finish(task.task_id, "failed", exc.code)
        except acp_client.AcpUnavailableError as exc:
            final = self._finish(task.task_id, "failed", ADAPTER_UNAVAILABLE, str(exc))
        except acp_client.AcpTimeoutError:
            final = self._finish(task.task_id, "failed", ADAPTER_TIMEOUT)
        except (acp_client.AcpProtocolError, acp_client.AcpSessionError):
            final = self._finish(task.task_id, "failed", INTERNAL, MSG_PROTOCOL)
        except Exception as exc:  # 未预期：任务必须终态化，绝不能悬着（D15）
            final = self._finish(task.task_id, "failed", INTERNAL, str(exc))
        finally:
            if session is not None:
                session.close()  # D8：每条路径都回收
            self._leases.release(task.task_id)
            with self._sessions_lock:
                self._sessions.pop(task.task_id, None)
                self._cancel_requested.discard(task.task_id)
                self._workers.discard(threading.current_thread())
        self._publish(final)

    # -- 内部 ----------------------------------------------------------------
    def _is_terminal(self, task_id: str) -> bool:
        current = self._tasks.get(task_id)
        return current is None or current.state not in TASK_ACTIVE

    def _channel_for(self, bot_id: str) -> agent_registry.Channel:
        """D3（U18 收口）：`bot.agentKind` 优先，缺省用第一条已登记通道。"""
        bot = self._bots.get(bot_id)
        kind = bot.agent_kind if bot is not None else None
        if kind is not None:
            channel = agent_registry.resolve(kind)
            if channel is None:  # 库里存了未登记的 kind：这是坏数据，不是运行时状态
                raise self._fail(INVALID_PARAMS, f"未登记的通道：{kind}")
            return channel
        channels = agent_registry.channels()
        if not channels:
            raise self._fail(INTERNAL, "没有任何已登记通道")
        return channels[0]

    def _map_stop(self, stop_reason: str, task_id: str) -> Task:
        """D9：stopReason → 状态，fail-closed。end_turn 也不代表有改动（S11 才看磁盘）。"""
        if stop_reason == "end_turn":
            return self._finish(task_id, "succeeded", None)
        if stop_reason == "cancelled":
            return self._finish(task_id, "cancelled", None)
        return self._finish(task_id, "failed", INTERNAL, f"stopReason={stop_reason}")

    def _finish(
        self, task_id: str, state: str, error_code: str | None, detail: str | None = None
    ) -> Task:
        """终态迁移（竞态输家如实回读——绝不覆盖别人的终态）。"""
        updated = self._tasks.transition(task_id, state, error_code=error_code)
        if updated is None:
            current = self._tasks.get(task_id)
            assert current is not None  # 行刚被本线程读过，删行不在任何路径上
            return current
        if detail is not None and error_code is not None:
            logger.info("任务 %s 以 %s 结束：%s", task_id, error_code, detail)
        return updated

    def _publish(self, task: Task) -> None:
        """`task.updated`（D12 的载荷：恰好 taskId/state/errorCode?）。"""
        data: dict[str, object] = {"taskId": task.task_id, "state": task.state}
        if task.error_code is not None:
            data["errorCode"] = task.error_code
        self._hub.publish("task.updated", data)

    def _payload(self, task: Task) -> dict[str, object]:
        """Task → 帧载荷（camelCase 只在边界发生）。"""
        data: dict[str, object] = {
            "taskId": task.task_id,
            "botId": task.bot_id,
            "state": task.state,
            "prompt": task.prompt,
            "baseRef": task.base_ref,
            "createdAt": task.created_at,
        }
        if task.started_at is not None:
            data["startedAt"] = task.started_at
        if task.ended_at is not None:
            data["endedAt"] = task.ended_at
        if task.error_code is not None:
            data["errorCode"] = task.error_code
        return data

    def _require_bot(self, params: dict[str, object]) -> Bot:
        """botId 形状 + 存在性（S6/S7 同一条先例：归属由复合键本身保证）。"""
        value = params.get("botId")
        if not isinstance(value, str) or not value:
            raise self._fail(INVALID_PARAMS, MSG_BOT_ID_INVALID)
        bot = self._bots.get(value)
        if bot is None:
            raise self._fail(INVALID_PARAMS, MSG_BOT_NOT_FOUND)
        return bot

    def _require_task(self, params: dict[str, object]) -> Task:
        """taskId 形状 + 存在性。"""
        value = params.get("taskId")
        if not isinstance(value, str) or not value:
            raise self._fail(INVALID_PARAMS, MSG_TASK_ID_INVALID)
        task = self._tasks.get(value)
        if task is None:
            raise self._fail(INVALID_PARAMS, MSG_TASK_NOT_FOUND)
        return task

    @staticmethod
    def _default_session(command: tuple[str, ...], cwd: str) -> acp_client.Session:
        """生产路径的会话工厂（测试注入假工厂替换它）。"""
        return acp_client.Session(command, cwd)

    @staticmethod
    def _fail(code: str, message: str) -> frames.FrameError:
        return frames.FrameError(code, message)
