# Anthropic provider-owned usage and credential design

Research date: 2026-09-03. This began as a design-only record. The research
conclusions below are preserved; implementation-status notes now identify the
parts shipped by the 0.5.0 migration.

## Conclusion

**Partially available.** Anthropic provides supported, provider-owned,
organization-level interfaces for Claude Code analytics and API/Enterprise
usage and cost reporting. They are not a supported replacement for
PilferedParrot's former live, individual Claude.ai plan-allowance display or
the former direct refresh of a Claude Code OAuth credential.

In particular, the supported reporting interfaces are historical aggregates
(daily for Claude Code analytics) and require an organization-level
administrative or Enterprise Analytics credential. They do not return the
current five-hour/weekly allowance or reset time used by the pre-migration
sidebar.
The Claude Code and Agent SDK documentation also says that, unless previously
approved, third-party developers may not offer Claude.ai login or rate limits.

## Official Anthropic capability

| Need | Supported Anthropic interface | Fit for PilferedParrot |
| --- | --- | --- |
| Claude Code auth and credential renewal | [Claude Code authentication](https://code.claude.com/docs/en/authentication) documents CLI-owned login, automatic profile refresh, renewal, and logout. It states that Claude Code manages `.credentials.json` through `/login` and `/logout`. | Use the CLI as the credential owner. There is no documented third-party refresh API for PilferedParrot to call with the CLI credential. |
| Claude Code organization analytics | [Claude Code Analytics API](https://platform.claude.com/docs/en/manage-claude/claude-code-analytics-api) and its [endpoint reference](https://platform.claude.com/docs/en/api/http/admin/usage_report/retrieve_claude_code) provide daily, per-user aggregated sessions, productivity, tool, estimated-cost, and token data. The API is not available to individual accounts, is not real-time, and does not include third-party cloud deployments. | Optional, separate organization reporting only. It cannot drive a personal live allowance bar. |
| Console API usage and cost | The [Usage and Cost Admin API message-usage reference](https://platform.claude.com/docs/en/api/http/admin/usage_report/retrieve_messages) reports organization API activity. | Optional, separate API-billing reporting only; not Claude.ai subscription quota. |
| Claude Enterprise product analytics | [Analytics APIs](https://platform.claude.com/docs/en/manage-claude/analytics-api) documents Enterprise token/cost/activity reports across Claude surfaces. | Optional Enterprise reporting only; data is historical and requires an Analytics API key with `read:analytics`. |
| Admin authorization | The [Admin API overview](https://platform.claude.com/docs/en/manage-claude/overview) requires an Admin API key or an OAuth token with `org:admin`, and says the Admin API is unavailable to individual accounts. | Never substitute a user's Claude Code/Claude.ai session credential. Treat this as an explicit administrator integration. |

Anthropic announced the Claude Code Analytics API on **2025-09-10** in its
[official platform release notes](https://platform.claude.com/docs/en/release-notes/overview).
The documentation pages above were checked on 2026-09-03; where a page did not
show its own publication or update date, this record does not infer one.

## Historical pre-migration PilferedParrot surface

This section describes the 0.4.x behavior that motivated the migration. It is
historical, not a description of the 0.5.0 runtime.

`pilferedparrot/budgets.py` coupled the Claude allowance display to Claude
Code's local credential implementation:

- `_claude_credentials_path`, `_load_claude_credentials`, and
  `_claude_access_token` read `CLAUDE_CODE_OAUTH_TOKEN` or
  `$CLAUDE_CONFIG_DIR/.credentials.json` (default `~/.claude/.credentials.json`).
- When the access token is near expiry, `_claude_access_token` sends the stored
  refresh token and a hard-coded client ID to `CLAUDE_TOKEN_URL`, then atomically
  rewrites rotating access/refresh tokens, expirations, and scopes into the
  Claude Code credential file (`0600`).
- `read_claude_budget` sends that bearer token to `CLAUDE_USAGE_URL` with an
  `oauth-2025-04-20` beta header and converts five-hour and weekly windows in
  `claude_budget_from_response`.
- `read_claude_status` first runs `claude auth status --json` and then triggers
  that direct budget flow; `collect_budgets` calls it for Claude.

The direct path was separate from normal execution: `dispatch.py` invoked the
Claude CLI in print/stream-JSON mode and received per-turn token telemetry.
`web.py` cached budget snapshots and `/api/budgets` returned normalized
`ProviderBudget` values to dashboard-capability holders. `app.js` rendered the
five-hour and weekly bars. `ledger.py` recorded budget snapshots with run
metadata in an owner-only local ledger. The app's sign-in/out UI invoked the
Claude CLI; it did not intentionally put credential values into browser state.

`ProviderAdapter`/`ClaudeAdapter` in `adapters.py`, plus `ProviderBudget` and
`BudgetWindow` in `model.py`, were the useful existing abstraction boundary.
The 0.5.0 implementation now distinguishes CLI-owned authentication, per-turn
execution telemetry, optional provider-owned organization reporting, and
unsupported plan allowance. No credential reader, refresher, or private
endpoint belongs in that boundary.

## Risks in the historical design

Directly reading and rewriting another product's credential store exposes an
access token, refresh token, scopes, and expiry metadata to PilferedParrot. A
file mode of `0600`, atomic replacement, a lock, and browser-state exclusion
reduce risk but do not make the interface supported. Token rotation also creates
race, availability, and sign-out/revocation failure modes. The undocumented URL,
beta header, credential-file schema, and embedded client ID can change without a
supported compatibility commitment.

Usage snapshots expose account-consumption patterns and reset times to every
dashboard context authorized to call `/api/budgets`, and ledger retention makes
those patterns durable. Organization APIs add more sensitive data, including
user email addresses, activity, tool acceptance, and estimated cost. A generic
personal allowance bar could also imply that a delayed administrative metric is
a live quota, which it is not.

## Recommended architecture

### Default and no-replacement path

1. Let the Claude CLI own login, refresh, revocation, credential placement, and
   execution. PilferedParrot must neither read nor write its credential store,
   invoke a token-refresh grant, nor call an undocumented allowance endpoint.
2. Define a provider-owned `AuthenticationStatus` capability whose only
   supported result is non-secret state such as signed-in, signed-out, expired,
   unavailable, or unknown. If a stable, documented CLI status interface is not
   available, report `unknown`; do not inspect credentials to improve it.
3. Keep execution-derived token counts as local, per-turn telemetry and label
   them as such. Remove/withhold live account-allowance windows when there is no
   supported source. Do not estimate or reconstruct plan quota.
4. Make usage reporting a separate, optional `OrganizationUsageReporter`
   capability, not a method of the CLI auth provider and not a prerequisite for
   running Claude.

### Partial replacement path available now

For a Console organization, an administrator can explicitly enable a Claude
Code Analytics reporter backed by the documented Admin API. For Claude
Enterprise, select the documented Enterprise Analytics API instead. Normalize
its result into a distinct historical-report schema with source, scope,
aggregation period, observed-at time, and freshness. Never coerce it into
`BudgetWindow`, a reset countdown, or an individual plan limit.

The reporter must be disabled by default and must not run merely because Claude
CLI auth exists. It should display its organization scope, daily aggregation,
delay, and coverage limitations. Authentication and execution continue to work
without it.

### If Anthropic later publishes a replacement

Adopt only a documented, generally available interface that explicitly supports
the intended account type and use by an independent application. Add a new
provider-owned implementation behind the reporting/authentication capabilities;
retain the CLI-only fallback until the replacement is validated. Do not infer
permission from a beta header, a CLI implementation detail, or an endpoint used
by Anthropic's own clients.

## Authentication, consent, storage, errors, and fallback

- Obtain explicit, informed consent before enabling organization reporting. Tell
  the user which organization-level fields (including identity/activity/cost)
  are retrieved, who administers the credential, and the retention policy.
- Do not collect, paste, proxy, log, send to browser code, or persist Claude.ai
  OAuth access/refresh tokens, credential-file contents, Admin API keys,
  Analytics keys, or `org:admin` tokens. A future integration may read a
  deliberately provisioned administrator credential only from the host's
  approved secret mechanism at request time; it must never add that secret to
  `config.json`, chat persistence, the ledger, diagnostics, or URLs.
- Grant the least available authority. An Admin API credential is organization
  wide; an `org:admin` token is especially sensitive. Separate a reporting
  process/account from an end-user Claude Code login and support rotation and
  immediate revocation.
- Fail closed and independently: a 401/403, timeout, malformed response, stale
  result, or unavailable reporter produces a non-secret `unavailable`/`unknown`
  result with retry guidance. It must not trigger credential refresh, downgrade
  to an undocumented endpoint, block a Claude run, or change auth state.
- Default to minimal display and retention. Redact email/account identifiers
  from user-facing errors; aggregate or omit identity fields unless the
  administrator explicitly needs them. Apply a bounded retention period to any
  allowed report cache and exclude it from the run ledger by default.

## Migration plan

Migration steps 1–3 are implemented in PilferedParrot 0.5.0. Steps 4–5 remain
future constraints if organization reporting or further schema work is ever
undertaken.

1. **Implemented in 0.5.0.** Add capability interfaces and fixtures first,
   with the existing normalized budget model retained only for compatibility
   at its API boundary.
2. **Implemented in 0.5.0.** Introduce CLI-owned authentication status and an
   unavailable allowance state; keep existing execution and per-turn token
   telemetry unchanged.
3. **Implemented in 0.5.0.** Deprecate the direct Claude allowance source with
   a clear release note and feature-independent fallback. Remove the
   credential-file reader/writer,
   refresh code, hard-coded client ID, private URLs, beta header, and their
   tests only in the implementation release that performs that behavior change.
4. Add organization reporters only behind explicit administrator configuration
   and consent. Migrate no local Claude credentials or existing user settings;
   require deliberate fresh provisioning through the host secret mechanism.
5. Preserve old clients by returning a documented unavailable/unsupported usage
   state rather than fabricated windows. Version persisted report data and do
   not write secrets into any migration.

## Tests required before implementation

- Contract tests for CLI-owned signed-in/signed-out/expired/unknown status that
  prove no Claude credential file or OAuth token endpoint is read or written.
- Provider-adapter tests separating run telemetry, live allowance, and
  organization historical reports; verify historical data cannot render as a
  resettable quota bar.
- Secret-safety tests for configuration serialization, browser API responses,
  logs/errors, chat persistence, diagnostics, and ledger entries.
- Consent, disabled-by-default, role/scope, organization-selection, and
  revocation/rotation tests for each organization reporter.
- HTTP contract tests using fixtures only for pagination, date boundaries,
  delayed/stale data, 401/403/429/5xx/malformed responses, and no fallback to
  undocumented endpoints. Add live tests only in a separately approved,
  non-production Anthropic organization.
- UI accessibility and copy tests for unavailable data, delay/scope disclosure,
  and assurance that failed reporting never blocks Claude execution.
- Migration tests covering existing `ProviderBudget` consumers and persisted
  ledger data while proving that no credential material is migrated or exposed.

## Non-goals and unsupported approaches

- The original research record was not implementation authorization and did
  not itself add an Anthropic integration, credential collection,
  authentication change, or runtime probe.
- Do not use `/api/oauth/usage`, `platform.claude.com/v1/oauth/token`, a
  Claude Code credential-file schema, an embedded OAuth client ID, a beta header,
  reverse-engineered CLI traffic, browser/session cookies, or third-party
  wrappers as a product contract.
- Do not present Claude Code Analytics, Admin Usage/Cost, or Enterprise
  Analytics data as a personal Pro/Max/Teams live quota or reset countdown.
- Do not use the Agent SDK as a way to resell/proxy Claude.ai login or rate
  limits; the [Agent SDK overview](https://code.claude.com/docs/en/agent-sdk/overview)
  explicitly disallows that without prior Anthropic approval.
