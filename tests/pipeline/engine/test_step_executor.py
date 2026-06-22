import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, call, patch

import pytest

from iac_code.agent.message import Message, ToolResultBlock, ToolUseBlock
from iac_code.pipeline.engine.context import PipelineContext
from iac_code.pipeline.engine.step_executor import StepExecutor
from iac_code.pipeline.engine.step_spec import IncludeExcludeConfig, LoadedPipeline, StepSpec
from iac_code.pipeline.engine.types import StepResult, StepStatus
from iac_code.tools.base import ToolContext, ToolRegistry
from iac_code.types.stream_events import (
    AskUserQuestionEvent,
    MessageEndEvent,
    MessageStartEvent,
    TextDeltaEvent,
    ToolResultEvent,
    ToolUseEndEvent,
    ToolUseStartEvent,
    Usage,
)

SIMPLE_DEPS = {"intent": [], "architecture": ["intent"]}


def _make_step() -> StepSpec:
    return StepSpec(
        step_id="intent_parsing",
        conclusion_field="intent",
        forward="architecture_planning",
        prompt_file="prompts/intent_parsing.md",
        skill="iac-aliyun-intent",
    )


def _make_architecture_step() -> StepSpec:
    return StepSpec(
        step_id="architecture_planning",
        conclusion_field="architecture",
        forward="evaluate_candidates",
        prompt_file="prompts/architecture_planning.md",
        skill="iac-aliyun-architecture",
        conclusion_schema={
            "type": "object",
            "required": ["candidates"],
            "properties": {
                "candidates": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "required": ["name", "output_path", "products", "topology", "monthly_estimate", "pros", "cons"],
                    },
                }
            },
        },
    )


def _make_pipeline(skill_content: str = "# Skill") -> LoadedPipeline:
    return LoadedPipeline(
        name="test",
        steps=[_make_step(), _make_architecture_step()],
        context_dependencies=SIMPLE_DEPS,
        max_rollbacks=3,
        skills={"iac-aliyun-intent": skill_content, "iac-aliyun-architecture": "# Architecture"},
    )


def _make_executor(tmp_path: Path) -> StepExecutor:
    (tmp_path / "prompts").mkdir(exist_ok=True)
    (tmp_path / "prompts" / "intent_parsing.md").write_text("Parse intent.", encoding="utf-8")
    (tmp_path / "prompts" / "architecture_planning.md").write_text("Plan architecture.", encoding="utf-8")
    return StepExecutor(
        provider_manager=MagicMock(),
        base_tool_registry=ToolRegistry(),
        pipeline=_make_pipeline(),
        pipeline_dir=tmp_path,
    )


def _make_fake_agent_loop_class(events_to_yield):
    class FakeAgentLoop:
        def __init__(self, **kwargs):
            self.system_prompt = kwargs.get("system_prompt", "")
            self.tool_registry = kwargs.get("tool_registry")

        async def run_streaming(self, user_input):
            for event in events_to_yield:
                yield event

    return FakeAgentLoop


class TestStepExecutorToolSetup:
    def test_complete_step_tool_registered(self, tmp_path):
        executor = _make_executor(tmp_path)
        step = _make_step()
        ctx = PipelineContext(SIMPLE_DEPS)
        tool_reg = executor._build_step_tools(step, ctx)
        assert tool_reg.get("complete_step") is not None

    def test_full_tools_when_step_returns_none(self, tmp_path):
        registry = ToolRegistry()

        class DummyTool:
            name = "dummy"

        registry._tools["dummy"] = DummyTool()

        (tmp_path / "prompts").mkdir(exist_ok=True)
        (tmp_path / "prompts" / "intent_parsing.md").write_text("Parse.", encoding="utf-8")
        executor = StepExecutor(
            provider_manager=MagicMock(),
            base_tool_registry=registry,
            pipeline=_make_pipeline(),
            pipeline_dir=tmp_path,
        )
        step = _make_step()
        ctx = PipelineContext(SIMPLE_DEPS)
        tool_reg = executor._build_step_tools(step, ctx)
        assert tool_reg.get("dummy") is not None
        assert tool_reg.get("complete_step") is not None


class TestStepExecutor:
    @pytest.mark.asyncio
    async def test_yields_agent_events_and_step_result(self, tmp_path):
        events = [
            TextDeltaEvent(text="Analyzing..."),
            ToolUseStartEvent(tool_use_id="tu_1", name="complete_step"),
            ToolUseEndEvent(
                tool_use_id="tu_1",
                name="complete_step",
                input={"conclusion": {"type": "e-commerce"}},
            ),
            ToolResultEvent(tool_use_id="tu_1", tool_name="complete_step", result="步骤完成"),
        ]
        fake_loop = _make_fake_agent_loop_class(events)
        executor = _make_executor(tmp_path)
        step = _make_step()
        ctx = PipelineContext(SIMPLE_DEPS)

        collected = []
        with patch("iac_code.agent.agent_loop.AgentLoop", fake_loop):
            async for event in executor.execute(step, ctx, "test_session"):
                collected.append(event)

        text_events = [e for e in collected if isinstance(e, TextDeltaEvent)]
        assert len(text_events) == 1
        assert text_events[0].text == "Analyzing..."

        results = [e for e in collected if isinstance(e, StepResult)]
        assert len(results) == 1
        assert results[0].conclusion == {"type": "e-commerce"}

    @pytest.mark.asyncio
    async def test_detects_rollback_request(self, tmp_path):
        events = [
            ToolUseStartEvent(tool_use_id="tu_1", name="complete_step"),
            ToolUseEndEvent(
                tool_use_id="tu_1",
                name="complete_step",
                input={
                    "conclusion": {"cost": 5000},
                    "rollback_request": {"target_step": "spec_recommending", "reason": "cost_too_high"},
                },
            ),
            ToolResultEvent(tool_use_id="tu_1", tool_name="complete_step", result="ok"),
        ]
        fake_loop = _make_fake_agent_loop_class(events)
        executor = _make_executor(tmp_path)
        step = _make_step()
        ctx = PipelineContext(SIMPLE_DEPS)

        collected = []
        with patch("iac_code.agent.agent_loop.AgentLoop", fake_loop):
            async for event in executor.execute(step, ctx, "test_session"):
                collected.append(event)

        results = [e for e in collected if isinstance(e, StepResult)]
        assert results[0].rollback_request == ("spec_recommending", "cost_too_high")

    @pytest.mark.asyncio
    async def test_failed_when_no_complete_step(self, tmp_path):
        events = [TextDeltaEvent(text="Done analyzing.")]
        fake_loop = _make_fake_agent_loop_class(events)
        executor = _make_executor(tmp_path)
        step = _make_step()
        ctx = PipelineContext(SIMPLE_DEPS)

        collected = []
        with patch("iac_code.agent.agent_loop.AgentLoop", fake_loop):
            async for event in executor.execute(step, ctx, "test_session"):
                collected.append(event)

        results = [e for e in collected if isinstance(e, StepResult)]
        assert len(results) == 1
        assert results[0].error == "No conclusion extracted"

    @pytest.mark.asyncio
    async def test_nudge_retry_succeeds_on_second_attempt(self, tmp_path, caplog):
        """When LLM forgets complete_step on first attempt, nudge makes it call on retry."""
        call_count = [0]
        first_events = [TextDeltaEvent(text="Waiting for user...")]
        second_events = [
            ToolUseStartEvent(tool_use_id="tu_1", name="complete_step"),
            ToolUseEndEvent(tool_use_id="tu_1", name="complete_step", input={"conclusion": {"deployed": True}}),
            ToolResultEvent(tool_use_id="tu_1", tool_name="complete_step", result="ok"),
        ]

        class FakeAgentLoopMultiCall:
            def __init__(self, **kwargs):
                self.system_prompt = kwargs.get("system_prompt", "")
                self.tool_registry = kwargs.get("tool_registry")

            async def run_streaming(self, user_input):
                call_count[0] += 1
                events = first_events if call_count[0] == 1 else second_events
                for event in events:
                    yield event

        executor = _make_executor(tmp_path)
        executor._observability.step_nudged = MagicMock()
        step = _make_step()
        ctx = PipelineContext(SIMPLE_DEPS)
        caplog.set_level(logging.INFO, logger="iac_code.pipeline.engine.step_executor")

        collected = []
        with patch("iac_code.agent.agent_loop.AgentLoop", FakeAgentLoopMultiCall):
            async for event in executor.execute(step, ctx, "test_session"):
                collected.append(event)

        results = [e for e in collected if isinstance(e, StepResult)]
        assert len(results) == 1
        assert results[0].status.value == "completed"
        assert results[0].conclusion == {"deployed": True}
        assert call_count[0] == 2
        executor._observability.step_nudged.assert_called_once_with(
            step_id="intent_parsing",
            nudge_count=1,
            max_nudges=2,
            session_id="test_session",
        )

        expected_log_message = (
            "Pipeline step nudge issued: step_id=intent_parsing nudge_count=1 max_nudges=2 session_id=test_session"
        )
        nudge_logs = [record for record in caplog.records if record.message == expected_log_message]
        assert len(nudge_logs) == 1
        assert nudge_logs[0].step_id == "intent_parsing"
        assert nudge_logs[0].nudge_count == 1
        assert nudge_logs[0].max_nudges == 2
        assert nudge_logs[0].session_id == "test_session"

    @pytest.mark.asyncio
    async def test_nudge_after_invalid_complete_step_includes_schema_error_and_wrapper(self, tmp_path):
        call_inputs: list[str] = []

        class FakeAgentLoopInvalidThenValid:
            def __init__(self, **kwargs):
                self.system_prompt = kwargs.get("system_prompt", "")
                self.tool_registry = kwargs.get("tool_registry")

            async def run_streaming(self, user_input):
                call_inputs.append(user_input)
                if len(call_inputs) == 1:
                    yield ToolUseStartEvent(tool_use_id="tu_bad", name="complete_step")
                    yield ToolUseEndEvent(
                        tool_use_id="tu_bad",
                        name="complete_step",
                        input={"is_infra_intent": True, "confidence": "medium"},
                    )
                    yield ToolResultEvent(
                        tool_use_id="tu_bad",
                        tool_name="complete_step",
                        result=(
                            "Invalid input for tool 'complete_step': 'conclusion' is a required property. "
                            "Please provide all required parameters as defined in the tool schema."
                        ),
                        is_error=True,
                    )
                    return

                yield ToolUseStartEvent(tool_use_id="tu_good", name="complete_step")
                yield ToolUseEndEvent(
                    tool_use_id="tu_good",
                    name="complete_step",
                    input={"conclusion": {"is_infra_intent": True, "confidence": "medium"}},
                )
                yield ToolResultEvent(tool_use_id="tu_good", tool_name="complete_step", result="ok")

        executor = _make_executor(tmp_path)
        executor._observability.step_nudged = MagicMock()
        step = _make_step()
        ctx = PipelineContext(SIMPLE_DEPS)

        collected = []
        with patch("iac_code.agent.agent_loop.AgentLoop", FakeAgentLoopInvalidThenValid):
            async for event in executor.execute(step, ctx, "test_session"):
                collected.append(event)

        assert len(call_inputs) == 2
        retry_prompt = call_inputs[1]
        assert "上一次 complete_step 调用失败" in retry_prompt
        assert "'conclusion' is a required property" in retry_prompt
        assert '{"conclusion": {...}}' in retry_prompt
        assert "不要重复" in retry_prompt
        assert "is_infra_intent" in retry_prompt

        results = [event for event in collected if isinstance(event, StepResult)]
        assert results[-1].status == StepStatus.COMPLETED

    @pytest.mark.asyncio
    async def test_nudge_after_invalid_complete_step_uses_current_step_schema(self, tmp_path):
        call_inputs: list[str] = []

        class FakeAgentLoopInvalidThenValid:
            def __init__(self, **kwargs):
                self.system_prompt = kwargs.get("system_prompt", "")
                self.tool_registry = kwargs.get("tool_registry")

            async def run_streaming(self, user_input):
                call_inputs.append(user_input)
                if len(call_inputs) == 1:
                    yield ToolUseStartEvent(tool_use_id="tu_bad", name="complete_step")
                    yield ToolUseEndEvent(tool_use_id="tu_bad", name="complete_step", input={})
                    yield ToolResultEvent(
                        tool_use_id="tu_bad",
                        tool_name="complete_step",
                        result=(
                            "Invalid input for tool 'complete_step': 'conclusion' is a required property. "
                            "Please provide all required parameters as defined in the tool schema."
                        ),
                        is_error=True,
                    )
                    return

                yield ToolUseStartEvent(tool_use_id="tu_good", name="complete_step")
                yield ToolUseEndEvent(
                    tool_use_id="tu_good",
                    name="complete_step",
                    input={"conclusion": {"candidates": []}},
                )
                yield ToolResultEvent(tool_use_id="tu_good", tool_name="complete_step", result="ok")

        executor = _make_executor(tmp_path)
        step = _make_architecture_step()
        ctx = PipelineContext(SIMPLE_DEPS)

        collected = []
        with patch("iac_code.agent.agent_loop.AgentLoop", FakeAgentLoopInvalidThenValid):
            async for event in executor.execute(step, ctx, "test_session"):
                collected.append(event)

        assert len(call_inputs) == 2
        retry_prompt = call_inputs[1]
        assert "architecture_planning" in retry_prompt
        assert "candidates" in retry_prompt
        assert "is_infra_intent" not in retry_prompt

        results = [event for event in collected if isinstance(event, StepResult)]
        assert results[-1].status == StepStatus.COMPLETED

    @pytest.mark.asyncio
    async def test_fresh_recovery_loop_after_repeated_empty_complete_step(self, tmp_path):
        call_inputs: list[tuple[int, str]] = []
        instances: list[object] = []

        class FakeAgentLoopRepeatedInvalidThenFreshValid:
            def __init__(self, **kwargs):
                self.index = len(instances)
                instances.append(self)

            async def run_streaming(self, user_input):
                call_inputs.append((self.index, user_input))
                if self.index == 0:
                    yield ToolUseStartEvent(tool_use_id=f"tu_bad_{len(call_inputs)}", name="complete_step")
                    yield ToolUseEndEvent(
                        tool_use_id=f"tu_bad_{len(call_inputs)}",
                        name="complete_step",
                        input={},
                    )
                    yield ToolResultEvent(
                        tool_use_id=f"tu_bad_{len(call_inputs)}",
                        tool_name="complete_step",
                        result=(
                            "Invalid input for tool 'complete_step': 'conclusion' is a required property\n"
                            "当前步骤：architecture_planning"
                        ),
                        is_error=True,
                    )
                    return

                yield ToolUseStartEvent(tool_use_id="tu_good", name="complete_step")
                yield ToolUseEndEvent(
                    tool_use_id="tu_good",
                    name="complete_step",
                    input={"conclusion": {"candidates": [{"name": "基础方案"}]}},
                )
                yield ToolResultEvent(tool_use_id="tu_good", tool_name="complete_step", result="ok")

        executor = _make_executor(tmp_path)
        step = _make_architecture_step()
        ctx = PipelineContext(SIMPLE_DEPS)

        collected = []
        with patch("iac_code.agent.agent_loop.AgentLoop", FakeAgentLoopRepeatedInvalidThenFreshValid):
            async for event in executor.execute(step, ctx, "test_session"):
                collected.append(event)

        assert len(instances) == 2
        assert [index for index, _ in call_inputs] == [0, 0, 0, 1]
        assert "重新执行当前步骤" in call_inputs[-1][1]
        assert "architecture_planning" in call_inputs[-1][1]
        assert "candidates" in call_inputs[-1][1]

        results = [event for event in collected if isinstance(event, StepResult)]
        assert results[-1].status == StepStatus.COMPLETED
        assert results[-1].conclusion == {"candidates": [{"name": "基础方案"}]}

    @pytest.mark.asyncio
    async def test_fresh_recovery_loop_preserves_rollback_budget(self, tmp_path):
        instances: list[object] = []

        class FakeAgentLoopRepeatedInvalidThenRollback:
            def __init__(self, **kwargs):
                self.index = len(instances)
                self.tool_registry = kwargs.get("tool_registry")
                instances.append(self)

            async def run_streaming(self, user_input):
                if self.index == 0:
                    yield ToolUseStartEvent(tool_use_id="tu_bad", name="complete_step")
                    yield ToolUseEndEvent(tool_use_id="tu_bad", name="complete_step", input={})
                    yield ToolResultEvent(
                        tool_use_id="tu_bad",
                        tool_name="complete_step",
                        result=(
                            "Invalid input for tool 'complete_step': 'conclusion' is a required property\n"
                            "当前步骤：architecture_planning"
                        ),
                        is_error=True,
                    )
                    return

                tool = self.tool_registry.get("complete_step")
                tool_input = {
                    "conclusion": {
                        "candidates": [
                            {
                                "name": "基础方案",
                                "output_path": "plan-a.yaml",
                                "products": ["ECS"],
                                "topology": "single node",
                                "monthly_estimate": "CNY 100",
                                "pros": ["simple"],
                                "cons": ["limited"],
                            }
                        ]
                    },
                    "rollback_request": {"target_step": "intent_parsing", "reason": "redo"},
                }
                tool_result = await tool.execute(tool_input=tool_input, context=ToolContext())
                yield ToolUseStartEvent(tool_use_id="tu_recovery", name="complete_step")
                yield ToolUseEndEvent(tool_use_id="tu_recovery", name="complete_step", input=tool_input)
                yield ToolResultEvent(
                    tool_use_id="tu_recovery",
                    tool_name="complete_step",
                    result=tool_result.content,
                    is_error=tool_result.is_error,
                    metadata=tool_result.metadata,
                )

        executor = _make_executor(tmp_path)
        step = _make_architecture_step()
        ctx = PipelineContext(SIMPLE_DEPS)

        collected = []
        with patch("iac_code.agent.agent_loop.AgentLoop", FakeAgentLoopRepeatedInvalidThenRollback):
            async for event in executor.execute(
                step,
                ctx,
                "test_session",
                rollback_targets=["intent_parsing"],
                rollback_count=5,
                max_rollbacks=5,
            ):
                collected.append(event)

        results = [event for event in collected if isinstance(event, StepResult)]
        assert len(instances) == 2
        assert results[-1].status == StepStatus.FAILED
        assert results[-1].rollback_request is None

    def test_nudge_after_completion_guard_error_asks_required_tool_first(self, tmp_path):
        nudge = StepExecutor._build_complete_step_nudge(
            "这个输入需要先向用户澄清。请先调用 ask_user_question，收到工具结果后再调用 complete_step。",
            {"conclusion": {"is_infra_intent": True, "confidence": "medium"}},
        )

        assert "先调用 ask_user_question" in nudge
        assert "收到 ask_user_question 的工具结果后" in nudge
        assert "不要再次直接调用 complete_step" in nudge

    @pytest.mark.asyncio
    async def test_no_nudge_when_initial_attempt_completes(self, tmp_path, caplog):
        events = [
            ToolUseStartEvent(tool_use_id="tu_1", name="complete_step"),
            ToolUseEndEvent(tool_use_id="tu_1", name="complete_step", input={"conclusion": {"ok": True}}),
            ToolResultEvent(tool_use_id="tu_1", tool_name="complete_step", result="ok"),
        ]
        fake_loop = _make_fake_agent_loop_class(events)
        executor = _make_executor(tmp_path)
        executor._observability.step_nudged = MagicMock()
        step = _make_step()
        ctx = PipelineContext(SIMPLE_DEPS)
        caplog.set_level(logging.INFO, logger="iac_code.pipeline.engine.step_executor")

        with patch("iac_code.agent.agent_loop.AgentLoop", fake_loop):
            async for _ in executor.execute(step, ctx, "test_session"):
                pass

        executor._observability.step_nudged.assert_not_called()
        assert not [record for record in caplog.records if record.message.startswith("Pipeline step nudge issued:")]

    @pytest.mark.asyncio
    async def test_exhausted_retries_emit_two_nudges(self, tmp_path, caplog):
        call_count = [0]

        class FakeAgentLoopNoComplete:
            def __init__(self, **kwargs):
                self.system_prompt = kwargs.get("system_prompt", "")
                self.tool_registry = kwargs.get("tool_registry")

            async def run_streaming(self, user_input):
                call_count[0] += 1
                yield TextDeltaEvent(text=f"attempt {call_count[0]}")

        executor = _make_executor(tmp_path)
        executor._observability.step_nudged = MagicMock()
        step = _make_step()
        ctx = PipelineContext(SIMPLE_DEPS)
        caplog.set_level(logging.INFO, logger="iac_code.pipeline.engine.step_executor")

        collected = []
        with patch("iac_code.agent.agent_loop.AgentLoop", FakeAgentLoopNoComplete):
            async for event in executor.execute(step, ctx, "test_session"):
                collected.append(event)

        results = [e for e in collected if isinstance(e, StepResult)]
        assert len(results) == 1
        assert results[0].status == StepStatus.FAILED
        assert results[0].error == "No conclusion extracted"
        assert call_count[0] == 3
        assert executor._observability.step_nudged.call_args_list == [
            call(step_id="intent_parsing", nudge_count=1, max_nudges=2, session_id="test_session"),
            call(step_id="intent_parsing", nudge_count=2, max_nudges=2, session_id="test_session"),
        ]

        nudge_logs = [record.message for record in caplog.records if record.message.startswith("Pipeline step nudge")]
        assert nudge_logs == [
            "Pipeline step nudge issued: step_id=intent_parsing nudge_count=1 max_nudges=2 session_id=test_session",
            "Pipeline step nudge issued: step_id=intent_parsing nudge_count=2 max_nudges=2 session_id=test_session",
        ]

    @pytest.mark.asyncio
    async def test_sets_conclusion_on_context(self, tmp_path):
        events = [
            ToolUseStartEvent(tool_use_id="tu_1", name="complete_step"),
            ToolUseEndEvent(
                tool_use_id="tu_1",
                name="complete_step",
                input={"conclusion": {"type": "e-commerce"}},
            ),
            ToolResultEvent(tool_use_id="tu_1", tool_name="complete_step", result="ok"),
        ]
        fake_loop = _make_fake_agent_loop_class(events)
        executor = _make_executor(tmp_path)
        step = _make_step()
        ctx = PipelineContext(SIMPLE_DEPS)

        with patch("iac_code.agent.agent_loop.AgentLoop", fake_loop):
            async for _ in executor.execute(step, ctx, "test_session"):
                pass

        assert ctx.get_conclusion("intent") == {"type": "e-commerce"}

    @pytest.mark.asyncio
    async def test_user_message_overrides_initial_prompt(self, tmp_path):
        captured_input = {}
        events = [
            ToolUseStartEvent(tool_use_id="tu_1", name="complete_step"),
            ToolUseEndEvent(tool_use_id="tu_1", name="complete_step", input={"conclusion": {"ok": True}}),
            ToolResultEvent(tool_use_id="tu_1", tool_name="complete_step", result="ok"),
        ]

        class CapturingAgentLoop:
            def __init__(self, **kwargs):
                self.system_prompt = kwargs.get("system_prompt", "")
                self.tool_registry = kwargs.get("tool_registry")

            async def run_streaming(self, user_input):
                captured_input["value"] = user_input
                for event in events:
                    yield event

        executor = _make_executor(tmp_path)
        step = _make_step()
        ctx = PipelineContext(SIMPLE_DEPS)

        with patch("iac_code.agent.agent_loop.AgentLoop", CapturingAgentLoop):
            async for _ in executor.execute(step, ctx, "test_session", user_message="帮我搭建电商网站"):
                pass

        assert captured_input["value"] == "帮我搭建电商网站"

    @pytest.mark.asyncio
    async def test_default_prompt_when_no_user_message(self, tmp_path):
        captured_input = {}
        events = [
            ToolUseStartEvent(tool_use_id="tu_1", name="complete_step"),
            ToolUseEndEvent(tool_use_id="tu_1", name="complete_step", input={"conclusion": {"ok": True}}),
            ToolResultEvent(tool_use_id="tu_1", tool_name="complete_step", result="ok"),
        ]

        class CapturingAgentLoop:
            def __init__(self, **kwargs):
                self.system_prompt = kwargs.get("system_prompt", "")
                self.tool_registry = kwargs.get("tool_registry")

            async def run_streaming(self, user_input):
                captured_input["value"] = user_input
                for event in events:
                    yield event

        executor = _make_executor(tmp_path)
        step = _make_step()
        ctx = PipelineContext(SIMPLE_DEPS)

        with patch("iac_code.agent.agent_loop.AgentLoop", CapturingAgentLoop):
            async for _ in executor.execute(step, ctx, "test_session"):
                pass

        assert captured_input["value"] == "请完成当前步骤：intent_parsing。"


class TestStepExecutorSkillResolution:
    def test_skill_content_as_primary_prompt(self, tmp_path):
        """When step has skill field, skill content is primary and prompt.md is supplement."""
        (tmp_path / "prompts").mkdir(exist_ok=True)
        (tmp_path / "prompts" / "intent_parsing.md").write_text(
            "## Pipeline Context\nAnalyze: {intent}", encoding="utf-8"
        )

        step = StepSpec(
            step_id="intent_parsing",
            conclusion_field="intent",
            forward=None,
            prompt_file="prompts/intent_parsing.md",
            skill="iac-aliyun-intent",
        )
        pipeline = LoadedPipeline(
            name="test",
            steps=[step],
            context_dependencies={"intent": []},
            max_rollbacks=3,
            skills={"iac-aliyun-intent": "# Intent Skill\nAnalyze user intent."},
        )
        executor = StepExecutor(
            provider_manager=MagicMock(),
            base_tool_registry=ToolRegistry(),
            pipeline=pipeline,
            pipeline_dir=tmp_path,
        )
        ctx = PipelineContext({"intent": []})
        prompt = executor._build_full_system_prompt(step, ctx)

        # Skill content present
        assert "# Intent Skill" in prompt
        # Separator
        assert "\n\n---\n\n" in prompt
        # Step prompt is appended as supplement
        assert "## Pipeline Context" in prompt
        # Ordering: skill before step prompt
        skill_pos = prompt.find("# Intent Skill")
        step_pos = prompt.find("## Pipeline Context")
        assert skill_pos < step_pos

    def test_no_skill_uses_prompt_only(self, tmp_path):
        """When step has no skill field, prompt.md content is present (base sections may precede)."""
        (tmp_path / "prompts").mkdir(exist_ok=True)
        (tmp_path / "prompts" / "test.md").write_text("# Step Prompt Only", encoding="utf-8")

        step = StepSpec(
            step_id="test",
            conclusion_field="result",
            forward=None,
            prompt_file="prompts/test.md",
        )
        pipeline = LoadedPipeline(
            name="test",
            steps=[step],
            context_dependencies={"result": []},
            max_rollbacks=3,
            skills={},
        )
        executor = StepExecutor(
            provider_manager=MagicMock(),
            base_tool_registry=ToolRegistry(),
            pipeline=pipeline,
            pipeline_dir=tmp_path,
        )
        ctx = PipelineContext({"result": []})
        prompt = executor._build_full_system_prompt(step, ctx)
        assert "# Step Prompt Only" in prompt
        assert prompt.endswith("# Step Prompt Only")

    def test_empty_prompt_file_with_skill(self, tmp_path):
        """When prompt_file is empty string but skill exists, just use skill content."""
        step = StepSpec(
            step_id="test",
            conclusion_field="result",
            forward=None,
            prompt_file="",
            skill="my-skill",
        )
        pipeline = LoadedPipeline(
            name="test",
            steps=[step],
            context_dependencies={"result": []},
            max_rollbacks=3,
            skills={"my-skill": "# My Skill Content"},
        )
        executor = StepExecutor(
            provider_manager=MagicMock(),
            base_tool_registry=ToolRegistry(),
            pipeline=pipeline,
            pipeline_dir=tmp_path,
        )
        ctx = PipelineContext({"result": []})
        prompt = executor._build_full_system_prompt(step, ctx)
        assert "# My Skill Content" in prompt


class TestStepExecutorBaseSections:
    def test_system_prompt_includes_base_sections(self, tmp_path):
        """Base sections appear before skill content in system prompt."""
        (tmp_path / "prompts").mkdir(exist_ok=True)
        (tmp_path / "prompts" / "test.md").write_text("# Step Task", encoding="utf-8")

        sections_config = IncludeExcludeConfig(include=["identity", "tools"])
        step = StepSpec(
            step_id="test",
            conclusion_field="result",
            forward=None,
            prompt_file="prompts/test.md",
            skill="my-skill",
        )
        pipeline = LoadedPipeline(
            name="test",
            steps=[step],
            context_dependencies={"result": []},
            max_rollbacks=3,
            skills={"my-skill": "# Skill Content"},
            base_prompt_sections=sections_config,
        )
        executor = StepExecutor(
            provider_manager=MagicMock(),
            base_tool_registry=ToolRegistry(),
            pipeline=pipeline,
            pipeline_dir=tmp_path,
        )
        ctx = PipelineContext({"result": []})
        prompt = executor._build_full_system_prompt(step, ctx)

        assert "Infrastructure as Code" in prompt
        assert "Using Tools" in prompt
        assert "# Skill Content" in prompt
        assert "# Step Task" in prompt
        # Order: base sections -> skill -> step prompt
        identity_pos = prompt.find("Infrastructure as Code")
        skill_pos = prompt.find("# Skill Content")
        step_pos = prompt.find("# Step Task")
        assert identity_pos < skill_pos < step_pos

    def test_step_level_sections_override_pipeline(self, tmp_path):
        """Step-level base_prompt_sections overrides pipeline-level."""
        (tmp_path / "prompts").mkdir(exist_ok=True)
        (tmp_path / "prompts" / "test.md").write_text("Task", encoding="utf-8")

        step = StepSpec(
            step_id="test",
            conclusion_field="result",
            forward=None,
            prompt_file="prompts/test.md",
            base_prompt_sections=IncludeExcludeConfig(include=["identity"]),
        )
        pipeline = LoadedPipeline(
            name="test",
            steps=[step],
            context_dependencies={"result": []},
            max_rollbacks=3,
            skills={},
            base_prompt_sections=IncludeExcludeConfig(include=["identity", "tools"]),
        )
        executor = StepExecutor(
            provider_manager=MagicMock(),
            base_tool_registry=ToolRegistry(),
            pipeline=pipeline,
            pipeline_dir=tmp_path,
        )
        ctx = PipelineContext({"result": []})
        prompt = executor._build_full_system_prompt(step, ctx)

        assert "Infrastructure as Code" in prompt
        assert "Using Tools" not in prompt


class TestStepExecutorToolsIncludeExclude:
    def test_exclude_removes_tools(self, tmp_path):
        (tmp_path / "prompts").mkdir(exist_ok=True)
        (tmp_path / "prompts" / "test.md").write_text("Task", encoding="utf-8")

        registry = ToolRegistry()
        registry.register_default_tools()

        step = StepSpec(
            step_id="test",
            conclusion_field="result",
            forward=None,
            prompt_file="prompts/test.md",
            tools=IncludeExcludeConfig(exclude=["bash"]),
        )
        pipeline = LoadedPipeline(
            name="test",
            steps=[step],
            context_dependencies={"result": []},
            max_rollbacks=3,
            skills={},
        )
        executor = StepExecutor(
            provider_manager=MagicMock(),
            base_tool_registry=registry,
            pipeline=pipeline,
            pipeline_dir=tmp_path,
        )
        ctx = PipelineContext({"result": []})
        tool_reg = executor._build_step_tools(step, ctx)

        assert tool_reg.get("bash") is None
        assert tool_reg.get("read_file") is not None
        assert tool_reg.get("complete_step") is not None

    def test_include_limits_tools(self, tmp_path):
        (tmp_path / "prompts").mkdir(exist_ok=True)
        (tmp_path / "prompts" / "test.md").write_text("Task", encoding="utf-8")

        registry = ToolRegistry()
        registry.register_default_tools()

        step = StepSpec(
            step_id="test",
            conclusion_field="result",
            forward=None,
            prompt_file="prompts/test.md",
            tools=IncludeExcludeConfig(include=["read_file", "grep"]),
        )
        pipeline = LoadedPipeline(
            name="test",
            steps=[step],
            context_dependencies={"result": []},
            max_rollbacks=3,
            skills={},
        )
        executor = StepExecutor(
            provider_manager=MagicMock(),
            base_tool_registry=registry,
            pipeline=pipeline,
            pipeline_dir=tmp_path,
        )
        ctx = PipelineContext({"result": []})
        tool_reg = executor._build_step_tools(step, ctx)

        assert tool_reg.get("read_file") is not None
        assert tool_reg.get("grep") is not None
        assert tool_reg.get("bash") is None
        assert tool_reg.get("complete_step") is not None

    def test_include_and_exclude_together(self, tmp_path):
        (tmp_path / "prompts").mkdir(exist_ok=True)
        (tmp_path / "prompts" / "test.md").write_text("Task", encoding="utf-8")

        registry = ToolRegistry()
        registry.register_default_tools()

        step = StepSpec(
            step_id="test",
            conclusion_field="result",
            forward=None,
            prompt_file="prompts/test.md",
            tools=IncludeExcludeConfig(include=["read_file", "bash", "grep"], exclude=["bash"]),
        )
        pipeline = LoadedPipeline(
            name="test",
            steps=[step],
            context_dependencies={"result": []},
            max_rollbacks=3,
            skills={},
        )
        executor = StepExecutor(
            provider_manager=MagicMock(),
            base_tool_registry=registry,
            pipeline=pipeline,
            pipeline_dir=tmp_path,
        )
        ctx = PipelineContext({"result": []})
        tool_reg = executor._build_step_tools(step, ctx)

        assert tool_reg.get("read_file") is not None
        assert tool_reg.get("grep") is not None
        assert tool_reg.get("bash") is None
        assert tool_reg.get("complete_step") is not None


class TestInjectTools:
    def test_inject_tools_registers_show_diagram(self, tmp_path):
        (tmp_path / "prompts").mkdir(exist_ok=True)
        (tmp_path / "prompts" / "confirm.md").write_text("Confirm step.", encoding="utf-8")

        step = StepSpec(
            step_id="confirm_and_select",
            conclusion_field="selected_plan",
            forward=None,
            prompt_file="prompts/confirm.md",
            inject_tools=["show_architecture_diagram"],
        )
        pipeline = LoadedPipeline(
            name="test",
            steps=[step],
            context_dependencies={"selected_plan": []},
            max_rollbacks=3,
            skills={},
        )
        executor = StepExecutor(
            provider_manager=MagicMock(),
            base_tool_registry=ToolRegistry(),
            pipeline=pipeline,
            pipeline_dir=tmp_path,
        )

        context = PipelineContext({"selected_plan": []})
        registry = executor._build_step_tools(step, context)

        assert registry.get("show_architecture_diagram") is not None
        assert registry.get("complete_step") is not None

    def test_inject_tools_registers_ask_user_question(self, tmp_path):
        (tmp_path / "prompts").mkdir(exist_ok=True)
        (tmp_path / "prompts" / "parse.md").write_text("Parse.", encoding="utf-8")

        step = StepSpec(
            step_id="intent_parsing",
            conclusion_field="intent",
            forward=None,
            prompt_file="prompts/parse.md",
            inject_tools=["ask_user_question"],
        )
        pipeline = LoadedPipeline(
            name="test",
            steps=[step],
            context_dependencies={"intent": []},
            max_rollbacks=3,
            skills={},
        )
        executor = StepExecutor(
            provider_manager=MagicMock(),
            base_tool_registry=ToolRegistry(),
            pipeline=pipeline,
            pipeline_dir=tmp_path,
        )

        registry = executor._build_step_tools(step, PipelineContext({"intent": []}))

        assert registry.get("ask_user_question") is not None
        assert registry.get("complete_step") is not None

    def test_no_inject_tools_only_has_complete_step(self, tmp_path):
        (tmp_path / "prompts").mkdir(exist_ok=True)
        (tmp_path / "prompts" / "parse.md").write_text("Parse.", encoding="utf-8")

        step = StepSpec(
            step_id="intent_parsing",
            conclusion_field="intent",
            forward=None,
            prompt_file="prompts/parse.md",
        )
        pipeline = LoadedPipeline(
            name="test",
            steps=[step],
            context_dependencies={"intent": []},
            max_rollbacks=3,
            skills={},
        )
        executor = StepExecutor(
            provider_manager=MagicMock(),
            base_tool_registry=ToolRegistry(),
            pipeline=pipeline,
            pipeline_dir=tmp_path,
        )

        context = PipelineContext({"intent": []})
        registry = executor._build_step_tools(step, context)

        assert registry.get("show_architecture_diagram") is None
        assert registry.get("ask_user_question") is None
        assert registry.get("complete_step") is not None

    @pytest.mark.asyncio
    async def test_ask_user_question_continues_same_agent_loop_to_complete_step(self, tmp_path):
        (tmp_path / "prompts").mkdir(exist_ok=True)
        (tmp_path / "prompts" / "parse.md").write_text("Parse.", encoding="utf-8")

        step = StepSpec(
            step_id="intent_parsing",
            conclusion_field="intent",
            forward=None,
            prompt_file="prompts/parse.md",
            inject_tools=["ask_user_question"],
            max_agent_turns=5,
        )
        pipeline = LoadedPipeline(
            name="test",
            steps=[step],
            context_dependencies={"intent": []},
            max_rollbacks=3,
            skills={},
        )

        class Provider:
            def __init__(self) -> None:
                self.calls = 0
                self.tool_result_seen = False

            def get_model_name(self) -> str:
                return "test-model"

            async def stream(self, messages, system, tools=None):
                self.calls += 1
                tool_names = {tool.name for tool in tools or []}
                assert {"ask_user_question", "complete_step"}.issubset(tool_names)

                if self.calls == 1:
                    yield MessageStartEvent(message_id="m1")
                    yield ToolUseStartEvent(tool_use_id="ask_1", name="ask_user_question")
                    yield ToolUseEndEvent(
                        tool_use_id="ask_1",
                        name="ask_user_question",
                        input={
                            "question": "请选择下一步",
                            "options": [
                                {"id": "deploy_to_aliyun", "label": "部署到阿里云"},
                                {"id": "not_iac", "label": "不是基础设施需求"},
                            ],
                            "allow_free_text": False,
                        },
                    )
                    yield MessageEndEvent(stop_reason="tool_use", usage=Usage())
                    return

                if self.calls == 2:
                    self.tool_result_seen = any(
                        getattr(block, "type", None) == "tool_result"
                        and getattr(block, "tool_use_id", None) == "ask_1"
                        and "deploy_to_aliyun" in getattr(block, "content", "")
                        for message in messages
                        for block in (message.content if isinstance(message.content, list) else [])
                    )
                    yield MessageStartEvent(message_id="m2")
                    yield ToolUseStartEvent(tool_use_id="done_1", name="complete_step")
                    yield ToolUseEndEvent(
                        tool_use_id="done_1",
                        name="complete_step",
                        input={
                            "conclusion": {
                                "is_infra_intent": True,
                                "confidence": "medium",
                                "selected_id": "deploy_to_aliyun",
                            }
                        },
                    )
                    yield MessageEndEvent(stop_reason="tool_use", usage=Usage())
                    return

                yield MessageStartEvent(message_id="m3")
                yield MessageEndEvent(stop_reason="end_turn", usage=Usage())

        provider = Provider()
        executor = StepExecutor(
            provider_manager=provider,
            base_tool_registry=ToolRegistry(),
            pipeline=pipeline,
            pipeline_dir=tmp_path,
        )
        context = PipelineContext({"intent": []})

        collected = []
        async for event in executor.execute(step, context, "test_session", user_message="我有个项目想上线"):
            if isinstance(event, AskUserQuestionEvent):
                assert event.response_future is not None
                event.response_future.set_result(
                    {"selected_id": "deploy_to_aliyun", "selected_label": "部署到阿里云", "free_text": ""}
                )
            collected.append(event)

        results = [event for event in collected if isinstance(event, StepResult)]
        assert len(results) == 1
        assert results[0].status == StepStatus.COMPLETED
        assert results[0].conclusion["selected_id"] == "deploy_to_aliyun"
        assert context.get_conclusion("intent") == results[0].conclusion
        assert provider.calls == 2
        assert provider.tool_result_seen is True

    @pytest.mark.asyncio
    async def test_successful_complete_step_stops_agent_loop_before_followup_tools(self, tmp_path):
        """A successful complete_step is terminal for the current pipeline step."""
        (tmp_path / "prompts").mkdir(exist_ok=True)
        (tmp_path / "prompts" / "parse.md").write_text("Parse.", encoding="utf-8")

        step = StepSpec(
            step_id="intent_parsing",
            conclusion_field="intent",
            forward=None,
            prompt_file="prompts/parse.md",
            inject_tools=["ask_user_question"],
            max_agent_turns=5,
        )
        pipeline = LoadedPipeline(
            name="test",
            steps=[step],
            context_dependencies={"intent": []},
            max_rollbacks=3,
            skills={},
        )

        class Provider:
            def __init__(self) -> None:
                self.calls = 0

            def get_model_name(self) -> str:
                return "test-model"

            async def stream(self, messages, system, tools=None):
                self.calls += 1
                if self.calls == 1:
                    yield MessageStartEvent(message_id="m1")
                    yield ToolUseStartEvent(tool_use_id="ask_1", name="ask_user_question")
                    yield ToolUseEndEvent(
                        tool_use_id="ask_1",
                        name="ask_user_question",
                        input={
                            "question": "请补充这个项目是什么，以及希望怎么上线。",
                            "options": [
                                {"id": "provide_details", "label": "补充项目信息"},
                                {"id": "not_deployment", "label": "暂不处理部署"},
                            ],
                            "allow_free_text": True,
                        },
                    )
                    yield MessageEndEvent(stop_reason="tool_use", usage=Usage())
                    return

                if self.calls == 2:
                    yield MessageStartEvent(message_id="m2")
                    yield ToolUseStartEvent(tool_use_id="done_1", name="complete_step")
                    yield ToolUseEndEvent(
                        tool_use_id="done_1",
                        name="complete_step",
                        input={
                            "conclusion": {
                                "is_infra_intent": False,
                                "confidence": "low",
                                "category": "other",
                                "rejection_reason": "用户选择暂不处理部署",
                                "user_message_summary": "用户有项目想上线但选择暂不处理部署",
                                "clarification_choice": "not_deployment",
                            }
                        },
                    )
                    yield MessageEndEvent(stop_reason="tool_use", usage=Usage())
                    return

                yield MessageStartEvent(message_id="m3")
                yield ToolUseStartEvent(tool_use_id="ask_after_done", name="ask_user_question")
                yield ToolUseEndEvent(
                    tool_use_id="ask_after_done",
                    name="ask_user_question",
                    input={
                        "question": "这个网站希望按哪种目标来规划？",
                        "options": [{"id": "economy", "label": "经济型上线"}],
                    },
                )
                yield MessageEndEvent(stop_reason="tool_use", usage=Usage())

        provider = Provider()
        executor = StepExecutor(
            provider_manager=provider,
            base_tool_registry=ToolRegistry(),
            pipeline=pipeline,
            pipeline_dir=tmp_path,
        )

        collected = []
        async for event in executor.execute(step, PipelineContext({"intent": []}), "test", "我有个项目想上线"):
            if isinstance(event, AskUserQuestionEvent) and event.response_future is not None:
                event.response_future.set_result(
                    {"selected_id": "not_deployment", "selected_label": "暂不处理部署", "free_text": ""}
                )
            collected.append(event)

        questions = [event for event in collected if isinstance(event, AskUserQuestionEvent)]
        assert [question.tool_use_id for question in questions] == ["ask_1"]
        assert provider.calls == 2
        step_results = [event for event in collected if isinstance(event, StepResult)]
        assert len(step_results) == 1
        assert step_results[0].status == StepStatus.COMPLETED
        assert step_results[0].conclusion["clarification_choice"] == "not_deployment"

    @pytest.mark.asyncio
    async def test_completion_guard_rejects_direct_complete_before_required_question(self, tmp_path):
        (tmp_path / "prompts").mkdir(exist_ok=True)
        (tmp_path / "prompts" / "parse.md").write_text("Parse.", encoding="utf-8")

        step = StepSpec(
            step_id="intent_parsing",
            conclusion_field="intent",
            forward=None,
            prompt_file="prompts/parse.md",
            inject_tools=["ask_user_question"],
            completion_guards=[
                {
                    "require_tool": "ask_user_question",
                    "when_user_message_matches_any": ["项目.*上线"],
                    "required_conclusion_field": "clarification_choice",
                    "copy_tool_result_to_conclusion": {
                        "selected_id": "clarification_choice",
                        "free_text": "clarification_text",
                    },
                    "message": "这个输入需要先向用户澄清。",
                }
            ],
            max_agent_turns=6,
        )
        pipeline = LoadedPipeline(
            name="test",
            steps=[step],
            context_dependencies={"intent": []},
            max_rollbacks=3,
            skills={},
        )

        class Provider:
            def __init__(self) -> None:
                self.calls = 0

            def get_model_name(self) -> str:
                return "test-model"

            async def stream(self, messages, system, tools=None):
                self.calls += 1
                if self.calls == 1:
                    yield MessageStartEvent(message_id="m1")
                    yield ToolUseStartEvent(tool_use_id="done_bad", name="complete_step")
                    yield ToolUseEndEvent(
                        tool_use_id="done_bad",
                        name="complete_step",
                        input={"conclusion": {"is_infra_intent": True, "confidence": "medium"}},
                    )
                    yield MessageEndEvent(stop_reason="tool_use", usage=Usage())
                    return

                if self.calls == 2:
                    assert any(
                        getattr(block, "type", None) == "tool_result"
                        and getattr(block, "tool_use_id", None) == "done_bad"
                        and getattr(block, "is_error", False)
                        and "ask_user_question" in getattr(block, "content", "")
                        for message in messages
                        for block in (message.content if isinstance(message.content, list) else [])
                    )
                    yield MessageStartEvent(message_id="m2")
                    yield ToolUseStartEvent(tool_use_id="ask_1", name="ask_user_question")
                    yield ToolUseEndEvent(
                        tool_use_id="ask_1",
                        name="ask_user_question",
                        input={
                            "question": "你是想把这个需求转成阿里云部署/IaC 方案吗？",
                            "options": [{"id": "deploy_to_aliyun", "label": "转成部署方案"}],
                            "allow_free_text": False,
                        },
                    )
                    yield MessageEndEvent(stop_reason="tool_use", usage=Usage())
                    return

                if self.calls == 3:
                    yield MessageStartEvent(message_id="m3")
                    yield ToolUseStartEvent(tool_use_id="done_good", name="complete_step")
                    yield ToolUseEndEvent(
                        tool_use_id="done_good",
                        name="complete_step",
                        input={
                            "conclusion": {
                                "is_infra_intent": True,
                                "confidence": "medium",
                                "selected_id": "deploy_to_aliyun",
                            }
                        },
                    )
                    yield MessageEndEvent(stop_reason="tool_use", usage=Usage())
                    return

                yield MessageStartEvent(message_id="m4")
                yield MessageEndEvent(stop_reason="end_turn", usage=Usage())

        provider = Provider()
        executor = StepExecutor(
            provider_manager=provider,
            base_tool_registry=ToolRegistry(),
            pipeline=pipeline,
            pipeline_dir=tmp_path,
        )

        collected = []
        async for event in executor.execute(step, PipelineContext({"intent": []}), "test", "我有个项目想上线"):
            if isinstance(event, AskUserQuestionEvent) and event.response_future is not None:
                event.response_future.set_result(
                    {"selected_id": "deploy_to_aliyun", "selected_label": "转成部署方案", "free_text": ""}
                )
            collected.append(event)

        complete_results = [
            event for event in collected if isinstance(event, ToolResultEvent) and event.tool_name == "complete_step"
        ]
        assert complete_results[0].is_error
        assert "这个输入需要先向用户澄清" in complete_results[0].result
        assert "ask_user_question" in complete_results[0].result
        assert complete_results[-1].is_error is False
        step_results = [event for event in collected if isinstance(event, StepResult)]
        assert step_results[-1].status == StepStatus.COMPLETED
        assert step_results[-1].conclusion["clarification_choice"] == "deploy_to_aliyun"
        assert "selected_id" not in step_results[-1].conclusion


class TestPipelineToolsDiscovery:
    def test_inject_tool_from_pipeline_tools_dir(self, tmp_path):
        (tmp_path / "prompts").mkdir(exist_ok=True)
        (tmp_path / "prompts" / "intent_parsing.md").write_text("Parse.", encoding="utf-8")

        tools_dir = tmp_path / "tools"
        tools_dir.mkdir()
        (tools_dir / "__init__.py").write_text("", encoding="utf-8")
        (tools_dir / "my_tool.py").write_text(
            """
from iac_code.tools.base import Tool, ToolContext, ToolResult
from typing import Any

class MyCustomTool(Tool):
    @property
    def name(self): return "my_custom_tool"
    @property
    def description(self): return "Custom tool"
    @property
    def input_schema(self): return {"type": "object", "properties": {}}
    async def execute(self, *, tool_input, context):
        return ToolResult.success("ok")
""",
            encoding="utf-8",
        )

        step = StepSpec(
            step_id="test_step",
            conclusion_field="intent",
            forward=None,
            prompt_file="prompts/intent_parsing.md",
            inject_tools=["my_custom_tool"],
        )
        pipeline = LoadedPipeline(
            name="test",
            steps=[step],
            context_dependencies=SIMPLE_DEPS,
            max_rollbacks=3,
            skills={},
        )

        from iac_code.pipeline.engine.loader import _discover_pipeline_tools

        pipeline.pipeline_tools = _discover_pipeline_tools(tmp_path)

        executor = StepExecutor(
            provider_manager=MagicMock(),
            base_tool_registry=ToolRegistry(),
            pipeline=pipeline,
            pipeline_dir=tmp_path,
        )
        ctx = PipelineContext(SIMPLE_DEPS)
        tool_reg = executor._build_step_tools(step, ctx)
        assert tool_reg.get("my_custom_tool") is not None

    def test_engine_tool_still_works(self, tmp_path):
        (tmp_path / "prompts").mkdir(exist_ok=True)
        (tmp_path / "prompts" / "intent_parsing.md").write_text("Parse.", encoding="utf-8")

        step = StepSpec(
            step_id="test_step",
            conclusion_field="intent",
            forward=None,
            prompt_file="prompts/intent_parsing.md",
            inject_tools=["show_architecture_diagram"],
        )
        pipeline = LoadedPipeline(
            name="test",
            steps=[step],
            context_dependencies=SIMPLE_DEPS,
            max_rollbacks=3,
            skills={},
        )
        executor = StepExecutor(
            provider_manager=MagicMock(),
            base_tool_registry=ToolRegistry(),
            pipeline=pipeline,
            pipeline_dir=tmp_path,
        )
        ctx = PipelineContext(SIMPLE_DEPS)
        tool_reg = executor._build_step_tools(step, ctx)
        assert tool_reg.get("show_architecture_diagram") is not None


class TestStepExecutorValidationRespect:
    """Tests that step_executor respects tool validation results (is_error on ToolResultEvent)."""

    @pytest.mark.asyncio
    async def test_rejects_conclusion_when_tool_returns_error(self, tmp_path):
        """When complete_step tool returns is_error=True, step_executor should NOT accept the conclusion."""
        events = [
            ToolUseStartEvent(tool_use_id="tu_1", name="complete_step"),
            ToolUseEndEvent(
                tool_use_id="tu_1",
                name="complete_step",
                input={"conclusion": {"bad": "data"}},
            ),
            ToolResultEvent(tool_use_id="tu_1", tool_name="complete_step", result="校验失败", is_error=True),
        ]
        fake_loop = _make_fake_agent_loop_class(events)
        executor = _make_executor(tmp_path)
        step = _make_step()
        ctx = PipelineContext(SIMPLE_DEPS)

        collected = []
        with patch("iac_code.agent.agent_loop.AgentLoop", fake_loop):
            async for event in executor.execute(step, ctx, "test_session"):
                collected.append(event)

        results = [e for e in collected if isinstance(e, StepResult)]
        assert len(results) == 1
        assert results[0].status == StepStatus.FAILED
        assert results[0].error == "No conclusion extracted"

    @pytest.mark.asyncio
    async def test_terminal_failed_step_result_stops_without_nudge(self, tmp_path):
        """A terminal failed step_result from complete_step should end the step with its specific error."""
        terminal_result = StepResult(
            step_id="intent_parsing",
            status=StepStatus.FAILED,
            error="Schema validation failed after 2 attempts: missing required field",
        )

        class FakeAgentLoop:
            def __init__(self, **kwargs):
                self.system_prompt = kwargs.get("system_prompt", "")
                self.tool_registry = kwargs.get("tool_registry")
                self.prompts: list[str] = []

            async def run_streaming(self, user_input):
                self.prompts.append(user_input)
                yield ToolUseStartEvent(tool_use_id="tu_1", name="complete_step")
                yield ToolUseEndEvent(
                    tool_use_id="tu_1",
                    name="complete_step",
                    input={"conclusion": {"bad": "data"}},
                )
                yield ToolResultEvent(
                    tool_use_id="tu_1",
                    tool_name="complete_step",
                    result="conclusion 校验失败（已超过最大重试次数 1）",
                    is_error=True,
                    metadata={"step_result": terminal_result},
                )

        executor = _make_executor(tmp_path)
        step = _make_step()
        ctx = PipelineContext(SIMPLE_DEPS)

        collected = []
        with patch("iac_code.agent.agent_loop.AgentLoop", FakeAgentLoop):
            async for event in executor.execute(step, ctx, "test_session"):
                collected.append(event)

        results = [e for e in collected if isinstance(e, StepResult)]
        assert results == [terminal_result]

    @pytest.mark.asyncio
    async def test_accepts_conclusion_only_after_successful_validation(self, tmp_path):
        """When first call fails validation and second succeeds, only second conclusion is accepted."""
        call_count = [0]  # noqa: F841
        first_events = [
            ToolUseStartEvent(tool_use_id="tu_1", name="complete_step"),
            ToolUseEndEvent(tool_use_id="tu_1", name="complete_step", input={"conclusion": {"bad": "data"}}),
            ToolResultEvent(tool_use_id="tu_1", tool_name="complete_step", result="校验失败", is_error=True),
            ToolUseStartEvent(tool_use_id="tu_2", name="complete_step"),
            ToolUseEndEvent(tool_use_id="tu_2", name="complete_step", input={"conclusion": {"good": "data"}}),
            ToolResultEvent(tool_use_id="tu_2", tool_name="complete_step", result="步骤完成", is_error=False),
        ]

        class FakeAgentLoop:
            def __init__(self, **kwargs):
                self.system_prompt = kwargs.get("system_prompt", "")
                self.tool_registry = kwargs.get("tool_registry")

            async def run_streaming(self, user_input):
                for event in first_events:
                    yield event

        executor = _make_executor(tmp_path)
        step = _make_step()
        ctx = PipelineContext(SIMPLE_DEPS)

        collected = []
        with patch("iac_code.agent.agent_loop.AgentLoop", FakeAgentLoop):
            async for event in executor.execute(step, ctx, "test_session"):
                collected.append(event)

        results = [e for e in collected if isinstance(e, StepResult)]
        assert len(results) == 1
        assert results[0].status == StepStatus.COMPLETED
        assert results[0].conclusion == {"good": "data"}


class TestStepExecutorSchemaWiring:
    def test_passes_conclusion_schema_to_tool(self, tmp_path):
        (tmp_path / "prompts").mkdir(exist_ok=True)
        (tmp_path / "prompts" / "intent_parsing.md").write_text("Parse.", encoding="utf-8")
        step = StepSpec(
            step_id="intent_parsing",
            conclusion_field="intent",
            forward="arch",
            prompt_file="prompts/intent_parsing.md",
            conclusion_schema={"type": "object", "required": ["x"], "properties": {"x": {"type": "string"}}},
        )
        executor = StepExecutor(
            provider_manager=MagicMock(),
            base_tool_registry=ToolRegistry(),
            pipeline=_make_pipeline(),
            pipeline_dir=tmp_path,
        )
        ctx = PipelineContext(SIMPLE_DEPS)
        tool_reg = executor._build_step_tools(step, ctx)
        complete_tool = tool_reg.get("complete_step")
        assert complete_tool.input_schema["properties"]["conclusion"]["required"] == ["x"]

    def test_passes_rollback_targets_to_tool(self, tmp_path):
        (tmp_path / "prompts").mkdir(exist_ok=True)
        (tmp_path / "prompts" / "intent_parsing.md").write_text("Parse.", encoding="utf-8")
        step = StepSpec(
            step_id="intent_parsing",
            conclusion_field="intent",
            forward="arch",
            prompt_file="prompts/intent_parsing.md",
        )
        executor = StepExecutor(
            provider_manager=MagicMock(),
            base_tool_registry=ToolRegistry(),
            pipeline=_make_pipeline(),
            pipeline_dir=tmp_path,
        )
        ctx = PipelineContext(SIMPLE_DEPS)
        tool_reg = executor._build_step_tools(step, ctx, rollback_targets=["prev_step"])
        complete_tool = tool_reg.get("complete_step")
        schema = complete_tool.input_schema
        assert schema["properties"]["rollback_request"]["properties"]["target_step"]["enum"] == ["prev_step"]

    def test_does_not_fallback_to_static_rollback_rules(self, tmp_path):
        (tmp_path / "prompts").mkdir(exist_ok=True)
        (tmp_path / "prompts" / "intent_parsing.md").write_text("Parse.", encoding="utf-8")
        step = StepSpec(
            step_id="intent_parsing",
            conclusion_field="intent",
            forward="arch",
            prompt_file="prompts/intent_parsing.md",
        )
        setattr(step, "rollback_rules", [SimpleNamespace(target_step="legacy_prev", condition="bad")])
        executor = StepExecutor(
            provider_manager=MagicMock(),
            base_tool_registry=ToolRegistry(),
            pipeline=_make_pipeline(),
            pipeline_dir=tmp_path,
        )
        ctx = PipelineContext(SIMPLE_DEPS)
        tool_reg = executor._build_step_tools(step, ctx)
        complete_tool = tool_reg.get("complete_step")

        assert "rollback_request" not in complete_tool.input_schema["properties"]


class TestSchemaIntegration:
    """Integration test: schema flows from StepSpec through StepExecutor to CompleteStepTool."""

    def test_conclusion_schema_and_rollback_targets_propagate(self, tmp_path):
        """Verify that conclusion_schema from StepSpec reaches the tool's input_schema,
        and explicit rollback targets produce correct enum constraint on rollback_request.target_step."""
        (tmp_path / "prompts").mkdir(exist_ok=True)
        (tmp_path / "prompts" / "intent_parsing.md").write_text("Do it.", encoding="utf-8")
        step = StepSpec(
            step_id="intent_parsing",
            conclusion_field="intent",
            forward="arch",
            prompt_file="prompts/intent_parsing.md",
            conclusion_schema={
                "type": "object",
                "required": ["is_infra_intent", "confidence"],
                "properties": {
                    "is_infra_intent": {"type": "boolean"},
                    "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
                },
            },
            max_conclusion_retries=3,
        )
        executor = StepExecutor(
            provider_manager=MagicMock(),
            base_tool_registry=ToolRegistry(),
            pipeline=_make_pipeline(),
            pipeline_dir=tmp_path,
        )
        ctx = PipelineContext(SIMPLE_DEPS)
        tool_reg = executor._build_step_tools(step, ctx, rollback_targets=["prev_step", "other_step"])
        tool = tool_reg.get("complete_step")
        schema = tool.input_schema

        # Conclusion schema injected correctly
        conclusion = schema["properties"]["conclusion"]
        assert conclusion["type"] == "object"
        assert conclusion["required"] == ["is_infra_intent", "confidence"]
        assert "is_infra_intent" in conclusion["properties"]
        assert conclusion["properties"]["confidence"]["enum"] == ["high", "medium", "low"]

        # Rollback targets injected as enum
        rollback = schema["properties"]["rollback_request"]
        assert rollback["properties"]["target_step"]["enum"] == ["prev_step", "other_step"]
        assert rollback["required"] == ["target_step", "reason"]

    def test_no_schema_falls_back_to_generic(self, tmp_path):
        """When no conclusion_schema, tool uses generic schema."""
        (tmp_path / "prompts").mkdir(exist_ok=True)
        (tmp_path / "prompts" / "intent_parsing.md").write_text("Do it.", encoding="utf-8")
        step = StepSpec(
            step_id="intent_parsing",
            conclusion_field="intent",
            forward="arch",
            prompt_file="prompts/intent_parsing.md",
        )
        executor = StepExecutor(
            provider_manager=MagicMock(),
            base_tool_registry=ToolRegistry(),
            pipeline=_make_pipeline(),
            pipeline_dir=tmp_path,
        )
        ctx = PipelineContext(SIMPLE_DEPS)
        tool_reg = executor._build_step_tools(step, ctx)
        tool = tool_reg.get("complete_step")
        schema = tool.input_schema

        conclusion = schema["properties"]["conclusion"]
        assert conclusion["type"] == "object"
        assert "description" in conclusion
        assert "properties" not in conclusion
        # No rollback unless the runner passes dynamic rollback targets.
        assert "rollback_request" not in schema["properties"]


@pytest.mark.asyncio
async def test_resumed_step_uses_continue_streaming_without_duplicate_prompt(monkeypatch, tmp_path):
    calls: list[tuple[str, object]] = []
    agent_loop_init_kwargs: dict[str, object] = {}

    class FakeAgentLoop:
        def __init__(self, *args, **kwargs):
            agent_loop_init_kwargs.update(kwargs)
            self.resume_messages = kwargs.get("resume_messages")

        async def run_streaming(self, user_input):
            calls.append(("run", user_input))
            if False:
                yield None

        async def continue_streaming(self):
            calls.append(("continue", None))
            yield ToolUseStartEvent(tool_use_id="tu_1", name="complete_step")
            yield ToolUseEndEvent(
                tool_use_id="tu_1",
                name="complete_step",
                input={"conclusion": {"resumed": True}},
            )
            yield ToolResultEvent(tool_use_id="tu_1", tool_name="complete_step", result="ok")

    monkeypatch.setattr("iac_code.agent.agent_loop.AgentLoop", FakeAgentLoop)

    executor = _make_executor(tmp_path)
    step = _make_step()
    ctx = PipelineContext(SIMPLE_DEPS)
    resume_messages = [Message(role="user", content="请完成当前步骤：intent_parsing。")]
    events = []
    async for event in executor.execute(
        step,
        ctx,
        session_id="root",
        attempt_id="att_0001",
        transcript_id="transcript_att_0001",
        resume_messages=resume_messages,
    ):
        events.append(event)

    assert calls == [("continue", None)]
    assert agent_loop_init_kwargs["session_id"] == "transcript_att_0001"
    assert agent_loop_init_kwargs["resume_messages"] == resume_messages
    results = [event for event in events if isinstance(event, StepResult)]
    assert len(results) == 1
    assert results[0].status == StepStatus.COMPLETED
    assert results[0].conclusion == {"resumed": True}


@pytest.mark.asyncio
async def test_resumed_step_returns_reconstructed_complete_step(monkeypatch, tmp_path):
    class FailIfAgentLoopIsCreated:
        def __init__(self, *args, **kwargs):
            raise AssertionError("AgentLoop should not be created for already-completed transcript")

    monkeypatch.setattr("iac_code.agent.agent_loop.AgentLoop", FailIfAgentLoopIsCreated)

    executor = _make_executor(tmp_path)
    step = _make_step()
    ctx = PipelineContext(SIMPLE_DEPS)
    resume_messages = [
        Message(role="user", content="start"),
        Message(
            role="assistant",
            content=[
                ToolUseBlock(
                    id="tu_complete",
                    name="complete_step",
                    input={"conclusion": {"result": "restored"}},
                )
            ],
        ),
        Message(role="user", content=[ToolResultBlock(tool_use_id="tu_complete", content="ok", is_error=False)]),
    ]

    results = []
    async for event in executor.execute(
        step,
        ctx,
        session_id="root",
        attempt_id="att_0001",
        transcript_id="transcript_att_0001",
        resume_messages=resume_messages,
    ):
        if isinstance(event, StepResult):
            results.append(event)

    assert len(results) == 1
    assert results[0].status == StepStatus.COMPLETED
    assert results[0].conclusion == {"result": "restored"}
    assert ctx.get_conclusion(step.conclusion_field) == {"result": "restored"}


@pytest.mark.asyncio
async def test_resumed_completed_step_sets_empty_conclusion_and_calls_on_exit(monkeypatch, tmp_path):
    class FailIfAgentLoopIsCreated:
        def __init__(self, *args, **kwargs):
            raise AssertionError("AgentLoop should not be created for already-completed transcript")

    monkeypatch.setattr("iac_code.agent.agent_loop.AgentLoop", FailIfAgentLoopIsCreated)

    executor = _make_executor(tmp_path)
    step = _make_step()
    step.on_exit = MagicMock()
    ctx = PipelineContext(SIMPLE_DEPS)
    resume_messages = [
        Message(
            role="assistant",
            content=[ToolUseBlock(id="tu_complete", name="complete_step", input={"conclusion": {}})],
        ),
        Message(role="user", content=[ToolResultBlock(tool_use_id="tu_complete", content="ok", is_error=False)]),
    ]

    results = []
    async for event in executor.execute(
        step,
        ctx,
        session_id="root",
        attempt_id="att_0001",
        transcript_id="transcript_att_0001",
        resume_messages=resume_messages,
    ):
        if isinstance(event, StepResult):
            results.append(event)

    assert len(results) == 1
    assert results[0].status == StepStatus.COMPLETED
    assert results[0].conclusion == {}
    assert ctx.get_conclusion(step.conclusion_field) == {}
    step.on_exit.assert_called_once_with(ctx, {})


@pytest.mark.asyncio
async def test_resumed_completed_step_normalizes_completion_guard_copy(monkeypatch, tmp_path):
    class FailIfAgentLoopIsCreated:
        def __init__(self, *args, **kwargs):
            raise AssertionError("AgentLoop should not be created for already-completed transcript")

    monkeypatch.setattr("iac_code.agent.agent_loop.AgentLoop", FailIfAgentLoopIsCreated)

    (tmp_path / "prompts").mkdir(exist_ok=True)
    (tmp_path / "prompts" / "parse.md").write_text("Parse.", encoding="utf-8")
    step = StepSpec(
        step_id="intent_parsing",
        conclusion_field="intent",
        forward=None,
        prompt_file="prompts/parse.md",
        inject_tools=["ask_user_question"],
        completion_guards=[
            {
                "require_tool": "ask_user_question",
                "required_conclusion_field": "clarification_choice",
                "copy_tool_result_to_conclusion": {
                    "selected_id": "clarification_choice",
                    "free_text": "clarification_text",
                },
            }
        ],
    )
    pipeline = LoadedPipeline(
        name="test",
        steps=[step],
        context_dependencies={"intent": []},
        max_rollbacks=3,
        skills={},
    )
    executor = StepExecutor(
        provider_manager=MagicMock(),
        base_tool_registry=ToolRegistry(),
        pipeline=pipeline,
        pipeline_dir=tmp_path,
    )
    ctx = PipelineContext({"intent": []})
    complete_input = {"conclusion": {"is_infra_intent": True, "confidence": "medium"}}
    resume_messages = [
        Message(
            role="assistant",
            content=[ToolUseBlock(id="tu_question", name="ask_user_question", input={"question": "q", "options": []})],
        ),
        Message(
            role="user",
            content=[
                ToolResultBlock(
                    tool_use_id="tu_question",
                    content='{"selected_id": "deploy", "selected_label": "Deploy", "free_text": "cn-hangzhou"}',
                    is_error=False,
                )
            ],
        ),
        Message(
            role="assistant",
            content=[ToolUseBlock(id="tu_complete", name="complete_step", input=complete_input)],
        ),
        Message(role="user", content=[ToolResultBlock(tool_use_id="tu_complete", content="ok", is_error=False)]),
    ]

    results = []
    async for event in executor.execute(
        step,
        ctx,
        session_id="root",
        attempt_id="att_0001",
        transcript_id="transcript_att_0001",
        resume_messages=resume_messages,
    ):
        if isinstance(event, StepResult):
            results.append(event)

    assert len(results) == 1
    assert results[0].status == StepStatus.COMPLETED
    assert results[0].conclusion == {
        "is_infra_intent": True,
        "confidence": "medium",
        "clarification_choice": "deploy",
        "clarification_text": "cn-hangzhou",
    }
    assert complete_input == {"conclusion": {"is_infra_intent": True, "confidence": "medium"}}
    assert ctx.get_conclusion("intent") == results[0].conclusion


@pytest.mark.asyncio
async def test_resumed_step_rebuilds_ask_user_question_guard_state(monkeypatch, tmp_path):
    captured_guard_state = {}

    executor = _make_executor(tmp_path)
    step = _make_step()
    ctx = PipelineContext(SIMPLE_DEPS)
    original_build_tools = executor._build_step_tools

    def spy_build_step_tools(step, context_arg, user_message="", completion_guard_state=None):
        captured_guard_state.update(completion_guard_state or {})
        return original_build_tools(step, context_arg, user_message, completion_guard_state)

    class FakeAgentLoop:
        def __init__(self, *args, **kwargs):
            pass

        async def continue_streaming(self):
            if False:
                yield None

        async def run_streaming(self, user_input):
            if False:
                yield None

    monkeypatch.setattr(executor, "_build_step_tools", spy_build_step_tools)
    monkeypatch.setattr("iac_code.agent.agent_loop.AgentLoop", FakeAgentLoop)

    resume_messages = [
        Message(
            role="assistant",
            content=[ToolUseBlock(id="tu_question", name="ask_user_question", input={"question": "q", "options": []})],
        ),
        Message(
            role="user",
            content=[
                ToolResultBlock(
                    tool_use_id="tu_question",
                    content='{"selected_id": "deploy", "selected_label": "Deploy", "free_text": "budget 500"}',
                    is_error=False,
                )
            ],
        ),
    ]

    async for _event in executor.execute(
        step,
        ctx,
        session_id="root",
        attempt_id="att_0001",
        transcript_id="transcript_att_0001",
        resume_messages=resume_messages,
    ):
        pass

    assert captured_guard_state["successful_tools"] == {"ask_user_question"}
    assert captured_guard_state["tool_results"]["ask_user_question"]["free_text"] == "budget 500"
