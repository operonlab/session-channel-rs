# session-channel-rs

Rust port of [`session-channel`](https://github.com/operonlab/session-channel) — a cross-pane, cross-CLI pub-sub bus over **tmux + Redis Streams**. **Alpha — `v0.1.0-alpha`.**

## Status (2026-05-12, v0.1.0-alpha)

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
- ✅ `GET /health` — Redis PING + topic count
- ✅ `itsdangerous`-compatible signed cookies (HMAC-SHA1) — Python-issued cookies validate unchanged
- ✅ Background trim loop (XTRIM `MINID`) + fanout loop (XREAD across topics)

### Real-world validation
- All 47 tests pass (9 inline auth + 11 data-flow + 15 E2E parity + 12 mutation-killer)
- **Live-swap verified**: Python service stopped, `channel-service` bound to `:10101`, Python CLI / dashboard / SSE all worked against the Rust service without modification.

### Known gaps in alpha
- JSON response key order: serde sorted vs Python dict-insertion. Functional parity confirmed by shape; byte-string diffs differ.
- `cargo test` parallel run has a port-allocation race in `test_agents_active_parity` — use `--test-threads=1` for stable pass.
- No cross-platform CI release binaries yet — users currently `cargo build` locally.
- `channel send --meta <invalid-json>` prints anyhow's multi-line `Error: ... Caused by: ...` instead of Python's `❌ ...` one-liner.

## Build & run

```bash
git clone https://github.com/operonlab/session-channel-rs
cd session-channel-rs
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

`session-channel-rs` is byte-compatible with the Python reference on every public surface (CLI args, HTTP routes, Redis stream format, signed cookies). The two implementations can:

- Coexist on the same Redis (different ports)
- Issue signed cookies that the other validates
- Be swapped at the service layer without changing CLIs, hooks, wrappers, or the dashboard

The Python version (`operonlab/session-channel`, v0.2.0) remains the reference implementation while this Rust port stabilises. Following the upstream plan (`CHANGELOG.md` over there), the Rust port is **v2 polishing**, not a rewrite of unstable territory.

## License

MIT — see `LICENSE` (inherited from upstream).
