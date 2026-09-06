"""Safe, portable subprocess helpers for provider CLIs."""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path


_NPM_PACKAGES = {
    "codex": ("@openai/codex", "codex"),
    "gemini": ("@google/gemini-cli", "gemini"),
    "claude": ("@anthropic-ai/claude-code", "claude"),
}


def _windows() -> bool:
    return sys.platform == "win32"


def _npm_entry(shim: Path, executable: str) -> Path | None:
    package_info = _NPM_PACKAGES.get(executable.lower())
    if package_info is None:
        return None
    package, _ = package_info
    # npm's global bin is adjacent to its node_modules directory. Local npm
    # shims live in node_modules/.bin, so the package is a sibling of .bin.
    # Keep these layouts explicit: searching arbitrary ancestors could select
    # an unrelated package from a parent checkout.
    package_dirs = (
        shim.parent / "node_modules" / Path(package),
        shim.parent.parent / Path(package),
    )
    for package_dir in package_dirs:
        manifest = package_dir / "package.json"
        try:
            payload = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            continue
        if not isinstance(payload, dict):
            continue
        if payload.get("name") != package:
            continue
        bins = payload.get("bin")
        entry = bins.get(executable) if isinstance(bins, dict) else bins
        if not isinstance(entry, str) or not entry.strip():
            continue
        package_root = package_dir.resolve()
        candidate = (package_root / entry).resolve()
        try:
            candidate.relative_to(package_root)
        except ValueError:
            continue
        if candidate.is_file():
            return candidate
    return None


def provider_argv(command: list[str]) -> list[str]:
    """Return an argv safe for direct provider execution on this platform.

    Windows npm installs expose ``.cmd`` shims. Only the three supported
    provider packages are translated to ``node.exe`` plus their package entry;
    arbitrary batch files are rejected instead of being passed through a shell.
    """
    if not command or not _windows():
        return list(command)
    first = Path(command[0])
    suffix = first.suffix.lower()
    if suffix not in {".cmd", ".bat"}:
        return list(command)
    entry = _npm_entry(first, first.stem)
    if entry is None:
        raise RuntimeError(
            f"Unsupported Windows CLI shim `{command[0]}`; install a native .exe "
            "or the supported npm provider package"
        )
    node = shutil.which("node.exe") or shutil.which("node")
    if not node:
        raise RuntimeError("Node.js is required to run the supported npm provider CLI")
    return [node, str(entry), *command[1:]]
