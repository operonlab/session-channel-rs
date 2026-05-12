//! `channel agents` — list active agents from `/api/agents/active`.
//!
//! 1:1 port of `cmd_agents` in channel.py (lines 236-273).

use std::collections::HashMap;
use std::process::Command;
use std::time::{SystemTime, UNIX_EPOCH};

use anyhow::Result;
use clap::Args as ClapArgs;
use serde::Deserialize;
use serde_json::Value;

use crate::client::ApiClient;

#[derive(ClapArgs, Debug)]
pub struct Args {
    /// Look-back window in seconds (default 300).
    #[arg(long, default_value_t = 300)]
    pub within: u64,
}

// ── API response types ────────────────────────────────────────────────────────

#[derive(Deserialize)]
struct AgentsResp {
    #[serde(default)]
    agents: Vec<Agent>,
    #[serde(default)]
    count: u64,
    within: Option<Value>,
}

#[derive(Deserialize)]
struct Agent {
    #[serde(rename = "_meta")]
    meta: Option<HashMap<String, Value>>,
    sender: Option<String>,
    last_seen: Option<Value>,
}

// ── Helpers ───────────────────────────────────────────────────────────────────

/// Mirror of Python `_CLI_ICON`.
fn cli_icon(cli: &str) -> &'static str {
    match cli {
        "claude" => "🔷",
        "codex" => "🔶",
        "gemini" => "💎",
        _ => "·",
    }
}

/// Mirror of Python `_live_panes` (lines 209-222).
/// Calls `tmux list-panes -aF "#{pane_id}"` with a 1-second timeout; on any
/// error returns an empty set.
fn live_panes() -> std::collections::HashSet<String> {
    let out = Command::new("tmux")
        .args(["list-panes", "-aF", "#{pane_id}"])
        .output();

    match out {
        Ok(o) if o.status.success() => {
            let text = String::from_utf8_lossy(&o.stdout);
            text.lines()
                .map(|l| l.trim().to_string())
                .filter(|l| !l.is_empty())
                .collect()
        }
        _ => std::collections::HashSet::new(),
    }
}

/// Mirror of Python `_fmt_age` (lines 225-233).
/// Returns `"Ns"` | `"Nm"` | `"Nh"`.
fn fmt_age(last_seen: f64) -> String {
    let now = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs_f64())
        .unwrap_or(0.0);
    let age = (now - last_seen).max(0.0) as u64;
    if age < 60 {
        format!("{age}s")
    } else if age < 3600 {
        format!("{}m", age / 60)
    } else {
        format!("{}h", age / 3600)
    }
}

/// Extract a string value from a `HashMap<String, Value>`.
fn meta_str<'a>(meta: &'a HashMap<String, Value>, key: &str) -> Option<&'a str> {
    meta.get(key).and_then(|v| v.as_str())
}

// ── run ───────────────────────────────────────────────────────────────────────

pub fn run(args: Args) -> Result<()> {
    let w = args.within.to_string();
    let client = ApiClient::new()?;
    let resp: AgentsResp = client.get_json("/api/agents/active", &[("within", w.as_str())])?;

    let agents = &resp.agents;
    if agents.is_empty() {
        println!("  (no active agents in last {} s)", args.within);
        return Ok(());
    }

    let live = live_panes();

    // Header — matches Python format string exactly.
    // "  {icon:<4} {pane:<14} {role:<8} {ctx:<5} {branch:<18} {task:<28} {age:>5} {live:<4}"
    // len = 2+4+1+14+1+8+1+5+1+18+1+28+1+5+1+4 = 95 chars
    let header = format!(
        "  {:<4} {:<14} {:<8} {:<5} {:<18} {:<28} {:>5} {:<4}",
        "icon", "pane", "role", "ctx", "branch", "task", "age", "live"
    );
    println!("{header}");
    // Python: "  " + "─" * (len(header) - 2)  →  2 + 93 = 95 total
    println!("  {}", "─".repeat(header.chars().count() - 2));

    for a in agents {
        let empty_meta: HashMap<String, Value> = HashMap::new();
        let m = a.meta.as_ref().unwrap_or(&empty_meta);

        // cli icon
        let cli = meta_str(m, "cli").unwrap_or("?").to_lowercase();
        let icon = cli_icon(&cli);

        // pane: _meta.pane → agent.sender → "?"
        // Python: m.get("pane") or a.get("sender") or "?" — falls back on empty string too.
        let pane = meta_str(m, "pane")
            .filter(|s| !s.is_empty())
            .or_else(|| a.sender.as_deref().filter(|s| !s.is_empty()))
            .unwrap_or("?");

        // role — Python: m.get("role") or "?"
        let role = meta_str(m, "role").filter(|s| !s.is_empty()).unwrap_or("?");

        // ctx_pct — numeric value → "N%" else "?"
        let ctx_s = match m.get("ctx_pct") {
            Some(Value::Number(n)) => {
                if let Some(f) = n.as_f64() {
                    format!("{}%", f.round() as i64)
                } else {
                    "?".to_string()
                }
            }
            _ => "?".to_string(),
        };

        // branch / task — truncated.
        // Python uses `(m.get("branch") or "-")` which falls back on None AND
        // empty string; replicate that by treating "" the same as None.
        let branch_raw = meta_str(m, "branch")
            .filter(|s| !s.is_empty())
            .unwrap_or("-");
        let branch: String = branch_raw.chars().take(18).collect();

        let task_raw = meta_str(m, "task").filter(|s| !s.is_empty()).unwrap_or("-");
        let task: String = task_raw.chars().take(28).collect();

        // last_seen → age string
        let last_seen: f64 = match a.last_seen.as_ref() {
            Some(Value::Number(n)) => n.as_f64().unwrap_or(0.0),
            Some(Value::String(s)) => s.parse::<f64>().unwrap_or(0.0),
            _ => 0.0,
        };
        let age = fmt_age(last_seen);

        // live check
        let is_live = if live.contains(pane) { "✓" } else { "·" };

        println!(
            "  {:<4} {:<14} {:<8} {:<5} {:<18} {:<28} {:>5} {:<4}",
            icon, pane, role, ctx_s, branch, task, age, is_live
        );
    }

    // Footer — mirror Python: f"--- {d['count']} agents in last {d['within']}s ---"
    let within_val = match &resp.within {
        Some(Value::Number(n)) => n
            .as_u64()
            .map(|v| v.to_string())
            .or_else(|| n.as_f64().map(|v| format!("{}", v as u64)))
            .unwrap_or_else(|| args.within.to_string()),
        Some(Value::String(s)) => s.clone(),
        _ => args.within.to_string(),
    };
    println!("--- {} agents in last {}s ---", resp.count, within_val);

    Ok(())
}
