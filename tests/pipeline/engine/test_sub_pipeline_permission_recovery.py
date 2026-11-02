"""Candidate suspension leaves other real engine executions running."""

import asyncio
from unittest.mock import MagicMock

import pytest

from iac_code.pipeline.engine.events import PipelineEvent, PipelineEventType
from iac_code.pipeline.engine.pipeline_runner import PipelineRunner
from iac_code.pipeline.engine.sub_pipeline_executor import SubPipelineExecutor
from iac_code.pipeline.engine.types import StepResult, StepStatus
from iac_code.services.session_storage import SessionStorage
from iac_code.tools.base import ToolRegistry
from iac_code.types.stream_events import PermissionWaitSuspended, SubPipelineStreamEvent, TextDeltaEvent


@pytest.mark.asyncio
async def test_suspended_candidate_waits_for_other_candidate_to_finish_before_whole_pipeline_suspends(
    tmp_path, monkeypatch
):
    (tmp_path / "pipeline.yaml").write_text(
        """name: parallel-wait
context_dependencies:
  architecture: []
  evaluated: [architecture]
sub_pipelines:
  evaluate_candidate:
    iterate_over: architecture.candidates
    steps:
      - id: protected
        conclusion_field: result
        forward: null
        context_fields: [candidate]
steps:
  - id: evaluate
    type: parallel_sub_pipeline
    sub_pipeline: evaluate_candidate
    conclusion_field: evaluated
    forward: null
""",
        encoding="utf-8",
    )
    release_work = asyncio.Event()
    finished_work = asyncio.Event()
    work_started = asyncio.Event()

    class CandidateStepExecutor:
        current_agent_loop = None

        async def execute(self, step, context, session_id, **kwargs):
            name = context.get_conclusion("candidate")["name"]
            if name == "waiting":
                await work_started.wait()
                raise PermissionWaitSuspended("pwb_waiting")
            work_started.set()
            yield TextDeltaEvent(text="other candidate still working")
            await release_work.wait()
            finished_work.set()
            context.set_conclusion("result", {"done": True})
            yield StepResult(step_id=step.step_id, status=StepStatus.COMPLETED, conclusion={"done": True})

    monkeypatch.setattr(SubPipelineExecutor, "_make_step_executor", lambda self: CandidateStepExecutor())
    runner = PipelineRunner(
        pipeline_dir=tmp_path,
        provider_manager=MagicMock(),
        base_tool_registry=ToolRegistry(),
        session_storage=SessionStorage(tmp_path / "sessions"),
        session_id="original-session",
        cwd=str(tmp_path),
    )
    runner.context.set_conclusion("architecture", {"candidates": [{"name": "waiting"}, {"name": "working"}]})
    events = []

    async def consume():
        async for event in runner._continue_from_current():
            events.append(event)

    consumer = asyncio.create_task(consume())
    try:
        async def suspended_state():
            while True:
                restored = runner.session.restore_sync(runner._pipeline_identity)
                candidates = restored.execution.get("candidates", {}) if restored.execution else {}
                if candidates.get("0", {}).get("status") == "suspended":
                    return restored
                await asyncio.sleep(0.01)

        restored = await asyncio.wait_for(suspended_state(), 5)
        assert not consumer.done()
        assert not finished_work.is_set()
        assert restored.status == "running"
        assert restored.execution["candidates"]["1"]["status"] == "running"
        waiting = restored.execution["candidates"]["0"]
        assert waiting["permission_boundary_id"] == "pwb_waiting"
        assert restored.attempts["items"][waiting["active_attempt_id"]]["status"] == "running"
        release_work.set()
        with pytest.raises(PermissionWaitSuspended):
            await asyncio.wait_for(consumer, 5)
        assert finished_work.is_set()
        assert any(
            isinstance(event, SubPipelineStreamEvent) and isinstance(event.inner, TextDeltaEvent)
            for event in events
        )
        assert not any(
            isinstance(event, PipelineEvent)
            and event.type
            in {PipelineEventType.SUB_STEP_FAILED, PipelineEventType.STEP_FAILED, PipelineEventType.PIPELINE_COMPLETED}
            for event in events
        )
        restored = runner.session.restore_sync(runner._pipeline_identity)
        assert restored.execution["candidates"]["0"]["status"] == "suspended"
        assert restored.execution["candidates"]["1"]["status"] == "completed"
        assert runner.context.get_conclusion("evaluated") is None
    finally:
        if not consumer.done():
            consumer.cancel()
        await asyncio.gather(consumer, return_exceptions=True)
