"""Provider/run orchestration used by the web application.

This module deliberately has no dependency on :mod:`pilferedparrot.web`.
The application supplies the dispatch and optional provider bootstrap
functions, which keeps the execution policy testable without an HTTP server.
"""
from __future__ import annotations

import copy
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .budgets import collect_budgets
from .config import (
    codex_additional_write_dirs, context_window_percent, effective_model,
    model_context_window, model_max_context_window,
)
from .dispatch import RunCancelled, RunResult, capture_dispatch
from .ledger import append_run
from .model import PROVIDERS, Conversation, ProviderBudget
from .qwen import ensure_qwen


@dataclass
class ActiveRun:
    """Cancellation and lifecycle state shared by a worker thread."""

    cancel_event: threading.Event = field(default_factory=threading.Event)
    thread: threading.Thread | None = None
    last_checkpoint: float = 0.0


@dataclass(frozen=True)
class PreparedRun:
    """Immutable provider invocation inputs produced before a worker starts."""

    provider: str
    model: str | None
    cwd: Path
    config: dict[str, Any]
    conversation: Conversation
    context_limit: int | None = None
    same_session: bool = False


Dispatch = Callable[[str, str, Path, Conversation, dict[str, Any], threading.Event], RunResult]
EnsureProvider = Callable[..., Any]
ThreadFactory = Callable[..., threading.Thread]


class ProviderRunOrchestrator:
    """Prepare and execute one provider turn with explicit app dependencies.

    ``config`` is never mutated.  The supplied dispatch callable retains the
    web application's existing seam (and is easy to replace in tests).
    ``mode`` is ``"technical"`` for work sessions and ``"chat"`` for the
    read-only Chat window.
    """

    def __init__(
        self,
        config: dict[str, Any],
        *,
        store: Any | None = None,
        default_cwd: Path | None = None,
        default_provider: str = "codex",
        chat_model: str = "gpt-5.6-terra",
        chat_model_options: tuple[str, ...] = ("gpt-5.6-terra", "gpt-5.6-luna"),
        chat_reasoning_effort: str = "low",
        chat_context_warning_chars: int = 80_000,
        dispatch: Dispatch | None = None,
        ensure_provider: EnsureProvider | None = None,
        budget_collector: Callable[[dict[str, Any]], dict[str, ProviderBudget]] | None = None,
        ledger_writer: Callable[..., None] | None = None,
        thread_factory: ThreadFactory | None = None,
        effective_model_for: Callable[[dict[str, Any], str], str | None] | None = None,
        context_percent_for: Callable[[dict[str, Any], str], int] | None = None,
        context_max_for: Callable[[dict[str, Any], str, str | None], int | None] | None = None,
        context_limit_for: Callable[
            [dict[str, Any], str, str | None, int | None], int | None
        ] | None = None,
        context_usage: Callable[..., dict[str, Any]] | None = None,
        context_percent: Callable[[Any], int] | None = None,
        project_directory: Callable[[Any], Path] | None = None,
        migrate_project_path: Callable[[Any], Path] | None = None,
        validate_workspace: Callable[[str, Path, dict[str, Any]], None] | None = None,
        outside_write_target: Callable[[str, tuple[Path, ...]], Path | None] | None = None,
    ) -> None:
        self.config = config
        self.store = store
        self.default_cwd = Path(default_cwd) if default_cwd is not None else Path.cwd()
        self.default_provider = default_provider
        self.chat_model = chat_model
        self.chat_model_options = chat_model_options
        self.chat_reasoning_effort = chat_reasoning_effort
        self.chat_context_warning_chars = chat_context_warning_chars
        self.dispatch = dispatch
        self.ensure_provider = ensure_provider
        self.budget_collector = budget_collector
        self.ledger_writer = ledger_writer
        self.thread_factory = thread_factory
        self.effective_model_for = effective_model_for
        self.context_percent_for = context_percent_for
        self.context_max_for = context_max_for
        self.context_limit_for = context_limit_for
        self.context_usage = context_usage
        self.context_percent = context_percent
        self.project_directory = project_directory
        self.migrate_project_path = migrate_project_path
        self.validate_workspace = validate_workspace
        self.outside_write_target = outside_write_target
        self.runs_lock = threading.RLock()
        self.runs: dict[str, ActiveRun] = {}
        self.chat_run: ActiveRun | None = None

    def _require_store(self) -> Any:
        if self.store is None:
            raise RuntimeError("provider run persistence is not configured")
        return self.store

    def _effective_model(self, provider: str) -> str | None:
        resolver = self.effective_model_for or effective_model
        return resolver(self.config, provider)

    def _context_percent_for(self, provider: str) -> int:
        resolver = self.context_percent_for or context_window_percent
        return resolver(self.config, provider)

    def _context_max(self, provider: str, model: str | None) -> int | None:
        resolver = self.context_max_for or model_max_context_window
        return resolver(self.config, provider, model)

    def _context_limit(
        self, provider: str, model: str | None, percent: int | None = None,
    ) -> int | None:
        resolver = self.context_limit_for or model_context_window
        return resolver(self.config, provider, model, percent)

    def _context_percent_value(self, value: Any) -> int:
        if self.context_percent is not None:
            return self.context_percent(value)
        if isinstance(value, bool):
            raise ValueError("context window percent must be between 1 and 100")
        try:
            percent = int(value)
        except (TypeError, ValueError) as error:
            raise ValueError("context window percent must be between 1 and 100") from error
        if percent < 1 or percent > 100:
            raise ValueError("context window percent must be between 1 and 100")
        return percent

    def _configured_model(self, provider: str) -> str | None:
        value = self.config.get(provider, {}).get("model")
        return str(value) if value else None

    def prepare(
        self,
        provider: str,
        cwd: Path,
        *,
        model: str | None = None,
        session_id: str | None = None,
        provider_messages: list[dict[str, Any]] | None = None,
        current_provider: str | None = None,
        current_model: str | None = None,
        context_limit_tokens: int | None = None,
        mode: str = "technical",
    ) -> PreparedRun:
        """Build an isolated config and correctly resumable conversation."""
        selected_model = model or self._configured_model(provider)
        same_session = provider == current_provider and selected_model == current_model
        run_config = copy.deepcopy(self.config)
        provider_config = run_config.setdefault(provider, {})
        if selected_model:
            provider_config["model"] = selected_model
        if mode not in {"technical", "chat"}:
            raise ValueError("run mode must be technical or chat")
        if mode == "chat":
            if provider == "codex":
                provider_config["reasoning_effort"] = str(
                    self.config.get("web", {}).get("chat_reasoning_effort") or "low"
                ).strip().lower()
                provider_config["sandbox"] = "read-only"
                provider_config["additional_write_dirs"] = []
            elif provider == "claude":
                provider_config["permission_mode"] = "plan"
            elif provider == "gemini":
                provider_config["approval_mode"] = "plan"
            else:
                provider_config["read_only"] = True
                provider_config["additional_dirs"] = []
        if provider == "codex" and context_limit_tokens is not None:
            provider_config["context_window_limit_tokens"] = context_limit_tokens
        compatible = provider == "qwen" or provider_config.get("adapter") == "openai_compatible"
        conversation = Conversation(
            provider=provider,
            provider_session_id=session_id if same_session else None,
        )
        # HEAD calls this legacy-compatible transcript field qwen_messages;
        # newer callers may expose the provider-neutral name messages.
        transcript = copy.deepcopy(provider_messages or []) if compatible and same_session else []
        if hasattr(conversation, "messages"):
            conversation.messages = transcript
        else:
            conversation.qwen_messages = transcript
        return PreparedRun(
            provider, selected_model, Path(cwd), run_config, conversation,
            context_limit_tokens, same_session,
        )

    def execute(
        self,
        prompt: str,
        prepared: PreparedRun,
        active: ActiveRun | None = None,
        *,
        on_progress: Callable[[str, str], None] | None = None,
    ) -> RunResult:
        """Run a prepared turn, normalizing progress and cancellation wiring."""
        active = active or ActiveRun()
        if self.ensure_provider is not None and prepared.provider == "qwen":
            self.ensure_provider(self.config)
        if on_progress is not None:
            setattr(active.cancel_event, "_pilferedparrot_progress", on_progress)
        dispatcher = self.dispatch or capture_dispatch
        return dispatcher(
            prepared.provider, prompt, prepared.cwd, prepared.conversation,
            prepared.config, active.cancel_event,
        )

    @staticmethod
    def apply_session(result: RunResult, prepared: PreparedRun) -> None:
        """Copy provider session and usage telemetry back into the conversation."""
        if result.exit_code == 0 and result.session_id:
            prepared.conversation.provider_session_id = result.session_id
        usage = prepared.conversation.token_usage
        for source, target in (("input_tokens", "input_tokens"), ("output_tokens", "output_tokens")):
            value = getattr(result, source, None)
            if value is not None:
                usage[target] = int(value)
        live_window = getattr(result, "live_context_window_tokens", None)
        if live_window is not None:
            usage["context_window_tokens"] = int(live_window)

    def recover_interrupted(self) -> int:
        """Convert persisted in-flight responses into retryable terminal states."""
        store = self._require_store()
        recovered = 0
        with store.lock:
            for chat in store.data["chats"]:
                chat_recovered = False
                for message in chat.get("messages", []):
                    if not message.get("pending"):
                        continue
                    message.update({
                        "content": (
                            "PilferedParrot restarted before this response finished. "
                            "You can retry."
                        ),
                        "error": True,
                        "interrupted": True,
                    })
                    message.pop("pending", None)
                    message.pop("cancel_requested", None)
                    recovered += 1
                    chat_recovered = True
                if chat_recovered:
                    chat["updated_at"] = int(time.time())
            chat_thread = store.data["chat"]
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
                store.save()
        return recovered

    def budgets(self) -> dict[str, ProviderBudget]:
        collector = self.budget_collector or collect_budgets
        return collector(self.config)

    @staticmethod
    def normalize_model(value: Any) -> str | None:
        if value is None or value == "":
            return None
        if not isinstance(value, str):
            raise ValueError("model must be a string")
        model = value.strip()
        if not model or len(model) > 128 or any(ord(char) < 32 for char in model):
            raise ValueError("model must be a valid model ID")
        return model

    def normalize_chat_model(self, value: Any) -> str:
        if not isinstance(value, str) or value.strip() not in self.chat_model_options:
            choices = " or ".join(self.chat_model_options)
            raise ValueError(f"chat model must be {choices}")
        return value.strip()

    @staticmethod
    def _message(chat: dict[str, Any], message_id: str) -> dict[str, Any]:
        for message in chat["messages"]:
            if message.get("id") == message_id:
                return message
        raise KeyError(message_id)

    def send_message(self, chat_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Validate, persist, and start a technical provider turn."""
        store = self._require_store()
        prompt = str(payload.get("content") or "").strip()
        if not prompt:
            raise ValueError("message cannot be empty")
        active = ActiveRun()
        with self.runs_lock:
            if chat_id in self.runs:
                raise ValueError("this work session is already running")
            with store.lock:
                chat = store.get(chat_id)
                if any(message.get("pending") for message in chat["messages"]):
                    raise ValueError("this work session is already running")
                provider = str(
                    payload.get("provider") or chat.get("requested_provider")
                    or self.default_provider
                )
                if provider not in PROVIDERS:
                    raise ValueError("provider must be qwen or codex")
                model_value = (
                    payload.get("model") if "model" in payload
                    else chat.get("requested_model")
                )
                requested_model = self.normalize_model(model_value)
                chat["requested_provider"] = provider
                chat["requested_model"] = requested_model
                if not chat["messages"]:
                    cwd_value: Any = payload.get("cwd") or chat["cwd"]
                    if self.migrate_project_path is not None:
                        cwd_value = self.migrate_project_path(cwd_value)
                    requested_cwd = (
                        self.project_directory(cwd_value)
                        if self.project_directory is not None else Path(str(cwd_value)).resolve()
                    )
                    if self.validate_workspace is not None:
                        self.validate_workspace(provider, requested_cwd, self.config)
                    writable_roots = (requested_cwd,)
                    if provider == "codex":
                        writable_roots += codex_additional_write_dirs(self.config)
                    outside_target = (
                        self.outside_write_target(prompt, writable_roots)
                        if self.outside_write_target is not None else None
                    )
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
                store.save()
                public = store.public(chat)
            self.runs[chat_id] = active
        factory = self.thread_factory or threading.Thread
        thread = factory(
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
            with store.lock:
                chat = store.get(chat_id)
                failed = self._message(chat, pending["id"])
                failed.update({"content": f"PilferedParrot error: {error}", "error": True})
                failed.pop("pending", None)
                store.save()
            raise
        return public

    def _run_message(
        self, chat_id: str, pending_id: str, prompt: str, active: ActiveRun,
    ) -> None:
        store = self._require_store()
        provider: str | None = None
        budgets: dict[str, ProviderBudget] = {}
        try:
            with store.lock:
                chat = store.get(chat_id)
                provider = chat["requested_provider"]
                requested_model = chat.get("requested_model")
                current_provider = chat.get("provider")
                current_model = chat.get("model")
                if current_provider and current_model is None and current_provider in PROVIDERS:
                    current_model = self._effective_model(current_provider)
                session_id = chat.get("provider_session_id")
                provider_messages = list(chat.get("qwen_messages") or [])
                cwd = Path(chat["cwd"])
                percent = self._context_percent_value(chat.get(
                    "context_window_percent", self._context_percent_for(provider),
                ))

            selected_model = requested_model or self._effective_model(provider)
            context_max = self._context_max(provider, selected_model)
            context_limit = self._context_limit(provider, selected_model, percent)
            prepared = self.prepare(
                provider,
                cwd,
                model=selected_model,
                session_id=session_id,
                provider_messages=provider_messages,
                current_provider=current_provider,
                current_model=current_model,
                context_limit_tokens=context_limit,
            )

            with store.lock:
                chat = store.get(chat_id)
                pending = self._message(chat, pending_id)
                pending["model"] = selected_model
                if not prepared.same_session:
                    chat.pop("last_turn_usage", None)
                if context_limit is not None:
                    chat["context_limit_tokens"] = context_limit
                    chat["context_max_tokens"] = context_max
                    chat["context_window_percent"] = percent
                else:
                    chat.pop("context_limit_tokens", None)
                    chat.pop("context_max_tokens", None)
                store.save()

            def report_progress(kind: str, text: str) -> None:
                rendered = str(text).strip()
                if not rendered:
                    return
                rendered = rendered[:4_000]
                with store.lock:
                    pending_message = self._message(store.get(chat_id), pending_id)
                    activity = pending_message.setdefault("activity", [])
                    activity.append({
                        "kind": kind,
                        "content": rendered,
                        "created_at": int(time.time()),
                    })
                    if len(activity) > 100:
                        del activity[:-100]
                    store.save()

            result = self.execute(prompt, prepared, active, on_progress=report_progress)
            self.apply_session(result, prepared)
            content = result.text or result.error or f"{provider.title()} exited without a response."
            if result.exit_code and result.error and result.text:
                content += f"\n\n{result.error}"
            with store.lock:
                chat = store.get(chat_id)
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
                    chat["provider_session_id"] = prepared.conversation.provider_session_id
                    chat["qwen_messages"] = prepared.conversation.qwen_messages
                    if result.input_tokens is not None and result.output_tokens is not None:
                        chat["last_turn_usage"] = {
                            "input_tokens": result.input_tokens,
                            "output_tokens": result.output_tokens,
                        }
            writer = self.ledger_writer or append_run
            try:
                writer(
                    self.config["ledger"], provider=provider, prompt=prompt, cwd=cwd,
                    session_id=prepared.conversation.provider_session_id, budgets=budgets,
                    exit_code=result.exit_code, run_id=pending["run_id"],
                    chat_id=chat_id, message_id=pending_id,
                )
            except OSError as error:
                print(f"[web] could not append run ledger: {error}")
        except RunCancelled:
            with store.lock:
                pending = self._message(store.get(chat_id), pending_id)
                pending.update({"content": "Cancelled.", "cancelled": True, "exit_code": 130})
        except Exception as error:
            with store.lock:
                pending = self._message(store.get(chat_id), pending_id)
                pending.update({
                    "content": f"PilferedParrot error: {error}",
                    "provider": provider,
                    "error": True,
                    "exit_code": 1,
                })
        finally:
            with self.runs_lock:
                with store.lock:
                    chat = store.get(chat_id)
                    pending = self._message(chat, pending_id)
                    pending.pop("pending", None)
                    pending.pop("cancel_requested", None)
                    chat["updated_at"] = int(time.time())
                    store.save()
                    if self.runs.get(chat_id) is active:
                        self.runs.pop(chat_id, None)

    def cancel_message(self, chat_id: str) -> dict[str, Any]:
        store = self._require_store()
        with self.runs_lock:
            active = self.runs.get(chat_id)
            with store.lock:
                chat = store.get(chat_id)
                pending = next((item for item in chat["messages"] if item.get("pending")), None)
                if pending is None:
                    raise ValueError("this work session is not running")
                pending["cancel_requested"] = True
                store.save()
                public = store.public(chat)
            if active is not None:
                active.cancel_event.set()
            else:
                with store.lock:
                    chat = store.get(chat_id)
                    pending = next(item for item in chat["messages"] if item.get("pending"))
                    pending.update({"content": "Cancelled.", "cancelled": True, "exit_code": 130})
                    pending.pop("pending", None)
                    pending.pop("cancel_requested", None)
                    chat["updated_at"] = int(time.time())
                    store.save()
                    public = store.public(chat)
        return public

    def send_chat_message(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Persist and start one read-only Chat provider turn."""
        store = self._require_store()
        content = str(payload.get("content") or "").strip()
        if not content:
            raise ValueError("message cannot be empty")
        if len(content) > 40_000:
            raise ValueError("message is too long")
        active = ActiveRun()
        with self.runs_lock:
            if self.chat_run is not None:
                raise ValueError("Chat is already responding")
            with store.lock:
                chat_thread = store.data["chat"]
                if any(message.get("pending") for message in chat_thread["messages"]):
                    raise ValueError("Chat is already responding")
                requested_model = self.normalize_chat_model(
                    payload.get("model") if "model" in payload else chat_thread.get("model")
                )
                if requested_model != chat_thread.get("model"):
                    if chat_thread.get("messages"):
                        raise ValueError("start a new chat before changing the chat model")
                    chat_thread["model"] = requested_model
                    chat_thread.pop("provider_session_id", None)
                percent = self._context_percent_value(chat_thread.get(
                    "context_window_percent", self._context_percent_for("codex"),
                ))
                context_max = self._context_max("codex", requested_model)
                context_limit = self._context_limit("codex", requested_model, percent)
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
                store.save()
                public = store.chat_public()
            self.chat_run = active
        factory = self.thread_factory or threading.Thread
        thread = factory(
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
                with store.lock:
                    chat_thread = store.data["chat"]
                    failed = next(
                        message for message in chat_thread["messages"]
                        if message.get("id") == pending["id"]
                    )
                    failed.update({"content": f"Chat error: {error}", "error": True})
                    failed.pop("pending", None)
                    chat_thread["updated_at"] = int(time.time())
                    store.save()
                if self.chat_run is active:
                    self.chat_run = None
            raise
        return public

    def _run_chat(
        self, pending_id: str, content: str, model: str, active: ActiveRun,
    ) -> None:
        store = self._require_store()
        reply = ""
        try:
            with store.lock:
                chat_thread = store.data["chat"]
                session_id = chat_thread.get("provider_session_id")
                percent = self._context_percent_value(chat_thread.get(
                    "context_window_percent", self._context_percent_for("codex"),
                ))
            context_limit = self._context_limit("codex", model, percent)
            prepared = self.prepare(
                "codex",
                self.default_cwd,
                model=model,
                session_id=session_id,
                current_provider="codex",
                current_model=model,
                context_limit_tokens=context_limit,
                mode="chat",
            )
            result = self.execute(content, prepared, active)
            if result.exit_code:
                raise RuntimeError(
                    result.error or result.text or "Chat model exited without a response"
                )
            reply = result.text.strip()
            self.apply_session(result, prepared)
            with store.lock:
                chat_thread = store.data["chat"]
                chat_thread["provider_session_id"] = (
                    result.session_id or prepared.conversation.provider_session_id
                )
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
                with store.lock:
                    chat_thread = store.data["chat"]
                    pending = next(
                        message for message in chat_thread["messages"]
                        if message.get("id") == pending_id
                    )
                    chat_thread["context_chars"] = int(chat_thread.get("context_chars", 0)) \
                        + len(content) + len(reply)
                    percent = self._context_percent_value(chat_thread.get(
                        "context_window_percent", self._context_percent_for("codex"),
                    ))
                    context_max = self._context_max("codex", model)
                    context_limit = self._context_limit("codex", model, percent)
                    if context_limit is not None:
                        chat_thread["context_limit_tokens"] = context_limit
                        chat_thread["context_max_tokens"] = context_max
                        chat_thread["context_window_percent"] = percent
                    else:
                        chat_thread.pop("context_limit_tokens", None)
                        chat_thread.pop("context_max_tokens", None)
                    if self.context_usage is None:
                        raise RuntimeError("Chat context usage calculation is not configured")
                    usage = self.context_usage(
                        chat_thread["context_chars"], self.chat_context_warning_chars,
                        limit_tokens=chat_thread.get("context_limit_tokens"),
                        max_tokens=chat_thread.get("context_max_tokens"),
                        allowance_percent=chat_thread.get("context_window_percent"),
                    )
                    crossed_warning = usage["percent"] >= 80 \
                        and not chat_thread.get("warning_announced")
                    if crossed_warning:
                        reply += (
                            "\n\nThis Chat thread is carrying a lot of context now. "
                            "Start a new Chat conversation soon to keep responses quick "
                            "and economical."
                        )
                        chat_thread["warning_announced"] = True
                    chat_thread["context_warning"] = usage["percent"] >= 80
                    pending["content"] = reply
                    if reply.startswith("Chat error:"):
                        pending["error"] = True
                    pending.pop("pending", None)
                    pending.pop("cancel_requested", None)
                    chat_thread["updated_at"] = int(time.time())
                    store.save()
                if self.chat_run is active:
                    self.chat_run = None

    def cancel_chat(self) -> dict[str, Any]:
        store = self._require_store()
        with self.runs_lock:
            active = self.chat_run
            with store.lock:
                chat_thread = store.data["chat"]
                pending = next(
                    (message for message in chat_thread["messages"] if message.get("pending")),
                    None,
                )
                if pending is None:
                    raise ValueError("Chat is not responding")
                pending["cancel_requested"] = True
                store.save()
            if active is not None:
                active.cancel_event.set()
            return store.chat_public()

    def reset_chat(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        store = self._require_store()
        payload = payload or {}
        model = self.normalize_chat_model(payload["model"]) \
            if "model" in payload else self.chat_model
        with self.runs_lock:
            if self.chat_run is not None:
                raise ValueError("stop Chat before starting a new conversation")
            chat_thread = store.reset_chat(model)
            percent = self._context_percent_for("codex")
            context_max = self._context_max("codex", model)
            context_limit = self._context_limit("codex", model, percent)
            if context_limit is not None:
                with store.lock:
                    store.data["chat"]["context_limit_tokens"] = context_limit
                    store.data["chat"]["context_max_tokens"] = context_max
                    store.data["chat"]["context_window_percent"] = percent
                    store.save()
                chat_thread = store.chat_public()
            return {
                "chat": chat_thread,
                "chat_history": store.chat_history_public(),
            }


__all__ = ["ActiveRun", "PreparedRun", "ProviderRunOrchestrator", "RunCancelled", "RunResult"]
