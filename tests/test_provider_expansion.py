"""Application integration checks for the new coding-provider paths."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pilferedparrot.adapters import adapter_for
from pilferedparrot.budgets import collect_budgets
from pilferedparrot.config import load_config
from pilferedparrot.web import PilferedParrotApp
from pilferedparrot.web_provider import ProviderRunOrchestrator


class ProviderExpansionTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.config = load_config(self.root / "missing.json")
        self.config["web"].update({
            "chat_store": str(self.root / "chats.json"),
            "model_catalog_store": str(self.root / "models.json"),
        })
        self.config["ledger"] = str(self.root / "runs.jsonl")

    def test_coding_templates_persist_and_restore_without_discovery(self):
        app = PilferedParrotApp(self.config, self.root)
        with patch.object(app, "_discover_provider_models") as discover:
            cards = [app.add_provider({"template": template, "model": model})
                     for template, model in (
                         ("openrouter", "deepseek/fixture"),
                         ("mistral", "devstral-fixture"),
                         ("lmstudio", "local-fixture"),
                     )]
        discover.assert_not_called()
        restored = PilferedParrotApp(self.config, self.root)
        for card, endpoint, variable in zip(cards, (
            "https://openrouter.ai/api/v1", "https://api.mistral.ai/v1",
            "http://127.0.0.1:1234/v1",
        ), ("OPENROUTER_API_KEY", "MISTRAL_API_KEY", "")):
            settings = restored.config[card["id"]]
            self.assertEqual(settings["base_url"], endpoint)
            self.assertEqual(settings["api_key_env"], variable)
            self.assertTrue(adapter_for(card["id"], restored.config).capabilities.tools)

    def test_antigravity_work_resume_is_preserved_and_chat_rejected_before_mutation(self):
        app = PilferedParrotApp(self.config, self.root)
        orchestrator = ProviderRunOrchestrator(self.config)
        model = self.config["antigravity"]["model"]
        prepared = orchestrator.prepare(
            "antigravity", self.root, session_id="exact-session", model=model,
            current_provider="antigravity", current_model=model,
        )
        self.assertEqual(prepared.conversation.provider_session_id, "exact-session")
        original = app.store.data["chat"].copy()
        for operation in (
            lambda: orchestrator.prepare("antigravity", self.root, mode="chat"),
            lambda: app.issue_capability("chat", provider="antigravity"),
            lambda: app.open_chat_window("http://127.0.0.1", {"provider": "antigravity"}),
            lambda: app.send_chat_message({"provider": "antigravity", "content": "hello"}),
        ):
            with self.assertRaisesRegex(ValueError, "Work only"):
                operation()
        self.assertEqual(app.store.data["chat"], original)
        self.assertIsNone(app.chat_run)

    def test_antigravity_presence_does_not_claim_authenticated_access(self):
        self.config["_hidden_providers"] = ["qwen", "codex", "claude", "gemini"]
        with patch("pilferedparrot.config.resolve_command", return_value="/fake/agy"):
            budget = collect_budgets(self.config)["antigravity"]
        self.assertTrue(budget.available)
        self.assertEqual(budget.status, "auth_unverified")
        self.assertIn("checked when used", budget.note)


if __name__ == "__main__":
    unittest.main()
