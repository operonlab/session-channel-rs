"""Session Channel API routes."""

from __future__ import annotations

import asyncio
import json
import time
from collections import defaultdict

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from auth import require_auth
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


def sse_broadcast(data: dict) -> None:
    """Push a message to all SSE clients. Drop slow clients."""
    payload = json.dumps(data, default=str)
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

    # XADD with inline MAXLEN trim (first line of defense)
    msg_id = await redis.xadd(
        stream_key, entry, maxlen=config.max_stream_len, approximate=True
    )
    # Track topic in set (avoid SCAN)
    await redis.sadd(config.topics_key, topic)

    # Broadcast to SSE clients
    sse_broadcast({
        "id": msg_id,
        "topic": topic,
        **entry,
    })

    return {"ok": True, "id": msg_id, "topic": topic}


# --- Read messages ---


@router.get("/api/messages/{topic}")
async def read_messages(
    request: Request,
    topic: str,
    since: str = "0-0",
    count: int = Query(default=50, le=200),
    _=Depends(require_auth),
):
    redis = request.app.state.redis
    stream_key = f"{config.stream_prefix}{topic}"

    raw = await redis.xrange(stream_key, min=since, count=count)
    messages = []
    for msg_id, fields in raw:
        messages.append({"id": msg_id, "topic": topic, **fields})
    return {"messages": messages, "count": len(messages)}


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
                except asyncio.TimeoutError:
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
