//! `channel tasks` — stub. To be implemented by W3.

use anyhow::Result;
use clap::Args as ClapArgs;

#[derive(ClapArgs, Debug)]
pub struct Args {
    #[arg(long, default_value_t = 200)]
    pub count: u32,
    #[arg(long, default_value_t = 300)]
    pub max_age: u64,
    #[arg(long)]
    pub pending: bool,
    #[arg(long)]
    pub mark_timeout: bool,
}

pub fn run(_args: Args) -> Result<()> {
    anyhow::bail!("channel tasks: not yet implemented (skeleton)")
}
