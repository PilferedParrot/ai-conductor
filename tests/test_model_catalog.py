"""Focused coverage for configurable provider model catalogs."""

import json
import tempfile
import unittest
from pathlib import Path

from pilferedparrot.config import load_config, model_catalog, model_context_window, model_max_context_window


class ModelCatalogTests(unittest.TestCase):
    def test_legacy_and_structured_options_are_normalized(self):
        config = load_config("/definitely/missing/config.json")
        config["qwen"]["model_options"] = [
            "legacy-model",
            {"id": "structured-model", "label": "Structured", "context_window": 32_000,
             "max_context_window": 64_000},
            {"model": "model-alias", "context_window": "bad"},
            {"value": ""}, None, 42,
        ]
        options = model_catalog(config)["qwen"]["options"]
        self.assertEqual(options[:3], [
            {"value": "legacy-model", "label": "legacy-model"},
            {"value": "structured-model", "label": "Structured",
             "context_window": 32_000, "max_context_window": 64_000},
            {"value": "model-alias", "label": "model-alias"},
        ])
        self.assertEqual(options[3]["value"], "qwen3-coder-next")

    def test_per_model_window_is_used_for_non_codex_provider(self):
        config = load_config("/definitely/missing/config.json")
        config["claude"]["model_options"] = [
            {"value": "custom", "max_context_window": 200_000},
        ]
        self.assertEqual(model_max_context_window(config, "claude", "custom"), 200_000)
        self.assertEqual(model_context_window(config, "claude", "custom", 50), 100_000)

    def test_provider_window_is_fallback_and_overrides_model_metadata(self):
        config = load_config("/definitely/missing/config.json")
        config["claude"]["model_options"] = [
            {"value": "custom", "max_context_window": 200_000},
        ]
        config["claude"]["context_window_tokens"] = 300_000
        self.assertEqual(model_max_context_window(config, "claude", "custom"), 300_000)

    def test_structured_option_can_override_discovered_codex_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory) / "models.json"
            cache.write_text(json.dumps({"models": [{
                "slug": "gpt-custom", "display_name": "Discovered",
                "visibility": "list", "max_context_window": 100_000,
            }]}))
            config = load_config("/definitely/missing/config.json")
            config["codex"]["models_cache"] = str(cache)
            config["codex"]["model_options"] = [{
                "value": "gpt-custom", "label": "My deployment",
                "max_context_window": 300_000,
            }]
            option = model_catalog(config)["codex"]["options"][0]
            self.assertEqual(option["label"], "My deployment")
            self.assertEqual(option["max_context_window"], 300_000)
            self.assertEqual(model_max_context_window(config, "codex", "gpt-custom"), 300_000)

    def test_default_claude_catalog_exposes_numbered_models_only(self):
        options = model_catalog(load_config("/definitely/missing/config.json"))["claude"]["options"]
        self.assertEqual(options, [
            {"value": "claude-fable-5-1", "label": "Claude Fable 5.1",
             "context_window": 200_000, "max_context_window": 200_000},
            {"value": "claude-fable-5", "label": "Claude Fable 5",
             "context_window": 200_000, "max_context_window": 200_000},
            {"value": "claude-opus-5", "label": "Claude Opus 5",
             "context_window": 200_000, "max_context_window": 200_000},
            {"value": "claude-opus-4-8", "label": "Claude Opus 4.8",
             "context_window": 200_000, "max_context_window": 200_000},
            {"value": "claude-sonnet-5", "label": "Claude Sonnet 5",
             "context_window": 200_000, "max_context_window": 200_000},
            {"value": "claude-sonnet-4-6", "label": "Claude Sonnet 4.6",
             "context_window": 200_000, "max_context_window": 200_000},
            {"value": "claude-haiku-4-5", "label": "Claude Haiku 4.5",
             "context_window": 200_000, "max_context_window": 200_000},
        ])
        self.assertEqual(
            [item["value"] for item in options if item["value"].startswith("claude-fable-")],
            ["claude-fable-5-1", "claude-fable-5"],
        )
        self.assertEqual(
            model_max_context_window(load_config(), "claude", "claude-sonnet-5"), 200_000,
        )
        self.assertEqual(
            model_max_context_window(load_config(), "claude", "claude-haiku-4-5"), 200_000,
        )
        self.assertEqual(model_max_context_window(load_config(), "claude"), 200_000)


if __name__ == "__main__":
    unittest.main()
