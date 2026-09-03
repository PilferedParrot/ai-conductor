"""Isolated real-server fixture for Playwright browser tests."""

from __future__ import annotations

import json
import tempfile
import threading
import time
from copy import deepcopy
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.request import urlopen

from pilferedparrot import web
from pilferedparrot.config import DEFAULTS
from pilferedparrot.dispatch import RunResult


class FakeBudget:
    """Version-neutral provider status returned by the fake integration."""

    def as_dict(self):
        return {
            "provider": "codex", "available": True, "window": None,
            "observed_at": int(time.time()), "note": "Deterministic browser fixture",
            "status": "ok", "windows": [], "auth_status": "signed_in",
            "reachability": "reachable",
        }


class FakeProvider:
    """Deterministic provider boundary with explicit progress/completion gates."""

    ERROR_PROMPT = "Trigger the fake provider error"
    ERROR_TEXT = "Fake provider rejected this deterministic request."

    def __init__(self) -> None:
        self.requests: list[tuple[str, str, Path]] = []
        self._gates: dict[str, threading.Event] = {}
        self._lock = threading.Lock()

    def hold(self, prompt: str) -> None:
        with self._lock:
            self._gates[prompt] = threading.Event()

    def complete(self, prompt: str) -> None:
        with self._lock:
            gate = self._gates[prompt]
        gate.set()

    def release_all(self) -> None:
        with self._lock:
            gates = tuple(self._gates.values())
        for gate in gates:
            gate.set()

    def dispatch(
        self, provider, prompt, cwd, conversation, _config, cancel_event,
    ) -> RunResult:
        del conversation
        assert provider == "codex"
        with self._lock:
            self.requests.append((provider, prompt, cwd))
            gate = self._gates.get(prompt)
        progress = getattr(cancel_event, "_pilferedparrot_progress", None)
        if progress:
            progress("status", "Preparing deterministic response")
        if gate is not None and not gate.wait(timeout=10):
            raise TimeoutError("browser test did not release the fake provider")
        if self.ERROR_PROMPT in prompt:
            return RunResult("", 7, error=self.ERROR_TEXT)
        return RunResult(
            f"Fake provider completed: {prompt}", 0,
            session_id="fake-browser-session", input_tokens=12, output_tokens=6,
            live_input_tokens=12, live_output_tokens=6,
        )


class BrowserTestApp(web.PilferedParrotApp):
    """The production application with only external integrations replaced."""

    def budgets(self):
        return {"codex": FakeBudget()}

    def state(self, *args, **kwargs):
        if hasattr(self, "capabilities"):
            return super().state(*args, **kwargs)
        return super().state()

    def create_chat(self, payload, **kwargs):
        if hasattr(self, "capabilities"):
            return super().create_chat(payload, **kwargs)
        return super().create_chat(payload)

    def capability_context(self, supplied: str):
        parent = getattr(super(), "capability_context", None)
        if parent is not None:
            return parent(supplied)
        if supplied == self.dashboard_capability:
            return {"scope": "dashboard", "window_id": "main", "provider": "codex"}
        return None

    def persist_dashboard_capability(self, origin: str):
        parent = getattr(super(), "persist_dashboard_capability", None)
        if parent is not None:
            parent(origin)

    def remove_dashboard_capability(self):
        parent = getattr(super(), "remove_dashboard_capability", None)
        if parent is not None:
            parent()

    def poll_provider_models(self, provider: str):
        if provider != "codex":
            raise ValueError("unknown fake provider")
        return {
            "provider": "codex", "default": "fake-small", "source": "browser_fixture",
            "polled_at": int(time.time()),
            "options": [
                {"value": "fake-small", "label": "Fake Small"},
                {"value": "fake-large", "label": "Fake Large"},
            ],
        }

    def browser_theme(self):
        return {"active": False}

    def chrome_theme_background(self):
        return None


class PilferedParrotBrowserFixture:
    """Own temporary config/state plus an ephemeral loopback HTTP server."""

    def __init__(self) -> None:
        self._temporary = tempfile.TemporaryDirectory(prefix="pilferedparrot-browser-")
        self.root = Path(self._temporary.name)
        self.project = self.root / "project"
        self.project.mkdir()
        self.provider = FakeProvider()
        self.config = self._config()
        self.config_path = self.root / "config.json"
        self.config_path.write_text(
            json.dumps(self.config, indent=2) + "\n", encoding="utf-8",
        )

        self._original_dispatch = web.capture_dispatch
        web.capture_dispatch = self.provider.dispatch
        try:
            self.app = BrowserTestApp(self.config, self.project)
            if not hasattr(self.app, "dashboard_capability"):
                self.app.dashboard_capability = self.app.csrf_token
            self.server = ThreadingHTTPServer(
                ("127.0.0.1", 0), web.make_handler(self.app),
            )
            port = int(self.server.server_address[1])
            self.base_url = f"http://127.0.0.1:{port}"
            self.app.persist_dashboard_capability(self.base_url)
            self.thread = threading.Thread(
                target=self.server.serve_forever,
                name="pilferedparrot-playwright-server", daemon=True,
            )
            self.thread.start()
            self._wait_until_ready()
        except BaseException:
            web.capture_dispatch = self._original_dispatch
            self._temporary.cleanup()
            raise

    def _config(self):
        config = deepcopy(DEFAULTS)
        state = self.root / "state"
        config["_hidden_providers"] = ["qwen", "claude", "gemini"]
        config["web"].update({
            "host": "127.0.0.1",
            "port": 0,
            "open_browser": False,
            "default_provider": "codex",
            "chat_store": str(state / "chats.json"),
            "model_catalog_store": str(state / "models.json"),
        })
        config["codex"].update({
            "command": str(self.root / "never-invoked-codex"),
            "model": "fake-small",
            "model_options": [
                {"value": "fake-small", "label": "Fake Small"},
                {"value": "fake-large", "label": "Fake Large"},
            ],
            "config_path": str(self.root / "codex-config.toml"),
            "models_cache": str(self.root / "models-cache.json"),
        })
        config["ledger"] = str(state / "runs.jsonl")
        return config

    @property
    def browser_url(self) -> str:
        if hasattr(self.app, "capabilities"):
            return f"{self.base_url}/#capability={self.app.dashboard_capability}"
        return f"{self.base_url}/"

    def _wait_until_ready(self) -> None:
        deadline = time.monotonic() + 5
        while True:
            try:
                with urlopen(f"{self.base_url}/", timeout=0.25) as response:
                    if response.status == 200:
                        return
            except OSError:
                if time.monotonic() >= deadline:
                    raise RuntimeError("PilferedParrot browser fixture did not start")
                time.sleep(0.01)

    def stop(self) -> None:
        self.provider.release_all()
        self.server.shutdown()
        self.thread.join(timeout=5)
        self.app.shutdown()
        self.app.remove_dashboard_capability()
        self.server.server_close()
        web.capture_dispatch = self._original_dispatch
        self._temporary.cleanup()
        if self.thread.is_alive():
            raise RuntimeError("PilferedParrot browser fixture did not stop")
