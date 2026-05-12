# Contributing to `session-channel`

Thanks for your interest. This is a small project, so the workflow is light.

## Getting set up

```bash
git clone https://github.com/operonlab/session-channel
cd session-channel
cargo build --release --bins
```

You also need a running Redis (any 6+) on `redis://127.0.0.1:6379/0` to run the
service or integration tests. The quickest options:

```bash
docker run -d -p 6379:6379 --name session-channel-redis redis:7-alpine
# or:
brew services start redis
```

## Running the test suite

```bash
cargo test
```

If you hit the known port-allocation race in `test_agents_active_parity`, use:

```bash
cargo test -- --test-threads=1
```

CI (`.github/workflows/test.yml`) runs `cargo fmt --check`, `cargo clippy -D warnings`,
and `cargo test` on every push and pull request, so please run them locally first.

## Code style

- `cargo fmt` before committing.
- `cargo clippy --all-targets -- -D warnings` should be clean.
- New public modules should keep the `service` / `client` / `cmd` separation:
  - `src/service/` — HTTP service (axum + redis-rs).
  - `src/client/` — HTTP client (reqwest blocking).
  - `src/cmd/` — one file per CLI subcommand.

## Commit messages

Conventional Commits-style is preferred but not enforced:

```
feat(cmd): add channel doctor subcommand
fix(service): correct topic-not-found status code
docs(readme): expand quickstart
```

## Reporting issues

Use the templates under `.github/ISSUE_TEMPLATE/`. Include:

- `channel --version` output
- OS / arch (`uname -a`)
- Redis version (`redis-cli --version`)
- The full command you ran and its output

## License

By contributing you agree your contributions will be licensed under the MIT
License (see `LICENSE`).
