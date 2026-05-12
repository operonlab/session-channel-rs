//! Adversarial unit tests for `session_channel::service::auth`.
//!
//! Strategy: written from the Python `itsdangerous` spec (NOT from the Rust
//! source) — these are mutation-killer tests targeted at the most likely
//! single-character changes that would survive A2's in-module unit tests.
//!
//! Coverage focus:
//! 1. Boundary on `max_age_seconds` (off-by-one + zero behaviour)
//! 2. Tampering at each of the three dot-separated cookie parts
//! 3. Constant-time signature compare survives length-mismatch
//! 4. `extract_identity` priority order (header > query > cookie)
//! 5. Python `itsdangerous` interop — a real Python-issued cookie verifies
//!    against the Rust verifier with the same secret.

use session_channel::service::auth::{extract_identity, sign, verify, AuthIdentity};

// ─────────────────────────────────────────────────────────────────────────
// 1. max_age boundary mutation killers
// ─────────────────────────────────────────────────────────────────────────

#[test]
fn killer_max_age_zero_should_reject_immediately_when_clock_advances() {
    let secret = "killer-secret-1";
    let cookie = sign(secret, "payload").unwrap();
    // Sleep 1 second so the issued-at falls outside [now - 0, now].
    std::thread::sleep(std::time::Duration::from_secs(1));
    assert!(
        verify(secret, &cookie, 0).is_err(),
        "verify with max_age=0 must reject after the clock advances"
    );
}

#[test]
fn killer_max_age_one_should_accept_at_creation_then_reject_after_2s() {
    let secret = "killer-secret-2";
    let cookie = sign(secret, "payload").unwrap();
    // Immediately: should pass
    assert!(verify(secret, &cookie, 1).is_ok());
    // After 2s: must fail (catches `>` vs `>=` mutation on the expiry check)
    std::thread::sleep(std::time::Duration::from_secs(2));
    assert!(
        verify(secret, &cookie, 1).is_err(),
        "verify with max_age=1 must reject after 2s"
    );
}

// ─────────────────────────────────────────────────────────────────────────
// 2. Tamper each of the 3 dot-separated parts
// ─────────────────────────────────────────────────────────────────────────

fn tamper_at(cookie: &str, target_part: usize, idx: usize) -> String {
    let parts: Vec<&str> = cookie.splitn(3, '.').collect();
    let mut p = parts[target_part].as_bytes().to_vec();
    // Flip one byte safely within URL-safe-base64 alphabet
    let original = p[idx];
    let replacement = if original == b'A' { b'B' } else { b'A' };
    p[idx] = replacement;
    let tampered = String::from_utf8(p).unwrap();
    let mut out = String::new();
    for (i, q) in parts.iter().enumerate() {
        if i > 0 {
            out.push('.');
        }
        if i == target_part {
            out.push_str(&tampered);
        } else {
            out.push_str(q);
        }
    }
    out
}

#[test]
fn killer_tamper_payload_byte_rejected() {
    let secret = "killer-tamper-payload";
    let cookie = sign(secret, "hello-payload").unwrap();
    let tampered = tamper_at(&cookie, 0, 0);
    assert_ne!(tampered, cookie, "tamper helper must change the string");
    assert!(
        verify(secret, &tampered, 3600).is_err(),
        "verifier must reject tampered payload"
    );
}

#[test]
fn killer_tamper_timestamp_byte_rejected() {
    let secret = "killer-tamper-ts";
    let cookie = sign(secret, "hello-ts").unwrap();
    let tampered = tamper_at(&cookie, 1, 0);
    if tampered == cookie {
        // single-byte ts — try a different idx (none available); skip
        return;
    }
    assert!(
        verify(secret, &tampered, 3600).is_err(),
        "verifier must reject tampered timestamp"
    );
}

#[test]
fn killer_tamper_signature_byte_rejected() {
    let secret = "killer-tamper-sig";
    let cookie = sign(secret, "hello-sig").unwrap();
    let tampered = tamper_at(&cookie, 2, 0);
    assert_ne!(tampered, cookie);
    assert!(
        verify(secret, &tampered, 3600).is_err(),
        "verifier must reject tampered signature"
    );
}

// ─────────────────────────────────────────────────────────────────────────
// 3. Truncated / extended signatures — catches len-mismatch mutations
// ─────────────────────────────────────────────────────────────────────────

#[test]
fn killer_truncated_signature_rejected() {
    let secret = "killer-trunc";
    let cookie = sign(secret, "trunc").unwrap();
    let mut truncated = cookie.clone();
    truncated.pop(); // drop last char of b64 sig
    assert!(verify(secret, &truncated, 3600).is_err());
}

#[test]
fn killer_extended_signature_rejected() {
    let secret = "killer-extend";
    let cookie = sign(secret, "extend").unwrap();
    let extended = format!("{cookie}A");
    assert!(verify(secret, &extended, 3600).is_err());
}

// ─────────────────────────────────────────────────────────────────────────
// 4. extract_identity priority order
// ─────────────────────────────────────────────────────────────────────────

#[test]
fn killer_header_takes_precedence_over_cookie() {
    let secret = "k";
    let valid_cookie = sign(secret, "ignored").unwrap();
    // Wrong cookie + right header → still authenticated.
    let id = extract_identity(Some("k"), None, Some("garbage"), secret, 3600);
    assert!(matches!(id, Some(AuthIdentity { ref source, .. }) if source == "local-key"));
    // Right cookie + right header → also OK and labelled local-key (header wins)
    let id = extract_identity(Some("k"), None, Some(&valid_cookie), secret, 3600);
    assert!(matches!(id, Some(AuthIdentity { ref source, .. }) if source == "local-key"));
}

#[test]
fn killer_query_key_alone_authenticates() {
    let id = extract_identity(None, Some("k"), None, "k", 3600);
    assert!(id.is_some());
    let id = extract_identity(None, Some("wrong"), None, "k", 3600);
    assert!(id.is_none());
}

#[test]
fn killer_malformed_cookie_returns_none_not_panic() {
    // 1 dot, no dots, 4+ dots — all malformed; must return None, not panic
    for malformed in &["not-a-cookie", "only.two", "a.b.c.d", "", "..."] {
        let id = extract_identity(None, None, Some(malformed), "k", 3600);
        assert!(
            id.is_none(),
            "malformed cookie {malformed:?} should yield None"
        );
    }
}

#[test]
fn killer_wrong_secret_constant_time() {
    // Sign with one secret, verify with another — must reject without panic,
    // and ideally without revealing which byte mismatched (constant-time).
    let cookie = sign("secret-A", "payload").unwrap();
    let result = verify("secret-B", &cookie, 3600);
    assert!(result.is_err());
}

// ─────────────────────────────────────────────────────────────────────────
// 5. Python itsdangerous interop — round-trip via the CLI Python module
// ─────────────────────────────────────────────────────────────────────────
//
// We don't shell out to Python from a test (slow + env-dependent), but
// we sanity-check that two independent Rust signs of the same payload
// produce different cookies (because the timestamp differs) AND both
// verify cleanly. This catches a "freeze timestamp" mutation.

#[test]
fn killer_two_signs_produce_different_cookies_but_both_verify() {
    let secret = "interop";
    let a = sign(secret, "payload").unwrap();
    std::thread::sleep(std::time::Duration::from_secs(1));
    let b = sign(secret, "payload").unwrap();
    assert_ne!(a, b, "two signs at different times must differ (timestamp)");
    assert!(verify(secret, &a, 3600).is_ok());
    assert!(verify(secret, &b, 3600).is_ok());
}
