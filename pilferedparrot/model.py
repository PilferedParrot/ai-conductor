from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


# Provider presentation and capabilities live beside the provider registry so the
# browser does not need to know which LLM integrations happen to be installed in
# this release. Native adapters live here; arbitrary compatible APIs are loaded
# from persisted provider definitions without another provider-specific UI branch.
PROVIDER_CATALOG: tuple[dict[str, Any], ...] = (
    {
        "id": "qwen",
        "label": "Local LLM",
        "initial": "L",
        "description": "A configured OpenAI-compatible local model server.",
        "auth_mode": "none",
        "auth_label": "No account required",
        "auth_help": "Connection stays on the configured local endpoint.",
    },
    {
        "id": "codex",
        "label": "OpenAI Codex",
        "initial": "O",
        "description": "OpenAI's coding agent through your local Codex CLI.",
        "auth_mode": "cli",
        "login_command": "codex login",
        "auth_help": "Sign-in opens in your default browser and stays with OpenAI.",
        "login_help": "Complete the OpenAI sign-in in your browser, then return here.",
    },
    {
        "id": "claude",
        "label": "Claude Code",
        "initial": "C",
        "description": "Anthropic's coding agent through your local Claude Code CLI.",
        "auth_mode": "cli",
        "login_command": "claude auth login",
        "auth_help": "Sign-in opens in your default browser and stays with Anthropic.",
        "login_help": "Complete the Anthropic sign-in at claude.ai in your browser, then return here.",
    },
    {
        "id": "gemini",
        "label": "Google Gemini",
        "initial": "G",
        "description": "Google's coding agent through the local Gemini CLI.",
        "auth_mode": "external_cli",
        "auth_label": "Uses your Gemini CLI sign-in",
        "auth_help": "Run gemini once in a terminal to choose or change Google sign-in.",
    },
)
PROVIDERS = tuple(item["id"] for item in PROVIDER_CATALOG)


def provider_catalog(config: dict[str, Any] | None = None, *, include_hidden: bool = False) \
        -> tuple[dict[str, Any], ...]:
    """Return built-in and user-added provider cards from one registry.

    Custom providers use the OpenAI-compatible adapter.  Keeping their public
    presentation in the same registry as the native CLI adapters lets the web
    UI stay provider-neutral while preserving the long-standing ``PROVIDERS``
    constant for command-line compatibility.
    """
    if config is None:
        return PROVIDER_CATALOG
    hidden = {
        str(value) for value in config.get("_hidden_providers", ())
        if isinstance(value, str)
    }
    result = [
        dict(item) for item in PROVIDER_CATALOG
        if include_hidden or item["id"] not in hidden
    ]
    definitions = config.get("provider_definitions")
    if isinstance(definitions, dict):
        for provider, raw in definitions.items():
            if not isinstance(provider, str) or not isinstance(raw, dict):
                continue
            if not include_hidden and provider in hidden:
                continue
            label = str(raw.get("label") or provider).strip() or provider
            api_key_env = str(raw.get("api_key_env") or "").strip()
            result.append({
                "id": provider,
                "label": label,
                "initial": label[:1].upper(),
                "description": str(raw.get("description") or
                                   "An OpenAI-compatible LLM endpoint."),
                "adapter": "openai_compatible",
                "auth_mode": "environment" if api_key_env else "none",
                "auth_label": f"Uses ${api_key_env}" if api_key_env else
                              "No API key configured",
                "auth_help": f"API access comes from ${api_key_env}; the key is never stored."
                             if api_key_env else
                             "This endpoint is configured without an API key.",
                "base_url": str(raw.get("base_url") or ""),
                "api_key_env": api_key_env,
            })
    return tuple(result)


def provider_ids(config: dict[str, Any] | None = None, *, include_hidden: bool = False) \
        -> tuple[str, ...]:
    return tuple(
        str(item["id"]) for item in provider_catalog(config, include_hidden=include_hidden)
    )


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

# Authentication and transport are independent facts. A provider can be signed
# in while its endpoint is down, or reachable without having an auth concept.
AUTH_SIGNED_IN = "signed_in"
AUTH_SIGNED_OUT = "signed_out"
AUTH_UNKNOWN = "auth_unknown"
AUTH_LOCAL_NO_AUTH = "local_no_auth"
REACHABLE = "reachable"
UNREACHABLE = "unreachable"

# Usage reporting is independent of authentication and execution readiness.
# In particular, a signed-in CLI may be fully usable even when its provider
# exposes no supported live allowance interface.
USAGE_AVAILABLE = "available"
USAGE_UNAVAILABLE = "unavailable"
USAGE_UNSUPPORTED = "unsupported"

STATUS_LABELS = {
    STATUS_CLI_MISSING: "CLI not found",
    STATUS_SIGNED_OUT: "signed out",
    STATUS_AUTH_UNVERIFIED: "auth unverified",
}


@dataclass(frozen=True)
class ProviderBudget:
    provider: str
    # Execution readiness. This deliberately does not imply live allowance
    # reporting; consult ``usage_status`` for that independent capability.
    available: bool
    window: BudgetWindow | None = None
    observed_at: int | None = None
    note: str | None = None
    status: str = STATUS_OK
    windows: tuple[BudgetWindow, ...] = ()
    auth_status: str = AUTH_UNKNOWN
    reachability: str = UNREACHABLE
    usage_status: str = USAGE_UNSUPPORTED
    usage_note: str | None = None

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
            "auth_status": self.auth_status,
            "reachability": self.reachability,
            "usage_status": self.usage_status,
            "usage_note": self.usage_note,
        }


@dataclass
class Conversation:
    provider: str | None = None
    provider_session_id: str | None = None
    messages: list[dict[str, Any]] = field(default_factory=list)
    token_usage: dict[str, int] = field(default_factory=dict)

    def reset(self, provider: str | None = None) -> None:
        self.provider = provider
        self.provider_session_id = None
        self.messages.clear()
        self.token_usage.clear()
