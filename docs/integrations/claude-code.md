# Claude Code integration

Claude Code (Anthropic's CLI) connects to session-channel through three
moving parts:

1. **Worker rule** — instructions the model reads on session start, so it
   knows how to handle dispatched prompts.
2. **Hooks** — process exec'd by Claude Code's hook framework on session
   lifecycle events; publishes to session-channel and (on
   `UserPromptSubmit`) injects an inbox digest.
3. **Pane management** — supervisor entry in `config.yaml` so crashed
   panes get respawned.

## 1. Worker rule

```bash
mkdir -p ~/.claude/rules
cp examples/worker-rule.md ~/.claude/rules/session-channel-worker.md
```

The rule is picked up on the next session start. Verify with `/rules`
inside Claude Code.

## 2. Hooks (recommended)

The sample hook in `examples/hooks/session_channel.py` is stdlib-only and
covers the five lifecycle events Claude Code exposes. Wire it via
`~/.claude/settings.json`:

```jsonc
{
  "hooks": {
    "SessionStart":     [{"command": "$HOME/.session-channel/examples/hooks/session_channel.py SessionStart"}],
    "PreToolUse":       [{"command": "$HOME/.session-channel/examples/hooks/session_channel.py PreToolUse"}],
    "Stop":             [{"command": "$HOME/.session-channel/examples/hooks/session_channel.py Stop"}],
    "SessionEnd":       [{"command": "$HOME/.session-channel/examples/hooks/session_channel.py SessionEnd"}],
    "UserPromptSubmit": [{"command": "$HOME/.session-channel/examples/hooks/session_channel.py UserPromptSubmit"}]
  }
}
```

Notes:

- Stdout from `UserPromptSubmit` is injected into the model's context.
  All other events emit no output.
- The hook fails open — if `session-channel` is not running, the user's
  prompt still goes through; nothing blocks Claude Code's normal flow.
- For high-volume sessions, the `PreToolUse` heartbeat is throttled to
  30s; the tool-event itself is published every call so the dashboard
  shows live "what tool ran" updates.

If you run your own hook dispatcher (e.g. the Go `hook-dispatcher` from
the workshop monorepo), use it instead — the Python sample is reference
for those without one.

## 3. Spawn Claude in a tmux pane

Plain spawn — no wrapper needed (Claude Code's hooks publish heartbeats
once the rule + hook config are installed):

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

## 4. E2E smoke test

```bash
# Dispatch one task to pane %5
channel send tasks "Say 'hi' and nothing else." --tag assign \
  --meta '{"v":1,"task_id":"smoke-1","target_pane":"%5","prompt":"Say hi and nothing else."}' \
  --notify-target '%5'

# Watch
channel tasks --pending     # smoke-1 should transition to done within ~30s
channel read agents --count 5   # should show tool events from pane %5
```

Race-style fan-out (1-to-N — requires Codex / Gemini panes too; see
their integration docs):

```bash
channel race "What is the meaning of 42?" \
  --task-id meaning42 \
  --workers claude:%5,codex:%6,gemini:%7 \
  --wait 120
```

Debate-style multi-round:

```bash
channel debate "Should this codebase migrate to async I/O?" \
  --debate-id async-debate \
  --participants A:claude:%5,B:codex:%6 \
  --rounds 3 \
  --synthesizer gemini:%7
```

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Hook commands not running | settings.json path uses `~` literal | Expand to `$HOME` (settings.json doesn't tilde-expand) |
| Pane never appears in `channel agents` | hook script not executable | `chmod +x examples/hooks/session_channel.py` |
| Pane appears but disappears quickly | `--within` window too short | `channel agents --within 600` (10 min) |
| Inbox digest not injected | `UserPromptSubmit` hook missing or non-zero exit | Run hook manually: `echo '{}' \| ./session_channel.py UserPromptSubmit; echo $?` |
| Tasks stuck in pending | worker rule not picked up by the model | Add `cat ~/.claude/rules/session-channel-worker.md` to a new session and re-test |
| Multiple Claude panes interfere | task assigns are broadcast unless `target_pane` set | Always include `--notify-target` + `target_pane` in meta |
