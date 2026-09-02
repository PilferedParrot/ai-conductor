"""Regression coverage for context-window accounting and its browser surfaces."""

import json
import re
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from pilferedparrot.config import (
    load_config, model_catalog, model_context_window, model_max_context_window,
)
from pilferedparrot.dispatch import RunResult, capture_codex, capture_dispatch
from pilferedparrot.model import Conversation
from pilferedparrot.web import ChatStore, PilferedParrotApp


ROOT = Path(__file__).parents[1]
ASSETS = ROOT / "pilferedparrot" / "web_assets"


def _wait_for_idle(app, chat_id=None):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        with app.runs_lock:
            busy = app.chat_run is not None if chat_id is None else chat_id in app.runs
        if not busy:
            return
        time.sleep(0.01)
    raise AssertionError("background response did not finish")


def _app(directory):
    config = load_config(Path(directory) / "missing-config.json")
    config["web"]["chat_store"] = str(Path(directory) / "chats.json")
    config["ledger"] = str(Path(directory) / "runs.jsonl")
    config["codex"]["model"] = "gpt-context-test"
    config["codex"]["context_window_tokens"] = 128_000
    return PilferedParrotApp(config, Path(directory))


class ContextDataTests(unittest.TestCase):
    def test_turn_completed_usage_is_exact_and_cached_input_is_not_added_twice(self):
        events = [
            {"type": "thread.started", "thread_id": "thread-1"},
            {"type": "item.completed", "item": {"type": "agent_message", "text": "Done"}},
            {"type": "turn.completed", "usage": {
                "input_tokens": 1_200, "output_tokens": 300, "cached_input_tokens": 900,
            }},
        ]
        config = load_config()
        config["codex"]["command"] = "/usr/bin/codex"
        with patch("pilferedparrot.dispatch._codex_command", return_value=["codex"]), \
             patch("pilferedparrot.dispatch._stream_process") as stream:
            def emit(_command, _prompt, _cwd, *, stdout_line, **_kwargs):
                for event in events:
                    stdout_line(json.dumps(event) + "\n")
                return subprocess.CompletedProcess(["codex"], 0, "", "")

            stream.side_effect = emit
            result = capture_codex("hello", Path.cwd(), Conversation(), config)
        self.assertEqual((result.input_tokens, result.output_tokens), (1_200, 300))
        self.assertEqual(result.session_id, "thread-1")

    def test_model_catalog_exposes_context_window(self):
        with tempfile.TemporaryDirectory() as directory:
            config = load_config()
            cache = Path(directory) / "models.json"
            cache.write_text(json.dumps({"models": [{
                "slug": "gpt-context-test", "display_name": "Context test",
                "visibility": "list", "context_window": 272_000,
                "max_context_window": 872_000,
            }]}))
            config["codex"]["models_cache"] = str(cache)
            config["codex"]["model"] = "gpt-context-test"
            catalog = model_catalog(config)
            option = next(item for item in catalog["codex"]["options"]
                          if item["value"] == "gpt-context-test")
            self.assertEqual(option["context_window"], 272_000)
            self.assertEqual(option["max_context_window"], 872_000)
            self.assertEqual(model_max_context_window(config, "codex"), 872_000)
            self.assertEqual(model_context_window(config, "codex"), 872_000)
            self.assertEqual(model_context_window(config, "codex", percent=50), 436_000)

    def test_context_maximum_follows_selected_model_on_each_surface(self):
        with tempfile.TemporaryDirectory() as directory:
            config = load_config()
            config["web"]["chat_store"] = str(Path(directory) / "chats.json")
            config["codex"]["models_cache"] = str(Path(directory) / "models.json")
            config["codex"]["model"] = "gpt-5.6-terra"
            config["codex"]["context_window_tokens"] = None
            Path(config["codex"]["models_cache"]).write_text(json.dumps({"models": [
                {"slug": "gpt-5.6-terra", "display_name": "Terra",
                 "visibility": "list", "context_window": 100_000,
                 "max_context_window": 128_000},
                {"slug": "gpt-5.6-luna", "display_name": "Luna",
                 "visibility": "list", "context_window": 200_000,
                 "max_context_window": 256_000},
            ]}))
            app = PilferedParrotApp(config, Path(directory))
            technical = app.create_chat({
                "provider": "codex", "cwd": directory, "model": "gpt-5.6-luna",
            })
            chat = app.store.chat_public()

            self.assertEqual(technical["requested_model"], "gpt-5.6-luna")
            self.assertEqual(technical["context_usage"]["max_tokens"], 256_000)
            self.assertEqual(chat["model"], "gpt-5.6-terra")
            self.assertEqual(chat["context_usage"]["max_tokens"], 128_000)
            self.assertNotEqual(
                technical["context_usage"]["max_tokens"],
                chat["context_usage"]["max_tokens"],
            )

    @patch("pilferedparrot.qwen._chat_completion")
    def test_qwen_completion_usage_is_not_presented_as_context(self, complete):
        complete.return_value = {
            "content": "Done", "_pilferedparrot_usage": {
                "prompt_tokens": 700, "completion_tokens": 25, "total_tokens": 725,
            },
        }
        conversation = Conversation()
        result = capture_dispatch(
            "qwen", "hello", Path.cwd(), conversation, load_config(),
        )
        self.assertEqual(result.text, "Done")
        self.assertEqual((result.input_tokens, result.output_tokens), (700, 25))

    def test_public_objects_always_have_context_usage_schema(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ChatStore(Path(directory) / "chats.json")
            technical = store.create(Path(directory), "codex")
            chat = store.chat_public()
        for public in (technical, chat):
            self.assertEqual(set(public["context_usage"]), {
                "used_tokens", "limit_tokens", "max_tokens", "allowance_percent",
                "percent", "estimated", "basis",
            })
            self.assertTrue(public["context_usage"]["estimated"])
            self.assertEqual(public["context_usage"]["basis"], "visible_transcript")

    def test_missing_backend_usage_is_a_labeled_estimate(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ChatStore(Path(directory) / "chats.json")
            chat = store.create(Path(directory), "codex")
            with store.lock:
                stored = store.get(chat["id"])
                stored["messages"].append({"role": "user", "content": "x" * 400})
            usage = store.public(stored)["context_usage"]
        self.assertTrue(usage["estimated"])
        self.assertGreater(usage["used_tokens"], 0)


class ContextPersistenceTests(unittest.TestCase):
    def test_user_context_allowance_is_persisted_and_applied_to_codex(self):
        with tempfile.TemporaryDirectory() as directory:
            app = _app(directory)
            chat = app.create_chat({"provider": "codex", "cwd": directory})
            updated = app.set_context_window(chat["id"], {"percent": 50})
            self.assertEqual(updated["context_usage"]["max_tokens"], 128_000)
            self.assertEqual(updated["context_usage"]["limit_tokens"], 64_000)
            self.assertEqual(updated["context_usage"]["allowance_percent"], 50)
            calls = []

            def dispatch(_provider, _prompt, _cwd, _conversation, config, _cancel):
                calls.append(config["codex"]["context_window_limit_tokens"])
                return RunResult("Done", 0, session_id="context-thread")

            with patch("pilferedparrot.web.capture_dispatch", side_effect=dispatch):
                app.send_message(chat["id"], {"content": "hello"})
                _wait_for_idle(app, chat["id"])
            self.assertEqual(calls, [64_000])
            reloaded = PilferedParrotApp(app.config, Path(directory))
            stored = reloaded.store.get(chat["id"])
            self.assertEqual(stored["context_window_percent"], 50)
            self.assertEqual(stored["context_limit_tokens"], 64_000)

    def test_chat_model_choice_is_separate_persisted_and_thread_safe(self):
        with tempfile.TemporaryDirectory() as directory:
            app = _app(directory)
            calls = []

            def dispatch(provider, prompt, cwd, conversation, config, _cancel_event):
                calls.append((provider, prompt, cwd, conversation.provider_session_id, config))
                return RunResult("Luna reply", 0, session_id="luna-session")

            with patch("pilferedparrot.web.capture_dispatch", side_effect=dispatch):
                app.send_chat_message({"content": "hello", "model": "gpt-5.6-luna"})
                _wait_for_idle(app)

            self.assertEqual(app.store.data["chat"]["model"], "gpt-5.6-luna")
            self.assertEqual(calls[0][0:2], ("codex", "hello"))
            self.assertIsNone(calls[0][3])
            self.assertEqual(calls[0][4]["codex"]["model"], "gpt-5.6-luna")
            self.assertEqual(calls[0][4]["codex"]["sandbox"], "read-only")
            self.assertEqual(app.state()["chat_model_choices"], [
                "gpt-5.6-terra", "gpt-5.6-luna",
            ])
            with self.assertRaisesRegex(ValueError, "new chat"):
                app.send_chat_message({"content": "switch", "model": "gpt-5.6-terra"})

            reset = app.reset_chat()
            self.assertEqual(reset["chat"]["model"], "gpt-5.6-terra")
            self.assertEqual(reset["chat_history"][0]["model"], "gpt-5.6-luna")
            with self.assertRaisesRegex(ValueError, "terra or gpt-5.6-luna"):
                app.send_chat_message({"content": "invalid", "model": "gpt-5.6-sol"})

    def test_empty_chat_model_selection_updates_context_before_first_message(self):
        with tempfile.TemporaryDirectory() as directory:
            config = load_config()
            config["web"]["chat_store"] = str(Path(directory) / "chats.json")
            config["codex"]["models_cache"] = str(Path(directory) / "models.json")
            config["codex"]["context_window_tokens"] = None
            Path(config["codex"]["models_cache"]).write_text(json.dumps({"models": [
                {"slug": "gpt-5.6-terra", "visibility": "list",
                 "max_context_window": 128_000},
                {"slug": "gpt-5.6-luna", "visibility": "list",
                 "max_context_window": 256_000},
            ]}))
            app = PilferedParrotApp(config, Path(directory))

            selected = app.set_chat_model({"model": "gpt-5.6-luna"})

            self.assertEqual(selected["model"], "gpt-5.6-luna")
            self.assertEqual(selected["context_usage"]["max_tokens"], 256_000)
            self.assertEqual(
                app.state()["model_context_windows"]["codex"]["gpt-5.6-terra"],
                128_000,
            )
            self.assertEqual(
                app.state()["model_context_windows"]["codex"]["gpt-5.6-luna"],
                256_000,
            )

    def test_reset_chat_can_start_with_selected_model(self):
        with tempfile.TemporaryDirectory() as directory:
            app = _app(directory)
            app.store.data["chat"]["messages"].append({
                "role": "user", "content": "archive me",
            })

            reset = app.reset_chat({"model": "gpt-5.6-luna"})

            self.assertEqual(reset["chat"]["model"], "gpt-5.6-luna")
            self.assertEqual(reset["chat_history"][0]["model"], "gpt-5.6-terra")

    def test_completed_technical_message_uses_visible_transcript_not_aggregate_usage(self):
        with tempfile.TemporaryDirectory() as directory:
            app = _app(directory)
            chat = app.create_chat({"provider": "codex", "cwd": directory})
            results = iter([
                RunResult("first", 0, session_id="s1", input_tokens=80, output_tokens=20),
                RunResult("second", 0, session_id="s1", input_tokens=1_800, output_tokens=200),
            ])
            with patch("pilferedparrot.web.capture_dispatch", side_effect=lambda *a: next(results)):
                app.send_message(chat["id"], {"content": "one"})
                _wait_for_idle(app, chat["id"])
                app.send_message(chat["id"], {"content": "two"})
                _wait_for_idle(app, chat["id"])
            public = next(item for item in app.store.list_public() if item["id"] == chat["id"])
            self.assertEqual(public["context_usage"]["used_tokens"], 5)
            self.assertTrue(public["context_usage"]["estimated"])
            self.assertEqual(public["context_usage"]["basis"], "visible_transcript")
            self.assertNotIn("context_used_tokens", app.store.get(chat["id"]))
            self.assertEqual(app.store.get(chat["id"])["last_turn_usage"], {
                "input_tokens": 1_800, "output_tokens": 200,
            })
            reloaded = ChatStore(Path(directory) / "chats.json")
            self.assertEqual(reloaded.public(reloaded.get(chat["id"]))["context_usage"],
                             public["context_usage"])

    def test_completed_chat_message_uses_visible_transcript_not_aggregate_usage(self):
        with tempfile.TemporaryDirectory() as directory:
            app = _app(directory)
            result = RunResult("Hello.", 0,
                               session_id="p1", input_tokens=500, output_tokens=100)
            with patch("pilferedparrot.web.capture_dispatch", return_value=result):
                app.send_chat_message({"content": "hello"})
                _wait_for_idle(app)
            public = app.store.chat_public()
            self.assertEqual(public["context_usage"]["used_tokens"], 3)
            self.assertTrue(public["context_usage"]["estimated"])
            self.assertEqual(public["context_usage"]["basis"], "visible_transcript")
            self.assertEqual(app.store.data["chat"]["last_turn_usage"], {
                "input_tokens": 500, "output_tokens": 100,
            })
            reloaded = ChatStore(Path(directory) / "chats.json")
            self.assertEqual(reloaded.chat_public()["context_usage"], public["context_usage"])

    def test_load_migrates_inflated_aggregate_usage_and_backfills_known_limits(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "chats.json"
            path.write_text(json.dumps({
                "version": 4,
                "chats": [{
                    "id": "technical", "requested_provider": "codex",
                    "requested_model": "gpt-context-test", "messages": [],
                    "context_used_tokens": 4_000_000,
                }],
                "coordinator": {
                    "id": "old-chat", "messages": [{"role": "user", "content": "old"}],
                    "context_used_tokens": 800_000,
                },
            }))
            config = load_config()
            config["web"]["chat_store"] = str(path)
            config["codex"]["model"] = "gpt-context-test"
            config["codex"]["context_window_tokens"] = 128_000
            app = PilferedParrotApp(config, Path(directory))
            technical = app.store.get("technical")
            current_chat = app.store.data["chat"]
        self.assertNotIn("context_used_tokens", technical)
        self.assertNotIn("context_used_tokens", current_chat)
        self.assertEqual(technical["context_limit_tokens"], 128_000)
        self.assertEqual(current_chat["context_limit_tokens"], 128_000)
        self.assertEqual(app.store.public(technical)["context_usage"]["used_tokens"], 0)
        self.assertEqual(app.store.chat_public()["context_usage"]["used_tokens"], 1)


class ContextFrontendTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.js = (ASSETS / "app.js").read_text(encoding="utf-8")
        cls.chat_js = (ASSETS / "chat.js").read_text(encoding="utf-8")
        cls.css = (ASSETS / "app.css").read_text(encoding="utf-8")
        cls.html = (ASSETS / "index.html").read_text(encoding="utf-8")
        cls.chat_html = (ASSETS / "chat.html").read_text(encoding="utf-8")

    def test_both_surfaces_render_compact_used_limit_bar_percentage_and_estimate_label(self):
        self.assertIn("context_usage", self.js)
        self.assertRegex(self.js, r"(?i)estimate")
        self.assertRegex(self.js, r"(?i)(context|usage)[-_]?(bar|meter)")
        self.assertRegex(self.js + self.chat_js, r"(?i)(technicalContext|chatContext)")
        self.assertRegex(self.css, r"(?i)(context|usage)[-_]?(bar|meter)")
        self.assertRegex(self.js, r"percent[^\n]{0,100}%")
        self.assertIn("technicalContext", self.html)
        self.assertIn("chatContext", self.chat_html)
        self.assertNotIn("chatContext", self.html)
        self.assertIn("data-context-percent", self.js)
        self.assertIn("data-context-percent", self.chat_js)
        self.assertIn("max_tokens", self.js + self.chat_js)
        self.assertIn("/api/chat/context", self.chat_js)


if __name__ == "__main__":
    unittest.main()
