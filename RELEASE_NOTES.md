# PilferedParrot 0.5.0 release notes

PilferedParrot 0.5.0 is a public Linux preview of the local browser interface for coding CLIs and
compatible local or remote models. It is best validated on Linux Mint with X11. Google Antigravity
supports Work only, and other provider accounts or compatible endpoints require their own setup and
credentials. Bubblewrap is required for provider API or local shell tools, but not merely to open
the interface. See [provider compatibility](docs/provider-compatibility.md) and the
[provider validation notes](docs/provider-expansion-validation.md) for known limitations and
account-dependent coverage.

- Added Google Antigravity CLI for Work: streamed response/tool progress, exact conversation
  continuation, model discovery, and cancellation. Read-only Chat is disabled for this adapter.
- Added Mistral/Devstral and LM Studio connection templates. OpenRouter DeepSeek/GLM and other
  compatible models retain reasoning metadata and structured content through tool continuation.
- Added an opt-in `bin/check-provider` command for contained live API tool/continuation checks.
  See [provider validation](docs/provider-expansion-validation.md) for live versus offline coverage.
- Work and Chat preserve underscores inside identifiers such as `PARROT_UI_7281` while
  retaining Markdown emphasis.
- Claude's normal signed-out CLI result is recognized even when it exits with status 1.
- Gemini CLI status no longer treats cached credentials as verified access. Unsupported
  consumer-account requests explain the supported authentication alternatives.
- Add provider includes Google AI Studio/Gemini API using Google's documented compatible
  endpoint and an environment-variable credential.
- Provider setup now distinguishes missing CLI installations from sign-in, reports status-refresh
  progress, and keeps model-discovery failures visible in the provider dialog.
- Work and Chat expose supported reasoning choices beside the model picker. Work sessions inherit
  the most recently used model and reasoning setting; stale inherited reasoning capabilities fall
  back to Default instead of blocking a new session.
- Response details show the requested model and destination, plus compatible API model IDs when
  reported. These details describe routing evidence without claiming to prove the loaded weights.
- Known configured API keys are redacted from compatible-provider error paths. Model-discovery
  HTTP failures expose the status without reflecting upstream response bodies.
- Sidebar labels, provider headings, folder selection, and narrow-window layouts have been refined.
- Chat keeps its model, reasoning, and send controls within narrow windows. Resizing an open
  mobile sidebar back to desktop restores conversation interaction in both Work and Chat.
- Canceling a required project-folder prompt keeps that choice required before starting or
  submitting a work session.
- Browser checks can be installed and rerun through `bin/check-browser-ui`.
- Provider CLI update notices run independently of session loading and never install software
  automatically.

- Provider-neutral execution now keeps authentication, execution readiness, per-turn telemetry,
  live allowance reporting, and future organization reporting as separate capabilities.
- Claude authentication remains CLI-owned. PilferedParrot no longer reads or rewrites Claude
  credential files, consumes OAuth tokens from the environment, refreshes them, or calls private
  token and allowance endpoints.
- A provider without a supported live plan-allowance interface reports an explicit unsupported or
  unavailable state without blocking execution or inventing quota windows. Claude per-turn token
  and context telemetry remains available.
- The work and Chat surfaces share one safe Markdown renderer. It escapes HTML, limits links to
  absolute HTTP(S) and mailto destinations, supports a documented bounded subset, and exposes
  terminal actions only for validated top-level single-line shell fences.
- These changes reduce credential exposure and prevent quoted, malformed, multiline, or oversized
  content from being presented as a runnable terminal action; the server independently revalidates
  stored commands.
- Existing configuration, chat history, provider sessions, and historical ledger entries remain
  readable. Older Claude allowance snapshots remain historical records and are never reused as a
  current live quota. No organization analytics integration is included.
