import asyncio
import base64
import json
import logging
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from a2a.types import Task, TaskStatusUpdateEvent
from a2a.utils.errors import InvalidParamsError
from google.protobuf.json_format import MessageToDict

from iac_code.a2a.backup import backup_session_async
from iac_code.a2a.executor import IacCodeA2AExecutor, _normal_handoff_has_backup_ack
from iac_code.a2a.exposure import A2AExposureType
from iac_code.a2a.input_required import PermissionResponse
from iac_code.a2a.metrics import NoOpA2AMetrics
from iac_code.a2a.persistence import A2AContextSnapshot, A2APersistenceStore, A2ATaskSnapshot
from iac_code.a2a.pipeline_executor import recoverable_task_id_from_sidecar
from iac_code.a2a.pipeline_journal import A2APipelineJournal
from iac_code.a2a.pipeline_paths import a2a_pipeline_dir_for_session
from iac_code.a2a.pipeline_snapshot import A2APipelineSnapshotStore, reduce_pipeline_events
from iac_code.a2a.task_store import A2ATaskStore
from iac_code.agent.message import ImageBlock, Message, TextBlock
from iac_code.commands.registry import CommandRegistry, PromptCommand
from iac_code.mcp.errors import MCPConnectionError, MCPNeedsAuthError
from iac_code.mcp.prompts import register_mcp_prompt_commands
from iac_code.mcp.types import (
    MCPConfigScope,
    MCPConnectionMetadata,
    MCPConnectionState,
    MCPServerConfig,
    ScopedMCPServerConfig,
)
from iac_code.pipeline.engine.user_input import PipelineUserInput
from iac_code.services.permission_wait import PermissionExecutionIdentity, RecoveredPermissionAuditBoundary
from iac_code.services.session_backup import (
    BACKUP_STATE_FILENAME,
    BackupReason,
    BackupResult,
    SessionBackupBlocked,
    SessionBackupService,
    SessionReconcileResult,
)
from iac_code.services.session_backup_staging import StagedSessionBackupService
from iac_code.services.session_backup_state import NORMAL_HANDOFF_PROOF_KEY, BackupPublicationProof
from iac_code.services.session_storage import SessionStorage
from iac_code.services.telemetry import get_user_id
from iac_code.skills.frontmatter import SkillFrontmatter
from iac_code.skills.skill_definition import SkillDefinition
from iac_code.types.skill_source import SkillSource
from iac_code.types.stream_events import (
    MessageEndEvent,
    MessageStartEvent,
    PermissionRequestEvent,
    TextDeltaEvent,
    ToolResultEvent,
    Usage,
)

from .fakes import FakeAgentLoop, FakeEventQueue, FakeRequestContext, FakeRuntime, pending_future


def dump(event):
    return MessageToDict(event, preserving_proto_field_name=False)


def _image_only_pipeline_input() -> PipelineUserInput:
    return PipelineUserInput(
        content=[ImageBlock(media_type="image/png", data="aGVsbG8=")],
        display_text="[Image input]",
        has_images=True,
    )


def _ensure_v2_session(cwd: str, session_id: str) -> Path:
    return SessionStorage().ensure_v2_session_dir_for_new_session(cwd, session_id)


def _committed_normal_handoff_events(
    *,
    context_id: str,
    task_id: str,
    summary: str,
    extra_data: dict | None = None,
) -> tuple[dict, dict]:
    handoff_data = {
        "action": "switch_to_normal",
        "targetMode": "normal",
        "summary": summary,
        **(extra_data or {}),
    }
    handoff = {
        "schemaVersion": "1.0",
        "eventId": "{}-handoff".format(task_id),
        "sequence": 1,
        "createdAt": "2026-01-01T00:00:00Z",
        "eventType": "pipeline_handoff_ready",
        "scope": "pipeline",
        "pipelineRunId": context_id,
        "taskId": task_id,
        "contextId": context_id,
        "pipelineName": "selling",
        "status": "completed",
        "visibility": "committed",
        "data": handoff_data,
    }
    backup_ack = {
        "schemaVersion": "1.0",
        "eventId": "{}-backup-committed".format(task_id),
        "sequence": 2,
        "createdAt": "2026-01-01T00:00:01Z",
        "eventType": "backup_committed",
        "scope": "pipeline",
        "pipelineRunId": context_id,
        "taskId": task_id,
        "contextId": context_id,
        "pipelineName": "selling",
        "status": "completed",
        "data": {
            "committedEventId": handoff["eventId"],
            "committedSequence": handoff["sequence"],
            "committedEventType": handoff["eventType"],
        },
    }
    return handoff, backup_ack


class FailingBackupService:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, BackupReason, bool]] = []

    def backup_session(self, cwd: str, session_id: str, *, reason: BackupReason, critical: bool) -> None:
        self.calls.append((cwd, session_id, reason, critical))
        raise RuntimeError("backup destination unavailable")


@pytest.mark.asyncio
async def test_pipeline_request_reconciles_before_route_and_task_recovery(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("IAC_CODE_MODE", "pipeline")
    cwd = tmp_path / "workspace"
    cwd.mkdir()
    persistence = A2APersistenceStore(tmp_path / "a2a")
    persistence.save_context(A2AContextSnapshot(context_id="ctx-1", session_id="session-1", cwd=str(cwd)))
    calls: list[str] = []

    class RecordingBackupService:
        def reconcile_session(self, *_args, **_kwargs) -> SessionReconcileResult:
            calls.append("reconcile")
            return SessionReconcileResult(enabled=True, action="current")

    class RecordingPipelineExecutor:
        def __init__(self, **_kwargs) -> None:
            pass

        async def execute(self, **_kwargs) -> None:
            calls.append("pipeline")

    store = A2ATaskStore(metrics=NoOpA2AMetrics(), persistence=persistence)
    executor = IacCodeA2AExecutor(
        task_store=store,
        model="qwen3.6-plus",
        backup_service=RecordingBackupService(),
    )

    async def route(**_kwargs) -> bool:
        calls.append("route")
        return False

    async def recover(**_kwargs) -> None:
        calls.append("recover-task")
        return None

    monkeypatch.setattr(executor, "_should_route_pipeline_handoff_to_normal", route)
    monkeypatch.setattr(executor, "_recoverable_pipeline_task_id_for_context", recover)
    monkeypatch.setattr("iac_code.a2a.executor.IacCodeA2APipelineExecutor", RecordingPipelineExecutor)

    await executor.execute(
        FakeRequestContext(task_id="", context_id="ctx-1", text="deploy", metadata={"iac_code": {"cwd": str(cwd)}}),
        FakeEventQueue(),
    )

    assert calls[:3] == ["reconcile", "route", "recover-task"]
    assert await store.context_reconciliation_is_blocked("ctx-1") is False


@pytest.mark.asyncio
async def test_pipeline_request_waits_for_active_execution_then_reconciles(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("IAC_CODE_MODE", "pipeline")
    cwd = tmp_path / "workspace"
    cwd.mkdir()
    persistence = A2APersistenceStore(tmp_path / "a2a")
    persistence.save_context(A2AContextSnapshot(context_id="ctx-1", session_id="session-1", cwd=str(cwd)))
    calls: list[str] = []

    class RecordingBackupService:
        def reconcile_session(self, *_args, **_kwargs) -> SessionReconcileResult:
            calls.append("reconcile")
            return SessionReconcileResult(enabled=True, action="current")

    class RecordingPipelineExecutor:
        def __init__(self, **_kwargs) -> None:
            pass

        async def execute(self, **_kwargs) -> None:
            calls.append("pipeline")

    store = A2ATaskStore(metrics=NoOpA2AMetrics(), persistence=persistence)
    active_record = await store.get_or_create_task(task_id="task-active", context_id="ctx-1")
    release_active = asyncio.Event()

    async def hold_active_execution() -> None:
        await release_active.wait()

    active_execution = asyncio.create_task(hold_active_execution())
    active_record.active_task = active_execution
    executor = IacCodeA2AExecutor(
        task_store=store,
        model="qwen3.6-plus",
        backup_service=RecordingBackupService(),
    )

    async def route_to_pipeline(**_kwargs) -> bool:
        return False

    monkeypatch.setattr(executor, "_should_route_pipeline_handoff_to_normal", route_to_pipeline)
    monkeypatch.setattr("iac_code.a2a.executor.IacCodeA2APipelineExecutor", RecordingPipelineExecutor)

    request = asyncio.create_task(
        executor.execute(
            FakeRequestContext(
                task_id="task-next",
                context_id="ctx-1",
                text="next",
                metadata={"iac_code": {"cwd": str(cwd)}},
            ),
            FakeEventQueue(),
        )
    )
    await asyncio.sleep(0)
    calls_while_active = list(calls)
    release_active.set()
    await active_execution
    await request

    assert calls_while_active == []
    assert calls == ["reconcile", "pipeline"]


@pytest.mark.asyncio
async def test_active_pipeline_followup_does_not_start_new_pipeline_after_owner_finishes(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("IAC_CODE_MODE", "pipeline")
    cwd = tmp_path / "workspace"
    cwd.mkdir()
    persistence = A2APersistenceStore(tmp_path / "a2a")
    persistence.save_context(A2AContextSnapshot(context_id="ctx-1", session_id="session-1", cwd=str(cwd)))
    active_only_values: list[bool] = []

    class RecordingPipelineExecutor:
        def __init__(self, **_kwargs) -> None:
            pass

        async def execute(self, **kwargs) -> bool:
            active_only_values.append(bool(kwargs.get("active_followup_only")))
            return False

    store = A2ATaskStore(metrics=NoOpA2AMetrics(), persistence=persistence)
    task = await store.get_or_create_task(task_id="task-1", context_id="ctx-1")
    release_active = asyncio.Event()

    async def hold_active_execution() -> None:
        await release_active.wait()

    active_execution = asyncio.create_task(hold_active_execution())
    task.active_task = active_execution
    executor = IacCodeA2AExecutor(task_store=store, model="qwen3.6-plus")

    async def finish_before_route(**_kwargs) -> bool:
        release_active.set()
        await active_execution
        return False

    monkeypatch.setattr(executor, "_should_route_pipeline_handoff_to_normal", finish_before_route)
    monkeypatch.setattr("iac_code.a2a.executor.IacCodeA2APipelineExecutor", RecordingPipelineExecutor)

    await executor.execute(
        FakeRequestContext(
            task_id="task-1",
            context_id="ctx-1",
            text="pause",
            metadata={"iac_code": {"cwd": str(cwd)}},
        ),
        FakeEventQueue(),
    )

    assert active_only_values == [True]


@pytest.mark.asyncio
async def test_stale_sandbox_reconciles_committed_handoff_before_routing(monkeypatch, tmp_path) -> None:
    backup_root = tmp_path / "backup"
    cwd = tmp_path / "workspace"
    cwd.mkdir()
    context_id = "ctx-cross-sandbox"
    session_id = "session-cross-sandbox"
    task_id = "task-pipeline"
    monkeypatch.setenv("IAC_CODE_CONFIG_BACKUP_DIR", str(backup_root))

    sandbox_1_config = tmp_path / "sandbox-1"
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(sandbox_1_config))
    storage_1 = SessionStorage(projects_dir=sandbox_1_config / "projects")
    storage_1.ensure_v2_session_dir_for_new_session(str(cwd), session_id)
    session_1 = storage_1.session_dir(str(cwd), session_id)
    (session_1 / "a2a").mkdir()
    (session_1 / "a2a" / "context.json").write_text(
        json.dumps(
            A2AContextSnapshot(
                context_id=context_id,
                session_id=session_id,
                cwd=str(cwd),
                active_task_id=task_id,
            ).__dict__
        ),
        encoding="utf-8",
    )
    service_1 = SessionBackupService(storage_1, retry_delays=())
    service_1.initialize_session(str(cwd), session_id)
    service_1.backup_session(str(cwd), session_id, reason=BackupReason.INPUT_REQUIRED, critical=True)

    persistence = A2APersistenceStore(tmp_path / "a2a-persistence")
    persistence.save_context(
        A2AContextSnapshot(
            context_id=context_id,
            session_id=session_id,
            cwd=str(cwd),
            active_task_id=task_id,
        )
    )
    stale_runtime = SimpleNamespace(closed=False)

    async def close_stale_runtime() -> None:
        stale_runtime.closed = True

    stale_runtime.aclose = close_stale_runtime
    store = A2ATaskStore(metrics=NoOpA2AMetrics(), persistence=persistence)
    stale_context = await store.get_or_create_context(
        context_id=context_id,
        cwd=str(cwd),
        runtime_factory=lambda _session_id: stale_runtime,
    )
    stale_context.active_task_id = task_id
    store.mirror_context(stale_context)

    sandbox_2_config = tmp_path / "sandbox-2"
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(sandbox_2_config))
    storage_2 = SessionStorage(projects_dir=sandbox_2_config / "projects")
    service_2 = SessionBackupService(storage_2, retry_delays=())
    service_2.reconcile_session(str(cwd), session_id)
    handoff_data = {"action": "switch_to_normal", "targetMode": "normal", "summary": "handoff"}
    pending_handoff = {
        "schemaVersion": "1.0",
        "eventId": "event-handoff-pending",
        "sequence": 7,
        "eventType": "pipeline_handoff_ready",
        "scope": "pipeline",
        "pipelineRunId": context_id,
        "taskId": task_id,
        "contextId": context_id,
        "pipelineName": "selling",
        "status": "completed",
        "visibility": "pending_backup",
        "data": handoff_data,
    }
    handoff = {
        "schemaVersion": "1.0",
        "eventId": "event-handoff",
        "sequence": 8,
        "eventType": "pipeline_handoff_ready",
        "scope": "pipeline",
        "pipelineRunId": context_id,
        "taskId": task_id,
        "contextId": context_id,
        "pipelineName": "selling",
        "status": "completed",
        "visibility": "committed",
        "data": handoff_data,
    }
    pipeline_dir = a2a_pipeline_dir_for_session(cwd=str(cwd), session_id=session_id)
    journal = A2APipelineJournal(pipeline_dir)
    journal.append(pending_handoff, durable=True)
    journal.append(handoff, durable=True)
    snapshot = reduce_pipeline_events([pending_handoff, handoff])
    assert snapshot["normalHandoff"] is None
    assert snapshot["pendingNormalHandoff"]["eventId"] == pending_handoff["eventId"]
    A2APipelineSnapshotStore(pipeline_dir).save(snapshot)
    service_2.backup_session(
        str(cwd),
        session_id,
        reason=BackupReason.HANDOFF_READY,
        critical=True,
        publication_proofs={NORMAL_HANDOFF_PROOF_KEY: BackupPublicationProof.from_envelope(handoff)},
    )

    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(sandbox_1_config))
    executor = IacCodeA2AExecutor(task_store=store, model="qwen3.6-plus", backup_service=service_1)

    result = await executor._reconcile_session_before_route(context_id=context_id, cwd=str(cwd))

    assert result is not None
    assert result.action == "restored"
    assert result.state is not None and result.state.generation == 2
    assert stale_runtime.closed is True
    assert store._contexts[context_id].active_task_id is None
    restored_pipeline_dir = a2a_pipeline_dir_for_session(cwd=str(cwd), session_id=session_id)
    restored_events = A2APipelineJournal(restored_pipeline_dir).read_all()
    restored_snapshot = A2APipelineSnapshotStore(restored_pipeline_dir).load()
    assert restored_events == [pending_handoff, handoff]
    assert restored_snapshot is not None
    assert restored_snapshot["normalHandoff"] is None
    assert restored_snapshot["pendingNormalHandoff"]["eventId"] == pending_handoff["eventId"]
    assert executor._normal_handoff_has_state_proof(cwd=str(cwd), session_id=session_id, state=result.state) is True
    assert await executor._should_route_pipeline_handoff_to_normal(context_id=context_id, cwd=str(cwd)) is True


@pytest.mark.asyncio
async def test_current_generation_proof_clears_stale_active_task_and_runtime(monkeypatch, tmp_path) -> None:
    backup_root = tmp_path / "backup"
    config_dir = tmp_path / "sandbox"
    cwd = tmp_path / "workspace"
    cwd.mkdir()
    context_id = "ctx-current-handoff"
    session_id = "session-current-handoff"
    task_id = "task-pipeline"
    monkeypatch.setenv("IAC_CODE_CONFIG_BACKUP_DIR", str(backup_root))
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(config_dir))
    storage = SessionStorage(projects_dir=config_dir / "projects")
    storage.ensure_v2_session_dir_for_new_session(str(cwd), session_id)
    session_dir = storage.session_dir(str(cwd), session_id)
    (session_dir / "a2a").mkdir()
    context_snapshot = A2AContextSnapshot(
        context_id=context_id,
        session_id=session_id,
        cwd=str(cwd),
        active_task_id=task_id,
    )
    (session_dir / "a2a" / "context.json").write_text(json.dumps(context_snapshot.__dict__), encoding="utf-8")
    service = SessionBackupService(storage, retry_delays=())
    service.initialize_session(str(cwd), session_id)
    handoff_data = {"action": "switch_to_normal", "targetMode": "normal", "summary": "handoff"}
    pending_handoff = {
        "schemaVersion": "1.0",
        "eventId": "event-handoff-pending",
        "sequence": 7,
        "eventType": "pipeline_handoff_ready",
        "scope": "pipeline",
        "pipelineRunId": context_id,
        "taskId": task_id,
        "contextId": context_id,
        "pipelineName": "selling",
        "status": "completed",
        "visibility": "pending_backup",
        "data": handoff_data,
    }
    committed_handoff = {**pending_handoff, "eventId": "event-handoff", "sequence": 8, "visibility": "committed"}
    pipeline_dir = a2a_pipeline_dir_for_session(cwd=str(cwd), session_id=session_id)
    journal = A2APipelineJournal(pipeline_dir)
    journal.append(pending_handoff, durable=True)
    journal.append(committed_handoff, durable=True)
    A2APipelineSnapshotStore(pipeline_dir).save(reduce_pipeline_events([pending_handoff, committed_handoff]))
    service.backup_session(
        str(cwd),
        session_id,
        reason=BackupReason.HANDOFF_READY,
        critical=True,
        publication_proofs={NORMAL_HANDOFF_PROOF_KEY: BackupPublicationProof.from_envelope(committed_handoff)},
    )

    persistence = A2APersistenceStore(tmp_path / "a2a-persistence")
    persistence.save_context(context_snapshot)
    stale_runtime = SimpleNamespace(closed=False)

    async def close_stale_runtime() -> None:
        stale_runtime.closed = True

    stale_runtime.aclose = close_stale_runtime
    store = A2ATaskStore(metrics=NoOpA2AMetrics(), persistence=persistence)
    context = await store.get_or_create_context(
        context_id=context_id,
        cwd=str(cwd),
        runtime_factory=lambda _session_id: stale_runtime,
    )
    context.active_task_id = task_id
    store.mirror_context(context)
    executor = IacCodeA2AExecutor(task_store=store, model="qwen3.6-plus", backup_service=service)

    result = await executor._reconcile_session_before_route(context_id=context_id, cwd=str(cwd))

    assert result is not None and result.action == "current"
    assert result.payload_changed is False
    assert stale_runtime.closed is True
    assert store._contexts[context_id].active_task_id is None
    assert persistence.load_context(context_id).active_task_id is None
    assert await executor._should_route_pipeline_handoff_to_normal(context_id=context_id, cwd=str(cwd)) is True


@pytest.mark.asyncio
async def test_reconcile_rejects_running_context_task_before_filesystem_mutation(tmp_path) -> None:
    cwd = tmp_path / "workspace"
    cwd.mkdir()
    calls: list[str] = []

    class RecordingBackupService:
        def reconcile_session(self, *_args, **_kwargs) -> SessionReconcileResult:
            calls.append("reconcile")
            return SessionReconcileResult(enabled=True, action="current")

    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    context = await store.get_or_create_context(
        context_id="ctx-active",
        cwd=str(cwd),
        runtime_factory=lambda _session_id: FakeRuntime(),
    )
    task = await store.get_or_create_task(task_id="task-active", context_id=context.context_id)
    running = asyncio.create_task(asyncio.sleep(60))
    task.active_task = running
    context.active_task_id = task.task_id
    store.mirror_context(context)
    executor = IacCodeA2AExecutor(
        task_store=store,
        model="qwen3.6-plus",
        backup_service=RecordingBackupService(),
    )

    try:
        with pytest.raises(ValueError, match="A2A context not found"):
            await executor._reconcile_session_before_route(context_id=context.context_id, cwd=str(cwd))
    finally:
        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await running

    assert calls == []


@pytest.mark.asyncio
async def test_reconcile_rejects_context_execution_start_before_filesystem_mutation(tmp_path) -> None:
    cwd = tmp_path / "workspace"
    cwd.mkdir()
    calls: list[str] = []

    class RecordingBackupService:
        def reconcile_session(self, *_args, **_kwargs) -> SessionReconcileResult:
            calls.append("reconcile")
            return SessionReconcileResult(enabled=True, action="current")

    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    context = await store.get_or_create_context(
        context_id="ctx-starting",
        cwd=str(cwd),
        runtime_factory=lambda _session_id: FakeRuntime(),
    )
    executor = IacCodeA2AExecutor(
        task_store=store,
        model="qwen3.6-plus",
        backup_service=RecordingBackupService(),
    )
    token = await store.begin_context_execution(context.context_id)

    try:
        with pytest.raises(ValueError, match="A2A context not found"):
            await executor._reconcile_session_before_route(context_id=context.context_id, cwd=str(cwd))
    finally:
        await store.end_context_execution(context.context_id, token)

    assert calls == []


@pytest.mark.asyncio
async def test_reconcile_cancellation_holds_lock_until_background_thread_finishes(tmp_path) -> None:
    cwd = tmp_path / "workspace"
    cwd.mkdir()
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()

    class BlockingBackupService:
        def reconcile_session(self, *_args, **_kwargs) -> SessionReconcileResult:
            started.set()
            try:
                release.wait(timeout=5)
                return SessionReconcileResult(enabled=True, action="current")
            finally:
                finished.set()

    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    context = await store.get_or_create_context(
        context_id="ctx-1",
        cwd=str(cwd),
        runtime_factory=lambda _session_id: FakeRuntime(),
    )
    executor = IacCodeA2AExecutor(
        task_store=store,
        model="qwen3.6-plus",
        backup_service=BlockingBackupService(),
    )
    reconciliation = asyncio.create_task(
        executor._reconcile_session_before_route(context_id=context.context_id, cwd=str(cwd))
    )
    while not started.is_set():
        await asyncio.sleep(0)

    reconciliation.cancel()
    await asyncio.sleep(0.05)
    lock_held_while_thread_runs = store.reconciliation_lock(context.context_id).locked()
    lease_attempt = asyncio.create_task(store.begin_context_execution(context.context_id))
    await asyncio.sleep(0)
    lease_waited_for_thread = not lease_attempt.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await reconciliation
    token = await lease_attempt
    await store.end_context_execution(context.context_id, token)

    assert finished.is_set()
    assert lock_held_while_thread_runs is True
    assert lease_waited_for_thread is True


class SnapshotReadingBackupService:
    def __init__(self) -> None:
        self.snapshots: list[tuple[BackupReason, dict, dict]] = []
        self.calls: list[tuple[str, str, BackupReason, bool]] = []

    def backup_session(self, cwd: str, session_id: str, *, reason: BackupReason, critical: bool) -> None:
        self.calls.append((cwd, session_id, reason, critical))
        session_dir = SessionStorage().session_dir(cwd, session_id)
        task_snapshot = json.loads((session_dir / "a2a" / "task.json").read_text(encoding="utf-8"))
        context_snapshot = json.loads((session_dir / "a2a" / "context.json").read_text(encoding="utf-8"))
        self.snapshots.append((reason, task_snapshot, context_snapshot))


class UnsuccessfulBackupService:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, BackupReason, bool]] = []

    def backup_session(self, cwd: str, session_id: str, *, reason: BackupReason, critical: bool) -> BackupResult:
        self.calls.append((cwd, session_id, reason, critical))
        return BackupResult(enabled=True, succeeded=False, error="backup destination unavailable")


class UnsuccessfulBackupServiceWithoutError:
    def backup_session(self, cwd: str, session_id: str, *, reason: BackupReason, critical: bool) -> BackupResult:
        return BackupResult(enabled=True, succeeded=False, error=None)


class BlockedBackupServiceWithRetries:
    def backup_session(self, cwd: str, session_id: str, *, reason: BackupReason, critical: bool) -> BackupResult:
        raise SessionBackupBlocked("backup destination unavailable", retry_count=2)


class BackupBlockedMetrics(NoOpA2AMetrics):
    def __init__(self) -> None:
        self.backup_blocked: list[tuple[str, bool]] = []
        self.backup_failed: list[tuple[str, bool, int]] = []
        self.backup_succeeded: list[tuple[str, bool, int]] = []
        self.executor_error = 0
        self.task_canceled = 0
        self.task_failed = 0

    def record_backup_blocked(self, *, reason: str, recoverable: bool) -> None:
        self.backup_blocked.append((reason, recoverable))

    def record_backup_failed(self, *, reason: str, critical: bool, retry_count: int) -> None:
        self.backup_failed.append((reason, critical, retry_count))

    def record_backup_succeeded(self, *, reason: str, critical: bool, retry_count: int) -> None:
        self.backup_succeeded.append((reason, critical, retry_count))

    def record_executor_error(self) -> None:
        self.executor_error += 1

    def record_task_canceled(self) -> None:
        self.task_canceled += 1

    def record_task_failed(self) -> None:
        self.task_failed += 1


class ExplodingBackupMetrics(NoOpA2AMetrics):
    def record_backup_blocked(self, *, reason: str, recoverable: bool) -> None:
        raise RuntimeError("metrics sink failed with token=abc123")

    def record_backup_failed(self, *, reason: str, critical: bool, retry_count: int) -> None:
        raise RuntimeError("metrics sink failed with token=abc123")

    def record_backup_succeeded(self, *, reason: str, critical: bool, retry_count: int) -> None:
        raise RuntimeError("metrics sink failed with token=abc123")


class FakeContextManager:
    def __init__(self) -> None:
        self.raw_messages: list[Message] = []

    def add_raw_message(self, message: dict[str, object]) -> Message:
        converted = Message(role=str(message.get("role", "user")), content=str(message.get("content", "")))
        self.raw_messages.append(converted)
        return converted


class A2APromptCommandFakeLoop:
    def __init__(self, *, cwd: str | None = None, session_id: str = "session-1") -> None:
        self.context_manager = FakeContextManager()
        self.prompts: list[object] = []
        self.continued = False
        self.context_modifiers: list[object] = []
        self._session_storage = SessionStorage() if cwd is not None else None
        self._cwd = cwd
        self._session_id = session_id
        self._current_git_branch = None

    async def run_streaming(self, prompt: object):
        self.prompts.append(prompt)
        yield TextDeltaEvent(text=f"unexpected raw prompt: {prompt}")

    async def continue_streaming(self):
        self.continued = True
        yield TextDeltaEvent(text="MCP prompt executed")

    def _apply_context_modifier(self, modifier: object) -> None:
        self.context_modifiers.append(modifier)


class FakeA2AMCPPromptManager:
    def __init__(self) -> None:
        self.called_with: dict[str, object] | None = None

    def list_prompts(self) -> list[object]:
        return [
            SimpleNamespace(
                server_name="ros",
                prompt_name="review",
                public_name="mcp__ros__review",
                description="Review ROS template",
                arguments={"template": {"required": True}},
                original_server_name="ros",
                original_prompt_name="review",
            )
        ]

    async def get_prompt(self, server_name: str, prompt_name: str, arguments: dict[str, str]):
        self.called_with = {
            "server_name": server_name,
            "prompt_name": prompt_name,
            "arguments": dict(arguments),
        }
        return {
            "messages": [
                {
                    "role": "user",
                    "content": {
                        "type": "text",
                        "text": f"A2A_MCP_PROMPT_EXECUTED:{arguments['template']}",
                    },
                }
            ]
        }


class FailingA2AMCPPromptManager(FakeA2AMCPPromptManager):
    async def get_prompt(self, server_name: str, prompt_name: str, arguments: dict[str, str]):
        self.called_with = {
            "server_name": server_name,
            "prompt_name": prompt_name,
            "arguments": dict(arguments),
        }
        raise RuntimeError("MCP prompt server failed with access_token=super-secret-token")


def _mcp_prompt_command_registry(manager: FakeA2AMCPPromptManager) -> CommandRegistry:
    registry = CommandRegistry()
    warnings = register_mcp_prompt_commands(registry, manager)
    assert warnings == []
    return registry


@pytest.fixture(autouse=True)
def default_normal_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("IAC_CODE_MODE", raising=False)


@pytest.mark.asyncio
async def test_backup_session_async_uses_translatable_fallback_for_missing_error() -> None:
    with pytest.raises(SessionBackupBlocked, match="Session backup failed"):
        await backup_session_async(
            UnsuccessfulBackupServiceWithoutError(),
            "/repo",
            "session-1",
            reason=BackupReason.TERMINAL,
            critical=True,
        )


@pytest.mark.asyncio
async def test_backup_session_async_records_retry_count_from_blocked_exception() -> None:
    metrics = BackupBlockedMetrics()

    with pytest.raises(SessionBackupBlocked):
        await backup_session_async(
            BlockedBackupServiceWithRetries(),
            "/repo",
            "session-1",
            reason=BackupReason.TERMINAL,
            critical=True,
            metrics=metrics,
        )

    assert metrics.backup_failed == [(BackupReason.TERMINAL.value, True, 2)]


@pytest.mark.asyncio
async def test_backup_session_async_swallows_metrics_errors_for_noncritical_failure() -> None:
    result = await backup_session_async(
        UnsuccessfulBackupService(),
        "/repo",
        "session-1",
        reason=BackupReason.NORMAL_TURN_END,
        critical=False,
        metrics=ExplodingBackupMetrics(),
    )

    assert result is not None
    assert result.succeeded is False


@pytest.mark.asyncio
async def test_backup_session_async_cancellation_waits_for_worker() -> None:
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()

    class BlockingBackupService:
        def backup_session(self, *_args, **_kwargs) -> BackupResult:
            started.set()
            try:
                release.wait(timeout=5)
                return BackupResult(enabled=True)
            finally:
                finished.set()

    backup = asyncio.create_task(
        backup_session_async(
            BlockingBackupService(),
            "/repo",
            "session-1",
            reason=BackupReason.NORMAL_TURN_END,
            critical=False,
        )
    )
    while not started.is_set():
        await asyncio.sleep(0)

    backup.cancel()
    await asyncio.sleep(0.05)

    assert backup.done() is False
    assert finished.is_set() is False

    release.set()
    with pytest.raises(asyncio.CancelledError):
        await backup
    assert finished.is_set() is True


def test_normal_handoff_without_committed_visibility_has_no_backup_ack() -> None:
    assert (
        _normal_handoff_has_backup_ack(
            {
                "action": "switch_to_normal",
                "targetMode": "normal",
            },
            [],
        )
        is False
    )


@pytest.mark.asyncio
async def test_executor_runs_prompt_and_finishes_input_required(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    loop = FakeAgentLoop([TextDeltaEvent(text="hi")])
    runtime = FakeRuntime(agent_loop=loop, session_id="session-1")

    monkeypatch.setattr("iac_code.a2a.executor.create_agent_runtime", lambda options: runtime)

    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    backup_service = SnapshotReadingBackupService()
    executor = IacCodeA2AExecutor(task_store=store, model="qwen3.6-plus", backup_service=backup_service)
    queue = FakeEventQueue()
    context = FakeRequestContext(metadata={"iac_code": {"cwd": str(tmp_path)}})

    await executor.execute(context, queue)

    assert loop.prompts == ["hello"]
    states = [dump(event)["status"]["state"] for event in queue.events if isinstance(event, TaskStatusUpdateEvent)]
    assert states[0] == "TASK_STATE_SUBMITTED"
    assert "TASK_STATE_WORKING" in states
    assert states[-1] == "TASK_STATE_INPUT_REQUIRED"
    record = await store.get_or_create_task(task_id="task-1", context_id="ctx-1")
    assert "".join(record.output_text) == "hi"
    final_events = [
        dump(event)
        for event in queue.events
        if isinstance(event, TaskStatusUpdateEvent)
        and dump(event).get("metadata", {}).get("iac_code", {}).get("assistantFinal", {}).get("complete") is True
    ]
    assert final_events[0]["status"]["message"]["parts"][0]["text"] == "hi"


@pytest.mark.asyncio
async def test_executor_publishes_only_last_assistant_message_as_authoritative_final(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    loop = FakeAgentLoop(
        [
            MessageStartEvent(message_id="planning"),
            TextDeltaEvent(text="I will inspect the VPC first."),
            MessageEndEvent(stop_reason="tool_use", usage=Usage()),
            ToolResultEvent(tool_use_id="tool-1", tool_name="aliyun_api", result="done"),
            MessageStartEvent(message_id="final"),
            TextDeltaEvent(text="VSwitch deployment completed."),
            MessageEndEvent(stop_reason="end_turn", usage=Usage()),
        ]
    )
    runtime = FakeRuntime(agent_loop=loop, session_id="session-1")
    monkeypatch.setattr("iac_code.a2a.executor.create_agent_runtime", lambda options: runtime)
    queue = FakeEventQueue()

    await IacCodeA2AExecutor(
        task_store=A2ATaskStore(metrics=NoOpA2AMetrics()),
        model="qwen3.6-plus",
    ).execute(FakeRequestContext(metadata={"iac_code": {"cwd": str(tmp_path)}}), queue)

    final_events = [
        dump(event)
        for event in queue.events
        if isinstance(event, TaskStatusUpdateEvent)
        and dump(event).get("metadata", {}).get("iac_code", {}).get("assistantFinal", {}).get("complete") is True
    ]
    assert len(final_events) == 1
    assert final_events[0]["status"]["message"]["parts"][0]["text"] == "VSwitch deployment completed."


@pytest.mark.asyncio
async def test_executor_enables_a2a_safe_mode_for_normal_runtime_from_env(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    loop = FakeAgentLoop([TextDeltaEvent(text="hi")])
    captured_options = {}

    def fake_create_agent_runtime(options):
        captured_options["a2a_safe_mode"] = getattr(options, "a2a_safe_mode", False)
        return FakeRuntime(agent_loop=loop, session_id=options.session_id)

    monkeypatch.setenv("IAC_CODE_A2A_SAFE_MODE", "1")
    monkeypatch.setattr("iac_code.a2a.executor.create_agent_runtime", fake_create_agent_runtime)

    executor = IacCodeA2AExecutor(task_store=A2ATaskStore(metrics=NoOpA2AMetrics()), model="qwen3.6-plus")

    await executor.execute(
        FakeRequestContext(metadata={"iac_code": {"cwd": str(tmp_path)}}),
        FakeEventQueue(),
    )

    assert captured_options["a2a_safe_mode"] is True


@pytest.mark.asyncio
async def test_executor_does_not_enable_a2a_safe_mode_for_unknown_env_value(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    loop = FakeAgentLoop([TextDeltaEvent(text="hi")])
    captured_options = {}

    def fake_create_agent_runtime(options):
        captured_options["a2a_safe_mode"] = getattr(options, "a2a_safe_mode", False)
        return FakeRuntime(agent_loop=loop, session_id=options.session_id)

    monkeypatch.setenv("IAC_CODE_A2A_SAFE_MODE", "definitely")
    monkeypatch.setattr("iac_code.a2a.executor.create_agent_runtime", fake_create_agent_runtime)

    executor = IacCodeA2AExecutor(task_store=A2ATaskStore(metrics=NoOpA2AMetrics()), model="qwen3.6-plus")

    await executor.execute(
        FakeRequestContext(metadata={"iac_code": {"cwd": str(tmp_path)}}),
        FakeEventQueue(),
    )

    assert captured_options["a2a_safe_mode"] is False


@pytest.mark.asyncio
async def test_executor_executes_mcp_prompt_command_before_llm(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    loop = A2APromptCommandFakeLoop(cwd=str(tmp_path), session_id="session-1")
    manager = FakeA2AMCPPromptManager()
    runtime = FakeRuntime(
        agent_loop=loop,
        session_id="session-1",
        command_registry=_mcp_prompt_command_registry(manager),
    )
    monkeypatch.setattr("iac_code.a2a.executor.create_agent_runtime", lambda options: runtime)

    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    executor = IacCodeA2AExecutor(task_store=store, model="qwen3.6-plus")
    queue = FakeEventQueue()
    context = FakeRequestContext(
        text="/mcp__ros__review template=manual-real",
        metadata={"iac_code": {"cwd": str(tmp_path)}},
    )

    await executor.execute(context, queue)

    assert loop.prompts == []
    assert loop.continued is True
    assert [message.content for message in loop.context_manager.raw_messages] == [
        "user: A2A_MCP_PROMPT_EXECUTED:manual-real"
    ]
    persisted = SessionStorage().load(str(tmp_path), "session-1")
    assert [message.content for message in persisted] == ["user: A2A_MCP_PROMPT_EXECUTED:manual-real"]
    assert all("/mcp__ros__review" not in str(message.content) for message in persisted)
    assert manager.called_with == {
        "server_name": "ros",
        "prompt_name": "review",
        "arguments": {"template": "manual-real"},
    }
    record = await store.get_or_create_task(task_id="task-1", context_id="ctx-1")
    assert "".join(record.output_text) == "MCP prompt executed"


@pytest.mark.asyncio
async def test_executor_mcp_prompt_missing_required_argument_fails_before_llm(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    loop = A2APromptCommandFakeLoop(cwd=str(tmp_path), session_id="session-1")
    manager = FakeA2AMCPPromptManager()
    runtime = FakeRuntime(
        agent_loop=loop,
        session_id="session-1",
        command_registry=_mcp_prompt_command_registry(manager),
    )
    monkeypatch.setattr("iac_code.a2a.executor.create_agent_runtime", lambda options: runtime)

    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    executor = IacCodeA2AExecutor(task_store=store, model="qwen3.6-plus")
    queue = FakeEventQueue()
    context = FakeRequestContext(
        text="/mcp__ros__review",
        metadata={"iac_code": {"cwd": str(tmp_path)}},
    )

    await executor.execute(context, queue)

    assert loop.prompts == []
    assert loop.context_manager.raw_messages == []
    assert manager.called_with is None
    dumped = dump(queue.events[-1])
    assert dumped["status"]["state"] == "TASK_STATE_FAILED"
    text = dumped["status"]["message"]["parts"][0]["text"]
    assert text == "ValueError: Missing required MCP prompt argument: template"
    record = await store.get_or_create_task(task_id="task-1", context_id="ctx-1")
    assert record.state == "failed"


@pytest.mark.asyncio
async def test_executor_mcp_prompt_server_error_fails_before_llm_and_preserves_user_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    loop = A2APromptCommandFakeLoop(cwd=str(tmp_path), session_id="session-1")
    manager = FailingA2AMCPPromptManager()
    runtime = FakeRuntime(
        agent_loop=loop,
        session_id="session-1",
        command_registry=_mcp_prompt_command_registry(manager),
    )
    monkeypatch.setattr("iac_code.a2a.executor.create_agent_runtime", lambda options: runtime)

    executor = IacCodeA2AExecutor(task_store=A2ATaskStore(metrics=NoOpA2AMetrics()), model="qwen3.6-plus")
    queue = FakeEventQueue()
    context = FakeRequestContext(
        text="/mcp__ros__review template=manual-real",
        metadata={"iac_code": {"cwd": str(tmp_path)}},
    )

    await executor.execute(context, queue)

    assert loop.prompts == []
    assert loop.context_manager.raw_messages == []
    assert manager.called_with == {
        "server_name": "ros",
        "prompt_name": "review",
        "arguments": {"template": "manual-real"},
    }
    dumped = dump(queue.events[-1])
    assert dumped["status"]["state"] == "TASK_STATE_FAILED"
    text = dumped["status"]["message"]["parts"][0]["text"]
    assert text.startswith("RuntimeError: MCP prompt server failed with access_token=")
    assert "super-secret-token" in text


@pytest.mark.asyncio
async def test_executor_does_not_execute_non_mcp_prompt_command(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    registry = CommandRegistry()
    registry.register(
        PromptCommand(
            name="regular_review",
            description="Review local context",
            skill=SkillDefinition(
                name="regular_review",
                description="Review local context",
                frontmatter=SkillFrontmatter(description="Review local context"),
                content="Review local context: {{args}}",
                source=SkillSource.PROJECT,
                file_path=str(tmp_path / "skills" / "regular_review.md"),
                content_length=0,
            ),
            source=SkillSource.PROJECT,
        )
    )
    loop = FakeAgentLoop([TextDeltaEvent(text="hi")])
    runtime = FakeRuntime(agent_loop=loop, session_id="session-1", command_registry=registry)
    monkeypatch.setattr("iac_code.a2a.executor.create_agent_runtime", lambda options: runtime)

    executor = IacCodeA2AExecutor(task_store=A2ATaskStore(metrics=NoOpA2AMetrics()), model="qwen3.6-plus")
    await executor.execute(
        FakeRequestContext(
            text="/regular_review topic=iac",
            metadata={"iac_code": {"cwd": str(tmp_path)}},
        ),
        FakeEventQueue(),
    )

    assert loop.prompts == ["/regular_review topic=iac"]


@pytest.mark.asyncio
async def test_executor_does_not_execute_mcp_resource_skill_as_prompt_command(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    registry = CommandRegistry()
    registry.register(
        PromptCommand(
            name="mcp__ros__resource_skill",
            description="Remote resource skill",
            skill=SkillDefinition(
                name="mcp__ros__resource_skill",
                description="Remote resource skill",
                frontmatter=SkillFrontmatter(description="Remote resource skill"),
                content="Resource skill content: {{args}}",
                source=SkillSource.PROJECT,
                file_path="mcp://ros/skill://ros/resource_skill",
                content_length=0,
            ),
            source=SkillSource.PROJECT,
        )
    )
    loop = FakeAgentLoop([TextDeltaEvent(text="hi")])
    runtime = FakeRuntime(agent_loop=loop, session_id="session-1", command_registry=registry)
    monkeypatch.setattr("iac_code.a2a.executor.create_agent_runtime", lambda options: runtime)

    executor = IacCodeA2AExecutor(task_store=A2ATaskStore(metrics=NoOpA2AMetrics()), model="qwen3.6-plus")
    await executor.execute(
        FakeRequestContext(
            text="/mcp__ros__resource_skill topic=iac",
            metadata={"iac_code": {"cwd": str(tmp_path)}},
        ),
        FakeEventQueue(),
    )

    assert loop.prompts == ["/mcp__ros__resource_skill topic=iac"]


@pytest.mark.asyncio
async def test_executor_does_not_execute_mcp_resource_skill_with_prompt_path_segment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    registry = CommandRegistry()
    registry.register(
        PromptCommand(
            name="mcp__ros__skill_prompt",
            description="Remote resource skill containing prompt path segment",
            skill=SkillDefinition(
                name="mcp__ros__skill_prompt",
                description="Remote resource skill containing prompt path segment",
                frontmatter=SkillFrontmatter(description="Remote resource skill containing prompt path segment"),
                content="Resource skill content: {{args}}",
                source=SkillSource.PROJECT,
                file_path="mcp://ros/skill://ros/prompt/foo",
                content_length=0,
            ),
            source=SkillSource.PROJECT,
        )
    )
    loop = FakeAgentLoop([TextDeltaEvent(text="hi")])
    runtime = FakeRuntime(agent_loop=loop, session_id="session-1", command_registry=registry)
    monkeypatch.setattr("iac_code.a2a.executor.create_agent_runtime", lambda options: runtime)

    executor = IacCodeA2AExecutor(task_store=A2ATaskStore(metrics=NoOpA2AMetrics()), model="qwen3.6-plus")
    await executor.execute(
        FakeRequestContext(
            text="/mcp__ros__skill_prompt topic=iac",
            metadata={"iac_code": {"cwd": str(tmp_path)}},
        ),
        FakeEventQueue(),
    )

    assert loop.prompts == ["/mcp__ros__skill_prompt topic=iac"]


@pytest.mark.asyncio
async def test_executor_restores_backup_only_session_for_persisted_context(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config_dir = tmp_path / "config"
    backup_root = tmp_path / "backup"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    cwd = str(workspace)
    session_id = "backup-only-session"
    context_id = "ctx-restore"
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("IAC_CODE_CONFIG_BACKUP_DIR", str(backup_root))
    backup_storage = SessionStorage(projects_dir=backup_root / "projects")
    backup_storage.save(cwd, session_id, [Message(role="user", content="from backup")], git_branch=None)
    backup_session_dir = backup_storage.session_dir(cwd, session_id)
    (backup_session_dir / "a2a").mkdir()
    (backup_session_dir / "a2a" / "context.json").write_text(
        json.dumps(A2AContextSnapshot(context_id=context_id, session_id=session_id, cwd=cwd).__dict__),
        encoding="utf-8",
    )
    SessionBackupService(backup_storage).initialize_session(cwd, session_id)
    persistence = A2APersistenceStore(tmp_path / "a2a-persistence")
    persistence.save_context(A2AContextSnapshot(context_id=context_id, session_id=session_id, cwd=cwd))
    captured_options = {}
    loop = FakeAgentLoop([TextDeltaEvent(text="hi")])

    def fake_create_agent_runtime(options):
        captured_options["session_id"] = options.session_id
        captured_options["resume_messages"] = options.resume_messages
        return FakeRuntime(agent_loop=loop, session_id=options.session_id)

    monkeypatch.setattr("iac_code.a2a.executor.create_agent_runtime", fake_create_agent_runtime)

    store = A2ATaskStore(metrics=NoOpA2AMetrics(), persistence=persistence)
    executor = IacCodeA2AExecutor(task_store=store, model="qwen3.6-plus")

    await executor.execute(
        FakeRequestContext(context_id=context_id, metadata={"iac_code": {"cwd": cwd}}),
        FakeEventQueue(),
    )

    restored_storage = SessionStorage(projects_dir=config_dir / "projects")
    assert captured_options["session_id"] == session_id
    assert captured_options["resume_messages"][0].content == "from backup"
    assert restored_storage.load(cwd, session_id)[0].content == "from backup"


@pytest.mark.asyncio
async def test_executor_runs_noncritical_backup_after_normal_turn_without_failing_task(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    loop = FakeAgentLoop([TextDeltaEvent(text="hi")])
    runtime = FakeRuntime(agent_loop=loop, session_id="session-1")
    backup_service = FailingBackupService()
    metrics = BackupBlockedMetrics()

    monkeypatch.setattr("iac_code.a2a.executor.create_agent_runtime", lambda options: runtime)

    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    executor = IacCodeA2AExecutor(
        task_store=store,
        model="qwen3.6-plus",
        metrics=metrics,
        backup_service=backup_service,
    )
    queue = FakeEventQueue()

    await executor.execute(FakeRequestContext(metadata={"iac_code": {"cwd": str(tmp_path)}}), queue)

    assert [(reason, critical) for *_ids, reason, critical in backup_service.calls] == [
        (BackupReason.NORMAL_TURN_END, False)
    ]
    assert dump(queue.events[-1])["status"]["state"] == "TASK_STATE_INPUT_REQUIRED"
    record = await store.get_or_create_task(task_id="task-1", context_id="ctx-1")
    assert record.state == "input-required"
    assert "A2A session backup failed" in caplog.text
    assert "reason=normal_turn_end" in caplog.text
    assert "retry_count=0" in caplog.text
    assert metrics.backup_failed == [(BackupReason.NORMAL_TURN_END.value, False, 0)]


@pytest.mark.asyncio
async def test_executor_mirrors_input_required_before_normal_turn_backup(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    loop = FakeAgentLoop([TextDeltaEvent(text="hi")])
    runtime = FakeRuntime(agent_loop=loop, session_id="session-1")
    backup_service = SnapshotReadingBackupService()

    monkeypatch.setattr("iac_code.a2a.executor.create_agent_runtime", lambda options: runtime)

    executor = IacCodeA2AExecutor(
        task_store=A2ATaskStore(metrics=NoOpA2AMetrics()),
        model="qwen3.6-plus",
        backup_service=backup_service,
    )

    await executor.execute(FakeRequestContext(metadata={"iac_code": {"cwd": str(tmp_path)}}), FakeEventQueue())

    assert len(backup_service.snapshots) == 1
    reason, task_snapshot, context_snapshot = backup_service.snapshots[0]
    assert reason == BackupReason.NORMAL_TURN_END
    assert task_snapshot["state"] == "input-required"
    assert context_snapshot["active_task_id"] is None


@pytest.mark.asyncio
async def test_executor_mirrors_failed_terminal_before_terminal_backup(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    class ExplodingLoop:
        async def run_streaming(self, prompt: str):
            raise RuntimeError("boom")
            yield TextDeltaEvent(text="never")

    runtime = FakeRuntime(agent_loop=ExplodingLoop(), session_id="session-1")
    backup_service = SnapshotReadingBackupService()

    monkeypatch.setattr("iac_code.a2a.executor.create_agent_runtime", lambda options: runtime)

    executor = IacCodeA2AExecutor(
        task_store=A2ATaskStore(metrics=NoOpA2AMetrics()),
        model="qwen3.6-plus",
        backup_service=backup_service,
    )

    await executor.execute(FakeRequestContext(metadata={"iac_code": {"cwd": str(tmp_path)}}), FakeEventQueue())

    assert len(backup_service.snapshots) == 1
    reason, task_snapshot, context_snapshot = backup_service.snapshots[0]
    assert reason == BackupReason.TERMINAL
    assert task_snapshot["state"] == "failed"
    assert context_snapshot["active_task_id"] is None
    assert [(reason, critical) for *_ids, reason, critical in backup_service.calls] == [(BackupReason.TERMINAL, True)]
    record = await executor._task_store.get_or_create_task(task_id="task-1", context_id="ctx-1")
    assert record.state == "failed"


@pytest.mark.asyncio
async def test_executor_failed_terminal_backup_result_blocks_terminal_status(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    class ExplodingLoop:
        async def run_streaming(self, prompt: str):
            raise RuntimeError("boom")
            yield TextDeltaEvent(text="never")

    runtime = FakeRuntime(agent_loop=ExplodingLoop(), session_id="session-1")
    backup_service = UnsuccessfulBackupService()
    metrics = BackupBlockedMetrics()

    monkeypatch.setattr("iac_code.a2a.executor.create_agent_runtime", lambda options: runtime)

    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    executor = IacCodeA2AExecutor(
        task_store=store,
        model="qwen3.6-plus",
        metrics=metrics,
        backup_service=backup_service,
    )
    queue = FakeEventQueue()

    await executor.execute(FakeRequestContext(metadata={"iac_code": {"cwd": str(tmp_path)}}), queue)

    assert [(reason, critical) for *_ids, reason, critical in backup_service.calls] == [(BackupReason.TERMINAL, True)]
    states = [dump(event)["status"]["state"] for event in queue.events if isinstance(event, TaskStatusUpdateEvent)]
    assert states[-1] == "TASK_STATE_INPUT_REQUIRED"
    assert "TASK_STATE_FAILED" not in states
    record = await store.get_or_create_task(task_id="task-1", context_id="ctx-1")
    assert record.state == "input-required"
    terminal_update = dump(queue.events[-1])
    backup_metadata = terminal_update["metadata"]["iac_code"]["backupBlocked"]
    assert backup_metadata["reason"] == BackupReason.TERMINAL.value
    assert backup_metadata["blockedTerminalState"] == "failed"
    assert backup_metadata["recoverable"] is True
    assert metrics.backup_blocked == [(BackupReason.TERMINAL.value, True)]
    assert metrics.backup_failed == [(BackupReason.TERMINAL.value, True, 0)]
    assert metrics.executor_error == 1
    assert metrics.task_failed == 1
    assert metrics.task_canceled == 0


@pytest.mark.asyncio
async def test_executor_terminal_backup_blocked_ignores_metrics_sink_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    class ExplodingLoop:
        async def run_streaming(self, prompt: str):
            raise RuntimeError("boom")
            yield TextDeltaEvent(text="never")

    runtime = FakeRuntime(agent_loop=ExplodingLoop(), session_id="session-1")
    backup_service = UnsuccessfulBackupService()

    monkeypatch.setattr("iac_code.a2a.executor.create_agent_runtime", lambda options: runtime)

    executor = IacCodeA2AExecutor(
        task_store=A2ATaskStore(metrics=NoOpA2AMetrics()),
        model="qwen3.6-plus",
        metrics=ExplodingBackupMetrics(),
        backup_service=backup_service,
    )
    queue = FakeEventQueue()

    await executor.execute(FakeRequestContext(metadata={"iac_code": {"cwd": str(tmp_path)}}), queue)

    states = [dump(event)["status"]["state"] for event in queue.events if isinstance(event, TaskStatusUpdateEvent)]
    assert states[-1] == "TASK_STATE_INPUT_REQUIRED"
    terminal_update = dump(queue.events[-1])
    backup_metadata = terminal_update["metadata"]["iac_code"]["backupBlocked"]
    assert backup_metadata["reason"] == BackupReason.TERMINAL.value


@pytest.mark.asyncio
async def test_executor_mirrors_canceled_terminal_before_terminal_backup(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    started = asyncio.Event()

    class BlockingLoop:
        async def run_streaming(self, prompt: str):
            started.set()
            await asyncio.Future()
            yield TextDeltaEvent(text="never")

    runtime = FakeRuntime(agent_loop=BlockingLoop(), session_id="session-1")
    backup_service = SnapshotReadingBackupService()

    monkeypatch.setattr("iac_code.a2a.executor.create_agent_runtime", lambda options: runtime)

    executor = IacCodeA2AExecutor(
        task_store=A2ATaskStore(metrics=NoOpA2AMetrics()),
        model="qwen3.6-plus",
        backup_service=backup_service,
    )
    execute_task = asyncio.create_task(
        executor.execute(FakeRequestContext(metadata={"iac_code": {"cwd": str(tmp_path)}}), FakeEventQueue())
    )
    await started.wait()

    execute_task.cancel()
    await execute_task

    assert len(backup_service.snapshots) == 1
    reason, task_snapshot, context_snapshot = backup_service.snapshots[0]
    assert reason == BackupReason.TERMINAL
    assert task_snapshot["state"] == "canceled"
    assert context_snapshot["active_task_id"] is None
    assert [(reason, critical) for *_ids, reason, critical in backup_service.calls] == [(BackupReason.TERMINAL, True)]
    record = await executor._task_store.get_or_create_task(task_id="task-1", context_id="ctx-1")
    assert record.state == "canceled"


@pytest.mark.asyncio
async def test_cancel_wait_returns_after_terminal_snapshot_staged_before_shared_publication(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config_root = tmp_path / "config"
    staging_root = tmp_path / "staging"
    backup_root = tmp_path / "backup"
    cwd = tmp_path / "workspace"
    cwd.mkdir()
    cwd = cwd.resolve()
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(config_root))
    monkeypatch.setenv("IAC_CODE_CONFIG_BACKUP_DIR", str(backup_root))
    started = asyncio.Event()

    class BlockingLoop:
        async def run_streaming(self, prompt: str):
            started.set()
            await asyncio.Future()
            yield TextDeltaEvent(text="never")

    runtime = FakeRuntime(agent_loop=BlockingLoop(), session_id="session-1")
    monkeypatch.setattr("iac_code.a2a.executor.create_agent_runtime", lambda options: runtime)
    storage = SessionStorage(projects_dir=config_root / "projects")
    backup_service = StagedSessionBackupService(staging_root, storage, retry_delays=())
    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    executor = IacCodeA2AExecutor(
        task_store=store,
        model="qwen3.6-plus",
        backup_service=backup_service,
    )
    execute_task = asyncio.create_task(
        executor.execute(
            FakeRequestContext(metadata={"iac_code": {"cwd": str(cwd)}}),
            FakeEventQueue(),
        )
    )
    await started.wait()
    context_record = await store.get_context_record("ctx-1")
    session_dir = storage.session_dir(str(cwd), context_record.session_id)
    (session_dir / "session.jsonl").write_text("terminal-v1\n", encoding="utf-8")
    cancel_queue = FakeEventQueue()

    await executor.cancel(FakeRequestContext(), cancel_queue)
    await execute_task

    snapshot = staging_root / "projects" / session_dir.parent.name / "{}_v1".format(context_record.session_id)
    staged_state = json.loads((snapshot / BACKUP_STATE_FILENAME).read_text(encoding="utf-8"))
    staged_task = json.loads((snapshot / "a2a" / "task.json").read_text(encoding="utf-8"))
    staged_context = json.loads((snapshot / "a2a" / "context.json").read_text(encoding="utf-8"))
    assert dump(cancel_queue.events[-1])["status"]["state"] == "TASK_STATE_CANCELED"
    assert staged_state["generation"] == 1
    assert staged_state["reason"] == BackupReason.TERMINAL.value
    assert (snapshot / "session.jsonl").read_text(encoding="utf-8") == "terminal-v1\n"
    assert staged_task["state"] == "canceled"
    assert staged_context["active_task_id"] is None
    assert not list(staging_root.rglob("*.copying"))
    assert not backup_root.exists()


@pytest.mark.asyncio
async def test_executor_canceled_terminal_backup_blocked_records_cancel_intent(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    started = asyncio.Event()

    class BlockingLoop:
        async def run_streaming(self, prompt: str):
            started.set()
            await asyncio.Future()
            yield TextDeltaEvent(text="never")

    runtime = FakeRuntime(agent_loop=BlockingLoop(), session_id="session-1")
    backup_service = UnsuccessfulBackupService()
    metrics = BackupBlockedMetrics()

    monkeypatch.setattr("iac_code.a2a.executor.create_agent_runtime", lambda options: runtime)

    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    executor = IacCodeA2AExecutor(
        task_store=store,
        model="qwen3.6-plus",
        metrics=metrics,
        backup_service=backup_service,
    )
    queue = FakeEventQueue()
    execute_task = asyncio.create_task(
        executor.execute(FakeRequestContext(metadata={"iac_code": {"cwd": str(tmp_path)}}), queue)
    )
    await started.wait()

    execute_task.cancel()
    await execute_task

    assert [(reason, critical) for *_ids, reason, critical in backup_service.calls] == [(BackupReason.TERMINAL, True)]
    states = [dump(event)["status"]["state"] for event in queue.events if isinstance(event, TaskStatusUpdateEvent)]
    assert states[-1] == "TASK_STATE_INPUT_REQUIRED"
    assert "TASK_STATE_CANCELED" not in states
    terminal_update = dump(queue.events[-1])
    backup_metadata = terminal_update["metadata"]["iac_code"]["backupBlocked"]
    assert backup_metadata["reason"] == BackupReason.TERMINAL.value
    assert backup_metadata["blockedTerminalState"] == "canceled"
    record = await store.get_or_create_task(task_id="task-1", context_id="ctx-1")
    assert record.state == "input-required"
    assert metrics.backup_blocked == [(BackupReason.TERMINAL.value, True)]
    assert metrics.backup_failed == [(BackupReason.TERMINAL.value, True, 0)]
    assert metrics.task_canceled == 1
    assert metrics.executor_error == 0
    assert metrics.task_failed == 0


@pytest.mark.asyncio
async def test_executor_keeps_initial_task_echo_canonical_without_changing_runtime_prompt(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from a2a.types import Message, Part, Role

    config_dir = tmp_path / "config"
    cwd = tmp_path / "workspace"
    config_dir.mkdir()
    cwd.mkdir()
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(config_dir))
    loop = FakeAgentLoop([TextDeltaEvent(text="ok")])
    runtime = FakeRuntime(agent_loop=loop, session_id="session-1")
    monkeypatch.setattr("iac_code.a2a.executor.create_agent_runtime", lambda options: runtime)

    prompt = f"paths: {cwd}/src/app.py {config_dir}/tool-results/session-1/result.txt /opt/iac-code-outside/config.yaml"
    context = FakeRequestContext(text=prompt, metadata={"iac_code": {"cwd": str(cwd)}})
    context.message = Message(
        role=Role.ROLE_USER,
        parts=[Part(text=prompt)],
        metadata={"iac_code": {"cwd": str(cwd)}},
        message_id="msg-paths",
    )

    queue = FakeEventQueue()
    executor = IacCodeA2AExecutor(task_store=A2ATaskStore(metrics=NoOpA2AMetrics()), model="qwen3.6-plus")

    await executor.execute(context, queue)

    initial_task = next(event for event in queue.events if isinstance(event, Task))
    rendered = dump(initial_task)
    history = rendered["history"][0]
    assert history["parts"][0]["text"] == prompt
    assert history["metadata"]["iac_code"]["cwd"] == str(cwd)
    assert str(cwd) in history["parts"][0]["text"]
    assert str(config_dir) in history["parts"][0]["text"]
    assert "/opt/iac-code-outside/config.yaml" in history["parts"][0]["text"]
    assert loop.prompts == [prompt]


@pytest.mark.asyncio
async def test_executor_publishes_mcp_warnings(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    runtime = FakeRuntime(
        agent_loop=FakeAgentLoop([TextDeltaEvent(text="hi")]),
        session_id="session-1",
        mcp_config_warnings=[
            SimpleNamespace(
                server_name="broken",
                code="connection_failed",
                message="MCP server failed with access_token=super-secret-token",
            )
        ],
    )
    monkeypatch.setattr("iac_code.a2a.executor.create_agent_runtime", lambda options: runtime)

    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    backup_service = SnapshotReadingBackupService()
    executor = IacCodeA2AExecutor(task_store=store, model="qwen3.6-plus", backup_service=backup_service)
    queue = FakeEventQueue()

    await executor.execute(FakeRequestContext(metadata={"iac_code": {"cwd": str(tmp_path)}}), queue)

    warning_events = [
        dump(event)
        for event in queue.events
        if isinstance(event, TaskStatusUpdateEvent)
        and "mcpWarning" in dump(event).get("metadata", {}).get("iac_code", {})
    ]
    assert len(warning_events) == 1
    assert warning_events[0]["status"]["message"]["parts"][0]["text"] == (
        "MCP warning: MCP server failed with access_token=[REDACTED]"
    )
    assert warning_events[0]["metadata"]["iac_code"]["mcpWarning"]["code"] == "connection_failed"
    assert "super-secret-token" not in repr(warning_events[0]["metadata"]["iac_code"]["mcpWarning"])


@pytest.mark.asyncio
async def test_executor_publishes_mcp_status_metadata(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    private_marker = "IAC_PRIVATE_COMMAND_ARG_MARKER_36_A2A"
    scoped_config = ScopedMCPServerConfig(
        config=MCPServerConfig.from_mapping("broken", {"command": "node", "args": ["server.js", private_marker]}),
        scope=MCPConfigScope.USER,
    )
    runtime = FakeRuntime(
        agent_loop=FakeAgentLoop([TextDeltaEvent(text="hi")]),
        session_id="session-1",
        mcp_manager=SimpleNamespace(
            list_connections=lambda: [
                SimpleNamespace(
                    name="broken",
                    scoped_config=scoped_config,
                    state=MCPConnectionState.FAILED,
                    error="access_token=super-secret-token",
                    capability_errors={},
                    tools=[],
                    resources=[],
                    prompts=[],
                    retry_count=2,
                    metadata=MCPConnectionMetadata(
                        state=MCPConnectionState.FAILED,
                        server_name="broken",
                        protocol_version="2025-06-18",
                    ),
                )
            ]
        ),
    )
    monkeypatch.setattr("iac_code.a2a.executor.create_agent_runtime", lambda options: runtime)

    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    executor = IacCodeA2AExecutor(task_store=store, model="qwen3.6-plus")
    queue = FakeEventQueue()

    await executor.execute(FakeRequestContext(metadata={"iac_code": {"cwd": str(tmp_path)}}), queue)

    status_events = [dump(event) for event in queue.events if isinstance(event, TaskStatusUpdateEvent)]
    states = [event["status"]["state"] for event in status_events]
    assert states[:2] == ["TASK_STATE_SUBMITTED", "TASK_STATE_WORKING"]
    mcp_status_indexes = [
        index
        for index, event in enumerate(status_events)
        if "mcpStatus" in event.get("metadata", {}).get("iac_code", {})
    ]
    assert mcp_status_indexes and mcp_status_indexes[0] >= 2
    status_events = [status_events[index] for index in mcp_status_indexes]
    assert len(status_events) == 1
    status = status_events[0]["metadata"]["iac_code"]["mcpStatus"]
    assert status["servers"][0]["serverName"] == "broken"
    assert status["servers"][0]["state"] == "failed"
    assert status["servers"][0]["protocolVersion"] == "2025-06-18"
    assert status["servers"][0]["retryCount"] == 2
    assert "super-secret-token" not in repr(status)
    assert private_marker not in repr(status)


@pytest.mark.asyncio
async def test_executor_publishes_initial_mcp_status_for_each_task_in_same_context(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    runtime = FakeRuntime(
        agent_loop=FakeAgentLoop([TextDeltaEvent(text="hi")]),
        session_id="session-1",
        mcp_manager=SimpleNamespace(
            list_connections=lambda: [
                SimpleNamespace(
                    name="remote",
                    state=MCPConnectionState.CONNECTED,
                    error=None,
                    capability_errors={},
                    tools=[],
                    resources=[],
                    prompts=[],
                    retry_count=0,
                    metadata=None,
                )
            ]
        ),
    )
    monkeypatch.setattr("iac_code.a2a.executor.create_agent_runtime", lambda options: runtime)

    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    executor = IacCodeA2AExecutor(task_store=store, model="qwen3.6-plus")
    first_queue = FakeEventQueue()
    second_queue = FakeEventQueue()

    await executor.execute(
        FakeRequestContext(task_id="task-1", context_id="ctx-1", metadata={"iac_code": {"cwd": str(tmp_path)}}),
        first_queue,
    )
    await executor.execute(
        FakeRequestContext(task_id="task-2", context_id="ctx-1", metadata={"iac_code": {"cwd": str(tmp_path)}}),
        second_queue,
    )

    first_status_events = [dump(event) for event in first_queue.events if isinstance(event, TaskStatusUpdateEvent)]
    second_status_events = [dump(event) for event in second_queue.events if isinstance(event, TaskStatusUpdateEvent)]
    assert any("mcpStatus" in event.get("metadata", {}).get("iac_code", {}) for event in first_status_events)
    assert any("mcpStatus" in event.get("metadata", {}).get("iac_code", {}) for event in second_status_events)


@pytest.mark.asyncio
async def test_executor_pushes_updated_mcp_status_when_capabilities_change(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class DynamicMCPManager:
        def __init__(self) -> None:
            self.include_dynamic_tool = False

        def list_connections(self) -> list[SimpleNamespace]:
            tools = [
                SimpleNamespace(
                    public_name="mcp__remote__echo",
                    original_server_name="remote",
                    original_tool_name="echo",
                )
            ]
            if self.include_dynamic_tool:
                tools.append(
                    SimpleNamespace(
                        public_name="mcp__remote__live_added",
                        original_server_name="remote",
                        original_tool_name="live-added",
                    )
                )
            return [
                SimpleNamespace(
                    name="remote",
                    state=MCPConnectionState.CONNECTED,
                    error=None,
                    capability_errors={},
                    tools=tools,
                    resources=[],
                    prompts=[],
                    retry_count=0,
                    metadata=MCPConnectionMetadata(state=MCPConnectionState.CONNECTED, server_name="remote"),
                )
            ]

    class DynamicStatusLoop:
        def __init__(self, manager: DynamicMCPManager) -> None:
            self.manager = manager

        async def run_streaming(self, prompt: str):
            yield TextDeltaEvent(text="before")
            self.manager.include_dynamic_tool = True
            yield TextDeltaEvent(text="after")

    manager = DynamicMCPManager()
    runtime = FakeRuntime(
        agent_loop=DynamicStatusLoop(manager),
        session_id="session-1",
        mcp_manager=manager,
    )
    monkeypatch.setattr("iac_code.a2a.executor.create_agent_runtime", lambda options: runtime)

    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    executor = IacCodeA2AExecutor(task_store=store, model="qwen3.6-plus")
    queue = FakeEventQueue()

    await executor.execute(FakeRequestContext(metadata={"iac_code": {"cwd": str(tmp_path)}}), queue)

    status_events = [dump(event) for event in queue.events if isinstance(event, TaskStatusUpdateEvent)]
    mcp_statuses = [
        event["metadata"]["iac_code"]["mcpStatus"]
        for event in status_events
        if "mcpStatus" in event.get("metadata", {}).get("iac_code", {})
    ]

    assert len(mcp_statuses) == 2
    initial_tools = {tool["publicName"] for tool in mcp_statuses[0]["servers"][0]["tools"]}
    updated_tools = {tool["publicName"] for tool in mcp_statuses[1]["servers"][0]["tools"]}
    assert initial_tools == {"mcp__remote__echo"}
    assert updated_tools == {"mcp__remote__echo", "mcp__remote__live_added"}


@pytest.mark.asyncio
async def test_executor_closes_pipeline_runtime_when_replacing_with_normal_runtime(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from iac_code.a2a.pipeline_executor import A2APipelineRuntime

    old_agent_runtime = SimpleNamespace(closed=False)

    async def old_aclose() -> None:
        old_agent_runtime.closed = True

    old_agent_runtime.aclose = old_aclose
    old_pipeline_runtime = A2APipelineRuntime(agent_runtime=old_agent_runtime)
    new_runtime = FakeRuntime(agent_loop=FakeAgentLoop([TextDeltaEvent(text="hi")]), session_id="session-1")
    monkeypatch.setattr("iac_code.a2a.executor.create_agent_runtime", lambda options: new_runtime)

    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    await store.get_or_create_context(
        context_id="ctx-1",
        cwd=str(tmp_path),
        runtime_factory=lambda sid: old_pipeline_runtime,
    )
    executor = IacCodeA2AExecutor(task_store=store, model="qwen3.6-plus")

    await executor.execute(
        FakeRequestContext(task_id="task-1", context_id="ctx-1", metadata={"iac_code": {"cwd": str(tmp_path)}}),
        FakeEventQueue(),
    )

    assert old_agent_runtime.closed is True
    assert store._contexts["ctx-1"].runtime is new_runtime


@pytest.mark.asyncio
async def test_executor_exposes_iac_code_session_id_in_status_metadata(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def factory(options):
        return FakeRuntime(agent_loop=FakeAgentLoop([TextDeltaEvent(text="hi")]), session_id=options.session_id)

    monkeypatch.setattr("iac_code.a2a.executor.create_agent_runtime", factory)

    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    backup_service = SnapshotReadingBackupService()
    executor = IacCodeA2AExecutor(task_store=store, model="qwen3.6-plus", backup_service=backup_service)
    queue = FakeEventQueue()

    await executor.execute(
        FakeRequestContext(task_id="task-1", context_id="ctx-1", metadata={"iac_code": {"cwd": str(tmp_path)}}),
        queue,
    )

    session_id = store._contexts["ctx-1"].session_id
    status_updates = [dump(event) for event in queue.events if isinstance(event, TaskStatusUpdateEvent)]
    assert status_updates
    assert all(event["metadata"]["iac_code"]["iacCodeSessionId"] == session_id for event in status_updates)


@pytest.mark.asyncio
async def test_executor_passes_artifact_store_to_stream_event_publisher(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    artifact_store = object()
    seen_artifact_stores: list[object | None] = []
    seen_auto_approve_permissions: list[bool] = []
    seen_exposure_types: list[frozenset[A2AExposureType]] = []

    async def spy_publish_stream_event(
        event_queue,
        *,
        task_id,
        context_id,
        event,
        artifact_store=None,
        permission_resolver=None,
        permission_input_registry=None,
        auto_approve_permissions=False,
        exposure_types=None,
        iac_code_session_id=None,
        permission_wait_cwd=None,
        permission_wait_backup_service=None,
        permission_wait_metrics=None,
    ):
        seen_artifact_stores.append(artifact_store)
        assert permission_input_registry is not None
        seen_auto_approve_permissions.append(auto_approve_permissions)
        seen_exposure_types.append(exposure_types)
        return None

    loop = FakeAgentLoop(
        [
            ToolResultEvent(
                tool_use_id="tool-1",
                tool_name="write_file",
                result={"artifact": {"filename": "out.txt", "content": "hello", "mediaType": "text/plain"}},
                is_error=False,
            )
        ]
    )
    runtime = FakeRuntime(agent_loop=loop, session_id="session-1")
    monkeypatch.setattr("iac_code.a2a.executor.create_agent_runtime", lambda options: runtime)
    monkeypatch.setattr("iac_code.a2a.executor.publish_stream_event", spy_publish_stream_event)

    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    executor = IacCodeA2AExecutor(
        task_store=store,
        model="qwen3.6-plus",
        artifact_store=artifact_store,
        thinking_exposure_types=[A2AExposureType.RAW_THINKING, A2AExposureType.TOOL_TRACE],
    )

    await executor.execute(FakeRequestContext(metadata={"iac_code": {"cwd": str(tmp_path)}}), FakeEventQueue())

    assert seen_artifact_stores == [artifact_store]
    assert seen_auto_approve_permissions == [False]
    assert seen_exposure_types == [frozenset({A2AExposureType.RAW_THINKING, A2AExposureType.TOOL_TRACE})]


@pytest.mark.asyncio
async def test_executor_auto_approves_permissions_when_configured(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    future = pending_future()
    loop = FakeAgentLoop(
        [
            PermissionRequestEvent(
                tool_name="bash",
                tool_input={"cmd": "pwd"},
                tool_use_id="tool-1",
                response_future=future,
            )
        ]
    )
    runtime = FakeRuntime(agent_loop=loop, session_id="session-1")
    monkeypatch.setattr("iac_code.a2a.executor.create_agent_runtime", lambda options: runtime)

    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    executor = IacCodeA2AExecutor(
        task_store=store,
        model="qwen3.6-plus",
        auto_approve_permissions=True,
    )
    queue = FakeEventQueue()

    await executor.execute(FakeRequestContext(metadata={"iac_code": {"cwd": str(tmp_path)}}), queue)

    assert future.result() is True
    permission_events = [
        dump(event)["metadata"]["iac_code"]["permission"]
        for event in queue.events
        if "permission" in dump(event).get("metadata", {}).get("iac_code", {})
    ]
    assert permission_events[0]["autoApproved"] is True


@pytest.mark.asyncio
async def test_executor_persists_terminal_task_state_and_output(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    loop = FakeAgentLoop([TextDeltaEvent(text="persisted output")])
    runtime = FakeRuntime(agent_loop=loop, session_id="session-1")
    persistence = A2APersistenceStore(tmp_path / "state")
    monkeypatch.setattr("iac_code.a2a.executor.create_agent_runtime", lambda options: runtime)

    store = A2ATaskStore(metrics=NoOpA2AMetrics(), persistence=persistence)
    executor = IacCodeA2AExecutor(task_store=store, model="qwen3.6-plus")

    await executor.execute(FakeRequestContext(metadata={"iac_code": {"cwd": str(tmp_path)}}), FakeEventQueue())

    snapshot = persistence.load_task("task-1")
    assert snapshot is not None
    assert snapshot.state == "input-required"
    assert snapshot.output_text == ["persisted output"]


@pytest.mark.asyncio
async def test_executor_persists_working_state_for_interrupted_restoration(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    started = asyncio.Event()

    class SlowLoop:
        async def run_streaming(self, prompt: str):
            started.set()
            await asyncio.sleep(5)
            yield TextDeltaEvent(text="never")

    runtime = FakeRuntime(agent_loop=SlowLoop(), session_id="session-1")
    persistence = A2APersistenceStore(tmp_path / "state")
    monkeypatch.setattr("iac_code.a2a.executor.create_agent_runtime", lambda options: runtime)

    store = A2ATaskStore(metrics=NoOpA2AMetrics(), persistence=persistence)
    executor = IacCodeA2AExecutor(task_store=store, model="qwen3.6-plus")
    context = FakeRequestContext(metadata={"iac_code": {"cwd": str(tmp_path)}})
    queue = FakeEventQueue()
    running = asyncio.create_task(executor.execute(context, queue))
    await asyncio.wait_for(started.wait(), timeout=5.0)

    task_snapshot = persistence.load_task("task-1")
    context_snapshot = persistence.load_context("ctx-1")
    assert task_snapshot is not None
    assert task_snapshot.state == "working"
    assert context_snapshot is not None
    assert context_snapshot.active_task_id == "task-1"

    await executor.cancel(context, queue)
    assert running.done()
    await running


@pytest.mark.asyncio
async def test_executor_notifies_push_for_terminal_success(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    class SpyPushNotifier:
        def __init__(self) -> None:
            self.calls: list[dict[str, str]] = []

        async def notify_task_state(self, **kwargs) -> bool:
            self.calls.append(kwargs)
            return True

    loop = FakeAgentLoop([TextDeltaEvent(text="hi")])
    runtime = FakeRuntime(agent_loop=loop, session_id="session-1")
    notifier = SpyPushNotifier()
    monkeypatch.setattr("iac_code.a2a.executor.create_agent_runtime", lambda options: runtime)

    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    executor = IacCodeA2AExecutor(task_store=store, model="qwen3.6-plus", push_notifier=notifier)

    await executor.execute(FakeRequestContext(metadata={"iac_code": {"cwd": str(tmp_path)}}), FakeEventQueue())

    assert notifier.calls == [{"task_id": "task-1", "context_id": "ctx-1", "state": "input-required"}]


@pytest.mark.asyncio
async def test_executor_logs_and_swallows_push_failures(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    class FailingPushNotifier:
        async def notify_task_state(self, **kwargs) -> bool:
            raise RuntimeError("push endpoint down")

    class ExplodingLoop:
        async def run_streaming(self, prompt: str):
            raise RuntimeError("internal failure")
            yield TextDeltaEvent(text="never")

    runtime = FakeRuntime(agent_loop=ExplodingLoop(), session_id="session-1")
    monkeypatch.setattr("iac_code.a2a.executor.create_agent_runtime", lambda options: runtime)
    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    executor = IacCodeA2AExecutor(task_store=store, model="qwen3.6-plus", push_notifier=FailingPushNotifier())

    await executor.execute(FakeRequestContext(metadata={"iac_code": {"cwd": str(tmp_path)}}), FakeEventQueue())

    assert "A2A push notification failed" in caplog.text


@pytest.mark.asyncio
async def test_executor_creates_missing_workspace(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    runtime = FakeRuntime(agent_loop=FakeAgentLoop([TextDeltaEvent(text="hi")]), session_id="session-1")
    monkeypatch.setattr("iac_code.a2a.executor.create_agent_runtime", lambda options: runtime)

    missing = tmp_path / "missing"
    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    executor = IacCodeA2AExecutor(task_store=store, model="qwen3.6-plus")
    queue = FakeEventQueue()
    context = FakeRequestContext(metadata={"iac_code": {"cwd": str(missing)}})

    await executor.execute(context, queue)

    assert missing.is_dir()
    final_state = dump(queue.events[-1])["status"]["state"]
    assert final_state != "TASK_STATE_FAILED"


@pytest.mark.asyncio
async def test_executor_uses_metadata_cwd_when_process_cwd_is_deleted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    runtime = FakeRuntime(agent_loop=FakeAgentLoop([TextDeltaEvent(text="hi")]), session_id="session-1")
    monkeypatch.setattr("iac_code.a2a.executor.create_agent_runtime", lambda options: runtime)
    monkeypatch.setenv("IACCODE_A2A_ALLOWED_CWDS", str(tmp_path))

    def deleted_process_cwd() -> str:
        raise FileNotFoundError("[Errno 2] No such file or directory")

    monkeypatch.setattr("iac_code.a2a.executor.os.getcwd", deleted_process_cwd)

    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    executor = IacCodeA2AExecutor(task_store=store, model="qwen3.6-plus")
    queue = FakeEventQueue()

    await executor.execute(FakeRequestContext(metadata={"iac_code": {"cwd": str(tmp_path)}}), queue)

    assert runtime.agent_loop.prompts == ["hello"]
    final_state = dump(queue.events[-1])["status"]["state"]
    assert final_state != "TASK_STATE_FAILED"


def test_resolve_cwd_returns_logical_metadata_path_for_symlinked_workspace(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    physical_root = tmp_path / "mount-root"
    physical_root.mkdir()
    logical_root = tmp_path / "workspace"
    logical_root.symlink_to(physical_root, target_is_directory=True)
    logical_cwd = logical_root / "ctx-1"
    monkeypatch.setenv("IACCODE_A2A_ALLOWED_CWDS", str(logical_root))

    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    executor = IacCodeA2AExecutor(task_store=store, model="qwen3.6-plus")

    cwd = executor._resolve_cwd({"iac_code": {"cwd": str(logical_cwd)}})

    assert cwd == str(logical_cwd)
    assert logical_cwd.is_dir()
    assert logical_cwd.resolve() == physical_root / "ctx-1"


@pytest.mark.asyncio
async def test_executor_rejects_workspace_path_pointing_at_file(tmp_path: Path) -> None:
    file_path = tmp_path / "not-a-dir"
    file_path.write_text("blocker", encoding="utf-8")
    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    executor = IacCodeA2AExecutor(task_store=store, model="qwen3.6-plus")
    queue = FakeEventQueue()
    context = FakeRequestContext(metadata={"iac_code": {"cwd": str(file_path)}})

    await executor.execute(context, queue)

    dumped = dump(queue.events[-1])
    assert dumped["status"]["state"] == "TASK_STATE_FAILED"
    assert "workspace" in dumped["status"]["message"]["parts"][0]["text"].lower()


@pytest.mark.asyncio
async def test_executor_rejects_workspace_outside_allowed_roots(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    allowed = tmp_path / "allowed"
    outside = tmp_path / "outside"
    allowed.mkdir()
    outside.mkdir()
    monkeypatch.setenv("IACCODE_A2A_ALLOWED_CWDS", str(allowed))
    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    executor = IacCodeA2AExecutor(task_store=store, model="qwen3.6-plus")
    queue = FakeEventQueue()
    context = FakeRequestContext(metadata={"iac_code": {"cwd": str(outside)}})

    await executor.execute(context, queue)

    dumped = dump(queue.events[-1])
    assert dumped["status"]["state"] == "TASK_STATE_FAILED"
    assert "workspace" in dumped["status"]["message"]["parts"][0]["text"].lower()


def test_resolve_cwd_trusts_per_context_workspace_for_skill_runtime(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    allowed = tmp_path / "allowed"
    outside = tmp_path / "outside"
    allowed.mkdir()
    outside.mkdir()
    monkeypatch.setenv("IACCODE_A2A_ALLOWED_CWDS", str(allowed))
    monkeypatch.setenv("IAC_CODE_A2A_TRUST_REQUEST_CWD", "1")
    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    executor = IacCodeA2AExecutor(task_store=store, model="qwen3.6-plus")

    assert executor._resolve_cwd({"iac_code": {"cwd": str(outside)}}) == str(outside)


@pytest.mark.asyncio
async def test_executor_reports_invalid_task_id() -> None:
    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    executor = IacCodeA2AExecutor(task_store=store, model="qwen3.6-plus")
    queue = FakeEventQueue()
    context = FakeRequestContext(task_id="../bad")

    await executor.execute(context, queue)

    dumped = dump(queue.events[-1])
    assert dumped["status"]["state"] == "TASK_STATE_FAILED"
    assert dumped["status"]["message"]["parts"][0]["text"] == "Invalid A2A id"


@pytest.mark.asyncio
async def test_executor_rejects_empty_prompt_before_creating_runtime(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def fail_if_called(options):
        raise AssertionError("runtime should not be created for empty prompt")

    monkeypatch.setattr("iac_code.a2a.executor.create_agent_runtime", fail_if_called)

    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    executor = IacCodeA2AExecutor(task_store=store, model="qwen3.6-plus")
    queue = FakeEventQueue()
    context = FakeRequestContext(text="   ", metadata={"iac_code": {"cwd": str(tmp_path)}})

    await executor.execute(context, queue)

    dumped = dump(queue.events[-1])
    assert dumped["status"]["state"] == "TASK_STATE_FAILED"
    assert dumped["status"]["message"]["parts"][0]["text"] == "A2A server currently accepts text input only."


@pytest.mark.asyncio
async def test_executor_delegates_pipeline_mode_after_validation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from iac_code.services.telemetry.attributes import AttributeBuilder
    from iac_code.services.telemetry.identity import Identity

    monkeypatch.setenv("IAC_CODE_MODE", "normal")
    monkeypatch.setenv("IAC_CODE_A2A_SAFE_MODE", "1")
    monkeypatch.setenv("IAC_CODE_CHANNEL", "environment")
    calls = []
    captured_channels: list[str] = []
    attributes = AttributeBuilder(Identity(tmp_path / "settings.yml"), "iac-code", "0.1.0")

    class SpyPipelineExecutor:
        def __init__(self, **kwargs):
            calls.append(("init", kwargs))

        async def execute(
            self,
            *,
            context,
            event_queue,
            task,
            task_id,
            context_id,
            cwd,
            pipeline_input,
            active_followup_only=False,
        ):
            captured_channels.append(attributes.build_signal_attributes()["iac_code.channel"])
            calls.append(
                (
                    "execute",
                    {
                        "task_id": task_id,
                        "context_id": context_id,
                        "cwd": cwd,
                        "pipeline_input": pipeline_input,
                    },
                )
            )

    monkeypatch.setattr("iac_code.a2a.executor.IacCodeA2APipelineExecutor", SpyPipelineExecutor)

    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    executor = IacCodeA2AExecutor(task_store=store, model="qwen3.6-plus")

    await executor.execute(
        FakeRequestContext(
            metadata={
                "iac_code": {
                    "cwd": str(tmp_path),
                    "run_mode": "pipeline",
                    "channel": "a2a-pipeline",
                    "user_id": "client-user",
                    "iac_code_model": "metadata-model",
                    "iac_code_api_key": "metadata-api-key",
                    "thinking": {"enabled": True, "effort": "high", "budget": 2048},
                    "alibaba_cloud_access_key_id": "client-id",
                    "alibaba_cloud_access_key_secret": "client-secret",
                    "alibaba_cloud_region_id": "cn-beijing",
                    "alibaba_cloud_security_token": "client-sts",
                }
            }
        ),
        FakeEventQueue(),
    )

    init_kwargs = calls[0][1]
    assert init_kwargs["model"] == "metadata-model"
    assert init_kwargs["user_id"] == "client-user"
    assert init_kwargs["model_from_metadata"] is True
    assert init_kwargs["metadata_api_key"] == "metadata-api-key"
    assert init_kwargs["request_policy_override"].thinking_enabled is True
    assert init_kwargs["request_policy_override"].effort == "high"
    assert init_kwargs["request_policy_override"].thinking_budget == 2048
    assert init_kwargs["aliyun_credential"].access_key_id == "client-id"
    assert init_kwargs["aliyun_credential"].access_key_secret == "client-secret"
    assert init_kwargs["aliyun_credential"].region_id == "cn-beijing"
    assert init_kwargs["aliyun_credential"].sts_token == "client-sts"
    assert captured_channels == ["a2a-pipeline"]
    assert calls[-1] == (
        "execute",
        {
            "task_id": "task-1",
            "context_id": "ctx-1",
            "cwd": str(tmp_path),
            "pipeline_input": PipelineUserInput(content="hello", display_text="hello", has_images=False),
        },
    )


@pytest.mark.asyncio
async def test_executor_hydrates_running_pipeline_task_id_from_sidecar(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("IAC_CODE_MODE", "pipeline")
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    persistence = A2APersistenceStore(tmp_path / "a2a-state")
    persistence.save_context(A2AContextSnapshot(context_id="ctx-1", session_id="session-1", cwd=str(tmp_path)))
    persistence.save_task(A2ATaskSnapshot(task_id="task-1", context_id="ctx-1", state="working"))
    _ensure_v2_session(str(tmp_path), "session-1")
    journal = A2APipelineJournal(a2a_pipeline_dir_for_session(cwd=str(tmp_path), session_id="session-1"))
    journal.append(
        {
            "schemaVersion": "1.0",
            "eventId": "evt-running",
            "sequence": 1,
            "eventType": "step_started",
            "scope": "step",
            "pipelineRunId": "ctx-1",
            "pipelineName": "selling",
            "contextId": "ctx-1",
            "taskId": "task-1",
            "status": "working",
            "data": {},
        }
    )
    calls = []

    class SpyPipelineExecutor:
        def __init__(self, **kwargs):
            calls.append(("init", kwargs))

        async def execute(
            self,
            *,
            context,
            event_queue,
            task,
            task_id,
            context_id,
            cwd,
            pipeline_input,
            active_followup_only=False,
        ):
            calls.append(
                (
                    "execute",
                    {
                        "task_id": task_id,
                        "context_id": context_id,
                        "cwd": cwd,
                        "pipeline_input": pipeline_input,
                    },
                )
            )

    monkeypatch.setattr("iac_code.a2a.executor.IacCodeA2APipelineExecutor", SpyPipelineExecutor)

    store = A2ATaskStore(metrics=NoOpA2AMetrics(), persistence=persistence)
    executor = IacCodeA2AExecutor(task_store=store, model="qwen3.6-plus")

    await executor.execute(
        FakeRequestContext(
            task_id="",
            context_id="ctx-1",
            text="继续",
            metadata={"iac_code": {"cwd": str(tmp_path)}},
        ),
        FakeEventQueue(),
    )

    assert calls[-1] == (
        "execute",
        {
            "task_id": "task-1",
            "context_id": "ctx-1",
            "cwd": str(tmp_path),
            "pipeline_input": PipelineUserInput(content="继续", display_text="继续", has_images=False),
        },
    )


@pytest.mark.asyncio
async def test_pipeline_mode_accepts_image_only_input(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("IAC_CODE_MODE", "pipeline")
    pipeline_input = _image_only_pipeline_input()
    calls = []

    class CapturingPipelineExecutor:
        def __init__(self, **kwargs):
            pass

        async def execute(self, **kwargs):
            calls.append(kwargs)

    monkeypatch.setattr("iac_code.a2a.executor.IacCodeA2APipelineExecutor", CapturingPipelineExecutor)
    monkeypatch.setattr(
        IacCodeA2AExecutor,
        "_pipeline_input_from_context",
        lambda self, context, *, cwd: pipeline_input,
    )
    monkeypatch.setattr("iac_code.a2a.executor.is_model_multimodal", lambda *args, **kwargs: True)

    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    executor = IacCodeA2AExecutor(task_store=store, model="qwen3.6-plus")
    queue = FakeEventQueue()

    await executor.execute(FakeRequestContext(metadata={"iac_code": {"cwd": str(tmp_path)}}), queue)

    assert calls
    assert calls[0]["pipeline_input"] == pipeline_input
    states = [dump(event)["status"]["state"] for event in queue.events if isinstance(event, TaskStatusUpdateEvent)]
    assert "TASK_STATE_FAILED" not in states


@pytest.mark.asyncio
async def test_pipeline_mode_image_input_checks_provider_context(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("IAC_CODE_MODE", "pipeline")
    monkeypatch.setattr(
        IacCodeA2AExecutor,
        "_pipeline_input_from_context",
        lambda self, context, *, cwd: _image_only_pipeline_input(),
    )
    seen = {}

    def fake_is_model_multimodal(model, *, provider_key=None, base_url=None, api_key=None):
        seen.update(
            {
                "model": model,
                "provider_key": provider_key,
                "base_url": base_url,
                "api_key": api_key,
            }
        )
        return False

    monkeypatch.setattr("iac_code.a2a.executor.get_active_provider_key", lambda: "openai_compatible")
    monkeypatch.setattr(
        "iac_code.a2a.executor.get_provider_config",
        lambda provider_key: {"keyName": provider_key, "apiBase": "https://example.test/v1"},
    )
    monkeypatch.setattr(
        "iac_code.a2a.executor.load_credentials",
        lambda model=None: {"openai_compatible": "test-key"},
    )
    monkeypatch.setattr("iac_code.a2a.executor.is_model_multimodal", fake_is_model_multimodal)

    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    executor = IacCodeA2AExecutor(task_store=store, model="custom-vl")

    queue = FakeEventQueue()
    with pytest.raises(InvalidParamsError, match="Current model custom-vl does not support image input"):
        await executor.execute(
            FakeRequestContext(metadata={"iac_code": {"cwd": str(tmp_path)}}),
            queue,
        )

    assert seen == {
        "model": "custom-vl",
        "provider_key": "openai_compatible",
        "base_url": "https://example.test/v1",
        "api_key": "test-key",
    }
    assert not [event for event in queue.events if isinstance(event, TaskStatusUpdateEvent)]
    with pytest.raises(ValueError, match="A2A task not found"):
        await store.get_task_record("task-1")


@pytest.mark.asyncio
async def test_executor_empty_prompt_takes_precedence_over_pipeline_mode(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("IAC_CODE_MODE", "pipeline")

    def fail_if_called(options):  # noqa: ARG001
        raise AssertionError("runtime should not be created for empty prompt")

    monkeypatch.setattr("iac_code.a2a.executor.create_agent_runtime", fail_if_called)

    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    executor = IacCodeA2AExecutor(task_store=store, model="qwen3.6-plus")
    queue = FakeEventQueue()
    context = FakeRequestContext(text="   ", metadata={"iac_code": {"cwd": str(tmp_path)}})

    with pytest.raises(InvalidParamsError, match="A2A server received empty input"):
        await executor.execute(context, queue)
    assert not [event for event in queue.events if isinstance(event, TaskStatusUpdateEvent)]
    with pytest.raises(ValueError, match="A2A task not found"):
        await store.get_task_record("task-1")


@pytest.mark.asyncio
async def test_executor_workspace_errors_take_precedence_over_pipeline_mode(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("IAC_CODE_MODE", "pipeline")

    def fail_if_called(options):  # noqa: ARG001
        raise AssertionError("runtime should not be created for invalid workspace")

    monkeypatch.setattr("iac_code.a2a.executor.create_agent_runtime", fail_if_called)

    file_path = tmp_path / "not-a-dir"
    file_path.write_text("blocker", encoding="utf-8")
    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    executor = IacCodeA2AExecutor(task_store=store, model="qwen3.6-plus")
    queue = FakeEventQueue()
    context = FakeRequestContext(metadata={"iac_code": {"cwd": str(file_path)}})

    await executor.execute(context, queue)

    dumped = dump(queue.events[-1])
    assert dumped["status"]["state"] == "TASK_STATE_FAILED"
    assert "workspace" in dumped["status"]["message"]["parts"][0]["text"].lower()


@pytest.mark.asyncio
async def test_executor_runs_normal_mode_when_iac_code_mode_is_normal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("IAC_CODE_MODE", "normal")
    loop = FakeAgentLoop([TextDeltaEvent(text="normal")])
    runtime = FakeRuntime(agent_loop=loop, session_id="session-1")
    monkeypatch.setattr("iac_code.a2a.executor.create_agent_runtime", lambda options: runtime)

    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    executor = IacCodeA2AExecutor(task_store=store, model="qwen3.6-plus")
    queue = FakeEventQueue()

    await executor.execute(FakeRequestContext(metadata={"iac_code": {"cwd": str(tmp_path)}}), queue)

    assert loop.prompts == ["hello"]
    dumped = dump(queue.events[-1])
    assert dumped["status"]["state"] == "TASK_STATE_INPUT_REQUIRED"


@pytest.mark.asyncio
async def test_normal_mode_image_request_passes_image_blocks_to_agent_loop(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from a2a.types import Message, Part, Role

    monkeypatch.setenv("IAC_CODE_MODE", "normal")
    monkeypatch.setattr(
        "iac_code.a2a.parts.maybe_resize_and_downsample",
        lambda raw: SimpleNamespace(data=b"resized-image", media_type="image/webp"),
    )
    loop = FakeAgentLoop([TextDeltaEvent(text="normal")])
    runtime = FakeRuntime(agent_loop=loop, session_id="session-1")
    monkeypatch.setattr("iac_code.a2a.executor.create_agent_runtime", lambda options: runtime)

    context = FakeRequestContext(metadata={"iac_code": {"cwd": str(tmp_path)}})
    context.message = Message(
        role=Role.ROLE_USER,
        parts=[
            Part(text="请识别附件架构图", media_type="text/plain"),
            Part(raw=b"fake-image", media_type="image/png", filename="diagram.png"),
        ],
        message_id="msg-normal-image",
    )

    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    executor = IacCodeA2AExecutor(task_store=store, model="qwen3.6-plus")
    await executor.execute(context, FakeEventQueue())

    assert loop.prompts == [
        [
            TextBlock(text="请识别附件架构图"),
            ImageBlock(media_type="image/webp", data=base64.b64encode(b"resized-image").decode("ascii")),
        ]
    ]


@pytest.mark.asyncio
async def test_cancel_bypasses_context_lock(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    started = asyncio.Event()

    class SlowLoop:
        async def run_streaming(self, prompt: str):
            started.set()
            await asyncio.sleep(5)
            yield TextDeltaEvent(text="never")

    runtime = FakeRuntime(agent_loop=SlowLoop(), session_id="session-1")
    monkeypatch.setattr("iac_code.a2a.executor.create_agent_runtime", lambda options: runtime)

    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    executor = IacCodeA2AExecutor(task_store=store, model="qwen3.6-plus")
    context = FakeRequestContext(metadata={"iac_code": {"cwd": str(tmp_path)}})
    queue = FakeEventQueue()
    running = asyncio.create_task(executor.execute(context, queue))
    await asyncio.wait_for(started.wait(), timeout=5.0)

    await executor.cancel(context, queue)
    assert running.done()
    await running

    assert dump(queue.events[-1])["status"]["state"] == "TASK_STATE_CANCELED"


@pytest.mark.asyncio
async def test_cancel_running_task_discards_context_runtime_before_same_context_retry(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    started = asyncio.Event()
    factory_sessions: list[str] = []

    class BlockingLoop:
        async def run_streaming(self, prompt: str):
            started.set()
            await asyncio.Future()
            yield TextDeltaEvent(text="never")

    class FreshLoop:
        def __init__(self) -> None:
            self.prompts: list[str] = []

        async def run_streaming(self, prompt: str):
            self.prompts.append(prompt)
            yield TextDeltaEvent(text="fresh retry")

    fresh_loop = FreshLoop()

    def runtime_factory(options):
        factory_sessions.append(options.session_id)
        if len(factory_sessions) == 1:
            return FakeRuntime(agent_loop=BlockingLoop(), session_id=options.session_id)
        return FakeRuntime(agent_loop=fresh_loop, session_id=options.session_id)

    monkeypatch.setattr("iac_code.a2a.executor.create_agent_runtime", runtime_factory)

    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    executor = IacCodeA2AExecutor(task_store=store, model="qwen3.6-plus")
    first = FakeRequestContext(task_id="task-1", context_id="ctx-1", metadata={"iac_code": {"cwd": str(tmp_path)}})
    first_queue = FakeEventQueue()
    running = asyncio.create_task(executor.execute(first, first_queue))
    await asyncio.wait_for(started.wait(), timeout=5.0)

    await executor.cancel(first, first_queue)
    await running

    second_queue = FakeEventQueue()
    await executor.execute(
        FakeRequestContext(task_id="task-2", context_id="ctx-1", metadata={"iac_code": {"cwd": str(tmp_path)}}),
        second_queue,
    )

    assert len(factory_sessions) == 2
    assert factory_sessions[0] == factory_sessions[1]
    assert fresh_loop.prompts == ["hello"]
    assert dump(first_queue.events[-1])["status"]["state"] == "TASK_STATE_CANCELED"
    assert dump(second_queue.events[-1])["status"]["state"] == "TASK_STATE_INPUT_REQUIRED"


@pytest.mark.asyncio
async def test_same_context_concurrent_message_is_rejected(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    started = asyncio.Event()

    class SlowLoop:
        async def run_streaming(self, prompt: str):
            started.set()
            await asyncio.sleep(5)
            yield TextDeltaEvent(text="never")

    runtime = FakeRuntime(agent_loop=SlowLoop(), session_id="session-1")
    monkeypatch.setattr("iac_code.a2a.executor.create_agent_runtime", lambda options: runtime)

    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    executor = IacCodeA2AExecutor(task_store=store, model="qwen3.6-plus")
    first = FakeRequestContext(task_id="task-1", context_id="ctx-1", metadata={"iac_code": {"cwd": str(tmp_path)}})
    second = FakeRequestContext(task_id="task-2", context_id="ctx-1", metadata={"iac_code": {"cwd": str(tmp_path)}})
    first_queue = FakeEventQueue()
    second_queue = FakeEventQueue()
    running = asyncio.create_task(executor.execute(first, first_queue))
    await asyncio.wait_for(started.wait(), timeout=5.0)

    await executor.execute(second, second_queue)
    await executor.cancel(first, first_queue)
    await running

    dumped = dump(second_queue.events[-1])
    assert dumped["status"]["state"] == "TASK_STATE_FAILED"
    assert "already working" in dumped["status"]["message"]["parts"][0]["text"]


@pytest.mark.asyncio
async def test_same_context_lock_race_fails_fast(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    class ContendedLock:
        def __init__(self) -> None:
            self.acquire_requested = asyncio.Event()
            self.acquire_waiter = asyncio.get_running_loop().create_future()

        def acquire(self) -> asyncio.Future[bool]:
            self.acquire_requested.set()
            return self.acquire_waiter

        def release(self) -> None:
            raise AssertionError("release should not be called when acquire times out")

    runtime = FakeRuntime(agent_loop=FakeAgentLoop([TextDeltaEvent(text="never")]), session_id="session-1")
    monkeypatch.setattr("iac_code.a2a.executor.create_agent_runtime", lambda options: runtime)
    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    ctx = await store.get_or_create_context(
        context_id="ctx-1",
        cwd=str(tmp_path),
        runtime_factory=lambda sid: runtime,
    )
    lock = ContendedLock()
    ctx.lock = lock

    async def deterministic_timeout(awaitable, timeout):
        assert awaitable is lock.acquire_waiter
        assert timeout == 1
        raise TimeoutError

    monkeypatch.setattr("iac_code.a2a.executor.asyncio.wait_for", deterministic_timeout)
    executor = IacCodeA2AExecutor(task_store=store, model="qwen3.6-plus")
    queue = FakeEventQueue()

    await executor.execute(
        FakeRequestContext(
            task_id="task-2",
            context_id="ctx-1",
            metadata={"iac_code": {"cwd": str(tmp_path)}},
        ),
        queue,
    )

    assert lock.acquire_requested.is_set()
    dumped = dump(queue.events[-1])
    assert dumped["status"]["state"] == "TASK_STATE_FAILED"
    assert "already working" in dumped["status"]["message"]["parts"][0]["text"]


@pytest.mark.asyncio
async def test_cancelling_during_context_lock_acquire_clears_active_task(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    class ContendedLock:
        def __init__(self) -> None:
            self.acquire_requested = asyncio.Event()
            self.acquire_waiter = asyncio.get_running_loop().create_future()

        def acquire(self) -> asyncio.Future[bool]:
            self.acquire_requested.set()
            return self.acquire_waiter

        def release(self) -> None:
            raise AssertionError("release should not be called when acquire is cancelled")

    runtime = FakeRuntime(agent_loop=FakeAgentLoop([TextDeltaEvent(text="never")]), session_id="session-1")
    monkeypatch.setattr("iac_code.a2a.executor.create_agent_runtime", lambda options: runtime)
    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    ctx = await store.get_or_create_context(
        context_id="ctx-1",
        cwd=str(tmp_path),
        runtime_factory=lambda sid: runtime,
    )
    lock = ContendedLock()
    ctx.lock = lock
    executor = IacCodeA2AExecutor(task_store=store, model="qwen3.6-plus")
    queue = FakeEventQueue()

    run_task = asyncio.create_task(
        executor.execute(
            FakeRequestContext(
                task_id="task-lock-cancel",
                context_id="ctx-1",
                metadata={"iac_code": {"cwd": str(tmp_path)}},
            ),
            queue,
        )
    )
    await lock.acquire_requested.wait()
    task_record = await store.get_or_create_task(task_id="task-lock-cancel", context_id="ctx-1")
    assert task_record.active_task is run_task

    run_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await run_task

    assert task_record.active_task is None
    assert task_record.state == "canceled"
    dumped = dump(queue.events[-1])
    assert dumped["status"]["state"] == "TASK_STATE_CANCELED"


@pytest.mark.asyncio
async def test_independent_contexts_execute_concurrently(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    prompts: list[str] = []

    class FastLoop:
        async def run_streaming(self, prompt: str):
            prompts.append(prompt)
            await asyncio.sleep(0)
            yield TextDeltaEvent(text=prompt)

    monkeypatch.setattr(
        "iac_code.a2a.executor.create_agent_runtime",
        lambda options: FakeRuntime(agent_loop=FastLoop(), session_id=options.session_id),
    )

    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    executor = IacCodeA2AExecutor(task_store=store, model="qwen3.6-plus")
    await asyncio.gather(
        executor.execute(
            FakeRequestContext(
                task_id="task-1", context_id="ctx-1", text="one", metadata={"iac_code": {"cwd": str(tmp_path)}}
            ),
            FakeEventQueue(),
        ),
        executor.execute(
            FakeRequestContext(
                task_id="task-2", context_id="ctx-2", text="two", metadata={"iac_code": {"cwd": str(tmp_path)}}
            ),
            FakeEventQueue(),
        ),
    )

    assert sorted(prompts) == ["one", "two"]


@pytest.mark.asyncio
async def test_executor_overrides_telemetry_session_id_per_context(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Per-context conversation session_id must surface in telemetry while
    run_streaming is executing, instead of the process-level bootstrap id."""
    from iac_code.services.telemetry import bootstrap_telemetry, get_session_id, set_client
    from iac_code.services.telemetry.identity import SESSION_ID_PREFIX

    monkeypatch.setenv("DISABLE_TELEMETRY", "1")
    monkeypatch.setenv("HOME", str(tmp_path))
    set_client(None)
    bootstrap_telemetry(session_id="a2a-server-process")
    try:
        process_level = get_session_id()
        assert process_level == f"{SESSION_ID_PREFIX}a2a-server-process"

        observed: dict[str, str] = {}

        class ObservingLoop:
            def __init__(self, label: str) -> None:
                self._label = label

            async def run_streaming(self, prompt: str):
                observed[self._label] = get_session_id()
                yield TextDeltaEvent(text="ok")

        def factory(options):
            return FakeRuntime(
                agent_loop=ObservingLoop(label=options.session_id),
                session_id=options.session_id,
            )

        monkeypatch.setattr("iac_code.a2a.executor.create_agent_runtime", factory)

        store = A2ATaskStore(metrics=NoOpA2AMetrics())
        executor = IacCodeA2AExecutor(task_store=store, model="qwen3.6-plus")

        await executor.execute(
            FakeRequestContext(task_id="task-1", context_id="ctx-1", metadata={"iac_code": {"cwd": str(tmp_path)}}),
            FakeEventQueue(),
        )
        await executor.execute(
            FakeRequestContext(task_id="task-2", context_id="ctx-2", metadata={"iac_code": {"cwd": str(tmp_path)}}),
            FakeEventQueue(),
        )

        session_one = store._contexts["ctx-1"].session_id
        session_two = store._contexts["ctx-2"].session_id
        assert session_one != session_two
        assert observed[session_one] == f"{SESSION_ID_PREFIX}{session_one}"
        assert observed[session_two] == f"{SESSION_ID_PREFIX}{session_two}"
        # And the per-context override does not leak back to the parent scope.
        assert get_session_id() == process_level
    finally:
        set_client(None)


@pytest.mark.asyncio
async def test_executor_resumes_messages_after_restart(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from iac_code.a2a.persistence import A2APersistenceStore
    from iac_code.agent.message import Message
    from iac_code.services.session_storage import SessionStorage

    config_dir = tmp_path / "config"
    config_dir.mkdir()
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(config_dir))

    cwd = tmp_path / "ws"
    cwd.mkdir()

    seen_resume: list[object | None] = []

    def fake_factory(options):
        seen_resume.append(options.resume_messages)
        return FakeRuntime(
            agent_loop=FakeAgentLoop([TextDeltaEvent(text="ok")]),
            session_id=options.session_id,
        )

    monkeypatch.setattr("iac_code.a2a.executor.create_agent_runtime", fake_factory)

    persistence = A2APersistenceStore(tmp_path / "a2a")

    store_one = A2ATaskStore(metrics=NoOpA2AMetrics(), persistence=persistence)
    executor_one = IacCodeA2AExecutor(task_store=store_one, model="qwen3.6-plus")
    ctx_one = FakeRequestContext(
        task_id="task-1",
        context_id="ctx-shared",
        text="hi-1",
        metadata={"iac_code": {"cwd": str(cwd)}},
    )
    await executor_one.execute(ctx_one, FakeEventQueue())
    session_id = store_one._contexts["ctx-shared"].session_id

    SessionStorage().append(str(cwd), session_id, Message(role="user", content="prior turn"))

    store_two = A2ATaskStore(metrics=NoOpA2AMetrics(), persistence=persistence)
    executor_two = IacCodeA2AExecutor(task_store=store_two, model="qwen3.6-plus")
    ctx_two = FakeRequestContext(
        task_id="task-2",
        context_id="ctx-shared",
        text="hi-2",
        metadata={"iac_code": {"cwd": str(cwd)}},
    )
    await executor_two.execute(ctx_two, FakeEventQueue())

    assert store_two._contexts["ctx-shared"].session_id == session_id
    assert seen_resume[0] is None
    assert seen_resume[1] is not None
    assert any(getattr(m, "content", "") == "prior turn" for m in seen_resume[1])


@pytest.mark.asyncio
async def test_pipeline_handoff_context_routes_followup_to_normal_after_restart(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from iac_code.a2a.pipeline_paths import a2a_pipeline_dir_for_session
    from iac_code.a2a.pipeline_snapshot import A2APipelineSnapshotStore
    from iac_code.agent.message import Message
    from iac_code.services.session_storage import SessionStorage

    monkeypatch.setenv("IAC_CODE_MODE", "pipeline")
    monkeypatch.setenv("IAC_CODE_CHANNEL", "environment")
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(config_dir))

    cwd = tmp_path / "ws"
    cwd.mkdir()
    session_id = "session-handoff"
    context_id = "ctx-handoff"
    persistence = A2APersistenceStore(tmp_path / "a2a")
    persistence.save_context(
        A2AContextSnapshot(
            context_id=context_id,
            session_id=session_id,
            cwd=str(cwd),
            telemetry_channel="a2a-pipeline",
        )
    )
    _ensure_v2_session(str(cwd), session_id)
    pipeline_dir = a2a_pipeline_dir_for_session(cwd=str(cwd), session_id=session_id)
    handoff_events = _committed_normal_handoff_events(
        context_id=context_id,
        task_id="task-pipeline",
        summary="[Pipeline Handoff Context]\nPipeline: selling",
    )
    A2APipelineJournal(pipeline_dir).append_many(handoff_events, durable=True)
    A2APipelineSnapshotStore(pipeline_dir).save(reduce_pipeline_events(handoff_events))
    SessionStorage().append(str(cwd), session_id, Message(role="user", content="[Pipeline Handoff Context]"))

    from iac_code.services.telemetry.attributes import AttributeBuilder
    from iac_code.services.telemetry.identity import Identity

    attributes = AttributeBuilder(Identity(tmp_path / "settings.yml"), "iac-code", "0.1.0")
    captured_channels: list[str] = []
    original_run_streaming = FakeAgentLoop.run_streaming

    async def capturing_run_streaming(self, prompt):
        captured_channels.append(attributes.build_signal_attributes()["iac_code.channel"])
        async for event in original_run_streaming(self, prompt):
            yield event

    monkeypatch.setattr(FakeAgentLoop, "run_streaming", capturing_run_streaming)
    loop = FakeAgentLoop([TextDeltaEvent(text="normal-ok")])
    seen_resume: list[object | None] = []

    def fake_factory(options):
        seen_resume.append(options.resume_messages)
        return FakeRuntime(agent_loop=loop, session_id=options.session_id)

    class FailingPipelineExecutor:
        def __init__(self, **kwargs) -> None:
            pass

        async def execute(self, **kwargs) -> None:
            raise AssertionError("pipeline executor should not be used after normal handoff")

    monkeypatch.setattr("iac_code.a2a.executor.create_agent_runtime", fake_factory)
    monkeypatch.setattr("iac_code.a2a.executor.IacCodeA2APipelineExecutor", FailingPipelineExecutor)

    store = A2ATaskStore(metrics=NoOpA2AMetrics(), persistence=persistence)
    executor = IacCodeA2AExecutor(task_store=store, model="qwen3.6-plus")
    context = FakeRequestContext(
        task_id="task-followup",
        context_id=context_id,
        text="继续解释一下",
        metadata={"iac_code": {"cwd": str(cwd)}},
    )
    await executor.execute(context, FakeEventQueue())

    assert loop.prompts == ["继续解释一下"]
    assert captured_channels == ["a2a-pipeline"]
    assert store._contexts[context_id].session_id == session_id
    assert seen_resume and seen_resume[0] is not None
    assert any(getattr(message, "content", "") == "[Pipeline Handoff Context]" for message in seen_resume[0])


@pytest.mark.asyncio
async def test_pipeline_handoff_route_replays_newer_backup_blocked_journal(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("IAC_CODE_MODE", "pipeline")

    cwd = tmp_path / "ws"
    cwd.mkdir()
    session_id = "session-stale-blocked"
    context_id = "ctx-stale-blocked"
    task_id = "task-stale-blocked"
    persistence = A2APersistenceStore(tmp_path / "a2a")
    persistence.save_context(A2AContextSnapshot(context_id=context_id, session_id=session_id, cwd=str(cwd)))
    _ensure_v2_session(str(cwd), session_id)

    def event(sequence: int, event_type: str, status: str, *, data: dict | None = None) -> dict:
        return {
            "schemaVersion": "1.0",
            "eventId": f"evt-{sequence}",
            "sequence": sequence,
            "eventType": event_type,
            "scope": "pipeline",
            "pipelineRunId": context_id,
            "taskId": task_id,
            "contextId": context_id,
            "pipelineName": "selling",
            "status": status,
            "data": data or {},
        }

    pending_input = {
        "inputId": "ask-ask-1",
        "kind": "ask_user_question",
        "toolUseId": "ask-1",
        "question": "请选择部署目标",
        "options": [{"id": "nginx", "label": "Nginx 网站"}],
        "allowFreeText": True,
    }
    input_required = event(1, "input_required", "input_required", data={"kind": "ask_user_question"})
    input_required["input"] = pending_input
    canceled = event(2, "pipeline_canceled", "canceled", data={"source": "a2a_cancel"})
    handoff = event(
        3,
        "pipeline_handoff_ready",
        "canceled",
        data={
            "action": "switch_to_normal",
            "targetMode": "normal",
            "outcome": "canceled",
            "summary": "[Pipeline Handoff Context]\nOutcome: canceled",
        },
    )
    backup_blocked = event(
        4,
        "backup_blocked",
        "input_required",
        data={"reason": "terminal", "error": "Backup blocked.", "recoverable": True},
    )
    backup_blocked["input"] = pending_input

    pipeline_dir = a2a_pipeline_dir_for_session(cwd=str(cwd), session_id=session_id)
    journal = A2APipelineJournal(pipeline_dir)
    journal.append_many([input_required, canceled, handoff, backup_blocked], durable=True)
    snapshot_store = A2APipelineSnapshotStore(pipeline_dir)
    snapshot_store.save(
        {
            "schemaVersion": "1.1",
            "pipelineRunId": context_id,
            "taskId": task_id,
            "contextId": context_id,
            "pipelineName": "selling",
            "status": "canceled",
            "lastSequence": 3,
            "pendingInput": None,
            "normalHandoff": {
                "action": "switch_to_normal",
                "targetMode": "normal",
                "outcome": "canceled",
                "summary": "[Pipeline Handoff Context]\nOutcome: canceled",
            },
        }
    )

    executor = IacCodeA2AExecutor(
        task_store=A2ATaskStore(metrics=NoOpA2AMetrics(), persistence=persistence),
        model="qwen3.6-plus",
    )

    assert not await executor._should_route_pipeline_handoff_to_normal(context_id=context_id, cwd=str(cwd))
    repaired = snapshot_store.load()
    assert repaired is not None
    assert repaired["status"] == "waiting_input"
    assert repaired["lastSequence"] == 4
    assert repaired["pendingInput"]["kind"] == "ask_user_question"
    assert repaired["normalHandoff"] is None
    assert recoverable_task_id_from_sidecar(cwd=str(cwd), session_id=session_id, context_id=context_id) == task_id


@pytest.mark.asyncio
async def test_pipeline_handoff_image_request_passes_image_blocks_to_normal_agent(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from a2a.types import Message, Part, Role

    from iac_code.a2a.pipeline_paths import a2a_pipeline_dir_for_session
    from iac_code.a2a.pipeline_snapshot import A2APipelineSnapshotStore
    from iac_code.agent.message import Message as AgentMessage
    from iac_code.services.session_storage import SessionStorage

    monkeypatch.setenv("IAC_CODE_MODE", "pipeline")
    monkeypatch.setattr(
        "iac_code.a2a.parts.maybe_resize_and_downsample",
        lambda raw: SimpleNamespace(data=b"resized-handoff-image", media_type="image/png"),
    )
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(config_dir))

    cwd = tmp_path / "ws"
    cwd.mkdir()
    session_id = "session-handoff"
    context_id = "ctx-handoff"
    persistence = A2APersistenceStore(tmp_path / "a2a")
    persistence.save_context(A2AContextSnapshot(context_id=context_id, session_id=session_id, cwd=str(cwd)))
    _ensure_v2_session(str(cwd), session_id)
    pipeline_dir = a2a_pipeline_dir_for_session(cwd=str(cwd), session_id=session_id)
    handoff_events = _committed_normal_handoff_events(
        context_id=context_id,
        task_id="task-pipeline",
        summary="handoff",
    )
    A2APipelineJournal(pipeline_dir).append_many(handoff_events, durable=True)
    A2APipelineSnapshotStore(pipeline_dir).save(reduce_pipeline_events(handoff_events))
    SessionStorage().append(str(cwd), session_id, AgentMessage(role="user", content="handoff"))

    def fail_pipeline_input(*args, **kwargs):
        raise AssertionError("normal handoff must not build PipelineUserInput")

    monkeypatch.setattr(IacCodeA2AExecutor, "_pipeline_input_from_context", fail_pipeline_input)
    loop = FakeAgentLoop([TextDeltaEvent(text="normal-ok")])
    monkeypatch.setattr(
        "iac_code.a2a.executor.create_agent_runtime",
        lambda options: FakeRuntime(agent_loop=loop, session_id=options.session_id),
    )

    class FailingPipelineExecutor:
        def __init__(self, **kwargs) -> None:
            pass

        async def execute(self, **kwargs) -> None:
            raise AssertionError("pipeline executor should not be used after normal handoff")

    monkeypatch.setattr("iac_code.a2a.executor.IacCodeA2APipelineExecutor", FailingPipelineExecutor)

    context = FakeRequestContext(
        task_id="task-followup",
        context_id=context_id,
        text="",
        metadata={"iac_code": {"cwd": str(cwd)}},
    )
    context.message = Message(
        role=Role.ROLE_USER,
        parts=[Part(raw=b"\x89PNG\r\n\x1a\nimage", media_type="image/png", filename="diagram.png")],
        message_id="msg-1",
    )

    executor = IacCodeA2AExecutor(
        task_store=A2ATaskStore(metrics=NoOpA2AMetrics(), persistence=persistence),
        model="qwen3.6-plus",
    )
    await executor.execute(
        context,
        FakeEventQueue(),
    )

    assert loop.prompts == [
        [ImageBlock(media_type="image/png", data=base64.b64encode(b"resized-handoff-image").decode("ascii"))]
    ]


@pytest.mark.asyncio
async def test_pipeline_handoff_context_is_backfilled_from_snapshot_when_session_missing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from iac_code.a2a.pipeline_paths import a2a_pipeline_dir_for_session
    from iac_code.a2a.pipeline_snapshot import A2APipelineSnapshotStore
    from iac_code.services.session_storage import SessionStorage

    monkeypatch.setenv("IAC_CODE_MODE", "pipeline")
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(config_dir))

    cwd = tmp_path / "ws"
    cwd.mkdir()
    session_id = "session-handoff"
    context_id = "ctx-handoff"
    summary = "[Pipeline Handoff Context]\nPipeline: selling"
    persistence = A2APersistenceStore(tmp_path / "a2a")
    persistence.save_context(A2AContextSnapshot(context_id=context_id, session_id=session_id, cwd=str(cwd)))
    _ensure_v2_session(str(cwd), session_id)
    pipeline_dir = a2a_pipeline_dir_for_session(cwd=str(cwd), session_id=session_id)
    handoff_events = _committed_normal_handoff_events(
        context_id=context_id,
        task_id="task-pipeline",
        summary=summary,
    )
    A2APipelineJournal(pipeline_dir).append_many(handoff_events, durable=True)
    A2APipelineSnapshotStore(pipeline_dir).save(reduce_pipeline_events(handoff_events))

    loop = FakeAgentLoop([TextDeltaEvent(text="normal-ok")])
    seen_resume: list[object | None] = []

    def fake_factory(options):
        seen_resume.append(options.resume_messages)
        return FakeRuntime(agent_loop=loop, session_id=options.session_id)

    class FailingPipelineExecutor:
        def __init__(self, **kwargs) -> None:
            pass

        async def execute(self, **kwargs) -> None:
            raise AssertionError("pipeline executor should not be used after normal handoff")

    monkeypatch.setattr("iac_code.a2a.executor.create_agent_runtime", fake_factory)
    monkeypatch.setattr("iac_code.a2a.executor.IacCodeA2APipelineExecutor", FailingPipelineExecutor)

    store = A2ATaskStore(metrics=NoOpA2AMetrics(), persistence=persistence)
    executor = IacCodeA2AExecutor(task_store=store, model="qwen3.6-plus")
    await executor.execute(
        FakeRequestContext(
            task_id="task-followup",
            context_id=context_id,
            text="继续解释一下",
            metadata={"iac_code": {"cwd": str(cwd)}},
        ),
        FakeEventQueue(),
    )

    assert loop.prompts == ["继续解释一下"]
    assert seen_resume and seen_resume[0] is not None
    assert any(getattr(message, "content", "") == summary for message in seen_resume[0])
    loaded = SessionStorage().load(str(cwd), session_id)
    assert loaded is not None
    assert any(getattr(message, "content", "") == summary for message in loaded)


@pytest.mark.asyncio
async def test_pipeline_handoff_context_routes_and_backfills_public_summary_from_journal_when_snapshot_corrupt(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from iac_code.a2a.pipeline_paths import a2a_pipeline_dir_for_session
    from iac_code.pipeline.engine.cleanup import CleanupLedger, CleanupResource
    from iac_code.services.session_storage import SessionStorage

    monkeypatch.setenv("IAC_CODE_MODE", "pipeline")
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(config_dir))

    cwd = tmp_path / "ws"
    cwd.mkdir()
    session_id = "session-handoff"
    context_id = "ctx-handoff"
    summary = "[Pipeline Handoff Context]\nPipeline: selling"
    cleanup_prompt = "cleanup prompt for stack-123"
    persistence = A2APersistenceStore(tmp_path / "a2a")
    persistence.save_context(A2AContextSnapshot(context_id=context_id, session_id=session_id, cwd=str(cwd)))
    _ensure_v2_session(str(cwd), session_id)
    pipeline_dir = a2a_pipeline_dir_for_session(cwd=str(cwd), session_id=session_id)
    pipeline_dir.mkdir(parents=True, exist_ok=True)
    (pipeline_dir / "a2a-snapshot.json").write_text("{broken", encoding="utf-8")
    handoff_events = _committed_normal_handoff_events(
        context_id=context_id,
        task_id="task-pipeline",
        summary=summary,
        extra_data={
            "cleanup": {
                "status": "pending",
                "resourceCount": 1,
                "prompt": cleanup_prompt,
                "resources": [{"resourceId": "stack-123", "regionId": "cn-hangzhou"}],
            }
        },
    )
    A2APipelineJournal(pipeline_dir).append_many(handoff_events, durable=True)
    ledger = CleanupLedger(SessionStorage().session_dir(str(cwd), session_id) / "pipeline" / "cleanup.yaml")
    ledger.mark_cleanup_required(
        [
            CleanupResource(
                provider="ros",
                resource_type="stack",
                resource_id="stack-123",
                region_id="cn-hangzhou",
                cleanup_status="completed",
                progress_status="DELETE_COMPLETE",
            )
        ],
        source_step_id="deploying",
        reason="rollback",
    )

    loop = FakeAgentLoop([TextDeltaEvent(text="normal-ok")])
    seen_resume: list[object | None] = []

    def fake_factory(options):
        seen_resume.append(options.resume_messages)
        return FakeRuntime(agent_loop=loop, session_id=options.session_id)

    class FailingPipelineExecutor:
        def __init__(self, **kwargs) -> None:
            pass

        async def execute(self, **kwargs) -> None:
            raise AssertionError("pipeline executor should not be used after normal handoff")

    monkeypatch.setattr("iac_code.a2a.executor.create_agent_runtime", fake_factory)
    monkeypatch.setattr("iac_code.a2a.executor.IacCodeA2APipelineExecutor", FailingPipelineExecutor)

    store = A2ATaskStore(metrics=NoOpA2AMetrics(), persistence=persistence)
    executor = IacCodeA2AExecutor(task_store=store, model="qwen3.6-plus")
    await executor.execute(
        FakeRequestContext(
            task_id="task-followup",
            context_id=context_id,
            text="继续解释一下",
            metadata={"iac_code": {"cwd": str(cwd)}},
        ),
        FakeEventQueue(),
    )

    assert loop.prompts == ["继续解释一下"]
    assert seen_resume and seen_resume[0] is not None
    assert any(getattr(message, "content", "") == summary for message in seen_resume[0])
    assert not any(getattr(message, "content", "") == cleanup_prompt for message in seen_resume[0])
    loaded = SessionStorage().load(str(cwd), session_id)
    assert loaded is not None
    assert any(getattr(message, "content", "") == summary for message in loaded)
    assert not any(getattr(message, "content", "") == cleanup_prompt for message in loaded)


@pytest.mark.asyncio
async def test_auth_error_is_sanitized(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def raise_auth_error(options):
        raise ValueError("provider not configured: secret internal detail")

    monkeypatch.setattr("iac_code.a2a.executor.create_agent_runtime", raise_auth_error)

    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    executor = IacCodeA2AExecutor(task_store=store, model="qwen3.6-plus")
    queue = FakeEventQueue()
    context = FakeRequestContext(metadata={"iac_code": {"cwd": str(tmp_path)}})

    await executor.execute(context, queue)

    dumped = dump(queue.events[-1])
    assert dumped["status"]["state"] == "TASK_STATE_FAILED"
    assert (
        dumped["status"]["message"]["parts"][0]["text"] == "Authentication required. Configure credentials and retry."
    )


@pytest.mark.asyncio
async def test_retryable_executor_error_returns_input_required(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    class TimeoutLoop:
        async def run_streaming(self, prompt: str):
            raise TimeoutError("upstream timed out")
            yield TextDeltaEvent(text="never")

    runtime = FakeRuntime(agent_loop=TimeoutLoop(), session_id="session-1")
    monkeypatch.setattr("iac_code.a2a.executor.create_agent_runtime", lambda options: runtime)

    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    backup_service = SnapshotReadingBackupService()
    executor = IacCodeA2AExecutor(task_store=store, model="qwen3.6-plus", backup_service=backup_service)
    queue = FakeEventQueue()
    context = FakeRequestContext(metadata={"iac_code": {"cwd": str(tmp_path)}})

    await executor.execute(context, queue)

    dumped = dump(queue.events[-1])
    assert dumped["status"]["state"] == "TASK_STATE_INPUT_REQUIRED"
    assert dumped["status"]["message"]["parts"][0]["text"] == "A temporary error occurred. Please retry."
    assert [(reason, critical) for *_ids, reason, critical in backup_service.calls] == [
        (BackupReason.INPUT_REQUIRED, False)
    ]
    reason, task_snapshot, context_snapshot = backup_service.snapshots[0]
    assert reason == BackupReason.INPUT_REQUIRED
    assert task_snapshot["state"] == "input-required"
    assert context_snapshot["active_task_id"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("exc", "state"),
    [
        (MCPConnectionError("MCP server 'remote' connection failed"), MCPConnectionState.FAILED),
        (MCPNeedsAuthError("MCP server 'remote' requires authentication"), MCPConnectionState.NEEDS_AUTH),
    ],
)
async def test_mcp_stream_error_after_text_returns_input_required(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    exc: Exception,
    state: MCPConnectionState,
) -> None:
    class MCPErrorAfterTextLoop:
        async def run_streaming(self, prompt: str):
            yield TextDeltaEvent(text="partial reply")
            raise exc

    runtime = FakeRuntime(
        agent_loop=MCPErrorAfterTextLoop(),
        session_id="session-1",
        mcp_manager=SimpleNamespace(
            list_connections=lambda: [
                SimpleNamespace(
                    name="remote",
                    state=state,
                    error=str(exc),
                    auth_error=str(exc) if state is MCPConnectionState.NEEDS_AUTH else None,
                    required_auth_scopes=["write:stack"] if state is MCPConnectionState.NEEDS_AUTH else [],
                    auth_resource_metadata_url=None,
                    capability_errors={},
                    tools=[],
                    resources=[],
                    prompts=[],
                    retry_count=0,
                    metadata=MCPConnectionMetadata(state=state, server_name="remote"),
                    scoped_config=ScopedMCPServerConfig(
                        config=MCPServerConfig.from_mapping(
                            "remote",
                            {"type": "http", "url": "https://example.com/mcp"},
                        ),
                        scope=MCPConfigScope.USER,
                    ),
                )
            ]
        ),
    )
    monkeypatch.setattr("iac_code.a2a.executor.create_agent_runtime", lambda options: runtime)

    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    executor = IacCodeA2AExecutor(task_store=store, model="qwen3.6-plus")
    queue = FakeEventQueue()

    await executor.execute(FakeRequestContext(metadata={"iac_code": {"cwd": str(tmp_path)}}), queue)

    status_events = [dump(event) for event in queue.events if isinstance(event, TaskStatusUpdateEvent)]
    rendered_events = [dump(event) for event in queue.events if isinstance(event, Task | TaskStatusUpdateEvent)]
    assert any("partial reply" in json.dumps(event, ensure_ascii=False) for event in rendered_events)
    assert any("mcpStatus" in event.get("metadata", {}).get("iac_code", {}) for event in status_events)
    assert status_events[-1]["status"]["state"] == "TASK_STATE_INPUT_REQUIRED"
    record = await store.get_or_create_task(task_id="task-1", context_id="ctx-1")
    assert record.state == "input-required"
    assert "".join(record.output_text) == "partial reply"


@pytest.mark.asyncio
async def test_retryable_setup_error_returns_input_required(tmp_path: Path) -> None:
    class TimeoutTaskStore(A2ATaskStore):
        async def ensure_task_not_expired(self, task_id: str) -> None:
            raise TimeoutError("task store timed out")

    store = TimeoutTaskStore(metrics=NoOpA2AMetrics())
    executor = IacCodeA2AExecutor(task_store=store, model="qwen3.6-plus")
    queue = FakeEventQueue()
    context = FakeRequestContext(metadata={"iac_code": {"cwd": str(tmp_path)}})

    await executor.execute(context, queue)

    dumped = dump(queue.events[-1])
    assert dumped["status"]["state"] == "TASK_STATE_INPUT_REQUIRED"
    assert dumped["status"]["message"]["parts"][0]["text"] == "A temporary error occurred. Please retry."


@pytest.mark.asyncio
async def test_setup_failure_logs_stage_without_raw_traceback(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    class FailingTaskStore(A2ATaskStore):
        async def ensure_task_not_expired(self, task_id: str) -> None:
            raise FileNotFoundError(2, "No such file or directory")

    caplog.set_level(logging.ERROR, logger="iac_code.a2a.executor")

    store = FailingTaskStore(metrics=NoOpA2AMetrics())
    executor = IacCodeA2AExecutor(task_store=store, model="qwen3.6-plus")
    queue = FakeEventQueue()
    context = FakeRequestContext(metadata={"iac_code": {"cwd": str(tmp_path)}})

    await executor.execute(context, queue)

    dumped = dump(queue.events[-1])
    assert dumped["status"]["state"] == "TASK_STATE_FAILED"
    assert dumped["status"]["message"]["parts"][0]["text"] == "[Errno 2] No such file or directory"
    assert "A2A executor setup failed" in caplog.text
    assert "task_id=task-1" in caplog.text
    assert "context_id=ctx-1" in caplog.text
    assert "Traceback (most recent call last)" not in caplog.text
    assert "FileNotFoundError: [Errno 2] No such file or directory" not in caplog.text


@pytest.mark.asyncio
async def test_runtime_creation_failure_logs_stage_without_raw_traceback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    def raise_missing_dependency(options):
        raise FileNotFoundError(2, "No such file or directory")

    caplog.set_level(logging.ERROR, logger="iac_code.a2a.executor")
    monkeypatch.setattr("iac_code.a2a.executor.create_agent_runtime", raise_missing_dependency)

    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    executor = IacCodeA2AExecutor(task_store=store, model="qwen3.6-plus")
    queue = FakeEventQueue()
    context = FakeRequestContext(metadata={"iac_code": {"cwd": str(tmp_path)}})

    await executor.execute(context, queue)

    dumped = dump(queue.events[-1])
    assert dumped["status"]["state"] == "TASK_STATE_FAILED"
    assert dumped["status"]["message"]["parts"][0]["text"].startswith("FileNotFoundError:")
    assert "A2A executor runtime setup failed" in caplog.text
    assert "task_id=task-1" in caplog.text
    assert "context_id=ctx-1" in caplog.text
    assert "Traceback (most recent call last)" not in caplog.text
    assert "FileNotFoundError: [Errno 2] No such file or directory" not in caplog.text


@pytest.mark.asyncio
async def test_streaming_failure_logs_stage_without_raw_traceback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    class ExplodingLoop:
        async def run_streaming(self, prompt: str):
            raise FileNotFoundError(2, "No such file or directory")
            yield TextDeltaEvent(text="never")

    caplog.set_level(logging.ERROR, logger="iac_code.a2a.executor")
    runtime = FakeRuntime(agent_loop=ExplodingLoop(), session_id="session-1")
    monkeypatch.setattr("iac_code.a2a.executor.create_agent_runtime", lambda options: runtime)

    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    executor = IacCodeA2AExecutor(task_store=store, model="qwen3.6-plus")
    queue = FakeEventQueue()
    context = FakeRequestContext(metadata={"iac_code": {"cwd": str(tmp_path)}})

    await executor.execute(context, queue)

    dumped = dump(queue.events[-1])
    assert dumped["status"]["state"] == "TASK_STATE_FAILED"
    assert dumped["status"]["message"]["parts"][0]["text"].startswith("FileNotFoundError:")
    assert "A2A executor streaming failed" in caplog.text
    assert "task_id=task-1" in caplog.text
    assert "context_id=ctx-1" in caplog.text
    assert "Traceback (most recent call last)" not in caplog.text
    assert "FileNotFoundError: [Errno 2] No such file or directory" not in caplog.text


@pytest.mark.asyncio
async def test_unexpected_error_surfaces_type_and_message(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    class ExplodingLoop:
        async def run_streaming(self, prompt: str):
            raise RuntimeError("Authorization: Bearer sk-live at /Users/alice/.iac-code/settings.yml")
            yield TextDeltaEvent(text="never")

    runtime = FakeRuntime(agent_loop=ExplodingLoop(), session_id="session-1")
    monkeypatch.setattr("iac_code.a2a.executor.create_agent_runtime", lambda options: runtime)

    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    executor = IacCodeA2AExecutor(task_store=store, model="qwen3.6-plus")
    queue = FakeEventQueue()
    context = FakeRequestContext(metadata={"iac_code": {"cwd": str(tmp_path)}})

    await executor.execute(context, queue)

    dumped = dump(queue.events[-1])
    assert dumped["status"]["state"] == "TASK_STATE_FAILED"
    text = dumped["status"]["message"]["parts"][0]["text"]
    assert text.startswith("RuntimeError:")
    assert "sk-live" in text
    assert "/Users/alice" in text


@pytest.mark.asyncio
async def test_auth_error_still_uses_friendly_hint(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    class AuthFailingLoop:
        async def run_streaming(self, prompt: str):
            raise ValueError("please configure your provider via /auth")
            yield TextDeltaEvent(text="never")

    runtime = FakeRuntime(agent_loop=AuthFailingLoop(), session_id="session-1")
    monkeypatch.setattr("iac_code.a2a.executor.create_agent_runtime", lambda options: runtime)

    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    executor = IacCodeA2AExecutor(task_store=store, model="qwen3.6-plus")
    queue = FakeEventQueue()
    context = FakeRequestContext(metadata={"iac_code": {"cwd": str(tmp_path)}})

    await executor.execute(context, queue)

    dumped = dump(queue.events[-1])
    assert dumped["status"]["state"] == "TASK_STATE_FAILED"
    assert (
        dumped["status"]["message"]["parts"][0]["text"] == "Authentication required. Configure credentials and retry."
    )


@pytest.mark.asyncio
async def test_executor_flushes_telemetry_after_task(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    flush_calls: list[int] = []

    def fake_flush() -> None:
        flush_calls.append(1)

    monkeypatch.setattr("iac_code.services.telemetry.flush_telemetry", fake_flush)

    loop = FakeAgentLoop([TextDeltaEvent(text="hi")])
    runtime = FakeRuntime(agent_loop=loop, session_id="session-flush")
    monkeypatch.setattr("iac_code.a2a.executor.create_agent_runtime", lambda options: runtime)

    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    executor = IacCodeA2AExecutor(task_store=store, model="qwen3.6-plus")
    await executor.execute(
        FakeRequestContext(metadata={"iac_code": {"cwd": str(tmp_path)}}),
        FakeEventQueue(),
    )

    assert flush_calls == [1]


@pytest.mark.asyncio
async def test_executor_flushes_telemetry_even_on_failure(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    flush_calls: list[int] = []

    def fake_flush() -> None:
        flush_calls.append(1)

    monkeypatch.setattr("iac_code.services.telemetry.flush_telemetry", fake_flush)

    class ExplodingLoop:
        async def run_streaming(self, prompt):  # noqa: ARG002
            raise RuntimeError("boom")
            yield  # pragma: no cover - generator marker

    runtime = FakeRuntime(agent_loop=ExplodingLoop(), session_id="session-flush-fail")
    monkeypatch.setattr("iac_code.a2a.executor.create_agent_runtime", lambda options: runtime)

    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    executor = IacCodeA2AExecutor(task_store=store, model="qwen3.6-plus")
    await executor.execute(
        FakeRequestContext(metadata={"iac_code": {"cwd": str(tmp_path)}}),
        FakeEventQueue(),
    )

    assert flush_calls == [1]


@pytest.mark.asyncio
async def test_executor_swallows_flush_errors(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def boom() -> None:
        raise RuntimeError("flush exporter network down")

    monkeypatch.setattr("iac_code.services.telemetry.flush_telemetry", boom)

    loop = FakeAgentLoop([TextDeltaEvent(text="hi")])
    runtime = FakeRuntime(agent_loop=loop, session_id="session-flush-error")
    monkeypatch.setattr("iac_code.a2a.executor.create_agent_runtime", lambda options: runtime)

    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    executor = IacCodeA2AExecutor(task_store=store, model="qwen3.6-plus")

    # Flush failure must not break task completion.
    await executor.execute(
        FakeRequestContext(metadata={"iac_code": {"cwd": str(tmp_path)}}),
        FakeEventQueue(),
    )


class TestResolveUserId:
    def _make_executor(self) -> IacCodeA2AExecutor:
        store = A2ATaskStore(metrics=NoOpA2AMetrics())
        return IacCodeA2AExecutor(task_store=store, model="qwen3.6-plus")

    def test_extracts_user_id_from_iac_code_metadata(self) -> None:
        executor = self._make_executor()
        result = executor._resolve_user_id({"iac_code": {"user_id": "custom-user-123"}})
        assert result == "custom-user-123"

    def test_returns_none_when_no_metadata(self) -> None:
        executor = self._make_executor()
        assert executor._resolve_user_id(None) is None

    def test_returns_none_when_empty_metadata(self) -> None:
        executor = self._make_executor()
        assert executor._resolve_user_id({}) is None

    def test_returns_none_when_no_iac_code_key(self) -> None:
        executor = self._make_executor()
        assert executor._resolve_user_id({"other": "value"}) is None

    def test_returns_none_when_no_user_id_key(self) -> None:
        executor = self._make_executor()
        assert executor._resolve_user_id({"iac_code": {"cwd": "/tmp"}}) is None

    def test_returns_none_for_empty_string(self) -> None:
        executor = self._make_executor()
        assert executor._resolve_user_id({"iac_code": {"user_id": ""}}) is None

    def test_returns_none_for_whitespace_only(self) -> None:
        executor = self._make_executor()
        assert executor._resolve_user_id({"iac_code": {"user_id": "   "}}) is None

    def test_strips_whitespace(self) -> None:
        executor = self._make_executor()
        result = executor._resolve_user_id({"iac_code": {"user_id": "  user-abc  "}})
        assert result == "user-abc"

    def test_passes_through_non_prefixed_value(self) -> None:
        executor = self._make_executor()
        result = executor._resolve_user_id({"iac_code": {"user_id": "raw-value"}})
        assert result == "raw-value"

    def test_returns_none_for_non_string_value(self) -> None:
        executor = self._make_executor()
        assert executor._resolve_user_id({"iac_code": {"user_id": 12345}}) is None


class TestResolveTelemetryChannel:
    def _make_executor(self) -> IacCodeA2AExecutor:
        store = A2ATaskStore(metrics=NoOpA2AMetrics())
        return IacCodeA2AExecutor(task_store=store, model="qwen3.6-plus")

    def test_extracts_and_bounds_channel(self) -> None:
        executor = self._make_executor()

        assert executor._resolve_telemetry_channel({"iac_code": {"channel": "  skill  "}}) == "skill"
        assert executor._resolve_telemetry_channel({"iac_code": {"channel": "x" * 200}}) == "x" * 128

    def test_ignores_missing_blank_or_non_string_channel(self) -> None:
        executor = self._make_executor()

        assert executor._resolve_telemetry_channel({}) is None
        assert executor._resolve_telemetry_channel({"iac_code": {"channel": "   "}}) is None
        assert executor._resolve_telemetry_channel({"iac_code": {"channel": 123}}) is None


class TestResolvePreferredLanguage:
    def _make_executor(self) -> IacCodeA2AExecutor:
        store = A2ATaskStore(metrics=NoOpA2AMetrics())
        return IacCodeA2AExecutor(task_store=store, model="qwen3.6-plus")

    def test_accepts_supported_language_and_normalizes_region(self) -> None:
        executor = self._make_executor()

        assert executor._resolve_preferred_language({"iac_code": {"preferredLanguage": "zh-CN"}}) == "zh"
        assert executor._resolve_preferred_language({"iac_code": {"preferred_language": "JA_jp"}}) == "ja"

    def test_rejects_unknown_or_missing_language(self) -> None:
        executor = self._make_executor()

        assert executor._resolve_preferred_language({"iac_code": {"preferredLanguage": "xx"}}) is None
        assert executor._resolve_preferred_language({}) is None


class TestResolveCandidatePresentation:
    def _make_executor(self) -> IacCodeA2AExecutor:
        store = A2ATaskStore(metrics=NoOpA2AMetrics())
        return IacCodeA2AExecutor(task_store=store, model="qwen3.6-plus")

    def test_accepts_skill_rich_presentation_metadata(self) -> None:
        executor = self._make_executor()

        assert (
            executor._resolve_candidate_presentation({"iac_code": {"candidatePresentation": " rich-v1 "}}) == "rich-v1"
        )
        assert (
            executor._resolve_candidate_presentation({"iac_code": {"candidate_presentation": "RICH-V1"}}) == "rich-v1"
        )

    def test_rejects_unknown_or_missing_presentation(self) -> None:
        executor = self._make_executor()

        assert executor._resolve_candidate_presentation({"iac_code": {"candidatePresentation": "unknown"}}) is None
        assert executor._resolve_candidate_presentation({}) is None


class TestResolveAliyunCredential:
    def _make_executor(self) -> IacCodeA2AExecutor:
        store = A2ATaskStore(metrics=NoOpA2AMetrics())
        return IacCodeA2AExecutor(task_store=store, model="qwen3.6-plus")

    def test_extracts_aliyun_credential_from_iac_code_metadata(self) -> None:
        executor = self._make_executor()

        result = executor._resolve_aliyun_credential(
            {
                "iac_code": {
                    "alibaba_cloud_access_key_id": "client-id",
                    "alibaba_cloud_access_key_secret": "client-secret",
                    "alibaba_cloud_region_id": "cn-beijing",
                    "alibaba_cloud_security_token": "client-sts",
                }
            }
        )

        assert result is not None
        assert result.mode == "StsToken"
        assert result.access_key_id == "client-id"
        assert result.access_key_secret == "client-secret"
        assert result.region_id == "cn-beijing"
        assert result.sts_token == "client-sts"

    def test_uses_default_region_when_metadata_region_is_missing(self) -> None:
        executor = self._make_executor()

        result = executor._resolve_aliyun_credential(
            {
                "iac_code": {
                    "alibaba_cloud_access_key_id": "client-id",
                    "alibaba_cloud_access_key_secret": "client-secret",
                }
            }
        )

        assert result is not None
        assert result.region_id == "cn-hangzhou"
        assert result.mode == "AK"

    def test_returns_none_for_incomplete_aliyun_metadata(self) -> None:
        executor = self._make_executor()

        result = executor._resolve_aliyun_credential(
            {
                "iac_code": {
                    "alibaba_cloud_access_key_id": "client-id",
                    "alibaba_cloud_region_id": "cn-beijing",
                }
            }
        )

        assert result is None


@pytest.mark.asyncio
async def test_executor_applies_user_id_to_telemetry(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from iac_code.services.telemetry.identity import _user_id_override

    captured_user_ids: list[str | None] = []

    original_run_streaming = FakeAgentLoop.run_streaming

    async def capturing_run_streaming(self, prompt):
        captured_user_ids.append(_user_id_override.get())
        async for event in original_run_streaming(self, prompt):
            yield event

    monkeypatch.setattr(FakeAgentLoop, "run_streaming", capturing_run_streaming)

    loop = FakeAgentLoop([TextDeltaEvent(text="ok")])
    runtime = FakeRuntime(agent_loop=loop, session_id="sess-uid")
    monkeypatch.setattr("iac_code.a2a.executor.create_agent_runtime", lambda options: runtime)

    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    executor = IacCodeA2AExecutor(task_store=store, model="qwen3.6-plus")
    queue = FakeEventQueue()
    context = FakeRequestContext(metadata={"iac_code": {"cwd": str(tmp_path), "user_id": "client-user-xyz"}})

    await executor.execute(context, queue)

    assert captured_user_ids == ["client-user-xyz"]


@pytest.mark.asyncio
async def test_executor_applies_metadata_channel_over_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from iac_code.services.telemetry.attributes import AttributeBuilder
    from iac_code.services.telemetry.identity import Identity

    monkeypatch.setenv("IAC_CODE_CHANNEL", "environment")
    attributes = AttributeBuilder(Identity(tmp_path / "settings.yml"), "iac-code", "0.1.0")
    captured_channels: list[str] = []
    original_run_streaming = FakeAgentLoop.run_streaming

    async def capturing_run_streaming(self, prompt):
        captured_channels.append(attributes.build_signal_attributes()["iac_code.channel"])
        async for event in original_run_streaming(self, prompt):
            yield event

    monkeypatch.setattr(FakeAgentLoop, "run_streaming", capturing_run_streaming)
    runtime = FakeRuntime(agent_loop=FakeAgentLoop([TextDeltaEvent(text="ok")]), session_id="sess-channel")
    monkeypatch.setattr("iac_code.a2a.executor.create_agent_runtime", lambda options: runtime)

    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    executor = IacCodeA2AExecutor(task_store=store, model="qwen3.6-plus")
    first_context = FakeRequestContext(
        task_id="task-channel-1",
        context_id="ctx-channel",
        metadata={"iac_code": {"cwd": str(tmp_path), "channel": "a2a-request"}},
    )
    followup_context = FakeRequestContext(
        task_id="task-channel-2",
        context_id="ctx-channel",
        metadata={"iac_code": {"cwd": str(tmp_path)}},
    )

    await executor.execute(first_context, FakeEventQueue())
    await executor.execute(followup_context, FakeEventQueue())

    assert captured_channels == ["a2a-request", "a2a-request"]
    assert attributes.build_signal_attributes()["iac_code.channel"] == "environment"


@pytest.mark.asyncio
async def test_executor_no_user_id_override_when_not_specified(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from iac_code.services.telemetry.identity import _user_id_override

    captured_user_ids: list[str | None] = []

    original_run_streaming = FakeAgentLoop.run_streaming

    async def capturing_run_streaming(self, prompt):
        captured_user_ids.append(_user_id_override.get())
        async for event in original_run_streaming(self, prompt):
            yield event

    monkeypatch.setattr(FakeAgentLoop, "run_streaming", capturing_run_streaming)

    loop = FakeAgentLoop([TextDeltaEvent(text="ok")])
    runtime = FakeRuntime(agent_loop=loop, session_id="sess-no-uid")
    monkeypatch.setattr("iac_code.a2a.executor.create_agent_runtime", lambda options: runtime)

    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    executor = IacCodeA2AExecutor(task_store=store, model="qwen3.6-plus")
    queue = FakeEventQueue()
    context = FakeRequestContext(metadata={"iac_code": {"cwd": str(tmp_path)}})

    await executor.execute(context, queue)

    assert captured_user_ids == [None]


@pytest.mark.asyncio
async def test_executor_uses_metadata_iac_code_model_when_creating_runtime(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    seen_models: list[str] = []

    def factory(options):
        seen_models.append(options.model)
        return FakeRuntime(
            agent_loop=FakeAgentLoop([TextDeltaEvent(text="ok")]),
            session_id=options.session_id,
        )

    monkeypatch.setattr("iac_code.a2a.executor.create_agent_runtime", factory)

    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    executor = IacCodeA2AExecutor(task_store=store, model="server-default-model")
    context = FakeRequestContext(metadata={"iac_code": {"cwd": str(tmp_path), "iac_code_model": "metadata-model"}})

    await executor.execute(context, FakeEventQueue())

    assert seen_models == ["metadata-model"]


@pytest.mark.asyncio
async def test_executor_reconfigures_cached_runtime_iac_code_model_per_call(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class FakeProviderManager:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def reconfigure(self, model, credentials, provider_key_override=None, base_url_override=None):
            self.calls.append(model)

    provider_manager = FakeProviderManager()
    runtime = FakeRuntime(
        agent_loop=FakeAgentLoop([TextDeltaEvent(text="ok")]),
        session_id="session-1",
        provider_manager=provider_manager,
    )
    monkeypatch.setattr("iac_code.a2a.executor.create_agent_runtime", lambda options: runtime)

    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    executor = IacCodeA2AExecutor(task_store=store, model="server-default-model")

    await executor.execute(
        FakeRequestContext(
            context_id="ctx-1",
            metadata={"iac_code": {"cwd": str(tmp_path), "iac_code_model": "metadata-model"}},
        ),
        FakeEventQueue(),
    )
    await executor.execute(
        FakeRequestContext(context_id="ctx-1", task_id="task-2", metadata={"iac_code": {"cwd": str(tmp_path)}}),
        FakeEventQueue(),
    )

    assert provider_manager.calls == ["metadata-model", "server-default-model"]


@pytest.mark.asyncio
async def test_executor_reconfigures_cached_runtime_iac_code_api_key_per_call(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class FakeProviderManager:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, str]]] = []

        def reconfigure(self, model, credentials, provider_key_override=None, base_url_override=None):
            self.calls.append((model, dict(credentials)))

    provider_manager = FakeProviderManager()
    runtime = FakeRuntime(
        agent_loop=FakeAgentLoop([TextDeltaEvent(text="ok")]),
        session_id="session-1",
        provider_manager=provider_manager,
    )
    monkeypatch.setattr("iac_code.a2a.executor.create_agent_runtime", lambda options: runtime)
    monkeypatch.setattr("iac_code.config.load_credentials", lambda model=None: {"dashscope": "fallback-key"})

    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    executor = IacCodeA2AExecutor(task_store=store, model="qwen3.6-plus")

    await executor.execute(
        FakeRequestContext(
            context_id="ctx-1",
            metadata={"iac_code": {"cwd": str(tmp_path), "iac_code_api_key": "metadata-key"}},
        ),
        FakeEventQueue(),
    )
    await executor.execute(
        FakeRequestContext(context_id="ctx-1", task_id="task-2", metadata={"iac_code": {"cwd": str(tmp_path)}}),
        FakeEventQueue(),
    )

    assert provider_manager.calls == [
        ("qwen3.6-plus", {"dashscope": "metadata-key"}),
        ("qwen3.6-plus", {"dashscope": "fallback-key"}),
    ]


@pytest.mark.asyncio
async def test_executor_reconfigures_cached_runtime_thinking_per_call(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class FakeProviderManager:
        def __init__(self) -> None:
            self.calls: list[tuple[str, object | None]] = []

        def reconfigure(
            self,
            model,
            credentials,
            provider_key_override=None,
            base_url_override=None,
            request_policy_override=None,
        ):
            self.calls.append((model, request_policy_override))

    provider_manager = FakeProviderManager()
    runtime = FakeRuntime(
        agent_loop=FakeAgentLoop([TextDeltaEvent(text="ok")]),
        session_id="session-1",
        provider_manager=provider_manager,
    )
    monkeypatch.setattr("iac_code.a2a.executor.create_agent_runtime", lambda options: runtime)
    monkeypatch.setattr("iac_code.config.load_credentials", lambda model=None: {})

    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    executor = IacCodeA2AExecutor(task_store=store, model="qwen3.6-plus")

    await executor.execute(
        FakeRequestContext(
            context_id="ctx-1",
            metadata={
                "iac_code": {
                    "cwd": str(tmp_path),
                    "thinking": {"enabled": False, "effort": "high", "budget": 2048},
                }
            },
        ),
        FakeEventQueue(),
    )
    await executor.execute(
        FakeRequestContext(context_id="ctx-1", task_id="task-2", metadata={"iac_code": {"cwd": str(tmp_path)}}),
        FakeEventQueue(),
    )

    first_policy = provider_manager.calls[0][1]
    assert provider_manager.calls[0][0] == "qwen3.6-plus"
    assert getattr(first_policy, "thinking_enabled", None) is False
    assert getattr(first_policy, "effort", None) == "high"
    assert getattr(first_policy, "thinking_budget", None) == 2048
    assert provider_manager.calls[1] == ("qwen3.6-plus", None)


@pytest.mark.asyncio
async def test_executor_applies_aliyun_metadata_to_task_credentials(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from iac_code.services.providers.aliyun import AliyunCredentials

    captured_access_key_ids: list[str | None] = []

    original_run_streaming = FakeAgentLoop.run_streaming

    async def capturing_run_streaming(self, prompt):
        cred = AliyunCredentials.load()
        captured_access_key_ids.append(cred.access_key_id if cred else None)
        async for event in original_run_streaming(self, prompt):
            yield event

    monkeypatch.setattr(FakeAgentLoop, "run_streaming", capturing_run_streaming)

    env = {
        "ALIBABA_CLOUD_ACCESS_KEY_ID": "env-id",
        "ALIBABA_CLOUD_ACCESS_KEY_SECRET": "env-secret",
        "ALIBABA_CLOUD_REGION_ID": "cn-shanghai",
    }
    loop = FakeAgentLoop([TextDeltaEvent(text="ok")])
    runtime = FakeRuntime(agent_loop=loop, session_id="sess-aliyun")
    monkeypatch.setattr("iac_code.a2a.executor.create_agent_runtime", lambda options: runtime)

    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    executor = IacCodeA2AExecutor(task_store=store, model="qwen3.6-plus")
    context = FakeRequestContext(
        metadata={
            "iac_code": {
                "cwd": str(tmp_path),
                "alibaba_cloud_access_key_id": "client-id",
                "alibaba_cloud_access_key_secret": "client-secret",
                "alibaba_cloud_region_id": "cn-beijing",
            }
        }
    )

    monkeypatch.setattr("iac_code.services.providers.aliyun.AliyunCredentials._load_from_iac_code_config", lambda: None)
    with monkeypatch.context() as m:
        for key, value in env.items():
            m.setenv(key, value)
        await executor.execute(context, FakeEventQueue())
        after = AliyunCredentials.load()

    assert captured_access_key_ids == ["client-id"]
    assert after is not None
    assert after.access_key_id == "env-id"


@pytest.mark.asyncio
async def test_executor_applies_aliyun_metadata_while_creating_runtime(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from iac_code.services.providers.aliyun import AliyunCredentials

    captured_access_key_ids: list[str | None] = []

    def factory(options):
        cred = AliyunCredentials.load()
        captured_access_key_ids.append(cred.access_key_id if cred else None)
        return FakeRuntime(
            agent_loop=FakeAgentLoop([TextDeltaEvent(text="ok")]),
            session_id=options.session_id,
        )

    monkeypatch.setattr("iac_code.a2a.executor.create_agent_runtime", factory)
    monkeypatch.setattr("iac_code.services.providers.aliyun.AliyunCredentials._load_from_iac_code_config", lambda: None)

    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    executor = IacCodeA2AExecutor(task_store=store, model="qwen3.6-plus")
    context = FakeRequestContext(
        metadata={
            "iac_code": {
                "cwd": str(tmp_path),
                "alibaba_cloud_access_key_id": "client-id",
                "alibaba_cloud_access_key_secret": "client-secret",
                "alibaba_cloud_region_id": "cn-beijing",
            }
        }
    )

    await executor.execute(context, FakeEventQueue())

    assert captured_access_key_ids == ["client-id"]


@pytest.mark.asyncio
async def test_executor_refreshes_cloud_tools_with_aliyun_metadata_for_reused_context(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    seen_access_key_ids: list[str | None] = []
    runtime = FakeRuntime(
        agent_loop=FakeAgentLoop([TextDeltaEvent(text="ok")]),
        session_id="session-1",
        tool_registry=object(),
        aliyun_services=object(),
    )

    def fake_register_cloud_tools(registry, credentials, services):
        assert registry is runtime.tool_registry
        assert services is runtime.aliyun_services
        credential = credentials.get_provider("aliyun")
        seen_access_key_ids.append(credential.access_key_id if credential else None)

    monkeypatch.setattr("iac_code.tools.cloud.registry.register_cloud_tools", fake_register_cloud_tools)
    monkeypatch.setattr("iac_code.a2a.executor.create_agent_runtime", lambda options: runtime)
    monkeypatch.setattr("iac_code.services.providers.aliyun.AliyunCredentials._load_from_iac_code_config", lambda: None)

    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    await store.get_or_create_context(
        context_id="ctx-1",
        cwd=str(tmp_path.resolve()),
        runtime_factory=lambda session_id: runtime,
    )
    executor = IacCodeA2AExecutor(task_store=store, model="qwen3.6-plus")
    context = FakeRequestContext(
        context_id="ctx-1",
        metadata={
            "iac_code": {
                "cwd": str(tmp_path),
                "alibaba_cloud_access_key_id": "client-id",
                "alibaba_cloud_access_key_secret": "client-secret",
                "alibaba_cloud_region_id": "cn-beijing",
            }
        },
    )

    await executor.execute(context, FakeEventQueue())

    assert seen_access_key_ids == ["client-id"]


@pytest.mark.asyncio
async def test_executor_installs_permission_reply_identity_before_live_answer(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from iac_code.services.providers.aliyun import AliyunCredentials

    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    executor = IacCodeA2AExecutor(task_store=store, model="qwen3.6-plus")
    response = PermissionResponse(
        task_id="task-1",
        context_id="ctx-1",
        request_task_id="task-1",
        input_id="pwi-test",
        tool_use_id="tool-1",
        decision="allow_once",
    )
    pending = SimpleNamespace(state="pending", boundary_id=None)
    observed: list[tuple[str, str | None]] = []

    async def pending_for_response(_response):
        return pending

    async def answer(_response, *, execution_context=None):
        assert execution_context is not None
        with execution_context.install():
            credential = AliyunCredentials.load()
            observed.append((get_user_id(), credential.access_key_id if credential is not None else None))
        return True

    async def claim_continuation(_pending):
        return None

    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setattr("iac_code.a2a.executor.parse_permission_response", lambda _message: response)
    monkeypatch.setattr(executor._permission_input_registry, "pending_for_response", pending_for_response)
    monkeypatch.setattr(executor._permission_input_registry, "answer", answer)
    monkeypatch.setattr(executor._permission_input_registry, "claim_continuation", claim_continuation)

    await executor.execute(
        FakeRequestContext(
            task_id="task-1",
            context_id="ctx-1",
            metadata={
                "iac_code": {
                    "user_id": "stable-a2a-user",
                    "alibaba_cloud_access_key_id": "rotated-sts-ak",
                    "alibaba_cloud_access_key_secret": "rotated-sts-secret",
                    "alibaba_cloud_security_token": "rotated-sts-token",
                    "alibaba_cloud_region_id": "cn-beijing",
                }
            },
        ),
        FakeEventQueue(),
    )

    assert observed == [("stable-a2a-user", "rotated-sts-ak")]


@pytest.mark.asyncio
async def test_suspending_permission_answer_waits_for_owner_then_resumes_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    executor = IacCodeA2AExecutor(task_store=store, model="qwen3.6-plus")
    response = PermissionResponse(
        task_id="task-1",
        context_id="ctx-1",
        request_task_id="task-1",
        input_id="pwi-test",
        tool_use_id="tool-1",
        decision="allow_once",
    )
    pending = SimpleNamespace(state="suspended_decision_claimed", boundary_id="pwb-test")
    waits = [False, False, True]
    completed: list[object] = []
    resumed: list[PermissionResponse] = []
    published: list[dict] = []

    async def pending_for_response(_response):
        return pending

    async def answer(_response):
        return True

    async def wait_for_suspended_owner(_boundary_id):
        return waits.pop(0)

    async def complete(value):
        completed.append(value)

    async def resume(_context, _queue, *, response):
        resumed.append(response)
        return True

    async def publish(_queue, **kwargs):
        published.append(kwargs)

    monkeypatch.setattr("iac_code.a2a.executor.parse_permission_response", lambda _message: response)
    monkeypatch.setattr(executor._permission_input_registry, "pending_for_response", pending_for_response)
    monkeypatch.setattr(executor._permission_input_registry, "answer", answer)
    monkeypatch.setattr(
        executor._permission_wait_coordinator,
        "wait_for_suspended_owner",
        wait_for_suspended_owner,
    )
    monkeypatch.setattr(executor._permission_input_registry, "complete", complete)
    monkeypatch.setattr(executor, "_resume_persisted_permission", resume)
    monkeypatch.setattr(executor, "_publish_status", publish)

    await executor._execute(
        FakeRequestContext(task_id="task-1", context_id="ctx-1"),
        FakeEventQueue(),
        context_id="ctx-1",
    )

    assert waits == []
    assert completed == [pending]
    assert resumed == [response]
    assert len(published) == 1
    assert published[0]["metadata"]["iac_code"]["permissionAck"]["recoveryPending"] is True


@pytest.mark.asyncio
async def test_normal_persisted_permission_recovery_publishes_final_and_terminal_state(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from iac_code.a2a.runtime_overrides import a2a_request_context
    from iac_code.services.permission_wait import permission_execution_identity
    from iac_code.services.providers.aliyun import AliyunCredential, AliyunCredentials

    recovered_access_key_ids: list[str | None] = []
    reply_credential = AliyunCredential(
        mode="StsToken",
        access_key_id="rotated-sts-ak",
        access_key_secret="rotated-sts-secret",
        sts_token="rotated-sts-token",
        region_id="cn-beijing",
    )
    with a2a_request_context(
        user_id="stable-a2a-user",
        aliyun_credential=reply_credential,
    ):
        principal_ref, region = permission_execution_identity(
            tool_name="aliyun_api",
            tool_input={"product": "ros", "action": "CreateStack", "region_id": "cn-beijing"},
        )

    class RecoveryLoop:
        async def resume_permission_boundary(self, _checkpoint):
            credential = AliyunCredentials.load()
            recovered_access_key_ids.append(credential.access_key_id if credential is not None else None)
            yield MessageStartEvent(message_id="final")
            yield TextDeltaEvent(text="Cleanup completed.")
            yield MessageEndEvent(stop_reason="end_turn", usage=Usage())

    checkpoint = {
        "boundaryId": "pwb-boundary1",
        "phase": "SUSPENDED",
        "permissionClass": "normal",
        "principalRef": principal_ref,
        "principalKind": "a2a_user",
        "region": region,
        "decision": {
            "status": "claimed",
            "value": "allow_once",
            "claimId": "claim-1",
            "auditStatus": "recorded",
            "backupStatus": "committed",
        },
    }
    resolved: list[dict] = []

    class CheckpointStore:
        def find(self, **_kwargs):
            return checkpoint

        def reconcile_deadline(self, *_args, **_kwargs):
            return checkpoint

        def claim_decision(self, *_args, **_kwargs):
            return checkpoint, False

        def run_claim_audit_once(self, *_args, **_kwargs):
            return checkpoint, False

        def begin_restore(self, _boundary_id):
            checkpoint["phase"] = "RESTORING"
            return checkpoint

        def resolve(self, _boundary_id, **kwargs):
            checkpoint["phase"] = "RESOLVED"
            resolved.append(kwargs)
            return checkpoint

    backup_service = SnapshotReadingBackupService()
    task_store = A2ATaskStore(metrics=NoOpA2AMetrics())
    context_record = await task_store.get_or_create_context(
        context_id="ctx-1",
        cwd=str(tmp_path),
        runtime_factory=lambda session_id: FakeRuntime(session_id=session_id),
    )
    SessionStorage().append(
        str(tmp_path),
        context_record.session_id,
        Message(role="user", content="delete the stack"),
    )
    task_record = await task_store.get_or_create_task(task_id="task-1", context_id="ctx-1")
    task_record.state = "input-required"
    task_store.mirror_task(task_record)
    runtime = FakeRuntime(agent_loop=RecoveryLoop(), session_id=context_record.session_id)
    monkeypatch.setattr("iac_code.a2a.executor.PermissionWaitCheckpointStore", lambda *_args: CheckpointStore())
    monkeypatch.setattr(
        "iac_code.a2a.executor.recover_permission_audit_boundary",
        lambda *_args, **_kwargs: RecoveredPermissionAuditBoundary(
            tool_name="aliyun_api",
            tool_input={"product": "ros", "action": "CreateStack", "region_id": "cn-beijing"},
            tool_use_id="tool-1",
            audit_context={"session_id": context_record.session_id, "cwd": str(tmp_path)},
        ),
    )
    monkeypatch.setattr("iac_code.a2a.executor.create_agent_runtime", lambda _options: runtime)
    monkeypatch.setattr(
        "iac_code.services.providers.aliyun.AliyunCredentials._load_from_iac_code_config",
        lambda: None,
    )

    executor = IacCodeA2AExecutor(
        task_store=task_store,
        model="qwen3.6-plus",
        backup_service=backup_service,
    )
    response = PermissionResponse(
        task_id="task-1",
        context_id="ctx-1",
        request_task_id="task-1",
        input_id="input-1",
        tool_use_id="tool-1",
        decision="allow_once",
    )
    queue = FakeEventQueue()

    assert await executor._resume_persisted_permission(
        FakeRequestContext(
            task_id="task-1",
            context_id="ctx-1",
            metadata={
                "iac_code": {
                    "user_id": "stable-a2a-user",
                    "alibaba_cloud_access_key_id": "rotated-sts-ak",
                    "alibaba_cloud_access_key_secret": "rotated-sts-secret",
                    "alibaba_cloud_security_token": "rotated-sts-token",
                    "alibaba_cloud_region_id": "cn-beijing",
                }
            },
        ),
        queue,
        response=response,
    )

    states = [dump(event)["status"]["state"] for event in queue.events if isinstance(event, TaskStatusUpdateEvent)]
    final_events = [
        dump(event)
        for event in queue.events
        if isinstance(event, TaskStatusUpdateEvent)
        and dump(event).get("metadata", {}).get("iac_code", {}).get("assistantFinal", {}).get("complete") is True
    ]
    assert states[-1] == "TASK_STATE_INPUT_REQUIRED"
    assert final_events[0]["status"]["message"]["parts"][0]["text"] == "Cleanup completed."
    assert "".join(task_record.output_text) == "Cleanup completed."
    assert task_record.state == "input-required"
    assert checkpoint["phase"] == "RESOLVED"
    assert len(resolved) == 1
    assert backup_service.calls == [(str(tmp_path), context_record.session_id, BackupReason.NORMAL_TURN_END, False)]
    assert recovered_access_key_ids == ["rotated-sts-ak"]


@pytest.mark.asyncio
async def test_restart_audit_rebuild_failure_precedes_permission_claim_and_backup(monkeypatch, tmp_path) -> None:
    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    executor = IacCodeA2AExecutor(task_store=store, model="qwen3.6-plus")
    task_record = SimpleNamespace(context_id="ctx-1")
    context_record = SimpleNamespace(cwd=str(tmp_path), session_id="session-1")

    async def get_task_record(_task_id):
        return task_record

    async def get_context_record(_context_id):
        return context_record

    monkeypatch.setattr(store, "get_task_record", get_task_record)
    monkeypatch.setattr(store, "get_context_record", get_context_record)
    checkpoint = {
        "boundaryId": "pwb-boundary1",
        "phase": "SUSPENDED",
        "permissionClass": "normal",
        "decision": {"status": "none", "value": None},
        "principalRef": None,
        "region": None,
    }
    store_calls: list[str] = []

    class CheckpointStore:
        def find(self, **_kwargs):
            return checkpoint

        def reconcile_deadline(self, *_args, **_kwargs):
            store_calls.append("reconcile")
            return checkpoint

        def claim_decision(self, *_args, **_kwargs):
            store_calls.append("claim")
            return checkpoint, True

    monkeypatch.setattr(
        "iac_code.a2a.executor.PermissionWaitCheckpointStore",
        lambda *_args, **_kwargs: CheckpointStore(),
    )
    monkeypatch.setattr(
        "iac_code.a2a.executor.recover_permission_audit_boundary",
        lambda *_args, **_kwargs: RecoveredPermissionAuditBoundary(
            tool_name="write_file",
            tool_input={"path": "template.yml"},
            tool_use_id="tool-1",
            audit_context={"session_id": "session-1", "cwd": str(tmp_path)},
        ),
    )

    async def fail_rebuild(**_kwargs):
        raise ValueError("current tool unavailable")

    monkeypatch.setattr(executor, "_rebuild_normal_permission_audit_event", fail_rebuild)
    response = PermissionResponse(
        task_id="task-1",
        context_id="ctx-1",
        request_task_id="task-1",
        input_id="input-1",
        tool_use_id="tool-1",
        decision="allow_once",
    )

    with pytest.raises(InvalidParamsError, match="permission_resume_invalid"):
        await executor._resume_persisted_permission(
            FakeRequestContext(task_id="task-1", context_id="ctx-1"),
            FakeEventQueue(),
            response=response,
        )

    assert store_calls == []


@pytest.mark.asyncio
async def test_restart_identity_validation_uses_resume_request_identity(monkeypatch, tmp_path) -> None:
    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    executor = IacCodeA2AExecutor(task_store=store, model="qwen3.6-plus")
    task_record = SimpleNamespace(context_id="ctx-1")
    context_record = SimpleNamespace(cwd=str(tmp_path), session_id="session-1")

    async def get_task_record(_task_id):
        return task_record

    async def get_context_record(_context_id):
        return context_record

    monkeypatch.setattr(store, "get_task_record", get_task_record)
    monkeypatch.setattr(store, "get_context_record", get_context_record)
    checkpoint = {
        "boundaryId": "pwb-boundary1",
        "phase": "SUSPENDED",
        "permissionClass": "normal",
        "decision": {"status": "none", "value": None},
        "principalRef": "client-principal",
        "region": "cn-beijing",
    }

    class CheckpointStore:
        def find(self, **_kwargs):
            return checkpoint

        def reconcile_deadline(self, *_args, **_kwargs):
            raise RuntimeError("identity validation completed")

    monkeypatch.setattr(
        "iac_code.a2a.executor.PermissionWaitCheckpointStore",
        lambda *_args, **_kwargs: CheckpointStore(),
    )
    monkeypatch.setattr(
        "iac_code.a2a.executor.recover_permission_audit_boundary",
        lambda *_args, **_kwargs: RecoveredPermissionAuditBoundary(
            tool_name="aliyun_api",
            tool_input={"region_id": "cn-beijing"},
            tool_use_id="tool-1",
            audit_context={"session_id": "session-1", "cwd": str(tmp_path)},
        ),
    )

    async def rebuild(**_kwargs):
        return SimpleNamespace(
            tool_name="aliyun_api",
            tool_input={"region_id": "cn-beijing"},
            permission_result=SimpleNamespace(audit=None),
        )

    seen_request_identities: list[tuple[str, str | None]] = []

    def identity(**kwargs):
        from iac_code.services.providers.aliyun import AliyunCredentials

        credential = AliyunCredentials.load()
        seen_request_identities.append(
            (
                get_user_id(),
                credential.access_key_id if credential is not None else None,
            )
        )
        return PermissionExecutionIdentity(
            "client-principal",
            "cn-beijing",
            kwargs.get("principal_kind"),
        )

    monkeypatch.setattr(executor, "_rebuild_normal_permission_audit_event", rebuild)
    monkeypatch.setattr(PermissionExecutionIdentity, "resolve", staticmethod(identity))
    monkeypatch.setattr(
        "iac_code.services.providers.aliyun.AliyunCredentials._load_from_iac_code_config",
        lambda: None,
    )
    response = PermissionResponse(
        task_id="task-1",
        context_id="ctx-1",
        request_task_id="task-1",
        input_id="input-1",
        tool_use_id="tool-1",
        decision="deny",
    )
    context = FakeRequestContext(
        task_id="task-1",
        context_id="ctx-1",
        metadata={
            "iac_code": {
                "user_id": "stable-a2a-user",
                "alibaba_cloud_access_key_id": "client-id",
                "alibaba_cloud_access_key_secret": "client-secret",
                "alibaba_cloud_region_id": "cn-beijing",
            }
        },
    )

    with pytest.raises(RuntimeError, match="identity validation completed"):
        await executor._resume_persisted_permission(context, FakeEventQueue(), response=response)

    assert seen_request_identities == [
        ("stable-a2a-user", "client-id"),
        ("stable-a2a-user", "client-id"),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("reply_user_id", "reply_region"),
    [
        ("different-a2a-user", "cn-hangzhou"),
        ("stable-a2a-user", "cn-shanghai"),
    ],
)
async def test_claimed_restart_revalidates_principal_and_region_before_runtime(
    monkeypatch,
    tmp_path,
    reply_user_id,
    reply_region,
) -> None:
    from iac_code.a2a.runtime_overrides import a2a_request_context
    from iac_code.services.permission_wait import permission_execution_identity
    from iac_code.services.providers.aliyun import AliyunCredential

    original_credential = AliyunCredential(
        mode="StsToken",
        access_key_id="original-sts-ak",
        access_key_secret="original-sts-secret",
        sts_token="original-sts-token",
        region_id="cn-hangzhou",
    )
    with a2a_request_context(
        user_id="stable-a2a-user",
        aliyun_credential=original_credential,
    ):
        principal_ref, region = permission_execution_identity(
            tool_name="aliyun_api",
            tool_input={"product": "ros", "action": "CreateStack"},
        )

    checkpoint = {
        "boundaryId": "pwb-boundary1",
        "phase": "SUSPENDED",
        "permissionClass": "normal",
        "decision": {
            "status": "claimed",
            "value": "allow_once",
            "claimId": "claim-1",
            "auditStatus": "recorded",
            "backupStatus": "committed",
        },
        "principalRef": principal_ref,
        "principalKind": "a2a_user",
        "region": region,
    }

    class CheckpointStore:
        def find(self, **_kwargs):
            return checkpoint

        def reconcile_deadline(self, *_args, **_kwargs):
            pytest.fail("mismatched identity must be rejected before recovery")

    task_store = A2ATaskStore(metrics=NoOpA2AMetrics())

    async def get_task_record(_task_id):
        return SimpleNamespace(context_id="ctx-1")

    async def get_context_record(_context_id):
        return SimpleNamespace(cwd=str(tmp_path), session_id="session-1")

    monkeypatch.setattr(task_store, "get_task_record", get_task_record)
    monkeypatch.setattr(task_store, "get_context_record", get_context_record)
    monkeypatch.setattr(
        "iac_code.a2a.executor.PermissionWaitCheckpointStore",
        lambda *_args, **_kwargs: CheckpointStore(),
    )
    monkeypatch.setattr(
        "iac_code.a2a.executor.recover_permission_audit_boundary",
        lambda *_args, **_kwargs: RecoveredPermissionAuditBoundary(
            tool_name="aliyun_api",
            tool_input={"product": "ros", "action": "CreateStack"},
            tool_use_id="tool-1",
            audit_context={"session_id": "session-1", "cwd": str(tmp_path)},
        ),
    )
    monkeypatch.setattr(
        "iac_code.a2a.executor.create_agent_runtime",
        lambda *_args, **_kwargs: pytest.fail("mismatched identity must not create a runtime"),
    )
    executor = IacCodeA2AExecutor(task_store=task_store, model="qwen3.6-plus")
    response = PermissionResponse(
        task_id="task-1",
        context_id="ctx-1",
        request_task_id="task-1",
        input_id="input-1",
        tool_use_id="tool-1",
        decision="allow_once",
    )
    context = FakeRequestContext(
        task_id="task-1",
        context_id="ctx-1",
        metadata={
            "iac_code": {
                "user_id": reply_user_id,
                "alibaba_cloud_access_key_id": "reply-sts-ak",
                "alibaba_cloud_access_key_secret": "reply-sts-secret",
                "alibaba_cloud_security_token": "reply-sts-token",
                "alibaba_cloud_region_id": reply_region,
            }
        },
    )

    with pytest.raises(InvalidParamsError, match="cloud execution identity changed"):
        await executor._resume_persisted_permission(context, FakeEventQueue(), response=response)
