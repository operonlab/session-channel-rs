//! `channel` — Rust port of session-channel CLI.
//!
//! Phase A: all 8 CLI commands ported. The service binary (axum +
//! redis-rs replacing the Python FastAPI) is sequenced for a follow-up.

use anyhow::Result;
use clap::{Parser, Subcommand};

mod client;
mod cmd;
mod config;

#[derive(Parser, Debug)]
#[command(
    name = "channel",
    version,
    about = "Rust port of session-channel CLI",
    long_about = "Reads SESSION_CHANNEL_URL (default http://localhost:10101) and \
SESSION_CHANNEL_KEY (default change-me-in-production) for the running session-channel service."
)]
struct Cli {
    #[command(subcommand)]
    cmd: Cmd,
}

#[derive(Subcommand, Debug)]
enum Cmd {
    /// Send a message to a topic.
    Send(cmd::send::Args),
    /// Read messages from a topic.
    Read(cmd::read::Args),
    /// List active topics.
    Topics(cmd::topics::Args),
    /// Check station health.
    Health(cmd::health::Args),
    /// List active agents (panes).
    Agents(cmd::agents::Args),
    /// Show task status (pending/done/failed/timeout) for the tasks topic.
    Tasks(cmd::tasks::Args),
    /// Race the same prompt across N workers.
    Race(cmd::race::Args),
    /// Multi-round cross-CLI debate.
    Debate(cmd::debate::Args),
}

fn main() -> Result<()> {
    let cli = Cli::parse();
    match cli.cmd {
        Cmd::Send(a) => cmd::send::run(a),
        Cmd::Read(a) => cmd::read::run(a),
        Cmd::Topics(a) => cmd::topics::run(a),
        Cmd::Health(a) => cmd::health::run(a),
        Cmd::Agents(a) => cmd::agents::run(a),
        Cmd::Tasks(a) => cmd::tasks::run(a),
        Cmd::Race(a) => cmd::race::run(a),
        Cmd::Debate(a) => cmd::debate::run(a),
    }
}
