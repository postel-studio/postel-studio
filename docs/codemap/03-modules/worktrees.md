# 03 · worktrees — `src/postel_studio/api/worktrees.py`

> Layer L3 (**business orchestration, never touches fastapi**) · 300 lines · Up:
> [module index](README.md) · Related: [S7 spec](../../design/issues/S7-worktree-visible.md) ·
> [07 §2.8 / §4.9](../../design/07-api-contract.md#28-工作区) ·
> [06 §2.5](../../design/06-data-model.md#25-worktreelease) · [gitops](gitops.md) · [paths](paths.md)

> Written in English on purpose: it is a **new** document ([LANGUAGE.md](../../LANGUAGE.md) rule 3).
> The rest of the codemap is still Chinese and is translated as a set in phase 2.

## Responsibility

Orchestration for `worktree.acquire` / `worktree.list` / `worktree.release`: `repoId` → validate →
compute the path → ask git → map errors to stable codes → build the payload. It is the **only entry
point in S7 that writes to the user's repository**, yet it never runs git itself: every write lives in
[`gitops/worktrees.py`](gitops.md) (`add` / `remove` / `prune`, nothing else).

**Nothing is persisted** (S7 D4): the truth is `git worktree list --porcelain` plus "does the
directory exist", so this module writes no store rows, and after a successful `acquire`/`release` it
**re-reads the facts** instead of composing a "should be" HEAD. The `WorktreeLease` table is due in
S10 (the decision recorded in 06 §2.5).

## Symbols

| Symbol | Line | What it does | Used by |
|---|---|---|---|
| `WORKTREE_CONFLICT` | 40 | The code S7 **adds** (409): directory occupied / worktree dirty / branch checked out elsewhere | `acquire`, `release`, page `ERROR_TEXT` |
| `MSG_*` | 44–56 | Error text (**never contains user input**, 05 §4.2) | this module |
| `_fail(code, message)` | 62 | Build a `FrameError` (the gateway turns it into an `ok:false` frame) | this module |
| `_same_path(candidate, target)` | 67 | git's reported path vs the path we want (both `resolve()`d) | `_find` |
| `_find(entries, target)` | 72 | Locate "our path" among git's records; `None` = not registered | `acquire`, `release` |
| `WorktreeService.list(params)` | 86 | `worktree.list`: workspaces of this repo that belong to this kernel | gateway `worktree.list` |
| `WorktreeService.acquire(params)` | 111 | Allocate (idempotent): reuse / repair / create | gateway `worktree.acquire` |
| `WorktreeService.release(params)` | 179 | Release (keeps the branch, never `--force`) | gateway `worktree.release` |
| `WorktreeService._require_repo(params)` | 222 | `repoId` shape/existence → `INVALID_PARAMS` / `REPO_NOT_BOUND` | all three methods |
| `WorktreeService._require_bot(params, repo)` | 232 | `botId` shape/existence/**ownership** (cross-repo is rejected) | all three methods |
| `WorktreeService._base_ref(params, repo)` | 247 | `baseRef` default + shape (reuses `domain.ref_name_ok`) | `acquire` |
| `WorktreeService._is_ours(repo, entry)` | 262 | Three filters: registered, directory exists, path under this repo's worktrees subtree with a valid bot name | `list` |
| `WorktreeService._payload(repo_id, entry, reused=)` | 271 | Entity → frame payload (7 fields; `list` passes `reused=None` so the field is absent) | all three methods |
| `WorktreeService._drop_empty_dir(target)` | 289 | After a failed create, remove **only** an empty directory (non-empty is never touched) | `acquire` failure path |

## Key decisions and why

### 1. Two booleans decide everything (S7 D7)

| Registered in git | Directory exists | `acquire` | `release` |
|---|---|---|---|
| yes | yes | reuse, `reused: true`, **zero writes** | `worktree remove` → `removed: true`; dirty → conflict |
| yes | no | `prune`, then create | `prune` → `removed: true` |
| no | no | create | `removed: false` (**not an error**) |
| no | yes (empty) | create (git accepts an existing empty dir, measured) | conflict (not ours) |
| no | yes (non-empty) | conflict, not a single byte touched | conflict |

The decision uses the filesystem and the **field prefixes** of `--porcelain` only, never git's
localized prose (`prunable gitdir 文件指向一个不存在的位置` is Chinese here; matching on text would
fail silently in another locale).

### 2. Why Bot ownership is checked

`botId` is the composite key `{repo_id}/{name}`. Without the ownership check, a `botId` from repo B
could allocate a workspace under repo A — the `name` part is whitelisted, so only "does this Bot
belong to this repo" can stop it.

### 3. `WORKTREE_CONFLICT` is a new code (registered in 07 §1.3 in the same PR)

All three triggers are **normal user situations**: an occupied directory, uncommitted work in the
worktree, a branch already checked out in another worktree. Mapping them to `INTERNAL` ("unexpected
error, report a bug") would be a lie — S4 D5 already refused that mapping for "the user deleted the
repo". `CONTEXT_CONFLICT` (409) is registered as "charter/AGENTS.md conflict"; widening it would
quietly change a contract someone else reads. So: a new code, at the price of one table row in 07 and
one `ERROR_TEXT` line in the page.

### 4. Failures only clean up an **empty** directory

`worktree add` sometimes creates the directory and then fails. `_drop_empty_dir` removes it only when
it is empty **and** did not exist before the call; anything non-empty survives (it may be half a
checkout, or the user's own files). 06 §5 ("never auto-delete user files") becomes one line of code.

## Before you change it

- **Do not widen the write surface.** Adding any git write requires editing the D5 allowlist in
  `S7-worktree-visible.md` and the module docstring of `gitops/worktrees.py`. Today: `add` / `remove`
  / `prune`.
- Changing a code → change `07-api-contract.md` §1.3 + §4.9 + `ERROR_TEXT` in `web/assets.py` in the
  same commit.
- Changing a payload field → the seven-field table in 07 §2.8 is the contract; `list` entries have
  **no** `reused` (reason in D1).
- "Allocate per `task_id`" (S10) lands here; that is when the `WorktreeLease` table appears.

## Tests

- `tests/test_worktree.py`: acceptance 1/3/4/5 + the D6/D7/D8 boundaries (35 cases, real git, real
  SQLite, real directories).
- `tests/test_reverse_checks.py`: S7 #1–#4 (whitelist / reuse / `--force` / baseRef shape).
- `tools/acceptance/s7_worktree.mjs`: real browser + real Chrome (allocate / reuse / dirty release
  refused / release / `BAD_BASE_REF` / Enter key / disconnect clears / both user repos untouched).
