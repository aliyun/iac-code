"""Period-neutral model contracts, persisted candidates and legacy display clients."""

import asyncio
import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from iac_code.pipeline.engine.context import PipelineContext
from iac_code.pipeline.engine.loader import load_pipeline_dir
from iac_code.pipeline.engine.step_executor import StepExecutor
from iac_code.pipeline.selling_solution_first.tools.candidate_planning_records import normalize_candidate_costs
from iac_code.pipeline.selling_solution_first.tools.show_architecture_plan_tool import ShowArchitecturePlanTool
from iac_code.pipeline.selling_solution_first.tools.show_candidate_detail_tool import ShowCandidateDetailTool
from iac_code.tools.base import ToolContext, ToolRegistry
from iac_code.types.stream_events import CandidateDetailEvent

PIPELINE = Path(__file__).resolve().parents[3] / "src/iac_code/pipeline/selling_solution_first"
LEGACY_FIELDS = {"total_monthly_cost", "rough_monthly_cost", "monthly_range", "monthly_cost"}


def legacy_candidate():
    return {
        "candidate_id": "candidate-0", "name": "GPU", "summary": "Eight-hour GPU test",
        "output_path": "templates/0-gpu.yml",
        "resource_inventory": [{"resource_id": "gpu", "rough_monthly_cost": "¥240/8小时"}],
        "rough_cost": {
            "monthly_range": "¥240/8小时", "items": [{"name": "ECS", "monthly_cost": "¥240/8小时"}],
        },
    }


@pytest.mark.parametrize("step_id", ["materialize_selected_candidate", "deploying"])
def test_restored_candidates_are_neutral_before_rendering_prompts(step_id, tmp_path, monkeypatch):
    monkeypatch.delenv("IAC_CODE_ROS_AGENT_AWARENESS", raising=False)
    pipeline = load_pipeline_dir(PIPELINE)
    ctx = PipelineContext(pipeline.context_dependencies)
    candidate = legacy_candidate()
    ctx.set_conclusion("solution_selection", {
        "status": "selected", "continue_pipeline": True,
        "candidates": [candidate], "selected_candidate": copy.deepcopy(candidate),
        "selected_candidate_index": 0, "selected_candidate_name": "GPU",
    })
    field = ctx.get_field("solution_selection")
    field.stale = True
    metadata = (field.version, field.stale, field.updated_at, copy.deepcopy(field.history))
    step = next(step for step in pipeline.steps if step.step_id == step_id)
    step.on_enter(ctx)

    selection = ctx.get_conclusion("solution_selection")
    for value in [selection["candidates"][0], selection["selected_candidate"]]:
        assert value["rough_cost"]["cost_range"] == "¥240/8小时"
        assert value["rough_cost"]["items"][0]["cost"] == "¥240/8小时"
        assert value["resource_inventory"][0]["rough_cost"] == "¥240/8小时"
        assert not any(key in json.dumps(value) for key in LEGACY_FIELDS)
    assert (field.version, field.stale, field.updated_at, field.history) == metadata
    assert candidate == legacy_candidate(), "Do not mutate retained historical candidate references"

    executor = StepExecutor(
        provider_manager=SimpleNamespace(), base_tool_registry=ToolRegistry(),
        pipeline=pipeline, pipeline_dir=PIPELINE, cwd=str(tmp_path),
    )
    prompt = executor._build_full_system_prompt(step, ctx)
    assert not any(key in prompt for key in LEGACY_FIELDS)
    if step_id == "materialize_selected_candidate":
        assert "¥240/8小时" in prompt
        assert selection["selection_valid"] is True


def test_canonical_values_including_zero_take_precedence_over_legacy_values():
    candidate = legacy_candidate()
    candidate["resource_inventory"][0]["rough_cost"] = "¥0/小时"
    candidate["rough_cost"]["cost_range"] = "¥0/小时"
    candidate["rough_cost"]["items"][0]["cost"] = 0
    migrated = normalize_candidate_costs(candidate)
    assert migrated["rough_cost"]["cost_range"] == "¥0/小时"
    assert migrated["rough_cost"]["items"][0]["cost"] == 0
    assert migrated["resource_inventory"][0]["rough_cost"] == "¥0/小时"
    assert normalize_candidate_costs(migrated) == migrated


@pytest.mark.asyncio
async def test_new_outline_matches_legacy_record_without_changing_candidate_batch():
    legacy = {"candidate_name": "GPU", "summary": "8h GPU", "total_monthly_cost": "¥240/8小时",
              "key_tradeoff": "Compute only"}
    state = {"tool_result_records": [{
        "tool_name": "show_architecture_plan", "input": {"candidates": [legacy]},
        "is_error": False, "record_id": "original-batch", "sequence": 1,
    }]}
    current = {**legacy, "total_cost": legacy["total_monthly_cost"]}
    del current["total_monthly_cost"]
    queue = asyncio.Queue()
    result = await ShowArchitecturePlanTool(state).execute(
        tool_input={"candidates": [current]}, context=ToolContext(event_queue=queue, tool_use_id="new-call"),
    )
    assert not result.is_error
    assert result.metadata == {"candidate_set_id": "original-batch", "idempotent": True}
    assert queue.empty()


@pytest.mark.asyncio
async def test_neutral_tools_keep_costs_in_the_existing_display_event_contract():
    outline = {"candidate_name": "GPU", "summary": "8h GPU", "total_cost": "¥240/8小时",
               "key_tradeoff": "Compute only"}
    state = {"tool_result_records": [{
        "tool_name": "show_architecture_plan", "input": {"candidates": [outline]},
        "is_error": False, "record_id": "batch", "sequence": 1,
    }]}
    queue = asyncio.Queue()
    result = await ShowCandidateDetailTool(state).execute(
        tool_input={
            "candidate_name": "GPU", "candidate_index": 0,
            "topology_graph": {"nodes": [{"id": "gpu", "label": "GPU", "product": "ECS"}], "edges": []},
            "resource_inventory": [{"resource_id": "gpu", "product": "ECS", "rough_cost": "¥240/8小时"}],
        }, context=ToolContext(event_queue=queue, tool_use_id="detail"),
    )
    assert not result.is_error
    event = queue.get_nowait()
    assert isinstance(event, CandidateDetailEvent)
    assert event.total_monthly_cost == "¥240/8小时"
    assert event.cost_items == [{"name": "ECS", "spec": "", "monthly_cost": "¥240/8小时"}]


def test_all_current_model_contracts_and_skill_examples_avoid_legacy_cost_fields():
    pipeline = load_pipeline_dir(PIPELINE)
    skill_root = PIPELINE / "skills/iac-aliyun-solution-first"
    contracts = [
        ShowArchitecturePlanTool().input_schema, ShowCandidateDetailTool().input_schema,
        pipeline.steps[0].conclusion_schema,
    ]
    rendered = json.dumps(contracts) + (skill_root / "SKILL.md").read_text() + (skill_root / "evals.json").read_text()
    assert not any(key in rendered for key in LEGACY_FIELDS)
