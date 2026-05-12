//! Library crate for session-channel.
//!
//! Exposes the service-side modules (store / auth / routes / config) so the
//! `channel-service` binary AND integration tests can link them.
//!
//! The CLI binary (`src/main.rs`) does NOT use the library — it's a thin
//! HTTP client built on reqwest and has its own private modules.

pub mod service;
