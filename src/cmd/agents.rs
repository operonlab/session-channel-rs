//! `channel agents` — stub. To be implemented by W2.

use anyhow::Result;
use clap::Args as ClapArgs;

#[derive(ClapArgs, Debug)]
pub struct Args {
    /// Look-back window in seconds (default 300).
    #[arg(long, default_value_t = 300)]
    pub within: u64,
}

pub fn run(_args: Args) -> Result<()> {
    anyhow::bail!("channel agents: not yet implemented (skeleton)")
}
