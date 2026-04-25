"""Migrate v1 board state to v2 (Streams consumer group + XCLAIM).

v1 schema:
  - Stream:        ws:channel:board:{board_id}
  - Claims hash:   ws:board:claims:{board_id}  (task_id → JSON{pane, lease_until, ...})
  - Self-written Lua CAS for claim/drop

v2 schema:
  - Stream:        ws:channel:board:{board_id}                (unchanged)
  - Consumer group: board-{board_id}                          (new)
  - Pending entries: managed by Redis (XPENDING / XAUTOCLAIM)
  - No claims hash; lease = idle time

This script:
  1. Scans `ws:board:claims:*` hashes
  2. For each board:
     a. XGROUP CREATE board-{id} (idempotent, MKSTREAM)
     b. For each (task_id, pane) in claims hash → XCLAIM that entry to consumer=pane
        so the v2 reaper sees it as a pending entry owned by the original pane.
     c. Leaves `ws:board:claims:{id}` intact (not auto-deleted) for manual verify.

Usage:
    ~/.local/bin/python3 stations/session-channel/scripts/migrate_v1_to_v2.py
    # then manually verify with:
    #   redis-cli XPENDING ws:channel:board:<id> board-<id>
    # finally clean up:
    #   redis-cli DEL ws:board:claims:<id>

Idempotent: re-runs are safe; XGROUP CREATE swallows BUSYGROUP, XCLAIM force=True.
"""

from __future__ import annotations

import asyncio
import json
import sys

import redis.asyncio as aioredis

REDIS_URL = "redis://127.0.0.1:6379/0"
CLAIM_KEY_PREFIX = "ws:board:claims:"
STREAM_KEY_PREFIX = "ws:channel:board:"


async def _scan_claim_keys(r: aioredis.Redis) -> list[str]:
    keys: list[str] = []
    cursor = 0
    while True:
        cursor, batch = await r.scan(cursor=cursor, match=f"{CLAIM_KEY_PREFIX}*", count=200)
        keys.extend(batch)
        if cursor == 0:
            break
    return keys


async def _ensure_group(r: aioredis.Redis, stream_key: str, group_name: str) -> None:
    try:
        await r.xgroup_create(name=stream_key, groupname=group_name, id="0", mkstream=True)
    except Exception as e:
        if "BUSYGROUP" not in str(e):
            raise


async def _migrate_board(r: aioredis.Redis, claim_key: str) -> tuple[int, int]:
    """Migrate a single board. Returns (claims_total, claims_migrated)."""
    board_id = claim_key[len(CLAIM_KEY_PREFIX) :]
    stream_key = f"{STREAM_KEY_PREFIX}{board_id}"
    group_name = f"board-{board_id}"

    await _ensure_group(r, stream_key, group_name)

    claims = await r.hgetall(claim_key)
    migrated = 0
    for task_id, raw in claims.items():
        try:
            data = json.loads(raw) if isinstance(raw, str) else {}
        except Exception:
            data = {}
        pane = (data.get("pane") or "").strip()
        if not pane:
            print(f"  [skip] {board_id}/{task_id}: no pane in claim payload")
            continue
        try:
            await r.xclaim(
                name=stream_key,
                groupname=group_name,
                consumername=pane,
                min_idle_time=0,
                message_ids=[task_id],
                force=True,
            )
            migrated += 1
        except Exception as e:
            print(f"  [err]  {board_id}/{task_id} → {pane}: {e}")
    return len(claims), migrated


async def migrate() -> int:
    r = aioredis.from_url(REDIS_URL, decode_responses=True)
    try:
        claim_keys = await _scan_claim_keys(r)
        print(f"Found {len(claim_keys)} v1 claim hashes")
        if not claim_keys:
            return 0
        total = 0
        ok = 0
        for ck in claim_keys:
            board_id = ck[len(CLAIM_KEY_PREFIX) :]
            t, m = await _migrate_board(r, ck)
            total += t
            ok += m
            print(f"  migrated board={board_id}: {m}/{t} claims")
        print(f"Done. {ok}/{total} claims migrated across {len(claim_keys)} boards.")
        print("Verify with: redis-cli XPENDING ws:channel:board:<id> board-<id>")
        print("Cleanup (after verify): redis-cli DEL ws:board:claims:<id>")
        return 0
    finally:
        await r.aclose()


if __name__ == "__main__":
    sys.exit(asyncio.run(migrate()))
