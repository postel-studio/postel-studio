"""通道登记：内核认识哪些「怎么和 Agent 说话」的方式。

登记的是**传输**（ACP over stdio，`02b §1`），不是产品（S9 D1）。加一条通道 = 往
`_CHANNELS` 里加一行数据，不是加一个 `if`——`04 §6` 禁止在分支条件里出现 Agent 名。

命令是**字面数组**（S9 D2 / `05 §3.3`）：不经 shell，不展开 `~`、`$VAR`、通配符。
覆盖只来自进程环境，不来自帧——帧里指定可执行文件等于任意代码执行。
"""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass
from typing import Final

ACP_STDIO: Final = "acp-stdio"
COMMAND_ENV: Final = "POSTEL_ACP_COMMAND"


@dataclass(frozen=True)
class Channel:
    """一条通道：稳定 id + 默认可执行文件与参数。"""

    agent_kind: str
    command: tuple[str, ...]


_CHANNELS: Final = (Channel(agent_kind=ACP_STDIO, command=("opencode", "acp")),)


class BrokenOverrideError(Exception):
    """`POSTEL_ACP_COMMAND` 给了值但读不懂。

    **不回退默认**（S9 D2 的 fail-closed）：静默回退会让用户看着 A 的检测结果、
    以为检测的是自己配置的 B。
    """


def channels() -> tuple[Channel, ...]:
    """全部已登记通道。顺序即 `agent.list` 的顺序。"""
    return _CHANNELS


def resolve(agent_kind: str) -> Channel | None:
    """按 id 找通道；未登记回 `None`（调用方翻成 `INVALID_PARAMS`）。"""
    for channel in _CHANNELS:
        if channel.agent_kind == agent_kind:
            return channel
    return None


def command_for(channel: Channel) -> tuple[str, ...]:
    """通道的命令数组。环境变量覆盖只由**启动内核的人**设置，不来自协议。"""
    raw = os.environ.get(COMMAND_ENV, "").strip()
    if not raw:
        return channel.command
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise BrokenOverrideError(f"{COMMAND_ENV} 不是合法 JSON") from exc
    if not isinstance(parsed, list) or not parsed:
        raise BrokenOverrideError(f"{COMMAND_ENV} 必须是非空 JSON 数组")
    command: list[str] = []
    for item in parsed:
        if not isinstance(item, str) or not item:
            raise BrokenOverrideError(f"{COMMAND_ENV} 的每一项都必须是非空字符串")
        command.append(item)
    return tuple(command)


def is_available(command: tuple[str, ...]) -> bool:
    """静态可用性：可执行文件在不在 `PATH` 上。**不启动进程**（S9 D4）。

    这只是「文件找得到」，**不代表**握手会成功——后者由 `agent.capabilities` 回答。
    """
    return bool(command) and shutil.which(command[0]) is not None
