//! HTTP routes. Composed of `core` (send/read/topics/health) and `realtime`
//! (agents/stream SSE).

pub mod core;
pub mod realtime;

use axum::Router;

use crate::service::AppState;

pub fn router() -> Router<AppState> {
    Router::new()
        .merge(core::router())
        .merge(realtime::router())
}
