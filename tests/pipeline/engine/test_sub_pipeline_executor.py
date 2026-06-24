"""Tests for SubPipelineExecutor."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from iac_code.agent.message import ImageBlock, Message, ToolResultBlock, ToolUseBlock
from iac_code.pipeline.engine.context import PipelineContext
from iac_code.pipeline.engine.events import PipelineEvent, PipelineEventType
from iac_code.pipeline.engine.state_machine import StateMachine
from iac_code.pipeline.engine.step_spec import LoadedPipeline, StepSpec, SubPipelineSpec
from iac_code.pipeline.engine.sub_pipeline_executor import SubPipelineExecutor, SubPipelineResult
from iac_code.pipeline.engine.types import StepResult, StepStatus
from iac_code.tools.base import ToolRegistry
from iac_code.types.stream_events import ToolResultEvent, ToolUseEndEvent, ToolUseStartEvent


def _make_sub_spec() -> SubPipelineSpec:
    return SubPipelineSpec(
        name="evaluate_candidate",
        steps=[
            StepSpec(
                step_id="template_generating",
                conclusion_field="template",
                forward="cost_estimating",
                prompt_file="prompts/template.md",
                skill="iac_aliyun",
                context_fields=["candidate", "intent"],
            ),
            StepSpec(
                step_id="cost_estimating",
                conclusion_field="cost",
                forward=None,
                prompt_file="prompts/cost.md",
                skill="iac-aliyun-cost",
                context_fields=["template"],
            ),
        ],
        max_rollbacks=2,
        iterate_over="architecture.candidates",
        context_fields_from_parent=["intent"],
    )


class TestSubPipelineResult:
    def test_success_result_to_dict(self):
        result = SubPipelineResult(
            sub_pipeline_id="eval_abc123",
            candidate_index=0,
            candidate={"name": "Plan A"},
            conclusions={"template": {"body": "..."}, "cost": {"total": 100}},
            failed=False,
        )
        d = result.to_dict()
        assert d["candidate"] == {"name": "Plan A"}
        assert d["template"] == {"body": "..."}
        assert d["cost"] == {"total": 100}
        assert d["failed"] is False
        assert "error" not in d

    def test_failed_result_to_dict(self):
        result = SubPipelineResult(
            sub_pipeline_id="eval_abc123",
            candidate_index=1,
            candidate={"name": "Plan B"},
            conclusions={},
            failed=True,
            error="Max rollbacks exceeded",
        )
        d = result.to_dict()
        assert d["failed"] is True
        assert d["error"] == "Max rollbacks exceeded"

    def test_success_result_no_error_key(self):
        result = SubPipelineResult(
            sub_pipeline_id="eval_abc",
            candidate_index=0,
            candidate={},
            conclusions={"cost": {"total": 50}},
            failed=False,
        )
        d = result.to_dict()
        assert "error" not in d


class TestSubPipelineExecutor:
    @pytest.mark.asyncio
    async def test_execute_streaming_resumes_current_sub_step_with_attempt_data(self, tmp_path, monkeypatch):
        """Resume starts at the recorded sub-step and passes attempt/transcript state through."""
        (tmp_path / "prompts").mkdir(exist_ok=True)
        (tmp_path / "prompts" / "template.md").write_text("Generate template for {candidate}", encoding="utf-8")
        (tmp_path / "prompts" / "cost.md").write_text("Estimate cost for {template}", encoding="utf-8")

        pipeline = LoadedPipeline(
            name="test",
            steps=[],
            context_dependencies={"intent": []},
            max_rollbacks=3,
            skills={"iac_aliyun": "# IaC Skill", "iac-aliyun-cost": "# Cost Skill"},
        )
        sub_spec = _make_sub_spec()
        parent_ctx = PipelineContext({"intent": []})
        parent_ctx.set_conclusion("intent", {"type": "e-commerce"})

        sub_context = PipelineContext(
            {
                "candidate": [],
                "template": [],
                "cost": [],
                "intent": [],
            }
        )
        sub_context.set_conclusion("candidate", {"name": "Plan A"})
        sub_context.set_conclusion("intent", {"type": "e-commerce"})
        sub_context.set_conclusion("template", {"body": "already generated"})
        state_machine = StateMachine(sub_spec.steps, sub_spec.max_rollbacks)
        state_machine.jump_to("cost_estimating")
        resume_messages = [object()]
        resume_state = {
            "sub_pipeline_id": "evaluate_candidate_existing",
            "context": sub_context.to_snapshot(),
            "state_machine": state_machine.to_snapshot(),
            "current_sub_step": "cost_estimating",
            "current_index": 1,
            "active_attempt_id": "att_0007",
            "transcript_id": "transcript_att_0007",
            "resume_messages": resume_messages,
        }

        captured = []

        class FakeStepExecutor:
            current_agent_loop = None

            async def execute(
                self,
                step,
                context,
                session_id,
                user_message=None,
                *,
                attempt_id=None,
                transcript_id=None,
                resume_messages=None,
                **_kwargs,
            ):
                captured.append(
                    {
                        "step_id": step.step_id,
                        "session_id": session_id,
                        "attempt_id": attempt_id,
                        "transcript_id": transcript_id,
                        "resume_messages": resume_messages,
                        "user_message": user_message,
                        "template": context.get_conclusion("template"),
                    }
                )
                yield StepResult(step_id=step.step_id, status=StepStatus.COMPLETED, conclusion={"total": 200})

        executor = SubPipelineExecutor(
            provider_manager=MagicMock(),
            base_tool_registry=ToolRegistry(),
            pipeline=pipeline,
            pipeline_dir=tmp_path,
        )
        monkeypatch.setattr(executor, "_make_step_executor", lambda: FakeStepExecutor())

        async for _event in executor.execute_streaming(
            sub_spec=sub_spec,
            candidate={"name": "Plan A"},
            candidate_index=0,
            parent_context=parent_ctx,
            session_id="test_session",
            user_message="restored rollback context",
            resume_state=resume_state,
        ):
            pass

        assert captured == [
            {
                "step_id": "cost_estimating",
                "session_id": "test_session",
                "attempt_id": "att_0007",
                "transcript_id": "transcript_att_0007",
                "resume_messages": resume_messages,
                "user_message": None,
                "template": {"body": "already generated"},
            }
        ]

    @pytest.mark.asyncio
    async def test_execute_streaming_appends_explicit_resume_messages_to_repaired_transcript(
        self, tmp_path, monkeypatch
    ):
        (tmp_path / "prompts").mkdir(exist_ok=True)
        (tmp_path / "prompts" / "template.md").write_text("Generate template", encoding="utf-8")
        sub_spec = SubPipelineSpec(
            name="evaluate_candidate",
            steps=[
                StepSpec(
                    step_id="template_generating",
                    conclusion_field="template",
                    forward=None,
                    prompt_file="prompts/template.md",
                    skill="iac_aliyun",
                    context_fields=["candidate", "intent"],
                )
            ],
            max_rollbacks=2,
            iterate_over="architecture.candidates",
            context_fields_from_parent=["intent"],
        )
        repaired = [
            Message(
                role="assistant",
                content=[ToolUseBlock(id="toolu_1", name="ask_user_question", input={"question": "q"})],
            )
        ]
        tool_result = Message(
            role="user",
            content=[ToolResultBlock(tool_use_id="toolu_1", content='{"free_text":"see image"}', is_error=False)],
        )
        image_message = [ImageBlock(media_type="image/png", data="aGVsbG8=")]
        captured = {}

        class FakeStepExecutor:
            current_agent_loop = None

            async def execute(self, step, context, session_id, user_message=None, **kwargs):
                captured["resume_messages"] = kwargs["resume_messages"]
                captured["user_message"] = user_message
                yield StepResult(step_id=step.step_id, status=StepStatus.COMPLETED, conclusion={"body": "ok"})

        executor = SubPipelineExecutor(
            provider_manager=MagicMock(),
            base_tool_registry=ToolRegistry(),
            pipeline=LoadedPipeline(
                name="test",
                steps=[],
                context_dependencies={"intent": []},
                max_rollbacks=3,
                skills={"iac_aliyun": "# IaC Skill", "iac-aliyun-cost": "# Cost Skill"},
            ),
            pipeline_dir=tmp_path,
        )
        monkeypatch.setattr(executor, "_make_step_executor", lambda: FakeStepExecutor())
        parent_ctx = PipelineContext({"intent": []})
        parent_ctx.set_conclusion("intent", {"type": "test"})

        async for _event in executor.execute_streaming(
            sub_spec=sub_spec,
            candidate={"name": "Plan A"},
            candidate_index=0,
            parent_context=parent_ctx,
            session_id="test_session",
            user_message=image_message,
            resume_messages=[tool_result],
            resume_state={
                "current_sub_step": "template_generating",
                "active_attempt_id": "att_1",
                "transcript_id": "transcript_1",
                "resume_messages": repaired,
            },
        ):
            pass

        assert captured["resume_messages"] == [*repaired, tool_result]
        assert captured["user_message"] == image_message

    @pytest.mark.asyncio
    async def test_completed_sub_step_resume_state_starts_at_next_sub_step(self, tmp_path, monkeypatch):
        """A crash after persisting sub-step completion must resume at the next sub-step."""
        (tmp_path / "prompts").mkdir(exist_ok=True)
        (tmp_path / "prompts" / "template.md").write_text("Generate template for {candidate}", encoding="utf-8")
        (tmp_path / "prompts" / "cost.md").write_text("Estimate cost for {template}", encoding="utf-8")

        pipeline = LoadedPipeline(
            name="test",
            steps=[],
            context_dependencies={"intent": []},
            max_rollbacks=3,
            skills={"iac_aliyun": "# IaC Skill", "iac-aliyun-cost": "# Cost Skill"},
        )
        sub_spec = _make_sub_spec()
        parent_ctx = PipelineContext({"intent": []})
        parent_ctx.set_conclusion("intent", {"type": "e-commerce"})
        executed_steps = []
        persisted_states = []

        class FakeStepExecutor:
            current_agent_loop = None

            async def execute(self, step, context, session_id, **kwargs):
                executed_steps.append(step.step_id)
                conclusion = {"body": "ros_template"} if step.step_id == "template_generating" else {"total": 200}
                yield StepResult(step_id=step.step_id, status=StepStatus.COMPLETED, conclusion=conclusion)

        executor = SubPipelineExecutor(
            provider_manager=MagicMock(),
            base_tool_registry=ToolRegistry(),
            pipeline=pipeline,
            pipeline_dir=tmp_path,
        )
        monkeypatch.setattr(executor, "_make_step_executor", lambda: FakeStepExecutor())

        async for _event in executor.execute_streaming(
            sub_spec=sub_spec,
            candidate={"name": "Plan A"},
            candidate_index=0,
            parent_context=parent_ctx,
            session_id="test_session",
            sub_step_attempt_allocator=lambda request: {
                "attempt_id": f"attempt_{request['sub_step_id']}",
                "transcript_id": f"transcript_{request['sub_step_id']}",
            },
            sub_step_state_callback=lambda payload: persisted_states.append(dict(payload)),
        ):
            if any(state.get("attempt_status") == "completed" for state in persisted_states):
                break

        completed_state = next(state for state in persisted_states if state["attempt_status"] == "completed")
        assert completed_state["state_machine"]["current_index"] == 1
        assert completed_state["current_sub_step"] == "cost_estimating"

        resumed_steps = []

        class ResumeStepExecutor:
            current_agent_loop = None

            async def execute(self, step, context, session_id, **kwargs):
                resumed_steps.append(step.step_id)
                yield StepResult(step_id=step.step_id, status=StepStatus.COMPLETED, conclusion={"total": 200})

        resume_executor = SubPipelineExecutor(
            provider_manager=MagicMock(),
            base_tool_registry=ToolRegistry(),
            pipeline=pipeline,
            pipeline_dir=tmp_path,
        )
        monkeypatch.setattr(resume_executor, "_make_step_executor", lambda: ResumeStepExecutor())

        async for _event in resume_executor.execute_streaming(
            sub_spec=sub_spec,
            candidate={"name": "Plan A"},
            candidate_index=0,
            parent_context=parent_ctx,
            session_id="test_session",
            resume_state=completed_state,
        ):
            pass

        assert executed_steps == ["template_generating"]
        assert resumed_steps == ["cost_estimating"]

    @pytest.mark.asyncio
    async def test_allocator_failure_does_not_publish_previous_attempt_for_next_sub_step(self, tmp_path, monkeypatch):
        """A later allocator failure must not report the previous sub-step's attempt id."""
        (tmp_path / "prompts").mkdir(exist_ok=True)
        (tmp_path / "prompts" / "template.md").write_text("Generate template for {candidate}", encoding="utf-8")
        (tmp_path / "prompts" / "cost.md").write_text("Estimate cost for {template}", encoding="utf-8")

        pipeline = LoadedPipeline(
            name="test",
            steps=[],
            context_dependencies={"intent": []},
            max_rollbacks=3,
            skills={"iac_aliyun": "# IaC Skill", "iac-aliyun-cost": "# Cost Skill"},
        )
        sub_spec = _make_sub_spec()
        parent_ctx = PipelineContext({"intent": []})
        parent_ctx.set_conclusion("intent", {"type": "e-commerce"})
        persisted_states = []

        class FakeStepExecutor:
            current_agent_loop = None

            async def execute(self, step, context, session_id, **kwargs):
                yield StepResult(step_id=step.step_id, status=StepStatus.COMPLETED, conclusion={"ok": step.step_id})

        def allocate_attempt(request):
            if request["sub_step_id"] == "cost_estimating":
                raise RuntimeError("allocator failed token=abc123 /Users/alice/work")
            return {
                "attempt_id": f"attempt_{request['sub_step_id']}",
                "transcript_id": f"transcript_{request['sub_step_id']}",
            }

        executor = SubPipelineExecutor(
            provider_manager=MagicMock(),
            base_tool_registry=ToolRegistry(),
            pipeline=pipeline,
            pipeline_dir=tmp_path,
        )
        monkeypatch.setattr(executor, "_make_step_executor", lambda: FakeStepExecutor())

        events = [
            event
            async for event in executor.execute_streaming(
                sub_spec=sub_spec,
                candidate={"name": "Plan A"},
                candidate_index=0,
                parent_context=parent_ctx,
                session_id="test_session",
                sub_step_attempt_allocator=allocate_attempt,
                sub_step_state_callback=lambda payload: persisted_states.append(dict(payload)),
            )
        ]

        assert not any(
            state["attempt_status"] == "failed"
            and state["current_sub_step"] == "cost_estimating"
            and state["active_attempt_id"] == "attempt_template_generating"
            for state in persisted_states
        )
        terminal = [
            event
            for event in events
            if isinstance(event, PipelineEvent) and event.type == PipelineEventType.SUB_PIPELINE_COMPLETED
        ][-1]
        assert terminal.data["failed"] is True
        assert terminal.data["error_details"]["type"] == "RuntimeError"
        assert "error_id" in terminal.data["error_details"]
        assert "abc123" not in terminal.data["error_summary"]
        assert "/Users/alice" not in terminal.data["error_summary"]

    @pytest.mark.asyncio
    async def test_rollback_persistence_drops_stale_target_and_downstream_conclusions(self, tmp_path, monkeypatch):
        """Rollback-persisted resume state should not revive fields from the rolled-back range."""
        pipeline = LoadedPipeline(
            name="test",
            steps=[],
            context_dependencies={"intent": []},
            max_rollbacks=3,
            skills={},
        )
        sub_spec = SubPipelineSpec(
            name="evaluate_candidate",
            steps=[
                StepSpec(
                    step_id="template_generating",
                    conclusion_field="template",
                    forward="cost_estimating",
                    prompt_file="prompts/template.md",
                    context_fields=["candidate", "intent"],
                ),
                StepSpec(
                    step_id="cost_estimating",
                    conclusion_field="cost",
                    forward=None,
                    prompt_file="prompts/cost.md",
                    context_fields=["template"],
                ),
            ],
            max_rollbacks=2,
            iterate_over="architecture.candidates",
            context_fields_from_parent=["intent"],
        )
        parent_ctx = PipelineContext({"intent": []})
        parent_ctx.set_conclusion("intent", {"type": "e-commerce"})
        persisted_states = []

        class RollbackStepExecutor:
            current_agent_loop = None

            async def execute(self, step, context, session_id, **kwargs):
                if step.step_id == "template_generating":
                    yield StepResult(
                        step_id=step.step_id,
                        status=StepStatus.COMPLETED,
                        conclusion={"body": "stale-template"},
                    )
                    return
                yield StepResult(
                    step_id=step.step_id,
                    status=StepStatus.COMPLETED,
                    conclusion={"total": 999},
                    rollback_request=("template_generating", "needs_template_rework"),
                )

        executor = SubPipelineExecutor(
            provider_manager=MagicMock(),
            base_tool_registry=ToolRegistry(),
            pipeline=pipeline,
            pipeline_dir=tmp_path,
        )
        monkeypatch.setattr(executor, "_make_step_executor", lambda: RollbackStepExecutor())

        async for _event in executor.execute_streaming(
            sub_spec=sub_spec,
            candidate={"name": "Plan A"},
            candidate_index=0,
            parent_context=parent_ctx,
            session_id="test_session",
            sub_step_attempt_allocator=lambda request: {
                "attempt_id": f"attempt_{request['sub_step_id']}",
                "transcript_id": f"transcript_{request['sub_step_id']}",
            },
            sub_step_state_callback=lambda payload: persisted_states.append(dict(payload)),
        ):
            if any(state.get("attempt_status") == "rolled_back" for state in persisted_states):
                break

        rollback_state = next(state for state in persisted_states if state["attempt_status"] == "rolled_back")
        assert rollback_state["current_sub_step"] == "template_generating"
        assert rollback_state["conclusions"] == {}
        assert rollback_state["context"]["template"]["stale"] is True
        assert rollback_state["context"]["cost"]["stale"] is True

    @pytest.mark.asyncio
    async def test_resume_state_conclusions_do_not_rehydrate_stale_context_fields(self, tmp_path, monkeypatch):
        """Resume uses persisted conclusions instead of stale values from the context snapshot."""
        pipeline = LoadedPipeline(
            name="test",
            steps=[],
            context_dependencies={"intent": []},
            max_rollbacks=3,
            skills={},
        )
        sub_spec = SubPipelineSpec(
            name="evaluate_candidate",
            steps=[
                StepSpec(
                    step_id="designing",
                    conclusion_field="design",
                    forward="template_generating",
                    prompt_file="prompts/design.md",
                    context_fields=["candidate", "intent"],
                ),
                StepSpec(
                    step_id="template_generating",
                    conclusion_field="template",
                    forward="costing",
                    prompt_file="prompts/template.md",
                    context_fields=["design"],
                ),
                StepSpec(
                    step_id="costing",
                    conclusion_field="cost",
                    forward=None,
                    prompt_file="prompts/cost.md",
                    context_fields=["template"],
                ),
            ],
            max_rollbacks=2,
            iterate_over="architecture.candidates",
            context_fields_from_parent=["intent"],
        )
        parent_ctx = PipelineContext({"intent": []})
        parent_ctx.set_conclusion("intent", {"type": "e-commerce"})

        sub_context = PipelineContext(
            {
                "candidate": [],
                "intent": [],
                "design": [],
                "template": ["design"],
                "cost": ["template"],
            }
        )
        sub_context.set_conclusion("candidate", {"name": "Plan A"})
        sub_context.set_conclusion("intent", {"type": "e-commerce"})
        sub_context.set_conclusion("design", {"summary": "preserved"})
        sub_context.set_conclusion("template", {"body": "stale-template"})
        sub_context.set_conclusion("cost", {"total": 999})
        sub_context.mark_stale("template")

        state_machine = StateMachine(sub_spec.steps, sub_spec.max_rollbacks)
        state_machine.jump_to("costing")
        resume_state = {
            "sub_pipeline_id": "evaluate_candidate_existing",
            "context": sub_context.to_snapshot(),
            "state_machine": state_machine.to_snapshot(),
            "current_sub_step": "costing",
            "current_index": 2,
            "conclusions": {"design": {"summary": "preserved"}},
        }

        class FakeStepExecutor:
            current_agent_loop = None

            async def execute(self, step, context, session_id, **kwargs):
                assert step.step_id == "costing"
                assert context.get_field("template").stale is True
                yield StepResult(step_id=step.step_id, status=StepStatus.COMPLETED, conclusion={"total": 42})

        executor = SubPipelineExecutor(
            provider_manager=MagicMock(),
            base_tool_registry=ToolRegistry(),
            pipeline=pipeline,
            pipeline_dir=tmp_path,
        )
        monkeypatch.setattr(executor, "_make_step_executor", lambda: FakeStepExecutor())

        events = []
        async for event in executor.execute_streaming(
            sub_spec=sub_spec,
            candidate={"name": "Plan A"},
            candidate_index=0,
            parent_context=parent_ctx,
            session_id="test_session",
            resume_state=resume_state,
        ):
            events.append(event)

        completed = [
            event
            for event in events
            if isinstance(event, PipelineEvent) and event.type == PipelineEventType.SUB_PIPELINE_COMPLETED
        ]
        assert len(completed) == 1
        assert completed[0].data["conclusions"] == {
            "design": {"summary": "preserved"},
            "cost": {"total": 42},
        }

    @pytest.mark.asyncio
    async def test_executes_all_steps_for_candidate(self, tmp_path):
        """Sub-pipeline runs all steps sequentially and returns accumulated conclusions."""
        (tmp_path / "prompts").mkdir(exist_ok=True)
        (tmp_path / "prompts" / "template.md").write_text("Generate template for {candidate}", encoding="utf-8")
        (tmp_path / "prompts" / "cost.md").write_text("Estimate cost for {template}", encoding="utf-8")

        pipeline = LoadedPipeline(
            name="test",
            steps=[],
            context_dependencies={"intent": []},
            max_rollbacks=3,
            skills={"iac_aliyun": "# IaC Skill", "iac-aliyun-cost": "# Cost Skill"},
        )

        call_count = {"n": 0}
        conclusions = [{"body": "ros_template"}, {"total": 200}]

        class FakeAgentLoop:
            def __init__(self, **kwargs):
                self.system_prompt = kwargs.get("system_prompt", "")

            async def run_streaming(self, user_input):
                idx = call_count["n"]
                call_count["n"] += 1
                yield ToolUseStartEvent(tool_use_id=f"tu_{idx}", name="complete_step")
                yield ToolUseEndEvent(
                    tool_use_id=f"tu_{idx}",
                    name="complete_step",
                    input={"conclusion": conclusions[idx]},
                )
                yield ToolResultEvent(tool_use_id=f"tu_{idx}", tool_name="complete_step", result="ok")

        executor = SubPipelineExecutor(
            provider_manager=MagicMock(),
            base_tool_registry=ToolRegistry(),
            pipeline=pipeline,
            pipeline_dir=tmp_path,
        )

        sub_spec = _make_sub_spec()
        parent_ctx = PipelineContext({"intent": []})
        parent_ctx.set_conclusion("intent", {"type": "e-commerce"})

        events: list[PipelineEvent] = []
        with patch("iac_code.agent.agent_loop.AgentLoop", FakeAgentLoop):
            result = await executor.execute(
                sub_spec=sub_spec,
                candidate={"name": "Plan A", "products": ["ECS"]},
                candidate_index=0,
                parent_context=parent_ctx,
                session_id="test_session",
                event_callback=events.append,
            )

        assert result.failed is False
        assert result.conclusions["template"] == {"body": "ros_template"}
        assert result.conclusions["cost"] == {"total": 200}
        assert result.candidate == {"name": "Plan A", "products": ["ECS"]}
        assert result.candidate_index == 0

        # Verify events emitted
        event_types = [e.type for e in events]
        assert PipelineEventType.SUB_PIPELINE_STARTED in event_types
        assert PipelineEventType.SUB_STEP_STARTED in event_types
        assert PipelineEventType.SUB_STEP_COMPLETED in event_types
        assert PipelineEventType.SUB_PIPELINE_COMPLETED in event_types
        for event in events:
            if event.type in {
                PipelineEventType.SUB_PIPELINE_STARTED,
                PipelineEventType.SUB_STEP_STARTED,
                PipelineEventType.SUB_STEP_COMPLETED,
                PipelineEventType.SUB_PIPELINE_COMPLETED,
            }:
                assert event.data["sub_pipeline_id"] == result.sub_pipeline_id
                assert event.data["candidate_index"] == 0

    @pytest.mark.asyncio
    async def test_failed_step_returns_failure_result(self, tmp_path):
        """When a step fails (no complete_step call), sub-pipeline returns failure."""
        (tmp_path / "prompts").mkdir(exist_ok=True)
        (tmp_path / "prompts" / "template.md").write_text("Gen template", encoding="utf-8")
        (tmp_path / "prompts" / "cost.md").write_text("Cost", encoding="utf-8")

        pipeline = LoadedPipeline(
            name="test",
            steps=[],
            context_dependencies={"intent": []},
            max_rollbacks=3,
            skills={"iac_aliyun": "# Skill", "iac-aliyun-cost": "# Cost"},
        )

        class FailingAgentLoop:
            def __init__(self, **kwargs):
                self.system_prompt = kwargs.get("system_prompt", "")

            async def run_streaming(self, user_input):
                # Yield a non-complete_step tool — no conclusion will be extracted
                yield ToolUseStartEvent(tool_use_id="tu_1", name="some_tool")
                yield ToolUseEndEvent(tool_use_id="tu_1", name="some_tool", input={})
                yield ToolResultEvent(tool_use_id="tu_1", tool_name="some_tool", result="ok")

        executor = SubPipelineExecutor(
            provider_manager=MagicMock(),
            base_tool_registry=ToolRegistry(),
            pipeline=pipeline,
            pipeline_dir=tmp_path,
        )

        sub_spec = _make_sub_spec()
        parent_ctx = PipelineContext({"intent": []})
        parent_ctx.set_conclusion("intent", {"type": "test"})

        with patch("iac_code.agent.agent_loop.AgentLoop", FailingAgentLoop):
            result = await executor.execute(
                sub_spec=sub_spec,
                candidate={"name": "Plan B"},
                candidate_index=1,
                parent_context=parent_ctx,
                session_id="test_session",
            )

        assert result.failed is True
        assert result.error == "No conclusion extracted"

    @pytest.mark.asyncio
    async def test_execute_streaming_failed_step_emits_sub_step_failed_before_terminal(self, tmp_path, monkeypatch):
        pipeline = LoadedPipeline(
            name="test",
            steps=[],
            context_dependencies={"intent": []},
            max_rollbacks=3,
            skills={"iac_aliyun": "# Skill", "iac-aliyun-cost": "# Cost"},
        )

        class FailingStepExecutor:
            current_agent_loop = None

            async def execute(self, step, context, session_id, **kwargs):
                yield StepResult(
                    step_id=step.step_id,
                    status=StepStatus.FAILED,
                    error="step failed token=secret-value",
                )

        executor = SubPipelineExecutor(
            provider_manager=MagicMock(),
            base_tool_registry=ToolRegistry(),
            pipeline=pipeline,
            pipeline_dir=tmp_path,
        )
        monkeypatch.setattr(executor, "_make_step_executor", lambda: FailingStepExecutor())
        parent_ctx = PipelineContext({"intent": []})
        parent_ctx.set_conclusion("intent", {"type": "test"})

        events = [
            event
            async for event in executor.execute_streaming(
                sub_spec=_make_sub_spec(),
                candidate={"name": "Plan B"},
                candidate_index=1,
                parent_context=parent_ctx,
                session_id="test_session",
            )
        ]

        sub_step_failed = [
            event
            for event in events
            if isinstance(event, PipelineEvent) and event.type == PipelineEventType.SUB_STEP_FAILED
        ]
        terminal = [
            event
            for event in events
            if isinstance(event, PipelineEvent) and event.type == PipelineEventType.SUB_PIPELINE_COMPLETED
        ]
        assert len(sub_step_failed) == 1
        assert len(terminal) == 1
        assert events.index(sub_step_failed[0]) < events.index(terminal[0])
        assert sub_step_failed[0].step_id == "template_generating"
        assert sub_step_failed[0].data["error_details"]["type"] == "StepFailed"
        assert sub_step_failed[0].data["error_details"]["error_id"]
        assert "secret-value" not in str(sub_step_failed[0].data)
        assert terminal[0].data["failed"] is True

    @pytest.mark.asyncio
    async def test_context_isolation_inherits_parent_fields(self, tmp_path):
        """Sub-pipeline context inherits specified parent fields and injects candidate."""
        (tmp_path / "prompts").mkdir(exist_ok=True)
        (tmp_path / "prompts" / "template.md").write_text("{candidate} {intent}", encoding="utf-8")
        (tmp_path / "prompts" / "cost.md").write_text("{template}", encoding="utf-8")

        pipeline = LoadedPipeline(
            name="test",
            steps=[],
            context_dependencies={"intent": []},
            max_rollbacks=3,
            skills={"iac_aliyun": "# Skill", "iac-aliyun-cost": "# Cost"},
        )

        captured_prompts: list[str] = []

        class CapturingAgentLoop:
            def __init__(self, **kwargs):
                self.system_prompt = kwargs.get("system_prompt", "")
                captured_prompts.append(self.system_prompt)

            async def run_streaming(self, user_input):
                yield ToolUseStartEvent(tool_use_id="tu_1", name="complete_step")
                yield ToolUseEndEvent(
                    tool_use_id="tu_1",
                    name="complete_step",
                    input={"conclusion": {"ok": True}},
                )
                yield ToolResultEvent(tool_use_id="tu_1", tool_name="complete_step", result="ok")

        executor = SubPipelineExecutor(
            provider_manager=MagicMock(),
            base_tool_registry=ToolRegistry(),
            pipeline=pipeline,
            pipeline_dir=tmp_path,
        )

        sub_spec = _make_sub_spec()
        parent_ctx = PipelineContext({"intent": []})
        parent_ctx.set_conclusion("intent", {"type": "e-commerce"})

        with patch("iac_code.agent.agent_loop.AgentLoop", CapturingAgentLoop):
            result = await executor.execute(
                sub_spec=sub_spec,
                candidate={"name": "Plan A"},
                candidate_index=0,
                parent_context=parent_ctx,
                session_id="test_session",
            )

        assert result.failed is False
        # First step prompt should contain the candidate data and inherited intent
        first_prompt = captured_prompts[0]
        assert "Plan A" in first_prompt
        assert "e-commerce" in first_prompt

    @pytest.mark.asyncio
    async def test_no_event_callback_does_not_crash(self, tmp_path):
        """Execute works fine without an event callback."""
        (tmp_path / "prompts").mkdir(exist_ok=True)
        (tmp_path / "prompts" / "template.md").write_text("go", encoding="utf-8")
        (tmp_path / "prompts" / "cost.md").write_text("go", encoding="utf-8")

        pipeline = LoadedPipeline(
            name="test",
            steps=[],
            context_dependencies={"intent": []},
            max_rollbacks=3,
            skills={"iac_aliyun": "# Skill", "iac-aliyun-cost": "# Cost"},
        )

        class SimpleAgentLoop:
            def __init__(self, **kwargs):
                self.system_prompt = kwargs.get("system_prompt", "")

            async def run_streaming(self, user_input):
                yield ToolUseStartEvent(tool_use_id="tu_1", name="complete_step")
                yield ToolUseEndEvent(
                    tool_use_id="tu_1",
                    name="complete_step",
                    input={"conclusion": {"data": "x"}},
                )
                yield ToolResultEvent(tool_use_id="tu_1", tool_name="complete_step", result="ok")

        executor = SubPipelineExecutor(
            provider_manager=MagicMock(),
            base_tool_registry=ToolRegistry(),
            pipeline=pipeline,
            pipeline_dir=tmp_path,
        )

        sub_spec = _make_sub_spec()
        parent_ctx = PipelineContext({"intent": []})
        parent_ctx.set_conclusion("intent", {"type": "test"})

        with patch("iac_code.agent.agent_loop.AgentLoop", SimpleAgentLoop):
            result = await executor.execute(
                sub_spec=sub_spec,
                candidate={"name": "Plan C"},
                candidate_index=2,
                parent_context=parent_ctx,
                session_id="test_session",
                event_callback=None,
            )

        assert result.failed is False

    @pytest.mark.asyncio
    async def test_exception_in_step_returns_failure(self, tmp_path):
        """An unexpected exception during execution is caught and returned as failure."""
        (tmp_path / "prompts").mkdir(exist_ok=True)
        (tmp_path / "prompts" / "template.md").write_text("go", encoding="utf-8")
        (tmp_path / "prompts" / "cost.md").write_text("go", encoding="utf-8")

        pipeline = LoadedPipeline(
            name="test",
            steps=[],
            context_dependencies={"intent": []},
            max_rollbacks=3,
            skills={"iac_aliyun": "# Skill", "iac-aliyun-cost": "# Cost"},
        )

        class ExplodingAgentLoop:
            def __init__(self, **kwargs):
                self.system_prompt = kwargs.get("system_prompt", "")

            async def run_streaming(self, user_input):
                raise RuntimeError("LLM provider unavailable")
                yield  # noqa: F841 — unreachable but needed to make this an async generator

        executor = SubPipelineExecutor(
            provider_manager=MagicMock(),
            base_tool_registry=ToolRegistry(),
            pipeline=pipeline,
            pipeline_dir=tmp_path,
        )

        sub_spec = _make_sub_spec()
        parent_ctx = PipelineContext({"intent": []})
        parent_ctx.set_conclusion("intent", {"type": "test"})

        with patch("iac_code.agent.agent_loop.AgentLoop", ExplodingAgentLoop):
            result = await executor.execute(
                sub_spec=sub_spec,
                candidate={"name": "Plan X"},
                candidate_index=0,
                parent_context=parent_ctx,
                session_id="test_session",
            )

        assert result.failed is True
        assert "LLM provider unavailable" in result.error

    @pytest.mark.asyncio
    async def test_parent_field_not_in_parent_context_is_skipped(self, tmp_path):
        """When a parent field is specified but not set in parent context, it's skipped gracefully."""
        (tmp_path / "prompts").mkdir(exist_ok=True)
        (tmp_path / "prompts" / "template.md").write_text("go", encoding="utf-8")
        (tmp_path / "prompts" / "cost.md").write_text("go", encoding="utf-8")

        pipeline = LoadedPipeline(
            name="test",
            steps=[],
            context_dependencies={"intent": []},
            max_rollbacks=3,
            skills={"iac_aliyun": "# Skill", "iac-aliyun-cost": "# Cost"},
        )

        class SimpleAgentLoop:
            def __init__(self, **kwargs):
                self.system_prompt = kwargs.get("system_prompt", "")

            async def run_streaming(self, user_input):
                yield ToolUseStartEvent(tool_use_id="tu_1", name="complete_step")
                yield ToolUseEndEvent(
                    tool_use_id="tu_1",
                    name="complete_step",
                    input={"conclusion": {"ok": True}},
                )
                yield ToolResultEvent(tool_use_id="tu_1", tool_name="complete_step", result="ok")

        executor = SubPipelineExecutor(
            provider_manager=MagicMock(),
            base_tool_registry=ToolRegistry(),
            pipeline=pipeline,
            pipeline_dir=tmp_path,
        )

        sub_spec = _make_sub_spec()
        # Parent context has "intent" field but its value is None (not set)
        parent_ctx = PipelineContext({"intent": []})

        with patch("iac_code.agent.agent_loop.AgentLoop", SimpleAgentLoop):
            result = await executor.execute(
                sub_spec=sub_spec,
                candidate={"name": "Plan D"},
                candidate_index=0,
                parent_context=parent_ctx,
                session_id="test_session",
            )

        # Should not crash — just skip the missing parent field
        assert result.failed is False


class TestStartFromStepValidation:
    """P-I4: invalid start_from_step must produce SUB_PIPELINE_STARTED +
    a SUB_PIPELINE_COMPLETED(failed=True) event, not a silent ValueError
    out of the async generator."""

    @pytest.mark.asyncio
    async def test_invalid_start_from_step_yields_failed_event(self, tmp_path):
        """Non-existent start_from_step should be caught and surfaced as failure.

        Before the fix, ``state_machine.jump_to`` is called BEFORE the
        SUB_PIPELINE_STARTED yield AND outside any try/except, so a
        ValueError propagates out of the generator silently. The UI never
        sees that the candidate started, and never sees a failure.
        """
        pipeline = LoadedPipeline(
            name="test",
            steps=[],
            context_dependencies={"intent": []},
            max_rollbacks=3,
            skills={"iac_aliyun": "# Skill", "iac-aliyun-cost": "# Cost"},
        )

        executor = SubPipelineExecutor(
            provider_manager=MagicMock(),
            base_tool_registry=ToolRegistry(),
            pipeline=pipeline,
            pipeline_dir=tmp_path,
        )

        sub_spec = _make_sub_spec()
        parent_ctx = PipelineContext({"intent": []})
        parent_ctx.set_conclusion("intent", {"type": "test"})

        events: list = []
        async for ev in executor.execute_streaming(
            sub_spec=sub_spec,
            candidate={"name": "Plan A"},
            candidate_index=0,
            parent_context=parent_ctx,
            session_id="test_session",
            start_from_step="this_step_does_not_exist",
        ):
            events.append(ev)

        # SUB_PIPELINE_STARTED must be emitted first (so UI knows the candidate began).
        started = [
            e for e in events if isinstance(e, PipelineEvent) and e.type == PipelineEventType.SUB_PIPELINE_STARTED
        ]
        assert len(started) == 1, f"missing SUB_PIPELINE_STARTED in events: {events!r}"

        # SUB_PIPELINE_COMPLETED with failed=True must be the terminal event.
        completed = [
            e for e in events if isinstance(e, PipelineEvent) and e.type == PipelineEventType.SUB_PIPELINE_COMPLETED
        ]
        assert len(completed) == 1, f"expected exactly 1 SUB_PIPELINE_COMPLETED, got {completed!r}"
        assert completed[0].data.get("failed") is True, (
            f"SUB_PIPELINE_COMPLETED should be failed=True, got: {completed[0].data!r}"
        )
        assert completed[0].data["error_details"]["type"] == "ValueError"
        assert completed[0].data["error_details"]["traceback"] == "Stack trace omitted from public event; see error_id."
        assert "Traceback" not in str(completed[0].data)
        error = completed[0].data.get("error") or ""
        assert "this_step_does_not_exist" in error, f"error should mention invalid step id, got: {error!r}"


class TestSubPipelineRollbackEmitsSubStepFailed:
    """P-I9: sub-pipeline rollback ValueError must emit SUB_STEP_FAILED event,
    mirroring parent runner's P-C3 STEP_FAILED behavior. The event provides
    a structured failure record for logging/judge state (separate from the
    user-visible SUB_PIPELINE_COMPLETED(failed=True) terminal event)."""

    @pytest.mark.asyncio
    async def test_invalid_rollback_target_emits_sub_step_failed_event(self, tmp_path):
        """When a sub-step returns rollback_request with a non-existent target,
        execute_streaming should yield a SUB_STEP_FAILED event with valid
        targets in the error message before yielding the terminal completion."""
        (tmp_path / "prompts").mkdir(exist_ok=True)
        (tmp_path / "prompts" / "template.md").write_text("Gen template", encoding="utf-8")
        (tmp_path / "prompts" / "cost.md").write_text("Cost", encoding="utf-8")

        pipeline = LoadedPipeline(
            name="test",
            steps=[],
            context_dependencies={"intent": []},
            max_rollbacks=3,
            skills={"iac_aliyun": "# Skill", "iac-aliyun-cost": "# Cost"},
        )

        class RollbackToInvalidTargetAgentLoop:
            """First step returns a rollback_request to a non-existent step id."""

            def __init__(self, **kwargs):
                self.system_prompt = kwargs.get("system_prompt", "")

            async def run_streaming(self, user_input):
                yield ToolUseStartEvent(tool_use_id="tu_1", name="complete_step")
                yield ToolUseEndEvent(
                    tool_use_id="tu_1",
                    name="complete_step",
                    input={
                        "conclusion": {"body": "template_x"},
                        "rollback_request": {
                            "target_step": "totally_invalid_step_id",
                            "reason": "user wants reset",
                        },
                    },
                )
                yield ToolResultEvent(tool_use_id="tu_1", tool_name="complete_step", result="ok")

        executor = SubPipelineExecutor(
            provider_manager=MagicMock(),
            base_tool_registry=ToolRegistry(),
            pipeline=pipeline,
            pipeline_dir=tmp_path,
        )

        sub_spec = _make_sub_spec()
        parent_ctx = PipelineContext({"intent": []})
        parent_ctx.set_conclusion("intent", {"type": "test"})

        events: list = []
        with patch("iac_code.agent.agent_loop.AgentLoop", RollbackToInvalidTargetAgentLoop):
            async for ev in executor.execute_streaming(
                sub_spec=sub_spec,
                candidate={"name": "Plan A"},
                candidate_index=0,
                parent_context=parent_ctx,
                session_id="test_session",
            ):
                events.append(ev)

        # SUB_STEP_FAILED event should appear with the invalid target and valid targets.
        failed_events = [
            e for e in events if isinstance(e, PipelineEvent) and e.type == PipelineEventType.SUB_STEP_FAILED
        ]
        assert len(failed_events) == 1, f"missing SUB_STEP_FAILED in events: {events!r}"
        err = failed_events[0].data.get("error", "")
        assert "totally_invalid_step_id" in err, f"invalid target missing in: {err!r}"
        # Valid targets should also be listed so the user/judge can pick a real one.
        assert "template_generating" in err or "valid" in err.lower(), f"valid targets should be in error: {err!r}"
        assert failed_events[0].data["error_summary"] == err
        assert failed_events[0].data["error_details"]["type"] == "InvalidRollbackTarget"
        assert failed_events[0].data["error_details"]["target"] == "totally_invalid_step_id"

    @pytest.mark.asyncio
    async def test_invalid_rollback_target_sub_step_failed_event_redacts_public_error(self, tmp_path):
        (tmp_path / "prompts").mkdir(exist_ok=True)
        (tmp_path / "prompts" / "template.md").write_text("Gen template", encoding="utf-8")
        (tmp_path / "prompts" / "cost.md").write_text("Cost", encoding="utf-8")
        pipeline = LoadedPipeline(
            name="test",
            steps=[],
            context_dependencies={"intent": []},
            max_rollbacks=3,
            skills={"iac_aliyun": "# Skill", "iac-aliyun-cost": "# Cost"},
        )

        class RollbackToSensitiveInvalidTargetAgentLoop:
            def __init__(self, **kwargs):
                self.system_prompt = kwargs.get("system_prompt", "")

            async def run_streaming(self, user_input):
                yield ToolUseStartEvent(tool_use_id="tu_1", name="complete_step")
                yield ToolUseEndEvent(
                    tool_use_id="tu_1",
                    name="complete_step",
                    input={
                        "conclusion": {"body": "template_x"},
                        "rollback_request": {
                            "target_step": "bad_token=abc123",
                            "reason": "user wants reset",
                        },
                    },
                )
                yield ToolResultEvent(tool_use_id="tu_1", tool_name="complete_step", result="ok")

        executor = SubPipelineExecutor(
            provider_manager=MagicMock(),
            base_tool_registry=ToolRegistry(),
            pipeline=pipeline,
            pipeline_dir=tmp_path,
        )
        parent_ctx = PipelineContext({"intent": []})
        parent_ctx.set_conclusion("intent", {"type": "test"})

        events: list = []
        with patch("iac_code.agent.agent_loop.AgentLoop", RollbackToSensitiveInvalidTargetAgentLoop):
            async for ev in executor.execute_streaming(
                sub_spec=_make_sub_spec(),
                candidate={"name": "Plan A"},
                candidate_index=0,
                parent_context=parent_ctx,
                session_id="test_session",
            ):
                events.append(ev)

        failed_event = next(
            e for e in events if isinstance(e, PipelineEvent) and e.type == PipelineEventType.SUB_STEP_FAILED
        )
        rendered = str(failed_event.data)
        assert "abc123" not in rendered
        assert "bad_token=[REDACTED]" in failed_event.data["error"]
        assert failed_event.data["error_details"]["target"] == "bad_token=[REDACTED]"


class TestBuildSubContext:
    def test_candidate_injected(self):
        sub_spec = _make_sub_spec()
        parent_ctx = PipelineContext({"intent": []})
        parent_ctx.set_conclusion("intent", {"type": "blog"})

        executor = SubPipelineExecutor(
            provider_manager=MagicMock(),
            base_tool_registry=ToolRegistry(),
            pipeline=MagicMock(),
            pipeline_dir=Path("/tmp"),
        )

        ctx = executor._build_sub_context(sub_spec, {"name": "Plan A"}, parent_ctx)
        assert ctx.get_conclusion("candidate") == {"name": "Plan A"}

    def test_parent_fields_copied(self):
        sub_spec = _make_sub_spec()
        parent_ctx = PipelineContext({"intent": []})
        parent_ctx.set_conclusion("intent", {"type": "blog", "scale": "small"})

        executor = SubPipelineExecutor(
            provider_manager=MagicMock(),
            base_tool_registry=ToolRegistry(),
            pipeline=MagicMock(),
            pipeline_dir=Path("/tmp"),
        )

        ctx = executor._build_sub_context(sub_spec, {"name": "Plan B"}, parent_ctx)
        assert ctx.get_conclusion("intent") == {"type": "blog", "scale": "small"}

    def test_step_conclusion_fields_present(self):
        sub_spec = _make_sub_spec()
        parent_ctx = PipelineContext({"intent": []})
        parent_ctx.set_conclusion("intent", {"type": "test"})

        executor = SubPipelineExecutor(
            provider_manager=MagicMock(),
            base_tool_registry=ToolRegistry(),
            pipeline=MagicMock(),
            pipeline_dir=Path("/tmp"),
        )

        ctx = executor._build_sub_context(sub_spec, {"name": "P"}, parent_ctx)
        # "template" and "cost" fields should exist (from steps), initially None
        assert ctx.get_conclusion("template") is None
        assert ctx.get_conclusion("cost") is None


class TestFailedCandidateErrorPropagated:
    """P-I22: SubPipelineResult carries error_details; the parallel runner
    surfaces both error_summary + error_details in SUB_PIPELINE_COMPLETED data."""

    def test_sub_pipeline_result_has_error_details_field(self):
        from iac_code.pipeline.engine.sub_pipeline_executor import SubPipelineResult

        result = SubPipelineResult(
            sub_pipeline_id="s1",
            candidate_index=0,
            candidate={"name": "test"},
            conclusions={},
            failed=True,
            error="boom",
            error_details={"type": "ValueError", "traceback": "Traceback...\n"},
        )
        assert result.error == "boom"
        assert result.error_details["type"] == "ValueError"

    @pytest.mark.asyncio
    async def test_execute_exception_error_details_do_not_expose_traceback(self, tmp_path, monkeypatch):
        async def failing_execute(*args, **kwargs):
            raise RuntimeError("secret stack path token=abc123 /Users/alice/project")
            if False:
                yield

        monkeypatch.setattr("iac_code.pipeline.engine.sub_pipeline_executor.StepExecutor.execute", failing_execute)
        pipeline = LoadedPipeline(
            name="test",
            steps=[],
            context_dependencies={"intent": []},
            max_rollbacks=3,
            skills={"iac_aliyun": "# Skill", "iac-aliyun-cost": "# Cost"},
        )
        executor = SubPipelineExecutor(
            provider_manager=MagicMock(),
            base_tool_registry=ToolRegistry(),
            pipeline=pipeline,
            pipeline_dir=tmp_path,
        )
        parent_ctx = PipelineContext({"intent": []})
        parent_ctx.set_conclusion("intent", {"type": "test"})

        result = await executor.execute(
            sub_spec=_make_sub_spec(),
            candidate={"name": "Plan A"},
            candidate_index=0,
            parent_context=parent_ctx,
            session_id="test_session",
        )

        assert result.failed is True
        assert result.error_details["type"] == "RuntimeError"
        assert "error_id" in result.error_details
        assert "abc123" not in result.error
        assert "/Users/alice" not in result.error
        assert "Traceback" not in str(result.to_dict())

    def test_sub_pipeline_result_error_details_defaults_none(self):
        from iac_code.pipeline.engine.sub_pipeline_executor import SubPipelineResult

        result = SubPipelineResult(
            sub_pipeline_id="s1",
            candidate_index=0,
            candidate={"name": "test"},
            conclusions={},
        )
        assert result.error_details is None
