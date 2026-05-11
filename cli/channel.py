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
    # `--notify-target X` implicitly turns push on (saves a flag).
    if args.notify or args.notify_target:
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

        # For `tasks` topic with an explicit prompt in _meta, push the prompt
        # itself as the wakeup — much more reliable than asking Claude to
        # re-discover the task via `channel read`. Other topics still get
        # the read-style nudge.
        task_id = ""
        task_prompt = ""
        if args.topic == "tasks" and isinstance(meta_dict, dict):
            task_id = str(meta_dict.get("task_id") or "")
            task_prompt = str(meta_dict.get("prompt") or "")

        for pane in targets:
            _tmux_nudge(pane, args.topic, task_id=task_id, task_prompt=task_prompt)


def _tmux_nudge(pane: str, topic: str, task_id: str = "", task_prompt: str = "") -> None:
    """Push a wakeup line into the target pane via tmux send-keys.

    Sends the literal text + a real Enter (C-m). Prints a one-line summary
    so the caller knows the push happened.

    Modes:
    - `tasks` topic + non-empty `task_prompt`: push the prompt itself as the
      user-ask, with a tail asking the worker to report done via channel.
      This works for a Claude Code pane (treated as a real user prompt) and
      for a zsh pane (the prompt is presumed to be a shell command — though
      zsh won't auto-report done; the human starting zsh worker accepts that).
    - other topics: push `channel read <topic> --count N` so the recipient
      sees what was published.
    """
    import subprocess

    if not pane.startswith("%"):
        pane = "%" + pane.lstrip("%")

    if topic == "tasks" and task_prompt:
        report_meta = '{"v":1,"task_id":"' + task_id + '","status":"ok","summary":"<one-line>"}'
        fail_meta = '{"v":1,"task_id":"' + task_id + '","error":"<describe what went wrong>"}'
        sender = os.environ.get("TMUX_PANE", "?")
        # Trust marker — tells the worker Claude this push came from
        # session-channel (a user-configured local bus), not from an untrusted
        # external source. Helps the auto-mode classifier route follow-up
        # `channel send` correctly. zsh treats the whole `# …` part as comment.
        trust = f"[session-channel:trusted task={task_id} from={sender}]"
        wakeup = (
            f"{task_prompt}  # {trust} "
            f"on success run: channel send tasks "
            f"\"{task_id}: done\" --tag done --meta '{report_meta}' ; "
            f"on failure run: channel send tasks "
            f"\"{task_id}: failed\" --tag failed --meta '{fail_meta}'"
        )
    elif topic == "tasks":
        wakeup = "channel read tasks --count 10"
    else:
        wakeup = f"channel read {topic} --count 5"
    import time

    try:
        # send the prompt text
        subprocess.run(  # noqa: S603
            ["tmux", "send-keys", "-t", pane, wakeup],  # noqa: S607
            check=True,
            timeout=2,
            stderr=subprocess.PIPE,
        )
        # Brief settle delay before submitting. Claude TUI is unaffected; Codex
        # TUI dropped Enter when fired immediately after the text payload
        # (Phase E validation 2026-05-11), so a small buffer makes the push
        # reliable across all CLIs.
        time.sleep(0.3)
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
    # Default to "newest" so human-facing `channel read` shows the most recent
    # messages first (XRANGE from 0-0 would always return the oldest, which
    # surprised at least one Claude worker — see commit log).
    params = {"count": args.count, "order": "oldest" if args.oldest else "newest"}
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


# ── Task status helpers ───────────────────────────────────────────────────

_TASK_STATUS_ICON = {
    "done": "✅",
    "failed": "❌",
    "timeout": "⏱",
    "pending": "⏳",
}


def _parse_task_id(text: str) -> str:
    """Extract task_id from a tasks message text.

    Convention: messages sent by workers follow '<task_id>: done|failed'.
    assign messages have task_id in _meta.task_id.
    """
    if ": " in text:
        return text.split(": ", 1)[0].strip()
    return ""


def cmd_tasks(args):
    """Show task status summary for the `tasks` topic.

    Reads the entire `tasks` stream (up to --count messages), pairs assign
    messages with their done/failed counterparts by task_id, and marks
    unresolved assigns as pending (or timeout if older than --max-age seconds).

    Retry policy: Strategy A — events only; no automatic retry.
    Callers inspect the output and decide whether to re-dispatch.
    """
    import json as _json
    import time as _t

    max_age = getattr(args, "max_age", 300)
    count = getattr(args, "count", 200)
    show_pending_only = getattr(args, "pending", False)

    params = {"count": count, "order": "oldest"}
    r = httpx.get(
        f"{BASE_URL}/api/messages/tasks",
        params=params,
        headers=HEADERS,
        timeout=TIMEOUT,
    )
    d = r.json()
    messages = d.get("messages", [])

    # --- Reduce: last-write-wins per task_id ---
    assigns: dict[str, dict] = {}  # task_id → assign message
    outcomes: dict[str, dict] = {}  # task_id → done/failed/timeout message

    now = _t.time()

    for m in messages:
        tag = m.get("tag", "")
        text = m.get("text", "")
        meta = m.get("_meta") or {}
        if isinstance(meta, str):
            try:
                meta = _json.loads(meta)
            except (_json.JSONDecodeError, TypeError):
                meta = {}

        # Extract task_id: prefer _meta.task_id, fall back to text prefix
        task_id = str(meta.get("task_id") or _parse_task_id(text) or "")
        if not task_id:
            continue

        # Parse Redis stream id timestamp (milliseconds)
        try:
            ts_ms = int(str(m.get("id", "0")).split("-")[0])
            ts = ts_ms / 1000.0
        except (ValueError, AttributeError):
            ts = 0.0

        if tag == "assign":
            assigns[task_id] = {**m, "_ts": ts, "_meta": meta}
        elif tag in ("done", "failed", "timeout"):
            # last outcome wins (in case of duplicate publish)
            if task_id not in outcomes or ts > outcomes[task_id].get("_ts", 0):
                outcomes[task_id] = {**m, "_ts": ts, "_meta": meta, "_tag": tag}

    # --- Build report rows ---
    rows = []
    for task_id, assign in sorted(assigns.items(), key=lambda x: x[1].get("_ts", 0)):
        a_ts = assign.get("_ts", 0)
        age_s = int(now - a_ts)

        if task_id in outcomes:
            outcome = outcomes[task_id]
            status = outcome["_tag"]
            o_ts = outcome.get("_ts", 0)
            latency_s = int(o_ts - a_ts) if o_ts > a_ts else 0
            extra = ""
            if status == "failed":
                extra = " error=" + str((outcome.get("_meta") or {}).get("error", "?"))[:40]
            rows.append(
                {
                    "task_id": task_id,
                    "status": status,
                    "age_s": age_s,
                    "latency_s": latency_s,
                    "extra": extra,
                    "assign": assign,
                }
            )
        else:
            # Still pending — check if it should be marked timeout
            if age_s > max_age:
                status = "timeout"
                extra = f" (>{max_age}s, no done/failed received)"
            else:
                status = "pending"
                extra = ""
            rows.append(
                {
                    "task_id": task_id,
                    "status": status,
                    "age_s": age_s,
                    "latency_s": -1,
                    "extra": extra,
                    "assign": assign,
                }
            )

    # Filter if --pending
    if show_pending_only:
        rows = [r for r in rows if r["status"] in ("pending", "timeout")]

    if not rows:
        print("  (no tasks found)")
        return

    # --- Print table ---
    header = f"  {'status':<9} {'task_id':<28} {'age':>6} {'latency':>8}  extra"
    print(header)
    print("  " + "─" * (len(header) - 2))
    counts: dict[str, int] = {}
    for row in rows:
        st = row["status"]
        counts[st] = counts.get(st, 0) + 1
        icon = _TASK_STATUS_ICON.get(st, "?")
        lat_s = f"{row['latency_s']}s" if row["latency_s"] >= 0 else "-"
        print(
            f"  {icon} {st:<7} {row['task_id'][:28]:<28} {row['age_s']:>5}s {lat_s:>8}  {row['extra']}"
        )

    summary_parts = [f"{v} {k}" for k, v in sorted(counts.items())]
    print(f"--- {len(rows)} tasks: {', '.join(summary_parts)} ---")

    # --- Auto-publish timeout events for detected timeouts ---
    if getattr(args, "mark_timeout", False):
        import json as _json2

        for row in rows:
            if row["status"] == "timeout":
                tid = row["task_id"]
                assign_meta = row["assign"].get("_meta") or {}
                timeout_meta = _json2.dumps(
                    {
                        "v": 1,
                        "task_id": tid,
                        "reason": f"no done/failed within {max_age}s",
                        "assign_sender": row["assign"].get("sender", "?"),
                    }
                )
                body = {
                    "topic": "tasks",
                    "text": f"{tid}: timeout",
                    "sender": _default_sender(),
                    "tag": "timeout",
                    "_meta": {
                        "v": 1,
                        "task_id": tid,
                        "reason": f"no done/failed within {max_age}s",
                    },
                }
                try:
                    tr = httpx.post(
                        f"{BASE_URL}/api/messages",
                        json=body,
                        headers=HEADERS,
                        timeout=TIMEOUT,
                    )
                    if tr.status_code == 200:
                        print(f"  📢 timeout event published for task {tid}")
                    else:
                        print(
                            f"  ⚠️  failed to publish timeout for {tid}: {tr.status_code}",
                            file=sys.stderr,
                        )
                except httpx.HTTPError as e:
                    print(f"  ⚠️  publish error for {tid}: {e}", file=sys.stderr)


def _parse_workers(spec: str) -> list[tuple[str, str]]:
    """Parse 'claude:%5,codex:%6,gemini:%7' into [(cli, pane), ...].

    pane is normalised so '%5' and '5' both yield '%5'.
    """
    out: list[tuple[str, str]] = []
    for chunk in spec.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if ":" not in chunk:
            raise ValueError(
                f"worker spec '{chunk}' must be 'cli:pane' (e.g. claude:%5)"
            )
        cli, pane = chunk.split(":", 1)
        cli = cli.strip().lower()
        pane = pane.strip()
        if not pane:
            raise ValueError(f"worker spec '{chunk}' missing pane id")
        if not pane.startswith("%"):
            pane = "%" + pane.lstrip("%")
        if not cli:
            raise ValueError(f"worker spec '{chunk}' missing cli name")
        out.append((cli, pane))
    return out


def cmd_race(args):
    """Race the same prompt across N workers; one assign per worker, one
    done-track per worker. Reuses the tasks-topic schema so `channel tasks`
    shows results in the same status table.

    Each worker gets task_id = "<base>-<cli>" so done/failed events match
    cleanly. `_meta.race_base_id` lets downstream tooling filter results.
    """
    import json as _json
    import time as _t

    try:
        workers = _parse_workers(args.workers)
    except ValueError as e:
        print(f"❌ {e}", file=sys.stderr)
        sys.exit(2)
    if not workers:
        print(
            "❌ --workers required, e.g. --workers claude:%5,codex:%6,gemini:%7",
            file=sys.stderr,
        )
        sys.exit(2)

    base_id = args.task_id
    prompt = args.message

    extra_meta: dict = {}
    if args.meta:
        try:
            parsed = _json.loads(args.meta)
            if not isinstance(parsed, dict):
                raise ValueError("--meta must be a JSON object")
            extra_meta = parsed
        except (_json.JSONDecodeError, ValueError) as e:
            print(f"❌ --meta invalid: {e}", file=sys.stderr)
            sys.exit(2)

    task_ids: list[str] = []
    print(f"🏁 race: {len(workers)} worker(s), base_id={base_id}")

    for cli, pane in workers:
        task_id = f"{base_id}-{cli}"
        task_ids.append(task_id)
        meta = {
            "v": 1,
            "task_id": task_id,
            "race_base_id": base_id,
            "race_cli": cli,
            "target_pane": pane,
            "prompt": prompt,
        }
        meta.update(extra_meta)

        body = {
            "topic": "tasks",
            "text": prompt,
            "sender": _default_sender(),
            "priority": "normal",
            "tag": "assign",
            "_meta": meta,
        }
        try:
            r = httpx.post(
                f"{BASE_URL}/api/messages",
                json=body,
                headers=HEADERS,
                timeout=TIMEOUT,
            )
            if r.status_code != 200:
                print(
                    f"  ⚠️  {cli} ({pane}): publish failed HTTP {r.status_code}",
                    file=sys.stderr,
                )
                continue
            rid = r.json().get("id", "?")
            print(f"  ✅ [tasks] {task_id} → {cli} ({pane}) id={rid}")
        except httpx.HTTPError as e:
            print(f"  ⚠️  {cli} ({pane}): publish error {e}", file=sys.stderr)
            continue

        if not args.no_notify:
            _tmux_nudge(pane, "tasks", task_id=task_id, task_prompt=prompt)

    if args.wait <= 0:
        print(
            "\n→ Watch progress: channel tasks --pending  "
            "(or rerun: channel race ... --wait 300)"
        )
        return

    deadline = _t.time() + args.wait
    pending = set(task_ids)
    print(f"\n⏳ waiting up to {args.wait}s for {len(pending)} task(s)...")

    while pending and _t.time() < deadline:
        _t.sleep(5)
        try:
            rr = httpx.get(
                f"{BASE_URL}/api/messages/tasks",
                params={"count": 200, "order": "oldest"},
                headers=HEADERS,
                timeout=TIMEOUT,
            )
            msgs = rr.json().get("messages", [])
        except httpx.HTTPError:
            continue
        for m in msgs:
            mt = m.get("_meta") or {}
            if isinstance(mt, str):
                try:
                    mt = _json.loads(mt)
                except _json.JSONDecodeError:
                    mt = {}
            tid = str(mt.get("task_id") or _parse_task_id(m.get("text", "")) or "")
            if tid in pending and m.get("tag") in ("done", "failed"):
                pending.discard(tid)
                print(f"  [{m.get('tag')}] {tid}")

    if pending:
        print(
            f"\n⏱ {len(pending)} task(s) still pending after {args.wait}s: "
            + ", ".join(sorted(pending))
        )
    else:
        print(f"\n🏁 all {len(task_ids)} race task(s) settled")


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
    sp_read.add_argument(
        "--oldest",
        action="store_true",
        help="Show oldest N (xrange from 0-0); default shows newest N",
    )

    sub.add_parser("topics", help="List active topics")
    sub.add_parser("health", help="Check station health")

    sp_agents = sub.add_parser("agents", help="List active agents (panes)")
    sp_agents.add_argument(
        "--within",
        type=int,
        default=300,
        help="Look-back window in seconds (default: 300)",
    )

    sp_tasks = sub.add_parser(
        "tasks",
        help="Show task status (pending/done/failed/timeout) for the tasks topic",
    )
    sp_tasks.add_argument(
        "--count",
        type=int,
        default=200,
        help="Max messages to scan from tasks stream (default: 200)",
    )
    sp_tasks.add_argument(
        "--max-age",
        type=int,
        default=300,
        dest="max_age",
        help="Seconds after which an unresolved assign is considered timeout (default: 300)",
    )
    sp_tasks.add_argument(
        "--pending",
        action="store_true",
        help="Only show pending and timeout tasks",
    )
    sp_tasks.add_argument(
        "--mark-timeout",
        action="store_true",
        dest="mark_timeout",
        help=(
            "Publish a `timeout` tag event for each detected timeout task "
            "(retry policy: Strategy A — no auto-retry, caller decides)"
        ),
    )

    sp_race = sub.add_parser(
        "race",
        help=(
            "Race the same prompt across N workers (one assign per worker, "
            "shared task base id; results land in the tasks topic)"
        ),
    )
    sp_race.add_argument("message", help="Prompt to send to every worker")
    sp_race.add_argument(
        "--task-id",
        required=True,
        dest="task_id",
        help=(
            "Base task id; each worker gets <base>-<cli> so done/failed "
            "events match cleanly (e.g. retry-policy-claude)"
        ),
    )
    sp_race.add_argument(
        "--workers",
        required=True,
        help=(
            "Comma-separated cli:pane list, e.g. "
            "claude:%%5,codex:%%6,gemini:%%7"
        ),
    )
    sp_race.add_argument(
        "--meta",
        default="",
        help="Extra JSON object merged into each worker's _meta",
    )
    sp_race.add_argument(
        "--wait",
        type=int,
        default=0,
        help=(
            "Poll the tasks stream until all workers settle or this many "
            "seconds elapse (default 0 = fire-and-forget, watch via "
            "`channel tasks --pending`)"
        ),
    )
    sp_race.add_argument(
        "--no-notify",
        action="store_true",
        dest="no_notify",
        help="Publish only; do NOT push the prompt into each pane via tmux send-keys",
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
        "tasks": cmd_tasks,
        "race": cmd_race,
    }
    try:
        fn[args.cmd](args)
    except httpx.ConnectError:
        print("❌ Cannot connect to session-channel (is it running?)", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
