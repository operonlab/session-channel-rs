#!/Users/joneshong/.local/bin/python3
"""Codex notify hook → publish heartbeat to session-channel.

Codex CLI 0.130.0 has a single hook point: `config.toml.notify` fires on
every agent turn completion. This script replaces (or chains with) oh-my-codex's
notify-hook.js, publishing per-turn heartbeats to session-channel so that
Codex panes appear in `channel agents` alongside Claude panes.

Codex notify payload schema (observed 2026-05):
    {
      "timestamp": "ISO8601",
      "type": "agent-turn-complete",
      "thread_id": "UUIDv7",
      "turn_id":   "UUIDv7",
      "input_preview":  "...",
      "output_preview": "..."
    }
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time

CHANNEL_CLI = "/Users/joneshong/workshop/stations/session-channel/cli/channel.py"


def main() -> int:
    if len(sys.argv) < 2:
        return 0
    raw = sys.argv[-1]
    if raw.startswith("-"):
        return 0
    try:
        payload = json.loads(raw)
    except Exception:
        return 0

    pane = os.environ.get("TMUX_PANE") or f"pid-{os.getpid()}"
    session_id = (
        payload.get("thread_id")
        or payload.get("thread-id")
        or payload.get("session_id")
        or payload.get("session-id")
        or ""
    )
    turn_id = payload.get("turn_id") or payload.get("turn-id") or ""
    event = payload.get("type") or "agent-turn-complete"

    meta = {
        "v": 1,
        "host": socket.gethostname().split(".")[0],
        "pane": pane,
        "cli": "codex",
        "role": os.environ.get("CHANNEL_ROLE", "worker"),
        "ts": int(time.time()),
        "session_id": session_id,
        "turn_id": turn_id,
        "event": event,
    }

    label = session_id[:8] if session_id else pane
    msg = f"codex/{label} turn done"

    try:
        subprocess.run(
            [
                CHANNEL_CLI,
                "send",
                "agents",
                msg,
                "--tag",
                "heartbeat",
                "--meta",
                json.dumps(meta),
            ],
            check=False,
            timeout=5,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
