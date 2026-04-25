"""Prometheus metrics for session-channel board v2."""

from __future__ import annotations

from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest

# Counters
LEASE_EXPIRED_TOTAL = Counter(
    "session_channel_lease_expired_total",
    "Total tasks whose lease expired and were reclaimed by reaper",
    ["board_id", "task_class"],
)

CLAIM_CONFLICT_TOTAL = Counter(
    "session_channel_claim_conflict_total",
    "Claim attempts rejected due to caps/assignment/blocked",
    ["board_id", "reason"],  # reason: caps_mismatch | assignment_mismatch | blocked
)

ORPHAN_RECOVERED_TOTAL = Counter(
    "session_channel_orphan_recovered_total",
    "Orphan PEL entries XAUTOCLAIM'd by reaper",
    ["board_id", "task_class"],
)

DEAD_LETTER_TOTAL = Counter(
    "session_channel_dead_letter_total",
    "Tasks promoted to dead-letter stream after exceeding retry threshold",
    ["board_id", "task_class"],
)

# Histograms
PROJECTION_MS = Histogram(
    "session_channel_projection_ms",
    "Time to build board projection (XRANGE + XPENDING)",
    ["board_id"],
    buckets=(1, 5, 10, 25, 50, 100, 250, 500, 1000, 2500),
)

XREAD_LAG_MS = Histogram(
    "session_channel_xread_lag_ms",
    "XREADGROUP latency for claim",
    ["board_id"],
    buckets=(1, 5, 10, 25, 50, 100, 250, 500),
)

HEARTBEAT_LATENCY_MS = Histogram(
    "session_channel_heartbeat_latency_ms",
    "Heartbeat XCLAIM JUSTID latency",
    ["board_id"],
    buckets=(1, 5, 10, 25, 50, 100, 250),
)


def metrics_response_body() -> bytes:
    return generate_latest()


__all__ = [
    "CLAIM_CONFLICT_TOTAL",
    "CONTENT_TYPE_LATEST",
    "DEAD_LETTER_TOTAL",
    "HEARTBEAT_LATENCY_MS",
    "LEASE_EXPIRED_TOTAL",
    "ORPHAN_RECOVERED_TOTAL",
    "PROJECTION_MS",
    "XREAD_LAG_MS",
    "metrics_response_body",
]
