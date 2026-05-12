//! Thin entry for the `channel-service` binary.
//!
//! Delegates to `session_channel_rs::service::run`. Keeping this file tiny
//! means the library crate can be unit-/integration-tested without a binary
//! `main()` getting in the way.

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    session_channel_rs::service::run().await
}
