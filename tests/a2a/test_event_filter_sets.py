from __future__ import annotations

import asyncio
import json

import pytest
from a2a.server.context import ServerCallContext
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.types import (
    Artifact,
    Message,
    Part,
    Role,
    SendMessageRequest,
    SubscribeToTaskRequest,
    Task,
    TaskArtifactUpdateEvent,
    TaskState,
    TaskStatus,
    TaskStatusUpdateEvent,
)
from a2a.utils.errors import InvalidParamsError
from google.protobuf.json_format import MessageToDict, ParseDict
from starlette.testclient import TestClient

from iac_code.a2a.app import create_app
from iac_code.a2a.event_filter_sets import (
    EVENT_FILTER_SETS,
    GATEWAY_REQUIRED_PIPELINE_EVENTS,
    NONE,
    SKILL,
    USAGE,
    A2AEventDeliveryFilter,
    A2AEventFilterSet,
    filter_set_message_request,
    message_event_filter_set,
    resolve_event_filter_set,
    task_event_filter_set,
)
from iac_code.a2a.task_store import A2ATaskStore
from iac_code.a2a.transports.dispatcher import IacCodeRequestHandler
from iac_code.types.stream_events import (
    MessageEndEvent,
    TextDeltaEvent,
    ThinkingDeltaEvent,
    ToolInputDeltaEvent,
    ToolResultEvent,
    ToolUseEndEvent,
    ToolUseStartEvent,
    Usage,
)

from .fakes import FakeAgentLoop, FakeRuntime

ACTIVE_FILTER_SETS = [USAGE, SKILL]
INTERACTION_AND_RECOVERY_EVENTS = {
    "input_required",
    "input_received",
    "permission_requested",
    "permission_resolved",
    "pipeline_completed",
    "pipeline_failed",
    "pipeline_canceled",
    "pipeline_handoff_ready",
    "backup_blocked",
    "backup_committed",
}


def update(iac_code=None, *, state=TaskState.TASK_STATE_WORKING, text=None, extra_metadata=None):
    event = TaskStatusUpdateEvent(task_id="task-1", context_id="ctx-1", status=TaskStatus(state=state))
    metadata = dict(extra_metadata or {})
    if iac_code is not None:
        metadata["iac_code"] = iac_code
    if metadata:
        ParseDict(metadata, event.metadata)
    if text:
        event.status.message.CopyFrom(Message(role=Role.ROLE_AGENT, parts=[Part(text=text)]))
    return event


def active_filter(name):
    delivery = A2AEventDeliveryFilter(EVENT_FILTER_SETS[name])
    assert delivery.project(Task(id="task-1", context_id="ctx-1")) is not None
    return delivery


def data(event):
    return MessageToDict(event, preserving_proto_field_name=False)


def test_default_none_preserves_the_original_event_object():
    event = update({"usage": {"inputTokens": 12}, "futureExtension": {"arbitrary": True}})
    assert resolve_event_filter_set().name == NONE
    assert A2AEventDeliveryFilter(resolve_event_filter_set()).project(event) is event


@pytest.mark.parametrize("value", ["unknown", "all", "ALL", "ROS_AI_ASSISTANT", "", 123, True, [], {}])
def test_invalid_filter_set_is_rejected(value):
    with pytest.raises(InvalidParamsError, match="Unsupported A2A event filter set"):
        resolve_event_filter_set({"iac_code": {"eventFilterSet": value}})


def test_request_metadata_precedes_message_metadata_and_is_recorded_for_subscribe():
    params = SendMessageRequest(
        message=Message(
            role=Role.ROLE_USER,
            metadata={"iac_code": {"eventFilterSet": USAGE, "channel": "console"}},
        ),
        metadata={"iac_code": {"eventFilterSet": SKILL}},
    )
    filter_set = message_event_filter_set(params)
    assert filter_set.name == SKILL
    prepared = filter_set_message_request(params, filter_set)
    assert prepared is not params
    assert data(params.message)["metadata"]["iac_code"]["eventFilterSet"] == USAGE
    assert data(prepared.message)["metadata"]["iac_code"] == {
        "eventFilterSet": SKILL,
        "channel": "console",
    }
    task = Task(id="task-1", history=[prepared.message])
    assert task_event_filter_set(task).name == SKILL


def test_subscribe_uses_latest_user_filter_set_and_supports_snake_case_alias():
    task = Task(
        id="task-1",
        history=[
            Message(role=Role.ROLE_USER, metadata={"iac_code": {"eventFilterSet": USAGE}}),
            Message(role=Role.ROLE_USER, metadata={"iac_code": {"event_filter_set": SKILL}}),
        ],
    )
    assert task_event_filter_set(task).name == SKILL
    task.history.append(Message(role=Role.ROLE_AGENT, metadata={"iac_code": {"eventFilterSet": NONE}}))
    assert task_event_filter_set(task).name == SKILL
    task.history.append(Message(role=Role.ROLE_USER, parts=[Part(text="reply")]))
    assert task_event_filter_set(task).name == NONE


@pytest.mark.parametrize("filter_set", ACTIVE_FILTER_SETS)
def test_usage_is_removed_without_mutating_canonical_metadata(filter_set):
    event = update(
        {"iacCodeSessionId": "session-1", "usage": {"inputTokens": 12}},
        extra_metadata={"businessRevision": 7, "ownerGeneration": 2},
    )
    original = event.SerializeToString()
    projected = active_filter(filter_set).project(event)
    assert data(projected)["metadata"] == {
        "iac_code": {"iacCodeSessionId": "session-1"},
        "businessRevision": 7,
        "ownerGeneration": 2,
    }
    assert event.SerializeToString() == original


@pytest.mark.parametrize("filter_set", ACTIVE_FILTER_SETS)
def test_first_binding_frame_and_explicit_working_control_pulses_are_retained(filter_set):
    delivery = A2AEventDeliveryFilter(EVENT_FILTER_SETS[filter_set])
    projected = delivery.project(
        update(
            {"iacCodeSessionId": "session-1", "usage": {"inputTokens": 12}},
            extra_metadata={"businessRevision": 7, "ownerGeneration": 2},
        )
    )
    assert data(projected)["metadata"] == {
        "iac_code": {"iacCodeSessionId": "session-1"},
        "businessRevision": 7,
        "ownerGeneration": 2,
    }
    assert delivery.project(update()) is not None


@pytest.mark.parametrize("filter_set", ACTIVE_FILTER_SETS)
def test_unknown_root_control_metadata_is_not_discarded(filter_set):
    projected = active_filter(filter_set).project(
        update({"usage": {"inputTokens": 12}}, extra_metadata={"naturalHandoff": {"revision": 4}})
    )
    assert data(projected)["metadata"]["naturalHandoff"] == {"revision": 4}


@pytest.mark.parametrize("filter_set", ACTIVE_FILTER_SETS)
@pytest.mark.parametrize(
    "state",
    [
        TaskState.TASK_STATE_SUBMITTED,
        TaskState.TASK_STATE_INPUT_REQUIRED,
        TaskState.TASK_STATE_COMPLETED,
        TaskState.TASK_STATE_FAILED,
        TaskState.TASK_STATE_CANCELED,
        TaskState.TASK_STATE_REJECTED,
        TaskState.TASK_STATE_AUTH_REQUIRED,
    ],
)
def test_lifecycle_states_survive_display_signal_removal(filter_set, state):
    event = update({"usage": {"inputTokens": 12}}, state=state)
    projected = active_filter(filter_set).project(event)
    assert projected.status.state == state
    assert "usage" not in data(projected).get("metadata", {}).get("iac_code", {})


@pytest.mark.parametrize("filter_set", ACTIVE_FILTER_SETS)
@pytest.mark.parametrize("event_type", sorted(INTERACTION_AND_RECOVERY_EVENTS))
def test_control_pipeline_events_cannot_be_filtered_by_online_presets(filter_set, event_type):
    envelope = {"eventType": event_type, "eventId": "event-1", "sequence": 14, "data": {"kind": "permission"}}
    projected = active_filter(filter_set).project(update({"pipeline": envelope}))
    assert data(projected)["metadata"]["iac_code"]["pipeline"] == envelope


def test_gateway_required_events_survive_even_when_named_in_a_filter_set():
    delivery = A2AEventDeliveryFilter(A2AEventFilterSet("future", frozenset({"usage", "input_required"})))
    delivery.project(Task(id="task-1", context_id="ctx-1"))
    for event_type in GATEWAY_REQUIRED_PIPELINE_EVENTS:
        envelope = {"eventType": event_type, "sequence": 7, "input": {"kind": "permission"}}
        single = delivery.project(update({"pipeline": envelope}))
        assert data(single)["metadata"]["iac_code"]["pipeline"] == envelope
        batch = delivery.project(update({"pipelineBatch": {"events": [{"eventType": "usage"}, envelope]}}))
        assert data(batch)["metadata"]["iac_code"]["pipelineBatch"]["events"] == [envelope]


def test_gateway_session_binding_survives_when_first_seen_on_a_filtered_event():
    delivery = A2AEventDeliveryFilter(A2AEventFilterSet("future", frozenset({"usage", "iacCodeSessionId"})))
    delivery.project(Task(id="task-1", context_id="ctx-1"))
    event = update({"iacCodeSessionId": "session-1", "usage": {"inputTokens": 12}})
    projected = delivery.project(event)
    assert data(projected)["metadata"]["iac_code"] == {"iacCodeSessionId": "session-1"}
    assert delivery.project(event) is None  # The gateway already received this identity.


@pytest.mark.parametrize("filter_set", ACTIVE_FILTER_SETS)
@pytest.mark.parametrize(
    "watermark",
    [
        {"businessRevision": 7, "ownerGeneration": 2},
        {"naturalHandoff": {"businessRevision": "7", "ownerGeneration": "2"}},
    ],
)
def test_gateway_watermark_frame_survives_even_when_all_business_events_are_filtered(filter_set, watermark):
    event = update({"pipelineBatch": {"events": [{"eventType": "usage"}]}}, extra_metadata=watermark)
    original = event.SerializeToString()
    projected = active_filter(filter_set).project(event)
    assert data(projected)["metadata"] == watermark
    assert event.SerializeToString() == original


@pytest.mark.parametrize("first", [True, False])
def test_artifact_and_its_gateway_binding_and_recovery_watermark_survive(first):
    delivery = A2AEventDeliveryFilter(EVENT_FILTER_SETS[SKILL])
    if not first:
        delivery.project(Task(id="task-1", context_id="ctx-1"))
    event = TaskArtifactUpdateEvent(
        task_id="task-1",
        context_id="ctx-1",
        artifact=Artifact(artifact_id="artifact-1"),
        metadata={"businessRevision": 7, "ownerGeneration": 2},
    )
    projected = delivery.project(event)
    assert projected is event
    assert projected.task_id == "task-1" and projected.context_id == "ctx-1"
    assert data(projected)["metadata"] == {"businessRevision": 7, "ownerGeneration": 2}
    assert event.artifact.artifact_id == "artifact-1"


@pytest.mark.parametrize(
    "filter_set,expected_names",
    [
        (USAGE, ["text_delta", "step_started", "input_required", "backup_committed"]),
        (SKILL, ["text_delta", "step_started", "input_required", "backup_committed"]),
    ],
)
def test_mixed_batch_is_filtered_in_order_without_changing_the_source(filter_set, expected_names):
    names = ["usage", "text_delta", "step_started", "context_usage", "input_required", "backup_committed"]
    envelopes = [{"eventType": name, "eventId": name, "sequence": i + 1} for i, name in enumerate(names)]
    event = update({"pipelineBatch": {"schemaVersion": "1.0", "events": envelopes}})
    original = event.SerializeToString()
    projected = active_filter(filter_set).project(event)
    batch = data(projected)["metadata"]["iac_code"]["pipelineBatch"]
    expected = [e for e in envelopes if e["eventType"] in expected_names]
    assert batch == {"schemaVersion": "1.0", "events": expected}
    assert event.SerializeToString() == original


@pytest.mark.parametrize("filter_set", ACTIVE_FILTER_SETS)
def test_fully_filtered_batch_is_not_delivered_as_empty_working_frame(filter_set):
    assert (
        active_filter(filter_set).project(
            update({"pipelineBatch": {"events": [{"eventType": "usage"}, {"eventType": "context_usage"}]}})
        )
        is None
    )


@pytest.mark.parametrize("filter_set", ACTIVE_FILTER_SETS)
def test_mcp_warning_and_its_text_are_preserved_together(filter_set):
    event = update({"mcpWarning": {"message": "server configuration warning"}}, text="MCP warning: details")
    projected = active_filter(filter_set).project(event)
    assert data(projected) == data(event)


@pytest.mark.parametrize("filter_set", ACTIVE_FILTER_SETS)
def test_interaction_projection_and_recovery_watermarks_are_preserved(filter_set):
    iac_code = {
        "iacCodeSessionId": "session-1",
        "pipelineName": "selling",
        "pipelineRunId": "run-1",
        "scope": "pipeline",
        "input": {"kind": "cloud_resource_selection", "inputId": "input-1"},
        "inputRequired": {"kind": "cloud_resource_selection", "inputId": "input-1"},
        "permission": {"pending": True, "toolUseId": "tool-1"},
        "pendingPermissions": [{"kind": "permission", "inputId": "permission-1"}],
        "permissionIdentityError": {"code": "identity_changed"},
        "backupBlocked": {"recoverable": True},
    }
    event = update(iac_code, extra_metadata={"businessRevision": 9, "ownerGeneration": 3})
    projected = active_filter(filter_set).project(event)
    assert data(projected)["metadata"] == data(event)["metadata"]


@pytest.mark.parametrize("filter_set", ACTIVE_FILTER_SETS)
def test_plain_text_final_snapshot_and_permission_ack_message_are_preserved(filter_set):
    delivery = active_filter(filter_set)
    assert data(delivery.project(update(text="partial")))["status"]["message"]["parts"] == [{"text": "partial"}]
    final = delivery.project(update({"assistantFinal": {"complete": True}}, text="final"))
    assert data(final)["metadata"]["iac_code"]["assistantFinal"] == {"complete": True}
    ack = ParseDict(
        {"role": "ROLE_AGENT", "parts": [{"data": {"data": {"kind": "permission_ack", "accepted": True}}}]},
        Message(),
    )
    assert delivery.project(ack) is ack


def test_ui_and_skill_have_independent_tool_thinking_and_milestone_views():
    ui = active_filter(USAGE)
    skill = active_filter(SKILL)
    tool = update({"tool": {"status": "input_complete", "toolUseId": "tool-1"}})
    thinking = update({"thinking": {"type": "raw_thinking", "text": "thinking"}})
    step = update({"pipeline": {"eventType": "step_started"}})
    for event in (tool, thinking):
        assert ui.project(event) is not None
    assert skill.project(tool) is not None
    assert skill.project(thinking) is None
    assert ui.project(step) is not None
    assert skill.project(step) is not None
    assert ui.project(update({"tool": {"status": "progress", "toolUseId": "tool-1"}})) is not None


@pytest.mark.parametrize("filter_set", ACTIVE_FILTER_SETS)
def test_standard_artifacts_are_preserved(filter_set):
    artifact = Artifact(artifact_id="artifact-1", name="template.yml")
    event = TaskArtifactUpdateEvent(task_id="task-1", context_id="ctx-1", artifact=artifact)
    task = Task(id="task-1", context_id="ctx-1", artifacts=[artifact])
    delivery = active_filter(filter_set)
    assert delivery.project(event) is event
    projected_task = delivery.project(task)
    assert data(projected_task) == data(task)
    assert len(task.artifacts) == 1


def test_selling_handoff_preserves_both_tool_start_formats_and_permission_recovery():
    def pipeline(name, **fields):
        return update({"pipeline": {"eventType": name, **fields}})

    events = [
        pipeline("tool_started", data={"toolUseId": "tool-1", "toolName": "ros_deploy"}),
        pipeline("tool_result", data={"toolUseId": "tool-1", "isError": True}),
        pipeline("step_failed", data={"errorSummary": "deployment failed"}),
        pipeline("pipeline_failed"),
        pipeline("pipeline_handoff_ready", data={"targetMode": "normal", "outcome": "failed"}),
        update({"tool": {"status": "started", "toolUseId": "tool-2", "name": "ros_stack"}}),
        update({"tool": {"status": "input_complete", "toolUseId": "tool-2", "toolInput": {"action": "get"}}}),
        update({"permission": {"pending": True, "toolUseId": "tool-2"}}),
        update({"permissionAck": {"accepted": True, "toolUseId": "tool-2"}}),
        update({"permissionWait": {"status": "suspending", "resumable": True}}),
        update({"permissionRecovered": {"inputId": "input-2", "toolUseId": "tool-2"}}),
        update({"tool": {"status": "completed", "toolUseId": "tool-2", "result": "stack found"}}),
        update({"assistantFinal": {"complete": True}}, text="final answer"),
        update(state=TaskState.TASK_STATE_INPUT_REQUIRED),
    ]
    delivery = active_filter(USAGE)
    for event in events:
        original = event.SerializeToString()
        assert data(delivery.project(event)) == data(event)
        assert event.SerializeToString() == original


def test_selling_preserves_low_volume_and_future_signals_in_single_and_batch_delivery():
    names = [
        "usage",
        "context_usage",
        "context_compaction_started",
        "context_compacted",
        "context_compaction_failed",
        "message_tombstone",
        "pipeline_warning",
        "candidate_failed",
        "candidate_step_failed",
        "candidate_restart_requested",
        "cleanup_started",
        "cleanup_progress",
        "cleanup_completed",
        "cleanup_failed",
        "future_event",
    ]
    envelopes = [{"eventType": name, "sequence": i + 1} for i, name in enumerate(names)]
    delivery = active_filter(USAGE)
    for envelope in envelopes[2:]:
        event = update({"pipeline": envelope})
        assert data(delivery.project(event)) == data(event)
    event = update({"pipelineBatch": {"schemaVersion": "1.0", "events": envelopes}})
    projected = delivery.project(event)
    assert data(projected)["metadata"]["iac_code"]["pipelineBatch"]["events"] == envelopes[2:]
    assert data(event)["metadata"]["iac_code"]["pipelineBatch"]["events"] == envelopes


def test_normal_usage_filter_keeps_unreviewed_signals_and_only_removes_usage():
    metadata = {
        "usage": {"inputTokens": 12},
        "cleanupOnly": {"pending": 1},
        "permission": {"autoApproved": True, "toolUseId": "tool-1"},
        "permissionWait": {"status": "suspended", "resumable": True},
        "permissionRecovered": {"inputId": "input-1"},
        "mcpStatus": {"servers": []},
        "tool": {"status": "future_status", "toolUseId": "tool-1"},
        "futureSignal": {"enabled": True},
    }
    event = update(metadata)
    projected = active_filter(USAGE).project(event)
    assert data(projected)["metadata"]["iac_code"] == {key: value for key, value in metadata.items() if key != "usage"}
    assert "usage" in data(event)["metadata"]["iac_code"]


def test_skill_filters_thinking_and_argument_progress_but_keeps_text_results_and_recovery_signals():
    delivery = active_filter(SKILL)
    for status in ("started", "input_complete", "completed", "failed", "progress", "future_status"):
        event = update({"tool": {"status": status, "toolUseId": "tool-1", "result": "result"}})
        assert data(delivery.project(event)) == data(event)
    assert delivery.project(update({"tool": {"status": "input_delta", "toolUseId": "tool-1"}})) is None
    assert delivery.project(update({"thinking": {"type": "raw_thinking", "text": "thinking"}})) is None

    names = [
        "usage",
        "context_usage",
        "thinking_delta",
        "text_delta",
        "tool_started",
        "tool_result",
        "stack_progress",
        "diagram_shown",
        "candidate_detail_shown",
        "artifact_created",
        "candidate_failed",
        "message_tombstone",
        "interrupt_received",
        "interrupt_classified",
        "rollback_completed",
        "candidate_restart_requested",
        "pipeline_warning",
        "cleanup_started",
        "cleanup_progress",
        "cleanup_completed",
        "cleanup_failed",
        "future_event",
    ]
    envelopes = [
        {"eventType": name, "sequence": i + 1, "data": {"text": "visible answer"} if name == "text_delta" else {}}
        for i, name in enumerate(names)
    ]
    for envelope in envelopes[3:]:
        event = update({"pipeline": envelope})
        assert data(delivery.project(event)) == data(event)
    event = update({"pipelineBatch": {"events": envelopes}})
    assert data(delivery.project(event))["metadata"]["iac_code"]["pipelineBatch"]["events"] == envelopes[3:]
    assert data(event)["metadata"]["iac_code"]["pipelineBatch"]["events"] == envelopes


def test_skill_retains_normal_text_final_answer_and_control_data_even_alongside_suppressed_deltas():
    delivery = active_filter(SKILL)
    event = update(
        {
            "usage": {"totalTokens": 12},
            "thinking": {"text": "thinking"},
            "tool": {"status": "input_delta", "toolUseId": "tool-1"},
            "permissionWait": {"status": "suspending", "resumable": True},
            "permissionRecovered": {"inputId": "input-1", "toolUseId": "tool-1"},
            "permission": {"autoApproved": True},
            "assistantFinal": {"complete": True},
            "cleanupOnly": {"pending": 0},
            "futureSignal": {"enabled": True},
        },
        text="authoritative final answer",
        extra_metadata={"businessRevision": 7, "ownerGeneration": 2},
    )
    projected = data(delivery.project(event))
    expected = data(event)
    for key in ("usage", "thinking", "tool"):
        expected["metadata"]["iac_code"].pop(key)
    assert projected == expected
    assert data(event)["metadata"]["iac_code"]["tool"]["status"] == "input_delta"


@pytest.mark.asyncio
async def test_concurrent_stream_requests_have_independent_profiles_and_keep_source(monkeypatch):
    shared = update({"usage": {"totalTokens": 11}})
    original = data(shared)

    async def source(self, params, context):
        yield Task(id="task-1")
        await asyncio.sleep(0)
        yield shared
        yield update({"assistantFinal": {"complete": True}}, state=TaskState.TASK_STATE_INPUT_REQUIRED)

    monkeypatch.setattr(IacCodeRequestHandler, "_on_message_send_stream_unfiltered", source)
    handler = IacCodeRequestHandler.__new__(IacCodeRequestHandler)

    async def collect(filter_set):
        request = SendMessageRequest(message=Message(metadata={"iac_code": {"eventFilterSet": filter_set}}))
        return [event async for event in handler.on_message_send_stream(request, None)]

    full, filtered = await asyncio.gather(collect(NONE), collect(USAGE))
    assert full[1] is shared
    assert len(full) == 3
    assert len(filtered) == 2
    assert data(shared) == original


@pytest.mark.asyncio
async def test_subscription_inherits_profile_without_pruning_persisted_task(monkeypatch):
    task = Task(
        id="task-1",
        context_id="ctx-1",
        status=TaskStatus(state=TaskState.TASK_STATE_WORKING),
        history=[Message(role=Role.ROLE_USER, metadata={"iac_code": {"eventFilterSet": USAGE}})],
        metadata={"iac_code": {"usage": {"totalTokens": 11}}},
    )
    store = A2ATaskStore()
    context = ServerCallContext()
    await store.save(task, context)
    record = await store.get_or_create_task(task_id=task.id, context_id=task.context_id)
    record.active_task = asyncio.current_task()
    original = data(await store.get(task.id, context))

    async def source(self, params, context):
        yield task
        yield update({"pipeline": {"eventType": "usage", "sequence": 2}})
        yield update({"input": {"kind": "permission", "inputId": "input-1"}}, state=TaskState.TASK_STATE_INPUT_REQUIRED)

    monkeypatch.setattr(DefaultRequestHandler, "on_subscribe_to_task", source)
    handler = IacCodeRequestHandler.__new__(IacCodeRequestHandler)
    handler.task_store = store
    handler._validate_extensions = lambda context: None
    result = [event async for event in handler.on_subscribe_to_task(SubscribeToTaskRequest(id=task.id), context)]
    assert len(result) == 2
    assert "usage" not in data(result[0]).get("metadata", {}).get("iac_code", {})
    assert data(result[1])["metadata"]["iac_code"]["input"]["inputId"] == "input-1"
    assert data(await store.get(task.id, context)) == original


@pytest.mark.parametrize("filter_set", [NONE, *ACTIVE_FILTER_SETS])
def test_rest_sse_uses_profile_without_changing_text_or_turn_completion(monkeypatch, tmp_path, filter_set):
    runtime = FakeRuntime(
        session_id="session-1",
        agent_loop=FakeAgentLoop(
            [
                ThinkingDeltaEvent(text="planning"),
                ToolUseStartEvent(tool_use_id="tool-1", name="read_file"),
                ToolInputDeltaEvent(tool_use_id="tool-1", partial_json='{"path":'),
                ToolUseEndEvent(tool_use_id="tool-1", name="read_file", input={"path": "template.yml"}),
                ToolResultEvent(tool_use_id="tool-1", tool_name="read_file", result="tool failed", is_error=True),
                TextDeltaEvent(text="answer"),
                MessageEndEvent(stop_reason="end_turn", usage=Usage(input_tokens=8, output_tokens=3)),
            ]
        ),
    )
    monkeypatch.setattr("iac_code.a2a.executor.create_agent_runtime", lambda options: runtime)
    app = create_app(host="127.0.0.1", port=41242, token=None, model="test-model", thinking_exposure="all")
    with TestClient(app) as client:
        response = client.post(
            "/message:stream",
            headers={"A2A-Version": "1.0"},
            json={
                "message": {
                    "messageId": "message-1",
                    "role": "ROLE_USER",
                    "parts": [{"text": "hello"}],
                    "metadata": {"iac_code": {"cwd": str(tmp_path), "eventFilterSet": filter_set}},
                }
            },
        )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    frames = [json.loads(line[5:].strip()) for line in response.text.splitlines() if line.startswith("data:")]
    updates = [frame["statusUpdate"] for frame in frames if "statusUpdate" in frame]
    metadata = [entry.get("metadata", {}).get("iac_code", {}) for entry in updates]
    assert any("usage" in item for item in metadata) is (filter_set == NONE)
    tool_statuses = {item["tool"]["status"] for item in metadata if "tool" in item}
    assert tool_statuses == (
        {"started", "input_complete", "failed"}
        if filter_set == SKILL
        else {"started", "input_delta", "input_complete", "failed"}
    )
    assert any("thinking" in item for item in metadata) is (filter_set != SKILL)
    assert any(item.get("assistantFinal", {}).get("complete") is True for item in metadata)
    assert updates[-1]["status"]["state"] == "TASK_STATE_INPUT_REQUIRED"
    assert any(
        part.get("text") == "answer"
        for entry in updates
        for part in entry.get("status", {}).get("message", {}).get("parts", [])
    )
    assert runtime.agent_loop.prompts == ["hello"]
