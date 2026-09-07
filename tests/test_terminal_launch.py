import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pilferedparrot.config import load_config
from pilferedparrot.web import PilferedParrotApp, _fenced_code_blocks


def _app(directory):
    config = load_config()
    config["web"]["chat_store"] = str(Path(directory) / "chats.json")
    config["ledger"] = str(Path(directory) / "runs.jsonl")
    return PilferedParrotApp(config, Path(directory))


class TerminalCommandTests(unittest.TestCase):
    @unittest.skipIf(sys.platform == "win32", "Linux desktop installer")
    def test_desktop_installer_quotes_repository_paths_with_spaces(self):
        root = Path(__file__).parents[1]
        with tempfile.TemporaryDirectory() as directory:
            temporary = Path(directory)
            environment = {
                **os.environ,
                "HOME": str(temporary / "home"),
                "XDG_DATA_HOME": str(temporary / "data"),
                "XDG_STATE_HOME": str(temporary / "state"),
            }
            (temporary / "home").mkdir()
            subprocess.run(
                [str(root / "bin" / "install-pilferedparrot-desktop")],
                check=True, env=environment, capture_output=True, text=True,
            )
            desktop = (temporary / "data" / "applications" /
                       "pilferedparrot.desktop").read_text()
        self.assertIn(f'Exec="{root}/bin/pilferedparrot-gui"', desktop)
        self.assertIn(f"TryExec={root}/bin/pilferedparrot-gui", desktop)

    def test_fenced_blocks_match_browser_command_indexing(self):
        self.assertEqual(
            _fenced_code_blocks("Text\n```bash\nsudo apt update\n```\n```\nnot a language\n```"),
            ["sudo apt update", "not a language"],
        )

    def test_only_complete_top_level_line_fences_can_be_commands(self):
        content = (
            "inline ```bash\necho inline```\n"
            "> ```bash\n> echo quoted\n> ```\n"
            "```bash\necho top-level\n```\n"
            "```sh\necho unmatched"
        )
        self.assertEqual(_fenced_code_blocks(content), ["echo top-level"])

    def test_launch_uses_exact_stored_assistant_command_and_chat_cwd(self):
        with tempfile.TemporaryDirectory() as directory:
            app = _app(directory)
            chat = app.create_chat({"provider": "codex", "cwd": directory})
            with app.store.lock:
                stored = app.store.get(chat["id"])
                stored["messages"].append({
                    "id": "assistant-1",
                    "role": "assistant",
                    "content": "Run this:\n\n```shell\nsudo apt update\n```",
                })
                app.store.save()
            with patch("pilferedparrot.web.launch_terminal") as launch:
                app.launch_terminal_command(chat["id"], {
                    "message_id": "assistant-1", "block_index": 0,
                })
            launch.assert_called_once_with("sudo apt update", Path(directory).resolve())

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



if __name__ == "__main__":
    unittest.main()
