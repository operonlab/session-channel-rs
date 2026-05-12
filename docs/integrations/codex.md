# Codex CLI integration

Codex (OpenAI's CLI) integrates via the `codex-with-channel.sh` wrapper +
the `codex_channel_hook.py` notify hook.

## 1. Worker rule

Append [`examples/worker-rule.md`](../../examples/worker-rule.md) to your
project's `AGENTS.md` (or to a system-wide instructions file Codex reads).

## 2. Wrapper

Spawn Codex via the bundled wrapper rather than the raw `codex` binary:

```bash
# Foreground
~/.session-channel/wrappers/codex-with-channel.sh

# In a tmux pane (relay-pool style)
tmux send-keys -t '%6' "~/.session-channel/wrappers/codex-with-channel.sh" Enter
```

What the wrapper does:

- Publishes `announce` on launch and `leave` on exit (via shell `trap`).
- Passes `-c notify=[…codex_channel_hook.py]` so every turn completion
  publishes a `heartbeat` event to the `agents` topic. This is
  **session-scoped** — the wrapper does not modify
  `~/.codex/config.toml`. Any global notify config you have (e.g. an
  oh-my-codex desktop notifier) keeps working.
- Background idle loop publishes a heartbeat every 60s
  (`CHANNEL_HEARTBEAT_INTERVAL` to override).
- Codex runs in bypass mode (`--dangerously-bypass-approvals-and-sandbox`)
  by default — required so the worker can execute the explicit shell
  command after `after completion run:`.

Env knobs the wrapper honours:

| Var | Effect |
|---|---|
| `SESSION_CHANNEL_HOME` | Install root (auto-detected if unset) |
| `SESSION_CHANNEL_PY` | Python interpreter (defaults to `python3` on PATH) |
| `CHANNEL_ROLE` | `worker` (default) or `leader` |
| `CHANNEL_DRY_RUN` | Announce / leave only — do NOT exec codex (used by tests) |
| `CHANNEL_HEARTBEAT_INTERVAL` | Idle heartbeat interval (default 60s) |

## 3. Supervisor entry

```yaml
relay_pool:
  workers:
    - pane_id: "%6"
      cli_type: codex
      enabled: true
```

`scripts/supervisor.py` will respawn the wrapper if pane `%6` returns to
its shell prompt (after the `relay_pool.grace_seconds` window —
default 120s).

## 4. E2E smoke test

```bash
# Wrapper-level sanity check (no Codex spawn)
CHANNEL_DRY_RUN=1 ~/.session-channel/wrappers/codex-with-channel.sh
# Expect: "codex-with-channel: dry-run (announce sent, sleeping briefly, then leave)"

# After the wrapper has been spawned in a real pane
channel agents --within 30
# Expect: codex/%<pane> entry with role=worker

# Dispatch
channel send tasks "Print 'codex ok'" --tag assign \
  --meta '{"v":1,"task_id":"codex-smoke","target_pane":"%6","prompt":"Print codex ok"}' \
  --notify-target '%6'
channel tasks --pending
```

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `exec codex "$@"` fallback fires | `cli/channel.py` not executable or wrong `SESSION_CHANNEL_HOME` | Re-run `./install.sh`, then `chmod +x cli/channel.py` |
| No `heartbeat` events on agents topic | Codex `notify` hook not loaded — usually because `$PY` interpreter doesn't exist | Set `$SESSION_CHANNEL_PY` to a real `python3` |
| Wrapper exits immediately | `set -u` tripped by an unset env var | Run with `bash -x wrappers/codex-with-channel.sh` and trace the unbound variable |
| `leave` event never published | Wrapper was killed with `kill -9`, bypassing the trap | Use `kill -TERM` (SIGTERM); the wrapper's `trap cleanup EXIT INT TERM` will fire |
| Tasks completing but channel `done` not received | Codex didn't run the explicit shell command from `after completion run:` | Confirm the worker-rule file is in `AGENTS.md`; Codex prefers the project-local file |
