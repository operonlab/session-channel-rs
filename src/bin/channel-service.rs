//! Thin entry for the `channel-service` binary.
//!
//! Delegates to `session_channel::service::run`. Keeping this file tiny
//! means the library crate can be unit-/integration-tested without a binary
//! `main()` getting in the way.
//!
//! `--version` and `--help` are handled by clap before the server starts.
//! Previously the binary had no arg parsing at all, which meant any invocation
//! (including `channel-service --version` in `brew test do`) attempted to bind
//! the port and either hung or failed with a bind error.

use clap::Parser;

/// session-channel HTTP service — replaces the Python FastAPI implementation
#[derive(Parser, Debug)]
#[command(name = "channel-service", version, about)]
struct Cli {}

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    // clap parses --version / --help here and exits before the server starts.
    let _cli = Cli::parse();
    session_channel::service::run().await
}
