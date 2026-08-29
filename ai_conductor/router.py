from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from typing import Any

from .model import PROVIDERS, ProviderBudget, RouteDecision


ROUTE_SCHEMA = {
    "type": "object",
    "properties": {
        "backend": {"type": "string", "enum": list(PROVIDERS)},
        "alternates": {
            "type": "array",
            "items": {"type": "string", "enum": list(PROVIDERS)},
            "minItems": 2,
            "maxItems": 2,
            "uniqueItems": True,
        },
        "reason": {"type": "string", "maxLength": 240},
        "mode": {"type": "string", "enum": ["ask", "work"]},
    },
    "required": ["backend", "alternates", "reason", "mode"],
    "additionalProperties": False,
}


SYSTEM_PROMPT = """You are the routing controller for one user's AI coding interface.
Choose exactly one backend and rank the other two. Consider the request itself, provider
strengths, current quota percentages, reset times, and reserves.

Qwen: local/private, bulk reading, repetitive transformations, self-contained questions.
Claude: project continuity, writing/design judgment, nuanced product reasoning.
Codex: repository-wide implementation, debugging, testing, and autonomous engineering.

Do not answer or critique the user's request. Return only the requested JSON decision.
Do not select a provider marked unavailable. Preserve scarce hosted quota when a local model
is sufficient, but use the strongest appropriate hosted model for consequential work.
"""


def parse_decision(content: str) -> RouteDecision:
    cleaned = content.strip()
    fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", cleaned, flags=re.DOTALL | re.IGNORECASE)
    if fenced:
        cleaned = fenced.group(1)
    data = json.loads(cleaned)
    if not isinstance(data, dict):
        raise ValueError("Qwen route must be a JSON object")
    backend = data.get("backend")
    alternates = data.get("alternates")
    reason = data.get("reason")
    mode = data.get("mode")
    if backend not in PROVIDERS:
        raise ValueError(f"invalid backend: {backend!r}")
    if not isinstance(alternates, list) or len(alternates) != 2:
        raise ValueError("alternates must contain two providers")
    if set(alternates) != set(PROVIDERS) - {backend}:
        raise ValueError("alternates must rank each remaining provider exactly once")
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("route reason is required")
    if mode not in ("ask", "work"):
        raise ValueError("mode must be ask or work")
    return RouteDecision(backend, tuple(alternates), reason.strip(), mode)


def _post_json(url: str, payload: dict[str, Any], timeout: float = 120) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.load(response)


def ask_qwen(prompt: str, budgets: dict[str, ProviderBudget], config: dict[str, Any]) -> RouteDecision:
    budget_payload = {name: budget.as_dict() for name, budget in budgets.items()}
    user_payload = json.dumps({"budgets": budget_payload, "request": prompt}, ensure_ascii=False)
    qwen = config["qwen"]
    payload = {
        "model": qwen["model"],
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_payload},
        ],
        "temperature": 0.1,
        "max_tokens": int(qwen.get("route_max_tokens", 256)),
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": "route_decision", "strict": True, "schema": ROUTE_SCHEMA},
        },
    }
    response = _post_json(qwen["base_url"].rstrip("/") + "/chat/completions", payload)
    content = response["choices"][0]["message"]["content"]
    return parse_decision(content)


def eligible(provider: str, budget: ProviderBudget, config: dict[str, Any]) -> bool:
    if not budget.available:
        return False
    if provider == "qwen":
        return True
    if budget.window is None:
        return bool(config["routing"].get("allow_unknown_budget", True))
    reserve = float(config["routing"].get("reserve_percent", {}).get(provider, 0))
    return budget.window.remaining_percent > reserve


def enforce_constraints(
    decision: RouteDecision,
    budgets: dict[str, ProviderBudget],
    config: dict[str, Any],
) -> tuple[str, str | None]:
    ranked = (decision.backend,) + decision.alternates
    for provider in ranked:
        if eligible(provider, budgets[provider], config):
            note = None if provider == decision.backend else f"{decision.backend} crossed a host constraint"
            return provider, note
    raise RuntimeError("no provider is available above its configured reserve")
