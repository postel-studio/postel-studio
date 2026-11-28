"""S16 · 执行前裁决层：把 Bot 权限变成 opencode 的会话配置，并留下审计。

裁决发生在**会话启动前**，由 opencode v2 的 `permissions` 在工具调用执行前实施；
本模块不靠 prompt 约束，也不读 Agent 类型 / 凭据。无法识别的权限一律 fail-closed：
写 `FALLBACK` 审计后抛错，调用方必须把任务终态化为 `failed` + `INTERNAL`。
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable, Sequence

from postel_studio.agent import acp_client
from postel_studio.domain.permission import rules_for
from postel_studio.storage import paths

CONFIG_KEY = "OPENCODE_CONFIG_CONTENT"


def session_factory(
    permission: object, bot_id: str
) -> Callable[[Sequence[str], str], acp_client.Session]:
    """造一个带权限规则的会话工厂；签名与 `SessionFactory` 协议保持一致。

    合并已有的 `OPENCODE_CONFIG_CONTENT`（S14 的 deepseek 模型注入），只覆盖 `permissions`
    一项——这样凭据仍由 opencode 自己的 db 读取，规则从不在源码里硬编码。
    """
    rules = rules_for(permission)

    def fallback(reason: str) -> None:
        """D6：任何无法真正阻断的路径都留一条 FALLBACK 审计，再拒绝会话。"""
        paths.append_audit(
            {
                "at": time.time(),
                "botId": bot_id,
                "permission": str(permission),
                "action": "*",
                "resource": reason,
                "effect": "FALLBACK",
                "source": "gate",
            }
        )

    if rules is None:  # D6：无法阻断 → 不启动会话
        fallback("unknown-permission")
        raise ValueError(f"无法识别的权限级别 {permission!r}：按只读 fail-closed 处理")

    payload = os.environ.get(CONFIG_KEY, "")
    config: dict[str, object] = {}
    if payload:
        loaded = json.loads(payload)
        if isinstance(loaded, dict):
            config = loaded
    config["permissions"] = [
        {"action": action, "resource": resource, "effect": effect}
        for action, resource, effect in rules
    ]

    for action, resource, effect in rules:
        paths.append_audit(
            {
                "at": time.time(),
                "botId": bot_id,
                "permission": str(permission),
                "action": action,
                "resource": resource,
                "effect": effect.upper(),
                "source": "gate",
            }
        )

    env = dict(os.environ)
    env[CONFIG_KEY] = json.dumps(config, ensure_ascii=False, sort_keys=True)

    def factory(command: Sequence[str], cwd: str) -> acp_client.Session:
        try:
            # D6：握手后、session/new 之前探测版本；v1 静默忽略 permissions → 不开会话。
            return acp_client.Session(command, cwd, parent_env=env, require_permissions=True)
        except acp_client.AcpPermissionsUnsupportedError:
            fallback("agent-permissions-unsupported")
            raise

    return factory
