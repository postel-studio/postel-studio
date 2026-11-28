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

**S3 的写法（左栏仓库区）**：需要"按状态显隐一整组元素"时，**不要**在 JS 里逐个赋
`style.display`，只改父容器的一个 `data-state` 属性，显隐规则全部写在 CSS 里：

```css
#repos[data-state="loading"] #repo-list { display: none; }   /* CSS 说了算 */
#repos[data-state="ready"]   #repo-state { display: none; }
```

```js
reposEl.dataset.state = state === "connected" ? "ready" : state;   // ✅ 只改这一个属性
```

这样不会有"某一路忘了恢复 display"的半开状态，也不会踩 T8（CSS 里的 `display:none`
被内联的 `""` 回退继承）。`hidden` 属性（`#repos-error` / `#repo-empty`）也只由 JS 切
`true/false`，不从 `""` 开始。

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

### T39 ❌ 进程替换失败 → 变更检测把「读不到」当成「没改」（已修，Issue #17）

`gate-codemap.sh` 读改动清单原本用进程替换：

```bash
while IFS= read -r f; do [ -n "$f" ] && add "$f"; done < <(git diff --name-only "$base"...HEAD)
```

进程替换需要有 `/dev/fd`。受限容器 / 精简镜像里没有，重定向直接失败：

```text
scripts/gate-codemap.sh: 行 67: /dev/fd/63: 没有那个文件或目录
```

而重定向失败时 `while` 循环**一句都不读**，`changed` 保持空 → 门禁打印
「✅ 未改代码，无需动代码地图」→ **放行**。代码明明改了，门禁说没改。
这是 T24 的镜像形态：T24 是「找不到基准就跳过」，这个是「读不到清单就当空」——
两者都是**假绿**，但后者更糟：它说的不是「我没查」，而是「查过了，没问题」。

**为什么不显眼**：GitHub 的 `ubuntu-latest` 有 `/dev/fd`，CI 永远绿；
只有在这种环境里 `tests/test_gate_codemap.py` 那 5 条「必须拦得住」的用例会红
（实测：`5 failed, 299 passed`，红的全是这一个文件）。

**正解**：`collect()` 用临时文件（`mktemp` + `trap … EXIT` 清理）读清单，
**并且 fail-closed**——`git diff` 非零退出就打印原因并 `exit 1`，绝不报 ✅。
临时文件而不是管道，是因为 `add()` 要改当前 shell 的 `changed`；
管道会把 `while` 丢进子 shell，改动全丢。

**通用**：门禁只有两个方向，要么红，要么绿。**「查不到」必须落在红这一边**；
落在绿那边的每一次，都是在给下一个人一张假通行证。

### T40 ❌ 分支已存在时仍把 commit id 交给 `worktree add` → 静默变成游离 HEAD（S7 已处理）

`git worktree add` 有两种形态，差别不只是 `-b`，**检出对象也不同**：

| `create_branch` | 命令 | 检出什么 |
|---|---|---|
| `True` | `worktree add -b <branch> <path> <start_point>` | `start_point`（commit id） |
| `False` | `worktree add <path> <branch>` | 分支自己的 tip |

S7 的第一版两种形态都传了 `worktree add <path> <sha>`。于是"回收之后再分配"这条路径上：

```text
$ git -C demo worktree add /tmp/wt ffcfc715      # 分支 bot/builder/workspace 已经存在
exit 0                                            # 没有报错
$ git -C /tmp/wt symbolic-ref --short HEAD
fatal: 不是符号引用                          ← 游离 HEAD
$ git -C demo worktree list --porcelain
worktree /tmp/wt
HEAD 27999c10                                     ← 基础提交，不是分支的 tip
                                                  ← `branch` 字段整行消失
```

分支上已有的提交**看不见了**，`git worktree list` 里那条记录也不再显示分支名——
而 `worktree.list` 的过滤条件之一正是"记录里得有个分支"，于是页面上连工作区都不显示了。
**全程零报错**：`git worktree add` 认为它成功了。

**为什么测试第一版没抓到**：写的用例是"分配 → 回收 → 再分配，断言路径相同"，
而路径确实相同。断言 tip 的那条（`test_acquire_after_release_keeps_the_workspace_branch_tip`）
是 S7 补的——**只有"在工作区分支上提交过"的用例才能暴露它**。

**正解**：`add_worktree(create_branch=False)` 时把**分支名**交给 git，不传 sha（见
[gitops](03-modules/gitops.md#写面gitopsworktreespys7)）。`start_point` 只在建分支时使用。

**通用**：一个"幂等"的创建动作，第二次调用必须与第一次**等价**。等价的判据不是
"命令退出码 0"，而是"产物与第一次一致"——这里一致的是路径，不一致的是 HEAD 与分支。

### T41 ❌ 提交时禁用按钮 → 焦点掉回 `body` → 绑在 `<form>` 上的 Escape 静默失效（S8 已处理）

S8 的新建 Bot 表单有一条"Escape 收起表单"的键盘承诺。第一版把 `keydown` 绑在 `<form>` 上：

```js
botCreateForm.addEventListener("keydown", function (ev) {
  if (ev.key === "Escape") { ev.preventDefault(); closeBotCreate(); }
});
```

点击「创建」时，`submitBotCreate()` 先把提交按钮 `disabled = true`（防连点）。**浏览器在
"当前聚焦的元素被禁用"时会把焦点移到 `document.body`**——于是回包到达前的这几百毫秒里，
焦点已经不在表单里，`keydown` 不再冒泡到 `<form>`，Escape 完全没人接。回包失败后按钮虽被
重新启用，焦点也不会自己回来。

真实后果：填了一个非法名字 → 报错 → 想按 Escape 退出 → **没反应**（只能点「取消」）。

**是谁抓到的**：`tools/acceptance/s8_create_bot.mjs` 的第 6 步（真 Chrome 里按 Escape 再读
`#bot-create-form.hidden`）。pytest 里的静态断言（"页面里有这行监听"）当时是**绿的**——
它证明的是"意图存在"，不是"行为正确"（T9 的同一课，这次栽在键盘事件上）。

**正解**：

```js
document.addEventListener("keydown", function (ev) {
  if (ev.key === "Escape" && !botCreateForm.hidden) { ev.preventDefault(); closeBotCreate(); }
});
```

守卫是"表单开着才管"，所以不会抢别的面板的 Escape。另外失败回包会把焦点送回名字输入框
（人要改的就是那里）。

**通用**：凡是把键盘/焦点行为绑在**容器**上，都要问一句"焦点凭什么还在容器里？"
`disabled`、`hidden`、`display:none`、DOM 重建都会把它挤出去。**绑定范围要覆盖焦点可能
去的地方，判定条件要写清楚什么情况下才生效。**

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

---

## 用户数据与子进程（S3 首次出现）

### T25 ❌ `git -C <dir> rev-parse --show-toplevel` 会 discover 到父目录 → 普通目录被"绑定成功"（已修）

**现象**：粘 `/tmp`（一个普通目录）点「绑定」→ 成功，左栏出现 `demo`。

**原因**：`rev-parse --show-toplevel` **不是**"这里是不是仓库"的判据，它是"这里**属于**哪个
工作区"。git 会从 `-C` 给的目录一路向上找 `.git`：

```bash
$ mkdir -p /tmp/ps/outer && git -C /tmp/ps/outer init -q
$ mkdir -p /tmp/ps/plain
$ git -C /tmp/ps/plain rev-parse --show-toplevel
/tmp/ps/plain          # 普通目录自带 .git → 是根
$ mkdir -p /tmp/ps/outer/sub
$ git -C /tmp/ps/outer/sub rev-parse --show-toplevel
/tmp/ps/outer          # 子目录 → 父仓库根（这是 D2 要的正确行为）
```

两条行为共用一个命令，但语义不同。测试里更糟：`tmp_path` 下必然有一堆平级仓库，
于是**每个**普通目录都会被答"是"。终态修法试了三版：

| 版本 | 做法 | 结果 |
|---|---|---|
| v1 | `repo_root()` 返回非 `None` 就算仓库 | ❌ 普通目录、`/tmp` 都过了 |
| v2 | 再要求 `root == target` | ❌ 子目录校正（D2）全断，粘 `proj/src` 变 `NOT_A_REPO` |
| v3 | 要求 `target.is_relative_to(root.parent)`（"根在目标祖先链的紧邻一级"） | ❌ `deep` 的祖先链就含 `root.parent`，恒真 |
| **v4（现行）** | **`target/.git` 存在 → 是根；否则逐级向上找第一个带 `.git` 的祖先；都不是 → `NOT_A_REPO`** | ✅ |

**已修**：`api/repository.py:_bindable_root()` —— 只看**文件系统**（`path/.git` 是否存在），
**不拿 git 的 discover 当判据**；裸仓库才问 git（`rev-parse --is-bare-repository`）。

**别再踩**：
- `gitops.repo_root()` / `is_repo()` 的 docstring 里都写了"别单独当判据"，不是客套话。
- 回归锁：`test_bind.py::test_root_level_plain_directory_that_contains_a_repo_is_not_a_repo`，
  它专门在一个普通目录下面建一个真仓库来制造歧义。
- **接受的近似**：`outer/proj/plain/deep` 与 `outer/proj/src` 在路径上无法区分，前者会被
  校到 `outer/proj`（见 U11）。这条如实记着，别假装解决了。

### T26 ❌ SQLite 的 `ON CONFLICT` 只更新**命中那条约束**的列 → `repo_id` 静默不更新（已修）

**现象**：仓库先无 origin 绑定，再 `git remote add origin` 重绑 → 返回值是新的 URL 哈希
`89473a01e438`，但库里那一行的 `repo_id` 还是旧的路径哈希。**同一条记录，契约是两个 id。**

**原因**（最小复现，不经过本项目代码）：

```python
SQL = (
    "INSERT INTO repos VALUES (?,?,?,?,?) "
    "ON CONFLICT(repo_id) DO UPDATE SET path=excluded.path "
    "ON CONFLICT(path) DO UPDATE SET origin_url=excluded.origin_url"
)
c.execute(SQL, ("aaa", "/p", None, "main", 1.0))
# 第二次命中的是 path 约束；repo_id 不在那个分支的 SET 里
c.execute(SQL, ("bbb", "/p", "https://u", "main", 2.0))
# 结果：[('aaa', '/p', 'https://u', 'main', 1.0)]   ← repo_id 仍是 aaa
```

`ON CONFLICT(path)` 分支只执行它自己 `DO UPDATE SET` 里列的列；`repo_id` 不在里面就不更新。
（而且这条语句只允许"一个冲突目标命中"，两条 `ON CONFLICT` 是 SQLite 的扩展写法，
真正的 upsert 想把 "id 冲突" 与 "path 冲突" 两件事都办掉，可读性也很差。）

**已修**：`storage/db.py:RepoStore.upsert()` 换成**一个事务里的三步**——
按 `path` 命中 → `UPDATE`（含 `repo_id`）；否则按 `repo_id` 命中 → `UPDATE`（含 `path`）；
都没有 → `INSERT`。三行 SQL，每步都能单独读懂。

**别再踩**：把 `upsert` 改回单条 `INSERT … ON CONFLICT` 之前，先跑
`test_rebinding_after_origin_added_updates_the_row`。

> **S3.5 更新**：这条测试的语义已经反过来了——现在断言的是 **id 不变**（U9 已修，见 T32 与
> 08 §6）。所以 `upsert` 按 path 命中的那条 `UPDATE` **不许**再写 `repo_id`。

### T27 ❌ 断连时的空列表会撒谎（已修）

**现象（S3 初版设计时会踩）**：仓库区只在"连上"时渲染；断连时 `repo-list` 为空、`repo-empty`
也不显示——看起来像"没有仓库"。

**D1 的原话**：「若内核不在（连接断了），左栏就没有仓库列表。UI 必须显示「已断开」而不是空列表」。

**已修**：`#repos` 有四个 `data-state`（`loading` / `reconnecting` / `ready` / `disconnected`），
**初始值就是 `loading`**；只有 `ready` 时才显示列表与空态，其它三种状态显示一句状态文案
（「已断开：内核没有连着，仓库列表未知」）并隐藏 `#repo-list` 与 `#repo-empty`
（`assets.py:217` 的 `setRepoState()`）。

**别再踩**：
- 空态（「还没有绑定仓库」）**只能**在"真的拿到了一个数组"之后出现。
- 浏览器验收时专门做了这一步：CDP 断网 → 读 DOM（`listVisible:false`、
  `emptyVisible:false`、`stateText:"已断开：…"`）→ 恢复网络 → 点「重连」→ 列表回来。
- pytest 里**没有**这个行为的自动化测试（T9 的老问题）。

### T28 ⚠️ 测试环境的 `tmp_path` 下必然有别的仓库 —— 别拿它当"普通目录"

这是 T25 的推论，单独列出来因为它坑人不只一次：**测试里构造"非 git 目录"时，
不能只 `mkdir` 一个 `tmp_path/xxx`**，因为 `tmp_path` 的其他兄弟目录可能已经是仓库。
S3 的 `repo_factory` 把仓库统一建在 `tmp_path/repos/` 下，而"非仓库"用例显式
`shutil.rmtree(x / ".git")`，就是为了让意图（"这确实不是仓库"）不依赖运气。

---

## 审核退回（S3 复审）

### T29 ❌ 粘 `.git` 内部路径会被**静默改绑到外层仓库**（审核发现，已修）

**现象**（审核者构造，实测复现）：

```
粘 <repo>/.git          → 成功，落库 path = <repo>
粘 <repo>/.git/objects  → 成功，落库 path = <repo>
```

**原因**：T25 的修法里那条"逐级向上找第一个带 `.git` 的祖先"——`.git/objects` 的祖先链上
正好有 `<repo>/.git`，于是它被当成"`<repo>` 的子目录"校上去了。**D2 那个坑（用户以为绑了 A
其实绑了 B）换了入口重现**：用户明确粘的是 `.git` 内部，系统不报错，还绑了别的东西。
我自己的 U 清单里没有它，因为我的对照表只想到"普通目录"，没想到"仓库自己的实现细节"。

**已修（第一版，有缺陷）**：`_inside_git_dir()` —— "目标自身或任何祖先的名字是 `.git`"
→ `NOT_A_REPO`。**这一版本身是坏的，见 T31。** 判据最终落成"相对已确认的 `root`"：

```python
relative = target.relative_to(root)
if any(part == ".git" for part in relative.parts):
    return True
```

即：**从仓库根走到目标的那一段路里有没有 `.git`**。root 由 `_bindable_root()` 先求出，
`_inside_git_dir(root, target)` 再判——判据必须拿到 root，不能只看绝对路径。

| 路径 | root | 相对路径 | 判定 |
|---|---|---|---|
| `outer`（工作区根） | 它自己 | `""` | 能绑 |
| `outer/src`（真子目录） | `outer` | `src` | 能绑（校到 `outer`） |
| `outer/.git` / `outer/.git/objects` | `outer` | `.git` / `.git/objects` | **`NOT_A_REPO`** |
| worktree 根（`.git` 是**文件**） | 它自己 | `""` | 能绑（**别把它一起打死**） |
| `/tmp/edge/.git/workspace`（父目录叫 `.git` 的合法仓库） | 它自己 | `""` | **能绑（T31）** |

**别再踩**：
- 回归锁三条：`test_bind_git_directory_itself_is_not_a_repo`、
  `test_bind_path_inside_the_git_directory_is_not_a_repo`、
  `test_bind_refuses_any_path_with_a_dot_git_segment`；防误伤三条：
  `test_worktree_root_with_git_file_still_binds`、
  `test_worktree_subdirectory_still_corrects_to_the_worktree_root`、
  `test_repo_root_whose_parent_is_named_dot_git_still_binds`（+ 它的子目录版）。
- 反向验证（三条，都在 `test_reverse_checks.py` 里固化了）：删掉判定 → 3 条红；只判尾段
  → `.git/objects` 逃逸；**改成"整条绝对路径任意段 = `.git`" → 误杀合法仓库（T31 那条红）**。
- **教训**：判定"用户粘的是不是仓库入口"时，要连同"这个路径在仓库内部扮演什么角色"一起想
  （`.git`、`.git/objects`、`.git/index` 都是实现细节，不是入口）。只想到"目录 vs 仓库"
  会漏掉一整类。

### T31 ❌ 拒绝得太多：父目录恰好叫 `.git` 的**合法仓库**被误杀（复审核回，已修）

**现象**（复审核退时实测）：

```
/tmp/edge/.git/workspace     ← 一个合法仓库：自己有 .git、有提交、能 git status
  → NOT_A_REPO               ❌ 它只是"父目录正好叫 .git"
```

**原因**：T29 的第一版写成 `any(part == ".git" for part in target.parts)`，判的是**整条绝对
路径**的每一段。于是只要 `/tmp` · `/home/u/proj` 之上任何一层目录叫 `.git`，下面就全被拒。
**这是修 T29 时自己引进的新缺陷**：我把"用户粘了 `.git` 内部"和"路径里恰好有 `.git` 这个名字"
混成一件事——前者相对于**仓库根**有意义，后者相对于**文件系统根**没有意义。

**已修**：先求 `root`，再判 `target.relative_to(root)` 里有没有 `.git` 段（见 T29 的代码块）。
`root == target` 时相对路径为空，若 `target.name == ".git"` 仍拒绝（直接粘 `.git` 的兜底）。

**别再踩**：
- 两条回归锁：`test_repo_root_whose_parent_is_named_dot_git_still_binds`、
  `test_subdirectory_of_a_repo_whos_parent_is_named_dot_git_still_corrects`。
- 反向验证：把判据换回"整条绝对路径任意段 = `.git`" → 上述两条红（已固化在
  `test_reverse_checks.py` 的 `T31：...` 那一条）。
- **通用教训（比缺陷本身值钱）**：**"拒绝得太少"和"拒绝得太多"要有一对反向锁**。
  任何"不许 X"的判定，都要同时写一条"合法的 X' 必须仍然能通过"——否则修完泄漏很容易
  顺手改出一个误杀，而误杀在测试里**看起来是绿的**（原来的用例只测"该拒的拒了"）。
  S3 两次退回都是这个形状：第一次退"漏了 `.git`"，第二次退"补得太狠"。

### T30 ❌ 反向验证里的"变绿假象"：注入跑了，但被测测试读的**不是被注入的代码**（已修）

固化 `tests/test_reverse_checks.py` 时踩到三次，每一次都表现为"注入缺陷了，测试还是绿的"：

| # | 假象 | 根因 |
|---|---|---|
| 1 | `Kernel`（真内核子进程）看到的是**已安装的包**，不是 `PYTHONPATH` 指的副本 | `gateway_util.Kernel` 用 `.venv/bin/postel-studio` 且 `env.pop("PYTHONPATH")`。要让内核读副本得用 `python -m pytest`（`-m` 把 cwd 放进 `sys.path[0]`） |
| 2 | 静态 AST 守卫从**测试文件**反推 `src/`（`__file__.parents[1]/"src"`），在副本里运行时它还是读仓库里的原文件 | 被测文件在 `/tmp/.../src`、测试文件还在仓库里，两者不在同一棵树。**从已 import 的包反推**：`Path(repository_mod.__file__).parents[2]` |
| 3 | 守卫里"全项目只有 `gitops/repo.py` 起子进程"那段从测试文件反推 `src`，副本里该路径不存在 → `rglob` 空 → **断言空过** | 同上；改成从**被测文件**反推包目录（`repo.py` → `gitops` → `postel_studio`），并加一句"包目录必须存在"的断言 |

**已修**：三处都改成"从被 import 的对象反推真实位置"；`test_reverse_checks.py` 里还有一条
`test_sanity_the_copy_is_really_what_runs` 专门钉住前提——副本不生效就当场红，而不是让
15 条注入一起"看起来对"。

**别再踩**：写任何"改代码再看结果"的机制，**先写一条证明机制真的生效的用例**。
没这条用例，反向验证本身就是最容易被镀金的东西（和 06 顶部的通用教训是同一件事）。

### T32 ❌ 反向验证只能红一半：「删除任一处保护」**注不出** U9（S3.5 踩到）

**现象**：给 U9（`repo_id` 加 origin 就变）写反向验证时，按直觉注入了两处各自的缺陷——
"把 id 算法改回 URL 哈希"、"让 `upsert` 覆盖 `repo_id`"——结果**两条注入之后，验收 2/3 的
用例全是绿的**。第一反应是"测试漏了"，实际不是。

**原因**：U9 需要**两处同时存在**才复现：

| 状态 | 验收 2/3 的用例 | 为什么 |
|---|---|---|
| (a) 算法依赖 origin **且** (b) `upsert` 覆盖 id（= S3 的真实代码） | **红** | 加 origin → 算出新 id → 覆盖进库 |
| 只有 (a)：算法依赖 origin | 绿 | 规则 1 不写 `repo_id`，算出的 URL 哈希被丢弃 |
| 只有 (b)：`upsert` 覆盖 id | 绿 | 新算法对**同一路径**算出的 id 本来就相同，覆盖了也是同一个值 |

S3.5 修完之后两处都被修好，于是单独回退任意一处都不会让 id 变——这不是测试的问题，
是**两道锁各自独立成立**。能红的注入只有 (a)+(b) 的完整形态。

**怎么处理的**：`tests/test_reverse_checks.py` 里加了 `COMPOSITE_INJECTIONS`（一条注入可改
多个文件片段，只用于 (a)+(b)）；单独两半各写成 `test_each_half_of_u9_alone_*`，**断言它们
仍然绿**，并在 docstring 里写清"为什么绿"。

**顺带抓到的两个"红错原因"**（都固化进了机制）：

1. 注入 `(b)` 时只改 SQL 没改参数元组 → 测试红在
   `sqlite3.ProgrammingError: Incorrect number of bindings supplied` 上。这次"变红"什么也
   没证明。现在 `_RUNTIME_BREAKAGES` 会把这类红判失败。
2. 锚点静默失配：S3.5 改完 `db.py` 后，旧 T26 那条注入的锚点（`UPDATE repos SET repo_id = ?,
   …`）**在源码里已经不存在**，`old in text` 检查若只做一次 `assert` 而不数字数，会让这条
   注入退化成空操作而**永远绿**。现在 `_apply_patches` 要求 `text.count(old) == 1`。

**别再踩**：
- 写反向验证之前先问一句："这个缺陷是**一处**代码造成的，还是**两处**缺一不可？"
- 一处缺陷 → 单点注入；两处 → 组合注入 + 把"单独一半仍然绿"也写成用例。
- **"注入之后测试是绿的"有两种解释**：测试漏了，或者**另一半在兜底**。先证明是哪一种，
  别急着把测试改红。

---

## 分支列表与 ref 解析（S4 首次出现）

### T33 ❌ `%(HEAD)` 的空格是**真值**；整段 strip 只改最后一行的 marker，不改字段数（S4 已处理）

**现象**：用一次 `for-each-ref` 同时取"分支列表"与"当前分支"时（S4 D3 的固定命令），
`%(HEAD)` 有两个反直觉之处：

1. **非当前分支的 marker 是 `" "`（一个空格），不是空串**——它和当前分支的 `"*"` 一样是**真值**。
   用（未 strip 的）marker 做真值判断（`if marker` / `bool(marker)`）判"当前"，
   **除最后一条外全部判反**。
2. `_stdout()` 对**整段** stdout 做 `.strip()`，只会把**最后一条记录**的 marker 从 `" "` 变成
   `""`（**值**变了）——`%00` 分隔符是 NUL、**不是空白**，行尾的 NUL 不会被 strip 掉，
   所以 split 的**字段数恒为 4**（末段是空串）。**字段不会消失**，"整段 strip 会改变段数"是错的。
   实测单分支 / 多分支 / 游离 HEAD / 最后一条有 upstream 等布局，段数全是 4。

**怎么复现**（真跑过；输出就是下面这三行）：

```bash
tmp=$(mktemp -d)
git -c init.defaultBranch=main -C "$tmp" init -q
git -C "$tmp" -c user.email=a@b -c user.name=a commit -q --allow-empty -m init
git -C "$tmp" branch feature
git -C "$tmp" branch zz
fmt='%(refname:lstrip=2)%00%(objectname)%00%(upstream:short)%00%(HEAD)'
git -C "$tmp" for-each-ref refs/heads/ --format="$fmt" | od -c
# feature 行末尾: \0 \0 空格 \n    ← 非当前 = 空格（不是空串）
# main    行末尾: \0 \0 * \n       ← 当前 = *
# zz      行末尾: \0 \0 空格 \n

python3 - "$tmp" <<'PY'
import subprocess, sys
tmp = sys.argv[1]
fmt = "%(refname:lstrip=2)%00%(objectname)%00%(upstream:short)%00%(HEAD)"
out = subprocess.run(
    ["git", "-C", tmp, "for-each-ref", "refs/heads/", "--format=" + fmt],
    capture_output=True, text=True,
).stdout
for line in out.strip().splitlines():
    fields = line.split("\x00")
    print(f"字段数={len(fields)} 名字={fields[0]!r} marker={fields[3]!r} 真值判当前={bool(fields[3])}")
PY
# 真实输出：
# 字段数=4 名字='feature' marker=' ' 真值判当前=True   ← 判反（非当前被当成当前）
# 字段数=4 名字='main'    marker='*' 真值判当前=True
# 字段数=4 名字='zz'      marker=''  真值判当前=False  ← 最后一条，碰巧对
```

**为什么这个坑特别阴**：真值判断下，**最后一条**非当前分支因为被整段 strip 成 `""`，
反而**碰巧判对**。于是：

- 只测单分支仓库（当前分支在最后一条）→ 通过；
- 只断言最后一行的用例 → 通过；
- 只有"多分支里非最后一条的非当前分支"才会露馅。

**怎么防**（`gitops/repo.py:list_branches`，S4 的写法）：

- 字段逐个 `.strip()`，当前判定写 `marker == "*"`，**不要**用真值判断；
- `len(fields) != 4` 只可能意味着 `--format` 被改写 / git 换了实现：**不猜**（猜成"非当前"
  等于把坏数据当正常结果），整体返回 `None` —— "问不出来"是 D4 已经定义好的诚实答案
  （帧层翻成 `REPO_NOT_BOUND`）；
- 回归测试：`tests/test_branches.py` 的 **D7 第 2 行**（断言**每一条**分支的 `isCurrent`，
  且 `feature` 是非最后一条的非当前分支）、**第 3 行**（当前分支恰是最后一条且有 upstream）、
  **第 7 行**（`color.ui always` + 非 ASCII 名）；
- 反向验证：`tests/test_reverse_checks.py` 里把 `marker == "*"` 注入成 `bool(fields[3])`
  → 上述用例**真的变红**（已固化；T32 的纪律：注入必须真的红，且红在断言上）。

**教训**：① 布尔"标记字段"的空值可能是**真值**（空格），永远用显式比较，别用真值；
② 整段 strip 改变的是**值**，不一定是**段数**——解析器和它的"怎么复现"都要盯实测字节；
③ 只测边界（单分支 / 最后一条）的用例会给 bug 开后门，**列表类断言要遍历每一条**。

## 差异视图（S5：第一次把用户 ref 送进 git）

### T34 ❌ git 的用户配置会改 diff 输出——每一条都要一个 flag 钉死（S5 已处理）

**现象**：同一个仓库、同一对 commit，用户机器上的 git 配置不同会让 `repo.diff` 给出**不同答案**。
这不是过度设计：下面每条都在 `/tmp` 里**真跑过**（git 2.53）：

| 配置 | 不钉 flag 会怎样 | 钉法 |
|---|---|---|
| `color.ui=always` | patch 里混入 ANSI：`od -c` 首字节是 `033 [ 1 m`（`\x1b[1m`） | `--no-color` |
| `diff.external=false` | patch 直接失败：`fatal: 外部 diff 退出，停止在 f.txt`，exit 128 | `--no-ext-diff` |
| `.gitattributes` + `diff.<driver>.textconv` | hunk 内容被换成外部命令输出（实测 `-SECRET` / `+b` 而不是 `-a` / `+b`） | `--no-textconv` |
| `diff.renames`（缺省 **true**） | 重命名算 1 个文件（`0 0 old => new`）；用户设 false 就变 2 个文件（`0 2 old` + `2 0 new`） | `--no-renames`（MVP 裁决：重命名 = 一删一增，D4） |
| `diff.context=1` | hunk 从 `@@ -2,7 +2,7 @@` 变成 `@@ -4,3 +4,3 @@` | `--unified=3` |
| `core.quotePath`（缺省 true） | 非 ASCII 路径变 `"\344\270\255\346\226\207.txt"`；关掉后是 `中文.txt` | `-c core.quotePath=false`（**必须放在 `diff` 子命令之前**） |

**怎么复现**（S5 实测脚本的形状）：

```bash
tmp=$(mktemp -d); cd "$tmp"; git init -q --initial-branch=main p; cd p
git -c user.email=a@b -c user.name=a commit -q --allow-empty -m init
printf 'one\ntwo\n' > f.txt; git add -A; git -c user.email=a@b -c user.name=a commit -q -m base
git checkout -q -b feature; printf 'one\nTWO\n' > f.txt; git add -A
git -c user.email=a@b -c user.name=a commit -q -m change; git checkout -q main
git config color.ui always
git diff --no-ext-diff main...feature | od -c | head -1   # 033 [ 1 m ...
git diff --no-color --no-ext-diff main...feature | head -1 # diff --git a/f.txt b/f.txt
git config --unset color.ui; git config diff.external false
git diff main...feature >/dev/null; echo "exit=$?"          # exit=128（外部 diff）
git diff --no-ext-diff main...feature | head -1; echo "exit=$?"  # diff --git ...; exit=0
```

**教训**：① "git 的输出"不是你的输出——用户的全局/仓库配置都参与；② 定 flag 时**逐条**想
"哪个配置会改这个字节"，并写成测试；③ 反向验证：把 `--no-color` / `--no-ext-diff` 从
`DIFF_FLAGS` 里删掉 → `test_patch_has_no_ansi_when_color_ui_always` /
`test_patch_ignores_external_diff_driver` **真的变红**（D12 #1/#2 已固化）。
**位置**：`gitops/repo.py` 的 `DIFF_FLAGS` / `STAT_FLAGS` / `_DIFF_CONFIG`。

### T35 ❌ 三点 diff 没有共同祖先时 git 报的是"无合并基线"，不是 500（S5 已处理）

**现象**：`git checkout --orphan` 造两个不相干历史后：

```text
$ git diff --numstat main...other
fatal: main...other：无合并基线        # exit 128，stdout 空
```

`repo.diff` 的契约是 `ok:false` + `BAD_REF` + 一句人话「两个 ref 没有共同祖先，无法比较」，
**不是** `INTERNAL`（那会把正常处境报成"未预期错误需报障"），也**不新增码**（07 §1.3 里
`BAD_REF` 是语义最贴的：分支/提交不存在——比较基线不存在）。回归：`test_unrelated_histories_are_bad_ref`。

**别再踩**：不要把 `git diff` 的非零退出统一翻成 `INTERNAL`；D7 的表里没有一行是"未预期"——
先按**语义**找最近的既有码。

### T36 ❌ `--numstat` 必须 `-z`；而且不能和 `--unified` 同给（S5 已处理）

**现象 1（路径可含制表/换行）**：`--numstat` 的默认输出按 `\n` 分行，路径里的换行会被转义成
`"new\nline.txt"`（带引号）——按行切、再按 `\t` 切都会坏。**必须** `-z`：

```text
$ git -c core.quotePath=false diff --numstat -z --no-renames main...feature | od -c
-  \t   -  \t   b . b i n \0            ← 二进制：-/-，计 1 个文件
0  \t   0  \t   m o d e . s h \0        ← 纯 mode 变更：0/0
1  \t   0  \t   n e w \n l i n e . t x t \0   ← 路径里的换行**原样**在 NUL 记录里
1  \t   0  \t   t a b \t n a m e . t x t \0   ← 路径里的 tab 也是原样
```

解析时**只取前两段**（`record.split("\t", 2)`），路径怎么脏都不影响统计。

**现象 2（`--unified` 会把 patch 混回来）**：`--numstat` 隐含 `--no-patch`，但 `--unified`
隐含 `--patch` 并把它覆盖回来——git 2.53 实测：

```text
$ git diff --numstat -z --unified=3 main...feature | od -c | head -2
1 \t 1 \t f . t x t \0 \0 d i f f   - - g i t   a / f . t x t ...
                ↑ numstat 记录          ↑ 完整 patch 也出来了
```

按 NUL 切记录时，patch 段会被当成第 2 条"记录"，解析直接崩或被静默算错。所以
`STAT_FLAGS` 是 `DIFF_FLAGS` **去掉 `--unified`** 的那一组；`--unified=3` 只给 patch 用。

**回归**：`test_stat_counts_binary_file_without_line_counts` / `test_stat_counts_mode_only_change`
/ `test_rename_counts_as_delete_plus_add` / `test_stat_exact_numbers`（S5 的 -z 解析）；
`--unified` 那条由 `test_patch_contains_expected_line_kinds` + 实现里的 `STAT_FLAGS` 守着。

### T37 ❌ 页面 JS 的转义要在 Python 字符串里**多写一层**（S5 真浏览器抓到）

**现象**：S5 的 `#diff` 面板加完后，`tests/test_diff.py` 的 7 条页面契约**全绿**，但真 Chrome 打开
页面时整页 JS 不执行：`PAGEERROR: SyntaxError: Invalid or unexpected token`，`#repos` 永远停在
`loading`（`s5_diff.mjs` 卡在等 `#repos=ready` 超时）。

**根因**：页面 JS 是 `assets.py` 里的一个 Python 三引号字符串。JS 想要 `patch.split("\n")`，
Python 源码必须写 `patch.split("\\n")`——写成 `"\n"` 时 Python 会在渲染阶段把它变成**真换行**，
于是浏览器看到的是跨行的字符串字面量。同理 JS 里的单反斜杠（`line.indexOf("\\")`）在 Python
源码里要写四个反斜杠。

**实测**：

```text
$ python -c 'from postel_studio.web import assets; …'   # 把渲染后的 <script> 抽出来
$ node --check /tmp/page.js
/tmp/page.js:355
    var lines = patch.split("
                            ^
SyntaxError: Invalid or unexpected token
```

改成 Python 源码 `"\\n"` 后 `node --check` 通过、整页 JS 正常。

**为什么 pytest 抓不到**：那 7 条页面契约是 `assert "…" in page` 的**文本断言**（T9 的老问题），
它们证明"这句话在页面里"，不证明"浏览器能解析这段 JS"。`06 §5` 写了不要求单测覆盖 DOM——
但**语法错误是静态可查的**：改内联 JS 后跑 `node --check`（或直接跑真浏览器验收脚本）。

**教训**：① 在 Python 里写内联 JS，每一层转义都要数一遍（`\n` / `\\` / `\x1b`）；
② 文本断言全绿 ≠ 页面能跑——改前端后**必须**跑 `tools/acceptance/*.mjs`（DoD §3.2 同一纪律）；
③ 抓到后先把现象记下来：这条坑是 S5 实施时真踩到、真修掉的。

## Bot 与 charter（S6：第一次把用户可编辑文本落进状态根）

### T38 ❌ `mkdir(mode=0o700)` 只管叶子、`write_text` 是 0644 —— 权限必须逐层显式 chmod（S6 已处理）

**现象**：`<状态根>/bots/<repo_id>/<bot>/` 要求目录 `0700`、`charter.md` `0600`（05 §4.1）。
只写 `Path("a/b/c").mkdir(parents=True, mode=0o700)` + `write_text(...)` 时，`stat` 出来是
`0755 / 0755 / 0700` 与 `0644`——**父目录与文件都比契约松**。

**实测（umask 022，D3 的原始命令）**：

```text
$ python -c "from pathlib import Path; import stat; Path('a/b/c').mkdir(parents=True, mode=0o700); print([oct(stat.S_IMODE(p.stat().st_mode)) for p in (Path('a'),Path('a/b'),Path('a/b/c'))])"
['0o755', '0o755', '0o700']
$ python -c "from pathlib import Path; Path('a/b/c/f.txt').write_text('x'); import stat; print(oct(stat.S_IMODE(Path('a/b/c/f.txt').stat().st_mode)))"
0o644
```

**修法**：每一层建完显式 `_chmod(path, 0o700)`；写 charter 用 `paths.atomic_write`
（mkstemp `0600` → fsync → rename，顺带保证读到的永远是完整文件，不会读到半截 charter）。

**测试纪律**：权限用例必须断言**真实 `stat` mode**，并且**先把 umask 设成 `0o022` 再跑**——
本机 umask 恰好是 077 时，去掉 chmod 的反向注入不会变红（那就是假测试）。

**防线**：`tests/test_bot_charter.py::test_ensure_memory_space_layout_and_permissions`
（D11 #5）+ 反向注入 D12 #2（`write_text` 替 `atomic_write`）/#3（去掉逐层 chmod）。

### T42 ❌ 「空对象」被当成「不支持」→ 全能 Agent 报出零能力，页面看起来还正常（S9 已处理）

ACP 的 `initialize` 应答里，`agentCapabilities` 同时存在三种形状（实测 OpenCode 1.19.6）：

```jsonc
{"loadSession": true,                              // 布尔叶子
 "mcpCapabilities": {"http": true, "sse": true},    // 命名空间 + 布尔
 "sessionCapabilities": {"close": {}, "fork": {}}}  // 命名空间 + **空对象**
```

直觉写法是「读到 `true` 才算支持」，于是 `sessionCapabilities` 一条都不产出——而这个 Agent
明明声明了四类会话能力（`close` / `fork` / `list` / `resume`）。页面不会报错：它只是显示一个
更短的列表，看起来像「这个 Agent 就会这么多」。

**为什么险**：没有任何断言会红。错误的方向是「少报」，而少报在 UI 上完全合法——用户不会
知道自己少看了四项。

**正解**：ACP 用**键存在**表达支持，值是该特性的选项容器。派生要按形状分支：

| 值 | 含义 | 产出 |
|---|---|---|
| `true` | 支持 | 键名 |
| `{}` / `{...}` | 支持（可能带选项） | `命名空间.子键名` |
| `false` / 缺失 | 不支持 | 不产出 |

**防线**：`agent/acp_client.py:70` `flatten_capabilities`；`test_flags_match_the_measured_response`
拿真实应答的九个 flag 做对照；`test_flags_are_derived_not_hardcoded` 用八组形状参数化；
反向注入 S9 #1（改成返回固定列表）必须让它变红。

**通用**：解析外部协议时，「值」的含义问协议，别按语言的类型直觉。空容器在不同协议里可能
是「没有」，也可能是「有，且无选项」——这一字之差决定功能有没有。

### T43 ⚠️ 「答完了」不等于「退出了」——回收要用 pid 证据（S9 已处理）

握手成功只证明「对方答了」。`opencode acp` 这类长驻进程答完**不会自己退出**，如果实现只在
成功路径上返回、忘了 `terminate`，每点一次「检测 Agent」就漏一个进程，而所有断言都是绿的。

**做法**：`initialize` 的 `finally` 里无条件 `stdin.close → terminate → wait(5) → kill → wait`
（`agent/acp_client.py:144`）。测试让假服务端「答完故意不退」，并让它把自己的 pid 写到文件；
断言用 `os.kill(pid, 0)` 抛 `ProcessLookupError`——这条同时证明 `wait` 过（否则 zombie 还占着 pid）。

**通用**：判断「资源释放了」不能看返回值，要看资源本身。进程看 pid，文件看 `exists`，
锁看能不能再取。反向注入 S9 #2 把 `terminate` 换成 `return`，目标测试必须红。

## 派活与编排（S10 首次出现）

### T44 ❌ `transition` 只拿了线程锁、没拿连接 → UPDATE 的写事务永不提交，堵死所有写（已修）

症状极其隐蔽：单测全绿，真实服务里**第一个任务之后**所有写操作 `database is locked`。
定位过程用了逐步 label：把疑点从 orchestrator 缩到 `TaskStore.transition`，再在方法里
插 `self._conn.in_transaction` 检查——发现方法返回后连接**仍在事务里**。

根因：第一版只写了 `with self._lock`（保护并发），没写 `with self._conn`（提交事务）。
`sqlite3` 的 `Connection` 上下文管理器的语义是**事务边界**（退出时 commit/rollback），
不是连接开关；UPDATE 一执行就隐式开写事务，不提交就一直持着写锁，把其它连接全部堵死。
修法一行：`with self._lock, self._conn:`。

**为什么单测没抓到**：测试各自用临时库、每个连接用完即弃，锁死的连接随测试结束消失；
只有「同一个库被多个连接长期共享」的真实形状才会暴露。

**通用**：手写锁管并发，事务边界管持久化——**二者替代不了对方**。任何「拿了锁就完事」
的写方法都要问一句：事务谁提交？防线：`tests/test_dispatch.py` 的 race 测试与
`mark_interrupted` 后的读回断言；教训记录在 [storage-db §transition](03-modules/storage-db.md) 与
[06-data-model §7](../design/06-data-model.md)。

### T45 ⚠️ 反向注入要打**组合拳**：单删一道防线时，下一道会兜住（S10 踩到）

S10 的 BUSY 有两道防线：orchestrator 的活跃预检 + `tasks_active_per_bot` 部分唯一索引
（D5 的设计：检查给友好错误，正确性靠索引）。第一版反向注入只删预检——
**测试保持绿**，因为 `IntegrityError` 照样被翻成 `BUSY`，注入看起来「没生效」。

这不是注入失败，是防线冗余按设计工作。正解：注入必须**同时**删预检 + 删索引
（`_RUNTIME_BREAKAGES` 里的 S10 #2 是一条组合项），BUSY 测试才会红。

**通用**：给「多层防御」写反向锁时，先画清楚每层**单独**被拆后行为如何变化——
如果删掉一层行为不变，那层就不该单独注入；如果删掉全部行为才变，注入就是组合项。
「注入了但测试还绿」要先怀疑防线冗余，再怀疑注入没打中被测代码（T30 的教训）。

### T46 ❌ ULID 的**前缀**不能当短 id：同毫秒必然撞车（S11 实测）

候选分支名第一版用 `task_id[:8].lower()`。实测两个连续派活的任务 ID 是
`01M8E3EYAZ10VYDVQ...` 与 `01M8E3EYAZ10VYDVQ...`——同毫秒时前 40 位全是时间戳，
前缀**必然**相同，第二个任务直接 `failed` + `INTERNAL`「候选分支已存在：
bot/builder/01m8e3ey」。

**正解**：取 ULID 的**随机尾部** 8 位（`task_id[-8:]`），同毫秒碰撞概率 2^-40。
防线：`test_candidate_branch_name_is_predictable_and_sanitized` + 反向注入 S11 #1。
**通用**：给 ULID/时间戳 id 取"短 id"时先问：截的是时间段还是随机段？截时间段等于
把"同一毫秒"变成确定性碰撞。

### T47 ⚠️  redundant defence 的反向注入要拆到最后一道（S11）

S11 的"无事可做"有三道守卫（commit 前、commit 后、`diff_stat == 0`）。反向注入第一版只拆
第一道——测试仍绿；拆前两道——仍绿，第三道兜住；**三道全拆**才红。
这是 T45（BUSY 双锁）的同一教训第二次出现，已把 S11 #3 登记成组合注入。
**通用**：写注入前先数一数代码里有几道等价守卫；"注入了但绿"先怀疑冗余，再怀疑注入没打中。

### T49 ⚠️ 改 `data-state` 取值要同步所有验收脚本（S13 实测）

`#candidate[data-state]` 从 S11/S12 的 `done` 改成 S13 的 `reviewState` 后，s11/s12
验收脚本里 `=== "done"` 的等待条件永远不满足（CHECK 6 / CHECK 15 超时）。
**通用**：`data-state` 是 DOM 契约，和 id 一样要全仓 grep 所有断言；前端改它的取值集合时，
把 `tools/acceptance/*.mjs` 当作消费者同步更新，并在 spec 里显式记录破坏性变更。

### T48 ⚠️ 验收脚本的 check(ok, label) 参数顺序写反 → 全程假绿（S13 实测）

`s12_inbox.mjs` 的调用写成 `check("标签", 布尔表达式)`，而 `check(ok, label, detail)`
把第一个参数当布尔——标签是非空字符串（truthy），于是**每条检查都 ✓**，布尔表达式
被打印成 `true`/`false` 当标签。直到 `CHECK ✓ 10 false` 才暴露。

**正解**：`check()` 同时兼容两种顺序（`typeof a === "string" ? b : a`），并把 s12/s13
两个脚本都修好重跑；修后 s12 仍 17/17 真绿、s13 暴露出一条真实失败（默认列表未隐藏
rejected），修正后 14/14。**通用**：验收脚本的 `✓` 也要反向注入一次——故意让一条检查失败，
确认计数真的加了；否则"全绿"可能只是断言没接上。

## 已知未决问题

**S4.5 起的规则**：每项必须有「到期切片」——到那一刀开工前，要么修掉，要么明确豁免并写清后果。
没有到期的未决项等于「永远不会解决」，不允许存在。依赖某个 U 项的切片**不得先开工**（[09 §1.4](../design/09-slice-dod.md)）。

**flaky 政策**：连续 3 次尝试仍无法复现的失败 → 标记 quarantine（不进主套件）+ 写下环境与种子，
并在本项到期前修掉；**禁止静默重试**把不稳定的测试「修绿」。

| # | 问题 | 状态 | 到期（必须裁决的切片） | 不解决会怎样 |
|---|---|---|---|---|
| U1 | **同族三个 flake，全在 `tests/test_server_boot.py`（锁/启停路径）**：<br>① `test_overwrites_stale_lock_when_pid_is_dead`（`FileNotFoundError: server.json`），S2 期间 1 次、之后连跑 12 轮未复现；<br>② `test_garbage_lock_is_treated_as_stale`（`assert _stop(proc) == 0` 得 1），**PR #24 / #25 两次独立全量复跑中均失败**，单独 3/3 通过；<br>③ `test_sigint_deletes_server_json_and_leaves_no_process`，PR #24 实现期全量跑偶发 2 次，单独 4/4、后续两轮全量通过。<br>**环境（②③④ 的实测）**：干净 detached worktree、`uv sync --frozen`、Python 3.12.15、Node.js 22.22.2 / Google Chrome 157（浏览器脚本无关，仅记录完整环境）；覆盖 PR head `54074bb`（#24）与 `c5121a4`（#25）。<br>**复现率**：② 全量 2/2 失败、单独 0/3；③ 全量 2/6 失败、单独 0/4；① 全量 0/12。 | **未定位根因**。怀疑与 SIGKILL 掉的子进程/端口残留、全量负载下的进程回收竞态有关，无证据。**不进 quarantine**（未达到「连续 3 次单独复现失败」的政策门槛——相反，单独 3～4 次全过；失败只在全量负载下出现）；先登记环境与复现率，S15 前定位 | **S20（M3 验收）前**（S15 裁决顺延：S15 的真重启测试用确定性假 ACP + 真内核，不依赖这三条路径；单独复跑均过、只在全量负载偶发，本刀无法在预算内定位） | S15 主体就是重启/锁路径，flaky 会让它分不清「改坏了」还是「本来就不稳」。② 在 PR #24/#25 的独立复跑中让 `check.sh test` 红，而 CI 同一 head 绿——若不在 S15 前定位，后续无法用门禁区分回归与环境噪声 |
| U2 | ticket 每次渲染 `/` 都签发、60s 才清一次、**无内存上限** | 正常使用无碍，但没有上限就是没有上限。已裁决不修 | **M3 上线前** | 长期运行内存单调增长；配合 U3 放大 |
| U3 | 一次重连会打两次 HTTP（`/api/where` + `/`）→ 留下 1 个未用 ticket | 不影响正确性，票据生成量比预期高一倍 | **M3 上线前**（或明确豁免） | 票据生成量翻倍，与 U2 同一条内存方向 |
| U4 | 页面从 `localhost:` 而非 `127.0.0.1:` 打开时未测试 | 端口比较仍成立，但该 variant 未验证 | **M1 验收（S14）前** | M1 验收若从 `localhost:` 打开，结论建立在未验证的变体上 |
| U5 | 层依赖检查未进门禁（见 [02-layers §4](02-layers.md#4-可执行检查)） | ✅ **已修（S4.5）**：`scripts/gate-layers.py` 进门禁（`check.sh layers` + CI），且**新模块不登记层就失败**，不再靠 review。文档里那段片段已退役 | ✅ **已修（S4.5）** | 规则靠 review，新模块可静默向上 import——已被 `scripts/gate-layers.py` 收走 |
| U6 | 版本号有 `pyproject.toml` 与 `__init__.py` 两个副本 | S20 自动化 | **S20（发版）前** | 两个版本号副本，发版时必然出现「包上是 A、运行时是 B」 |
| U7 | `encode_event()` 从未被调用（S2 登记） | ✅ **已收口（S10 起，S12 复核确认）**：`api/gateway.py:241-245` 的每连接事件泵真实调用 `frames.encode_event()`；`task.updated` / `candidate.proposed` 两个 topic 都有真实订阅者，S12 收件箱沿用同一泵，未新增 topic。原「到期 S12」的复核已完成 | ✅ **已收口（S12 复核）** | — |
| U8 | codemap 门禁只判「动没动地图」，不判「动得对不对」 | **部分结掉（S4.5）**：机械可判的部分（相对链接 / 锚点 / `#L` 行号 / 模块行数表 / 页面 id 一致性）已由 `scripts/gate-docs.py` 进门禁；**文字内容质量仍靠 review**，这一半如实承认没办法 | ✅ **部分结掉（S4.5）** | 机械可判的部分已进门禁；**文字内容质量仍靠 review**，如实承认 |
| U9 | **仓库加了 origin 之后 `repo_id` 会变**（S3 的已知让步）：界面/后续切片挂在 id 上会"丢失"这个仓库 | ✅ **已修（S3.5）。**怎么修的：`repo_id` 改成 `sha256(绑定时的绝对路径)`、**只在 INSERT 时算一次**，`upsert` 按 path 命中时不再写它（D1）；origin 的哈希降级成新列 `origin_hash`，只识别"同一远端"、不参与主键（D2）；schema v1→v2 只加列、**不重写任何历史 id**（D3）。详见 [08 §6](../design/08-repo-id-stability.md) 与本文 §T32。 | ✅ **已修（S3.5）** | — |
| U10 | `state.db` 的连接**没地方关**（`create_app()` 构造 `RepoStore`，进程退出时由 OS 回收） | 不丢数据（WAL 已 fsync），但"有开必有关"的纪律是破的。将来 `server.serve()` 的 `finally` 应该关它 | **S15：在 `server.serve()` 的 `finally` 里关闭 store（收口）** | 生命周期是 S15 的主题；「有开必有关」的纪律破着，进程内测试还可能锁库 |
| U11 | **普通目录放在别人工作区里会被校到那个仓库**（`proj/plain/deep` 与 `proj/src` 路径上不可区分） | 按 D2 的字面语义处理的**近似**。真要解决需要"区分子目录与普通目录"的判据（例如要求子目录里有被跟踪的文件），代价大且会引入误判，本刀不做 | **明确豁免（不修）** | 用户粘普通子目录会被校到父仓库——已作为 D2 的近似记录在案 |
| U12 | `tmp_path` 属于测试夹具，`repo.bind` 允许绑系统临时目录下的仓库 | 行为正确（用户没理由这么做），但测试里必须注意 T28 | **无需修（行为正确）** | 测试夹具踩坑风险，已写进 T28 提醒 |
| U13 | **worktree 的 origin / 默认分支会答"链接工作区自己的事"**：`origin_url` 得 `None`（S3.5 起 `repo_id` **永远**是路径哈希，与 origin 无关，所以 id 本身不再有这个问题；`origin_hash` 则是 `None`）、`default_branch` 答该 worktree 的分支名而不是主仓库的 | S3 未修（S7 才引入 worktree）。三条事实与推荐做法见 [03/gitops §worktree](03-modules/gitops.md)。测试只断言"能绑、id 12 位"，**不断言 `main`** | ✅ **已裁决（S7）：明确豁免** | **实测（git 2.53）**：`git -C <linked worktree> config --get remote.origin.url` **答得出** origin（共享配置），所以第一条不复现；第二条成立（HEAD 答工作区自己的分支，这是对的）。S7 的工作区卡片只显示**它自己的**分支，不显示 origin / 默认分支，所以本刀不依赖它。到期改 S19（doctor 可以如实报告"这个路径是一个 linked worktree"），不解决的后果：把 worktree 当独立仓库绑定时，左栏的"默认分支"显示的是那条工作区分支名。 |
| U14 | **移动仓库后重绑会产生第二行，旧行指向已不存在的路径**（S3.5 D1 的**副作用**，不是实现 bug）：绑定 `/repo-a` → `mv /repo-a /repo-b` → 绑 `/repo-b`，`repo.list` 出两行，旧行 `path` 已不存在。根因：`repo_id` 从「origin 哈希」改成「路径哈希」后，`upsert` 的规则 2（"没有 path 行、但 `repo_id` 已存在 → UPDATE 那行的 path"）几乎不可达——`repo_id = hash(path)` 意味着"id 存在但 path 不存在"只可能是 v1 遗留的 URL 哈希行。S3 时代 id 来自 origin，移动后 origin 不变，规则 2 把 path 更新掉，仍是一行 | **未修（下一刀的事，本刀只记录）**。影响：左栏出现一条打不开的坏记录；S4 起对它查分支报 `REPO_NOT_BOUND`（「仓库路径已不存在」，D5 的裁决）。**建议修法**：现在有 `origin_hash` 了，可在"路径与 id 都没命中"时用 `origin_hash` 找一行 `path` 已不存在的记录，`UPDATE` 它的 `path`（**保留 `repo_id`**——id 是身份，不因路径变而变，符合 D1 的精神）。**注意**：这会把 `origin_hash` 从"只用于去重提示"升格成匹配键，与 D2「不参与主键/外键」有张力；且 `origin_hash` 为 `NULL` 的老行匹配不到、两行同 `origin_hash` 时选哪行也要定。要不要做、做成什么样，由 S4 前的裁决定。见 [08 §7](../design/08-repo-id-stability.md) | ✅ **已裁决（S7）：明确豁免，到期 S19** | 修它要把 `origin_hash` 从"只用于识别"升格成**匹配键**，还要定两条尚不存在的规则（`NULL` 老行怎么办、两行同 hash 选哪行）——那是数据模型层的一刀，且 04 §6 禁止一个 PR 混两条用户路径。**后果照旧**：移动仓库后左栏留一条打不开的记录、`~/.postel/worktrees/<旧 id>/` 变成孤儿目录（06 §5：永不自动删除，S19 的 doctor 报告）。S7 本身不依赖身份跨移动存活：工作区按用户**实际点到的**那一行（repo_id）建。 |
| U15 | **非 UTF-8 refname 会让 `repo.branches` 与 `repo.bind` 抛未捕获的 `UnicodeDecodeError`**（S3 就有的脆性，S4 未修）。触发：`git branch $'bad-\xff'` 之后——① 查分支：`for-each-ref` 的输出含非法 UTF-8 字节 → `run_git(text=True)` 严格解码抛 `UnicodeDecodeError`；它不在 `GIT_ERRORS`（只有 `GitUnavailableError` / `GitTimeoutError`）里，网关 `except Exception` 回 `INTERNAL`（**内核不崩、连接还能继续用 `system.health`**）；② 把 HEAD 切到该分支再 `repo.bind`：`default_branch()` 的 `symbolic-ref` 同样解码失败 → `bind` 回 `INTERNAL`（用户只看到「内部错误」）。根因：`subprocess.run(text=True)` 的严格 UTF-8 解码 + 异常清单不含解码错。影响：这类仓库看不到分支、绑不上，但不丢数据、不崩内核。**为什么现在不顺手修**：`errors="surrogateescape"` 会让载荷里带 lone surrogate，WS 发帧时编码会炸；`errors="replace"` 会让分支名显示成 `�`，且 S5 的 `repo.diff(base=…)` 把 mangled 名字回传给 git 会变成 `BAD_REF`（甚至指向别的 ref）。**建议**：先裁决「ref 名怎么过边界」（例如只接受 UTF-8 合法 ref 名并回稳定码，或 L0 隔离成永不回传的表示），在 S5（引入 ref 参数）之前定。本刀只记录 + 在 `run_git` docstring 指路。**→ ✅ 结案（S5）**：按 D3.2 的裁决修 —— `run_git` 增加 `decode_errors`（缺省 `strict`），严格模式解码失败抛新异常 `GitOutputUndecodableError`（加进 `GIT_ERRORS`）；`api/repository.py` 把 `repo.branches` / `repo.bind` / `repo.diff` 三条路径一律翻成 `NOT_SUPPORTED` +「仓库含非 UTF-8 的名称，第一版不支持」，不再回 `INTERNAL`。实测触发：`git branch $'bad-\xff'` 之后 `for-each-ref` 原始输出是 `b'bad-\xff\nmain\n'`（严格解码在 position 4 抛 `UnicodeDecodeError`）；JSON 的 lone surrogate 经 `surrogateescape` 会变成真字节、`rev-parse --verify` 真的能解析成功——所以入站也挡（D3.1）。回归：`tests/test_diff.py` D10 第 19/20 行 + 反向注入 D12 #3。 | ✅ **已修（S5）** | — | ref 名回传 git 会变 `BAD_REF`，甚至指向别的 ref——比报错更糟 |
| U16 | **`repo.diff` 把完整 patch 读进内存再截断**：`diff_patch` 先拿全量 stdout（`run_git` 只有 5s 超时，没有字节上限），边界层才 `[:maxChars]`。一个 200MB 的 diff 会先整个进内存再切 100k | S5 未修（`maxChars` 只限制**回给客户端**的大小，不是内核内存）。默认上限 100k 让客户端侧受控，但内核侧仍全量读 | **S18（空态/错误态与大仓库体检）前** | 超大 diff 可能内存尖峰、拖慢整个网关（git 调用在 WS 处理链路上）；届时裁决：流式读 + 字节上限，或明确豁免并写进 05 安全红线 |
| U17 | **`bot.list` 惰性播种 + `charter_rev` 未维护**（S6 的两处临时形态）。① 惰性播种：`bot.list` 是读方法，却会在该 repo **一个 Bot 都没有**时落一行（S6 D2 的权宜：M1 没有创建入口，而 S6 之前绑定的老仓库只有惰性播种才能自动补上）；② `bots.charter_rev` 列 v3 就建了，但**只在创建时写一次**（D5：charter 正文的事实源是 `charter.md`，载荷里的 `charterRev` 现场按内容算；改 charter 的写路径是 S22 的 `charter.set`）。 | **① ✅ 已结案（S8 D7）**：`_seed` 与 `PRESET_BOT_NAME` 删除，`bot.list` 变成纯读，空仓库回 `[]`（`test_bot_list_returns_created_bots_and_repeats_identically` / `test_list_on_a_repo_with_no_bots_writes_nothing`）。**② 仍未修**（`charter_rev` 允许过期）。 | ① 已到期并完成；② 到期 **S22**（`memory.charter.set`） | ① 无——代价已消失。② 有人去读那一列会读到过期值：**今天没有任何读路径**，S22 必须改成由 `charter.set` 维护它，或者直接删掉这一列。 |
| U18 | **`bot.agentKind` 仍不可写**：S8 的 `_refuse_agent_kind` 拒绝一切非 null 值，理由是「S9 之前没有任何通道被验证过」。S9 验证了 `acp-stdio`，但**没有**开这个字段——因为它没有「选通道」的 UI，接受页面产生不了的值等于接口先于用户路径（S9 D13）。 | ✅ **已收口（S10 D3）**：派活的通道选择裁决为**缺省第一条已登记通道**（`orchestrator._channel_for`：`bot.agentKind` 优先、缺省 `acp-stdio`；库里存了未登记的 kind 按坏数据报 `INVALID_PARAMS`，不静默回退）。`bot.create` 对非 null `agentKind` **仍拒绝**——页面依然产生不了这个值，但派活不再依赖它 | ✅ **已收口（S10）** | — |
| U19 | **握手在事件循环里同步执行**：`api/gateway.py:191` 在 async WS handler 里直接调同步的 `dispatch()`；`agent.capabilities` 最坏把**所有** WS/HTTP 请求（含 `system.health`）冻结 30s + 两次 5s reap grace。S9 之前 `worktree add` 已有同样形状，但 S9 让它变成一次普通按钮点击就能触发。 | **部分收口（S10）**：派活这条**分钟级**路径已移出事件循环——`task.dispatch` 只做毫秒级校验与建行，慢活全部在 worker 线程（`orchestrator._run`），ACP 会话在 `acp_client.Session` 里阻塞等（worker 不在事件循环里，实测一轮 8.7s 不影响其它请求）。**但 `dispatch()` 本身仍是同步调用**：`agent.capabilities` / `worktree.acquire` 的 30s 最坏情况还在事件循环里，属既有架构，本刀不顺手改（预算纪律）——升级为独立项 U20 | **U19 主体已收口（S10）；残余进 U20（到期 S15 前，重启恢复会重构 dispatch 形状）** | 派活不再冻结页面；握手/建工作区的长操作仍可能卡别的请求几十秒 |
| U20 | **`dispatch()` 仍在事件循环里同步执行**（U19 的残余）：`agent.capabilities` 最坏 30s + 两次 5s reap grace、`worktree.acquire` 最坏几秒，期间所有 WS/HTTP 请求冻结。S10 把分钟级的派活移出了事件循环（worker 线程），但这条既有形状没动——本刀只加不重构。 | **未修（S10 只收口了派活路径）** | 到期 **S20（M3 验收）必须收口，不再顺延**（S16 D7 裁决：S16 是 P2 最高安全项，`src/` 预算集中在权限规则/执行前阻断/审计；U20 是性能问题且要改 async 网关 + `asyncio.to_thread`，与权限门形状不同） | 点「检测 Agent」期间页面其它按钮（health、切仓库）最长卡 40 秒 |
| U22 | **opencode 版本不识别顶层 `permissions`（v1 用 `permission`/`bash`）**：规则不生效 = 越权可能落空 | **已实现（S16 D6 + review must-fix 2）**：`Session.__init__` 在 `initialize` 之后、`session/new` **之前**用 `supports_top_level_permissions(agentInfo.version)` 探测（major≥2 才认，缺失/异常一律不认）；v1 抛 `AcpPermissionsUnsupportedError`（`AcpProtocolError` 子类，orchestrator 仍翻 `failed` + `INTERNAL`，不新增错误码），`gate` 接住写 `FALLBACK` 审计（`resource=agent-permissions-unsupported`），UI 的 `loadAudit` 见 FALLBACK 就把该 Bot 徽标标「（不可用）」。假 ACP `v1` 模式断言 `session/new` 从未发出；反向注入 S16 #5/#6 守住探测与徽标 | ✅ **已收口（fail-closed，不静默放行）** | — |
| U21 | **真实 opencode 集成用例依赖外部免费额度**：`tests/test_dispatch.py::test_real_integration_creates_file_and_dirty_worktree` 用真 `opencode acp`（默认 `opencode/big-pickle`）。额度耗尽时 prompt **既不回终态帧也不报错**（日志 `AI_APICallError: Free usage limit reached`），任务永远停在 `running` → 180s 超时；另一形态是终态 `failed/INTERNAL`（维护者独立复跑 0/3）。默认免费模型账号级限额，换免费模型无效。 | **✅ 已收口（S13 收尾）**：测试注入 `session_factory`，用 `XDG_DATA_HOME` 指回本机真实 opencode 凭据库、`OPENCODE_CONFIG_CONTENT` 指定付费模型 `deepseek/deepseek-flash`（凭据只由 opencode 自己读，测试不碰 key），真跑真绿——3/3 独立复跑全 `passed`（2~3s）。无 opencode / 无 deepseek 凭据才 skip；两种失败形态（超时、`failed/INTERNAL`）都只在拿到明确限流日志证据时才跳过，泛化 `INTERNAL` 照常 fail。产品代码零改动（无 prompt 超时，S10 D8 不变） | ✅ **已收口（S13 收尾）**；若日后 deepseek 凭据失效，测试会明确 skip 而非假绿 | — |


| U23 | **SIGKILL 落在 migration 的 `executescript` 与 `user_version` 写入之间 → 下一任连接撞 `table candidates already exists`**：PR #32 CI 的 `test_real_kernel_restart_interrupts_half_done_task` 确定性失败（本地全量偶发）。`executescript` 隐式 commit，`with conn` 保不住这一步原子性；表/列已建但版本还停在旧值。 | **已修（S17）**：`storage/db.py::migrate` 捕获「table already exists / duplicate column name / duplicate index」三类「对象已在」错误，若 `user_version >= version`（说明这一步其实做完了）就推进版本继续，其它错误照常抛；`tests/test_recovery.py::test_migration_is_idempotent_when_version_lags` 确定性复现（建库 → 版本退回 0 → 重开必须幂等成功），反向注入 S17 #4 守住 | ✅ **已收口（幂等迁移）** | — |

---

### S17 ❌ `request()` 的回调是 `(err, data)`，不是 `(frame)`（已踩到）

`assets.py` 的 `request(method, params, done)` 在 `onMessage` 里成功时调 `done(null, result.data)`、
失败时 `done(code, null)`（07 §1.3：第一参数是错误码，不读 message）。
S16 的 `loadAudit` 把回调写成 `function (frame) { frame.ok / frame.data … }`——**两个字段都取不到**，
审计面板实际上永远显示「审计读取失败」。S17 的 `loadContext` 最初照抄了这个形状，真浏览器
验收里 context 面板一直停在 `error`（`s17_context.mjs` 的第 2 步抓到），改成
`function (err, data) { if (err || !data) … }` 后 6/6 CHECK 全绿。

**别再踩**：写一个走 `request()` 的新面板，先去看 `onMessage` 的 `done(null, result.data)` 那一行，
不要照抄 `loadAudit`。S16 的 `loadAudit` 是本刀之外的遗留 bug（本刀不顺手改，避免扩大 diff；
下一个人读到这段请修它）。静态 DOM 契约测试只能守 id 与 `innerHTML` 零使用，**守不住回调形状**
——真浏览器脚本才能抓到。

## 一条通用教训（如果只能记住一句）

> **「让它看起来对」比「让它对」容易得多。**
>
> S1 退回的两处（硬编码版本号 + 引用不存在的属性）与 S2 退回的三处
> （端口/路径设计与契约冲突、ticket 泄进日志、函数内 import 绕环），
> **全都是"看起来是对的"**：注释说得通、代码能跑、测试能过。
>
> S3 的三条（T25/T26/T27）同一类：git 的 discover "看起来像在问是不是仓库"、
> `ON CONFLICT` "看起来像 upsert"、断连时的空列表 "看起来像没有仓库"。
> **三条都是先写对、后由测试或真浏览器抓出来的**——不是靠读代码发现的。
>
> 区分二者的唯一办法是**独立验证**：跑真实输出、读依赖源码、删掉前提再看行为。
> 这也是审核者不能采信实现者自检报告的原因。
