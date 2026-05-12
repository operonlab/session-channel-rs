# Codex CLI adapter

Codex (OpenAI's CLI) integrates via the `codex-with-channel.sh` wrapper +
the `codex_channel_hook.py` notify hook.

## 1. Install the worker rule

Append [`examples/worker-rule.md`](../worker-rule.md) to your project's
`AGENTS.md` (or to a system-wide instructions file Codex reads).

## 2. Spawn Codex via the wrapper

```bash
# Foreground
~/.session-channel/wrappers/codex-with-channel.sh

# In a tmux pane (relay-pool style)
tmux send-keys -t '%6' "~/.session-channel/wrappers/codex-with-channel.sh" Enter
```

What the wrapper does:

- Publishes `announce` on launch and `leave` on exit (via `trap`)
- Spawns Codex with `notify=[…codex_channel_hook.py]`, so every turn
  completion publishes a `heartbeat` event to the `agents` topic
- Idle background loop also publishes heartbeats every 60s (override:
  `CHANNEL_HEARTBEAT_INTERVAL`)

Important: the wrapper does **not** touch `~/.codex/config.toml`. The
notify hook is passed as a session-scoped `-c notify=…` arg, so any
global oh-my-codex notify config you have stays untouched.

## 3. Add to supervisor

```yaml
relay_pool:
  workers:
    - pane_id: "%6"
      cli_type: codex
      enabled: true
```

The supervisor will respawn the wrapper if the pane returns to a shell
prompt (after a 120s grace period — adjust via `relay_pool.grace_seconds`).

## 4. Smoke test

```bash
CHANNEL_DRY_RUN=1 ~/.session-channel/wrappers/codex-with-channel.sh
# Expect: "codex-with-channel: dry-run (announce sent, sleeping briefly, then leave)"

channel agents --within 30
# Expect: a codex/%<pane> entry
```
