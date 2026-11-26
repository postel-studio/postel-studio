"""S2 · 帧编解码纯单测（不起任何服务，D2 的收益就在这里）。

覆盖 §7.1 清单：合法 req / `type` 错 / 缺 `id` / `id` 超 128 / 非 JSON / 超 1 MiB /
`encode_response` 含 `data` 与 `seq` / `encode_event` 形状。
"""

from __future__ import annotations

import json

import pytest

from postel_studio.api.frames import (
    EVT,
    FRAME_TOO_LARGE,
    INVALID_PARAMS,
    MAX_FRAME_BYTES,
    MAX_ID_LENGTH,
    RES,
    FrameError,
    decode_incoming,
    encode_error,
    encode_event,
    encode_response,
)


def _frame(**overrides: object) -> str:
    base: dict[str, object] = {
        "type": "req",
        "id": "c1",
        "method": "system.echo",
        "params": {"hello": "world"},
    }
    base.update(overrides)
    return json.dumps(base)


def _code(text: str) -> str:
    with pytest.raises(FrameError) as exc:
        decode_incoming(text)
    return exc.value.code


# ---------------------------------------------------------------------------
# 合法输入
# ---------------------------------------------------------------------------


def test_decodes_valid_request():
    frame = decode_incoming(_frame())

    assert frame.id == "c1"
    assert frame.method == "system.echo"
    assert frame.params == {"hello": "world"}


def test_params_defaults_to_empty_object():
    """`system.health` 没有参数：缺 `params` 不该被当作错误。"""
    frame = decode_incoming(json.dumps({"type": "req", "id": "h1", "method": "system.health"}))

    assert frame.params == {}


def test_id_at_the_length_limit_is_accepted():
    frame = decode_incoming(_frame(id="x" * MAX_ID_LENGTH))

    assert frame.id == "x" * MAX_ID_LENGTH


# ---------------------------------------------------------------------------
# 拒绝：每条都断言稳定错误码（不是断言文案）
# ---------------------------------------------------------------------------


def test_wrong_type_is_invalid_params():
    """`res` / `evt` 不该被当成请求处理。"""
    assert _code(_frame(type="res")) == INVALID_PARAMS
    assert _code(_frame(type="evt")) == INVALID_PARAMS
    assert _code(_frame(type=123)) == INVALID_PARAMS


def test_missing_id_is_invalid_params():
    raw = json.dumps({"type": "req", "method": "system.echo"})

    with pytest.raises(FrameError) as exc:
        decode_incoming(raw)
    assert exc.value.code == INVALID_PARAMS
    # 没有可信的 id：网关只能记账并忽略，不能回一条「回不去的」响应
    assert exc.value.reply_id is None


def test_id_too_long_is_invalid_params():
    with pytest.raises(FrameError) as exc:
        decode_incoming(_frame(id="x" * (MAX_ID_LENGTH + 1)))
    assert exc.value.code == INVALID_PARAMS
    # 超长 id 本身也不该被拿来当回包的 id
    assert exc.value.reply_id is None


def test_non_string_id_is_invalid_params():
    for bad_id in (123, None, ["c1"], {"id": "c1"}, ""):
        assert _code(_frame(id=bad_id)) == INVALID_PARAMS


def test_non_json_is_invalid_params():
    assert _code("not json") == INVALID_PARAMS
    assert _code("") == INVALID_PARAMS


def test_non_object_json_is_invalid_params():
    """`[]` / `"x"` / `1` 都是合法 JSON，但不是帧。"""
    for raw in ("[]", '"x"', "1", "null"):
        assert _code(raw) == INVALID_PARAMS


def test_bad_method_or_params_is_invalid_params():
    assert _code(_frame(method=None)) == INVALID_PARAMS
    assert _code(_frame(method=1)) == INVALID_PARAMS
    assert _code(_frame(params=[1, 2])) == INVALID_PARAMS


def test_type_and_id_errors_carry_reply_id_when_id_is_usable():
    """`type` 错但 id 合法时，网关应该能回一条 `ok:false`——这是 `reply_id` 存在的理由。"""
    with pytest.raises(FrameError) as exc:
        decode_incoming(_frame(type="evt"))
    assert exc.value.reply_id == "c1"


# ---------------------------------------------------------------------------
# 超大帧：按 UTF-8 **字节**算，不是字符数
# ---------------------------------------------------------------------------


def test_frame_at_exactly_the_limit_is_accepted():
    """> 1 MiB 才拒；正好 1 MiB 属于「不超过」。"""
    head = json.dumps({"type": "req", "id": "c1", "method": "system.echo", "params": {"b": ""}})
    body_len = MAX_FRAME_BYTES - len(head.encode("utf-8"))
    assert body_len > 0
    raw = head.replace('"b": ""', '"b": "' + "a" * body_len + '"')
    assert len(raw.encode("utf-8")) == MAX_FRAME_BYTES

    assert decode_incoming(raw).id == "c1"


def test_too_large_frame_is_rejected_in_bytes_not_chars():
    """中文 1 字符 = 3 字节：字符数没超、字节数超了，也必须拒（否则按字符判会放过 3 MiB 的帧）。"""
    char_count = MAX_FRAME_BYTES // 3
    raw = json.dumps(
        {"type": "req", "id": "c1", "method": "system.echo", "params": {"b": "中" * char_count}},
        ensure_ascii=False,  # 不让 json 把它转成 \uXXXX：那会让「字符 vs 字节」的对照失效
    )
    assert len(raw) < MAX_FRAME_BYTES  # 字符数确实没超
    assert len(raw.encode("utf-8")) > MAX_FRAME_BYTES

    assert _code(raw) == FRAME_TOO_LARGE


def test_too_large_frame_reports_stable_code_without_reply_id():
    """超大帧不可信：不回 res，`reply_id` 为 `None`（网关据此直接关连接）。"""
    raw = json.dumps(
        {"type": "req", "id": "c1", "method": "system.echo", "params": {"b": "a" * MAX_FRAME_BYTES}}
    )

    with pytest.raises(FrameError) as exc:
        decode_incoming(raw)
    assert exc.value.code == FRAME_TOO_LARGE
    assert exc.value.reply_id is None


# ---------------------------------------------------------------------------
# 编码：形状照 02b §2.2 / 07 §1.2
# ---------------------------------------------------------------------------


def test_encode_response_wraps_data_and_seq():
    payload = json.loads(encode_response("c1", seq=7, data={"ok": 1}))

    assert payload == {"type": RES, "id": "c1", "ok": True, "result": {"data": {"ok": 1}, "seq": 7}}
    assert isinstance(payload["result"]["seq"], int)


def test_encode_error_shape():
    payload = json.loads(encode_error("c1", "UNKNOWN_METHOD", "没有这个方法"))

    assert payload == {
        "type": RES,
        "id": "c1",
        "ok": False,
        "error": {"code": "UNKNOWN_METHOD", "message": "没有这个方法"},
    }


def test_encode_event_shape():
    payload = json.loads(encode_event("task.updated", seq=41, data={"taskId": "t1"}))

    assert payload == {"type": EVT, "topic": "task.updated", "seq": 41, "data": {"taskId": "t1"}}


def test_encoded_frames_are_single_line():
    """帧是单行 JSON：多行会让「一帧一行」的日志与抓包判定失效。"""
    encoded = encode_response("c1", seq=1, data={"a": {"b": [1, 2]}})

    assert "\n" not in encoded
    assert encoded == encoded.strip()
