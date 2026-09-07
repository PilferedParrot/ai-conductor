# Bounded baseline: routing established, savings unestablished

Read-only convenience sample with an exclusive cutoff of **2026-09-06 02:40 UTC**
(September 5, 21:40 CDT). The resumed inspection reproduced the counts below. No paid comparison was launched.
Raw session identifiers, prompts, paths, private state and credentials are omitted.

| Evidence | Observation |
| --- | --- |
| PilferedParrot sample | Latest 12 assistant replies created before cutoff with completed exit records at inspection, across 8 work sessions |
| Requested models | 11 Astra, 1 Terra |
| Requested reasoning | 11 xhigh, 1 medium |
| Exits | 10 zero, 2 nonzero; a zero exit is not independent acceptance |
| Historical measurement gaps | 0 reported-model identity records; 0 package outcome records; 0 per-message usage-observation records |
| Codex sample | Latest 6 content-completed sessions as of cutoff within 50 most recently modified candidate files; 4 roots and 2 children |
| Runtime context metadata by session | 2 Astra, 3 Terra, 1 Luna; 2 xhigh and 4 medium |
| Explicit spawn requests within sampled sessions | 1 Luna/medium and 1 Terra/medium; both requested fresh context (`fork_turns=none`) |
| Usage | All 6 Codex sessions had cumulative usage snapshots; they were not summed across overlapping scopes |

The stores can overlap; these are not 18 independent tasks. Shortlisting by file modification time
bounds the search and introduces selection bias. Codex completion is established from content events,
not file age. Legacy PilferedParrot replies record creation time, not completion time: their exits
establish completion at inspection, not necessarily before the cutoff. Some sessions include multiple turns. Worker requests in sampled parents and sampled
child sessions are different observations, not a complete joined delegation tree.

The sample establishes that actual routing diverged from the standing Sol/high policy and that
explicit economy-worker delegation was occurring. It does not establish model superiority, task
quality, final acceptance, or net savings. No matched direct control, reliable per-package review
or rework effort, complete lineage accounting, or subscription consumption is available. Some
session-level usage exists, but attributing it to individual replies would invent measurements.
The active top-level Codex config also requested Astra/high; UI/session overrides explain why it
cannot be treated as proof of the effective setting of every run.

To repeat a bounded inspection on explicitly chosen local data, use:

```bash
python3 -m pilferedparrot.harness_baseline \
  --chat-store "$PPI_CHAT_STORE" --codex-sessions "$PPI_CODEX_SESSIONS" \
  --before 2026-09-06T02:40:00Z --limit 12 --session-limit 6 --scan-limit 50
```

Set the two variables to your private store locations; do not commit them or the underlying data.
The command emits only aggregate labels/counts, caps candidate count and transcript size, and never
runs a provider. Newer files may change the bounded candidate shortlist. It does not import data or
attempt to attest the loaded model weights.

Three [matched fixture pairs](../examples/harness/comparison/README.md) are prepared for a later,
explicitly authorized comparison. They cover implementation, regression-test writing and mechanical
refactoring. Acceptance checks are specified independently and kept outside write scope. The seeded
baselines fail locally by design; this is fixture verification, not evidence about model outcomes.
Collect direct and delegated costs with the same targets and reviewer standards before making a
savings claim. Include all failed attempts, briefing, review and rework, and keep measured usage,
API-equivalent estimates and subscription consumption separate.

## Subsequent pilot

The [2026-09-06 live comparison](harness-comparison-results.md) completed the six prepared arms.
It does not change this historical sample or establish savings.
