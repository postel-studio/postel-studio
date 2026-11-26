# 05 · 改动影响矩阵

> 上游：[README.md](README.md) · 相关：[06-pitfalls](06-pitfalls.md)

**用途**：改 X 之前，一眼看到还要连带检查什么。**这张表比代码本身更容易过时，请随改随更。**

---

## 改这里 → 波及这些

| 我改的 | 必须连带检查 | 会因为不检查而坏掉的东西 |
|---|---|---|
| `frames.py` 的 `MAX_FRAME_BYTES` | `02b §5`、`05 §3.1`、`test_gateway.py::test_oversized_frame_closes_the_connection` | 协议文档与实现对不上 |
| 帧错误码常量 | `07-api-contract §1.3` 表 | 客户端按码分支会失配 |
| `dispatch()` 的方法表 | `07-api-contract §2`、`test_gateway.py::test_unknown_method_*` | 未登记方法 / 反例失效 |
| `TicketRegistry` 的 TTL 或语义 | `05-security §1.1.1`、`test_ticket_is_single_use` | 安全声明与实现不符 |
| `gateway_path()` 的派生方式 | `test_ws_path_survives_restart_and_differs_per_state_root` | 旧页面跨重启失效（D7 的初衷） |
| `create_app()` 的签名 | `api/server.py:_run` 的调用、`test_gateway_inprocess.py` | 启动即崩 |
| `app.py` 的 `health()` 字段 | `07-api-contract §2.1`、`test_health_roundtrip_matches_http_health` | HTTP 与 WS 两处答案不一致 |
| `app.py` 的 `API_VERSION` | `07-api-contract §5`（破坏性才改） | 客户端版本门 |
| `server.py` 启动顺序 ①~⑤ | `test_server_boot.py` 全部 16 个；`S1` issue 的竞态故事 | **残留文件、收场被跳过** |
| `server.py` 的信号处理 | `_BUILDING`/`_STOP` 的逻辑、两个信号用例 | 建设期 SIGTERM 会绕过 `finally` |
| `server.py` 的 `log_config()` | `test_server_cleans_up_connection_after_client_disconnect`（读日志） | 日志静默 → 那条验收测不到 |
| `server.py` 的 `_WsQueryRedactor` | `test_ticket_never_reaches_kernel_logs` | **ticket 进日志，违反 05 §4.1** |
| `paths.py` 的 `state_root()` | `tests/conftest.py` 的隔离、全部集成测试 | **测试开始写真实 `~/.postel`** |
| `paths.py` 的 `atomic_write()` | 全部写路径、0600 权限断言 | 读到半截文件 |
| `paths.py` 的 `acquire_lock()` 语义 | `test_refuses_...`、`test_overwrites_stale...`、`test_garbage...` | 单实例保护失效 / 被坏锁锁死 |
| `server.json` 字段 | `02b §2.1` 字段表、`01-overview §4`、`/api/where`（**不读它**） | 客户端发现逻辑 |
| `assets.py` 的 `HEARTBEAT_INTERVAL_MS` / `RECONNECT_DELAY_MS` | `test_frontend_disconnect_budget_constants` | **「≤3 秒变红」承诺破灭** |
| `assets.py` 的占位符 | `REQUIRED_PARAMS` + `render_index` + `app.index()` | 页面渲染出 `%%XXX%%` 原文 或 断言失败 |
| `assets.py` 的 `setState()` | 三态 DOM、`stale` toggle、按钮显示 | 状态机出现第二个出口 → 只加不删 |
| `__init__.py` 的子命令 | `test_smoke.py`、`README.md` 用法段 | 用户文档与 CLI 不符 |
| 版本号 | `pyproject.toml:version` **和** `__init__.py:__version__` | 发版时两处不一致 |
| 新增依赖 | `pyproject.toml` + `uv.lock` + **CI 的 `uv sync --frozen`** | CI 装不上 |
| `scripts/check.sh` 的门禁项 | `CONTRIBUTING.md` 门禁表、`.github/workflows/ci.yml` | 本地与 CI 分叉 |
| `scripts/gate-codemap.sh` 的判定范围 | 它自己的 `tests/test_gate_codemap.py`、CONTRIBUTING 门禁表 | 门禁悄悄放行（见 06 §门禁） |

---

## 安全红线（改动涉及以下任一，必须逐条过）

来源：[05-security §7](../design/05-security.md) 验收清单 + 本项目的教训。

| # | 检查 | 怎么验 |
|---|---|---|
| 1 | 新路径处理是否 `resolve()` 并在允许根内？ | 05 §3.2 |
| 2 | 新子进程调用是否**列表形式、无 shell**？ | 05 §3.3；禁止 `shell=True` |
| 3 | 新日志是否可能打印凭据/敏感绝对路径？ | 05 §4.1；ticket 的教训见 06 §安全 |
| 4 | 新增权限判定是否在执行**之前**？ | 05 §2.1；哨兵文件测试（S16） |
| 5 | 凭据是否可能进入帧 / UI / 错误消息 / 提交历史？ | 05 §4.1 |
| 6 | 是否新增了**不需要认证**的 HTTP 路由？ | 本地无认证是**有意的**（05 §1.3），但确认不回显敏感值 |
| 7 | 是否把绑定面从 `127.0.0.1` 放开？ | **禁止**，05 §1.1 |
| 8 | 是否把 ticket / 长期令牌塞进 URL 或页面源码？ | **禁止**；05 §1.1.1 |

**这 8 条不过，PR 不该合并**——即使测试全绿。

---

## 门禁

唯一入口：`bash scripts/check.sh`（本地与 CI 同一份，不允许分叉）

| 门禁 | 工具 | 失败即阻断 | 覆盖什么 |
|---|---|---|---|
| lint | `ruff check .` | 是 | E/W/F/I/N/UP/B/SIM/PTH/RUF |
| format | `ruff format --check .` | 是 | |
| types | `mypy`（strict，`src`+`tests`） | 是 | |
| test | `pytest --cov` | 是 | 覆盖率下限 **60%**（S2 时实际 76%） |
| codemap | `scripts/gate-codemap.sh` | 是 | `src/`+`tests/` 变了，`docs/codemap/` 也必须变 |
| secrets | 正则 + 与 `.env` 逐值比对 | 是 | |

另有 `bash scripts/check.sh build`（`uv build` + 装进干净虚拟环境跑 `postel-studio version`）：
**不在 `all` 里**（慢），CI 与发版跑，本地发版前手动跑。

`pre-commit` 钩子只跑快的四项（lint / format / codemap / secrets）。**完整门禁必须手动跑。**

钩子本身受版本管理：`scripts/hooks/pre-commit` + `scripts/install-hooks.sh`
（`.git/hooks` 不进 git，换机器/重新 clone 后要重装一次）。

配置全部在 `pyproject.toml`。CI 用 `uv sync --frozen` 保证可复现。

### codemap 门禁怎么判

`src/` 或 `tests/` 有改动、而 `docs/codemap/` 零改动 → 失败。
它取**并集**：相对基准分支的提交 + 暂存区 + 工作区。只看 `main...HEAD` 会漏掉
还没 commit 的改动，而 pre-commit 时改动恰好还在暂存区——这个漏报修过一次，见 06 §门禁。

基准分支缺省取 `main`，依次回退 `origin/main`；都找不到时**提示后仍比对未提交改动**，
不在 git 仓库里时**跳过**（导出 tarball 场景不能误报）。
CI 里 `origin/main` 存在的前提是 `fetch-depth: 0`——浅克隆下它会退化成跳过。

### 门禁的已知盲区（**重要**）

| 盲区 | 后果 |
|---|---|
| **前端 DOM 行为无自动化测试** | 状态灯是否真的变红、按钮是否真的出现，只有人工验证 |
| `opencode` / `dsh` / `pi` 相关集成测试是 skip-gated | CI 绿 ≠ 真实 Agent 路径可用（S9/S10 起会出现） |
| 层依赖检查（[02-layers §4](02-layers.md#4-可执行检查)）是**文档片段**，未进门禁 | 有人写出向上依赖时门禁不会拦 |
| 覆盖率 60% 是很低的下限 | 新模块可以「测一点点就过」——**靠 review 而非门禁守** |
| codemap 门禁只看「动没动」，不看「写得对不对」 | 在 `docs/codemap/` 加一行错别字也能过门禁；**内容质量仍靠 review** |

---

## 提交前自查（30 秒版）

```bash
git diff --stat                      # 触及文件数与行数（预算 ≤12 文件 / ≤400 行）
git diff main -- tests/test_server_boot.py tests/conftest.py   # 不该有改动
bash scripts/check.sh                # 全绿
```

外加：

- [ ] 安全问题过了上面 8 条
- [ ] 新增/改动的模块在 `docs/codemap/` 有对应条目
- [ ] 没有把「已实测」写成「应该是」——**没验证过的就说没验证过**
- [ ] 提交信息正文说了**为什么**，不只说**改了什么**