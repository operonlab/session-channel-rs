//! Shared axum `State<AppState>` — Redis connection + SSE broadcaster + config.

use std::sync::Arc;
use std::time::Duration;

use anyhow::{Context, Result};
use redis::aio::ConnectionManager;
use serde_json::Value;
use tokio::sync::broadcast;

use crate::service::config::ServiceConfig;

const SSE_CHANNEL_CAPACITY: usize = 1024;

#[derive(Clone)]
pub struct AppState {
    pub cfg: Arc<ServiceConfig>,
    pub redis: ConnectionManager,
    /// Tokio broadcast channel — every published message is sent here so
    /// SSE handlers can subscribe and stream.
    pub sse: broadcast::Sender<Value>,
}

impl AppState {
    pub async fn connect(cfg: ServiceConfig) -> Result<Self> {
        let client =
            redis::Client::open(cfg.redis_url.clone()).context("invalid redis URL")?;
        let redis = ConnectionManager::new(client)
            .await
            .context("redis ConnectionManager")?;
        let (sse, _) = broadcast::channel(SSE_CHANNEL_CAPACITY);
        Ok(Self {
            cfg: Arc::new(cfg),
            redis,
            sse,
        })
    }

    /// Spawn the background trim + fanout loops. Should be called once at
    /// startup. Cancelled implicitly on process exit.
    pub fn spawn_background_tasks(&self) {
        let trim = TrimLoop {
            cfg: self.cfg.clone(),
            redis: self.redis.clone(),
        };
        tokio::spawn(async move {
            trim.run().await;
        });

        let fanout = FanoutLoop {
            cfg: self.cfg.clone(),
            redis: self.redis.clone(),
            sse: self.sse.clone(),
        };
        tokio::spawn(async move {
            fanout.run().await;
        });
    }
}

struct TrimLoop {
    cfg: Arc<ServiceConfig>,
    redis: ConnectionManager,
}

impl TrimLoop {
    async fn run(mut self) {
        let interval = Duration::from_secs(self.cfg.trim_interval.max(1));
        loop {
            tokio::time::sleep(interval).await;
            if let Err(e) = self.tick().await {
                tracing::warn!(error = %e, "trim_loop error");
                tokio::time::sleep(Duration::from_secs(10)).await;
            }
        }
    }

    async fn tick(&mut self) -> anyhow::Result<()> {
        crate::service::store::trim_expired(
            &mut self.redis,
            &self.cfg.topics_key,
            &self.cfg.stream_prefix,
            self.cfg.ttl_seconds,
        )
        .await
    }
}

struct FanoutLoop {
    cfg: Arc<ServiceConfig>,
    redis: ConnectionManager,
    sse: broadcast::Sender<Value>,
}

impl FanoutLoop {
    async fn run(mut self) {
        if let Err(e) = self.run_inner().await {
            tracing::warn!(error = %e, "fanout_loop terminated");
        }
    }

    async fn run_inner(&mut self) -> anyhow::Result<()> {
        crate::service::store::fanout_loop(
            &mut self.redis,
            &self.cfg.topics_key,
            &self.cfg.stream_prefix,
            self.sse.clone(),
        )
        .await
    }
}
