# session-channel-rs

Rust port of [`session-channel`](https://github.com/operonlab/session-channel). **Skeleton — not feature-complete.**

## Scope (2026-05-12 skeleton)

- ✅ `channel send` — POST to `/api/messages`
- ✅ `channel read` — GET from `/api/messages/{topic}`
- 🚧 `topics` / `health` / `agents` / `tasks` / `race` / `debate` — next session
- 🚧 Service binary (axum + redis-rs replacing the Python FastAPI) — v0.2

The CLI talks to the **existing Python service** (`localhost:10101` by
default), so you can drop the Rust binary into an existing install and
mix it with the Python CLI. The service-side rewrite is sequenced after
all eight CLI commands are at parity.

## Build

```bash
cargo build --release
./target/release/channel send broadcasts "hello from rust"
./target/release/channel read broadcasts --count 5
```

Env vars (same as the Python CLI):

| Var | Default |
|---|---|
| `SESSION_CHANNEL_URL` | `http://localhost:10101` |
| `SESSION_CHANNEL_KEY` | `change-me-in-production` |
| `TMUX_PANE` | (informs default `sender` field as `pane-<n>`) |

## Why a Rust port?

The Python implementation has been stable since v0.1 and `operonlab/session-channel`
v0.2.0 is the reference release. A Rust port targets:

- Single-binary deploy (no Python runtime / venv)
- Lower memory footprint for the long-lived service
- Same external API and stream format — Python and Rust binaries can
  coexist against the same Redis topics

Following the upstream's plan (see `CHANGELOG.md` in the Python repo),
the Rust port is **v2 polishing**, not a rewrite of unstable territory.
