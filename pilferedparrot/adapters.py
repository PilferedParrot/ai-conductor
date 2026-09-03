"""Provider-neutral execution contracts and built-in provider adapters."""
from __future__ import annotations

import json
import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable


@dataclass(frozen=True)
class ProgressEvent:
    """Normalized provider progress, independent of a CLI's wire format."""
    kind: str
    text: str = ""
    provider: str | None = None
    raw: dict[str, Any] | None = None


@dataclass(frozen=True)
class ProviderCapabilities:
    run: bool = True
    resume: bool = False
    cancel: bool = True
    streaming: bool = True
    tools: bool = False
    # ``usage`` is retained for compatibility and means execution-derived,
    # per-turn token telemetry. Allowance reporting is a separate capability.
    usage: bool = False
    allowance_reporting: bool = False
    organization_usage_reporting: bool = False
    models: bool = False


@dataclass(frozen=True)
class TokenUsage:
    input_tokens: int | None = None
    output_tokens: int | None = None
    context_window_tokens: int | None = None

    def as_dict(self) -> dict[str, int | None]:
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "context_window_tokens": self.context_window_tokens,
        }


@dataclass(frozen=True)
class ProviderAvailability:
    available: bool
    authenticated: bool | None
    reason: str | None = None


class ProviderAdapter(ABC):
    provider: str
    capabilities: ProviderCapabilities = ProviderCapabilities()

    def __init__(self, provider: str, config: dict[str, Any]) -> None:
        self.provider, self.config = provider, config
        self._cancellations: dict[int, threading.Event] = {}

    @abstractmethod
    def run(self, prompt: str, cwd: Path, conversation: Any,
            cancel_event: threading.Event | None = None,
            on_progress: Callable[[ProgressEvent], None] | None = None) -> Any:
        raise NotImplementedError

    def resume(self, prompt: str, cwd: Path, conversation: Any,
               cancel_event: threading.Event | None = None,
               on_progress: Callable[[ProgressEvent], None] | None = None) -> Any:
        return self.run(prompt, cwd, conversation, cancel_event, on_progress)

    def cancel(self, conversation: Any) -> None:
        event = self._cancellations.get(id(conversation))
        if event is not None:
            event.set()

    def token_usage(self, conversation: Any) -> dict[str, int]:
        return dict(getattr(conversation, "token_usage", {}) or {})

    def context_usage(self, conversation: Any) -> dict[str, int | None]:
        usage = self.token_usage(conversation)
        return {"input_tokens": usage.get("input_tokens"),
                "output_tokens": usage.get("output_tokens"),
                "context_window_tokens": usage.get("context_window_tokens")}

    def authentication(self) -> bool | None:
        return None

    def available(self) -> bool:
        return True

    def availability(self) -> ProviderAvailability:
        available = self.available()
        return ProviderAvailability(
            available, self.authentication() if available else None,
            None if available else "provider command or endpoint is unavailable",
        )

    def models(self) -> list[dict[str, Any]]:
        from .config import model_catalog
        if not self.available():
            raise RuntimeError(f"{self.provider} is unavailable")
        return list(model_catalog(self.config).get(self.provider, {}).get("options", []))

    # Explicit names make the contract convenient for status endpoints and
    # avoid forcing callers to know whether a provider uses a CLI or HTTP.
    def is_authenticated(self) -> bool:
        return self.authentication() is True

    def is_available(self) -> bool:
        return self.available()

    def usage(self, conversation: Any) -> TokenUsage:
        values = self.context_usage(conversation)
        return TokenUsage(**values)

    def _run_capture(self, func: Callable[..., Any], *args: Any, conversation: Any,
                     cancel_event: threading.Event | None,
                     on_progress: Callable[[ProgressEvent], None] | None) -> Any:
        event = cancel_event or threading.Event()
        self._cancellations[id(conversation)] = event
        callback = None
        if on_progress:
            callback = lambda kind, text: on_progress(
                ProgressEvent(kind, str(text), self.provider))
        try:
            result = func(*args, cancel_event=event, on_progress=callback)
            usage = getattr(conversation, "token_usage", None)
            if isinstance(usage, dict):
                for field_name, result_name in (
                    ("input_tokens", "input_tokens"),
                    ("output_tokens", "output_tokens"),
                    ("context_window_tokens", "live_context_window_tokens"),
                ):
                    value = getattr(result, result_name, None)
                    if value is not None:
                        usage[field_name] = int(value)
            return result
        finally:
            self._cancellations.pop(id(conversation), None)


class CodexAdapter(ProviderAdapter):
    capabilities = ProviderCapabilities(
        resume=True, tools=True, usage=True, allowance_reporting=True, models=True,
    )

    def run(self, prompt, cwd, conversation, cancel_event=None, on_progress=None):
        from .dispatch import capture_codex
        return self._run_capture(capture_codex, prompt, cwd, conversation, self.config,
                                 conversation=conversation, cancel_event=cancel_event,
                                 on_progress=on_progress)

    def authentication(self) -> bool | None:
        from .budgets import read_codex_budget
        from .model import AUTH_SIGNED_IN, AUTH_SIGNED_OUT
        status = read_codex_budget(self.config).auth_status
        return True if status == AUTH_SIGNED_IN else False if status == AUTH_SIGNED_OUT else None

    def available(self) -> bool:
        from .config import resolve_command
        return resolve_command(self.config, "codex") is not None


class ClaudeAdapter(ProviderAdapter):
    capabilities = ProviderCapabilities(resume=True, tools=True, usage=True, models=True)

    def run(self, prompt, cwd, conversation, cancel_event=None, on_progress=None):
        from .dispatch import capture_claude
        return self._run_capture(capture_claude, prompt, cwd, conversation, self.config,
                                 conversation=conversation, cancel_event=cancel_event,
                                 on_progress=on_progress)

    def authentication(self) -> bool | None:
        from .budgets import read_claude_status
        from .model import AUTH_SIGNED_IN, AUTH_SIGNED_OUT
        status = read_claude_status(self.config).auth_status
        return True if status == AUTH_SIGNED_IN else False if status == AUTH_SIGNED_OUT else None

    def available(self) -> bool:
        from .config import resolve_command
        return resolve_command(self.config, "claude") is not None


class OpenAICompatibleAdapter(ProviderAdapter):
    capabilities = ProviderCapabilities(
        resume=True, cancel=True, tools=True, usage=True, models=True,
    )

    def run(self, prompt, cwd, conversation, cancel_event=None, on_progress=None):
        from . import dispatch as dispatch_module
        run_compatible_agent = dispatch_module.run_compatible_agent
        run_qwen_agent = dispatch_module.run_qwen_agent
        event = cancel_event or threading.Event()
        self._cancellations[id(conversation)] = event
        callback = (lambda kind, text: on_progress(ProgressEvent(kind, str(text), self.provider))) \
            if on_progress else None
        try:
            runner = run_qwen_agent if self.provider == "qwen" else run_compatible_agent
            args = (prompt, conversation.messages, self.config, cwd)
            if runner is run_compatible_agent:
                args = (self.provider, *args)
            text = runner(*args, cancel_event=event, on_progress=callback,
                          token_usage=conversation.token_usage)
            from .dispatch import RunResult
            usage = conversation.token_usage
            return RunResult(
                text, 0, input_tokens=usage.get("input_tokens"),
                output_tokens=usage.get("output_tokens"),
                live_input_tokens=usage.get("input_tokens"),
                live_output_tokens=usage.get("output_tokens"),
            )
        finally:
            self._cancellations.pop(id(conversation), None)

    def available(self) -> bool:
        return bool(self.config.get(self.provider, {}).get("base_url"))

    def authentication(self) -> bool | None:
        import os
        variable = str(self.config.get(self.provider, {}).get("api_key_env") or "").strip()
        return bool(os.environ.get(variable)) if variable else True

    def models(self) -> list[dict[str, Any]]:
        """Poll the standard OpenAI model-list endpoint for this account."""
        from .config import compatible_api_headers, model_catalog, open_compatible_url
        from urllib.request import Request
        base_url = str(self.config.get(self.provider, {}).get("base_url") or "").rstrip("/")
        request = Request(
            base_url + "/models", headers=compatible_api_headers(self.config, self.provider),
        )
        with open_compatible_url(self.config, self.provider, request, timeout=5) as response:
            payload = json.loads(response.read().decode("utf-8"))
        raw_models = payload.get("data") if isinstance(payload, dict) else None
        candidates = raw_models if isinstance(raw_models, list) else []
        ids = list(dict.fromkeys(
            value.strip()
            for item in candidates
            if isinstance(item, dict)
            if isinstance((value := item.get("id")), str)
            and value.strip() and len(value.strip()) <= 128
        ))[:200]
        if not ids:
            raise ValueError("provider returned no models")
        known = {
            str(option.get("value")): option
            for option in model_catalog(self.config).get(self.provider, {}).get("options", [])
            if isinstance(option, dict)
        }
        return [dict(known.get(model_id) or {"value": model_id, "label": model_id})
                for model_id in ids]


class GeminiAdapter(ProviderAdapter):
    capabilities = ProviderCapabilities(resume=True, tools=True, usage=True, models=True)

    def _command(self, conversation: Any) -> list[str]:
        cfg = self.config.get("gemini", {})
        from .dispatch import provider_command
        # A piped stdin puts Gemini in headless mode without exposing the user
        # prompt in the process list. Gemini's JSONL formatter then supplies a
        # stable provider-neutral stream to normalize below.
        command = [provider_command(self.config, "gemini"), "--output-format", "stream-json"]
        approval_mode = str(cfg.get("approval_mode") or "auto_edit")
        if approval_mode not in {"default", "auto_edit", "yolo", "plan"}:
            raise ValueError(f"unsupported Gemini approval mode: {approval_mode}")
        command += ["--approval-mode", approval_mode]
        if cfg.get("model"):
            command += ["--model", str(cfg["model"])]
        if conversation.provider_session_id:
            command += ["--resume", str(conversation.provider_session_id)]
        return command

    def run(self, prompt, cwd, conversation, cancel_event=None, on_progress=None):
        event = cancel_event or threading.Event()
        self._cancellations[id(conversation)] = event
        text_parts: list[str] = []
        error_message: str | None = None
        try:
            from .dispatch import _stream_process

            def receive(line: str) -> None:
                nonlocal error_message
                try: item = json.loads(line)
                except json.JSONDecodeError:
                    if on_progress and line.strip():
                        on_progress(ProgressEvent("output", line.rstrip(), self.provider))
                    return
                event_type = item.get("type")
                if event_type == "init" and item.get("session_id"):
                    conversation.provider_session_id = str(item["session_id"])
                    if on_progress:
                        on_progress(ProgressEvent("status", "Started working", self.provider, item))
                text = item.get("content")
                if event_type == "message" and item.get("role") == "assistant" \
                        and isinstance(text, str):
                    text_parts.append(text)
                    if on_progress:
                        on_progress(ProgressEvent("output", text, self.provider, item))
                elif event_type == "tool_use" and on_progress:
                    on_progress(ProgressEvent(
                        "tool", f"Using {item.get('tool_name') or 'tool'}", self.provider, item,
                    ))
                elif event_type == "tool_result" and on_progress:
                    on_progress(ProgressEvent(
                        "tool_result", f"Tool finished ({item.get('status') or 'unknown'})",
                        self.provider, item,
                    ))
                elif event_type == "error" and on_progress:
                    on_progress(ProgressEvent(
                        "commentary", str(item.get("message") or "Gemini warning"),
                        self.provider, item,
                    ))
                elif event_type == "result" and item.get("status") == "error":
                    detail = item.get("error")
                    error_message = str(detail.get("message")) \
                        if isinstance(detail, dict) and detail.get("message") else "Gemini failed"
                usage = item.get("stats") if event_type == "result" else None
                if isinstance(usage, dict):
                    for dest in ("input_tokens", "output_tokens"):
                        if usage.get(dest) is not None:
                            try: conversation.token_usage[dest] = int(usage[dest])
                            except (TypeError, ValueError): pass
            completed = _stream_process(
                self._command(conversation), prompt, cwd,
                cancel_event=event,
                timeout_seconds=float(
                    self.config.get("gemini", {}).get("request_timeout_seconds", 1800),
                ),
                stdout_line=receive,
            )
        finally:
            self._cancellations.pop(id(conversation), None)
        from .dispatch import RunResult
        error = error_message or completed.stderr.strip() or None
        exit_code = completed.returncode or (1 if error_message else 0)
        return RunResult(
            "".join(text_parts), exit_code, conversation.provider_session_id, error,
            unavailable=exit_code != 0 and not text_parts,
            input_tokens=conversation.token_usage.get("input_tokens"),
            output_tokens=conversation.token_usage.get("output_tokens"),
        )

    def available(self) -> bool:
        from .config import resolve_command
        return resolve_command(self.config, "gemini") is not None

    def authentication(self) -> bool | None:
        import os
        if os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY"):
            return True
        credentials = Path(os.environ.get("GEMINI_CLI_HOME") or Path.home() / ".gemini") \
            / "oauth_creds.json"
        return True if credentials.is_file() else None


def adapter_for(provider: str, config: dict[str, Any]) -> ProviderAdapter:
    adapter_type = PROVIDER_ADAPTERS.get(provider)
    if adapter_type is not None:
        return adapter_type(provider, config)
    if config.get(provider, {}).get("adapter") == "openai_compatible":
        return OpenAICompatibleAdapter(provider, config)
    raise ValueError(f"unknown provider: {provider}")


# Public registry hook for applications that want to inspect or extend the
# built-ins without adding another dispatch branch.
PROVIDER_ADAPTERS = {
    "codex": CodexAdapter,
    "claude": ClaudeAdapter,
    "gemini": GeminiAdapter,
    "qwen": OpenAICompatibleAdapter,
}

get_provider_adapter = adapter_for
