"""Canonical pipeline failure payloads.

Pipeline errors are functional state: they must remain recoverable and are
projected only when copied to an A2A delivery or observability sink.
"""

from __future__ import annotations

import copy
import hashlib
from typing import Any

from iac_code.i18n import _
from iac_code.utils.public_errors import PublicError, public_exception_summary, sanitize_public_text


def public_error_from_exception(exc: BaseException, *, fallback_summary: str | None = None) -> PublicError:
    return public_error(
        message=str(exc),
        error_type=type(exc).__name__,
        fallback_summary=fallback_summary,
    )


def public_error(
    *,
    message: Any,
    error_type: str,
    fallback_summary: str | None = None,
    extra_details: dict[str, Any] | None = None,
) -> PublicError:
    """Build a structured canonical error without irreversible redaction."""

    if fallback_summary is None:
        fallback_summary = _("Unknown error")
    summary = str(message) if message is not None else ""
    summary = summary or fallback_summary
    digest = hashlib.sha256(f"{error_type}\0{summary}".encode("utf-8", errors="replace")).hexdigest()
    error_id = digest[:12]
    details: dict[str, Any] = {
        "type": error_type,
        "error_id": error_id,
        "traceback": _("Stack trace omitted from public event; see error_id."),
    }
    for key, value in (extra_details or {}).items():
        if value is not None:
            details[str(key)] = copy.deepcopy(value)
    return PublicError(summary=summary, details=details, error_id=error_id)


__all__ = [
    "PublicError",
    "public_error",
    "public_error_from_exception",
    "public_exception_summary",
    "sanitize_public_text",
]
