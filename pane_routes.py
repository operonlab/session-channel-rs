"""Pane Capability Registry — advertise/list pane capabilities.

Stores per-pane capability snapshot in Redis Hash `ws:panes:{pane_id}` with
an integer 5-minute TTL. Hook handlers POST advertisements on SessionStart
and DELETE on Stop; the Board API consumes the registry to enforce
capability-aware claim routing (Wave 3).

Schema (until W1-B lands `schemas.py`):

    {
        "pane_id":   str,            # e.g. "%5", "pane-5", "sdk-12345"
        "cli_type":  str,            # "claude-code" | "codex" | "gemini" | "unknown"
        "mcps":      list[str],      # ["memvault", "intelflow", ...]
        "skills":    list[str],      # ["forge", "blueprint", ...]
        "started_at": int,           # unix ts
        "last_seen":  int,           # unix ts
    }
"""

from __future__ import annotations

import json
import time

from auth import require_auth
from fastapi import APIRouter, Depends, HTTPException, Request

pane_router = APIRouter(tags=["panes"])

_PANE_PREFIX = "ws:panes:"
_PANE_TTL_SECONDS = 300  # 5 min — heartbeat-driven refresh
_SCAN_COUNT = 100


def _pane_key(pane_id: str) -> str:
    return f"{_PANE_PREFIX}{pane_id}"


def _validate_advertise(body: dict) -> dict:
    """Coerce + validate raw advertise payload (lenient pre-W1-B)."""
    pane_id = str(body.get("pane_id", "")).strip()
    if not pane_id:
        raise HTTPException(status_code=400, detail="pane_id required")
    if len(pane_id) > 128:
        raise HTTPException(status_code=400, detail="pane_id max 128 chars")

    cli_type = str(body.get("cli_type", "unknown")).strip() or "unknown"

    mcps_raw = body.get("mcps", []) or []
    skills_raw = body.get("skills", []) or []
    if not isinstance(mcps_raw, list) or not isinstance(skills_raw, list):
        raise HTTPException(status_code=400, detail="mcps/skills must be lists")

    mcps = [str(m).strip() for m in mcps_raw if str(m).strip()][:200]
    skills = [str(s).strip() for s in skills_raw if str(s).strip()][:500]

    now = int(time.time())
    started_at = int(body.get("started_at") or now)
    last_seen = int(body.get("last_seen") or now)

    return {
        "pane_id": pane_id,
        "cli_type": cli_type,
        "mcps": mcps,
        "skills": skills,
        "started_at": started_at,
        "last_seen": last_seen,
    }


def _serialize_fields(record: dict) -> dict[str, str]:
    """Encode list/int fields as JSON strings for Redis Hash storage."""
    return {
        "pane_id": record["pane_id"],
        "cli_type": record["cli_type"],
        "mcps": json.dumps(record["mcps"], ensure_ascii=False),
        "skills": json.dumps(record["skills"], ensure_ascii=False),
        "started_at": str(record["started_at"]),
        "last_seen": str(record["last_seen"]),
    }


def _deserialize_fields(fields: dict) -> dict:
    """Decode Hash fields back to typed structure. Robust to legacy/missing keys."""
    if not fields:
        return {}

    def _list(value: str | None) -> list[str]:
        if not value:
            return []
        try:
            data = json.loads(value)
            return [str(x) for x in data] if isinstance(data, list) else []
        except (ValueError, TypeError):
            return []

    def _int(value: str | None) -> int:
        try:
            return int(value) if value is not None else 0
        except (ValueError, TypeError):
            return 0

    return {
        "pane_id": fields.get("pane_id", ""),
        "cli_type": fields.get("cli_type", "unknown"),
        "mcps": _list(fields.get("mcps")),
        "skills": _list(fields.get("skills")),
        "started_at": _int(fields.get("started_at")),
        "last_seen": _int(fields.get("last_seen")),
    }


# --- Endpoints ---


@pane_router.post("/api/panes/advertise")
async def advertise_pane(request: Request, _=Depends(require_auth)) -> dict:
    """Upsert pane capability record. Refreshes 5-min TTL on every call."""
    body = await request.json()
    record = _validate_advertise(body)

    redis = request.app.state.redis
    key = _pane_key(record["pane_id"])
    fields = _serialize_fields(record)

    try:
        async with redis.pipeline(transaction=False) as pipe:
            pipe.delete(key)  # clear stale fields if cli_type/mcps shrunk
            pipe.hset(key, mapping=fields)
            pipe.expire(key, _PANE_TTL_SECONDS)
            await pipe.execute()
    except Exception as exc:  # pragma: no cover — Redis hiccup
        raise HTTPException(status_code=503, detail=f"redis_unavailable: {exc}") from exc

    return {"status": "ok", "pane_id": record["pane_id"], "ttl": _PANE_TTL_SECONDS}


@pane_router.get("/api/panes")
async def list_panes(request: Request, _=Depends(require_auth)) -> dict:
    """List all live pane capability records (TTL-bound)."""
    redis = request.app.state.redis
    results: list[dict] = []
    cursor = 0
    seen = 0

    while True:
        cursor, keys = await redis.scan(cursor=cursor, match=f"{_PANE_PREFIX}*", count=_SCAN_COUNT)
        for key in keys:
            fields = await redis.hgetall(key)
            record = _deserialize_fields(fields)
            if record:
                results.append(record)
            seen += 1
        if cursor == 0:
            break

    return {"panes": results, "count": len(results), "scanned": seen}


@pane_router.get("/api/panes/{pane_id}")
async def get_pane(pane_id: str, request: Request, _=Depends(require_auth)) -> dict:
    """Fetch a single pane capability record. 404 if expired or never advertised."""
    redis = request.app.state.redis
    key = _pane_key(pane_id)
    fields = await redis.hgetall(key)
    if not fields:
        raise HTTPException(status_code=404, detail=f"pane not found: {pane_id}")
    return _deserialize_fields(fields)


@pane_router.delete("/api/panes/{pane_id}")
async def release_pane(pane_id: str, request: Request, _=Depends(require_auth)) -> dict:
    """Voluntary offline (called from Stop hook). Idempotent."""
    redis = request.app.state.redis
    key = _pane_key(pane_id)
    deleted = await redis.delete(key)
    return {"status": "ok", "pane_id": pane_id, "deleted": bool(deleted)}
