from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


PROVIDERS = ("qwen", "claude", "codex")


@dataclass(frozen=True)
class BudgetWindow:
    used_percent: float
    window_minutes: int | None = None
    resets_at: int | None = None

    @property
    def remaining_percent(self) -> float:
        return max(0.0, 100.0 - self.used_percent)

    def as_dict(self) -> dict[str, Any]:
        return {
            "used_percent": self.used_percent,
            "remaining_percent": self.remaining_percent,
            "window_minutes": self.window_minutes,
            "resets_at": self.resets_at,
        }


@dataclass(frozen=True)
class ProviderBudget:
    provider: str
    available: bool
    window: BudgetWindow | None = None
    observed_at: int | None = None
    note: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "available": self.available,
            "window": self.window.as_dict() if self.window else None,
            "observed_at": self.observed_at,
            "note": self.note,
        }


@dataclass(frozen=True)
class RouteDecision:
    backend: str
    alternates: tuple[str, ...]
    reason: str
    mode: str = "work"


@dataclass
class Conversation:
    provider: str | None = None
    provider_session_id: str | None = None
    qwen_messages: list[dict[str, Any]] = field(default_factory=list)

    def reset(self, provider: str | None = None) -> None:
        self.provider = provider
        self.provider_session_id = None
        self.qwen_messages.clear()
