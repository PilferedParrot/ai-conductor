"""Claude CLI authentication normalization and secret-boundary regressions."""

from __future__ import annotations

import json
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from pilferedparrot.budgets import read_claude_status
from pilferedparrot.cli import budget_text
from pilferedparrot.config import load_config
from pilferedparrot.ledger import append_run
from pilferedparrot.model import (
    AUTH_SIGNED_IN,
    AUTH_SIGNED_OUT,
    AUTH_UNKNOWN,
    REACHABLE,
    STATUS_AUTH_UNVERIFIED,
    STATUS_CLI_MISSING,
    STATUS_OK,
    STATUS_SIGNED_OUT,
    UNREACHABLE,
    USAGE_UNAVAILABLE,
    USAGE_UNSUPPORTED,
    ProviderBudget,
)
from pilferedparrot.web import PilferedParrotApp, make_handler


class ClaudeStatusNormalizationTests(unittest.TestCase):
    def setUp(self):
        self.config = load_config(Path("/definitely/missing/config.json"))

    def _read(self, returncode: int, stdout: str, stderr: str = ""):
        completed = subprocess.CompletedProcess([], returncode, stdout, stderr)
        with patch("pilferedparrot.budgets.resolve_command", return_value="/usr/bin/claude"), \
             patch("pilferedparrot.budgets.subprocess.run", return_value=completed):
            return read_claude_status(self.config)

    def assert_unknown(self, status):
        self.assertFalse(status.available)
        self.assertEqual(status.status, STATUS_AUTH_UNVERIFIED)
        self.assertEqual(status.auth_status, AUTH_UNKNOWN)
        self.assertEqual(status.reachability, UNREACHABLE)
        self.assertEqual(status.usage_status, USAGE_UNSUPPORTED)

    def test_signed_in_uses_only_supported_logged_in_boolean(self):
        status = self._read(0, json.dumps({
            "loggedIn": True,
            "authMethod": "claude.ai",
            "email": "private@example.invalid",
        }))
        self.assertTrue(status.available)
        self.assertEqual(status.status, STATUS_OK)
        self.assertEqual(status.auth_status, AUTH_SIGNED_IN)
        self.assertEqual(status.reachability, REACHABLE)

    def test_signed_out_requires_successful_explicit_false(self):
        status = self._read(0, json.dumps({"loggedIn": False}))
        self.assertFalse(status.available)
        self.assertEqual(status.status, STATUS_SIGNED_OUT)
        self.assertEqual(status.auth_status, AUTH_SIGNED_OUT)
        self.assertEqual(status.reachability, UNREACHABLE)

    def test_expiration_like_failure_is_unknown(self):
        status = self._read(
            1,
            json.dumps({"loggedIn": False, "error": "OAuth token expired"}),
            "authentication expired; sign in again",
        )
        self.assert_unknown(status)
        self.assertNotIn("expired", json.dumps(status.as_dict()).lower())

    def test_malformed_or_incomplete_success_is_unknown(self):
        for stdout in ("not json", "[]", "{}", json.dumps({"loggedIn": "yes"})):
            with self.subTest(stdout=stdout):
                self.assert_unknown(self._read(0, stdout))

    def test_timeout_is_unknown(self):
        with patch("pilferedparrot.budgets.resolve_command", return_value="/usr/bin/claude"), \
             patch(
                 "pilferedparrot.budgets.subprocess.run",
                 side_effect=subprocess.TimeoutExpired(
                     ["claude", "auth", "status", "--json"], 5,
                    output="sensitive-timeout-stdout", stderr="sensitive-timeout-stderr",
                 ),
             ):
            status = read_claude_status(self.config)
        self.assert_unknown(status)
        self.assertEqual(status.note, "auth unverifiable (`claude auth status` timed out)")

    def test_missing_cli_is_unknown(self):
        with patch("pilferedparrot.budgets.resolve_command", return_value=None):
            status = read_claude_status(self.config)
        self.assertFalse(status.available)
        self.assertEqual(status.status, STATUS_CLI_MISSING)
        self.assertEqual(status.auth_status, AUTH_UNKNOWN)
        self.assertEqual(status.reachability, UNREACHABLE)
        self.assertEqual(status.usage_status, USAGE_UNSUPPORTED)


class ClaudeSecretSafetyTests(unittest.TestCase):
    def test_diagnostics_use_provider_usage_status_without_inventing_a_warning(self):
        unavailable = ProviderBudget(
            "future-provider", True, auth_status=AUTH_SIGNED_IN,
            usage_status=USAGE_UNAVAILABLE, usage_note="Provider-owned explanation",
        )
        unexplained = ProviderBudget(
            "future-provider", True, auth_status=AUTH_SIGNED_IN,
            usage_status=USAGE_UNAVAILABLE,
        )
        self.assertEqual(budget_text(unavailable), "signed in; Provider-owned explanation")
        self.assertEqual(budget_text(unexplained), "ready")

    def test_cli_stdout_and_stderr_secrets_cannot_reach_public_or_persistent_sinks(self):
        stdout_secret = "sensitive-stdout-credential-marker"
        stderr_secret = "sensitive-stderr-credential-marker"
        stdout = json.dumps({
            "loggedIn": True,
            "authMethod": "claude.ai",
            "email": "private@example.invalid",
            "accessToken": stdout_secret,
        })
        completed = subprocess.CompletedProcess([], 0, stdout, stderr_secret)

        with tempfile.TemporaryDirectory() as directory, \
             patch("pilferedparrot.budgets.resolve_command", return_value="/usr/bin/claude"), \
             patch("pilferedparrot.budgets.subprocess.run", return_value=completed):
            root = Path(directory)
            config = load_config(root / "missing-config.json")
            config["web"]["chat_store"] = str(root / "chats.json")
            config["web"]["model_catalog_store"] = str(root / "models.json")
            config["ledger"] = str(root / "runs.jsonl")
            status = read_claude_status(config)
            app = PilferedParrotApp(config, root)
            app.budget_snapshot = {"claude": status}
            app.budget_refreshed_at = time.monotonic()

            handler_type = make_handler(app)
            handler = object.__new__(handler_type)
            handler.path = "/api/budgets"
            handler.client_address = ("127.0.0.1", 12345)
            handler.headers = {
                "Host": "127.0.0.1:8765",
                "X-PilferedParrot-Capability": app.dashboard_capability,
            }
            handler._json = MagicMock()
            handler.do_GET()
            browser_response = json.dumps(handler._json.call_args.args[0])

            diagnostics = budget_text(status)
            app.create_chat({"provider": "claude", "cwd": directory})
            chat_persistence = (root / "chats.json").read_text(encoding="utf-8")
            append_run(
                config["ledger"], provider="claude", prompt="safe prompt", cwd=root,
                session_id="safe-session", budgets={"claude": status}, exit_code=0,
            )
            ledger = (root / "runs.jsonl").read_text(encoding="utf-8")

        for sink_name, contents in {
            "browser API response": browser_response,
            "diagnostics": diagnostics,
            "chat persistence": chat_persistence,
            "ledger": ledger,
        }.items():
            with self.subTest(sink=sink_name):
                self.assertNotIn(stdout_secret, contents)
                self.assertNotIn(stderr_secret, contents)
                self.assertNotIn("private@example.invalid", contents)

    def test_legacy_claude_allowance_record_stays_readable_but_current_status_has_no_window(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ledger_path = root / "runs.jsonl"
            legacy = {
                "timestamp": 1,
                "provider": "claude",
                "budgets": {"claude": {
                    "provider": "claude",
                    "available": True,
                    "window": {"remaining_percent": 42, "resets_at": 1_800_000_000},
                    "windows": [{
                        "remaining_percent": 42,
                        "resets_at": 1_800_000_000,
                        "label": "Weekly included usage",
                    }],
                }},
                "exit_code": 0,
            }
            ledger_path.write_text(json.dumps(legacy) + "\n", encoding="utf-8")
            append_run(
                str(ledger_path), provider="claude", prompt="new", cwd=root,
                session_id=None, budgets={}, exit_code=0,
            )
            records = [json.loads(line) for line in ledger_path.read_text().splitlines()]

            completed = subprocess.CompletedProcess([], 0, '{"loggedIn":true}', "")
            config = load_config(root / "missing-config.json")
            with patch("pilferedparrot.budgets.resolve_command", return_value="/usr/bin/claude"), \
                 patch("pilferedparrot.budgets.subprocess.run", return_value=completed):
                current = read_claude_status(config)

        self.assertEqual(records[0], legacy)
        self.assertEqual(records[0]["budgets"]["claude"]["windows"][0]["remaining_percent"], 42)
        self.assertEqual(len(records), 2)
        self.assertIsNone(current.window)
        self.assertEqual(current.windows, ())
        self.assertEqual(current.usage_status, USAGE_UNSUPPORTED)


if __name__ == "__main__":
    unittest.main()
