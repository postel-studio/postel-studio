# S9 · 点「检测 Agent」→ 看到 OpenCode 状态

- **里程碑** M2 · **预计** 3h · **依赖** S2 · **优先级** P0
- **用户路径**：点「检测 Agent」→ 显示 OpenCode 是否可用、版本、能力清单

## 背景

派活前必须知道"对面有没有 Agent、它支持什么"。这一刀只做**握手 + 显示**，不发 prompt。

## 验收标准

1. 检测到本机 `opencode` 时显示「可用」与版本（真实来自 `initialize` 响应）。
2. 未安装时显示「不可用」与安装提示，不报错崩溃。
3. 显示能力清单（如 `sessionCapabilities`），**来自握手响应而非硬编码**。
4. 握手超时（默认 30s）显示「超时」并可重试。
5. 子进程在检测完成后**必须被回收**，不留孤儿。

## 涉及文件

- `src/postel_studio/agent/acp_client.py`（新建，stdio JSON-RPC 客户端，≤180 行）
- `src/postel_studio/agent/registry.py`（新建，通道登记）
- `src/postel_studio/api/app.py`（加 `agent.list` / `agent.capabilities`）
- `src/postel_studio/web/assets.py`（Agent 状态卡）
- `tests/test_acp_detect.py`（新建）
- `tests/fixtures/fake_acp.py`（新建，仅用于异常路径）

## 测试要求

- **真实**：对本机 `opencode acp` 做 `initialize`，断言拿到 `protocolVersion`（未装则 skip）。
- 假服务端：非法 JSON 行被忽略、超时返回 `ADAPTER_TIMEOUT`、命令不存在返回 `ADAPTER_UNAVAILABLE`。
- 回收断言：检测后无残留子进程。

## 禁止事项

- 不得硬编码任何 Agent 的能力值。
- 不得在命令参数里展开 shell 表达式（必须字面数组直接 exec）。
- 不得在方法名或分支条件里出现 Agent 名。
- 本刀不发 prompt（那是 S10）。
