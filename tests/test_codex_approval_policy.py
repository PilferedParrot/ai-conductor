import unittest
from pathlib import Path
from unittest.mock import patch

from pilferedparrot.config import load_config
from pilferedparrot.dispatch import _codex_command
from pilferedparrot.model import Conversation


class CodexApprovalPolicyTests(unittest.TestCase):
    @patch("pilferedparrot.dispatch.provider_command", return_value="codex")
    def test_policy_is_added_to_fresh_and_resumed_commands(self, _command):
        config = load_config(Path("/definitely/missing/config.json"))
        for policy in ("untrusted", "on-failure", "on-request", "never"):
            for session_id in (None, "thread-1"):
                with self.subTest(policy=policy, session_id=session_id):
                    config["codex"]["approval_policy"] = policy
                    command = _codex_command(
                        Conversation(provider_session_id=session_id), config, Path.cwd(),
                    )
                    self.assertIn(f'approval_policy="{policy}"', command)
                    if session_id:
                        self.assertLess(command.index("--config"), command.index("resume"))

    @patch("pilferedparrot.dispatch.provider_command", return_value="codex")
    def test_unset_policy_is_omitted(self, _command):
        config = load_config(Path("/definitely/missing/config.json"))
        command = _codex_command(Conversation(), config, Path.cwd())
        self.assertFalse(any("approval_policy" in argument for argument in command))

    @patch("pilferedparrot.dispatch.provider_command", return_value="codex")
    def test_invalid_policy_fails(self, _command):
        config = load_config(Path("/definitely/missing/config.json"))
        config["codex"]["approval_policy"] = "always"
        with self.assertRaisesRegex(ValueError, "unsupported Codex approval policy: always"):
            _codex_command(Conversation(), config, Path.cwd())


if __name__ == "__main__":
    unittest.main()
