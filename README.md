# Postel Studio

**A repository-first Agent workbench.**

Postel Studio is the control plane for repo-resident bots. Humans dispatch work, review diffs and
sign off; bots live inside the repository, claim work from the repository event stream, and produce
branches, checks and pull requests — not chat replies.

- **Repository is the first view**, not a sidebar of an editor.
- **Bots are first-class citizens** of a repo: identity, memory, isolated git worktree, work log.
- **Pluggable agents**: the product is an agent adapter layer, not another agent kernel.
- **Local execution first**, optional remote sandbox. Git platform agnostic.

> Status: early skeleton (`v0.1`). CLI entry point only; no product features yet.

## Install

```bash
pip install postel-studio
```

## Layout

```text
src/postel_studio/__init__.py    # main() -> prints a greeting
```

## Development

```bash
uv sync                 # create .venv, install (editable)
uv run postel-studio    # run the CLI entry point
uv build                # build sdist + wheel into dist/
```

Naming convention: distribution `postel-studio` (hyphen), importable package `postel_studio`
(underscore) — this mirrors the upstream repo layout.

## Quality gates

```bash
bash scripts/check.sh    # lint + format + types + tests/coverage + secret scan
```

See [CONTRIBUTING.md](CONTRIBUTING.md).

## Design

The product is designed before it is built. Everything below lives in [`docs/design/`](docs/design/):

| Doc | Contents |
|---|---|
| [01-requirements](docs/design/01-requirements.md) | Positioning, 15 settled decisions, permission model, memory layers, integration facts |
| [02-architecture](docs/design/02-architecture.md) | Process model, Cordis-style modules, unified Bot runtime interface |
| [02b-protocol](docs/design/02b-protocol.md) | Frontend↔kernel and kernel↔PI frame contracts |
| [03-ui-spec](docs/design/03-ui-spec.md) | GitHub-faithful repository UI spec |
| [04-task-breakdown](docs/design/04-task-breakdown.md) | Milestones M1–M3, 21 vertical slices |
| [issues/](docs/design/issues/) | One file per slice: background, acceptance, files, tests, prohibitions |
| [prototype/](docs/design/prototype/) | Clickable HTML prototypes (open in a browser) |

## Links

- Source: https://github.com/postel-studio/postel-studio (private)
- 中文说明: see [README.zh.md](README.zh.md)

## License

[MIT](LICENSE) © 2026 MaoZhiqin
