# Security Policy

## Supported versions

Security fixes are applied to the latest release and the current default branch. Older minor
release lines are not supported.

| Version | Supported |
| --- | --- |
| 0.3.x | Yes |

## Reporting a vulnerability

Do not open a public issue for a suspected vulnerability. Use the repository's private
**Security advisories → Report a vulnerability** flow. If private reporting is unavailable, contact
a repository owner through GitHub and request a private channel before sharing exploit details,
credentials, local paths, or provider output.

Include the affected commit, prerequisites, impact, and a minimal reproduction. You should receive
an acknowledgement within seven days. Please allow time for a fix before public disclosure.

## Security model

PilferedParrot is a local, single-operator application. Its web server is intended to bind only to a
loopback address. It does not provide accounts, multi-user authorization, TLS, or safe exposure
through a LAN listener or reverse proxy. Processes running as the same OS user are inside the trust
boundary and can access PilferedParrot's state and local HTTP service.

Provider prompts and outputs may contain sensitive source code. Chats and run
metadata are stored locally with owner-only file permissions, but the selected provider CLI or
Qwen endpoint receives the prompt and any tool output needed for the task. Users are responsible
for the data-handling terms and configuration of those providers.

Qwen file tools resolve paths inside the selected workspace. Qwen shell tools require Bubblewrap;
the workspace and ephemeral `/tmp` are writable, the operator's home outside the workspace is
hidden, inherited environment variables are reduced to a small allowlist, and other mounted host
paths are read-only. Network access is disabled unless `qwen.shell_network` is explicitly enabled.
Selecting the operator's entire home directory as the Qwen workspace is rejected unless
`qwen.allow_home_workspace` is explicitly enabled, because that selection exposes credentials and
documents that the normal home mask protects.
This limits accidents but is not a hardened boundary against hostile kernel,
Bubblewrap, compiler, or project inputs. Use a disposable VM or container for untrusted code.

Codex executes through its own CLI. Its sandbox, permissions, authentication, and network behavior
remain an independent security boundary. The selected project is writable in `workspace-write`
mode. Operators may configure narrowly scoped `codex.additional_write_dirs`; PilferedParrot validates
them and passes them as Codex `--add-dir` roots on new and resumed turns. Every such root expands
the model's write authority, so prefer project directories and do not grant the whole home folder
without deliberately accepting that scope. PilferedParrot has no Claude Code integration or automatic
provider router, delegator, or second-model review loop.

The optional Chat pane is a separate, read-only Codex session. User messages go to that session
directly; PilferedParrot does not inject technical messages or conversation metadata. Chat cannot
switch, interrupt, rewrite, or relay technical requests, widen
filesystem permissions, or bypass the technical provider's normal sandbox.

All mutating browser requests require a per-server CSRF token plus loopback peer, Host, and Origin
checks. Keep `web.host` on a loopback address; remote exposure is not supported.

Completed assistant responses may offer a terminal button for single-line fenced commands. A click
first shows the exact command and project folder for confirmation, then launches the stored command
in a graphical terminal rooted at the conversation's project folder; the browser cannot supply a
replacement command or working directory. The command runs as
the operator and may request elevation through `sudo`, so inspect it before clicking. This action is
not covered by the Qwen Bubblewrap sandbox.
