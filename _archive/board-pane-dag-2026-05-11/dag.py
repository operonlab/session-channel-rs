"""DAG dependency tracking for board v2.

Tasks publish with a ``depends_on: list[str]`` referring to *logical* task ids
(``TaskPublish.id``, e.g. ``"t1"``). Until every dependency has emitted a
``done`` event, the task is considered ``blocked``: claim attempts ack-and-redo
without delivering it to a worker, and the projection surfaces it with
``status="blocked"`` plus an ``unmet_deps`` list.

Key layout (per board)::

    ws:board:logical:{board_id}                       Hash logical_id -> msg_id
    ws:board:deps:{board_id}:{logical_id}             Set of unmet dep logical_ids
    ws:board:rev_deps:{board_id}:{logical_id}         Set of downstream logical_ids

The reverse-dep set lets ``mark_done_and_unblock`` find downstream tasks in
O(out-degree) instead of scanning every deps key.

This is intentionally separate from the stream itself: storing the DAG inline
in each publish event would force every consumer to replay the whole stream
to evaluate readiness. A small auxiliary index is cheaper and lets us emit
``dep_satisfied`` SSE events without re-reading the stream.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from redis.asyncio import Redis  # noqa: F401  (type-only import)


# --------------------------------------------------------------------------- #
# Key helpers                                                                  #
# --------------------------------------------------------------------------- #


def _logical_key(board_id: str) -> str:
    return f"ws:board:logical:{board_id}"


def _deps_key(board_id: str, logical_id: str) -> str:
    return f"ws:board:deps:{board_id}:{logical_id}"


def _rev_deps_key(board_id: str, logical_id: str) -> str:
    return f"ws:board:rev_deps:{board_id}:{logical_id}"


# --------------------------------------------------------------------------- #
# Public API                                                                   #
# --------------------------------------------------------------------------- #


async def register_task(
    redis,
    board_id: str,
    logical_id: str,
    msg_id: str,
    depends_on: list[str],
) -> None:
    """Record a freshly XADDed task in the DAG index.

    Called from the publish endpoint immediately after ``XADD`` returns
    ``msg_id``. Updates:

    * ``logical_id -> msg_id`` mapping (Hash)
    * unmet-deps set for this task (if any)
    * reverse-deps set for each upstream id (so completion can find us)

    No-op when ``logical_id`` is empty — without a logical id we cannot
    track DAG state, so the task degrades to "always ready".
    """
    if not logical_id:
        return
    pipe = redis.pipeline()
    pipe.hset(_logical_key(board_id), logical_id, msg_id)
    if depends_on:
        pipe.sadd(_deps_key(board_id, logical_id), *depends_on)
        for dep in depends_on:
            pipe.sadd(_rev_deps_key(board_id, dep), logical_id)
    await pipe.execute()


async def is_blocked(redis, board_id: str, logical_id: str) -> bool:
    """True iff this task still has unmet dependencies."""
    if not logical_id:
        return False
    return bool(await redis.scard(_deps_key(board_id, logical_id)))


async def get_unmet_deps(redis, board_id: str, logical_id: str) -> list[str]:
    """Snapshot of currently unmet dep logical ids (empty list when ready)."""
    if not logical_id:
        return []
    members = await redis.smembers(_deps_key(board_id, logical_id))
    # redis-py async returns a set[str] (decode_responses=True is on)
    return sorted(members) if isinstance(members, (set, list, tuple)) else list(members)


async def mark_done_and_unblock(
    redis,
    board_id: str,
    done_logical_id: str,
) -> list[str]:
    """SREM the completed task from every downstream's deps set.

    Returns the logical ids of tasks whose deps set just became empty —
    these are the newly-unblocked downstream tasks. The caller is expected
    to broadcast a ``dep_satisfied`` SSE event for each, so claim loops
    re-attempt them.
    """
    if not done_logical_id:
        return []

    unblocked: list[str] = []
    rev_key = _rev_deps_key(board_id, done_logical_id)
    downstream = await redis.smembers(rev_key)
    if not downstream:
        return []

    for downstream_id in downstream:
        if not downstream_id:
            continue
        deps_k = _deps_key(board_id, downstream_id)
        await redis.srem(deps_k, done_logical_id)
        if await redis.scard(deps_k) == 0:
            unblocked.append(downstream_id)
            await redis.delete(deps_k)
    # Reverse mapping for the completed task is no longer useful — drop it
    # so cleanup_board has less to scan later.
    try:
        await redis.delete(rev_key)
    except Exception:
        pass
    return unblocked


async def msg_id_for_logical(redis, board_id: str, logical_id: str) -> str | None:
    """Resolve a logical task id to its stream msg_id (or None)."""
    if not logical_id:
        return None
    return await redis.hget(_logical_key(board_id), logical_id)


async def cleanup_board(redis, board_id: str) -> None:
    """Drop all DAG state for a board.

    Intended for board reset / wipe paths. Uses ``SCAN`` with ``MATCH`` to
    avoid blocking the server on large boards.
    """
    patterns = [
        f"ws:board:deps:{board_id}:*",
        f"ws:board:rev_deps:{board_id}:*",
    ]
    for pattern in patterns:
        try:
            async for key in redis.scan_iter(match=pattern, count=200):
                await redis.delete(key)
        except Exception:
            # Best-effort: cleanup must not raise into request paths.
            continue
    try:
        await redis.delete(_logical_key(board_id))
    except Exception:
        pass


__all__ = [
    "cleanup_board",
    "get_unmet_deps",
    "is_blocked",
    "mark_done_and_unblock",
    "msg_id_for_logical",
    "register_task",
]
