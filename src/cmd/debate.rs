//! `channel debate` — multi-round cross-CLI debate with optional synthesizer.
//!
//! Ports Python `cmd_debate` + helpers from `channel.py` 1:1.
//! Structs and functions mirror the Python layout:
//!   - `Participant` ↔ `{label, cli, pane}` dict
//!   - `parse_participants` ↔ `_parse_participants`
//!   - `parse_synthesizer` ↔ `_parse_synthesizer`
//!   - `tmux_nudge` ↔ `_tmux_nudge` (inlined — not shared with race.rs)
//!   - `wait_for_outcome` ↔ `_wait_for_outcome`
//!   - `dispatch_one` ↔ `_dispatch_one`
//!   - `run` ↔ `cmd_debate`

use std::thread;
use std::time::{Duration, Instant};

use anyhow::{bail, Context, Result};
use clap::Args as ClapArgs;
use serde::{Deserialize, Serialize};
use serde_json::{Map, Value};

use crate::client::ApiClient;
use crate::config::default_sender;

// ── CLI args ─────────────────────────────────────────────────────────────────

#[derive(ClapArgs, Debug)]
pub struct Args {
    /// Opening question / topic for the debate.
    pub message: String,

    /// Unique debate session identifier (e.g. debate-2026-abc).
    #[arg(long)]
    pub debate_id: String,

    /// Comma-separated participant specs: `A:claude:%5,B:codex:%6`
    /// or short form `claude:%5,codex:%6` (auto-labels P1, P2, …).
    #[arg(long)]
    pub participants: String,

    /// Number of alternating rounds (default 3).
    #[arg(long, default_value_t = 3)]
    pub rounds: u32,

    /// Optional synthesizer pane `cli:pane` (or `label:cli:pane`) that
    /// receives the full transcript after all rounds complete.
    #[arg(long, default_value = "")]
    pub synthesizer: String,

    /// Per-round wait timeout in seconds (default 120).
    #[arg(long, default_value_t = 120)]
    pub round_timeout: u64,
}

// ── Domain types ─────────────────────────────────────────────────────────────

#[derive(Debug, Clone)]
struct Participant {
    label: String,
    cli: String,
    pane: String,
}

#[derive(Debug)]
struct Outcome {
    status: String, // "done" | "failed" | "timeout"
    result: String,
    summary: String,
    error: String,
}

// ── Transcript entry (one per round) ─────────────────────────────────────────

#[derive(Debug)]
struct TranscriptEntry {
    round: usize,
    label: String,
    cli: String,
    status: String,
    result: String,
    summary: String,
}

// ── HTTP bodies ──────────────────────────────────────────────────────────────

#[derive(Serialize)]
struct SendBody<'a> {
    topic: &'a str,
    text: String,
    sender: String,
    priority: &'a str,
    tag: &'a str,
    #[serde(rename = "_meta")]
    meta: Map<String, Value>,
}

#[derive(Deserialize)]
struct SendResp {
    id: Option<String>,
}

#[derive(Deserialize)]
struct ReadResp {
    #[serde(default)]
    messages: Vec<TaskMessage>,
}

#[derive(Deserialize)]
struct TaskMessage {
    #[serde(default)]
    text: String,
    #[serde(default)]
    tag: String,
    #[serde(rename = "_meta")]
    meta: Option<Value>,
}

// ── parse_participants ────────────────────────────────────────────────────────

/// Parse `"A:claude:%5,B:codex:%6"` or `"claude:%5,codex:%6"` into a
/// `Vec<Participant>`.  Short form (2 colon-separated parts) auto-assigns
/// labels P1, P2, …  Mirrors Python `_parse_participants`.
fn parse_participants(spec: &str) -> Result<Vec<Participant>> {
    let mut out = Vec::new();
    for (i, chunk) in spec.split(',').enumerate() {
        let chunk = chunk.trim();
        if chunk.is_empty() {
            continue;
        }
        let parts: Vec<&str> = chunk.splitn(3, ':').collect();
        let (label, cli, pane_raw) = match parts.as_slice() {
            [cli, pane] => (
                format!("P{}", i + 1),
                cli.trim().to_string(),
                pane.trim().to_string(),
            ),
            [label, cli, pane] => (
                label.trim().to_string(),
                cli.trim().to_string(),
                pane.trim().to_string(),
            ),
            _ => bail!("participant spec '{chunk}' must be 'label:cli:pane' or 'cli:pane'"),
        };
        let pane = if pane_raw.starts_with('%') {
            pane_raw
        } else {
            format!("%{}", pane_raw.trim_start_matches('%'))
        };
        if label.is_empty() || cli.is_empty() || pane == "%" {
            bail!("participant spec '{chunk}' missing field");
        }
        out.push(Participant {
            label,
            cli: cli.to_lowercase(),
            pane,
        });
    }
    Ok(out)
}

// ── parse_synthesizer ─────────────────────────────────────────────────────────

/// Parse optional `--synthesizer cli:pane` (or `label:cli:pane`).
/// Returns `None` for empty input; label is forced to `"S"`.
fn parse_synthesizer(spec: &str) -> Result<Option<Participant>> {
    if spec.is_empty() {
        return Ok(None);
    }
    let mut parsed = parse_participants(spec).context("--synthesizer parse failed")?;
    if parsed.len() != 1 {
        bail!("--synthesizer must be exactly one cli:pane");
    }
    parsed[0].label = "S".to_string();
    Ok(Some(parsed.remove(0)))
}

// ── tmux_nudge ────────────────────────────────────────────────────────────────

/// Push a wakeup line into the target pane via `tmux send-keys`.
///
/// When `topic == "tasks"` and `task_prompt` is non-empty, sends the full
/// prompt with the trust marker and success/failure report instructions.
/// Mirrors Python `_tmux_nudge`.
///
/// Sleep 300 ms between payload and Enter — critical for Codex (Phase E
/// validation 2026-05-11: Codex drops Enter when fired immediately).
fn tmux_nudge(pane: &str, topic: &str, task_id: &str, task_prompt: &str) {
    let sender = std::env::var("TMUX_PANE").unwrap_or_else(|_| "?".to_string());

    let wakeup = if topic == "tasks" && !task_prompt.is_empty() {
        let report_meta =
            format!(r#"{{"v":1,"task_id":"{task_id}","status":"ok","summary":"<one-line>"}}"#);
        let fail_meta =
            format!(r#"{{"v":1,"task_id":"{task_id}","error":"<describe what went wrong>"}}"#);
        let trust = format!("[session-channel:trusted task={task_id} from={sender}]");
        format!(
            "{task_prompt}  # {trust} \
on success run: channel send tasks \"{task_id}: done\" --tag done --meta '{report_meta}' ; \
on failure run: channel send tasks \"{task_id}: failed\" --tag failed --meta '{fail_meta}'"
        )
    } else if topic == "tasks" {
        "channel read tasks --count 10".to_string()
    } else {
        format!("channel read {topic} --count 5")
    };

    // Send payload text.
    let status = std::process::Command::new("tmux")
        .args(["send-keys", "-t", pane, &wakeup])
        .stderr(std::process::Stdio::piped())
        .status();

    match status {
        Err(e) => {
            eprintln!("warning: tmux send-keys (payload) failed: {e}");
            return;
        }
        Ok(s) if !s.success() => {
            eprintln!("warning: tmux send-keys (payload) exited {:?}", s.code());
            return;
        }
        _ => {}
    }

    // Brief settle delay — Codex drops Enter when fired immediately.
    thread::sleep(Duration::from_millis(300));

    // Send Enter separately so the payload text isn't escape-interpreted.
    let status2 = std::process::Command::new("tmux")
        .args(["send-keys", "-t", pane, "Enter"])
        .stderr(std::process::Stdio::piped())
        .status();

    if let Err(e) = status2 {
        eprintln!("warning: tmux send-keys (Enter) failed: {e}");
        return;
    }

    println!("push → {pane}: {wakeup}");
}

// ── wait_for_outcome ──────────────────────────────────────────────────────────

/// Poll `/api/messages/tasks` until `task_id` has a `done`/`failed` event.
///
/// Uses an xrange `since` cursor so old messages are not re-processed on
/// each poll.  Returns `Outcome { status="timeout" }` if deadline is hit.
/// Mirrors Python `_wait_for_outcome`.
fn wait_for_outcome(client: &ApiClient, task_id: &str, timeout_s: u64) -> Outcome {
    let deadline = Instant::now() + Duration::from_secs(timeout_s);
    // We use a since-id cursor; "0-0" means start from the very beginning so
    // we always find the matching event even if it was published before we
    // started polling (consistent with Python's behaviour of fetching 200
    // oldest messages every cycle).
    let count_str = "200".to_string();
    loop {
        if Instant::now() >= deadline {
            break;
        }
        thread::sleep(Duration::from_millis(500));

        let query = vec![("count", count_str.as_str()), ("order", "oldest")];
        let resp: Result<ReadResp> = client.get_json("/api/messages/tasks", &query);
        let msgs = match resp {
            Ok(r) => r.messages,
            Err(_) => continue,
        };

        for m in &msgs {
            if m.tag != "done" && m.tag != "failed" {
                continue;
            }
            // Extract _meta — may be a JSON object or a JSON string
            let mt: Map<String, Value> = match &m.meta {
                Some(Value::Object(obj)) => obj.clone(),
                Some(Value::String(s)) => serde_json::from_str(s).unwrap_or_default(),
                _ => Map::new(),
            };

            // task_id can live in _meta.task_id or be parseable from text
            let tid = mt
                .get("task_id")
                .and_then(|v| v.as_str())
                .unwrap_or("")
                .to_string();

            // Fallback: parse "<task_id>: done" pattern from text
            let tid_from_text: String = m
                .text
                .split(':')
                .next()
                .map(|s| s.trim().to_string())
                .unwrap_or_default();

            let matched_id = if !tid.is_empty() {
                &tid
            } else {
                &tid_from_text
            };

            if matched_id == task_id {
                return Outcome {
                    status: m.tag.clone(),
                    result: mt
                        .get("result")
                        .and_then(|v| v.as_str())
                        .unwrap_or("")
                        .to_string(),
                    summary: mt
                        .get("summary")
                        .and_then(|v| v.as_str())
                        .unwrap_or("")
                        .to_string(),
                    error: mt
                        .get("error")
                        .and_then(|v| v.as_str())
                        .unwrap_or("")
                        .to_string(),
                };
            }
        }
    }

    Outcome {
        status: "timeout".to_string(),
        result: String::new(),
        summary: String::new(),
        error: format!("no done/failed within {timeout_s}s"),
    }
}

// ── dispatch_one ──────────────────────────────────────────────────────────────

/// Publish one assign message to the tasks topic + tmux nudge.
/// Returns `true` on publish success.  Mirrors Python `_dispatch_one`.
fn dispatch_one(
    client: &ApiClient,
    task_id: &str,
    prompt: &str,
    pane: &str,
    role: &str,
    base_id: &str,
    extra_meta: Option<Map<String, Value>>,
    summary_text: Option<&str>,
) -> bool {
    // Build _meta sidecar.
    let mut meta = Map::new();
    meta.insert("v".to_string(), Value::Number(1.into()));
    meta.insert("task_id".to_string(), Value::String(task_id.to_string()));
    meta.insert(
        "debate_base_id".to_string(),
        Value::String(base_id.to_string()),
    );
    meta.insert("debate_role".to_string(), Value::String(role.to_string()));
    meta.insert("target_pane".to_string(), Value::String(pane.to_string()));
    meta.insert("prompt".to_string(), Value::String(prompt.to_string()));
    if let Some(extra) = extra_meta {
        for (k, v) in extra {
            meta.insert(k, v);
        }
    }

    // Truncate summary to 200 chars (mirrors Python slice [:200] + "...")
    let text = match summary_text {
        Some(s) => s.to_string(),
        None => {
            if prompt.len() > 200 {
                format!("{}...", &prompt[..200])
            } else {
                prompt.to_string()
            }
        }
    };

    let body = SendBody {
        topic: "tasks",
        text,
        sender: default_sender(),
        priority: "normal",
        tag: "assign",
        meta,
    };

    match client.post_json::<_, SendResp>("/api/messages", &body) {
        Ok(resp) => {
            println!(
                "  [tasks] {task_id} dispatched, id={}",
                resp.id.unwrap_or_else(|| "?".to_string())
            );
        }
        Err(e) => {
            eprintln!("  warning: publish failed: {e}");
            return false;
        }
    }

    tmux_nudge(pane, "tasks", task_id, prompt);
    true
}

// ── run (cmd_debate) ──────────────────────────────────────────────────────────

pub fn run(args: Args) -> Result<()> {
    // Parse participants.
    let participants =
        parse_participants(&args.participants).context("--participants parse failed")?;
    if participants.len() < 2 {
        bail!("--participants needs >= 2 (e.g. A:claude:%5,B:codex:%6)");
    }

    // Parse optional synthesizer.
    let synthesizer = parse_synthesizer(&args.synthesizer).context("--synthesizer parse failed")?;

    let rounds = args.rounds.max(1) as usize;
    let base_id = &args.debate_id;
    let question = &args.message;
    let round_timeout = args.round_timeout;

    let client = ApiClient::new()?;

    println!(
        "debate: {} participants x {rounds} rounds, base_id={base_id}",
        participants.len()
    );
    if let Some(ref s) = synthesizer {
        println!("   synthesizer: {} ({})", s.cli, s.pane);
    }

    let mut transcript: Vec<TranscriptEntry> = Vec::new();

    for i in 0..rounds {
        let p = &participants[i % participants.len()];
        let role = if i == 0 { "opening" } else { "respond" };

        let prompt: String = if i == 0 {
            question.clone()
        } else {
            let prev = &transcript[transcript.len() - 1];
            let prev_body = if !prev.result.is_empty() {
                prev.result.clone()
            } else if !prev.summary.is_empty() {
                prev.summary.clone()
            } else {
                "(empty)".to_string()
            };
            format!(
                "原始問題：{question}\n\n\
---\n\
以下是 {label} (Round {round}, {cli}) 的回應：\n\n\
{prev_body}\n\
---\n\n\
請以你的視角 critic / 補強 / 回應。同意請說明理由；不同意請給 counter argument。",
                label = prev.label,
                round = prev.round,
                cli = prev.cli,
            )
        };

        let task_id = format!("{base_id}-r{}-{}", i + 1, p.label);
        println!(
            "\n-- Round {}/{rounds}: {} ({} @ {}) -- {role} --",
            i + 1,
            p.label,
            p.cli,
            p.pane
        );

        let mut extra = Map::new();
        extra.insert("debate_round".to_string(), Value::Number((i + 1).into()));

        let ok = dispatch_one(
            &client,
            &task_id,
            &prompt,
            &p.pane,
            &p.label,
            base_id,
            Some(extra),
            None,
        );
        if !ok {
            return Ok(());
        }

        println!(
            "  waiting up to {round_timeout}s for {}'s response...",
            p.label
        );
        let outcome = wait_for_outcome(&client, &task_id, round_timeout);

        transcript.push(TranscriptEntry {
            round: i + 1,
            label: p.label.clone(),
            cli: p.cli.clone(),
            status: outcome.status.clone(),
            result: outcome.result.clone(),
            summary: outcome.summary.clone(),
        });

        match outcome.status.as_str() {
            "timeout" => {
                println!("  timeout -- debate halted at round {}", i + 1);
                break;
            }
            "failed" => {
                println!("  {} failed: {}", p.label, outcome.error);
                break;
            }
            _ => {
                println!("  {} done", p.label);
            }
        }
    }

    // Print transcript.
    println!(
        "\n{}\nTranscript ({} round(s))\n{}",
        "=".repeat(60),
        transcript.len(),
        "=".repeat(60)
    );
    for entry in &transcript {
        let body = if !entry.result.is_empty() {
            entry.result.clone()
        } else if !entry.summary.is_empty() {
            entry.summary.clone()
        } else {
            format!("(empty -- {})", entry.status)
        };
        println!(
            "\n-- Round {}: {} ({}) [{}] --",
            entry.round, entry.label, entry.cli, entry.status
        );
        println!("{body}");
    }

    // Synthesizer round.
    if let Some(ref synth) = synthesizer {
        if !transcript.is_empty() {
            println!(
                "\n{}\nSynthesizer round ({} @ {})\n{}",
                "=".repeat(60),
                synth.cli,
                synth.pane,
                "=".repeat(60)
            );

            let synth_id = format!("{base_id}-synth");
            let mut parts = vec![
                format!("原始問題：{question}\n"),
                format!("以下是 {} 輪 debate transcript：\n", transcript.len()),
            ];
            for entry in &transcript {
                let body = if !entry.result.is_empty() {
                    entry.result.clone()
                } else if !entry.summary.is_empty() {
                    entry.summary.clone()
                } else {
                    "(empty)".to_string()
                };
                parts.push(format!(
                    "### Round {}: {} ({})\n{body}\n",
                    entry.round, entry.label, entry.cli
                ));
            }
            parts.push(
                "---\n請 synthesize 上述 debate 成一份 refined position：\n\
1. **Consensus** — 所有人同意的點\n\
2. **Conflicts** — 主要分歧 + 各方論述\n\
3. **Final Direction** — 你建議的 refined answer 與理由"
                    .to_string(),
            );
            let synth_prompt = parts.join("\n");

            let ok = dispatch_one(
                &client,
                &synth_id,
                &synth_prompt,
                &synth.pane,
                "synthesizer",
                base_id,
                None,
                Some("synthesize debate transcript"),
            );
            if !ok {
                return Ok(());
            }

            println!("  waiting up to {round_timeout}s for synthesis...");
            let outcome = wait_for_outcome(&client, &synth_id, round_timeout);
            match outcome.status.as_str() {
                "timeout" => println!("  synthesizer timeout"),
                "failed" => println!("  synthesizer failed: {}", outcome.error),
                _ => {
                    println!("  synthesis done\n");
                    let body = if !outcome.result.is_empty() {
                        outcome.result
                    } else if !outcome.summary.is_empty() {
                        outcome.summary
                    } else {
                        "(empty synthesis)".to_string()
                    };
                    println!("{body}");
                }
            }
        }
    }

    Ok(())
}
