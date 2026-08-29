from __future__ import annotations

import json
import os
import subprocess
import sys

from .budgets import write_claude_cache
from .config import expanded_path, load_config


def _chained_statusline(config: dict) -> list[str]:
    command = config["claude"].get("statusline_command") or []
    if not isinstance(command, list) or not command \
            or not all(isinstance(part, str) and part for part in command):
        return []
    return [os.path.expanduser(part) for part in command]


def main() -> int:
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        payload = None
    config = load_config()
    if isinstance(payload, dict):
        write_claude_cache(payload, expanded_path(config["claude"]["budget_cache"]))
    command = _chained_statusline(config)
    if command:
        completed = subprocess.run(
            command, input=raw, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        )
        if completed.stdout:
            print(completed.stdout.rstrip())
        return completed.returncode
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
