# Provider compatibility

PPI keeps the interface independent from the selected model through its provider adapter
contract. The current implementation has these integration paths:

| Integration | Connection | Conversation continuity | Setup |
| --- | --- | --- | --- |
| OpenAI Codex | Local Codex CLI | CLI session resume | Install and authenticate the CLI |
| Anthropic Claude | Local Claude Code CLI | CLI session resume | Install and authenticate the CLI |
| Google Antigravity | Local Antigravity CLI (`agy`) | Exact conversation ID resume | Update and authenticate the CLI; Work only |
| Google Gemini | Local Gemini CLI | CLI session resume | Install and authenticate the CLI |
| Local LLM | OpenAI-compatible HTTP endpoint | Locally stored message history | Start a compatible model server |
| Additional providers | OpenAI-compatible HTTP endpoint | Locally stored message history | Add a provider card and configure its credential environment variable |

The Add provider form includes xAI/Grok, OpenRouter, Google AI Studio/Gemini API, Mistral/Devstral,
LM Studio, Ollama,
and a custom connection template.
A template supplies connection defaults; it does not certify every model offered by that service.
Use an exact model ID available to your account. If model discovery is unavailable, enter the ID
manually. A successful model-list request does not establish that the model can execute a work turn.

## Account access and subscriptions

An editor subscription or consumer chat login does not supply an API credential for a separate
service. PPI uses native CLI authentication only through its supported CLI integrations; API
cards require the credentials issued for the endpoint you choose. It never extracts editor
tokens or routes requests through undocumented subscription endpoints.

Google ended consumer Google-login access to Gemini CLI on June 18, 2026; its notice says
Code Assist Standard and Enterprise access continues. PPI leaves Gemini CLI available for
supported authentication methods and checks account access when a request runs. An installed
CLI or cached credential file is not reported as a verified login. See
[Google's deprecation notice](https://developers.google.com/gemini-code-assist/docs/deprecations/code-assist-individuals).

The Google AI Studio/Gemini API template uses
`https://generativelanguage.googleapis.com/v1beta/openai` and `GEMINI_API_KEY` as documented
in [Google's OpenAI compatibility guide](https://ai.google.dev/gemini-api/docs/openai).
Choose an account-accessible model with function calling. This is a separate API connection
with its own access and usage limits.

Gemini can still be used without a paid subscription through the Gemini API's
[Free Tier](https://ai.google.dev/gemini-api/docs/billing), subject to the eligible models
and the account's rate limits. Create a key in
[Google AI Studio](https://aistudio.google.com/apikey), make it available as
`GEMINI_API_KEY` in the environment that launches PPI, and restart PPI. In **Providers →
Add provider**, choose **Google AI Studio / Gemini API** and select a model available
on the project's Free Tier. The form expects the variable name `GEMINI_API_KEY`,
not the secret value. Model discovery lists IDs; it does not verify free-tier eligibility.

For consumer Google sign-in, Google's migration path is
[Antigravity CLI](https://antigravity.google/docs/cli/gcli-migration/).
PPI implements the current CLI's
[headless streaming protocol](https://antigravity.google/docs/cli/headless/): prompts travel
through stdin, progress includes file/tool steps, and continuation selects the exact
`conversation_id`. Run `agy update` if an older binary rejects the streaming flags, then
authenticate in an interactive `agy` terminal. **Poll models** reads `agy models`.
The initial model is `gemini-3.8-flash-low`; choose another account-accessible model in the picker.
Model-list success does not establish that inference is available.

Antigravity is currently **Work only**. Its
[plan mode](https://antigravity.google/docs/cli/modes/) adds a planning instruction rather
than establishing a read-only tool boundary. PPI disables Chat and rejects Chat requests
for this adapter. Work uses `accept-edits` with the CLI's normal shell permission rules;
PPI does not pass `--dangerously-skip-permissions`. Headless tools needing approval can be
denied by the CLI; configure scoped provider-owned permissions when needed. Antigravity's
cumulative session token counters are not presented as per-turn usage or remaining allowance.

## Coding API and local setup

For **DeepSeek or GLM**, choose **OpenRouter**, supply `OPENROUTER_API_KEY`, and enter an
exact model ID from OpenRouter's [tool-capable catalog](https://openrouter.ai/models?supported_parameters=tools).
PPI preserves the provider's opaque reasoning continuation fields between tool calls and
later user turns, as required by OpenRouter's
[reasoning continuation protocol](https://openrouter.ai/docs/guides/best-practices/reasoning-tokens).
Free access depends on the exact model variant and account limits; ordinary IDs are not
implicitly free. Check the [free-variant documentation](https://openrouter.ai/docs/guides/routing/model-variants/free).

For **Mistral Devstral**, choose **Mistral / Devstral**, supply `MISTRAL_API_KEY`, and select
an account-accessible Devstral model. The template uses `https://api.mistral.ai/v1` and
Mistral's [Chat Completions API](https://docs.mistral.ai/api). PPI retains tool-call IDs and
structured assistant content for continuation, while displaying text content only.

For **LM Studio**, load a model with native tool support and start its local server from the
Developer tab, or run `lms server start`. Choose **LM Studio** in PPI; its default endpoint is
`http://127.0.0.1:1234/v1` and no API credential is configured by default. If server authentication
is enabled, enter the environment-variable name holding its token. Use the exact model ID
returned by discovery. See [LM Studio tool support](https://lmstudio.ai/docs/developer/openai-compat/tools)
and [headless installation](https://lmstudio.ai/docs/developer/core/headless).
PPI does not start LM Studio automatically; the existing Qwen automatic-start setting remains separate.

These paths share PPI's contained file/tool loop. Protocol regression tests are separate from
live model certification; see [provider expansion validation](provider-expansion-validation.md)
for the tested combinations and remaining account prerequisites.

Cursor is not a native PPI adapter, and a Cursor account is not an xAI API key. Cursor's
[pricing page](https://cursor.com/pricing) lists limited Agent use and Composer for Hobby;
its [Grok access documentation](https://cursor.com/help/models-and-usage/grok-4-5) lists paid
plans. PPI's xAI/Grok card uses an xAI-issued API key, or you can use another configured
compatible provider that offers an accessible Grok model. Account access must be tested
separately; templates do not promise a particular subscription entitlement.

## Compatible endpoint requirements

The current adapter sends non-streaming JSON requests to `POST /chat/completions` beneath the
configured base URL. It supplies `model`, `messages`, `tools`, `tool_choice`, `max_tokens`, and
`stream`. The endpoint must accept these fields and return a Chat Completions response with
`choices[0].message`. Work models must support function tool calls. Chat uses the same protocol
with only read-only tools. A service that only supports a different native API needs a new adapter;
changing the base URL alone is insufficient.

Optional model discovery uses `GET /models` and reads IDs from `data`. Missing usage telemetry
does not prevent execution; the interface uses estimates where supported. Compatible responses
are displayed after each HTTP request completes, with progress between tool steps.
Cancel returns control to the interface and stops subsequent tool steps. A non-streaming
HTTP request already submitted to a compatible provider may continue there until its
response or timeout; cancellation does not guarantee that remote generation or billing stops.

API keys remain in the server process environment. Enter the environment-variable name in the
provider card, never the key value. Restart PPI after changing its inherited environment. Remote
keyed endpoints require HTTPS, and redirects cannot move credentials to another origin.
Adding a remote provider authorizes sending that session's prompts and tool results to it.

Shell execution requires Bubblewrap. Read/write tools stay within the approved workspace roots;
Chat removes write and shell tools. Native CLI providers retain their own permission systems.
See [Security policy](../SECURITY.md) for the complete local trust boundary.

## Compatibility validation

The automated suite uses synthetic provider responses and isolated application state. It verifies
adapter behavior, session handling, credential redaction, and browser authorization without using
real accounts or spending model tokens. Browser tests use a fake provider and require Chromium
and loopback sockets.

Before claiming a particular provider/model combination works end to end, verify sign-in or API
credentials, model selection, a reply, continuation, cancellation, and a contained tool operation
against that integration. Provider updates can change CLI or API behavior. No release can guarantee
support for every present and future LLM merely from a shared UI or a compatible model-list endpoint.

New protocols should implement the existing adapter contract and capability reporting, keeping
provider-specific request formats and authentication outside the browser interaction code.
