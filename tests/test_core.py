import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ai_conductor.budgets import (
    claude_budget_from_payload,
    codex_budget_from_response,
    read_claude_budget,
    write_claude_cache,
)
from ai_conductor.config import load_config
from ai_conductor.model import ProviderBudget, RouteDecision
from ai_conductor.qwen import run_qwen_agent
from ai_conductor.qwen_tools import QwenToolbox
from ai_conductor.router import enforce_constraints, parse_decision


class BudgetTests(unittest.TestCase):
    def test_claude_status_payload(self):
        budget = claude_budget_from_payload({
            "rate_limits": {"five_hour": {"used_percentage": 37, "resets_at": 1234}}
        }, 1000)
        self.assertEqual(budget.window.remaining_percent, 63)
        self.assertEqual(budget.window.window_minutes, 300)
        self.assertEqual(budget.window.resets_at, 1234)

    def test_codex_app_server_payload(self):
        budget = codex_budget_from_response({
            "rateLimits": {"primary": {
                "usedPercent": 12, "windowDurationMins": 10080, "resetsAt": 5678
            }}
        })
        self.assertEqual(budget.window.remaining_percent, 88)
        self.assertEqual(budget.window.window_minutes, 10080)

    def test_cache_is_private_json(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "budget.json"
            write_claude_cache({"rate_limits": {}}, path)
            cached = json.loads(path.read_text())
            self.assertIn("observed_at", cached)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    @patch("ai_conductor.budgets.shutil.which", return_value="/usr/bin/claude")
    @patch("ai_conductor.budgets.subprocess.run")
    def test_signed_out_claude_is_unavailable(self, run, _which):
        run.return_value.returncode = 0
        run.return_value.stdout = '{"loggedIn": false, "authMethod": "none"}'
        budget = read_claude_budget(load_config())
        self.assertFalse(budget.available)
        self.assertIn("auth login", budget.note)   # the note must name the actual command


class RouterTests(unittest.TestCase):
    def setUp(self):
        self.config = load_config()

    def test_parse_strict_route(self):
        decision = parse_decision(json.dumps({
            "backend": "codex", "alternates": ["qwen", "claude"],
            "reason": "repository implementation", "mode": "work",
        }))
        self.assertEqual(decision.backend, "codex")

    def test_rejects_incomplete_ranking(self):
        with self.assertRaises(ValueError):
            parse_decision(json.dumps({
                "backend": "codex", "alternates": ["qwen", "qwen"],
                "reason": "bad", "mode": "work",
            }))

    def test_host_reserve_uses_qwen_rank_order(self):
        decision = RouteDecision("claude", ("codex", "qwen"), "nuance")
        budgets = {
            "claude": ProviderBudget("claude", True, _window(90)),
            "codex": ProviderBudget("codex", True, _window(10)),
            "qwen": ProviderBudget("qwen", True),
        }
        provider, note = enforce_constraints(decision, budgets, self.config)
        self.assertEqual(provider, "codex")
        self.assertIn("constraint", note)


class QwenToolTests(unittest.TestCase):
    def setUp(self):
        project_root = Path(__file__).resolve().parent.parent
        self.directory = tempfile.TemporaryDirectory(dir=project_root)
        self.root = Path(self.directory.name)
        self.config = load_config()["qwen"]
        self.tools = QwenToolbox(self.root, self.config)

    def tearDown(self):
        self.directory.cleanup()

    def test_file_edit_returns_diff(self):
        (self.root / "example.txt").write_text("alpha\nbeta\n", encoding="utf-8")
        read = self.tools.execute("read_file", {"path": "example.txt"})
        self.assertIn("2  beta", read)
        diff = self.tools.execute("edit_file", {
            "path": "example.txt", "old_text": "beta", "new_text": "gamma",
        })
        self.assertIn("-beta", diff)
        self.assertIn("+gamma", diff)

    def test_file_tool_rejects_workspace_escape(self):
        with self.assertRaises(PermissionError):
            self.tools.execute("read_file", {"path": "../outside.txt"})

    def test_shell_is_writable_only_in_workspace(self):
        outside = self.root.parent / f".{self.root.name}-outside"
        self.assertFalse(outside.exists())
        result = self.tools.execute("shell", {
            "command": f"touch allowed; touch {outside}",
        })
        self.assertIn("exit_code: 1", result)
        self.assertTrue((self.root / "allowed").exists())
        self.assertFalse(outside.exists())

    @patch("ai_conductor.qwen._chat_completion")
    def test_agent_executes_tool_call_and_keeps_protocol_messages(self, complete):
        complete.side_effect = [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{
                    "id": "call-1",
                    "type": "function",
                    "function": {
                        "name": "write_file",
                        "arguments": json.dumps({"path": "made.txt", "content": "done\n"}),
                    },
                }],
            },
            {"role": "assistant", "content": "Created the file."},
        ]
        messages = []
        answer = run_qwen_agent("create it", messages, load_config(), self.root)
        self.assertEqual(answer, "Created the file.")
        self.assertEqual((self.root / "made.txt").read_text(), "done\n")
        self.assertEqual(messages[2]["role"], "tool")
        self.assertEqual(messages[2]["tool_call_id"], "call-1")
        self.assertEqual(complete.call_count, 2)


class ClaudeAuthProbeTests(unittest.TestCase):
    """The probe has THREE outcomes, and `unknown` must never read as `available`."""

    def _budget(self, run, **attrs):
        for key, value in attrs.items():
            setattr(run.return_value, key, value)
        return read_claude_budget(load_config())

    @patch("ai_conductor.budgets.shutil.which", return_value="/usr/bin/claude")
    @patch("ai_conductor.budgets.subprocess.run")
    def test_unparseable_status_is_unavailable(self, run, _which):
        budget = self._budget(run, returncode=0, stdout="not json at all", stderr="")
        self.assertFalse(budget.available)
        self.assertIn("unverifiable", budget.note)

    @patch("ai_conductor.budgets.shutil.which", return_value="/usr/bin/claude")
    @patch("ai_conductor.budgets.subprocess.run")
    def test_missing_loggedin_field_is_unavailable(self, run, _which):
        budget = self._budget(run, returncode=0, stdout='{"authMethod": "none"}', stderr="")
        self.assertFalse(budget.available)
        self.assertIn("unverifiable", budget.note)

    @patch("ai_conductor.budgets.shutil.which", return_value="/usr/bin/claude")
    @patch("ai_conductor.budgets.subprocess.run")
    def test_nonzero_exit_is_unavailable(self, run, _which):
        budget = self._budget(run, returncode=1, stdout="", stderr="boom")
        self.assertFalse(budget.available)
        self.assertIn("unverifiable", budget.note)

    @patch("ai_conductor.budgets.shutil.which", return_value="/usr/bin/claude")
    @patch("ai_conductor.budgets.subprocess.run",
           side_effect=subprocess.TimeoutExpired("claude", 5))
    def test_timeout_is_unavailable(self, _run, _which):
        budget = read_claude_budget(load_config())
        self.assertFalse(budget.available)
        self.assertIn("unverifiable", budget.note)

    @patch("ai_conductor.budgets.shutil.which", return_value="/usr/bin/claude")
    @patch("ai_conductor.budgets.subprocess.run")
    def test_logged_in_without_telemetry_stays_available(self, run, _which):
        """Auth is proven; only the budget is unknown. That must NOT exclude Claude."""
        with tempfile.TemporaryDirectory() as directory:
            config = load_config()
            config["claude"]["budget_cache"] = str(Path(directory) / "absent.json")
            run.return_value.returncode = 0
            run.return_value.stdout = '{"loggedIn": true, "authMethod": "oauth"}'
            run.return_value.stderr = ""
            budget = read_claude_budget(config)
        self.assertTrue(budget.available)
        self.assertIn("telemetry", budget.note)


def _window(used):
    from ai_conductor.model import BudgetWindow
    return BudgetWindow(used)


if __name__ == "__main__":
    unittest.main()
