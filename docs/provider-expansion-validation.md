# Provider expansion validation — 2026-09-05

This local source update adds Antigravity Work sessions, Mistral/Devstral and LM Studio
connection templates, and compatible API conversation handling for DeepSeek/GLM reasoning
and Mistral structured content. Earlier interface improvements are retained.

## Automated checks

Python 3.14.7 unittest discovery with mandatory Playwright: **440 passed, zero skipped,
zero failures**. Coverage includes real browser provider setup, native onboarding,
Antigravity Work-only enforcement, exact conversation routing, malformed CLI/API responses,
credential redaction, contained file edits, API tool continuation, and the live-check helper.
JavaScript syntax, Python compilation, example JSON parsing, and `git diff --check` passed.
Automated provider responses are synthetic and incur no remote inference usage.

## Live results

| Integration | Result | Remaining limitation |
| --- | --- | --- |
| Antigravity CLI 1.1.27 / `gemini-3.8-flash-low` | Reply, native file read/write, exact conversation continuation after deleting marker files, and cancellation passed. Work UI reply, continuation across reload, persisted history, disabled Chat, and zero JavaScript errors passed. | Work only. Native headless permission rules can deny shell commands. Cumulative tokens are not reported as remaining allowance. |
| OpenRouter / DeepSeek and GLM | Offline protocol, tool, and continuation tests passed. | API credential unavailable; no live model certification. |
| Mistral / Devstral | Offline structured-message, function-call ID, tool, and continuation tests passed. | API credential unavailable; no live model certification. |
| LM Studio / Qwen2.5 7B Instruct Q4_K_M | Random hidden marker read and copied exactly with native file tools; files deleted before a successful no-tools continuation. Cancellation passed, with no further progress during the following second. | Verified on CPU with 8,192-token context; this is a harness compatibility check, not a general coding-quality benchmark. |

The Antigravity CLI was updated using its own `agy update` command. LM Studio's official
llmster runtime was installed user-locally without an automatic startup service or shell
profile changes. Live checks used disposable synthetic workspaces. No paid accounts or
credits were purchased, and no real project files were included in prompts.

An initial Qwen3 1.7B trial invoked the tools but failed exact file-copy validation.
That model was removed; the installed 7B model passed the stronger check. LM Studio's
server and daemon were stopped after validation.

To start the installed local setup:

```bash
lms daemon up
lms server start --bind 127.0.0.1 --port 1234
lms load qwen2.5-7b-instruct --gpu off --context-length 8192 --identifier qwen2.5-7b-instruct --yes
```

Then choose **Providers → Add provider → LM Studio** and enter `qwen2.5-7b-instruct`
as the model ID. The `lms` command is available through a user-local symlink; no shell
profile change is needed. To stop it later, use `lms server stop` and `lms daemon down`.

On this date, OpenRouter's [public model catalog](https://openrouter.ai/api/v1/models)
listed `z-ai/glm-5.2:free` with tool support and zero prompt/completion pricing. No
DeepSeek model with tool support had zero prompt and completion pricing in that response.
These are catalog observations, not verified account access; recheck pricing and limits
before a live run. PPI does not switch to a paid model automatically.

## Reproduce API validation

`bin/check-provider` is an explicit, bounded live check. It owns a temporary workspace,
requires actual read/write tool progress and exact file contents, removes both files
before checking a second user turn, and checks cancellation. It emits a JSON summary
and exits nonzero if a requirement fails. It supports compatible APIs, not native CLIs.
It uses built-in defaults unless `--config` explicitly names a configuration file, and
clears inherited additional directories and automatic-start commands.

For example, with the API key already available in PPI's environment:

```bash
bin/check-provider --provider openrouter --model z-ai/glm-5.2:free \
  --api-key-env OPENROUTER_API_KEY --run
```

The command uses the selected provider's normal usage limits and pricing. Its bounded
output and request limits can reject models that need more time or reasoning tokens;
a failed smoke check does not establish that the whole service is incompatible.
Canceling a non-streaming API request stops subsequent PPI tools; generation already
submitted to the endpoint may continue until its response or timeout.

## Delivery checkpoint

Reopen PPI after any active response finishes to load the changed runtime and assets.
The new Antigravity card is built in; add Mistral and LM Studio through **Providers →
Add provider**. OpenRouter continues to use its existing card template.

The live validation used isolated local evidence, including test logs, Antigravity
adapter/UI summaries, and LM Studio validation results. These results describe the
pre-publication validation checkpoint; account-specific evidence is not distributed.
