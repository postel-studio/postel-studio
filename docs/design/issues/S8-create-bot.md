# S8 · Create a Bot → it appears in the left panel

- **Milestone** M2 · **Estimate** 3h · **Depends on** S6 (Bot + charter) · **Priority** P0
- **User path**: click **New Bot** → type a name → pick a permission → the bot appears in the left
  panel with its badge → reload the page → it is still there. Change its permission later from the
  charter panel; L3/L4 are not offered and are refused if asked for.

> Language: new document → English ([LANGUAGE.md](../../LANGUAGE.md) rule 3). Slice specs written
> before S6.6 stay Chinese and are not rewritten.

## 1. Why this slice, and what it refuses to do

M1 shipped a **preset** bot: `bot.list` seeded `builder` the first time a repo was opened, because
there was no way for a human to create one (S6 D2, recorded as U17 with "due S8"). This slice
replaces that with the real thing: a human names a bot and **grants its permission**. It is the
first time the permission model becomes visible to a person instead of being a constant in code.

Two consequences follow from deleting the seed, and both are part of this slice rather than
"follow-up work":

1. `bot.list` becomes a **pure read**. A repo with zero bots is now a real state the page must
   render, not a state the kernel papers over.
2. Every acceptance script that binds a repo and expects `builder` to be there is testing a
   contract that no longer exists. `scripts/accept_m1.py`, `tools/acceptance/s6_bots.mjs` and
   `tools/acceptance/s7_worktree.mjs` are re-baselined here (D10).

Out of scope, deliberately: `bot.delete`, renaming, editing the charter (`memory.charter.set` is
S22), choosing an agent channel (S9), **enforcing** permissions (S16 — this slice stores and shows
the level; nothing is blocked by it yet), multi-user, and any notion of a bot changing its own
permission (05 §2.3 forbids it; there is no such code path because there is no agent connection).

## 2. Decisions (D1–D11). These are contract; implementation may not re-decide them.

### D1 · Two methods, one payload — the S6 payload, unchanged

| Method | Params | Result |
|---|---|---|
| `bot.create` | `repoId`, `name`, `permission?`, `agentKind?` | `bot` |
| `bot.set_permission` | `botId`, `permission` | `bot` |

Both return the **exactly-eight-field** payload from S6 D1 / 07 §4.7 (`botId`, `repoId`, `name`,
`permission`, `agentKind`, `charterRev`, `charterSummary`, `memoryPath`). No new field, no
`createdAt` (the DB has it; the payload never had it and adding it now buys nothing).

Unknown extra keys in `params` are ignored, as everywhere else in this kernel.

### D2 · Permission: four assignable levels, two refused by name, one default

| Value | Rank | Means (01 §4) | Assignable in v1 |
|---|---|---|---|
| `L0` | 0 | read files, run read-only commands | yes |
| `L1` | 1 | edit its own worktree, commit to its bot branch | yes (**default**) |
| `L2` | 2 | push the bot branch to **this** repo | yes |
| `L2+` | 3 | open a PR to **this** repo | yes |
| `L3` | — | fork + PR to an **upstream** repo | **no** → `NOT_SUPPORTED` |
| `L4` | — | merge | **no** → `NOT_SUPPORTED` (the product never offers it) |

**Only these four form a ladder.** `L0 < L1 < L2 < L2+` (`permission_covers("L2", "L1") == True`).
`L3`/`L4` are **not** on it: 01 §4 opens with the reason — "merge" and "cross-repo PR" are different
axes, not higher rungs. So `permission_covers` answers `False` for anything unparsable, and nothing
in this slice orders `L2+` against `L3`.

**Default is `L1`.** This contradicts one line in `01-requirements §4`, whose table has the note
"新 Bot 默认" on the **L0** row. Resolution, with the evidence:

| Source | Says |
|---|---|
| `05-security §2.2` ("默认值", the doc that declares itself a hard constraint) | 新建 Bot → **L1** ("可改工作区，不可推送") |
| `06-data-model §2.2` | `permission` … **默认 L1** |
| `storage/db.py` v3 DDL | `permission TEXT NOT NULL DEFAULT 'L1'` |
| S8 stub, acceptance 4 | 默认权限为 L1 |
| `01 §4` L0 row note | 新 Bot 默认 (**stale**) |

Four sources against one, and L0 would ship a bot that cannot do the only thing a bot is for. `01
§4` is corrected in this PR (one cell, pointing at 05 §2.2) rather than left as a contradiction in
a hard-constraint document.

### D3 · Name: the S6 whitelist, one message that states the rule, nothing echoes the input

`^[a-z][a-z0-9-]{0,31}$` — `bot_name_ok`, unchanged since S6 (05 §3.3, 06 §2.2). Nothing new is
invented here; what is new is that it now guards a **user-facing** action, so:

- a rejected name is `INVALID_PARAMS` with the fixed message
  `名字不合法：1–32 位，小写字母开头，只能用小写字母、数字和连接符` — **the input is not echoed**
  (05 §4.2), and the message states the rule so the person can fix it without a doc;
- the check runs **before** any disk or DB write (D6), so "名字非法 → 什么都没创建" is literally true
  (no row, no directory);
- non-`str` (`null`, `42`, `[]`) is the same error, not an exception.

`abc-` (trailing hyphen) **passes**: the regex allows it and so does git's branch naming
(`bot/abc-/workspace`). Recorded because it looks like an oversight and is not one.

### D4 · `agentKind` is `null` until S9 verified a channel

`bot.create` accepts `agentKind` **absent or `null`** only; any string → `NOT_SUPPORTED`
(`第一版还不能指定 Agent 通道`). 07 §4.7 already says the payload's `agentKind` is `null` before S9
because "编一个适配器名等于宣称已支持" (04 §6 forbids adapter-specific knowledge in the generic
layer). Accepting a name we cannot verify would be exactly that lie; ignoring the parameter
silently would be a second one.

### D5 · One new error code: `ALREADY_EXISTS` (409)

A duplicate name is a normal user state that deserves its own sentence. The three options:

| Option | Why not |
|---|---|
| reuse `INVALID_PARAMS` | the page maps **codes** to text (it never shows server `message`s), so a legal-but-taken name would be reported as "名字不合法" — the UI would be forced to lie |
| silent upsert / return the existing bot | the human thinks a second bot was created; nothing distinguishes "created" from "already there" |
| **new code `ALREADY_EXISTS`** | 409, "the thing you asked to create already exists"; the page can say 这个仓库已经有叫这个名字的 Bot |

Same reasoning and same shape as S7's `WORKTREE_CONFLICT`: a normal state, not `INTERNAL`, not
overloaded onto a code that means something else. The table row, the `ERROR_TEXT` mapping and a PR
callout are all part of this slice (DoD §2.2). **Scope limit**: `ALREADY_EXISTS` is for create
collisions only — "updated a row that vanished" stays `INVALID_PARAMS`.

### D6 · Write order: reject before touching anything

`bot.create` runs, in this order, and the first failure stops it:

1. `repoId` shape → `INVALID_PARAMS`; unknown → `REPO_NOT_BOUND` (S6 D6, unchanged);
2. `name` shape → `INVALID_PARAMS` (D3);
3. `permission` (D2) → `NOT_SUPPORTED` / `INVALID_PARAMS`;
4. `agentKind` (D4) → `NOT_SUPPORTED`;
5. duplicate pre-check (`BotStore.get(bot_id)`) → `ALREADY_EXISTS`;
6. `ensure_memory_space` → `INTERNAL` on `OSError` (creates dir + `charter.md` from the template);
7. `BotStore.insert` → `sqlite3.IntegrityError` → `ALREADY_EXISTS`.

Why this order: 1–5 cannot touch the disk at all, so every rejection in the table above leaves
**nothing** behind; step 6 is idempotent and never overwrites an edited charter (S6 D3), so a
failure at 7 leaves at most a memory directory with a template charter and no row — invisible to
the user, and reused if they create the same name again.

The `IntegrityError` → `ALREADY_EXISTS` mapping is a **today**-claim: the only realistic cause is a
concurrent duplicate (two frames, one lock on each side), because step 1 already proved the repo row
exists and `repo.unbind` is not implemented. S22's unbind must revisit this (a vanished repo would
surface as `IntegrityError: FOREIGN KEY constraint failed`, **measured** — see §8.3).

### D7 · `bot.list` stops writing (U17, first half — closed here)

Delete `_seed` and `PRESET_BOT_NAME`. `bot.list` on a repo with no bots returns `[]` and writes
nothing: no row, no directory, no `chmod`. Measured before/after in §8.1.

The page's empty state (`这个仓库还没有 Bot。`) and the new create form are what make `[]` a usable
answer instead of a dead end. The workspace card (S7) already has the phrase for "no bot"; it gains
a pointer to the action: `Bot：（还没有 Bot，先新建一个）`.

**U17's second half stays open**: `bots.charter_rev` is written once at creation and not maintained
afterwards, because editing the charter is S22's `memory.charter.set`. The payload's `charterRev` is
computed from the file, so nothing reads the stale column — it is a snapshot for `doctor`, and S22
must either maintain it or drop it. Recorded in `06-pitfalls`, not silently left.

### D8 · Page contract (DOM ids are contract)

Left panel, `#bots` (existing section):

| id | Element | Behaviour |
|---|---|---|
| `bot-new` | `<button type="button">新建 Bot</button>` | visible **only** in `ready`; opens the form, clears it, moves focus to `bot-create-name` |
| `bot-create-form` | `<form autocomplete="off" hidden>` | submit = create; `Enter` in the name field submits (real form) |
| `bot-create-name` | `<input type="text">` | `aria-label="Bot 名字"` |
| `bot-create-permission` | `<select aria-label="权限">` | exactly four options, see below |
| `bot-create-submit` | `<button type="submit" disabled>` | enabled only while the form is open and no creation is in flight |
| `bot-create-cancel` | `<button type="button">取消</button>` | closes, clears, returns focus to `bot-new` |
| `bot-create-error` | `<p role="alert" hidden>` | code-mapped text (below) |

Permission select — **exactly these four, in this order, `L1` selected**:

```html
<option value="L0">L0 · 只读</option>
<option value="L1" selected>L1 · 改工作区（默认）</option>
<option value="L2">L2 · 推分支</option>
<option value="L2+">L2+ · 开本仓库 PR</option>
```

No `L3` / `L4` option — not even disabled (the stub's forbidden list; a greyed-out "L4 merge" would
still teach people it exists).

Flow:

- open → `hidden` removed, name cleared, permission reset to `L1`, error cleared, name focused;
- cancel / `Escape` → `hidden`, error cleared, focus back on `bot-new`;
- success → form closes, `loadBots()` re-reads the list (the server is the ordering source; the page
  does not splice the returned row into the list), focus back on `bot-new`;
- failure → form stays open, values kept, `bot-create-error` shows the mapped text, submit
  re-enabled.

Charter panel, `#charter` (existing section) gains one control, visible only in `ready`:

| id | Element | Behaviour |
|---|---|---|
| `charter-permission` | `<select aria-label="权限">` | same four options; `change` → `bot.set_permission` |

- success → the select keeps the new value and `loadBots()` refreshes the badge in the left panel
  (one fact, one place: the badge is rendered from `bot.list`);
- failure → the select is **reverted** to the value the server still has, and `#charter-error`
  shows the mapped text. A control that visibly claims a change the kernel refused is worse than an
  error line.

Code → text, both flows (a per-panel map, like `charterErrorText` does for `INVALID_PARAMS`; the
global `ERROR_TEXT` is untouched because its `NOT_SUPPORTED` text is about non-UTF-8 ref names):

| Code | Text |
|---|---|
| `INVALID_PARAMS` (create) | 名字不合法：1–32 位，小写字母开头，只能用小写字母、数字和连接符 |
| `INVALID_PARAMS` (set_permission) | 找不到这个 Bot |
| `ALREADY_EXISTS` | 这个仓库已经有叫这个名字的 Bot |
| `NOT_SUPPORTED` | 第一版不支持这个权限级别（L3 / L4） |
| `REPO_NOT_BOUND` | 仓库未绑定或已不存在 |
| anything else | 新建 Bot 失败 / 改权限失败 |

All text via `textContent` (the page has zero `innerHTML`, T9). The badge already renders
`bot.permission` as text since S6, so `L2+` needs no special casing — checked in the browser script.

### D9 · A dead repo path does not block creation

`bot.create` writes only under `<state root>/bots/…`; it never reads git. So it does **not** call
`is_usable_repo` (S5 D7 #4 / S7's check), unlike `repo.diff` and `worktree.acquire` — those ask git
a question, this one does not.

That is not sloppiness: `bot_id` is `{repo_id}/{name}` and `repo_id` is derived from the **path**
(S3.5 D1), so re-binding the repo at the same path restores the same id and the bot becomes usable
again. The row is not garbage; refusing to create it would just make the human wait.

### D10 · The acceptance scripts are re-baselined, not left green by accident

| Script | Change | Why |
|---|---|---|
| `scripts/accept_m1.py` | S6 step: assert `bot.list` on a fresh repo is `[]`, then `bot.create` (`builder`, `L1`), then `memory.charter.get` | it asserted the seed; the seed is gone |
| `tools/acceptance/s6_bots.mjs` | creates `builder` through the new form before asserting the left panel / charter panel | same |
| `tools/acceptance/s7_worktree.mjs` | same setup (it asserts `Bot：builder`) | same |

These are executable acceptance of **today's** product, not historical records: the M1 script keeps
proving "a human can read a bot's charter", it just stops pretending the kernel invented the bot.

### D11 · No schema migration

`bots` exists since v3 (S6 D7): `PRAGMA user_version` is already 3 and the DDL is in
`_MIGRATIONS[2]`. This slice adds **no** column and **no** table, so `SCHEMA_VERSION` stays 3 and
the v2→v3 upgrade test keeps its meaning. A create on an upgraded v2 database is covered by the
existing migration test plus one call.

## 3. Acceptance, each with the command that proves it

```bash
# 1 · create → the row is in the list, with the badge the human picked
#   (WS frame; the service tests assert the same thing in-process)
{"method":"bot.create","params":{"repoId":"<id>","name":"builder","permission":"L2"}}
#   → ok:true, data.{botId,name,permission} == ("<id>/builder", "builder", "L2")
#   → followed by bot.list → exactly one row, same eight fields

# 2 · an illegal name creates nothing
{"method":"bot.create","params":{"repoId":"<id>","name":"Builder"}}
#   → error.code == "INVALID_PARAMS"; then: bots table 0 rows and <state root>/bots/<id> absent

# 3 · the select offers exactly four levels
#   node tools/acceptance/s8_create_bot.mjs → "L0/L1/L2/L2+, 无 L3/L4"

# 4 · default permission is L1
{"method":"bot.create","params":{"repoId":"<id>","name":"reviewer"}}   # no permission
#   → data.permission == "L1"

# 5 · creation also creates the memory space
#   <state root>/bots/<id>/<name>/{charter.md,memory,sessions} exist; charter.md == CHARTER_TEMPLATE
#   directory 0700, charter.md 0600 (umask 022, T38)

# 6 · reload keeps it
#   node tools/acceptance/s8_create_bot.mjs → page.reload() → the row is still listed

# 7 · permission can be changed, L3/L4 cannot
{"method":"bot.set_permission","params":{"botId":"<id>/builder","permission":"L1"}}
#   → data.permission == "L1"; bot.list shows L1
{"method":"bot.set_permission","params":{"botId":"<id>/builder","permission":"L3"}}
#   → error.code == "NOT_SUPPORTED"; the stored permission is still "L1"
```

## 4. Acceptance → test map (write the table first, then the code)

| # | Acceptance | Test |
|---|---|---|
| 1 | create → listed | `test_bot_create.py::test_create_returns_the_eight_field_payload` + `::test_create_then_list_shows_it`; browser `s8_create_bot.mjs` `1 …` |
| 2 | illegal name → nothing created | `::test_create_rejects_illegal_names[<6 shapes>]` + `::test_create_rejects_illegal_names_and_writes_nothing` |
| 3 | four options only | `::test_page_permission_select_lists_exactly_four_levels` (static) + browser `2 …` |
| 4 | default L1 | `::test_create_defaults_to_l1` + browser `1 … 默认选中 L1` |
| 5 | memory space at creation | `::test_create_makes_memory_space_with_template_charter` (+ `umask_022` fixture) |
| 6 | survives a reload | browser `s8_create_bot.mjs` `4 …` |
| 7 | set_permission, L3/L4 refused | `::test_set_permission_changes_the_value`, `::test_set_permission_rejects_l3_and_l4_without_writing`, `::test_set_permission_on_unknown_bot_is_invalid_params` |
| — | U17, first half | `::test_list_on_a_repo_with_no_bots_writes_nothing` (row count + no directory) |
| — | duplicate | `::test_create_duplicate_name_is_already_exists` + `::test_same_name_in_two_repos_is_allowed` |
| — | agentKind | `::test_create_accepts_null_agent_kind_and_refuses_a_string` |
| — | frame level | `::test_ws_bot_create_and_set_permission_frames` (real socket) |
| — | the M1 script still proves its path | `tests/test_accept_m1.py` (existing) after its re-baseline |

## 5. Boundary matrix (enumerated from 06 §2.2 and the frame contract, not from the happy path)

| Entity | States that must be covered |
|---|---|
| `name` | `builder` / `qa-1` / `a` / 32 chars · `Builder` / `b uilder` / `a_b` / `1abc` / `-abc` / 33 chars / `""` / `../x` · `null` / `42` / `[]` · duplicate in the same repo · the same name in **another** repo (allowed: the PK is composite) |
| `permission` | absent · `null` · all four legal values · `L3` · `L4` · `L9` · `l1` (lowercase) · `42` · `""` |
| `botId` (set) | missing · non-string · unknown · a bot of another repo (a valid `botId` — the method has no `repoId`) |
| repo | bound and present · `repoId` unknown · bound but the **path is gone** (create still works, D9) · bare repo |
| `bots` table | empty · one row · two rows in two repos · a v2 database upgraded to v3 |
| frame | `params` missing · extra unknown keys (ignored) · `ok:false` carries the code the page maps |

## 6. Files and budget

| Layer | File | What |
|---|---|---|
| L0 | `domain/models.py` | `Permission` (four values + rank), `permission_parse`, `permission_covers`, `UNASSIGNABLE_PERMISSIONS` |
| L0 | `storage/db.py` | `BotStore.set_permission(bot_id, permission) -> Bot \| None` |
| L3 | `api/bots.py` | `create` / `set_permission`, the validation order, `_split` reuse; **delete** `_seed` + `PRESET_BOT_NAME` |
| L3 | `api/gateway.py` | two providers, two dispatch branches |
| L3 | `api/app.py` | wiring |
| L4 | `web/assets.py` | create form + charter permission select + error maps |
| — | `tests/test_bot_create.py` (new), `tests/test_bot_charter.py` (de-seed), `tests/test_reverse_checks.py` (+5) | |
| — | `scripts/accept_m1.py`, `tools/acceptance/s6_bots.mjs`, `s7_worktree.mjs`, `s8_create_bot.mjs` (new) | D10 |

Estimate: **~420 `src/` lines / 7 files** (ceiling 1000 / 12, 04 §1.2 after the S7 recalibration).
The page is the largest part (~180), as in S6/S7.

## 7. Reverse injections (new hard constraints only)

| # | Inject | Must redden |
|---|---|---|
| 1 | `bot.create` skips the assignable check (accepts `L3`) | `test_create_rejects_l3_and_l4` |
| 2 | `bot.create` skips `bot_name_ok` | `test_create_rejects_illegal_names_and_writes_nothing` |
| 3 | default becomes `L0` instead of `L1` | `test_create_defaults_to_l1` |
| 4 | `bot.list` seeds again (writes when empty) | `test_list_on_a_repo_with_no_bots_writes_nothing` |
| 5 | the page's select gains an `L3` option | `test_page_permission_select_lists_exactly_four_levels` |

## 8. What was measured before writing this spec

### 8.1 U17's "before": `bot.list` writes

```text
绑定时 bots 行数: 0
bot.list -> 1 个
list 之后 bots 行数: 1
list 之后记忆目录存在: True | charter: True
```

Run on a fresh `POSTEL_HOME` with one real repo. This is the state D7 removes.

### 8.2 The name whitelist, exactly as it behaves

```text
'builder' -> True    'a'*32 -> True     'abc-' -> True     'a'*31 + '-' -> True
'a'*33    -> False   'Builder' -> False 'b uilder' -> False 'a_b' -> False
'1abc'    -> False   '-abc'   -> False  ''  -> False       ' '   -> False
'bö'      -> False   '../x'   -> False  'a/b' -> False     'a.b' -> False
```

`abc-` passing is real, not a typo in this document (D3).

### 8.3 Duplicate insert: which exception, and does it roll back

```text
重复 insert -> sqlite3.IntegrityError | UNIQUE constraint failed: bots.repo_id, bots.name
主键撞车   -> IntegrityError | UNIQUE constraint failed: bots.bot_id
行数: 1
```

So the transaction rolls back (`行数` stays 1), and a duplicate is an `IntegrityError` — but an
insert for a **non-existent repo** fails the same way with a different message
(`FOREIGN KEY constraint failed`, also measured), which is why D6 scopes the mapping to "today,
where the repo was just proven to exist".

### 8.4 The memory space a create must produce

```text
dir: <state root>/bots/0123456789ab/builder exists: True
children: ['charter.md', 'memory', 'sessions']
charter 前两行: ['# builder', '']
二次调用后 charter: # 手改过
```

`ensure_memory_space` is already idempotent and never overwrites an edited charter (S6 D3), so D6
step 6 only has to call it.

## 9. 05 §7 checklist (security)

- [x] New path handling goes through `paths.bot_memory_dir` → `resolve()` + whitelist + inside-root
      assertion (S6 D3, unchanged; the name is validated again in D3 before it gets there).
- [x] No new subprocess; no new dependency.
- [x] No new log line contains a path or user input.
- [x] No new credential path.
- [x] Permission decisions happen **before** execution — here there is nothing to execute yet; the
      stored value is what S16 will judge against (05 §2.2).
- [x] No UI text claims a guarantee: the panel says which level a bot has, not that it is enforced.

## 10. Forbidden

- Adding `L3` / `L4` to the select, even disabled.
- Any code path where a **bot** sets its own permission (there is no agent connection; do not
  prepare one).
- Silent upsert on duplicate names.
- Echoing the submitted name back in an error message.
- Making `bot.list` write anything, for any reason.
- Adding a field to the `bot` payload.

## 11. Definition of done

- [ ] `bash scripts/check.sh all` green; `build` run separately.
- [ ] `uv run python scripts/accept_m1.py` → `M1 OK` after the re-baseline.
- [ ] All five `tools/acceptance/*.mjs` pass in real Chrome (the four existing ones re-run because
      the page changed).
- [ ] Five reverse injections redden the named tests.
- [ ] Budget actually measured and reported, over or under.
- [ ] `07` (both method sections + the new code), `06` (U17), `03` (the form), `01` (the default
      cell), `codemap` (modules, capabilities, change impact, pitfalls) all updated in this PR.
