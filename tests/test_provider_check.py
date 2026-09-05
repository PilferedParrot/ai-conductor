"""Offline tests for the opt-in provider smoke checker."""
from __future__ import annotations

import importlib.util
from importlib.machinery import SourceFileLoader
import io
import json
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace

from pilferedparrot.adapters import ProgressEvent, ProviderAdapter
from pilferedparrot.dispatch import RunCancelled, RunResult
from pilferedparrot.model import Conversation


SCRIPT = Path(__file__).parents[1] / "bin" / "check-provider"
SPEC = importlib.util.spec_from_loader(
    "check_provider", SourceFileLoader("check_provider", str(SCRIPT)),
)
assert SPEC and SPEC.loader
check_provider = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(check_provider)


class _GoodAdapter(ProviderAdapter):
    def __init__(self, *, include_tools: bool = True, first_exit: int = 0,
                 second_exit: int = 0, wrong_continuation: bool = False,
                 ignore_cancel: bool = False):
        super().__init__("fake", {})
        self.include_tools = include_tools
        self.first_exit = first_exit
        self.second_exit = second_exit
        self.wrong_continuation = wrong_continuation
        self.ignore_cancel = ignore_cancel
        self.calls: list[Conversation] = []

    def run(self, prompt, cwd, conversation, cancel_event=None, on_progress=None):
        self.calls.append(conversation)
        print("fake adapter chatter")
        if prompt.startswith("Begin a long"):
            if on_progress:
                on_progress(ProgressEvent("status", "starting", "fake"))
            if self.ignore_cancel:
                return RunResult("ignored", 0)
            while cancel_event is None or not cancel_event.is_set():
                time.sleep(0.01)
            raise RunCancelled("request cancelled")
        if len(self.calls) == 1:
            if self.include_tools:
                if on_progress:
                    on_progress(ProgressEvent("tool", "read_file(provider-smoke-marker.txt)", "fake"))
                    on_progress(ProgressEvent("tool", "write_file(verified.txt)", "fake"))
                marker = (cwd / "provider-smoke-marker.txt").read_text(encoding="utf-8")
                (cwd / "verified.txt").write_text(marker, encoding="utf-8")
                conversation.messages.append({"role": "tool", "content": marker})
            return RunResult("verified", self.first_exit)
        marker = ""
        # The checker deliberately removes the files before this call; recover
        # the marker from the prior tool result represented in this fake's history.
        if conversation.messages:
            marker = str(conversation.messages[-1].get("content", ""))
        return RunResult("wrong marker" if self.wrong_continuation else marker, self.second_exit)

    def cancel(self, conversation):
        return None


class _BadAdapter(_GoodAdapter):
    def __init__(self):
        super().__init__(include_tools=False)


class ProviderCheckTests(unittest.TestCase):
    def test_success_checks_tools_containment_continuation_and_cancellation(self):
        with tempfile.TemporaryDirectory() as directory:
            adapter = _GoodAdapter()
            summary = check_provider.run_smoke(adapter, "fake", "model-1", Path(directory))
            self.assertEqual(summary["provider"], "fake")
            self.assertTrue(all(summary["checks"].values()))
            self.assertEqual(len(adapter.calls), 3)
            self.assertFalse((Path(directory) / "provider-smoke-marker.txt").exists())
            self.assertFalse((Path(directory) / "verified.txt").exists())

    def test_failure_reports_missing_tool_progress_or_file(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(RuntimeError, "contained read_file/write_file"):
                check_provider.run_smoke(_BadAdapter(), "fake", "model-1", Path(directory))

    def test_build_config_defaults_are_isolated_and_explicit_config_is_sanitized(self):
        no_config = SimpleNamespace(
            config=None, provider="lmstudio", model="local-model", base_url=None,
            api_key_env=None,
        )
        config, model = check_provider.build_config(no_config)
        self.assertEqual(model, "local-model")
        self.assertEqual(config["lmstudio"]["additional_dirs"], [])
        self.assertEqual(config["lmstudio"]["api_key_env"], "")

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps({"provider_definitions": {
                "fake": {"base_url": "http://example.invalid/v1", "model": "saved",
                          "api_key_env": "TEST_ONLY_KEY", "additional_dirs": [directory]},
            }}), encoding="utf-8")
            explicit = SimpleNamespace(
                config=str(path), provider="fake", model=None,
                base_url="http://override.invalid/v1", api_key_env="KEY_NAME",
            )
            selected, selected_model = check_provider.build_config(explicit)
            self.assertEqual(selected_model, "saved")
            self.assertEqual(selected["fake"]["base_url"], "http://override.invalid/v1")
            self.assertEqual(selected["fake"]["api_key_env"], "KEY_NAME")
        self.assertEqual(selected["fake"]["additional_dirs"], [])

        qwen_args = SimpleNamespace(
            config=None, provider="qwen", model="local-model", base_url=None,
            api_key_env=None,
        )
        qwen_config, _ = check_provider.build_config(qwen_args)
        self.assertFalse(qwen_config["qwen"]["auto_start"])
        self.assertEqual(qwen_config["qwen"]["start_command"], [])

        native_args = SimpleNamespace(
            config=None, provider="codex", model="cli-model", base_url=None,
            api_key_env=None,
        )
        with self.assertRaisesRegex(ValueError, "native CLI provider"):
            check_provider.build_config(native_args)

    def test_base_url_and_model_are_validated_before_requests(self):
        bad_url = SimpleNamespace(
            config=None, provider="lmstudio", model="model-1", base_url="not-a-url",
            api_key_env=None,
        )
        with self.assertRaises(ValueError):
            check_provider.build_config(bad_url)
        missing_model = SimpleNamespace(
            config=None, provider="lmstudio", model=None, base_url=None,
            api_key_env=None,
        )
        with self.assertRaisesRegex(ValueError, "--model is required"):
            check_provider.build_config(missing_model)

    def test_cli_requires_explicit_live_run_and_emits_json_summary(self):
        output = io.StringIO()
        with redirect_stdout(output):
            code = check_provider.main(["--provider", "fake", "--model", "model-1"])
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(output.getvalue())["error"], "live check requires --run")

        output = io.StringIO()
        adapter = _GoodAdapter()
        with redirect_stdout(output):
            code = check_provider.main(
                ["--provider", "lmstudio", "--model", "model-1", "--run"],
                adapter_factory=lambda _provider, _config: adapter,
            )
        self.assertEqual(code, 0)
        summary = json.loads(output.getvalue())
        self.assertEqual((summary["provider"], summary["model"]), ("lmstudio", "model-1"))
        self.assertTrue(summary["ok"])

    def test_failure_checks_nonzero_results_and_wrong_continuation(self):
        for adapter, detail in [
            (_GoodAdapter(first_exit=1), "nonzero result"),
            (_GoodAdapter(second_exit=1), "nonzero result"),
            (_GoodAdapter(wrong_continuation=True), "same conversation"),
        ]:
            with self.subTest(detail=detail), tempfile.TemporaryDirectory() as directory:
                with self.assertRaisesRegex(RuntimeError, detail):
                    check_provider.run_smoke(adapter, "fake", "model-1", Path(directory))

    def test_cancellation_ignoring_signal_fails_and_adapter_stdout_is_suppressed(self):
        with tempfile.TemporaryDirectory() as directory:
            output = io.StringIO()
            with redirect_stdout(output):
                with self.assertRaisesRegex(RuntimeError, "cancellation"):
                    check_provider.run_smoke(
                        _GoodAdapter(ignore_cancel=True), "fake", "model-1", Path(directory),
                    )
            self.assertEqual(output.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
