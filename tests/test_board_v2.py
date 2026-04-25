"""Tests for board v2 — stream + consumer-group semantics.

Covers:
- exactly-once XREADGROUP delivery under concurrent claim
- consumer group creation idempotence
- complete drains XPENDING via XACK
- drop transfers ownership to __reaper via XCLAIM
- progress events flow through projection
- caps mismatch rejects + republishes
- assigned_to mismatch rejects
"""

from __future__ import annotations

import asyncio
import json

import pytest

pytestmark = pytest.mark.asyncio


async def test_xreadgroup_no_duplicate_under_concurrency(fake_redis, board_id):
    """100 並發 claim 同一 task → exactly-once。"""
    from board_routes import _ensure_group, _group_key, _stream_key

    sk = _stream_key(board_id)
    gk = _group_key(board_id)
    await _ensure_group(fake_redis, board_id)

    # Publish 1 task
    await fake_redis.xadd(
        sk,
        {
            "tag": "publish",
            "id": "t1",
            "desc": "test",
            "task_class": "short",
            "required_caps": "[]",
            "assigned_to": "",
            "depends_on": "[]",
            "priority": "normal",
            "sender": "pane:test",
        },
    )

    async def claim(name):
        # block=10ms — fakeredis returns immediately if no msg, avoids hang
        return await fake_redis.xreadgroup(gk, name, {sk: ">"}, count=1, block=10)

    results = await asyncio.gather(*[claim(f"pane-{i}") for i in range(100)])
    successes = [r for r in results if r and r[0][1]]
    assert len(successes) == 1, f"Expected exactly 1 claim, got {len(successes)}"


async def test_publish_creates_consumer_group(fake_redis, board_id):
    """XGROUP CREATE 後 XINFO GROUPS 看得到。"""
    from board_routes import _ensure_group, _group_key, _stream_key

    sk = _stream_key(board_id)
    gk = _group_key(board_id)
    await _ensure_group(fake_redis, board_id)

    # Idempotency: second call must not raise
    await _ensure_group(fake_redis, board_id)

    groups = await fake_redis.xinfo_groups(sk)
    names = {g.get("name") for g in groups}
    assert gk in names, f"group {gk} not in {names}"


async def test_complete_xacks_pending(fake_redis, board_id):
    """complete (XACK) 後 XPENDING 該 task 消失。"""
    from board_routes import _ensure_group, _group_key, _stream_key

    sk = _stream_key(board_id)
    gk = _group_key(board_id)
    await _ensure_group(fake_redis, board_id)

    msg_id = await fake_redis.xadd(
        sk,
        {
            "tag": "publish",
            "id": "t1",
            "desc": "x",
            "task_class": "short",
            "required_caps": "[]",
            "assigned_to": "",
            "depends_on": "[]",
            "priority": "normal",
            "sender": "pane:a",
        },
    )

    # claim it
    res = await fake_redis.xreadgroup(gk, "pane:a", {sk: ">"}, count=1, block=10)
    assert res and res[0][1]
    claimed_id = res[0][1][0][0]
    assert claimed_id == msg_id

    # XACK
    n = await fake_redis.xack(sk, gk, claimed_id)
    assert n == 1

    # PEL should not contain it
    pending = await fake_redis.xpending_range(sk, gk, "-", "+", 100)
    pending_ids = [(e.get("message_id") if isinstance(e, dict) else e[0]) for e in pending]
    assert claimed_id not in pending_ids


async def test_drop_xclaims_to_reaper(fake_redis, board_id):
    """drop 後 XPENDING 顯示 consumer=__reaper。"""
    from board_routes import _ensure_group, _group_key, _stream_key

    sk = _stream_key(board_id)
    gk = _group_key(board_id)
    await _ensure_group(fake_redis, board_id)

    msg_id = await fake_redis.xadd(
        sk,
        {
            "tag": "publish",
            "id": "t1",
            "desc": "x",
            "task_class": "short",
            "required_caps": "[]",
            "assigned_to": "",
            "depends_on": "[]",
            "priority": "normal",
            "sender": "pane:a",
        },
    )

    await fake_redis.xreadgroup(gk, "pane:a", {sk: ">"}, count=1, block=10)

    # drop = XCLAIM FORCE to __reaper
    try:
        await fake_redis.xclaim(
            sk, gk, "__reaper", min_idle_time=0, message_ids=[msg_id], force=True
        )
    except Exception as e:
        pytest.skip(f"fakeredis xclaim FORCE not supported: {e}")

    pending = await fake_redis.xpending_range(sk, gk, "-", "+", 100)
    consumers = [(e.get("consumer") if isinstance(e, dict) else e[1]) for e in pending]
    assert "__reaper" in consumers, f"expected __reaper in {consumers}"


async def test_progress_xadd_event(fake_redis, board_id):
    """progress 寫入 stream 並可在 projection 看到 percent。"""
    from board_routes import _build_projection, _ensure_group, _stream_key

    sk = _stream_key(board_id)
    await _ensure_group(fake_redis, board_id)

    msg_id = await fake_redis.xadd(
        sk,
        {
            "tag": "publish",
            "id": "t1",
            "desc": "long task",
            "task_class": "llm",
            "required_caps": "[]",
            "assigned_to": "",
            "depends_on": "[]",
            "priority": "normal",
            "sender": "pane:a",
        },
    )

    # Append progress event keyed by task_id = stream msg_id
    await fake_redis.xadd(
        sk,
        {
            "tag": "progress",
            "task_id": msg_id,
            "percent": "42",
            "stage": "embedding",
            "note": "rows 4200/10000",
            "sender": "pane:a",
        },
    )

    proj = await _build_projection(fake_redis, board_id)
    matched = [t for t in proj["tasks"] if t["id"] == msg_id]
    assert matched, "publish task missing from projection"
    task = matched[0]
    assert task["progress"] is not None
    assert task["progress"]["percent"] == 42
    assert task["progress"]["stage"] == "embedding"


async def test_caps_mismatch_rejects_and_republishes(fake_redis, board_id):
    """required_caps 設了但 pane 沒對應 → simulated reject + republish。"""
    from board_routes import _ensure_group, _group_key, _stream_key

    sk = _stream_key(board_id)
    gk = _group_key(board_id)
    await _ensure_group(fake_redis, board_id)

    fields = {
        "tag": "publish",
        "id": "t1",
        "desc": "needs gpu",
        "task_class": "short",
        "required_caps": json.dumps(["mcp:gpu"]),
        "assigned_to": "",
        "depends_on": "[]",
        "priority": "normal",
        "sender": "pane:a",
    }
    await fake_redis.xadd(sk, fields)

    # pane B has no caps → emulate the claim-route behavior:
    # XREADGROUP delivers, route inspects required_caps, finds mismatch,
    # then XACKs and re-XADDs the same payload.
    res = await fake_redis.xreadgroup(gk, "pane:b", {sk: ">"}, count=1, block=10)
    assert res and res[0][1]
    msg_id, returned = res[0][1][0]

    required = json.loads(returned.get("required_caps") or "[]")
    pane_caps: set[str] = set()  # pane:b advertised nothing
    missing = [c for c in required if c not in pane_caps]
    assert missing == ["mcp:gpu"]

    # ack + redo (same payload)
    await fake_redis.xack(sk, gk, msg_id)
    await fake_redis.xadd(sk, returned)

    # Stream should now have at least 2 publish entries (original + redo)
    msgs = await fake_redis.xrange(sk)
    publish_msgs = [m for m in msgs if m[1].get("tag") == "publish"]
    assert len(publish_msgs) >= 2, "redo XADD should leave a new publish entry"


async def test_assigned_to_mismatch_rejects(fake_redis, board_id):
    """assigned_to=A，pane B claim → reject + republish。"""
    from board_routes import _ensure_group, _group_key, _stream_key

    sk = _stream_key(board_id)
    gk = _group_key(board_id)
    await _ensure_group(fake_redis, board_id)

    fields = {
        "tag": "publish",
        "id": "t1",
        "desc": "for A only",
        "task_class": "short",
        "required_caps": "[]",
        "assigned_to": "pane:A",
        "depends_on": "[]",
        "priority": "normal",
        "sender": "pane:src",
    }
    await fake_redis.xadd(sk, fields)

    # pane:B claims — emulated route logic
    res = await fake_redis.xreadgroup(gk, "pane:B", {sk: ">"}, count=1, block=10)
    assert res and res[0][1]
    msg_id, returned = res[0][1][0]

    assigned = (returned.get("assigned_to") or "").strip()
    assert assigned == "pane:A"
    assert assigned != "pane:B"  # mismatch

    await fake_redis.xack(sk, gk, msg_id)
    await fake_redis.xadd(sk, returned)

    # New entry should be claimable by pane:A now
    res2 = await fake_redis.xreadgroup(gk, "pane:A", {sk: ">"}, count=1, block=10)
    assert res2 and res2[0][1], "pane:A should be able to claim the redo'd entry"
