"""Chaos tests — pane SIGKILL, retry exhaustion → dead-letter."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.asyncio


async def test_pane_kill_releases_pending_via_lease(fake_redis, board_id):
    """pane claim 後不 ack（模擬 SIGKILL）→ reaper 撿走。"""
    from board_routes import _ensure_group, _group_key, _stream_key

    sk = _stream_key(board_id)
    gk = _group_key(board_id)
    await _ensure_group(fake_redis, board_id)

    fields = {
        "tag": "publish",
        "id": "t1",
        "desc": "killed work",
        "task_class": "short",
        "required_caps": "[]",
        "assigned_to": "",
        "depends_on": "[]",
        "priority": "normal",
        "sender": "pane:src",
    }
    await fake_redis.xadd(sk, fields)

    # pane:victim claims
    res = await fake_redis.xreadgroup(gk, "pane:victim", {sk: ">"}, count=1, block=10)
    assert res and res[0][1]
    msg_id = res[0][1][0][0]

    # No ack, no heartbeat → simulate SIGKILL.
    # reaper: XAUTOCLAIM min_idle=0 (i.e. lease "expired" instantly)
    try:
        _next, claimed, _deleted = await fake_redis.xautoclaim(
            sk, gk, "__reaper", min_idle_time=0, start_id="0-0", count=10
        )
    except Exception as e:
        pytest.skip(f"fakeredis xautoclaim not supported: {e}")

    claimed_ids = [c[0] for c in claimed]
    assert msg_id in claimed_ids, "reaper must rescue the orphaned claim"


async def test_retry_count_promotes_to_dead_letter(fake_redis, board_id):
    """retry_count > 3 → 出現在 board:{id}:failed stream。"""
    from board_routes import _ensure_group, _group_key, _stream_key

    from config import config

    sk = _stream_key(board_id)
    gk = _group_key(board_id)
    dl_key = f"{config.stream_prefix}board:{board_id}:failed"
    await _ensure_group(fake_redis, board_id)

    # Simulate a publish entry that's already been retried 3 times — next reaper
    # pass would push retry_count to 4, which exceeds the threshold.
    fields = {
        "tag": "publish",
        "id": "t-doomed",
        "desc": "always fails",
        "task_class": "short",
        "required_caps": "[]",
        "assigned_to": "",
        "depends_on": "[]",
        "priority": "normal",
        "sender": "pane:src",
        "retry_count": "3",
    }
    msg_id = await fake_redis.xadd(sk, fields)

    # pane crashes immediately — claim then never ack
    await fake_redis.xreadgroup(gk, "pane:victim", {sk: ">"}, count=1, block=10)

    # Reaper logic (paraphrased from main.py::_reaper_loop):
    try:
        _next, claimed, _deleted = await fake_redis.xautoclaim(
            sk, gk, "__reaper", min_idle_time=0, start_id="0-0", count=10
        )
    except Exception as e:
        pytest.skip(f"fakeredis xautoclaim not supported: {e}")

    for cm_id, cm_fields in claimed:
        rc = int(cm_fields.get("retry_count", "0") or "0") + 1
        new_fields = {**cm_fields, "retry_count": str(rc)}
        if rc > 3:
            await fake_redis.xadd(dl_key, new_fields)
            await fake_redis.xack(sk, gk, cm_id)

    # Dead-letter stream should now contain it
    dl_msgs = await fake_redis.xrange(dl_key)
    assert dl_msgs, "doomed task must land in :failed stream"
    assert any(m[1].get("id") == "t-doomed" for m in dl_msgs)
    # Original PEL drained
    pending = await fake_redis.xpending_range(sk, gk, "-", "+", 100)
    pending_ids = [(e.get("message_id") if isinstance(e, dict) else e[0]) for e in pending]
    assert msg_id not in pending_ids
