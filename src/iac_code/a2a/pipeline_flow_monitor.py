from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from iac_code.services.session_layout import ensure_session_owned_dir
from iac_code.utils.file_security import ensure_private_dir, ensure_private_file
from iac_code.utils.state_io import append_jsonl_rotating_locked

FLOW_BLOCKED_PENDING_EVENTS = 64
FLOW_BLOCKED_AGE_SECONDS = 0.250
FLOW_SAMPLE_INTERVAL_SECONDS = 5.0
FLOW_LOG_MAX_FILE_BYTES = 4 * 1024 * 1024
FLOW_LOG_MAX_FILES = 3
FLOW_LOG_CLOSE_TIMEOUT_SECONDS = 0.5

_PHASE_PERSIST = "persist"
_PHASE_COALESCE = "coalesce"
_PHASE_A2A_INTERNAL = "a2a_internal_queue"
_PHASE_DOWNSTREAM = "downstream_transport"
_PHASES = (_PHASE_PERSIST, _PHASE_COALESCE, _PHASE_A2A_INTERNAL, _PHASE_DOWNSTREAM)
_IAC_PHASES = {_PHASE_PERSIST, _PHASE_COALESCE, _PHASE_A2A_INTERNAL}
_PHASE_HANDOFF_GRACE_NS = 50_000_000
_WRITER_STOP = None

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PipelineA2AFlowIdentity:
    session_id: str
    context_id: str
    task_id: str
    pipeline_run_id: str


@dataclass(frozen=True)
class PipelineA2AFlowItem:
    arrival_no: int
    enqueued_at_ns: int
    estimated_bytes: int


class PipelineA2AFlowMonitor:
    def __init__(
        self,
        path: str | Path,
        identity: PipelineA2AFlowIdentity,
        *,
        blocked_pending_events: int = FLOW_BLOCKED_PENDING_EVENTS,
        blocked_age_seconds: float = FLOW_BLOCKED_AGE_SECONDS,
        sample_interval_seconds: float = FLOW_SAMPLE_INTERVAL_SECONDS,
        clock_ns: Callable[[], int] = time.monotonic_ns,
        wall_clock: Callable[[], datetime] | None = None,
        session_dir: str | Path | None = None,
    ) -> None:
        self.path = Path(path)
        self._session_dir = Path(session_dir) if session_dir is not None else None
        self.identity = identity
        self._blocked_pending_events = max(1, blocked_pending_events)
        self._blocked_age_ns = max(1, int(blocked_age_seconds * 1_000_000_000))
        self._sample_interval_ns = max(1, int(sample_interval_seconds * 1_000_000_000))
        self._watch_interval_seconds = max(0.050, min(blocked_age_seconds, sample_interval_seconds, 0.250))
        self._clock_ns = clock_ns
        self._wall_clock = wall_clock or (lambda: datetime.now(timezone.utc))

        self._started_at_ns = clock_ns()
        self._pending: dict[int, tuple[int, int]] = {}
        self._pending_bytes = 0
        self._in_flight: list[PipelineA2AFlowItem] = []
        self._current_phase: str | None = None
        self._phase_started_at_ns: int | None = None
        self._last_phase: str | None = None
        self._last_phase_ended_at_ns: int | None = None
        self._phase_total_ns: dict[str, int] = {}
        self._phase_max_ns: dict[str, int] = {}

        self._input_events = 0
        self._delivered_events = 0
        self._persisted_envelopes = 0
        self._wire_envelopes = 0
        self._network_frames = 0
        self._batch_count = 0
        self._batch_event_total = 0
        self._batch_size_max = 0
        self._queue_wait_total_ns = 0
        self._queue_wait_max_ns = 0
        self._max_pending_events = 0
        self._max_pending_bytes = 0
        self._max_oldest_queue_age_ns = 0
        self._blocked_episode_count = 0

        self._blocked = False
        self._blocked_at_ns: int | None = None
        self._blocked_components: set[str] = set()
        self._last_sample_at_ns = 0
        self._episode_max_pending_events = 0
        self._episode_max_pending_bytes = 0
        self._episode_max_oldest_age_ns = 0
        self._episode_max_phase_age_ns = 0

        self._work_available = asyncio.Event()
        self._watch_task: asyncio.Task[None] | None = None
        self._write_queue: asyncio.Queue[dict[str, Any] | None] | None = None
        self._writer_task: asyncio.Task[None] | None = None
        self._writer_final_record: dict[str, Any] | None = None
        self._dropped_log_records = 0
        self._write_error_reported = False
        self._closed = False

    async def start(self) -> None:
        if self._watch_task is None:
            self._watch_task = asyncio.create_task(self._watch_flow(), name="pipeline-a2a-flow-monitor")

    def event_enqueued(self, item: PipelineA2AFlowItem) -> None:
        if self._closed:
            return
        self._pending[item.arrival_no] = (item.enqueued_at_ns, item.estimated_bytes)
        self._pending_bytes += item.estimated_bytes
        self._input_events += 1
        self._max_pending_events = max(self._max_pending_events, len(self._pending))
        self._max_pending_bytes = max(self._max_pending_bytes, self._pending_bytes)
        if len(self._pending) == 1:
            self._work_available.set()
        if len(self._pending) == self._blocked_pending_events:
            self._evaluate(self._clock_ns())

    def batch_started(self, items: list[PipelineA2AFlowItem], *, trigger_reason: str) -> None:
        if self._closed:
            return
        now_ns = self._clock_ns()
        queue_waits = [max(0, now_ns - item.enqueued_at_ns) for item in items]
        self._queue_wait_total_ns += sum(queue_waits)
        if queue_waits:
            self._queue_wait_max_ns = max(self._queue_wait_max_ns, max(queue_waits))
            self._max_oldest_queue_age_ns = max(self._max_oldest_queue_age_ns, max(queue_waits))
        self._evaluate(now_ns, trigger_reason=trigger_reason)
        for item in items:
            pending = self._pending.pop(item.arrival_no, None)
            if pending is not None:
                self._pending_bytes = max(0, self._pending_bytes - pending[1])
        self._in_flight = list(items)
        self._batch_count += 1
        self._batch_event_total += len(items)
        self._batch_size_max = max(self._batch_size_max, len(items))
        self._set_phase(_PHASE_PERSIST, now_ns)

    def phase_started(self, phase: str, *, at_ns: int | None = None) -> None:
        if self._closed:
            return
        self._set_phase(phase, self._clock_ns() if at_ns is None else at_ns)

    def transport_stage_changed(self, stage: str, at_ns: int) -> None:
        if self._closed or not self._in_flight:
            return
        if stage in {"registered", "enqueued"}:
            self._set_phase(_PHASE_A2A_INTERNAL, at_ns)
        elif stage == "dequeued":
            self._set_phase(_PHASE_DOWNSTREAM, at_ns)
        elif stage in {"acknowledged", "discarded", "closed"}:
            self._set_phase(None, at_ns)

    def batch_completed(
        self,
        *,
        persisted_envelopes: int,
        wire_envelopes: int,
        network_frames: int,
    ) -> None:
        if self._closed:
            return
        now_ns = self._clock_ns()
        self._finish_current_phase(now_ns)
        self._current_phase = None
        self._phase_started_at_ns = None
        self._delivered_events += len(self._in_flight)
        self._persisted_envelopes += persisted_envelopes
        self._wire_envelopes += wire_envelopes
        self._network_frames += network_frames
        self._in_flight = []
        self._evaluate(now_ns)

    def batch_failed(self) -> None:
        if self._closed:
            return
        now_ns = self._clock_ns()
        self._finish_current_phase(now_ns)
        self._current_phase = None
        self._phase_started_at_ns = None
        self._in_flight = []
        self._evaluate(now_ns)

    async def close(self, *, aborted: bool = False) -> None:
        if self._closed:
            return
        now_ns = self._clock_ns()
        self._finish_current_phase(now_ns)
        self._current_phase = None
        self._phase_started_at_ns = None
        self._in_flight = []
        if aborted:
            self._pending.clear()
            self._pending_bytes = 0
        self._evaluate(now_ns, recovery_reason="aborted" if aborted else "drained")
        self._closed = True
        self._work_available.set()
        if self._watch_task is not None:
            self._watch_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._watch_task
        await self._close_writer(now_ns=now_ns, aborted=aborted)

    async def _watch_flow(self) -> None:
        while not self._closed:
            if not self._has_work():
                self._work_available.clear()
                if not self._has_work():
                    await self._work_available.wait()
                    continue
            await asyncio.sleep(self._watch_interval_seconds)
            if not self._closed:
                self._evaluate(self._clock_ns())

    def _has_work(self) -> bool:
        return bool(self._pending or self._in_flight or self._current_phase or self._blocked)

    def _set_phase(self, phase: str | None, at_ns: int) -> None:
        if phase == self._current_phase:
            return
        self._finish_current_phase(at_ns)
        self._current_phase = phase
        self._phase_started_at_ns = at_ns if phase is not None else None
        self._evaluate(at_ns)

    def _finish_current_phase(self, at_ns: int) -> None:
        if self._current_phase is None or self._phase_started_at_ns is None:
            return
        duration_ns = max(0, at_ns - self._phase_started_at_ns)
        self._phase_total_ns[self._current_phase] = self._phase_total_ns.get(self._current_phase, 0) + duration_ns
        self._phase_max_ns[self._current_phase] = max(self._phase_max_ns.get(self._current_phase, 0), duration_ns)
        self._last_phase = self._current_phase
        self._last_phase_ended_at_ns = at_ns

    def _evaluate(
        self,
        now_ns: int,
        *,
        trigger_reason: str | None = None,
        recovery_reason: str | None = None,
    ) -> None:
        oldest_age_ns = self._oldest_pending_age_ns(now_ns)
        phase_age_ns = self._phase_age_ns(now_ns)
        self._max_oldest_queue_age_ns = max(self._max_oldest_queue_age_ns, oldest_age_ns)
        is_blocked = (
            len(self._pending) >= self._blocked_pending_events
            or oldest_age_ns >= self._blocked_age_ns
            or phase_age_ns >= self._blocked_age_ns
        )
        component = self._component(now_ns)

        if not self._blocked and is_blocked:
            self._blocked = True
            self._blocked_at_ns = self._congestion_started_at_ns(now_ns, oldest_age_ns, phase_age_ns)
            self._blocked_components = {component}
            self._last_sample_at_ns = now_ns
            self._blocked_episode_count += 1
            self._reset_episode_maxima()
            self._update_episode_maxima(oldest_age_ns, phase_age_ns)
            self._emit(self._flow_record("a2a_pipeline_flow_blocked", now_ns, component, trigger_reason))
            return

        if self._blocked and is_blocked:
            self._blocked_components.add(component)
            self._update_episode_maxima(oldest_age_ns, phase_age_ns)
            if now_ns - self._last_sample_at_ns >= self._sample_interval_ns:
                self._last_sample_at_ns = now_ns
                self._emit(self._flow_record("a2a_pipeline_flow_sample", now_ns, component, trigger_reason))
            return

        if self._blocked:
            blocked_at_ns = self._blocked_at_ns or now_ns
            recovered = self._flow_record(
                "a2a_pipeline_flow_recovered",
                now_ns,
                self._episode_component(),
                trigger_reason,
            )
            recovered["congestion_duration_ms"] = _milliseconds(now_ns - blocked_at_ns)
            recovered["recovery_reason"] = recovery_reason or "below_threshold"
            recovered["episode_max_pending_events"] = self._episode_max_pending_events
            recovered["episode_max_pending_bytes"] = self._episode_max_pending_bytes
            recovered["episode_max_oldest_queue_age_ms"] = _milliseconds(self._episode_max_oldest_age_ns)
            recovered["episode_max_phase_age_ms"] = _milliseconds(self._episode_max_phase_age_ns)
            self._emit(recovered)
            self._blocked = False
            self._blocked_at_ns = None
            self._blocked_components.clear()

    def _flow_record(
        self,
        event: str,
        now_ns: int,
        component: str,
        trigger_reason: str | None,
    ) -> dict[str, Any]:
        oldest_age_ns = self._oldest_pending_age_ns(now_ns)
        phase_age_ns = self._phase_age_ns(now_ns)
        record = self._base_record(event)
        record.update(
            {
                "component": component,
                "pending_events": len(self._pending),
                "pending_bytes": self._pending_bytes,
                "in_flight_events": len(self._in_flight),
                "oldest_queue_age_ms": _milliseconds(oldest_age_ns),
                "phase": self._current_phase,
                "phase_age_ms": _milliseconds(phase_age_ns),
                "input_events": self._input_events,
                "delivered_events": self._delivered_events,
                "batch_count": self._batch_count,
            }
        )
        if trigger_reason:
            record["batch_trigger"] = trigger_reason
        if self._blocked_at_ns is not None:
            record["congestion_started_ms_ago"] = _milliseconds(max(0, now_ns - self._blocked_at_ns))
        return record

    def _summary_record(self, now_ns: int, *, aborted: bool) -> dict[str, Any]:
        duration_ns = max(1, now_ns - self._started_at_ns)
        duration_seconds = duration_ns / 1_000_000_000
        record = self._base_record("a2a_pipeline_flow_summary")
        record.update(
            {
                "aborted": aborted,
                "duration_ms": _milliseconds(duration_ns),
                "input_events": self._input_events,
                "delivered_events": self._delivered_events,
                "persisted_envelopes": self._persisted_envelopes,
                "wire_envelopes": self._wire_envelopes,
                "network_frames": self._network_frames,
                "coalesced_envelopes": max(0, self._persisted_envelopes - self._wire_envelopes),
                "batch_count": self._batch_count,
                "batch_size_mean": self._batch_event_total / self._batch_count if self._batch_count else 0.0,
                "batch_size_max": self._batch_size_max,
                "queue_wait_total_ms": _milliseconds(self._queue_wait_total_ns),
                "queue_wait_mean_ms": (
                    _milliseconds(self._queue_wait_total_ns // self._batch_event_total)
                    if self._batch_event_total
                    else 0.0
                ),
                "queue_wait_max_ms": _milliseconds(self._queue_wait_max_ns),
                "input_events_per_second": self._input_events / duration_seconds,
                "delivered_events_per_second": self._delivered_events / duration_seconds,
                "max_pending_events": self._max_pending_events,
                "max_pending_bytes": self._max_pending_bytes,
                "max_oldest_queue_age_ms": _milliseconds(self._max_oldest_queue_age_ns),
                "blocked_episodes": self._blocked_episode_count,
                "blocked_pending_events_threshold": self._blocked_pending_events,
                "blocked_age_threshold_ms": _milliseconds(self._blocked_age_ns),
                "sample_interval_ms": _milliseconds(self._sample_interval_ns),
                "dropped_log_records": self._dropped_log_records,
                "phase_total_ms": {phase: _milliseconds(self._phase_total_ns.get(phase, 0)) for phase in _PHASES},
                "phase_max_ms": {phase: _milliseconds(self._phase_max_ns.get(phase, 0)) for phase in _PHASES},
            }
        )
        return record

    def _base_record(self, event: str) -> dict[str, Any]:
        timestamp = (
            self._wall_clock().astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        )
        return {
            "timestamp": timestamp,
            "event": event,
            "session_id": self.identity.session_id,
            "context_id": self.identity.context_id,
            "task_id": self.identity.task_id,
            "pipeline_run_id": self.identity.pipeline_run_id,
        }

    def _component(self, now_ns: int) -> str:
        if self._current_phase == _PHASE_DOWNSTREAM:
            return "downstream_transport"
        if self._current_phase in _IAC_PHASES:
            return "iac_processing"
        if self._pending:
            oldest_enqueued_at_ns, _size = next(iter(self._pending.values()))
            if (
                self._last_phase == _PHASE_DOWNSTREAM
                and self._last_phase_ended_at_ns is not None
                and now_ns - self._last_phase_ended_at_ns <= _PHASE_HANDOFF_GRACE_NS
                and oldest_enqueued_at_ns <= self._last_phase_ended_at_ns
            ):
                return "downstream_transport"
            return "iac_processing"
        return "unknown"

    def _episode_component(self) -> str:
        components = self._blocked_components - {"unknown"}
        if len(components) == 1:
            return next(iter(components))
        return "mixed" if components else "unknown"

    def _oldest_pending_age_ns(self, now_ns: int) -> int:
        if not self._pending:
            return 0
        oldest_enqueued_ns, _size = next(iter(self._pending.values()))
        return max(0, now_ns - oldest_enqueued_ns)

    def _phase_age_ns(self, now_ns: int) -> int:
        if self._phase_started_at_ns is None:
            return 0
        return max(0, now_ns - self._phase_started_at_ns)

    def _congestion_started_at_ns(self, now_ns: int, oldest_age_ns: int, phase_age_ns: int) -> int:
        candidates: list[int] = []
        if oldest_age_ns >= self._blocked_age_ns:
            candidates.append(now_ns - oldest_age_ns)
        if phase_age_ns >= self._blocked_age_ns:
            candidates.append(now_ns - phase_age_ns)
        if len(self._pending) >= self._blocked_pending_events:
            candidates.append(now_ns)
        return min(candidates, default=now_ns)

    def _reset_episode_maxima(self) -> None:
        self._episode_max_pending_events = 0
        self._episode_max_pending_bytes = 0
        self._episode_max_oldest_age_ns = 0
        self._episode_max_phase_age_ns = 0

    def _update_episode_maxima(self, oldest_age_ns: int, phase_age_ns: int) -> None:
        self._episode_max_pending_events = max(self._episode_max_pending_events, len(self._pending))
        self._episode_max_pending_bytes = max(self._episode_max_pending_bytes, self._pending_bytes)
        self._episode_max_oldest_age_ns = max(self._episode_max_oldest_age_ns, oldest_age_ns)
        self._episode_max_phase_age_ns = max(self._episode_max_phase_age_ns, phase_age_ns)

    def _emit(self, record: dict[str, Any]) -> None:
        self._ensure_writer()
        assert self._write_queue is not None
        try:
            self._write_queue.put_nowait(record)
        except asyncio.QueueFull:
            self._dropped_log_records += 1

    def _ensure_writer(self) -> None:
        if self._write_queue is None:
            self._write_queue = asyncio.Queue(maxsize=64)
        if self._writer_task is None:
            self._writer_task = asyncio.create_task(self._write_records(), name="pipeline-a2a-flow-log-writer")

    async def _write_records(self) -> None:
        assert self._write_queue is not None
        while True:
            record = await self._write_queue.get()
            stop_requested = record is _WRITER_STOP
            record_to_write = self._writer_final_record if stop_requested else record
            try:
                if record_to_write is not None:
                    await asyncio.to_thread(_append_flow_record, self.path, record_to_write, self._session_dir)
            except Exception as exc:
                if not self._write_error_reported:
                    self._write_error_reported = True
                    logger.warning(
                        "Failed to write session A2A pipeline flow log path=%s error_type=%s",
                        self.path,
                        type(exc).__name__,
                    )
            finally:
                self._write_queue.task_done()
            if stop_requested:
                self._writer_final_record = None
                return

    async def _close_writer(self, *, now_ns: int, aborted: bool) -> None:
        self._ensure_writer()
        assert self._write_queue is not None
        assert self._writer_task is not None

        if self._write_queue.full():
            while True:
                try:
                    record = self._write_queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                self._write_queue.task_done()
                if record is not _WRITER_STOP:
                    self._dropped_log_records += 1

        self._writer_final_record = self._summary_record(now_ns, aborted=aborted)
        self._write_queue.put_nowait(_WRITER_STOP)
        try:
            await asyncio.wait_for(self._writer_task, timeout=FLOW_LOG_CLOSE_TIMEOUT_SECONDS)
        except TimeoutError:
            self._writer_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._writer_task
            logger.warning("Timed out closing session A2A pipeline flow log writer path=%s", self.path)
        finally:
            self._writer_final_record = None


def _append_flow_record(path: Path, record: dict[str, Any], session_dir: Path | None) -> None:
    if session_dir is None:
        ensure_private_dir(path.parent)
    else:
        ensure_session_owned_dir(session_dir, path.parent)
    append_jsonl_rotating_locked(
        path,
        [record],
        max_file_bytes=FLOW_LOG_MAX_FILE_BYTES,
        max_files=FLOW_LOG_MAX_FILES,
        durable=False,
        create_mode=0o600,
    )
    ensure_private_file(path)


def _milliseconds(value_ns: int) -> float:
    return round(value_ns / 1_000_000, 3)


__all__ = [
    "FLOW_BLOCKED_AGE_SECONDS",
    "FLOW_BLOCKED_PENDING_EVENTS",
    "FLOW_SAMPLE_INTERVAL_SECONDS",
    "PipelineA2AFlowIdentity",
    "PipelineA2AFlowItem",
    "PipelineA2AFlowMonitor",
]
