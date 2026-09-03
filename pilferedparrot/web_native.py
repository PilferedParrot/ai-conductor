"""Native desktop and browser-window integration for the web application."""

from __future__ import annotations

import ipaddress
import shutil
import subprocess
import tempfile
import threading
import time
import webbrowser
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse
from urllib.request import Request, urlopen


CHROMIUM_BROWSER_CANDIDATES = (
    "google-chrome-stable", "google-chrome", "chromium", "chromium-browser",
)


def chromium_browser() -> str | None:
    """Return the same preferred Chrome-family browser used by the app launcher."""
    return next((
        path for candidate in CHROMIUM_BROWSER_CANDIDATES
        if (path := shutil.which(candidate))
    ), None)


def browser_url(
    url: str, capability: str, *, api_generation: int,
    asset_version: str, runtime_version: str,
) -> str:
    """Deliver the instance credential in a non-referrer URL fragment."""
    return (
        f"{url}/?generation={api_generation}&assets={asset_version}"
        f"&runtime={runtime_version}#close_token={capability}"
    )


def open_browser(url: str) -> bool:
    """Open a URL through the operator's configured desktop browser."""
    return webbrowser.open(url)


def notify_window_closed(
    browser_url: str, *, opener: Callable[..., Any] | None = None,
    is_loopback: Callable[[str], bool] | None = None,
) -> bool:
    """Tell the exact loopback server instance that its app window closed."""
    try:
        parsed = urlparse(browser_url)
        if parsed.hostname is None:
            return False
        if is_loopback is None:
            try:
                local_host = parsed.hostname == "localhost" \
                    or ipaddress.ip_address(parsed.hostname).is_loopback
            except ValueError:
                local_host = False
        else:
            local_host = is_loopback(parsed.hostname)
        if parsed.scheme != "http" or not local_host \
                or parsed.username is not None or parsed.password is not None:
            return False
        tokens = parse_qs(parsed.fragment, keep_blank_values=True).get("close_token", [])
        if len(tokens) != 1 or not tokens[0]:
            return False
        request = Request(
            f"http://{parsed.netloc}/api/shutdown", data=b"{}",
            headers={
                "Content-Type": "application/json",
                "X-PilferedParrot-CSRF": tokens[0],
            },
            method="POST",
        )
        with (opener or urlopen)(request, timeout=2) as response:
            return 200 <= response.status < 300
    except (OSError, ValueError):
        return False


class NativeIntegration:
    """Own the native Chat browser process and temporary profile lifecycle."""

    def __init__(self):
        self.chat_window_lock = threading.RLock()
        self.chat_window_process: subprocess.Popen[bytes] | None = None
        self.chat_window_profile: Path | None = None

    @staticmethod
    def window_number(payload: dict[str, Any], name: str, minimum: int) -> int:
        value = payload.get(name)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"chat window {name} is invalid")
        number = int(value)
        if number < minimum or number > 32_768:
            raise ValueError(f"chat window {name} is invalid")
        return number

    def _clean_chat_window_profile(self) -> None:
        profile = self.chat_window_profile
        self.chat_window_profile = None
        if profile is not None:
            shutil.rmtree(profile, ignore_errors=True)

    def _watch_chat_window(self, process: subprocess.Popen[bytes]) -> None:
        process.wait()
        with self.chat_window_lock:
            if self.chat_window_process is process:
                self.chat_window_process = None
                self._clean_chat_window_profile()

    def open_chat_window(
        self, url: str, payload: dict[str, Any], *, browser: str | None = None,
    ) -> dict[str, Any]:
        """Open Chat in a normal native window with an isolated profile."""
        width = self.window_number(payload, "width", 320)
        height = self.window_number(payload, "height", 240)
        left = self.window_number(payload, "left", -32_768)
        top = self.window_number(payload, "top", -32_768)
        with self.chat_window_lock:
            process = self.chat_window_process
            if process is not None and process.poll() is None:
                wmctrl = shutil.which("wmctrl")
                if wmctrl:
                    subprocess.run(
                        [wmctrl, "-x", "-a", "pilferedparrot-chat"], check=False,
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    )
                return {"ok": True, "existing": True}
            self.chat_window_process = None
            self._clean_chat_window_profile()
            browser = browser or chromium_browser()
            if browser is None:
                raise RuntimeError("Chrome or Chromium is required for the Chat window")
            profile = Path(tempfile.mkdtemp(prefix="pilferedparrot-chat-"))
            try:
                process = subprocess.Popen(
                    [
                        browser, f"--user-data-dir={profile}", "--no-first-run",
                        "--no-default-browser-check", "--disable-background-mode",
                        "--disable-session-crashed-bubble", "--class=pilferedparrot-chat",
                        f"--window-size={width},{height}", f"--window-position={left},{top}",
                        f"--app={url}",
                    ],
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL, start_new_session=True, close_fds=True,
                )
            except Exception:
                shutil.rmtree(profile, ignore_errors=True)
                raise
            self.chat_window_profile = profile
            self.chat_window_process = process
            threading.Thread(
                target=self._watch_chat_window, args=(process,),
                name="pilferedparrot-chat-window", daemon=True,
            ).start()
            return {"ok": True, "existing": False}

    def shutdown(self, *, deadline: float | None = None, timeout: float = 3) -> None:
        if deadline is None:
            deadline = time.monotonic() + timeout
        with self.chat_window_lock:
            process = self.chat_window_process
            if process is not None and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=max(0, deadline - time.monotonic()))
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=1)
            self.chat_window_process = None
            self._clean_chat_window_profile()
