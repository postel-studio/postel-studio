# S11 · Task succeeded → one candidate appears, with a real diff

- **Milestone** M2 · **Estimate** 3h · **Depends on** S10 · **Priority** P0
- **User path**: a task succeeds → a candidate card appears with `bot/<bot>/<task-short>`,
  `+X −Y`, and `仅本地` → clicking it shows the same diff renderer as S5 → a reload still
  finds the row.

## 1. Scope

S10 proved that a task really changes a worktree but deliberately stopped at
`end_turn → succeeded` ("S11 looks at the disk"). This slice freezes those changes into a
reviewable **candidate branch + row** and gives the user one detail card. In: `Candidate`
entity and `candidates` table (v5), candidate emergence inside the S10 worker, the new git
write surface needed to freeze uncommitted work, `candidate.list` (filtered by `repoId` or
`taskId`), `candidate.proposed`, and a single-candidate detail card reusing S5's diff code.
Out: the inbox list page and filtering UI (S12), approve/reject/push/PR (S13), persistence of
diff text (06 §8: never cached), `task.output` streaming.

**Q1 (03-ui-spec §6, inbox entry) is still deferred to S12.** S11 deliberately does not
build an inbox or a tab: its detail card lives in the main area next to `#task`, and is
driven by the task it belongs to. No UI code in this slice depends on where S12 puts the list.

## 2. Decisions

- **D1 - the candidate branch is derived, never authored.** `branch =
  bot/{bot_name}/{task_id[-8:].lower()}` via a pure `candidate_branch()` in `domain/models.py`.
  **Amended after measurement:** the first draft used the 8-char *prefix*, but the first 40
  bits of a ULID are all timestamp — two dispatches in the same millisecond produced the
  same branch (`bot/builder/01m8e3ey`, second task failed with "候选分支已存在"). The tail
  8 chars come from the 80-bit random segment (2^-40 collision within a millisecond).
  Bot names already pass `bot_name_ok` (`^[a-z][a-z0-9-]{0,31}$`); the function still replaces
  any character outside `[a-z0-9-]` with `-` (defence in depth for bad stored data). Agent
  prompt text never reaches a ref name. `workspace` (S7) cannot collide with an 8-char ULID
  tail (Crockford chars never spell `workspace` in 8 chars).
- **D2 - base_sha is the worktree HEAD captured at task start, not the task's `baseRef`.**
  S7 D3/D5 forbids resetting a reused workspace branch, so a second task on the same Bot
  starts from the previous candidate's commit. Snapshotting HEAD immediately before the
  session makes `base...head` contain **exactly this task's** changes. Measured in §8:
  task 2's diff is `second.txt` only, whereas diffing from the original base contains both
  files. 06 §2.4's "base_sha is an ancestor of head_sha" holds by construction.
- **D3 - "nothing to do" is not a failure.** If HEAD is unchanged and the worktree is clean
  after the prompt, the task stays `succeeded` with `candidate_id = NULL` and no event. A
  post-commit zero-stat diff is treated the same. This is 06 §2.3's invariant, made
  executable.
- **D4 - the kernel commits with its own identity, deterministically.** `git add -A` then
  `git commit -m "candidate <task_id>"` in the **linked worktree**, with explicit
  `-c user.email=kernel@localhost -c user.name=postel` so the user's git config cannot
  change the outcome (T34: git config must never alter a kernel assertion). No user prose in
  the commit message. Ignored files stay ignored: they are not part of a git candidate and
  are reported as a documented limitation, not silently deleted.
- **D5 - a narrow, explicit extension of the S7 write surface.** S7 D5 allowed only
  worktree add/remove/prune and forbade `reset --hard`, `clean`, `stash`, branch deletion,
  and kernel writes into the worktree. S11 keeps **all** of those prohibitions and adds
  exactly three operations: `commit_all(worktree, message)`, `create_branch(root, branch,
  start_sha)` (in the **main** repo, via `git -C <repo.path>`), and the read-only
  `head_commit(worktree)`. Timeout stays 30 s. `--force` and `reset --hard` remain forbidden.
  The kernel never edits a file: the Agent writes files, the kernel only stages and commits
  the Bot-owned worktree. This supersedes S7 D5 **for S11 only**, by explicit allowlist.
- **D6 - candidate row first, task transition second.** `tasks.candidate_id` already exists
  (v4). Insert the candidate row (FK to task), then `TaskStore.transition(...,
  candidate_id=...)`. If the transition loses a cancel race (returns `None`), delete only
  the row this worker just inserted — no orphan points at a cancelled task. Publish
  `candidate.proposed` after the winning transition, never before.
- **D7 - `candidate.list` is the detail read.** Params: `repoId?`, `taskId?`, `reviewState?`.
  `repoId` resolves through `tasks → bots` (the candidate table has no denormalised repo id);
  `taskId` is the card's single-row read. S11 stores and filters `reviewState` but the only
  value produced is `local_only`. Payload (camelCase at the boundary): `candidateId`,
  `taskId`, `botName`, `branch`, `baseSha`, `headSha`, `filesChanged`, `insertions`,
  `deletions`, `reviewState`, `remote`, `target`, `createdAt` (`remote`/`target` are `null`
  for local-only). The detail view calls the existing `repo.diff` with `base`/`head` SHAs;
  diff text and stat are **never** stored on the candidate (06 §8).
- **D8 - `candidate.proposed` joins the subscription whitelist.** `frames.KNOWN_TOPICS`
  gains it; the page subscribes to `task.updated` **and** `candidate.proposed` on open and
  keeps the card scoped to the selected Bot's most recent task. New topic, new payload
  `{candidateId, taskId}` exactly as registered in 07 §3.
- **D9 - `#candidate` is one card, not a list.** DOM ids: `candidate`, `candidate-title`,
  `candidate-state`, `candidate-detail`, `candidate-branch`, `candidate-stat`,
  `candidate-diff`, `candidate-error`, `candidate-open`. Five states
  (`idle/ready/loading/done/error`) via `data-state` + CSS, same discipline as `#task` /
  `#diff`. `candidate-open` reuses `renderDiffPatch`/`diffStatText` against the S5 `#diff`
  panel (no second renderer). TaskId mismatch events are ignored; disconnect / repo switch
  reset the card (same hooks as `resetTask`).
- **D10 - error mapping.** Bad `repoId`/`taskId` shapes → `INVALID_PARAMS`; unknown repo →
  `REPO_NOT_BOUND`; missing git binary → `ADAPTER_UNAVAILABLE` (existing code); a failure
  while *creating* a candidate (git commit/branch/stat) → the task is terminal `failed` +
  `INTERNAL` (S10 D15: the task happened, do not lie with a frame error). `candidate.list`
  never returns `INTERNAL` for a bad id.

## 3. Acceptance

1. A successful task with changes creates a row: branch `bot/<bot>/<task-short>`, `+X −Y`
   computed by git, `reviewState = local_only`.
2. The branch name is predictable and contains no user prose; sanitising a deliberately
   illegal Bot name yields `-` in place of every illegal character.
3. A successful task with **no** changes creates no candidate and stays `succeeded`.
4. Opening the candidate shows a diff equal to `git diff <baseSha>...<headSha>` (S5 renderer).
5. The row survives closing and reopening the store (status root, not memory).
6. A browser check drives the whole path: dispatch a file-creating prompt → card appears →
   open it → diff line visible → reload → card still present.

## 4. Acceptance → test map

| Acceptance | Test |
|---|---|
| 1 | `tests/test_candidate.py::test_success_with_changes_creates_candidate` (real repo) |
| 2 | `tests/test_candidate.py::test_candidate_branch_name_is_predictable_and_sanitized` |
| 3 | `tests/test_candidate.py::test_no_change_task_stays_succeeded_without_candidate` |
| 4 | `tests/test_candidate.py::test_candidate_diff_matches_git_shortstat` (+ frame roundtrip) |
| 5 | `tests/test_candidate.py::test_candidate_row_survives_store_reopen` |
| 6 | `tools/acceptance/s11_candidate.mjs` (real Chrome + real opencode) |
| event contract | `tests/test_candidate.py::test_candidate_proposed_reaches_only_subscribers` |
| reused worktree | `tests/test_candidate.py::test_second_task_on_reused_worktree_diff_is_isolated` |
| agent committed on its own | `tests/test_candidate.py::test_agent_committed_changes_still_produce_candidate` |

## 5. Boundary changes (09 §2.3)

- **New L3 module** `src/postel_studio/candidates.py` (`CandidateService`): keeps the S10
  orchestrator from absorbing all git/DB work. Injected via a `CandidateMaker` Protocol;
  tests replace it with a fake. Registered in `scripts/gate-layers.py` (forbids `web` and all
  same-layer handlers, same as `orchestrator.py`).
- **Git write surface** extended in `gitops/worktrees.py` as listed in D5; codemap page must
  state the new allowlist and the unchanged prohibitions.
- **Schema** append v5 (`candidates` table only; `tasks.candidate_id` already exists).
- No new dependency, no CI/config change, no new directory outside `tools/acceptance/`.

## 6. Budget (04 §1.1: `src/` only)

Estimate: **~620 lines / 9 files** (models ~60, db ~110, gitops/wt ~70, candidates.py ~140,
orchestrator ~80, gateway/app/frames ~45, assets ~115). Cap 1000 / 12, three public methods
(`candidate.list` is the only new one; `CandidateService.emerge` / `CandidateStore` are
internal). Measured numbers go in the PR body; over-budget means splitting, not restating.

## 7. Reverse injections

- **S11 #1**: remove the sanitising `re.sub` from `candidate_branch()` →
  `test_candidate_branch_name_is_predictable_and_sanitized` must go red. Guards D1.
- **S11 #2**: capture HEAD *after* the agent instead of before →
  `test_second_task_on_reused_worktree_diff_is_isolated` must go red. Guards D2 (the whole
  reason base_sha is not `baseRef`).
- **S11 #3 (composite)**: remove **all three** no-change guards together — the
  pre-commit `not dirty and head == base_sha`, the post-commit `head == base_sha`,
  and the zero-`diff_stat` check → `test_no_change_task_stays_succeeded_without_candidate`
  must go red. Removing one or two keeps it green (the rest cover it) — the same
  redundant-defence lesson as T45/S10 #2, measured three times during implementation.
  Guards D3.
- **S11 #4**: remove the compensating delete after a lost transition race → the cancelled-
  during-emergence test must find an orphan candidate. Guards D6.

## 8. Measured evidence (real git 2.53, probed 2026-11-27)

Freeze and diff (base → commit in linked worktree → branch in main repo → `diff --numstat`):

```text
$ git -C /tmp/s11-probe/wt status --porcelain | wc -l
1
$ git -C /tmp/s11-probe/wt add -A && git -C /tmp/s11-probe/wt \
    -c user.email=kernel@localhost -c user.name=postel commit -q -m 'candidate: task-1'
$ git -C /tmp/s11-probe/main branch bot/builder/01JTEST <head>
base=02c9b541ec181ee1977d29de04987c186eb30701
head=96fbd1997070dae39989c142a088315d6a9bcdd7
ancestor=yes
$ git -C /tmp/s11-probe/main diff --numstat -z --no-renames "$BASE...$HEAD" | tr '\0' '|'
1	0	hello.txt|
$ git -C /tmp/s11-probe/main diff --shortstat "$BASE...$HEAD"
 1 file changed, 1 insertion(+)
```

Reuse without reset (the second task's diff must not include the first task's file):

```text
$ git -C wt add -A && git -C wt -c user.email=kernel@localhost -c user.name=postel \
    commit -q -m 'candidate: task-2' && git -C main branch bot/builder/01JTEST2 <head2>
task2 base=96fbd1997070dae39989c142a088315d6a9bcdd7
head=022e77967f1711aacd3c8d8ce803173fe34ed564
$ git -C main diff --numstat "$HEAD1...$HEAD2"
1	0	second.txt
$ git -C main diff --numstat <original-base>..."$HEAD2"     # what NOT to use
1	0	hello.txt
1	0	second.txt
clean worktree no-change: dirty=0 head_same=yes
```

`git diff --numstat -z` is exactly the command `gitops.diff_stat` already uses; the
`--shortstat` line confirms the `+X −Y` numbers agree with git. `merge-base --is-ancestor`
confirms D2's invariant. No `reset`, no `clean`, no `--force` was needed anywhere.

Failed first attempt (the reason D1 now uses the ULID tail), from the real test log:

```text
INFO orchestrator 任务 01M8E3EYAZ10VYDVQM63EFXPPA 以 INTERNAL 结束：候选分支已存在：bot/builder/01m8e3ey
```

Two tasks dispatched in the same millisecond share the ULID **prefix** (40 bits of
timestamp) — the branch-name collision is structural, not probabilistic.

## 9. Non-goals

- No inbox list / empty state / filtering UI (S12) and no review actions (S13).
- No repeated candidate for a task with no changes, and no candidate for cancelled / failed /
  interrupted tasks.
- No commit of ignored files, no rewrite of the user's main working tree, no deletion of any
  git branch (including a rejected candidate's branch, per 06 §2.4).
