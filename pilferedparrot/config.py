from __future__ import annotations

import json
import ipaddress
import os
import re
import shutil
import tomllib
import urllib.error
import urllib.request
from copy import deepcopy
from glob import glob
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .model import provider_ids


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
    # User-created cards are merged here at runtime. Each one uses the generic
    # OpenAI-compatible coding-agent adapter and names an environment variable
    # for credentials, so secrets never enter the dashboard store.
    "provider_definitions": {},
    "qwen": {
        "base_url": "http://127.0.0.1:8080/v1",
        "health_url": "http://127.0.0.1:8080/health",
        "allow_remote_egress": False,
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
        "additional_dirs": [],
        "default_workspace": None,
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
    "claude": {
        "command": "claude",
        "model": None,
        # Show concrete models only. Rolling aliases make the picker longer and
        # can silently change the model behind an existing saved preference.
        "model_options": [
            {"value": "claude-fable-5-1", "label": "Claude Fable 5.1",
             "context_window": 200_000, "max_context_window": 200_000},
            {"value": "claude-fable-5", "label": "Claude Fable 5",
             "context_window": 200_000, "max_context_window": 200_000},
            {"value": "claude-opus-5", "label": "Claude Opus 5",
             "context_window": 200_000, "max_context_window": 200_000},
            {"value": "claude-opus-4-8", "label": "Claude Opus 4.8",
             "context_window": 200_000, "max_context_window": 200_000},
            {"value": "claude-sonnet-5", "label": "Claude Sonnet 5",
             "context_window": 200_000, "max_context_window": 200_000},
            {"value": "claude-sonnet-4-6", "label": "Claude Sonnet 4.6",
             "context_window": 200_000, "max_context_window": 200_000},
            {"value": "claude-haiku-4-5", "label": "Claude Haiku 4.5",
             "context_window": 200_000, "max_context_window": 200_000},
        ],
        "context_window_tokens": None,
        "context_window_percent": 100,
        "request_timeout_seconds": 1800,
    },
    "gemini": {
        "command": "gemini",
        "model": "auto",
        "model_options": [
            {"value": "auto", "label": "Gemini Auto"},
            {"value": "gemini-3-pro-preview", "label": "Gemini 3 Pro Preview"},
            {"value": "gemini-3-flash-preview", "label": "Gemini 3 Flash Preview"},
            {"value": "gemini-2.5-pro", "label": "Gemini 2.5 Pro"},
            {"value": "gemini-2.5-flash", "label": "Gemini 2.5 Flash"},
            {"value": "gemini-2.5-flash-lite", "label": "Gemini 2.5 Flash Lite"},
        ],
        "context_window_tokens": None,
        "context_window_percent": 100,
        "approval_mode": "auto_edit",
        "request_timeout_seconds": 1800,
    },
    "web": {
        "host": "127.0.0.1",
        "port": 8765,
        "open_browser": True,
        "default_provider": "codex",
        "chat_store": "~/.local/state/pilferedparrot/chats.json",
        "model_catalog_store": None,
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
    configured = config.get(provider, {}).get("model")
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
    for provider in provider_ids(config):
        selected = effective_model(config, provider)
        raw_hidden = config.get(provider, {}).get("hidden_models", ())
        hidden = {
            value.strip() for value in raw_hidden
            if isinstance(value, str) and value.strip()
        } if isinstance(raw_hidden, (list, tuple, set)) else set()
        labels: dict[str, str] = {}
        context_windows: dict[str, tuple[int, int]] = {}
        for value, label, window, maximum in _configured_model_entries(config, provider):
            if value in hidden:
                continue
            labels[value] = label
            if window is not None or maximum is not None:
                context_windows[value] = (
                    window if window is not None else maximum,
                    maximum if maximum is not None else window,
                )
        if selected and selected not in hidden:
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
                    if isinstance(value, str) and value.strip() and value.strip() not in hidden:
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
            # Structured local entries may intentionally correct a stale
            # provider cache. Reapply explicit labels and limits while legacy
            # string-only options retain the provider's nicer display name.
            for value, label, window, maximum in _configured_model_entries(config, provider):
                if value in hidden:
                    continue
                if label != value or value not in labels:
                    labels[value] = label
                if window is not None or maximum is not None:
                    context_windows[value] = (
                        window if window is not None else maximum,
                        maximum if maximum is not None else window,
                    )
        options = []
        for value, label in labels.items():
            option: dict[str, Any] = {"value": value, "label": label}
            if value in context_windows:
                window, maximum = context_windows[value]
                if window is not None:
                    option["context_window"] = window
                if maximum is not None:
                    option["max_context_window"] = maximum
            options.append(option)
        result[provider] = {"default": selected, "options": options}
    return result


def _configured_model_entries(
    config: dict[str, Any], provider: str,
) -> list[tuple[str, str, int | None, int | None]]:
    """Normalize legacy model strings and structured model option records.

    Invalid records are ignored so a single stale machine-local option cannot
    prevent the dashboard from loading its remaining provider models.
    """
    raw_options = config.get(provider, {}).get("model_options") or ()
    if not isinstance(raw_options, (list, tuple)):
        return []
    result: list[tuple[str, str, int | None, int | None]] = []
    for raw in raw_options:
        if isinstance(raw, str):
            value, label, raw_window, raw_maximum = raw.strip(), raw.strip(), None, None
        elif isinstance(raw, dict):
            candidate = raw.get("value") or raw.get("id") or raw.get("model")
            value = candidate.strip() if isinstance(candidate, str) else ""
            label = raw.get("label")
            label = label.strip() if isinstance(label, str) and label.strip() else value
            raw_window = raw.get("context_window")
            raw_maximum = raw.get("max_context_window")
        else:
            continue
        if not value:
            continue
        def positive_integer(raw_value: Any) -> int | None:
            if isinstance(raw_value, bool) or raw_value is None:
                return None
            try:
                number = int(raw_value)
            except (TypeError, ValueError):
                return None
            return number if number > 0 else None
        window = positive_integer(raw_window)
        maximum = positive_integer(raw_maximum)
        normalized = (value, label, window, maximum)
        for index, existing in enumerate(result):
            if existing[0] == value:
                result[index] = normalized
                break
        else:
            result.append(normalized)
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


def compatible_api_headers(config: dict[str, Any], provider: str) -> dict[str, str]:
    """Build generic API headers without ever serializing the credential."""
    provider_config = config.get(provider) or {}
    result = {"Content-Type": "application/json", "Accept": "application/json"}
    variable = str(provider_config.get("api_key_env") or "").strip()
    if variable:
        value = os.environ.get(variable)
        if not value:
            raise RuntimeError(f"Set ${variable} before using {provider}")
        result["Authorization"] = f"Bearer {value}"
    return result


def validate_compatible_base_url(value: Any) -> str:
    """Normalize an operator-supplied OpenAI-compatible API base URL."""
    if not isinstance(value, str):
        raise ValueError("base URL must be a string")
    result = value.strip().rstrip("/")
    try:
        parsed = urlparse(result)
    except ValueError as error:
        raise ValueError("base URL must be a valid http or https URL") from error
    if parsed.scheme not in {"http", "https"} or not parsed.hostname \
            or parsed.username is not None or parsed.password is not None \
            or parsed.query or parsed.fragment:
        raise ValueError("base URL must be an http or https URL without credentials or query data")
    return result


def _url_origin(value: str) -> tuple[str, str, int | None]:
    """Return a normalized HTTP origin suitable for redirect comparisons."""
    parsed = urlparse(value)
    port = parsed.port
    if port is None:
        port = 443 if parsed.scheme == "https" else 80 if parsed.scheme == "http" else None
    return parsed.scheme.lower(), (parsed.hostname or "").lower(), port


class _SameOriginRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Keep provider credentials on the endpoint the operator configured."""

    def redirect_request(
        self, request: urllib.request.Request, fp: Any, code: int, message: str,
        headers: Any, new_url: str,
    ) -> urllib.request.Request | None:
        if _url_origin(request.full_url) != _url_origin(new_url):
            raise urllib.error.HTTPError(
                new_url, code, "provider redirect to another origin was blocked", headers, fp,
            )
        return super().redirect_request(request, fp, code, message, headers, new_url)


def _compatible_opener() -> Any:
    return urllib.request.build_opener(_SameOriginRedirectHandler())


def open_compatible_url(
    config: dict[str, Any], provider: str,
    target: str | urllib.request.Request, *, timeout: float,
) -> Any:
    """Open a configured API URL, retaining Qwen's local-egress boundary."""
    provider_config = config.get(provider) or {}
    base_url = validate_compatible_base_url(provider_config.get("base_url"))
    parsed = urlparse(base_url)
    if provider_config.get("api_key_env") and parsed.scheme != "https" \
            and not qwen_endpoint_is_loopback(base_url):
        raise ValueError("API keys require HTTPS for non-loopback provider endpoints")
    if provider == "qwen":
        return open_qwen_url(config, target, timeout=timeout)
    target_url = target.full_url if isinstance(target, urllib.request.Request) else str(target)
    if _url_origin(target_url) != _url_origin(base_url):
        raise ValueError("provider request target does not match its configured origin")
    return _compatible_opener().open(target, timeout=timeout)


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
    configured_entries = _configured_model_entries(config, provider)
    selected = model or effective_model(config, provider)
    if selected:
        for value, _label, _window, maximum in configured_entries:
            if value == selected:
                if maximum is not None:
                    return maximum
                for entry_value, _entry_label, window, _entry_maximum in configured_entries:
                    if entry_value == selected and window is not None:
                        return window
                break
    elif configured_entries:
        # A provider-controlled default is still knowable when every configured
        # choice has the same maximum (for example Claude's numbered models).
        maxima = {
            maximum if maximum is not None else window
            for _value, _label, window, maximum in configured_entries
        }
        if None not in maxima and len(maxima) == 1:
            return maxima.pop()
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


def model_effective_context_window_percent(
    config: dict[str, Any], provider: str, model: str | None = None,
) -> int:
    """Return the provider's effective input share after response headroom.

    Codex publishes this alongside its local model catalog. Older catalogs and
    local OpenAI-compatible servers do not, so retaining the full configured
    window is the least surprising fallback.
    """
    if provider != "codex":
        return 100
    selected = model or effective_model(config, "codex")
    if not selected:
        return 100
    try:
        with expanded_path(config["codex"]["models_cache"]).open(encoding="utf-8") as handle:
            payload = json.load(handle)
    except (FileNotFoundError, OSError, ValueError, json.JSONDecodeError):
        return 100
    for raw in payload.get("models", ()) if isinstance(payload, dict) else ():
        if not isinstance(raw, dict) or (raw.get("slug") or raw.get("model")) != selected:
            continue
        try:
            percent = int(raw.get("effective_context_window_percent", 100))
        except (TypeError, ValueError):
            return 100
        return percent if 1 <= percent <= 100 else 100
    return 100


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
        if os.name == "posix" and config_path.is_file():
            config_path.chmod(0o600)
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
    definitions = result.get("provider_definitions")
    if not isinstance(definitions, dict):
        raise ValueError("provider_definitions must be an object")
    for provider, definition in definitions.items():
        if not isinstance(provider, str) or not re.fullmatch(
            r"[a-z0-9][a-z0-9_-]{0,63}", provider,
        ):
            raise ValueError(f"invalid custom provider ID: {provider!r}")
        if provider in provider_ids():
            raise ValueError(f"custom provider ID conflicts with built-in provider: {provider}")
        if not isinstance(definition, dict):
            raise ValueError(f"custom provider definition must be an object: {provider}")
    return result


def qwen_endpoint_is_loopback(url: str) -> bool:
    """Return whether a Qwen endpoint has an explicitly local hostname."""
    try:
        parsed = urlparse(str(url))
        hostname = parsed.hostname
        if (
            parsed.scheme not in {"http", "https"}
            or not hostname
            or parsed.username is not None
            or parsed.password is not None
        ):
            return False
        if hostname.lower() == "localhost":
            return True
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


def validate_qwen_endpoints(config: dict[str, Any]) -> None:
    """Reject remote Qwen endpoints unless remote egress was explicitly enabled."""
    qwen = config["qwen"]
    if qwen.get("allow_remote_egress") is True:
        return
    for key in ("base_url", "health_url"):
        endpoint = qwen.get(key, "")
        if not qwen_endpoint_is_loopback(endpoint):
            raise ValueError(
                f"qwen.{key} must use a loopback endpoint unless "
                "qwen.allow_remote_egress is enabled"
            )


class _LoopbackRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self, request: urllib.request.Request, fp: Any, code: int, message: str,
        headers: Any, new_url: str,
    ) -> urllib.request.Request | None:
        if not qwen_endpoint_is_loopback(new_url):
            raise urllib.error.HTTPError(
                new_url, code, "Qwen redirect to a remote endpoint was blocked", headers, fp,
            )
        return super().redirect_request(request, fp, code, message, headers, new_url)


def open_qwen_url(
    config: dict[str, Any], target: str | urllib.request.Request, *, timeout: float,
) -> Any:
    """Open a Qwen URL without proxies or remote redirects unless opted in."""
    validate_qwen_endpoints(config)
    if config["qwen"].get("allow_remote_egress") is True:
        return _compatible_opener().open(target, timeout=timeout)
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        _LoopbackRedirectHandler(),
    )
    return opener.open(target, timeout=timeout)


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


def provider_additional_dirs(config: dict[str, Any], provider: str) -> tuple[Path, ...]:
    """Return validated, de-duplicated extra workspace roots for a sandboxed provider.

    These roots exist so a task spanning several projects does not have to reach
    for the entire home directory. They are configuration-only for the same
    reason Codex's are: a prompt must not be able to widen its own filesystem
    authority.

    The home directory and its parents are rejected here even when
    `allow_home_workspace` is set. That setting is a decision about the
    *workspace*, and honouring it through this list would let an extra root
    silently reintroduce the home mask bypass that the workspace check exists
    to make deliberate.
    """
    raw_values = config[provider].get("additional_dirs", [])
    if not isinstance(raw_values, list) or not all(isinstance(value, str) for value in raw_values):
        raise ValueError(f"{provider}.additional_dirs must be an array of directory paths")
    home = Path.home().resolve()
    result: list[Path] = []
    for raw_value in raw_values:
        if not raw_value.strip():
            raise ValueError(f"{provider}.additional_dirs cannot contain an empty path")
        path = expanded_path(raw_value)
        if not path.is_dir():
            raise ValueError(f"{provider} additional directory does not exist: {path}")
        # Rebinding a writable ancestor of home after the sandbox's home mask
        # would expose the entire home tree just as surely as binding home
        # itself. Check this before the ordinary writability diagnostic so the
        # security rule remains explicit on machines with unusual ownership.
        if path in home.parents:
            raise ValueError(
                f"{provider}.additional_dirs cannot contain a parent of the home directory"
            )
        if path == home:
            raise ValueError(
                f"{provider}.additional_dirs cannot contain the home directory; "
                f"select it as the workspace with {provider}.allow_home_workspace instead"
            )
        if not os.access(path, os.W_OK | os.X_OK):
            raise ValueError(f"{provider} additional directory is not writable: {path}")
        if path not in result:
            result.append(path)
    return tuple(result)


def provider_default_workspace(config: dict[str, Any], provider: str) -> Path | None:
    """Return the configured fallback workspace for a provider, if it is usable.

    A launch must not be lost to an unusable fallback, so a missing or
    unwritable directory reads as "unset" rather than raising. The caller then
    asks the operator to choose instead of failing with no window at all.
    """
    raw_value = config.get(provider, {}).get("default_workspace")
    if not isinstance(raw_value, str) or not raw_value.strip():
        return None
    try:
        path = expanded_path(raw_value)
    except (OSError, RuntimeError):
        return None
    if not path.is_dir() or not os.access(path, os.W_OK | os.X_OK):
        return None
    return path


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
