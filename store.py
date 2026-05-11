"""Session Channel Station — NgRx-style FeatureStore (Redis Streams topic state).

Reducer depth: tracks topics, subscriber count, and message throughput
for the cross-session communication bus backed by Redis Streams.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "core"))

import logging

from src.shared.actions import create_action, create_reducer, on
from src.shared.immutable_utils import to_dict, update_in
from src.shared.middleware import LoggerMiddleware
from src.shared.selectors import create_selector
from src.shared.store import FeatureStore, effect, register_effects

logger = logging.getLogger(__name__)

# ── Actions ──────────────────────────────────────────────────────────────

TopicCreated = create_action("channel.topic.created")
MessagePublished = create_action("channel.message.published")
SubscriberJoined = create_action("channel.subscriber.joined")
SubscriberLeft = create_action("channel.subscriber.left")
StreamTrimmed = create_action("channel.stream.trimmed")
AgentSnapshotReceived = create_action("channel.agent.snapshot")
AgentLeft = create_action("channel.agent.left")

# ── Reducer ──────────────────────────────────────────────────────────────

_INITIAL_STATE = {
    "topics": {},
    "subscriber_count": 0,
    "message_count": 0,
    "agents": {},  # key="{host}:{pane}" → snapshot dict
}


def _p(action):
    """Safe payload accessor — returns empty dict if payload is None."""
    return action.payload or {}


channel_reducer = create_reducer(
    _INITIAL_STATE,
    on(
        TopicCreated,
        lambda s, a: update_in(
            s,
            ["topics", _p(a).get("topic", "")],
            lambda _: {"created_at": _p(a).get("ts"), "message_count": 0},
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
            ["topics", _p(a).get("topic", ""), "trimmed_count"],
            lambda prev: (prev or 0) + _p(a).get("trimmed", 0),
        ),
    ),
    on(
        AgentSnapshotReceived,
        lambda s, a: update_in(
            s,
            ["agents", _p(a).get("key", "")],
            lambda _: _p(a),
        ),
    ),
    on(
        AgentLeft,
        lambda s, a: update_in(
            s,
            ["agents"],
            lambda agents: {
                k: v for k, v in (to_dict(agents) or {}).items() if k != _p(a).get("key", "")
            },
        ),
    ),
)

# ── Selectors ─────────────────────────────────────────────────────────────

select_topics = create_selector(lambda s: s["topics"])
select_subscriber_count = create_selector(lambda s: s["subscriber_count"])
select_message_count = create_selector(lambda s: s["message_count"])

select_topic_names = create_selector(
    select_topics,
    result_fn=lambda topics: list(topics.keys()),
)

select_topic_count = create_selector(
    select_topics,
    result_fn=lambda topics: len(topics),
)

select_topic_by_name = lambda name: create_selector(  # noqa: E731
    select_topics,
    result_fn=lambda topics: topics.get(name),
)

select_active_topics = create_selector(
    select_topics,
    result_fn=lambda topics: {k: v for k, v in topics.items() if v is not None},
)

select_channel_summary = create_selector(
    select_topics,
    select_subscriber_count,
    select_message_count,
    result_fn=lambda topics, subs, msgs: {
        "topic_count": len(topics),
        "subscriber_count": subs,
        "message_count": msgs,
    },
)

# ── Agent selectors ──────────────────────────────────────────────────────

select_agents = create_selector(lambda s: s.get("agents", {}))


def select_active_agents(within_seconds: int = 300):
    """Factory returning a selector for agents seen within the window."""
    cutoff = time.time() - within_seconds
    return create_selector(
        select_agents,
        result_fn=lambda agents: {
            k: to_dict(v)
            for k, v in (to_dict(agents) or {}).items()
            if (v or {}).get("last_seen", 0) >= cutoff
        },
    )


def _agent_sort_key(agent: dict) -> tuple:
    meta = (agent.get("_meta") or {}) if isinstance(agent, dict) else {}
    role_rank = 0 if meta.get("role") == "main" else 1
    try:
        ctx = float(meta.get("ctx_pct") or 0)
    except (TypeError, ValueError):
        ctx = 0.0
    return (role_rank, -ctx, -(agent.get("ts_ms", 0) if isinstance(agent, dict) else 0))


select_agents_sorted = create_selector(
    select_agents,
    result_fn=lambda agents: sorted(
        (to_dict(v) for v in (to_dict(agents) or {}).values()),
        key=_agent_sort_key,
    ),
)

# ── Store Singleton ───────────────────────────────────────────────────────

channel_store: FeatureStore = FeatureStore(
    "session-channel",
    channel_reducer,
    middlewares=[LoggerMiddleware("session-channel")],
)

# ── Effects ──────────────────────────────────────────────────────────────────


@effect(MessagePublished, store=channel_store)
async def log_message_published(action, store) -> None:
    """Log message published for throughput tracking."""
    payload = action.payload or {}
    logger.info(
        "channel.message.published",
        extra={"topic": payload.get("topic")},
    )


@effect(StreamTrimmed, store=channel_store)
async def log_stream_trimmed(action, store) -> None:
    """Log stream trim operation."""
    payload = action.payload or {}
    logger.info(
        "channel.stream.trimmed",
        extra={"topic": payload.get("topic"), "trimmed": payload.get("trimmed", 0)},
    )


register_effects(channel_store, log_message_published, log_stream_trimmed)
