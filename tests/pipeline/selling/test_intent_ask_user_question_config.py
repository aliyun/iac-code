from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from iac_code.pipeline.engine.context import PipelineContext
from iac_code.pipeline.engine.loader import load_pipeline_dir
from iac_code.pipeline.engine.step_executor import StepExecutor
from iac_code.tools.base import ToolRegistry


def _selling_dir() -> Path:
    return Path(__file__).resolve().parents[3] / "src" / "iac_code" / "pipeline" / "selling"


def test_selling_intent_step_injects_ask_user_question():
    loaded = load_pipeline_dir(_selling_dir())
    intent_step = next(step for step in loaded.steps if step.step_id == "intent_parsing")

    assert "ask_user_question" in intent_step.inject_tools


def test_selling_intent_step_guards_guidable_completion_until_question():
    loaded = load_pipeline_dir(_selling_dir())
    intent_step = next(step for step in loaded.steps if step.step_id == "intent_parsing")

    assert any(
        guard.get("require_tool") == "ask_user_question"
        and guard.get("required_conclusion_any_of") == ["clarification_choice", "clarification_text"]
        and guard.get("copy_tool_result_to_conclusion", {}).get("selected_id") == "clarification_choice"
        and guard.get("copy_tool_result_to_conclusion", {}).get("free_text") == "clarification_text"
        for guard in intent_step.completion_guards
    )


def test_selling_intent_step_guards_non_deployment_completion_until_question():
    loaded = load_pipeline_dir(_selling_dir())
    intent_step = next(step for step in loaded.steps if step.step_id == "intent_parsing")

    guarded_categories = {
        guard.get("when_conclusion_field_equals", {}).get("category")
        for guard in intent_step.completion_guards
        if guard.get("require_tool") == "ask_user_question"
    }

    assert {"chat", "code_request", "knowledge_question"}.issubset(guarded_categories)


def test_selling_intent_step_builds_registry_with_ask_user_question():
    loaded = load_pipeline_dir(_selling_dir())
    intent_step = next(step for step in loaded.steps if step.step_id == "intent_parsing")
    executor = StepExecutor(
        provider_manager=MagicMock(),
        base_tool_registry=ToolRegistry(),
        pipeline=loaded,
        pipeline_dir=_selling_dir(),
    )

    registry = executor._build_step_tools(intent_step, PipelineContext(loaded.context_dependencies))

    assert registry.get("ask_user_question") is not None
