import os
import subprocess
import shlex
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pilferedparrot.terminal import _focus_window, terminal_argv


@unittest.skipIf(sys.platform == "win32", "Linux terminal wrappers")
class TerminalWrapperTests(unittest.TestCase):
    def test_linux_wrapper_displays_data_before_running_once(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fakebin = root / "bin"
            fakebin.mkdir()
            fake_sudo = fakebin / "sudo"
            fake_sudo.write_text("#!/bin/sh\nprintf 'EXEC:%s\\n' \"$*\"\n", encoding="utf-8")
            fake_sudo.chmod(0o755)
            command = shlex.quote(str(fake_sudo)) + " printf '%s' 'café & $HOME'"
            with patch.dict(os.environ, {"PATH": f"{fakebin}:/usr/bin:/bin"}), \
                 patch("pilferedparrot.terminal.shutil.which", side_effect=lambda name: {
                     "bash": "/bin/bash", "xterm": "/usr/bin/xterm",
                 }.get(name)):
                argv = terminal_argv(command, root)
            script = argv[argv.index("-lc") + 1]
            result = subprocess.run(
                ["/bin/bash", "-lc", script, "pilferedparrot-terminal", str(root), command],
                cwd=root, input="", capture_output=True, text=True, timeout=10,
                env=os.environ | {"PATH": f"{fakebin}:/usr/bin:/bin"},
                check=True,
            )
            self.assertLess(result.stdout.index("Working directory:"), result.stdout.index("Command:"))
            self.assertLess(result.stdout.index(command), result.stdout.index("EXEC:"))
            self.assertEqual(result.stdout.count("EXEC:"), 1)

    def test_gnome_argv_requests_normal_active_window(self):
        with patch("pilferedparrot.terminal.shutil.which", side_effect=lambda name: {
            "bash": "/bin/bash", "gnome-terminal": "/usr/bin/gnome-terminal",
        }.get(name)):
            argv = terminal_argv("echo café & $x", Path("/tmp/a b"))
        self.assertEqual(argv[0:6], ["/usr/bin/gnome-terminal", "--window", "--active", "--geometry", "100x30", "--title"])
        self.assertIn("PPI Terminal", argv[6])

    def test_debian_gnome_wrapper_uses_xterm_compatible_arguments(self):
        with patch("pilferedparrot.terminal.shutil.which", side_effect=lambda name: {
            "bash": "/bin/bash", "x-terminal-emulator": "/usr/bin/x-terminal-emulator",
        }.get(name)), patch("pilferedparrot.terminal.os.path.realpath",
                            return_value="/usr/bin/gnome-terminal.wrapper"):
            argv = terminal_argv("echo hello", Path("/tmp/project"))
        self.assertIn("-T", argv)
        self.assertIn("-geometry", argv)
        self.assertIn("-e", argv)
        self.assertNotIn("--window", argv)

    def test_focus_restores_and_activates_only_the_exact_new_window(self):
        windows = (
            "0x01 0 unrelated.host host Other PPI Terminal unique\n"
            "0x02 0 gnome-terminal.Gnome-terminal host PPI Terminal unique\n"
        )
        with patch("pilferedparrot.terminal.shutil.which", return_value="/usr/bin/wmctrl"), \
                patch("pilferedparrot.terminal.subprocess.run",
                      return_value=subprocess.CompletedProcess([], 0, windows)) as run:
            _focus_window("PPI Terminal unique")
        self.assertEqual([call.args[0] for call in run.call_args_list], [
            ["/usr/bin/wmctrl", "-l", "-x"],
            ["/usr/bin/wmctrl", "-ir", "0x02", "-b", "remove,shaded"],
            ["/usr/bin/wmctrl", "-ia", "0x02"],
        ])

    def test_focus_failure_is_nonfatal(self):
        with patch("pilferedparrot.terminal.shutil.which", return_value="/usr/bin/wmctrl"), \
                patch("pilferedparrot.terminal.subprocess.run", side_effect=OSError):
            _focus_window("PPI Terminal unique")

    def test_working_folder_and_literal_command_are_preserved(self):
        with tempfile.TemporaryDirectory(prefix="ppi space & ") as directory:
            root = Path(directory)
            marker = root / "executions"
            command = "printf '%s\\n' '100% café & $(touch never-created)' >> executions; pwd"
            with patch("pilferedparrot.terminal.shutil.which", side_effect=lambda name: {
                "bash": "/bin/bash", "xterm": "/usr/bin/xterm",
            }.get(name)):
                argv = terminal_argv(command, root)
            child = argv[argv.index("-e") + 1:]
            result = subprocess.run(child, input="", capture_output=True, text=True,
                                    timeout=10, check=True)
            self.assertIn(command, result.stdout)
            self.assertIn(str(root), result.stdout)
            self.assertEqual(marker.read_text(), "100% café & $(touch never-created)\n")
            self.assertFalse((root / "never-created").exists())



if __name__ == "__main__":
    unittest.main()
