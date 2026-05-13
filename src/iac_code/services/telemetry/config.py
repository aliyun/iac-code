"""Privacy-level and content-capture detection for telemetry."""

from __future__ import annotations

import os
from enum import Enum

# =====================================================================
# Privacy level
# =====================================================================


class PrivacyLevel(Enum):
    DEFAULT = "default"
    NO_TELEMETRY = "no-telemetry"
    ESSENTIAL_TRAFFIC = "essential-traffic"


_TRUTHY = {"1", "true", "yes", "on"}


def _is_env_truthy(name: str) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    return raw in _TRUTHY


def get_privacy_level() -> PrivacyLevel:
    """Most restrictive wins: ESSENTIAL_TRAFFIC > NO_TELEMETRY > DEFAULT."""
    if _is_env_truthy("IAC_CODE_DISABLE_NONESSENTIAL_TRAFFIC"):
        return PrivacyLevel.ESSENTIAL_TRAFFIC
    if _is_env_truthy("DISABLE_TELEMETRY"):
        return PrivacyLevel.NO_TELEMETRY
    return PrivacyLevel.DEFAULT


def is_telemetry_disabled() -> bool:
    return get_privacy_level() != PrivacyLevel.DEFAULT


def is_essential_traffic_only() -> bool:
    return get_privacy_level() == PrivacyLevel.ESSENTIAL_TRAFFIC


# =====================================================================
# Content capture mode (gen_ai message/tool content on spans)
# =====================================================================


class ContentCaptureMode(Enum):
    NO_CONTENT = "no_content"
    SPAN_ONLY = "span_only"
    EVENT_ONLY = "event_only"
    SPAN_AND_EVENT = "span_and_event"


def get_content_capture_mode() -> ContentCaptureMode:
    """Read OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT env var.

    Compatible with loongsuite-util-genai. Default: NO_CONTENT.
    """
    raw = os.environ.get("OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT", "").strip().upper()
    _mapping = {
        "SPAN_ONLY": ContentCaptureMode.SPAN_ONLY,
        "EVENT_ONLY": ContentCaptureMode.EVENT_ONLY,
        "SPAN_AND_EVENT": ContentCaptureMode.SPAN_AND_EVENT,
    }
    return _mapping.get(raw, ContentCaptureMode.NO_CONTENT)


def should_capture_content_on_span() -> bool:
    from iac_code.utils.log import is_debug_enabled

    if is_debug_enabled():
        return True
    mode = get_content_capture_mode()
    return mode in (ContentCaptureMode.SPAN_ONLY, ContentCaptureMode.SPAN_AND_EVENT)
