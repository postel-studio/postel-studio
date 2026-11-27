# S10 · Dispatch: one sentence in, a status light turns green, the worktree really changed

- **Milestone** M2 · **Estimate** 4h · **Depends on** S7,S8,S9 · **Priority** P0
- **User path**: select a Bot → type one sentence → click 派活 → status grey→green → the worktree contains a new file

## 1. Scope

The first slice of the core loop. Everything before this was scaffolding; this slice makes a
Bot do work. In: `task.dispatch` / `task.status` / `task.cancel`, the `tasks` and
`worktree_leases` tables (v4), a session-level ACP client, a worker-thread orchestrator, an
`evt`/`subscribe` mechanism, and the page card. Out: candidate production (S11), `task.list`
(S11 needs it for the inbox; nothing in S10 does), `task.output` streaming (07 §5 defers it;
S10 consumes agent notifications and forwards nothing), `permission.check` as a frame method
(S16 - S10 walks the *internal* path, see D4).

## 2. Decisions

- **D1 - the frame never blocks.** `task.dispatch` validates, inserts a `queued` row, starts a
  worker thread, and answers `{taskId, state: "queued", botId}`. Everything slow (worktree
  acquisition, agent process, prompt) happens in that thread. `queued` exists for the window
  between the answer and the worker's first transition; acceptance 1 wants it observable.
- **D2 - U19 closes here.** ACP calls run on the worker thread, so the handshake-blocks-the-loop
  problem disappears for dispatch. The gateway gains a per-connection event pump instead of
  growing any synchronous wait.
- **D3 - U18 closes here: default channel, no selector.** The channel is `bot.agentKind` when
  that field is set, otherwise the first registered channel (today: `acp-stdio`). `agentKind`
  stays unwritable (`NOT_SUPPORTED`, unchanged): S10 has no product decision on "how a human
  picks", and accepting page-impossible values is interface-before-user-path. What S10 pins is
  the *default*, which is all dispatch needs. Multi-channel selection returns when a second
  channel exists.
- **D4 - the permission path is walked, not skipped.** After param validation and before any
  git call or process spawn: `permission_covers(bot.permission, L1)` - a task writes inside a
  worktree, which is exactly L1's grant. L0 bot → `PERMISSION_DENIED`. 05 §2.1: check before
  execution, and there is no second path around it (the worker checks nothing again - it is
  started only by a dispatch that already passed).
- **D5 - DB v4 builds both tables at once.** `tasks` (06 §2.3) and `worktree_leases` (06 §2.5)
  in one migration, as the S7 ruling requires. Lease row is created at `queued→running` and
  `released_at` set when the task reaches any terminal state; the worktree itself is **not**
  removed (acceptance 3 needs the changes visible). `BUSY` is answered by an active-task check
  (`state IN ('queued','running')`); the backstop is a partial unique index on `tasks`
  (`UNIQUE(bot_id) WHERE state IN ('queued','running')`) alongside the lease index from 06 -
  the check-then-insert race gets a second, data-level lock.
- **D6 - the state machine is enforced where the rows live.** Transitions go through
  `TaskStore.transition(task_id, from_state, to_state, ...)`, which validates against 06 §3.1
  and does an atomic `UPDATE ... WHERE state = <from>`; the loser of a race (cancel vs. the
  worker finishing) gets `None` and must treat the row as already terminal. Illegal transitions
  (`queued → succeeded`) are refused, not coerced.
- **D7 - restart marks, never lies.** On orchestrator construction, `running` rows become
  `interrupted`. `queued` rows become `interrupted` too (D: a kernel restart means nobody will
  ever pick them up; leaving them queued forever is a lie of a different kind). 06 §3.1's
  "needs human confirmation whether to rerun" applies to both; rerunning is not in S10.
- **D8 - one process per task, no timeout on prompt.** Spawn → `initialize` → `session/new`
  (cwd = the acquired worktree path) → `prompt` → reap on every path. A persistent daemon is
  rejected: S10 has no resume/load story, and a per-task process cannot leak state between
  tasks. Prompt has **no deadline by design** - a task is minute-scale; the exits are a
  stopReason, process death (`EOF → ADAPTER_UNAVAILABLE`), and `task.cancel`. The 30s constant
  still bounds `initialize` / `session/new`.
- **D9 - stopReason maps to states, fail-closed.** `end_turn` → `succeeded` (`candidate_id`
  stays NULL; "succeeded with no change" is a legal shape per 06 §2.3); `cancelled` →
  `cancelled`; anything else → `failed` + `INTERNAL` (measured OpenCode only returns the first
  two on our paths; an unknown stopReason must not be celebrated).
- **D10 - cancel semantics.** `queued`: transition to `cancelled` directly; the worker checks
  before acquiring the worktree and aborts without side effects. `running`: send the
  `session/cancel` notification and return the row as it is - the worker does the
  `running→cancelled` transition when the prompt answers (measured: it answers within ~0.3 s).
  A task in a terminal state → `INVALID_PARAMS` (cancelling a finished task is a caller bug,
  and silent no-ops hide that).
- **D11 - evt/subscribe, minimal and whitelisted.** Frame method `subscribe` (02b §2.3 spells
  it without a module prefix; it is connection-level, so the WS handler special-cases it - it
  is not in `dispatch()` and not a Provider). Params `{topics: [...]}`; the whitelist is
  `("task.updated",)` (single source in `frames.KNOWN_TOPICS`, L1 - the gateway reads it, the
  orchestrator publishes through the same list); unknown topic → `INVALID_PARAMS`. A second
  `subscribe` **replaces** the set (idempotent). The hub hands events to per-connection
  `asyncio.Queue`s via `call_soon_threadsafe` (plain `asyncio.Queue` is not thread-safe);
  `evt` frames share the connection's `seq` counter with `res` frames. Subscriptions die with
  the connection.
- **D12 - `task.updated` only.** Data is exactly `{taskId, state, errorCode?}` (07 §5). Agent
  notifications (`session/update` chunks) are consumed to keep the pipe moving and forwarded
  to nothing - `task.output` is a later slice's contract, and half-implementing throttling
  here would be U17 all over again.
- **D13 - the orchestrator touches no `api/*` import.** It receives the `WorktreeService`
  instance from `app.py` (same assembly pattern as everything else; the no-L3→L3-import rule
  is honoured by construction, not by exception). It imports `agent/`, `storage/`, `domain/`,
  `frames`.
- **D14 - ULID without a dependency.** A pure `ulid()` in `domain/models.py`: 48-bit
  millisecond time + 80 bits from `os.urandom`, Crockford base32 - the 06 §2.3 shape, stdlib
  only (S3's no-new-dependency rule still stands).
- **D15 - idempotent-looking failures stay distinct.** Agent spawn failure at dispatch time
  (`ADAPTER_UNAVAILABLE`) surfaces as `failed`, not as a refused dispatch: the task happened,
  the agent didn't answer. Only *pre-execution* validation failures refuse the frame.

## 3. Acceptance

| # | Claimed | Command (runs green as written) |
|---|---|---|
| 1 | queued → running → succeeded, file exists in the worktree | `uv run pytest tests/test_dispatch.py -k real_integration -v` (skips honestly without `opencode`) |
| 2 | agent cwd is the assigned worktree | `uv run pytest tests/test_dispatch.py -k cwd -v` (fake session records the cwd it was handed) |
| 3 | worktree `git status` non-empty after success | covered by #1's assertions (the real integration checks `status --porcelain` in the worktree) |
| 4 | failure yields a stable code, never a silent failed | `uv run pytest tests/test_dispatch.py -k failure -v` (fake session dies mid-prompt → `ADAPTER_UNAVAILABLE`) |
| 5 | second concurrent dispatch gets `BUSY` | `uv run pytest tests/test_dispatch.py -k busy -v` |
| 6 | page shows the transitions without reload | `node tools/acceptance/s10_dispatch.mjs` (real Chrome, real opencode) |
| 7 | illegal transitions refused | `uv run pytest tests/test_dispatch.py -k transition -v` |
| 8 | evt reaches a subscribed client, not an unsubscribed one | `uv run pytest tests/test_dispatch.py -k evt -v` |

## 4. Acceptance → test map

| Acceptance | Tests |
|---|---|
| 1, 3 | `test_real_integration_creates_file_and_dirty_worktree` (skips without opencode) |
| 2 | `test_session_cwd_is_the_acquired_worktree` |
| 4 | `test_agent_death_marks_failed_with_stable_code`, `test_unknown_stop_reason_fails_closed` |
| 5 | `test_second_dispatch_while_active_is_busy` |
| 6 | `tools/acceptance/s10_dispatch.mjs` CHECK 1-18 |
| 7 | `test_illegal_transitions_are_refused`, `test_taskstore_transition_loses_race_returns_none` |
| 8 | `test_evt_reaches_subscriber_only`, `test_subscribe_replaces_and_validates_topics` |
| - (D6) | `test_restart_marks_running_and_queued_interrupted` |
| - (D10) | `test_cancel_queued_aborts_without_side_effects`, `test_cancel_running_sends_session_cancel` |
| - (D4) | `test_l0_bot_refused_before_any_execution` |

## 5. Boundary changes (09 §2.3)

None new. `tests/fixtures/` (declared in S9) gains a fake session object **defined inside
`tests/test_dispatch.py` itself** - it fakes the orchestrator's seam (the session factory), not
the ACP wire, so `fake_acp.py` is untouched.

## 6. Budget (04 §1.1: `src/` only)

Estimate at spec time: **~950 lines / 8 files** (models ~110, db ~190, acp_client ~150,
orchestrator ~200, gateway ~110, frames ~15, app ~25, assets ~150). Cap 1000 / 12. Measured
numbers go in the PR body; over-budget means splitting, not restating.

## 7. Reverse injections

- **S10 #1**: remove the `permission_covers` call from dispatch → `test_l0_bot_refused_before_any_execution` must go red. (Guards D4: the check is load-bearing, not decorative.)
- **S10 #2**: drop the active-task check *and* the partial unique index → `test_second_dispatch_while_active_is_busy` must go red. (Guards D5's two locks: removing one alone must still be caught by the other.)

## 8. Measured evidence (OpenCode 1.19.6, probed 2026-11-27)

All session-level shapes below were measured live, not inferred from docs.

1. **`session/new`** request `{cwd: <abs>, mcpServers: []}` → result `{"sessionId":
   "ses_…", "configOptions": [...]}`. `configOptions` (model/mode selectors) is OpenCode
   extra; S10 consumes **only** `sessionId`.
2. **`session/prompt`** request `{sessionId, prompt: [{type: "text", text}]}` **blocks until
   the turn ends** (8.7 s for "create hello.txt"), then answers `{stopReason: "end_turn",
   usage: {...}, _meta: {}}`. The file existed afterwards with the exact content. This is why
   D1/D2 need a worker thread: the frame layer cannot afford an 8.7 s (let alone minute-scale)
   synchronous call.
3. **During prompt**, stdout mixes `session/update` notifications with
   `sessionUpdate` ∈ {`agent_message_chunk`, `agent_thought_chunk`, `tool_call`,
   `tool_call_update`, `available_commands_update`, `usage_update`}. `_read_matching` already
   skips non-matching lines (S9 D6), so prompt reuses it unchanged.
4. **`session/cancel`** is a *notification* (no id): sent 6.0 s into a 100-file turn, the
   pending prompt request answered at 6.3 s with `stopReason: "cancelled"` and **zero files
   written**. Cancel is cheap and the answer shape is measured, so D9/D10 rest on data.
5. `initialize` is unchanged from S9 §8.1 (same keys, same capability shapes).

## 9. Non-goals

Candidate rows (S11), `task.list` (S11), `task.output` streaming, `interrupted` rerun UI,
`permission.check` frame method (S16), multi-channel selection UI, worktree auto-release into
`worktree.remove` (the dirty worktree is the deliverable of acceptance 3; releasing it is a
human decision).
