# PilferedParrot Interface 0.6.1 release readiness — 2026-09-06

Version 0.6.1 fixes native terminal visibility and command display on Linux and Windows.
The command and working folder appear before execution or an interactive password prompt.
The Windows terminal retains console input and output. Focus remains subject to the desktop.

The terminal implementation at `22d71ea` passed all required CI checks on Python 3.12, 3.13,
and 3.14, Playwright Chromium, Windows x64 packaging, and CodeQL. Linux CI discovered 490
tests, with 34 browser tests run separately and two Windows-only cases exercised on Windows.
Windows passed 38 focused platform tests and all 34 browser tests; the portable executable
passed help and self-test checks. A native Linux/X11 check confirmed a visible terminal,
command display before simulated input, accepted input, and restoration from minimized state.
No actual password or privileged command was used. Final release documentation is checked
on the release branch by the same required workflows.

The source archive and Windows ZIP are distributed with SHA-256 checksums. Windows remains
an unsigned preview. Native Windows 10/11 foreground behavior and Wayland focus were not
interactively certified. Provider-account coverage remains the historical evidence below.

## Earlier release evidence

# PilferedParrot Interface 0.6.0 release readiness — 2026-09-05

Version 0.6.0 retains the stable Linux release and adds a portable Windows 10/11 x64 preview.
Release assets are
[`PilferedParrot-0.6.0-windows-x64.zip`](https://github.com/PilferedParrot/PilferedParrot-Interface/releases/download/v0.6.0/PilferedParrot-0.6.0-windows-x64.zip)
and
[`pilferedparrot-0.6.0-source.tar.gz`](https://github.com/PilferedParrot/PilferedParrot-Interface/releases/download/v0.6.0/pilferedparrot-0.6.0-source.tar.gz).
The Windows package bundles Python, requires no administrator rights, uses Chrome/Chromium/Edge,
keeps state under `%LOCALAPPDATA%\PilferedParrot`, and defaults projects to
`%USERPROFILE%\PilferedParrot Projects`. Its console remains part of the app lifetime.

Release validation covers 480 local tests with mandatory Playwright, Windows browser and platform
checks, and a native executable smoke test on the Windows Server 2022 runner. No real Windows provider account,
provider CLI, or model validation is claimed. The preview supports browser Chat and file tools;
Bubblewrap shell execution is disabled, and supported npm provider shims are resolved through Node
without `cmd.exe`. The 0.5.1 Linux validation record below is retained unchanged.

The [Windows release candidate checks](https://github.com/PilferedParrot/PilferedParrot-Interface/actions/runs/34000685450)
passed all 34 platform tests, all 33 browser tests, the applicable process-cleanup checks,
the source launcher, and the bundled executable self-test. The four POSIX-only cleanup checks
are intentionally skipped on Windows. The local Linux suite passed all 480 tests with no skips.
The landing page passed image, script-error, and overflow checks at 1440, 390, and 320 pixels.
Windows x64 is now a required check alongside the three Python versions, Linux Playwright,
and the existing CodeQL security requirement.

## 0.5.1 historical readiness record

The interface and desktop integrations have passed automated and live validation on
Linux Mint/X11. These checks describe the local source validation checkpoint; provider access remains
account-specific. The 0.5.1 release is a repository/governance release that renames the project and
exits preview. The complete 440-test suite was rerun for 0.5.1 with mandatory Playwright: all
passed, with zero skips or failures. The renamed landing page also passed desktop and mobile
checks at 1440, 390, and 320 pixels. No new provider-account or platform coverage is claimed.
See the [v0.5.1 release](https://github.com/PilferedParrot/PilferedParrot-Interface/releases/tag/v0.5.1)
for final GitHub CI and publication evidence; the earlier live-validation evidence is preserved below.

The subsequent provider expansion passed **440 tests with mandatory Playwright, zero
skips or failures**. Antigravity and LM Studio now have live validation, while OpenRouter
and Mistral retain explicit account prerequisites. See
[provider expansion validation](provider-expansion-validation.md) for the new results
and Antigravity's Work-only limitation. The results below describe the preceding candidate.

## Verification

Full unittest discovery on Python 3.14.7 with mandatory Playwright ran **414 tests:
414 passed, zero skipped, zero failures**. This includes **30 browser tests** and both
Bubblewrap integration tests. The earlier Python 3.12 run covered the preceding
394-test candidate; the final 414-test suite was run on Python 3.14.

All five JavaScript assets passed Node syntax checks. Python compilation, desktop
launcher shell syntax, the browser-check helper shell syntax, and `git diff --check`
passed. Browser regression fixtures use synthetic providers and isolated state.

Live requests used disposable workspaces with synthetic marker files. User projects,
chat history, and credentials were not included in test prompts.

| Integration | Live result |
| --- | --- |
| Codex CLI 0.153.4 / gpt-5.6-luna | Reply, native session continuation, synthetic file read, cancellation passed. Work UI reply, continuation, cancellation, reload persistence, and separate Chat reply passed with no JavaScript errors. |
| Local Qwen / qwen3-coder-next | Configured automatic startup, file tool, second user-turn continuity, and cancellation passed. No further tool/progress events after cancellation. Local services restored to their initial stopped state. |
| Claude Code 2.1.257 | Signed out. Live reply/continuation/tool/cancellation validation awaits the user's provider-owned login. |
| Gemini CLI 0.51.0 / gemini-2.5-flash-lite | Actual requests rejected with exit 55 / IneligibleTierError. Consumer Google sign-in no longer supplies CLI access. API or eligible organization authentication is needed. |
| Additional compatible APIs | Offline adapter and browser setup checks passed. Google AI Studio, xAI/Grok, OpenRouter, and other remote accounts were not live-certified without their API credentials. |

Actual desktop checks passed for native folder selection and cancellation, browser URL
handoff, window-close callback, and browser notifications reaching Cinnamon through
`org.freedesktop.Notifications.Notify`. Notification permission was explicitly granted
in an isolated test browser context; the user's own notification preference was not changed.

Desktop and narrow layouts have browser regressions. The earlier visual review checked
Chat at 320, 390, and 1440 pixels with no horizontal document overflow; the final live
Work and Chat screenshots were also inspected.

## Finishing changes

- Shared Work/Chat Markdown preserves underscores inside identifiers.
- Claude's explicit signed-out status is recognized with its normal exit-1 JSON result;
  malformed responses and unrelated CLI failures remain unverified.
- Gemini credential-file presence no longer claims authenticated or reachable access.
  Installed, unverified CLI status is described as checked when used. Rejected account
  tiers receive a concise explanation of supported authentication options.
- Add provider includes Google's documented Google AI Studio/Gemini API endpoint.
- Browser regressions cover manual model IDs, failed discovery retaining the editable
  draft, and failed polling preserving model selection without changing providers.
- Previous onboarding, reasoning, session-default, routing-identity, responsive layout,
  folder-requirement, provider-update, redaction, and sandbox fixes are retained.

## Remaining account prerequisites

Complete Claude sign-in to run its live tests. Gemini CLI requires supported API or
organization authentication; the separate Google AI Studio card requires a Gemini API
key. Direct xAI/Grok and other keyed APIs require credentials for the chosen endpoint.
A Cursor subscription does not supply an xAI API key, and Cursor is not a native PPI
adapter. See [provider compatibility](provider-compatibility.md) for official sources,
protocol requirements, credential handling, and the limits of compatibility claims.

The running PPI instance had an active response during delivery and was left running.
Reopen the app after that response completes to load the changed runtime and assets.
The existing desktop launcher already points to this checkout.

Reproduce the automated browser checks with `bin/check-browser-ui`; full-suite setup
and the mandatory Playwright command are described in [CONTRIBUTING.md](../CONTRIBUTING.md).
