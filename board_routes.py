"""Board API — task bulletin board built on Redis Streams consumer groups.

v2 architecture:
- Each board is a Redis Stream: ``ws:channel:board:{board_id}``
- A single consumer group ``board-{board_id}`` provides atomic exactly-once
  claim semantics via ``XREADGROUP``.
- Completion is acknowledged with ``XACK`` and a ``tag=done`` event is
  appended via ``XADD`` so downstream readers (and the projection) can
  observe the result.
- Drops use ``XCLAIM`` to transfer ownership to the special ``__reaper``
  consumer; the reaper loop (W2-B) is responsible for re-publishing.
- The projection rebuilds state from the stream + ``XPENDING`` summary,
  so there is no auxiliary claims hash to maintain.

This module replaces the v1 Lua-CAS based implementation in ``board_lua.py``.
"""

from __future__ import annotations

import json
import time
from typing import Any

from auth import require_auth
from fastapi import APIRouter, Depends, HTTPException, Request
from schemas import (
    LEASE_CONFIG,
    TaskClass,
    TaskPublish,
    TaskResult,
)

from config import config
from routes import sse_broadcast

board_router = APIRouter(tags=["board"])


# --------------------------------------------------------------------------- #
# Key / group helpers                                                          #
# --------------------------------------------------------------------------- #


def _stream_key(board_id: str) -> str:
    return f"{config.stream_prefix}board:{board_id}"


def _group_key(board_id: str) -> str:
    return f"board-{board_id}"


async def _ensure_group(redis, board_id: str) -> None:
    """Ensure the consumer group exists for this board (idempotent).

    Uses ``XGROUP CREATE ... MKSTREAM`` so the stream is auto-created the
    first time anything publishes. Re-creation raises ``BUSYGROUP`` which
    we swallow.
    """
    sk = _stream_key(board_id)
    gk = _group_key(board_id)
    try:
        await redis.xgroup_create(sk, gk, id="0", mkstream=True)
    except Exception as e:  # pragma: no cover — depends on redis backend
        if "BUSYGROUP" not in str(e):
            raise


# --------------------------------------------------------------------------- #
# Field encoding helpers                                                       #
# --------------------------------------------------------------------------- #


def _encode_publish_fields(task: dict, sender: str) -> dict[str, str]:
    """Convert a TaskPublish dict to flat string fields for XADD."""
    tp = TaskPublish(**task)
    return {
        "tag": "publish",
        "id": tp.id,
        "desc": tp.desc,
        "task_class": tp.task_class.value,
        "required_caps": json.dumps(tp.required_caps),
        "assigned_to": tp.assigned_to or "",
        "depends_on": json.dumps(tp.depends_on),
        "priority": tp.priority,
        "sender": sender,
    }


def _decode_publish_fields(fields: dict[str, str]) -> dict[str, Any]:
    """Reverse of _encode_publish_fields — used during projection."""
    try:
        required_caps = json.loads(fields.get("required_caps") or "[]")
    except (json.JSONDecodeError, TypeError):
        required_caps = []
    try:
        task_class = TaskClass(fields.get("task_class") or TaskClass.SHORT.value)
    except ValueError:
        task_class = TaskClass.SHORT
    return {
        "id": fields.get("id", ""),
        "desc": fields.get("desc", ""),
        "task_class": task_class,
        "required_caps": required_caps,
        "priority": fields.get("priority", "normal"),
        "sender": fields.get("sender", ""),
    }


# --------------------------------------------------------------------------- #
# Projection                                                                   #
# --------------------------------------------------------------------------- #


async def _build_projection(redis, board_id: str) -> dict:
    """Compute board state from the stream + pending summary.

    Strategy:
      1. ``XRANGE`` the entire stream once to recover publish/done events.
      2. ``XPENDING`` (detail form) to enumerate currently-claimed entries
         (msg_id, consumer, idle_ms, delivery_count).
      3. Tasks are keyed by Redis stream message id (which is the canonical
         ``task_id`` post-v2). Status derivation:
           - publish event seen + msg_id in pending     → ``claimed``
           - publish event seen + done event seen        → ``done``
           - publish event seen + neither pending/done   → ``open``
      4. ``lease_until`` is computed from ``idle_ms`` against the per-class
         lease budget so the UI can render a countdown.
    """
    sk = _stream_key(board_id)
    gk = _group_key(board_id)

    # 1. Bulk read of stream
    raw_msgs = await redis.xrange(sk)

    # 2. Pending detail (we want consumer + idle time per id)
    pending_detail: list[Any] = []
    try:
        # redis-py: xpending_range(name, groupname, min, max, count, consumername=None)
        pending_detail = await redis.xpending_range(sk, gk, "-", "+", 1000)
    except Exception:
        pending_detail = []

    pending_by_id: dict[str, dict[str, Any]] = {}
    for entry in pending_detail:
        # entry is a dict in redis-py async client:
        #   {"message_id": ..., "consumer": ..., "time_since_delivered": ..., "times_delivered": ...}
        if isinstance(entry, dict):
            mid = entry.get("message_id") or entry.get("messageId")
            consumer = entry.get("consumer")
            idle = (
                entry.get("time_since_delivered") or entry.get("milliseconds_since_delivered") or 0
            )
            delivery = entry.get("times_delivered") or entry.get("delivery_count") or 0
        else:
            # Defensive: tuple form (id, consumer, idle, delivery)
            try:
                mid, consumer, idle, delivery = entry
            except (TypeError, ValueError):
                continue
        pending_by_id[mid] = {
            "consumer": consumer or "",
            "idle_ms": int(idle or 0),
            "delivery_count": int(delivery or 0),
        }

    # 3. Walk stream events
    tasks: dict[str, dict[str, Any]] = {}
    done_events: dict[str, dict[str, Any]] = {}
    for msg_id, fields in raw_msgs:
        tag = fields.get("tag", "")
        if tag == "publish":
            decoded = _decode_publish_fields(fields)
            # Use the stream msg_id as canonical task_id (stable per consumer-group)
            tasks[msg_id] = {
                "id": msg_id,
                "logical_id": decoded["id"],
                "desc": decoded["desc"],
                "task_class": decoded["task_class"],
                "status": "open",
                "claimed_by": None,
                "done_by": None,
                "result": None,
                "delivery_count": 0,
                "lease_until": None,
                "required_caps": decoded["required_caps"],
            }
        elif tag == "done":
            tid = fields.get("task_id", "")
            try:
                result_payload = json.loads(fields.get("result") or "{}")
            except (json.JSONDecodeError, TypeError):
                result_payload = {}
            done_events[tid] = {
                "done_by": fields.get("done_by", ""),
                "result": result_payload,
            }

    now_ms = int(time.time() * 1000)

    # 4. Overlay claim / done state
    for tid, task in tasks.items():
        if tid in done_events:
            task["status"] = "done"
            task["done_by"] = done_events[tid]["done_by"]
            try:
                task["result"] = TaskResult(**done_events[tid]["result"]).model_dump()
            except Exception:
                task["result"] = done_events[tid]["result"]
        elif tid in pending_by_id:
            p = pending_by_id[tid]
            task["status"] = "claimed"
            task["claimed_by"] = p["consumer"]
            task["delivery_count"] = p["delivery_count"]
            lease_seconds = LEASE_CONFIG[task["task_class"]]["lease_seconds"]
            lease_remaining_ms = max(lease_seconds * 1000 - p["idle_ms"], 0)
            task["lease_until"] = (now_ms + lease_remaining_ms) // 1000

    # 5. Convert TaskClass enum to string for JSON serialization
    task_list: list[dict[str, Any]] = []
    for task in tasks.values():
        item = dict(task)
        if isinstance(item.get("task_class"), TaskClass):
            item["task_class"] = item["task_class"].value
        task_list.append(item)
    task_list.sort(key=lambda t: t["id"])

    summary = {
        "total": len(task_list),
        "open": sum(1 for t in task_list if t["status"] == "open"),
        "claimed": sum(1 for t in task_list if t["status"] == "claimed"),
        "done": sum(1 for t in task_list if t["status"] == "done"),
    }

    return {"board_id": board_id, "tasks": task_list, "summary": summary}


# --------------------------------------------------------------------------- #
# HTTP endpoints                                                               #
# --------------------------------------------------------------------------- #


@board_router.get("/api/board/{board_id}")
async def get_board(request: Request, board_id: str, _=Depends(require_auth)):
    """Read-only projection: stream messages + pending summary → board state."""
    redis = request.app.state.redis
    projection = await _build_projection(redis, board_id)
    if not projection["tasks"]:
        raise HTTPException(status_code=404, detail=f"Board '{board_id}' not found or empty")
    return projection


@board_router.post("/api/board/{board_id}/publish")
async def publish_tasks(request: Request, board_id: str, _=Depends(require_auth)):
    """Publish a batch of tasks to the board stream.

    Body::

        {
          "sender": "pane:foo",
          "tasks": [TaskPublish, ...]
        }

    Each task is XADD'd as its own stream entry so the consumer group can
    deliver them independently. Returns the stream message ids — these are
    the canonical task ids used for claim/complete/drop.
    """
    body = await request.json()
    sender = (body.get("sender") or "").strip()
    tasks_raw = body.get("tasks") or []

    if not sender:
        raise HTTPException(status_code=400, detail="sender required")
    if not isinstance(tasks_raw, list) or not tasks_raw:
        raise HTTPException(status_code=400, detail="tasks must be a non-empty list")

    redis = request.app.state.redis
    sk = _stream_key(board_id)

    await _ensure_group(redis, board_id)

    msg_ids: list[str] = []
    for raw in tasks_raw:
        try:
            fields = _encode_publish_fields(raw, sender)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"invalid task: {exc}") from exc
        msg_id = await redis.xadd(sk, fields, maxlen=config.max_stream_len, approximate=True)
        msg_ids.append(msg_id)

    sse_broadcast(
        {
            "topic": f"board:{board_id}",
            "tag": "publish",
            "ids": msg_ids,
            "sender": sender,
            "count": len(msg_ids),
        }
    )

    return {"ok": True, "ids": msg_ids}


@board_router.post("/api/board/{board_id}/claim")
async def claim_task(request: Request, board_id: str, _=Depends(require_auth)):
    """Atomically claim up to ``count`` tasks via XREADGROUP.

    Body::

        {"pane": "pane:foo", "count": 1}

    Returns either::

        {"ok": True, "tasks": [{...}]}

    or::

        {"ok": False, "reason": "no_tasks"}

    The returned ``id`` field is the stream message id and serves as the
    canonical task id for subsequent complete/drop calls.
    """
    body = await request.json()
    pane = (body.get("pane") or "").strip()
    try:
        count = int(body.get("count") or 1)
    except (TypeError, ValueError):
        count = 1
    count = max(1, min(count, 100))

    if not pane:
        raise HTTPException(status_code=400, detail="pane required")

    redis = request.app.state.redis
    sk = _stream_key(board_id)
    gk = _group_key(board_id)

    await _ensure_group(redis, board_id)

    # XREADGROUP returns: [(stream_name, [(msg_id, {field: value, ...}), ...])]
    try:
        result = await redis.xreadgroup(gk, pane, {sk: ">"}, count=count, block=0)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"xreadgroup failed: {e}") from e

    if not result:
        return {"ok": False, "reason": "no_tasks"}

    claimed: list[dict[str, Any]] = []
    for _stream_name, entries in result:
        for msg_id, fields in entries:
            decoded = _decode_publish_fields(fields)
            claimed.append(
                {
                    "id": msg_id,
                    "logical_id": decoded["id"],
                    "desc": decoded["desc"],
                    "task_class": decoded["task_class"].value,
                    "required_caps": decoded["required_caps"],
                    "priority": decoded["priority"],
                    "sender": decoded["sender"],
                }
            )

    if not claimed:
        return {"ok": False, "reason": "no_tasks"}

    sse_broadcast(
        {
            "topic": f"board:{board_id}",
            "tag": "claim",
            "pane": pane,
            "task_ids": [t["id"] for t in claimed],
        }
    )

    return {"ok": True, "tasks": claimed}


@board_router.post("/api/board/{board_id}/complete")
async def complete_task(request: Request, board_id: str, _=Depends(require_auth)):
    """Acknowledge a claimed task and append a ``done`` event.

    Body::

        {"task_id": "<stream-msg-id>", "pane": "pane:foo", "result": {...}}
    """
    body = await request.json()
    task_id = (body.get("task_id") or "").strip()
    pane = (body.get("pane") or "").strip()
    result_raw = body.get("result") or {}

    if not task_id or not pane:
        raise HTTPException(status_code=400, detail="task_id and pane required")

    try:
        result = TaskResult(**result_raw)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"invalid result: {exc}") from exc

    redis = request.app.state.redis
    sk = _stream_key(board_id)
    gk = _group_key(board_id)

    await _ensure_group(redis, board_id)

    # 1. XACK — removes from PEL
    acked = await redis.xack(sk, gk, task_id)

    # 2. Append done event so projection / readers can see the result.
    done_msg_id = await redis.xadd(
        sk,
        {
            "tag": "done",
            "task_id": task_id,
            "result": result.model_dump_json(),
            "done_by": pane,
        },
        maxlen=config.max_stream_len,
        approximate=True,
    )

    sse_broadcast(
        {
            "topic": f"board:{board_id}",
            "tag": "done",
            "task_id": task_id,
            "pane": pane,
            "result_status": result.status,
        }
    )

    return {"ok": True, "task_id": task_id, "acked": int(acked or 0), "done_event": done_msg_id}


@board_router.post("/api/board/{board_id}/drop")
async def drop_task(request: Request, board_id: str, _=Depends(require_auth)):
    """Release a claimed task by force-claiming it for ``__reaper``.

    Body::

        {"task_id": "<stream-msg-id>", "pane": "pane:foo"}

    Uses ``XCLAIM ... FORCE``: the reaper consumer becomes the new owner and
    the W2-B reaper loop will subsequently re-publish or expire the entry.
    """
    body = await request.json()
    task_id = (body.get("task_id") or "").strip()
    pane = (body.get("pane") or "").strip()

    if not task_id or not pane:
        raise HTTPException(status_code=400, detail="task_id and pane required")

    redis = request.app.state.redis
    sk = _stream_key(board_id)
    gk = _group_key(board_id)

    await _ensure_group(redis, board_id)

    try:
        # XCLAIM <stream> <group> <consumer> <min-idle-ms> <id> [FORCE]
        await redis.xclaim(sk, gk, "__reaper", min_idle_time=0, message_ids=[task_id], force=True)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"xclaim failed: {e}") from e

    sse_broadcast(
        {
            "topic": f"board:{board_id}",
            "tag": "drop",
            "task_id": task_id,
            "pane": pane,
        }
    )

    return {"ok": True, "task_id": task_id}


@board_router.post("/api/board/{board_id}/heartbeat")
async def heartbeat_task(request: Request, board_id: str, _=Depends(require_auth)):
    """Reset idle time on a claimed entry via XCLAIM (no ownership transfer).

    Body::

        {"task_id": "<stream-msg-id>", "pane": "pane:foo"}

    Uses ``XCLAIM ... JUSTID`` with ``min_idle_time=0`` to refresh the PEL
    delivery time without changing the consumer. If the entry isn't pending
    or the caller isn't the current holder, returns ``{"ok": False}``.
    """
    body = await request.json()
    task_id = (body.get("task_id") or "").strip()
    pane = (body.get("pane") or "").strip()

    if not task_id or not pane:
        raise HTTPException(status_code=400, detail="task_id and pane required")

    redis = request.app.state.redis
    sk = _stream_key(board_id)
    gk = _group_key(board_id)

    await _ensure_group(redis, board_id)

    try:
        result = await redis.xclaim(
            sk, gk, pane, min_idle_time=0, message_ids=[task_id], justid=True
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"heartbeat failed: {e}") from e

    if not result:
        return {"ok": False, "reason": "not_pending_or_not_holder"}

    sse_broadcast(
        {
            "topic": f"board:{board_id}",
            "tag": "heartbeat",
            "task_id": task_id,
            "pane": pane,
        }
    )

    return {"ok": True, "task_id": task_id}


@board_router.get("/api/board/{board_id}/pending")
async def get_pending(
    request: Request,
    board_id: str,
    pane: str = "",
    _=Depends(require_auth),
):
    """List currently-pending entries via ``XPENDING`` (detail form).

    Optional ``?pane=`` filters to a single consumer. Returned shape::

        [{"task_id": "...", "consumer": "...", "idle_ms": 1234, "delivery_count": 1}, ...]

    Used by W2-C (Stop hook auto-release) and the reaper loop for inspection.
    """
    redis = request.app.state.redis
    sk = _stream_key(board_id)
    gk = _group_key(board_id)

    await _ensure_group(redis, board_id)

    try:
        if pane:
            detail = await redis.xpending_range(sk, gk, "-", "+", 1000, consumername=pane)
        else:
            detail = await redis.xpending_range(sk, gk, "-", "+", 1000)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"xpending failed: {e}") from e

    out: list[dict[str, Any]] = []
    for entry in detail:
        if isinstance(entry, dict):
            mid = entry.get("message_id") or entry.get("messageId")
            consumer = entry.get("consumer") or ""
            idle = (
                entry.get("time_since_delivered") or entry.get("milliseconds_since_delivered") or 0
            )
            delivery = entry.get("times_delivered") or entry.get("delivery_count") or 0
        else:
            try:
                mid, consumer, idle, delivery = entry
            except (TypeError, ValueError):
                continue
        out.append(
            {
                "task_id": mid,
                "consumer": consumer,
                "idle_ms": int(idle or 0),
                "delivery_count": int(delivery or 0),
            }
        )

    return {"board_id": board_id, "pending": out, "count": len(out)}


@board_router.get("/api/panes/{pane_id}/pending")
async def get_pending_by_pane(
    request: Request,
    pane_id: str,
    _=Depends(require_auth),
):
    """List all pending entries claimed by ``pane_id`` across active boards.

    Used by the Stop hook (W2-C) to enumerate every board task this pane
    currently holds so they can be force-released back to ``__reaper`` —
    avoiding zombie claims that block the lease until ``visibility_timeout``.

    Discovers boards by scanning ``config.topics_key`` for ``board:*``
    entries (excluding dead-letter ``:failed`` topics). For each board, runs
    ``XPENDING`` filtered by consumer = ``pane_id``. Boards whose group has
    not yet been created are skipped silently.

    Returned shape::

        {"pane_id": "...", "pending": [
            {"board_id": "...", "task_id": "...", "idle_ms": 1234, "delivery_count": 1},
            ...
        ], "count": N}
    """
    redis = request.app.state.redis

    try:
        topics = await redis.smembers(config.topics_key)
    except Exception:
        topics = set()

    board_topics = [t for t in topics if t.startswith("board:") and not t.endswith(":failed")]

    items: list[dict[str, Any]] = []
    for topic in board_topics:
        board_id = topic.removeprefix("board:")
        sk = _stream_key(board_id)
        gk = _group_key(board_id)
        try:
            # XPENDING filtered by consumer — group must already exist; we do
            # NOT call _ensure_group here because that would auto-create empty
            # groups for every dead board on every Stop hook.
            pending = await redis.xpending_range(
                sk, gk, min="-", max="+", count=1000, consumername=pane_id
            )
        except Exception:
            # Group not found, stream gone, or other transient error → skip
            continue

        for entry in pending:
            if isinstance(entry, dict):
                mid = entry.get("message_id") or entry.get("messageId")
                idle = (
                    entry.get("time_since_delivered")
                    or entry.get("milliseconds_since_delivered")
                    or 0
                )
                delivery = entry.get("times_delivered") or entry.get("delivery_count") or 0
            else:
                # tuple form: (msg_id, consumer, idle_ms, delivery_count)
                try:
                    mid, _consumer, idle, delivery = entry[:4]
                except (TypeError, ValueError):
                    continue
            if mid is None:
                continue
            if isinstance(mid, bytes):
                mid = mid.decode()
            items.append(
                {
                    "board_id": board_id,
                    "task_id": mid,
                    "idle_ms": int(idle or 0),
                    "delivery_count": int(delivery or 0),
                }
            )

    return {"pane_id": pane_id, "pending": items, "count": len(items)}


# Re-export commonly used helpers for sibling modules (W2-B reaper, W2-C hook)
__all__ = [
    "_build_projection",
    "_ensure_group",
    "_group_key",
    "_stream_key",
    "board_router",
]
