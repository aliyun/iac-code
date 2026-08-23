from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from iac_code.pipeline.engine.complete_step_tool import CompleteStepTool, _completion_guard_message_from_key
from iac_code.pipeline.engine.context import PipelineContext
from iac_code.pipeline.engine.loader import load_pipeline_dir
from iac_code.pipeline.engine.step_executor import StepExecutor
from iac_code.pipeline.engine.types import StepConfig
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


def test_other_cloud_guard_does_not_match_provider_tokens_inside_aliyun_resource_ids():
    loaded = load_pipeline_dir(_selling_dir())
    intent_step = next(step for step in loaded.steps if step.step_id == "intent_parsing")
    guard = next(
        guard for guard in intent_step.completion_guards if guard.get("message_key") == "intent_alibaba_cloud_only"
    )
    pattern = guard["when_user_message_matches_any"][0]

    assert not CompleteStepTool._matches(pattern, "使用已有 VPC vpc-bp1gko19lwa6ngkey6wv7 创建 VSwitch")
    assert CompleteStepTool._matches(pattern, "请部署到 GKE")
    assert CompleteStepTool._matches(pattern, "请部署到华为云")


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


def _ga_topology_guard() -> dict:
    loaded = load_pipeline_dir(_selling_dir())
    intent_step = next(step for step in loaded.steps if step.step_id == "intent_parsing")
    return next(
        guard
        for guard in intent_step.completion_guards
        if guard.get("message_key") == "intent_ga_topology_clarification_required"
    )


def _ga_topology_guard_applies(message: str) -> bool:
    guard = _ga_topology_guard()
    matched = any(
        CompleteStepTool._matches(pattern, message) for pattern in guard["when_user_message_matches_any"]
    )
    escaped = any(
        CompleteStepTool._matches(pattern, message) for pattern in guard["unless_user_message_matches_any"]
    )
    return matched and not escaped


def test_ga_topology_guard_requires_question_and_topology_output():
    guard = _ga_topology_guard()

    assert guard["require_tool"] == "ask_user_question"
    assert "topology.entry_points" in guard["required_conclusion_any_of"]
    assert guard["copy_tool_result_to_conclusion"]["free_text"] == "clarification_text"


def test_ga_topology_guard_applies_to_unclarified_cross_region_intents():
    assert _ga_topology_guard_applies("帮我用全球加速做跨境加速")
    assert _ga_topology_guard_applies("部署一个 GA 跨境加速实例")


def test_ga_topology_guard_skips_intents_that_already_state_topology():
    assert not _ga_topology_guard_applies("创建 GA，需要 hk 上车、深圳下车")
    assert not _ga_topology_guard_applies("需要 5 个独立 GA，分别 hk 上车 深圳下车")


def test_ga_topology_guard_does_not_apply_to_ordinary_aliyun_intents():
    assert not _ga_topology_guard_applies("在 cn-hangzhou 创建 VPC、VSwitch 和一台 ECS")
    assert not _ga_topology_guard_applies("使用已有 VPC vpc-bp1gko19lwa6ngkey6wv7 创建 VSwitch")
    assert not _ga_topology_guard_applies("部署一个 nginx 网站")


def test_ga_topology_guard_message_key_resolves_to_actionable_text():
    message = _completion_guard_message_from_key("intent_ga_topology_clarification_required")

    assert message
    assert message != "intent_ga_topology_clarification_required"


def _intent_complete_step_tool(user_message: str, guard_state: dict) -> CompleteStepTool:
    loaded = load_pipeline_dir(_selling_dir())
    intent_step = next(step for step in loaded.steps if step.step_id == "intent_parsing")
    step_config = StepConfig(
        step_id=intent_step.step_id,
        conclusion_field=intent_step.conclusion_field,
        forward=intent_step.forward,
        conclusion_schema=intent_step.conclusion_schema,
    )
    return CompleteStepTool(
        step_config,
        completion_guards=intent_step.completion_guards,
        completion_guard_state=guard_state,
        user_message=user_message,
    )


def test_cross_region_intent_cannot_complete_without_topology_clarification():
    tool = _intent_complete_step_tool("帮我用全球加速做跨境加速方案", {})

    error = tool._validate_completion_guards(
        {"is_infra_intent": True, "confidence": "high", "hard_constraints": []}
    )

    assert error is not None
    assert "ask_user_question" in error


def test_cross_region_intent_completes_once_topology_is_confirmed():
    tool = _intent_complete_step_tool(
        "帮我用全球加速做跨境加速方案",
        {"successful_tools": {"ask_user_question"}},
    )

    error = tool._validate_completion_guards(
        {
            "is_infra_intent": True,
            "confidence": "high",
            "hard_constraints": [],
            "topology": {
                "instance_cardinality": "multiple",
                "entry_points": ["hk"],
                "exit_points": ["深圳"],
                "confirmed": True,
            },
        }
    )

    assert error is None


def test_intent_schema_exposes_topology_fields():
    loaded = load_pipeline_dir(_selling_dir())
    intent_step = next(step for step in loaded.steps if step.step_id == "intent_parsing")

    topology = intent_step.conclusion_schema["properties"]["topology"]

    assert set(topology["properties"]) >= {"instance_cardinality", "entry_points", "exit_points", "confirmed"}
    assert topology["properties"]["instance_cardinality"]["enum"] == ["single", "multiple", "unspecified"]
