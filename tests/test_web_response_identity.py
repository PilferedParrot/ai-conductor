"""Response routing evidence survives the application and persistence boundaries."""
from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pilferedparrot.dispatch import RunResult
from pilferedparrot.web import PilferedParrotApp
from tests import test_reasoning


class WebResponseIdentityTests(unittest.TestCase):
    config = test_reasoning.ReasoningTests.config
    wait_for_work = test_reasoning.ReasoningTests.wait_for_work
    wait_for_chat = test_reasoning.ReasoningTests.wait_for_chat

    def test_actual_compatible_response_evidence_persists_in_work_and_chat(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self.config(root)
            config["qwen"].update(model="requested-qwen", base_url="http://127.0.0.1:18080/v1")
            app = PilferedParrotApp(config, root)
            work = app.create_chat({"provider": "qwen", "model": "requested-qwen"})
            sent = []

            def respond(_config, provider, request, **_kwargs):
                sent.append((provider, json.loads(request.data)))
                return io.StringIO(json.dumps({
                    "model": "served-qwen", "choices": [{"message": {
                        "role": "assistant", "content": "A synthetic response.",
                    }}],
                }))

            with patch("pilferedparrot.web.ensure_qwen"), patch(
                "pilferedparrot.qwen.open_compatible_url", side_effect=respond,
            ):
                app.send_message(work["id"], {"content": "Identify your route."})
                work_state = self.wait_for_work(app, work["id"])
                app.reset_chat({"provider": "qwen", "model": "requested-qwen"})
                app.send_chat_message({"content": "Identify your route."}, provider="qwen")
                chat_state = self.wait_for_chat(app)
            self.assertEqual(len(sent), 2)
            for provider, request in sent:
                self.assertEqual(provider, "qwen")
                self.assertEqual(request["model"], "requested-qwen")
                self.assertIn("requested-qwen", request["messages"][0]["content"])
            expected = work_state["messages"][-1]["response_identity"]
            self.assertEqual(expected["requested_model"], "requested-qwen")
            self.assertEqual(expected["reported_models"], ["served-qwen"])
            self.assertTrue(expected["model_mismatch"])
            self.assertEqual(expected["endpoint_origin"], "http://127.0.0.1:18080")
            self.assertEqual(expected["endpoint_kind"], "loopback")
            self.assertEqual(chat_state["messages"][-1]["response_identity"], expected)
            reloaded = PilferedParrotApp(config, root)
            self.assertEqual(reloaded.chat_state(work["id"])["messages"][-1]["response_identity"], expected)
            self.assertEqual(reloaded.current_chat_state()["messages"][-1]["response_identity"], expected)

    def test_cli_request_identity_does_not_claim_server_attestation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            app = PilferedParrotApp(self.config(root), root)
            work = app.create_chat({"provider": "codex", "model": "gpt-reasoning"})
            with patch("pilferedparrot.web.capture_dispatch", return_value=RunResult("done", 0)):
                app.send_message(work["id"], {"content": "hello"})
                state = self.wait_for_work(app, work["id"])
            identity = state["messages"][-1]["response_identity"]
            self.assertEqual(identity["requested_model"], "gpt-reasoning")
            self.assertEqual(identity["endpoint_kind"], "cli")
            self.assertEqual(identity["reported_models"], [])
            self.assertFalse(identity["model_mismatch"])


if __name__ == "__main__":
    unittest.main()
