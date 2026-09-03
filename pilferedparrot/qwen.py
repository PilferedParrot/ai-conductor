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
    validate_qwen_endpoints,
)
from .qwen_tools import QwenToolbox, TOOL_DEFINITIONS, parse_tool_arguments


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
            message = dict(result["choices"][0]["message"])
            usage = result.get("usage")
            if isinstance(usage, dict):
                message["_pilferedparrot_usage"] = usage
            return message
        except urllib.error.HTTPError as exc:
            try:
                detail = exc.read(16_384).decode("utf-8", errors="replace")
            finally:
                exc.close()
            raise RuntimeError(f"{provider} request failed ({exc.code}): {detail}") from exc

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


def run_qwen_agent(
    prompt: str,
    messages: list[dict[str, Any]],
    config: dict[str, Any],
    cwd: Path,
    cancel_event: threading.Event | None = None,
    on_progress: Callable[[str, str], None] | None = None,
    token_usage: dict[str, int] | None = None,
) -> str:
    return run_compatible_agent(
        "qwen", prompt, messages, config, cwd, cancel_event, on_progress, token_usage,
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
) -> str:
    """Run any OpenAI-compatible model through the same contained tool loop."""
    provider_config = config[provider]
    additional_dirs = provider_additional_dirs(config, provider)
    toolbox = QwenToolbox(cwd, provider_config, additional_dirs)
    messages.append({"role": "user", "content": prompt})
    # Appended rather than interpolated: the context estimator formats this
    # template with an empty workspace, so the template keeps one field.
    system_prompt = AGENT_SYSTEM_PROMPT.format(cwd=cwd)
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
        content = message.get("content") or ""
        tool_calls = []
        for index, raw_call in enumerate(message.get("tool_calls") or []):
            call = dict(raw_call)
            call.setdefault("id", f"qwen-tool-{_turn}-{index}")
            tool_calls.append(call)
        assistant_message: dict[str, Any] = {"role": "assistant", "content": content}
        if tool_calls:
            assistant_message["tool_calls"] = tool_calls
        messages.append(assistant_message)
        if not tool_calls:
            print(content)
            if on_progress and content.strip():
                on_progress("commentary", content.rstrip())
            return content
        if content.strip():
            print(content.rstrip())
            if on_progress:
                on_progress("commentary", content.rstrip())
        for index, call in enumerate(tool_calls):
            if cancel_event is not None and cancel_event.is_set():
                from .dispatch import RunCancelled
                raise RunCancelled("request cancelled")
            function = call.get("function") or {}
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
