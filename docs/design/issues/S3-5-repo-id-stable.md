# S3.5 · `repo_id` 稳定化（S3 遗留补丁）

- **里程碑** M1 · **预计** 1.5h · **依赖** S3 · **优先级** P0（S4 的硬前置）
- **用户路径**：给已绑定的仓库加一个 remote → 再绑一次 → **仓库还是那个仓库**（Bot、记忆、分支都还在）

## 背景

S3 审核实测发现的真实缺陷：`repo_id = sha256(归一化 origin URL)[:12]`，无 origin 时用绝对路径。
所以**用户加一个 remote，仓库身份就变了**。

实测（S3 审核，真实 git 仓库）：

```text
绑定无 origin 的仓库:      repoId = 77347b364a59
git remote add origin ……
再绑定同一路径:            repoId = 13632b79027a     ← id 变了
按旧 id 查询:              REPO_NOT_BOUND            ← 旧身份失效
repo.list 长度:            1                         ← 没有第二行（数据没丢，但身份被改写）
```

**S3 范围内不可观测**（只有 `bind`/`list`）。**S4 起致命**：

```
用户绑定仓库 → 建 Bot builder → bot_id = <旧 id>/builder → 记忆落 ~/.postel/bots/<旧 id>/
用户加个 remote → 重绑 → repo_id 变 → Bot 找不到、记忆目录成孤儿
```

这是**数据模型层面**的问题。S3 严格照 D3 实现了，锅在设计文档。

详见 [08-repo-id-stability.md](../08-repo-id-stability.md)（决策记录）。

---

## 裁决（D1–D3）

### D1 · `repo_id` 改为**绑定时一次性计算、写后永不更新**

| 项 | 裁决 |
|---|---|
| 算法 | `repo_id = sha256(第一次绑定时 resolve 后的绝对路径)[:12]` |
| 何时算 | **仅 INSERT 时**。已存在的行**永不**改写 `repo_id` |
| 稳定性 | 加/改/删 remote、换 HEAD、切分支、fetch —— **都不影响它** |

**为什么不随机（B2）**：保留"从路径能算出来"的可重建性（06 §5：DB 丢失后可从文件系统重建索引）。
可解释、可复现、便于排障。代价是"仓库被移动到新路径后 id 会变"——但这比"加个 remote 就变"
罕见得多，且移动仓库本来就需要人确认（S19 的 doctor 会做）。

### D2 · origin 哈希降级为 `origin_hash`，只用于识别，不参与主键

新增列 `origin_hash TEXT`，存 `sha256(归一化 origin URL)[:12]`（复用现有 `normalize_origin`）。
用途：将来提示"你绑的这两个路径其实是同一个远端仓库"（去重提示），**不参与任何主键或外键**。

06 §2.1 的「不变量」必须改写：

> **原文**：`repo_id` 对同一仓库的 https / ssh 两种 URL 必须相同（归一化后再哈希）。
>
> **改为**：
> 1. `repo_id` 一经写入**永不改变**（加/改/删 remote 不影响）。
> 2. 同一仓库的 https / ssh 两种 URL 归一化后**在 `origin_hash` 上相同**，用来识别"同一远端"。
> 3. `repo_id` 是一次性的路径哈希，算法见上。

### D3 · 迁移：schema v1 → v2，**不重写任何已有 `repo_id`**

```sql
ALTER TABLE repos ADD COLUMN origin_hash TEXT;
-- 已有行的 repo_id 保持原值（此时尚无 Bot，改写无收益只有风险）
PRAGMA user_version = 2;
```

**理由**：S3 刚合并且只有 `bind`/`list` 在用，`bots`/`tasks` 表都还不存在（S6 起）。
现在改写历史 id 没有任何收益，只会引入迁移 bug 的风险。宁可让"已经绑过的少数仓库"
保留旧 id（它们的数据是自洽的），新绑定走新算法。

**已知留痕**：迁移后，一个在 v1 时代绑定、且有 origin 的仓库，其 `repo_id` 是**URL 哈希**
而不是路径哈希。这不影响稳定性（它已经稳定了，不会再变）。**不要**为了"统一"去重算它。

---

## 验收标准（可操作）

```bash
POSTEL_HOME=/tmp/s35 uv run postel-studio serve
```

1. **绑定无 origin 的仓库 → 记下 `repoId`。**
2. **`git -C <repo> remote add origin https://github.com/o/p.git`，再绑同一路径** →
   - `repoId` **不变**（与步骤 1 相同）
   - `originUrl` 更新为 `https://github.com/o/p.git`
3. **按步骤 1 记下的旧 `repoId` 取这一行** → 能取到（不再 `REPO_NOT_BOUND`）。
   本刀没有"按 id 查询"的帧方法（`repo.list` 不收参数），所以这一条在
   `RepositoryService.get({repoId})` / `RepoStore.get(repo_id)` 这一层断言——
   它正是 S4 的 `repo.branches(repoId)` 会走的入口。
4. **同一仓库的 https / ssh 两种 origin**（两个不同路径的真仓库，各自加一种 remote）：
   - 两者 `repoId` **不同**（因为 id 只看路径）
   - 但 `origin_hash` 相同 —— 若帧层暂不暴露该字段，则在 store 层断言
5. **v1 库升级到 v2**：现有 `state.db`（`user_version=1`）打开后
   - `user_version == 2`
   - 所有 `repo_id` **一个都没变**（升级前后逐一比对）
6. **迁移可重入**：连续打开两次，第二次不做任何改动（`user_version` 已是最新，`repos` 行不变）。

## 涉及文件（预计 6 个）

| 文件 | 改动 |
|---|---|
| `src/postel_studio/domain/models.py` | `repo_id()` 改为只用路径；新增 `origin_hash()`；`repo_id_for_origin` 保留但改用途 |
| `src/postel_studio/storage/db.py` | schema v2 迁移；`upsert` 规则 1 **不再更新 `repo_id`**；读写了 `origin_hash` |
| `src/postel_studio/api/repository.py` | 构造 `Repo` 时按新算法算 id |
| `docs/design/06-data-model.md` | §2.1 字段表 + 不变量改写 |
| `tests/test_bind.py` | 改掉基于旧算法的断言；新增上面 6 条验收的用例 |
| `docs/codemap/*` | `storage-db.md` 的 schema、`domain.md` 的符号表、U9 结案 |

## 测试要求

- **迁移必须有真库测试**：先用 v1 的 SQL 建库塞两行，再 `migrate()`，断言 id 未变、`user_version=2`。
  **不得**只测"新库建出来是 v2"——那测不到迁移。
- 反向验证（进 `tests/test_reverse_checks.py`）：
  - 把 `upsert` 改回"更新 `repo_id`" → 验收 2 的用例必须红
  - 把 `repo_id()` 改回"有 origin 用 URL 哈希" → 验收 2/3 必须红
- 幂等：连续两次 `bind` 同一路径，行数与 id 都不变。

## 禁止事项

- **不得**重写已有 `repo_id`（D3）。
- **不得**新造错误码。
- **不得**改 `repo_id` 长度（仍 12 位 hex，07 §4.1 的示例即 12 位）。
- **不得**顺手做 S4 的分支列表。
- **不得**引入新依赖。

---

## 完成定义（DoD）

- [ ] 上面 6 条验收在真实内核上逐条做到
- [ ] 测试要求全部实现，含迁移真库测试
- [ ] 两条反向验证进 `test_reverse_checks.py`
- [ ] `bash scripts/check.sh` 全绿
- [ ] 06-data-model §2.1 改写；08 决策记录标记"已落地"
- [ ] codemap：U9 结案（标"已修 + 怎么修的"，**不要删**）；schema v2 写进 `storage-db.md`
- [ ] 05 §7 逐条答「是/不适用 + 为什么」
- [ ] diff 控制在 ≤400 行 / ≤12 文件内（这一刀是纯补丁，没有例外理由）