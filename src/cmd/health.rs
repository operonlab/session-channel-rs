//! `channel health` — GET /health and print redis + topic count.
//!
//! Output mirrors the Python CLI exactly:
//!   ✅ redis=true  topics=3
//!   ❌ redis=false  topics=0

use anyhow::Result;
use clap::Args as ClapArgs;
use serde::Deserialize;

use crate::client::ApiClient;

#[derive(ClapArgs, Debug)]
pub struct Args {}

#[derive(Deserialize)]
struct HealthResp {
    #[serde(default)]
    redis: bool,
    #[serde(default)]
    active_topics: u64,
}

pub fn run(_args: Args) -> Result<()> {
    let client = ApiClient::new()?;
    let d: HealthResp = client.get_json("/health", &[])?;
    let status = if d.redis { "✅" } else { "❌" };
    // Python str(bool) → "True"/"False" (capital first letter); match exactly.
    let redis_str = if d.redis { "True" } else { "False" };
    println!("{} redis={}  topics={}", status, redis_str, d.active_topics);
    // Failure-only diagnostic hint (does not pollute success output that older
    // scripts may grep against).
    if !d.redis {
        eprintln!(
            "   Hint: run `channel doctor` for diagnostics, or start Redis \
             with `brew services start redis` / `docker compose up -d`."
        );
    }
    Ok(())
}
