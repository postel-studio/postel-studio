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
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
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
    proc: subprocess.Popen[bytes], request_id: object, timeout_s: float
) -> dict[str, object]:
    """按行读，**跳过非 JSON 行**（D6），直到 `id` 对上或者到点。

    Agent 会往 stdout 打 banner 和日志；把第一行非 JSON 当协议错，会把一个健康的 Agent
    报成坏的。fail-closed 只适用于「数据缺失」（超时 / EOF / 没有 protocolVersion）。
    """
    assert proc.stdout is not None
    deadline = time.monotonic() + timeout_s
    buffer = b""
    while True:
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
