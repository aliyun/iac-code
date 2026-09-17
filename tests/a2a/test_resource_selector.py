from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from a2a.types import Message, Role, TaskState
from a2a.utils.errors import InvalidParamsError

from iac_code.a2a.executor import IacCodeA2AExecutor
from iac_code.a2a.metrics import NoOpA2AMetrics
from iac_code.a2a.pipeline_events import PipelineA2AContext, PipelineEventTranslator
from iac_code.a2a.pipeline_stream import _unified_input_projection
from iac_code.a2a.resource_selector import (
    PendingResourceSelection,
    ResourceSelectionCheckpointStore,
    ResourceSelectionInputRegistry,
    ResourceSelectionResponse,
    checkpoint_record,
    parse_resource_selection_response,
)
from iac_code.a2a.task_store import A2ATaskStore
from iac_code.agent.message import Message as AgentMessage
from iac_code.resource_selector.profiles import PROFILE_HASH, get_profile
from iac_code.resource_selector.tools import SelectCloudResourceTool
from iac_code.services.session_backup import BackupReason
from iac_code.services.session_storage import SessionStorage
from iac_code.tools.base import ToolContext
from iac_code.types.stream_events import (
    CloudResourceSelectionEvent,
    MessageEndEvent,
    MessageStartEvent,
    TextDeltaEvent,
    Usage,
)

from .fakes import FakeEventQueue, FakeRequestContext, FakeRuntime

_E2E_CASES = json.loads(
    (Path(__file__).parents[1] / "resource_selector/e2e-cases.json").read_text(encoding="utf-8")
)["cases"]
_ENABLED_E2E_CASES = [
    case
    for case in _E2E_CASES
    if (profile := get_profile(case["selectorId"])) is not None and profile.enabled
]


def selection_event(*, future=None) -> CloudResourceSelectionEvent:
    return CloudResourceSelectionEvent(
        tool_use_id="tool-1",
        input_id="resource-" + "a" * 32,
        question="请选择 ECS 实例",
        selector_id="ecs.instance",
        association_property="ALIYUN::ECS::Instance::InstanceId",
        output_kind="resource_id",
        association_property_metadata={"RegionId": "cn-hangzhou"},
        source=None,
        profile_hash=PROFILE_HASH,
        continuation_frame={
            "assistantMessageRef": "session.jsonl:1",
            "assistantMessageDigest": "digest",
            "orderedToolUseIds": ["tool-1"],
            "currentIndex": 0,
            "currentPayloadDigest": "payload",
        },
        response_future=future,
    )


def response(*, value="i-test123") -> ResourceSelectionResponse:
    return ResourceSelectionResponse(
        task_id="task-1",
        context_id="ctx-1",
        input_id="resource-" + "a" * 32,
        tool_use_id="tool-1",
        status="selected",
        selector_id="ecs.instance",
        value=value,
        label="app-server",
    )


def test_parser_only_accepts_structured_metadata_and_never_text() -> None:
    plain = Message(message_id="m-1", role=Role.ROLE_USER)
    assert parse_resource_selection_response(plain) is None

    message = Message(message_id="m-2", role=Role.ROLE_USER)
    message.metadata.update(
        {
            "iac_code": {
                "inputResponse": {
                    "schemaVersion": 1,
                    "kind": "cloud_resource_selection",
                    "status": "selected",
                    "requestTaskId": "task-1",
                    "contextId": "ctx-1",
                    "inputId": "resource-" + "a" * 32,
                    "toolUseId": "tool-1",
                    "selectorId": "ecs.instance",
                    "value": "i-test123",
                }
            }
        }
    )
    parsed = parse_resource_selection_response(message)
    assert parsed is not None
    assert parsed.value == "i-test123"


def test_canceled_response_preserves_empty_options_signal() -> None:
    message = Message(message_id="m-canceled", role=Role.ROLE_USER)
    message.metadata.update(
        {
            "iac_code": {
                "inputResponse": {
                    "schemaVersion": 1,
                    "kind": "cloud_resource_selection",
                    "status": "canceled",
                    "requestTaskId": "task-1",
                    "contextId": "ctx-1",
                    "inputId": "resource-" + "a" * 32,
                    "toolUseId": "tool-1",
                    "optionsEmpty": True,
                }
            }
        }
    )

    parsed = parse_resource_selection_response(message)

    assert parsed is not None
    assert parsed.options_empty is True
    assert parsed.tool_response(expected_selector_id="ecs.instance")["options_empty"] is True
    assert parsed.to_dict()["optionsEmpty"] is True


def test_canceled_response_rejects_non_boolean_empty_options_signal() -> None:
    message = Message(message_id="m-invalid-canceled", role=Role.ROLE_USER)
    message.metadata.update(
        {
            "iac_code": {
                "inputResponse": {
                    "schemaVersion": 1,
                    "kind": "cloud_resource_selection",
                    "status": "canceled",
                    "requestTaskId": "task-1",
                    "contextId": "ctx-1",
                    "inputId": "resource-" + "a" * 32,
                    "toolUseId": "tool-1",
                    "optionsEmpty": "true",
                }
            }
        }
    )

    with pytest.raises(InvalidParamsError, match="optionsEmpty is invalid"):
        parse_resource_selection_response(message)


@pytest.mark.asyncio
async def test_durable_claim_is_idempotent_and_conflicting_answer_is_rejected(tmp_path) -> None:
    loop = asyncio.get_running_loop()
    event = selection_event(future=loop.create_future())
    store = ResourceSelectionCheckpointStore(str(tmp_path), "session-1")
    pending = PendingResourceSelection(
        task_id="task-1",
        context_id="ctx-1",
        session_id="session-1",
        cwd=str(tmp_path),
        event=event,
        store=store,
    )
    store.create(checkpoint_record(pending))
    registry = ResourceSelectionInputRegistry()
    await registry.register(pending)

    accepted, replayed = await registry.answer(pending, response())
    assert accepted and not replayed
    assert await event.response_future == {
        "status": "selected",
        "input_id": event.input_id,
        "selector_id": "ecs.instance",
        "value": "i-test123",
        "label": "app-server",
    }
    accepted, replayed = await registry.answer(pending, response())
    assert accepted and replayed
    with pytest.raises(InvalidParamsError, match="conflicts"):
        await registry.answer(pending, response(value="i-other123"))


@pytest.mark.asyncio
async def test_durable_claim_commits_session_state_before_delivery_and_not_on_replay(tmp_path) -> None:
    event = selection_event(future=asyncio.get_running_loop().create_future())
    store = ResourceSelectionCheckpointStore(str(tmp_path), "session-1")
    pending = PendingResourceSelection(
        task_id="task-1",
        context_id="ctx-1",
        session_id="session-1",
        cwd=str(tmp_path),
        event=event,
        store=store,
    )
    store.create(checkpoint_record(pending))
    registry = ResourceSelectionInputRegistry()
    await registry.register(pending)
    callback_observations: list[bool] = []

    async def before_delivery() -> None:
        callback_observations.append(event.response_future.done())

    accepted, replayed = await registry.answer(pending, response(), before_delivery=before_delivery)
    assert accepted and not replayed
    assert callback_observations == [False]
    assert event.response_future.done()

    accepted, replayed = await registry.answer(pending, response(), before_delivery=before_delivery)
    assert accepted and replayed
    assert callback_observations == [False]


@pytest.mark.asyncio
async def test_executor_resource_answer_commits_and_activates_session_headers_before_resume(tmp_path) -> None:
    task_store = A2ATaskStore(metrics=NoOpA2AMetrics())
    await task_store.get_or_create_task(task_id="task-1", context_id="ctx-1")
    executor = IacCodeA2AExecutor(task_store=task_store, model="qwen3.6-plus")
    event = selection_event(future=asyncio.get_running_loop().create_future())
    checkpoint_store = ResourceSelectionCheckpointStore(str(tmp_path), "session-1")
    pending = PendingResourceSelection(
        task_id="task-1",
        context_id="ctx-1",
        session_id="session-1",
        cwd=str(tmp_path),
        event=event,
        store=checkpoint_store,
    )
    checkpoint_store.create(checkpoint_record(pending))
    await executor._resource_selection_registry.register(pending)
    observations: list[tuple[str, dict[str, str]]] = []

    async def commit_headers() -> None:
        binding = await task_store.bind_context_llm_headers("ctx-1", {"X-A2A-Session": "selector-session"})
        observations.append(("commit", dict(binding)))

    async def activate_headers() -> None:
        binding = await task_store.bind_context_llm_headers("ctx-1", None)
        observations.append(("activate", dict(binding)))

    await executor._answer_resource_selection(
        SimpleNamespace(call_context=None),
        FakeEventQueue(),
        response=response(),
        commit_llm_headers=commit_headers,
        activate_bound_llm_headers=activate_headers,
    )

    assert observations == [
        ("commit", {"X-A2A-Session": "selector-session"}),
        ("activate", {"X-A2A-Session": "selector-session"}),
    ]
    assert event.response_future.done()


@pytest.mark.asyncio
async def test_live_resource_selection_can_be_answered_while_input_required_is_published(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    event = selection_event(future=asyncio.get_running_loop().create_future())
    resumed = asyncio.Event()

    class RecordingBackup:
        def __init__(self):
            self.calls = []

        async def backup(self, _service, _cwd, _session_id, *, reason, critical, **_kwargs):
            self.calls.append((reason, critical))
            return None

    backup = RecordingBackup()
    monkeypatch.setattr("iac_code.a2a.executor.backup_session_async", backup.backup)

    class ImmediateAnswerLoop:
        async def run_streaming(self, _prompt):
            yield event
            resumed.set()
            yield MessageStartEvent(message_id="after-selection")
            yield TextDeltaEvent(text="selection accepted")
            yield MessageEndEvent(stop_reason="end_turn", usage=Usage())

    runtime = FakeRuntime(agent_loop=ImmediateAnswerLoop(), session_id="session-1")
    monkeypatch.setattr("iac_code.a2a.executor.create_agent_runtime", lambda _options: runtime)
    executor = IacCodeA2AExecutor(task_store=A2ATaskStore(metrics=NoOpA2AMetrics()), model="qwen3.6-plus")
    queue = FakeEventQueue()
    original_publish_status = executor._publish_status
    answer_task: asyncio.Task[None] | None = None

    async def publish_status(target_queue, **kwargs):
        nonlocal answer_task
        await original_publish_status(target_queue, **kwargs)
        metadata = kwargs.get("metadata")
        iac_code = metadata.get("iac_code") if isinstance(metadata, dict) else None
        if kwargs.get("state") == TaskState.TASK_STATE_INPUT_REQUIRED and isinstance(iac_code, dict):
            if isinstance(iac_code.get("inputRequired"), dict):
                answer_task = asyncio.create_task(
                    executor._answer_resource_selection(
                        SimpleNamespace(call_context=None),
                        queue,
                        response=response(),
                    )
                )
                # Observe answer delivery without waiting for the continuation,
                # which needs execute() to release the context lock first.
                await asyncio.shield(event.response_future)

    monkeypatch.setattr(executor, "_publish_status", publish_status)

    try:
        await executor.execute(FakeRequestContext(metadata={"iac_code": {"cwd": str(tmp_path)}}), queue)
        assert answer_task is not None
        await asyncio.shield(answer_task)

        assert event.response_future.done()
        assert resumed.is_set()
        assert not await executor._resource_selection_registry.has_pending_task("task-1")
        record = await executor._task_store.get_task_record("task-1")
        assert "selection accepted" in record.output_text
        assert (BackupReason.NORMAL_TURN_END, False) in backup.calls
    finally:
        if answer_task is not None:
            if not answer_task.done():
                answer_task.cancel()
            await asyncio.gather(answer_task, return_exceptions=True)
        await executor._resource_selection_registry.cancel_task("task-1")
        await executor._task_store.stop_cleanup_loop()


@pytest.mark.asyncio
async def test_persisted_resource_selection_recovery_reuses_request_scoped_runtime_overrides(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from iac_code.a2a.runtime_overrides import get_a2a_preferred_language
    from iac_code.services.providers.aliyun import AliyunCredentials
    from iac_code.services.telemetry import get_user_id

    observations: list[tuple[str, str | None, str | None]] = []

    class RecoveryLoop:
        async def resume_resource_selection_boundary(self, *_args, **_kwargs):
            credential = AliyunCredentials.load()
            observations.append(
                (
                    get_user_id(),
                    get_a2a_preferred_language(),
                    credential.access_key_id if credential else None,
                )
            )
            yield MessageStartEvent(message_id="recovered")
            yield TextDeltaEvent(text="done")
            yield MessageEndEvent(stop_reason="end_turn", usage=Usage())

    class RecoveryRuntime(SimpleNamespace):
        def set_resource_selector_enabled(self, enabled: bool) -> None:
            self.resource_selector_enabled = enabled

    task_store = A2ATaskStore(metrics=NoOpA2AMetrics())
    context_record = await task_store.get_or_create_context(
        context_id="ctx-1",
        cwd=str(tmp_path),
        runtime_factory=lambda session_id: RecoveryRuntime(session_id=session_id),
    )
    await task_store.discard_context_runtime("ctx-1")
    await task_store.get_or_create_task(task_id="task-1", context_id="ctx-1")
    SessionStorage().append(
        str(tmp_path),
        context_record.session_id,
        AgentMessage(role="user", content="select a resource"),
    )
    event = selection_event(future=asyncio.get_running_loop().create_future())
    checkpoint_store = ResourceSelectionCheckpointStore(str(tmp_path), context_record.session_id)
    pending = PendingResourceSelection(
        task_id="task-1",
        context_id="ctx-1",
        session_id=context_record.session_id,
        cwd=str(tmp_path),
        event=event,
        store=checkpoint_store,
    )
    checkpoint_store.create(checkpoint_record(pending))

    runtime_options = []
    configured = []
    refreshed: list[tuple[str | None, str | None]] = []

    def create_runtime(options):
        credential = AliyunCredentials.load()
        runtime_options.append((options, credential.access_key_id if credential else None))
        return RecoveryRuntime(agent_loop=RecoveryLoop(), session_id=options.session_id)

    def configure_runtime(runtime, model, **kwargs):
        configured.append((runtime, model, kwargs))

    def refresh_runtime(_runtime):
        credential = AliyunCredentials.load()
        refreshed.append(
            (
                get_a2a_preferred_language(),
                credential.access_key_id if credential else None,
            )
        )

    async def skip_backup(*_args, **_kwargs):
        return None

    monkeypatch.setattr("iac_code.a2a.executor.create_agent_runtime", create_runtime)
    monkeypatch.setattr("iac_code.a2a.executor.configure_runtime_model", configure_runtime)
    monkeypatch.setattr("iac_code.a2a.executor.refresh_runtime_cloud_tools", refresh_runtime)
    monkeypatch.setattr("iac_code.a2a.executor.backup_session_async", skip_backup)
    monkeypatch.setattr("iac_code.services.providers.aliyun.AliyunCredentials._load_from_iac_code_config", lambda: None)

    executor = IacCodeA2AExecutor(task_store=task_store, model="server-model")
    await executor._answer_resource_selection(
        SimpleNamespace(
            call_context=None,
            metadata={
                "iac_code": {
                    "user_id": "request-user",
                    "preferredLanguage": "ja-JP",
                    "iac_code_model": "request-model",
                    "iac_code_api_key": "request-key",
                    "thinking": {"enabled": True, "effort": "high", "budget": 1024},
                    "alibaba_cloud_access_key_id": "request-ak",
                    "alibaba_cloud_access_key_secret": "request-secret",
                    "alibaba_cloud_region_id": "cn-hangzhou",
                    "alibaba_cloud_security_token": "request-token",
                }
            },
            message=None,
        ),
        FakeEventQueue(),
        response=response(),
    )

    assert runtime_options[0][0].model == "request-model"
    assert runtime_options[0][1] == "request-ak"
    assert configured[0][1] == "request-model"
    assert configured[0][2]["from_metadata"] is True
    assert configured[0][2]["metadata_api_key"] == "request-key"
    policy = configured[0][2]["request_policy_override"]
    assert policy.thinking_enabled is True
    assert policy.effort == "high"
    assert policy.thinking_budget == 1024
    assert refreshed == [("ja", "request-ak")]
    assert observations == [("request-user", "ja", "request-ak")]


@pytest.mark.asyncio
async def test_executor_rejects_cross_owner_resource_answer_before_claim_or_header_commit(tmp_path) -> None:
    task_store = A2ATaskStore(metrics=NoOpA2AMetrics(), owner_resolver=lambda context: str(context or ""))
    await task_store.get_or_create_task(task_id="task-1", context_id="ctx-1", owner="owner-a")
    await task_store.bind_context_llm_headers("ctx-1", {"Authorization": "Bearer victim"})
    executor = IacCodeA2AExecutor(task_store=task_store, model="qwen3.6-plus")
    event = selection_event(future=asyncio.get_running_loop().create_future())
    checkpoint_store = ResourceSelectionCheckpointStore(str(tmp_path), "session-1")
    pending = PendingResourceSelection(
        task_id="task-1",
        context_id="ctx-1",
        session_id="session-1",
        cwd=str(tmp_path),
        event=event,
        store=checkpoint_store,
    )
    checkpoint_store.create(checkpoint_record(pending))
    await executor._resource_selection_registry.register(pending)
    commit_called = False

    async def commit_headers() -> None:
        nonlocal commit_called
        commit_called = True
        await task_store.bind_context_llm_headers("ctx-1", {"Authorization": "Bearer attacker"})

    with pytest.raises(InvalidParamsError, match="different owner"):
        await executor._answer_resource_selection(
            SimpleNamespace(call_context="owner-b"),
            FakeEventQueue(),
            response=response(),
            commit_llm_headers=commit_headers,
        )

    assert not commit_called
    assert not event.response_future.done()
    assert checkpoint_store.load(event.input_id)["state"] == "pending"
    assert await task_store.resolve_context_llm_headers("ctx-1", None) == {"Authorization": "Bearer victim"}


@pytest.mark.asyncio
async def test_explicit_task_termination_removes_pending_wait_without_timeout(tmp_path) -> None:
    event = selection_event(future=asyncio.get_running_loop().create_future())
    store = ResourceSelectionCheckpointStore(str(tmp_path), "session-1")
    pending = PendingResourceSelection(
        task_id="task-1",
        context_id="ctx-1",
        session_id="session-1",
        cwd=str(tmp_path),
        event=event,
        store=store,
    )
    store.create(checkpoint_record(pending))
    registry = ResourceSelectionInputRegistry()
    await registry.register(pending)

    assert await registry.has_pending_task("task-1")
    assert await registry.cancel_task("task-1") == 1
    assert not await registry.has_pending_task("task-1")
    assert store.load(event.input_id)["state"] == "resolved"
    assert not event.response_future.done()


@pytest.mark.asyncio
async def test_derived_selection_callback_must_echo_the_pending_source(tmp_path) -> None:
    source = {
        "selector_id": "redis.instance",
        "value": "r-test123",
        "association_property_metadata": {"RegionId": "cn-hangzhou"},
    }
    event = CloudResourceSelectionEvent(
        tool_use_id="tool-1",
        input_id="resource-" + "b" * 32,
        question="请选择 Redis 连接地址",
        selector_id="redis.connection_url",
        association_property="ALIYUN::Redis::Instance::ConnectionURL",
        output_kind="endpoint",
        association_property_metadata={"RegionId": "cn-hangzhou", "InstanceId": "r-test123"},
        source=source,
        profile_hash=PROFILE_HASH,
        continuation_frame={
            "assistantMessageRef": "session.jsonl:1",
            "assistantMessageDigest": "digest",
            "orderedToolUseIds": ["tool-1"],
            "currentIndex": 0,
            "currentPayloadDigest": "payload",
        },
        response_future=asyncio.get_running_loop().create_future(),
    )
    store = ResourceSelectionCheckpointStore(str(tmp_path), "session-derived")
    pending = PendingResourceSelection(
        task_id="task-1",
        context_id="ctx-1",
        session_id="session-derived",
        cwd=str(tmp_path),
        event=event,
        store=store,
    )
    store.create(checkpoint_record(pending))
    registry = ResourceSelectionInputRegistry()
    await registry.register(pending)
    missing_source = ResourceSelectionResponse(
        task_id="task-1",
        context_id="ctx-1",
        input_id=event.input_id,
        tool_use_id="tool-1",
        status="selected",
        selector_id="redis.connection_url",
        value="redis://r-test123.example:6379",
    )
    with pytest.raises(InvalidParamsError, match="source mismatch"):
        await registry.answer(pending, missing_source)

    selected = ResourceSelectionResponse(
        task_id="task-1",
        context_id="ctx-1",
        input_id=event.input_id,
        tool_use_id="tool-1",
        status="selected",
        selector_id="redis.connection_url",
        value="redis://r-test123.example:6379",
        source=source,
    )
    accepted, replayed = await registry.answer(pending, selected)
    assert accepted and not replayed


def test_pipeline_event_projects_authoritative_custom_input_required() -> None:
    translator = PipelineEventTranslator(
        PipelineA2AContext(
            pipeline_run_id="run-1",
            task_id="task-1",
            context_id="ctx-1",
            pipeline_name="selling",
        )
    )
    envelope = translator.translate(selection_event())[0]
    assert envelope["eventType"] == "input_required"
    assert envelope["status"] == "input_required"
    assert envelope["input"]["kind"] == "cloud_resource_selection"
    projection = _unified_input_projection(envelope)
    assert projection == {
        "schemaVersion": 1,
        "kind": "cloud_resource_selection",
        "requestTaskId": "task-1",
        "contextId": "ctx-1",
        "inputId": "resource-" + "a" * 32,
        "prompt": "请选择 ECS 实例",
        "required": True,
        "toolUseId": "tool-1",
        "selector": {
            "id": "ecs.instance",
            "associationProperty": "ALIYUN::ECS::Instance::InstanceId",
            "outputKind": "resource_id",
            "associationPropertyMetadata": {"RegionId": "cn-hangzhou"},
            "source": None,
            "profileHash": PROFILE_HASH,
        },
        "options": [],
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("case", _ENABLED_E2E_CASES, ids=lambda case: case["selectorId"])
async def test_all_selectors_round_trip_through_a2a_input_required_protocol(case, tmp_path) -> None:
    queue: asyncio.Queue = asyncio.Queue()
    tool = SelectCloudResourceTool(lambda: "cn-hangzhou")
    tool_use_id = "tool-{}".format(case["selectorId"])
    tool_input = {
        "question": "请选择资源",
        "selector_id": case["selectorId"],
        "association_property_metadata": case["metadata"],
    }
    if case["source"] is not None:
        tool_input["source"] = case["source"]

    task = asyncio.create_task(
        tool.execute(tool_input=tool_input, context=ToolContext(event_queue=queue, tool_use_id=tool_use_id))
    )
    event = await asyncio.wait_for(queue.get(), timeout=1)
    assert isinstance(event, CloudResourceSelectionEvent)
    assert event.selector_id == case["selectorId"]
    assert event.profile_hash == PROFILE_HASH
    if case["source"] is None:
        assert event.source is None
    else:
        assert event.source is not None
        assert event.source["selector_id"] == case["source"]["selector_id"]
        assert event.source["value"] == case["source"]["value"]

    envelope = PipelineEventTranslator(
        PipelineA2AContext(
            pipeline_run_id="run-1",
            task_id="task-1",
            context_id="ctx-1",
            pipeline_name="selling",
        )
    ).translate(event)[0]
    assert envelope["eventType"] == "input_required"
    assert envelope["input"]["selector"]["id"] == case["selectorId"]

    # The production agent loop attaches this frame before handing the event
    # to A2A.  This focused protocol test supplies an equivalent durable frame
    # because it invokes the tool directly.
    event.continuation_frame = {
        "assistantMessageRef": "session.jsonl:1",
        "assistantMessageDigest": "digest",
        "orderedToolUseIds": [tool_use_id],
        "currentIndex": 0,
        "currentPayloadDigest": "payload",
    }
    store = ResourceSelectionCheckpointStore(str(tmp_path), "session-" + case["selectorId"].replace(".", "-"))
    pending = PendingResourceSelection(
        task_id="task-1",
        context_id="ctx-1",
        session_id="session-" + case["selectorId"].replace(".", "-"),
        cwd=str(tmp_path),
        event=event,
        store=store,
    )
    store.create(checkpoint_record(pending))
    registry = ResourceSelectionInputRegistry()
    await registry.register(pending)

    input_response = {
        "schemaVersion": 1,
        "kind": "cloud_resource_selection",
        "status": "selected",
        "requestTaskId": "task-1",
        "contextId": "ctx-1",
        "inputId": event.input_id,
        "toolUseId": event.tool_use_id,
        "selectorId": event.selector_id,
        "value": str(case["expected"]["value"]),
        "label": str(case["expected"]["value"]),
    }
    if case["source"] is not None:
        # ROS echoes the source contract from the input-required event, which
        # may contain server-normalized metadata beyond the original tool arg.
        input_response["source"] = event.source
    callback = Message(message_id="ros-callback", role=Role.ROLE_USER)
    callback.metadata.update({"iac_code": {"inputResponse": input_response}})
    parsed = parse_resource_selection_response(callback)
    assert parsed is not None
    accepted, replayed = await registry.answer(pending, parsed)
    assert accepted and not replayed

    result = json.loads((await task).content)
    assert result["selector_id"] == case["selectorId"]
    assert result["value"] == str(case["expected"]["value"])
    await registry.complete(pending)
