# Language policy

**Canonical language: English for repository artifacts.** Written so that a contributor who
reads only English can onboard, change code, and review a PR without hitting a wall.

The repository was built in Chinese (design notes, code maps, slice specs, commit history).
That corpus is **not** being thrown away or silently rewritten — see [Migration](#migration).

## Rules

1. **Commit messages are English** (S6.6 onward, Conventional Commits). History before that
   stays Chinese; **do not rewrite history** — PR references (`#7`, `#13`, …) point at commits.
2. **Pull request descriptions are English** (S6.6 onward). Review discussion may be in any
   language the maintainers share.
3. **New documents are English.** A document keeps the language it was authored in until it is
   translated *as a whole*.
4. **No parallel bilingual copies** of the same document — one canonical text, one language.
   The single exception is the storefront pair [`README.md`](../README.md) /
   [`README.zh.md`](../README.zh.md): it is small, stable, and deliberately paired.
5. **Translation is not editing.** A translated document must keep the same facts, the same
   numbers (coverage, line counts, timings) and the same accepted/rejected decisions. "Fixing a
   number while translating" forges the record. If a fact is wrong, fix it in a separate,
   non-translation commit.
6. **Code comments and docstrings are not a translation project.** They are opportunistic: when
   you are already editing a module for another reason, English is welcome. A dedicated
   all-comments translation PR is rejected — there is no test that can prove it correct.

## Why not "translate everything now"

The Chinese corpus is not a manual, it is an **incident log**. Every entry in
[`06-pitfalls`](codemap/06-pitfalls.md) is attached to a real revert (a `.git` directory that was
silently rebound to an outer repo, a marker whose single space is truthy, inline JS that broke only
in a real browser). The value is in the precision of the wording; a
documentation-only translation has no executable evidence, so a mistranslation is worse than the
original — it reads authoritative and is wrong.

## Migration

| Phase | What | When |
|---|---|---|
| 1 | Policy lands: English commits/PRs, English for new docs, this file | done (S6.6) |
| 2 | Translate the **onboarding path**: `CONTRIBUTING.md`, `docs/design/{02-architecture,04-task-breakdown,09-slice-dod}.md`, `docs/design/README.md`, `docs/codemap/{README,01-overview,02-layers}.md`, `docs/codemap/03-modules/README.md` | after M2 |
| 3 | Historical records stay Chinese: slice specs under `docs/design/issues/`, `06-pitfalls` T/U tables, old ADR-style notes. They are evidence of how decisions were made; an English index summarises them | not scheduled |
| — | Code comments/docstrings | opportunistic only |

## What the gates already protect

Translation cannot quietly break the repo: [`scripts/gate-docs.py`](../scripts/gate-docs.py)
checks relative links, `#anchor` slugs, `#L<n>` line references, the module line-count table and
page-id consistency — so translating a heading that another doc links to fails the gate until the
link is updated. The module index row format (`| L0 | [x](x.md) | \`src/…\` | 197 | … |`) must keep
its first four columns; the description column is free text.
