//! Core routes — `/health`, `POST /api/messages`, `GET /api/messages/{topic}`,
//! `GET /api/topics`. Implemented by code-agent A3.
//!
//! Note: `store::*` and `auth::extract_identity` are still stubs (filled by A1/A2).
//! This file compiles cleanly against those stub signatures.

use std::collections::HashMap;
use std::sync::{Mutex, OnceLock};
use std::time::Instant;

use axum::extract::{Path, Query, State};
use axum::http::{HeaderMap, StatusCode};
use axum::response::IntoResponse;
use axum::routing::{get, post};
use axum::{Json, Router};
use serde::Deserialize;
use serde_json::{json, Value};

use crate::service::AppState;

// ─── Error type ──────────────────────────────────────────────────────────────

struct ApiError(StatusCode, String);

impl IntoResponse for ApiError {
    fn into_response(self) -> axum::response::Response {
        (self.0, Json(json!({"detail": self.1}))).into_response()
    }
}

macro_rules! api_err {
    ($status:expr, $msg:expr) => {
        return Err(ApiError($status, $msg.to_string()))
    };
}

// ─── Rate-limit state (module-private static) ────────────────────────────────

static RATE_MAP: OnceLock<Mutex<HashMap<String, Vec<Instant>>>> = OnceLock::new();

fn rate_map() -> &'static Mutex<HashMap<String, Vec<Instant>>> {
    RATE_MAP.get_or_init(|| Mutex::new(HashMap::new()))
}

/// Returns `Err` with 429 if sender has sent ≥ 10 messages in the last second.
fn check_rate(sender: &str) -> Result<(), ApiError> {
    let mut map = rate_map().lock().unwrap();
    let now = Instant::now();
    let window = map.entry(sender.to_string()).or_default();
    window.retain(|t| now.duration_since(*t).as_secs_f64() < 1.0);
    if window.len() >= 10 {
        return Err(ApiError(
            StatusCode::TOO_MANY_REQUESTS,
            "Rate limit exceeded (10 msg/s)".into(),
        ));
    }
    window.push(now);
    Ok(())
}

// ─── Auth helper ─────────────────────────────────────────────────────────────

/// Returns true if any auth source produces a valid identity.
/// Reads x-local-key header, ?key= query param, or workshop_session cookie.
fn is_authenticated(
    headers: &HeaderMap,
    key_query: Option<&str>,
    cfg: &crate::service::ServiceConfig,
) -> bool {
    let local_key_header = headers.get("x-local-key").and_then(|v| v.to_str().ok());

    // Manual cookie parse — no `cookie` crate available.
    let cookie_value: Option<String> = headers
        .get(axum::http::header::COOKIE)
        .and_then(|v| v.to_str().ok())
        .and_then(|raw| {
            raw.split(';').find_map(|part| {
                let part = part.trim();
                part.strip_prefix("workshop_session=")
                    .map(|v| v.to_string())
            })
        });

    crate::service::auth::extract_identity(
        local_key_header,
        key_query,
        cookie_value.as_deref(),
        &cfg.secret_key,
        cfg.session_max_age,
    )
    .is_some()
}

// ─── Router ──────────────────────────────────────────────────────────────────

pub fn router() -> Router<AppState> {
    Router::new()
        .route("/health", get(health))
        .route("/api/messages", post(send_message))
        .route("/api/messages/:topic", get(read_messages))
        .route("/api/topics", get(list_topics))
}

// ─── /health — no auth required ──────────────────────────────────────────────

async fn health(State(mut state): State<AppState>) -> Json<Value> {
    match crate::service::store::health_check(&mut state.redis, &state.cfg.topics_key).await {
        Ok((redis_ok, active_topics)) => Json(json!({
            "status": if redis_ok { "ok" } else { "degraded" },
            "redis": redis_ok,
            "active_topics": active_topics,
        })),
        Err(_) => Json(json!({
            "status": "degraded",
            "redis": false,
            "active_topics": 0_u64,
        })),
    }
}

// ─── Shared query params ──────────────────────────────────────────────────────

#[derive(Deserialize)]
struct KeyQuery {
    key: Option<String>,
}

// ─── GET /api/messages/:topic ─────────────────────────────────────────────────

#[derive(Deserialize)]
struct ReadQuery {
    since: Option<String>,
    count: Option<u64>,
    order: Option<String>,
    key: Option<String>,
}

async fn read_messages(
    State(mut state): State<AppState>,
    headers: HeaderMap,
    Path(topic): Path<String>,
    Query(q): Query<ReadQuery>,
) -> Result<Json<Value>, ApiError> {
    if !is_authenticated(&headers, q.key.as_deref(), &state.cfg) {
        api_err!(StatusCode::UNAUTHORIZED, "Not authenticated");
    }

    let since = q.since.unwrap_or_else(|| "0-0".into());
    let count = q.count.unwrap_or(50).min(200);
    let order = q.order.unwrap_or_else(|| "oldest".into());

    if order != "oldest" && order != "newest" {
        api_err!(StatusCode::BAD_REQUEST, "order must be oldest or newest");
    }

    let entries = if order == "newest" {
        crate::service::store::range_newest(
            &mut state.redis,
            &state.cfg.stream_prefix,
            &topic,
            count,
        )
        .await
    } else {
        crate::service::store::range_oldest(
            &mut state.redis,
            &state.cfg.stream_prefix,
            &topic,
            &since,
            count,
        )
        .await
    }
    .map_err(|e| ApiError(StatusCode::INTERNAL_SERVER_ERROR, e.to_string()))?;

    let messages: Vec<Value> = entries
        .into_iter()
        .map(|entry| {
            let mut map = serde_json::Map::new();
            map.insert("id".into(), Value::String(entry.id));
            map.insert("topic".into(), Value::String(topic.clone()));
            for (k, v) in entry.fields {
                if k == "_meta" {
                    // _hydrate_meta: parse to JSON object; keep as string on failure.
                    if let Ok(parsed) = serde_json::from_str::<Value>(&v) {
                        map.insert(k, parsed);
                    } else {
                        map.insert(k, Value::String(v));
                    }
                } else {
                    map.insert(k, Value::String(v));
                }
            }
            Value::Object(map)
        })
        .collect();

    let count_out = messages.len();
    Ok(Json(json!({
        "messages": messages,
        "count": count_out,
    })))
}

// ─── GET /api/topics ─────────────────────────────────────────────────────────

async fn list_topics(
    State(mut state): State<AppState>,
    headers: HeaderMap,
    Query(kq): Query<KeyQuery>,
) -> Result<Json<Value>, ApiError> {
    if !is_authenticated(&headers, kq.key.as_deref(), &state.cfg) {
        api_err!(StatusCode::UNAUTHORIZED, "Not authenticated");
    }

    let topics = crate::service::store::list_topics(
        &mut state.redis,
        &state.cfg.stream_prefix,
        &state.cfg.topics_key,
    )
    .await
    .map_err(|e| ApiError(StatusCode::INTERNAL_SERVER_ERROR, e.to_string()))?;

    let result: Vec<Value> = topics
        .into_iter()
        .map(|(t, n)| json!({"topic": t, "count": n}))
        .collect();

    Ok(Json(json!({"topics": result})))
}

// ─── POST /api/messages ──────────────────────────────────────────────────────

#[derive(Deserialize)]
struct SendBody {
    #[serde(default)]
    topic: String,
    #[serde(default)]
    text: String,
    #[serde(default)]
    sender: String,
    #[serde(default)]
    tag: String,
    #[serde(default)]
    priority: String,
    #[serde(default, rename = "_meta")]
    meta: Option<Value>,
}

async fn send_message(
    State(mut state): State<AppState>,
    headers: HeaderMap,
    Query(kq): Query<KeyQuery>,
    Json(body): Json<SendBody>,
) -> Result<Json<Value>, ApiError> {
    if !is_authenticated(&headers, kq.key.as_deref(), &state.cfg) {
        api_err!(StatusCode::UNAUTHORIZED, "Not authenticated");
    }

    let topic = body.topic.trim();
    let text = body.text.trim();
    let sender = if body.sender.trim().is_empty() {
        "anon"
    } else {
        body.sender.trim()
    };
    let tag = body.tag.trim();
    let priority = if body.priority.trim().is_empty() {
        "normal"
    } else {
        body.priority.trim()
    };

    if topic.is_empty() || text.is_empty() {
        api_err!(StatusCode::BAD_REQUEST, "topic and text required");
    }
    if topic.len() > 100 || text.len() > 4096 {
        api_err!(
            StatusCode::BAD_REQUEST,
            "topic max 100, text max 4096 chars"
        );
    }
    if priority != "normal" && priority != "high" {
        api_err!(StatusCode::BAD_REQUEST, "priority must be normal or high");
    }

    // Serialize _meta to JSON string for the hash field (Python parity).
    let meta_string: Option<String> = match &body.meta {
        Some(Value::Object(_)) => {
            let s = serde_json::to_string(&body.meta)
                .map_err(|e| ApiError(StatusCode::BAD_REQUEST, e.to_string()))?;
            if s.len() > 8192 {
                api_err!(StatusCode::BAD_REQUEST, "_meta max 8192 chars");
            }
            Some(s)
        }
        Some(_) => None, // ignore non-object meta (Python silently drops)
        None => None,
    };

    check_rate(sender)?;

    // Build the fields list (sender, text, priority, optional tag, optional _meta).
    let mut fields: Vec<(&str, &str)> =
        vec![("sender", sender), ("text", text), ("priority", priority)];
    if !tag.is_empty() {
        fields.push(("tag", tag));
    }
    if let Some(ref m) = meta_string {
        fields.push(("_meta", m.as_str()));
    }

    let msg_id = crate::service::store::publish(
        &mut state.redis,
        &state.cfg.stream_prefix,
        &state.cfg.topics_key,
        topic,
        &fields,
        state.cfg.max_stream_len,
    )
    .await
    .map_err(|e| ApiError(StatusCode::INTERNAL_SERVER_ERROR, e.to_string()))?;

    // Broadcast to SSE clients.
    let mut envelope = serde_json::Map::new();
    envelope.insert("id".into(), Value::String(msg_id.clone()));
    envelope.insert("topic".into(), Value::String(topic.to_string()));
    envelope.insert("sender".into(), Value::String(sender.to_string()));
    envelope.insert("text".into(), Value::String(text.to_string()));
    envelope.insert("priority".into(), Value::String(priority.to_string()));
    if !tag.is_empty() {
        envelope.insert("tag".into(), Value::String(tag.to_string()));
    }
    if let Some(parsed) = body.meta {
        if parsed.is_object() {
            envelope.insert("_meta".into(), parsed);
        }
    }
    crate::service::sse::broadcast(&state.sse, Value::Object(envelope));

    Ok(Json(json!({
        "ok": true,
        "id": msg_id,
        "topic": topic,
    })))
}
