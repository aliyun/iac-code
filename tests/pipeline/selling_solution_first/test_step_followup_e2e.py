"""Multi-turn followup contracts through the production runner, tools and sidecars."""

from __future__ import annotations

import copy
import json
from contextlib import aclosing
from dataclasses import asdict

import pytest

from iac_code.pipeline.engine.events import PipelineEvent, PipelineEventType
from iac_code.pipeline.engine.pipeline_runner import PipelineRunner
from iac_code.pipeline.engine.types import StepResult
from iac_code.pipeline.engine.ui_contract import encode_deployment_confirmation, encode_selected_candidate
from iac_code.pipeline.selling.tools import ros_template_tools
from iac_code.services.session_storage import SessionStorage
from iac_code.tools.base import ToolRegistry, ToolResult
from iac_code.tools.read_file import ReadFileTool
from iac_code.tools.write_file import WriteFileTool
from iac_code.types.permissions import PermissionMode, PermissionResult, ToolPermissionContext
from iac_code.types.stream_events import (
    MessageEndEvent,
    MessageStartEvent,
    TextDeltaEvent,
    TombstoneEvent,
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
        self.requests = []

    def get_model_name(self):
        return "offline-followup-contract"

    def add(self, *calls):
        self.queue.extend(calls)

    async def stream(self, messages, system, tools=None):
        assert self.queue, [repr(m) for m in messages[-2:]]
        self.requests.append(copy.deepcopy(messages))
        name, arguments = self.queue.pop(0)
        self.calls += 1
        call_id = f"call-{self.calls}"
        yield MessageStartEvent(message_id=call_id)
        yield TextDeltaEvent(text="Here is the explanation; the saved configuration is retained.")
        yield ToolUseStartEvent(tool_use_id=call_id, name=name)
        yield ToolUseEndEvent(tool_use_id=call_id, name=name, input=arguments)
        yield MessageEndEvent(stop_reason="tool_use", usage=Usage())


def make_flow(tmp_path, monkeypatch, *, model=None, resume=False):
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("IAC_CODE_CONFIG_BACKUP_DIR", str(tmp_path / "backup"))
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    model = model or ScriptedModel()
    registry = ToolRegistry()
    registry.register(WriteFileTool())
    registry.register(ReadFileTool())
    flow = PipelineRunner(
        pipeline_dir=PIPELINE,
        provider_manager=model,
        base_tool_registry=registry,
        session_storage=SessionStorage(tmp_path / "projects"),
        session_id="followup-contract",
        cwd=str(workspace),
        resume_from_sidecar=resume,
        permission_context_getter=lambda: ToolPermissionContext(
            mode=PermissionMode.BYPASS_PERMISSIONS, cwd=str(workspace)
        ),
    )
    return flow, model, workspace


def request_text(messages):
    return json.dumps([asdict(message) for message in messages], ensure_ascii=False)


@pytest.mark.parametrize("after_completion", [False, True])
@pytest.mark.parametrize("repeat_input", [False, True])
async def test_step1_cold_recovery_distinguishes_pending_input_from_completed_turn(
    tmp_path, monkeypatch, after_completion, repeat_input,
):
    flow, model, _ = make_flow(tmp_path, monkeypatch)
    plan_calls(model)
    assert_wait([event async for event in flow.run("Plan one managed database")])
    prompt = "Plan one managed database" if repeat_input else "Change the database capacity to 300 GB"
    if after_completion:
        plan_calls(model, updated=True)
    else:
        model.add(("complete_step", {"conclusion": {}}))
    execute = flow._step_executor.execute

    class InterruptedError(Exception):
        pass

    async def interrupt(*args, **kwargs):
        if not after_completion:
            raise InterruptedError
        async for event in execute(*args, **kwargs):
            if isinstance(event, StepResult):
                raise InterruptedError
            yield event

    monkeypatch.setattr(flow._step_executor, "execute", interrupt)
    with pytest.raises(InterruptedError):
        _ = [event async for event in flow.resume(prompt)]
    before = len(model.requests)
    restored, _, _ = make_flow(tmp_path, monkeypatch, model=model, resume=True)
    assert restored._restored_current_step_user_input.display_text == prompt
    assert_wait([event async for event in restored.continue_from_sidecar()], unchanged=not after_completion)
    assert len(model.requests) == before + (0 if after_completion else 1)
    assert request_text(model.requests[-1]).count(prompt) == (2 if repeat_input else 1)


async def test_repeated_followup_already_appended_before_crash_is_not_appended_again(tmp_path, monkeypatch):
    flow, model, _ = make_flow(tmp_path, monkeypatch)
    prompt = "Plan one managed database"
    plan_calls(model)
    assert_wait([event async for event in flow.run(prompt)])
    model.add(("complete_step", {"conclusion": {}}))
    stream = model.stream

    class InterruptedError(Exception):
        pass

    async def interrupt(*args, **kwargs):
        raise InterruptedError
        yield

    monkeypatch.setattr(model, "stream", interrupt)
    with pytest.raises(InterruptedError):
        _ = [event async for event in flow.resume(prompt)]
    monkeypatch.setattr(model, "stream", stream)
    before = len(model.requests)
    restored, _, _ = make_flow(tmp_path, monkeypatch, model=model, resume=True)
    assert_wait([event async for event in restored.continue_from_sidecar()], unchanged=True)
    assert len(model.requests) == before + 1
    assert request_text(model.requests[-1]).count(prompt) == 2


@pytest.mark.parametrize("cold", [False, True])
async def test_step1_followup_preserves_complete_history(tmp_path, monkeypatch, cold):
    flow, model, workspace = make_flow(tmp_path, monkeypatch)
    body = "Previously verified product knowledge: followup-history-sentinel"
    (workspace / "reference.md").write_text(body, encoding="utf-8")
    model.add(("read_file", {"path": "reference.md"}))
    plan_calls(model)
    original_prompt = "Plan one managed database with the original requirement"
    assert_wait([event async for event in flow.run(original_prompt)])
    previous_prompt = None
    for prompt in ("Explain the original requirement without changes", "Explain that again with the prior evidence"):
        if cold:
            flow, _, _ = make_flow(tmp_path, monkeypatch, model=model, resume=True)
        count = len(model.requests)
        await consult(flow, model, "solution_selection", prompt)
        # Exactly one new model call: the historical complete_step cannot finish
        # this turn, and neither the read nor the old completion is replayed.
        assert len(model.requests) == count + 1
        text = request_text(model.requests[count])
        assert text.count(original_prompt) == 1
        assert text.count(body) == 1
        assert text.count(prompt) == 1
        assert '"name": "complete_step"' in text
        if previous_prompt:
            assert text.count(previous_prompt) == 1
        previous_prompt = prompt


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


async def consult(
    flow, model, field, prompt="Explain the current cost, without changes", *, nested=False, repeat_status=False,
):
    saved = copy.deepcopy(flow.context.get_conclusion(field))
    evidence = copy.deepcopy(
        flow._current_parent_attempt(
            "solution_planning_and_selection" if field == "solution_selection" else "materialize_selected_candidate"
        ).get("completion_evidence")
    )
    conclusion = {"status": saved["status"]} if repeat_status else {"conclusion": {}} if nested else {}
    model.add(("complete_step", {"conclusion": conclusion}))
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


@pytest.mark.parametrize("visible_answer", ["none", "replacement", "previous"])
async def test_followup_answer_evidence_ignores_retracted_messages(tmp_path, monkeypatch, visible_answer):
    flow, model, _ = make_flow(tmp_path, monkeypatch)
    plan_calls(model)
    assert_wait([event async for event in flow.run("Plan one managed database")])
    saved = copy.deepcopy(flow.context.get_conclusion("solution_selection"))
    original_stream = model.stream

    async def stream(messages, system, tools=None):
        kind, _ = model.queue[0]
        if kind not in {"truncated", "silent", "previous"}:
            async for event in original_stream(messages, system, tools=tools):
                yield event
            return
        model.queue.pop(0)
        model.calls += 1
        message_id = f"call-{model.calls}"
        yield MessageStartEvent(message_id=message_id)
        if kind != "silent":
            yield TextDeltaEvent(text="The current plan is retained; here is its explanation.")
        if kind != "previous":
            yield ToolUseStartEvent(tool_use_id=message_id, name="complete_step")
            yield ToolUseEndEvent(tool_use_id=message_id, name="complete_step", input={"conclusion": {}})
        yield MessageEndEvent(stop_reason="tool_use" if kind == "silent" else "max_tokens", usage=Usage())

    monkeypatch.setattr(model, "stream", stream)
    if visible_answer == "previous":
        # Pure text continued after max_tokens stays visible; only the later
        # truncated tool message is withdrawn by AgentLoop.
        model.add(("previous", {}))
    model.add(("truncated", {}))
    if visible_answer == "replacement":
        model.add(("complete_step", {"conclusion": {}}))
    else:
        model.add(("silent", {}))
        if visible_answer == "none":
            model.add(("complete_step", {"conclusion": {}}))
    events = [event async for event in flow.resume("Explain the current plan without changing it")]
    results = [event for event in events if isinstance(event, ToolResultEvent) and event.tool_name == "complete_step"]
    assert len([event for event in events if isinstance(event, TombstoneEvent)]) == 1
    assert [result.is_error for result in results] == ([True, False] if visible_answer == "none" else [False])
    if visible_answer == "none":
        assert "visibly before" in results[0].result
    completed = [event for event in events
                 if isinstance(event, PipelineEvent) and event.type == PipelineEventType.STEP_COMPLETED]
    assert completed[-1].data["conclusion_unchanged"] is True
    actual = copy.deepcopy(flow.context.get_conclusion("solution_selection"))
    saved.pop("user_input", None)
    actual.pop("user_input", None)
    assert actual == saved
    assert not model.queue


async def test_step1_followup_selection_and_new_batch(tmp_path, monkeypatch):
    flow, model, _ = make_flow(tmp_path, monkeypatch)
    plan_calls(model)
    assert_wait([e async for e in flow.run("Plan one managed database")])
    original = copy.deepcopy(flow.context.get_conclusion("solution_selection"))
    await consult(flow, model, "solution_selection", nested=True)
    await consult(flow, model, "solution_selection", "Explain billing after stopping")
    await consult(flow, model, "solution_selection", repeat_status=True)
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
    step1_prompt = "Plan one managed database with step-one-only-history"
    assert_wait([e async for e in flow.run(step1_prompt)])
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
    first_step2_request = len(model.requests)
    assert_wait([e async for e in flow.resume(encode_selected_candidate(selection["candidates"][0]["name"], 0))])
    step2_messages = model.requests[first_step2_request]
    assert step1_prompt not in request_text(step2_messages)
    assert '"name": "show_candidate_detail"' not in request_text(step2_messages)
    assert (workspace / path).is_file()
    if control == "reselect":
        flow, _, _ = make_flow(tmp_path, monkeypatch, model=model, resume=True)
    step2_question = "Explain the current quote with step-two-only-history"
    count = len(model.requests)
    await consult(flow, model, "selected_plan", step2_question, repeat_status=True)
    assert len(model.requests) == count + 1
    assert '"name": "ros_preview_template"' in request_text(model.requests[count])
    if control == "reselect":
        flow, _, _ = make_flow(tmp_path, monkeypatch, model=model, resume=True)
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
    assert step2_question in request_text(model.requests[-1])
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
        rollback_request_index = len(model.requests) + 1
        plan_calls(model, updated=True)
        assert_wait([event async for event in flow.resume(prompt)])
        rebuilt = request_text(model.requests[rollback_request_index])
        assert prompt in rebuilt
        assert step1_prompt not in rebuilt
        assert step2_question not in rebuilt
        assert '"name": "complete_step"' not in rebuilt
        assert '"name": "ros_preview_template"' not in rebuilt
        assert flow.context.get_conclusion("solution_selection")["candidate_set_id"] != selection["candidate_set_id"]
        assert not model.queue
        return
    else:
        prompt = encode_deployment_confirmation(
            "confirm" if control == "confirm-overrides" else control,
            {"DBInstanceStorage": 220} if control == "confirm-overrides" else None,
        )
    boundary = "deploying"
    async with aclosing(flow.resume(prompt)) as stream:
        async for event in stream:
            if (
                isinstance(event, PipelineEvent)
                and event.type == PipelineEventType.STEP_STARTED
                and event.step_id == boundary
            ):
                break
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
