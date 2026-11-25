# I02 · WebSocket gateway 最小帧协议

- **里程碑**：M1　**预计**：3h　**依赖**：I01　**优先级**：P0

## 背景

前后端唯一通道是 WebSocket + JSON 帧（见 `docs/02b-protocol.md` §2）。本切片只实现三种帧与订阅，不接任何业务。

## 验收标准

1. 客户端连上 `/gateway` 后，发 `{"type":"req","id":"x","method":"ping"}` 能收到 `{"type":"res","id":"x","ok":true,...}`。
2. 未知方法返回 `ok:false` 且 `error.code == "UNKNOWN_METHOD"`，不是 500。
3. `subscribe` 后，内核向该连接推 `evt` 帧；未订阅的 topic 不得推送。
4. 每帧 `seq` 单调递增。
5. 单帧超过 1 MiB 被拒绝，连接不崩。
6. 断连后重连，客户端重新订阅即恢复（内核不保存订阅状态）。

## 涉及文件

- `src/postel_studio/api/gateway.py`（新建）
- `src/postel_studio/api/frames.py`（新建，帧类型的纯函数）
- `tests/test_gateway.py`（新建）

## 测试要求

- 单测：帧的编解码、`seq` 递增、超大帧拒绝。
- 集成：真实 WebSocket 客户端连本地实例，完成 req/res/evt/subscribe 全流程。
- 必须有一条「未知方法」用例，断言 `error.code` 稳定。

## 禁止事项

- 不得在方法名里出现具体 Agent 名。
- 不得把人类可读文案当错误码。
- 不得在帧中传输任何凭据。
