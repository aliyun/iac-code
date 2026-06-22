from __future__ import annotations

from pathlib import Path

from iac_code.pipeline.engine.loader import load_pipeline_dir


def _selling_dir() -> Path:
    return Path(__file__).resolve().parents[3] / "src" / "iac_code" / "pipeline" / "selling"


def _step_by_id(step_id: str):
    loaded = load_pipeline_dir(_selling_dir())
    return next(step for step in loaded.steps if step.step_id == step_id)


def _sub_step_by_id(sub_pipeline_name: str, step_id: str):
    loaded = load_pipeline_dir(_selling_dir())
    return next(step for step in loaded.sub_pipelines[sub_pipeline_name].steps if step.step_id == step_id)


def test_memory_read_is_available_to_steps_that_need_autonomous_context_choice() -> None:
    intent = _step_by_id("intent_parsing")
    architecture = _step_by_id("architecture_planning")

    assert intent.tools is not None
    assert "read_memory" in intent.tools.include
    assert architecture.tools is not None
    assert "read_memory" in architecture.tools.include


def test_pipeline_steps_do_not_offer_write_memory_by_default() -> None:
    template = _sub_step_by_id("evaluate_candidate", "template_generating")
    cost = _sub_step_by_id("evaluate_candidate", "cost_estimating")
    deploying = _step_by_id("deploying")

    assert template.tools is not None
    assert "write_memory" in template.tools.exclude
    assert cost.tools is not None
    assert "write_memory" in cost.tools.exclude
    assert deploying.tools is not None
    assert "write_memory" in deploying.tools.exclude
