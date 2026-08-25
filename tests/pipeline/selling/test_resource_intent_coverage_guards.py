from __future__ import annotations

from pathlib import Path

from iac_code.pipeline.engine.loader import load_pipeline_dir


def _selling_dir() -> Path:
    return Path(__file__).resolve().parents[3] / "src" / "iac_code" / "pipeline" / "selling"


def _step(step_id: str):
    loaded = load_pipeline_dir(_selling_dir())
    return next(step for step in loaded.steps if step.step_id == step_id)


def _coverage_guard(step):
    return next(guard["require_resource_intent_coverage"] for guard in step.completion_guards)


def test_architecture_planning_guards_full_resource_intent_coverage():
    guard = _coverage_guard(_step("architecture_planning"))

    assert guard["source_fields"] == ["intent.resource_intents"]
    assert guard["items_field"] == "candidates"
    assert guard["covered_products_fields"] == ["resource_intents", "products"]
    assert guard["gaps_field"] == "resource_intent_gaps"


def test_confirm_and_select_guards_options_against_intent():
    step = _step("confirm_and_select")
    guard = _coverage_guard(step)

    assert guard["source_fields"] == ["intent.resource_intents"]
    assert guard["items_field"] == "options"
    assert guard["covered_products_fields"] == ["covered_products"]
    assert "intent" in step.context_fields


def test_confirm_and_select_options_expose_coverage_fields_on_every_surface():
    step = _step("confirm_and_select")
    schemas = [step.conclusion_schema] + [
        override.conclusion_schema
        for override in step.surface_overrides.values()
        if override.conclusion_schema is not None
    ]

    for schema in schemas:
        option_schema = schema["properties"]["options"]["items"]
        assert "covered_products" in option_schema["required"]
        gaps_schema = option_schema["properties"]["resource_intent_gaps"]
        assert gaps_schema["items"]["required"] == ["product", "reason"]
