//! `channel doctor` — diagnose the local environment.
//!
//! `health` answers "is the service up?". `doctor` answers "is everything
//! installed and reachable, and if not, what's the exact fix?".
//!
//! Each line falls into one of four levels:
//!   PASS  — green; the thing is fine
//!   INFO  — dim; informational, no action required
//!   WARN  — yellow; non-blocking but worth addressing
//!   FAIL  — red; blocks normal operation, includes a "Fix:" line
//!
//! Exits 0 if no FAILs, 1 otherwise. WARNs do not affect the exit code.

use std::env;
use std::path::PathBuf;
use std::time::Duration;

use anyhow::Result;
use clap::Args as ClapArgs;
use serde::Deserialize;

use crate::config::{default_sender, Config};

#[derive(ClapArgs, Debug)]
pub struct Args {}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Level {
    Pass,
    Info,
    Warn,
    Fail,
}

struct Line {
    level: Level,
    title: &'static str,
    detail: String,
    fix: Option<String>,
}

impl Line {
    fn pass(title: &'static str, detail: impl Into<String>) -> Self {
        Self {
            level: Level::Pass,
            title,
            detail: detail.into(),
            fix: None,
        }
    }
    fn info(title: &'static str, detail: impl Into<String>) -> Self {
        Self {
            level: Level::Info,
            title,
            detail: detail.into(),
            fix: None,
        }
    }
    fn warn(title: &'static str, detail: impl Into<String>, fix: impl Into<String>) -> Self {
        Self {
            level: Level::Warn,
            title,
            detail: detail.into(),
            fix: Some(fix.into()),
        }
    }
    fn fail(title: &'static str, detail: impl Into<String>, fix: impl Into<String>) -> Self {
        Self {
            level: Level::Fail,
            title,
            detail: detail.into(),
            fix: Some(fix.into()),
        }
    }
}

pub fn run(_args: Args) -> Result<()> {
    let mut lines = Vec::<Line>::new();

    // 1. CLI binary (always passes — if we got here, channel exists)
    let cli_path = env::current_exe()
        .map(|p| p.display().to_string())
        .unwrap_or_else(|_| "(unknown)".to_string());
    lines.push(Line::pass(
        "channel binary",
        format!("{cli_path} (v{})", env!("CARGO_PKG_VERSION")),
    ));

    // 2. channel-service binary on PATH (best-effort)
    match which_binary("channel-service") {
        Some(p) => {
            let detail = probe_service_version(&p)
                .map(|v| format!("{} ({})", p.display(), v))
                .unwrap_or_else(|| p.display().to_string());
            lines.push(Line::pass("channel-service binary", detail));
        }
        None => lines.push(Line::warn(
            "channel-service binary",
            "not found on $PATH (only matters if you want to run the service on this host)",
            "brew install operonlab/tap/session-channel  · or use `docker compose up -d`",
        )),
    }

    // 3. Service reachability + (4) Redis (single /health call)
    let cfg = Config::from_env();
    let (svc_line, redis_line) = probe_service(&cfg);
    lines.push(svc_line);
    lines.push(redis_line);

    // 5. Environment variables snapshot
    let key =
        env::var("SESSION_CHANNEL_KEY").unwrap_or_else(|_| "change-me-in-production".to_string());
    if key == "change-me-in-production" {
        lines.push(Line::warn(
            "SESSION_CHANNEL_KEY",
            "using default 'change-me-in-production'",
            "export SESSION_CHANNEL_KEY=\"$(openssl rand -hex 32)\" in ~/.zshrc (or .env for docker compose)",
        ));
    } else {
        lines.push(Line::pass(
            "SESSION_CHANNEL_KEY",
            format!("custom ({}…)", &key.chars().take(6).collect::<String>()),
        ));
    }

    lines.push(Line::info(
        "SESSION_CHANNEL_URL",
        env::var("SESSION_CHANNEL_URL").unwrap_or_else(|_| cfg.base_url.clone()),
    ));

    // 6. tmux context — sender field hint
    match env::var("TMUX_PANE") {
        Ok(p) if !p.is_empty() => lines.push(Line::pass(
            "tmux",
            format!("TMUX_PANE={p}, sender resolves to {}", default_sender()),
        )),
        _ => lines.push(Line::info(
            "tmux",
            format!(
                "not in a tmux pane; sender will fallback to {}",
                default_sender()
            ),
        )),
    }

    // 7. Config-file pointers (informational only — service uses these, not CLI)
    if let Ok(p) = env::var("SESSION_CHANNEL_CONFIG") {
        lines.push(Line::info("SESSION_CHANNEL_CONFIG", p));
    } else if let Ok(home) = env::var("SESSION_CHANNEL_HOME") {
        lines.push(Line::info(
            "SESSION_CHANNEL_HOME",
            format!("{home} (service may load config.yaml from here)"),
        ));
    }

    render(&lines)
}

fn probe_service(cfg: &Config) -> (Line, Line) {
    let url = format!("{}/health", cfg.base_url);
    let client = match reqwest::blocking::Client::builder()
        .timeout(Duration::from_secs(3))
        .build()
    {
        Ok(c) => c,
        Err(e) => {
            let svc = Line::fail(
                "service reachable",
                format!("failed to build HTTP client: {e}"),
                "this is a bug — please open an issue",
            );
            let redis = Line::info("redis", "skipped (service unreachable)");
            return (svc, redis);
        }
    };

    let resp = client
        .get(&url)
        .header("x-local-key", &cfg.local_key)
        .send();

    match resp {
        Err(e) => {
            let svc = Line::fail(
                "service reachable",
                format!("GET {url} failed: {e}"),
                "start the service: `docker compose up -d`  ·  or `brew services start session-channel`  ·  or `channel-service &`",
            );
            let redis = Line::info("redis", "skipped (service unreachable)");
            (svc, redis)
        }
        Ok(r) if !r.status().is_success() => {
            let status = r.status();
            let svc = Line::fail(
                "service reachable",
                format!("{url} returned {status}"),
                if status == 401 {
                    "SESSION_CHANNEL_KEY does not match the running service".to_string()
                } else {
                    "see service logs (e.g. `docker compose logs channel-service`)".to_string()
                },
            );
            let redis = Line::info("redis", "skipped (service unhealthy)");
            (svc, redis)
        }
        Ok(r) => {
            #[derive(Deserialize)]
            struct H {
                #[serde(default)]
                redis: bool,
                #[serde(default)]
                active_topics: u64,
            }
            let body: H = r.json().unwrap_or(H {
                redis: false,
                active_topics: 0,
            });
            let svc = Line::pass(
                "service reachable",
                format!("{} (topics={})", cfg.base_url, body.active_topics),
            );
            let redis = if body.redis {
                Line::pass("redis", "reachable via service /health")
            } else {
                Line::fail(
                    "redis",
                    "service reports redis=False",
                    "start Redis: `docker compose up -d`  ·  or `brew services start redis`  ·  or `docker run -d -p 6379:6379 redis:7-alpine`",
                )
            };
            (svc, redis)
        }
    }
}

/// Spawn `<path> --version` and return the trimmed first line of stdout.
/// Returns `None` on any error (process spawn failure, non-zero exit, empty output).
fn probe_service_version(path: &std::path::Path) -> Option<String> {
    let out = std::process::Command::new(path)
        .arg("--version")
        .output()
        .ok()?;
    if !out.status.success() {
        return None;
    }
    let stdout = String::from_utf8(out.stdout).ok()?;
    let first = stdout.lines().next()?.trim().to_string();
    if first.is_empty() {
        None
    } else {
        Some(first)
    }
}

fn which_binary(name: &str) -> Option<PathBuf> {
    let path = env::var_os("PATH")?;
    for dir in env::split_paths(&path) {
        let candidate = dir.join(name);
        if is_executable(&candidate) {
            return Some(candidate);
        }
    }
    None
}

fn is_executable(p: &std::path::Path) -> bool {
    if !p.is_file() {
        return false;
    }
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        if let Ok(m) = std::fs::metadata(p) {
            return m.permissions().mode() & 0o111 != 0;
        }
        false
    }
    #[cfg(not(unix))]
    {
        // Best-effort on non-unix; we only ship unix targets anyway.
        let _ = p;
        true
    }
}

fn render(lines: &[Line]) -> Result<()> {
    let isatty = is_tty();

    println!("session-channel doctor");
    println!("──────────────────────");
    for line in lines {
        let (tag, color) = match line.level {
            Level::Pass => ("PASS", "32"), // green
            Level::Info => ("INFO", "2"),  // dim
            Level::Warn => ("WARN", "33"), // yellow
            Level::Fail => ("FAIL", "31"), // red
        };
        let tag = paint(tag, color, isatty);
        println!("{tag}  {:<22} {}", line.title, line.detail);
        if let Some(fix) = &line.fix {
            let label = paint("Fix:", "2", isatty);
            println!("        {label} {fix}");
        }
    }
    println!();

    let fails = lines.iter().filter(|l| l.level == Level::Fail).count();
    let warns = lines.iter().filter(|l| l.level == Level::Warn).count();

    match (fails, warns) {
        (0, 0) => println!("All green."),
        (0, w) => println!("{w} warning(s). See above."),
        (f, _) => {
            println!("{f} failure(s). Fix the FAIL lines above, then re-run.");
            std::process::exit(1);
        }
    }
    Ok(())
}

fn paint(s: &str, ansi: &str, on: bool) -> String {
    if on {
        format!("\x1b[{ansi}m{s}\x1b[0m")
    } else {
        s.to_string()
    }
}

fn is_tty() -> bool {
    // Don't pull in atty just for this; check the fd directly via libc-style env.
    // Most CI runners set NO_COLOR or are non-tty; honour NO_COLOR explicitly.
    if env::var_os("NO_COLOR").is_some() {
        return false;
    }
    // Best-effort: stdout is a tty if isatty(1) returns true.
    #[cfg(unix)]
    unsafe {
        extern "C" {
            fn isatty(fd: i32) -> i32;
        }
        isatty(1) == 1
    }
    #[cfg(not(unix))]
    {
        true
    }
}
