"""Persistent state and file-backed storage for the web application.

This module deliberately contains no provider or HTTP lifecycle policy.  The
chat store accepts the small presentation callback it needs so that context
accounting remains owned by the web application.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
import uuid
from copy import deepcopy
from pathlib import Path
from typing import Any, Callable, Iterable

from .config import expanded_path


DEFAULT_CHAT_MODEL_OPTIONS = ("gpt-5.6-terra", "gpt-5.6-luna")
_PROVIDER_ID = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")
NOTIFICATION_PERMISSION_STATES = frozenset({
    "unasked", "granted", "denied", "dismissed", "unavailable",
})


def _atomic_json_write(
    path: Path, payload: Any, *, indent: int | None = 2,
    ensure_ascii: bool = True, sort_keys: bool = False,
    separators: tuple[str, str] | None = None,
    trailing_newline: bool = True, fsync: bool = False,
) -> None:
    """Write JSON owner-only through an exclusive temporary file and replace."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(
                payload, handle, ensure_ascii=ensure_ascii, indent=indent,
                sort_keys=sort_keys, separators=separators,
            )
            if trailing_newline:
                handle.write("\n")
            if fsync:
                handle.flush()
                os.fsync(handle.fileno())
        os.replace(temporary, path)
        if os.name == "posix":
            path.chmod(0o600)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def chat_store_path(config: dict[str, Any]) -> Path:
    return expanded_path(config["web"]["chat_store"])


def model_catalog_path(config: dict[str, Any]) -> Path:
    store = chat_store_path(config)
    configured = config["web"].get("model_catalog_store")
    return expanded_path(configured) if configured else store.with_name("models.json")


def legacy_chat_store_path(config: dict[str, Any]) -> Path | None:
    store = chat_store_path(config)
    default = expanded_path("~/.local/state/pilferedparrot/chats.json")
    return expanded_path("~/.local/state/ai-conductor/chats.json") if store == default else None


def dashboard_capability_path(config: dict[str, Any]) -> Path:
    port = int(config["web"]["port"])
    return chat_store_path(config).parent / f"server-{port}.json"


def read_dashboard_capability(url: str, config: dict[str, Any]) -> str | None:
    try:
        path = dashboard_capability_path(config)
        if os.name == "posix" and path.stat().st_mode & 0o077:
            return None
        payload = json.loads(path.read_text(encoding="utf-8"))
        token = payload.get("dashboard_capability") if isinstance(payload, dict) else None
        if payload.get("origin") != url or not isinstance(token, str) or not token:
            return None
        return token
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return None


def write_dashboard_capability(config: dict[str, Any], origin: str, token: str) -> None:
    _atomic_json_write(dashboard_capability_path(config), {
        "origin": origin, "pid": os.getpid(), "dashboard_capability": token,
    }, indent=None, separators=(",", ":"), trailing_newline=False)


def remove_dashboard_capability(config: dict[str, Any], token: str) -> None:
    path = dashboard_capability_path(config)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload.get("dashboard_capability") == token:
            path.unlink()
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        pass


def empty_dashboard_models(provider_ids: Iterable[str]) -> dict[str, Any]:
    return {
        "version": 2,
        "providers": {provider: {} for provider in provider_ids},
        "provider_cards": {},
        "hidden_providers": [],
    }


def load_dashboard_models(path: Path, provider_ids: Iterable[str]) -> dict[str, Any]:
    empty = empty_dashboard_models(provider_ids)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, ValueError, json.JSONDecodeError):
        return empty
    if not isinstance(payload, dict) or not isinstance(payload.get("providers"), dict):
        return empty
    normalized = deepcopy(empty)
    for provider, raw in payload["providers"].items():
        if not isinstance(provider, str) or not _PROVIDER_ID.fullmatch(provider) \
                or not isinstance(raw, dict):
            continue
        models, hidden = raw.get("models"), raw.get("hidden")
        normalized["providers"][provider] = {
            "models": [item for item in models if isinstance(item, dict)]
            if isinstance(models, list) else [],
            "hidden": [item for item in hidden if isinstance(item, str)]
            if isinstance(hidden, list) else [],
        }
    cards = payload.get("provider_cards")
    if isinstance(cards, dict):
        normalized["provider_cards"] = {
            key: deepcopy(value) for key, value in cards.items()
            if isinstance(key, str) and _PROVIDER_ID.fullmatch(key)
            and isinstance(value, dict)
        }
    hidden = payload.get("hidden_providers")
    if isinstance(hidden, list):
        normalized["hidden_providers"] = [value for value in hidden if isinstance(value, str)]
    return normalized


class DashboardModelStore:
    """Normalized dashboard model catalog with an application-visible lock."""

    def __init__(self, path: Path, provider_ids: Iterable[str]):
        self.path = path
        self.lock = threading.RLock()
        self.data = load_dashboard_models(path, provider_ids)

    def save(self, data: dict[str, Any] | None = None) -> None:
        with self.lock:
            if data is not None:
                self.data = data
            _atomic_json_write(
                self.path, self.data, indent=2, sort_keys=True,
                ensure_ascii=True, trailing_newline=True,
            )


class PersistentChatStore:
    """File-backed chat and Chat transcript state.

    Context accounting is injected so presentation policy remains in
    :mod:`pilferedparrot.web`.
    """

    def __init__(
        self, path: Path, *, chat_warning_chars: int = 80_000,
        technical_warning_chars: int = 120_000, legacy_path: Path | None = None,
        chat_model: str = "gpt-5.6-terra",
        context_usage: Callable[..., dict[str, Any]],
        chat_model_options: tuple[str, ...] = DEFAULT_CHAT_MODEL_OPTIONS,
    ):
        self.path = path
        self.legacy_path = legacy_path
        self.lock = threading.RLock()
        self.chat_warning_chars = max(10_000, chat_warning_chars)
        self.technical_warning_chars = max(10_000, technical_warning_chars)
        self.chat_model_options = tuple(chat_model_options)
        self.chat_model = chat_model if chat_model in self.chat_model_options \
            else self.chat_model_options[0]
        self._context_usage = context_usage
        self.data: dict[str, Any] = {"version": 8, "chats": [], "preferences": {}}
        self._load()

    def _load(self) -> None:
        source = self.path
        if not source.exists() and self.legacy_path is not None and self.legacy_path.exists():
            source = self.legacy_path
        if source.exists():
            try:
                payload = json.loads(source.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError) as error:
                raise RuntimeError(
                    f"could not safely load chat history from {source}: {error}"
                ) from error
            if not isinstance(payload, dict) or not isinstance(payload.get("chats"), list):
                raise RuntimeError(f"chat history has an invalid structure: {source}")
            version = payload.get("version", 1)
            if isinstance(version, bool) or not isinstance(version, int) or version > 8:
                raise RuntimeError(
                    f"chat history uses an unsupported format version: {source}"
                )
            self.data = payload
        chats = self.data["chats"]
        if not all(isinstance(chat, dict) for chat in chats):
            raise RuntimeError(f"chat history contains an invalid work session: {source}")
        seen_chat_ids: set[str] = set()
        # Historical provider and auto-routed chats remain readable. Only the
        # retired auto-routing marker is migrated to a direct Codex selection.
        for chat in chats:
            for field_name in (
                "window_id", "title", "cwd", "requested_provider", "requested_model",
                "provider", "model", "provider_session_id",
            ):
                value = chat.get(field_name)
                if value is not None and not isinstance(value, str):
                    raise RuntimeError(
                        f"chat history contains an invalid {field_name}: {source}"
                    )
            messages = chat.get("messages", [])
            if not isinstance(messages, list) or not all(
                isinstance(message, dict) for message in messages
            ):
                raise RuntimeError(f"chat history contains invalid messages: {source}")
            chat["messages"] = messages
            now = int(time.time())
            created = chat.get("created_at")
            chat["created_at"] = int(created) if isinstance(created, (int, float)) \
                and not isinstance(created, bool) else now
            updated = chat.get("updated_at")
            chat["updated_at"] = int(updated) if isinstance(updated, (int, float)) \
                and not isinstance(updated, bool) else chat["created_at"]
            order = chat.get("last_used_order")
            if order is not None and (isinstance(order, bool) or not isinstance(order, int) or order < 1):
                chat.pop("last_used_order", None)
            raw_id = chat.get("id")
            chat_id = raw_id if isinstance(raw_id, str) and re.fullmatch(
                r"[A-Za-z0-9_-]{1,128}", raw_id,
            ) and raw_id not in seen_chat_ids else uuid.uuid4().hex
            chat["id"] = chat_id
            seen_chat_ids.add(chat_id)
            # Chats created before per-window isolation belong to the original
            # dashboard. New provider windows receive their own opaque id.
            chat.setdefault("window_id", "main")
            if "provider_messages" not in chat:
                legacy_messages = chat.pop("qwen_messages", [])
                if not isinstance(legacy_messages, list):
                    raise RuntimeError(f"chat history contains invalid provider state: {source}")
                chat["provider_messages"] = legacy_messages \
                    if isinstance(legacy_messages, list) else []
            else:
                chat.pop("qwen_messages", None)
            if not isinstance(chat["provider_messages"], list) or not all(
                isinstance(message, dict) for message in chat["provider_messages"]
            ):
                raise RuntimeError(f"chat history contains invalid provider state: {source}")
            if chat.get("requested_provider") in {None, "", "auto", "technical"}:
                chat["requested_provider"] = "codex"
                chat["requested_model"] = None
            provider = chat.get("requested_provider") or chat.get("provider")
            # Provider windows used to receive an ephemeral UUID, making their
            # session list vanish after the window closed. Migrate those chats
            # into a stable per-provider history scope. The main dashboard
            # remains its own scope.
            if chat.get("window_id") != "main" and isinstance(provider, str) and provider:
                chat["window_id"] = f"provider-{provider}"
            if chat.get("title") in {"New conversation", "New technical activity"} \
                    and not chat.get("messages"):
                chat["title"] = "New work session"
        # Version 5 renames the old interpreter-centric coordinator fields to
        # plain Chat fields while preserving every existing transcript.
        if "chat" in self.data and not isinstance(self.data["chat"], dict):
            raise RuntimeError(f"chat history contains an invalid Chat thread: {source}")
        if not isinstance(self.data.get("chat"), dict):
            legacy_chat = self.data.pop("coordinator", None)
            self.data["chat"] = legacy_chat if isinstance(legacy_chat, dict) \
                else self._new_chat_thread(self.chat_model)
        else:
            self.data.pop("coordinator", None)
        if "chat_history" in self.data and not isinstance(self.data["chat_history"], list):
            raise RuntimeError(f"chat history contains an invalid Chat archive: {source}")
        if not isinstance(self.data.get("chat_history"), list):
            legacy_history = self.data.pop("coordinator_history", None)
            self.data["chat_history"] = legacy_history if isinstance(legacy_history, list) else []
        else:
            self.data.pop("coordinator_history", None)
        self._normalize_chat_thread(self.data["chat"])
        if not all(isinstance(item, dict) for item in self.data["chat_history"]):
            raise RuntimeError(f"chat history contains an invalid archived thread: {source}")
        for chat_thread in self.data["chat_history"]:
            self._normalize_chat_thread(chat_thread, archived=True)
        for technical_chat in self.data["chats"]:
            # Discard values produced by mislabeling aggregate turn usage as
            # context occupancy. Keep last-turn telemetry separately when new
            # runs provide it.
            technical_chat.pop("context_used_tokens", None)
        raw_preferences = self.data.get("preferences")
        preferences = raw_preferences if isinstance(raw_preferences, dict) else {}
        work_models = preferences.get("work_models")
        work_context = preferences.get("work_context_window_percent")
        self.data["preferences"] = {
            "work_models": {
                str(provider): model.strip()
                for provider, model in work_models.items()
                if isinstance(provider, str) and isinstance(model, str) and model.strip()
            } if isinstance(work_models, dict) else {},
            "work_context_window_percent": {
                str(provider): percent
                for provider, percent in work_context.items()
                if isinstance(provider, str) and isinstance(percent, int)
                and not isinstance(percent, bool) and 1 <= percent <= 100
            } if isinstance(work_context, dict) else {},
        }
        chat_model = preferences.get("chat_model")
        if isinstance(chat_model, str) and chat_model in self.chat_model_options:
            self.data["preferences"]["chat_model"] = chat_model
        chat_context = preferences.get("chat_context_window_percent")
        if isinstance(chat_context, int) and not isinstance(chat_context, bool) \
                and 1 <= chat_context <= 100:
            self.data["preferences"]["chat_context_window_percent"] = chat_context
        notification_permission = preferences.get("notification_permission", "unasked")
        if notification_permission in NOTIFICATION_PERMISSION_STATES:
            self.data["preferences"]["notification_permission"] = notification_permission
        else:
            self.data["preferences"]["notification_permission"] = "unasked"
        self.data["version"] = 8

    def preferences_public(self) -> dict[str, Any]:
        with self.lock:
            return deepcopy(self.data["preferences"])

    def set_notification_permission(self, decision: Any) -> dict[str, Any]:
        """Persist a browser notification decision using the shared preference store."""
        if decision not in NOTIFICATION_PERMISSION_STATES:
            raise ValueError("notification permission decision is invalid")
        with self.lock:
            self.data["preferences"]["notification_permission"] = decision
            self.save()
            return self.preferences_public()

    @staticmethod
    def _new_chat_thread(
        model: str | None = "gpt-5.6-terra", provider: str = "codex",
        cwd: str | None = None,
    ) -> dict[str, Any]:
        now = int(time.time())
        return {
            "id": uuid.uuid4().hex,
            "title": "New Chat",
            "provider": provider,
            "model": model,
            "cwd": cwd,
            "created_at": now,
            "updated_at": now,
            "provider_session_id": None,
            "provider_messages": [],
            "messages": [],
            "context_chars": 0,
            "context_warning": False,
            "warning_announced": False,
            "reasoning_effort": None,
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
        raw_id = chat_thread.get("id")
        if not isinstance(raw_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", raw_id):
            chat_thread["id"] = uuid.uuid4().hex
        for field_name in ("title", "provider", "model", "cwd", "provider_session_id", "reasoning_effort"):
            value = chat_thread.get(field_name)
            if value is not None and not isinstance(value, str):
                raise RuntimeError(f"Chat thread contains an invalid {field_name}")
        chat_thread["title"] = self._chat_title(chat_thread)
        now = int(time.time())
        created = chat_thread.get("created_at")
        chat_thread["created_at"] = int(created) if isinstance(created, (int, float)) \
            and not isinstance(created, bool) else now
        updated = chat_thread.get("updated_at")
        chat_thread["updated_at"] = int(updated) if isinstance(updated, (int, float)) \
            and not isinstance(updated, bool) else chat_thread["created_at"]
        messages = chat_thread.get("messages", [])
        if not isinstance(messages, list) or not all(
            isinstance(message, dict) for message in messages
        ):
            raise RuntimeError("Chat thread contains invalid messages")
        chat_thread["messages"] = messages
        provider_messages = chat_thread.get("provider_messages", [])
        if not isinstance(provider_messages, list) or not all(
            isinstance(message, dict) for message in provider_messages
        ):
            raise RuntimeError("Chat thread contains invalid provider state")
        chat_thread["provider_messages"] = provider_messages
        chat_thread["provider"] = chat_thread.get("provider") or "codex"
        chat_thread.setdefault("reasoning_effort", None)
        if chat_thread.get("model") is None and chat_thread["provider"] == "codex":
            chat_thread["model"] = self.chat_model
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
        with self.lock:
            _atomic_json_write(
                self.path, self.data, ensure_ascii=False, indent=2,
                trailing_newline=True, fsync=True,
            )

    def list_public(
        self, window_id: str | None = None, provider: str | None = None,
    ) -> list[dict[str, Any]]:
        with self.lock:
            return [self.public(chat) for chat in sorted(
                (
                    item for item in self.data["chats"]
                    if window_id is None or item.get("window_id", "main") == window_id
                    if provider is None or (
                        item.get("requested_provider") or item.get("provider")
                    ) == provider
                ),
                key=lambda item: item.get("updated_at", 0), reverse=True,
            )]

    def public(self, chat: dict[str, Any]) -> dict[str, Any]:
        result = deepcopy({
            key: value for key, value in chat.items()
            if key not in {
                "provider_messages", "qwen_messages", "context_used_tokens", "context_limit_tokens",
                "context_max_tokens", "context_window_percent", "last_turn_usage",
                "live_context_usage", "context_overhead_tokens",
                "output_reservation_tokens",
            }
        })
        context_chars = sum(
            len(str(message.get("content") or "")) for message in chat.get("messages", [])
        )
        # A newly created work session is a user-visible context boundary. Keep
        # its meter at zero until the first request starts instead of making the
        # fresh session look partly consumed by estimated provider overhead.
        fresh_session = not chat.get("messages") and not chat.get("live_context_usage")
        result["context_chars"] = context_chars
        usage = self._context_usage(
            context_chars, self.technical_warning_chars,
            limit_tokens=chat.get("context_limit_tokens"),
            max_tokens=chat.get("context_max_tokens"),
            allowance_percent=chat.get("context_window_percent"),
            live_input_tokens=(chat.get("live_context_usage") or {}).get("input_tokens"),
            live_output_tokens=(chat.get("live_context_usage") or {}).get("output_tokens"),
            overhead_tokens=0 if fresh_session else chat.get("context_overhead_tokens", 0),
            output_reservation_tokens=(
                0 if fresh_session else chat.get("output_reservation_tokens", 0)
            ),
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
                "provider_session_id", "provider_messages", "warning_announced", "context_warning",
                "context_used_tokens", "context_limit_tokens", "context_max_tokens",
                "context_window_percent", "last_turn_usage",
                "live_context_usage", "context_overhead_tokens",
                "output_reservation_tokens",
            }
        })
        usage = self._context_usage(
            int(chat_thread.get("context_chars", 0)), self.chat_warning_chars,
            limit_tokens=chat_thread.get("context_limit_tokens"),
            max_tokens=chat_thread.get("context_max_tokens"),
            allowance_percent=chat_thread.get("context_window_percent"),
            live_input_tokens=(chat_thread.get("live_context_usage") or {}).get("input_tokens"),
            live_output_tokens=(chat_thread.get("live_context_usage") or {}).get("output_tokens"),
            overhead_tokens=chat_thread.get("context_overhead_tokens", 0),
            output_reservation_tokens=chat_thread.get("output_reservation_tokens", 0),
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

    def chat_history_public(self, provider: str | None = None) -> list[dict[str, Any]]:
        with self.lock:
            return [self._chat_public(item) for item in sorted(
                (
                    item for item in self.data["chat_history"]
                    if provider is None or item.get("provider") == provider
                ),
                key=lambda item: item.get("updated_at", 0), reverse=True,
            )]

    def reset_chat(
        self, model: str | None = None, provider: str | None = None,
        cwd: str | None = None, reasoning_effort: str | None = None,
    ) -> dict[str, Any]:
        with self.lock:
            current = self.data["chat"]
            if current.get("messages"):
                current["title"] = self._chat_title(current)
                current["archived"] = True
                current["archived_at"] = int(time.time())
                self.data["chat_history"].append(current)
            self.data["chat"] = self._new_chat_thread(
                model, provider or str(current.get("provider") or "codex"),
                cwd or current.get("cwd"),
            )
            self.data["chat"]["reasoning_effort"] = reasoning_effort
            self.save()
            return self.chat_public()

    def get(self, chat_id: str) -> dict[str, Any]:
        for chat in self.data["chats"]:
            if chat.get("id") == chat_id:
                return chat
        raise KeyError(chat_id)

    def latest_work_defaults(
        self, provider: str, window_id: str = "main",
    ) -> tuple[str | None, str | None] | None:
        """Return the latest session's requested model and reasoning choice.

        Work sessions are scoped to a provider window.  Keep the explicit
        ``None`` reasoning value: it represents the user's Default choice and
        must not be replaced by an older non-default setting.
        """
        with self.lock:
            candidates = [
                chat for chat in self.data["chats"]
                if chat.get("window_id", "main") == window_id
                and not chat.get("harness_parent")
                and (chat.get("requested_provider") or chat.get("provider")) == provider
            ]
            if not candidates:
                return None
            # Recency is independent from display/activity timestamps.  A
            # background response completion can update ``updated_at`` but
            # cannot silently replace the model selection the user last used.
            ordered = [
                (index, chat) for index, chat in enumerate(candidates)
                if isinstance(chat.get("last_used_order"), int)
                and not isinstance(chat.get("last_used_order"), bool)
                and chat["last_used_order"] > 0
            ]
            latest = max(
                ordered,
                key=lambda pair: (pair[1]["last_used_order"], pair[0]),
            )[1] if ordered else max(enumerate(candidates), key=lambda pair: (
                pair[1].get("updated_at", 0), pair[0],
            ))[1]
            model = latest.get("requested_model") or latest.get("model")
            return (model if isinstance(model, str) and model else None,
                    latest.get("reasoning_effort") if isinstance(
                        latest.get("reasoning_effort"), str
                    ) else None)

    def create(
        self, cwd: Path, requested_provider: str, requested_model: str | None = None,
        context_limit_tokens: int | None = None, context_max_tokens: int | None = None,
        context_window_percent: int = 100, context_overhead_tokens: int = 0,
        output_reservation_tokens: int = 0, window_id: str = "main",
        reasoning_effort: str | None = None,
    ) -> dict[str, Any]:
        now = int(time.time())
        chat = {
            "id": uuid.uuid4().hex,
            "window_id": window_id,
            "title": "New work session",
            "created_at": now,
            "updated_at": now,
            "cwd": str(cwd),
            "requested_provider": requested_provider,
            "requested_model": requested_model,
            "reasoning_effort": reasoning_effort,
            "provider": None,
            "model": None,
            "provider_session_id": None,
            "provider_messages": [],
            "messages": [],
            "context_overhead_tokens": max(0, int(context_overhead_tokens)),
            "output_reservation_tokens": max(0, int(output_reservation_tokens)),
        }
        if context_limit_tokens is not None and context_limit_tokens > 0:
            chat["context_limit_tokens"] = context_limit_tokens
        if context_max_tokens is not None and context_max_tokens > 0:
            chat["context_max_tokens"] = context_max_tokens
            chat["context_window_percent"] = context_window_percent
        with self.lock:
            self.data["chats"].append(chat)
            self.mark_used(chat)
            self.save()
        return self.public(chat)

    def mark_used(self, chat: dict[str, Any]) -> None:
        """Persist selection order while holding ``lock`` without changing timestamps."""
        latest_order = max(
            (
                item.get("last_used_order", 0)
                for item in self.data["chats"]
                if isinstance(item.get("last_used_order", 0), int)
                and not isinstance(item.get("last_used_order", 0), bool)
            ),
            default=0,
        )
        chat["last_used_order"] = latest_order + 1

    def delete(self, chat_id: str) -> None:
        with self.lock:
            before = len(self.data["chats"])
            self.data["chats"] = [chat for chat in self.data["chats"] if chat.get("id") != chat_id]
            if len(self.data["chats"]) == before:
                raise KeyError(chat_id)
            self.save()
