from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from a2a.types import TaskStatusUpdateEvent
from google.protobuf.json_format import MessageToDict

from iac_code.a2a.executor import (
    IacCodeA2AExecutor,
    _a2a_deferred_cleanup_prompts_path,
    _append_a2a_deferred_cleanup_prompt,
    _cleanup_ledger_for_a2a_normal_chat,
    _cleanup_payload_from_private_ledger_or_unavailable,
    _cleanup_publisher_for_a2a_normal_chat,
    _cleanup_resource_states,
    _load_a2a_deferred_cleanup_prompts,
    _observe_cleanup_stream,
    _prune_completed_cleanup_prompt_from_runtime,
    _publish_cleanup_resource_changes,
)
from iac_code.a2a.metrics import NoOpA2AMetrics
from iac_code.a2a.persistence import A2AContextSnapshot, A2APersistenceStore
from iac_code.a2a.pipeline_journal import A2APipelineJournal
from iac_code.a2a.pipeline_snapshot import A2APipelineSnapshotStore
from iac_code.a2a.task_store import A2ATaskStore
from iac_code.agent.message import Message
from iac_code.pipeline.engine.cleanup import (
    CLEANUP_PROMPT_METADATA_TYPE,
    CleanupLedger,
    CleanupResource,
    ObservedResource,
    create_cleanup_prompt_message,
)
from iac_code.services.session_storage import SessionStorage
from iac_code.types.stream_events import TextDeltaEvent, ToolResultEvent, ToolUseEndEvent

from .fakes import FakeAgentLoop, FakeEventQueue, FakeRequestContext, FakeRuntime


def _dump(event):
    return MessageToDict(event, preserving_proto_field_name=False)


class _TaskStore:
    def __init__(self, *, cwd: str, session_id: str) -> None:
        self._record = SimpleNamespace(cwd=cwd, session_id=session_id)

    async def get_context_record(self, context_id: str) -> SimpleNamespace:
        return self._record


def test_a2a_handoff_does_not_reconstruct_cleanup_prompt_from_public_snapshot(tmp_path: Path) -> None:
    import inspect

    assert "public_snapshot" not in inspect.signature(_cleanup_payload_from_private_ledger_or_unavailable).parameters

    cleanup = _cleanup_payload_from_private_ledger_or_unavailable(
        ledger_path=tmp_path / "missing-cleanup.yaml",
    )

    assert cleanup["status"] == "unavailable"
    assert "statusMessage" in cleanup
    assert "prompt" not in cleanup
    assert "resources" not in cleanup


def test_normal_chat_cleanup_ledger_ignores_observed_only_ledger(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    cwd = tmp_path / "workspace"
    cwd.mkdir()
    session_id = "session-observed-only"
    storage = SessionStorage(projects_dir=tmp_path / "projects")
    ledger = CleanupLedger(storage.session_dir(str(cwd), session_id) / "pipeline" / "cleanup.yaml")
    ledger.record_observed(
        ObservedResource(
            provider="ros",
            resource_type="stack",
            resource_id="stack-success",
            region_id="cn-hangzhou",
            observed_action="CreateStack",
            source_step_id="deploying",
        )
    )

    class StorageFactory:
        repair_interrupted = staticmethod(SessionStorage.repair_interrupted)

        def __call__(self):
            return storage

    monkeypatch.setattr("iac_code.a2a.executor.SessionStorage", StorageFactory())

    assert _cleanup_ledger_for_a2a_normal_chat(cwd=str(cwd), session_id=session_id) is None


def test_normal_chat_cleanup_ledger_recovers_pending_cleanup_without_prompt(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    cwd = tmp_path / "workspace"
    cwd.mkdir()
    session_id = "session-cleanup-required"
    storage = SessionStorage(projects_dir=tmp_path / "projects")
    ledger = CleanupLedger(storage.session_dir(str(cwd), session_id) / "pipeline" / "cleanup.yaml")
    ledger.mark_cleanup_required(
        [
            CleanupResource(
                provider="ros",
                resource_type="stack",
                resource_id="stack-leftover",
                region_id="cn-hangzhou",
                source_step_id="deploying",
            )
        ],
        source_step_id="deploying",
        reason="rollback",
    )

    class StorageFactory:
        repair_interrupted = staticmethod(SessionStorage.repair_interrupted)

        def __call__(self):
            return storage

    monkeypatch.setattr("iac_code.a2a.executor.SessionStorage", StorageFactory())

    recovered = _cleanup_ledger_for_a2a_normal_chat(cwd=str(cwd), session_id=session_id)

    assert recovered is not None
    assert recovered.path == ledger.path


def test_normal_chat_cleanup_ledger_recovers_completed_cleanup_for_legacy_prompt(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    cwd = tmp_path / "workspace"
    cwd.mkdir()
    session_id = "session-cleanup-completed"
    storage = SessionStorage(projects_dir=tmp_path / "projects")
    ledger = CleanupLedger(storage.session_dir(str(cwd), session_id) / "pipeline" / "cleanup.yaml")
    ledger.mark_cleanup_required(
        [
            CleanupResource(
                provider="ros",
                resource_type="stack",
                resource_id="stack-deleted",
                region_id="cn-hangzhou",
                source_step_id="deploying",
            )
        ],
        source_step_id="deploying",
        reason="rollback",
    )
    ledger.update_resource(
        provider="ros",
        resource_type="stack",
        resource_id="stack-deleted",
        region_id="cn-hangzhou",
        cleanup_status="completed",
        progress_status="DELETE_COMPLETE",
    )
    storage.append(str(cwd), session_id, create_cleanup_prompt_message("legacy cleanup prompt without ledger path"))

    class StorageFactory:
        repair_interrupted = staticmethod(SessionStorage.repair_interrupted)

        def __call__(self):
            return storage

    monkeypatch.setattr("iac_code.a2a.executor.SessionStorage", StorageFactory())

    recovered = _cleanup_ledger_for_a2a_normal_chat(cwd=str(cwd), session_id=session_id)

    assert recovered is not None
    assert recovered.path == ledger.path


@pytest.mark.asyncio
async def test_pipeline_handoff_context_backfills_summary_without_cleanup_prompt(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    cwd = tmp_path / "workspace"
    cwd.mkdir()
    session_id = "session-cleanup"
    context_id = "ctx-cleanup"
    pipeline_dir = tmp_path / "pipeline"
    ledger_path = tmp_path / "cleanup.yaml"
    storage = SessionStorage(projects_dir=tmp_path / "projects")
    prompt = "cleanup prompt for stack-123"

    A2APipelineSnapshotStore(pipeline_dir).save(
        {
            "normalHandoff": {
                "action": "switch_to_normal",
                "targetMode": "normal",
                "summary": "[Pipeline Handoff Context]\nPipeline: selling",
                "data": {
                    "cleanup": {
                        "status": "pending",
                        "resourceCount": 1,
                        "statusMessage": "检测到 1 个回滚残留资源，开始清理流程。",
                        "prompt": prompt,
                        "ledgerPath": str(ledger_path),
                    }
                },
            }
        }
    )

    class StorageFactory:
        repair_interrupted = staticmethod(SessionStorage.repair_interrupted)

        def __call__(self):
            return storage

    monkeypatch.setattr("iac_code.a2a.executor.SessionStorage", StorageFactory())
    monkeypatch.setattr("iac_code.a2a.executor.existing_a2a_pipeline_dir_for_session", lambda **kwargs: pipeline_dir)

    executor = IacCodeA2AExecutor.__new__(IacCodeA2AExecutor)
    executor._task_store = _TaskStore(cwd=str(cwd), session_id=session_id)

    await executor._ensure_pipeline_handoff_context_in_session(context_id=context_id, cwd=str(cwd))
    await executor._ensure_pipeline_handoff_context_in_session(context_id=context_id, cwd=str(cwd))

    messages = storage.load(str(cwd), session_id)
    assert [message.content for message in messages] == [
        "[Pipeline Handoff Context]\nPipeline: selling",
    ]
    assert not any(message.content == prompt for message in messages)
    assert not any(message.metadata.get("type") == CLEANUP_PROMPT_METADATA_TYPE for message in messages)


@pytest.mark.asyncio
async def test_normal_a2a_turn_updates_pipeline_cleanup_ledger(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    cwd = tmp_path / "workspace"
    cwd.mkdir()
    session_id = "session-cleanup"
    context_id = "ctx-cleanup"
    pipeline_task_id = "task-pipeline"
    normal_task_id = "task-normal-cleanup"
    storage = SessionStorage(projects_dir=tmp_path / "projects")
    pipeline_dir = storage.session_dir(str(cwd), session_id) / "a2a" / "pipeline"
    ledger = CleanupLedger(storage.session_dir(str(cwd), session_id) / "pipeline" / "cleanup.yaml")
    ledger.mark_cleanup_required(
        [
            CleanupResource(
                provider="ros",
                resource_type="stack",
                resource_id="stack-123",
                region_id="cn-hangzhou",
                source_step_id="deploying",
            )
        ],
        source_step_id="deploying",
        reason="rollback",
    )
    cleanup_prompt = ledger.build_pending_prompt()
    assert cleanup_prompt is not None
    storage.append(
        str(cwd),
        session_id,
        create_cleanup_prompt_message(
            cleanup_prompt.prompt,
            cleanup_ledger_path=ledger.path,
            cleanup_status="pending",
        ),
    )
    A2APipelineSnapshotStore(pipeline_dir).save(
        {
            "pipelineRunId": context_id,
            "taskId": pipeline_task_id,
            "contextId": context_id,
            "pipelineName": "selling",
            "cleanup": {
                "status": "pending",
                "resourceCount": 1,
                "resources": [{"resourceId": "stack-123", "regionId": "cn-hangzhou"}],
                "history": [],
            },
        }
    )
    loop = FakeAgentLoop(
        [
            ToolUseEndEvent(
                tool_use_id="tool-delete",
                name="aliyun_api",
                input={
                    "product": "ROS",
                    "action": "DeleteStack",
                    "params": {"StackId": "stack-123", "RegionId": "cn-hangzhou"},
                },
            ),
            ToolResultEvent(
                tool_use_id="tool-delete",
                tool_name="aliyun_api",
                result={"StackId": "stack-123"},
                is_error=False,
            ),
            ToolUseEndEvent(
                tool_use_id="tool-get",
                name="aliyun_api",
                input={
                    "product": "ROS",
                    "action": "GetStack",
                    "params": {"StackId": "stack-123", "RegionId": "cn-hangzhou"},
                },
            ),
            ToolResultEvent(
                tool_use_id="tool-get",
                tool_name="aliyun_api",
                result={"Stack": {"StackId": "stack-123", "StackStatus": "DELETE_COMPLETE"}},
                is_error=False,
            ),
            TextDeltaEvent(text="cleanup done"),
        ]
    )

    async def continue_streaming():
        for event in loop.events:
            yield event

    loop.continue_streaming = continue_streaming
    runtime = FakeRuntime(agent_loop=loop, session_id=session_id)
    persistence = A2APersistenceStore(tmp_path / "a2a")
    persistence.save_context(A2AContextSnapshot(context_id=context_id, session_id=session_id, cwd=str(cwd)))

    class StorageFactory:
        repair_interrupted = staticmethod(SessionStorage.repair_interrupted)

        def __call__(self):
            return storage

    monkeypatch.setattr("iac_code.a2a.executor.SessionStorage", StorageFactory())
    monkeypatch.setattr("iac_code.a2a.executor.existing_a2a_pipeline_dir_for_session", lambda **kwargs: pipeline_dir)
    monkeypatch.setattr("iac_code.a2a.executor.create_agent_runtime", lambda options: runtime)

    store = A2ATaskStore(metrics=NoOpA2AMetrics(), persistence=persistence)
    executor = IacCodeA2AExecutor(task_store=store, model="qwen3.6-plus")
    queue = FakeEventQueue()

    await executor.execute(
        FakeRequestContext(
            task_id=normal_task_id,
            context_id=context_id,
            text="continue",
            metadata={"iac_code": {"cwd": str(cwd)}},
        ),
        queue,
    )

    resource = ledger.cleanup_resources()[0]
    assert resource.cleanup_status == "completed"
    assert resource.cleanup_tool_use_id == "tool-get"
    assert resource.progress_status == "DELETE_COMPLETE"
    pipeline_updates = [
        _dump(event)
        for event in queue.events
        if isinstance(event, TaskStatusUpdateEvent)
        and "pipeline" in _dump(event).get("metadata", {}).get("iac_code", {})
    ]
    pipeline_events = [update["metadata"]["iac_code"]["pipeline"] for update in pipeline_updates]
    assert [event["eventType"] for event in pipeline_events] == [
        "cleanup_started",
        "cleanup_progress",
        "cleanup_completed",
    ]
    assert {update["taskId"] for update in pipeline_updates} == {normal_task_id}
    assert {event["taskId"] for event in pipeline_events} == {pipeline_task_id}
    assert {event["deliveryTaskId"] for event in pipeline_events} == {normal_task_id}
    snapshot = A2APipelineSnapshotStore(pipeline_dir).load()
    assert snapshot is not None
    assert snapshot["cleanup"]["status"] == "completed"
    assert snapshot["cleanup"]["resources"][0]["stackStatus"] == "DELETE_COMPLETE"
    messages = storage.load(str(cwd), session_id)
    cleanup_messages = [message for message in messages if message.metadata.get("type") == CLEANUP_PROMPT_METADATA_TYPE]
    assert len(cleanup_messages) == 1
    assert cleanup_messages[0].metadata["cleanupLedgerPath"] == str(ledger.path)
    assert cleanup_messages[0].metadata["cleanupStatus"] == "completed"


class _FlakyCleanupPublisher:
    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.fail_next = True

    async def publish_manual(
        self,
        event_type,
        scope,
        *,
        status="working",
        data=None,
        coordinates=None,
        require_durable_metadata=False,
    ):
        self.calls.append(
            {
                "event_type": event_type,
                "scope": scope,
                "status": status,
                "data": data,
                "coordinates": coordinates,
                "require_durable_metadata": require_durable_metadata,
            }
        )
        if self.fail_next:
            self.fail_next = False
            return None
        return {"eventType": event_type}


class _CatchUpCleanupPublisher:
    def __init__(self, snapshot: dict) -> None:
        self.snapshot_store = SimpleNamespace(load=lambda: snapshot)
        self.calls: list[dict] = []

    async def publish_manual(
        self,
        event_type,
        scope,
        *,
        status="working",
        data=None,
        coordinates=None,
        require_durable_metadata=False,
    ):
        self.calls.append(
            {
                "event_type": event_type,
                "scope": scope,
                "status": status,
                "data": data,
                "coordinates": coordinates,
                "require_durable_metadata": require_durable_metadata,
            }
        )
        return {"eventType": event_type}


class _CleanupContinuationLoop:
    def __init__(self, *, cleanup_stack_id: str = "stack-123") -> None:
        self.run_prompts: list[str] = []
        self.continue_calls = 0
        self.cleanup_stack_id = cleanup_stack_id
        self._run_events: list[object] = [TextDeltaEvent(text="user prompt handled")]
        self.context_manager = _CleanupContextManager()
        self.run_context_snapshots: list[list[Message]] = []
        self.continue_context_snapshots: list[list[Message]] = []

    async def run_streaming(self, prompt: str):
        self.run_prompts.append(prompt)
        self.run_context_snapshots.append(list(self.context_manager.get_messages()))
        for event in self._run_events:
            yield event

    async def continue_streaming(self):
        self.continue_calls += 1
        self.continue_context_snapshots.append(list(self.context_manager.get_messages()))
        yield ToolUseEndEvent(
            tool_use_id="tool-delete",
            name="aliyun_api",
            input={
                "product": "ROS",
                "action": "DeleteStack",
                "params": {"StackId": self.cleanup_stack_id, "RegionId": "cn-hangzhou"},
            },
        )
        yield ToolResultEvent(
            tool_use_id="tool-delete",
            tool_name="aliyun_api",
            result={"StackId": self.cleanup_stack_id, "Status": "DELETE_COMPLETE"},
            is_error=False,
        )


class _TwoStepCleanupContinuationLoop(_CleanupContinuationLoop):
    async def continue_streaming(self):
        self.continue_calls += 1
        self.continue_context_snapshots.append(list(self.context_manager.get_messages()))
        if self.continue_calls == 1:
            if False:
                yield None
            return
        yield ToolUseEndEvent(
            tool_use_id="tool-delete",
            name="aliyun_api",
            input={
                "product": "ROS",
                "action": "DeleteStack",
                "params": {"StackId": self.cleanup_stack_id, "RegionId": "cn-hangzhou"},
            },
        )
        yield ToolResultEvent(
            tool_use_id="tool-delete",
            tool_name="aliyun_api",
            result={"StackId": self.cleanup_stack_id, "Status": "DELETE_COMPLETE"},
            is_error=False,
        )


class _CleanupContextManager:
    def __init__(self) -> None:
        self._messages: list[Message] = []

    def add_raw_message(self, raw_msg):
        message = Message(role=raw_msg["role"], content=raw_msg["content"], metadata=raw_msg.get("metadata", {}))
        self._messages.append(message)
        return message

    def get_messages(self):
        return self._messages

    def remove_cleanup_prompt_messages(self):
        kept = [message for message in self._messages if message.metadata.get("type") != CLEANUP_PROMPT_METADATA_TYPE]
        removed = len(self._messages) - len(kept)
        self._messages = kept
        return removed


@pytest.mark.asyncio
async def test_cleanup_progress_publish_retries_after_none_result(tmp_path: Path) -> None:
    ledger = CleanupLedger(tmp_path / "cleanup.yaml")
    ledger.mark_cleanup_required(
        [
            CleanupResource(
                provider="ros",
                resource_type="stack",
                resource_id="stack-123",
                region_id="cn-hangzhou",
                source_step_id="deploying",
            )
        ],
        source_step_id="deploying",
        reason="rollback",
    )
    previous = _cleanup_resource_states(ledger)
    ledger.update_resource(
        provider="ros",
        resource_type="stack",
        resource_id="stack-123",
        region_id="cn-hangzhou",
        cleanup_status="in_progress",
        progress_status="DELETE_IN_PROGRESS",
        progress_percentage=45,
    )
    publisher = _FlakyCleanupPublisher()

    still_previous = await _publish_cleanup_resource_changes(publisher, ledger, previous)
    advanced = await _publish_cleanup_resource_changes(publisher, ledger, still_previous)

    assert still_previous == previous
    assert advanced != previous
    assert [call["event_type"] for call in publisher.calls] == ["cleanup_progress", "cleanup_progress"]
    assert [call["require_durable_metadata"] for call in publisher.calls] == [True, True]
    assert publisher.calls[1]["data"]["progressPercentage"] == 45


@pytest.mark.asyncio
async def test_cleanup_observer_catches_up_snapshot_after_restart(tmp_path: Path) -> None:
    ledger = CleanupLedger(tmp_path / "cleanup.yaml")
    ledger.mark_cleanup_required(
        [
            CleanupResource(
                provider="ros",
                resource_type="stack",
                resource_id="stack-123",
                region_id="cn-hangzhou",
                source_step_id="deploying",
            )
        ],
        source_step_id="deploying",
        reason="rollback",
    )
    ledger.update_resource(
        provider="ros",
        resource_type="stack",
        resource_id="stack-123",
        region_id="cn-hangzhou",
        cleanup_status="in_progress",
        progress_status="DELETE_IN_PROGRESS",
        progress_percentage=45,
    )
    publisher = _CatchUpCleanupPublisher(
        {
            "cleanup": {
                "resources": [
                    {
                        "provider": "ros",
                        "resourceType": "stack",
                        "resourceId": "stack-123",
                        "regionId": "cn-hangzhou",
                        "cleanupStatus": "pending",
                    }
                ]
            }
        }
    )

    async def empty_stream():
        if False:
            yield None

    async for _event in _observe_cleanup_stream(empty_stream(), ledger, publisher=publisher):
        pass

    assert [call["event_type"] for call in publisher.calls] == ["cleanup_progress"]
    assert publisher.calls[0]["data"]["progressPercentage"] == 45


def test_a2a_cleanup_prune_keeps_prompt_when_cleanup_ledger_is_corrupt(tmp_path: Path) -> None:
    path = tmp_path / "cleanup.yaml"
    path.write_text("[broken", encoding="utf-8")
    ledger = CleanupLedger(path)
    remover = MagicMock()
    runtime = SimpleNamespace(
        agent_loop=SimpleNamespace(
            context_manager=SimpleNamespace(remove_cleanup_prompt_messages=remover),
        )
    )

    _prune_completed_cleanup_prompt_from_runtime(runtime, ledger)

    remover.assert_not_called()


def test_a2a_cleanup_prune_keeps_prompt_when_cleanup_ledger_is_missing() -> None:
    cleanup_message = create_cleanup_prompt_message("cleanup prompt for stack-123")
    context_manager = _CleanupContextManager()
    context_manager.add_raw_message(cleanup_message.to_dict())
    context_manager.remove_cleanup_prompt_messages = MagicMock(wraps=context_manager.remove_cleanup_prompt_messages)
    runtime = SimpleNamespace(agent_loop=SimpleNamespace(context_manager=context_manager))

    _prune_completed_cleanup_prompt_from_runtime(runtime, None)

    context_manager.remove_cleanup_prompt_messages.assert_not_called()


def test_a2a_deferred_cleanup_prompts_keep_latest_meaningful_turn(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(config_dir))
    cwd = tmp_path / "workspace"
    cwd.mkdir()
    session_id = "session-cleanup"
    prompts = [f"blocked prompt {index}" for index in range(25)]

    for prompt in prompts:
        _append_a2a_deferred_cleanup_prompt(cwd=str(cwd), session_id=session_id, prompt=prompt)

    assert _load_a2a_deferred_cleanup_prompts(cwd=str(cwd), session_id=session_id) == [prompts[-1]]


def test_a2a_deferred_cleanup_prompts_do_not_accumulate_repeated_continue_turns(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(config_dir))
    cwd = tmp_path / "workspace"
    cwd.mkdir()
    session_id = "session-cleanup"

    assert _append_a2a_deferred_cleanup_prompt(cwd=str(cwd), session_id=session_id, prompt="continue") is True
    assert _append_a2a_deferred_cleanup_prompt(cwd=str(cwd), session_id=session_id, prompt="continue") is True

    assert _load_a2a_deferred_cleanup_prompts(cwd=str(cwd), session_id=session_id) == ["continue"]


def test_a2a_deferred_cleanup_continue_preserves_existing_meaningful_turn(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(config_dir))
    cwd = tmp_path / "workspace"
    cwd.mkdir()
    session_id = "session-cleanup"

    assert _append_a2a_deferred_cleanup_prompt(cwd=str(cwd), session_id=session_id, prompt="update template") is True
    assert _append_a2a_deferred_cleanup_prompt(cwd=str(cwd), session_id=session_id, prompt="continue") is True

    assert _load_a2a_deferred_cleanup_prompts(cwd=str(cwd), session_id=session_id) == ["update template"]


def test_a2a_deferred_cleanup_prompt_append_does_not_overwrite_corrupt_sidecar(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(config_dir))
    cwd = tmp_path / "workspace"
    cwd.mkdir()
    session_id = "session-cleanup"
    path = _a2a_deferred_cleanup_prompts_path(cwd=str(cwd), session_id=session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{broken", encoding="utf-8")

    assert _append_a2a_deferred_cleanup_prompt(cwd=str(cwd), session_id=session_id, prompt="new prompt") is False

    assert path.read_text(encoding="utf-8") == "{broken"


@pytest.mark.asyncio
async def test_a2a_cleanup_observer_does_not_mutate_corrupt_ledger_or_prune_prompt(tmp_path: Path) -> None:
    path = tmp_path / "cleanup.yaml"
    path.write_text("[broken", encoding="utf-8")
    ledger = CleanupLedger(path)
    remover = MagicMock()
    runtime = SimpleNamespace(
        agent_loop=SimpleNamespace(
            context_manager=SimpleNamespace(remove_cleanup_prompt_messages=remover),
        )
    )

    async def events():
        yield ToolUseEndEvent(
            tool_use_id="toolu-delete",
            name="aliyun_api",
            input={
                "product": "ros",
                "action": "DeleteStack",
                "region_id": "cn-hangzhou",
                "params": {"StackId": "stack-123"},
            },
        )

    async for _event in _observe_cleanup_stream(events(), ledger):
        pass
    _prune_completed_cleanup_prompt_from_runtime(runtime, ledger)

    assert path.exists()
    assert not list(tmp_path.glob("cleanup.yaml.corrupt*"))
    remover.assert_not_called()


def test_cleanup_publisher_falls_back_to_journal_when_snapshot_is_unreadable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    cwd = tmp_path / "workspace"
    cwd.mkdir()
    pipeline_dir = tmp_path / "pipeline"
    pipeline_dir.mkdir()
    (pipeline_dir / "a2a-snapshot.json").write_text("{broken", encoding="utf-8")
    A2APipelineJournal(pipeline_dir).append(
        {
            "schemaVersion": "1.0",
            "eventId": "evt-start",
            "sequence": 1,
            "createdAt": "2026-01-01T00:00:00Z",
            "eventType": "pipeline_started",
            "scope": "pipeline",
            "pipelineRunId": "ctx-cleanup",
            "taskId": "task-pipeline",
            "contextId": "ctx-cleanup",
            "pipelineName": "selling",
            "status": "working",
            "data": {"totalSteps": 1, "stepIds": ["deploying"]},
        }
    )

    monkeypatch.setattr("iac_code.a2a.executor.existing_a2a_pipeline_dir_for_session", lambda **kwargs: pipeline_dir)

    publisher = _cleanup_publisher_for_a2a_normal_chat(
        event_queue=FakeEventQueue(),
        cwd=str(cwd),
        session_id="session-cleanup",
        task_id="task-normal",
        context_id="ctx-cleanup",
        artifact_store=None,
        exposure_types=None,
    )

    assert publisher is not None
    assert publisher.translator._context.task_id == "task-pipeline"
    assert publisher.delivery_task_id == "task-normal"


@pytest.mark.asyncio
async def test_normal_a2a_turn_runs_cleanup_prompt_as_continuation_then_processes_user_prompt(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(config_dir))
    cwd = tmp_path / "workspace"
    cwd.mkdir()
    session_id = "session-cleanup"
    context_id = "ctx-cleanup"
    storage = SessionStorage()
    ledger = CleanupLedger(storage.session_dir(str(cwd), session_id) / "pipeline" / "cleanup.yaml")
    ledger.mark_cleanup_required(
        [
            CleanupResource(
                provider="ros",
                resource_type="stack",
                resource_id="stack-123",
                region_id="cn-hangzhou",
                source_step_id="deploying",
            )
        ],
        source_step_id="deploying",
        reason="rollback",
    )
    cleanup_prompt = ledger.build_pending_prompt()
    assert cleanup_prompt is not None
    storage.append(str(cwd), session_id, create_cleanup_prompt_message(cleanup_prompt.prompt))
    persistence = A2APersistenceStore(tmp_path / "a2a")
    persistence.save_context(A2AContextSnapshot(context_id=context_id, session_id=session_id, cwd=str(cwd)))
    loop = _CleanupContinuationLoop()
    runtime = FakeRuntime(agent_loop=loop, session_id=session_id)

    monkeypatch.setattr("iac_code.a2a.executor.create_agent_runtime", lambda options: runtime)

    store = A2ATaskStore(metrics=NoOpA2AMetrics(), persistence=persistence)
    executor = IacCodeA2AExecutor(task_store=store, model="qwen3.6-plus")

    await executor.execute(
        FakeRequestContext(
            task_id="task-normal",
            context_id=context_id,
            text="user follow-up",
            metadata={"iac_code": {"cwd": str(cwd)}},
        ),
        FakeEventQueue(),
    )

    assert loop.continue_calls == 1
    assert loop.run_prompts == ["user follow-up"]
    assert not any(
        message.metadata.get("type") == CLEANUP_PROMPT_METADATA_TYPE for message in loop.run_context_snapshots[0]
    )
    assert ledger.cleanup_resources()[0].cleanup_status == "completed"


@pytest.mark.asyncio
async def test_normal_a2a_turn_injects_cleanup_prompt_from_ledger_before_cleanup_continuation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(config_dir))
    cwd = tmp_path / "workspace"
    cwd.mkdir()
    session_id = "session-cleanup"
    context_id = "ctx-cleanup"
    storage = SessionStorage()
    ledger = CleanupLedger(storage.session_dir(str(cwd), session_id) / "pipeline" / "cleanup.yaml")
    ledger.mark_cleanup_required(
        [
            CleanupResource(
                provider="ros",
                resource_type="stack",
                resource_id="stack-123",
                region_id="cn-hangzhou",
                source_step_id="deploying",
            )
        ],
        source_step_id="deploying",
        reason="rollback",
    )
    persistence = A2APersistenceStore(tmp_path / "a2a")
    persistence.save_context(A2AContextSnapshot(context_id=context_id, session_id=session_id, cwd=str(cwd)))
    loop = _CleanupContinuationLoop()
    runtime = FakeRuntime(agent_loop=loop, session_id=session_id)

    monkeypatch.setattr("iac_code.a2a.executor.create_agent_runtime", lambda options: runtime)

    store = A2ATaskStore(metrics=NoOpA2AMetrics(), persistence=persistence)
    executor = IacCodeA2AExecutor(task_store=store, model="qwen3.6-plus")

    await executor.execute(
        FakeRequestContext(
            task_id="task-normal",
            context_id=context_id,
            text="user follow-up",
            metadata={"iac_code": {"cwd": str(cwd)}},
        ),
        FakeEventQueue(),
    )

    messages = storage.load(str(cwd), session_id)
    assert any(message.metadata.get("type") == CLEANUP_PROMPT_METADATA_TYPE for message in messages)
    assert loop.continue_calls == 1
    assert loop.run_prompts == ["user follow-up"]


@pytest.mark.asyncio
async def test_normal_a2a_turn_defers_prompt_until_pending_cleanup_completes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(config_dir))
    cwd = tmp_path / "workspace"
    cwd.mkdir()
    session_id = "session-cleanup"
    context_id = "ctx-cleanup"
    storage = SessionStorage()
    ledger = CleanupLedger(storage.session_dir(str(cwd), session_id) / "pipeline" / "cleanup.yaml")
    ledger.mark_cleanup_required(
        [
            CleanupResource(
                provider="ros",
                resource_type="stack",
                resource_id="stack-123",
                region_id="cn-hangzhou",
                source_step_id="deploying",
            )
        ],
        source_step_id="deploying",
        reason="rollback",
    )
    cleanup_prompt = ledger.build_pending_prompt()
    assert cleanup_prompt is not None
    storage.append(str(cwd), session_id, create_cleanup_prompt_message(cleanup_prompt.prompt))
    persistence = A2APersistenceStore(tmp_path / "a2a")
    persistence.save_context(A2AContextSnapshot(context_id=context_id, session_id=session_id, cwd=str(cwd)))
    loop = _TwoStepCleanupContinuationLoop()
    runtime = FakeRuntime(agent_loop=loop, session_id=session_id)

    monkeypatch.setattr("iac_code.a2a.executor.create_agent_runtime", lambda options: runtime)

    store = A2ATaskStore(metrics=NoOpA2AMetrics(), persistence=persistence)
    executor = IacCodeA2AExecutor(task_store=store, model="qwen3.6-plus")

    await executor.execute(
        FakeRequestContext(
            task_id="task-normal-1",
            context_id=context_id,
            text="update the template after cleanup",
            metadata={"iac_code": {"cwd": str(cwd)}},
        ),
        FakeEventQueue(),
    )

    assert loop.continue_calls == 1
    assert loop.run_prompts == []
    assert _load_a2a_deferred_cleanup_prompts(cwd=str(cwd), session_id=session_id) == [
        "update the template after cleanup"
    ]

    await executor.execute(
        FakeRequestContext(
            task_id="task-normal-2",
            context_id=context_id,
            text="continue",
            metadata={"iac_code": {"cwd": str(cwd)}},
        ),
        FakeEventQueue(),
    )

    assert loop.continue_calls == 2
    assert loop.run_prompts == ["update the template after cleanup"]
    assert _load_a2a_deferred_cleanup_prompts(cwd=str(cwd), session_id=session_id) == []
    assert not any(
        message.metadata.get("type") == CLEANUP_PROMPT_METADATA_TYPE for message in loop.run_context_snapshots[0]
    )
    assert ledger.cleanup_resources()[0].cleanup_status == "completed"


@pytest.mark.asyncio
async def test_normal_a2a_turn_does_not_overwrite_corrupt_deferred_prompt_sidecar(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(config_dir))
    cwd = tmp_path / "workspace"
    cwd.mkdir()
    session_id = "session-cleanup"
    context_id = "ctx-cleanup"
    storage = SessionStorage()
    ledger = CleanupLedger(storage.session_dir(str(cwd), session_id) / "pipeline" / "cleanup.yaml")
    ledger.mark_cleanup_required(
        [
            CleanupResource(
                provider="ros",
                resource_type="stack",
                resource_id="stack-123",
                region_id="cn-hangzhou",
                source_step_id="deploying",
            )
        ],
        source_step_id="deploying",
        reason="rollback",
    )
    cleanup_prompt = ledger.build_pending_prompt()
    assert cleanup_prompt is not None
    storage.append(str(cwd), session_id, create_cleanup_prompt_message(cleanup_prompt.prompt))
    deferred_path = _a2a_deferred_cleanup_prompts_path(cwd=str(cwd), session_id=session_id)
    deferred_path.parent.mkdir(parents=True, exist_ok=True)
    deferred_path.write_text("{broken", encoding="utf-8")
    persistence = A2APersistenceStore(tmp_path / "a2a")
    persistence.save_context(A2AContextSnapshot(context_id=context_id, session_id=session_id, cwd=str(cwd)))
    loop = _TwoStepCleanupContinuationLoop()
    runtime = FakeRuntime(agent_loop=loop, session_id=session_id)

    monkeypatch.setattr("iac_code.a2a.executor.create_agent_runtime", lambda options: runtime)

    store = A2ATaskStore(metrics=NoOpA2AMetrics(), persistence=persistence)
    executor = IacCodeA2AExecutor(task_store=store, model="qwen3.6-plus")

    await executor.execute(
        FakeRequestContext(
            task_id="task-normal",
            context_id=context_id,
            text="update the template after cleanup",
            metadata={"iac_code": {"cwd": str(cwd)}},
        ),
        FakeEventQueue(),
    )

    assert loop.continue_calls == 1
    assert loop.run_prompts == []
    assert deferred_path.read_text(encoding="utf-8") == "{broken"
    task = await store.get_or_create_task(task_id="task-normal", context_id=context_id)
    assert "deferred prompt state is unavailable" in "".join(task.output_text)


@pytest.mark.asyncio
async def test_normal_a2a_turn_blocks_agent_execution_when_cleanup_ledger_is_corrupt_with_prompt(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(config_dir))
    cwd = tmp_path / "workspace"
    cwd.mkdir()
    session_id = "session-cleanup"
    context_id = "ctx-cleanup"
    storage = SessionStorage()
    ledger_path = storage.session_dir(str(cwd), session_id) / "pipeline" / "cleanup.yaml"
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    ledger_path.write_text("[broken", encoding="utf-8")
    cleanup_message = create_cleanup_prompt_message("cleanup prompt for stack-123")
    storage.append(str(cwd), session_id, cleanup_message)
    persistence = A2APersistenceStore(tmp_path / "a2a")
    persistence.save_context(A2AContextSnapshot(context_id=context_id, session_id=session_id, cwd=str(cwd)))
    loop = _CleanupContinuationLoop()
    loop.context_manager.add_raw_message(cleanup_message.to_dict())
    runtime = FakeRuntime(agent_loop=loop, session_id=session_id)

    monkeypatch.setattr("iac_code.a2a.executor.create_agent_runtime", lambda options: runtime)

    store = A2ATaskStore(metrics=NoOpA2AMetrics(), persistence=persistence)
    executor = IacCodeA2AExecutor(task_store=store, model="qwen3.6-plus")

    await executor.execute(
        FakeRequestContext(
            task_id="task-normal",
            context_id=context_id,
            text="user follow-up",
            metadata={"iac_code": {"cwd": str(cwd)}},
        ),
        FakeEventQueue(),
    )

    assert loop.continue_calls == 0
    assert loop.run_prompts == []
    task = await store.get_or_create_task(task_id="task-normal", context_id=context_id)
    assert "cleanup state is unavailable" in "".join(task.output_text)


@pytest.mark.asyncio
async def test_normal_a2a_turn_blocks_agent_execution_when_cleanup_ledger_is_missing_with_prompt(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(config_dir))
    cwd = tmp_path / "workspace"
    cwd.mkdir()
    session_id = "session-cleanup"
    context_id = "ctx-cleanup"
    storage = SessionStorage()
    cleanup_message = create_cleanup_prompt_message("cleanup prompt for stack-123")
    storage.append(str(cwd), session_id, cleanup_message)
    persistence = A2APersistenceStore(tmp_path / "a2a")
    persistence.save_context(A2AContextSnapshot(context_id=context_id, session_id=session_id, cwd=str(cwd)))
    loop = _CleanupContinuationLoop()
    loop.context_manager.add_raw_message(cleanup_message.to_dict())
    runtime = FakeRuntime(agent_loop=loop, session_id=session_id)

    monkeypatch.setattr("iac_code.a2a.executor.create_agent_runtime", lambda options: runtime)

    store = A2ATaskStore(metrics=NoOpA2AMetrics(), persistence=persistence)
    executor = IacCodeA2AExecutor(task_store=store, model="qwen3.6-plus")

    await executor.execute(
        FakeRequestContext(
            task_id="task-normal",
            context_id=context_id,
            text="user follow-up",
            metadata={"iac_code": {"cwd": str(cwd)}},
        ),
        FakeEventQueue(),
    )

    assert loop.continue_calls == 0
    assert loop.run_prompts == []
    cleanup_messages = [
        message
        for message in loop.context_manager.get_messages()
        if message.metadata.get("type") == CLEANUP_PROMPT_METADATA_TYPE
    ]
    assert cleanup_messages
    task = await store.get_or_create_task(task_id="task-normal", context_id=context_id)
    assert "cleanup state is unavailable" in "".join(task.output_text)


@pytest.mark.asyncio
async def test_normal_a2a_turn_replaces_stale_cleanup_prompt_before_partial_cleanup_continuation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(config_dir))
    cwd = tmp_path / "workspace"
    cwd.mkdir()
    session_id = "session-cleanup"
    context_id = "ctx-cleanup"
    storage = SessionStorage()
    ledger = CleanupLedger(storage.session_dir(str(cwd), session_id) / "pipeline" / "cleanup.yaml")
    ledger.mark_cleanup_required(
        [
            CleanupResource(
                provider="ros",
                resource_type="stack",
                resource_id="stack-done",
                region_id="cn-hangzhou",
                source_step_id="deploying",
            ),
            CleanupResource(
                provider="ros",
                resource_type="stack",
                resource_id="stack-pending",
                region_id="cn-hangzhou",
                source_step_id="deploying",
            ),
        ],
        source_step_id="deploying",
        reason="rollback",
    )
    stale_prompt = ledger.build_pending_prompt()
    assert stale_prompt is not None
    stale_message = create_cleanup_prompt_message(stale_prompt.prompt)
    storage.append(str(cwd), session_id, stale_message)
    ledger.update_resource(
        provider="ros",
        resource_type="stack",
        resource_id="stack-done",
        region_id="cn-hangzhou",
        cleanup_status="completed",
        progress_status="DELETE_COMPLETE",
    )
    persistence = A2APersistenceStore(tmp_path / "a2a")
    persistence.save_context(A2AContextSnapshot(context_id=context_id, session_id=session_id, cwd=str(cwd)))
    loop = _CleanupContinuationLoop(cleanup_stack_id="stack-pending")
    loop.context_manager.add_raw_message(stale_message.to_dict())
    runtime = FakeRuntime(agent_loop=loop, session_id=session_id)

    monkeypatch.setattr("iac_code.a2a.executor.create_agent_runtime", lambda options: runtime)

    store = A2ATaskStore(metrics=NoOpA2AMetrics(), persistence=persistence)
    executor = IacCodeA2AExecutor(task_store=store, model="qwen3.6-plus")

    await executor.execute(
        FakeRequestContext(
            task_id="task-normal",
            context_id=context_id,
            text="user follow-up",
            metadata={"iac_code": {"cwd": str(cwd)}},
        ),
        FakeEventQueue(),
    )

    cleanup_messages = [
        message
        for message in loop.continue_context_snapshots[0]
        if message.metadata.get("type") == CLEANUP_PROMPT_METADATA_TYPE
    ]
    assert len(cleanup_messages) == 1
    assert "stack-pending" in cleanup_messages[0].content
    assert "stack-done" not in cleanup_messages[0].content
