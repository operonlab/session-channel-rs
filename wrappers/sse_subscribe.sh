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
    # Backoff state: exponential 1s→2s→4s→…cap 60s, reset on success (≥5s).
    local _backoff=1
    local _nudge_write_count=0   # counter for dedup-log rotation check

    # Outer loop: reconnect on SSE drop. curl exits on connection close;
    # service binary keep-alive is 30s.
    while true; do
      local _conn_start
      _conn_start=$(date +%s)

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

                # Dedup-log rotation: check every 100 writes; truncate to last 200
                # lines when file exceeds 500 lines. Old task_ids never resurface so
                # dropping history is safe.
                _nudge_write_count=$(( _nudge_write_count + 1 ))
                if [ $(( _nudge_write_count % 100 )) -eq 0 ]; then
                  local _log_lines
                  _log_lines=$(wc -l < "$nudge_log" 2>/dev/null || echo 0)
                  if [ "$_log_lines" -gt 500 ]; then
                    tail -200 "$nudge_log" > "$nudge_log.tmp" \
                      && mv "$nudge_log.tmp" "$nudge_log"
                  fi
                fi

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

      # curl pipeline closed — apply exponential backoff with ±20% jitter.
      # A connection lasting ≥5s counts as success → reset backoff to 1s.
      local _conn_end
      _conn_end=$(date +%s)
      local _conn_dur=$(( _conn_end - _conn_start ))
      if [ "$_conn_dur" -ge 5 ]; then
        _backoff=1
      fi

      # jitter: ±20% of _backoff (integer arithmetic via RANDOM 0..32767)
      # Simplified: sleep = _backoff*4/5 + RANDOM % (_backoff*2/5 + 1)
      local _jitter_range=$(( _backoff * 2 / 5 + 1 ))
      local _sleep=$(( _backoff * 4 / 5 + RANDOM % _jitter_range ))
      [ "$_sleep" -lt 1 ] && _sleep=1
      sleep "$_sleep"

      # Double for next iteration, cap at 60s.
      _backoff=$(( _backoff * 2 ))
      [ "$_backoff" -gt 60 ] && _backoff=60
    done
  ) &
  SSE_PID=$!
}
