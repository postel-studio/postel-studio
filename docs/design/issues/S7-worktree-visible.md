# S7 · Allocate a workspace → a real worktree on disk

- **Milestone** M2 · **Estimate** 3h · **Depends on** S3 (bind) · **Priority** P0 (first M2 slice)
- **User path**: select a repo → type a base ref → click **Allocate workspace** → the page shows the
  path and the branch → the directory really exists → click **Release** → it is gone, and the repo's
  `git worktree list` no longer mentions it.

> Language: this is a **new** document, so it is English ([LANGUAGE.md](../../LANGUAGE.md) rule 3).
> Slice specs written before S6.6 stay Chinese and are not rewritten.

## 1. Why this slice, and what it refuses to do

A Bot cannot be dispatched without an isolated worktree (M2 order: S7 → S8 → S9 → S10). This slice
delivers the **smallest visible** version: allocate, see, release. It does **not** touch an agent.

It is also the slice where **the kernel writes to git for the first time**. Since S3 the read-only
invariant has held: the only thing the kernel ever wrote was `<state root>/state.db`. `git worktree
add` breaks that invariant — it writes `.git/worktrees/<name>/` inside the user's repository. So this
spec spends most of its length on **confining that write surface** (D5), not on the happy path.

Out of scope, deliberately: tasks, leases in the DB, dispatch, agent handshake, permission checks,
candidate branches, deleting branches, cleaning up directories the kernel did not create.

## 2. Decisions (D1–D10). These are contract; implementation may not re-decide them.

### D1 · Three methods, one payload shape

| Method | Params | Result |
|---|---|---|
| `worktree.acquire` | `repoId`, `botId`, `baseRef?` | `worktree` |
| `worktree.list` | `repoId` | `worktree[]` |
| `worktree.release` | `repoId`, `botId` | `{removed: bool, path: str}` |

`worktree` payload — **exactly seven fields**, no more, no fewer:

```json
{
  "repoId": "adec6540a725",
  "botId": "adec6540a725/builder",
  "botName": "builder",
  "path": "/home/u/.postel/worktrees/adec6540a725/builder",
  "branch": "bot/builder/workspace",
  "headSha": "91a5baa7",
  "reused": false
}
```

- `headSha` is **8 lowercase hex** (boundary formatting, same rule as S4 D1: gitops keeps 40, the
  frame gets 8).
- `list` entries carry the same fields **minus `reused`** — a list is not an action, and a
  `reused: false` in a list would be a claim about the past.
- `baseRef` is **input only, never echoed**. On reuse the requested base was not applied (D3), so
  echoing it would assert something that did not happen.
- Three methods = the per-PR ceiling (`04 §1`: ≤3 new public methods) is exactly reached, not
  exceeded.

### D2 · Where the directory lives

`<state root>/worktrees/<repo_id>/<bot_name>/`

- The resolved path must be inside `worktrees_root()`, asserted after `resolve()` — the same
  defence-in-depth as `bot_memory_dir` (S6 D3). The bot name passes the `^[a-z][a-z0-9-]{0,31}$`
  whitelist **before** it becomes a path component; `repo_id` must match 12 lowercase hex.
- Never inside the user's repository. Acceptance 1 and the "repo is untouched" assertion pin this.
- Directory mode: inherited from the state root (`0700`), parents created with `parents=True`; the
  leaf is created by git itself.

### D3 · The worktree gets a **new branch**, not the base branch

`branch = bot/<bot_name>/workspace`, created at the base commit when it does not exist yet.

Measured on this machine (git 2.53.0), and this is the whole reason for the decision:

```text
$ git -C demo worktree add /tmp/wt2 main          # main is checked out in demo
fatal: 'main' is already used by worktree at '/tmp/s7-probe/demo'

$ git -C demo worktree add -b bot/builder/workspace /tmp/wt-bot main
exit 0 ; git -C /tmp/wt-bot symbolic-ref --short HEAD
bot/builder/workspace
```

The base branch is *almost always* checked out in the user's main working tree, so checking it out
again is not an option. A detached worktree would satisfy git but not acceptance 2 ("show the branch
name") — a detached HEAD has no branch name to show.

Consequences, all deliberate:

- **Reuse never resets.** If the branch already exists (a previous release kept it, D5), acquire
  checks it out as-is. Resetting it would destroy work — harmless today, destructive from S10 on.
- **Release never deletes the branch.** `worktree remove` leaves `refs/heads/bot/*` alone (measured:
  the branch survives). Deleting a branch that may hold commits is the branch owner's decision, and
  06 §2.4 already says a rejected candidate keeps its branch.
- The namespace `bot/*` is shared with candidates (`bot/{bot}/{task_id}`). `workspace` is not a ULID,
  so the two are distinguishable; S10 decides whether the workspace branch becomes the task branch or
  is renamed per task.

### D4 · Nothing is persisted. The filesystem and git are the truth

No new table, no schema v4. `worktree.list` is computed as
`git worktree list --porcelain` ∩ `path` under `<worktrees_root>/<repo_id>/` ∩ directory exists.

Why: 06 §1 says the DB holds **only a rebuildable index**; a lease that can be recomputed from git is
not worth a migration. `WorktreeLease` (06 §2.5) needs `task_id`, and tasks arrive in S10 — creating a
table with a mandatory FK to a table that does not exist yet would mean inventing a nullable column
and migrating it again in S10.

**Correction to 06 §2.5, recorded here:** `WorktreeLease` is due **S10**, together with `Task`. Until
then the workspace is identified by `(repo_id, bot_name)` — exactly the directory path, exactly the
`bot_id` composite key.

### D5 · The write surface (the first write to git) — an allowlist, not vibes

Every git invocation in this slice goes through `git -C <repo.path>` (the bound path is the only
allowed root, S3 D4). **Allowed writes, complete list:**

| Command | Writes | When |
|---|---|---|
| `git worktree add [-b <branch>] <path> [<sha> \| <branch>]` | `.git/worktrees/<name>/`, `refs/heads/bot/<bot>/workspace` (only with `-b`), reflog | acquire |
| `git worktree remove <path>` | deletes `.git/worktrees/<name>/` and the (clean) directory | release |
| `git worktree prune` | removes metadata of worktrees whose directory is gone | acquire/release, **only** when a registered path has no directory |

**Forbidden, and each is pinned by a reverse injection or an assertion:**

- `--force` anywhere: `worktree remove --force` would delete uncommitted work (measured: plain
  `remove` refuses with exit 128 when the worktree is dirty, `--force` does not).
- `reset --hard`, `checkout` in the main worktree, `clean`, `stash`: the kernel never touches the
  user's working tree.
- Deleting branches, `-D`, `update-ref -d`.
- Writing any file inside the worktree (it belongs to the Bot from S10 on).
- Any command whose path argument is not prefixed by `worktrees_root()`.

Timeout: `WORKTREE_TIMEOUT_SECONDS = 30.0`. Reads use 5 s (S3 D5) because they are milliseconds; a
`worktree add` checks out a whole tree and can legitimately take seconds on a large repository. A
timeout maps to `INTERNAL` (same as S3's git timeout, until S19's doctor refines it).

### D6 · Error codes: one new code, justified

| Situation | Code |
|---|---|
| `repoId` / `botId` missing, not a string, empty | `INVALID_PARAMS` |
| `repoId` unknown, or the bound path is gone / `.git` deleted | `REPO_NOT_BOUND` |
| `botId` unknown (including a path-traversal attempt) | `INVALID_PARAMS` (S6 D6 precedent) |
| `baseRef` shape invalid (`-` prefix, control chars, >200 chars, lone surrogate) | `INVALID_PARAMS` |
| no `baseRef` given **and** the repo has no default branch (detached HEAD) | `INVALID_PARAMS` |
| `baseRef` does not resolve to a commit — including a repo with **zero commits** | `BAD_BASE_REF` |
| the target path exists, is not our worktree, and is not empty | **`WORKTREE_CONFLICT`** (new) |
| release: the worktree has uncommitted changes or untracked files | **`WORKTREE_CONFLICT`** (new) |
| release: the path exists but is not a registered worktree of this repo | **`WORKTREE_CONFLICT`** (new) |
| `bot/<bot>/workspace` is checked out in another worktree | **`WORKTREE_CONFLICT`** (new) |
| `git` missing | `ADAPTER_UNAVAILABLE` |
| `git` timeout, or `worktree add/remove` fails for any other reason | `INTERNAL` |
| non-UTF-8 ref name (`NOT_SUPPORTED` from `resolve_commit`) | `NOT_SUPPORTED` |

**Why a new code** (09 §2.2 allows it with the table + mapping + PR callout): the three
`WORKTREE_CONFLICT` rows are *normal user situations* — a directory that is already occupied, a
worktree with uncommitted work, a branch already checked out elsewhere. Mapping them to `INTERNAL`
("unexpected error, report a bug") would be the same lie S4 D5 refused to tell about a deleted repo.
`CONTEXT_CONFLICT` is the closest existing 409, but its registered meaning is "charter/AGENTS.md
conflict", and widening it would silently change a contract someone else reads. So: new code, one row,
`409`, retry "after the human resolves it". `ERROR_TEXT` gains the matching sentence; the PR body
calls it out separately.

### D7 · Reuse / repair state machine (the boundary matrix in executable form)

Two independent facts decide everything: **is the path registered in `git worktree list`?** and
**does the directory exist?**

| registered | directory | acquire | release |
|---|---|---|---|
| yes | yes | **reuse** (`reused: true`), no git write at all | `worktree remove` → `removed: true`; dirty → `WORKTREE_CONFLICT` |
| yes | no | `prune`, then create (`reused: false`) | `prune` → `removed: true` (it was already gone) |
| no | no | create (`reused: false`) | `removed: false` — nothing to do, **not an error** |
| no | yes, empty | create into it (git accepts an existing empty dir; measured) | `WORKTREE_CONFLICT` — not ours, may hold files |
| no | yes, non-empty | `WORKTREE_CONFLICT` | `WORKTREE_CONFLICT` |

`prune` in row 2 is the only "repair" write, and it is safe by definition: it deletes metadata of
worktrees whose directory is already gone (measured: the stale entry disappears, live worktrees are
untouched). Detection uses the `prunable` marker line — its **text is localized** (measured:
`prunable gitdir 文件指向一个不存在的位置`), so only the line **prefix** is parsed. That is also why
no git error *message* is ever parsed in this slice: every decision comes from filesystem state plus
the porcelain records, never from localized output.

### D8 · `baseRef` handling: one place takes user input

Shape checks are S5 D3.1's, reused verbatim (length ≤ 200, no control chars, no leading `-`, must
`encode("utf-8")`, non-empty after strip). Then `gitops.resolve_commit` turns it into an object id —
**the only function that hands a user string to git** (S5 D2). `worktree add` therefore receives
`-b <branch> <path> <sha>` with a resolved sha, never the raw ref: the injection surface stays at one
command, with `--end-of-options`.

`baseRef` defaults to `repo.default_branch`. Detached HEAD → `default_branch` is `None` → the caller
must pass `baseRef` explicitly (`INVALID_PARAMS` when it does not).

### D9 · Page contract (DOM ids are contract)

New section `#workspace` in the main area, after `#charter` (no re-layout of existing panels).

| Element | Contract |
|---|---|
| `#workspace` | `data-state` ∈ `idle` / `loading` / `ready` / `error`; visibility **only** via CSS on `data-state` (T8: never `style.display`) |
| `#workspace-form`, `#workspace-base`, `#workspace-acquire` | base input (pre-filled with the repo's default branch) + submit button; a real `<form>`, so Enter works |
| `#workspace-state` | idle: "select a repo first"; loading: "reading…"; disconnected: "已断开：工作区未知" |
| `#workspace-empty` | `ready` **and** no workspace: "这个仓库还没有分配工作区" |
| `#workspace-path`, `#workspace-branch`, `#workspace-bot` | absolute path, branch name, and the Bot name the workspace belongs to |
| `#workspace-release` | only in `ready` with a workspace; fires `worktree.release` |
| `#workspace-error` | mapped by **code** (`ERROR_TEXT`), never the server `message` (05 §4.2) |

Discipline inherited from S4/S5/S6: switching repo clears the panel; disconnect → `idle` + cleared
list (no stale data pretending to be fresh); reconnect re-fetches `worktree.list`; all text via
`textContent`; a per-panel request token so a stale response cannot overwrite a newer one.

**Which Bot?** S7 has exactly one Bot per repo (S6's seeded `builder`). The card states the Bot it acts
on (`Bot：builder`) and acquires for **the first Bot in `bot.list`**; S8 makes the choice explicit when
there is more than one. The Bot is shown, not assumed silently.

### D10 · The two pending items the roadmap required before S7

- **U13 (a worktree's origin / default branch)** — measured again on git 2.53.0:

  ```text
  $ git -C /tmp/wt config --get remote.origin.url
  https://example.com/o/p.git          # NOT None

  $ git -C /tmp/wt symbolic-ref --short HEAD
  bot/b/workspace                       # the worktree's own branch, not the main repo's
  ```

  So the first half of U13 ("origin answers None") **does not reproduce** on this git; the second half
  does (a linked worktree's HEAD is its own branch, correctly labelled as such). Decision: **waive, do
  not fix** — this slice never displays a worktree's origin or "default branch"; the workspace card
  shows *its own* branch, which is what git answers. The odd field only appears if a human binds a
  worktree path as a repo, which S7 does not do. Record the measurement in 06-pitfalls; the due date
  moves to S19 (doctor), where "this repo is a linked worktree" can be reported honestly.
- **U14 (moving a repo leaves a second row pointing at a dead path)** — **waive, due S19.** Fixing it
  means promoting `origin_hash` from "identification only" to a matching key, plus two rules that do
  not exist yet (what to do with `NULL` origin_hash rows; which row wins when two share a hash). That
  is a data-model slice, and 04 §6 forbids mixing two user paths in one PR. Consequence, stated
  plainly: after a move the left panel keeps one unopenable row, and `~/.postel/worktrees/<old id>/`
  becomes an orphan directory. Nothing is deleted (06 §5); S19's doctor reports both. S7 itself does
  not depend on identity surviving a move: workspaces are keyed by the row the user actually clicks.

## 3. Acceptance, each with a command that was run

The kernel under test: `POSTEL_HOME=/tmp/s7 uv run postel-studio serve --port 0`, then frames over the
WebSocket (the tests do this in-process; the browser script does it through the page).

```text
# 1 · allocate → directory exists, and git knows it is a worktree of this repo
worktree.acquire {repoId, botId}                        → ok, path=<state>/worktrees/<id>/builder
test -d <path>                                          → 0
git -C <repo> worktree list --porcelain | grep <path>    → one record, branch refs/heads/bot/builder/workspace
git -C <path> symbolic-ref --short HEAD                 → bot/builder/workspace

# 2 · the page shows path and branch (real browser, s7_worktree.mjs)
#workspace[data-state=ready] + #workspace-path == the path + #workspace-branch == the branch

# 3 · second click reuses: same path, reused=true, still exactly one worktree record
worktree.acquire {repoId, botId} again                  → same path, reused=true
git -C <repo> worktree list --porcelain | grep -c <path>  → 1

# 4 · release → directory gone, no record left, branch kept
worktree.release {repoId, botId}                        → {removed: true, path: …}
test -d <path>                                          → 1 (missing)
git -C <repo> worktree list --porcelain | grep <path>    → no output
git -C <repo> branch --list 'bot/builder/workspace'      → still there (D3)

# 5 · bad base → BAD_BASE_REF and nothing half-created
worktree.acquire {repoId, botId, baseRef:"nosuch"}       → error.code == BAD_BASE_REF
test -d <path>                                          → 1 (missing; no half directory)
```

## 4. Acceptance → test map (write the table first, then the code)

| # | Test |
|---|---|
| 1 | `tests/test_worktree.py::test_acquire_creates_directory_registered_by_git` |
| 2 | `tools/acceptance/s7_worktree.mjs` (real Chrome) + `tests/test_worktree.py::test_page_declares_workspace_panel` (text-level: ids and `data-state` wiring exist) |
| 3 | `::test_acquire_twice_reuses_the_same_worktree` |
| 4 | `::test_release_removes_directory_and_registration_but_keeps_the_branch` |
| 5 | `::test_bad_base_ref_leaves_no_directory` |

Boundary tests (D7 table, D6 table, D8 rules):

```text
::test_acquire_without_base_ref_uses_default_branch
::test_acquire_on_detached_repo_requires_base_ref
::test_acquire_on_empty_repo_is_bad_base_ref
::test_acquire_on_bare_repo_with_commits_works
::test_acquire_rejects_invalid_base_ref_shape
::test_acquire_unknown_repo_is_repo_not_bound
::test_acquire_when_repo_path_is_gone_is_repo_not_bound
::test_acquire_unknown_or_malformed_bot_id_is_invalid_params
::test_acquire_on_occupied_directory_is_worktree_conflict
::test_acquire_repairs_a_pruned_worktree_entry
::test_release_dirty_worktree_is_conflict_and_keeps_files
::test_release_is_idempotent_when_nothing_was_allocated
::test_release_repairs_a_pruned_worktree_entry
::test_list_ignores_directories_that_are_not_worktrees
::test_list_returns_one_entry_per_bot
::test_worktree_dir_rejects_names_that_escape_worktrees_root      (paths layer)
::test_user_repository_has_no_new_files_after_acquire_and_release
```

## 5. Boundary matrix (enumerated from the data model, not from the happy path)

| Entity | States that must be covered |
|---|---|
| Repo | normal / bare with commits / bare without commits / zero commits / path deleted / `.git` deleted / the repo itself is a linked worktree |
| Bot | exists (`builder`) / unknown id / id containing `../` / name failing the whitelist |
| Base ref | default branch / explicit branch / sha / nonexistent / detached HEAD (no default) / `-x` / control char / 201 chars / lone surrogate / non-UTF-8 refname |
| Worktree path | absent / empty dir / non-empty dir / registered+present / registered+deleted (prunable) / registered but on another branch |
| Release | clean / dirty (modified) / untracked only / nothing allocated / path deleted by hand |
| Connection | connected / disconnected (panel → idle, list cleared) / reconnect with a repo still selected |

## 6. Files and budget

`src/` only counts (04 §1.1). Estimate written **before** coding:

| File | Change | Est. lines |
|---|---|---|
| `src/postel_studio/gitops/worktrees.py` | new: the write surface + porcelain parser | ~150 |
| `src/postel_studio/api/worktrees.py` | new: `WorktreeService` (3 methods, error mapping) | ~170 |
| `src/postel_studio/storage/paths.py` | `worktrees_root()` / `worktree_dir()` | ~22 |
| `src/postel_studio/api/gateway.py` | 3 dispatch rows + 3 provider fields | ~10 |
| `src/postel_studio/api/app.py` | wire the service | ~5 |
| `src/postel_studio/web/assets.py` | workspace card (CSS + DOM + JS) | ~150 |

Total ≈ **507 lines / 6 files** — inside 700 / 12. Tests, acceptance script and docs do not count
(04 §1.1) but are not optional either (DoD).

## 7. Reverse injections (new hard constraints only)

| # | Inject | Must turn red |
|---|---|---|
| 1 | drop the bot-name whitelist in `worktree_dir` | `test_worktree_dir_rejects_names_that_escape_worktrees_root` |
| 2 | acquire always calls `worktree add` instead of checking the registered list | `test_acquire_twice_reuses_the_same_worktree` |
| 3 | release uses `worktree remove --force` | `test_release_dirty_worktree_is_conflict_and_keeps_files` |
| 4 | pass `baseRef` straight to `worktree add` instead of resolving it | `test_acquire_rejects_invalid_base_ref_shape` |

## 8. Measured git behaviour (evidence, run before writing this spec)

```text
$ git -C demo worktree add /tmp/wt2 main            # base branch already checked out
fatal: 'main' is already used by worktree at '/tmp/s7-probe/demo'

$ git -C demo worktree add -b bot/d/workspace /tmp/wt3 main     # /tmp/wt3 exists and is empty
HEAD 现在位于 090160f one                                        # exit 0 — an empty dir is accepted

$ mkdir /tmp/wt2 && touch /tmp/wt2/keep.txt
$ git -C demo worktree add -b bot/c/workspace /tmp/wt2 main
fatal: '/tmp/s7-probe2/wt2' 已经存在                            # exit 128, keep.txt untouched

$ git -C demo worktree remove /tmp/wt3              # wt3 has  M a.txt + ?? untracked.txt
fatal: '/tmp/s7-probe2/wt3' 包含修改或未跟踪的文件，使用 --force 删除   # exit 128

$ rm -rf /tmp/wt && git -C demo worktree list --porcelain
worktree /tmp/s7-probe2/wt
HEAD 090160f710c2be3ea755de4e2434389c9852705b
branch refs/heads/bot/b/workspace
prunable gitdir 文件指向一个不存在的位置        # localized text: parse the prefix only

$ git -C demo worktree prune && git -C demo worktree list --porcelain
worktree /tmp/s7-probe2/demo                    # the stale entry is gone; live ones untouched

$ git -C demo branch --list 'bot/*'
  bot/b/workspace                              # survives release (D3)

$ git -C bare.git worktree add /tmp/wt main     # bare repo **with** commits: works
$ git -C empty worktree add -b bot/b/workspace /tmp/wt-empty main
fatal: 无效引用：main                          # zero commits → BAD_BASE_REF (D6)

$ git -C /tmp/wt config --get remote.origin.url  → https://example.com/o/p.git   （U13, see D10）
```

## 9. 05 §7 checklist (security)

| Question | Answer |
|---|---|
| New paths `resolve()`d and inside an allowed root? | Yes: `worktree_dir()` asserts under `worktrees_root()` after `resolve()`; every git call uses the bound `repo.path` as `-C` |
| New subprocess calls list-form, no shell? | Yes: literal argv, `shell=False`, shared env (`GIT_TERMINAL_PROMPT=0`, `GIT_CONFIG_NOSYSTEM=1`), 30 s timeout |
| New logs leaking credentials or sensitive absolute paths? | No new logging. Errors never echo user input (05 §4.2) |
| Permission decided **before** execution? | Not applicable — S7 has no permission gate (S16). The Bot's permission is not consulted because nothing runs yet |
| New path handing credentials to an agent? | No (no agent in this slice) |
| New third-party packages? | No |
| UI text overstating security? | No. The card shows a path and a branch; no "safe"/"isolated" claims |

## 10. Forbidden

- Creating a worktree inside the user's repository working tree.
- `reset --hard` / `checkout` / `clean` / `stash` on the main worktree; `--force` on any worktree
  command; deleting branches.
- Writing files into the worktree (that is the Bot's, from S10).
- Persisting a lease/table in this slice (D4).
- Touching `repo.diff` / `repo.branches` semantics (S4/S5 own them).
- Adding dispatch/agent code "while we are here" — S10.
- Parsing any localized git message to make a decision (D7).

## 11. Definition of done

- [ ] The five acceptance criteria run against the real kernel, with raw output pasted in the PR
- [ ] Every boundary row in §5 has a test that would fail if the behaviour flipped
- [ ] The four reverse injections are in `tests/test_reverse_checks.py` and green
- [ ] `bash scripts/check.sh all` green; `build` run separately
- [ ] All four existing `tools/acceptance/*.mjs` re-run (page changed) **plus** the new
      `s7_worktree.mjs`; skips stated explicitly, never counted as passes
- [ ] `07-api-contract.md`: new method group + `WORKTREE_CONFLICT` row; `06-data-model.md`: §2.5
      corrected (lease due S10); `03-ui-spec.md`: workspace card
- [ ] codemap: gitops page gains the write-surface section and the new symbol table; module index
      line counts refreshed; `04-capabilities` gains a C-row for the workspace
- [ ] `06-pitfalls`: U13/U14 decisions recorded with the measurement above
- [ ] Budget reported honestly: actual `src/` lines and files vs §6

## 12. Addendum (written while implementing, not edited into the decisions above)

The spec is the first commit; this section records what the implementation learned, in the order it
learned it. Nothing above was rewritten to match the outcome.

### 12.1 A real defect: `worktree add` with a commit id silently detaches HEAD

The first implementation passed the resolved commit id in **both** forms of `add`. When the branch
already exists (i.e. every acquire after a release) git checks out the *commit*, not the branch, and
says nothing:

```text
$ git -C /tmp/wt symbolic-ref --short HEAD
fatal: 不是符号引用                     # detached
$ git -C demo worktree list --porcelain
HEAD 27999c10                          # the base commit, not the branch tip
                                       # no `branch` line at all
```

The test that would have caught it ("commit on the workspace branch, release, acquire, assert the
tip") did not exist in the first pass; the reuse test only compared paths, and the path was right.
Recorded as **T40** in `06-pitfalls`, fixed in `gitops/worktrees.py` (form without `-b` takes the
branch name), pinned by `test_acquire_after_release_keeps_the_workspace_branch_tip`.

### 12.2 A real defect: after a refused release the button disappeared

The first CSS hid `#workspace-details` in the `error` state. A dirty worktree therefore left the user
with an error and **no way to retry** — the real-browser script (`s7_worktree.mjs`) failed on the
click. Error state now keeps the path and the button visible; only `#workspace-state` and
`#workspace-empty` are hidden. The D9 table above says "release button only in ready"; the accepted
behaviour is "visible whenever a workspace is shown, including after an error".

### 12.3 Two shared predicates moved one layer down (so L3 does not import L3)

`worktree.acquire` needs the same two checks `repo.diff` uses: the inbound ref-name shape and "is
this bound path still a repo". Importing them from `api/repository.py` would be an L3→L3 import,
which [`02-layers §3`](../../codemap/02-layers.md) forbids. They moved to the layer that owns each
question:

| Was | Now | Why there |
|---|---|---|
| `api/repository.MAX_REF_LENGTH` + `ref_name_is_valid` | `domain/models.REF_NAME_MAX_LENGTH` + `ref_name_ok` | a pure string predicate, no IO — the domain layer's shape |
| `api/repository.repo_path_is_gone(repo)` | `gitops/repo.is_usable_repo(path)` | it only asks git semantics; it never needed the `Repo` entity |

Behaviour is unchanged (`repo.diff` calls both), and `api/worktrees.py` now imports nothing from
`api/`.

### 12.4 Budget: the estimate was wrong, here are the actuals

`src/` diff measured with `git diff --numstat origin/main -- src`:

| File | Added | Deleted | Total |
|---|---|---|---|
| `api/worktrees.py` (new) | 300 | 0 | 300 |
| `web/assets.py` | 231 | 0 | 231 |
| `gitops/worktrees.py` (new) | 226 | 0 | 226 |
| `storage/paths.py` | 49 | 0 | 49 |
| `domain/models.py` | 40 | 0 | 40 |
| `gitops/repo.py` | 32 | 0 | 32 |
| `api/gateway.py` | 15 | 4 | 19 |
| `api/app.py` | 10 | 1 | 11 |
| `api/repository.py` | 5 | 32 | 37 |
| **Total** | **908** | **37** | **945 / 9 files** |

**945 > 700.** The estimate in §6 (507) was wrong by 1.9×. Composition: two new modules whose
docstrings carry the decisions (526 lines total, of which ~120 are relocated from `repository.py`/
`repo.py`), the workspace card (231), and the wiring. Nothing was split after the fact; the honest
options are (a) accept it as one visible path — allocate → see → release is a single user path and
splitting it would produce a PR with nothing visible — or (b) recalibrate the number again, as
`04 §1.2` did for 400 → 700. Both are the maintainer's call; this PR only reports the measurement.

### 12.5 Test names as shipped

§4 listed the planned names. Those that changed while implementing:

| Planned | Shipped |
|---|---|
| `::test_acquire_without_base_ref_uses_default_branch` | same, plus `::test_acquire_with_explicit_base_ref_uses_that_commit` (the default-branch case alone cannot prove `baseRef` is used) |
| — | `::test_acquire_after_release_keeps_the_workspace_branch_tip` (T40's regression) |
| — | `::test_acquire_into_an_empty_directory_succeeds`, `::test_acquire_when_the_target_path_is_a_file_is_conflict`, `::test_release_on_a_foreign_directory_is_conflict`, `::test_list_on_a_gone_repo_is_repo_not_bound`, `::test_gateway_exposes_the_three_worktree_methods` |

35 cases in `tests/test_worktree.py`; the parametrised repo-gone test covers both "directory deleted"
and "`.git` deleted" (the T25 shape).
