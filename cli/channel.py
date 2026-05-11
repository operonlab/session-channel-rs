#!/Users/joneshong/.local/bin/python3
"""Session Channel CLI — send/read messages across Claude Code sessions."""

from __future__ import annotations

import argparse
import os
import sys

import httpx

BASE_URL = os.environ.get("SESSION_CHANNEL_URL", "http://localhost:10101")
LOCAL_KEY = os.environ.get("SESSION_CHANNEL_KEY", "change-me-in-production")
HEADERS = {"x-local-key": LOCAL_KEY, "Content-Type": "application/json"}
TIMEOUT = 10


def _default_sender() -> str:
    pane = os.environ.get("TMUX_PANE", "")
    if pane:
        return pane.replace("%", "pane-")
    return f"cli-{os.getpid()}"


def cmd_health(_args):
    r = httpx.get(f"{BASE_URL}/health", timeout=TIMEOUT)
    d = r.json()
    status = "✅" if d.get("redis") else "❌"
    print(f"{status} redis={d.get('redis')}  topics={d.get('active_topics', 0)}")


def cmd_send(args):
    import json as _json

    body = {
        "topic": args.topic,
        "text": args.message,
        "sender": args.sender or _default_sender(),
        "priority": args.priority,
    }
    if args.tag:
        body["tag"] = args.tag

    meta_dict: dict = {}
    if args.meta:
        try:
            meta_dict = _json.loads(args.meta)
        except _json.JSONDecodeError as e:
            print(f"❌ --meta must be valid JSON: {e}", file=sys.stderr)
            sys.exit(2)
        if not isinstance(meta_dict, dict):
            print("❌ --meta must be a JSON object (got list/string/etc)", file=sys.stderr)
            sys.exit(2)
        body["_meta"] = meta_dict

    r = httpx.post(f"{BASE_URL}/api/messages", json=body, headers=HEADERS, timeout=TIMEOUT)
    if r.status_code == 200:
        d = r.json()
        print(f"✅ [{d.get('topic')}] id={d.get('id')}")
    else:
        print(f"❌ {r.status_code}: {r.text}", file=sys.stderr)
        sys.exit(1)

    # Optional push: nudge target pane(s) via tmux send-keys so they don't have
    # to wait for the next user prompt. The message is published either way;
    # this just shortens the latency at the cost of typing into target pane.
    if args.notify:
        targets = []
        explicit = args.notify_target or meta_dict.get("target_pane")
        if explicit:
            targets = [explicit]
        else:
            # Fan out to all currently-active worker panes (excluding self)
            try:
                ar = httpx.get(
                    f"{BASE_URL}/api/agents/active?within=300",
                    headers=HEADERS,
                    timeout=TIMEOUT,
                )
                self_pane = os.environ.get("TMUX_PANE", "")
                for a in (ar.json() or {}).get("agents", []):
                    m = a.get("_meta") or {}
                    pane = m.get("pane")
                    if not pane or pane == self_pane:
                        continue
                    if m.get("role") == "worker":
                        targets.append(pane)
            except (httpx.HTTPError, ValueError):
                pass

        if not targets:
            print("⚠️  --notify: no target pane(s) found, skipping push", file=sys.stderr)
            return

        for pane in targets:
            _tmux_nudge(pane, args.topic)


def _tmux_nudge(pane: str, topic: str) -> None:
    """Push a wakeup line into the target pane via tmux send-keys.

    Sends the literal text + a real Enter (C-m). Prints a one-line summary
    so the caller knows the push happened.
    """
    import subprocess

    if not pane.startswith("%"):
        pane = "%" + pane.lstrip("%")
    wakeup = f"/channel read {topic} --count 5"
    try:
        # send the prompt text
        subprocess.run(  # noqa: S603
            ["tmux", "send-keys", "-t", pane, wakeup],  # noqa: S607
            check=True,
            timeout=2,
            stderr=subprocess.PIPE,
        )
        # send Enter (separate call to avoid escape interpretation in payload)
        subprocess.run(  # noqa: S603
            ["tmux", "send-keys", "-t", pane, "Enter"],  # noqa: S607
            check=True,
            timeout=2,
            stderr=subprocess.PIPE,
        )
        print(f"📤 push → {pane}: {wakeup}")
    except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
        print(f"⚠️  push to {pane} failed: {e}", file=sys.stderr)


def cmd_read(args):
    params = {"count": args.count}
    r = httpx.get(
        f"{BASE_URL}/api/messages/{args.topic}",
        params=params,
        headers=HEADERS,
        timeout=TIMEOUT,
    )
    d = r.json()
    for m in d.get("messages", []):
        tag = f" #{m['tag']}" if m.get("tag") else ""
        pri = " ⚡" if m.get("priority") == "high" else ""
        print(f"  {m.get('sender', '?'):>10} │ {m.get('text', '')}{tag}{pri}")
    print(f"--- {d.get('count', 0)} messages ---")


def cmd_topics(_args):
    r = httpx.get(f"{BASE_URL}/api/topics", headers=HEADERS, timeout=TIMEOUT)
    d = r.json()
    if not d.get("topics"):
        print("  (no active topics)")
        return
    for t in d["topics"]:
        print(f"  {t['topic']:>20}  {t['count']} msgs")


_CLI_ICON = {"claude": "🔷", "codex": "🔶", "gemini": "💎"}


def _live_panes() -> set[str]:
    """Best-effort: return the set of currently-attached tmux pane ids ('%23')."""
    try:
        import subprocess

        out = subprocess.check_output(
            ["tmux", "list-panes", "-aF", "#{pane_id}"],  # noqa: S607
            text=True,
            timeout=1,
            stderr=subprocess.DEVNULL,
        )
        return {line.strip() for line in out.splitlines() if line.strip()}
    except (FileNotFoundError, OSError, subprocess.SubprocessError):
        return set()


def _fmt_age(last_seen: float) -> str:
    import time as _t

    age = max(0, int(_t.time() - last_seen))
    if age < 60:
        return f"{age}s"
    if age < 3600:
        return f"{age // 60}m"
    return f"{age // 3600}h"


def cmd_agents(args):
    """List currently-active agents (panes) reduced from the agents topic."""
    r = httpx.get(
        f"{BASE_URL}/api/agents/active",
        params={"within": args.within},
        headers=HEADERS,
        timeout=TIMEOUT,
    )
    d = r.json()
    agents = d.get("agents", [])
    if not agents:
        print("  (no active agents in last", args.within, "s)")
        return

    live = _live_panes()
    header = (
        f"  {'icon':<4} {'pane':<14} {'role':<8} {'ctx':<5} "
        f"{'branch':<18} {'task':<28} {'age':>5} {'live':<4}"
    )
    print(header)
    print("  " + "─" * (len(header) - 2))
    for a in agents:
        m = a.get("_meta") or {}
        cli = (m.get("cli") or "?").lower()
        icon = _CLI_ICON.get(cli, "·")
        pane = m.get("pane") or a.get("sender") or "?"
        role = m.get("role") or "?"
        ctx = m.get("ctx_pct")
        ctx_s = f"{round(float(ctx))}%" if isinstance(ctx, (int, float)) else "?"
        branch = (m.get("branch") or "-")[:18]
        task = (m.get("task") or "-")[:28]
        age = _fmt_age(a.get("last_seen") or 0)
        is_live = "✓" if pane in live else "·"
        print(
            f"  {icon:<4} {pane:<14} {role:<8} {ctx_s:<5} "
            f"{branch:<18} {task:<28} {age:>5} {is_live:<4}"
        )
    print(f"--- {d.get('count', 0)} agents in last {d.get('within', args.within)}s ---")


def main():
    p = argparse.ArgumentParser(prog="channel", description="Session Channel CLI")
    sub = p.add_subparsers(dest="cmd")

    sp_send = sub.add_parser("send", help="Send a message")
    sp_send.add_argument("topic")
    sp_send.add_argument("message")
    sp_send.add_argument("--tag", default="")
    sp_send.add_argument("--priority", default="normal", choices=["normal", "high"])
    sp_send.add_argument("--sender", default="")
    sp_send.add_argument("--meta", default="", help="JSON object attached as _meta")
    sp_send.add_argument(
        "--notify",
        action="store_true",
        help="After publishing, push a wakeup line into target pane via tmux send-keys",
    )
    sp_send.add_argument(
        "--notify-target",
        default="",
        help=(
            "Pane id (e.g. %%23) to push to; default: meta.target_pane, "
            "else all active worker panes"
        ),
    )

    sp_read = sub.add_parser("read", help="Read messages from a topic")
    sp_read.add_argument("topic")
    sp_read.add_argument("--count", type=int, default=50)

    sub.add_parser("topics", help="List active topics")
    sub.add_parser("health", help="Check station health")

    sp_agents = sub.add_parser("agents", help="List active agents (panes)")
    sp_agents.add_argument(
        "--within",
        type=int,
        default=300,
        help="Look-back window in seconds (default: 300)",
    )

    args = p.parse_args()
    if not args.cmd:
        p.print_help()
        sys.exit(1)

    fn = {
        "send": cmd_send,
        "read": cmd_read,
        "topics": cmd_topics,
        "health": cmd_health,
        "agents": cmd_agents,
    }
    try:
        fn[args.cmd](args)
    except httpx.ConnectError:
        print("❌ Cannot connect to session-channel (is it running?)", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
