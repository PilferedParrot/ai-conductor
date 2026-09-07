"""Bounded packages in the existing work-session store and dispatch lifecycle.

No scheduler, autonomous reviewer, provider transcript crawler, or second database.
"""
from __future__ import annotations

import hashlib
import math
import time
import uuid
from copy import deepcopy
from pathlib import Path
from typing import Any

from .config import effective_model
from .harness import PRESETS, metric, normalize_usage, outcome_summary, resolve_policy, route_task, validate_contract


def _text(value: Any, name: str, limit: int = 8000) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError(f"{name} must be nonempty text of at most {limit} characters")
    return value.strip()


def _effort(value: Any, source: str) -> dict[str, Any]:
    if value is None:
        return metric(None, unit="seconds")
    if isinstance(value, bool) or not isinstance(value, (int, float)) \
            or not math.isfinite(value) or value < 0:
        raise ValueError("effort must be finite nonnegative seconds or null")
    if source not in {"estimated", "measured"}:
        raise ValueError("effort source must be measured or estimated")
    return metric(value, source, "seconds")


def _check_paths(contract: dict[str, Any], cwd: Path) -> None:
    # Contract paths constrain the assignment, not the provider's OS sandbox.
    # Reject symlink escapes as well as lexical escapes before dispatch.
    for reference in contract["inputs"] + contract["write_scope"] + [contract["artifact"]]:
        if not (cwd / reference).resolve().is_relative_to(cwd.resolve()):
            raise ValueError("harness file references must stay inside the project")


class HarnessWorkflow:
    """Mixin using PilferedParrotApp's locks, session ownership and provider run."""

    def harness_metadata(self) -> dict[str, Any]:
        configured = self.config.get("harness", {})
        presets = {**PRESETS, **configured.get("presets", {})}
        return {
            "default_preset": configured.get("preset", "manual"),
            "presets": [{"id": name, "label": value.get("label", name),
                         "provider": value.get("provider")}
                        for name, value in presets.items() if isinstance(value, dict)],
        }

    @staticmethod
    def _harness_task(chat: dict[str, Any], task_id: Any) -> dict[str, Any]:
        task = next((task for task in chat.get("harness_tasks", [])
                     if task.get("id") == task_id), None)
        if task is None:
            raise ValueError("unknown harness task")
        return task

    def harness_action(
        self, chat_id: str, payload: dict[str, Any], *,
        window_id: str | None = None, window_provider: str | None = None,
    ) -> dict[str, Any]:
        # Same lock order as send/cancel. Hold through registration so two Run
        # clicks cannot launch the same attempt or overlap writes in this project.
        with self.runs_lock, self.store.lock:
            chat = self._owned_chat(chat_id, window_id)
            provider = chat.get("requested_provider") or chat.get("provider")
            if window_provider and provider != window_provider:
                raise ValueError("harness task belongs to a different provider")
            if chat.get("harness_parent"):
                raise ValueError("manage this package from its parent session")
            action = payload.get("action")
            if action == "plan":
                if chat_id in self.runs or any(task.get("status") == "running" for task in chat.get("harness_tasks", [])):
                    raise ValueError("wait for the active package before changing its lead selection")
                policy = resolve_policy(self.config, payload.get("preset"))
                contract = validate_contract(payload.get("contract"))
                _check_paths(contract, Path(chat["cwd"]))
                route = route_task(policy, contract, payload.get("estimates") or {})
                route["prior_selection"] = {
                    "model": chat.get("requested_model") or effective_model(self.config, provider),
                    "reasoning_effort": chat.get("reasoning_effort") or self.config.get(provider, {}).get("reasoning_effort"),
                    "source": "session_selection_or_provider_config", "runtime_confirmed": False,
                }
                if route["requested"]["provider"] != provider:
                    raise ValueError("open the preset's provider window before planning")
                if provider not in {"codex", "claude"}:
                    raise ValueError("bounded harness execution supports Codex and Claude")
                task = {
                    "id": uuid.uuid4().hex, "contract": contract, "route": route,
                    "policy": policy, "status": "planned", "attempts": [],
                    "created_at": time.time(), "events": [],
                }
                chat.setdefault("harness_tasks", []).append(task)
                chat["requested_model"] = policy["lead"]["model"]
                chat["reasoning_effort"] = policy["lead"]["reasoning_effort"]
                self.store.data["preferences"]["work_models"][provider] = policy["lead"]["model"]
                self.store.mark_used(chat)
            else:
                task = self._harness_task(chat, payload.get("task_id"))
                if action == "run":
                    return self._harness_run(chat, task, window_id, window_provider)
                if action == "review":
                    self._harness_review(task, payload, Path(chat["cwd"]))
                elif action == "retry":
                    self._harness_retry(task, payload, Path(chat["cwd"]))
                else:
                    raise ValueError("unknown harness action")
            chat["updated_at"] = int(time.time())
            self.store.save()
            return self.store.public(chat)

    def _harness_run(self, chat, task, window_id, window_provider):
        if task["status"] != "planned":
            raise ValueError("only a planned package can run; review before retrying")
        if len(task["attempts"]) >= 3:
            raise ValueError("package stopped after three attempts; choose a new approach")
        for running_id in self.runs:
            if self.store.get(running_id)["cwd"] == chat["cwd"]:
                raise ValueError("wait for the active project run before launching a package")
        _check_paths(task["contract"], Path(chat["cwd"]))
        route = task["route"]
        requested = route["requested"]
        execution = chat
        if route["mode"] == "delegate":
            # Existing work-session creation gives the worker a fresh provider
            # session and no inherited transcript. No native agent tree is cloned.
            public = self.store.create(
                Path(chat["cwd"]), requested["provider"], requested["model"],
                window_id=chat.get("window_id", "main"),
                reasoning_effort=requested["reasoning_effort"],
            )
            execution = self.store.get(public["id"])
            execution["harness_parent"] = {"chat_id": chat["id"], "task_id": task["id"]}
        started = time.monotonic()
        attempt = {
            "id": uuid.uuid4().hex, "chat_id": execution["id"],
            "parent_chat_id": chat["id"], "task_id": task["id"],
            "category": task["contract"]["category"],
            "contract": deepcopy(task["contract"]), "requested": deepcopy(requested),
            "route_mode": route["mode"], "retry_index": len(task["attempts"]),
            "started_at": time.time(), "status": "running",
            "confirmed": {"model": None, "reasoning_effort": None, "source": "unknown"},
            "elapsed_seconds": metric(None, unit="seconds"),
            "usage_observations": [], "usage": normalize_usage([]), "review": None,
            "api_equivalent_cost": metric(None, unit="USD"),
            "subscription_consumption": metric(None),
            "inherited_context": "none" if route["mode"] == "delegate" else "provider session if compatible",
        }
        task["attempts"].append(attempt)
        task["status"] = "running"
        task["summary"] = outcome_summary(task["attempts"])
        self.store.save()
        try:
            return self.send_message(
                execution["id"], {"content": task["contract"]["task"],
                    "provider": requested["provider"], "model": requested["model"],
                    "reasoning_effort": requested["reasoning_effort"]},
                window_id=window_id, window_provider=window_provider,
                _harness_attempt=(chat["id"], task["id"], attempt["id"]),
            )
        except Exception:
            task["status"] = attempt["status"] = "failed"
            attempt["elapsed_seconds"] = metric(time.monotonic() - started, "measured", "seconds")
            attempt["failure_reason"] = "launch failed before provider completion was recorded"
            task["summary"] = outcome_summary(task["attempts"])
            self.store.save()
            raise

    def _harness_review(self, task, payload, cwd):
        if task["status"] not in {"awaiting_review", "failed"} or not task["attempts"]:
            raise ValueError("review requires a completed attempt")
        attempt = task["attempts"][-1]
        if attempt.get("review") is not None:
            raise ValueError("this attempt already has a review")
        accepted = payload.get("accepted")
        if not isinstance(accepted, bool):
            raise ValueError("accepted must be true or false")
        if accepted and attempt["status"] != "awaiting_review":
            raise ValueError("a failed run cannot be accepted; record a rejection")
        artifact = _text(payload.get("artifact"), "artifact", 1000)
        # Use the same path validator for evidence artifacts without changing
        # the independently supplied acceptance check.
        checked = validate_contract({**task["contract"], "artifact": artifact})
        _check_paths(checked, cwd)
        snapshot = None
        artifact_path = cwd / artifact
        if accepted:
            if not artifact_path.is_file():
                raise ValueError("acceptance requires an existing artifact file")
            digest = hashlib.sha256()
            size = 0
            with artifact_path.open("rb") as stream:
                for block in iter(lambda: stream.read(65536), b""):
                    size += len(block)
                    if size > 16_000_000:
                        raise ValueError("use a small verification report for artifacts larger than 16 MB")
                    digest.update(block)
            snapshot = {"sha256": digest.hexdigest(), "bytes": size}
        review = {
            "accepted": accepted, "artifact_snapshot": snapshot, "artifact": artifact,
            "evidence": _text(payload.get("evidence"), "verification evidence"),
            "acceptance_check": attempt["contract"]["acceptance_check"],
            "review_seconds": _effort(payload.get("review_seconds"), payload.get("effort_source", "estimated")),
            "rework_seconds": _effort(payload.get("rework_seconds"), payload.get("effort_source", "estimated")),
            "recorded_at": time.time(), "source": "operator_recorded",
        }
        attempt["review"] = review
        task["status"] = "accepted" if accepted else "rejected"
        task["summary"] = outcome_summary(task["attempts"])
        task["events"].append({"type": "review", "attempt_id": attempt["id"], **deepcopy(review)})

    def _harness_retry(self, task, payload, cwd):
        if task["status"] != "rejected":
            raise ValueError("record a rejection with evidence before retrying")
        if len(task["attempts"]) >= 3:
            raise ValueError("package stopped after three attempts; choose a new approach")
        evidence = _text(payload.get("evidence"), "escalation evidence")
        contract = validate_contract(payload.get("contract", task["contract"]))
        _check_paths(contract, cwd)
        if contract["acceptance_check"] != task["contract"]["acceptance_check"]:
            raise ValueError("acceptance check is fixed; a changed target requires a new package")
        route = deepcopy(task["route"])
        # Change model/effort or the brief; never repeat an identical assignment.
        previous = task["attempts"][-1]
        if contract == previous["contract"]:
            ladder = [task["policy"]["worker"], *task["policy"].get("escalation", [])]
            choices = [{"provider": task["policy"]["provider"], **item} for item in ladder]
            used = [item["requested"] for item in task["attempts"]]
            position = choices.index(previous["requested"]) if previous["requested"] in choices else len(choices)
            next_route = next((item for item in choices[position + 1:] if item not in used), None)
            if next_route is None:
                raise ValueError("no unused escalation setting; change the brief or stop")
            route["requested"] = next_route
        if any(item["requested"] == route["requested"] and item["contract"] == contract
               for item in task["attempts"]):
            raise ValueError("identical assignment has already been attempted")
        route["reason"] = "Evidence-based retry: " + evidence
        task["contract"], task["route"], task["status"] = contract, route, "planned"
        task["events"].append({"type": "escalation", "evidence": evidence,
                               "requested": deepcopy(route["requested"]), "at": time.time()})

    def _harness_reference(self, reference):
        parent_id, task_id, attempt_id = reference
        parent = self.store.get(parent_id)
        task = self._harness_task(parent, task_id)
        attempt = next(item for item in task["attempts"] if item["id"] == attempt_id)
        return parent, task, attempt

    def _harness_complete(self, pending, result=None, elapsed=None):
        reference = pending.get("harness_reference")
        if not reference:
            return
        parent, task, attempt = self._harness_reference(reference)
        attempt["message_id"] = pending["id"]
        attempt["run_id"] = pending["run_id"]
        attempt["status"] = task["status"] = (
            "awaiting_review" if pending.get("exit_code") == 0
            and not pending.get("error") and not pending.get("cancelled") else "failed"
        )
        attempt["elapsed_seconds"] = metric(elapsed, "measured" if elapsed is not None else "unknown", "seconds")
        if result is not None:
            model = getattr(result, "reported_model", None)
            effort = getattr(result, "reported_reasoning_effort", None)
            attempt["confirmed"] = {"model": model, "reasoning_effort": effort,
                                    "source": "provider_reported" if model or effort else "unknown"}
            attempt["reported_usage"] = deepcopy(getattr(result, "reported_usage", None))
            scope = result.session_id or pending["run_id"]
            attempt["usage_observations"] = [{
                "id": pending["run_id"], "scope_id": scope,
                "basis": getattr(result, "usage_basis", "unknown"),
                "input_tokens": result.input_tokens, "output_tokens": result.output_tokens,
                "cached_input_tokens": getattr(result, "cached_input_tokens", None),
                "source": "measured", "includes_children": getattr(result, "usage_includes_children", None),
            }]
            attempt["usage"] = normalize_usage(attempt["usage_observations"])
        task["summary"] = outcome_summary(task["attempts"])
        pending["harness_outcome"] = deepcopy(attempt)
        parent["updated_at"] = int(time.time())
