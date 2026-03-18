"""Workshop cookie auth middleware — validates workshop_session cookie."""

from __future__ import annotations

from fastapi import HTTPException, Request
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from config import config

_serializer = URLSafeTimedSerializer(config.secret_key)


async def require_auth(request: Request) -> dict:
    """FastAPI dependency — validate workshop_session cookie or X-Local-Key header."""
    local_key = request.headers.get("x-local-key")
    if local_key and local_key == config.secret_key:
        return {"status": "active", "source": "local-key"}

    cookie = request.cookies.get(config.session_cookie_name)
    if not cookie:
        raise HTTPException(status_code=401, detail="Not authenticated")

    try:
        data = _serializer.loads(cookie, max_age=config.session_max_age)
    except (BadSignature, SignatureExpired):
        raise HTTPException(status_code=401, detail="Session expired")

    if isinstance(data, str):
        return {"status": "active", "source": "v2-token"}

    user = data.get("user") if isinstance(data, dict) else None
    if not user or user.get("status") != "active":
        raise HTTPException(status_code=401, detail="Invalid session")

    return user
