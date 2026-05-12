//! `channel read` — GET messages from `/api/messages/{topic}`.

use anyhow::Result;
use clap::Args as ClapArgs;
use serde::Deserialize;

use crate::client::ApiClient;

#[derive(ClapArgs, Debug)]
pub struct Args {
    /// Topic to read from.
    pub topic: String,

    /// Maximum number of messages to fetch.
    #[arg(long, default_value_t = 50)]
    pub count: u32,

    /// Show oldest N (xrange from 0-0); default shows newest N.
    #[arg(long)]
    pub oldest: bool,
}

#[derive(Deserialize)]
struct ReadResp {
    #[serde(default)]
    messages: Vec<Message>,
    #[serde(default)]
    count: u64,
}

#[derive(Deserialize)]
struct Message {
    #[serde(default)]
    text: String,
    #[serde(default)]
    sender: String,
    #[serde(default)]
    tag: String,
    #[serde(default)]
    priority: String,
}

pub fn run(args: Args) -> Result<()> {
    let count_str = args.count.to_string();
    let order = if args.oldest { "oldest" } else { "newest" };
    let query: Vec<(&str, &str)> =
        vec![("count", count_str.as_str()), ("order", order)];

    let client = ApiClient::new()?;
    let path = format!("/api/messages/{}", args.topic);
    let resp: ReadResp = client.get_json(&path, &query)?;

    for m in &resp.messages {
        let tag = if m.tag.is_empty() {
            String::new()
        } else {
            format!(" #{}", m.tag)
        };
        let pri = if m.priority == "high" { " ⚡" } else { "" };
        let sender = if m.sender.is_empty() {
            "?".to_string()
        } else {
            m.sender.clone()
        };
        // Right-align sender in a 10-char column to match the Python CLI.
        println!("  {:>10} │ {}{}{}", sender, m.text, tag, pri);
    }
    println!("--- {} messages ---", resp.count);
    Ok(())
}
