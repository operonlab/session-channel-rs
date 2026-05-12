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
- `cargo test` parallel run has a port-allocation race in `test_agents_active_parity` — use `--test-threads=1` for stable pass.
- No cross-platform CI release binaries yet — users currently `cargo build` locally.
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

- **Rust 1.75+** — only if you want to build from source. Install via [rustup](https://rustup.rs/).
- **Redis 6+** — local or remote. Quickest: `docker run -d -p 6379:6379 redis:7-alpine` or `brew services start redis`.
- **(optional) tmux** — required only if you want pane-aware `sender` fields.

Pre-built binaries, a Docker stack, a Homebrew tap, and a one-line `install.sh` are tracked for v0.3 (see [`CHANGELOG.md`](./CHANGELOG.md) and the project roadmap).

## Build & run

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

### Environment variables

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
