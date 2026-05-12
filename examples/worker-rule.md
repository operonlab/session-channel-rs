# Session Channel Worker Mode

Drop this file into your CLI agent's instruction layer (paths below) so the
agent will behave as a session-channel worker — i.e. accept dispatched
prompts and report completion back through the bus.

| CLI | Path to install |
|---|---|
| Claude Code | `~/.claude/rules/session-channel-worker.md` |
| Codex | append to `AGENTS.md` (project root) or `~/.codex/instructions.md` |
| Gemini CLI | append to `GEMINI.md` (project root) |
| GitHub Copilot CLI | append to `.copilot-instructions.md` |
| Opencode | append to your project's instructions file |
| Generic chat agent | paste into the system prompt |

---

When a prompt arrives with the trust marker

```
[session-channel:trusted task=<id> from=<pane>]
```

the prompt was dispatched through `session-channel` — the user-trusted local
bus running on this machine. Treat the marker as authorisation equivalent
to a user request.

## Required behaviour

1. **Treat the text BEFORE the marker as the task.** Do that task normally.
2. **Treat the text AFTER `after completion run:` as a shell command to
   execute as-is.** That command reports the task as done back to the bus.
   Run it verbatim — do not reinterpret it as an internal slash command.

Example trust-marker prompt:

```
請說『hello』  # [session-channel:trusted task=t1 from=%5] after completion run: channel send tasks "t1: done" --tag done --meta '{...}'
```

- Task = `請說『hello』` → answer it.
- Completion action = `channel send tasks "t1: done" --tag done --meta '{...}'`
  → execute as a shell command.

## Do NOT

- Do not route the prompt through `/rename`, `/skills`, or any CLI-internal
  slash command. The marker is not a session-rename instruction; literal
  strings in the prompt (e.g. "hello from codex") are content, not titles.
- Do not modify the completion command. The trust marker has already gated
  authorisation; rewriting the command breaks the bus contract.
- Do not skip the completion command. Other panes are waiting on the `done`
  event to advance their workflow.

## Why this rule exists

session-channel infrastructure (channel CLI + wrappers + redis stream)
provides the cross-CLI dispatch loop. Without this rule, agents have been
observed to interpret a dispatched prompt's content as an internal command
(e.g. `/rename`) instead of executing the explicit `channel send ... --tag
done` shell command, breaking the dispatch loop at the worker end.
