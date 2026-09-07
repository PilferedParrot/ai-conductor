# Harness migration

Harness reuses the product's Work sessions, provider dispatch, chat store and run ledger.
It adds package contracts, parent/child links, attempt records and artifact verdicts. It does
not import provider transcripts, install hooks, replace credentials or create a separate scheduler.

## Upgrade without losing state

1. Complete or cancel active provider work before restarting with a different application version.
   Back up the existing chat store using your usual local backup process; keep that backup private.
2. Run the updated product against its existing state directory. Harness fields are additive to
   version-8 work sessions. Existing conversations, provider continuations, imported legacy chats
   and historical ledger rows remain readable.
3. Open the correct provider and project, then choose a preset in Harness. Shared defaults remain
   manual; Sol/Luna is an optional built-in preset. To choose a default, merge only the `harness`
   object from a reviewed portable example into the ignored local configuration.
4. Enter project-relative inputs and writable artifacts in the package contract. A legacy STATE,
   LEDGER or CANON file can be an input. Include it in write scope only when updating it is part
   of the assigned task. Do not expose unrelated history or symlink outside the project.
5. Verify one small package through planning, launch, artifact inspection and a recorded verdict.
   Old prose verdicts remain historical evidence; they are not backfilled as measured outcomes.

## Existing external workflows

| Existing component | Migration approach |
| --- | --- |
| Global Codex or Claude instructions | Keep personal policy external. The selected package passes explicit model/effort settings; requested settings remain distinct from runtime reports. |
| Explicit work skills or registries | Keep them usable. Resolve the needed project and enter reviewed relative references manually; no registry or transcript is bulk-imported. |
| STATE / LEDGER / CANON / WORKLIST conventions | Reuse necessary files as task inputs. Existing project history stays append-only when an update is explicitly assigned. |
| Binding utilities or legacy session indexes | Retain them for their existing workflows. Product package identity uses native Work session IDs and parent links. |
| Retired hooks, standing reviewers or maintenance queues | Leave them retired. Harness adds no activation, background queue or second-model review. |
| External usage or cost reports | Retain provenance and scope. Do not import inferred subscription consumption as measured cost. |

The product does not modify global provider instructions or configurations. Conflicting personal
instructions can still affect provider behavior; inspect requested settings and provider reports.
Provider CLI tools, authentication and permissions remain provider-owned. Task write scope is an
assignment contract, not an OS file sandbox. Returning to an older app while packages are running
is unsupported; complete or cancel them first and retain a backup.

See [the workflow and measurement guide](harness.md) and [comparison evidence](harness-comparison-results.md).
