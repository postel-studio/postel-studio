# 决策记录：`repo_id` 的稳定性（S3 遗留，S4 前必须落地）

> 状态：**已裁决，待实施**（S3 审核时发现，S3 本身不修）
> 上游：[06-data-model.md](06-data-model.md) §2.1、[07-api-contract.md](07-api-contract.md) §4.1
> 来源：[issues/S3-bind-repo.md](issues/S3-bind-repo.md) D3、codemap 06-pitfalls U9

---

## 1. 问题

06 §2.1 规定 `repo_id = sha256(归一化 origin URL)[:12]`，**无 origin 时用绝对路径**。

这让 `repo_id` 依赖一个**用户随时会改的东西**。S3 审核实测（真实 git 仓库）：

```text
绑定无 origin 的仓库:            repoId = 77347b364a59, originUrl = None
git remote add origin https://github.com/o/p.git
再绑定同一路径:                  repoId = 13632b79027a, originUrl = https://github.com/o/p.git

旧 id 查询 → REPO_NOT_BOUND
repo.list 长度 → 1（没有产生第二行）
```

**后果**：

| 表面 | 实际 |
|---|---|
| 列表里仍是一行 | 行被**改写**了，不是"这个是同一个" |
| 用户看不出变化 | 任何按旧 `repoId` 发出的请求都变 `REPO_NOT_BOUND` |
| S3 本身无碍（只有 bind/list） | **S4 起致命**：分支列表、Bot（`bot_id = {repo_id}/{name}`）、
候选变更都挂在 `repo_id` 上 |

具体坏法（S4 之后的真实后果）：

1. 用户绑了仓库 → 创建了 Bot `builder`（`bot_id = <旧id>/builder`）。
2. 用户给仓库加了个 remote（很常见）。
3. 重绑 → `repo_id` 变 → **Bot 不见了**，记忆目录 `~/.postel/bots/<旧id>/` 成孤儿。
4. 06 §5 的"DB 丢失可从文件系统重建"也没法工作：重建时算出的 id 与旧数据对不上。

**这是数据模型层面的问题，不是 S3 的实现 bug。** S3 的实现严格照 D3 做了，所以问题在设计文档。

---

## 2. 两条路

### A · 迁移：启动时重算并改写

启动时扫 `repos` 表，对每个有 origin 的行按 URL 重算 id，若不同则改写 `repo_id`，
并级联改 `bots`/`tasks`/`candidates` 的外键。

| 优点 | 缺点 |
|---|---|
| 与 06 现有规定一致，不用改设计文档 | **id 仍然不稳定**：再加一个 remote，又变一次 |
| 实施小 | 每次启动都要扫表 + 可能的多表级联改写 |
| | `bots/` 记忆目录要跟着改名（文件系统 + DB 两处事务） |
| | 外键级联要 `PRAGMA foreign_keys` 开着做，做错就是数据损坏 |

**根本问题没解决**：它把一个"不确定性"变成了"每次启动都重新赌一次"。

### B · 拆开：稳定 id + 展示字段（**裁决采用**）

`repo_id` 改为**绑定时生成一次、此后永不变**的标识；origin URL 哈希只作为**展示/去重**用途。

| 字段 | 作用 | 稳定性 |
|---|---|---|
| `repo_id` | 主键，所有外键引用它 | **永不变** |
| `origin_url` | 展示（06 §2.1 本来就有） | 可变 |
| `origin_hash`（新增） | 归一化 URL 哈希，用于**同一仓库换路径时认出来** | 可变 |

**id 怎么生成**（两种都可，倾向 B1）：

- **B1 · 路径哈希，只在插入时算一次**：`sha256(第一次绑定的绝对路径)[:12]`。
  - 优点：可预测、可从路径推断、与"无 origin 时用路径"的现有规则连续。
  - 缺点：**换路径后 id 变**（仓库被移动）。但"仓库被移动"比"加 remote"罕见得多，
    且移动后本来就需要人确认。
- **B2 · 随机 12 位**：`secrets.token_hex(6)`。永不受任何外部状态影响，
  但完全不可推断，跨机器/跨备份恢复时无法对齐。

**裁决采用 B1**：`repo_id = sha256(首次绑定的绝对路径)[:12]`，写入后**永不更新**。
理由：可变频率最低（移动仓库 << 加 remote）、可解释、且保留了"从路径能算出来"的可重建性
（06 §5 的重建场景仍然成立）。

### 06 §2.1 的「不变量」需要改写

原文：

> **不变量**：`repo_id` 对同一仓库的 https / ssh 两种 URL 必须相同（归一化后再哈希）。

**改为**：

> **不变量**：
> 1. `repo_id` 一经写入**永不改变**（加/改/删 remote、换分支、换 HEAD 都不影响它）。
> 2. 同一仓库的 https / ssh 两种 URL 归一化后**哈希相同**——但它记录在 `origin_hash`，
>    用途是**识别"这两个绑定是不是同一个远端仓库"**（例如将来做去重提示），不参与主键。
> 3. `repo_id` 由**绑定时的一次性计算**得到，算法见下。

### 迁移（schema v1 → v2）

S3 已发布 v1（`user_version = 1`）。改造要一次迁移，06 §7 说"只增不减、可重入"：

```sql
-- v2：新增 origin_hash；repo_id 仍是 PK，不改值
ALTER TABLE repos ADD COLUMN origin_hash TEXT;
UPDATE repos SET origin_hash = <按现有 origin_url 归一化后哈希> WHERE origin_url IS NOT NULL;
PRAGMA user_version = 2;
```

**关键**：**不重写已有 `repo_id`**。已经绑过的仓库保持原 id（多数用户此时只绑过一两个仓库，
且尚未创建 Bot），新的绑定才走新算法。这避免了迁移期的一切数据损坏风险。

> 若用户已经在 S3 之后、v2 之前创建过 Bot（实际不可能，S6 才做），才需要真正的 id 改写。
> 到 v2 时这一步不存在，所以迁移是安全的。

---

## 3. 实施位置

| 改什么 | 文件 |
|---|---|
| `repo_id()` 改为只算路径哈希 | `src/postel_studio/domain/models.py` |
| 新增 `origin_hash()` | 同上 |
| `upsert` 改成"已存在则不覆盖 repo_id" | `src/postel_studio/storage/db.py` |
| schema v2 + 迁移 | 同上 |
| 06 §2.1 字段表 + 不变量改写 | `docs/design/06-data-model.md` |
| 07 §4.1 示例（`repoId` 语义不变，但要说明它稳定） | `docs/design/07-api-contract.md` |
| 测试：加 remote 后 id 不变、旧 id 仍可查 | `tests/test_bind.py` |

**属于哪一刀**：建议作为 S3 的一个小补丁（S3.5）单独提 PR，**不要**塞进 S4。
理由是 S4 会依赖 `repo_id` 的语义，先把它钉死再做分支列表，S4 的审查会简单很多。

---

## 4. 验收（S3.5 的验收标准）

1. 绑定无 origin 的仓库 → `repo_id` 稳定。
2. `git remote add origin <url>` → **重绑** → `repo_id` **不变**，`originUrl` 更新为新值。
3. 按旧 `repo_id` 查询 → **仍能查到**（不再是 `REPO_NOT_BOUND`）。
4. 同一仓库的 https / ssh 两种 origin → `origin_hash` 相同，`repo_id` 相同（因为 id 只看路径）。
5. 已有 v1 数据库升级到 v2：`repo_id` 一个都没变，`user_version = 2`。
6. 迁移可重入：连续启动两次，第二次不做任何改动。
7. 反向验证：把"已存在不覆盖 repo_id"删掉 → 用例 2/3 变红。

---

## 5. 为什么不现在就在 S3 里修

S3 的验收标准（1–5）已经全部满足，`repo_id` 变化**在 S3 的能力范围内不可观测**——S3 只有
`repo.bind` / `repo.list`，没有按 id 查询的用户路径。把它塞进 S3 会让本已超预算的 diff
再多一整套 schema 迁移，且 S3 的测试重心会从"绑仓库"漂移到"数据模型迁移"。

**但它是硬阻塞**：S4 只要有 `repo.branches(repoId)`，这个问题就变成用户可见的失败。所以
**S4 的规格里必须先引用本文**。