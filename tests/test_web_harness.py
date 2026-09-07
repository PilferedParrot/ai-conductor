"""Contract-to-provider-to-review integration without paid provider execution."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from pilferedparrot.config import DEFAULTS
from pilferedparrot.dispatch import RunResult
from pilferedparrot.harness import outcome_summary
from pilferedparrot.web import PilferedParrotApp
from test_web_server import bare_handler
from pilferedparrot import web_server


CONTRACT = {
    "task": "Repair the fixture formatter", "category": "implementation",
    "inputs": ["input.txt"], "write_scope": ["output.txt"],
    "acceptance_check": "Compare output.txt with the two-line fixture; both lines match",
    "artifact": "output.txt", "stop_conditions": "Stop if another file needs editing",
}
ESTIMATES = dict(unit="effort_points", direct=20, briefing=1, execution=4, verification=2, rework=1)


class WebHarnessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = deepcopy(DEFAULTS)
        self.config["web"].update(chat_store=str(self.root / "chats.json"),
                                   model_catalog_store=str(self.root / "models.json"))
        self.config["codex"].update(config_path=str(self.root / "codex.toml"),
                                     models_cache=str(self.root / "cache.json"), model="gpt-6-astra")
        self.config["ledger"] = str(self.root / "runs.jsonl")
        self.app = PilferedParrotApp(self.config, self.root)
        self.addCleanup(self.app.shutdown)
        self.parent = self.app.create_chat({"provider": "codex"})["id"]

    def action(self, action, task_id=None, **payload):
        return self.app.harness_action(self.parent, {"action": action, "task_id": task_id, **payload})

    def plan(self, contract=None, estimates=None):
        response = self.action("plan", preset="sol-luna", contract=contract or CONTRACT,
                               estimates=ESTIMATES if estimates is None else estimates)
        return response["harness_tasks"][-1]

    def run_package(self, task_id, fake=None):
        seen = []
        def capture(provider, prompt, cwd, conversation, config, cancel):
            seen.append((provider, prompt, cwd, conversation.provider_session_id, config))
            (cwd / "output.txt").write_text("first\nsecond\n")
            return fake or RunResult("Artifact: output.txt; check passed", 0, "fixture-session",
                                     input_tokens=100, output_tokens=20, usage_basis="delta",
                                     usage_includes_children=False)
        with patch("pilferedparrot.web.capture_dispatch", side_effect=capture):
            chat = self.action("run", task_id)
            with self.app.runs_lock:
                run = self.app.runs.get(chat["id"])
            if run:
                run.thread.join(5)
                self.assertFalse(run.thread.is_alive())
        return self.app.chat_state(chat["id"]), seen

    def test_delegate_fresh_explicit_route_handoff_review_and_persistence(self):
        task = self.plan()
        self.assertEqual(task["route"]["requested"]["model"], "gpt-5.6-luna")
        chat, seen = self.run_package(task["id"])
        self.assertNotEqual(chat["id"], self.parent)
        self.assertIsNone(seen[0][3])
        self.assertEqual(seen[0][4]["codex"]["reasoning_effort"], "medium")
        self.assertTrue(seen[0][4]["_harness"]["bounded"])
        for text in (CONTRACT["task"], CONTRACT["acceptance_check"], "output.txt", "Stop if"):
            self.assertIn(text, seen[0][1])
        parent = self.app.chat_state(self.parent)
        task = parent["harness_tasks"][0]
        self.assertEqual(task["status"], "awaiting_review")
        attempt = task["attempts"][0]
        self.assertIsNone(attempt["confirmed"]["model"])
        self.assertEqual(attempt["usage"]["input_tokens"]["value"], 100)
        self.assertEqual(self.app.store.latest_work_defaults("codex"), ("gpt-5.6-sol", "high"))
        self.assertEqual(self.app.store.data["preferences"]["work_models"]["codex"], "gpt-5.6-sol")
        self.assertIsNone(attempt["subscription_consumption"]["value"])
        with self.assertRaisesRegex(ValueError, "existing artifact"):
            self.action("review", task["id"], accepted=True, artifact="missing.txt", evidence="unverified claim")
        with self.assertRaisesRegex(ValueError, "parent session"):
            self.app.send_message(chat["id"], {"content": "keep going"})
        self.action("review", task["id"], accepted=True, artifact="output.txt",
                    evidence="Both expected lines match", review_seconds=8, effort_source="estimated")
        with self.assertRaises(ValueError):
            self.action("review", task["id"], accepted=True, artifact="output.txt", evidence="repeat")
        reloaded = PilferedParrotApp(self.config, self.root)
        self.addCleanup(reloaded.shutdown)
        stored = reloaded.chat_state(self.parent)["harness_tasks"][0]
        self.assertEqual(stored["status"], "accepted")
        self.assertEqual(stored["attempts"][0]["review"]["artifact_snapshot"]["bytes"], 13)
        self.assertEqual(stored["attempts"][0]["review"]["review_seconds"]["source"], "estimated")
        ledger = json.loads((self.root / "runs.jsonl").read_text().splitlines()[0])
        self.assertEqual(ledger["harness_reference"][:2], [self.parent, task["id"]])
        self.assertNotIn("usage", ledger)  # References, not a second billable observation.
        summary = outcome_summary(stored["attempts"])
        self.assertEqual(summary["usage"]["input_tokens"]["value"], 100)

    def test_trivial_and_unknown_estimates_run_locally_on_lead(self):
        for contract, estimates in [(dict(CONTRACT, category="trivial"), ESTIMATES), (CONTRACT, {})]:
            task = self.plan(contract, estimates)
            self.assertEqual(task["route"]["mode"], "direct")
            chat, seen = self.run_package(task["id"])
            self.assertEqual(chat["id"], self.parent)
            self.assertEqual(seen[0][4]["codex"]["model"], "gpt-5.6-sol")
            self.assertEqual(seen[0][4]["codex"]["reasoning_effort"], "high")

    def test_rejected_attempt_escalates_once_and_preserves_target(self):
        task = self.plan()
        self.run_package(task["id"])
        with self.assertRaises(ValueError):
            self.action("retry", task["id"], evidence="no evidence yet")
        self.action("review", task["id"], accepted=False, artifact="output.txt", evidence="Second line absent")
        with self.assertRaises(ValueError):
            self.action("retry", task["id"], evidence="", contract=CONTRACT)
        with self.assertRaisesRegex(ValueError, "acceptance check"):
            self.action("retry", task["id"], evidence="new target", contract=dict(CONTRACT, acceptance_check="Skip second line"))
        response = self.action("retry", task["id"], evidence="Luna missed the second fixture line")
        retried = response["harness_tasks"][0]
        self.assertEqual(retried["route"]["requested"]["model"], "gpt-5.6-terra")
        self.assertEqual(len(retried["attempts"]), 1)  # Planning spends no provider call.
        self.run_package(task["id"])
        response = self.action("review", task["id"], accepted=True, artifact="output.txt", evidence="Both lines match")
        self.assertEqual(response["harness_tasks"][0]["attempts"][1]["retry_index"], 1)

    def test_direct_failure_requires_a_new_brief_instead_of_downgrading(self):
        task = self.plan(dict(CONTRACT, category="trivial"))
        self.run_package(task["id"])
        self.action("review", task["id"], accepted=False, artifact="output.txt", evidence="Wrong target")
        with self.assertRaisesRegex(ValueError, "no unused escalation"):
            self.action("retry", task["id"], evidence="Sol failed")
        response = self.action("retry", task["id"], evidence="Clarify target", contract={
            **task["contract"], "task": "Repair only the fixture's second line"})
        self.assertEqual(response["harness_tasks"][0]["route"]["requested"]["model"], "gpt-5.6-sol")

    def test_scope_symlink_provider_window_and_review_validation(self):
        with tempfile.TemporaryDirectory() as outside:
            (self.root / "escape").symlink_to(outside, target_is_directory=True)
            with self.assertRaisesRegex(ValueError, "inside the project"):
                self.plan(dict(CONTRACT, write_scope=["escape/new.txt"]))
        with self.assertRaises(KeyError):
            self.app.harness_action(self.parent, {"action": "plan"}, window_id="different-window")
        with self.assertRaisesRegex(ValueError, "different provider"):
            self.app.harness_action(self.parent, {"action": "plan"}, window_provider="claude")
        task = self.plan()
        self.run_package(task["id"])
        with self.assertRaises(ValueError):
            self.action("review", task["id"], accepted=True, artifact="output.txt", evidence="", review_seconds=0)
        with self.assertRaises(ValueError):
            self.action("review", task["id"], accepted=True, artifact="output.txt", evidence="ok", review_seconds=float("nan"))

    def test_duplicate_launch_and_parent_deletion_while_running(self):
        task = self.plan()
        gate = threading.Event()
        self.addCleanup(gate.set)
        def capture(*args):
            gate.wait(5)
            return RunResult("done", 0)
        with patch("pilferedparrot.web.capture_dispatch", side_effect=capture):
            chat = self.action("run", task["id"])
            with self.assertRaises(ValueError): self.action("run", task["id"])
            with self.assertRaises(ValueError): self.app.delete_chat(self.parent)
            with self.assertRaises(ValueError): self.app.send_message(self.parent, {"content": "extra work"})
            with self.app.runs_lock: run = self.app.runs[chat["id"]]
            gate.set(); run.thread.join(5)

    def test_failures_are_reviewable_and_restart_recovers(self):
        task = self.plan()
        self.run_package(task["id"], RunResult("", 7, error="fixture error"))
        with self.assertRaises(ValueError):
            self.action("review", task["id"], accepted=True, artifact="output.txt", evidence="claim")
        response = self.action("review", task["id"], accepted=False, artifact="output.txt", evidence="Provider exited 7")
        self.assertEqual(response["harness_tasks"][0]["status"], "rejected")
        with self.app.store.lock:
            task = self.app.store.get(self.parent)["harness_tasks"][0]
            task["status"] = "running"
            task["attempts"][-1]["status"] = "running"
            self.app.store.save()
        self.app.recover_interrupted()
        task = self.app.chat_state(self.parent)["harness_tasks"][0]
        self.assertEqual(task["status"], "failed")

    def test_http_harness_uses_dashboard_capability(self):
        handler_type = web_server.make_handler(self.app)
        handler = bare_handler(handler_type, path=f"/api/chats/{self.parent}/harness")
        handler._json = lambda payload, status=None: results.append((payload, status))
        results = []
        handler.do_POST()
        self.assertEqual(results[0][1], 403)

    def test_failed_launch_retains_unknown_usage_and_monotonic_effort(self):
        task = self.plan()
        with patch.object(self.app, "send_message", side_effect=ValueError("unsupported setting")), \
                patch("pilferedparrot.web_harness.time.monotonic", side_effect=[20, 23]):
            with self.assertRaisesRegex(ValueError, "unsupported setting"):
                self.action("run", task["id"])
        task = self.app.chat_state(self.parent)["harness_tasks"][0]
        self.assertEqual(task["summary"]["counts"]["failed"], 1)
        self.assertEqual(task["summary"]["elapsed"]["value"], 3)
        self.assertEqual(task["summary"]["elapsed"]["source"], "measured")
        self.assertIsNone(task["summary"]["usage"]["input_tokens"]["value"])
        reloaded = PilferedParrotApp(self.config, self.root)
        self.addCleanup(reloaded.shutdown)
        self.assertEqual(reloaded.chat_state(self.parent)["harness_tasks"][0]["summary"], task["summary"])

    def test_retries_stop_after_three_launches_and_keep_all_outcomes(self):
        task = self.plan()
        for model in ("gpt-5.6-luna", "gpt-5.6-terra", "gpt-5.6-sol"):
            _, seen = self.run_package(task["id"])
            self.assertEqual(seen[0][4]["codex"]["model"], model)
            result = self.action("review", task["id"], accepted=False,
                                 artifact="output.txt", evidence="Required line still missing")
            if model != "gpt-5.6-sol":
                self.action("retry", task["id"], evidence="Artifact does not satisfy the fixed check")
        summary = result["harness_tasks"][0]["summary"]
        self.assertEqual(summary["counts"]["rejected"], 3)
        self.assertEqual(summary["rework_attempts"], 2)
        with self.assertRaisesRegex(ValueError, "three attempts"):
            self.action("retry", task["id"], evidence="Try again")

    def test_retry_cannot_reuse_an_earlier_failed_brief(self):
        task = self.plan()
        self.run_package(task["id"])
        self.action("review", task["id"], accepted=False,
                    artifact="output.txt", evidence="Brief needs a clearer target")
        self.action("retry", task["id"], evidence="Name the target line", contract={
            **task["contract"], "task": "Repair only the second fixture line"})
        self.run_package(task["id"])
        self.action("review", task["id"], accepted=False,
                    artifact="output.txt", evidence="Still omitted the line")
        with self.assertRaisesRegex(ValueError, "identical assignment"):
            self.action("retry", task["id"], evidence="Revert the brief", contract=task["contract"])

    def test_restart_before_message_registration_reconciles_summary(self):
        task = self.plan()
        # Simulate loss after attempt persistence but before send_message can
        # persist its pending response. Recovery has no message to complete.
        with self.app.store.lock:
            stored = self.app.store.get(self.parent)["harness_tasks"][0]
            stored["status"] = "running"
            stored["attempts"] = [{"id": "interrupted", "status": "running",
                                    "contract": deepcopy(stored["contract"]),
                                    "usage_observations": [], "review": None}]
            stored["summary"] = outcome_summary(stored["attempts"])
            self.app.store.save()
        self.app.recover_interrupted()
        recovered = self.app.chat_state(self.parent)["harness_tasks"][0]
        self.assertEqual(recovered["status"], "failed")
        self.assertEqual(recovered["summary"]["counts"]["running"], 0)
        self.assertEqual(recovered["summary"]["counts"]["failed"], 1)
        self.assertIsNone(recovered["summary"]["elapsed"]["value"])
        self.assertIsNone(recovered["summary"]["usage"]["input_tokens"]["value"])
        response = self.action("review", task["id"], accepted=False,
                               artifact="output.txt", evidence="Application stopped before dispatch")
        self.assertEqual(response["harness_tasks"][0]["summary"]["counts"]["rejected"], 1)
