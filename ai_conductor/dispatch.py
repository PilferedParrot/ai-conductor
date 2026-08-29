from __future__ import annotations

import json
import os
import signal
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import resolve_command
from .model import Conversation
from .qwen import run_qwen_agent


@dataclass(frozen=True)
class RunResult:
    text: str
    exit_code: int
    session_id: str | None = None
    error: str | None = None
    unavailable: bool = False


class RunCancelled(RuntimeError):
    pass


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


def shared_prompt(prompt: str, config: dict[str, Any]) -> str:
    policy_path = Path(config["policy_file"]).expanduser()
    try:
        policy = policy_path.read_text(encoding="utf-8").strip()
    except OSError:
        policy = ""
    if not policy:
        return prompt
    return f"<conductor-policy>\n{policy}\n</conductor-policy>\n\n<user-request>\n{prompt}\n</user-request>"


def dispatch_claude(prompt: str, cwd: Path, conversation: Conversation, config: dict[str, Any]) -> int:
    result = capture_claude(prompt, cwd, conversation, config)
    if result.text:
        print(result.text)
    if result.error:
        print(result.error)
    return result.exit_code


def capture_claude(
    prompt: str,
    cwd: Path,
    conversation: Conversation,
    config: dict[str, Any],
    cancel_event: threading.Event | None = None,
) -> RunResult:
    claude = config["claude"]
    session_id = conversation.provider_session_id or str(uuid.uuid4())
    command = [
        provider_command(config, "claude"), "-p", "--safe-mode", "--output-format", "json",
        "--permission-mode", claude["permission_mode"], "--effort", claude["effort"],
        "--model", claude["model"],
    ]
    if conversation.provider_session_id:
        command += ["--resume", session_id]
    else:
        command += ["--session-id", session_id]
    completed = _capture_process(
        command, shared_prompt(prompt, config), cwd,
        cancel_event=cancel_event,
        timeout_seconds=float(claude.get("request_timeout_seconds", 1800)),
    )
    text = ""
    error = completed.stderr.strip() or None
    if completed.stdout.strip():
        try:
            payload = json.loads(completed.stdout)
            result = payload.get("result")
            if result:
                text = str(result)
            else:
                text = completed.stdout.rstrip()
            session_id = payload.get("session_id", session_id)
        except json.JSONDecodeError:
            text = completed.stdout.rstrip()
    if completed.returncode == 0:
        conversation.provider_session_id = session_id
    return RunResult(
        text, completed.returncode, conversation.provider_session_id, error,
        unavailable=completed.returncode != 0 and not text and _looks_unavailable(error),
    )


def _codex_message(event: dict[str, Any]) -> str | None:
    item = event.get("item") or {}
    if event.get("type") == "item.completed" and item.get("type") == "agent_message":
        return item.get("text") or item.get("content")
    if event.get("type") in ("agent_message", "message.completed"):
        return event.get("text") or event.get("message")
    return None


def dispatch_codex(prompt: str, cwd: Path, conversation: Conversation, config: dict[str, Any]) -> int:
    command = _codex_command(conversation, config, cwd)
    proc = subprocess.Popen(
        command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=None,
        text=True, cwd=cwd, bufsize=1,
    )
    assert proc.stdin is not None and proc.stdout is not None
    proc.stdin.write(shared_prompt(prompt, config))
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
    if conversation.provider_session_id:
        command = [executable, "exec", "resume", "--json", "--skip-git-repo-check"]
        if codex.get("model"):
            command += ["--model", codex["model"]]
        command += [conversation.provider_session_id, "-"]
    else:
        command = [
            executable, "exec", "--json", "--skip-git-repo-check",
            "--cd", str(cwd), "--sandbox", codex["sandbox"],
        ]
        if codex.get("model"):
            command += ["--model", codex["model"]]
        command.append("-")
    return command


def capture_codex(
    prompt: str,
    cwd: Path,
    conversation: Conversation,
    config: dict[str, Any],
    cancel_event: threading.Event | None = None,
) -> RunResult:
    command = _codex_command(conversation, config, cwd)
    completed = _capture_process(
        command, shared_prompt(prompt, config), cwd,
        cancel_event=cancel_event,
        timeout_seconds=float(config["codex"].get("request_timeout_seconds", 1800)),
    )
    messages: list[str] = []
    for line in completed.stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            messages.append(line.rstrip())
            continue
        if event.get("type") == "thread.started" and event.get("thread_id"):
            conversation.provider_session_id = event["thread_id"]
        message = _codex_message(event)
        if message:
            messages.append(str(message))
    error = completed.stderr.strip()
    text = "\n\n".join(filter(None, messages))
    return RunResult(
        text, completed.returncode, conversation.provider_session_id, error or None,
        unavailable=completed.returncode != 0 and not text and _looks_unavailable(error),
    )


def capture_dispatch(
    provider: str,
    prompt: str,
    cwd: Path,
    conversation: Conversation,
    config: dict[str, Any],
    cancel_event: threading.Event | None = None,
) -> RunResult:
    _check_cancelled(cancel_event)
    conversation.provider = provider
    if provider == "qwen":
        text = run_qwen_agent(
            shared_prompt(prompt, config), conversation.qwen_messages, config, cwd,
            cancel_event=cancel_event,
        )
        _check_cancelled(cancel_event)
        return RunResult(text, 0)
    if provider == "claude":
        return capture_claude(prompt, cwd, conversation, config, cancel_event)
    if provider == "codex":
        return capture_codex(prompt, cwd, conversation, config, cancel_event)
    raise ValueError(f"unknown provider: {provider}")


def dispatch(
    provider: str,
    prompt: str,
    cwd: Path,
    conversation: Conversation,
    config: dict[str, Any],
) -> int:
    conversation.provider = provider
    prepared = shared_prompt(prompt, config)
    if provider == "qwen":
        run_qwen_agent(prepared, conversation.qwen_messages, config, cwd)
        return 0
    if provider == "claude":
        return dispatch_claude(prompt, cwd, conversation, config)
    if provider == "codex":
        return dispatch_codex(prompt, cwd, conversation, config)
    raise ValueError(f"unknown provider: {provider}")
