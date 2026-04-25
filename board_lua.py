"""DEPRECATED in v2 — Board now uses Redis Streams consumer groups (XREADGROUP/XACK).

The original v1 implementation used a custom Lua CAS over an auxiliary
``ws:board:claims:{board_id}`` Hash to enforce exactly-once claims. v2 replaces
this with native Redis Streams consumer-group primitives (XREADGROUP, XACK,
XCLAIM, XAUTOCLAIM), which are atomic, cluster-safe, and built-in.

This file is kept as a placeholder so that any lingering imports do not break
during the migration window. It will be removed once BOARD_V2 stabilizes.
"""

from __future__ import annotations

import warnings

warnings.warn(
    "board_lua is deprecated; use Streams consumer group APIs in board_routes.py",
    DeprecationWarning,
    stacklevel=2,
)

# Empty placeholders for legacy imports — do not use.
CLAIM_TASK_LUA = ""
DROP_TASK_LUA = ""
