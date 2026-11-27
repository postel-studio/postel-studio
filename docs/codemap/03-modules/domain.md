# 03 · domain — `src/postel_studio/domain/`

> 层 L0（纯函数，**无 IO**）· 453 行 · 上游：[模块索引](README.md) · 相关：[06-data-model §2.1](../../design/06-data-model.md) · [08-repo-id-stability](../../design/08-repo-id-stability.md)

## 职责

领域模型与**纯派生规则**：`Repo` / `Bot` / `Task` / `WorktreeLease` 实体、origin URL 归一化、
`repo_id` / `bot_id` 与名字白名单的算法（S6 加了 Bot，S8 加了权限阶梯，S10 加了任务状态机与 ULID，S11 加了候选实体与分支名派生）。
这一层**不碰磁盘、不起子进程、不 import 框架**——它的每个函数都能在一行断言里被验证。

S3 把它单独成层而不是塞进 `api/repository.py` 的原因：id 的算法是被验收断言过的契约
（S3.5 D1：长度 12、只由路径决定；D2：https / ssh 归一化后 `origin_hash` 相同）。
如果它和 git、SQLite 长在一起，验证这些不变量就得先建仓库、再建数据库。

## 文件

| 文件 | 行数 | 内容 |
|---|---|---|
| `__init__.py` | 5 | 包标记 + 本层"无 IO"的声明 |
| `models.py` | 453 | `Repo` / `Bot` / `normalize_origin` / `repo_id*` / `origin_hash` / `bot_name_ok` / `bot_id` / `ref_name_ok`（S7）/ `Permission`（S8）/ `ulid` / `prompt_ok` / `Task` / `WorktreeLease` / 任务状态机（S10） |

## 符号表（`domain/models.py`）

| 符号 | 行 | 做什么 | 谁在用 |
|---|---|---|---|
| `REPO_ID_LENGTH` | 23 | `12`：06 §2.1 的 id 长度 | `models.repo_id*`、测试 |
| `_SCP_LIKE` / `_URL_LIKE` | 27-28 | 两种 remote 写法的正则 | `normalize_origin` |
| `Repo` | 32 | frozen dataclass：`repo_id` / `path` / `origin_url` / `default_branch` / `added_at` / `origin_hash`（**末位、有缺省值**） | `storage/db.py`、`api/repository.py` |
| `Repo.name`（property） | 49 | 根目录名 = 左栏显示名（`proj/src` 绑成 `proj` 后显示 `proj`） | `api/repository.py:_split` |
| `normalize_origin(url)` | 54 | URL → 与传输方式无关的 `host/path` | `repo_id_for_origin`、测试 |
| `repo_id_for_origin(url)` | 91 | `sha256(归一化 URL)[:12]`——**只给 `origin_hash` 用**（S3.5 D2） | `origin_hash` |
| `repo_id_for_path(path)` | 101 | `sha256(绝对路径)[:12]`——`repo_id` 的**唯一**算法（D1） | `repo_id` |
| `origin_hash(origin_url)` | 112 | 归一化 URL 的哈希；无 origin → `None`（D2，**不参与主键**） | `api/repository.py:bind` |
| `repo_id(path)` | 125 | **只看路径**（S3 的签名是 `repo_id(path, origin_url)`，已收窄） | `api/repository.py:bind` |
| `BOT_NAME_RE` | 140 | 06 §2.2 白名单 `^[a-z][a-z0-9-]{0,31}$` | `bot_name_ok`、`storage/paths.py` |
| `Permission` | 143 | **S8**：四个**可授予**级别（`L0`/`L1`/`L2`/`L2+`）的枚举；`rank` 是它们在 v1 阶梯上的位置 | `api/bots.py`、页面、测试 |
| `Permission.rank`（property） | 162 | 0..3，只对那四个成员有意义 | `permission_covers` |
| `_PERMISSION_LADDER` | 168 | 阶梯顺序的**唯一**事实源（`rank` 与 `covers` 都读它） | 本模块 |
| `UNASSIGNABLE_PERMISSIONS` | 172 | `{"L3","L4"}`：**认识但不可写入**的两个名字（`NOT_SUPPORTED`） | `api/bots.py:_permission` |
| `BOT_PERMISSION_DEFAULT` | 175 | `Permission.L1`：新建 Bot 的默认级别（05 §2.2；不再是一句字符串） | `api/bots.py:create` |
| `Bot` | 179 | frozen dataclass：`bot_id` / `repo_id` / `name` / `permission` / `agent_kind` / `charter_rev` / `created_at` | `storage/db.py`、`api/bots.py` |
| `bot_name_ok(name)` | 196 | 名字白名单判定（**纯函数**）；进路径前必须过它（D3） | `storage/paths.py:bot_memory_dir`、`api/bots.py:_require_name` |
| `bot_id(repo_id, name)` | 201 | 复合主键 `{repo_id}/{name}`——**唯一的拼接处**（06 §2.2） | `api/bots.py`、`api/worktrees.py`、测试 |
| `permission_parse(value)` | 206 | 入站值 → `Permission`；不是那四个就 `None`（**不抛**）。`L3`/`L4` 也走 `None` | `permission_covers`、`api/bots.py:_permission` |
| `permission_covers(held, needed)` | 222 | 阶梯包含关系（`L2` ⊨ `L1`）；任一端解析不出 → `False`（失败即收紧） | S16 的权限门、测试 |
| `REF_NAME_MAX_LENGTH` | 244 | `200`：入站 ref 名的长度上限（S5 D3.1）——**S7 从 `api/repository.py` 搬来** | `ref_name_ok` |
| `ref_name_ok(value)` | 249 | 入站 ref 名的六条形状（S5 D3.1，**纯判定不抛**）——**S7 搬来** | `api/repository._require_ref`、`api/worktrees._base_ref`、`orchestrator.dispatch`（S10 校验 `baseRef`） |
| `PROMPT_MAX_BYTES` | 283 | **S10**：`32 * 1024`——prompt 的字节上限（02b §5） | `prompt_ok` |
| `TASK_TRANSITIONS` | 287 | **S10**：状态机表（06 §3.1 的唯一事实源）：`queued→running/cancelled`、`running→succeeded/failed/cancelled/interrupted` 等 | `task_transition_ok`、`TaskStore.transition` |
| `TASK_TERMINAL` | 293 | 终态集合（`succeeded` / `failed` / `cancelled` / `interrupted`）：**终态封死**，没有出边 | 同上 |
| `TASK_ACTIVE` | 298 | `{"queued","running"}`：活跃态（BUSY 检查、重启记账都用它） | `orchestrator._is_terminal`、`db.TaskStore.active_for_bot` |
| `TASK_TRIGGERS` | 301 | `{"manual","dialogue","local_event"}`：触发源白名单（S10 只用 `manual`） | `Task` |
| `ulid(now_ms=None, randomness=None)` | 306 | **S10**：手写 ULID（48-bit 时间戳 + 80-bit 随机、Crockford base32、26 字符）——**纯函数**，可注入时间与随机（测试确定性） | `orchestrator.dispatch`、租约 id |
| `prompt_ok(value)` | 331 | **S10**：非空字符串且 ≤ 32 KiB（**按 UTF-8 字节**，同帧上限的口径） | `orchestrator.dispatch` |
| `Task` | 346 | **S10** frozen dataclass（06 §2.3 十二字段）：`task_id` / `bot_id` / `trigger` / `trigger_key` / `prompt` / `base_ref` / `state` / `candidate_id` / `error_code` / `created_at` / `started_at` / `ended_at` | `storage/db.py`、`orchestrator.py` |
| `task_transition_ok(current, target)` | 367 | **S10**：查 `TASK_TRANSITIONS` 的纯判定（非法对 → `False`）；`TaskStore.transition` 在 SQL 之前先问它 | `storage/db.py` |
| `WorktreeLease` | 383 | **S10** frozen dataclass（06 §2.5）：`lease_id` / `bot_id` / `task_id` / `path` / `created_at` / `released_at` | `storage/db.py`、`orchestrator.py` |

## 关键决策与理由

### `normalize_origin` 只抹平"传输方式"，不抹平别的一切

| 输入 | 归一化 |
|---|---|
| `https://github.com/o/p.git` | `github.com/o/p` |
| `git@github.com:o/p.git` | `github.com/o/p` |
| `ssh://git@github.com:22/o/p.git` | `github.com/o/p` |
| `https://GitHub.com/o/p/` | `github.com/o/p` |
| `/srv/git/p.git`（本地路径 remote） | `/srv/git/p.git`（原样） |

去掉的：scheme、`user@`、端口、尾部 `.git`、首尾 `/`；host 统一小写。
**不做 URL 解码**：解码只会把两种来源的差异放大。
**本地路径 remote 原样返回**：它不是 URL，猜它的"仓库身份"没有意义。

### 为什么 `repo_id` 只看路径，origin 降级成 `origin_hash`（S3.5 D1/D2，**U9 已结案**）

S3 的算法是"有 origin 用 URL 哈希、没有才用路径"。后果实测过：`git remote add origin …`
再重绑，同一个仓库的 `repo_id` 就变了——S4 起 `bot_id`、记忆目录、分支列表全挂在 id 上，
会一起失效（[08 §1](../../design/08-repo-id-stability.md) 的实测输出）。

现在拆成两个字段：

| 字段 | 算法 | 稳定性 | 参与主键 |
|---|---|---|---|
| `repo_id` | `sha256(绑定时 resolve 的绝对路径)[:12]` | **永不更新**（`upsert` 不写它） | 是 |
| `origin_hash` | `sha256(normalize_origin(url))[:12]` | 可变（origin 变它就变） | **否** |

**为什么不随机生成（B2）**：保留"从路径能算出来"的可重建性（06 §5：DB 丢失后可从文件
系统重建索引），可解释、可复现。代价是"仓库被移动到新路径后 id 会变"——比"加 remote"
罕见得多，且移动仓库本来就需要人确认。

**注意 `repo_id()` 的签名收窄了**：S3 是 `repo_id(path, origin_url)`，现在 `repo_id(path)`。
把 origin 传进来就等于允许"origin 变了 id 就变"，少一个参数就少一条错路。

### Bot 实体与名字白名单（S6 D7）

`Bot` 是 06 §2.2 的实体，**纯数据类**；`bot_id()` 只做一次 `{repo_id}/{name}` 拼接，
`bot_name_ok()` 就是白名单正则。三者都不碰 IO，所以名字规则（`../x`、`Builder`、33 字符
必须被拒）可以在 L0 用一行断言钉死，不用建数据库。

**`charter_rev` 在实体里，但 M1 不拿它当事实源**：D5 规定 charter 正文的事实源是文件，
帧载荷里的 `charterRev` 由 `api/bots.py` 现场算；DB 里这一列只记播种那一刻的值（U17）。

### 任务状态机与 ULID（S10 D6）

`TASK_TRANSITIONS` 是 06 §3.1 的**唯一**事实源，`task_transition_ok` 是它的纯判定，
`TaskStore.transition` 在写 SQL 之前先问它——非法迁移在 L0 就被一行断言钉死（06-pitfalls
的教训：状态机的正确性不该等到集成测试才验证）。终态封死：没有一条出边从终态出发，
`transition` 对终态的任何调用都拿不到行。

**为什么 ULID 手写**：需求只有「时间有序的随机 id」，引入 `ulid-py` 违反 S3 DoD（不新增依赖）。
48-bit 毫秒时间 + 80-bit 随机 + Crockford base32，30 行纯函数；时间与随机都可注入，
测试能断言「同一毫秒不撞车」「字典序 = 时间序」。

### `prompt_ok` 按字节算，与帧上限同口径

「非空」挡掉空白 prompt；32 KiB 按 **UTF-8 字节**（`len(value.encode("utf-8"))`），
与 02b §5 的帧大小口径一致——中文一个字 3 字节，按字符数判会放行超限载荷。

### 为什么 `name` 是 property 而不是字段

`name` 是**派生**的（`Path(path).name`），落库的只有 `path`。存两列会让"路径改名"时
两列打架。06 §2.1 的 Repo 表里也确实没有 `name` 列。

## 修改须知

| 如果你要… | 还波及 |
|---|---|
| 改 `normalize_origin` | `origin_hash` 的所有已有值（含 v1 迁移后的 NULL → 将来若回填）会变；`repo_id` **不受影响**（它不看 origin） |
| 改 `repo_id` 算法 | **所有已存在的 `repo_id`** 都会失配 → 必须配一次迁移，且**不允许**回写历史 id（D3 的教训，见 08 §3） |
| 改 `upsert` 的规则 1 | D1 的"写后永不更新"会破 → `test_rebind_*` + `test_each_half_of_u9_alone_*` 会亮 |
| 给 `Repo` 加字段 | `storage/db.py` 的 DDL（`_MIGRATIONS` 追加一版）+ `api/repository.py:_split` 的帧字段 |
| 改 Bot 名字白名单 | `storage/paths.py:bot_memory_dir` 的路径安全（D3）+ `tests/test_bot_charter.py` + 06 §2.2 |
| 在本层 import `gitops` / `storage` | **禁止**：会把它从"无 IO 的纯函数层"变成"有依赖的层"，分层表也要改 |

## 测试

| 测试 | 守什么 |
|---|---|
| `tests/test_bind.py::test_repo_id_normalisation_edge_cases` | `normalize_origin` 的每一条规则 |
| `tests/test_bind.py::test_https_and_ssh_origins_share_the_same_repo_id` | D2：https/ssh → `origin_hash` 相同、id 不同（真实 git） |
| `tests/test_bind.py::test_rebinding_after_origin_added_updates_the_row` | D1：加 origin 后 id **不变**、旧 id 仍可取（验收 2/3） |
| `tests/test_bind.py::test_double_bind_is_idempotent_row_and_id` | 幂等：两次 bind 行/id 都不变 |
| `tests/test_bind.py::test_origin_hash_tracks_origin_changes_while_repo_id_does_not` | D2 的分工（可变 vs 不可变） |
| `tests/test_bind.py::test_repo_id_without_origin_is_derived_from_the_absolute_path` | id 来自路径 |
| `tests/test_bind.py::test_absolute_path_hash_is_stable_across_calls` | 纯函数稳定性 |
| `tests/test_reverse_checks.py`（`REPO_ID_LENGTH→16` / 去掉归一化 / U9 组合注入） | 改这些会让上面的测试红（反向锁） |
| `tests/test_bot_charter.py::test_bot_name_whitelist` | S6 D11 #11：`../x` / `Builder` / `a_b` / 33 字符被拒 |
| `tests/test_dispatch.py::test_illegal_transitions_are_refused` | S10 D6：`queued→succeeded` 等非法对被 `TaskStore.transition` 拒绝（事实源在本模块的表） |
| `tests/test_dispatch.py::test_l0_bot_refused_before_any_execution` | S10 D4：权限判定先于建行/起线程（`permission_covers` 的消费方） |

### Candidate 与分支名派生（S11 D1）

`candidate_branch()` 是纯函数：`bot/{bot_name}/{task_id[-8:].lower()}`，非法字符换 `-`。
**用随机尾部而不是前缀**是实测返工：ULID 前 40 位全是时间戳，同毫秒两个任务前缀必然相同
（`bot/builder/01m8e3ey` 撞车，见 S11 spec §8）；尾部 8 位来自 80 位随机段。
`Candidate` 实体（12 字段，06 §2.4）与 `REVIEW_STATES`（06 §3.2，S11 只生产 `local_only`）
都在这个无 IO 层，能被一行断言钉死。


### S13 审核状态机

`REVIEW_TRANSITIONS` + `review_transition_ok()` 是 06 §3.2 的唯一事实源（纯函数，fail closed）。
`approved` / `rejected` 为终态；`rejected → approved` 与任何 `→ merged` 都不允许。
`REASON_MAX_BYTES = 2000`。`Candidate` 加 `reject_reason` 字段（缺省 None）。
