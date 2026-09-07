import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from copy import deepcopy

from pilferedparrot.adapters import GeminiAdapter
from pilferedparrot.antigravity import AntigravityAdapter
from pilferedparrot.config import DEFAULTS
from pilferedparrot.dispatch import (
    RunCancelled, _stream_process, capture_claude, capture_codex,
)
from pilferedparrot.model import Conversation


class ProviderJobLifetimeTests(unittest.TestCase):
    def test_stream_process_completes_after_simulated_elapsed_1800_seconds(self):
        output = []
        clock = iter((0, 1801))
        with patch("pilferedparrot.dispatch.time.monotonic",
                   side_effect=lambda: next(clock, 1801)):
            completed = _stream_process(
                [sys.executable, "-c",
                 "import sys,time; time.sleep(.03); print('done'); print('diag', file=sys.stderr)"],
                "", Path.cwd(),
                cancel_event=None, stdout_line=output.append,
            )
        self.assertEqual(completed.returncode, 0)
        self.assertEqual("".join(output), "done\n")
        self.assertEqual(completed.stderr, "diag\n")

    def test_silent_provider_waits_until_explicit_cancellation(self):
        cancel = threading.Event()
        timer = threading.Timer(0.15, cancel.set)
        timer.start()
        started = time.monotonic()
        try:
            with self.assertRaises(RunCancelled):
                _stream_process(
                    [sys.executable, "-c", "import time; time.sleep(30)"], "",
                    Path.cwd(), cancel_event=cancel, stdout_line=lambda _line: None,
                )
        finally:
            timer.cancel()
        self.assertLess(time.monotonic() - started, 3)

    def test_legacy_request_timeout_is_not_passed_to_provider_jobs(self):
        config = deepcopy(DEFAULTS)
        config["codex"]["request_timeout_seconds"] = 0
        config["claude"]["request_timeout_seconds"] = 0
        config["gemini"]["request_timeout_seconds"] = 0
        config["antigravity"]["request_timeout_seconds"] = 0

        def cli(*lines):
            source = "import sys,time\n" \
                     "sys.stdin.read()\n" \
                     "time.sleep(.03)\n" \
                     + "\n".join(f"print({line!r}, flush=True)" for line in lines)
            return [sys.executable, "-c", source]

        codex = cli(
            '{"type":"thread.started","thread_id":"thread-123"}',
            '{"type":"item.completed","item":{"type":"agent_message","text":"codex ok"}}',
        )
        claude = cli(
            '{"type":"result","session_id":"claude-123","result":"claude ok"}',
        )
        gemini = cli(
            '{"type":"init","session_id":"gemini-123"}',
            '{"type":"message","role":"assistant","content":"gemini ok"}',
            '{"type":"result","status":"success","stats":{}}',
        )
        antigravity = cli(
            '{"event":"init","conversation_id":"agy-123"}',
            '{"event":"result","result":{"status":"SUCCESS","conversation_id":"agy-123","response":"agy ok"}}',
        )
        with patch("pilferedparrot.dispatch._codex_command", return_value=codex), \
             patch("pilferedparrot.dispatch._claude_command", return_value=claude), \
             patch.object(GeminiAdapter, "_command", return_value=gemini), \
             patch.object(AntigravityAdapter, "_command", return_value=antigravity):
            codex_result = capture_codex("p", Path.cwd(), Conversation(), config)
            claude_result = capture_claude("p", Path.cwd(), Conversation(), config)
            gemini_result = GeminiAdapter("gemini", config).run("p", Path.cwd(), Conversation())
            agy_result = AntigravityAdapter("antigravity", config).run(
                "p", Path.cwd(), Conversation(),
            )
        self.assertEqual((codex_result.exit_code, codex_result.text), (0, "codex ok"))
        self.assertEqual((claude_result.exit_code, claude_result.text), (0, "claude ok"))
        self.assertEqual((gemini_result.exit_code, gemini_result.text), (0, "gemini ok"))
        self.assertEqual((agy_result.exit_code, agy_result.text), (0, "agy ok"))


if __name__ == "__main__":
    unittest.main()
