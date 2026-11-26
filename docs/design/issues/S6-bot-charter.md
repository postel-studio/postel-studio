# S6 · 左栏出现 Bot 与它的职责

- **里程碑** M1（最后一刀）· **预计** 2h · **依赖** S3（实际还依赖 S4/S5 的页面结构）· **优先级** P1
- **用户路径**：左栏上方有「Bots」区 → 看到 Bot 名与职责摘要 → 点开主区看到 charter 全文与记忆目录
- **M1 收口**：本刀同时交付 **M1 全链路验收**（`scripts/accept_m1.py`，一条命令复现 S1–S6）。

## 背景

Bot 是这个产品与普通 Agent 工具的分界。M1 不需要它能跑任务，只需要它**存在且可读**——证明"Bot 是一等公民"不是空话。

## 验收标准

1. 左栏「Bots」区列出该仓库下的 Bot（M1 有一个预置 Bot，不需要创建入口）。
2. 每个 Bot 显示名字、权限徽标与职责摘要（charter 第一段）。
3. 点开主区显示 charter 全文 + 记忆目录路径。
4. 记忆目录在仓库**外**：`<状态根>/bots/<repo-id>/<bot>/`，含 `charter.md` + `memory/` + `sessions/`。
5. 首次访问幂等创建，**绝不覆盖**已编辑的 charter；目录 `0700`、文件 `0600`。
6. **M1 验收**：`uv run python scripts/accept_m1.py` 一条命令走通 S1–S6，退出码 0/1。

## 涉及文件

- `src/postel_studio/domain/models.py`（`Bot` 实体 + `bot_id()` + 名字白名单，纯函数）
- `src/postel_studio/storage/paths.py`（`bots_root` / `ensure_memory_space` / `read_charter` / 模板 + 文件 IO）
- `src/postel_studio/storage/db.py`（v3 迁移 + `BotStore`）
- `src/postel_studio/api/bots.py`（**新模块** L3：`BotService`，必须登记到 `scripts/gate-layers.py` 与 `02-layers`）
- `src/postel_studio/api/gateway.py` / `api/app.py`（`bot.list` / `memory.charter.get` 注册与注入）
- `src/postel_studio/web/assets.py`（左栏 Bots 区 + 主区 charter 面板）
- `scripts/accept_m1.py`（新建，M1 全链路验收）
- `tests/test_bot_charter.py`、`tests/test_accept_m1.py`（新建）、`tests/test_reverse_checks.py`（加 5 条）

## 禁止事项

- 不得把记忆写进仓库（即使只在 `.gitignore` 里）。
- 不得在 charter 模板里预设业务职责（骨架必须空白待填）。
- 不得让 `accept_m1.py` 使用真实 `~/.postel`。
- 不得新增错误码、不得加 HTTP 路由、不得加依赖。

---

# 裁决与验收细则（管理层，实施前钉死）

> 每条都是契约：实现可以换写法，但字段名、错误码、DOM id、常量值、文案不许改。
> 上游：[07-api-contract.md](../07-api-contract.md) §2.3 / §2.6 / §1.3、[06-data-model.md](../06-data-model.md) §2.2 / §8、
> [03-ui-spec.md](../03-ui-spec.md) §2 / §4.3、[05-security.md](../05-security.md) §3.3 / §4.1、
> [09-slice-dod.md](../09-slice-dod.md)。
> "实测"都是我（裁决者）跑过的命令；**S6 没有前置未决项**（U15 已在 S5 结案，U14/U1 的到期在 S7/S15）。

## D1 · `Bot` 实体与载荷（钉死 **8 个字段**）

`bot.list` 的 `data` 是**数组**，每项八个字段，**不多不少**：

```json
{"botId":"adec6540a725/builder","repoId":"adec6540a725","name":"builder",
 "permission":"L1","agentKind":null,"charterRev":3254720643,
 "charterSummary":"尚未填写职责","memoryPath":"/home/u/.postel/bots/adec6540a725/builder"}
```

| 字段 | 类型 | 规则 |
|---|---|---|
| `botId` | string | `{repoId}/{name}`（06 §2.2 的复合主键） |
| `repoId` | string | 12 位 hex（S3.5 D1，直接消费） |
| `name` | string | 06 §2.2 白名单 `^[a-z][a-z0-9-]{0,31}$` |
| `permission` | string | M1 恒为 `"L1"`（06 §2.2 默认；改权限是 M2） |
| `agentKind` | string \| **null** | M1 是 **`null`（未选择）**，不是编一个适配器名：S9 之前没有任何通道被验证过，编名字等于宣称"已支持"（04 §6 禁止） |
| `charterRev` | number | 见 D5：`int(sha256(charter 内容)[:8], 16)` |
| `charterSummary` | string | 见 D5；没有正文时是 `"尚未填写职责"` |
| `memoryPath` | string | **绝对路径**（裁决见 D9） |

## D2 · 预置 Bot：**惰性播种**，只在一个地方

M1 没有创建入口（`bot.create` 是 S8）。规则：

- `bot.list(repoId)` 时，若该 repo **一个 Bot 都没有**，创建预置 Bot：`name="builder"`、`permission="L1"`、
  `agentKind=NULL`、`charterRev` 按 D5 算，并 `ensure_memory_space`。
- 已有任何 Bot → **绝不新建**（幂等；测试连调两次仍是 1 个）。
- 为什么放在 `bot.list` 而不是 `repo.bind`：① bind 是 S3 的路径，把 Bot 逻辑塞进去会让 S3 的测试
  依赖 Bot；② **老的已绑定仓库**（S6 之前绑的）只有惰性播种才能自动补上，bind 播种会漏；
  ③ 06 §2.2 的 Bot 行本来就是"元数据"，与仓库路径是否还在无关。
- 代价（如实记）：`bot.list` 是一个**读方法但会落一行**。这是 M1 的临时形态，S8 有创建入口后
  预置 Bot 要么删掉、要么由显式的"初始化仓库"动作负责（记入 U17）。

## D3 · 记忆目录与文件（**路径与权限是安全约束**）

```text
<状态根>/bots/<repo_id>/<name>/
├── charter.md      # 0600
├── memory/         # 0700
└── sessions/       # 0700
```

- `<状态根>` = `storage/paths.state_root()`（`POSTEL_HOME` 或 `~/.postel`）。
- `ensure_memory_space(repo_id, name)` 的语义：**缺什么建什么**；
  `charter.md` 已存在就**只读不写**（幂等，不覆盖用户编辑）。
- 权限：目录 `0700`、文件 `0600`（05 §4.1）。**必须显式 chmod**，因为 umask 会让默认值变松——
  实测（umask 022）：

  ```text
  $ python -c "from pathlib import Path; import stat; Path('a/b/c').mkdir(parents=True, mode=0o700); print([oct(stat.S_IMODE(p.stat().st_mode)) for p in (Path('a'),Path('a/b'),Path('a/b/c'))])"
  ['0o755', '0o755', '0o700']        # ← 只有叶子吃到 mode，父目录是 0755
  $ python -c "from pathlib import Path; Path('a/b/c/f.txt').write_text('x'); import stat; print(oct(stat.S_IMODE(Path('a/b/c/f.txt').stat().st_mode)))"
  0o644                               # ← 普通写入是 0644
  ```

  所以：**每一层建完都要 chmod 0700**；写 charter 用 `paths.atomic_write`（内部 mkstemp→0600→rename），
  **不要**用 `Path.write_text`。
- 名字进路径之前必须过白名单（`^[a-z][a-z0-9-]{0,31}$`）**并**校验最终 `resolve()` 的路径仍在
  `bots_root()` 之下（纵深防御：白名单已足够，但解析后再断言一次能挡住将来放宽白名单的人）。

## D4 · charter 模板（骨架空白待填，**禁止业务职责**）

`CHARTER_TEMPLATE`（`name` 代入；末尾一个换行）：

```markdown
# {name}

## 身份

## 职责

## 边界

## 工作方式
```

只有标题骨架，**没有任何职责描述**（禁止事项）。人填之前它就是空的——UI 的空态由 D5 的摘要规则负责。

## D5 · `charterRev` 与 `charterSummary`

- **内容的事实源是文件**（不是 DB）：`memory.charter.get` 读 `charter.md`、现场算
  `charterRev = int(sha256(content.encode("utf-8")).hexdigest()[:8], 16)`（整数、随内容变、无状态）。
  DB 的 `charter_rev` 列先建（06 §2.2 的字段），**M1 不在读路径写它**——改 charter 的写路径是 S22，
  届时由 `charter.set` 维护这一列（如实记入 U17）。
- **摘要规则**（实现与测试都照这五条）：
  1. 跳过 `#` 开头的标题行、空行、HTML 注释（`<!-- … -->` 整段）；
  2. 取**第一个非空正文行**，直到段落结束（下一个空行或标题行）——多行拼成一段；
  3. 去掉行内 Markdown 强调标记（`**` / `*` / `_` / `` ` ``）后再输出；
  4. 超过 **80 字符**截断并加 `…`（按字符数，不按宽度）；
  5. 没有任何正文 → `"尚未填写职责"`（不是空串、不是编造）。

## D6 · 错误码（**不新增码**，07 §1.3 之内）

| 情形 | 码 |
|---|---|
| `repoId` 缺失 / 非字符串 / 空 | `INVALID_PARAMS` |
| `repoId` 查不到 | `REPO_NOT_BOUND` |
| `botId` 缺失 / 非字符串 / 格式不合法（含路径穿越企图） | `INVALID_PARAMS` |
| `botId` 格式合法但查不到 | `INVALID_PARAMS`（文案「找不到这个 Bot」） |
| 记忆目录 / charter 写失败（权限、磁盘满） | `INTERNAL` |
| 成功（含"尚未填写"的空 charter） | `ok: true` |

**为什么 `botId` 查不到用 `INVALID_PARAMS`**：07 §1.3 里没有更贴的码（`REPO_NOT_BOUND` 专指 repoId），
而 `INVALID_PARAMS` 的语义是"参数指向的东西不成立"（400）。**M2 若要给 Bot 一族正式码**
（`bot.create` 的重名、`bot.set_permission` 的越界都需要），应在那一刀里**同时**改 07 表 + `errors` 映射 +
PR 体单独标注（DoD §2.2），并把这行改掉。M1 不擅自扩码表。

**`bot.list` 不探测仓库路径是否还在**：Bot 元数据在 `state.db`、charter 在状态根，与仓库目录是否存在
无关——仓库被删了，你仍然应该能读到它给 Bot 写的 charter（这是审计材料）。只有 `repoId` 查不到才报
`REPO_NOT_BOUND`。

## D7 · 分层落点（新模块必须登记，未登记 gate 会拦）

| 层 | 文件 | 新增 |
|---|---|---|
| L0 | `domain/models.py` | `Bot`（frozen dataclass）、`bot_id(repo_id, name)`、`BOT_NAME_RE`/`bot_name_ok()`（纯函数，无 IO） |
| L0 | `storage/paths.py` | `bots_root()`、`bot_memory_dir()`、`ensure_memory_space()`、`read_charter()`、`CHARTER_TEMPLATE`（文件 IO 只在这个模块） |
| L0 | `storage/db.py` | v3 迁移（`bots` 表）+ `BotStore`（`list_for_repo` / `get` / `insert`） |
| L3 | `api/bots.py`（新） | `BotService.list` / `BotService.charter_get`（播种、摘要、rev、错误码） |
| L3 | `api/gateway.py` | `Providers` 加 `bot_list` / `memory_charter_get`，`dispatch()` 两行 |
| L3 | `api/app.py` | 注入 `BotService` |
| L4 | `web/assets.py` | `#bots` 左栏区 + `#charter` 主区面板 |

- **必须**在 `scripts/gate-layers.py` 的 `LAYERS` 登记 `api/bots.py`（L3，禁止 `postel_studio.web`），
  并在 `02-layers.md` 的层表里加一行；否则 `check.sh layers` 会红——这是门禁的预期行为，别绕过。
- `bots` 表 v3 迁移：**只加表、不动 `repos`**；迁移失败整体回滚（既有 `migrate()` 的机制）。
  老库（v2）升级后 `repos` 数据必须原样还在（D11 第 14 行）。
- `bot.list` / `memory.charter.get` 的参数是 `dict`、返回 `dict`/`list[dict]`（网关只认这个形状）。

## D8 · DOM 契约（浏览器验收按这些 id 点）

**左栏 Bots 区在 `#repos` 之前**（03-ui-spec §2：左栏上 Bot / 下 仓库）：

```text
<section id="bots" data-state="idle" aria-live="polite">
  <h2 id="bots-title">Bots</h2>
  <p id="bots-state">选择一个仓库查看它的 Bot</p>
  <p id="bots-error" role="alert" hidden></p>
  <ul id="bot-list"></ul>
  <p id="bot-empty" hidden>这个仓库还没有 Bot。</p>
</section>
```

**主区 charter 面板放在 `#diff` 之后**（不重排既有布局）：

```text
<section id="charter" data-state="idle" aria-live="polite">
  <h2 id="charter-title">Charter</h2>
  <p id="charter-state">点左侧的 Bot 查看它的 charter</p>
  <p id="charter-error" role="alert" hidden></p>
  <p id="charter-memory" hidden></p>
  <pre id="charter-content" hidden></pre>
</section>
```

| 元素 / 属性 | 规则 |
|---|---|
| `#bots[data-state]` / `#charter[data-state]` | `idle` / `loading` / `ready` / `error`；显隐**只由 CSS** 决定，JS 只改属性与 `hidden`（T8） |
| `#bots-state` | 未选仓库「选择一个仓库查看它的 Bot」；选中后 `loading` 时「正在读取 Bot…」；断连「已断开：Bot 列表未知」 |
| 每行 | `li[data-bot-id]`（`tabindex="0"` + `role="button"` + 选中时 `data-selected="true"`），内含 `.bot-name` / `.bot-permission`（文本 `L1`）/ `.bot-charter-summary` |
| `#bot-empty` | `ready` + 空列表时可见（M1 正常不会出现，但空态必须可读，不许空白） |
| `#charter-title` | 未选中「Charter」；选中后「Charter · <bot name>」（`textContent`） |
| `#charter-memory` | 文本 `记忆目录：<绝对路径>`（D9 的裁决） |
| `#charter-content` | charter 全文，**一个 `<pre>` + `textContent`**（charter 是用户可编辑文本 = 不可信，**禁止 innerHTML**） |
| 切仓库 / 断连 | 两个面板都回 `idle` 并清空（不留旧 charter；同 S4/S5 纪律）；重连后若仍有选中仓库则重取 `bot.list`，**但不清空已显示的 charter**（选中 Bot 与 repo 强绑定，重连时 repo 仍在就仍有效）——这一条与 diff 的"不重放"不同，因为 charter 是**静态内容**，重放不会覆盖用户正在看的对比 |
| 快速切换 | `botRequestToken` / `charterRequestToken` 各自拦住过期回包 |

CSS：`.bot-permission` 用中性徽标样式；`#charter-content` 用 `white-space: pre-wrap` + 等宽字体 +
`max-height: 60vh; overflow:auto`；不设固定宽度、不假设中文宽度。

## D9 · 路径显示：**显示绝对路径，不伪装 `~`**（对验收 #4 字面的偏离与理由）

S6 验收 #4 写的是 `~/.postel/bots/<repo-id>/<bot>/`。实现**显示 `memoryPath` 绝对路径原样**，理由：

1. 状态根可被 `POSTEL_HOME` 覆盖；把临时路径显示成 `~/.postel/...` 是**假话**，用户会去找一个
   不存在的地方。安全类信息宁丑不假（05 §4.2 的同一精神）。
2. 路径本来就是给用户复制去打开的，绝对路径更可直接用。

缺省 `POSTEL_HOME` 时绝对路径恰好等于 `~/.postel/...`，验收的意图（"告诉用户记忆在哪"）满足。
测试断言：`memoryPath` 以 `str(state_root())` 开头、**不在**仓库路径内、目录真实存在。

## D10 · M1 全链路验收脚本契约（`scripts/accept_m1.py`）

一条命令：`uv run python scripts/accept_m1.py`（在仓库根、已 `uv sync` 的前提下）。

**硬约束**：
- 全程用 `tempfile.mkdtemp()` 造独立工作目录，`POSTEL_HOME` 与 `HOME` 都指过去；**绝不碰真实 `~/.postel`**。
- 起**真内核**子进程（`<venv>/bin/postel-studio serve --port 0`），从 `server.json` 或启动输出取地址；
  从 `/` 页面抠 `ticket` / `wsPath`；用真 WebSocket 发帧。
- 建**真 git 仓库**：`main` 一个提交，`feature` 分支改一个文件（供 S5 用）。
- 无论成功失败，都要**收掉内核进程**并删临时目录（`try/finally`），不留残留（06 T18 的纪律）。
- 不依赖浏览器、不依赖 puppeteer（DOM 由 `tools/acceptance/*.mjs` 负责，脚本只验协议 + 页面静态标记）。

**步骤与输出（格式是契约，`tests/test_accept_m1.py` 会解析）**：

| 步 | 验什么 | 通过条件 |
|---|---|---|
| S1 | 页面与存活 | `GET /` 200 且页面含渲染后的版本号；`GET /api/health` 的 `status=="ok"`、`apiVersion=="1"` |
| S2 | 连接与帧 | 连 WS → `system.health` 回 `ok:true` 且 `result.data.status=="ok"` |
| S3 | 绑仓库 | `repo.bind(临时路径)` → `ok`；`repo.list` 里能找到该 `repoId` |
| S4 | 分支 | `repo.branches` → 至少含 `main` 且 `isCurrent==true` |
| S5 | diff | `repo.diff(main, feature)` → `ok`、`stat.filesChanged>=1`、`patch` 非空、`truncated==false` |
| S6 | Bot 与 charter | `bot.list` → 恰好 1 个 `builder`（permission L1、`charterSummary=="尚未填写职责"`）；`memory.charter.get` → `content` 含 `# builder`、`rev>0`；`memoryPath` 存在、含 `charter.md`/`memory/`/`sessions/`、**不在仓库路径内**、目录 0700、charter 0600 |
| 家具 | 页面有六刀的容器 | 页面 HTML 含 `id="bots"`、`id="repos"`、`id="branches"`、`id="diff"`、`id="charter"`、`id="status"` |

- 每步打印一行：`PASS S1 页面与存活` … `PASS S6 Bot 与 charter`；失败打印 `FAIL S<n>: <原因>` 并
  **继续跑完剩余步骤**（一次跑出全部问题，而不是修一个跑一次），最后退出码 1。
- 末行：`M1 OK（6/6 步 + 家具）`；失败时末行 `M1 FAILED（<n> 处）`。
- 脚本要能被 `tests/test_accept_m1.py` 当子进程跑（超时 120s），断言 `returncode==0` 且输出含
  `M1 OK`、且 6 个 `PASS S` 都出现。

## D11 · 测试矩阵（验收 → 测试，`tests/test_bot_charter.py` 内 + 注明者除外）

| # | 用例 | 断言要点 |
|---|---|---|
| 1 | 惰性播种 | 首次 `bot.list` 返回 1 个 `builder`；再调一次仍是 1 个（不重复播种） |
| 2 | 幂等不覆盖 | 手写 charter 内容 → 再 `bot.list` + `charter.get` → 内容**逐字未变** |
| 3 | 仓库隔离 | 两个 repo 都播种 `builder`，各自写不同 charter → `charter.get` 各回各的 |
| 4 | 路径与布局 | `memoryPath` 在 `bots_root()` 下、**不在仓库路径内**；目录含 `charter.md`/`memory/`/`sessions/` |
| 5 | 权限 | 目录链全 `0700`、`charter.md` `0600`（**用 `stat` 断言真实 mode**） |
| 6 | `charterRev` | 载荷 `{content, rev}`；改内容后 `rev` 变、不改则 `rev` 相同（确定性） |
| 7 | 摘要五条规则 | 跳过标题/空行/注释、多行合并、去强调标记、>80 截断加 `…`、空 → 「尚未填写职责」 |
| 8 | 载荷 8 字段 | `set(payload)=={botId,repoId,name,permission,agentKind,charterRev,charterSummary,memoryPath}`，`agentKind is None` |
| 9 | 错误码 | 未知 `repoId`→`REPO_NOT_BOUND`；`repoId`/`botId` 形状错→`INVALID_PARAMS`；`botId` 查不到→`INVALID_PARAMS` |
| 10 | 路径穿越 | `botId="../../etc/passwd"` 之类 → `INVALID_PARAMS`，且**没有在状态根外创建任何东西** |
| 11 | 名字白名单 | `domain.bot_id`/`bot_name_ok` 对 `../x`、`Builder`、`a_b`、33 字符 → 拒绝；`builder`、`qa-1` → 接受 |
| 12 | v2→v3 迁移 | 造一个有 `repos` 行的 v2 库 → `open_state_db` 升级后 `repos` 行还在、`bots` 表可用 |
| 13 | 端到端 WS 帧 | `bot.list` / `memory.charter.get` 走真 WS 帧，载荷与直调服务层一致；`seq` 递增 |
| 14 | 页面契约（文本断言） | 两个面板的 id、文案、行结构、`innerHTML` 零使用（真实 DOM 由浏览器验收） |
| 15 | M1 验收脚本 | `tests/test_accept_m1.py` 跑脚本 → exit 0、输出含 6 个 `PASS S` + `M1 OK`；跑完不残留 `POSTEL_HOME` |
| 16 | 反向注入 5 条 | 见 D12 |

**既有测试要改一处**：`tests/test_gateway.py::test_unknown_method_is_unknown_method_not_subscribe`
的反例列表里若有 `bot.list`，必须移出（它已实现），反例仍需 ≥4 个未实现方法。

## D12 · 反向注入（`tests/test_reverse_checks.py`，每条只注入一类缺陷）

| # | 注入 | 应当变红的测试 |
|---|---|---|
| 1 | `ensure_memory_space` 每次都用模板覆盖 `charter.md` | 幂等不覆盖（D11 #2） |
| 2 | 去掉 `charter.md` 写入后的 `0600` 收紧（用 `Path.write_text`） | 权限（D11 #5） |
| 3 | 目录建完后不 chmod（只靠 `mode=0o700`） | 权限（D11 #5，父级会变 0755——D3 实测） |
| 4 | 进路径前不校验 bot 名字白名单 | 路径穿越（D11 #10） |
| 5 | charter 渲染从 `textContent` 改成 `innerHTML` | 页面 `innerHTML` 零使用（D11 #14） |

## D13 · 文档与门禁

- `07-api-contract.md`：**§4.7 `bot.list`**（载荷 8 字段表 + 惰性播种说明）、**§4.8 `memory.charter.get`**
  （`{content, rev}`、rev 算法、D6 的错误码表）；§2.3 / §2.6 已登记方法不用改。
- `06-data-model.md`：§2.2 补一行「charter 内容在文件、`charter_rev` M1 不维护（S22 维护）」；
  §8「明确不存的东西」补一行：**charter 正文不落库**（文件是事实源）。
- `03-ui-spec.md`：§2 左栏加 Bots 区、§4.3 补 M1 的可读形态（全文 + 记忆目录；四张卡是 M2）。
- `02-layers.md`：层表加 `api/bots.py`（L3）。
- codemap：`03-modules/{paths,db,models,bots,gateway,app,assets}.md` + `README` 行数表 +
  `04-capabilities.md`（加「Bot 与 charter」）+ `05-change-impact.md` + `06-pitfalls.md` 新增
  **T38**（umask：`mkdir(mode=)` 只管叶子、`write_text` 是 0644 → 每层显式 chmod + 用 atomic_write）
  与 **U17**（`bot.list` 惰性播种 + `charter_rev` 未维护 → 到期 S8 / S22）。
- `scripts/gate-layers.py` 的 `LAYERS` 加 `api/bots.py`；`README.md` 或 `CONTRIBUTING.md` 加一行
  M1 验收命令。
- `bash scripts/check.sh all` 全绿、`build` 单独跑绿；改了前端 → 重跑**全部** `tools/acceptance/*.mjs`
  （本刀应新增 `tools/acceptance/s6_bots.mjs`，并在 Dev 里顺带确认 S3/S4/S5 的脚本仍绿）。

## D14 · 预算与禁止事项

**`src/` 预算**：预计 **7 个文件 / 约 330–400 行**（口径：只计 `src/`）。上一次 S5 超到 641 行——
本刀**必须在规格阶段控住**：如果实现过程中 `src/` 逼近 400 行，先砍 UI 装饰（徽标样式、过渡动画），
**不许砍验收项**；仍超就当场拆（把 `#charter` 面板留给 S6b，但 M1 验收里"点开看全文"必须可演示）。

禁止：不加错误码、不加 HTTP 路由、不加依赖、不写仓库目录、不实现 charter 编辑（S22）、
不实现 `bot.create`（S8）、不做 Bot 状态点/运行态（M2）、不做记忆内容注入（S17）。
