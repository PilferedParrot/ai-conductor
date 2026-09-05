"""Best-effort update checks for provider command-line integrations.

The checker deliberately has no install side effects.  It only inspects the
configured executable and the public npm registry, and any failure is reported
as an unavailable check so opening a session is never blocked by the network.
"""
from __future__ import annotations

import re
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

from .config import resolve_command
from .processes import provider_argv


_TIMEOUT_SECONDS = 5


@dataclass(frozen=True)
class _ProviderPackage:
    package: str
    executable: str

    @property
    def registry_url(self) -> str:
        return f"https://registry.npmjs.org/{self.package}/latest"

    @property
    def update_command(self) -> str:
        if self.executable == "claude":
            return "claude update"
        return f"npm install -g {self.package}"


_PACKAGES = {
    "codex": _ProviderPackage("@openai/codex", "codex"),
    "claude": _ProviderPackage("@anthropic-ai/claude-code", "claude"),
    "gemini": _ProviderPackage("@google/gemini-cli", "gemini"),
}

# A complete semver version is required.  This avoids treating a date, a
# vendor build identifier, or an arbitrary CLI message as a version.
_VERSION_RE = re.compile(
    r"(?<![0-9A-Za-z-])v?(0|[1-9][0-9]*)\."
    r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
    r"(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?(?![0-9A-Za-z-])"
)


def _version(value: Any) -> tuple[int, int, int, tuple[tuple[int, Any], ...]] | None:
    """Extract one semver from CLI text, or parse a registry version exactly."""
    text = value.decode("utf-8", "replace") if isinstance(value, bytes) else str(value or "")
    match = _VERSION_RE.search(text)
    if match is None:
        return None
    prerelease = match.group(4)
    parts: list[tuple[int, Any]] = []
    if prerelease:
        for item in prerelease.split("."):
            if item.isdigit():
                # Numeric identifiers have lower precedence than nonnumeric
                # identifiers, and leading zeroes are invalid semver.
                if len(item) > 1 and item.startswith("0"):
                    return None
                parts.append((0, int(item)))
            else:
                parts.append((1, item))
    return int(match.group(1)), int(match.group(2)), int(match.group(3)), tuple(parts)


def _registry_version(value: Any) -> tuple[int, int, int, tuple[tuple[int, Any], ...]] | None:
    """Parse the registry tag strictly; unlike CLI output it must be only a version."""
    text = value.decode("utf-8", "replace") if isinstance(value, bytes) else str(value or "")
    if _VERSION_RE.fullmatch(text.strip()) is None:
        return None
    return _version(text.strip())


def _compare(left: tuple[int, int, int, tuple[tuple[int, Any], ...]],
             right: tuple[int, int, int, tuple[tuple[int, Any], ...]]) -> int:
    if left[:3] != right[:3]:
        return (left[:3] > right[:3]) - (left[:3] < right[:3])
    left_pre, right_pre = left[3], right[3]
    if not left_pre or not right_pre:
        return (not left_pre) - (not right_pre)
    for left_item, right_item in zip(left_pre, right_pre):
        if left_item != right_item:
            # Numeric identifiers sort before string identifiers.
            return (left_item > right_item) - (left_item < right_item)
    return (len(left_pre) > len(right_pre)) - (len(left_pre) < len(right_pre))


def _result(status: str, *, installed: str | None = None,
            latest: str | None = None, message: str = "",
            update_command: str | None = None) -> dict[str, Any]:
    return {
        "status": status,
        "installed_version": installed,
        "latest_version": latest,
        "message": message,
        "update_command": update_command,
    }


def check_provider_update(config: dict[str, Any], provider: str) -> dict[str, Any]:
    """Check whether a supported provider CLI has an npm update available.

    This function is intentionally best effort: missing executables, malformed
    output, registry failures, and timeouts all return a normal result rather
    than raising into session startup.
    """
    package = _PACKAGES.get(provider)
    if package is None:
        message = ("Qwen updates are managed manually with your local model server."
                   if provider == "qwen" else "Updates are managed by the provider.")
        return _result("not_applicable", message=message)

    update_command = package.update_command
    try:
        executable = resolve_command(config, provider)
    except (KeyError, TypeError, ValueError, OSError):
        executable = None
    if not executable:
        return _result("unavailable", message="Provider CLI is not installed.",
                       update_command=update_command)

    try:
        completed = subprocess.run(
            provider_argv([executable, "--version"]), capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=_TIMEOUT_SECONDS, check=False,
        )
    except (OSError, RuntimeError, subprocess.SubprocessError) as error:
        return _result("unavailable", message=f"Could not read the installed CLI version: {error}.",
                       update_command=update_command)
    if completed.returncode != 0:
        return _result("unavailable", message="Could not read the installed CLI version.",
                       update_command=update_command)
    installed_semver = _version(completed.stdout)
    if installed_semver is None:
        return _result("unavailable", message="Installed CLI version is unavailable.",
                       update_command=update_command)
    installed_text = _VERSION_RE.search(
        completed.stdout.decode("utf-8", "replace") if isinstance(completed.stdout, bytes)
        else str(completed.stdout or "")
    )
    installed_version = installed_text.group(0).lstrip("v") if installed_text else None

    try:
        with urllib.request.urlopen(package.registry_url, timeout=_TIMEOUT_SECONDS) as response:
            import json
            raw_payload = response.read(1_000_001)
            if len(raw_payload) > 1_000_000:
                raise ValueError("registry response too large")
            payload = json.loads(raw_payload.decode("utf-8"))
        latest_version = payload.get("version") if isinstance(payload, dict) else None
    except (OSError, ValueError, TypeError, KeyError, urllib.error.URLError):
        return _result("unavailable", installed=installed_version,
                       message="Could not check the npm registry.",
                       update_command=update_command)
    latest_semver = _registry_version(latest_version)
    if latest_semver is None:
        return _result("unavailable", installed=installed_version,
                       message="Registry returned an invalid version.",
                       update_command=update_command)
    latest_text = str(latest_version).lstrip("v")
    status = "update_available" if _compare(installed_semver, latest_semver) < 0 else "current"
    message = (f"Update available: {installed_version} → {latest_text}."
               if status == "update_available" else f"CLI is current ({installed_version}).")
    return _result(status, installed=installed_version, latest=latest_text,
                   message=message, update_command=update_command)
