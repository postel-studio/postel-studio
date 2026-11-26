# 06 · 陷阱清单

> 上游：[README.md](README.md) · 相关：[05-change-impact](05-change-impact.md)

**这一页是整个 Code Map 最有价值的部分。** 每条都是**真人踩过**的坑，不是臆测。

**收录标准**：让某人花了超过 15 分钟才想明白的事。
**维护**：修掉后标「已修 + 怎么修的」，**不要删**——下一个人需要知道这里有坑。

---

## 启动与退出

### T1 ❌ 装信号处理器之前写 `server.json` → 留下残骸（已修）

**现象**：客户端一看见 `server.json` 就发 SIGTERM，进程直接死，`finally` 不执行，
盘上留 `server.json` + 锁。

**原因**：信号缺失省动作——`SIGTERM` 缺省是直接终止，**不抛异常**，所以 `finally` 不跑。
窗口期实测约 15~20ms。

**已修**：`_install_signal_handlers()` 移到写文件之前（`server.py:201-205`），
并加了 `_BUILDING`/`_STOP` 处理「装好处理器但 uvicorn 还没接管」的那段。

**别再踩**：这五步的顺序是硬要求，见 [03/server §顺序](03-modules/server.md#顺序是有讲究的五步不能换)。

### T2 ❌ 给 uvicorn 传 `port=` 或 `fd=`（已避免，别引入）

**后果**：`port=` 会让 uvicorn 自己 bind → 「取端口」与「监听端口」之间有空窗竞态，
两个内核可能同时认为自己拿到了一个端口；`fd=` 会触发 reload 子进程化并关掉继承的 socket。

**正确**：内核自己 `bind(0)` 拿到 socket，再 `server.run(sockets=[sock])`。

### T3 ⚠️ SIGKILL 后同端口重启必被 TIME_WAIT 挡住（**已知限制，未修**）

**现象**：浏览器标签页开着，内核被 `SIGKILL`，然后用同一端口重启 →
`LISTEN_FAILED: 端口 N 无法监听：Address already in use`。

**实测数据**：

| 场景 | 结果 |
|---|---|
| 标签页开着，内核 SIGTERM | ✅ 成功 |
| 标签页开着，内核 SIGKILL，等 3s / 10s / 30s | ❌ 全部失败 |
| 无浏览器连接，内核 SIGKILL | ✅ 成功 |

**原因**：标签页持有 keep-alive 连接，内核被 SIGKILL 时由**内核侧**发 FIN →
在 `127.0.0.1:PORT → client` 上留下**服务端端口**的 `TIME_WAIT`。
`SO_REUSEADDR`（已在用）与 `SO_REUSEPORT`（试过）**都救不了这一类**——它们只能复用
「远端端口」为服务端的 TIME_WAIT。实测该记录在 `tcp_fin_timeout=60` 下仍存活 >300s。

**为什么没修**：SIGKILL 不是正常用户路径（正常停止是 SIGTERM / Ctrl+C）。
缓解手段是客户端重连自身，不是内核硬抢端口。

**别再踩**：看到"同端口重启失败"不要以为是自己代码坏了，先看是不是 SIGKILL 场景。

---

## 日志

### T4 ❌ uvicorn 的 `LOGGING_CONFIG` 没有 root logger → 自己的日志全静默（已修）

**现象**：`logger.info(...)` 一条都不出现，连 `logger.warning(...)` 也不出现（取决于 lastResort）。

**实测**：

```python
>>> list(uvicorn.config.LOGGING_CONFIG)
['version', 'disable_existing_loggers', 'formatters', 'handlers', 'loggers']
>>> list(uvicorn.config.LOGGING_CONFIG['loggers'])
['uvicorn', 'uvicorn.error', 'uvicorn.access']     # 没有 root
```

**已修**：`log_config()` 把 `postel_studio` 显式挂到 uvicorn 的 `default` handler。

**为什么重要**：S2 有一条验收（断连后服务端清理）**就是读这条日志**。
没有这个修复，那条测试永远「测不到」。

### T5 ❌ `access_log=False` 管不住 WebSocket 访问日志（已修）

**现象**：

```
INFO: 127.0.0.1:51234 - "WebSocket /gateway/xxx?ticket=d9f3b91abf...8ad" [accepted]
```

**原因**：WS 的三条日志（`[accepted]` / `403` / 关闭码）走 **`uvicorn.error`** logger，
不是 `uvicorn.access`。

**已修**：`_WsQueryRedactor` 过滤 `uvicorn.error`，把 `?` 之后替换成 `?<REDACTED>`。

**两个附带的坑**：
1. logger 的 `filters` 列表**不能**写 `{"()": ...}`，会 `ValueError: Unable to add filter`，
   **让内核直接起不来**；
2. 只能在 `dictConfig` 执行完之后，对 logger 对象 `addFilter`。

**这是 05 §4.1「凭据绝不进日志」的硬约束，不是最佳实践。** 过滤器的覆盖范围只到
「带 `?` 且含 `WebSocket` 的行」——**新加日志时别自己再泄一遍**。

---

## 前端

### T6 ❌ 浏览器 `new WebSocket()` 不能设置请求头

**后果**：Bearer 令牌在浏览器侧**物理上不可用**。这不是我们实现得不好，是 Web 平台的限制。

**替代路的陷阱**：
- `?token=<长期令牌>` → 令牌进 URL → 进日志（05 §4.1）
- 渲染页面时注入 `<script>` → 违反「令牌不进页面源码」

**正解**：一次性 ticket（见 [03/gateway §认证](03-modules/gateway.md#认证一次性-handshake-ticketd1)）。

### T7 ❌ 用 `frame.data` 读 HTTP 路由的返回（已修，曾导致同端口重连也失败）

**现象**：前端按 WS 帧的 `{data, seq}` 包络去读 `/api/where`，拿到 `undefined`，
导致**连端口没变的重连也失败**。真浏览器实测才发现。

**原因**：`/api/health` 与 `/api/where` 回**裸对象**，WS 帧才用包络。
这是**有意的边界**（HTTP 用 HTTP 形状，WS 用帧形状），不是疏漏。

**别再踩**：看到「不一致」先确认是不是有意的边界。**新增 HTTP 路由保持裸对象**，
别引入第三种形状。

### T8 ❌ `style.display = ""` 会回退到 CSS 的 `display: none`（已踩到）

**现象**：想让按钮显示，写 `button.style.display = ""` → 反而不显示。

**原因**：`''` 清空内联样式 → 回退到样式表，而样式表里是 `#reconnect { display: none }`。

**正解**：显式给值 —— `button.style.display = state === "disconnected" ? "inline-block" : "none"`。
`test_page_has_reconnect_button` 断言的就是这一整句。

### T9 ⚠️ 前端 DOM 行为没有自动化测试

**现状**：所有前端测试都是**文本断言**（`'classList.toggle(...)' in page`）。
它们证明「这句话在字符串里」，**不证明「灯真的会变红」**。

**真实行为靠人工**：Chrome headless + 真内核 + SIGKILL，读 DOM。
S2 审核时实测：变红 5~22ms、按钮出现、灰化生效、换端口时文案正确且不跳页。

**为什么**：需要 Node + puppeteer，与「不得引入前端构建链」冲突。

**别再踩**：**不要因为 `test_gateway.py` 全绿就认为前端是验证过的。**

### T10 ❌ `location.reload()` 作为重连兜底（已删）

**现象**：端口变了时页面重载，但重载后地址还是旧的 → 仍然连不上，而且用户被"闪"了一下。

**正解**：不重载，`fail()` + 明确文案「内核已换端口或未启动，请从启动它的终端重新打开地址」。
`test_frontend_reports_port_change_and_does_not_guess` 断言页面里**根本没有** `location.reload()`。

---

## 协议与帧

### T11 ❌ `ClientDisconnected` 不是 `WebSocketDisconnect` 的子类（已修）

**现象**：每断一次连接，日志就打一条 `ERROR: Exception in ASGI application` 栈。

**原因**：发帧时发现对端已没，uvicorn 抛的是 `uvicorn.protocols.utils.ClientDisconnected`；
Starlette 的 `WebSocketDisconnect` 是**另一个**类型（实测 `issubclass` 为 `False`）。

**已修**：`except (WebSocketDisconnect, ClientDisconnect)`（`gateway.py:115`）。

**别再踩**：**别把它当冗余删掉。**

### T12 ❌ 帧大小按字符数算（已避免）

中文 1 字符 = 3 字节。字符数不超但字节超的帧必须拒。
`len(text.encode("utf-8"))`，不是 `len(text)`。
`test_frames.py::test_too_large_frame_is_rejected_in_bytes_not_chars` 守着。

### T13 ⚠️ `seq` 是**连接级**，不是全局

「检测丢帧」的语义只在单连接内成立（02b §2.2）。
新连接从 1 重新开始——**不要以为它是全局序号**，也不要为它加全局锁。

---

## 依赖与结构

### T14 ❌ 为绕开循环依赖而用函数内 import（已修，S2 审核退回）

**现象**：`gateway.py` 里 `from postel_studio.api.app import health`（函数内），
注释说「避免 import 环」。

**核实结果**：环**是真的**（模移到模块级 → `ImportError: cannot import name 'health'
from partially initialized module`）。**但结论是错的**——环是因为分层结构不好，
应该改结构（依赖注入），而不是绕开。

**已修**：`app` 把 `health` 作为 `Providers(health=health)` 注入给 `gateway`。

**判断标准**（见 [02-layers §2.1](02-layers.md#21-两处函数内-import的理由不同不要混为一谈)）：

| 理由 | 正当吗 |
|---|---|
| 懒加载（CLI 不想拖起 fastapi） | ✅ |
| 单一事实源（版本号只在包根定义） | ✅ |
| **绕开循环依赖** | ❌ 改结构 |

### T15 ❌ 硬编码版本号并声称「避免 import 环」（已修，S1 审核退回）

**现象**：`paths.py` 里出现 `_VERSION = "0.1.2"` 作为**第三份**版本号。

**核实**：那个「import 环」的理由**不成立**（我已实测），而且第三份副本会在发版时漏改。

**已修**：删掉，改为函数内 `from postel_studio import __version__`（**单一事实源**的正当用法）。

**⚠️ 现在版本号仍有**两个**合法副本**：`pyproject.toml:version` 与
`__init__.py:__version__`。发版时必须一起改。S20 应自动化这件事。

### T16 ❌ 引用不存在的第三方属性（已修，S1 审核退回）

**现象**：用了 `server._exit_if_started`，注释描述它在做什么。

**核实**：**uvicorn 0.54.0 源码里 0 命中**。是无效代码 + 错误注释。

**教训**：注释描述的行为必须能在依赖源码里找到。**"看起来像对的"比"是对的"容易得多**——
这是 AI 生成代码最常见的一类错误。

---

## 测试

### T17 ❌ `uv run` 的包装进程 pid ≠ 内核 pid

**现象**：测试「第二个实例启动应被拒绝」，结果第一个实例看起来死了。

**原因**：`uv run postel-studio serve` 会有**包装进程**，`Popen().pid` 拿到的是包装进程。
用 `os.kill(pid, 0)` 探测或发 SIGTERM 时打到了错的对象。

**正解**：测试直接跑 `.venv/bin/postel-studio`（`test_server_boot.py` 的 `CLI_BIN` 就是
`Path(sys.executable).with_name("postel-studio")`）。

### T18 ⚠️ 测试绝不能碰真实 `~/.postel`

`tests/conftest.py` 有两道锁：会话级 `POSTEL_HOME` + `Path.home` 改写。

**踩过的坑**：S1 复验时误启动了真实 `~/.postel`（用 `uv run` 且没传环境变量）。
残留文件被内核自己的 SIGTERM 清理机制删掉了，但**不该依赖这个**。

**别再踩**：任何新写的集成测试都要传 `POSTEL_HOME` 到 `tmp_path`。

### T19 ⚠️ 断言日志文案是脆弱的（已知，接受）

`test_server_cleans_up_connection_after_client_disconnect` 断言日志里有
`"网关连接关闭"` 与 `"当前活动连接 0"`。

这是**日志不是帧**，没有稳定错误码可断言。改日志措辞会弄挂这条测试。
**接受这个脆弱性**，但改措辞时要知道会连带改测试。

---

## 门禁

### T23 ❌ 变更检测只比 `main...HEAD` → 漏掉未提交的改动（已修，自己写的）

`scripts/gate-codemap.sh` 第一版只跑 `git diff --name-only main...HEAD`。结果是：
代码已经改了、还没提交 → 门禁说「未改代码，无需动代码地图」→ **放行**。
而 pre-commit 触发时，改动恰好就躺在暂存区，正好是这个被漏掉的状态。

**正解**：取三段并集——`base...HEAD` + `--cached` + 工作区，用 herestring 去重逐行判。
`tests/test_gate_codemap.py::test_detects_uncommitted_code_change` 专门守这一条；
把并集逻辑删掉后它会立刻变红（验证过）。

**教训**：门禁自己也要有测试，而且测试要能**让门禁失败**。
只跑「应该通过」的用例，等于没测。

### T24 ⚠️ 浅克隆下变更检测会静默退化成「跳过」

CI 默认 `actions/checkout` 是 `fetch-depth: 1`，没有 `origin/main`，门禁找不到基准就跳过——
**看起来是绿的，其实什么都没查**。

**正解**：`ci.yml` 的 checkout 必须带 `fetch-depth: 0`。脚本本身在找不到基准时打
`⚠️ 找不到基准分支…` 而不是装作通过，是第二道保险。

**通用**：任何「找不到数据就跳过」的检查都要**大声说出来**，否则它会悄悄变成装饰品。

---

## 安全

### T20 ❌ ticket 出现在日志里（已修，S2 发现）

见 T5。**这条单独列出是因为它最危险**：实现者在代码里写了「ticket 不进日志」的注释，
但 uvicorn 的访问日志把它整串打了出来。

**教训**：**「写了注释」不等于「做到了」**。验证一条安全约束的方式是**读真实输出**，
不是读代码意图。`test_ticket_never_reaches_kernel_logs` 就是按这个原则写的。

### T21 ⚠️ 威胁模型的边界必须如实

05 §0 明确：**防的是「Agent 跑偏」和「凭据不小心外泄」，不是「主机被攻陷」**。
同用户的 shell 权限、物理接触、内存取证**不在**威胁模型内。

**别再踩**：不要在某处实现一个"看起来更安全"的机制然后声称它解决了威胁模型外的问题。
也不要因为「同用户攻击者能绕过」就否定 ticket 机制——那是**另一个**威胁。

### T22 ⚠️ 凭据绝不进 Agent 的环境变量（S3+ 会出现）

05 §4.3 硬约束：Agent 子进程的环境变量中**不得包含**可用于 push 的令牌。
需要远端操作时由**内核**发起，而非把令牌交给 Agent。

**本刀还没有 Agent，但 S9/S10 会引入。** 到时要确保这条。

---

## 已知未决问题

| # | 问题 | 状态 |
|---|---|---|
| U1 | `test_overwrites_stale_lock_when_pid_is_dead` 偶发失败（`FileNotFoundError: server.json`），S2 期间出现 1 次，之后连跑 12 轮未复现 | **未定位根因**。怀疑与 SIGKILL 掉的子进程/端口残留有关，无证据 |
| U2 | ticket 每次渲染 `/` 都签发、60s 才清一次、**无内存上限** | 正常使用无碍，但没有上限就是没有上限。已裁决不修 |
| U3 | 一次重连会打两次 HTTP（`/api/where` + `/`）→ 留下 1 个未用 ticket | 不影响正确性，票据生成量比预期高一倍 |
| U4 | 页面从 `localhost:` 而非 `127.0.0.1:` 打开时未测试 | 端口比较仍成立，但该 variant 未验证 |
| U5 | 层依赖检查未进门禁（见 [02-layers §4](02-layers.md#4-可执行检查)） | 规则靠 review |
| U6 | 版本号有 `pyproject.toml` 与 `__init__.py` 两个副本 | S20 自动化 |
| U7 | `encode_event()` 从未被调用 | 形状已定，无调用方测试 |
| U8 | codemap 门禁只判「动没动地图」，不判「动得对不对」 | 内容质量靠 review；一点办法没有之前如实承认 |

---

## 一条通用教训（如果只能记住一句）

> **「让它看起来对」比「让它对」容易得多。**
>
> S1 退回的两处（硬编码版本号 + 引用不存在的属性）与 S2 退回的三处
> （端口/路径设计与契约冲突、ticket 泄进日志、函数内 import 绕环），
> **全都是"看起来是对的"**：注释说得通、代码能跑、测试能过。
>
> 区分二者的唯一办法是**独立验证**：跑真实输出、读依赖源码、删掉前提再看行为。
> 这也是审核者不能采信实现者自检报告的原因。