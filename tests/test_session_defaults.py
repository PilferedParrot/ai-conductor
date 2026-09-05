import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pilferedparrot.web_persistence import PersistentChatStore


class SessionDefaultTests(unittest.TestCase):
    @staticmethod
    def _usage(*args, **kwargs):
        return {"percent": 0, "used_tokens": 0, "limit_tokens": 100}

    def test_latest_defaults_are_scoped_to_provider_window(self):
        with tempfile.TemporaryDirectory() as directory:
            store = PersistentChatStore(
                Path(directory) / "chats.json", context_usage=self._usage,
            )
            first = store.create(Path(directory), "codex", "gpt-5.6-luna", window_id="main",
                                 reasoning_effort="high")
            second = store.create(Path(directory), "ollama", "qwen", window_id="main")
            third = store.create(Path(directory), "codex", "gpt-5.6-terra", window_id="provider-codex",
                                 reasoning_effort="low")
            # Selection order, not activity timestamps, controls inheritance.
            store.mark_used(store.get(first["id"]))
            self.assertEqual(
                store.latest_work_defaults("codex", "main"),
                ("gpt-5.6-luna", "high"),
            )
            self.assertEqual(
                store.latest_work_defaults("codex", "provider-codex"),
                ("gpt-5.6-terra", "low"),
            )
            self.assertIsNone(store.latest_work_defaults("missing", "main"))

    def test_default_reasoning_choice_is_preserved_as_none(self):
        with tempfile.TemporaryDirectory() as directory:
            store = PersistentChatStore(
                Path(directory) / "chats.json", context_usage=self._usage,
            )
            store.create(Path(directory), "codex", "gpt-5.6-luna", reasoning_effort=None)
            self.assertEqual(store.latest_work_defaults("codex"), ("gpt-5.6-luna", None))

    def test_latest_defaults_choose_the_newest_same_second_session_after_reload(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "chats.json"
            store = PersistentChatStore(path, context_usage=self._usage)
            with patch("pilferedparrot.web_persistence.time.time", return_value=1234):
                first = store.create(
                    Path(directory), "codex", "gpt-5.6-luna", reasoning_effort="high",
                )
                second = store.create(
                    Path(directory), "codex", "gpt-5.6-terra", reasoning_effort="low",
                )
            self.assertEqual(first["created_at"], 1234)
            self.assertEqual(first["updated_at"], 1234)
            self.assertEqual(second["created_at"], 1234)
            self.assertEqual(second["updated_at"], 1234)
            reloaded = PersistentChatStore(path, context_usage=self._usage)
            self.assertEqual(
                reloaded.latest_work_defaults("codex"),
                ("gpt-5.6-terra", "low"),
            )

    def test_selecting_an_older_session_makes_its_selection_the_default(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "chats.json"
            store = PersistentChatStore(path, context_usage=self._usage)
            older = store.create(
                Path(directory), "codex", "gpt-5.6-luna", reasoning_effort="high",
            )
            store.create(
                Path(directory), "codex", "gpt-5.6-terra", reasoning_effort="low",
            )
            with store.lock:
                store.mark_used(store.get(older["id"]))
                store.save()
            reloaded = PersistentChatStore(path, context_usage=self._usage)
            self.assertEqual(
                reloaded.latest_work_defaults("codex"),
                ("gpt-5.6-luna", "high"),
            )

    def test_background_activity_timestamp_cannot_override_last_selection(self):
        with tempfile.TemporaryDirectory() as directory:
            store = PersistentChatStore(
                Path(directory) / "chats.json", context_usage=self._usage,
            )
            selected = store.create(
                Path(directory), "codex", "gpt-5.6-luna", reasoning_effort="high",
            )
            background = store.create(
                Path(directory), "codex", "gpt-5.6-terra", reasoning_effort="low",
            )
            with store.lock:
                store.mark_used(store.get(selected["id"]))
                store.get(background["id"])["updated_at"] = 9_999_999_999
                store.save()
            self.assertEqual(
                store.latest_work_defaults("codex"),
                ("gpt-5.6-luna", "high"),
            )


if __name__ == "__main__":
    unittest.main()
