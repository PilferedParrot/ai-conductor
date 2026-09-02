from __future__ import annotations

import difflib
import json
import os
import shutil
import subprocess
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
    def __init__(self, cwd: Path, config: dict[str, Any]):
        self.cwd = cwd.resolve()
        if self.cwd == Path.home().resolve() and not config.get("allow_home_workspace", False):
            raise ValueError(
                "Qwen cannot use the entire home directory unless "
                "qwen.allow_home_workspace is explicitly enabled"
            )
        self.config = config
        self.output_limit = int(config.get("tool_output_chars", 24_000))
        self.file_limit = int(config.get("file_limit_bytes", 1_000_000))
        self.shell_timeout = int(config.get("shell_timeout_seconds", 120))
        self.shell_max_timeout = int(config.get("shell_max_timeout_seconds", 600))
        self._baselines: dict[Path, str | None] = {}

    def execute(self, name: str, arguments: dict[str, Any]) -> str:
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
        path = (self.cwd / value).resolve()
        if path != self.cwd and self.cwd not in path.parents:
            raise PermissionError(f"path escapes workspace: {value}")
        if must_exist and not path.exists():
            raise FileNotFoundError(value)
        return path

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
        relative = str(path.relative_to(self.cwd))
        diff = difflib.unified_diff(
            [] if before is None else before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile="/dev/null" if before is None else f"a/{relative}",
            tofile=f"b/{relative}",
        )
        rendered = "".join(diff)
        return rendered or f"{relative}: no change"

    def _shell(self, command: str, timeout_seconds: int | None = None) -> str:
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
        if not self.config.get("shell_network", False):
            argv.append("--unshare-net")

        # A read-only root still exposes every operator-readable credential and
        # document. Hide the home directory, then mount back only the selected
        # workspace. An entire-home workspace reaches this branch only after
        # the operator enables the explicit allow_home_workspace override.
        home = Path.home().resolve()
        if self.cwd != home:
            argv.extend(["--tmpfs", str(home)])
            if home in self.cwd.parents:
                argv.extend(["--dir", str(self.cwd)])

        # Put the workspace bind after /tmp and the home mask so workspaces below
        # either location remain visible and writable.
        argv.extend([
            "--bind", str(self.cwd), str(self.cwd),
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
        pathspec: list[str] = []
        if path:
            target = self._path(path)
            pathspec = ["--", str(target.relative_to(self.cwd))]
        probe = subprocess.run(
            ["git", "-C", str(self.cwd), "rev-parse", "--show-toplevel"],
            text=True, capture_output=True,
        )
        if probe.returncode:
            rendered = [self._file_diff(changed) for changed in self._baselines]
            return "\n".join(rendered) if rendered else "workspace is not a Git repository; no file-tool changes yet"
        commands = [
            ["git", "-C", str(self.cwd), "status", "--short", *pathspec],
            ["git", "-C", str(self.cwd), "diff", "--no-ext-diff", "--no-color", *pathspec],
            ["git", "-C", str(self.cwd), "diff", "--cached", "--no-ext-diff", "--no-color", *pathspec],
        ]
        sections: list[str] = []
        for command in commands:
            completed = subprocess.run(command, text=True, capture_output=True)
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
