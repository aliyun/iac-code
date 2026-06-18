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


def test_confirm_prompt_tells_model_to_output_candidate_index():
    prompt = (_selling_pipeline_dir() / "prompts" / "confirm_and_select.md").read_text(encoding="utf-8")

    assert "`options[].candidate_index`" in prompt


def test_deploying_can_rollback_to_confirm_and_select_for_invalid_selection():
    loaded = load_pipeline_dir(_selling_pipeline_dir())
    deploying = next(step for step in loaded.steps if step.step_id == "deploying")
    rollback_pairs = {(rule.target_step, rule.condition) for rule in deploying.rollback_rules}

    assert ("confirm_and_select", "invalid_selection") in rollback_pairs


def test_deploying_pauses_when_interrupt_judge_fails():
    loaded = load_pipeline_dir(_selling_pipeline_dir())
    deploying = next(step for step in loaded.steps if step.step_id == "deploying")

    assert deploying.interrupt_judge_failure == "pause"
