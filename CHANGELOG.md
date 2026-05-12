# Changelog

All notable changes to `session-channel` will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- `LICENSE` file (MIT) — previously referenced from README but missing.
- `CHANGELOG.md` (this file).
- `CONTRIBUTING.md` with development workflow.
- GitHub issue templates (`bug_report.md`, `feature_request.md`).
- CI workflow `.github/workflows/test.yml` — runs `cargo fmt --check`, `cargo clippy -D warnings`, and `cargo test` (default + `--ignored`) against a Redis 7 service container on every push and pull request.
- `Cargo.toml` metadata: `homepage`, `documentation`, `readme`, `keywords`, `categories`, `rust-version` (1.82).
- Release tarballs now include `LICENSE` and `README.md`, and ship with a `.sha256` companion file (used by Homebrew formula bumps).
- Release notes auto-include install snippets (Docker / Homebrew / `install.sh`).
- `Dockerfile` + `docker-compose.yml` + `.env.example` for a one-command stack (`docker compose up -d` brings up Redis + service).
- `.github/workflows/docker.yml` builds multi-arch (`linux/amd64` + `linux/arm64`) images and pushes to `ghcr.io/operonlab/session-channel` on `v*.*.*` tags and `main`.
- `install.sh` — POSIX one-liner that detects OS/arch, downloads the right tarball, verifies the SHA256, and installs to `~/.local/bin`. Supports `--uninstall`.
- `packaging/homebrew/session-channel.rb` — Homebrew formula source of truth (copied into `operonlab/homebrew-tap` per release). Includes `service do …` for `brew services start session-channel`.
- README Install section restructured: Docker / Homebrew / `install.sh` / source as four distinct paths with the recommended path first.

### Changed
- `test_agents_active_parity` marked `#[ignore]` — it reads the shared `ws:channel:agents` stream and is host-pollution-prone on dev boxes with live wrappers. CI runs it explicitly via `--ignored`.
- `cargo test` in CI no longer needs `--test-threads=1` for the default suite.

## [0.2.0] — 2026-05-12

### Changed
- **P8 cutover** — Rust implementation took over as the canonical `session-channel`. Package name dropped the `-rs` suffix; OSS repo URL moved from `operonlab/session-channel-rs` to `operonlab/session-channel`. Old clone URLs continue to resolve via GitHub's automatic repo-rename redirect.
- The original Python reference is archived at [`operonlab/session-channel-py`](https://github.com/operonlab/session-channel-py) (read-only).

### Added
- Byte-level parity with the Python implementation (all 8 CLI subcommands: `send` / `read` / `topics` / `health` / `agents` / `tasks` / `race` / `debate`).
- HTTP service (`channel-service`, `:10101`) — drop-in for the Python FastAPI service. Dashboard HTML embedded via `include_str!`.
- `itsdangerous`-compatible signed cookies (HMAC-SHA1) — Python-issued cookies validate against the Rust service unchanged.
- Background trim loop (XTRIM `MINID`) + fanout loop (XREAD across topics).
- SSE push delivery via `GET /api/stream?topic=…`.
- Shell wrappers under `wrappers/` — `claude-with-channel.sh`, `codex-with-channel.sh`, `gemini-with-channel.sh`, `sse_subscribe.sh`.
- `CHANNEL_LOOP=1` opt-in respawn for Gemini / Codex wrappers.
- Live-swap verified: stopping the Python service and starting `channel-service` on the same port keeps Python CLIs, hooks, and the dashboard working without modification.

### Known issues
- `cargo test` parallel run has a port-allocation race in `test_agents_active_parity` — use `--test-threads=1` for stable pass. (Fix tracked for the next release.)
- No cross-platform CI release binaries yet — users currently `cargo build` locally.
- `channel send --meta <invalid-json>` prints anyhow's multi-line `Error: ... Caused by: ...` instead of Python's `❌ ...` one-liner.

[Unreleased]: https://github.com/operonlab/session-channel/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/operonlab/session-channel/releases/tag/v0.2.0
