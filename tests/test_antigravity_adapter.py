import json
import os
import threading
import unittest
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import patch

from pilferedparrot.antigravity import AntigravityAdapter
from pilferedparrot.dispatch import RunCancelled
from pilferedparrot.model import Conversation


def config():
    return {"antigravity": {"command": "agy", "mode": "accept-edits"}}


def stream(lines, *, returncode=0, stderr=""):
    seen = {}

    def fake(command, prompt, cwd, **kwargs):
        seen["command"] = command
        seen["prompt"] = prompt
        for line in lines:
            kwargs["stdout_line"](json.dumps(line) + "\n")
        return CompletedProcess(command, returncode, "", stderr)

    return fake, seen


class AntigravityAdapterTests(unittest.TestCase):
    def setUp(self):
        # Keep these protocol tests independent of whether an `agy` binary happens
        # to be installed on the host running the suite.  provider_command is the
        # dispatch boundary used by both command assembly and model discovery.
        self._resolve_command = patch(
            "pilferedparrot.dispatch.resolve_command",
            return_value="/synthetic/bin/agy",
        )
        self._resolve_command.start()
        self.addCleanup(self._resolve_command.stop)

    def test_nested_step_updates_and_resume_are_normalized(self):
        fake, seen = stream([
            {"event": "init", "conversation_id": "conv-1"},
            {"event": "step_update", "step_update": {
                "step_type": "agent_response", "text_delta": "Hi",
            }},
            {"event": "result", "result": {
                "status": "SUCCESS", "response": "Hello", "conversation_id": "conv-1",
            }},
        ])
        adapter = AntigravityAdapter("antigravity", config())
        conversation = Conversation()
        updates = []
        with patch("pilferedparrot.dispatch._stream_process", side_effect=fake):
            result = adapter.run("secret prompt", Path.cwd(), conversation, on_progress=updates.append)
        self.assertEqual(result.text, "Hello")
        self.assertEqual(conversation.provider_session_id, "conv-1")
        self.assertNotIn("secret prompt", seen["command"])
        self.assertEqual(json.loads(seen["prompt"])["message"]["content"], "secret prompt")
        self.assertIn("Hi", [update.text for update in updates])

        second_fake, second_seen = stream([{"event": "result", "result": {
            "status": "SUCCESS", "response": "Again", "conversation_id": "conv-1",
        }}])
        with patch("pilferedparrot.dispatch._stream_process", side_effect=second_fake):
            adapter.run("next", Path.cwd(), conversation)
        index = second_seen["command"].index("--conversation")
        self.assertEqual(second_seen["command"][index + 1], "conv-1")

    def test_tool_updates_and_errors_are_redacted(self):
        secret, variable = "antigravity-test-secret", "PILFEREDPARROT_ANTIGRAVITY_KEY"
        os.environ[variable] = secret
        self.addCleanup(os.environ.pop, variable, None)
        settings = config()
        settings["antigravity"]["api_key_env"] = variable
        fake, _ = stream([
            {"event": "step_update", "step_update": {
                "step_type": "tool", "tool_name": "shell", "tool_info": {"argument": secret},
            }},
            {"event": "step_update", "step_update": {
                "step_type": "tool", "tool_name": "shell", "state": "DONE",
            }},
            {"event": "result", "result": {
                "status": "PARTIAL_FAILURE", "message": "tool failed " + secret,
                "conversation_id": "conv-1",
            }},
        ])
        updates = []
        with patch("pilferedparrot.dispatch._stream_process", side_effect=fake):
            result = AntigravityAdapter("antigravity", settings).run(
                "p", Path.cwd(), Conversation(), on_progress=updates.append,
            )
        self.assertEqual(result.exit_code, 1)
        self.assertIn("tool failed [redacted]", result.error)
        self.assertTrue(any(item.kind == "tool" and "shell" in item.text for item in updates))
        self.assertTrue(any(item.kind == "tool_result" for item in updates))
        self.assertNotIn(secret, repr(updates))

    def test_nonzero_cli_errors_precede_missing_result_and_old_flags_are_actionable(self):
        fake, _ = stream([{"event": "result", "result": {
            "status": "ERROR", "conversation_id": "", "error": "invalid model selection",
        }}], returncode=1)
        with patch("pilferedparrot.dispatch._stream_process", side_effect=fake):
            result = AntigravityAdapter("antigravity", config()).run("p", Path.cwd(), Conversation())
        self.assertIn("invalid model selection", result.error)

        fake, _ = stream([], returncode=1, stderr="authentication failed")
        with patch("pilferedparrot.dispatch._stream_process", side_effect=fake):
            result = AntigravityAdapter("antigravity", config()).run("p", Path.cwd(), Conversation())
        self.assertIn("authentication failed", result.error)

        fake, _ = stream([], returncode=1, stderr="flag provided but not defined: --input-format")
        with patch("pilferedparrot.dispatch._stream_process", side_effect=fake):
            result = AntigravityAdapter("antigravity", config()).run("p", Path.cwd(), Conversation())
        self.assertIn("update agy", result.error)

    def test_rejects_duplicate_and_wrong_session_results(self):
        fake, _ = stream([
            {"event": "init", "conversation_id": "one"},
            {"event": "result", "result": {
                "status": "SUCCESS", "response": "ok", "conversation_id": "two",
            }},
        ])
        conversation = Conversation()
        with patch("pilferedparrot.dispatch._stream_process", side_effect=fake):
            result = AntigravityAdapter("antigravity", config()).run("p", Path.cwd(), conversation)
        self.assertIn("different conversation", result.error)
        self.assertIsNone(conversation.provider_session_id)

        fake, _ = stream([
            {"event": "result", "result": {"status": "SUCCESS", "response": "one", "conversation_id": "one"}},
            {"event": "result", "result": {"status": "SUCCESS", "response": "two", "conversation_id": "one"}},
        ])
        with patch("pilferedparrot.dispatch._stream_process", side_effect=fake):
            result = AntigravityAdapter("antigravity", config()).run("p", Path.cwd(), Conversation())
        self.assertIn("more than one", result.error)

    def test_read_only_is_rejected_without_launching_a_process(self):
        adapter = AntigravityAdapter("antigravity", {"antigravity": {
            "command": "agy", "read_only": True,
        }})
        with patch("pilferedparrot.dispatch._stream_process") as launch:
            with self.assertRaisesRegex(ValueError, "read-only Chat is not yet supported"):
                adapter.run("p", Path.cwd(), Conversation())
        launch.assert_not_called()

    def test_work_plan_mode_keeps_slash_commands_available(self):
        adapter = AntigravityAdapter("antigravity", {"antigravity": {
            "command": "agy", "mode": "plan",
        }})
        command = adapter._command(Conversation())
        self.assertEqual(command[command.index("--mode") + 1], "plan")
        self.assertNotIn("--disable-slash-commands", command)

    def test_malformed_stdout_is_not_emitted_as_progress(self):
        def fake(command, prompt, cwd, **kwargs):
            kwargs["stdout_line"]("credential-like raw stdout\n")
            return CompletedProcess(command, 0, "", "")

        updates = []
        with patch("pilferedparrot.dispatch._stream_process", side_effect=fake):
            result = AntigravityAdapter("antigravity", config()).run(
                "p", Path.cwd(), Conversation(), on_progress=updates.append,
            )
        self.assertIn("malformed JSON", result.error)
        self.assertEqual(updates, [])

    def test_cancel_cleans_up_registration(self):
        adapter = AntigravityAdapter("antigravity", config())
        conversation, started, outcome = Conversation(), threading.Event(), []

        def cancellable(*_args, cancel_event, **_kwargs):
            started.set()
            cancel_event.wait(1)
            raise RunCancelled("request cancelled")

        def run():
            try:
                adapter.run("p", Path.cwd(), conversation)
            except RunCancelled as error:
                outcome.append(error)

        with patch("pilferedparrot.dispatch._stream_process", side_effect=cancellable):
            worker = threading.Thread(target=run)
            worker.start()
            self.assertTrue(started.wait(1))
            adapter.cancel(conversation)
            worker.join(1)
        self.assertFalse(worker.is_alive())
        self.assertIsInstance(outcome[0], RunCancelled)
        self.assertNotIn(id(conversation), adapter._cancellations)

    def test_models_deduplicates_and_bounds_output(self):
        def fake(command, prompt, cwd, **kwargs):
            return CompletedProcess(command, 0,
                "ID\tLabel\ngemini-x\tGemini X\ngemini-x\tDuplicate\n" +
                ("x" * 201) + "\tToo long\n", "")

        with patch("pilferedparrot.dispatch._capture_process", side_effect=fake):
            models = AntigravityAdapter("antigravity", config()).models()
        self.assertEqual(models, [{"value": "gemini-x", "label": "Gemini X"}])


if __name__ == "__main__":
    unittest.main()
