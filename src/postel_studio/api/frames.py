"""帧编解码：**纯函数**，无 IO、无 async、无 WebSocket 依赖（S2 D2）。

这样单测不需要起服务，网关只负责「收字符串 → 解码 → 分派 → 编码 → 发」。

帧形状照抄 02b §2.2，不要在这里改设计：

- 请求 `{"type": "req", "id": "c1", "method": "...", "params": {...}}`
- 响应 `{"type": "res", "id": "c1", "ok": true, "result": {"data": ..., "seq": N}}`
- 推送 `{"type": "evt", "topic": "...", "seq": N, "data": {...}}`

错误码是稳定契约（07 §1.3）：`message` 只给人看，**不得**作为判断依据。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Final

logger = logging.getLogger(__name__)

# 单帧上限（02b §5 / 05 §3.1）。按 UTF-8 字节数算，不是字符数。
MAX_FRAME_BYTES: Final = 1024 * 1024
MAX_ID_LENGTH: Final = 128

REQ: Final = "req"
RES: Final = "res"
EVT: Final = "evt"

FRAME_TOO_LARGE: Final = "FRAME_TOO_LARGE"
INVALID_PARAMS: Final = "INVALID_PARAMS"
UNKNOWN_METHOD: Final = "UNKNOWN_METHOD"
INTERNAL: Final = "INTERNAL"

# S10 D11：可订阅 topic 的**唯一清单**。网关用它校验 `subscribe` 的入参（白名单外的
# topic 一律 `INVALID_PARAMS`——客户端拿不到一个"永远不会有事件"的假订阅）；编排器
# 只发布这里出现过的 topic。加 topic = 改这一行，两处自动一致。
KNOWN_TOPICS: Final = ("task.updated",)


class FrameError(Exception):
    """帧不合法。`code` 是稳定错误码（07 §1.3），`reply_id` 为 `None` 表示「有错但没 id 可回」."""

    def __init__(self, code: str, message: str, reply_id: str | None = None) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.reply_id = reply_id


@dataclass(frozen=True, slots=True)
class ReqFrame:
    """一条通过校验的请求。`params` 缺省为空字典（方法必须自己校验字段）。"""

    id: str
    method: str
    params: dict[str, object]


def decode_incoming(text: str) -> ReqFrame:
    """把一帧文本解成 `ReqFrame`；任何不合法都抛 `FrameError`。

    `FrameError.reply_id` 的取值决定了调用方怎么处理（D2 裁决）：

    - 能确定 `id`（合法字符串、长度合法）→ 回一条 `ok:false` 的 `res`；
    - 确定不了 `id` → 计一条日志并**忽略**，不要断连（05 §3.1「忽略并计数，不崩溃」）。
    """
    size = len(text.encode("utf-8"))
    if size > MAX_FRAME_BYTES:
        raise FrameError(FRAME_TOO_LARGE, f"单帧 {size} 字节超过 {MAX_FRAME_BYTES} 上限")
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        raise FrameError(INVALID_PARAMS, f"不是合法 JSON：{exc.msg}") from None
    if not isinstance(raw, dict):
        raise FrameError(INVALID_PARAMS, "帧必须是 JSON 对象")

    # 先尽量捞一个可信的 id：有了它，后面的错误才回得去
    raw_id = raw.get("id")
    reply_id = raw_id if isinstance(raw_id, str) and 0 < len(raw_id) <= MAX_ID_LENGTH else None

    if raw.get("type") != REQ:
        raise FrameError(INVALID_PARAMS, "type 必须是 req", reply_id)

    if not isinstance(raw_id, str) or not raw_id:
        raise FrameError(INVALID_PARAMS, "id 必须是非空字符串", None)
    if len(raw_id) > MAX_ID_LENGTH:
        raise FrameError(INVALID_PARAMS, f"id 超过 {MAX_ID_LENGTH} 字符", None)

    method = raw.get("method")
    if not isinstance(method, str) or not method:
        raise FrameError(INVALID_PARAMS, "method 必须是非空字符串", raw_id)

    params = raw.get("params", {})
    if not isinstance(params, dict):
        raise FrameError(INVALID_PARAMS, "params 必须是 JSON 对象", raw_id)

    return ReqFrame(id=raw_id, method=method, params=params)


def encode_response(request_id: str, *, seq: int, data: object) -> str:
    """成功响应：`result` 统一包裹成 `{data, seq}`（07 §1.2）。"""
    return json.dumps(
        {"type": RES, "id": request_id, "ok": True, "result": {"data": data, "seq": seq}},
        ensure_ascii=False,
        separators=(",", ":"),
    )


def encode_error(request_id: str, code: str, message: str) -> str:
    """失败响应。`code` 稳定，`message` 可改（07 §1.3）。"""
    return json.dumps(
        {"type": RES, "id": request_id, "ok": False, "error": {"code": code, "message": message}},
        ensure_ascii=False,
        separators=(",", ":"),
    )


def encode_event(topic: str, *, seq: int, data: object) -> str:
    """订阅推送帧（02b §2.2）。S2 不发事件，这里只把形状定死，供后续切片直接用。"""
    return json.dumps(
        {"type": EVT, "topic": topic, "seq": seq, "data": data},
        ensure_ascii=False,
        separators=(",", ":"),
    )
