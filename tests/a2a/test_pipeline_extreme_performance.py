from __future__ import annotations

from pathlib import Path

import pytest

from iac_code.a2a.pipeline_events import PipelineA2AContext, PipelineEventTranslator
from iac_code.a2a.pipeline_journal import A2APipelineJournal
from iac_code.a2a.pipeline_performance import (
    A2A_EXTREME_PERFORMANCE_ENV,
    a2a_extreme_performance_enabled,
)
from iac_code.a2a.pipeline_snapshot import A2APipelineSnapshotStore
from iac_code.a2a.pipeline_stream import PipelineA2AEventPublisher
from iac_code.types.stream_events import TextDeltaEvent

from .fakes import FakeEventQueue


def _publisher(tmp_path: Path) -> tuple[PipelineA2AEventPublisher, FakeEventQueue]:
    queue = FakeEventQueue()
    context = PipelineA2AContext(
        pipeline_run_id="run-1",
        task_id="task-1",
        context_id="ctx-1",
        pipeline_name="selling",
    )
    pipeline_dir = tmp_path / "pipeline"
    publisher = PipelineA2AEventPublisher(
        event_queue=queue,
        translator=PipelineEventTranslator(context),
        journal=A2APipelineJournal(pipeline_dir),
        snapshot_store=A2APipelineSnapshotStore(pipeline_dir),
    )
    return publisher, queue


def test_extreme_performance_is_enabled_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(A2A_EXTREME_PERFORMANCE_ENV, raising=False)

    assert a2a_extreme_performance_enabled() is True


@pytest.mark.parametrize("value", ["0", "false", "False", "no", "off", "disabled"])
def test_extreme_performance_can_be_disabled(value: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(A2A_EXTREME_PERFORMANCE_ENV, value)

    assert a2a_extreme_performance_enabled() is False


@pytest.mark.asyncio
async def test_default_extreme_mode_defers_text_delta_sidecar_writes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(A2A_EXTREME_PERFORMANCE_ENV, raising=False)
    publisher, queue = _publisher(tmp_path)

    returned = await publisher.publish(TextDeltaEvent(text="hello"))

    assert returned == "hello"
    assert len(queue.events) == 1
    assert publisher.journal.read_all() == []
    assert publisher.snapshot_store.load() is None


@pytest.mark.asyncio
async def test_default_extreme_mode_flushes_deferred_text_with_next_semantic_event(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(A2A_EXTREME_PERFORMANCE_ENV, raising=False)
    publisher, _queue = _publisher(tmp_path)

    await publisher.publish(TextDeltaEvent(text="hello"))
    await publisher.publish_manual("pipeline_warning", "pipeline", data={"message": "check"})

    journal_events = publisher.journal.read_all()
    assert [event["eventType"] for event in journal_events] == ["text_delta", "pipeline_warning"]
    snapshot = publisher.snapshot_store.load()
    assert snapshot is not None
    assert snapshot["display"]["messages"][0]["text"] == "hello"
    assert snapshot["lastSequence"] == journal_events[-1]["sequence"]


@pytest.mark.asyncio
async def test_default_extreme_mode_does_not_replay_journal_for_text_delta(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(A2A_EXTREME_PERFORMANCE_ENV, raising=False)
    publisher, _queue = _publisher(tmp_path)

    def fail_read_all_repairing_tail() -> list[dict]:
        raise AssertionError("text delta should not replay the A2A journal")

    publisher.journal.read_all_repairing_tail = fail_read_all_repairing_tail  # type: ignore[method-assign]

    assert await publisher.publish(TextDeltaEvent(text="hello")) == "hello"


@pytest.mark.asyncio
async def test_disabled_extreme_mode_keeps_immediate_text_delta_sidecar_writes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(A2A_EXTREME_PERFORMANCE_ENV, "0")
    publisher, _queue = _publisher(tmp_path)

    await publisher.publish(TextDeltaEvent(text="hello"))

    assert publisher.journal.read_all()[0]["data"]["text"] == "hello"
    snapshot = publisher.snapshot_store.load()
    assert snapshot is not None
    assert snapshot["display"]["messages"][0]["text"] == "hello"
