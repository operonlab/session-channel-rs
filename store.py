"""Session Channel Station — NgRx-style FeatureStore (Redis Streams topic state).

Reducer depth: tracks topics, subscriber count, and message throughput
for the cross-session communication bus backed by Redis Streams.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "core"))

from src.shared.actions import create_action, create_reducer, on
from src.shared.immutable_utils import update_in
from src.shared.store import FeatureStore

# ── Actions ──────────────────────────────────────────────────────────────

TopicCreated = create_action("channel.topic.created")
MessagePublished = create_action("channel.message.published")
SubscriberJoined = create_action("channel.subscriber.joined")
SubscriberLeft = create_action("channel.subscriber.left")
StreamTrimmed = create_action("channel.stream.trimmed")

# ── Reducer ──────────────────────────────────────────────────────────────

_INITIAL_STATE = {
    "topics": {},
    "subscriber_count": 0,
    "message_count": 0,
}

channel_reducer = create_reducer(
    _INITIAL_STATE,
    on(
        TopicCreated,
        lambda s, a: update_in(
            s,
            ["topics", a.payload.get("topic", "")],
            lambda _: {"created_at": a.payload.get("ts"), "message_count": 0},
        ),
    ),
    on(
        MessagePublished,
        lambda s, a: s.set("message_count", s["message_count"] + 1),
    ),
    on(
        SubscriberJoined,
        lambda s, a: s.set("subscriber_count", s["subscriber_count"] + 1),
    ),
    on(
        SubscriberLeft,
        lambda s, a: s.set("subscriber_count", max(0, s["subscriber_count"] - 1)),
    ),
    on(
        StreamTrimmed,
        lambda s, a: update_in(
            s,
            ["topics", a.payload.get("topic", ""), "trimmed_count"],
            lambda prev: (prev or 0) + a.payload.get("trimmed", 0),
        ),
    ),
)

# ── Store Singleton ───────────────────────────────────────────────────────

channel_store: FeatureStore = FeatureStore("session-channel", channel_reducer)
