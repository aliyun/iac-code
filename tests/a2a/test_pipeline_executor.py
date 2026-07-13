from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from a2a.types import TaskStatusUpdateEvent
from google.protobuf.json_format import MessageToDict

from iac_code.a2a.artifacts import A2AArtifactStore
from iac_code.a2a.executor import IacCodeA2AExecutor
from iac_code.a2a.metrics import NoOpA2AMetrics
from iac_code.a2a.persistence import A2APersistenceStore
from iac_code.a2a.pipeline_journal import A2APipelineJournal
from iac_code.a2a.pipeline_snapshot import A2APipelineSnapshotStore, reduce_pipeline_events
from iac_code.a2a.task_store import A2ATaskStore
from iac_code.agent.message import ImageBlock
from iac_code.pipeline.engine.cleanup import CleanupLedger, CleanupResource
from iac_code.pipeline.engine.events import PipelineEvent, PipelineEventType
from iac_code.pipeline.engine.interrupt import InterruptVerdict
from iac_code.pipeline.engine.prerequisites import PrerequisiteDecision, PrerequisiteResolution
from iac_code.pipeline.engine.user_input import PipelineUserInput, normalize_pipeline_user_input
from iac_code.services.session_backup import BackupReason, BackupResult, SessionBackupBlocked
from iac_code.services.session_layout import UnsupportedSessionLayoutError
from iac_code.services.session_metadata import SESSION_LAYOUT_VERSION_V2, SessionMetadata, write_session_metadata
from iac_code.services.session_storage import SessionStorage
from iac_code.types.stream_events import AskUserQuestionEvent, TextDeltaEvent

from .fakes import FakeEventQueue, FakeRequestContext

RETRY_TEXT = "A temporary error occurred. Please retry."
AUTH_TEXT = "Authentication required. Configure credentials and retry."
_A2A_ASYNC_TEST_TIMEOUT = 5


def _write_pipeline_yaml(pipeline_dir: Path) -> None:
    pipeline_dir.mkdir(parents=True)
    (pipeline_dir / "pipeline.yaml").write_text(
        json.dumps(
            {
                "name": "test-pipeline",
                "feature_flags": {"reviewing": {"default": True}},
                "prerequisites": {
                    "infraguard": {
                        "command": "infraguard",
                        "required_by_flags": ["reviewing"],
                        "on_missing": {"non_interactive": "disable_feature"},
                    }
                },
            }
        ),
        encoding="utf-8",
    )


def _pipeline_executor():
    from iac_code.a2a.pipeline_executor import IacCodeA2APipelineExecutor

    return IacCodeA2APipelineExecutor(
        task_store=MagicMock(),
        model="qwen3.6-plus",
        metrics=NoOpA2AMetrics(),
        artifact_store=None,
        push_notifier=None,
        permission_resolver=None,
        auto_approve_permissions=False,
        thinking_exposure_types=None,
    )


def test_active_sidecar_mismatch_error_exposes_jsonrpc_data() -> None:
    from iac_code.a2a.pipeline_executor import _active_sidecar_mismatch_error

    error = _active_sidecar_mismatch_error(
        recoverable_task_id="task-owner",
        context_id="ctx-1",
        sidecar_status="running",
    )

    assert error.code == -32602
    assert error.data == {
        "recoverableTaskId": "task-owner",
        "contextId": "ctx-1",
        "sidecarStatus": "running",
    }
    assert "task-owner" in error.message


def test_create_pipeline_inspects_prerequisites_for_fresh_a2a_run(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from iac_code.a2a import pipeline_executor as pipeline_executor_module

    pipeline_dir = tmp_path / "pipeline-def"
    _write_pipeline_yaml(pipeline_dir)
    resolution = PrerequisiteResolution(
        feature_flags={"reviewing": False},
        decisions={
            "infraguard": PrerequisiteDecision(
                name="infraguard",
                command="infraguard",
                status="disabled_feature",
                required_flags=["reviewing"],
            )
        },
    )
    inspect_calls = []
    create_kwargs = {}

    def fake_inspect(raw_prerequisites, *, feature_flags):
        inspect_calls.append({"raw_prerequisites": raw_prerequisites, "feature_flags": feature_flags})
        return resolution

    def fake_create_pipeline(*args, **kwargs):
        create_kwargs.update(kwargs)
        return object()

    monkeypatch.setattr(pipeline_executor_module, "discover_pipelines", lambda: {"test-pipeline": pipeline_dir})
    monkeypatch.setattr(pipeline_executor_module, "get_pipeline_name", lambda: "test-pipeline")
    monkeypatch.setattr(pipeline_executor_module, "inspect_prerequisites", fake_inspect, raising=False)
    monkeypatch.setattr(pipeline_executor_module, "create_pipeline", fake_create_pipeline)

    _pipeline_executor()._create_pipeline(
        session_id="session-1",
        cwd=str(tmp_path),
        runtime=_fake_runtime(),
        session_storage=MagicMock(),
        resume_from_sidecar=False,
    )

    assert inspect_calls == [
        {
            "raw_prerequisites": {
                "infraguard": {
                    "command": "infraguard",
                    "required_by_flags": ["reviewing"],
                    "on_missing": {"non_interactive": "disable_feature"},
                }
            },
            "feature_flags": {"reviewing": True},
        }
    ]
    assert create_kwargs["surface"] == "a2a"
    assert create_kwargs["prerequisite_resolution"] == resolution.to_metadata()


def test_create_pipeline_resume_sidecar_prerequisites_skip_a2a_inspection(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from iac_code.a2a import pipeline_executor as pipeline_executor_module

    pipeline_dir = tmp_path / "pipeline-def"
    _write_pipeline_yaml(pipeline_dir)
    session_root = tmp_path / "session-1"
    sidecar = session_root / "pipeline"
    sidecar.mkdir(parents=True)
    stored_prerequisites = {
        "feature_flags": {"reviewing": False},
        "decisions": {},
        "env_overrides": {"PATH": "/tmp/iac-code-infraguard/bin"},
    }
    (sidecar / "meta.yaml").write_text(
        json.dumps(
            {
                "status": "running",
                "updated_at": 0.0,
                "prerequisites": stored_prerequisites,
            }
        ),
        encoding="utf-8",
    )
    create_kwargs = {}
    session_storage = MagicMock()
    session_storage.session_dir.return_value = session_root

    def fail_inspect(*args, **kwargs):
        raise AssertionError("resume with stored prerequisites must not inspect again")

    def fake_create_pipeline(*args, **kwargs):
        create_kwargs.update(kwargs)
        return object()

    monkeypatch.setattr(pipeline_executor_module, "discover_pipelines", lambda: {"test-pipeline": pipeline_dir})
    monkeypatch.setattr(pipeline_executor_module, "get_pipeline_name", lambda: "test-pipeline")
    monkeypatch.setattr(pipeline_executor_module, "inspect_prerequisites", fail_inspect, raising=False)
    monkeypatch.setattr(pipeline_executor_module, "create_pipeline", fake_create_pipeline)

    _pipeline_executor()._create_pipeline(
        session_id="session-1",
        cwd=str(tmp_path),
        runtime=_fake_runtime(),
        session_storage=session_storage,
        resume_from_sidecar=True,
    )

    assert create_kwargs["resume_from_sidecar"] is True
    assert create_kwargs["prerequisite_resolution"] == stored_prerequisites


def test_create_pipeline_resume_empty_sidecar_prerequisites_skip_a2a_inspection(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from iac_code.a2a import pipeline_executor as pipeline_executor_module

    pipeline_dir = tmp_path / "pipeline-def"
    _write_pipeline_yaml(pipeline_dir)
    session_root = tmp_path / "session-1"
    sidecar = session_root / "pipeline"
    sidecar.mkdir(parents=True)
    (sidecar / "meta.yaml").write_text(
        json.dumps({"status": "running", "updated_at": 0.0, "prerequisites": {}}),
        encoding="utf-8",
    )
    create_kwargs = {}
    session_storage = MagicMock()
    session_storage.session_dir.return_value = session_root

    def fail_inspect(*args, **kwargs):
        raise AssertionError("empty sidecar prerequisites should still win")

    def fake_create_pipeline(*args, **kwargs):
        create_kwargs.update(kwargs)
        return object()

    monkeypatch.setattr(pipeline_executor_module, "discover_pipelines", lambda: {"test-pipeline": pipeline_dir})
    monkeypatch.setattr(pipeline_executor_module, "get_pipeline_name", lambda: "test-pipeline")
    monkeypatch.setattr(pipeline_executor_module, "inspect_prerequisites", fail_inspect, raising=False)
    monkeypatch.setattr(pipeline_executor_module, "create_pipeline", fake_create_pipeline)

    _pipeline_executor()._create_pipeline(
        session_id="session-1",
        cwd=str(tmp_path),
        runtime=_fake_runtime(),
        session_storage=session_storage,
        resume_from_sidecar=True,
    )

    assert create_kwargs["resume_from_sidecar"] is True
    assert create_kwargs["prerequisite_resolution"] == {}


def test_active_sidecar_mismatch_error_serializes_raw_jsonrpc_data() -> None:
    from iac_code.a2a.jsonrpc_passthrough import install_jsonrpc_error_data_passthrough
    from iac_code.a2a.pipeline_executor import _active_sidecar_mismatch_error

    install_jsonrpc_error_data_passthrough()
    from a2a.server.request_handlers.response_helpers import build_error_response

    error = _active_sidecar_mismatch_error(
        recoverable_task_id="task-owner",
        context_id="ctx-1",
        sidecar_status="waiting_input",
    )

    response = build_error_response("req-1", error)

    assert response["error"]["code"] == -32602
    assert response["error"]["data"] == {
        "recoverableTaskId": "task-owner",
        "contextId": "ctx-1",
        "sidecarStatus": "waiting_input",
    }


@pytest.mark.asyncio
async def test_sync_backup_blocked_sidecar_warning_omits_mounted_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    from iac_code.a2a import pipeline_executor as module

    warning_calls = []

    class FailingPipeline:
        def _save_backup_blocked_sidecar(self, step_id, reason):
            raise OSError("failed at /mnt/oss/customer-bucket/sensitive-session-id/pipeline/meta.yaml")

    monkeypatch.setattr(module.logger, "warning", lambda *args, **kwargs: warning_calls.append(args))

    result = await module._sync_pipeline_backup_blocked_sidecar(
        FailingPipeline(),
        reason=BackupReason.PIPELINE_STEP_COMPLETED,
        step_id="step-1",
    )

    assert result is False
    assert warning_calls
    logged = " ".join(str(arg) for arg in warning_calls[0])
    assert "OSError" in logged
    assert "/mnt/oss" not in logged
    assert "customer-bucket" not in logged
    assert "sensitive-session-id" not in logged


def test_sidecar_matches_backup_blocked_pending_input(tmp_path: Path) -> None:
    from iac_code.a2a.pipeline_events import PipelineA2AContext, PipelineEventTranslator
    from iac_code.a2a.pipeline_executor import _sidecar_matches_task
    from iac_code.a2a.pipeline_stream import PipelineA2AEventPublisher

    pipeline_dir = tmp_path / "pipeline"
    context = PipelineA2AContext(
        pipeline_run_id="ctx-1",
        task_id="task-1",
        context_id="ctx-1",
        pipeline_name="selling",
    )
    translator = PipelineEventTranslator(context)
    backup_blocked = translator.manual_event(
        "backup_blocked",
        "pipeline",
        status="input_required",
        data={"reason": "terminal", "error": "copy failed", "recoverable": True},
    )
    journal = A2APipelineJournal(pipeline_dir)
    journal.append(backup_blocked, durable=True)
    A2APipelineSnapshotStore(pipeline_dir).save(reduce_pipeline_events([backup_blocked]))
    publisher = PipelineA2AEventPublisher(
        event_queue=FakeEventQueue(),
        translator=translator,
        journal=journal,
        snapshot_store=A2APipelineSnapshotStore(pipeline_dir),
    )

    assert _sidecar_matches_task(
        publisher,
        task_id="task-1",
        context_id="ctx-1",
        sidecar_status="backup_blocked",
    )


def dump(event):
    return MessageToDict(event, preserving_proto_field_name=False)


def image_interrupt_input() -> PipelineUserInput:
    return PipelineUserInput(
        content=[ImageBlock(media_type="image/png", data="aGVsbG8=")],
        display_text="[Image input]",
        has_images=True,
    )


def _display_text(value):
    return value.display_text if isinstance(value, PipelineUserInput) else value


class FakePipeline:
    def __init__(self, events, *, session_dir: Path) -> None:
        self.events = events
        self.run_prompts: list[str] = []
        self.resume_prompts: list[str] = []
        self.continue_inputs: list[str | None] = []
        self.continue_calls = 0
        self.pipeline_name = "selling"
        self.sidecar_status = None
        self.sidecar_restore_result = None
        self.clear_sidecar_calls = 0
        self.session = SimpleNamespace(session_dir=session_dir)
        self.handoff_enabled = False
        self.handoff_summary = "handoff summary"

    async def run(self, prompt: str):
        self.run_prompts.append(_display_text(prompt))
        for event in self.events:
            if isinstance(event, BaseException):
                raise event
            yield event

    async def resume(self, prompt: str):
        self.resume_prompts.append(_display_text(prompt))
        for event in self.events:
            if isinstance(event, BaseException):
                raise event
            yield event

    def continue_from_sidecar(self, user_input: str | None = None):
        self.continue_calls += 1
        self.continue_inputs.append(_display_text(user_input))
        return self.run(user_input or "continued")

    def clear_sidecar(self) -> None:
        self.clear_sidecar_calls += 1
        self.sidecar_status = None

    def should_switch_to_normal(self, data: dict) -> bool:
        return self.handoff_enabled

    def build_normal_handoff_summary(self, data: dict) -> str:
        return self.handoff_summary


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy_sidecar_status", ["waiting_input", "completed", "failed"])
async def test_select_stream_promotes_backup_blocked_pending_input_for_legacy_sidecar(
    tmp_path: Path,
    legacy_sidecar_status: str,
) -> None:
    from iac_code.a2a.pipeline_events import PipelineA2AContext, PipelineEventTranslator
    from iac_code.a2a.pipeline_executor import IacCodeA2APipelineExecutor
    from iac_code.a2a.pipeline_stream import PipelineA2AEventPublisher

    pipeline_dir = tmp_path / "pipeline"
    context = PipelineA2AContext(
        pipeline_run_id="ctx-1",
        task_id="task-1",
        context_id="ctx-1",
        pipeline_name="selling",
    )
    translator = PipelineEventTranslator(context)
    backup_blocked = translator.manual_event(
        "backup_blocked",
        "pipeline",
        status="input_required",
        data={"reason": "terminal", "error": "copy failed", "recoverable": True},
    )
    journal = A2APipelineJournal(pipeline_dir)
    journal.append(backup_blocked, durable=True)
    snapshot_store = A2APipelineSnapshotStore(pipeline_dir)
    snapshot_store.save(reduce_pipeline_events([backup_blocked]))
    publisher = PipelineA2AEventPublisher(
        event_queue=FakeEventQueue(),
        translator=translator,
        journal=journal,
        snapshot_store=snapshot_store,
    )
    pipeline = FakePipeline([], session_dir=pipeline_dir)
    pipeline.sidecar_status = legacy_sidecar_status
    executor = IacCodeA2APipelineExecutor(
        task_store=A2ATaskStore(metrics=NoOpA2AMetrics()),
        model="qwen3.6-plus",
        metrics=NoOpA2AMetrics(),
        artifact_store=None,
        push_notifier=None,
        permission_resolver=None,
        auto_approve_permissions=False,
        thinking_exposure_types=None,
    )

    selected = await executor._select_stream(
        pipeline,
        "retry",
        pipeline_input=normalize_pipeline_user_input("retry"),
        publisher=publisher,
        task_id="task-1",
        context_id="ctx-1",
        fresh_pipeline_factory=lambda: FakePipeline([], session_dir=pipeline_dir),
    )

    assert selected.pipeline is pipeline
    assert pipeline.sidecar_status == "backup_blocked"
    assert pipeline.continue_calls == 1
    assert pipeline.continue_inputs == ["retry"]


@pytest.mark.asyncio
async def test_select_stream_rejects_when_backup_blocked_sidecar_resync_fails(tmp_path: Path) -> None:
    from iac_code.a2a.pipeline_events import PipelineA2AContext, PipelineEventTranslator
    from iac_code.a2a.pipeline_executor import IacCodeA2APipelineExecutor, RecoverablePipelineInvalidParamsError
    from iac_code.a2a.pipeline_stream import PipelineA2AEventPublisher

    class FailingSyncPipeline(FakePipeline):
        def _save_backup_blocked_sidecar(self, step_id, reason):
            return False

    pipeline_dir = tmp_path / "pipeline"
    context = PipelineA2AContext(
        pipeline_run_id="ctx-1",
        task_id="task-1",
        context_id="ctx-1",
        pipeline_name="selling",
    )
    translator = PipelineEventTranslator(context)
    backup_blocked = translator.manual_event(
        "backup_blocked",
        "pipeline",
        status="input_required",
        data={"reason": "terminal", "error": "copy failed", "recoverable": True},
    )
    journal = A2APipelineJournal(pipeline_dir)
    journal.append(backup_blocked, durable=True)
    snapshot_store = A2APipelineSnapshotStore(pipeline_dir)
    snapshot_store.save(reduce_pipeline_events([backup_blocked]))
    publisher = PipelineA2AEventPublisher(
        event_queue=FakeEventQueue(),
        translator=translator,
        journal=journal,
        snapshot_store=snapshot_store,
    )
    pipeline = FailingSyncPipeline([], session_dir=pipeline_dir)
    pipeline.sidecar_status = "waiting_input"
    executor = IacCodeA2APipelineExecutor(
        task_store=A2ATaskStore(metrics=NoOpA2AMetrics()),
        model="qwen3.6-plus",
        metrics=NoOpA2AMetrics(),
        artifact_store=None,
        push_notifier=None,
        permission_resolver=None,
        auto_approve_permissions=False,
        thinking_exposure_types=None,
    )

    with pytest.raises(RecoverablePipelineInvalidParamsError) as exc_info:
        await executor._select_stream(
            pipeline,
            "retry",
            pipeline_input=normalize_pipeline_user_input("retry"),
            publisher=publisher,
            task_id="task-1",
            context_id="ctx-1",
            fresh_pipeline_factory=lambda: FakePipeline([], session_dir=pipeline_dir),
        )

    assert exc_info.value.data["recoverableTaskId"] == "task-1"
    assert exc_info.value.data["sidecarStatus"] == "backup_blocked"
    assert pipeline.continue_calls == 0


class TerminalSidecarAfterCompletionPipeline(FakePipeline):
    async def run(self, prompt: str):
        self.run_prompts.append(_display_text(prompt))
        for event in self.events:
            if isinstance(event, BaseException):
                raise event
            yield event
            if isinstance(event, PipelineEvent) and event.type == PipelineEventType.PIPELINE_COMPLETED:
                self.sidecar_status = "completed"


class CloseableEventStream:
    def __init__(self, events, *, wait_until_closed: bool = True) -> None:
        self.events = list(events)
        self.wait_until_closed = wait_until_closed
        self.started = asyncio.Event()
        self.closed_event = asyncio.Event()
        self.closed = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        self.started.set()
        if self.events:
            await asyncio.sleep(0)
            return self.events.pop(0)
        if self.wait_until_closed:
            await self.closed_event.wait()
        raise StopAsyncIteration

    async def aclose(self) -> None:
        self.closed = True
        self.closed_event.set()


class FakeToolRegistry:
    def register(self, tool) -> None:
        pass

    def unregister(self, tool_name: str) -> None:
        pass


class RecordingBackupService:
    def __init__(
        self,
        *,
        block_reasons: set[BackupReason] | None = None,
        expected_task_states: dict[BackupReason, str] | None = None,
        expected_handoff_summaries: dict[BackupReason, str] | None = None,
        pipeline_snapshot_dir: Path | None = None,
        on_backup=None,
    ) -> None:
        self.block_reasons = block_reasons or set()
        self.expected_task_states = expected_task_states or {}
        self.expected_handoff_summaries = expected_handoff_summaries or {}
        self.pipeline_snapshot_dir = pipeline_snapshot_dir
        self.on_backup = on_backup
        self.calls: list[tuple[str, str, BackupReason, bool]] = []
        self.session_snapshots: list[tuple[BackupReason, dict, dict]] = []
        self.pipeline_snapshots: list[tuple[BackupReason, dict]] = []

    def backup_session(self, cwd: str, session_id: str, *, reason: BackupReason, critical: bool) -> None:
        self.calls.append((cwd, session_id, reason, critical))
        session_dir = SessionStorage().session_dir(cwd, session_id)
        task_snapshot = json.loads((session_dir / "a2a" / "task.json").read_text(encoding="utf-8"))
        context_snapshot = json.loads((session_dir / "a2a" / "context.json").read_text(encoding="utf-8"))
        self.session_snapshots.append((reason, task_snapshot, context_snapshot))
        expected_state = self.expected_task_states.get(reason)
        if expected_state is not None:
            assert task_snapshot["state"] == expected_state
            assert context_snapshot["session_id"] == session_id
        expected_handoff_summary = self.expected_handoff_summaries.get(reason)
        if expected_handoff_summary is not None:
            assert self.pipeline_snapshot_dir is not None
            pipeline_snapshot = A2APipelineSnapshotStore(self.pipeline_snapshot_dir).load()
            assert pipeline_snapshot is not None
            self.pipeline_snapshots.append((reason, pipeline_snapshot))
            handoff_snapshot = pipeline_snapshot.get("normalHandoff") or pipeline_snapshot.get("pendingNormalHandoff")
            assert handoff_snapshot["summary"] == expected_handoff_summary
        if self.on_backup is not None:
            self.on_backup(reason)
        if reason in self.block_reasons:
            raise SessionBackupBlocked(f"backup failed for secret_token=tok-live at /tmp/iac-code/{reason.value}")


class SpyMetrics(NoOpA2AMetrics):
    def __init__(self) -> None:
        self.task_failed = 0
        self.turn_completed = 0
        self.executor_error = 0
        self.backup_blocked: list[tuple[str, bool]] = []
        self.backup_failed: list[tuple[str, bool, int]] = []
        self.backup_succeeded: list[tuple[str, bool, int]] = []

    def record_task_failed(self) -> None:
        self.task_failed += 1

    def record_turn_completed(self) -> None:
        self.turn_completed += 1

    def record_executor_error(self) -> None:
        self.executor_error += 1

    def record_backup_blocked(self, *, reason: str, recoverable: bool) -> None:
        self.backup_blocked.append((reason, recoverable))

    def record_backup_failed(self, *, reason: str, critical: bool, retry_count: int) -> None:
        self.backup_failed.append((reason, critical, retry_count))

    def record_backup_succeeded(self, *, reason: str, critical: bool, retry_count: int) -> None:
        self.backup_succeeded.append((reason, critical, retry_count))


def _fake_runtime():
    return SimpleNamespace(provider_manager=object(), tool_registry=FakeToolRegistry())


def _status_events(queue: FakeEventQueue) -> list[dict]:
    return [dump(event) for event in queue.events if isinstance(event, TaskStatusUpdateEvent)]


def _pipeline_status_events(queue: FakeEventQueue) -> list[dict]:
    return [
        dumped["metadata"]["iac_code"]["pipeline"]
        for event in queue.events
        if isinstance(event, TaskStatusUpdateEvent)
        for dumped in [dump(event)]
        if "pipeline" in dumped.get("metadata", {}).get("iac_code", {})
    ]


def test_a2a_pipeline_dir_for_session_rejects_legacy_session(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from iac_code.a2a.pipeline_paths import a2a_pipeline_dir_for_session

    config_dir = tmp_path / "config"
    cwd = tmp_path / "workspace"
    cwd.mkdir()
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(config_dir))
    session_id = "legacy-session"
    legacy_session_dir = SessionStorage().session_dir(str(cwd), session_id)
    legacy_session_dir.mkdir(parents=True)
    (legacy_session_dir / "session.jsonl").write_text("", encoding="utf-8")

    with pytest.raises(UnsupportedSessionLayoutError):
        a2a_pipeline_dir_for_session(cwd=str(cwd), session_id=session_id)


def test_a2a_pipeline_dir_for_session_uses_long_cwd_legacy_sidecar_over_metadata_shadow(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from iac_code.a2a.pipeline_paths import a2a_pipeline_dir_for_session
    from iac_code.utils import project_paths

    config_dir = tmp_path / "config"
    cwd = "x" * (project_paths.MAX_SANITIZED_LENGTH + 50)
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(config_dir))
    current_project_dir, legacy_project_dir = project_paths.project_dir_candidates(cwd, config_dir / "projects")
    session_id = "legacy-a2a-sidecar-shadow"
    write_session_metadata(
        current_project_dir / session_id,
        SessionMetadata(session_id=session_id, cwd=cwd, layout_version=SESSION_LAYOUT_VERSION_V2),
    )
    legacy_a2a_pipeline_dir = legacy_project_dir / session_id / "a2a" / "pipeline"
    legacy_a2a_pipeline_dir.mkdir(parents=True)
    (legacy_a2a_pipeline_dir / "a2a-events.jsonl").write_text("", encoding="utf-8")

    assert a2a_pipeline_dir_for_session(cwd=cwd, session_id=session_id) == legacy_a2a_pipeline_dir


def test_a2a_pipeline_dir_for_session_reuses_legacy_flat_direct_a2a_sidecar(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from iac_code.a2a.pipeline_paths import a2a_pipeline_dir_for_session, existing_a2a_pipeline_dir_for_session

    config_dir = tmp_path / "config"
    cwd = tmp_path / "workspace"
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(config_dir))
    storage = SessionStorage()
    legacy_path = storage.legacy_session_path(str(cwd), "legacy-a2a")
    legacy_path.parent.mkdir(parents=True)
    legacy_path.write_text('{"role":"user","content":"legacy"}\n', encoding="utf-8")
    a2a_pipeline_dir = legacy_path.parent / "legacy-a2a" / "a2a" / "pipeline"
    a2a_pipeline_dir.mkdir(parents=True)
    (a2a_pipeline_dir / "a2a-events.jsonl").write_text("", encoding="utf-8")

    assert a2a_pipeline_dir_for_session(cwd=str(cwd), session_id="legacy-a2a") == a2a_pipeline_dir
    assert existing_a2a_pipeline_dir_for_session(cwd=str(cwd), session_id="legacy-a2a") == a2a_pipeline_dir


def test_pipeline_publisher_prefers_session_artifact_store_when_session_available(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from iac_code.a2a.pipeline_executor import IacCodeA2APipelineExecutor

    config_dir = tmp_path / "config"
    cwd = tmp_path / "workspace"
    cwd.mkdir()
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(config_dir))
    session_id = "session-1"
    session_dir = SessionStorage().session_dir(str(cwd), session_id)
    write_session_metadata(
        session_dir,
        SessionMetadata(session_id=session_id, cwd=str(cwd), layout_version=SESSION_LAYOUT_VERSION_V2),
    )
    global_store = A2AArtifactStore(config_dir / "a2a" / "artifacts")
    executor = IacCodeA2APipelineExecutor(
        task_store=A2ATaskStore(metrics=NoOpA2AMetrics()),
        model="qwen3.6-plus",
        metrics=NoOpA2AMetrics(),
        artifact_store=global_store,
        push_notifier=None,
        permission_resolver=None,
        auto_approve_permissions=False,
        thinking_exposure_types=None,
    )

    publisher = executor._publisher(
        event_queue=FakeEventQueue(),
        pipeline=FakePipeline([], session_dir=session_dir / "pipeline"),
        task_id="task-1",
        context_id="ctx-1",
        session_id=session_id,
        cwd=str(cwd),
    )

    assert publisher.artifact_store is not None
    assert publisher.artifact_store.root == session_dir / "a2a" / "artifacts"
    assert publisher.artifact_store.root != global_store.root


def test_pipeline_artifact_store_uses_global_store_for_legacy_session(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from iac_code.a2a.pipeline_executor import IacCodeA2APipelineExecutor

    config_dir = tmp_path / "config"
    cwd = tmp_path / "workspace"
    cwd.mkdir()
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(config_dir))
    session_id = "legacy-session"
    SessionStorage().session_dir(str(cwd), session_id).mkdir(parents=True)
    global_store = A2AArtifactStore(config_dir / "a2a" / "artifacts")
    executor = IacCodeA2APipelineExecutor(
        task_store=A2ATaskStore(metrics=NoOpA2AMetrics()),
        model="qwen3.6-plus",
        metrics=NoOpA2AMetrics(),
        artifact_store=global_store,
        push_notifier=None,
        permission_resolver=None,
        auto_approve_permissions=False,
        thinking_exposure_types=None,
    )

    assert executor._artifact_store_for_session(cwd=str(cwd), session_id=session_id) is global_store


def test_pipeline_artifact_store_rejects_unsupported_session_layout(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from iac_code.a2a.pipeline_executor import IacCodeA2APipelineExecutor
    from iac_code.services.session_layout import UnsupportedSessionLayoutError

    config_dir = tmp_path / "config"
    cwd = tmp_path / "workspace"
    cwd.mkdir()
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(config_dir))
    session_id = "session-1"
    session_dir = SessionStorage().session_dir(str(cwd), session_id)
    write_session_metadata(session_dir, SessionMetadata(session_id=session_id, cwd=str(cwd), layout_version=99))
    global_store = A2AArtifactStore(config_dir / "a2a" / "artifacts")
    executor = IacCodeA2APipelineExecutor(
        task_store=A2ATaskStore(metrics=NoOpA2AMetrics()),
        model="qwen3.6-plus",
        metrics=NoOpA2AMetrics(),
        artifact_store=global_store,
        push_notifier=None,
        permission_resolver=None,
        auto_approve_permissions=False,
        thinking_exposure_types=None,
    )

    with pytest.raises(UnsupportedSessionLayoutError):
        executor._artifact_store_for_session(cwd=str(cwd), session_id=session_id)


def test_existing_a2a_pipeline_dir_rejects_unsupported_session_layout(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from iac_code.a2a.pipeline_paths import existing_a2a_pipeline_dir_for_session

    config_dir = tmp_path / "config"
    cwd = tmp_path / "workspace"
    cwd.mkdir()
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(config_dir))
    session_id = "session-1"
    session_dir = SessionStorage().session_dir(str(cwd), session_id)
    write_session_metadata(session_dir, SessionMetadata(session_id=session_id, cwd=str(cwd), layout_version=99))
    preferred = session_dir / "a2a" / "pipeline"
    preferred.mkdir(parents=True)
    (preferred / "a2a-events.jsonl").write_text("", encoding="utf-8")

    with pytest.raises(UnsupportedSessionLayoutError, match="Unsupported session layout version"):
        existing_a2a_pipeline_dir_for_session(cwd=str(cwd), session_id=session_id)


def test_existing_a2a_pipeline_dir_ignores_symlinked_legacy_sidecar_placeholder(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from iac_code.a2a.pipeline_paths import existing_a2a_pipeline_dir_for_session

    config_dir = tmp_path / "config"
    cwd = tmp_path / "workspace"
    cwd.mkdir()
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(config_dir))
    session_id = "legacy-session"
    storage = SessionStorage()
    legacy_path = storage.legacy_session_path(str(cwd), session_id)
    legacy_path.parent.mkdir(parents=True, exist_ok=True)
    legacy_path.write_text('{"role":"user","content":"legacy"}\n', encoding="utf-8")
    external_dir = tmp_path / "external-sidecars"
    external_a2a_pipeline = external_dir / "a2a" / "pipeline"
    external_a2a_pipeline.mkdir(parents=True)
    (external_a2a_pipeline / "a2a-events.jsonl").write_text("", encoding="utf-8")
    placeholder_dir = legacy_path.with_name(f"{legacy_path.stem}.legacy-sidecars")
    try:
        placeholder_dir.symlink_to(external_dir, target_is_directory=True)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"symlink creation unsupported: {exc}")

    pipeline_dir = existing_a2a_pipeline_dir_for_session(cwd=str(cwd), session_id=session_id)

    assert pipeline_dir != external_a2a_pipeline
    assert pipeline_dir.parent.parent.name == f"{placeholder_dir.name}.conflict-sidecars"


def test_pipeline_sidecar_dir_uses_existing_legacy_a2a_sidecar_without_v2_comparison(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from iac_code.a2a.pipeline_executor import _pipeline_sidecar_dir

    config_dir = tmp_path / "config"
    cwd = tmp_path / "workspace"
    cwd.mkdir()
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(config_dir))
    session_id = "legacy-session"
    session_dir = SessionStorage().session_dir(str(cwd), session_id)
    legacy_sidecar = session_dir / "pipeline"
    legacy_sidecar.mkdir(parents=True)
    (legacy_sidecar / "a2a-events.jsonl").write_text("", encoding="utf-8")
    pipeline = FakePipeline([], session_dir=legacy_sidecar)

    assert _pipeline_sidecar_dir(pipeline, str(cwd), session_id) == legacy_sidecar


def test_pipeline_artifact_store_propagates_unexpected_session_helper_errors(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from iac_code.a2a.pipeline_executor import IacCodeA2APipelineExecutor

    cwd = tmp_path / "workspace"
    cwd.mkdir()
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    SessionStorage().ensure_v2_session_dir_for_new_session(str(cwd), "session-1")
    executor = IacCodeA2APipelineExecutor(
        task_store=A2ATaskStore(metrics=NoOpA2AMetrics()),
        model="qwen3.6-plus",
        metrics=NoOpA2AMetrics(),
        artifact_store=A2AArtifactStore(tmp_path / "global-artifacts"),
        push_notifier=None,
        permission_resolver=None,
        auto_approve_permissions=False,
        thinking_exposure_types=None,
    )
    monkeypatch.setattr(
        "iac_code.a2a.pipeline_executor.artifact_store_for_session",
        lambda _session_dir: (_ for _ in ()).throw(RuntimeError("helper failed")),
    )

    with pytest.raises(RuntimeError, match="helper failed"):
        executor._artifact_store_for_session(cwd=str(cwd), session_id="session-1")


@pytest.mark.asyncio
async def test_pipeline_executor_applies_aliyun_metadata_while_creating_runtime(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from iac_code.a2a.pipeline_executor import IacCodeA2APipelineExecutor
    from iac_code.services.providers.aliyun import AliyunCredential, AliyunCredentials

    captured_credentials: list[tuple[str, str | None, str | None]] = []

    def runtime_factory(options):
        credential = AliyunCredentials.load()
        captured_credentials.append(
            (
                options.model,
                credential.access_key_id if credential else None,
                credential.region_id if credential else None,
            )
        )
        return _fake_runtime()

    fake_pipeline = FakePipeline(
        [PipelineEvent(type=PipelineEventType.PIPELINE_COMPLETED, step_id=None, timestamp=1717821601.0, data={})],
        session_dir=tmp_path / "sidecar",
    )
    monkeypatch.setenv("ALIBABA_CLOUD_ACCESS_KEY_ID", "env-id")
    monkeypatch.setenv("ALIBABA_CLOUD_ACCESS_KEY_SECRET", "env-secret")
    monkeypatch.setenv("ALIBABA_CLOUD_REGION_ID", "cn-shanghai")
    monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_agent_runtime", runtime_factory)
    monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_pipeline", lambda *args, **kwargs: fake_pipeline)
    monkeypatch.setattr("iac_code.tools.cloud.registry.register_cloud_tools", lambda *args, **kwargs: None)
    monkeypatch.setattr("iac_code.services.providers.aliyun.AliyunCredentials._load_from_iac_code_config", lambda: None)

    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    executor = IacCodeA2APipelineExecutor(
        task_store=store,
        model="qwen3.6-plus",
        metrics=NoOpA2AMetrics(),
        artifact_store=None,
        push_notifier=None,
        permission_resolver=None,
        auto_approve_permissions=False,
        thinking_exposure_types=None,
        aliyun_credential=AliyunCredential(
            access_key_id="client-id",
            access_key_secret="client-secret",
            region_id="cn-beijing",
        ),
    )

    await executor.execute(
        context=FakeRequestContext(metadata={"iac_code": {"cwd": str(tmp_path)}}),
        event_queue=FakeEventQueue(),
        task=await store.get_or_create_task(task_id="task-1", context_id="ctx-1"),
        task_id="task-1",
        context_id="ctx-1",
        cwd=str(tmp_path),
        prompt="部署网站",
    )

    assert captured_credentials == [("qwen3.6-plus", "client-id", "cn-beijing")]
    assert AliyunCredentials.load().access_key_id == "env-id"


@pytest.mark.asyncio
async def test_pipeline_executor_refreshes_cloud_tools_with_aliyun_metadata_for_reused_context(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from iac_code.a2a.pipeline_executor import IacCodeA2APipelineExecutor
    from iac_code.services.providers.aliyun import AliyunCredential, AliyunCredentials

    seen_access_key_ids: list[str | None] = []
    runtime = _fake_runtime()

    def fake_register_cloud_tools(registry, credentials):
        assert registry is runtime.tool_registry
        credential = credentials.get_provider("aliyun")
        seen_access_key_ids.append(credential.access_key_id if credential else None)

    fake_pipeline = FakePipeline(
        [PipelineEvent(type=PipelineEventType.PIPELINE_COMPLETED, step_id=None, timestamp=1717821601.0, data={})],
        session_dir=tmp_path / "sidecar",
    )
    monkeypatch.setenv("ALIBABA_CLOUD_ACCESS_KEY_ID", "env-id")
    monkeypatch.setenv("ALIBABA_CLOUD_ACCESS_KEY_SECRET", "env-secret")
    monkeypatch.setenv("ALIBABA_CLOUD_REGION_ID", "cn-shanghai")
    monkeypatch.setattr("iac_code.tools.cloud.registry.register_cloud_tools", fake_register_cloud_tools)
    monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_pipeline", lambda *args, **kwargs: fake_pipeline)
    monkeypatch.setattr("iac_code.services.providers.aliyun.AliyunCredentials._load_from_iac_code_config", lambda: None)

    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    await store.get_or_create_context(
        context_id="ctx-1",
        cwd=str(tmp_path),
        runtime_factory=lambda _session_id: runtime,
    )
    executor = IacCodeA2APipelineExecutor(
        task_store=store,
        model="qwen3.6-plus",
        metrics=NoOpA2AMetrics(),
        artifact_store=None,
        push_notifier=None,
        permission_resolver=None,
        auto_approve_permissions=False,
        thinking_exposure_types=None,
        aliyun_credential=AliyunCredential(
            access_key_id="client-id",
            access_key_secret="client-secret",
            region_id="cn-beijing",
        ),
    )

    await executor.execute(
        context=FakeRequestContext(context_id="ctx-1", metadata={"iac_code": {"cwd": str(tmp_path)}}),
        event_queue=FakeEventQueue(),
        task=await store.get_or_create_task(task_id="task-1", context_id="ctx-1"),
        task_id="task-1",
        context_id="ctx-1",
        cwd=str(tmp_path),
        prompt="部署网站",
    )

    assert seen_access_key_ids == ["client-id"]
    assert AliyunCredentials.load().access_key_id == "env-id"


@pytest.mark.asyncio
async def test_pipeline_executor_reconfigures_cached_runtime_model_and_api_key_per_call(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from iac_code.a2a.pipeline_executor import IacCodeA2APipelineExecutor
    from iac_code.providers.request_policy import ProviderRequestPolicy

    class FakeProviderManager:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, str], object | None]] = []

        def reconfigure(
            self,
            model,
            credentials,
            provider_key_override=None,
            base_url_override=None,
            request_policy_override=None,
        ):
            self.calls.append((model, dict(credentials), request_policy_override))

    provider_manager = FakeProviderManager()
    runtime = SimpleNamespace(provider_manager=provider_manager, tool_registry=object())
    created_pipeline_count = 0

    def fake_create_pipeline(*args, **kwargs):
        nonlocal created_pipeline_count
        created_pipeline_count += 1
        return FakePipeline(
            [PipelineEvent(type=PipelineEventType.PIPELINE_COMPLETED, step_id=None, timestamp=1717821601.0, data={})],
            session_dir=tmp_path / f"sidecar-{created_pipeline_count}",
        )

    monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_agent_runtime", lambda options: runtime)
    monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_pipeline", fake_create_pipeline)
    monkeypatch.setattr("iac_code.config.load_credentials", lambda model=None: {"dashscope": "fallback-key"})

    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    task_one = await store.get_or_create_task(task_id="task-1", context_id="ctx-1")
    metadata_executor = IacCodeA2APipelineExecutor(
        task_store=store,
        model="qwen3.6-max",
        metrics=NoOpA2AMetrics(),
        artifact_store=None,
        push_notifier=None,
        permission_resolver=None,
        auto_approve_permissions=False,
        thinking_exposure_types=None,
        model_from_metadata=True,
        metadata_api_key="metadata-key",
        request_policy_override=ProviderRequestPolicy(effort="high", thinking_budget=2048),
    )
    await metadata_executor.execute(
        context=FakeRequestContext(context_id="ctx-1", metadata={"iac_code": {"cwd": str(tmp_path)}}),
        event_queue=FakeEventQueue(),
        task=task_one,
        task_id="task-1",
        context_id="ctx-1",
        cwd=str(tmp_path),
        prompt="部署网站",
    )

    task_two = await store.get_or_create_task(task_id="task-2", context_id="ctx-1")
    default_executor = IacCodeA2APipelineExecutor(
        task_store=store,
        model="qwen3.6-plus",
        metrics=NoOpA2AMetrics(),
        artifact_store=None,
        push_notifier=None,
        permission_resolver=None,
        auto_approve_permissions=False,
        thinking_exposure_types=None,
    )
    await default_executor.execute(
        context=FakeRequestContext(context_id="ctx-1", task_id="task-2", metadata={"iac_code": {"cwd": str(tmp_path)}}),
        event_queue=FakeEventQueue(),
        task=task_two,
        task_id="task-2",
        context_id="ctx-1",
        cwd=str(tmp_path),
        prompt="继续",
    )

    first_policy = provider_manager.calls[0][2]
    assert provider_manager.calls[0][0] == "qwen3.6-max"
    assert provider_manager.calls[0][1] == {"dashscope": "metadata-key"}
    assert getattr(first_policy, "effort", None) == "high"
    assert getattr(first_policy, "thinking_budget", None) == 2048
    assert provider_manager.calls[1] == ("qwen3.6-plus", {"dashscope": "fallback-key"}, None)


async def _wait_for_output_text(task, expected: str) -> None:
    for _ in range(_A2A_ASYNC_TEST_TIMEOUT * 100):
        if "".join(task.output_text) == expected:
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"Expected output text {expected!r}, got {''.join(task.output_text)!r}")


async def _wait_for_pipeline_event(queue: FakeEventQueue, expected_event_type: str) -> None:
    for _ in range(_A2A_ASYNC_TEST_TIMEOUT * 100):
        for event in queue.events:
            if not isinstance(event, TaskStatusUpdateEvent):
                continue
            metadata = dump(event).get("metadata", {}).get("iac_code", {})
            pipeline = metadata.get("pipeline", {})
            if pipeline.get("eventType") == expected_event_type:
                return
        await asyncio.sleep(0.01)
    event_types = [
        dump(event).get("metadata", {}).get("iac_code", {}).get("pipeline", {}).get("eventType")
        for event in queue.events
        if isinstance(event, TaskStatusUpdateEvent)
    ]
    raise AssertionError(f"Expected pipeline event {expected_event_type!r}, got {event_types!r}")


def _pending_coro_names() -> list[str]:
    current = asyncio.current_task()
    return [
        getattr(task.get_coro(), "__qualname__", repr(task.get_coro()))
        for task in asyncio.all_tasks()
        if task is not current and not task.done()
    ]


@pytest.mark.asyncio
async def test_executor_runs_pipeline_mode(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("IAC_CODE_MODE", "pipeline")
    fake_pipeline = FakePipeline(
        [
            PipelineEvent(
                type=PipelineEventType.PIPELINE_STARTED,
                step_id=None,
                timestamp=1717821600.0,
                data={"total_steps": 1, "step_names": ["intent_parsing"]},
            ),
            TextDeltaEvent(text="pipeline output"),
            PipelineEvent(
                type=PipelineEventType.PIPELINE_COMPLETED,
                step_id=None,
                timestamp=1717821601.0,
                data={"total_steps": 1},
            ),
        ],
        session_dir=tmp_path / "sidecar",
    )
    monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_pipeline", lambda *args, **kwargs: fake_pipeline)
    monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_agent_runtime", lambda options: _fake_runtime())

    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    executor = IacCodeA2AExecutor(task_store=store, model="qwen3.6-plus")
    queue = FakeEventQueue()

    await executor.execute(FakeRequestContext(metadata={"iac_code": {"cwd": str(tmp_path)}}), queue)

    assert fake_pipeline.run_prompts == ["hello"]
    states = [dump(event)["status"]["state"] for event in queue.events if isinstance(event, TaskStatusUpdateEvent)]
    assert "TASK_STATE_WORKING" in states
    assert states[-1] == "TASK_STATE_COMPLETED"
    event_types = [
        dump(event)["metadata"]["iac_code"]["pipeline"]["eventType"]
        for event in queue.events
        if isinstance(event, TaskStatusUpdateEvent)
        and "pipeline" in dump(event).get("metadata", {}).get("iac_code", {})
    ]
    assert event_types == ["pipeline_started", "text_delta", "pipeline_completed", "backup_committed"]
    record = await store.get_or_create_task(task_id="task-1", context_id="ctx-1")
    assert record.state == "completed"
    assert "".join(record.output_text) == "pipeline output"


@pytest.mark.asyncio
async def test_pipeline_executor_publishes_mcp_warnings(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("IAC_CODE_MODE", "pipeline")
    fake_pipeline = FakePipeline(
        [
            PipelineEvent(
                type=PipelineEventType.PIPELINE_COMPLETED,
                step_id=None,
                timestamp=1717821601.0,
                data={"total_steps": 1},
            ),
        ],
        session_dir=tmp_path / "sidecar",
    )
    runtime = _fake_runtime()
    runtime.mcp_config_warnings = [
        SimpleNamespace(server_name="broken", code="connection_failed", message="MCP server failed")
    ]
    monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_pipeline", lambda *args, **kwargs: fake_pipeline)
    monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_agent_runtime", lambda options: runtime)

    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    executor = IacCodeA2AExecutor(task_store=store, model="qwen3.6-plus")
    queue = FakeEventQueue()

    await executor.execute(FakeRequestContext(metadata={"iac_code": {"cwd": str(tmp_path)}}), queue)

    warning_events = [
        dump(event)
        for event in queue.events
        if isinstance(event, TaskStatusUpdateEvent)
        and "mcpWarning" in dump(event).get("metadata", {}).get("iac_code", {})
    ]
    assert len(warning_events) == 1
    assert warning_events[0]["status"]["message"]["parts"][0]["text"] == "MCP warning: MCP server failed"
    assert warning_events[0]["metadata"]["iac_code"]["mcpWarning"]["serverName"] == "broken"


@pytest.mark.asyncio
async def test_pipeline_executor_passes_mcp_status_metadata_to_pipeline_factory(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("IAC_CODE_MODE", "pipeline")
    fake_pipeline = FakePipeline(
        [
            PipelineEvent(
                type=PipelineEventType.PIPELINE_COMPLETED,
                step_id=None,
                timestamp=1717821601.0,
                data={"total_steps": 1},
            ),
        ],
        session_dir=tmp_path / "sidecar",
    )
    runtime = _fake_runtime()
    runtime.mcp_manager = SimpleNamespace(list_connections=lambda: [])
    runtime.mcp_config_warnings = [
        SimpleNamespace(server_name="broken", code="connection_failed", message="MCP server failed")
    ]
    captured_kwargs: dict = {}

    def fake_create_pipeline(*args, **kwargs):
        captured_kwargs.update(kwargs)
        return fake_pipeline

    monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_pipeline", fake_create_pipeline)
    monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_agent_runtime", lambda options: runtime)

    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    executor = IacCodeA2AExecutor(task_store=store, model="qwen3.6-plus")

    await executor.execute(FakeRequestContext(metadata={"iac_code": {"cwd": str(tmp_path)}}), FakeEventQueue())

    assert captured_kwargs["mcp_manager"] is runtime.mcp_manager
    assert captured_kwargs["mcp_config_warnings"] == runtime.mcp_config_warnings


@pytest.mark.asyncio
async def test_executor_publishes_normal_handoff_ready_after_completed_pipeline(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("IAC_CODE_MODE", "pipeline")
    session_dir = tmp_path / "sidecar"
    fake_pipeline = FakePipeline(
        [
            PipelineEvent(
                type=PipelineEventType.PIPELINE_COMPLETED,
                step_id=None,
                timestamp=1717821601.0,
                data={"total_steps": 1},
            ),
        ],
        session_dir=session_dir,
    )
    fake_pipeline.handoff_enabled = True
    fake_pipeline.handoff_summary = "[Pipeline Handoff Context]\nPipeline: selling"

    def fake_create_pipeline(*args, **kwargs):
        fake_pipeline._session_storage = kwargs["session_storage"]
        fake_pipeline._session_id = kwargs["session_id"]
        fake_pipeline._cwd = kwargs["cwd"]
        return fake_pipeline

    monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_pipeline", fake_create_pipeline)
    monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_agent_runtime", lambda options: _fake_runtime())

    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    executor = IacCodeA2AExecutor(task_store=store, model="qwen3.6-plus")
    queue = FakeEventQueue()

    await executor.execute(FakeRequestContext(metadata={"iac_code": {"cwd": str(tmp_path)}}), queue)

    pipeline_events = [
        dump(event)["metadata"]["iac_code"]["pipeline"]
        for event in queue.events
        if isinstance(event, TaskStatusUpdateEvent)
        and "pipeline" in dump(event).get("metadata", {}).get("iac_code", {})
    ]
    assert [event["eventType"] for event in pipeline_events] == [
        "pipeline_completed",
        "backup_committed",
        "pipeline_handoff_ready",
        "backup_committed",
    ]
    assert [event["data"]["committedEventType"] for event in pipeline_events[1::2]] == [
        "pipeline_completed",
        "pipeline_handoff_ready",
    ]
    handoff = pipeline_events[2]
    assert handoff["status"] == "completed"
    assert handoff["data"] == {
        "action": "switch_to_normal",
        "targetMode": "normal",
        "outcome": "completed",
        "summary": "[Pipeline Handoff Context]\nPipeline: selling",
    }

    snapshot = A2APipelineSnapshotStore(session_dir).load()
    assert snapshot is not None
    assert snapshot["normalHandoff"]["action"] == "switch_to_normal"
    assert snapshot["normalHandoff"]["targetMode"] == "normal"
    assert snapshot["normalHandoff"]["summary"] == "[Pipeline Handoff Context]\nPipeline: selling"
    from iac_code.services.session_storage import SessionStorage

    session_id = store._contexts["ctx-1"].session_id
    messages = SessionStorage().load(str(tmp_path), session_id)
    assert messages[-1].role == "user"
    assert messages[-1].content == "[Pipeline Handoff Context]\nPipeline: selling"


@pytest.mark.asyncio
async def test_executor_publishes_normal_handoff_ready_with_cleanup_resources(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("IAC_CODE_MODE", "pipeline")
    session_dir = tmp_path / "sidecar"
    ledger = CleanupLedger(session_dir / "cleanup.yaml")
    ledger.mark_cleanup_required(
        [
            CleanupResource(
                provider="ros",
                resource_type="stack",
                resource_id="stack-123",
                resource_name="selling-stack",
                region_id="cn-hangzhou",
                source_step_id="deploying",
            )
        ],
        source_step_id="deploying",
        reason="rollback from deploying",
    )
    fake_pipeline = FakePipeline(
        [
            PipelineEvent(
                type=PipelineEventType.PIPELINE_COMPLETED,
                step_id=None,
                timestamp=1717821601.0,
                data={"total_steps": 1},
            ),
        ],
        session_dir=session_dir,
    )
    fake_pipeline.handoff_enabled = True
    fake_pipeline.handoff_summary = "[Pipeline Handoff Context]\nPipeline: selling"
    fake_pipeline.cleanup_ledger = lambda: ledger

    def fake_create_pipeline(*args, **kwargs):
        fake_pipeline._session_storage = kwargs["session_storage"]
        fake_pipeline._session_id = kwargs["session_id"]
        fake_pipeline._cwd = kwargs["cwd"]
        return fake_pipeline

    monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_pipeline", fake_create_pipeline)
    monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_agent_runtime", lambda options: _fake_runtime())

    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    executor = IacCodeA2AExecutor(task_store=store, model="qwen3.6-plus")
    queue = FakeEventQueue()

    await executor.execute(FakeRequestContext(metadata={"iac_code": {"cwd": str(tmp_path)}}), queue)

    pipeline_events = [
        dump(event)["metadata"]["iac_code"]["pipeline"]
        for event in queue.events
        if isinstance(event, TaskStatusUpdateEvent)
        and "pipeline" in dump(event).get("metadata", {}).get("iac_code", {})
    ]
    handoff = next(event for event in reversed(pipeline_events) if event["eventType"] == "pipeline_handoff_ready")
    cleanup = handoff["data"]["cleanup"]
    assert cleanup["status"] == "pending"
    assert cleanup["resourceCount"] == 1
    assert cleanup["statusMessage"] == "Detected 1 rollback cleanup resources; starting cleanup."
    assert "prompt" not in cleanup
    assert "ledgerPath" not in cleanup
    assert cleanup["resources"] == [
        {
            "provider": "ros",
            "resourceType": "stack",
            "resourceId": "stack-123",
            "resourceName": "selling-stack",
            "regionId": "cn-hangzhou",
            "sourceStepId": "deploying",
            "cleanupStatus": "pending",
            "progressStatus": None,
            "lastError": None,
        }
    ]

    snapshot = A2APipelineSnapshotStore(session_dir).load()
    assert snapshot is not None
    assert snapshot["cleanup"]["status"] == "pending"
    assert snapshot["cleanup"]["resourceCount"] == 1
    assert snapshot["normalHandoff"]["data"]["cleanup"]["resourceCount"] == 1
    assert "prompt" not in snapshot["cleanup"]
    assert "ledgerPath" not in snapshot["cleanup"]
    assert "prompt" not in snapshot["normalHandoff"]["data"]["cleanup"]
    assert "ledgerPath" not in snapshot["normalHandoff"]["data"]["cleanup"]


@pytest.mark.asyncio
async def test_executor_sets_pipeline_telemetry_correlation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("IAC_CODE_MODE", "pipeline")
    fake_pipeline = FakePipeline(
        [
            PipelineEvent(
                type=PipelineEventType.PIPELINE_COMPLETED,
                step_id=None,
                timestamp=1717821601.0,
                data={"total_steps": 1},
            ),
        ],
        session_dir=tmp_path / "sidecar",
    )
    fake_pipeline.set_telemetry_correlation = MagicMock()
    create_pipeline_kwargs = {}

    def fake_create_pipeline(*args, **kwargs):
        create_pipeline_kwargs.update(kwargs)
        fake_pipeline._session_storage = kwargs["session_storage"]
        fake_pipeline._session_id = kwargs["session_id"]
        fake_pipeline._cwd = kwargs["cwd"]
        return fake_pipeline

    monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_pipeline", fake_create_pipeline)
    monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_agent_runtime", lambda options: _fake_runtime())

    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    backup_service = RecordingBackupService()
    executor = IacCodeA2AExecutor(task_store=store, model="qwen3.6-plus", backup_service=backup_service)

    await executor.execute(
        FakeRequestContext(task_id="task-1", context_id="ctx-1", metadata={"iac_code": {"cwd": str(tmp_path)}}),
        FakeEventQueue(),
    )

    fake_pipeline.set_telemetry_correlation.assert_called_once_with(
        task_id="task-1",
        context_id="ctx-1",
        pipeline_run_id="ctx-1",
    )
    assert create_pipeline_kwargs["surface"] == "a2a"
    assert create_pipeline_kwargs["backup_service"] is backup_service


@pytest.mark.asyncio
async def test_executor_publishes_normal_handoff_ready_after_failed_pipeline_when_policy_allows(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("IAC_CODE_MODE", "pipeline")
    session_dir = tmp_path / "sidecar"
    fake_pipeline = FakePipeline(
        [
            PipelineEvent(
                type=PipelineEventType.PIPELINE_COMPLETED,
                step_id=None,
                timestamp=1717821601.0,
                data={"total_steps": 1, "failed": True},
            ),
        ],
        session_dir=session_dir,
    )
    fake_pipeline.handoff_enabled = True
    fake_pipeline.handoff_summary = "[Pipeline Handoff Context]\nOutcome: failed"

    def fake_create_pipeline(*args, **kwargs):
        fake_pipeline._session_storage = kwargs["session_storage"]
        fake_pipeline._session_id = kwargs["session_id"]
        fake_pipeline._cwd = kwargs["cwd"]
        return fake_pipeline

    monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_pipeline", fake_create_pipeline)
    monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_agent_runtime", lambda options: _fake_runtime())

    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    executor = IacCodeA2AExecutor(task_store=store, model="qwen3.6-plus")
    queue = FakeEventQueue()

    await executor.execute(FakeRequestContext(metadata={"iac_code": {"cwd": str(tmp_path)}}), queue)

    pipeline_events = [
        dump(event)["metadata"]["iac_code"]["pipeline"]
        for event in queue.events
        if isinstance(event, TaskStatusUpdateEvent)
        and "pipeline" in dump(event).get("metadata", {}).get("iac_code", {})
    ]
    assert [event["eventType"] for event in pipeline_events] == [
        "pipeline_failed",
        "backup_committed",
        "pipeline_handoff_ready",
        "backup_committed",
    ]
    assert [event["data"]["committedEventType"] for event in pipeline_events[1::2]] == [
        "pipeline_failed",
        "pipeline_handoff_ready",
    ]
    handoff = pipeline_events[2]
    assert handoff["status"] == "failed"
    assert handoff["data"]["outcome"] == "failed"
    assert handoff["data"]["summary"] == "[Pipeline Handoff Context]\nOutcome: failed"

    snapshot = A2APipelineSnapshotStore(session_dir).load()
    assert snapshot is not None
    assert snapshot["status"] == "failed"
    assert snapshot["normalHandoff"]["outcome"] == "failed"


@pytest.mark.asyncio
async def test_pipeline_executor_runs_critical_backup_before_input_required_publication(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("IAC_CODE_MODE", "pipeline")
    fake_pipeline = FakePipeline(
        [
            PipelineEvent(
                type=PipelineEventType.USER_INPUT_REQUIRED,
                step_id="confirm",
                timestamp=1717821601.0,
                data={"prompt": "请选择方案"},
            ),
        ],
        session_dir=tmp_path / "sidecar",
    )
    monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_pipeline", lambda *args, **kwargs: fake_pipeline)
    monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_agent_runtime", lambda options: _fake_runtime())
    backup_service = RecordingBackupService(
        expected_task_states={BackupReason.INPUT_REQUIRED: "input-required"},
    )

    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    executor = IacCodeA2AExecutor(task_store=store, model="qwen3.6-plus", backup_service=backup_service)
    queue = FakeEventQueue()

    await executor.execute(FakeRequestContext(metadata={"iac_code": {"cwd": str(tmp_path)}}), queue)

    assert [(reason, critical) for *_ids, reason, critical in backup_service.calls] == [
        (BackupReason.INPUT_REQUIRED, True)
    ]
    assert _pipeline_status_events(queue)[-1]["eventType"] == "input_required"
    assert _status_events(queue)[-1]["status"]["state"] == "TASK_STATE_INPUT_REQUIRED"


@pytest.mark.asyncio
async def test_pipeline_executor_runs_critical_backups_for_terminal_and_handoff_ready(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("IAC_CODE_MODE", "pipeline")
    session_dir = tmp_path / "sidecar"
    fake_pipeline = FakePipeline(
        [
            PipelineEvent(
                type=PipelineEventType.PIPELINE_COMPLETED,
                step_id=None,
                timestamp=1717821601.0,
                data={"total_steps": 1},
            ),
        ],
        session_dir=session_dir,
    )
    fake_pipeline.handoff_enabled = True
    fake_pipeline.handoff_summary = "[Pipeline Handoff Context]\nPipeline: selling"
    monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_pipeline", lambda *args, **kwargs: fake_pipeline)
    monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_agent_runtime", lambda options: _fake_runtime())

    def assert_committed_publication_before_backup(reason: BackupReason) -> None:
        events = A2APipelineJournal(session_dir).read_all()
        snapshot = A2APipelineSnapshotStore(session_dir).load()
        assert snapshot is not None
        if reason == BackupReason.TERMINAL:
            terminal_events = [event for event in events if event["eventType"] == "pipeline_completed"]
            ack_events = [event for event in events if event["eventType"] == "backup_committed"]
            assert [event.get("visibility") for event in terminal_events] == [
                "pending_backup",
                "committed",
            ]
            assert ack_events == []
            assert snapshot["pendingTerminal"]["eventType"] == "pipeline_completed"
        elif reason == BackupReason.HANDOFF_READY:
            terminal_events = [event for event in events if event["eventType"] == "pipeline_completed"]
            handoff_events = [event for event in events if event["eventType"] == "pipeline_handoff_ready"]
            ack_events = [event for event in events if event["eventType"] == "backup_committed"]
            assert terminal_events[-1].get("visibility") == "committed"
            assert handoff_events[-1].get("visibility") == "committed"
            assert ack_events == []
            assert snapshot["pendingNormalHandoff"]["summary"] == "[Pipeline Handoff Context]\nPipeline: selling"

    backup_service = RecordingBackupService(
        expected_task_states={
            BackupReason.TERMINAL: "working",
            BackupReason.HANDOFF_READY: "working",
        },
        expected_handoff_summaries={
            BackupReason.HANDOFF_READY: "[Pipeline Handoff Context]\nPipeline: selling",
        },
        pipeline_snapshot_dir=session_dir,
        on_backup=assert_committed_publication_before_backup,
    )

    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    executor = IacCodeA2AExecutor(task_store=store, model="qwen3.6-plus", backup_service=backup_service)
    queue = FakeEventQueue()

    await executor.execute(FakeRequestContext(metadata={"iac_code": {"cwd": str(tmp_path)}}), queue)

    assert [(reason, critical) for *_ids, reason, critical in backup_service.calls] == [
        (BackupReason.TERMINAL, True),
        (BackupReason.HANDOFF_READY, True),
    ]
    pipeline_events = _pipeline_status_events(queue)
    assert [event["eventType"] for event in pipeline_events] == [
        "pipeline_completed",
        "backup_committed",
        "pipeline_handoff_ready",
        "backup_committed",
    ]
    assert [event["data"]["committedEventType"] for event in pipeline_events[1::2]] == [
        "pipeline_completed",
        "pipeline_handoff_ready",
    ]
    committed_events = [event for event in pipeline_events if event["eventType"] != "backup_committed"]
    assert [event["visibility"] for event in committed_events] == ["committed", "committed"]
    assert [reason for reason, _snapshot in backup_service.pipeline_snapshots] == [BackupReason.HANDOFF_READY]
    handoff_backup_snapshot = backup_service.pipeline_snapshots[0][1]
    assert handoff_backup_snapshot["status"] == "working"
    assert handoff_backup_snapshot["normalHandoff"] is None
    assert handoff_backup_snapshot["pendingNormalHandoff"]["summary"] == "[Pipeline Handoff Context]\nPipeline: selling"
    final_pipeline_snapshot = A2APipelineSnapshotStore(session_dir).load()
    assert final_pipeline_snapshot is not None
    assert final_pipeline_snapshot["status"] == "completed"
    assert final_pipeline_snapshot["pendingNormalHandoff"] is None
    assert final_pipeline_snapshot["normalHandoff"]["summary"] == "[Pipeline Handoff Context]\nPipeline: selling"
    record = await store.get_or_create_task(task_id="task-1", context_id="ctx-1")
    assert record.state == "completed"
    context_record = store._contexts["ctx-1"]
    root_session_dir = SessionStorage().session_dir(str(tmp_path), context_record.session_id)
    task_snapshot = json.loads((root_session_dir / "a2a" / "task.json").read_text(encoding="utf-8"))
    assert task_snapshot["state"] == "completed"


@pytest.mark.asyncio
async def test_pipeline_backup_blocked_before_input_required_publishes_recoverable_state(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("IAC_CODE_MODE", "pipeline")
    session_dir = tmp_path / "sidecar"
    fake_pipeline = FakePipeline(
        [
            PipelineEvent(
                type=PipelineEventType.USER_INPUT_REQUIRED,
                step_id="confirm",
                timestamp=1717821601.0,
                data={"prompt": "请选择方案"},
            ),
        ],
        session_dir=session_dir,
    )
    monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_pipeline", lambda *args, **kwargs: fake_pipeline)
    monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_agent_runtime", lambda options: _fake_runtime())
    metrics = SpyMetrics()
    backup_service = RecordingBackupService(block_reasons={BackupReason.INPUT_REQUIRED})

    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    executor = IacCodeA2AExecutor(
        task_store=store,
        model="qwen3.6-plus",
        metrics=metrics,
        backup_service=backup_service,
    )
    queue = FakeEventQueue()

    await executor.execute(FakeRequestContext(metadata={"iac_code": {"cwd": str(tmp_path)}}), queue)

    pipeline_events = _pipeline_status_events(queue)
    assert [event["eventType"] for event in pipeline_events] == ["backup_blocked"]
    blocked = pipeline_events[0]
    assert blocked["status"] == "input_required"
    assert blocked["data"]["reason"] == "input_required"
    assert blocked["data"]["recoverable"] is True
    assert "tok-live" not in blocked["data"]["error"]
    assert "/tmp/iac-code" not in blocked["data"]["error"]
    assert _status_events(queue)[-1]["status"]["state"] == "TASK_STATE_INPUT_REQUIRED"
    record = await store.get_or_create_task(task_id="task-1", context_id="ctx-1")
    assert record.state == "input-required"
    assert fake_pipeline.sidecar_status == "backup_blocked"
    assert metrics.task_failed == 0
    assert metrics.backup_blocked == [("input_required", True)]
    assert [event["eventType"] for event in A2APipelineJournal(session_dir).read_all()] == [
        "input_required",
        "backup_blocked",
    ]
    snapshot = A2APipelineSnapshotStore(session_dir).load()
    assert snapshot is not None
    assert snapshot["status"] == "waiting_input"


@pytest.mark.asyncio
async def test_pipeline_backup_blocked_sidecar_persist_failure_is_not_reported_recoverable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    class FailingBackupBlockedSidecarPipeline(FakePipeline):
        def _save_backup_blocked_sidecar(self, step_id, reason):
            return False

    monkeypatch.setenv("IAC_CODE_MODE", "pipeline")
    session_dir = tmp_path / "sidecar"
    fake_pipeline = FailingBackupBlockedSidecarPipeline(
        [
            PipelineEvent(
                type=PipelineEventType.USER_INPUT_REQUIRED,
                step_id="confirm",
                timestamp=1717821601.0,
                data={"prompt": "请选择方案"},
            ),
        ],
        session_dir=session_dir,
    )
    monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_pipeline", lambda *args, **kwargs: fake_pipeline)
    monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_agent_runtime", lambda options: _fake_runtime())
    metrics = SpyMetrics()
    backup_service = RecordingBackupService(block_reasons={BackupReason.INPUT_REQUIRED})

    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    executor = IacCodeA2AExecutor(
        task_store=store,
        model="qwen3.6-plus",
        metrics=metrics,
        backup_service=backup_service,
    )
    queue = FakeEventQueue()

    await executor.execute(FakeRequestContext(metadata={"iac_code": {"cwd": str(tmp_path)}}), queue)

    assert [event["eventType"] for event in _pipeline_status_events(queue)] == []
    record = await store.get_or_create_task(task_id="task-1", context_id="ctx-1")
    assert record.state == "input-required"
    assert fake_pipeline.sidecar_status is None
    assert metrics.backup_blocked == [("input_required", False)]
    events = A2APipelineJournal(session_dir).read_all()
    assert "backup_blocked" not in [event["eventType"] for event in events]
    assert events[-1]["eventType"] == "input_required"
    assert events[-1]["data"]["kind"] == "terminal_publication_unavailable"
    assert events[-1]["data"]["reason"] == "backup_blocked_sidecar_persist_failed"
    snapshot = A2APipelineSnapshotStore(session_dir).load()
    assert snapshot is not None
    assert snapshot["status"] == "waiting_input"
    assert snapshot["pendingInput"]["kind"] == "terminal_publication_unavailable"


@pytest.mark.asyncio
async def test_pipeline_backup_blocked_before_terminal_suppresses_terminal_publication(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("IAC_CODE_MODE", "pipeline")
    session_dir = tmp_path / "sidecar"
    fake_pipeline = FakePipeline(
        [
            PipelineEvent(
                type=PipelineEventType.PIPELINE_COMPLETED,
                step_id=None,
                timestamp=1717821601.0,
                data={"total_steps": 1},
            ),
        ],
        session_dir=session_dir,
    )
    monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_pipeline", lambda *args, **kwargs: fake_pipeline)
    monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_agent_runtime", lambda options: _fake_runtime())
    metrics = SpyMetrics()
    backup_service = RecordingBackupService(
        block_reasons={BackupReason.TERMINAL},
        expected_task_states={BackupReason.TERMINAL: "working"},
    )

    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    executor = IacCodeA2AExecutor(
        task_store=store,
        model="qwen3.6-plus",
        metrics=metrics,
        backup_service=backup_service,
    )
    queue = FakeEventQueue()

    await executor.execute(FakeRequestContext(metadata={"iac_code": {"cwd": str(tmp_path)}}), queue)

    pipeline_events = _pipeline_status_events(queue)
    assert [event["eventType"] for event in pipeline_events] == ["backup_blocked"]
    assert pipeline_events[0]["data"]["reason"] == "terminal"
    assert _status_events(queue)[-1]["status"]["state"] == "TASK_STATE_INPUT_REQUIRED"
    record = await store.get_or_create_task(task_id="task-1", context_id="ctx-1")
    assert record.state == "input-required"
    assert fake_pipeline.sidecar_status == "backup_blocked"
    assert metrics.task_failed == 0
    assert metrics.turn_completed == 0
    assert metrics.executor_error == 1
    assert metrics.backup_blocked == [("terminal", True)]
    journal_events = A2APipelineJournal(session_dir).read_all()
    assert [event["eventType"] for event in journal_events] == [
        "pipeline_completed",
        "pipeline_completed",
        "backup_blocked",
    ]
    terminal_events = [event for event in journal_events if event["eventType"] == "pipeline_completed"]
    assert [event.get("visibility") for event in terminal_events] == ["pending_backup", "committed"]
    snapshot = A2APipelineSnapshotStore(session_dir).load()
    assert snapshot is not None
    assert snapshot["status"] == "waiting_input"
    assert snapshot.get("pendingTerminal") is None


@pytest.mark.asyncio
async def test_pipeline_backup_blocked_publication_requires_durable_metadata(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from iac_code.a2a.pipeline_stream import PipelineA2AEventPublisher

    monkeypatch.setenv("IAC_CODE_MODE", "pipeline")
    fake_pipeline = FakePipeline(
        [
            PipelineEvent(
                type=PipelineEventType.PIPELINE_COMPLETED,
                step_id=None,
                timestamp=1717821601.0,
                data={"total_steps": 1},
            ),
        ],
        session_dir=tmp_path / "sidecar",
    )
    monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_pipeline", lambda *args, **kwargs: fake_pipeline)
    monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_agent_runtime", lambda options: _fake_runtime())
    original_publish_manual = PipelineA2AEventPublisher.publish_manual
    durable_flags: list[bool | None] = []

    async def recording_publish_manual(self, event_type, *args, **kwargs):
        if event_type == "backup_blocked":
            durable_flags.append(kwargs.get("require_durable_metadata"))
        return await original_publish_manual(self, event_type, *args, **kwargs)

    monkeypatch.setattr(PipelineA2AEventPublisher, "publish_manual", recording_publish_manual)
    executor = IacCodeA2AExecutor(
        task_store=A2ATaskStore(metrics=NoOpA2AMetrics()),
        model="qwen3.6-plus",
        backup_service=RecordingBackupService(block_reasons={BackupReason.TERMINAL}),
    )

    await executor.execute(FakeRequestContext(metadata={"iac_code": {"cwd": str(tmp_path)}}), FakeEventQueue())

    assert durable_flags == [True]


@pytest.mark.asyncio
async def test_pipeline_terminal_backup_success_commits_after_pending_publication(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("IAC_CODE_MODE", "pipeline")
    session_dir = tmp_path / "sidecar"
    fake_pipeline = FakePipeline(
        [
            PipelineEvent(
                type=PipelineEventType.PIPELINE_COMPLETED,
                step_id=None,
                timestamp=1717821601.0,
                data={"total_steps": 1},
            ),
        ],
        session_dir=session_dir,
    )
    monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_pipeline", lambda *args, **kwargs: fake_pipeline)
    monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_agent_runtime", lambda options: _fake_runtime())

    def assert_committed_terminal_before_backup(reason: BackupReason) -> None:
        if reason == BackupReason.TERMINAL:
            events = A2APipelineJournal(session_dir).read_all()
            assert [event["eventType"] for event in events] == [
                "pipeline_completed",
                "pipeline_completed",
            ]
            assert [event.get("visibility") for event in events[:2]] == ["pending_backup", "committed"]
            snapshot = A2APipelineSnapshotStore(session_dir).load()
            assert snapshot is not None
            assert snapshot["pendingTerminal"]["eventType"] == "pipeline_completed"

    backup_service = RecordingBackupService(
        expected_task_states={BackupReason.TERMINAL: "working"},
        on_backup=assert_committed_terminal_before_backup,
    )

    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    executor = IacCodeA2AExecutor(task_store=store, model="qwen3.6-plus", backup_service=backup_service)
    queue = FakeEventQueue()

    await executor.execute(FakeRequestContext(metadata={"iac_code": {"cwd": str(tmp_path)}}), queue)

    assert [(reason, critical) for *_ids, reason, critical in backup_service.calls] == [(BackupReason.TERMINAL, True)]
    pipeline_events = _pipeline_status_events(queue)
    assert [event["eventType"] for event in pipeline_events] == ["pipeline_completed", "backup_committed"]
    assert pipeline_events[0]["visibility"] == "committed"
    assert pipeline_events[1]["data"]["committedEventType"] == "pipeline_completed"
    journal_events = A2APipelineJournal(session_dir).read_all()
    assert [event["eventType"] for event in journal_events] == [
        "pipeline_completed",
        "pipeline_completed",
        "backup_committed",
    ]
    assert [event.get("visibility") for event in journal_events[:2]] == ["pending_backup", "committed"]
    assert journal_events[-1]["data"]["committedEventType"] == "pipeline_completed"
    snapshot = A2APipelineSnapshotStore(session_dir).load()
    assert snapshot is not None
    assert snapshot["status"] == "completed"
    assert snapshot.get("pendingTerminal") is None
    record = await store.get_or_create_task(task_id="task-1", context_id="ctx-1")
    assert record.state == "completed"
    context_record = store._contexts["ctx-1"]
    root_session_dir = SessionStorage().session_dir(str(tmp_path), context_record.session_id)
    task_snapshot = json.loads((root_session_dir / "a2a" / "task.json").read_text(encoding="utf-8"))
    context_snapshot = json.loads((root_session_dir / "a2a" / "context.json").read_text(encoding="utf-8"))
    assert task_snapshot["state"] == "completed"
    assert context_snapshot["active_task_id"] is None


@pytest.mark.asyncio
async def test_pipeline_backup_blocked_before_handoff_suppresses_terminal_publication(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("IAC_CODE_MODE", "pipeline")
    session_dir = tmp_path / "sidecar"
    fake_pipeline = FakePipeline(
        [
            PipelineEvent(
                type=PipelineEventType.PIPELINE_COMPLETED,
                step_id=None,
                timestamp=1717821601.0,
                data={"total_steps": 1},
            ),
        ],
        session_dir=session_dir,
    )
    fake_pipeline.handoff_enabled = True
    monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_pipeline", lambda *args, **kwargs: fake_pipeline)
    monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_agent_runtime", lambda options: _fake_runtime())
    metrics = SpyMetrics()
    backup_service = RecordingBackupService(
        block_reasons={BackupReason.HANDOFF_READY},
        expected_task_states={
            BackupReason.TERMINAL: "working",
            BackupReason.HANDOFF_READY: "working",
        },
    )

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
        (BackupReason.TERMINAL, True),
        (BackupReason.HANDOFF_READY, True),
    ]
    pipeline_events = _pipeline_status_events(queue)
    assert [event["eventType"] for event in pipeline_events] == ["backup_blocked"]
    assert pipeline_events[0]["status"] == "input_required"
    assert pipeline_events[0]["data"]["reason"] == "handoff_ready"
    assert _status_events(queue)[-1]["status"]["state"] == "TASK_STATE_INPUT_REQUIRED"
    record = await store.get_or_create_task(task_id="task-1", context_id="ctx-1")
    assert record.state == "input-required"
    assert fake_pipeline.sidecar_status == "backup_blocked"
    assert metrics.task_failed == 0
    assert metrics.turn_completed == 0
    assert metrics.executor_error == 1
    assert metrics.backup_blocked == [("handoff_ready", True)]
    journal_events = A2APipelineJournal(session_dir).read_all()
    assert [event["eventType"] for event in journal_events] == [
        "pipeline_completed",
        "pipeline_handoff_ready",
        "pipeline_completed",
        "pipeline_handoff_ready",
        "backup_blocked",
    ]
    assert [event.get("visibility") for event in journal_events[:4]] == [
        "pending_backup",
        "pending_backup",
        "committed",
        "committed",
    ]
    snapshot = A2APipelineSnapshotStore(session_dir).load()
    assert snapshot is not None
    assert snapshot["status"] == "waiting_input"
    assert snapshot["normalHandoff"] is None
    assert snapshot.get("pendingNormalHandoff") is None


@pytest.mark.asyncio
async def test_pipeline_cancel_handoff_publication_unavailable_keeps_task_nonterminal(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("IAC_CODE_MODE", "pipeline")
    session_dir = tmp_path / "sidecar"
    fake_pipeline = FakePipeline([asyncio.CancelledError()], session_dir=session_dir)
    fake_pipeline.handoff_enabled = True
    monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_pipeline", lambda *args, **kwargs: fake_pipeline)
    monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_agent_runtime", lambda options: _fake_runtime())

    async def block_publication(*args, **kwargs) -> bool:
        return False

    monkeypatch.setattr(
        "iac_code.a2a.pipeline_executor.IacCodeA2APipelineExecutor._backup_before_pipeline_publication",
        block_publication,
    )

    metrics = SpyMetrics()
    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    executor = IacCodeA2AExecutor(
        task_store=store,
        model="qwen3.6-plus",
        metrics=metrics,
    )
    queue = FakeEventQueue()

    await executor.execute(FakeRequestContext(metadata={"iac_code": {"cwd": str(tmp_path)}}), queue)

    assert _pipeline_status_events(queue) == []
    assert _status_events(queue)[-1]["status"]["state"] == "TASK_STATE_INPUT_REQUIRED"
    record = await store.get_or_create_task(task_id="task-1", context_id="ctx-1")
    assert record.state == "input-required"
    assert metrics.task_failed == 0
    journal_events = A2APipelineJournal(session_dir).read_all()
    assert [event["eventType"] for event in journal_events[:5]] == [
        "pipeline_canceled",
        "pipeline_handoff_ready",
        "pipeline_canceled",
        "pipeline_handoff_ready",
        "input_required",
    ]
    assert [event.get("visibility") for event in journal_events[:4]] == [
        "pending_backup",
        "pending_backup",
        "committed",
        "committed",
    ]
    assert any(
        event["eventType"] == "input_required"
        and isinstance(event.get("data"), dict)
        and event["data"].get("kind") == "terminal_publication_unavailable"
        for event in journal_events
    )
    snapshot = A2APipelineSnapshotStore(session_dir).load()
    assert snapshot is not None
    assert snapshot["status"] not in {"completed", "failed", "canceled"}
    assert snapshot["normalHandoff"] is None
    assert await executor._should_route_pipeline_handoff_to_normal(context_id="ctx-1", cwd=str(tmp_path)) is False


@pytest.mark.asyncio
async def test_pipeline_completed_handoff_persist_failure_blocks_terminal_sidecar_recovery(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from iac_code.a2a.pipeline_stream import PipelineA2AEventPublisher

    monkeypatch.setenv("IAC_CODE_MODE", "pipeline")
    session_dir = tmp_path / "sidecar"
    fake_pipeline = TerminalSidecarAfterCompletionPipeline(
        [
            PipelineEvent(
                type=PipelineEventType.PIPELINE_COMPLETED,
                step_id=None,
                timestamp=1717821601.0,
                data={"total_steps": 1},
            ),
        ],
        session_dir=session_dir,
    )
    fake_pipeline.handoff_enabled = True
    monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_pipeline", lambda *args, **kwargs: fake_pipeline)
    monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_agent_runtime", lambda options: _fake_runtime())
    original_persist_envelope = PipelineA2AEventPublisher.persist_envelope

    async def fail_pending_handoff_persist(self, envelope, *args, **kwargs):
        if envelope.get("eventType") == "pipeline_handoff_ready" and envelope.get("visibility") == "pending_backup":
            return None
        return await original_persist_envelope(self, envelope, *args, **kwargs)

    monkeypatch.setattr(PipelineA2AEventPublisher, "persist_envelope", fail_pending_handoff_persist)

    metrics = SpyMetrics()
    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    executor = IacCodeA2AExecutor(
        task_store=store,
        model="qwen3.6-plus",
        metrics=metrics,
    )
    queue = FakeEventQueue()

    await executor.execute(FakeRequestContext(metadata={"iac_code": {"cwd": str(tmp_path)}}), queue)

    assert not any(event["eventType"] == "pipeline_completed" for event in _pipeline_status_events(queue))
    record = await store.get_or_create_task(task_id="task-1", context_id="ctx-1")
    assert record.state not in {"completed", "failed", "canceled"}
    journal_events = A2APipelineJournal(session_dir).read_all()
    assert not any(
        event["eventType"] == "pipeline_completed" and event.get("visibility") == "committed"
        for event in journal_events
    )
    snapshot = A2APipelineSnapshotStore(session_dir).load()
    assert snapshot is not None
    assert snapshot["status"] not in {"completed", "failed", "canceled"}
    assert snapshot["normalHandoff"] is None
    assert await executor._should_route_pipeline_handoff_to_normal(context_id="ctx-1", cwd=str(tmp_path)) is False


@pytest.mark.asyncio
async def test_pipeline_completed_handoff_terminal_enqueue_failure_does_not_commit_available_terminal(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from iac_code.a2a.pipeline_events import PipelineA2AContext, PipelineEventTranslator
    from iac_code.a2a.pipeline_executor import _committed_terminal_status_for_task_context
    from iac_code.a2a.pipeline_stream import PipelineA2AEventPublisher

    monkeypatch.setenv("IAC_CODE_MODE", "pipeline")
    session_dir = tmp_path / "sidecar"
    fake_pipeline = TerminalSidecarAfterCompletionPipeline(
        [
            PipelineEvent(
                type=PipelineEventType.PIPELINE_COMPLETED,
                step_id=None,
                timestamp=1717821601.0,
                data={"total_steps": 1},
            ),
        ],
        session_dir=session_dir,
    )
    fake_pipeline.handoff_enabled = True
    monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_pipeline", lambda *args, **kwargs: fake_pipeline)
    monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_agent_runtime", lambda options: _fake_runtime())
    original_enqueue_persisted = PipelineA2AEventPublisher.enqueue_persisted

    async def fail_committed_terminal_enqueue(self, envelope, *args, **kwargs):
        if envelope.get("eventType") == "pipeline_completed" and envelope.get("visibility") == "committed":
            return False
        return await original_enqueue_persisted(self, envelope, *args, **kwargs)

    monkeypatch.setattr(PipelineA2AEventPublisher, "enqueue_persisted", fail_committed_terminal_enqueue)

    metrics = SpyMetrics()
    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    executor = IacCodeA2AExecutor(
        task_store=store,
        model="qwen3.6-plus",
        metrics=metrics,
    )
    queue = FakeEventQueue()

    await executor.execute(FakeRequestContext(metadata={"iac_code": {"cwd": str(tmp_path)}}), queue)

    record = await store.get_or_create_task(task_id="task-1", context_id="ctx-1")
    assert record.state not in {"completed", "failed", "canceled"}
    snapshot = A2APipelineSnapshotStore(session_dir).load()
    assert snapshot is not None
    assert snapshot["status"] not in {"completed", "failed", "canceled"}
    assert snapshot["normalHandoff"] is None
    assert await executor._should_route_pipeline_handoff_to_normal(context_id="ctx-1", cwd=str(tmp_path)) is False
    context_record = store._contexts["ctx-1"]
    publisher = PipelineA2AEventPublisher(
        event_queue=FakeEventQueue(),
        translator=PipelineEventTranslator(
            PipelineA2AContext(
                pipeline_run_id="ctx-1",
                task_id="task-1",
                context_id="ctx-1",
                pipeline_name="selling",
                iac_code_session_id=context_record.session_id,
            )
        ),
        journal=A2APipelineJournal(session_dir),
        snapshot_store=A2APipelineSnapshotStore(session_dir),
    )
    assert (
        _committed_terminal_status_for_task_context(
            publisher,
            task_id="task-1",
            context_id="ctx-1",
        )
        is None
    )


@pytest.mark.asyncio
async def test_pipeline_completed_handoff_enqueue_failure_keeps_terminal_available(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from iac_code.a2a.pipeline_stream import PipelineA2AEventPublisher

    monkeypatch.setenv("IAC_CODE_MODE", "pipeline")
    session_dir = tmp_path / "sidecar"
    fake_pipeline = TerminalSidecarAfterCompletionPipeline(
        [
            PipelineEvent(
                type=PipelineEventType.PIPELINE_COMPLETED,
                step_id=None,
                timestamp=1717821601.0,
                data={"total_steps": 1},
            ),
        ],
        session_dir=session_dir,
    )
    fake_pipeline.handoff_enabled = True
    monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_pipeline", lambda *args, **kwargs: fake_pipeline)
    monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_agent_runtime", lambda options: _fake_runtime())
    original_enqueue_persisted = PipelineA2AEventPublisher.enqueue_persisted

    async def fail_committed_handoff_enqueue(self, envelope, *args, **kwargs):
        if envelope.get("eventType") == "pipeline_handoff_ready" and envelope.get("visibility") == "committed":
            return False
        return await original_enqueue_persisted(self, envelope, *args, **kwargs)

    monkeypatch.setattr(PipelineA2AEventPublisher, "enqueue_persisted", fail_committed_handoff_enqueue)

    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    executor = IacCodeA2AExecutor(task_store=store, model="qwen3.6-plus")
    queue = FakeEventQueue()

    await executor.execute(FakeRequestContext(metadata={"iac_code": {"cwd": str(tmp_path)}}), queue)

    pipeline_events = _pipeline_status_events(queue)
    assert [event["eventType"] for event in pipeline_events] == ["pipeline_completed", "backup_committed"]
    assert _status_events(queue)[-1]["status"]["state"] == "TASK_STATE_COMPLETED"
    record = await store.get_or_create_task(task_id="task-1", context_id="ctx-1")
    assert record.state == "completed"
    assert await executor._should_route_pipeline_handoff_to_normal(context_id="ctx-1", cwd=str(tmp_path)) is False
    journal_events = A2APipelineJournal(session_dir).read_all()
    unavailable_handoff = [
        event
        for event in journal_events
        if event["eventType"] == "pipeline_handoff_ready"
        and isinstance(event.get("data"), dict)
        and event["data"].get("action") == "switch_to_normal_unavailable"
    ]
    assert len(unavailable_handoff) == 1
    assert unavailable_handoff[0].get("visibility") is None
    snapshot = A2APipelineSnapshotStore(session_dir).load()
    assert snapshot is not None
    assert snapshot["status"] == "completed"
    assert snapshot["normalHandoff"]["action"] == "switch_to_normal_unavailable"
    assert snapshot["pendingNormalHandoff"] is None


@pytest.mark.asyncio
async def test_pipeline_pending_handoff_is_not_routed_to_normal(tmp_path: Path) -> None:
    from iac_code.a2a.pipeline_events import PipelineA2AContext, PipelineEventTranslator

    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    ctx = await store.get_or_create_context(
        context_id="ctx-1",
        cwd=str(tmp_path),
        runtime_factory=lambda _session_id: _fake_runtime(),
    )
    pipeline_dir = SessionStorage().session_dir(str(tmp_path), ctx.session_id) / "a2a" / "pipeline"
    translator = PipelineEventTranslator(
        PipelineA2AContext(
            pipeline_run_id="ctx-1",
            task_id="task-1",
            context_id="ctx-1",
            pipeline_name="selling",
            iac_code_session_id=ctx.session_id,
        )
    )
    terminal = translator.manual_event("pipeline_completed", "pipeline", status="completed", data={"totalSteps": 1})
    handoff = translator.manual_event(
        "pipeline_handoff_ready",
        "pipeline",
        status="completed",
        data={
            "action": "switch_to_normal",
            "targetMode": "normal",
            "outcome": "completed",
            "summary": "[Pipeline Handoff Context]\nPipeline: selling",
        },
    )
    handoff["visibility"] = "pending_backup"
    journal = A2APipelineJournal(pipeline_dir)
    journal.append_many([terminal, handoff], durable=True)
    snapshot_store = A2APipelineSnapshotStore(pipeline_dir)
    snapshot_store.save(reduce_pipeline_events(journal.read_all_repairing_tail()))

    executor = IacCodeA2AExecutor(task_store=store, model="qwen3.6-plus")

    assert await executor._should_route_pipeline_handoff_to_normal(context_id="ctx-1", cwd=str(tmp_path)) is False
    snapshot = snapshot_store.load()
    assert snapshot is not None
    assert snapshot["normalHandoff"] is None
    assert snapshot["pendingNormalHandoff"]["summary"] == "[Pipeline Handoff Context]\nPipeline: selling"


@pytest.mark.asyncio
async def test_committed_handoff_routes_to_normal_after_backup_visibility_commit(tmp_path: Path) -> None:
    from iac_code.a2a.pipeline_events import PipelineA2AContext, PipelineEventTranslator

    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    ctx = await store.get_or_create_context(
        context_id="ctx-1",
        cwd=str(tmp_path),
        runtime_factory=lambda _session_id: _fake_runtime(),
    )
    pipeline_dir = SessionStorage().session_dir(str(tmp_path), ctx.session_id) / "a2a" / "pipeline"
    translator = PipelineEventTranslator(
        PipelineA2AContext(
            pipeline_run_id="ctx-1",
            task_id="task-1",
            context_id="ctx-1",
            pipeline_name="selling",
            iac_code_session_id=ctx.session_id,
        )
    )
    terminal = translator.manual_event("pipeline_completed", "pipeline", status="completed", data={"totalSteps": 1})
    pending = translator.manual_event(
        "pipeline_handoff_ready",
        "pipeline",
        status="completed",
        data={
            "action": "switch_to_normal",
            "targetMode": "normal",
            "outcome": "completed",
            "summary": "[Pipeline Handoff Context]\nPipeline: selling",
        },
    )
    pending["visibility"] = "pending_backup"
    committed = translator.manual_event(
        "pipeline_handoff_ready",
        "pipeline",
        status="completed",
        data={
            "action": "switch_to_normal",
            "targetMode": "normal",
            "outcome": "completed",
            "summary": "[Pipeline Handoff Context]\nPipeline: selling",
        },
    )
    committed["visibility"] = "committed"
    ack = translator.manual_event(
        "backup_committed",
        "pipeline",
        data={
            "committedEventId": committed["eventId"],
            "committedEventType": "pipeline_handoff_ready",
            "committedSequence": committed["sequence"],
        },
    )
    ack.pop("status", None)
    journal = A2APipelineJournal(pipeline_dir)
    journal.append_many([terminal, pending, committed, ack], durable=True)
    snapshot_store = A2APipelineSnapshotStore(pipeline_dir)
    snapshot_store.save(reduce_pipeline_events(journal.read_all_repairing_tail()))

    executor = IacCodeA2AExecutor(task_store=store, model="qwen3.6-plus")

    assert await executor._should_route_pipeline_handoff_to_normal(context_id="ctx-1", cwd=str(tmp_path)) is True
    snapshot = snapshot_store.load()
    assert snapshot is not None
    assert snapshot["pendingNormalHandoff"] is None
    assert snapshot["normalHandoff"]["summary"] == "[Pipeline Handoff Context]\nPipeline: selling"


@pytest.mark.asyncio
async def test_pending_handoff_snapshot_with_committed_backup_ack_repairs_before_normal_route(tmp_path: Path) -> None:
    from iac_code.a2a.pipeline_events import PipelineA2AContext, PipelineEventTranslator

    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    ctx = await store.get_or_create_context(
        context_id="ctx-1",
        cwd=str(tmp_path),
        runtime_factory=lambda _session_id: _fake_runtime(),
    )
    pipeline_dir = SessionStorage().session_dir(str(tmp_path), ctx.session_id) / "a2a" / "pipeline"
    translator = PipelineEventTranslator(
        PipelineA2AContext(
            pipeline_run_id="ctx-1",
            task_id="task-1",
            context_id="ctx-1",
            pipeline_name="selling",
            iac_code_session_id=ctx.session_id,
        )
    )
    started = translator.manual_event("pipeline_started", "pipeline", status="working", data={"totalSteps": 1})
    pending_terminal = translator.manual_event("pipeline_canceled", "pipeline", status="canceled", data={})
    pending_terminal["visibility"] = "pending_backup"
    pending_handoff = translator.manual_event(
        "pipeline_handoff_ready",
        "pipeline",
        status="canceled",
        data={
            "action": "switch_to_normal",
            "targetMode": "normal",
            "outcome": "canceled",
            "summary": "[Pipeline Handoff Context]\nPipeline: selling",
        },
    )
    pending_handoff["visibility"] = "pending_backup"
    committed_terminal = translator.manual_event("pipeline_canceled", "pipeline", status="canceled", data={})
    committed_terminal["visibility"] = "committed"
    committed_handoff = translator.manual_event(
        "pipeline_handoff_ready",
        "pipeline",
        status="canceled",
        data={
            "action": "switch_to_normal",
            "targetMode": "normal",
            "outcome": "canceled",
            "summary": "[Pipeline Handoff Context]\nPipeline: selling",
        },
    )
    committed_handoff["visibility"] = "committed"
    terminal_ack = translator.manual_event(
        "backup_committed",
        "pipeline",
        data={
            "committedEventId": committed_terminal["eventId"],
            "committedEventType": "pipeline_canceled",
            "committedSequence": committed_terminal["sequence"],
        },
    )
    terminal_ack.pop("status", None)
    handoff_ack = translator.manual_event(
        "backup_committed",
        "pipeline",
        data={
            "committedEventId": committed_handoff["eventId"],
            "committedEventType": "pipeline_handoff_ready",
            "committedSequence": committed_handoff["sequence"],
        },
    )
    handoff_ack.pop("status", None)
    journal = A2APipelineJournal(pipeline_dir)
    journal.append_many(
        [
            started,
            pending_terminal,
            pending_handoff,
            committed_terminal,
            committed_handoff,
            terminal_ack,
            handoff_ack,
        ],
        durable=True,
    )
    snapshot_store = A2APipelineSnapshotStore(pipeline_dir)
    snapshot_store.save(reduce_pipeline_events([started, pending_terminal, pending_handoff, terminal_ack, handoff_ack]))
    stale = snapshot_store.load()
    assert stale is not None
    assert stale["lastSequence"] == handoff_ack["sequence"]
    assert stale["normalHandoff"] is None
    assert stale["pendingNormalHandoff"]["summary"] == "[Pipeline Handoff Context]\nPipeline: selling"

    executor = IacCodeA2AExecutor(task_store=store, model="qwen3.6-plus")

    assert await executor._should_route_pipeline_handoff_to_normal(context_id="ctx-1", cwd=str(tmp_path)) is True
    repaired = snapshot_store.load()
    assert repaired is not None
    assert repaired["status"] == "canceled"
    assert repaired["pendingTerminal"] is None
    assert repaired["pendingNormalHandoff"] is None
    assert repaired["normalHandoff"]["summary"] == "[Pipeline Handoff Context]\nPipeline: selling"


@pytest.mark.asyncio
@pytest.mark.parametrize("publish_failure", ["none", "raise"])
async def test_handoff_backup_blocked_publish_failure_does_not_expose_terminal_or_normal_handoff(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    publish_failure: str,
) -> None:
    from iac_code.a2a.pipeline_stream import PipelineA2AEventPublisher

    monkeypatch.setenv("IAC_CODE_MODE", "pipeline")
    session_dir = tmp_path / "sidecar"
    fake_pipeline = FakePipeline(
        [
            PipelineEvent(
                type=PipelineEventType.PIPELINE_COMPLETED,
                step_id=None,
                timestamp=1717821601.0,
                data={"total_steps": 1},
            ),
        ],
        session_dir=session_dir,
    )
    fake_pipeline.handoff_enabled = True
    monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_pipeline", lambda *args, **kwargs: fake_pipeline)
    monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_agent_runtime", lambda options: _fake_runtime())
    original_publish_manual = PipelineA2AEventPublisher.publish_manual

    async def fail_backup_blocked_publish(self, event_type, *args, **kwargs):
        if event_type == "backup_blocked":
            if publish_failure == "raise":
                raise RuntimeError("publish failed")
            return None
        return await original_publish_manual(self, event_type, *args, **kwargs)

    monkeypatch.setattr(PipelineA2AEventPublisher, "publish_manual", fail_backup_blocked_publish)
    metrics = SpyMetrics()
    backup_service = RecordingBackupService(block_reasons={BackupReason.HANDOFF_READY})

    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    executor = IacCodeA2AExecutor(
        task_store=store,
        model="qwen3.6-plus",
        metrics=metrics,
        backup_service=backup_service,
    )
    queue = FakeEventQueue()

    await executor.execute(FakeRequestContext(metadata={"iac_code": {"cwd": str(tmp_path)}}), queue)

    assert _pipeline_status_events(queue) == []
    record = await store.get_or_create_task(task_id="task-1", context_id="ctx-1")
    assert record.state == "input-required"
    assert await executor._should_route_pipeline_handoff_to_normal(context_id="ctx-1", cwd=str(tmp_path)) is False
    snapshot = A2APipelineSnapshotStore(session_dir).load()
    assert snapshot is not None
    assert snapshot["status"] not in {"completed", "failed", "canceled"}
    assert snapshot["normalHandoff"] is None
    assert metrics.backup_blocked == [(BackupReason.HANDOFF_READY.value, False)]
    assert metrics.task_failed == 0


@pytest.mark.asyncio
async def test_executor_candidate_started_includes_steps_from_loaded_sub_pipeline(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("IAC_CODE_MODE", "pipeline")
    fake_pipeline = FakePipeline(
        [
            PipelineEvent(
                type=PipelineEventType.SUB_PIPELINE_STARTED,
                step_id=None,
                timestamp=1717821600.0,
                data={
                    "parent_step_id": "evaluate_candidates",
                    "sub_pipeline_id": "evaluate_candidate_candidate_0",
                    "sub_pipeline_name": "evaluate_candidate",
                    "candidate_index": 0,
                    "candidate_name": "轻量应用服务器方案",
                    "total_steps": 2,
                },
            ),
            PipelineEvent(
                type=PipelineEventType.PIPELINE_COMPLETED,
                step_id=None,
                timestamp=1717821601.0,
                data={},
            ),
        ],
        session_dir=tmp_path / "sidecar",
    )
    fake_pipeline._loaded = SimpleNamespace(
        steps=[
            SimpleNamespace(step_id="intent_parsing"),
            SimpleNamespace(
                step_id="evaluate_candidates",
                step_type="parallel_sub_pipeline",
                sub_pipeline_name="evaluate_candidate",
            ),
            SimpleNamespace(step_id="confirm_and_select"),
        ],
        sub_pipelines={
            "evaluate_candidate": SimpleNamespace(
                steps=[
                    SimpleNamespace(step_id="template_generating"),
                    SimpleNamespace(step_id="cost_estimating"),
                ],
            )
        },
    )
    monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_pipeline", lambda *args, **kwargs: fake_pipeline)
    monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_agent_runtime", lambda options: _fake_runtime())

    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    executor = IacCodeA2AExecutor(task_store=store, model="qwen3.6-plus")
    queue = FakeEventQueue()

    await executor.execute(FakeRequestContext(metadata={"iac_code": {"cwd": str(tmp_path)}}), queue)

    candidate_started = next(
        dump(event)["metadata"]["iac_code"]["pipeline"]
        for event in queue.events
        if isinstance(event, TaskStatusUpdateEvent)
        and dump(event).get("metadata", {}).get("iac_code", {}).get("pipeline", {}).get("eventType")
        == "candidate_started"
    )
    assert [step["id"] for step in candidate_started["candidate"]["steps"]] == [
        "template_generating",
        "cost_estimating",
    ]


@pytest.mark.asyncio
async def test_executor_hydrates_translator_step_attempts_before_resuming_waiting_input(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("IAC_CODE_MODE", "pipeline")
    session_dir = tmp_path / "sidecar"
    waiting_event = {
        "schemaVersion": "1.0",
        "extensionUri": "urn:iac-code:a2a:pipeline-events:v1",
        "eventId": "evt-waiting",
        "sequence": 42,
        "createdAt": "2026-06-11T06:15:55Z",
        "eventType": "input_required",
        "scope": "step",
        "pipelineRunId": "ctx-1",
        "taskId": "task-1",
        "contextId": "ctx-1",
        "pipelineName": "selling",
        "status": "input_required",
        "step": {"id": "confirm_and_select", "runId": "step-confirm_and_select-2", "attempt": 2},
        "data": {"prompt": "请选择要部署的方案："},
    }
    journal = A2APipelineJournal(session_dir)
    journal.append(waiting_event)
    A2APipelineSnapshotStore(session_dir).save(reduce_pipeline_events([waiting_event]))
    fake_pipeline = FakePipeline(
        [
            PipelineEvent(
                type=PipelineEventType.USER_INPUT_RECEIVED,
                step_id="confirm_and_select",
                timestamp=1717821601.0,
                data={"selected_value": "已有VPC下新建VSwitch"},
            ),
            PipelineEvent(
                type=PipelineEventType.PIPELINE_COMPLETED,
                step_id=None,
                timestamp=1717821602.0,
                data={"total_steps": 5},
            ),
        ],
        session_dir=session_dir,
    )
    fake_pipeline.sidecar_status = "waiting_input"
    monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_pipeline", lambda *args, **kwargs: fake_pipeline)
    monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_agent_runtime", lambda options: _fake_runtime())

    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    executor = IacCodeA2AExecutor(task_store=store, model="qwen3.6-plus")
    queue = FakeEventQueue()

    await executor.execute(
        FakeRequestContext(
            task_id="task-1",
            context_id="ctx-1",
            text="已有VPC下新建VSwitch",
            metadata={"iac_code": {"cwd": str(tmp_path)}},
        ),
        queue,
    )

    input_received = next(
        dump(event)["metadata"]["iac_code"]["pipeline"]
        for event in queue.events
        if isinstance(event, TaskStatusUpdateEvent)
        and dump(event).get("metadata", {}).get("iac_code", {}).get("pipeline", {}).get("eventType") == "input_received"
    )
    assert fake_pipeline.resume_prompts == ["已有VPC下新建VSwitch"]
    assert input_received["step"]["runId"] == "step-confirm_and_select-2"
    assert input_received["step"]["attempt"] == 2


@pytest.mark.asyncio
async def test_executor_returns_input_required_for_retryable_stream_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("IAC_CODE_MODE", "pipeline")
    fake_pipeline = FakePipeline([TimeoutError("upstream timed out")], session_dir=tmp_path / "sidecar")
    monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_pipeline", lambda *args, **kwargs: fake_pipeline)
    monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_agent_runtime", lambda options: _fake_runtime())

    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    executor = IacCodeA2AExecutor(task_store=store, model="qwen3.6-plus")
    queue = FakeEventQueue()

    await executor.execute(FakeRequestContext(metadata={"iac_code": {"cwd": str(tmp_path)}}), queue)

    final_status = _status_events(queue)[-1]["status"]
    assert final_status["state"] == "TASK_STATE_INPUT_REQUIRED"
    assert final_status["message"]["parts"][0]["text"] == RETRY_TEXT
    record = await store.get_or_create_task(task_id="task-1", context_id="ctx-1")
    assert record.state == "input-required"


@pytest.mark.asyncio
async def test_executor_returns_input_required_for_retryable_pipeline_creation_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("IAC_CODE_MODE", "pipeline")

    def raise_timeout(*args, **kwargs):
        raise TimeoutError("pipeline setup timed out")

    monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_pipeline", raise_timeout)
    monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_agent_runtime", lambda options: _fake_runtime())

    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    executor = IacCodeA2AExecutor(task_store=store, model="qwen3.6-plus")
    queue = FakeEventQueue()

    await executor.execute(FakeRequestContext(metadata={"iac_code": {"cwd": str(tmp_path)}}), queue)

    final_status = _status_events(queue)[-1]["status"]
    assert final_status["state"] == "TASK_STATE_INPUT_REQUIRED"
    assert final_status["message"]["parts"][0]["text"] == RETRY_TEXT
    record = await store.get_or_create_task(task_id="task-1", context_id="ctx-1")
    assert record.state == "input-required"


@pytest.mark.asyncio
async def test_executor_returns_input_required_for_retryable_runtime_creation_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("IAC_CODE_MODE", "pipeline")

    def raise_timeout(options):
        raise TimeoutError("runtime setup timed out")

    monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_agent_runtime", raise_timeout)

    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    executor = IacCodeA2AExecutor(task_store=store, model="qwen3.6-plus")
    queue = FakeEventQueue()

    await executor.execute(FakeRequestContext(metadata={"iac_code": {"cwd": str(tmp_path)}}), queue)

    final_status = _status_events(queue)[-1]["status"]
    assert final_status["state"] == "TASK_STATE_INPUT_REQUIRED"
    assert final_status["message"]["parts"][0]["text"] == RETRY_TEXT
    record = await store.get_or_create_task(task_id="task-1", context_id="ctx-1")
    assert record.state == "input-required"


@pytest.mark.asyncio
async def test_executor_sanitizes_auth_looking_pipeline_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("IAC_CODE_MODE", "pipeline")
    fake_pipeline = FakePipeline(
        [ValueError("missing API key: secret-internal-detail")],
        session_dir=tmp_path / "sidecar",
    )
    monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_pipeline", lambda *args, **kwargs: fake_pipeline)
    monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_agent_runtime", lambda options: _fake_runtime())

    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    executor = IacCodeA2AExecutor(task_store=store, model="qwen3.6-plus")
    queue = FakeEventQueue()

    await executor.execute(FakeRequestContext(metadata={"iac_code": {"cwd": str(tmp_path)}}), queue)

    final_status = _status_events(queue)[-1]["status"]
    assert final_status["state"] == "TASK_STATE_FAILED"
    assert final_status["message"]["parts"][0]["text"] == AUTH_TEXT


@pytest.mark.asyncio
async def test_executor_persists_pipeline_failed_event_for_nonretryable_stream_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("IAC_CODE_MODE", "pipeline")
    session_dir = tmp_path / "sidecar"
    fake_pipeline = FakePipeline(
        [ValueError("planner crashed INTERNAL_TOKEN=tok-live /tmp/iac-code/work.py")],
        session_dir=session_dir,
    )
    monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_pipeline", lambda *args, **kwargs: fake_pipeline)
    monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_agent_runtime", lambda options: _fake_runtime())

    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    executor = IacCodeA2AExecutor(task_store=store, model="qwen3.6-plus")
    queue = FakeEventQueue()

    await executor.execute(FakeRequestContext(metadata={"iac_code": {"cwd": str(tmp_path)}}), queue)

    events = A2APipelineJournal(session_dir).read_all()
    assert events[-1]["eventType"] == "backup_committed"
    assert events[-1]["data"]["committedEventType"] == "pipeline_failed"
    terminal_event = next(
        event
        for event in reversed(events)
        if event["eventType"] == "pipeline_failed" and event.get("visibility") == "committed"
    )
    assert terminal_event["status"] == "failed"
    assert terminal_event["data"]["errorSummary"] == "ValueError: planner crashed INTERNAL_TOKEN=[REDACTED] [PATH]"
    assert terminal_event["data"]["errorDetails"]["type"] == "ValueError"
    assert terminal_event["data"]["errorDetails"]["errorId"]
    assert terminal_event["data"]["errorDetails"]["traceback"] == "Stack trace omitted from public event; see error_id."
    assert "tok-live" not in json.dumps(terminal_event)
    assert "/tmp/iac-code" not in json.dumps(terminal_event)
    snapshot = A2APipelineSnapshotStore(session_dir).load()
    assert snapshot is not None
    assert snapshot["status"] == "failed"
    assert _status_events(queue)[-1]["status"]["state"] == "TASK_STATE_FAILED"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("sidecar_status", "expected_state", "expected_snapshot_status", "expected_event_type"),
    [
        ("completed", "TASK_STATE_COMPLETED", "completed", "pipeline_completed"),
        ("failed", "TASK_STATE_FAILED", "failed", "pipeline_failed"),
        ("user_aborted", "TASK_STATE_CANCELED", "canceled", "pipeline_canceled"),
        ("canceled", "TASK_STATE_CANCELED", "canceled", "pipeline_canceled"),
    ],
)
async def test_executor_preserves_terminal_sidecar_recovery_state_on_followup(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    sidecar_status: str,
    expected_state: str,
    expected_snapshot_status: str,
    expected_event_type: str,
) -> None:
    monkeypatch.setenv("IAC_CODE_MODE", "pipeline")
    session_dir = tmp_path / "sidecar"
    A2APipelineJournal(session_dir).append(
        {
            "schemaVersion": "1.0",
            "extensionUri": "urn:iac-code:a2a:pipeline-events:v1",
            "eventId": "evt-terminal",
            "sequence": 1,
            "createdAt": "2026-06-08T10:00:00Z",
            "eventType": expected_event_type,
            "scope": "pipeline",
            "pipelineRunId": "ctx-1",
            "taskId": "task-1",
            "contextId": "ctx-1",
            "pipelineName": "selling",
            "status": expected_snapshot_status,
            "data": {"sidecarStatus": sidecar_status},
        }
    )
    fake_pipeline = FakePipeline([], session_dir=session_dir)
    fake_pipeline.sidecar_status = sidecar_status
    monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_pipeline", lambda *args, **kwargs: fake_pipeline)
    monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_agent_runtime", lambda options: _fake_runtime())

    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    executor = IacCodeA2AExecutor(task_store=store, model="qwen3.6-plus")
    queue = FakeEventQueue()

    await executor.execute(
        FakeRequestContext(
            task_id="task-1",
            context_id="ctx-1",
            text="new request",
            metadata={"iac_code": {"cwd": str(tmp_path)}},
        ),
        queue,
    )

    assert fake_pipeline.clear_sidecar_calls == 0
    assert fake_pipeline.run_prompts == []
    assert (session_dir / "a2a-events.jsonl").exists()
    assert _status_events(queue)[-1]["status"]["state"] == expected_state
    snapshot = A2APipelineSnapshotStore(session_dir).load()
    assert snapshot is not None
    assert snapshot["status"] == expected_snapshot_status
    last_event = A2APipelineJournal(session_dir).read_all()[-1]
    assert last_event["eventType"] == expected_event_type
    assert last_event["taskId"] == "task-1"
    record = await store.get_or_create_task(task_id="task-1", context_id="ctx-1")
    assert record.state in {"completed", "failed", "canceled"}


@pytest.mark.asyncio
async def test_executor_clears_previous_task_terminal_sidecar_and_runs_new_task(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("IAC_CODE_MODE", "pipeline")
    session_dir = tmp_path / "sidecar"
    journal = A2APipelineJournal(session_dir)
    previous_terminal = {
        "schemaVersion": "1.0",
        "extensionUri": "urn:iac-code:a2a:pipeline-events:v1",
        "eventId": "evt-old-terminal",
        "sequence": 1,
        "createdAt": "2026-06-08T10:00:00Z",
        "eventType": "pipeline_completed",
        "scope": "pipeline",
        "pipelineRunId": "ctx-1",
        "taskId": "task-old",
        "contextId": "ctx-1",
        "pipelineName": "selling",
        "status": "completed",
        "data": {"sidecarStatus": "completed"},
    }
    journal.append(previous_terminal)
    A2APipelineSnapshotStore(session_dir).save(
        {
            "schemaVersion": "1.0",
            "snapshotVersion": 1,
            "pipelineRunId": "ctx-1",
            "taskId": "task-old",
            "contextId": "ctx-1",
            "pipelineName": "selling",
            "status": "completed",
            "lastSequence": 1,
            "steps": [],
            "display": {"messages": [], "diagrams": [], "candidateDetails": [], "artifacts": []},
            "pendingInput": None,
            "control": {"activeCandidateRunIds": [], "rollbackHistory": [], "candidateRestarts": []},
            "seenEventIds": ["evt-old-terminal"],
        }
    )
    fake_pipeline = FakePipeline(
        [
            TextDeltaEvent(text="new output"),
            PipelineEvent(type=PipelineEventType.PIPELINE_COMPLETED, step_id=None, timestamp=1717821601.0, data={}),
        ],
        session_dir=session_dir,
    )
    fake_pipeline.sidecar_status = "completed"
    monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_pipeline", lambda *args, **kwargs: fake_pipeline)
    monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_agent_runtime", lambda options: _fake_runtime())

    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    executor = IacCodeA2AExecutor(task_store=store, model="qwen3.6-plus")
    queue = FakeEventQueue()

    await executor.execute(
        FakeRequestContext(
            task_id="task-new",
            context_id="ctx-1",
            text="new request",
            metadata={"iac_code": {"cwd": str(tmp_path)}},
        ),
        queue,
    )

    assert fake_pipeline.clear_sidecar_calls == 1
    assert fake_pipeline.run_prompts == ["new request"]
    events = journal.read_all()
    event_types = [event["eventType"] for event in events]
    assert event_types == [
        "pipeline_completed",
        "text_delta",
        "pipeline_completed",
        "pipeline_completed",
        "backup_committed",
    ]
    assert [event.get("visibility") for event in events[-3:-1]] == ["pending_backup", "committed"]
    assert events[-2]["taskId"] == "task-new"
    assert events[-1]["data"]["committedEventType"] == "pipeline_completed"
    assert _status_events(queue)[-1]["status"]["state"] == "TASK_STATE_COMPLETED"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("sidecar_status", "event_type", "event_status"),
    [
        ("completed", "pipeline_completed", "completed"),
    ],
)
async def test_executor_replaces_terminal_restored_pipeline_when_sidecar_owner_mismatches(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    sidecar_status: str,
    event_type: str,
    event_status: str,
) -> None:
    from iac_code.a2a.pipeline_executor import IacCodeA2APipelineExecutor

    monkeypatch.setenv("IAC_CODE_MODE", "pipeline")
    session_dir = tmp_path / "sidecar"
    old_event = {
        "schemaVersion": "1.0",
        "extensionUri": "urn:iac-code:a2a:pipeline-events:v1",
        "eventId": "evt-old-sidecar",
        "sequence": 1,
        "createdAt": "2026-06-08T10:00:00Z",
        "eventType": event_type,
        "scope": "pipeline",
        "pipelineRunId": "ctx-1",
        "taskId": "task-old",
        "contextId": "ctx-1",
        "pipelineName": "selling",
        "status": event_status,
        "data": {"prompt": "old choice"} if event_type == "input_required" else {},
    }
    A2APipelineJournal(session_dir).append(old_event)
    A2APipelineSnapshotStore(session_dir).save(reduce_pipeline_events([old_event]))

    class RestoredMemoryPipeline(FakePipeline):
        async def run(self, prompt: str):
            self.run_prompts.append(prompt)
            yield TextDeltaEvent(text="stale restored output")
            yield PipelineEvent(
                type=PipelineEventType.PIPELINE_COMPLETED,
                step_id=None,
                timestamp=1717821601.0,
                data={},
            )

    restored_pipeline = RestoredMemoryPipeline([], session_dir=session_dir)
    restored_pipeline.sidecar_status = sidecar_status
    restored_pipeline._loaded = SimpleNamespace(
        steps=[],
        sub_pipelines={
            "evaluate_candidate": SimpleNamespace(
                steps=[
                    SimpleNamespace(
                        step_id="template_generating",
                        a2a_artifacts=[{"role": "final", "supersedesPath": "conclusion.file_path"}],
                    ),
                    SimpleNamespace(step_id="cost_estimating", a2a_artifacts=[]),
                ]
            )
        },
    )
    fresh_pipeline = FakePipeline(
        [
            TextDeltaEvent(text="fresh output"),
            PipelineEvent(type=PipelineEventType.PIPELINE_COMPLETED, step_id=None, timestamp=1717821601.0, data={}),
        ],
        session_dir=session_dir,
    )
    fresh_pipeline._loaded = SimpleNamespace(
        steps=[],
        sub_pipelines={
            "evaluate_candidate": SimpleNamespace(
                steps=[
                    SimpleNamespace(
                        step_id="template_generating",
                        a2a_artifacts=[{"role": "intermediate", "supersedesPath": "conclusion.file_path"}],
                    ),
                    SimpleNamespace(
                        step_id="reviewing",
                        a2a_artifacts=[{"role": "final", "supersedesPath": "conclusion.file_path"}],
                    ),
                    SimpleNamespace(step_id="cost_estimating", a2a_artifacts=[]),
                ]
            )
        },
    )
    create_resume_flags: list[bool | None] = []
    publisher_contexts = []

    def fake_create_pipeline(*args, **kwargs):
        create_resume_flags.append(kwargs.get("resume_from_sidecar"))
        return restored_pipeline if len(create_resume_flags) == 1 else fresh_pipeline

    original_publisher = IacCodeA2APipelineExecutor._publisher

    def spy_publisher(self, *args, **kwargs):
        publisher = original_publisher(self, *args, **kwargs)
        publisher_contexts.append(publisher.translator.context)
        return publisher

    monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_pipeline", fake_create_pipeline)
    monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_agent_runtime", lambda options: _fake_runtime())
    monkeypatch.setattr(IacCodeA2APipelineExecutor, "_publisher", spy_publisher)

    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    executor = IacCodeA2AExecutor(task_store=store, model="qwen3.6-plus")

    await executor.execute(
        FakeRequestContext(
            task_id="task-new",
            context_id="ctx-1",
            text="new request",
            metadata={"iac_code": {"cwd": str(tmp_path)}},
        ),
        FakeEventQueue(),
    )

    assert create_resume_flags == [True, False]
    assert restored_pipeline.clear_sidecar_calls == 1
    assert restored_pipeline.run_prompts == []
    assert fresh_pipeline.run_prompts == ["new request"]
    record = await store.get_task_record("task-new")
    assert "".join(record.output_text) == "fresh output"
    assert publisher_contexts[-1].candidate_step_order == [
        "template_generating",
        "reviewing",
        "cost_estimating",
    ]
    assert "reviewing" in publisher_contexts[-1].a2a_artifacts_by_step_id


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("sidecar_status", "event_type", "event_status"),
    [
        ("waiting_input", "input_required", "waiting_input"),
        ("running", "pipeline_started", "working"),
    ],
)
async def test_executor_rejects_active_restored_pipeline_owner_mismatch_without_clearing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    sidecar_status: str,
    event_type: str,
    event_status: str,
) -> None:
    from iac_code.a2a.pipeline_executor import (
        IacCodeA2APipelineExecutor,
        RecoverablePipelineInvalidParamsError,
    )

    monkeypatch.setenv("IAC_CODE_MODE", "pipeline")
    session_dir = tmp_path / "sidecar"
    owner_event = {
        "schemaVersion": "1.0",
        "extensionUri": "urn:iac-code:a2a:pipeline-events:v1",
        "eventId": "evt-owner",
        "sequence": 1,
        "createdAt": "2026-06-08T10:00:00Z",
        "eventType": event_type,
        "scope": "pipeline",
        "pipelineRunId": "ctx-1",
        "taskId": "task-owner",
        "contextId": "ctx-1",
        "pipelineName": "selling",
        "status": event_status,
        "data": {"prompt": "owner choice"} if event_type == "input_required" else {},
    }
    journal = A2APipelineJournal(session_dir)
    journal.append(owner_event)
    A2APipelineSnapshotStore(session_dir).save(reduce_pipeline_events([owner_event]))
    restored_pipeline = FakePipeline(
        [
            TextDeltaEvent(text="stale restored output"),
            PipelineEvent(type=PipelineEventType.PIPELINE_COMPLETED, step_id=None, timestamp=1717821601.0, data={}),
        ],
        session_dir=session_dir,
    )
    restored_pipeline.sidecar_status = sidecar_status
    created_pipelines: list[FakePipeline] = []

    def fake_create_pipeline(*args, **kwargs):
        created_pipelines.append(restored_pipeline)
        return restored_pipeline

    monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_pipeline", fake_create_pipeline)
    monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_agent_runtime", lambda options: _fake_runtime())

    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    executor = IacCodeA2APipelineExecutor(
        task_store=store,
        model="qwen3.6-plus",
        metrics=NoOpA2AMetrics(),
        artifact_store=None,
        push_notifier=None,
        permission_resolver=None,
        auto_approve_permissions=False,
        thinking_exposure_types=None,
    )

    with pytest.raises(RecoverablePipelineInvalidParamsError) as exc_info:
        await executor.execute(
            context=FakeRequestContext(
                task_id="task-new",
                context_id="ctx-1",
                text="new request",
                metadata={"iac_code": {"cwd": str(tmp_path)}},
            ),
            event_queue=FakeEventQueue(),
            task=await store.get_or_create_task(task_id="task-new", context_id="ctx-1"),
            task_id="task-new",
            context_id="ctx-1",
            cwd=str(tmp_path),
            prompt="new request",
        )

    assert exc_info.value.data == {
        "recoverableTaskId": "task-owner",
        "contextId": "ctx-1",
        "sidecarStatus": sidecar_status,
    }
    assert len(created_pipelines) == 1
    assert restored_pipeline.clear_sidecar_calls == 0
    assert restored_pipeline.run_prompts == []
    assert journal.read_all() == [owner_event]


@pytest.mark.asyncio
async def test_executor_keeps_a2a_metadata_when_mismatch_clears_pipeline_sidecar(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("IAC_CODE_MODE", "pipeline")
    session_root = tmp_path / "session"
    sidecar_dir = session_root / "pipeline"
    a2a_dir = session_root / "a2a" / "pipeline"
    sidecar_dir.mkdir(parents=True)
    old_event = {
        "schemaVersion": "1.0",
        "extensionUri": "urn:iac-code:a2a:pipeline-events:v1",
        "eventId": "evt-old-sidecar",
        "sequence": 1,
        "createdAt": "2026-06-08T10:00:00Z",
        "eventType": "pipeline_completed",
        "scope": "pipeline",
        "pipelineRunId": "ctx-1",
        "taskId": "task-old",
        "contextId": "ctx-1",
        "pipelineName": "selling",
        "status": "completed",
        "data": {"sidecarStatus": "completed"},
    }
    A2APipelineJournal(a2a_dir).append(old_event)
    A2APipelineSnapshotStore(a2a_dir).save(reduce_pipeline_events([old_event]))

    class DeletingSidecarPipeline(FakePipeline):
        def clear_sidecar(self) -> None:
            super().clear_sidecar()
            shutil.rmtree(self.session.session_dir, ignore_errors=True)

    restored_pipeline = DeletingSidecarPipeline([], session_dir=sidecar_dir)
    restored_pipeline.sidecar_status = "completed"
    fresh_pipeline = FakePipeline(
        [
            TextDeltaEvent(text="fresh output"),
            PipelineEvent(type=PipelineEventType.PIPELINE_COMPLETED, step_id=None, timestamp=1717821601.0, data={}),
        ],
        session_dir=sidecar_dir,
    )
    create_resume_flags: list[bool | None] = []

    def fake_create_pipeline(*args, **kwargs):
        create_resume_flags.append(kwargs.get("resume_from_sidecar"))
        return restored_pipeline if len(create_resume_flags) == 1 else fresh_pipeline

    monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_pipeline", fake_create_pipeline)
    monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_agent_runtime", lambda options: _fake_runtime())

    executor = IacCodeA2AExecutor(task_store=A2ATaskStore(metrics=NoOpA2AMetrics()), model="qwen3.6-plus")

    await executor.execute(
        FakeRequestContext(
            task_id="task-new",
            context_id="ctx-1",
            text="new request",
            metadata={"iac_code": {"cwd": str(tmp_path)}},
        ),
        FakeEventQueue(),
    )

    assert create_resume_flags == [True, False]
    assert restored_pipeline.clear_sidecar_calls == 1
    events = A2APipelineJournal(a2a_dir).read_all()
    assert [event["taskId"] for event in events] == ["task-old", "task-new", "task-new", "task-new", "task-new"]
    assert [event.get("visibility") for event in events[-3:-1]] == ["pending_backup", "committed"]
    assert events[-1]["eventType"] == "backup_committed"
    assert not (sidecar_dir / "a2a-events.jsonl").exists()


@pytest.mark.asyncio
async def test_executor_does_not_duplicate_existing_terminal_recovery_event_when_snapshot_missing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("IAC_CODE_MODE", "pipeline")
    session_dir = tmp_path / "sidecar"
    journal = A2APipelineJournal(session_dir)
    journal.append(
        {
            "schemaVersion": "1.0",
            "extensionUri": "urn:iac-code:a2a:pipeline-events:v1",
            "eventId": "evt-terminal",
            "sequence": 1,
            "createdAt": "2026-06-08T10:00:00Z",
            "eventType": "pipeline_failed",
            "scope": "pipeline",
            "pipelineRunId": "ctx-1",
            "taskId": "task-1",
            "contextId": "ctx-1",
            "pipelineName": "selling",
            "status": "failed",
            "data": {"sidecarStatus": "failed", "recovered": True},
        }
    )
    fake_pipeline = FakePipeline([], session_dir=session_dir)
    fake_pipeline.sidecar_status = "failed"
    monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_pipeline", lambda *args, **kwargs: fake_pipeline)
    monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_agent_runtime", lambda options: _fake_runtime())

    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    executor = IacCodeA2AExecutor(task_store=store, model="qwen3.6-plus")
    queue = FakeEventQueue()

    await executor.execute(
        FakeRequestContext(
            task_id="task-1",
            context_id="ctx-1",
            text="new request",
            metadata={"iac_code": {"cwd": str(tmp_path)}},
        ),
        queue,
    )

    terminal_events = [event for event in journal.read_all() if event["eventType"] == "pipeline_failed"]
    assert len(terminal_events) == 1
    snapshot = A2APipelineSnapshotStore(session_dir).load()
    assert snapshot is not None
    assert snapshot["status"] == "failed"
    assert _status_events(queue)[-1]["status"]["state"] == "TASK_STATE_FAILED"


@pytest.mark.asyncio
async def test_executor_does_not_publish_conflicting_terminal_sidecar_recovery_event(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("IAC_CODE_MODE", "pipeline")
    session_dir = tmp_path / "sidecar"
    failed_event = {
        "schemaVersion": "1.0",
        "extensionUri": "urn:iac-code:a2a:pipeline-events:v1",
        "eventId": "evt-terminal",
        "sequence": 1,
        "createdAt": "2026-06-08T10:00:00Z",
        "eventType": "pipeline_failed",
        "scope": "pipeline",
        "pipelineRunId": "ctx-1",
        "taskId": "task-1",
        "contextId": "ctx-1",
        "pipelineName": "selling",
        "status": "failed",
        "data": {"source": "executor"},
    }
    journal = A2APipelineJournal(session_dir)
    journal.append(failed_event)
    A2APipelineSnapshotStore(session_dir).save(reduce_pipeline_events([failed_event]))
    fake_pipeline = FakePipeline([], session_dir=session_dir)
    fake_pipeline.sidecar_status = "completed"
    monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_pipeline", lambda *args, **kwargs: fake_pipeline)
    monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_agent_runtime", lambda options: _fake_runtime())

    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    executor = IacCodeA2AExecutor(task_store=store, model="qwen3.6-plus")
    queue = FakeEventQueue()

    await executor.execute(
        FakeRequestContext(
            task_id="task-1",
            context_id="ctx-1",
            text="new request",
            metadata={"iac_code": {"cwd": str(tmp_path)}},
        ),
        queue,
    )

    events = journal.read_all()
    assert [event["eventType"] for event in events] == ["pipeline_failed"]
    snapshot = A2APipelineSnapshotStore(session_dir).load()
    assert snapshot is not None
    assert snapshot["status"] == "failed"
    assert _status_events(queue)[-1]["status"]["state"] == "TASK_STATE_FAILED"


@pytest.mark.asyncio
async def test_executor_rebuilds_stale_snapshot_from_existing_terminal_recovery_event(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("IAC_CODE_MODE", "pipeline")
    session_dir = tmp_path / "sidecar"
    journal = A2APipelineJournal(session_dir)
    working_event = {
        "schemaVersion": "1.0",
        "extensionUri": "urn:iac-code:a2a:pipeline-events:v1",
        "eventId": "evt-working",
        "sequence": 1,
        "createdAt": "2026-06-08T10:00:00Z",
        "eventType": "pipeline_started",
        "scope": "pipeline",
        "pipelineRunId": "ctx-1",
        "taskId": "task-1",
        "contextId": "ctx-1",
        "pipelineName": "selling",
        "status": "working",
        "data": {},
    }
    terminal_event = dict(working_event)
    terminal_event.update(
        {
            "eventId": "evt-terminal",
            "sequence": 2,
            "eventType": "pipeline_failed",
            "status": "failed",
            "data": {"sidecarStatus": "failed", "recovered": True},
        }
    )
    other_context_terminal_event = dict(working_event)
    other_context_terminal_event.update(
        {
            "eventId": "evt-other-terminal",
            "sequence": 99,
            "eventType": "pipeline_completed",
            "pipelineRunId": "ctx-other",
            "taskId": "task-other",
            "contextId": "ctx-other",
            "status": "completed",
            "data": {"sidecarStatus": "completed", "recovered": True},
        }
    )
    journal.append(working_event)
    journal.append(terminal_event)
    journal.append(other_context_terminal_event)
    A2APipelineSnapshotStore(session_dir).save(
        {
            "schemaVersion": "1.0",
            "snapshotVersion": 1,
            "pipelineRunId": "ctx-1",
            "taskId": "task-1",
            "contextId": "ctx-1",
            "pipelineName": "selling",
            "status": "working",
            "lastSequence": 1,
            "steps": [],
            "display": {"messages": [], "diagrams": [], "candidateDetails": [], "artifacts": []},
            "pendingInput": None,
            "control": {"activeCandidateRunIds": [], "rollbackHistory": [], "candidateRestarts": []},
            "seenEventIds": ["evt-working"],
        }
    )
    fake_pipeline = FakePipeline([], session_dir=session_dir)
    fake_pipeline.sidecar_status = "failed"
    monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_pipeline", lambda *args, **kwargs: fake_pipeline)
    monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_agent_runtime", lambda options: _fake_runtime())

    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    executor = IacCodeA2AExecutor(task_store=store, model="qwen3.6-plus")
    queue = FakeEventQueue()

    await executor.execute(
        FakeRequestContext(
            task_id="task-1",
            context_id="ctx-1",
            text="new request",
            metadata={"iac_code": {"cwd": str(tmp_path)}},
        ),
        queue,
    )

    terminal_events = [event for event in journal.read_all() if event["eventType"] == "pipeline_failed"]
    assert len(terminal_events) == 1
    snapshot = A2APipelineSnapshotStore(session_dir).load()
    assert snapshot is not None
    assert snapshot["status"] == "failed"
    assert snapshot["lastSequence"] == 2
    assert snapshot["taskId"] == "task-1"
    assert snapshot["contextId"] == "ctx-1"
    assert _status_events(queue)[-1]["status"]["state"] == "TASK_STATE_FAILED"


@pytest.mark.asyncio
async def test_executor_does_not_rebuild_terminal_snapshot_from_unrepairable_journal(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("IAC_CODE_MODE", "pipeline")
    session_dir = tmp_path / "sidecar"
    working_event = {
        "schemaVersion": "1.0",
        "extensionUri": "urn:iac-code:a2a:pipeline-events:v1",
        "eventId": "evt-working",
        "sequence": 1,
        "createdAt": "2026-06-08T10:00:00Z",
        "eventType": "pipeline_started",
        "scope": "pipeline",
        "pipelineRunId": "ctx-1",
        "taskId": "task-1",
        "contextId": "ctx-1",
        "pipelineName": "selling",
        "status": "working",
        "data": {},
    }
    terminal_event = dict(working_event)
    terminal_event.update(
        {
            "eventId": "evt-terminal",
            "sequence": 3,
            "eventType": "pipeline_failed",
            "status": "failed",
            "data": {"sidecarStatus": "failed", "recovered": True},
        }
    )
    journal = A2APipelineJournal(session_dir)
    journal.append(working_event)
    journal.path.write_text(
        journal.path.read_text(encoding="utf-8")
        + "not-json\n"
        + json.dumps(terminal_event, ensure_ascii=False, separators=(",", ":"))
        + "\n",
        encoding="utf-8",
    )
    A2APipelineSnapshotStore(session_dir).save(reduce_pipeline_events([working_event]))

    class TerminalAfterRunPipeline(FakePipeline):
        async def run(self, prompt: str):
            self.run_prompts.append(prompt)
            self.sidecar_status = "failed"
            if False:
                yield None

    fake_pipeline = TerminalAfterRunPipeline([], session_dir=session_dir)
    monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_pipeline", lambda *args, **kwargs: fake_pipeline)
    monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_agent_runtime", lambda options: _fake_runtime())

    executor = IacCodeA2AExecutor(task_store=A2ATaskStore(metrics=NoOpA2AMetrics()), model="qwen3.6-plus")
    queue = FakeEventQueue()

    await executor.execute(
        FakeRequestContext(
            task_id="task-1",
            context_id="ctx-1",
            text="resume",
            metadata={"iac_code": {"cwd": str(tmp_path)}},
        ),
        queue,
    )

    snapshot = A2APipelineSnapshotStore(session_dir).load()
    assert snapshot is not None
    assert snapshot["status"] == "working"
    assert snapshot["lastSequence"] == 1
    assert _status_events(queue)[-1]["status"]["state"] == "TASK_STATE_INPUT_REQUIRED"


@pytest.mark.asyncio
async def test_executor_repairs_same_task_terminal_sidecar_with_nonterminal_snapshot_without_rerun(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("IAC_CODE_MODE", "pipeline")
    session_dir = tmp_path / "sidecar"
    working_event = {
        "schemaVersion": "1.0",
        "extensionUri": "urn:iac-code:a2a:pipeline-events:v1",
        "eventId": "evt-working",
        "sequence": 1,
        "createdAt": "2026-06-08T10:00:00Z",
        "eventType": "pipeline_started",
        "scope": "pipeline",
        "pipelineRunId": "ctx-1",
        "taskId": "task-1",
        "contextId": "ctx-1",
        "pipelineName": "selling",
        "status": "working",
        "data": {},
    }
    journal = A2APipelineJournal(session_dir)
    journal.append(working_event)
    A2APipelineSnapshotStore(session_dir).save(reduce_pipeline_events([working_event]))
    fake_pipeline = FakePipeline(
        [
            TextDeltaEvent(text="should not rerun"),
            PipelineEvent(type=PipelineEventType.PIPELINE_COMPLETED, step_id=None, timestamp=1717821601.0, data={}),
        ],
        session_dir=session_dir,
    )
    fake_pipeline.sidecar_status = "completed"
    monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_pipeline", lambda *args, **kwargs: fake_pipeline)
    monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_agent_runtime", lambda options: _fake_runtime())

    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    executor = IacCodeA2AExecutor(task_store=store, model="qwen3.6-plus")
    queue = FakeEventQueue()

    await executor.execute(
        FakeRequestContext(
            task_id="task-1",
            context_id="ctx-1",
            text="new request",
            metadata={"iac_code": {"cwd": str(tmp_path)}},
        ),
        queue,
    )

    assert fake_pipeline.clear_sidecar_calls == 0
    assert fake_pipeline.run_prompts == []
    events = journal.read_all()
    assert [event["eventType"] for event in events] == [
        "pipeline_started",
        "pipeline_completed",
        "pipeline_completed",
        "backup_committed",
    ]
    assert [event.get("visibility") for event in events[-3:-1]] == ["pending_backup", "committed"]
    assert events[-2]["data"]["recovered"] is True
    assert events[-1]["data"]["committedEventType"] == "pipeline_completed"
    snapshot = A2APipelineSnapshotStore(session_dir).load()
    assert snapshot is not None
    assert snapshot["status"] == "completed"
    assert _status_events(queue)[-1]["status"]["state"] == "TASK_STATE_COMPLETED"


@pytest.mark.asyncio
async def test_executor_recovers_terminal_snapshot_with_unacknowledged_committed_event(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("IAC_CODE_MODE", "pipeline")
    session_dir = tmp_path / "sidecar"
    pending_event = {
        "schemaVersion": "1.0",
        "extensionUri": "urn:iac-code:a2a:pipeline-events:v1",
        "eventId": "evt-pending-terminal",
        "sequence": 1,
        "createdAt": "2026-06-08T10:00:00Z",
        "eventType": "pipeline_completed",
        "scope": "pipeline",
        "pipelineRunId": "ctx-1",
        "taskId": "task-1",
        "contextId": "ctx-1",
        "pipelineName": "selling",
        "status": "completed",
        "visibility": "pending_backup",
        "data": {},
    }
    committed_event = {
        **pending_event,
        "eventId": "evt-committed-terminal",
        "sequence": 2,
        "visibility": "committed",
    }
    journal = A2APipelineJournal(session_dir)
    journal.append_many([pending_event, committed_event], durable=True)
    A2APipelineSnapshotStore(session_dir).save(reduce_pipeline_events([pending_event, committed_event]))
    fake_pipeline = FakePipeline(
        [
            TextDeltaEvent(text="should not rerun"),
            PipelineEvent(type=PipelineEventType.PIPELINE_COMPLETED, step_id=None, timestamp=1717821601.0, data={}),
        ],
        session_dir=session_dir,
    )
    fake_pipeline.sidecar_status = "completed"
    monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_pipeline", lambda *args, **kwargs: fake_pipeline)
    monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_agent_runtime", lambda options: _fake_runtime())

    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    executor = IacCodeA2AExecutor(task_store=store, model="qwen3.6-plus")
    queue = FakeEventQueue()

    await executor.execute(
        FakeRequestContext(
            task_id="task-1",
            context_id="ctx-1",
            text="new request",
            metadata={"iac_code": {"cwd": str(tmp_path)}},
        ),
        queue,
    )

    assert fake_pipeline.run_prompts == []
    events = journal.read_all()
    assert [event["eventType"] for event in events[-3:]] == [
        "pipeline_completed",
        "pipeline_completed",
        "backup_committed",
    ]
    assert [event.get("visibility") for event in events[-3:-1]] == ["pending_backup", "committed"]
    assert events[-1]["data"]["committedEventType"] == "pipeline_completed"
    assert _status_events(queue)[-1]["status"]["state"] == "TASK_STATE_COMPLETED"


@pytest.mark.asyncio
async def test_executor_keeps_task_nonterminal_when_terminal_sidecar_publication_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("IAC_CODE_MODE", "pipeline")
    session_dir = tmp_path / "sidecar"
    working_event = {
        "schemaVersion": "1.0",
        "extensionUri": "urn:iac-code:a2a:pipeline-events:v1",
        "eventId": "evt-working",
        "sequence": 1,
        "createdAt": "2026-06-08T10:00:00Z",
        "eventType": "pipeline_started",
        "scope": "pipeline",
        "pipelineRunId": "ctx-1",
        "taskId": "task-1",
        "contextId": "ctx-1",
        "pipelineName": "selling",
        "status": "working",
        "data": {},
    }
    journal = A2APipelineJournal(session_dir)
    journal.append(working_event)
    A2APipelineSnapshotStore(session_dir).save(reduce_pipeline_events([working_event]))
    fake_pipeline = FakePipeline([], session_dir=session_dir)
    fake_pipeline.sidecar_status = "completed"
    monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_pipeline", lambda *args, **kwargs: fake_pipeline)
    monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_agent_runtime", lambda options: _fake_runtime())

    async def block_publication(*args, **kwargs) -> bool:
        return False

    monkeypatch.setattr(
        "iac_code.a2a.pipeline_executor.IacCodeA2APipelineExecutor._backup_before_pipeline_publication",
        block_publication,
    )

    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    executor = IacCodeA2AExecutor(task_store=store, model="qwen3.6-plus")
    queue = FakeEventQueue()

    await executor.execute(
        FakeRequestContext(
            task_id="task-1",
            context_id="ctx-1",
            text="new request",
            metadata={"iac_code": {"cwd": str(tmp_path)}},
        ),
        queue,
    )

    events = journal.read_all()
    assert [event["eventType"] for event in events] == [
        "pipeline_started",
        "pipeline_completed",
        "pipeline_completed",
        "input_required",
    ]
    assert [event.get("visibility") for event in events[1:3]] == ["pending_backup", "committed"]
    assert events[-1]["data"]["kind"] == "terminal_publication_unavailable"
    assert A2APipelineSnapshotStore(session_dir).load()["status"] == "waiting_input"
    assert _status_events(queue)[-1]["status"]["state"] == "TASK_STATE_INPUT_REQUIRED"
    record = await store.get_or_create_task(task_id="task-1", context_id="ctx-1")
    assert record.state == "input-required"


@pytest.mark.asyncio
async def test_executor_keeps_exception_task_nonterminal_when_terminal_publication_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("IAC_CODE_MODE", "pipeline")
    session_dir = tmp_path / "sidecar"
    fake_pipeline = FakePipeline([RuntimeError("boom")], session_dir=session_dir)
    monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_pipeline", lambda *args, **kwargs: fake_pipeline)
    monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_agent_runtime", lambda options: _fake_runtime())

    async def block_publication(*args, **kwargs) -> bool:
        return False

    monkeypatch.setattr(
        "iac_code.a2a.pipeline_executor.IacCodeA2APipelineExecutor._backup_before_pipeline_publication",
        block_publication,
    )

    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    executor = IacCodeA2AExecutor(task_store=store, model="qwen3.6-plus")
    queue = FakeEventQueue()

    await executor.execute(
        FakeRequestContext(
            task_id="task-1",
            context_id="ctx-1",
            text="new request",
            metadata={"iac_code": {"cwd": str(tmp_path)}},
        ),
        queue,
    )

    events = A2APipelineJournal(session_dir).read_all()
    assert [event["eventType"] for event in events] == [
        "pipeline_failed",
        "pipeline_failed",
        "input_required",
    ]
    assert [event.get("visibility") for event in events[:2]] == ["pending_backup", "committed"]
    assert events[-1]["data"]["kind"] == "terminal_publication_unavailable"
    assert A2APipelineSnapshotStore(session_dir).load()["status"] == "waiting_input"
    assert _status_events(queue)[-1]["status"]["state"] == "TASK_STATE_INPUT_REQUIRED"
    record = await store.get_or_create_task(task_id="task-1", context_id="ctx-1")
    assert record.state == "input-required"


@pytest.mark.asyncio
async def test_executor_repairs_terminal_sidecar_after_partial_nonterminal_stream(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("IAC_CODE_MODE", "pipeline")
    session_dir = tmp_path / "sidecar"

    class PartialTerminalPipeline(FakePipeline):
        async def run(self, prompt: str):
            self.run_prompts.append(prompt)
            yield TextDeltaEvent(text="partial output")
            self.sidecar_status = "failed"

    fake_pipeline = PartialTerminalPipeline([], session_dir=session_dir)
    monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_pipeline", lambda *args, **kwargs: fake_pipeline)
    monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_agent_runtime", lambda options: _fake_runtime())

    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    executor = IacCodeA2AExecutor(task_store=store, model="qwen3.6-plus")
    queue = FakeEventQueue()

    await executor.execute(FakeRequestContext(metadata={"iac_code": {"cwd": str(tmp_path)}}), queue)

    events = A2APipelineJournal(session_dir).read_all()
    event_types = [event["eventType"] for event in events]
    assert event_types == ["text_delta", "pipeline_failed", "pipeline_failed", "backup_committed"]
    assert [event.get("visibility") for event in events[-3:-1]] == ["pending_backup", "committed"]
    assert events[-1]["data"]["committedEventType"] == "pipeline_failed"
    snapshot = A2APipelineSnapshotStore(session_dir).load()
    assert snapshot is not None
    assert snapshot["status"] == "failed"
    assert _status_events(queue)[-1]["status"]["state"] == "TASK_STATE_FAILED"


@pytest.mark.asyncio
async def test_executor_routes_waiting_sidecar_prompt_to_resume(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("IAC_CODE_MODE", "pipeline")
    fake_pipeline = FakePipeline(
        [
            PipelineEvent(
                type=PipelineEventType.PIPELINE_COMPLETED,
                step_id=None,
                timestamp=1717821601.0,
                data={},
            )
        ],
        session_dir=tmp_path / "sidecar",
    )
    fake_pipeline.sidecar_status = "waiting_input"
    input_event = {
        "schemaVersion": "1.0",
        "extensionUri": "urn:iac-code:a2a:pipeline-events:v1",
        "eventId": "evt-input",
        "sequence": 1,
        "createdAt": "2026-06-08T10:00:00Z",
        "eventType": "input_required",
        "scope": "pipeline",
        "pipelineRunId": "ctx-1",
        "taskId": "task-1",
        "contextId": "ctx-1",
        "pipelineName": "selling",
        "status": "waiting_input",
        "data": {"prompt": "choose"},
    }
    A2APipelineJournal(tmp_path / "sidecar").append(input_event)
    A2APipelineSnapshotStore(tmp_path / "sidecar").save(reduce_pipeline_events([input_event]))
    monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_pipeline", lambda *args, **kwargs: fake_pipeline)
    monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_agent_runtime", lambda options: _fake_runtime())

    executor = IacCodeA2AExecutor(task_store=A2ATaskStore(metrics=NoOpA2AMetrics()), model="qwen3.6-plus")

    await executor.execute(
        FakeRequestContext(text="selected", metadata={"iac_code": {"cwd": str(tmp_path)}}),
        FakeEventQueue(),
    )

    assert fake_pipeline.resume_prompts == ["selected"]
    assert fake_pipeline.run_prompts == []


@pytest.mark.asyncio
async def test_executor_does_not_resume_waiting_sidecar_when_restore_failed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from iac_code.pipeline.engine.session import RestoreResult

    monkeypatch.setenv("IAC_CODE_MODE", "pipeline")
    session_dir = tmp_path / "sidecar"
    fake_pipeline = FakePipeline(
        [
            PipelineEvent(
                type=PipelineEventType.PIPELINE_COMPLETED,
                step_id=None,
                timestamp=1717821601.0,
                data={},
            )
        ],
        session_dir=session_dir,
    )
    fake_pipeline.sidecar_status = "waiting_input"
    fake_pipeline.sidecar_restore_result = RestoreResult(ok=False, status="waiting_input", reason="invalid_context")
    input_event = {
        "schemaVersion": "1.0",
        "extensionUri": "urn:iac-code:a2a:pipeline-events:v1",
        "eventId": "evt-input",
        "sequence": 1,
        "createdAt": "2026-06-08T10:00:00Z",
        "eventType": "input_required",
        "scope": "pipeline",
        "pipelineRunId": "ctx-1",
        "taskId": "task-1",
        "contextId": "ctx-1",
        "pipelineName": "selling",
        "status": "waiting_input",
        "data": {"prompt": "choose"},
    }
    A2APipelineJournal(session_dir).append(input_event)
    A2APipelineSnapshotStore(session_dir).save(reduce_pipeline_events([input_event]))
    monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_pipeline", lambda *args, **kwargs: fake_pipeline)
    monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_agent_runtime", lambda options: _fake_runtime())

    executor = IacCodeA2AExecutor(task_store=A2ATaskStore(metrics=NoOpA2AMetrics()), model="qwen3.6-plus")
    queue = FakeEventQueue()

    await executor.execute(
        FakeRequestContext(text="selected", metadata={"iac_code": {"cwd": str(tmp_path)}}),
        queue,
    )

    assert fake_pipeline.resume_prompts == []
    assert fake_pipeline.run_prompts == []
    assert _status_events(queue)[-1]["status"]["state"] == "TASK_STATE_FAILED"


@pytest.mark.asyncio
async def test_executor_resumes_matching_waiting_sidecar_when_journal_has_partial_tail(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("IAC_CODE_MODE", "pipeline")
    session_dir = tmp_path / "sidecar"
    fake_pipeline = FakePipeline(
        [
            PipelineEvent(
                type=PipelineEventType.PIPELINE_COMPLETED,
                step_id=None,
                timestamp=1717821601.0,
                data={},
            )
        ],
        session_dir=session_dir,
    )
    fake_pipeline.sidecar_status = "waiting_input"
    input_event = {
        "schemaVersion": "1.0",
        "extensionUri": "urn:iac-code:a2a:pipeline-events:v1",
        "eventId": "evt-input",
        "sequence": 1,
        "createdAt": "2026-06-08T10:00:00Z",
        "eventType": "input_required",
        "scope": "pipeline",
        "pipelineRunId": "ctx-1",
        "taskId": "task-1",
        "contextId": "ctx-1",
        "pipelineName": "selling",
        "status": "waiting_input",
        "data": {"prompt": "choose"},
    }
    journal = A2APipelineJournal(session_dir)
    journal.append(input_event)
    journal.path.write_text(journal.path.read_text(encoding="utf-8") + '{"eventId":"evt-partial"', encoding="utf-8")
    A2APipelineSnapshotStore(session_dir).save(reduce_pipeline_events([input_event]))
    monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_pipeline", lambda *args, **kwargs: fake_pipeline)
    monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_agent_runtime", lambda options: _fake_runtime())

    executor = IacCodeA2AExecutor(task_store=A2ATaskStore(metrics=NoOpA2AMetrics()), model="qwen3.6-plus")

    await executor.execute(
        FakeRequestContext(text="selected", metadata={"iac_code": {"cwd": str(tmp_path)}}),
        FakeEventQueue(),
    )

    assert fake_pipeline.clear_sidecar_calls == 0
    assert fake_pipeline.resume_prompts == ["selected"]
    assert fake_pipeline.run_prompts == []


@pytest.mark.asyncio
async def test_executor_does_not_trust_snapshot_owner_when_journal_has_middle_corruption(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("IAC_CODE_MODE", "pipeline")
    session_dir = tmp_path / "sidecar"
    fake_pipeline = FakePipeline(
        [
            PipelineEvent(
                type=PipelineEventType.PIPELINE_COMPLETED,
                step_id=None,
                timestamp=1717821601.0,
                data={},
            )
        ],
        session_dir=session_dir,
    )
    fake_pipeline.sidecar_status = "waiting_input"
    old_input = {
        "schemaVersion": "1.0",
        "extensionUri": "urn:iac-code:a2a:pipeline-events:v1",
        "eventId": "evt-old-input",
        "sequence": 1,
        "createdAt": "2026-06-08T10:00:00Z",
        "eventType": "input_required",
        "scope": "pipeline",
        "pipelineRunId": "ctx-1",
        "taskId": "task-old",
        "contextId": "ctx-1",
        "pipelineName": "selling",
        "status": "waiting_input",
        "data": {"prompt": "old choice"},
    }
    new_event = dict(old_input)
    new_event.update(
        {
            "eventId": "evt-new",
            "sequence": 3,
            "taskId": "task-new",
            "status": "working",
            "eventType": "pipeline_started",
            "data": {},
        }
    )
    journal = A2APipelineJournal(session_dir)
    journal.append(old_input)
    journal.path.write_text(
        journal.path.read_text(encoding="utf-8")
        + "not-json\n"
        + json.dumps(new_event, ensure_ascii=False, separators=(",", ":"))
        + "\n",
        encoding="utf-8",
    )
    A2APipelineSnapshotStore(session_dir).save(reduce_pipeline_events([old_input]))
    monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_pipeline", lambda *args, **kwargs: fake_pipeline)
    monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_agent_runtime", lambda options: _fake_runtime())

    executor = IacCodeA2AExecutor(task_store=A2ATaskStore(metrics=NoOpA2AMetrics()), model="qwen3.6-plus")
    queue = FakeEventQueue()

    await executor.execute(
        FakeRequestContext(
            task_id="task-old",
            context_id="ctx-1",
            text="old followup",
            metadata={"iac_code": {"cwd": str(tmp_path)}},
        ),
        queue,
    )

    assert fake_pipeline.clear_sidecar_calls == 0
    assert fake_pipeline.resume_prompts == []
    assert fake_pipeline.run_prompts == []
    assert _status_events(queue)[-1]["status"]["state"] == "TASK_STATE_INPUT_REQUIRED"


@pytest.mark.asyncio
@pytest.mark.parametrize("sidecar_status", ["waiting_input", "running"])
@pytest.mark.parametrize(
    ("terminal_event_type", "terminal_status", "expected_state"),
    [
        ("pipeline_completed", "completed", "TASK_STATE_COMPLETED"),
        ("pipeline_failed", "failed", "TASK_STATE_FAILED"),
        ("pipeline_canceled", "canceled", "TASK_STATE_CANCELED"),
    ],
)
async def test_executor_does_not_resume_nonterminal_sidecar_when_a2a_state_is_terminal(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    sidecar_status: str,
    terminal_event_type: str,
    terminal_status: str,
    expected_state: str,
) -> None:
    monkeypatch.setenv("IAC_CODE_MODE", "pipeline")
    session_dir = tmp_path / "sidecar"
    terminal_event = {
        "schemaVersion": "1.0",
        "extensionUri": "urn:iac-code:a2a:pipeline-events:v1",
        "eventId": "evt-terminal",
        "sequence": 1,
        "createdAt": "2026-06-08T10:00:00Z",
        "eventType": terminal_event_type,
        "scope": "pipeline",
        "pipelineRunId": "ctx-1",
        "taskId": "task-1",
        "contextId": "ctx-1",
        "pipelineName": "selling",
        "status": terminal_status,
        "data": {},
    }
    A2APipelineJournal(session_dir).append(terminal_event)
    A2APipelineSnapshotStore(session_dir).save(reduce_pipeline_events([terminal_event]))
    fake_pipeline = FakePipeline(
        [
            TextDeltaEvent(text="fresh output"),
            PipelineEvent(type=PipelineEventType.PIPELINE_COMPLETED, step_id=None, timestamp=1717821601.0, data={}),
        ],
        session_dir=session_dir,
    )
    fake_pipeline.sidecar_status = sidecar_status
    monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_pipeline", lambda *args, **kwargs: fake_pipeline)
    monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_agent_runtime", lambda options: _fake_runtime())

    executor = IacCodeA2AExecutor(task_store=A2ATaskStore(metrics=NoOpA2AMetrics()), model="qwen3.6-plus")
    queue = FakeEventQueue()

    await executor.execute(
        FakeRequestContext(
            task_id="task-1",
            context_id="ctx-1",
            text="retry",
            metadata={"iac_code": {"cwd": str(tmp_path)}},
        ),
        queue,
    )

    assert fake_pipeline.clear_sidecar_calls == 0
    assert fake_pipeline.resume_prompts == []
    assert fake_pipeline.continue_calls == 0
    assert fake_pipeline.run_prompts == []
    assert _status_events(queue)[-1]["status"]["state"] == expected_state
    events = A2APipelineJournal(session_dir).read_all()
    assert [event["eventType"] for event in events] == [terminal_event_type]
    snapshot = A2APipelineSnapshotStore(session_dir).load()
    assert snapshot is not None
    assert snapshot["status"] == terminal_status


@pytest.mark.asyncio
async def test_executor_rejects_previous_task_waiting_sidecar_without_starting_new_task(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from iac_code.a2a.pipeline_executor import RecoverablePipelineInvalidParamsError

    monkeypatch.setenv("IAC_CODE_MODE", "pipeline")
    session_dir = tmp_path / "sidecar"
    old_input = {
        "schemaVersion": "1.0",
        "extensionUri": "urn:iac-code:a2a:pipeline-events:v1",
        "eventId": "evt-old-input",
        "sequence": 1,
        "createdAt": "2026-06-08T10:00:00Z",
        "eventType": "input_required",
        "scope": "pipeline",
        "pipelineRunId": "ctx-1",
        "taskId": "task-old",
        "contextId": "ctx-1",
        "pipelineName": "selling",
        "status": "waiting_input",
        "data": {"prompt": "old choice"},
    }
    A2APipelineJournal(session_dir).append(old_input)
    A2APipelineSnapshotStore(session_dir).save(reduce_pipeline_events([old_input]))
    fake_pipeline = FakePipeline(
        [
            TextDeltaEvent(text="new output"),
            PipelineEvent(type=PipelineEventType.PIPELINE_COMPLETED, step_id=None, timestamp=1717821601.0, data={}),
        ],
        session_dir=session_dir,
    )
    fake_pipeline.sidecar_status = "waiting_input"
    monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_pipeline", lambda *args, **kwargs: fake_pipeline)
    monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_agent_runtime", lambda options: _fake_runtime())

    executor = IacCodeA2AExecutor(task_store=A2ATaskStore(metrics=NoOpA2AMetrics()), model="qwen3.6-plus")

    with pytest.raises(RecoverablePipelineInvalidParamsError) as exc_info:
        await executor.execute(
            FakeRequestContext(
                task_id="task-new",
                context_id="ctx-1",
                text="new request",
                metadata={"iac_code": {"cwd": str(tmp_path)}},
            ),
            FakeEventQueue(),
        )

    assert exc_info.value.data == {
        "recoverableTaskId": "task-old",
        "contextId": "ctx-1",
        "sidecarStatus": "waiting_input",
    }
    assert fake_pipeline.clear_sidecar_calls == 0
    assert fake_pipeline.resume_prompts == []
    assert fake_pipeline.run_prompts == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("sidecar_status", "event_type", "event_status"),
    [
        ("waiting_input", "input_required", "waiting_input"),
        ("running", "pipeline_started", "working"),
    ],
)
async def test_executor_does_not_attach_current_sidecar_to_historical_task(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    sidecar_status: str,
    event_type: str,
    event_status: str,
) -> None:
    from iac_code.a2a.pipeline_executor import RecoverablePipelineInvalidParamsError

    monkeypatch.setenv("IAC_CODE_MODE", "pipeline")
    session_dir = tmp_path / "sidecar"
    old_event = {
        "schemaVersion": "1.0",
        "extensionUri": "urn:iac-code:a2a:pipeline-events:v1",
        "eventId": "evt-old",
        "sequence": 1,
        "createdAt": "2026-06-08T10:00:00Z",
        "eventType": event_type,
        "scope": "pipeline",
        "pipelineRunId": "ctx-1",
        "taskId": "task-old",
        "contextId": "ctx-1",
        "pipelineName": "selling",
        "status": event_status,
        "data": {"prompt": "old choice"} if event_type == "input_required" else {},
    }
    current_event = dict(old_event)
    current_event.update(
        {
            "eventId": "evt-current",
            "sequence": 2,
            "taskId": "task-current",
            "status": event_status,
            "data": {"prompt": "current choice"} if event_type == "input_required" else {},
        }
    )
    journal = A2APipelineJournal(session_dir)
    journal.append(old_event)
    journal.append(current_event)
    A2APipelineSnapshotStore(session_dir).save(reduce_pipeline_events([old_event, current_event]))
    fake_pipeline = FakePipeline(
        [
            TextDeltaEvent(text="old followup output"),
            PipelineEvent(type=PipelineEventType.PIPELINE_COMPLETED, step_id=None, timestamp=1717821601.0, data={}),
        ],
        session_dir=session_dir,
    )
    fake_pipeline.sidecar_status = sidecar_status
    monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_pipeline", lambda *args, **kwargs: fake_pipeline)
    monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_agent_runtime", lambda options: _fake_runtime())

    executor = IacCodeA2AExecutor(task_store=A2ATaskStore(metrics=NoOpA2AMetrics()), model="qwen3.6-plus")

    with pytest.raises(RecoverablePipelineInvalidParamsError) as exc_info:
        await executor.execute(
            FakeRequestContext(
                task_id="task-old",
                context_id="ctx-1",
                text="old followup",
                metadata={"iac_code": {"cwd": str(tmp_path)}},
            ),
            FakeEventQueue(),
        )

    assert exc_info.value.data == {
        "recoverableTaskId": "task-current",
        "contextId": "ctx-1",
        "sidecarStatus": sidecar_status,
    }
    assert fake_pipeline.clear_sidecar_calls == 0
    assert fake_pipeline.resume_prompts == []
    assert fake_pipeline.continue_calls == 0
    assert fake_pipeline.run_prompts == []
    assert journal.read_all()[-1]["taskId"] == "task-current"


@pytest.mark.asyncio
async def test_executor_routes_running_sidecar_to_continue(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("IAC_CODE_MODE", "pipeline")
    fake_pipeline = FakePipeline(
        [
            PipelineEvent(
                type=PipelineEventType.PIPELINE_COMPLETED,
                step_id=None,
                timestamp=1717821601.0,
                data={},
            )
        ],
        session_dir=tmp_path / "sidecar",
    )
    fake_pipeline.sidecar_status = "running"
    running_event = {
        "schemaVersion": "1.0",
        "extensionUri": "urn:iac-code:a2a:pipeline-events:v1",
        "eventId": "evt-running",
        "sequence": 1,
        "createdAt": "2026-06-08T10:00:00Z",
        "eventType": "pipeline_started",
        "scope": "pipeline",
        "pipelineRunId": "ctx-1",
        "taskId": "task-1",
        "contextId": "ctx-1",
        "pipelineName": "selling",
        "status": "working",
        "data": {},
    }
    A2APipelineJournal(tmp_path / "sidecar").append(running_event)
    A2APipelineSnapshotStore(tmp_path / "sidecar").save(reduce_pipeline_events([running_event]))
    monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_pipeline", lambda *args, **kwargs: fake_pipeline)
    monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_agent_runtime", lambda options: _fake_runtime())

    executor = IacCodeA2AExecutor(task_store=A2ATaskStore(metrics=NoOpA2AMetrics()), model="qwen3.6-plus")

    await executor.execute(
        FakeRequestContext(text="not fresh input", metadata={"iac_code": {"cwd": str(tmp_path)}}),
        FakeEventQueue(),
    )

    assert fake_pipeline.continue_calls == 1
    assert fake_pipeline.continue_inputs == ["not fresh input"]
    assert fake_pipeline.run_prompts == ["not fresh input"]


@pytest.mark.asyncio
async def test_executor_preserves_running_sidecar_pause_as_input_required(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("IAC_CODE_MODE", "pipeline")
    pause_event = PipelineEvent(
        type=PipelineEventType.USER_INPUT_REQUIRED,
        step_id="deploying",
        timestamp=1717821601.0,
        data={
            "kind": "pipeline_pause_confirmation",
            "prompt": "Pipeline paused.",
            "reason": "judge failed: timeout after 90.0s",
            "paused": True,
            "options": [],
        },
    )
    fake_pipeline = FakePipeline([pause_event], session_dir=tmp_path / "sidecar")
    fake_pipeline.sidecar_status = "running"
    running_event = {
        "schemaVersion": "1.0",
        "extensionUri": "urn:iac-code:a2a:pipeline-events:v1",
        "eventId": "evt-running",
        "sequence": 1,
        "createdAt": "2026-06-08T10:00:00Z",
        "eventType": "pipeline_started",
        "scope": "pipeline",
        "pipelineRunId": "ctx-1",
        "taskId": "task-1",
        "contextId": "ctx-1",
        "pipelineName": "selling",
        "status": "working",
        "data": {},
    }
    A2APipelineJournal(tmp_path / "sidecar").append(running_event)
    A2APipelineSnapshotStore(tmp_path / "sidecar").save(reduce_pipeline_events([running_event]))
    monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_pipeline", lambda *args, **kwargs: fake_pipeline)
    monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_agent_runtime", lambda options: _fake_runtime())

    queue = FakeEventQueue()
    executor = IacCodeA2AExecutor(task_store=A2ATaskStore(metrics=NoOpA2AMetrics()), model="qwen3.6-plus")

    await executor.execute(
        FakeRequestContext(text="stop deploying", metadata={"iac_code": {"cwd": str(tmp_path)}}),
        queue,
    )

    assert fake_pipeline.continue_calls == 1
    events = A2APipelineJournal(tmp_path / "sidecar").read_all()
    assert events[-1]["eventType"] == "input_required"
    assert events[-1]["status"] == "input_required"
    assert events[-1]["data"]["kind"] == "pipeline_pause_confirmation"
    assert "timeout" in events[-1]["data"]["reason"]
    statuses = _status_events(queue)
    assert statuses[-1]["status"]["state"] == "TASK_STATE_INPUT_REQUIRED"


@pytest.mark.asyncio
async def test_executor_routes_waiting_input_pause_confirmation_through_interrupt_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("IAC_CODE_MODE", "pipeline")
    session_dir = tmp_path / "sidecar"
    pending = {
        "schemaVersion": "1.0",
        "extensionUri": "urn:iac-code:a2a:pipeline-events:v1",
        "eventId": "evt-pause",
        "sequence": 1,
        "createdAt": "2026-06-08T10:00:00Z",
        "eventType": "input_required",
        "scope": "step",
        "pipelineRunId": "ctx-1",
        "taskId": "task-1",
        "contextId": "ctx-1",
        "pipelineName": "selling",
        "status": "input_required",
        "step": {"runId": "step-deploying-1", "id": "deploying", "attempt": 1},
        "data": {
            "kind": "pipeline_pause_confirmation",
            "prompt": "Pipeline paused.",
            "reason": "judge failed: timeout",
            "paused": True,
            "options": [],
        },
    }
    A2APipelineJournal(session_dir).append(pending)
    A2APipelineSnapshotStore(session_dir).save(reduce_pipeline_events([pending]))
    fake_pipeline = FakePipeline(
        [
            PipelineEvent(
                type=PipelineEventType.PIPELINE_COMPLETED,
                step_id=None,
                timestamp=1717821601.0,
                data={},
            )
        ],
        session_dir=session_dir,
    )
    fake_pipeline.sidecar_status = "waiting_input"
    monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_pipeline", lambda *args, **kwargs: fake_pipeline)
    monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_agent_runtime", lambda options: _fake_runtime())

    executor = IacCodeA2AExecutor(task_store=A2ATaskStore(metrics=NoOpA2AMetrics()), model="qwen3.6-plus")
    await executor.execute(
        FakeRequestContext(text="rollback to design", metadata={"iac_code": {"cwd": str(tmp_path)}}),
        FakeEventQueue(),
    )

    assert fake_pipeline.continue_inputs == ["rollback to design"]
    assert fake_pipeline.resume_prompts == []


@pytest.mark.asyncio
async def test_executor_rejects_previous_task_running_sidecar_without_starting_new_task(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from iac_code.a2a.pipeline_executor import RecoverablePipelineInvalidParamsError

    monkeypatch.setenv("IAC_CODE_MODE", "pipeline")
    session_dir = tmp_path / "sidecar"
    old_running = {
        "schemaVersion": "1.0",
        "extensionUri": "urn:iac-code:a2a:pipeline-events:v1",
        "eventId": "evt-old-running",
        "sequence": 1,
        "createdAt": "2026-06-08T10:00:00Z",
        "eventType": "pipeline_started",
        "scope": "pipeline",
        "pipelineRunId": "ctx-1",
        "taskId": "task-old",
        "contextId": "ctx-1",
        "pipelineName": "selling",
        "status": "working",
        "data": {},
    }
    A2APipelineJournal(session_dir).append(old_running)
    A2APipelineSnapshotStore(session_dir).save(reduce_pipeline_events([old_running]))
    fake_pipeline = FakePipeline(
        [
            TextDeltaEvent(text="new output"),
            PipelineEvent(type=PipelineEventType.PIPELINE_COMPLETED, step_id=None, timestamp=1717821601.0, data={}),
        ],
        session_dir=session_dir,
    )
    fake_pipeline.sidecar_status = "running"
    monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_pipeline", lambda *args, **kwargs: fake_pipeline)
    monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_agent_runtime", lambda options: _fake_runtime())

    executor = IacCodeA2AExecutor(task_store=A2ATaskStore(metrics=NoOpA2AMetrics()), model="qwen3.6-plus")

    with pytest.raises(RecoverablePipelineInvalidParamsError) as exc_info:
        await executor.execute(
            FakeRequestContext(
                task_id="task-new",
                context_id="ctx-1",
                text="new request",
                metadata={"iac_code": {"cwd": str(tmp_path)}},
            ),
            FakeEventQueue(),
        )

    assert exc_info.value.data == {
        "recoverableTaskId": "task-old",
        "contextId": "ctx-1",
        "sidecarStatus": "running",
    }
    assert fake_pipeline.clear_sidecar_calls == 0
    assert fake_pipeline.continue_calls == 0
    assert fake_pipeline.run_prompts == []


@pytest.mark.asyncio
async def test_executor_rejected_active_sidecar_mismatch_does_not_persist_new_working_task(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from iac_code.a2a.pipeline_executor import RecoverablePipelineInvalidParamsError

    monkeypatch.setenv("IAC_CODE_MODE", "pipeline")
    persistence = A2APersistenceStore(tmp_path / "a2a")
    session_dir = tmp_path / "sidecar"
    owner_event = {
        "schemaVersion": "1.0",
        "extensionUri": "urn:iac-code:a2a:pipeline-events:v1",
        "eventId": "evt-owner-running",
        "sequence": 1,
        "createdAt": "2026-06-08T10:00:00Z",
        "eventType": "pipeline_started",
        "scope": "pipeline",
        "pipelineRunId": "ctx-1",
        "taskId": "task-owner",
        "contextId": "ctx-1",
        "pipelineName": "selling",
        "status": "working",
        "data": {},
    }
    A2APipelineJournal(session_dir).append(owner_event)
    A2APipelineSnapshotStore(session_dir).save(reduce_pipeline_events([owner_event]))
    fake_pipeline = FakePipeline(
        [
            TextDeltaEvent(text="new output"),
            PipelineEvent(type=PipelineEventType.PIPELINE_COMPLETED, step_id=None, timestamp=1717821601.0, data={}),
        ],
        session_dir=session_dir,
    )
    fake_pipeline.sidecar_status = "running"
    monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_pipeline", lambda *args, **kwargs: fake_pipeline)
    monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_agent_runtime", lambda options: _fake_runtime())

    task_store = A2ATaskStore(metrics=NoOpA2AMetrics(), persistence=persistence)
    executor = IacCodeA2AExecutor(task_store=task_store, model="qwen3.6-plus")

    with pytest.raises(RecoverablePipelineInvalidParamsError):
        await executor.execute(
            FakeRequestContext(
                task_id="task-new",
                context_id="ctx-1",
                text="new request",
                metadata={"iac_code": {"cwd": str(tmp_path)}},
            ),
            FakeEventQueue(),
        )

    assert fake_pipeline.clear_sidecar_calls == 0
    assert fake_pipeline.run_prompts == []
    rejected_task = persistence.load_task("task-new")
    assert rejected_task is not None
    assert rejected_task.state != "working"
    assert [task.task_id for task in persistence.list_tasks() if task.state == "working"] == []


def test_cleanup_handoff_missing_ledger_ignores_empty_public_cleanup_snapshot(tmp_path: Path) -> None:
    from iac_code.a2a.pipeline_executor import _pipeline_cleanup_handoff_data_from_session

    cleanup = _pipeline_cleanup_handoff_data_from_session(
        cwd=str(tmp_path),
        session_id="session-empty-cleanup",
        public_snapshot={"cleanup": {"resourceCount": 0, "resources": [], "status": ""}},
    )

    assert cleanup is None


def test_cleanup_handoff_missing_ledger_does_not_reconstruct_prompt_from_public_snapshot(tmp_path: Path) -> None:
    from iac_code.a2a.pipeline_executor import _pipeline_cleanup_handoff_data_from_session

    cleanup = _pipeline_cleanup_handoff_data_from_session(
        cwd=str(tmp_path),
        session_id="session-public-cleanup-only",
        public_snapshot={
            "cleanup": {
                "resourceCount": 1,
                "resources": [
                    {
                        "provider": "ros",
                        "resourceType": "stack",
                        "resourceId": "stack-public-only",
                        "cleanupStatus": "pending",
                    }
                ],
                "status": "pending",
            }
        },
    )

    assert cleanup is not None
    assert cleanup["status"] == "unavailable"
    assert "prompt" not in cleanup
    assert "resources" not in cleanup
    assert "stack-public-only" not in repr(cleanup)


@pytest.mark.asyncio
async def test_pipeline_executor_routes_second_prompt_as_interrupt(tmp_path: Path) -> None:
    from iac_code.a2a.pipeline_events import PipelineA2AContext, PipelineEventTranslator
    from iac_code.a2a.pipeline_executor import A2APipelineRuntime, IacCodeA2APipelineExecutor
    from iac_code.a2a.pipeline_journal import A2APipelineJournal
    from iac_code.a2a.pipeline_snapshot import A2APipelineSnapshotStore
    from iac_code.a2a.pipeline_stream import PipelineA2AEventPublisher
    from iac_code.services.providers.aliyun import AliyunCredential, AliyunCredentials

    captured_interrupt_credentials: list[str | None] = []

    class InterruptiblePipeline(FakePipeline):
        def __init__(self, *, session_dir: Path) -> None:
            super().__init__([TextDeltaEvent(text="running")], session_dir=session_dir)
            self.interrupts: list[str] = []

        async def handle_user_interrupt(self, message: str) -> SimpleNamespace:
            credential = AliyunCredentials.load()
            captured_interrupt_credentials.append(credential.access_key_id if credential else None)
            self.interrupts.append(message)
            return SimpleNamespace(
                action="supplement",
                reason="added context",
                rollback_target=None,
                candidate_scope=None,
            )

    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    task = await store.get_or_create_task(task_id="task-1", context_id="ctx-1")
    ctx = await store.get_or_create_context(
        context_id="ctx-1",
        cwd=str(tmp_path),
        runtime_factory=lambda _session_id: _fake_runtime(),
    )
    ctx.active_task_id = "task-1"

    queue = FakeEventQueue()
    pipeline = InterruptiblePipeline(session_dir=tmp_path / "sidecar")
    publisher = PipelineA2AEventPublisher(
        event_queue=queue,
        translator=PipelineEventTranslator(
            PipelineA2AContext(
                pipeline_run_id="ctx-1",
                task_id="task-1",
                context_id="ctx-1",
                pipeline_name="selling",
            )
        ),
        journal=A2APipelineJournal(tmp_path / "pipeline"),
        snapshot_store=A2APipelineSnapshotStore(tmp_path / "pipeline"),
    )
    ctx.runtime = A2APipelineRuntime(agent_runtime=_fake_runtime(), pipeline=pipeline, publisher=publisher)
    store.mirror_context(ctx)

    executor = IacCodeA2APipelineExecutor(
        task_store=store,
        model="qwen3.6-plus",
        metrics=NoOpA2AMetrics(),
        artifact_store=None,
        push_notifier=None,
        permission_resolver=None,
        auto_approve_permissions=False,
        thinking_exposure_types=None,
        aliyun_credential=AliyunCredential(
            access_key_id="client-id",
            access_key_secret="client-secret",
            region_id="cn-beijing",
        ),
    )

    await executor.execute(
        context=FakeRequestContext(
            text="please change cpu",
            metadata={"iac_code": {"cwd": str(tmp_path)}},
        ),
        event_queue=queue,
        task=task,
        task_id="task-1",
        context_id="ctx-1",
        cwd=str(tmp_path),
        prompt="please change cpu",
    )

    assert pipeline.interrupts == ["please change cpu"]
    assert captured_interrupt_credentials == ["client-id"]
    event_types = [event["eventType"] for event in publisher.journal.read_all()]
    assert event_types == ["interrupt_received", "interrupt_classified"]


@pytest.mark.asyncio
async def test_pipeline_executor_reconfigures_active_interrupt_runtime_thinking_per_call(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from iac_code.a2a.pipeline_events import PipelineA2AContext, PipelineEventTranslator
    from iac_code.a2a.pipeline_executor import A2APipelineRuntime, IacCodeA2APipelineExecutor
    from iac_code.a2a.pipeline_journal import A2APipelineJournal
    from iac_code.a2a.pipeline_snapshot import A2APipelineSnapshotStore
    from iac_code.a2a.pipeline_stream import PipelineA2AEventPublisher
    from iac_code.providers.request_policy import ProviderRequestPolicy

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

    class InterruptiblePipeline(FakePipeline):
        async def handle_user_interrupt(self, message: str) -> SimpleNamespace:
            return SimpleNamespace(
                action="supplement",
                reason="added context",
                rollback_target=None,
                candidate_scope=None,
            )

    provider_manager = FakeProviderManager()
    agent_runtime = SimpleNamespace(provider_manager=provider_manager, tool_registry=object())
    monkeypatch.setattr("iac_code.config.load_credentials", lambda model=None: {})

    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    task = await store.get_or_create_task(task_id="task-1", context_id="ctx-1")
    ctx = await store.get_or_create_context(
        context_id="ctx-1",
        cwd=str(tmp_path),
        runtime_factory=lambda _session_id: agent_runtime,
    )
    ctx.active_task_id = "task-1"

    queue = FakeEventQueue()
    pipeline = InterruptiblePipeline([TextDeltaEvent(text="running")], session_dir=tmp_path / "sidecar")
    publisher = PipelineA2AEventPublisher(
        event_queue=queue,
        translator=PipelineEventTranslator(
            PipelineA2AContext(
                pipeline_run_id="ctx-1",
                task_id="task-1",
                context_id="ctx-1",
                pipeline_name="selling",
            )
        ),
        journal=A2APipelineJournal(tmp_path / "pipeline"),
        snapshot_store=A2APipelineSnapshotStore(tmp_path / "pipeline"),
    )
    ctx.runtime = A2APipelineRuntime(agent_runtime=agent_runtime, pipeline=pipeline, publisher=publisher)
    store.mirror_context(ctx)

    executor = IacCodeA2APipelineExecutor(
        task_store=store,
        model="qwen3.6-plus",
        metrics=NoOpA2AMetrics(),
        artifact_store=None,
        push_notifier=None,
        permission_resolver=None,
        auto_approve_permissions=False,
        thinking_exposure_types=None,
        request_policy_override=ProviderRequestPolicy(thinking_enabled=False, effort="high", thinking_budget=2048),
    )

    await executor.execute(
        context=FakeRequestContext(text="please change cpu", metadata={"iac_code": {"cwd": str(tmp_path)}}),
        event_queue=queue,
        task=task,
        task_id="task-1",
        context_id="ctx-1",
        cwd=str(tmp_path),
        prompt="please change cpu",
    )

    policy = provider_manager.calls[0][1]
    assert provider_manager.calls[0][0] == "qwen3.6-plus"
    assert getattr(policy, "thinking_enabled", None) is False
    assert getattr(policy, "effort", None) == "high"
    assert getattr(policy, "thinking_budget", None) == 2048


@pytest.mark.asyncio
async def test_pipeline_executor_publishes_input_required_for_live_paused_interrupt(tmp_path: Path) -> None:
    from iac_code.a2a.pipeline_events import PipelineA2AContext, PipelineEventTranslator
    from iac_code.a2a.pipeline_executor import A2APipelineRuntime, IacCodeA2APipelineExecutor
    from iac_code.a2a.pipeline_journal import A2APipelineJournal
    from iac_code.a2a.pipeline_snapshot import A2APipelineSnapshotStore
    from iac_code.a2a.pipeline_stream import PipelineA2AEventPublisher

    class PausingPipeline(FakePipeline):
        def __init__(self, *, session_dir: Path) -> None:
            super().__init__([TextDeltaEvent(text="running")], session_dir=session_dir)
            self.interrupts: list[str] = []
            self.saved_pause_verdicts: list[SimpleNamespace] = []

        async def handle_user_interrupt(self, message: str) -> SimpleNamespace:
            self.interrupts.append(message)
            return SimpleNamespace(
                action="continue",
                reason="judge failed: timeout",
                rollback_target=None,
                candidate_scope=None,
                paused=True,
            )

        async def save_interrupt_pause(self, verdict: SimpleNamespace) -> PipelineEvent:
            self.saved_pause_verdicts.append(verdict)
            return PipelineEvent(
                type=PipelineEventType.USER_INPUT_REQUIRED,
                step_id="deploying",
                timestamp=1717821601.0,
                data={
                    "kind": "pipeline_pause_confirmation",
                    "prompt": "Pipeline paused.",
                    "reason": verdict.reason,
                    "paused": True,
                    "options": [],
                },
            )

    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    task = await store.get_or_create_task(task_id="task-1", context_id="ctx-1")
    task.state = "working"
    task.active_task = asyncio.current_task()
    ctx = await store.get_or_create_context(
        context_id="ctx-1",
        cwd=str(tmp_path),
        runtime_factory=lambda _session_id: _fake_runtime(),
    )
    ctx.active_task_id = "task-1"

    queue = FakeEventQueue()
    pipeline = PausingPipeline(session_dir=tmp_path / "sidecar")
    publisher = PipelineA2AEventPublisher(
        event_queue=queue,
        translator=PipelineEventTranslator(
            PipelineA2AContext(
                pipeline_run_id="ctx-1",
                task_id="task-1",
                context_id="ctx-1",
                pipeline_name="selling",
            )
        ),
        journal=A2APipelineJournal(tmp_path / "pipeline"),
        snapshot_store=A2APipelineSnapshotStore(tmp_path / "pipeline"),
    )
    ctx.runtime = A2APipelineRuntime(agent_runtime=_fake_runtime(), pipeline=pipeline, publisher=publisher)
    store.mirror_task(task)
    store.mirror_context(ctx)

    executor = IacCodeA2APipelineExecutor(
        task_store=store,
        model="qwen3.6-plus",
        metrics=NoOpA2AMetrics(),
        artifact_store=None,
        push_notifier=None,
        permission_resolver=None,
        auto_approve_permissions=False,
        thinking_exposure_types=None,
    )

    await executor.execute(
        context=FakeRequestContext(
            text="please stop",
            metadata={"iac_code": {"cwd": str(tmp_path)}},
        ),
        event_queue=queue,
        task=task,
        task_id="task-1",
        context_id="ctx-1",
        cwd=str(tmp_path),
        prompt="please stop",
    )

    assert pipeline.interrupts == ["please stop"]
    assert len(pipeline.saved_pause_verdicts) == 1
    event_types = [event["eventType"] for event in publisher.journal.read_all()]
    assert event_types == ["interrupt_received", "interrupt_classified", "input_required"]
    assert publisher.snapshot_store.load()["pendingInput"]["kind"] == "pipeline_pause_confirmation"
    assert task.state == "input-required"


@pytest.mark.asyncio
async def test_live_paused_interrupt_releases_active_task_and_next_reply_clears_pending_input(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("IAC_CODE_MODE", "pipeline")

    class PausingPipeline(FakePipeline):
        def __init__(self, *, session_dir: Path) -> None:
            super().__init__([], session_dir=session_dir)
            self.primary_stream = CloseableEventStream([TextDeltaEvent(text="before pause")])
            self.resume_stream = CloseableEventStream(
                [
                    PipelineEvent(
                        type=PipelineEventType.USER_INPUT_RECEIVED,
                        step_id="deploying",
                        timestamp=1717821602.0,
                        data={"kind": "pipeline_pause_confirmation", "user_input_length": 8},
                    ),
                    PipelineEvent(
                        type=PipelineEventType.PIPELINE_COMPLETED,
                        step_id=None,
                        timestamp=1717821603.0,
                        data={},
                    ),
                ],
                wait_until_closed=False,
            )
            self.interrupts: list[str] = []
            self.saved_pause_verdicts: list[SimpleNamespace] = []

        def run(self, prompt: str):
            self.run_prompts.append(prompt)
            return self.primary_stream

        def continue_from_sidecar(self, user_input: str | None = None):
            self.continue_calls += 1
            self.continue_inputs.append(user_input)
            self.sidecar_status = "running"
            return self.resume_stream

        async def handle_user_interrupt(self, message: str) -> SimpleNamespace:
            self.interrupts.append(message)
            return SimpleNamespace(
                action="continue",
                reason="judge failed: timeout",
                rollback_target=None,
                candidate_scope=None,
                paused=True,
            )

        async def save_interrupt_pause(self, verdict: SimpleNamespace) -> PipelineEvent:
            self.saved_pause_verdicts.append(verdict)
            self.sidecar_status = "waiting_input"
            return PipelineEvent(
                type=PipelineEventType.USER_INPUT_REQUIRED,
                step_id="deploying",
                timestamp=1717821601.0,
                data={
                    "kind": "pipeline_pause_confirmation",
                    "prompt": "Pipeline paused.",
                    "reason": verdict.reason,
                    "paused": True,
                    "options": [],
                },
            )

    pipeline = PausingPipeline(session_dir=tmp_path / "sidecar")
    monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_pipeline", lambda *args, **kwargs: pipeline)
    monkeypatch.setattr("iac_code.a2a.executor.create_agent_runtime", lambda options: _fake_runtime())
    monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_agent_runtime", lambda options: _fake_runtime())

    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    executor = IacCodeA2AExecutor(task_store=store, model="qwen3.6-plus")
    queue = FakeEventQueue()
    first = asyncio.create_task(
        executor.execute(
            FakeRequestContext(task_id="task-1", context_id="ctx-1", metadata={"iac_code": {"cwd": str(tmp_path)}}),
            queue,
        )
    )
    await _wait_for_output_text(await store.get_or_create_task(task_id="task-1", context_id="ctx-1"), "before pause")

    await executor.execute(
        FakeRequestContext(
            task_id="task-1",
            context_id="ctx-1",
            text="please pause",
            metadata={"iac_code": {"cwd": str(tmp_path)}},
        ),
        queue,
    )
    await asyncio.wait_for(first, timeout=_A2A_ASYNC_TEST_TIMEOUT)
    ctx = await store.get_or_create_context(context_id="ctx-1", cwd=str(tmp_path), runtime_factory=lambda _sid: None)
    assert ctx.active_task_id is None
    assert pipeline.primary_stream.closed is True
    assert pipeline.sidecar_status == "waiting_input"
    assert pipeline.interrupts == ["please pause"]
    assert (
        A2APipelineSnapshotStore(tmp_path / "sidecar").load()["pendingInput"]["kind"] == "pipeline_pause_confirmation"
    )

    await executor.execute(
        FakeRequestContext(
            task_id="task-1",
            context_id="ctx-1",
            text="continue",
            metadata={"iac_code": {"cwd": str(tmp_path)}},
        ),
        queue,
    )

    events = A2APipelineJournal(tmp_path / "sidecar").read_all()
    assert [event["eventType"] for event in events][-4:] == [
        "input_received",
        "pipeline_completed",
        "pipeline_completed",
        "backup_committed",
    ]
    assert [event.get("visibility") for event in events[-3:-1]] == ["pending_backup", "committed"]
    assert events[-1]["data"]["committedEventType"] == "pipeline_completed"
    assert A2APipelineSnapshotStore(tmp_path / "sidecar").load()["pendingInput"] is None
    assert pipeline.continue_inputs == ["continue"]


@pytest.mark.asyncio
async def test_active_pause_continuation_keeps_active_owner_until_continuation_finishes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("IAC_CODE_MODE", "pipeline")

    class BlockingCloseStream(CloseableEventStream):
        def __init__(self, events) -> None:
            super().__init__(events)
            self.allow_close = asyncio.Event()

        async def aclose(self) -> None:
            self.closed = True
            self.closed_event.set()
            await self.allow_close.wait()

    class PausingPipeline(FakePipeline):
        def __init__(self, *, session_dir: Path) -> None:
            super().__init__([], session_dir=session_dir)
            self.primary_stream = BlockingCloseStream([TextDeltaEvent(text="before pause")])
            self.continuation_started = asyncio.Event()
            self.finish_continuation = asyncio.Event()

        def run(self, prompt: str):
            return self.primary_stream

        async def handle_user_interrupt(self, message: str) -> SimpleNamespace:
            return SimpleNamespace(action="continue", reason="pause requested", paused=True)

        async def save_interrupt_pause(self, verdict: SimpleNamespace) -> PipelineEvent:
            self.sidecar_status = "waiting_input"
            return PipelineEvent(
                type=PipelineEventType.USER_INPUT_REQUIRED,
                step_id="deploying",
                timestamp=1717821601.0,
                data={
                    "kind": "pipeline_pause_confirmation",
                    "prompt": "Pipeline paused.",
                    "reason": verdict.reason,
                    "paused": True,
                    "options": [],
                },
            )

        def continue_from_sidecar(self, user_input: str | None = None):
            self.continue_calls += 1
            self.continue_inputs.append(user_input)
            self.sidecar_status = "running"

            async def stream():
                self.continuation_started.set()
                yield PipelineEvent(
                    type=PipelineEventType.USER_INPUT_RECEIVED,
                    step_id="deploying",
                    timestamp=1717821602.0,
                    data={"kind": "pipeline_pause_confirmation", "user_input_length": len(user_input or "")},
                )
                await self.finish_continuation.wait()
                yield PipelineEvent(
                    type=PipelineEventType.PIPELINE_COMPLETED,
                    step_id=None,
                    timestamp=1717821603.0,
                    data={},
                )

            return stream()

    pipeline = PausingPipeline(session_dir=tmp_path / "sidecar")
    monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_pipeline", lambda *args, **kwargs: pipeline)
    monkeypatch.setattr("iac_code.a2a.executor.create_agent_runtime", lambda options: _fake_runtime())
    monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_agent_runtime", lambda options: _fake_runtime())

    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    executor = IacCodeA2AExecutor(task_store=store, model="qwen3.6-plus")
    queue = FakeEventQueue()
    first = asyncio.create_task(
        executor.execute(
            FakeRequestContext(task_id="task-1", context_id="ctx-1", metadata={"iac_code": {"cwd": str(tmp_path)}}),
            queue,
        )
    )
    active_task = await store.get_or_create_task(task_id="task-1", context_id="ctx-1")
    await _wait_for_output_text(active_task, "before pause")

    await executor.execute(
        FakeRequestContext(
            task_id="task-1",
            context_id="ctx-1",
            text="please pause",
            metadata={"iac_code": {"cwd": str(tmp_path)}},
        ),
        queue,
    )

    continuation = asyncio.create_task(
        executor.execute(
            FakeRequestContext(
                task_id="task-1",
                context_id="ctx-1",
                text="continue",
                metadata={"iac_code": {"cwd": str(tmp_path)}},
            ),
            queue,
        )
    )
    try:
        await asyncio.wait_for(pipeline.continuation_started.wait(), timeout=_A2A_ASYNC_TEST_TIMEOUT)
        pipeline.primary_stream.allow_close.set()
        await asyncio.wait_for(first, timeout=_A2A_ASYNC_TEST_TIMEOUT)

        ctx = await store.get_or_create_context(
            context_id="ctx-1", cwd=str(tmp_path), runtime_factory=lambda _sid: None
        )
        active_task = await store.get_or_create_task(task_id="task-1", context_id="ctx-1")
        assert ctx.active_task_id == "task-1"
        assert active_task.active_task is continuation

        pipeline.finish_continuation.set()
        await asyncio.wait_for(continuation, timeout=_A2A_ASYNC_TEST_TIMEOUT)
        assert ctx.active_task_id is None
        assert active_task.active_task is None
    finally:
        pipeline.primary_stream.allow_close.set()
        pipeline.finish_continuation.set()
        for runner in (first, continuation):
            if not runner.done():
                runner.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await runner


@pytest.mark.asyncio
async def test_active_task_route_continues_pending_pause_confirmation(tmp_path: Path) -> None:
    from iac_code.a2a.pipeline_events import PipelineA2AContext, PipelineEventTranslator
    from iac_code.a2a.pipeline_executor import A2APipelineRuntime, IacCodeA2APipelineExecutor
    from iac_code.a2a.pipeline_stream import PipelineA2AEventPublisher

    class PausedPipeline(FakePipeline):
        def __init__(self, *, session_dir: Path) -> None:
            super().__init__([], session_dir=session_dir)
            self.sidecar_status = "waiting_input"
            self.interrupts: list[str] = []

        async def handle_user_interrupt(self, message: str) -> SimpleNamespace:
            self.interrupts.append(message)
            return SimpleNamespace(action="continue", reason="should not run")

        def continue_from_sidecar(self, user_input: str | None = None):
            self.continue_calls += 1
            self.continue_inputs.append(user_input)

            async def stream():
                yield PipelineEvent(
                    type=PipelineEventType.USER_INPUT_RECEIVED,
                    step_id="deploying",
                    timestamp=1717821602.0,
                    data={"kind": "pipeline_pause_confirmation", "user_input_length": len(user_input or "")},
                )
                yield PipelineEvent(
                    type=PipelineEventType.PIPELINE_COMPLETED,
                    step_id=None,
                    timestamp=1717821603.0,
                    data={},
                )

            return stream()

    sidecar_dir = tmp_path / "sidecar"
    pending = {
        "schemaVersion": "1.0",
        "extensionUri": "urn:iac-code:a2a:pipeline-events:v1",
        "eventId": "evt-pause",
        "sequence": 1,
        "createdAt": "2026-06-08T10:00:00Z",
        "eventType": "input_required",
        "scope": "pipeline",
        "pipelineRunId": "ctx-1",
        "taskId": "task-1",
        "contextId": "ctx-1",
        "pipelineName": "selling",
        "status": "input_required",
        "data": {
            "kind": "pipeline_pause_confirmation",
            "prompt": "Pipeline paused.",
            "reason": "judge failed: timeout",
            "paused": True,
            "options": [],
        },
    }
    A2APipelineJournal(sidecar_dir).append(pending)
    A2APipelineSnapshotStore(sidecar_dir).save(reduce_pipeline_events([pending]))
    pipeline = PausedPipeline(session_dir=sidecar_dir)
    queue = FakeEventQueue()
    publisher = PipelineA2AEventPublisher(
        event_queue=queue,
        translator=PipelineEventTranslator(
            PipelineA2AContext(
                pipeline_run_id="ctx-1",
                task_id="task-1",
                context_id="ctx-1",
                pipeline_name="selling",
            )
        ),
        journal=A2APipelineJournal(sidecar_dir),
        snapshot_store=A2APipelineSnapshotStore(sidecar_dir),
    )
    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    task = await store.get_or_create_task(task_id="task-1", context_id="ctx-1")
    task.state = "input-required"
    task.active_task = asyncio.current_task()
    ctx = await store.get_or_create_context(
        context_id="ctx-1",
        cwd=str(tmp_path),
        runtime_factory=lambda _session_id: _fake_runtime(),
    )
    ctx.active_task_id = "task-1"
    ctx.runtime = A2APipelineRuntime(agent_runtime=_fake_runtime(), pipeline=pipeline, publisher=publisher)
    store.mirror_task(task)
    store.mirror_context(ctx)

    executor = IacCodeA2APipelineExecutor(
        task_store=store,
        model="qwen3.6-plus",
        metrics=NoOpA2AMetrics(),
        artifact_store=None,
        push_notifier=None,
        permission_resolver=None,
        auto_approve_permissions=False,
        thinking_exposure_types=None,
    )

    await executor.execute(
        context=FakeRequestContext(text="continue", metadata={"iac_code": {"cwd": str(tmp_path)}}),
        event_queue=queue,
        task=task,
        task_id="task-1",
        context_id="ctx-1",
        cwd=str(tmp_path),
        prompt="continue",
    )

    assert pipeline.interrupts == []
    assert pipeline.continue_inputs == ["continue"]
    events = A2APipelineJournal(sidecar_dir).read_all()
    assert [event["eventType"] for event in events][-2:] == ["input_received", "pipeline_completed"]
    assert A2APipelineSnapshotStore(sidecar_dir).load()["pendingInput"] is None


@pytest.mark.asyncio
async def test_active_pause_confirmation_failure_marks_task_and_pipeline_failed(tmp_path: Path) -> None:
    from iac_code.a2a.pipeline_events import PipelineA2AContext, PipelineEventTranslator
    from iac_code.a2a.pipeline_executor import A2APipelineRuntime, IacCodeA2APipelineExecutor
    from iac_code.a2a.pipeline_stream import PipelineA2AEventPublisher

    class FailingPausedPipeline(FakePipeline):
        def __init__(self, *, session_dir: Path) -> None:
            super().__init__([], session_dir=session_dir)
            self.sidecar_status = "waiting_input"

        def continue_from_sidecar(self, user_input: str | None = None):
            self.continue_calls += 1
            self.continue_inputs.append(user_input)

            async def stream():
                raise RuntimeError("pause continuation failed token=secret-value")
                if False:
                    yield

            return stream()

    sidecar_dir = tmp_path / "sidecar"
    pending = {
        "schemaVersion": "1.0",
        "extensionUri": "urn:iac-code:a2a:pipeline-events:v1",
        "eventId": "evt-pause",
        "sequence": 1,
        "createdAt": "2026-06-08T10:00:00Z",
        "eventType": "input_required",
        "scope": "pipeline",
        "pipelineRunId": "ctx-1",
        "taskId": "task-1",
        "contextId": "ctx-1",
        "pipelineName": "selling",
        "status": "input_required",
        "data": {"kind": "pipeline_pause_confirmation", "prompt": "Pipeline paused."},
    }
    A2APipelineJournal(sidecar_dir).append(pending)
    A2APipelineSnapshotStore(sidecar_dir).save(reduce_pipeline_events([pending]))
    queue = FakeEventQueue()
    publisher = PipelineA2AEventPublisher(
        event_queue=queue,
        translator=PipelineEventTranslator(
            PipelineA2AContext(
                pipeline_run_id="ctx-1",
                task_id="task-1",
                context_id="ctx-1",
                pipeline_name="selling",
            )
        ),
        journal=A2APipelineJournal(sidecar_dir),
        snapshot_store=A2APipelineSnapshotStore(sidecar_dir),
    )
    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    task = await store.get_or_create_task(task_id="task-1", context_id="ctx-1")
    task.state = "input-required"
    ctx = await store.get_or_create_context(
        context_id="ctx-1",
        cwd=str(tmp_path),
        runtime_factory=lambda _session_id: _fake_runtime(),
    )
    ctx.active_task_id = "task-1"
    ctx.runtime = A2APipelineRuntime(
        agent_runtime=_fake_runtime(),
        pipeline=FailingPausedPipeline(session_dir=sidecar_dir),
        publisher=publisher,
    )
    store.mirror_task(task)
    store.mirror_context(ctx)
    executor = IacCodeA2APipelineExecutor(
        task_store=store,
        model="qwen3.6-plus",
        metrics=NoOpA2AMetrics(),
        artifact_store=None,
        push_notifier=None,
        permission_resolver=None,
        auto_approve_permissions=False,
        thinking_exposure_types=None,
    )

    await executor.execute(
        context=FakeRequestContext(text="continue", metadata={"iac_code": {"cwd": str(tmp_path)}}),
        event_queue=queue,
        task=task,
        task_id="task-1",
        context_id="ctx-1",
        cwd=str(tmp_path),
        prompt="continue",
    )

    assert task.state == "failed"
    journal_events = A2APipelineJournal(sidecar_dir).read_all()
    assert journal_events[-1]["eventType"] == "pipeline_failed"
    snapshot = A2APipelineSnapshotStore(sidecar_dir).load()
    assert snapshot["status"] == "failed"
    assert snapshot["pendingInput"] is None
    assert any(
        dump(event).get("metadata", {}).get("iac_code", {}).get("pipeline", {}).get("eventType") == "pipeline_failed"
        for event in queue.events
    )


@pytest.mark.asyncio
async def test_pipeline_executor_publishes_interrupt_received_before_slow_judge(tmp_path: Path) -> None:
    from iac_code.a2a.pipeline_events import PipelineA2AContext, PipelineEventTranslator
    from iac_code.a2a.pipeline_executor import A2APipelineRuntime, IacCodeA2APipelineExecutor
    from iac_code.a2a.pipeline_journal import A2APipelineJournal
    from iac_code.a2a.pipeline_snapshot import A2APipelineSnapshotStore
    from iac_code.a2a.pipeline_stream import PipelineA2AEventPublisher

    class SlowInterruptPipeline(FakePipeline):
        def __init__(self, *, session_dir: Path) -> None:
            super().__init__([], session_dir=session_dir)
            self.judge_started = asyncio.Event()
            self.finish_judge = asyncio.Event()

        async def handle_user_interrupt(self, message: str) -> SimpleNamespace:
            self.judge_started.set()
            await self.finish_judge.wait()
            return SimpleNamespace(
                action="continue",
                reason="not relevant",
                rollback_target=None,
                candidate_scope=None,
                supplement_target=None,
            )

    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    task = await store.get_or_create_task(task_id="task-1", context_id="ctx-1")
    task.state = "working"
    task.active_task = asyncio.current_task()
    ctx = await store.get_or_create_context(
        context_id="ctx-1",
        cwd=str(tmp_path),
        runtime_factory=lambda _session_id: _fake_runtime(),
    )
    ctx.active_task_id = "task-1"
    pipeline = SlowInterruptPipeline(session_dir=tmp_path / "sidecar")
    publisher = PipelineA2AEventPublisher(
        event_queue=FakeEventQueue(),
        translator=PipelineEventTranslator(
            PipelineA2AContext(
                pipeline_run_id="ctx-1",
                task_id="task-1",
                context_id="ctx-1",
                pipeline_name="selling",
            )
        ),
        journal=A2APipelineJournal(tmp_path / "pipeline"),
        snapshot_store=A2APipelineSnapshotStore(tmp_path / "pipeline"),
    )
    ctx.runtime = A2APipelineRuntime(agent_runtime=_fake_runtime(), pipeline=pipeline, publisher=publisher)
    store.mirror_task(task)
    store.mirror_context(ctx)

    executor = IacCodeA2APipelineExecutor(
        task_store=store,
        model="qwen3.6-plus",
        metrics=NoOpA2AMetrics(),
        artifact_store=None,
        push_notifier=None,
        permission_resolver=None,
        auto_approve_permissions=False,
        thinking_exposure_types=None,
    )
    interrupt_task = asyncio.create_task(
        executor.execute(
            context=FakeRequestContext(text="hello", metadata={"iac_code": {"cwd": str(tmp_path)}}),
            event_queue=FakeEventQueue(),
            task=task,
            task_id="task-1",
            context_id="ctx-1",
            cwd=str(tmp_path),
            prompt="hello",
        )
    )
    try:
        await asyncio.wait_for(pipeline.judge_started.wait(), timeout=_A2A_ASYNC_TEST_TIMEOUT)
        assert [event["eventType"] for event in publisher.journal.read_all()] == ["interrupt_received"]
    finally:
        pipeline.finish_judge.set()
        await asyncio.wait_for(interrupt_task, timeout=_A2A_ASYNC_TEST_TIMEOUT)

    assert [event["eventType"] for event in publisher.journal.read_all()] == [
        "interrupt_received",
        "interrupt_classified",
    ]


@pytest.mark.asyncio
async def test_pipeline_executor_stops_at_ask_user_question_without_holding_active_task(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from iac_code.a2a.pipeline_executor import IacCodeA2APipelineExecutor

    class AskingPipeline(FakePipeline):
        def __init__(self, *, session_dir: Path) -> None:
            super().__init__([], session_dir=session_dir)
            self.question_ready = asyncio.Event()
            self.answers: list[dict[str, str] | None] = []
            self.future: asyncio.Future[dict[str, str] | None] | None = None
            self.closed = asyncio.Event()

        async def run(self, prompt: str):
            self.run_prompts.append(prompt)
            self.future = asyncio.get_running_loop().create_future()
            self.question_ready.set()
            answer = None
            try:
                yield AskUserQuestionEvent(
                    tool_use_id="ask-1",
                    question="请选择部署目标",
                    options=[
                        {"id": "nginx", "label": "Nginx 网站"},
                        {"id": "ecs", "label": "ECS 应用"},
                    ],
                    allow_free_text=True,
                    free_text_prompt="也可以直接描述目标",
                    response_future=self.future,
                )
                answer = await self.future
                self.answers.append(answer)
                yield TextDeltaEvent(text=answer["selected_id"] if answer else "cancelled")
                yield PipelineEvent(
                    type=PipelineEventType.PIPELINE_COMPLETED,
                    step_id=None,
                    timestamp=1717821602.0,
                    data={},
                )
            finally:
                self.closed.set()
                if answer is None and self.future is not None and not self.future.done():
                    self.future.set_result(None)

    pipeline = AskingPipeline(session_dir=tmp_path / "sidecar")
    monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_pipeline", lambda *args, **kwargs: pipeline)
    monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_agent_runtime", lambda options: _fake_runtime())

    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    active_task = await store.get_or_create_task(task_id="task-1", context_id="ctx-1")
    executor = IacCodeA2APipelineExecutor(
        task_store=store,
        model="qwen3.6-plus",
        metrics=NoOpA2AMetrics(),
        artifact_store=None,
        push_notifier=None,
        permission_resolver=None,
        auto_approve_permissions=False,
        thinking_exposure_types=None,
    )
    queue = FakeEventQueue()
    runner = asyncio.create_task(
        executor.execute(
            context=FakeRequestContext(metadata={"iac_code": {"cwd": str(tmp_path)}}),
            event_queue=queue,
            task=active_task,
            task_id="task-1",
            context_id="ctx-1",
            cwd=str(tmp_path),
            prompt="帮我部署网站",
        )
    )
    await asyncio.wait_for(pipeline.question_ready.wait(), timeout=_A2A_ASYNC_TEST_TIMEOUT)
    await _wait_for_pipeline_event(queue, "input_required")

    await asyncio.wait_for(runner, timeout=_A2A_ASYNC_TEST_TIMEOUT)
    await asyncio.wait_for(pipeline.closed.wait(), timeout=_A2A_ASYNC_TEST_TIMEOUT)

    ctx = await store.get_or_create_context(
        context_id="ctx-1",
        cwd=str(tmp_path),
        runtime_factory=lambda _session_id: _fake_runtime(),
    )
    assert ctx.active_task_id is None
    assert active_task.state == "input-required"
    assert pipeline.answers == []
    assert "".join(active_task.output_text) == ""
    event_types = [
        dump(event)["metadata"]["iac_code"]["pipeline"]["eventType"]
        for event in queue.events
        if isinstance(event, TaskStatusUpdateEvent)
        and "pipeline" in dump(event).get("metadata", {}).get("iac_code", {})
    ]
    assert event_types == ["input_required"]


def test_ask_user_question_answer_accepts_one_based_option_index() -> None:
    from iac_code.a2a.pipeline_executor import _ask_user_question_answer_from_prompt

    answer = _ask_user_question_answer_from_prompt(
        AskUserQuestionEvent(
            tool_use_id="ask-1",
            question="请选择部署目标",
            options=[
                {"id": "nginx", "label": "Nginx 网站"},
                {"id": "ecs", "label": "ECS 应用"},
            ],
        ),
        "1",
    )

    assert answer == {"selected_id": "nginx", "selected_label": "Nginx 网站", "free_text": ""}


def test_ask_user_question_answer_keeps_partial_option_label_as_free_text() -> None:
    from iac_code.a2a.pipeline_executor import _ask_user_question_answer_from_prompt

    answer = _ask_user_question_answer_from_prompt(
        AskUserQuestionEvent(
            tool_use_id="ask-1",
            question="是否继续处理部署需求？",
            options=[
                {"id": "skip", "label": "暂不处理，我只是随便测试一下"},
                {"id": "describe", "label": "补充部署需求"},
            ],
            allow_free_text=True,
        ),
        "暂不处理",
    )

    assert answer == {
        "selected_id": "",
        "selected_label": "",
        "free_text": "暂不处理",
    }


@pytest.mark.asyncio
async def test_pipeline_executor_does_not_resolve_pending_question_when_input_received_publish_fails(
    tmp_path: Path,
) -> None:
    from iac_code.a2a.pipeline_events import PipelineA2AContext, PipelineEventTranslator
    from iac_code.a2a.pipeline_executor import (
        A2APipelineRuntime,
        IacCodeA2APipelineExecutor,
        _PendingAskUserQuestion,
    )
    from iac_code.a2a.pipeline_journal import A2APipelineJournal
    from iac_code.a2a.pipeline_snapshot import A2APipelineSnapshotStore
    from iac_code.a2a.pipeline_stream import PipelineA2AEventPublisher

    future: asyncio.Future[dict[str, str] | None] = asyncio.get_running_loop().create_future()
    question = AskUserQuestionEvent(
        tool_use_id="ask-1",
        question="请选择部署目标",
        options=[{"id": "nginx", "label": "Nginx 网站"}],
        response_future=future,
    )
    publisher = PipelineA2AEventPublisher(
        event_queue=FakeEventQueue(),
        translator=PipelineEventTranslator(
            PipelineA2AContext(
                pipeline_run_id="ctx-1",
                task_id="task-1",
                context_id="ctx-1",
                pipeline_name="selling",
            )
        ),
        journal=A2APipelineJournal(tmp_path / "pipeline"),
        snapshot_store=A2APipelineSnapshotStore(tmp_path / "pipeline"),
    )
    publisher.publish_manual = AsyncMock(return_value=None)  # type: ignore[method-assign]
    runtime = A2APipelineRuntime(agent_runtime=_fake_runtime(), publisher=publisher)
    runtime.pending_question = _PendingAskUserQuestion(
        event=question,
        envelope={
            "eventType": "input_required",
            "scope": "step",
            "input": {"inputId": "ask-ask-1"},
            "step": {"runId": "step-intent_parsing-1", "id": "intent_parsing"},
        },
    )
    executor = IacCodeA2APipelineExecutor(
        task_store=A2ATaskStore(metrics=NoOpA2AMetrics()),
        model="qwen3.6-plus",
        metrics=NoOpA2AMetrics(),
        artifact_store=None,
        push_notifier=None,
        permission_resolver=None,
        auto_approve_permissions=False,
        thinking_exposure_types=None,
    )

    routed = await executor._route_pending_question_answer(runtime, "Nginx 网站")

    assert routed == "not_routed"
    assert future.done() is False
    assert runtime.pending_question is not None


@pytest.mark.asyncio
async def test_active_task_route_does_not_treat_finished_pending_question_as_interrupt(
    tmp_path: Path,
) -> None:
    from iac_code.a2a.pipeline_events import PipelineA2AContext, PipelineEventTranslator
    from iac_code.a2a.pipeline_executor import (
        A2APipelineRuntime,
        IacCodeA2APipelineExecutor,
        _PendingAskUserQuestion,
    )
    from iac_code.a2a.pipeline_journal import A2APipelineJournal
    from iac_code.a2a.pipeline_snapshot import A2APipelineSnapshotStore
    from iac_code.a2a.pipeline_stream import PipelineA2AEventPublisher

    class InterruptRecordingPipeline(FakePipeline):
        def __init__(self, *, session_dir: Path) -> None:
            super().__init__([], session_dir=session_dir)
            self.interrupts: list[str] = []

        async def handle_user_interrupt(self, message: str) -> SimpleNamespace:
            self.interrupts.append(message)
            return SimpleNamespace(action="supplement", reason="wrong route")

    future: asyncio.Future[dict[str, str] | None] = asyncio.get_running_loop().create_future()
    future.set_result(None)
    question = AskUserQuestionEvent(
        tool_use_id="ask-1",
        question="请选择部署目标",
        options=[{"id": "nginx", "label": "Nginx 网站"}],
        response_future=future,
    )
    publisher = PipelineA2AEventPublisher(
        event_queue=FakeEventQueue(),
        translator=PipelineEventTranslator(
            PipelineA2AContext(
                pipeline_run_id="ctx-1",
                task_id="task-1",
                context_id="ctx-1",
                pipeline_name="selling",
            )
        ),
        journal=A2APipelineJournal(tmp_path / "pipeline"),
        snapshot_store=A2APipelineSnapshotStore(tmp_path / "pipeline"),
    )
    runtime = A2APipelineRuntime(
        agent_runtime=_fake_runtime(),
        pipeline=InterruptRecordingPipeline(session_dir=tmp_path / "sidecar"),
        publisher=publisher,
    )
    runtime.pending_question = _PendingAskUserQuestion(
        event=question,
        envelope={
            "eventType": "input_required",
            "scope": "step",
            "input": {"inputId": "ask-ask-1"},
            "step": {"runId": "step-intent_parsing-1", "id": "intent_parsing"},
        },
    )
    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    task = await store.get_or_create_task(task_id="task-1", context_id="ctx-1")
    ctx = await store.get_or_create_context(
        context_id="ctx-1",
        cwd=str(tmp_path),
        runtime_factory=lambda _session_id: _fake_runtime(),
    )
    ctx.runtime = runtime
    ctx.active_task_id = "task-1"
    executor = IacCodeA2APipelineExecutor(
        task_store=store,
        model="qwen3.6-plus",
        metrics=NoOpA2AMetrics(),
        artifact_store=None,
        push_notifier=None,
        permission_resolver=None,
        auto_approve_permissions=False,
        thinking_exposure_types=None,
    )

    routed = await executor._route_active_pipeline_interrupt(
        FakeEventQueue(),
        task=task,
        ctx=ctx,
        task_id="task-1",
        context_id="ctx-1",
        cwd=str(tmp_path),
        pipeline_input="Nginx 网站",
        preserve_task_record=True,
    )

    assert routed is True
    assert runtime.pipeline.interrupts == []


@pytest.mark.asyncio
async def test_active_task_route_answers_pending_question_without_marking_input_required(
    tmp_path: Path,
) -> None:
    from iac_code.a2a.pipeline_events import PipelineA2AContext, PipelineEventTranslator
    from iac_code.a2a.pipeline_executor import (
        A2APipelineRuntime,
        IacCodeA2APipelineExecutor,
        _PendingAskUserQuestion,
    )
    from iac_code.a2a.pipeline_journal import A2APipelineJournal
    from iac_code.a2a.pipeline_snapshot import A2APipelineSnapshotStore
    from iac_code.a2a.pipeline_stream import PipelineA2AEventPublisher

    class InterruptRecordingPipeline(FakePipeline):
        def __init__(self, *, session_dir: Path) -> None:
            super().__init__([], session_dir=session_dir)
            self.interrupts: list[str] = []

        async def handle_user_interrupt(self, message: str) -> SimpleNamespace:
            self.interrupts.append(message)
            return SimpleNamespace(action="supplement", reason="wrong route")

    future: asyncio.Future[dict[str, str] | None] = asyncio.get_running_loop().create_future()
    question = AskUserQuestionEvent(
        tool_use_id="ask-1",
        question="请选择部署目标",
        options=[{"id": "nginx", "label": "Nginx 网站"}],
        response_future=future,
    )
    publisher = PipelineA2AEventPublisher(
        event_queue=FakeEventQueue(),
        translator=PipelineEventTranslator(
            PipelineA2AContext(
                pipeline_run_id="ctx-1",
                task_id="task-1",
                context_id="ctx-1",
                pipeline_name="selling",
            )
        ),
        journal=A2APipelineJournal(tmp_path / "pipeline"),
        snapshot_store=A2APipelineSnapshotStore(tmp_path / "pipeline"),
    )
    runtime = A2APipelineRuntime(
        agent_runtime=_fake_runtime(),
        pipeline=InterruptRecordingPipeline(session_dir=tmp_path / "sidecar"),
        publisher=publisher,
    )
    runtime.pending_question = _PendingAskUserQuestion(
        event=question,
        envelope={
            "eventType": "input_required",
            "scope": "step",
            "input": {"inputId": "ask-ask-1"},
            "step": {"runId": "step-intent_parsing-1", "id": "intent_parsing"},
        },
    )
    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    task = await store.get_or_create_task(task_id="task-1", context_id="ctx-1")
    task.state = "input-required"
    ctx = await store.get_or_create_context(
        context_id="ctx-1",
        cwd=str(tmp_path),
        runtime_factory=lambda _session_id: _fake_runtime(),
    )
    ctx.runtime = runtime
    ctx.active_task_id = "task-1"
    executor = IacCodeA2APipelineExecutor(
        task_store=store,
        model="qwen3.6-plus",
        metrics=NoOpA2AMetrics(),
        artifact_store=None,
        push_notifier=None,
        permission_resolver=None,
        auto_approve_permissions=False,
        thinking_exposure_types=None,
    )

    routed = await executor._route_active_pipeline_interrupt(
        FakeEventQueue(),
        task=task,
        ctx=ctx,
        task_id="task-1",
        context_id="ctx-1",
        cwd=str(tmp_path),
        pipeline_input="Nginx 网站",
        preserve_task_record=True,
    )

    assert routed is True
    assert runtime.pipeline.interrupts == []
    assert future.result()["selected_id"] == "nginx"
    assert runtime.pending_question is None
    assert task.state == "working"


@pytest.mark.asyncio
async def test_executor_routes_running_sidecar_pending_ask_to_ask_resume(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from iac_code.services.providers.aliyun import AliyunCredentials

    monkeypatch.setenv("IAC_CODE_MODE", "pipeline")
    captured_resume_credentials: list[str | None] = []

    class AskResumePipeline(FakePipeline):
        def __init__(self, *, session_dir: Path) -> None:
            super().__init__(
                [
                    TextDeltaEvent(text="nginx selected"),
                    PipelineEvent(
                        type=PipelineEventType.PIPELINE_COMPLETED,
                        step_id=None,
                        timestamp=1717821601.0,
                        data={},
                    ),
                ],
                session_dir=session_dir,
            )
            self.ask_answers: list[dict[str, str]] = []
            self.pending_inputs: list[dict[str, object] | None] = []

        async def resume_ask_user_question(
            self,
            answer: dict[str, str],
            *,
            tool_use_id: str,
            pending_input: dict[str, object] | None = None,
        ):
            credential = AliyunCredentials.load()
            captured_resume_credentials.append(credential.access_key_id if credential else None)
            self.ask_answers.append(answer)
            self.pending_inputs.append(pending_input)
            assert tool_use_id == "ask-1"
            for event in self.events:
                yield event

    session_dir = tmp_path / "sidecar"
    pending = {
        "schemaVersion": "1.0",
        "extensionUri": "urn:iac-code:a2a:pipeline-events:v1",
        "eventId": "evt-ask",
        "sequence": 1,
        "createdAt": "2026-06-08T10:00:00Z",
        "eventType": "input_required",
        "scope": "candidate_step",
        "pipelineRunId": "ctx-1",
        "taskId": "task-1",
        "contextId": "ctx-1",
        "pipelineName": "selling",
        "status": "input_required",
        "step": {"runId": "step-intent_parsing-1", "id": "intent_parsing", "attempt": 1},
        "candidate": {"runId": "candidate-evaluate_candidate-0-1", "id": "evaluate_candidate", "index": 0},
        "candidateStep": {
            "runId": "candidate-evaluate_candidate-0-1-template_generating-1",
            "id": "template_generating",
        },
        "data": {"kind": "ask_user_question", "toolUseId": "ask-1"},
        "input": {
            "inputId": "ask-ask-1",
            "kind": "ask_user_question",
            "toolUseId": "ask-1",
            "question": "请选择部署目标",
            "options": [{"id": "nginx", "label": "Nginx 网站"}],
            "allowFreeText": True,
        },
    }
    A2APipelineJournal(session_dir).append(pending)
    A2APipelineSnapshotStore(session_dir).save(reduce_pipeline_events([pending]))
    fake_pipeline = AskResumePipeline(session_dir=session_dir)
    fake_pipeline.sidecar_status = "running"
    monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_pipeline", lambda *args, **kwargs: fake_pipeline)
    monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_agent_runtime", lambda options: _fake_runtime())
    monkeypatch.setattr("iac_code.tools.cloud.registry.register_cloud_tools", lambda *args, **kwargs: None)

    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    executor = IacCodeA2AExecutor(task_store=store, model="qwen3.6-plus")
    queue = FakeEventQueue()

    await executor.execute(
        FakeRequestContext(
            text="Nginx 网站",
            metadata={
                "iac_code": {
                    "cwd": str(tmp_path),
                    "alibaba_cloud_access_key_id": "client-id",
                    "alibaba_cloud_access_key_secret": "client-secret",
                    "alibaba_cloud_region_id": "cn-beijing",
                }
            },
        ),
        queue,
    )

    assert fake_pipeline.continue_calls == 0
    assert fake_pipeline.ask_answers == [{"selected_id": "nginx", "selected_label": "Nginx 网站", "free_text": ""}]
    assert captured_resume_credentials == ["client-id"]
    assert fake_pipeline.pending_inputs[0]["candidate"] == {
        "runId": "candidate-evaluate_candidate-0-1",
        "id": "evaluate_candidate",
        "index": 0,
    }
    assert fake_pipeline.pending_inputs[0]["candidateStep"] == {
        "runId": "candidate-evaluate_candidate-0-1-template_generating-1",
        "id": "template_generating",
    }
    task_record = await store.get_or_create_task(task_id="task-1", context_id="ctx-1")
    assert "nginx selected" in "".join(task_record.output_text)
    assert "input_received" in [event["eventType"] for event in A2APipelineJournal(session_dir).read_all()]


@pytest.mark.asyncio
async def test_executor_routes_waiting_input_sidecar_pending_ask_to_ask_resume(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("IAC_CODE_MODE", "pipeline")

    class AskResumePipeline(FakePipeline):
        def __init__(self, *, session_dir: Path) -> None:
            super().__init__(
                [
                    TextDeltaEvent(text="nginx selected"),
                    PipelineEvent(
                        type=PipelineEventType.PIPELINE_COMPLETED,
                        step_id=None,
                        timestamp=1717821601.0,
                        data={},
                    ),
                ],
                session_dir=session_dir,
            )
            self.ask_answers: list[dict[str, str]] = []

        async def resume_ask_user_question(self, answer: dict[str, str], *, tool_use_id: str):
            self.ask_answers.append(answer)
            assert tool_use_id == "ask-1"
            for event in self.events:
                yield event

    session_dir = tmp_path / "sidecar"
    pending = {
        "schemaVersion": "1.0",
        "extensionUri": "urn:iac-code:a2a:pipeline-events:v1",
        "eventId": "evt-ask",
        "sequence": 1,
        "createdAt": "2026-06-08T10:00:00Z",
        "eventType": "input_required",
        "scope": "step",
        "pipelineRunId": "ctx-1",
        "taskId": "task-1",
        "contextId": "ctx-1",
        "pipelineName": "selling",
        "status": "input_required",
        "step": {"runId": "step-intent_parsing-1", "id": "intent_parsing", "attempt": 1},
        "data": {"kind": "ask_user_question", "toolUseId": "ask-1"},
        "input": {
            "inputId": "ask-ask-1",
            "kind": "ask_user_question",
            "toolUseId": "ask-1",
            "question": "请选择部署目标",
            "options": [{"id": "nginx", "label": "Nginx 网站"}],
            "allowFreeText": True,
        },
    }
    A2APipelineJournal(session_dir).append(pending)
    A2APipelineSnapshotStore(session_dir).save(reduce_pipeline_events([pending]))
    fake_pipeline = AskResumePipeline(session_dir=session_dir)
    fake_pipeline.sidecar_status = "waiting_input"
    monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_pipeline", lambda *args, **kwargs: fake_pipeline)
    monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_agent_runtime", lambda options: _fake_runtime())

    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    executor = IacCodeA2AExecutor(task_store=store, model="qwen3.6-plus")
    queue = FakeEventQueue()

    await executor.execute(
        FakeRequestContext(
            text="Nginx 网站",
            metadata={"iac_code": {"cwd": str(tmp_path)}},
        ),
        queue,
    )

    assert fake_pipeline.resume_prompts == []
    assert fake_pipeline.ask_answers == [{"selected_id": "nginx", "selected_label": "Nginx 网站", "free_text": ""}]
    task_record = await store.get_or_create_task(task_id="task-1", context_id="ctx-1")
    assert "nginx selected" in "".join(task_record.output_text)
    assert "input_received" in [event["eventType"] for event in A2APipelineJournal(session_dir).read_all()]


@pytest.mark.asyncio
async def test_pending_ask_resume_that_leaves_running_sidecar_allows_next_message_to_continue(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("IAC_CODE_MODE", "pipeline")

    class SuspendedResumePipeline(FakePipeline):
        def __init__(self, *, session_dir: Path) -> None:
            super().__init__([], session_dir=session_dir)
            self.ask_answers: list[dict[str, str]] = []
            self.interrupts: list[str] = []

        async def resume_ask_user_question(self, answer: dict[str, str], *, tool_use_id: str):
            self.ask_answers.append(answer)
            assert tool_use_id == "ask-1"
            self.sidecar_status = "running"
            if False:
                yield

        def continue_from_sidecar(self, user_input: str | None = None):
            self.continue_calls += 1
            self.continue_inputs.append(_display_text(user_input))

            async def stream():
                yield TextDeltaEvent(text="continued after suspended ask")
                self.sidecar_status = "completed"
                yield PipelineEvent(
                    type=PipelineEventType.PIPELINE_COMPLETED,
                    step_id=None,
                    timestamp=1717821601.0,
                    data={},
                )

            return stream()

        async def handle_user_interrupt(self, message: str) -> SimpleNamespace:
            self.interrupts.append(message)
            return SimpleNamespace(action="supplement", reason="wrong route")

    session_dir = tmp_path / "sidecar"
    pending = {
        "schemaVersion": "1.0",
        "extensionUri": "urn:iac-code:a2a:pipeline-events:v1",
        "eventId": "evt-ask",
        "sequence": 1,
        "createdAt": "2026-06-08T10:00:00Z",
        "eventType": "input_required",
        "scope": "step",
        "pipelineRunId": "ctx-1",
        "taskId": "task-1",
        "contextId": "ctx-1",
        "pipelineName": "selling",
        "status": "input_required",
        "step": {"runId": "step-intent_parsing-1", "id": "intent_parsing", "attempt": 1},
        "data": {"kind": "ask_user_question", "toolUseId": "ask-1"},
        "input": {
            "inputId": "ask-ask-1",
            "kind": "ask_user_question",
            "toolUseId": "ask-1",
            "question": "请选择部署目标",
            "options": [{"id": "skip", "label": "暂不处理，我只是随便测试一下"}],
            "allowFreeText": True,
        },
    }
    A2APipelineJournal(session_dir).append(pending)
    A2APipelineSnapshotStore(session_dir).save(reduce_pipeline_events([pending]))
    fake_pipeline = SuspendedResumePipeline(session_dir=session_dir)
    fake_pipeline.sidecar_status = "waiting_input"
    monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_pipeline", lambda *args, **kwargs: fake_pipeline)
    monkeypatch.setattr("iac_code.a2a.executor.create_agent_runtime", lambda options: _fake_runtime())
    monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_agent_runtime", lambda options: _fake_runtime())

    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    executor = IacCodeA2AExecutor(task_store=store, model="qwen3.6-plus")

    await executor.execute(
        FakeRequestContext(
            task_id="task-1",
            context_id="ctx-1",
            text="skip",
            metadata={"iac_code": {"cwd": str(tmp_path)}},
        ),
        FakeEventQueue(),
    )
    ctx = await store.get_or_create_context(
        context_id="ctx-1",
        cwd=str(tmp_path),
        runtime_factory=lambda _session_id: _fake_runtime(),
    )
    assert ctx.active_task_id is None

    await executor.execute(
        FakeRequestContext(
            task_id="task-1",
            context_id="ctx-1",
            text="继续",
            metadata={"iac_code": {"cwd": str(tmp_path)}},
        ),
        FakeEventQueue(),
    )

    assert fake_pipeline.ask_answers == [
        {"selected_id": "skip", "selected_label": "暂不处理，我只是随便测试一下", "free_text": ""}
    ]
    assert fake_pipeline.continue_inputs == ["继续"]
    assert fake_pipeline.interrupts == []


@pytest.mark.asyncio
async def test_executor_routes_waiting_input_sidecar_by_context_when_task_id_is_missing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("IAC_CODE_MODE", "pipeline")

    class AskResumePipeline(FakePipeline):
        def __init__(self, *, session_dir: Path) -> None:
            super().__init__(
                [
                    TextDeltaEvent(text="nginx selected"),
                    PipelineEvent(
                        type=PipelineEventType.PIPELINE_COMPLETED,
                        step_id=None,
                        timestamp=1717821601.0,
                        data={},
                    ),
                ],
                session_dir=session_dir,
            )
            self.ask_answers: list[dict[str, str]] = []

        async def resume_ask_user_question(self, answer: dict[str, str], *, tool_use_id: str):
            self.ask_answers.append(answer)
            assert tool_use_id == "ask-1"
            for event in self.events:
                yield event

    from iac_code.a2a.persistence import A2AContextSnapshot, A2APersistenceStore
    from iac_code.a2a.pipeline_paths import a2a_pipeline_dir_for_session

    persistence = A2APersistenceStore(tmp_path / "a2a")
    session_id = "session-ctx-1"
    persistence.save_context(A2AContextSnapshot(context_id="ctx-1", session_id=session_id, cwd=str(tmp_path)))
    session_dir = a2a_pipeline_dir_for_session(cwd=str(tmp_path), session_id=session_id)
    pending = {
        "schemaVersion": "1.0",
        "extensionUri": "urn:iac-code:a2a:pipeline-events:v1",
        "eventId": "evt-ask",
        "sequence": 1,
        "createdAt": "2026-06-08T10:00:00Z",
        "eventType": "input_required",
        "scope": "step",
        "pipelineRunId": "ctx-1",
        "taskId": "task-1",
        "contextId": "ctx-1",
        "pipelineName": "selling",
        "status": "input_required",
        "step": {"runId": "step-intent_parsing-1", "id": "intent_parsing", "attempt": 1},
        "data": {"kind": "ask_user_question", "toolUseId": "ask-1"},
        "input": {
            "inputId": "ask-ask-1",
            "kind": "ask_user_question",
            "toolUseId": "ask-1",
            "question": "请选择部署目标",
            "options": [{"id": "nginx", "label": "Nginx 网站"}],
            "allowFreeText": True,
        },
    }
    A2APipelineJournal(session_dir).append(pending)
    A2APipelineSnapshotStore(session_dir).save(reduce_pipeline_events([pending]))
    fake_pipeline = AskResumePipeline(session_dir=session_dir)
    fake_pipeline.session = SimpleNamespace()
    fake_pipeline.sidecar_status = "waiting_input"
    monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_pipeline", lambda *args, **kwargs: fake_pipeline)
    monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_agent_runtime", lambda options: _fake_runtime())

    store = A2ATaskStore(metrics=NoOpA2AMetrics(), persistence=persistence)
    executor = IacCodeA2AExecutor(task_store=store, model="qwen3.6-plus")

    await executor.execute(
        FakeRequestContext(
            task_id=None,
            context_id="ctx-1",
            text="Nginx 网站",
            metadata={"iac_code": {"cwd": str(tmp_path)}},
        ),
        FakeEventQueue(),
    )

    assert fake_pipeline.clear_sidecar_calls == 0
    assert fake_pipeline.resume_prompts == []
    assert fake_pipeline.ask_answers == [{"selected_id": "nginx", "selected_label": "Nginx 网站", "free_text": ""}]
    task_record = await store.get_or_create_task(task_id="task-1", context_id="ctx-1")
    assert "nginx selected" in "".join(task_record.output_text)


def test_waiting_input_task_id_from_sidecar_accepts_candidate_selection(tmp_path: Path) -> None:
    from iac_code.a2a.pipeline_executor import waiting_input_task_id_from_sidecar
    from iac_code.a2a.pipeline_paths import a2a_pipeline_dir_for_session

    cwd = tmp_path / "workspace"
    session_id = "session-ctx-1"
    context_id = "ctx-1"
    session_dir = a2a_pipeline_dir_for_session(cwd=str(cwd), session_id=session_id)
    pending = {
        "schemaVersion": "1.0",
        "extensionUri": "urn:iac-code:a2a:pipeline-events:v1",
        "eventId": "evt-selection",
        "sequence": 1,
        "createdAt": "2026-06-08T10:00:00Z",
        "eventType": "input_required",
        "scope": "step",
        "pipelineRunId": context_id,
        "taskId": "task-1",
        "contextId": context_id,
        "pipelineName": "selling",
        "status": "input_required",
        "step": {"runId": "step-confirm_and_select-1", "id": "confirm_and_select", "attempt": 1},
        "input": {
            "inputId": "input-confirm_and_select-1",
            "kind": "candidate_selection",
            "prompt": "请选择方案",
            "options": [{"name": "方案A", "candidate_index": 0}],
        },
    }
    A2APipelineJournal(session_dir).append(pending)
    A2APipelineSnapshotStore(session_dir).save(reduce_pipeline_events([pending]))

    assert waiting_input_task_id_from_sidecar(cwd=str(cwd), session_id=session_id, context_id=context_id) == "task-1"


def test_cancel_waiting_input_sidecar_appends_cancel_handoff_as_durable_group(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from iac_code.a2a.pipeline_executor import (
        WaitingInputCancelResult,
        cancel_waiting_input_task_from_sidecar,
        terminal_task_state_from_sidecar,
    )
    from iac_code.a2a.pipeline_paths import a2a_pipeline_dir_for_session

    cwd = tmp_path / "workspace"
    session_id = "session-ctx-1"
    context_id = "ctx-1"
    pipeline_dir = a2a_pipeline_dir_for_session(cwd=str(cwd), session_id=session_id)
    pending = {
        "schemaVersion": "1.0",
        "extensionUri": "urn:iac-code:a2a:pipeline-events:v1",
        "eventId": "evt-selection",
        "sequence": 1,
        "createdAt": "2026-06-08T10:00:00Z",
        "eventType": "input_required",
        "scope": "step",
        "pipelineRunId": context_id,
        "taskId": "task-1",
        "contextId": context_id,
        "pipelineName": "selling",
        "status": "input_required",
        "step": {"runId": "step-confirm_and_select-1", "id": "confirm_and_select", "attempt": 1},
        "input": {
            "inputId": "input-confirm_and_select-1",
            "kind": "candidate_selection",
            "prompt": "请选择方案",
            "options": [{"name": "方案A", "candidate_index": 0}],
        },
    }
    A2APipelineJournal(pipeline_dir).append(pending)
    A2APipelineSnapshotStore(pipeline_dir).save(reduce_pipeline_events([pending]))
    append_many_calls = []
    original_append_many = A2APipelineJournal.append_many

    def recording_append_many(self, events, durable: bool = False):
        append_many_calls.append(([event["eventType"] for event in events], durable))
        return original_append_many(self, events, durable=durable)

    monkeypatch.setattr(A2APipelineJournal, "append_many", recording_append_many)

    canceled = cancel_waiting_input_task_from_sidecar(
        cwd=str(cwd),
        session_id=session_id,
        context_id=context_id,
        task_id="task-1",
        reason="user canceled",
    )

    assert canceled == WaitingInputCancelResult.CANCELED
    assert append_many_calls[-2:] == [
        (["pipeline_canceled", "pipeline_handoff_ready"], True),
        (["backup_committed", "backup_committed"], True),
    ]
    events = A2APipelineJournal(pipeline_dir).read_all()
    assert [event["eventType"] for event in events[-4:]] == [
        "pipeline_canceled",
        "pipeline_handoff_ready",
        "backup_committed",
        "backup_committed",
    ]
    assert (
        terminal_task_state_from_sidecar(
            cwd=str(cwd),
            session_id=session_id,
            context_id=context_id,
            task_id="task-1",
        )
        == "canceled"
    )


@pytest.mark.asyncio
async def test_cancel_waiting_input_backup_sees_committed_cancel_and_mirrored_task(
    tmp_path: Path,
) -> None:
    from iac_code.a2a.pipeline_executor import WaitingInputCancelResult, cancel_waiting_input_task_from_sidecar
    from iac_code.a2a.pipeline_paths import a2a_pipeline_dir_for_session

    class InspectingBackupService:
        def backup_session(self, cwd_arg, session_id_arg, *, reason, critical) -> BackupResult:
            assert (cwd_arg, session_id_arg, reason, critical) == (
                str(cwd),
                session_id,
                BackupReason.TERMINAL,
                True,
            )
            events = A2APipelineJournal(pipeline_dir).read_all()
            assert [event.get("visibility") for event in events[-2:]] == ["committed", "committed"]
            task_snapshot = json.loads((session_dir / "a2a" / "task.json").read_text(encoding="utf-8"))
            context_snapshot = json.loads((session_dir / "a2a" / "context.json").read_text(encoding="utf-8"))
            assert task_snapshot["state"] == "input-required"
            assert context_snapshot["active_task_id"] == "task-1"
            return BackupResult(enabled=True, retry_count=1)

    cwd = tmp_path / "workspace"
    context_id = "ctx-1"
    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    task_record = await store.get_or_create_task(task_id="task-1", context_id=context_id)
    task_record.state = "input-required"
    store.mirror_task(task_record)
    context_record = await store.get_or_create_context(
        context_id=context_id,
        cwd=str(cwd),
        runtime_factory=lambda _session_id: _fake_runtime(),
    )
    context_record.active_task_id = "task-1"
    store.mirror_context(context_record)
    session_id = context_record.session_id
    session_dir = SessionStorage().session_dir(str(cwd), session_id)
    pipeline_dir = a2a_pipeline_dir_for_session(cwd=str(cwd), session_id=session_id)
    pending = {
        "schemaVersion": "1.0",
        "extensionUri": "urn:iac-code:a2a:pipeline-events:v1",
        "eventId": "evt-selection",
        "sequence": 1,
        "createdAt": "2026-06-08T10:00:00Z",
        "eventType": "input_required",
        "scope": "step",
        "pipelineRunId": context_id,
        "taskId": "task-1",
        "contextId": context_id,
        "pipelineName": "selling",
        "status": "input_required",
        "step": {"runId": "step-confirm_and_select-1", "id": "confirm_and_select", "attempt": 1},
        "input": {
            "inputId": "input-confirm_and_select-1",
            "kind": "candidate_selection",
            "prompt": "请选择方案",
            "options": [{"name": "方案A", "candidate_index": 0}],
        },
    }
    A2APipelineJournal(pipeline_dir).append(pending)
    A2APipelineSnapshotStore(pipeline_dir).save(reduce_pipeline_events([pending]))
    metrics = SpyMetrics()

    canceled = cancel_waiting_input_task_from_sidecar(
        cwd=str(cwd),
        session_id=session_id,
        context_id=context_id,
        task_id="task-1",
        reason="user canceled",
        backup_service=InspectingBackupService(),
        task_store=store,
        task_record=task_record,
        context_record=context_record,
        metrics=metrics,
    )

    assert canceled == WaitingInputCancelResult.CANCELED
    assert metrics.backup_succeeded == [(BackupReason.TERMINAL.value, True, 1)]
    assert metrics.backup_failed == []


def test_cancel_waiting_input_sidecar_returns_false_when_durable_group_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from iac_code.a2a.pipeline_executor import WaitingInputCancelResult, cancel_waiting_input_task_from_sidecar
    from iac_code.a2a.pipeline_paths import a2a_pipeline_dir_for_session

    cwd = tmp_path / "workspace"
    session_id = "session-ctx-1"
    context_id = "ctx-1"
    pipeline_dir = a2a_pipeline_dir_for_session(cwd=str(cwd), session_id=session_id)
    pending = {
        "schemaVersion": "1.0",
        "extensionUri": "urn:iac-code:a2a:pipeline-events:v1",
        "eventId": "evt-selection",
        "sequence": 1,
        "createdAt": "2026-06-08T10:00:00Z",
        "eventType": "input_required",
        "scope": "step",
        "pipelineRunId": context_id,
        "taskId": "task-1",
        "contextId": context_id,
        "pipelineName": "selling",
        "status": "input_required",
        "step": {"runId": "step-confirm_and_select-1", "id": "confirm_and_select", "attempt": 1},
        "input": {
            "inputId": "input-confirm_and_select-1",
            "kind": "candidate_selection",
            "prompt": "请选择方案",
            "options": [{"name": "方案A", "candidate_index": 0}],
        },
    }
    A2APipelineJournal(pipeline_dir).append(pending)
    A2APipelineSnapshotStore(pipeline_dir).save(reduce_pipeline_events([pending]))

    def fail_append_many(self, events, durable: bool = False):
        assert durable is True
        assert [event["eventType"] for event in events] == ["pipeline_canceled", "pipeline_handoff_ready"]
        raise OSError("journal locked")

    monkeypatch.setattr(A2APipelineJournal, "append_many", fail_append_many)

    canceled = cancel_waiting_input_task_from_sidecar(
        cwd=str(cwd),
        session_id=session_id,
        context_id=context_id,
        task_id="task-1",
        reason="user canceled",
    )

    assert canceled == WaitingInputCancelResult.PERSIST_FAILED
    assert [event["eventType"] for event in A2APipelineJournal(pipeline_dir).read_all()] == ["input_required"]
    snapshot = A2APipelineSnapshotStore(pipeline_dir).load()
    assert snapshot is not None
    assert snapshot["status"] == "waiting_input"


def test_cancel_waiting_input_backup_blocked_persist_failure_records_unavailable_marker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from iac_code.a2a.pipeline_executor import (
        WaitingInputCancelResult,
        cancel_waiting_input_task_from_sidecar,
        terminal_task_state_from_sidecar,
    )
    from iac_code.a2a.pipeline_paths import a2a_pipeline_dir_for_session

    class BlockingBackupService:
        def backup_session(self, *args, **kwargs) -> None:
            raise SessionBackupBlocked("backup unavailable at /tmp/secret-token", retry_count=2)

    cwd = tmp_path / "workspace"
    session_id = "session-ctx-1"
    context_id = "ctx-1"
    pipeline_dir = a2a_pipeline_dir_for_session(cwd=str(cwd), session_id=session_id)
    pending = {
        "schemaVersion": "1.0",
        "extensionUri": "urn:iac-code:a2a:pipeline-events:v1",
        "eventId": "evt-selection",
        "sequence": 1,
        "createdAt": "2026-06-08T10:00:00Z",
        "eventType": "input_required",
        "scope": "step",
        "pipelineRunId": context_id,
        "taskId": "task-1",
        "contextId": context_id,
        "pipelineName": "selling",
        "status": "input_required",
        "step": {"runId": "step-confirm_and_select-1", "id": "confirm_and_select", "attempt": 1},
        "input": {
            "inputId": "input-confirm_and_select-1",
            "kind": "candidate_selection",
            "prompt": "请选择方案",
            "options": [{"name": "方案A", "candidate_index": 0}],
        },
    }
    A2APipelineJournal(pipeline_dir).append(pending)
    A2APipelineSnapshotStore(pipeline_dir).save(reduce_pipeline_events([pending]))
    original_append = A2APipelineJournal.append

    def fail_backup_blocked_append(self, event, durable: bool = False):
        if event.get("eventType") == "backup_blocked":
            raise OSError("journal locked")
        return original_append(self, event, durable=durable)

    monkeypatch.setattr(A2APipelineJournal, "append", fail_backup_blocked_append)
    metrics = SpyMetrics()

    canceled = cancel_waiting_input_task_from_sidecar(
        cwd=str(cwd),
        session_id=session_id,
        context_id=context_id,
        task_id="task-1",
        reason="user canceled",
        backup_service=BlockingBackupService(),
        metrics=metrics,
    )

    assert canceled == WaitingInputCancelResult.BACKUP_BLOCKED_PERSIST_FAILED
    assert metrics.backup_blocked == [(BackupReason.TERMINAL.value, False)]
    events = A2APipelineJournal(pipeline_dir).read_all()
    assert [event["eventType"] for event in events] == [
        "input_required",
        "pipeline_canceled",
        "pipeline_handoff_ready",
        "pipeline_canceled",
        "pipeline_handoff_ready",
        "input_required",
    ]
    assert [event.get("visibility") for event in events[1:5]] == [
        "pending_backup",
        "pending_backup",
        "committed",
        "committed",
    ]
    assert events[-1]["data"]["kind"] == "terminal_publication_unavailable"
    snapshot = A2APipelineSnapshotStore(pipeline_dir).load()
    assert snapshot is not None
    assert snapshot["status"] == "waiting_input"
    assert snapshot["normalHandoff"] is None
    assert snapshot["pendingNormalHandoff"] is None
    assert (
        terminal_task_state_from_sidecar(
            cwd=str(cwd),
            session_id=session_id,
            context_id=context_id,
            task_id="task-1",
        )
        is None
    )


def test_cancel_waiting_input_failed_backup_result_persist_failure_records_unrecoverable_metric(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from iac_code.a2a.pipeline_executor import (
        WaitingInputCancelResult,
        cancel_waiting_input_task_from_sidecar,
    )
    from iac_code.a2a.pipeline_paths import a2a_pipeline_dir_for_session

    class FailedBackupService:
        def backup_session(self, *args, **kwargs) -> BackupResult:
            return BackupResult(
                enabled=True,
                succeeded=False,
                error="backup unavailable at /tmp/secret-token",
                retry_count=3,
            )

    cwd = tmp_path / "workspace"
    session_id = "session-ctx-1"
    context_id = "ctx-1"
    pipeline_dir = a2a_pipeline_dir_for_session(cwd=str(cwd), session_id=session_id)
    pending = {
        "schemaVersion": "1.0",
        "extensionUri": "urn:iac-code:a2a:pipeline-events:v1",
        "eventId": "evt-selection",
        "sequence": 1,
        "createdAt": "2026-06-08T10:00:00Z",
        "eventType": "input_required",
        "scope": "step",
        "pipelineRunId": context_id,
        "taskId": "task-1",
        "contextId": context_id,
        "pipelineName": "selling",
        "status": "input_required",
        "step": {"runId": "step-confirm_and_select-1", "id": "confirm_and_select", "attempt": 1},
        "input": {
            "inputId": "input-confirm_and_select-1",
            "kind": "candidate_selection",
            "prompt": "请选择方案",
            "options": [{"name": "方案A", "candidate_index": 0}],
        },
    }
    A2APipelineJournal(pipeline_dir).append(pending)
    A2APipelineSnapshotStore(pipeline_dir).save(reduce_pipeline_events([pending]))
    original_append = A2APipelineJournal.append

    def fail_backup_blocked_append(self, event, durable: bool = False):
        if event.get("eventType") == "backup_blocked":
            raise OSError("journal locked")
        return original_append(self, event, durable=durable)

    monkeypatch.setattr(A2APipelineJournal, "append", fail_backup_blocked_append)
    metrics = SpyMetrics()

    canceled = cancel_waiting_input_task_from_sidecar(
        cwd=str(cwd),
        session_id=session_id,
        context_id=context_id,
        task_id="task-1",
        reason="user canceled",
        backup_service=FailedBackupService(),
        metrics=metrics,
    )

    assert canceled == WaitingInputCancelResult.BACKUP_BLOCKED_PERSIST_FAILED
    assert metrics.backup_blocked == [(BackupReason.TERMINAL.value, False)]
    assert metrics.backup_failed == [(BackupReason.TERMINAL.value, True, 3)]


def test_cancel_waiting_input_backup_blocked_records_metric(tmp_path: Path) -> None:
    from iac_code.a2a.pipeline_executor import (
        WaitingInputCancelResult,
        cancel_waiting_input_task_from_sidecar,
    )
    from iac_code.a2a.pipeline_paths import a2a_pipeline_dir_for_session

    class BlockingBackupService:
        def backup_session(self, *args, **kwargs) -> None:
            raise SessionBackupBlocked("backup unavailable at /tmp/secret-token", retry_count=2)

    cwd = tmp_path / "workspace"
    session_id = "session-ctx-1"
    context_id = "ctx-1"
    pipeline_dir = a2a_pipeline_dir_for_session(cwd=str(cwd), session_id=session_id)
    pending = {
        "schemaVersion": "1.0",
        "extensionUri": "urn:iac-code:a2a:pipeline-events:v1",
        "eventId": "evt-selection",
        "sequence": 1,
        "createdAt": "2026-06-08T10:00:00Z",
        "eventType": "input_required",
        "scope": "step",
        "pipelineRunId": context_id,
        "taskId": "task-1",
        "contextId": context_id,
        "pipelineName": "selling",
        "status": "input_required",
        "step": {"runId": "step-confirm_and_select-1", "id": "confirm_and_select", "attempt": 1},
        "input": {
            "inputId": "input-confirm_and_select-1",
            "kind": "candidate_selection",
            "prompt": "请选择方案",
            "options": [{"name": "方案A", "candidate_index": 0}],
        },
    }
    A2APipelineJournal(pipeline_dir).append(pending)
    A2APipelineSnapshotStore(pipeline_dir).save(reduce_pipeline_events([pending]))
    metrics = SpyMetrics()

    canceled = cancel_waiting_input_task_from_sidecar(
        cwd=str(cwd),
        session_id=session_id,
        context_id=context_id,
        task_id="task-1",
        reason="user canceled",
        backup_service=BlockingBackupService(),
        metrics=metrics,
    )

    assert canceled == WaitingInputCancelResult.BACKUP_BLOCKED
    assert metrics.backup_blocked == [(BackupReason.TERMINAL.value, True)]
    assert metrics.backup_failed == [(BackupReason.TERMINAL.value, True, 2)]
    assert metrics.backup_succeeded == []
    assert A2APipelineJournal(pipeline_dir).read_all()[-1]["eventType"] == "backup_blocked"


def test_cancel_waiting_input_failed_backup_result_records_blocked_metric(tmp_path: Path) -> None:
    from iac_code.a2a.pipeline_executor import (
        WaitingInputCancelResult,
        cancel_waiting_input_task_from_sidecar,
    )
    from iac_code.a2a.pipeline_paths import a2a_pipeline_dir_for_session

    class FailedBackupService:
        def backup_session(self, *args, **kwargs) -> BackupResult:
            return BackupResult(
                enabled=True,
                succeeded=False,
                error="backup unavailable at /tmp/secret-token",
                retry_count=3,
            )

    cwd = tmp_path / "workspace"
    session_id = "session-ctx-1"
    context_id = "ctx-1"
    pipeline_dir = a2a_pipeline_dir_for_session(cwd=str(cwd), session_id=session_id)
    pending = {
        "schemaVersion": "1.0",
        "extensionUri": "urn:iac-code:a2a:pipeline-events:v1",
        "eventId": "evt-selection",
        "sequence": 1,
        "createdAt": "2026-06-08T10:00:00Z",
        "eventType": "input_required",
        "scope": "step",
        "pipelineRunId": context_id,
        "taskId": "task-1",
        "contextId": context_id,
        "pipelineName": "selling",
        "status": "input_required",
        "step": {"runId": "step-confirm_and_select-1", "id": "confirm_and_select", "attempt": 1},
        "input": {
            "inputId": "input-confirm_and_select-1",
            "kind": "candidate_selection",
            "prompt": "请选择方案",
            "options": [{"name": "方案A", "candidate_index": 0}],
        },
    }
    A2APipelineJournal(pipeline_dir).append(pending)
    A2APipelineSnapshotStore(pipeline_dir).save(reduce_pipeline_events([pending]))
    metrics = SpyMetrics()

    canceled = cancel_waiting_input_task_from_sidecar(
        cwd=str(cwd),
        session_id=session_id,
        context_id=context_id,
        task_id="task-1",
        reason="user canceled",
        backup_service=FailedBackupService(),
        metrics=metrics,
    )

    assert canceled == WaitingInputCancelResult.BACKUP_BLOCKED
    assert metrics.backup_blocked == [(BackupReason.TERMINAL.value, True)]
    assert metrics.backup_failed == [(BackupReason.TERMINAL.value, True, 3)]
    assert metrics.backup_succeeded == []
    assert A2APipelineJournal(pipeline_dir).read_all()[-1]["eventType"] == "backup_blocked"


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_mode", ["append_many", "snapshot_save", "fsync_after_write"])
async def test_cancel_waiting_input_committed_persist_failure_keeps_task_input_required(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_mode: str,
) -> None:
    from iac_code.a2a.pipeline_executor import (
        WaitingInputCancelResult,
        cancel_waiting_input_task_from_sidecar,
        recoverable_task_id_from_sidecar,
        terminal_task_state_from_sidecar,
    )
    from iac_code.a2a.pipeline_paths import a2a_pipeline_dir_for_session

    class SuccessfulBackupService:
        def __init__(self) -> None:
            self.calls = 0

        def backup_session(self, *args, **kwargs) -> None:
            self.calls += 1

    cwd = tmp_path / "workspace"
    context_id = "ctx-1"
    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    task_record = await store.get_or_create_task(task_id="task-1", context_id=context_id)
    task_record.state = "input-required"
    store.mirror_task(task_record)
    context_record = await store.get_or_create_context(
        context_id=context_id,
        cwd=str(cwd),
        runtime_factory=lambda _session_id: _fake_runtime(),
    )
    context_record.active_task_id = "task-1"
    store.mirror_context(context_record)
    session_id = context_record.session_id
    pipeline_dir = a2a_pipeline_dir_for_session(cwd=str(cwd), session_id=session_id)
    pending = {
        "schemaVersion": "1.0",
        "extensionUri": "urn:iac-code:a2a:pipeline-events:v1",
        "eventId": "evt-selection",
        "sequence": 1,
        "createdAt": "2026-06-08T10:00:00Z",
        "eventType": "input_required",
        "scope": "step",
        "pipelineRunId": context_id,
        "taskId": "task-1",
        "contextId": context_id,
        "pipelineName": "selling",
        "status": "input_required",
        "step": {"runId": "step-confirm_and_select-1", "id": "confirm_and_select", "attempt": 1},
        "input": {
            "inputId": "input-confirm_and_select-1",
            "kind": "candidate_selection",
            "prompt": "请选择方案",
            "options": [{"name": "方案A", "candidate_index": 0}],
        },
    }
    A2APipelineJournal(pipeline_dir).append(pending)
    A2APipelineSnapshotStore(pipeline_dir).save(reduce_pipeline_events([pending]))
    original_append_many = A2APipelineJournal.append_many
    append_many_calls = 0
    fail_during_committed_append = False

    def fail_committed_append_many(self, events, durable: bool = False):
        nonlocal append_many_calls, fail_during_committed_append
        append_many_calls += 1
        assert durable is True
        if failure_mode == "append_many" and append_many_calls == 2:
            raise OSError("journal locked")
        if failure_mode == "fsync_after_write" and append_many_calls == 2:
            fail_during_committed_append = True
            try:
                return original_append_many(self, events, durable=durable)
            finally:
                fail_during_committed_append = False
        return original_append_many(self, events, durable=durable)

    monkeypatch.setattr(A2APipelineJournal, "append_many", fail_committed_append_many)
    original_save = A2APipelineSnapshotStore.save
    save_calls = 0

    def fail_committed_snapshot_save(self, snapshot):
        nonlocal save_calls
        if Path(self.pipeline_dir) == pipeline_dir:
            save_calls += 1
            if failure_mode == "snapshot_save" and save_calls == 2:
                return False
        return original_save(self, snapshot)

    monkeypatch.setattr(A2APipelineSnapshotStore, "save", fail_committed_snapshot_save)
    if failure_mode == "fsync_after_write":
        from iac_code.a2a import pipeline_journal as pipeline_journal_module

        real_fsync = os.fsync
        raised = False

        def fail_committed_journal_fsync(fd: int) -> None:
            nonlocal raised
            if fail_during_committed_append and not raised:
                raised = True
                raise OSError("fsync failed after journal write")
            real_fsync(fd)

        monkeypatch.setattr(pipeline_journal_module.os, "fsync", fail_committed_journal_fsync)
    backup_service = SuccessfulBackupService()

    canceled = cancel_waiting_input_task_from_sidecar(
        cwd=str(cwd),
        session_id=session_id,
        context_id=context_id,
        task_id="task-1",
        reason="user canceled",
        backup_service=backup_service,
        task_store=store,
        task_record=task_record,
        context_record=context_record,
    )

    assert canceled == WaitingInputCancelResult.PERSIST_FAILED
    assert backup_service.calls == 0
    assert task_record.state == "input-required"
    assert context_record.active_task_id == "task-1"
    persisted_task = await store.get_task_record("task-1")
    assert persisted_task.state == "input-required"
    events = A2APipelineJournal(pipeline_dir).read_all()
    assert [event["eventType"] for event in events[:3]] == [
        "input_required",
        "pipeline_canceled",
        "pipeline_handoff_ready",
    ]
    assert all(event.get("visibility") == "pending_backup" for event in events[1:3])
    assert events[-1]["eventType"] == "input_required"
    assert events[-1]["data"]["kind"] == "terminal_publication_unavailable"
    snapshot = A2APipelineSnapshotStore(pipeline_dir).load()
    assert snapshot is not None
    assert snapshot["status"] == "waiting_input"
    assert snapshot["normalHandoff"] is None
    assert (
        terminal_task_state_from_sidecar(
            cwd=str(cwd),
            session_id=session_id,
            context_id=context_id,
            task_id="task-1",
        )
        is None
    )
    executor = IacCodeA2AExecutor(task_store=store, model="qwen3.6-plus")
    assert not await executor._should_route_pipeline_handoff_to_normal(context_id=context_id, cwd=str(cwd))
    assert (
        recoverable_task_id_from_sidecar(
            cwd=str(cwd),
            session_id=session_id,
            context_id=context_id,
            include_running=False,
        )
        == "task-1"
    )


def test_concurrent_cancel_waiting_input_sidecar_is_serialized(tmp_path: Path) -> None:
    from iac_code.a2a.pipeline_executor import WaitingInputCancelResult, cancel_waiting_input_task_from_sidecar
    from iac_code.a2a.pipeline_paths import a2a_pipeline_dir_for_session

    class BlockingBackupService:
        def __init__(self) -> None:
            self.calls = 0
            self._lock = threading.Lock()
            self.entered = threading.Event()
            self.second_entered = threading.Event()
            self.release = threading.Event()

        def backup_session(self, *args, **kwargs) -> None:
            with self._lock:
                self.calls += 1
                calls = self.calls
                if calls == 1:
                    self.entered.set()
                else:
                    self.second_entered.set()
            assert self.release.wait(timeout=2)

    cwd = tmp_path / "workspace"
    session_id = "session-ctx-1"
    context_id = "ctx-1"
    pipeline_dir = a2a_pipeline_dir_for_session(cwd=str(cwd), session_id=session_id)
    pending = {
        "schemaVersion": "1.0",
        "extensionUri": "urn:iac-code:a2a:pipeline-events:v1",
        "eventId": "evt-selection",
        "sequence": 1,
        "createdAt": "2026-06-08T10:00:00Z",
        "eventType": "input_required",
        "scope": "step",
        "pipelineRunId": context_id,
        "taskId": "task-1",
        "contextId": context_id,
        "pipelineName": "selling",
        "status": "input_required",
        "step": {"runId": "step-confirm_and_select-1", "id": "confirm_and_select", "attempt": 1},
        "input": {
            "inputId": "input-confirm_and_select-1",
            "kind": "candidate_selection",
            "prompt": "请选择方案",
            "options": [{"name": "方案A", "candidate_index": 0}],
        },
    }
    A2APipelineJournal(pipeline_dir).append(pending)
    A2APipelineSnapshotStore(pipeline_dir).save(reduce_pipeline_events([pending]))
    backup_service = BlockingBackupService()
    results: list[WaitingInputCancelResult] = []
    errors: list[BaseException] = []

    def cancel() -> None:
        try:
            results.append(
                cancel_waiting_input_task_from_sidecar(
                    cwd=str(cwd),
                    session_id=session_id,
                    context_id=context_id,
                    task_id="task-1",
                    reason="user canceled",
                    backup_service=backup_service,
                )
            )
        except BaseException as exc:
            errors.append(exc)

    first = threading.Thread(target=cancel)
    second = threading.Thread(target=cancel)
    first.start()
    assert backup_service.entered.wait(timeout=1)
    second.start()
    assert not backup_service.second_entered.wait(timeout=0.1)
    backup_service.release.set()
    first.join(timeout=2)
    second.join(timeout=2)

    assert not first.is_alive()
    assert not second.is_alive()
    assert errors == []
    assert backup_service.calls == 1
    assert sorted(results) == sorted([WaitingInputCancelResult.CANCELED, WaitingInputCancelResult.NOT_OWNER])
    events = A2APipelineJournal(pipeline_dir).read_all()
    assert [event["eventType"] for event in events].count("pipeline_canceled") == 2
    assert [event["eventType"] for event in events].count("pipeline_handoff_ready") == 2


def test_pending_backup_terminal_journal_is_not_terminal_authoritative(tmp_path: Path) -> None:
    from iac_code.a2a.pipeline_executor import (
        _latest_terminal_a2a_event,
        recoverable_task_id_from_sidecar,
        terminal_task_state_from_sidecar,
    )
    from iac_code.a2a.pipeline_paths import a2a_pipeline_dir_for_session

    cwd = tmp_path / "workspace"
    session_id = "session-ctx-1"
    context_id = "ctx-1"
    pipeline_dir = a2a_pipeline_dir_for_session(cwd=str(cwd), session_id=session_id)
    pending_input = {
        "schemaVersion": "1.0",
        "extensionUri": "urn:iac-code:a2a:pipeline-events:v1",
        "eventId": "evt-selection",
        "sequence": 1,
        "createdAt": "2026-06-08T10:00:00Z",
        "eventType": "input_required",
        "scope": "step",
        "pipelineRunId": context_id,
        "taskId": "task-1",
        "contextId": context_id,
        "pipelineName": "selling",
        "status": "input_required",
        "step": {"runId": "step-confirm_and_select-1", "id": "confirm_and_select", "attempt": 1},
        "input": {
            "inputId": "input-confirm_and_select-1",
            "kind": "candidate_selection",
            "prompt": "请选择方案",
            "options": [{"name": "方案A", "candidate_index": 0}],
        },
    }
    pending_terminal = {
        **pending_input,
        "eventId": "evt-pending-terminal",
        "sequence": 2,
        "eventType": "pipeline_canceled",
        "scope": "pipeline",
        "status": "canceled",
        "visibility": "pending_backup",
        "data": {"source": "a2a_cancel", "reason": "user canceled"},
    }
    pending_handoff = {
        **pending_input,
        "eventId": "evt-pending-handoff",
        "sequence": 3,
        "eventType": "pipeline_handoff_ready",
        "scope": "pipeline",
        "status": "canceled",
        "visibility": "pending_backup",
        "data": {
            "action": "switch_to_normal",
            "targetMode": "normal",
            "outcome": "canceled",
            "summary": "[Pipeline Handoff Context]\nOutcome: canceled",
        },
    }
    journal = A2APipelineJournal(pipeline_dir)
    journal.append_many([pending_input, pending_terminal, pending_handoff], durable=True)

    events = journal.read_all()
    assert _latest_terminal_a2a_event(events) is None
    assert (
        terminal_task_state_from_sidecar(
            cwd=str(cwd),
            session_id=session_id,
            context_id=context_id,
            task_id="task-1",
        )
        is None
    )
    assert (
        recoverable_task_id_from_sidecar(
            cwd=str(cwd),
            session_id=session_id,
            context_id=context_id,
            include_running=False,
        )
        == "task-1"
    )


def test_committed_backup_terminal_without_backup_ack_is_not_terminal_authoritative(tmp_path: Path) -> None:
    from iac_code.a2a.pipeline_executor import (
        _latest_terminal_a2a_event,
        terminal_task_state_from_sidecar,
    )
    from iac_code.a2a.pipeline_paths import a2a_pipeline_dir_for_session

    cwd = tmp_path / "workspace"
    session_id = "session-ctx-1"
    context_id = "ctx-1"
    pipeline_dir = a2a_pipeline_dir_for_session(cwd=str(cwd), session_id=session_id)
    committed_terminal = {
        "schemaVersion": "1.0",
        "extensionUri": "urn:iac-code:a2a:pipeline-events:v1",
        "eventId": "evt-terminal",
        "sequence": 1,
        "createdAt": "2026-06-08T10:00:00Z",
        "eventType": "pipeline_completed",
        "scope": "pipeline",
        "pipelineRunId": context_id,
        "taskId": "task-1",
        "contextId": context_id,
        "pipelineName": "selling",
        "status": "completed",
        "visibility": "committed",
        "data": {"totalSteps": 1},
    }
    journal = A2APipelineJournal(pipeline_dir)
    journal.append(committed_terminal, durable=True)
    A2APipelineSnapshotStore(pipeline_dir).save(reduce_pipeline_events([committed_terminal]))

    events = journal.read_all()
    assert _latest_terminal_a2a_event(events) is None
    assert (
        terminal_task_state_from_sidecar(
            cwd=str(cwd),
            session_id=session_id,
            context_id=context_id,
            task_id="task-1",
        )
        is None
    )


@pytest.mark.asyncio
async def test_executor_recovers_pending_ask_from_journal_when_snapshot_is_missing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("IAC_CODE_MODE", "pipeline")

    class AskResumePipeline(FakePipeline):
        def __init__(self, *, session_dir: Path) -> None:
            super().__init__(
                [
                    TextDeltaEvent(text="nginx selected"),
                    PipelineEvent(
                        type=PipelineEventType.PIPELINE_COMPLETED,
                        step_id=None,
                        timestamp=1717821601.0,
                        data={},
                    ),
                ],
                session_dir=session_dir,
            )
            self.ask_answers: list[dict[str, str]] = []

        async def resume_ask_user_question(self, answer: dict[str, str], *, tool_use_id: str):
            self.ask_answers.append(answer)
            assert tool_use_id == "ask-1"
            for event in self.events:
                yield event

    session_dir = tmp_path / "sidecar"
    pending = {
        "schemaVersion": "1.0",
        "extensionUri": "urn:iac-code:a2a:pipeline-events:v1",
        "eventId": "evt-ask",
        "sequence": 1,
        "createdAt": "2026-06-08T10:00:00Z",
        "eventType": "input_required",
        "scope": "step",
        "pipelineRunId": "ctx-1",
        "taskId": "task-1",
        "contextId": "ctx-1",
        "pipelineName": "selling",
        "status": "input_required",
        "step": {"runId": "step-intent_parsing-1", "id": "intent_parsing", "attempt": 1},
        "data": {"kind": "ask_user_question", "toolUseId": "ask-1"},
        "input": {
            "inputId": "ask-ask-1",
            "kind": "ask_user_question",
            "toolUseId": "ask-1",
            "question": "请选择部署目标",
            "options": [{"id": "nginx", "label": "Nginx 网站"}],
            "allowFreeText": True,
        },
    }
    A2APipelineJournal(session_dir).append(pending)
    fake_pipeline = AskResumePipeline(session_dir=session_dir)
    fake_pipeline.sidecar_status = "waiting_input"
    monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_pipeline", lambda *args, **kwargs: fake_pipeline)
    monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_agent_runtime", lambda options: _fake_runtime())

    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    executor = IacCodeA2AExecutor(task_store=store, model="qwen3.6-plus")

    await executor.execute(
        FakeRequestContext(
            task_id="task-1",
            context_id="ctx-1",
            text="Nginx 网站",
            metadata={"iac_code": {"cwd": str(tmp_path)}},
        ),
        FakeEventQueue(),
    )

    assert fake_pipeline.resume_prompts == []
    assert fake_pipeline.ask_answers == [{"selected_id": "nginx", "selected_label": "Nginx 网站", "free_text": ""}]


@pytest.mark.asyncio
async def test_executor_ignores_stale_snapshot_pending_ask_after_journal_input_received(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from iac_code.a2a.pipeline_executor import IacCodeA2APipelineExecutor

    monkeypatch.setenv("IAC_CODE_MODE", "pipeline")

    class RunningPipeline(FakePipeline):
        def __init__(self, *, session_dir: Path) -> None:
            super().__init__(
                [
                    TextDeltaEvent(text="continued"),
                    PipelineEvent(
                        type=PipelineEventType.PIPELINE_COMPLETED,
                        step_id=None,
                        timestamp=1717821601.0,
                        data={},
                    ),
                ],
                session_dir=session_dir,
            )
            self.ask_answers: list[dict[str, str]] = []

        async def resume_ask_user_question(self, answer: dict[str, str], *, tool_use_id: str):
            self.ask_answers.append(answer)
            raise AssertionError("stale pending ask should not be replayed")

    session_dir = tmp_path / "sidecar"
    pending = {
        "schemaVersion": "1.0",
        "extensionUri": "urn:iac-code:a2a:pipeline-events:v1",
        "eventId": "evt-ask",
        "sequence": 1,
        "createdAt": "2026-06-08T10:00:00Z",
        "eventType": "input_required",
        "scope": "step",
        "pipelineRunId": "ctx-1",
        "taskId": "task-1",
        "contextId": "ctx-1",
        "pipelineName": "selling",
        "status": "input_required",
        "step": {"runId": "step-intent_parsing-1", "id": "intent_parsing", "attempt": 1},
        "data": {"kind": "ask_user_question", "toolUseId": "ask-1"},
        "input": {
            "inputId": "ask-ask-1",
            "kind": "ask_user_question",
            "toolUseId": "ask-1",
            "question": "请选择部署目标",
            "options": [{"id": "nginx", "label": "Nginx 网站"}],
            "allowFreeText": True,
        },
    }
    received = {
        **pending,
        "eventId": "evt-answer",
        "sequence": 2,
        "eventType": "input_received",
        "status": "working",
        "data": {"kind": "ask_user_question", "toolUseId": "ask-1"},
        "input": None,
    }
    A2APipelineSnapshotStore(session_dir).save(reduce_pipeline_events([pending]))
    journal = A2APipelineJournal(session_dir)
    journal.append(pending)
    journal.append(received)
    fake_pipeline = RunningPipeline(session_dir=session_dir)
    fake_pipeline.sidecar_status = "running"
    monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_pipeline", lambda *args, **kwargs: fake_pipeline)
    monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_agent_runtime", lambda options: _fake_runtime())

    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    executor = IacCodeA2APipelineExecutor(
        task_store=store,
        model="qwen3.6-plus",
        metrics=NoOpA2AMetrics(),
        artifact_store=None,
        push_notifier=None,
        permission_resolver=None,
        auto_approve_permissions=False,
        thinking_exposure_types=None,
    )

    await executor.execute(
        context=FakeRequestContext(
            task_id="task-1",
            context_id="ctx-1",
            text="Nginx 网站",
            metadata={"iac_code": {"cwd": str(tmp_path)}},
        ),
        event_queue=FakeEventQueue(),
        task=await store.get_or_create_task(task_id="task-1", context_id="ctx-1"),
        task_id="task-1",
        context_id="ctx-1",
        cwd=str(tmp_path),
        prompt="Nginx 网站",
    )

    assert fake_pipeline.ask_answers == []
    assert fake_pipeline.continue_inputs == ["Nginx 网站"]


@pytest.mark.asyncio
async def test_pipeline_executor_rejects_new_task_while_context_has_active_pipeline(tmp_path: Path) -> None:
    from iac_code.a2a.pipeline_events import PipelineA2AContext, PipelineEventTranslator
    from iac_code.a2a.pipeline_executor import A2APipelineRuntime, IacCodeA2APipelineExecutor
    from iac_code.a2a.pipeline_journal import A2APipelineJournal
    from iac_code.a2a.pipeline_snapshot import A2APipelineSnapshotStore
    from iac_code.a2a.pipeline_stream import PipelineA2AEventPublisher

    class InterruptiblePipeline(FakePipeline):
        def __init__(self, *, session_dir: Path) -> None:
            super().__init__([], session_dir=session_dir)
            self.interrupts: list[str] = []

        async def handle_user_interrupt(self, message: str) -> SimpleNamespace:
            self.interrupts.append(message)
            return SimpleNamespace(
                action="supplement",
                reason="added context",
                rollback_target=None,
                candidate_scope=None,
            )

    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    active_task = await store.get_or_create_task(task_id="active-task", context_id="ctx-1")
    active_task.state = "working"
    active_task.active_task = asyncio.current_task()
    new_task = await store.get_or_create_task(task_id="new-task", context_id="ctx-1")
    ctx = await store.get_or_create_context(
        context_id="ctx-1",
        cwd=str(tmp_path),
        runtime_factory=lambda _session_id: _fake_runtime(),
    )
    ctx.active_task_id = "active-task"
    active_task_handle = active_task.active_task

    queue = FakeEventQueue()
    pipeline = InterruptiblePipeline(session_dir=tmp_path / "sidecar")
    publisher = PipelineA2AEventPublisher(
        event_queue=queue,
        translator=PipelineEventTranslator(
            PipelineA2AContext(
                pipeline_run_id="ctx-1",
                task_id="active-task",
                context_id="ctx-1",
                pipeline_name="selling",
            )
        ),
        journal=A2APipelineJournal(tmp_path / "pipeline"),
        snapshot_store=A2APipelineSnapshotStore(tmp_path / "pipeline"),
    )
    ctx.runtime = A2APipelineRuntime(agent_runtime=_fake_runtime(), pipeline=pipeline, publisher=publisher)
    store.mirror_task(active_task)
    store.mirror_context(ctx)

    executor = IacCodeA2APipelineExecutor(
        task_store=store,
        model="qwen3.6-plus",
        metrics=NoOpA2AMetrics(),
        artifact_store=None,
        push_notifier=None,
        permission_resolver=None,
        auto_approve_permissions=False,
        thinking_exposure_types=None,
    )

    await executor.execute(
        context=FakeRequestContext(
            task_id="new-task",
            context_id="ctx-1",
            text="please change cpu",
            metadata={"iac_code": {"cwd": str(tmp_path)}},
        ),
        event_queue=queue,
        task=new_task,
        task_id="new-task",
        context_id="ctx-1",
        cwd=str(tmp_path),
        prompt="please change cpu",
    )

    assert pipeline.interrupts == []
    assert publisher.journal.read_all() == []
    final_status = _status_events(queue)[-1]["status"]
    assert final_status["state"] == "TASK_STATE_FAILED"
    assert final_status["message"]["parts"][0]["text"] == "Task is already working."
    assert new_task.state == "failed"
    assert ctx.active_task_id == "active-task"
    assert active_task.active_task is active_task_handle
    assert active_task.state == "working"


@pytest.mark.asyncio
async def test_parent_hard_interrupt_closes_active_stream_and_restarts(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from iac_code.a2a.pipeline_executor import IacCodeA2APipelineExecutor

    class RestartablePipeline(FakePipeline):
        def __init__(self, *, session_dir: Path) -> None:
            super().__init__([], session_dir=session_dir)
            self.primary_stream = CloseableEventStream([TextDeltaEvent(text="before interrupt")])
            self.restart_stream = CloseableEventStream(
                [
                    TextDeltaEvent(text="after interrupt"),
                    PipelineEvent(
                        type=PipelineEventType.PIPELINE_COMPLETED,
                        step_id=None,
                        timestamp=1717821602.0,
                        data={},
                    ),
                ],
                wait_until_closed=False,
            )
            self.interrupts: list[str] = []
            self.applied_verdicts: list[SimpleNamespace] = []
            self.applied_source_inputs: list[str] = []
            self.continue_after_interrupt_calls = 0

        def run(self, prompt: str):
            self.run_prompts.append(prompt)
            return self.primary_stream

        def continue_after_interrupt(self):
            self.continue_after_interrupt_calls += 1
            return self.restart_stream

        async def handle_user_interrupt(self, message: str) -> SimpleNamespace:
            self.interrupts.append(message)
            return SimpleNamespace(
                action="hard_interrupt",
                reason="changed parent plan",
                rollback_target="architecture_planning",
                candidate_scope=None,
            )

        def apply_hard_interrupt(self, verdict: SimpleNamespace, *, source_input: str | None = None) -> bool:
            self.applied_verdicts.append(verdict)
            if source_input is not None:
                self.applied_source_inputs.append(source_input)
            return True

    pipeline = RestartablePipeline(session_dir=tmp_path / "sidecar")
    monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_pipeline", lambda *args, **kwargs: pipeline)
    monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_agent_runtime", lambda options: _fake_runtime())

    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    active_task = await store.get_or_create_task(task_id="task-1", context_id="ctx-1")
    executor = IacCodeA2APipelineExecutor(
        task_store=store,
        model="qwen3.6-plus",
        metrics=NoOpA2AMetrics(),
        artifact_store=None,
        push_notifier=None,
        permission_resolver=None,
        auto_approve_permissions=False,
        thinking_exposure_types=None,
    )
    active_queue = FakeEventQueue()
    runner = asyncio.create_task(
        executor.execute(
            context=FakeRequestContext(metadata={"iac_code": {"cwd": str(tmp_path)}}),
            event_queue=active_queue,
            task=active_task,
            task_id="task-1",
            context_id="ctx-1",
            cwd=str(tmp_path),
            prompt="build ecs",
        )
    )
    await asyncio.wait_for(pipeline.primary_stream.started.wait(), timeout=_A2A_ASYNC_TEST_TIMEOUT)
    await _wait_for_output_text(active_task, "before interrupt")

    try:
        await executor.execute(
            context=FakeRequestContext(
                text="please change cpu",
                metadata={"iac_code": {"cwd": str(tmp_path)}},
            ),
            event_queue=FakeEventQueue(),
            task=active_task,
            task_id="task-1",
            context_id="ctx-1",
            cwd=str(tmp_path),
            prompt="please change cpu",
        )

        await asyncio.wait_for(pipeline.primary_stream.closed_event.wait(), timeout=_A2A_ASYNC_TEST_TIMEOUT)
        assert pipeline.primary_stream.closed is True
        await asyncio.wait_for(runner, timeout=_A2A_ASYNC_TEST_TIMEOUT)
        assert pipeline.continue_after_interrupt_calls == 1
        assert pipeline.applied_source_inputs == ["please change cpu"]
        assert "".join(active_task.output_text) == "before interruptafter interrupt"
    finally:
        if not pipeline.primary_stream.closed:
            await pipeline.primary_stream.aclose()
        if not runner.done():
            runner.cancel()
            try:
                await runner
            except asyncio.CancelledError:
                pass


@pytest.mark.asyncio
async def test_parent_hard_interrupt_waits_before_publishing_racing_terminal_event(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from iac_code.a2a.pipeline_executor import IacCodeA2APipelineExecutor

    class RacingTerminalPipeline(FakePipeline):
        def __init__(self, *, session_dir: Path) -> None:
            super().__init__([], session_dir=session_dir)
            self.restart_stream = CloseableEventStream(
                [
                    TextDeltaEvent(text="after interrupt"),
                    PipelineEvent(
                        type=PipelineEventType.PIPELINE_COMPLETED,
                        step_id=None,
                        timestamp=1717821602.0,
                        data={},
                    ),
                ],
                wait_until_closed=False,
            )
            self.interrupt_started = asyncio.Event()
            self.allow_interrupt = asyncio.Event()
            self.allow_terminal = asyncio.Event()
            self.continue_after_interrupt_calls = 0

        def run(self, prompt: str):
            self.run_prompts.append(prompt)
            return self._primary_stream()

        async def _primary_stream(self):
            yield TextDeltaEvent(text="before interrupt")
            await self.allow_terminal.wait()
            yield PipelineEvent(
                type=PipelineEventType.PIPELINE_COMPLETED,
                step_id=None,
                timestamp=1717821601.0,
                data={},
            )

        def continue_after_interrupt(self):
            self.continue_after_interrupt_calls += 1
            return self.restart_stream

        async def handle_user_interrupt(self, message: str) -> SimpleNamespace:
            self.interrupt_started.set()
            await self.allow_interrupt.wait()
            return SimpleNamespace(
                action="hard_interrupt",
                reason="changed parent plan",
                rollback_target="architecture_planning",
                candidate_scope=None,
            )

        def apply_hard_interrupt(self, verdict: SimpleNamespace) -> bool:
            return True

    pipeline = RacingTerminalPipeline(session_dir=tmp_path / "sidecar")
    monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_pipeline", lambda *args, **kwargs: pipeline)
    monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_agent_runtime", lambda options: _fake_runtime())

    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    active_task = await store.get_or_create_task(task_id="task-1", context_id="ctx-1")
    executor = IacCodeA2APipelineExecutor(
        task_store=store,
        model="qwen3.6-plus",
        metrics=NoOpA2AMetrics(),
        artifact_store=None,
        push_notifier=None,
        permission_resolver=None,
        auto_approve_permissions=False,
        thinking_exposure_types=None,
    )
    queue = FakeEventQueue()
    runner = asyncio.create_task(
        executor.execute(
            context=FakeRequestContext(metadata={"iac_code": {"cwd": str(tmp_path)}}),
            event_queue=queue,
            task=active_task,
            task_id="task-1",
            context_id="ctx-1",
            cwd=str(tmp_path),
            prompt="build ecs",
        )
    )
    await _wait_for_output_text(active_task, "before interrupt")

    try:
        interrupt = asyncio.create_task(
            executor.execute(
                context=FakeRequestContext(
                    text="please change cpu",
                    metadata={"iac_code": {"cwd": str(tmp_path)}},
                ),
                event_queue=FakeEventQueue(),
                task=active_task,
                task_id="task-1",
                context_id="ctx-1",
                cwd=str(tmp_path),
                prompt="please change cpu",
            )
        )
        await asyncio.wait_for(pipeline.interrupt_started.wait(), timeout=_A2A_ASYNC_TEST_TIMEOUT)
        pipeline.allow_terminal.set()
        await asyncio.sleep(0.05)

        assert runner.done() is False

        pipeline.allow_interrupt.set()
        await asyncio.wait_for(interrupt, timeout=_A2A_ASYNC_TEST_TIMEOUT)
        await asyncio.wait_for(runner, timeout=_A2A_ASYNC_TEST_TIMEOUT)

        event_types = [event["eventType"] for event in _pipeline_status_events(queue)]
        assert event_types.count("pipeline_completed") == 1
        assert pipeline.continue_after_interrupt_calls == 1
        assert "".join(active_task.output_text) == "before interruptafter interrupt"
    finally:
        pipeline.allow_interrupt.set()
        pipeline.allow_terminal.set()
        if not runner.done():
            runner.cancel()
            try:
                await runner
            except asyncio.CancelledError:
                pass


@pytest.mark.asyncio
async def test_parent_hard_interrupt_cancels_blocked_async_generator_before_restart(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from iac_code.a2a.pipeline_executor import IacCodeA2APipelineExecutor

    class BlockedGeneratorPipeline(FakePipeline):
        def __init__(self, *, session_dir: Path) -> None:
            super().__init__([], session_dir=session_dir)
            self.generator_blocked = asyncio.Event()
            self.primary_cancelled = asyncio.Event()
            self.primary_closed = asyncio.Event()
            self.release_stale = asyncio.Event()
            self.continue_after_interrupt_calls = 0
            self.interrupts: list[str] = []

        async def run(self, prompt: str):
            self.run_prompts.append(prompt)
            yield TextDeltaEvent(text="before interrupt")
            self.generator_blocked.set()
            try:
                await self.release_stale.wait()
            except asyncio.CancelledError:
                self.primary_cancelled.set()
                raise
            finally:
                self.primary_closed.set()
            yield TextDeltaEvent(text="stale")

        def continue_after_interrupt(self):
            self.continue_after_interrupt_calls += 1
            return self._restart_stream()

        async def _restart_stream(self):
            yield TextDeltaEvent(text="after interrupt")
            yield PipelineEvent(
                type=PipelineEventType.PIPELINE_COMPLETED,
                step_id=None,
                timestamp=1717821602.0,
                data={},
            )

        async def handle_user_interrupt(self, message: str) -> SimpleNamespace:
            self.interrupts.append(message)
            return SimpleNamespace(
                action="hard_interrupt",
                reason="changed parent plan",
                rollback_target="architecture_planning",
                candidate_scope=None,
            )

        def apply_hard_interrupt(self, verdict: SimpleNamespace) -> bool:
            return True

    pipeline = BlockedGeneratorPipeline(session_dir=tmp_path / "sidecar")
    monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_pipeline", lambda *args, **kwargs: pipeline)
    monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_agent_runtime", lambda options: _fake_runtime())

    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    active_task = await store.get_or_create_task(task_id="task-1", context_id="ctx-1")
    executor = IacCodeA2APipelineExecutor(
        task_store=store,
        model="qwen3.6-plus",
        metrics=NoOpA2AMetrics(),
        artifact_store=None,
        push_notifier=None,
        permission_resolver=None,
        auto_approve_permissions=False,
        thinking_exposure_types=None,
    )
    runner = asyncio.create_task(
        executor.execute(
            context=FakeRequestContext(metadata={"iac_code": {"cwd": str(tmp_path)}}),
            event_queue=FakeEventQueue(),
            task=active_task,
            task_id="task-1",
            context_id="ctx-1",
            cwd=str(tmp_path),
            prompt="build ecs",
        )
    )
    await asyncio.wait_for(pipeline.generator_blocked.wait(), timeout=_A2A_ASYNC_TEST_TIMEOUT)

    try:
        await executor.execute(
            context=FakeRequestContext(
                text="please change cpu",
                metadata={"iac_code": {"cwd": str(tmp_path)}},
            ),
            event_queue=FakeEventQueue(),
            task=active_task,
            task_id="task-1",
            context_id="ctx-1",
            cwd=str(tmp_path),
            prompt="please change cpu",
        )

        await asyncio.wait_for(pipeline.primary_cancelled.wait(), timeout=_A2A_ASYNC_TEST_TIMEOUT)
        await asyncio.wait_for(pipeline.primary_closed.wait(), timeout=_A2A_ASYNC_TEST_TIMEOUT)
        await asyncio.wait_for(runner, timeout=_A2A_ASYNC_TEST_TIMEOUT)
        assert pipeline.continue_after_interrupt_calls == 1
        assert "".join(active_task.output_text) == "before interruptafter interrupt"
    finally:
        pipeline.release_stale.set()
        if not runner.done():
            runner.cancel()
            try:
                await runner
            except asyncio.CancelledError:
                pass


@pytest.mark.asyncio
async def test_canceled_pipeline_run_closes_blocked_stream_without_child_task_leak(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from iac_code.a2a.pipeline_executor import IacCodeA2APipelineExecutor

    class CancellablePipeline(FakePipeline):
        def __init__(self, *, session_dir: Path) -> None:
            super().__init__([], session_dir=session_dir)
            self.generator_blocked = asyncio.Event()
            self.primary_cancelled = asyncio.Event()
            self.primary_closed = asyncio.Event()
            self.release_stale = asyncio.Event()

        async def run(self, prompt: str):
            self.run_prompts.append(prompt)
            self.generator_blocked.set()
            try:
                await self.release_stale.wait()
            except asyncio.CancelledError:
                self.primary_cancelled.set()
                raise
            finally:
                self.primary_closed.set()
            yield TextDeltaEvent(text="stale")

    cwd = tmp_path / "workspace"
    cwd.mkdir()
    pipeline_holder: dict[str, CancellablePipeline] = {}
    pipeline_created = asyncio.Event()

    def fake_create_pipeline(*args, **kwargs):
        session_dir = SessionStorage().session_dir(str(cwd), kwargs["session_id"]) / "pipeline"
        pipeline = CancellablePipeline(session_dir=session_dir)
        pipeline.handoff_enabled = True
        pipeline.handoff_summary = "[Pipeline Handoff Context]\nOutcome: canceled"
        pipeline_holder["pipeline"] = pipeline
        pipeline_created.set()
        return pipeline

    monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_pipeline", fake_create_pipeline)
    monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_agent_runtime", lambda options: _fake_runtime())

    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    active_task = await store.get_or_create_task(task_id="task-1", context_id="ctx-1")
    executor = IacCodeA2APipelineExecutor(
        task_store=store,
        model="qwen3.6-plus",
        metrics=NoOpA2AMetrics(),
        artifact_store=None,
        push_notifier=None,
        permission_resolver=None,
        auto_approve_permissions=False,
        thinking_exposure_types=None,
    )
    runner = asyncio.create_task(
        executor.execute(
            context=FakeRequestContext(metadata={"iac_code": {"cwd": str(tmp_path)}}),
            event_queue=FakeEventQueue(),
            task=active_task,
            task_id="task-1",
            context_id="ctx-1",
            cwd=str(cwd),
            prompt="build ecs",
        )
    )
    await asyncio.wait_for(pipeline_created.wait(), timeout=_A2A_ASYNC_TEST_TIMEOUT)
    pipeline = pipeline_holder["pipeline"]
    await asyncio.wait_for(pipeline.generator_blocked.wait(), timeout=_A2A_ASYNC_TEST_TIMEOUT)

    try:
        runner.cancel()
        await asyncio.wait_for(runner, timeout=_A2A_ASYNC_TEST_TIMEOUT)
        await asyncio.sleep(0)

        pending_coro_names = _pending_coro_names()
        assert "_next_stream_event" not in pending_coro_names
        assert "Event.wait" not in pending_coro_names
        assert pipeline.primary_cancelled.is_set()
        assert pipeline.primary_closed.is_set()
        session_id = store._contexts["ctx-1"].session_id
        session_dir = SessionStorage().session_dir(str(cwd), session_id)
        assert pipeline.session.session_dir == session_dir / "pipeline"
        a2a_pipeline_dir = session_dir / "a2a" / "pipeline"
        events = A2APipelineJournal(a2a_pipeline_dir).read_all()
        assert [event["eventType"] for event in events[-4:]] == [
            "pipeline_canceled",
            "pipeline_handoff_ready",
            "backup_committed",
            "backup_committed",
        ]
        assert events[-4]["status"] == "canceled"
        assert events[-3]["status"] == "canceled"
        assert events[-3]["data"]["outcome"] == "canceled"
        assert events[-3]["data"]["summary"] == "[Pipeline Handoff Context]\nOutcome: canceled"
        assert [event["data"]["committedEventType"] for event in events[-2:]] == [
            "pipeline_canceled",
            "pipeline_handoff_ready",
        ]
        snapshot = A2APipelineSnapshotStore(a2a_pipeline_dir).load()
        assert snapshot is not None
        assert snapshot["status"] == "canceled"
        assert snapshot["normalHandoff"]["outcome"] == "canceled"
    finally:
        pipeline.release_stale.set()
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_candidate_hard_interrupt_does_not_close_or_restart_parent_stream(tmp_path: Path) -> None:
    from iac_code.a2a.pipeline_events import PipelineA2AContext, PipelineEventTranslator
    from iac_code.a2a.pipeline_executor import A2APipelineRuntime, IacCodeA2APipelineExecutor
    from iac_code.a2a.pipeline_journal import A2APipelineJournal
    from iac_code.a2a.pipeline_snapshot import A2APipelineSnapshotStore
    from iac_code.a2a.pipeline_stream import PipelineA2AEventPublisher

    class CandidateInterruptPipeline(FakePipeline):
        async def handle_user_interrupt(self, message: str) -> SimpleNamespace:
            return SimpleNamespace(
                action="hard_interrupt",
                reason=message,
                rollback_target="template_generating",
                candidate_scope="candidate-0",
            )

        def apply_hard_interrupt(self, verdict: SimpleNamespace) -> bool:
            return False

    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    task = await store.get_or_create_task(task_id="task-1", context_id="ctx-1")
    ctx = await store.get_or_create_context(
        context_id="ctx-1",
        cwd=str(tmp_path),
        runtime_factory=lambda _session_id: _fake_runtime(),
    )
    ctx.active_task_id = "task-1"

    queue = FakeEventQueue()
    pipeline = CandidateInterruptPipeline([], session_dir=tmp_path / "sidecar")
    stream = CloseableEventStream([])
    publisher = PipelineA2AEventPublisher(
        event_queue=queue,
        translator=PipelineEventTranslator(
            PipelineA2AContext(
                pipeline_run_id="ctx-1",
                task_id="task-1",
                context_id="ctx-1",
                pipeline_name="selling",
            )
        ),
        journal=A2APipelineJournal(tmp_path / "pipeline"),
        snapshot_store=A2APipelineSnapshotStore(tmp_path / "pipeline"),
    )
    runtime = A2APipelineRuntime(agent_runtime=_fake_runtime(), pipeline=pipeline, publisher=publisher)
    runtime.current_stream = stream
    runtime.restart_after_interrupt = False
    ctx.runtime = runtime

    executor = IacCodeA2APipelineExecutor(
        task_store=store,
        model="qwen3.6-plus",
        metrics=NoOpA2AMetrics(),
        artifact_store=None,
        push_notifier=None,
        permission_resolver=None,
        auto_approve_permissions=False,
        thinking_exposure_types=None,
    )

    await executor.execute(
        context=FakeRequestContext(text="candidate change", metadata={"iac_code": {"cwd": str(tmp_path)}}),
        event_queue=queue,
        task=task,
        task_id="task-1",
        context_id="ctx-1",
        cwd=str(tmp_path),
        prompt="candidate change",
    )

    assert stream.closed is False
    assert runtime.restart_after_interrupt is False


@pytest.mark.asyncio
async def test_hard_interrupt_failure_marks_active_task_failed_without_restart(tmp_path: Path) -> None:
    from iac_code.a2a.pipeline_events import PipelineA2AContext, PipelineEventTranslator
    from iac_code.a2a.pipeline_executor import A2APipelineRuntime, IacCodeA2APipelineExecutor
    from iac_code.a2a.pipeline_journal import A2APipelineJournal
    from iac_code.a2a.pipeline_snapshot import A2APipelineSnapshotStore
    from iac_code.a2a.pipeline_stream import PipelineA2AEventPublisher

    class FailedInterruptPipeline(FakePipeline):
        def __init__(self, *args, **kwargs) -> None:
            super().__init__(*args, **kwargs)
            self.resume_calls = 0

        async def handle_user_interrupt(self, message: str) -> SimpleNamespace:
            return SimpleNamespace(
                action="hard_interrupt",
                reason=message,
                rollback_target="missing",
                candidate_scope=None,
            )

        def apply_hard_interrupt(self, verdict: SimpleNamespace) -> bool:
            self.sidecar_status = "failed"
            return False

        def resume_agent_loops(self) -> None:
            self.resume_calls += 1

    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    task = await store.get_or_create_task(task_id="task-1", context_id="ctx-1")
    ctx = await store.get_or_create_context(
        context_id="ctx-1",
        cwd=str(tmp_path),
        runtime_factory=lambda _session_id: _fake_runtime(),
    )
    ctx.active_task_id = "task-1"

    queue = FakeEventQueue()
    pipeline = FailedInterruptPipeline([], session_dir=tmp_path / "sidecar")
    publisher = PipelineA2AEventPublisher(
        event_queue=queue,
        translator=PipelineEventTranslator(
            PipelineA2AContext(
                pipeline_run_id="ctx-1",
                task_id="task-1",
                context_id="ctx-1",
                pipeline_name="selling",
            )
        ),
        journal=A2APipelineJournal(tmp_path / "pipeline"),
        snapshot_store=A2APipelineSnapshotStore(tmp_path / "pipeline"),
    )
    runtime = A2APipelineRuntime(agent_runtime=_fake_runtime(), pipeline=pipeline, publisher=publisher)
    runtime.current_stream = CloseableEventStream([])
    ctx.runtime = runtime

    executor = IacCodeA2APipelineExecutor(
        task_store=store,
        model="qwen3.6-plus",
        metrics=NoOpA2AMetrics(),
        artifact_store=None,
        push_notifier=None,
        permission_resolver=None,
        auto_approve_permissions=False,
        thinking_exposure_types=None,
    )

    await executor.execute(
        context=FakeRequestContext(text="change architecture", metadata={"iac_code": {"cwd": str(tmp_path)}}),
        event_queue=queue,
        task=task,
        task_id="task-1",
        context_id="ctx-1",
        cwd=str(tmp_path),
        prompt="change architecture",
    )

    assert task.state == "failed"
    assert runtime.restart_after_interrupt is False
    assert runtime.pause_after_interrupt is True
    assert runtime.restart_requested.is_set() is True
    assert pipeline.resume_calls == 0


@pytest.mark.asyncio
async def test_escalated_candidate_interrupt_publishes_parent_rollback(tmp_path: Path) -> None:
    from iac_code.a2a.pipeline_events import PipelineA2AContext, PipelineEventTranslator
    from iac_code.a2a.pipeline_executor import A2APipelineRuntime, IacCodeA2APipelineExecutor
    from iac_code.a2a.pipeline_journal import A2APipelineJournal
    from iac_code.a2a.pipeline_snapshot import A2APipelineSnapshotStore
    from iac_code.a2a.pipeline_stream import PipelineA2AEventPublisher

    class EscalatingInterruptPipeline(FakePipeline):
        async def handle_user_interrupt(self, message: str) -> SimpleNamespace:
            return SimpleNamespace(
                action="hard_interrupt",
                reason=message,
                rollback_target="architecture_planning",
                candidate_scope="all",
            )

        def apply_hard_interrupt(self, verdict: SimpleNamespace) -> bool:
            return True

    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    task = await store.get_or_create_task(task_id="task-1", context_id="ctx-1")
    ctx = await store.get_or_create_context(
        context_id="ctx-1",
        cwd=str(tmp_path),
        runtime_factory=lambda _session_id: _fake_runtime(),
    )
    ctx.active_task_id = "task-1"

    queue = FakeEventQueue()
    pipeline = EscalatingInterruptPipeline([], session_dir=tmp_path / "sidecar")
    publisher = PipelineA2AEventPublisher(
        event_queue=queue,
        translator=PipelineEventTranslator(
            PipelineA2AContext(
                pipeline_run_id="ctx-1",
                task_id="task-1",
                context_id="ctx-1",
                pipeline_name="selling",
            )
        ),
        journal=A2APipelineJournal(tmp_path / "pipeline"),
        snapshot_store=A2APipelineSnapshotStore(tmp_path / "pipeline"),
    )
    runtime = A2APipelineRuntime(agent_runtime=_fake_runtime(), pipeline=pipeline, publisher=publisher)
    ctx.runtime = runtime

    executor = IacCodeA2APipelineExecutor(
        task_store=store,
        model="qwen3.6-plus",
        metrics=NoOpA2AMetrics(),
        artifact_store=None,
        push_notifier=None,
        permission_resolver=None,
        auto_approve_permissions=False,
        thinking_exposure_types=None,
    )

    await executor.execute(
        context=FakeRequestContext(text="escalate candidate", metadata={"iac_code": {"cwd": str(tmp_path)}}),
        event_queue=queue,
        task=task,
        task_id="task-1",
        context_id="ctx-1",
        cwd=str(tmp_path),
        prompt="escalate candidate",
    )

    events = publisher.journal.read_all()
    event_types = [event["eventType"] for event in events]
    assert "candidate_restart_requested" not in event_types
    rollback = next(event for event in events if event["eventType"] == "rollback_completed")
    assert rollback["data"]["rollbackScope"] == "parent"
    assert rollback["step"]["id"] == "architecture_planning"
    assert rollback["step"]["runId"] == "step-architecture_planning-2"
    assert rollback["step"]["attempt"] == 2
    assert runtime.restart_after_interrupt is True
    assert runtime.restart_requested.is_set() is True


@pytest.mark.asyncio
async def test_same_task_interrupt_handler_failure_preserves_active_record(tmp_path: Path) -> None:
    from iac_code.a2a.pipeline_events import PipelineA2AContext, PipelineEventTranslator
    from iac_code.a2a.pipeline_executor import A2APipelineRuntime, IacCodeA2APipelineExecutor
    from iac_code.a2a.pipeline_journal import A2APipelineJournal
    from iac_code.a2a.pipeline_snapshot import A2APipelineSnapshotStore
    from iac_code.a2a.pipeline_stream import PipelineA2AEventPublisher

    class FailingInterruptPipeline(FakePipeline):
        async def handle_user_interrupt(self, message: str) -> SimpleNamespace:
            raise ValueError(f"cannot judge {message}")

    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    task = await store.get_or_create_task(task_id="task-1", context_id="ctx-1")
    task.state = "working"
    task.active_task = asyncio.current_task()
    ctx = await store.get_or_create_context(
        context_id="ctx-1",
        cwd=str(tmp_path),
        runtime_factory=lambda _session_id: _fake_runtime(),
    )
    ctx.active_task_id = "task-1"
    active_task = task.active_task

    queue = FakeEventQueue()
    pipeline = FailingInterruptPipeline([], session_dir=tmp_path / "sidecar")
    publisher = PipelineA2AEventPublisher(
        event_queue=queue,
        translator=PipelineEventTranslator(
            PipelineA2AContext(
                pipeline_run_id="ctx-1",
                task_id="task-1",
                context_id="ctx-1",
                pipeline_name="selling",
            )
        ),
        journal=A2APipelineJournal(tmp_path / "pipeline"),
        snapshot_store=A2APipelineSnapshotStore(tmp_path / "pipeline"),
    )
    ctx.runtime = A2APipelineRuntime(agent_runtime=_fake_runtime(), pipeline=pipeline, publisher=publisher)

    executor = IacCodeA2APipelineExecutor(
        task_store=store,
        model="qwen3.6-plus",
        metrics=NoOpA2AMetrics(),
        artifact_store=None,
        push_notifier=None,
        permission_resolver=None,
        auto_approve_permissions=False,
        thinking_exposure_types=None,
    )

    await executor.execute(
        context=FakeRequestContext(text="bad interrupt", metadata={"iac_code": {"cwd": str(tmp_path)}}),
        event_queue=queue,
        task=task,
        task_id="task-1",
        context_id="ctx-1",
        cwd=str(tmp_path),
        prompt="bad interrupt",
    )

    assert ctx.active_task_id == "task-1"
    assert task.active_task is active_task
    assert task.state == "working"
    assert _status_events(queue)[-1]["status"]["state"] == "TASK_STATE_FAILED"


@pytest.mark.asyncio
async def test_same_task_non_interruptible_active_context_preserves_active_record(tmp_path: Path) -> None:
    from iac_code.a2a.pipeline_executor import A2APipelineRuntime, IacCodeA2APipelineExecutor

    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    task = await store.get_or_create_task(task_id="task-1", context_id="ctx-1")
    task.state = "working"
    task.active_task = asyncio.current_task()
    ctx = await store.get_or_create_context(
        context_id="ctx-1",
        cwd=str(tmp_path),
        runtime_factory=lambda _session_id: _fake_runtime(),
    )
    ctx.active_task_id = "task-1"
    ctx.runtime = A2APipelineRuntime(
        agent_runtime=_fake_runtime(),
        pipeline=FakePipeline([], session_dir=tmp_path / "sidecar"),
        publisher=None,
    )
    active_task = task.active_task

    executor = IacCodeA2APipelineExecutor(
        task_store=store,
        model="qwen3.6-plus",
        metrics=NoOpA2AMetrics(),
        artifact_store=None,
        push_notifier=None,
        permission_resolver=None,
        auto_approve_permissions=False,
        thinking_exposure_types=None,
    )
    queue = FakeEventQueue()

    await executor.execute(
        context=FakeRequestContext(text="second", metadata={"iac_code": {"cwd": str(tmp_path)}}),
        event_queue=queue,
        task=task,
        task_id="task-1",
        context_id="ctx-1",
        cwd=str(tmp_path),
        prompt="second",
    )

    assert ctx.active_task_id == "task-1"
    assert task.active_task is active_task
    assert task.state == "working"
    final_status = _status_events(queue)[-1]["status"]
    assert final_status["state"] == "TASK_STATE_FAILED"
    assert final_status["message"]["parts"][0]["text"] == "Task is already working."


@pytest.mark.asyncio
async def test_active_pipeline_interrupt_receives_structured_image_input(tmp_path: Path) -> None:
    from iac_code.a2a.pipeline_executor import IacCodeA2APipelineExecutor

    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    task = await store.get_or_create_task(task_id="task-1", context_id="ctx-1")
    task.active_task = asyncio.current_task()
    ctx = await store.get_or_create_context(
        context_id="ctx-1",
        cwd=str(tmp_path),
        runtime_factory=lambda session_id: _fake_runtime(),
    )
    ctx.active_task_id = "task-1"
    received = []

    class InterruptPipeline(FakePipeline):
        async def handle_user_interrupt(self, message):
            received.append(message)
            return InterruptVerdict(action="continue", reason="keep going")

        def pause_agent_loops(self) -> None:
            pass

        def resume_agent_loops(self) -> None:
            pass

    pipeline = InterruptPipeline([], session_dir=tmp_path / "pipeline")
    publisher = SimpleNamespace(
        publish_interrupt_received=AsyncMock(),
        publish_interrupt=AsyncMock(),
        journal=A2APipelineJournal(tmp_path / "pipeline"),
        snapshot_store=A2APipelineSnapshotStore(tmp_path / "pipeline"),
    )
    ctx.runtime = SimpleNamespace(
        agent_runtime=_fake_runtime(),
        pipeline=pipeline,
        publisher=publisher,
        current_stream=None,
        restart_after_interrupt=False,
        pause_after_interrupt=False,
        restart_requested=asyncio.Event(),
    )
    store.mirror_context(ctx)
    pipeline_input = image_interrupt_input()

    executor = IacCodeA2APipelineExecutor(
        task_store=store,
        model="qwen3.6-plus",
        metrics=NoOpA2AMetrics(),
        artifact_store=None,
        push_notifier=None,
        permission_resolver=None,
        auto_approve_permissions=False,
        thinking_exposure_types=None,
    )
    await executor.execute(
        context=FakeRequestContext(metadata={"iac_code": {"cwd": str(tmp_path)}}),
        event_queue=FakeEventQueue(),
        task=task,
        task_id="task-1",
        context_id="ctx-1",
        cwd=str(tmp_path),
        pipeline_input=pipeline_input,
    )

    assert received == [pipeline_input]
    publisher.publish_interrupt_received.assert_awaited_once_with(prompt="[Image input]")


@pytest.mark.asyncio
async def test_active_pending_question_answer_preserves_image_input(tmp_path: Path) -> None:
    from iac_code.a2a.pipeline_executor import IacCodeA2APipelineExecutor, _PendingAskUserQuestion

    future = asyncio.get_running_loop().create_future()
    injected = []

    class Pipeline:
        def inject_pending_question_supplement(self, message, *, envelope):
            injected.append((message, envelope))

    runtime = SimpleNamespace(
        pending_question=_PendingAskUserQuestion(
            event=AskUserQuestionEvent(
                tool_use_id="toolu_1",
                question="Upload diagram",
                options=[],
                response_future=future,
            ),
            envelope={"scope": "pipeline", "inputId": "ask-toolu_1"},
        ),
        pipeline=Pipeline(),
        publisher=SimpleNamespace(
            publish_manual=AsyncMock(return_value=object()),
        ),
    )
    pipeline_input = image_interrupt_input()
    executor = IacCodeA2APipelineExecutor(
        task_store=A2ATaskStore(metrics=NoOpA2AMetrics()),
        model="qwen3.6-plus",
        metrics=NoOpA2AMetrics(),
        artifact_store=None,
        push_notifier=None,
        permission_resolver=None,
        auto_approve_permissions=False,
        thinking_exposure_types=None,
    )

    result = await executor._route_pending_question_answer(runtime, pipeline_input)

    assert result == "answered"
    answer = future.result()
    assert answer == {"selected_id": "", "selected_label": "", "free_text": "[Image input]"}
    assert injected == [(pipeline_input.content, {"scope": "pipeline", "inputId": "ask-toolu_1"})]


@pytest.mark.asyncio
async def test_active_pending_question_image_injection_failure_is_not_marked_answered(tmp_path: Path) -> None:
    from iac_code.a2a.pipeline_executor import IacCodeA2APipelineExecutor, _PendingAskUserQuestion

    future = asyncio.get_running_loop().create_future()

    class Pipeline:
        def inject_pending_question_supplement(self, message, *, envelope):
            return False

    runtime = SimpleNamespace(
        pending_question=_PendingAskUserQuestion(
            event=AskUserQuestionEvent(
                tool_use_id="toolu_1",
                question="Upload diagram",
                options=[],
                response_future=future,
            ),
            envelope={"scope": "pipeline", "inputId": "ask-toolu_1"},
        ),
        pipeline=Pipeline(),
        publisher=SimpleNamespace(
            publish_manual=AsyncMock(return_value=object()),
        ),
    )
    pipeline_input = image_interrupt_input()
    executor = IacCodeA2APipelineExecutor(
        task_store=A2ATaskStore(metrics=NoOpA2AMetrics()),
        model="qwen3.6-plus",
        metrics=NoOpA2AMetrics(),
        artifact_store=None,
        push_notifier=None,
        permission_resolver=None,
        auto_approve_permissions=False,
        thinking_exposure_types=None,
    )

    with pytest.raises(RuntimeError, match="image supplement could not be delivered"):
        await executor._route_pending_question_answer(runtime, pipeline_input)

    assert future.done() is False
    assert runtime.pending_question is not None


@pytest.mark.asyncio
async def test_active_pending_question_image_injection_failure_restores_snapshot_pending_input(tmp_path: Path) -> None:
    from iac_code.a2a.pipeline_events import PipelineA2AContext, PipelineEventTranslator
    from iac_code.a2a.pipeline_executor import IacCodeA2APipelineExecutor, _PendingAskUserQuestion
    from iac_code.a2a.pipeline_journal import A2APipelineJournal
    from iac_code.a2a.pipeline_snapshot import A2APipelineSnapshotStore
    from iac_code.a2a.pipeline_stream import PipelineA2AEventPublisher

    future = asyncio.get_running_loop().create_future()

    class Pipeline:
        def inject_pending_question_supplement(self, message, *, envelope):
            return False

    publisher = PipelineA2AEventPublisher(
        event_queue=FakeEventQueue(),
        translator=PipelineEventTranslator(
            PipelineA2AContext(
                pipeline_run_id="ctx-1",
                task_id="task-1",
                context_id="ctx-1",
                pipeline_name="selling",
            )
        ),
        journal=A2APipelineJournal(tmp_path / "pipeline"),
        snapshot_store=A2APipelineSnapshotStore(tmp_path / "pipeline"),
    )
    await publisher.publish_manual(
        "input_required",
        "pipeline",
        status="input_required",
        data={
            "kind": "ask_user_question",
            "inputId": "ask-toolu_1",
            "toolUseId": "toolu_1",
            "question": "Upload diagram",
            "prompt": "Upload diagram",
            "options": [],
            "required": True,
        },
    )
    assert publisher.snapshot_store.load()["pendingInput"]["inputId"] == "ask-toolu_1"

    runtime = SimpleNamespace(
        pending_question=_PendingAskUserQuestion(
            event=AskUserQuestionEvent(
                tool_use_id="toolu_1",
                question="Upload diagram",
                options=[],
                response_future=future,
            ),
            envelope={"scope": "pipeline", "inputId": "ask-toolu_1"},
        ),
        pipeline=Pipeline(),
        publisher=publisher,
    )
    executor = IacCodeA2APipelineExecutor(
        task_store=A2ATaskStore(metrics=NoOpA2AMetrics()),
        model="qwen3.6-plus",
        metrics=NoOpA2AMetrics(),
        artifact_store=None,
        push_notifier=None,
        permission_resolver=None,
        auto_approve_permissions=False,
        thinking_exposure_types=None,
    )

    with pytest.raises(RuntimeError, match="image supplement could not be delivered"):
        await executor._route_pending_question_answer(runtime, image_interrupt_input())

    snapshot = publisher.snapshot_store.load()
    assert snapshot["status"] == "waiting_input"
    assert snapshot["pendingInput"]["inputId"] == "ask-toolu_1"
    assert future.done() is False
    assert runtime.pending_question is not None


@pytest.mark.asyncio
async def test_execute_reports_active_pending_question_image_injection_failure(tmp_path: Path) -> None:
    from iac_code.a2a.pipeline_events import PipelineA2AContext, PipelineEventTranslator
    from iac_code.a2a.pipeline_executor import (
        A2APipelineRuntime,
        IacCodeA2APipelineExecutor,
        _PendingAskUserQuestion,
    )
    from iac_code.a2a.pipeline_journal import A2APipelineJournal
    from iac_code.a2a.pipeline_snapshot import A2APipelineSnapshotStore
    from iac_code.a2a.pipeline_stream import PipelineA2AEventPublisher

    future = asyncio.get_running_loop().create_future()

    class Pipeline:
        def inject_pending_question_supplement(self, message, *, envelope):
            return False

    queue = FakeEventQueue()
    publisher = PipelineA2AEventPublisher(
        event_queue=queue,
        translator=PipelineEventTranslator(
            PipelineA2AContext(
                pipeline_run_id="ctx-1",
                task_id="task-1",
                context_id="ctx-1",
                pipeline_name="selling",
            )
        ),
        journal=A2APipelineJournal(tmp_path / "pipeline"),
        snapshot_store=A2APipelineSnapshotStore(tmp_path / "pipeline"),
    )
    publisher.publish_manual = AsyncMock(return_value=object())  # type: ignore[method-assign]
    runtime = A2APipelineRuntime(agent_runtime=_fake_runtime(), pipeline=Pipeline(), publisher=publisher)
    runtime.pending_question = _PendingAskUserQuestion(
        event=AskUserQuestionEvent(
            tool_use_id="toolu_1",
            question="Upload diagram",
            options=[],
            response_future=future,
        ),
        envelope={"scope": "pipeline", "inputId": "ask-toolu_1"},
    )
    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    task = await store.get_or_create_task(task_id="task-1", context_id="ctx-1")
    task.state = "input-required"
    ctx = await store.get_or_create_context(
        context_id="ctx-1",
        cwd=str(tmp_path),
        runtime_factory=lambda _session_id: _fake_runtime(),
    )
    ctx.runtime = runtime
    ctx.active_task_id = "task-1"
    executor = IacCodeA2APipelineExecutor(
        task_store=store,
        model="qwen3.6-plus",
        metrics=NoOpA2AMetrics(),
        artifact_store=None,
        push_notifier=None,
        permission_resolver=None,
        auto_approve_permissions=False,
        thinking_exposure_types=None,
    )

    await executor.execute(
        context=FakeRequestContext(task_id="task-1", context_id="ctx-1"),
        event_queue=queue,
        task=task,
        task_id="task-1",
        context_id="ctx-1",
        cwd=str(tmp_path),
        pipeline_input=image_interrupt_input(),
    )

    states = [dump(event)["status"]["state"] for event in queue.events if isinstance(event, TaskStatusUpdateEvent)]
    assert "TASK_STATE_FAILED" in states
    assert future.done() is False
    assert runtime.pending_question is not None


@pytest.mark.asyncio
async def test_pending_ask_user_question_resume_preserves_image_input(tmp_path: Path) -> None:
    from iac_code.a2a.pipeline_executor import _resume_pending_ask_user_question_stream

    pipeline_input = image_interrupt_input()
    received = {}

    class AskPipeline(FakePipeline):
        sidecar_status = "waiting_input"

        async def resume_ask_user_question(self, answer, **kwargs):
            received["answer"] = answer
            received["supplemental_input"] = kwargs.get("supplemental_input")
            yield PipelineEvent(
                type=PipelineEventType.PIPELINE_COMPLETED,
                step_id="ask",
                timestamp=0.0,
                data={"total_steps": 1},
            )

    pending_input = {
        "kind": "ask_user_question",
        "toolUseId": "toolu_1",
        "inputId": "ask-toolu_1",
    }
    pipeline = AskPipeline([], session_dir=tmp_path / "pipeline")
    publisher = SimpleNamespace(
        snapshot_store=SimpleNamespace(load=lambda: {"status": "waiting_input"}),
        publish_manual=AsyncMock(return_value=object()),
    )

    stream = _resume_pending_ask_user_question_stream(
        pipeline=pipeline,
        publisher=publisher,
        pending_input=pending_input,
        prompt="[Image input]",
        pipeline_input=pipeline_input,
    )
    events = [event async for event in stream]

    assert events
    assert received["supplemental_input"] == pipeline_input
