"""Event and error mapping for CLI process mode."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from iac_code.cli.output_formats import stream_json_event_data
from iac_code.cli.process_protocol import ProcessFrameValidationError, SDKErrorPayload, SDKProcessRuntimeError
from iac_code.providers.manager import ProviderNotConfiguredError
from iac_code.types.stream_events import ErrorEvent


@dataclass(frozen=True)
class ProcessSerializedEvent:
    """Already-public event payload to embed in a process-mode stream_event frame."""

    payload: dict[str, Any]


class ProcessEventSerializer:
    """Serialize internal stream events to the public stream-json event shape."""

    def serialize(self, event: Any) -> dict:
        if isinstance(event, ProcessSerializedEvent):
            return event.payload
        return stream_json_event_data(event)


class ProcessErrorMapper:
    """Map runtime exceptions and error events to stable process-mode error payloads."""

    def from_event(self, event: ErrorEvent) -> SDKErrorPayload:
        return SDKErrorPayload(
            code="stream_error",
            message=event.error,
            retryable=event.is_retryable,
            error_id=event.error_id,
        )

    def from_exception(self, exc: BaseException) -> SDKErrorPayload:
        if isinstance(exc, SDKProcessRuntimeError):
            return exc.payload
        if isinstance(exc, ProcessFrameValidationError):
            return SDKErrorPayload(code=exc.code, message=exc.message, retryable=exc.retryable)
        if isinstance(exc, ProviderNotConfiguredError):
            return SDKErrorPayload(
                code="provider_not_configured",
                message=str(exc),
                retryable=False,
            )
        if isinstance(exc, asyncio.CancelledError):
            return SDKErrorPayload(code="turn_canceled", message="Turn canceled.", retryable=False)
        return SDKErrorPayload(code="internal_error", message=str(exc), retryable=False)
