"""Focused provider metadata and bounded harness adapter coverage."""
from __future__ import annotations

import json
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

from pilferedparrot.config import load_config
from pilferedparrot.dispatch import (
    RunResult, _claude_command, _codex_command, capture_claude, capture_codex,
)
from pilferedparrot.model import Conversation


class HarnessAdapterTests(unittest.TestCase):
    def _stream(self, events):
        def emit(command, _prompt, _cwd, **kwargs):
            for event in events:
                kwargs["stdout_line"](json.dumps(event) + "\n")
            return subprocess.CompletedProcess(command, 0, "", "")
        return emit

    def test_run_result_metadata_defaults_preserve_legacy_positional_shape(self):
        result = RunResult("done", 0, "session")
        self.assertEqual((result.text, result.exit_code, result.session_id),
                         ("done", 0, "session"))
        self.assertEqual(result.usage_basis, "unknown")
        self.assertIsNone(result.reported_model)
        self.assertIsNone(result.cached_input_tokens)

    @patch("pilferedparrot.dispatch._stream_process")
    @patch("pilferedparrot.dispatch.provider_command", return_value="codex")
    def test_codex_reports_structured_runtime_metadata_and_delta_usage(self, _command, stream):
        stream.side_effect = self._stream([
            {"type": "thread.started", "thread_id": "thread-1"},
            {"type": "turn_context", "payload": {"model": "runtime-model", "effort": "high"}},
            {"type": "item.completed", "item": {
                "type": "agent_message", "text": "requested model=wrong effort=low",
            }},
            {"type": "turn.completed", "usage": {
                "input_tokens": 11, "output_tokens": 7, "cached_input_tokens": 3,
            }},
        ])
        result = capture_codex("hello", Path.cwd(), Conversation(), load_config())
        self.assertEqual(result.reported_model, "runtime-model")
        self.assertEqual(result.reported_reasoning_effort, "high")
        self.assertEqual(result.usage_basis, "delta")
        self.assertEqual(result.cached_input_tokens, 3)
        self.assertEqual((result.input_tokens, result.output_tokens), (11, 7))

    @patch("pilferedparrot.dispatch._stream_process")
    @patch("pilferedparrot.dispatch.provider_command", return_value="codex")
    def test_codex_missing_usage_remains_unknown_and_null(self, _command, stream):
        stream.side_effect = self._stream([
            {"type": "item.completed", "item": {
                "type": "agent_message", "text": "No usage reported",
            }},
        ])
        result = capture_codex("hello", Path.cwd(), Conversation(), load_config())
        self.assertEqual(result.usage_basis, "unknown")
        self.assertIsNone(result.input_tokens)
        self.assertIsNone(result.output_tokens)
        self.assertIsNone(result.cached_input_tokens)

    @patch("pilferedparrot.dispatch._stream_process")
    @patch("pilferedparrot.dispatch.provider_command", return_value="claude")
    def test_claude_resume_keeps_usage_scope_unknown_and_uses_runtime_model(self, _command, stream):
        config = load_config()
        config["claude"]["reasoning_effort"] = "high"
        conversation = Conversation(provider_session_id="session-1")
        stream.side_effect = self._stream([
            {"type": "system", "subtype": "init", "session_id": "session-1",
             "model": "runtime-claude"},
            {"type": "assistant", "message": {"model": "runtime-claude",
             "content": [{"type": "text", "text": "done"}]}},
            {"type": "result", "result": "done", "usage": {
                "input_tokens": 20, "output_tokens": 4,
                "cache_read_input_tokens": 100, "cache_creation_input_tokens": 50,
            }},
        ])
        result = capture_claude("hello", Path.cwd(), conversation, config)
        command = stream.call_args.args[0]
        self.assertIn("--effort", command)
        self.assertEqual(result.reported_model, "runtime-claude")
        self.assertEqual(result.usage_basis, "unknown")
        self.assertEqual(result.cached_input_tokens, None)
        self.assertEqual(result.reported_usage["cache_read_input_tokens"], 100)
        self.assertEqual(result.reported_usage["cache_creation_input_tokens"], 50)

    @patch("pilferedparrot.dispatch.provider_command", side_effect=lambda _config, provider: provider)
    def test_bounded_harness_limits_provider_native_nested_workers(self, _command):
        config = load_config()
        config["_harness"] = {"bounded": True}
        codex = _codex_command(Conversation(), config, Path.cwd())
        claude = _claude_command(Conversation(), config)
        self.assertIn("agents.enabled=false", codex)
        self.assertEqual(claude[claude.index("--disallowedTools") + 1:], ["Agent", "Task"])


if __name__ == "__main__":
    unittest.main()
