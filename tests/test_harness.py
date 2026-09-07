import math
import unittest
from copy import deepcopy

from pilferedparrot.harness import PRESETS, metric, normalize_usage, outcome_summary, render_handoff, resolve_policy, route_task, validate_contract

CONTRACT = {"task": "Implement bounded change", "category": "feature", "inputs": ["pilferedparrot/config.py"], "write_scope": ["pilferedparrot/harness.py"], "acceptance_check": "Open the artifact and run its focused test", "artifact": "tests/test_harness.py", "stop_conditions": "Stop on failed acceptance"}
ESTIMATES = {"unit": "effort_points", "direct": 10, "briefing": 1, "execution": 2, "verification": 2, "rework": 1}


class HarnessTests(unittest.TestCase):
    def test_manual_requires_explicit_settings_and_custom_models_are_allowed(self):
        self.assertEqual(PRESETS["sol-luna"]["provider"], "codex")
        with self.assertRaisesRegex(ValueError, "explicit"):
            resolve_policy({})
        config = {"harness": {"preset": "manual", "presets": {"manual": {"label": "My routing", "provider": "codex", "lead": {"provider": "codex", "model": "gpt-5.6-astra", "reasoning_effort": "high"}, "worker": {"provider": "codex", "model": "a-private-model", "reasoning_effort": "low"}}}}}
        policy = resolve_policy(config)
        self.assertEqual(policy["worker"]["model"], "a-private-model")
        self.assertEqual(policy["label"], "My routing")

    def test_explicit_direct_control_and_multiline_contract(self):
        policy = resolve_policy({"harness": {"preset": "sol-luna", "presets": {
            "sol-luna": {"delegation_enabled": False}}}})
        route = route_task(policy, dict(CONTRACT, task="First line\nSecond line"), ESTIMATES)
        self.assertEqual(route["mode"], "direct")
        self.assertEqual(route["requested"], policy["lead"])

    def test_policy_rejects_cross_provider_and_bad_escalation(self):
        config = {"harness": {"preset": "x", "presets": {"x": {"lead": {"provider": "codex", "model": "a", "reasoning_effort": "low"}, "worker": {"provider": "claude", "model": "b", "reasoning_effort": "low"}}}}}
        with self.assertRaises(ValueError): resolve_policy(config)
        with self.assertRaises(ValueError): resolve_policy({"harness": {"preset": "sol-luna", "presets": {"sol-luna": {"escalation": [{"provider": "codex", "model": "x", "reasoning_effort": "bad"}]}}}})

    def test_direct_always_selects_lead_and_invalid_numerics_raise(self):
        policy = resolve_policy({"harness": {"preset": "sol-luna"}})
        for estimate in (None, {}, dict(ESTIMATES, direct=6)):
            route = route_task(policy, CONTRACT, estimate)
            self.assertEqual(route["mode"], "direct")
            self.assertEqual(route["requested"], policy["lead"])
            self.assertEqual(route["estimates"]["source"], "estimated")
        self.assertEqual(route_task(policy, CONTRACT, ESTIMATES)["requested"], policy["worker"])
        for value in (-1, math.inf, math.nan, True, "3"):
            with self.assertRaises(ValueError): route_task(policy, CONTRACT, dict(ESTIMATES, direct=value))

    def test_contract_paths_acceptance_and_bounded_rendering(self):
        for field, value in (("inputs", ["../secret"]), ("write_scope", ["x/*.py"]), ("artifact", "C:/x"), ("artifact", "x\x00y")):
            with self.assertRaises(ValueError): validate_contract(dict(CONTRACT, **{field: value}))
        checked = validate_contract(dict(CONTRACT, hypothesis="A focused change should work"))
        text = render_handoff(checked, {"mode": "direct", "requested": resolve_policy({"harness": {"preset": "sol-luna"}})["lead"]})
        self.assertIn("gpt-5.6-sol / high", text)
        self.assertIn("artifact review", text)
        self.assertIn("hypothesis:", text)

    def test_usage_cumulative_missing_and_resumed_mixed_bases_are_conservative(self):
        usage = normalize_usage([{"id": "one", "scope_id": "run", "basis": "cumulative", "input_tokens": 10, "output_tokens": 2, "source": "measured"}, {"id": "two", "scope_id": "run", "basis": "cumulative", "input_tokens": 15, "output_tokens": 3, "source": "estimated"}])
        self.assertEqual(usage["input_tokens"]["value"], 15)
        self.assertEqual(usage["input_tokens"]["source"], "estimated")
        self.assertTrue(usage["complete"])
        missing = normalize_usage([{"id": "missing", "scope_id": "x", "basis": "delta", "input_tokens": 2, "source": "measured"}])
        self.assertIsNone(missing["output_tokens"]["value"])
        self.assertFalse(missing["complete"])
        resumed = normalize_usage([{"id": "a", "scope_id": "x", "basis": "delta", "input_tokens": 2, "output_tokens": 1, "source": "measured"}, {"id": "b", "scope_id": "x", "basis": "cumulative", "input_tokens": 3, "output_tokens": 2, "source": "measured"}])
        self.assertIsNone(resumed["input_tokens"]["value"])
        self.assertFalse(resumed["complete"])

    def test_summary_uses_integrated_attempts_and_no_derived_usage(self):
        attempts = [{"id": "a", "status": "awaiting_review", "elapsed_seconds": metric(2, "measured", "seconds"), "usage_observations": [{"id": "u", "scope_id": "u", "basis": "delta", "input_tokens": 4, "output_tokens": 1, "source": "measured"}], "usage": {"input_tokens": 999}, "review": None, "retry_index": 0}, {"id": "b", "status": "failed", "elapsed_seconds": metric(3, "estimated", "seconds"), "usage_observations": [], "review": {"accepted": False, "review_seconds": metric(1, "measured", "seconds"), "rework_seconds": metric(2, "estimated", "seconds")}, "retry_index": 1}, {"id": "b", "status": "failed", "elapsed_seconds": metric(99, "measured", "seconds")}]
        summary = outcome_summary(attempts)
        self.assertEqual(summary["counts"], {"total": 2, "accepted": 0, "rejected": 1, "awaiting_review": 1, "failed": 0, "running": 0})
        self.assertIsNone(summary["usage"]["input_tokens"]["value"])
        self.assertFalse(summary["usage"]["complete"])
        self.assertEqual(summary["elapsed"]["source"], "estimated")
        self.assertIsNone(summary["review"]["value"])
        self.assertEqual(summary["rework_attempts"], 1)

    def test_usage_deduplicates_observations_without_counting_inherited_context(self):
        first = {"id": "event-1", "scope_id": "worker-1", "basis": "delta",
                 "input_tokens": 10, "output_tokens": 2, "source": "measured",
                 "includes_children": False, "inherited_context_tokens": 1000}
        second = {**first, "id": "event-2", "scope_id": "worker-2",
                  "input_tokens": 20, "output_tokens": 3}
        usage = normalize_usage([first, deepcopy(first), second])
        self.assertTrue(usage["complete"])
        self.assertEqual(usage["observations"], 2)
        self.assertEqual(usage["input_tokens"]["value"], 30)
        self.assertEqual(usage["output_tokens"]["value"], 5)
        self.assertIsNone(usage["api_equivalent_cost"]["value"])
        self.assertIsNone(usage["subscription_consumption"]["value"])
        for overlap in (True, None):
            ambiguous = normalize_usage([{**first, "includes_children": overlap}, second])
            self.assertFalse(ambiguous["complete"])
            self.assertIsNone(ambiguous["input_tokens"]["value"])


if __name__ == "__main__":
    unittest.main()
