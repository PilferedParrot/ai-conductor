"""Windows-only checks for the interactive PowerShell terminal launcher."""

import base64
import json
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from pilferedparrot.terminal import (
    _windows_console_handle,
    launch_terminal,
    terminal_argv,
)


class WindowsTerminalTests(unittest.TestCase):
    @unittest.skipUnless(sys.platform == "win32", "requires Windows PowerShell")
    def test_encoded_wrapper_handles_metacharacters_and_executes_once(self):
        shell = shutil.which("powershell.exe") or shutil.which("pwsh.exe")
        if shell is None:
            self.skipTest("PowerShell is not installed")

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "folder & ; $[café]"
            root.mkdir()
            command = '$value = [Console]::In.ReadLine(); Write-Output ("EXEC:" + $value)'
            argv = terminal_argv(command, root)
            wrapper = base64.b64decode(argv[-1]).decode("utf-16-le")

            self.assertIn("-NoExit", argv)
            self.assertNotIn(str(root), wrapper)
            self.assertNotIn(command, wrapper)
            result = subprocess.run(
                [shell, "-NoLogo", "-NoProfile", "-NonInteractive", "-EncodedCommand", argv[-1]],
                input="input & $value\n", capture_output=True, text=True,
                encoding="utf-8", check=True, timeout=20,
            )

        lines = result.stdout.splitlines()
        self.assertLess(lines.index("Working directory:"), lines.index(str(root)))
        self.assertLess(lines.index("Command:"), lines.index(command))
        self.assertLess(lines.index(command), lines.index("EXEC:input & $value"))
        self.assertEqual(lines.count("EXEC:input & $value"), 1)

    @unittest.skipUnless(sys.platform == "win32", "requires a native Windows console")
    def test_new_console_has_interactive_input_and_output(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            report = root / "console.json"
            command = (
                "@{inputRedirected=[Console]::IsInputRedirected; "
                "outputRedirected=[Console]::IsOutputRedirected; "
                "cwd=(Get-Location).Path} | ConvertTo-Json | "
                "Set-Content -LiteralPath 'console.json' -Encoding UTF8; exit"
            )
            real_popen = subprocess.Popen
            children = []

            def spawn(*args, **kwargs):
                child = real_popen(*args, **kwargs)
                children.append(child)
                return child

            with patch("pilferedparrot.terminal.subprocess.Popen", side_effect=spawn):
                launch_terminal(command, root)
            try:
                self.assertEqual(children[0].wait(timeout=15), 0)
            finally:
                if children[0].poll() is None:
                    children[0].kill()
                    children[0].wait(timeout=5)
            self.assertTrue(report.exists(), "PowerShell did not execute in the new console")
            data = json.loads(report.read_text(encoding="utf-8-sig"))
            self.assertFalse(data["inputRedirected"])
            self.assertFalse(data["outputRedirected"])
            self.assertEqual(Path(data["cwd"]).resolve(), root.resolve())

    def test_windows_spawn_uses_a_new_console_without_stdio_redirection(self):
        with patch("pilferedparrot.terminal.sys.platform", "win32"), \
                patch("pilferedparrot.terminal.subprocess.CREATE_NEW_CONSOLE", 0x10, create=True), \
                patch("pilferedparrot.terminal.terminal_argv", return_value=["powershell.exe"]) as argv_builder, \
                patch("pilferedparrot.terminal.subprocess.Popen", return_value=Mock()) as popen, \
                patch("pilferedparrot.terminal._focus_windows_console") as focus:
            launch_terminal("Write-Output hi", Path("project"))

        kwargs = popen.call_args.kwargs
        self.assertEqual(kwargs["close_fds"], True)
        self.assertEqual(kwargs["creationflags"], 0x10)
        self.assertNotIn("stdin", kwargs)
        self.assertNotIn("stdout", kwargs)
        self.assertNotIn("stderr", kwargs)
        focus.assert_called_once()
        title = focus.call_args.args[0]
        self.assertTrue(title.startswith("PPI Terminal "))
        self.assertEqual(
            argv_builder.call_args.kwargs["_windows_title"], title,
        )

    def test_focus_selects_only_the_exact_console_title(self):
        class FakeUser32:
            windows = {
                1: ("ConsoleWindowClass", "PPI Terminal wrong"),
                2: ("NotAConsole", "PPI Terminal exact"),
                3: ("ConsoleWindowClass", "PPI Terminal exact"),
                4: ("CASCADIA_HOSTING_WINDOW_CLASS", "PPI Terminal modern"),
            }

            def IsWindowVisible(self, hwnd):
                return True

            def GetClassNameW(self, hwnd, buffer, _size):
                buffer.value = self.windows[hwnd][0]

            def GetWindowTextLengthW(self, hwnd):
                return len(self.windows[hwnd][1])

            def GetWindowTextW(self, hwnd, buffer, _size):
                buffer.value = self.windows[hwnd][1]

            def EnumWindows(self, callback, _lparam):
                for hwnd in self.windows:
                    if not callback(hwnd, 0):
                        break

        def callback_type(*_signature):
            return lambda function: function

        user32 = FakeUser32()
        self.assertIsNone(
            _windows_console_handle("PPI Terminal absent", user32, callback_type)
        )
        self.assertEqual(
            _windows_console_handle("PPI Terminal exact", user32, callback_type), 3
        )
        self.assertEqual(
            _windows_console_handle("PPI Terminal modern", user32, callback_type), 4
        )


if __name__ == "__main__":
    unittest.main()
