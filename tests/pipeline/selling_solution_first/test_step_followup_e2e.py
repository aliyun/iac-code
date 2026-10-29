"""Multi-turn followup contracts through the production runner, tools and sidecars."""

from __future__ import annotations

import copy
import json
from contextlib import aclosing

import pytest

from iac_code.pipeline.engine.events import PipelineEvent, PipelineEventType
from iac_code.pipeline.engine.pipeline_runner import PipelineRunner
from iac_code.pipeline.engine.ui_contract import encode_deployment_confirmation, encode_selected_candidate
from iac_code.pipeline.selling.tools import ros_template_tools
from iac_code.services.session_storage import SessionStorage
from iac_code.tools.base import ToolRegistry, ToolResult
from iac_code.tools.write_file import WriteFileTool
from iac_code.types.permissions import PermissionMode, PermissionResult, ToolPermissionContext
from iac_code.types.stream_events import (
    MessageEndEvent,
    MessageStartEvent,
    TextDeltaEvent,
    ToolResultEvent,
    ToolUseEndEvent,
    ToolUseStartEvent,
    Usage,
)
from tests.pipeline.selling_solution_first import test_completion_projection as fixture
from tests.pipeline.selling_solution_first.test_candidate_batch_e2e import DETAIL, OUTLINE, PIPELINE

pytestmark = [pytest.mark.asyncio, pytest.mark.integration, pytest.mark.timeout(60)]


class ScriptedModel:
    def __init__(self):
        self.queue = []
        self.calls = 0

    def get_model_name(self):
        return "offline-followup-contract"

    def add(self, *calls):
        self.queue.extend(calls)

    async def stream(self, messages, system, tools=None, max_tokens=8192):
        assert self.queue, [repr(m) for m in messages[-2:]]
        name, arguments = self.queue.pop(0)
        self.calls += 1
        call_id = f"call-{self.calls}"
        yield MessageStartEvent(message_id=call_id)
        yield TextDeltaEvent(text="Here is the explanation; the saved configuration is retained.")
        yield ToolUseStartEvent(tool_use_id=call_id, name=name)
        yield ToolUseEndEvent(tool_use_id=call_id, name=name, input=arguments)
        yield MessageEndEvent(stop_reason="tool_use", usage=Usage())


def make_flow(tmp_path, monkeypatch):
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("IAC_CODE_CONFIG_BACKUP_DIR", str(tmp_path / "backup"))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    model = ScriptedModel()
    registry = ToolRegistry()
    registry.register(WriteFileTool())
    flow = PipelineRunner(
        pipeline_dir=PIPELINE,
        provider_manager=model,
        base_tool_registry=registry,
        session_storage=SessionStorage(tmp_path / "projects"),
        session_id="followup-contract",
        cwd=str(workspace),
        permission_context_getter=lambda: ToolPermissionContext(
            mode=PermissionMode.BYPASS_PERMISSIONS, cwd=str(workspace)
        ),
    )
    return flow, model, workspace


def plan_calls(model, *, updated=False):
    outline, detail = copy.deepcopy(OUTLINE), copy.deepcopy(DETAIL)
    if updated:
        outline["candidates"][0]["summary"] = "Managed database with increased storage"
        detail["resource_inventory"][0]["quantity"] = 2
    model.add(
        ("show_architecture_plan", outline),
        ("show_candidate_detail", detail),
        (
            "complete_step",
            {
                "conclusion": {
                    "status": "awaiting_selection",
                    "intent": {
                        "cloud_platform": "aliyun",
                        "resource_intents": [{"product": "RDS", "action": "create"}],
                        "hard_constraints": [],
                    },
                }
            },
        ),
    )


def assert_wait(events, *, unchanged=False):
    errors = [e.result for e in events if isinstance(e, ToolResultEvent) and e.is_error]
    assert not errors, errors
    assert any(isinstance(e, PipelineEvent) and e.type == PipelineEventType.USER_INPUT_REQUIRED for e in events)
    completed = [e for e in events if isinstance(e, PipelineEvent) and e.type == PipelineEventType.STEP_COMPLETED]
    assert completed
    assert bool(completed[-1].data.get("conclusion_unchanged")) == unchanged
    assert completed[-1].data["conclusion"]


async def consult(flow, model, field, prompt="Explain the current cost, without changes", *, nested=False):
    saved = copy.deepcopy(flow.context.get_conclusion(field))
    evidence = copy.deepcopy(
        flow._current_parent_attempt(
            "solution_planning_and_selection" if field == "solution_selection" else "materialize_selected_candidate"
        ).get("completion_evidence")
    )
    model.add(("complete_step", {"conclusion": {"conclusion": {}}} if nested else {"conclusion": {}}))
    events = [event async for event in flow.resume(prompt)]
    assert_wait(events, unchanged=True)
    actual = copy.deepcopy(flow.context.get_conclusion(field))
    actual.pop("user_input", None)
    saved.pop("user_input", None)
    assert actual == saved
    assert (
        flow._current_parent_attempt(
            "solution_planning_and_selection" if field == "solution_selection" else "materialize_selected_candidate"
        )["completion_evidence"]
        == evidence
    )
    assert not model.queue


async def test_step1_followup_selection_and_new_batch(tmp_path, monkeypatch):
    flow, model, _ = make_flow(tmp_path, monkeypatch)
    plan_calls(model)
    assert_wait([e async for e in flow.run("Plan one managed database")])
    original = copy.deepcopy(flow.context.get_conclusion("solution_selection"))
    await consult(flow, model, "solution_selection", nested=True)
    await consult(flow, model, "solution_selection", "Explain billing after stopping")
    plan_calls(model, updated=True)
    assert_wait([e async for e in flow.resume("Increase database capacity")])
    saved = flow.context.get_conclusion("solution_selection")
    assert saved["candidate_set_id"] != original["candidate_set_id"]
    assert saved["candidates"] != original["candidates"]
    await consult(flow, model, "solution_selection")
    count = model.calls
    async with aclosing(flow.resume(encode_selected_candidate(saved["candidates"][0]["name"], 0))) as stream:
        async for event in stream:
            if (
                isinstance(event, PipelineEvent)
                and event.type == PipelineEventType.STEP_STARTED
                and event.step_id == "materialize_selected_candidate"
            ):
                break
    assert flow.context.get_conclusion("solution_selection")["status"] == "selected"
    assert model.calls == count


@pytest.mark.parametrize("control", ["confirm", "confirm-overrides", "cancel", "reselect"])
async def test_step2_followup_comparison_update_and_confirmation(tmp_path, monkeypatch, control):
    flow, model, workspace = make_flow(tmp_path, monkeypatch)
    responses = {r["tool_name"]: r["result"] for r in fixture._records()}

    async def cloud_response(self, *, tool_input, context):
        return ToolResult.success(json.dumps(responses[self.name]))

    async def allow_cloud(self, input, context=None):
        return PermissionResult(behavior="allow")

    for cls in (
        ros_template_tools.RosValidateTemplateTool,
        ros_template_tools.RosPreviewTemplateTool,
        ros_template_tools.RosEstimateTemplateCostTool,
    ):
        monkeypatch.setattr(cls, "execute", cloud_response)
        monkeypatch.setattr(cls, "check_permissions", allow_cloud)
        monkeypatch.setattr(cls, "requires_runtime_execution_class", property(lambda self: False))
    plan_calls(model)
    assert_wait([e async for e in flow.run("Plan one managed database")])
    selection = flow.context.get_conclusion("solution_selection")
    path = selection["candidates"][0]["output_path"]
    fixture._write_template(workspace)
    template = (workspace / fixture.TEMPLATE_PATH).read_text(encoding="utf-8")

    def materialize(storage, *, write=False):
        if write:
            model.add(("write_file", {"path": path, "content": template}))
        params = {**fixture.PARAMETERS, "DBInstanceStorage": storage}
        for name in ("ros_validate_template", "ros_preview_template", "ros_estimate_template_cost"):
            model.add(
                (
                    name,
                    {
                        "template_url": path,
                        "region_id": "cn-hangzhou",
                        **({"parameters": params} if name != "ros_validate_template" else {}),
                        **({"stack_name": "preview-contract"} if name == "ros_preview_template" else {}),
                    },
                )
            )
        model.add(
            (
                "complete_step",
                {
                    "conclusion": {
                        "status": "awaiting_confirmation",
                        "solution_summary": "Managed database",
                        "parameter_overrides": {},
                        "missing_deployment_parameters": [],
                        "hard_constraint_checks": [],
                    }
                },
            )
        )

    materialize(120, write=True)
    assert_wait([e async for e in flow.resume(encode_selected_candidate(selection["candidates"][0]["name"], 0))])
    assert (workspace / path).is_file()
    await consult(flow, model, "selected_plan")
    model.add(
        (
            "ros_estimate_template_cost",
            {
                "template_url": path,
                "region_id": "cn-hangzhou",
                "parameters": {**fixture.PARAMETERS, "DBInstanceStorage": 160},
            },
        )
    )
    await consult(flow, model, "selected_plan", "Compare larger storage without changing it")
    assert flow.context.get_conclusion("selected_plan")["effective_deployment_parameters"]["DBInstanceStorage"] == 120
    materialize(180)
    assert_wait([e async for e in flow.resume("Change database storage to 180 GB")])
    assert flow.context.get_conclusion("selected_plan")["effective_deployment_parameters"]["DBInstanceStorage"] == 180
    await consult(flow, model, "selected_plan")
    count = model.calls
    if control == "reselect":
        model.add(
            (
                "complete_step",
                {"conclusion": {"status": "reselect_requested", "reselect_reason": "Choose another plan"}},
            )
        )
        prompt = "Choose another plan"
    else:
        prompt = encode_deployment_confirmation(
            "confirm" if control == "confirm-overrides" else control,
            {"DBInstanceStorage": 220} if control == "confirm-overrides" else None,
        )
    boundary = "solution_planning_and_selection" if control == "reselect" else "deploying"
    async with aclosing(flow.resume(prompt)) as stream:
        async for event in stream:
            if (
                isinstance(event, PipelineEvent)
                and event.type == PipelineEventType.STEP_STARTED
                and event.step_id == boundary
            ):
                break
    if control == "reselect":
        assert flow.state_machine.current_step.step_id == boundary
        assert model.calls == count + 1
    else:
        expected = "confirmed" if control.startswith("confirm") else "cancelled"
        assert flow.context.get_conclusion("selected_plan")["status"] == expected
        assert model.calls == count
        if control == "confirm-overrides":
            assert (
                flow.context.get_conclusion("selected_plan")["effective_deployment_parameters"]["DBInstanceStorage"]
                == 220
            )


@pytest.mark.parametrize("invalid", ["first-empty", "changed-detail"])
async def test_step1_followup_invalid_empty_then_corrected(tmp_path, monkeypatch, invalid):
    flow, model, _ = make_flow(tmp_path, monkeypatch)
    if invalid == "first-empty":
        model.add(("complete_step", {"conclusion": {}}))
        plan_calls(model)
        events = [e async for e in flow.run("Plan one database")]
    else:
        plan_calls(model)
        assert_wait([e async for e in flow.run("Plan one database")])
        changed = copy.deepcopy(DETAIL)
        changed["resource_inventory"][0]["quantity"] = 2
        model.add(("show_candidate_detail", changed), ("complete_step", {"conclusion": {}}))
        plan_calls(model, updated=True)
        events = [e async for e in flow.resume("Increase database capacity")]
    complete_ids = {e.tool_use_id for e in events if isinstance(e, ToolUseEndEvent) and e.name == "complete_step"}
    results = [e for e in events if isinstance(e, ToolResultEvent) and e.tool_use_id in complete_ids]
    assert results[0].is_error
    assert results[-1].is_error is False
    assert flow.context.get_conclusion("solution_selection")["status"] == "awaiting_selection"
    model.add(("complete_step", {"conclusion": {"status": "rejected", "rejection_reason": "User cancelled"}}))
    ended = [e async for e in flow.resume("Cancel this purchase")]
    assert not any(isinstance(e, ToolResultEvent) and e.is_error for e in ended)
    assert flow.context.get_conclusion("solution_selection")["status"] == "rejected"
