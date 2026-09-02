import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pilferedparrot.config import load_config
from pilferedparrot.web import PilferedParrotApp, _fenced_code_blocks, _terminal_argv


def _app(directory):
    config = load_config()
    config["web"]["chat_store"] = str(Path(directory) / "chats.json")
    config["ledger"] = str(Path(directory) / "runs.jsonl")
    return PilferedParrotApp(config, Path(directory))


class TerminalCommandTests(unittest.TestCase):
    def test_fenced_blocks_match_browser_command_indexing(self):
        self.assertEqual(
            _fenced_code_blocks("Text\n```bash\nsudo apt update\n```\n```\nnot a language\n```"),
            ["sudo apt update", "not a language"],
        )

    def test_launch_uses_exact_stored_assistant_command_and_chat_cwd(self):
        with tempfile.TemporaryDirectory() as directory:
            app = _app(directory)
            chat = app.create_chat({"provider": "codex", "cwd": directory})
            with app.store.lock:
                stored = app.store.get(chat["id"])
                stored["messages"].append({
                    "id": "assistant-1",
                    "role": "assistant",
                    "content": "Run this:\n\n```bash\nsudo apt update\n```",
                })
                app.store.save()
            with patch("pilferedparrot.web._terminal_argv", return_value=["terminal"]) as argv, \
                 patch("pilferedparrot.web.subprocess.Popen") as popen:
                app.launch_terminal_command(chat["id"], {
                    "message_id": "assistant-1", "block_index": 0,
                })
            argv.assert_called_once_with("sudo apt update", Path(directory))
            self.assertEqual(popen.call_args.kwargs["cwd"], Path(directory))
            self.assertTrue(popen.call_args.kwargs["start_new_session"])

    def test_launch_rejects_multiline_and_non_assistant_blocks(self):
        with tempfile.TemporaryDirectory() as directory:
            app = _app(directory)
            chat = app.create_chat({"provider": "codex", "cwd": directory})
            with app.store.lock:
                stored = app.store.get(chat["id"])
                stored["messages"].extend([
                    {"id": "assistant", "role": "assistant", "content": "```bash\none\ntwo\n```"},
                    {"id": "python", "role": "assistant", "content": "```python\nprint('no')\n```"},
                    {"id": "user", "role": "user", "content": "```bash\nsudo true\n```"},
                ])
                app.store.save()
            with self.assertRaisesRegex(ValueError, "single"):
                app.launch_terminal_command(chat["id"], {
                    "message_id": "assistant", "block_index": 0,
                })
            with self.assertRaisesRegex(ValueError, "assistant"):
                app.launch_terminal_command(chat["id"], {
                    "message_id": "user", "block_index": 0,
                })
            with self.assertRaisesRegex(ValueError, "shell"):
                app.launch_terminal_command(chat["id"], {
                    "message_id": "python", "block_index": 0,
                })

    @patch("pilferedparrot.web.shutil.which")
    def test_terminal_wrapper_passes_command_as_an_argument(self, which):
        which.side_effect = lambda name: {
            "bash": "/bin/bash", "gnome-terminal": "/usr/bin/gnome-terminal",
        }.get(name)
        command = "sudo sh -c 'printf hello'"
        argv = _terminal_argv(command, Path("/tmp/project"))
        self.assertEqual(argv[-1], command)
        self.assertNotIn(command, argv[4])
        self.assertEqual(argv[:2], ["/usr/bin/gnome-terminal", "--"])


if __name__ == "__main__":
    unittest.main()
