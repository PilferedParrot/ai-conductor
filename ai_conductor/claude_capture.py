from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from .budgets import write_claude_cache
from .config import expanded_path, load_config


LEGACY_STATUSLINE = Path("/opt/.claude/harness/statusline.py")


def main() -> int:
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        payload = None
    config = load_config()
    if isinstance(payload, dict):
        write_claude_cache(payload, expanded_path(config["claude"]["budget_cache"]))
    if LEGACY_STATUSLINE.exists():
        completed = subprocess.run(
            [sys.executable, str(LEGACY_STATUSLINE)], input=raw, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        )
        if completed.stdout:
            print(completed.stdout.rstrip())
        return completed.returncode
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
