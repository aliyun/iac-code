"""Optional progress is transient, request-local, and never answer content."""

import asyncio
from unittest.mock import AsyncMock

import pytest
from a2a.types import Task, TaskStatus
from google.protobuf.json_format import MessageToDict

from iac_code.a2a.display_timing import DisplayEventClassifier
from iac_code.a2a.progress import ENV, ProgressEventQueue, progress_enabled
from iac_code.services.first_token_timing import timing_phase
from iac_code.services.request_progress import notify_progress, observe_progress
from tests.a2a.test_first_token_timing import Clock, frame, run_wire, text_update
from tests.a2a.test_first_token_timing import records as records


def initial_task():
    return Task(id="task-1", context_id="ctx-1", status=TaskStatus(state="TASK_STATE_WORKING"))


def progress_data(stage="preparing"):
    return {"schemaVersion": 1, "id": "request-progress-1", "sequence": 1, "stage": stage}


def progress_events(queue):
    return [
        MessageToDict(call.args[0])["metadata"]["iac_code"]["progress"]
        for call in queue.enqueue_event.call_args_list
        if call.args[0].__class__.__name__ == "TaskStatusUpdateEvent"
    ]


def test_progress_is_opt_in(monkeypatch):
    monkeypatch.delenv(ENV, raising=False)
    assert not progress_enabled()
    for value in ("", "false", "0", "invalid"):
        monkeypatch.setenv(ENV, value)
        assert not progress_enabled()
    monkeypatch.setenv(ENV, "true")
    assert progress_enabled()


@pytest.mark.asyncio
async def test_stages_work_without_timing_collection_and_stop_on_display():
    output = AsyncMock()
    progress = ProgressEventQueue(output)
    try:
        await progress.enqueue_event(initial_task())
        with observe_progress(progress.on_stage):
            with timing_phase("session_load"):
                pass
            await asyncio.sleep(0)
            await progress._progress_task
            notify_progress("llm_request_started")
            await asyncio.sleep(0)
            await progress._progress_task
            # A late background Wiki result cannot move the foreground stage backwards.
            await asyncio.to_thread(notify_progress, "wiki_search_started")
            await asyncio.sleep(0)
        assert [event["stage"] for event in progress_events(output)] == ["preparing", "loading_history", "analyzing"]
        payload = text_update()["result"]["statusUpdate"]
        await progress.enqueue_event(payload)
        count = output.enqueue_event.call_count
        progress.on_stage("wiki_search_started")
        await asyncio.sleep(0)
        assert output.enqueue_event.call_count == count
        assert progress._progress_finished
    finally:
        await progress.finish_progress()
    assert progress._progress_task is None or progress._progress_task.done()


@pytest.mark.asyncio
async def test_fast_answer_has_no_progress_before_initial_task_and_closes_pending_updates():
    output = AsyncMock()
    progress = ProgressEventQueue(output)
    progress.on_stage("llm_request_started")
    await asyncio.sleep(0)
    assert not output.enqueue_event.called
    await progress.enqueue_event(text_update()["result"]["statusUpdate"])
    await progress.finish_progress()
    assert output.enqueue_event.call_count == 1
    assert progress._progress_task.done()


@pytest.mark.asyncio
async def test_progress_failure_cannot_drop_business_event():
    output = AsyncMock()
    output.enqueue_event.side_effect = [None, RuntimeError("progress sink failure"), None]
    progress = ProgressEventQueue(output)
    await progress.enqueue_event(initial_task())
    await progress.enqueue_event(text_update()["result"]["statusUpdate"])
    await progress.finish_progress()
    assert output.enqueue_event.call_count == 3


@pytest.mark.asyncio
async def test_progress_cleanup_does_not_close_or_shadow_the_underlying_queue():
    output = AsyncMock()
    progress = ProgressEventQueue(output)
    await progress.finish_progress()
    output.close.assert_not_called()
    await progress.close()
    output.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_progress_does_not_change_content_metrics_even_if_it_has_fallback_text(records, monkeypatch):
    from iac_code.services import first_token_timing

    clock = Clock()
    monkeypatch.setattr(first_token_timing.time, "monotonic_ns", clock)
    payload = text_update("This is status, never model content")
    payload["result"]["statusUpdate"]["metadata"] = {"iac_code": {"progress": progress_data()}}
    assert DisplayEventClassifier().classify(payload) is None

    async def send(message):
        if message["type"] == "http.response.body":
            clock.advance(10)

    await run_wire([frame(payload), frame(text_update())], send=send, new_session=True)
    summary = next(record for record in records if record["stage"] == "first_token_summary")
    assert summary["elapsed_ms"] == 20
    assert summary["first_event_type"] == "text"
    assert summary["first_progress_sent"]
    assert summary["milestones_ms"]["first_progress_send_completed"] == 10
    assert summary["milestones_ms"]["first_text_send_completed"] == 20


@pytest.mark.asyncio
async def test_request_scopes_do_not_share_progress():
    async def run(stage):
        stages = []
        with observe_progress(stages.append):
            await asyncio.sleep(0)
            notify_progress(stage)
        return stages

    assert await asyncio.gather(run("session_load_started"), run("llm_request_started")) == [
        ["session_load_started"],
        ["llm_request_started"],
    ]
    notify_progress("outside_request")
