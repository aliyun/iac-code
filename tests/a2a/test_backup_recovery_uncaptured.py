from __future__ import annotations

import asyncio
import json
import multiprocessing
import os
from pathlib import Path
from typing import Any

import pytest

from iac_code.a2a.backup import PENDING_JOBS_DIRNAME, SessionBackupCoordinator, SessionBackupHandoffError
from iac_code.services.session_backup import BackupReason, BackupResult
from iac_code.services.session_backup_staging import SessionBackupStagingWorker, StagedSessionBackupService
from iac_code.services.session_metadata import SESSION_LAYOUT_VERSION_V2, SessionMetadata, write_session_metadata
from iac_code.services.session_storage import SessionStorage


class _CountingRecoveryBackupService(StagedSessionBackupService):
    def __init__(self, staging_root: Path, storage: SessionStorage) -> None:
        super().__init__(staging_root, storage, retry_delays=())
        self.capture_calls = 0

    def backup_session(self, *args: Any, **kwargs: Any) -> BackupResult:
        self.capture_calls += 1
        return super().backup_session(*args, **kwargs)


class _UncapturedRecoveryProcess:
    def __init__(self, root: str, output: Any) -> None:
        self.root = Path(root)
        self.output = output

    @staticmethod
    def start(root: str, output: Any) -> None:
        asyncio.run(_UncapturedRecoveryProcess(root, output).recover())

    async def recover(self) -> None:
        os.environ["IAC_CODE_CONFIG_DIR"] = str(self.root / "config")
        os.environ["IAC_CODE_CONFIG_BACKUP_DIR"] = str(self.root / "backup")
        storage = SessionStorage(projects_dir=self.root / "config" / "projects")
        service = _CountingRecoveryBackupService(self.root / "staging", storage)
        coordinator = SessionBackupCoordinator(service, state_root=self.root / "state", retry_delays=())
        recovered = None
        error_type = None
        try:
            try:
                recovered = await coordinator.recover()
            except SessionBackupHandoffError as exc:
                error_type = type(exc).__name__
        finally:
            await coordinator.aclose()
        self.output.put({"recovered": recovered, "error_type": error_type, "capture_calls": service.capture_calls})


class _UncapturedPipelineRecoveryFixture:
    def __init__(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        self.root = tmp_path
        self.staging_root = tmp_path / "staging"
        self.state_root = tmp_path / "state"
        self.backup_root = tmp_path / "backup"
        monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
        monkeypatch.setenv("IAC_CODE_CONFIG_BACKUP_DIR", str(self.backup_root))
        storage = SessionStorage(projects_dir=tmp_path / "config" / "projects")
        self.session_dir = storage.session_dir("/repo", "s1")
        write_session_metadata(
            self.session_dir,
            SessionMetadata(session_id="s1", cwd="/repo", layout_version=SESSION_LAYOUT_VERSION_V2),
        )
        self.history = self.session_dir / "session.jsonl"
        self.original_payload = "uncaptured-original-boundary\n"
        self.history.write_text(self.original_payload, encoding="utf-8")
        self.service = StagedSessionBackupService(self.staging_root, storage, retry_delays=())
        self.service.initialize_session("/repo", "s1")
        self.initial_state = self.service.read_local_state("/repo", "s1")
        assert self.initial_state is not None
        self.coordinator = SessionBackupCoordinator(self.service, state_root=self.state_root, retry_delays=())
        self.job = self.coordinator._persist_pending_job(
            "/repo",
            "s1",
            "ctx-old",
            "task-old",
            "pipeline_publication",
            BackupReason.INPUT_REQUIRED.value,
            None,
            {},
            False,
            None,
        )
        assert self.job is not None
        self.marker = self.staging_root / PENDING_JOBS_DIRNAME / f"{self.job.job_id}.json"
        self.original_document = self.marker.read_bytes()
        document = json.loads(self.original_document)
        assert document["captureStarted"] is False
        assert document["stagedGeneration"] is None
        assert document["callbackRequired"] is False
        self.receipt_path = (
            self.state_root
            / "session-backup-coordinator"
            / self.job.project
            / "s1"
            / "receipts"
            / f"{self.job.job_id}.json"
        )
        self.worker = SessionBackupStagingWorker(
            self.staging_root,
            self.backup_root,
            coordinator_state_root=self.state_root,
        )

    async def recover_in_new_process(self) -> dict[str, Any]:
        await self.coordinator.aclose()
        context = multiprocessing.get_context("spawn")
        output = context.Queue()
        process = context.Process(target=_UncapturedRecoveryProcess.start, args=(str(self.root), output))
        try:
            process.start()
            result = await asyncio.to_thread(output.get, True, 15)
            await asyncio.to_thread(process.join, 10)
            assert not process.is_alive(), "fresh coordinator recovery did not finish"
            assert process.exitcode == 0, "fresh recovery process failed outside its expected handoff boundary"
            return result
        finally:
            if process.is_alive():
                process.terminate()
                await asyncio.to_thread(process.join, 5)
            output.close()
            output.join_thread()


@pytest.mark.asyncio
async def test_uncaptured_pipeline_job_recovery_in_new_process_does_not_capture_changed_live(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    fixture = _UncapturedPipelineRecoveryFixture(monkeypatch, tmp_path)
    fixture.history.write_text("later-live-content\n", encoding="utf-8")
    result = await fixture.recover_in_new_process()

    assert result["capture_calls"] == 0, "historical uncaptured job reread later live as its old capture commit"
    assert result["error_type"] == "SessionBackupHandoffError"
    assert result["recovered"] is None
    assert fixture.marker.read_bytes() == fixture.original_document
    assert not fixture.receipt_path.exists()
    assert fixture.worker.scan_snapshots() == []
    assert fixture.history.read_text(encoding="utf-8") == "later-live-content\n"
    local = fixture.service.read_local_state("/repo", "s1")
    assert local is not None and local.commit_id != fixture.job.capture_commit_id
    assert not (fixture.backup_root / "projects").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("proof_location", ["immutable", "shared"])
async def test_uncaptured_pipeline_job_recovery_adopts_exact_immutable_capture_without_rereading_live(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    proof_location: str,
) -> None:
    fixture = _UncapturedPipelineRecoveryFixture(monkeypatch, tmp_path)
    captured = fixture.service.backup_session(
        "/repo",
        "s1",
        reason=BackupReason.INPUT_REQUIRED,
        critical=True,
        operation_commit_id=fixture.job.capture_commit_id,
    )
    assert captured.staged_committed and captured.destination is not None
    assert fixture.marker.read_bytes() == fixture.original_document
    if proof_location == "shared":
        # Reproduce the historical publisher retirement window using the real
        # legacy publisher, which did not have the coordinator ACK fence.
        legacy_worker = SessionBackupStagingWorker(fixture.staging_root, fixture.backup_root)
        await asyncio.to_thread(legacy_worker.run_once)
        assert not captured.destination.exists()
        fixture.service._write_state(
            fixture.session_dir,
            fixture.initial_state.failed_attempt(
                reason=BackupReason.INPUT_REQUIRED.value,
                writer_id="historical-owner",
                attempt_commit_id=fixture.job.capture_commit_id,
                attempted_proofs={},
                error="historical marker was not committed",
                attempt=1,
                retry_count=0,
                exhausted=True,
            ),
        )
    fixture.history.write_text("later-live-content\n", encoding="utf-8")
    result = await fixture.recover_in_new_process()

    assert result["capture_calls"] == 0, "exact immutable proof must be adopted even if captureStarted was not recorded"
    assert result["error_type"] is None
    assert result["recovered"] == 1
    assert not fixture.marker.exists()
    receipt = json.loads(fixture.receipt_path.read_text(encoding="utf-8"))
    assert receipt["jobId"] == fixture.job.job_id
    assert receipt["boundary"] == "pipeline_publication"
    assert receipt["commitId"] == fixture.job.capture_commit_id
    assert receipt["backupGeneration"] == captured.generation
    snapshots = fixture.worker.scan_snapshots()
    if proof_location == "immutable":
        assert len(snapshots) == 1
        assert snapshots[0].path == captured.destination
        assert (snapshots[0].path / "session.jsonl").read_text(encoding="utf-8") == fixture.original_payload
    else:
        assert snapshots == []
    adopted = fixture.service.read_local_state("/repo", "s1")
    assert adopted is not None and adopted.status == "succeeded"
    assert adopted.generation == captured.generation
    assert adopted.commit_id == fixture.job.capture_commit_id
    assert fixture.history.read_text(encoding="utf-8") == "later-live-content\n"
    await asyncio.to_thread(fixture.worker.run_once)
    shared = fixture.backup_root / "projects" / fixture.job.project / "s1" / "session.jsonl"
    assert shared.read_text(encoding="utf-8") == fixture.original_payload
    assert fixture.worker.scan_snapshots() == []


@pytest.mark.asyncio
@pytest.mark.parametrize("proof_location", ["immutable", "shared"])
async def test_uncaptured_pipeline_job_recovery_rejects_different_commit_with_higher_generation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    proof_location: str,
) -> None:
    fixture = _UncapturedPipelineRecoveryFixture(monkeypatch, tmp_path)
    first = fixture.service.backup_session(
        "/repo",
        "s1",
        reason=BackupReason.INPUT_REQUIRED,
        critical=True,
        operation_commit_id="different-boundary-commit-1",
    )
    fixture.history.write_text("newer-unrelated-boundary\n", encoding="utf-8")
    second = fixture.service.backup_session(
        "/repo",
        "s1",
        reason=BackupReason.INPUT_REQUIRED,
        critical=True,
        operation_commit_id="different-boundary-commit-2",
    )
    assert first.staged_committed and second.staged_committed
    assert first.generation == 1 and second.generation == 2
    assert first.commit_id != fixture.job.capture_commit_id
    assert second.commit_id != fixture.job.capture_commit_id
    assert first.destination is not None and second.destination is not None
    assert fixture.marker.read_bytes() == fixture.original_document
    if proof_location == "shared":
        legacy_worker = SessionBackupStagingWorker(fixture.staging_root, fixture.backup_root)
        await asyncio.to_thread(legacy_worker.run_once)
        assert not first.destination.exists()
        assert not second.destination.exists()
    fixture.history.write_text("later-live-content\n", encoding="utf-8")
    result = await fixture.recover_in_new_process()

    assert result["capture_calls"] == 0, "unrelated higher generation cannot authorize a historical live capture"
    assert result["error_type"] == "SessionBackupHandoffError"
    assert result["recovered"] is None
    assert fixture.marker.read_bytes() == fixture.original_document
    assert not fixture.receipt_path.exists()
    local = fixture.service.read_local_state("/repo", "s1")
    assert local is not None and local.status == "succeeded"
    assert local.generation == second.generation
    assert local.commit_id == second.commit_id
    assert fixture.history.read_text(encoding="utf-8") == "later-live-content\n"
    if proof_location == "immutable":
        snapshots = fixture.worker.scan_snapshots()
        assert [snapshot.generation for snapshot in snapshots] == [1, 2]
        assert (first.destination / "session.jsonl").read_text(encoding="utf-8") == fixture.original_payload
        assert (second.destination / "session.jsonl").read_text(encoding="utf-8") == "newer-unrelated-boundary\n"
        assert not (fixture.backup_root / "projects").exists()
    else:
        assert fixture.worker.scan_snapshots() == []
        shared = fixture.backup_root / "projects" / fixture.job.project / "s1"
        shared_state = fixture.service._read_state(shared, session_id="s1", shared=True)
        assert shared_state is not None and shared_state.status == "succeeded"
        assert shared_state.generation == second.generation
        assert shared_state.commit_id == second.commit_id
        assert (shared / "session.jsonl").read_text(encoding="utf-8") == "newer-unrelated-boundary\n"
