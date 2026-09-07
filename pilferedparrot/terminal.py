"""Launch commands in a user-facing interactive terminal window."""

from __future__ import annotations

import base64
import os
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path


_TITLE_PREFIX = "PPI Terminal"
_GEOMETRY = "100x30"


def _title() -> str:
    return f"{_TITLE_PREFIX} {uuid.uuid4().hex}"


def _unix_wrapper() -> str:
    return (
        'cd -- "$1" || exit 1; '
        'printf \'%s\\n\' "Working directory:"; '
        'printf \'%s\\n\' "$1"; '
        'printf \'%s\\n\' "Command:"; '
        'printf \'%s\\n\' "$2"; '
        'printf \'\\n\'; '
        'bash -lc "$2"; status=$?; '
        'printf \'%s\\n\' "Command exited with status $status."; '
        'exec bash'
    )


def _powershell_wrapper(command: str, cwd: Path, title: str) -> str:
    """Return a PowerShell command with untrusted values represented as data.

    ``-EncodedCommand`` cannot safely take positional arguments after the
    encoded script.  Encode the two values separately, then decode them inside
    that script so PowerShell never parses either value while starting up.
    """
    def encoded(value: str) -> str:
        return base64.b64encode(value.encode("utf-8")).decode("ascii")

    cwd_data = encoded(str(cwd))
    command_data = encoded(command)
    title_data = encoded(title)
    return (
        "$utf8 = [System.Text.UTF8Encoding]::new(); "
        "[Console]::InputEncoding = $utf8; [Console]::OutputEncoding = $utf8; "
        "$OutputEncoding = $utf8; "
        "$title = [System.Text.Encoding]::UTF8.GetString("
        f"[System.Convert]::FromBase64String('{title_data}')); "
        "$Host.UI.RawUI.WindowTitle = $title; "
        "$cwd = [System.Text.Encoding]::UTF8.GetString("
        f"[System.Convert]::FromBase64String('{cwd_data}')); "
        "$command = [System.Text.Encoding]::UTF8.GetString("
        f"[System.Convert]::FromBase64String('{command_data}')); "
        "Set-Location -LiteralPath $cwd -ErrorAction Stop; "
        'Write-Output "Working directory:"; Write-Output $cwd; '
        'Write-Output "Command:"; Write-Output $command; '
        'Write-Output ""; Invoke-Expression -Command $command'
    )


def _gnome_terminal(path: str) -> bool:
    """Return whether *path* names gnome-terminal, including a symlink."""
    try:
        target = os.path.basename(os.path.realpath(path)).lower()
    except OSError:
        target = os.path.basename(path).lower()
    # Debian's gnome-terminal.wrapper only accepts xterm-style arguments.
    return target == "gnome-terminal"


def terminal_argv(
    command: str, cwd: Path, *, _windows_title: str | None = None,
) -> list[str]:
    """Build an interactive terminal invocation with command supplied as data."""
    cwd = Path(cwd)
    if sys.platform == "win32":
        shell = shutil.which("powershell.exe") or shutil.which("pwsh.exe")
        if shell is None:
            raise RuntimeError("PowerShell is required to open a command window")
        title = _windows_title or _title()
        encoded = base64.b64encode(
            _powershell_wrapper(command, cwd, title).encode("utf-16-le")
        ).decode("ascii")
        return [
            shell, "-NoLogo", "-NoProfile", "-NoExit", "-WindowStyle", "Normal",
            "-EncodedCommand", encoded,
        ]

    shell = shutil.which("bash") or "/bin/bash"
    script = _unix_wrapper()
    title = _title()
    gnome = shutil.which("gnome-terminal")
    if gnome:
        return [
            gnome, "--window", "--active", "--geometry", _GEOMETRY,
            "--title", title, "--", shell, "-lc", script,
            "pilferedparrot-terminal", str(cwd), command,
        ]

    emulator = shutil.which("x-terminal-emulator")
    if emulator:
        if _gnome_terminal(emulator):
            return [
                emulator, "--window", "--active", "--geometry", _GEOMETRY,
                "--title", title, "--", shell, "-lc", script,
                "pilferedparrot-terminal", str(cwd), command,
            ]
        return [
            emulator, "-T", title, "-geometry", _GEOMETRY, "-e", shell,
            "-lc", script, "pilferedparrot-terminal", str(cwd), command,
        ]
    xterm = shutil.which("xterm")
    if xterm:
        return [
            xterm, "-T", title, "-geometry", _GEOMETRY, "-e", shell,
            "-lc", script, "pilferedparrot-terminal", str(cwd), command,
        ]
    raise RuntimeError("no supported graphical terminal was found")


def _focus_window(title: str) -> None:
    """Best-effort activation of the newly launched window, bounded in time."""
    wmctrl = shutil.which("wmctrl")
    if not wmctrl:
        return
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline:
        try:
            listed = subprocess.run(
                [wmctrl, "-l", "-x"], capture_output=True, text=True,
                check=False, timeout=0.2,
            )
            window_id = None
            for line in listed.stdout.splitlines():
                columns = line.split(None, 4)
                if len(columns) == 5 and columns[4] == title:
                    window_id = columns[0]
                    break
            if window_id:
                subprocess.run(
                    [wmctrl, "-ir", window_id, "-b", "remove,shaded"],
                    capture_output=True, check=False, timeout=0.2,
                )
                subprocess.run(
                    [wmctrl, "-ia", window_id], capture_output=True,
                    check=False, timeout=0.2,
                )
                return
        except (OSError, ValueError, subprocess.SubprocessError):
            return
        time.sleep(0.05)


def _windows_console_handle(title: str, user32: object, callback_type: object) -> int | None:
    """Find the visible console whose title exactly matches *title*."""
    import ctypes
    from ctypes import wintypes

    found: int | None = None

    @callback_type(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def find_console(hwnd: int, _lparam: int) -> bool:
        nonlocal found
        if not user32.IsWindowVisible(hwnd):
            return True
        class_name = ctypes.create_unicode_buffer(64)
        user32.GetClassNameW(hwnd, class_name, len(class_name))
        if class_name.value not in {"ConsoleWindowClass", "CASCADIA_HOSTING_WINDOW_CLASS"}:
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        window_title = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, window_title, len(window_title))
        if window_title.value == title:
            found = hwnd
            return False
        return True

    user32.EnumWindows(find_console, 0)
    return found


def _focus_windows_console(title: str) -> None:
    """Bring the exact newly-created console to the foreground when permitted."""
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        enum_proc = ctypes.WINFUNCTYPE(
            wintypes.BOOL, wintypes.HWND, wintypes.LPARAM,
        )
        user32.EnumWindows.argtypes = [enum_proc, wintypes.LPARAM]
        user32.EnumWindows.restype = wintypes.BOOL
        user32.IsWindowVisible.argtypes = [wintypes.HWND]
        user32.IsWindowVisible.restype = wintypes.BOOL
        user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        user32.GetClassNameW.restype = ctypes.c_int
        user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
        user32.GetWindowTextLengthW.restype = ctypes.c_int
        user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        user32.GetWindowTextW.restype = ctypes.c_int
        user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
        user32.ShowWindow.restype = wintypes.BOOL
        user32.SetForegroundWindow.argtypes = [wintypes.HWND]
        user32.SetForegroundWindow.restype = wintypes.BOOL

        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            window = _windows_console_handle(title, user32, ctypes.WINFUNCTYPE)
            if window is not None:
                user32.ShowWindow(window, 9)  # SW_RESTORE
                user32.SetForegroundWindow(window)
                return
            time.sleep(0.05)
    except (AttributeError, OSError):
        return


def launch_terminal(command: str, cwd: Path) -> None:
    """Launch *command* in an interactive terminal rooted at *cwd*."""
    title: str | None = None
    if sys.platform == "win32":
        title = _title()
        argv = terminal_argv(command, cwd, _windows_title=title)
    else:
        argv = terminal_argv(command, cwd)
    kwargs: dict[str, object] = {"cwd": Path(cwd)}
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NEW_CONSOLE
        kwargs["close_fds"] = True
    else:
        kwargs["start_new_session"] = True
        kwargs["close_fds"] = True
    subprocess.Popen(argv, **kwargs)
    if sys.platform == "win32":
        _focus_windows_console(title or "")
    if sys.platform != "win32":
        title = next((argv[i + 1] for i, value in enumerate(argv[:-1])
                      if value in {"--title", "-T"}), None)
        if title:
            _focus_window(title)
