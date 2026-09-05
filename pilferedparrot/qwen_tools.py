from __future__ import annotations

import difflib
import json
import os
import shutil
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any


TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a UTF-8 text file in the workspace with line numbers.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Workspace-relative path"},
                    "start_line": {"type": "integer", "minimum": 1, "default": 1},
                    "max_lines": {"type": "integer", "minimum": 1, "maximum": 1000, "default": 250},
                },
                "required": ["path"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Create or completely replace a UTF-8 text file in the workspace.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Workspace-relative path"},
                    "content": {"type": "string"},
                },
                "required": ["path", "content"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_file",
            "description": "Replace an exact text fragment in a workspace file. By default the fragment must occur exactly once.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Workspace-relative path"},
                    "old_text": {"type": "string"},
                    "new_text": {"type": "string"},
                    "replace_all": {"type": "boolean", "default": False},
                },
                "required": ["path", "old_text", "new_text"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "shell",
            "description": "Run a sandboxed Bash command. Only workspace writes persist; home data is hidden and /tmp is ephemeral.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string"},
                    "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 600},
                },
                "required": ["command"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "diff",
            "description": "Show the current Git status and unstaged/staged diff for the workspace or one path.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Optional workspace-relative path"},
                },
                "additionalProperties": False,
            },
        },
    },
]


class QwenToolbox:
    def __init__(
        self,
        cwd: Path,
        config: dict[str, Any],
        additional_dirs: Sequence[Path] = (),
    ):
        self.cwd = cwd.resolve()
        home = Path.home().resolve()
        if self.cwd in home.parents:
            raise ValueError(
                "Qwen cannot use a parent of the home directory as its workspace; "
                "select a narrower project directory instead"
            )
        if self.cwd == home and config.get("allow_home_workspace") is not True:
            raise ValueError(
                "Qwen cannot use the entire home directory unless "
                "qwen.allow_home_workspace is explicitly enabled"
            )
        # Extra roots let one task span several projects without widening the
        # workspace to the whole home directory. The primary workspace stays
        # first so a path inside it keeps reporting workspace-relative names.
        roots: list[Path] = [self.cwd]
        for raw_root in additional_dirs:
            root = Path(raw_root).resolve()
            if not root.is_dir():
                raise ValueError(f"additional directory does not exist: {root}")
            if root == home or root in home.parents:
                raise ValueError(
                    "additional directories cannot contain the home directory "
                    "or one of its parents"
                )
            if not os.access(root, os.W_OK | os.X_OK):
                raise ValueError(f"additional directory is not writable: {root}")
            if any(root == kept or kept in root.parents for kept in roots):
                continue
            roots.append(root)
        self.roots = tuple(roots)
        self.config = config
        self.output_limit = int(config.get("tool_output_chars", 24_000))
        self.file_limit = int(config.get("file_limit_bytes", 1_000_000))
        self.shell_timeout = int(config.get("shell_timeout_seconds", 120))
        self.shell_max_timeout = int(config.get("shell_max_timeout_seconds", 600))
        self._baselines: dict[Path, str | None] = {}

    def execute(self, name: str, arguments: dict[str, Any]) -> str:
        if self.config.get("read_only") and name not in {"read_file", "diff"}:
            raise PermissionError(f"tool is unavailable in read-only Chat: {name}")
        handlers = {
            "read_file": self._read_file,
            "write_file": self._write_file,
            "edit_file": self._edit_file,
            "shell": self._shell,
            "diff": self._diff,
        }
        handler = handlers.get(name)
        if handler is None:
            raise ValueError(f"unknown tool: {name}")
        if not isinstance(arguments, dict):
            raise ValueError("tool arguments must be a JSON object")
        return self._limit(handler(**arguments))

    def _path(self, value: str, *, must_exist: bool = False) -> Path:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("path must be a non-empty string")
        # A relative path stays workspace-relative; an absolute one is accepted
        # only when it lands inside a root the operator configured.
        path = (self.cwd / value).resolve()
        if self._root_for(path) is None:
            raise PermissionError(f"path escapes workspace: {value}")
        if must_exist and not path.exists():
            raise FileNotFoundError(value)
        return path

    def _root_for(self, path: Path) -> Path | None:
        for root in self.roots:
            if path == root or root in path.parents:
                return root
        return None

    def _display(self, path: Path) -> str:
        """Name a path the way the operator will recognise it."""
        if path == self.cwd or self.cwd in path.parents:
            return str(path.relative_to(self.cwd))
        return str(path)

    def _remember(self, path: Path) -> None:
        if path in self._baselines:
            return
        self._baselines[path] = path.read_text(encoding="utf-8") if path.exists() else None

    def _read_file(self, path: str, start_line: int = 1, max_lines: int = 250) -> str:
        target = self._path(path, must_exist=True)
        if not target.is_file():
            raise IsADirectoryError(path)
        if target.stat().st_size > self.file_limit:
            raise ValueError(f"file is larger than {self.file_limit} bytes")
        lines = target.read_text(encoding="utf-8").splitlines()
        start = max(1, int(start_line))
        count = min(1000, max(1, int(max_lines)))
        selected = lines[start - 1:start - 1 + count]
        body = "\n".join(f"{number:>6}  {line}" for number, line in enumerate(selected, start))
        end = start + len(selected) - 1
        return f"{path}: lines {start}-{end} of {len(lines)}\n{body}"

    def _write_file(self, path: str, content: str) -> str:
        target = self._path(path)
        if target.exists() and not target.is_file():
            raise IsADirectoryError(path)
        if target.exists() and target.stat().st_size > self.file_limit:
            raise ValueError(f"existing file is larger than {self.file_limit} bytes")
        encoded = content.encode("utf-8")
        if len(encoded) > self.file_limit:
            raise ValueError(f"content is larger than {self.file_limit} bytes")
        self._remember(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(encoded)
        return self._file_diff(target)

    def _edit_file(
        self,
        path: str,
        old_text: str,
        new_text: str,
        replace_all: bool = False,
    ) -> str:
        target = self._path(path, must_exist=True)
        if not target.is_file():
            raise IsADirectoryError(path)
        if not old_text:
            raise ValueError("old_text must not be empty")
        content = target.read_text(encoding="utf-8")
        occurrences = content.count(old_text)
        if occurrences == 0:
            raise ValueError("old_text was not found")
        if not replace_all and occurrences != 1:
            raise ValueError(f"old_text occurs {occurrences} times; provide more context or set replace_all")
        self._remember(target)
        updated = content.replace(old_text, new_text, -1 if replace_all else 1)
        if len(updated.encode("utf-8")) > self.file_limit:
            raise ValueError(f"result is larger than {self.file_limit} bytes")
        target.write_text(updated, encoding="utf-8")
        return self._file_diff(target)

    def _file_diff(self, path: Path) -> str:
        before = self._baselines[path]
        after = path.read_text(encoding="utf-8")
        relative = self._display(path)
        diff = difflib.unified_diff(
            [] if before is None else before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile="/dev/null" if before is None else f"a/{relative}",
            tofile=f"b/{relative}",
        )
        rendered = "".join(diff)
        return rendered or f"{relative}: no change"

    def _shell(self, command: str, timeout_seconds: int | None = None) -> str:
        if sys.platform == "win32":
            raise RuntimeError(
                "Sandboxed shell tools require Linux and Bubblewrap; Windows supports "
                "file tools and diff. Use a native coding CLI for shell commands."
            )
        if not isinstance(command, str) or not command.strip():
            raise ValueError("command must be a non-empty string")
        timeout = self.shell_timeout if timeout_seconds is None else int(timeout_seconds)
        timeout = max(1, min(timeout, self.shell_max_timeout))
        bwrap = shutil.which("bwrap")
        if not bwrap:
            raise RuntimeError("bubblewrap is required for Qwen shell isolation")
        argv = [
            bwrap,
            "--die-with-parent",
            "--new-session",
            "--unshare-pid",
            "--ro-bind", "/", "/",
            "--tmpfs", "/tmp",
            "--tmpfs", "/run",
            "--dir", "/tmp/home",
            "--dev", "/dev",
            "--proc", "/proc",
        ]
        if self.config.get("shell_network") is not True:
            argv.append("--unshare-net")

        # A read-only root still exposes every operator-readable credential and
        # document. Hide the home directory, then mount back only the selected
        # workspace. An entire-home workspace reaches this branch only after
        # the operator enables the explicit allow_home_workspace override.
        home = Path.home().resolve()
        if self.cwd != home:
            argv.extend(["--tmpfs", str(home)])
            # The mask replaces home with an empty tmpfs, so every root below it
            # needs its mount point recreated before the bind can attach.
            for root in self.roots:
                if home in root.parents:
                    argv.extend(["--dir", str(root)])

        # Put the root binds after /tmp and the home mask so roots below either
        # location remain visible and writable.
        for root in self.roots:
            argv.extend(["--bind", str(root), str(root)])
        argv.extend([
            "--chdir", str(self.cwd),
            "/bin/bash", "-lc", command,
        ])
        environment = {
            "HOME": "/tmp/home",
            "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
            "TMPDIR": "/tmp",
        }
        for name in ("LANG", "LANGUAGE", "LC_ALL", "LC_CTYPE", "TERM", "TZ"):
            if name in os.environ:
                environment[name] = os.environ[name]
        try:
            completed = subprocess.run(
                argv,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=timeout,
                env=environment,
            )
        except subprocess.TimeoutExpired as exc:
            partial = exc.stdout or ""
            raise TimeoutError(f"command timed out after {timeout}s\n{partial}") from exc
        output = completed.stdout.rstrip()
        return f"exit_code: {completed.returncode}\n{output}" if output else f"exit_code: {completed.returncode}"

    def _diff(self, path: str | None = None) -> str:
        root = self.cwd
        pathspec: list[str] = []
        if path:
            target = self._path(path)
            root = self._root_for(target) or self.cwd
            pathspec = ["--", str(target.relative_to(root))]
        probe = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--show-toplevel"],
            text=True, encoding="utf-8", errors="replace", capture_output=True,
        )
        if probe.returncode:
            changed_paths = self._baselines
            if path:
                changed_paths = {
                    changed: baseline for changed, baseline in self._baselines.items()
                    if changed == target or target in changed.parents
                }
            rendered = [self._file_diff(changed) for changed in changed_paths]
            return "\n".join(rendered) if rendered else "workspace is not a Git repository; no file-tool changes yet"
        commands = [
            ["git", "-C", str(root), "status", "--short", *pathspec],
            ["git", "-C", str(root), "diff", "--no-ext-diff", "--no-color", *pathspec],
            ["git", "-C", str(root), "diff", "--cached", "--no-ext-diff", "--no-color", *pathspec],
        ]
        sections: list[str] = []
        for command in commands:
            completed = subprocess.run(command, text=True, encoding="utf-8", errors="replace", capture_output=True)
            text = completed.stdout.rstrip()
            if text:
                sections.append(text)
            if completed.returncode:
                raise RuntimeError(completed.stderr.strip() or "git diff failed")
        return "\n\n".join(sections) or "working tree clean"

    def _limit(self, text: str) -> str:
        if len(text) <= self.output_limit:
            return text
        removed = len(text) - self.output_limit
        return f"{text[:self.output_limit]}\n...[truncated {removed} characters]"


def parse_tool_arguments(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str):
        raise ValueError("tool arguments are neither an object nor a JSON string")
    parsed = json.loads(value)
    if not isinstance(parsed, dict):
        raise ValueError("tool arguments must decode to an object")
    return parsed
