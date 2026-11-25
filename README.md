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

## Links

- Source: https://github.com/postel-studio/postel-studio (private)
- 中文说明: see [README.zh.md](README.zh.md)

## License

[MIT](LICENSE) © 2026 MaoZhiqin
