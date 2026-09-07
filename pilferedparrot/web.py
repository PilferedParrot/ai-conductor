from __future__ import annotations

import argparse
import hmac
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import webbrowser
from copy import deepcopy
from dataclasses import asdict, dataclass, field
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from . import __version__
from .adapters import ProviderCapabilities, adapter_for
from .budgets import collect_budgets
from .config import (
    codex_additional_write_dirs, compatible_api_headers, context_window_percent,
    provider_default_workspace,
    effective_model, expanded_path,
    load_config, model_catalog, model_context_window, model_effective_context_window_percent,
    model_max_context_window, open_compatible_url, resolve_command,
    redact_configured_secrets,
    validate_compatible_base_url,
)
from .dispatch import RunCancelled, RunResult, capture_dispatch, _stop_process
from .processes import provider_argv
from .ledger import append_run
from .harness import metric, outcome_summary, render_handoff
from .web_harness import HarnessWorkflow
from .model import (
    Conversation, PROVIDER_CATALOG, PROVIDERS, ProviderBudget,
    provider_catalog, provider_ids,
)
from .qwen import AGENT_SYSTEM_PROMPT, TOOL_DEFINITIONS, ensure_qwen
from .response_identity import configured_identity
from .terminal import launch_terminal, terminal_argv as _terminal_argv
from .web_persistence import (
    DashboardModelStore, PersistentChatStore, chat_store_path,
    dashboard_capability_path, legacy_chat_store_path, load_dashboard_models,
    model_catalog_path, read_dashboard_capability, remove_dashboard_capability,
    write_dashboard_capability,
)
from .web_native import (
    CHROME_THEME_GALLERY_URL, NativeIntegration,
    browser_url as native_browser_url, chromium_browser as _chromium_browser,
    notify_window_closed as native_notify_window_closed,
    open_browser as open_native_browser,
    select_project_directory as native_select_project_directory,
    selected_chrome_theme as _selected_chrome_theme,
)
from . import web_server as _server


# Compatibility aliases: transport ownership lives in web_server, but callers
# (and established tests) patch these web.py names at composition time.
ASSET_ROOT = _server.ASSET_ROOT
RUNTIME_ROOT = _server.RUNTIME_ROOT
ASSET_NAMES = _server.ASSET_NAMES
CODE_BLOCK_LANGUAGES = frozenset({
    "bash", "console", "fish", "powershell", "shell", "sh", "terminal", "zsh",
})
ABSOLUTE_PATH = re.compile(r"(?<![\w.])(/[^\s`'\"<>|]+)")
API_GENERATION = 21
CHAT_MODEL_OPTIONS = ("gpt-5.6-terra", "gpt-5.6-luna")
MESSAGE_MAX_CHARS = 40_000
PROVIDER_LABELS = {item["id"]: item["label"] for item in PROVIDER_CATALOG}
PROVIDER_TEMPLATES: tuple[dict[str, str], ...] = (
    {
        "id": "xai", "label": "xAI / Grok", "adapter": "openai_compatible",
        "description": "Grok models through xAI's OpenAI-compatible API.",
        "base_url": "https://api.x.ai/v1", "api_key_env": "XAI_API_KEY",
        "model": "",
    },
    {
        "id": "openrouter", "label": "OpenRouter", "adapter": "openai_compatible",
        "description": "DeepSeek, GLM, and other model families. Choose a model with tool support; pricing and access vary by model.",
        "base_url": "https://openrouter.ai/api/v1",
        "api_key_env": "OPENROUTER_API_KEY", "model": "",
    },
    {
        "id": "google-ai-studio", "label": "Google AI Studio / Gemini API",
        "adapter": "openai_compatible",
        "description": "Gemini models with a Google AI Studio API key; separate from Gemini CLI sign-in.",
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
        "api_key_env": "GEMINI_API_KEY", "model": "",
    },
    {
        "id": "mistral", "label": "Mistral / Devstral", "adapter": "openai_compatible",
        "description": "Devstral coding models through Mistral's API. Choose a tool-capable model available to your account.",
        "base_url": "https://api.mistral.ai/v1",
        "api_key_env": "MISTRAL_API_KEY", "model": "",
    },
    {
        "id": "lmstudio", "label": "LM Studio", "adapter": "openai_compatible",
        "description": "Start LM Studio's local server and load a model with native tool support.",
        "base_url": "http://127.0.0.1:1234/v1", "api_key_env": "", "model": "",
    },
    {
        "id": "ollama", "label": "Ollama", "adapter": "openai_compatible",
        "description": "Any locally installed Ollama model.",
        "base_url": "http://127.0.0.1:11434/v1", "api_key_env": "", "model": "",
    },
    {
        "id": "custom", "label": "Any OpenAI-compatible provider",
        "adapter": "openai_compatible",
        "description": "Bring any service or local server that supports chat completions and tools.",
        "base_url": "", "api_key_env": "", "model": "",
    },
)


def _compatible_provider_config(definition: dict[str, Any]) -> dict[str, Any]:
    """Turn a persisted card definition into a complete agent configuration."""
    model = str(definition.get("model") or "").strip()
    result: dict[str, Any] = {
        "adapter": "openai_compatible",
        "base_url": str(definition.get("base_url") or "").rstrip("/"),
        "api_key_env": str(definition.get("api_key_env") or "").strip(),
        "model": model or None,
        "model_options": [model] if model else [],
        "context_window_tokens": definition.get("context_window_tokens"),
        "context_window_percent": 100,
        "agent_max_tokens": 4096,
        "agent_request_timeout_seconds": 600,
        "max_tool_turns": 24,
        "tool_output_chars": 24000,
        "file_limit_bytes": 1_000_000,
        "shell_timeout_seconds": 120,
        "shell_max_timeout_seconds": 600,
        "shell_network": False,
        "allow_home_workspace": False,
    }
    # Config-file definitions may intentionally tune the contained agent.
    for key in tuple(result):
        if key in definition and key not in {"adapter", "base_url", "api_key_env"}:
            result[key] = deepcopy(definition[key])
    return result
# Codex does not expose its built-in tool schemas before the first request.
# Include a conservative schema allowance until per-request telemetry replaces
# the entire prompt estimate with the provider's live input count.
CODEX_TOOL_DEFINITION_ESTIMATE_TOKENS = 2_000
WRITE_SCOPE_INTENT = re.compile(
    r"\b(?:write access|writable|work(?:ing)?\s+(?:on|in)|implement|build|modify|edit|fix|create)\b",
    re.IGNORECASE,
)


def _estimated_tokens(characters: int) -> int:
    """Use a deliberately simple, visible fallback when providers omit usage."""
    return max(0, (max(0, int(characters)) + 3) // 4)


def _context_usage(
    context_chars: int, fallback_chars: int, *, limit_tokens: int | None = None,
    max_tokens: int | None = None, allowance_percent: int | None = None,
    live_input_tokens: int | None = None, live_output_tokens: int | None = None,
    overhead_tokens: int = 0, output_reservation_tokens: int = 0,
    breakdown: dict[str, int] | None = None,
) -> dict[str, Any]:
    transcript = _estimated_tokens(context_chars)
    if breakdown is None:
        measured_input = (
            max(0, int(live_input_tokens)) if live_input_tokens is not None
            else transcript + max(0, int(overhead_tokens))
        )
        breakdown = {
            "live_input": measured_input,
            "latest_output": max(0, int(live_output_tokens or 0)),
            "output_reservation": max(0, int(output_reservation_tokens)),
        }
    else:
        breakdown = {
            str(key): max(0, int(value)) for key, value in breakdown.items()
        }
    # Output headroom limits how much of the configured window can be input.
    # It is capacity, not prompt content, so never report it as consumed.
    used = sum(
        value for key, value in breakdown.items() if key != "output_reservation"
    )
    known_limit = limit_tokens is not None and limit_tokens > 0
    limit = int(limit_tokens) if known_limit else max(1, _estimated_tokens(fallback_chars))
    maximum = int(max_tokens) if max_tokens is not None and max_tokens > 0 else None
    allowed_percent = allowance_percent if allowance_percent is not None else 100
    return {
        "used_tokens": used,
        "limit_tokens": limit,
        "max_tokens": maximum,
        "allowance_percent": allowed_percent,
        "percent": min(100, round(used * 100 / limit)),
        "estimated": True,
        "basis": "live_next_request",
        "transcript_tokens": transcript,
        "breakdown": breakdown,
    }


def _codex_instruction_tokens(config: dict[str, Any], model: str | None) -> int:
    """Estimate the catalog-provided system instructions for a fresh Codex thread."""
    selected = model or effective_model(config, "codex")
    if not selected:
        return 0
    try:
        with expanded_path(config["codex"]["models_cache"]).open(encoding="utf-8") as handle:
            payload = json.load(handle)
    except (FileNotFoundError, OSError, ValueError, json.JSONDecodeError):
        return 0
    for raw in payload.get("models", ()) if isinstance(payload, dict) else ():
        if not isinstance(raw, dict) or (raw.get("slug") or raw.get("model")) != selected:
            continue
        messages = raw.get("model_messages")
        template = messages.get("instructions_template") if isinstance(messages, dict) else ""
        return _estimated_tokens(len(template)) if isinstance(template, str) else 0
    return 0


def _workspace_context_tokens(cwd: Path) -> int:
    """Estimate repository instructions that the provider can inject."""
    root = cwd
    for candidate in (cwd, *cwd.parents):
        root = candidate
        if (candidate / ".git").exists():
            break
    characters = len(str(cwd)) + 512  # cwd, shell, date, and workspace metadata
    chain = [cwd]
    while chain[-1] != root and chain[-1].parent != chain[-1]:
        chain.append(chain[-1].parent)
    for current in reversed(chain):
        instructions = current / "AGENTS.md"
        try:
            characters += instructions.stat().st_size
        except OSError:
            pass
    return _estimated_tokens(characters)


def _initial_context_overhead_tokens(
    config: dict[str, Any], provider: str, model: str | None, cwd: Path,
) -> int:
    workspace = _workspace_context_tokens(cwd)
    if provider == "qwen" or config.get(provider, {}).get("adapter") == "openai_compatible":
        instructions = _estimated_tokens(len(AGENT_SYSTEM_PROMPT.format(cwd="")))
        tools = _estimated_tokens(len(json.dumps(TOOL_DEFINITIONS, separators=(",", ":"))))
        return instructions + tools + workspace
    if provider == "codex":
        return (
            _codex_instruction_tokens(config, model)
            + CODEX_TOOL_DEFINITION_ESTIMATE_TOKENS
            + workspace
        )
    # Native CLIs do not expose their injected prompts or tool schemas. Do not
    # label a Codex-shaped guess as Claude/Gemini context; retain only the
    # workspace portion we can actually observe (plus an explicit override).
    try:
        configured = max(0, int(config.get(provider, {}).get("context_overhead_tokens", 0)))
    except (TypeError, ValueError):
        configured = 0
    return configured + workspace


def _output_reservation_tokens(
    config: dict[str, Any], provider: str, model: str | None, limit_tokens: int | None,
) -> int:
    if provider == "qwen" or config.get(provider, {}).get("adapter") == "openai_compatible":
        try:
            return max(0, int(config[provider].get("agent_max_tokens", 0)))
        except (TypeError, ValueError):
            return 0
    if provider != "codex" or limit_tokens is None:
        return 0
    effective = model_effective_context_window_percent(config, provider, model)
    return max(0, int(limit_tokens) - int(limit_tokens) * effective // 100)


def _set_context_estimate_metadata(
    target: dict[str, Any], config: dict[str, Any], provider: str,
    model: str | None, cwd: Path, limit_tokens: int | None,
) -> None:
    target["context_overhead_tokens"] = _initial_context_overhead_tokens(
        config, provider, model, cwd,
    )
    target["output_reservation_tokens"] = _output_reservation_tokens(
        config, provider, model, limit_tokens,
    )


def _context_percent(value: Any) -> int:
    if isinstance(value, bool):
        raise ValueError("context window percent must be between 1 and 100")
    try:
        percent = int(value)
    except (TypeError, ValueError) as error:
        raise ValueError("context window percent must be between 1 and 100") from error
    if percent < 1 or percent > 100:
        raise ValueError("context window percent must be between 1 and 100")
    return percent


def _request_id(value: Any) -> str:
    if value is None:
        return uuid.uuid4().hex
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{8,128}", value):
        raise ValueError("request_id is invalid")
    return value


def _select_project_directory(initial: Any) -> Path | None:
    return native_select_project_directory(initial, normalize=_project_directory)


def _asset_fingerprint(root: Path) -> str:
    """Compatibility wrapper for the server-owned frontend fingerprint."""
    return _server._asset_fingerprint(root)


ASSET_VERSION = _server.ASSET_VERSION


def _runtime_fingerprint(root: Path) -> str:
    """Compatibility wrapper for the server-owned runtime fingerprint."""
    return _server._runtime_fingerprint(root)


RUNTIME_VERSION = _server.RUNTIME_VERSION
LEGACY_REPOSITORY_NAME = "ai-conductor"
RENAMED_REPOSITORY_NAME = "Pilfered Parrot"


def _renamed_repository_root(default_cwd: Path) -> Path | None:
    """Find this checkout when it is the known ai-conductor rename."""
    for candidate in (default_cwd.expanduser().resolve(), RUNTIME_ROOT.parent.resolve()):
        if (
            candidate.name == RENAMED_REPOSITORY_NAME
            and candidate.is_dir()
            and (candidate / "pilferedparrot").is_dir()
        ):
            return candidate
    return None


def _migrate_renamed_project_path(value: Any, renamed_root: Path | None) -> Path:
    """Map only the missing former checkout path to this renamed checkout."""
    path = Path(str(value)).expanduser().resolve()
    if renamed_root is None:
        return path
    legacy_root = renamed_root.parent / LEGACY_REPOSITORY_NAME
    if legacy_root.exists():
        return path
    if path == legacy_root:
        return renamed_root
    try:
        relative = path.relative_to(legacy_root)
    except ValueError:
        return path
    destination = renamed_root / relative
    return destination if destination.is_dir() else path


def _project_directory(value: Any) -> Path:
    path = Path(str(value)).expanduser().resolve()
    if not path.is_dir():
        raise ValueError(f"project folder does not exist: {path}")
    if not os.access(path, os.W_OK | os.X_OK):
        raise ValueError(f"project folder is not writable: {path}")
    return path


def _validate_provider_workspace(provider: str, cwd: Path, config: dict[str, Any]) -> None:
    sandboxed = (
        provider == "qwen"
        or config.get(provider, {}).get("adapter") == "openai_compatible"
    )
    if not sandboxed:
        return
    home = Path.home().resolve()
    if cwd in home.parents:
        raise ValueError(
            f"{provider} cannot use a parent of the home directory as its workspace; "
            "choose a narrower project folder"
        )
    if cwd == home and config[provider].get("allow_home_workspace") is not True:
        raise ValueError(
            f"{provider} cannot use the entire home directory as its workspace unless "
            f"{provider}.allow_home_workspace is explicitly enabled"
        )


def _repository_root(path: Path) -> Path:
    # A directory named directly by the operator is already the least-surprising
    # scope. Only walk upward for a mentioned file, where the checkout root is
    # more useful than granting one file's parent directory.
    if path.is_dir():
        return path
    current = path.parent
    for candidate in (current, *current.parents):
        if (candidate / ".git").exists():
            return candidate
    return current


def _outside_write_target(prompt: str, writable_roots: tuple[Path, ...]) -> Path | None:
    """Spot an unambiguous project mismatch before consuming a provider turn."""
    if not WRITE_SCOPE_INTENT.search(prompt):
        return None
    outside: list[Path] = []
    for match in ABSOLUTE_PATH.finditer(prompt):
        raw = match.group(1).rstrip(".,;:!?)]}")
        candidate = Path(raw).expanduser()
        if not candidate.exists():
            continue
        target = _repository_root(candidate.resolve())
        if any(target == root or root in target.parents for root in writable_roots):
            continue
        if target not in outside:
            outside.append(target)
    return outside[0] if len(outside) == 1 else None


def _fenced_code_block_details(content: str) -> list[tuple[str | None, str]]:
    """Extract complete top-level fences using the browser's bounded subset."""
    lines = str(content or "").replace("\r", "").split("\n")
    blocks: list[tuple[str | None, str]] = []
    index = 0
    while index < len(lines):
        opening = re.fullmatch(r" {0,3}```(?:([A-Za-z0-9_+-]+))?[ \t]*", lines[index])
        if opening is None:
            index += 1
            continue
        closing = index + 1
        while closing < len(lines) and re.fullmatch(
            r" {0,3}```[ \t]*", lines[closing],
        ) is None:
            closing += 1
        if closing >= len(lines):
            break
        language = opening.group(1)
        blocks.append((language.lower() if language else None,
                       "\n".join(lines[index + 1:closing]).strip()))
        index = closing + 1
    return blocks


def _fenced_code_blocks(content: str) -> list[str]:
    return [code for _language, code in _fenced_code_block_details(content)]


def _loopback_host(value: str) -> bool:
    return _server.loopback_host(value)


def _web_authority(host: str, port: int) -> str:
    return _server.web_authority(host, port)


_IPv6ThreadingHTTPServer = _server.IPv6ThreadingHTTPServer


def _pilferedparrot_status(url: str) -> str:
    return _server.pilferedparrot_status(
        url, opener=urlopen, api_generation=API_GENERATION,
        asset_version=ASSET_VERSION, runtime_version=RUNTIME_VERSION,
    )


def _browser_url(url: str, capability: str) -> str:
    return native_browser_url(
        url, capability, api_generation=API_GENERATION,
        asset_version=ASSET_VERSION, runtime_version=RUNTIME_VERSION,
    )


def _notify_window_closed(browser_url: str) -> bool:
    return native_notify_window_closed(
        browser_url, opener=urlopen, is_loopback=_loopback_host,
    )


def _pilferedparrot_is_running(url: str) -> bool:
    # Kept as a small compatibility wrapper for callers and older integrations.
    return _pilferedparrot_status(url) in {"compatible", "stale"}


def _terminate_stale_pilferedparrot(url: str, port: int) -> None:
    if sys.platform == "win32":
        raise RuntimeError(
            "An older PilferedParrot is still running. Close its console window "
            "and start the new version again."
        )
    _server.terminate_stale_pilferedparrot(
        url, port, status=_pilferedparrot_status, which=shutil.which,
        runner=subprocess.run, monotonic=time.monotonic, sleeper=time.sleep,
    )


@dataclass
class ActiveRun:
    cancel_event: threading.Event = field(default_factory=threading.Event)
    thread: threading.Thread | None = None
    last_checkpoint: float = 0.0


@dataclass
class ActiveProviderLogin:
    process: subprocess.Popen[bytes]


class ChatStore(PersistentChatStore):
    """Compatibility facade retaining the historical web.ChatStore API."""

    def __init__(
        self, path: Path, *, chat_warning_chars: int = 80_000,
        technical_warning_chars: int = 120_000, legacy_path: Path | None = None,
        chat_model: str = "gpt-5.6-terra",
    ):
        super().__init__(
            path, chat_warning_chars=chat_warning_chars,
            technical_warning_chars=technical_warning_chars,
            legacy_path=legacy_path, chat_model=chat_model,
            context_usage=_context_usage, chat_model_options=CHAT_MODEL_OPTIONS,
        )


_dashboard_capability_path = dashboard_capability_path
_pilferedparrot_dashboard_capability = read_dashboard_capability


class PilferedParrotApp(HarnessWorkflow):
    def __init__(self, config: dict[str, Any], default_cwd: Path):
        self.config = config
        self.default_cwd = default_cwd
        self.renamed_repository_root = _renamed_repository_root(default_cwd)
        self.chat_context_warning_chars = max(
            10_000, int(config["web"].get("chat_context_warning_chars", 80_000)),
        )
        self.technical_context_warning_chars = max(
            10_000, int(config["web"].get("technical_context_warning_chars", 120_000)),
        )
        store_path = chat_store_path(config)
        self.model_catalog_path = model_catalog_path(config)
        raw_definitions = config.get("provider_definitions")
        self._base_provider_definitions = deepcopy(raw_definitions) \
            if isinstance(raw_definitions, dict) else {}
        self._base_hidden_providers = list(config.get("_hidden_providers") or [])
        for provider, definition in self._base_provider_definitions.items():
            if isinstance(provider, str) and isinstance(definition, dict):
                config[provider] = {
                    **_compatible_provider_config(definition),
                    **deepcopy(config.get(provider) or {}),
                }
        initial_provider_ids = provider_ids(config, include_hidden=True)
        self._base_model_options = {
            provider: deepcopy(config[provider].get("model_options") or [])
            for provider in initial_provider_ids
        }
        self._base_hidden_models = {
            provider: list(config[provider].get("hidden_models") or [])
            if isinstance(config[provider].get("hidden_models") or [], list) else []
            for provider in initial_provider_ids
        }
        self._model_catalog_store = DashboardModelStore(
            self.model_catalog_path, provider_ids(config, include_hidden=True),
        )
        self.model_catalog_lock = self._model_catalog_store.lock
        self.dashboard_models = self._model_catalog_store.data
        self._apply_dashboard_models()
        active_providers = self._provider_ids()
        self.default_provider = str(config["web"].get("default_provider", "codex"))
        if self.default_provider not in active_providers:
            self.default_provider = "codex" if "codex" in active_providers else active_providers[0]
        self.store = ChatStore(
            store_path,
            chat_warning_chars=self.chat_context_warning_chars,
            technical_warning_chars=self.technical_context_warning_chars,
            chat_model=str(config["web"].get("chat_model") or "gpt-5.6-terra").strip(),
            legacy_path=legacy_chat_store_path(config),
        )
        self.capabilities_lock = threading.RLock()
        self.capabilities: dict[str, dict[str, str]] = {}
        self.dashboard_capability = self.issue_capability(
            "dashboard", window_id="main", provider=self.default_provider,
        )
        self.native = NativeIntegration(self.revoke_capability)
        self.provider_login_lock = threading.RLock()
        self.provider_logins: dict[str, ActiveProviderLogin] = {}
        self.budget_condition = threading.Condition(threading.RLock())
        self.budget_snapshot: dict[str, ProviderBudget] = {}
        self.budget_refreshed_at = 0.0
        self.budget_refreshing = False
        self.budget_error: Exception | None = None
        self.runs_lock = threading.RLock()
        self.runs: dict[str, ActiveRun] = {}
        self.chat_run: ActiveRun | None = None
        self.chat_model = str(
            config["web"].get("chat_model") or "gpt-5.6-terra"
        ).strip()
        if self.chat_model not in CHAT_MODEL_OPTIONS:
            self.chat_model = CHAT_MODEL_OPTIONS[0]
        self.chat_reasoning_effort = str(
            config["web"].get("chat_reasoning_effort") or "low"
        ).strip().lower()
        if self.chat_reasoning_effort not in {
            "none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra",
        }:
            raise ValueError("web.chat_reasoning_effort is invalid")
        self.store.chat_model = self.chat_model
        with self.store.lock:
            self.store._normalize_chat_thread(self.store.data["chat"])
            for archived in self.store.data["chat_history"]:
                self.store._normalize_chat_thread(archived, archived=True)
            for chat_thread in (
                self.store.data["chat"], *self.store.data["chat_history"],
            ):
                chat_thread["cwd"] = str(chat_thread.get("cwd") or self.default_cwd)
                provider = str(chat_thread.get("provider") or "codex")
                if provider not in self._provider_ids(include_hidden=True):
                    provider = self.default_provider
                    chat_thread["provider"] = provider
                percent = _context_percent(chat_thread.get(
                    "context_window_percent", context_window_percent(self.config, provider),
                ))
                model = chat_thread.get("model") or self._preferred_chat_model(provider)
                chat_thread["model"] = model
                try:
                    chat_thread["reasoning_effort"] = self._selected_reasoning_effort(
                        provider, model, chat_thread.get("reasoning_effort"),
                    )
                except ValueError:
                    chat_thread["reasoning_effort"] = None
                chat_max = model_max_context_window(self.config, provider, model)
                chat_limit = model_context_window(self.config, provider, model, percent)
                if chat_limit is not None:
                    chat_thread["context_limit_tokens"] = chat_limit
                    chat_thread["context_max_tokens"] = chat_max
                    chat_thread["context_window_percent"] = percent
                else:
                    chat_thread.pop("context_limit_tokens", None)
                    chat_thread.pop("context_max_tokens", None)
                _set_context_estimate_metadata(
                    chat_thread, self.config, provider, model,
                    Path(chat_thread["cwd"]), chat_limit,
                )
            for technical_chat in self.store.data["chats"]:
                stored_cwd = technical_chat.get("cwd")
                if stored_cwd:
                    resolved_cwd = Path(str(stored_cwd)).expanduser().resolve()
                    migrated_cwd = _migrate_renamed_project_path(
                        stored_cwd, self.renamed_repository_root,
                    )
                    if migrated_cwd != resolved_cwd:
                        technical_chat["cwd"] = str(migrated_cwd)
                provider = technical_chat.get("provider") \
                    or technical_chat.get("requested_provider") or self.default_provider
                if provider not in self._provider_ids(include_hidden=True):
                    provider = technical_chat.get("requested_provider")
                    if provider not in self._provider_ids(include_hidden=True):
                        provider = self.default_provider
                model = technical_chat.get("model") or technical_chat.get("requested_model") \
                    or effective_model(self.config, provider)
                percent = _context_percent(technical_chat.get(
                    "context_window_percent", context_window_percent(self.config, provider),
                ))
                maximum = model_max_context_window(self.config, provider, model)
                limit = model_context_window(self.config, provider, model, percent)
                if limit is not None:
                    technical_chat["context_limit_tokens"] = limit
                    technical_chat["context_max_tokens"] = maximum
                    technical_chat["context_window_percent"] = percent
                else:
                    technical_chat.pop("context_limit_tokens", None)
                    technical_chat.pop("context_max_tokens", None)
                _set_context_estimate_metadata(
                    technical_chat, self.config, provider, model,
                    Path(technical_chat.get("cwd") or self.default_cwd), limit,
                )
            self.store.save()

    @property
    def chat_window_capability(self) -> str | None:
        """Compatibility view of the native Chat-window capability."""
        return self.native.chat_window_capability

    @property
    def provider_windows(self) -> dict[str, dict[str, Any]]:
        """Compatibility view of active native provider windows."""
        return self.native.provider_windows

    def recover_interrupted(self) -> int:
        recovered = 0
        with self.store.lock:
            for chat in self.store.data["chats"]:
                chat_recovered = False
                for message in chat.get("messages", []):
                    if not message.get("pending"):
                        continue
                    message.update({
                        "content": "PilferedParrot restarted before this response finished. You can retry.",
                        "error": True,
                        "interrupted": True,
                    })
                    self._harness_complete(message)
                    message.pop("pending", None)
                    message.pop("cancel_requested", None)
                    recovered += 1
                    chat_recovered = True
                if chat_recovered:
                    chat["updated_at"] = int(time.time())
            chat_thread = self.store.data["chat"]
            for message in chat_thread.get("messages", []):
                if not message.get("pending"):
                    continue
                message.update({
                    "content": "PilferedParrot restarted before Chat finished. You can retry.",
                    "error": True,
                    "interrupted": True,
                })
                message.pop("pending", None)
                message.pop("cancel_requested", None)
                recovered += 1
                chat_thread["updated_at"] = int(time.time())
            for work in self.store.data["chats"]:
                for task in work.get("harness_tasks", []):
                    if task.get("status") == "running":
                        task["status"] = "failed"
                        if task.get("attempts"):
                            attempt = task["attempts"][-1]
                            attempt["status"] = "failed"
                            attempt["elapsed_seconds"] = metric(None, unit="seconds")
                            attempt["failure_reason"] = "application restarted before completion was recorded"
                        task["summary"] = outcome_summary(task.get("attempts", []))
                        recovered += 1
            if recovered:
                self.store.save()
        return recovered

    def budgets(self) -> dict[str, ProviderBudget]:
        # All dashboard windows ask for the same provider data. Coalesce requests
        # arriving together and briefly reuse the result instead of spawning a
        # duplicate set of CLI/network probes for every window.
        with self.budget_condition:
            age = time.monotonic() - self.budget_refreshed_at
            if self.budget_snapshot and age < 2:
                return dict(self.budget_snapshot)
            if self.budget_refreshing:
                self.budget_condition.wait_for(lambda: not self.budget_refreshing)
                if not self.budget_snapshot and self.budget_error is not None:
                    raise RuntimeError(f"provider status refresh failed: {self.budget_error}")
                return dict(self.budget_snapshot)
            self.budget_refreshing = True
        try:
            snapshot = collect_budgets(self.config)
        except Exception as error:
            with self.budget_condition:
                self.budget_error = error
            raise
        finally:
            with self.budget_condition:
                if "snapshot" in locals():
                    self.budget_snapshot = snapshot
                    self.budget_refreshed_at = time.monotonic()
                    self.budget_error = None
                self.budget_refreshing = False
                self.budget_condition.notify_all()
        return dict(snapshot)

    def _invalidate_budgets(self) -> None:
        with self.budget_condition:
            self.budget_snapshot = {}
            self.budget_refreshed_at = 0.0
            self.budget_error = None

    def issue_capability(
        self, scope: str, *, window_id: str = "", provider: str = "",
        history_id: str = "", model: str = "",
    ) -> str:
        if scope not in {"dashboard", "chat"}:
            raise ValueError("invalid window capability scope")
        if scope == "dashboard":
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", window_id):
                raise ValueError("window id is invalid")
            if history_id and not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", history_id):
                raise ValueError("history id is invalid")
            if provider not in self._provider_ids(include_hidden=True):
                raise ValueError("unknown provider")
        elif provider and provider not in self._provider_ids(include_hidden=True):
            raise ValueError("unknown provider")
        if scope == "chat" and provider:
            self._require_chat_support(provider)
        token = secrets.token_urlsafe(32)
        with self.capabilities_lock:
            self.capabilities[token] = {
                "scope": scope, "window_id": window_id, "provider": provider,
                "history_id": history_id or window_id, "model": model,
            }
        return token

    def capability_scope(self, supplied: str) -> str | None:
        context = self.capability_context(supplied)
        return context.get("scope") if context else None

    def capability_context(self, supplied: str) -> dict[str, str] | None:
        if not supplied:
            return None
        with self.capabilities_lock:
            # Compare secrets in constant time even though dictionary membership
            # would be simpler. The registry is intentionally tiny (one token per
            # native window).
            for token, context in self.capabilities.items():
                if hmac.compare_digest(supplied, token):
                    return dict(context)
        return None

    def revoke_capability(self, token: str | None) -> None:
        if token:
            with self.capabilities_lock:
                self.capabilities.pop(token, None)

    def persist_dashboard_capability(self, origin: str) -> None:
        write_dashboard_capability(self.config, origin, self.dashboard_capability)

    def remove_dashboard_capability(self) -> None:
        remove_dashboard_capability(self.config, self.dashboard_capability)

    def _provider_ids(self, *, include_hidden: bool = False) -> tuple[str, ...]:
        return provider_ids(self.config, include_hidden=include_hidden)

    def _provider_catalog(self, *, include_hidden: bool = False) \
            -> tuple[dict[str, Any], ...]:
        result = []
        for item in provider_catalog(self.config, include_hidden=include_hidden):
            public = dict(item)
            try:
                public["capabilities"] = asdict(
                    adapter_for(str(public["id"]), self.config).capabilities,
                )
            except ValueError:
                public["capabilities"] = asdict(ProviderCapabilities(run=False, cancel=False))
            result.append(public)
        return tuple(result)

    def _preferred_work_model(self, provider: str) -> str | None:
        model = self.store.data["preferences"]["work_models"].get(provider)
        return self._normalize_model(model)

    def _preferred_work_context_percent(self, provider: str) -> int:
        stored = self.store.data["preferences"]["work_context_window_percent"].get(provider)
        return _context_percent(
            stored if stored is not None else context_window_percent(self.config, provider)
        )

    def _chat_model_options(self, provider: str) -> list[dict[str, Any]]:
        options = list(model_catalog(self.config).get(provider, {}).get("options", []))
        if provider == "codex":
            seen = {str(option.get("value")) for option in options}
            for value in (*CHAT_MODEL_OPTIONS, "gpt-5.6-sol"):
                if value not in seen:
                    options.append({"value": value, "label": value})
        return options

    def _preferred_chat_model(self, provider: str) -> str | None:
        if provider == "codex":
            stored = self.store.data["preferences"].get("chat_model")
            if isinstance(stored, str) and stored.strip():
                return self._normalize_model(stored)
        work_model = self.store.data["preferences"]["work_models"].get(provider)
        return self._normalize_model(work_model) or effective_model(self.config, provider) \
            or next((
                str(option["value"]) for option in self._chat_model_options(provider)
                if option.get("value")
            ), None)

    def _preferred_chat_context_percent(self, provider: str) -> int:
        stored = self.store.data["preferences"].get("chat_context_window_percent")
        return _context_percent(
            stored if stored is not None else context_window_percent(self.config, provider)
        )

    def set_provider_preferences(
        self, payload: dict[str, Any], *, window_provider: str | None = None,
    ) -> dict[str, Any]:
        provider = str(window_provider or payload.get("provider") or "")
        if provider not in self._provider_ids(include_hidden=bool(window_provider)):
            raise ValueError("unknown provider")
        if "model" not in payload:
            raise ValueError("model is required")
        model = self._normalize_model(payload.get("model"))
        if model is None:
            raise ValueError("choose a model before saving the preference")
        with self.store.lock:
            self.store.data["preferences"]["work_models"][provider] = model
            self.store.save()
            return self.store.preferences_public()

    def set_notification_preferences(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Save the browser notification decision without changing provider settings."""
        return self.store.set_notification_permission(payload.get("decision"))

    def _load_dashboard_models(self) -> dict[str, Any]:
        loaded = load_dashboard_models(
            self.model_catalog_path, self._provider_ids(include_hidden=True),
        )
        self._model_catalog_store.data = loaded
        return loaded

    def _apply_dashboard_models(self) -> None:
        definitions = deepcopy(self._base_provider_definitions)
        definitions.update(deepcopy(self.dashboard_models.get("provider_cards") or {}))
        self.config["provider_definitions"] = definitions
        hidden_providers = [
            *self._base_hidden_providers,
            *(self.dashboard_models.get("hidden_providers") or []),
        ]
        # There must always be one recoverable card to keep the current dashboard usable.
        all_ids = (*PROVIDERS, *definitions)
        if all(provider in hidden_providers for provider in all_ids):
            hidden_providers = [provider for provider in hidden_providers if provider != "codex"]
        self.config["_hidden_providers"] = list(dict.fromkeys(hidden_providers))
        for provider, definition in definitions.items():
            if not isinstance(definition, dict):
                continue
            existing = self.config.get(provider)
            self.config[provider] = {
                **_compatible_provider_config(definition),
                **(deepcopy(existing) if isinstance(existing, dict) else {}),
            }
        for provider in self._provider_ids(include_hidden=True):
            overrides = self.dashboard_models["providers"].get(provider) or {}
            base_options = self._base_model_options.get(provider)
            if base_options is None:
                base_options = deepcopy(self.config[provider].get("model_options") or [])
                self._base_model_options[provider] = deepcopy(base_options)
            base_hidden = self._base_hidden_models.setdefault(provider, [])
            self.config[provider]["model_options"] = [
                *deepcopy(base_options),
                *deepcopy(overrides.get("models") or []),
            ]
            self.config[provider]["hidden_models"] = [
                *base_hidden,
                *(overrides.get("hidden") or []),
            ]

    def _save_dashboard_models(self) -> None:
        self._model_catalog_store.save(self.dashboard_models)

    @staticmethod
    def _model_text(value: Any, field: str, *, required: bool = True) -> str:
        if value is None and not required:
            return ""
        if not isinstance(value, str):
            raise ValueError(f"{field} must be a string")
        result = value.strip()
        if (required and not result) or len(result) > 128 \
                or any(ord(char) < 32 for char in result):
            raise ValueError(f"{field} must be valid text of at most 128 characters")
        return result

    def provider_templates(self) -> list[dict[str, Any]]:
        templates = [deepcopy(item) for item in PROVIDER_TEMPLATES]
        hidden = set(self.config.get("_hidden_providers") or [])
        known = {item["id"]: item for item in self._provider_catalog(include_hidden=True)}
        for provider in sorted(hidden):
            info = known.get(provider)
            if not info:
                continue
            provider_config = self.config.get(provider) or {}
            templates.insert(0, {
                "id": f"restore:{provider}",
                "label": f"Restore {info['label']}",
                "description": "Restore the previous card and all of its saved settings.",
                "adapter": str(provider_config.get("adapter") or "native"),
                "base_url": str(provider_config.get("base_url") or ""),
                "api_key_env": str(provider_config.get("api_key_env") or ""),
                "model": str(provider_config.get("model") or ""),
                "restorable": True,
            })
        return templates

    @staticmethod
    def _provider_slug(label: str) -> str:
        slug = re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-")
        return (slug or "provider")[:48]

    @staticmethod
    def _discover_provider_models(
        base_url: str, api_key_env: str,
    ) -> list[str]:
        """Use the standard model list when the user leaves Model ID blank."""
        temporary_config = {
            "discovery": {
                "base_url": base_url, "api_key_env": api_key_env,
                "adapter": "openai_compatible",
            },
        }
        try:
            request = Request(
                base_url.rstrip("/") + "/models",
                headers=compatible_api_headers(temporary_config, "discovery"),
            )
            with open_compatible_url(
                temporary_config, "discovery", request, timeout=5,
            ) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except Exception as error:
            raise ValueError(
                "Could not discover models automatically: "
                f"{redact_configured_secrets(temporary_config, error)}. Enter a Model ID to add "
                "the card without discovery."
            ) from None
        raw_models = payload.get("data") if isinstance(payload, dict) else None
        models = []
        for item in raw_models if isinstance(raw_models, list) else []:
            value = item.get("id") if isinstance(item, dict) else None
            if isinstance(value, str) and value.strip() and len(value.strip()) <= 128:
                models.append(value.strip())
        if not models:
            raise ValueError(
                "The endpoint returned no models. Enter a Model ID to add the card anyway."
            )
        return list(dict.fromkeys(models))[:200]

    def provider_update(self, provider: str) -> dict[str, Any]:
        """Check the selected CLI without running a model or installing software."""
        from .provider_updates import check_provider_update

        if provider not in self._provider_ids(include_hidden=True):
            raise ValueError("unknown provider")
        return check_provider_update(self.config, provider)

    def poll_provider_models(self, provider: str) -> dict[str, Any]:
        """Refresh a provider's model choices without spending a model turn."""
        if provider not in self._provider_ids():
            raise ValueError("unknown provider")
        catalog = model_catalog(self.config).get(provider, {"default": None, "options": []})
        try:
            options = adapter_for(provider, self.config).models()
        except Exception as error:
            # Keep a configured selection usable during a transient outage,
            # while making the failed poll visible to the caller.
            return {
                "provider": provider, **deepcopy(catalog),
                "polled_at": int(time.time()), "source": "configured_fallback",
                "warning": redact_configured_secrets(self.config, error),
            }
        default = catalog.get("default")
        if default and not any(option.get("value") == default for option in options):
            known = {
                str(option.get("value")): option
                for option in catalog.get("options", []) if isinstance(option, dict)
            }
            options.insert(0, deepcopy(known.get(str(default)) or {
                "value": str(default), "label": str(default),
            }))
        source = "provider" if provider == "qwen" \
            or self.config.get(provider, {}).get("adapter") == "openai_compatible" \
            else "native_catalog"
        return {
            "provider": provider, "default": default, "options": options,
            "polled_at": int(time.time()), "source": source,
        }

    def add_provider(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Persist or restore one provider card as an atomic operation."""
        template_id = self._model_text(payload.get("template"), "provider template")
        if template_id.startswith("restore:"):
            provider = template_id.partition(":")[2]
            if provider not in self._provider_ids(include_hidden=True):
                raise ValueError("provider is no longer available to restore")
            with self.model_catalog_lock:
                previous = deepcopy(self.dashboard_models)
                self.dashboard_models["hidden_providers"] = [
                    value for value in self.dashboard_models.get("hidden_providers", [])
                    if value != provider
                ]
                try:
                    self._save_dashboard_models()
                    self._apply_dashboard_models()
                except Exception:
                    self.dashboard_models = previous
                    self._apply_dashboard_models()
                    raise
            self._invalidate_budgets()
            return next(item for item in self._provider_catalog() if item["id"] == provider)

        template = next(
            (item for item in PROVIDER_TEMPLATES if item["id"] == template_id), None,
        )
        if template is None:
            raise ValueError("unknown provider template")
        label = self._model_text(payload.get("label") or template["label"], "display name")
        base_url = validate_compatible_base_url(payload.get("base_url") or template["base_url"])
        api_key_env = self._model_text(
            payload.get("api_key_env") or template["api_key_env"],
            "API-key environment variable", required=False,
        )
        if api_key_env and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", api_key_env):
            raise ValueError("API-key environment variable must be a valid variable name")
        parsed_base = urlparse(base_url)
        if api_key_env and parsed_base.scheme != "https" \
                and not _loopback_host(parsed_base.hostname or ""):
            raise ValueError("API keys require HTTPS for non-loopback provider endpoints")
        model = self._model_text(payload.get("model"), "model ID", required=False)
        discovered_models = [model] if model else self._discover_provider_models(
            base_url, api_key_env,
        )
        model = model or discovered_models[0]
        known_ids = set(self._provider_ids(include_hidden=True))
        base_slug = self._provider_slug(label)
        provider = base_slug
        suffix = 2
        while provider in known_ids:
            provider = f"{base_slug[:55]}-{suffix}"
            suffix += 1
        definition = {
            "label": label,
            "description": str(template["description"]),
            "adapter": "openai_compatible",
            "base_url": base_url,
            "api_key_env": api_key_env,
            "model": model,
            "model_options": discovered_models,
        }
        with self.model_catalog_lock:
            previous = deepcopy(self.dashboard_models)
            self.dashboard_models.setdefault("provider_cards", {})[provider] = definition
            self.dashboard_models.setdefault("providers", {})[provider] = {
                "models": [], "hidden": [],
            }
            try:
                self._save_dashboard_models()
                self._apply_dashboard_models()
            except Exception:
                self.dashboard_models = previous
                self._apply_dashboard_models()
                raise
        self._invalidate_budgets()
        return next(item for item in self._provider_catalog() if item["id"] == provider)

    def remove_provider(self, payload: dict[str, Any]) -> None:
        provider = self._model_text(payload.get("provider"), "provider")
        if provider not in self._provider_ids():
            raise ValueError("unknown provider")
        if len(self._provider_ids()) <= 1:
            raise ValueError("Add another provider before removing the last card")
        with self.model_catalog_lock:
            previous = deepcopy(self.dashboard_models)
            hidden = self.dashboard_models.setdefault("hidden_providers", [])
            if provider not in hidden:
                hidden.append(provider)
            try:
                self._save_dashboard_models()
                self._apply_dashboard_models()
                if self.default_provider == provider:
                    self.default_provider = self._provider_ids()[0]
            except Exception:
                self.dashboard_models = previous
                self._apply_dashboard_models()
                raise
        self._invalidate_budgets()

    def state(
        self, scope: str = "dashboard", *, window_id: str = "main",
        window_provider: str | None = None,
    ) -> dict[str, Any]:
        catalog = model_catalog(self.config)
        codex_models = {
            str(option.get("value"))
            for option in catalog.get("codex", {}).get("options", [])
            if option.get("value")
        }
        codex_models.update((*CHAT_MODEL_OPTIONS, "gpt-5.6-sol"))
        def catalog_maximum(provider: str, option: dict[str, Any]) -> int | None:
            raw = option.get("max_context_window") or option.get("context_window")
            try:
                value = int(raw)
            except (TypeError, ValueError):
                value = 0
            return value if value > 0 else model_max_context_window(
                self.config, provider, str(option["value"]),
            )
        model_context_windows = {
            provider: {
                str(option["value"]): catalog_maximum(provider, option)
                for option in catalog.get(provider, {}).get("options", [])
                if option.get("value")
            }
            for provider in self._provider_ids()
        }
        codex_windows = model_context_windows.setdefault("codex", {})
        for model in codex_models:
            if model not in codex_windows:
                codex_windows[model] = model_max_context_window(self.config, "codex", model)
        shared = {
            "api_generation": API_GENERATION,
            "asset_version": ASSET_VERSION,
            "runtime_version": RUNTIME_VERSION,
            "model_context_windows": model_context_windows,
        }
        if scope == "chat":
            chat = self.store.chat_public()
            provider = window_provider or str(chat.get("provider") or self.default_provider)
            if provider not in self._provider_ids(include_hidden=True):
                raise ValueError("unknown provider")
            options = self._chat_model_options(provider)
            return {
                **shared,
                "chat": chat,
                "chat_history": self.store.chat_history_public(provider),
                "chat_provider": provider,
                "chat_model": chat.get("model") or self._preferred_chat_model(provider),
                "chat_model_choices": [
                    str(option["value"]) for option in options if option.get("value")
                ],
                "model_catalog": {provider: {**deepcopy(catalog.get(provider, {})),
                                               "options": deepcopy(options)}},
                "providers": [deepcopy(item) for item in self._provider_catalog()],
                "preferences": self.store.preferences_public(),
            }
        if scope != "dashboard":
            raise ValueError("invalid state scope")
        provider = window_provider or self.default_provider
        if provider not in self._provider_ids(include_hidden=True):
            raise ValueError("unknown provider")
        return {
            **shared,
            "chats": self.store.list_public(window_id, provider),
            "window_id": window_id,
            "window_provider": provider,
            "default_cwd": str(self.default_cwd),
            "default_provider": self.default_provider,
            "models": {
                name: catalog[name]["default"] or "provider default" for name in self._provider_ids()
            },
            "providers": [deepcopy(item) for item in self._provider_catalog()],
            "provider_templates": self.provider_templates(),
            "harness": self.harness_metadata(),
            "model_catalog": deepcopy(catalog),
            "preferences": self.store.preferences_public(),
        }

    def create_chat(
        self, payload: dict[str, Any], *, window_id: str = "main",
        window_provider: str | None = None,
    ) -> dict[str, Any]:
        provider = str(window_provider or payload.get("provider") or self.default_provider)
        if provider not in self._provider_ids(include_hidden=bool(window_provider)):
            raise ValueError(f"provider must be one of: {', '.join(self._provider_ids())}")
        latest_defaults = self.store.latest_work_defaults(provider, window_id)
        latest_model = latest_defaults[0] if latest_defaults else None
        latest_reasoning = latest_defaults[1] if latest_defaults else None
        explicit_model = bool(payload.get("model"))
        requested_model = self._normalize_model(payload.get("model")) \
            if explicit_model else self._normalize_model(latest_model) \
            or self._preferred_work_model(provider)
        cwd = _project_directory(_migrate_renamed_project_path(
            payload.get("cwd") or self.default_cwd, self.renamed_repository_root,
        ))
        _validate_provider_workspace(provider, cwd, self.config)
        model = requested_model or effective_model(self.config, provider)
        explicit_reasoning = "reasoning_effort" in payload
        requested_reasoning = payload.get("reasoning_effort") if explicit_reasoning else latest_reasoning
        try:
            reasoning_effort = self._selected_reasoning_effort(
                provider, model, requested_reasoning,
            )
        except ValueError:
            # Model capabilities can change between sessions, even when the
            # model ID stays the same. Discard stale inherited preferences;
            # explicit invalid choices remain errors for callers to correct.
            if not explicit_reasoning:
                reasoning_effort = None
            else:
                raise
        percent = self._preferred_work_context_percent(provider)
        context_max = model_max_context_window(self.config, provider, model)
        context_limit = model_context_window(self.config, provider, model, percent)
        overhead = _initial_context_overhead_tokens(self.config, provider, model, cwd)
        reservation = _output_reservation_tokens(
            self.config, provider, model, context_limit,
        )
        with self.store.lock:
            if requested_model:
                self.store.data["preferences"]["work_models"][provider] = requested_model
            return self.store.create(
                cwd, provider, requested_model, context_limit, context_max, percent,
                overhead, reservation, window_id, reasoning_effort,
            )

    def _owned_chat(self, chat_id: str, window_id: str | None) -> dict[str, Any]:
        chat = self.store.get(chat_id)
        if window_id is not None and chat.get("window_id", "main") != window_id:
            raise KeyError(chat_id)
        return chat

    def chat_state(self, chat_id: str, *, window_id: str | None = None) -> dict[str, Any]:
        with self.store.lock:
            return self.store.public(self._owned_chat(chat_id, window_id))

    def current_chat_state(self) -> dict[str, Any]:
        """Expose the persisted Chat view through the server-facing app boundary."""
        return self.store.chat_public()

    def set_context_window(
        self, chat_id: str, payload: dict[str, Any], *, window_id: str | None = None,
    ) -> dict[str, Any]:
        percent = _context_percent(payload.get("percent"))
        with self.runs_lock:
            if chat_id in self.runs:
                raise ValueError("stop the response before changing the context allowance")
            with self.store.lock:
                chat = self._owned_chat(chat_id, window_id)
                provider = chat.get("requested_provider") or self.default_provider
                model = chat.get("requested_model") or chat.get("model") \
                    or effective_model(self.config, provider)
                maximum = model_max_context_window(self.config, provider, model)
                limit = model_context_window(self.config, provider, model, percent)
                if maximum is None or limit is None:
                    raise ValueError("the selected model does not publish a context maximum")
                chat["context_window_percent"] = percent
                chat["context_max_tokens"] = maximum
                chat["context_limit_tokens"] = limit
                _set_context_estimate_metadata(
                    chat, self.config, provider, model, Path(chat["cwd"]), limit,
                )
                self.store.data["preferences"]["work_context_window_percent"][provider] = percent
                self.store.save()
                return self.store.public(chat)

    def set_chat_context_window(
        self, payload: dict[str, Any], *, provider: str | None = None,
    ) -> dict[str, Any]:
        percent = _context_percent(payload.get("percent"))
        with self.runs_lock:
            if self.chat_run is not None:
                raise ValueError("stop Chat before changing the context allowance")
            with self.store.lock:
                chat_thread = self.store.data["chat"]
                selected_provider = str(provider or chat_thread.get("provider")
                                        or self.default_provider)
                if selected_provider not in self._provider_ids(include_hidden=True) \
                        or chat_thread.get("provider") != selected_provider:
                    raise ValueError("Chat belongs to another provider window")
                model = chat_thread.get("model") or self._preferred_chat_model(selected_provider)
                maximum = model_max_context_window(self.config, selected_provider, model)
                limit = model_context_window(
                    self.config, selected_provider, model, percent,
                )
                if maximum is None or limit is None:
                    raise ValueError("the selected model does not publish a context maximum")
                chat_thread["context_window_percent"] = percent
                chat_thread["context_max_tokens"] = maximum
                chat_thread["context_limit_tokens"] = limit
                _set_context_estimate_metadata(
                    chat_thread, self.config, selected_provider, model,
                    Path(chat_thread.get("cwd") or self.default_cwd), limit,
                )
                self.store.data["preferences"]["chat_context_window_percent"] = percent
                self.store.save()
                return self.store.chat_public()

    def set_chat_model(
        self, payload: dict[str, Any], *, provider: str | None = None,
    ) -> dict[str, Any]:
        model = self._normalize_model(payload.get("model"))
        if model is None:
            raise ValueError("choose a model")
        with self.runs_lock:
            if self.chat_run is not None:
                raise ValueError("stop Chat before changing the chat model")
            with self.store.lock:
                chat_thread = self.store.data["chat"]
                selected_provider = str(provider or chat_thread.get("provider")
                                        or self.default_provider)
                if selected_provider not in self._provider_ids(include_hidden=True) \
                        or chat_thread.get("provider") != selected_provider:
                    raise ValueError("Chat belongs to another provider window")
                if chat_thread.get("messages"):
                    raise ValueError("start a new chat before changing the chat model")
                percent = _context_percent(chat_thread.get(
                    "context_window_percent", context_window_percent(
                        self.config, selected_provider,
                    ),
                ))
                maximum = model_max_context_window(self.config, selected_provider, model)
                limit = model_context_window(
                    self.config, selected_provider, model, percent,
                )
                chat_thread["model"] = model
                try:
                    chat_thread["reasoning_effort"] = self._selected_reasoning_effort(
                        selected_provider, model, chat_thread.get("reasoning_effort"),
                    )
                except ValueError:
                    chat_thread["reasoning_effort"] = None
                chat_thread.pop("provider_session_id", None)
                chat_thread.pop("live_context_usage", None)
                chat_thread.pop("last_turn_usage", None)
                if limit is not None:
                    chat_thread["context_max_tokens"] = maximum
                    chat_thread["context_limit_tokens"] = limit
                    chat_thread["context_window_percent"] = percent
                else:
                    chat_thread.pop("context_max_tokens", None)
                    chat_thread.pop("context_limit_tokens", None)
                _set_context_estimate_metadata(
                    chat_thread, self.config, selected_provider, model,
                    Path(chat_thread.get("cwd") or self.default_cwd), limit,
                )
                self.store.data["preferences"]["work_models"][selected_provider] = model
                if selected_provider == "codex":
                    self.store.data["preferences"]["chat_model"] = model
                self.store.save()
                return self.store.chat_public()

    @staticmethod
    def _normalize_model(value: Any) -> str | None:
        if value is None or value == "":
            return None
        if not isinstance(value, str):
            raise ValueError("model must be a string")
        model = value.strip()
        if not model or len(model) > 128 or any(ord(char) < 32 for char in model):
            raise ValueError("model must be a valid model ID")
        return model

    def _selected_reasoning_effort(
        self, provider: str, model: str | None, value: Any,
    ) -> str | None:
        """Validate an explicit reasoning choice against local model metadata."""
        if value is None:
            return None
        if not isinstance(value, str):
            raise ValueError("reasoning effort must be a string or null")
        effort = value.strip().lower()
        if not effort:
            raise ValueError("reasoning effort must be a string or null")
        if provider != "codex":
            raise ValueError("reasoning effort is only supported for Codex")
        option = next((item for item in model_catalog(self.config).get("codex", {}).get("options", [])
                       if item.get("value") == model), None)
        # A manually entered Codex model has no cache record. Match catalog's
        # conservative metadata fallback so the picker and request validator
        # do not disagree.
        supported = option.get("reasoning_efforts") if option else ["low", "medium", "high"]
        if not isinstance(supported, list) or effort not in supported:
            raise ValueError("reasoning effort is not supported by the selected model")
        return effort

    def set_reasoning_effort(
        self, chat_id: str, payload: dict[str, Any], *, window_id: str | None = None,
    ) -> dict[str, Any]:
        if "reasoning_effort" not in payload:
            raise ValueError("reasoning_effort is required")
        with self.runs_lock:
            if chat_id in self.runs:
                raise ValueError("stop the response before changing reasoning effort")
            with self.store.lock:
                chat = self._owned_chat(chat_id, window_id)
                provider = str(chat.get("requested_provider") or self.default_provider)
                requested_model = self._normalize_model(payload.get("model")) \
                    if "model" in payload else chat.get("requested_model")
                model = requested_model or effective_model(self.config, provider)
                effort = self._selected_reasoning_effort(
                    provider, model, payload.get("reasoning_effort"),
                )
                chat["requested_model"] = requested_model
                chat["reasoning_effort"] = effort
                self.store.mark_used(chat)
                self.store.save()
                return self.store.public(chat)

    def activate_chat(
        self, chat_id: str, *, window_id: str | None = None,
    ) -> dict[str, Any]:
        """Persist an explicitly opened work session as the latest selection."""
        with self.store.lock:
            chat = self._owned_chat(chat_id, window_id)
            self.store.mark_used(chat)
            self.store.save()
            return self.store.public(chat)

    def set_chat_reasoning_effort(
        self, payload: dict[str, Any], *, provider: str | None = None,
    ) -> dict[str, Any]:
        if "reasoning_effort" not in payload:
            raise ValueError("reasoning_effort is required")
        with self.runs_lock:
            if self.chat_run is not None:
                raise ValueError("stop Chat before changing reasoning effort")
            with self.store.lock:
                chat = self.store.data["chat"]
                selected_provider = str(provider or chat.get("provider") or self.default_provider)
                if selected_provider not in self._provider_ids(include_hidden=True) \
                        or chat.get("provider") != selected_provider:
                    raise ValueError("Chat belongs to another provider window")
                model = self._normalize_model(payload.get("model")) \
                    if "model" in payload else chat.get("model")
                if model is None:
                    raise ValueError("choose a model before selecting reasoning effort")
                effort = self._selected_reasoning_effort(
                    selected_provider, model, payload.get("reasoning_effort"),
                )
                if model != chat.get("model"):
                    if chat.get("messages"):
                        raise ValueError("start a new chat before changing the chat model")
                    chat["model"] = model
                    chat.pop("provider_session_id", None)
                chat["reasoning_effort"] = effort
                self.store.save()
                return self.store.chat_public()

    def delete_chat(self, chat_id: str, *, window_id: str | None = None) -> None:
        with self.runs_lock:
            if chat_id in self.runs:
                raise ValueError("cancel the running response before deleting this work session")
            with self.store.lock:
                chat = self._owned_chat(chat_id, window_id)
                if any(task.get("status") == "running" for task in chat.get("harness_tasks", [])):
                    raise ValueError("cancel the running harness package before deleting its parent")
            self.store.delete(chat_id)

    def send_message(
        self, chat_id: str, payload: dict[str, Any], *,
        window_id: str | None = None, window_provider: str | None = None,
        _harness_attempt: tuple[str, str, str] | None = None,
    ) -> dict[str, Any]:
        prompt = str(payload.get("content") or "").strip()
        if not prompt:
            raise ValueError("message cannot be empty")
        if len(prompt) > MESSAGE_MAX_CHARS:
            raise ValueError(f"message cannot exceed {MESSAGE_MAX_CHARS:,} characters")
        request_id = _request_id(payload.get("request_id"))
        active = ActiveRun()
        with self.runs_lock:
            if chat_id in self.runs:
                raise ValueError("this work session is already running")
            with self.store.lock:
                chat = self._owned_chat(chat_id, window_id)
                if chat.get("harness_parent") and _harness_attempt is None:
                    raise ValueError("review or retry this bounded package from its parent session")
                if _harness_attempt is None and any(
                    task.get("status") == "running" for task in chat.get("harness_tasks", [])
                ):
                    raise ValueError("wait for the active harness package")
                if any(message.get("pending") for message in chat["messages"]):
                    raise ValueError("this work session is already running")
                provider = str(window_provider or payload.get("provider")
                               or chat.get("requested_provider") or self.default_provider)
                if provider not in self._provider_ids(include_hidden=bool(window_provider)):
                    raise ValueError(f"provider must be one of: {', '.join(self._provider_ids())}")
                model_value = payload.get("model") if "model" in payload \
                    else chat.get("requested_model")
                requested_model = self._normalize_model(model_value)
                selected_model = requested_model or effective_model(self.config, provider)
                if _harness_attempt is not None and provider == "claude":
                    # Validated by the selected harness policy. Ordinary Claude
                    # composer defaults retain their existing behavior.
                    reasoning_effort = payload.get("reasoning_effort")
                elif "reasoning_effort" in payload:
                    reasoning_effort = self._selected_reasoning_effort(
                        provider, selected_model, payload.get("reasoning_effort"),
                    )
                else:
                    reasoning_effort = chat.get("reasoning_effort")
                    try:
                        reasoning_effort = self._selected_reasoning_effort(
                            provider, selected_model, reasoning_effort,
                        )
                    except ValueError:
                        reasoning_effort = None
                if requested_model and not chat.get("harness_parent"):
                    self.store.data["preferences"]["work_models"][provider] = requested_model
                same_session = provider == chat.get("provider") \
                    and selected_model == chat.get("model")
                chat["requested_provider"] = provider
                chat["requested_model"] = requested_model
                chat["reasoning_effort"] = reasoning_effort
                if not same_session:
                    chat.pop("live_context_usage", None)
                    chat.pop("last_turn_usage", None)
                if not chat["messages"]:
                    requested_cwd = _project_directory(_migrate_renamed_project_path(
                        payload.get("cwd") or chat["cwd"], self.renamed_repository_root,
                    ))
                    _validate_provider_workspace(provider, requested_cwd, self.config)
                    writable_roots = (requested_cwd,)
                    if provider == "codex":
                        writable_roots += codex_additional_write_dirs(self.config)
                    outside_target = _outside_write_target(prompt, writable_roots)
                    if outside_target is not None:
                        raise ValueError(
                            f"project mismatch: this task appears to modify {outside_target}, "
                            f"but the writable project is {requested_cwd}; choose that Project folder"
                            + (" or add it to codex.additional_write_dirs" if provider == "codex" else "")
                        )
                    chat["cwd"] = str(requested_cwd)
                percent = _context_percent(chat.get(
                    "context_window_percent", context_window_percent(self.config, provider),
                ))
                context_max = model_max_context_window(
                    self.config, provider, selected_model,
                )
                context_limit = model_context_window(
                    self.config, provider, selected_model, percent,
                )
                if context_limit is not None:
                    chat["context_max_tokens"] = context_max
                    chat["context_limit_tokens"] = context_limit
                    chat["context_window_percent"] = percent
                else:
                    chat.pop("context_max_tokens", None)
                    chat.pop("context_limit_tokens", None)
                _set_context_estimate_metadata(
                    chat, self.config, provider, selected_model,
                    Path(chat["cwd"]), context_limit,
                )
                now = int(time.time())
                if not chat["messages"]:
                    chat["title"] = " ".join(prompt.split())[:54]
                chat["messages"].append({
                    "id": request_id,
                    "role": "user",
                    "content": prompt,
                    "created_at": now,
                })
                pending = {
                    "id": uuid.uuid4().hex,
                    "role": "assistant",
                    "content": "",
                    "created_at": now,
                    "pending": True,
                    "run_id": uuid.uuid4().hex,
                    "requested_provider": provider,
                    "requested_model": requested_model,
                    "provider": provider,
                    "reasoning_effort": reasoning_effort,
                }
                if _harness_attempt is not None:
                    pending["harness_reference"] = list(_harness_attempt)
                    _, task, attempt = self._harness_reference(_harness_attempt)
                    attempt["message_id"] = pending["id"]
                    attempt["run_id"] = pending["run_id"]
                    pending["harness_contract"] = deepcopy(task["contract"])
                    pending["harness_route"] = deepcopy(task["route"])
                chat["messages"].append(pending)
                chat["updated_at"] = now
                self.store.mark_used(chat)
                self.store.save()
                public = self.store.public(chat)
            self.runs[chat_id] = active
        thread = threading.Thread(
            target=self._run_message,
            args=(chat_id, pending["id"], prompt, active),
            name=f"pilferedparrot-{chat_id[:8]}",
            daemon=True,
        )
        active.thread = thread
        try:
            thread.start()
        except Exception as error:
            with self.runs_lock:
                self.runs.pop(chat_id, None)
            with self.store.lock:
                chat = self.store.get(chat_id)
                failed = self._message(chat, pending["id"])
                failed.update({
                    "content": "PilferedParrot error: "
                    f"{redact_configured_secrets(self.config, error)}",
                    "error": True,
                })
                failed.pop("pending", None)
                self.store.save()
            raise
        return public

    def _run_message(
        self, chat_id: str, pending_id: str, prompt: str, active: ActiveRun,
    ) -> None:
        provider: str | None = None
        budgets: dict[str, ProviderBudget] = {}
        conversation: Conversation | None = None
        result: RunResult | None = None
        started = time.monotonic()
        try:
            with self.store.lock:
                chat = self.store.get(chat_id)
                provider = chat["requested_provider"]
                requested_model = chat.get("requested_model")
                reasoning_effort = chat.get("reasoning_effort")
                current_provider = chat.get("provider")
                current_model = chat.get("model")
                if current_provider and current_model is None \
                        and current_provider in self._provider_ids(include_hidden=True):
                    current_model = effective_model(self.config, current_provider)
                session_id = chat.get("provider_session_id")
                provider_messages = list(chat.get("provider_messages") or [])
                cwd = Path(chat["cwd"])

            if provider == "qwen":
                ensure_qwen(self.config, cancel_event=active.cancel_event)
            selected_model = requested_model or effective_model(self.config, provider)
            same_session = provider == current_provider and selected_model == current_model
            percent = _context_percent(chat.get(
                "context_window_percent", context_window_percent(self.config, provider),
            ))
            context_max = model_max_context_window(self.config, provider, selected_model)
            context_limit = model_context_window(
                self.config, provider, selected_model, percent,
            )
            run_config = deepcopy(self.config)
            if selected_model:
                run_config[provider]["model"] = selected_model
            if provider == "codex" and reasoning_effort is not None:
                run_config["codex"]["reasoning_effort"] = reasoning_effort
            if provider == "codex" and context_limit is not None:
                run_config["codex"]["context_window_limit_tokens"] = context_limit
            pending_snapshot = self._message(chat, pending_id)
            if pending_snapshot.get("harness_reference"):
                run_config["_harness"] = {"bounded": True}
                run_config[provider]["reasoning_effort"] = reasoning_effort
                if provider == "codex" and not pending_snapshot["harness_contract"]["write_scope"]:
                    run_config["codex"]["sandbox"] = "read-only"
                prompt = render_handoff(pending_snapshot["harness_contract"], pending_snapshot["harness_route"])
            conversation = Conversation(
                provider=provider,
                response_identity=configured_identity(run_config, provider),
                provider_session_id=session_id if same_session else None,
                messages=provider_messages
                if (provider == "qwen" or
                    self.config.get(provider, {}).get("adapter") == "openai_compatible")
                and same_session else [],
            )

            with self.store.lock:
                chat = self.store.get(chat_id)
                pending = self._message(chat, pending_id)
                pending["model"] = selected_model
                pending["reasoning_effort"] = reasoning_effort
                if not same_session:
                    chat.pop("last_turn_usage", None)
                if context_limit is not None:
                    chat["context_limit_tokens"] = context_limit
                    chat["context_max_tokens"] = context_max
                    chat["context_window_percent"] = percent
                else:
                    chat.pop("context_limit_tokens", None)
                    chat.pop("context_max_tokens", None)
                self.store.save()

            def report_progress(kind: str, text: str) -> None:
                rendered = str(text).strip()
                if not rendered:
                    return
                rendered = rendered[:4_000]
                with self.store.lock:
                    pending = self._message(self.store.get(chat_id), pending_id)
                    activity = pending.setdefault("activity", [])
                    activity.append({
                        "kind": kind,
                        "content": rendered,
                        "created_at": int(time.time()),
                    })
                    if len(activity) > 100:
                        del activity[:-100]
                    now = time.monotonic()
                    if now - active.last_checkpoint >= 0.5:
                        self.store.save()
                        active.last_checkpoint = now

            setattr(active.cancel_event, "_pilferedparrot_progress", report_progress)
            result = capture_dispatch(
                provider, prompt, cwd, conversation, run_config, active.cancel_event,
            )
            if result.exit_code == 0 and result.session_id:
                conversation.provider_session_id = result.session_id
            content = result.text or result.error or f"{provider.title()} exited without a response."
            if result.exit_code and result.error and result.text:
                content += f"\n\n{result.error}"
            if result.exit_code:
                content = redact_configured_secrets(self.config, content)
            with self.store.lock:
                chat = self.store.get(chat_id)
                pending = self._message(chat, pending_id)
                pending.update({
                    "content": content,
                    "provider": provider,
                    "model": selected_model,
                    "reasoning_effort": reasoning_effort,
                    "exit_code": result.exit_code,
                    "error": bool(result.exit_code),
                })
                if result.exit_code == 0:
                    chat["provider"] = provider
                    chat["model"] = selected_model
                    chat["provider_session_id"] = conversation.provider_session_id
                    chat["provider_messages"] = conversation.messages
                    if result.input_tokens is not None and result.output_tokens is not None:
                        chat["last_turn_usage"] = {
                            "input_tokens": result.input_tokens,
                            "output_tokens": result.output_tokens,
                        }
                    if result.live_input_tokens is not None:
                        chat["live_context_usage"] = {
                            "input_tokens": result.live_input_tokens,
                            "output_tokens": result.live_output_tokens or 0,
                        }
                        if result.live_context_window_tokens is not None \
                                and chat.get("context_limit_tokens"):
                            chat["output_reservation_tokens"] = max(
                                0,
                                int(chat["context_limit_tokens"])
                                - result.live_context_window_tokens,
                            )
            try:
                append_run(
                    self.config["ledger"], provider=provider, prompt=prompt, cwd=cwd,
                    session_id=conversation.provider_session_id, budgets=budgets,
                    exit_code=result.exit_code, run_id=pending["run_id"],
                    chat_id=chat_id, message_id=pending_id,
                    harness_reference=pending.get("harness_reference"),
                )
            except OSError as error:
                print(f"[web] could not append run ledger: {error}")
        except RunCancelled:
            with self.store.lock:
                chat = self.store.get(chat_id)
                pending = self._message(chat, pending_id)
                pending.update({"content": "Cancelled.", "cancelled": True, "exit_code": 130})
        except Exception as exc:
            with self.store.lock:
                chat = self.store.get(chat_id)
                pending = self._message(chat, pending_id)
                pending.update({
                    "content": "PilferedParrot error: "
                    f"{redact_configured_secrets(self.config, exc)}",
                    "provider": provider,
                    "error": True,
                    "exit_code": 1,
                })
        finally:
            with self.runs_lock:
                with self.store.lock:
                    chat = self.store.get(chat_id)
                    pending = self._message(chat, pending_id)
                    if conversation is not None:
                        pending["response_identity"] = deepcopy(conversation.response_identity)
                    self._harness_complete(pending, result, time.monotonic() - started)
                    pending.pop("pending", None)
                    pending.pop("cancel_requested", None)
                    chat["updated_at"] = int(time.time())
                    self.store.save()
                    if self.runs.get(chat_id) is active:
                        self.runs.pop(chat_id, None)

    @staticmethod
    def _message(chat: dict[str, Any], message_id: str) -> dict[str, Any]:
        for message in chat["messages"]:
            if message.get("id") == message_id:
                return message
        raise KeyError(message_id)

    def cancel_message(
        self, chat_id: str, *, window_id: str | None = None,
    ) -> dict[str, Any]:
        with self.runs_lock:
            active = self.runs.get(chat_id)
            with self.store.lock:
                chat = self._owned_chat(chat_id, window_id)
                pending = next((item for item in chat["messages"] if item.get("pending")), None)
                if pending is None:
                    raise ValueError("this work session is not running")
                pending["cancel_requested"] = True
                self.store.save()
                public = self.store.public(chat)
            if active is not None:
                active.cancel_event.set()
            else:
                with self.store.lock:
                    chat = self.store.get(chat_id)
                    pending = next(item for item in chat["messages"] if item.get("pending"))
                    pending.update({"content": "Cancelled.", "cancelled": True, "exit_code": 130})
                    pending.pop("pending", None)
                    pending.pop("cancel_requested", None)
                    chat["updated_at"] = int(time.time())
                    self.store.save()
                    public = self.store.public(chat)
        return public

    def _require_chat_support(self, provider: str) -> None:
        if not adapter_for(provider, self.config).capabilities.chat:
            raise ValueError("This provider supports Work only; read-only Chat is not yet supported.")

    def send_chat_message(
        self, payload: dict[str, Any], *, provider: str | None = None,
    ) -> dict[str, Any]:
        content = str(payload.get("content") or "").strip()
        if not content:
            raise ValueError("message cannot be empty")
        if len(content) > MESSAGE_MAX_CHARS:
            raise ValueError(f"message cannot exceed {MESSAGE_MAX_CHARS:,} characters")
        request_id = _request_id(payload.get("request_id"))
        active = ActiveRun()
        with self.runs_lock:
            if self.chat_run is not None:
                raise ValueError("Chat is already responding")
            with self.store.lock:
                chat_thread = self.store.data["chat"]
                selected_provider = str(provider or payload.get("provider")
                                        or chat_thread.get("provider")
                                        or self.default_provider)
                if selected_provider not in self._provider_ids(include_hidden=True):
                    raise ValueError("unknown provider")
                self._require_chat_support(selected_provider)
                if chat_thread.get("provider") != selected_provider:
                    if provider is not None or chat_thread.get("messages"):
                        raise ValueError("Chat belongs to another provider window")
                    chat_thread["provider"] = selected_provider
                    chat_thread["model"] = None
                    chat_thread["provider_session_id"] = None
                    chat_thread["provider_messages"] = []
                if any(message.get("pending") for message in chat_thread["messages"]):
                    raise ValueError("Chat is already responding")
                requested_model = self._normalize_model(
                    payload.get("model") if "model" in payload else chat_thread.get("model")
                )
                if requested_model is None:
                    raise ValueError("choose a model before sending a Chat message")
                if "reasoning_effort" in payload:
                    reasoning_effort = self._selected_reasoning_effort(
                        selected_provider, requested_model, payload.get("reasoning_effort"),
                    )
                else:
                    reasoning_effort = chat_thread.get("reasoning_effort")
                    try:
                        reasoning_effort = self._selected_reasoning_effort(
                            selected_provider, requested_model, reasoning_effort,
                        )
                    except ValueError:
                        reasoning_effort = None
                if requested_model != chat_thread.get("model"):
                    if chat_thread.get("messages"):
                        raise ValueError("start a new chat before changing the chat model")
                    chat_thread["model"] = requested_model
                    # A Codex resume token is model-specific. Never carry it
                    # across a model picker change.
                    chat_thread.pop("provider_session_id", None)
                    chat_thread.pop("live_context_usage", None)
                    chat_thread.pop("last_turn_usage", None)
                chat_thread["reasoning_effort"] = reasoning_effort
                self.store.data["preferences"]["work_models"][selected_provider] = requested_model
                if selected_provider == "codex":
                    self.store.data["preferences"]["chat_model"] = requested_model
                percent = _context_percent(chat_thread.get(
                    "context_window_percent", context_window_percent(
                        self.config, selected_provider,
                    ),
                ))
                context_max = model_max_context_window(
                    self.config, selected_provider, requested_model,
                )
                context_limit = model_context_window(
                    self.config, selected_provider, requested_model, percent,
                )
                if context_limit is not None:
                    chat_thread["context_max_tokens"] = context_max
                    chat_thread["context_limit_tokens"] = context_limit
                    chat_thread["context_window_percent"] = percent
                else:
                    chat_thread.pop("context_max_tokens", None)
                    chat_thread.pop("context_limit_tokens", None)
                _set_context_estimate_metadata(
                    chat_thread, self.config, selected_provider, requested_model,
                    Path(chat_thread.get("cwd") or self.default_cwd), context_limit,
                )
                now = int(time.time())
                if not chat_thread["messages"]:
                    chat_thread["title"] = " ".join(content.split())[:54]
                chat_thread["messages"].append({
                    "id": request_id,
                    "role": "user",
                    "content": content,
                    "created_at": now,
                })
                pending = {
                    "id": uuid.uuid4().hex,
                    "role": "assistant",
                    "content": "",
                    "created_at": now,
                    "pending": True,
                    "provider": selected_provider,
                    "model": requested_model,
                    "reasoning_effort": reasoning_effort,
                }
                chat_thread["messages"].append(pending)
                chat_thread["updated_at"] = now
                self.store.save()
                public = self.store.chat_public()
            self.chat_run = active
        thread = threading.Thread(
            target=self._run_chat,
            args=(pending["id"], content, selected_provider, requested_model, reasoning_effort, active),
            name="pilferedparrot-chat",
            daemon=True,
        )
        active.thread = thread
        try:
            thread.start()
        except Exception as error:
            with self.runs_lock:
                with self.store.lock:
                    chat_thread = self.store.data["chat"]
                    failed = next(
                        message for message in chat_thread["messages"]
                        if message.get("id") == pending["id"]
                    )
                    failed.update({
                        "content": "Chat error: "
                        f"{redact_configured_secrets(self.config, error)}",
                        "error": True,
                    })
                    failed.pop("pending", None)
                    chat_thread["updated_at"] = int(time.time())
                    self.store.save()
                if self.chat_run is active:
                    self.chat_run = None
            raise
        return public

    def _run_chat(
        self, pending_id: str, content: str, provider: str, model: str,
        reasoning_effort: str | None,
        active: ActiveRun,
    ) -> None:
        reply = ""
        conversation: Conversation | None = None
        try:
            with self.store.lock:
                chat_thread = self.store.data["chat"]
                session_id = chat_thread.get("provider_session_id")
                percent = _context_percent(chat_thread.get(
                    "context_window_percent", context_window_percent(self.config, provider),
                ))
                provider_messages = list(chat_thread.get("provider_messages") or [])
                cwd = Path(chat_thread.get("cwd") or self.default_cwd)
            if provider == "qwen":
                ensure_qwen(self.config, cancel_event=active.cancel_event)
            context_limit = model_context_window(self.config, provider, model, percent)
            run_config = deepcopy(self.config)
            run_config[provider]["model"] = model
            if provider == "codex":
                run_config["codex"]["reasoning_effort"] = (
                    reasoning_effort or self.chat_reasoning_effort
                )
                run_config["codex"]["sandbox"] = "read-only"
                run_config["codex"]["additional_write_dirs"] = []
            elif provider == "claude":
                run_config["claude"]["permission_mode"] = "plan"
            elif provider == "gemini":
                run_config["gemini"]["approval_mode"] = "plan"
            elif provider == "antigravity":
                run_config[provider]["mode"] = "plan"
                run_config[provider]["read_only"] = True
                run_config[provider]["additional_dirs"] = []
            else:
                run_config[provider]["read_only"] = True
                run_config[provider]["additional_dirs"] = []
            if provider == "codex" and context_limit is not None:
                run_config["codex"]["context_window_limit_tokens"] = context_limit
            conversation = Conversation(
                provider=provider, provider_session_id=session_id,
                response_identity=configured_identity(run_config, provider),
                messages=provider_messages
                if provider == "qwen" or run_config.get(provider, {}).get("adapter") \
                == "openai_compatible" else [],
            )
            result = capture_dispatch(
                provider, content, cwd, conversation,
                run_config, active.cancel_event,
            )
            if result.exit_code:
                raise RuntimeError(result.error or result.text or "Chat model exited without a response")
            reply = result.text.strip()
            with self.store.lock:
                chat_thread = self.store.data["chat"]
                chat_thread["provider_session_id"] = result.session_id \
                    or conversation.provider_session_id
                chat_thread["provider_messages"] = conversation.messages
                if result.input_tokens is not None and result.output_tokens is not None:
                    chat_thread["last_turn_usage"] = {
                        "input_tokens": result.input_tokens,
                        "output_tokens": result.output_tokens,
                    }
                if result.live_input_tokens is not None:
                    chat_thread["live_context_usage"] = {
                        "input_tokens": result.live_input_tokens,
                        "output_tokens": result.live_output_tokens or 0,
                    }
                    if result.live_context_window_tokens is not None \
                            and chat_thread.get("context_limit_tokens"):
                        chat_thread["output_reservation_tokens"] = max(
                            0,
                            int(chat_thread["context_limit_tokens"])
                            - result.live_context_window_tokens,
                        )
        except RunCancelled:
            reply = "Stopped."
        except Exception as error:
            reply = f"Chat error: {redact_configured_secrets(self.config, error)}"
        finally:
            with self.runs_lock:
                with self.store.lock:
                    chat_thread = self.store.data["chat"]
                    pending = next(
                        message for message in chat_thread["messages"]
                        if message.get("id") == pending_id
                    )
                    chat_thread["context_chars"] = int(chat_thread.get("context_chars", 0)) \
                        + len(content) + len(reply)
                    percent = _context_percent(chat_thread.get(
                        "context_window_percent", context_window_percent(
                            self.config, provider,
                        ),
                    ))
                    context_max = model_max_context_window(self.config, provider, model)
                    context_limit = model_context_window(
                        self.config, provider, model, percent,
                    )
                    if context_limit is not None:
                        chat_thread["context_limit_tokens"] = context_limit
                        chat_thread["context_max_tokens"] = context_max
                        chat_thread["context_window_percent"] = percent
                    else:
                        chat_thread.pop("context_limit_tokens", None)
                        chat_thread.pop("context_max_tokens", None)
                    usage = _context_usage(
                        chat_thread["context_chars"], self.chat_context_warning_chars,
                        limit_tokens=chat_thread.get("context_limit_tokens"),
                        max_tokens=chat_thread.get("context_max_tokens"),
                        allowance_percent=chat_thread.get("context_window_percent"),
                        live_input_tokens=(chat_thread.get("live_context_usage") or {}).get(
                            "input_tokens"
                        ),
                        live_output_tokens=(chat_thread.get("live_context_usage") or {}).get(
                            "output_tokens"
                        ),
                        overhead_tokens=chat_thread.get("context_overhead_tokens", 0),
                        output_reservation_tokens=chat_thread.get(
                            "output_reservation_tokens", 0,
                        ),
                    )
                    crossed_warning = (
                        usage["percent"] >= 80
                        and not chat_thread.get("warning_announced")
                    )
                    if crossed_warning:
                        reply += (
                            "\n\nThis Chat thread is carrying a lot of context now. "
                            "Start a new Chat conversation soon to keep responses quick and economical."
                        )
                        chat_thread["warning_announced"] = True
                    chat_thread["context_warning"] = usage["percent"] >= 80
                    pending["content"] = reply
                    if conversation is not None:
                        pending["response_identity"] = deepcopy(conversation.response_identity)
                    pending["reasoning_effort"] = reasoning_effort
                    if reply.startswith("Chat error:"):
                        pending["error"] = True
                    pending.pop("pending", None)
                    pending.pop("cancel_requested", None)
                    chat_thread["updated_at"] = int(time.time())
                    self.store.save()
                if self.chat_run is active:
                    self.chat_run = None

    def cancel_chat(self) -> dict[str, Any]:
        with self.runs_lock:
            active = self.chat_run
            with self.store.lock:
                chat_thread = self.store.data["chat"]
                pending = next(
                    (message for message in chat_thread["messages"] if message.get("pending")), None,
                )
                if pending is None:
                    raise ValueError("Chat is not responding")
                pending["cancel_requested"] = True
                self.store.save()
            if active is not None:
                active.cancel_event.set()
            return self.store.chat_public()

    def reset_chat(
        self, payload: dict[str, Any] | None = None, *, provider: str | None = None,
    ) -> dict[str, Any]:
        payload = payload or {}
        with self.runs_lock:
            if self.chat_run is not None:
                raise ValueError("stop Chat before starting a new conversation")
            with self.store.lock:
                current_provider = str(self.store.data["chat"].get("provider")
                                       or self.default_provider)
            selected_provider = str(provider or payload.get("provider") or current_provider)
            if selected_provider not in self._provider_ids(include_hidden=True):
                raise ValueError("unknown provider")
            if provider is not None and current_provider != selected_provider:
                raise ValueError("Chat belongs to another provider window")
            model = self._normalize_model(payload.get("model")) \
                if "model" in payload else self._preferred_chat_model(selected_provider)
            if model is None:
                raise ValueError("choose a model before starting a Chat")
            if "reasoning_effort" in payload:
                reasoning_effort = self._selected_reasoning_effort(
                    selected_provider, model, payload.get("reasoning_effort"),
                )
            else:
                with self.store.lock:
                    previous_effort = self.store.data["chat"].get("reasoning_effort")
                try:
                    reasoning_effort = self._selected_reasoning_effort(
                        selected_provider, model, previous_effort,
                    )
                except ValueError:
                    reasoning_effort = None
            chat_thread = self.store.reset_chat(
                model, selected_provider, reasoning_effort=reasoning_effort,
            )
            percent = self._preferred_chat_context_percent(selected_provider)
            context_max = model_max_context_window(self.config, selected_provider, model)
            context_limit = model_context_window(
                self.config, selected_provider, model, percent,
            )
            with self.store.lock:
                self.store.data["preferences"]["work_models"][selected_provider] = model
                if selected_provider == "codex":
                    self.store.data["preferences"]["chat_model"] = model
            if context_limit is not None:
                with self.store.lock:
                    self.store.data["chat"]["context_limit_tokens"] = context_limit
                    self.store.data["chat"]["context_max_tokens"] = context_max
                    self.store.data["chat"]["context_window_percent"] = percent
                    _set_context_estimate_metadata(
                        self.store.data["chat"], self.config, selected_provider, model,
                        Path(self.store.data["chat"].get("cwd") or self.default_cwd),
                        context_limit,
                    )
                    self.store.save()
                chat_thread = self.store.chat_public()
            else:
                with self.store.lock:
                    self.store.save()
            return {
                "chat": chat_thread,
                "chat_history": self.store.chat_history_public(selected_provider),
            }

    def launch_terminal_command(
        self, chat_id: str, payload: dict[str, Any], *, window_id: str | None = None,
    ) -> None:
        message_id = payload.get("message_id")
        block_index = payload.get("block_index")
        if not isinstance(message_id, str) or not message_id:
            raise ValueError("message_id must be a non-empty string")
        if isinstance(block_index, bool) or not isinstance(block_index, int) or block_index < 0:
            raise ValueError("block_index must be a non-negative integer")
        with self.store.lock:
            chat = self._owned_chat(chat_id, window_id)
            message = self._message(chat, message_id)
            if message.get("role") != "assistant" or message.get("pending"):
                raise ValueError("only completed assistant commands can be run")
            blocks = _fenced_code_block_details(str(message.get("content") or ""))
            if block_index >= len(blocks):
                raise ValueError("command block was not found")
            language, command = blocks[block_index]
            cwd = Path(chat["cwd"])
        if language and language not in CODE_BLOCK_LANGUAGES:
            raise ValueError("only shell command blocks can be run")
        if not command or "\n" in command or len(command) > 4_000:
            raise ValueError("only a single non-empty command line can be run")
        if not cwd.is_dir():
            raise ValueError(f"project folder does not exist: {cwd}")
        if sys.platform == "win32" and language in {"bash", "sh", "zsh", "fish"}:
            raise ValueError("This command requires a Unix shell; use a PowerShell command on Windows")
        launch_terminal(command, cwd)

    def provider_auth_action(self, provider: str, action: str) -> dict[str, Any]:
        """Launch a provider-owned browser sign-in or clear its stored CLI login."""
        if provider not in self._provider_ids(include_hidden=True):
            raise ValueError("unknown provider")
        if action not in {"login", "logout"}:
            raise ValueError("provider action must be login or logout")
        if provider not in {"codex", "claude"}:
            raise ValueError("This provider uses environment or local endpoint authentication")
        command = resolve_command(self.config, provider)
        if command is None:
            raise RuntimeError(f"{PROVIDER_LABELS.get(provider, provider)} CLI was not found")
        argv = {
            ("codex", "login"): [command, "login"],
            ("codex", "logout"): [command, "logout"],
            ("claude", "login"): [command, "auth", "login"],
            ("claude", "logout"): [command, "auth", "logout"],
        }.get((provider, action))
        if argv is None:
            raise ValueError(f"{PROVIDER_LABELS.get(provider, provider)} does not support {action}")
        argv = provider_argv(argv)
        if action == "login":
            with self.provider_login_lock:
                active = self.provider_logins.get(provider)
                if active is not None and active.process.poll() is None:
                    return {
                        "ok": True, "launched": False, "active": True,
                        "destination": "browser",
                        "confirmation_code": provider == "claude",
                    }
                self.provider_logins.pop(provider, None)
            login_env = os.environ.copy()
            # The desktop launcher sets BROWSER to PilferedParrot's private
            # app-window helper so Python opens the dashboard in its isolated
            # Chrome profile. Provider CLIs must not inherit that override:
            # their OAuth pages belong in the user's normal default browser.
            login_env.pop("BROWSER", None)
            process = subprocess.Popen(
                argv, cwd=self.default_cwd, stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                env=login_env,
                start_new_session=True, close_fds=True,
            )
            # Claude's native login pauses for Enter before opening its OAuth
            # URL. Feed that confirmation through a private pipe so no terminal
            # or copied link is part of the user flow. Codex opens the system
            # browser directly and safely ignores the unused input stream.
            if provider == "claude" and process.stdin is not None:
                try:
                    process.stdin.write(b"\n")
                    process.stdin.flush()
                except (BrokenPipeError, OSError):
                    pass
            # Claude races its loopback callback against a manual `code#state`
            # fallback shown by Anthropic in the browser. Keep its pipe open so
            # the dashboard can submit that fallback into this same PKCE flow.
            # A code from one attempt cannot be used by a second login process.
            if provider != "claude" and process.stdin is not None:
                process.stdin.close()

            # Startup failures are otherwise indistinguishable from a healthy
            # OAuth flow: the UI begins polling while the CLI has already
            # exited. Give the provider a short startup window and return a
            # useful, URL-scrubbed error instead of claiming a browser opened.
            time.sleep(0.5)
            if process.poll() not in {None, 0}:
                detail = process.stderr.read().decode("utf-8", errors="replace") \
                    if process.stderr is not None else ""
                if process.stderr is not None:
                    process.stderr.close()
                if process.stdin is not None:
                    process.stdin.close()
                detail = re.sub(r"https?://\S+", "[sign-in URL omitted]", detail)
                detail = re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", "", detail).strip()
                raise RuntimeError(
                    detail[:500]
                    or f"{PROVIDER_LABELS.get(provider, provider)} sign-in failed to start"
                )

            login = ActiveProviderLogin(process=process)
            with self.provider_login_lock:
                self.provider_logins[provider] = login

            def reap_login() -> None:
                try:
                    if process.stderr is not None:
                        process.stderr.read()
                    process.wait()
                finally:
                    if process.stdin is not None:
                        try:
                            process.stdin.close()
                        except OSError:
                            pass
                    if process.stderr is not None:
                        try:
                            process.stderr.close()
                        except OSError:
                            pass
                    with self.provider_login_lock:
                        if self.provider_logins.get(provider) is login:
                            self.provider_logins.pop(provider, None)

            threading.Thread(
                target=reap_login, name=f"pilferedparrot-{provider}-login",
                daemon=True,
            ).start()
            return {
                "ok": True, "launched": True, "active": True,
                "destination": "browser", "confirmation_code": provider == "claude",
            }
        # A still-running login could otherwise win the race after logout and
        # immediately authenticate the account again.
        with self.provider_login_lock:
            active_login = self.provider_logins.pop(provider, None)
        if active_login is not None and active_login.process.poll() is None:
            if active_login.process.stdin is not None:
                try:
                    active_login.process.stdin.close()
                except OSError:
                    pass
            _stop_process(active_login.process)
        completed = subprocess.run(
            argv, cwd=self.default_cwd, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=15,
        )
        if completed.returncode != 0:
            detail = ((completed.stderr or completed.stdout) or "").strip()
            raise RuntimeError(detail or f"{PROVIDER_LABELS.get(provider, provider)} logout failed")
        return {"ok": True, "launched": False}

    def submit_provider_auth_code(self, provider: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Send Anthropic's manual browser confirmation to the active Claude login."""
        if provider != "claude":
            raise ValueError("confirmation codes are supported only for Claude sign-in")
        code = payload.get("code")
        if not isinstance(code, str):
            raise ValueError("confirmation code is required")
        code = code.strip()
        if not re.fullmatch(r"[^\s#]{1,1024}#[^\s#]{1,1024}", code):
            raise ValueError("paste the complete Anthropic confirmation code, including #")
        with self.provider_login_lock:
            active = self.provider_logins.get(provider)
            if active is None or active.process.poll() is not None:
                self.provider_logins.pop(provider, None)
                raise ValueError("Claude sign-in is no longer waiting for a confirmation code")
            if active.process.stdin is None:
                raise RuntimeError("Claude sign-in cannot accept a confirmation code")
            try:
                active.process.stdin.write(code.encode("utf-8") + b"\n")
                active.process.stdin.flush()
            except (BrokenPipeError, OSError) as exc:
                raise ValueError(
                    "Claude sign-in is no longer waiting for a confirmation code"
                ) from exc
        return {"ok": True, "submitted": True}

    def _provider_launch_workspace(self, provider: str, requested: Any) -> Path | None:
        """Pick a workspace for a new provider window, or None to let the operator choose.

        Tries the inherited folder, then the provider's configured default. A
        candidate is skipped rather than fatal: the window is the only surface
        on which a wrong folder can be corrected, so it must open either way.
        """
        candidates: list[Any] = [requested or self.default_cwd]
        default_workspace = provider_default_workspace(self.config, provider)
        if default_workspace is not None:
            candidates.append(default_workspace)
        for candidate in candidates:
            try:
                cwd = _project_directory(_migrate_renamed_project_path(
                    candidate, self.renamed_repository_root,
                ))
                _validate_provider_workspace(provider, cwd, self.config)
            except (ValueError, OSError, RuntimeError):
                continue
            return cwd
        return None

    def choose_project_directory(
        self, payload: dict[str, Any], provider: str | None = None,
    ) -> dict[str, str | None]:
        """Let the operator choose a writable provider workspace natively."""
        cwd = _select_project_directory(payload.get("cwd"))
        if cwd is None:
            return {"path": None}
        selected_provider = provider or self.default_provider
        if selected_provider not in self._provider_ids():
            raise ValueError("unknown provider")
        _validate_provider_workspace(selected_provider, cwd, self.config)
        return {"path": str(cwd)}

    def open_provider_window(self, url: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Validate provider policy, then ask the native façade to open its window."""
        provider = str(payload.get("provider") or "")
        if provider not in self._provider_ids():
            raise ValueError("unknown provider")
        model = self._normalize_model(payload.get("model"))
        # Carry the source window's selected project into the isolated window
        # when the provider can accept it. The source window may sit somewhere
        # this provider is not allowed to work — the home directory reaches
        # Codex legitimately but is refused for sandboxed providers — and a
        # refusal that raised here produced no window at all, leaving nowhere
        # to correct the folder. Fall back, then ask.
        cwd = self._provider_launch_workspace(provider, payload.get("cwd"))
        return self.native.open_provider_window(
            url, provider=provider, model=model, cwd=cwd, payload=payload,
            issue_capability=self.issue_capability, browser=_chromium_browser(),
        )

    def open_chrome_theme_gallery(self) -> dict[str, Any]:
        return self.native.open_theme_gallery(browser=_chromium_browser())

    def browser_theme(self) -> dict[str, Any]:
        return self.native.browser_theme()

    def chrome_theme_background(self) -> tuple[bytes, str] | None:
        return self.native.chrome_theme_background()

    def open_chat_window(self, url: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Open Chat in a normal native window, isolated from the maximized main profile."""
        provider = str(payload.get("provider") or self.default_provider)
        if provider not in self._provider_ids(include_hidden=True):
            raise ValueError("unknown provider")
        self._require_chat_support(provider)
        requested_model = self._normalize_model(payload.get("model"))
        model = requested_model or self._preferred_chat_model(provider)
        if model is None:
            raise ValueError("choose a model before opening Chat")
        cwd = self._provider_launch_workspace(provider, payload.get("cwd"))
        if cwd is None:
            raise ValueError("choose a project folder this provider can use before opening Chat")
        with self.runs_lock:
            if self.chat_run is not None:
                with self.store.lock:
                    active_provider = self.store.data["chat"].get("provider")
                if active_provider != provider:
                    raise ValueError("stop Chat before opening it from another provider")
            with self.store.lock:
                chat_thread = self.store.data["chat"]
                if chat_thread.get("provider") != provider:
                    chat_thread = self.store.reset_chat(model, provider, str(cwd))
                elif not chat_thread.get("messages"):
                    private_chat = self.store.data["chat"]
                    model_changed = private_chat.get("model") != model
                    private_chat["model"] = model
                    private_chat["cwd"] = str(cwd)
                    if model_changed:
                        private_chat["provider_session_id"] = None
                        private_chat["provider_messages"] = []
                    self.store.save()
        return self.native.open_chat_window(
            url, provider=provider, model=model, payload=payload,
            issue_capability=self.issue_capability, browser=_chromium_browser(),
        )

    def shutdown(self, timeout: float = 3) -> None:
        with self.runs_lock:
            active = list(self.runs.values())
            if self.chat_run is not None:
                active.append(self.chat_run)
            for run in active:
                run.cancel_event.set()
        deadline = time.monotonic() + timeout
        for run in active:
            if run.thread is not None:
                run.thread.join(max(0, deadline - time.monotonic()))
        with self.provider_login_lock:
            provider_logins = list(self.provider_logins.values())
            self.provider_logins.clear()
        for login in provider_logins:
            process = login.process
            if process.stdin is not None:
                try:
                    process.stdin.close()
                except OSError:
                    pass
            if process.poll() is None:
                _stop_process(process)
        self.native.shutdown(deadline=deadline)


def make_handler(app: PilferedParrotApp) -> type[Any]:
    """Build the HTTP handler with web.py's compatibility patch points."""
    return _server.make_handler(
        app, asset_root=ASSET_ROOT, asset_version=ASSET_VERSION,
        runtime_version=RUNTIME_VERSION, api_generation=API_GENERATION,
        version=__version__,
        timer_factory=lambda *args, **kwargs: threading.Timer(*args, **kwargs),
        thread_factory=lambda *args, **kwargs: threading.Thread(*args, **kwargs),
    )


def serve(config: dict[str, Any], cwd: Path, *, open_browser: bool | None = None) -> int:
    """Wire the composition root into the transport-owned server lifecycle."""
    return _server.serve(
        config, cwd, open_browser=open_browser, create_app=PilferedParrotApp,
        make_handler=make_handler,
        read_capability=_pilferedparrot_dashboard_capability,
        browser_url=_browser_url, browser_open=webbrowser.open,
        status=_pilferedparrot_status, terminate=_terminate_stale_pilferedparrot,
        http_server=ThreadingHTTPServer, ipv6_http_server=_IPv6ThreadingHTTPServer,
        timer_factory=threading.Timer,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Browser UI for local and CLI-based LLM providers",
    )
    parser.add_argument("--config")
    parser.add_argument("--cwd", default=str(Path.cwd()))
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args(argv)
    cwd = Path(args.cwd).expanduser().resolve()
    if not cwd.is_dir():
        raise SystemExit(f"not a directory: {cwd}")
    return serve(load_config(args.config), cwd, open_browser=not args.no_browser)


if __name__ == "__main__":
    raise SystemExit(main())
