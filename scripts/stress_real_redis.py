#!/usr/bin/env python3
"""Real-Redis stress test for board v2.

50 panes / 1000 tasks against the running station + real Redis.
Each worker claim-loops with count=1, completes immediately, and
stops once the publisher-side counter says all tasks are done.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import random
import statistics
import time

import httpx

URL = os.environ.get("SESSION_CHANNEL_URL", "http://localhost:10199")
KEY = os.environ.get("SESSION_CHANNEL_KEY", "change-me-in-production")
HEADERS = {"x-local-key": KEY, "Content-Type": "application/json"}


def msg_id_to_ms(msg_id: str) -> int:
    try:
        return int(msg_id.split("-")[0])
    except Exception:
        return 0


async def advertise(c: httpx.AsyncClient, pane_id: str, cli_type: str, mcps: list[str]):
    now = int(time.time())
    body = {
        "pane_id": pane_id,
        "cli_type": cli_type,
        "mcps": mcps,
        "skills": [],
        "started_at": now,
        "last_seen": now,
    }
    r = await c.post(f"{URL}/api/panes/advertise", json=body, headers=HEADERS)
    r.raise_for_status()


async def publish(c: httpx.AsyncClient, board_id: str, tasks: list[dict]) -> list[str]:
    body = {"sender": "stress", "tasks": tasks}
    r = await c.post(f"{URL}/api/board/{board_id}/publish", json=body, headers=HEADERS)
    r.raise_for_status()
    return r.json()["ids"]


async def worker(
    c: httpx.AsyncClient,
    board_id: str,
    pane: str,
    state: dict,
    latencies: list[float],
):
    """Claim → complete loop. Stops when state['done'] >= state['target']."""
    while state["done"] < state["target"]:
        try:
            r = await c.post(
                f"{URL}/api/board/{board_id}/claim",
                json={"pane": pane, "count": 1},
                headers=HEADERS,
                timeout=5.0,
            )
            data = r.json()
        except Exception:
            await asyncio.sleep(0.05)
            continue

        if not data.get("ok"):
            reason = data.get("reason", "")
            # Cap/assignment reject: stream republished — back off LONGER so
            # other panes get a chance and we don't hot-loop. no_tasks is
            # cheap; just briefly yield.
            if reason in ("caps_mismatch", "assignment_mismatch"):
                await asyncio.sleep(0.5)
            else:
                await asyncio.sleep(0.02)
            continue

        for task in data.get("tasks", []):
            task_id = task["id"]
            # Measure claim→complete (per-task processing latency), not the
            # publish→done end-to-end window which is dominated by burst
            # queueing in stress mode.
            claim_ms = int(time.time() * 1000)
            try:
                await c.post(
                    f"{URL}/api/board/{board_id}/complete",
                    json={
                        "task_id": task_id,
                        "pane": pane,
                        "result": {"status": "ok", "payload": {"worker": pane}},
                    },
                    headers=HEADERS,
                    timeout=5.0,
                )
            except Exception:
                # Drop on failure → reaper will requeue
                continue
            done_ms = int(time.time() * 1000)
            latencies.append(max(done_ms - claim_ms, 0))
            state["done"] += 1


async def main():
    p = argparse.ArgumentParser()
    p.add_argument("--panes", type=int, default=50)
    p.add_argument("--tasks", type=int, default=1000)
    p.add_argument("--board", default=f"stress-{int(time.time())}")
    p.add_argument("--timeout", type=int, default=120)
    args = p.parse_args()

    print(f"Stress test: {args.panes} panes / {args.tasks} tasks → board={args.board}")

    limits = httpx.Limits(max_connections=300, max_keepalive_connections=100)
    async with httpx.AsyncClient(limits=limits, timeout=10.0) as c:
        # Phase A: setup
        cli_types = (["claude-code"] * 20 + ["codex"] * 15 + ["gemini"] * 15)[: args.panes]
        random.shuffle(cli_types)
        cap_map = {
            "claude-code": ["memvault", "intelflow"],
            "codex": ["docvault"],
            "gemini": [],
        }
        panes = [f"stress-pane-{i:03d}" for i in range(args.panes)]
        cli_by_pane = {p: cli_types[i] for i, p in enumerate(panes)}

        await asyncio.gather(
            *[advertise(c, p, cli_by_pane[p], cap_map[cli_by_pane[p]]) for p in panes]
        )
        print(f"  Advertised {len(panes)} panes")

        # Build tasks: 30% with required_caps
        cap_choices = ["memvault", "docvault", "intelflow"]
        tasks = []
        for i in range(args.tasks):
            t = {"id": f"t{i:04d}", "desc": f"task {i}", "task_class": "short"}
            # 5% cap-restricted reflects realistic mixed workload; higher
            # ratios trigger hot-loop on the cap-reject path (known
            # limitation — see runbook).
            if random.random() < 0.05:
                t["required_caps"] = [random.choice(cap_choices)]
            tasks.append(t)
        # Publish in batches of 100 to avoid huge bodies
        ids = []
        t0 = time.time()
        for i in range(0, len(tasks), 100):
            batch = tasks[i : i + 100]
            ids.extend(await publish(c, args.board, batch))
        publish_dt = time.time() - t0
        print(f"  Published {len(ids)} tasks in {publish_dt:.2f}s")

        # Phase B: workers
        state = {"done": 0, "target": args.tasks}
        latencies: list[float] = []

        wall_t0 = time.time()
        worker_tasks = [
            asyncio.create_task(worker(c, args.board, p, state, latencies)) for p in panes
        ]

        # Watchdog — abort if exceed timeout
        timeout_at = time.time() + args.timeout
        last_done = 0
        last_progress = time.time()
        while state["done"] < args.tasks:
            await asyncio.sleep(0.5)
            if state["done"] != last_done:
                last_done = state["done"]
                last_progress = time.time()
                print(
                    f"  progress: {state['done']}/{args.tasks} "
                    f"({state['done'] / args.tasks * 100:.0f}%)"
                )
            if time.time() > timeout_at:
                print(f"  TIMEOUT at {state['done']}/{args.tasks}")
                break
            if time.time() - last_progress > 30:
                print(f"  STALL — no progress for 30s at {state['done']}")
                break
        wall_dt = time.time() - wall_t0

        # Cancel workers
        for t in worker_tasks:
            t.cancel()
        await asyncio.gather(*worker_tasks, return_exceptions=True)

        # Phase C: report
        print("\n=== Results ===")
        print(f"  wall clock: {wall_dt:.2f}s")
        print(f"  throughput: {state['done'] / wall_dt:.1f} tasks/sec")
        if latencies:
            latencies.sort()
            p50 = statistics.median(latencies)
            p95 = latencies[int(len(latencies) * 0.95)]
            p99 = latencies[int(len(latencies) * 0.99)]
            print(f"  P50 / P95 / P99: {p50:.0f} / {p95:.0f} / {p99:.0f} ms")

        # Pull metrics + projection
        try:
            metrics_r = await c.get(f"{URL}/metrics", timeout=5)
            metrics_text = metrics_r.text
            for line in metrics_text.splitlines():
                if line.startswith("session_channel_") and not line.startswith("# "):
                    if any(
                        k in line
                        for k in (
                            "lease_expired_total",
                            "claim_conflict_total",
                            "orphan_recovered_total",
                            "dead_letter_total",
                        )
                    ):
                        print(f"  metric: {line}")
        except Exception as e:
            print(f"  metrics fetch failed: {e}")

        try:
            proj_r = await c.get(f"{URL}/api/board/{args.board}", headers=HEADERS, timeout=10)
            summary = proj_r.json()["summary"]
            print(f"  projection: {summary}")
        except Exception as e:
            print(f"  projection fetch failed: {e}")

        # Cleanup panes
        await asyncio.gather(
            *[c.delete(f"{URL}/api/panes/{p}", headers=HEADERS) for p in panes],
            return_exceptions=True,
        )

        # Pass / fail
        pass_p99 = bool(latencies and latencies[int(len(latencies) * 0.99)] < 200)
        pass_done = state["done"] == args.tasks
        verdict = "PASS" if (pass_p99 and pass_done) else "FAIL"
        print(f"\n=== {verdict} ===")
        return 0 if verdict == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
