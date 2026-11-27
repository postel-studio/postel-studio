# `api/agents.py` — Agent 通道编排（S9）

> 上游：[03 模块索引](README.md) · 相关：[agent/ 客户端层](agent.md) ·
> 契约：[07 §4.11](../../design/07-api-contract.md) · 规格：[S9](../../design/issues/S9-agent-detect.md)

## 1. 职责

`agent.list` 与 `agent.capabilities` 的业务编排。与 `repository` / `bots` / `worktrees` 同款
L3 handler：不碰 fastapi，抛 `frames.FrameError`，只向下依赖 `agent/` 与 `frames/`。

**两个方法的分工是这一刀的核心**（D4）：

| 方法 | 成本 | 做什么 |
|---|---|---|
| `agent.list` | 微秒 | 读登记表 + `shutil.which`，回答「文件在不在」 |
| `agent.capabilities` | 一个进程、一次往返 | 真握手，能力来自应答 |

## 2. 符号表（108 行）

| 符号 | 行 | 做什么 |
|---|---|---|
| `INVALID_PARAMS` 等五个码 | 17-21 | `07 §1.3` 的既有码，本刀**一个不新增** |
| `AgentPayloads` | 30 | `list[dict]` 的别名（`list` 方法名会遮蔽内建 `list[...]`，`bots.py` 同一处教训） |
| `_fail` | 33 | 构造 `FrameError` |
| `AgentsService.__init__` | 40 | 注入 `timeout_s`（产品 30s，测试用 0.3s） |
| `AgentsService.list` | 43 | 每条通道回 `{agentKind, command, available}` |
| `AgentsService.capabilities` | 57 | 判 `agentKind` → 判 `target` → 取命令 → 握手 → 回载荷 |
| `_require_channel` | 93 | 缺 / 非字符串 / 未登记 → `INVALID_PARAMS` |
| `_command_and_availability` | 102 | 覆盖值坏了 → `available: false`（不执行默认）；`command` 回默认值仅作诊断 |

## 3. 关键决策与理由

**1. 编排不写进 `app.py`。** 规格 stub 写的是「`api/app.py`（加两个方法）」，但 `app.py` 是路由
表 + 装配，S6/S7/S8 都把业务路径放在自己的 L3 模块里。这一刀沿用同一形状，`app.py` 只加构造
与两行注入（D11）。

**2. `target` 在 S9 是保留字，非 null 一律 `NOT_SUPPORTED`。** 唯一说得通的语义是「换一个可
执行文件」，而那正是 D2 关掉的门。等 S10 给它「在哪个工作区开会话」的语义时，写附录改，不做
静默重解释（D5）。

**3. 握手失败分三种码，不合并。** `ADAPTER_UNAVAILABLE`（没有）/ `ADAPTER_TIMEOUT`（没答）/
`INTERNAL`（答了但答错）。合并成一种会让用户看到错误的建议——「去装一个」对「已经装了但版本
不兼容」是误导（D6）。

**4. 检测不落库。** 能力随 Agent 升级变化，缓存下来就是第二个 U17（一个会过期的存储答案）。
每次点都重新握手，实测 0.58–0.7 秒（D12）。

**5. `bot.agentKind` 仍不可写（U18，到期 S10）。** S9 没有「选通道」的 UI，接受一个页面产生
不了的值等于接口先于用户路径。S10 派活时再放开或加选择器（D13）。

## 4. 修改须知

- 改返回字段 = 改契约：`07 §4.11` 与 `tests/test_acp_detect.py` 的页面断言都要跟着改。
- 不要在这里 import `web` 或同层其它 handler（`scripts/gate-layers.py` 有登记）。
- 加通道不需要改本文件；它遍历 `registry.channels()`。
- 如果将来允许帧传 `target`，先想清楚它是不是一条**路径**——那要走 `05 §3.3` 的校验。

## 5. 测试

| 文件 | 覆盖 |
|---|---|
| `tests/test_acp_detect.py` | 三种失败码、`target` 拒绝、`agentKind` 五种非法输入、`available` 与覆盖值 |
| `tests/test_gateway.py` | `agent.list` 已是真方法（反例清单换成了 `memory.charter.set`） |
| `tools/acceptance/s9_agent.mjs` | 真浏览器正例 + 命令不存在的负例 |
