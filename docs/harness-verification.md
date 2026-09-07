# Harness preview verification — 2026-09-06

This preview integrates the local Harness and provider-lifetime changes with the published
0.6.1 Linux/Windows source. The original development checkout and private state were backed up
before integration. Shared configuration stays manual; provider credentials and global model
settings are unchanged.

## Checks

Full unittest discovery on Linux with Python 3.14.7 and mandatory Playwright discovered **525
tests: 523 passed, no failures, two native-Windows cases skipped on Linux**. Python parsing, all
browser JavaScript syntax, JSON duplicate-key validation and whitespace checks passed. Mandatory
Playwright uses isolated loopback servers, synthetic provider streams and disposable browser state.
The native Windows console cases run in the required Windows CI job.

Independent backend review covered contract/path validation, provider permissions, project overlap,
launch registration, cancellation, restart recovery, bounded retries, additive persistence and
conservative usage summaries. Frontend review inspected the desktop and narrow renders and tested
planning, direct/delegated execution, worker return, review, retry, acceptance, reload and the
three-attempt limit. Typed review drafts and disclosure state remain stable during redraws.

The real Codex comparison ran six isolated fixture arms. All passed unchanged acceptance checks
and independent lead artifact review with no retries or substitutions. See [the full pilot record](harness-comparison-results.md)
for invocation usage, elapsed time, estimated review effort and its disclosed protocol deviation.
It establishes functional coverage, not savings or broad model quality.

## Limits

The comparison used Codex CLI 0.153.4 on Linux. Runtime model and effort were not reported by the
CLI stream; explicit requested settings are retained. Claude Harness behavior has synthetic adapter
coverage, not a new live account certification. Existing provider-account and platform evidence
remains in [release readiness](release-readiness.md).

Acceptance evidence is operator-recorded. Assignment write scope is not an OS sandbox. Unknown
usage scope, briefing/setup cost, API-equivalent cost and subscription consumption remain unknown.
The product does not restore external hooks, clone native agent trees or add a background scheduler.

Reproduce local verification using the environment described in [Contributing](../CONTRIBUTING.md):

```bash
PYTHONDONTWRITEBYTECODE=1 PILFEREDPARROT_REQUIRE_PLAYWRIGHT=1 \
  python3 -m unittest discover -s tests -v
```
