# 06 · 数据模型

> 状态：定稿 v1 · 2026-11-26
> 上游：[01-requirements.md](01-requirements.md)、[02-architecture.md](02-architecture.md) §7
> 实现细节（表名、索引）以本文为准；SQLite 是**索引**，不是事实源——真正的文件在文件系统里。

---

## 1. 实体总览

```
Repo ──1:N── Bot ──1:N── Task ──0..1── Candidate
  │
  └──1:N── WorktreeLease
```

| 实体 | 落盘位置 | 生命周期 |
|---|---|---|
| Repo | `state.db`（绑定关系） | 人手动解绑 |
| Bot | `state.db`（元数据）+ `~/.postel/bots/`（记忆） | 人手动删除 |
| Task | `state.db` | 永久保留（可归档） |
| Candidate | `state.db`（元数据）+ Git 分支（实体） | 人拒绝后保留，不删分支 |
| WorktreeLease | `state.db`（租约）+ `~/.postel/worktrees/`（目录） | 任务结束回收 |

**核心原则**：数据库只存**能重建的索引**。若 `state.db` 丢失，扫描 `~/.postel/worktrees/` 与 Git 分支应能重建大部分状态。

---

## 2. 实体定义

### 2.1 Repo

| 字段 | 类型 | 约束 | 说明 |
|---|---|---|---|
| `repo_id` | TEXT PK | 12 位十六进制 | `sha256(第一次绑定时 resolve 后的绝对路径)[:12]`，**写后永不更新**（S3.5 D1） |
| `path` | TEXT UNIQUE | 绝对路径，已 `resolve()` | 本地仓库根 |
| `origin_url` | TEXT NULL | — | 归一化前的原始值（仅展示用，可变） |
| `origin_hash` | TEXT NULL | — | `sha256(归一化 origin URL)[:12]`；**只用于识别"同一远端"**，不参与主键/外键（S3.5 D2） |
| `default_branch` | TEXT NULL | — | 探测结果 |
| `added_at` | REAL | — | Unix 时间戳 |

**不变量**（S3.5 改写，见 [08-repo-id-stability.md](08-repo-id-stability.md)）：

1. `repo_id` 一经写入**永不改变**（加/改/删 remote、换分支、换 HEAD、fetch 都不影响它）。
2. 同一仓库的 https / ssh 两种 URL 归一化后**哈希相同**——但它记录在 `origin_hash`，
   用途是**识别"这两个绑定是不是同一个远端仓库"**（例如将来做去重提示），不参与主键。
3. `repo_id` 由**绑定时的一次性计算**得到，算法见第 1 行；只有仓库被移动到新路径时才会变。

### 2.2 Bot

| 字段 | 类型 | 约束 | 说明 |
|---|---|---|---|
| `bot_id` | TEXT PK | `{repo_id}/{name}` | 复合主键，天然按仓库隔离 |
| `repo_id` | TEXT FK → Repo | ON DELETE CASCADE | |
| `name` | TEXT | `^[a-z][a-z0-9-]{0,31}$` | 白名单校验（见 05 §3.3） |
| `permission` | TEXT | `L0` / `L1` / `L2` / `L2+` 之一 | **默认 L1**（05 §2.2）。L3/L4 是**认识但不可写入**的值（`NOT_SUPPORTED`），不在 `L0<L1<L2<L2+` 这根梯子上（01 §4；S8 D2） |
| `agent_kind` | TEXT | 注册表中的通道 id | 不含单个 Agent 特判 |
| `charter_rev` | INTEGER | 单调递增 | charter 内容哈希的版本号，用于注入缓存失效 |
| `created_at` | REAL | | |

**不变量**：

- 同一 `repo_id` 下 `name` 唯一。
- **charter 正文在文件、不落库**（S6 D5）：`<状态根>/bots/<repo_id>/<name>/charter.md` 是内容的事实源；`charter_rev` 列 v3 起存在，**S8 起在创建时写一次**（那一刻的内容哈希），之后不维护——改 charter 的写路径是 S22 的 `charter.set`（06 U17 的第二半）。
- `permission` 变更只能由人发起（Agent 无写权限）。
- Bot 删除时**不删**记忆目录（保留可审计；由显式清理命令处理）。

### 2.3 Task

| 字段 | 类型 | 约束 | 说明 |
|---|---|---|---|
| `task_id` | TEXT PK | ULID | 可排序、无碰撞 |
| `bot_id` | TEXT FK → Bot | ON DELETE CASCADE | |
| `trigger` | TEXT | `manual` / `dialogue` / `local_event` | 主动性来源（用于去重与优先级） |
| `trigger_key` | TEXT NULL | 幂等键 | 同一事件的重复触发在此去重 |
| `prompt` | TEXT | ≤ 32 KiB | 派活指令 |
| `base_ref` | TEXT NULL | 分支白名单 | 缺省用 `default_branch` |
| `state` | TEXT | 状态机（见 §3） | |
| `candidate_id` | TEXT NULL FK → Candidate | 0..1 | **成功且产生改动**时才有 |
| `error_code` | TEXT NULL | 稳定错误码 | 失败原因 |
| `created_at` / `started_at` / `ended_at` | REAL | 单调 | 用于排队时长统计 |

**不变量**：

- `state = succeeded` **不代表**有改动——可能 `candidate_id IS NULL`（无事可做）。
- `state = failed` 必须有 `error_code`。
- `trigger_key` 在 `(bot_id, trigger, trigger_key)` 上唯一（同一事件不重复派活）。

> **S10 落地注记**：本表与 `worktree_leases` 在 schema v4 一次建齐（`_MIGRATIONS` append）。
> `candidate_id` **先建列、恒 NULL**（S11 才有值）——现在占住，S11 不用再迁移。
> 两个部分唯一索引落了数据级锁：`(bot_id) WHERE state IN ('queued','running')` 唯一
> （**BUSY 的兜底**——orchestrator 的「检查-再插入」竞态由它收口，D5）；
> `(bot_id, trigger, trigger_key) WHERE trigger_key IS NOT NULL`（06 §6 的部分索引口径）。
> 状态机的唯一事实源是 `domain/models.py` 的 `TASK_TRANSITIONS`（纯函数层），
> `TaskStore.transition` 用 `UPDATE … WHERE state=<from>` 原子迁移，竞态输家拿 `None`。

### 2.4 Candidate

| 字段 | 类型 | 约束 | 说明 |
|---|---|---|---|
| `candidate_id` | TEXT PK | `cand_` + ULID | |
| `task_id` | TEXT FK → Task | UNIQUE | 1 Task : 0..1 Candidate |
| `branch` | TEXT | 白名单，可预测 | `bot/{bot_name}/{task_id 短}` |
| `base_sha` / `head_sha` | TEXT | 40 位 SHA | |
| `files_changed` / `insertions` / `deletions` | INTEGER | ≥ 0 | 冗余存一份，便于列表排序 |

> **命名映射**：落盘用 `snake_case`（`files_changed`），帧层用 `camelCase`（`filesChanged`）。
> 转换只在边界发生（见 [07-api-contract.md](07-api-contract.md) §4.2），内部不混用。
| `review_state` | TEXT | 状态机（见 §3） | |
| `remote` | TEXT NULL | `origin` 等 | 仅 `≥ pushed` 时有值 |
| `target` | TEXT NULL | `owner/repo#base` | `NULL` = 仅本地 |

**不变量**：

- `base_sha` 必须是 `head_sha` 的祖先（否则 diff 无意义）。
- `branch` 不以人的输入命名（防注入，见 05 §3.3）。
- 拒绝（`rejected`）**不删分支**，也不删记录。

### 2.5 WorktreeLease

> **S7 裁决（2026-11-27）：本表到期 S10，与 `Task` 一起建。** S7 的工作区（`worktree.acquire` /
> `list` / `release`）**不落库**：路径与分支可由 `git worktree list` 加目录是否存在完整重建
> （§1 的核心原则），而本表的 `task_id` 是 NOT NULL 外键、`Task` 表要到 S10 才存在。为了一个
> 3 小时切片提前建一张带哑列的表，S10 还得再迁移一次。S7 期间工作区的身份 = `(repo_id, bot_name)`
> = 目录路径 = `bot_id` 复合键，三者是同一个东西。
>
> 代价（明确记着）：S7 的 `worktree.list` 只看得见"当前这个仓库"的工作区；别的 `repo_id` 下
> 遗留的目录（例如仓库被移动、U14）不在它的视野里，由 S19 的 doctor 统一报告（**不删除**，§5）。

| 字段 | 类型 | 约束 | 说明 |
|---|---|---|---|
| `lease_id` | TEXT PK | ULID | |
| `bot_id` | TEXT FK → Bot | | 每 Bot 同时最多 1 个有效租约 |
| `task_id` | TEXT FK → Task | UNIQUE | 一个任务一个租约 |
| `path` | TEXT | 在 `worktrees_root` 内 | `resolve()` 后断言 |
| `acquired_at` / `released_at` | REAL | | `released_at IS NULL` 表示活跃 |

**不变量**：同一 `bot_id` 上 `released_at IS NULL` 的记录 ≤ 1（串行队列的实现基础）。

> **S10 落地注记**：本表已在 schema v4 建齐（与 `Task` 一起，兑现 S7 的裁决）。
> 一处与上表的偏差：建表用的列名是 **`created_at`**（与 `tasks`/`repos`/`bots` 的
> 时间列同名，读路径的 `_row_to_lease` 依赖这个统一命名），而不是 `acquired_at`——
> 语义相同（租约建立时刻）。`task_id` UNIQUE + `(bot_id) WHERE released_at IS NULL`
> 部分唯一索引照上表；**释放只置 `released_at`**，不删行（审计留痕，06 §5 精神）。
> orchestrator 在 worker 的 `finally` 里释放——`cancelled` / `failed` / `interrupted`
> 的每一条路径都必须走到。

---

## 3. 状态机

### 3.1 Task

```
queued ──start──> running ──ok────> succeeded
                      │  └─fail──> failed
                      └─cancel──-> cancelled
interrupted  ← 进程重启时，原 running 被标记（不是 succeeded！）
```

| 迁移 | 允许来源 | 副作用 |
|---|---|---|
| `queued → running` | 租约获取成功 | `started_at` |
| `queued → cancelled` | 人取消（**worker 尚未开工**，零副作用——S10 实测） | 回收排队位置 |
| `running → succeeded` | 会话结束且无异常 | 有改动则建 Candidate |
| `running → failed` | 会话失败 / 越界检查失败 | 必须写 `error_code` |
| `running → cancelled` | 人取消 | 回收租约 |
| `running → interrupted` | **内核重启** | 需人工确认是否重跑 |

**禁止**：`queued → succeeded`（跳过执行）；`interrupted → succeeded`（撒谎）。

> **S10 落地注记**：表里补上了 `queued → cancelled`（原始图漏了这条——取消排队中的任务
> 是必须支持的用户动作）。`queued → interrupted` 也实现了：重启记账（D7）把 queued 和
> running **一起**标 `interrupted`——queued 的 worker 已经不在了，留着它就是假承诺。
> `stopReason → 状态` 的映射 fail-closed：只认 `end_turn` / `cancelled`，其余全 `failed`
> + `INTERNAL`（不猜没见过的值）。

### 3.2 Candidate

```
local_only ──push──> pushed ──pr──> pr_open ──approve──> approved
     │                                  │
     └──────────reject──────────────────┴──> rejected
```

| 迁移 | 前置条件 | 说明 |
|---|---|---|
| `local_only → pushed` | 权限 ≥ L2，且 remote 可用 | 人的动作 |
| `pushed → pr_open` | 权限 ≥ L2+（**本仓库** PR） | 跨仓库 PR 不在 MVP |
| `pr_open → approved` | 人批准 | |
| 任意 → `rejected` | 人拒绝 | 不删分支 |

**不存在** `→ merged` 的迁移：产品不提供合并。

---

## 4. 关系与约束

| 关系 | 基数 | 级联 |
|---|---|---|
| Repo → Bot | 1:N | 删 Repo 级联删 Bot（**不删记忆目录**） |
| Bot → Task | 1:N | 删 Bot 级联删 Task |
| Task → Candidate | 1:0..1 | 删 Task 级联删 Candidate（**不删 Git 分支**） |
| Bot → WorktreeLease | 1:N（同时活跃 ≤1） | 删 Bot 必须释放活跃租约 |

**外键强制**：`PRAGMA foreign_keys=ON`。

---

## 5. 文件系统 ↔ 数据库的一致性

| 场景 | 处理 |
|---|---|
| DB 有 Candidate，分支已被人删除 | 启动时检测，标 `orphaned`，不自动重建 |
| 目录有 worktree，DB 无租约 | 视为孤儿，**报告不删除**（可能是人手工建的） |
| DB 丢失 | 从 `worktrees/` 与 `git branch --list 'bot/*'` 重建索引，标记为 `reconstructed` |

**原则**：**永不自动删除用户文件**。所有清理都要人确认。

---

## 6. 索引与性能

| 表 | 索引 | 用途 |
|---|---|---|
| Task | `(bot_id, state)` | 队列查询 |
| Task | `(bot_id, trigger, trigger_key)` UNIQUE | 事件去重 |
| Candidate | `(review_state, created_at DESC)` | 收件箱排序 |
| WorktreeLease | `(bot_id) WHERE released_at IS NULL` | 活跃租约唯一性 |

数据量预期：单机、百级仓库、千级任务。**不做分页索引优化**，超出再议。

---

## 7. 迁移

- `PRAGMA user_version` 作为 schema 版本。
- 每个版本一个迁移函数，**只增不减**（不删列，改为停止使用）。
- 迁移必须可重入：同一版本重复执行不改变状态。
- 迁移失败时**回滚并保留原库**（先复制备份），不得半迁移。

> **已落地的版本**：v1（S3 `repos`）→ v2（S3.5 `origin_hash` 列）→ v3（S6 `bots`）→
> **v4（S10 `tasks` + `worktree_leases`，含两个部分唯一索引）**。实现见
> `storage/db.py` 的 `_MIGRATIONS`；每版一个真库升级测试（v1→v4 全链路）。
> 注意 sqlite3 的 `Connection` 上下文管理器是**事务边界**不是连接开关：`transition`
> 忘拿连接会让 UPDATE 的写事务永不提交、堵死其它连接（T44 的实测教训）。

---

## 8. 明确不存的东西

| 不存 | 原因 |
|---|---|
| 凭据、token | 见 05 §4 |
| diff 全文 | 可由 Git 重建；存了会成为一致性负担 |
| **diff 本身（含 stat）** | 视图实体：`repo.diff` 每次按需从 git 读，**不落库、不缓存**。缓存会与仓库实时状态不一致（用户刚提交就看不到新 diff），比慢更糟（S5 D13） |
| Agent 的完整对话记录 | 属于 Agent 自己的会话文件；我们只存引用与摘要 |
| **charter 正文** | 文件是事实源（`<状态根>/bots/<repo_id>/<name>/charter.md`）；落库会与用户手改不一致（S6 D5） |
| 用户源码副本 | 只在 worktree 里，不在 DB |
| 分支列表 | **视图实体**：每次按需从 git 读（`repo.branches`），不落库。分支的真相只可能在 git 里，存一份就是第二事实源（S4 D4/D5） |

## 9. S11 落地注记（候选）

- v5 只加了 `candidates` 表：`tasks.candidate_id` 是 v4 就占好的列，
  S11 用 `TaskStore.transition(..., candidate_id=...)` 与终态一起原子写入；
  候选行先插、迁移后做，竞态输家删掉自己刚插的那行（S11 D6）。
- `branch` 的形态是 `bot/<bot_name>/<ULID 随机尾部 8 位>`（不是前缀——
  同毫秒前缀必然撞车，见 S11 spec §8 / 06-pitfalls T46）；`bot_name` 从
  bot 实体取、经过 `bot_name_ok` 白名单，分支名派生是纯函数。
- `base_sha` 是**任务开始时工作区 HEAD**（不是 `base_ref`）。S7 的"复用不重置"意味着
  第二个任务从上一候选的 commit 起步；用开始时的 HEAD 做 base 才能保证每个候选的
  `base...head` 只含本任务的改动。06 §2.4 的"base 是 head 的祖先"由构建顺序保证。
- `files_changed` / `insertions` / `deletions` 来自 `gitops.diff_stat` 的 `--numstat`，
  与 `repo.diff` 的 stat 同口径；**载荷与数据库都不存 patch**（06 §8）。


## 10. S13 落地注记（审核动作）

- v6 给 `candidates` 加 `reject_reason TEXT`；`Candidate` 实体加 `reject_reason`
  （缺省 None，兼容 S11 构造点）。理由 trim 后按 UTF-8 字节上限 2000 存储。
- `REVIEW_TRANSITIONS` 是审核迁移的唯一事实源（纯函数，fail closed）：
  `approved` / `rejected` 为终态；S13 明示 `local_only → approved` 在 MVP 合法
  （不要求先 push/PR），`rejected → approved` 与 `→ merged` 永不合法。
- 迁移与理由写入同一个 `UPDATE … WHERE review_state = <from>`（T44 的锁+连接纪律）；
  竞态输家回 `None`，不覆盖赢家理由。
- `candidate.updated`（07 §3 早已登记）在 S13 首次发出，载荷 `{candidateId, reviewState}`。
