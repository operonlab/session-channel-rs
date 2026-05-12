# Generic CLI adapter (minimal recipe)

If your CLI is not Claude Code / Codex / Gemini, you can still plug it
into session-channel with three small pieces:

## 1. Install the worker rule

Paste `examples/worker-rule.md` into wherever your CLI reads its system
instructions (top of `CLAUDE.md`-like file, `AGENTS.md`, a custom
`--system-prompt`, etc.).

## 2. Lifecycle: announce / heartbeat / leave

Wrap your CLI launch in a tiny shell script. Below is the minimum that
makes the dashboard show your agent as alive:

```bash
#!/usr/bin/env bash
set -u

PANE="${TMUX_PANE:-pid-$$}"
HOST="$(hostname -s)"
CLI="my-cli"   # change to your CLI's short name
ROLE="${CHANNEL_ROLE:-worker}"

# Resolve session-channel install (env > $HOME/.session-channel > script-dir)
SESSION_CHANNEL_HOME="${SESSION_CHANNEL_HOME:-$HOME/.session-channel}"
CHANNEL="${SESSION_CHANNEL_HOME}/cli/channel.py"

meta() {
  printf '{"v":1,"host":"%s","pane":"%s","cli":"%s","role":"%s","ts":%s}' \
    "$HOST" "$PANE" "$CLI" "$ROLE" "$(date +%s)"
}

publish() {
  "$CHANNEL" send agents "$2" --tag "$1" --meta "$(meta)" >/dev/null 2>&1 || true
}

publish announce "$CLI/$PANE started"
( while sleep 60; do publish heartbeat "$CLI/$PANE idle"; done ) &
HB=$!
trap "kill $HB 2>/dev/null; publish leave \"$CLI/$PANE left\"" EXIT INT TERM

# Replace the next line with your CLI's launch command
exec my-cli "$@"
```

## 3. Completion reporting

The worker rule already mandates this: when your CLI finishes a dispatched
task, it must execute the verbatim shell command after `after completion run:`.

That command is always of the form:

```bash
channel send tasks "<task_id>: done" --tag done \
  --meta '{"v":1,"task_id":"<task_id>","status":"ok","summary":"..."}'
```

As long as your CLI runs that command, the dispatch loop closes correctly
and `channel tasks --pending` will show the task as resolved.

## 4. Verify

```bash
# In one terminal, run your wrapper
./my-cli-with-channel.sh

# In another
channel agents --within 30
# Expect: my-cli/%<pane>
```
