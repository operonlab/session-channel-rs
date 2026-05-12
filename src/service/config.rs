//! Service-side runtime config — 1:1 with Python `config.py` (skeleton).
//!
//! YAML resolution order:
//!   1. $SESSION_CHANNEL_CONFIG (explicit path)
//!   2. $SESSION_CHANNEL_HOME/config.yaml
//!   3. config.yaml relative to the current working directory
//!   4. fall back to in-code defaults (no file required)

use anyhow::{Context, Result};
use serde::{Deserialize, Serialize};
use std::env;
use std::path::{Path, PathBuf};

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ServiceConfig {
    #[serde(default = "default_port")]
    pub port: u16,
    #[serde(default = "default_host")]
    pub host: String,
    #[serde(default = "default_redis_url")]
    pub redis_url: String,
    #[serde(default = "default_secret_key")]
    pub secret_key: String,
    #[serde(default = "default_session_cookie_name")]
    pub session_cookie_name: String,
    #[serde(default = "default_session_max_age")]
    pub session_max_age: u64,
    #[serde(default = "default_stream_prefix")]
    pub stream_prefix: String,
    #[serde(default = "default_topics_key")]
    pub topics_key: String,
    #[serde(default = "default_ttl_seconds")]
    pub ttl_seconds: u64,
    #[serde(default = "default_trim_interval")]
    pub trim_interval: u64,
    #[serde(default = "default_max_stream_len")]
    pub max_stream_len: u64,
    #[serde(default = "default_allowed_origins")]
    pub allowed_origins: Vec<String>,
}

fn default_port() -> u16 {
    10101
}
fn default_host() -> String {
    "127.0.0.1".into()
}
fn default_redis_url() -> String {
    "redis://127.0.0.1:6379/0".into()
}
fn default_secret_key() -> String {
    "change-me-in-production".into()
}
fn default_session_cookie_name() -> String {
    "workshop_session".into()
}
fn default_session_max_age() -> u64 {
    604_800
}
fn default_stream_prefix() -> String {
    "ws:channel:".into()
}
fn default_topics_key() -> String {
    "ws:channel:__topics".into()
}
fn default_ttl_seconds() -> u64 {
    1_800
}
fn default_trim_interval() -> u64 {
    60
}
fn default_max_stream_len() -> u64 {
    500
}
fn default_allowed_origins() -> Vec<String> {
    vec![
        "http://localhost:3000".into(),
        "http://localhost:10101".into(),
    ]
}

impl Default for ServiceConfig {
    fn default() -> Self {
        Self {
            port: default_port(),
            host: default_host(),
            redis_url: default_redis_url(),
            secret_key: default_secret_key(),
            session_cookie_name: default_session_cookie_name(),
            session_max_age: default_session_max_age(),
            stream_prefix: default_stream_prefix(),
            topics_key: default_topics_key(),
            ttl_seconds: default_ttl_seconds(),
            trim_interval: default_trim_interval(),
            max_stream_len: default_max_stream_len(),
            allowed_origins: default_allowed_origins(),
        }
    }
}

impl ServiceConfig {
    pub fn load() -> Result<Self> {
        let path = resolve_path();
        let mut cfg = match path {
            Some(p) if p.is_file() => {
                let raw = std::fs::read_to_string(&p)
                    .with_context(|| format!("reading {}", p.display()))?;
                serde_yaml::from_str::<Self>(&raw)
                    .with_context(|| format!("parsing {}", p.display()))?
            }
            _ => Self::default(),
        };
        if let Ok(p) = env::var("SESSION_CHANNEL_PORT") {
            if let Ok(v) = p.parse() {
                cfg.port = v;
            }
        }
        if let Ok(url) = env::var("SESSION_CHANNEL_REDIS_URL") {
            cfg.redis_url = url;
        }
        if let Ok(origins) = env::var("SESSION_CHANNEL_ALLOWED_ORIGINS") {
            cfg.allowed_origins = origins
                .split(',')
                .map(|s| s.trim().to_string())
                .filter(|s| !s.is_empty())
                .collect();
        }
        Ok(cfg)
    }

    pub fn stream_key(&self, topic: &str) -> String {
        format!("{}{}", self.stream_prefix, topic)
    }
}

fn resolve_path() -> Option<PathBuf> {
    if let Ok(explicit) = env::var("SESSION_CHANNEL_CONFIG") {
        return Some(PathBuf::from(explicit));
    }
    if let Ok(home) = env::var("SESSION_CHANNEL_HOME") {
        let p = Path::new(&home).join("config.yaml");
        if p.is_file() {
            return Some(p);
        }
    }
    let cwd = Path::new("config.yaml");
    if cwd.is_file() {
        return Some(cwd.to_path_buf());
    }
    None
}
