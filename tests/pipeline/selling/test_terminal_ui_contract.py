from pathlib import Path

from iac_code.pipeline.engine.loader import load_pipeline_dir


def _selling_pipeline_dir() -> Path:
    return Path(__file__).resolve().parents[3] / "src" / "iac_code" / "pipeline" / "selling"


def test_selling_pipeline_base_sections_include_runtime_context():
    loaded = load_pipeline_dir(_selling_pipeline_dir())
    intent = next(step for step in loaded.steps if step.step_id == "intent_parsing")

    assert "runtime_context" in loaded.base_prompt_sections.include
    assert intent.base_prompt_sections is not None
    assert "runtime_context" in intent.base_prompt_sections.include


def test_confirm_options_schema_requires_candidate_index():
    loaded = load_pipeline_dir(_selling_pipeline_dir())
    confirm = next(step for step in loaded.steps if step.step_id == "confirm_and_select")
    schema = confirm.conclusion_schema
    assert schema is not None
    option_schema = schema["properties"]["options"]["items"]

    assert "candidate_index" in option_schema["required"]
    assert option_schema["properties"]["candidate_index"]["type"] == "integer"


def test_confirm_schema_accepts_parameter_overrides():
    loaded = load_pipeline_dir(_selling_pipeline_dir())
    confirm = next(step for step in loaded.steps if step.step_id == "confirm_and_select")
    schema = confirm.conclusion_schema
    assert schema is not None

    assert "parameter_overrides" in schema["properties"]
    assert schema["properties"]["parameter_overrides"]["type"] == "object"


def test_confirm_prompt_tells_model_to_output_candidate_index():
    prompt = (_selling_pipeline_dir() / "prompts" / "confirm_and_select.md").read_text(encoding="utf-8")

    assert "`options[].candidate_index`" in prompt


def test_confirm_prompt_shows_candidate_detail_in_same_round_as_architecture_diagram():
    prompt = (_selling_pipeline_dir() / "prompts" / "confirm_and_select.md").read_text(encoding="utf-8")

    diagram_pos = prompt.index("调用一次 `show_architecture_diagram`")
    detail_pos = prompt.index("调用 `show_candidate_detail` 工具")

    assert diagram_pos < detail_pos
    assert "在同一个工具调用轮次中，同时调用以下两个只读展示工具" in prompt
    assert "先为所有方案调用 `show_architecture_diagram`，再为所有方案调用 `show_candidate_detail`" not in prompt


def test_confirm_prompt_forbids_completion_before_optimized_architecture_diagram():
    prompt = (_selling_pipeline_dir() / "prompts" / "confirm_and_select.md").read_text(encoding="utf-8")

    assert "在 `show_architecture_diagram` 工具返回之前，不要调用 `complete_step`" in prompt


def test_confirm_prompt_does_not_ask_main_model_to_generate_semantic_plan():
    prompt = (_selling_pipeline_dir() / "prompts" / "confirm_and_select.md").read_text(encoding="utf-8")

    assert "不要根据工具返回内容自行生成 `semantic_plan`" in prompt
    assert "不要为同一候选方案再调用第二次 `show_architecture_diagram`" in prompt
    assert "随后第二次调用 `show_architecture_diagram`" not in prompt
    assert "`semantic_plan_scaffold`" not in prompt
    assert "非平凡架构" not in prompt


def test_confirm_step_only_exposes_selection_display_tools():
    from unittest.mock import MagicMock

    from iac_code.pipeline.engine.context import PipelineContext
    from iac_code.pipeline.engine.step_executor import StepExecutor
    from iac_code.tools.base import ToolRegistry

    loaded = load_pipeline_dir(_selling_pipeline_dir())
    confirm = next(step for step in loaded.steps if step.step_id == "confirm_and_select")
    registry = ToolRegistry()
    registry.register_default_tools()
    executor = StepExecutor(
        provider_manager=MagicMock(),
        base_tool_registry=registry,
        pipeline=loaded,
        pipeline_dir=_selling_pipeline_dir(),
    )

    tool_reg = executor._build_step_tools(confirm, PipelineContext({"evaluated_candidates": []}))

    assert tool_reg.get("complete_step") is not None
    assert tool_reg.get("show_architecture_diagram") is not None
    assert tool_reg.get("show_candidate_detail") is not None
    assert tool_reg.get("read_file") is None


def test_confirm_prompt_tells_model_to_preserve_parameter_overrides():
    prompt = (_selling_pipeline_dir() / "prompts" / "confirm_and_select.md").read_text(encoding="utf-8")

    assert "`parameter_overrides`" in prompt
    assert "用户选择方案时传入" in prompt
    assert "结构化 JSON" in prompt
    forbidden = ["A2A", "前端", "客户端", "方案 A", "方案 B", "策略 A", "策略 B", "讨论"]
    for phrase in forbidden:
        assert phrase not in prompt


def test_confirm_prompts_share_selection_contract_structure():
    repl_prompt = (_selling_pipeline_dir() / "prompts" / "confirm_and_select.md").read_text(encoding="utf-8")
    a2a_prompt = (_selling_pipeline_dir() / "prompts" / "confirm_and_select.a2a.md").read_text(encoding="utf-8")

    shared_fragments = [
        "## 首次执行",
        "### 待选择结论",
        "`complete_step.conclusion.options`",
        "`complete_step.conclusion.user_prompt`",
        "## 收到用户选择",
        '"selected_candidate_index": 0',
        "`parameter_overrides`",
        "`parameters`",
        "## 约束",
        "不要在本步骤重新询价",
        "不要修改模板 Default",
    ]
    for fragment in shared_fragments:
        assert fragment in repl_prompt
        assert fragment in a2a_prompt


def test_confirm_a2a_surface_uses_thin_prompt_without_display_tools():
    loaded = load_pipeline_dir(_selling_pipeline_dir())
    confirm = next(step for step in loaded.steps if step.step_id == "confirm_and_select")
    a2a = confirm.surface_overrides["a2a"]

    assert a2a.prompt_file == "prompts/confirm_and_select.a2a.md"
    assert a2a.inject_tools == []

    prompt = (_selling_pipeline_dir() / "prompts" / "confirm_and_select.a2a.md").read_text(encoding="utf-8")
    assert "`selected_candidate_index`" in prompt
    assert "`parameter_overrides`" in prompt
    assert "`complete_step.conclusion.user_prompt`" in prompt
    assert "不要在本步骤重新询价" in prompt
    assert "show_architecture_diagram" not in prompt
    assert "show_candidate_detail" not in prompt


def test_selling_steps_do_not_expose_static_rollback_rules():
    loaded = load_pipeline_dir(_selling_pipeline_dir())

    assert all(not hasattr(step, "rollback_rules") for step in loaded.steps)


def test_deploying_pauses_when_interrupt_judge_fails():
    loaded = load_pipeline_dir(_selling_pipeline_dir())
    deploying = next(step for step in loaded.steps if step.step_id == "deploying")

    assert deploying.interrupt_judge_failure == "pause"


def test_deploying_success_requires_create_stack_complete_guard():
    loaded = load_pipeline_dir(_selling_pipeline_dir())
    deploying = next(step for step in loaded.steps if step.step_id == "deploying")

    guard = next(
        (
            item
            for item in deploying.completion_guards
            if item.get("when_conclusion_field_equals") == {"status": "success"}
        ),
        None,
    )

    assert guard is not None
    assert guard["required_conclusion_field"] == "stack_id"
    assert guard["require_tool_result"] == {
        "tool": "ros_deploy",
        "action_in": ["create", "continue_create", "delete_and_create", "wait"],
        "is_success": True,
        "status_in": ["CREATE_COMPLETE"],
        "match_conclusion_field": "stack_id",
        "latest_record_must_match": True,
    }
