<!--
Fill this in against docs/design/09-slice-dod.md. An empty item gets "n/a + reason" — do not
delete rows. Numbers must be pasted from commands, never from memory (09 §3.4).
-->

## User path

One sentence: what does the user do → what do they see?

## Acceptance → test map

| # | Acceptance criterion | Test |
|---|---|---|
| 1 |  | `tests/test_x.py::test_y` |

## Contract changes

- New / changed fields:
- Error codes: existing codes from `07-api-contract.md §1.3` only (yes / no — if no, call it out here)
- DOM ids (page changes only):

## Evidence (paste the **tail of real output**, never just "passing")

```text
# tail of `bash scripts/check.sh all` (passed count + coverage)
```

```text
# `node tools/acceptance/<x>.mjs` CHECK ✓/✗ counts (including regressions of existing scripts)
```

## Budget (see [`04-task-breakdown.md §1.1`](docs/design/04-task-breakdown.md); counts `src/` only)

- `src/`: N files / +A −B (limits: 12 files / 700 lines)
- Full `git diff --stat`:

## Not done / not verified (**separate the two**)

- Not done:
- Not verified (including any SKIPped acceptance script — say why):

## Open-question changes

- New U:
- Closed U:

## Self-check (maps to 09)

- [ ] Spec is the first commit of the branch, with executable evidence (§1.1)
- [ ] Every acceptance criterion has a test; map above (§1.2)
- [ ] Boundary matrix covers the data-model states, not just the happy path (§1.3)
- [ ] The open questions this slice depends on are ruled (§1.4)
- [ ] New hard constraints come with reverse injection (§2.1)
- [ ] No new error codes (§2.2)
- [ ] `conftest`, CI and dependencies were not changed silently (§2.3)
- [ ] `bash scripts/check.sh all` green; `build` run separately (§3.1)
- [ ] Frontend touched → **all** `tools/acceptance/*.mjs` re-run (§3.2)
- [ ] Every number pasted from a command (§3.4)
- [ ] `docs/codemap/` synced and the `docs` / `layers` gates are green
