"""Stress tests — 50 panes × 1k tasks. Marked @slow so CI can opt out."""

from __future__ import annotations

import asyncio
import time

import pytest

pytestmark = [pytest.mark.asyncio, pytest.mark.slow]


@pytest.mark.slow
async def test_50_panes_1k_tasks_p99(fake_redis, board_id):
    """50 個 pane 並發 claim 1000 個 task，量 P50/P95/P99 時間。

    fakeredis 下 P99 會非常低（無網路），主要驗證 NO deadlock 且
    exactly-once 不破。真 redis 才有意義。
    """
    from board_routes import _ensure_group, _group_key, _stream_key

    sk = _stream_key(board_id)
    gk = _group_key(board_id)
    await _ensure_group(fake_redis, board_id)

    # publish 1000 tasks
    publish_fields_template = {
        "tag": "publish",
        "desc": "stress",
        "task_class": "short",
        "required_caps": "[]",
        "assigned_to": "",
        "depends_on": "[]",
        "priority": "normal",
        "sender": "pane:src",
    }
    for i in range(1000):
        await fake_redis.xadd(sk, {**publish_fields_template, "id": f"t-{i}"})

    latencies: list[float] = []
    claimed_ids: list[str] = []
    lock = asyncio.Lock()

    async def worker(name: str):
        while True:
            t0 = time.perf_counter()
            res = await fake_redis.xreadgroup(gk, name, {sk: ">"}, count=1, block=10)
            dt = time.perf_counter() - t0
            if not res or not res[0][1]:
                return
            msg_id = res[0][1][0][0]
            async with lock:
                latencies.append(dt)
                claimed_ids.append(msg_id)

    panes = [worker(f"pane-{i}") for i in range(50)]
    await asyncio.gather(*panes)

    # exactly-once
    assert len(claimed_ids) == len(set(claimed_ids)), "duplicate delivery detected"
    assert len(claimed_ids) == 1000, f"expected 1000 claims, got {len(claimed_ids)}"

    # P-stats (informational; sanity bound only)
    latencies.sort()

    def pct(p: float) -> float:
        idx = max(0, min(len(latencies) - 1, int(len(latencies) * p) - 1))
        return latencies[idx]

    p50, p95, p99 = pct(0.50), pct(0.95), pct(0.99)
    print(f"\n[stress] P50={p50 * 1000:.2f}ms P95={p95 * 1000:.2f}ms P99={p99 * 1000:.2f}ms")
    # Sanity: even on a slow CI box, fakeredis P99 stays well under 200ms.
    assert p99 < 0.2, f"P99 latency {p99 * 1000:.2f}ms exceeds 200ms — possible deadlock"
