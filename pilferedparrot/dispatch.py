from __future__ import annotations

import json
import os
import signal
import subprocess
import threading
import time
from queue import Empty, Queue
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .config import codex_additional_write_dirs, model_context_window, resolve_command
from .model import Conversation
from .qwen import run_qwen_agent


@dataclass(frozen=True)
class RunResult:
    text: str
    exit_code: int
    session_id: str | None = None
    error: str | None = None
    unavailable: bool = False
    input_tokens: int | None = None
    output_tokens: int | None = None


class RunCancelled(RuntimeError):
    pass


ProgressCallback = Callable[[str, str], None]


def _check_cancelled(cancel_event: threading.Event | None) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise RunCancelled("request cancelled")


def _stop_process(proc: subprocess.Popen[str]) -> None:
    if proc.poll() is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except (OSError, ProcessLookupError):
        proc.terminate()
    try:
        proc.wait(timeout=2)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (OSError, ProcessLookupError):
            proc.kill()


def _capture_process(
    command: list[str],
    prompt: str,
    cwd: Path,
    *,
    cancel_event: threading.Event | None,
    timeout_seconds: float,
) -> subprocess.CompletedProcess[str]:
    """Capture a CLI while retaining the ability to cancel or time it out."""
    _check_cancelled(cancel_event)
    proc = subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=cwd,
        start_new_session=True,
    )
    deadline = time.monotonic() + timeout_seconds
    first_communicate = True
    try:
        while True:
            _check_cancelled(cancel_event)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"provider request timed out after {timeout_seconds:g}s")
            try:
                stdout, stderr = proc.communicate(
                    input=prompt if first_communicate else None,
                    timeout=min(0.25, remaining),
                )
                return subprocess.CompletedProcess(command, proc.returncode, stdout, stderr)
            except subprocess.TimeoutExpired:
                first_communicate = False
    except BaseException:
        _stop_process(proc)
        try:
            proc.communicate(timeout=1)
        except (OSError, subprocess.TimeoutExpired):
            pass
        raise


def _stream_process(
    command: list[str],
    prompt: str,
    cwd: Path,
    *,
    cancel_event: threading.Event | None,
    timeout_seconds: float,
    stdout_line: Callable[[str], None],
) -> subprocess.CompletedProcess[str]:
    """Drain a provider line-by-line while retaining cancellation and stderr."""
    _check_cancelled(cancel_event)
    proc = subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=cwd,
        bufsize=1,
        start_new_session=True,
    )
    assert proc.stdin is not None and proc.stdout is not None and proc.stderr is not None
    lines: Queue[tuple[str, str | None]] = Queue()

    def drain(channel: str, stream: Any) -> None:
        try:
            for line in stream:
                lines.put((channel, line))
        finally:
            lines.put((channel, None))

    readers = [
        threading.Thread(target=drain, args=("stdout", proc.stdout), daemon=True),
        threading.Thread(target=drain, args=("stderr", proc.stderr), daemon=True),
    ]
    for reader in readers:
        reader.start()
    stdout: list[str] = []
    stderr: list[str] = []
    finished: set[str] = set()
    deadline = time.monotonic() + timeout_seconds
    try:
        try:
            proc.stdin.write(prompt)
            proc.stdin.close()
        except BrokenPipeError:
            pass
        while len(finished) < 2 or proc.poll() is None:
            _check_cancelled(cancel_event)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"provider request timed out after {timeout_seconds:g}s")
            try:
                channel, line = lines.get(timeout=min(0.1, remaining))
            except Empty:
                continue
            if line is None:
                finished.add(channel)
            elif channel == "stdout":
                stdout.append(line)
                stdout_line(line)
            else:
                stderr.append(line)
        return subprocess.CompletedProcess(
            command, proc.wait(), "".join(stdout), "".join(stderr),
        )
    except BaseException:
        _stop_process(proc)
        raise


def _looks_unavailable(text: str | None) -> bool:
    detail = (text or "").lower()
    return any(marker in detail for marker in (
        "not logged in", "signed out", "authentication", "authenticate",
        "conversation is already running", "failed to fetch", "connection refused",
    ))


def provider_command(config: dict[str, Any], provider: str) -> str:
    """Resolve a provider CLI for dispatch, failing loudly when it is missing.

    Budget collection degrades quietly on a missing CLI; dispatch must not. If we
    fall through to the bare name here, subprocess raises a bare FileNotFoundError
    that tells the user nothing about which knob to turn.
    """
    command = resolve_command(config, provider)
    if command is None:
        raise FileNotFoundError(
            f"{provider} CLI `{config[provider]['command']}` was not found on PATH or in the "
            f"usual install locations; set {provider}.command in config.json to its full path"
        )
    return command


def _codex_message(event: dict[str, Any]) -> str | None:
    item = event.get("item") or {}
    if event.get("type") == "item.completed" and item.get("type") == "agent_message":
        return item.get("text") or item.get("content")
    if event.get("type") in ("agent_message", "message.completed"):
        return event.get("text") or event.get("message")
    return None


def _token_count(value: Any) -> int | None:
    try:
        count = int(value)
    except (TypeError, ValueError):
        return None
    return count if count >= 0 else None


def dispatch_codex(prompt: str, cwd: Path, conversation: Conversation, config: dict[str, Any]) -> int:
    command = _codex_command(conversation, config, cwd)
    proc = subprocess.Popen(
        command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=None,
        text=True, cwd=cwd, bufsize=1,
    )
    assert proc.stdin is not None and proc.stdout is not None
    proc.stdin.write(prompt)
    proc.stdin.close()
    printed = False
    for line in proc.stdout:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            print(line, end="")
            continue
        if event.get("type") == "thread.started" and event.get("thread_id"):
            conversation.provider_session_id = event["thread_id"]
        message = _codex_message(event)
        if message:
            if printed:
                print()
            print(message, end="", flush=True)
            printed = True
    if printed:
        print()
    return proc.wait()


def _codex_command(conversation: Conversation, config: dict[str, Any], cwd: Path) -> list[str]:
    codex = config["codex"]
    executable = provider_command(config, "codex")
    sandbox = str(codex.get("sandbox") or "workspace-write")
    if sandbox not in {"read-only", "workspace-write", "danger-full-access"}:
        raise ValueError(f"unsupported Codex sandbox mode: {sandbox}")
    command = [
        executable, "exec", "--json", "--skip-git-repo-check",
        "--cd", str(cwd), "--sandbox", sandbox,
    ]
    reasoning_effort = codex.get("reasoning_effort")
    if reasoning_effort is not None:
        reasoning_effort = str(reasoning_effort).strip().lower()
        if reasoning_effort not in {"none", "low", "medium", "high", "xhigh", "max"}:
            raise ValueError(f"unsupported Codex reasoning effort: {reasoning_effort}")
        command += ["--config", f'model_reasoning_effort="{reasoning_effort}"']
    context_limit = codex.get("context_window_limit_tokens")
    if context_limit is None:
        context_limit = model_context_window(config, "codex", codex.get("model"))
    if context_limit is not None:
        try:
            context_limit = int(context_limit)
        except (TypeError, ValueError) as error:
            raise ValueError("Codex context window limit must be a positive integer") from error
        if context_limit <= 0:
            raise ValueError("Codex context window limit must be a positive integer")
        command += ["--config", f"model_context_window={context_limit}"]
    for path in codex_additional_write_dirs(config):
        if path != cwd:
            command += ["--add-dir", str(path)]
    if codex.get("model"):
        command += ["--model", codex["model"]]
    if conversation.provider_session_id:
        # Keep invocation-level workspace policy in front of the subcommand.
        # Otherwise a resumed thread silently falls back to its old filesystem
        # roots even though PilferedParrot launches the process from the current cwd.
        command += ["resume", conversation.provider_session_id, "-"]
    else:
        command.append("-")
    return command


def capture_codex(
    prompt: str,
    cwd: Path,
    conversation: Conversation,
    config: dict[str, Any],
    cancel_event: threading.Event | None = None,
    on_progress: ProgressCallback | None = None,
) -> RunResult:
    command = _codex_command(conversation, config, cwd)
    final_message = ""
    plain_output: list[str] = []
    input_tokens: int | None = None
    output_tokens: int | None = None

    def receive(line: str) -> None:
        nonlocal final_message, input_tokens, output_tokens
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            plain_output.append(line.rstrip())
            if on_progress and line.strip():
                on_progress("output", line.rstrip())
            return
        if event.get("type") == "thread.started" and event.get("thread_id"):
            conversation.provider_session_id = event["thread_id"]
        message = _codex_message(event)
        if message:
            final_message = str(message)
        if event.get("type") == "turn.completed" and isinstance(event.get("usage"), dict):
            usage = event["usage"]
            input_tokens = _token_count(usage.get("input_tokens"))
            output_tokens = _token_count(usage.get("output_tokens"))
        if on_progress:
            _report_codex_event(event, on_progress)

    completed = _stream_process(
        command, prompt, cwd,
        cancel_event=cancel_event,
        timeout_seconds=float(config["codex"].get("request_timeout_seconds", 1800)),
        stdout_line=receive,
    )
    error = completed.stderr.strip()
    text = final_message or "\n".join(filter(None, plain_output))
    return RunResult(
        text, completed.returncode, conversation.provider_session_id, error or None,
        unavailable=completed.returncode != 0 and not text and _looks_unavailable(error),
        input_tokens=input_tokens, output_tokens=output_tokens,
    )


def _report_codex_event(event: dict[str, Any], report: ProgressCallback) -> None:
    event_type = str(event.get("type") or "")
    item = event.get("item") if isinstance(event.get("item"), dict) else {}
    item_type = str(item.get("type") or "")
    if event_type == "turn.started":
        report("status", "Started working")
    elif item_type == "reasoning" and event_type == "item.completed":
        text = item.get("text") or item.get("summary") or item.get("content")
        if text:
            report("commentary", str(text))
    elif item_type == "command_execution":
        command = " ".join(str(item.get("command") or "command").split())[:300]
        if event_type == "item.started":
            report("tool", f"Running: {command}")
        elif event_type == "item.completed":
            code = item.get("exit_code")
            report("tool_result", f"Command finished{f' (exit {code})' if code is not None else ''}: {command}")
    elif item_type in ("file_change", "file_changes") and event_type == "item.completed":
        report("tool_result", "Applied file changes")
    elif item_type in ("mcp_tool_call", "web_search", "image_generation"):
        name = item.get("tool") or item.get("name") or item_type.replace("_", " ")
        if event_type == "item.started":
            report("tool", f"Using {name}")
        elif event_type == "item.completed":
            report("tool_result", f"Finished {name}")


def capture_dispatch(
    provider: str,
    prompt: str,
    cwd: Path,
    conversation: Conversation,
    config: dict[str, Any],
    cancel_event: threading.Event | None = None,
    on_progress: ProgressCallback | None = None,
) -> RunResult:
    _check_cancelled(cancel_event)
    if on_progress is None and cancel_event is not None:
        candidate = getattr(cancel_event, "_pilferedparrot_progress", None)
        if callable(candidate):
            on_progress = candidate
    conversation.provider = provider
    if provider == "qwen":
        text = run_qwen_agent(
            prompt, conversation.qwen_messages, config, cwd,
            cancel_event=cancel_event, on_progress=on_progress,
            token_usage=conversation.token_usage,
        )
        _check_cancelled(cancel_event)
        return RunResult(
            text, 0,
            input_tokens=conversation.token_usage.get("input_tokens"),
            output_tokens=conversation.token_usage.get("output_tokens"),
        )
    if provider == "codex":
        return capture_codex(prompt, cwd, conversation, config, cancel_event, on_progress)
    raise ValueError(f"unknown provider: {provider}")


def dispatch(
    provider: str,
    prompt: str,
    cwd: Path,
    conversation: Conversation,
    config: dict[str, Any],
) -> int:
    conversation.provider = provider
    if provider == "qwen":
        run_qwen_agent(prompt, conversation.qwen_messages, config, cwd)
        return 0
    if provider == "codex":
        return dispatch_codex(prompt, cwd, conversation, config)
    raise ValueError(f"unknown provider: {provider}")
