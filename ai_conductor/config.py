from __future__ import annotations

import json
import os
import shutil
from copy import deepcopy
from glob import glob
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent.parent

# Desktop launchers and systemd units inherit a minimal session PATH, so CLIs
# installed by npm -g / volta / bun / homebrew are invisible to us even though an
# interactive terminal finds them instantly (the PATH export usually lives in
# ~/.bashrc, which non-interactive shells skip). Look here before concluding that
# a provider is not installed.
CLI_SEARCH_PATHS = [
    "~/.npm-global/bin",
    "~/.local/bin",
    "~/.local/share/npm/bin",
    "~/.yarn/bin",
    "~/.bun/bin",
    "~/.volta/bin",
    "~/.nvm/versions/node/*/bin",
    "/opt/homebrew/bin",
    "/usr/local/bin",
]

DEFAULTS: dict[str, Any] = {
    "qwen": {
        "base_url": "http://127.0.0.1:8080/v1",
        "health_url": "http://127.0.0.1:8080/health",
        "model": "qwen3-coder-next",
        "auto_start": False,
        "start_command": [],
        "start_timeout_seconds": 120,
        "route_max_tokens": 256,
        "agent_max_tokens": 4096,
        "agent_request_timeout_seconds": 600,
        "max_tool_turns": 24,
        "tool_output_chars": 24000,
        "file_limit_bytes": 1000000,
        "shell_timeout_seconds": 120,
        "shell_max_timeout_seconds": 600,
        "shell_network": False,
    },
    "claude": {
        "command": "claude",
        "model": "opus",
        "effort": "high",
        "permission_mode": "auto",
        "budget_cache": "~/.cache/ai-conductor/claude-budget.json",
        "budget_stale_seconds": 600,
        "request_timeout_seconds": 1800,
        "statusline_command": [],
    },
    "codex": {
        "command": "codex",
        "model": None,
        "sandbox": "workspace-write",
        "budget_timeout_seconds": 8,
        "request_timeout_seconds": 1800,
    },
    "routing": {
        "reserve_percent": {"claude": 20, "codex": 20},
        "allow_unknown_budget": True,
    },
    "web": {
        "host": "127.0.0.1",
        "port": 8765,
        "open_browser": True,
        "chat_store": "~/.local/state/ai-conductor/chats.json",
        "board_store": "~/.local/state/ai-conductor/board.jsonl",
    },
    "cli_search_paths": list(CLI_SEARCH_PATHS),
    "policy_file": str(ROOT / "POLICY.md"),
    "ledger": "~/.local/state/ai-conductor/runs.jsonl",
}


def _merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _merge(base[key], value)
        else:
            base[key] = value
    return base


def load_config(path: str | Path | None = None) -> dict[str, Any]:
    result = deepcopy(DEFAULTS)
    config_path = Path(path).expanduser() if path else ROOT / "config.json"
    if config_path.exists():
        with config_path.open(encoding="utf-8") as handle:
            overlay = json.load(handle)
        if not isinstance(overlay, dict):
            raise ValueError(f"configuration root must be an object: {config_path}")
        _merge(result, overlay)
    return result


def expanded_path(value: str) -> Path:
    return Path(value).expanduser().resolve()


def _executable(candidate: Path) -> str | None:
    return str(candidate) if candidate.is_file() and os.access(candidate, os.X_OK) else None


def resolve_command(config: dict[str, Any], provider: str) -> str | None:
    """Locate a provider CLI, or return None when it genuinely is not installed.

    `None` means "not found" and nothing else -- callers must not confuse it with
    "found but signed out", which is a different failure with a different fix.

    An explicit path in `<provider>.command` is authoritative: when the user names
    a file we never silently fall back to some other binary that happens to be on
    PATH, because running the wrong CLI is worse than reporting it missing.
    """
    command = str(config[provider]["command"])
    if os.sep in command or command.startswith("~"):
        return _executable(Path(command).expanduser())
    found = shutil.which(command)
    if found:
        return found
    for pattern in config.get("cli_search_paths") or ():
        expanded = os.path.expanduser(str(pattern))
        directories = sorted(glob(expanded)) if any(c in expanded for c in "*?[") else [expanded]
        for directory in directories:
            found = _executable(Path(directory) / command)
            if found:
                return found
    return None
