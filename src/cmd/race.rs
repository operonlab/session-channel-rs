//! `channel race` — stub. To be implemented by W4.

use anyhow::Result;
use clap::Args as ClapArgs;

#[derive(ClapArgs, Debug)]
pub struct Args {
    pub message: String,
    #[arg(long)]
    pub task_id: String,
    #[arg(long)]
    pub workers: String,
    #[arg(long, default_value = "")]
    pub meta: String,
    #[arg(long, default_value_t = 0)]
    pub wait: u64,
    #[arg(long)]
    pub no_notify: bool,
}

pub fn run(_args: Args) -> Result<()> {
    anyhow::bail!("channel race: not yet implemented (skeleton)")
}
