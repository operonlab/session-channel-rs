//! `channel send` — POST a message to `/api/messages`.
//!
//! Output format mirrors the Python CLI exactly so downstream tooling that
//! grep for `✅ [topic] id=<id>` keeps working.

use std::process::ExitCode;

use anyhow::{bail, Context, Result};
use clap::Args as ClapArgs;
use serde::{Deserialize, Serialize};
use serde_json::{Map, Value};

use crate::client::ApiClient;
use crate::config::default_sender;

#[derive(ClapArgs, Debug)]
pub struct Args {
    /// Topic to publish to (e.g. broadcasts, tasks, agents).
    pub topic: String,

    /// Message body.
    pub message: String,

    /// Optional tag (verb-style label like `done`, `assign`, `tool`).
    #[arg(long, default_value = "")]
    pub tag: String,

    /// Priority — `normal` (default) or `high`.
    #[arg(long, default_value = "normal", value_parser = ["normal", "high"])]
    pub priority: String,

    /// Override the default sender id (default: `pane-<tmux>` or `cli-<pid>`).
    #[arg(long, default_value = "")]
    pub sender: String,

    /// JSON object attached as the `_meta` sidecar field.
    #[arg(long, default_value = "")]
    pub meta: String,
}

#[derive(Serialize)]
struct SendBody<'a> {
    topic: &'a str,
    text: &'a str,
    sender: String,
    priority: &'a str,
    #[serde(skip_serializing_if = "str::is_empty")]
    tag: &'a str,
    #[serde(rename = "_meta", skip_serializing_if = "Option::is_none")]
    meta: Option<Map<String, Value>>,
}

#[derive(Deserialize)]
struct SendResp {
    topic: Option<String>,
    id: Option<String>,
}

pub fn run(args: Args) -> Result<()> {
    // Parse --meta into a JSON object (must be object, not array/scalar) so the
    // server-side schema sees a proper sidecar map.
    let meta = if args.meta.is_empty() {
        None
    } else {
        let v: Value = serde_json::from_str(&args.meta).context("--meta must be valid JSON")?;
        match v {
            Value::Object(m) => Some(m),
            _ => {
                bail!("--meta must be a JSON object (got list/string/etc)");
            }
        }
    };

    let sender = if args.sender.is_empty() {
        default_sender()
    } else {
        args.sender.clone()
    };

    let body = SendBody {
        topic: &args.topic,
        text: &args.message,
        sender,
        priority: &args.priority,
        tag: &args.tag,
        meta,
    };

    let client = ApiClient::new()?;
    match client.post_json::<_, SendResp>("/api/messages", &body) {
        Ok(d) => {
            println!(
                "✅ [{}] id={}",
                d.topic.unwrap_or_else(|| args.topic.clone()),
                d.id.unwrap_or_else(|| "?".to_string())
            );
            Ok(())
        }
        Err(e) => {
            eprintln!("❌ {e}");
            std::process::exit(1);
        }
    }
}

// (Kept for symmetry with future cmds; not strictly required while the
// only call site is `main` returning anyhow::Result.)
#[allow(dead_code)]
fn _exit(code: u8) -> ExitCode {
    ExitCode::from(code)
}
