"""Observe effective A2A display events and new-session content without changing the wire."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any

from iac_code.a2a.display_timing import DisplayEventClassifier
from iac_code.services.first_token_timing import (
    RequestTiming,
    bind_timing,
    mark_timing,
    use_request_timing,
)
from iac_code.services.request_progress import is_progress_metadata

_MAX_FRAME_BYTES = 262_144


def observe_request_data(data: Any) -> None:
    if not isinstance(data, dict):
        return
    params = data.get("params", data)
    if not isinstance(params, dict):
        return
    message = params.get("message", params)
    if not isinstance(message, dict):
        return
    bind_timing(
        context_id=message.get("contextId", message.get("context_id")),
        task_id=message.get("taskId", message.get("task_id")),
        message_id=message.get("messageId", message.get("message_id")),
        iac_code_session_id=data.get("sessionId"),
        thread_id=data.get("threadId"),
        agui_run_id=data.get("runId"),
    )
    metadata = message.get("metadata")
    iac = metadata.get("iac_code") if isinstance(metadata, dict) else None
    if isinstance(iac, dict):
        mode = iac.get("run_mode", iac.get("runMode"))
        bind_timing(
            mode=mode if isinstance(mode, str) and mode in {"normal", "pipeline"} else None,
            pipeline_name=iac.get("pipeline_name"),
        )
    mark_timing("request_parsed", once=True)


def _pipeline_content(value: Any, *, text_only: bool = False) -> str | None:
    if isinstance(value, dict):
        data = value.get("data")
        event_type = value.get("eventType")
        if event_type in ("text_delta", "thinking_delta") and isinstance(data, dict):
            text = data.get("text") or data.get("delta")
            metadata_only = data.get("is_metadata_only", data.get("isMetadataOnly", False))
            if isinstance(text, str) and text and not metadata_only and (event_type == "text_delta" or not text_only):
                return "text" if event_type == "text_delta" else "thinking"
        for key in ("events", "envelopes"):
            if content := _pipeline_content(value.get(key), text_only=text_only):
                return content
    if isinstance(value, list):
        for item in value[:1024]:
            if content := _pipeline_content(item, text_only=text_only):
                return content
    return None


def _content_frame(
    value: Any, *, budget: int = 16, text_only: bool = False, container: str | None = None
) -> str | None:
    if not isinstance(value, dict) or budget <= 0:
        return None
    metadata = value.get("metadata")
    iac = metadata.get("iac_code") if isinstance(metadata, dict) else None
    if is_progress_metadata(iac):
        return None
    if not text_only:
        # Content summaries describe newly exposed content, excluding snapshots and receipts.
        if "task" in value or value.get("kind") == "task":
            return None
        metadata = value.get("metadata")
        iac = metadata.get("iac_code") if isinstance(metadata, dict) else None
        if isinstance(iac, dict) and (iac.get("inputReceived") or iac.get("mcpWarning")):
            return None
        parts = value.get("parts")
        if isinstance(parts, list) and any(
            isinstance(part, dict)
            and isinstance(part.get("data"), dict)
            and part["data"].get("kind") in ("permission_ack", "cloud_resource_selection_ack")
            for part in parts
        ):
            return None
    # AG-UI text deltas, excluding RUN_STARTED, thinking, status, and heartbeats.
    if value.get("type") == "TEXT_MESSAGE_CONTENT":
        return "text" if isinstance(value.get("delta"), str) and value["delta"] else None
    bind_timing(
        context_id=value.get("contextId", value.get("context_id")), task_id=value.get("taskId", value.get("task_id"))
    )
    # Normal A2A text uses this message ID; lifecycle/status messages use other IDs.
    message_id = value.get("messageId", value.get("message_id"))
    if (
        isinstance(message_id, str)
        and (
            message_id.endswith("-message")
            or container in (None, "result", "message")
            or value.get("kind") == "message"
        )
        and value.get("role") in {"agent", "ROLE_AGENT"}
    ):
        parts = value.get("parts")
        if isinstance(parts, list) and any(
            isinstance(p, dict) and isinstance(p.get("text"), str) and p["text"] for p in parts
        ):
            return "text"
    message = value.get("message")
    if (
        value.get("state") in ("TASK_STATE_COMPLETED", "completed")
        and isinstance(message, dict)
        and message.get("role") in ("agent", "ROLE_AGENT")
    ):
        parts = message.get("parts")
        if isinstance(parts, list) and any(
            isinstance(p, dict) and isinstance(p.get("text"), str) and p["text"] for p in parts
        ):
            return "text"
    metadata = value.get("metadata")
    if isinstance(metadata, dict):
        iac = metadata.get("iac_code")
        if isinstance(iac, dict):
            thinking = iac.get("thinking")
            if (
                not text_only
                and isinstance(thinking, dict)
                and not thinking.get("is_metadata_only", thinking.get("isMetadataOnly", False))
                and isinstance(thinking.get("text"), str)
            ):
                if thinking["text"]:
                    return "thinking"
            for key in ("pipeline", "pipelineBatch"):
                if content := _pipeline_content(iac.get(key), text_only=text_only):
                    return content
    # Only protocol containers; never descend into arbitrary tool results or history.
    for key in ("result", "statusUpdate", "status_update", "task", "status", "message"):
        child_container = "status_message" if key == "message" and (container == "status" or "state" in value) else key
        if content := _content_frame(value.get(key), budget=budget - 1, text_only=text_only, container=child_container):
            return content
    return None


@dataclass
class _FrameObservation:
    event_type: str | None = None
    content_type: str | None = None
    text: bool = False
    progress: bool = False

    def merge(self, other: _FrameObservation) -> None:
        self.event_type = self.event_type or other.event_type
        self.content_type = self.content_type or other.content_type
        self.text = self.text or other.text
        self.progress = self.progress or other.progress


def _progress_frame(value: Any, budget: int = 16) -> bool:
    if not isinstance(value, dict) or budget <= 0 or "task" in value or ("id" in value and "status" in value):
        return False
    metadata = value.get("metadata")
    if isinstance(metadata, dict) and is_progress_metadata(metadata.get("iac_code")):
        return True
    return any(
        _progress_frame(value.get(key), budget - 1) for key in ("result", "statusUpdate", "status_update", "status")
    )


class _FrameObserver:
    def __init__(self) -> None:
        self.buffer = bytearray()
        self.discarding = False
        self.terminal_result: str | None = None
        self.display = DisplayEventClassifier()

    def feed(self, chunk: bytes, *, sse: bool, final: bool) -> _FrameObservation:
        # Split before retaining, so an oversized body is never buffered wholesale.
        found = _FrameObservation()
        for piece in chunk.splitlines(keepends=True):
            if not self.discarding:
                if len(self.buffer) + len(piece) > _MAX_FRAME_BYTES:
                    self.buffer.clear()
                    self.discarding = True
                    bind_timing(wire_observation_incomplete=True)
                    mark_timing("wire_observation_skipped", once=True, result="oversized_frame")
                else:
                    self.buffer.extend(piece)
            if sse and (
                self.buffer.endswith(b"\n\n")
                or self.buffer.endswith(b"\r\n\r\n")
                or (self.discarding and piece.strip() == b"")
            ):
                if not self.discarding:
                    found.merge(self._parse(self.buffer, sse=True))
                self.buffer.clear()
                self.discarding = False
        if final and not sse and self.buffer and not self.discarding:
            found.merge(self._parse(self.buffer, sse=sse))
            self.buffer.clear()
        return found

    def _parse(self, frame: bytes | bytearray, *, sse: bool) -> _FrameObservation:
        try:
            payload = b"\n".join(line[5:].lstrip() for line in frame.splitlines() if line.startswith(b"data:"))
            data = json.loads(payload if sse else frame)
            self.terminal_result = _terminal_result(data) or self.terminal_result
            display_type = self.display.classify(data)
            if self.display.incomplete:
                bind_timing(wire_observation_incomplete=True)
                mark_timing("wire_observation_skipped", once=True, result="display_observation_limit")
            return _FrameObservation(
                event_type=display_type,
                content_type=_content_frame(data),
                text=_content_frame(data, text_only=True) == "text",
                progress=_progress_frame(data),
            )
        except (ValueError, TypeError, RecursionError):
            return _FrameObservation()


def _terminal_result(value: Any, budget: int = 16) -> str | None:
    if not isinstance(value, dict) or budget <= 0:
        return None
    if isinstance(value.get("error"), dict) or value.get("type") == "RUN_ERROR":
        return "failed"
    state = value.get("state")
    for result, states in (
        ("failed", ("TASK_STATE_FAILED", "failed")),
        ("cancelled", ("TASK_STATE_CANCELED", "canceled", "cancelled")),
        ("input_required", ("TASK_STATE_INPUT_REQUIRED", "input-required", "input_required")),
    ):
        if state in states:
            return result
    for key in ("result", "statusUpdate", "status_update", "task", "status"):
        if result := _terminal_result(value.get(key), budget - 1):
            return result
    return None


class FirstTokenTimingMiddleware:
    def __init__(self, app: Any, *, protocol: str = "a2a") -> None:
        self.app = app
        self.protocol = protocol

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http" or scope.get("path") in {"/health", "/iac-code/readiness"}:
            await self.app(scope, receive, send)
            return
        headers = dict(scope.get("headers", []))
        raw_id = headers.get(b"x-request-id", b"")
        request_id = raw_id.decode("ascii", errors="replace") if isinstance(raw_id, bytes) else None
        timing = RequestTiming(
            request_id,
            protocol=self.protocol,
            method=scope.get("method"),
            parent_call_id=headers.get(b"x-iac-parent-call-id", b"").decode("ascii", errors="replace"),
            request_kind="session_restore"
            if scope.get("path") == "/iac-code/session/ensure-restored"
            else self.protocol,
        )
        status: int | None = None
        sse = False
        json_body = False
        disconnected = False
        response_complete = False
        observer = _FrameObserver()

        async def observed_receive():
            nonlocal disconnected
            message = await receive()
            if message.get("type") == "http.disconnect":
                disconnected = not response_complete
            return message

        async def observed_send(message: dict[str, Any]) -> None:
            nonlocal status, sse, json_body, response_complete
            if message.get("type") == "http.response.start":
                status = message.get("status")
                content_type = dict(message.get("headers", [])).get(b"content-type", b"")
                sse = content_type.lower().startswith(b"text/event-stream")
                json_body = b"json" in content_type.lower()
                timing.record("response_started", http_status=status)
            observation = _FrameObservation()
            if timing.active and message.get("type") == "http.response.body" and (sse or json_body):
                try:
                    observation = observer.feed(
                        message.get("body", b""), sse=sse, final=not message.get("more_body", False)
                    )
                except Exception:
                    # A parser/diagnostic failure must not drop or delay a response.
                    pass
            is_a2a = timing.identity.get("request_kind") == "a2a"
            event_type = observation.event_type if is_a2a else None
            content_type = observation.content_type if is_a2a else None
            if event_type is not None:
                timing.record("first_event_send_started", once=True)
            if content_type is not None and timing.identity.get("is_new_session") is True:
                timing.record("first_content_send_started", once=True)
            if observation.text:
                timing.record("first_text_send_started", once=True)
            await send(message)
            if message.get("type") == "http.response.body" and not message.get("more_body", False):
                response_complete = True
            if message.get("type") == "http.response.body":
                if is_a2a and observation.progress:
                    timing.record("first_progress_send_completed", once=True)
                timing.record_send_completed(
                    event_type=event_type,
                    content_type=content_type,
                    text=observation.text,
                    result=observer.terminal_result,
                )
            if not timing.active:
                observer.buffer.clear()

        with use_request_timing(timing):
            timing.record("request_received")
            result = "completed"
            try:
                await self.app(scope, observed_receive, observed_send)
                result = (
                    "disconnected"
                    if disconnected
                    else "http_error"
                    if status and status >= 400
                    else observer.terminal_result or "completed"
                )
            except BaseException as error:
                result = "cancelled" if isinstance(error, asyncio.CancelledError) else "failed"
                timing.record("request_failed", error_type=type(error).__name__, result=result, force=True)
                raise
            finally:
                timing.record("request_completed", result=result, http_status=status, force=True)
                timing.summary(result)
                timing.closed = True
