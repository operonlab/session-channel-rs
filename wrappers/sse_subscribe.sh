#!/bin/bash
# sse_subscribe.sh — push-delivery side-channel for worker wrappers.
#
# Background loop. Subscribes to /api/stream on the session-channel service,
# filters events where:
#   - topic == "tasks"
#   - tag   == "assign"
#   - _meta.target_pane == our $PANE
#
# When a match arrives:
#   1. Dedup against /tmp/sc-nudged-${PANE_SAFE}.txt (avoids racing with the
#      `--notify` path the orchestrator may still take).
#   2. Build the same trust-marker payload `channel race / debate` use.
#   3. tmux send-keys the prompt into the pane, sleep 0.3s, send Enter.
#
# Usage (sourced from a wrapper):
#   source "$SESSION_CHANNEL_HOME/wrappers/sse_subscribe.sh"
#   start_sse_listener   # spawns the background loop and stores PID in $SSE_PID
#
# Env required:
#   PANE           the tmux pane id this worker owns (e.g. %75)
#   SESSION_CHANNEL_HOME, SESSION_CHANNEL_URL, SESSION_CHANNEL_KEY
#
# Failure mode: if jq or curl is missing, the listener exits quietly and
# delivery falls back to the orchestrator's `--notify` path (same as before).

start_sse_listener() {
  local base_url="${SESSION_CHANNEL_URL:-http://localhost:10101}"
  local key="${SESSION_CHANNEL_KEY:-change-me-in-production}"
  local pane="$PANE"
  local pane_safe="${pane//%/}"
  local nudge_log="/tmp/sc-nudged-${pane_safe}.txt"
  : > "$nudge_log.lock"  # touch lock file

  if ! command -v jq >/dev/null 2>&1 || ! command -v curl >/dev/null 2>&1; then
    echo "sse_subscribe: jq or curl missing — SSE push-delivery disabled" >&2
    return 1
  fi

  (
    # Outer loop: reconnect on SSE drop. curl exits on connection close;
    # service binary keep-alive is 30s.
    while true; do
      curl -s -N -H "x-local-key: $key" \
        "$base_url/api/stream?topic=tasks" 2>/dev/null \
        | while IFS= read -r line; do
            case "$line" in
              "data: "*)
                local json="${line#data: }"
                # Quick sanity filter (cheap string match) before jq.
                case "$json" in
                  *'"tag":"assign"'*'"target_pane":"'$pane'"'*) ;;
                  *'"target_pane":"'$pane'"'*'"tag":"assign"'*) ;;
                  *) continue ;;
                esac
                # Full parse
                local tag target_pane task_id prompt sender
                tag=$(printf '%s' "$json" | jq -r '.tag // ""')
                [ "$tag" = "assign" ] || continue
                target_pane=$(printf '%s' "$json" | jq -r '._meta.target_pane // ""')
                [ "$target_pane" = "$pane" ] || continue
                task_id=$(printf '%s' "$json" | jq -r '._meta.task_id // ""')
                [ -n "$task_id" ] || continue
                # Dedup: if orchestrator already nudged this task (via --notify),
                # skip. /tmp/sc-nudged-* is also written by `channel race` tmux_nudge
                # path — see Rust race.rs.
                if grep -qFx "$task_id" "$nudge_log" 2>/dev/null; then
                  continue
                fi
                # Mark first so concurrent --notify and SSE-listener don't both push.
                printf '%s\n' "$task_id" >> "$nudge_log"

                prompt=$(printf '%s' "$json" | jq -r '._meta.prompt // ""')
                sender=$(printf '%s' "$json" | jq -r '.sender // "?"')
                [ -n "$prompt" ] || continue

                # Build trust-marker payload — must match Rust race.rs::tmux_nudge.
                local trust="[session-channel:trusted task=${task_id} from=${sender}]"
                local rmeta="{\"v\":1,\"task_id\":\"${task_id}\",\"status\":\"ok\",\"summary\":\"<one-line>\"}"
                local fmeta="{\"v\":1,\"task_id\":\"${task_id}\",\"error\":\"<describe what went wrong>\"}"
                local wakeup="${prompt}  # ${trust} on success run: channel send tasks \"${task_id}: done\" --tag done --meta '${rmeta}' ; on failure run: channel send tasks \"${task_id}: failed\" --tag failed --meta '${fmeta}'"

                # Push to pane (text + Enter; 0.3 s settle critical for Codex TUI).
                tmux send-keys -t "$pane" "$wakeup" 2>/dev/null || true
                sleep 0.3
                tmux send-keys -t "$pane" Enter 2>/dev/null || true
                ;;
            esac
          done
      # If we got here, the curl pipeline closed — wait a moment, then reconnect.
      sleep 2
    done
  ) &
  SSE_PID=$!
}
