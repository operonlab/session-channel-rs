//! Realtime routes — `/api/agents/active`, `/api/stream` (SSE).

use std::time::Duration;

use axum::extract::{Query, State};
use axum::http::{HeaderMap, HeaderName, HeaderValue, StatusCode};
use axum::response::sse::{Event, KeepAlive, Sse};
use axum::response::IntoResponse;
use axum::routing::get;
use axum::Json;
use axum::Router;
use serde::Deserialize;
use serde_json::{json, Value};
use tokio::sync::broadcast::error::RecvError;

use crate::service::AppState;

// ─────────────────────────────────────────────────────────────────────────────
// Router
// ─────────────────────────────────────────────────────────────────────────────

pub fn router() -> Router<AppState> {
    Router::new()
        .route("/api/agents/active", get(agents_active))
        .route("/api/stream", get(sse_stream))
}

// ─────────────────────────────────────────────────────────────────────────────
// Auth helper (inline — separate from core.rs, refactored later by A2)
// ─────────────────────────────────────────────────────────────────────────────

/// Returns `Ok(())` if any auth credential is present, `Err(401)` otherwise.
///
/// Three methods checked in priority order (mirrors Python `require_auth`):
///   1. `x-local-key` header
///   2. `?key=` query string param
///   3. `workshop_session` cookie
///
/// Note: `auth::extract_identity` is a stub (A2's domain). We mirror the
/// same priority logic and accept any non-empty credential as "authenticated"
/// until A2 fills in the real HMAC-SHA1 verification.
fn require_auth(
    headers: &HeaderMap,
    key_query: Option<&str>,
    cfg: &crate::service::config::ServiceConfig,
) -> Result<(), (StatusCode, Json<Value>)> {
    let secret = cfg.secret_key.as_str();

    // 1. x-local-key header
    if let Some(v) = headers.get("x-local-key").and_then(|h| h.to_str().ok()) {
        if v == secret {
            return Ok(());
        }
    }

    // 2. ?key= query param
    if let Some(k) = key_query {
        if k == secret {
            return Ok(());
        }
    }

    // 3. workshop_session cookie (delegates to auth::extract_identity stub)
    let cookie_val = extract_cookie_value(headers, &cfg.session_cookie_name);
    let identity = crate::service::auth::extract_identity(
        headers.get("x-local-key").and_then(|h| h.to_str().ok()),
        key_query,
        cookie_val.as_deref(),
        &cfg.secret_key,
        cfg.session_max_age,
    );
    if identity.is_some() {
        return Ok(());
    }

    Err((
        StatusCode::UNAUTHORIZED,
        Json(json!({"detail": "Not authenticated"})),
    ))
}

/// Pull the value of a named cookie from the `Cookie:` header.
fn extract_cookie_value(headers: &HeaderMap, name: &str) -> Option<String> {
    let raw = headers.get("cookie")?.to_str().ok()?;
    for part in raw.split(';') {
        let part = part.trim();
        if let Some(rest) = part.strip_prefix(name) {
            if let Some(val) = rest.strip_prefix('=') {
                return Some(val.trim().to_string());
            }
        }
    }
    None
}

/// Standard SSE response headers that match the Python reference implementation.
fn sse_headers() -> HeaderMap {
    let mut m = HeaderMap::new();
    m.insert(
        HeaderName::from_static("cache-control"),
        HeaderValue::from_static("no-cache"),
    );
    m.insert(
        HeaderName::from_static("connection"),
        HeaderValue::from_static("keep-alive"),
    );
    m.insert(
        HeaderName::from_static("x-accel-buffering"),
        HeaderValue::from_static("no"),
    );
    m
}

// ─────────────────────────────────────────────────────────────────────────────
// Query param shapes
// ─────────────────────────────────────────────────────────────────────────────

#[derive(Debug, Deserialize)]
struct AgentsQuery {
    /// Look-back window in seconds. Default 300, range [10, 3600].
    #[serde(default = "default_within")]
    within: i64,
    /// Optional local-key passed in the query string.
    key: Option<String>,
}

fn default_within() -> i64 {
    300
}

#[derive(Debug, Deserialize)]
struct StreamQuery {
    /// Filter by topic — empty string = no filter (all topics).
    #[serde(default)]
    topic: String,
    /// Optional local-key passed in the query string.
    key: Option<String>,
}

// ─────────────────────────────────────────────────────────────────────────────
// GET /api/agents/active
// ─────────────────────────────────────────────────────────────────────────────

async fn agents_active(
    State(state): State<AppState>,
    headers: HeaderMap,
    Query(params): Query<AgentsQuery>,
) -> impl IntoResponse {
    // Auth
    if let Err(e) = require_auth(&headers, params.key.as_deref(), &state.cfg) {
        return e.into_response();
    }

    // Validate `within` range [10, 3600]
    if params.within < 10 || params.within > 3600 {
        return (
            StatusCode::BAD_REQUEST,
            Json(json!({
                "error": "within must be between 10 and 3600"
            })),
        )
            .into_response();
    }

    let mut redis = state.redis.clone();
    match crate::service::store::list_active_agents(
        &mut redis,
        &state.cfg.stream_prefix,
        params.within as u64,
    )
    .await
    {
        Ok(agents) => {
            let count = agents.len();
            (
                StatusCode::OK,
                Json(json!({
                    "agents": agents,
                    "count": count,
                    "within": params.within
                })),
            )
                .into_response()
        }
        Err(e) => {
            tracing::error!(error = %e, "list_active_agents failed");
            (
                StatusCode::INTERNAL_SERVER_ERROR,
                Json(json!({"error": "internal error"})),
            )
                .into_response()
        }
    }
}

// ─────────────────────────────────────────────────────────────────────────────
// GET /api/stream  (SSE)
// ─────────────────────────────────────────────────────────────────────────────

async fn sse_stream(
    State(state): State<AppState>,
    headers: HeaderMap,
    Query(params): Query<StreamQuery>,
) -> impl IntoResponse {
    // Auth
    if let Err((status, body)) = require_auth(&headers, params.key.as_deref(), &state.cfg) {
        // SSE endpoint returns plain JSON on auth failure (same as Python)
        return (status, body).into_response();
    }

    let topic_filter = if params.topic.is_empty() {
        None
    } else {
        Some(params.topic.clone())
    };

    let mut rx = state.sse.subscribe();

    let stream = async_stream::stream! {
            loop {
                match rx.recv().await {
                    Ok(envelope) => {
                        // Apply topic filter if set
                        if let Some(ref t) = topic_filter {
                            if envelope.get("topic").and_then(|v| v.as_str()) != Some(t.as_str()) {
                                continue;
                            }
                        }
                        let data = match serde_json::to_string(&envelope) {
                            Ok(s) => s,
                            Err(e) => {
                                tracing::warn!(error = %e, "SSE serialize error");
                                continue;
                            }
                        };
                        yield Ok::<Event, std::convert::Infallible>(Event::default().data(data));
                    }
                    Err(RecvError::Lagged(n)) => {
                        tracing::warn!(dropped = n, "SSE receiver lagged — dropping messages");
                        // Continue streaming; do not disconnect
                        continue;
                    }
                    Err(RecvError::Closed) => {
                        // Broadcast channel closed — end stream cleanly
                        tracing::info!("SSE broadcast channel closed");
                        break;
                    }
                }
            }
        };

    let sse_response = Sse::new(stream).keep_alive(
        KeepAlive::new()
            .interval(Duration::from_secs(30))
            .text("keepalive"),
    );

    // Attach the three parity headers then return
    (sse_headers(), sse_response).into_response()
}
