from __future__ import annotations

import argparse
import errno
import hashlib
import hmac
import ipaddress
import json
import os
import re
import secrets
import shutil
import subprocess
import tempfile
import threading
import time
import webbrowser
from copy import deepcopy
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse
from urllib.request import Request, urlopen

from . import __version__
from .budgets import collect_budgets
from .config import (
    context_window_percent, effective_model, expanded_path, load_config, model_catalog,
    model_context_window, model_max_context_window,
)
from .dispatch import RunCancelled, RunResult, capture_dispatch
from .ledger import append_run
from .model import Conversation, PROVIDERS, ProviderBudget
from .qwen import ensure_qwen
from .web_provider import ActiveRun, ProviderRunOrchestrator
from .web_persistence import PersistentChatStore, chat_store_path, legacy_chat_store_path


ASSET_ROOT = Path(__file__).resolve().parent / "web_assets"
RUNTIME_ROOT = Path(__file__).resolve().parent
ASSET_NAMES = (
    "index.html", "chat.html", "app.css", "app.js", "chat.js", "icon.svg",
    "pilferedparrot-icon.png", "company-logo.png", "company-logo-dark.png",
)
CODE_BLOCK_LANGUAGES = frozenset({
    "bash", "console", "fish", "powershell", "shell", "sh", "terminal", "zsh",
})
ABSOLUTE_PATH = re.compile(r"(?<![\w.])(/[^\s`'\"<>|]+)")
API_GENERATION = 10
CHAT_MODEL_OPTIONS = ("gpt-5.6-terra", "gpt-5.6-luna")
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
) -> dict[str, Any]:
    # Completed-turn usage is aggregate compute consumption, not the number of
    # tokens currently occupying a resumable model context. Use the retained
    # visible transcript as an honest estimate until providers expose the
    # actual current-context measurement.
    used = _estimated_tokens(context_chars)
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
        "basis": "visible_transcript",
    }


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


def _asset_fingerprint(root: Path) -> str:
    """Identify the exact frontend bundle snapshotted by a server process."""
    digest = hashlib.sha256()
    for name in ASSET_NAMES:
        digest.update(name.encode("utf-8") + b"\0")
        try:
            content = (root / name).read_bytes()
        except OSError:
            digest.update(b"missing\0")
            continue
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()[:16]


ASSET_VERSION = _asset_fingerprint(ASSET_ROOT)


def _runtime_fingerprint(root: Path) -> str:
    """Identify the Python runtime loaded by a newly launched process."""
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*.py")):
        relative = path.relative_to(root).as_posix()
        digest.update(relative.encode("utf-8") + b"\0")
        try:
            content = path.read_bytes()
        except OSError:
            digest.update(b"missing\0")
            continue
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()[:16]


RUNTIME_VERSION = _runtime_fingerprint(RUNTIME_ROOT)
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
    if (
        provider == "qwen"
        and cwd == Path.home().resolve()
        and not config["qwen"].get("allow_home_workspace", False)
    ):
        raise ValueError(
            "Qwen cannot use the entire home directory as its workspace unless "
            "qwen.allow_home_workspace is explicitly enabled"
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
    """Extract fenced blocks using the same lightweight rules as the browser."""
    chunks = str(content or "").replace("\r", "").split("```")
    blocks: list[tuple[str | None, str]] = []
    for raw in chunks[1::2]:
        first, separator, rest = raw.partition("\n")
        language = first.lower() if separator and re.fullmatch(r"[\w+-]+", first) else None
        if language:
            raw = rest
        blocks.append((language, raw.strip()))
    return blocks


def _fenced_code_blocks(content: str) -> list[str]:
    return [code for _language, code in _fenced_code_block_details(content)]


def _terminal_argv(command: str, cwd: Path) -> list[str]:
    """Build an interactive terminal command without interpolating shell input."""
    shell = shutil.which("bash") or "/bin/bash"
    script = (
        'cd -- "$1" || exit; bash -lc "$2"; status=$?; '
        'printf "\\nCommand exited with status %s.\\n" "$status"; exec bash'
    )
    terminal = shutil.which("gnome-terminal")
    if terminal:
        return [
            terminal, "--", shell, "-lc", script, "pilferedparrot-terminal", str(cwd), command,
        ]
    terminal = shutil.which("x-terminal-emulator") or shutil.which("xterm")
    if terminal:
        return [
            terminal, "-e", shell, "-lc", script, "pilferedparrot-terminal", str(cwd), command,
        ]
    raise RuntimeError("no supported graphical terminal was found")


def _loopback_host(value: str) -> bool:
    if value == "localhost":
        return True
    try:
        return ipaddress.ip_address(value).is_loopback
    except ValueError:
        return False


def _pilferedparrot_status(url: str) -> str:
    """Classify the listener without trusting an arbitrary service on the port."""
    try:
        with urlopen(f"{url}/api/state", timeout=1) as response:
            payload = json.load(response)
            server = response.headers.get("Server", "")
        if not (
            server.startswith(("PilferedParrot/", "ParrotRelay/", "AIConductor/"))
            and isinstance(payload, dict)
            and isinstance(payload.get("chats"), list)
            and isinstance(payload.get("csrf_token"), str)
        ):
            return "other"
        return "compatible" if (
            payload.get("api_generation") == API_GENERATION
            and payload.get("asset_version") == ASSET_VERSION
            and payload.get("runtime_version") == RUNTIME_VERSION
        ) else "stale"
    except (OSError, ValueError, json.JSONDecodeError):
        return "unavailable"


def _pilferedparrot_csrf_token(url: str) -> str | None:
    """Read the instance-specific control token from a validated local server."""
    try:
        with urlopen(f"{url}/api/state", timeout=1) as response:
            payload = json.load(response)
            server = response.headers.get("Server", "")
        token = payload.get("csrf_token") if isinstance(payload, dict) else None
        if not server.startswith("PilferedParrot/") or not isinstance(token, str) or not token:
            return None
        return token
    except (OSError, ValueError, json.JSONDecodeError):
        return None


def _browser_url(url: str, csrf_token: str) -> str:
    """Build an app URL whose fragment lets only this server instance be closed."""
    return (
        f"{url}/?generation={API_GENERATION}&assets={ASSET_VERSION}"
        f"&runtime={RUNTIME_VERSION}#close_token={csrf_token}"
    )


def _notify_window_closed(browser_url: str) -> bool:
    """Ask the exact local server instance associated with a closed app window to stop."""
    try:
        parsed = urlparse(browser_url)
        if (
            parsed.scheme != "http" or parsed.hostname is None
            or not _loopback_host(parsed.hostname)
            or parsed.username is not None or parsed.password is not None
        ):
            return False
        tokens = parse_qs(parsed.fragment, keep_blank_values=True).get("close_token", [])
        if len(tokens) != 1 or not tokens[0]:
            return False
        request = Request(
            f"http://{parsed.netloc}/api/shutdown",
            data=b"{}",
            headers={
                "Content-Type": "application/json",
                "X-PilferedParrot-CSRF": tokens[0],
            },
            method="POST",
        )
        with urlopen(request, timeout=2) as response:
            return 200 <= response.status < 300
    except (OSError, ValueError):
        return False


def _pilferedparrot_is_running(url: str) -> bool:
    # Kept as a small compatibility wrapper for callers and older integrations.
    return _pilferedparrot_status(url) in {"compatible", "stale"}


def _terminate_stale_pilferedparrot(url: str, port: int) -> None:
    """Stop only the validated PilferedParrot listener occupying this TCP port."""
    fuser = shutil.which("fuser")
    if fuser is None:
        raise RuntimeError("cannot replace stale PilferedParrot because fuser is unavailable")
    subprocess.run(
        [fuser, "-k", "-INT", f"{port}/tcp"],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    # PilferedParrot's graceful shutdown gives active provider runs up to three
    # seconds to observe cancellation and reap their subprocesses.
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        status = _pilferedparrot_status(url)
        if status == "unavailable":
            return
        if status == "other":
            raise RuntimeError(f"another service took over port {port} during restart")
        time.sleep(0.05)
    raise RuntimeError(f"stale PilferedParrot did not release port {port}")


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
            context_usage=_context_usage,
        )


class PilferedParrotApp:
    def __init__(self, config: dict[str, Any], default_cwd: Path):
        self.config = config
        self.default_cwd = default_cwd
        self.renamed_repository_root = _renamed_repository_root(default_cwd)
        self.default_provider = str(config["web"].get("default_provider", "codex"))
        if self.default_provider not in PROVIDERS:
            self.default_provider = "codex"
        self.chat_context_warning_chars = max(
            10_000, int(config["web"].get("chat_context_warning_chars", 80_000)),
        )
        self.technical_context_warning_chars = max(
            10_000, int(config["web"].get("technical_context_warning_chars", 120_000)),
        )
        store_path = chat_store_path(config)
        self.store = ChatStore(
            store_path,
            chat_warning_chars=self.chat_context_warning_chars,
            technical_warning_chars=self.technical_context_warning_chars,
            chat_model=str(config["web"].get("chat_model") or "gpt-5.6-terra").strip(),
            legacy_path=legacy_chat_store_path(config),
        )
        self.csrf_token = secrets.token_urlsafe(32)
        self.chat_window_lock = threading.RLock()
        self.chat_window_process: subprocess.Popen[bytes] | None = None
        self.chat_window_profile: Path | None = None
        self.chat_model = str(
            config["web"].get("chat_model") or "gpt-5.6-terra"
        ).strip()
        if self.chat_model not in CHAT_MODEL_OPTIONS:
            self.chat_model = CHAT_MODEL_OPTIONS[0]
        self.chat_reasoning_effort = str(
            config["web"].get("chat_reasoning_effort") or "low"
        ).strip().lower()
        if self.chat_reasoning_effort not in {
            "none", "low", "medium", "high", "xhigh", "max",
        }:
            raise ValueError("web.chat_reasoning_effort is invalid")
        self.provider_runs = ProviderRunOrchestrator(
            config,
            store=self.store,
            default_cwd=self.default_cwd,
            default_provider=self.default_provider,
            chat_model=self.chat_model,
            chat_model_options=CHAT_MODEL_OPTIONS,
            chat_reasoning_effort=self.chat_reasoning_effort,
            chat_context_warning_chars=self.chat_context_warning_chars,
            dispatch=lambda *args, **kwargs: capture_dispatch(*args, **kwargs),
            ensure_provider=lambda provider_config: ensure_qwen(provider_config),
            budget_collector=lambda provider_config: collect_budgets(provider_config),
            ledger_writer=lambda *args, **kwargs: append_run(*args, **kwargs),
            thread_factory=lambda *args, **kwargs: threading.Thread(*args, **kwargs),
            effective_model_for=lambda provider_config, provider: effective_model(
                provider_config, provider,
            ),
            context_percent_for=lambda provider_config, provider: context_window_percent(
                provider_config, provider,
            ),
            context_max_for=lambda provider_config, provider, model: model_max_context_window(
                provider_config, provider, model,
            ),
            context_limit_for=lambda provider_config, provider, model, percent: (
                model_context_window(provider_config, provider, model, percent)
            ),
            context_usage=_context_usage,
            context_percent=_context_percent,
            project_directory=_project_directory,
            migrate_project_path=lambda value: _migrate_renamed_project_path(
                value, self.renamed_repository_root,
            ),
            validate_workspace=_validate_provider_workspace,
            outside_write_target=_outside_write_target,
        )
        self.store.chat_model = self.chat_model
        with self.store.lock:
            self.store._normalize_chat_thread(self.store.data["chat"])
            for archived in self.store.data["chat_history"]:
                self.store._normalize_chat_thread(archived, archived=True)
            for chat_thread in (
                self.store.data["chat"], *self.store.data["chat_history"],
            ):
                percent = _context_percent(chat_thread.get(
                    "context_window_percent", context_window_percent(self.config, "codex"),
                ))
                model = chat_thread.get("model") or self.chat_model
                chat_max = model_max_context_window(self.config, "codex", model)
                chat_limit = model_context_window(self.config, "codex", model, percent)
                if chat_limit is not None:
                    chat_thread["context_limit_tokens"] = chat_limit
                    chat_thread["context_max_tokens"] = chat_max
                    chat_thread["context_window_percent"] = percent
                else:
                    chat_thread.pop("context_limit_tokens", None)
                    chat_thread.pop("context_max_tokens", None)
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
                if provider not in PROVIDERS:
                    provider = technical_chat.get("requested_provider")
                    if provider not in PROVIDERS:
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
            self.store.save()

    @property
    def runs_lock(self) -> threading.RLock:
        """Compatibility view of the provider coordinator's run lock."""
        return self.provider_runs.runs_lock

    @property
    def runs(self) -> dict[str, ActiveRun]:
        """Compatibility view of active work-session runs."""
        return self.provider_runs.runs

    @property
    def chat_run(self) -> ActiveRun | None:
        """Compatibility view of the active Chat run."""
        return self.provider_runs.chat_run

    def recover_interrupted(self) -> int:
        return self.provider_runs.recover_interrupted()

    def budgets(self) -> dict[str, ProviderBudget]:
        return self.provider_runs.budgets()

    def state(self) -> dict[str, Any]:
        catalog = model_catalog(self.config)
        codex_models = {
            str(option.get("value"))
            for option in catalog.get("codex", {}).get("options", [])
            if option.get("value")
        }
        codex_models.update((*CHAT_MODEL_OPTIONS, "gpt-5.6-sol"))
        model_context_windows = {
            "qwen": {
                str(option["value"]): model_max_context_window(
                    self.config, "qwen", str(option["value"]),
                )
                for option in catalog.get("qwen", {}).get("options", [])
                if option.get("value")
            },
            "codex": {
                model: model_max_context_window(self.config, "codex", model)
                for model in codex_models
            },
        }
        return {
            "chats": self.store.list_public(),
            "default_cwd": str(self.default_cwd),
            "default_provider": self.default_provider,
            "api_generation": API_GENERATION,
            "asset_version": ASSET_VERSION,
            "runtime_version": RUNTIME_VERSION,
            "chat": self.store.chat_public(),
            "chat_history": self.store.chat_history_public(),
            "chat_model": self.chat_model,
            "chat_model_choices": list(CHAT_MODEL_OPTIONS),
            "csrf_token": self.csrf_token,
            "models": {
                name: catalog[name]["default"] or "provider default" for name in PROVIDERS
            },
            "model_catalog": deepcopy(catalog),
            "model_context_windows": model_context_windows,
        }

    def create_chat(self, payload: dict[str, Any]) -> dict[str, Any]:
        provider = str(payload.get("provider") or self.default_provider)
        if provider not in PROVIDERS:
            raise ValueError("provider must be qwen or codex")
        requested_model = self._normalize_model(payload.get("model"))
        cwd = _project_directory(_migrate_renamed_project_path(
            payload.get("cwd") or self.default_cwd, self.renamed_repository_root,
        ))
        _validate_provider_workspace(provider, cwd, self.config)
        model = requested_model or effective_model(self.config, provider)
        percent = context_window_percent(self.config, provider)
        context_max = model_max_context_window(self.config, provider, model)
        context_limit = model_context_window(self.config, provider, model, percent)
        return self.store.create(
            cwd, provider, requested_model, context_limit, context_max, percent,
        )

    def set_context_window(self, chat_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        percent = _context_percent(payload.get("percent"))
        with self.runs_lock:
            if chat_id in self.runs:
                raise ValueError("stop the response before changing the context allowance")
            with self.store.lock:
                chat = self.store.get(chat_id)
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
                self.store.save()
                return self.store.public(chat)

    def set_chat_context_window(self, payload: dict[str, Any]) -> dict[str, Any]:
        percent = _context_percent(payload.get("percent"))
        with self.runs_lock:
            if self.chat_run is not None:
                raise ValueError("stop Chat before changing the context allowance")
            with self.store.lock:
                chat_thread = self.store.data["chat"]
                model = chat_thread.get("model") or self.chat_model
                maximum = model_max_context_window(self.config, "codex", model)
                limit = model_context_window(self.config, "codex", model, percent)
                if maximum is None or limit is None:
                    raise ValueError("the selected model does not publish a context maximum")
                chat_thread["context_window_percent"] = percent
                chat_thread["context_max_tokens"] = maximum
                chat_thread["context_limit_tokens"] = limit
                self.store.save()
                return self.store.chat_public()

    def set_chat_model(self, payload: dict[str, Any]) -> dict[str, Any]:
        model = self._normalize_chat_model(payload.get("model"))
        with self.runs_lock:
            if self.chat_run is not None:
                raise ValueError("stop Chat before changing the chat model")
            with self.store.lock:
                chat_thread = self.store.data["chat"]
                if chat_thread.get("messages"):
                    raise ValueError("start a new chat before changing the chat model")
                percent = _context_percent(chat_thread.get(
                    "context_window_percent", context_window_percent(self.config, "codex"),
                ))
                maximum = model_max_context_window(self.config, "codex", model)
                limit = model_context_window(self.config, "codex", model, percent)
                chat_thread["model"] = model
                chat_thread.pop("provider_session_id", None)
                if limit is not None:
                    chat_thread["context_max_tokens"] = maximum
                    chat_thread["context_limit_tokens"] = limit
                    chat_thread["context_window_percent"] = percent
                else:
                    chat_thread.pop("context_max_tokens", None)
                    chat_thread.pop("context_limit_tokens", None)
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

    def delete_chat(self, chat_id: str) -> None:
        with self.runs_lock:
            if chat_id in self.runs:
                raise ValueError("cancel the running response before deleting this work session")
            self.store.delete(chat_id)

    def send_message(self, chat_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        return self.provider_runs.send_message(chat_id, payload)

    def _run_message(
        self, chat_id: str, pending_id: str, prompt: str, active: ActiveRun,
    ) -> None:
        self.provider_runs._run_message(chat_id, pending_id, prompt, active)

    @staticmethod
    def _message(chat: dict[str, Any], message_id: str) -> dict[str, Any]:
        return ProviderRunOrchestrator._message(chat, message_id)

    def cancel_message(self, chat_id: str) -> dict[str, Any]:
        return self.provider_runs.cancel_message(chat_id)

    def send_chat_message(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self.provider_runs.send_chat_message(payload)

    def _normalize_chat_model(self, value: Any) -> str:
        return self.provider_runs.normalize_chat_model(value)

    def _run_chat(
        self, pending_id: str, content: str, model: str, active: ActiveRun,
    ) -> None:
        self.provider_runs._run_chat(pending_id, content, model, active)

    def cancel_chat(self) -> dict[str, Any]:
        return self.provider_runs.cancel_chat()

    def reset_chat(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        return self.provider_runs.reset_chat(payload)

    def launch_terminal_command(self, chat_id: str, payload: dict[str, Any]) -> None:
        message_id = payload.get("message_id")
        block_index = payload.get("block_index")
        if not isinstance(message_id, str) or not message_id:
            raise ValueError("message_id must be a non-empty string")
        if isinstance(block_index, bool) or not isinstance(block_index, int) or block_index < 0:
            raise ValueError("block_index must be a non-negative integer")
        with self.store.lock:
            chat = self.store.get(chat_id)
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
        subprocess.Popen(
            _terminal_argv(command, cwd), cwd=cwd, stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True, close_fds=True,
        )

    @staticmethod
    def _chat_window_number(payload: dict[str, Any], name: str, minimum: int) -> int:
        value = payload.get(name)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"chat window {name} is invalid")
        number = int(value)
        if number < minimum or number > 32_768:
            raise ValueError(f"chat window {name} is invalid")
        return number

    def _clean_chat_window_profile(self) -> None:
        profile = self.chat_window_profile
        self.chat_window_profile = None
        if profile is not None:
            shutil.rmtree(profile, ignore_errors=True)

    def _watch_chat_window(self, process: subprocess.Popen[bytes]) -> None:
        process.wait()
        with self.chat_window_lock:
            if self.chat_window_process is process:
                self.chat_window_process = None
                self._clean_chat_window_profile()

    def open_chat_window(self, url: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Open Chat in a normal native window, isolated from the maximized main profile."""
        width = self._chat_window_number(payload, "width", 320)
        height = self._chat_window_number(payload, "height", 240)
        left = self._chat_window_number(payload, "left", -32_768)
        top = self._chat_window_number(payload, "top", -32_768)
        with self.chat_window_lock:
            process = self.chat_window_process
            if process is not None and process.poll() is None:
                wmctrl = shutil.which("wmctrl")
                if wmctrl:
                    subprocess.run(
                        [wmctrl, "-x", "-a", "pilferedparrot-chat"], check=False,
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    )
                return {"ok": True, "existing": True}
            self.chat_window_process = None
            self._clean_chat_window_profile()
            browser = next((
                path for candidate in (
                    "google-chrome-stable", "google-chrome", "chromium", "chromium-browser",
                ) if (path := shutil.which(candidate))
            ), None)
            if browser is None:
                raise RuntimeError("Chrome or Chromium is required for the Chat window")
            profile = Path(tempfile.mkdtemp(prefix="pilferedparrot-chat-"))
            try:
                process = subprocess.Popen(
                    [
                        browser, f"--user-data-dir={profile}", "--no-first-run",
                        "--no-default-browser-check", "--disable-background-mode",
                        "--disable-session-crashed-bubble", "--class=pilferedparrot-chat",
                        f"--window-size={width},{height}", f"--window-position={left},{top}",
                        f"--app={url}",
                    ],
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL, start_new_session=True, close_fds=True,
                )
            except Exception:
                shutil.rmtree(profile, ignore_errors=True)
                raise
            self.chat_window_profile = profile
            self.chat_window_process = process
            watcher = threading.Thread(
                target=self._watch_chat_window, args=(process,),
                name="pilferedparrot-chat-window", daemon=True,
            )
            watcher.start()
            return {"ok": True, "existing": False}

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
        with self.chat_window_lock:
            process = self.chat_window_process
            if process is not None and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=max(0, deadline - time.monotonic()))
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=1)
            self.chat_window_process = None
            self._clean_chat_window_profile()


def make_handler(app: PilferedParrotApp) -> type[BaseHTTPRequestHandler]:
    # Keep the frontend and API on the same generation. During development or
    # an in-place update, rereading assets from disk would let an old process
    # serve new JavaScript that calls routes the process does not have yet.
    asset_cache: dict[str, bytes] = {}
    window_close_lock = threading.Lock()
    window_close_timer: threading.Timer | None = None

    def cancel_window_close() -> None:
        nonlocal window_close_timer
        with window_close_lock:
            if window_close_timer is not None:
                window_close_timer.cancel()
                window_close_timer = None

    def schedule_window_close(server: ThreadingHTTPServer) -> None:
        nonlocal window_close_timer
        with window_close_lock:
            if window_close_timer is not None:
                window_close_timer.cancel()
            # A reload closes and immediately reopens the document. Give that
            # new document time to cancel shutdown while still making a real
            # window close reliably release the server.
            window_close_timer = threading.Timer(2, server.shutdown)
            window_close_timer.name = "pilferedparrot-window-close-grace"
            window_close_timer.daemon = True
            window_close_timer.start()
    for asset_name in ASSET_NAMES:
        try:
            asset_cache[asset_name] = (ASSET_ROOT / asset_name).read_bytes()
        except OSError:
            pass

    class Handler(BaseHTTPRequestHandler):
        server_version = f"PilferedParrot/{__version__}"

        def log_message(self, fmt: str, *args: Any) -> None:
            print(f"[web] {self.address_string()} {fmt % args}")

        def _json(self, payload: Any, status: HTTPStatus = HTTPStatus.OK) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-PilferedParrot-Assets", ASSET_VERSION)
            self.send_header("X-PilferedParrot-Runtime", RUNTIME_VERSION)
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def _read_json(self) -> dict[str, Any]:
            content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
            if content_type != "application/json":
                raise ValueError("Content-Type must be application/json")
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError as error:
                raise ValueError("invalid Content-Length") from error
            if length < 0 or length > 1_000_000:
                raise ValueError("invalid request size")
            data = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(data, dict):
                raise ValueError("request body must be an object")
            return data

        def _local_request_allowed(self) -> bool:
            try:
                peer_is_local = ipaddress.ip_address(self.client_address[0]).is_loopback
                host = urlparse(f"//{self.headers.get('Host', '')}").hostname
                host_is_local = host is not None and _loopback_host(host)
            except ValueError:
                return False
            return peer_is_local and host_is_local

        def _control_allowed(self) -> bool:
            origin = self.headers.get("Origin")
            origin_is_local = True
            if origin:
                parsed = urlparse(origin)
                origin_is_local = (
                    parsed.scheme == "http" and parsed.hostname is not None
                    and _loopback_host(parsed.hostname)
                )
            supplied = self.headers.get(
                "X-PilferedParrot-CSRF",
                self.headers.get("X-Parrot-Relay-CSRF", self.headers.get("X-Conductor-CSRF", "")),
            )
            return (
                self._local_request_allowed() and origin_is_local
                and hmac.compare_digest(supplied, app.csrf_token)
            )

        def _asset(self, name: str, content_type: str) -> None:
            body = asset_cache.get(name)
            if body is None:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-PilferedParrot-Assets", ASSET_VERSION)
            self.send_header("X-PilferedParrot-Runtime", RUNTIME_VERSION)
            self.send_header("Content-Security-Policy", (
                "default-src 'self'; base-uri 'none'; frame-ancestors 'none'; "
                "form-action 'self'; object-src 'none'; script-src 'self'; "
                "style-src 'self' 'unsafe-inline'"
            ))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def do_GET(self) -> None:
            path = urlparse(self.path).path
            if path.startswith("/api/") and not self._local_request_allowed():
                self._json({"error": "local API authorization failed"}, HTTPStatus.FORBIDDEN)
                return
            if path == "/":
                self._asset("index.html", "text/html; charset=utf-8")
            elif path == "/chat":
                self._asset("chat.html", "text/html; charset=utf-8")
            elif path == "/app.css":
                self._asset("app.css", "text/css; charset=utf-8")
            elif path == "/app.js":
                self._asset("app.js", "text/javascript; charset=utf-8")
            elif path == "/chat.js":
                self._asset("chat.js", "text/javascript; charset=utf-8")
            elif path == "/icon.svg":
                self._asset("icon.svg", "image/svg+xml")
            elif path == "/pilferedparrot-icon.png":
                self._asset("pilferedparrot-icon.png", "image/png")
            elif path == "/company-logo.png":
                self._asset("company-logo.png", "image/png")
            elif path == "/company-logo-dark.png":
                self._asset("company-logo-dark.png", "image/png")
            elif path == "/favicon.ico":
                self.send_response(HTTPStatus.MOVED_PERMANENTLY)
                self.send_header("Location", "/pilferedparrot-icon.png")
                self.end_headers()
            elif path == "/api/state":
                self._json(app.state())
            elif path == "/api/budgets":
                self._json({name: value.as_dict() for name, value in app.budgets().items()})
            else:
                self.send_error(HTTPStatus.NOT_FOUND)

        def do_POST(self) -> None:
            try:
                if not self._control_allowed():
                    self._json({"error": "local control authorization failed"}, HTTPStatus.FORBIDDEN)
                    return
                path = urlparse(self.path).path
                parts = path.strip("/").split("/")
                payload = self._read_json()
                if path == "/api/chats":
                    self._json(app.create_chat(payload), HTTPStatus.CREATED)
                elif path == "/api/chat/messages":
                    self._json(app.send_chat_message(payload), HTTPStatus.ACCEPTED)
                elif path == "/api/chat/cancel":
                    self._json(app.cancel_chat())
                elif path == "/api/chat/reset":
                    self._json(app.reset_chat(payload))
                elif path == "/api/chat/model":
                    self._json(app.set_chat_model(payload))
                elif path == "/api/chat/context":
                    self._json(app.set_chat_context_window(payload))
                elif path == "/api/chat/window":
                    host = self.headers.get("Host", "")
                    self._json(app.open_chat_window(f"http://{host}/chat", payload))
                elif path == "/api/window/open":
                    cancel_window_close()
                    self._json({"ok": True})
                elif path == "/api/window/close":
                    self._json({"ok": True}, HTTPStatus.ACCEPTED)
                    schedule_window_close(self.server)
                elif path == "/api/shutdown":
                    cancel_window_close()
                    self._json({"ok": True}, HTTPStatus.ACCEPTED)
                    threading.Thread(
                        target=self.server.shutdown,
                        name="pilferedparrot-window-close",
                        daemon=True,
                    ).start()
                elif len(parts) == 4 and parts[:2] == ["api", "chats"] and parts[3] == "messages":
                    self._json(app.send_message(parts[2], payload), HTTPStatus.ACCEPTED)
                elif len(parts) == 4 and parts[:2] == ["api", "chats"] and parts[3] == "context":
                    self._json(app.set_context_window(parts[2], payload))
                elif len(parts) == 4 and parts[:2] == ["api", "chats"] and parts[3] == "cancel":
                    self._json(app.cancel_message(parts[2]))
                elif len(parts) == 4 and parts[:2] == ["api", "chats"] and parts[3] == "terminal":
                    app.launch_terminal_command(parts[2], payload)
                    self._json({"ok": True}, HTTPStatus.ACCEPTED)
                else:
                    self.send_error(HTTPStatus.NOT_FOUND)
            except KeyError:
                self._json({"error": "work session not found"}, HTTPStatus.NOT_FOUND)
            except (ValueError, json.JSONDecodeError) as exc:
                self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            except Exception as exc:
                print(f"[web] request failed: {type(exc).__name__}: {exc}")
                self._json({"error": f"PilferedParrot error: {exc}"}, HTTPStatus.INTERNAL_SERVER_ERROR)

        def do_DELETE(self) -> None:
            try:
                if not self._control_allowed():
                    self._json({"error": "local control authorization failed"}, HTTPStatus.FORBIDDEN)
                    return
                parts = urlparse(self.path).path.strip("/").split("/")
                if len(parts) == 3 and parts[:2] == ["api", "chats"]:
                    app.delete_chat(parts[2])
                    self._json({"ok": True})
                else:
                    self.send_error(HTTPStatus.NOT_FOUND)
            except KeyError:
                self._json({"error": "work session not found"}, HTTPStatus.NOT_FOUND)

    return Handler


def serve(config: dict[str, Any], cwd: Path, *, open_browser: bool | None = None) -> int:
    web = config["web"]
    host, port = str(web["host"]), int(web["port"])
    if not _loopback_host(host):
        raise ValueError("web.host must be a loopback address; remote exposure is not supported")
    url = f"http://{host}:{port}"
    should_open = bool(web.get("open_browser", True) if open_browser is None else open_browser)
    status = _pilferedparrot_status(url)
    if status == "compatible":
        print(f"PilferedParrot is already running at {url}")
        if should_open:
            csrf_token = _pilferedparrot_csrf_token(url)
            if csrf_token is None:
                raise RuntimeError("could not attach the app window to the running server")
            webbrowser.open(_browser_url(url, csrf_token))
        return 0
    if status == "stale":
        print(f"Replacing stale PilferedParrot at {url}")
        _terminate_stale_pilferedparrot(url, port)
    app = PilferedParrotApp(config, cwd)
    try:
        server = ThreadingHTTPServer((host, port), make_handler(app))
    except OSError as error:
        status = _pilferedparrot_status(url)
        if error.errno != errno.EADDRINUSE or status == "other":
            raise
        if status == "stale":
            _terminate_stale_pilferedparrot(url, port)
            server = ThreadingHTTPServer((host, port), make_handler(app))
        elif status != "compatible":
            raise
        else:
            print(f"PilferedParrot is already running at {url}")
            if should_open:
                csrf_token = _pilferedparrot_csrf_token(url)
                if csrf_token is None:
                    raise RuntimeError("could not attach the app window to the running server")
                webbrowser.open(_browser_url(url, csrf_token))
            return 0
    recovered = app.recover_interrupted()
    if should_open:
        browser_url = _browser_url(url, app.csrf_token)
        threading.Timer(0.4, lambda: webbrowser.open(browser_url)).start()
    print(f"PilferedParrot is running at {url}")
    if recovered:
        print(f"Recovered {recovered} interrupted response(s).")
    print("Press Ctrl-C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print()
    finally:
        app.shutdown()
        server.server_close()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Browser UI for local Qwen and OpenAI Codex")
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
