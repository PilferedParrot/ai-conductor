from __future__ import annotations

import argparse
import hmac
import ipaddress
import json
import secrets
import threading
import time
import uuid
import webbrowser
from copy import deepcopy
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from .board import MAX_CONTENT_CHARS, MODEL_ACTORS, PUBLIC_KINDS, BoardError, BoardStore
from .budgets import collect_budgets
from .config import expanded_path, load_config
from .dispatch import RunCancelled, RunResult, capture_dispatch
from .ledger import append_run
from .model import Conversation, ProviderBudget
from .qwen import ensure_qwen
from .router import ask_qwen, enforce_constraints


ASSET_ROOT = Path(__file__).resolve().parent / "web_assets"


def _loopback_host(value: str) -> bool:
    if value == "localhost":
        return True
    try:
        return ipaddress.ip_address(value).is_loopback
    except ValueError:
        return False


@dataclass
class ActiveRun:
    cancel_event: threading.Event = field(default_factory=threading.Event)
    thread: threading.Thread | None = None


class ChatStore:
    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.RLock()
        self.data: dict[str, Any] = {"version": 1, "chats": []}
        self._load()

    def _load(self) -> None:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(payload, dict) and isinstance(payload.get("chats"), list):
                self.data = payload
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            pass

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f".{self.path.name}.{uuid.uuid4().hex}.tmp")
        temporary.write_text(json.dumps(self.data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.chmod(0o600)
        temporary.replace(self.path)

    def list_public(self) -> list[dict[str, Any]]:
        with self.lock:
            return [self.public(chat) for chat in sorted(
                self.data["chats"], key=lambda item: item.get("updated_at", 0), reverse=True
            )]

    @staticmethod
    def public(chat: dict[str, Any]) -> dict[str, Any]:
        # Worker threads mutate the stored chat after a POST returns. Give callers
        # a snapshot, not shared list/dict references that can change mid-encode.
        return deepcopy({key: value for key, value in chat.items() if key != "qwen_messages"})

    def get(self, chat_id: str) -> dict[str, Any]:
        for chat in self.data["chats"]:
            if chat.get("id") == chat_id:
                return chat
        raise KeyError(chat_id)

    def create(self, cwd: Path, requested_provider: str = "auto") -> dict[str, Any]:
        now = int(time.time())
        chat = {
            "id": uuid.uuid4().hex,
            "title": "New conversation",
            "created_at": now,
            "updated_at": now,
            "cwd": str(cwd),
            "requested_provider": requested_provider,
            "provider": None,
            "provider_session_id": None,
            "qwen_messages": [],
            "messages": [],
        }
        with self.lock:
            self.data["chats"].append(chat)
            self.save()
        return self.public(chat)

    def delete(self, chat_id: str) -> None:
        with self.lock:
            before = len(self.data["chats"])
            self.data["chats"] = [chat for chat in self.data["chats"] if chat.get("id") != chat_id]
            if len(self.data["chats"]) == before:
                raise KeyError(chat_id)
            self.save()


class ConductorApp:
    def __init__(self, config: dict[str, Any], default_cwd: Path):
        self.config = config
        self.default_cwd = default_cwd
        self.store = ChatStore(expanded_path(config["web"]["chat_store"]))
        self.board = BoardStore(expanded_path(config["web"]["board_store"]))
        self.csrf_token = secrets.token_urlsafe(32)
        self.runs_lock = threading.RLock()
        self.runs: dict[str, ActiveRun] = {}

    def recover_interrupted(self) -> int:
        """Release persisted jobs that could not survive a server restart."""
        recovered = 0
        with self.store.lock:
            for chat in self.store.data["chats"]:
                chat_recovered = False
                for message in chat.get("messages", []):
                    if not message.get("pending"):
                        continue
                    message.update({
                        "content": "Conductor restarted before this response finished. You can retry.",
                        "error": True,
                        "interrupted": True,
                    })
                    message.pop("pending", None)
                    message.pop("cancel_requested", None)
                    recovered += 1
                    chat_recovered = True
                if chat_recovered:
                    chat["updated_at"] = int(time.time())
            if recovered:
                self.store.save()
        return recovered

    def budgets(self) -> dict[str, ProviderBudget]:
        return collect_budgets(self.config)

    def state(self) -> dict[str, Any]:
        return {
            "chats": self.store.list_public(),
            "default_cwd": str(self.default_cwd),
            "csrf_token": self.csrf_token,
        }

    def board_state(self, limit: int = 200) -> dict[str, Any]:
        return {
            "events": self.board.list_events(limit=limit),
            "limits": {"content_chars": MAX_CONTENT_CHARS, "events_per_read": 500},
            "public_kinds": sorted(PUBLIC_KINDS),
            "trust": {
                "posting_actor": "chris",
                "passive": True,
                "assignments_trigger_execution": False,
                "provider_bridge": "completed_successful_runs_only",
                "board_content_enters_prompts": False,
            },
        }

    def post_board_message(self, payload: dict[str, Any]) -> dict[str, Any]:
        unexpected = set(payload) - {"kind", "content"}
        if unexpected:
            raise BoardError(f"unsupported board field: {sorted(unexpected)[0]}")
        return self.board.post(
            actor="chris",
            kind=payload.get("kind"),
            content=payload.get("content"),
            source="local_web",
        )

    def publish_board_message(self, kind: str, content: str) -> dict[str, Any]:
        """Trusted control-path hook; it remains passive and never dispatches work."""
        return self.board.post(
            actor="conductor", kind=kind, content=content, source="conductor_control",
        )

    def publish_chat_result(
        self, chat_id: str, message_id: str, payload: dict[str, Any],
    ) -> dict[str, Any]:
        """Publish an exact successful provider response through the trusted bridge.

        The caller chooses only the semantic kind. Identity, content, and provenance
        come from the completed monitored run and cannot be supplied by the request.
        """
        unexpected = set(payload) - {"kind"}
        if unexpected:
            raise BoardError(f"unsupported run publication field: {sorted(unexpected)[0]}")
        kind = payload.get("kind", "result")
        with self.store.lock:
            chat = self.store.get(chat_id)
            message = deepcopy(self._message(chat, message_id))
        if message.get("role") != "assistant":
            raise BoardError("only an assistant response may be published")
        if message.get("pending"):
            raise BoardError("a running response cannot be published")
        if message.get("cancelled") or message.get("interrupted"):
            raise BoardError("an incomplete response cannot be published")
        if message.get("error") or message.get("exit_code") != 0:
            raise BoardError("only a successful response may be published")
        actor = message.get("provider")
        run_id = message.get("run_id")
        if actor not in MODEL_ACTORS or not run_id:
            raise BoardError("response has no authenticated monitored-run provenance")
        event, created = self.board.publish_run_result(
            actor=actor,
            kind=kind,
            content=message.get("content"),
            run_id=run_id,
            chat_id=chat_id,
            message_id=message_id,
        )
        with self.store.lock:
            stored = self._message(self.store.get(chat_id), message_id)
            if stored.get("board_event_id") != event["id"]:
                stored["board_event_id"] = event["id"]
                self.store.save()
        return {"event": event, "created": created}

    def acknowledge_board_event(self, event_id: str) -> dict[str, Any]:
        return self.board.acknowledge(event_id, actor="chris", source="local_web")

    def create_chat(self, payload: dict[str, Any]) -> dict[str, Any]:
        provider = str(payload.get("provider") or "auto")
        if provider not in ("auto", "qwen", "claude", "codex"):
            raise ValueError("provider must be auto, qwen, claude, or codex")
        cwd = Path(str(payload.get("cwd") or self.default_cwd)).expanduser().resolve()
        if not cwd.is_dir():
            raise ValueError(f"project folder does not exist: {cwd}")
        return self.store.create(cwd, provider)

    def delete_chat(self, chat_id: str) -> None:
        with self.runs_lock:
            if chat_id in self.runs:
                raise ValueError("cancel the running response before deleting this conversation")
            self.store.delete(chat_id)

    def send_message(self, chat_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Persist a turn and start it in the background.

        The POST returns immediately. The browser observes completion through
        /api/state, so closing a tab or losing one fetch cannot orphan the UI.
        """
        prompt = str(payload.get("content") or "").strip()
        if not prompt:
            raise ValueError("message cannot be empty")
        active = ActiveRun()
        with self.runs_lock:
            if chat_id in self.runs:
                raise ValueError("this conversation is already running")
            with self.store.lock:
                chat = self.store.get(chat_id)
                if any(message.get("pending") for message in chat["messages"]):
                    raise ValueError("this conversation is already running")
                requested_provider = str(
                    payload.get("provider") or chat.get("requested_provider") or "auto"
                )
                if requested_provider not in ("auto", "qwen", "claude", "codex"):
                    raise ValueError("provider must be auto, qwen, claude, or codex")
                chat["requested_provider"] = requested_provider
                if not chat["messages"]:
                    requested_cwd = Path(
                        str(payload.get("cwd") or chat["cwd"])
                    ).expanduser().resolve()
                    if not requested_cwd.is_dir():
                        raise ValueError(f"project folder does not exist: {requested_cwd}")
                    chat["cwd"] = str(requested_cwd)
                now = int(time.time())
                if not chat["messages"]:
                    chat["title"] = " ".join(prompt.split())[:54]
                chat["messages"].append({
                    "id": uuid.uuid4().hex, "role": "user", "content": prompt,
                    "created_at": now,
                })
                pending = {
                    "id": uuid.uuid4().hex, "role": "assistant", "content": "",
                    "created_at": now, "pending": True, "run_id": uuid.uuid4().hex,
                }
                chat["messages"].append(pending)
                chat["updated_at"] = now
                self.store.save()
                public = self.store.public(chat)
            self.runs[chat_id] = active
        thread = threading.Thread(
            target=self._run_message,
            args=(chat_id, pending["id"], prompt, active),
            name=f"conductor-{chat_id[:8]}",
            daemon=True,
        )
        active.thread = thread
        try:
            thread.start()
        except Exception as error:
            with self.runs_lock:
                self.runs.pop(chat_id, None)
            with self.store.lock:
                chat = self.store.get(chat_id)
                failed = self._message(chat, pending["id"])
                failed.update({"content": f"Conductor error: {error}", "error": True})
                failed.pop("pending", None)
                self.store.save()
            raise
        return public

    def _run_message(
        self,
        chat_id: str,
        pending_id: str,
        prompt: str,
        active: ActiveRun,
    ) -> None:
        budgets: dict[str, ProviderBudget] = {}
        requested = "auto"
        route_reason = "manual provider selection"
        constraint_note = None
        routed_by_qwen = False
        provider: str | None = None
        result: RunResult | None = None
        try:
            with self.store.lock:
                chat = self.store.get(chat_id)
                requested = chat.get("requested_provider", "auto")
                current_provider = chat.get("provider")
                session_id = chat.get("provider_session_id")
                qwen_messages = list(chat.get("qwen_messages") or [])
                cwd = Path(chat["cwd"])

            budgets = self.budgets()
            if requested != "auto":
                provider = requested
                budget = budgets[provider]
                if not budget.available:
                    raise RuntimeError(budget.note or f"{provider} is unavailable")
                if provider == "qwen":
                    ensure_qwen(self.config)
            else:
                ensure_qwen(self.config)
                budgets["qwen"] = ProviderBudget("qwen", True, note="local; no subscription quota")
                decision = ask_qwen(prompt, budgets, self.config, current_provider)
                provider, constraint_note = enforce_constraints(decision, budgets, self.config)
                route_reason = decision.reason
                routed_by_qwen = True

            conversation = Conversation(
                provider=provider,
                provider_session_id=session_id if provider == current_provider else None,
                qwen_messages=qwen_messages if provider == "qwen" and provider == current_provider else [],
            )
            try:
                result = capture_dispatch(
                    provider, prompt, cwd, conversation, self.config, active.cancel_event,
                )
            except FileNotFoundError as error:
                if not routed_by_qwen or provider == "qwen":
                    raise
                result = RunResult("", 1, error=str(error), unavailable=True)
            if result.unavailable and routed_by_qwen and provider != "qwen":
                failed_provider = provider
                ensure_qwen(self.config)
                provider = "qwen"
                route_reason = f"{route_reason}; {failed_provider} was unavailable, so Qwen handled it"
                constraint_note = f"{failed_provider} could not start the turn"
                conversation = Conversation(provider="qwen")
                result = capture_dispatch(
                    "qwen", prompt, cwd, conversation, self.config, active.cancel_event,
                )
            if result.exit_code == 0 and result.session_id:
                conversation.provider_session_id = result.session_id
            content = result.text or result.error or f"{provider.title()} exited without a response."
            if result.exit_code and result.error and result.text:
                content += f"\n\n{result.error}"
            with self.store.lock:
                chat = self.store.get(chat_id)
                pending = self._message(chat, pending_id)
                pending.update({
                    "content": content,
                    "provider": provider,
                    "route_reason": route_reason,
                    "routed_by_qwen": routed_by_qwen,
                    "constraint_note": constraint_note,
                    "exit_code": result.exit_code,
                    "error": bool(result.exit_code),
                })
                pending.pop("pending", None)
                pending.pop("cancel_requested", None)
                if result.exit_code == 0:
                    chat["provider"] = provider
                    chat["provider_session_id"] = conversation.provider_session_id
                    chat["qwen_messages"] = conversation.qwen_messages
            try:
                append_run(
                    self.config["ledger"], provider=provider, prompt=prompt, cwd=cwd,
                    session_id=conversation.provider_session_id, budgets=budgets,
                    exit_code=result.exit_code, run_id=pending["run_id"],
                    chat_id=chat_id, message_id=pending_id,
                )
            except OSError as error:
                print(f"[web] could not append run ledger: {error}")
        except RunCancelled:
            with self.store.lock:
                chat = self.store.get(chat_id)
                pending = self._message(chat, pending_id)
                pending.update({"content": "Cancelled.", "cancelled": True, "exit_code": 130})
                pending.pop("pending", None)
                pending.pop("cancel_requested", None)
        except Exception as exc:
            with self.store.lock:
                chat = self.store.get(chat_id)
                pending = self._message(chat, pending_id)
                pending.update({
                    "content": f"Conductor error: {exc}", "provider": provider,
                    "error": True, "exit_code": 1,
                })
                pending.pop("pending", None)
                pending.pop("cancel_requested", None)
        finally:
            with self.store.lock:
                chat["updated_at"] = int(time.time())
                self.store.save()
            with self.runs_lock:
                if self.runs.get(chat_id) is active:
                    self.runs.pop(chat_id, None)

    @staticmethod
    def _message(chat: dict[str, Any], message_id: str) -> dict[str, Any]:
        for message in chat["messages"]:
            if message.get("id") == message_id:
                return message
        raise KeyError(message_id)

    def cancel_message(self, chat_id: str) -> dict[str, Any]:
        with self.runs_lock:
            active = self.runs.get(chat_id)
            with self.store.lock:
                chat = self.store.get(chat_id)
                pending = next((item for item in chat["messages"] if item.get("pending")), None)
                if pending is None:
                    raise ValueError("this conversation is not running")
                pending["cancel_requested"] = True
                self.store.save()
                public = self.store.public(chat)
            if active is not None:
                active.cancel_event.set()
            else:
                # No in-memory worker owns this persisted job. Recover it now.
                with self.store.lock:
                    chat = self.store.get(chat_id)
                    pending = next(item for item in chat["messages"] if item.get("pending"))
                    pending.update({"content": "Cancelled.", "cancelled": True, "exit_code": 130})
                    pending.pop("pending", None)
                    pending.pop("cancel_requested", None)
                    chat["updated_at"] = int(time.time())
                    self.store.save()
                    public = self.store.public(chat)
        return public

    def shutdown(self, timeout: float = 3) -> None:
        with self.runs_lock:
            active = list(self.runs.values())
            for run in active:
                run.cancel_event.set()
        deadline = time.monotonic() + timeout
        for run in active:
            if run.thread is not None:
                run.thread.join(max(0, deadline - time.monotonic()))


def make_handler(app: ConductorApp) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "AIConductor/0.3"

        def log_message(self, fmt: str, *args: Any) -> None:
            print(f"[web] {self.address_string()} {fmt % args}")

        def _json(self, payload: Any, status: HTTPStatus = HTTPStatus.OK) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()
            self.wfile.write(body)

        def _read_json(self) -> dict[str, Any]:
            content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
            if content_type != "application/json":
                raise ValueError("Content-Type must be application/json")
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError as error:
                raise ValueError("invalid Content-Length") from error
            if length < 0:
                raise ValueError("invalid Content-Length")
            if length > 1_000_000:
                raise ValueError("request is too large")
            data = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(data, dict):
                raise ValueError("request body must be an object")
            return data

        def _local_request_allowed(self) -> bool:
            try:
                peer_is_local = ipaddress.ip_address(self.client_address[0]).is_loopback
            except ValueError:
                peer_is_local = False
            try:
                host = urlparse(f"//{self.headers.get('Host', '')}").hostname
                host_is_local = host is not None and _loopback_host(host)
            except ValueError:
                host_is_local = False
            return peer_is_local and host_is_local

        def _board_control_allowed(self) -> bool:
            """Require a local request, local Origin, and per-run CSRF token."""
            origin = self.headers.get("Origin")
            origin_is_local = True
            if origin:
                parsed = urlparse(origin)
                try:
                    origin_is_local = (
                        parsed.scheme == "http" and parsed.hostname is not None
                        and _loopback_host(parsed.hostname)
                    )
                except ValueError:
                    origin_is_local = False
            supplied = self.headers.get("X-Conductor-CSRF", "")
            return (
                self._local_request_allowed() and origin_is_local
                and hmac.compare_digest(supplied, app.csrf_token)
            )

        def _require_board_control(self) -> bool:
            if self._board_control_allowed():
                return True
            self._json({"error": "local board authorization failed"}, HTTPStatus.FORBIDDEN)
            return False

        def _asset(self, name: str, content_type: str) -> None:
            try:
                body = (ASSET_ROOT / name).read_bytes()
            except OSError:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Content-Security-Policy", (
                "default-src 'self'; base-uri 'none'; frame-ancestors 'none'; "
                "form-action 'self'; object-src 'none'; script-src 'self'; "
                "style-src 'self' 'unsafe-inline'"
            ))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            path = parsed.path
            if path.startswith("/api/") and not self._local_request_allowed():
                self._json({"error": "local API authorization failed"}, HTTPStatus.FORBIDDEN)
                return
            if path == "/":
                self._asset("index.html", "text/html; charset=utf-8")
            elif path == "/app.css":
                self._asset("app.css", "text/css; charset=utf-8")
            elif path == "/app.js":
                self._asset("app.js", "text/javascript; charset=utf-8")
            elif path == "/favicon.ico":
                self.send_response(HTTPStatus.NO_CONTENT)
                self.end_headers()
            elif path == "/api/state":
                self._json(app.state())
            elif path == "/api/budgets":
                self._json({name: value.as_dict() for name, value in app.budgets().items()})
            elif path == "/api/board":
                try:
                    raw_limit = parse_qs(parsed.query).get("limit", ["200"])[0]
                    self._json(app.board_state(int(raw_limit)))
                except (ValueError, BoardError) as exc:
                    self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            else:
                self.send_error(HTTPStatus.NOT_FOUND)

        def do_POST(self) -> None:
            try:
                path = urlparse(self.path).path
                if not self._local_request_allowed():
                    self._json({"error": "local API authorization failed"}, HTTPStatus.FORBIDDEN)
                    return
                parts = path.strip("/").split("/")
                board_post = path == "/api/board/events"
                board_acknowledgement = (
                    len(parts) == 5 and parts[:2] == ["api", "board"]
                    and parts[2] == "events" and parts[4] == "acknowledge"
                )
                run_publication = (
                    len(parts) == 6 and parts[:2] == ["api", "chats"]
                    and parts[3] == "messages" and parts[5] == "publish"
                )
                if (board_post or board_acknowledgement or run_publication) \
                        and not self._require_board_control():
                    return
                payload = self._read_json()
                if path == "/api/chats":
                    self._json(app.create_chat(payload), HTTPStatus.CREATED)
                    return
                if board_post:
                    self._json(app.post_board_message(payload), HTTPStatus.CREATED)
                    return
                if board_acknowledgement:
                    if payload:
                        raise BoardError("acknowledgement body must be empty")
                    try:
                        acknowledged = app.acknowledge_board_event(parts[3])
                    except KeyError:
                        self._json({"error": "board event not found"}, HTTPStatus.NOT_FOUND)
                        return
                    self._json(acknowledged, HTTPStatus.CREATED)
                    return
                if run_publication:
                    publication = app.publish_chat_result(parts[2], parts[4], payload)
                    status = HTTPStatus.CREATED if publication["created"] else HTTPStatus.OK
                    self._json(publication, status)
                    return
                if len(parts) == 4 and parts[:2] == ["api", "chats"] and parts[3] == "messages":
                    self._json(app.send_message(parts[2], payload), HTTPStatus.ACCEPTED)
                    return
                if len(parts) == 4 and parts[:2] == ["api", "chats"] and parts[3] == "cancel":
                    self._json(app.cancel_message(parts[2]))
                    return
                self.send_error(HTTPStatus.NOT_FOUND)
            except KeyError:
                self._json({"error": "conversation not found"}, HTTPStatus.NOT_FOUND)
            except (ValueError, json.JSONDecodeError) as exc:
                self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            except Exception as exc:
                print(f"[web] request failed: {type(exc).__name__}: {exc}")
                self._json({"error": f"Conductor error: {exc}"}, HTTPStatus.INTERNAL_SERVER_ERROR)

        def do_DELETE(self) -> None:
            try:
                if not self._local_request_allowed():
                    self._json({"error": "local API authorization failed"}, HTTPStatus.FORBIDDEN)
                    return
                parts = urlparse(self.path).path.strip("/").split("/")
                if len(parts) == 3 and parts[:2] == ["api", "chats"]:
                    app.delete_chat(parts[2])
                    self._json({"ok": True})
                    return
                self.send_error(HTTPStatus.NOT_FOUND)
            except KeyError:
                self._json({"error": "conversation not found"}, HTTPStatus.NOT_FOUND)

    return Handler


def serve(config: dict[str, Any], cwd: Path, *, open_browser: bool | None = None) -> int:
    web = config["web"]
    host, port = str(web["host"]), int(web["port"])
    if not _loopback_host(host):
        raise ValueError("web.host must be a loopback address; remote exposure is not supported")
    app = ConductorApp(config, cwd)
    server = ThreadingHTTPServer((host, port), make_handler(app))
    recovered = app.recover_interrupted()
    url = f"http://{host}:{port}"
    should_open = bool(web.get("open_browser", True) if open_browser is None else open_browser)
    if should_open:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    print(f"AI Conductor is running at {url}")
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Claude-style UI for Qwen, Claude Code, and Codex")
    parser.add_argument("--config")
    parser.add_argument("--cwd", default=str(Path.cwd()))
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args(argv)
    cwd = Path(args.cwd).expanduser().resolve()
    if not cwd.is_dir():
        raise SystemExit(f"not a directory: {cwd}")
    return serve(load_config(args.config), cwd, open_browser=not args.no_browser)


if __name__ == "__main__":
    raise SystemExit(main())
