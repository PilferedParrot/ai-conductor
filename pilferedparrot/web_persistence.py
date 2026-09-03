"""Persistent chat-state storage for the web application."""

from __future__ import annotations

import json
import threading
import time
import uuid
from copy import deepcopy
from pathlib import Path
from typing import Any, Callable

from .config import expanded_path
from .model import PROVIDERS


CHAT_MODEL_OPTIONS = ("gpt-5.6-terra", "gpt-5.6-luna")


def chat_store_path(config: dict[str, Any]) -> Path:
    return expanded_path(config["web"]["chat_store"])


def legacy_chat_store_path(config: dict[str, Any]) -> Path | None:
    store = chat_store_path(config)
    default = expanded_path("~/.local/state/pilferedparrot/chats.json")
    return expanded_path("~/.local/state/ai-conductor/chats.json") \
        if store == default else None


class PersistentChatStore:
    def __init__(
        self, path: Path, *, chat_warning_chars: int = 80_000,
        technical_warning_chars: int = 120_000, legacy_path: Path | None = None,
        chat_model: str = "gpt-5.6-terra",
        context_usage: Callable[..., dict[str, Any]],
    ):
        self.path = path
        self.legacy_path = legacy_path
        self.lock = threading.RLock()
        self.chat_warning_chars = max(10_000, chat_warning_chars)
        self.technical_warning_chars = max(10_000, technical_warning_chars)
        self.chat_model = chat_model if chat_model in CHAT_MODEL_OPTIONS else CHAT_MODEL_OPTIONS[0]
        self._context_usage = context_usage
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
        usage = self._context_usage(
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
        usage = self._context_usage(
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


