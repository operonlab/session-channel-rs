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
    body = {
        "topic": args.topic,
        "text": args.message,
        "sender": args.sender or _default_sender(),
        "priority": args.priority,
    }
    if args.tag:
        body["tag"] = args.tag
    r = httpx.post(f"{BASE_URL}/api/messages", json=body, headers=HEADERS, timeout=TIMEOUT)
    if r.status_code == 200:
        d = r.json()
        print(f"✅ [{d.get('topic')}] id={d.get('id')}")
    else:
        print(f"❌ {r.status_code}: {r.text}", file=sys.stderr)
        sys.exit(1)


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


def cmd_board_show(args):
    r = httpx.get(f"{BASE_URL}/api/board/{args.board_id}", headers=HEADERS, timeout=TIMEOUT)
    if r.status_code == 404:
        print(f"❌ Board '{args.board_id}' not found", file=sys.stderr)
        sys.exit(1)
    d = r.json()
    s = d.get("summary", {})
    print(f"Board: {d.get('board_id')}  ({s.get('total', 0)} tasks)")
    print(f"  open={s.get('open', 0)}  claimed={s.get('claimed', 0)}  done={s.get('done', 0)}")
    print()
    for t in d.get("tasks", []):
        icon = {"open": "○", "claimed": "◌", "done": "●"}.get(t["status"], "?")
        extra = ""
        if t.get("claimed_by"):
            extra = f"  ← {t['claimed_by']}"
        elif t.get("done_by"):
            extra = f"  ✓ {t['done_by']}"
        print(f"  {icon} {t['id']:>15}  {t['status']:<8} {t['desc']}{extra}")


def cmd_board_claim(args):
    pane = args.pane or _default_sender().replace("pane-", "%")
    r = httpx.post(
        f"{BASE_URL}/api/board/{args.board_id}/claim",
        json={"task_id": args.task_id, "pane": pane},
        headers=HEADERS,
        timeout=TIMEOUT,
    )
    d = r.json()
    if d.get("ok"):
        print(f"✅ Claimed {args.task_id} (pane={pane})")
    else:
        holder = d.get("holder", {})
        print(f"❌ {d.get('reason')}: held by {holder.get('pane', '?')}", file=sys.stderr)
        sys.exit(1)


def cmd_board_drop(args):
    pane = args.pane or _default_sender().replace("pane-", "%")
    r = httpx.post(
        f"{BASE_URL}/api/board/{args.board_id}/drop",
        json={"task_id": args.task_id, "pane": pane},
        headers=HEADERS,
        timeout=TIMEOUT,
    )
    d = r.json()
    if d.get("ok"):
        print(f"✅ Dropped {args.task_id}")
    else:
        print(f"❌ {d.get('reason')}", file=sys.stderr)
        sys.exit(1)


def cmd_board_publish(args):
    import json

    tasks = [{"id": t, "desc": t} for t in args.tasks]
    body = {
        "topic": f"board:{args.board_id}",
        "text": json.dumps({"tasks": tasks}),
        "sender": args.sender or _default_sender(),
        "tag": "publish",
        "priority": "high",
    }
    r = httpx.post(f"{BASE_URL}/api/messages", json=body, headers=HEADERS, timeout=TIMEOUT)
    if r.status_code == 200:
        print(f"✅ Board '{args.board_id}' published ({len(tasks)} tasks)")
    else:
        print(f"❌ {r.status_code}: {r.text}", file=sys.stderr)
        sys.exit(1)


def cmd_board_complete(args):
    import json

    body = {
        "topic": f"board:{args.board_id}",
        "text": json.dumps({"task_id": args.task_id, "result": args.result}),
        "sender": args.sender or _default_sender(),
        "tag": "done",
    }
    r = httpx.post(f"{BASE_URL}/api/messages", json=body, headers=HEADERS, timeout=TIMEOUT)
    if r.status_code == 200:
        print(f"✅ Completed {args.task_id}")
    else:
        print(f"❌ {r.status_code}: {r.text}", file=sys.stderr)
        sys.exit(1)


def _board_dispatch(args):
    cmds = {
        "show": cmd_board_show,
        "claim": cmd_board_claim,
        "drop": cmd_board_drop,
        "publish": cmd_board_publish,
        "complete": cmd_board_complete,
    }
    fn = cmds.get(args.board_cmd)
    if fn:
        fn(args)
    else:
        print("Usage: channel board {show|claim|drop|publish|complete} ...", file=sys.stderr)
        sys.exit(1)


def main():
    p = argparse.ArgumentParser(prog="channel", description="Session Channel CLI")
    sub = p.add_subparsers(dest="cmd")

    sp_send = sub.add_parser("send", help="Send a message")
    sp_send.add_argument("topic")
    sp_send.add_argument("message")
    sp_send.add_argument("--tag", default="")
    sp_send.add_argument("--priority", default="normal", choices=["normal", "high"])
    sp_send.add_argument("--sender", default="")

    sp_read = sub.add_parser("read", help="Read messages from a topic")
    sp_read.add_argument("topic")
    sp_read.add_argument("--count", type=int, default=50)

    sub.add_parser("topics", help="List active topics")
    sub.add_parser("health", help="Check station health")

    # Board subcommands
    sp_board = sub.add_parser("board", help="Task bulletin board")
    board_sub = sp_board.add_subparsers(dest="board_cmd")

    bp_show = board_sub.add_parser("show", help="Show board state")
    bp_show.add_argument("board_id")

    bp_claim = board_sub.add_parser("claim", help="Claim a task")
    bp_claim.add_argument("board_id")
    bp_claim.add_argument("task_id")
    bp_claim.add_argument("--pane", default="")

    bp_drop = board_sub.add_parser("drop", help="Drop a claimed task")
    bp_drop.add_argument("board_id")
    bp_drop.add_argument("task_id")
    bp_drop.add_argument("--pane", default="")

    bp_pub = board_sub.add_parser("publish", help="Publish a board")
    bp_pub.add_argument("board_id")
    bp_pub.add_argument("tasks", nargs="+", help="Task IDs (desc defaults to ID)")
    bp_pub.add_argument("--sender", default="")

    bp_done = board_sub.add_parser("complete", help="Mark task done")
    bp_done.add_argument("board_id")
    bp_done.add_argument("task_id")
    bp_done.add_argument("result", nargs="?", default="done")
    bp_done.add_argument("--sender", default="")

    args = p.parse_args()
    if not args.cmd:
        p.print_help()
        sys.exit(1)

    fn = {
        "send": cmd_send,
        "read": cmd_read,
        "topics": cmd_topics,
        "health": cmd_health,
        "board": _board_dispatch,
    }
    try:
        fn[args.cmd](args)
    except httpx.ConnectError:
        print("❌ Cannot connect to session-channel (is it running?)", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
