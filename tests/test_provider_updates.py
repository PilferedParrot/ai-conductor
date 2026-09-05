"""Tests for the best-effort provider CLI update checker."""
import json
import subprocess
import unittest
from unittest.mock import patch

from pilferedparrot.provider_updates import check_provider_update


class _Response:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, _size=-1):
        return json.dumps(self.payload).encode()


class ProviderUpdateTests(unittest.TestCase):
    config = {"codex": {"command": "codex"}}

    @patch("pilferedparrot.provider_updates.urllib.request.urlopen")
    @patch("pilferedparrot.provider_updates.subprocess.run")
    @patch("pilferedparrot.provider_updates.resolve_command", return_value="/bin/codex")
    def test_update_available(self, _resolve, run, open_url):
        run.return_value = subprocess.CompletedProcess([], 0, "codex-cli 1.2.3\n", "")
        open_url.return_value = _Response({"version": "1.3.0"})
        result = check_provider_update(self.config, "codex")
        self.assertEqual(result["status"], "update_available")
        self.assertEqual(result["installed_version"], "1.2.3")
        self.assertEqual(result["latest_version"], "1.3.0")
        self.assertEqual(result["update_command"], "npm install -g @openai/codex")
        run.assert_called_once_with(["/bin/codex", "--version"], capture_output=True,
                                    text=True, timeout=5, check=False)

    @patch("pilferedparrot.provider_updates.urllib.request.urlopen")
    @patch("pilferedparrot.provider_updates.subprocess.run")
    @patch("pilferedparrot.provider_updates.resolve_command", return_value="/bin/claude")
    def test_prerelease_and_ahead_versions_are_current(self, _resolve, run, open_url):
        run.return_value = subprocess.CompletedProcess([], 0, "2.0.0\n", "")
        open_url.return_value = _Response({"version": "2.0.0-beta.1"})
        self.assertEqual(check_provider_update({}, "claude")["status"], "current")

    @patch("pilferedparrot.provider_updates.urllib.request.urlopen")
    @patch("pilferedparrot.provider_updates.subprocess.run")
    @patch("pilferedparrot.provider_updates.resolve_command", return_value="/bin/codex")
    def test_numeric_semver_ordering(self, _resolve, run, open_url):
        run.return_value = subprocess.CompletedProcess([], 0, "1.2.9", "")
        open_url.return_value = _Response({"version": "1.2.10"})
        self.assertEqual(check_provider_update({}, "codex")["status"], "update_available")

    @patch("pilferedparrot.provider_updates.resolve_command", return_value=None)
    def test_missing_cli_is_nonfatal(self, _resolve):
        result = check_provider_update({}, "gemini")
        self.assertEqual(result["status"], "unavailable")
        self.assertIsNone(result["installed_version"])

    def test_other_provider_is_not_applicable(self):
        result = check_provider_update({}, "qwen")
        self.assertEqual(result["status"], "not_applicable")
        self.assertIsNone(result["update_command"])

    def test_qwen_is_manual(self):
        result = check_provider_update({}, "qwen")
        self.assertEqual(result["status"], "not_applicable")
        self.assertIn("local model", result["message"])

    @patch("pilferedparrot.provider_updates.urllib.request.urlopen")
    @patch("pilferedparrot.provider_updates.subprocess.run")
    @patch("pilferedparrot.provider_updates.resolve_command", return_value="/bin/claude")
    def test_claude_uses_native_update_command(self, _resolve, run, open_url):
        run.return_value = subprocess.CompletedProcess([], 0, "1.0.0", "")
        open_url.return_value = _Response({"version": "1.0.0"})
        self.assertEqual(check_provider_update({}, "claude")["update_command"], "claude update")

    @patch("pilferedparrot.provider_updates.urllib.request.urlopen")
    @patch("pilferedparrot.provider_updates.subprocess.run")
    @patch("pilferedparrot.provider_updates.resolve_command", return_value="/bin/codex")
    def test_malformed_payload_is_unavailable(self, _resolve, run, open_url):
        run.return_value = subprocess.CompletedProcess([], 0, "1.0.0", "")
        open_url.return_value = _Response({"dist-tags": {"latest": "9.0.0"}})
        self.assertEqual(check_provider_update({}, "codex")["status"], "unavailable")

    @patch("pilferedparrot.provider_updates.subprocess.run", side_effect=subprocess.TimeoutExpired("codex", 5))
    @patch("pilferedparrot.provider_updates.resolve_command", return_value="/bin/codex")
    def test_subprocess_timeout_is_nonfatal(self, _resolve, _run):
        result = check_provider_update({}, "codex")
        self.assertEqual(result["status"], "unavailable")

    @patch("pilferedparrot.provider_updates.urllib.request.urlopen", side_effect=TimeoutError())
    @patch("pilferedparrot.provider_updates.subprocess.run")
    @patch("pilferedparrot.provider_updates.resolve_command", return_value="/bin/gemini")
    def test_registry_timeout_is_nonfatal(self, _resolve, run, _open_url):
        run.return_value = subprocess.CompletedProcess([], 0, "1.0.0", "secret token")
        result = check_provider_update({}, "gemini")
        self.assertEqual(result["status"], "unavailable")
        self.assertNotIn("secret", result["message"])


if __name__ == "__main__":
    unittest.main()
