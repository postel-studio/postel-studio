# 04 · 按能力反查代码

> 上游：[README.md](README.md) · 相关：[05-change-impact](05-change-impact.md)

**用途**：当你只知道「用户能做什么」，想知道「代码在哪」时看这一页。

---

## 1. 已实现的能力（S1、S2）

### C1 · 打开页面看到内核存活

| 项 | 位置 |
|---|---|
| 用户路径 | 终端打印 `http://127.0.0.1:PORT` → 浏览器打开 |
| 打印地址 | `api/server.py:224`（`_run` 里那几行 `print`） |
| 渲染页面 | web/assets.py:1429 `render_index()` |
| 路由 | `api/app.py:76` `GET /` |
| 由谁守住 | `test_server_boot.py::test_stdout_prints_clickable_loopback_url`、`::test_page_shows_version_and_state_root` |

### C2 · 单实例保护（不重复启动）

| 项 | 位置 |
|---|---|
| 用户路径 | 已有一个内核时再启动 → 退出码 1 + `KERNEL_ALREADY_RUNNING` |
| 抢锁 | `storage/paths.py:180` `acquire_lock()` |
| 判定存活 | `storage/paths.py:149` `pid_alive()` |
| 由谁守住 | `test_server_boot.py::test_refuses_to_start_when_lock_pid_is_alive`、`::test_overwrites_stale_lock_when_pid_is_dead`、`::test_garbage_lock_is_treated_as_stale` |

### C3 · 优雅退出且不留残骸

| 项 | 位置 |
|---|---|
| 用户路径 | `Ctrl+C` 或 `kill` → 进程退出、三个文件全删 |
| 信号处理器 | `api/server.py:164` `_install_signal_handlers()` |
| 收场 | `api/server.py:245`（`_run` 的 `finally`） |
| 建设期竞态保护 | `api/server.py:66-66` `_BUILDING` / `_STOP` |
| 由谁守住 | `test_server_boot.py::test_sigterm_deletes_server_json_and_leaves_no_process`、`::test_sigint_...` |

### C4 · 页面显示连接状态（三态）

| 项 | 位置 |
|---|---|
| 用户路径 | 右上角状态灯：已连接 / 已断开 / 重连中 |
| DOM | `web/assets.py` 里 `id="status"`、`id="status-text"` |
| 状态切换 | `web/assets.py:527` `setState()` |
| 由谁守住 | `test_gateway.py::test_page_declares_status_lamp_and_three_states`（**文本断言**） |

### C5 · 断连 ≤3 秒可见 + 灰化 + 重连按钮

| 项 | 位置 |
|---|---|
| 心跳判活 | `web/assets.py:1327` `startHeartbeat()` |
| 唯一向下出口 | `web/assets.py:1282` `fail()` |
| 灰化规则 | `web/assets.py:117-119` `#content.stale` / `::after` |
| 按钮显示 | `setState()` 里 `button.style.display = ...` |
| 由谁守住 | `test_gateway.py::test_server_cleans_up_connection_after_client_disconnect`（**后端侧**）、`::test_page_has_stale_rule_and_toggles_it_on_disconnect`（**文本**） |
| ⚠️ 真实 DOM 行为 | **无自动化测试**，靠人工浏览器验证 |

### C6 · 点重连恢复（端口未变时）

| 项 | 位置 |
|---|---|
| 重连入口 | `web/assets.py:1388` `reconnect()` |
| 取新票 | `web/assets.py:1338` `freshTicket()` |
| 端口校验 | `web/assets.py:1349` `PORT_CHANGED` |
| 建连 | `web/assets.py:1364` `connect()` |
| 由谁守住 | `test_gateway.py::test_reconnect_after_disconnect_works_without_restart`（网关侧）、`::test_frontend_reports_port_change_and_does_not_guess`（文本） |
| ⚠️ 边界 | **端口变了无法自动恢复**（D7），页面会明确告知 |

### C7 · 帧往返（`system.echo`）

| 项 | 位置 |
|---|---|
| 用户路径 | 无 UI 入口；是通道自检与心跳的基础 |
| 方法表 | `api/gateway.py:116` `dispatch()` |
| 编解码 | `api/frames.py:56` / `:97` |
| 由谁守住 | `test_gateway.py::test_echo_roundtrip_returns_data_and_seq`、`test_frames.py`（18 例） |

### C8 · 连接认证（一次性 ticket）

| 项 | 位置 |
|---|---|
| 签发 | api/gateway.py:53 `TicketRegistry.issue()`（在 `app.index()` 里调用） |
| 消费 | `api/gateway.py:62` `consume()` |
| 页面注入 | `web/assets.py` 的 `<meta name="postel-handshake">` |
| 拒绝握手 | `api/gateway.py:151` → HTTP 403 |
| 由谁守住 | `test_gateway.py::test_ticket_is_single_use`、`::test_missing_ticket_is_rejected_with_403`、`::test_forged_ticket_is_rejected_with_403` |

### C9 · 内核告知自己的位置（`/api/where`）

| 项 | 位置 |
|---|---|
| 路由 | `api/app.py:76` |
| 数据来源 | `create_app(ws_path, host=, port=)` 注入的**本进程真实绑定** |
| 由谁守住 | `test_gateway_inprocess.py::test_where_reports_this_kernels_real_binding`（删掉 server.json 后仍答同样值） |

### C10 · 凭据不进日志 / 不进页面

| 项 | 位置 |
|---|---|
| WS 访问日志脱敏 | `api/server.py:110` `_WsQueryRedactor` |
| 内核 logger 挂载 | `api/server.py:128` `log_config()` |
| token 不进 server.json | `api/server.py:213-216`（只放 `tokenFingerprint`） |
| 由谁守住 | `test_gateway.py::test_ticket_never_reaches_kernel_logs`、`::test_page_does_not_carry_the_long_term_token`、`test_server_boot.py::test_server_json_does_not_carry_secrets_or_token` |

### C11 · 绑定本地仓库（`repo.bind`）—— S3

| 项 | 位置 |
|---|---|
| 用户路径 | 左栏输入框粘绝对路径 → 点「绑定」→ 左栏出现「名称 / 默认分支」 |
| 前端入口 | `web/assets.py:1251` `submitBind()`（真 `<form>`，回车也可） |
| 方法表 | `api/gateway.py:116` `dispatch()` 的 `repo.bind` 分支 |
| 业务编排 | api/repository.py:205 `RepositoryService.bind()` |
| 路径校验 | 相对 → `BAD_CWD`；`resolve()`（`api/repository.py` 的 `bind`） |
| **`.git` 判定** | api/repository.py:123 `_inside_git_dir(root, target)`（T29/T31：看**相对仓库根**的那一段；粘 `.git` / `.git/objects` → `NOT_A_REPO`，但父目录叫 `.git` 的合法仓库照绑） |
| **子目录校正** | `api/repository.py:_bindable_root()`（D2，见 06 T25） |
| 探测 | gitops/repo.py:274 `origin_url()`、`:268` `default_branch()` |
| id | `domain/models.py:124` `repo_id(path)`——**只看路径**，写后永不更新（S3.5 D1，U9 已结案） |
| origin 识别 | `domain/models.py:111` `origin_hash()` → `repos.origin_hash`（D2；帧层**不暴露**） |
| 落库 | `storage/db.py:179` `RepoStore.upsert()`（同路径不产第二行；`repo_id` 只 INSERT 时写） |
| 由谁守住 | `tests/test_bind.py`：**52 个 `def test_` → 59 个用例**（参数化展开），真实 git + 真 SQLite；S3.5 加的稳定性/迁移用例见 `storage-db.md`；端到端 `test_websocket_bind_then_list_returns_the_repo` |
| ⚠️ UI 行为 | pytest 里只有文本断言；验收 1–4 由 `node tools/acceptance/s3_bind.mjs` 在真实 Chrome 里跑（T9） |

### C12 · 左栏仓库列表（`repo.list`）

| 项 | 位置 |
|---|---|
| 用户路径 | 页面加载 / 重连成功后自动拉一次 |
| 前端 | `web/assets.py:637` `loadRepos()` → `:578` `renderRepos()` |
| 方法表 | `api/gateway.py:122` |
| 业务 | api/repository.py:201 `RepositoryService.list()` |
| 读库 | `storage/db.py:161` `RepoStore.list()`（`ORDER BY added_at, repo_id`） |
| 由谁守住 | `test_bind.py::test_list_returns_array_payload_with_camel_case_keys`、`::test_data_survives_reopening_the_store` |

### C13 · 「没连上」与「没有仓库」可区分（D1）

| 项 | 位置 |
|---|---|
| 用户路径 | 内核不在 / 断连时，左栏仓库区显示「已断开：内核没有连着，仓库列表未知」，**不显示**空态 |
| 状态源 | `web/assets.py:538` `setRepoState()` → `#repos[data-state]` |
| 显隐规则 | `web/assets.py:124-318` 的 CSS（**不是** JS 逐个开关，见 06 T8/T27） |
| 由谁守住 | `tools/acceptance/s3_bind.mjs` 的 `CHECK D1 …` 四行（CDP 断网 → 读 DOM → 恢复 → 点重连，见 06 T27） |

### C14 · 点仓库 → 看到分支列表（`repo.branches`）—— S4

| 项 | 位置 |
|---|---|
| 用户路径 | 点左栏仓库行（鼠标，或聚焦后 `Enter`/`Space`）→ 主区分支面板列出该仓库的**本地**分支：当前分支带「当前」徽章、每个 8 位 sha、有 upstream 才显示 |
| 前端入口 | `web/assets.py:1231` `selectRepo()` → `:731` `loadBranches()` → `:687` `renderBranches()`；断连/切换的收口是 `:661` `resetBranches()` |
| DOM 契约 | `#branches[data-state]`（idle/loading/ready/error）+ `#branches-title` / `#branch-list` / `#branch-empty` / `#branches-error`（D6） |
| 方法表 | `api/gateway.py:116` `dispatch()` 的 `repo.branches` 分支 |
| 业务编排 | api/repository.py:269 `RepositoryService.branches()`；`repoId` → 允许根走 `:294` `_require_repo()`（与 `get()` / `diff()` 共用） |
| 探测 | gitops/repo.py:307 `list_branches()`：一次 `for-each-ref refs/heads/`、`%00` 分隔四字段（D2/D3） |
| 三态 | `list`（空 = 空仓库，**成功**）/ `None`（不是仓库/路径没了 → `REPO_NOT_BOUND`）/ 抛 `GitUnavailableError`·`GitTimeoutError`（D4/D5） |
| 载荷 | `{name, sha(8 位), isCurrent, upstream}` 四字段（D1）；截 8 位发生在 `api/repository.py` 边界 |
| 由谁守住 | `tests/test_branches.py` **23 个用例**（真 git + 真 SQLite + 真 WS 帧，零 mock 解析）：D7 第 1–10 行 + 页面契约 5 条 |
| ⚠️ UI 行为 | pytest 里只有文本断言；验收 1–5 与 D6 由 `node tools/acceptance/s4_branches.mjs` 在真实 Chrome 里跑（T9） |

### C15 · 点分支 → 看到 diff（`repo.diff`）—— S5

| 项 | 位置 |
|---|---|
| 用户路径 | 点主区分支行（鼠标，或聚焦后 `Enter`/`Space`）→ 该分支与**默认分支**的差异：`N 个文件 · +X −Y` 统计 + 逐行着色的 patch；也可在 `#diff` 面板手填两个 ref 对比 |
| 前端入口 | `web/assets.py` 的 `#diff` 面板：`selectBranch()` / `submitDiff()` / `requestDiff()`；断连/切仓库收口是 `resetDiff()` |
| DOM 契约 | `#diff[data-state]`（idle/loading/ready/error）+ `#diff-title` / `#diff-base` / `#diff-head` / `#diff-stat` / `#diff-patch` / `#diff-truncated` / `#diff-error`（S5 D9） |
| 方法表 | `api/gateway.py:116` `dispatch()` 的 `repo.diff` 分支 |
| 业务编排 | `api/repository.py:361` `RepositoryService.diff()`：形状 → 路径存活 → 解析 ref → stat/patch → 截断 + camelCase（D7 的顺序就是契约） |
| 探测（L0） | gitops/repo.py:363 `resolve_commit()`（唯一把用户 ref 送进 git 的地方，D2）、`:383` `diff_stat()`、`:424` `diff_patch()` |
| 三点语义 | `base...head`（共同祖先 → head）：只报 head 改了什么，不把 base 在分支点之后的提交算进来（D1） |
| 确定性 | `DIFF_FLAGS`（`--no-color` / `--no-ext-diff` / `--no-textconv` / `--no-renames` / `--unified=3`）+ `-c core.quotePath=false`：用户配置改不动输出（D4，T34） |
| 截断 | `maxChars ∈ [1000, 200000]`、缺省 100k、**越界报错不夹紧**；`truncated` 只影响 patch，`stat` 描述完整差异（D6） |
| 错误码 | `INVALID_PARAMS` / `REPO_NOT_BOUND`（含路径没了，先于 ref 判）/ `BAD_REF`（解析不到、无共同祖先）/ `ADAPTER_UNAVAILABLE` / `INTERNAL` / `NOT_SUPPORTED`（非 UTF-8 ref 名）—— 07 §1.3 既有码，**一个不新增**（D7） |
| 由谁守住 | `tests/test_diff.py` **47 个用例**（真 git + 真 SQLite + 真 WS 帧）：D10 第 1–21 行 + 页面契约 7 条；反向注入 5 条在 `tests/test_reverse_checks.py` |
| ⚠️ UI 行为 | pytest 里只有文本断言；D11 的 10 条由 `node tools/acceptance/s5_diff.mjs` 在真实 Chrome 里跑（T9） |

### C16 · 左栏看到 Bot 与它的 charter（`bot.list` / `memory.charter.get`）—— S6

| 项 | 位置 |
|---|---|
| 用户路径 | 左栏「Bots」区在选中仓库后出现 `builder`（名字 / L1 徽标 / 职责摘要）→ 点它（鼠标或 `Enter`）→ 主区「Charter」面板显示 charter 全文与记忆目录**绝对路径** |
| 前端入口 | `web/assets.py` 的 `#bots` / `#charter` 面板：`loadBots()` / `selectBot()` / `loadCharter()`；断连 / 切仓库的收口是 `resetBots()` / `resetCharter()` |
| DOM 契约 | `#bots[data-state]` / `#charter[data-state]` + `li[data-bot-id]` / `.bot-name` / `.bot-permission` / `.bot-charter-summary` / `#charter-memory` / `#charter-content`（S6 D8） |
| 方法表 | `api/gateway.py:130` `bot.list` / `api/gateway.py:132` `memory.charter.get` |
| 业务编排 | `api/bots.py:86` `BotService`：惰性播种（D2）、摘要五条与 `charterRev`（D5）、错误码（D6） |
| 数据 | `storage/db.py:284` `BotStore`（v3 的 `bots` 表）；charter **正文在文件**：`storage/paths.py:253` `ensure_memory_space` / `storage/paths.py:272` `read_charter` |
| 安全 | 记忆目录 `<状态根>/bots/<repo_id>/<name>/`：目录 `0700`、`charter.md` `0600`（D3 / T38）；名字白名单 + resolve 越界断言 |
| 错误码 | `INVALID_PARAMS`（repoId / botId 形状错、botId 查不到）/ `REPO_NOT_BOUND`（repoId 查不到）/ `INTERNAL`（记忆目录读写失败）——07 §1.3 既有码，**一个不新增** |
| 由谁守住 | `tests/test_bot_charter.py` **26 个用例**（真 SQLite + 真文件 + 真 WS 帧）：D11 第 1–14 行；反向注入 5 条在 `tests/test_reverse_checks.py` |
| M1 收口 | `scripts/accept_m1.py` 一条命令跑通 S1–S6 + 家具（真内核 / 真 git / 真 WS），由 `tests/test_accept_m1.py` 子进程验收 |
| ⚠️ UI 行为 | pytest 里只有文本断言；D8 的 DOM 行为由 `node tools/acceptance/s6_bots.mjs` 在真实 Chrome 里跑（T9） |

### C17 · 分配 / 回收工作区（`worktree.acquire` / `list` / `release`）—— S7

| 项 | 位置 |
|---|---|
| 用户路径 | 选中仓库 → 在基础分支输入框（预填默认分支）点「分配工作区」→ 页面显示**路径 / 分支 / Bot**，磁盘上真有那个 worktree → 点「回收工作区」→ 目录消失、`git worktree list` 里不再出现（分支保留） |
| 前端入口 | `web/assets.py` 的 `#workspace` 卡片：`loadWorkspace()` / `submitWorkspace()` / `releaseWorkspace()`；断连 / 切仓库的收口是 `resetWorkspace()` |
| DOM 契约 | `#workspace[data-state]` + `#workspace-form` / `#workspace-base` / `#workspace-acquire` / `#workspace-bot` / `#workspace-path` / `#workspace-branch` / `#workspace-release` / `#workspace-empty` / `#workspace-error`（S7 D9） |
| 方法表 | `api/gateway.py:116` 的 `worktree.acquire` / `worktree.list` / `worktree.release` 三个分支 |
| 业务编排 | `api/worktrees.py:78` `WorktreeService`：复用/修复/创建状态机（D7）、Bot 归属校验、错误码（D6）——**不落库**（D4） |
| 写面（L0） | `gitops/worktrees.py:136` `add_worktree` / `:163` `remove_worktree` / `:172` `prune_worktrees`——**S7 起内核唯一会写用户仓库的地方**，允许清单见 S7 D5 |
| 路径 | `storage/paths.py:295` `worktree_dir()` / `:311` `ensure_worktree_parent()`：`<状态根>/worktrees/<repo_id>/<bot>/`，白名单 + resolve 越界断言（D2） |
| 错误码 | `INVALID_PARAMS` / `REPO_NOT_BOUND` / `BAD_BASE_REF`（既有）+ **`WORKTREE_CONFLICT`（S7 新增）** / `ADAPTER_UNAVAILABLE` / `INTERNAL` / `NOT_SUPPORTED`（07 §1.3） |
| 安全 | 工作区只在状态根下（绝不在用户仓库工作目录里）；**不 `--force`**、不删分支、不 reset；脏工作区回收被拒且文件原样保留（D5） |
| 由谁守住 | `tests/test_worktree.py` **35 个用例**（真 git + 真 SQLite + 真目录）；反向注入 4 条在 `tests/test_reverse_checks.py`（S7 #1–#4） |
| ⚠️ UI 行为 | pytest 里只有文本断言；D9 的 DOM 行为由 `node tools/acceptance/s7_worktree.mjs` 在真实 Chrome 里跑（T9） |

---

## 2. 常见任务 → 文件

### 加一个新方法（WS 帧）

```
① api/frames.py            不一定要动（帧形状已够用）
② api/gateway.py:116       dispatch() 加一个分支（**别把业务写进来**，转手给 handler 模块）
③ docs/design/07-api-contract.md §2   登记方法签名
④ tests/test_gateway.py    加一个往返用例 + 一个反例（坏参数 → INVALID_PARAMS）
⑤ 若错误码是新的          frames.py 常量 + 07 §1.3 表（**S3 D6：不许新造码**，先改表）
⑥ 把该方法的 matcher 从 tests/test_gateway.py::test_unknown_method_is_unknown_method_not_subscribe
                           的反例清单里**拿掉**（S3 实现 repo.bind 时踩过；S6 的 bot.list 同）
⑦ 若属于新的一族（如 S6 的 bot.* / memory.*）：新开 L3 handler 模块 + LAYERS 登记 +
                           02-layers 层表 + Providers 注入（照 api/bots.py 抄）
```

**注意**：方法多到 6~8 个时 `dispatch()` 的 `if` 链该改注册表，但现在别提前抽象。

### 加一个 HTTP 路由

```
① api/app.py              加 @app.get(...)
② 决定要不要认证          本地一律不要（05 §1.3），但别回显敏感值
③ 返回形状                裸对象，**不套 {data,seq}**
④ 若前端要用              改 web/assets.py + 在真浏览器里验证
```

### 加一个前端状态或按钮

```
① web/assets.py           DOM + CSS + JS
② 若新增常量              放 Python 侧（可被测试断言），用 %%NAME%% 注入
③ tests/test_gateway.py   文本断言（只能证明意图）
④ 必须                   在真实浏览器里跑一遍并记录结果
```

### 加一个状态文件

```
① storage/paths.py        路径函数 + 写/读/删 + 模块级文件名常量（唯一事实源）
② 05-security §4.1        权限表登记
③ tests/                  权限断言（0600）
④ api/server.py           在 _run 的 finally 里清理（用户数据不清理，见 state.db）
⑤ docs/codemap/01-overview.md §4 的状态根表
```

### 加一张表 / 加一列（S3 起）

```
① storage/db.py           _MIGRATIONS **append** 一段 DDL + SCHEMA_VERSION +1（不改历史条目）
② _row_to_repo / 新映射    类型收敛只在这里，别让 sqlite3.Row 漏出去
③ 06-data-model §2 的表   snake_case 落盘、camelCase 帧层，转换只在 api/* 的边界
④ 写入语义                先写 Clear 的 SQL（UPDATE / INSERT 三步），别用 ON CONFLICT 猜（T26）
```

### 加一个 git 探测

```
① gitops/repo.py          用 run_git()（字面数组/无 shell/超时/显式 root=）
② **不要**自己 subprocess.run —— 允许根（D4）只有一处实现
③ 用 `rev-parse --show-toplevel` 判"是不是仓库"？**别**，读 06 T25
④ tests/test_bind.py      真实 git init 的临时仓库，不 mock
```

### 改启动顺序 / 信号处理

```
① 先读 docs/codemap/03-modules/server.md §「顺序是有讲究的」
② 再读 docs/design/issues/S1-kernel-visible.md（那个竞态的故事）
③ 跑 test_server_boot.py 全部 16 个
```

---

## 3. 尚未实现（S3+）

| Issue | 用户看到什么 | 会新增的东西 | 影响本目录 |
|---|---|---|---|
| ~~S3~~ | **已完成（复审通过）**：填仓库路径 → 左栏出现仓库 | `gitops/`、`storage/db.py`、`domain/`、`api/repository.py`、`tools/acceptance/` | 新增 4 页模块页；**L0 第一次碰子进程与 SQLite**；逼出 T25/T26/T27/T29/T30 |
| S3 遗留 | `repo.unbind` / `repo.list` 之外的 `REPO_NOT_BOUND` 帧方法入口 | — | 见 06 U9（`repo_id` 已稳定）、U10（store 不关）、U14（移动仓库产生两行） |
| ~~S4~~ | **已完成**：点仓库行 → 主区分支面板列出本地分支（当前徽章 / 8 位 sha / upstream） | `gitops.BranchInfo` + `list_branches`；`api/repository.branches`；前端 `#branches` | 新增 C14；`for-each-ref` 的 ref 解析首次出现，逼出 **T33** |
| ~~S5~~ | **已完成**：点分支 → 统计 + 着色 patch（截断不静默丢、非法 ref 可读、键盘可达） | `gitops.resolve_commit`/`diff_stat`/`diff_patch`；`api/repository.diff`；前端 `#diff` | 新增 C15；**第一次把用户 ref 送进 git**，裁决 U15（T34/T35/T36） |
| ~~S6~~ | **已完成**：左栏 Bots 区 + 主区只读 charter 面板（M1 一条命令复现 S1–S6） | `api/bots.py`、`bots` 表（v3）、paths 的记忆空间；前端 `#bots`/`#charter`；`scripts/accept_m1.py` | 新增 C16；逼出 **T38**（umask 权限）与 **U17**（`bot.list` 惰性播种 / `charter_rev` 未维护） |
| S7 | worktree 路径 | `git/worktree` | |
| S8 | 创建 Bot | `api/bots.py` 加 `create`；`bots` 表已在 v3 存在；预置播种改由显式动作负责（U17 到期） | 01-overview 已是"单进程 + 一个库"，再改一张表图；`charter_rev` 的写路径要到 S22 |
| S9 | 检测 Agent | `agent/`（ACP 握手） | **第二个进程模型出现，01-overview 重画** |
| S10 | 派活跑起来 | 任务状态机、流式输出 | `dispatch()` 会明显变长 |
| S11 | 收件箱出现候选 | diff → candidate | |
| S12 | 收件箱列表 | 前端大幅扩张 | `assets.py` 会超 400 行 → **考虑拆文件** |
| S13 | 批准/拒绝 | 审核状态机 | |
| S14 | M2 验收 | | |
| S15 | 重启不丢 | 持久化恢复 | |
| S16 | 越权被拦 | `permission/`（纯函数） | **新增 L1 模块，05 §2 的核心** |
| S17 | 上下文可见 | 记忆注入 | |
| S18 | 空态/错误态 | 前端文案体系 | |
| S19 | doctor | 自检命令 | `__init__.py` 加子命令 |
| S20 | 发版 | 版本号同步 | 见 [entry.md 版本号两个来源](03-modules/entry.md) |

> **规则**：每个 Issue 完成时，本目录必须同步更新（见 [README §维护规则](README.md#维护规则合并前的硬要求)）。
