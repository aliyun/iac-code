"""AnalyticsSink — routes events through the privacy gate to the Events pipeline.

Design:
  - Before `activate()`, events are queued in-memory (bounded by maxlen=10k).
  - After `activate()`, events go directly to the EventEmitter.
  - `drain_sync()` / `drain_soon()` flushes the pre-queue once activated.
  - The privacy gate blocks emission when no-telemetry / essential-traffic is on.
"""

from __future__ import annotations

import asyncio
import re
from collections import deque
from threading import Lock
from typing import Any

from loguru import logger

from iac_code.services.telemetry.config import is_telemetry_disabled
from iac_code.services.telemetry.events import EventEmitter
from iac_code.services.telemetry.names import ALIYUN_API_TARGET_OUTCOMES, Events

_ALIYUN_API_CALLED_FIELDS = frozenset(
    {
        "metadata_source",
        "api_style",
        "http_method",
        "transport",
        "signature_scheme",
        "endpoint_source",
        "host_template_applied",
        "contract_override_used",
        "openmeta_cache_status",
        "outcome",
    }
)
_ALIYUN_API_CALLED_FINITE_FIELDS = {
    "metadata_source": frozenset({"fresh", "cache", "stale_cache", "explicit_fallback"}),
    "api_style": frozenset({"RPC", "ROA"}),
    "http_method": frozenset({"GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"}),
    "transport": frozenset({"tea", "acs1", "acs3_streaming", "oss_v4_sdk"}),
    "signature_scheme": frozenset({"acs1", "acs3", "oss_v4"}),
    "endpoint_source": frozenset(
        {"explicit", "override", "location", "catalog_region", "catalog_global", "override_pattern", "error"}
    ),
    "openmeta_cache_status": frozenset({"memory_fresh", "disk_fresh", "remote", "disk_stale", "negative_hit", "miss"}),
    "outcome": ALIYUN_API_TARGET_OUTCOMES,
}
_ALIYUN_PRODUCT_RESOLVED_FIELDS = frozenset(
    {"requested_product", "canonical_product", "match_strategy", "confidence", "outcome"}
)
_SAFE_TELEMETRY_PRODUCT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_ASCII_WHITESPACE = " \t\n\r\f\v"
_PRODUCT_MATCH_STRATEGIES = frozenset(
    {
        "exact_code",
        "trimmed_exact",
        "separator_normalized",
        "short_name",
        "builtin_alias",
        "single_edit",
        "separator_ambiguous",
        "alias_ambiguous",
        "single_edit_ambiguous",
        "not_found",
        "unavailable",
        "unverified",
    }
)
_PRODUCT_MATCH_CONFIDENCES = frozenset({"high", "medium", "none"})
_PRODUCT_MATCH_OUTCOMES = frozenset({"matched", "not_found", "error", "unverified"})


class AnalyticsSink:
    """Privacy-gated event router with pre-activation queue."""

    def __init__(self, emitter: EventEmitter, queue_max: int = 10_000) -> None:
        self._emitter = emitter
        self._queue: deque[tuple[str, dict[str, Any]]] = deque(maxlen=queue_max)
        self._lock = Lock()
        self._active = False

    def log_event(self, event_name: str, metadata: dict[str, Any]) -> None:
        """Queue the event if not yet active, else gate+emit directly."""
        _validate_event_contract(event_name, metadata)
        with self._lock:
            if not self._active:
                self._queue.append((event_name, metadata))
                return
        self._dispatch(event_name, metadata)

    def activate(self) -> None:
        """Mark the sink active. Idempotent. Does NOT drain — call drain_*()."""
        with self._lock:
            self._active = True

    def drain_sync(self) -> None:
        """Synchronously flush the pre-activation queue."""
        with self._lock:
            queued = list(self._queue)
            self._queue.clear()
        for name, meta in queued:
            self._dispatch(name, meta)

    def drain_soon(self) -> None:
        """Schedule an async drain on the running loop, if any. Else sync."""
        try:
            loop = asyncio.get_running_loop()
            loop.call_soon(self.drain_sync)
            return
        except RuntimeError:
            pass
        self.drain_sync()

    def _dispatch(self, event_name: str, metadata: dict[str, Any]) -> None:
        _validate_event_contract(event_name, metadata)
        logger.info("[event] {} {}", event_name, metadata)
        if is_telemetry_disabled():
            return
        self._emitter.emit(event_name, metadata)


def _validate_event_contract(event_name: str, metadata: dict[str, Any]) -> None:
    if event_name == Events.ALIYUN_API_LEGACY_CALLED:
        outcome = metadata.get("outcome")
        if set(metadata) != {"outcome"} or not isinstance(outcome, str) or outcome not in {"success", "failure"}:
            raise ValueError("invalid_aliyun_telemetry_event")
        return
    if event_name == Events.ALIYUN_PRODUCT_RESOLVED:
        if set(metadata) != _ALIYUN_PRODUCT_RESOLVED_FIELDS:
            raise ValueError("invalid_aliyun_telemetry_event")
        requested = metadata["requested_product"]
        canonical = metadata["canonical_product"]
        normalized_requested = requested.strip(_ASCII_WHITESPACE) if isinstance(requested, str) else ""
        if (
            not normalized_requested
            or len(requested) > 140
            or _SAFE_TELEMETRY_PRODUCT.fullmatch(normalized_requested) is None
        ):
            raise ValueError("invalid_aliyun_telemetry_event")
        if canonical != "" and (not isinstance(canonical, str) or _SAFE_TELEMETRY_PRODUCT.fullmatch(canonical) is None):
            raise ValueError("invalid_aliyun_telemetry_event")
        if (
            not isinstance(metadata["match_strategy"], str)
            or metadata["match_strategy"] not in _PRODUCT_MATCH_STRATEGIES
        ):
            raise ValueError("invalid_aliyun_telemetry_event")
        if not isinstance(metadata["confidence"], str) or metadata["confidence"] not in _PRODUCT_MATCH_CONFIDENCES:
            raise ValueError("invalid_aliyun_telemetry_event")
        if not isinstance(metadata["outcome"], str) or metadata["outcome"] not in _PRODUCT_MATCH_OUTCOMES:
            raise ValueError("invalid_aliyun_telemetry_event")
        return
    if event_name != Events.ALIYUN_API_CALLED:
        return
    if set(metadata) != _ALIYUN_API_CALLED_FIELDS:
        raise ValueError("invalid_aliyun_telemetry_event")
    if not isinstance(metadata["host_template_applied"], bool) or not isinstance(
        metadata["contract_override_used"], bool
    ):
        raise ValueError("invalid_aliyun_telemetry_event")
    if any(
        not isinstance(metadata[name], str) or metadata[name] not in allowed
        for name, allowed in _ALIYUN_API_CALLED_FINITE_FIELDS.items()
    ):
        raise ValueError("invalid_aliyun_telemetry_event")
