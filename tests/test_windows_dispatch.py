"""Exercise portable subprocess I/O and cancellation with a real local child."""

import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from pilferedparrot.dispatch import RunCancelled, _capture_process, _stream_process


class PortableDispatchTests(unittest.TestCase):
    def test_capture_preserves_unicode_stdin_and_literal_arguments(self):
        with tempfile.TemporaryDirectory(prefix="parrot project ") as directory:
            prompt = "café 🦜 & $(literal) %PATH%"
            result = _capture_process(
                [sys.executable, "-X", "utf8", "-c",
                 "import sys; print(sys.stdin.read()); print(sys.argv[1])", prompt],
                prompt, Path(directory), cancel_event=None, timeout_seconds=10,
            )
            self.assertEqual(result.returncode, 0)
            self.assertEqual(result.stdout.splitlines(), [prompt, prompt])

    def test_stream_cancellation_terminates_the_actual_child(self):
        with tempfile.TemporaryDirectory() as directory:
            cancelled = threading.Event()
            with patch("pilferedparrot.dispatch.subprocess.Popen", wraps=subprocess.Popen) as spawn:
                # Retain the real process so a broken cancellation cannot leak it.
                children = []
                real_popen = spawn._mock_wraps
                def start(*args, **kwargs):
                    child = real_popen(*args, **kwargs)
                    children.append(child)
                    return child
                spawn.side_effect = start
                try:
                    with self.assertRaises(RunCancelled):
                        _stream_process(
                            [sys.executable, "-u", "-c",
                             "import time; print('ready'); time.sleep(30)"],
                            "", Path(directory), cancel_event=cancelled,
                            timeout_seconds=10, stdout_line=lambda _line: cancelled.set(),
                        )
                    self.assertEqual(len(children), 1)
                    self.assertIsNotNone(children[0].poll())
                finally:
                    for child in children:
                        if child.poll() is None:
                            child.kill()
                            child.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()
