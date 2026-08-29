from __future__ import annotations

import json
import os
import select
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from .config import expanded_path, resolve_command
from .model import (
    STATUS_AUTH_UNVERIFIED,
    STATUS_CLI_MISSING,
    STATUS_SIGNED_OUT,
    BudgetWindow,
    ProviderBudget,
)


def _number(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) else None


def claude_budget_from_payload(payload: dict[str, Any], observed_at: int | None = None) -> ProviderBudget:
    five_hour = ((payload.get("rate_limits") or {}).get("five_hour") or {})
    used = _number(five_hour.get("used_percentage"))
    if used is None:
        return ProviderBudget("claude", True, observed_at=observed_at,
                              note="Claude has not emitted a five-hour budget snapshot yet")
    resets_at = five_hour.get("resets_at") or five_hour.get("reset_at")
    if isinstance(resets_at, float):
        resets_at = int(resets_at)
    if not isinstance(resets_at, int):
        resets_at = None
    return ProviderBudget(
        "claude",
        True,
        BudgetWindow(used_percent=used, window_minutes=300, resets_at=resets_at),
        observed_at=observed_at,
    )


def probe_claude_auth(command: str) -> tuple[bool | None, str]:
    """Ask the CLI whether it is signed in.

    Returns (logged_in, detail). `None` means the probe could not tell -- which is
    NOT the same as signed in. Every caller must keep those two apart: a probe that
    collapses `unknown` into `yes` routes real work to a lane that cannot run it.
    """
    try:
        auth = subprocess.run(
            [command, "auth", "status"], capture_output=True, text=True, timeout=5,
        )
    except subprocess.TimeoutExpired:
        return None, "`claude auth status` timed out after 5s"
    except OSError as error:
        return None, f"could not run `claude auth status`: {error}"

    if auth.returncode != 0:
        first = ((auth.stderr or auth.stdout) or "").strip().splitlines()
        return None, f"`claude auth status` exited {auth.returncode}: {first[0] if first else 'no output'}"
    try:
        payload = json.loads(auth.stdout)
    except json.JSONDecodeError:
        return None, "`claude auth status` did not return JSON"
    if not isinstance(payload, dict) or "loggedIn" not in payload:
        return None, "`claude auth status` JSON has no `loggedIn` field"
    return bool(payload["loggedIn"]), str(payload.get("authMethod") or "unknown")


def _missing_note(provider: str, config: dict[str, Any]) -> str:
    """Say what we looked for and how to fix it -- "not installed" is often a lie.

    The usual cause is a launcher with a minimal PATH, not a missing install, so
    pointing at the config knob beats implying the user needs to reinstall.
    """
    name = config[provider]["command"]
    if os.sep in name or name.startswith("~"):
        return f"{provider}.command points at {name}, which is not an executable file"
    return (f"`{name}` was not found on PATH or in the usual install locations; "
            f"set {provider}.command in config.json to its full path")


def read_claude_budget(config: dict[str, Any]) -> ProviderBudget:
    command = resolve_command(config, "claude")
    if command is None:
        return ProviderBudget("claude", False, status=STATUS_CLI_MISSING,
                              note=_missing_note("claude", config))

    logged_in, detail = probe_claude_auth(command)
    if logged_in is False:
        return ProviderBudget("claude", False, status=STATUS_SIGNED_OUT,
                              note="signed out -- run `claude auth login`")
    if logged_in is None:
        # Fail CLOSED, and name the blind spot rather than swallowing it. Excluding
        # Claude costs one suboptimal route; including it on a guess costs the turn.
        return ProviderBudget("claude", False, status=STATUS_AUTH_UNVERIFIED,
                              note=f"auth unverifiable ({detail})")

    # Auth is proven from here down, so a missing/unreadable budget cache means the
    # BUDGET is unknown -- never that the lane is dead.
    path = expanded_path(config["claude"]["budget_cache"])
    try:
        with path.open(encoding="utf-8") as handle:
            cached = json.load(handle)
    except FileNotFoundError:
        return ProviderBudget("claude", True, note="waiting for Claude statusline telemetry")
    except (OSError, json.JSONDecodeError) as error:
        return ProviderBudget("claude", True,
                              note=f"telemetry cache unreadable ({type(error).__name__})")
    try:
        observed = int(cached.get("observed_at", path.stat().st_mtime))
        result = claude_budget_from_payload(cached.get("status", cached), observed)
    except (OSError, ValueError, TypeError, AttributeError) as error:
        return ProviderBudget("claude", True,
                              note=f"telemetry cache malformed ({type(error).__name__})")
    age = max(0, int(time.time()) - observed)
    stale = int(config["claude"].get("budget_stale_seconds", 600))
    if age > stale:
        return ProviderBudget(result.provider, result.available, result.window, observed,
                              f"snapshot is {age // 60} minutes old")
    return result


def codex_budget_from_response(result: dict[str, Any], observed_at: int | None = None) -> ProviderBudget:
    limits = result.get("rateLimits") or {}
    primary = limits.get("primary") or {}
    used = _number(primary.get("usedPercent"))
    if used is None:
        return ProviderBudget("codex", True, observed_at=observed_at,
                              note="Codex returned no primary rate-limit window")
    duration = primary.get("windowDurationMins")
    reset = primary.get("resetsAt")
    return ProviderBudget(
        "codex",
        True,
        BudgetWindow(
            used_percent=used,
            window_minutes=duration if isinstance(duration, int) else None,
            resets_at=reset if isinstance(reset, int) else None,
        ),
        observed_at=observed_at,
    )


def read_codex_budget(config: dict[str, Any]) -> ProviderBudget:
    command = resolve_command(config, "codex")
    if command is None:
        return ProviderBudget("codex", False, status=STATUS_CLI_MISSING,
                              note=_missing_note("codex", config))
    try:
        auth = subprocess.run(
            [command, "login", "status"], capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return ProviderBudget(
            "codex", False, status=STATUS_AUTH_UNVERIFIED,
            note=f"auth unverifiable (`codex login status` failed: {error})",
        )
    auth_detail = ((auth.stdout or auth.stderr) or "").strip()
    if auth.returncode != 0:
        signed_out = "not logged in" in auth_detail.lower()
        return ProviderBudget(
            "codex", False,
            status=STATUS_SIGNED_OUT if signed_out else STATUS_AUTH_UNVERIFIED,
            note="signed out -- run `codex login`" if signed_out else
                 f"auth unverifiable (`codex login status` exited {auth.returncode})",
        )
    timeout = float(config["codex"].get("budget_timeout_seconds", 8))
    proc: subprocess.Popen[str] | None = None
    try:
        proc = subprocess.Popen(
            [command, "app-server", "--listen", "stdio://"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
        )
        assert proc.stdin is not None and proc.stdout is not None
        messages = [
            {"method": "initialize", "id": 0, "params": {"clientInfo": {
                "name": "ai_conductor", "title": "AI Conductor", "version": "0.3.0"}}},
            {"method": "initialized", "params": {}},
            {"method": "account/rateLimits/read", "id": 1, "params": {}},
        ]
        for message in messages:
            proc.stdin.write(json.dumps(message, separators=(",", ":")) + "\n")
        proc.stdin.flush()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            ready, _, _ = select.select([proc.stdout], [], [], max(0, deadline - time.monotonic()))
            if not ready:
                break
            line = proc.stdout.readline()
            if not line:
                break
            message = json.loads(line)
            if message.get("id") == 1:
                if message.get("error"):
                    raise RuntimeError(message["error"].get("message", "rate-limit request failed"))
                return codex_budget_from_response(message.get("result") or {}, int(time.time()))
        raise TimeoutError("Codex budget probe timed out")
    except Exception as exc:
        return ProviderBudget("codex", True, note=f"budget unavailable: {exc}")
    finally:
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()


def qwen_available(config: dict[str, Any]) -> bool:
    try:
        with urllib.request.urlopen(config["qwen"]["health_url"], timeout=2) as response:
            return 200 <= response.status < 300
    except (OSError, urllib.error.URLError):
        return False


def collect_budgets(config: dict[str, Any]) -> dict[str, ProviderBudget]:
    running = qwen_available(config)
    start_command = config["qwen"].get("start_command") or []
    can_start = bool(
        config["qwen"].get("auto_start", True)
        and start_command
        and Path(start_command[0]).expanduser().exists()
    )
    qwen_note = "local; no subscription quota"
    if not running and can_start:
        qwen_note += "; stopped and will auto-start"
    return {
        "qwen": ProviderBudget("qwen", running or can_start, note=qwen_note),
        "claude": read_claude_budget(config),
        "codex": read_codex_budget(config),
    }


def write_claude_cache(payload: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    wrapper = {"observed_at": int(time.time()), "status": payload}
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(wrapper, handle, separators=(",", ":"))
        handle.write("\n")
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)
