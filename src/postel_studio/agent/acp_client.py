"""ACP over stdio 的最小客户端：一次 `initialize`，一行应答，一个必然被回收的子进程。

S9 只做握手（`02b §4` 的第 1 步），不建会话、不发 prompt（那是 S10）。所有协议细节来自
实测（规格 §8.1），不是推测：`protocolVersion` 在顶层、`agentInfo` 可缺席、能力树里既有
布尔叶子也有**空对象**表示支持。

三条硬约束，改坏了测试会红：
- 命令是字面数组，`shell=False`（S9 D2 / `05 §3.3`）；
- 子进程环境不带凭据（S9 D8 / `06 T22`）；
- 无论成功、超时还是协议错，`finally` 里都回收（S9 D7）。
"""

from __future__ import annotations

import json
import os
import select
import subprocess
import threading
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

PROTOCOL_VERSION: Final = 1
DEFAULT_TIMEOUT_S: Final = 30.0
REAP_GRACE_S: Final = 5.0
_READ_CHUNK: Final = 65536

# S9 D8：名字里带这些字样（大小写无关）的变量一律不传给 Agent。`SSH_AUTH_SOCK` 命中
# `AUTH` 是**有意的**——子进程不该有能用用户密钥去 push 的路径（`05 §4.3` / `06 T22`）。
_CREDENTIAL_MARKERS: Final = ("TOKEN", "KEY", "SECRET", "PASSWORD", "CREDENTIAL", "AUTH")


class AcpUnavailableError(Exception):
    """命令不存在 / 起不来 / 没应答就退出。翻成 `ADAPTER_UNAVAILABLE`。"""


class AcpTimeoutError(Exception):
    """到点还没拿出应答。翻成 `ADAPTER_TIMEOUT`。"""


class AcpProtocolError(Exception):
    """应答不是合法的 `initialize` 结果。翻成 `INTERNAL`——说「没安装」是撒谎。"""


@dataclass(frozen=True)
class Handshake:
    """一次成功握手的全部产出。**没有一项来自源码常量**（S9 D3）。"""

    protocol_version: int
    agent_name: str | None
    agent_version: str | None
    flags: tuple[str, ...]


def child_env(parent: Mapping[str, str]) -> dict[str, str]:
    """D8：只搬运名字里不含凭据字样的变量。

    白名单会更严，但会一并挡住 `XDG_*`、`TERM` 这类 Agent 真正需要的东西；黑名单在这里
    **不会漏**，因为漏的唯一后果是某个变量没被过滤——而过滤规则本身是「不含即通过」，
    任何新出现的 `*_TOKEN` 都会自动命中。
    """
    return {
        name: value
        for name, value in parent.items()
        if not any(marker in name.upper() for marker in _CREDENTIAL_MARKERS)
    }


def flatten_capabilities(caps: object) -> tuple[str, ...]:
    """D3：把 `agentCapabilities` 摊平成稳定 flag 名，**并正确处理空对象**。

    实测（§8.1）三种形状并存：

    - 布尔叶子   `loadSession: true`                → `loadSession`
    - 命名空间   `mcpCapabilities: {http: true}`    → `mcp.http`
    - **空对象** `sessionCapabilities: {close: {}}` → `session.close`

    第三种最容易漏：ACP 用「键存在」表达支持，值只是该特性的选项容器。只读布尔的实现
    会把一个全能 Agent 报成「零会话能力」。返回排序结果，让页面与测试对顺序有共识。
    """
    if not isinstance(caps, dict):
        return ()
    flags: list[str] = []
    for key, value in caps.items():
        if not isinstance(key, str):
            continue
        if value is True:
            flags.append(key)
        elif isinstance(value, dict):
            suffix = "Capabilities"
            namespace = key[: -len(suffix)] if key.endswith(suffix) else key
            for sub_key, sub_value in value.items():
                if isinstance(sub_key, str) and sub_value is not False:
                    flags.append(f"{namespace}.{sub_key}")
    return tuple(sorted(flags))


def initialize(
    command: Sequence[str],
    *,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    parent_env: Mapping[str, str] | None = None,
    client_version: str = "0",
) -> Handshake:
    """起子进程 → 发 `initialize` → 读回 `id` 匹配的那一行 → 回收 → 解析。

    调用方只需要区分三种异常（见上面三个类的 docstring），不必知道管道细节。
    """
    request = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": PROTOCOL_VERSION,
            "clientCapabilities": {},
            "clientInfo": {"name": "postel-studio", "version": client_version},
        },
    }
    if isinstance(command, str):
        # 字符串会被当成一个整体可执行名（或更糟：被 shell 解释）。05 §3.3 要的是数组。
        raise AcpUnavailableError("命令必须是数组，不能是字符串（05 §3.3）")
    try:
        # 字面数组 + 无 shell（D2）。stderr 丢掉：它会打日志，但 S9 不用它。
        # `shell=False` 写成显式常量，`tests/test_bind.py` 的静态守卫按字面量查它。
        proc = subprocess.Popen(
            list(command),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            bufsize=0,
            shell=False,
            env=child_env(os.environ if parent_env is None else parent_env),
        )
    except OSError as exc:  # FileNotFoundError / PermissionError / EACCES 都在这条线上
        raise AcpUnavailableError(str(exc)) from exc
    try:
        payload = _exchange(proc, request, timeout_s)
    finally:
        reap(proc)
    return _parse(payload)


def reap(proc: subprocess.Popen[bytes]) -> None:
    """D7：无条件回收。先关 stdin（多数 ACP 会因此自行退出），再 terminate，最后 kill。"""
    try:
        if proc.stdin is not None and not proc.stdin.closed:
            proc.stdin.close()
    except OSError:
        pass
    if proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=REAP_GRACE_S)
        return
    except subprocess.TimeoutExpired:
        pass
    proc.kill()
    proc.wait(timeout=REAP_GRACE_S)


def _exchange(
    proc: subprocess.Popen[bytes], request: dict[str, object], timeout_s: float
) -> dict[str, object]:
    if proc.stdin is None or proc.stdout is None:  # pragma: no cover - PIPE 保证非 None
        raise AcpUnavailableError("子进程没有可用的管道")
    try:
        proc.stdin.write(json.dumps(request).encode("utf-8") + b"\n")
        proc.stdin.flush()
    except OSError as exc:
        raise AcpUnavailableError(f"写不进 Agent 的 stdin：{exc}") from exc
    return _read_matching(proc, request["id"], timeout_s)


def _read_matching(
    proc: subprocess.Popen[bytes], request_id: object, timeout_s: float | None
) -> dict[str, object]:
    """按行读，**跳过非 JSON 行**（D6），直到 `id` 对上或者到点。

    Agent 会往 stdout 打 banner 和日志；把第一行非 JSON 当协议错，会把一个健康的 Agent
    报成坏的。fail-closed 只适用于「数据缺失」（超时 / EOF / 没有 protocolVersion）。

    `timeout_s=None`（S10 D8）表示**无限等**：prompt 是分钟级，死线只会制造误报；
    这条模式下的出口只有应答、EOF 与取消（取消改变的是应答内容，不是这条循环）。
    """
    assert proc.stdout is not None
    deadline = None if timeout_s is None else time.monotonic() + timeout_s
    buffer = b""
    while True:
        if deadline is None:
            ready, _, _ = select.select([proc.stdout], [], [], None)
        else:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AcpTimeoutError(f"Agent 在 {timeout_s:g} 秒内没有应答")
            ready, _, _ = select.select([proc.stdout], [], [], remaining)
        if not ready:
            continue  # 下一轮 remaining 已 <= 0，从上面统一抛出
        chunk = os.read(proc.stdout.fileno(), _READ_CHUNK)
        if not chunk:
            raise AcpUnavailableError("Agent 在应答前关闭了 stdout")
        buffer += chunk
        while b"\n" in buffer:
            line, buffer = buffer.split(b"\n", 1)
            message = _try_json(line)
            if isinstance(message, dict) and message.get("id") == request_id:
                return message


def _try_json(line: bytes) -> object:
    """非 JSON 行（banner / 日志 / 空行）回 `None`，由调用方跳过。"""
    if not line.strip():
        return None
    try:
        return json.loads(line)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None


def _parse(message: dict[str, object]) -> Handshake:
    result = message.get("result")
    if not isinstance(result, dict):
        raise AcpProtocolError(f"握手没有 result 对象：{message.get('error')!r}")
    version = result.get("protocolVersion")
    if not isinstance(version, int) or isinstance(version, bool):
        raise AcpProtocolError("握手没有整数 protocolVersion")
    name: str | None = None
    agent_version: str | None = None
    info = result.get("agentInfo")
    if isinstance(info, dict):
        raw_name = info.get("name")
        raw_version = info.get("version")
        name = raw_name if isinstance(raw_name, str) else None
        agent_version = raw_version if isinstance(raw_version, str) else None
    return Handshake(
        protocol_version=version,
        agent_name=name,
        agent_version=agent_version,
        flags=flatten_capabilities(result.get("agentCapabilities")),
    )


# ---------------------------------------------------------------------------
# 会话级客户端（S10，02b §4 第 2-4 步）。实测依据见 S10 规格 §8。
# ---------------------------------------------------------------------------


class AcpSessionError(Exception):
    """会话期的协议失败（`session/new` 没给 sessionId、应答缺 stopReason 等）。翻成 `INTERNAL`。"""


@dataclass(frozen=True)
class ToolCall:
    """S16 D4：一次工具调用的命令与终态（给本地审计用，不进网关事件）。"""

    command: str
    status: str


@dataclass(frozen=True)
class SessionOutcome:
    """一次 prompt 的产出（D9 的原料）。`updates` 只作计数观测，内容**不外传**（S10 D12）。

    S16：额外带本轮工具调用摘要给本地审计——这不是向页面转发流式输出。
    """

    stop_reason: str
    updates: int
    tool_calls: tuple[ToolCall, ...] = ()


class Session:
    """一个任务一个进程（S10 D8）：spawn → initialize → session/new → prompt → reap。

    与 `initialize()` 的差别是**进程的生命周期横跨多次请求**：握手与建会话在构造时完成，
    `prompt()` 可以阻塞任意久（分钟级是常态），`cancel()` 允许从**另一个线程**调用——
    所以 stdin 的写有一把锁，读循环在调用方线程里转。所有路径都要走 `close()`；
    `reap` 是 S9 的同一份（先关 stdin，terminate，kill），这里只是包了 `__exit__`。
    """

    def __init__(
        self,
        command: Sequence[str],
        cwd: str,
        *,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        parent_env: Mapping[str, str] | None = None,
        client_version: str = "0",
    ) -> None:
        if isinstance(command, str):  # 与 initialize 同一条守卫：数组，不是字符串
            raise AcpUnavailableError("命令必须是数组，不能是字符串（05 §3.3）")
        if not isinstance(cwd, str) or not Path(cwd).is_absolute():
            # 02b §4：session/new 要**绝对** cwd。相对路径会让 Agent 自己 guess 进程 cwd，
            # 那"cwd 是被分配的工作区"（S10 验收 2）就无从断言。
            raise AcpSessionError("cwd 必须是绝对路径")
        try:
            self._proc = subprocess.Popen(
                list(command),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                bufsize=0,
                shell=False,
                cwd=cwd,
                env=child_env(os.environ if parent_env is None else parent_env),
            )
        except OSError as exc:
            raise AcpUnavailableError(str(exc)) from exc
        self._cwd = cwd
        self._stdin_lock = threading.Lock()
        self._next_id = 1
        self._session_id: str | None = None
        self.updates_seen = 0
        self._tool_calls: list[ToolCall] = []
        try:
            handshake = self._request(
                "initialize",
                {
                    "protocolVersion": PROTOCOL_VERSION,
                    "clientCapabilities": {},
                    "clientInfo": {"name": "postel-studio", "version": client_version},
                },
                timeout_s,
            )
            _parse(handshake)  # 校验结果形状（不存——能力检测是 agent.capabilities 的事）
            result = self._request("session/new", {"cwd": cwd, "mcpServers": []}, timeout_s)
            payload = result.get("result")
            if not isinstance(payload, dict) or not isinstance(payload.get("sessionId"), str):
                raise AcpProtocolError(f"session/new 没有回 sessionId：{result.get('error')!r}")
            self._session_id = payload["sessionId"]
        except BaseException:
            reap(self._proc)  # 构造失败也要回收（S9 D7 的纪律，一个例外都不留）
            raise

    # -- 会话动作 ------------------------------------------------------------
    @property
    def session_id(self) -> str:
        """会话 id（构造成功后必有）。"""
        assert self._session_id is not None  # 由 __init__ 保证
        return self._session_id

    @property
    def cwd(self) -> str:
        """传给 `session/new` 的绝对路径（S10 验收 2 的断言点）。"""
        return self._cwd

    def prompt(self, text: str, *, timeout_s: float | None = None) -> SessionOutcome:
        """投喂一句话并**等到这一轮结束**（D8：无默认死线）。

        期间混在 stdout 里的 `session/update` 通知按行丢弃（D12：S10 不转发流式输出），
        只计数。返回 `stopReason` 原文——`end_turn` / `cancelled` / 未知值都由调用方
        按 D9 映射，客户端不替产品做决定。
        """
        if self._session_id is None:  # pragma: no cover - 构造失败就不可能有实例
            raise AcpSessionError("会话尚未建立")
        self._tool_calls.clear()
        result = self._request(
            "session/prompt",
            {"sessionId": self._session_id, "prompt": [{"type": "text", "text": text}]},
            timeout_s,
        )
        payload = result.get("result")
        if not isinstance(payload, dict):
            raise AcpProtocolError(f"prompt 没有结果对象：{result.get('error')!r}")
        stop_reason = payload.get("stopReason")
        if not isinstance(stop_reason, str) or not stop_reason:
            # fail-closed：没有 stopReason 的"成功"不可信（D9）。
            raise AcpProtocolError("prompt 应答缺 stopReason")
        return SessionOutcome(
            stop_reason=stop_reason,
            updates=self.updates_seen,
            tool_calls=tuple(self._tool_calls),
        )

    def cancel(self) -> None:
        """发 `session/cancel` **通知**（无 id，实测：挂起的 prompt 随即回
        `stopReason: "cancelled"`）。可从任意线程调用；正在读的那条线程不受影响。"""
        if self._session_id is None:  # pragma: no cover
            raise AcpSessionError("会话尚未建立")
        self._send(
            {
                "jsonrpc": "2.0",
                "method": "session/cancel",
                "params": {"sessionId": self._session_id},
            }
        )

    def close(self) -> None:
        """无条件回收（S9 D7 的同一个 `reap`）。幂等：进程已死就直接返回。"""
        reap(self._proc)

    def __enter__(self) -> Session:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # -- 管道 ----------------------------------------------------------------
    def _send(self, message: dict[str, object]) -> None:
        """写一行 JSON-RPC。带锁：`cancel()` 从别的线程调用时与 `_request` 的写互斥。"""
        if self._proc.stdin is None:  # pragma: no cover - PIPE 保证非 None
            raise AcpUnavailableError("子进程没有可用的管道")
        with self._stdin_lock:
            try:
                self._proc.stdin.write(json.dumps(message).encode("utf-8") + b"\n")
                self._proc.stdin.flush()
            except OSError as exc:  # 进程已死 / 管道已断
                raise AcpUnavailableError(f"写不进 Agent 的 stdin：{exc}") from exc

    def _request(
        self, method: str, params: dict[str, object], timeout_s: float | None
    ) -> dict[str, object]:
        """发一个**带 id 的请求**并读回 id 对应的那一行（通知被跳过并计数）。"""
        self._next_id += 1
        request_id = self._next_id
        self._send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        assert self._proc.stdout is not None  # PIPE 保证非 None
        deadline = None if timeout_s is None else time.monotonic() + timeout_s
        buffer = b""
        while True:
            remaining = None
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise AcpTimeoutError(f"Agent 在 {timeout_s:g} 秒内没有应答")
            ready, _, _ = select.select([self._proc.stdout], [], [], remaining)
            if not ready:
                continue
            chunk = os.read(self._proc.stdout.fileno(), _READ_CHUNK)
            if not chunk:
                raise AcpUnavailableError("Agent 在应答前关闭了 stdout")
            buffer += chunk
            while b"\n" in buffer:
                line, buffer = buffer.split(b"\n", 1)
                message = _try_json(line)
                if not isinstance(message, dict):
                    continue
                if message.get("method") == "session/update":  # D12：不转发；仅本地计数/审计
                    self.updates_seen += 1
                    note_params = message.get("params")
                    update = note_params.get("update") if isinstance(note_params, dict) else None
                    if (
                        isinstance(update, dict)
                        and update.get("sessionUpdate") == "tool_call_update"
                    ):
                        raw = update.get("rawInput")
                        command = raw.get("command") if isinstance(raw, dict) else None
                        status = update.get("status")
                        if isinstance(command, str) and isinstance(status, str):
                            self._tool_calls.append(ToolCall(command=command, status=status))
                    continue
                if message.get("id") == request_id:
                    return message
