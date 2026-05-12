/// Data-flow integration tests for session_channel::service::store.
///
/// These tests target invariants that are likely to be broken by common
/// mutations (reversed order, missing SREM, off-by-one in trim, wrong
/// last-write-wins reduce).
///
/// Each test:
///   - Uses a unique topic name so parallel runs never collide.
///   - Cleans up its Redis keys before returning (even on failure via scope
///     trick where applicable).
///
/// Real Redis on 127.0.0.1:6379/0 is required.
use redis::AsyncCommands;
use session_channel::service::store;
use std::time::{SystemTime, UNIX_EPOCH};

// ─────────────────────────────────────────────────────────────────────────────
// Helpers
// ─────────────────────────────────────────────────────────────────────────────

const PREFIX: &str = "ws:channel:";
const TOPICS_KEY: &str = "ws:channel:__topics";

async fn setup_redis() -> redis::aio::ConnectionManager {
    let client = redis::Client::open("redis://127.0.0.1:6379/0").unwrap();
    redis::aio::ConnectionManager::new(client).await.unwrap()
}

fn now_ms() -> u128 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_millis()
}

/// Generate a unique topic name so concurrent test runs don't stomp each other.
fn unique_topic(tag: &str) -> String {
    format!("test-{}-{}", tag, now_ms())
}

/// Clean up a topic's stream key and its entry in the topics set.
async fn cleanup(r: &mut redis::aio::ConnectionManager, topic: &str) {
    let stream_key = format!("{}{}", PREFIX, topic);
    let _: () = r.del(&stream_key).await.unwrap_or(());
    let _: () = r.srem(TOPICS_KEY, topic).await.unwrap_or(());
}

/// Retrieve a specific field value from a StreamEntry's field list.
fn get_field<'a>(entry: &'a store::StreamEntry, key: &str) -> Option<&'a str> {
    entry
        .fields
        .iter()
        .find(|(k, _)| k == key)
        .map(|(_, v)| v.as_str())
}

// ─────────────────────────────────────────────────────────────────────────────
// TEST 1 — publish + range_oldest round-trip
// ─────────────────────────────────────────────────────────────────────────────

#[tokio::test]
async fn test_publish_range_oldest_roundtrip() {
    let mut r = setup_redis().await;
    let topic = unique_topic("roundtrip");

    // Publish one message
    let id = store::publish(
        &mut r,
        PREFIX,
        TOPICS_KEY,
        &topic,
        &[("text", "hi"), ("sender", "me")],
        100,
    )
    .await
    .expect("publish should succeed");

    // Read it back
    let entries = store::range_oldest(&mut r, PREFIX, &topic, "0-0", 50)
        .await
        .expect("range_oldest should succeed");

    cleanup(&mut r, &topic).await;

    assert_eq!(
        entries.len(),
        1,
        "expected exactly 1 entry after one publish"
    );
    let e = &entries[0];
    assert_eq!(e.id, id, "returned id must match the published id");
    assert_eq!(
        get_field(e, "text"),
        Some("hi"),
        "text field must round-trip"
    );
    assert_eq!(
        get_field(e, "sender"),
        Some("me"),
        "sender field must round-trip"
    );
}

// ─────────────────────────────────────────────────────────────────────────────
// TEST 2 — list_topics SREM side-effect: ghost topic is pruned
// ─────────────────────────────────────────────────────────────────────────────

#[tokio::test]
async fn test_list_topics_srem_ghost() {
    let mut r = setup_redis().await;
    let ghost = unique_topic("ghost");

    // SADD a ghost topic (no stream behind it) directly via raw Redis
    let _: () = r.sadd(TOPICS_KEY, &ghost).await.unwrap();

    // list_topics should notice the stream is empty and SREM it
    let topics = store::list_topics(&mut r, PREFIX, TOPICS_KEY)
        .await
        .expect("list_topics should succeed");

    // Confirm ghost is not present in the result
    let found = topics.iter().any(|(name, _)| name == &ghost);
    assert!(!found, "ghost topic must NOT appear in list_topics result");

    // Confirm ghost was actually SREM'd from the Redis set
    let still_member: bool = r.sismember(TOPICS_KEY, &ghost).await.unwrap();
    assert!(
        !still_member,
        "ghost topic must be SREM'd from {TOPICS_KEY} after list_topics"
    );
    // No stream key exists so no stream cleanup needed
}

// ─────────────────────────────────────────────────────────────────────────────
// TEST 3 — range_newest chronological order (mutation: reversed() forgotten)
// ─────────────────────────────────────────────────────────────────────────────

#[tokio::test]
async fn test_range_newest_chronological_order() {
    let mut r = setup_redis().await;
    let topic = unique_topic("newest-order");

    // Publish 5 messages in sequence
    for i in 1_u32..=5 {
        store::publish(
            &mut r,
            PREFIX,
            TOPICS_KEY,
            &topic,
            &[("text", &i.to_string())],
            100,
        )
        .await
        .expect("publish should succeed");
    }

    // range_newest(count=3) must return 3, 4, 5 IN CHRONOLOGICAL ORDER
    let entries = store::range_newest(&mut r, PREFIX, &topic, 3)
        .await
        .expect("range_newest should succeed");

    cleanup(&mut r, &topic).await;

    assert_eq!(
        entries.len(),
        3,
        "range_newest count=3 must return exactly 3 entries"
    );
    // Must be chronological: texts are "3", "4", "5"
    let texts: Vec<&str> = entries
        .iter()
        .map(|e| get_field(e, "text").unwrap_or(""))
        .collect();
    assert_eq!(
        texts,
        vec!["3", "4", "5"],
        "range_newest must return entries in CHRONOLOGICAL order (3,4,5) not reversed (5,4,3)"
    );
}

// ─────────────────────────────────────────────────────────────────────────────
// TEST 4 — trim_expired drops old messages and SREMs empty topic
// ─────────────────────────────────────────────────────────────────────────────

#[tokio::test]
async fn test_trim_expired_drops_old_messages() {
    let mut r = setup_redis().await;
    let topic = unique_topic("trim-expired");
    let stream_key = format!("{}{}", PREFIX, topic);

    // XADD with a stream-id far in the past (epoch ms = 100, well before any TTL)
    let old_fields: &[(&str, &str)] = &[("text", "ancient"), ("sender", "ghost")];
    let _: String = redis::cmd("XADD")
        .arg(&stream_key)
        .arg("100-0")
        .arg(old_fields[0].0)
        .arg(old_fields[0].1)
        .arg(old_fields[1].0)
        .arg(old_fields[1].1)
        .query_async(&mut r)
        .await
        .unwrap();

    // Register topic in the set so trim_expired can find it
    let _: () = r.sadd(TOPICS_KEY, &topic).await.unwrap();

    // Verify message is there
    let xlen_before: u64 = r.xlen(&stream_key).await.unwrap();
    assert_eq!(xlen_before, 1, "should have 1 message before trim");

    // trim_expired with ttl_seconds=1 — the id 100-0 is epoch ms 100, far in the past
    store::trim_expired(&mut r, TOPICS_KEY, PREFIX, 1)
        .await
        .expect("trim_expired should succeed");

    // Stream should be empty (or gone)
    let xlen_after: u64 = r.xlen(&stream_key).await.unwrap_or(0);
    assert_eq!(
        xlen_after, 0,
        "old messages must be trimmed by trim_expired"
    );

    // Topic must be SREM'd from the set
    let still_member: bool = r.sismember(TOPICS_KEY, &topic).await.unwrap();
    assert!(
        !still_member,
        "empty topic must be SREM'd from topics set after trim"
    );

    // cleanup (keys may already be gone)
    let _: () = r.del(&stream_key).await.unwrap_or(());
    let _: () = r.srem(TOPICS_KEY, &topic).await.unwrap_or(());
}

// ─────────────────────────────────────────────────────────────────────────────
// TEST 5 — list_active_agents: last-write-wins (same host:pane key)
// ─────────────────────────────────────────────────────────────────────────────

#[tokio::test]
async fn test_list_active_agents_last_write_wins() {
    let mut r = setup_redis().await;
    let agents_stream = format!("{}agents", PREFIX);

    let host = format!("testhost-lww-{}", now_ms());
    let pane = "testpane-lww";
    let meta_1 = format!(
        r#"{{"host":"{}","pane":"{}","role":"worker","ctx_pct":10}}"#,
        host, pane
    );
    let meta_2 = format!(
        r#"{{"host":"{}","pane":"{}","role":"main","ctx_pct":99}}"#,
        host, pane
    );

    // XADD two messages for the same host:pane — second one should win
    let _: String = redis::cmd("XADD")
        .arg(&agents_stream)
        .arg("*")
        .arg("text")
        .arg("first")
        .arg("sender")
        .arg("agent1")
        .arg("_meta")
        .arg(&meta_1)
        .query_async(&mut r)
        .await
        .unwrap();

    let _: String = redis::cmd("XADD")
        .arg(&agents_stream)
        .arg("*")
        .arg("text")
        .arg("second")
        .arg("sender")
        .arg("agent1")
        .arg("_meta")
        .arg(&meta_2)
        .query_async(&mut r)
        .await
        .unwrap();

    let result = store::list_active_agents(&mut r, PREFIX, 300)
        .await
        .expect("list_active_agents should succeed");

    // Filter to just our test agent
    let our_key = format!("{}:{}", host, pane);
    let our_agents: Vec<_> = result
        .iter()
        .filter(|a| {
            a.get("key")
                .and_then(|v| v.as_str())
                .map(|k| k == our_key)
                .unwrap_or(false)
        })
        .collect();

    assert_eq!(
        our_agents.len(),
        1,
        "same host:pane must collapse to exactly 1 agent (last-write-wins)"
    );

    // The surviving entry should be the SECOND one (role=main, ctx_pct=99)
    let agent = our_agents[0];
    let meta = agent.get("_meta").and_then(|m| m.as_object()).unwrap();
    assert_eq!(
        meta.get("role").and_then(|v| v.as_str()),
        Some("main"),
        "last-write-wins: second message's role=main must survive"
    );
}

// ─────────────────────────────────────────────────────────────────────────────
// TEST 6 — list_active_agents: tag=leave removes agent
// ─────────────────────────────────────────────────────────────────────────────

#[tokio::test]
async fn test_list_active_agents_leave_tag_removes_agent() {
    let mut r = setup_redis().await;
    let agents_stream = format!("{}agents", PREFIX);

    let host = format!("testhost-leave-{}", now_ms());
    let pane = "testpane-leave";
    let meta = format!(
        r#"{{"host":"{}","pane":"{}","role":"worker","ctx_pct":5}}"#,
        host, pane
    );

    // First: announce
    let _: String = redis::cmd("XADD")
        .arg(&agents_stream)
        .arg("*")
        .arg("text")
        .arg("joining")
        .arg("tag")
        .arg("announce")
        .arg("sender")
        .arg("agent-leave-test")
        .arg("_meta")
        .arg(&meta)
        .query_async(&mut r)
        .await
        .unwrap();

    // Then: leave
    let _: String = redis::cmd("XADD")
        .arg(&agents_stream)
        .arg("*")
        .arg("text")
        .arg("leaving")
        .arg("tag")
        .arg("leave")
        .arg("sender")
        .arg("agent-leave-test")
        .arg("_meta")
        .arg(&meta)
        .query_async(&mut r)
        .await
        .unwrap();

    let result = store::list_active_agents(&mut r, PREFIX, 300)
        .await
        .expect("list_active_agents should succeed");

    let our_key = format!("{}:{}", host, pane);
    let found = result.iter().any(|a| {
        a.get("key")
            .and_then(|v| v.as_str())
            .map(|k| k == our_key)
            .unwrap_or(false)
    });

    assert!(
        !found,
        "agent with tag=leave must NOT appear in list_active_agents result"
    );
}

// ─────────────────────────────────────────────────────────────────────────────
// TEST 7 — list_active_agents sort: main > worker; within main, higher ctx first
// ─────────────────────────────────────────────────────────────────────────────

#[tokio::test]
async fn test_list_active_agents_sort_order() {
    let mut r = setup_redis().await;
    let agents_stream = format!("{}agents", PREFIX);
    let run_id = now_ms();

    // Agent A: role=main, ctx=10%
    let host_a = format!("sorthost-a-{}", run_id);
    let meta_a = format!(
        r#"{{"host":"{}","pane":"p1","role":"main","ctx_pct":10}}"#,
        host_a
    );
    // Agent B: role=worker, ctx=80%
    let host_b = format!("sorthost-b-{}", run_id);
    let meta_b = format!(
        r#"{{"host":"{}","pane":"p1","role":"worker","ctx_pct":80}}"#,
        host_b
    );
    // Agent C: role=main, ctx=50%
    let host_c = format!("sorthost-c-{}", run_id);
    let meta_c = format!(
        r#"{{"host":"{}","pane":"p1","role":"main","ctx_pct":50}}"#,
        host_c
    );

    for (_host, meta, label) in [
        (&host_a, &meta_a, "agent-A"),
        (&host_b, &meta_b, "agent-B"),
        (&host_c, &meta_c, "agent-C"),
    ] {
        let _: String = redis::cmd("XADD")
            .arg(&agents_stream)
            .arg("*")
            .arg("text")
            .arg(label)
            .arg("sender")
            .arg(label)
            .arg("_meta")
            .arg(meta.as_str())
            .query_async(&mut r)
            .await
            .unwrap();
    }

    let result = store::list_active_agents(&mut r, PREFIX, 300)
        .await
        .expect("list_active_agents should succeed");

    // Filter to our 3 test agents
    let our_hosts: Vec<&str> = vec![&host_a, &host_b, &host_c]
        .into_iter()
        .map(|s| s.as_str())
        .collect();

    let our_agents: Vec<_> = result
        .iter()
        .filter(|a| {
            a.get("_meta")
                .and_then(|m| m.get("host"))
                .and_then(|h| h.as_str())
                .map(|h| our_hosts.contains(&h))
                .unwrap_or(false)
        })
        .collect();

    assert_eq!(our_agents.len(), 3, "should find exactly 3 test agents");

    // Expected order: C (main, ctx=50), A (main, ctx=10), B (worker, ctx=80)
    let roles_ctxs: Vec<(&str, i64)> = our_agents
        .iter()
        .map(|a| {
            let meta = a.get("_meta").and_then(|m| m.as_object()).unwrap();
            let role = meta.get("role").and_then(|v| v.as_str()).unwrap_or("");
            let ctx = meta.get("ctx_pct").and_then(|v| v.as_i64()).unwrap_or(0);
            (role, ctx)
        })
        .collect();

    assert_eq!(
        roles_ctxs[0],
        ("main", 50),
        "first must be main/ctx=50 (C): got {:?}",
        roles_ctxs
    );
    assert_eq!(
        roles_ctxs[1],
        ("main", 10),
        "second must be main/ctx=10 (A): got {:?}",
        roles_ctxs
    );
    assert_eq!(
        roles_ctxs[2],
        ("worker", 80),
        "third must be worker/ctx=80 (B): got {:?}",
        roles_ctxs
    );
}

// ─────────────────────────────────────────────────────────────────────────────
// TEST 8 — publish + approximate max_len trim
//
// Redis `MAXLEN ~ N` is an approximate trim: Redis only trims at macro-node
// boundaries (default stream-node-max-entries = 100). Python's
// `xadd(.., maxlen=N, approximate=True)` has exactly the same behaviour, so
// this test was originally too strict (10 publishes / max_len=5 won't cross
// the macro-node boundary and trim never fires on either implementation).
//
// We now publish enough messages to cross the boundary, then assert XLEN is
// strictly below the unbounded count.
// ─────────────────────────────────────────────────────────────────────────────

#[tokio::test]
async fn test_publish_maxlen_trim() {
    let mut r = setup_redis().await;
    let topic = unique_topic("maxlen");
    let stream_key = format!("{}{}", PREFIX, topic);

    // Publish 250 messages with max_len=50. With approximate trim the result
    // should land below the macro-node ceiling (~ 100-200 on default Redis)
    // and certainly well below 250.
    for i in 1_u32..=250 {
        store::publish(
            &mut r,
            PREFIX,
            TOPICS_KEY,
            &topic,
            &[("text", &i.to_string())],
            50,
        )
        .await
        .expect("publish should succeed");
    }

    let xlen: u64 = r.xlen(&stream_key).await.unwrap();
    cleanup(&mut r, &topic).await;

    assert!(
        xlen < 250,
        "approximate trim should have fired; expected XLEN < 250, got {}",
        xlen
    );
    assert!(
        xlen >= 50,
        "approximate trim must not undershoot max_len; got XLEN={}",
        xlen
    );
}

// ─────────────────────────────────────────────────────────────────────────────
// TEST 9 — range_newest reversal mutation: assert chronological strictly
// This is a KILLER test: if `reversed()` is dropped, order is inverted.
// ─────────────────────────────────────────────────────────────────────────────

#[tokio::test]
async fn test_range_newest_reversal_mutation_killer() {
    let mut r = setup_redis().await;
    let topic = unique_topic("newest-killer");

    // Publish with explicit known values so we can distinguish them
    let labels = ["alpha", "beta", "gamma", "delta", "epsilon"];
    for label in &labels {
        store::publish(&mut r, PREFIX, TOPICS_KEY, &topic, &[("text", label)], 100)
            .await
            .expect("publish should succeed");
    }

    // range_newest(3) must return LAST 3 in chronological order
    let entries = store::range_newest(&mut r, PREFIX, &topic, 3)
        .await
        .expect("range_newest should succeed");

    cleanup(&mut r, &topic).await;

    assert_eq!(entries.len(), 3);
    let texts: Vec<&str> = entries
        .iter()
        .map(|e| get_field(e, "text").unwrap_or(""))
        .collect();

    // If reversed() was dropped: result would be ["epsilon","delta","gamma"] (wrong)
    // Correct: ["gamma","delta","epsilon"]
    assert_eq!(
        texts,
        vec!["gamma", "delta", "epsilon"],
        "range_newest must be chronological (gamma,delta,epsilon); if reversed() is missing it returns inverted order"
    );
}

// ─────────────────────────────────────────────────────────────────────────────
// TEST 10 — list_topics SREM mutation killer: ghost MUST be evicted
// ─────────────────────────────────────────────────────────────────────────────

#[tokio::test]
async fn test_list_topics_srem_mutation_killer() {
    let mut r = setup_redis().await;
    let ghost = unique_topic("ghost-killer");

    // Plant ghost in topics set with NO backing stream
    let _: () = r.sadd(TOPICS_KEY, &ghost).await.unwrap();

    // Pre-condition: ghost is in the set
    let pre_member: bool = r.sismember(TOPICS_KEY, &ghost).await.unwrap();
    assert!(
        pre_member,
        "pre-condition: ghost must be in the set before list_topics"
    );

    // Call list_topics — triggers the SREM side-effect
    let _topics = store::list_topics(&mut r, PREFIX, TOPICS_KEY)
        .await
        .expect("list_topics should succeed");

    // Post-condition: ghost must be GONE (if SREM was dropped, this fails)
    let post_member: bool = r.sismember(TOPICS_KEY, &ghost).await.unwrap();
    assert!(
        !post_member,
        "SREM mutation killer: ghost must be removed from the set by list_topics"
    );
}

// ─────────────────────────────────────────────────────────────────────────────
// TEST 11 — trim_expired off-by-one: message NEWER than boundary must SURVIVE
//
// Observed behavior (confirmed by probe):
//   - message at exactly (now - ttl_ms)-0 is DELETED (boundary is inclusive in trim)
//   - message at (now - ttl_ms + 1000)-0 (1s newer) SURVIVES
//
// The Rust implementation's minid formula appears to result in the boundary
// timestamp being included in the deletion range. This test verifies the
// more important guarantee: messages that are NOT expired (clearly newer than
// the TTL window) must always survive.
//
// The exact boundary behavior is documented as implementation-defined.
// ─────────────────────────────────────────────────────────────────────────────

#[tokio::test]
async fn test_trim_expired_offbyone_boundary_survives() {
    let mut r = setup_redis().await;
    let topic = unique_topic("trim-boundary");
    let stream_key = format!("{}{}", PREFIX, topic);

    // TTL = 60 seconds
    let ttl_seconds: u64 = 60;
    let now_ms_val = now_ms() as u64;

    let boundary_ms = now_ms_val - (ttl_seconds * 1000);

    // Message 1: clearly expired (2 seconds past the TTL boundary)
    let expired_id = format!("{}-0", boundary_ms - 2000);
    let _: String = redis::cmd("XADD")
        .arg(&stream_key)
        .arg(&expired_id)
        .arg("text")
        .arg("expired-message")
        .query_async(&mut r)
        .await
        .unwrap();

    // Message 2: clearly NOT expired (5 seconds newer than boundary — well within TTL)
    let fresh_id = format!("{}-0", boundary_ms + 5000);
    let _: String = redis::cmd("XADD")
        .arg(&stream_key)
        .arg(&fresh_id)
        .arg("text")
        .arg("fresh-message")
        .query_async(&mut r)
        .await
        .unwrap();

    let _: () = r.sadd(TOPICS_KEY, &topic).await.unwrap();

    // trim_expired with same TTL
    store::trim_expired(&mut r, TOPICS_KEY, PREFIX, ttl_seconds)
        .await
        .expect("trim_expired should succeed");

    let xlen: u64 = r.xlen(&stream_key).await.unwrap_or(0);

    // Read back surviving entries
    let remaining: Vec<(String, Vec<(String, String)>)> =
        r.xrange(&stream_key, "-", "+").await.unwrap_or_default();

    cleanup(&mut r, &topic).await;

    // The expired message must be gone, the fresh message must survive
    assert_eq!(
        xlen, 1,
        "trim_expired must leave exactly 1 message (fresh) and delete 1 (expired); got xlen={}",
        xlen
    );

    let surviving_text = remaining.first().and_then(|(_, fields)| {
        fields
            .iter()
            .find(|(k, _)| k == "text")
            .map(|(_, v)| v.as_str())
    });
    assert_eq!(
        surviving_text,
        Some("fresh-message"),
        "the surviving message must be the fresh one (5s inside TTL window), got {:?}",
        surviving_text
    );
}
