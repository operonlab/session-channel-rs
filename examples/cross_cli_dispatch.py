#!/usr/bin/env python3
"""Cross-CLI Dispatch Demo — Step 7 in action.

Shows the full flow:
1. Supervisor (this script) dispatches 6 mixed tasks via TmuxRelayClient
2. Three worker subprocesses simulate CC / Codex / Gemini panes — each
   advertises its capabilities then claim-loops
3. Supervisor watches the projection until all done, prints summary

Run:
    SESSION_CHANNEL_URL=http://localhost:10101 \
      ~/.local/bin/python3 stations/session-channel/examples/cross_cli_dispatch.py

Designed to also work as a smoke test for the v2 board after merge.

Capabilities chosen to exercise capability-aware routing:
- demo-cc       (claude-code): mcp:memvault    + skill:memory-curator
- demo-codex    (codex):       mcp:docvault    + skill:doc-qa
- demo-gemini   (gemini):      mcp:paper       + skill:paper-research

Tasks (logical ids):
- mem-1, mem-2          required_caps=["memvault"]      → only demo-cc can claim
- doc-1, doc-2          required_caps=["docvault"]      → only demo-codex can claim
- paper-1               required_caps=["paper-research"]→ only demo-gemini can claim
- public-1              required_caps=[]                → any pane can claim

Cleanup at exit:
- DELETE /api/panes/{id} for each pane
- SIGTERM workers, wait briefly, SIGKILL stragglers
- Redis SCAN+DEL for ws:channel:board:demo-board* keys (best-effort)
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

# ------------------------------------------------------------------ #
# Configuration
# ------------------------------------------------------------------ #

CHANNEL_URL = os.environ.get("SESSION_CHANNEL_URL", "http://localhost:10101")
CHANNEL_KEY = os.environ.get("SESSION_CHANNEL_KEY", "change-me-in-production")
BOARD_ID = os.environ.get("DEMO_BOARD_ID", "demo-board")
DEMO_TIMEOUT_S = int(os.environ.get("DEMO_TIMEOUT_S", "60"))
POLL_INTERVAL_S = float(os.environ.get("DEMO_POLL_INTERVAL_S", "0.5"))
SIMULATE_S = float(os.environ.get("DEMO_SIMULATE_S", "1.0"))

# Make sdk-client importable when run from a worktree
_REPO = Path(__file__).resolve().parents[3]
_SDK = _REPO / "libs" / "sdk-client"
if str(_SDK) not in sys.path:
    sys.path.insert(0, str(_SDK))

from sdk_client.session_channel import (  # noqa: E402
    PaneAdvertise,
    SessionChannelClient,
    TaskPublish,
)
from sdk_client.tmux_relay import TmuxRelayClient  # noqa: E402

# Pane definitions
PANES = [
    {
        "pane_id": "demo-cc",
        "cli_type": "claude-code",
        "mcps": ["memvault"],
        "skills": ["memory-curator"],
    },
    {
        "pane_id": "demo-codex",
        "cli_type": "codex",
        "mcps": ["docvault"],
        "skills": ["doc-qa"],
    },
    {
        "pane_id": "demo-gemini",
        "cli_type": "gemini",
        "mcps": ["paper"],
        "skills": ["paper-research"],
    },
]

TASKS = [
    {"id": "mem-1", "desc": "Recall last week's design notes", "required_caps": ["memvault"]},
    {"id": "mem-2", "desc": "Summarize captured ideas", "required_caps": ["memvault"]},
    {"id": "doc-1", "desc": "Answer FAQ from PDF #42", "required_caps": ["docvault"]},
    {"id": "doc-2", "desc": "Extract tables from quarterly report", "required_caps": ["docvault"]},
    {"id": "paper-1", "desc": "Find ICLR 2025 RAG papers", "required_caps": ["paper-research"]},
    {"id": "public-1", "desc": "Anyone can do this", "required_caps": []},
]


# ------------------------------------------------------------------ #
# Worker subprocess (inlined — uses python child running this same file
# with --worker-mode flag so we don't depend on board-worker.sh)
# ------------------------------------------------------------------ #


def _worker_main(pane_id: str) -> int:
    """Claim-loop worker. Exits when CHANNEL_DEMO_STOP file appears or SIGTERM."""
    client = SessionChannelClient(base_url=CHANNEL_URL, local_key=CHANNEL_KEY)

    stop_flag = Path(f"/tmp/cross-cli-demo-stop-{os.getppid()}")
    log_path = Path(f"/tmp/cross-cli-demo-{pane_id}.log")
    log = log_path.open("a", buffering=1)

    def _log(msg: str) -> None:
        log.write(f"[{pane_id}] {msg}\n")

    _log(f"worker started (pid={os.getpid()}, ppid={os.getppid()})")

    claimed: list[str] = []
    backoff = 0.3
    while not stop_flag.exists():
        try:
            tasks = client.claim_task(BOARD_ID, pane=pane_id, count=1)
        except Exception as e:
            _log(f"claim error: {e}")
            time.sleep(1.0)
            continue

        if not tasks:
            time.sleep(backoff)
            continue

        for t in tasks:
            tid = t.get("id") or ""
            desc = t.get("desc") or ""
            _log(f"claimed {tid}  desc={desc!r}")
            # Simulate work: progress 50, complete
            try:
                client.progress(BOARD_ID, tid, percent=50, stage="working")
            except Exception as e:
                _log(f"progress error: {e}")
            time.sleep(SIMULATE_S)
            try:
                client.complete(
                    BOARD_ID,
                    tid,
                    {
                        "status": "ok",
                        "payload": {"ran_in": pane_id, "logical_id": t.get("logical_id", "")},
                    },
                )
                claimed.append(t.get("logical_id") or tid)
                _log(f"completed {tid}")
            except Exception as e:
                _log(f"complete error: {e}")

    _log(f"stopping; claimed={claimed}")
    log.close()
    return 0


# ------------------------------------------------------------------ #
# Supervisor
# ------------------------------------------------------------------ #


def _print_section(title: str) -> None:
    print()
    print("=" * 64)
    print(f"  {title}")
    print("=" * 64)


def _supervisor() -> int:
    print(f"channel_url={CHANNEL_URL}")
    print(f"board_id={BOARD_ID}")
    print(f"timeout={DEMO_TIMEOUT_S}s")

    relay = TmuxRelayClient()
    sc = SessionChannelClient(base_url=CHANNEL_URL, local_key=CHANNEL_KEY)

    # 0. Health check
    try:
        h = sc.health()
        print(f"health: {h}")
    except Exception as e:
        print(f"FATAL: cannot reach session-channel at {CHANNEL_URL}: {e}")
        return 2

    # 1. Advertise 3 panes (capability registry)
    _print_section("Step 1: Advertise 3 panes")
    now = int(time.time())
    for p in PANES:
        adv = PaneAdvertise(
            pane_id=p["pane_id"],
            cli_type=p["cli_type"],
            mcps=p["mcps"],
            skills=p["skills"],
            started_at=now,
            last_seen=now,
        )
        sc.advertise(adv)
        print(
            f"  advertised {p['pane_id']:14s} cli={p['cli_type']:12s} caps={p['mcps'] + p['skills']}"
        )

    # Round-trip: confirm registry sees them
    panes_seen = sc.list_panes()
    pane_ids_seen = {x.get("pane_id") for x in panes_seen}
    for p in PANES:
        if p["pane_id"] not in pane_ids_seen:
            print(f"  WARN: {p['pane_id']} not visible in /api/panes")

    # 2. Spawn 3 worker subprocesses (this same file in --worker-mode)
    _print_section("Step 2: Spawn 3 worker subprocesses")
    workers: list[subprocess.Popen] = []
    stop_flag = Path(f"/tmp/cross-cli-demo-stop-{os.getpid()}")
    if stop_flag.exists():
        stop_flag.unlink()

    for p in PANES:
        # Clear any old log
        Path(f"/tmp/cross-cli-demo-{p['pane_id']}.log").unlink(missing_ok=True)
        env = os.environ.copy()
        env["SESSION_CHANNEL_URL"] = CHANNEL_URL
        env["SESSION_CHANNEL_KEY"] = CHANNEL_KEY
        env["DEMO_BOARD_ID"] = BOARD_ID
        env["DEMO_SIMULATE_S"] = str(SIMULATE_S)
        proc = subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), "--worker-mode", p["pane_id"]],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        workers.append(proc)
        print(f"  spawned worker {p['pane_id']:14s} pid={proc.pid}")

    # 3. Dispatch via board (TmuxRelayClient → publish_board)
    _print_section("Step 3: Dispatch 6 tasks via TmuxRelayClient.dispatch_via_board")
    dispatch_resp = relay.dispatch_via_board(
        BOARD_ID,
        [TaskPublish(**t).model_dump(mode="json") for t in TASKS],
        sender="demo-supervisor",
    )
    print(f"  dispatch response: {json.dumps(dispatch_resp, default=str)[:200]}")
    if not dispatch_resp.get("ok"):
        print("  FATAL: dispatch failed")
        _cleanup(workers, stop_flag, sc)
        return 3

    msg_ids = dispatch_resp.get("ids") or []
    print(f"  published {len(msg_ids)} stream messages")

    # 4. Poll projection until all done
    _print_section("Step 4: Poll /api/board/<id> until summary.done == 6")
    deadline = time.monotonic() + DEMO_TIMEOUT_S
    last_summary = None
    final_board = None
    while time.monotonic() < deadline:
        try:
            board = sc.get_board(BOARD_ID)
        except Exception as e:
            print(f"  poll error: {e}")
            time.sleep(POLL_INTERVAL_S)
            continue
        summary = board.get("summary") or {}
        if summary != last_summary:
            print(
                f"  t+{int(DEMO_TIMEOUT_S - (deadline - time.monotonic())):2d}s  "
                f"open={summary.get('open', 0)} claimed={summary.get('claimed', 0)} "
                f"done={summary.get('done', 0)} blocked={summary.get('blocked', 0)}"
            )
            last_summary = summary
        if summary.get("done", 0) >= len(TASKS):
            final_board = board
            break
        time.sleep(POLL_INTERVAL_S)

    if final_board is None:
        try:
            final_board = sc.get_board(BOARD_ID)
        except Exception:
            final_board = {}
        print(f"  TIMEOUT after {DEMO_TIMEOUT_S}s — last summary: {last_summary}")

    # 5. Per-pane assignment summary
    _print_section("Step 5: Result summary")
    pane_to_tasks: dict[str, list[str]] = {p["pane_id"]: [] for p in PANES}
    pane_to_tasks["<unclaimed>"] = []
    for t in (final_board or {}).get("tasks", []) or []:
        owner = t.get("done_by") or t.get("claimed_by") or "<unclaimed>"
        pane_to_tasks.setdefault(owner, []).append(
            f"{t.get('logical_id') or t.get('id')}({t.get('status')})"
        )
    for pane, tids in pane_to_tasks.items():
        if not tids and pane != "<unclaimed>":
            print(f"  {pane:14s} (none)")
        elif tids:
            print(f"  {pane:14s} {tids}")

    summary = (final_board or {}).get("summary") or {}
    print(f"\n  totals: {summary}")

    # Capability routing assertion (informational only)
    _print_section("Step 6: Capability routing check")
    cap_expectations = {
        "mem-1": "demo-cc",
        "mem-2": "demo-cc",
        "doc-1": "demo-codex",
        "doc-2": "demo-codex",
        "paper-1": "demo-gemini",
    }
    by_logical_id: dict[str, dict] = {}
    for t in (final_board or {}).get("tasks", []) or []:
        lid = t.get("logical_id") or t.get("id")
        by_logical_id[lid] = t
    misroutes = 0
    for lid, expected in cap_expectations.items():
        t = by_logical_id.get(lid) or {}
        actual = t.get("done_by") or t.get("claimed_by") or "<unclaimed>"
        ok = actual == expected
        misroutes += 0 if ok else 1
        print(
            f"  {lid:10s} expected={expected:14s} actual={actual:14s} {'OK' if ok else 'MISROUTE'}"
        )
    print(f"  misroutes: {misroutes}")

    # 6. Cleanup
    _print_section("Step 7: Cleanup")
    _cleanup(workers, stop_flag, sc)

    done = summary.get("done", 0) if summary else 0
    return 0 if (done == len(TASKS) and misroutes == 0) else 1


def _cleanup(
    workers: list[subprocess.Popen],
    stop_flag: Path,
    sc: SessionChannelClient,
) -> None:
    # Signal workers to stop
    stop_flag.touch()
    for w in workers:
        try:
            w.send_signal(signal.SIGTERM)
        except Exception:
            pass
    # Brief wait, then escalate to SIGKILL
    deadline = time.monotonic() + 3.0
    for w in workers:
        remaining = max(0.1, deadline - time.monotonic())
        try:
            w.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            try:
                w.kill()
            except Exception:
                pass

    # Release panes
    for p in PANES:
        try:
            sc.delete_pane(p["pane_id"])
            print(f"  released {p['pane_id']}")
        except Exception as e:
            print(f"  release {p['pane_id']} failed: {e}")

    # Best-effort redis cleanup of board keys
    _redis_cleanup_board(BOARD_ID)

    # Remove stop flag
    try:
        stop_flag.unlink()
    except Exception:
        pass


def _redis_cleanup_board(board_id: str) -> None:
    """SCAN + DEL ws:channel:board:{id}* (best-effort, requires redis)."""
    try:
        import redis  # type: ignore
    except ImportError:
        print("  redis cleanup skipped (no `redis` module on path)")
        return
    try:
        r = redis.Redis(host="127.0.0.1", port=6379, decode_responses=True)
        patterns = [
            f"ws:channel:board:{board_id}*",
            f"ws:board:{board_id}*",
            f"ws:dag:{board_id}*",
        ]
        n = 0
        for pat in patterns:
            for key in r.scan_iter(match=pat, count=200):
                r.delete(key)
                n += 1
        print(f"  redis cleanup: {n} keys removed")
    except Exception as e:
        print(f"  redis cleanup failed: {e}")


# ------------------------------------------------------------------ #
# Entry
# ------------------------------------------------------------------ #


def main() -> int:
    if len(sys.argv) >= 3 and sys.argv[1] == "--worker-mode":
        return _worker_main(sys.argv[2])

    try:
        return _supervisor()
    except KeyboardInterrupt:
        print("\ninterrupted")
        return 130


if __name__ == "__main__":
    sys.exit(main())
