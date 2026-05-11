"""Concurrency tests — lease expiry, heartbeat, three-class buckets."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.asyncio


async def test_lease_expiry_xautoclaim_releases(fake_redis, board_id):
    """Idle > lease → XAUTOCLAIM 應拿到 idle pending → 重發。

    fakeredis 不支援 freezegun 改 idle_ms 行為，但支援 min_idle_time=0
    強制取走 PEL — 我們以 idle_ms threshold=0 模擬 lease 過期。
    """
    from board_routes import _ensure_group, _group_key, _stream_key

    sk = _stream_key(board_id)
    gk = _group_key(board_id)
    await _ensure_group(fake_redis, board_id)

    fields = {
        "tag": "publish",
        "id": "t1",
        "desc": "x",
        "task_class": "short",
        "required_caps": "[]",
        "assigned_to": "",
        "depends_on": "[]",
        "priority": "normal",
        "sender": "pane:a",
    }
    await fake_redis.xadd(sk, fields)

    # pane:a claims but never acks (simulating crash)
    res = await fake_redis.xreadgroup(gk, "pane:a", {sk: ">"}, count=1, block=10)
    assert res and res[0][1]

    # XAUTOCLAIM with min_idle_time=0 → reaper grabs it
    try:
        next_cursor, claimed, _deleted = await fake_redis.xautoclaim(
            sk, gk, "__reaper", min_idle_time=0, start_id="0-0", count=10
        )
    except Exception as e:
        pytest.skip(f"fakeredis xautoclaim not supported: {e}")

    assert claimed, "reaper must reclaim idle pending entry"


async def test_heartbeat_resets_idle(fake_redis, board_id):
    """XCLAIM JUSTID 後 XPENDING idle_ms 重置接近 0。"""
    from board_routes import _ensure_group, _group_key, _stream_key

    sk = _stream_key(board_id)
    gk = _group_key(board_id)
    await _ensure_group(fake_redis, board_id)

    fields = {
        "tag": "publish",
        "id": "t1",
        "desc": "x",
        "task_class": "short",
        "required_caps": "[]",
        "assigned_to": "",
        "depends_on": "[]",
        "priority": "normal",
        "sender": "pane:a",
    }
    await fake_redis.xadd(sk, fields)

    res = await fake_redis.xreadgroup(gk, "pane:a", {sk: ">"}, count=1, block=10)
    assert res and res[0][1]
    msg_id = res[0][1][0][0]

    # heartbeat — XCLAIM JUSTID with min_idle_time=0
    try:
        out = await fake_redis.xclaim(
            sk, gk, "pane:a", min_idle_time=0, message_ids=[msg_id], justid=True
        )
    except Exception as e:
        pytest.skip(f"fakeredis xclaim JUSTID not supported: {e}")

    assert out, "JUSTID should return the refreshed id"

    # idle_ms must be small (just refreshed)
    pending = await fake_redis.xpending_range(sk, gk, "-", "+", 10)
    assert pending
    e = pending[0]
    idle = e.get("time_since_delivered") if isinstance(e, dict) else e[2]
    assert int(idle or 0) < 500, f"idle should be ~0 right after heartbeat, got {idle}"


async def test_three_class_buckets_separate_lease(fake_redis, board_id):
    """short=30s / llm=300s / video=1800s — lease 配置正確。"""
    from schemas import LEASE_CONFIG, TaskClass, lease_ms_for_class

    assert LEASE_CONFIG[TaskClass.SHORT]["lease_seconds"] == 30
    assert LEASE_CONFIG[TaskClass.LLM]["lease_seconds"] == 300
    assert LEASE_CONFIG[TaskClass.VIDEO]["lease_seconds"] == 1800

    assert lease_ms_for_class(TaskClass.SHORT) == 30_000
    assert lease_ms_for_class(TaskClass.LLM) == 300_000
    assert lease_ms_for_class(TaskClass.VIDEO) == 1_800_000

    # Reaper-loop semantics: after 30s of idle, only SHORT crosses its threshold.
    idle_ms = 30_000
    crossed = {tc.value for tc in TaskClass if idle_ms >= lease_ms_for_class(tc)}
    assert crossed == {"short"}, f"only SHORT should be due, got {crossed}"
