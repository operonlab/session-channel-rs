//! `channel debate` — stub. To be implemented by W4.

use anyhow::Result;
use clap::Args as ClapArgs;

#[derive(ClapArgs, Debug)]
pub struct Args {
    pub message: String,
    #[arg(long)]
    pub debate_id: String,
    #[arg(long)]
    pub participants: String,
    #[arg(long, default_value_t = 3)]
    pub rounds: u32,
    #[arg(long, default_value = "")]
    pub synthesizer: String,
    #[arg(long, default_value_t = 120)]
    pub round_timeout: u64,
}

pub fn run(_args: Args) -> Result<()> {
    anyhow::bail!("channel debate: not yet implemented (skeleton)")
}
