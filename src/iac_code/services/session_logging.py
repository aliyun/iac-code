"""Session lifecycle logging helpers."""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable, Mapping
from typing import Any
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)
_SAFE_LOG_VALUE_PATTERN = re.compile(r"^[A-Za-z0-9_.:/+-]+$")


def is_session_start_logging_enabled() -> bool:
    return logger.isEnabledFor(logging.INFO)


def log_session_start_safely(callback: Callable[[], None]) -> None:
    """Run session-start logging without letting diagnostics affect startup."""

    if not is_session_start_logging_enabled():
        return
    try:
        callback()
    except Exception:
        try:
            logger.debug("Session start logging failed", exc_info=True)
        except Exception:
            pass


def log_session_started(
    *,
    session_id: str,
    cwd: str | None = None,
    provider: str | None = None,
    provider_display: str | None = None,
    model: str | None = None,
    effort: str | None = None,
    thinking_enabled: bool | None = None,
    thinking_budget: int | None = None,
    max_completion_tokens: int | None = None,
    endpoint_origin: str | None = None,
    endpoint_custom: bool | None = None,
    stream_idle_timeout: float | None = None,
    max_turns: int | None = None,
    tool_count: int | None = None,
    mcp_server_count: int | None = None,
    permission_mode: str | None = None,
    provider_config_frozen: bool | None = None,
    a2a_safe_mode: bool | None = None,
    resume_message_count: int | None = None,
    external_services_enabled: bool | None = None,
    source: str | None = None,
) -> None:
    """Log non-sensitive settings that materially affect session performance."""

    fields: dict[str, Any] = {
        "session_id": session_id,
        "source": source,
        "cwd": cwd,
        "provider": provider,
        "provider_display": provider_display,
        "model": model,
        "effort": effort,
        "thinking_enabled": thinking_enabled,
        "thinking_budget": thinking_budget,
        "max_completion_tokens": max_completion_tokens,
        "endpoint_origin": _sanitize_endpoint_origin(endpoint_origin),
        "endpoint_custom": endpoint_custom,
        "stream_idle_timeout": stream_idle_timeout,
        "max_turns": max_turns,
        "tool_count": tool_count,
        "mcp_server_count": mcp_server_count,
        "permission_mode": permission_mode,
        "provider_config_frozen": provider_config_frozen,
        "a2a_safe_mode": a2a_safe_mode,
        "resume_message_count": resume_message_count,
        "external_services_enabled": external_services_enabled,
    }
    _log_session_lifecycle("Session started", fields)


def log_session_configured(
    *,
    session_id: str,
    cwd: str | None = None,
    provider: str | None = None,
    provider_display: str | None = None,
    model: str | None = None,
    effort: str | None = None,
    thinking_enabled: bool | None = None,
    thinking_budget: int | None = None,
    max_completion_tokens: int | None = None,
    endpoint_origin: str | None = None,
    endpoint_custom: bool | None = None,
    stream_idle_timeout: float | None = None,
    max_turns: int | None = None,
    tool_count: int | None = None,
    mcp_server_count: int | None = None,
    permission_mode: str | None = None,
    provider_config_frozen: bool | None = None,
    a2a_safe_mode: bool | None = None,
    resume_message_count: int | None = None,
    external_services_enabled: bool | None = None,
    source: str | None = None,
) -> None:
    """Log non-sensitive persisted session settings before a runtime exists."""

    fields: dict[str, Any] = {
        "session_id": session_id,
        "source": source,
        "cwd": cwd,
        "provider": provider,
        "provider_display": provider_display,
        "model": model,
        "effort": effort,
        "thinking_enabled": thinking_enabled,
        "thinking_budget": thinking_budget,
        "max_completion_tokens": max_completion_tokens,
        "endpoint_origin": _sanitize_endpoint_origin(endpoint_origin),
        "endpoint_custom": endpoint_custom,
        "stream_idle_timeout": stream_idle_timeout,
        "max_turns": max_turns,
        "tool_count": tool_count,
        "mcp_server_count": mcp_server_count,
        "permission_mode": permission_mode,
        "provider_config_frozen": provider_config_frozen,
        "a2a_safe_mode": a2a_safe_mode,
        "resume_message_count": resume_message_count,
        "external_services_enabled": external_services_enabled,
    }
    _log_session_lifecycle("Session configured", fields)


def log_session_started_from_provider_settings(
    *,
    session_id: str,
    provider_settings: Mapping[str, Any],
    cwd: str | None = None,
    max_turns: int | None = None,
    tool_count: int | None = None,
    mcp_server_count: int | None = None,
    permission_mode: str | None = None,
    provider_config_frozen: bool | None = None,
    a2a_safe_mode: bool | None = None,
    resume_message_count: int | None = None,
    external_services_enabled: bool | None = None,
    source: str | None = None,
) -> None:
    log_session_started(
        session_id=session_id,
        cwd=cwd,
        provider=_string_or_none(provider_settings.get("provider")),
        provider_display=_string_or_none(provider_settings.get("provider_display")),
        model=_string_or_none(provider_settings.get("model")),
        effort=_string_or_none(provider_settings.get("effort")),
        thinking_enabled=_bool_or_none(provider_settings.get("thinking_enabled")),
        thinking_budget=_int_or_none(provider_settings.get("thinking_budget")),
        max_completion_tokens=_int_or_none(provider_settings.get("max_completion_tokens")),
        endpoint_origin=_string_or_none(provider_settings.get("endpoint_origin")),
        endpoint_custom=_bool_or_none(provider_settings.get("endpoint_custom")),
        stream_idle_timeout=_float_or_none(provider_settings.get("stream_idle_timeout")),
        max_turns=max_turns,
        tool_count=tool_count,
        mcp_server_count=mcp_server_count,
        permission_mode=permission_mode,
        provider_config_frozen=provider_config_frozen,
        a2a_safe_mode=a2a_safe_mode,
        resume_message_count=resume_message_count,
        external_services_enabled=external_services_enabled,
        source=source,
    )


def _format_log_fields(fields: Mapping[str, Any]) -> str:
    return " ".join("{}={}".format(key, _format_log_value(value)) for key, value in fields.items())


def format_log_value(value: Any) -> str:
    return _format_log_value(value)


def _log_session_lifecycle(message: str, fields: Mapping[str, Any]) -> None:
    logger.info("%s %s", message, _format_log_fields(fields))


def _format_log_value(value: Any) -> str:
    if value is None:
        return "unset"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    text = str(value)
    if _SAFE_LOG_VALUE_PATTERN.fullmatch(text):
        return text
    return json.dumps(text, ensure_ascii=True)


def sanitize_endpoint_origin(value: str | None) -> str | None:
    return _sanitize_endpoint_origin(value)


def is_custom_endpoint(explicit_endpoint: str | None, default_endpoint: str | None) -> bool:
    if _sanitize_endpoint_origin(explicit_endpoint) is None:
        return False
    return _canonical_endpoint_text(explicit_endpoint) != _canonical_endpoint_text(default_endpoint)


def _sanitize_endpoint_origin(value: str | None) -> str | None:
    if not value:
        return None
    try:
        parsed = urlsplit(value.strip())
    except ValueError:
        return None
    if not parsed.scheme or not parsed.hostname:
        return None
    scheme = parsed.scheme.lower()
    hostname = parsed.hostname.lower()
    if ":" in hostname and not hostname.startswith("["):
        hostname = "[{}]".format(hostname)
    try:
        port = parsed.port
    except ValueError:
        port = None
    return "{}://{}{}".format(scheme, hostname, ":{}".format(port) if port is not None else "")


def _canonical_endpoint_text(value: str | None) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip().rstrip("/")
    if not text:
        return None
    try:
        parsed = urlsplit(text)
    except ValueError:
        return text
    if not parsed.scheme or not parsed.hostname:
        return text
    scheme = parsed.scheme.lower()
    hostname = parsed.hostname.lower()
    if ":" in hostname and not hostname.startswith("["):
        hostname = "[{}]".format(hostname)
    try:
        port = parsed.port
    except ValueError:
        port = None
    path = parsed.path.rstrip("/")
    return "{}://{}{}{}".format(scheme, hostname, ":{}".format(port) if port is not None else "", path)


def _string_or_none(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def _bool_or_none(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


def _int_or_none(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    return value if isinstance(value, int) else None


def _float_or_none(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (float, int)):
        return float(value)
    return None
