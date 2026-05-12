# session-channel

> Cross-pane, cross-CLI pub-sub bus over **tmux + Redis Streams** — give every coding agent (Claude Code / Codex / Gemini / generic) a shared inbox so they can race a prompt, debate an answer, or simply tell each other "task done".

`session-channel` runs as a tiny FastAPI service on `localhost:10101`. It exposes:

- A **CLI** (`channel send`, `channel read`, `channel race`, `channel debate`, …) for humans and scripts
- An **HTTP API** + **SSE stream** for live dashboards
- A **wrapper layer** that turns any CLI agent into a "session-channel worker" (announce on launch, heartbeat while idle, leave on exit)
- A **supervisor** that respawns crashed worker panes via Cronicle / systemd

It is the orchestration plane behind the "blog tmux-as-bridge" pattern: every tmux pane becomes an addressable node, and you can dispatch / race / debate across them without writing custom IPC code.

---

## Quickstart

```bash
# 1. Clone + install
git clone https://github.com/operonlab/session-channel ~/.session-channel
cd ~/.session-channel
./install.sh          # creates venv, installs deps, symlinks `channel` into ~/.local/bin

# 2. Start Redis (any 5.0+ works; sample compose included)
docker compose -f dist/docker-compose.yml up -d redis

# 3. Start the channel service
python3 -m uvicorn main:app --host 127.0.0.1 --port 10101

# 4. Smoke test
channel send broadcasts "hello from $(hostname)"
channel read broadcasts --count 5
channel topics
```

Open `http://localhost:10101/` for the live dashboard.

---

## Why session-channel?

| Existing tool | What it does | What it doesn't do |
|---|---|---|
| `tmux send-keys` | Push a line into another pane | No reply, no visibility, no fan-out |
| `redis-cli xadd` | Append to a stream | No tmux awareness, no agent lifecycle |
| MCP servers | Tool-call to a specific server | Single in-process, no cross-pane bus |
| Maestro / dispatcher | Spawn parallel headless agents | Each run is fresh; no persistent pool |

`session-channel` is the missing **persistent relay-pool with observability**:

- Workers stay alive across many tasks (re-use long-lived sessions, keep model warm)
- Every cross-pane message lands in a single Redis Stream you can `read` or visualise
- One command races / debates across N CLIs — no per-CLI glue

---

## Architecture

```
                 ┌──────────────────────────────────────────────┐
                 │  Dashboard  (FastAPI + SSE, port 10101)      │
                 │  - live agent cards (CLI / last tool / msg)  │
                 │  - topic browser                             │
                 └─────────────────┬────────────────────────────┘
                                   │
                                   │ HTTP + SSE
                                   │
                 ┌─────────────────┴────────────────────────────┐
                 │              Redis Streams                   │
                 │ topic=agents   topic=tasks   topic=handoffs  │
                 │ topic=broadcasts   topic=sessions  …         │
                 └───┬───────────────────┬─────────────────┬────┘
                     │XADD/XREAD          │                 │
                     │                    │                 │
       ┌─────────────┴──┐  ┌──────────────┴──┐  ┌───────────┴──┐
       │  channel CLI   │  │  Wrappers       │  │  Supervisor  │
       │  (humans +     │  │  codex-with-…   │  │  (Cronicle / │
       │   scripts)     │  │  gemini-with-…  │  │   systemd)   │
       └────────┬───────┘  │  hook handler   │  └──────────────┘
                │          └────────┬────────┘
                │                   │
                ▼                   ▼
        tmux panes ─────────────────► CLI agents (Claude Code / Codex / Gemini / …)
                                      announce / heartbeat / leave
```

The CLI is a thin wrapper around Redis XADD/XRANGE; the dashboard reads the same streams. Wrappers translate CLI-specific lifecycle (Codex `notify` hook, Gemini `--session-id`, Claude Code hooks) into the same `agents` topic events, so the dashboard is CLI-agnostic.

---

## CLI Reference (8 commands)

```bash
channel send <topic> <message> [--tag T] [--meta JSON] [--notify-target %P]
channel read <topic> [--count N] [--oldest]
channel topics
channel health
channel agents [--within SECONDS]
channel tasks [--pending] [--max-age SECONDS] [--mark-timeout]
channel race "<prompt>" --task-id <id> --workers cli:pane,cli:pane,…
channel debate "<question>" --debate-id <id> --participants A:cli:pane,B:cli:pane \
               [--rounds 3] [--synthesizer cli:pane]
```

| Command | Use case |
|---|---|
| `send` | Publish to any topic. `--tag` carries a verb (`announce`, `done`, `assign`, `tool`, …); `--meta` carries a JSON sidecar; `--notify-target` pushes a wakeup line into a pane via `tmux send-keys`. |
| `read` | XRANGE/XREVRANGE the topic stream. Default: newest N. `--oldest` for replay. |
| `topics` | List every Redis stream currently tracked. |
| `health` | Round-trip ping to the FastAPI service + Redis. |
| `agents` | Snapshot of "panes currently alive" in the agents topic (heartbeat + tool events within `--within` seconds). |
| `tasks` | Reconciles the `tasks` topic: which `assign`s are still pending, which `done`/`failed`, which exceeded `--max-age`. `--mark-timeout` publishes a `timeout` event so callers can react. |
| `race` | 1-to-N: dispatch the same prompt to several workers; each gets a unique `<base>-<cli>` task id. Use `--wait N` to block until all settle. |
| `debate` | N-round critique loop across participants (alternating). Optional `--synthesizer` produces a Consensus/Conflicts/Final summary at the end. |

See [`docs/integrations/`](docs/integrations/) for per-CLI integration recipes.

---

## Worker Rule Integration

For any CLI agent to behave as a session-channel worker, it needs to:

1. Treat a prompt containing `[session-channel:trusted task=<id> from=<pane>]` as an authorised task
2. Execute the task **as the user request**, then run the verbatim shell command that follows `after completion run:`

The reference rule (Claude Code / Codex / Gemini / Copilot / Opencode / Qwen all support it) lives in [`examples/worker-rule.md`](examples/worker-rule.md). Drop it into your CLI's instruction layer (e.g. `~/.claude/rules/`, `AGENTS.md`, `GEMINI.md`) and the dispatch loop closes.

---

## Configuration

`config.yaml` (or override via env vars):

| Key | Env var | Default | Purpose |
|---|---|---|---|
| `port` | `SESSION_CHANNEL_PORT` | `10101` | FastAPI bind port |
| `redis_url` | — | `redis://127.0.0.1:6379/0` | Redis URL |
| `allowed_origins` | `SESSION_CHANNEL_ALLOWED_ORIGINS` | localhost variants | CORS allow-list (comma-sep in env) |
| `ttl_seconds` | — | `1800` | XTRIM minid age |
| `relay_pool.workers` | — | (empty) | Supervisor-watched pane list |

Install location is resolved by every wrapper / hook in this order:

1. `$SESSION_CHANNEL_HOME` (explicit override)
2. `$HOME/.session-channel` (standard install)
3. Script-relative path (works in monorepo / source tree)

Python interpreter override: `$SESSION_CHANNEL_PY` (defaults to `python3` on `$PATH`).

---

## Development

```bash
# Run tests (requires fakeredis + freezegun + pytest-asyncio)
pip install fakeredis freezegun pytest-asyncio
python3 -m pytest tests/ -v

# Lint
ruff check .

# Dev server with autoreload
uvicorn main:app --reload --host 127.0.0.1 --port 10101
```

Repo layout:

```
cli/channel.py            — CLI entrypoint
main.py                   — FastAPI app
routes.py / store.py      — HTTP handlers + Redis store
config.py / config.yaml   — config loader
wrappers/                 — Codex / Gemini launch wrappers + Codex notify hook
scripts/supervisor.py     — Cronicle-driven respawn supervisor
templates/index.html      — single-file dashboard (vanilla JS)
tests/                    — pytest fixtures + (planned) test suites
dist/                     — install assets (Dockerfile, systemd unit, launchd plist)
examples/                 — worker rule, CLI adapters, Cronicle job sample
docs/integrations/        — per-CLI integration guides
```

---

## License

MIT — see [LICENSE](LICENSE).

## Status

Open-source debut: **v0.2.0** (2026-05). Phase 1-8 stabilised inside the upstream monorepo; Rust port (`session-channel-rs`) tracked as v2 milestone — Python stays the reference implementation until the API is locked in by real-world use.
