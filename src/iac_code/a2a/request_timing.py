"""Observe request admission and completed text frames without changing the wire."""

from __future__ import annotations

import asyncio
import json
from typing import Any

from iac_code.services.first_token_timing import (
    RequestTiming,
    bind_timing,
    mark_timing,
    use_request_timing,
)

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


def _pipeline_text(value: Any) -> bool:
    if isinstance(value, dict):
        data = value.get("data")
        if value.get("eventType") == "text_delta" and isinstance(data, dict):
            text = data.get("text") or data.get("delta")
            return isinstance(text, str) and bool(text)
        return any(_pipeline_text(value.get(key)) for key in ("events", "envelopes"))
    if isinstance(value, list):
        return any(_pipeline_text(item) for item in value[:1024])
    return False


def _text_frame(value: Any, *, budget: int = 16) -> bool:
    if not isinstance(value, dict) or budget <= 0:
        return False
    # AG-UI text deltas, excluding RUN_STARTED, thinking, status, and heartbeats.
    if value.get("type") == "TEXT_MESSAGE_CONTENT":
        return isinstance(value.get("delta"), str) and bool(value["delta"])
    bind_timing(
        context_id=value.get("contextId", value.get("context_id")), task_id=value.get("taskId", value.get("task_id"))
    )
    # Normal A2A text uses this message ID; lifecycle/status messages use other IDs.
    message_id = value.get("messageId", value.get("message_id"))
    if isinstance(message_id, str) and message_id.endswith("-message") and value.get("role") in {"agent", "ROLE_AGENT"}:
        parts = value.get("parts")
        if isinstance(parts, list) and any(
            isinstance(p, dict) and isinstance(p.get("text"), str) and p["text"] for p in parts
        ):
            return True
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
            return True
    metadata = value.get("metadata")
    if isinstance(metadata, dict):
        iac = metadata.get("iac_code")
        if isinstance(iac, dict):
            if _pipeline_text(iac.get("pipeline")) or _pipeline_text(iac.get("pipelineBatch")):
                return True
    # Only protocol containers; never descend into arbitrary tool results or history.
    return any(
        _text_frame(value.get(key), budget=budget - 1)
        for key in ("result", "statusUpdate", "status_update", "task", "status", "message")
    )


class _FrameObserver:
    def __init__(self) -> None:
        self.buffer = bytearray()
        self.discarding = False
        self.terminal_result: str | None = None

    def feed(self, chunk: bytes, *, sse: bool, final: bool) -> bool:
        # Split before retaining, so an oversized body is never buffered wholesale.
        found = False
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
                    found = self._parse(self.buffer, sse=True) or found
                self.buffer.clear()
                self.discarding = False
        if final and not sse and self.buffer and not self.discarding:
            found = self._parse(self.buffer, sse=sse) or found
            self.buffer.clear()
        return found

    def _parse(self, frame: bytes | bytearray, *, sse: bool) -> bool:
        try:
            payload = b"\n".join(line[5:].lstrip() for line in frame.splitlines() if line.startswith(b"data:"))
            data = json.loads(payload if sse else frame)
            self.terminal_result = _terminal_result(data) or self.terminal_result
            return _text_frame(data)
        except (ValueError, TypeError, RecursionError):
            return False


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
            is_text = False
            if timing.active and message.get("type") == "http.response.body" and (sse or json_body):
                try:
                    is_text = observer.feed(
                        message.get("body", b""), sse=sse, final=not message.get("more_body", False)
                    )
                except Exception:
                    # A parser/diagnostic failure must not drop or delay a response.
                    pass
            if is_text:
                timing.record("first_text_send_started", once=True)
            await send(message)
            if message.get("type") == "http.response.body" and not message.get("more_body", False):
                response_complete = True
            if is_text:
                timing.record("first_text_send_completed", once=True)
                timing.summary("first_text_sent")
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
