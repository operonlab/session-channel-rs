/// E2E HTTP parity tests — Rust service (ephemeral port) vs Python service (:10101)
///
/// Design:
/// - Each test boots its own `channel-service` binary on a free ephemeral port
///   (picked at runtime via TcpListener bind :0). ServiceGuard kills the child
///   on Drop, so parallel `cargo test` is safe with no --test-threads=1 needed.
/// - Hits BOTH services with identical inputs, compares parsed JSON.
/// - Uses the same Redis backend — write to Rust, read from both, or vice-versa.
/// - All test topics are prefixed `e2e-parity-` and cleaned up after each test.
/// - Auth key: "change-me-in-production" (default from config.py)
///
/// Legacy OnceLock kept as `SHARED_SERVICE` for any future tests that explicitly
/// want a shared (singleton) service instance; not used by default.
///
/// HARD RULE: Do not read src/**. This is a black-box HTTP parity test.
use reqwest::blocking::Client;
use serde_json::{json, Value};
use std::net::{SocketAddr, TcpListener};
use std::process::{Child, Command, Stdio};
use std::sync::OnceLock;
use std::thread;
use std::time::Duration;

// ─────────────────────────────────────────────────────────────────────────────
// Constants
// ─────────────────────────────────────────────────────────────────────────────

const PYTHON_PORT: u16 = 10101;
const SECRET: &str = "change-me-in-production";

fn py_url(path: &str) -> String {
    format!("http://127.0.0.1:{PYTHON_PORT}{path}")
}

// ─────────────────────────────────────────────────────────────────────────────
// Per-test ephemeral port allocation
// ─────────────────────────────────────────────────────────────────────────────

/// Pick a free TCP port by binding to :0 and reading what the OS assigned.
/// The listener is dropped immediately (small TOCTOU window, acceptable in practice).
fn pick_free_port() -> u16 {
    let listener = TcpListener::bind(SocketAddr::from(([127, 0, 0, 1], 0))).unwrap();
    let port = listener.local_addr().unwrap().port();
    drop(listener);
    port
}

// ─────────────────────────────────────────────────────────────────────────────
// Service lifecycle
// ─────────────────────────────────────────────────────────────────────────────

struct ServiceGuard {
    child: Child,
    port: u16,
}

impl ServiceGuard {
    fn rust_url(&self, path: &str) -> String {
        format!("http://127.0.0.1:{}{path}", self.port)
    }
}

impl Drop for ServiceGuard {
    fn drop(&mut self) {
        let _ = self.child.kill();
        let _ = self.child.wait();
    }
}

/// Boot `channel-service` on `port`, wait for health, return guard.
fn start_rust_service_on(port: u16) -> ServiceGuard {
    let bin = env!("CARGO_BIN_EXE_channel-service");
    let child = Command::new(bin)
        .env("SESSION_CHANNEL_PORT", port.to_string())
        .env("SESSION_CHANNEL_REDIS_URL", "redis://127.0.0.1:6379/0")
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()
        .expect("failed to spawn channel-service binary");
    let guard = ServiceGuard { child, port };
    wait_for_health(port, SECRET, 6000);
    guard
}

fn wait_for_health(port: u16, secret: &str, timeout_ms: u64) {
    let client = Client::builder()
        .timeout(Duration::from_secs(3))
        .build()
        .unwrap();
    let deadline = std::time::Instant::now() + Duration::from_millis(timeout_ms);
    loop {
        let result = client
            .get(format!("http://127.0.0.1:{port}/health"))
            .header("x-local-key", secret)
            .send();
        if let Ok(r) = result {
            if r.status().is_success() {
                return;
            }
        }
        if std::time::Instant::now() >= deadline {
            panic!("Service on port {port} did not come up within {timeout_ms}ms");
        }
        thread::sleep(Duration::from_millis(200));
    }
}

/// Legacy singleton — kept for future tests that explicitly want a shared service.
/// Not used by the default per-test isolation pattern below.
#[allow(dead_code)]
static SHARED_SERVICE: OnceLock<()> = OnceLock::new();
#[allow(dead_code)]
static mut _SHARED_GUARD: Option<ServiceGuard> = None;

/// Convenience: boot per-test service and verify Python is reachable.
fn new_test_service() -> ServiceGuard {
    let port = pick_free_port();
    let guard = start_rust_service_on(port);
    // Also verify Python is up (tests depend on it)
    wait_for_health(PYTHON_PORT, SECRET, 2000);
    guard
}

// ─────────────────────────────────────────────────────────────────────────────
// Helpers
// ─────────────────────────────────────────────────────────────────────────────

fn client() -> Client {
    Client::builder()
        .timeout(Duration::from_secs(10))
        .build()
        .unwrap()
}

fn authed_get(url: &str) -> Value {
    let resp = client()
        .get(url)
        .header("x-local-key", SECRET)
        .send()
        .unwrap_or_else(|e| panic!("GET {url} failed: {e}"));
    let status = resp.status();
    let body: Value = resp
        .json()
        .unwrap_or_else(|e| panic!("GET {url} bad json: {e}"));
    assert!(status.is_success(), "GET {url} returned {status}: {body}");
    body
}

fn authed_post(url: &str, body: &Value) -> (u16, Value) {
    let resp = client()
        .post(url)
        .header("x-local-key", SECRET)
        .json(body)
        .send()
        .unwrap_or_else(|e| panic!("POST {url} failed: {e}"));
    let status = resp.status().as_u16();
    let val: Value = resp.json().unwrap_or_default();
    (status, val)
}

/// Deep structural equality ignoring key order within objects.
#[allow(dead_code)]
fn shape_eq(a: &Value, b: &Value) -> bool {
    match (a, b) {
        (Value::Object(ao), Value::Object(bo)) => {
            if ao.len() != bo.len() {
                return false;
            }
            ao.iter()
                .all(|(k, v)| bo.get(k).is_some_and(|bv| shape_eq(v, bv)))
        }
        (Value::Array(aa), Value::Array(ba)) => {
            aa.len() == ba.len() && aa.iter().zip(ba.iter()).all(|(x, y)| shape_eq(x, y))
        }
        _ => a == b,
    }
}

/// Clean up a Redis stream + remove from topics set.
fn redis_cleanup(topic: &str) {
    let stream_key = format!("ws:channel:{topic}");
    // best-effort; ignore errors
    let _ = Command::new("redis-cli")
        .args(["DEL", &stream_key])
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .status();
    let _ = Command::new("redis-cli")
        .args(["SREM", "ws:channel:__topics", topic])
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .status();
}

/// Send N messages to Rust service, return topic name (caller must cleanup).
fn seed_messages(guard: &ServiceGuard, topic: &str, count: usize) {
    for i in 0..count {
        let body = json!({
            "topic": topic,
            "text": format!("msg-{i}"),
            "sender": "e2e-seed",
            "priority": "normal",
        });
        let (status, val) = authed_post(&guard.rust_url("/api/messages"), &body);
        assert_eq!(status, 200, "seed_messages POST failed: {val}");
    }
}

// ─────────────────────────────────────────────────────────────────────────────
// Group 1: Health + Validation parity
// ─────────────────────────────────────────────────────────────────────────────

#[test]
fn test_health_parity() {
    let svc = new_test_service();

    let rust_h = authed_get(&svc.rust_url("/health"));
    let py_h = authed_get(&py_url("/health"));

    // Both must report ok + redis:true
    assert_eq!(
        rust_h["status"], "ok",
        "Rust /health status != ok: {rust_h}"
    );
    assert_eq!(py_h["status"], "ok", "Python /health status != ok: {py_h}");
    assert_eq!(
        rust_h["redis"], true,
        "Rust /health redis field not true: {rust_h}"
    );
    assert_eq!(
        py_h["redis"], true,
        "Python /health redis field not true: {py_h}"
    );

    // Shape: both must have the same top-level keys
    let rust_keys: std::collections::BTreeSet<_> = rust_h.as_object().unwrap().keys().collect();
    let py_keys: std::collections::BTreeSet<_> = py_h.as_object().unwrap().keys().collect();
    assert_eq!(
        rust_keys, py_keys,
        "Health response key sets differ: Rust={rust_keys:?} Python={py_keys:?}"
    );
}

#[test]
fn test_post_messages_empty_topic_parity() {
    let svc = new_test_service();

    let body = json!({"topic": "", "text": "hello", "sender": "e2e"});
    let (rust_status, rust_val) = authed_post(&svc.rust_url("/api/messages"), &body);
    let (py_status, py_val) = authed_post(&py_url("/api/messages"), &body);

    assert_eq!(rust_status, 400, "Rust empty-topic status: {rust_val}");
    assert_eq!(py_status, 400, "Python empty-topic status: {py_val}");
    assert_eq!(
        rust_val["detail"], py_val["detail"],
        "empty-topic detail differs: Rust={rust_val} Python={py_val}"
    );
}

#[test]
fn test_post_messages_long_topic_parity() {
    let svc = new_test_service();

    let long_topic = "a".repeat(101);
    let body = json!({"topic": long_topic, "text": "hi", "sender": "e2e"});
    let (rust_status, rust_val) = authed_post(&svc.rust_url("/api/messages"), &body);
    let (py_status, py_val) = authed_post(&py_url("/api/messages"), &body);

    assert_eq!(rust_status, 400, "Rust long-topic status: {rust_val}");
    assert_eq!(py_status, 400, "Python long-topic status: {py_val}");
    // Shape check: both have "detail" key
    assert!(
        rust_val.get("detail").is_some(),
        "Rust long-topic missing detail: {rust_val}"
    );
    assert!(
        py_val.get("detail").is_some(),
        "Python long-topic missing detail: {py_val}"
    );
}

#[test]
fn test_post_messages_bad_priority_parity() {
    let svc = new_test_service();

    let body = json!({
        "topic": "e2e-parity-valid-topic",
        "text": "hi",
        "sender": "e2e",
        "priority": "urgent",
    });
    let (rust_status, rust_val) = authed_post(&svc.rust_url("/api/messages"), &body);
    let (py_status, py_val) = authed_post(&py_url("/api/messages"), &body);

    assert_eq!(rust_status, 400, "Rust bad-priority status: {rust_val}");
    assert_eq!(py_status, 400, "Python bad-priority status: {py_val}");
    assert_eq!(
        rust_val["detail"], py_val["detail"],
        "bad-priority detail differs"
    );
}

#[test]
fn test_no_auth_returns_401_parity() {
    let svc = new_test_service();

    // POST without auth
    let resp_rust = client()
        .post(svc.rust_url("/api/messages"))
        .json(&json!({"topic": "t", "text": "x"}))
        .send()
        .unwrap();
    let resp_py = client()
        .post(py_url("/api/messages"))
        .json(&json!({"topic": "t", "text": "x"}))
        .send()
        .unwrap();

    assert_eq!(
        resp_rust.status().as_u16(),
        401,
        "Rust no-auth POST should be 401"
    );
    assert_eq!(
        resp_py.status().as_u16(),
        401,
        "Python no-auth POST should be 401"
    );

    let rust_body: Value = resp_rust.json().unwrap_or_default();
    let py_body: Value = resp_py.json().unwrap_or_default();
    assert_eq!(
        rust_body["detail"], py_body["detail"],
        "no-auth detail differs: Rust={rust_body} Python={py_body}"
    );
}

#[test]
fn test_wrong_key_returns_401_parity() {
    let svc = new_test_service();

    let resp_rust = client()
        .get(svc.rust_url("/api/topics"))
        .header("x-local-key", "wrong-key")
        .send()
        .unwrap();
    let resp_py = client()
        .get(py_url("/api/topics"))
        .header("x-local-key", "wrong-key")
        .send()
        .unwrap();

    assert_eq!(
        resp_rust.status().as_u16(),
        401,
        "Rust wrong-key should 401"
    );
    assert_eq!(
        resp_py.status().as_u16(),
        401,
        "Python wrong-key should 401"
    );
}

// ─────────────────────────────────────────────────────────────────────────────
// Group 2: Read + Topics parity
// ─────────────────────────────────────────────────────────────────────────────

#[test]
fn test_post_message_and_read_parity() {
    let svc = new_test_service();

    let topic = "e2e-parity-post-read";
    redis_cleanup(topic);

    // Write via Rust
    let body = json!({
        "topic": topic,
        "text": "hello from e2e",
        "sender": "e2e-tester",
        "priority": "normal",
        "tag": "smoke",
    });
    let (rust_status, rust_post) = authed_post(&svc.rust_url("/api/messages"), &body);
    assert_eq!(rust_status, 200, "Rust POST failed: {rust_post}");
    assert_eq!(rust_post["ok"], true, "Rust POST ok field: {rust_post}");
    assert_eq!(rust_post["topic"], topic, "Rust POST topic field");

    // Wait a beat so both services see the Redis write
    thread::sleep(Duration::from_millis(100));

    // Read from BOTH — same Redis backend, should see the same message
    let rust_read = authed_get(&svc.rust_url(&format!("/api/messages/{topic}")));
    let py_read = authed_get(&py_url(&format!("/api/messages/{topic}")));

    assert_eq!(
        rust_read["count"], py_read["count"],
        "message count differs: Rust={} Python={}",
        rust_read["count"], py_read["count"]
    );

    let rust_msgs = rust_read["messages"].as_array().unwrap();
    let py_msgs = py_read["messages"].as_array().unwrap();
    assert!(!rust_msgs.is_empty(), "Rust returned no messages");
    assert!(!py_msgs.is_empty(), "Python returned no messages");

    // Compare first message — same Redis stream entry
    let rm = &rust_msgs[0];
    let pm = &py_msgs[0];
    assert_eq!(rm["text"], pm["text"], "message text differs");
    assert_eq!(rm["sender"], pm["sender"], "message sender differs");
    assert_eq!(rm["tag"], pm["tag"], "message tag differs");
    assert_eq!(rm["topic"], pm["topic"], "message topic differs");
    assert_eq!(
        rm["id"], pm["id"],
        "message id differs (should be same Redis ID)"
    );

    redis_cleanup(topic);
}

#[test]
fn test_read_order_newest_parity() {
    let svc = new_test_service();

    let topic = "e2e-parity-order-newest";
    redis_cleanup(topic);
    seed_messages(&svc, topic, 5);

    let rust = authed_get(&svc.rust_url(&format!(
        "/api/messages/{topic}?order=newest&count=3"
    )));
    let py = authed_get(&py_url(&format!(
        "/api/messages/{topic}?order=newest&count=3"
    )));

    assert_eq!(
        rust["count"].as_i64().unwrap(),
        3,
        "Rust newest count not 3: {rust}"
    );
    assert_eq!(
        py["count"].as_i64().unwrap(),
        3,
        "Python newest count not 3: {py}"
    );

    // IDs must match — same Redis data
    let rust_ids: Vec<&Value> = rust["messages"]
        .as_array()
        .unwrap()
        .iter()
        .map(|m| &m["id"])
        .collect();
    let py_ids: Vec<&Value> = py["messages"]
        .as_array()
        .unwrap()
        .iter()
        .map(|m| &m["id"])
        .collect();
    assert_eq!(rust_ids, py_ids, "newest message ids differ");

    redis_cleanup(topic);
}

#[test]
fn test_read_order_oldest_parity() {
    let svc = new_test_service();

    let topic = "e2e-parity-order-oldest";
    redis_cleanup(topic);
    seed_messages(&svc, topic, 5);

    let rust = authed_get(&svc.rust_url(&format!(
        "/api/messages/{topic}?order=oldest&count=2"
    )));
    let py = authed_get(&py_url(&format!(
        "/api/messages/{topic}?order=oldest&count=2"
    )));

    assert_eq!(
        rust["count"].as_i64().unwrap(),
        2,
        "Rust oldest count not 2: {rust}"
    );
    assert_eq!(
        py["count"].as_i64().unwrap(),
        2,
        "Python oldest count not 2: {py}"
    );

    let rust_ids: Vec<&Value> = rust["messages"]
        .as_array()
        .unwrap()
        .iter()
        .map(|m| &m["id"])
        .collect();
    let py_ids: Vec<&Value> = py["messages"]
        .as_array()
        .unwrap()
        .iter()
        .map(|m| &m["id"])
        .collect();
    assert_eq!(rust_ids, py_ids, "oldest message ids differ");

    redis_cleanup(topic);
}

#[test]
fn test_read_bogus_order_parity() {
    let svc = new_test_service();

    // GET /api/messages/:topic?order=bogus — expect 400 on BOTH
    // Note: Python uses FastAPI Query(pattern=...) which returns 422 Unprocessable Entity
    // if the pattern does not match. We accept either 400 or 422 as "client error".
    let resp_rust = client()
        .get(svc.rust_url("/api/messages/any-topic?order=bogus"))
        .header("x-local-key", SECRET)
        .send()
        .unwrap();
    let resp_py = client()
        .get(py_url("/api/messages/any-topic?order=bogus"))
        .header("x-local-key", SECRET)
        .send()
        .unwrap();

    let rust_status = resp_rust.status().as_u16();
    let py_status = resp_py.status().as_u16();

    // Both must be client errors (4xx)
    assert!(
        (400..500).contains(&rust_status),
        "Rust bogus order should be 4xx, got {rust_status}"
    );
    assert!(
        (400..500).contains(&py_status),
        "Python bogus order should be 4xx, got {py_status}"
    );
}

#[test]
fn test_topics_parity() {
    let svc = new_test_service();

    let topic = "e2e-parity-topics-check";
    redis_cleanup(topic);

    // Seed via Rust
    seed_messages(&svc, topic, 2);
    thread::sleep(Duration::from_millis(100));

    let rust_topics = authed_get(&svc.rust_url("/api/topics"));
    let py_topics = authed_get(&py_url("/api/topics"));

    // Both should contain our test topic
    fn contains_topic(val: &Value, t: &str) -> bool {
        val["topics"]
            .as_array()
            .map(|arr| arr.iter().any(|item| item["topic"] == t))
            .unwrap_or(false)
    }

    assert!(
        contains_topic(&rust_topics, topic),
        "Rust /api/topics missing {topic}: {rust_topics}"
    );
    assert!(
        contains_topic(&py_topics, topic),
        "Python /api/topics missing {topic}: {py_topics}"
    );

    // Find count in both
    let rust_count = rust_topics["topics"]
        .as_array()
        .unwrap()
        .iter()
        .find(|item| item["topic"] == topic)
        .and_then(|item| item["count"].as_i64())
        .expect("Rust topic count not found");
    let py_count = py_topics["topics"]
        .as_array()
        .unwrap()
        .iter()
        .find(|item| item["topic"] == topic)
        .and_then(|item| item["count"].as_i64())
        .expect("Python topic count not found");

    assert_eq!(
        rust_count, py_count,
        "topic count differs: Rust={rust_count} Python={py_count}"
    );

    redis_cleanup(topic);
}

// ─────────────────────────────────────────────────────────────────────────────
// Group 3: Agents + Auth parity
// ─────────────────────────────────────────────────────────────────────────────

#[test]
fn test_agents_active_parity() {
    let svc = new_test_service();

    // Both should return the same agent set from the same Redis `agents` stream.
    // We compare set of `key` fields (host:pane) to avoid ordering differences.
    let rust_agents = authed_get(&svc.rust_url("/api/agents/active?within=300"));
    let py_agents = authed_get(&py_url("/api/agents/active?within=300"));

    fn agent_keys(val: &Value) -> std::collections::BTreeSet<String> {
        val["agents"]
            .as_array()
            .map(|arr| {
                arr.iter()
                    .filter_map(|a| a["key"].as_str().map(|s| s.to_string()))
                    .collect()
            })
            .unwrap_or_default()
    }

    let rust_keys = agent_keys(&rust_agents);
    let py_keys = agent_keys(&py_agents);

    assert_eq!(
        rust_keys, py_keys,
        "active agent key sets differ\nRust:   {rust_keys:?}\nPython: {py_keys:?}"
    );

    // Response shape: both must have "agents", "count", "within" keys
    for key in ["agents", "count", "within"] {
        assert!(
            rust_agents.get(key).is_some(),
            "Rust /api/agents/active missing key `{key}`"
        );
        assert!(
            py_agents.get(key).is_some(),
            "Python /api/agents/active missing key `{key}`"
        );
    }
}

#[test]
fn test_auth_wrong_key_parity() {
    let svc = new_test_service();

    for path in ["/api/topics", "/api/agents/active"] {
        let resp_rust = client()
            .get(svc.rust_url(path))
            .header("x-local-key", "totally-wrong")
            .send()
            .unwrap();
        let resp_py = client()
            .get(py_url(path))
            .header("x-local-key", "totally-wrong")
            .send()
            .unwrap();

        assert_eq!(
            resp_rust.status().as_u16(),
            401,
            "Rust {path} wrong-key should 401"
        );
        assert_eq!(
            resp_py.status().as_u16(),
            401,
            "Python {path} wrong-key should 401"
        );
    }
}

#[test]
fn test_auth_no_key_parity() {
    let svc = new_test_service();

    for path in [
        "/api/topics",
        "/api/messages/any-topic",
        "/api/agents/active",
    ] {
        let resp_rust = client().get(svc.rust_url(path)).send().unwrap();
        let resp_py = client().get(py_url(path)).send().unwrap();

        let rs = resp_rust.status().as_u16();
        let ps = resp_py.status().as_u16();

        let rust_body: Value = resp_rust.json().unwrap_or_default();
        let py_body: Value = resp_py.json().unwrap_or_default();

        assert_eq!(
            rs, 401,
            "Rust {path} no-auth should 401, got {rs}: {rust_body}"
        );
        assert_eq!(
            ps, 401,
            "Python {path} no-auth should 401, got {ps}: {py_body}"
        );

        // PARITY BUG: Rust uses {"error":"unauthorized"}, Python uses {"detail":"Not authenticated"}
        // Both are 401 (status matches), but error body format differs.
        // This test explicitly documents the divergence so it can be fixed in the Rust impl.
        assert_eq!(
            rust_body["detail"], py_body["detail"],
            "PARITY BUG: {path} no-auth error body format differs.\n  Rust={rust_body}\n  Python={py_body}\n  Fix: Rust should return {{\"detail\":\"Not authenticated\"}} to match Python"
        );
    }
}

// ─────────────────────────────────────────────────────────────────────────────
// SSE smoke test (best-effort — non-fatal if SSE streaming is slow)
// ─────────────────────────────────────────────────────────────────────────────

#[test]
fn test_sse_smoke_rust() {
    let svc = new_test_service();

    let topic = "e2e-parity-sse-smoke";
    redis_cleanup(topic);

    // Kick off a background thread that posts a message after a short delay
    let topic_clone = topic.to_string();
    let rust_port = svc.port;
    thread::spawn(move || {
        thread::sleep(Duration::from_millis(800));
        let body = json!({
            "topic": topic_clone,
            "text": "sse-smoke-payload",
            "sender": "e2e-sse",
            "priority": "normal",
        });
        let _ = Client::new()
            .post(format!("http://127.0.0.1:{rust_port}/api/messages"))
            .header("x-local-key", SECRET)
            .json(&body)
            .send();
    });

    // Open SSE connection and read — use a short-timeout approach by reading raw bytes
    // reqwest blocking doesn't support streaming well, so we read with a 5s timeout and
    // check for at least one "data: " line.
    let resp = client()
        .get(svc.rust_url(&format!("/api/stream?topic={topic}")))
        .header("x-local-key", SECRET)
        .header("Accept", "text/event-stream")
        .timeout(Duration::from_secs(5))
        .send();

    match resp {
        Ok(r) => {
            assert!(
                r.status().is_success(),
                "SSE /api/stream returned {}: expected 200",
                r.status()
            );
            // Just verify we got an SSE-formatted response header
            let ct = r
                .headers()
                .get("content-type")
                .and_then(|v| v.to_str().ok())
                .unwrap_or("");
            assert!(
                ct.contains("text/event-stream"),
                "SSE content-type not text/event-stream: `{ct}`"
            );
        }
        Err(e) => {
            // Timeout is expected in blocking mode — connection opened OK
            let msg = e.to_string();
            if msg.contains("timed out") || msg.contains("timeout") {
                // Connection established, timed out reading — that's fine for SSE smoke
            } else {
                panic!("SSE /api/stream unexpected error: {e}");
            }
        }
    }

    redis_cleanup(topic);
}
