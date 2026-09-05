"""Honest, secret-free evidence about which provider handled a response."""

from __future__ import annotations

import ipaddress
from typing import Any
from urllib.parse import urlsplit

from .config import redact_configured_secrets


def _endpoint_kind(hostname: str | None) -> str:
    host = (hostname or "").strip().lower().rstrip(".")
    if host == "localhost":
        return "loopback"
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        # A DNS name establishes neither where the endpoint is hosted nor
        # where inference occurs.  Do not turn an unverified hostname into a
        # claim that it is remote.
        return "unknown"
    if address.is_loopback:
        return "loopback"
    if address.is_private or address.is_link_local:
        return "local-network"
    return "remote"


def _origin(base_url: Any) -> tuple[str | None, str]:
    """Return only scheme/host/port; never expose configured URL path or query."""
    if not isinstance(base_url, str):
        return None, "unknown"
    try:
        parsed = urlsplit(base_url)
        hostname = parsed.hostname
        if parsed.scheme not in {"http", "https"} or not hostname:
            return None, "unknown"
        host = hostname.lower().rstrip(".")
        if ":" in host:
            host = f"[{host}]"
        port = parsed.port
        origin = f"{parsed.scheme.lower()}://{host}"
        if port is not None and not ((parsed.scheme == "http" and port == 80) or
                                     (parsed.scheme == "https" and port == 443)):
            origin += f":{port}"
        return origin, _endpoint_kind(hostname)
    except (TypeError, ValueError):
        return None, "unknown"


def _safe_value(config: dict[str, Any], value: Any) -> str:
    """Return public identity text after removing configured credential values."""
    return redact_configured_secrets(config, value)


def configured_identity(config: dict[str, Any], provider: str) -> dict[str, Any]:
    """Build identity evidence from configuration, without credentials or URL paths."""
    provider_config = config.get(provider) if isinstance(config.get(provider), dict) else {}
    identity: dict[str, Any] = {
        "provider": provider,
        "requested_model": _safe_value(config, provider_config.get("model") or ""),
        "endpoint_origin": None,
        "endpoint_kind": "cli",
        "reported_models": [],
        "model_mismatch": False,
    }
    if provider_config.get("base_url"):
        origin, kind = _origin(
            provider_config.get("base_url")
        )
        identity["endpoint_origin"] = _safe_value(config, origin) if origin else None
        identity["endpoint_kind"] = kind
    return identity


def record_reported_model(
    identity: dict[str, Any], value: Any, config: dict[str, Any] | None = None,
) -> None:
    """Add a bounded response model ID and compare it exactly to the request."""
    if not isinstance(value, str):
        return
    model = _safe_value(config, value).strip() if config is not None else value.strip()
    if not model or len(model) > 256:
        return
    models = identity.setdefault("reported_models", [])
    if not isinstance(models, list) or len(models) >= 24 or model in models:
        return
    models.append(model)
    requested = identity.get("requested_model")
    identity["model_mismatch"] = any(item != requested for item in models)
