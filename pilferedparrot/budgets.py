from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any
from concurrent.futures import ThreadPoolExecutor
from queue import Empty, Queue

from . import __version__
from .config import (
    compatible_api_headers, open_compatible_url, open_qwen_url,
    qwen_endpoint_is_loopback, resolve_command, validate_qwen_endpoints,
)
from .model import (
    AUTH_LOCAL_NO_AUTH,
    AUTH_SIGNED_IN,
    AUTH_SIGNED_OUT,
    AUTH_UNKNOWN,
    REACHABLE,
    STATUS_AUTH_UNVERIFIED,
    STATUS_CLI_MISSING,
    STATUS_OK,
    STATUS_SIGNED_OUT,
    UNREACHABLE,
    USAGE_AVAILABLE,
    USAGE_UNAVAILABLE,
    USAGE_UNSUPPORTED,
    BudgetWindow,
    ProviderBudget,
    provider_ids,
)


CLAUDE_USAGE_UNSUPPORTED_NOTE = (
    "Live allowance unavailable. Claude does not provide a supported live plan-allowance interface."
)


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def _epoch(value: Any) -> int | None:
    if isinstance(value, (int, float)):
        return int(value)
    if not isinstance(value, str):
        return None
    try:
        return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())
    except ValueError:
        return None


def _window_label(minutes: Any) -> str | None:
    if not isinstance(minutes, int):
        return None
    if minutes == 300:
        return "5-hour included usage"
    if minutes == 10080:
        return "Weekly included usage"
    if minutes % 1440 == 0:
        return f"{minutes // 1440}-day included usage"
    if minutes % 60 == 0:
        return f"{minutes // 60}-hour included usage"
    return f"{minutes}-minute included usage"


def _limiting_window(windows: list[BudgetWindow]) -> BudgetWindow | None:
    return max(windows, key=lambda window: window.used_percent) if windows else None


def _missing_note(provider: str, config: dict[str, Any]) -> str:
    name = config[provider]["command"]
    if os.sep in name or name.startswith("~"):
        return f"{provider}.command points at {name}, which is not an executable file"
    return (f"`{name}` was not found on PATH or in the usual install locations; "
            f"set {provider}.command in config.json to its full path")


def codex_budget_from_response(
    result: dict[str, Any], observed_at: int | None = None,
) -> ProviderBudget:
    """Normalize every included-usage window reported by Codex.

    Codex reports percentage used. PilferedParrot derives percentage left, but does
    not describe that figure as a percentage of the subscription.
    """
    by_id = result.get("rateLimitsByLimitId")
    snapshots: list[tuple[str | None, dict[str, Any]]] = []
    if isinstance(by_id, dict):
        snapshots = [
            (str(limit_id), raw) for limit_id, raw in sorted(by_id.items())
            if isinstance(raw, dict)
        ]
    if not snapshots:
        limits = result.get("rateLimits") or {}
        if isinstance(limits, dict):
            snapshots = [(None, limits)]

    windows: list[BudgetWindow] = []
    multiple_buckets = len(snapshots) > 1
    for limit_id, limits in snapshots:
        bucket = limits.get("limitName")
        if not isinstance(bucket, str) or not bucket.strip():
            bucket = limit_id.replace("_", " ").strip().title() if limit_id else None
        else:
            bucket = bucket.strip()
        for key in ("primary", "secondary"):
            raw = limits.get(key)
            if not isinstance(raw, dict):
                continue
            used = _number(raw.get("usedPercent"))
            if used is None:
                continue
            duration = raw.get("windowDurationMins")
            label = _window_label(duration)
            if multiple_buckets and bucket:
                label = f"{bucket} · {label or key.title()}"
            windows.append(BudgetWindow(
                used_percent=max(0.0, min(100.0, used)),
                window_minutes=duration if isinstance(duration, int) else None,
                resets_at=_epoch(raw.get("resetsAt")),
                label=label,
            ))

    window = _limiting_window(windows)
    if window is None:
        return ProviderBudget(
            "codex", True, observed_at=observed_at,
            note="Codex returned no included-usage window",
            auth_status=AUTH_SIGNED_IN, reachability=REACHABLE,
            usage_status=USAGE_UNAVAILABLE,
        )
    return ProviderBudget(
        "codex", True, window, observed_at=observed_at,
        note=f"live Codex allowance; limiting window: {window.label or 'included usage'}",
        windows=tuple(windows),
        auth_status=AUTH_SIGNED_IN, reachability=REACHABLE,
        usage_status=USAGE_AVAILABLE,
    )


def read_codex_budget(config: dict[str, Any]) -> ProviderBudget:
    command = resolve_command(config, "codex")
    if command is None:
        return ProviderBudget(
            "codex", False, status=STATUS_CLI_MISSING,
            note=_missing_note("codex", config),
            auth_status=AUTH_UNKNOWN, reachability=UNREACHABLE,
            usage_status=USAGE_UNAVAILABLE,
        )
    try:
        auth = subprocess.run(
            [command, "login", "status"], capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return ProviderBudget(
            "codex", False, status=STATUS_AUTH_UNVERIFIED,
            note=f"auth unverifiable (`codex login status` failed: {error})",
            auth_status=AUTH_UNKNOWN, reachability=UNREACHABLE,
            usage_status=USAGE_UNAVAILABLE,
        )
    auth_detail = ((auth.stdout or auth.stderr) or "").strip()
    if auth.returncode != 0:
        signed_out = "not logged in" in auth_detail.lower()
        return ProviderBudget(
            "codex", False,
            status=STATUS_SIGNED_OUT if signed_out else STATUS_AUTH_UNVERIFIED,
            note="signed out -- run `codex login`" if signed_out else
                 f"auth unverifiable (`codex login status` exited {auth.returncode})",
            auth_status=AUTH_SIGNED_OUT if signed_out else AUTH_UNKNOWN,
            reachability=UNREACHABLE,
            usage_status=USAGE_UNAVAILABLE,
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
        initialize = {"method": "initialize", "id": 0, "params": {"clientInfo": {
            "name": "pilferedparrot", "title": "PilferedParrot", "version": __version__}}}
        proc.stdin.write(json.dumps(initialize, separators=(",", ":")) + "\n")
        proc.stdin.flush()
        deadline = time.monotonic() + timeout
        initialized = False
        responses: Queue[str | None] = Queue()

        def read_responses() -> None:
            assert proc is not None and proc.stdout is not None
            try:
                for response_line in proc.stdout:
                    responses.put(response_line)
            finally:
                responses.put(None)

        threading.Thread(
            target=read_responses, name="pilferedparrot-codex-budget", daemon=True,
        ).start()
        while time.monotonic() < deadline:
            try:
                line = responses.get(timeout=max(0, deadline - time.monotonic()))
            except Empty:
                break
            if line is None:
                break
            message = json.loads(line)
            if message.get("id") == 0:
                if message.get("error"):
                    raise RuntimeError(message["error"].get("message", "initialize failed"))
                proc.stdin.write(json.dumps({"method": "initialized"}) + "\n")
                proc.stdin.write(json.dumps({
                    "method": "account/rateLimits/read", "id": 1,
                }, separators=(",", ":")) + "\n")
                proc.stdin.flush()
                initialized = True
                continue
            if message.get("id") == 1:
                if message.get("error"):
                    raise RuntimeError(message["error"].get("message", "rate-limit request failed"))
                return codex_budget_from_response(message.get("result") or {}, int(time.time()))
        if proc.poll() is not None:
            raise RuntimeError(f"Codex app server exited {proc.returncode}")
        phase = "rate-limit request" if initialized else "initialization"
        raise TimeoutError(f"Codex budget probe timed out during {phase}")
    except Exception as exc:
        return ProviderBudget(
            "codex", False, note=f"Codex endpoint unreachable: {exc}",
            # `codex login status` succeeded above. Keep that independently
            # verified fact even when the app-server allowance probe fails.
            auth_status=AUTH_SIGNED_IN, reachability=UNREACHABLE,
            usage_status=USAGE_UNAVAILABLE,
        )
    finally:
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=1)


def read_claude_status(config: dict[str, Any]) -> ProviderBudget:
    """Report only the non-secret authentication state owned by Claude Code."""
    command = resolve_command(config, "claude")
    if command is None:
        return ProviderBudget(
            "claude", False, status=STATUS_CLI_MISSING,
            note=_missing_note("claude", config),
            auth_status=AUTH_UNKNOWN, reachability=UNREACHABLE,
            usage_status=USAGE_UNSUPPORTED, usage_note=CLAUDE_USAGE_UNSUPPORTED_NOTE,
        )
    try:
        completed = subprocess.run(
            [command, "auth", "status", "--json"], capture_output=True,
            text=True, timeout=5,
        )
    except subprocess.TimeoutExpired:
        return ProviderBudget(
            "claude", False, status=STATUS_AUTH_UNVERIFIED,
            note="auth unverifiable (`claude auth status` timed out)",
            auth_status=AUTH_UNKNOWN, reachability=UNREACHABLE,
            usage_status=USAGE_UNSUPPORTED, usage_note=CLAUDE_USAGE_UNSUPPORTED_NOTE,
        )
    except OSError:
        return ProviderBudget(
            "claude", False, status=STATUS_AUTH_UNVERIFIED,
            note="auth unverifiable (`claude auth status` could not be run)",
            auth_status=AUTH_UNKNOWN, reachability=UNREACHABLE,
            usage_status=USAGE_UNSUPPORTED, usage_note=CLAUDE_USAGE_UNSUPPORTED_NOTE,
        )
    # The supported JSON contract exposes ``loggedIn``.  Do not inspect or
    # retain account details, error prose, stdout, or stderr.  In particular,
    # the contract has no reliable token-expiration state, so every nonzero
    # result is unknown even if its untrusted payload resembles signed-out JSON.
    if completed.returncode != 0:
        return ProviderBudget(
            "claude", False, status=STATUS_AUTH_UNVERIFIED,
            note=f"auth unverifiable (`claude auth status` exited {completed.returncode})",
            auth_status=AUTH_UNKNOWN, reachability=UNREACHABLE,
            usage_status=USAGE_UNSUPPORTED, usage_note=CLAUDE_USAGE_UNSUPPORTED_NOTE,
        )
    try:
        parsed = json.loads(completed.stdout or "{}")
    except json.JSONDecodeError:
        parsed = {}
    payload = parsed if isinstance(parsed, dict) else {}
    logged_in = payload.get("loggedIn") is True
    if logged_in:
        return ProviderBudget(
            "claude", True,
            note="Claude CLI reports signed in",
            auth_status=AUTH_SIGNED_IN, reachability=REACHABLE,
            usage_status=USAGE_UNSUPPORTED, usage_note=CLAUDE_USAGE_UNSUPPORTED_NOTE,
        )
    if payload.get("loggedIn") is False:
        return ProviderBudget(
            "claude", False, status=STATUS_SIGNED_OUT,
            note="signed out -- use Sign in to Claude",
            auth_status=AUTH_SIGNED_OUT, reachability=UNREACHABLE,
            usage_status=USAGE_UNSUPPORTED, usage_note=CLAUDE_USAGE_UNSUPPORTED_NOTE,
        )
    return ProviderBudget(
        "claude", False, status=STATUS_AUTH_UNVERIFIED,
        note="auth unverifiable (`claude auth status` returned an invalid result)",
        auth_status=AUTH_UNKNOWN, reachability=UNREACHABLE,
        usage_status=USAGE_UNSUPPORTED, usage_note=CLAUDE_USAGE_UNSUPPORTED_NOTE,
    )


def qwen_available(config: dict[str, Any]) -> bool:
    validate_qwen_endpoints(config)
    try:
        with open_qwen_url(config, config["qwen"]["health_url"], timeout=2) as response:
            return 200 <= response.status < 300
    except urllib.error.HTTPError as error:
        error.close()
        return False
    except (OSError, urllib.error.URLError):
        return False


def _start_command_available(value: Any) -> bool:
    if not isinstance(value, list) or not value or not isinstance(value[0], str):
        return False
    command = value[0].strip()
    if not command:
        return False
    if os.sep in command or command.startswith("~"):
        path = Path(command).expanduser()
        return path.is_file() and os.access(path, os.X_OK)
    return shutil.which(command) is not None


def read_compatible_status(provider: str, config: dict[str, Any]) -> ProviderBudget:
    """Probe the standard model-list route without spending a model turn."""
    provider_config = config.get(provider) or {}
    base_url = str(provider_config.get("base_url") or "").rstrip("/")
    try:
        request = urllib.request.Request(
            base_url + "/models",
            headers=compatible_api_headers(config, provider),
        )
        with open_compatible_url(config, provider, request, timeout=3) as response:
            reachable = 200 <= response.status < 300
        return ProviderBudget(
            provider, reachable,
            note="OpenAI-compatible endpoint is ready" if reachable else
                 f"endpoint returned HTTP {response.status}",
            auth_status=AUTH_SIGNED_IN if provider_config.get("api_key_env") else
                        AUTH_LOCAL_NO_AUTH,
            reachability=REACHABLE if reachable else UNREACHABLE,
        )
    except RuntimeError as error:
        return ProviderBudget(
            provider, False, status=STATUS_SIGNED_OUT, note=str(error),
            auth_status=AUTH_SIGNED_OUT, reachability=UNREACHABLE,
        )
    except urllib.error.HTTPError as error:
        code = error.code
        error.close()
        auth_error = code in {401, 403}
        return ProviderBudget(
            provider, False,
            status=STATUS_SIGNED_OUT if auth_error else STATUS_AUTH_UNVERIFIED,
            note=f"endpoint returned HTTP {code}",
            auth_status=AUTH_SIGNED_OUT if auth_error else AUTH_UNKNOWN,
            reachability=UNREACHABLE,
        )
    except (OSError, ValueError, urllib.error.URLError) as error:
        return ProviderBudget(
            provider, False, note=f"endpoint unavailable: {error}",
            auth_status=AUTH_UNKNOWN, reachability=UNREACHABLE,
        )


def collect_budgets(config: dict[str, Any]) -> dict[str, ProviderBudget]:
    active = provider_ids(config)
    def probe(provider: str) -> ProviderBudget:
        if provider == "qwen":
            return qwen_status()
        if provider == "codex":
            return read_codex_budget(config)
        if provider == "claude":
            return read_claude_status(config)
        if provider == "gemini":
            from .adapters import adapter_for
            adapter = adapter_for("gemini", config)
            installed = adapter.available()
            authenticated = adapter.authentication() if installed else None
            return ProviderBudget(
                "gemini", installed and authenticated is not False,
                status=STATUS_OK if installed else STATUS_CLI_MISSING,
                note=("Gemini CLI is ready" if authenticated is True else
                      "Gemini CLI found; sign-in will be checked when used" if installed else
                      "Gemini CLI not found"),
                auth_status=AUTH_SIGNED_IN if authenticated is True else
                            AUTH_SIGNED_OUT if authenticated is False else AUTH_UNKNOWN,
                reachability=REACHABLE if installed and authenticated is True else UNREACHABLE,
            )
        return read_compatible_status(provider, config)

    def qwen_status() -> ProviderBudget:
        qwen_error: str | None = None
        try:
            running = qwen_available(config)
        except ValueError as error:
            running = False
            qwen_error = str(error)
        can_start = bool(
            qwen_error is None
            and config["qwen"].get("auto_start") is True
            and _start_command_available(config["qwen"].get("start_command"))
        )
        local_qwen = all(qwen_endpoint_is_loopback(config["qwen"].get(key, "")) for key in (
            "base_url", "health_url",
        ))
        qwen_note = qwen_error or ("running locally" if running else "local server stopped")
        if not running and can_start:
            qwen_note += "; starts automatically when selected"
        return ProviderBudget(
            "qwen", running or can_start, note=qwen_note,
            auth_status=AUTH_LOCAL_NO_AUTH if local_qwen else AUTH_UNKNOWN,
            reachability=REACHABLE if running else UNREACHABLE,
        )
    if not active:
        return {}
    with ThreadPoolExecutor(
        max_workers=min(8, len(active)), thread_name_prefix="pilferedparrot-budget",
    ) as executor:
        futures = {provider: executor.submit(probe, provider) for provider in active}
        return {provider: futures[provider].result() for provider in active}
