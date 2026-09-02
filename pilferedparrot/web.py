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
import uuid
import webbrowser
from copy import deepcopy
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse
from urllib.request import Request, urlopen

from . import __version__
from .budgets import collect_budgets
from .config import (
    codex_additional_write_dirs, context_window_percent, effective_model, expanded_path,
    load_config, model_catalog, model_context_window, model_max_context_window,
)
from .dispatch import RunCancelled, RunResult, capture_dispatch
from .ledger import append_run
from .model import Conversation, PROVIDERS, ProviderBudget
from .qwen import ensure_qwen


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


@dataclass
class ActiveRun:
    cancel_event: threading.Event = field(default_factory=threading.Event)
    thread: threading.Thread | None = None


class ChatStore:
    def __init__(
        self, path: Path, *, chat_warning_chars: int = 80_000,
        technical_warning_chars: int = 120_000, legacy_path: Path | None = None,
        chat_model: str = "gpt-5.6-terra",
    ):
        self.path = path
        self.legacy_path = legacy_path
        self.lock = threading.RLock()
        self.chat_warning_chars = max(10_000, chat_warning_chars)
        self.technical_warning_chars = max(10_000, technical_warning_chars)
        self.chat_model = chat_model if chat_model in CHAT_MODEL_OPTIONS else CHAT_MODEL_OPTIONS[0]
        self.data: dict[str, Any] = {"version": 5, "chats": []}
        self._load()

    def _load(self) -> None:
        try:
            source = self.path
            if not source.exists() and self.legacy_path is not None and self.legacy_path.exists():
                source = self.legacy_path
            payload = json.loads(source.read_text(encoding="utf-8"))
            if isinstance(payload, dict) and isinstance(payload.get("chats"), list):
                self.data = payload
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            pass
        # Historical Claude and auto-routed chats remain readable. A new turn in
        # one of them starts in Codex instead of reviving the retired machinery.
        for chat in self.data["chats"]:
            if chat.get("requested_provider") not in PROVIDERS:
                chat["requested_provider"] = "codex"
                chat["requested_model"] = None
            if chat.get("title") in {"New conversation", "New technical activity"} \
                    and not chat.get("messages"):
                chat["title"] = "New work session"
        # Version 5 renames the old interpreter-centric coordinator fields to
        # plain Chat fields while preserving every existing transcript.
        if not isinstance(self.data.get("chat"), dict):
            legacy_chat = self.data.pop("coordinator", None)
            self.data["chat"] = legacy_chat if isinstance(legacy_chat, dict) \
                else self._new_chat_thread(self.chat_model)
        else:
            self.data.pop("coordinator", None)
        if not isinstance(self.data.get("chat_history"), list):
            legacy_history = self.data.pop("coordinator_history", None)
            self.data["chat_history"] = legacy_history if isinstance(legacy_history, list) else []
        else:
            self.data.pop("coordinator_history", None)
        self._normalize_chat_thread(self.data["chat"])
        self.data["chat_history"] = [
            item for item in self.data["chat_history"] if isinstance(item, dict)
        ]
        for chat_thread in self.data["chat_history"]:
            self._normalize_chat_thread(chat_thread, archived=True)
        for technical_chat in self.data["chats"]:
            # Discard values produced by mislabeling aggregate turn usage as
            # context occupancy. Keep last-turn telemetry separately when new
            # runs provide it.
            technical_chat.pop("context_used_tokens", None)
        self.data["version"] = 5

    @staticmethod
    def _new_chat_thread(model: str = "gpt-5.6-terra") -> dict[str, Any]:
        now = int(time.time())
        return {
            "id": uuid.uuid4().hex,
            "title": "New Chat",
            "model": model if model in CHAT_MODEL_OPTIONS else CHAT_MODEL_OPTIONS[0],
            "created_at": now,
            "updated_at": now,
            "provider_session_id": None,
            "messages": [],
            "context_chars": 0,
            "context_warning": False,
            "warning_announced": False,
        }

    @staticmethod
    def _chat_title(chat_thread: dict[str, Any]) -> str:
        existing = str(chat_thread.get("title") or "").strip()
        if existing and existing != "New Chat":
            return existing[:54]
        first_user = next((
            message for message in chat_thread.get("messages", [])
            if message.get("role") == "user" and str(message.get("content") or "").strip()
        ), None)
        return " ".join(str(first_user.get("content")).split())[:54] \
            if first_user else "New Chat"

    def _normalize_chat_thread(
        self, chat_thread: dict[str, Any], *, archived: bool = False,
    ) -> None:
        chat_thread.setdefault("id", uuid.uuid4().hex)
        chat_thread["title"] = self._chat_title(chat_thread)
        chat_thread.setdefault("created_at", int(time.time()))
        chat_thread.setdefault("updated_at", chat_thread["created_at"])
        chat_thread.setdefault("messages", [])
        model = chat_thread.get("model")
        chat_thread["model"] = model if model in CHAT_MODEL_OPTIONS else self.chat_model
        for message in chat_thread["messages"]:
            if isinstance(message, dict):
                message.pop("active_chat_id", None)
                message.pop("control_action", None)
        chat_thread["context_chars"] = sum(
            len(str(message.get("content") or ""))
            for message in chat_thread["messages"] if isinstance(message, dict)
        )
        chat_thread.setdefault("context_warning", False)
        chat_thread.setdefault("warning_announced", False)
        chat_thread.pop("context_used_tokens", None)
        if archived:
            chat_thread["archived"] = True

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f".{self.path.name}.{uuid.uuid4().hex}.tmp")
        temporary.write_text(
            json.dumps(self.data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
        )
        temporary.chmod(0o600)
        temporary.replace(self.path)

    def list_public(self) -> list[dict[str, Any]]:
        with self.lock:
            return [self.public(chat) for chat in sorted(
                self.data["chats"], key=lambda item: item.get("updated_at", 0), reverse=True,
            )]

    def public(self, chat: dict[str, Any]) -> dict[str, Any]:
        result = deepcopy({
            key: value for key, value in chat.items()
            if key not in {
                "qwen_messages", "context_used_tokens", "context_limit_tokens",
                "context_max_tokens", "context_window_percent", "last_turn_usage",
            }
        })
        context_chars = sum(
            len(str(message.get("content") or "")) for message in chat.get("messages", [])
        )
        result["context_chars"] = context_chars
        usage = _context_usage(
            context_chars, self.technical_warning_chars,
            limit_tokens=chat.get("context_limit_tokens"),
            max_tokens=chat.get("context_max_tokens"),
            allowance_percent=chat.get("context_window_percent"),
        )
        result["context_usage"] = usage
        result["context_percent"] = usage["percent"]
        result["context_status"] = self._context_status(usage["percent"])
        return result

    @staticmethod
    def _context_status(percent: int) -> str:
        if percent >= 100:
            return "limit"
        if percent >= 80:
            return "near_limit"
        return "normal"

    def _chat_public(self, chat_thread: dict[str, Any]) -> dict[str, Any]:
        result = deepcopy({
            key: value for key, value in chat_thread.items()
            if key not in {
                "provider_session_id", "warning_announced", "context_warning",
                "context_used_tokens", "context_limit_tokens", "context_max_tokens",
                "context_window_percent", "last_turn_usage",
            }
        })
        usage = _context_usage(
            int(chat_thread.get("context_chars", 0)), self.chat_warning_chars,
            limit_tokens=chat_thread.get("context_limit_tokens"),
            max_tokens=chat_thread.get("context_max_tokens"),
            allowance_percent=chat_thread.get("context_window_percent"),
        )
        result["context_usage"] = usage
        result["context_percent"] = usage["percent"]
        result["context_status"] = self._context_status(usage["percent"])
        result["pending"] = any(
            message.get("pending") for message in chat_thread.get("messages", [])
        )
        result["context_warning"] = (
            "Near practical limit · archive and start a new Chat"
            if result["context_status"] == "limit" or chat_thread.get("context_warning")
            else "Thread is getting long · consider a new Chat"
            if result["context_status"] == "near_limit" else ""
        )
        return result

    def chat_public(self) -> dict[str, Any]:
        with self.lock:
            return self._chat_public(self.data["chat"])

    def chat_history_public(self) -> list[dict[str, Any]]:
        with self.lock:
            return [self._chat_public(item) for item in sorted(
                self.data["chat_history"],
                key=lambda item: item.get("updated_at", 0), reverse=True,
            )]

    def reset_chat(self, model: str | None = None) -> dict[str, Any]:
        with self.lock:
            current = self.data["chat"]
            if current.get("messages"):
                current["title"] = self._chat_title(current)
                current["archived"] = True
                current["archived_at"] = int(time.time())
                self.data["chat_history"].append(current)
            self.data["chat"] = self._new_chat_thread(model or self.chat_model)
            self.save()
            return self.chat_public()

    def get(self, chat_id: str) -> dict[str, Any]:
        for chat in self.data["chats"]:
            if chat.get("id") == chat_id:
                return chat
        raise KeyError(chat_id)

    def create(
        self, cwd: Path, requested_provider: str, requested_model: str | None = None,
        context_limit_tokens: int | None = None, context_max_tokens: int | None = None,
        context_window_percent: int = 100,
    ) -> dict[str, Any]:
        now = int(time.time())
        chat = {
            "id": uuid.uuid4().hex,
            "title": "New work session",
            "created_at": now,
            "updated_at": now,
            "cwd": str(cwd),
            "requested_provider": requested_provider,
            "requested_model": requested_model,
            "provider": None,
            "model": None,
            "provider_session_id": None,
            "qwen_messages": [],
            "messages": [],
        }
        if context_limit_tokens is not None and context_limit_tokens > 0:
            chat["context_limit_tokens"] = context_limit_tokens
        if context_max_tokens is not None and context_max_tokens > 0:
            chat["context_max_tokens"] = context_max_tokens
            chat["context_window_percent"] = context_window_percent
        with self.lock:
            self.data["chats"].append(chat)
            self.save()
        return self.public(chat)

    def delete(self, chat_id: str) -> None:
        with self.lock:
            before = len(self.data["chats"])
            self.data["chats"] = [chat for chat in self.data["chats"] if chat.get("id") != chat_id]
            if len(self.data["chats"]) == before:
                raise KeyError(chat_id)
            self.save()


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
        store_path = expanded_path(config["web"]["chat_store"])
        default_store_path = expanded_path("~/.local/state/pilferedparrot/chats.json")
        self.store = ChatStore(
            store_path,
            chat_warning_chars=self.chat_context_warning_chars,
            technical_warning_chars=self.technical_context_warning_chars,
            chat_model=str(config["web"].get("chat_model") or "gpt-5.6-terra").strip(),
            legacy_path=expanded_path("~/.local/state/ai-conductor/chats.json")
            if store_path == default_store_path else None,
        )
        self.csrf_token = secrets.token_urlsafe(32)
        self.chat_window_lock = threading.RLock()
        self.chat_window_process: subprocess.Popen[bytes] | None = None
        self.chat_window_profile: Path | None = None
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
            "none", "low", "medium", "high", "xhigh", "max",
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
            if recovered:
                self.store.save()
        return recovered

    def budgets(self) -> dict[str, ProviderBudget]:
        return collect_budgets(self.config)

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
        prompt = str(payload.get("content") or "").strip()
        if not prompt:
            raise ValueError("message cannot be empty")
        active = ActiveRun()
        with self.runs_lock:
            if chat_id in self.runs:
                raise ValueError("this work session is already running")
            with self.store.lock:
                chat = self.store.get(chat_id)
                if any(message.get("pending") for message in chat["messages"]):
                    raise ValueError("this work session is already running")
                provider = str(payload.get("provider") or chat.get("requested_provider")
                               or self.default_provider)
                if provider not in PROVIDERS:
                    raise ValueError("provider must be qwen or codex")
                model_value = payload.get("model") if "model" in payload \
                    else chat.get("requested_model")
                requested_model = self._normalize_model(model_value)
                chat["requested_provider"] = provider
                chat["requested_model"] = requested_model
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
                            f"but the writable project is {requested_cwd}; choose that Project "
                            "folder or add it to codex.additional_write_dirs"
                        )
                    chat["cwd"] = str(requested_cwd)
                now = int(time.time())
                if not chat["messages"]:
                    chat["title"] = " ".join(prompt.split())[:54]
                chat["messages"].append({
                    "id": uuid.uuid4().hex,
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
                }
                chat["messages"].append(pending)
                chat["updated_at"] = now
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
                failed.update({"content": f"PilferedParrot error: {error}", "error": True})
                failed.pop("pending", None)
                self.store.save()
            raise
        return public

    def _run_message(
        self, chat_id: str, pending_id: str, prompt: str, active: ActiveRun,
    ) -> None:
        provider: str | None = None
        budgets: dict[str, ProviderBudget] = {}
        try:
            with self.store.lock:
                chat = self.store.get(chat_id)
                provider = chat["requested_provider"]
                requested_model = chat.get("requested_model")
                current_provider = chat.get("provider")
                current_model = chat.get("model")
                if current_provider and current_model is None and current_provider in PROVIDERS:
                    current_model = effective_model(self.config, current_provider)
                session_id = chat.get("provider_session_id")
                qwen_messages = list(chat.get("qwen_messages") or [])
                cwd = Path(chat["cwd"])

            if provider == "qwen":
                ensure_qwen(self.config)
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
            if provider == "codex" and context_limit is not None:
                run_config["codex"]["context_window_limit_tokens"] = context_limit
            conversation = Conversation(
                provider=provider,
                provider_session_id=session_id if same_session else None,
                qwen_messages=qwen_messages if provider == "qwen" and same_session else [],
            )

            with self.store.lock:
                chat = self.store.get(chat_id)
                pending = self._message(chat, pending_id)
                pending["model"] = selected_model
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
                    self.store.save()

            setattr(active.cancel_event, "_pilferedparrot_progress", report_progress)
            result = capture_dispatch(
                provider, prompt, cwd, conversation, run_config, active.cancel_event,
            )
            if result.exit_code == 0 and result.session_id:
                conversation.provider_session_id = result.session_id
            content = result.text or result.error or f"{provider.title()} exited without a response."
            if result.exit_code and result.error and result.text:
                content += f"\n\n{result.error}"
            with self.store.lock:
                chat = self.store.get(chat_id)
                pending = self._message(chat, pending_id)
                pending.update({
                    "content": content,
                    "provider": provider,
                    "model": selected_model,
                    "exit_code": result.exit_code,
                    "error": bool(result.exit_code),
                })
                if result.exit_code == 0:
                    chat["provider"] = provider
                    chat["model"] = selected_model
                    chat["provider_session_id"] = conversation.provider_session_id
                    chat["qwen_messages"] = conversation.qwen_messages
                    if result.input_tokens is not None and result.output_tokens is not None:
                        chat["last_turn_usage"] = {
                            "input_tokens": result.input_tokens,
                            "output_tokens": result.output_tokens,
                        }
            try:
                append_run(
                    self.config["ledger"], provider=provider, prompt=prompt, cwd=cwd,
                    session_id=conversation.provider_session_id, budgets=budgets,
                    exit_code=result.exit_code, run_id=pending["run_id"],
                    chat_id=chat_id, message_id=pending_id,
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
                    "content": f"PilferedParrot error: {exc}",
                    "provider": provider,
                    "error": True,
                    "exit_code": 1,
                })
        finally:
            with self.runs_lock:
                with self.store.lock:
                    chat = self.store.get(chat_id)
                    pending = self._message(chat, pending_id)
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

    def cancel_message(self, chat_id: str) -> dict[str, Any]:
        with self.runs_lock:
            active = self.runs.get(chat_id)
            with self.store.lock:
                chat = self.store.get(chat_id)
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

    def send_chat_message(self, payload: dict[str, Any]) -> dict[str, Any]:
        content = str(payload.get("content") or "").strip()
        if not content:
            raise ValueError("message cannot be empty")
        if len(content) > 40_000:
            raise ValueError("message is too long")
        active = ActiveRun()
        with self.runs_lock:
            if self.chat_run is not None:
                raise ValueError("Chat is already responding")
            with self.store.lock:
                chat_thread = self.store.data["chat"]
                if any(message.get("pending") for message in chat_thread["messages"]):
                    raise ValueError("Chat is already responding")
                requested_model = self._normalize_chat_model(
                    payload.get("model") if "model" in payload else chat_thread.get("model")
                )
                if requested_model != chat_thread.get("model"):
                    if chat_thread.get("messages"):
                        raise ValueError("start a new chat before changing the chat model")
                    chat_thread["model"] = requested_model
                    # A Codex resume token is model-specific. Never carry it
                    # across a model picker change.
                    chat_thread.pop("provider_session_id", None)
                percent = _context_percent(chat_thread.get(
                    "context_window_percent", context_window_percent(self.config, "codex"),
                ))
                context_max = model_max_context_window(
                    self.config, "codex", requested_model,
                )
                context_limit = model_context_window(
                    self.config, "codex", requested_model, percent,
                )
                if context_limit is not None:
                    chat_thread["context_max_tokens"] = context_max
                    chat_thread["context_limit_tokens"] = context_limit
                    chat_thread["context_window_percent"] = percent
                now = int(time.time())
                if not chat_thread["messages"]:
                    chat_thread["title"] = " ".join(content.split())[:54]
                chat_thread["messages"].append({
                    "id": uuid.uuid4().hex,
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
                    "model": requested_model,
                }
                chat_thread["messages"].append(pending)
                chat_thread["updated_at"] = now
                self.store.save()
                public = self.store.chat_public()
            self.chat_run = active
        thread = threading.Thread(
            target=self._run_chat,
            args=(pending["id"], content, requested_model, active),
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
                    failed.update({"content": f"Chat error: {error}", "error": True})
                    failed.pop("pending", None)
                    chat_thread["updated_at"] = int(time.time())
                    self.store.save()
                if self.chat_run is active:
                    self.chat_run = None
            raise
        return public

    @staticmethod
    def _normalize_chat_model(value: Any) -> str:
        if not isinstance(value, str) or value.strip() not in CHAT_MODEL_OPTIONS:
            raise ValueError("chat model must be gpt-5.6-terra or gpt-5.6-luna")
        return value.strip()

    def _run_chat(
        self, pending_id: str, content: str, model: str, active: ActiveRun,
    ) -> None:
        reply = ""
        try:
            with self.store.lock:
                chat_thread = self.store.data["chat"]
                session_id = chat_thread.get("provider_session_id")
                percent = _context_percent(chat_thread.get(
                    "context_window_percent", context_window_percent(self.config, "codex"),
                ))
            context_limit = model_context_window(self.config, "codex", model, percent)
            run_config = deepcopy(self.config)
            run_config["codex"]["model"] = model
            run_config["codex"]["reasoning_effort"] = self.chat_reasoning_effort
            run_config["codex"]["sandbox"] = "read-only"
            run_config["codex"]["additional_write_dirs"] = []
            if context_limit is not None:
                run_config["codex"]["context_window_limit_tokens"] = context_limit
            conversation = Conversation(provider="codex", provider_session_id=session_id)
            result = capture_dispatch(
                "codex", content, self.default_cwd, conversation,
                run_config, active.cancel_event,
            )
            if result.exit_code:
                raise RuntimeError(result.error or result.text or "Chat model exited without a response")
            reply = result.text.strip()
            with self.store.lock:
                chat_thread = self.store.data["chat"]
                chat_thread["provider_session_id"] = result.session_id \
                    or conversation.provider_session_id
                if result.input_tokens is not None and result.output_tokens is not None:
                    chat_thread["last_turn_usage"] = {
                        "input_tokens": result.input_tokens,
                        "output_tokens": result.output_tokens,
                    }
        except RunCancelled:
            reply = "Stopped."
        except Exception as error:
            reply = f"Chat error: {error}"
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
                        "context_window_percent", context_window_percent(self.config, "codex"),
                    ))
                    context_max = model_max_context_window(self.config, "codex", model)
                    context_limit = model_context_window(
                        self.config, "codex", model, percent,
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

    def reset_chat(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = payload or {}
        model = self._normalize_chat_model(payload["model"]) \
            if "model" in payload else self.chat_model
        with self.runs_lock:
            if self.chat_run is not None:
                raise ValueError("stop Chat before starting a new conversation")
            chat_thread = self.store.reset_chat(model)
            percent = context_window_percent(self.config, "codex")
            context_max = model_max_context_window(self.config, "codex", model)
            context_limit = model_context_window(
                self.config, "codex", model, percent,
            )
            if context_limit is not None:
                with self.store.lock:
                    self.store.data["chat"]["context_limit_tokens"] = context_limit
                    self.store.data["chat"]["context_max_tokens"] = context_max
                    self.store.data["chat"]["context_window_percent"] = percent
                    self.store.save()
                chat_thread = self.store.chat_public()
            return {
                "chat": chat_thread,
                "chat_history": self.store.chat_history_public(),
            }

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
