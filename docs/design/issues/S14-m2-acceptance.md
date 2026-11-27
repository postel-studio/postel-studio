# S14 · M2 验收：派活到批准的完整路径

- **里程碑** M2 · **预计** 4h · **依赖** S10,S11,S12,S13 · **优先级** P1
- **用户路径**：一条命令跑完 → 输出各步结果 → 全程真实 Agent、真实仓库

## 背景

前面几刀都是分开验证的。这一刀证明它们**串起来**能用。它也是 M2 的唯一验收口径。

## 验收标准

1. `uv run python scripts/accept_m2.py` 完成：建临时仓库 → 绑定 → 建 Bot → 分配工作区 → 检测 Agent → 派活 → 等完成 → 候选出现 → 查 diff → 批准。
2. 每步打印 `✓ 步骤名 (耗时)`，任一步失败非零退出并指明失败步。
3. 全程使用临时 `POSTEL_HOME` 与临时仓库，**不碰用户真实数据**。
4. 结束后清理：worktree 回收、临时目录删除、无残留进程。
5. `opencode` 未安装时明确 skip 并给出提示，**不伪装成功**。

## 1. Decisions

- **D1 — in-process WS against a real kernel subprocess.** Like `accept_m1.py`, the script
  starts the real `postel-studio serve --port 0` child and drives it over one real
  WebSocket (ticket from `/`), so the ten steps exercise the same `dispatch()` path the
  browser uses. No direct store/service calls, no mock: step 7 reads `candidate.list`
  through the frame, step 10 calls `candidate.approve` through the frame.
- **D2 — output contract: `✓ <step> (<seconds>s)` per step; `M2 OK (10/10 steps)` on
  success; `M2 FAILED at <step>: <reason>` plus a non-zero exit on the first failure.**
  Unlike `accept_m1.py` (which continues to collect all failures), M2 stops at the first
  failed step: later steps depend on earlier ids (`repoId` → `botId` → `taskId` →
  `candidateId`), so continuing would only emit misleading cascades. The literal
  `✓ `, `M2 OK (`, `M2 FAILED at `, `SKIP M2:` and `清理：` lines are parsed by
  `tests/test_accept_m2.py`.
- **D3 — real Agent with a real credential, via a read-only snapshot.** The host's
  opencode credential DB is the only source of a working paid provider (the free models
  are quota-exhausted — U21, closed in S13). To honour "不碰用户真实数据", the script
  takes a **SQLite backup snapshot** (`sqlite3.Connection.backup`, source opened
  `mode=ro`) into the temp work dir, sets `XDG_DATA_HOME` to that temp snapshot for the
  kernel child, and selects `deepseek/deepseek-flash` with `OPENCODE_CONFIG_CONTENT`.
  opencode therefore reads the key from its own DB in the temp dir and writes only
  there; the script never reads the key value (it checks only
  `integration_id='deepseek' AND active=1`). No credential is written into source, env
  names visible to `child_env` contain no `TOKEN/KEY/AUTH` marker, and
  `POSTEL_HOME`/`HOME` still point at the temp dir.
- **D4 — skip only on a missing prerequisite, never on a failure.** `SKIP M2: opencode
  not installed` (`shutil.which("opencode") is None`) or `SKIP M2: no deepseek credential
  configured` (snapshot has no active deepseek row). Exit 0 with the SKIP line so a
  machine without either prerequisite is not painted green but is not a failure either.
  Any other exception → `M2 FAILED at <step>` + exit 1. This is the same "explicit skip,
  never fake green" rule as 禁止事项 #2.
- **D5 — step 9 (diff) is a real `repo.diff` over the candidate branch**, not a field
  copied from the candidate payload: `repo.diff({repoId, base: baseSha, head: headSha})`
  and assert `stat.filesChanged >= 1` and a non-empty patch. This proves the candidate
  branch actually contains the Agent's file, not just a DB row.
- **D6 — cleanup idempotent and verified.** `finally` releases the worktree via
  `worktree.release` (best effort), stops the kernel (SIGTERM → SIGKILL after 15s),
  removes the temp dir, then verifies no `postel-studio serve` child survives and the
  work dir is gone. A cleanup failure is a `M2 FAILED at 清理` (never silently hidden).
- **D7 — no new `src/` line, no new U.** This slice adds only `scripts/accept_m2.py`
  and `tests/test_accept_m2.py` (plus docs). `src/` budget: **0 files / 0 lines**.
  U21 is already closed (S13); U4 is adjudicated in §5.

## 2. Acceptance → test map

| # | Acceptance criterion | Enforced by |
|---|---|---|
| 1 | Ten steps run end to end | `scripts/accept_m2.py` itself (each step asserts its own result frame) + `tests/test_accept_m2.py::test_accept_m2_script_runs_to_green` (subprocess, exit 0, ten `✓`) |
| 2 | Per-step timing + failure names the step / non-zero exit | `test_accept_m2_step_failure_names_step_and_exits_nonzero` (bad `repoId` injected via env, assert `M2 FAILED at <step>` + rc 1) |
| 3 | Temp `POSTEL_HOME` + temp repo, no real data touched | script's snapshot design (D3) + `test_accept_m2_never_touches_real_home` (assert no write to real `~/.postel`; snapshot db lives under the temp work dir) |
| 4 | Cleanup: worktree released, temp dir gone, no residual process | `test_accept_m2_script_runs_to_green` parses `清理：…临时目录已删除` and asserts the printed work dir does not exist |
| 5 | opencode absent → explicit SKIP, no fake green | `test_accept_m2_skips_when_agent_unavailable` (opencode hidden via PATH, assert `SKIP M2:` + exit 0, no `M2 OK`) |
| 真实集成 | candidate generated and diff non-zero | script step 7/9 assertions (`filesChanged >= 1`) |
| 真实集成 | approved → `review_state == approved` | script step 10 re-reads `candidate.list` and asserts `reviewState == "approved"` |

## 3. Boundary matrix

| Entity | States covered |
|---|---|
| Prerequisite | opencode installed / absent (SKIP); deepseek credential present / absent (SKIP) |
| Repo | temp git repo with one `main` commit; never the user's real repo |
| Task | `queued → running → succeeded` polled via `task.status`; any terminal non-`succeeded` fails immediately |
| Candidate | created only when Agent touched disk; `local_only` → approved via `candidate.approve`; re-read to prove the transition |
| Diff | candidate branch `baseSha...headSha` non-empty; step 9 never skipped |
| Cleanup | normal path / child still alive at SIGTERM deadline / fail path still cleans |

## 4. Executable evidence (run before the first commit, §1.1)

```text
$ cd <worktree> && uv run python /tmp/probe_m2.py      # sqlite backup snapshot + deepseek
snapshot credentials deepseek: 1
prompt 1.9s stop=end_turn, hello=True
real db mtime unchanged-ish; snapshot wrote only in /tmp/probe-m2-9v_9brqx
```

This proves D3: a read-only SQLite snapshot of the real credential DB is enough for
opencode to run a real paid-model prompt inside a fully temporary HOME, and the real
DB is not written. (The probe is a scratch script, not shipped; the shipped script
performs the same `sqlite3.Connection.backup` + `mode=ro` source open.)

## 5. Prior U items due at S14

- **U4** (`localhost:` vs `127.0.0.1:` page open): M2's canonical acceptance drives
  `127.0.0.1` only (that is where the kernel binds and where M1 / all browser scripts
  already run). This slice does not widen the test to `localhost:`. U4 stays open,
  due **M3 acceptance (S20)** as originally registered; no new decision needed here
  beyond recording that M2's canonical acceptance uses `127.0.0.1`.

## 6. Out of scope

push / open_pr / true server-side permission (S16); UI changes; new protocol methods
or error codes; new dependencies (`httpx` / `websockets` are already used by
`accept_m1.py`). Both `conftest.py` and CI are untouched.

## 涉及文件

- `scripts/accept_m2.py`（新建）
- `tests/test_accept_m2.py`（新建）
- 本文（规格扩充）

## 测试要求

- 真实集成：断言候选生成且 `diff_stat` 非零。
- 断言批准后 `review_state == approved`。
- 断言清理生效（无残留 worktree 与进程）。

## 禁止事项

- 不得在用户真实仓库上跑（必须先临时创建）。
- 不得在未装 Agent 时输出「通过」。
- 不得跳过 diff 查看步骤。
- 不得把脚本写成只打印「通过」而不实际校验。
