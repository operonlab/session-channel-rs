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
- **`channel doctor`** — new subcommand. Diagnoses CLI binary, service reachability, Redis, env vars, tmux context, and config-file pointers. Every FAIL line carries a `Fix:` line with an exact command. Exits 1 on any FAIL.
- `channel health` now appends a one-line `Hint:` (to stderr) when the service reports Redis down, pointing at `channel doctor` and concrete restart commands. Success output is unchanged for grep compatibility.
- `quickstart.sh` — interactive first-run script: picks a Redis + service path (docker compose / brew / skip), optionally generates a `SESSION_CHANNEL_KEY`, brings things up, and ends with a `channel doctor` verdict. `--yes` mode is fully unattended.
- README Quickstart section added between Install and Environment variables.
- `channel-service --version` / `--help` (clap-driven; previously the binary started the server with no arg parsing, which broke `brew test do` and prevented doctor from showing the service version).
- `channel doctor` now displays the channel-service binary version alongside the path when the binary is on $PATH.
- `channel doctor` now actively verifies `SESSION_CHANNEL_KEY` against the running service by hitting an authenticated endpoint (`/api/topics`); FAILs with a "key mismatch" Fix when the service returns 401. Previously doctor only checked the public `/health`, so a wrong key would silently PASS.
- Unit tests for `cmd::doctor` (binary lookup + exec bit check).

### Changed
- `install.sh` + `quickstart.sh`: silence ShellCheck SC2016 on documentation lines (single-quoted `$VAR` shown to the user verbatim is intentional, not a bug).
- `packaging/homebrew/session-channel.rb` `test do` assertions updated to the new (correct) channel-service --version behaviour.
- All tests in `tests/e2e_parity.rs` are now `#[ignore]` — they require an `operonlab/session-channel-py` reference service at `PYTHON_PORT` (10101 by default) for byte-level parity comparison; CI does not provision Python. Devs run `cargo test -- --ignored` locally on a box with the reference service. (Codex audit caught that CI would otherwise FAIL at `wait_for_health(PYTHON_PORT, …)` for every parity test.)
- `cargo test` in CI no longer needs `--test-threads=1` for the default suite.
- MSRV raised to **1.86** to match transitive deps `idna_adapter@1.2.2` + `icu_properties_data@2.2.0` (both require edition2024, stable in Rust 1.85; the deps further require 1.86). Builds against Rust 1.82–1.84 will fail.
- Service now reads `SESSION_CHANNEL_HOST` env (default `127.0.0.1`). The Dockerfile and `docker-compose.yml` set this to `0.0.0.0` so the published container port is reachable.
- Service now reads `SESSION_CHANNEL_KEY` env (previously the secret key was effectively hard-coded to `change-me-in-production` because `ServiceConfig::load` did not consult the env). The KEY injection that `docker-compose.yml` and `quickstart.sh` were already writing was a silent no-op before this fix; verified end-to-end (a request with a wrong `x-local-key` now correctly returns 401 against a container started with a custom KEY).
- README's `Compatibility with Python` section rewritten to consistently describe the Python implementation as *archived reference only*; previously the section still called Python "the reference implementation" while the top of the README declared Rust canonical (P8 cutover).
- `.dockerignore` no longer carries a `!README.md` exception (the Dockerfile does not COPY README).

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
