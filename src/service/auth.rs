//! Signed cookie / x-local-key auth. Stub — filled by code-agent A2.
//!
//! 1:1 with Python `auth.py`:
//!   - x-local-key header OR `?key=` query param matching `secret_key` →
//!     accept as `{ "status": "active", "source": "local-key" }`
//!   - else: cookie `workshop_session` validated via itsdangerous
//!     URLSafeTimedSerializer (HMAC-SHA1, max_age = session_max_age)
//!   - reject (401) otherwise

use anyhow::Result;
use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct AuthIdentity {
    pub status: String,
    pub source: String,
}

/// Sign a payload string using itsdangerous URLSafeTimedSerializer-compatible
/// format. The output can be sent as the `workshop_session` cookie value and
/// read back by a Python session-channel service for full compatibility.
pub fn sign(_secret_key: &str, _payload: &str) -> Result<String> {
    anyhow::bail!("auth::sign: not yet implemented (skeleton)")
}

/// Verify + decode a cookie value. Returns the payload string if the
/// signature is valid and the timestamp is within `max_age_seconds`.
pub fn verify(_secret_key: &str, _cookie: &str, _max_age_seconds: u64) -> Result<String> {
    anyhow::bail!("auth::verify: not yet implemented (skeleton)")
}

/// Helper used by routes: extract the requesting identity from header /
/// query-param / cookie. None → unauthenticated (caller should 401).
pub fn extract_identity(
    _local_key_header: Option<&str>,
    _key_query: Option<&str>,
    _cookie_value: Option<&str>,
    _secret_key: &str,
    _max_age_seconds: u64,
) -> Option<AuthIdentity> {
    // Stub — A2 implements full logic. For now treat absence as "no identity".
    None
}
