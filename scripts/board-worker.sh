#!/bin/bash
# Worker loop — claim board task, simulate work with heartbeat, complete.
# Usage:
#   board-worker.sh --board <board_id> --pane <pane_id> [--simulate-seconds 10]
#
# Designed to run inside a tmux pane spawned by pane-wrapper.sh.
# Polls /api/board/{id}/claim every 3s. On claim:
#   1. Print task desc
#   2. Send heartbeat every 10s during simulated work
#   3. Send progress percent at 25/50/75
#   4. Complete with TaskResult {status:ok, payload:{ran_in:<pane>}}
# Loop forever until SIGTERM.

set -u

BOARD=""
PANE=""
SIMULATE=10
CHANNEL_URL="${SESSION_CHANNEL_URL:-http://localhost:10101}"
CHANNEL_KEY="${SESSION_CHANNEL_KEY:-change-me-in-production}"

_log() { echo "[board-worker] $*" >&2; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --board)             BOARD="$2"; shift 2 ;;
    --pane)              PANE="$2"; shift 2 ;;
    --simulate-seconds)  SIMULATE="$2"; shift 2 ;;
    --channel-url)       CHANNEL_URL="$2"; shift 2 ;;
    --channel-key)       CHANNEL_KEY="$2"; shift 2 ;;
    *)                   _log "unknown arg: $1"; shift ;;
  esac
done

if [[ -z "$BOARD" || -z "$PANE" ]]; then
  echo "Usage: board-worker.sh --board <board_id> --pane <pane_id> [--simulate-seconds 10]" >&2
  exit 2
fi

HAS_JQ=0
command -v jq >/dev/null 2>&1 && HAS_JQ=1

# JSON field extractor: prefer jq, fallback to python
_jget() {
  local json="$1" path="$2"
  if [[ "$HAS_JQ" -eq 1 ]]; then
    echo "$json" | jq -r "$path // empty" 2>/dev/null
  else
    ~/.local/bin/python3 -c "
import json, sys
data = json.loads('''$json''') if '''$json''' else {}
path = '$path'.lstrip('.').split('.')
cur = data
for p in path:
    if isinstance(cur, dict):
        cur = cur.get(p)
    else:
        cur = None
        break
print(cur if cur is not None else '')
" 2>/dev/null
  fi
}

_post() {
  local url="$1" body="$2"
  curl -s --max-time 5 -X POST "$CHANNEL_URL$url" \
    -H "x-local-key: $CHANNEL_KEY" \
    -H 'Content-Type: application/json' \
    -d "$body" 2>/dev/null
}

_heartbeat_loop() {
  local task_id="$1" lease=30
  while :; do
    sleep 10
    _post "/api/board/$BOARD/heartbeat" \
      "{\"task_id\":\"$task_id\",\"pane_id\":\"$PANE\",\"lease_seconds\":$lease}" >/dev/null
  done
}

_progress() {
  local task_id="$1" pct="$2"
  _post "/api/board/$BOARD/progress" \
    "{\"task_id\":\"$task_id\",\"pane_id\":\"$PANE\",\"percent\":$pct}" >/dev/null
}

_complete() {
  local task_id="$1"
  local body
  body="$(~/.local/bin/python3 -c "
import json
print(json.dumps({
    'task_id': '$task_id',
    'pane_id': '$PANE',
    'result': {'status':'ok','payload':{'ran_in':'$PANE'}}
}))" 2>/dev/null)"
  _post "/api/board/$BOARD/complete" "$body" >/dev/null
}

_log "starting worker board=$BOARD pane=$PANE simulate=${SIMULATE}s"

trap 'kill $(jobs -p) 2>/dev/null; exit 0' INT TERM

while :; do
  CLAIM_RESP="$(_post "/api/board/$BOARD/claim" \
    "{\"pane_id\":\"$PANE\",\"count\":1}")"

  if [[ -z "$CLAIM_RESP" ]]; then
    _log "claim returned empty (server down?), sleep 5"
    sleep 5
    continue
  fi

  STATUS="$(_jget "$CLAIM_RESP" '.status')"

  case "$STATUS" in
    no_tasks|"")
      sleep 3
      continue
      ;;
    caps_mismatch|busy|locked)
      _log "claim status=$STATUS, backoff 5s"
      sleep 5
      continue
      ;;
    ok|claimed)
      ;;
    *)
      _log "unknown status=$STATUS, body=$CLAIM_RESP"
      sleep 5
      continue
      ;;
  esac

  TASK_ID="$(_jget "$CLAIM_RESP" '.tasks[0].id')"
  [[ -z "$TASK_ID" ]] && TASK_ID="$(_jget "$CLAIM_RESP" '.task.id')"
  [[ -z "$TASK_ID" ]] && TASK_ID="$(_jget "$CLAIM_RESP" '.id')"
  TASK_DESC="$(_jget "$CLAIM_RESP" '.tasks[0].desc')"
  [[ -z "$TASK_DESC" ]] && TASK_DESC="$(_jget "$CLAIM_RESP" '.task.desc')"

  if [[ -z "$TASK_ID" ]]; then
    _log "no task_id in claim response: $CLAIM_RESP"
    sleep 3
    continue
  fi

  _log "claimed task_id=$TASK_ID desc=${TASK_DESC:-<none>}"

  _heartbeat_loop "$TASK_ID" &
  HB_PID=$!

  # Simulate work with progress at 25/50/75
  Q=$(( SIMULATE / 4 ))
  [[ $Q -lt 1 ]] && Q=1

  sleep "$Q"; _progress "$TASK_ID" 25
  sleep "$Q"; _progress "$TASK_ID" 50
  sleep "$Q"; _progress "$TASK_ID" 75
  sleep "$Q"

  kill "$HB_PID" 2>/dev/null
  wait "$HB_PID" 2>/dev/null

  _complete "$TASK_ID"
  _log "completed task_id=$TASK_ID"
done
