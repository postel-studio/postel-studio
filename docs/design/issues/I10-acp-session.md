# I10 · ACP 通道：会话与提示

- **里程碑**：M2　**预计**：3h　**依赖**：I09　**优先级**：P0

## 背景

握手通过后要能真正派活：建会话、发提示、收流式更新、可取消。

## 验收标准

1. `session/new` 使用**绝对** `cwd`，可携带 `mcpServers`；返回 `sessionId`。
2. `session/prompt` 期间 `session/update` 通知被转为内核事件流（消息增量、工具调用）。
3. `session/cancel` 能被发出并终止当前轮次。
4. 恢复路径按能力选择：有 `resume` 用 `resume`，只有 `loadSession` 用 `load`；**不得**假定两者等价。
5. 会话结束调用 `session/close`（若能力声明支持）。

## 涉及文件

- `src/postel_studio/agent/acp_client.py`（扩展）
- `src/postel_studio/agent/events.py`（新建：ACP 更新 → 内核事件映射）
- `tests/test_acp_session.py`（新建）

## 测试要求

- 假服务端覆盖：正常完成、取消、Agent 报错三条路径，各有断言。
- 恢复路径：构造两种能力组合（有 resume / 只有 load），断言走对应方法。
- 相对 `cwd` 必须被拒绝（`BAD_CWD`）。

## 禁止事项

- 不得把 `resume` 与 `load` 当同一个东西处理。
- 不得假定某 Agent 支持 `close`（以能力为准）。
- 不得把逐 token 更新无节流转发给客户端。
