# Changelog

All notable changes to session-channel are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and the project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.2.0] — 2026-05-12

First open-source release. Eight CLI commands, dashboard, wrappers, and
supervisor are stable and verified in three-CLI (Claude Code / Codex /
Gemini) topologies.

### Added
- **`channel debate`** — N-round cross-CLI critique loop with optional
  synthesizer (`--synthesizer cli:pane`) producing a
  Consensus/Conflicts/Final-Direction summary.
- **`channel race`** — 1-prompt-to-N-workers dispatcher; each worker
  gets a unique `<base>-<cli>` task id, results land in the `tasks`
  topic, optional `--wait` blocks until all settle.
- **Streaming observability** — `PreToolUse` events publish a per-call
  `tool` tag to the `agents` topic. The dashboard shows
  `⚙ <last tool call>` on each agent card, independent of the 30s
  heartbeat throttle.
- **Worker supervised respawn** — `scripts/supervisor.py` (Cronicle-
  driven by default; systemd / launchd templates included). Scans
  `config.yaml relay_pool.workers`, re-launches wrappers whose panes
  have returned to a shell prompt for longer than `grace_seconds`.
- **Tasks topic failure + timeout policy** — `channel tasks` reconciles
  `assign` / `done` / `failed` and reports unresolved tasks past
  `--max-age`. `--mark-timeout` publishes a `timeout` event; retry
  policy is "Strategy A — no auto-retry, caller decides".
- **Codex CLI wrapper** (`wrappers/codex-with-channel.sh`) — announces
  on launch, sets a session-scoped `notify=` so every turn publishes
  a heartbeat, traps `EXIT INT TERM` to publish `leave`.
- **Gemini CLI wrapper** (`wrappers/gemini-with-channel.sh`) — same
  lifecycle as Codex; pre-allocates `--session-id` for cross-restart
  correlation.
- **Sample Python hook** (`examples/hooks/session_channel.py`) —
  stdlib-only, single-file, covers 5 lifecycle events including
  `UserPromptSubmit` inbox digest.
- **Deployment manifests** (`dist/Dockerfile`, `dist/docker-compose.yml`,
  `dist/session-channel.service`, `dist/com.operonlab.session-channel.plist`).
- **Per-CLI integration guides** (`docs/integrations/`) for Claude Code,
  Codex, Gemini, and a generic-CLI template.

### Changed
- All hard-coded `/Users/joneshong/...` paths replaced with three-tier
  resolution: `$SESSION_CHANNEL_HOME` env override > `$HOME/.session-channel`
  standard install > script-relative.
- Portable `#!/usr/bin/env python3` shebangs throughout.
- CORS `allow_origins` reads from `config.yaml.allowed_origins` (or
  `$SESSION_CHANNEL_ALLOWED_ORIGINS` env, comma-separated). Self origin
  auto-appended at startup.
- `supervisor.py` Python interpreter resolves via `sys.executable` (or
  `$SESSION_CHANNEL_PY` override) rather than a hard-coded path.
- `pyproject.toml` bumped to 0.2.0 with full PyPI metadata, MIT license,
  optional `[dev]` dependencies, and `channel` console_script.

## [0.1.0] — Earlier (internal)

Internal version inside the upstream workshop monorepo. Phases 1-4a
(send/read/topics/health/agents, tmux bridge, three-CLI bring-up).
Not released publicly.
