"""Agent 通道的业务编排（L3；与 `repository` / `bots` / `worktrees` 同款：不碰 fastapi）。

S9 两个方法的分工就是 D4：`list` 只读登记表**不启进程**，`capabilities` 真握一次手。
检测结果不落库（D12）——能力会随 Agent 升级变化，缓存下来就是下一个 U17。
"""

from __future__ import annotations

import time
from typing import Final

from postel_studio import __version__
from postel_studio.agent import acp_client, registry
from postel_studio.api import frames

# 07 §1.3 的既有码，一个不新增（S9 规格 §9 最后一条）。
INVALID_PARAMS: Final = "INVALID_PARAMS"
ADAPTER_UNAVAILABLE: Final = "ADAPTER_UNAVAILABLE"
ADAPTER_TIMEOUT: Final = "ADAPTER_TIMEOUT"
NOT_SUPPORTED: Final = "NOT_SUPPORTED"
INTERNAL: Final = "INTERNAL"

MSG_AGENT_KIND_INVALID: Final = "agentKind 缺失或不是字符串"
MSG_AGENT_KIND_UNKNOWN: Final = "未登记的 Agent 通道"
MSG_TARGET_UNSUPPORTED: Final = "target 第一版不支持（S9 D5）"
MSG_TIMEOUT: Final = "Agent 握手超时"

# `AgentsService` 里有个方法叫 `list`，类作用域的 `list[...]` 会被 mypy 解析成那个方法
# （`valid-type`）。列表载荷用模块级别名——与 `bots.py` 同一处教训。
type AgentPayloads = list[dict[str, object]]


def _fail(code: str, message: str) -> frames.FrameError:
    return frames.FrameError(code, message)


class AgentsService:
    """两个只读入口。`timeout_s` 由构造注入，测试用短超时（30s 的默认值只给产品用）。"""

    def __init__(self, *, timeout_s: float = acp_client.DEFAULT_TIMEOUT_S) -> None:
        self._timeout_s = timeout_s

    def list(self) -> AgentPayloads:
        """`agent.list`：登记了什么、命令是什么、**文件在不在**（不启动进程）。"""
        payloads: AgentPayloads = []
        for channel in registry.channels():
            command, available = self._command_and_availability(channel)
            payloads.append(
                {
                    "agentKind": channel.agent_kind,
                    "command": list(command),
                    "available": available,
                }
            )
        return payloads

    def capabilities(self, params: dict[str, object]) -> dict[str, object]:
        """`agent.capabilities`：真握手。成功才回能力；失败按 D6 的三种码回。"""
        channel = self._require_channel(params)
        if params.get("target") is not None:
            # D5：唯一说得通的语义是「换一个可执行文件」，而那正是 D2 关掉的门。
            raise _fail(NOT_SUPPORTED, MSG_TARGET_UNSUPPORTED)
        try:
            command = registry.command_for(channel)
        except registry.BrokenOverrideError as exc:
            raise _fail(ADAPTER_UNAVAILABLE, f"Agent 命令不可用：{exc}") from exc
        started = time.monotonic()
        try:
            handshake = acp_client.initialize(
                command,
                timeout_s=self._timeout_s,
                client_version=__version__,
            )
        except acp_client.AcpUnavailableError as exc:
            raise _fail(ADAPTER_UNAVAILABLE, f"Agent 命令不可用：{exc}") from exc
        except acp_client.AcpTimeoutError as exc:
            raise _fail(ADAPTER_TIMEOUT, MSG_TIMEOUT) from exc
        except acp_client.AcpProtocolError as exc:
            # 通道在、进程也答了，但答的不是 initialize 结果——说「没安装」是撒谎。
            raise _fail(INTERNAL, f"Agent 握手应答不合法：{exc}") from exc
        elapsed_ms = int((time.monotonic() - started) * 1000)
        return {
            "agentKind": channel.agent_kind,
            "available": True,
            "protocolVersion": handshake.protocol_version,
            "agentName": handshake.agent_name,
            "agentVersion": handshake.agent_version,
            "flags": list(handshake.flags),
            "unsupported": [],
            "elapsedMs": elapsed_ms,
        }

    def _require_channel(self, params: dict[str, object]) -> registry.Channel:
        raw = params.get("agentKind")
        if not isinstance(raw, str) or not raw:
            raise _fail(INVALID_PARAMS, MSG_AGENT_KIND_INVALID)
        channel = registry.resolve(raw)
        if channel is None:
            raise _fail(INVALID_PARAMS, MSG_AGENT_KIND_UNKNOWN)
        return channel

    def _command_and_availability(self, channel: registry.Channel) -> tuple[tuple[str, ...], bool]:
        """坏覆盖 = 不可用，**不回退默认**（D2）。这里只回答事实，不抛错。"""
        try:
            command = registry.command_for(channel)
        except registry.BrokenOverrideError:
            return channel.command, False
        return command, registry.is_available(command)
