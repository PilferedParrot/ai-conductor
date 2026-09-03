"""Persistence and validation coverage for dashboard-managed providers."""

import json
import tempfile
import unittest
from http import HTTPStatus
from pathlib import Path
from unittest.mock import MagicMock, patch

from pilferedparrot.config import load_config
from pilferedparrot.dispatch import capture_dispatch
from pilferedparrot.model import Conversation
from pilferedparrot.web import PilferedParrotApp, make_handler


class ModelManagementTests(unittest.TestCase):
    def _config(self, root: Path):
        config = load_config(root / "missing-config.json")
        config["web"]["chat_store"] = str(root / "chats.json")
        config["web"]["model_catalog_store"] = str(root / "models.json")
        config["ledger"] = str(root / "runs.jsonl")
        return config

    def test_add_remove_and_restore_arbitrary_provider_card(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            app = PilferedParrotApp(self._config(root), root)
            added = app.add_provider({
                "template": "xai", "label": "My Grok", "model": "grok-test",
                "base_url": "https://api.x.ai/v1", "api_key_env": "XAI_API_KEY",
            })
            self.assertEqual(added["label"], "My Grok")
            provider = added["id"]
            state = app.state()
            self.assertIn(provider, state["model_catalog"])
            self.assertEqual(state["model_catalog"][provider]["default"], "grok-test")
            self.assertEqual(app.config[provider]["adapter"], "openai_compatible")
            self.assertNotIn("api_key", app.dashboard_models["provider_cards"][provider])

            app.remove_provider({"provider": provider})
            self.assertNotIn(provider, [item["id"] for item in app.state()["providers"]])
            restore_id = f"restore:{provider}"
            self.assertIn(restore_id, [item["id"] for item in app.provider_templates()])

            restarted = PilferedParrotApp(self._config(root), root)
            self.assertNotIn(provider, [item["id"] for item in restarted.state()["providers"]])
            restarted.add_provider({"template": restore_id})
            self.assertIn(provider, [item["id"] for item in restarted.state()["providers"]])

    def test_provider_card_validation_is_safe_and_atomic(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            app = PilferedParrotApp(self._config(root), root)
            before = app.state()["providers"]
            for payload, message in (
                ({"template": "xai", "model": "grok", "base_url": "file:///tmp/x"}, "base URL"),
                ({"template": "xai", "model": "", "base_url": "https://api.x.ai/v1"}, "discover models"),
                ({"template": "xai", "model": "grok", "base_url": "https://api.x.ai/v1",
                  "api_key_env": "not valid"}, "variable"),
            ):
                with self.assertRaisesRegex(ValueError, message):
                    app.add_provider(payload)
            self.assertEqual(app.state()["providers"], before)

    @patch("pilferedparrot.web.open_compatible_url")
    def test_blank_model_is_discovered_and_all_choices_are_saved(self, opener):
        response = MagicMock()
        response.read.return_value = json.dumps({
            "data": [{"id": "grok-fast"}, {"id": "grok-deep"}],
        }).encode()
        opener.return_value.__enter__.return_value = response
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            app = PilferedParrotApp(self._config(root), root)
            added = app.add_provider({
                "template": "custom", "label": "Discovered",
                "base_url": "https://models.example/v1", "model": "",
            })
            catalog = app.state()["model_catalog"][added["id"]]
            self.assertEqual(catalog["default"], "grok-fast")
            self.assertEqual(
                [item["value"] for item in catalog["options"]],
                ["grok-fast", "grok-deep"],
            )

    @patch("pilferedparrot.web.adapter_for")
    def test_picker_poll_refreshes_models_through_provider_adapter(self, factory):
        factory.return_value.models.return_value = [
            {"value": "claude-sonnet-5", "label": "Claude Sonnet 5"},
        ]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            app = PilferedParrotApp(self._config(root), root)
            result = app.poll_provider_models("claude")
        factory.assert_called_once_with("claude", app.config)
        self.assertEqual(result["source"], "native_catalog")
        self.assertEqual(result["options"][0]["label"], "Claude Sonnet 5")
        self.assertIn("polled_at", result)

    def test_dashboard_routes_add_and_remove_providers(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            app = PilferedParrotApp(self._config(root), root)
            handler_type = make_handler(app)
            handler = object.__new__(handler_type)
            handler._control_allowed = lambda _scope="dashboard": True
            handler._request_capability_context = lambda **_kwargs: {
                "scope": "dashboard", "window_id": "main", "provider": "codex",
            }
            responses = []
            handler._json = lambda payload, status=HTTPStatus.OK: responses.append((status, payload))
            handler.path = "/api/providers"
            handler._read_json = lambda: {
                "template": "ollama", "label": "Local Llama", "model": "llama3.2",
                "base_url": "http://127.0.0.1:11434/v1", "api_key_env": "",
            }
            handler.do_POST()
            self.assertEqual(responses[-1][0], HTTPStatus.CREATED)
            provider = responses[-1][1]["id"]
            handler.path = "/api/providers/remove"
            handler._read_json = lambda: {"provider": provider}
            handler.do_POST()
            self.assertEqual(responses[-1], (HTTPStatus.OK, {"ok": True}))

    def test_model_poll_route_uses_dashboard_capability(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            app = PilferedParrotApp(self._config(root), root)
            handler_type = make_handler(app)
            handler = object.__new__(handler_type)
            handler.path = "/api/providers/claude/models"
            handler._local_request_allowed = lambda: True
            handler._request_capability_scope = lambda **_kwargs: "dashboard"
            handler._json = MagicMock()
            response = {"provider": "claude", "options": []}
            with patch.object(app, "poll_provider_models", return_value=response) as poll:
                handler.do_GET()
            poll.assert_called_once_with("claude")
            handler._json.assert_called_once_with(response)

    @patch("pilferedparrot.dispatch.run_compatible_agent", return_value="generic result")
    def test_custom_provider_uses_contained_compatible_agent(self, runner):
        config = load_config("/definitely/missing/config.json")
        config["my-llm"] = {
            "adapter": "openai_compatible", "model": "model-1",
        }
        conversation = Conversation()
        result = capture_dispatch(
            "my-llm", "work", Path("/tmp"), conversation, config,
        )
        self.assertEqual(result.text, "generic result")
        self.assertEqual(conversation.provider, "my-llm")
        self.assertEqual(runner.call_args.args[0], "my-llm")


if __name__ == "__main__":
    unittest.main()
