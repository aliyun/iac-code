"""Deterministic P18 regression through the real pipeline, tools and disk recovery."""

from contextlib import aclosing
from pathlib import Path

import pytest
import yaml

from iac_code.pipeline.engine.events import PipelineEvent, PipelineEventType
from iac_code.pipeline.engine.pipeline_runner import PipelineRunner
from iac_code.pipeline.engine.ui_contract import encode_selected_candidate
from iac_code.services.session_storage import SessionStorage
from iac_code.tools.base import ToolRegistry
from iac_code.types.stream_events import (
    MessageEndEvent,
    MessageStartEvent,
    ToolResultEvent,
    ToolUseEndEvent,
    ToolUseStartEvent,
    Usage,
)

pytestmark = [pytest.mark.asyncio, pytest.mark.integration, pytest.mark.timeout(60)]
PIPELINE = Path(__file__).resolve().parents[3] / "src/iac_code/pipeline/selling_solution_first"
NAME = "Managed database"
BATCH_ID = "outline-original"
REPEAT_ID = "outline-idempotent-repeat"
OUTLINE = {
    "candidates": [
        {
            "candidate_name": NAME,
            "summary": "One managed RDS instance in Hangzhou",
            "total_cost": "CNY 800-1200/month",
            "key_tradeoff": "Fixed monthly cost",
        }
    ],
}
DETAIL = {
    "candidate_index": 0,
    "candidate_name": NAME,
    "total_cost": "CNY 900-1300/month",
    "applicable_scenarios": ["Managed relational database"],
    "resource_intents": [{"product": "RDS", "action": "create", "role": "database"}],
    "topology_graph": {"nodes": [{"id": "rds", "label": "RDS", "product": "RDS"}], "edges": []},
    "resource_inventory": [
        {
            "resource_id": "rds",
            "product": "RDS",
            "purpose": "database",
            "quantity": 1,
            "rough_cost": "CNY 900-1300/month",
            "lifecycle": "create",
        }
    ],
    "cost_assumptions": ["cn-hangzhou"],
    "cost_exclusions": [],
    "cost_confidence": "medium",
    "decision_notes": {
        "why_recommended": ["Meets the managed database request"],
        "problems_solved": ["Avoid manual database maintenance"],
        "pros": ["Managed backups", "Managed monitoring"],
        "cons": ["Fixed monthly cost"],
    },
}


class PlanningModel:
    """Only model output is scripted; no display, persistence or recovery mocks."""

    def __init__(self):
        self.calls = 0
        self.materialization_inspected = False

    def get_model_name(self):
        return "deterministic-p18-fixture"

    async def stream(self, messages, system, tools=None, max_tokens=8192):
        script = [
            (BATCH_ID, "show_architecture_plan", OUTLINE),
            ("candidate-detail", "show_candidate_detail", DETAIL),
            (REPEAT_ID, "show_architecture_plan", OUTLINE),
            (
                "wait-for-selection",
                "complete_step",
                {
                    "conclusion": {
                        "status": "awaiting_selection",
                        "intent": {
                            "cloud_platform": "aliyun",
                            "resource_intents": [{"product": "RDS", "action": "create", "source": "user"}],
                            "hard_constraints": [],
                        },
                    },
                },
            ),
            # If deterministic selection falls back, attempt the same valid
            # choice through the model so the real rejection is visible.
            (
                "select-candidate",
                "complete_step",
                {
                    "conclusion": {"status": "selected", "selected_candidate_index": 0},
                },
            ),
        ]
        if self.calls == len(script) - 1 and "第二步" in system:
            assert all(key not in system for key in ("monthly_range", "rough_monthly_cost", "monthly_cost"))
            assert "CNY 900-1300/month" in system
            assert "CNY 800-1200/month" not in system
            self.materialization_inspected = True
            yield MessageStartEvent(message_id="materialization-prompt-checked")
            return
        assert self.calls < len(script), (
            "Valid selection fell back to the model instead of advancing",
            [
                (block.tool_use_id, block.content)
                for message in messages
                if isinstance(message.content, list)
                for block in message.content
                if block.type == "tool_result"
            ],
        )
        call_id, name, arguments = script[self.calls]
        self.calls += 1
        yield MessageStartEvent(message_id=f"planning-{self.calls}")
        yield ToolUseStartEvent(tool_use_id=call_id, name=name)
        yield ToolUseEndEvent(tool_use_id=call_id, name=name, input=arguments)
        yield MessageEndEvent(stop_reason="tool_use", usage=Usage())


@pytest.mark.parametrize("mode", ["same-process", "cold-recovery", "legacy-cold-recovery"])
async def test_idempotent_outline_survives_selection_resume(tmp_path, monkeypatch, mode):
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("IAC_CODE_CONFIG_BACKUP_DIR", str(tmp_path / "backup"))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    model = PlanningModel()

    def runner(*, resume=False):
        return PipelineRunner(
            pipeline_dir=PIPELINE,
            provider_manager=model,
            base_tool_registry=ToolRegistry(),
            session_storage=SessionStorage(tmp_path / "projects"),
            session_id="p18-regression",
            cwd=str(workspace),
            resume_from_sidecar=resume,
        )

    flow = runner()
    events = [event async for event in flow.run("Plan a managed RDS database in Hangzhou")]
    results = {event.tool_use_id: event for event in events if isinstance(event, ToolResultEvent)}
    assert set(results) == {BATCH_ID, "candidate-detail", REPEAT_ID, "wait-for-selection"}
    assert not any(result.is_error for result in results.values()), results
    assert results[REPEAT_ID].metadata["idempotent"] is True
    assert results[REPEAT_ID].metadata["candidate_set_id"] == BATCH_ID
    saved = flow.context.get_conclusion("solution_selection")
    assert saved["status"] == "awaiting_selection"
    assert saved["candidates"][0]["rough_cost"]["cost_range"] == DETAIL["total_cost"]
    assert saved["candidate_set_id"] == BATCH_ID
    assert any(
        isinstance(event, PipelineEvent) and event.type == PipelineEventType.USER_INPUT_REQUIRED for event in events
    )
    assert model.calls == 4

    if mode == "legacy-cold-recovery":
        # Simulate a sidecar written by the previous version, keeping state/version metadata intact.
        context_path = flow.session.context_path
        snapshot = yaml.safe_load(context_path.read_text(encoding="utf-8"))
        for candidate in snapshot["solution_selection"]["value"]["candidates"]:
            cost = candidate["rough_cost"]
            cost["monthly_range"] = cost.pop("cost_range")
            for item in cost["items"]:
                item["monthly_cost"] = item.pop("cost")
            for resource in candidate["resource_inventory"]:
                resource["rough_monthly_cost"] = resource.pop("rough_cost")
        context_path.write_text(yaml.safe_dump(snapshot, allow_unicode=True), encoding="utf-8")
    if mode != "same-process":
        # Construct a new runner/storage: restore the real sidecar and attempt
        # transcript from disk, without carrying the live completion guard.
        flow = runner(resume=True)
        restored = flow.context.get_conclusion("solution_selection")
        if mode == "legacy-cold-recovery":
            assert "monthly_range" in restored["candidates"][0]["rough_cost"]
        else:
            assert restored == saved

    resumed = []
    async with aclosing(flow.resume(encode_selected_candidate(NAME, 0))) as stream:
        async for event in stream:
            resumed.append(event)
            if isinstance(event, ToolResultEvent):
                assert not event.is_error, event.result
            if (
                isinstance(event, PipelineEvent)
                and event.type == PipelineEventType.STEP_STARTED
                and event.step_id == "materialize_selected_candidate"
                and mode != "legacy-cold-recovery"
            ):
                break  # The selection boundary is verified; do not create cloud resources.
            if isinstance(event, MessageStartEvent) and event.message_id == "materialization-prompt-checked":
                break

    selected = flow.context.get_conclusion("solution_selection")
    assert selected["status"] == "selected", (
        "P18: an unchanged candidate batch was rejected after transcript recovery",
        resumed,
    )
    assert selected["candidate_set_id"] == BATCH_ID
    assert selected["selected_candidate_index"] == 0
    assert selected["selected_candidate"] == saved["candidates"][0]
    assert model.calls == 4, "Selection must not regenerate candidates or call the model again"
    if mode == "legacy-cold-recovery":
        assert model.materialization_inspected, "Verify the real Step 2 prompt after legacy disk recovery"
    assert any(
        isinstance(event, PipelineEvent)
        and event.type == PipelineEventType.STEP_STARTED
        and event.step_id == "materialize_selected_candidate"
        for event in resumed
    )
