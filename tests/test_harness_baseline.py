"""Baseline output is bounded metadata, never private transcript content."""
import json
from pathlib import Path
import tempfile
import unittest
from pilferedparrot.harness_baseline import chat_baseline, codex_baseline


class HarnessBaselineTests(unittest.TestCase):
    def test_completed_metadata_only_and_explicit_sample_limit(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "chats.json"
            path.write_text(json.dumps({"chats": [{"id": "private-session", "cwd": "/private/workspace",
                "messages": [{"role": "assistant", "content": "secret credential", "model": "model-a",
                              "created_at": 10, "exit_code": 0},
                             {"role": "assistant", "content": "another secret", "created_at": 11,
                              "pending": True},
                             {"role": "assistant", "content": "missing exit", "created_at": 12}]}]}))
            report = chat_baseline(path, before=20, limit=1)
            self.assertEqual(report["replies"], 1)
            self.assertEqual(report["zero_exits"], 1)
            text = json.dumps(report)
            for private in ("private-session", "/private/workspace", "secret credential"):
                self.assertNotIn(private, text)

    def test_content_completion_and_scope_do_not_sum_cumulative_usage(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            events = [
                {"type": "session_meta", "timestamp": 1, "payload": {"parent_thread_id": "private-parent"}},
                {"type": "turn_context", "timestamp": 2, "payload": {"model": "model-b", "effort": "medium"}},
                {"type": "event_msg", "timestamp": 3, "payload": {"type": "token_count", "info": {"total_token_usage": {"input_tokens": 20}}}},
                {"type": "event_msg", "timestamp": 4, "payload": {"type": "task_complete"}},
                {"type": "event_msg", "timestamp": 9, "payload": {"type": "task_started"}},
            ]
            (root / "private.jsonl").write_text("\n".join(json.dumps(event) for event in events))
            report = codex_baseline(root, before=5, limit=1, scan_limit=1)
            self.assertEqual(report["sessions"], 1)
            self.assertEqual(report["child_sessions"], 1)
            self.assertIsNone(report["usage_total"])
            self.assertNotIn("private", json.dumps(report))
            self.assertEqual(codex_baseline(root, before=10, limit=1, scan_limit=1)["sessions"], 0)
