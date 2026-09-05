"""Regression coverage for compatible-provider adapter boundaries."""
from __future__ import annotations

import os
import urllib.error
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from pilferedparrot.adapters import OpenAICompatibleAdapter
from pilferedparrot.config import DEFAULTS
from pilferedparrot.dispatch import RunResult


class _LegacyConversation:
    """Provider-neutral conversation shape from before identity telemetry."""

    def __init__(self) -> None:
        self.messages: list[dict[str, str]] = []
        self.token_usage: dict[str, int] = {}


class CompatibleAdapterHardeningTests(unittest.TestCase):
    def setUp(self) -> None:
        self.secret = "adapter-discovery-secret"
        self.variable = "PILFEREDPARROT_ADAPTER_DISCOVERY_KEY"
        os.environ[self.variable] = self.secret
        self.addCleanup(os.environ.pop, self.variable, None)
        self.config = deepcopy(DEFAULTS)
        self.config["synthetic"] = {
            "adapter": "openai_compatible",
            "base_url": "https://inference.example/v1",
            "api_key_env": self.variable,
            "model": "test-model",
        }

    def test_run_accepts_provider_neutral_conversation_without_identity_field(self) -> None:
        conversation = _LegacyConversation()
        result = RunResult("done", 0)
        with patch("pilferedparrot.dispatch.run_compatible_agent", return_value="done") as run:
            actual = OpenAICompatibleAdapter("synthetic", self.config).run(
                "hello", Path("/tmp"), conversation,
            )
        self.assertEqual(actual.text, result.text)
        self.assertIsNone(run.call_args.kwargs["response_identity"])

    def test_model_discovery_omits_upstream_http_error_details(self) -> None:
        error = urllib.error.HTTPError(
            "https://inference.example/v1/models", 401, "Unauthorized", {}, None,
        )
        error.read = lambda _size=-1: f"invalid key {self.secret}".encode()
        adapter = OpenAICompatibleAdapter("synthetic", self.config)
        with patch("pilferedparrot.config.open_compatible_url", side_effect=error):
            with self.assertRaisesRegex(RuntimeError, r"model discovery failed \(HTTP 401\)") as caught:
                adapter.models()
        self.assertNotIn(self.secret, str(caught.exception))
        self.assertNotIn("invalid key", str(caught.exception))

    def test_model_discovery_redacts_provider_key_from_transport_errors(self) -> None:
        adapter = OpenAICompatibleAdapter("synthetic", self.config)
        error = urllib.error.URLError(f"connection failed {self.secret}")
        with patch("pilferedparrot.config.open_compatible_url", side_effect=error):
            with self.assertRaisesRegex(RuntimeError, "model discovery failed") as caught:
                adapter.models()
        self.assertNotIn(self.secret, str(caught.exception))
        self.assertIn("[redacted]", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
