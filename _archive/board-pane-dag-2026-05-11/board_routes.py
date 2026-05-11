"""Board API — task bulletin board built on Redis Streams consumer groups.

Architecture:
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
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

logger = logging.getLogger("session-channel.board")

from auth import require_auth
from dag import (
    get_unmet_deps,
    is_blocked,
    mark_done_and_unblock,
    register_task,
)
from fastapi import APIRouter, Depends, HTTPException, Request
from metrics import (
    CLAIM_CONFLICT_TOTAL,
    HEARTBEAT_LATENCY_MS,
    PROJECTION_MS,
    XREAD_LAG_MS,
)
from schemas import (
    LEASE_CONFIG,
    TaskClass,
    TaskProgress,
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
        "assigned_to": fields.get("assigned_to", "") or "",
        "priority": fields.get("priority", "normal"),
        "sender": fields.get("sender", ""),
    }


def _msg_id_to_timestamp(msg_id: str) -> int:
    """Stream msg_id format: '<ms>-<seq>'. Return ms epoch (0 on failure)."""
    try:
        return int(msg_id.split("-")[0])
    except (ValueError, IndexError, AttributeError):
        return 0


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
    with PROJECTION_MS.labels(board_id=board_id).time():
        return await _build_projection_inner(redis, board_id)


async def _build_projection_inner(redis, board_id: str) -> dict:
    """Inner projection (instrumented by ``_build_projection``)."""
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
    progress_events: dict[str, dict[str, Any]] = {}
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
                "progress": None,
                "result": None,
                "delivery_count": 0,
                "lease_until": None,
                "required_caps": decoded["required_caps"],
                "assigned_to": decoded["assigned_to"] or None,
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
        elif tag == "progress":
            tid = fields.get("task_id", "")
            if not tid:
                continue
            try:
                percent = int(fields.get("percent", "0"))
            except (TypeError, ValueError):
                percent = 0
            # Last-write-wins: stream order is monotonic, so plain assignment works.
            progress_events[tid] = {
                "percent": percent,
                "stage": fields.get("stage", ""),
                "note": fields.get("note", ""),
                "last_seen": _msg_id_to_timestamp(msg_id),
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
        # Progress overlay applies to any status (claimed mid-flight or done after report)
        if tid in progress_events:
            task["progress"] = progress_events[tid]

    # 4b. W4-B: open tasks with unmet deps surface as ``blocked``.
    # Done/claimed tasks are not re-evaluated — once claimed, the DAG check
    # has already been bypassed (or the task was never blocked to begin
    # with). Only ``open`` matters for UI gating.
    for task in tasks.values():
        if task["status"] != "open":
            continue
        logical_id = task.get("logical_id") or ""
        if not logical_id:
            continue
        try:
            if await is_blocked(redis, board_id, logical_id):
                task["status"] = "blocked"
                task["unmet_deps"] = await get_unmet_deps(redis, board_id, logical_id)
        except Exception:
            # Best-effort: a Redis hiccup must not break the projection.
            continue

    # 5. Dedupe by logical_id — DAG unblock and cap/assignment retries can
    # produce multiple stream entries for the same logical task. The user
    # mental model is one task per logical_id, so collapse stale entries
    # using a status-priority + recency rule:
    #   done > claimed > blocked > open
    # Within the same priority, the latest msg_id wins.
    _STATUS_RANK = {"done": 4, "claimed": 3, "blocked": 2, "open": 1}
    deduped: dict[str, dict[str, Any]] = {}
    for task in tasks.values():
        logical_id = task.get("logical_id") or task.get("id", "")
        # Tasks without logical_id (defensive) keep msg_id as their key so
        # they don't collide.
        key = logical_id if logical_id else task["id"]
        existing = deduped.get(key)
        if existing is None:
            deduped[key] = task
            continue
        new_rank = _STATUS_RANK.get(task["status"], 0)
        old_rank = _STATUS_RANK.get(existing["status"], 0)
        if new_rank > old_rank or (new_rank == old_rank and task["id"] > existing["id"]):
            deduped[key] = task

    task_list: list[dict[str, Any]] = []
    for task in deduped.values():
        item = dict(task)
        if isinstance(item.get("task_class"), TaskClass):
            item["task_class"] = item["task_class"].value
        task_list.append(item)
    task_list.sort(key=lambda t: t["id"])

    # 5b. W4-C: overlay dead-letter (failed) tasks from the failed stream.
    # Reaper promotes entries that exceed retry > 3 here; surfacing them in
    # the projection lets the UI distinguish persistent failures from
    # transient retries.
    failed_stream = f"{config.stream_prefix}board:{board_id}:failed"
    try:
        failed_entries = await redis.xrange(failed_stream)
    except Exception:
        failed_entries = []

    failed_tasks: list[dict[str, Any]] = []
    total_failed_retry = 0
    for fmsg_id, ffields in failed_entries:
        try:
            retry_count = int(ffields.get("retry_count", "0") or "0")
        except (TypeError, ValueError):
            retry_count = 0
        total_failed_retry += retry_count
        failed_tasks.append(
            {
                "id": fmsg_id,
                "logical_id": ffields.get("id", ""),
                "desc": ffields.get("desc", ""),
                "task_class": ffields.get("task_class", TaskClass.SHORT.value),
                "retry_count": retry_count,
                "status": "failed",
                "failed_at": _msg_id_to_timestamp(fmsg_id),
                "original_sender": ffields.get("sender", ""),
                "assigned_to": ffields.get("assigned_to", "") or None,
            }
        )

    summary = {
        "total": len(task_list),
        "open": sum(1 for t in task_list if t["status"] == "open"),
        "claimed": sum(1 for t in task_list if t["status"] == "claimed"),
        "done": sum(1 for t in task_list if t["status"] == "done"),
        "blocked": sum(1 for t in task_list if t["status"] == "blocked"),
        "failed": len(failed_tasks),
        "with_progress": sum(1 for t in task_list if t.get("progress")),
        "with_result": sum(1 for t in task_list if t.get("result")),
        "total_tokens": sum(
            int((t.get("result") or {}).get("tokens_used") or 0) for t in task_list
        ),
        "total_retry_count": total_failed_retry,
    }

    return {
        "board_id": board_id,
        "tasks": task_list,
        "failed_tasks": failed_tasks,
        "summary": summary,
    }


# --------------------------------------------------------------------------- #
# HTTP endpoints                                                               #
# --------------------------------------------------------------------------- #


@board_router.get("/api/board/{board_id}")
async def get_board(request: Request, board_id: str, _=Depends(require_auth)):
    """Read-only projection: stream messages + pending summary → board state."""
    redis = request.app.state.redis
    projection = await _build_projection(redis, board_id)
    if not projection["tasks"] and not projection.get("failed_tasks"):
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

    # Register topic so reaper / trim / fanout loops can discover this board.
    # Without this SADD, reaper_loop's smembers(topics_key) never sees board:*
    # entries and lease expiration is silently broken.
    try:
        await redis.sadd(config.topics_key, f"board:{board_id}")
    except Exception:
        logger.exception("topics_key_sadd_failed")

    msg_ids: list[str] = []
    for raw in tasks_raw:
        try:
            fields = _encode_publish_fields(raw, sender)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"invalid task: {exc}") from exc
        msg_id = await redis.xadd(sk, fields, maxlen=config.max_stream_len, approximate=True)
        msg_ids.append(msg_id)

        # W4-B: record DAG state so blocked tasks can be filtered at claim
        # time. We re-derive logical_id / depends_on from the raw dict —
        # the encoded fields are already JSON strings.
        logical_id = (fields.get("id") or "").strip()
        try:
            deps = json.loads(fields.get("depends_on") or "[]")
        except (json.JSONDecodeError, TypeError):
            deps = []
        if not isinstance(deps, list):
            deps = []
        await register_task(redis, board_id, logical_id, msg_id, deps)

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
    """Atomically claim up to ``count`` tasks via XREADGROUP, with cap check.

    Body::

        {"pane": "pane:foo", "count": 1}

    Capability-aware claim (W3-B):
      1. Look up pane caps from ``ws:panes:{pane}`` (mcps + skills union).
      2. XREADGROUP delivers candidate entries.
      3. For each entry, compare ``required_caps`` against the pane's set.
         Mismatches are XACKed (cleared from the PEL) and the publish
         payload is XADDed back so another pane can claim them. SSE
         broadcasts a ``cap_rejected`` event.
      4. Only matching entries are returned to the caller.

    Note: a pane that never advertised has ``pane_caps_set = set()``; any
    task with non-empty ``required_caps`` will therefore be rejected. Tasks
    with empty ``required_caps`` are always acceptable.

    Returns::

        {"ok": True, "tasks": [{...}], "rejected_count": N}

    or when nothing matches::

        {"ok": False, "reason": "caps_mismatch", "missing_caps": [...], "rejected_count": N}
        {"ok": False, "reason": "no_tasks"}
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

    logger.info(
        "claim_request",
        extra={"board_id": board_id, "pane": pane, "count": count},
    )

    redis = request.app.state.redis
    sk = _stream_key(board_id)
    gk = _group_key(board_id)

    await _ensure_group(redis, board_id)

    # 1. Look up pane caps (mcps + skills union)
    pane_caps_key = f"ws:panes:{pane}"
    try:
        raw_caps = await redis.hgetall(pane_caps_key)
    except Exception:
        raw_caps = {}
    try:
        pane_mcps = json.loads(raw_caps.get("mcps", "[]")) if raw_caps else []
    except (json.JSONDecodeError, TypeError):
        pane_mcps = []
    try:
        pane_skills = json.loads(raw_caps.get("skills", "[]")) if raw_caps else []
    except (json.JSONDecodeError, TypeError):
        pane_skills = []
    pane_caps_set: set[str] = set(pane_mcps) | set(pane_skills)

    # 2. XREADGROUP returns: [(stream_name, [(msg_id, {field: value, ...}), ...])]
    _xread_t0 = time.perf_counter()
    try:
        result = await redis.xreadgroup(gk, pane, {sk: ">"}, count=count, block=0)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"xreadgroup failed: {e}") from e
    XREAD_LAG_MS.labels(board_id=board_id).observe((time.perf_counter() - _xread_t0) * 1000)

    if not result:
        return {"ok": False, "reason": "no_tasks"}

    claimed: list[dict[str, Any]] = []
    rejected: list[tuple[str, list[str]]] = []  # (msg_id, missing_caps)
    rejected_assignment: list[tuple[str, str]] = []  # (msg_id, assigned_to)
    rejected_blocked: list[tuple[str, list[str]]] = []  # (msg_id, unmet_deps)

    for _stream_name, entries in result:
        for msg_id, fields in entries:
            tag = fields.get("tag", "")
            if tag != "publish":
                # Defensive: only publish entries should reach the consumer
                # group, but ack anything else to drain the PEL.
                await redis.xack(sk, gk, msg_id)
                continue

            # DAG check (W4-B): runs BEFORE assignment + caps checks.
            # If unmet deps remain, XCLAIM the entry to the special
            # ``__blocked_holder`` consumer instead of ack+republish.
            # Holding it in the PEL prevents stream bloat (republish would
            # accumulate a new entry per claim attempt) while still keeping
            # the message reachable — ``mark_done_and_unblock`` will XCLAIM
            # it back to a regular consumer once deps clear.
            logical_id = (fields.get("id") or "").strip()
            if logical_id and await is_blocked(redis, board_id, logical_id):
                unmet = await get_unmet_deps(redis, board_id, logical_id)
                try:
                    await redis.xclaim(
                        sk,
                        gk,
                        "__blocked_holder",
                        min_idle_time=0,
                        message_ids=[msg_id],
                        justid=True,
                    )
                except Exception:
                    logger.exception("blocked_xclaim_failed")
                rejected_blocked.append((msg_id, unmet))
                CLAIM_CONFLICT_TOTAL.labels(board_id=board_id, reason="blocked").inc()
                continue

            # Assignment check (W4-A): runs BEFORE caps check.
            # Empty assigned_to → public task, fall through to caps logic.
            # Non-empty + mismatch → ack + redo + SSE assignment_rejected.
            assigned = (fields.get("assigned_to") or "").strip()
            if assigned and assigned != pane:
                try:
                    await redis.xack(sk, gk, msg_id)
                    await redis.xadd(sk, fields, maxlen=config.max_stream_len, approximate=True)
                except Exception:
                    pass
                rejected_assignment.append((msg_id, assigned))
                CLAIM_CONFLICT_TOTAL.labels(board_id=board_id, reason="assignment_mismatch").inc()
                continue

            try:
                required = json.loads(fields.get("required_caps") or "[]")
            except (json.JSONDecodeError, TypeError):
                required = []
            if not isinstance(required, list):
                required = []

            missing = [c for c in required if c not in pane_caps_set]
            if missing:
                # Cap mismatch: ack original entry (clears PEL) + re-publish
                # the same payload so another pane can claim it. We do NOT
                # bump any retry counter — this is not a worker failure.
                try:
                    await redis.xack(sk, gk, msg_id)
                    await redis.xadd(sk, fields, maxlen=config.max_stream_len, approximate=True)
                except Exception:
                    # Best-effort: if redo fails, the entry stays in PEL and
                    # the reaper loop will eventually recover it.
                    pass
                rejected.append((msg_id, missing))
                CLAIM_CONFLICT_TOTAL.labels(board_id=board_id, reason="caps_mismatch").inc()
                continue

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

    # SSE: claim success + cap_rejected events
    if claimed:
        sse_broadcast(
            {
                "topic": f"board:{board_id}",
                "tag": "claim",
                "pane": pane,
                "task_ids": [t["id"] for t in claimed],
            }
        )
    for msg_id, missing in rejected:
        sse_broadcast(
            {
                "topic": f"board:{board_id}",
                "tag": "cap_rejected",
                "task_id": msg_id,
                "pane": pane,
                "missing_caps": missing,
            }
        )
    for msg_id, assigned in rejected_assignment:
        sse_broadcast(
            {
                "topic": f"board:{board_id}",
                "tag": "assignment_rejected",
                "task_id": msg_id,
                "pane": pane,
                "assigned_to": assigned,
            }
        )
    for msg_id, unmet in rejected_blocked:
        sse_broadcast(
            {
                "topic": f"board:{board_id}",
                "tag": "blocked",
                "task_id": msg_id,
                "pane": pane,
                "unmet_deps": unmet,
            }
        )

    if not claimed and rejected_blocked:
        return {
            "ok": False,
            "reason": "blocked",
            "unmet_deps": rejected_blocked[0][1],
            "rejected_count": len(rejected),
            "rejected_assignment_count": len(rejected_assignment),
            "rejected_blocked_count": len(rejected_blocked),
        }
    if not claimed and rejected_assignment:
        return {
            "ok": False,
            "reason": "assignment_mismatch",
            "rejected_count": len(rejected),
            "rejected_assignment_count": len(rejected_assignment),
            "rejected_blocked_count": len(rejected_blocked),
        }
    if not claimed and rejected:
        return {
            "ok": False,
            "reason": "caps_mismatch",
            "missing_caps": rejected[0][1],
            "rejected_count": len(rejected),
            "rejected_assignment_count": len(rejected_assignment),
            "rejected_blocked_count": len(rejected_blocked),
        }
    if not claimed:
        return {"ok": False, "reason": "no_tasks"}

    return {
        "ok": True,
        "tasks": claimed,
        "rejected_count": len(rejected),
        "rejected_assignment_count": len(rejected_assignment),
        "rejected_blocked_count": len(rejected_blocked),
    }


@board_router.post("/api/board/{board_id}/complete")
async def complete_task(request: Request, board_id: str, _=Depends(require_auth)):
    """Acknowledge a claimed task and append a ``done`` event.

    Body::

        {"task_id": "<stream-msg-id>", "pane": "pane:foo", "result": {...}}
    """
    body = await request.json()
    task_id = (body.get("task_id") or "").strip()
    pane = (body.get("pane") or "").strip()
    result_raw = body.get("result")
    if result_raw is None:
        result_raw = {}

    if not task_id or not pane:
        raise HTTPException(status_code=400, detail="task_id and pane required")

    # Backward compat: accept legacy str result by wrapping into payload.note
    if isinstance(result_raw, str):
        result_raw = {"status": "ok", "payload": {"note": result_raw}}
    if not isinstance(result_raw, dict):
        raise HTTPException(
            status_code=400, detail="invalid result schema: must be object or string"
        )

    try:
        result = TaskResult(**result_raw)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"invalid result schema: {exc}") from exc

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

    # 3. W4-B: resolve task_id (msg_id) → logical_id and unblock downstream.
    # The deps map is keyed by logical id (the caller-provided ``id`` field
    # on TaskPublish), so we need to look up the publish entry from the
    # stream to recover it.
    unblocked: list[str] = []
    try:
        entries = await redis.xrange(sk, min=task_id, max=task_id)
    except Exception:
        entries = []
    done_logical_id = ""
    if entries:
        try:
            _id, fields = entries[0]
            done_logical_id = (fields.get("id") or "").strip()
        except (TypeError, ValueError, AttributeError):
            done_logical_id = ""
    if done_logical_id:
        try:
            unblocked = await mark_done_and_unblock(redis, board_id, done_logical_id)
        except Exception:
            unblocked = []
        # Release each newly-unblocked downstream from __blocked_holder PEL
        # so XREADGROUP can deliver it again to a real consumer.
        for downstream in unblocked:
            try:
                downstream_msg_id = await redis.hget(f"ws:board:logical:{board_id}", downstream)
                if downstream_msg_id:
                    await redis.xclaim(
                        sk,
                        gk,
                        "__reaper",
                        min_idle_time=0,
                        message_ids=[downstream_msg_id],
                        justid=True,
                    )
                    # Reaper loop will republish on next sweep; nudge by also
                    # XACK + XADD now so it surfaces immediately.
                    entries_dn = await redis.xrange(
                        sk, min=downstream_msg_id, max=downstream_msg_id
                    )
                    if entries_dn:
                        _dn_id, dn_fields = entries_dn[0]
                        await redis.xack(sk, gk, downstream_msg_id)
                        await redis.xadd(
                            sk, dn_fields, maxlen=config.max_stream_len, approximate=True
                        )
            except Exception:
                logger.exception("dep_satisfied_release_failed")
            sse_broadcast(
                {
                    "topic": f"board:{board_id}",
                    "tag": "dep_satisfied",
                    "logical_id": downstream,
                    "completed": done_logical_id,
                }
            )

    return {
        "ok": True,
        "task_id": task_id,
        "acked": int(acked or 0),
        "done_event": done_msg_id,
        "unblocked": unblocked,
    }


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

    _hb_t0 = time.perf_counter()
    try:
        result = await redis.xclaim(
            sk, gk, pane, min_idle_time=0, message_ids=[task_id], justid=True
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"heartbeat failed: {e}") from e
    HEARTBEAT_LATENCY_MS.labels(board_id=board_id).observe((time.perf_counter() - _hb_t0) * 1000)

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


@board_router.post("/api/board/{board_id}/progress")
async def report_progress(request: Request, board_id: str, _=Depends(require_auth)):
    """Report task progress between claim and complete.

    Body::

        {"pane": "pane:foo", "task_id": "...", "percent": 42,
         "stage": "embedding", "note": "rows 4200/10000"}

    XADDs a ``tag=progress`` event so the projection enriches the task with
    a ``progress`` block (percent / stage / note / last_seen). Does NOT touch
    the consumer-group PEL — for lease refresh use ``/heartbeat`` instead.
    """
    body = await request.json()
    pane = (body.get("pane") or "").strip()
    if not pane:
        raise HTTPException(status_code=400, detail="pane required")

    try:
        prog = TaskProgress(**body)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"invalid progress: {exc}") from exc

    redis = request.app.state.redis
    sk = _stream_key(board_id)

    fields = {
        "tag": "progress",
        "task_id": prog.task_id,
        "percent": str(prog.percent),
        "stage": prog.stage,
        "note": prog.note,
        "sender": pane,
    }
    msg_id = await redis.xadd(sk, fields, maxlen=config.max_stream_len, approximate=True)

    sse_broadcast(
        {
            "id": msg_id,
            "topic": f"board:{board_id}",
            **fields,
        }
    )

    return {"ok": True, "id": msg_id, "task_id": prog.task_id}


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


@board_router.get("/api/board/{board_id}/failed")
async def list_failed(request: Request, board_id: str, _=Depends(require_auth)):
    """List dead-letter entries (tasks promoted after retry > 3).

    Reads ``ws:channel:board:{board_id}:failed`` written by the W2-B reaper
    when an entry's retry_count exceeds 3. ``failed_at`` is derived from the
    dead-letter msg_id timestamp (i.e. when the reaper promoted it, not when
    the original task was first published).
    """
    redis = request.app.state.redis
    failed_stream = f"{config.stream_prefix}board:{board_id}:failed"
    try:
        raw = await redis.xrange(failed_stream)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"xrange failed: {e}") from e

    items: list[dict[str, Any]] = []
    for msg_id, fields in raw:
        try:
            retry_count = int(fields.get("retry_count", "0") or "0")
        except (TypeError, ValueError):
            retry_count = 0
        items.append(
            {
                "id": msg_id,
                "logical_id": fields.get("id", ""),
                "desc": fields.get("desc", ""),
                "task_class": fields.get("task_class", "short"),
                "retry_count": retry_count,
                "failed_at": _msg_id_to_timestamp(msg_id),
                "original_sender": fields.get("sender", ""),
                "assigned_to": fields.get("assigned_to", "") or "",
            }
        )

    return {"board_id": board_id, "failed": items, "count": len(items)}


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
