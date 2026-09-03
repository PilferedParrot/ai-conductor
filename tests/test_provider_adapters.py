"""Provider contract and native Gemini normalization coverage."""

import json
import subprocess
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from pilferedparrot.adapters import (
    ClaudeAdapter,
    CodexAdapter,
    GeminiAdapter,
    OpenAICompatibleAdapter,
    ProgressEvent,
    ProviderAdapter,
    ProviderCapabilities,
    TokenUsage,
    adapter_for,
)
from pilferedparrot.config import load_config
from pilferedparrot.dispatch import RunResult
from pilferedparrot.model import (
    AUTH_SIGNED_IN, Conversation, ProviderBudget, USAGE_UNSUPPORTED,
)


class ProviderAdapterTests(unittest.TestCase):
    def test_contract_and_registry_expose_provider_neutral_operations(self):
        config = load_config("/definitely/missing/config.json")
        expected = {
            "codex": CodexAdapter,
            "claude": ClaudeAdapter,
            "gemini": GeminiAdapter,
            "qwen": OpenAICompatibleAdapter,
        }
        for provider, adapter_type in expected.items():
            adapter = adapter_for(provider, config)
            self.assertIsInstance(adapter, ProviderAdapter)
            self.assertIsInstance(adapter.capabilities, ProviderCapabilities)
            for operation in (
                "run", "resume", "cancel", "usage", "context_usage",
                "models", "authentication", "availability",
            ):
                self.assertTrue(callable(getattr(adapter, operation)))
            self.assertIsInstance(adapter, adapter_type)

    def test_conversation_uses_generic_history(self):
        conversation = Conversation(messages=[{"role": "user", "content": "generic"}])
        self.assertEqual(conversation.messages[0]["content"], "generic")

    def test_usage_is_normalized(self):
        adapter = OpenAICompatibleAdapter("qwen", load_config())
        usage = adapter.usage(Conversation(token_usage={
            "input_tokens": 12, "output_tokens": 3, "context_window_tokens": 100,
        }))
        self.assertEqual(usage, TokenUsage(12, 3, 100))

    def test_claude_capabilities_separate_telemetry_from_allowance_reporting(self):
        config = load_config("/definitely/missing/config.json")
        claude = adapter_for("claude", config)
        codex = adapter_for("codex", config)

        self.assertTrue(claude.capabilities.run)
        self.assertTrue(claude.capabilities.usage)
        self.assertFalse(claude.capabilities.allowance_reporting)
        self.assertFalse(claude.capabilities.organization_usage_reporting)
        self.assertTrue(codex.capabilities.allowance_reporting)

    @patch("pilferedparrot.budgets.read_claude_status")
    @patch("pilferedparrot.dispatch.capture_claude")
    def test_claude_runs_and_records_telemetry_when_allowance_is_unsupported(
        self, capture, status,
    ):
        config = load_config("/definitely/missing/config.json")
        status.return_value = ProviderBudget(
            "claude", True, auth_status=AUTH_SIGNED_IN,
            usage_status=USAGE_UNSUPPORTED,
            usage_note="Live allowance unavailable",
        )
        capture.return_value = RunResult(
            "Claude completed", 0, session_id="claude-session",
            input_tokens=17, output_tokens=9, live_context_window_tokens=256,
        )
        adapter = ClaudeAdapter("claude", config)
        conversation = Conversation(provider="claude")

        # Authentication status is CLI-owned and can be queried independently;
        # execution must not require an allowance probe.
        self.assertTrue(adapter.is_authenticated())
        result = adapter.run("hello", Path("/tmp"), conversation)

        self.assertEqual(result.text, "Claude completed")
        self.assertEqual(result.session_id, "claude-session")
        self.assertEqual(
            conversation.token_usage,
            {"input_tokens": 17, "output_tokens": 9, "context_window_tokens": 256},
        )
        status.assert_called_once()
        capture.assert_called_once()

    @patch("pilferedparrot.dispatch.provider_command", return_value="/usr/bin/gemini")
    def test_gemini_command_uses_headless_stdin_model_and_resume(self, _command):
        config = load_config("/definitely/missing/config.json")
        command = GeminiAdapter("gemini", config)._command(
            Conversation(provider_session_id="session-1"),
        )
        self.assertEqual(command[0], "/usr/bin/gemini")
        self.assertIn("stream-json", command)
        self.assertIn("auto_edit", command)
        self.assertEqual(command[-2:], ["--resume", "session-1"])
        self.assertNotIn("-p", command)

    def test_gemini_stream_is_normalized_without_repeating_output(self):
        config = load_config("/definitely/missing/config.json")
        conversation = Conversation(provider_session_id=None)
        events: list[ProgressEvent] = []
        wire = [
            {"type": "init", "session_id": "session-1", "model": "gemini-3-pro-preview"},
            {"type": "message", "role": "user", "content": "hello"},
            {"type": "message", "role": "assistant", "content": "Done", "delta": True},
            {"type": "tool_use", "tool_name": "read_file", "tool_id": "1"},
            {"type": "tool_result", "tool_id": "1", "status": "success"},
            {"type": "result", "status": "success", "stats": {
                "input_tokens": 120, "output_tokens": 8,
            }},
        ]

        def stream(_command, prompt, _cwd, *, stdout_line, **_kwargs):
            self.assertEqual(prompt, "hello")
            for item in wire:
                stdout_line(json.dumps(item) + "\n")
            return subprocess.CompletedProcess(["gemini"], 0, "", "")

        adapter = GeminiAdapter("gemini", config)
        with patch.object(adapter, "_command", return_value=["gemini"]), \
             patch("pilferedparrot.dispatch._stream_process", side_effect=stream):
            result = adapter.run(
                "hello", Path("/tmp"), conversation, threading.Event(), events.append,
            )
        self.assertEqual(result.text, "Done")
        self.assertEqual(result.session_id, "session-1")
        self.assertEqual((result.input_tokens, result.output_tokens), (120, 8))
        self.assertEqual([event.text for event in events].count("Done"), 1)
        self.assertEqual(
            {event.kind for event in events}, {"status", "output", "tool", "tool_result"},
        )


if __name__ == "__main__":
    unittest.main()
