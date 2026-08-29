# AI Conductor

A small local front door for Qwen, Claude Code, and Codex. Qwen receives each new top-level
request plus normalized provider budgets, returns one strict routing decision, and the selected
provider's answer is delivered without a second-model review.

Follow-up messages remain pinned to the selected provider so conversation context is not split
between incompatible session stores. `/new` returns to automatic routing.

## Start

```bash
~/ai-conductor/bin/conductor
```

Useful non-interactive commands:

```bash
~/ai-conductor/bin/conductor budget
~/ai-conductor/bin/conductor route "inspect this repository for the cause of the failing tests"
~/ai-conductor/bin/conductor run --provider codex "implement the agreed fix"
```

The first automatic route starts the stock Qwen code profile when needed. The existing local
idle watchdog remains responsible for releasing its VRAM.

## Claude budget telemetry

Claude Code supplies the exact five-hour used percentage to its statusline process. The included
wrapper caches that value and then calls the existing statusline unchanged. Activate it by setting:

```json
"statusLine": {
  "type": "command",
  "command": "/opt/ai-conductor/bin/claude-statusline"
}
```

Until Claude renders a statusline after activation, the conductor reports its subscription budget
as unknown. Unknown budgets are eligible by default; edit `config.json` to change reserves or that
policy. If `claude auth status` reports that the CLI is signed out, Claude is excluded from automatic
routing until you complete one interactive `claude` login.

Codex quota telemetry comes directly from `codex app-server`'s `account/rateLimits/read` method.
No API key is used; it uses the Codex CLI's existing ChatGPT authentication.

## Qwen coding tools

When Qwen is selected it now runs an agent loop with native tool calls. It can read, create, and
edit files under `--cwd`, execute Bash commands, and inspect the current Git diff. Shell commands
run in a Bubblewrap sandbox: the selected workspace and `/tmp` are writable while the rest of the
host filesystem is read-only. Tool output, file size, command duration, and loop length are capped
in the Qwen defaults in `ai_conductor/config.py`.

`bubblewrap` (`bwrap`) must be installed for Qwen shell calls. File and diff tools continue to work
without it; a shell attempt will return a tool error to the model.

## Current scope

- Qwen routes once and can work as a local coding agent with file, shell, and diff tools.
- Claude and Codex keep resumable provider sessions inside the REPL.
- Provider output is not sent to another model for checking.
- A full-screen diff approval UI is still outside the current terminal interface.
