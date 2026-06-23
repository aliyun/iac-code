from pathlib import Path

from iac_code.pipeline.engine.loader import load_pipeline_dir


def _selling_pipeline_dir() -> Path:
    return Path(__file__).resolve().parents[3] / "src" / "iac_code" / "pipeline" / "selling"


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
        "tool": "ros_stack",
        "action_in": ["CreateStack", "ContinueCreateStack"],
        "is_success": True,
        "status_in": ["CREATE_COMPLETE"],
        "match_conclusion_field": "stack_id",
    }
