"""Windows startup, storage, and tool boundaries without provider credentials."""

import base64
import io
import json
import os
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from pilferedparrot import windows
from pilferedparrot.config import DEFAULTS
from pilferedparrot.qwen import _chat_completion
from pilferedparrot.qwen_tools import QwenToolbox
from pilferedparrot.web import _terminal_argv


class WindowsEntrypointTests(unittest.TestCase):
    def test_first_run_creates_user_state_and_preserves_existing_configuration(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.dict(os.environ, {"LOCALAPPDATA": str(root / "local")}), \
                    patch.object(windows.Path, "home", return_value=root):
                args = windows.prepare_arguments(["gui", "--no-browser"])
                config_path = root / "local/PilferedParrot/config.json"
                config = json.loads(config_path.read_text(encoding="utf-8"))
                self.assertEqual(config["web"]["chat_store"], str(config_path.parent / "chats.json"))
                self.assertEqual(config["ledger"], str(config_path.parent / "runs.jsonl"))
                self.assertEqual(args, ["--cwd", str(root / "PilferedParrot Projects"),
                                       "--config", str(config_path), "gui", "--no-browser"])
                self.assertTrue((root / "PilferedParrot Projects").is_dir())
                config_path.write_text('{"custom": true}', encoding="utf-8")
                windows.prepare_arguments([])
                self.assertEqual(json.loads(config_path.read_text(encoding="utf-8")), {"custom": True})

    def test_explicit_paths_do_not_create_default_directories(self):
        for args in (["--config", "custom.json", "--cwd", "project", "gui"],
                     ["--config=custom.json", "--cwd=project", "gui"]):
            with patch.object(windows, "state_directory") as state, \
                    patch.object(windows.Path, "home") as home:
                self.assertEqual(windows.prepare_arguments(args), args)
                state.assert_not_called()
                home.assert_not_called()

    def test_help_does_not_write_state_or_require_a_browser(self):
        with patch("pilferedparrot.cli.main", return_value=0) as cli, \
                patch.object(windows, "prepare_arguments") as prepare:
            self.assertEqual(windows.main(["--help"]), 0)
            cli.assert_called_once_with(["--help"])
            prepare.assert_not_called()

    def test_headless_start_does_not_require_a_browser(self):
        args = ["--config", "custom.json", "--cwd", ".", "gui", "--no-browser"]
        with patch("pilferedparrot.cli.main", return_value=0) as cli, \
                patch("pilferedparrot.web_native.chromium_browser", return_value=None), \
                patch.object(windows.webbrowser, "register"):
            self.assertEqual(windows.main(args), 0)
            cli.assert_called_once_with(args)

    def test_windows_terminal_preserves_unicode_and_metacharacters(self):
        command = 'Write-Output "parrot & café; $value"'
        with patch("pilferedparrot.web.sys.platform", "win32"), \
                patch("pilferedparrot.web.shutil.which", return_value="powershell.exe"):
            argv = _terminal_argv(command, Path("project"))
        self.assertEqual(argv[:-1], [
            "powershell.exe", "-NoLogo", "-NoProfile", "-NoExit",
            "-WindowStyle", "Normal", "-EncodedCommand",
        ])
        wrapper = base64.b64decode(argv[-1]).decode("utf-16-le")
        self.assertIn("Set-Location -LiteralPath $cwd -ErrorAction Stop", wrapper)
        self.assertIn("Invoke-Expression -Command $command", wrapper)
        self.assertNotIn(command, wrapper)

    def test_windows_compatible_tools_exclude_shell_and_enforce_read_only(self):
        config = deepcopy(DEFAULTS)
        for read_only, expected in (
            (False, {"read_file", "write_file", "edit_file", "diff"}),
            (True, {"read_file", "diff"}),
        ):
            config["qwen"]["read_only"] = read_only
            response = io.BytesIO(json.dumps({"choices": [{"message": {"content": "ok"}}]}).encode())
            with patch("pilferedparrot.qwen.sys.platform", "win32"), \
                    patch("pilferedparrot.qwen.open_compatible_url", return_value=response) as opened:
                _chat_completion([], config)
            payload = json.loads(opened.call_args.args[2].data)
            self.assertEqual({tool["function"]["name"] for tool in payload["tools"]}, expected)

    def test_windows_shell_cannot_be_invoked_even_if_model_requests_it(self):
        with tempfile.TemporaryDirectory() as directory:
            toolbox = QwenToolbox(Path(directory), DEFAULTS["qwen"])
            with patch("pilferedparrot.qwen_tools.sys.platform", "win32"), \
                    patch("pilferedparrot.qwen_tools.subprocess.run") as run:
                with self.assertRaisesRegex(RuntimeError, "require Linux"):
                    toolbox._shell("echo unsafe")
                run.assert_not_called()

    def test_file_tool_diff_works_without_a_git_installation(self):
        with tempfile.TemporaryDirectory() as directory:
            toolbox = QwenToolbox(Path(directory), DEFAULTS["qwen"])
            with patch("pilferedparrot.qwen_tools.subprocess.run", side_effect=FileNotFoundError):
                self.assertIn("Git is not installed", toolbox._diff())
                toolbox._write_file("parrot.txt", "hello parrot\n")
                self.assertIn("+hello parrot", toolbox._diff())


if __name__ == "__main__":
    unittest.main()
