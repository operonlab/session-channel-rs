#!/bin/bash
# gemini-with-channel — launch Gemini CLI inside session-channel.
#
# Replaces direct `gemini` invocation in the relay pool so that:
#   - Gemini pane appears in `channel agents` immediately on launch (announce)
#   - Gemini pane disappears on exit (leave, via trap)
#   - Idle pane keeps a periodic heartbeat (background loop, 60s default)
#   - YOLO mode is on by default (-y)
#
# Notes vs. codex-with-channel:
#   - Gemini hooks subcommand only supports `migrate` (v0.41.2 preview);
#     there is no per-turn notify hook, so heartbeats are loop-only.
#   - Gemini accepts `--session-id UUID`, which we pre-allocate so the
#     channel meta carries a stable session identifier (Codex/Claude can't).
#
# Usage:
#   gemini-with-channel [gemini args...]
#
# Env overrides:
#   CHANNEL_ROLE                 — "worker" (default) or "leader"
#   CHANNEL_DRY_RUN              — when set, announce/leave only; skip gemini
#   CHANNEL_HEARTBEAT_INTERVAL   — idle heartbeat interval, default 60s
#   CHANNEL_SESSION_ID           — pre-allocated UUID; default = `uuidgen`

set -u

PANE="${TMUX_PANE:-pid-$$}"
HOST="$(hostname -s)"
ROLE="${CHANNEL_ROLE:-worker}"
HB_INTERVAL="${CHANNEL_HEARTBEAT_INTERVAL:-60}"
SESSION_ID="${CHANNEL_SESSION_ID:-$(uuidgen | tr 'A-Z' 'a-z')}"

# Resolve session-channel install home:
#   1. $SESSION_CHANNEL_HOME (explicit override)
#   2. $HOME/.session-channel (standard install location)
#   3. script-relative (running from source tree / monorepo)
if [[ -z "${SESSION_CHANNEL_HOME:-}" ]]; then
  if [[ -d "$HOME/.session-channel/cli" ]]; then
    SESSION_CHANNEL_HOME="$HOME/.session-channel"
  else
    SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    SESSION_CHANNEL_HOME="${SCRIPT_DIR%/wrappers}"
  fi
fi

CHANNEL="${SESSION_CHANNEL_HOME}/cli/channel.py"

if [[ ! -x "$CHANNEL" ]]; then
  echo "gemini-with-channel: channel CLI not found at $CHANNEL" >&2
  exec gemini "$@"
fi

build_meta() {
  local ts
  ts=$(date +%s)
  printf '{"v":1,"host":"%s","pane":"%s","cli":"gemini","role":"%s","ts":%s,"session_id":"%s"}' \
    "$HOST" "$PANE" "$ROLE" "$ts" "$SESSION_ID"
}

publish() {
  local tag="$1" msg="$2"
  "$CHANNEL" send agents "$msg" --tag "$tag" --meta "$(build_meta)" \
    >/dev/null 2>&1 || true
}

publish announce "gemini/$PANE started"

(
  while sleep "$HB_INTERVAL"; do
    publish heartbeat "gemini/$PANE idle heartbeat"
  done
) &
HB_PID=$!

# Background SSE listener — push-delivery for tasks targeted at this pane.
SSE_PID=""
source "${SESSION_CHANNEL_HOME}/wrappers/sse_subscribe.sh" 2>/dev/null || true
if command -v start_sse_listener >/dev/null 2>&1; then
  start_sse_listener
fi

cleanup() {
  kill "$HB_PID" 2>/dev/null || true
  [ -n "$SSE_PID" ] && kill "$SSE_PID" 2>/dev/null || true
  publish leave "gemini/$PANE left"
}
trap cleanup EXIT INT TERM

if [[ -n "${CHANNEL_DRY_RUN:-}" ]]; then
  echo "gemini-with-channel: dry-run (announce sent, brief sleep, then leave)" >&2
  sleep 1
  exit 0
fi

# Run as child (NOT exec) so the EXIT trap fires on quit and a `leave`
# event is always published — same fix as codex-with-channel.
#
# Respawn loop (opt-in via CHANNEL_LOOP=1): Gemini sometimes self-exits
# after handling a task ("Agent powering down. Goodbye!"). When this
# happens, the pane drops to zsh and SSE-pushed prompts go to a shell
# that interprets the trust marker as a bash command. The loop relaunches
# Gemini so the pane stays a live worker. Bail after 3 quick exits to
# avoid hot-spin if `gemini` is broken.
RESPAWN_QUICK_EXIT_THRESHOLD=5  # exits faster than this (seconds) count as quick
RESPAWN_MAX_QUICK=3
quick_count=0
while true; do
  start_ts=$(date +%s)
  gemini --yolo --session-id "$SESSION_ID" "$@"
  rc=$?
  end_ts=$(date +%s)

  if [[ -z "${CHANNEL_LOOP:-}" ]]; then
    exit $rc
  fi

  if (( end_ts - start_ts < RESPAWN_QUICK_EXIT_THRESHOLD )); then
    quick_count=$((quick_count + 1))
    if (( quick_count >= RESPAWN_MAX_QUICK )); then
      echo "gemini-with-channel: $quick_count quick exits, giving up loop" >&2
      exit $rc
    fi
  else
    quick_count=0
  fi
  publish heartbeat "gemini/$PANE respawn (rc=$rc, ran ${end_ts}s-${start_ts}s)"
  sleep 1
  # Renew session id so the new instance has a fresh tag in metadata.
  SESSION_ID="$(uuidgen | tr 'A-Z' 'a-z')"
done
