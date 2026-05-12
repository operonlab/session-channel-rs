//! Redis Streams operations. Stub — filled by code-agent A1.
//!
//! The HTTP routes call into these helpers; keeping them in one module lets
//! the data-flow test agent target the same surface without coupling to
//! handler internals.

use anyhow::Result;
use redis::aio::ConnectionManager;
use serde_json::Value;
use tokio::sync::broadcast;

/// One entry retrieved from XRANGE / XREVRANGE — fields preserved as-is.
#[derive(Debug, Clone)]
pub struct StreamEntry {
    pub id: String,
    pub fields: Vec<(String, String)>,
}

/// XADD a message to `{stream_prefix}{topic}` and register the topic in
/// the topic-set. Returns the new message id.
pub async fn publish(
    _redis: &mut ConnectionManager,
    _stream_prefix: &str,
    _topics_key: &str,
    _topic: &str,
    _fields: &[(&str, &str)],
    _max_len: u64,
) -> Result<String> {
    anyhow::bail!("store::publish: not yet implemented (skeleton)")
}

/// XRANGE — oldest order, `since` cursor inclusive.
pub async fn range_oldest(
    _redis: &mut ConnectionManager,
    _stream_prefix: &str,
    _topic: &str,
    _since: &str,
    _count: u64,
) -> Result<Vec<StreamEntry>> {
    anyhow::bail!("store::range_oldest: not yet implemented (skeleton)")
}

/// XREVRANGE — newest order, last `count` entries.
pub async fn range_newest(
    _redis: &mut ConnectionManager,
    _stream_prefix: &str,
    _topic: &str,
    _count: u64,
) -> Result<Vec<StreamEntry>> {
    anyhow::bail!("store::range_newest: not yet implemented (skeleton)")
}

/// Topics: SMEMBERS + XLEN for each. Returns `(topic, count)` sorted alphabetically.
/// Empty streams are SREM'd from the topic-set as a side effect (Python parity).
pub async fn list_topics(
    _redis: &mut ConnectionManager,
    _stream_prefix: &str,
    _topics_key: &str,
) -> Result<Vec<(String, u64)>> {
    anyhow::bail!("store::list_topics: not yet implemented (skeleton)")
}

/// Health ping — returns (redis_ok, active_topic_count).
pub async fn health_check(
    _redis: &mut ConnectionManager,
    _topics_key: &str,
) -> Result<(bool, u64)> {
    anyhow::bail!("store::health_check: not yet implemented (skeleton)")
}

/// Compute snapshot of active agents within the `within` window (seconds).
pub async fn list_active_agents(
    _redis: &mut ConnectionManager,
    _stream_prefix: &str,
    _within_seconds: u64,
) -> Result<Vec<Value>> {
    anyhow::bail!("store::list_active_agents: not yet implemented (skeleton)")
}

/// Background trim — drop messages older than `ttl_seconds` across all topics.
pub async fn trim_expired(
    _redis: &mut ConnectionManager,
    _topics_key: &str,
    _stream_prefix: &str,
    _ttl_seconds: u64,
) -> Result<()> {
    // No-op stub — real implementation iterates SMEMBERS, XTRIM each stream,
    // SREM empty topics. Filled by A1.
    Ok(())
}

/// Background XREAD fanout — read new entries from every stream and forward
/// to the broadcast channel. Runs forever.
pub async fn fanout_loop(
    _redis: &mut ConnectionManager,
    _topics_key: &str,
    _stream_prefix: &str,
    _sse: broadcast::Sender<Value>,
) -> Result<()> {
    // No-op stub — real implementation does XREAD with a cursor map.
    futures::future::pending::<()>().await;
    Ok(())
}
