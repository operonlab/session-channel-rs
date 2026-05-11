"""Session Channel API routes."""

from __future__ import annotations

import asyncio
import json
import time
from collections import defaultdict

from auth import require_auth
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from config import config

router = APIRouter()

# --- Rate limiting (in-memory, per-sender) ---
_rate_counts: dict[str, list[float]] = defaultdict(list)
_RATE_LIMIT = 10  # max msgs per second per sender


def _check_rate(sender: str) -> None:
    now = time.monotonic()
    window = _rate_counts[sender]
    # Prune entries older than 1s
    _rate_counts[sender] = [t for t in window if now - t < 1.0]
    if len(_rate_counts[sender]) >= _RATE_LIMIT:
        raise HTTPException(status_code=429, detail="Rate limit exceeded (10 msg/s)")
    _rate_counts[sender].append(now)


# --- SSE client registry ---
_sse_clients: set[asyncio.Queue] = set()


def _hydrate_meta(entry: dict) -> dict:
    """Parse stringified _meta back into a dict for consumers (SSE / HTTP)."""
    raw = entry.get("_meta")
    if not raw or not isinstance(raw, str):
        return entry
    try:
        entry["_meta"] = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        pass
    return entry


def sse_broadcast(data: dict) -> None:
    """Push a message to all SSE clients. Drop slow clients."""
    payload = json.dumps(_hydrate_meta(dict(data)), default=str)
    message = f"data: {payload}\n\n"
    dead: set[asyncio.Queue] = set()
    for q in _sse_clients:
        try:
            q.put_nowait(message)
        except asyncio.QueueFull:
            dead.add(q)
    _sse_clients.difference_update(dead)


# --- Health ---


@router.get("/health")
async def health(request: Request):
    redis = request.app.state.redis
    try:
        await redis.ping()
        topics = await redis.scard(config.topics_key)
        return {"status": "ok", "redis": True, "active_topics": topics}
    except Exception:
        return {"status": "degraded", "redis": False, "active_topics": 0}


# --- Send message ---


@router.post("/api/messages")
async def send_message(request: Request, _=Depends(require_auth)):
    body = await request.json()
    topic = body.get("topic", "").strip()
    text = body.get("text", "").strip()
    sender = body.get("sender", "anon").strip()
    tag = body.get("tag", "").strip() or None
    priority = body.get("priority", "normal").strip()
    meta = body.get("_meta")

    if not topic or not text:
        raise HTTPException(status_code=400, detail="topic and text required")
    if len(topic) > 100 or len(text) > 4096:
        raise HTTPException(status_code=400, detail="topic max 100, text max 4096 chars")
    if priority not in ("normal", "high"):
        raise HTTPException(status_code=400, detail="priority must be normal or high")

    _check_rate(sender)

    redis = request.app.state.redis
    stream_key = f"{config.stream_prefix}{topic}"

    entry: dict[str, str] = {
        "sender": sender,
        "text": text,
        "priority": priority,
    }
    if tag:
        entry["tag"] = tag
    if isinstance(meta, dict) and meta:
        meta_json = json.dumps(meta, default=str)
        if len(meta_json) > 8192:
            raise HTTPException(status_code=400, detail="_meta max 8192 chars")
        entry["_meta"] = meta_json

    # XADD with inline MAXLEN trim (first line of defense)
    msg_id = await redis.xadd(stream_key, entry, maxlen=config.max_stream_len, approximate=True)
    # Track topic in set (avoid SCAN)
    await redis.sadd(config.topics_key, topic)

    # Broadcast to SSE clients
    sse_broadcast(
        {
            "id": msg_id,
            "topic": topic,
            **entry,
        }
    )

    return {"ok": True, "id": msg_id, "topic": topic}


# --- Read messages ---


@router.get("/api/messages/{topic}")
async def read_messages(
    request: Request,
    topic: str,
    since: str = "0-0",
    count: int = Query(default=50, le=200),
    order: str = Query(default="oldest", pattern="^(oldest|newest)$"),
    _=Depends(require_auth),
):
    """Read messages on a topic.

    - `order=oldest` (default): xrange from `since` (cursor-friendly, used by
      hook inbox to read new messages since last cursor).
    - `order=newest`: xrevrange from end, returns the N most-recent messages
      (human-friendly, used by CLI `channel read` so users see latest first).
    """
    redis = request.app.state.redis
    stream_key = f"{config.stream_prefix}{topic}"

    if order == "newest":
        raw = await redis.xrevrange(stream_key, count=count)
        raw = list(reversed(raw))  # keep chronological order in payload
    else:
        raw = await redis.xrange(stream_key, min=since, count=count)
    messages = []
    for msg_id, fields in raw:
        entry = {"id": msg_id, "topic": topic, **fields}
        messages.append(_hydrate_meta(entry))
    return {"messages": messages, "count": len(messages)}


# --- Active agents (reduced view of `agents` topic) ---


@router.get("/api/agents/active")
async def list_active_agents(
    request: Request,
    within: int = Query(default=300, ge=10, le=3600, description="Look-back window in seconds"),
    _=Depends(require_auth),
):
    """Return latest snapshot per agent (keyed by _meta.host:_meta.pane).

    Reads the `agents` Redis stream within the look-back window, applies a
    last-write-wins reduce per agent key, and drops entries marked tag=leave.
    """
    redis = request.app.state.redis
    stream_key = f"{config.stream_prefix}agents"

    min_ts_ms = int((time.time() - within) * 1000)
    min_id = f"{min_ts_ms}-0"
    raw = await redis.xrange(stream_key, min=min_id, count=2000)

    seen: dict[str, dict] = {}
    for msg_id, fields in raw:
        meta_raw = fields.get("_meta", "")
        meta: dict = {}
        if meta_raw:
            try:
                meta = json.loads(meta_raw)
            except (json.JSONDecodeError, TypeError):
                meta = {}
        host = str(meta.get("host", "?"))
        pane = str(meta.get("pane", fields.get("sender", "?")))
        key = f"{host}:{pane}"

        try:
            ts_ms = int(str(msg_id).split("-")[0])
        except (ValueError, AttributeError):
            ts_ms = 0

        tag = fields.get("tag", "")
        if tag == "leave":
            seen.pop(key, None)
            continue

        seen[key] = {
            "id": msg_id,
            "key": key,
            "ts_ms": ts_ms,
            "last_seen": ts_ms / 1000.0,
            "tag": tag,
            "sender": fields.get("sender", ""),
            "text": fields.get("text", ""),
            "_meta": meta,
        }

    def _sort_key(agent: dict) -> tuple:
        m = agent.get("_meta") or {}
        role_rank = 0 if m.get("role") == "main" else 1
        try:
            ctx = float(m.get("ctx_pct") or 0)
        except (TypeError, ValueError):
            ctx = 0.0
        return (role_rank, -ctx, -agent["ts_ms"])

    agents = sorted(seen.values(), key=_sort_key)
    return {"agents": agents, "count": len(agents), "within": within}


# --- List topics ---


@router.get("/api/topics")
async def list_topics(request: Request, _=Depends(require_auth)):
    redis = request.app.state.redis
    topics = await redis.smembers(config.topics_key)
    result = []
    for t in sorted(topics):
        stream_key = f"{config.stream_prefix}{t}"
        length = await redis.xlen(stream_key)
        if length > 0:
            result.append({"topic": t, "count": length})
        else:
            # Clean up empty topic from set
            await redis.srem(config.topics_key, t)
    return {"topics": result}


# --- SSE stream ---


@router.get("/api/stream")
async def sse_stream(
    request: Request,
    topic: str = Query(default="", description="Filter by topic (empty=all)"),
    _=Depends(require_auth),
):
    queue: asyncio.Queue = asyncio.Queue(maxsize=200)
    _sse_clients.add(queue)

    async def event_generator():
        try:
            while True:
                try:
                    msg = await asyncio.wait_for(queue.get(), timeout=30)
                    # Optional topic filter
                    if topic:
                        data = json.loads(msg.split("data: ", 1)[1].rsplit("\n", 2)[0])
                        if data.get("topic") != topic:
                            continue
                    yield msg
                except TimeoutError:
                    yield ": keepalive\n\n"
        except asyncio.CancelledError:
            pass
        finally:
            _sse_clients.discard(queue)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
