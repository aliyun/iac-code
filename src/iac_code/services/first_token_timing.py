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

logger = logging.getLogger(__name__)
_SAFE_VALUE = re.compile(r"^[A-Za-z0-9_.:/+-]{1,256}$")
_MAX_TIMELINE_EVENTS = 512
# One restore + chat chain emits nine lines, or ten when thinking is returned.
# Repeated phases, LLM attempts, and pipeline steps remain in the summary only.
_CHAT_LOG_STAGES = frozenset(
    {
        "request_received",
        "llm_request_started",
        "llm_first_thinking_received",
        "llm_first_text_received",
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
    }
)
_current: contextvars.ContextVar[RequestTiming | None] = contextvars.ContextVar("iac_first_token_timing", default=None)
_phase_stack: contextvars.ContextVar[tuple[str, ...]] = contextvars.ContextVar("iac_timing_phases", default=())
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
        self.timeline: list[dict[str, Any]] = []
        self.timeline_events_dropped = 0
        self._open_phases: dict[str, tuple[str, int]] = {}
        self._cutoff_ns: int | None = None
        self._cutoff_phases: dict[str, tuple[str, int]] = {}
        self._once: set[str] = set()
        self._emitted: set[str] = set()
        self._lock = threading.RLock()
        self._summarized = False
        self.closed = False

    @property
    def active(self) -> bool:
        return not self.closed and not self._summarized and self._cutoff_ns is None

    def bind(self, **fields: Any) -> None:
        with self._lock:
            for key, value in _fields(fields).items():
                self.identity.setdefault(key, value)

    def record(self, stage: str, *, once: bool = False, force: bool = False, **fields: Any) -> None:
        with self._lock:
            if self.closed or self._summarized or (not self.active and not force) or (once and stage in self._once):
                return
            self._once.add(stage)
            now = self.clock_ns()
            elapsed = round(max(0, now - self.started_ns) / 1_000_000, 3)
            self.milestones.setdefault(stage, elapsed)
            if stage in {"first_text_send_completed", "request_completed"} and self._cutoff_ns is None:
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

    def _write(self, fields: dict[str, Any]) -> None:
        try:
            logger.info(
                json.dumps(
                    {
                        "log_type": "first_token_timing",
                        "service": "iac-code",
                        "schema_version": 1,
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
                "first_text_send_completed",
                self.milestones.get(
                    "request_completed", round(max(0, self.clock_ns() - self.started_ns) / 1_000_000, 3)
                ),
            )
            cutoff_ns = self._cutoff_ns if self._cutoff_ns is not None else self.clock_ns()
            open_phases = self._cutoff_phases if self._cutoff_ns is not None else self._open_phases
            self._write(
                {
                    "stage": "first_token_summary",
                    "result": result,
                    "elapsed_ms": elapsed,
                    "first_text_sent": "first_text_send_completed" in self.milestones,
                    "first_text_observation_complete": not self.identity.get("wire_observation_incomplete", False),
                    "milestones_ms": dict(self.milestones),
                    "phase_totals_ms": dict(self.phase_totals_ms),
                    "timeline": list(self.timeline),
                    "timeline_events_dropped": self.timeline_events_dropped,
                    "in_progress_phases": [
                        {
                            "phase_id": phase_id,
                            "stage": stage,
                            "elapsed_ms": round(max(0, cutoff_ns - started) / 1_000_000, 3),
                        }
                        for phase_id, (stage, started) in open_phases.items()
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
    if timing := current_timing():
        timing.record(stage, once=once, **fields)


@contextmanager
def timing_phase(stage: str, *, first_only: bool = False, **fields: Any) -> Iterator[dict[str, Any]]:
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
        self.timing = current_timing()
        self.started: int | None = None
        self.seen: set[str] = set()

    def __aiter__(self):
        return self

    async def __anext__(self):
        assert self.timing is not None
        if self.started is None:
            self.started = self.timing.clock_ns()
            self.timing.record("llm_request_started", streaming=True, **self.fields)
        try:
            event = await anext(self.stream)
        except StopAsyncIteration:
            raise
        except BaseException as error:
            self.timing.record("llm_request_failed", error_type=type(error).__name__, streaming=True, **self.fields)
            raise
        kind = getattr(event, "type", "")
        if kind == "message_start":
            self.timing.record(
                "llm_response_started", llm_response_id=getattr(event, "message_id", None), **self.fields
            )
        elif kind == "message_end":
            self.timing.record(
                "llm_request_completed", result=getattr(event, "stop_reason", "completed"), **self.fields
            )
        if (
            isinstance(kind, str)
            and kind in {"text_delta", "thinking_delta"}
            and getattr(event, "text", "")
            and not getattr(event, "is_metadata_only", False)
            and kind not in self.seen
        ):
            self.seen.add(kind)
            stage = "llm_first_text_received" if kind == "text_delta" else "llm_first_thinking_received"
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
    timing = current_timing()
    return _TimedModelStream(stream, fields) if timing is not None and timing.active else stream


async def time_model_complete(awaitable: Any, **fields: Any) -> Any:
    with timing_phase("llm_request", streaming=False, **fields):
        result = await awaitable
        mark_timing("llm_response_received", streaming=False, **fields)
        return result


def timing_headers() -> dict[str, str]:
    timing = current_timing()
    if timing is None or timing.closed:
        return {}
    return {"X-Request-Id": timing.request_id, "X-Iac-Parent-Call-Id": timing.call_id}
