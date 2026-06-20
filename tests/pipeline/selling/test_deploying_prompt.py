from pathlib import Path

from iac_code.pipeline.engine.context import PipelineContext
from iac_code.pipeline.engine.loader import load_pipeline_dir
from iac_code.pipeline.engine.step_spec import render_prompt


def _selling_dir() -> Path:
    return Path(__file__).resolve().parents[3] / "src" / "iac_code" / "pipeline" / "selling"


def test_deploying_prompt_preserves_explicit_stack_name_without_e2e_controls() -> None:
    selling_dir = _selling_dir()
    loaded = load_pipeline_dir(selling_dir)
    deploying_step = next(step for step in loaded.steps if step.step_id == "deploying")
    stack_name = "iac-e2e-original-intent"

    ctx = PipelineContext(loaded.context_dependencies)
    ctx.set_conclusion(
        "intent",
        {
            "requirement": f"创建 VSwitch，部署资源栈 StackName 必须精确等于 {stack_name}",
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
    assert "params.StackName" in prompt
    assert "必须精确等于该名称" in prompt
    assert "用户未明确指定 StackName" in prompt
    assert "禁止省略 `params.StackName`" not in prompt
    assert "vswitch-in-existing-vpc" not in prompt
    assert "部署后是否等待用户继续" not in prompt
    assert "如果无法确定应使用的 StackName，不要调用 `CreateStack`" not in prompt
