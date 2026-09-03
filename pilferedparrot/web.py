from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import shutil
import subprocess
import threading
import time
import webbrowser
from copy import deepcopy
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.request import urlopen

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
from .web_native import (
    NativeIntegration, browser_url as native_browser_url,
    notify_window_closed as native_notify_window_closed,
    open_browser as open_native_browser,
)
from . import web_server as _server


# Compatibility aliases retain the historical web.py import surface.
ASSET_ROOT = _server.ASSET_ROOT
RUNTIME_ROOT = _server.RUNTIME_ROOT
ASSET_NAMES = _server.ASSET_NAMES
CODE_BLOCK_LANGUAGES = frozenset({
    "bash", "console", "fish", "powershell", "shell", "sh", "terminal", "zsh",
})
ABSOLUTE_PATH = re.compile(r"(?<![\w.])(/[^\s`'\"<>|]+)")
API_GENERATION = _server.API_GENERATION
CHAT_MODEL_OPTIONS = ("gpt-5.6-terra", "gpt-5.6-luna")
WRITE_SCOPE_INTENT = re.compile(
    r"\b(?:write access|writable|work(?:ing)?\s+(?:on|in)|implement|build|modify|edit|fix|create)\b",
    re.IGNORECASE,
)


def _asset_fingerprint(root: Path) -> str:
    return _server._asset_fingerprint(root)


ASSET_VERSION = _server.ASSET_VERSION


def _runtime_fingerprint(root: Path) -> str:
    return _server._runtime_fingerprint(root)


RUNTIME_VERSION = _server.RUNTIME_VERSION


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
    return _server.loopback_host(value)


def _pilferedparrot_status(url: str) -> str:
    return _server.pilferedparrot_status(
        url, opener=urlopen, api_generation=API_GENERATION,
        asset_version=ASSET_VERSION, runtime_version=RUNTIME_VERSION,
    )


def _pilferedparrot_csrf_token(url: str) -> str | None:
    return _server.pilferedparrot_csrf_token(url, opener=urlopen)


def _browser_url(url: str, csrf_token: str) -> str:
    return native_browser_url(
        url, csrf_token, api_generation=API_GENERATION,
        asset_version=ASSET_VERSION, runtime_version=RUNTIME_VERSION,
    )


def _notify_window_closed(browser_url: str) -> bool:
    return native_notify_window_closed(
        browser_url, opener=urlopen, is_loopback=_loopback_host,
    )


def _pilferedparrot_is_running(url: str) -> bool:
    return _pilferedparrot_status(url) in {"compatible", "stale"}


def _terminate_stale_pilferedparrot(url: str, port: int) -> None:
    _server.terminate_stale_pilferedparrot(
        url, port, status=_pilferedparrot_status, which=shutil.which,
        runner=subprocess.run, monotonic=time.monotonic, sleeper=time.sleep,
    )


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
        self.native = NativeIntegration()
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

    def open_chat_window(self, url: str, payload: dict[str, Any]) -> dict[str, Any]:
        return self.native.open_chat_window(url, payload)

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
        self.native.shutdown(deadline=deadline)


def make_handler(app: PilferedParrotApp) -> type[Any]:
    return _server.make_handler(
        app, asset_root=ASSET_ROOT, asset_version=ASSET_VERSION,
        runtime_version=RUNTIME_VERSION, api_generation=API_GENERATION,
        version=__version__,
        timer_factory=lambda *args, **kwargs: threading.Timer(*args, **kwargs),
        thread_factory=lambda *args, **kwargs: threading.Thread(*args, **kwargs),
    )


def serve(config: dict[str, Any], cwd: Path, *, open_browser: bool | None = None) -> int:
    return _server.serve(
        config, cwd, open_browser=open_browser, create_app=PilferedParrotApp,
        make_handler=make_handler, read_capability=_pilferedparrot_csrf_token,
        browser_url=_browser_url, browser_open=webbrowser.open,
        status=_pilferedparrot_status, terminate=_terminate_stale_pilferedparrot,
        http_server=ThreadingHTTPServer, timer_factory=threading.Timer,
    )


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
