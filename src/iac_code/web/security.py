"""Security helpers for the local Web workbench."""

from __future__ import annotations

import ipaddress
import re
from collections.abc import Mapping
from typing import Any

LOCALHOST = "localhost"
SECRET_KEY_PARTS = (
    "api_key",
    "apikey",
    "access_key_secret",
    "secret",
    "token",
    "authorization",
    "cookie",
    "private_key",
    "credential_uri",
)
NORMALIZED_SECRET_KEY_PARTS = tuple(re.sub(r"[^a-z0-9]", "", part.lower()) for part in SECRET_KEY_PARTS)


def _normalize_secret_key_text(key: str) -> str:
    return re.sub(r"[^a-z0-9]", "", key.lower())


def _looks_secret_key(key: str) -> bool:
    key_text = key.lower()
    normalized_key = _normalize_secret_key_text(key)
    return any(
        part in key_text or normalized_part in normalized_key
        for part, normalized_part in zip(SECRET_KEY_PARTS, NORMALIZED_SECRET_KEY_PARTS, strict=True)
    )


def ensure_loopback_host(host: str) -> str:
    """Return a normalized loopback host or raise for public bind addresses."""
    normalized = host.strip()
    if normalized.startswith("[") and normalized.endswith("]") and ":" in normalized:
        normalized = normalized[1:-1]
    if normalized == LOCALHOST:
        return normalized
    try:
        address = ipaddress.ip_address(normalized)
    except ValueError as exc:
        raise ValueError("local Web server only supports loopback hosts in this phase") from exc
    if not address.is_loopback:
        raise ValueError("local Web server only supports loopback hosts in this phase")
    return normalized


def redact_secrets(value: Any) -> Any:
    """Recursively redact values whose keys look credential-like."""
    if isinstance(value, Mapping):
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            if _looks_secret_key(key_text):
                redacted[key_text] = "[REDACTED]"
            else:
                redacted[key_text] = redact_secrets(item)
        return redacted
    if isinstance(value, list):
        return [redact_secrets(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_secrets(item) for item in value)
    return value
