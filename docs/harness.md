# The PilferedParrot harness

PilferedParrot owns the reusable work-package workflow. Open **Harness** in the Work toolbar to
plan a package, inspect its requested route, launch it, and record acceptance against its artifact.
Planning and reviewing use no model. A launch executes exactly one existing provider run. There is
no automatic queue, standing reviewer, hook installation, or recursive agent scheduler.

## Setup

A fresh checkout needs Python and an authenticated, supported Codex or Claude CLI, as described in
the main README. It needs no private registry, home-directory skill, or copied session history.
Start PilferedParrot, choose the provider and project folder, then open Harness. Opening it can
create the initial work session. Choose **Sol / Luna** to use the built-in Codex preference:

| Role | Requested model | Requested reasoning |
| --- | --- | --- |
| Lead / direct execution | `gpt-5.6-sol` | `high` |
| First worker | `gpt-5.6-luna` | `medium` |
| First escalation | `gpt-5.6-terra` | `medium` |
| Final escalation | `gpt-5.6-sol` | `high` |

This is an optional preset, not a requirement for other users. Portable defaults leave
`harness.preset` at `manual`, which requires explicit configuration before planning. The menu
always offers Sol / Luna. To make it the default for this installation, merge the `harness` object
from [sol-luna.json](../examples/harness/sol-luna.json) into the ignored `config.json` and restart.
Merge rather than overwrite other provider settings. [claude.json](../examples/harness/claude.json)
is a separate configurable example; it is not part of the OpenAI-only Sol/Luna preference.
Model IDs and effort support depend on the installed provider and account; examples make requests,
not availability guarantees. Rejection stays a failed attempt and never silently upgrades a model.

Custom preset names are allowed under `harness.presets`. Specify provider, model and reasoning
for the lead, worker, and every escalation. A preset stays within one provider. The current effort
choices for harness presets are low, medium and high. `delegation_enabled: false` forces direct
execution on the explicit lead, including for a comparison control. No provider configuration,
credential, global instruction file, or model catalog is rewritten by selecting a preset.

## One bounded package

1. Enter an exact task and category. Supply only necessary relative file references: a short
   STATE, project instructions/CANON and relevant source are useful inputs. Empty inputs are valid.
2. List allowed writable files or directories. Empty write scope means read-only work. Absolute
   paths, globs, traversal and symlinks escaping the project are rejected. These paths constrain the
   assignment; provider permissions still control the actual workspace. This is not a file-level
   sandbox. Codex read-only packages also request its read-only sandbox.
3. Specify the acceptance check independently before execution, its expected artifact file, and
   conditions requiring a stop or escalation. For visual work, the acceptance check can require
   inspecting the actual render. A metric alone does not establish visual acceptance.
4. Estimate direct execution and the separate briefing, worker execution, verification and expected
   rework costs. The browser uses common effort points: weight model work consistently, including
   lead review. They are estimates, not measured dollars or subscription credits. API callers can
   label estimates `api_usd` or `seconds`. Unknown values stay blank.
5. **Plan package** applies the preset's lead selection to the parent session and computes a route locally. Delegate only when all estimates are known and
   `briefing + execution + verification + expected rework < direct`. Ties, unknown estimates,
   trivial categories and presets disabling delegation stay direct. Inspect the requested model
   and reasoning and the previous session selection before choosing **Launch**.
   Expand the saved contract and prospective estimates to inspect scope, acceptance and each
   overhead component. These remain available after reloading the page.
6. A direct package runs in the parent session on the preset lead. A delegated package gets a
   fresh work session with a parent link and no inherited conversation. It receives the same
   compact contract and file references, not a copied transcript. Project/provider instructions
   can still be loaded by the CLI. Worker choices do not become the defaults for new lead sessions.
   Return to the parent when the worker finishes; the interface opens its package review.
7. Inspect the artifact and the independently specified check. Record evidence and Accept or
   Reject. A successful provider exit only means **awaiting_review**. Acceptance requires an
   existing artifact file; the review records its SHA-256 and size. For an artifact over 16 MB,
   use a small verification report referencing it. Evidence is operator-recorded, not an automatic
   attestation that the check was run. Review/rework seconds default to estimated; select measured
   only when timed. Blank is unknown, not zero.
8. A rejected attempt can **Plan retry** with evidence explaining the defect in the model or brief.
   Leave Revised task blank for the next unused escalation setting, or supply a changed brief.
   The API also accepts a revised contract while preserving the original acceptance check.
   A direct lead failure requires a changed brief when there is no higher configured setting;
   the retry does not downgrade the lead to a worker. It rejects identical assignments and stops
   after three launches. A retry never launches automatically. A different target is a new package.

One artifact review is recorded per attempt. Old verdicts and attempt contracts remain intact and
can be expanded in the interface, alongside recorded review/rework effort and the package summary.
A worker session cannot continue through its ordinary composer; a further attempt belongs to the
parent package. Concurrent package launch into a running project, duplicate launch and deleting a
running parent are rejected. Provider cancel and interrupted-run recovery use the existing lifecycle.
The task stops after its artifact and check; extra findings do not become an automatic backlog.
Failed launches count as failed attempts. Restart recovery also reconciles attempts interrupted
before their response message was saved; absent usage and elapsed time remain unknown.

## Records and measurement

`chats.json` remains the source of truth. Parent sessions contain `harness_tasks`, contracts, policy
snapshots, requested settings, attempts, review events and a derived summary. Child sessions contain
`harness_parent`. Assistant messages link to the parent/task/attempt and keep a completion snapshot.
The existing `runs.jsonl` contains the same reference, not another usage observation. Review updates
belong to the parent task; completion snapshots are not a separate acceptance history.

Each attempt records category, parent/child IDs, requested model/effort, provider-reported runtime
fields where available, elapsed monotonic seconds, observed usage, review/rework effort, retry index
and acceptance. The prior session selection is configuration evidence, never runtime confirmation.
The UI presents requested and reported fields separately; omitted runtime metadata stays unknown.
Provider-reported IDs are not independent attestation of model weights.

Measurement helpers distinguish `measured`, `estimated` and `unknown`. They deduplicate observations
and attempts by ID. For cumulative snapshots of one scope they use a maximum, never a sum. Mixed
cumulative/delta observations, missing main counters and overlapping or indeterminate scopes cannot
produce a complete total. Inherited context estimates are never added to usage. Billing input
already accounts for context charged by the provider; live-context occupancy is a different metric.

Codex's `turn.completed.usage` is recorded as invocation usage; its live-context snapshots remain
separate. The [official non-interactive Codex interface](https://learn.chatgpt.com/docs/non-interactive-mode)
documents these completion events. Some CLI streams report no runtime model or effort. Claude's final result counters are
retained, including separate cache read/creation counters, but their per-task scope is not established
by the inspected CLI. Its normalized task usage therefore remains unknown. The raw counters appear
separately with the scope limitation. Missing telemetry on failure or cancellation remains unknown.

API-equivalent cost and subscription consumption are separate fields, currently unknown. This
implementation does not ship assumed price tables, translate subscription allowance into tokens, or
compute savings from cheap-model assignment percentages. Even a complete usage total is not a matched
savings estimate. Outcome summaries include failed/rejected work and unknown effort; they do not
silently discard it. See the [baseline](harness-baseline.md) and
[reusable matched comparison](../examples/harness/comparison/README.md).

## Product and provider responsibilities

| Responsibility | Owner |
| --- | --- |
| Presets, economic decision, contract validation and handoff rendering | `pilferedparrot/harness.py` |
| Package lifecycle, parent/child links, bounded retries and artifact verdicts | `web_harness.py`, using existing Work sessions |
| Storage, ownership checks, HTTP authorization, run/resume/cancel and browser UX | Existing PilferedParrot modules, extended in place |
| Outcome deduplication, provenance and conservative summaries | `harness.py`; stored with the package |
| CLI flags, structured runtime metadata and usage translation | Codex/Claude adapters in `dispatch.py` |
| Credentials, provider-native tools, workspace sandbox/permissions, compaction and session execution | Provider CLI |
| Task decomposition, independent acceptance design and final judgment | Lead/operator; the UI does not buy a second model review |

Bounded Codex invocations request `agents.enabled=false`, which current
[OpenAI subagent documentation](https://learn.chatgpt.com/docs/agent-configuration/subagents)
defines as disabling native multi-agent tools. A thread count of one would still allow a worker;
it is not used as a disabling mechanism. Claude invocations disallow `Agent` and `Task` and pass an
explicit model/effort. Keep provider CLIs current enough to support those options. CLI behavior is
not a guarantee against an agent invoking another process through a shell. Existing sandbox and
permission policy remains the enforcement boundary. Ordinary Work retains native provider behavior.

PilferedParrot launches a single explicit work session for a package and reuses provider tool loops.
It does not implement a parallel graph executor, clone native agent state or synchronize native
child transcripts into another harness. Native trees created outside this bounded workflow remain
provider records, not automatically imported task records. Codex and Claude are the supported
harness execution adapters in this release; other providers keep ordinary Work.

## Sharing and migration

See [migration and inventory](harness-migration.md). Share the code, portable presets, contracts with
reviewed relative references, and synthetic comparison fixtures. Keep ignored `config.json`, chat
stores, provider transcripts, registry data, credentials and unreviewed STATE/LEDGER contents private.
There is no raw session export button. The baseline command emits aggregate metadata only.

The [2026-09-06 comparison pilot](harness-comparison-results.md) records six accepted fixture
outcomes, measured invocation usage and elapsed time, estimated review effort, and evidence limits.
