from __future__ import annotations

import fcntl
import json
import os
import re
import unicodedata
import uuid
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from typing import Any


ACTORS = frozenset({"chris", "conductor", "claude", "qwen", "codex"})
MODEL_ACTORS = frozenset({"claude", "qwen", "codex"})
PUBLIC_KINDS = frozenset({
    "observation", "proposal", "decision", "result", "question", "status",
    "security_report", "assignment",
})
MODEL_KINDS = frozenset({
    "observation", "proposal", "result", "question", "status", "security_report",
})
INTERNAL_KINDS = frozenset({"acknowledgement"})
SOURCES = frozenset({"local_web", "conductor_control", "provider_run", "board_guard"})
MAX_CONTENT_CHARS = 2_000
MAX_STORE_BYTES = 10_000_000

_HTML = re.compile(r"<\s*/?\s*[a-z!][^>]*>", re.IGNORECASE)
_ENCODED = re.compile(r"(?:[A-Za-z0-9+/]{120,}={0,2}|[A-Fa-f0-9]{160,})")
_LONG_TOKEN = re.compile(r"\S{301,}")
_EVENT_ID = re.compile(r"[a-f0-9]{32}\Z")
_TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z\Z")
_SUSPICIOUS = (
    (re.compile(r"\bignore\s+(?:all\s+|any\s+|the\s+|your\s+)?(?:previous|prior|above)\b", re.I),
     "prompt-override language"),
    (re.compile(r"\b(?:system|developer|assistant)\s+(?:message|prompt|instruction)s?\b", re.I),
     "prompt-channel language"),
    (re.compile(r"\b(?:you are now|act as|impersonate)\s+(?:claude|qwen|codex|conductor)\b", re.I),
     "participant impersonation"),
    (re.compile(
        r"\b(?:claude|qwen|codex|conductor)\b\s*[:,]?\s*.{0,32}\b"
        r"(?:execute|run|invoke|prompt|ignore|obey|read|write|delete|call)\b",
        re.I,
    ), "cross-participant instruction"),
)


class BoardError(ValueError):
    """A board request failed its trust or content boundary."""


class BoardCorruptionError(RuntimeError):
    """The append-only audit log cannot be read without losing history."""


class _ExistingRunPublication(Exception):
    """Internal signal carrying the event that already represents a provider run."""

    def __init__(self, event: dict[str, Any]):
        super().__init__(event["id"])
        self.event = event


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _validate_content(value: Any, *, trim: bool = True) -> str:
    if not isinstance(value, str):
        raise BoardError("content must be text")
    content = value.strip() if trim else value
    if not content.strip():
        raise BoardError("content cannot be empty")
    if len(content) > MAX_CONTENT_CHARS:
        raise BoardError(f"content exceeds {MAX_CONTENT_CHARS} characters")
    if unicodedata.normalize("NFC", content) != content:
        raise BoardError("content must use normalized Unicode")
    for character in content:
        category = unicodedata.category(character)
        if (category in {"Cc", "Cf"} and character != "\n") or category in {"Zl", "Zp"}:
            raise BoardError("content contains hidden or control characters")
    lowered = content.casefold()
    if "```" in content or "~~~" in content:
        raise BoardError("code fences are not allowed on the message board")
    if _HTML.search(content) or "javascript:" in lowered or "data:" in lowered:
        raise BoardError("HTML or active content is not allowed on the message board")
    if _ENCODED.search(content) or _LONG_TOKEN.search(content):
        raise BoardError("encoded or machine-oriented payloads are not allowed")
    return content


def _suspicion(content: str, *, actor: str, kind: str) -> str | None:
    for pattern, reason in _SUSPICIOUS:
        if pattern.search(content):
            # A trusted assignment may name a participant and describe work, but
            # prompt overrides and impersonation are never normal board content.
            if kind == "assignment" and actor in {"chris", "conductor"} \
                    and reason == "cross-participant instruction":
                continue
            return reason
    return None


class BoardStore:
    """Small append-only board with process and OS-level write serialization.

    This module deliberately has no dependency on routing, sessions, budgets, or
    provider dispatch. Its only side effect is appending UTF-8 JSON lines.
    """

    def __init__(self, path: Path):
        self.path = path
        self.lock = RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path.parent.chmod(0o700)
        if self.path.exists():
            self.path.chmod(0o600)
            self.list_events(limit=1)

    def list_events(self, *, limit: int = 200) -> list[dict[str, Any]]:
        if not isinstance(limit, int) or not 1 <= limit <= 500:
            raise BoardError("limit must be between 1 and 500")
        with self.lock:
            events = self._read_locked()
        return list(reversed(events[-limit:]))

    def post(
        self,
        *,
        actor: str,
        kind: str,
        content: Any,
        source: str,
    ) -> dict[str, Any]:
        if actor not in ACTORS:
            raise BoardError("unsupported board actor")
        if kind not in PUBLIC_KINDS:
            raise BoardError("unsupported board message kind")
        if source not in {"local_web", "conductor_control"}:
            raise BoardError("unsupported board source")
        if source == "local_web" and actor != "chris":
            raise BoardError("the local web interface may only author messages as Chris")
        if source == "conductor_control" and actor != "conductor":
            raise BoardError("the trusted control path may only author messages as Conductor")
        if kind == "assignment" and actor not in {"chris", "conductor"}:
            raise BoardError("only Chris or Conductor may author assignments")

        clean = _validate_content(content)
        reason = _suspicion(clean, actor=actor, kind=kind)
        event = self._event(
            actor=actor,
            kind=kind,
            content=clean,
            source=source,
            status="quarantined" if reason else ("open" if kind == "security_report" else "published"),
        )
        batch = [event]
        if reason:
            batch.append(self._event(
                actor="conductor",
                kind="security_report",
                content=f"Board guard quarantined event {event['id']}: {reason}.",
                source="board_guard",
                status="open",
                related_event_id=event["id"],
            ))
        self._append(batch)
        result = dict(event)
        if len(batch) == 2:
            result["security_event_id"] = batch[1]["id"]
        return result

    def publish_run_result(
        self,
        *,
        actor: str,
        kind: str,
        content: Any,
        run_id: str,
        chat_id: str,
        message_id: str,
    ) -> tuple[dict[str, Any], bool]:
        """Publish one exact, completed provider response at most once.

        ConductorApp authenticates the run against its chat store and derives every
        argument except ``kind``. This store method fixes the source and makes the
        run-id claim atomic across processes; it is not a general model-post API.
        """
        if actor not in MODEL_ACTORS:
            raise BoardError("only a monitored model run may use the provider bridge")
        if kind not in MODEL_KINDS:
            raise BoardError("unsupported model board message kind")
        for label, value in (
            ("run", run_id), ("chat", chat_id), ("message", message_id),
        ):
            if not isinstance(value, str) or not _EVENT_ID.fullmatch(value):
                raise BoardError(f"invalid related {label} ID")

        clean = _validate_content(content, trim=False)
        reason = _suspicion(clean, actor=actor, kind=kind)
        event = self._event(
            actor=actor,
            kind=kind,
            content=clean,
            source="provider_run",
            status="quarantined" if reason else ("open" if kind == "security_report" else "published"),
            related_run_id=run_id,
            related_chat_id=chat_id,
            related_message_id=message_id,
        )
        batch = [event]
        if reason:
            batch.append(self._event(
                actor="conductor",
                kind="security_report",
                content=f"Board guard quarantined event {event['id']}: {reason}.",
                source="board_guard",
                status="open",
                related_event_id=event["id"],
            ))

        def refuse_duplicate(events: list[dict[str, Any]]) -> None:
            existing = next((
                item for item in events
                if item.get("source") == "provider_run"
                and item.get("related_run_id") == run_id
            ), None)
            if existing is not None:
                raise _ExistingRunPublication(existing)

        try:
            with self.lock:
                self._append_locked(batch, validate_current=refuse_duplicate)
        except _ExistingRunPublication as existing:
            return dict(existing.event), False
        result = dict(event)
        if len(batch) == 2:
            result["security_event_id"] = batch[1]["id"]
        return result, True

    def acknowledge(self, event_id: str, *, actor: str, source: str) -> dict[str, Any]:
        if actor not in {"chris", "conductor"}:
            raise BoardError("only Chris or Conductor may acknowledge security events")
        if (source, actor) not in {
            ("local_web", "chris"), ("conductor_control", "conductor"),
        }:
            raise BoardError("unsupported board source")
        if not isinstance(event_id, str) or not _EVENT_ID.fullmatch(event_id):
            raise BoardError("invalid board event ID")
        acknowledgement = self._event(
            actor=actor,
            kind="acknowledgement",
            content="Security event acknowledged without executing its content.",
            source=source,
            status="recorded",
            related_event_id=event_id,
        )

        def validate(events: list[dict[str, Any]]) -> None:
            target = next((event for event in events if event["id"] == event_id), None)
            if target is None:
                raise KeyError(event_id)
            if target["status"] not in {"quarantined", "open"}:
                raise BoardError("only quarantined messages or open security reports can be acknowledged")
            if any(event.get("kind") == "acknowledgement"
                   and event.get("related_event_id") == event_id for event in events):
                raise BoardError("this event has already been acknowledged")

        with self.lock:
            self._append_locked([acknowledgement], validate_current=validate)
        return acknowledgement

    @staticmethod
    def _event(
        *,
        actor: str,
        kind: str,
        content: str,
        source: str,
        status: str,
        related_event_id: str | None = None,
        related_run_id: str | None = None,
        related_chat_id: str | None = None,
        related_message_id: str | None = None,
    ) -> dict[str, Any]:
        event: dict[str, Any] = {
            "id": uuid.uuid4().hex,
            "created_at": _timestamp(),
            "actor": actor,
            "kind": kind,
            "source": source,
            "status": status,
            "content": content,
        }
        if related_event_id is not None:
            event["related_event_id"] = related_event_id
        if related_run_id is not None:
            event["related_run_id"] = related_run_id
        if related_chat_id is not None:
            event["related_chat_id"] = related_chat_id
        if related_message_id is not None:
            event["related_message_id"] = related_message_id
        return event

    def _open(self, flags: int) -> int:
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(self.path, flags, 0o600)
        os.fchmod(descriptor, 0o600)
        return descriptor

    def _read_locked(self) -> list[dict[str, Any]]:
        try:
            descriptor = self._open(os.O_RDONLY)
        except FileNotFoundError:
            return []
        try:
            fcntl.flock(descriptor, fcntl.LOCK_SH)
            return self._read_descriptor(descriptor)
        finally:
            os.close(descriptor)

    def _read_descriptor(self, descriptor: int) -> list[dict[str, Any]]:
        size = os.fstat(descriptor).st_size
        if size > MAX_STORE_BYTES:
            raise BoardCorruptionError("board store exceeds its safety limit")
        os.lseek(descriptor, 0, os.SEEK_SET)
        chunks: list[bytes] = []
        remaining = size
        while remaining:
            chunk = os.read(descriptor, min(65_536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        try:
            lines = raw.decode("utf-8").splitlines()
            events = [json.loads(line) for line in lines if line]
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise BoardCorruptionError("board audit log is not valid UTF-8 JSONL") from error
        for event in events:
            if not self._valid_stored_event(event):
                raise BoardCorruptionError("board audit log contains an invalid event")
        return events

    @staticmethod
    def _valid_stored_event(event: Any) -> bool:
        if not isinstance(event, dict):
            return False
        required = {"id", "created_at", "actor", "kind", "source", "status", "content"}
        relation_fields = {
            "related_event_id", "related_run_id", "related_chat_id", "related_message_id",
        }
        allowed = required | relation_fields
        if not required.issubset(event) or not set(event).issubset(allowed):
            return False
        if not all(isinstance(event[key], str) for key in required):
            return False
        kind = event["kind"]
        valid_status = {
            "acknowledgement": {"recorded"},
            "security_report": {"published", "open", "quarantined"},
        }.get(kind, {"published", "quarantined"})
        valid = (
            bool(_EVENT_ID.fullmatch(event["id"]))
            and event["actor"] in ACTORS
            and kind in PUBLIC_KINDS | INTERNAL_KINDS
            and event["source"] in SOURCES
            and bool(_TIMESTAMP.fullmatch(event["created_at"]))
            and event["status"] in valid_status
        )
        if not valid:
            return False
        if any(
            value is not None and (
                not isinstance(value, str) or not _EVENT_ID.fullmatch(value)
            )
            for value in (event.get(field) for field in relation_fields)
        ):
            return False
        provider_relations = (
            event.get("related_run_id"), event.get("related_chat_id"),
            event.get("related_message_id"),
        )
        if event["source"] == "provider_run":
            return (
                event["actor"] in MODEL_ACTORS and kind in MODEL_KINDS
                and all(provider_relations) and event.get("related_event_id") is None
            )
        if any(provider_relations):
            return False
        if event["source"] == "local_web" and event["actor"] != "chris":
            return False
        if event["source"] == "conductor_control" and event["actor"] != "conductor":
            return False
        if event["source"] == "board_guard":
            return event["actor"] == "conductor" and kind == "security_report"
        return True

    def _append(self, events: list[dict[str, Any]]) -> None:
        with self.lock:
            self._append_locked(events)

    def _append_locked(self, events: list[dict[str, Any]], *, validate_current: Any = None) -> None:
        descriptor = self._open(os.O_RDWR | os.O_CREAT | os.O_APPEND)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            current = self._read_descriptor(descriptor)
            if validate_current is not None:
                validate_current(current)
            current_size = os.fstat(descriptor).st_size
            payload = b"".join(
                (json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
                for event in events
            )
            if current_size + len(payload) > MAX_STORE_BYTES:
                raise BoardError("board store has reached its safety limit")
            view = memoryview(payload)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    raise OSError("could not append the board event")
                view = view[written:]
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
