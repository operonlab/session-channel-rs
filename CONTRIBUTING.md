# Contributing to session-channel

Thanks for your interest! session-channel is a small, focused project — a
cross-pane / cross-CLI pub-sub bus over tmux + Redis Streams. The scope is
deliberately narrow; please read the **Scope** section below before
investing time in a large patch.

## Scope

### In scope
- The eight-command CLI (`send`, `read`, `topics`, `health`, `agents`,
  `tasks`, `race`, `debate`)
- The Redis-Streams store, FastAPI service, and single-file dashboard
- Wrappers for major CLIs (Claude Code, Codex, Gemini) and the generic
  shell template
- The supervisor (Cronicle / systemd / launchd) and lifecycle hooks
- Documentation: README, integration guides, examples

### Out of scope
- Mobile UX — that belongs in a separate dashboard project
- Resource governance / OOM protection — that's the operator's
  monitoring stack
- Persistent message storage beyond TTL — session-channel is **ephemeral
  coordination**; anything that matters long-term should land on disk
  elsewhere (handoff files, git, etc.)
- Typed schemas / formal protocol — 5 commands stable since v0.1; YAGNI

If your idea falls outside scope, that does not mean it's a bad idea —
it just means it should live in its own project that builds **on top of**
session-channel.

## Quickstart for contributors

```bash
git clone https://github.com/operonlab/session-channel
cd session-channel
make dev          # creates .venv with editable install + dev deps
make test         # pytest tests/
make lint         # ruff check
make run          # uvicorn on 10101 with --reload
```

## Pull request checklist

- [ ] One logical change per PR (split unrelated work)
- [ ] `make lint` clean
- [ ] `make test` passes (or, if you change behaviour, add/update tests)
- [ ] README / integration docs updated when public behaviour changes
- [ ] Commit messages follow the existing pattern: `<type>(<area>): <subject>`
      where `<type>` is one of `feat | refactor | docs | build | fix |
      test | chore`
- [ ] For new public CLI commands or HTTP routes, add a short note to
      `CHANGELOG.md`

## Reporting bugs

Open an issue with:

1. What you ran (full command)
2. What you expected
3. What you saw (paste output)
4. Versions: `session-channel --version`, Redis version, Python version, OS

If the issue depends on a specific CLI agent (Claude Code / Codex /
Gemini), include the agent's version too.

## Code style

- Python 3.12+, type hints encouraged but not required
- `ruff` is the linter of record; configuration lives in `pyproject.toml`
- Shell scripts: `set -u` at minimum; `bash -n` your changes
- No new runtime dependencies without discussion — the dependency surface
  is intentionally small

## Security

Do not file security-sensitive issues in public. Email the maintainer
(see `pyproject.toml` `[project]` authors) with a description and
reproducer.

## License

By contributing, you agree that your contributions will be licensed under
the project's [MIT License](LICENSE).
