"""Windows native desktop integration tests that run on any host OS."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from pilferedparrot.web_native import (
    NativeIntegration,
    WindowsAppBrowser,
    chromium_browser,
    open_app_browser,
    persistent_browser_profile,
    select_project_directory,
)


class WindowsDesktopTests(unittest.TestCase):
    @patch("pilferedparrot.web_native.WINDOWS", True)
    @patch("pilferedparrot.web_native.shutil.which", return_value=None)
    def test_discovery_checks_windows_installation_paths(self, _which):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            chrome = root / "Google/Chrome/Application/chrome.exe"
            chrome.parent.mkdir(parents=True)
            chrome.touch()
            with patch.dict(os.environ, {
                "ProgramFiles": directory,
                "ProgramFiles(x86)": "",
                "LOCALAPPDATA": "",
            }, clear=False):
                self.assertEqual(chromium_browser(), str(chrome))

    @patch("pilferedparrot.web_native.WINDOWS", True)
    @patch("pilferedparrot.web_native.shutil.which", return_value=None)
    def test_discovery_prefers_user_chrome_over_system_edge(self, _which):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            edge = root / "Microsoft/Edge/Application/msedge.exe"
            edge.parent.mkdir(parents=True)
            edge.touch()
            with patch.dict(os.environ, {
                "ProgramFiles": directory,
                "ProgramFiles(x86)": "",
                "LOCALAPPDATA": directory + "/user",
            }, clear=False):
                user_chrome = Path(directory) / "user/Google/Chrome/Application/chrome.exe"
                user_chrome.parent.mkdir(parents=True)
                user_chrome.touch()
                self.assertEqual(chromium_browser(), str(user_chrome))

    @patch("pilferedparrot.web_native.WINDOWS", True)
    @patch("pilferedparrot.web_native.shutil.which", return_value="powershell.exe")
    @patch("pilferedparrot.web_native.subprocess.Popen")
    def test_folder_chooser_passes_initial_path_via_environment(self, popen, _which):
        process = MagicMock(returncode=0)
        process.communicate.return_value = ("C:\\chosen\\folder\r\n", "")
        popen.return_value = process
        normalize = MagicMock(return_value=Path("C:/chosen/folder"))

        result = select_project_directory("C:/start folder", normalize=normalize)

        self.assertEqual(result, Path("C:/chosen/folder"))
        command = popen.call_args.args[0]
        self.assertEqual(command[0:5], [
            "powershell.exe", "-NoProfile", "-NonInteractive", "-STA", "-Command",
        ])
        self.assertNotIn("C:/start folder", command[-1])
        self.assertEqual(
            popen.call_args.kwargs["env"]["PILFEREDPARROT_CHOOSER_INITIAL"],
            str(Path.home().resolve()),
        )

    @patch("pilferedparrot.web_native.WINDOWS", True)
    def test_windows_profile_uses_local_app_data(self):
        with patch.dict(os.environ, {"LOCALAPPDATA": r"C:\Users\me\AppData\Local"}):
            self.assertEqual(
                persistent_browser_profile(),
                Path(r"C:\Users\me\AppData\Local/PilferedParrot/chrome-profile"),
            )

    @patch("pilferedparrot.web_native.WINDOWS", True)
    @patch("pilferedparrot.web_native.subprocess.Popen")
    def test_app_browser_uses_persistent_profile_and_app_flags(self, popen):
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"LOCALAPPDATA": directory}
        ):
            self.assertTrue(open_app_browser("http://127.0.0.1:8000/#capability=x", browser="chrome.exe"))
            command = popen.call_args.args[0]
            self.assertEqual(command[0], "chrome.exe")
            self.assertIn("--no-first-run", command)
            self.assertIn("--disable-background-mode", command)
            self.assertEqual(command[-1], "--app=http://127.0.0.1:8000/#capability=x")

    @patch("pilferedparrot.web_native.WINDOWS", True)
    @patch("pilferedparrot.web_native.subprocess.Popen")
    def test_webbrowser_controller_delegates_to_app_launcher(self, popen):
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"LOCALAPPDATA": directory}
        ):
            self.assertTrue(WindowsAppBrowser("chrome.exe").open("http://localhost:1/"))
            self.assertEqual(popen.call_args.args[0][0], "chrome.exe")

    @patch("pilferedparrot.web_native.WINDOWS", True)
    @patch("pilferedparrot.web_native.subprocess.Popen")
    def test_edge_app_browser_uses_separate_profile(self, popen):
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"LOCALAPPDATA": directory}
        ):
            self.assertTrue(open_app_browser("http://localhost:1/", browser="msedge.exe"))
            command = popen.call_args.args[0]
            self.assertEqual(command[1], f"--user-data-dir={Path(directory) / 'PilferedParrot/edge-profile'}")
            self.assertNotIn("chrome-profile", command[1])

    def test_theme_gallery_rejects_edge(self):
        with self.assertRaisesRegex(RuntimeError, "Chrome or Chromium"):
            NativeIntegration(lambda _token: None).open_theme_gallery(browser="msedge.exe")


if __name__ == "__main__":
    unittest.main()
