"""Offline coverage for compatible-provider response identity evidence."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from pilferedparrot.adapters import OpenAICompatibleAdapter
from pilferedparrot.config import DEFAULTS
from pilferedparrot.model import Conversation
from pilferedparrot.response_identity import configured_identity, record_reported_model


class FakeResponse:
    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = json.dumps(payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, *_args):
        return self.payload


class CompatibleResponseIdentityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.secret = "identity-test-secret"
        self.variable = "PILFEREDPARROT_RESPONSE_IDENTITY_KEY"
        os.environ[self.variable] = self.secret
        self.addCleanup(os.environ.pop, self.variable, None)
        self.config = deepcopy(DEFAULTS)
        self.config["synthetic"] = {
            "adapter": "openai_compatible",
            "base_url": f"https://user:{self.secret}@inference.example/v1?key={self.secret}",
            "api_key_env": self.variable,
            "model": f"requested-{self.secret}",
            "agent_max_tokens": 16,
            "agent_request_timeout_seconds": 1,
            "max_tool_turns": 3,
            "tool_output_chars": 1_000,
            "file_limit_bytes": 1_000,
            "shell_timeout_seconds": 1,
            "shell_max_timeout_seconds": 1,
            "shell_network": False,
            "allow_home_workspace": False,
            "additional_dirs": [],
        }

    def test_configured_identity_hides_credentials_and_marks_dns_unknown(self) -> None:
        identity = configured_identity(self.config, "synthetic")
        rendered = json.dumps(identity)
        self.assertNotIn(self.secret, rendered)
        self.assertEqual(identity["endpoint_origin"], "https://inference.example")
        self.assertEqual(identity["endpoint_kind"], "unknown")
        self.assertEqual(identity["requested_model"], "requested-[redacted]")

    def test_literal_endpoints_are_classified_without_exposing_url_extras(self) -> None:
        for url, origin, kind in [
            ("http://127.0.0.1:18080/v1", "http://127.0.0.1:18080", "loopback"),
            ("http://[::1]:18080/v1", "http://[::1]:18080", "loopback"),
            ("http://192.168.1.2:8000/v1", "http://192.168.1.2:8000", "local-network"),
            ("https://8.8.8.8/v1?key=hidden", "https://8.8.8.8", "remote"),
            ("not a URL", None, "unknown"),
        ]:
            with self.subTest(url=url):
                self.config["synthetic"]["base_url"] = url
                identity = configured_identity(self.config, "synthetic")
                self.assertEqual(identity["endpoint_origin"], origin)
                self.assertEqual(identity["endpoint_kind"], kind)

    def test_missing_matching_and_bounded_reported_models_and_reset(self) -> None:
        self.config["synthetic"]["model"] = "same-model"
        identity = configured_identity(self.config, "synthetic")
        for value in (None, {}, "", "x" * 257):
            record_reported_model(identity, value, self.config)
        self.assertEqual(identity["reported_models"], [])
        record_reported_model(identity, "same-model", self.config)
        record_reported_model(identity, "same-model", self.config)
        self.assertEqual(identity["reported_models"], ["same-model"])
        self.assertFalse(identity["model_mismatch"])
        for number in range(40):
            record_reported_model(identity, f"other-{number}", self.config)
        self.assertEqual(len(identity["reported_models"]), 24)
        self.assertTrue(identity["model_mismatch"])
        conversation = Conversation(response_identity=identity)
        conversation.reset()
        self.assertEqual(conversation.response_identity, {})

    def test_adapter_records_server_models_across_tool_turn_and_keeps_prompt_safe(self) -> None:
        requests: list[dict[str, object]] = []
        responses = [
            {
                "model": f"served-{self.secret}",
                "choices": [{"model": f"alias-{self.secret}", "message": {
                    "role": "assistant", "content": "", "tool_calls": [{
                        "id": "read-1", "type": "function", "function": {
                            "name": "read_file", "arguments": '{"path":"sample.txt"}',
                        },
                    }],
                }}],
            },
            {"choices": [{"message": {"role": "assistant", "content": "done"}}]},
        ]

        def open_url(_config, _provider, request, *, timeout):
            self.assertEqual(timeout, 1)
            requests.append(json.loads(request.data.decode()))
            return FakeResponse(responses.pop(0))

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "sample.txt").write_text("contents")
            conversation = Conversation(provider="synthetic")
            adapter = OpenAICompatibleAdapter("synthetic", self.config)
            with patch("pilferedparrot.qwen.open_compatible_url", side_effect=open_url):
                result = adapter.run("read the file", root, conversation)

        self.assertEqual(result.text, "done")
        self.assertEqual(len(requests), 2)
        self.assertEqual(requests[1]["messages"][-1]["role"], "tool")
        system_prompt = str(requests[0]["messages"][0]["content"])
        self.assertIn("requested-[redacted]", system_prompt)
        self.assertIn("https://inference.example", system_prompt)
        self.assertNotIn(self.secret, system_prompt)
        self.assertEqual(conversation.response_identity["reported_models"], [
            "served-[redacted]", "alias-[redacted]",
        ])
        self.assertTrue(conversation.response_identity["model_mismatch"])
        self.assertNotIn(self.secret, json.dumps(conversation.response_identity))
