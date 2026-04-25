"""Pytest fixtures for session-channel board v2 tests.

Run:
    cd /Users/joneshong/workshop/.claude/worktrees/feature+session-channel-team-parity-v2
    ~/.local/bin/python3 -m pytest stations/session-channel/tests/ -v
    # skip stress tests
    ~/.local/bin/python3 -m pytest stations/session-channel/tests/ -v -m "not slow"

Optional deps (tests skip cleanly if missing):
    pip install fakeredis freezegun pytest-asyncio
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

try:
    import pytest_asyncio
except ImportError:  # pragma: no cover
    pytest_asyncio = None  # type: ignore[assignment]

# Ensure station + libs are importable
ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "stations" / "session-channel"))
sys.path.insert(0, str(ROOT / "libs" / "sdk-client"))


def pytest_configure(config):
    """Register custom marks to silence PytestUnknownMarkWarning."""
    config.addinivalue_line("markers", "slow: mark test as slow (stress / 1k+ ops)")


@pytest.fixture
def board_id():
    """Unique board id per test."""
    import uuid

    return f"test-{uuid.uuid4().hex[:8]}"


# Use pytest_asyncio.fixture so STRICT mode picks it up; fall back to pytest.fixture
# if pytest-asyncio is missing (the dependent tests will skip via the import guard).
_async_fixture = pytest_asyncio.fixture if pytest_asyncio is not None else pytest.fixture


@_async_fixture
async def fake_redis():
    """In-memory Redis fake using fakeredis (asyncio)."""
    try:
        import fakeredis.aioredis as fakeredis_aio
    except ImportError:
        pytest.skip("fakeredis not installed; pip install fakeredis to run")
    r = fakeredis_aio.FakeRedis(decode_responses=True)
    try:
        yield r
    finally:
        try:
            await r.aclose()
        except Exception:
            pass


@pytest.fixture
def fake_clock():
    """freezegun helper — returns the freeze_time callable."""
    try:
        from freezegun import freeze_time
    except ImportError:
        pytest.skip("freezegun not installed")
    return freeze_time
