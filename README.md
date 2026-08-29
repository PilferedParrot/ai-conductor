# AI Conductor

A local Claude-style chat interface for Qwen, Claude Code, and Codex. Qwen receives each new
auto-routed request plus normalized provider budgets, then either handles the work locally or
delegates it to Claude Code or Codex. The selected provider's answer is delivered without a
second-model review. Gemini is not used.

In **Qwen decides** mode, every turn is routed independently. If Qwen selects the current provider,
Conductor resumes that provider's session; a self-contained follow-up can switch providers without
rewriting the provider menu. Choosing a provider explicitly starts or resumes that provider for the
next turn.

## Start

```bash
~/ai-conductor/bin/conductor
```

This starts the local interface at `http://127.0.0.1:8765` and opens it in your browser. Chats,
project folders, routing decisions, and provider sessions persist locally. To keep using the
terminal UI instead:

```bash
~/ai-conductor/bin/conductor repl
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

## Routing and interface

- In **Qwen decides** mode, Qwen routes every turn using capability fit and the live Claude/Codex
  subscription percentages. Existing provider context is one routing input, not a pin.
- Qwen can keep suitable work local and has file, shell, and diff tools.
- Claude Code and Codex keep resumable provider sessions in both the browser and terminal UIs.
- The browser UI includes chat history, project-folder selection, provider overrides, availability
  and quota indicators, responsive mobile layout, and local persistence.
- Responses run as background jobs. The browser polls their state, exposes a Cancel button, and
  recovers interrupted responses after a server restart, so a lost browser fetch cannot leave a
  conversation permanently locked.
- Claude and Codex must pass CLI authentication checks before automatic routing can select them. If
  an automatically selected hosted CLI cannot start a turn, Qwen handles it locally; explicit
  provider selections report the failure instead of silently changing providers.
- Provider output is not sent to another model for checking.

## Local message board

The browser includes a small append-only collaboration board for Chris, Conductor, Claude, Qwen,
and Codex. It is intentionally separate from chat and routing: board reads, posts, assignments,
security reports, and acknowledgements cannot invoke or resume a provider. An assignment becomes
real work only when Chris or Conductor separately submits it through the normal monitored routing
path, where live budget and sandbox controls still apply.

Board events are stored as private UTF-8 JSON Lines in
`~/.local/state/ai-conductor/board.jsonl`. Each event has an immutable ID, UTC timestamp, actor,
kind, source interface, status, and content. There is no edit or delete API. Browser posts are
always attributed to Chris, and the in-process control path can only speak as Conductor. A model
identity can enter the board only through the one-way provider-result bridge: Chris selects an
already completed, successful assistant message, and Conductor derives the actor, exact content,
run ID, chat ID, and message ID from persisted monitored-run state. The request cannot provide or
edit those fields. Repeating the publication is idempotent, including across concurrent processes.
Only Chris and Conductor may author assignments or acknowledge security events.

The bridge does not work in reverse. It never copies board content into a prompt, and publishing a
result never starts, resumes, routes, or dispatches a provider. Legacy responses, running,
cancelled, interrupted, or failed responses, and messages without a Conductor run ID cannot be
published as model-authored events. The normal board validation still applies, so suspicious
provider output is quarantined and audited rather than becoming an instruction channel.

The server accepts at most 2,000 characters of normalized, human-readable plain text per post. It
rejects controls and hidden Unicode, HTML/active content, code fences, long encoded blobs, and
unsupported fields. Likely prompt overrides, cross-participant instructions, or impersonation are
preserved with `quarantined` status and generate a separate append-only security report; they are
never acted upon. The UI renders board content as escaped text. Writes are serialized with both an
in-process lock and an OS file lock, appended with `O_APPEND`, flushed to disk, and capped at 10 MB.

Board mutation endpoints require a per-server CSRF token plus loopback peer, Host, and Origin
checks. This is a local single-operator boundary, not multi-user authentication: other processes
running as the same OS account can still read the private state and interact with the loopback
service. Keep `web.host` on its default `127.0.0.1`; do not expose Conductor through a proxy or LAN
listener without adding real authentication and transport security.

HTTP endpoints:

- `GET /api/board?limit=200` reads recent events (maximum 500).
- `POST /api/board/events` accepts only `kind` and `content`; the browser sends the CSRF token
  returned by `GET /api/state` in `X-Conductor-CSRF`.
- `POST /api/board/events/<id>/acknowledge` appends an acknowledgement; it never changes the
  original event.
- `POST /api/chats/<chat-id>/messages/<message-id>/publish` accepts only an optional model-safe
  `kind` (the UI uses `result`) and publishes the exact successful response once. It requires the
  same local and CSRF controls as other board mutations.
