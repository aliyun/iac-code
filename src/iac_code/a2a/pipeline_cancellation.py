"""Structured attribution for ``pipeline_canceled`` terminal events.

``pipeline_canceled`` used to carry only a localized display string
(``reason="Task canceled."``), so an audit could not tell a user-initiated stop
apart from an upstream timeout, an executor crash or a resource limit. This
module defines the machine-readable attribution that cancel call sites attach and
terminal publication merges into the event ``data``, while leaving the existing
``source`` and localized ``reason`` fields untouched for current consumers.
"""

from __future__ import annotations

from dataclasses import dataclass

from iac_code.utils.public_errors import sanitize_strict_text

CANCEL_REASON_USER_INITIATED = "user_initiated"
CANCEL_REASON_UPSTREAM_TIMEOUT = "upstream_timeout"
CANCEL_REASON_EXECUTOR_ERROR = "executor_error"
CANCEL_REASON_RESOURCE_LIMIT = "resource_limit"
CANCEL_REASON_UNKNOWN = "unknown"

CANCEL_REASON_CODES = frozenset(
    {
        CANCEL_REASON_USER_INITIATED,
        CANCEL_REASON_UPSTREAM_TIMEOUT,
        CANCEL_REASON_EXECUTOR_ERROR,
        CANCEL_REASON_RESOURCE_LIMIT,
        CANCEL_REASON_UNKNOWN,
    }
)

CANCEL_TRIGGER_USER = "user"
CANCEL_TRIGGER_SCHEDULER = "scheduler"
CANCEL_TRIGGER_SYSTEM = "system"

CANCEL_TRIGGER_SOURCES = frozenset({CANCEL_TRIGGER_USER, CANCEL_TRIGGER_SCHEDULER, CANCEL_TRIGGER_SYSTEM})

_DEFAULT_TRIGGER_BY_REASON = {
    CANCEL_REASON_USER_INITIATED: CANCEL_TRIGGER_USER,
    CANCEL_REASON_UPSTREAM_TIMEOUT: CANCEL_TRIGGER_SCHEDULER,
    CANCEL_REASON_EXECUTOR_ERROR: CANCEL_TRIGGER_SYSTEM,
    CANCEL_REASON_RESOURCE_LIMIT: CANCEL_TRIGGER_SYSTEM,
    CANCEL_REASON_UNKNOWN: CANCEL_TRIGGER_SYSTEM,
}

_DETAIL_MAX_CHARS = 200


@dataclass(frozen=True)
class PipelineCancellation:
    """Machine-readable attribution for one cancellation.

    ``detail`` is a short non-localized diagnostic hint (never user input) so
    audits stay stable across locales.
    """

    reason_code: str = CANCEL_REASON_UNKNOWN
    trigger_source: str = CANCEL_TRIGGER_SYSTEM
    detail: str | None = None

    def event_data(self) -> dict[str, str]:
        payload = {
            "reasonCode": self.reason_code,
            "triggerSource": self.trigger_source,
        }
        if self.detail:
            payload["detail"] = self.detail
        return payload


def pipeline_cancellation(
    reason_code: str | None = None,
    *,
    trigger_source: str | None = None,
    detail: str | None = None,
) -> PipelineCancellation:
    """Build a normalized attribution, falling back to ``unknown``/``system``."""

    normalized_reason = reason_code if reason_code in CANCEL_REASON_CODES else CANCEL_REASON_UNKNOWN
    if trigger_source in CANCEL_TRIGGER_SOURCES:
        normalized_trigger = trigger_source
    else:
        normalized_trigger = _DEFAULT_TRIGGER_BY_REASON[normalized_reason]
    return PipelineCancellation(
        reason_code=normalized_reason,
        trigger_source=normalized_trigger,
        detail=_normalized_detail(detail),
    )


def unknown_pipeline_cancellation() -> PipelineCancellation:
    """Attribution used when a cancel path did not declare its origin."""

    return PipelineCancellation()


def resolve_pipeline_cancellation(value: object) -> PipelineCancellation:
    """Coerce a recorded attribution back into a normalized value."""

    if isinstance(value, PipelineCancellation):
        return pipeline_cancellation(
            value.reason_code,
            trigger_source=value.trigger_source,
            detail=value.detail,
        )
    return unknown_pipeline_cancellation()


def cancellation_event_data(
    cancellation: object,
    *,
    base: dict[str, object] | None = None,
) -> dict[str, object]:
    """Merge structured attribution into an existing cancel event payload."""

    payload: dict[str, object] = dict(base or {})
    payload.update(resolve_pipeline_cancellation(cancellation).event_data())
    return payload


def _normalized_detail(detail: str | None) -> str | None:
    if not detail:
        return None
    sanitized = sanitize_strict_text(detail).strip()
    if not sanitized:
        return None
    return sanitized[:_DETAIL_MAX_CHARS]
