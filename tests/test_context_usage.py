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
from pilferedparrot.dispatch import (
    RunResult, _codex_session_live_usage, capture_codex, capture_dispatch,
)
from pilferedparrot.model import Conversation
from pilferedparrot.web import (
    ChatStore, PilferedParrotApp, _context_usage,
    _initial_context_overhead_tokens, _output_reservation_tokens,
)


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
    config["codex"]["models_cache"] = str(Path(directory) / "missing-models.json")
    return PilferedParrotApp(config, Path(directory))


class ContextDataTests(unittest.TestCase):
    @patch("pilferedparrot.web._codex_instruction_tokens")
    def test_claude_context_fallback_does_not_use_codex_assumptions(self, codex_tokens):
        config = load_config("/definitely/missing/config.json")
        overhead = _initial_context_overhead_tokens(
            config, "claude", "claude-sonnet-5", Path("/tmp"),
        )
        codex_tokens.assert_not_called()
        self.assertGreaterEqual(overhead, 0)
        self.assertEqual(
            _output_reservation_tokens(config, "claude", "claude-sonnet-5", 200_000),
            0,
        )

    def test_context_usage_excludes_reserved_output_capacity_from_used_tokens(self):
        breakdown = {
            "transcript": 1_000,
            "instructions": 700,
            "tools": 450,
            "workspace": 300,
            "prompt_inputs": 250,
            "output_reservation": 1_200,
        }

        usage = _context_usage(
            0, 1, limit_tokens=10_000, breakdown=breakdown,
        )

        self.assertEqual(
            usage["used_tokens"],
            sum(breakdown.values()) - breakdown["output_reservation"],
        )
        self.assertEqual(usage["limit_tokens"], 10_000)
        self.assertEqual(usage["breakdown"], breakdown)
        self.assertEqual(usage["basis"], "live_next_request")

    def test_live_usage_replaces_fallback_overhead_and_reports_reservation_separately(self):
        usage = _context_usage(
            400, 1, limit_tokens=10_000,
            live_input_tokens=2_000, live_output_tokens=300,
            overhead_tokens=9_000, output_reservation_tokens=700,
        )

        self.assertEqual(usage["used_tokens"], 2_300)
        self.assertEqual(usage["percent"], 23)
        self.assertEqual(usage["transcript_tokens"], 100)
        self.assertEqual(usage["breakdown"], {
            "live_input": 2_000,
            "latest_output": 300,
            "output_reservation": 700,
        })

    def test_turn_completed_usage_is_exact_and_cached_input_is_not_added_twice(self):
        events = [
            {"type": "thread.started", "thread_id": "thread-1"},
            {"type": "item.completed", "item": {"type": "agent_message", "text": "Done"}},
            {"type": "token_count", "info": {
                "last_token_usage": {"input_tokens": 650, "output_tokens": 75},
                "model_context_window": 120_000,
            }},
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
        self.assertEqual((result.live_input_tokens, result.live_output_tokens), (650, 75))
        self.assertEqual(result.live_context_window_tokens, 120_000)
        self.assertEqual(result.session_id, "thread-1")
        self.assertEqual(stream.call_args.args[1], "hello")

    def test_codex_session_telemetry_reads_final_live_request_after_aggregate_usage(self):
        with tempfile.TemporaryDirectory() as directory:
            session_id = "01a00000-0000-7000-8000-000000000001"
            sessions = Path(directory) / "sessions" / "2026" / "09" / "02"
            sessions.mkdir(parents=True)
            (sessions / f"rollout-test-{session_id}.jsonl").write_text("\n".join([
                json.dumps({"type": "event_msg", "payload": {
                    "type": "token_count", "info": {
                        "last_token_usage": {
                            "input_tokens": 700, "output_tokens": 80,
                        },
                        "model_context_window": 9_500,
                    },
                }}),
                json.dumps({"type": "turn.completed", "usage": {
                    "input_tokens": 8_000, "output_tokens": 900,
                }}),
            ]) + "\n")

            with patch.dict("os.environ", {"CODEX_HOME": directory}):
                usage = _codex_session_live_usage(session_id, load_config())

        self.assertEqual(usage, (700, 80, 9_500))

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
                 "max_context_window": 256_000,
                 "effective_context_window_percent": 90},
            ]}))
            app = PilferedParrotApp(config, Path(directory))
            technical = app.create_chat({
                "provider": "codex", "cwd": directory, "model": "gpt-5.6-luna",
            })
            chat = app.store.chat_public()

            self.assertEqual(technical["requested_model"], "gpt-5.6-luna")
            self.assertEqual(technical["context_usage"]["max_tokens"], 256_000)
            self.assertEqual(
                app.store.get(technical["id"])["output_reservation_tokens"],
                25_600,
            )
            self.assertEqual(
                technical["context_usage"]["breakdown"]["output_reservation"], 0,
            )
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
                "percent", "estimated", "basis", "transcript_tokens", "breakdown",
            })
            self.assertTrue(public["context_usage"]["estimated"])
            self.assertEqual(public["context_usage"]["basis"], "live_next_request")

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

    def test_new_work_session_resets_context_usage_to_zero(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ChatStore(Path(directory) / "chats.json")
            old = store.create(
                Path(directory), "codex", context_limit_tokens=128_000,
                context_max_tokens=128_000, context_overhead_tokens=2_500,
                output_reservation_tokens=12_800,
            )
            with store.lock:
                stored = store.get(old["id"])
                stored["messages"].append({"role": "user", "content": "previous work"})
                stored["live_context_usage"] = {
                    "input_tokens": 60_000, "output_tokens": 4_000,
                }
            self.assertGreater(store.public(stored)["context_usage"]["used_tokens"], 0)

            fresh = store.create(
                Path(directory), "codex", context_limit_tokens=128_000,
                context_max_tokens=128_000, context_overhead_tokens=2_500,
                output_reservation_tokens=12_800,
            )

        usage = fresh["context_usage"]
        self.assertEqual(usage["used_tokens"], 0)
        self.assertEqual(usage["percent"], 0)
        self.assertEqual(usage["limit_tokens"], 128_000)
        self.assertEqual(set(usage["breakdown"].values()), {0})


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

    def test_latest_work_model_and_context_allowance_seed_new_sessions(self):
        with tempfile.TemporaryDirectory() as directory:
            app = _app(directory)
            first = app.create_chat({"provider": "codex", "cwd": directory})
            app.set_provider_preferences({"provider": "codex", "model": "gpt-context-test"})
            app.set_context_window(first["id"], {"percent": 50})

            second = app.create_chat({"provider": "codex", "cwd": directory})
            self.assertEqual(second["requested_model"], "gpt-context-test")
            self.assertEqual(second["context_usage"]["allowance_percent"], 50)

            reloaded = PilferedParrotApp(app.config, Path(directory))
            third = reloaded.create_chat({"provider": "codex", "cwd": directory})
            self.assertEqual(third["requested_model"], "gpt-context-test")
            self.assertEqual(third["context_usage"]["allowance_percent"], 50)

    def test_latest_chat_model_and_context_allowance_seed_reset(self):
        with tempfile.TemporaryDirectory() as directory:
            app = _app(directory)
            app.set_chat_model({"model": "gpt-5.6-luna"})
            app.set_chat_context_window({"percent": 50})

            reset = app.reset_chat()
            self.assertEqual(reset["chat"]["model"], "gpt-5.6-luna")
            self.assertEqual(reset["chat"]["context_usage"]["allowance_percent"], 50)

            reloaded = PilferedParrotApp(app.config, Path(directory))
            restarted = reloaded.reset_chat()
            self.assertEqual(restarted["chat"]["model"], "gpt-5.6-luna")
            self.assertEqual(restarted["chat"]["context_usage"]["allowance_percent"], 50)

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
            self.assertIn("gpt-5.6-terra", app.state("chat")["chat_model_choices"])
            self.assertIn("gpt-5.6-luna", app.state("chat")["chat_model_choices"])
            with self.assertRaisesRegex(ValueError, "new chat"):
                app.send_chat_message({"content": "switch", "model": "gpt-5.6-terra"})

            reset = app.reset_chat()
            self.assertEqual(reset["chat"]["model"], "gpt-5.6-luna")
            self.assertEqual(reset["chat_history"][0]["model"], "gpt-5.6-luna")
            with self.assertRaisesRegex(ValueError, "model must be a string"):
                app.send_chat_message({"content": "invalid", "model": 5})

    def test_chat_dispatch_uses_inherited_provider_family_and_model_context(self):
        """Chat must dispatch through the provider selected by its source window."""
        with tempfile.TemporaryDirectory() as directory:
            app = _app(directory)
            calls = []

            def dispatch(provider, prompt, cwd, conversation, config, _cancel_event):
                calls.append((provider, prompt, conversation.provider, config))
                return RunResult("Claude reply", 0, session_id="claude-session")

            with patch("pilferedparrot.web.capture_dispatch", side_effect=dispatch):
                app.send_chat_message({
                    "content": "hello", "provider": "claude", "model": "claude-sonnet-5",
                })
                _wait_for_idle(app)

            self.assertEqual(calls[0][0:3], ("claude", "hello", "claude"))
            self.assertEqual(calls[0][3]["claude"]["model"], "claude-sonnet-5")
            self.assertEqual(calls[0][3]["claude"]["permission_mode"], "plan")
            chat = app.store.chat_public()
            self.assertEqual(chat["provider"], "claude")
            self.assertEqual(chat["model"], "claude-sonnet-5")
            self.assertEqual(chat["context_usage"]["max_tokens"], 200_000)

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
                app.state("chat")["model_context_windows"]["codex"]["gpt-5.6-terra"],
                128_000,
            )
            self.assertEqual(
                app.state("chat")["model_context_windows"]["codex"]["gpt-5.6-luna"],
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

    def test_completed_technical_message_uses_live_request_not_aggregate_usage(self):
        with tempfile.TemporaryDirectory() as directory:
            app = _app(directory)
            chat = app.create_chat({"provider": "codex", "cwd": directory})
            results = iter([
                RunResult(
                    "first", 0, session_id="s1", input_tokens=80, output_tokens=20,
                    live_input_tokens=60, live_output_tokens=20,
                ),
                RunResult(
                    "second", 0, session_id="s1", input_tokens=1_800, output_tokens=200,
                    live_input_tokens=900, live_output_tokens=100,
                ),
            ])
            with patch("pilferedparrot.web.capture_dispatch", side_effect=lambda *a: next(results)):
                app.send_message(chat["id"], {"content": "one"})
                _wait_for_idle(app, chat["id"])
                app.send_message(chat["id"], {"content": "two"})
                _wait_for_idle(app, chat["id"])
            public = next(item for item in app.store.list_public() if item["id"] == chat["id"])
            self.assertEqual(public["context_usage"]["used_tokens"], 1_000)
            self.assertTrue(public["context_usage"]["estimated"])
            self.assertEqual(public["context_usage"]["basis"], "live_next_request")
            self.assertEqual(public["context_usage"]["transcript_tokens"], 5)
            self.assertNotIn("context_used_tokens", app.store.get(chat["id"]))
            self.assertEqual(app.store.get(chat["id"])["last_turn_usage"], {
                "input_tokens": 1_800, "output_tokens": 200,
            })
            reloaded = ChatStore(Path(directory) / "chats.json")
            self.assertEqual(reloaded.public(reloaded.get(chat["id"]))["context_usage"],
                             public["context_usage"])

    def test_completed_chat_message_uses_live_request_not_aggregate_usage(self):
        with tempfile.TemporaryDirectory() as directory:
            app = _app(directory)
            result = RunResult("Hello.", 0,
                               session_id="p1", input_tokens=900, output_tokens=150,
                               live_input_tokens=500, live_output_tokens=100)
            with patch("pilferedparrot.web.capture_dispatch", return_value=result):
                app.send_chat_message({"content": "hello"})
                _wait_for_idle(app)
            public = app.store.chat_public()
            self.assertEqual(public["context_usage"]["used_tokens"], 600)
            self.assertTrue(public["context_usage"]["estimated"])
            self.assertEqual(public["context_usage"]["basis"], "live_next_request")
            self.assertEqual(public["context_usage"]["transcript_tokens"], 3)
            self.assertEqual(app.store.data["chat"]["last_turn_usage"], {
                "input_tokens": 900, "output_tokens": 150,
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
        self.assertGreater(app.store.chat_public()["context_usage"]["used_tokens"], 1)


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

    def test_primary_display_is_full_live_context_and_pie_shares_its_total(self):
        for source in (self.js, self.chat_js):
            self.assertRegex(source, r"Estimated context used")
            self.assertIn("used_tokens", source)
            self.assertIn("limit_tokens", source)
            self.assertRegex(source, r"(?:used_tokens|used)\b[^\n]{0,180}(?:limit_tokens|limit)\b")

        self.assertRegex(
            self.js + self.chat_js,
            r"(?i)(next request|live context).*(?:compaction|injected|prompt)",
        )
        self.assertNotIn("Visible transcript estimate", self.js + self.chat_js)
        self.assertIn("Response capacity reserved (not used)", self.js + self.chat_js)


if __name__ == "__main__":
    unittest.main()
