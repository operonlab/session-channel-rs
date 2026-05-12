//! Core routes — `/health`, `POST /api/messages`, `GET /api/messages/{topic}`,
//! `GET /api/topics`. Stub — filled by code-agent A3.

use axum::routing::{get, post};
use axum::Router;

use crate::service::AppState;

pub fn router() -> Router<AppState> {
    Router::new()
        .route("/health", get(stub_health))
        .route("/api/messages", post(stub_send))
        .route("/api/messages/:topic", get(stub_read))
        .route("/api/topics", get(stub_topics))
}

async fn stub_health() -> &'static str {
    "core::health: not yet implemented (skeleton)"
}
async fn stub_send() -> &'static str {
    "core::send: not yet implemented (skeleton)"
}
async fn stub_read() -> &'static str {
    "core::read: not yet implemented (skeleton)"
}
async fn stub_topics() -> &'static str {
    "core::topics: not yet implemented (skeleton)"
}
