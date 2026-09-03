# PilferedParrot

PilferedParrot is a local browser interface for four coding engines:

- **Local Qwen**, served through an OpenAI-compatible endpoint and equipped with contained file,
  shell, and diff tools.
- **OpenAI Codex**, run through the authenticated Codex CLI with its normal model, sandbox, and
  approval behavior.
- **Claude Code**, run through the authenticated Claude CLI in non-interactive print mode while
  preserving its provider session between turns.
- **Google Gemini**, run through the authenticated Gemini CLI in headless streaming mode with
  resumable, project-scoped sessions.

The operator chooses the provider. PilferedParrot does not ask one model to route, delegate, review,
or adjudicate another model's work.

The browser also opens **Chat** in its own floating window, powered by a separate read-only session
from the provider active in the work window that opened it. Chat offers that provider's model menu.
It is an ordinary conversation kept separate from work sessions; it is not an interpreter or hidden
router.

The project targets Linux and Python 3.12 or later. Qwen shell tools require
[Bubblewrap](https://github.com/containers/bubblewrap). The Python application has no third-party
package dependencies.

## Install and configure

```bash
git clone https://github.com/PilferedParrot/ai-conductor.git PilferedParrot
cd PilferedParrot
cp config.example.json config.json
```

`config.json` is ignored so machine-specific paths stay local. Start your Qwen server yourself,
or set `qwen.auto_start` to `true` and provide `qwen.start_command` as an argument array. The
committed example leaves automatic startup disabled. Existing config files are tightened to mode
`0600` when loaded on POSIX systems. All defaults are in
`pilferedparrot/config.py`.

Start the browser interface:

```bash
./bin/pilferedparrot
```

It listens on `http://127.0.0.1:8765` and opens a dedicated app-style browser window when the
launcher can find Chrome or Chromium. Chats and run metadata persist under
`~/.local/state/pilferedparrot/` with owner-only permissions. On first start after upgrading, the
app imports existing chats from the former state directory. The desktop installer also moves the
known legacy state files and browser profile into the new directory without overwriting newer data.
Closing the last PilferedParrot work window stops its local server and cancels any active provider
run; closing only the optional Chat window does not.

Use **Providers** in the header to open the provider dashboard. It shows connection state, lets you
choose a model, and can start another maximized app window with that fixed provider and model.
Every LLM integration is one card. Use **Add provider** to add xAI/Grok, OpenRouter, Ollama, or any
service with an OpenAI-compatible chat-completions and tool-calling API. Presets fill the standard
base URL and credential environment-variable name; leave Model ID blank to discover available
models automatically. PilferedParrot stores settings beside the chat store (or at
`web.model_catalog_store` when configured), but never stores the API key itself. Each card has a
**Remove provider** action. Removal hides the card without deleting its settings, and **Add
provider** offers it for one-click restoration.
Keyed remote endpoints must use HTTPS. Provider HTTP redirects are restricted to the originally
configured origin so a redirect cannot carry an API key to another host or downgrade it to plaintext.
Each provider window shows that provider's persistent session history, including after the window is
closed and reopened; context usage remains local to each session. Codex and Claude maintain
separate CLI credentials, so they can remain signed in and run in different windows at the same
time; Local Qwen needs no account. Signing out affects every PilferedParrot window using that
provider, but does not sign out the other providers. Selecting **Sign in** starts the provider's
official CLI flow in the background and opens its login page in the system's default browser; no
terminal link or command is part of the user flow. The dashboard detects completion and presents a
clear **Use Provider** action. Authentication and account choices remain owned by the provider CLI;
PilferedParrot never receives the account password.

Use **Appearance → Choose this window's theme** in the sidebar to open Chrome's theme gallery with
PilferedParrot's dedicated browser profile. PilferedParrot runs as a private Chrome app window, and
this control does not change the Chrome theme used for normal browsing. The selected theme persists for the main app window and
PilferedParrot applies its colors and available new-tab background artwork when the user returns to
the app. The isolated Chat window uses the same selected theme without sharing browser state.

The browser shows:

- three independent spaces for usage/history, technical work, and optional provider-matched Chat;
- the selected provider and model;
- the project folder and conversation history;
- live provider commentary, commands, and tool completion events in a bounded work-details panel;
- a native desktop notification when a work-session or Chat response finishes (after browser
  notification permission is granted);
- a one-click terminal button on single-line command blocks in completed assistant responses;
- whether local Qwen is ready;
- Codex, Claude, and Gemini CLI sign-in/availability status;
- Codex and Claude's provider-reported included-usage amounts and reset times.

The project folder shown in the header is the primary writable workspace. Change it before the
first message when starting work in another checkout. PilferedParrot rejects a clear first-turn request
to modify a different, unapproved project before spending a provider turn.

Codex can also work across a small, explicit set of related checkouts without disabling its
sandbox. Add those directories to the machine-local `config.json`:

```json
{
  "codex": {
    "sandbox": "workspace-write",
    "additional_write_dirs": ["~/related-project"]
  }
}
```

PilferedParrot passes each path with Codex's supported
[`--add-dir` option](https://learn.chatgpt.com/docs/developer-commands?surface=cli) on both new and resumed turns.
Paths must already exist and be writable by the operator. Prefer individual project roots over the
whole home directory.

Other commands remain available for diagnostics and scripts:

```bash
./bin/pilferedparrot repl
./bin/pilferedparrot budget
./bin/pilferedparrot run --provider qwen "inspect this repository"
./bin/pilferedparrot run --provider codex "implement the agreed fix"
./bin/pilferedparrot run --provider claude "review the implementation"
./bin/pilferedparrot run --provider gemini "summarize the design"
```

Run `./bin/install-pilferedparrot-desktop` to replace the old Mint/Cinnamon start-menu entry and
its letter-C icon with the PilferedParrot launcher and parrot artwork.

## Provider usage display

The sidebar reports the percentage **left in Codex's weekly included-usage window**. It does not
describe that number as a percentage of the subscription. For example, “Weekly included usage ·
98% left” means Codex reported 2% used in that weekly allowance at the time shown. Other
provider-reported windows are retained for diagnostics but are not rendered as sidebar bars.

PilferedParrot reads this through the Codex app server's account status method. The probe does not send
a model prompt or create a Codex conversation. It runs at startup, when manually refreshed, and
once per minute while the browser is open. OpenAI notes that local messages and cloud chats share
a rolling five-hour window and that additional weekly limits may apply; exact consumption depends
on the model and task size. See the [official Codex pricing and usage documentation](https://learn.chatgpt.com/docs/pricing).

For a signed-in Claude Code account, the sidebar shows compact current-session and weekly
allowance bars with a live reset countdown; hovering the reset shows the exact local date and time.
Model-specific weekly windows remain available to diagnostics without taking permanent sidebar
space. This check uses the CLI's stored OAuth credential and does not send a model prompt.

## Context and token behavior

PilferedParrot deliberately keeps the provider path thin:

All execution goes through a `ProviderAdapter` contract for run/resume/cancel, normalized progress,
token and context usage, capabilities, authentication, availability, and model discovery. Codex,
Claude, Gemini, and OpenAI-compatible endpoints retain separate implementations behind that contract.

- A Codex turn receives the user's prompt directly. PilferedParrot does not prepend a policy document,
  route request, reviewer brief, or hidden board content. Chat is informational and does not interpret,
  rewrite, or relay technical requests.
- Continuing with the same provider and model resumes that provider session. A new conversation,
  provider change, or model change starts fresh context.
- Chat has its own persisted, read-only provider session. It inherits the provider and selected model
  from the work window that opens it, then lets the user choose another model in that provider family.
  It receives the Chat message directly and does not receive hidden work-session snapshots. Resetting
  Chat does not reset a work session.
- Chat and each work session show an estimated next-request live-context pie against the usable
  model limit. A new work session starts at zero and begins accounting when its first request
  starts. After a request, the estimate uses the provider's final per-request input plus its
  latest output; this includes injected instructions, tools and relevant results, workspace context,
  attachments, and other prompt inputs. Before that telemetry exists, PilferedParrot estimates the
  transcript and known provider/workspace overhead at roughly four characters per token. Reserved
  output headroom is shown separately and is not counted as used. Completed-turn aggregate compute
  totals remain separate because they are not current context occupancy. Each provider can define
  per-model `context_window` and `max_context_window` metadata in `model_options`; Codex also uses
  its local model catalog's `max_context_window` when available. The browser lets Codex users choose
  25%, 50%, 75%, or 100% of that maximum. The selected
  usable-token limit is passed to Codex as `model_context_window`. Set `qwen.context_window_tokens`,
  `codex.context_window_tokens`, or `claude.context_window_tokens` to preserve a manual maximum
  override when a provider does not publish the correct limit locally. The matching
  `context_window_percent` settings default to `100`.
- Model menus refresh through the selected provider adapter whenever the picker opens. Friendly
  names and exact IDs are shown when useful; Claude's shorter menu shows concrete numbered model
  names only, so a saved selection does not silently move to a newer release. The
  most recently chosen model and context-window allowance become the initial selections for the
  next matching provider window or Chat thread and persist across app restarts.
- There is no automatic last-look, model-to-model ping-pong, background delegation, or generated
  maintenance work.
- Browser progress is local display data. It is bounded and is not copied into later prompts.
- Run history records a prompt hash rather than the prompt text.

OpenAI-compatible providers maintain their conversation history locally under provider-neutral
state. Their tool results are capped, shell duration
and loop length are bounded, and the browser stores only a bounded progress view.

## Qwen tools and isolation

When selected, Qwen runs an agent loop with file, shell, and Git diff tools. File access is limited
to the selected project. Shell commands run under Bubblewrap with the project and an ephemeral
`/tmp` writable, home data outside the project hidden, sensitive environment variables removed,
networking disabled by default, and other mounted paths read-only.

Selecting the entire home directory would defeat that isolation, so it is rejected by default.
The machine-local `qwen.allow_home_workspace` setting can opt in to that exact directory when the
broad authority is genuinely intended. Selecting one of home's parents is always rejected because
it grants an even broader scope.

A task that spans several projects usually does not need the home directory at all. List the extra
roots in `qwen.additional_dirs` instead: each one is mounted into the sandbox and reachable by
absolute path, while everything unlisted stays hidden. The list is configuration-only, so a prompt
cannot widen its own authority, and it cannot contain the home directory or one of its parents —
that remains a deliberate `allow_home_workspace` decision rather than something an extra root can
smuggle in.

Because a sandboxed provider cannot open in the home directory, a provider window launched from a
window that *is* sitting there would previously fail to open at all, leaving nowhere to correct the
folder. Such a launch now falls back to `qwen.default_workspace` when it is set, and otherwise opens
the window and asks for a project folder before starting the first chat.

Qwen endpoints must also be loopback URLs by default. Set `qwen.allow_remote_egress` to `true`
only when you intentionally want prompts and tool results sent to the configured remote server.

Set `qwen.shell_network` to `true` only when a task needs network access. Bubblewrap is a useful
accident boundary, not a substitute for a disposable VM when working with hostile projects,
compilers, or kernel inputs.

Codex retains its own sandbox, authentication, approvals, and network behavior. PilferedParrot reasserts
the configured working directory, sandbox mode, and additional write roots on new and resumed
turns; it does not disable those controls.

Claude Code likewise owns its authentication, tool permissions, settings, and network behavior.
PilferedParrot uses Claude's print-mode CLI and resumes only the session ID returned by Claude.
Account actions use `claude auth login`, `claude auth status`, and `claude auth logout` when
supported by the installed CLI.

The browser API uses separate dashboard and Chat capabilities, delivered in URL fragments and
kept out of `/api/state`. Browser mutations must come from the server's exact origin; a capability
for one window cannot operate the other window's controls.

## Branding

PilferedParrot is developed by PilferedParrot Global Industries, makers of 3D Bumper Billiards.
The app uses the company logo and compact parrot emblem from those branding assets.

## Development

Run the same checks used by CI:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -v
python3 -m compileall -q pilferedparrot tests
```

See [CONTRIBUTING.md](CONTRIBUTING.md) and [SECURITY.md](SECURITY.md).

## License

PilferedParrot is available under the [Apache License 2.0](LICENSE). Redistributions must preserve
the attribution notices described in the license and [NOTICE](NOTICE) file.

If PilferedParrot is useful to you, you can support its development on
[Patreon](https://www.patreon.com/PilferedParrot).
