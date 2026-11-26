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
| 渲染页面 | `web/assets.py:242` `render_index()` |
| 路由 | `api/app.py:61` `GET /` |
| 由谁守住 | `test_server_boot.py::test_stdout_prints_clickable_loopback_url`、`::test_page_shows_version_and_state_root` |

### C2 · 单实例保护（不重复启动）

| 项 | 位置 |
|---|---|
| 用户路径 | 已有一个内核时再启动 → 退出码 1 + `KERNEL_ALREADY_RUNNING` |
| 抢锁 | `storage/paths.py:169` `acquire_lock()` |
| 判定存活 | `storage/paths.py:138` `pid_alive()` |
| 由谁守住 | `test_server_boot.py::test_refuses_to_start_when_lock_pid_is_alive`、`::test_overwrites_stale_lock_when_pid_is_dead`、`::test_garbage_lock_is_treated_as_stale` |

### C3 · 优雅退出且不留残骸

| 项 | 位置 |
|---|---|
| 用户路径 | `Ctrl+C` 或 `kill` → 进程退出、三个文件全删 |
| 信号处理器 | `api/server.py:164` `_install_signal_handlers()` |
| 收场 | `api/server.py:245`（`_run` 的 `finally`） |
| 建设期竞态保护 | `api/server.py:60-61` `_BUILDING` / `_STOP` |
| 由谁守住 | `test_server_boot.py::test_sigterm_deletes_server_json_and_leaves_no_process`、`::test_sigint_...` |

### C4 · 页面显示连接状态（三态）

| 项 | 位置 |
|---|---|
| 用户路径 | 右上角状态灯：已连接 / 已断开 / 重连中 |
| DOM | `web/assets.py` 里 `id="status"`、`id="status-text"` |
| 状态切换 | `web/assets.py:112` `setState()` |
| 由谁守住 | `test_gateway.py::test_page_declares_status_lamp_and_three_states`（**文本断言**） |

### C5 · 断连 ≤3 秒可见 + 灰化 + 重连按钮

| 项 | 位置 |
|---|---|
| 心跳判活 | `web/assets.py:157` `startHeartbeat()` |
| 唯一向下出口 | `web/assets.py:129` `fail()` |
| 灰化规则 | `web/assets.py:68-69` `#content.stale` / `::after` |
| 按钮显示 | `setState()` 里 `button.style.display = ...` |
| 由谁守住 | `test_gateway.py::test_server_cleans_up_connection_after_client_disconnect`（**后端侧**）、`::test_page_has_stale_rule_and_toggles_it_on_disconnect`（**文本**） |
| ⚠️ 真实 DOM 行为 | **无自动化测试**，靠人工浏览器验证 |

### C6 · 点重连恢复（端口未变时）

| 项 | 位置 |
|---|---|
| 重连入口 | `web/assets.py:218` `reconnect()` |
| 取新票 | `web/assets.py:168` `freshTicket()` |
| 端口校验 | `web/assets.py:179` `PORT_CHANGED` |
| 建连 | `web/assets.py:194` `connect()` |
| 由谁守住 | `test_gateway.py::test_reconnect_after_disconnect_works_without_restart`（网关侧）、`::test_frontend_reports_port_change_and_does_not_guess`（文本） |
| ⚠️ 边界 | **端口变了无法自动恢复**（D7），页面会明确告知 |

### C7 · 帧往返（`system.echo`）

| 项 | 位置 |
|---|---|
| 用户路径 | 无 UI 入口；是通道自检与心跳的基础 |
| 方法表 | `api/gateway.py:92` `dispatch()` |
| 编解码 | `api/frames.py:56` / `:97` |
| 由谁守住 | `test_gateway.py::test_echo_roundtrip_returns_data_and_seq`、`test_frames.py`（18 例） |

### C8 · 连接认证（一次性 ticket）

| 项 | 位置 |
|---|---|
| 签发 | `api/gateway.py:45` `TicketRegistry.issue()`（在 `app.index()` 里调用） |
| 消费 | `api/gateway.py:54` `consume()` |
| 页面注入 | `web/assets.py` 的 `<meta name="postel-handshake">` |
| 拒绝握手 | `api/gateway.py:101` → HTTP 403 |
| 由谁守住 | `test_gateway.py::test_ticket_is_single_use`、`::test_missing_ticket_is_rejected_with_403`、`::test_forged_ticket_is_rejected_with_403` |

### C9 · 内核告知自己的位置（`/api/where`）

| 项 | 位置 |
|---|---|
| 路由 | `api/app.py:67` |
| 数据来源 | `create_app(ws_path, host=, port=)` 注入的**本进程真实绑定** |
| 由谁守住 | `test_gateway_inprocess.py::test_where_reports_this_kernels_real_binding`（删掉 server.json 后仍答同样值） |

### C10 · 凭据不进日志 / 不进页面

| 项 | 位置 |
|---|---|
| WS 访问日志脱敏 | `api/server.py:110` `_WsQueryRedactor` |
| 内核 logger 挂载 | `api/server.py:128` `log_config()` |
| token 不进 server.json | `api/server.py:213-216`（只放 `tokenFingerprint`） |
| 由谁守住 | `test_gateway.py::test_ticket_never_reaches_kernel_logs`、`::test_page_does_not_carry_the_long_term_token`、`test_server_boot.py::test_server_json_does_not_carry_secrets_or_token` |

---

## 2. 常见任务 → 文件

### 加一个新方法（WS 帧）

```
① api/frames.py            不一定要动（帧形状已够用）
② api/gateway.py:92        dispatch() 加一个分支
③ docs/design/07-api-contract.md §2   登记方法签名
④ tests/test_gateway.py    加一个往返用例 + 一个反例（坏参数 → INVALID_PARAMS）
⑤ 若错误码是新的          frames.py 常量 + 07 §1.3 表
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
① storage/paths.py        路径函数 + 写/读/删
② 05-security §4.1        权限表登记
③ tests/                  权限断言（0600）
④ api/server.py           在 _run 的 finally 里清理
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
| S3 | 填仓库路径 → 左栏出现仓库 | `git/`、路径校验、`PATH_OUTSIDE_ROOT` | 新增 `03-modules/git.md`；**L0 第一次碰子进程** |
| S4 | 分支列表 | 同上 | |
| S5 | diff 视图 | patch 生成 + 截断 | |
| S6 | Bot 与 charter | `memory/`、`store/` | 新增两页 |
| S7 | worktree 路径 | `git/worktree` | |
| S8 | 创建 Bot | SQLite 首次出现 | **新增持久化层，01-overview 的「单进程无库」图失效** |
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