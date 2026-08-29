from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent.parent

DEFAULTS: dict[str, Any] = {
    "qwen": {
        "base_url": "http://127.0.0.1:8080/v1",
        "health_url": "http://127.0.0.1:8080/health",
        "model": "qwen3-coder-next",
        "auto_start": True,
        "start_command": ["/opt/llm/serve.sh", "start", "code"],
        "start_timeout_seconds": 120,
        "route_max_tokens": 256,
        "agent_max_tokens": 4096,
        "max_tool_turns": 24,
        "tool_output_chars": 24000,
        "file_limit_bytes": 1000000,
        "shell_timeout_seconds": 120,
        "shell_max_timeout_seconds": 600,
    },
    "claude": {
        "command": "claude",
        "model": "opus",
        "effort": "high",
        "permission_mode": "auto",
        "budget_cache": "~/.cache/ai-conductor/claude-budget.json",
        "budget_stale_seconds": 600,
    },
    "codex": {
        "command": "codex",
        "model": None,
        "sandbox": "workspace-write",
        "budget_timeout_seconds": 8,
    },
    "routing": {
        "reserve_percent": {"claude": 20, "codex": 20},
        "allow_unknown_budget": True,
    },
    "policy_file": str(ROOT / "POLICY.md"),
    "ledger": "~/.local/state/ai-conductor/runs.jsonl",
}


def _merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _merge(base[key], value)
        else:
            base[key] = value
    return base


def load_config(path: str | Path | None = None) -> dict[str, Any]:
    result = deepcopy(DEFAULTS)
    config_path = Path(path).expanduser() if path else ROOT / "config.json"
    if config_path.exists():
        with config_path.open(encoding="utf-8") as handle:
            overlay = json.load(handle)
        if not isinstance(overlay, dict):
            raise ValueError(f"configuration root must be an object: {config_path}")
        _merge(result, overlay)
    return result


def expanded_path(value: str) -> Path:
    return Path(value).expanduser().resolve()
