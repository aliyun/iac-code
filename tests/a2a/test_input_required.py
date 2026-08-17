from __future__ import annotations

import asyncio

import pytest
from a2a.types import Message, Part, Role, Task, TaskState, TaskStatus
from a2a.utils.errors import InvalidParamsError
from google.protobuf.json_format import MessageToDict
from google.protobuf.struct_pb2 import Value

from iac_code.a2a.events import publish_stream_event
from iac_code.a2a.executor import IacCodeA2AExecutor
from iac_code.a2a.input_required import (
    PermissionInputRegistry,
    PermissionResponse,
    parse_permission_response,
    permission_input_envelope,
    permission_safe_summary,
)
from iac_code.a2a.pipeline_events import PipelineA2AContext, PipelineEventTranslator
from iac_code.a2a.pipeline_journal import A2APipelineJournal
from iac_code.a2a.pipeline_snapshot import A2APipelineSnapshotStore
from iac_code.a2a.pipeline_stream import PipelineA2AEventPublisher, _unified_input_projection
from iac_code.a2a.runtime_overrides import a2a_request_context
from iac_code.a2a.task_store import A2ATaskStore
from iac_code.types.permissions import PermissionAuditMetadata, PermissionResult
from iac_code.types.stream_events import PermissionRequestEvent, SubPipelineStreamEvent

from .fakes import FakeEventQueue, pending_future


def _permission_message(
    *,
    decision: str = "allow_once",
    extra_part: bool = False,
    input_id: str = "permission-task-1-tool-1",
) -> Message:
    data = Value()
    data.struct_value.update(
        {
            "schemaVersion": 1,
            "kind": "permission",
            "requestTaskId": "task-1",
            "inputId": input_id,
            "toolUseId": "tool-1",
            "decision": decision,
        }
    )
    parts = [Part(data=data, media_type="application/json")]
    if extra_part:
        parts.append(Part(text="also allow"))
    return Message(
        message_id="message-1",
        task_id="task-1",
        context_id="ctx-1",
        role=Role.ROLE_USER,
        parts=parts,
    )


def test_permission_parser_requires_unique_json_part_and_exact_correlation() -> None:
    response = parse_permission_response(_permission_message())
    assert response is not None
    assert response.task_id == "task-1"
    assert response.context_id == "ctx-1"
    assert response.decision == "allow_once"
    with pytest.raises(InvalidParamsError, match="exactly one"):
        parse_permission_response(_permission_message(extra_part=True))
    with pytest.raises(InvalidParamsError, match="allow_once or deny"):
        parse_permission_response(_permission_message(decision="always"))
    extra_field = _permission_message()
    extra_field.parts[0].data.struct_value.update({"unexpected": "value"})
    with pytest.raises(InvalidParamsError, match="payload fields"):
        parse_permission_response(extra_field)


def test_other_json_data_parts_continue_to_generic_input_path() -> None:
    data = Value()
    data.struct_value.update({"kind": "unrelated", "value": 1})
    message = Message(
        message_id="message-1",
        task_id="task-1",
        context_id="ctx-1",
        role=Role.ROLE_USER,
        parts=[Part(data=data, media_type="application/json")],
    )
    assert parse_permission_response(message) is None


@pytest.mark.asyncio
async def test_normal_permission_publishes_input_required_and_resumes_live_future(monkeypatch) -> None:
    registry = PermissionInputRegistry()
    queue = FakeEventQueue()
    future = pending_future()
    request = PermissionRequestEvent(
        tool_name="bash",
        tool_input={"cmd": "rm /tmp/demo", "token": "secret-value"},
        tool_use_id="tool-1",
        response_future=future,
    )
    monkeypatch.setattr("iac_code.a2a.input_required.emit_permission_boundary_audit", lambda *_args, **_kwargs: True)
    publishing = asyncio.create_task(
        publish_stream_event(
            queue,
            task_id="task-1",
            context_id="ctx-1",
            event=request,
            permission_input_registry=registry,
        )
    )
    for _ in range(20):
        if queue.events:
            break
        await asyncio.sleep(0)
    assert queue.events[0].status.state == TaskState.TASK_STATE_INPUT_REQUIRED
    dumped = MessageToDict(queue.events[0], preserving_proto_field_name=False)
    parsed = parse_permission_response(_permission_message(input_id=dumped["metadata"]["iac_code"]["input"]["inputId"]))
    assert parsed is not None
    assert await registry.answer(parsed) is True
    await publishing
    assert future.result() is True
    assert queue.events[-1].status.state == TaskState.TASK_STATE_WORKING


@pytest.mark.asyncio
async def test_permission_mismatch_and_duplicate_reply_fail_closed(monkeypatch) -> None:
    registry = PermissionInputRegistry()
    request = PermissionRequestEvent(
        tool_name="bash",
        tool_input={"cmd": "pwd"},
        tool_use_id="tool-1",
        response_future=pending_future(),
    )
    pending = await registry.register(request, task_id="task-1", context_id="ctx-1")
    monkeypatch.setattr("iac_code.a2a.input_required.emit_permission_boundary_audit", lambda *_args, **_kwargs: True)
    wrong = parse_permission_response(_permission_message(input_id=pending.input_id))
    assert wrong is not None
    wrong = type(wrong)(**{**wrong.__dict__, "context_id": "ctx-other"})
    with pytest.raises(InvalidParamsError, match="input_response_mismatch"):
        await registry.answer(wrong)
    parsed = parse_permission_response(_permission_message(decision="deny", input_id=pending.input_id))
    assert parsed is not None
    assert await registry.answer(parsed) is False
    await registry.complete(pending)
    with pytest.raises(InvalidParamsError, match="pending permission"):
        await registry.answer(parsed)


def test_safe_summary_preserves_decision_values_and_redacts_secret() -> None:
    summary = permission_safe_summary(
        PermissionRequestEvent(
            tool_name="bash",
            tool_input={"cmd": "rm /tmp/demo", "api_key": "secret-value"},
            tool_use_id="tool-1",
        )
    )
    assert "rm /tmp/demo" in summary
    assert "secret-value" not in summary
    assert '"redacted":true' in summary


def test_permission_envelope_exposes_deterministic_human_readable_semantics() -> None:
    request = PermissionRequestEvent(
        tool_name="aliyun_api",
        tool_input={"product": "vpc", "action": "DescribeVpcs", "region_id": "cn-hangzhou"},
        tool_use_id="tool-1",
        permission_result=PermissionResult(
            behavior="ask",
            audit=PermissionAuditMetadata(
                scope="once",
                source="permission_pipeline",
                is_read_only=True,
                operation={"product": "vpc", "action": "DescribeVpcs", "region": "cn-hangzhou"},
            ),
        ),
    )
    envelope = permission_input_envelope(request, task_id="task-1", context_id="ctx-1")

    assert envelope["title"] == "Read Alibaba Cloud data with vpc DescribeVpcs"
    assert envelope["purpose"] == "Call vpc DescribeVpcs for the requested Alibaba Cloud infrastructure task."
    assert envelope["effect"] == "read"
    assert envelope["target"] == "vpc DescribeVpcs in cn-hangzhou"
    assert envelope["isReadOnly"] is True
    assert envelope["toolName"] == "aliyun_api"


def test_ros_stack_permission_names_action_and_target_stack() -> None:
    request = PermissionRequestEvent(
        tool_name="ros_stack",
        tool_input={
            "action": "CreateStack",
            "region_id": "cn-hangzhou",
            "params": {"StackName": "demo-vswitch-stack"},
        },
        tool_use_id="tool-stack",
        permission_result=PermissionResult(
            behavior="ask",
            audit=PermissionAuditMetadata(
                scope="once",
                source="permission_pipeline",
                is_read_only=False,
                operation={
                    "product": "ros",
                    "action": "CreateStack",
                    "region": "cn-hangzhou",
                    "stackName": "demo-vswitch-stack",
                },
            ),
        ),
    )

    envelope = permission_input_envelope(request, task_id="task-1", context_id="ctx-1")

    assert envelope["title"] == "Create ROS stack"
    assert envelope["effect"] == "cloud_change"
    assert envelope["target"] == "ros CreateStack in cn-hangzhou; stack demo-vswitch-stack"
    assert envelope["isReadOnly"] is False


def test_ros_deployment_permission_is_localized_and_preserves_safe_plan_summary() -> None:
    request = PermissionRequestEvent(
        tool_name="ros_deploy",
        tool_input={"parameters": {"Password": "must-not-leak"}},
        tool_use_id="tool-deploy",
        permission_result=PermissionResult(
            behavior="ask",
            audit=PermissionAuditMetadata(
                scope="once",
                source="permission_pipeline",
                is_read_only=False,
                operation={
                    "product": "ros",
                    "action": "CreateStack",
                    "region": "cn-hangzhou",
                    "stackName": "demo-stack",
                    "deploymentSummary": {
                        "candidateName": "低成本方案",
                        "region": "cn-hangzhou",
                        "stackName": "demo-stack",
                        "template": "templates/demo.yml",
                        "totalMonthlyCost": "¥88/月",
                        "resources": [
                            {"name": "ECS", "spec": "2 vCPU / 4 GiB", "monthlyCost": "¥88/月"}
                        ],
                    },
                },
            ),
        ),
    )

    with a2a_request_context(preferred_language="zh"):
        envelope = permission_input_envelope(request, task_id="task-1", context_id="ctx-1")

    assert envelope["title"] == "创建 ROS 资源栈"
    assert envelope["prompt"] == "是否允许本次操作：创建 ROS 资源栈？"
    assert envelope["options"] == [
        {"id": "allow_once", "label": "本次允许"},
        {"id": "deny", "label": "拒绝"},
    ]
    assert envelope["deploymentSummary"]["candidateName"] == "低成本方案"
    assert "预计月费用：¥88/月" in envelope["safeSummary"]
    assert "must-not-leak" not in envelope["safeSummary"]


def test_unknown_bash_permission_is_not_mislabeled_as_read_only() -> None:
    request = PermissionRequestEvent(
        tool_name="bash",
        tool_input={"command": "python helper.py"},
        tool_use_id="tool-1",
        permission_result=PermissionResult(
            behavior="ask",
            audit=PermissionAuditMetadata(
                scope="once",
                source="permission_pipeline",
                is_read_only=False,
                operation={"is_read_only": False},
            ),
        ),
    )
    envelope = permission_input_envelope(request, task_id="task-1", context_id="ctx-1")

    assert envelope["title"] == "Run a local shell command"
    assert envelope["purpose"] == "Execute a local command needed for the requested infrastructure task."
    assert envelope["effect"] == "local_execution"
    assert envelope["target"] == "the current local workspace; command: python helper.py"
    assert envelope["isReadOnly"] is False


@pytest.mark.asyncio
async def test_pipeline_permission_uses_same_input_envelope_and_waits_serially(monkeypatch, tmp_path) -> None:
    registry = PermissionInputRegistry()
    queue = FakeEventQueue()
    before_enqueue: list[dict[str, object]] = []

    async def record_before_enqueue(envelope):
        before_enqueue.append(dict(envelope))
        return True

    publisher = PipelineA2AEventPublisher(
        event_queue=queue,
        translator=PipelineEventTranslator(
            PipelineA2AContext(
                pipeline_run_id="run-1",
                task_id="task-1",
                context_id="ctx-1",
                pipeline_name="selling",
            )
        ),
        journal=A2APipelineJournal(tmp_path / "pipeline"),
        snapshot_store=A2APipelineSnapshotStore(tmp_path / "pipeline"),
        permission_input_registry=registry,
        before_enqueue=record_before_enqueue,
    )
    future = pending_future()
    request = PermissionRequestEvent(
        tool_name="bash",
        tool_input={"cmd": "rm /tmp/demo"},
        tool_use_id="tool-1",
        response_future=future,
    )
    monkeypatch.setattr("iac_code.a2a.input_required.emit_permission_boundary_audit", lambda *_args, **_kwargs: True)
    publishing = asyncio.create_task(publisher.publish(request))
    for _ in range(20):
        if queue.events:
            break
        await asyncio.sleep(0)
    dumped = MessageToDict(queue.events[0], preserving_proto_field_name=False)
    assert dumped["status"]["state"] == "TASK_STATE_INPUT_REQUIRED"
    assert dumped["metadata"]["iac_code"]["input"]["kind"] == "permission"
    assert dumped["metadata"]["iac_code"]["input"]["safeSummary"] == 'bash: {"cmd":"rm /tmp/demo"}'
    assert dumped["metadata"]["iac_code"]["input"]["title"] == "Run a local shell command"
    assert dumped["metadata"]["iac_code"]["input"]["effect"] == "unknown"
    assert dumped["metadata"]["iac_code"]["input"]["target"] == "the current local workspace; command: rm /tmp/demo"
    assert dumped["metadata"]["iac_code"]["input"]["isReadOnly"] is False
    pipeline_envelope = dumped["metadata"]["iac_code"]["pipeline"]
    assert pipeline_envelope["eventType"] == "permission_requested"
    assert "input" not in pipeline_envelope
    assert before_enqueue[0]["status"] == "input_required"

    parsed = parse_permission_response(
        _permission_message(decision="deny", input_id=dumped["metadata"]["iac_code"]["input"]["inputId"])
    )
    assert parsed is not None
    assert await registry.answer(parsed) is False
    await publishing
    assert future.result() is False


@pytest.mark.asyncio
async def test_sub_pipeline_permissions_stay_working_and_resolve_independently(monkeypatch, tmp_path) -> None:
    registry = PermissionInputRegistry()
    store = A2ATaskStore()
    await store.save(
        Task(id="task-1", context_id="ctx-1", status=TaskStatus(state=TaskState.TASK_STATE_WORKING))
    )
    queue = FakeEventQueue()
    publisher = PipelineA2AEventPublisher(
        event_queue=queue,
        translator=PipelineEventTranslator(
            PipelineA2AContext(
                pipeline_run_id="run-1",
                task_id="task-1",
                context_id="ctx-1",
                pipeline_name="selling",
            )
        ),
        journal=A2APipelineJournal(tmp_path / "pipeline-sideband"),
        snapshot_store=A2APipelineSnapshotStore(tmp_path / "pipeline-sideband"),
        permission_input_registry=registry,
        task_store=store,
    )
    monkeypatch.setattr("iac_code.a2a.pipeline_stream.emit_permission_boundary_audit", lambda *_args, **_kwargs: True)
    futures = [pending_future(), pending_future()]
    with a2a_request_context(preferred_language="zh"):
        for candidate_index, future in enumerate(futures):
            await publisher.publish_sub_pipeline_permission(
                SubPipelineStreamEvent(
                    sub_pipeline_id="candidate-{}".format(candidate_index),
                    candidate_index=candidate_index,
                    inner=PermissionRequestEvent(
                        tool_name="bash",
                        tool_input={"cmd": "pwd"},
                        tool_use_id="same-tool-use-id",
                        response_future=future,
                    ),
                )
            )

    requests = [
        MessageToDict(event, preserving_proto_field_name=False)["metadata"]["iac_code"]["input"]
        for event in queue.events
    ]
    assert [request["language"] for request in requests] == ["zh", "zh"]
    assert all(event.status.state == TaskState.TASK_STATE_WORKING for event in queue.events)
    assert requests[0]["inputId"] != requests[1]["inputId"]
    assert not any(future.done() for future in futures)
    task = await store.get("task-1")
    assert task is not None
    task_metadata = MessageToDict(task.metadata, preserving_proto_field_name=False)
    assert len(task_metadata["iac_code"]["pendingPermissions"]) == 2

    response = PermissionResponse(
        task_id="task-1",
        context_id="ctx-1",
        request_task_id="task-1",
        input_id=requests[0]["inputId"],
        tool_use_id="same-tool-use-id",
        decision="allow_once",
    )
    executor = IacCodeA2AExecutor(task_store=store, model="qwen3.6-plus", permission_input_registry=registry)
    ack = await executor.resolve_sideband_permission(response)
    assert ack is not None
    ack_payload = MessageToDict(ack, preserving_proto_field_name=False)
    assert ack_payload["role"] == "ROLE_AGENT"
    assert ack_payload["parts"][0]["data"]["kind"] == "permission_ack"
    assert ack_payload["parts"][0]["data"]["inputId"] == requests[0]["inputId"]
    assert futures[0].result() is True
    assert not futures[1].done()
    journal = publisher.journal.read_all()
    assert journal[-1]["eventType"] == "permission_resolved"
    assert journal[-1]["candidate"]["index"] == 0
    snapshot = publisher.snapshot_store.load()
    assert snapshot is not None
    resolved_display = next(
        item for item in snapshot["display"]["permissions"] if item["inputId"] == requests[0]["inputId"]
    )
    assert resolved_display["pending"] is False
    task = await store.get("task-1")
    assert task is not None
    task_metadata = MessageToDict(task.metadata, preserving_proto_field_name=False)
    assert [item["inputId"] for item in task_metadata["iac_code"]["pendingPermissions"]] == [
        requests[1]["inputId"]
    ]
    remaining = task_metadata["iac_code"]["pendingPermissions"][0]
    assert remaining["language"] == "zh"
    assert remaining["prompt"] == "是否允许本次操作：运行本地 Shell 命令？"
    assert remaining["options"] == [
        {"id": "allow_once", "label": "本次允许"},
        {"id": "deny", "label": "拒绝"},
    ]

    await registry.cancel_task("task-1")
    assert futures[1].result() is False
    assert publisher.journal.read_all()[-1]["permission"]["canceled"] is True
    task = await store.get("task-1")
    assert task is not None
    task_metadata = MessageToDict(task.metadata, preserving_proto_field_name=False)
    assert "pendingPermissions" not in task_metadata.get("iac_code", {})


@pytest.mark.asyncio
async def test_task_cancel_latch_rejects_permission_registered_during_owner_drain(monkeypatch) -> None:
    registry = PermissionInputRegistry()
    cancel_entered = asyncio.Event()
    release_cancel = asyncio.Event()

    class BlockingOwner:
        async def resolve_permission(self, pending, response) -> bool:
            raise AssertionError("permission response must not win after task cancellation starts")

        async def fail_permission(self, pending) -> None:
            await registry.complete(pending)

        async def cancel_permissions(self, task_id: str) -> None:
            claimed = await registry.claim_for_cancel(task_id, self)
            cancel_entered.set()
            await release_cancel.wait()
            for pending in claimed:
                future = pending.request.response_future
                if future is not None and not future.done():
                    future.set_result(False)
                await registry.complete(pending)

    monkeypatch.setattr("iac_code.a2a.input_required.emit_permission_boundary_audit", lambda *_args, **_kwargs: True)
    owner = BlockingOwner()
    first_future = pending_future()
    await registry.register(
        PermissionRequestEvent(
            tool_name="bash",
            tool_input={"cmd": "pwd"},
            tool_use_id="tool-1",
            response_future=first_future,
        ),
        task_id="task-1",
        context_id="ctx-1",
        resolution_owner=owner,
    )

    cancellation = asyncio.create_task(registry.cancel_task("task-1"))
    await cancel_entered.wait()
    late_future = pending_future()
    with pytest.raises(InvalidParamsError, match="cancellation is already in progress"):
        await registry.register(
            PermissionRequestEvent(
                tool_name="bash",
                tool_input={"cmd": "ls"},
                tool_use_id="tool-2",
                response_future=late_future,
            ),
            task_id="task-1",
            context_id="ctx-1",
            resolution_owner=owner,
        )
    assert late_future.result() is False

    release_cancel.set()
    await cancellation
    assert first_future.result() is False


@pytest.mark.asyncio
async def test_reversible_terminal_token_cannot_reopen_later_permanent_cancel(monkeypatch) -> None:
    registry = PermissionInputRegistry()
    terminal_token = await registry.cancel_task("task-1", reversible=True)
    await registry.cancel_task("task-1")
    await registry.reopen_task(terminal_token)
    monkeypatch.setattr("iac_code.a2a.input_required.emit_permission_boundary_audit", lambda *_args, **_kwargs: True)
    future = pending_future()

    with pytest.raises(InvalidParamsError, match="cancellation is already in progress"):
        await registry.register(
            PermissionRequestEvent(
                tool_name="bash",
                tool_input={"cmd": "pwd"},
                tool_use_id="tool-1",
                response_future=future,
            ),
            task_id="task-1",
            context_id="ctx-1",
            resolution_owner=object(),
        )
    assert future.result() is False


def test_existing_pipeline_inputs_get_unified_projection_without_mutating_legacy_envelope() -> None:
    envelope = {
        "eventId": "evt-1",
        "eventType": "input_required",
        "taskId": "task-1",
        "contextId": "ctx-1",
        "status": "input_required",
        "input": {
            "kind": "candidate_selection",
            "inputId": "selection-1",
            "prompt": "Choose a plan",
            "options": [{"name": "Plan A", "candidate_index": 0}],
        },
    }
    original = dict(envelope["input"])
    projected = _unified_input_projection(envelope)
    assert projected == {
        "schemaVersion": 1,
        "kind": "candidate_selection",
        "requestTaskId": "task-1",
        "contextId": "ctx-1",
        "inputId": "selection-1",
        "prompt": "Choose a plan",
        "required": True,
        "options": [{"id": "0", "label": "Plan A"}],
    }
    assert envelope["input"] == original


def test_candidate_selection_projection_preserves_solution_presentation() -> None:
    envelope = {
        "eventId": "evt-rich",
        "eventType": "input_required",
        "taskId": "task-1",
        "contextId": "ctx-1",
        "status": "input_required",
        "input": {
            "kind": "candidate_selection",
            "inputId": "selection-rich",
            "prompt": "请选择方案",
            "options": [
                {
                    "name": "方案 A",
                    "candidate_index": 2,
                    "summary": "单 ECS 低成本方案。",
                    "architecture_diagram": "flowchart LR\nU[用户] --> E[ECS]",
                    "total_monthly_cost": "¥88/月",
                    "cost_items": [{"name": "ECS", "spec": "2核4G", "monthly_cost": "¥88/月"}],
                }
            ],
        },
    }

    projected = _unified_input_projection(envelope)

    assert projected is not None
    assert projected["options"] == [
        {
            "id": "2",
            "label": "方案 A",
            "summary": "单 ECS 低成本方案。",
            "architectureDiagram": "flowchart LR\nU[用户] --> E[ECS]",
            "totalMonthlyCost": "¥88/月",
            "costItems": [{"name": "ECS", "spec": "2核4G", "monthlyCost": "¥88/月"}],
        }
    ]


def test_legacy_pending_permission_gets_conservative_display_fallback() -> None:
    envelope = {
        "eventId": "evt-1",
        "eventType": "permission_requested",
        "taskId": "task-1",
        "contextId": "ctx-1",
        "status": "input_required",
        "permission": {
            "pending": True,
            "inputId": "permission-task-1-tool-1",
            "toolUseId": "tool-1",
            "toolName": "bash",
            "safeSummary": "bash: pwd",
        },
    }
    projected = _unified_input_projection(envelope)

    assert projected is not None
    assert projected["title"] == "Run bash"
    assert projected["purpose"] == "Run this operation for the requested infrastructure task."
    assert projected["effect"] == "unknown"
    assert projected["target"] == "the current task scope"
    assert projected["isReadOnly"] is False


def test_candidate_selection_projection_can_use_runtime_step_ui_mode_without_mutating_envelope() -> None:
    envelope = {
        "eventId": "evt-1",
        "eventType": "input_required",
        "taskId": "task-1",
        "contextId": "ctx-1",
        "status": "input_required",
        "data": {
            "prompt": "Choose a plan",
            "options": [{"name": "Plan A", "candidate_index": 0}],
        },
    }
    original = dict(envelope)
    projected = _unified_input_projection(envelope, kind_hint="candidate_selection")
    assert projected is not None
    assert projected["kind"] == "candidate_selection"
    assert projected["options"] == [{"id": "0", "label": "Plan A"}]
    assert envelope == original


def test_ask_question_projection_preserves_free_text_contract() -> None:
    envelope = {
        "eventId": "evt-1",
        "eventType": "input_required",
        "taskId": "task-1",
        "contextId": "ctx-1",
        "status": "input_required",
        "input": {
            "kind": "ask_user_question",
            "inputId": "question-1",
            "prompt": "Choose or describe",
            "allowFreeText": True,
            "freeTextPrompt": "Describe the custom region",
            "options": [{"id": "cn-hangzhou", "label": "Hangzhou"}],
        },
    }
    projected = _unified_input_projection(envelope)
    assert projected is not None
    assert projected["allowFreeText"] is True
    assert projected["freeTextPrompt"] == "Describe the custom region"


def test_pipeline_publisher_uses_context_ui_mode_only_for_unified_projection(tmp_path) -> None:
    context = PipelineA2AContext(
        pipeline_run_id="run-1",
        task_id="task-1",
        context_id="ctx-1",
        pipeline_name="selling",
        parent_step_ui_modes={"confirm_and_select": "candidate_selection"},
    )
    publisher = PipelineA2AEventPublisher(
        event_queue=FakeEventQueue(),
        translator=PipelineEventTranslator(context),
        journal=A2APipelineJournal(tmp_path / "pipeline"),
        snapshot_store=A2APipelineSnapshotStore(tmp_path / "pipeline"),
    )
    envelope = {
        "eventId": "evt-1",
        "eventType": "input_required",
        "taskId": "task-1",
        "contextId": "ctx-1",
        "status": "input_required",
        "step": {"id": "confirm_and_select"},
        "data": {"prompt": "Choose", "options": [{"name": "Plan A", "candidate_index": 0}]},
    }
    projected = publisher._unified_input_projection(envelope)
    assert projected is not None and projected["kind"] == "candidate_selection"
    assert "kind" not in envelope["data"]
