#!/bin/bash
# Universal pane wrapper — advertise capability before spawning a CLI,
# release on exit. Lets Codex/Gemini/etc participate in session-channel
# capability registry without needing a Claude Code SessionStart hook.
#
# Usage:
#   pane-wrapper.sh --cli-type codex --pane-id pane-codex-1 -- codex
#   pane-wrapper.sh --cli-type gemini --pane-id pane-gem-1 -- gemini chat
#   pane-wrapper.sh --cli-type claude-code -- claude   # auto-detect pane_id from $TMUX_PANE
#
# Detected automatically:
# - mcps: read ~/.mcpproxy/mcp_config.json
# - skills: scan ~/.claude/skills/
# - pane_id: $TMUX_PANE if not given

set -u

CLI_TYPE=""
PANE_ID=""
CHANNEL_URL="${SESSION_CHANNEL_URL:-http://localhost:10101}"
CHANNEL_KEY="${SESSION_CHANNEL_KEY:-change-me-in-production}"
MCPS_OVERRIDE=""
SKILLS_OVERRIDE=""

_warn() { echo "[pane-wrapper] WARN: $*" >&2; }
_info() { echo "[pane-wrapper] $*" >&2; }

# Parse args until --
while [[ $# -gt 0 ]]; do
  case "$1" in
    --cli-type)     CLI_TYPE="$2"; shift 2 ;;
    --pane-id)      PANE_ID="$2"; shift 2 ;;
    --channel-url)  CHANNEL_URL="$2"; shift 2 ;;
    --channel-key)  CHANNEL_KEY="$2"; shift 2 ;;
    --mcps)         MCPS_OVERRIDE="$2"; shift 2 ;;
    --skills)       SKILLS_OVERRIDE="$2"; shift 2 ;;
    --)             shift; break ;;
    *)              _warn "unknown arg: $1"; shift ;;
  esac
done

if [[ -z "$CLI_TYPE" ]]; then
  echo "Usage: pane-wrapper.sh --cli-type <claude-code|codex|gemini|copilot|unknown> [--pane-id ID] -- <cmd...>" >&2
  exit 2
fi

# Default pane_id from $TMUX_PANE or PID
if [[ -z "$PANE_ID" ]]; then
  if [[ -n "${TMUX_PANE:-}" ]]; then
    PANE_ID="pane-${TMUX_PANE//%/}"
  else
    PANE_ID="pane-$$"
  fi
fi

# Detect MCPs from ~/.mcpproxy/mcp_config.json
_detect_mcps() {
  if [[ -n "$MCPS_OVERRIDE" ]]; then
    echo "$MCPS_OVERRIDE"
    return
  fi
  local cfg="$HOME/.mcpproxy/mcp_config.json"
  if [[ ! -f "$cfg" ]]; then
    echo ""
    return
  fi
  ~/.local/bin/python3 -c "
import json, sys
try:
    with open('$cfg') as f:
        data = json.load(f)
    servers = data.get('mcpServers', {}) or data.get('mcp_servers', {})
    print(','.join(sorted(servers.keys())))
except Exception as e:
    sys.stderr.write(f'mcps parse fail: {e}\n')
    print('')
" 2>/dev/null || echo ""
}

# Detect skills from ~/.claude/skills/ first-level dirs
_detect_skills() {
  if [[ -n "$SKILLS_OVERRIDE" ]]; then
    echo "$SKILLS_OVERRIDE"
    return
  fi
  local dir="$HOME/.claude/skills"
  if [[ ! -d "$dir" ]]; then
    echo ""
    return
  fi
  ls -1 "$dir" 2>/dev/null | head -50 | tr '\n' ',' | sed 's/,$//'
}

# Build JSON list literal from comma-separated string: "a,b" -> ["a","b"]
_to_json_array() {
  local csv="$1"
  if [[ -z "$csv" ]]; then
    echo "[]"
    return
  fi
  ~/.local/bin/python3 -c "
import json, sys
csv = '''$csv'''
items = [x.strip() for x in csv.split(',') if x.strip()]
print(json.dumps(items))
" 2>/dev/null || echo "[]"
}

MCPS_CSV="$(_detect_mcps)"
SKILLS_CSV="$(_detect_skills)"
MCPS_JSON="$(_to_json_array "$MCPS_CSV")"
SKILLS_JSON="$(_to_json_array "$SKILLS_CSV")"
NOW_EPOCH="$(date +%s)"

BODY="$(~/.local/bin/python3 -c "
import json
print(json.dumps({
    'pane_id': '$PANE_ID',
    'cli_type': '$CLI_TYPE',
    'mcps': $MCPS_JSON,
    'skills': $SKILLS_JSON,
    'started_at': $NOW_EPOCH,
    'last_seen': $NOW_EPOCH,
}))
" 2>/dev/null)"

if [[ -z "$BODY" ]]; then
  BODY="{\"pane_id\":\"$PANE_ID\",\"cli_type\":\"$CLI_TYPE\",\"mcps\":[],\"skills\":[],\"started_at\":$NOW_EPOCH,\"last_seen\":$NOW_EPOCH}"
fi

_info "advertising pane_id=$PANE_ID cli_type=$CLI_TYPE mcps=$(echo "$MCPS_CSV" | awk -F, '{print NF}') skills=$(echo "$SKILLS_CSV" | awk -F, '{print NF}')"

if ! curl -s --max-time 3 -X POST "$CHANNEL_URL/api/panes/advertise" \
    -H "x-local-key: $CHANNEL_KEY" \
    -H 'Content-Type: application/json' \
    -d "$BODY" >/dev/null 2>&1; then
  _warn "advertise to $CHANNEL_URL failed (continuing without registry)"
fi

_release_pane() {
  curl -s --max-time 2 -X DELETE "$CHANNEL_URL/api/panes/$PANE_ID" \
    -H "x-local-key: $CHANNEL_KEY" >/dev/null 2>&1 || true
}
trap _release_pane EXIT INT TERM

if [[ $# -eq 0 ]]; then
  _warn "no command after --; nothing to exec"
  exit 0
fi

exec "$@"
