"""Windows provider command discovery and direct argv translation."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pilferedparrot.config import resolve_command
from pilferedparrot.budgets import read_claude_status
from pilferedparrot.processes import provider_argv
from pilferedparrot.provider_updates import check_provider_update


class WindowsProviderProcessTests(unittest.TestCase):
    def _file(self, path: Path, content: str = "") -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        path.chmod(0o755)
        return path

    def _package(self, root: Path, package: str = "@openai/codex",
                 entry: str = "bin/cli.js", *, name: str | None = None,
                 executable: str = "codex") -> Path:
        package_dir = root / "node_modules" / Path(package)
        package_dir.mkdir(parents=True, exist_ok=True)
        (package_dir / "package.json").write_text(json.dumps({
            "name": name or package,
            "bin": {executable: entry},
        }), encoding="utf-8")
        return self._file(package_dir / entry)

    def test_global_npm_layout_translates_to_node_and_preserves_literal_args(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shim = self._file(root / "npm" / "codex.cmd")
            entry = self._package(root / "npm")
            with patch("pilferedparrot.processes._windows", return_value=True), \
                 patch("pilferedparrot.processes.shutil.which", return_value="node.exe"):
                result = provider_argv([str(shim), "say & do not run", "$(literal)"])
        self.assertEqual(result, ["node.exe", str(entry.resolve()), "say & do not run", "$(literal)"])

    def test_local_npm_layout_uses_package_sibling_of_dot_bin(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "project"
            shim = self._file(project / "node_modules" / ".bin" / "codex.cmd")
            entry = self._package(project)
            with patch("pilferedparrot.processes._windows", return_value=True), \
                 patch("pilferedparrot.processes.shutil.which", return_value="node.exe"):
                result = provider_argv([str(shim)])
        self.assertEqual(result, ["node.exe", str(entry.resolve())])

    def test_all_supported_npm_packages_resolve_their_known_entries(self):
        packages = {
            "codex": "@openai/codex",
            "gemini": "@google/gemini-cli",
            "claude": "@anthropic-ai/claude-code",
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            commands = []
            for executable, package in packages.items():
                shim = self._file(root / executable / f"{executable}.cmd")
                entry = self._package(root / executable, package, executable=executable)
                commands.append((shim, entry))
            with patch("pilferedparrot.processes._windows", return_value=True), \
                 patch("pilferedparrot.processes.shutil.which", return_value="node.exe"):
                for shim, entry in commands:
                    with self.subTest(executable=shim.stem):
                        self.assertEqual(
                            provider_argv([str(shim)]), ["node.exe", str(entry.resolve())]
                        )

    def test_parent_package_is_not_selected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shim = self._file(root / "project" / "bin" / "codex.cmd")
            self._package(root)
            with patch("pilferedparrot.processes._windows", return_value=True):
                with self.assertRaisesRegex(RuntimeError, "Unsupported Windows CLI shim"):
                    provider_argv([str(shim)])

    def test_manifest_name_must_match(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shim = self._file(root / "npm" / "codex.cmd")
            self._package(root / "npm", name="wrong-package")
            with patch("pilferedparrot.processes._windows", return_value=True):
                with self.assertRaises(RuntimeError):
                    provider_argv([str(shim)])

    def test_entry_must_stay_inside_package(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shim = self._file(root / "npm" / "codex.cmd")
            self._package(root / "npm", entry="../outside.js")
            self._file(root / "npm" / "outside.js")
            with patch("pilferedparrot.processes._windows", return_value=True):
                with self.assertRaises(RuntimeError):
                    provider_argv([str(shim)])

    def test_missing_node_is_reported_after_valid_entry(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shim = self._file(root / "npm" / "codex.cmd")
            self._package(root / "npm")
            with patch("pilferedparrot.processes._windows", return_value=True), \
                 patch("pilferedparrot.processes.shutil.which", return_value=None):
                with self.assertRaisesRegex(RuntimeError, "Node.js is required"):
                    provider_argv([str(shim)])

    def test_unknown_batch_shim_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            shim = self._file(Path(directory) / "anything.bat")
            with patch("pilferedparrot.processes._windows", return_value=True):
                with self.assertRaisesRegex(RuntimeError, "Unsupported Windows CLI shim"):
                    provider_argv([str(shim)])

    def test_budget_probe_turns_unsupported_shim_into_status(self):
        config = {"claude": {"command": "claude"}}
        with patch("pilferedparrot.budgets.resolve_command", return_value="claude.cmd"), \
             patch("pilferedparrot.processes._windows", return_value=True):
            result = read_claude_status(config)
        self.assertIn("Unsupported Windows CLI shim", result.note)

    def test_update_probe_turns_unsupported_shim_into_status(self):
        config = {"codex": {"command": "codex"}}
        with patch("pilferedparrot.provider_updates.resolve_command", return_value="codex.cmd"), \
             patch("pilferedparrot.processes._windows", return_value=True):
            result = check_provider_update(config, "codex")
        self.assertEqual(result["status"], "unavailable")
        self.assertIn("Unsupported Windows CLI shim", result["message"])

    def test_windows_command_discovery_checks_configured_directories(self):
        with tempfile.TemporaryDirectory() as directory:
            executable = self._file(Path(directory) / "gemini.cmd")
            config = {"gemini": {"command": "gemini"}, "cli_search_paths": [directory]}
            with patch("pilferedparrot.config.sys.platform", "win32"), \
                 patch("pilferedparrot.config.shutil.which", return_value=None):
                result = resolve_command(config, "gemini")
        self.assertEqual(result, str(executable))


if __name__ == "__main__":
    unittest.main()
