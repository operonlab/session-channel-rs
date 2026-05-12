//! Redis Streams operations. Stub — filled by code-agent A1.
//!
//! The HTTP routes call into these helpers; keeping them in one module lets
//! the data-flow test agent target the same surface without coupling to
//! handler internals.

use std::collections::HashMap;
use std::time::{SystemTime, UNIX_EPOCH};

use anyhow::Result;
use redis::aio::ConnectionManager;
use redis::streams::{StreamMaxlen, StreamRangeReply, StreamReadOptions, StreamReadReply};
use redis::AsyncCommands;
use serde_json::{Map, Value};
use tokio::sync::broadcast;

/// One entry retrieved from XRANGE / XREVRANGE — fields preserved as-is.
#[derive(Debug, Clone)]
pub struct StreamEntry {
    pub id: String,
    pub fields: Vec<(String, String)>,
}

// ─── private helpers ──────────────────────────────────────────────────────────

/// Current Unix time in milliseconds.
fn now_ms() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_millis() as u64
}

/// Parse a Redis bulk-string map (from StreamId.map) into Vec<(String,String)>.
fn redis_map_to_fields(
    map: &std::collections::HashMap<String, redis::Value>,
) -> Vec<(String, String)> {
    map.iter()
        .filter_map(|(k, v)| {
            let s = match v {
                redis::Value::BulkString(b) => String::from_utf8_lossy(b).into_owned(),
                redis::Value::SimpleString(s) => s.clone(),
                other => format!("{:?}", other),
            };
            Some((k.clone(), s))
        })
        .collect()
}

/// Re-hydrate the `_meta` field in a JSON object: if it is a string, parse it
/// into a JSON object in-place (best-effort — leaves it as string on error).
fn hydrate_meta(obj: &mut Map<String, Value>) {
    if let Some(Value::String(raw)) = obj.get("_meta").cloned() {
        if let Ok(parsed) = serde_json::from_str::<Value>(&raw) {
            obj.insert("_meta".to_string(), parsed);
        }
        // on error: leave as string — matches Python _hydrate_meta behaviour
    }
}

// ─── public surface ───────────────────────────────────────────────────────────

/// XADD a message to `{stream_prefix}{topic}` and register the topic in
/// the topic-set. Returns the new message id.
pub async fn publish(
    redis: &mut ConnectionManager,
    stream_prefix: &str,
    topics_key: &str,
    topic: &str,
    fields: &[(&str, &str)],
    max_len: u64,
) -> Result<String> {
    let stream_key = format!("{}{}", stream_prefix, topic);

    // XADD with approximate MAXLEN (the `~` modifier — Python parity)
    let raw_id: redis::Value = redis
        .xadd_maxlen(
            &stream_key,
            StreamMaxlen::Approx(max_len as usize),
            "*",
            fields,
        )
        .await?;

    let msg_id = match raw_id {
        redis::Value::BulkString(b) => String::from_utf8_lossy(&b).into_owned(),
        redis::Value::SimpleString(s) => s,
        other => format!("{:?}", other),
    };

    // Track topic in set so we can avoid SCAN later
    redis.sadd::<_, _, ()>(topics_key, topic).await?;

    Ok(msg_id)
}

/// XRANGE — oldest order, `since` cursor inclusive.
pub async fn range_oldest(
    redis: &mut ConnectionManager,
    stream_prefix: &str,
    topic: &str,
    since: &str,
    count: u64,
) -> Result<Vec<StreamEntry>> {
    let stream_key = format!("{}{}", stream_prefix, topic);

    let reply: StreamRangeReply = redis
        .xrange_count(&stream_key, since, "+", count as usize)
        .await?;

    let entries = reply
        .ids
        .into_iter()
        .map(|sid| StreamEntry {
            id: sid.id,
            fields: redis_map_to_fields(&sid.map),
        })
        .collect();

    Ok(entries)
}

/// XREVRANGE — newest order, last `count` entries returned in chronological order.
pub async fn range_newest(
    redis: &mut ConnectionManager,
    stream_prefix: &str,
    topic: &str,
    count: u64,
) -> Result<Vec<StreamEntry>> {
    let stream_key = format!("{}{}", stream_prefix, topic);

    // XREVRANGE returns newest-first; we reverse to restore chronological order
    // (mirrors Python: `list(reversed(raw))`)
    let reply: StreamRangeReply = redis
        .xrevrange_count(&stream_key, "+", "-", count as usize)
        .await?;

    let mut entries: Vec<StreamEntry> = reply
        .ids
        .into_iter()
        .map(|sid| StreamEntry {
            id: sid.id,
            fields: redis_map_to_fields(&sid.map),
        })
        .collect();

    entries.reverse();
    Ok(entries)
}

/// Topics: SMEMBERS + XLEN for each. Returns `(topic, count)` sorted alphabetically.
/// Empty streams are SREM'd from the topic-set as a side effect (Python parity).
pub async fn list_topics(
    redis: &mut ConnectionManager,
    stream_prefix: &str,
    topics_key: &str,
) -> Result<Vec<(String, u64)>> {
    let raw: Vec<String> = redis.smembers(topics_key).await?;

    let mut result: Vec<(String, u64)> = Vec::new();

    let mut sorted = raw;
    sorted.sort();

    for t in sorted {
        let stream_key = format!("{}{}", stream_prefix, t);
        let length: u64 = redis.xlen(&stream_key).await?;
        if length > 0 {
            result.push((t, length));
        } else {
            // Clean up empty topic from set (Python parity side-effect)
            redis.srem::<_, _, ()>(topics_key, &t).await?;
        }
    }

    Ok(result)
}

/// Health ping — returns (redis_ok, active_topic_count).
pub async fn health_check(redis: &mut ConnectionManager, topics_key: &str) -> Result<(bool, u64)> {
    let ping_ok = redis
        .send_packed_command(&redis::cmd("PING"))
        .await
        .map(|_| true)
        .unwrap_or(false);

    let topic_count: u64 = redis.scard(topics_key).await.unwrap_or(0);

    Ok((ping_ok, topic_count))
}

/// Compute snapshot of active agents within the `within` window (seconds).
pub async fn list_active_agents(
    redis: &mut ConnectionManager,
    stream_prefix: &str,
    within_seconds: u64,
) -> Result<Vec<Value>> {
    let stream_key = format!("{}agents", stream_prefix);
    let min_ts_ms = now_ms().saturating_sub(within_seconds * 1000);
    let min_id = format!("{}-0", min_ts_ms);

    // XRANGE with count=2000 (Python parity)
    let reply: StreamRangeReply = redis
        .xrange_count(&stream_key, &min_id, "+", 2000usize)
        .await?;

    // last-write-wins per host:pane key
    let mut seen: HashMap<String, Value> = HashMap::new();

    for sid in reply.ids {
        let fields = redis_map_to_fields(&sid.map);
        let fields_map: HashMap<String, String> = fields.iter().cloned().collect();

        // Parse _meta
        let meta: Map<String, Value> = fields_map
            .get("_meta")
            .and_then(|raw| serde_json::from_str(raw).ok())
            .unwrap_or_default();

        let host = meta
            .get("host")
            .and_then(|v| v.as_str())
            .unwrap_or("?")
            .to_string();
        let pane = meta
            .get("pane")
            .and_then(|v| v.as_str())
            .or_else(|| fields_map.get("sender").map(|s| s.as_str()))
            .unwrap_or("?")
            .to_string();
        let key = format!("{}:{}", host, pane);

        // Parse timestamp from stream id
        let ts_ms: u64 = sid
            .id
            .split('-')
            .next()
            .and_then(|s| s.parse().ok())
            .unwrap_or(0);

        let tag = fields_map.get("tag").cloned().unwrap_or_default();

        if tag == "leave" {
            seen.remove(&key);
            continue;
        }

        let mut entry = Map::new();
        entry.insert("id".into(), Value::String(sid.id));
        entry.insert("key".into(), Value::String(key.clone()));
        entry.insert("ts_ms".into(), Value::Number(ts_ms.into()));
        entry.insert(
            "last_seen".into(),
            Value::Number(
                serde_json::Number::from_f64(ts_ms as f64 / 1000.0)
                    .unwrap_or(serde_json::Number::from(0)),
            ),
        );
        entry.insert("tag".into(), Value::String(tag));
        entry.insert(
            "sender".into(),
            Value::String(fields_map.get("sender").cloned().unwrap_or_default()),
        );
        entry.insert(
            "text".into(),
            Value::String(fields_map.get("text").cloned().unwrap_or_default()),
        );
        // _meta is already a parsed JSON object
        entry.insert("_meta".into(), Value::Object(meta));

        seen.insert(key, Value::Object(entry));
    }

    // Sort: role=="main" first, then -ctx_pct, then -ts_ms
    let mut agents: Vec<Value> = seen.into_values().collect();
    agents.sort_by(|a, b| {
        let meta_a = a
            .get("_meta")
            .and_then(|m| m.as_object())
            .cloned()
            .unwrap_or_default();
        let meta_b = b
            .get("_meta")
            .and_then(|m| m.as_object())
            .cloned()
            .unwrap_or_default();

        let role_rank_a: i32 = if meta_a.get("role").and_then(|v| v.as_str()) == Some("main") {
            0
        } else {
            1
        };
        let role_rank_b: i32 = if meta_b.get("role").and_then(|v| v.as_str()) == Some("main") {
            0
        } else {
            1
        };

        let ctx_a: f64 = meta_a
            .get("ctx_pct")
            .and_then(|v| v.as_f64())
            .unwrap_or(0.0);
        let ctx_b: f64 = meta_b
            .get("ctx_pct")
            .and_then(|v| v.as_f64())
            .unwrap_or(0.0);

        let ts_a: u64 = a.get("ts_ms").and_then(|v| v.as_u64()).unwrap_or(0);
        let ts_b: u64 = b.get("ts_ms").and_then(|v| v.as_u64()).unwrap_or(0);

        role_rank_a
            .cmp(&role_rank_b)
            .then(
                ctx_b
                    .partial_cmp(&ctx_a)
                    .unwrap_or(std::cmp::Ordering::Equal),
            )
            .then(ts_b.cmp(&ts_a))
    });

    Ok(agents)
}

/// Background trim — drop messages older than `ttl_seconds` across all topics.
pub async fn trim_expired(
    redis: &mut ConnectionManager,
    topics_key: &str,
    stream_prefix: &str,
    ttl_seconds: u64,
) -> Result<()> {
    use redis::streams::{StreamTrimOptions, StreamTrimmingMode};

    let min_id = format!("{}-0", now_ms().saturating_sub(ttl_seconds * 1000));
    let topics: Vec<String> = redis.smembers(topics_key).await?;

    for t in topics {
        let stream_key = format!("{}{}", stream_prefix, t);
        let opts = StreamTrimOptions::minid(StreamTrimmingMode::Exact, &min_id);
        // Returns number of trimmed entries (we ignore the count, as Python does)
        let _: u64 = redis.xtrim_options(&stream_key, &opts).await.unwrap_or(0);

        let length: u64 = redis.xlen(&stream_key).await.unwrap_or(0);
        if length == 0 {
            redis.srem::<_, _, ()>(topics_key, &t).await.ok();
        }
    }

    Ok(())
}

/// Background XREAD fanout — read new entries from every stream and forward
/// to the broadcast channel. Runs forever.
pub async fn fanout_loop(
    redis: &mut ConnectionManager,
    topics_key: &str,
    stream_prefix: &str,
    sse: broadcast::Sender<Value>,
) -> Result<()> {
    // cursor map: stream_key → last id seen (start with "$" = only new)
    let mut last_ids: HashMap<String, String> = HashMap::new();

    loop {
        // Refresh topic list each iteration (topics may be added dynamically)
        let topics: Vec<String> = match redis.smembers::<_, Vec<String>>(topics_key).await {
            Ok(t) => t,
            Err(_) => {
                tokio::time::sleep(tokio::time::Duration::from_secs(2)).await;
                continue;
            }
        };

        if topics.is_empty() {
            tokio::time::sleep(tokio::time::Duration::from_secs(2)).await;
            continue;
        }

        // Build stream_keys + id vectors for XREAD
        let stream_keys: Vec<String> = topics
            .iter()
            .map(|t| format!("{}{}", stream_prefix, t))
            .collect();

        let ids: Vec<String> = stream_keys
            .iter()
            .map(|k| last_ids.get(k).cloned().unwrap_or_else(|| "$".to_string()))
            .collect();

        // XREAD BLOCK 2000ms COUNT 50
        let opts = StreamReadOptions::default().block(2000).count(50);
        let reply: Option<StreamReadReply> =
            redis.xread_options(&stream_keys, &ids, &opts).await.ok();

        let reply = match reply {
            Some(r) => r,
            None => continue,
        };

        for stream_key_entry in reply.keys {
            let sk = stream_key_entry.key.clone();
            // Derive topic by stripping prefix
            let topic = sk.strip_prefix(stream_prefix).unwrap_or(&sk).to_string();

            for sid in stream_key_entry.ids {
                // Update cursor
                last_ids.insert(sk.clone(), sid.id.clone());

                let fields = redis_map_to_fields(&sid.map);

                // Build JSON envelope
                let mut obj = Map::new();
                obj.insert("id".into(), Value::String(sid.id));
                obj.insert("topic".into(), Value::String(topic.clone()));

                for (k, v) in &fields {
                    obj.insert(k.clone(), Value::String(v.clone()));
                }

                // Re-hydrate _meta from JSON string → JSON object (best-effort)
                hydrate_meta(&mut obj);

                // Ignore send errors (no receivers is OK)
                let _ = sse.send(Value::Object(obj));
            }
        }
    }
}
