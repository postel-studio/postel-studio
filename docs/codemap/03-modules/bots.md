# 03 · bots — `src/postel_studio/api/bots.py`

> 层 L3 · 181 行 · 上游：[模块索引](README.md) · 相关：[storage-db](storage-db.md) · [paths](paths.md) · [06 §2.2](../../design/06-data-model.md) · [07 §4.7/§4.8](../../design/07-api-contract.md)

## 职责

`bot.list` / `memory.charter.get` 的**业务编排**（S6）。与 `repository` 同一层、同一纪律：
只吃 `dict`、返回 `dict`/`list[dict]`，**不碰 fastapi**；下游异常翻成 07 §1.3 的既有码。

三处裁决在这里落地：**D2 惰性播种**（只在一个地方）、**D5 文件是事实源**（rev + 摘要现场算）、
**D9 绝对路径**（`memoryPath` 不伪装成 `~`）。

## 符号表

| 符号 | 行 | 做什么 | 谁在用 |
|---|---|---|---|
| `INVALID_PARAMS` / `INTERNAL` / `REPO_NOT_BOUND` | 29-31 | 07 §1.3 的既有码（**一个都不新增**） | 错误映射 |
| `MSG_*` 六个文案 | 34-38 | 可读中文，**不含任何用户输入**（05 §4.2） | 错误映射 |
| `PRESET_BOT_NAME` | 41 | `"builder"`：M1 唯一的预置 Bot（没有创建入口） | `_seed`、测试 |
| `DEFAULT_SUMMARY` | 43 | `"尚未填写职责"`（D5 第 5 条：空正文不是空串） | `charter_summary` |
| `SUMMARY_MAX_CHARS` | 44 | `80`（D5 第 4 条：按字符数截断 + `…`） | `charter_summary` |
| `_COMMENT` / `_EMPHASIS` | 50-51 | 摘要规则的正则：整段 HTML 注释 / 行内强调标记 | `charter_summary` |
| `_fail(code, message)` | 54 | 抛 `frames.FrameError`（网关翻成 `ok:false` 帧） | 本模块 |
| `charter_rev(content)` | 59 | D5：`int(sha256(content)[:8], 16)`，随内容变、无状态 | `_split`、`charter_get`、`_seed` |
| `charter_summary(content)` | 64 | D5 的摘要五条（跳过标题/空行/注释 → 首段 → 去强调 → 截断 → 默认文案） | `_split` |
| `BotService` | 86 | 两个帧方法的实现（构造时注入两个 store） | `app.create_app` → `gateway.Providers` |
| `BotService.list(params)` | 97 | `repoId` → Bot 数组；**空则播种**（D2） | `bot.list` |
| `BotService.charter_get(params)` | 105 | `botId` → `{content, rev}`（读文件，缺目录自愈） | `memory.charter.get` |
| `_require_repo` / `_require_bot` | 119/135 | 参数形状 + 存在性 → D6 的错误码 | 两个公开方法 |
| `_seed(repo_id)` | 147 | 建记忆空间 + 落一行 `builder` / L1 / `agentKind=NULL` | `list` |
| `_split(bot)` | 168 | 实体 → **恰好 8 字段**的 camelCase 载荷（D1） | `list` |

## 关键决策与理由

### D2：惰性播种放进 `bot.list`（一个读方法会落一行）

`bot.create` 是 S8，M1 没有创建入口；但 M1 必须能"看到"一个 Bot。所以：`bot.list(repoId)`
发现该 repo **一个 Bot 都没有**时，创建预置 Bot 并 `ensure_memory_space`。

- 为什么不在 `repo.bind`：① 会让 S3 的路径依赖 Bot 逻辑；② **S6 之前绑定的老仓库**只有
  惰性播种才能自动补上（bind 播种会漏）；③ Bot 元数据与仓库路径是否还在无关（06 §2.2）。
- 为什么"已有任何 Bot 就绝不新建"：幂等。连调两次仍是 1 个（D11 #1）。
- **代价（如实记）**：`bot.list` 是读方法却会写一行。M1 的临时形态，U17 到期 S8。

### D5：charter 正文的事实源是**文件**，不是 DB

- `charterRev` 与 `charterSummary` 都**现场**从 `charter.md` 算（`charter_rev` / `charter_summary`），
  镜像 U17 的另一半：DB 的 `charter_rev` 列 M1 **不维护**（写路径是 S22 的 `charter.set`）。
- 用户手改文件后，下一次 `bot.list` 的摘要与 rev **立刻变**——这是"文件是事实源"的可观测后果。
- 摘要是给左栏一行看的：跳过标题/空行/HTML 注释 → 第一个正文段落（多行用空格拼）→
  去掉 `**`/`*`/`_`/`` ` `` → 超过 80 字符截断加 `…` → 没正文回「尚未填写职责」。

### D6：`botId` 查不到 = `INVALID_PARAMS`（不新增码）

07 §1.3 没有更贴的码（`REPO_NOT_BOUND` 专指 `repoId`）。路径穿越企图**不需要单独判格式**：
不合法的 `botId` 在库里必然查不到，查不到就拒绝，**一个文件都不会碰**；名字进路径前的
白名单与越界断言在 `storage/paths.py:bot_memory_dir`（D3 的纵深防御）。

### `_charter()` 对"目录被用户删了"的语义

`bot.list` 只读：文件不存在按**空正文**处理（摘要走默认文案，不编造），也不在这里重建目录。
`memory.charter.get` 才调 `ensure_memory_space`（缺什么建什么、已有 charter 不覆盖），
所以"手删记忆目录后再点开"会自愈出模板骨架。两种语义的差别是有意的。

## 修改须知

| 如果你要… | 还必须 |
|---|---|
| 改载荷字段 | D1 的 8 字段是契约：`07 §4.7` 表 + `tests/test_bot_charter.py`（`set(payload)` 断言）+ 前端 `assets.py` |
| 改摘要规则 | D5 五条 + `test_charter_summary_follows_the_five_rules` + `03-ui-spec §4.3` 的空态文案 |
| 改成"`bot.list` 也 ensure 所有 Bot" | 会让读方法写文件；M1 不做，见 U17 |
| 加 `bot.create` / `charter.set` | 那是 S8 / S22；不要在 S6 的实现里提前长出来 |
| 在这里 import fastapi / web | **禁止**：L3 业务编排不碰框架（层门禁会拦 `web`） |

## 测试

| 测试 | 守什么 |
|---|---|
| `tests/test_bot_charter.py::test_first_bot_list_seeds_preset_builder` | D11 #1 惰性播种 + 幂等 |
| `...::test_bot_list_never_overwrites_an_edited_charter` | D11 #2 幂等不覆盖 |
| `...::test_two_repos_have_isolated_builders_and_charters` | D11 #3 仓库隔离 |
| `...::test_memory_path_is_absolute_outside_the_repo_and_exists` | D11 #4 + D9 |
| `...::test_seeded_bot_permissions_are_0700_and_0600` | D11 #5（真 stat） |
| `...::test_charter_rev_changes_with_content_and_is_deterministic` | D11 #6 |
| `...::test_charter_summary_follows_the_five_rules` | D11 #7（参数化五条） |
| `...::test_bot_payload_has_exactly_eight_fields` | D11 #8 |
| `...::test_bot_error_codes` | D11 #9（D6 的码表，不新增） |
| `...::test_bot_id_traversal_is_rejected_and_creates_nothing` | D11 #10 |
| `...::test_ws_bot_frames_match_service` | D11 #13：真 WS 帧与直调服务层一致 + `seq` 递增 |
| `tests/test_reverse_checks.py` 的 4 条 S6 注入 | 改 D2/D3 的实现会让这些测试红（D12） |
