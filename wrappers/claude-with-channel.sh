#!/bin/bash
# claude-with-channel — launch Claude Code inside session-channel.
#
# Claude Code already publishes announce/heartbeat/leave via the
# hook-dispatcher (Go binary in stations/hook-dispatcher/). What it
# does NOT do is subscribe to the bus — tasks dispatched to a Claude
# pane only arrive via the orchestrator's `tmux send-keys` (the
# `--notify` path of `channel race / debate`).
#
# This wrapper closes the loop by starting the same SSE listener the
# codex/gemini wrappers use, so Claude panes also receive
# push-delivery (and the `--no-notify` race path works for Claude).
#
# Usage (typically from a relay pool spawn or manual launch):
#   claude-with-channel [claude args...]
#
# Env overrides:
#   CHANNEL_DRY_RUN  — when set, don't exec claude; just smoke the SSE
#                      listener + trap path.
#   CHANNEL_CLAUDE_FLAGS — extra flags appended to `claude` (default
#                          --dangerously-skip-permissions).

set -u

PANE="${TMUX_PANE:-pid-$$}"

# Resolve session-channel install home (same logic as codex/gemini wrappers).
if [[ -z "${SESSION_CHANNEL_HOME:-}" ]]; then
  if [[ -d "$HOME/.session-channel/cli" ]]; then
    SESSION_CHANNEL_HOME="$HOME/.session-channel"
  else
    SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    SESSION_CHANNEL_HOME="${SCRIPT_DIR%/wrappers}"
  fi
fi

# Background SSE listener — push-delivery for tasks targeted at this pane.
# Coordinates with orchestrator's --notify path via /tmp/sc-nudged-${pane}.txt.
SSE_PID=""
source "${SESSION_CHANNEL_HOME}/wrappers/sse_subscribe.sh" 2>/dev/null || true
if command -v start_sse_listener >/dev/null 2>&1; then
  start_sse_listener
fi

cleanup() {
  [ -n "$SSE_PID" ] && kill "$SSE_PID" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

if [[ -n "${CHANNEL_DRY_RUN:-}" ]]; then
  echo "claude-with-channel: dry-run (SSE listener started; sleeping briefly)" >&2
  sleep 1
  exit 0
fi

# Run claude as a child (NOT exec) so the EXIT trap fires on quit and
# the SSE listener gets cleaned up. Claude's own hooks publish announce/
# heartbeat/leave to the agents topic — we don't duplicate those here.
CLAUDE_FLAGS="${CHANNEL_CLAUDE_FLAGS:---dangerously-skip-permissions}"
# shellcheck disable=SC2086  # word-splitting intentional for flag list
claude $CLAUDE_FLAGS "$@"
exit $?
