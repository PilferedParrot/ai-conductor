# Reusable direct/delegated comparison

Three small matched pairs provide isolated inputs for an explicitly authorized experiment. A
[contained pilot](../../../docs/harness-comparison-results.md) completed all six arms; the fixtures
here remain unsolved baselines for reuse. The pilot establishes functional coverage, not savings.

| Pair | Category | Independently specified acceptance | Order |
| --- | --- | --- | --- |
| 1 | Implementation | Trim newline-delimited labels, drop blanks and stable-deduplicate while preserving case | Direct, delegated |
| 2 | Regression-test writing | Submitted test must fail the zero/None defect and pass a supplied correction; production code stays unchanged | Delegated, direct |
| 3 | Mechanical refactor | Positive integer/count validators retain behavior and actually share implementation | Direct, delegated |

Each pair has identical source baselines in `direct/` and `delegated/`, a contract manifest for each
arm, and an independent `check_acceptance.py` outside write scope. The six seeded checks intentionally
fail. They must not be counted as failed model runs. Pair 2's checker verifies both defect detection
and success with corrected behavior, so a test that always fails cannot pass acceptance.

For a future run:

1. Copy the entire pair directory to a separate temporary project for each arm. Select that copied
   pair as PilferedParrot's Project folder. Retain only the relevant arm's inputs in the brief; never
   show one arm's output to the other. Source/check hashes, provider versions, permissions, context
   policy and acceptance target must match across the pair.
2. In the direct installation/config, use the Sol/Luna preset with `delegation_enabled: false`.
   In the delegated condition enable delegation and use a recorded prospective estimate of all
   overhead. If that estimate is unfavorable, record the skip rather than fabricating estimates
   to force a worker. An experiment requiring forced random assignment needs a separately declared
   experimental protocol; it is not a savings claim from this production router.
3. Enter the arm's manifest contract in Harness. Use a fresh parent session for each arm. Sol/high
   executes direct; Luna/medium executes the delegated package. Planning/briefing and reviewer time
   count toward the respective arm, including any lead model call made to prepare the brief.
4. Launch only after experiment authorization. Run the fixed acceptance command from the manifest.
   Do not allow edits to the independent checker. A human/lead reviews each final artifact to the
   same standard and records verdict, evidence and effort in the package. Include every retry and
   any escalation, retaining the original acceptance check.
5. Compare accepted outcomes, full usage when scopes are known, total elapsed, briefing, review and
   rework. `run_log.json` is a blank worksheet for experiment-only facts absent from session records,
   such as arm order and starting snapshot; references to product records should replace copied
   token totals. Unknowns remain null. Store filled worksheets privately.

Counterbalancing three pairs helps expose order effects; it does not remove task-selection bias,
small-sample uncertainty, variable cache state, model nondeterminism or unequal reviewer effort.
Report failures and incomplete evidence. A high percentage of cheap-worker assignments or lower
wall-clock time alone cannot establish monetary or subscription savings. API-equivalent prices and
subscription consumption use different units and must stay separate.
