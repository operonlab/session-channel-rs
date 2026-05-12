//! `channel topics` — GET /api/topics and list active topics with counts.
//!
//! Output mirrors the Python CLI exactly:
//!   (no active topics)        — when list is empty
//!   or
//!                  tasks  12 msgs
//!             broadcasts   3 msgs

use anyhow::Result;
use clap::Args as ClapArgs;
use serde::Deserialize;

use crate::client::ApiClient;

#[derive(ClapArgs, Debug)]
pub struct Args {}

#[derive(Deserialize)]
struct TopicsResp {
    #[serde(default)]
    topics: Vec<TopicEntry>,
}

#[derive(Deserialize)]
struct TopicEntry {
    #[serde(default)]
    topic: String,
    #[serde(default)]
    count: u64,
}

pub fn run(_args: Args) -> Result<()> {
    let client = ApiClient::new()?;
    let d: TopicsResp = client.get_json("/api/topics", &[])?;
    if d.topics.is_empty() {
        println!("  (no active topics)");
        return Ok(());
    }
    for t in &d.topics {
        println!("  {:>20}  {} msgs", t.topic, t.count);
    }
    Ok(())
}
