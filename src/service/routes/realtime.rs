//! Realtime routes — `/api/agents/active`, `/api/stream` (SSE).
//! Stub — filled by code-agent A4.

use axum::routing::get;
use axum::Router;

use crate::service::AppState;

pub fn router() -> Router<AppState> {
    Router::new()
        .route("/api/agents/active", get(stub_agents))
        .route("/api/stream", get(stub_stream))
}

async fn stub_agents() -> &'static str {
    "realtime::agents: not yet implemented (skeleton)"
}
async fn stub_stream() -> &'static str {
    "realtime::stream: not yet implemented (skeleton)"
}
