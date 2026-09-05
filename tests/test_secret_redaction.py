"""Offline regression coverage for configured API-key reflection."""
from __future__ import annotations

import io
import json
import os
import secrets
import tempfile
import time
import traceback
import unittest
from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
from pathlib import Path
from urllib.error import HTTPError, URLError
from unittest.mock import patch


PRIMARY_ENV = "PILFEREDPARROT_TEST_SYNTHETIC_KEY"
OVERLAPPING_ENV = "PILFEREDPARROT_TEST_SYNTHETIC_KEY_EXTENDED"


class FakeResponse:
    def __init__(self, payload: dict[str, object]):
        self.payload = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, *_args):
        return self.payload


class IsolatedSecretRedactionTests(unittest.TestCase):
    def setUp(self):
        from pilferedparrot.config import DEFAULTS

        self.directory = tempfile.TemporaryDirectory(prefix="pilferedparrot-secret-redaction-")
        self.root = Path(self.directory.name)
        self.primary_secret = "synthetic-" + secrets.token_urlsafe(24)
        self.overlapping_secret = self.primary_secret + "-extended"
        os.environ[PRIMARY_ENV] = self.primary_secret
        os.environ[OVERLAPPING_ENV] = self.overlapping_secret
        self.addCleanup(self.directory.cleanup)
        self.addCleanup(os.environ.pop, PRIMARY_ENV, None)
        self.addCleanup(os.environ.pop, OVERLAPPING_ENV, None)

        self.config = deepcopy(DEFAULTS)
        self.config["codex"]["config_path"] = str(self.root / "missing-codex.toml")
        self.config["codex"]["models_cache"] = str(self.root / "missing-models.json")
        self.config["web"].update({
            "chat_store": str(self.root / "chats.json"),
            "model_catalog_store": str(self.root / "models.json"),
            "default_provider": "synthetic",
        })
        self.config["ledger"] = str(self.root / "runs.jsonl")
        self.config["_hidden_providers"] = ["qwen", "codex", "claude", "gemini", "antigravity"]
        self.config["provider_definitions"] = {
            "synthetic": self._provider_definition(PRIMARY_ENV),
            "related": self._provider_definition(OVERLAPPING_ENV),
        }
        self.config["synthetic"] = self._provider_config(PRIMARY_ENV)
        self.config["related"] = self._provider_config(OVERLAPPING_ENV)

    @staticmethod
    def _provider_definition(api_key_env: str) -> dict[str, object]:
        return {
            "label": "Synthetic compatible provider",
            "adapter": "openai_compatible",
            "base_url": "https://synthetic.invalid/v1",
            "api_key_env": api_key_env,
            "model": "synthetic-model",
        }

    @staticmethod
    def _provider_config(api_key_env: str) -> dict[str, object]:
        return {
            "adapter": "openai_compatible",
            "base_url": "https://synthetic.invalid/v1",
            "api_key_env": api_key_env,
            "model": "synthetic-model",
            "agent_max_tokens": 16,
            "agent_request_timeout_seconds": 1,
            "max_tool_turns": 2,
            "tool_output_chars": 1_000,
            "file_limit_bytes": 1_000,
            "shell_timeout_seconds": 1,
            "shell_max_timeout_seconds": 1,
            "shell_network": False,
            "allow_home_workspace": False,
            "additional_dirs": [],
        }

    def _assert_secret_free(self, value: object) -> None:
        rendered = str(value)
        self.assertFalse(self.primary_secret in rendered)
        self.assertFalse(self.overlapping_secret in rendered)

    def _reflected_http_error(self, *_args, **_kwargs):
        raise HTTPError(
            "https://synthetic.invalid/v1/chat/completions", 502, "upstream failure", {},
            io.BytesIO(("upstream detail: " + self.overlapping_secret).encode("utf-8")),
        )

    def _wait_for_work_message(self, app, chat_id: str) -> dict[str, object]:
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            with app.store.lock:
                chat = app.store.public(app.store.get(chat_id))
            if not any(message.get("pending") for message in chat["messages"]):
                return chat["messages"][-1]
            time.sleep(0.01)
        self.fail("isolated work request did not finish")

    def test_redactor_uses_configured_values_and_longest_match_first(self):
        from pilferedparrot.config import redact_configured_secrets

        redacted = redact_configured_secrets(self.config, self.overlapping_secret)
        self._assert_secret_free(redacted)
        self.assertNotIn("[redacted]-extended", redacted)

    def test_request_http_and_transport_errors_redact_traceback_text(self):
        from pilferedparrot.qwen import _chat_completion

        with patch("pilferedparrot.qwen.open_compatible_url",
                   side_effect=self._reflected_http_error):
            with self.assertRaises(RuntimeError) as raised:
                _chat_completion([], self.config, "synthetic")
        self._assert_secret_free(raised.exception)
        self._assert_secret_free("".join(traceback.format_exception(raised.exception)))
        self.assertIsNone(raised.exception.__cause__)
        self.assertTrue(raised.exception.__suppress_context__)

        with patch("pilferedparrot.qwen.open_compatible_url",
                   side_effect=URLError("transport detail: " + self.overlapping_secret)):
            with self.assertRaises(RuntimeError) as raised:
                _chat_completion([], self.config, "synthetic")
        self._assert_secret_free(raised.exception)
        self._assert_secret_free("".join(traceback.format_exception(raised.exception)))
        self.assertIsNone(raised.exception.__cause__)
        self.assertTrue(raised.exception.__suppress_context__)

    def test_discovery_and_model_poll_warnings_redact_transport_reasons(self):
        from pilferedparrot.web import PilferedParrotApp

        with patch("pilferedparrot.web.open_compatible_url",
                   side_effect=URLError("discovery detail: " + self.primary_secret)):
            with self.assertRaises(ValueError) as raised:
                PilferedParrotApp._discover_provider_models(
                    "https://synthetic.invalid/v1", PRIMARY_ENV,
                )
        self._assert_secret_free(raised.exception)
        self._assert_secret_free("".join(traceback.format_exception(raised.exception)))
        self.assertIsNone(raised.exception.__cause__)
        self.assertTrue(raised.exception.__suppress_context__)

        app = PilferedParrotApp(self.config, self.root)
        self.addCleanup(app.shutdown)
        with patch("pilferedparrot.config.open_compatible_url",
                   side_effect=URLError("poll detail: " + self.overlapping_secret)):
            warning = app.poll_provider_models("synthetic")
        self.assertEqual(warning["source"], "configured_fallback")
        self._assert_secret_free(json.dumps(warning))

    def test_real_app_success_and_error_surfaces_are_secret_free(self):
        from pilferedparrot.web import PilferedParrotApp

        app = PilferedParrotApp(self.config, self.root)
        self.addCleanup(app.shutdown)
        captured = io.StringIO()
        success = {"choices": [{"message": {"role": "assistant", "content": "ready"}}]}
        with redirect_stdout(captured), redirect_stderr(captured), \
             patch("pilferedparrot.qwen.open_compatible_url", return_value=FakeResponse(success)):
            chat = app.create_chat({
                "provider": "synthetic", "model": "synthetic-model", "cwd": str(self.root),
            })
            app.send_message(chat["id"], {"content": "return ready", "provider": "synthetic"})
            completed = self._wait_for_work_message(app, chat["id"])
        self.assertEqual(completed["content"], "ready")
        self.assertFalse(completed.get("error"))
        self._assert_secret_free(json.dumps(app.state()))
        self._assert_secret_free(Path(self.config["web"]["chat_store"]).read_text())
        self._assert_secret_free(Path(self.config["ledger"]).read_text())
        self._assert_secret_free(captured.getvalue())

        with redirect_stdout(captured), redirect_stderr(captured), \
             patch("pilferedparrot.qwen.open_compatible_url",
                   side_effect=self._reflected_http_error):
            chat = app.create_chat({
                "provider": "synthetic", "model": "synthetic-model", "cwd": str(self.root),
            })
            app.send_message(chat["id"], {"content": "return ready", "provider": "synthetic"})
            completed = self._wait_for_work_message(app, chat["id"])
        self.assertTrue(completed.get("error"))
        self._assert_secret_free(json.dumps(app.state()))
        self._assert_secret_free(Path(self.config["web"]["chat_store"]).read_text())
        self._assert_secret_free(Path(self.config["ledger"]).read_text())
        self._assert_secret_free(captured.getvalue())

    def test_structured_provider_failure_is_redacted_before_persistence(self):
        from pilferedparrot.dispatch import RunResult
        from pilferedparrot.web import PilferedParrotApp

        app = PilferedParrotApp(self.config, self.root)
        self.addCleanup(app.shutdown)
        with patch("pilferedparrot.web.capture_dispatch", return_value=RunResult(
            "failed: " + self.primary_secret, 1,
            error="detail: " + self.overlapping_secret,
        )):
            chat = app.create_chat({"provider": "synthetic", "cwd": str(self.root)})
            app.send_message(chat["id"], {"content": "return ready", "provider": "synthetic"})
            completed = self._wait_for_work_message(app, chat["id"])
        self.assertTrue(completed.get("error"))
        self._assert_secret_free(json.dumps(app.state()))
        self._assert_secret_free(Path(self.config["web"]["chat_store"]).read_text())
        self._assert_secret_free(Path(self.config["ledger"]).read_text())


if __name__ == "__main__":
    unittest.main()
