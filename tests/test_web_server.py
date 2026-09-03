"""Focused HTTP transport and server-lifecycle coverage."""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from http import HTTPStatus
from pathlib import Path
from unittest.mock import MagicMock

from pilferedparrot import web_server


class FakeApp:
    def __init__(self, port: int = 8765):
        self.config = {"web": {"host": "127.0.0.1", "port": port}}
        self.default_provider = "codex"
        self.dashboard_capability = "dashboard-token"

    def current_chat_state(self):
        return {"messages": []}

    def capability_context(self, supplied: str):
        if supplied != self.dashboard_capability:
            return None
        return {"scope": "dashboard", "window_id": "main", "provider": "codex"}


def bare_handler(handler_type, *, path: str = "/api/status", port: int = 8765):
    handler = object.__new__(handler_type)
    handler.path = path
    handler.server = MagicMock(server_address=("127.0.0.1", port))
    handler.client_address = ("127.0.0.1", 43210)
    handler.headers = {"Host": f"127.0.0.1:{port}"}
    return handler


class WebServerHTTPTests(unittest.TestCase):
    def test_status_serializes_exact_metadata_and_headers(self):
        handler = bare_handler(web_server.make_handler(
            FakeApp(), asset_version="assets", runtime_version="runtime",
            api_generation=42, version="test",
        ))
        responses: list[HTTPStatus] = []
        headers: dict[str, str] = {}
        handler.send_response = responses.append
        handler.send_header = headers.__setitem__
        handler.end_headers = lambda: None
        handler.wfile = io.BytesIO()

        handler.do_GET()

        self.assertEqual(responses, [HTTPStatus.OK])
        self.assertEqual(json.loads(handler.wfile.getvalue()), {
            "service": "pilferedparrot",
            "api_generation": 42,
            "asset_version": "assets",
            "runtime_version": "runtime",
        })
        self.assertEqual(headers["Content-Type"], "application/json; charset=utf-8")
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(headers["X-PilferedParrot-Assets"], "assets")
        self.assertEqual(headers["X-PilferedParrot-Runtime"], "runtime")
        self.assertEqual(handler.server_version, "PilferedParrot/test")

    def test_assets_are_snapshotted_with_security_headers(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "index.html").write_bytes(b"first")
            handler_type = web_server.make_handler(FakeApp(), asset_root=root)
            (root / "index.html").write_bytes(b"second")

            handler = bare_handler(handler_type, path="/")
            headers: dict[str, str] = {}
            handler.send_response = MagicMock()
            handler.send_header = headers.__setitem__
            handler.end_headers = lambda: None
            handler.wfile = io.BytesIO()
            handler.do_GET()

        self.assertEqual(handler.wfile.getvalue(), b"first")
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(headers["X-Frame-Options"], "DENY")
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])

    def test_shared_markdown_asset_is_snapshotted_fingerprinted_and_secured(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            renderer = root / "markdown.js"
            renderer.write_bytes(b"initial shared renderer")
            fingerprint = web_server._asset_fingerprint(root)
            handler_type = web_server.make_handler(
                FakeApp(), asset_root=root, asset_version=fingerprint,
            )
            renderer.write_bytes(b"changed after handler creation")
            self.assertNotEqual(web_server._asset_fingerprint(root), fingerprint)

            handler = bare_handler(handler_type, path="/markdown.js")
            headers: dict[str, str] = {}
            handler.send_response = MagicMock()
            handler.send_header = headers.__setitem__
            handler.end_headers = lambda: None
            handler.wfile = io.BytesIO()
            handler.do_GET()

        self.assertEqual(handler.wfile.getvalue(), b"initial shared renderer")
        self.assertEqual(headers["Content-Type"], "text/javascript; charset=utf-8")
        self.assertEqual(headers["X-PilferedParrot-Assets"], fingerprint)
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        self.assertIn("script-src 'self'", headers["Content-Security-Policy"])

    def test_api_rejects_a_mismatched_listener_authority(self):
        handler = bare_handler(web_server.make_handler(FakeApp()))
        handler.headers["Host"] = "localhost:8765"
        handler._json = MagicMock()

        handler.do_GET()

        handler._json.assert_called_once_with(
            {"error": "local API authorization failed"}, HTTPStatus.FORBIDDEN,
        )

    def test_json_parser_requires_an_object_and_bounded_length(self):
        handler = bare_handler(web_server.make_handler(FakeApp()))
        handler.rfile = io.BytesIO(b"[]")
        handler.headers.update({
            "Content-Type": "application/json; charset=utf-8",
            "Content-Length": "2",
        })
        with self.assertRaisesRegex(ValueError, "object"):
            handler._read_json()

        handler.headers["Content-Length"] = "1000001"
        with self.assertRaisesRegex(ValueError, "request size"):
            handler._read_json()

        handler.headers["Content-Length"] = "2"
        handler.headers["Content-Type"] = "text/plain"
        with self.assertRaisesRegex(ValueError, "application/json"):
            handler._read_json()

    def test_document_close_waits_for_the_last_open_document(self):
        app = FakeApp()
        timer = MagicMock()
        timer_factory = MagicMock(return_value=timer)
        handler = bare_handler(web_server.make_handler(
            app, timer_factory=timer_factory,
        ))
        handler._control_allowed = lambda _scope="dashboard": True
        handler._request_capability_context = lambda **_kwargs: {
            "scope": "dashboard", "window_id": "main", "provider": "codex",
        }
        handler._json = MagicMock()
        payload = {"document_id": "document-one"}
        handler._read_json = lambda: payload

        handler.path = "/api/window/open"
        handler.do_POST()
        payload["document_id"] = "document-two"
        handler.do_POST()
        handler.path = "/api/window/close"
        payload["document_id"] = "document-one"
        handler.do_POST()
        timer_factory.assert_not_called()
        payload["document_id"] = "document-two"
        handler.do_POST()

        timer_factory.assert_called_once_with(2, handler.server.shutdown)
        timer.start.assert_called_once_with()


class WebServerLifecycleTests(unittest.TestCase):
    def test_status_probe_distinguishes_compatible_and_foreign_servers(self):
        response = MagicMock()
        response.__enter__.return_value = response
        response.headers = {"Server": "PilferedParrot/test"}
        response.read.return_value = json.dumps({
            "service": "pilferedparrot", "api_generation": 7,
            "asset_version": "assets", "runtime_version": "runtime",
        }).encode()
        opener = MagicMock(return_value=response)

        self.assertEqual(web_server.pilferedparrot_status(
            "http://127.0.0.1:8765", opener=opener, api_generation=7,
            asset_version="assets", runtime_version="runtime",
        ), "compatible")
        response.headers = {"Server": "SomethingElse/1"}
        self.assertEqual(web_server.pilferedparrot_status(
            "http://127.0.0.1:8765", opener=opener, api_generation=7,
            asset_version="assets", runtime_version="runtime",
        ), "other")

    def test_serve_uses_ephemeral_port_and_always_cleans_up(self):
        config = {"web": {"host": "127.0.0.1", "port": 0, "open_browser": False}}
        app = FakeApp(port=0)
        app.persist_dashboard_capability = MagicMock()
        app.recover_interrupted = MagicMock(return_value=0)
        app.shutdown = MagicMock()
        app.remove_dashboard_capability = MagicMock()
        server = MagicMock(server_address=("127.0.0.1", 43123))
        server.serve_forever.side_effect = KeyboardInterrupt
        server_factory = MagicMock(return_value=server)
        status = MagicMock()

        result = web_server.serve(
            config, Path("/tmp"), open_browser=False,
            create_app=MagicMock(return_value=app), make_handler=MagicMock(return_value=object),
            read_capability=MagicMock(), browser_url=MagicMock(), browser_open=MagicMock(),
            status=status, terminate=MagicMock(), http_server=server_factory,
        )

        self.assertEqual(result, 0)
        status.assert_not_called()
        app.persist_dashboard_capability.assert_called_once_with("http://127.0.0.1:43123")
        app.shutdown.assert_called_once_with()
        app.remove_dashboard_capability.assert_called_once_with()
        server.server_close.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
