"""HTTP transport and local-server lifecycle for PilferedParrot."""

from __future__ import annotations

import errno
import hashlib
import hmac
import ipaddress
import json
import shutil
import subprocess
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Protocol
from urllib.parse import urlparse
from urllib.request import urlopen


ASSET_ROOT = Path(__file__).resolve().parent / "web_assets"
RUNTIME_ROOT = Path(__file__).resolve().parent
ASSET_NAMES = (
    "index.html", "chat.html", "app.css", "app.js", "chat.js", "icon.svg",
    "pilferedparrot-icon.png", "company-logo.png", "company-logo-dark.png",
)
API_GENERATION = 10


class HTTPApplication(Protocol):
    csrf_token: str

    def state(self) -> dict[str, Any]: ...
    def budgets(self) -> dict[str, Any]: ...
    def create_chat(self, payload: dict[str, Any]) -> Any: ...
    def send_chat_message(self, payload: dict[str, Any]) -> Any: ...
    def cancel_chat(self) -> Any: ...
    def reset_chat(self, payload: dict[str, Any]) -> Any: ...
    def set_chat_model(self, payload: dict[str, Any]) -> Any: ...
    def set_chat_context_window(self, payload: dict[str, Any]) -> Any: ...
    def open_chat_window(self, url: str, payload: dict[str, Any]) -> Any: ...
    def send_message(self, chat_id: str, payload: dict[str, Any]) -> Any: ...
    def set_context_window(self, chat_id: str, payload: dict[str, Any]) -> Any: ...
    def cancel_message(self, chat_id: str) -> Any: ...
    def launch_terminal_command(self, chat_id: str, payload: dict[str, Any]) -> None: ...
    def delete_chat(self, chat_id: str) -> None: ...


class LifecycleApplication(HTTPApplication, Protocol):
    def recover_interrupted(self) -> int: ...
    def shutdown(self) -> None: ...


def _asset_fingerprint(root: Path) -> str:
    """Identify the exact frontend bundle snapshotted by a server process."""
    digest = hashlib.sha256()
    for name in ASSET_NAMES:
        digest.update(name.encode("utf-8") + b"\0")
        try:
            content = (root / name).read_bytes()
        except OSError:
            digest.update(b"missing\0")
            continue
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()[:16]


def _runtime_fingerprint(root: Path) -> str:
    """Identify the Python runtime loaded by a newly launched process."""
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*.py")):
        relative = path.relative_to(root).as_posix()
        digest.update(relative.encode("utf-8") + b"\0")
        try:
            content = path.read_bytes()
        except OSError:
            digest.update(b"missing\0")
            continue
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()[:16]


ASSET_VERSION = _asset_fingerprint(ASSET_ROOT)
RUNTIME_VERSION = _runtime_fingerprint(RUNTIME_ROOT)


def loopback_host(value: str) -> bool:
    if value == "localhost":
        return True
    try:
        return ipaddress.ip_address(value).is_loopback
    except ValueError:
        return False


def pilferedparrot_status(
    url: str, *, opener: Callable[..., Any] = urlopen,
    api_generation: int = API_GENERATION, asset_version: str = ASSET_VERSION,
    runtime_version: str = RUNTIME_VERSION,
) -> str:
    try:
        with opener(f"{url}/api/state", timeout=1) as response:
            payload = json.load(response)
            server = response.headers.get("Server", "")
        if not (
            server.startswith(("PilferedParrot/", "ParrotRelay/", "AIConductor/"))
            and isinstance(payload, dict)
            and isinstance(payload.get("chats"), list)
            and isinstance(payload.get("csrf_token"), str)
        ):
            return "other"
        return "compatible" if (
            payload.get("api_generation") == api_generation
            and payload.get("asset_version") == asset_version
            and payload.get("runtime_version") == runtime_version
        ) else "stale"
    except (OSError, ValueError, json.JSONDecodeError):
        return "unavailable"


def pilferedparrot_csrf_token(
    url: str, *, opener: Callable[..., Any] = urlopen,
) -> str | None:
    try:
        with opener(f"{url}/api/state", timeout=1) as response:
            payload = json.load(response)
            server = response.headers.get("Server", "")
        token = payload.get("csrf_token") if isinstance(payload, dict) else None
        return token if (
            server.startswith("PilferedParrot/")
            and isinstance(token, str)
            and token
        ) else None
    except (OSError, ValueError, json.JSONDecodeError):
        return None


def terminate_stale_pilferedparrot(
    url: str, port: int, *, status: Callable[[str], str] = pilferedparrot_status,
    which: Callable[[str], str | None] = shutil.which,
    runner: Callable[..., Any] = subprocess.run,
    monotonic: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], None] = time.sleep,
) -> None:
    fuser = which("fuser")
    if fuser is None:
        raise RuntimeError("cannot replace stale PilferedParrot because fuser is unavailable")
    runner(
        [fuser, "-k", "-INT", f"{port}/tcp"], check=False,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    deadline = monotonic() + 5
    while monotonic() < deadline:
        current = status(url)
        if current == "unavailable":
            return
        if current == "other":
            raise RuntimeError(f"another service took over port {port} during restart")
        sleeper(0.05)
    raise RuntimeError(f"stale PilferedParrot did not release port {port}")


def make_handler(
    app: HTTPApplication, *, asset_root: Path = ASSET_ROOT,
    asset_version: str = ASSET_VERSION, runtime_version: str = RUNTIME_VERSION,
    api_generation: int = API_GENERATION, version: str = "unknown",
    timer_factory: Callable[..., Any] = threading.Timer,
    thread_factory: Callable[..., Any] = threading.Thread,
) -> type[BaseHTTPRequestHandler]:
    """Adapt the application protocol to HTTP without application imports."""
    # Keep the frontend and API on the same generation. During development or
    # an in-place update, rereading assets from disk would let an old process
    # serve new JavaScript that calls routes the process does not have yet.
    asset_cache: dict[str, bytes] = {}
    window_close_lock = threading.Lock()
    window_close_timer: Any = None

    def cancel_window_close() -> None:
        nonlocal window_close_timer
        with window_close_lock:
            if window_close_timer is not None:
                window_close_timer.cancel()
                window_close_timer = None

    def schedule_window_close(server: ThreadingHTTPServer) -> None:
        nonlocal window_close_timer
        with window_close_lock:
            if window_close_timer is not None:
                window_close_timer.cancel()
            # A reload closes and immediately reopens the document. Give that
            # new document time to cancel shutdown while still making a real
            # window close reliably release the server.
            window_close_timer = timer_factory(2, server.shutdown)
            window_close_timer.name = "pilferedparrot-window-close-grace"
            window_close_timer.daemon = True
            window_close_timer.start()
    for asset_name in ASSET_NAMES:
        try:
            asset_cache[asset_name] = (asset_root / asset_name).read_bytes()
        except OSError:
            pass

    class Handler(BaseHTTPRequestHandler):
        server_version = f"PilferedParrot/{version}"

        def log_message(self, fmt: str, *args: Any) -> None:
            print(f"[web] {self.address_string()} {fmt % args}")

        def _json(self, payload: Any, status: HTTPStatus = HTTPStatus.OK) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-PilferedParrot-Assets", asset_version)
            self.send_header("X-PilferedParrot-Runtime", runtime_version)
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def _read_json(self) -> dict[str, Any]:
            content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
            if content_type != "application/json":
                raise ValueError("Content-Type must be application/json")
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError as error:
                raise ValueError("invalid Content-Length") from error
            if length < 0 or length > 1_000_000:
                raise ValueError("invalid request size")
            data = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(data, dict):
                raise ValueError("request body must be an object")
            return data

        def _local_request_allowed(self) -> bool:
            try:
                peer_is_local = ipaddress.ip_address(self.client_address[0]).is_loopback
                host = urlparse(f"//{self.headers.get('Host', '')}").hostname
                host_is_local = host is not None and loopback_host(host)
            except ValueError:
                return False
            return peer_is_local and host_is_local

        def _control_allowed(self) -> bool:
            origin = self.headers.get("Origin")
            origin_is_local = True
            if origin:
                parsed = urlparse(origin)
                origin_is_local = (
                    parsed.scheme == "http" and parsed.hostname is not None
                    and loopback_host(parsed.hostname)
                )
            supplied = self.headers.get(
                "X-PilferedParrot-CSRF",
                self.headers.get("X-Parrot-Relay-CSRF", self.headers.get("X-Conductor-CSRF", "")),
            )
            return (
                self._local_request_allowed() and origin_is_local
                and hmac.compare_digest(supplied, app.csrf_token)
            )

        def _asset(self, name: str, content_type: str) -> None:
            body = asset_cache.get(name)
            if body is None:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-PilferedParrot-Assets", asset_version)
            self.send_header("X-PilferedParrot-Runtime", runtime_version)
            self.send_header("Content-Security-Policy", (
                "default-src 'self'; base-uri 'none'; frame-ancestors 'none'; "
                "form-action 'self'; object-src 'none'; script-src 'self'; "
                "style-src 'self' 'unsafe-inline'"
            ))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def do_GET(self) -> None:
            path = urlparse(self.path).path
            if path.startswith("/api/") and not self._local_request_allowed():
                self._json({"error": "local API authorization failed"}, HTTPStatus.FORBIDDEN)
                return
            if path == "/":
                self._asset("index.html", "text/html; charset=utf-8")
            elif path == "/chat":
                self._asset("chat.html", "text/html; charset=utf-8")
            elif path == "/app.css":
                self._asset("app.css", "text/css; charset=utf-8")
            elif path == "/app.js":
                self._asset("app.js", "text/javascript; charset=utf-8")
            elif path == "/chat.js":
                self._asset("chat.js", "text/javascript; charset=utf-8")
            elif path == "/icon.svg":
                self._asset("icon.svg", "image/svg+xml")
            elif path == "/pilferedparrot-icon.png":
                self._asset("pilferedparrot-icon.png", "image/png")
            elif path == "/company-logo.png":
                self._asset("company-logo.png", "image/png")
            elif path == "/company-logo-dark.png":
                self._asset("company-logo-dark.png", "image/png")
            elif path == "/favicon.ico":
                self.send_response(HTTPStatus.MOVED_PERMANENTLY)
                self.send_header("Location", "/pilferedparrot-icon.png")
                self.end_headers()
            elif path == "/api/state":
                self._json(app.state())
            elif path == "/api/budgets":
                self._json({name: value.as_dict() for name, value in app.budgets().items()})
            else:
                self.send_error(HTTPStatus.NOT_FOUND)

        def do_POST(self) -> None:
            try:
                if not self._control_allowed():
                    self._json({"error": "local control authorization failed"}, HTTPStatus.FORBIDDEN)
                    return
                path = urlparse(self.path).path
                parts = path.strip("/").split("/")
                payload = self._read_json()
                if path == "/api/chats":
                    self._json(app.create_chat(payload), HTTPStatus.CREATED)
                elif path == "/api/chat/messages":
                    self._json(app.send_chat_message(payload), HTTPStatus.ACCEPTED)
                elif path == "/api/chat/cancel":
                    self._json(app.cancel_chat())
                elif path == "/api/chat/reset":
                    self._json(app.reset_chat(payload))
                elif path == "/api/chat/model":
                    self._json(app.set_chat_model(payload))
                elif path == "/api/chat/context":
                    self._json(app.set_chat_context_window(payload))
                elif path == "/api/chat/window":
                    host = self.headers.get("Host", "")
                    self._json(app.open_chat_window(f"http://{host}/chat", payload))
                elif path == "/api/window/open":
                    cancel_window_close()
                    self._json({"ok": True})
                elif path == "/api/window/close":
                    self._json({"ok": True}, HTTPStatus.ACCEPTED)
                    schedule_window_close(self.server)
                elif path == "/api/shutdown":
                    cancel_window_close()
                    self._json({"ok": True}, HTTPStatus.ACCEPTED)
                    thread_factory(
                        target=self.server.shutdown,
                        name="pilferedparrot-window-close",
                        daemon=True,
                    ).start()
                elif len(parts) == 4 and parts[:2] == ["api", "chats"] and parts[3] == "messages":
                    self._json(app.send_message(parts[2], payload), HTTPStatus.ACCEPTED)
                elif len(parts) == 4 and parts[:2] == ["api", "chats"] and parts[3] == "context":
                    self._json(app.set_context_window(parts[2], payload))
                elif len(parts) == 4 and parts[:2] == ["api", "chats"] and parts[3] == "cancel":
                    self._json(app.cancel_message(parts[2]))
                elif len(parts) == 4 and parts[:2] == ["api", "chats"] and parts[3] == "terminal":
                    app.launch_terminal_command(parts[2], payload)
                    self._json({"ok": True}, HTTPStatus.ACCEPTED)
                else:
                    self.send_error(HTTPStatus.NOT_FOUND)
            except KeyError:
                self._json({"error": "work session not found"}, HTTPStatus.NOT_FOUND)
            except (ValueError, json.JSONDecodeError) as exc:
                self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            except Exception as exc:
                print(f"[web] request failed: {type(exc).__name__}: {exc}")
                self._json({"error": f"PilferedParrot error: {exc}"}, HTTPStatus.INTERNAL_SERVER_ERROR)

        def do_DELETE(self) -> None:
            try:
                if not self._control_allowed():
                    self._json({"error": "local control authorization failed"}, HTTPStatus.FORBIDDEN)
                    return
                parts = urlparse(self.path).path.strip("/").split("/")
                if len(parts) == 3 and parts[:2] == ["api", "chats"]:
                    app.delete_chat(parts[2])
                    self._json({"ok": True})
                else:
                    self.send_error(HTTPStatus.NOT_FOUND)
            except KeyError:
                self._json({"error": "work session not found"}, HTTPStatus.NOT_FOUND)

    return Handler

def serve(
    config: dict[str, Any], cwd: Path, *, open_browser: bool | None,
    create_app: Callable[[dict[str, Any], Path], LifecycleApplication],
    make_handler: Callable[[HTTPApplication], type[BaseHTTPRequestHandler]],
    read_capability: Callable[[str], str | None],
    browser_url: Callable[[str, str], str], browser_open: Callable[[str], Any],
    status: Callable[[str], str], terminate: Callable[[str, int], None],
    http_server: Callable[..., Any] = ThreadingHTTPServer,
    timer_factory: Callable[..., Any] = threading.Timer,
) -> int:
    web = config["web"]
    host, port = str(web["host"]), int(web["port"])
    if not loopback_host(host):
        raise ValueError(
            "web.host must be a loopback address; remote exposure is not supported"
        )
    url = f"http://{host}:{port}"
    should_open = bool(
        web.get("open_browser", True) if open_browser is None else open_browser
    )

    def attach() -> int:
        print(f"PilferedParrot is already running at {url}")
        if should_open:
            capability = read_capability(url)
            if capability is None:
                raise RuntimeError("could not attach the app window to the running server")
            browser_open(browser_url(url, capability))
        return 0

    current = status(url)
    if current == "compatible":
        return attach()
    if current == "stale":
        print(f"Replacing stale PilferedParrot at {url}")
        terminate(url, port)
    app = create_app(config, cwd)
    try:
        server = http_server((host, port), make_handler(app))
    except OSError as error:
        current = status(url)
        if error.errno != errno.EADDRINUSE or current == "other":
            raise
        if current == "stale":
            terminate(url, port)
            server = http_server((host, port), make_handler(app))
        elif current != "compatible":
            raise
        else:
            return attach()
    recovered = app.recover_interrupted()
    if should_open:
        target = browser_url(url, app.csrf_token)
        timer_factory(0.4, lambda: browser_open(target)).start()
    print(f"PilferedParrot is running at {url}")
    if recovered:
        print(f"Recovered {recovered} interrupted response(s).")
    print("Press Ctrl-C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print()
    finally:
        app.shutdown()
        server.server_close()
    return 0
