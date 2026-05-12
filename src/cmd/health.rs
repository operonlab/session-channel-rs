//! `channel health` — stub. To be implemented by W1.

use anyhow::Result;
use clap::Args as ClapArgs;

#[derive(ClapArgs, Debug)]
pub struct Args {}

pub fn run(_args: Args) -> Result<()> {
    anyhow::bail!("channel health: not yet implemented (skeleton)")
}
