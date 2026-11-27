# 03 · server — `src/postel_studio/api/server.py`

> 层 L2 · 256 行 · 上游：[模块索引](README.md)
> **这是整个仓库最难的一个文件**。它管进程生死，而进程生死充满竞态。

## 职责

**唯一拥有 socket 与信号处理器的模块。** 抢锁 → 选端口 → 装信号 → 写发现文件 →
起 uvicorn（前台常驻）→ 退出即清场。

## 符号表

| 符号 | 行 | 做什么 | 谁在用 |
|---|---|---|---|
| `LOOPBACK` | 54 | `"127.0.0.1"`，硬约束 | `__init__.main()` |
| `GRACE_SECONDS` `5` | 55 | uvicorn 优雅关闭上限 | |
| `BUILD_SETTLE_SECONDS` `0.2` | 56 | 建设期收信号的等待 | |
| `LISTEN_BACKLOG` `2048` | 57 | | |
| `ERR_PORT_INVALID` / `ERR_LISTEN_FAILED` | 59-60 | 稳定码 | 测试 |
| `_BUILDING` / `_STOP` | 66-67 | 两个 `threading.Event`，见下 | |
| `listen(port=0)` | 70 | 绑 loopback 并监听，返回已监听 socket | `serve`、测试 |
| `serve(port=0, force=False)` | 86 | 前台常驻，返回退出码 | CLI |
| `_WsQueryRedactor` | 110 | 日志过滤器，打掉 WS URL 查询串 | `_install_ws_log_redaction` |
| `log_config()` | 128 | uvicorn 日志配置 + 挂内核 logger | 测试、`_configured_log_config` |
| `_install_ws_log_redaction()` | 144 | 给 `uvicorn.error` 挂过滤器 | |
| `_configured_log_config()` | 158 | 合体版 | `_run` |
| `_install_signal_handlers()` | 164 | SIGTERM/SIGINT → 优雅停止 | `_run` |
| `gateway_path(root)` | 187 | 由状态根派生 `/gateway/<sha16>` | `_run` |
| `_run(root, sock)` | 199 | 真正的启动与收场；**S15/U10**：`finally` 里 `app.state.close_stores()` 关闭所有 store | `serve` |

## 关键决策与理由

### 顺序是有讲究的：五步不能换

```
① acquire_lock      ← O_EXCL，先抢锁再谈其它
② listen(port)      ← 内核自己 bind，拿到端口
③ _install_signal_handlers()   ← **必须早于** 写 server.json
④ write_token + write_server_info   ← 客户端「看见文件」= 「内核已经在听」
⑤ uvicorn.run(sockets=[sock])
```

**③ 必须在 ④ 之前**，这是 S1 实测出来的坑：客户端一看见 `server.json` 就可能发信号。
若信号处理器还没装好，Python 走**缺省动作**——`SIGTERM` 直接终止进程，`finally` 根本不执行，
盘上留下 `server.json` + 锁。实测这个窗口约 15~20ms。

### 为什么自己 `bind(0)` 而不是给 uvicorn 传 `port=`

绝不给 uvicorn 传 `port=` 或 `fd=`：
- `port=` 会让 uvicorn 自己 bind，中间存在「取端口 → 监听」的空窗竞态；
- `fd=` 会触发 reload 子进程化，并把它继承到的监听 socket 关掉。

正确做法是**内核自己 bind**，再把同一个 socket 交给 `server.run(sockets=[sock])`。

### `_BUILDING` / `_STOP` 两个 Event 解决的是什么

承接上面 ③④ 之间的窗口期。若在这个窗口收到 `SIGTERM`：

| 做法 | 后果 |
|---|---|
| 处理器直接抛 `KeyboardInterrupt` | 直接跳出 `serve()`，`finally` 被跳过，**盘上留残骸** |
| 处理器只 `_STOP.set()` 不抛 | uvicorn 还没建好，没人收摊 |
| **实际做法**：建设期只记账 | `_await` 后再自己停，收场代码正常执行 |

```python
def _on_stop_signal(signum, frame):
    _STOP.set()
    if _BUILDING.is_set():
        return  # 建设期：只记账
    raise KeyboardInterrupt  # 已交给 uvicorn：走它的优雅关闭
```

`_run` 里对应地：进 uvicorn 前若 `_STOP.is_set()`，先 `sleep(BUILD_SETTLE_SECONDS)`
再 `server.should_exit = True`。

> 这段是 S1 子代理**自己发现**的竞态，我做审核时没能预见到。
> **改这段之前先读一遍 S1 的 issue 与这段注释，否则很容易「简化」掉它。**

### 为什么 `_install_signal_handlers` 检查主线程

```python
if threading.current_thread() is not threading.main_thread():
    return
```

`signal.signal` 只能在主线程调用，否则抛 `ValueError`。
测试里有用例在非主线程起 uvicorn（`test_gateway_inprocess.py`），所以这里要能安全退回缺省行为。

### 日志可见性：uvicorn 的坑（S2 发现）

uvicorn 0.54 的 `LOGGING_CONFIG` **没有 root logger 项**：

```python
>>> list(uvicorn.config.LOGGING_CONFIG)
['version', 'disable_existing_loggers', 'formatters', 'handlers', 'loggers']
>>> list(uvicorn.config.LOGGING_CONFIG['loggers'])
['uvicorn', 'uvicorn.error', 'uvicorn.access']
```

后果：内核自己的 `logging.getLogger("postel_studio.*")` **INFO 级日志全部静默**
（WARNING 靠 `logging.lastResort` 兜底才出得来）。

`log_config()` 把 `postel_studio` 显式挂到 uvicorn 的 `default` handler 上。

**这很重要，因为 S2 有一条验收（断开后服务端清理）就是读这条日志的。**
没有这个修复，那条测试永远「测不到」。

### WS 访问日志的 ticket 泄漏（S2 发现，已修）

uvicorn 的三条 WS 日志（`[accepted]` / `403` / 关闭码）都打
`get_path_with_query_string()`，于是：

```
INFO: 127.0.0.1:51234 - "WebSocket /gateway/xxx?ticket=d9f3b91abf...  [accepted]
```

`access_log=False` **管不着**它——因为它走的是 `uvicorn.error` logger，不是 `uvicorn.access`。
`_WsQueryRedactor` 把 `?` 之后打掉。

**两个实测踩到的坑**：
1. logger 的 `filters` 不能写 `{"()": ...}`，会 `ValueError: Unable to add filter`，
   **让内核直接起不来**；
2. 只能在 `dictConfig` 执行完之后对 logger 对象 `addFilter`。

见 [06-pitfalls §安全](../06-pitfalls.md#安全)。

### `gateway_path()` 为什么是「稳定」而不是「随机」

S2 初版每次启动随机生成 `/gateway/<random>`。审核时实测：
重启后路径变了，**旧页面拿着旧路径连不上**，而浏览器读不到 `server.json` 去发现新值。

改为 `sha256(str(state_root))[:16]`：同一机器同一用户 → 同一值，重启不变。
它**不是凭据**（真正的凭据是页面里的 ticket），稳定只为了让 URL 结构跨重启可复用。

详见 D7 裁决（在 `docs/design/issues/S2-connection-state.md`，该节位于尚未合入 main 的 PR #4 分支上）与
[01-overview §5](../01-overview.md#5-现在还没有的东西别去猜)。

## 修改须知

| 如果你要… | 还必须 |
|---|---|
| 改启动顺序 | 重读本页 §「顺序是有讲究的」；跑 `test_server_boot.py` 全部 |
| 改信号处理 | 至少跑 `test_sigterm_deletes_server_json_and_leaves_no_process`；**别删 `_BUILDING`** |
| 让内核同时监听别的地址 | **禁止**（05 §1.1 硬约束） |
| 加日志 | 确认不会打印 token / ticket / 绝对敏感路径（05 §4.2、§7 清单） |
| 加 `--flag` | `__init__.py` + `test_smoke.py` + README |
| 改 `gateway_path()` | 会打破「重启后旧页面可复用 URL」；`test_ws_path_survives_restart_and_differs_per_state_root` 会失败 |

## 测试

| 测试 | 守什么 |
|---|---|
| `test_serve_writes_server_json_and_serves_health` | 启动后文件齐全、健康检查通 |
| `test_page_shows_version_and_state_root` | 页面内容 |
| `test_server_json_does_not_carry_secrets_or_token` | 05 §4.1 |
| `test_sigterm_deletes_server_json_and_leaves_no_process` | SIGTERM 收场（无残留进程） |
| `test_sigint_deletes_server_json_and_leaves_no_process` | Ctrl+C 走同一条路 |
| `test_stdout_prints_clickable_loopback_url` | 打印可点地址 |
| `test_server_json_mode_is_600_and_state_root_is_700` | 权限 |
| `test_executable_port_refuses_busy_port` | `LISTEN_FAILED` |
| `test_rejects_host_other_than_loopback` | `HOST_NOT_ALLOWED` |
| `test_listen_binds_loopback_and_accepts_kernel_assigned_port` | 只绑 loopback |
| `test_ws_path_survives_restart_and_differs_per_state_root` | D7 稳定 wsPath |
| `test_ticket_never_reaches_kernel_logs` | ticket 不进日志（R2） |

## 已知限制

**SIGKILL 后同端口重启会失败。** 浏览器标签页开着时，内核被强杀会在**服务端口**留下
`TIME_WAIT`，`SO_REUSEADDR` 与 `SO_REUSEPORT` 都救不了（实测存活 >300s）。
SIGTERM（正常停止）下可复用。**这是本文件的已知限制，不是 bug，本刀不修。**

> 完整实测数据（含等待 3s/10s/30s 全部失败的记录）写在
> `docs/design/issues/S2-connection-state.md` 的「补记：SIGKILL 后的同端口重启会被 TIME_WAIT 挡住」一节。
> 该节在 PR #4 的 `docs/s2-api-registration` 分支上，**尚未合入 main**；合入后此处的交叉引用即成立。
> 摘要见 [06-pitfalls T3](../06-pitfalls.md)（已同步记录，本仓库内可查）。