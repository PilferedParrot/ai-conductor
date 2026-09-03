from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from pathlib import Path
from typing import Any

from .model import ProviderBudget


_LEDGER_LOCK = threading.Lock()


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
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
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
        record["pilferedparrot_run_id"] = run_id
    if chat_id is not None:
        record["chat_id"] = chat_id
    if message_id is not None:
        record["message_id"] = message_id
    payload = (json.dumps(record, separators=(",", ":")) + "\n").encode("utf-8")
    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0)
    with _LEDGER_LOCK:
        descriptor = os.open(path, flags, 0o600)
        try:
            os.fchmod(descriptor, 0o600)
            try:
                import fcntl
                fcntl.flock(descriptor, fcntl.LOCK_EX)
            except ImportError:
                pass
            view = memoryview(payload)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    raise OSError("could not append the run record")
                view = view[written:]
        finally:
            os.close(descriptor)
