"""Offline HTTP-shaped coverage for OpenAI-compatible coding providers."""
from __future__ import annotations

import json
import io
import tempfile
import unittest
from contextlib import redirect_stdout
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from pilferedparrot.config import DEFAULTS
from pilferedparrot.qwen import run_compatible_agent


class _Response:
    def __init__(self, payload: dict[str, object]) -> None:
        self._body = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, *_args):
        return self._body


class _RawResponse(_Response):
    def __init__(self, body: bytes) -> None:
        self._body = body


def _config(provider: str, *, read_only: bool = False) -> dict[str, object]:
    config = deepcopy(DEFAULTS)
    config[provider] = {
        "adapter": "openai_compatible",
        "base_url": (
            "https://openrouter.ai/api/v1" if provider.startswith("openrouter_")
            else "https://api.mistral.ai/v1" if provider == "mistral"
            else "http://127.0.0.1:1234/v1"
        ),
        "model": {
            "openrouter_deepseek": "deepseek/deepseek-chat-v3.1",
            "openrouter_glm": "z-ai/glm-4.5",
            "mistral": "devstral-small-latest",
            "lmstudio": "glm-4.5-air",
        }[provider],
        "agent_max_tokens": 100,
        "agent_request_timeout_seconds": 1,
        "max_tool_turns": 8,
        "tool_output_chars": 1_000,
        "file_limit_bytes": 1_000_000,
        "shell_timeout_seconds": 1,
        "shell_max_timeout_seconds": 1,
        "shell_network": False,
        "allow_home_workspace": False,
        "additional_dirs": [],
        "read_only": read_only,
    }
    return config


class CompatibleCodingTests(unittest.TestCase):
    def test_target_families_replay_multi_tool_reasoning_and_ids_across_user_turns(self):
        for provider in ("openrouter_deepseek", "openrouter_glm", "mistral", "lmstudio"):
            with self.subTest(provider=provider), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                (root / "sample.txt").write_text("hello", encoding="utf-8")
                first_id, second_id = (
                    ("abc123456", "def123456") if provider == "mistral"
                    else ("read-1", "diff-1")
                )
                continuation = {
                    "openrouter_deepseek": {"reasoning_details": [
                        {"type": "reasoning.text", "text": "keep me"},
                    ]},
                    "openrouter_glm": {"reasoning": "GLM opaque reasoning"},
                    "mistral": {},
                    "lmstudio": {"reasoning_content": "local opaque alias"},
                }[provider]
                first_content = [{"type": "text", "text": "Inspecting."}]
                if provider == "mistral":
                    first_content.append({
                        "type": "thinking", "thinking": "do not display", "signature": "sig",
                    })
                responses = [
                    {"choices": [{"message": {
                        "role": "assistant",
                        "content": first_content,
                        **continuation,
                        "tool_calls": [
                            {"id": first_id, "type": "function", "function": {
                                "name": "read_file", "arguments": {"path": "sample.txt"},
                            }},
                            {"id": second_id, "type": "function", "function": {
                                "name": "diff", "arguments": "{}",
                            }},
                        ],
                    }}]},
                    {"choices": [{"message": {
                        "role": "assistant", "content": "First done.",
                        "reasoning_details": [{"type": "reasoning.text", "text": "after tools"}],
                    }}]},
                    {"choices": [{"message": {
                        "role": "assistant", "content": "Second inspection.",
                        "tool_calls": [{"id": "ghi123456" if provider == "mistral" else "read-2", "type": "function", "function": {
                            "name": "read_file", "arguments": "{\"path\":\"sample.txt\"}",
                        }}],
                    }}]},
                    {"choices": [{"message": {"role": "assistant", "content": "All done."}}]},
                ]
                requests: list[dict[str, object]] = []

                def open_url(_config, _provider, request, *, timeout):
                    self.assertEqual(timeout, 1)
                    requests.append(json.loads(request.data.decode("utf-8")))
                    return _Response(responses.pop(0))

                config = _config(provider)
                history: list[dict[str, object]] = []
                printed = io.StringIO()
                with redirect_stdout(printed), patch(
                        "pilferedparrot.qwen.open_compatible_url", side_effect=open_url):
                    first = run_compatible_agent(
                        provider, "inspect the file", history, config, root,
                    )
                    second = run_compatible_agent(
                        provider, "inspect it again", history, config, root,
                    )

                self.assertEqual((first, second), ("First done.", "All done."))
                third_id = "ghi123456" if provider == "mistral" else "read-2"
                self.assertEqual([item["tool_call_id"] for item in history if item["role"] == "tool"],
                                 [first_id, second_id, third_id])
                first_assistant = history[1]
                for field, value in continuation.items():
                    self.assertEqual(first_assistant[field], value)
                # The next request replays the opaque continuation fields and
                # the later request still includes the first user turn/tool IDs.
                for field, value in continuation.items():
                    self.assertEqual(requests[1]["messages"][2][field], value)
                self.assertEqual(requests[2]["messages"][2]["tool_calls"][0]["id"], first_id)
                self.assertEqual(requests[2]["messages"][-1]["content"], "inspect it again")
                self.assertNotIn("do not display", printed.getvalue())

    def test_read_only_tool_advertisement_and_execution_remain_contained(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            responses = [
                {"choices": [{"message": {
                    "role": "assistant", "content": "",
                    "tool_calls": [{"id": "write-attempt", "type": "function", "function": {
                        "name": "write_file", "arguments": "{\"path\":\"x\",\"content\":\"bad\"}",
                    }}],
                }}]},
                {"choices": [{"message": {"role": "assistant", "content": "Read-only."}}]},
            ]
            requests: list[dict[str, object]] = []

            def open_url(_config, _provider, request, *, timeout):
                requests.append(json.loads(request.data.decode("utf-8")))
                return _Response(responses.pop(0))

            with patch("pilferedparrot.qwen.open_compatible_url", side_effect=open_url):
                result = run_compatible_agent(
                    "lmstudio", "do not change files", [],
                    _config("lmstudio", read_only=True), root,
                )

            names = [item["function"]["name"] for item in requests[0]["tools"]]
            self.assertEqual(names, ["read_file", "diff"])
            self.assertEqual(result, "Read-only.")
            self.assertFalse((root / "x").exists())
            self.assertIn("tool_error: PermissionError", requests[1]["messages"][-1]["content"])

    def test_write_then_read_proves_workspace_containment(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            responses = [
                {"choices": [{"message": {"role": "assistant", "content": None,
                    "tool_calls": [{"id": "write-1", "type": "function", "function": {
                        "name": "write_file", "arguments": "{\"path\":\"created.txt\",\"content\":\"inside\"}",
                    }}]}}]},
                {"choices": [{"message": {"role": "assistant", "content": None,
                    "tool_calls": [{"id": "read-1", "type": "function", "function": {
                        "name": "read_file", "arguments": "{\"path\":\"created.txt\"}",
                    }}]}}]},
                {"choices": [{"message": {"role": "assistant", "content": "Created and verified."}}]},
            ]

            def open_url(_config, _provider, _request, *, timeout):
                return _Response(responses.pop(0))

            with patch("pilferedparrot.qwen.open_compatible_url", side_effect=open_url):
                result = run_compatible_agent(
                    "lmstudio", "create and verify a file", [], _config("lmstudio"), root,
                )
            self.assertEqual(result, "Created and verified.")
            self.assertEqual((root / "created.txt").read_text(encoding="utf-8"), "inside")
            self.assertFalse((root.parent / "created.txt").exists())

    def test_malformed_payloads_have_meaningful_provider_errors(self):
        with tempfile.TemporaryDirectory() as directory:
            payloads = [
                (_RawResponse(b"not-json"), "invalid JSON body"),
                (_Response({"choices": 3}), "choices is missing or not a non-empty array"),
                (_Response({"choices": [{"message": {"content": {"bad": True}}}]}),
                 "assistant content is not a string, array, or null"),
                (_Response({"choices": [{"message": {"content": None, "tool_calls": 3}}]}),
                 "tool_calls is not an array"),
                (_Response({"choices": [{"message": {"content": None, "tool_calls": ["bad-call"]}}]}),
                 "tool call 0 is not an object"),
            ]
            for response, detail in payloads:
                with self.subTest(detail=detail), patch(
                        "pilferedparrot.qwen.open_compatible_url", return_value=response):
                    with self.assertRaisesRegex(RuntimeError, f"lmstudio returned malformed chat completion: {detail}"):
                        run_compatible_agent("lmstudio", "hello", [], _config("lmstudio"), Path(directory))


if __name__ == "__main__":
    unittest.main()
