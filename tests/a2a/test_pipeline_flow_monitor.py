from __future__ import annotations

import asyncio
import json
import stat
import threading
from datetime import datetime, timezone
from pathlib import Path

import pytest

import iac_code.a2a.pipeline_flow_monitor as pipeline_flow_monitor_module
from iac_code.a2a.pipeline_flow_monitor import (
    PipelineA2AFlowIdentity,
    PipelineA2AFlowItem,
    PipelineA2AFlowMonitor,
)
from iac_code.services.session_layout import SESSION_LAYOUT_VERSION_V2
from iac_code.services.session_metadata import SessionMetadata, write_session_metadata


class FakeClock:
    def __init__(self) -> None:
        self.value_ns = 1_000_000_000

    def now_ns(self) -> int:
        return self.value_ns

    def wall_now(self) -> datetime:
        return datetime(2026, 7, 18, tzinfo=timezone.utc)

    def advance_ms(self, value: float) -> None:
        self.value_ns += int(value * 1_000_000)


def _monitor(path: Path, clock: FakeClock, *, session_id: str = "session-1") -> PipelineA2AFlowMonitor:
    session_dir = path.parent.parent
    write_session_metadata(
        session_dir,
        SessionMetadata(session_id=session_id, cwd="/repo", layout_version=SESSION_LAYOUT_VERSION_V2),
    )
    return PipelineA2AFlowMonitor(
        path,
        PipelineA2AFlowIdentity(
            session_id=session_id,
            context_id="context-1",
            task_id="task-1",
            pipeline_run_id="pipeline-1",
        ),
        clock_ns=clock.now_ns,
        wall_clock=clock.wall_now,
        session_dir=session_dir,
    )


def _item(clock: FakeClock, arrival_no: int = 1) -> PipelineA2AFlowItem:
    return PipelineA2AFlowItem(
        arrival_no=arrival_no,
        enqueued_at_ns=clock.now_ns(),
        estimated_bytes=128,
    )


def _records(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


@pytest.mark.asyncio
async def test_healthy_flow_writes_only_session_summary(tmp_path: Path) -> None:
    clock = FakeClock()
    path = tmp_path / "projects" / "project" / "session-1" / "logs" / "a2a-pipeline-flow.jsonl"
    monitor = _monitor(path, clock)
    item = _item(clock)

    monitor.event_enqueued(item)
    clock.advance_ms(40)
    monitor.batch_started([item], trigger_reason="idle")
    clock.advance_ms(1)
    monitor.phase_started("coalesce")
    clock.advance_ms(1)
    monitor.phase_started("a2a_internal_queue")
    clock.advance_ms(1)
    monitor.batch_completed(persisted_envelopes=1, wire_envelopes=1, network_frames=1)
    await monitor.close()

    records = _records(path)
    assert [record["event"] for record in records] == ["a2a_pipeline_flow_summary"]
    assert records[0]["session_id"] == "session-1"
    assert records[0]["blocked_episodes"] == 0
    assert records[0]["delivered_events"] == 1
    assert records[0]["network_frames"] == 1
    assert records[0]["queue_wait_total_ms"] == 40.0
    assert records[0]["queue_wait_mean_ms"] == 40.0
    assert records[0]["queue_wait_max_ms"] == 40.0
    assert records[0]["phase_total_ms"]["downstream_transport"] == 0.0
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700


@pytest.mark.asyncio
async def test_downstream_stall_is_logged_and_recovers(tmp_path: Path) -> None:
    clock = FakeClock()
    path = tmp_path / "session-1" / "logs" / "a2a-pipeline-flow.jsonl"
    monitor = _monitor(path, clock)
    item = _item(clock)

    monitor.event_enqueued(item)
    monitor.batch_started([item], trigger_reason="idle")
    monitor.phase_started("a2a_internal_queue")
    monitor.transport_stage_changed("dequeued", clock.now_ns())
    clock.advance_ms(300)
    monitor._evaluate(clock.now_ns())
    monitor.transport_stage_changed("acknowledged", clock.now_ns())
    monitor.batch_completed(persisted_envelopes=1, wire_envelopes=1, network_frames=1)
    await monitor.close()

    records = _records(path)
    assert [record["event"] for record in records] == [
        "a2a_pipeline_flow_blocked",
        "a2a_pipeline_flow_recovered",
        "a2a_pipeline_flow_summary",
    ]
    assert records[0]["component"] == "downstream_transport"
    assert records[0]["phase"] == "downstream_transport"
    assert records[1]["component"] == "downstream_transport"
    assert records[0]["congestion_started_ms_ago"] == 300.0
    assert records[1]["congestion_duration_ms"] == 300.0
    assert records[2]["phase_max_ms"]["downstream_transport"] == 300.0


@pytest.mark.asyncio
async def test_persist_stall_is_classified_as_iac_processing(tmp_path: Path) -> None:
    clock = FakeClock()
    path = tmp_path / "session-1" / "logs" / "a2a-pipeline-flow.jsonl"
    monitor = _monitor(path, clock)
    item = _item(clock)

    monitor.event_enqueued(item)
    monitor.batch_started([item], trigger_reason="idle")
    clock.advance_ms(300)
    monitor._evaluate(clock.now_ns())
    monitor.phase_started("coalesce")
    monitor.batch_completed(persisted_envelopes=1, wire_envelopes=1, network_frames=1)
    await monitor.close()

    records = _records(path)
    assert records[0]["event"] == "a2a_pipeline_flow_blocked"
    assert records[0]["component"] == "iac_processing"
    assert records[0]["phase"] == "persist"
    assert records[1]["event"] == "a2a_pipeline_flow_recovered"
    assert records[1]["component"] == "iac_processing"


@pytest.mark.asyncio
async def test_queue_wait_crossing_threshold_is_observed_before_batch_snapshot(tmp_path: Path) -> None:
    clock = FakeClock()
    path = tmp_path / "session-1" / "logs" / "a2a-pipeline-flow.jsonl"
    monitor = _monitor(path, clock)
    item = _item(clock)

    monitor.event_enqueued(item)
    clock.advance_ms(300)
    monitor.batch_started([item], trigger_reason="idle")
    monitor.batch_completed(persisted_envelopes=1, wire_envelopes=1, network_frames=1)
    await monitor.close()

    records = _records(path)
    assert [record["event"] for record in records] == [
        "a2a_pipeline_flow_blocked",
        "a2a_pipeline_flow_recovered",
        "a2a_pipeline_flow_summary",
    ]
    assert records[0]["component"] == "iac_processing"
    assert records[0]["oldest_queue_age_ms"] == 300.0
    assert records[1]["congestion_duration_ms"] == 300.0
    assert records[2]["queue_wait_max_ms"] == 300.0


@pytest.mark.asyncio
async def test_pending_backlog_keeps_downstream_classification_at_ack_boundary(tmp_path: Path) -> None:
    clock = FakeClock()
    path = tmp_path / "session-1" / "logs" / "a2a-pipeline-flow.jsonl"
    monitor = _monitor(path, clock)
    first = _item(clock, arrival_no=1)

    monitor.event_enqueued(first)
    monitor.batch_started([first], trigger_reason="idle")
    monitor.phase_started("a2a_internal_queue")
    monitor.transport_stage_changed("dequeued", clock.now_ns())
    second = _item(clock, arrival_no=2)
    monitor.event_enqueued(second)
    clock.advance_ms(300)
    monitor.transport_stage_changed("acknowledged", clock.now_ns())
    monitor.batch_completed(persisted_envelopes=1, wire_envelopes=1, network_frames=1)
    monitor.batch_started([second], trigger_reason="idle")
    monitor.batch_completed(persisted_envelopes=1, wire_envelopes=1, network_frames=1)
    await monitor.close()

    records = _records(path)
    assert records[0]["event"] == "a2a_pipeline_flow_blocked"
    assert records[0]["component"] == "downstream_transport"
    assert records[1]["event"] == "a2a_pipeline_flow_recovered"
    assert records[1]["component"] == "downstream_transport"


@pytest.mark.asyncio
async def test_flow_logs_are_isolated_by_session_path(tmp_path: Path) -> None:
    first_clock = FakeClock()
    second_clock = FakeClock()
    first_path = tmp_path / "session-1" / "logs" / "a2a-pipeline-flow.jsonl"
    second_path = tmp_path / "session-2" / "logs" / "a2a-pipeline-flow.jsonl"
    first = _monitor(first_path, first_clock, session_id="session-1")
    second = _monitor(second_path, second_clock, session_id="session-2")

    await first.close()
    await second.close()

    assert _records(first_path)[0]["session_id"] == "session-1"
    assert _records(second_path)[0]["session_id"] == "session-2"


@pytest.mark.asyncio
async def test_close_cancels_sleeping_watcher_without_sampling_delay(tmp_path: Path) -> None:
    clock = FakeClock()
    path = tmp_path / "session-1" / "logs" / "a2a-pipeline-flow.jsonl"
    monitor = _monitor(path, clock)

    await monitor.start()
    monitor.event_enqueued(_item(clock))
    await asyncio.sleep(0)
    await asyncio.wait_for(monitor.close(aborted=True), timeout=0.2)
    assert monitor._watch_task is not None
    assert monitor._watch_task.done()


@pytest.mark.asyncio
async def test_close_prioritizes_summary_when_writer_queue_is_full(tmp_path: Path, monkeypatch) -> None:
    clock = FakeClock()
    path = tmp_path / "session-1" / "logs" / "a2a-pipeline-flow.jsonl"
    monitor = _monitor(path, clock)
    first_write_started = threading.Event()
    release_first_write = threading.Event()
    written: list[dict[str, object]] = []

    def append_record(_path, record, _session_dir) -> None:
        if not written:
            first_write_started.set()
            assert release_first_write.wait(timeout=1)
        written.append(record)

    monkeypatch.setattr(pipeline_flow_monitor_module, "_append_flow_record", append_record)
    monitor._emit({"event": "first"})
    assert await asyncio.to_thread(first_write_started.wait, 1)
    for index in range(64):
        monitor._emit({"event": "queued", "index": index})
    assert monitor._write_queue is not None and monitor._write_queue.full()

    close_task = asyncio.create_task(monitor.close())
    await asyncio.sleep(0)
    release_first_write.set()
    await asyncio.wait_for(close_task, timeout=0.4)

    assert [record["event"] for record in written] == ["first", "a2a_pipeline_flow_summary"]
    assert written[-1]["dropped_log_records"] == 64
