"""An abandoned question stream must not retain execution ownership."""

import asyncio

import pytest

from iac_code.a2a.execution_control import (
    ExecutionControlService,
    bind_execution_control,
    reset_execution_control,
)
from iac_code.agent.agent_loop import AgentLoop
from iac_code.pipeline.engine.ask_user_question_tool import AskUserQuestionTool
from iac_code.pipeline.engine.context import PipelineContext
from iac_code.pipeline.engine.pipeline_runner import PipelineRunner
from iac_code.pipeline.engine.step_executor import StepExecutor
from iac_code.pipeline.engine.step_spec import LoadedPipeline, StepSpec
from iac_code.services.session_storage import SessionStorage
from iac_code.tools.base import ToolRegistry
from iac_code.types.permissions import PermissionResult
from iac_code.types.stream_events import (
    AskUserQuestionEvent,
    MessageEndEvent,
    MessageStartEvent,
    ToolUseEndEvent,
    ToolUseStartEvent,
    Usage,
)


class AllowedQuestionTool(AskUserQuestionTool):
    async def check_permissions(self, input, context=None):
        return PermissionResult(behavior="allow")


class QuestionProvider:
    def get_model_name(self):
        return "offline"

    async def stream(self, messages, system, tools=None):
        yield MessageStartEvent(message_id="question")
        yield ToolUseStartEvent(tool_use_id="question-1", name="ask_user_question")
        yield ToolUseEndEvent(
            tool_use_id="question-1",
            name="ask_user_question",
            input={
                "question": "Which region?",
                "options": [{"id": "fixture", "label": "Fixture region"}],
            },
        )
        yield MessageEndEvent(stop_reason="tool_use", usage=Usage())


@pytest.mark.asyncio
@pytest.mark.parametrize("layer", ["agent", "step", "pipeline"])
async def test_closed_question_stream_drains_tool_and_allows_natural_handoff(tmp_path, layer, monkeypatch):
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("IAC_CODE_CONFIG_BACKUP_DIR", str(tmp_path / "backup"))
    service = ExecutionControlService(persistence_root=tmp_path, backup_service=None)
    control = await service.begin_execution(context_id="ctx-1", task_id="task-1", owner="owner", cwd=str(tmp_path))
    token = bind_execution_control(control)
    registry = ToolRegistry()
    registry.register(AllowedQuestionTool())
    loop = AgentLoop(provider_manager=QuestionProvider(), system_prompt="offline", tool_registry=registry, max_turns=1)
    if layer == "agent":
        stream = loop.run_streaming("Only plan; do not deploy.")
    else:
        (tmp_path / "prompt.md").write_text("Only plan; do not deploy.", encoding="utf-8")
        step = StepSpec(
            step_id="requirements",
            conclusion_field="request",
            forward=None,
            prompt_file="prompt.md",
            inject_tools=["ask_user_question"],
        )
        pipeline = LoadedPipeline(
            name="offline", steps=[step], context_dependencies={"request": []}, max_rollbacks=3, skills={}
        )
        executor = StepExecutor(
            provider_manager=QuestionProvider(), base_tool_registry=registry, pipeline=pipeline, pipeline_dir=tmp_path
        )
        if layer == "step":
            stream = executor.execute(
                step, PipelineContext({"request": []}), "offline-session", user_message="Plan only."
            )
        else:
            (tmp_path / "pipeline.yaml").write_text(
                "name: offline\ncontext_dependencies: {request: []}\nmax_rollbacks: 3\nsteps:\n"
                "  - id: requirements\n    conclusion_field: request\n    forward: null\n"
                "    prompt: prompt.md\n    inject_tools: [ask_user_question]\n",
                encoding="utf-8",
            )
            runner = PipelineRunner(
                pipeline_dir=tmp_path,
                provider_manager=QuestionProvider(),
                base_tool_registry=registry,
                session_storage=SessionStorage(projects_dir=tmp_path / "projects"),
                session_id="offline-session",
                cwd=str(tmp_path),
                surface="a2a",
            )
            stream = runner.run("Only plan; do not deploy.")
    question = None
    current = asyncio.current_task()
    try:
        async for event in stream:
            if isinstance(event, AskUserQuestionEvent):
                question = event
                break
        assert question is not None and question.response_future is not None
        await stream.aclose()
        generation = await control.detach_task(current, execution_status="completed", natural_completion=True)
        assert question.response_future.done(), "closed stream left its old question tool waiting forever"
        state = await control.finalize_natural_completion(task_id="task-1", completion_generation=generation)
        assert state["phase"] == "terminated" and control.natural_handoff_receipt() is not None
        assert not control.has_managed_work()
    finally:
        if question is not None and question.response_future is not None and not question.response_future.done():
            question.response_future.set_result(None)
        await stream.aclose()
        await control.detach_task(current, execution_status="completed")
        reset_execution_control(token)
        await service.close()
