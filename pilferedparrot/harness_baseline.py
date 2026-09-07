"""Read-only, bounded baseline of explicitly selected local stores.

Output is aggregate metadata only: no prompts, paths, session IDs, or credentials.
This does not import provider histories into the product or launch any model.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import json
from pathlib import Path
import re
from typing import Any


def _stamp(value: Any) -> float:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            pass
    return 0


def _label(value: Any) -> str:
    return value if isinstance(value, str) and re.fullmatch(r"[a-zA-Z0-9_.-]{1,100}", value) else "unknown_or_custom"


def chat_baseline(path: Path, before: float, limit: int) -> dict[str, Any]:
    data = json.loads(path.read_text())
    records = [(chat, message) for chat in data.get("chats", []) for message in chat.get("messages", [])
               if message.get("role") == "assistant" and not message.get("pending")
               and isinstance(message.get("exit_code"), int)
               and 0 < _stamp(message.get("created_at")) < before]
    records.sort(key=lambda pair: _stamp(pair[1]["created_at"]), reverse=True)
    records = records[:limit]
    models = Counter(_label(message.get("model")) for _, message in records)
    efforts = Counter(_label(message.get("reasoning_effort")) for _, message in records)
    return {
        "selection": "latest explicit completed assistant exits by creation timestamp before cutoff",
        "completion_time_limit": "legacy replies establish completion at inspection, not completion before cutoff",
        "replies": len(records), "sessions": len({chat["id"] for chat, _ in records}),
        "requested_models": dict(models), "requested_efforts": dict(efforts),
        "zero_exits": sum(message["exit_code"] == 0 for _, message in records),
        "reported_model_records": sum(bool((message.get("response_identity") or {}).get("reported_models")) for _, message in records),
        "harness_outcome_records": sum(bool(message.get("harness_outcome")) for _, message in records),
        "per_message_usage_records": sum(bool(message.get("usage_observations")) for _, message in records),
        "acceptance": "unknown for legacy replies; exit zero is not artifact acceptance",
    }


def codex_baseline(root: Path, before: float, limit: int, scan_limit: int) -> dict[str, Any]:
    candidates = sorted(root.rglob("*.jsonl"), key=lambda path: path.stat().st_mtime, reverse=True)[:scan_limit]
    rows = []
    skipped = 0
    for path in candidates:
        if path.stat().st_size > 16_000_000:
            skipped += 1
            continue
        row: dict[str, Any] = {"models": set(), "efforts": set(), "spawns": [], "last_event": None,
                               "has_total_usage": False, "completed": 0, "parent": False}
        with path.open() as stream:
            for line in stream:
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                stamp = _stamp(event.get("timestamp"))
                if stamp >= before:
                    continue
                payload = event.get("payload") or {}
                if not isinstance(payload, dict):
                    continue
                kind = event.get("type")
                if kind == "session_meta":
                    row["parent"] = bool(payload.get("parent_thread_id"))
                elif kind == "turn_context":
                    row["models"].add(_label(payload.get("model")))
                    row["efforts"].add(_label(payload.get("effort")))
                elif kind == "event_msg":
                    if payload.get("type") in {"task_started", "task_complete", "task_completed"}:
                        row["last_event"] = payload["type"]
                        if payload["type"] != "task_started":
                            row["completed"] = stamp
                    if payload.get("type") == "token_count":
                        info = payload.get("info") or {}
                        row["has_total_usage"] |= isinstance(info.get("total_token_usage"), dict)
                elif kind == "response_item" and payload.get("type") == "function_call" \
                        and str(payload.get("name", "")).endswith("spawn_agent"):
                    try:
                        args = json.loads(payload.get("arguments", "{}"))
                    except (ValueError, TypeError):
                        continue
                    row["spawns"].append({"model": _label(args.get("model", "omitted")),
                                          "effort": _label(args.get("reasoning_effort", "omitted")),
                                          "context": _label(args.get("fork_turns", "omitted"))})
        if row["last_event"] in {"task_complete", "task_completed"} and row["completed"]:
            rows.append(row)
    rows.sort(key=lambda row: row["completed"], reverse=True)
    rows = rows[:limit]
    return {
        "selection": "latest content-completed sessions as of cutoff within mtime-shortlisted candidates",
        "candidate_limit": scan_limit, "candidates_read": len(candidates) - skipped,
        "oversize_skipped": skipped, "sessions": len(rows),
        "root_sessions": sum(not row["parent"] for row in rows),
        "child_sessions": sum(row["parent"] for row in rows),
        "runtime_models_by_session": dict(Counter(model for row in rows for model in row["models"])),
        "runtime_efforts_by_session": dict(Counter(effort for row in rows for effort in row["efforts"])),
        "spawn_requests": len([spawn for row in rows for spawn in row["spawns"]]),
        "requested_worker_models": dict(Counter(spawn["model"] for row in rows for spawn in row["spawns"])),
        "requested_worker_efforts": dict(Counter(spawn["effort"] for row in rows for spawn in row["spawns"])),
        "requested_context_modes": dict(Counter(spawn["context"] for row in rows for spawn in row["spawns"])),
        "sessions_with_cumulative_usage": sum(row["has_total_usage"] for row in rows),
        "usage_total": None,
        "usage_limit": "No sum of session snapshots, inherited context, or potentially overlapping parent/child usage",
        "acceptance": "unknown; task completion does not independently verify the artifact",
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--chat-store", type=Path)
    parser.add_argument("--codex-sessions", type=Path)
    parser.add_argument("--before", required=True, help="ISO timestamp with timezone; exclusive cutoff")
    parser.add_argument("--limit", type=int, default=12)
    parser.add_argument("--session-limit", type=int, default=6)
    parser.add_argument("--scan-limit", type=int, default=50)
    args = parser.parse_args(argv)
    before = _stamp(args.before)
    if not before or not any((args.chat_store, args.codex_sessions)):
        parser.error("provide an input store and valid cutoff")
    if not (1 <= args.limit <= 100 and 1 <= args.session_limit <= 20 and 1 <= args.scan_limit <= 100):
        parser.error("sample limits out of range")
    result = {"cutoff_utc": datetime.fromtimestamp(before, timezone.utc).isoformat(),
              "savings": None, "api_equivalent_cost": None, "subscription_consumption": None,
              "limits": "Convenience sample; stores can overlap; models are requested or provider-reported, not weight attestation"}
    if args.chat_store:
        result["pilferedparrot"] = chat_baseline(args.chat_store, before, args.limit)
    if args.codex_sessions:
        result["codex"] = codex_baseline(args.codex_sessions, before, args.session_limit, args.scan_limit)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
