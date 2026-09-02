from __future__ import annotations

import json
import subprocess
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable

from .budgets import qwen_available
from .qwen_tools import QwenToolbox, TOOL_DEFINITIONS, parse_tool_arguments


AGENT_SYSTEM_PROMPT = """You are Qwen's local coding agent. Work directly on the user's request
inside the workspace given below. Inspect relevant files before editing, make focused changes, and
run proportionate checks. Use the file tools for precise reads and edits, shell for discovery and
tests, and diff to inspect the resulting patch. Never claim that a command passed unless its tool
result says it did. All paths are relative to the workspace unless stated otherwise.

Workspace: {cwd}
"""


def ensure_qwen(config: dict[str, Any], notify: Callable[[str], None] = print) -> None:
    if qwen_available(config):
        return
    qwen = config["qwen"]
    if not qwen.get("auto_start", True):
        raise RuntimeError(
            "Qwen is not running; start it manually or configure qwen.start_command "
            "and set qwen.auto_start to true"
        )
    command = qwen.get("start_command")
    if not isinstance(command, list) or not command:
        raise RuntimeError("qwen.auto_start is enabled but qwen.start_command is empty")
    notify("Qwen is down; starting the stock code profile (usually 30–60 seconds)…")
    completed = subprocess.run(command, text=True, capture_output=True)
    if completed.returncode and "already running" not in completed.stdout:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise RuntimeError(f"failed to start Qwen: {detail}")
    deadline = time.monotonic() + float(qwen.get("start_timeout_seconds", 120))
    while time.monotonic() < deadline:
        if qwen_available(config):
            notify("Qwen is ready.")
            return
        time.sleep(1)
    raise TimeoutError("Qwen did not become healthy before the startup timeout")


def _chat_completion(
    request_messages: list[dict[str, Any]],
    config: dict[str, Any],
) -> dict[str, Any]:
    qwen = config["qwen"]
    payload = {
        "model": qwen["model"],
        "messages": request_messages,
        "tools": TOOL_DEFINITIONS,
        "tool_choice": "auto",
        "temperature": 1.0,
        "top_p": 0.95,
        "max_tokens": int(qwen.get("agent_max_tokens", 4096)),
        "stream": False,
    }
    request = urllib.request.Request(
        qwen["base_url"].rstrip("/") + "/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        timeout = float(qwen.get("agent_request_timeout_seconds", 600))
        with urllib.request.urlopen(request, timeout=timeout) as response:
            result = json.load(response)
        message = dict(result["choices"][0]["message"])
        usage = result.get("usage")
        if isinstance(usage, dict):
            message["_pilferedparrot_usage"] = usage
        return message
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Qwen request failed ({exc.code}): {detail}") from exc


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
    qwen = config["qwen"]
    toolbox = QwenToolbox(cwd, qwen)
    messages.append({"role": "user", "content": prompt})
    system = {"role": "system", "content": AGENT_SYSTEM_PROMPT.format(cwd=cwd)}
    max_turns = int(qwen.get("max_tool_turns", 24))
    for _turn in range(max_turns):
        if cancel_event is not None and cancel_event.is_set():
            from .dispatch import RunCancelled
            raise RunCancelled("request cancelled")
        message = _chat_completion([system, *messages], config)
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
                print(f"  qwen › {label}")
                if on_progress:
                    on_progress("tool", label)
                result = toolbox.execute(name, arguments)
            except Exception as exc:
                result = f"tool_error: {type(exc).__name__}: {exc}"
                print(f"  qwen › {result}")
            if on_progress:
                outcome = "failed" if result.startswith("tool_error:") else "completed"
                on_progress("tool_result", f"{name or 'tool'} {outcome}")
            messages.append({
                "role": "tool",
                "tool_call_id": call_id,
                "name": name,
                "content": result,
            })
    raise RuntimeError(f"Qwen exceeded its {max_turns}-turn tool-loop limit")
