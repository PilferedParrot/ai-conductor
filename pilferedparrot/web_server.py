"""HTTP transport and local-server lifecycle for PilferedParrot.

This module knows nothing about providers, persistence, or native desktop
integration.  The composition layer supplies an application object and the
small callbacks needed to start or attach to a server.
"""

from __future__ import annotations

import errno
import hashlib
import ipaddress
import json
import re
import shutil
import socket
import subprocess
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Protocol
from urllib.error import HTTPError
from urllib.parse import urlparse
from urllib.request import urlopen


ASSET_ROOT = Path(__file__).resolve().parent / "web_assets"
RUNTIME_ROOT = Path(__file__).resolve().parent
ASSET_NAMES = (
    "index.html", "chat.html", "app.css", "app.js", "chat.js", "icon.svg",
    "pilferedparrot-icon.png", "company-logo.png", "company-logo-dark.png",
)
API_GENERATION = 20


class ServerApp(Protocol):
    config: dict[str, Any]
    default_provider: str
    dashboard_capability: str

    def capability_context(self, supplied: str) -> dict[str, str] | None: ...
    def state(self, scope: str, *, window_id: str, window_provider: str | None) -> Any: ...
    def chat_state(self, chat_id: str, *, window_id: str) -> Any: ...
    def current_chat_state(self) -> Any: ...
    def budgets(self) -> dict[str, Any]: ...
    def poll_provider_models(self, provider: str) -> Any: ...
    def browser_theme(self) -> Any: ...
    def chrome_theme_background(self) -> tuple[bytes, str] | None: ...
    def create_chat(self, payload: dict[str, Any], *, window_id: str, window_provider: str | None) -> Any: ...
    def add_provider(self, payload: dict[str, Any]) -> Any: ...
    def remove_provider(self, payload: dict[str, Any]) -> None: ...
    def set_provider_preferences(self, payload: dict[str, Any], *, window_provider: str | None) -> Any: ...
    def set_notification_preferences(self, payload: dict[str, Any]) -> Any: ...
    def choose_project_directory(self, payload: dict[str, Any], provider: str) -> Any: ...
    def send_chat_message(self, payload: dict[str, Any], *, provider: str | None) -> Any: ...
    def cancel_chat(self) -> Any: ...
    def reset_chat(self, payload: dict[str, Any], *, provider: str | None) -> Any: ...
    def set_chat_model(self, payload: dict[str, Any], *, provider: str | None) -> Any: ...
    def set_chat_context_window(self, payload: dict[str, Any], *, provider: str | None) -> Any: ...
    def open_chat_window(self, url: str, payload: dict[str, Any]) -> Any: ...
    def open_provider_window(self, url: str, payload: dict[str, Any]) -> Any: ...
    def provider_auth_action(self, provider: str, action: str) -> Any: ...
    def submit_provider_auth_code(self, provider: str, payload: dict[str, Any]) -> Any: ...
    def open_chrome_theme_gallery(self) -> Any: ...
    def send_message(
        self, chat_id: str, payload: dict[str, Any], *, window_id: str,
        window_provider: str | None,
    ) -> Any: ...
    def set_context_window(self, chat_id: str, payload: dict[str, Any], *, window_id: str) -> Any: ...
    def cancel_message(self, chat_id: str, *, window_id: str) -> Any: ...
    def launch_terminal_command(self, chat_id: str, payload: dict[str, Any], *, window_id: str) -> None: ...
    def delete_chat(self, chat_id: str, *, window_id: str) -> None: ...
    def persist_dashboard_capability(self, origin: str) -> None: ...
    def recover_interrupted(self) -> int: ...
    def shutdown(self) -> None: ...
    def remove_dashboard_capability(self) -> None: ...


def _asset_fingerprint(root: Path) -> str:
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


def web_authority(host: str, port: int) -> str:
    return f"[{host}]:{port}" if ":" in host else f"{host}:{port}"


class IPv6ThreadingHTTPServer(ThreadingHTTPServer):
    address_family = socket.AF_INET6


def pilferedparrot_status(
    url: str, *, opener: Callable[..., Any] = urlopen,
    api_generation: int = API_GENERATION, asset_version: str = ASSET_VERSION,
    runtime_version: str = RUNTIME_VERSION,
) -> str:
    try:
        with opener(f"{url}/api/status", timeout=1) as response:
            payload, server = json.load(response), response.headers.get("Server", "")
        if not (
            server.startswith(("PilferedParrot/", "ParrotRelay/", "AIConductor/"))
            and isinstance(payload, dict)
            and payload.get("service") == "pilferedparrot"
        ):
            return "other"
        return "compatible" if (
            payload.get("api_generation") == api_generation
            and payload.get("asset_version") == asset_version
            and payload.get("runtime_version") == runtime_version
        ) else "stale"
    except HTTPError as error:
        if error.code != HTTPStatus.NOT_FOUND:
            error.close()
            return "unavailable"
        error.close()
        try:
            with opener(f"{url}/api/state", timeout=1) as response:
                payload, server = json.load(response), response.headers.get("Server", "")
            return "stale" if (
                server.startswith(("PilferedParrot/", "ParrotRelay/", "AIConductor/"))
                and isinstance(payload, dict)
                and isinstance(payload.get("chats"), list)
            ) else "other"
        except (OSError, ValueError, json.JSONDecodeError):
            return "unavailable"
    except (OSError, ValueError, json.JSONDecodeError):
        return "unavailable"


def terminate_stale_pilferedparrot(
    url: str, port: int, *, status: Callable[[str], str] = pilferedparrot_status,
    which: Callable[[str], str | None] = shutil.which,
    runner: Callable[..., Any] = subprocess.run,
    monotonic: Callable[[], float] = time.monotonic, sleeper: Callable[[float], None] = time.sleep,
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
    app: ServerApp, *, asset_root: Path = ASSET_ROOT,
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
    open_window_documents: dict[str, set[str]] = {}

    def cancel_window_close() -> None:
        nonlocal window_close_timer
        with window_close_lock:
            if window_close_timer is not None:
                window_close_timer.cancel()
                window_close_timer = None

    def document_id(payload: dict[str, Any]) -> str:
        value = payload.get("document_id")
        if value is None:
            return "legacy"
        if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{8,128}", value):
            raise ValueError("document_id is invalid")
        return value

    def register_window(window_id: str, page_id: str) -> None:
        nonlocal window_close_timer
        with window_close_lock:
            open_window_documents.setdefault(window_id, set()).add(page_id)
            if window_close_timer is not None:
                window_close_timer.cancel()
                window_close_timer = None

    def unregister_window(
        window_id: str, page_id: str, server: ThreadingHTTPServer,
        *, close_all: bool = False,
    ) -> None:
        nonlocal window_close_timer
        with window_close_lock:
            pages = open_window_documents.get(window_id)
            if close_all:
                open_window_documents.pop(window_id, None)
            elif pages is not None:
                pages.discard(page_id)
                if not pages:
                    open_window_documents.pop(window_id, None)
            if open_window_documents:
                return
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
    configured_host = str(app.config["web"]["host"])
    configured_port = int(app.config["web"]["port"])

    def listener_authority(server: ThreadingHTTPServer | None = None) -> str:
        # Port zero requests an ephemeral listener; use the port selected by
        # bind(2) for Host/Origin validation as well as generated URLs.
        port = (
            server.server_address[1]
            if configured_port == 0 and server is not None
            else configured_port
        )
        return web_authority(configured_host, int(port))

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
                authority = self.headers.get("Host", "")
                host = urlparse(f"//{authority}").hostname
                host_is_local = host is not None and loopback_host(host)
            except ValueError:
                return False
            return peer_is_local and host_is_local and authority == listener_authority(
                getattr(self, "server", None),
            )

        def _request_capability_context(
            self, *, require_origin: bool = False,
        ) -> dict[str, str] | None:
            origin = self.headers.get("Origin")
            expected_origin = f"http://{listener_authority(getattr(self, 'server', None))}"
            if require_origin and origin != expected_origin:
                return None
            if not require_origin and origin is not None and origin != expected_origin:
                return None
            supplied = self.headers.get(
                "X-PilferedParrot-Capability",
                self.headers.get("X-PilferedParrot-CSRF", ""),
            )
            if not self._local_request_allowed():
                return None
            return app.capability_context(supplied)

        def _request_capability_scope(self, *, require_origin: bool = False) -> str | None:
            context = self._request_capability_context(require_origin=require_origin)
            return context.get("scope") if context else None

        def _control_allowed(self, required_scope: str = "dashboard") -> bool:
            return self._request_capability_scope(require_origin=True) == required_scope

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

        def _binary(self, body: bytes, content_type: str) -> None:
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "private, max-age=300")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def _do_GET(self) -> None:
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
            elif path == "/api/status":
                self._json({
                    "service": "pilferedparrot",
                    "api_generation": api_generation,
                    "asset_version": asset_version,
                    "runtime_version": runtime_version,
                })
            elif path == "/api/state":
                context = self._request_capability_context()
                if context is None:
                    self._json({"error": "window authorization failed"}, HTTPStatus.FORBIDDEN)
                else:
                    self._json(app.state(
                        context["scope"],
                        window_id=context.get("history_id") or context.get("window_id") or "main",
                        window_provider=context.get("provider") or None,
                    ))
            elif re.fullmatch(r"/api/chats/[^/]+", path):
                context = self._request_capability_context()
                if context is None or context.get("scope") != "dashboard":
                    self._json({"error": "dashboard authorization failed"}, HTTPStatus.FORBIDDEN)
                else:
                    self._json(app.chat_state(
                        path.rsplit("/", 1)[1],
                        window_id=context.get("history_id") or context.get("window_id") or "main",
                    ))
            elif path == "/api/chat/current":
                if self._request_capability_scope() != "chat":
                    self._json({"error": "Chat authorization failed"}, HTTPStatus.FORBIDDEN)
                else:
                    self._json(app.current_chat_state())
            elif path == "/api/budgets":
                if self._request_capability_scope() != "dashboard":
                    self._json({"error": "dashboard authorization failed"}, HTTPStatus.FORBIDDEN)
                else:
                    self._json({name: value.as_dict() for name, value in app.budgets().items()})
            elif re.fullmatch(r"/api/providers/[^/]+/models", path):
                provider = path.split("/")[3]
                scope = self._request_capability_scope()
                context = self._request_capability_context() if scope == "chat" else None
                if scope not in {"dashboard", "chat"} or scope == "chat" \
                        and (context is None or context.get("provider") != provider):
                    self._json({"error": "window authorization failed"}, HTTPStatus.FORBIDDEN)
                else:
                    self._json(app.poll_provider_models(provider))
            elif path == "/api/browser/theme":
                if self._request_capability_scope() not in {"dashboard", "chat"}:
                    self._json({"error": "window authorization failed"}, HTTPStatus.FORBIDDEN)
                else:
                    self._json(app.browser_theme())
            elif path == "/api/browser/theme/background":
                if self._request_capability_scope() not in {"dashboard", "chat"}:
                    self._json({"error": "window authorization failed"}, HTTPStatus.FORBIDDEN)
                else:
                    asset = app.chrome_theme_background()
                    if asset is None:
                        self.send_error(HTTPStatus.NOT_FOUND)
                    else:
                        self._binary(*asset)
            else:
                self.send_error(HTTPStatus.NOT_FOUND)

        def do_GET(self) -> None:
            try:
                self._do_GET()
            except (ValueError, json.JSONDecodeError) as exc:
                self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            except Exception as exc:
                print(f"[web] request failed: {type(exc).__name__}: {exc}")
                self._json(
                    {"error": f"PilferedParrot error: {exc}"},
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                )

        def do_POST(self) -> None:
            try:
                path = urlparse(self.path).path
                chat_control = path.startswith("/api/chat/") and path != "/api/chat/window"
                required_scope = "chat" if chat_control else "dashboard"
                if not self._control_allowed(required_scope):
                    self._json({"error": "local control authorization failed"}, HTTPStatus.FORBIDDEN)
                    return
                context = self._request_capability_context(require_origin=True)
                if context is None:
                    self._json({"error": "local control authorization failed"}, HTTPStatus.FORBIDDEN)
                    return
                lifecycle_window_id = context.get("window_id") or "main"
                window_id = context.get("history_id") or lifecycle_window_id
                window_provider = context.get("provider") or None
                parts = path.strip("/").split("/")
                payload = self._read_json()
                if path == "/api/chats":
                    self._json(app.create_chat(
                        payload, window_id=window_id, window_provider=window_provider,
                    ), HTTPStatus.CREATED)
                elif path == "/api/providers":
                    self._json(app.add_provider(payload), HTTPStatus.CREATED)
                elif path == "/api/providers/remove":
                    app.remove_provider(payload)
                    self._json({"ok": True})
                elif path == "/api/preferences/provider":
                    self._json(app.set_provider_preferences(
                        payload,
                        window_provider=window_provider if window_id != "main" else None,
                    ))
                elif path == "/api/preferences/notifications":
                    self._json(app.set_notification_preferences(payload))
                elif path == "/api/project/folder":
                    self._json(app.choose_project_directory(
                        payload, provider=window_provider or app.default_provider,
                    ))
                elif path == "/api/chat/messages":
                    self._json(app.send_chat_message(
                        payload, provider=window_provider,
                    ), HTTPStatus.ACCEPTED)
                elif path == "/api/chat/cancel":
                    self._json(app.cancel_chat())
                elif path == "/api/chat/reset":
                    self._json(app.reset_chat(payload, provider=window_provider))
                elif path == "/api/chat/model":
                    self._json(app.set_chat_model(payload, provider=window_provider))
                elif path == "/api/chat/context":
                    self._json(app.set_chat_context_window(
                        payload, provider=window_provider,
                    ))
                elif path == "/api/chat/window":
                    host = self.headers.get("Host", "")
                    self._json(app.open_chat_window(f"http://{host}/chat", {
                        **payload, "provider": window_provider or app.default_provider,
                    }))
                elif path == "/api/provider/window":
                    host = self.headers.get("Host", "")
                    self._json(app.open_provider_window(f"http://{host}/", payload))
                elif len(parts) == 4 and parts[:2] == ["api", "providers"] \
                        and parts[3] in {"login", "logout"}:
                    self._json(app.provider_auth_action(parts[2], parts[3]))
                elif len(parts) == 4 and parts[:2] == ["api", "providers"] \
                        and parts[3] == "code":
                    self._json(app.submit_provider_auth_code(parts[2], payload))
                elif path == "/api/browser/theme":
                    self._json(app.open_chrome_theme_gallery())
                elif path == "/api/window/open":
                    register_window(lifecycle_window_id, document_id(payload))
                    self._json({"ok": True})
                elif path == "/api/window/close":
                    self._json({"ok": True}, HTTPStatus.ACCEPTED)
                    page_id = document_id(payload)
                    unregister_window(
                        lifecycle_window_id, page_id, self.server,
                        close_all="document_id" not in payload,
                    )
                elif path == "/api/shutdown":
                    cancel_window_close()
                    self._json({"ok": True}, HTTPStatus.ACCEPTED)
                    thread_factory(
                        target=self.server.shutdown,
                        name="pilferedparrot-window-close",
                        daemon=True,
                    ).start()
                elif len(parts) == 4 and parts[:2] == ["api", "chats"] and parts[3] == "messages":
                    self._json(app.send_message(
                        parts[2], payload, window_id=window_id,
                        window_provider=window_provider,
                    ), HTTPStatus.ACCEPTED)
                elif len(parts) == 4 and parts[:2] == ["api", "chats"] and parts[3] == "context":
                    self._json(app.set_context_window(
                        parts[2], payload, window_id=window_id,
                    ))
                elif len(parts) == 4 and parts[:2] == ["api", "chats"] and parts[3] == "cancel":
                    self._json(app.cancel_message(parts[2], window_id=window_id))
                elif len(parts) == 4 and parts[:2] == ["api", "chats"] and parts[3] == "terminal":
                    app.launch_terminal_command(parts[2], payload, window_id=window_id)
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
                context = self._request_capability_context(require_origin=True)
                if context is None:
                    self._json({"error": "local control authorization failed"}, HTTPStatus.FORBIDDEN)
                    return
                window_id = context.get("history_id") or context.get("window_id") or "main"
                if len(parts) == 3 and parts[:2] == ["api", "chats"]:
                    app.delete_chat(parts[2], window_id=window_id)
                    self._json({"ok": True})
                else:
                    self.send_error(HTTPStatus.NOT_FOUND)
            except KeyError:
                self._json({"error": "work session not found"}, HTTPStatus.NOT_FOUND)
            except ValueError as exc:
                self._json({"error": str(exc)}, HTTPStatus.CONFLICT)
            except Exception as exc:
                print(f"[web] request failed: {type(exc).__name__}: {exc}")
                self._json(
                    {"error": f"PilferedParrot error: {exc}"},
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                )

    return Handler


def serve(
    config: dict[str, Any], cwd: Path, *, open_browser: bool | None,
    create_app: Callable[[dict[str, Any], Path], ServerApp],
    make_handler: Callable[[ServerApp], type[BaseHTTPRequestHandler]],
    read_capability: Callable[[str, dict[str, Any]], str | None],
    browser_url: Callable[[str, str], str], browser_open: Callable[[str], Any],
    status: Callable[[str], str], terminate: Callable[[str, int], None],
    http_server: Callable[..., Any] = ThreadingHTTPServer,
    ipv6_http_server: Callable[..., Any] = IPv6ThreadingHTTPServer,
    timer_factory: Callable[..., Any] = threading.Timer,
) -> int:
    web = config["web"]
    host, port = str(web["host"]), int(web["port"])
    if not loopback_host(host):
        raise ValueError(
            "web.host must be a loopback address; remote exposure is not supported"
        )
    url = f"http://{web_authority(host, port)}"
    should_open = bool(
        web.get("open_browser", True) if open_browser is None else open_browser
    )
    current = status(url) if port != 0 else "unavailable"

    def attach() -> int:
        print(f"PilferedParrot is already running at {url}")
        if should_open:
            capability = read_capability(url, config)
            if capability is None:
                raise RuntimeError("could not attach the app window to the running server")
            browser_open(browser_url(url, capability))
        return 0

    if current == "compatible":
        return attach()
    if current == "stale":
        print(f"Replacing stale PilferedParrot at {url}")
        terminate(url, port)
    app = create_app(config, cwd)
    factory = (
        ipv6_http_server
        if host != "localhost" and ipaddress.ip_address(host).version == 6
        else http_server
    )
    try:
        server = factory((host, port), make_handler(app))
        if port == 0:
            url = f"http://{web_authority(host, server.server_address[1])}"
    except OSError as error:
        current = status(url)
        if error.errno != errno.EADDRINUSE or current == "other":
            raise
        if current == "stale":
            terminate(url, port)
            server = factory((host, port), make_handler(app))
        elif current != "compatible":
            raise
        else:
            return attach()
    app.persist_dashboard_capability(url)
    recovered = app.recover_interrupted()
    if should_open:
        target = browser_url(url, app.dashboard_capability)
        timer_factory(0.4, lambda: browser_open(target)).start()
    print(f"PilferedParrot is running at {url}")
    if recovered:
        print(f"Recovered {recovered} interrupted response(s).")
    print("Press Ctrl-C to stop.")
    try: server.serve_forever()
    except KeyboardInterrupt:
        print()
    finally:
        app.shutdown()
        app.remove_dashboard_capability()
        server.server_close()
    return 0
