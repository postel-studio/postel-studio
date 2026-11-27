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
| `gitops/repo.py:run_git()` | `GIT_TERMINAL_PROMPT=0` / 超时 / `shell=False`、`test_bind.py::test_bind_uses_git_in_a_way_that_cannot_prompt` | **05 §3.3 违规 / 网关被 git 挂住** |
| `gitops/repo.py` 的 `root` 参数 | 变成可选 = 放弃 D4 的允许根 | 子进程会跑在 cwd 上 |
| `gitops/repo.py:run_git` 的 `decode_errors` 缺省值 | 06 U15、S5 D3.2；`tests/test_diff.py` 的 D10 第 19/20 行 | 非 UTF-8 refname 又回 `INTERNAL`（U15 复发），或 mangled 名字被回传 git |
| `api/bots.py` 的 `create` **判序**（repoId → 名字 → 权限 → agentKind → 重名 → 写盘） | S8 D6 + `test_create_rejects_illegal_names_and_writes_nothing`、`test_list_on_a_repo_with_no_bots_writes_nothing` | 被拒绝的请求开始留下行或目录（"什么都没创建"变成假话） |
| `Permission` 的四个成员 / `UNASSIGNABLE_PERMISSIONS` | S8 D2、`test_permission_parse_table`、`test_create_rejects_l3_and_l4`、反向注入 S8 #1 | 页面出现第五个选项，或 `L3` 被悄悄接受（**产品红线**：05 §2.3） |
| `BOT_PERMISSION_DEFAULT` | 05 §2.2、`test_create_defaults_to_l1`、反向注入 S8 #3、页面下拉的 `selected` | 新建 Bot 的默认级别变了却不报错 |
| `bot.list` 的**纯读**性质 | S8 D7 / U17、`test_list_on_a_repo_with_no_bots_writes_nothing`、反向注入 S8 #4 | 一个 GET 又开始写库（并行客户端撞 `UNIQUE`、`doctor` 误判） |
| `ALREADY_EXISTS` | 07 §1.3 表 + `botCreateErrorText` + `test_create_duplicate_name_is_already_exists` | 撞名被报成「名字不合法」——页面撒谎 |
| 页面两个权限下拉的选项 | S8 D8、`test_page_permission_select_lists_exactly_four_levels`、`tools/acceptance/s8_create_bot.mjs` | 出现 `L3`/`L4`（哪怕灰掉），或默认值不再是 L1 |
| `bot-create-form` 的 Escape 绑定位置 | 06 T41、`test_page_js_wires_the_form_without_inner_html` | 绑回 `<form>` 就会被 `disabled` 抢走焦点，Esc 静默失效 |
| `gitops/worktrees.py` 的**写面清单**（`add` / `remove` / `prune`） | S7 D5 的允许清单、`api/worktrees.py` 的三个方法、`tests/test_worktree.py` 全部用例 | **内核开始删/改用户仓库里的东西**——S7 之前它是只读的 |
| `remove_worktree` 加 `--force` | S7 D5 的红线；反向注入 S7 #3、`test_release_dirty_worktree_is_conflict_and_keeps_files` | 未提交的工作被静默删掉 |
| `add_worktree` 的两个形态（`-b` vs 检出已有分支） | 06 T40（传 sha 会变成游离 HEAD）；`test_acquire_after_release_keeps_the_workspace_branch_tip` | 分支上的提交"看不见"，`worktree list` 的 `branch` 也消失 |
| `worktree` 载荷的七个字段 | 07 §2.8 / §4.9、`api/worktrees.py:_payload`、页面 `#workspace-*` | 客户端字段错位；`list` 的条目**不该有** `reused` |
| `WORKTREE_CONFLICT` 的触发条件 | 07 §1.3 的码表 + `ERROR_TEXT` + `tests/test_worktree.py` | 把"正常用户处境"报成 `INTERNAL`（S4 D5 的同一课） |
| `storage/paths.worktree_dir` 的白名单 / 越界断言 | S7 D2；反向注入 S7 #1 | 用户可控的 bot 名进了路径（`..` 恰好 resolve 回根） |
| `gitops/repo.py:list_branches` 的 `--format` / 解析 | 06 T33（`%(HEAD)` 空格 + 整段 strip）、S4 D1/D2/D3；`tests/test_branches.py` 全部解析用例 | 当前分支标记判反 / 最后一条记录解析失败 / remote-tracking 混进列表 |
| `api/repository.py:branches` 的错误码或文案 | 07 §1.3（**不许新造码**）、S4 D5；`test_branches.py` 的错误码用例 | 空仓库被报成错误 / 删仓库被报成 INTERNAL / 错误文案回显用户路径 |
| `assets.py` 的 `#diff[data-state]` 状态机 / 行分类顺序 / `#diff-patch` 渲染 | S5 D9（DOM 契约）、`node tools/acceptance/s5_diff.mjs`、`test_diff.py` 的页面契约用例 | `+++` 变绿、截断静默丢、断连留旧 diff、XSS（文件名进 innerHTML） |
| `assets.py` 的 `selectBranch` / `submitDiff` / `diffRequestToken` | S5 D9：点分支填两个输入框、默认分支不发请求、过期回包丢弃 | 点默认分支也发请求 / 快速切换时旧对比覆盖新对比 |
| `assets.py` 的 `ERROR_TEXT`（S5 补 `BAD_REF` / `NOT_SUPPORTED`） | 07 §1.3 的码、`test_page_diff_error_text_*`；真浏览器里看一遍 | 差异面板显示码本身或「绑定失败」 |
| `assets.py` 的 `#branches[data-state]` 状态机 / 行内类名 | S4 D6（DOM 契约）、`node tools/acceptance/s4_branches.mjs`、`test_branches.py` 的页面契约用例 | 断连时留旧数据 / 空仓库显示成错误 / 浏览器验收脚本失效 |
| `assets.py` 左栏仓库行的交互（选中 / 键盘） | S4 D6：`click` 与 `Enter`/`Space` 都要能选；`renderRepos` 重建后恢复 `data-selected`；真实浏览器里验一遍 | 选中样式被 `repo.list` 冲掉 / 键盘用户无法选仓库 |
| `api/repository.py:_bindable_root()` | 06 T25 的回归锁、D2 的验收、`test_plain_directory_inside_another_working_tree_*` | **普通目录被"绑定成功"** 或 **子目录校正失效** |
| `api/repository.py` 的错误文案（`MSG_*`） | 05 §4.2、`test_not_a_repo_message_does_not_echo_any_user_path` | **错误回显用户路径** |
| `api/repository.py` 的错误码 | 07 §1.3 表（**不许新造**，S3 D6） | 客户端按码分支失配 |
| `storage/db.py:RepoStore.upsert()` | 06 T26/T32、验收 4、`test_rebinding_*`、**S3.5 D1：规则 1 不许写 `repo_id`**、`test_each_half_of_u9_alone_*` | 重复绑定出现两行 / **仓库加了 remote 就换身份（U9 复发）** |
| `storage/db.py:_MIGRATIONS` / `SCHEMA_VERSION` / `_COLUMNS` | 06 §7（只增不减、可重入）、升级路径、**真库迁移测试**（v1 手工建库 → migrate → id 未变） | 老用户的库迁移失败 / **历史 `repo_id` 被重写（D3）** |
| `paths.py:STATE_DB_FILE` / `state_db_file()` | `storage/db.py`、`01-overview §4` 的状态根表 | 库落到别处（甚至仓库里） |
| `domain/models.py:normalize_origin` / `repo_id` / `origin_hash` | S3.5 D1/D2、`test_https_and_ssh_origins_share_the_same_repo_id`、**已存在的 repo_id 与 origin_hash** | `repo_id` 一旦依赖 origin 就复发 U9（06 T32）；改 `normalize_origin` 会改 `origin_hash` 但**不该**改 `repo_id` |
| `api/app.py` 里 `RepoStore()` 的构造位置 | 决定 `state.db` 落在哪个状态根；测试靠 `POSTEL_HOME` | 测试写真实 `~/.postel`（06 T18） |
| `gitops/repo.py` 的 `DIFF_FLAGS` / `STAT_FLAGS` / `-c core.quotePath=false` | S5 D4、06 T34（用户配置会改输出）、T36（`--numstat` 不能与 `--unified` 同给）；反向注入 D12 #1/#2；`test_patch_has_no_ansi_*` / `test_patch_ignores_external_diff_driver` | 用户 `color.ui` / `diff.external` / `diff.renames` / `diff.context` 改掉内核输出；`--unified` 混进 numstat 会把 patch 当记录解析 |
| `gitops/repo.py:resolve_commit` 的命令形状（`--verify --end-of-options …^{commit}`） | S5 D2、D3.1；反向注入 D12 #4；`test_bad_refs_are_bad_ref`、`test_ref_*` | tree id 被当 commit 用；`-` 开头的 ref 被 git 当选项；多 rev 喂 `--verify` 直接报错 |
| `api/repository.py:diff` 的错误码 / 顺序 / 文案 | 07 §4.6（**不许新增码**）、S5 D7；`test_diff.py` 的错误码用例 | 仓库被删报成 `BAD_REF`（用户去改 ref）；或码表被悄悄扩 |
| `api/repository.py` 的 `DEFAULT/MIN/MAX_MAX_CHARS` | 07 §4.6（契约值）、S5 D6；`test_max_chars_rejects_*` / `test_truncation_boundary_both_sides` | 截断边界与契约脱节；调用方以为拿到了它要的长度 |
| `api/repository.py:diff` 的 `stat`/`patch` 组装 | S5 D5/D6/D8；`test_stat_exact_numbers`、`test_patch_contains_expected_line_kinds` | 字段名漏 camelCase / `stat` 跟着截断变形（等于撒谎） |
| `gateway.Providers` 的字段（S4 加了 `repo_branches`，S5 加了 `repo_diff`，S6 加了 `bot_list` / `memory_charter_get`） | `app.create_app()`（唯一构造点）+ `test_gateway_inprocess.py` + 07 §2.2/§4.5/§4.7/§4.8 | 启动即崩 / `repo.branches` 回 `UNKNOWN_METHOD` |
| `api/bots.py:BotService` 的播种 / 摘要 / rev / 错误码 | S6 D1–D6、07 §4.7/§4.8、`tests/test_bot_charter.py`、反向注入 D12 #1~#4 | 载荷多/少字段、摘要规则漂移、`botId` 查不到被报成别的码、或读了过期 `charter_rev` |
| `storage/paths.py` 的 Bot 记忆空间（`bot_memory_dir` / `ensure_memory_space` / `read_charter`） | S6 D3、05 §4.1（目录 0700 / 文件 0600）、06 T38、`test_ensure_memory_space_*`、D12 #1~#4 | 权限变松 / 覆盖用户编辑的 charter / 名字穿越写出状态根 |
| `storage/db.py` 的 `bots` 表（v3）/ `BotStore` | 06 §2.2、07 §4.7 的 8 字段、真库 v2→v3 迁移测试 | 老库升级丢 repos 行 / 载荷与列对不上 / 预置播种重复建行 |
| `assets.py` 的 `#bots[data-state]` / `#charter[data-state]` 状态机与行结构 | S6 D8（DOM 契约）、`node tools/acceptance/s6_bots.mjs`、`test_bot_charter.py` 的页面契约用例 | 断连留旧 charter / 切仓库不清空 / 选中的 Bot 与 repo 脱钩 / charter 进 `innerHTML`（XSS） |
| `scripts/accept_m1.py` 的步骤与输出格式 | D10 的契约（`PASS S<n>` / `M1 OK`）、`tests/test_accept_m1.py`、README/CONTRIBUTING 的验收命令 | M1 收口静默失效；改格式会让 `test_accept_m1.py` 红 |
| `scripts/accept_m2.py` 的步骤与输出格式 | S14 的契约（`✓ <step> (<sec>s)` / `M2 OK (10/10 steps)` / `M2 FAILED at <step>` / `SKIP M2:`）、`tests/test_accept_m2.py`、README/CONTRIBUTING 的验收命令 | M2 收口静默失效；改格式会让 `test_accept_m2.py` 红 |
| `assets.py` 的 `#repos[data-state]` 机制 | 06 T8/T27、D1 的验收 | 断连时显示空列表 = 撒谎 |
| `assets.py` 的 `ERROR_TEXT` | 07 §1.3 的码、`error_text_json()`；S4 补了 `REPO_NOT_BOUND`、分支面板另用 `BRANCH_ERROR_FALLBACK` | 错误显示成「绑定失败」或码本身；分支面板答「绑定失败」 |
| `assets.py` 左栏的 DOM 结构 / 数据来源 | **`node tools/acceptance/s3_bind.mjs`**（真实浏览器） | pytest 看不出来（T9） |
| `tests/test_reverse_checks.py` 的注入锚点 | 被改的那段源码一旦变形，锚点就找不到（会红，不是静默）；S5 的 5 条（D12 表）分别钉 `DIFF_FLAGS` 的两个 flag / `run_git` 缺省 `decode_errors` / `resolve_commit` 的 `^{commit}` / diff 行的 `textContent` | 反向验证失效——但**是响的** |

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
| 9 | 新子进程是否**有超时**、环境是否含 `GIT_TERMINAL_PROMPT=0`？ | 05 §3.3 + S3 D5；`test_bind_uses_git_in_a_way_that_cannot_prompt` |
| 10 | 错误 `message` 是否回显了用户输入（路径/分支名）？ | 05 §4.2；`test_not_a_repo_message_does_not_echo_any_user_path` |
| 11 | 新用户数据是否**只**写在状态根？ | S3 禁止事项；`test_bind_writes_nothing_into_the_user_repository` |

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
| docs | `scripts/gate-docs.py` | 是 | 相对链接 / 锚点 / `#L` 行号 / 模块行数表 / 页面 id 一致性（S4.5） |
| layers | `scripts/gate-layers.py` | 是 | 只许向下 import；**新模块必须登记层**（S4.5，结掉 U5） |
| codemap | `scripts/gate-codemap.sh` | 是 | `src/`+`tests/` 变了，`docs/codemap/` 也必须变 |
| secrets | 正则 + 与 `.env` 逐值比对 | 是 | |

另有 `bash scripts/check.sh build`（`uv build` + 装进干净虚拟环境跑 `postel-studio version`）：
**不在 `all` 里**（慢），CI 与发版跑，本地发版前手动跑。

`pre-commit` 钩子只跑快的几项（lint / format / docs / layers / codemap / secrets）。**完整门禁必须手动跑。**

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
| 覆盖率 60% 是很低的下限 | 新模块可以「测一点点就过」——**靠 review 而非门禁守** |
| codemap 门禁只看「动没动」，不看「写得对不对」 | **部分结掉（S4.5）**：链接/锚点/行号/行数表/页面 id 已机械检查；文字内容质量仍靠 review（U8） |
| 文档门禁不判「说的对不对」 | 一段错误的解释（如 S4 的 T33 初版）照样能过门禁；**内容对错只能靠跑它写的复现命令**（[09 §1.1](../design/09-slice-dod.md)） |

---

## 提交前自查（30 秒版）

```bash
git diff --stat -- src/              # 预算**只计 src/**：≤12 文件 / ≤400 行（04 §1.1 口径）
git diff --stat                      # 全量看一眼：tests/docs 不计入上限，但不该有空 diff 的 src 改动
git diff main -- tests/test_server_boot.py tests/conftest.py   # 不该有改动
bash scripts/check.sh                # 全绿
```

外加：

- [ ] 安全问题过了上面 8 条
- [ ] 新增/改动的模块在 `docs/codemap/` 有对应条目
- [ ] 没有把「已实测」写成「应该是」——**没验证过的就说没验证过**
- [ ] 提交信息正文说了**为什么**，不只说**改了什么**