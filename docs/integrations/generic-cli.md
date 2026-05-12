# Generic CLI integration

If your CLI is not Claude Code / Codex / Gemini, you can still plug it
into session-channel with three small pieces.

## 1. Worker rule

Paste [`examples/worker-rule.md`](../../examples/worker-rule.md) into
wherever your CLI reads its system instructions (top of a `CLAUDE.md`-like
file, an `AGENTS.md`, a custom `--system-prompt`, etc.).

If your CLI does not let users inject persistent instructions, you must
prepend the rule to every dispatched prompt — but in practice every
modern coding CLI supports some form of "always read this file at
startup".

## 2. Wrapper (lifecycle: announce / heartbeat / leave)

A 20-line shell wrapper is enough to make the dashboard show your agent
as alive. Save as `my-cli-with-channel.sh`:

```bash
#!/usr/bin/env bash
set -u

PANE="${TMUX_PANE:-pid-$$}"
HOST="$(hostname -s)"
CLI="my-cli"   # short name shown in the dashboard
ROLE="${CHANNEL_ROLE:-worker}"
HB_INTERVAL="${CHANNEL_HEARTBEAT_INTERVAL:-60}"

# Resolve session-channel install (env > $HOME/.session-channel > script-dir).
if [[ -z "${SESSION_CHANNEL_HOME:-}" ]]; then
  if [[ -d "$HOME/.session-channel/cli" ]]; then
    SESSION_CHANNEL_HOME="$HOME/.session-channel"
  else
    SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    SESSION_CHANNEL_HOME="${SCRIPT_DIR%/wrappers}"
  fi
fi
CHANNEL="${SESSION_CHANNEL_HOME}/cli/channel.py"

meta() {
  printf '{"v":1,"host":"%s","pane":"%s","cli":"%s","role":"%s","ts":%s}' \
    "$HOST" "$PANE" "$CLI" "$ROLE" "$(date +%s)"
}

publish() {
  "$CHANNEL" send agents "$2" --tag "$1" --meta "$(meta)" >/dev/null 2>&1 || true
}

publish announce "$CLI/$PANE started"
( while sleep "$HB_INTERVAL"; do publish heartbeat "$CLI/$PANE idle"; done ) &
HB=$!
trap "kill $HB 2>/dev/null; publish leave \"$CLI/$PANE left\"" EXIT INT TERM

# Replace the next line with your CLI's launch command.
exec my-cli "$@"
```

## 3. Per-turn observability (optional)

If your CLI exposes a "tool called" hook (most modern CLIs do — under
names like `notify`, `pre_tool`, `on_tool_call`, etc.), point it at
`examples/hooks/session_channel.py PreToolUse`. The hook expects a JSON
payload on stdin with at least:

```json
{
  "tool_name": "ToolName",
  "tool_input": { "...": "..." },
  "cwd": "/path/to/cwd",
  "session_id": "..."
}
```

and will publish a `tool` event to the agents topic. Adapt the input
shape if your CLI uses different field names — the hook is small and
copy-friendly.

## 4. Completion reporting

The worker rule already mandates this: when your CLI finishes a
dispatched task, it must execute the verbatim shell command after
`after completion run:`. That command is always of the form:

```bash
channel send tasks "<task_id>: done" --tag done \
  --meta '{"v":1,"task_id":"<task_id>","status":"ok","summary":"..."}'
```

As long as your CLI runs that command, the dispatch loop closes
correctly and `channel tasks --pending` will show the task as resolved.

## 5. E2E smoke test

```bash
# In one terminal, run your wrapper
chmod +x ./my-cli-with-channel.sh
./my-cli-with-channel.sh

# In another
channel agents --within 30
# Expect: my-cli/%<pane>

# Dispatch
channel send tasks "Acknowledge with: ok" --tag assign \
  --meta '{"v":1,"task_id":"generic-smoke","target_pane":"%<pane>","prompt":"Reply ok"}' \
  --notify-target '%<pane>'
channel tasks --pending
```

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| No `announce` shows up | `cli/channel.py` not executable | `chmod +x cli/channel.py` |
| Heartbeats published but `agents` query returns empty | `--within` too short relative to interval | `channel agents --within $((HB_INTERVAL * 2))` |
| `leave` never published | Wrapper killed with `kill -9` | Use SIGTERM; `trap … EXIT INT TERM` only fires on graceful exit |
| Worker accepts task but no `done` | Rule not picked up — your CLI didn't load the instructions | Verify the rule path; tail the CLI's startup log |
| Worker runs the `done` command as text instead of executing it | Your CLI is interpreting it as a chat message | Make sure your CLI is in a "tool/shell-capable" mode (e.g. Codex bypass, Gemini YOLO) |
