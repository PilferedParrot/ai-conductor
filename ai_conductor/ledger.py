from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any

from .model import ProviderBudget


def append_run(
    path_value: str,
    *,
    provider: str,
    prompt: str,
    cwd: Path,
    session_id: str | None,
    budgets: dict[str, ProviderBudget],
    exit_code: int,
    run_id: str | None = None,
    chat_id: str | None = None,
    message_id: str | None = None,
) -> None:
    path = Path(path_value).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    record: dict[str, Any] = {
        "timestamp": int(time.time()),
        "provider": provider,
        "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
        "cwd": str(cwd),
        "provider_session_id": session_id,
        "budgets": {name: budget.as_dict() for name, budget in budgets.items()},
        "exit_code": exit_code,
    }
    if run_id is not None:
        record["conductor_run_id"] = run_id
    if chat_id is not None:
        record["chat_id"] = chat_id
    if message_id is not None:
        record["message_id"] = message_id
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, separators=(",", ":")) + "\n")
