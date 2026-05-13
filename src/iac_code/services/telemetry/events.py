"""EventEmitter — OTel Logs signal emitter."""

from __future__ import annotations

from typing import Any

from loguru import logger
from opentelemetry._logs import LogRecord, SeverityNumber

from iac_code.services.telemetry.attributes import AttributeBuilder


class EventEmitter:
    """Wraps an OTel Logger. Call `attach()` after SDK initialization."""

    def __init__(self, attributes: AttributeBuilder) -> None:
        self._attributes = attributes
        self._otel_logger: Any = None

    def attach(self, otel_logger: Any) -> None:
        """Install the OTel Logger (or a mock during tests)."""
        self._otel_logger = otel_logger

    def emit(self, event_name: str, metadata: dict[str, Any]) -> None:
        """Build a LogRecord and emit via the attached logger.

        No-op when no logger is attached (e.g., before bootstrap or in tests
        that don't need emission).
        """
        if self._otel_logger is None:
            return
        attrs: dict[str, Any] = {}
        attrs.update(self._attributes.build_resource())
        attrs.update(self._attributes.build_event(event_name))
        attrs.update({k: v for k, v in metadata.items() if v is not None})
        logger.debug("[event:export] {} {}", event_name, attrs)
        record = LogRecord(
            body=event_name,
            severity_number=SeverityNumber.INFO,
            attributes=attrs,
        )
        self._otel_logger.emit(record)
