"""Privacy-level and content-capture detection for telemetry."""

from __future__ import annotations

import os
from enum import Enum
from ipaddress import ip_address
from urllib.parse import urlparse

# =====================================================================
# Privacy level
# =====================================================================


class PrivacyLevel(Enum):
    DEFAULT = "default"
    NO_TELEMETRY = "no-telemetry"
    ESSENTIAL_TRAFFIC = "essential-traffic"


_TRUTHY = {"1", "true", "yes", "on"}
_LOCAL_TELEMETRY_ENDPOINT_ENVS = (
    "IAC_CODE_TELEMETRY_ENDPOINT",
    "IAC_CODE_TELEMETRY_TRACES_ENDPOINT",
    "IAC_CODE_TELEMETRY_METRICS_ENDPOINT",
    "IAC_CODE_TELEMETRY_LOGS_ENDPOINT",
    "OTEL_EXPORTER_OTLP_ENDPOINT",
    "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
    "OTEL_EXPORTER_OTLP_METRICS_ENDPOINT",
    "OTEL_EXPORTER_OTLP_LOGS_ENDPOINT",
)


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


def _is_local_build() -> bool:
    # Empty __release_date__ means unpackaged source (see setup.py); don't ship telemetry from dev runs.
    from iac_code import __release_date__

    return not __release_date__.strip()


def _is_local_endpoint(raw: str) -> bool:
    endpoint = raw.strip()
    if not endpoint:
        return False
    parsed = urlparse(endpoint)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return False
    if parsed.hostname.lower() == "localhost":
        return True
    try:
        return ip_address(parsed.hostname).is_loopback
    except ValueError:
        return False


def _has_local_telemetry_endpoint() -> bool:
    return any(_is_local_endpoint(os.environ.get(name, "")) for name in _LOCAL_TELEMETRY_ENDPOINT_ENVS)


def _local_telemetry_opt_in_enabled() -> bool:
    return _is_env_truthy("IAC_CODE_ENABLE_LOCAL_TELEMETRY") and _has_local_telemetry_endpoint()


def is_telemetry_endpoint_allowed(raw: str) -> bool:
    """Whether a configured OTLP endpoint may be used for this build."""
    return not _is_local_build() or _is_local_endpoint(raw)


def is_telemetry_disabled() -> bool:
    if _is_local_build() and not _local_telemetry_opt_in_enabled():
        return True
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
