# Gemini CLI adapter

Gemini CLI integrates via the `gemini-with-channel.sh` wrapper.

## 1. Install the worker rule

Append [`examples/worker-rule.md`](../worker-rule.md) to your project's
`GEMINI.md`. Gemini reads this file at session start.

## 2. Spawn Gemini via the wrapper

```bash
~/.session-channel/wrappers/gemini-with-channel.sh

# In a tmux pane (relay-pool style)
tmux send-keys -t '%7' "~/.session-channel/wrappers/gemini-with-channel.sh" Enter
```

What the wrapper does:

- Publishes `announce` on launch and `leave` on exit (via `trap`)
- Pre-allocates `--session-id <uuid>` so the channel meta carries a stable
  session identifier (override with `CHANNEL_SESSION_ID`)
- Idle background loop publishes heartbeats every 60s (Gemini's `hooks`
  subcommand currently only supports `migrate` — no per-turn notify, so
  heartbeats are loop-only)
- YOLO mode (`-y`) is on by default

## 3. Add to supervisor

```yaml
relay_pool:
  workers:
    - pane_id: "%7"
      cli_type: gemini
      enabled: true
```

## 4. Known quirk — `/rename` reflex

Gemini has been observed to interpret incoming worker prompts as an
internal `/rename` target (cosmetic side-effect; shell prints
"No such file or directory" and the dispatch loop continues). The worker
rule explicitly forbids this, but Gemini sometimes runs both. It does not
break the loop — the explicit `channel send ... --tag done` shell command
still executes.

## 5. Smoke test

```bash
CHANNEL_DRY_RUN=1 ~/.session-channel/wrappers/gemini-with-channel.sh
# Expect: "gemini-with-channel: dry-run (announce sent, brief sleep, then leave)"

channel agents --within 30
# Expect: a gemini/%<pane> entry
```
