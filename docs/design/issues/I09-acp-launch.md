# I09 · ACP 通道：拉起与握手

- **里程碑**：M2　**预计**：3h　**依赖**：I02　**优先级**：P0

## 背景

ACP 是通用接入通道。本切片只做「拉起子进程 + initialize 握手 + 读能力」，不发 prompt。

## 验收标准

1. 给定 `command` + 字面参数列表，内核拉起 ACP 子进程，发送 `initialize`，读到 `protocolVersion` 与 `agentCapabilities`。
2. **能力来自握手响应**，不硬编码；返回结构里 `unsupported` 明确列出缺失项。
3. 子进程 stdout 只读协议帧；非 JSON 行不得使内核崩溃，需被记录并忽略。
4. 子进程启动失败（命令不存在）返回 `ADAPTER_UNAVAILABLE`，不是 500。
5. 握手超时（默认 30s）返回 `ADAPTER_TIMEOUT`。
6. 内核关闭时所有 ACP 子进程被回收（无孤儿）。

## 涉及文件

- `src/postel_studio/agent/acp_client.py`（新建：stdio JSON-RPC 客户端）
- `src/postel_studio/agent/registry.py`（新建：通道登记）
- `tests/test_acp_handshake.py`（新建）
- `tests/fixtures/fake_acp.py`（新建：一个最小 ACP 假服务端，仅用于异常路径）

## 测试要求

- **真实**：对已装的 `opencode acp` 做一次 `initialize`，断言拿到 `protocolVersion`（可跳过标记，CI 无 opencode 时 skip 而非失败）。
- 假服务端：验证非法 JSON 行被忽略、超时返回 `ADAPTER_TIMEOUT`、命令不存在返回 `ADAPTER_UNAVAILABLE`。
- 回收：启动后杀内核，断言无残留子进程。

## 禁止事项

- 不得在命令参数里展开 shell 表达式（必须直接 `exec` 字面参数）。
- 不得硬编码任何特定 Agent 的能力值。
- 不得把 Agent 写进方法名或分支条件。
