"""The template generation step must enforce resource type consistency in code."""

from __future__ import annotations

from pathlib import Path

import pytest

from iac_code.pipeline.engine.loader import load_pipeline_dir


def _template_generating_guards() -> list[dict]:
    selling_dir = Path(__file__).resolve().parents[3] / "src" / "iac_code" / "pipeline" / "selling"
    pipeline = load_pipeline_dir(selling_dir)
    step = next(
        step for step in pipeline.sub_pipelines["evaluate_candidate"].steps if step.step_id == "template_generating"
    )
    return step.completion_guards


@pytest.fixture(scope="module")
def guards() -> list[dict]:
    return _template_generating_guards()


def test_step_has_completion_guards(guards):
    assert guards, "template_generating must not rely on prompt wording alone"


def test_resource_type_consistency_guard_always_applies(guards):
    guard = next(guard for guard in guards if "require_template_resource_type_consistency" in guard)
    requirement = guard["require_template_resource_type_consistency"]

    assert guard["always"] is True
    assert requirement["template_field"] == "template"
    assert requirement["resource_intents_field"] == "candidate.resource_intents"
    assert guard["message_key"] == "template_resource_type_consistency_required"


def test_validate_template_result_is_required_for_the_generated_file(guards):
    guard = next(guard for guard in guards if "require_tool_result" in guard)
    requirement = guard["require_tool_result"]

    assert guard["always"] is True
    assert requirement["tool"] == "ros_validate_template"
    assert requirement["match_conclusion_field"] == "file_path"
    assert requirement["match_result_field"] == "input.template_url"


def test_rewriting_the_template_after_validation_is_rejected(guards):
    guard = next(guard for guard in guards if "require_tool_result" in guard)
    rules = guard["require_tool_result"]["disallow_tool_results_after_match"]

    assert any(set(rule.get("tools", [])) == {"write_file", "edit_file"} for rule in rules)


def test_guard_message_keys_are_translatable():
    from iac_code.pipeline.engine.complete_step_tool import (
        _COMPLETION_GUARD_MESSAGE_TEXT_BY_KEY,
        _completion_guard_message_i18n_markers,
    )

    for key in ("template_resource_type_consistency_required", "template_generating_validate_template_required"):
        assert key in _COMPLETION_GUARD_MESSAGE_TEXT_BY_KEY
        assert _COMPLETION_GUARD_MESSAGE_TEXT_BY_KEY[key] in set(_completion_guard_message_i18n_markers())
