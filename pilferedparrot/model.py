from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


PROVIDERS = ("qwen", "codex")


@dataclass(frozen=True)
class BudgetWindow:
    used_percent: float
    window_minutes: int | None = None
    resets_at: int | None = None
    label: str | None = None

    @property
    def remaining_percent(self) -> float:
        return max(0.0, min(100.0, 100.0 - self.used_percent))

    def as_dict(self) -> dict[str, Any]:
        return {
            "used_percent": self.used_percent,
            "remaining_percent": self.remaining_percent,
            "window_minutes": self.window_minutes,
            "resets_at": self.resets_at,
            "label": self.label,
        }


# Why a lane is unusable, so the UI can say something actionable instead of
# collapsing every cause into one word. Each maps to a different user fix.
STATUS_OK = "ok"
STATUS_CLI_MISSING = "cli_missing"
STATUS_SIGNED_OUT = "signed_out"
STATUS_AUTH_UNVERIFIED = "auth_unverified"

STATUS_LABELS = {
    STATUS_CLI_MISSING: "CLI not found",
    STATUS_SIGNED_OUT: "signed out",
    STATUS_AUTH_UNVERIFIED: "auth unverified",
}


@dataclass(frozen=True)
class ProviderBudget:
    provider: str
    available: bool
    window: BudgetWindow | None = None
    observed_at: int | None = None
    note: str | None = None
    status: str = STATUS_OK
    windows: tuple[BudgetWindow, ...] = ()

    @property
    def status_label(self) -> str:
        return STATUS_LABELS.get(self.status, "unavailable")

    def as_dict(self) -> dict[str, Any]:
        windows = self.windows or ((self.window,) if self.window else ())
        return {
            "provider": self.provider,
            "available": self.available,
            "window": self.window.as_dict() if self.window else None,
            "observed_at": self.observed_at,
            "note": self.note,
            "status": self.status,
            "windows": [window.as_dict() for window in windows],
        }


@dataclass
class Conversation:
    provider: str | None = None
    provider_session_id: str | None = None
    qwen_messages: list[dict[str, Any]] = field(default_factory=list)
    token_usage: dict[str, int] = field(default_factory=dict)

    def reset(self, provider: str | None = None) -> None:
        self.provider = provider
        self.provider_session_id = None
        self.qwen_messages.clear()
        self.token_usage.clear()
