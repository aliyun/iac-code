"""Regression tests for problems 1, 2, 3 — StepExecutor integration with main."""

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from iac_code.agent.message import Message
from iac_code.pipeline.engine.context import PipelineContext
from iac_code.pipeline.engine.step_executor import StepAgentLoopContext, StepExecutor
from iac_code.pipeline.engine.step_spec import LoadedPipeline, StepSpec
from iac_code.tools.base import ToolRegistry
from iac_code.types.stream_events import ContextUsageEvent


def _make_step(skill: str | None = None) -> StepSpec:
    return StepSpec(
        step_id="s1",
        conclusion_field="x",
        forward=None,
        prompt_file="prompts/x.md",
        skill=skill,
    )


def _make_pipeline(step: StepSpec) -> LoadedPipeline:
    return LoadedPipeline(
        name="t",
        steps=[step],
        context_dependencies={"x": []},
        max_rollbacks=1,
        skills={skill: "skill content" for skill in [step.skill] if skill},
    )


def _make_executor(tmp_path: Path, **kwargs) -> StepExecutor:
    (tmp_path / "prompts").mkdir(exist_ok=True)
    (tmp_path / "prompts" / "x.md").write_text("Do x.", encoding="utf-8")
    step = kwargs.pop("step", _make_step())
    return StepExecutor(
        provider_manager=MagicMock(),
        base_tool_registry=ToolRegistry(),
        pipeline=_make_pipeline(step),
        pipeline_dir=tmp_path,
        **kwargs,
    )


def test_step_executor_threads_permission_context_getter(tmp_path):
    """问题 1：StepExecutor 应接受 permission_context_getter 并保留为字段。"""
    sentinel = object()
    executor = _make_executor(tmp_path, permission_context_getter=lambda: sentinel)
    assert executor._permission_context_getter() is sentinel


def test_step_executor_threads_memory_content_getter(tmp_path):
    """问题 3：StepExecutor 应接受 memory_content_getter 并在 system prompt 中注入。"""
    executor = _make_executor(
        tmp_path,
        memory_content_getter=lambda: "- [test-memory](test.md) — hello world",
    )
    step = _make_step()
    ctx = PipelineContext({"x": []})
    prompt = executor._build_full_system_prompt(step, ctx)
    assert "# Memory" in prompt
    assert "hello world" in prompt


def test_step_executor_memory_getter_called_at_step_time(tmp_path):
    """问题 3：getter 应在 _build_full_system_prompt 调时被调用，拿最新值。"""
    counter = {"n": 0}

    def getter():
        counter["n"] += 1
        return f"call #{counter['n']}"

    executor = _make_executor(tmp_path, memory_content_getter=getter)
    step = _make_step()
    ctx = PipelineContext({"x": []})
    executor._build_full_system_prompt(step, ctx)
    executor._build_full_system_prompt(step, ctx)
    assert counter["n"] == 2  # 调两次拿两个不同的值


def test_step_executor_no_auto_trigger_when_step_has_skill(tmp_path):
    """问题 2：step 自己声明 skill 时，不应再用 auto_trigger_skills。"""
    auto_skills = [MagicMock(name="auto_skill")]
    executor = _make_executor(
        tmp_path,
        step=_make_step(skill="step_skill"),
        auto_trigger_skills=auto_skills,
    )
    resolved = executor._resolve_auto_trigger_skills(_make_step(skill="step_skill"))
    assert resolved is None


def test_step_executor_uses_auto_trigger_when_step_has_no_skill(tmp_path):
    """问题 2：step 没有 skill 时，应该用 auto_trigger_skills。"""
    auto_skills = [MagicMock(name="auto_skill")]
    executor = _make_executor(
        tmp_path,
        step=_make_step(skill=None),
        auto_trigger_skills=auto_skills,
    )
    resolved = executor._resolve_auto_trigger_skills(_make_step(skill=None))
    assert resolved == auto_skills


def test_step_executor_defaults_keep_existing_signatures(tmp_path):
    """普通模式回归：不传新参数也应该能构造 StepExecutor。"""
    executor = _make_executor(tmp_path)
    assert executor._permission_context_getter is None
    assert executor._memory_content_getter is None
    assert executor._auto_trigger_skills == []


def test_step_agent_loop_does_not_receive_memory_recall_service(monkeypatch, tmp_path):
    captured_kwargs = {}

    class FakeAgentLoop:
        def __init__(self, **kwargs):
            captured_kwargs.update(kwargs)

    monkeypatch.setattr("iac_code.agent.agent_loop.AgentLoop", FakeAgentLoop)

    executor = _make_executor(
        tmp_path,
        memory_content_getter=lambda: "this should not imply side recall",
    )
    step = _make_step()
    ctx = PipelineContext({"x": []})

    agent_context = executor.build_agent_loop_context(step, ctx, "session-1")

    assert agent_context.agent_loop is not None
    assert "memory_recall_service" not in captured_kwargs


def test_step_agent_loop_receives_pipeline_scoped_env_overrides(monkeypatch, tmp_path):
    captured_kwargs = {}

    class FakeAgentLoop:
        def __init__(self, **kwargs):
            captured_kwargs.update(kwargs)

    monkeypatch.setattr("iac_code.agent.agent_loop.AgentLoop", FakeAgentLoop)

    executor = _make_executor(
        tmp_path,
        tool_context_env_overrides={"PATH": "/tmp/iac-code-infraguard/bin"},
    )
    agent_context = executor.build_agent_loop_context(_make_step(), PipelineContext({"x": []}), "session-1")

    assert agent_context.agent_loop is not None
    assert captured_kwargs["tool_context_env_overrides"] == {"PATH": "/tmp/iac-code-infraguard/bin"}


@pytest.mark.asyncio
async def test_execute_emits_initial_context_usage_before_stream(tmp_path):
    """问题 3：step/候选一启动就抢发一次 ContextUsageEvent，用量圆环立即带上正确名称，
    不必等首个 MessageEndEvent（往往 7-8s 后才到，期间前端只能回退到「普通会话」）。"""
    executor = _make_executor(tmp_path)
    sentinel_usage = {"total_tokens": 4242, "context_window": 60000}

    async def _empty_stream():
        return
        yield  # pragma: no cover — make this an async generator with no events

    agent_loop = MagicMock()
    agent_loop.get_context_usage.return_value = sentinel_usage
    agent_loop.continue_streaming.return_value = _empty_stream()

    # resume_messages + user_message=None 走 continue_streaming 分支，避开真实 provider/流。
    fake_ctx = StepAgentLoopContext(
        agent_loop=agent_loop,
        initial_prompt="Do x.",
        resume_messages=[Message(role="user", content="resume")],
        completion_guard_state={},
        restored_step_result=None,
    )
    executor.build_agent_loop_context = MagicMock(return_value=fake_ctx)

    events = []
    async for event in executor.execute(_make_step(), PipelineContext({"x": []}), session_id="s"):
        events.append(event)

    # 第一个事件必须是 ContextUsageEvent，且早于任何流事件。
    assert isinstance(events[0], ContextUsageEvent)
    assert events[0].usage == sentinel_usage
    agent_loop.get_context_usage.assert_called()
