"""Board API — task bulletin board built on existing message/topic infrastructure.

Tasks are structured messages in `board:{board_id}` topics.
Claims use a separate Redis Hash with Lua CAS for atomic exactly-once claiming.
"""

from __future__ import annotations

import json
import time

from auth import require_auth
from board_lua import CLAIM_TASK_LUA, DROP_TASK_LUA
from fastapi import APIRouter, Depends, HTTPException, Request

from config import config
from routes import sse_broadcast

board_router = APIRouter(tags=["board"])

# Lua script SHA cache (populated on first call)
_claim_sha: str = ""
_drop_sha: str = ""

_CLAIMS_PREFIX = "ws:board:claims:"


def _stream_key(board_id: str) -> str:
    return f"{config.stream_prefix}board:{board_id}"


def _claims_key(board_id: str) -> str:
    return f"{_CLAIMS_PREFIX}{board_id}"


async def _ensure_scripts(redis) -> None:
    """Load Lua scripts into Redis (idempotent)."""
    global _claim_sha, _drop_sha
    if not _claim_sha:
        _claim_sha = await redis.script_load(CLAIM_TASK_LUA)
    if not _drop_sha:
        _drop_sha = await redis.script_load(DROP_TASK_LUA)


async def _build_projection(redis, board_id: str) -> dict:
    """Read stream + claims hash → produce board state projection."""
    sk = _stream_key(board_id)
    ck = _claims_key(board_id)

    raw_msgs = await redis.xrange(sk)
    claims = await redis.hgetall(ck)

    # Extract tasks from publish messages
    tasks: dict[str, dict] = {}
    for _msg_id, fields in raw_msgs:
        tag = fields.get("tag", "")
        if tag == "publish":
            try:
                payload = json.loads(fields.get("text", "{}"))
                for t in payload.get("tasks", []):
                    tid = t.get("id", "")
                    if tid:
                        tasks[tid] = {"id": tid, "desc": t.get("desc", ""), "status": "open"}
            except (json.JSONDecodeError, TypeError):
                pass
        elif tag == "done":
            try:
                payload = json.loads(fields.get("text", "{}"))
                tid = payload.get("task_id", "")
                if tid in tasks:
                    tasks[tid]["status"] = "done"
                    tasks[tid]["done_by"] = fields.get("sender", "")
            except (json.JSONDecodeError, TypeError):
                pass

    # Overlay claim state from Redis Hash (authoritative for claims)
    for tid, raw_claim in claims.items():
        if tid in tasks and tasks[tid]["status"] == "open":
            try:
                claim_data = json.loads(raw_claim)
                tasks[tid]["status"] = "claimed"
                tasks[tid]["claimed_by"] = claim_data.get("pane", "")
            except (json.JSONDecodeError, TypeError):
                tasks[tid]["status"] = "claimed"

    task_list = sorted(tasks.values(), key=lambda t: t["id"])
    summary = {
        "total": len(task_list),
        "open": sum(1 for t in task_list if t["status"] == "open"),
        "claimed": sum(1 for t in task_list if t["status"] == "claimed"),
        "done": sum(1 for t in task_list if t["status"] == "done"),
    }

    return {"board_id": board_id, "tasks": task_list, "summary": summary}


@board_router.get("/api/board/{board_id}")
async def get_board(request: Request, board_id: str, _=Depends(require_auth)):
    """Read-only projection: stream messages + claims → full board state."""
    redis = request.app.state.redis
    projection = await _build_projection(redis, board_id)
    if not projection["tasks"]:
        raise HTTPException(status_code=404, detail=f"Board '{board_id}' not found or empty")
    return projection


@board_router.post("/api/board/{board_id}/claim")
async def claim_task(request: Request, board_id: str, _=Depends(require_auth)):
    """Atomically claim a task via Lua CAS. Returns the claimed task or failure reason."""
    body = await request.json()
    task_id = body.get("task_id", "").strip()
    pane = body.get("pane", "").strip()

    if not task_id or not pane:
        raise HTTPException(status_code=400, detail="task_id and pane required")

    redis = request.app.state.redis
    await _ensure_scripts(redis)

    ck = _claims_key(board_id)
    now = str(int(time.time()))

    ttl = str(config.ttl_seconds)
    result = await redis.evalsha(_claim_sha, 1, ck, task_id, pane, now, ttl)

    if result is None:
        # Success — nil return means no prior holder
        sk = _stream_key(board_id)
        entry = {
            "sender": pane,
            "text": json.dumps({"task_id": task_id}),
            "tag": "claim",
            "priority": "normal",
        }
        await redis.xadd(sk, entry, maxlen=config.max_stream_len, approximate=True)
        await redis.sadd(config.topics_key, f"board:{board_id}")

        sse_broadcast(
            {
                "topic": f"board:{board_id}",
                "tag": "claim",
                "task_id": task_id,
                "pane": pane,
            }
        )

        return {"ok": True, "task_id": task_id}

    # Conflict — result is the current holder JSON
    try:
        holder = json.loads(result)
    except (json.JSONDecodeError, TypeError):
        holder = {"pane": result}

    return {"ok": False, "reason": "already_claimed", "holder": holder}


@board_router.post("/api/board/{board_id}/drop")
async def drop_task(request: Request, board_id: str, _=Depends(require_auth)):
    """Release a claimed task (only the claimer can drop)."""
    body = await request.json()
    task_id = body.get("task_id", "").strip()
    pane = body.get("pane", "").strip()

    if not task_id or not pane:
        raise HTTPException(status_code=400, detail="task_id and pane required")

    redis = request.app.state.redis
    await _ensure_scripts(redis)

    ck = _claims_key(board_id)

    result = await redis.evalsha(_drop_sha, 1, ck, task_id, pane)

    if result == 1:
        # Write drop event to stream
        sk = _stream_key(board_id)
        entry = {
            "sender": pane,
            "text": json.dumps({"task_id": task_id}),
            "tag": "drop",
            "priority": "normal",
        }
        await redis.xadd(sk, entry, maxlen=config.max_stream_len, approximate=True)

        sse_broadcast(
            {
                "topic": f"board:{board_id}",
                "tag": "drop",
                "task_id": task_id,
                "pane": pane,
            }
        )

        return {"ok": True, "task_id": task_id}

    if result == -1:
        return {"ok": False, "reason": "not_claimed"}
    return {"ok": False, "reason": "not_holder"}
