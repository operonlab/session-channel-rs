//! SSE broadcast helper shared by the publish path and stream handler.

use serde_json::Value;
use tokio::sync::broadcast;

/// Push one envelope into the broadcast channel. Lossy: if no subscribers
/// or the channel is full, the message is dropped — matches Python's
/// "drop slow clients" semantics.
pub fn broadcast(sender: &broadcast::Sender<Value>, payload: Value) {
    let _ = sender.send(payload);
}
