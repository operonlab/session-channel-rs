#!/usr/bin/env python3
"""
supervisor.py — Relay pool worker supervised respawn.

Scans each configured relay pool pane; if a pane's current command has
returned to `zsh` (worker exited), waits for the configured grace period
and then re-spawns the expected wrapper command.

Design choices:
  - Grace period: avoids immediate re-spawn when the user manually exits
    (Ctrl+C / /exit). Tracked via /tmp timestamps.
  - Disable flag: touch /tmp/session-channel-supervisor-disable-<pane_id>.flag
    to prevent supervisor from ever respawning that pane this boot.
  - Static config: relay_pool.workers in stations/session-channel/config.yaml.
  - No `--resume`: starts a fresh session each respawn (simpler, less risk of
    stale context confusing the worker).
  - Logging to stderr: Cronicle captures stderr as job log.

Usage (direct / manual test):
    python3 stations/session-channel/scripts/supervisor.py [--dry-run] [--config <path>]

Cronicle job:
    ~/.local/bin/python3 ~/workshop/stations/session-channel/scripts/supervisor.py
    interval: 60s
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

import yaml  # PyYAML — available in session-channel uv env

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

STATION_DIR = Path(__file__).resolve().parent.parent  # stations/session-channel/
DEFAULT_CONFIG = STATION_DIR / "config.yaml"

WRAPPERS_DIR = STATION_DIR / "wrappers"
CHANNEL_CLI = STATION_DIR / "cli" / "channel.py"
PY = Path("/Users/joneshong/.local/bin/python3")

# Worker spawn commands by cli_type. These mirror launch-relay-pool.sh logic.
CLI_COMMANDS: dict[str, list[str]] = {
    "claude": ["claude", "--dangerously-skip-permissions"],
    "codex": [str(WRAPPERS_DIR / "codex-with-channel.sh")],
    "gemini": [str(WRAPPERS_DIR / "gemini-with-channel.sh")],
}

# Commands whose presence in pane_current_command means the worker is ALIVE.
# tmux reports the leaf process name (basename), e.g. "node", "python3", "bash".
CLI_ALIVE_CMDS: dict[str, set[str]] = {
    "claude": {"claude", "node"},  # claude is a Node binary
    "codex": {"codex", "node", "bash"},
    "gemini": {"gemini", "node", "bash"},
}

# pane_current_command values that mean "idle / back to shell"
IDLE_CMDS = {"zsh", "bash", "sh", "fish"}

# Temp dir for grace-period timestamps and disable flags
TMP_PREFIX = "/tmp/session-channel-supervisor"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def log(msg: str) -> None:
    """Write timestamped line to stderr (captured by Cronicle as job log)."""
    ts = time.strftime("%Y-%m-%dT%H:%M:%S")
    print(f"[{ts}] {msg}", file=sys.stderr, flush=True)


def load_config(config_path: Path) -> dict:
    with config_path.open() as f:
        return yaml.safe_load(f)


def pane_current_cmd(pane_id: str) -> str | None:
    """Return the current command name running in the given tmux pane.

    Returns None if tmux is not running or the pane does not exist.
    Strips the leading '%' for pane_id normalisation if needed.
    """
    try:
        result = subprocess.run(
            ["tmux", "display-message", "-p", "-t", pane_id, "#{pane_current_command}"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode != 0:
            return None
        return result.stdout.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None


def pane_exists(pane_id: str) -> bool:
    """Return True if the pane id is known to this tmux server."""
    try:
        result = subprocess.run(
            ["tmux", "display-message", "-p", "-t", pane_id, "#{pane_id}"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return result.returncode == 0 and result.stdout.strip() != ""
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def exit_timestamp_path(pane_id: str) -> Path:
    safe = pane_id.lstrip("%")
    return Path(f"{TMP_PREFIX}-exit-{safe}.ts")


def disable_flag_path(pane_id: str) -> Path:
    safe = pane_id.lstrip("%")
    return Path(f"{TMP_PREFIX}-disable-{safe}.flag")


def record_exit_time(pane_id: str) -> None:
    p = exit_timestamp_path(pane_id)
    p.write_text(str(time.time()))


def seconds_since_exit(pane_id: str) -> float | None:
    """Return seconds elapsed since worker exited, or None if no record."""
    p = exit_timestamp_path(pane_id)
    if not p.exists():
        return None
    try:
        recorded = float(p.read_text().strip())
        return time.time() - recorded
    except ValueError:
        return None


def clear_exit_record(pane_id: str) -> None:
    p = exit_timestamp_path(pane_id)
    p.unlink(missing_ok=True)


def is_disabled(pane_id: str) -> bool:
    return disable_flag_path(pane_id).exists()


def spawn_worker(pane_id: str, cli_type: str, dry_run: bool) -> bool:
    """Send the wrapper command to the given tmux pane.

    Returns True if command was dispatched (or would have been in dry-run).
    """
    cmd_parts = CLI_COMMANDS.get(cli_type)
    if cmd_parts is None:
        log(f"  ERROR: unknown cli_type '{cli_type}' for pane {pane_id}")
        return False

    send_cmd = " ".join(cmd_parts)
    log(f"  SPAWN {pane_id}: tmux send-keys '{send_cmd}' Enter")

    if dry_run:
        log("  [dry-run] skipped actual send-keys")
        return True

    result = subprocess.run(
        ["tmux", "send-keys", "-t", pane_id, send_cmd, "Enter"],
        capture_output=True,
        text=True,
        timeout=5,
    )
    if result.returncode != 0:
        log(f"  ERROR: tmux send-keys failed: {result.stderr.strip()}")
        return False
    return True


def supervisor_announce(config: dict, dry_run: bool) -> None:
    """Publish a supervisor heartbeat to channel agents topic (optional)."""
    if not config.get("relay_pool", {}).get("supervisor_announce", False):
        return
    if not CHANNEL_CLI.exists():
        return
    msg = "supervisor heartbeat"
    import socket

    host = socket.gethostname().split(".")[0]
    meta = f'{{"v":1,"host":"{host}","pane":"supervisor","cli":"supervisor","role":"supervisor","ts":{int(time.time())}}}'
    cmd = [str(PY), str(CHANNEL_CLI), "send", "agents", msg, "--tag", "heartbeat", "--meta", meta]
    if dry_run:
        log(f"  [dry-run] would announce: {' '.join(cmd)}")
        return
    subprocess.run(cmd, capture_output=True, timeout=10)


# ---------------------------------------------------------------------------
# Main scan loop (one pass)
# ---------------------------------------------------------------------------


def run_once(config: dict, dry_run: bool) -> None:
    pool_cfg = config.get("relay_pool", {})
    grace = float(pool_cfg.get("grace_seconds", 120))
    workers = pool_cfg.get("workers", [])

    if not workers:
        log("WARNING: relay_pool.workers is empty in config.yaml — nothing to supervise")
        return

    log(f"Scanning {len(workers)} worker pane(s) (grace={grace}s, dry_run={dry_run})")

    for w in workers:
        pane_id: str = w.get("pane_id", "")
        cli_type: str = w.get("cli_type", "")
        enabled: bool = w.get("enabled", True)

        if not pane_id or not cli_type:
            log(f"  SKIP: malformed worker entry {w}")
            continue

        if not enabled:
            log(f"  {pane_id}: disabled in config — skip")
            continue

        if is_disabled(pane_id):
            log(f"  {pane_id}: disable flag exists — skip")
            continue

        if not pane_exists(pane_id):
            log(f"  {pane_id}: pane does not exist in tmux — skip")
            continue

        current_cmd = pane_current_cmd(pane_id)
        if current_cmd is None:
            log(f"  {pane_id}: could not query pane_current_command — skip")
            continue

        alive_cmds = CLI_ALIVE_CMDS.get(cli_type, set())

        if current_cmd in IDLE_CMDS:
            # Worker has exited (back to shell)
            elapsed = seconds_since_exit(pane_id)

            if elapsed is None:
                # First time we notice this exit — record the time
                record_exit_time(pane_id)
                log(f"  {pane_id}: worker ({cli_type}) exited → zsh, grace timer started")
            elif elapsed < grace:
                log(f"  {pane_id}: in grace period ({elapsed:.0f}s / {grace}s) — waiting")
            else:
                # Grace period over — respawn
                ok = spawn_worker(pane_id, cli_type, dry_run)
                if ok:
                    log(f"  {pane_id}: respawned {cli_type} (was idle {elapsed:.0f}s)")
                    clear_exit_record(pane_id)
                else:
                    log(f"  {pane_id}: respawn failed — will retry next cycle")

        elif current_cmd in alive_cmds or current_cmd not in IDLE_CMDS:
            # Worker is alive (running CLI or its runtime process).
            # Clear any stale exit record if present.
            if exit_timestamp_path(pane_id).exists():
                log(f"  {pane_id}: worker recovered (cmd={current_cmd!r}) — clearing exit record")
                clear_exit_record(pane_id)
            else:
                log(f"  {pane_id}: OK (cmd={current_cmd!r}, cli={cli_type})")

    supervisor_announce(config, dry_run)


# ---------------------------------------------------------------------------
# CLI entry
# ---------------------------------------------------------------------------


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Session-channel relay pool supervisor")
    p.add_argument("--config", type=Path, default=DEFAULT_CONFIG, help="Path to config.yaml")
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Check panes but do NOT send tmux keys or spawn workers",
    )
    return p.parse_args()


def main() -> None:
    args = parse_args()

    if not args.config.exists():
        log(f"ERROR: config not found: {args.config}")
        sys.exit(1)

    config = load_config(args.config)
    run_once(config, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
