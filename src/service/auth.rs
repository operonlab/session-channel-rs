//! Signed cookie / x-local-key auth — 1:1 with Python `auth.py`.
//!
//! Wire format mirrors `itsdangerous.URLSafeTimedSerializer`:
//!
//!   cookie = b64(payload) + "." + b64(ts_be) + "." + b64(hmac_sha1)
//!
//! Key derivation (`KEY_DERIVATION = "django-concat"`, the itsdangerous default):
//!   hmac_key = SHA1( salt + "signer" + secret_key )
//!   salt     = "itsdangerous.Signer"  (default, not overridden in auth.py)
//!
//! Timestamp epoch: 2011-01-01 00:00:00 UTC  (EPOCH = 1293840000)
//! Base64 alphabet: URL-safe (`-_`), NO padding (`=` stripped).
//! Signature: HMAC-SHA1 over  b64(payload) + "." + b64(ts_be)

use anyhow::{anyhow, bail, Result};
use base64::{engine::general_purpose::URL_SAFE_NO_PAD, Engine as _};
use hmac::{Hmac, Mac};
use serde::{Deserialize, Serialize};
use sha1::Sha1;
use std::time::{SystemTime, UNIX_EPOCH};

// ──────────────────────────────────────────────────────────────────────────────
// Constants
// ──────────────────────────────────────────────────────────────────────────────

/// itsdangerous EPOCH: 2011-01-01 00:00:00 UTC
const ITS_EPOCH: u64 = 1_293_840_000;

/// Default signer salt used by `URLSafeTimedSerializer` when no salt is passed.
const DEFAULT_SALT: &str = "itsdangerous.Signer";

// ──────────────────────────────────────────────────────────────────────────────
// Public types
// ──────────────────────────────────────────────────────────────────────────────

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct AuthIdentity {
    pub status: String,
    pub source: String,
}

// ──────────────────────────────────────────────────────────────────────────────
// Internal helpers
// ──────────────────────────────────────────────────────────────────────────────

type HmacSha1 = Hmac<Sha1>;

/// Derive the HMAC key using itsdangerous `django-concat` derivation:
///   SHA1( salt + "signer" + secret_key )
fn derive_key(secret_key: &str) -> Vec<u8> {
    use sha1::Digest;
    let mut hasher = sha1::Sha1::new();
    hasher.update(DEFAULT_SALT.as_bytes());
    hasher.update(b"signer");
    hasher.update(secret_key.as_bytes());
    hasher.finalize().to_vec()
}

/// Compute HMAC-SHA1 over `message` using the derived key.
fn hmac_sha1(key: &[u8], message: &[u8]) -> Vec<u8> {
    let mut mac =
        HmacSha1::new_from_slice(key).expect("HMAC-SHA1 accepts any key length");
    mac.update(message);
    mac.finalize().into_bytes().to_vec()
}

/// Encode `seconds_since_unix_epoch` as itsdangerous timestamp:
///   offset = unix_ts - ITS_EPOCH
///   encode as big-endian bytes with leading zero bytes stripped
///   then URL-safe base64 without padding
fn encode_timestamp(unix_ts: u64) -> String {
    let offset = unix_ts.saturating_sub(ITS_EPOCH);
    // big-endian u64, then strip leading zeros (minimum bytes)
    let be = offset.to_be_bytes();
    let stripped: &[u8] = {
        let pos = be.iter().position(|&b| b != 0).unwrap_or(be.len() - 1);
        &be[pos..]
    };
    URL_SAFE_NO_PAD.encode(stripped)
}

/// Decode a URL-safe base64 timestamp back to a Unix timestamp (seconds).
fn decode_timestamp(b64_ts: &str) -> Result<u64> {
    let bytes = URL_SAFE_NO_PAD
        .decode(b64_ts)
        .map_err(|e| anyhow!("bad timestamp base64: {e}"))?;
    if bytes.is_empty() || bytes.len() > 8 {
        bail!("timestamp out of range");
    }
    let mut buf = [0u8; 8];
    buf[8 - bytes.len()..].copy_from_slice(&bytes);
    let offset = u64::from_be_bytes(buf);
    Ok(offset + ITS_EPOCH)
}

/// Current Unix timestamp (seconds).
fn now_unix() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .expect("system clock before 1970")
        .as_secs()
}

// ──────────────────────────────────────────────────────────────────────────────
// Public API
// ──────────────────────────────────────────────────────────────────────────────

/// Sign a payload string using itsdangerous URLSafeTimedSerializer-compatible
/// format. The output can be sent as the `workshop_session` cookie value and
/// validated by a Python itsdangerous consumer.
///
/// Format:  `{b64(payload)}.{b64(ts_be)}.{b64(hmac_sha1)}`
pub fn sign(secret_key: &str, payload: &str) -> Result<String> {
    let b64_payload = URL_SAFE_NO_PAD.encode(payload.as_bytes());
    let b64_ts = encode_timestamp(now_unix());

    let key = derive_key(secret_key);
    let signed_part = format!("{b64_payload}.{b64_ts}");
    let sig = hmac_sha1(&key, signed_part.as_bytes());
    let b64_sig = URL_SAFE_NO_PAD.encode(&sig);

    Ok(format!("{signed_part}.{b64_sig}"))
}

/// Verify + decode a cookie value produced by `sign` (or by Python itsdangerous).
/// Returns the payload string if:
///   - signature is valid (HMAC-SHA1 matches)
///   - timestamp is within `[now - max_age_seconds, now]`
pub fn verify(secret_key: &str, cookie: &str, max_age_seconds: u64) -> Result<String> {
    // Split into exactly 3 dot-separated parts.
    // Note: the payload itself is base64-encoded, so it cannot contain '.'
    // The ts and sig parts are also base64 (no '.'), so 3 parts is unambiguous.
    let parts: Vec<&str> = cookie.splitn(3, '.').collect();
    if parts.len() != 3 {
        bail!("bad cookie format: expected 3 dot-separated parts");
    }
    let (b64_payload, b64_ts, b64_sig) = (parts[0], parts[1], parts[2]);

    // Verify HMAC
    let key = derive_key(secret_key);
    let signed_part = format!("{b64_payload}.{b64_ts}");
    let expected_sig = hmac_sha1(&key, signed_part.as_bytes());
    let provided_sig = URL_SAFE_NO_PAD
        .decode(b64_sig)
        .map_err(|e| anyhow!("bad signature base64: {e}"))?;

    // Constant-time comparison to avoid timing attacks
    if expected_sig.len() != provided_sig.len()
        || expected_sig
            .iter()
            .zip(provided_sig.iter())
            .fold(0u8, |acc, (a, b)| acc | (a ^ b))
            != 0
    {
        bail!("bad signature");
    }

    // Verify timestamp
    let issued_at = decode_timestamp(b64_ts)?;
    let now = now_unix();
    if issued_at > now {
        bail!("signature timestamp is in the future");
    }
    if now - issued_at > max_age_seconds {
        bail!("signature expired");
    }

    // Decode payload
    let payload_bytes = URL_SAFE_NO_PAD
        .decode(b64_payload)
        .map_err(|e| anyhow!("bad payload base64: {e}"))?;
    let payload = String::from_utf8(payload_bytes)
        .map_err(|e| anyhow!("payload is not valid UTF-8: {e}"))?;

    Ok(payload)
}

/// Extract the requesting identity from header / query-param / cookie.
/// Returns `None` → unauthenticated (caller should respond 401).
///
/// Logic mirrors Python `auth.py::require_auth`:
///   1. x-local-key header == secret_key  OR  ?key= == secret_key  → local-key identity
///   2. cookie present  → verify; on success parse payload JSON:
///      - `{"user": {"status": "active", ...}}` → use inner user dict
///      - anything else (plain string, other dict) → generic v2-token identity
///   3. else → None
pub fn extract_identity(
    local_key_header: Option<&str>,
    key_query: Option<&str>,
    cookie_value: Option<&str>,
    secret_key: &str,
    max_age_seconds: u64,
) -> Option<AuthIdentity> {
    // 1. Local-key shortcut
    let is_local_key = local_key_header
        .map(|h| h == secret_key)
        .unwrap_or(false)
        || key_query.map(|q| q == secret_key).unwrap_or(false);

    if is_local_key {
        return Some(AuthIdentity {
            status: "active".to_string(),
            source: "local-key".to_string(),
        });
    }

    // 2. Cookie-based
    let cookie = cookie_value?;
    let payload = verify(secret_key, cookie, max_age_seconds).ok()?;

    // Try to parse as JSON dict {"user": {"status": "active", ...}}
    if let Ok(val) = serde_json::from_str::<serde_json::Value>(&payload) {
        if let Some(user) = val.get("user") {
            if user.get("status").and_then(|s| s.as_str()) == Some("active") {
                return Some(AuthIdentity {
                    status: "active".to_string(),
                    source: "v2-token".to_string(),
                });
            }
            // user exists but status != active → reject
            return None;
        }
    }

    // Plain string payload (or dict without "user" key) → treat as active v2-token
    // (mirrors Python: `if isinstance(data, str): return {"status": "active", ...}`)
    Some(AuthIdentity {
        status: "active".to_string(),
        source: "v2-token".to_string(),
    })
}

// ──────────────────────────────────────────────────────────────────────────────
// Tests
// ──────────────────────────────────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;

    const SECRET: &str = "test-secret-key";
    const MAX_AGE: u64 = 3600;

    // ── Test 1: round-trip ────────────────────────────────────────────────────
    #[test]
    fn test_sign_verify_roundtrip() {
        let payload = "hello";
        let cookie = sign(SECRET, payload).expect("sign should succeed");
        let recovered = verify(SECRET, &cookie, MAX_AGE).expect("verify should succeed");
        assert_eq!(recovered, payload);
    }

    // ── Test 2: wrong secret → Err ────────────────────────────────────────────
    #[test]
    fn test_verify_wrong_secret_fails() {
        let cookie = sign(SECRET, "hello").expect("sign should succeed");
        let result = verify("wrong-secret", &cookie, MAX_AGE);
        assert!(result.is_err(), "wrong secret should return Err");
    }

    // ── Test 3: expired token → Err ───────────────────────────────────────────
    #[test]
    fn test_verify_expired_token_fails() {
        // Build a cookie with a timestamp far in the past (beyond max_age).
        // We craft it manually: timestamp = ITS_EPOCH + 1 (i.e. 2011-01-01 00:00:01 UTC,
        // which is definitely > 3600 seconds ago).
        let b64_payload = URL_SAFE_NO_PAD.encode(b"hello");

        // timestamp = 1 second after itsdangerous epoch → clearly expired
        let ancient_offset: u64 = 1;
        let be = ancient_offset.to_be_bytes();
        let pos = be.iter().position(|&b| b != 0).unwrap_or(be.len() - 1);
        let b64_ts = URL_SAFE_NO_PAD.encode(&be[pos..]);

        let key = derive_key(SECRET);
        let signed_part = format!("{b64_payload}.{b64_ts}");
        let sig = hmac_sha1(&key, signed_part.as_bytes());
        let b64_sig = URL_SAFE_NO_PAD.encode(&sig);
        let cookie = format!("{signed_part}.{b64_sig}");

        let result = verify(SECRET, &cookie, MAX_AGE);
        assert!(result.is_err(), "expired token should return Err");
        let err = result.unwrap_err().to_string();
        assert!(
            err.contains("expired"),
            "error should mention expiry, got: {err}"
        );
    }

    // ── Test 4: extract_identity with correct/wrong local key ─────────────────
    #[test]
    fn test_extract_identity_local_key() {
        // Correct key via header
        let id = extract_identity(Some(SECRET), None, None, SECRET, MAX_AGE);
        assert!(id.is_some());
        let id = id.unwrap();
        assert_eq!(id.source, "local-key");
        assert_eq!(id.status, "active");

        // Wrong key via header → None
        let id_bad = extract_identity(Some("wrong"), None, None, SECRET, MAX_AGE);
        assert!(id_bad.is_none());

        // Correct key via query param
        let id_q = extract_identity(None, Some(SECRET), None, SECRET, MAX_AGE);
        assert!(id_q.is_some());
        assert_eq!(id_q.unwrap().source, "local-key");

        // No key at all → None (no cookie either)
        let id_none = extract_identity(None, None, None, SECRET, MAX_AGE);
        assert!(id_none.is_none());
    }

    // ── Test 5: extract_identity via valid cookie (plain string payload) ──────
    #[test]
    fn test_extract_identity_valid_cookie() {
        let cookie = sign(SECRET, "user-session-id").expect("sign should succeed");
        let id = extract_identity(None, None, Some(&cookie), SECRET, MAX_AGE);
        assert!(id.is_some());
        let id = id.unwrap();
        assert_eq!(id.source, "v2-token");
        assert_eq!(id.status, "active");
    }

    // ── Test 6: extract_identity via valid cookie (JSON dict with active user) ─
    #[test]
    fn test_extract_identity_json_user_cookie() {
        let payload = r#"{"user": {"status": "active", "id": "abc123"}}"#;
        let cookie = sign(SECRET, payload).expect("sign should succeed");
        let id = extract_identity(None, None, Some(&cookie), SECRET, MAX_AGE);
        assert!(id.is_some());
        let id = id.unwrap();
        assert_eq!(id.source, "v2-token");
        assert_eq!(id.status, "active");
    }

    // ── Test 7: extract_identity via valid cookie (JSON dict with inactive user) ─
    #[test]
    fn test_extract_identity_inactive_user_cookie() {
        let payload = r#"{"user": {"status": "suspended", "id": "abc123"}}"#;
        let cookie = sign(SECRET, payload).expect("sign should succeed");
        let id = extract_identity(None, None, Some(&cookie), SECRET, MAX_AGE);
        assert!(id.is_none(), "inactive user should not be authenticated");
    }

    // ── Test 8: encode/decode timestamp round-trip ────────────────────────────
    #[test]
    fn test_timestamp_encode_decode_roundtrip() {
        let unix_ts = 1_748_700_000u64; // a timestamp in 2025
        let b64_ts = encode_timestamp(unix_ts);
        let decoded = decode_timestamp(&b64_ts).expect("decode should succeed");
        assert_eq!(decoded, unix_ts);
    }

    // ── Test 9: cookie with bad signature fails ───────────────────────────────
    #[test]
    fn test_tampered_cookie_fails() {
        let cookie = sign(SECRET, "payload").expect("sign should succeed");
        // Flip a character in the signature (last segment)
        let mut parts: Vec<&str> = cookie.splitn(3, '.').collect();
        let mut bad_sig = parts[2].to_string();
        // Replace last char with 'X' (or 'A' if already 'X')
        if bad_sig.ends_with('X') {
            bad_sig.pop();
            bad_sig.push('A');
        } else {
            bad_sig.pop();
            bad_sig.push('X');
        }
        parts[2] = &bad_sig;
        // Can't easily put back since parts[2] is now a local — rebuild manually
        let tampered = format!("{}.{}.{}", parts[0], parts[1], bad_sig);
        let result = verify(SECRET, &tampered, MAX_AGE);
        assert!(result.is_err(), "tampered signature should fail");
    }
}
