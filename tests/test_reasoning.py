"""Reasoning selection is local metadata, persisted state, and a Codex-only flag."""

from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from pilferedparrot.config import load_config, model_catalog
from pilferedparrot.dispatch import RunResult
from pilferedparrot.web import PilferedParrotApp


class ReasoningTests(unittest.TestCase):
    def config(self, root: Path) -> dict:
        config = load_config("/definitely/missing/pilferedparrot-config.json")
        cache = root / "models.json"
        cache.write_text(json.dumps({"models": [{
            "slug": "gpt-reasoning", "display_name": "Reasoning",
            "supported_reasoning_levels": [
                {"effort": "minimal", "description": "Fast"},
                {"effort": "low", "description": "Brief"},
                {"effort": "high", "description": "Detailed"},
                {"effort": "ultra", "description": "Thorough"},
                {"effort": "not-a-level", "description": "Ignore"},
            ],
            "default_reasoning_level": "low",
        }]}))
        config["codex"].update({"models_cache": str(cache), "model": "gpt-reasoning",
                                "config_path": str(root / "codex.toml")})
        config["ledger"] = str(root / "runs.jsonl")
        config["web"].update({"chat_store": str(root / "chats.json"), "chat_model": "gpt-reasoning",
                              "model_catalog_store": str(root / "dashboard-models.json")})
        return config

    def wait_for_work(self, app: PilferedParrotApp, chat_id: str) -> dict:
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            state = app.chat_state(chat_id)
            with app.runs_lock:
                finished = chat_id not in app.runs
            if not any(message.get("pending") for message in state["messages"]) and finished:
                return state
            time.sleep(0.01)
        self.fail("work response did not finish")

    def wait_for_chat(self, app: PilferedParrotApp) -> dict:
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            state = app.current_chat_state()
            with app.runs_lock:
                finished = app.chat_run is None
            if not state["pending"] and finished:
                return state
            time.sleep(0.01)
        self.fail("Chat response did not finish")

    def test_catalog_uses_cached_capabilities_and_defaults(self):
        with tempfile.TemporaryDirectory() as directory:
            catalog = model_catalog(self.config(Path(directory)))["codex"]
            option = next(item for item in catalog["options"] if item["value"] == "gpt-reasoning")
            self.assertEqual(option["reasoning_efforts"], ["minimal", "low", "high", "ultra"])
            self.assertEqual(option["default_reasoning_effort"], "low")
            self.assertEqual(catalog["reasoning_default_label"], "Codex default")
            self.assertEqual(catalog["chat_reasoning_default_label"], "Chat default · Low")

    def test_empty_config_override_is_an_explicit_no_capability(self):
        with tempfile.TemporaryDirectory() as directory:
            config = self.config(Path(directory))
            config["codex"]["model_options"] = [{
                "value": "gpt-no-reasoning", "reasoning_efforts": [],
            }]
            option = next(item for item in model_catalog(config)["codex"]["options"]
                          if item["value"] == "gpt-no-reasoning")
            self.assertEqual(option["reasoning_efforts"], [])

    def test_new_session_recovers_when_inherited_effort_is_no_longer_supported(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self.config(root)
            app = PilferedParrotApp(config, root)
            app.create_chat({"model": "gpt-reasoning", "reasoning_effort": "ultra"})
            # Provider metadata can change between sessions without a user
            # choosing a different model. A stale preference must not block New.
            config["codex"]["model_options"] = [{
                "value": "gpt-reasoning", "reasoning_efforts": ["low", "high"],
            }]
            reloaded = PilferedParrotApp(config, root)
            chat = reloaded.create_chat({})
            self.assertEqual(chat["requested_model"], "gpt-reasoning")
            self.assertIsNone(chat["reasoning_effort"])
            with self.assertRaisesRegex(ValueError, "not supported"):
                reloaded.create_chat({"reasoning_effort": "ultra"})

    def test_work_choice_reaches_dispatch_persists_and_keeps_resume_when_changed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self.config(root)
            app = PilferedParrotApp(config, root)
            chat = app.create_chat({"model": "gpt-reasoning", "reasoning_effort": "ultra"})
            seen = []

            def dispatch(_provider, _prompt, _cwd, conversation, run_config, _cancel):
                seen.append((conversation.provider_session_id, run_config["codex"].get("reasoning_effort")))
                return RunResult("done", 0, f"session-{len(seen)}")

            with patch("pilferedparrot.web.capture_dispatch", side_effect=dispatch):
                app.send_message(chat["id"], {"content": "first"})
                self.wait_for_work(app, chat["id"])
                saved = app.set_reasoning_effort(chat["id"], {"reasoning_effort": "high"})
                self.assertEqual(saved["reasoning_effort"], "high")
                app.send_message(chat["id"], {"content": "second"})
                self.wait_for_work(app, chat["id"])
            self.assertEqual(seen, [(None, "ultra"), ("session-1", "high")])
            reloaded = PilferedParrotApp(config, root)
            self.assertEqual(reloaded.chat_state(chat["id"])["reasoning_effort"], "high")

    def test_work_null_choice_uses_configured_codex_default(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self.config(root)
            config["codex"]["reasoning_effort"] = "high"
            app = PilferedParrotApp(config, root)
            chat = app.create_chat({"model": "gpt-reasoning", "reasoning_effort": None})
            seen = []
            with patch("pilferedparrot.web.capture_dispatch", side_effect=lambda *_args: (
                seen.append(_args[4]["codex"].get("reasoning_effort")) or RunResult("done", 0, "s")
            )):
                app.send_message(chat["id"], {"content": "default"})
                self.wait_for_work(app, chat["id"])
            self.assertEqual(seen, ["high"])
            config["codex"].pop("reasoning_effort")
            config["web"]["chat_store"] = str(root / "no-default.json")
            no_default = PilferedParrotApp(config, root)
            work = no_default.create_chat({"model": "gpt-reasoning"})
            keys = []
            with patch("pilferedparrot.web.capture_dispatch", side_effect=lambda *_args: (
                keys.append("reasoning_effort" in _args[4]["codex"]) or RunResult("done", 0, "s")
            )):
                no_default.send_message(work["id"], {"content": "inherit cli"})
                self.wait_for_work(no_default, work["id"])
            self.assertEqual(keys, [False])

    def test_new_work_session_inherits_latest_selection_after_reload(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self.config(root)
            app = PilferedParrotApp(config, root)
            first = app.create_chat({"model": "gpt-reasoning", "reasoning_effort": "ultra"})
            second = app.create_chat({})
            self.assertEqual(
                (second["requested_model"], second["reasoning_effort"]),
                ("gpt-reasoning", "ultra"),
            )
            reloaded = PilferedParrotApp(config, root)
            third = reloaded.create_chat({})
            self.assertEqual(
                (third["requested_model"], third["reasoning_effort"]),
                ("gpt-reasoning", "ultra"),
            )
            # An explicitly selected model cannot inherit an unsupported
            # effort from the prior selection.
            changed = reloaded.create_chat({"model": "manual-codex-model"})
            self.assertEqual(changed["reasoning_effort"], None)
            with self.assertRaisesRegex(ValueError, "not supported"):
                reloaded.create_chat({
                    "model": "manual-codex-model", "reasoning_effort": "ultra",
                })

    def test_opening_an_older_work_session_restores_its_defaults(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self.config(root)
            app = PilferedParrotApp(config, root)
            older = app.create_chat({"model": "gpt-reasoning", "reasoning_effort": "ultra"})
            app.create_chat({"model": "manual-codex-model", "reasoning_effort": "high"})
            app.activate_chat(older["id"])
            reloaded = PilferedParrotApp(config, root)
            inherited = reloaded.create_chat({})
            self.assertEqual(
                (inherited["requested_model"], inherited["reasoning_effort"]),
                ("gpt-reasoning", "ultra"),
            )

    def test_chat_explicit_choice_and_null_use_distinct_run_defaults(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            app = PilferedParrotApp(self.config(root), root)
            app.reset_chat({"model": "gpt-reasoning", "reasoning_effort": "ultra"})
            seen = []

            def dispatch(_provider, _prompt, _cwd, _conversation, run_config, _cancel):
                seen.append(run_config["codex"].get("reasoning_effort"))
                return RunResult("done", 0, f"chat-{len(seen)}")

            with patch("pilferedparrot.web.capture_dispatch", side_effect=dispatch):
                app.send_chat_message({"content": "explicit"})
                self.wait_for_chat(app)
                app.set_chat_reasoning_effort({"reasoning_effort": None})
                app.send_chat_message({"content": "default"})
                self.wait_for_chat(app)
            self.assertEqual(seen, ["ultra", "low"])

    def test_non_codex_explicit_effort_is_rejected_and_chat_default_stays_distinct(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            app = PilferedParrotApp(self.config(root), root)
            with self.assertRaisesRegex(ValueError, "only supported for Codex"):
                app.create_chat({"provider": "claude", "reasoning_effort": "low"})
            app.reset_chat({"model": "gpt-reasoning"})
            chat = app.set_chat_reasoning_effort({"reasoning_effort": "ultra"})
            self.assertEqual(chat["reasoning_effort"], "ultra")
            reset = app.reset_chat({"reasoning_effort": None})
            self.assertIsNone(reset["chat"]["reasoning_effort"])

    def test_unsupported_effort_cannot_mutate_or_dispatch(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            app = PilferedParrotApp(self.config(root), root)
            work = app.create_chat({"model": "gpt-reasoning"})
            before = app.chat_state(work["id"])["messages"]
            with patch("pilferedparrot.web.capture_dispatch") as dispatch:
                with self.assertRaisesRegex(ValueError, "not supported"):
                    app.send_message(work["id"], {
                        "content": "nope", "reasoning_effort": "max",
                    })
            self.assertFalse(dispatch.called)
            self.assertEqual(app.chat_state(work["id"])["messages"], before)

    def test_invalid_chat_effort_does_not_change_model(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            app = PilferedParrotApp(self.config(root), root)
            app.reset_chat({"model": "gpt-reasoning"})
            before = app.current_chat_state()
            with patch("pilferedparrot.web.capture_dispatch") as dispatch:
                with self.assertRaisesRegex(ValueError, "not supported"):
                    app.send_chat_message({
                        "content": "invalid", "model": "unknown-model", "reasoning_effort": "ultra",
                    })
            self.assertFalse(dispatch.called)
            self.assertEqual(app.current_chat_state(), before)

    def test_reasoning_setter_rejects_running_work_and_other_window(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            app = PilferedParrotApp(self.config(root), root)
            work = app.create_chat({"model": "gpt-reasoning"}, window_id="window-a")
            with app.runs_lock:
                app.runs[work["id"]] = object()
            with self.assertRaisesRegex(ValueError, "stop the response"):
                app.set_reasoning_effort(work["id"], {"reasoning_effort": "high"}, window_id="window-a")
            with app.runs_lock:
                app.runs.pop(work["id"], None)
            with self.assertRaises(KeyError):
                app.set_reasoning_effort(work["id"], {"reasoning_effort": "high"}, window_id="window-b")
