"""Session Channel — Cross-session communication via Redis Streams."""

from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager
from pathlib import Path

import redis.asyncio as aioredis
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from starlette.middleware.cors import CORSMiddleware
from sdk_client.station_bootstrap import setup_logging

from config import config
from routes import router, sse_broadcast

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
            if trimmed:
                logger.info("trimmed %d messages", trimmed)
        except asyncio.CancelledError:
            break
        except Exception:
            logger.exception("trim_loop_error")
            await asyncio.sleep(10)


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
    r = aioredis.from_url(
        config.redis_url, decode_responses=True, socket_connect_timeout=3
    )
    try:
        await r.ping()
        logger.info("redis_connected: %s", config.redis_url)
    except Exception:
        logger.warning("redis_unavailable: %s", config.redis_url)

    app.state.redis = r

    trim_task = asyncio.create_task(_trim_loop(r))
    # Note: fanout_loop is only needed if SSE clients connect without going through
    # the send endpoint (e.g., messages sent directly via redis-cli).
    # Since send_message already calls sse_broadcast, the fanout loop handles
    # external writes and catch-up on restart.
    fanout_task = asyncio.create_task(_fanout_loop(r))

    yield

    trim_task.cancel()
    fanout_task.cancel()
    await asyncio.gather(trim_task, fanout_task, return_exceptions=True)
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


@app.get("/", response_class=HTMLResponse)
async def dashboard():
    return HTMLResponse(_TEMPLATE_PATH.read_text())


def cli():
    import uvicorn

    uvicorn.run(
        "main:app", host=config.host, port=config.port, reload=False, log_level="info"
    )


if __name__ == "__main__":
    cli()
