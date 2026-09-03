from __future__ import annotations

import threading
import unittest
from pathlib import Path
from unittest.mock import Mock

from pilferedparrot.dispatch import RunResult
from pilferedparrot.web_provider import ActiveRun, ProviderRunOrchestrator


class ProviderRunOrchestratorTests(unittest.TestCase):
    def setUp(self):
        self.config = {
            "web": {"chat_reasoning_effort": "high"},
            "codex": {"model": "configured", "sandbox": "workspace-write", "additional_write_dirs": ["/x"]},
            "claude": {}, "gemini": {},
            "custom": {"adapter": "openai_compatible", "model": "custom-model"},
        }

    def test_prepare_resumes_only_matching_provider_and_model(self):
        runner = ProviderRunOrchestrator(self.config)
        prepared = runner.prepare(
            "custom", Path("/work"), model="m", session_id="s",
            provider_messages=[{"role": "user", "content": "old"}],
            current_provider="custom", current_model="m",
        )
        self.assertEqual(prepared.conversation.provider_session_id, "s")
        transcript = getattr(prepared.conversation, "messages", None)
        if transcript is None:
            transcript = prepared.conversation.qwen_messages
        self.assertEqual(transcript[0]["content"], "old")
        reset = runner.prepare(
            "custom", Path("/work"), model="new", session_id="s",
            provider_messages=[{"role": "user", "content": "old"}],
            current_provider="custom", current_model="m",
        )
        self.assertIsNone(reset.conversation.provider_session_id)
        reset_transcript = getattr(reset.conversation, "messages", None)
        if reset_transcript is None:
            reset_transcript = reset.conversation.qwen_messages
        self.assertEqual(reset_transcript, [])

    def test_chat_modes_are_read_only_and_do_not_mutate_config(self):
        runner = ProviderRunOrchestrator(self.config)
        prepared = runner.prepare("codex", Path("/work"), model="m", mode="chat", context_limit_tokens=123)
        self.assertEqual(prepared.config["codex"]["sandbox"], "read-only")
        self.assertEqual(prepared.config["codex"]["additional_write_dirs"], [])
        self.assertEqual(prepared.config["codex"]["context_window_limit_tokens"], 123)
        self.assertEqual(prepared.config["codex"]["reasoning_effort"], "high")
        self.assertEqual(self.config["codex"]["sandbox"], "workspace-write")

    def test_execute_bootstraps_qwen_and_applies_result_telemetry(self):
        dispatch = Mock(return_value=RunResult(
            "done", 0, session_id="new", input_tokens=4, output_tokens=2,
        ))
        ensure = Mock()
        runner = ProviderRunOrchestrator(self.config, dispatch=dispatch, ensure_provider=ensure)
        prepared = runner.prepare("qwen", Path("/work"), model="m")
        active = ActiveRun()
        result = runner.execute("hello", prepared, active)
        ensure.assert_called_once()
        dispatch.assert_called_once_with("qwen", "hello", Path("/work"), prepared.conversation,
                                         prepared.config, active.cancel_event)
        runner.apply_session(result, prepared)
        self.assertEqual(prepared.conversation.provider_session_id, "new")
        self.assertEqual(prepared.conversation.token_usage, {"input_tokens": 4, "output_tokens": 2})

    def test_execute_exposes_progress_callback_on_cancel_event(self):
        callback = Mock()
        dispatch = Mock(return_value=RunResult("", 130))
        runner = ProviderRunOrchestrator(self.config, dispatch=dispatch)
        prepared = runner.prepare("claude", Path("/work"), model="m", mode="chat")
        active = ActiveRun()
        runner.execute("stop", prepared, active, on_progress=callback)
        self.assertIs(getattr(active.cancel_event, "_pilferedparrot_progress"), callback)

    def test_invalid_mode_is_rejected(self):
        with self.assertRaises(ValueError):
            ProviderRunOrchestrator(self.config).prepare("codex", Path("/work"), mode="write")


if __name__ == "__main__":
    unittest.main()
