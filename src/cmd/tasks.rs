//! `channel tasks` — show task status summary for the `tasks` topic.
//!
//! Reads the entire `tasks` stream (up to --count messages), pairs assign
//! messages with their done/failed counterparts by task_id, and marks
//! unresolved assigns as pending (or timeout if older than --max-age seconds).

use std::collections::HashMap;
use std::time::{SystemTime, UNIX_EPOCH};

use anyhow::Result;
use clap::Args as ClapArgs;
use serde::{Deserialize, Serialize};
use serde_json::Value;

use crate::client::ApiClient;
use crate::config::default_sender;

#[derive(ClapArgs, Debug)]
pub struct Args {
    /// Maximum number of messages to fetch.
    #[arg(long, default_value_t = 200)]
    pub count: u32,

    /// Seconds before an assign with no outcome is considered timed-out.
    #[arg(long, default_value_t = 300)]
    pub max_age: u64,

    /// Only print pending + timed-out tasks.
    #[arg(long)]
    pub pending: bool,

    /// Publish a `timeout`-tagged event for each detected timeout.
    #[arg(long)]
    pub mark_timeout: bool,
}

// ---------------------------------------------------------------------------
// Serde types
// ---------------------------------------------------------------------------

#[derive(Deserialize)]
struct MessagesResp {
    #[serde(default)]
    messages: Vec<RawMessage>,
}

/// A message from the stream. `_meta` may be absent, null, a JSON object, or
/// (historically) a JSON-encoded string — handle all cases.
#[derive(Deserialize, Clone)]
struct RawMessage {
    #[serde(default)]
    id: String,
    #[serde(default)]
    text: String,
    #[serde(default)]
    tag: String,
    /// `_meta` arrives either as an object or as a JSON-encoded string.
    #[serde(default)]
    _meta: Value,
}

/// POST body for publishing a timeout event.
#[derive(Serialize)]
struct PostMessage {
    topic: &'static str,
    text: String,
    sender: String,
    tag: &'static str,
    _meta: PostMeta,
}

#[derive(Serialize)]
struct PostMeta {
    v: u8,
    task_id: String,
    reason: String,
}

#[derive(Deserialize)]
struct PostResp {
    // We only need to know it succeeded (200 OK), so we can ignore the body.
    #[allow(dead_code)]
    #[serde(default)]
    id: String,
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/// Extract `task_id` from a message text that may look like `"my-task: done"`.
fn parse_task_id(text: &str) -> &str {
    if let Some(pos) = text.find(": ") {
        text[..pos].trim()
    } else {
        ""
    }
}

/// Parse the millisecond-precision Redis stream id (`"1736789000123-0"`) into
/// seconds since epoch as f64.
fn redis_id_to_ts(id: &str) -> f64 {
    id.split('-')
        .next()
        .and_then(|ms| ms.parse::<u64>().ok())
        .map(|ms| ms as f64 / 1000.0)
        .unwrap_or(0.0)
}

/// Resolve `_meta` from a `serde_json::Value` that might be:
/// - a JSON object   → use directly
/// - a JSON string   → parse inner JSON
/// - null / missing  → empty object
fn resolve_meta(v: &Value) -> Value {
    match v {
        Value::Object(_) => v.clone(),
        Value::String(s) => serde_json::from_str(s).unwrap_or(Value::Object(Default::default())),
        _ => Value::Object(Default::default()),
    }
}

/// Best-effort string getter from a serde_json object.
fn meta_str<'a>(meta: &'a Value, key: &str) -> &'a str {
    meta.get(key).and_then(Value::as_str).unwrap_or("")
}

fn task_status_icon(status: &str) -> &'static str {
    match status {
        "done" => "✅",
        "failed" => "❌",
        "timeout" => "⏱",
        "pending" => "⏳",
        _ => "?",
    }
}

// ---------------------------------------------------------------------------
// Core logic
// ---------------------------------------------------------------------------

struct TaskRow {
    task_id: String,
    status: String,
    age_s: i64,
    latency_s: i64, // -1 means "no outcome yet"
    extra: String,
}

pub fn run(args: Args) -> Result<()> {
    let now = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs_f64();

    let client = ApiClient::new()?;
    let count_str = args.count.to_string();
    let query: Vec<(&str, &str)> = vec![("count", count_str.as_str()), ("order", "oldest")];
    let resp: MessagesResp = client.get_json("/api/messages/tasks", &query)?;

    // --- Reduce: last-write-wins per task_id ---
    // key: task_id → (message, ts)
    let mut assigns: HashMap<String, (RawMessage, f64)> = HashMap::new();
    // key: task_id → (message, ts, tag)
    let mut outcomes: HashMap<String, (RawMessage, f64, String)> = HashMap::new();

    for m in &resp.messages {
        let tag = m.tag.as_str();
        let meta = resolve_meta(&m._meta);

        // task_id: prefer _meta.task_id, fall back to text prefix
        let task_id_str = meta_str(&meta, "task_id");
        let task_id: String = if !task_id_str.is_empty() {
            task_id_str.to_owned()
        } else {
            let from_text = parse_task_id(&m.text);
            if from_text.is_empty() {
                continue;
            }
            from_text.to_owned()
        };

        let ts = redis_id_to_ts(&m.id);

        match tag {
            "assign" => {
                assigns.insert(task_id, (m.clone(), ts));
            }
            "done" | "failed" | "timeout" => {
                let better = outcomes
                    .get(&task_id)
                    .is_none_or(|(_, prev_ts, _)| ts > *prev_ts);
                if better {
                    outcomes.insert(task_id, (m.clone(), ts, tag.to_owned()));
                }
            }
            _ => {}
        }
    }

    // --- Build report rows, sorted by assign timestamp ---
    let mut assign_vec: Vec<(String, f64)> = assigns
        .into_iter()
        .map(|(tid, (_msg, ts))| (tid, ts))
        .collect();
    assign_vec.sort_by(|a, b| a.1.partial_cmp(&b.1).unwrap_or(std::cmp::Ordering::Equal));

    let mut rows: Vec<TaskRow> = Vec::new();
    for (task_id, a_ts) in assign_vec {
        let age_s = (now - a_ts) as i64;

        if let Some((outcome_msg, o_ts, outcome_tag)) = outcomes.get(&task_id) {
            let latency_s = if *o_ts > a_ts {
                (o_ts - a_ts) as i64
            } else {
                0
            };
            let extra = if outcome_tag == "failed" {
                let err = resolve_meta(&outcome_msg._meta);
                let err_str = meta_str(&err, "error");
                let truncated = if err_str.len() > 40 {
                    &err_str[..40]
                } else {
                    err_str
                };
                if truncated.is_empty() {
                    " error=?".to_owned()
                } else {
                    format!(" error={}", truncated)
                }
            } else {
                String::new()
            };
            rows.push(TaskRow {
                task_id,
                status: outcome_tag.clone(),
                age_s,
                latency_s,
                extra,
            });
        } else {
            // Still pending — check for timeout
            let (status, extra) = if age_s as u64 > args.max_age {
                (
                    "timeout".to_owned(),
                    format!(" (>{}s, no done/failed received)", args.max_age),
                )
            } else {
                ("pending".to_owned(), String::new())
            };
            rows.push(TaskRow {
                task_id,
                status,
                age_s,
                latency_s: -1,
                extra,
            });
        }
    }

    // Filter if --pending
    if args.pending {
        rows.retain(|r| r.status == "pending" || r.status == "timeout");
    }

    if rows.is_empty() {
        println!("  (no tasks found)");
        return Ok(());
    }

    // --- Print table ---
    // Python header:
    //   f"  {'status':<9} {'task_id':<28} {'age':>6} {'latency':>8}  extra"
    // Python separator: "  " + "─" * (len(header) - 2)
    //
    // header string length = 2 + 9 + 1 + 28 + 1 + 6 + 1 + 8 + 2 = 58 chars
    // separator = "  " + "─" * 56
    let header = format!(
        "  {status:<9} {task_id:<28} {age:>6} {latency:>8}  extra",
        status = "status",
        task_id = "task_id",
        age = "age",
        latency = "latency",
    );
    println!("{}", header);
    // len(header) - 2 = number of '─' chars; compute dynamically
    let dashes: String = "─".repeat(header.chars().count() - 2);
    println!("  {}", dashes);

    let mut counts: HashMap<String, usize> = HashMap::new();
    for row in &rows {
        *counts.entry(row.status.clone()).or_insert(0) += 1;
        let icon = task_status_icon(&row.status);
        let lat_str = if row.latency_s >= 0 {
            format!("{}s", row.latency_s)
        } else {
            "-".to_owned()
        };
        // task_id truncated to 28 chars
        let tid = if row.task_id.len() > 28 {
            &row.task_id[..28]
        } else {
            &row.task_id
        };
        // Python format:
        //   f"  {icon} {st:<7} {task_id[:28]:<28} {age_s:>5}s {lat_s:>8}  {extra}"
        println!(
            "  {} {st:<7} {tid:<28} {age:>5}s {lat:>8}  {extra}",
            icon,
            st = row.status,
            tid = tid,
            age = row.age_s,
            lat = lat_str,
            extra = row.extra,
        );
    }

    // Summary line
    let mut summary_parts: Vec<String> =
        counts.iter().map(|(k, v)| format!("{} {}", v, k)).collect();
    summary_parts.sort(); // deterministic order (matches sorted(counts.items()))
    println!("--- {} tasks: {} ---", rows.len(), summary_parts.join(", "));

    // --- Auto-publish timeout events for detected timeouts ---
    if args.mark_timeout {
        for row in &rows {
            if row.status != "timeout" {
                continue;
            }
            let tid = &row.task_id;
            let reason = format!("no done/failed within {}s", args.max_age);
            let body = PostMessage {
                topic: "tasks",
                text: format!("{}: timeout", tid),
                sender: default_sender(),
                tag: "timeout",
                _meta: PostMeta {
                    v: 1,
                    task_id: tid.clone(),
                    reason: reason.clone(),
                },
            };
            match client.post_json::<_, PostResp>("/api/messages", &body) {
                Ok(_) => println!("  📢 timeout event published for task {}", tid),
                Err(e) => eprintln!("  ⚠️  failed to publish timeout for {}: {}", tid, e),
            }
        }
    }

    Ok(())
}
