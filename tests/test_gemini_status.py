"""Gemini readiness and tier-error normalization regressions."""

from __future__ import annotations

import copy
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pilferedparrot.adapters import GeminiAdapter
from pilferedparrot.budgets import collect_budgets
from pilferedparrot.config import load_config
from pilferedparrot.model import AUTH_UNKNOWN, STATUS_AUTH_UNVERIFIED, UNREACHABLE, Conversation


class GeminiStatusTests(unittest.TestCase):
    def setUp(self):
        self.config = load_config(Path("/definitely/missing/config.json"))

    def test_authentication_does_not_trust_cached_credentials_or_environment(self):
        adapter = GeminiAdapter("gemini", self.config)
        with patch.dict("os.environ", {
            "GEMINI_API_KEY": "synthetic-key",
            "GOOGLE_API_KEY": "synthetic-google-key",
        }, clear=False), patch.object(Path, "is_file", return_value=True):
            self.assertIsNone(adapter.authentication())

    def test_budget_reports_installed_cli_with_unknown_auth(self):
        config = copy.deepcopy(self.config)
        with patch("pilferedparrot.budgets.provider_ids", return_value=("gemini",)), \
             patch("pilferedparrot.adapters.adapter_for") as factory:
            adapter = factory.return_value
            adapter.available.return_value = True
            result = collect_budgets(config)["gemini"]
        self.assertTrue(result.available)
        self.assertEqual(result.status, STATUS_AUTH_UNVERIFIED)
        self.assertEqual(result.auth_status, AUTH_UNKNOWN)
        self.assertEqual(result.reachability, UNREACHABLE)
        self.assertIn("checked when used", result.note or "")

    def test_unrelated_failed_stderr_is_preserved(self):
        adapter = GeminiAdapter("gemini", self.config)
        with tempfile.TemporaryDirectory() as directory, patch.object(
            adapter, "_command", return_value=["gemini"]
        ), patch(
            "pilferedparrot.dispatch._stream_process",
            return_value=subprocess.CompletedProcess(
                ["gemini"], 7, "", "synthetic provider failure"
            ),
        ):
            result = adapter.run("synthetic prompt", Path(directory), Conversation(provider="gemini"))
        self.assertEqual(result.exit_code, 7)
        self.assertEqual(result.error, "synthetic provider failure")

    def test_tier_error_is_actionable_and_does_not_expose_diagnostics(self):
        adapter = GeminiAdapter("gemini", self.config)
        with tempfile.TemporaryDirectory() as directory, patch.object(
            adapter, "_command", return_value=["gemini"]
        ), patch(
            "pilferedparrot.dispatch._stream_process",
            return_value=subprocess.CompletedProcess(
                ["gemini"], 55, "", "IneligibleTierError: This client is no longer supported for Gemini Code Assist for individuals"
            ),
        ):
            result = adapter.run("synthetic prompt", Path(directory), Conversation(provider="gemini"))
        self.assertEqual(result.exit_code, 55)
        self.assertEqual(
            result.error,
            "Gemini CLI account tier is not supported; configure a Gemini API key or an eligible organization account using the provider setup.",
        )

    def test_successful_stream_is_unchanged(self):
        adapter = GeminiAdapter("gemini", self.config)
        lines = [
            json.dumps({"type": "init", "session_id": "synthetic-session"}),
            json.dumps({"type": "message", "role": "assistant", "content": "PPI_OK"}),
            json.dumps({"type": "result", "status": "success"}),
        ]
        with tempfile.TemporaryDirectory() as directory, patch.object(
            adapter, "_command", return_value=["gemini"]
        ), patch("pilferedparrot.dispatch._stream_process") as stream:
            def fake(*args, stdout_line, **kwargs):
                for line in lines:
                    stdout_line(line)
                return subprocess.CompletedProcess(["gemini"], 0, "", "")
            stream.side_effect = fake
            conversation = Conversation(provider="gemini")
            result = adapter.run("synthetic prompt", Path(directory), conversation)
        self.assertEqual(result.text, "PPI_OK")
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(conversation.provider_session_id, "synthetic-session")


if __name__ == "__main__":
    unittest.main()
