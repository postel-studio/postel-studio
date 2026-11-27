# `agent/` — ACP over stdio（S9）

> 上游：[03 模块索引](README.md) · 相关：[02-layers §1](../02-layers.md) ·
> 规格：[S9-agent-detect](../../design/issues/S9-agent-detect.md)

## 1. 职责

内核**唯一**会起 Agent 子进程、并和它说话的地方。S9 只做一件事：一次 `initialize` 握手，
把应答里的版本与能力交出去，然后把进程回收。不建会话、不发 prompt（那是 S10）。

两个文件分工：

| 文件 | 管什么 |
|---|---|
| `agent/acp_client.py` | 协议：起进程、写请求、按行读、跳过噪声、按 deadline 收工、无条件回收 |
| `agent/registry.py` | 数据：内核认识哪几条通道、每条的命令数组是什么、文件在不在 |

## 2. 符号表

### `agent/acp_client.py`（235 行）

| 符号 | 行 | 做什么 | 谁在用 |
|---|---|---|---|
| `PROTOCOL_VERSION` | 24 | 发出的 `protocolVersion`（=1） | `initialize` |
| `DEFAULT_TIMEOUT_S` | 25 | 30 秒（验收 4 的数字） | `AgentsService` 的缺省 |
| `REAP_GRACE_S` | 26 | `terminate` 后等 5 秒再 `kill` | `reap` |
| `AcpUnavailableError` | 34 | 命令不存在 / 起不来 / 没答就退 | `api/agents.py` 翻 `ADAPTER_UNAVAILABLE` |
| `AcpTimeoutError` | 38 | 到点没应答 | 翻 `ADAPTER_TIMEOUT` |
| `AcpProtocolError` | 42 | 答了但不是 `initialize` 结果 | 翻 `INTERNAL`（说「没安装」是撒谎） |
| `Handshake` | 47 | 一次成功握手的全部产出（版本 / 名字 / `flags`） | `AgentsService.capabilities` |
| `child_env` | 56 | 滤掉名字含凭据字样的环境变量（D8 / `06 T22`） | `initialize` |
| `flatten_capabilities` | 70 | 把 `agentCapabilities` 摊平成 flag 名（D3） | `_parse` + 测试 |
| `initialize` | 99 | 起 → 发 → 读 → 回收 → 解析 | `api/agents.py` |
| `reap` | 144 | 关 stdin → terminate → wait(5) → kill → wait（D7） | `initialize` 的 `finally` |
| `_read_matching` | 176 | 按行读，跳过非 JSON，按 deadline 收工 | `_exchange` |

### `agent/registry.py`（78 行）

| 符号 | 行 | 做什么 |
|---|---|---|
| `ACP_STDIO` | 18 | 唯一通道 id：`"acp-stdio"`（传输名，不是产品名，D1） |
| `COMMAND_ENV` | 19 | `POSTEL_ACP_COMMAND`——进程级覆盖，**不来自帧**（D2） |
| `Channel` | 23 | `agent_kind` + 字面命令数组 |
| `_CHANNELS` | 30 | 只有一条：`("opencode", "acp")` |
| `BrokenOverrideError` | 33 | 覆盖值读不懂——**不回退默认**（fail-closed） |
| `channels` / `resolve` | 41 / 46 | 列出 / 按 id 找 |
| `command_for` | 54 | 解析覆盖值，返回命令数组 |
| `is_available` | 73 | `shutil.which`，**不启动进程** |

## 3. 关键决策与理由

**1. 通道 id 叫 `acp-stdio`，不叫 `opencode`。** `04 §6` 禁止方法名与分支条件里出现 Agent 名；
id 也一样——它描述的是「怎么说话」（ACP over stdio），不是「跟谁说话」。换一个 Agent 只要它在
stdio 上说 ACP，id 就不变；加一种传输才需要新 id。

**2. `sessionCapabilities` 的值是空对象，不是 `true`。** 这是本模块最容易写错的一处：ACP 用
「键存在」表达支持，值只是该特性的选项容器。只读布尔的实现会把一个全能 Agent 报成「零会话
能力」，而页面上看起来一切正常。`flatten_capabilities` 对三种形状（布尔叶子 / 命名空间 /
空对象）都要产出 flag，`test_flags_are_derived_not_hardcoded` 与反向注入 `S9 #1` 守着它。

**3. 非 JSON 行要跳过，不是报错。** Agent 会往 stdout 打 banner 和日志。把第一行非 JSON 当
协议错，会把健康的 Agent 报成坏的。fail-closed 只适用于**数据缺失**（超时 / EOF / 没有
`protocolVersion`），不适用于「有效数据旁边的噪声」。

**4. 命令只来自进程环境，不来自帧。** 帧里指定可执行文件 = 任意代码执行；`05 §3.3` 禁止信任
任何来自帧的命令。`POSTEL_ACP_COMMAND` 由启动内核的人设置，正是需要的信任级别。

**5. 覆盖值坏了就报不可用，不回退默认。** 静默回退会让用户看着默认 Agent 的结果、以为在测
自己配置的那个。

**6. 回收写在 `finally` 里，且测试要求 pid 证据。** 假服务端「答完不退」就是为了让这条断言
有意义——如果实现忘了杀，pid 会留在那里。反向注入 `S9 #2` 把 `terminate` 换成 `return`，
`test_child_is_reaped_after_timeout` 必须变红。

**7. `select` + `os.read`（POSIX），不用 `communicate`。** ACP 是长连接，stdout 不会 EOF，
`communicate(timeout=…)` 等的是 EOF，因此永远等不到「一行就返回」。这里要的是「有数据就读一行」，
只有 `select` 能做到。

## 4. 修改须知

- 改 `_CREDENTIAL_MARKERS` 会影响 `06 T22` 的保证；新增凭据类环境变量命名时记得它能命中。
- 改 `flatten_capabilities` 的 flag 命名 = 改**页面契约**：`#agent-flags` 里显示的就是它。
- 不要在这里 import `api` / `web` / `storage` / `domain` / `gitops`（`scripts/gate-layers.py`
  有登记）。需要业务判断时由 `api/agents.py` 传进来。
- 加一条新通道：往 `_CHANNELS` 加一行数据，不要在 `dispatch` 或 `initialize` 里加 `if`。

## 5. 测试

| 文件 | 覆盖 |
|---|---|
| `tests/test_acp_detect.py` | 真握手（本机 `opencode`，未装则 skip）、噪声行、超时、缺 `protocolVersion`、回收 pid、凭据过滤、覆盖值三条分支 |
| `tests/test_reverse_checks.py` | 注入 `S9 #1`（能力写死）/ `S9 #2`（不回收）必须让目标测试变红 |
| `tools/acceptance/s9_agent.mjs` | 真浏览器：点检测 → 真版本与能力、命令不存在时的负例文案 |
