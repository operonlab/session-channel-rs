#!/bin/bash
# launch-relay-pool.sh — bring up a cross-CLI pane pool in a tmux window
# (and have each pane self-advertise to the session-channel registry).
#
# Each pane runs an interactive CLI — session, skills and MCP state stay
# warm. tmux-relay can then route work across the pool with
# `route_to_capable_pane(...)`.
#
# Usage:
#   launch-relay-pool.sh [-w window_name] [-s session_name] -c claude:N -c codex:N -c gemini:N
#
#   -s session   tmux session to add to (default: current $TMUX session, or "default")
#   -w window    window name (default: "⚡pool")
#   -c spec      cli_type:count, repeatable. Supported cli_type values:
#                  claude  (runs `claude`)
#                  codex   (runs `codex`)
#                  gemini  (runs `gemini`)
#                  copilot (runs `copilot`)
#                  shell   (runs `zsh` — useful for placeholder / debug)
#   -k          kill existing window with the same name first
#   -h          show this help
#
# Examples:
#   launch-relay-pool.sh -c claude:1 -c codex:1 -c gemini:1
#   launch-relay-pool.sh -w ⚡crew -c claude:2 -c codex:1
#   launch-relay-pool.sh -k -c claude:3   # kill+rebuild a 3-CC pool
#
# After launch, verify with:
#   curl -s http://localhost:10101/api/panes -H "x-local-key: ${SESSION_CHANNEL_KEY:-change-me-in-production}" | jq

set -euo pipefail

WRAPPER="$(dirname "$0")/pane-wrapper.sh"
[[ -x "$WRAPPER" ]] || { echo "missing wrapper: $WRAPPER" >&2; exit 1; }

SESSION="${TMUX_SESSION:-}"
WINDOW="⚡pool"
KILL_OLD=0
declare -a SPECS=()

usage() { sed -n '2,30p' "$0"; exit "${1:-0}"; }

while getopts ":s:w:c:kh" opt; do
  case "$opt" in
    s) SESSION="$OPTARG" ;;
    w) WINDOW="$OPTARG" ;;
    c) SPECS+=("$OPTARG") ;;
    k) KILL_OLD=1 ;;
    h) usage 0 ;;
    *) usage 1 ;;
  esac
done

# Resolve session: explicit -s > $TMUX > "default"
if [[ -z "$SESSION" ]]; then
  if [[ -n "${TMUX:-}" ]]; then
    SESSION=$(tmux display-message -p '#{session_name}')
  else
    SESSION="default"
  fi
fi

# tmux session must exist
tmux has-session -t "$SESSION" 2>/dev/null || {
  echo "tmux session '$SESSION' not found; create it first or pass -s" >&2
  exit 2
}

[[ ${#SPECS[@]} -gt 0 ]] || { echo "no -c spec given (e.g. -c claude:1)" >&2; usage 1; }

# Resolve cli_type → command
cli_command() {
  case "$1" in
    claude|claude-code) echo "claude" ;;
    codex)              echo "codex" ;;
    gemini)             echo "gemini" ;;
    copilot)            echo "copilot --allow-all" ;;
    shell|zsh)          echo "zsh" ;;
    *) return 1 ;;
  esac
}

# Optional: kill stale pool window
if [[ "$KILL_OLD" == "1" ]]; then
  tmux kill-window -t "${SESSION}:${WINDOW}" 2>/dev/null || true
fi

# Tally total panes for layout
TOTAL=0
for spec in "${SPECS[@]}"; do
  count="${spec#*:}"
  TOTAL=$((TOTAL + count))
done

if [[ "$TOTAL" -lt 1 ]]; then
  echo "spec totals 0 panes" >&2
  exit 1
fi

# Create the pool window (first pane runs the first CLI).
SPEC_INDEX=0
PANE_INDEX=0

build_send_command() {
  # $1=cli_type, $2=index_within_pool
  local cli="$1" idx="$2"
  local cmd
  cmd=$(cli_command "$cli") || { echo "unknown cli_type: $cli" >&2; exit 3; }
  # pane-wrapper.sh fills pane-id from $TMUX_PANE automatically; we just
  # pass --cli-type so the registry tags it correctly. Use literal `exec`
  # so the wrapper replaces the shell instead of stacking.
  echo "exec '$WRAPPER' --cli-type $cli -- $cmd"
}

# Iterate specs in order, create one pane per count.
NEED_CREATE_WINDOW=1
for spec in "${SPECS[@]}"; do
  cli="${spec%%:*}"
  count="${spec#*:}"
  # Validate cli_type up-front so we fail fast.
  cli_command "$cli" >/dev/null
  for ((j=0; j<count; j++)); do
    if [[ "$NEED_CREATE_WINDOW" == "1" ]]; then
      tmux new-window -t "${SESSION}:" -n "$WINDOW"
      NEED_CREATE_WINDOW=0
    else
      tmux split-window -t "${SESSION}:${WINDOW}"
      tmux select-layout -t "${SESSION}:${WINDOW}" tiled >/dev/null
    fi
    target="${SESSION}:${WINDOW}.$(tmux display-message -t "${SESSION}:${WINDOW}" -p '#{pane_index}')"
    full_cmd=$(build_send_command "$cli" "$PANE_INDEX")
    tmux send-keys -t "$target" "$full_cmd" Enter
    PANE_INDEX=$((PANE_INDEX + 1))
  done
  SPEC_INDEX=$((SPEC_INDEX + 1))
done

tmux select-layout -t "${SESSION}:${WINDOW}" tiled >/dev/null

echo "✓ launched $TOTAL panes in ${SESSION}:${WINDOW}"
echo ""
echo "  panes:"
tmux list-panes -t "${SESSION}:${WINDOW}" -F "    #{pane_id} #{pane_current_command}" 2>&1
echo ""
echo "  give each CLI ~5 seconds to boot, then verify:"
echo "    curl -s http://localhost:10101/api/panes -H \"x-local-key: \${SESSION_CHANNEL_KEY:-change-me-in-production}\" | jq"
echo ""
echo "  attach to pool:   tmux select-window -t '${SESSION}:${WINDOW}'"
echo "  kill pool:        tmux kill-window -t '${SESSION}:${WINDOW}'"
