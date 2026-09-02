# PilferedParrot

PilferedParrot is a local browser interface for two coding engines:

- **Local Qwen**, served through an OpenAI-compatible endpoint and equipped with contained file,
  shell, and diff tools.
- **OpenAI Codex**, run through the authenticated Codex CLI with its normal model, sandbox, and
  approval behavior.

The operator chooses the provider. PilferedParrot does not ask one model to route, delegate, review,
or adjudicate another model's work. Claude Code is intentionally outside this application and is
used through its own app.

The browser also opens **Chat** in its own floating window, powered by a separate read-only Codex
Terra or Luna session. It is an ordinary conversation kept separate from work sessions; it is not
an interpreter or hidden router.

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
committed example leaves automatic startup disabled. All defaults are in
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
Closing the main PilferedParrot window stops its local server and cancels any active provider run;
closing only the optional Chat window does not.

The browser shows:

- three independent spaces for usage/history, technical work, and optional Terra chat;
- the selected provider and model;
- the project folder and conversation history;
- live provider commentary, commands, and tool completion events in a bounded work-details panel;
- a one-click terminal button on single-line command blocks in completed assistant responses;
- whether local Qwen is ready;
- Codex's provider-reported weekly included-usage amount and reset time.

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
```

Run `./bin/install-pilferedparrot-desktop` to replace the old Mint/Cinnamon start-menu entry and
its letter-C icon with the PilferedParrot launcher and parrot artwork.

## Codex usage display

The sidebar reports the percentage **left in Codex's weekly included-usage window**. It does not
describe that number as a percentage of the subscription. For example, “Weekly included usage ·
98% left” means Codex reported 2% used in that weekly allowance at the time shown. Other
provider-reported windows are retained for diagnostics but are not rendered as sidebar bars.

PilferedParrot reads this through the Codex app server's account status method. The probe does not send
a model prompt or create a Codex conversation. It runs at startup, when manually refreshed, and
once per minute while the browser is open. OpenAI notes that local messages and cloud chats share
a rolling five-hour window and that additional weekly limits may apply; exact consumption depends
on the model and task size. See the [official Codex pricing and usage documentation](https://learn.chatgpt.com/docs/pricing).

## Context and token behavior

PilferedParrot deliberately keeps the provider path thin:

- A Codex turn receives the user's prompt directly. PilferedParrot does not prepend a policy document,
  route request, reviewer brief, or hidden board content. Chat is informational and does not interpret,
  rewrite, or relay technical requests.
- Continuing with the same provider and model resumes that provider session. A new conversation,
  provider change, or model change starts fresh context.
- Chat has its own persisted, read-only Terra or Luna session. It receives the Chat message directly
  and does not receive hidden work-session snapshots. Resetting Chat does not reset a work session.
- Chat and each work session show a visible-transcript context pie against the usable model limit.
  Provider completed-turn token totals are aggregate compute usage, not current context occupancy,
  so the pies do not mislabel them as context. The estimate uses roughly four characters per token
  and is always labeled **Estimate**. Codex uses its local model catalog's `max_context_window` when
  available; the browser lets users choose 25%, 50%, 75%, or 100% of that maximum. The selected
  usable-token limit is passed to Codex as `model_context_window`. Set `qwen.context_window_tokens`
  or `codex.context_window_tokens` to preserve a manual maximum override when a provider does not
  publish the correct limit locally. `qwen.context_window_percent` and `codex.context_window_percent`
  default to `100`.
- There is no automatic last-look, model-to-model ping-pong, background delegation, or generated
  maintenance work.
- Browser progress is local display data. It is bounded and is not copied into later prompts.
- Run history records a prompt hash rather than the prompt text.

Qwen maintains its own conversation history locally. Its tool results are capped, shell duration
and loop length are bounded, and the browser stores only a bounded progress view.

## Qwen tools and isolation

When selected, Qwen runs an agent loop with file, shell, and Git diff tools. File access is limited
to the selected project. Shell commands run under Bubblewrap with the project and an ephemeral
`/tmp` writable, home data outside the project hidden, sensitive environment variables removed,
networking disabled by default, and other mounted paths read-only.

Selecting the entire home directory would defeat that isolation, so it is rejected by default.
The machine-local `qwen.allow_home_workspace` setting can opt in when that broad authority is
genuinely intended.

Set `qwen.shell_network` to `true` only when a task needs network access. Bubblewrap is a useful
accident boundary, not a substitute for a disposable VM when working with hostile projects,
compilers, or kernel inputs.

Codex retains its own sandbox, authentication, approvals, and network behavior. PilferedParrot reasserts
the configured working directory, sandbox mode, and additional write roots on new and resumed
turns; it does not disable those controls.

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
