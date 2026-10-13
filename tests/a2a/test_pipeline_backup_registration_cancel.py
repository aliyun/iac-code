from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path
from typing import Any

import pytest

from iac_code.a2a.backup import PENDING_JOBS_DIRNAME, SessionBackupCoordinator, SessionBackupJob
from iac_code.a2a.input_required import PermissionInputRegistry
from iac_code.a2a.metrics import NoOpA2AMetrics
from iac_code.a2a.pipeline_events import PipelineA2AContext, PipelineEventTranslator
from iac_code.a2a.pipeline_executor import IacCodeA2APipelineExecutor
from iac_code.a2a.pipeline_journal import A2APipelineJournal
from iac_code.a2a.pipeline_snapshot import A2APipelineSnapshotStore
from iac_code.a2a.pipeline_stream import PipelineA2AEventPublisher
from iac_code.a2a.task_store import A2ATaskStore
from iac_code.services.session_backup import BackupReason
from iac_code.services.session_backup_staging import SessionBackupStagingWorker, StagedSessionBackupService
from iac_code.services.session_metadata import SESSION_LAYOUT_VERSION_V2, SessionMetadata, write_session_metadata
from iac_code.services.session_storage import SessionStorage

from .fakes import FakeEventQueue, FakeRuntime


class _PipelinePublicationCancellationFixture:
    def __init__(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        self.monkeypatch = monkeypatch
        self.staging_root = tmp_path / "staging"
        self.backup_root = tmp_path / "backup"
        self.state_root = tmp_path / "state"
        monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
        monkeypatch.setenv("IAC_CODE_CONFIG_BACKUP_DIR", str(self.backup_root))
        self.storage = SessionStorage(projects_dir=tmp_path / "config" / "projects")
        self.service = StagedSessionBackupService(self.staging_root, self.storage, retry_delays=())
        self.coordinator = SessionBackupCoordinator(self.service, state_root=self.state_root, retry_delays=())
        self.worker = SessionBackupStagingWorker(
            self.staging_root,
            self.backup_root,
            coordinator_state_root=self.state_root,
        )
        self.task_store = A2ATaskStore(backup_service=self.service)
        self.registry = PermissionInputRegistry()
        self.registry.set_backup_coordinator(self.coordinator)
        self.executor = IacCodeA2APipelineExecutor(
            task_store=self.task_store,
            model="qwen3.6-plus",
            metrics=NoOpA2AMetrics(),
            artifact_store=None,
            push_notifier=None,
            permission_resolver=None,
            permission_input_registry=self.registry,
            auto_approve_permissions=False,
            thinking_exposure_types=None,
            backup_service=self.service,
        )
        self.queue = FakeEventQueue()
        self.persisted = threading.Event()
        self.return_job = threading.Event()
        self.original_persist = self.coordinator._persist_pending_job
        self.job: SessionBackupJob | None = None
        self.publication: asyncio.Task[Any] | None = None
        self.payload = "original-pipeline-user-input\n"

    @staticmethod
    def runtime(session_id: str) -> FakeRuntime:
        return FakeRuntime(session_id=session_id)

    async def prepare(self) -> PipelineA2AEventPublisher:
        self.ctx = await self.task_store.get_or_create_context(
            context_id="ctx-1",
            cwd="/repo",
            runtime_factory=self.runtime,
        )
        self.task = await self.task_store.get_or_create_task(task_id="task-1", context_id="ctx-1")
        self.session_dir = self.storage.session_dir("/repo", self.ctx.session_id)
        write_session_metadata(
            self.session_dir,
            SessionMetadata(session_id=self.ctx.session_id, cwd="/repo", layout_version=SESSION_LAYOUT_VERSION_V2),
        )
        self.history = self.session_dir / "session.jsonl"
        self.history.write_text(self.payload, encoding="utf-8")
        self.service.initialize_session("/repo", self.ctx.session_id)
        pipeline_dir = self.session_dir / "a2a" / "pipeline"
        publisher = PipelineA2AEventPublisher(
            event_queue=self.queue,
            translator=PipelineEventTranslator(
                PipelineA2AContext(
                    pipeline_run_id="run-1",
                    task_id=self.task.task_id,
                    context_id=self.task.context_id,
                    pipeline_name="selling",
                    parent_step_order=["confirm_and_select"],
                    candidate_step_order=[],
                )
            ),
            journal=A2APipelineJournal(pipeline_dir),
            snapshot_store=A2APipelineSnapshotStore(pipeline_dir),
        )
        self.executor._install_backup_hook(
            publisher,
            pipeline=FakeRuntime(),
            cwd="/repo",
            session_id=self.ctx.session_id,
            task=self.task,
            ctx=self.ctx,
        )
        self.monkeypatch.setattr(self.coordinator, "_persist_pending_job", self.persist_then_wait)
        return publisher

    def persist_then_wait(self, *args: Any, **kwargs: Any) -> SessionBackupJob | None:
        job = self.original_persist(*args, **kwargs)
        assert job is not None
        self.job = job
        self.persisted.set()
        assert self.return_job.wait(5), "pipeline durable registration barrier was not released"
        return job

    async def close(self) -> None:
        self.return_job.set()
        if self.publication is not None:
            await asyncio.wait_for(asyncio.gather(self.publication, return_exceptions=True), 5)
        await self.task_store.stop_cleanup_loop()
        await self.coordinator.aclose()


@pytest.mark.asyncio
async def test_pipeline_before_enqueue_cancel_finishes_original_registered_capture_before_propagating(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    fixture = _PipelinePublicationCancellationFixture(monkeypatch, tmp_path)
    try:
        publisher = await fixture.prepare()
        fixture.publication = asyncio.create_task(
            publisher.publish_manual(
                "input_required",
                "pipeline",
                status="input_required",
                data={"kind": "candidate_selection", "options": [{"id": "candidate-1", "label": "Candidate"}]},
            )
        )
        assert await asyncio.to_thread(fixture.persisted.wait, 3)
        job = fixture.job
        assert job is not None
        assert job.boundary == "pipeline_publication"
        assert job.reason == BackupReason.INPUT_REQUIRED.value
        assert job.context_id == fixture.task.context_id
        assert job.execution_id == fixture.task.task_id
        assert job.session_id == fixture.ctx.session_id
        assert job.completion_generation is None
        assert job.callback_required is False
        marker = fixture.staging_root / PENDING_JOBS_DIRNAME / f"{job.job_id}.json"
        pending = json.loads(marker.read_text(encoding="utf-8"))
        assert pending["captureStarted"] is False
        assert pending["stagedGeneration"] is None
        assert fixture.queue.events == [], "input must not become externally visible before its capture"
        persisted_events = publisher.journal.read_all_strict()
        assert len(persisted_events) == 1
        assert persisted_events[0]["eventType"] == "input_required"
        assert persisted_events[0]["taskId"] == fixture.task.task_id

        fixture.publication.cancel()
        await asyncio.sleep(0)
        assert not fixture.publication.done()
        fixture.return_job.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(fixture.publication, 3)

        snapshots = fixture.worker.scan_snapshots()
        assert len(snapshots) == 1, "canceled pipeline publication abandoned its durable registration"
        snapshot = snapshots[0]
        assert (snapshot.path / "session.jsonl").read_text(encoding="utf-8") == fixture.payload
        immutable_events = A2APipelineJournal(snapshot.path / "a2a" / "pipeline").read_all_strict()
        assert immutable_events == persisted_events
        local = fixture.service.read_local_state("/repo", fixture.ctx.session_id)
        assert local is not None and local.status == "succeeded"
        assert local.generation == snapshot.generation
        assert local.commit_id == job.capture_commit_id
        receipt_path = (
            fixture.state_root
            / "session-backup-coordinator"
            / job.project
            / job.session_id
            / "receipts"
            / f"{job.job_id}.json"
        )
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        assert receipt["jobId"] == job.job_id
        assert receipt["boundary"] == "pipeline_publication"
        assert receipt["commitId"] == job.capture_commit_id
        assert receipt["backupGeneration"] == snapshot.generation
        assert not marker.exists()
        assert fixture.queue.events == [], "caller cancellation must not publish an unconsumed input boundary"
        assert not (fixture.backup_root / "projects").exists(), "local completion must not require shared publication"

        fixture.history.write_text("later-live-input\n", encoding="utf-8")
        await asyncio.to_thread(fixture.worker.run_once)
        shared = fixture.backup_root / "projects" / job.project / job.session_id / "session.jsonl"
        assert shared.read_text(encoding="utf-8") == fixture.payload
        assert not snapshot.path.exists()
    finally:
        await fixture.close()
