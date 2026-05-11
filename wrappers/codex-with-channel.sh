#!/bin/bash
# codex-with-channel — launch Codex CLI inside session-channel.
#
# Replaces direct `codex` invocation in the relay pool so that:
#   - Codex pane appears in `channel agents` immediately on launch (announce)
#   - Codex pane disappears on exit (leave, via trap)
#   - Each agent turn publishes a heartbeat to topic=agents
#     (via session-scoped notify hook, NOT touching ~/.codex/config.toml)
#   - Idle pane keeps a periodic heartbeat (background loop, 60s)
#   - Bypass mode is on by default (--dangerously-bypass-approvals-and-sandbox)
#
# Usage (typically from relay pool spawn):
#   codex-with-channel [codex args...]
#
# Env overrides:
#   CHANNEL_ROLE     — "worker" (default) or "leader"
#   CHANNEL_DRY_RUN  — when set, announce/leave only; do NOT exec codex
#                     (used by tests to verify wiring without spawning Codex)
#   CHANNEL_HEARTBEAT_INTERVAL — idle heartbeat interval in seconds (default: 60)

set -u

PANE="${TMUX_PANE:-pid-$$}"
HOST="$(hostname -s)"
ROLE="${CHANNEL_ROLE:-worker}"
HB_INTERVAL="${CHANNEL_HEARTBEAT_INTERVAL:-60}"

CHANNEL=/Users/joneshong/workshop/stations/session-channel/cli/channel.py
HOOK=/Users/joneshong/workshop/stations/session-channel/wrappers/codex_channel_hook.py
PY=/Users/joneshong/.local/bin/python3

if [[ ! -x "$CHANNEL" ]]; then
  echo "codex-with-channel: channel CLI not found at $CHANNEL" >&2
  exec codex "$@"
fi

build_meta() {
  local ts
  ts=$(date +%s)
  printf '{"v":1,"host":"%s","pane":"%s","cli":"codex","role":"%s","ts":%s}' \
    "$HOST" "$PANE" "$ROLE" "$ts"
}

publish() {
  local tag="$1" msg="$2"
  "$CHANNEL" send agents "$msg" --tag "$tag" --meta "$(build_meta)" \
    >/dev/null 2>&1 || true
}

publish announce "codex/$PANE started"

# Background heartbeat: keeps pane visible in `channel agents` when Codex
# is idle (no turn completions for a while).
(
  while sleep "$HB_INTERVAL"; do
    publish heartbeat "codex/$PANE idle heartbeat"
  done
) &
HB_PID=$!

cleanup() {
  kill "$HB_PID" 2>/dev/null || true
  publish leave "codex/$PANE left"
}
trap cleanup EXIT INT TERM

if [[ -n "${CHANNEL_DRY_RUN:-}" ]]; then
  # Test mode: prove announce/leave wire correctly without spawning Codex.
  echo "codex-with-channel: dry-run (announce sent, sleeping briefly, then leave)" >&2
  sleep 1
  exit 0
fi

# Session-scoped notify override. oh-my-codex's global notify in
# ~/.codex/config.toml stays untouched; only this Codex process uses our hook.
#
# Plain invocation (not `exec`): `exec` would replace this shell image, which
# discards the EXIT trap and leaves a stale heartbeat in `channel agents`
# after Codex quits. Running codex as a child preserves the trap so the
# `leave` event always fires on exit (verified Phase E 2026-05-11).
codex \
  --dangerously-bypass-approvals-and-sandbox \
  -c "notify=[\"$PY\", \"$HOOK\"]" \
  "$@"
exit $?
