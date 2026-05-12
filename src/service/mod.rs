//! Service-side modules: config / store (Redis ops) / auth (signed cookie) /
//! routes (HTTP handlers) / state (shared app state).
//!
//! The `channel-service` binary is a thin entry that constructs the axum
//! Router from these modules and listens on `${PORT}` (default 10101).

pub mod auth;
pub mod config;
pub mod routes;
pub mod sse;
pub mod state;
pub mod store;

pub use config::ServiceConfig;
pub use state::AppState;

use anyhow::{Context, Result};
use axum::Router;
use std::net::SocketAddr;
use tower_http::cors::{Any, CorsLayer};
use tower_http::trace::TraceLayer;

/// Entry point for the `channel-service` binary. Wired in `src/bin/channel-service.rs`.
pub async fn run() -> Result<()> {
    init_tracing();

    let cfg = ServiceConfig::load().context("loading service config")?;
    tracing::info!(port = cfg.port, redis = %cfg.redis_url, "starting channel-service");

    let state = AppState::connect(cfg.clone())
        .await
        .context("connecting to Redis")?;

    // Background tasks (trim loop, fanout loop)
    state.spawn_background_tasks();

    let cors = build_cors(&cfg);

    let app = Router::new()
        .merge(routes::router())
        .layer(cors)
        .layer(TraceLayer::new_for_http())
        .with_state(state);

    let addr: SocketAddr = format!("{}:{}", cfg.host, cfg.port)
        .parse()
        .context("invalid host:port")?;
    let listener = tokio::net::TcpListener::bind(addr)
        .await
        .with_context(|| format!("binding {addr}"))?;
    tracing::info!(%addr, "listening");

    axum::serve(listener, app)
        .with_graceful_shutdown(shutdown_signal())
        .await
        .context("serve error")?;

    Ok(())
}

fn init_tracing() {
    use tracing_subscriber::{fmt, EnvFilter};
    let filter = EnvFilter::try_from_default_env()
        .unwrap_or_else(|_| EnvFilter::new("info,tower_http=warn,hyper=warn"));
    let _ = fmt().with_env_filter(filter).try_init();
}

fn build_cors(cfg: &ServiceConfig) -> CorsLayer {
    // Mirrors Python main.py CORS setup; allow_origins read from config.
    // If allowed_origins is empty we fall through to permissive (dev) — same
    // behaviour as Python's empty list path.
    let cors = CorsLayer::new()
        .allow_methods(Any)
        .allow_headers(Any);

    if cfg.allowed_origins.is_empty() {
        cors.allow_origin(Any)
    } else {
        let mut origins = Vec::with_capacity(cfg.allowed_origins.len() + 1);
        for o in &cfg.allowed_origins {
            if let Ok(v) = o.parse() {
                origins.push(v);
            }
        }
        // Self-origin always appended (port may be ephemeral in tests).
        if let Ok(self_origin) = format!("http://localhost:{}", cfg.port).parse() {
            if !origins.iter().any(|o| o == &self_origin) {
                origins.push(self_origin);
            }
        }
        cors.allow_origin(origins)
    }
}

async fn shutdown_signal() {
    let ctrl_c = async {
        let _ = tokio::signal::ctrl_c().await;
    };
    #[cfg(unix)]
    let terminate = async {
        if let Ok(mut sig) =
            tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate())
        {
            sig.recv().await;
        }
    };
    #[cfg(not(unix))]
    let terminate = std::future::pending::<()>();

    tokio::select! {
        _ = ctrl_c => {},
        _ = terminate => {},
    }
    tracing::info!("shutdown signal received");
}
