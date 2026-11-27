# candidates.py — 候选变更（S11，L3 纪律）

`src/postel_studio/candidates.py`（148 行）· 依赖：`gitops/`、`storage/`、`domain`、`api/frames` —— **不 import 任何同层 handler**（S11 D13，与 `orchestrator.py` 同款）。

## 为什么有这个模块

S10 把 Agent 的产出留在 linked worktree 里就结束了；S11 要把它**固化成可审查的分支 + 行**。
这件事既有 git 写（`commit_all` / `create_branch`），又有 DB 写（`CandidateStore`），还有帧载荷
（`candidate.list`）。把它塞进已经 450 行的 orchestrator 会让它变成上帝模块，所以单列一层，
由 `app.py` 装配并作为 `CandidateMaker` Protocol 注入编排器。

## 符号表

| 符号 | 行 | 职责 |
|---|---|---|
| `REPO_NOT_BOUND` | 35 | 稳定码（07 §1.3 既有码），不 import `api/repository.py` 拿常量 |
| `CandidateMaker` | 38 | orchestrator 看到的接缝：`snapshot_base` / `emerge` / `discard`（测试用假实现） |
| `CandidateService` | 50 | 生产实现：持有 `RepoStore` 与 `CandidateStore` |
| `snapshot_base(worktree_path)` | 58 | **会话之前**读工作区 HEAD（D2：复用工作区时上一任务候选的 commit） |
| `emerge(task, repo_id, worktree_path, base_sha)` | 62 | 有改动才固化：脏 → `git add -A` + 内核身份 commit → 候选分支 → 落一行；无改动回 `None` |
| `discard(candidate_id)` | 105 | transition 输掉 cancel 竞态时，删掉本 worker 刚插入的那一行（D6） |
| `list(params)` | 110 | `candidate.list`：`repoId` 必需，`taskId` / `reviewState` 可选；repoId 经 tasks→bots join |
| `_payload(candidate)` | 131 | `Candidate` → camelCase 帧载荷；`botName` 从派生分支名拆出，不冗余落库 |

## 三条实测裁决

1. **base 是任务开始时的 HEAD，不是 `baseRef`**（D2）。S7 D3/D5 禁止 reset 复用工作区；
   第二个任务必须从上一候选的 commit 起步，否则 diff 会把上一任务的文件也算进来。
   实测（规格 §8）：第二个任务的 `base...head` 只有 `second.txt`。
2. **分支名用 ULID 随机尾部 8 位，不是前缀**（D1）。实测两个同毫秒任务的前缀都是
   `01m8e3ey`——ULID 前 40 位全是时间戳，第二个任务直接撞"分支已存在"。
3. **"无事可做"有三道守卫**（D3）：commit 前 `not dirty and head == base`、commit 后
   `head == base`、以及 `diff_stat.files_changed == 0`。反向注入 S11 #3 因此是**组合项**，
   三道一起拆才会让目标测试变红（T45 的同一教训）。

## 修改须知

| 如果你要… | 还波及 |
|---|---|
| 改候选分支命名 | `domain/models.py:candidate_branch()` + 06 §2.4 + 反向注入 S11 #1 |
| 改 git 写操作 | `gitops/worktrees.py` 的 S11 D5 允许清单；**禁止** `--force` / `reset --hard` / `clean` / 删分支 |
| 改候选载荷 | 07 §2.5 + 03-ui-spec §4.6 + `assets.py` 的 `trackCandidate` |
| 改 `candidate.list` 的过滤 | `CandidateStore.list_for_repo` 的 join + 订单页 S12 |
| 在本模块 import `web` / 同层 handler | **禁止**（`scripts/gate-layers.py` 已登记） |


### S12 复用（无改动）

S12 的列表/筛选/倒序全部复用 S11 已有的 `CandidateService.list` 与
`CandidateStore.list_for_repo`（`reviewState` 过滤 + `created_at DESC` + `limit=100`）。
本刀没有给候选服务加方法、没有改载荷——S11 issue 里预告的「app.py 支持 reviewState」
其实在 S11 就已兑现，S12 只消费它。
