from pathlib import Path

from iac_code.pipeline.engine.context import PipelineContext
from iac_code.pipeline.engine.loader import load_pipeline_dir
from iac_code.pipeline.engine.step_spec import render_prompt
from iac_code.pipeline.engine.ui_contract import encode_selected_candidate


def _selling_dir() -> Path:
    return Path(__file__).resolve().parents[3] / "src" / "iac_code" / "pipeline" / "selling"


def test_deploying_prompt_passes_stack_name_base_without_duplicating_skill_policy() -> None:
    selling_dir = _selling_dir()
    loaded = load_pipeline_dir(selling_dir)
    deploying_step = next(step for step in loaded.steps if step.step_id == "deploying")
    stack_name = "iac-e2e-original-intent"

    ctx = PipelineContext(loaded.context_dependencies)
    ctx.set_conclusion(
        "intent",
        {
            "requirement": f"创建 VSwitch，资源栈名称使用 {stack_name}",
            "non_functional": {"stack_name": stack_name},
        },
    )
    ctx.set_conclusion(
        "selected_plan",
        {
            "selection_valid": True,
            "selected_candidate": {"name": "existing-vpc-vswitch", "output_path": "templates/vswitch.yml"},
        },
    )
    ctx.set_conclusion("evaluated_candidates", [{"candidate": {"name": "existing-vpc-vswitch"}}])

    prompt = render_prompt(
        (selling_dir / deploying_step.prompt_file).read_text(encoding="utf-8"),
        ctx,
        deploying_step.context_fields,
    )

    assert "intent" in deploying_step.context_fields
    assert stack_name in prompt
    assert "stack_name" in prompt
    assert "必须精确等于该名称" not in prompt
    assert "追加唯一后缀" not in prompt
    assert "禁止省略 `params.StackName`" not in prompt
    assert "vswitch-in-existing-vpc" not in prompt
    assert "部署后是否等待用户继续" not in prompt
    assert "如果无法确定应使用的 StackName，不要调用 `CreateStack`" not in prompt


def test_deploying_prompt_renders_concrete_template_url() -> None:
    selling_dir = _selling_dir()
    loaded = load_pipeline_dir(selling_dir)
    deploying_step = next(step for step in loaded.steps if step.step_id == "deploying")

    ctx = PipelineContext(loaded.context_dependencies)
    ctx.set_conclusion("intent", {"requirement": "创建一个 VSwitch"})
    ctx.set_conclusion("selected_plan", {"user_input": encode_selected_candidate("existing-vpc-vswitch", 0)})
    ctx.set_conclusion(
        "evaluated_candidates",
        [{"candidate": {"name": "existing-vpc-vswitch", "output_path": "templates/vswitch.yml"}}],
    )

    assert deploying_step.on_enter is not None
    deploying_step.on_enter(ctx)
    prompt = render_prompt(
        (selling_dir / deploying_step.prompt_file).read_text(encoding="utf-8"),
        ctx,
        deploying_step.context_fields,
    )

    assert 'template_url = "templates/vswitch.yml"' in prompt
    assert 'params.TemplateURL = "templates/vswitch.yml"' not in prompt
    assert "<选中方案模板文件路径>" not in prompt
    assert "{selected_plan.template_url}" not in prompt


def test_deploying_renders_stack_outputs_after_complete_step() -> None:
    selling_dir = _selling_dir()
    loaded = load_pipeline_dir(selling_dir)
    deploying_step = next(step for step in loaded.steps if step.step_id == "deploying")
    prompt = (selling_dir / deploying_step.prompt_file).read_text(encoding="utf-8")

    assert deploying_step.complete_step_terminal is False
    assert "`complete_step` 成功返回后" in prompt
    assert "`complete_step.conclusion.outputs` 渲染 Stack Outputs" in prompt


def test_deploying_declares_failure_condition_for_failed_status() -> None:
    loaded = load_pipeline_dir(_selling_dir())
    deploying_step = next(step for step in loaded.steps if step.step_id == "deploying")

    assert deploying_step.failure_condition == {"field": "status", "values": ["failed"]}
