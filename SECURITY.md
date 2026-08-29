# Security Policy

## Supported versions

Security fixes are applied to the current default branch. No stable release line is supported yet.

## Reporting a vulnerability

Do not open a public issue for a suspected vulnerability. Use the repository's private
**Security advisories → Report a vulnerability** flow. If private reporting is unavailable, contact
a repository owner through GitHub and request a private channel before sharing exploit details,
credentials, local paths, or provider output.

Include the affected commit, prerequisites, impact, and a minimal reproduction. You should receive
an acknowledgement within seven days. Please allow time for a fix before public disclosure.

## Security model

AI Conductor is a local, single-operator application. Its web server is intended to bind only to a
loopback address. It does not provide accounts, multi-user authorization, TLS, or safe exposure
through a LAN listener or reverse proxy. Processes running as the same OS user are inside the trust
boundary and can access AI Conductor's state and local HTTP service.

Provider prompts and outputs may contain sensitive source code. Chats, board events, and run
metadata are stored locally with owner-only file permissions, but the selected provider CLI or
Qwen endpoint receives the prompt and any tool output needed for the task. Users are responsible
for the data-handling terms and configuration of those providers.

Qwen file tools resolve paths inside the selected workspace. Qwen shell tools require Bubblewrap;
the workspace and ephemeral `/tmp` are writable, the operator's home outside the workspace is
hidden, inherited environment variables are reduced to a small allowlist, and other mounted host
paths are read-only. Network access is disabled unless `qwen.shell_network` is explicitly enabled.
This limits accidents but is not a hardened boundary against hostile kernel,
Bubblewrap, compiler, or project inputs. Use a disposable VM or container for untrusted code.

Claude Code and Codex execute through their own CLIs. Their sandbox, permissions, authentication,
and network behavior remain independent security boundaries.

The message board is passive, append-only data. It must never authorize work or enter a provider
prompt implicitly. Model-authored events can only be exact results from successful monitored runs.
See `POLICY.md` for the enforceable trust rules.
