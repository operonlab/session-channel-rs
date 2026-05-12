# Gemini CLI integration

Gemini CLI integrates via the `gemini-with-channel.sh` wrapper.

## 1. Worker rule

Append [`examples/worker-rule.md`](../../examples/worker-rule.md) to your
project's `GEMINI.md`. Gemini reads it at session start.

## 2. Wrapper

```bash
~/.session-channel/wrappers/gemini-with-channel.sh

# In a tmux pane (relay-pool style)
tmux send-keys -t '%7' "~/.session-channel/wrappers/gemini-with-channel.sh" Enter
```

What the wrapper does:

- Publishes `announce` on launch and `leave` on exit (via `trap`).
- Pre-allocates `--session-id <uuid>`; the UUID is included in every
  channel meta so observers can correlate panes across restarts.
  Override via `CHANNEL_SESSION_ID`.
- Background idle loop publishes a `heartbeat` every 60s. Gemini's
  `hooks` subcommand (as of v0.41.x) only supports `migrate` — there is
  no per-turn notify equivalent — so the loop is the only heartbeat
  source.
- YOLO mode (`-y`) is on by default — required so the worker can run
  the explicit shell command after `after completion run:`.

Env knobs:

| Var | Effect |
|---|---|
| `SESSION_CHANNEL_HOME` | Install root (auto-detected if unset) |
| `CHANNEL_ROLE` | `worker` (default) or `leader` |
| `CHANNEL_DRY_RUN` | Announce / leave only — do NOT exec gemini |
| `CHANNEL_HEARTBEAT_INTERVAL` | Idle heartbeat interval (default 60s) |
| `CHANNEL_SESSION_ID` | Override the pre-allocated session UUID |

## 3. Supervisor entry

```yaml
relay_pool:
  workers:
    - pane_id: "%7"
      cli_type: gemini
      enabled: true
```

## 4. E2E smoke test

```bash
CHANNEL_DRY_RUN=1 ~/.session-channel/wrappers/gemini-with-channel.sh
# Expect: "gemini-with-channel: dry-run (announce sent, brief sleep, then leave)"

channel agents --within 30
# Expect: gemini/%<pane> entry with role=worker and session_id meta

channel send tasks "Reply with only the word: ok" --tag assign \
  --meta '{"v":1,"task_id":"gemini-smoke","target_pane":"%7","prompt":"Reply with only the word: ok"}' \
  --notify-target '%7'
channel tasks --pending
```

## Known quirks

### `/rename` reflex

Gemini occasionally interprets incoming worker prompts as a `/rename`
target (e.g. when the prompt contains short phrases that look like
filenames). The worker rule explicitly forbids this, but Gemini sometimes
issues both: the rename attempt and the explicit `channel send ... --tag
done`. The rename always errors out ("No such file or directory") and the
correct `done` event is still published. Cosmetic side-effect only — the
dispatch loop still closes.

### Self-exit on idle (rare)

Gemini has been observed to self-exit after several minutes of idle. The
supervisor respawns the wrapper after `grace_seconds`.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Wrapper falls through to `exec gemini` | `cli/channel.py` not executable | `chmod +x cli/channel.py` |
| `agents` topic shows no `gemini/...` entry | `gemini` not on PATH | Install: `npm install -g @google/gemini-cli`, then re-spawn |
| Heartbeat never updates | Wrapper background `sleep` was killed | Inspect tmux pane — there should be a backgrounded shell loop alongside Gemini |
| Tasks accepted but no `done` published | YOLO mode not on, Gemini asked for confirmation and stalled | Confirm `-y` is in the wrapper invocation (default), or override args via the wrapper |
