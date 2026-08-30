from __future__ import annotations

import asyncio
import json
import sys

import pytest
from a2a.types import Message, Part, Role, Task, TaskState, TaskStatus
from a2a.utils.errors import InvalidParamsError
from google.protobuf.json_format import MessageToDict
from google.protobuf.struct_pb2 import Value

from iac_code.a2a.events import publish_stream_event
from iac_code.a2a.executor import IacCodeA2AExecutor
from iac_code.a2a.input_required import (
    PERMISSION_QUERY_PREFIX,
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
from iac_code.services.permission_wait import (
    PermissionWaitCheckpointStore,
    PermissionWaitCoordinator,
    PermissionWaitPolicy,
    build_permission_checkpoint,
)
from iac_code.services.session_storage import SessionStorage
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


def _text_permission_message(
    *,
    decision: str = "allow_once",
    context_id: str = "ctx-1",
    extra_part: bool = False,
    extra_payload: dict[str, object] | None = None,
    include_task_id: bool = True,
) -> Message:
    payload: dict[str, object] = {
        "schemaVersion": 1,
        "kind": "permission",
        "requestTaskId": "task-1",
        "contextId": context_id,
        "inputId": "permission-task-1-tool-1",
        "toolUseId": "tool-1",
        "decision": decision,
    }
    payload.update(extra_payload or {})
    parts = [Part(text="{} {}".format(PERMISSION_QUERY_PREFIX, json.dumps(payload)))]
    if extra_part:
        parts.append(Part(text="also allow"))
    message = Message(
        message_id="message-1",
        context_id="ctx-1",
        role=Role.ROLE_USER,
        parts=parts,
    )
    if include_task_id:
        message.task_id = "task-1"
    return message


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


def test_permission_parser_accepts_exact_json_text_part_for_text_only_gateways() -> None:
    response = parse_permission_response(_text_permission_message(include_task_id=False))

    assert response is not None
    assert response.task_id == "task-1"
    assert response.context_id == "ctx-1"
    assert response.input_id == "permission-task-1-tool-1"
    assert response.decision == "allow_once"


def test_json_text_permission_response_fails_closed_on_schema_or_correlation_mismatch() -> None:
    with pytest.raises(InvalidParamsError, match="exactly one JSON TextPart"):
        parse_permission_response(_text_permission_message(extra_part=True))
    with pytest.raises(InvalidParamsError, match="allow_once or deny"):
        parse_permission_response(_text_permission_message(decision="always"))
    with pytest.raises(InvalidParamsError, match="payload fields"):
        parse_permission_response(_text_permission_message(extra_payload={"unexpected": "value"}))
    with pytest.raises(InvalidParamsError, match="contextId"):
        parse_permission_response(_text_permission_message(context_id="ctx-other"))


def test_non_control_text_continues_to_generic_input_path() -> None:
    for text in (
        "allow",
        '{"kind":"permission"}',
        'prefix {"kind":"permission"}',
        ' IAC_CODE_PERMISSION: {"kind":"permission"}',
    ):
        message = Message(
            message_id="message-1",
            task_id="task-1",
            context_id="ctx-1",
            role=Role.ROLE_USER,
            parts=[Part(text=text)],
        )
        assert parse_permission_response(message) is None


def test_non_prefixed_text_does_not_attempt_json_decode(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail_json_loads(_value: str):
        raise AssertionError("ordinary query must not enter JSON decoding")

    monkeypatch.setattr("iac_code.a2a.input_required.json.loads", fail_json_loads)
    message = Message(
        message_id="message-1",
        task_id="task-1",
        context_id="ctx-1",
        role=Role.ROLE_USER,
        parts=[Part(text='ordinary query containing {"kind":"permission"}')],
    )

    assert parse_permission_response(message) is None


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


@pytest.mark.asyncio
async def test_concurrent_duplicate_normal_answers_claim_live_continuation_once(monkeypatch, tmp_path) -> None:
    registry = PermissionInputRegistry()
    coordinator = PermissionWaitCoordinator(PermissionWaitPolicy())
    registry.set_permission_wait_coordinator(coordinator)
    future = pending_future()
    request = PermissionRequestEvent(
        tool_name="aliyun_api",
        tool_input={"product": "ros", "action": "CreateStack"},
        tool_use_id="tool-1",
        response_future=future,
    )
    pending = await registry.register(request, task_id="task-1", context_id="ctx-1", scope="normal")
    SessionStorage().ensure_v2_session_dir_for_new_session(str(tmp_path), "session-1")
    checkpoint_store = PermissionWaitCheckpointStore(str(tmp_path), "session-1")
    record = checkpoint_store.create(
        build_permission_checkpoint(
            session_id="session-1",
            task_id="task-1",
            context_id="ctx-1",
            input_id=pending.input_id,
            tool_use_id="tool-1",
            tool_name="aliyun_api",
            tool_input=request.tool_input,
            permission_class="normal",
            continuation_frame={
                "assistantMessageRef": "session.jsonl:0",
                "assistantMessageDigest": "a" * 64,
                "orderedToolUseIds": ["tool-1"],
                "currentIndex": 0,
                "decisions": [{"toolUseId": "tool-1", "state": "pending", "source": None, "deniedResult": None}],
            },
            policy=PermissionWaitPolicy(),
        )
    )
    pending.boundary_id = record["boundaryId"]
    pending.checkpoint_store = checkpoint_store
    registry.activate_durable_boundary(pending, record)
    monkeypatch.setattr("iac_code.a2a.input_required.emit_permission_boundary_audit", lambda *_a, **_k: True)

    continuation_calls = 0

    async def continuation() -> None:
        nonlocal continuation_calls
        continuation_calls += 1
        await asyncio.sleep(0)

    pending.continuation = continuation
    response = PermissionResponse(
        task_id="task-1",
        context_id="ctx-1",
        request_task_id="task-1",
        input_id=pending.input_id,
        tool_use_id="tool-1",
        decision="allow_once",
    )

    async def answer_and_continue() -> bool:
        approved = await registry.answer(response)
        claimed = await registry.claim_continuation(pending)
        if claimed is not None:
            await claimed()
        return approved

    assert await asyncio.gather(answer_and_continue(), answer_and_continue()) == [True, True]
    assert continuation_calls == 1
    assert checkpoint_store.load(record["boundaryId"])["decision"]["status"] == "applied"
    await registry.complete(pending)


@pytest.mark.asyncio
async def test_top_pipeline_durable_boundary_persists_canonical_transcript_reference(tmp_path) -> None:
    registry = PermissionInputRegistry()
    registry.set_permission_wait_coordinator(PermissionWaitCoordinator(PermissionWaitPolicy()))
    SessionStorage().ensure_v2_session_dir_for_new_session(str(tmp_path), "session-pipeline-ref")
    request = PermissionRequestEvent(
        tool_name="aliyun_api",
        tool_input={"action": "CreateStack"},
        tool_use_id="tool-1",
        response_future=pending_future(),
        audit_context={
            "root_session_id": "session-pipeline-ref",
            "transcript_id": "transcript_att_0001",
        },
        continuation_frame={
            "assistantMessageRef": "session.jsonl:2",
            "assistantMessageDigest": "a" * 64,
            "orderedToolUseIds": ["tool-1"],
            "currentIndex": 0,
            "decisions": [{"toolUseId": "tool-1", "state": "pending", "source": None}],
        },
    )
    pending = await registry.register(request, task_id="task-1", context_id="ctx-1", scope="pipeline")

    record = await registry.open_durable_boundary(
        pending,
        cwd=str(tmp_path),
        session_id="session-pipeline-ref",
        permission_class="pipeline",
        backup_service=None,
        perform_backup=False,
    )

    assert record["continuationFrame"]["assistantMessageRef"] == (
        "pipeline/transcripts/transcript_att_0001/session.jsonl:2"
    )


@pytest.mark.asyncio
async def test_normal_successor_releases_old_registry_owner_before_publication(monkeypatch, tmp_path) -> None:
    class BackupService:
        def backup_session(self, *_args, **_kwargs) -> None:
            return None

    registry = PermissionInputRegistry()
    coordinator = PermissionWaitCoordinator(PermissionWaitPolicy())
    registry.set_permission_wait_coordinator(coordinator)
    SessionStorage().ensure_v2_session_dir_for_new_session(str(tmp_path), "session-successor")
    common = {
        "assistantMessageRef": "session.jsonl:0",
        "assistantMessageDigest": "a" * 64,
        "orderedToolUseIds": ["tool-1", "tool-2"],
    }
    first_request = PermissionRequestEvent(
        tool_name="aliyun_api",
        tool_input={"action": "CreateStack"},
        tool_use_id="tool-1",
        response_future=pending_future(),
        continuation_frame={
            **common,
            "currentIndex": 0,
            "decisions": [
                {"toolUseId": "tool-1", "state": "pending", "source": None, "deniedResult": None},
                {"toolUseId": "tool-2", "state": "not_evaluated", "source": None, "deniedResult": None},
            ],
        },
    )
    first = await registry.register(first_request, task_id="task-1", context_id="ctx-1", scope="normal")
    first_record = await registry.open_durable_boundary(
        first,
        cwd=str(tmp_path),
        session_id="session-successor",
        permission_class="normal",
        backup_service=BackupService(),
        perform_backup=False,
    )
    registry.activate_durable_boundary(first, first_record)
    monkeypatch.setattr("iac_code.a2a.input_required.emit_permission_boundary_audit", lambda *_a, **_k: True)
    first_response = PermissionResponse(
        task_id="task-1",
        context_id="ctx-1",
        request_task_id="task-1",
        input_id=first.input_id,
        tool_use_id="tool-1",
        decision="allow_once",
    )
    assert await registry.answer(first_response) is True

    second_request = PermissionRequestEvent(
        tool_name="aliyun_api",
        tool_input={"action": "DeleteStack"},
        tool_use_id="tool-2",
        response_future=pending_future(),
        continuation_frame={
            **common,
            "currentIndex": 1,
            "decisions": [
                {
                    "toolUseId": "tool-1",
                    "state": "allow",
                    "source": "user",
                    "principalRef": None,
                    "region": None,
                    "deniedResult": None,
                },
                {"toolUseId": "tool-2", "state": "pending", "source": None, "deniedResult": None},
            ],
            "previousBoundaryId": first_record["boundaryId"],
        },
    )
    second = await registry.register(second_request, task_id="task-1", context_id="ctx-1", scope="normal")
    await registry.open_durable_boundary(
        second,
        cwd=str(tmp_path),
        session_id="session-successor",
        permission_class="normal",
        backup_service=BackupService(),
        perform_backup=False,
    )

    assert coordinator.has_live_boundary(first_record["boundaryId"]) is False
    with pytest.raises(InvalidParamsError, match="pending permission"):
        await registry.pending_for_response(first_response)
    receipt = PermissionWaitCheckpointStore(str(tmp_path), "session-successor").load(first_record["boundaryId"])
    assert receipt["phase"] == "RESOLVED"
    assert receipt["ack"]["nextBoundaryId"] == second.boundary_id


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
    assert envelope["operation"] == {
        "product": "vpc",
        "action": "DescribeVpcs",
        "region": "cn-hangzhou",
        "apiCalls": [{"product": "VPC", "action": "DescribeVpcs", "effect": "read"}],
    }


def test_aliyun_api_change_exposes_original_safe_parameters_and_redacts_secrets() -> None:
    request = PermissionRequestEvent(
        tool_name="aliyun_api",
        tool_input={
            "product": "ecs",
            "action": "RunInstances",
            "region_id": "cn-hangzhou",
            "params": {
                "InstanceType": "ecs.g7.large",
                "Amount": 2,
                "SystemDisk": {"Size": 40, "Encrypted": True},
                "Password": "must-not-leak",
                "Tags": [{"Key": "team", "Value": "platform"}, {"Token": "hidden"}],
            },
            "headers": {"Authorization": "must-not-leak-either"},
        },
        tool_use_id="tool-change",
        permission_result=PermissionResult(
            behavior="ask",
            audit=PermissionAuditMetadata(
                scope="once",
                source="permission_pipeline",
                is_read_only=False,
                operation={"product": "ecs", "action": "RunInstances", "region": "cn-hangzhou"},
            ),
        ),
    )

    envelope = permission_input_envelope(request, task_id="task-1", context_id="ctx-1")

    assert envelope["operation"]["apiCalls"] == [{"product": "ECS", "action": "RunInstances", "effect": "change"}]
    assert envelope["displayParameters"] == {
        "format": "json",
        "value": {
            "InstanceType": "ecs.g7.large",
            "Amount": 2,
            "SystemDisk": {"Size": 40, "Encrypted": True},
            "Password": {"redacted": True},
            "Tags": [{"Key": "team", "Value": "platform"}, {"Token": {"redacted": True}}],
        },
    }
    rendered = json.dumps(envelope, ensure_ascii=False)
    assert "must-not-leak" not in rendered
    assert "Authorization" not in rendered


def test_aliyun_api_roa_parameters_keep_request_layers_but_never_headers() -> None:
    request = PermissionRequestEvent(
        tool_name="aliyun_api",
        tool_input={
            "product": "example",
            "action": "UpdateThing",
            "region_id": "cn-shanghai",
            "pathname": "/things/thing-1",
            "query": {"DryRun": False},
            "body": {"Name": "demo", "access_token": "hidden"},
            "headers": {"Cookie": "hidden"},
        },
        tool_use_id="tool-roa",
        permission_result=PermissionResult(
            behavior="ask",
            audit=PermissionAuditMetadata(
                scope="once",
                source="permission_pipeline",
                is_read_only=False,
                operation={"product": "example", "action": "UpdateThing", "region": "cn-shanghai"},
            ),
        ),
    )

    envelope = permission_input_envelope(request, task_id="task-1", context_id="ctx-1")

    assert envelope["displayParameters"]["value"] == {
        "pathname": "/things/thing-1",
        "query": {"DryRun": False},
        "body": {"Name": "demo", "access_token": {"redacted": True}},
    }
    assert "headers" not in envelope["displayParameters"]["value"]


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
    assert envelope["target"] == "demo-vswitch-stack · cn-hangzhou"
    assert envelope["isReadOnly"] is False
    assert envelope["operation"]["apiCalls"] == [{"product": "ROS", "action": "CreateStack", "effect": "change"}]
    assert envelope["displayParameters"]["value"] == {"StackName": "demo-vswitch-stack"}


def test_ros_tool_operation_infers_ros_product_without_audit_metadata() -> None:
    request = PermissionRequestEvent(
        tool_name="ros_stack",
        tool_input={
            "action": "UpdateStack",
            "region_id": "cn-hangzhou",
            "stack_name": "permission-handoff-stack",
            "params": {"Password": "secret"},
        },
        tool_use_id="tool-stack",
    )

    envelope = permission_input_envelope(request, task_id="task-1", context_id="ctx-1")

    assert envelope["operation"] == {
        "product": "ros",
        "action": "UpdateStack",
        "region": "cn-hangzhou",
        "target": {"type": "resource", "name": "permission-handoff-stack"},
        "apiCalls": [{"product": "ROS", "action": "UpdateStack", "effect": "change"}],
    }
    assert envelope["displayParameters"]["value"] == {"Password": {"redacted": True}}
    assert envelope["target"] == "permission-handoff-stack · cn-hangzhou"


@pytest.mark.parametrize(
    ("deploy_action", "audit_action", "expected_calls"),
    [
        ("create", "CreateStack", [{"product": "ROS", "action": "CreateStack", "effect": "change"}]),
        (
            "continue_create",
            "ContinueCreateStack",
            [{"product": "ROS", "action": "ContinueCreateStack", "effect": "change"}],
        ),
        (
            "delete_and_create",
            "CreateStack",
            [
                {"product": "ROS", "action": "DeleteStack", "effect": "change"},
                {"product": "ROS", "action": "CreateStack", "effect": "change"},
            ],
        ),
        (
            "wait",
            "GetStackStatus",
            [{"product": "ROS", "action": "GetStack", "effect": "read", "repeat": "polling"}],
        ),
    ],
)
def test_ros_deploy_projects_the_real_api_sequence(
    deploy_action: str,
    audit_action: str,
    expected_calls: list[dict[str, str]],
) -> None:
    request = PermissionRequestEvent(
        tool_name="ros_deploy",
        tool_input={
            "action": deploy_action,
            "stack_id": "old-stack-id",
            "stack_name": "new-stack",
            "region_id": "cn-hangzhou",
            "parameters": {"Environment": "production"},
        },
        tool_use_id="tool-deploy",
        permission_result=PermissionResult(
            behavior="ask",
            audit=PermissionAuditMetadata(
                scope="once",
                source="permission_pipeline",
                is_read_only=deploy_action == "wait",
                operation={
                    "product": "ros",
                    "action": audit_action,
                    "deployAction": deploy_action,
                    "region": "cn-hangzhou",
                    "stackId": "old-stack-id",
                    "stackName": "new-stack",
                },
            ),
        ),
    )

    envelope = permission_input_envelope(request, task_id="task-1", context_id="ctx-1")

    assert envelope["operation"]["action"] == deploy_action
    assert envelope["operation"]["apiCalls"] == expected_calls
    if deploy_action == "wait":
        assert not any(call["effect"] == "change" for call in expected_calls)


def test_every_ros_lifecycle_action_projects_its_canonical_effect() -> None:
    tool_actions = {
        "ros_stack": ({"CreateStack", "UpdateStack", "ContinueCreateStack", "DeleteStack"}, set()),
        "ros_stack_instances": (
            {"CreateStackInstances", "UpdateStackInstances", "DeleteStackInstances"},
            set(),
        ),
        "ros_stack_group": (
            {
                "CreateStackGroup",
                "UpdateStackGroup",
                "DeleteStackGroup",
                "DetectStackGroupDrift",
                "StopStackGroupOperation",
                "ImportStacksToStackGroup",
            },
            {
                "GetStackGroup",
                "ListStackGroups",
                "GetStackGroupOperation",
                "ListStackGroupOperations",
                "ListStackGroupOperationResults",
            },
        ),
        "ros_template": (
            {"CreateTemplate", "UpdateTemplate", "DeleteTemplate", "SetTemplatePermission"},
            {"GetTemplate", "ListTemplates", "ListTemplateVersions"},
        ),
        "ros_template_scratch": (
            {"CreateTemplateScratch", "UpdateTemplateScratch", "DeleteTemplateScratch", "GenerateTemplateByScratch"},
            {"GetTemplateScratch", "ListTemplateScratches"},
        ),
        "ros_diagnostic": ({"CreateDiagnostic", "DeleteDiagnostic"}, {"GetDiagnostic", "ListDiagnostics"}),
        "ros_resource_type_registration": (
            {"RegisterResourceType", "DeregisterResourceType", "SetResourceType"},
            {
                "GetResourceType",
                "GetResourceTypeTemplate",
                "ListResourceTypes",
                "ListResourceTypeRegistrations",
                "ListResourceTypeVersions",
            },
        ),
        "ros_tag": ({"TagResources", "UntagResources"}, {"ListTagKeys", "ListTagValues", "ListTagResources"}),
    }

    projected_actions: set[str] = set()
    for tool_name, (write_actions, read_actions) in tool_actions.items():
        for action in sorted(write_actions | read_actions):
            is_read_only = action in read_actions
            request = PermissionRequestEvent(
                tool_name=tool_name,
                tool_input={"action": action, "params": {}, "region_id": "cn-hangzhou"},
                tool_use_id="{}-{}".format(tool_name, action),
                permission_result=PermissionResult(
                    behavior="ask",
                    audit=PermissionAuditMetadata(
                        scope="once",
                        source="permission_pipeline",
                        is_read_only=is_read_only,
                        operation={"product": "ros", "action": action, "region": "cn-hangzhou"},
                    ),
                ),
            )
            envelope = permission_input_envelope(request, task_id="task-1", context_id="ctx-1")
            assert envelope["operation"]["apiCalls"] == [
                {"product": "ROS", "action": action, "effect": "read" if is_read_only else "change"}
            ]
            assert "displayParameters" not in envelope
            projected_actions.add(action)

    assert len(projected_actions) == 48


@pytest.mark.parametrize(
    ("tool_name", "tool_input", "expected_target"),
    [
        ("read_file", {"path": "templates/main.yml"}, "templates/main.yml"),
        ("write_file", {"file_path": "templates/main.yml"}, "templates/main.yml"),
        ("edit_file", {"path": "templates/main.yml"}, "templates/main.yml"),
        ("list_files", {"path": "templates"}, "templates"),
        ("glob", {"path": "templates", "pattern": "**/*.yml"}, "templates"),
        ("grep", {"path": "templates", "pattern": "ALIYUN::ECS"}, "ALIYUN::ECS"),
        ("bash", {"cwd": "/workspace", "command": "make test"}, "make test"),
        ("web_fetch", {"url": "https://example.com/docs"}, "https://example.com/docs"),
        ("read_memory", {"name": "deployment"}, "deployment"),
        ("write_memory", {"memory_name": "deployment"}, "deployment"),
        ("task_get", {"task_id": "task-42"}, "task-42"),
        ("task_stop", {"task_id": "task-42"}, "task-42"),
        ("agent", {"subagent_type": "general", "description": "inspect templates"}, "general"),
        ("skill", {"name": "iac-aliyun", "source": "bundled"}, "iac-aliyun"),
        ("aliyun_doc_search", {"query": "ROS CreateStack"}, "ROS CreateStack"),
        ("aliyun_api_doc", {"product": "ros", "action": "CreateStack"}, "CreateStack"),
        ("ask_user_question", {"question": "Which region?"}, "Which region?"),
        ("complete_step", {"step_id": "intent_analysis"}, "intent_analysis"),
        ("show_architecture_diagram", {"template_path": "templates/main.yml"}, "templates/main.yml"),
        ("show_architecture_plan", {"candidate_name": "low-cost"}, "low-cost"),
        ("show_candidate_detail", {"candidate_index": 2}, "2"),
        ("infraguard_scan", {"template_path": "templates/main.yml"}, "templates/main.yml"),
        ("list_mcp_resources", {"server": "github"}, "github"),
        ("read_mcp_resource", {"server": "github", "uri": "repo://demo"}, "repo://demo"),
        ("mcp__github__create_issue", {"title": "bug"}, "github:create_issue"),
        ("mcp__github__authenticate", {}, "github:authenticate"),
    ],
)
def test_known_non_cloud_tools_have_decision_relevant_targets(
    tool_name: str,
    tool_input: dict[str, object],
    expected_target: str,
) -> None:
    request = PermissionRequestEvent(
        tool_name=tool_name,
        tool_input=tool_input,
        tool_use_id="tool-target",
        permission_result=PermissionResult(
            behavior="ask",
            audit=PermissionAuditMetadata(
                scope="once",
                source="permission_pipeline",
                is_read_only=tool_name.startswith(("read_", "list_")),
                operation={},
            ),
        ),
    )

    envelope = permission_input_envelope(request, task_id="task-1", context_id="ctx-1")

    assert expected_target in envelope["target"]


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
                        "resources": [{"name": "ECS", "spec": "2 vCPU / 4 GiB", "monthlyCost": "¥88/月"}],
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
        {"id": "allow_once", "label": "仅允许一次"},
        {"id": "deny", "label": "拒绝"},
    ]
    assert envelope["deploymentSummary"]["candidateName"] == "低成本方案"
    assert "预计月费用：¥88/月" in envelope["safeSummary"]
    assert "must-not-leak" not in envelope["safeSummary"]


def test_permission_display_resolves_all_supported_languages_from_catalog() -> None:
    """preferredLanguage values beyond zh/en must resolve through the messages catalog."""
    if sys.platform == "win32":
        pytest.skip("compiled message catalogs are not built on Windows CI")
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

    expected_titles = {
        "ja": "ローカル Shell コマンドを実行する",
        "es": "Ejecutar un comando de shell local",
        "fr": "Exécuter une commande shell locale",
        "de": "Einen lokalen Shell-Befehl ausführen",
        "pt": "Executar um comando de shell local",
    }
    for language, expected_title in expected_titles.items():
        with a2a_request_context(preferred_language=language):
            envelope = permission_input_envelope(request, task_id="task-1", context_id="ctx-1")
        assert envelope["language"] == language
        assert envelope["title"] == expected_title
        # Option labels must be localized too, never the English fallback.
        labels = [option["label"] for option in envelope["options"]]
        assert "Allow once" not in labels
        assert "Deny" not in labels


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
@pytest.mark.parametrize(("decision", "allowed"), [("allow_once", True), ("deny", False)])
async def test_pipeline_permission_uses_same_input_envelope_and_waits_serially(
    monkeypatch, tmp_path, decision: str, allowed: bool
) -> None:
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
        _permission_message(decision=decision, input_id=dumped["metadata"]["iac_code"]["input"]["inputId"])
    )
    assert parsed is not None
    assert await registry.answer(parsed) is allowed
    await publishing
    assert future.result() is allowed


@pytest.mark.asyncio
async def test_sub_pipeline_permissions_stay_working_and_resolve_independently(monkeypatch, tmp_path) -> None:
    registry = PermissionInputRegistry()
    store = A2ATaskStore()
    await store.save(Task(id="task-1", context_id="ctx-1", status=TaskStatus(state=TaskState.TASK_STATE_WORKING)))
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
    assert [item["inputId"] for item in task_metadata["iac_code"]["pendingPermissions"]] == [requests[1]["inputId"]]
    remaining = task_metadata["iac_code"]["pendingPermissions"][0]
    assert remaining["language"] == "zh"
    assert remaining["prompt"] == "是否允许本次操作：运行本地 Shell 命令？"
    assert remaining["options"] == [
        {"id": "allow_once", "label": "仅允许一次"},
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


def test_candidate_permission_projection_preserves_sideband_coordinates() -> None:
    envelope = {
        "eventId": "evt-candidate-permission",
        "eventType": "permission_requested",
        "taskId": "task-1",
        "contextId": "ctx-1",
        "scope": "candidate",
        "candidate": {"id": "candidate-a"},
        "status": "working",
        "permission": {
            "pending": True,
            "inputId": "permission-candidate-a",
            "toolUseId": "tool-a",
            "toolName": "bash",
            "safeSummary": "bash: pwd",
        },
    }

    projected = _unified_input_projection(envelope)

    assert projected is not None
    assert projected["scope"] == "candidate"
    assert projected["subPipelineId"] == "candidate-a"


def test_pending_pipeline_permission_projection_preserves_safe_operation_details() -> None:
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
            "toolName": "aliyun_api",
            "safeSummary": "aliyun_api: safe",
            "operation": {
                "product": "vpc",
                "action": "CreateVSwitch",
                "region": "cn-hangzhou",
                "apiCalls": [{"product": "VPC", "action": "CreateVSwitch", "effect": "change"}],
            },
            "displayParameters": {
                "format": "json",
                "value": {"VpcId": "vpc-safe", "Password": {"redacted": True}},
            },
        },
    }

    projected = _unified_input_projection(envelope)

    assert projected is not None
    assert projected["operation"]["apiCalls"] == [{"product": "VPC", "action": "CreateVSwitch", "effect": "change"}]
    assert projected["displayParameters"] == {
        "format": "json",
        "value": {"VpcId": "vpc-safe", "Password": {"redacted": True}},
    }


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
            "toolUseId": "call-question-1",
            "prompt": "Choose or describe",
            "allowFreeText": True,
            "freeTextPrompt": "Describe the custom region",
            "options": [{"id": "cn-hangzhou", "label": "Hangzhou"}],
        },
    }
    projected = _unified_input_projection(envelope)
    assert projected is not None
    assert projected["toolUseId"] == "call-question-1"
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
