from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path
from typing import Any

import pytest

from iac_code.a2a.backup import PENDING_JOBS_DIRNAME, SessionBackupCoordinator, SessionBackupJob
from iac_code.services.session_backup import BackupReason, BackupResult, SessionBackupService
from iac_code.services.session_backup_staging import SessionBackupStagingWorker, StagedSessionBackupService
from iac_code.services.session_metadata import SESSION_LAYOUT_VERSION_V2, SessionMetadata, write_session_metadata
from iac_code.services.session_storage import SessionStorage


class _RegistrationCancellationFixture:
    """Cancel after the real durable job write, before its thread returns the job."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        self.monkeypatch = monkeypatch
        self.config_root = tmp_path / "config"
        self.staging_root = tmp_path / "staging"
        self.backup_root = tmp_path / "backup"
        self.state_root = tmp_path / "state"
        monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(self.config_root))
        monkeypatch.setenv("IAC_CODE_CONFIG_BACKUP_DIR", str(self.backup_root))
        storage = SessionStorage(projects_dir=self.config_root / "projects")
        self.session_dir = storage.session_dir("/repo", "s1")
        write_session_metadata(
            self.session_dir,
            SessionMetadata(session_id="s1", cwd="/repo", layout_version=SESSION_LAYOUT_VERSION_V2),
        )
        self.history = self.session_dir / "session.jsonl"
        self.service = StagedSessionBackupService(self.staging_root, storage, retry_delays=())
        self.service.initialize_session("/repo", "s1")
        self.coordinator = SessionBackupCoordinator(self.service, state_root=self.state_root, retry_delays=())
        self.worker = SessionBackupStagingWorker(
            self.staging_root,
            self.backup_root,
            coordinator_state_root=self.state_root,
        )
        self.persisted = threading.Event()
        self.return_job = threading.Event()
        self.publisher_started = threading.Event()
        self.release_publisher = threading.Event()
        self.job: SessionBackupJob | None = None
        self.registration: asyncio.Task[Any] | None = None
        self.publishing: asyncio.Task[Any] | None = None
        self.original_persist = self.coordinator._persist_pending_job
        self.original_mirror = SessionBackupService._mirror
        self.boundary_payload = "cancelled-registration-boundary\n"
        self.expected_generation = 1

    async def register(self, execution_id: str):
        return await self.coordinator.register_boundary(
            cwd="/repo",
            session_id="s1",
            context_id="ctx-1",
            execution_id=execution_id,
            boundary="natural_completion",
            reason=BackupReason.TERMINAL,
            completion_generation=1,
        )

    def persist_then_wait(self, *args: Any, **kwargs: Any) -> SessionBackupJob | None:
        job = self.original_persist(*args, **kwargs)
        assert job is not None
        self.job = job
        self.persisted.set()
        assert self.return_job.wait(5), "registration return barrier was not released"
        return job

    def mirror_with_shared_barrier(
        self,
        service: SessionBackupService,
        source: Path,
        destination: Path,
    ) -> BackupResult:
        if Path(destination).is_relative_to(self.backup_root) and not self.publisher_started.is_set():
            self.publisher_started.set()
            assert self.release_publisher.wait(5), "shared publication barrier was not released"
        return self.original_mirror(service, source, destination)

    async def block_prior_shared_publication(self) -> None:
        self.history.write_text("prior-boundary\n", encoding="utf-8")
        handoff = await self.register("exec-prior")
        assert handoff.staged_committed is True
        self.expected_generation = 2
        snapshot = self.worker.scan_snapshots()[0]
        self.monkeypatch.setattr(
            SessionBackupService,
            "_mirror",
            lambda service, source, destination: self.mirror_with_shared_barrier(service, source, destination),
        )
        self.publishing = asyncio.create_task(asyncio.to_thread(self.worker.publish_snapshot, snapshot))
        assert await asyncio.to_thread(self.publisher_started.wait, 3)

    async def cancel_after_durable_registration(self) -> SessionBackupJob:
        self.history.write_text(self.boundary_payload, encoding="utf-8")
        self.monkeypatch.setattr(self.coordinator, "_persist_pending_job", self.persist_then_wait)
        self.registration = asyncio.create_task(self.register("exec-cancelled-registration"))
        assert await asyncio.to_thread(self.persisted.wait, 3)
        job = self.job
        assert job is not None
        marker = self.staging_root / PENDING_JOBS_DIRNAME / f"{job.job_id}.json"
        document = json.loads(marker.read_text(encoding="utf-8"))
        assert document["jobId"] == job.job_id
        assert document["captureCommitId"] == job.capture_commit_id
        assert document["executionId"] == "exec-cancelled-registration"
        assert document["captureStarted"] is False
        assert document["stagedGeneration"] is None
        self.registration.cancel()
        await asyncio.sleep(0)
        assert self.registration.done() is False, "thread mutation escaped the cancellation fence"
        self.return_job.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(self.registration, 3)
        return job

    def assert_exact_local_completion(self, job: SessionBackupJob) -> Path:
        snapshot = self.staging_root / "projects" / job.project / f"s1_v{self.expected_generation}"
        assert snapshot.is_dir(), "cancelled registration lost its durable job before original local capture"
        assert (snapshot / "session.jsonl").read_text(encoding="utf-8") == self.boundary_payload
        state = self.service.read_local_state("/repo", "s1")
        assert state is not None
        assert state.status == "succeeded"
        assert state.generation == self.expected_generation
        assert state.commit_id == job.capture_commit_id
        receipt_path = (
            self.state_root / "session-backup-coordinator" / job.project / "s1" / "receipts" / f"{job.job_id}.json"
        )
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        assert receipt == {
            "version": 1,
            "jobId": job.job_id,
            "project": job.project,
            "sessionId": "s1",
            "boundary": "natural_completion",
            "backupGeneration": self.expected_generation,
            "commitId": job.capture_commit_id,
            "coveredThroughBusinessRevision": job.business_revision,
        }
        assert not (self.staging_root / PENDING_JOBS_DIRNAME / f"{job.job_id}.json").exists()
        assert not (self.staging_root / PENDING_JOBS_DIRNAME).exists()
        return snapshot

    async def publish_after_live_changes(self, job: SessionBackupJob, snapshot: Path) -> None:
        self.history.write_text("later-live-payload\n", encoding="utf-8")
        assert (snapshot / "session.jsonl").read_text(encoding="utf-8") == self.boundary_payload
        self.release_publisher.set()
        if self.publishing is not None:
            await asyncio.wait_for(self.publishing, 5)
        await asyncio.to_thread(self.worker.run_once)
        shared = self.backup_root / "projects" / job.project / "s1"
        assert (shared / "session.jsonl").read_text(encoding="utf-8") == self.boundary_payload
        shared_state = self.service._read_state(shared, session_id="s1", shared=True)
        assert shared_state is not None
        assert shared_state.generation == self.expected_generation
        assert shared_state.commit_id == job.capture_commit_id
        assert not snapshot.exists()

    async def close(self) -> None:
        self.return_job.set()
        self.release_publisher.set()
        tasks = [task for task in (self.registration, self.publishing) if task is not None]
        if tasks:
            await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 5)
        await self.coordinator.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("shared_publication_blocked", [False, True])
async def test_cancel_after_durable_registration_finishes_original_local_job_before_propagating(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    shared_publication_blocked: bool,
) -> None:
    fixture = _RegistrationCancellationFixture(monkeypatch, tmp_path)
    try:
        if shared_publication_blocked:
            await fixture.block_prior_shared_publication()
        job = await fixture.cancel_after_durable_registration()
        snapshot = fixture.assert_exact_local_completion(job)
        if shared_publication_blocked:
            assert fixture.publishing is not None and not fixture.publishing.done()
            assert not fixture.release_publisher.is_set(), "registration waited for shared publication"
        await fixture.publish_after_live_changes(job, snapshot)
    finally:
        await fixture.close()


class _RegistrationStagedCallbackBarrier:
    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.proof: tuple[int | None, str | None] | None = None

    async def on_staged(self, generation: int | None, commit_id: str | None) -> None:
        self.proof = (generation, commit_id)
        self.entered.set()
        await self.release.wait()


@pytest.mark.asyncio
async def test_repeated_cancel_after_durable_registration_waits_for_exact_staged_callback(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    fixture = _RegistrationCancellationFixture(monkeypatch, tmp_path)
    callback = _RegistrationStagedCallbackBarrier()
    try:
        fixture.history.write_text(fixture.boundary_payload, encoding="utf-8")
        monkeypatch.setattr(fixture.coordinator, "_persist_pending_job", fixture.persist_then_wait)
        fixture.registration = asyncio.create_task(
            fixture.coordinator.register_boundary(
                cwd="/repo",
                session_id="s1",
                context_id="ctx-1",
                execution_id="exec-cancelled-registration",
                boundary="natural_completion",
                reason=BackupReason.TERMINAL,
                completion_generation=1,
                on_staged=callback.on_staged,
            )
        )
        assert await asyncio.to_thread(fixture.persisted.wait, 3)
        job = fixture.job
        assert job is not None
        fixture.registration.cancel()
        await asyncio.sleep(0)
        assert not fixture.registration.done()
        fixture.return_job.set()
        await asyncio.wait_for(callback.entered.wait(), 3)
        assert callback.proof == (1, job.capture_commit_id)
        assert not fixture.registration.done(), "caller cancellation escaped before staged callback committed"
        fixture.registration.cancel()
        await asyncio.sleep(0)
        assert not fixture.registration.done(), "repeated cancellation interrupted the owned staged callback"
        marker = fixture.staging_root / PENDING_JOBS_DIRNAME / f"{job.job_id}.json"
        pending = json.loads(marker.read_text(encoding="utf-8"))
        assert pending["callbackRequired"] is True
        assert pending["callbackCompleted"] is False
        assert pending["stagedGeneration"] == 1
        assert pending["stagedCommitId"] == job.capture_commit_id
        callback.release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(fixture.registration, 3)
        snapshot = fixture.assert_exact_local_completion(job)
        await fixture.publish_after_live_changes(job, snapshot)
    finally:
        callback.release.set()
        await fixture.close()
