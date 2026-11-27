# S13 · 批准 / 拒绝 → 状态变化

- **里程碑** M2 · **预计** 3h · **依赖** S11,S12 · **优先级** P1
- **用户路径**：点候选 → 点「批准」或「拒绝并说明」→ 条目状态变化并从默认列表消失/保留
- **边界**：只做 `approve` / `reject` 两个动作。`push` / `open_pr` 属 S16（权限门 + 05 §7 的
  git 写面），本刀**不画**那两个按钮，`03 §4.2` 的四按钮顺序由 S16 补齐。

## 1. Scope

Two WS methods (`candidate.approve`, `candidate.reject`), one pure review-state machine,
one v6 schema migration adding `reject_reason`, one UI action block on `#candidate`, and
the `candidate.updated` event that 07 §3 already registered but no slice has emitted yet.
No merge entry in any form (L4 is never granted to a Bot; humans sign off).

Non-goals: push / open_pr (S16); M2 full path (S14); shared `permission.check` frame and
server-side permission enforcement (S16 — 07 §4.10 says permissions are stored/displayed
but not enforced until then). S13's UI gating is a **hint**, not a security boundary.

## 2. Decisions

- **D1 — review state machine is a pure function.** `CANDIDATE_TRANSITIONS` +
  `review_transition_ok()` in `domain/models.py`, shaped like the Task machine
  (`TASK_TRANSITIONS` / `task_transition_ok`). Legal per 06 §3.2:
  `local_only → approved` (explicit S13 acceptance 1 — approve does **not** require a
  prior push/PR in MVP), `pushed → approved`, `pr_open → approved`, any non-terminal →
  `rejected`. **Illegal**: `rejected → approved`, `rejected → rejected`, any transition
  out of `approved`, and `rejected → pushed`. No `→ merged` anywhere.
- **D2 — reject reason persisted in a new column**, not a side file: v6 migration adds
  `candidates.reject_reason TEXT` (nullable, default `NULL`). `Candidate` gains
  `reject_reason: str | None = None` (defaulted so S11 fixtures and `_payload` stay
  compatible). `candidate.reject(candidateId, reason?)` trims the reason, caps it at
  2000 chars (UTF-8 bytes for the cap), stores it **atomically with** the state change
  (`UPDATE … SET review_state=?, reject_reason=? WHERE candidate_id=? AND review_state=?`,
  race loser → `INVALID_PARAMS` since the row changed under the caller).
- **D3 — legal transitions use the store's atomic update**, same discipline as
  `TaskStore.transition` and T44: `with self._lock, self._conn`, `UPDATE … WHERE
  review_state = <from>`, check `cursor.rowcount`. No read-then-write without the WHERE.
- **D4 — permission hint in the UI**: the page already has `botPermissions[selectedBotId]`
  from `bot.list`. `approve` / `reject` buttons are `disabled` when the selected Bot's
  permission is not ≥ L1 (same floor as dispatch, `DISPATCH_MIN_PERMISSION`), with a
  visible reason (`需要 L1 以上，当前 L0`). Server-side enforcement stays S16; if a
  crafted frame bypasses the page, S13 still enforces the **state machine** (D1) but not
  permission — recorded in the PR's Not done section, not hidden.
- **D5 — no merge button**: static test asserts neither the page source nor any button
  label contains `合并` as an action. The word may appear in explanatory prose only if
  prefixed with `不提供`; the test checks button/label text, not arbitrary comments.
- **D6 — `candidate.updated` event** with the registered payload `{candidateId,
  reviewState}` (07 §3), published after a successful transition and delivered through
  the existing per-connection pump. Page subscribes to it, refreshes both `#inbox` and
  `#candidate`, and the reason appears in the detail card when `rejected`.
- **D7 — default inbox hides `rejected`** (S13 acceptance 2): `loadInbox()` sends
  `reviewState=local_only` when no explicit filter is chosen; the row is still visible
  under the `已拒绝` filter. This is a UI choice, not a store filter (the server already
  supports arbitrary reviewState).
- **D8 — U items**: none due ≤ S13 remains open (checked 06-pitfalls: U4→S14, U1/U10/U20
  →S15, U16→S18). S13 does not depend on an unresolved U.

## 3. Acceptance

1. Approve changes state to `approved`; UI reflects immediately (event + reload).
2. Reject changes state to `rejected`; it disappears from the default list but is visible
   under the `已拒绝` filter.
3. Reject reason is persisted and shown in the detail card.
4. No merge button in any form.
5. Insufficient permission → action button disabled with a visible reason.

## 4. Acceptance → test map

| # | Acceptance | Test |
|---|---|---|
| 1 | Approve → `approved`, UI immediate | `tests/test_review.py::test_approve_legal_and_event_reaches_subscriber` + `s13_review.mjs` |
| 2 | Reject → `rejected`, hidden by default | `tests/test_review.py::test_reject_hides_from_default_list` + `s13_review.mjs` |
| 3 | Reason persisted | `tests/test_review.py::test_reject_reason_survives_store_reopen` |
| 4 | No merge button | `tests/test_review.py::test_no_merge_button_anywhere` |
| 5 | Permission hint | `tests/test_review.py::test_permission_hint_disables_button` + `s13_review.mjs` |
| — | State machine legality | `tests/test_review.py::test_review_transition_matrix` (`rejected→approved` refused) |
| — | Race loser | `tests/test_review.py::test_transition_loser_does_not_overwrite_reason` |
| — | Reason cap | `tests/test_review.py::test_reason_is_capped_and_trimmed` |

## 5. Boundary changes (09 §2.3)

- Schema v6 (`reject_reason` column, additive); `Candidate` entity + 1 field.
- WS method table 19 → 21; `KNOWN_TOPICS` 2 → 3.
- New L3 service methods on `CandidateService` (no new module, no new store).
- No dependency, no CI/config change, no new directory beyond one acceptance script.

## 6. Budget (04 §1.1: `src/` only)

Estimate **~450 lines / 6 files** (models ~50, db/v6+store ~90, candidates service ~90,
gateway/app/frames ~35, assets ~150). Cap 1000 / 12; two new public methods
(`candidate.approve` / `candidate.reject`) — at the 3-method dike limit. Measured numbers
go in the PR body.

## 7. Reverse injections

- **S13 #1**: make `review_transition_ok()` always return `True` →
  `test_review_transition_matrix` must go red. Guards D1.
- **S13 #2**: drop `reject_reason` from the atomic UPDATE (read-then-write without WHERE)
  → `test_transition_loser_does_not_overwrite_reason` must go red. Guards D3.
- **S13 #3**: add a `合并` button/label to the page →
  `test_no_merge_button_anywhere` must go red. Guards D5.

## 8. Measured evidence

```text
$ grep -n "push\|open_pr" docs/design/04-task-breakdown.md | head
181:| S16 | 只读 Bot 尝试 push → 命令被拦，磁盘上无副作用 | 4h | S10 |

$ sed -n '178,194p' docs/design/06-data-model.md
local_only ─push→ pushed ─pr→ pr_open ─approve→ approved
     │                       │
     └────reject─────────────┴──> rejected
（没有 → merged 迁移；任意 → rejected 不删分支）

$ grep -n "candidate.updated" docs/design/07-api-contract.md
172:| candidate.updated | 审核状态变化 | {candidateId, reviewState} |
```

The v5 `candidates` table has no reason column, and `CandidateService._payload` has no
`botId`/`permission` — measured against `storage/db.py:118-138` and `candidates.py:131-148`.
Hence v6 and the UI hint described above.

## 9. Non-goals / open

- S16: `push` / `open_pr` buttons and true server-side permission enforcement; U20
  (synchronous dispatch) due before S15.
- S14: M2 end-to-end acceptance (dispatch → candidate → approve in one browser flow).
- Management adjudication from the PR #24 review: recompute S7–S14 `src/` budget and
  decide the 1000-line cap. S13 itself is estimated well under the cap.
