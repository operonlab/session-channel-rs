# session-channel

A cross-pane, cross-CLI pub-sub bus over **tmux + Redis Streams**. **v0.2.0 — Rust takeover release.**

> The Rust implementation took over as the canonical `session-channel` on 2026-05-12 (P8 cutover). The original Python reference is archived at `operonlab/session-channel-py` (kept read-accessible; new development lands here). Old user clones of `operonlab/session-channel-rs` continue to resolve via GitHub's automatic repo-rename redirect.

## Status (v0.2.0, post-P8 cutover)

### CLI (`channel`)
1:1 port of all 8 Python subcommands. Byte-level parity verified against the Python CLI.

- ✅ `send` · `read` · `topics` · `health` · `agents` · `tasks` · `race` · `debate`

### Service (`channel-service`)
HTTP service on `:10101` (configurable). Drops in for the Python FastAPI service — Python CLIs, hooks, wrappers, and the dashboard all keep working unchanged.

- ✅ `GET /` — dashboard (HTML embedded via `include_str!`, local-key injected)
- ✅ `POST /api/messages` — publish + rate-limit (10/s per sender) + SSE fan-out
- ✅ `GET /api/messages/:topic` — `since` / `count` / `order=oldest|newest`
- ✅ `GET /api/topics` — SMEMBERS + per-topic XLEN, SREM empty topics
- ✅ `GET /api/agents/active?within=N` — last-write-wins reduce by host:pane
- ✅ `GET /api/stream` — SSE with `topic=` filter + 30s keep-alive
- ✅ `GET /health` — Redis PING + topic count; JSON key order matches Python (`status`, `redis`, `active_topics`)
- ✅ `itsdangerous`-compatible signed cookies (HMAC-SHA1) — Python-issued cookies validate unchanged
- ✅ Background trim loop (XTRIM `MINID`) + fanout loop (XREAD across topics)

### Real-world validation
- All 47 tests pass (9 inline auth + 11 data-flow + 15 E2E parity + 12 mutation-killer)
- **Live-swap verified**: Python service stopped, `channel-service` bound to `:10101`, Python CLI / dashboard / SSE all worked against the Rust service without modification.

### Known gaps in alpha
- `test_agents_active_parity` is marked `#[ignore]` on dev boxes — it reads the shared `ws:channel:agents` stream and gets polluted by live `claude/codex/gemini-with-channel.sh` wrappers heartbeating during the test. CI runs it against an isolated Redis container via `cargo test -- --ignored`.
- `channel send --meta <invalid-json>` prints anyhow's multi-line `Error: ... Caused by: ...` instead of Python's `❌ ...` one-liner.

## Wrappers & SSE push

`stations/session-channel/wrappers/` provides four shell scripts that turn any
Claude Code / Codex / Gemini pane into a **registered session-channel worker**
with bi-directional push delivery:

| Script | What it does |
|--------|-------------|
| `claude-with-channel.sh` | Launches Claude Code; registers pane on startup; tears down on exit |
| `codex-with-channel.sh` | Same for Codex CLI |
| `gemini-with-channel.sh` | Same for Gemini CLI |
| `sse_subscribe.sh` | Background SSE listener (sourced by the wrappers above) |

### Push delivery loop

```
orchestrator                worker pane
     │                           │
     │  channel race / debate    │
     │  (--workers pane list)    │
     │──────────────────────────►│  dispatch via session-channel stream
     │                           │
     │                           │  sse_subscribe.sh (background)
     │                           │  ╔═════════════════════════════╗
     │                           │  ║ curl /api/stream (chunked)  ║
     │                           │  ║ filter: topic=tasks         ║
     │                           │  ║         tag=assign          ║
     │                           │  ║         _meta.target==pane  ║
     │                           │  ║ → tmux send-keys prompt     ║
     │                           │  ╚═════════════════════════════╝
     │                           │
     │◄──────────────────────────│  channel send tasks "done" --tag done
```

**Minimal 5-line loop** (illustrates what `sse_subscribe.sh` does internally):

```bash
curl -sN -H "x-local-key: $KEY" \
  "$BASE_URL/api/stream?topic=tasks" | while IFS= read -r line; do
  [[ "$line" == data:* ]] || continue
  payload="${line#data: }"
  tmux send-keys -t "$PANE" "$(echo "$payload" | jq -r '.text')" Enter
done
```

### CHANNEL_LOOP — opt-in respawn

Gemini and Codex occasionally self-exit (e.g. after an idle timeout). Set
`CHANNEL_LOOP=1` before running the wrapper to enable an auto-respawn loop:

```bash
CHANNEL_LOOP=1 gemini-with-channel.sh %7
```

When `CHANNEL_LOOP=1`, the wrapper re-launches the CLI each time it exits,
re-registers the pane, and resumes the SSE listener — keeping the worker slot
alive indefinitely. Claude Code does not need this (it does not self-exit), so
`claude-with-channel.sh` ignores `CHANNEL_LOOP`.

## Prerequisites

Pick one path; you don't need all three:

- **Docker + Docker Compose** — _recommended_; bundles Redis. Nothing else to install on the host.
- **Homebrew (macOS / Linux)** + a Redis you already run somewhere.
- **`install.sh`** for any *nix without `brew` + a Redis you already run somewhere.

Optional:

- **tmux** — only required if you want pane-aware `sender` fields.
- **Rust 1.82+** — only if you want to build from source.

## Install

### Docker (recommended — bundles Redis)

```bash
curl -fsSL https://raw.githubusercontent.com/operonlab/session-channel/main/docker-compose.yml -o docker-compose.yml
echo "SESSION_CHANNEL_KEY=$(openssl rand -hex 32)" > .env
docker compose up -d
```

Service comes up on `http://127.0.0.1:10101`. Then install the CLI on the host (see below) — `channel send` talks to the service over HTTP, no `docker exec` needed.

### Homebrew (macOS / Linux)

```bash
brew install operonlab/tap/session-channel
brew services start redis           # if you don't already have one
brew services start session-channel # background launch via launchd / systemd
```

### One-line installer

```bash
curl -fsSL https://raw.githubusercontent.com/operonlab/session-channel/main/install.sh | bash
```

Installs `channel` + `channel-service` to `~/.local/bin` (override with `INSTALL_DIR=...`). To remove:

```bash
curl -fsSL https://raw.githubusercontent.com/operonlab/session-channel/main/install.sh | bash -s -- --uninstall
```

### From source

```bash
git clone https://github.com/operonlab/session-channel
cd session-channel
cargo build --release --bins

# Start the service (replaces the Python uvicorn)
./target/release/channel-service

# CLI (defaults to http://localhost:10101)
./target/release/channel health
./target/release/channel send broadcasts "hello from rust"
./target/release/channel topics
./target/release/channel agents --within 600
./target/release/channel race "design Q?" --task-id demo \
    --workers claude:%5,codex:%6,gemini:%7 --wait 120
```

## Quickstart

Once `channel` is on `$PATH`, run the interactive bring-up — it asks where to put Redis + the service, optionally generates a random `SESSION_CHANNEL_KEY`, and finishes with a `channel doctor` verdict:

```bash
curl -fsSL https://raw.githubusercontent.com/operonlab/session-channel/main/quickstart.sh -o quickstart.sh
chmod +x quickstart.sh
./quickstart.sh           # interactive
./quickstart.sh --yes     # full-auto: docker compose + random key
```

Then verify and send your first message:

```bash
channel doctor                          # PASS / WARN / FAIL per check
channel send broadcasts "hello"
channel read broadcasts --count 1
```

`channel doctor` is also a good first stop whenever something feels off — every FAIL line carries a `Fix:` with the exact command to run.

## Environment variables

Applies to every install path (Docker / Homebrew / `install.sh` / source). For Docker, set these in your `.env` file; for the host CLI, export them in your shell rc.

| Var | Default | Effect |
|---|---|---|
| `SESSION_CHANNEL_URL` | `http://localhost:10101` | CLI: target service base URL |
| `SESSION_CHANNEL_KEY` | `change-me-in-production` | CLI: `x-local-key` value |
| `SESSION_CHANNEL_PORT` | `10101` | Service: bind port |
| `SESSION_CHANNEL_REDIS_URL` | `redis://127.0.0.1:6379/0` | Service: Redis URL |
| `SESSION_CHANNEL_ALLOWED_ORIGINS` | (config.yaml) | Service: CORS allow-list (comma-sep) |
| `SESSION_CHANNEL_HOME` | (auto-detect) | Service: optional config.yaml dir |
| `TMUX_PANE` | (auto) | CLI: informs default `sender` field |
| `CHANNEL_LOOP` | (unset) | Wrappers: set to `1` to enable auto-respawn |

## Architecture (same as Python — drop-in)

```
                ┌──────────────────────────────────────────────┐
                │  Dashboard  (axum + SSE, port 10101)         │
                │  GET / + GET /api/stream                     │
                └─────────────────┬────────────────────────────┘
                                  │ HTTP + SSE
                                  ▼
                ┌──────────────────────────────────────────────┐
                │              Redis Streams                   │
                └───┬───────────────────────────────────────┬──┘
                    │ XADD / XRANGE / XREVRANGE / XTRIM     │
                    ▼                                       ▼
            channel CLI                              channel-service
            (this crate)                             (this crate)
```

The CLI binary uses `reqwest` blocking; the service binary uses `axum` + `tokio` + `redis-rs`.

## Compatibility with Python

`session-channel` is byte-compatible with the archived Python reference on every public surface (CLI args, HTTP routes, Redis stream format, signed cookies). The two implementations can:

- Coexist on the same Redis (different ports)
- Issue signed cookies that the other validates
- Be swapped at the service layer without changing CLIs, hooks, wrappers, or the dashboard

The Python version (`operonlab/session-channel`, v0.2.0) remains the reference implementation while this Rust port stabilises. Following the upstream plan (`CHANGELOG.md` over there), the Rust port is **v2 polishing**, not a rewrite of unstable territory.

## License

MIT — see [`LICENSE`](./LICENSE). Copyright © 2026 Jones Hong.
