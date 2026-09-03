# PilferedParrot 0.5.0 release notes

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
