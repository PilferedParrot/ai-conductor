from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from queue import Empty, Queue
from pathlib import Path
from typing import Any, Callable

from .budgets import qwen_available
from .config import (
    compatible_api_headers, open_compatible_url, provider_additional_dirs,
    redact_configured_secrets,
    validate_qwen_endpoints,
)
from .qwen_tools import QwenToolbox, TOOL_DEFINITIONS, parse_tool_arguments
from .response_identity import configured_identity, record_reported_model


AGENT_SYSTEM_PROMPT = """You are PilferedParrot's coding agent. Work directly on the user's request
inside the workspace given below. Inspect relevant files before editing, make focused changes, and
run proportionate checks. Use the file tools for precise reads and edits, shell for discovery and
tests, and diff to inspect the resulting patch. Never claim that a command passed unless its tool
result says it did. All paths are relative to the workspace unless stated otherwise.

Workspace: {cwd}
"""


def ensure_qwen(
    config: dict[str, Any], notify: Callable[[str], None] = print,
    cancel_event: threading.Event | None = None,
) -> None:
    validate_qwen_endpoints(config)
    if qwen_available(config):
        return
    qwen = config["qwen"]
    if qwen.get("auto_start") is not True:
        raise RuntimeError(
            "Qwen is not running; start it manually or configure qwen.start_command "
            "and set qwen.auto_start to true"
        )
    command = qwen.get("start_command")
    if not isinstance(command, list) or not command:
        raise RuntimeError("qwen.auto_start is enabled but qwen.start_command is empty")
    notify("Qwen is down; starting the stock code profile (usually 30–60 seconds)…")
    timeout = float(qwen.get("start_timeout_seconds", 120))
    deadline = time.monotonic() + timeout
    from .dispatch import _capture_process
    completed = _capture_process(
        command, "", Path.cwd(), cancel_event=cancel_event, timeout_seconds=timeout,
    )
    if completed.returncode and "already running" not in completed.stdout:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise RuntimeError(f"failed to start Qwen: {detail}")
    while time.monotonic() < deadline:
        if cancel_event is not None and cancel_event.is_set():
            from .dispatch import RunCancelled
            raise RunCancelled("request cancelled")
        if qwen_available(config):
            notify("Qwen is ready.")
            return
        time.sleep(min(1, max(0, deadline - time.monotonic())))
    raise TimeoutError("Qwen did not become healthy before the startup timeout")


def _chat_completion(
    request_messages: list[dict[str, Any]],
    config: dict[str, Any],
    provider: str = "qwen",
    cancel_event: threading.Event | None = None,
    response_identity: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if provider == "qwen":
        validate_qwen_endpoints(config)
    provider_config = config[provider]
    tools = TOOL_DEFINITIONS
    if provider_config.get("read_only"):
        tools = [
            tool for tool in TOOL_DEFINITIONS
            if tool.get("function", {}).get("name") in {"read_file", "diff"}
        ]
    payload = {
        "model": provider_config["model"],
        "messages": request_messages,
        "tools": tools,
        "tool_choice": "auto",
        "max_tokens": int(provider_config.get("agent_max_tokens", 4096)),
        "stream": False,
    }
    request = urllib.request.Request(
        provider_config["base_url"].rstrip("/") + "/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers=compatible_api_headers(config, provider),
        method="POST",
    )
    def request_once() -> dict[str, Any]:
        timeout = float(provider_config.get("agent_request_timeout_seconds", 600))
        try:
            with open_compatible_url(config, provider, request, timeout=timeout) as response:
                result = json.load(response)
            if not isinstance(result, dict):
                raise _malformed_response(provider, "response body is not an object")
            choices = result.get("choices")
            if not isinstance(choices, list) or not choices:
                raise _malformed_response(provider, "choices is missing or not a non-empty array")
            if response_identity is not None:
                record_reported_model(response_identity, result.get("model"), config)
                for choice in choices:
                    if isinstance(choice, dict):
                        record_reported_model(response_identity, choice.get("model"), config)
            choice = choices[0]
            if not isinstance(choice, dict) or not isinstance(choice.get("message"), dict):
                raise _malformed_response(provider, "first choice has no message object")
            message = dict(choice["message"])
            usage = result.get("usage")
            if isinstance(usage, dict):
                message["_pilferedparrot_usage"] = usage
            return message
        except json.JSONDecodeError as exc:
            raise _malformed_response(provider, f"invalid JSON body: {exc.msg}") from None
        except urllib.error.HTTPError as exc:
            try:
                detail = exc.read(16_384).decode("utf-8", errors="replace")
            finally:
                exc.close()
            detail = redact_configured_secrets(config, detail)
            raise RuntimeError(f"{provider} request failed ({exc.code}): {detail}") from None
        except urllib.error.URLError as exc:
            detail = redact_configured_secrets(config, exc)
            raise RuntimeError(f"{provider} request failed: {detail}") from None

    if cancel_event is None:
        return request_once()
    completed: Queue[tuple[bool, Any]] = Queue(maxsize=1)

    def request_in_background() -> None:
        try:
            completed.put((True, request_once()))
        except BaseException as error:
            completed.put((False, error))

    threading.Thread(
        target=request_in_background, name=f"pilferedparrot-{provider}-http", daemon=True,
    ).start()
    while True:
        if cancel_event.is_set():
            from .dispatch import RunCancelled
            raise RunCancelled("request cancelled")
        try:
            succeeded, value = completed.get(timeout=0.1)
        except Empty:
            continue
        if succeeded:
            return value
        raise value


def _tool_label(name: str, arguments: dict[str, Any]) -> str:
    if name == "shell":
        detail = str(arguments.get("command", "")).replace("\n", " ")
    else:
        detail = str(arguments.get("path", ""))
    if len(detail) > 160:
        detail = detail[:157] + "..."
    return f"{name}({detail})"


def _content_text(content: Any) -> str:
    """Render OpenAI-compatible content for the UI without changing history."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if (isinstance(item, dict)
                    and item.get("type") in {"text", "output_text"}
                    and isinstance(item.get("text"), str)):
                parts.append(item["text"])
        return "".join(parts)
    return str(content)


def _malformed_response(provider: str, detail: str) -> RuntimeError:
    return RuntimeError(f"{provider} returned malformed chat completion: {detail}")


def run_qwen_agent(
    prompt: str,
    messages: list[dict[str, Any]],
    config: dict[str, Any],
    cwd: Path,
    cancel_event: threading.Event | None = None,
    on_progress: Callable[[str, str], None] | None = None,
    token_usage: dict[str, int] | None = None,
    response_identity: dict[str, Any] | None = None,
) -> str:
    return run_compatible_agent(
        "qwen", prompt, messages, config, cwd, cancel_event, on_progress, token_usage,
        response_identity,
    )


def run_compatible_agent(
    provider: str,
    prompt: str,
    messages: list[dict[str, Any]],
    config: dict[str, Any],
    cwd: Path,
    cancel_event: threading.Event | None = None,
    on_progress: Callable[[str, str], None] | None = None,
    token_usage: dict[str, int] | None = None,
    response_identity: dict[str, Any] | None = None,
) -> str:
    """Run any OpenAI-compatible model through the same contained tool loop."""
    provider_config = config[provider]
    if response_identity is not None:
        response_identity.clear()
        response_identity.update(configured_identity(config, provider))
    additional_dirs = provider_additional_dirs(config, provider)
    toolbox = QwenToolbox(cwd, provider_config, additional_dirs)
    messages.append({"role": "user", "content": prompt})
    # Appended rather than interpolated: the context estimator formats this
    # template with an empty workspace, so the template keeps one field.
    system_prompt = AGENT_SYSTEM_PROMPT.format(cwd=cwd)
    identity = response_identity or configured_identity(config, provider)
    system_prompt += (
        f"\nConfigured provider: {redact_configured_secrets(config, provider)}; requested model: "
        f"{identity.get('requested_model', '')}; endpoint: "
        f"{identity.get('endpoint_origin')} "
        f"({identity.get('endpoint_kind')}). "
        "This describes configured routing only: an endpoint location does not prove where "
        "inference runs, and any model ID reported by the server is self-reported evidence, "
        "not proof of the underlying model weights.\n"
    )
    if provider_config.get("read_only"):
        system_prompt += (
            "\nThis is a read-only Chat instance. Inspect and explain, but do not modify "
            "files or run commands.\n"
        )
    if additional_dirs:
        listed = "\n".join(f"- {root}" for root in additional_dirs)
        system_prompt += (
            "\nThese additional roots are also readable and writable. Reach them with "
            f"absolute paths; everything else outside the workspace is denied.\n{listed}\n"
        )
    system = {"role": "system", "content": system_prompt}
    max_turns = int(provider_config.get("max_tool_turns", 24))
    for _turn in range(max_turns):
        if cancel_event is not None and cancel_event.is_set():
            from .dispatch import RunCancelled
            raise RunCancelled("request cancelled")
        message = _chat_completion(
            [system, *messages], config, provider, cancel_event=cancel_event,
            response_identity=response_identity,
        )
        raw_usage = message.pop("_pilferedparrot_usage", None)
        if isinstance(raw_usage, dict):
            normalized: dict[str, int] = {}
            for key, aliases in {
                "input_tokens": ("input_tokens", "prompt_tokens"),
                "output_tokens": ("output_tokens", "completion_tokens"),
            }.items():
                raw_value = next(
                    (raw_usage.get(alias) for alias in aliases if raw_usage.get(alias) is not None),
                    None,
                )
                try:
                    value = int(raw_value)
                except (TypeError, ValueError):
                    continue
                if value >= 0:
                    normalized[key] = value
            if normalized:
                if token_usage is not None:
                    token_usage.clear()
                    token_usage.update(normalized)
        if cancel_event is not None and cancel_event.is_set():
            from .dispatch import RunCancelled
            raise RunCancelled("request cancelled")
        content = message.get("content")
        if content is not None and not isinstance(content, (str, list)):
            raise _malformed_response(provider, "assistant content is not a string, array, or null")
        if isinstance(content, list) and any(not isinstance(item, dict) for item in content):
            raise _malformed_response(provider, "assistant content array contains a non-object block")
        raw_tool_calls = message.get("tool_calls")
        if raw_tool_calls is not None and not isinstance(raw_tool_calls, list):
            raise _malformed_response(provider, "tool_calls is not an array")
        tool_calls = []
        for index, raw_call in enumerate(raw_tool_calls or []):
            if not isinstance(raw_call, dict):
                raise _malformed_response(provider, f"tool call {index} is not an object")
            call = dict(raw_call)
            function = call.get("function")
            if not isinstance(function, dict):
                raise _malformed_response(provider, f"tool call {index} has no function object")
            name = function.get("name")
            if not isinstance(name, str) or not name.strip():
                raise _malformed_response(provider, f"tool call {index} has no function name")
            if "id" in call and (not isinstance(call["id"], str) or not call["id"].strip()):
                raise _malformed_response(provider, f"tool call {index} has an invalid id")
            call.setdefault("id", f"qwen-tool-{_turn}-{index}")
            tool_calls.append(call)
        assistant_message: dict[str, Any] = {"role": "assistant", "content": content}
        # OpenRouter and reasoning-compatible servers require these opaque
        # fields to be replayed verbatim when tool results continue a turn.
        # Keep them in the wire transcript, while only rendering content below.
        for field in ("reasoning", "reasoning_details", "reasoning_content"):
            if field in message:
                assistant_message[field] = message[field]
        if tool_calls:
            assistant_message["tool_calls"] = tool_calls
        messages.append(assistant_message)
        if not tool_calls:
            rendered = _content_text(content)
            print(rendered)
            if on_progress and rendered.strip():
                on_progress("commentary", rendered.rstrip())
            return rendered
        rendered = _content_text(content)
        if rendered.strip():
            print(rendered.rstrip())
            if on_progress:
                on_progress("commentary", rendered.rstrip())
        for index, call in enumerate(tool_calls):
            if cancel_event is not None and cancel_event.is_set():
                from .dispatch import RunCancelled
                raise RunCancelled("request cancelled")
            function = call["function"]
            name = function.get("name", "")
            call_id = call["id"]
            try:
                arguments = parse_tool_arguments(function.get("arguments", "{}"))
                label = _tool_label(name, arguments)
                print(f"  {provider} › {label}")
                if on_progress:
                    on_progress("tool", label)
                result = toolbox.execute(name, arguments)
            except Exception as exc:
                result = f"tool_error: {type(exc).__name__}: {exc}"
                print(f"  {provider} › {result}")
            if on_progress:
                outcome = "failed" if result.startswith("tool_error:") else "completed"
                on_progress("tool_result", f"{name or 'tool'} {outcome}")
            messages.append({
                "role": "tool",
                "tool_call_id": call_id,
                "name": name,
                "content": result,
            })
    raise RuntimeError(f"{provider} exceeded its {max_turns}-turn tool-loop limit")
