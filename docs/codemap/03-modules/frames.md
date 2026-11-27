# 03 · frames — `src/postel_studio/api/frames.py`

> 层 L1 · 126 行 · 上游：[模块索引](README.md) · 协议：[02b-protocol §2.2](../../design/02b-protocol.md)

## 职责

帧编解码。**纯函数模块**：无 IO、无 async、无框架依赖。

这条约束的价值：帧编解码的单元测试**不需要起服务**（`test_frames.py` 有 18 个用例，全部毫秒级）。

**禁止** import fastapi / starlette / uvicorn。见 [02-layers §6](../02-layers.md#6-禁止事项)。

## 符号表

| 符号 | 行 | 做什么 |
|---|---|---|
| `MAX_FRAME_BYTES` | 24 | 1 MiB（02b §5 / 05 §3.1） |
| `MAX_ID_LENGTH` | 25 | 128（05 §3.1） |
| `REQ` / `RES` / `EVT` | 27-29 | `"req"` / `"res"` / `"evt"` |
| `FRAME_TOO_LARGE` | 31 | 稳定错误码 |
| `INVALID_PARAMS` | 32 | 稳定错误码 |
| `UNKNOWN_METHOD` | 33 | 稳定错误码 |
| `INTERNAL` | 34 | 稳定错误码 |
| `FrameError(code, message, reply_id)` | 37 | 帧不合法。`reply_id is None` = 「有错但没 id 可回」 |
| `ReqFrame` | 48 | frozen dataclass：`id` / `method` / `params` |
| `decode_incoming(text)` | 56 | `str → ReqFrame`，失败抛 `FrameError` |
| `encode_response(id, seq=, data)` | 102 | 成功帧 |
| `encode_error(id, code, message)` | 111 | 失败帧 |
| `encode_event(topic, seq=, data)` | 120 | 推送帧（**S10 起有真实调用方**：网关的 pump task） |
| `KNOWN_TOPICS` | 39 | **S10**：可订阅 topic 白名单（`("task.updated",)`）；`subscribe` 的入参校验与 `_topics_of` 都读它 |

## 关键决策与理由

### `reply_id` 决定「回不回」

05 §3.1 说「JSON 不合法 → 忽略并计数，不崩溃」。但**有 id 才能回错误**：

| 情况 | `reply_id` | 网关行为 |
|---|---|---|
| 能确定合法 `id` | 该 id | 回一条 `ok:false` 的 `res` |
| 确定不了 `id` | `None` | 记日志 + **忽略，不断连** |

这条区分很重要：**无差别断连会让一个坏客户端把连接搞死**；无差别静默会让客户端永远等不到回包。

### 校验顺序的讲究

```python
size = len(text.encode("utf-8"))  # ① 先看字节数
if size > MAX_FRAME_BYTES:
    raise ...
raw = json.loads(text)  # ② 再解析
reply_id = ...  # ③ 尽量先捞一个可信 id
if raw.get("type") != REQ:
    raise ...  # ④ 再判 type
```

**① 必须最先**：对超大帧做 `json.loads` 是白送 CPU/内存给攻击者。

**③ 必须在 ④ 之前**：先把 `id` 捞出来，后面的错误才回得去。
注意 `id` 自身不合法时 `reply_id=None`（没法回给它）。

### `id` 超长时 `reply_id` 为 `None`，不是原样回

```python
if len(raw_id) > MAX_ID_LENGTH:
    raise FrameError(INVALID_PARAMS, ..., None)  # 不回显超长 id
```

**不回显**：超长 id 本身可能是攻击载荷。`test_oversized_id_is_rejected_without_echoing_it`
守着这条。

### `params` 缺省为空字典，且必须是 JSON 对象

```python
params = raw.get("params", {})  # 缺省 {}
if not isinstance(params, dict):
    raise FrameError(INVALID_PARAMS, ...)
```

**方法自己负责校验 `params` 里的字段**（frames 只看「是不是对象」）。
这在方法变多时会出现重复校验 → 届时再引入 schema helper，**现在不要提前抽象**。

### `encode_event` 的第一个真实调用方（S10 D11 兑现 U7）

S2 只是定形状，S10 的网关事件泵开始真正用它：`task.updated` 事件经编排器的 `EventHub`
投递后由 pump 编码发出。U7 预言的「形状问题」没有出现——形状当初定对了；顺带补上的
集成测试是 `tests/test_dispatch.py::test_evt_reaches_subscriber_only`（订了才收到）。

`KNOWN_TOPICS` 是**订阅白名单**：`subscribe` 收到名单外的 topic 回 `INVALID_PARAMS`，
不静默忽略——客户端拼错 topic 名就该当场报错，而不是收不到事件还以为订阅生效了。
新增 topic 必须 append 这里 + `07-api-contract §3`。

### JSON 输出用 `separators=(",", ":")`

去掉空格，省带宽。`ensure_ascii=False` 保留中文可读性。
**不要改成 `indent=`**——帧不是给人读的。

## 错误码是稳定契约

07 §1.3：**文案可变，码不变**。

```python
ERROR_CODES = {FRAME_TOO_LARGE, INVALID_PARAMS, UNKNOWN_METHOD, INTERNAL}
```

新增码必须同时更新 `07-api-contract §1.3` 的表。
**禁止**在业务逻辑里用 `message` 判断分支。

## 修改须知

| 如果你要… | 还必须 |
|---|---|
| 加错误码 | `07-api-contract §1.3` + 本页 |
| 加帧类型 | `02b-protocol §2.2`（那是协议文档，不是这里） |
| 改 `MAX_FRAME_BYTES` | `02b §5` + `05 §3.1` + `test_gateway.py`（真发超大帧） |
| 让 frames 依赖状态 | **禁止**。它是纯函数层，加了状态就不再可单测 |

## 测试

`tests/test_frames.py`（18 用例）：

| 测试 | 守什么 |
|---|---|
| 合法 req 解码 | 基本路径 |
| `type` 错 / 缺 `id` / `id` 超 128 / 非 JSON / 非对象 | `INVALID_PARAMS` |
| `reply_id` 的三种取值 | 「能不能回」的判定 |
| 超 1 MiB | `FRAME_TOO_LARGE`（**按字节不是字符**） |
| 正好 1 MiB | 放行（边界） |
| 中文帧按字节判 | 3 字节/字符 |
| `encode_response` / `encode_error` / `encode_event` | 输出形状 |