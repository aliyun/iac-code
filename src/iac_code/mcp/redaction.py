from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from iac_code.utils.public_errors import sanitize_public_text

_URL_TEXT_PATTERN = re.compile(r"\b[A-Za-z][A-Za-z0-9+.-]*://[^\s<>'\"]+")
_PRIVATE_MARKER_PATTERN = re.compile(r"\b[A-Za-z0-9_-]*PRIVATE[A-Za-z0-9_-]*MARKER[A-Za-z0-9_-]*\b", re.IGNORECASE)
_TERMINAL_OSC_PATTERN = re.compile(r"(?:\x1b\]|\x9d).*?(?:\x07|\x1b\\|\x9c|$)", re.DOTALL)
_TERMINAL_CSI_PATTERN = re.compile(r"(?:\x1b\[|\x9b)[0-?]*[ -/]*[@-~]")
_TERMINAL_ESCAPE_PATTERN = re.compile(r"\x1b[@-Z\\-_]")
_TERMINAL_CONTROL_PATTERN = re.compile(r"[\x00-\x08\x0b\x0c\x0d-\x1f\x7f-\x9f]")


def sanitize_mcp_public_text(value: object, *, fallback_summary: str | None = None) -> str:
    text = strip_mcp_terminal_control_sequences(value)
    text = _PRIVATE_MARKER_PATTERN.sub("[REDACTED]", text)
    return sanitize_public_text(_redact_url_userinfo_in_text(text), fallback_summary=fallback_summary)


def sanitize_mcp_public_data(value: Any, *, fallback_summary: str | None = None) -> Any:
    if isinstance(value, str):
        return sanitize_mcp_public_text(value, fallback_summary=fallback_summary)
    if isinstance(value, list):
        return [sanitize_mcp_public_data(item, fallback_summary=fallback_summary) for item in value]
    if isinstance(value, tuple):
        return [sanitize_mcp_public_data(item, fallback_summary=fallback_summary) for item in value]
    if isinstance(value, dict):
        sanitized: dict[Any, Any] = {}
        for key, item in value.items():
            sanitized_key = sanitize_mcp_public_text(key, fallback_summary="") if isinstance(key, str) else key
            sanitized[sanitized_key] = sanitize_mcp_public_data(item, fallback_summary=fallback_summary)
        return sanitized
    return value


def strip_mcp_terminal_control_sequences(value: object) -> str:
    text = str(value) if value is not None else ""
    stripped = _TERMINAL_OSC_PATTERN.sub("", text)
    stripped = _TERMINAL_CSI_PATTERN.sub("", stripped)
    stripped = _TERMINAL_ESCAPE_PATTERN.sub("", stripped)
    return _TERMINAL_CONTROL_PATTERN.sub("", stripped)


def _redact_url_userinfo_in_text(value: str) -> str:
    return _URL_TEXT_PATTERN.sub(lambda match: _redact_url_userinfo(match.group(0)), value)


def _redact_url_userinfo(value: str) -> str:
    trailing = ""
    while value and value[-1] in ".,;:!?)}":
        trailing = value[-1] + trailing
        value = value[:-1]
    try:
        parsed = urlsplit(value)
    except ValueError:
        return value + trailing
    if not parsed.scheme or not parsed.netloc or (parsed.username is None and parsed.password is None):
        return value + trailing
    hostname = parsed.hostname or ""
    if ":" in hostname and not hostname.startswith("["):
        hostname = "[{}]".format(hostname)
    try:
        port = parsed.port
    except ValueError:
        port = None
    netloc = "[REDACTED]@{}".format(hostname)
    if port is not None:
        netloc = "{}:{}".format(netloc, port)
    return urlunsplit((parsed.scheme, netloc, parsed.path, parsed.query, parsed.fragment)) + trailing
