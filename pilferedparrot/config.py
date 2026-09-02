from __future__ import annotations

import json
import os
import shutil
import tomllib
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
        "model_options": ["qwen3-coder-next"],
        "context_window_tokens": None,
        "context_window_percent": 100,
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
        "allow_home_workspace": False,
    },
    "codex": {
        "command": "codex",
        "model": None,
        "model_options": [],
        "config_path": "~/.codex/config.toml",
        "models_cache": "~/.codex/models_cache.json",
        "context_window_tokens": None,
        "context_window_percent": 100,
        "sandbox": "workspace-write",
        "additional_write_dirs": [],
        "budget_timeout_seconds": 8,
        "request_timeout_seconds": 1800,
    },
    "web": {
        "host": "127.0.0.1",
        "port": 8765,
        "open_browser": True,
        "default_provider": "codex",
        "chat_store": "~/.local/state/pilferedparrot/chats.json",
        "chat_model": "gpt-5.6-terra",
        "chat_reasoning_effort": "low",
        "chat_context_warning_chars": 80_000,
        "technical_context_warning_chars": 120_000,
    },
    "cli_search_paths": list(CLI_SEARCH_PATHS),
    "ledger": "~/.local/state/pilferedparrot/runs.jsonl",
}


def effective_model(config: dict[str, Any], provider: str) -> str | None:
    """Return the model a provider will actually receive by default."""
    configured = config[provider].get("model")
    if isinstance(configured, str) and configured.strip():
        return configured.strip()
    if provider != "codex":
        return None
    try:
        with expanded_path(config["codex"]["config_path"]).open("rb") as handle:
            value = tomllib.load(handle).get("model")
    except (FileNotFoundError, OSError, tomllib.TOMLDecodeError):
        return None
    return value.strip() if isinstance(value, str) and value.strip() else None


def model_catalog(config: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Describe selectable models without making a provider request.

    Codex maintains a provider-supplied local catalog. Local server model IDs
    remain configurable because Qwen exposes no equally reliable picker API.
    """
    result: dict[str, dict[str, Any]] = {}
    for provider in ("qwen", "codex"):
        selected = effective_model(config, provider)
        labels: dict[str, str] = {}
        context_windows: dict[str, tuple[int, int]] = {}
        for value in config[provider].get("model_options") or ():
            if isinstance(value, str) and value.strip():
                labels[value.strip()] = value.strip()
        if selected:
            labels.setdefault(selected, selected)
        if provider == "codex":
            try:
                with expanded_path(config["codex"]["models_cache"]).open(encoding="utf-8") as handle:
                    payload = json.load(handle)
                for raw in payload.get("models", ()) if isinstance(payload, dict) else ():
                    if not isinstance(raw, dict) or raw.get("visibility") == "hide":
                        continue
                    value = raw.get("slug") or raw.get("model")
                    label = raw.get("display_name") or raw.get("displayName") or value
                    if isinstance(value, str) and value.strip():
                        labels[value.strip()] = str(label or value).strip()
                        try:
                            window = int(raw.get("context_window") or raw.get("contextWindow"))
                        except (TypeError, ValueError):
                            window = 0
                        try:
                            maximum = int(
                                raw.get("max_context_window")
                                or raw.get("maxContextWindow")
                                or window
                            )
                        except (TypeError, ValueError):
                            maximum = window
                        if window > 0 or maximum > 0:
                            context_windows[value.strip()] = (
                                window if window > 0 else maximum,
                                maximum if maximum > 0 else window,
                            )
            except (FileNotFoundError, OSError, ValueError, json.JSONDecodeError):
                pass
        result[provider] = {
            "default": selected,
            "options": [
                {
                    "value": value, "label": label,
                    **({
                        "context_window": context_windows[value][0],
                        "max_context_window": context_windows[value][1],
                    } if value in context_windows else {}),
                }
                for value, label in labels.items()
            ],
        }
    return result


def context_window_percent(config: dict[str, Any], provider: str) -> int:
    """Return the configured share of a provider's maximum context window."""
    raw = config.get(provider, {}).get("context_window_percent", 100)
    if isinstance(raw, bool):
        raise ValueError(f"{provider}.context_window_percent must be between 1 and 100")
    try:
        value = int(raw)
    except (TypeError, ValueError) as error:
        raise ValueError(
            f"{provider}.context_window_percent must be between 1 and 100"
        ) from error
    if value < 1 or value > 100:
        raise ValueError(f"{provider}.context_window_percent must be between 1 and 100")
    return value


def model_max_context_window(
    config: dict[str, Any], provider: str, model: str | None = None,
) -> int | None:
    """Return the configured or provider-catalog maximum usable context window."""
    configured = config.get(provider, {}).get("context_window_tokens")
    if configured is not None:
        try:
            value = int(configured)
        except (TypeError, ValueError):
            value = 0
        if value > 0:
            return value
    if provider != "codex":
        return None
    selected = model or effective_model(config, "codex")
    if not selected:
        return None
    try:
        with expanded_path(config["codex"]["models_cache"]).open(encoding="utf-8") as handle:
            payload = json.load(handle)
    except (FileNotFoundError, OSError, ValueError, json.JSONDecodeError):
        return None
    for raw in payload.get("models", ()) if isinstance(payload, dict) else ():
        if not isinstance(raw, dict) or (raw.get("slug") or raw.get("model")) != selected:
            continue
        try:
            value = int(
                raw.get("max_context_window")
                or raw.get("maxContextWindow")
                or raw.get("context_window")
                or raw.get("contextWindow")
            )
        except (TypeError, ValueError):
            return None
        return value if value > 0 else None
    return None


def model_context_window(
    config: dict[str, Any], provider: str, model: str | None = None,
    percent: int | None = None,
) -> int | None:
    """Return the user-allowed share of a model's maximum usable context window."""
    maximum = model_max_context_window(config, provider, model)
    if maximum is None:
        return None
    allowed_percent = context_window_percent(config, provider) if percent is None else percent
    if isinstance(allowed_percent, bool) or not isinstance(allowed_percent, int) \
            or allowed_percent < 1 or allowed_percent > 100:
        raise ValueError("context window percent must be between 1 and 100")
    return max(1, maximum * allowed_percent // 100)


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
        web = overlay.get("web")
        if isinstance(web, dict):
            # Accept the pre-0.4 names long enough for existing machine-local
            # configuration to migrate without reviving coordinator behavior.
            for old, new in (
                ("coordinator_model", "chat_model"),
                ("coordinator_reasoning_effort", "chat_reasoning_effort"),
                ("coordinator_context_warning_chars", "chat_context_warning_chars"),
            ):
                if new not in web and old in web:
                    web[new] = web.pop(old)
        _merge(result, overlay)
    return result


def expanded_path(value: str) -> Path:
    return Path(value).expanduser().resolve()


def codex_additional_write_dirs(config: dict[str, Any]) -> tuple[Path, ...]:
    """Return validated, de-duplicated Codex `--add-dir` paths.

    Extra writable roots are deliberately configuration-only: a prompt cannot
    grant itself filesystem access. Missing roots fail before a provider turn so
    the model never receives a misleading workspace description.
    """
    raw_values = config["codex"].get("additional_write_dirs", [])
    if not isinstance(raw_values, list) or not all(isinstance(value, str) for value in raw_values):
        raise ValueError("codex.additional_write_dirs must be an array of directory paths")
    result: list[Path] = []
    for raw_value in raw_values:
        if not raw_value.strip():
            raise ValueError("codex.additional_write_dirs cannot contain an empty path")
        path = expanded_path(raw_value)
        if not path.is_dir():
            raise ValueError(f"Codex additional write directory does not exist: {path}")
        if not os.access(path, os.W_OK | os.X_OK):
            raise ValueError(f"Codex additional write directory is not writable: {path}")
        if path not in result:
            result.append(path)
    return tuple(result)


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
