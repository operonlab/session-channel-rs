"""Session Channel — Cross-session communication via Redis Streams."""

from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager
from pathlib import Path

import redis.asyncio as aioredis
from board_routes import board_router
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, Response
from metrics import (
    CONTENT_TYPE_LATEST,
    DEAD_LETTER_TOTAL,
    LEASE_EXPIRED_TOTAL,
    ORPHAN_RECOVERED_TOTAL,
    metrics_response_body,
)
from pane_routes import pane_router
from starlette.middleware.cors import CORSMiddleware

from config import config
from routes import router, sse_broadcast
from sdk_client.station_bootstrap import setup_logging

logger = setup_logging("session-channel")
_TEMPLATE_PATH = Path(__file__).parent / "templates" / "index.html"


async def _trim_loop(r: aioredis.Redis) -> None:
    """Background trimmer: remove messages older than TTL."""
    while True:
        try:
            await asyncio.sleep(config.trim_interval)
            min_id = f"{int((time.time() - config.ttl_seconds) * 1000)}-0"
            topics = await r.smembers(config.topics_key)
            trimmed = 0
            for t in topics:
                stream_key = f"{config.stream_prefix}{t}"
                n = await r.xtrim(stream_key, minid=min_id)
                trimmed += n
                # Clean up empty streams
                if await r.xlen(stream_key) == 0:
                    await r.delete(stream_key)
                    await r.srem(config.topics_key, t)
                    # Clean up associated board claims hash
                    if t.startswith("board:"):
                        board_id = t.removeprefix("board:")
                        await r.delete(f"ws:board:claims:{board_id}")
            if trimmed:
                logger.info("trimmed %d messages", trimmed)
        except asyncio.CancelledError:
            break
        except Exception:
            logger.exception("trim_loop_error")
            await asyncio.sleep(10)


async def _reaper_loop(r: aioredis.Redis) -> None:
    """Background reaper: XAUTOCLAIM idle > lease pending entries, then re-publish.

    For each board topic, run XAUTOCLAIM per task_class bucket using its
    class-specific min_idle_time. Reaped publish entries are XACK'd and re-XADD'd
    with incremented retry_count. Entries exceeding retry > 3 are moved to a
    dead-letter stream (W4-C). Non-publish tags (done/drop/heartbeat) are
    just XACK'd to drain them from the PEL.
    """
    # Lazy import to avoid circular dependency at module load time
    from board_routes import _ensure_group, _group_key, _stream_key
    from schemas import TaskClass, lease_ms_for_class

    last_seen_ids: dict[str, str] = {}  # per (stream, class) cursor for XAUTOCLAIM

    while True:
        try:
            await asyncio.sleep(5)
            topics = await r.smembers(config.topics_key)
            board_topics = [t for t in topics if t.startswith("board:")]

            for topic in board_topics:
                board_id = topic.removeprefix("board:")
                sk = _stream_key(board_id)
                gk = _group_key(board_id)

                try:
                    await _ensure_group(r, board_id)
                except Exception:
                    continue

                # Sweep by task_class — each class has its own lease budget
                for tc in TaskClass:
                    min_idle = lease_ms_for_class(tc)
                    cursor_key = f"{sk}:{tc.value}"
                    cursor = last_seen_ids.get(cursor_key, "0-0")

                    try:
                        # Returns (next_cursor, claimed_entries, deleted_ids)
                        next_cursor, claimed, _deleted = await r.xautoclaim(
                            sk,
                            gk,
                            "__reaper",
                            min_idle_time=min_idle,
                            start_id=cursor,
                            count=100,
                        )
                        last_seen_ids[cursor_key] = next_cursor or "0-0"
                    except Exception:
                        logger.exception("xautoclaim_error")
                        continue

                    for msg_id, fields in claimed:
                        tag = fields.get("tag", "")
                        msg_class = fields.get("task_class", TaskClass.SHORT.value)

                        # Only handle entries belonging to this class bucket;
                        # others get re-encountered when their bucket runs.
                        # Entries without task_class default to SHORT.
                        if tag == "publish" and msg_class != tc.value:
                            continue

                        if tag != "publish":
                            # done / drop / heartbeat / etc. — drain from PEL.
                            try:
                                await r.xack(sk, gk, msg_id)
                            except Exception:
                                logger.exception("xack_drain_error")
                            continue

                        # W5-B: orphan recovered (any reaped publish entry)
                        ORPHAN_RECOVERED_TOTAL.labels(board_id=board_id, task_class=tc.value).inc()

                        # publish entry — re-deliver via XACK + XADD
                        retry_count = int(fields.get("retry_count", "0") or "0") + 1
                        new_fields = {
                            **fields,
                            "tag": "publish",
                            "retry_count": str(retry_count),
                        }

                        # W4-C dead-letter on retry > 3
                        if retry_count > 3:
                            try:
                                dl_key = f"{config.stream_prefix}board:{board_id}:failed"
                                await r.xadd(
                                    dl_key,
                                    new_fields,
                                    maxlen=config.max_stream_len * 2,
                                    approximate=True,
                                )
                                await r.xack(sk, gk, msg_id)
                            except Exception:
                                logger.exception("dead_letter_error")
                                continue
                            DEAD_LETTER_TOTAL.labels(board_id=board_id, task_class=tc.value).inc()
                            logger.info(
                                "dead_letter",
                                extra={
                                    "board_id": board_id,
                                    "task_id": msg_id,
                                    "task_class": tc.value,
                                    "retry_count": retry_count,
                                },
                            )
                            sse_broadcast(
                                {
                                    "topic": f"board:{board_id}",
                                    "tag": "failed",
                                    "task_id": msg_id,
                                    "retry_count": retry_count,
                                }
                            )
                            continue

                        # Re-publish: XACK old + XADD new
                        try:
                            await r.xack(sk, gk, msg_id)
                            new_id = await r.xadd(
                                sk,
                                new_fields,
                                maxlen=config.max_stream_len,
                                approximate=True,
                            )
                        except Exception:
                            logger.exception("reaper_republish_error")
                            continue

                        # W5-B: lease expired & recycled (retry < 3 republish)
                        LEASE_EXPIRED_TOTAL.labels(board_id=board_id, task_class=tc.value).inc()
                        logger.info(
                            "lease_expired",
                            extra={
                                "board_id": board_id,
                                "task_id": msg_id,
                                "new_id": new_id,
                                "task_class": tc.value,
                                "retry_count": retry_count,
                            },
                        )

                        sse_broadcast(
                            {
                                "topic": f"board:{board_id}",
                                "tag": "release",
                                "task_id": msg_id,
                                "new_id": new_id,
                                "retry_count": retry_count,
                            }
                        )
        except asyncio.CancelledError:
            break
        except Exception:
            logger.exception("reaper_loop_error")
            await asyncio.sleep(5)


async def _fanout_loop(r: aioredis.Redis) -> None:
    """Background XREAD loop: read new messages from all streams and push to SSE clients."""
    last_ids: dict[str, str] = {}
    while True:
        try:
            topics = await r.smembers(config.topics_key)
            if not topics:
                await asyncio.sleep(2)
                continue

            streams = {}
            for t in topics:
                key = f"{config.stream_prefix}{t}"
                streams[key] = last_ids.get(key, "$")

            result = await r.xread(streams, count=50, block=2000)
            if not result:
                continue

            for stream_key, entries in result:
                # stream_key may be bytes or str depending on decode_responses
                sk = stream_key if isinstance(stream_key, str) else stream_key.decode()
                topic = sk.removeprefix(config.stream_prefix)
                for msg_id, fields in entries:
                    last_ids[sk] = msg_id
                    sse_broadcast({"id": msg_id, "topic": topic, **fields})
        except asyncio.CancelledError:
            break
        except Exception:
            logger.exception("fanout_loop_error")
            await asyncio.sleep(5)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup: connect Redis, start background tasks."""
    r = aioredis.from_url(config.redis_url, decode_responses=True, socket_connect_timeout=3)
    try:
        await r.ping()
        logger.info("redis_connected: %s", config.redis_url)
    except Exception:
        logger.warning("redis_unavailable: %s", config.redis_url)

    app.state.redis = r

    # Wire reactive store
    from store import channel_store

    app.state.store = channel_store

    trim_task = asyncio.create_task(_trim_loop(r))
    # Note: fanout_loop is only needed if SSE clients connect without going through
    # the send endpoint (e.g., messages sent directly via redis-cli).
    # Since send_message already calls sse_broadcast, the fanout loop handles
    # external writes and catch-up on restart.
    fanout_task = asyncio.create_task(_fanout_loop(r))
    reaper_task = asyncio.create_task(_reaper_loop(r))

    yield

    trim_task.cancel()
    fanout_task.cancel()
    reaper_task.cancel()
    await asyncio.gather(trim_task, fanout_task, reaper_task, return_exceptions=True)
    await r.aclose()
    logger.info("shutdown_complete")


app = FastAPI(title="Session Channel", version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",
        f"http://localhost:{config.port}",
        "https://workshop.joneshong.com",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(router)
app.include_router(board_router)
app.include_router(pane_router)


@app.get("/metrics", include_in_schema=False)
async def metrics_endpoint():
    """Prometheus scrape endpoint (W5-B)."""
    return Response(content=metrics_response_body(), media_type=CONTENT_TYPE_LATEST)


@app.get("/", response_class=HTMLResponse)
async def dashboard():
    return HTMLResponse(_TEMPLATE_PATH.read_text())


def cli():
    import uvicorn

    uvicorn.run("main:app", host=config.host, port=config.port, reload=False, log_level="info")


if __name__ == "__main__":
    cli()
