from __future__ import annotations

import json
import os
import select
import subprocess
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any

from . import __version__
from .config import resolve_command
from .model import (
    STATUS_AUTH_UNVERIFIED,
    STATUS_CLI_MISSING,
    STATUS_SIGNED_OUT,
    BudgetWindow,
    ProviderBudget,
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
        )
    return ProviderBudget(
        "codex", True, window, observed_at=observed_at,
        note=f"live Codex allowance; limiting window: {window.label or 'included usage'}",
        windows=tuple(windows),
    )


def read_codex_budget(config: dict[str, Any]) -> ProviderBudget:
    command = resolve_command(config, "codex")
    if command is None:
        return ProviderBudget(
            "codex", False, status=STATUS_CLI_MISSING,
            note=_missing_note("codex", config),
        )
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
        initialize = {"method": "initialize", "id": 0, "params": {"clientInfo": {
            "name": "pilferedparrot", "title": "PilferedParrot", "version": __version__}}}
        proc.stdin.write(json.dumps(initialize, separators=(",", ":")) + "\n")
        proc.stdin.flush()
        deadline = time.monotonic() + timeout
        initialized = False
        while time.monotonic() < deadline:
            ready, _, _ = select.select([proc.stdout], [], [], max(0, deadline - time.monotonic()))
            if not ready:
                break
            line = proc.stdout.readline()
            if not line:
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
        return ProviderBudget("codex", True, note=f"included usage unavailable: {exc}")
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
    qwen_note = "running locally" if running else "local server stopped"
    if not running and can_start:
        qwen_note += "; starts automatically when selected"
    return {
        "qwen": ProviderBudget("qwen", running or can_start, note=qwen_note),
        "codex": read_codex_budget(config),
    }
