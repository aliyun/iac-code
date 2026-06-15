"""Tests for PipelineRunner pause/resume API and pause_event plumbing."""

import asyncio
from textwrap import dedent
from unittest.mock import MagicMock

import pytest

from iac_code.pipeline.engine.pipeline_runner import PipelineRunner


class FakeSessionStorage:
    def __init__(self):
        self.meta_entries = []
        self._path = MagicMock()

    def append_meta(self, cwd, session_id, meta):
        self.meta_entries.append(meta)

    def session_path(self, cwd, session_id):
        return self._path


@pytest.fixture
def pipeline_runner(tmp_path):
    (tmp_path / "prompts").mkdir(exist_ok=True)
    (tmp_path / "prompts" / "a.md").write_text("do A", encoding="utf-8")
    (tmp_path / "pipeline.yaml").write_text(
        dedent("""\
        name: test
        context_dependencies:
          a_out: []
        max_rollbacks: 3
        steps:
          - id: a
            conclusion_field: a_out
            forward: null
            prompt: prompts/a.md
            description: Step A
        """),
        encoding="utf-8",
    )

    pm = MagicMock()
    pm.get_model_name.return_value = "test-model"
    return PipelineRunner(
        pipeline_dir=tmp_path,
        provider_manager=pm,
        base_tool_registry=MagicMock(),
        session_storage=FakeSessionStorage(),
        session_id="test-session",
        cwd=str(tmp_path),
    )


class TestPauseEventOwnership:
    def test_event_starts_set(self, pipeline_runner):
        """Default state is 'not paused' so newly-created AgentLoops run normally."""
        assert pipeline_runner._agent_pause_event.is_set()

    def test_pause_clears_event(self, pipeline_runner):
        pipeline_runner.pause_agent_loops()
        assert not pipeline_runner._agent_pause_event.is_set()

    def test_resume_sets_event(self, pipeline_runner):
        pipeline_runner.pause_agent_loops()
        pipeline_runner.resume_agent_loops()
        assert pipeline_runner._agent_pause_event.is_set()

    def test_pause_is_idempotent(self, pipeline_runner):
        pipeline_runner.pause_agent_loops()
        pipeline_runner.pause_agent_loops()
        assert not pipeline_runner._agent_pause_event.is_set()

    def test_resume_is_idempotent(self, pipeline_runner):
        pipeline_runner.resume_agent_loops()
        pipeline_runner.resume_agent_loops()
        assert pipeline_runner._agent_pause_event.is_set()


class TestPauseEventPlumbing:
    def test_step_executor_receives_event(self, pipeline_runner):
        """The runner's StepExecutor should hold the same pause_event instance."""
        assert pipeline_runner._step_executor._pause_event is pipeline_runner._agent_pause_event

    def test_sub_pipeline_executor_receives_event(self, pipeline_runner):
        """SubPipelineExecutors created by the runner should share the pause event."""
        from iac_code.pipeline.engine.sub_pipeline_executor import SubPipelineExecutor

        sub_exec = SubPipelineExecutor(
            provider_manager=pipeline_runner._step_executor._provider_manager,
            base_tool_registry=pipeline_runner._step_executor._base_tool_registry,
            pipeline=pipeline_runner._loaded,
            pipeline_dir=pipeline_runner._step_executor._pipeline_dir,
            session_storage=pipeline_runner._session_storage,
            cwd=pipeline_runner._cwd,
            pause_event=pipeline_runner._agent_pause_event,
        )
        assert sub_exec._pause_event is pipeline_runner._agent_pause_event

        inner_step_exec = sub_exec._make_step_executor()
        assert inner_step_exec._pause_event is pipeline_runner._agent_pause_event


class TestPauseEventEndToEnd:
    @pytest.mark.asyncio
    async def test_cleared_event_blocks_agent_loop_constructed_by_runner(self, pipeline_runner):
        """An AgentLoop constructed by the runner's StepExecutor should park
        at the turn-top checkpoint when pause_agent_loops() is called."""
        from iac_code.agent.agent_loop import AgentLoop
        from iac_code.tools.base import ToolRegistry

        pipeline_runner.pause_agent_loops()
        assert not pipeline_runner._agent_pause_event.is_set()

        loop = AgentLoop(
            provider_manager=pipeline_runner._step_executor._provider_manager,
            system_prompt="t",
            tool_registry=ToolRegistry(),
            max_turns=2,
            pause_event=pipeline_runner._agent_pause_event,
        )

        async def consume():
            try:
                async for _ in loop.run_streaming("hello"):
                    pass
            except Exception:
                pass

        task = asyncio.create_task(consume())
        await asyncio.sleep(0.05)
        assert not task.done(), "run_streaming should be parked"

        pipeline_runner.resume_agent_loops()
        try:
            await asyncio.wait_for(task, timeout=1.0)
        except asyncio.TimeoutError:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    @pytest.mark.asyncio
    async def test_pause_then_resume_releases_existing_loop(self, pipeline_runner):
        """Resuming after pause sets the event and any AgentLoop parked on it wakes up."""
        from iac_code.agent.agent_loop import AgentLoop
        from iac_code.tools.base import ToolRegistry

        pipeline_runner.pause_agent_loops()
        loop = AgentLoop(
            provider_manager=pipeline_runner._step_executor._provider_manager,
            system_prompt="t",
            tool_registry=ToolRegistry(),
            max_turns=1,
            pause_event=pipeline_runner._agent_pause_event,
        )

        async def consume():
            try:
                async for _ in loop.run_streaming("x"):
                    pass
            except Exception:
                pass

        task = asyncio.create_task(consume())
        await asyncio.sleep(0.05)
        assert not task.done()

        pipeline_runner.resume_agent_loops()
        try:
            await asyncio.wait_for(task, timeout=1.0)
        except asyncio.TimeoutError:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
