# Claude Code adapter

Claude Code (Anthropic's CLI) integrates with session-channel through three
points: the worker rule (instruction layer), an optional hook handler
(observability), and a relay-pool spawn line.

## 1. Install the worker rule

```bash
mkdir -p ~/.claude/rules
cp examples/worker-rule.md ~/.claude/rules/session-channel-worker.md
```

The rule is picked up automatically on the next session start.

## 2. (Optional) Hook handler for live observability

If you run a hook dispatcher (e.g. `hook-dispatcher`), wire it to publish
PreToolUse / SessionStart / Stop / SessionEnd / UserPromptSubmit events to
the `agents` topic. A self-contained Python sample lives at
`examples/hooks/session_channel.py` — point your dispatcher at it, or
adapt the code to your hook framework of choice.

Effect: the dashboard's agent card shows `⚙ <last tool call>` for live
Claude Code panes, so you can see what each session is doing at a glance.

## 3. Spawn Claude in a tmux pane (relay-pool style)

Plain spawn — no wrapper needed for Claude (Claude Code's own hooks publish
heartbeats once the rule is installed):

```bash
tmux send-keys -t '%5' "claude --dangerously-skip-permissions" Enter
```

For supervisor-managed respawn, add the pane to `config.yaml`:

```yaml
relay_pool:
  workers:
    - pane_id: "%5"
      cli_type: claude
      enabled: true
```

## 4. Smoke test

```bash
# Dispatch one task to pane %5
channel send tasks "Say hi" --tag assign \
  --meta '{"v":1,"task_id":"smoke-1","target_pane":"%5","prompt":"Say hi"}' \
  --notify-target '%5'

# Watch for completion
channel tasks --pending     # should show smoke-1 transition to done
```
