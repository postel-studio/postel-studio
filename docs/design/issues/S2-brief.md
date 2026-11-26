# S2 派活说明（管理者 → 子代理）

> 本文件是 S2 的**执行合同**，不是设计文档。设计见
> [S2-connection-state.md](S2-connection-state.md)、[02b-protocol.md](../02b-protocol.md)、
> [07-api-contract.md](../07-api-contract.md)、[05-security.md](../05-security.md)。

---

## 0. 你的角色与边界

你是**执行者**，不是决策者。遇到设计疑问**先按本文档的裁决执行**；本文档没覆盖且影响
验收标准的，**停下来在交付说明里写清楚问题**，不要自行扩大范围。

- **不要**动 `main`，不要开 PR，不要改 `docs/design/` 下的设计文档（除本文档 §6 允许的项）。
- **不要**改 S1 已合并的行为（`server.json` 字段、信号处理、锁语义、退出清理）。
- **不要**顺手重构。看到与 S2 无关的坏味道记进交付说明，不要改。
- **一次只交付 S2**。

## 1. 工作目录与命令

```bash
cd /home/postel/workspace/postel-studio
git branch --show-current        # 必须是 feat/s2-connection-state
bash scripts/check.sh            # 全部门禁；提交前必须全绿
```

依赖已装好（`websockets>=14`、dev 组 `httpx`），**已提交**在 `a1d40a6`，不要再动
`pyproject.toml` / `uv.lock`。

## 2. 这个切片要交付的可见结果

用户已经能打开页面（S1）。S2 让页面**诚实反映连接**：

> 页面右上角有状态灯 → 杀掉内核 → ≤3 秒内灯变红且数据灰化 → 重启内核 → 点「重连」→ 灯变绿

S1 的页面是静态常量、没有任何脚本。S2 是**第一次**引入前端 JS 与 WebSocket，所以本刀
同时要把「帧协议」这个地基立起来——但**只立到能跑一次 `req/res` 往返为止**，不要提前实现任何业务方法。

## 3. 必须先读的文件

| 文件 | 你要从中获得什么 |
|---|---|
| `src/postel_studio/api/app.py` | 现有两个路由、`create_app()` 的构造方式 |
| `src/postel_studio/api/server.py` | `_run()` 里令牌生成时机、`create_app()` 被调用的位置 |
| `src/postel_studio/storage/paths.py` | `write_token` / `fingerprint` / `atomic_write` 已有能力 |
| `src/postel_studio/web/assets.py` | 现有页面是单个格式化字符串 |
| `tests/test_server_boot.py` | **测试风格的唯一事实源**：真实子进程 + 真实 HTTP，`_spawn` / `_serve_env` 辅助 |
| `tests/conftest.py` | 会话级 `POSTEL_HOME` 隔离 |
| `docs/design/02b-protocol.md` §2 | 帧形状（照抄，不要改设计） |
| `docs/design/07-api-contract.md` §1.2 §1.3 §2.1 | 响应包络 `{data, seq}`、错误码表 |
| `docs/design/05-security.md` §1.1 §3.1 §4.1 | 令牌不得进页面/日志；帧校验规则 |

## 4. 验收标准（逐条可验，验收时会真跑）

| # | 标准 | 验证方式 |
|---|---|---|
| A1 | 页面顶部显示连接状态：`已连接` / `已断开` / `重连中` | 真起内核，用 httpx 取页面，断言 HTML 含三个状态的字面量与状态灯元素 |
| A2 | 内核被杀后 ≤3 秒内状态变「已断开」，出现「重连」按钮 | 前端逻辑无法在 pytest 里跑 DOM。**改为**：后端断言「连接关闭时服务端在 ≤3s 内清理该连接」；前端用 JS 里的常量 `RECONNECT_DELAY_MS` ≤ 1000 且页面含「重连」按钮字面量来保证上限。**必须在交付说明里写明这条是如何被覆盖的、以及哪些部分是人工在浏览器里确认的** |
| A3 | 点重连能恢复，无需刷新 | 网关侧：同一客户端断开后重新连接、重新发 `system.echo` 成功。前端：重连函数存在且不清空页面 |
| A4 | 断连期间已加载的数据标记为过期（灰化或角标） | 页面 CSS 有 `.stale` 类（或等价），JS 在断开时给内容容器加该类，重连成功后移除。断言 HTML/CSS 里存在该规则 |
| A5 | 一次 WebSocket 帧的 `req/res` 往返可用（`system.echo`） | 真实 `websockets` 客户端连真内核 → 发 `system.echo` → 收到 `{"data":..., "seq":N}` |

**额外硬性要求**：

- `system.echo` 之外的方法返回 `UNKNOWN_METHOD`（错误码稳定，07 §1.3）。
- `seq` 单调递增（单连接内）。
- 超大帧被拒（> 1 MiB，05 §3.1）。

## 5. 设计裁决（照做，不要重新论证）

以下是管理者根据现有代码与文档做出的裁决。**不要改动这些决定**；若你认为某条无法实现，
停下来报告，不要换方案。

### D1 · 令牌如何到浏览器（最关键的一条，S1 遗留问题）

`05-security.md §1.1` 要求「客户端必须携带 `Authorization: Bearer <token>` 才能建立 WebSocket」，
而 S1 交付说明里记了一条偏离：「S1 还没有 WebSocket 客户端可发 Bearer」。

**浏览器无法在 WebSocket 握手时设置自定义头**（`new WebSocket()` 只接受 URL + subprotocol）。
所以两条路：把令牌放进 URL（`?token=`）或在页面里注入令牌。**两者都违反 S2 禁止事项「不得把连接令牌写进页面源码」**，
而放进 URL 又会进日志（05 §4.1）。

**裁决采用第三条路，不改安全文档里那条能力，而是换认证载体：**

1. `server.json` **新增** `wsPath` 字段：`/gateway/<一次性路径令牌>` 的前缀，例如
   `/gateway/9f3a...`（32 字节 hex，由内核启动时生成，与 `token` **不同**的另一个随机值）。
2. 根路径 `/` 仍然是**不需要认证**的页面（它本来就只有版本/状态根，公开无害）。
   但 `/` 的响应**不包含**任何令牌 —— 页面里的 JS 从 `server.json` **读不到**令牌，
   因为浏览器不能读文件系统。
3. **因此页面必须由内核注入一个「握手凭证」**：内核在渲染 `/` 时生成一个
   **短时效、单次使用**的 handshake ticket（随机值，内存里存 `dict[ticket] -> expiry`，
   有效期 60 秒、用后即删），把它写进页面的 `<meta name="postel-handshake" content="...">`。
   这个 ticket **不是** 连接令牌：它只能换一次 WS 连接，连上后立即作废，60 秒后无论如何作废，
   且不能用于任何 HTTP 请求。
4. 客户端连 `/gateway` 时把 ticket 放进 URL 查询串 `?ticket=...` **一次性**；内核验证后
   **立刻**从内存删除该 ticket —— 所以「令牌进 URL」的泄漏面只剩一个已作废的随机串。
5. **真正长期有效的 `token` 文件保持不变**（S1 已实现，`server.json` 里仍只有 `tokenFingerprint`）。
   S2 **不使用** `token` 文件做浏览器认证；它的用途留给将来的 CLI/脚本客户端（M3 之后）。

> **为什么这是最优解而不是偷懒**：Bearer 头在浏览器 WS 上物理不可用；把长期令牌塞进页面
> 会让「令牌不进页面源码」这条硬约束直接失守；一次性 ticket 把这两难拆开——页面里只有一个
> 60 秒内、且只能用一次的随机串。**必须把这个理由写进 `docs/design/05-security.md §1.1` 的修订**
> （本文档 §6 允许的唯一设计文档改动）。

### D2 · 帧编解码放纯函数模块

`frames.py` 只做：`decode_incoming(text: str) -> ReqFrame`、`encode_response(...)`、`encode_event(...)`、
`FrameError`（带 `code`）。**无 IO、无 async、无 WebSocket 依赖**，这样单测不需要起服务。
超过 `MAX_FRAME_BYTES = 1 MiB` 的输入抛 `FrameError("FRAME_TOO_LARGE")`。

用户给的 `id` 长度 > 128 → `INVALID_PARAMS`（05 §3.1）。`type != "req"` → `INVALID_PARAMS`。
JSON 解析失败 → `INVALID_PARAMS`（05 §3.1 说「忽略并计数，不崩溃」，但**有 id 才能回错误**；
无 id 时后端计日志并忽略，不要断连）。

### D3 · 网关只实现两个 handler

`system.echo`（回显 `params`，附加 `seq`）与 `system.health`（复用 `app.health()` 的返回）。
其它任何方法 → `UNKNOWN_METHOD`。**不要**提前实现 `repo.*` / `bot.*` / `task.*`。
不要实现订阅（`system.subscribe`）—— 那是后续切片的活，本刀连 topic 机制都不建。

### D4 · `seq` 是网关级单调计数器

每个 WebSocket 连接一个独立计数器，从 1 开始，每次**发出**一个 res 或 evt 都 +1。
不是全局的。这样丢帧检测在单连接内成立（02b §2.2 的语义就是单连接）。
`system.health` 的 `seq` 用这个计数器，不要另造一个。

### D5 · 服务端清理要能被测到

网关必须让测试能断言「连接断开后服务端清理了它」。做法：模块级 `_ACTIVE: dict[WebSocket, ConnectionState]`，
或用 `app.state.active_connections`。**要求**：提供一个只读的 `active_connection_count()` 函数供测试用；
不要为测试开后门改行为。断开时在 `finally` 里删除（不要只在正常路径删）。

### D6 · 前端不用构建链

`web/assets.py` 继续是**一个 Python 字符串常量**（S1 已定的禁止事项，不要引 vite/react）。
JS 内联在 `<script>` 里，< 150 行。风格与现有页面一致（深色、`#0f1115` 底）。

## 6. 允许改动的文件（白名单，不许越界）

| 文件 | 动作 | 上限 |
|---|---|---|
| `src/postel_studio/api/frames.py` | 新建 | ≤150 行 |
| `src/postel_studio/api/gateway.py` | 新建 | ≤150 行 |
| `src/postel_studio/api/app.py` | 挂载网关 + 注入 handshake ticket | 改动 ≤60 行 |
| `src/postel_studio/api/server.py` | 生成 `wsPath` 前缀 / 把 ticket 存储交给 app | 改动 ≤30 行 |
| `src/postel_studio/web/assets.py` | 状态灯 + 重连 + 过期标记 | 改动 ≤150 行 |
| `tests/test_frames.py` | 新建 | 无上限但别灌水 |
| `tests/test_gateway.py` | 新建 | 无上限但别灌水 |
| `docs/design/05-security.md` | **只允许** §1.1 加一小节说明 D1 的 ticket 机制 | ≤30 行 |

**白名单之外一律不动**。特别地：**不要**改 `storage/paths.py`（`wsPath` 的生成放 `server.py`）、
**不要**改 `pyproject.toml`、**不要**改 `02b-protocol.md`（帧形状没变）、**不要**改 `07-api-contract.md`
（`system.echo` 是内部调试方法，我会在验收后自己决定要不要登记）。

## 7. 测试要求

**必须**覆盖（这些是验收时会逐条独立复跑的点）：

1. `frames.py` 纯单测：合法 req 解码；`type` 错 → `INVALID_PARAMS`；缺 `id` → `INVALID_PARAMS`；
   `id` 超 128 → `INVALID_PARAMS`；非 JSON → `INVALID_PARAMS`；> 1 MiB → `FRAME_TOO_LARGE`；
   `encode_response` 产出含 `data` 与 `seq`；`encode_event` 形状对。
2. `test_gateway.py` 集成（**真子进程 + 真 WS 客户端**，参考 `test_server_boot.py` 的 `_spawn` 风格）：
   - 起真内核 → 从 `server.json` 取端口 → 用 `httpx` 取 `/` → 从 HTML 里**解析出 meta 里的 ticket**
     （用正则，不要用 HTML 解析库，避免新依赖）；
   - 用 `websockets` 客户端连 `/gateway?ticket=...` → 发 `system.echo` → 断言回包 `data` 回显且 `seq` 存在；
   - 再发一次 → 断言 `seq` 严格递增；
   - 发 `repo.bind` → 断言 `ok:false` 且 `error.code == "UNKNOWN_METHOD"`；
   - 发超 1 MiB 的帧 → 断言连接被拒/关闭（**说明清你断言了哪种行为**）；
   - 客户端主动断开 → 断言 `active_connection_count()` 归零（可另起一个进程内测试，见下）；
   - **同一 ticket 用两次** → 第二次连不上（一次性）。
3. 进程内集成（不起子进程，用 `uvicorn` + `websockets`，参考我已验证可行的探针）：
   - 断开后服务端清理：开 2 个连接，关 1 个，断言计数从 2 → 1；
   - 无 ticket / 错 ticket → 握手被拒。**我已实测过这条路，照这个断言**：
     服务端在 `accept()` **之前** `await ws.close(code=1008)`，客户端 `websockets` 抛
     `websockets.exceptions.InvalidStatus`，其消息含 `HTTP 403`。断言 `InvalidStatus` 且状态码 403 即可。
     **不要**先 `accept()` 再 `close()` —— 那样连接已经建立，算不上「拒绝」。
4. **回归**：`bash scripts/check.sh` 全绿，**且 S1 的 16 个用例全部仍然通过**（不许改它们的断言来凑绿）。

**禁止**：为了让测试过而放宽 S1 断言；用 mock 替掉真实 WS；`assert a or b`。跳过任一验收标准要用
`@pytest.mark.skip` 并在交付说明里写明理由。

## 8. 提交与交付

1. 在 `feat/s2-connection-state` 上做，**分 1~2 个提交**（建议：帧+网关一个，前端一个）。
   提交信息 `feat(s2): ...`，正文说明「为什么」而不只是「改了什么」。
2. 提交前 `bash scripts/check.sh` **必须全绿**（pre-commit 只跑快三项，完整门禁你自己跑）。
3. **不要** push，**不要**开 PR —— 我做审核后自己提交 PR。

## 9. 交付说明（你回复我的内容，按此结构写）

```
## 交付
- 提交：<hash list>
- 变更行数：X 新增 / Y 删除（`git diff main --stat` 的结果，原文贴）
- 触及文件数：N（逐一列出，与 §6 白名单对照）

## 验收标准逐条自检
A1: 怎么实现的 / 怎么验证的 / 结果
A2: ...
（每条都要写，A2 必须如实说明前端部分有多少是人工确认的、多少是自动断言的）

## 设计裁决的执行情况
D1~D6 逐条：照做了 / 偏离了（偏离必须说明原因，我会判断）

## 需要你知道的风险 / 我发现但没改的问题
（这一节写「你不想让我知道的事」，会显著影响我对你的信任评分）

## 我没做到 / 有疑问的地方
（不要美化）
```

## 10. 我会怎么审核你

不要猜我喜欢听什么，按这个来：

1. **逐条独立复跑** §4 的 A1~A5 与 §7 的测试要求，**不采信你的自检报告**。
2. 读代码，重点看：`finally` 里有没有清理、令牌/ticket 有没有进日志或页面、有没有 `except: pass` 掩盖错误。
3. 检查 §6 白名单有没有越界（`git diff main --stat` 逐文件比对）。
4. 检查 §7.4 回归：**S1 的测试文件有没有被改动**（`git diff main -- tests/test_server_boot.py tests/conftest.py` 必须为空）。
5. 门禁独立跑一遍。

**上一刀（S1）我退回了两处**：一处是硬编码版本号还编了个理由（「避免 import 环」，实测不成立），
一处是引用了 uvicorn 里不存在的属性并配了错误注释。**共同点是「让它看起来像对的」比「让它是对的」容易。**
S2 里同样的诱惑点在：ticket 有效期写成「反正够用」、超大帧「反正测了」、清理「反正 finally 里有」。
我会往这些点上看。
## 11. 我已替你验证过的技术事实（不要重复踩）

以下均在这台机器上**实测过**，不是查文档得来的。可以直接用：

1. **uvicorn 0.54.0 的 WebSocket 实现**：`uvicorn.protocols.websockets.auto` 先试 `websockets`，
   再试 `wsproto`，都没有则 `AutoWebSocketsProtocol = None`（即**无 WS 支持**）。S1 时两个都没装，
   所以是老版本依赖缺了 WS 能力。现已在 `pyproject.toml` 加 `websockets>=14`（实测 17.4 可用）。
2. **`server.run(sockets=[sock])` 与 WS 共存**：已用探针跑通，WS 与 HTTP 共用同一个 `sockets=[sock]`，
   不需要额外的 `ws=` 参数。
3. **拒绝握手**：`await ws.close(code=1008)`（不先 accept）→ 客户端得 `InvalidStatus` / HTTP 403（见 §7.3）。
4. **FastAPI 0.165 / Starlette 1.10** 的 `WebSocket` 对象有 `query_params`、`headers`、`accept`、`close`、
   `send_text`、`receive_text`、`receive_json`、`send_json`。
5. **`httpx` 不能测 WebSocket**（它没有 WS 支持），WS 客户端用 `websockets`；HTTP 取页面用 `httpx`。
6. **不要用 `TestClient` 测网关**：S1 的测试风格是「真实子进程 + 真实网络」，S2 保持一致。
   若某个内部行为（如 `active_connection_count`）用子进程测不到，用 §7.3 的进程内 uvicorn 方式，
   但**必须真的监听一个端口、真的连一个 WS**，不要用 ASGI 直调绕过网络。

## 12. 一个我拿不准、需要你判断的点（可以按你的判断做，但要在交付说明里说）

前端「≤3 秒内变红」这条**没法在 pytest 里验证 DOM**。我的约束是：让 JS 里的检测参数
（心跳间隔 / 重连延迟）成为**具名常量**且值明显小于 3 秒，然后用测试断言「这些常量 ≤ 3000」。
这只能证明**意图**，不能证明**实际表现**。

如果你有更好的做法（例如把重连状态机抽成可被 Node 跑的纯函数并单独测，或者用 `websockets` 服务端
模拟内核被杀来测前端逻辑），**采用它并在交付说明里写清楚**。若采用，注意不要为它引入前端构建链（D6 不变）。
