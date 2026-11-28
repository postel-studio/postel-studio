# 03 · bots — `src/postel_studio/api/bots.py`

> 层 L3 · 267 行 · 上游：[模块索引](README.md) · 相关：[storage-db](storage-db.md) · [paths](paths.md) · [domain](domain.md) · [06 §2.2](../../design/06-data-model.md) · [07 §4.7/§4.8/§4.10](../../design/07-api-contract.md) · [S8 规格](../../design/issues/S8-create-bot.md)

## 职责

Bot 的**业务编排**：两个读（`bot.list` / `memory.charter.get`，S6）+ 两个写（`bot.create` /
`bot.set_permission`，S8）。与 `repository` 同一层、同一纪律：只吃 `dict`、返回
`dict`/`list[dict]`，**不碰 fastapi**；下游异常翻成 07 §1.3 的码。

S8 把 M1 的一处临时形态清掉了：**`bot.list` 不再惰性播种**（一个读方法曾会落一行，U17 的
第一半）。现在"空仓库列出 `[]`"是正常状态，创建是人的动作。

三处裁决在这里落地：**D6 先判后写**（create 的前五步不碰磁盘）、**D5 文件是事实源**
（`charterRev` / 摘要现场算，`charter_rev` 列只在创建时写一次）、**D9 绝对路径**
（`memoryPath` 不伪装成 `~`）。

## 符号表

| 符号 | 行 | 做什么 | 谁在用 |
|---|---|---|---|
| `INVALID_PARAMS` / `INTERNAL` / `NOT_SUPPORTED` / `REPO_NOT_BOUND` | 42-45 | 07 §1.3 的既有码 | 错误映射 |
| `ALREADY_EXISTS` | 48 | **S8 新增码**：创建撞名（为什么不是 `INVALID_PARAMS` 见下） | `create`、页面 |
| `MSG_*` 十一个文案 | 51-62 | 可读中文，**不含任何用户输入**（05 §4.2） | 错误映射 |
| `DEFAULT_SUMMARY` | 65 | `"尚未填写职责"`（D5 第 5 条：空正文不是空串） | `charter_summary` |
| `SUMMARY_MAX_CHARS` | 66 | `80`（D5 第 4 条：按字符数截断 + `…`） | `charter_summary` |
| `type BotPayloads` | 70 | `list[dict]` 的类型别名（`list` 方法名会遮蔽泛型，同 `repository.py`） | 注解 |
| `_COMMENT` / `_EMPHASIS` | 72-73 | 摘要规则的正则：整段 HTML 注释 / 行内强调标记 | `charter_summary` |
| `_fail(code, message)` | 76 | 抛 `frames.FrameError`（网关翻成 `ok:false` 帧） | 本模块 |
| `charter_rev(content)` | 81 | D5：`int(sha256(content)[:8], 16)`，随内容变、无状态 | `_split`、`create`、`charter_get` |
| `charter_summary(content)` | 86 | D5 的摘要五条（跳过标题/空行/注释 → 首段 → 去强调 → 截断 → 默认文案） | `_split` |
| `BotService` | 108 | 五个帧方法的实现（构造时注入两个 store） | `app.create_app` → `gateway.Providers` |
| `BotService.list(params)` | 119 | `repoId` → Bot 数组；**纯读**，空仓库回 `[]`（S8 D7） | `bot.list` |
| `BotService.create(params)` | 124 | 七步：repoId → 名字 → 权限 → agentKind → 重名 → 记忆空间 → 落行（D6） | `bot.create` |
| `BotService.set_permission(params)` | 158 | `botId` + 权限 → 改库并回载荷（同值幂等） | `bot.set_permission` |
| `BotService.charter_get(params)` | 170 | `botId` → `{content, rev}`（读文件，缺目录自愈） | `memory.charter.get` |
| `BotService.context_preview(params)` | 181 | **S17**：`botId` → 只读来源清单（仓库实时读取 + `conflict` 标记，不抛错） | `memory.context.preview` |
| `_require_repo` / `_require_bot` | 181/191 | 参数形状 + 存在性 → D6 的错误码 | 四个公开方法 |
| `_require_name(params)` | 205 | 新名字的白名单（06 §2.2）；非 `str` 走同一句错误 | `create` |
| `_permission(params, *, default)` | 212 | 入站权限 → `Permission`；缺省 / `L3` / 其它 三种拒绝各有各的码 | `create`、`set_permission` |
| `_refuse_agent_kind(params)` | 233 | 只收缺省/`null`：给名字 → `NOT_SUPPORTED`，给别的类型 → `INVALID_PARAMS` | `create` |
| `_split(bot)` | 246 | 实体 → **恰好 8 字段**的 camelCase 载荷（D1） | 四个方法 |
| `_charter(bot)` | 260 | 读 charter；目录被用户删掉时按空正文处理（不在读路径上重建） | `_split` |

## 关键决策与理由

### S8 D6：判完再写（前五步一个字节都不落盘）

`create` 的顺序是 contract：**repoId → 名字 → 权限 → agentKind → 重名**（都不碰磁盘）→
`ensure_memory_space` → `insert`。所以"非法名字 → 什么都没创建"是**字面意义**上的：
没有行、没有目录（`test_create_rejects_illegal_names_and_writes_nothing` 数行数与目录）。

第六步是幂等的（`ensure_memory_space` 从不覆盖已有 charter，S6 D3），所以第七步失败最多留下
一个"只有模板 charter、没有行"的空目录——用户下次用同名重试时正好复用。

### S8 D5：为什么撞名要一个新码

页面按**码**选文案（它从不显示服务端 `message`）。把"名字合法但已被占用"塞进
`INVALID_PARAMS`，页面就只能说"名字不合法"——那是假话。所以 `ALREADY_EXISTS`(409)：
"你要创建的东西已经存在"。与 S7 的 `WORKTREE_CONFLICT` 同一个理由、同一种形状。

`IntegrityError → ALREADY_EXISTS` 的映射是**今天**的事实：第 1 步刚证明仓库行存在，而
`repo.unbind` 还没实现，所以不会是外键错。S22 做 unbind 时必须回来看这一行。

### S8 D7：`bot.list` 为什么必须是纯读

S6 的惰性播种是 M1 的权宜（没有创建入口，又得让人看见一个 Bot）。代价是一个 GET 会写盘。
创建入口一落地，这个代价就只剩坏处：并行客户端会撞 `UNIQUE`、`doctor` 会把"读"当成"没变"、
备份工具会把列表页当成写操作。所以删掉 `_seed` 与 `PRESET_BOT_NAME`，并把 S6 的测试改成
"先建后读"（`_make_bot`）。

### D5：charter 正文的事实源是**文件**，不是 DB

- `charterRev` 与 `charterSummary` 都**现场**从 `charter.md` 算。
- DB 的 `charter_rev` 列**只在创建时**写一次（S8 起），之后允许过期——U17 的第二半仍在
  S22（`memory.charter.set`）。没有任何读路径读那一列，它只是给将来自检用的快照。
- 摘要是给左栏一行看的：跳过标题/空行/HTML 注释 → 第一个正文段落（多行用空格拼）→
  去掉 `**`/`*`/`_`/`` ` `` → 超过 80 字符截断加 `…` → 没正文回「尚未填写职责」。

### D6：`botId` 查不到 = `INVALID_PARAMS`（不新增码）

07 §1.3 没有更贴的码（`REPO_NOT_BOUND` 专指 `repoId`）。路径穿越企图**不需要单独判格式**：
不合法的 `botId` 在库里必然查不到，查不到就拒绝，**一个文件都不会碰**；名字进路径前的
白名单与越界断言在 `storage/paths.py:bot_memory_dir`（D3 的纵深防御）。

### `_charter()` 对"目录被用户删了"的语义

`list` / `create` 的载荷只读文件：文件不存在按**空正文**处理（摘要走默认文案，不编造），
也不在读路径上重建目录。`memory.charter.get` 才调 `ensure_memory_space`（缺什么建什么、
已有 charter 不覆盖），所以"手删记忆目录后再点开"会自愈出模板骨架。两种语义的差别是有意的。

## 修改须知

| 如果你要… | 还必须 |
|---|---|
| 改载荷字段 | D1 的 8 字段是契约：`07 §4.7` 表 + `tests/test_bot_charter.py`（`set(payload)` 断言）+ 前端 `assets.py` |
| 改摘要规则 | D5 五条 + `test_charter_summary_follows_the_five_rules` + `03-ui-spec §4.3` 的空态文案 |
| 把 `L3`/`L4` 加进可授予集合 | **别**：这是产品决定（01 §4、05 §2.3）。改了会同时红掉 `test_create_rejects_l3_and_l4` 与两条反向注入 |
| 让 `bot.list` 再写点什么 | 别：U17 第一半就是为删掉这个才存在的（见上） |
| 加 `bot.delete` / `charter.set` | 那是后续切片（S22 起）；不要在 S8 的实现里提前长出来 |
| 在这里 import fastapi / web / 同层 handler | **禁止**：层门禁会拦（`api/worktrees.py` 那次之后，L3 之间也不再允许互 import） |

## 测试

| 测试 | 守什么 |
|---|---|
| `tests/test_bot_create.py::test_create_returns_the_eight_field_payload` | 验收 1：载荷 8 字段 + 选中的权限 |
| `...::test_create_defaults_to_l1` | 验收 4 / D2：缺省与 `null` 都落 L1 |
| `...::test_create_rejects_illegal_names`（10 种坏形状） | 验收 2 |
| `...::test_create_rejects_illegal_names_and_writes_nothing` | **D6 前五步**：行数与目录都是零 |
| `...::test_create_accepts_the_four_assignable_levels` | 验收 3 的反面 |
| `...::test_create_rejects_l3_and_l4` / `...::test_create_rejects_unknown_permission_values` | D2：两种拒绝不是一个码 |
| `...::test_create_accepts_null_agent_kind_and_refuses_a_string` | D4 |
| `...::test_create_duplicate_name_is_already_exists` / `...::test_same_name_in_two_repos_is_allowed` | D5 + 复合主键 |
| `...::test_create_makes_memory_space_with_template_charter` | 验收 5（真 stat，T38） |
| `...::test_list_on_a_repo_with_no_bots_writes_nothing` | **D7 / U17**：纯读 |
| `...::test_set_permission_changes_the_value` / `..._is_idempotent` / `..._rejects_l3_and_l4_without_writing` | 验收 7 |
| `...::test_ws_bot_create_and_set_permission_frames` | 帧层：两个新方法真的挂在网关上 |
| `...::test_page_permission_select_lists_exactly_four_levels` | 验收 3（静态：两个下拉都只有四个） |
| `tests/test_bot_charter.py::test_bot_list_returns_created_bots_and_repeats_identically` | S6 第 1 行在 S8 改写后的版本 |
| `...::test_ws_bot_frames_match_service` | 真 WS 帧与直调服务层一致（S8 起先建再列） |
| `tests/test_reverse_checks.py` 的 4 条 S6 注入 + 4 条 S8 注入 | 改播种/白名单/权限/默认值会让它们红 |
