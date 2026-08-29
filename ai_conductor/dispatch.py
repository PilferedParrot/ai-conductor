from __future__ import annotations

import json
import subprocess
import uuid
from pathlib import Path
from typing import Any

from .model import Conversation
from .qwen import run_qwen_agent


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
    claude = config["claude"]
    session_id = conversation.provider_session_id or str(uuid.uuid4())
    command = [
        claude["command"], "-p", "--safe-mode", "--output-format", "json",
        "--permission-mode", claude["permission_mode"], "--effort", claude["effort"],
        "--model", claude["model"],
    ]
    if conversation.provider_session_id:
        command += ["--resume", session_id]
    else:
        command += ["--session-id", session_id]
    completed = subprocess.run(
        command,
        input=shared_prompt(prompt, config),
        cwd=cwd,
        text=True,
        capture_output=True,
    )
    if completed.stdout.strip():
        try:
            payload = json.loads(completed.stdout)
            result = payload.get("result")
            if result:
                print(result)
            else:
                print(completed.stdout.rstrip())
            session_id = payload.get("session_id", session_id)
        except json.JSONDecodeError:
            print(completed.stdout.rstrip())
    if completed.stderr.strip():
        print(completed.stderr.rstrip())
    if completed.returncode == 0:
        conversation.provider_session_id = session_id
    return completed.returncode


def _codex_message(event: dict[str, Any]) -> str | None:
    item = event.get("item") or {}
    if event.get("type") == "item.completed" and item.get("type") == "agent_message":
        return item.get("text") or item.get("content")
    if event.get("type") in ("agent_message", "message.completed"):
        return event.get("text") or event.get("message")
    return None


def dispatch_codex(prompt: str, cwd: Path, conversation: Conversation, config: dict[str, Any]) -> int:
    codex = config["codex"]
    if conversation.provider_session_id:
        command = [codex["command"], "exec", "resume", "--json", "--skip-git-repo-check"]
        if codex.get("model"):
            command += ["--model", codex["model"]]
        command += [conversation.provider_session_id, "-"]
    else:
        command = [
            codex["command"], "exec", "--json", "--skip-git-repo-check",
            "--cd", str(cwd), "--sandbox", codex["sandbox"],
        ]
        if codex.get("model"):
            command += ["--model", codex["model"]]
        command.append("-")
    proc = subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=None,
        text=True,
        cwd=cwd,
        bufsize=1,
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
