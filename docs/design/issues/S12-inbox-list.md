# S12 · 收件箱列表（空态、多条、状态标签）

- **里程碑** M2 · **预计** 3h · **依赖** S11 · **优先级** P1
- **用户路径**：点左栏仓库条目下的「变更收件箱」→ 看到该仓库所有候选（或真实空态）→ 按状态筛选 → 点一条进入 S11 详情

## 1. Scope

One new main-area panel `#inbox` and one entry point under each left-sidebar repo row.
No protocol method, no schema, no new store: `candidate.list` already accepts
`{repoId, taskId?, reviewState?}`, filters server-side, and orders `created_at DESC`
(S11). The only backend fact S12 consumes is `REVIEW_STATES` from `domain/models.py`.

Non-goals: review actions (approve/reject/push/PR) are S13; M2 end-to-end is S14;
per-panel empty/error polish is S18. No pagination (S12 禁止事项): the existing
`LIMIT 100` in `CandidateStore.list_for_repo` is a safety ceiling, not a pager.

## 2. Decisions

- **D1 — Q1 resolved: entry under the repo row (option ②).** 03 §7 requires the seven
  top-level tabs to match the reference *verbatim*; adding "变更收件箱" as an eighth
  tab breaks that acceptance criterion. The v2 Code page has no product sidebar —
  the right-hand About column is static description, not a navigation surface
  (`shots/v2/v2-code.png`). The left sidebar already has a proven, keyboard-accessible
  row pattern (`li[data-repo-id]`, `role=button`, click/Enter/Space → `selectRepo`,
  `assets.py:824-848`). Therefore: each repo row gains a small `变更收件箱` link with a
  candidate count; clicking it selects that repo (same `selectRepo`) and expands
  `#inbox`. One repo's inbox at a time — candidates are queried by `repoId`, so a
  per-repo entry matches the data model and avoids an unscoped global list.
- **D2 — list state machine**: `#inbox[data-state]` ∈ `idle` / `loading` / `ready` /
  `empty` / `error`. CSS-only show/hide (same discipline as every other panel);
  JS only changes `dataset.state` and `textContent`. Zero rows ⇒ `empty` with the
  literal `暂无待审变更` (not blank — 禁止事项). Error keeps a readable mapped message.
- **D3 — filter is a `<select>` of the five `REVIEW_STATES`** with an `all` option
  first. Sending `reviewState` to `candidate.list` reuses S11's validation
  (`INVALID_PARAMS` for an unknown value). Filter changes reload the list; no
  client-side filtering (one source of truth = server SQL).
- **D4 — row content**: title (derived, e.g. `候选 · <botName>`), Bot name,
  `reviewState` label (`local_only` → `仅本地`; the other four use 03 §4.2's
  `已推远端 / PR 待审 / 已批准 / 已拒绝`), `+X −Y · N 个文件`, relative time.
  All via `textContent` — zero `innerHTML` (site-wide static guard).
- **D5 — relative time is a pure helper** `relativeTime(epochSeconds, nowMs)` in the
  page JS: `<60s` → `刚刚`, `<60min` → `N 分钟前`, `<24h` → `N 小时前`, else `N 天前`.
  No `Intl` locale dependency; negative/future timestamps clamp to `刚刚`. Tested by
  the browser acceptance script with a freshly created candidate (→ `刚刚`).
- **D6 — click-through reuses S11**: clicking a row calls the existing
  `loadCandidate(taskId)` → `trackCandidate()` and scrolls `#candidate` into view;
  the diff still goes through `openCandidateDiff()` → S5 `#diff`. No second detail
  renderer, no second diff renderer.
- **D7 — no new U**: U7 (`encode_event` never called) is due before S12 and is
  already resolved in code by S10's gateway event pump; this slice only retires the
  stale row in `06-pitfalls.md`. The task.updated / candidate.proposed subscriptions
  already exist; S12 does not add a topic or change the payload.

## 3. Acceptance

1. List shows each candidate's title, Bot name, status label, `+/−`, relative time.
2. Zero candidates → `暂无待审变更`, not a blank panel.
3. Filter by `reviewState` works (only local / pushed / PR open), backed by the
   existing `candidate.list` parameter.
4. Multiple candidates ordered newest first (server `ORDER BY created_at DESC`).
5. Clicking a row opens the S11 detail (same `#candidate` card and `#diff` panel).
6. Entry point is under the repo row and does not add an eighth top-level tab.

## 4. Acceptance → test map

| # | Acceptance | Test |
|---|---|---|
| 1 | Row content (title/bot/state/stat/time) | `tests/test_inbox_list.py::test_payload_has_every_field_the_row_renders` (frame-level contract) |
| 2 | Empty state text | `tests/test_inbox_list.py::test_empty_repo_returns_no_rows_and_page_has_empty_state_text` |
| 3 | Filter by reviewState | `tests/test_inbox_list.py::test_review_state_filter_returns_only_matching_rows` |
| 4 | Descending order | `tests/test_inbox_list.py::test_rows_are_newest_first` |
| 5 | Click-through to S11 detail | `tests/test_inbox_list.py::test_row_task_id_feeds_existing_detail_loader` (JS contract) + `tools/acceptance/s12_inbox.mjs` |
| 6 | Entry under repo row, no eighth tab | `tests/test_inbox_list.py::test_no_new_top_level_tab_and_entry_uses_repo_row` + `tools/acceptance/s12_inbox.mjs` |
| — | Relative-time helper | `tests/test_inbox_list.py::test_relative_time_boundaries` |
| — | Zero innerHTML / all user data via textContent | existing `tests/test_page.py::test_page_never_uses_inner_html` (no change) |

## 5. Boundary changes (09 §2.3)

- No schema, no store, no dependency, no config, no new directory beyond one
  acceptance script. Only `web/assets.py` (DOM/CSS/JS) and docs change.
- `src/` delta expected ~130 lines (one panel + helpers); well under 1000 / 12.

## 6. Budget (04 §1.1: `src/` only)

Estimate **~150 lines / 2 files** (`web/assets.py` ~140, no backend change needed —
the S11 issue's "app.py supports reviewState" was already delivered by S11).
Cap 1000 / 12; zero new public methods. Measured numbers go in the PR body.

## 7. Reverse injections

- **S12 #1**: remove the `empty` state branch (show blank when zero rows) →
  `test_empty_repo_returns_no_rows_and_page_has_empty_state_text` must go red.
- **S12 #2**: add an eighth top-level tab →
  `test_no_new_top_level_tab_and_entry_uses_repo_row` must go red.
- **S12 #3**: filter client-side by ignoring the `reviewState` request parameter →
  `test_review_state_filter_returns_only_matching_rows` must go red (guards D3).

## 8. Measured evidence

```text
$ grep -n '7 个一级 tab' docs/design/03-ui-spec.md
14:- 一级 tab 取 7 个：`Code / Issues / Pull requests / Actions / Projects / Security and quality / Insights`，**不含 Agents**。
277:- 7 个一级 tab 名称与参照页**逐字一致**。

$ grep -n 'data-repo-id\|role.*button\|keydown' src/postel_studio/web/assets.py | head
824:      li.dataset.repoId = repoId;
825:      li.tabIndex = 0;
826:      li.setAttribute("role", "button");
...
$ sed -n '110,127p' src/postel_studio/candidates.py   # S11 already filters + validates
```

`shots/v1/v1-inbox.png` shows the v1 tab entry; `shots/v2/v2-code.png` shows the v2
seven-tab baseline with no inbox entry and no product sidebar — both inspected.

## 9. Non-goals / open

- S13: approve / reject / push / open PR buttons (and their status transitions).
- S18: remaining per-panel empty/error copy polish; S14: M2 full-path acceptance.
- U7 row in 06-pitfalls is stale (code has had a caller since S10) — retire here.
