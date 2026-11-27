# S15 · 杀掉内核再启动 → 中断的任务不撒谎

- **里程碑** M3 · **预计** 3h · **依赖** S11 · **优先级** P2
- **用户路径**：任务跑到一半杀进程 → 重启 → 中断的任务显示「已中断」并可一键重跑

## 背景

常驻进程会重启（升级、崩溃）。最危险的不是丢任务，而是把"半截任务"显示成"成功"。

## 验收标准

1. 重启后发现 `running` 状态的任务，标记为 `interrupted` 并在 UI 明示。
2. **不得**把 `interrupted` 改成 `succeeded`。
3. 已完成任务与候选不受影响。
4. 中断任务可一键重跑（新建任务，不复用旧 id）。
5. 数据库 schema 有版本号；从空库与旧版本库各迁移一次成功。

## 0. 开工前实测：后端能力已在 S10 落地

探测（`/tmp/probe_s15.py`，§6 贴原始输出）确认：**恢复与迁移的后端已经在 S10 实现并测试过**——

- `TaskStore.mark_interrupted()`：一条原子 `UPDATE … WHERE state IN ('queued','running')`（S10 D7），实测标掉 2 行、已完成任务不动；
- 状态机 `TASK_TRANSITIONS` 里 `running→interrupted` 是合法边，`TASK_TERMINAL` 含 `interrupted`，实测 `interrupted→succeeded` = `False`、`interrupted ∉ TASK_ACTIVE`；
- schema `SCHEMA_VERSION = 6`，`_MIGRATIONS` 长度与之一致；真库 v1→当前 + 可重入由 `tests/test_bind.py::test_v1_database_upgrades_to_v2_without_rewriting_any_repo_id` 覆盖（S3.5 起就是真 DDL 迁移测试）。

因此 S15 **不重写后端**。本刀补的是 UI 的"已中断"专用态与重跑入口、真重启的端到端证据、以及三个到期 U 项的裁决。符合 04 §1.1 的预算纪律（不为"看起来做了事"重写已正确的代码）。

## 1. Decisions

- **D1 — UI 状态机加 `interrupted` 专用态**（不使用现有 `error` 态）：`#task[data-state="interrupted"]` 与 `done`/`error` 并列。显示 `TASK_STATE_TEXT` 里 S10 已有的「被打断（内核重启过，确认后可重跑）」、新增 `#task-rerun` 按钮可见、取消按钮不可见、派活表单可见（用户可改写 prompt 后重跑）。理由：验收 1 要求 UI "明示"中断；泛化 `error` 会把它和 `failed`（执行出错）混为一谈——`interrupted` 是内核重启导致 worker 消失，语义不同。
- **D2 — 重跑 = `task.dispatch` 新建任务，绝不复用旧 id**：`#task-rerun` 点击后把当前 `#task-prompt` 的值（原 prompt，用户可改）交给既有 `submitTask()` 的派活路径，帧仍是 `task.dispatch({botId, prompt})`，内核生成新 ULID。禁止事项 #4「不得自动重跑」：只有用户点按钮才发帧；页面加载/收到 `interrupted` 事件时**绝不**自动 dispatch。
- **D3 — 重跑按钮只在 `interrupted` 态出现**：显隐由 CSS 的 `#task[data-state]` 决定（与 S10 卡片同一纪律 T8），JS 只改 `dataset.state`。按钮 id 加入 03 §4.6 的 DOM 契约表；零 `innerHTML`，按钮文案静态 HTML、状态行用 `textContent`。
- **D4 — 终态封死的验证进 `tests/test_recovery.py`**：断言 `interrupted→succeeded` 为 `False`、`TaskStore.transition` 拒绝该边、`mark_interrupted` 只动活跃行、已完成任务与 `candidate_id` 外键不变。这是验收 2/3 的可执行证据（后端既有行为，S15 把它钉成回归测试）。
- **D5 — 真重启端到端**：`tests/test_recovery.py` 起真内核 → 用假 ACP 命令让任务停在 `running` → SIGKILL 内核 → 重启 → `task.status` 断言 `interrupted` → 同一库里的已完成任务不变。用真 `POSTEL_HOME` 临时目录。不强依赖 opencode（假 ACP 确定性），真 Agent 路径由 S14 的 `accept_m2.py` 覆盖。
- **D6 — 迁移验收复用既有真库测试**：验收 5 的"空库→当前"由 `open_state_db` 建库断言 `user_version == SCHEMA_VERSION`；"旧版本库→当前"复用 `test_bind.py` 的 v1 真 DDL 迁移用例 + 本文件补一条可重入断言（连开两次均不报错、版本不倒退）。不复制那份 v1 DDL 常量。
- **D7 — 三个到期 U 项裁决**：U1 / U10 / U20 均到期 S15，见 §5。本刀不为它们扩范围：U1 保持"未定位根因 + 不进 quarantine"（顺延 S20）；U10 在 `server.serve()` 的 `finally` 里关闭 store（小改动，收口）；U20 明确不重构 dispatch 形状，顺延 S16（权限门同样碰网关，届时一并裁决 `asyncio.to_thread`）。理由：本刀已有一个真 UI 改动 + 真重启测试，再加事件循环重构会突破预算并扩大审查面。
- **D8 — 新增 `task.latest({botId})` 帧方法**（实现期发现的缺口，spec 明确收口）：S10 的断连纪律会清空 `currentTaskId`，所以内核重启后页面**无从知道**旧任务 id，`task.status` 查不到、`interrupted` 态永远不会在 UI 出现——验收 1「UI 明示」无法达成。解法：只读新增 `task.latest`（`TaskStore.latest_for_bot` → `Orchestrator.latest` → 网关方法表），页面在 WS `onopen`（含首次加载）时查一次；**只有 `interrupted` 才落到卡片**，其它状态不覆盖 idle/ready（不把上次 succeeded 伪装成"当前在跑"）。不改 `task.status` / `task.dispatch` 契约，不新增错误码（复用 `INVALID_PARAMS`：botId 查不到）。

## 2. Acceptance → test map

| # | Acceptance criterion | Enforced by |
|---|---|---|
| 1 | running → interrupted on restart, UI shows it | `tests/test_recovery.py::test_restart_marks_running_and_queued_interrupted` (in-process 重启记账) + `tests/test_recovery.py::test_real_kernel_restart_interrupts_half_done_task` (真内核 SIGKILL→重启) + `tests/test_recovery.py::test_task_interrupted_dom_contract` (DOM 契约) + `tools/acceptance/s15_recovery.mjs` (真浏览器) |
| 2 | interrupted never becomes succeeded | `test_recovery.py::test_interrupted_cannot_become_succeeded` (状态机 + store 双重断言) + **注册的反向注入**（`tests/test_reverse_checks.py` 的 `COMPOSITE_INJECTIONS`「S15 #2（组合）」：拆终态守卫 + 补 `interrupted→succeeded` 边 → 该测试必须红）；同文件的 `INJECTIONS`「S15 #1」把 `rerunTask()` 塞进 `onTaskUpdated`，断言 `test_no_auto_rerun` 变红（09 §2.1 的注册机制，不是仅有直接断言） |
| 3 | finished tasks & candidates untouched | `test_recovery.py::test_finished_task_and_candidate_survive_restart` |
| 4 | one-click rerun, new taskId | `test_recovery.py::test_rerun_creates_new_task_id` + `s15_recovery.mjs` (点 `#task-rerun` → 新 taskId ≠ 旧) + `test_no_auto_rerun` (静态守卫) |
| 5 | schema versioned, empty + old db migrate once | `test_recovery.py::test_empty_db_is_current_version` + `test_old_db_migration_is_reentrant` + 既有 `test_bind.py` v1 迁移 |
| DOM | 中断态与重跑按钮契约 | `tests/test_recovery.py::test_task_interrupted_dom_contract` + 浏览器脚本 |

## 3. Boundary matrix

| Entity | States covered |
|---|---|
| Task | `queued` / `running` / `succeeded` / `failed` / `interrupted`；终态封死（含 `interrupted→succeeded` 拒绝） |
| Candidate | 已完成任务带 `candidate_id`：重启后外键不变、`candidate.list` 仍查得到 |
| Task card UI | `idle` / `ready` / `running` / `done` / `error` / **`interrupted`**；重跑按钮只在 interrupted 出现；断连不自动重跑 |
| DB | 空库（user_version=当前）/ v1 真 DDL → 当前 / 重复打开不倒退 |
| Process | 真内核 SIGKILL → 重启，旧 worker 不存在、新编排器完成记账 |

## 4. Executable evidence (run before code, §1.1)

```text
$ cd <worktree> && uv run python /tmp/probe_s15.py
mark_interrupted rows: 2
states: ['interrupted', 'interrupted', 'succeeded']
succeeded unchanged: True
interrupted->succeeded legal? False
interrupted in TASK_ACTIVE? False
SCHEMA_VERSION: 6
```

（探测脚本的旧库段用了带 `origin_hash` 的错误 v1 DDL，迁移报错是脚本问题而非产品缺陷；
真正的 v1→当前迁移由 `test_bind.py::test_v1_database_upgrades_to_v2_without_rewriting_any_repo_id`
用与 `_MIGRATIONS[0]` 逐字一致的 DDL 覆盖，D6 不复制它。）

## 5. Prior U items due at S15

- **U1** — 三个 lock/启停 flake 仍未定位根因；S15 的重启测试用确定性假 ACP + 真内核，不依赖那三条路径。到期顺延 **S20（M3 验收）**，保持"未定位、不进 quarantine"的登记不变；理由：连续多轮单独复跑均通过，失败只在全量负载下偶发，本刀无法在预算内定位。
- **U10** — `state.db` 连接关闭：本刀在 `server.serve()` 的 `finally` 里关闭 store，收口 U10。
- **U20** — `dispatch()` 同步执行仍在事件循环：本刀**不重构**；到期顺延 **S16**（权限门同样碰网关 dispatch 路径，届时一并裁决 `asyncio.to_thread`），理由见 D7。

## 6. Out of scope

`push` / `open_pr`（S16）；服务端权限强制（S16）；空态/错误态文案打磨（S18）；真 Agent 全链路（S14 的 `accept_m2.py` 已覆盖）。新增依赖 / CI / `conftest.py`：不改。

## 涉及文件

- `src/postel_studio/web/assets.py`（`interrupted` 态 + `#task-rerun`）
- `src/postel_studio/api/server.py`（U10：store 关闭）
- `tests/test_recovery.py`（新建）
- `tools/acceptance/s15_recovery.mjs`（新建，真浏览器）
- 本文 + codemap / UI spec 同步

## 测试要求

- 写入 `running` → 真杀内核 → 重启 → 断言变 `interrupted`。
- 断言已完成任务与候选不受影响。
- 迁移：空库 → 当前版本；旧版本 → 当前版本，各断言成功且可重入。

## 禁止事项

- 不得把 `running` 自动改成 `succeeded`。
- 不得自动重跑（交给人决定）。
- 不得丢失候选与任务的外键关系。
