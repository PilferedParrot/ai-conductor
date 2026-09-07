"""Adapter for Google's Antigravity (``agy``) headless CLI."""
from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any, Callable

from .adapters import ProgressEvent, ProviderAdapter, ProviderCapabilities


class AntigravityAdapter(ProviderAdapter):
    """Run Antigravity's newline-delimited JSON protocol without exposing prompts."""

    capabilities = ProviderCapabilities(
        chat=False, resume=True, tools=True, usage=False, models=True,
    )

    def _command(self, conversation: Any) -> list[str]:
        from .dispatch import provider_command

        settings = self.config.get("antigravity", {})
        command = [
            provider_command(self.config, "antigravity"),
            "--input-format", "stream-json",
            "--output-format", "stream-json",
        ]
        mode = str(settings.get("mode") or "accept-edits").strip().lower()
        if settings.get("read_only"):
            raise ValueError(
                "Antigravity read-only Chat is not yet supported; use Work instead."
            )
        if mode not in {"accept-edits", "plan"}:
            raise ValueError(f"unsupported Antigravity mode: {mode}")
        command += ["--mode", mode]
        if mode == "accept-edits":
            command.append("--disable-slash-commands")
        if settings.get("model"):
            command += ["--model", str(settings["model"])]
        # Antigravity requires the explicit conversation identifier.  Do not
        # substitute a provider-side "latest" session: that can resume the
        # wrong conversation when several requests are in flight.
        if conversation.provider_session_id:
            command += ["--conversation", str(conversation.provider_session_id)]
        return command

    @staticmethod
    def _input(prompt: str) -> str:
        return json.dumps({
            "event": "user",
            "message": {"content": prompt},
        }, separators=(",", ":")) + "\n"

    @staticmethod
    def _unsupported_flags(stderr: str) -> bool:
        text = stderr.lower()
        return any(value in text for value in (
            "unknown option", "unrecognized option", "unrecognised option",
            "unsupported option", "invalid option", "unexpected argument",
            "flag provided but not defined",
        ))

    def _redact_event(self, value: Any) -> Any:
        """Redact configured credentials before retaining provider event data."""
        from .config import redact_configured_secrets

        if isinstance(value, str):
            return redact_configured_secrets(self.config, value)
        if isinstance(value, list):
            return [self._redact_event(item) for item in value]
        if isinstance(value, dict):
            return {str(key): self._redact_event(item) for key, item in value.items()}
        return value

    def run(self, prompt: str, cwd: Path, conversation: Any,
            cancel_event: threading.Event | None = None,
            on_progress: Callable[[ProgressEvent], None] | None = None) -> Any:
        from .dispatch import RunResult, _stream_process

        event = cancel_event or threading.Event()
        self._cancellations[id(conversation)] = event
        response: str | None = None
        result_seen = False
        protocol_error: str | None = None
        expected_session_id = getattr(conversation, "provider_session_id", None)
        expected_session_id = str(expected_session_id) if expected_session_id else None
        init_session_id: str | None = None
        result_session_id: str | None = None

        def receive(line: str) -> None:
            nonlocal response, result_seen, protocol_error, init_session_id, result_session_id
            if not line.strip():
                return
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                protocol_error = "Antigravity returned malformed JSON output"
                return
            if not isinstance(item, dict):
                protocol_error = "Antigravity returned a non-object JSON event"
                return
            if item.get("event") == "init" or item.get("type") == "init":
                conversation_id = item.get("conversation_id") or item.get("conversationId")
                if conversation_id:
                    init_session_id = str(conversation_id)
                    if expected_session_id and init_session_id != expected_session_id:
                        protocol_error = "Antigravity resumed a different conversation"
                if on_progress:
                    on_progress(ProgressEvent("status", "Started working", self.provider,
                                               self._redact_event(item)))
                return
            event_type = item.get("event") or item.get("type")
            if event_type == "step_update":
                update = item.get("step_update")
                if not isinstance(update, dict):
                    protocol_error = "Antigravity returned a step update without a step_update payload"
                    return
                safe_item = self._redact_event(item)
                step_type = update.get("step_type")
                if step_type == "agent_response":
                    delta = update.get("text_delta")
                    if isinstance(delta, str) and on_progress:
                        on_progress(ProgressEvent("output", self._redact_event(delta), self.provider,
                                                   safe_item))
                elif step_type == "tool":
                    if on_progress:
                        tool_info = update.get("tool_info")
                        name = update.get("tool_name")
                        if not name and isinstance(tool_info, dict):
                            name = tool_info.get("name")
                        on_progress(ProgressEvent(
                            "tool_result" if update.get("state") == "DONE" else "tool",
                            f"{'Finished' if update.get('state') == 'DONE' else 'Using'} "
                            f"{self._redact_event(str(name or 'tool'))}", self.provider,
                            safe_item,
                        ))
                return
            if event_type == "result":
                if result_seen:
                    protocol_error = "Antigravity returned more than one result event"
                    return
                envelope = item.get("result")
                if not isinstance(envelope, dict):
                    protocol_error = "Antigravity returned a result without a result envelope"
                    return
                result_seen = True
                status = str(envelope.get("status") or "").upper()
                if status != "SUCCESS":
                    detail = envelope.get("error") or envelope.get("message") or status or "unknown status"
                    protocol_error = f"Antigravity request failed ({self._redact_event(detail)})"
                    return
                conversation_id = envelope.get("conversation_id")
                if not isinstance(conversation_id, str) or not conversation_id:
                    protocol_error = "Antigravity returned a result without a conversation ID"
                    return
                result_session_id = conversation_id
                if ((expected_session_id and result_session_id != expected_session_id)
                        or (init_session_id and result_session_id != init_session_id)):
                    protocol_error = "Antigravity returned a result for a different conversation"
                    return
                value = envelope.get("response")
                if not isinstance(value, str):
                    protocol_error = "Antigravity returned SUCCESS without a response"
                    return
                response = value
                # Usage is cumulative for resumed conversations in this
                # protocol, so it is intentionally not reported as per-turn
                # usage by this adapter.
                return
            if on_progress and event_type:
                on_progress(ProgressEvent("commentary", self._redact_event(str(event_type)), self.provider,
                                           self._redact_event(item)))

        try:
            completed = _stream_process(
                self._command(conversation), self._input(prompt), cwd,
                cancel_event=event,
                stdout_line=receive,
            )
        finally:
            self._cancellations.pop(id(conversation), None)

        from .config import redact_configured_secrets

        stderr = redact_configured_secrets(self.config, completed.stderr.strip())
        error = protocol_error
        if completed.returncode and not error:
            error = stderr or "Antigravity CLI exited with an error"
        if not result_seen and not error:
            error = "Antigravity returned no result event"
        if self._unsupported_flags(stderr):
            error = (
                "Antigravity CLI does not support the requested headless flags; "
                "update agy to a current version (1.1.27 or newer)."
            )
        exit_code = completed.returncode or (1 if error else 0)
        if not error and result_session_id:
            conversation.provider_session_id = result_session_id
        return RunResult(
            response or "", exit_code, conversation.provider_session_id,
            error, unavailable=bool(exit_code and not response),
        )

    def available(self) -> bool:
        from .config import resolve_command
        return resolve_command(self.config, "antigravity") is not None

    def authentication(self) -> bool | None:
        return None

    def models(self) -> list[dict[str, Any]]:
        """Read the CLI's tab-separated model listing, when available."""
        from .dispatch import _capture_process, provider_command

        try:
            completed = _capture_process(
                [provider_command(self.config, "antigravity"), "models"], "",
                Path.cwd(), cancel_event=None, timeout_seconds=10,
            )
        except (OSError, TimeoutError) as exc:
            raise RuntimeError(f"Antigravity model discovery failed: {exc}") from None
        if completed.returncode:
            raise RuntimeError(
                f"Antigravity model discovery failed (exit {completed.returncode})."
            )
        options: list[dict[str, Any]] = []
        seen: set[str] = set()
        for line in completed.stdout.splitlines():
            fields = line.split("\t", 1)
            if len(fields) != 2:
                continue
            value, label = (field.strip() for field in fields)
            if (value and label and value.lower() not in {"id", "model"}
                    and len(value) <= 200 and len(label) <= 500 and value not in seen):
                options.append({"value": value, "label": label})
                seen.add(value)
                if len(options) == 200:
                    break
        if not options:
            raise RuntimeError("Antigravity model discovery returned no models")
        return options
