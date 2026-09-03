"""Focused tests for native integration without opening real GUI resources."""

import subprocess
import unittest
from unittest.mock import MagicMock, call, patch

from pilferedparrot.web_native import (
    NativeIntegration, browser_url, chromium_browser, notify_window_closed, open_browser,
)


class BrowserIntegrationTests(unittest.TestCase):
    @patch("pilferedparrot.web_native.shutil.which")
    def test_chromium_discovery_preserves_launcher_preference_order(self, which):
        which.side_effect = lambda candidate: (
            "/usr/bin/chromium" if candidate == "chromium" else None
        )

        self.assertEqual(chromium_browser(), "/usr/bin/chromium")
        self.assertEqual(which.call_args_list, [
            call("google-chrome-stable"), call("google-chrome"), call("chromium"),
        ])

    @patch("pilferedparrot.web_native.webbrowser.open", return_value=True)
    def test_default_browser_open_is_a_mockable_boundary(self, browser_open):
        self.assertTrue(open_browser("http://127.0.0.1:8765/"))
        browser_open.assert_called_once_with("http://127.0.0.1:8765/")

    def test_browser_url_keeps_close_token_in_fragment(self):
        self.assertEqual(
            browser_url(
                "http://127.0.0.1:8765", "secret", api_generation=10,
                asset_version="assets", runtime_version="runtime",
            ),
            "http://127.0.0.1:8765/?generation=10&assets=assets"
            "&runtime=runtime#close_token=secret",
        )

    def test_close_notification_uses_injected_http_dependency(self):
        response = MagicMock(status=202)
        response.__enter__.return_value = response
        opener = MagicMock(return_value=response)

        self.assertTrue(notify_window_closed(
            "http://127.0.0.1:8765/#close_token=secret", opener=opener,
        ))

        request = opener.call_args.args[0]
        self.assertEqual(request.full_url, "http://127.0.0.1:8765/api/shutdown")
        self.assertEqual(request.data, b"{}")
        self.assertEqual(request.get_header("X-pilferedparrot-csrf"), "secret")

    def test_close_notification_refuses_non_loopback_destination(self):
        opener = MagicMock()
        self.assertFalse(notify_window_closed(
            "https://example.com/#close_token=secret", opener=opener,
        ))
        opener.assert_not_called()


class NativeWindowManagerTests(unittest.TestCase):
    @patch("pilferedparrot.web_native.threading.Thread")
    @patch("pilferedparrot.web_native.subprocess.Popen")
    @patch("pilferedparrot.web_native.tempfile.mkdtemp", return_value="/tmp/chat-profile")
    def test_chat_window_launch_isolated_behind_facade(self, _mkdtemp, popen, thread):
        manager = NativeIntegration()

        result = manager.open_chat_window(
            "http://127.0.0.1:8765/chat",
            {"width": 900, "height": 600, "left": 10, "top": 20},
            browser="/usr/bin/chromium",
        )

        self.assertEqual(result, {"ok": True, "existing": False})
        command = popen.call_args.args[0]
        self.assertEqual(command[0], "/usr/bin/chromium")
        self.assertIn("--user-data-dir=/tmp/chat-profile", command)
        self.assertIn("--window-size=900,600", command)
        self.assertIn("--window-position=10,20", command)
        self.assertIn("--app=http://127.0.0.1:8765/chat", command)
        thread.return_value.start.assert_called_once_with()

    @patch("pilferedparrot.web_native.subprocess.run")
    @patch("pilferedparrot.web_native.shutil.which", return_value="/usr/bin/wmctrl")
    def test_existing_chat_window_is_focused_without_new_process(self, _which, run):
        manager = NativeIntegration()
        process = MagicMock()
        process.poll.return_value = None
        manager.chat_window_process = process

        result = manager.open_chat_window(
            "http://127.0.0.1:8765/chat",
            {"width": 900, "height": 600, "left": 0, "top": 0},
        )

        self.assertEqual(result, {"ok": True, "existing": True})
        run.assert_called_once_with(
            ["/usr/bin/wmctrl", "-x", "-a", "pilferedparrot-chat"], check=False,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )


if __name__ == "__main__":
    unittest.main()
