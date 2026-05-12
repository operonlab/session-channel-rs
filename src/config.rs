//! Runtime configuration — pulled from environment to mirror the Python CLI.

use std::env;

const DEFAULT_BASE_URL: &str = "http://localhost:10101";
const DEFAULT_LOCAL_KEY: &str = "change-me-in-production";

pub struct Config {
    pub base_url: String,
    pub local_key: String,
}

impl Config {
    pub fn from_env() -> Self {
        Self {
            base_url: env::var("SESSION_CHANNEL_URL")
                .unwrap_or_else(|_| DEFAULT_BASE_URL.to_string()),
            local_key: env::var("SESSION_CHANNEL_KEY")
                .unwrap_or_else(|_| DEFAULT_LOCAL_KEY.to_string()),
        }
    }
}

/// Default `sender` field — mirrors the Python helper.
/// `%23` → `pane-23` when running inside tmux; otherwise `cli-<pid>`.
pub fn default_sender() -> String {
    match env::var("TMUX_PANE") {
        Ok(p) if !p.is_empty() => p.replace('%', "pane-"),
        _ => format!("cli-{}", std::process::id()),
    }
}
