//! `channel` — Rust port of session-channel CLI.
//!
//! Skeleton scope: `send` and `read` only. The remaining six commands
//! (topics / health / agents / tasks / race / debate) and the service
//! binary are planned for a follow-up session.

use anyhow::Result;
use clap::{Parser, Subcommand};

mod client;
mod config;
mod cmd;

#[derive(Parser, Debug)]
#[command(
    name = "channel",
    version,
    about = "Rust port of session-channel CLI (skeleton)",
    long_about = "Reads SESSION_CHANNEL_URL (default http://localhost:10101) and \
SESSION_CHANNEL_KEY (default change-me-in-production) for the running session-channel service.\n\
\n\
Skeleton supports `send` and `read` only — other subcommands land in a follow-up release."
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
}

fn main() -> Result<()> {
    let cli = Cli::parse();
    match cli.cmd {
        Cmd::Send(args) => cmd::send::run(args),
        Cmd::Read(args) => cmd::read::run(args),
    }
}
