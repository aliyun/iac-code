from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from a2a.types import TaskState

from iac_code.a2a import first_display_persistence as persistence
from iac_code.a2a.pipeline_journal import A2APipelineJournal
from iac_code.a2a.pipeline_outbound import PipelineA2AOutboundQueue
from iac_code.a2a.request_timing import FirstTokenTimingMiddleware
from iac_code.a2a.task_store import A2ATaskStore
from iac_code.types.stream_events import TextDeltaEvent, ToolResultEvent

from .test_pipeline_outbound import RecordingPublisher
from .test_pipeline_stream import _publisher
from .test_task_store import sdk_task


def test_permission_recovery_evidence_reads_event_groups_in_order(tmp_path):
    from scripts.a2a.e2e.permission_wait.run_permission_wait_restart import _pipeline_journal_events

    journal = A2APipelineJournal(tmp_path / "session" / "a2a" / "pipeline")
    journal.append({"eventType": "permission_requested", "sequence": 1})
    journal.append_many(
        [
            {"eventType": "usage", "sequence": 2},
            {"eventType": "step_completed", "sequence": 3, "step": {"id": "fixture_step"}},
        ]
    )
    journal.append({"eventType": "pipeline_completed", "sequence": 4})
    assert [e["eventType"] for e in _pipeline_journal_events(tmp_path)] == [
        "permission_requested",
        "usage",
        "step_completed",
        "pipeline_completed",
    ]


@pytest.fixture(autouse=True)
def performance(monkeypatch):
    monkeypatch.setenv("IAC_CODE_A2A_EXTREME_PERFORMANCE", "true")


@pytest.mark.asyncio
async def test_disabled_mode_never_creates_a_deferral_window(monkeypatch):
    monkeypatch.setenv("IAC_CODE_A2A_EXTREME_PERFORMANCE", "false")
    with persistence.first_display_persistence_scope() as window:
        assert window is None
        assert persistence.first_display_persistence("task", "ctx") is None


@pytest.mark.asyncio
async def test_latest_request_route_wins_over_resident_producers_and_close():
    with persistence.first_display_persistence_scope() as old:
        old.bind("task", "ctx")
        with persistence.first_display_persistence_scope() as new:
            new.bind("task", "ctx")
            old.bind("task", "ctx")
            assert await asyncio.to_thread(persistence.first_display_persistence, "task", "ctx") is new
            await old.aclose()
            assert persistence.first_display_persistence("task", "ctx") is new
            await new.aclose()
    assert (0, "task", "ctx") not in persistence._routes


@pytest.mark.asyncio
async def test_routes_for_separate_stores_do_not_share_windows():
    with persistence.first_display_persistence_scope() as first:
        first.bind("task", "ctx", namespace=1)
        with persistence.first_display_persistence_scope() as second:
            second.bind("task", "ctx", namespace=2)
            assert persistence.first_display_persistence("task", "ctx", namespace=1) is first
            assert persistence.first_display_persistence("task", "ctx", namespace=2) is second
            await second.aclose()
        await first.aclose()


@pytest.mark.asyncio
async def test_hot_request_binds_before_reusing_a_resident_runtime(monkeypatch):
    from iac_code.a2a.executor import IacCodeA2AExecutor

    from .fakes import FakeEventQueue

    store = A2ATaskStore()
    executor = IacCodeA2AExecutor(task_store=store, model="fixture")
    with persistence.first_display_persistence_scope() as window:

        async def execute(context, queue):
            assert persistence.first_display_persistence("task", "ctx", namespace=id(store)) is window

        monkeypatch.setattr(executor, "_execute_request", execute)
        await executor.execute(SimpleNamespace(task_id="task", context_id="ctx", message=None), FakeEventQueue())
        await window.aclose()


@pytest.mark.asyncio
async def test_projection_limit_is_bounded_and_timer_flushes_without_a_display(monkeypatch):
    monkeypatch.setattr(persistence, "_MAX_PROJECTIONS", 1)
    monkeypatch.setattr(persistence, "_MAX_DELAY_SECONDS", 0.001)
    flushed = asyncio.Event()

    async def flush():
        flushed.set()

    with persistence.first_display_persistence_scope() as window:
        assert window.defer("first", flush)
        assert not window.defer("overflow", flush)
        await asyncio.wait_for(flushed.wait(), 2)
        await window.aclose()


@pytest.mark.asyncio
async def test_cancelled_finish_drains_the_owned_projection_write():
    started, release, finished = (asyncio.Event() for _ in range(3))

    async def write():
        started.set()
        await release.wait()
        finished.set()

    with persistence.first_display_persistence_scope() as window:
        window.defer("write", write)
        task = asyncio.create_task(window.finish())
        await started.wait()
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done() and not finished.is_set()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert finished.is_set()
        await window.aclose()


@pytest.mark.asyncio
async def test_post_display_projection_failure_is_retried_on_close():
    attempts = []

    async def write():
        attempts.append(1)
        if len(attempts) == 1:
            raise OSError("fixture write failure")

    with persistence.first_display_persistence_scope() as window:
        window.defer("write", write)
        await window.finish()
        assert not window.active and len(attempts) == 1
        await window.aclose()
        assert len(attempts) == 2 and not window._pending


@pytest.mark.asyncio
@pytest.mark.parametrize("send_fails", [False, True])
async def test_wire_completion_ends_deferral_and_failed_send_still_drains(send_fails):
    order = []

    async def flush():
        order.append("flush")

    async def app(scope, receive, send):
        window = persistence.first_display_persistence("task", "ctx")
        assert window is not None
        window.defer("write", flush)
        await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"text/event-stream")]})
        payload = {"result": {"message": {"role": "agent", "parts": [{"text": "answer"}]}}}
        await send(
            {
                "type": "http.response.body",
                "body": b"data: " + json.dumps(payload).encode() + b"\n\n",
                "more_body": False,
            }
        )
        assert not window.active

    async def send(message):
        if message["type"] == "http.response.body":
            assert order == []
            order.append("send")
            if send_fails:
                raise ConnectionError("fixture disconnect")

    scope = {"type": "http", "path": "/", "method": "POST", "headers": []}
    if send_fails:
        with pytest.raises(ConnectionError):
            await FirstTokenTimingMiddleware(app)(scope, None, send)
    else:
        await FirstTokenTimingMiddleware(app)(scope, None, send)
    assert order == ["send", "flush"]


@pytest.mark.asyncio
@pytest.mark.parametrize("scope", ["pipeline", "step", "candidate"])
async def test_pipeline_prefix_is_appended_once_and_reduced_once_at_input_boundary(tmp_path, monkeypatch, scope):
    publisher, queue = _publisher(tmp_path)
    save = Mock(wraps=publisher.snapshot_store.save)
    append = Mock(wraps=publisher.journal.append_many)
    monkeypatch.setattr(publisher.snapshot_store, "save", save)
    monkeypatch.setattr(publisher.journal, "append_many", append)
    with persistence.first_display_persistence_scope() as window:
        for kind in ("message_started", "tool_started", "usage", "context_usage"):
            await publisher.persist_envelope(publisher.translator.manual_event(kind, scope, status="working", data={}))
        assert save.call_count == append.call_count == 0
        assert publisher.journal.read_all() == []
        boundary = publisher.translator.manual_event(
            "input_required", "pipeline", status="input_required", data={"kind": "ask_user_question", "question": "Q"}
        )
        await publisher.persist_envelope(boundary, require_journal_metadata=True)
        assert save.call_count == append.call_count == 1
        events = publisher.journal.read_all()
        assert [e["eventType"] for e in events] == [
            "message_started",
            "tool_started",
            "usage",
            "context_usage",
            "input_required",
        ]
        assert [e["sequence"] for e in events] == [1, 2, 3, 4, 5]
        assert publisher.snapshot_store.load()["lastSequence"] == 5
        assert queue.events == []
        await window.finish()
        assert save.call_count == 1
        await publisher.persist_envelope(publisher.translator.manual_event("usage", "pipeline", data={}))
        assert save.call_count == 2  # Existing post-display behavior resumes.
        await window.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", ["events", "bytes"])
async def test_pipeline_prefix_flushes_at_memory_limit(tmp_path, monkeypatch, limit):
    import iac_code.a2a.pipeline_stream as stream

    monkeypatch.setattr(stream, "_FIRST_DISPLAY_MAX_EVENTS" if limit == "events" else "_FIRST_DISPLAY_MAX_BYTES", 1)
    publisher, _ = _publisher(tmp_path)
    with persistence.first_display_persistence_scope() as window:
        await publisher.persist_envelope(publisher.translator.manual_event("usage", "pipeline", data={}))
        assert publisher.snapshot_store.load()["lastSequence"] == 1
        assert len(publisher.journal.read_all()) == 1
        await window.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["permission_requested", "input_received", "pipeline_failed", "pipeline_canceled"])
async def test_recovery_boundaries_flush_the_prefix_before_they_can_be_published(tmp_path, kind):
    publisher, queue = _publisher(tmp_path)
    with persistence.first_display_persistence_scope() as window:
        await publisher.persist_envelope(publisher.translator.manual_event("usage", "pipeline", data={}))
        envelope = publisher.translator.manual_event(kind, "pipeline", data={})
        assert await publisher.persist_envelope(envelope, require_durable_metadata=True)
        assert [e["eventType"] for e in publisher.journal.read_all()] == ["usage", kind]
        assert publisher.snapshot_store.load()["lastSequence"] == 2
        assert queue.events == []
        await window.aclose()


@pytest.mark.asyncio
async def test_explicit_durability_is_never_overridden_by_a_display_event_type(tmp_path):
    publisher, _ = _publisher(tmp_path)
    with persistence.first_display_persistence_scope() as window:
        event = publisher.translator.manual_event("tool_started", "step", status="working", data={})
        assert await publisher.persist_envelope(event, require_durable_metadata=True)
        assert publisher.snapshot_store.load()["lastSequence"] == 1
        assert publisher.journal.read_all()[0]["eventType"] == "tool_started"
        await window.aclose()


@pytest.mark.asyncio
async def test_disabled_mode_preserves_per_event_snapshot_writes(tmp_path, monkeypatch):
    monkeypatch.setenv("IAC_CODE_A2A_EXTREME_PERFORMANCE", "false")
    publisher, _ = _publisher(tmp_path)
    save = Mock(wraps=publisher.snapshot_store.save)
    monkeypatch.setattr(publisher.snapshot_store, "save", save)
    with persistence.first_display_persistence_scope() as window:
        assert window is None
        for _ in range(3):
            await publisher.persist_envelope(publisher.translator.manual_event("usage", "pipeline", data={}))
        assert save.call_count == 3


@pytest.mark.asyncio
async def test_input_backup_observes_the_complete_batch_before_any_delivery(tmp_path):
    publisher, queue = _publisher(tmp_path)
    hook_started, backup_committed = asyncio.Event(), asyncio.Event()

    async def backup(envelope):
        if envelope.get("eventType") == "input_required":
            assert publisher.snapshot_store.load()["lastSequence"] == envelope["sequence"]
            assert publisher.journal.read_all()[-1]["eventType"] == "input_required"
            assert not queue.events
            hook_started.set()
            await backup_committed.wait()
        return True

    publisher.before_enqueue = backup
    with persistence.first_display_persistence_scope() as window:
        await publisher.persist_envelope(publisher.translator.manual_event("usage", "pipeline", data={}))
        envelope = publisher.translator.manual_event("input_required", "pipeline", status="input_required", data={})
        await publisher.persist_envelope(envelope)
        task = asyncio.create_task(publisher.enqueue_persisted_batch([envelope]))
        await asyncio.wait_for(hook_started.wait(), 2)
        assert not queue.events
        backup_committed.set()
        await task
        assert queue.events
        await window.aclose()


@pytest.mark.asyncio
async def test_critical_append_failure_keeps_prefix_for_retry_and_suppresses_publication(tmp_path, monkeypatch):
    publisher, queue = _publisher(tmp_path)
    with persistence.first_display_persistence_scope() as window:
        await publisher.persist_envelope(publisher.translator.manual_event("usage", "pipeline", data={}))
        append = publisher.journal.append_many
        monkeypatch.setattr(publisher.journal, "append_many", Mock(side_effect=OSError("fixture disk failure")))
        boundary = publisher.translator.manual_event("input_required", "pipeline", status="input_required", data={})
        assert await publisher.persist_envelope(boundary, require_journal_metadata=True) is None
        assert queue.events == []
        assert len(publisher._extreme_pending_journal_events) == 1
        monkeypatch.setattr(publisher.journal, "append_many", append)
        # Retry the boundary: the failed batch has not been forgotten.
        assert await publisher.persist_envelope(boundary, require_journal_metadata=True)
        assert [e["eventType"] for e in publisher.journal.read_all()] == ["usage", "input_required"]
        await window.aclose()


@pytest.mark.asyncio
async def test_sdk_working_mirror_is_latest_only_but_business_boundary_is_immediate(tmp_path, monkeypatch):
    store = A2ATaskStore()
    await store.get_or_create_context(context_id="ctx-1", cwd=str(tmp_path), runtime_factory=lambda sid: object())
    mirror = Mock(wraps=store._mirror_session_task)
    monkeypatch.setattr(store, "_mirror_session_task", mirror)
    with persistence.first_display_persistence_scope() as window:
        task = sdk_task("task-1", state=TaskState.TASK_STATE_WORKING)
        await store.save(task)
        first = mirror.call_count
        for _ in range(5):
            task.status.timestamp.GetCurrentTime()
            await store.save(task)
        assert mirror.call_count == first
        assert (await store.get("task-1")).status == task.status
        await window.finish()
        assert mirror.call_count == first + 1
        assert mirror.call_args.args[0].updated_at == store._tasks["task-1"].updated_at
        await window.aclose()
    with persistence.first_display_persistence_scope() as next_request:
        await store.save(task)
        count = mirror.call_count
        task.status.state = TaskState.TASK_STATE_INPUT_REQUIRED
        await store.save(task)
        assert mirror.call_count == count + 1
        await next_request.finish()
        assert mirror.call_count == count + 1  # No stale working projection overwrites it.
        await next_request.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "event",
    [
        TextDeltaEvent(text="first answer"),
        ToolResultEvent(tool_use_id="fixture", tool_name="bash", result="fixture error", is_error=True),
    ],
)
async def test_first_display_candidate_does_not_wait_for_ordinary_batch_timer(event):
    publisher = RecordingPublisher()
    with persistence.first_display_persistence_scope() as window:
        publisher.first_display_window = lambda: window if window.active else None
        outbound = PipelineA2AOutboundQueue(publisher, max_batch_delay_seconds=3600)
        await outbound.start()
        try:
            await outbound.submit(event)
            await asyncio.wait_for(publisher.first_send_started.wait(), 2)
            await outbound.flush()
        finally:
            await outbound.close()
            await window.aclose()
    assert publisher.persisted_events == [event]
