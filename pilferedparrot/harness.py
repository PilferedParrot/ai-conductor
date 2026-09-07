"""Pure validation, routing, handoff, and accounting for harness packages."""
from __future__ import annotations

import json
import math
import re
from copy import deepcopy
from typing import Any, Mapping

_EFFORTS = {"low", "medium", "high"}
_PROVIDERS = {"codex", "claude"}
_MAX_TEXT, _MAX_ITEMS, _MAX_CONTRACT = 16_384, 256, 24_000
_TOKEN_FIELDS = ("input_tokens", "output_tokens", "cached_input_tokens")

PRESETS: dict[str, dict[str, Any]] = {
    "manual": {"name": "manual", "label": "Manual", "mode": "manual", "provider": None,
               "lead": None, "worker": None, "escalation": [], "custom_routing_required": True},
    "sol-luna": {
        "name": "sol-luna", "label": "Sol / Luna", "mode": "delegate", "provider": "codex",
        "lead": {"provider": "codex", "model": "gpt-5.6-sol", "reasoning_effort": "high"},
        "worker": {"provider": "codex", "model": "gpt-5.6-luna", "reasoning_effort": "medium"},
        "escalation": [
            {"provider": "codex", "model": "gpt-5.6-terra", "reasoning_effort": "medium"},
            {"provider": "codex", "model": "gpt-5.6-sol", "reasoning_effort": "high"},
        ], "custom_routing_required": False,
    },
}


def _overlay(base: Mapping[str, Any], override: Mapping[str, Any]) -> dict[str, Any]:
    result = deepcopy(base)
    for key, value in override.items():
        result[key] = _overlay(result[key], value) if isinstance(value, Mapping) and isinstance(result.get(key), Mapping) else deepcopy(value)
    return result


def _setting(value: Any, name: str) -> dict[str, str]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} requires explicit provider, model, and reasoning_effort settings")
    provider, model, effort = value.get("provider"), value.get("model"), value.get("reasoning_effort")
    if provider not in _PROVIDERS:
        raise ValueError(f"{name} provider must be supported and explicit")
    if not isinstance(model, str) or not model.strip() or len(model) > 200 or any(ord(c) < 32 for c in model):
        raise ValueError(f"{name} model must be an explicit nonempty string")
    if effort not in _EFFORTS:
        raise ValueError(f"{name} reasoning_effort must be low, medium, or high")
    return {"provider": provider, "model": model.strip(), "reasoning_effort": effort}


def resolve_policy(config: Mapping[str, Any], preset: str | None = None) -> dict[str, Any]:
    """Resolve a named built-in or user preset without imposing a model catalog."""
    if not isinstance(config, Mapping):
        raise ValueError("configuration must be an object")
    harness = config.get("harness", {}) or {}
    if not isinstance(harness, Mapping):
        raise ValueError("harness configuration must be an object")
    custom = harness.get("presets", {}) or {}
    if not isinstance(custom, Mapping):
        raise ValueError("harness.presets must be an object")
    selected = preset if preset is not None else harness.get("preset", "manual")
    if not isinstance(selected, str) or not selected:
        raise ValueError("harness preset must be a nonempty name")
    if selected not in PRESETS and selected not in custom:
        raise ValueError(f"unknown harness preset: {selected!r}")
    override = custom.get(selected, {})
    if not isinstance(override, Mapping):
        raise ValueError("custom harness preset must be an object")
    policy = _overlay(PRESETS.get(selected, {"name": selected}), override)
    policy["name"], policy["label"] = selected, policy.get("label", selected)
    lead, worker = _setting(policy.get("lead"), "lead"), _setting(policy.get("worker"), "worker")
    raw_escalation = policy.get("escalation", [])
    if not isinstance(raw_escalation, list):
        raise ValueError("escalation must be a list")
    escalation = [_setting(item, "escalation") for item in raw_escalation]
    providers = {lead["provider"], worker["provider"], *(item["provider"] for item in escalation)}
    declared = policy.get("provider")
    if declared is not None and declared not in _PROVIDERS:
        raise ValueError("preset provider must be supported")
    if len(providers) != 1 or (declared is not None and declared not in providers):
        raise ValueError("lead, worker, and escalation settings must use one provider")
    if not isinstance(policy.get("delegation_enabled", True), bool):
        raise ValueError("delegation_enabled must be a boolean")
    policy.update(provider=lead["provider"], lead=lead, worker=worker, escalation=escalation)
    return policy


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str) or len(value) > _MAX_TEXT or not value.strip():
        raise ValueError(f"{name} must be a nonempty bounded string")
    if any((ord(char) < 32 and char not in "\n\t\r") or ord(char) == 127 for char in value):
        raise ValueError(f"{name} cannot contain control characters")
    return value.strip()


def _relative(value: Any, name: str) -> str:
    item = _text(value, name)
    if any(ord(char) < 32 for char in item):
        raise ValueError(f"{name} cannot contain control characters")
    if item.startswith(("/", "\\", "~")) or "\\" in item or re.match(r"^[A-Za-z]:", item):
        raise ValueError(f"{name} must be a repository-relative path")
    parts = item.split("/")
    if any(part in {"", ".", ".."} for part in parts) or any(char in item for char in "*?[]{}"):
        raise ValueError(f"{name} must be a repository-relative filename, not a glob")
    return item


def validate_contract(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("contract must be an object")
    allowed = {"task", "category", "inputs", "write_scope", "acceptance_check", "artifact", "stop_conditions", "hypothesis"}
    if unknown := set(value) - allowed:
        raise ValueError(f"unknown contract fields: {sorted(unknown)!r}")
    result = {"task": _text(value.get("task"), "task"), "category": _text(value.get("category"), "category"),
              "inputs": [], "write_scope": [], "acceptance_check": _text(value.get("acceptance_check"), "acceptance_check"),
              "artifact": _relative(value.get("artifact"), "artifact"), "stop_conditions": _text(value.get("stop_conditions"), "stop_conditions")}
    for field in ("inputs", "write_scope"):
        items = value.get(field, [])
        if not isinstance(items, list) or len(items) > _MAX_ITEMS:
            raise ValueError(f"{field} must be a bounded list")
        result[field] = [_relative(item, f"{field} item") for item in items]
    if "hypothesis" in value:
        result["hypothesis"] = _text(value["hypothesis"], "hypothesis")
    if len(json.dumps(result, separators=(",", ":"), ensure_ascii=True)) > _MAX_CONTRACT:
        raise ValueError("contract exceeds the 24000 character serialized limit")
    return result


def _finite(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0


def _normalized_estimates(estimates: Mapping[str, Any] | None) -> tuple[dict[str, Any], bool]:
    if estimates is None:
        estimates = {}
    if not isinstance(estimates, Mapping):
        raise ValueError("estimates must be an object")
    result = {"unit": estimates.get("unit"), "source": "estimated"}
    complete = estimates.get("unit") in {"effort_points", "api_usd", "seconds"}
    for key in ("direct", "briefing", "execution", "verification", "rework"):
        value = estimates.get(key)
        if value is not None and not _finite(value):
            raise ValueError(f"{key} estimate must be finite and nonnegative")
        result[key] = value if _finite(value) else None
        complete = complete and _finite(value)
    return result, bool(complete)


def route_task(policy: Mapping[str, Any], contract: Mapping[str, Any], estimates: Mapping[str, Any] | None) -> dict[str, Any]:
    checked = validate_contract(contract)
    if not isinstance(policy, Mapping):
        raise ValueError("policy must be an object")
    lead, worker = _setting(policy.get("lead"), "lead"), _setting(policy.get("worker"), "worker")
    if lead["provider"] != worker["provider"]:
        raise ValueError("lead and worker must use one provider")
    normalized, complete = _normalized_estimates(estimates)
    result = {"mode": "direct", "requested": lead, "reason": "", "estimates": normalized}
    if policy.get("delegation_enabled", True) is False:
        result["reason"] = "delegation disabled by this preset"
    elif not complete:
        result["reason"] = "estimates are unknown"
    elif checked["category"].lower() == "trivial":
        result["reason"] = "trivial task"
    elif normalized["direct"] <= sum(normalized[key] for key in ("briefing", "execution", "verification", "rework")):
        result["reason"] = "delegation does not improve the estimate"
    else:
        result.update(mode="delegate", requested=worker, reason="delegation estimate is favorable")
    return result


def render_handoff(contract: Mapping[str, Any], route: Mapping[str, Any]) -> str:
    checked = validate_contract(contract)
    if not isinstance(route, Mapping) or route.get("mode") not in {"direct", "delegate"}:
        raise ValueError("route must identify direct or delegate mode")
    selected = _setting(route.get("requested"), "requested")
    lines = ["HARNESS HANDOFF", f"mode: {route['mode']}", f"selected: {selected['provider']} / {selected['model']} / {selected['reasoning_effort']}",
             f"task: {checked['task']}", f"category: {checked['category']}", f"inputs: {', '.join(checked['inputs']) or '(none)'}",
             f"write_scope: {', '.join(checked['write_scope']) or '(read-only)'}", f"acceptance_check: {checked['acceptance_check']}",
             f"artifact: {checked['artifact']}", f"stop_conditions: {checked['stop_conditions']}"]
    if "hypothesis" in checked:
        lines.append(f"hypothesis: {checked['hypothesis']}")
    lines += ["Read only the necessary referenced inputs and project instructions; do not load unrelated history.",
              "Update existing STATE/LEDGER files only if explicitly included in write scope; keep history append-only.",
              "Run the supplied acceptance check and return concise artifact references, observed results, and limitations.",
              "The lead records acceptance after proportional artifact review; do not repeat identical reviews.",
              "Write scope is an assignment boundary; the provider controls workspace permissions.", "Do not recursively delegate or use nested agents.",
              "Do not commit, push, publish, or deploy.", "Stop on the stated criteria; escalate a brief defect."]
    return "\n".join(lines)


def metric(value: Any = None, source: str = "unknown", unit: str | None = None) -> dict[str, Any]:
    return {"value": value, "source": source, "unit": unit}


def _provenance(sources: set[str], value: Any) -> str:
    return "unknown" if value is None or not sources else "estimated" if "estimated" in sources else "measured"


def normalize_usage(observations: list[Mapping[str, Any]]) -> dict[str, Any]:
    if not isinstance(observations, list):
        raise ValueError("observations must be a list")
    seen: set[str] = set(); by_scope: dict[str, list[Mapping[str, Any]]] = {}; invalid = False
    for item in observations:
        if not isinstance(item, Mapping) or not isinstance(item.get("id"), str) or not item["id"]:
            invalid = True; continue
        if item["id"] in seen:
            continue
        seen.add(item["id"])
        if item.get("basis") not in {"delta", "cumulative"} or item.get("source") not in {"measured", "estimated"}:
            invalid = True; continue
        scope = item.get("scope_id")
        if not isinstance(scope, str) or not scope:
            invalid = True; continue
        by_scope.setdefault(scope, []).append(item)
    ambiguity = any(len({entry["basis"] for entry in entries}) != 1 for entries in by_scope.values())
    if len(by_scope) > 1 and any(entry.get("includes_children") is not False for entries in by_scope.values() for entry in entries):
        ambiguity = True
    totals: dict[str, Any] = {}; sources: dict[str, set[str]] = {key: set() for key in _TOKEN_FIELDS}
    for field in _TOKEN_FIELDS:
        total, unknown = 0, ambiguity or invalid
        for entries in by_scope.values():
            if len({entry["basis"] for entry in entries}) != 1 or any(not _finite(entry.get(field)) for entry in entries):
                unknown = True; continue
            values = [entry[field] for entry in entries]
            total += max(values) if entries[0]["basis"] == "cumulative" else sum(values)
            sources[field].update(entry["source"] for entry in entries)
        totals[field] = None if unknown or not by_scope else total
    complete = bool(by_scope) and not invalid and not ambiguity and totals["input_tokens"] is not None and totals["output_tokens"] is not None
    return {**{field: metric(totals[field], _provenance(sources[field], totals[field]), "tokens") for field in _TOKEN_FIELDS},
            "observations": len(seen), "complete": complete, "ambiguity": "overlapping or indeterminate usage scopes" if ambiguity else None,
            "api_equivalent_cost": metric(None, "unknown", "USD"), "subscription_consumption": metric()}


def _sum_attempt_metrics(attempts: list[Mapping[str, Any]], field: str, *, review: bool = False) -> dict[str, Any]:
    values: list[float] = []; sources: set[str] = set()
    for attempt in attempts:
        raw = attempt.get("review", {}).get(field) if review and isinstance(attempt.get("review"), Mapping) else attempt.get(field)
        if not isinstance(raw, Mapping) or not _finite(raw.get("value")) or raw.get("source") not in {"measured", "estimated"}:
            return metric(None, "unknown", "seconds")
        values.append(raw["value"]); sources.add(raw["source"])
    return metric(sum(values), _provenance(sources, sum(values)), "seconds") if attempts else metric(None, "unknown", "seconds")


def outcome_summary(attempts: list[Mapping[str, Any]]) -> dict[str, Any]:
    if not isinstance(attempts, list):
        raise ValueError("attempts must be a list")
    unique: list[Mapping[str, Any]] = []; ids: set[str] = set()
    for attempt in attempts:
        if isinstance(attempt, Mapping) and isinstance(attempt.get("id"), str) and attempt["id"] and attempt["id"] not in ids:
            ids.add(attempt["id"]); unique.append(attempt)
    counts = {key: 0 for key in ("total", "accepted", "rejected", "awaiting_review", "failed", "running")}
    observations: list[Mapping[str, Any]] = []
    for attempt in unique:
        counts["total"] += 1; review = attempt.get("review")
        if isinstance(review, Mapping) and review.get("accepted") is True: state = "accepted"
        elif isinstance(review, Mapping) and review.get("accepted") is False: state = "rejected"
        else:
            status = str(attempt.get("status", "")).lower()
            state = status if status in {"awaiting_review", "failed", "running"} else "awaiting_review"
        counts[state] += 1
        raw = attempt.get("usage_observations", [])
        observations.extend(raw if isinstance(raw, list) and raw else [{}])
    return {"counts": counts, "usage": normalize_usage(observations),
            "elapsed": _sum_attempt_metrics(unique, "elapsed_seconds"),
            "review": _sum_attempt_metrics(unique, "review_seconds", review=True),
            "rework": _sum_attempt_metrics(unique, "rework_seconds", review=True),
            "rework_attempts": sum(1 for item in unique if isinstance(item.get("retry_index"), int) and not isinstance(item.get("retry_index"), bool) and item["retry_index"] > 0),
            "api_equivalent_cost": metric(None, "unknown", "USD"), "subscription_consumption": metric()}
