"""Content-free timing: a bounded in-memory timeline and a few INFO milestones."""

from __future__ import annotations

import asyncio
import contextvars
import inspect
import json
import logging
import math
import re
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timezone
from functools import wraps
from typing import Any, TypeVar, cast

from iac_code.services.request_progress import notify_progress

logger = logging.getLogger(__name__)
_SAFE_VALUE = re.compile(r"^[A-Za-z0-9_.:/+-]{1,256}$")
_MAX_TIMELINE_EVENTS = 512
# Emit each milestone once; new sessions also snapshot model content and its delivery.
# Repeated phases, LLM attempts, and pipeline steps remain in the summary only.
_CHAT_LOG_STAGES = frozenset(
    {
        "request_received",
        "llm_request_started",
        "llm_first_thinking_received",
        "llm_first_text_received",
        "first_event_send_completed",
        "first_content_send_completed",
        "first_text_send_completed",
    }
)
_RESTORE_LOG_STAGES = frozenset({"request_received", "backup_restore_started", "backup_restore_completed"})
_FIELDS = frozenset(
    {
        "context_id",
        "task_id",
        "iac_code_session_id",
        "message_id",
        "pipeline_run_id",
        "step_id",
        "sub_pipeline_id",
        "model",
        "provider",
        "llm_call_id",
        "llm_call_purpose",
        "llm_response_id",
        "attempt",
        "streaming",
        "result",
        "generation",
        "copied_files",
        "deleted_files",
        "enabled",
        "cache_hit",
        "http_status",
        "error_type",
        "protocol",
        "request_kind",
        "method",
        "phase_id",
        "duration_ms",
        "request_id_source",
        "restore_action",
        "thread_id",
        "agui_run_id",
        "ros_invocation_id",
        "mode",
        "pipeline_name",
        "parent_call_id",
        "wire_observation_incomplete",
        "is_new_session",
        "first_event_type",
        "first_content_type",
        "first_model_content_type",
        "file_kind",
    }
)
_current: contextvars.ContextVar[RequestTiming | None] = contextvars.ContextVar("iac_first_token_timing", default=None)
_phase_stack: contextvars.ContextVar[tuple[str, ...]] = contextvars.ContextVar("iac_timing_phases", default=())
_auxiliary_model_purpose: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "iac_auxiliary_model_timing_purpose", default=None
)
_F = TypeVar("_F", bound=Callable[..., Any])


def safe_id(value: object) -> str | None:
    return value if isinstance(value, str) and _SAFE_VALUE.fullmatch(value) else None


def _fields(values: dict[str, Any]) -> dict[str, str | int | float | bool]:
    result: dict[str, str | int | float | bool] = {}
    for key, value in values.items():
        if key not in _FIELDS:
            continue
        if isinstance(value, bool | int):
            result[key] = value
        elif isinstance(value, float) and math.isfinite(value):
            result[key] = value
        elif safe_id(value) is not None:
            result[key] = value
    return result


class RequestTiming:
    def __init__(
        self, request_id: str | None = None, *, clock_ns: Callable[[], int] | None = None, **fields: Any
    ) -> None:
        self.request_id = safe_id(request_id) or "local-" + uuid.uuid4().hex
        self.call_id = uuid.uuid4().hex
        self.clock_ns = clock_ns or time.monotonic_ns
        self.started_ns = self.clock_ns()
        self.identity = _fields(fields)
        self.identity["request_id_source"] = "upstream" if safe_id(request_id) else "local"
        self.milestones: dict[str, float] = {}
        self.phase_totals_ms: dict[str, float] = {}
        self.state_io_totals: dict[str, dict[str, int | float]] = {}
        self.timeline: list[dict[str, Any]] = []
        self.timeline_events_dropped = 0
        self._open_phases: dict[str, tuple[str, int]] = {}
        self._cutoff_ns: int | None = None
        self._cutoff_phases: dict[str, tuple[str, int]] = {}
        self._once: set[str] = set()
        self._emitted: set[str] = set()
        self._lock = threading.RLock()
        self._summarized = False
        self._first_event_summarized = False
        self._first_model_content_summarized = False
        self._first_content_summarized = False
        self.closed = False

    @property
    def active(self) -> bool:
        return not self.closed and not self._summarized and self._cutoff_ns is None

    @property
    def _new_a2a_session(self) -> bool:
        return self.identity.get("is_new_session") is True and self.identity.get("protocol") == "a2a"

    def bind(self, **fields: Any) -> None:
        with self._lock:
            for key, value in _fields(fields).items():
                self.identity.setdefault(key, value)

    def record(
        self, stage: str, *, once: bool = False, force: bool = False, _at_ns: int | None = None, **fields: Any
    ) -> None:
        with self._lock:
            if self.closed or self._summarized or (not self.active and not force) or (once and stage in self._once):
                return
            self._once.add(stage)
            now = self.clock_ns() if _at_ns is None else _at_ns
            elapsed = round(max(0, now - self.started_ns) / 1_000_000, 3)
            self.milestones.setdefault(stage, elapsed)
            if self._cutoff_ns is None and (
                stage == "request_completed"
                or (stage == "first_text_send_completed" and self.identity.get("protocol") != "a2a")
            ):
                self._cutoff_ns = now
                self._cutoff_phases = dict(self._open_phases)
            event = {"stage": stage, "elapsed_ms": elapsed, **_fields(fields)}
            if len(self.timeline) < _MAX_TIMELINE_EVENTS:
                self.timeline.append(event)
            else:
                self.timeline_events_dropped += 1
            log_stages = (
                _RESTORE_LOG_STAGES if self.identity.get("request_kind") == "session_restore" else _CHAT_LOG_STAGES
            )
            if stage in log_stages and stage not in self._emitted:
                self._emitted.add(stage)
                self._write(event)
            content_type = {
                "llm_first_text_received": "text",
                "llm_first_thinking_received": "thinking",
            }.get(stage)
            if stage == "llm_response_received":
                content_type = fields.get("first_model_content_type")
            if self._new_a2a_session and content_type is not None and not self._first_model_content_summarized:
                self.bind(first_model_content_type=content_type)
                self.record("first_model_content_received", once=True, _at_ns=now)
                self._first_model_content_summarized = True
                self._write_summary(
                    "first_model_content_summary", "first_model_content_received", elapsed, now, self._open_phases
                )
                self._finish_new_session_observation()

    def record_state_io(
        self, kind: str, *, elapsed_ms: float = 0, fsync_ms: float = 0, succeeded: bool = True, skipped: bool = False
    ) -> None:
        # Fixed categories only; never retain paths, data, or per-file cardinality.
        if kind not in {
            "task",
            "context",
            "execution_control",
            "pipeline_snapshot",
            "backup",
            "permission",
            "session_metadata",
            "other",
        }:
            kind = "other"
        with self._lock:
            if not self.active:
                return
            totals = self.state_io_totals.setdefault(
                kind, {"writes": 0, "skipped": 0, "failed": 0, "elapsed_ms": 0.0, "fsync_ms": 0.0}
            )
            totals["skipped" if skipped else "writes"] += 1
            if not succeeded:
                totals["failed"] += 1
            totals["elapsed_ms"] = round(totals["elapsed_ms"] + elapsed_ms, 3)
            totals["fsync_ms"] = round(totals["fsync_ms"] + fsync_ms, 3)

    def _write(self, fields: dict[str, Any]) -> None:
        try:
            logger.info(
                json.dumps(
                    {
                        "log_type": "first_token_timing",
                        "service": "iac-code",
                        "schema_version": 2,
                        "timestamp": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
                        "request_id": self.request_id,
                        "call_id": self.call_id,
                        **self.identity,
                        **fields,
                    },
                    ensure_ascii=True,
                    allow_nan=False,
                    separators=(",", ":"),
                )
            )
        except Exception:
            # Observability must not affect execution or transport delivery.
            pass

    def summary(self, result: str) -> None:
        """Snapshot completed phases and outstanding work; nested/parallel totals are not additive."""
        with self._lock:
            if self._summarized:
                return
            self._summarized = True
            elapsed = self.milestones.get(
                "first_content_send_completed",
                self.milestones.get(
                    "first_text_send_completed",
                    self.milestones.get(
                        "request_completed", round(max(0, self.clock_ns() - self.started_ns) / 1_000_000, 3)
                    ),
                ),
            )
            cutoff_ns = self._cutoff_ns if self._cutoff_ns is not None else self.clock_ns()
            open_phases = self._cutoff_phases if self._cutoff_ns is not None else self._open_phases
            if self.identity.get("protocol") == "a2a":
                elapsed = self.milestones.get("request_completed", round((cutoff_ns - self.started_ns) / 1_000_000, 3))
            if not self._first_event_summarized:
                self._write_summary("first_token_summary", result, elapsed, cutoff_ns, open_phases)
            if self._new_a2a_session:
                if not self._first_model_content_summarized:
                    self._write_summary("first_model_content_summary", result, elapsed, cutoff_ns, open_phases)
                if not self._first_content_summarized:
                    self._write_summary("first_content_summary", result, elapsed, cutoff_ns, open_phases)

    def record_send_completed(
        self,
        *,
        event_type: str | None = None,
        content_type: str | None = None,
        text: bool = False,
        result: str | None = None,
    ) -> None:
        """Snapshot successful wire delivery; all milestones in one send share its timestamp."""
        with self._lock:
            if not self.active:
                return
            now = self.clock_ns()
            if event_type is not None:
                self.bind(first_event_type=event_type)
                self.record("first_event_send_completed", once=True, _at_ns=now)
            if text:
                self.record("first_text_send_completed", once=True, force=True, _at_ns=now)
            new_content = content_type is not None and self._new_a2a_session
            if new_content:
                self.bind(first_content_type=content_type)
                self.record("first_content_send_completed", once=True, force=True, _at_ns=now)
            if event_type is not None and not self._first_event_summarized:
                self._first_event_summarized = True
                self._write_summary(
                    "first_token_summary",
                    result or "first_event_sent",
                    self.milestones["first_event_send_completed"],
                    now,
                    self._cutoff_phases if self._cutoff_ns is not None else self._open_phases,
                )
            if new_content and not self._first_content_summarized:
                self._first_content_summarized = True
                self._write_summary(
                    "first_content_summary",
                    "first_content_sent",
                    self.milestones["first_content_send_completed"],
                    now,
                    self._open_phases,
                )
            if self._new_a2a_session:
                self._finish_new_session_observation()
            elif "first_text_send_completed" in self.milestones and (
                self._first_event_summarized or self.identity.get("protocol") != "a2a"
            ):
                self._cutoff_ns = now
                self._cutoff_phases = dict(self._open_phases)
                self.summary("first_text_sent")

    def _finish_new_session_observation(self) -> None:
        if self._first_event_summarized and self._first_model_content_summarized and self._first_content_summarized:
            self._summarized = True

    def _write_summary(
        self,
        stage: str,
        result: str,
        elapsed: float,
        cutoff_ns: int,
        open_phases: dict[str, tuple[str, int]],
    ) -> None:
        observation_complete = not self.identity.get("wire_observation_incomplete", False)
        model_received = self.milestones.get("first_model_content_received")
        content_sent = self.milestones.get("first_content_send_completed")
        self._write(
            {
                "stage": stage,
                "result": result,
                "elapsed_ms": elapsed,
                "first_event_sent": "first_event_send_completed" in self.milestones,
                "first_progress_sent": "first_progress_send_completed" in self.milestones,
                **(
                    {"first_event_definition": "effective_display"}
                    if self.identity.get("request_kind") == "a2a"
                    else {}
                ),
                "first_event_observation_complete": observation_complete,
                "first_model_content_received": model_received is not None,
                "model_content_to_send_ms": round(content_sent - model_received, 3)
                if model_received is not None and content_sent is not None and content_sent >= model_received
                else None,
                "first_content_sent": "first_content_send_completed" in self.milestones,
                "first_content_observation_complete": observation_complete,
                "first_text_sent": "first_text_send_completed" in self.milestones,
                "first_text_observation_complete": observation_complete,
                "milestones_ms": dict(self.milestones),
                "phase_totals_ms": dict(self.phase_totals_ms),
                "state_io_totals": {kind: dict(totals) for kind, totals in self.state_io_totals.items()},
                "timeline": list(self.timeline),
                "timeline_events_dropped": self.timeline_events_dropped,
                "in_progress_phases": [
                    {
                        "phase_id": phase_id,
                        "stage": phase_stage,
                        "elapsed_ms": round(max(0, cutoff_ns - started) / 1_000_000, 3),
                    }
                    for phase_id, (phase_stage, started) in open_phases.items()
                ],
            }
        )


@contextmanager
def use_request_timing(timing: RequestTiming) -> Iterator[RequestTiming]:
    token = _current.set(timing)
    phases = _phase_stack.set(())
    try:
        yield timing
    finally:
        _phase_stack.reset(phases)
        _current.reset(token)


def current_timing() -> RequestTiming | None:
    return _current.get()


def bind_timing(**fields: Any) -> None:
    if timing := current_timing():
        timing.bind(**fields)


def mark_timing(stage: str, *, once: bool = False, **fields: Any) -> None:
    notify_progress(stage)
    if timing := current_timing():
        timing.record(stage, once=once, **fields)


@contextmanager
def auxiliary_model_timing(purpose: str) -> Iterator[None]:
    """Keep auxiliary inference in the timeline without claiming response-content milestones."""
    token = _auxiliary_model_purpose.set(purpose)
    try:
        yield
    finally:
        _auxiliary_model_purpose.reset(token)


@contextmanager
def timing_phase(stage: str, *, first_only: bool = False, **fields: Any) -> Iterator[dict[str, Any]]:
    notify_progress(stage + "_started")
    timing = current_timing()
    details = dict(fields)
    if timing is None or not timing.active or stage in _phase_stack.get():
        yield details
        return
    with timing._lock:
        skip = first_only and stage + "_started" in timing._once
        if not skip:
            timing._once.add(stage + "_started")
    if skip:
        yield details
        return
    phase_id = uuid.uuid4().hex
    started = timing.clock_ns()
    token = _phase_stack.set((*_phase_stack.get(), stage))
    with timing._lock:
        timing._open_phases[phase_id] = (stage, started)
    timing.record(stage + "_started", phase_id=phase_id, **details)
    result = "completed"
    try:
        yield details
    except BaseException as error:
        result = "cancelled" if isinstance(error, (asyncio.CancelledError, GeneratorExit)) else "failed"
        details["error_type"] = type(error).__name__
        raise
    finally:
        _phase_stack.reset(token)
        duration = round(max(0, timing.clock_ns() - started) / 1_000_000, 3)
        with timing._lock:
            timing._open_phases.pop(phase_id, None)
            if timing.active:
                timing.phase_totals_ms[stage] = round(timing.phase_totals_ms.get(stage, 0) + duration, 3)
        details["result"] = result
        timing.record(stage + "_completed", phase_id=phase_id, duration_ms=duration, **details)


@asynccontextmanager
async def timed_lock(lock: Any):
    with timing_phase("session_lock_wait"):
        await lock.acquire()
    try:
        yield
    finally:
        lock.release()


def timed_phase(stage: str, *, restore_result: bool = False) -> Callable[[_F], _F]:
    def decorate(function: _F) -> _F:
        def result_fields(result: Any, details: dict[str, Any]) -> None:
            if restore_result:
                details.update(
                    restore_action=getattr(result, "action", "unknown"),
                    enabled=getattr(result, "enabled", None),
                    generation=getattr(getattr(result, "state", None), "generation", None),
                    copied_files=getattr(result, "copied_files", None),
                    deleted_files=getattr(result, "deleted_files", None),
                )

        @wraps(function)
        def sync(*args: Any, **kwargs: Any) -> Any:
            with timing_phase(stage) as details:
                result = function(*args, **kwargs)
                result_fields(result, details)
                return result

        @wraps(function)
        async def asynchronous(*args: Any, **kwargs: Any) -> Any:
            with timing_phase(stage) as details:
                result = await function(*args, **kwargs)
                result_fields(result, details)
                return result

        return cast(_F, asynchronous if inspect.iscoroutinefunction(function) else sync)

    return decorate


class _TimedModelStream:
    def __init__(self, stream: Any, fields: dict[str, Any]) -> None:
        self.stream = stream
        self.fields: dict[str, Any] = fields
        from iac_code.services.telemetry.scope import get_span_attributes

        try:
            scope = get_span_attributes()
        except Exception:
            scope = {}
        self.fields = {**_fields(scope), "mode": scope.get("iac_code.mode"), **fields}
        purpose = _auxiliary_model_purpose.get()
        self.stage_prefix = f"{purpose}_" if purpose else "llm_"
        if purpose:
            self.fields["llm_call_purpose"] = purpose
        self.timing = current_timing()
        self.started: int | None = None
        self.seen: set[str] = set()

    def __aiter__(self):
        return self

    async def __anext__(self):
        assert self.timing is not None
        if self.started is None:
            self.started = self.timing.clock_ns()
            self.timing.record(self.stage_prefix + "request_started", streaming=True, **self.fields)
        try:
            event = await anext(self.stream)
        except StopAsyncIteration:
            raise
        except BaseException as error:
            self.timing.record(
                self.stage_prefix + "request_failed", error_type=type(error).__name__, streaming=True, **self.fields
            )
            raise
        kind = getattr(event, "type", "")
        if kind == "message_start":
            self.timing.record(
                self.stage_prefix + "response_started",
                llm_response_id=getattr(event, "message_id", None),
                **self.fields,
            )
        elif kind == "message_end":
            self.timing.record(
                self.stage_prefix + "request_completed",
                result=getattr(event, "stop_reason", "completed"),
                **self.fields,
            )
        if (
            isinstance(kind, str)
            and kind in {"text_delta", "thinking_delta"}
            and getattr(event, "text", "")
            and not getattr(event, "is_metadata_only", False)
            and kind not in self.seen
        ):
            self.seen.add(kind)
            stage = self.stage_prefix + ("first_text_received" if kind == "text_delta" else "first_thinking_received")
            self.timing.record(
                stage,
                streaming=True,
                duration_ms=max(0, self.timing.clock_ns() - self.started) / 1_000_000,
                **self.fields,
            )
        return event

    async def aclose(self) -> None:
        await self.stream.aclose()


def time_model_stream(stream: Any, **fields: Any) -> Any:
    if _auxiliary_model_purpose.get() is None:
        notify_progress("llm_request_started")
    timing = current_timing()
    return _TimedModelStream(stream, fields) if timing is not None and timing.active else stream


async def time_model_complete(awaitable: Any, **fields: Any) -> Any:
    purpose = _auxiliary_model_purpose.get()
    prefix = f"{purpose}_" if purpose else "llm_"
    if purpose:
        fields["llm_call_purpose"] = purpose
    with timing_phase(prefix + "request", streaming=False, **fields):
        result = await awaitable
        content_types = [kind for kind in ("text", "thinking") if getattr(result, kind, "")]
        mark_timing(
            prefix + "response_received",
            streaming=False,
            first_model_content_type="_and_".join(content_types) or None,
            **fields,
        )
        return result


def timing_headers() -> dict[str, str]:
    timing = current_timing()
    if timing is None or timing.closed:
        return {}
    return {"X-Request-Id": timing.request_id, "X-Iac-Parent-Call-Id": timing.call_id}
