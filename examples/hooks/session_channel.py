#!/usr/bin/env python3
"""Sample session-channel hook (Python, single-file, stdlib only).

Wire this into your CLI's hook system so that every session lifecycle event
publishes to session-channel. It is deliberately stdlib-only and self-contained
so you can copy it into any project and adapt it.

Usage (Claude Code-style hook):

    # In ~/.claude/settings.json:
    {
      "hooks": {
        "SessionStart":      [{"command": "/path/to/session_channel.py SessionStart"}],
        "PreToolUse":        [{"command": "/path/to/session_channel.py PreToolUse"}],
        "Stop":              [{"command": "/path/to/session_channel.py Stop"}],
        "SessionEnd":        [{"command": "/path/to/session_channel.py SessionEnd"}],
        "UserPromptSubmit":  [{"command": "/path/to/session_channel.py UserPromptSubmit"}]
      }
    }

Each hook command receives the event-type as argv[1] and a JSON payload on
stdin (whatever your CLI's hook framework supplies — at minimum `cwd`,
`session_id`, and for PreToolUse the `tool_name` + `tool_input`).

UserPromptSubmit additionally prints an "inbox digest" to stdout, which the
CLI (in Claude Code's case) injects into the model's context. Other hook
events emit no output — they only publish to session-channel.

Env vars:
    SESSION_CHANNEL_URL   default http://127.0.0.1:10101
    SESSION_CHANNEL_KEY   default "change-me-in-production"
"""
from __future__ import annotations

import http.client
import json
import os
import socket
import sys
import time
import urllib.parse
from pathlib import Path

# -------------------- constants ---------------------------------------------

BASE_URL = os.environ.get("SESSION_CHANNEL_URL", "http://127.0.0.1:10101")
LOCAL_KEY = os.environ.get("SESSION_CHANNEL_KEY", "change-me-in-production")
INBOX_TOPICS = ["broadcasts", "broadcast", "handoffs", "tasks"]
INBOX_MAX_ITEMS = 6
HEARTBEAT_THROTTLE_SEC = 30
DEBOUNCE_SEC = 60
HTTP_TIMEOUT = 2.0

# -------------------- helpers -----------------------------------------------


def pane_id() -> str:
    p = os.environ.get("TMUX_PANE", "")
    return p.replace("%", "pane-") if p else f"pid-{os.getpid()}"


def pane_safe() -> str:
    p = os.environ.get("TMUX_PANE", "")
    return p.replace("%", "") or str(os.getpid())


def read_stdin_json() -> dict:
    try:
        raw = sys.stdin.read()
        return json.loads(raw) if raw.strip() else {}
    except (json.JSONDecodeError, OSError):
        return {}


def now_unix() -> int:
    return int(time.time())


def short_path(p: str) -> str:
    home = os.path.expanduser("~")
    if home and p.startswith(home):
        p = "~" + p[len(home) :]
    if len(p) > 48:
        p = "…" + p[-47:]
    return p


def read_task_state() -> str:
    """Optional companion: agents can write a one-line task summary to
    /tmp/claude-task-<pane>.txt; we read it on Stop to publish 'done: <task>'."""
    pane = os.environ.get("TMUX_PANE", "")
    if not pane:
        return ""
    f = Path(f"/tmp/claude-task-{pane.replace('%', '')}.txt")
    try:
        return f.read_text().strip()
    except (FileNotFoundError, OSError):
        return ""


def http_send(method: str, path: str, body: dict | None = None) -> dict:
    parsed = urllib.parse.urlparse(BASE_URL)
    conn_cls = (
        http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
    )
    conn = conn_cls(parsed.hostname, parsed.port or 80, timeout=HTTP_TIMEOUT)
    headers = {"x-local-key": LOCAL_KEY}
    payload = b""
    if body is not None:
        headers["content-type"] = "application/json"
        payload = json.dumps(body).encode()
    try:
        conn.request(method, path, body=payload, headers=headers)
        resp = conn.getresponse()
        data = resp.read()
        if resp.status >= 400:
            return {}
        try:
            return json.loads(data)
        except json.JSONDecodeError:
            return {}
    except (OSError, socket.timeout):
        return {}
    finally:
        conn.close()


def publish(topic: str, text: str, tag: str = "", meta: dict | None = None) -> None:
    body = {"topic": topic, "text": text, "sender": pane_id(), "priority": "normal"}
    if tag:
        body["tag"] = tag
    if meta is not None:
        body["_meta"] = meta
    http_send("POST", "/api/messages", body)


def collect_meta(payload: dict) -> dict:
    cwd = payload.get("cwd") or os.getcwd()
    return {
        "v": 1,
        "host": socket.gethostname().split(".", 1)[0],
        "pane": os.environ.get("TMUX_PANE", ""),
        "sid": (payload.get("session_id") or "")[:8],
        "cli": payload.get("cli") or "generic",
        "role": os.environ.get("CC_PANE_ROLE", "main"),
        "cwd": short_path(cwd),
        "task": read_task_state(),
        "ts": now_unix(),
    }


def heartbeat_throttled() -> bool:
    f = Path(f"/tmp/agent-hb-{pane_id()}.ts")
    now = time.time()
    try:
        if now - float(f.read_text().strip()) < HEARTBEAT_THROTTLE_SEC:
            return True
    except (FileNotFoundError, ValueError, OSError):
        pass
    try:
        f.write_text(f"{now:.6f}")
    except OSError:
        pass
    return False


def stop_debounced() -> bool:
    f = Path(f"/tmp/session-channel-stop-debounce-{pane_id()}.ts")
    now = time.time()
    try:
        if now - float(f.read_text().strip()) < DEBOUNCE_SEC:
            return True
    except (FileNotFoundError, ValueError, OSError):
        pass
    try:
        f.write_text(f"{now:.6f}")
    except OSError:
        pass
    return False


def tool_preview(name: str, tool_input: dict | None) -> str:
    if not isinstance(tool_input, dict):
        return ""

    def pick(key: str, max_len: int = 50) -> str:
        v = (tool_input.get(key) or "").strip().replace("\n", " ")
        if max_len > 0 and len(v) > max_len:
            v = v[: max_len - 1] + "…"
        return v

    if name in {"Read", "Write", "Edit", "NotebookEdit"}:
        return short_path(tool_input.get("file_path", ""))
    if name == "Bash":
        return pick("command")
    if name in {"Grep", "Glob"}:
        return pick("pattern")
    if name in {"WebFetch", "WebSearch"}:
        return pick("url") or pick("query")
    if name in {"Task", "Agent"}:
        return pick("description")
    if name == "Skill":
        return pick("skill")
    return ""


# -------------------- event handlers ----------------------------------------


def on_session_start(payload: dict) -> None:
    meta = collect_meta(payload)
    publish("sessions", f"joined — {meta['cwd']}", tag="start", meta=meta)
    publish("agents", f"{meta['cli']}/{meta['role']}", tag="announce", meta=meta)


def on_pre_tool_use(payload: dict) -> None:
    name = payload.get("tool_name") or ""
    if name:
        meta = collect_meta(payload)
        meta["tool_name"] = name
        preview = tool_preview(name, payload.get("tool_input"))
        if preview:
            meta["tool_args_preview"] = preview
            text = f"{name} {preview}"[:80]
        else:
            text = name
        publish("agents", text, tag="tool", meta=meta)
    if not heartbeat_throttled():
        meta = collect_meta(payload)
        publish("agents", f"{meta['cli']}/{meta['role']}", tag="heartbeat", meta=meta)


def on_stop(payload: dict) -> None:
    if not stop_debounced():
        task = read_task_state()
        if task:
            publish("sessions", f"done: {task}", tag="stop", meta=collect_meta(payload))
    if not heartbeat_throttled():
        meta = collect_meta(payload)
        publish("agents", f"{meta['cli']}/{meta['role']}", tag="heartbeat", meta=meta)


def on_session_end(payload: dict) -> None:
    meta = collect_meta(payload)
    publish("agents", f"{meta['cli']}/{meta['role']} leaving", tag="leave", meta=meta)


def on_user_prompt_submit(payload: dict) -> None:
    """Fetch unread inbox items across watched topics and print a digest to
    stdout (Claude Code captures stdout from this hook and injects it into the
    model's context). Fail-open: any error → silent."""
    cursor_path = Path(f"/tmp/channel-cursor-{pane_safe()}.json")
    try:
        cursor = json.loads(cursor_path.read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        cursor = {}

    my_sender = pane_id()
    my_pane = os.environ.get("TMUX_PANE", "")
    items: list[tuple[str, str, str, str]] = []  # (topic, sender, text, tag)

    for topic in INBOX_TOPICS:
        since = cursor.get(topic) or f"{int((time.time() - 3600) * 1000)}-0"
        query = urllib.parse.urlencode({"since": since, "count": 20})
        data = http_send("GET", f"/api/messages/{topic}?{query}")
        msgs = data.get("messages", []) if isinstance(data, dict) else []
        if not msgs:
            continue
        # xrange returns `since` inclusive — drop the head if it matches.
        if msgs and msgs[0].get("id") == since:
            msgs = msgs[1:]
        if msgs:
            cursor[topic] = msgs[-1].get("id", since)
        for m in msgs:
            if m.get("sender") == my_sender:
                continue
            meta = m.get("_meta") or {}
            if topic in {"tasks", "handoffs"}:
                target = meta.get("target_pane", "")
                if target and target not in {my_pane, my_sender}:
                    continue
            text = (m.get("text") or "").replace("\n", " ")
            if len(text) > 120:
                text = text[:120] + "…"
            items.append((topic, m.get("sender", "?"), text, m.get("tag", "")))
            if len(items) >= INBOX_MAX_ITEMS:
                break
        if len(items) >= INBOX_MAX_ITEMS:
            break

    try:
        cursor_path.write_text(json.dumps(cursor))
    except OSError:
        pass

    if not items:
        return

    print(f"📬 session-channel inbox ({len(items)} unread):")
    for topic, sender, text, tag in items:
        tag_part = f" #{tag}" if tag else ""
        print(f"  [{topic}]{tag_part} {sender}: {text}")
    print("(use `channel read <topic>` for full thread)")


# -------------------- entrypoint --------------------------------------------

DISPATCH = {
    "SessionStart": on_session_start,
    "PreToolUse": on_pre_tool_use,
    "Stop": on_stop,
    "SessionEnd": on_session_end,
    "UserPromptSubmit": on_user_prompt_submit,
}


def main() -> int:
    if len(sys.argv) < 2:
        sys.stderr.write("usage: session_channel.py <event-type>\n")
        return 1
    event = sys.argv[1]
    fn = DISPATCH.get(event)
    if fn is None:
        return 0  # unknown event → silent allow
    fn(read_stdin_json())
    return 0


if __name__ == "__main__":
    sys.exit(main())
