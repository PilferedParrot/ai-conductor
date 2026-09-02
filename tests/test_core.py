import errno
import io
import json
import os
import subprocess
import tempfile
import threading
import time
import unittest
from http import HTTPStatus
from pathlib import Path
from unittest.mock import MagicMock, patch

from pilferedparrot.budgets import codex_budget_from_response, read_codex_budget
from pilferedparrot.config import effective_model, load_config, model_catalog, resolve_command
from pilferedparrot.dispatch import (
    RunCancelled, RunResult, _capture_process, _codex_command, capture_codex,
)
from pilferedparrot.model import (
    PROVIDERS, STATUS_CLI_MISSING, STATUS_SIGNED_OUT, Conversation, ProviderBudget,
)
from pilferedparrot.qwen import ensure_qwen, run_qwen_agent
from pilferedparrot.qwen_tools import QwenToolbox
from pilferedparrot.web import (
    API_GENERATION, ASSET_VERSION, RUNTIME_VERSION, ChatStore, PilferedParrotApp,
    _asset_fingerprint, _browser_url, _notify_window_closed, _pilferedparrot_status,
    _runtime_fingerprint,
    _terminate_stale_pilferedparrot, make_handler, serve,
)


def _web_config(directory):
    root = Path(directory)
    config = load_config(root / "missing-config.json")
    config["web"]["chat_store"] = str(root / "chats.json")
    config["ledger"] = str(root / "runs.jsonl")
    return config


class BudgetTests(unittest.TestCase):
    def test_only_supported_providers_remain(self):
        self.assertEqual(PROVIDERS, ("qwen", "codex"))
        self.assertNotIn("claude", load_config())
        self.assertNotIn("routing", load_config())

    def test_codex_app_server_payload_has_truthful_window_labels(self):
        budget = codex_budget_from_response({
            "rateLimits": {
                "primary": {"usedPercent": 12, "windowDurationMins": 300, "resetsAt": 1234},
                "secondary": {"usedPercent": 2, "windowDurationMins": 10080, "resetsAt": 5678},
            }
        })
        self.assertEqual([window.label for window in budget.windows], [
            "5-hour included usage", "Weekly included usage",
        ])
        self.assertEqual([window.remaining_percent for window in budget.windows], [88, 98])
        self.assertNotIn("subscription", json.dumps(budget.as_dict()).lower())

    def test_percentage_strings_are_normalized_and_clamped(self):
        normalized = codex_budget_from_response({
            "rateLimits": {"primary": {
                "usedPercent": "21.5", "windowDurationMins": 300,
            }},
        })
        over = codex_budget_from_response({
            "rateLimits": {"primary": {"usedPercent": 140, "windowDurationMins": 300}},
        })
        under = codex_budget_from_response({
            "rateLimits": {"primary": {"usedPercent": -5, "windowDurationMins": 300}},
        })
        self.assertEqual(normalized.window.remaining_percent, 78.5)
        self.assertEqual(over.window.remaining_percent, 0)
        self.assertEqual(under.window.remaining_percent, 100)

    def test_codex_uses_more_constrained_window_as_summary(self):
        budget = codex_budget_from_response({
            "rateLimits": {
                "primary": {"usedPercent": 20, "windowDurationMins": 300},
                "secondary": {"usedPercent": 84, "windowDurationMins": 10080},
            }
        })
        self.assertEqual(budget.window.label, "Weekly included usage")
        self.assertEqual(budget.window.remaining_percent, 16)

    def test_codex_exposes_named_rate_limit_buckets(self):
        budget = codex_budget_from_response({
            "rateLimitsByLimitId": {
                "codex": {
                    "limitName": "Codex",
                    "primary": {"usedPercent": 25, "windowDurationMins": 300},
                },
                "codex_luna": {
                    "limitName": "Codex Luna",
                    "primary": {"usedPercent": 9, "windowDurationMins": 10080},
                },
            },
        })
        self.assertEqual([window.label for window in budget.windows], [
            "Codex · 5-hour included usage",
            "Codex Luna · Weekly included usage",
        ])


class CommandResolutionTests(unittest.TestCase):
    def test_committed_defaults_have_no_machine_specific_start_command(self):
        config = load_config(Path("/definitely/missing/config.json"))
        self.assertFalse(config["qwen"]["auto_start"])
        self.assertEqual(config["qwen"]["start_command"], [])
        self.assertEqual(config["web"]["default_provider"], "codex")

    def test_codex_model_catalog_uses_cli_default_and_visible_cached_models(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / "config.toml"
            cache_path = root / "models.json"
            config_path.write_text('model = "gpt-current"\n')
            cache_path.write_text(json.dumps({"models": [
                {"slug": "gpt-current", "display_name": "GPT Current", "visibility": "list"},
                {"slug": "gpt-other", "display_name": "GPT Other", "visibility": "list"},
                {"slug": "gpt-hidden", "display_name": "Hidden", "visibility": "hide"},
            ]}))
            config = load_config(root / "missing.json")
            config["codex"]["config_path"] = str(config_path)
            config["codex"]["models_cache"] = str(cache_path)
            self.assertEqual(effective_model(config, "codex"), "gpt-current")
            catalog = model_catalog(config)
        self.assertEqual(set(catalog), {"qwen", "codex"})
        self.assertEqual(catalog["codex"]["options"], [
            {"value": "gpt-current", "label": "GPT Current"},
            {"value": "gpt-other", "label": "GPT Other"},
        ])

    @patch("pilferedparrot.qwen.qwen_available", return_value=False)
    def test_qwen_auto_start_requires_explicit_command(self, _available):
        config = load_config()
        config["qwen"]["auto_start"] = True
        config["qwen"]["start_command"] = []
        with self.assertRaisesRegex(RuntimeError, "start_command is empty"):
            ensure_qwen(config, notify=lambda _message: None)

    def test_found_in_extra_search_path(self):
        with tempfile.TemporaryDirectory() as directory:
            executable = Path(directory) / "codex"
            executable.write_text("#!/bin/sh\n")
            executable.chmod(0o700)
            config = load_config()
            config["cli_search_paths"] = [directory]
            with patch("pilferedparrot.config.shutil.which", return_value=None):
                self.assertEqual(resolve_command(config, "codex"), str(executable))

    def test_explicit_path_never_falls_back_to_path(self):
        config = load_config()
        config["codex"]["command"] = "/missing/explicit/codex"
        with patch("pilferedparrot.config.shutil.which", return_value="/usr/bin/codex") as which:
            self.assertIsNone(resolve_command(config, "codex"))
        which.assert_not_called()

    @patch("pilferedparrot.budgets.resolve_command", return_value=None)
    def test_missing_codex_cli_is_not_called_offline(self, _resolve):
        budget = read_codex_budget(load_config())
        self.assertFalse(budget.available)
        self.assertEqual(budget.status, STATUS_CLI_MISSING)
        self.assertNotIn("offline", budget.note.lower())

    @patch("pilferedparrot.budgets.resolve_command", return_value="/usr/bin/codex")
    @patch("pilferedparrot.budgets.subprocess.run")
    def test_signed_out_codex_is_distinct_from_missing(self, run, _resolve):
        run.return_value.returncode = 1
        run.return_value.stdout = "Not logged in"
        run.return_value.stderr = ""
        budget = read_codex_budget(load_config())
        self.assertFalse(budget.available)
        self.assertEqual(budget.status, STATUS_SIGNED_OUT)


class QwenToolTests(unittest.TestCase):
    def _config(self):
        config = load_config()["qwen"]
        config["shell_network"] = False
        return config

    def test_file_edit_returns_diff(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sample.txt"
            path.write_text("before\n")
            toolbox = QwenToolbox(Path(directory), self._config())
            result = toolbox.execute("edit_file", {
                "path": "sample.txt", "old_text": "before", "new_text": "after",
            })
            self.assertIn("-before", result)
            self.assertIn("+after", result)

    def test_file_tool_rejects_workspace_escape(self):
        with tempfile.TemporaryDirectory() as directory:
            toolbox = QwenToolbox(Path(directory), self._config())
            with self.assertRaises(PermissionError):
                toolbox.execute("read_file", {"path": "../secret"})

    def test_entire_home_workspace_requires_explicit_opt_in(self):
        with self.assertRaisesRegex(ValueError, "allow_home_workspace"):
            QwenToolbox(Path.home(), self._config())
        config = self._config()
        config["allow_home_workspace"] = True
        self.assertEqual(QwenToolbox(Path.home(), config).cwd, Path.home().resolve())

    def test_file_tool_rejects_symlink_escape(self):
        with tempfile.TemporaryDirectory() as directory, \
             tempfile.TemporaryDirectory() as outside:
            Path(directory, "escape").symlink_to(outside, target_is_directory=True)
            toolbox = QwenToolbox(Path(directory), self._config())
            with self.assertRaises(PermissionError):
                toolbox.execute("write_file", {"path": "escape/payload", "content": "blocked"})
            self.assertFalse(Path(outside, "payload").exists())

    @unittest.skipUnless(Path("/usr/bin/bwrap").exists(), "bubblewrap is not installed")
    def test_shell_cannot_read_sibling_home_data(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            workspace.mkdir()
            secret = root / "secret.txt"
            secret.write_text("do-not-leak")
            with patch("pilferedparrot.qwen_tools.Path.home", return_value=root):
                toolbox = QwenToolbox(workspace, self._config())
                result = toolbox.execute("shell", {"command": f"head -c 20 {secret}"})
            self.assertNotIn("do-not-leak", result)

    @patch("pilferedparrot.qwen._chat_completion")
    def test_agent_executes_tool_call_and_keeps_protocol_messages(self, complete):
        complete.side_effect = [
            {"content": "Checking.", "tool_calls": [{
                "id": "call-1", "type": "function",
                "function": {"name": "read_file", "arguments": '{"path":"sample.txt"}'},
            }]},
            {"content": "Done."},
        ]
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "sample.txt").write_text("hello\n")
            messages = []
            result = run_qwen_agent("read it", messages, load_config(), Path(directory))
        self.assertEqual(result, "Done.")
        self.assertEqual([message["role"] for message in messages], [
            "user", "assistant", "tool", "assistant",
        ])


class CodexDispatchTests(unittest.TestCase):
    @patch("pilferedparrot.dispatch.provider_command", return_value="codex")
    def test_fresh_command_sets_primary_and_additional_write_roots(self, _command):
        with tempfile.TemporaryDirectory() as workspace, \
             tempfile.TemporaryDirectory() as additional:
            config = load_config(Path(workspace) / "missing.json")
            config["codex"]["additional_write_dirs"] = [additional]
            command = _codex_command(Conversation(), config, Path(workspace))
        self.assertEqual(command[:2], ["codex", "exec"])
        self.assertEqual(command[command.index("--cd") + 1], workspace)
        self.assertEqual(command[command.index("--sandbox") + 1], "workspace-write")
        self.assertEqual(command[command.index("--add-dir") + 1], additional)
        self.assertNotIn("resume", command)

    @patch("pilferedparrot.dispatch.provider_command", return_value="codex")
    def test_resumed_command_reasserts_workspace_policy_before_resume(self, _command):
        with tempfile.TemporaryDirectory() as workspace, \
             tempfile.TemporaryDirectory() as additional:
            config = load_config(Path(workspace) / "missing.json")
            config["codex"]["additional_write_dirs"] = [additional]
            command = _codex_command(
                Conversation(provider_session_id="thread-1"), config, Path(workspace),
            )
        resume = command.index("resume")
        self.assertLess(command.index("--cd"), resume)
        self.assertLess(command.index("--sandbox"), resume)
        self.assertLess(command.index("--add-dir"), resume)
        self.assertEqual(command[resume:], ["resume", "thread-1", "-"])

    @patch("pilferedparrot.dispatch.provider_command", return_value="codex")
    def test_missing_additional_write_root_fails_before_codex_starts(self, _command):
        config = load_config(Path("/definitely/missing/config.json"))
        config["codex"]["additional_write_dirs"] = ["/definitely/missing/project"]
        with self.assertRaisesRegex(ValueError, "does not exist"):
            _codex_command(Conversation(), config, Path.cwd())

    @patch("pilferedparrot.dispatch.provider_command", return_value="codex")
    def test_invocation_can_override_codex_reasoning_effort(self, _command):
        config = load_config(Path("/definitely/missing/config.json"))
        config["codex"]["reasoning_effort"] = "low"
        command = _codex_command(Conversation(), config, Path.cwd())
        override = command.index("--config")
        self.assertEqual(command[override + 1], 'model_reasoning_effort="low"')

    @patch("pilferedparrot.dispatch.provider_command", return_value="codex")
    def test_invocation_applies_selected_context_window_share(self, _command):
        config = load_config(Path("/definitely/missing/config.json"))
        config["codex"]["context_window_tokens"] = 872_000
        config["codex"]["context_window_percent"] = 50
        command = _codex_command(Conversation(), config, Path.cwd())
        self.assertIn("model_context_window=436000", command)

    @patch("pilferedparrot.dispatch._stream_process")
    @patch("pilferedparrot.dispatch.provider_command", return_value="codex")
    def test_codex_receives_raw_user_prompt_without_workspace_wrapper(self, _command, stream):
        def emit(_argv, prompt, _cwd, **kwargs):
            self.assertEqual(prompt, "do the work")
            kwargs["stdout_line"](json.dumps({"type": "thread.started", "thread_id": "t-1"}) + "\n")
            kwargs["stdout_line"](json.dumps({
                "type": "item.completed", "item": {"type": "agent_message", "text": "Finished."},
            }) + "\n")
            return subprocess.CompletedProcess([], 0, "", "")

        stream.side_effect = emit
        result = capture_codex("do the work", Path.cwd(), __import__(
            "pilferedparrot.model", fromlist=["Conversation"]
        ).Conversation(), load_config(Path("/definitely/missing/config.json")))
        self.assertEqual(result.text, "Finished.")
        self.assertEqual(result.session_id, "t-1")

    @patch("pilferedparrot.dispatch._stream_process")
    @patch("pilferedparrot.dispatch.provider_command", return_value="codex")
    def test_codex_keeps_only_final_agent_message(self, _command, stream):
        def emit(_argv, _prompt, _cwd, **kwargs):
            for text in ("intermediate", "final"):
                kwargs["stdout_line"](json.dumps({
                    "type": "item.completed", "item": {"type": "agent_message", "text": text},
                }) + "\n")
            return subprocess.CompletedProcess([], 0, "", "")

        stream.side_effect = emit
        from pilferedparrot.model import Conversation
        result = capture_codex(
            "work", Path.cwd(), Conversation(),
            load_config(Path("/definitely/missing/config.json")),
        )
        self.assertEqual(result.text, "final")


class WebStoreTests(unittest.TestCase):
    def _wait_for_chat(self, app, chat_id, timeout=2):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with app.store.lock:
                chat = app.store.public(app.store.get(chat_id))
            if not any(message.get("pending") for message in chat["messages"]):
                return chat
            time.sleep(0.01)
        self.fail("chat did not finish")

    def _wait_for_chat_reply(self, app, timeout=2):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            chat_thread = app.store.chat_public()
            if not chat_thread["pending"]:
                return chat_thread
            time.sleep(0.01)
        self.fail("Chat did not finish")

    def test_chat_store_persists_without_internal_qwen_state(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "chats.json"
            store = ChatStore(path)
            public = store.create(Path(directory), "qwen")
            with store.lock:
                stored = store.get(public["id"])
                stored["qwen_messages"] = [{"role": "user", "content": "secret internal"}]
                store.save()
            self.assertNotIn("qwen_messages", store.list_public()[0])
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_legacy_routed_chat_becomes_direct_codex_without_erasing_history(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "chats.json"
            path.write_text(json.dumps({"chats": [{
                "id": "old", "requested_provider": "auto", "requested_model": "opus",
                "provider": "claude", "messages": [{
                    "role": "assistant", "provider": "claude", "content": "historical",
                }], "updated_at": 1,
            }]}))
            store = ChatStore(path)
            chat = store.list_public()[0]
        self.assertEqual(chat["requested_provider"], "codex")
        self.assertIsNone(chat["requested_model"])
        self.assertEqual(chat["messages"][0]["content"], "historical")

    def test_state_contains_only_direct_provider_catalogs(self):
        with tempfile.TemporaryDirectory() as directory:
            app = PilferedParrotApp(_web_config(directory), Path(directory))
            state = app.state()
        self.assertEqual(set(state["models"]), {"qwen", "codex"})
        self.assertEqual(set(state["model_catalog"]), {"qwen", "codex"})
        self.assertEqual(state["default_provider"], "codex")
        self.assertEqual(state["runtime_version"], RUNTIME_VERSION)
        self.assertEqual(state["chat_model"], "gpt-5.6-terra")
        self.assertEqual(state["chat"]["messages"], [])
        self.assertFalse(state["chat"]["pending"])
        self.assertEqual(state["chat_history"], [])

    def test_new_work_session_uses_session_title(self):
        with tempfile.TemporaryDirectory() as directory:
            app = PilferedParrotApp(_web_config(directory), Path(directory))
            chat = app.create_chat({"provider": "codex", "cwd": directory})
        self.assertEqual(chat["title"], "New work session")

    def test_persisted_legacy_repository_path_is_migrated_without_history_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "Pilfered Parrot"
            (root / "pilferedparrot").mkdir(parents=True)
            path = Path(directory) / "chats.json"
            messages = [{"role": "user", "content": "preserve this"}]
            path.write_text(json.dumps({"chats": [{
                "id": "legacy", "cwd": str(root.parent / "ai-conductor"),
                "requested_provider": "codex", "messages": messages,
                "updated_at": 1,
            }]}))
            with patch("pilferedparrot.web.RUNTIME_ROOT", root / "pilferedparrot"):
                app = PilferedParrotApp(_web_config(directory), root)
                migrated = app.store.get("legacy")
                created = app.create_chat({
                    "provider": "codex", "cwd": str(root.parent / "ai-conductor"),
                })
                persisted = json.loads(path.read_text())["chats"][0]

            self.assertEqual(migrated["cwd"], str(root.resolve()))
            self.assertEqual(migrated["messages"], messages)
            self.assertEqual(persisted["cwd"], str(root.resolve()))
            self.assertEqual(persisted["messages"], messages)
            self.assertEqual(created["cwd"], str(root.resolve()))

    def test_new_work_session_preserves_valid_current_project_path(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "Pilfered Parrot"
            (root / "pilferedparrot").mkdir(parents=True)
            project = root / "selected-project"
            project.mkdir()
            app = PilferedParrotApp(_web_config(directory), root)
            created = app.create_chat({"provider": "codex", "cwd": str(project)})
        self.assertEqual(created["cwd"], str(project.resolve()))

    def test_new_work_session_rejects_unrelated_nonexistent_project_path(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "Pilfered Parrot"
            (root / "pilferedparrot").mkdir(parents=True)
            app = PilferedParrotApp(_web_config(directory), root)
            missing = Path(directory) / "unrelated-missing-project"
            with self.assertRaisesRegex(ValueError, "project folder does not exist"):
                app.create_chat({"provider": "codex", "cwd": str(missing)})

    def test_new_technical_work_session_preserves_chat(self):
        with tempfile.TemporaryDirectory() as directory:
            app = PilferedParrotApp(_web_config(directory), Path(directory))
            with app.store.lock:
                chat_thread = app.store.data["chat"]
                chat_thread["messages"] = [{"role": "user", "content": "keep this chat"}]
                app.store.save()
                chat_id = chat_thread["id"]

            work = app.create_chat({"provider": "codex", "cwd": directory})

            self.assertEqual(app.store.data["chat"]["id"], chat_id)
            self.assertEqual(
                app.store.chat_public()["messages"][0]["content"], "keep this chat",
            )
            self.assertEqual(app.store.get(work["id"])["messages"], [])

    def test_chat_relays_raw_content_to_read_only_terra_and_persists_separate_session(self):
        with tempfile.TemporaryDirectory() as directory:
            app = PilferedParrotApp(_web_config(directory), Path(directory))
            chat = app.create_chat({"provider": "codex", "cwd": directory})
            seen = []

            def dispatch(provider, prompt, cwd, conversation, config, _cancel):
                seen.append((provider, prompt, cwd, conversation.provider_session_id, config))
                return RunResult("I can keep an eye on that.", 0, "terra-thread")

            with patch("pilferedparrot.web.capture_dispatch", side_effect=dispatch):
                pending = app.send_chat_message({"content": "What is technical doing?"})
                self.assertTrue(pending["pending"])
                chat_thread = self._wait_for_chat_reply(app)

            self.assertEqual(chat_thread["messages"][-1]["content"], "I can keep an eye on that.")
            self.assertEqual(seen[0][0], "codex")
            self.assertEqual(seen[0][1], "What is technical doing?")
            self.assertNotIn("TECHNICAL_STATE", seen[0][1])
            self.assertEqual(seen[0][2], Path(directory))
            self.assertIsNone(seen[0][3])
            self.assertEqual(seen[0][4]["codex"]["model"], "gpt-5.6-terra")
            self.assertEqual(seen[0][4]["codex"]["reasoning_effort"], "low")
            self.assertEqual(seen[0][4]["codex"]["sandbox"], "read-only")
            self.assertEqual(seen[0][4]["codex"]["additional_write_dirs"], [])

            reloaded = ChatStore(Path(directory) / "chats.json")
            self.assertNotIn("provider_session_id", reloaded.chat_public())
            self.assertEqual(reloaded.chat_public()["messages"][-1]["model"], "gpt-5.6-terra")
            self.assertEqual(reloaded.data["chat"]["provider_session_id"], "terra-thread")
            self.assertEqual(reloaded.get(chat["id"])["messages"], [])

    def test_technical_and_chat_histories_remain_separate_across_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            config = _web_config(directory)
            app = PilferedParrotApp(config, Path(directory))
            chat = app.create_chat({"provider": "codex", "cwd": directory})
            with app.store.lock:
                app.store.get(chat["id"])["messages"] = [{
                    "id": "technical", "role": "user", "content": "Fix the build",
                }]
                app.store.data["chat"].update({
                    "provider_session_id": "parrot-session",
                    "messages": [{"id": "chat", "role": "user", "content": "Keep me posted"}],
                })
                app.store.save()

            restarted = PilferedParrotApp(config, Path(directory))
            self.assertEqual(
                restarted.store.get(chat["id"])["messages"][0]["content"], "Fix the build",
            )
            self.assertEqual(
                restarted.store.chat_public()["messages"][0]["content"], "Keep me posted",
            )
            self.assertEqual(
                restarted.store.data["chat"]["provider_session_id"], "parrot-session",
            )

    def test_chat_does_not_interpret_control_protocol_or_affect_technical_activity(self):
        with tempfile.TemporaryDirectory() as directory:
            app = PilferedParrotApp(_web_config(directory), Path(directory))
            chat = app.create_chat({"provider": "codex", "cwd": directory})
            answer = (
                "Stopping it now.\nPARROT_RELAY_CONTROL: "
                f'{{"action":"interrupt","chat_id":"{chat["id"]}"}}'
            )
            with patch("pilferedparrot.web.capture_dispatch", return_value=RunResult(
                answer, 0, "terra-thread",
            )), patch.object(app, "cancel_message", return_value=chat) as cancel:
                app.send_chat_message({"content": "Stop technical now"})
                chat_thread = self._wait_for_chat_reply(app)
            cancel.assert_not_called()
            self.assertEqual(chat_thread["messages"][-1]["content"], answer)
            self.assertNotIn("control_action", chat_thread["messages"][-1])

    def test_chat_can_cancel_its_own_response(self):
        with tempfile.TemporaryDirectory() as directory:
            app = PilferedParrotApp(_web_config(directory), Path(directory))

            def wait_for_cancel(_provider, _prompt, _cwd, _conversation, _config, cancel):
                while not cancel.is_set():
                    time.sleep(0.005)
                raise RunCancelled("cancelled")

            with patch("pilferedparrot.web.capture_dispatch", side_effect=wait_for_cancel):
                app.send_chat_message({"content": "Never mind"})
                cancelled = app.cancel_chat()
                self.assertTrue(cancelled["pending"])
                chat_thread = self._wait_for_chat_reply(app)
            self.assertEqual(chat_thread["messages"][-1]["content"], "Stopped.")

    def test_chat_warns_when_context_is_getting_heavy(self):
        with tempfile.TemporaryDirectory() as directory:
            config = _web_config(directory)
            config["web"]["chat_context_warning_chars"] = 10_000
            config["codex"]["context_window_tokens"] = 3_000
            app = PilferedParrotApp(config, Path(directory))
            answer = "x" * 10_000
            with patch("pilferedparrot.web.capture_dispatch", return_value=RunResult(
                answer, 0, "terra-thread",
            )):
                app.send_chat_message({"content": "Keep tracking this"})
                chat_thread = self._wait_for_chat_reply(app)
            self.assertIn("Start a new Chat", chat_thread["messages"][-1]["content"])
            self.assertIn("practical limit", chat_thread["context_warning"])

    def test_reset_chat_archives_history_that_can_be_opened_from_state(self):
        with tempfile.TemporaryDirectory() as directory:
            app = PilferedParrotApp(_web_config(directory), Path(directory))
            with app.store.lock:
                chat_thread = app.store.data["chat"]
                chat_thread["provider_session_id"] = "old-thread"
                chat_thread["messages"] = [{"role": "user", "content": "old"}]
                chat_thread["context_chars"] = 50_000
                app.store.save()
            reset = app.reset_chat()
            self.assertEqual(reset["chat"]["messages"], [])
            self.assertEqual(reset["chat"]["context_chars"], 0)
            self.assertFalse(reset["chat"]["pending"])
            self.assertEqual(app.store.data["chat"]["provider_session_id"], None)
            self.assertEqual(len(reset["chat_history"]), 1)
            archived = reset["chat_history"][0]
            self.assertTrue(archived["archived"])
            self.assertEqual(archived["messages"][0]["content"], "old")
            self.assertNotIn("provider_session_id", archived)

            reloaded = ChatStore(Path(directory) / "chats.json")
            self.assertEqual(
                reloaded.chat_history_public()[0]["messages"][0]["content"], "old",
            )
            self.assertEqual(reloaded.chat_public()["messages"], [])

    def test_technical_conversations_expose_practical_context_status(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ChatStore(Path(directory) / "chats.json", technical_warning_chars=10_000)
            public = store.create(Path(directory), "codex")
            with store.lock:
                store.get(public["id"])["messages"] = [{
                    "role": "user", "content": "x" * 8_000,
                }]
                near_limit = store.public(store.get(public["id"]))
            self.assertEqual(near_limit["context_status"], "near_limit")
            self.assertEqual(near_limit["context_percent"], 80)

    def test_all_browser_mutations_require_loopback_origin_and_csrf(self):
        with tempfile.TemporaryDirectory() as directory:
            app = PilferedParrotApp(_web_config(directory), Path(directory))
            handler_type = make_handler(app)
            handler = object.__new__(handler_type)
            handler.client_address = ("127.0.0.1", 12345)
            handler.headers = {
                "Host": "127.0.0.1:8765",
                "Origin": "http://127.0.0.1:8765",
                "X-PilferedParrot-CSRF": app.csrf_token,
            }
            self.assertTrue(handler._control_allowed())
            handler.headers["X-PilferedParrot-CSRF"] = "wrong"
            self.assertFalse(handler._control_allowed())
            handler.headers.pop("X-PilferedParrot-CSRF")
            handler.headers["X-PilferedParrot-CSRF"] = app.csrf_token
            self.assertTrue(handler._control_allowed())

    def test_handler_snapshots_assets_with_its_api_generation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in (
                "index.html", "app.css", "app.js", "icon.svg",
                "pilferedparrot-icon.png", "company-logo.png", "company-logo-dark.png",
            ):
                (root / name).write_text(f"initial {name}")
            app = PilferedParrotApp(_web_config(directory), root)
            with patch("pilferedparrot.web.ASSET_ROOT", root):
                handler_type = make_handler(app)
            (root / "app.js").write_text("new incompatible JavaScript")
            handler = object.__new__(handler_type)
            handler.send_response = lambda _status: None
            handler.send_header = lambda _name, _value: None
            handler.end_headers = lambda: None
            handler.wfile = io.BytesIO()
            handler._asset("app.js", "text/javascript")
            self.assertEqual(handler.wfile.getvalue(), b"initial app.js")

    def test_handler_serves_separate_work_session_and_chat_controls(self):
        with tempfile.TemporaryDirectory() as directory:
            app = PilferedParrotApp(_web_config(directory), Path(directory))
            handler_type = make_handler(app)
            handler = object.__new__(handler_type)
            headers = {}
            handler.send_response = lambda _status: None
            handler.send_header = lambda name, value: headers.__setitem__(name, value)
            handler.end_headers = lambda: None
            handler.wfile = io.BytesIO()
            handler._asset("index.html", "text/html; charset=utf-8")

        body = handler.wfile.getvalue()
        self.assertIn(b'id="newWorkSession"', body)
        self.assertIn(b"New work session", body)
        self.assertNotIn(b'id="resetChat"', body)
        self.assertNotIn(b"New chat", body)
        self.assertNotIn(b'id="newPilferedParrotChat"', body)
        self.assertNotIn(b'id="newChat"', body)
        self.assertNotIn(b"New conversation", body)
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(headers["X-PilferedParrot-Assets"], ASSET_VERSION)
        self.assertEqual(headers["X-PilferedParrot-Runtime"], RUNTIME_VERSION)

        chat_headers = {}
        handler.wfile = io.BytesIO()
        handler.send_header = lambda name, value: chat_headers.__setitem__(name, value)
        handler._asset("chat.html", "text/html; charset=utf-8")
        chat_body = handler.wfile.getvalue()
        self.assertIn(b'id="resetChat"', chat_body)
        self.assertIn(b"New chat", chat_body)
        self.assertIn(b'id="chatHistoryList"', chat_body)
        self.assertEqual(chat_headers["X-PilferedParrot-Assets"], ASSET_VERSION)

    def test_asset_fingerprint_changes_with_frontend_content(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "index.html").write_text("old control")
            original = _asset_fingerprint(root)
            (root / "index.html").write_text("new Parrot chat")
            self.assertNotEqual(_asset_fingerprint(root), original)

    def test_runtime_fingerprint_changes_with_backend_content(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "web.py").write_text("old runtime")
            original = _runtime_fingerprint(root)
            (root / "web.py").write_text("new runtime")
            self.assertNotEqual(_runtime_fingerprint(root), original)

    def test_listener_is_stale_when_runtime_or_frontend_assets_do_not_match(self):
        response = MagicMock()
        response.__enter__.return_value = response
        response.headers = {"Server": "PilferedParrot/test"}

        def status(payload):
            response.read.return_value = json.dumps(payload).encode("utf-8")
            with patch("pilferedparrot.web.urlopen", return_value=response):
                return _pilferedparrot_status("http://127.0.0.1:8765")

        base = {
            "chats": [], "csrf_token": "token", "api_generation": API_GENERATION,
        }
        self.assertEqual(status(base), "stale")
        self.assertEqual(status({**base, "asset_version": "old-assets"}), "stale")
        current_assets = {**base, "asset_version": ASSET_VERSION}
        self.assertEqual(status(current_assets), "stale")
        self.assertEqual(status({**current_assets, "runtime_version": "old-runtime"}), "stale")
        self.assertEqual(
            status({**current_assets, "runtime_version": RUNTIME_VERSION}), "compatible",
        )

    def test_window_close_stops_only_its_exact_server_instance(self):
        response = MagicMock()
        response.__enter__.return_value = response
        response.status = HTTPStatus.ACCEPTED
        url = _browser_url("http://127.0.0.1:8765", "exact-instance-token")
        with patch("pilferedparrot.web.urlopen", return_value=response) as open_url:
            self.assertTrue(_notify_window_closed(url))
        request = open_url.call_args.args[0]
        self.assertEqual(request.full_url, "http://127.0.0.1:8765/api/shutdown")
        self.assertEqual(
            request.get_header("X-pilferedparrot-csrf"), "exact-instance-token",
        )
        self.assertNotIn("close_token", request.full_url)

    def test_shutdown_route_stops_the_serving_loop(self):
        with tempfile.TemporaryDirectory() as directory:
            app = PilferedParrotApp(_web_config(directory), Path(directory))
            handler_type = make_handler(app)
            handler = object.__new__(handler_type)
            handler.path = "/api/shutdown"
            handler.server = MagicMock()
            handler._control_allowed = lambda: True
            handler._read_json = lambda: {}
            handler._json = MagicMock()
            with patch("pilferedparrot.web.threading.Thread") as thread:
                handler.do_POST()
            handler._json.assert_called_once_with({"ok": True}, HTTPStatus.ACCEPTED)
            self.assertIs(thread.call_args.kwargs["target"], handler.server.shutdown)
            thread.return_value.start.assert_called_once_with()

    def test_main_window_reload_cancels_deferred_close(self):
        with tempfile.TemporaryDirectory() as directory:
            app = PilferedParrotApp(_web_config(directory), Path(directory))
            handler_type = make_handler(app)
            handler = object.__new__(handler_type)
            handler.server = MagicMock()
            handler._control_allowed = lambda: True
            handler._read_json = lambda: {}
            handler._json = MagicMock()
            timer = MagicMock()
            with patch("pilferedparrot.web.threading.Timer", return_value=timer) as factory:
                handler.path = "/api/window/close"
                handler.do_POST()
                handler.path = "/api/window/open"
                handler.do_POST()
            factory.assert_called_once_with(2, handler.server.shutdown)
            timer.start.assert_called_once_with()
            timer.cancel.assert_called_once_with()

    @patch("pilferedparrot.web.threading.Thread")
    @patch("pilferedparrot.web.subprocess.Popen")
    @patch("pilferedparrot.web.tempfile.mkdtemp", return_value="/tmp/pilferedparrot-chat-test")
    @patch("pilferedparrot.web.shutil.which")
    def test_chat_window_uses_an_isolated_normal_browser_profile(
        self, which, _mkdtemp, popen, thread,
    ):
        which.side_effect = lambda command: (
            "/usr/bin/google-chrome-stable" if command == "google-chrome-stable" else None
        )
        process = popen.return_value
        process.poll.return_value = None
        with tempfile.TemporaryDirectory() as directory:
            app = PilferedParrotApp(_web_config(directory), Path(directory))
            result = app.open_chat_window("http://127.0.0.1:8765/chat", {
                "width": 960, "height": 540, "left": 900, "top": 28,
            })
        self.assertEqual(result, {"ok": True, "existing": False})
        command = popen.call_args.args[0]
        self.assertEqual(command[0], "/usr/bin/google-chrome-stable")
        self.assertIn("--user-data-dir=/tmp/pilferedparrot-chat-test", command)
        self.assertIn("--window-size=960,540", command)
        self.assertIn("--window-position=900,28", command)
        self.assertIn("--class=pilferedparrot-chat", command)
        self.assertIn("--app=http://127.0.0.1:8765/chat", command)
        self.assertNotIn("--start-maximized", command)
        thread.return_value.start.assert_called_once_with()

    def test_chat_window_route_uses_the_current_local_server(self):
        with tempfile.TemporaryDirectory() as directory:
            app = PilferedParrotApp(_web_config(directory), Path(directory))
            handler_type = make_handler(app)
            handler = object.__new__(handler_type)
            handler.path = "/api/chat/window"
            handler.headers = {"Host": "127.0.0.1:8765"}
            handler._control_allowed = lambda: True
            payload = {"width": 960, "height": 540, "left": 900, "top": 28}
            handler._read_json = lambda: payload
            handler._json = MagicMock()
            with patch.object(app, "open_chat_window", return_value={"ok": True}) as launch:
                handler.do_POST()
        launch.assert_called_once_with("http://127.0.0.1:8765/chat", payload)
        handler._json.assert_called_once_with({"ok": True})

    @patch("pilferedparrot.web._pilferedparrot_csrf_token", return_value="close-token")
    @patch("pilferedparrot.web.webbrowser.open")
    @patch("pilferedparrot.web._pilferedparrot_status", return_value="compatible")
    def test_gui_launch_opens_existing_pilferedparrot(
        self, running, open_browser, csrf_token,
    ):
        with tempfile.TemporaryDirectory() as directory:
            config = _web_config(directory)
            self.assertEqual(serve(config, Path(directory)), 0)
        running.assert_called_once()
        open_browser.assert_called_once_with(
            f"http://127.0.0.1:8765/?generation={API_GENERATION}&assets={ASSET_VERSION}"
            f"&runtime={RUNTIME_VERSION}#close_token=close-token",
        )
        csrf_token.assert_called_once_with("http://127.0.0.1:8765")

    @patch("pilferedparrot.web._terminate_stale_pilferedparrot")
    @patch("pilferedparrot.web._pilferedparrot_status", return_value="stale")
    @patch("pilferedparrot.web.ThreadingHTTPServer")
    def test_gui_replaces_stale_pilferedparrot_after_generation_check(
        self, server_factory, status, terminate,
    ):
        server = server_factory.return_value
        server.serve_forever.side_effect = KeyboardInterrupt
        with tempfile.TemporaryDirectory() as directory:
            config = _web_config(directory)
            self.assertEqual(serve(config, Path(directory), open_browser=False), 0)
        terminate.assert_called_once_with(
            "http://127.0.0.1:8765", config["web"]["port"],
        )
        server_factory.assert_called_once()

    @patch("pilferedparrot.web._pilferedparrot_status", return_value="unavailable")
    @patch("pilferedparrot.web.subprocess.run")
    @patch("pilferedparrot.web.shutil.which", return_value="/usr/bin/fuser")
    def test_stale_replacement_signals_only_the_exact_listener(
        self, _which, run, _status,
    ):
        _terminate_stale_pilferedparrot("http://localhost:8765", 8765)
        run.assert_called_once_with(
            ["/usr/bin/fuser", "-k", "-INT", "8765/tcp"],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def test_direct_provider_runs_without_budget_or_router_turn(self):
        with tempfile.TemporaryDirectory() as directory:
            app = PilferedParrotApp(_web_config(directory), Path(directory))
            chat = app.create_chat({"provider": "codex", "cwd": directory})
            with patch.object(app, "budgets") as budgets, \
                 patch("pilferedparrot.web.capture_dispatch",
                       return_value=RunResult("done", 0, "codex-thread")) as dispatch:
                app.send_message(chat["id"], {"content": "fix it", "provider": "codex"})
                updated = self._wait_for_chat(app, chat["id"])
            budgets.assert_not_called()
            self.assertEqual(dispatch.call_args.args[0], "codex")
            self.assertEqual(updated["messages"][-1]["content"], "done")

    def test_first_turn_rejects_unapproved_project_mismatch_before_dispatch(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            selected = root / "selected"
            other = root / "game"
            selected.mkdir()
            other.mkdir()
            (other / ".git").mkdir()
            config = _web_config(directory)
            config["codex"]["additional_write_dirs"] = []
            app = PilferedParrotApp(config, selected)
            chat = app.create_chat({"provider": "codex", "cwd": selected})
            with patch("pilferedparrot.web.capture_dispatch") as dispatch, \
                 self.assertRaisesRegex(ValueError, "project mismatch"):
                app.send_message(chat["id"], {
                    "content": f"Implement the fix in {other}.", "provider": "codex",
                })
            dispatch.assert_not_called()
            self.assertEqual(app.store.get(chat["id"])["messages"], [])

    def test_qwen_home_workspace_requires_explicit_opt_in(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "home"
            home.mkdir()
            config = _web_config(directory)
            with patch("pilferedparrot.web.Path.home", return_value=home):
                app = PilferedParrotApp(config, home)
                with self.assertRaisesRegex(ValueError, "allow_home_workspace"):
                    app.create_chat({"provider": "qwen", "cwd": str(home)})
                config["qwen"]["allow_home_workspace"] = True
                allowed = PilferedParrotApp(config, home).create_chat({
                    "provider": "qwen", "cwd": str(home),
                })
            self.assertEqual(allowed["cwd"], str(home.resolve()))

    def test_configured_codex_write_root_allows_external_project_task(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            selected = root / "selected"
            other = root / "game"
            selected.mkdir()
            other.mkdir()
            config = _web_config(directory)
            config["codex"]["additional_write_dirs"] = [str(other)]
            app = PilferedParrotApp(config, selected)
            chat = app.create_chat({"provider": "codex", "cwd": selected})
            with patch("pilferedparrot.web.capture_dispatch",
                       return_value=RunResult("done", 0, "thread")) as dispatch:
                app.send_message(chat["id"], {
                    "content": f"Implement the fix in {other}.", "provider": "codex",
                })
                self._wait_for_chat(app, chat["id"])
            dispatch.assert_called_once()

    def test_provider_progress_is_visible_and_bounded(self):
        with tempfile.TemporaryDirectory() as directory:
            app = PilferedParrotApp(_web_config(directory), Path(directory))
            chat = app.create_chat({"provider": "codex", "cwd": directory})

            def stream(_provider, _prompt, _cwd, _conversation, _config, cancel_event):
                report = getattr(cancel_event, "_pilferedparrot_progress")
                for index in range(110):
                    report("tool", f"command {index}")
                return RunResult("Finished.", 0, "codex-thread")

            with patch("pilferedparrot.web.capture_dispatch", side_effect=stream):
                app.send_message(chat["id"], {"content": "show work", "provider": "codex"})
                updated = self._wait_for_chat(app, chat["id"])
            activity = updated["messages"][-1]["activity"]
            self.assertEqual(len(activity), 100)
            self.assertEqual(activity[0]["content"], "command 10")

    def test_switching_provider_does_not_resume_other_provider_session(self):
        with tempfile.TemporaryDirectory() as directory:
            app = PilferedParrotApp(_web_config(directory), Path(directory))
            chat = app.create_chat({"provider": "codex", "cwd": directory})
            resumed = []

            def dispatch(_provider, _prompt, _cwd, conversation, _config, _cancel):
                resumed.append(conversation.provider_session_id)
                return RunResult("done", 0, f"session-{len(resumed)}")

            with patch("pilferedparrot.web.capture_dispatch", side_effect=dispatch), \
                 patch("pilferedparrot.web.ensure_qwen"):
                app.send_message(chat["id"], {"content": "first", "provider": "codex"})
                self._wait_for_chat(app, chat["id"])
                app.send_message(chat["id"], {"content": "second", "provider": "qwen"})
                self._wait_for_chat(app, chat["id"])
            self.assertEqual(resumed, [None, None])

    def test_changing_models_starts_fresh_provider_session(self):
        with tempfile.TemporaryDirectory() as directory:
            app = PilferedParrotApp(_web_config(directory), Path(directory))
            chat = app.create_chat({"provider": "codex", "cwd": directory})
            resumed = []

            def dispatch(_provider, _prompt, _cwd, conversation, _config, _cancel):
                resumed.append(conversation.provider_session_id)
                return RunResult("done", 0, f"session-{len(resumed)}")

            with patch("pilferedparrot.web.capture_dispatch", side_effect=dispatch):
                app.send_message(chat["id"], {
                    "content": "first", "provider": "codex", "model": "gpt-a",
                })
                self._wait_for_chat(app, chat["id"])
                app.send_message(chat["id"], {
                    "content": "second", "provider": "codex", "model": "gpt-b",
                })
                self._wait_for_chat(app, chat["id"])
            self.assertEqual(resumed, [None, None])

    def test_restart_recovers_persisted_pending_response(self):
        with tempfile.TemporaryDirectory() as directory:
            app = PilferedParrotApp(_web_config(directory), Path(directory))
            chat = app.create_chat({"provider": "codex", "cwd": directory})
            with app.store.lock:
                stored = app.store.get(chat["id"])
                stored["messages"] = [{"id": "pending", "role": "assistant", "pending": True}]
                app.store.save()
            self.assertEqual(app.recover_interrupted(), 1)
            self.assertTrue(app.store.list_public()[0]["messages"][0]["interrupted"])


class DispatchCancellationTests(unittest.TestCase):
    def test_capture_process_can_be_cancelled(self):
        cancel = threading.Event()
        timer = threading.Timer(0.15, cancel.set)
        timer.start()
        try:
            with self.assertRaises(RunCancelled):
                _capture_process(
                    ["/bin/sh", "-c", "sleep 30"], "", Path.cwd(),
                    cancel_event=cancel, timeout_seconds=5,
                )
        finally:
            timer.cancel()


if __name__ == "__main__":
    unittest.main()
