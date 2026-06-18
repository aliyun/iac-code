from textwrap import dedent
from unittest.mock import MagicMock, patch

import pytest

from iac_code.pipeline.engine.events import PipelineEvent, PipelineEventType
from iac_code.pipeline.engine.pipeline_runner import PipelineRunner
from iac_code.types.stream_events import SubPipelineStreamEvent, TextDeltaEvent


class TestSubPipelineStreamEvent:
    def test_wraps_inner_event(self):
        inner = TextDeltaEvent(text="hello")
        wrapped = SubPipelineStreamEvent(
            sub_pipeline_id="eval_abc123",
            candidate_index=0,
            inner=inner,
        )
        assert wrapped.sub_pipeline_id == "eval_abc123"
        assert wrapped.candidate_index == 0
        assert wrapped.inner is inner
        assert wrapped.type == "sub_pipeline_stream"

    def test_different_candidates(self):
        e1 = SubPipelineStreamEvent(sub_pipeline_id="eval_a", candidate_index=0, inner=TextDeltaEvent(text="a"))
        e2 = SubPipelineStreamEvent(sub_pipeline_id="eval_b", candidate_index=1, inner=TextDeltaEvent(text="b"))
        assert e1.candidate_index != e2.candidate_index
        assert e1.sub_pipeline_id != e2.sub_pipeline_id


class TestSubPipelineExecutorStreaming:
    @pytest.fixture
    def simple_sub_spec(self):
        from iac_code.pipeline.engine.step_spec import StepSpec, SubPipelineSpec

        return SubPipelineSpec(
            name="evaluate_candidate",
            steps=[
                StepSpec(
                    step_id="template_gen",
                    conclusion_field="template",
                    forward=None,
                    prompt_file="prompts/template.md",
                    context_fields=["candidate"],
                ),
            ],
            max_rollbacks=2,
            iterate_over="architecture.candidates",
            context_fields_from_parent=[],
        )

    @pytest.fixture
    def loaded_pipeline(self, tmp_path):
        from iac_code.pipeline.engine.loader import load_pipeline_dir

        (tmp_path / "prompts").mkdir()
        (tmp_path / "prompts" / "template.md").write_text("Generate template", encoding="utf-8")
        (tmp_path / "pipeline.yaml").write_text(
            dedent("""\
            name: test
            context_dependencies:
              architecture: []
              template: []
            max_rollbacks: 3
            steps:
              - id: arch
                conclusion_field: architecture
                forward: null
                prompt: prompts/template.md
        """),
            encoding="utf-8",
        )
        return load_pipeline_dir(tmp_path)

    @pytest.mark.asyncio
    async def test_execute_streaming_yields_pipeline_events(self, simple_sub_spec, loaded_pipeline, tmp_path):
        """execute_streaming should yield PipelineEvent and SubPipelineStreamEvent."""
        from iac_code.pipeline.engine.context import PipelineContext
        from iac_code.pipeline.engine.events import PipelineEvent, PipelineEventType
        from iac_code.pipeline.engine.sub_pipeline_executor import SubPipelineExecutor
        from iac_code.pipeline.engine.types import StepResult, StepStatus

        executor = SubPipelineExecutor(
            provider_manager=MagicMock(),
            base_tool_registry=MagicMock(),
            pipeline=loaded_pipeline,
            pipeline_dir=tmp_path,
            session_storage=MagicMock(),
            cwd="/tmp",
        )
        executor._observability.sub_pipeline_started = MagicMock()

        parent_ctx = PipelineContext({"architecture": [], "template": []})
        candidate = {"name": "Plan A"}

        # Mock step_executor.execute to yield a TextDelta + StepResult
        async def fake_step_execute(step, context, session_id, user_message=None):
            yield TextDeltaEvent(text="generating...")
            yield StepResult(
                step_id=step.step_id,
                status=StepStatus.COMPLETED,
                conclusion={"body": "template content"},
            )

        with patch.object(executor, "_make_step_executor") as mock_make:
            mock_step_exec = MagicMock()
            mock_step_exec.execute = fake_step_execute
            mock_make.return_value = mock_step_exec

            events = []
            async for event in executor.execute_streaming(
                sub_spec=simple_sub_spec,
                candidate=candidate,
                candidate_index=0,
                parent_context=parent_ctx,
                session_id="test123",
            ):
                events.append(event)

        # Should have: SUB_PIPELINE_STARTED, SUB_STEP_STARTED, SubPipelineStreamEvent(TextDelta),
        # SUB_STEP_COMPLETED, SUB_PIPELINE_COMPLETED
        pipeline_events = [e for e in events if isinstance(e, PipelineEvent)]
        stream_events = [e for e in events if isinstance(e, SubPipelineStreamEvent)]

        assert any(e.type == PipelineEventType.SUB_PIPELINE_STARTED for e in pipeline_events)
        assert any(e.type == PipelineEventType.SUB_STEP_STARTED for e in pipeline_events)
        assert any(e.type == PipelineEventType.SUB_STEP_COMPLETED for e in pipeline_events)
        assert any(e.type == PipelineEventType.SUB_PIPELINE_COMPLETED for e in pipeline_events)
        completed = next(e for e in pipeline_events if e.type == PipelineEventType.SUB_STEP_COMPLETED)
        assert completed.data["conclusion_field"] == "template"
        assert completed.data["conclusion"] == {"body": "template content"}
        assert len(stream_events) >= 1
        assert stream_events[0].candidate_index == 0
        assert isinstance(stream_events[0].inner, TextDeltaEvent)
        assert executor._observability.sub_pipeline_started.call_args.kwargs["parent_step_id"] is None

    @pytest.mark.asyncio
    async def test_sub_pipeline_completion_events_include_candidate_identity(
        self,
        simple_sub_spec,
        loaded_pipeline,
        tmp_path,
    ):
        from iac_code.pipeline.engine.context import PipelineContext
        from iac_code.pipeline.engine.sub_pipeline_executor import SubPipelineExecutor
        from iac_code.pipeline.engine.types import StepResult, StepStatus

        executor = SubPipelineExecutor(
            provider_manager=MagicMock(),
            base_tool_registry=MagicMock(),
            pipeline=loaded_pipeline,
            pipeline_dir=tmp_path,
            session_storage=MagicMock(),
            cwd="/tmp",
        )
        parent_ctx = PipelineContext({"architecture": [], "template": []})

        async def fake_step_execute(step, context, session_id, user_message=None):
            yield StepResult(step_id=step.step_id, status=StepStatus.COMPLETED, conclusion={"ok": True})

        with patch.object(executor, "_make_step_executor") as mock_make:
            mock_step_exec = MagicMock()
            mock_step_exec.execute = fake_step_execute
            mock_make.return_value = mock_step_exec
            events = [
                event
                async for event in executor.execute_streaming(
                    sub_spec=simple_sub_spec,
                    candidate={"name": "Plan A"},
                    candidate_index=2,
                    parent_context=parent_ctx,
                    session_id="test123",
                )
            ]

        completed = [
            event
            for event in events
            if isinstance(event, PipelineEvent) and event.type == PipelineEventType.SUB_PIPELINE_COMPLETED
        ][-1]
        assert completed.data["candidate_index"] == 2
        assert completed.data["candidate_name"] == "Plan A"
        assert completed.data["sub_pipeline_name"] == "evaluate_candidate"
        assert completed.data["total_steps"] == 1

    @pytest.mark.asyncio
    async def test_failed_sub_pipeline_completion_events_include_candidate_identity(
        self,
        simple_sub_spec,
        loaded_pipeline,
        tmp_path,
    ):
        from iac_code.pipeline.engine.context import PipelineContext
        from iac_code.pipeline.engine.sub_pipeline_executor import SubPipelineExecutor
        from iac_code.pipeline.engine.types import StepResult, StepStatus

        executor = SubPipelineExecutor(
            provider_manager=MagicMock(),
            base_tool_registry=MagicMock(),
            pipeline=loaded_pipeline,
            pipeline_dir=tmp_path,
            session_storage=MagicMock(),
            cwd="/tmp",
        )
        parent_ctx = PipelineContext({"architecture": [], "template": []})

        async def fake_step_execute(step, context, session_id, user_message=None):
            yield StepResult(step_id=step.step_id, status=StepStatus.FAILED, error="boom")

        with patch.object(executor, "_make_step_executor") as mock_make:
            mock_step_exec = MagicMock()
            mock_step_exec.execute = fake_step_execute
            mock_make.return_value = mock_step_exec
            events = [
                event
                async for event in executor.execute_streaming(
                    sub_spec=simple_sub_spec,
                    candidate={"name": "Plan B"},
                    candidate_index=3,
                    parent_context=parent_ctx,
                    session_id="test123",
                )
            ]

        completed = [
            event
            for event in events
            if isinstance(event, PipelineEvent) and event.type == PipelineEventType.SUB_PIPELINE_COMPLETED
        ][-1]
        assert completed.data["failed"] is True
        assert completed.data["candidate_index"] == 3
        assert completed.data["candidate_name"] == "Plan B"
        assert completed.data["sub_pipeline_name"] == "evaluate_candidate"
        assert completed.data["total_steps"] == 1


class TestPipelineRunnerParallelStreaming:
    @pytest.mark.asyncio
    async def test_parallel_step_yields_events_in_realtime(self, tmp_path):
        """Events should arrive during execution, not batched after gather."""
        (tmp_path / "prompts").mkdir(exist_ok=True)
        (tmp_path / "prompts" / "arch.md").write_text("Arch", encoding="utf-8")
        (tmp_path / "prompts" / "eval.md").write_text("Eval", encoding="utf-8")
        (tmp_path / "prompts" / "template.md").write_text("T", encoding="utf-8")
        (tmp_path / "pipeline.yaml").write_text(
            dedent("""\
            name: test
            context_dependencies:
              architecture: []
              evaluated: [architecture]
            max_rollbacks: 3
            sub_pipelines:
              evaluate_candidate:
                max_rollbacks: 2
                iterate_over: architecture.candidates
                context_fields_from_parent: []
                steps:
                  - id: template_gen
                    conclusion_field: template
                    forward: null
                    prompt: prompts/template.md
                    context_fields: [candidate]
            steps:
              - id: arch
                conclusion_field: architecture
                forward: eval
                prompt: prompts/arch.md
              - id: eval
                type: parallel_sub_pipeline
                sub_pipeline: evaluate_candidate
                conclusion_field: evaluated
                forward: null
                prompt: prompts/eval.md
        """),
            encoding="utf-8",
        )

        storage = MagicMock()
        storage.session_path.return_value = MagicMock()
        runner = PipelineRunner(
            pipeline_dir=tmp_path,
            provider_manager=MagicMock(),
            base_tool_registry=MagicMock(),
            session_storage=storage,
            session_id="test123",
        )

        # Pre-set architecture with 2 candidates
        runner.context.set_conclusion("architecture", {"candidates": [{"name": "Plan A"}, {"name": "Plan B"}]})
        runner.state_machine.advance()  # past arch step

        # Mock execute_streaming on SubPipelineExecutor
        parent_step_ids = []

        async def fake_streaming(
            sub_spec,
            candidate,
            candidate_index,
            parent_context,
            session_id,
            *,
            start_from_step=None,
            preserved_conclusions=None,
            user_message=None,
            parent_step_id=None,
        ):
            parent_step_ids.append(parent_step_id)
            yield PipelineEvent(
                type=PipelineEventType.SUB_PIPELINE_STARTED,
                step_id=None,
                timestamp=0,
                data={
                    "sub_pipeline_id": f"eval_{candidate_index}",
                    "candidate_index": candidate_index,
                    "candidate_name": candidate["name"],
                    "total_steps": 1,
                    "sub_pipeline_name": "evaluate_candidate",
                },
            )
            yield SubPipelineStreamEvent(
                sub_pipeline_id=f"eval_{candidate_index}",
                candidate_index=candidate_index,
                inner=TextDeltaEvent(text=f"output_{candidate_index}"),
            )
            yield PipelineEvent(
                type=PipelineEventType.SUB_PIPELINE_COMPLETED,
                step_id=None,
                timestamp=0,
                data={
                    "sub_pipeline_id": f"eval_{candidate_index}",
                    "candidate_index": candidate_index,
                    "failed": False,
                    "conclusions": {"template": {"body": f"t_{candidate_index}"}},
                },
            )

        with patch("iac_code.pipeline.engine.pipeline_runner.SubPipelineExecutor") as mock_exec:
            instance = MagicMock()
            instance.execute_streaming = fake_streaming
            mock_exec.return_value = instance

            events = []
            async for event in runner._continue_from_current():
                events.append(event)

        # Should contain SubPipelineStreamEvents (not just PipelineEvents)
        stream_wrapped = [e for e in events if isinstance(e, SubPipelineStreamEvent)]
        assert len(stream_wrapped) == 2  # one per candidate
        assert parent_step_ids == ["eval", "eval"]

        # Should have set evaluated conclusion
        evaluated = runner.context.get_conclusion("evaluated")
        assert evaluated is not None
        assert len(evaluated) == 2

        completed = [
            event
            for event in events
            if isinstance(event, PipelineEvent)
            and event.type == PipelineEventType.STEP_COMPLETED
            and event.step_id == "eval"
        ]
        assert len(completed) == 1
        assert completed[0].data["conclusion_field"] == "evaluated"
        assert completed[0].data["conclusion"] == evaluated
