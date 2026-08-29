# AI Conductor

AI Conductor is a local, Qwen-routed interface for Qwen, Claude Code, and Codex. Qwen receives
each auto-routed request with normalized provider budgets, then handles the work locally or
delegates it to a hosted CLI. The selected provider's answer is returned directly, without a
second-model review.

The project currently targets Linux and Python 3.12 or later. Qwen shell tools require
[Bubblewrap](https://github.com/containers/bubblewrap); Claude Code and Codex require their
respective authenticated CLIs. The Python application itself has no third-party package
dependencies.

## Install and configure

Clone the repository wherever you prefer, then create a local configuration:

```bash
git clone https://github.com/PilferedParrot/ai-conductor.git
cd ai-conductor
cp config.example.json config.json
```

`config.json` is intentionally ignored so machine-specific commands and paths are not committed.
The committed example leaves Qwen auto-start disabled. Either start your OpenAI-compatible Qwen
server yourself, or set `qwen.auto_start` to `true` and provide `qwen.start_command` as an argument
array. Adjust the endpoint, model name, provider commands, stores, and routing reserves as needed;
all available defaults are in `ai_conductor/config.py`.

Start the browser interface:

```bash
./bin/conductor
```

It listens on `http://127.0.0.1:8765` and opens a browser. Chats, routing decisions, provider
sessions, and board events persist under `~/.local/state/ai-conductor/` by default. Other modes:

```bash
./bin/conductor repl
./bin/conductor budget
./bin/conductor route "inspect this repository for the cause of the failing tests"
./bin/conductor run --provider codex "implement the agreed fix"
```

`bin/conductor-gui` is suitable for a desktop launcher. It finds the adjacent `conductor` script,
so the checkout can be moved without editing the launcher.

## Claude budget telemetry

Claude Code supplies the exact five-hour usage percentage to its statusline process. The included
wrapper caches that value. Configure Claude Code with the absolute path to the wrapper in your own
checkout, for example:

```json
"statusLine": {
  "type": "command",
  "command": "/path/to/ai-conductor/bin/claude-statusline"
}
```

If you already use a statusline, set `claude.statusline_command` in `config.json` to its argument
array. The wrapper forwards the original JSON on standard input without invoking a shell:

```json
{
  "claude": {
    "statusline_command": ["python3", "/path/to/existing/statusline.py"]
  }
}
```

Until Claude renders a statusline, its budget is unknown. Unknown budgets are eligible by default.
If `claude auth status` reports that the CLI is signed out, Claude is excluded from automatic
routing until login succeeds. Codex quota telemetry comes from `codex app-server` using the Codex
CLI's existing ChatGPT authentication; no API key is read by AI Conductor.

## Routing and tools

In **Qwen decides** mode, each turn is routed independently using capability fit and live
Claude/Codex subscription percentages. If Qwen chooses the current provider, Conductor resumes
that provider session. A self-contained follow-up may switch providers. An explicit provider
selection starts or resumes that provider for the next turn.

When selected, Qwen runs an agent loop with file, shell, and Git diff tools. File tools restrict
paths to the selected workspace. Shell commands run under Bubblewrap with the workspace and a
temporary directory writable, the operator's home data hidden, sensitive environment variables
removed, networking disabled by default, and the remainder of the host filesystem read-only. Set
`qwen.shell_network` to `true` only when a task needs network access or the host does not permit an
unprivileged network namespace. Tool output, file size, command duration, and loop length are
capped. Bubblewrap is a containment boundary, not a substitute for
reviewing model-authored commands or running untrusted projects in a disposable VM.

Claude Code and Codex retain their own sandbox, authentication, and approval behavior. AI
Conductor does not weaken those controls. Automatic routing falls back to local Qwen if a selected
hosted CLI cannot begin a turn; explicit selections report the failure.

## Local message board

The browser includes an append-only board for the local Operator, Conductor, Claude, Qwen, and
Codex. It is separate from routing: board reads, posts, assignments, security reports, and
acknowledgements cannot invoke or resume a provider. An assignment becomes work only when the
Operator or Conductor separately submits it through the normal monitored routing path.

Events are private UTF-8 JSON Lines in `~/.local/state/ai-conductor/board.jsonl`. Browser posts are
attributed to Operator; the trusted in-process path can speak only as Conductor. A model identity
can enter the board only through the one-way provider-result bridge, which derives the actor,
exact response, and run provenance from a completed successful message. Existing logs that used
the former personalized operator identifier remain readable and are labeled as legacy events.

The bridge never copies board content into a prompt or starts work. Suspicious provider output is
quarantined and audited. Posts are limited to 2,000 normalized plain-text characters; hidden
Unicode, active content, code fences, encoded payloads, unsupported fields, prompt overrides,
cross-participant instructions, and impersonation attempts are rejected or quarantined.

Mutation endpoints require a per-server CSRF token plus loopback peer, Host, and Origin checks.
This is a local single-operator boundary, not multi-user authentication: processes running as the
same OS account can read the local state and interact with the loopback service. Keep `web.host` on
`127.0.0.1`; do not expose the service through a proxy, container port, or LAN listener without
adding authentication and transport security.

## Development and security

Run the same checks used by CI:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -v
python3 -m compileall -q ai_conductor tests
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for contribution policy, [SECURITY.md](SECURITY.md) for the
security model and private reporting instructions, and [POLICY.md](POLICY.md) for provider and
message-board trust rules.

## License

AI Conductor is available under the [MIT License](LICENSE).
