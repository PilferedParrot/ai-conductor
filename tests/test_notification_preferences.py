"""Focused coverage for durable desktop-notification preferences."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from pilferedparrot.web_native import NativeIntegration
from pilferedparrot.web_persistence import PersistentChatStore


class NotificationPreferencePersistenceTests(unittest.TestCase):
    @staticmethod
    def _usage(*_args, **_kwargs):
        return {"percent": 0, "used_tokens": 0, "limit_tokens": 1}

    def test_old_state_defaults_to_unasked_and_invalid_values_are_migrated(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "chats.json"
            path.write_text(json.dumps({
                "version": 8, "chats": [], "preferences": {
                    "chat_model": "gpt-5.6-luna", "notification_permission": "invalid",
                },
            }), encoding="utf-8")

            store = PersistentChatStore(path, context_usage=self._usage)

            self.assertEqual(store.preferences_public()["notification_permission"], "unasked")
            self.assertEqual(store.preferences_public()["chat_model"], "gpt-5.6-luna")

    def test_each_decision_persists_and_reset_is_durable(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "chats.json"
            store = PersistentChatStore(path, context_usage=self._usage)
            for decision in ("granted", "denied", "dismissed", "unavailable", "unasked"):
                self.assertEqual(
                    store.set_notification_permission(decision)["notification_permission"], decision,
                )
                restarted = PersistentChatStore(path, context_usage=self._usage)
                self.assertEqual(
                    restarted.preferences_public()["notification_permission"], decision,
                )

            with self.assertRaisesRegex(ValueError, "notification permission"):
                store.set_notification_permission("ask-again")


class ChatWindowFallbackTests(unittest.TestCase):
    @patch("pilferedparrot.web_native.threading.Thread")
    @patch("pilferedparrot.web_native.subprocess.Popen")
    @patch("pilferedparrot.web_native.tempfile.mkdtemp", return_value="/tmp/chat-profile")
    def test_maximized_chat_keeps_sized_window_arguments_as_backend_fallback(
        self, _mkdtemp, popen, thread,
    ):
        manager = NativeIntegration(MagicMock())
        manager.open_chat_window(
            "http://127.0.0.1:8765/chat", provider="codex", model="gpt-test",
            payload={"width": 960, "height": 540, "left": 20, "top": 30},
            issue_capability=MagicMock(return_value="capability"), browser="/usr/bin/chromium",
        )

        command = popen.call_args.args[0]
        self.assertIn("--start-maximized", command)
        # Chromium/window managers that ignore maximization still receive a useful window.
        self.assertIn("--window-size=960,540", command)
        self.assertIn("--window-position=20,30", command)
        thread.return_value.start.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
