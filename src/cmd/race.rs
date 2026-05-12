//! `channel race` — fan out the same prompt to N worker panes, one assign per
//! worker on the `tasks` topic.  Mirrors `cmd_race` in the Python CLI 1:1.
//!
//! Each worker gets a unique task_id `<base_id>-<cli>` so done/failed events
//! can be matched cleanly.  `_meta.race_base_id` lets downstream tooling
//! filter all legs of the same race.
//!
//! Output format mirrors the Python CLI so `channel tasks` shows results in the
//! same status table (grep anchors: `✅ [tasks] <task_id> → <cli> (<pane>) id=<id>`).

use std::collections::HashSet;
use std::process::Command;
use std::thread;
use std::time::{Duration, Instant};

use anyhow::{bail, Context, Result};
use clap::Args as ClapArgs;
use serde::{Deserialize, Serialize};
use serde_json::{Map, Value};

use crate::client::ApiClient;
use crate::config::default_sender;

// ------------------------------------------------------------------
// CLI args (stub already defined these; we reuse the same shape)
// ------------------------------------------------------------------

#[derive(ClapArgs, Debug)]
pub struct Args {
    /// Prompt message to broadcast to all workers.
    pub message: String,

    /// Base task-id; each worker gets `<task_id>-<cli>`.
    #[arg(long)]
    pub task_id: String,

    /// Comma-separated worker specs: `cli:pane[,cli:pane,…]` e.g. `claude:%5,codex:%6`.
    #[arg(long)]
    pub workers: String,

    /// Optional JSON object merged into each worker's `_meta` sidecar.
    #[arg(long, default_value = "")]
    pub meta: String,

    /// Seconds to wait for all workers to settle (0 = fire-and-forget).
    #[arg(long, default_value_t = 0)]
    pub wait: u64,

    /// Skip the tmux send-keys nudge (publish only).
    #[arg(long)]
    pub no_notify: bool,
}

// ------------------------------------------------------------------
// Wire types
// ------------------------------------------------------------------

#[derive(Serialize)]
struct SendBody {
    topic: String,
    text: String,
    sender: String,
    priority: String,
    tag: String,
    #[serde(rename = "_meta", skip_serializing_if = "Option::is_none")]
    meta: Option<Map<String, Value>>,
}

#[derive(Deserialize)]
struct SendResp {
    id: Option<String>,
    #[allow(dead_code)]
    topic: Option<String>,
}

#[derive(Deserialize)]
struct TasksResp {
    messages: Option<Vec<TaskMsg>>,
}

#[derive(Deserialize)]
struct TaskMsg {
    tag: Option<String>,
    text: Option<String>,
    #[serde(rename = "_meta")]
    meta: Option<Value>,
}

// ------------------------------------------------------------------
// Internal helpers
// ------------------------------------------------------------------

/// Parse `"claude:%5,codex:%6"` into `vec![("claude", "%5"), ("codex", "%6")]`.
/// Mirrors Python `_parse_workers`.
fn parse_workers(spec: &str) -> Result<Vec<(String, String)>> {
    let mut out = Vec::new();
    for chunk in spec.split(',') {
        let chunk = chunk.trim();
        if chunk.is_empty() {
            continue;
        }
        let Some(colon) = chunk.find(':') else {
            bail!("worker spec '{chunk}' must be 'cli:pane' (e.g. claude:%5)");
        };
        let cli = chunk[..colon].trim().to_lowercase();
        let pane_raw = chunk[colon + 1..].trim();
        if cli.is_empty() {
            bail!("worker spec '{chunk}' missing cli name");
        }
        if pane_raw.is_empty() {
            bail!("worker spec '{chunk}' missing pane id");
        }
        // Normalise — both `5` and `%5` → `%5`
        let pane = if pane_raw.starts_with('%') {
            pane_raw.to_string()
        } else {
            format!("%{}", pane_raw.trim_start_matches('%'))
        };
        out.push((cli, pane));
    }
    Ok(out)
}

/// Extract task_id from a tasks message text following the convention
/// `<task_id>: done|failed`.  Mirrors Python `_parse_task_id`.
fn parse_task_id_from_text(text: &str) -> &str {
    if let Some(pos) = text.find(": ") {
        return text[..pos].trim();
    }
    ""
}

/// Push the prompt into the target pane via `tmux send-keys`.
/// Mirrors Python `_tmux_nudge` for the `tasks` topic path.
///
/// Critical: 0.3 s settle between text payload and Enter — required for
/// Codex TUI compatibility (Phase E validation 2026-05-11).
fn tmux_nudge(pane: &str, task_id: &str, task_prompt: &str) {
    // Normalise pane id
    let pane = if pane.starts_with('%') {
        pane.to_string()
    } else {
        format!("%{}", pane.trim_start_matches('%'))
    };

    let sender = std::env::var("TMUX_PANE").unwrap_or_else(|_| "?".to_string());

    // Trust marker — tells the worker Claude this push came from
    // session-channel (a user-configured local bus), not an untrusted source.
    // zsh treats the `# …` part as a comment.
    let report_meta =
        format!(r#"{{"v":1,"task_id":"{task_id}","status":"ok","summary":"<one-line>"}}"#);
    let fail_meta =
        format!(r#"{{"v":1,"task_id":"{task_id}","error":"<describe what went wrong>"}}"#);
    let trust = format!("[session-channel:trusted task={task_id} from={sender}]");
    let wakeup = format!(
        "{task_prompt}  # {trust} \
         on success run: channel send tasks \"{task_id}: done\" \
         --tag done --meta '{report_meta}' ; \
         on failure run: channel send tasks \"{task_id}: failed\" \
         --tag failed --meta '{fail_meta}'"
    );

    // send-keys: payload text (no Enter yet)
    let text_result = Command::new("tmux")
        .args(["send-keys", "-t", &pane, &wakeup])
        .output();

    match text_result {
        Err(e) => {
            eprintln!("  push to {pane} failed (tmux text): {e}");
            return;
        }
        Ok(o) if !o.status.success() => {
            let stderr = String::from_utf8_lossy(&o.stderr);
            eprintln!("  push to {pane} failed (tmux text): {stderr}");
            return;
        }
        _ => {}
    }

    // 0.3 s settle before Enter — critical for Codex TUI compatibility
    thread::sleep(Duration::from_millis(300));

    // send-keys: Enter (separate call avoids escape interpretation in payload)
    let enter_result = Command::new("tmux")
        .args(["send-keys", "-t", &pane, "Enter"])
        .output();

    match enter_result {
        Err(e) => {
            eprintln!("  push to {pane} failed (tmux Enter): {e}");
        }
        Ok(o) if !o.status.success() => {
            let stderr = String::from_utf8_lossy(&o.stderr);
            eprintln!("  push to {pane} failed (tmux Enter): {stderr}");
        }
        _ => {
            println!("  push → {pane}: {wakeup}");
        }
    }
}

// ------------------------------------------------------------------
// Main command entry point
// ------------------------------------------------------------------

pub fn run(args: Args) -> Result<()> {
    // --- parse workers ---
    let workers = match parse_workers(&args.workers) {
        Ok(w) if w.is_empty() => {
            eprintln!("  --workers required, e.g. --workers claude:%5,codex:%6,gemini:%7");
            std::process::exit(2);
        }
        Err(e) => {
            eprintln!("  {e}");
            std::process::exit(2);
        }
        Ok(w) => w,
    };

    // --- parse --meta (must be JSON object) ---
    let extra_meta: Map<String, Value> = if args.meta.is_empty() {
        Map::new()
    } else {
        let v: Value = serde_json::from_str(&args.meta).context("--meta must be valid JSON")?;
        match v {
            Value::Object(m) => m,
            _ => bail!("--meta must be a JSON object (got list/string/etc)"),
        }
    };

    let base_id = &args.task_id;
    let prompt = &args.message;
    let client = ApiClient::new()?;

    println!("  race: {} worker(s), base_id={base_id}", workers.len());

    let mut task_ids: Vec<String> = Vec::new();

    for (cli, pane) in &workers {
        let task_id = format!("{base_id}-{cli}");
        task_ids.push(task_id.clone());

        // Build per-worker _meta — fixed fields first, then extra_meta overlay
        let mut meta: Map<String, Value> = Map::new();
        meta.insert("v".into(), Value::Number(1.into()));
        meta.insert("task_id".into(), Value::String(task_id.clone()));
        meta.insert("race_base_id".into(), Value::String(base_id.to_string()));
        meta.insert("race_cli".into(), Value::String(cli.clone()));
        meta.insert("target_pane".into(), Value::String(pane.clone()));
        meta.insert("prompt".into(), Value::String(prompt.to_string()));
        // extra_meta can override anything above (mirrors Python `meta.update(extra_meta)`)
        for (k, v) in &extra_meta {
            meta.insert(k.clone(), v.clone());
        }

        let body = SendBody {
            topic: "tasks".to_string(),
            text: prompt.to_string(),
            sender: default_sender(),
            priority: "normal".to_string(),
            tag: "assign".to_string(),
            meta: Some(meta),
        };

        match client.post_json::<_, SendResp>("/api/messages", &body) {
            Ok(resp) => {
                let rid = resp.id.unwrap_or_else(|| "?".to_string());
                println!("  [tasks] {task_id} → {cli} ({pane}) id={rid}");
            }
            Err(e) => {
                eprintln!("    {cli} ({pane}): publish error {e}");
                // Mirror Python: `continue` — skip notify but keep trying other workers
                continue;
            }
        }

        if !args.no_notify {
            tmux_nudge(pane, &task_id, prompt);
        }
    }

    // --- fire-and-forget ---
    if args.wait == 0 {
        println!(
            "\n  Watch progress: channel tasks --pending  \
             (or rerun: channel race ... --wait 300)"
        );
        return Ok(());
    }

    // --- wait loop: poll /api/messages/tasks until all settled or timeout ---
    let deadline = Instant::now() + Duration::from_secs(args.wait);
    let mut pending: HashSet<String> = task_ids.iter().cloned().collect();

    println!(
        "\n  waiting up to {}s for {} task(s)...",
        args.wait,
        pending.len()
    );

    while !pending.is_empty() && Instant::now() < deadline {
        thread::sleep(Duration::from_secs(5));

        let result: Result<TasksResp> = client.get_json(
            "/api/messages/tasks",
            &[("count", "200"), ("order", "oldest")],
        );

        let msgs = match result {
            Ok(r) => r.messages.unwrap_or_default(),
            Err(_) => continue,
        };

        for msg in &msgs {
            let tag = msg.tag.as_deref().unwrap_or("");
            if tag != "done" && tag != "failed" {
                continue;
            }

            // Resolve task_id: prefer _meta.task_id, fallback to text prefix
            let tid_from_meta = msg
                .meta
                .as_ref()
                .and_then(|m| {
                    // _meta may arrive as a JSON string (stringified) or object
                    let obj = match m {
                        Value::Object(o) => Some(o.clone()),
                        Value::String(s) => serde_json::from_str(s).ok(),
                        _ => None,
                    }?;
                    obj.get("task_id")
                        .and_then(|v| v.as_str())
                        .map(|s| s.to_string())
                })
                .unwrap_or_default();

            let tid_from_text = msg
                .text
                .as_deref()
                .map(parse_task_id_from_text)
                .unwrap_or("")
                .to_string();

            let tid = if !tid_from_meta.is_empty() {
                tid_from_meta
            } else {
                tid_from_text
            };

            if pending.contains(&tid) {
                pending.remove(&tid);
                println!("  [{tag}] {tid}");
            }
        }
    }

    if !pending.is_empty() {
        let sorted: Vec<_> = {
            let mut v: Vec<&String> = pending.iter().collect();
            v.sort();
            v
        };
        println!(
            "\n  {} task(s) still pending after {}s: {}",
            pending.len(),
            args.wait,
            sorted
                .iter()
                .map(|s| s.as_str())
                .collect::<Vec<_>>()
                .join(", ")
        );
    } else {
        println!("\n  all {} race task(s) settled", task_ids.len());
    }

    Ok(())
}
