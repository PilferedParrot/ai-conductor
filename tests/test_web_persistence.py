import json
import stat
import tempfile
import unittest
from pathlib import Path

from pilferedparrot.web_persistence import (
    PersistentChatStore, chat_store_path, legacy_chat_store_path,
)


class PersistencePathTests(unittest.TestCase):
    def test_configured_chat_store_path_is_resolved(self):
        with tempfile.TemporaryDirectory() as directory:
            configured = Path(directory) / "nested" / "chats.json"
            config = {"web": {"chat_store": str(configured)}}
            self.assertEqual(chat_store_path(config), configured.resolve())
            self.assertIsNone(legacy_chat_store_path(config))

    def test_default_store_retains_legacy_fallback(self):
        config = {"web": {"chat_store": "~/.local/state/pilferedparrot/chats.json"}}
        self.assertEqual(
            legacy_chat_store_path(config),
            Path("~/.local/state/ai-conductor/chats.json").expanduser().resolve(),
        )


class PersistentChatStoreTests(unittest.TestCase):
    @staticmethod
    def _usage(_context_chars, _fallback_chars, **_kwargs):
        return {"percent": 0, "used_tokens": 0, "limit_tokens": 1}

    def test_state_write_remains_owner_only_after_extraction(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "chats.json"
            store = PersistentChatStore(path, context_usage=self._usage)
            store.save()
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["version"], 5)


if __name__ == "__main__":
    unittest.main()
