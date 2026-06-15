"""Regression tests for problems 1, 2, 3 — StepExecutor integration with main."""

from pathlib import Path
from unittest.mock import MagicMock

from iac_code.pipeline.engine.context import PipelineContext
from iac_code.pipeline.engine.step_executor import StepExecutor
from iac_code.pipeline.engine.step_spec import LoadedPipeline, StepSpec
from iac_code.tools.base import ToolRegistry


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
