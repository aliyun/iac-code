from __future__ import annotations

import asyncio
import json
import multiprocessing
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

import iac_code.a2a.backup as backup_module
from iac_code.a2a.backup import PENDING_JOBS_DIRNAME, SessionBackupCoordinator, SessionBackupHandoffError
from iac_code.a2a.execution_control import ExecutionController
from iac_code.services.session_backup import BackupResult
from iac_code.services.session_backup_staging import SessionBackupStagingWorker, StagedSessionBackupService
from iac_code.services.session_metadata import SESSION_LAYOUT_VERSION_V2, SessionMetadata, write_session_metadata
from iac_code.services.session_storage import SessionStorage


class _NaturalReceiptRetryFixture:
    """Fail only receipt IO after a real immutable natural capture has committed."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        config_root = tmp_path / "config"
        self.config_root = config_root
        self.staging_root = tmp_path / "staging"
        self.backup_root = tmp_path / "backup"
        self.state_root = tmp_path / "state"
        monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(config_root))
        monkeypatch.setenv("IAC_CODE_CONFIG_BACKUP_DIR", str(self.backup_root))
        storage = SessionStorage(projects_dir=config_root / "projects")
        self.session_dir = storage.session_dir("/repo", "s1")
        write_session_metadata(
            self.session_dir,
            SessionMetadata(session_id="s1", cwd="/repo", layout_version=SESSION_LAYOUT_VERSION_V2),
        )
        self.history = self.session_dir / "session.jsonl"
        self.history.write_text("sealed-natural-boundary\n", encoding="utf-8")
        self.service = StagedSessionBackupService(self.staging_root, storage, retry_delays=())
        self.service.initialize_session("/repo", "s1")
        self.coordinator = SessionBackupCoordinator(self.service, state_root=self.state_root, retry_delays=())
        self.control = ExecutionController(
            context_id="ctx-1",
            task_id="task-1",
            owner="owner-1",
            cwd="/repo",
            server_instance_id="instance-1",
            persistence_path=self.state_root / "control.json",
            backup_service=self.service,
            backup_coordinator=self.coordinator,
            execution_id="exec-1",
        )
        self.control.bind_session("s1")
        self.worker = SessionBackupStagingWorker(
            self.staging_root,
            self.backup_root,
            coordinator_state_root=self.state_root,
        )
        self.block_receipts = True
        self.receipt_failures = 0
        self.capture_commits: list[str | None] = []
        self.fail_capture = False
        self._write_json = backup_module.atomic_write_json
        self._capture = self.service.backup_session
        monkeypatch.setattr(backup_module, "atomic_write_json", self.write_json)
        monkeypatch.setattr(self.service, "backup_session", self.capture)

    def write_json(self, path: Path, payload: Any, **kwargs: Any) -> None:
        if self.block_receipts and Path(path).parent.name == "receipts":
            self.receipt_failures += 1
            raise OSError("injected coordinator receipt IO failure")
        self._write_json(path, payload, **kwargs)

    def capture(self, *args: Any, **kwargs: Any) -> BackupResult:
        self.capture_commits.append(kwargs.get("operation_commit_id"))
        if self.fail_capture:
            raise OSError("injected local capture failure")
        return self._capture(*args, **kwargs)

    async def finalize(self) -> tuple[int, dict[str, Any]]:
        current = asyncio.current_task()
        assert current is not None
        await self.control.attach_task(current)
        generation = await self.control.detach_task(
            current,
            execution_status="completed",
            natural_completion=True,
        )
        assert generation is not None
        result = await self.control.finalize_natural_completion(
            task_id="task-1",
            completion_generation=generation,
        )
        return generation, result

    async def drain_control_tasks(self) -> None:
        while self.control._background_tasks:
            await asyncio.gather(*tuple(self.control._background_tasks))

    async def close(self) -> None:
        self.block_receipts = False
        await self.control.close()
        await self.coordinator.aclose()


@pytest.mark.asyncio
async def test_natural_completion_retry_settles_original_captured_job_before_tmp_empty(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    fixture = _NaturalReceiptRetryFixture(monkeypatch, tmp_path)
    try:
        completion_generation, failed = await asyncio.wait_for(fixture.finalize(), 5)
        assert fixture.receipt_failures == 2, "initial receipt and its local repair must both fail"
        assert failed["backup"]["status"] == "blocked"
        assert failed["naturalHandoff"] is None
        assert failed["releaseReady"] is False

        markers = list((fixture.staging_root / PENDING_JOBS_DIRNAME).glob("*.json"))
        assert len(markers) == 1
        old_marker = markers[0]
        old_job = json.loads(old_marker.read_text(encoding="utf-8"))
        assert old_job["boundary"] == "natural_completion"
        assert old_job["contextId"] == "ctx-1"
        assert old_job["executionId"] == "exec-1"
        assert old_job["completionGeneration"] == completion_generation
        assert old_job["captureStarted"] is True
        assert old_job["callbackCompleted"] is True
        assert old_job["stagedGeneration"] == 1
        assert old_job["stagedCommitId"] == old_job["captureCommitId"]
        old_snapshots = fixture.worker.scan_snapshots()
        assert len(old_snapshots) == 1
        assert (old_snapshots[0].path / "session.jsonl").read_text(encoding="utf-8") == "sealed-natural-boundary\n"
        assert fixture.capture_commits == [old_job["captureCommitId"]]

        # A retry must adopt the already committed boundary, never capture later
        # live content as a replacement for this exact natural completion job.
        fixture.history.write_text("later-live-content\n", encoding="utf-8")
        fixture.block_receipts = False
        await fixture.control.observe_state()
        await asyncio.wait_for(fixture.drain_control_tasks(), 5)
        await asyncio.wait_for(asyncio.to_thread(fixture.worker.run_once), 5)

        assert not old_marker.exists(), "natural retry left its exact captured coordinator job pending"
        receipt_path = (
            fixture.state_root
            / "session-backup-coordinator"
            / old_job["project"]
            / "s1"
            / "receipts"
            / (old_job["jobId"] + ".json")
        )
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        assert receipt["jobId"] == old_job["jobId"]
        assert receipt["backupGeneration"] == old_job["stagedGeneration"]
        assert receipt["commitId"] == old_job["captureCommitId"]
        assert fixture.capture_commits == [old_job["captureCommitId"]]
        natural = fixture.control.natural_handoff_receipt()
        assert natural is not None
        assert natural["contextId"] == "ctx-1"
        assert natural["taskId"] == "task-1"
        assert natural["executionId"] == "exec-1"
        assert natural["completionGeneration"] == completion_generation
        assert natural["pendingJobId"] == old_job["jobId"]
        assert natural["snapshotGeneration"] == old_job["stagedGeneration"]
        assert natural["snapshotCommitId"] == old_job["captureCommitId"]
        assert fixture.control.natural_handoff_admits_replacement()
        shared = fixture.backup_root / "projects" / old_job["project"] / "s1" / "session.jsonl"
        assert shared.read_text(encoding="utf-8") == "sealed-natural-boundary\n"
        assert not fixture.worker.scan_snapshots()
        assert not fixture.staging_root.exists() or not any(fixture.staging_root.iterdir())
    finally:
        await fixture.close()


@pytest.mark.asyncio
async def test_natural_completion_retry_without_immutable_proof_does_not_capture_changed_live_content(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fixture = _NaturalReceiptRetryFixture(monkeypatch, tmp_path)
    fixture.fail_capture = True
    try:
        _generation, failed = await asyncio.wait_for(fixture.finalize(), 5)
        assert failed["backup"]["status"] == "blocked"
        marker = next((fixture.staging_root / PENDING_JOBS_DIRNAME).glob("*.json"))
        original = marker.read_bytes()
        assert not fixture.worker.scan_snapshots()
        assert len(fixture.capture_commits) == 1
        fixture.history.write_text("later-live-content\n", encoding="utf-8")
        fixture.fail_capture = False
        fixture.block_receipts = False
        await fixture.control.observe_state()
        await asyncio.wait_for(fixture.drain_control_tasks(), 5)
        assert marker.read_bytes() == original
        assert len(fixture.capture_commits) == 1
        assert not fixture.worker.scan_snapshots()
        assert fixture.control.snapshot()["backup"]["status"] == "blocked"
        assert fixture.control.natural_handoff_receipt() is None
        assert not fixture.control.release_ready
    finally:
        await fixture.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["context_id", "execution_id", "completion_generation", "capture_commit_id"])
async def test_natural_completion_retry_rejects_wrong_original_job_identity(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, field: str
) -> None:
    fixture = _NaturalReceiptRetryFixture(monkeypatch, tmp_path)
    try:
        generation, _failed = await asyncio.wait_for(fixture.finalize(), 5)
        job = fixture.control._pending_natural_backup_job
        assert job is not None
        marker = fixture.staging_root / PENDING_JOBS_DIRNAME / (job.job_id + ".json")
        original = marker.read_bytes()
        wrong_value = generation + 1 if field == "completion_generation" else "different-identity"
        wrong = replace(job, **{field: wrong_value})
        fixture.block_receipts = False
        with pytest.raises(SessionBackupHandoffError, match="identity conflicts"):
            await fixture.coordinator.resume_natural_completion(
                wrong,
                cwd="/repo",
                session_id="s1",
                context_id="ctx-1",
                execution_id="exec-1",
                completion_generation=generation,
            )
        assert marker.read_bytes() == original
        assert fixture.capture_commits == [job.capture_commit_id]
        assert fixture.control.natural_handoff_receipt() is None
    finally:
        await fixture.close()


@pytest.mark.asyncio
async def test_natural_completion_retry_does_not_replace_a_changed_durable_owner(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fixture = _NaturalReceiptRetryFixture(monkeypatch, tmp_path)
    try:
        await asyncio.wait_for(fixture.finalize(), 5)
        path = fixture.state_root / "control.json"
        changed = json.loads(path.read_text(encoding="utf-8"))
        changed["owner"] = "another-owner"
        changed["ownerGeneration"] += 1
        fixture._write_json(path, changed, durable=True)
        original = path.read_bytes()
        fixture.block_receipts = False
        await fixture.control.observe_state()
        await asyncio.wait_for(fixture.drain_control_tasks(), 5)
        assert path.read_bytes() == original
        assert fixture.control.natural_handoff_receipt() is None
        assert not fixture.control.natural_handoff_admits_replacement()
        assert not fixture.control.release_ready
        assert fixture.control.snapshot()["commitError"] == "state_commit_failed"
        assert len(fixture.capture_commits) == 1
    finally:
        await fixture.close()


class _RejectRecaptureStagingService(StagedSessionBackupService):
    def backup_session(self, *args: Any, **kwargs: Any) -> BackupResult:
        raise AssertionError("historical coordinator recovery must not recapture live content")


class _NaturalRetryProcessActions:
    @staticmethod
    async def recover_job(staging_root: str, state_root: str, config_root: str) -> int:
        service = _RejectRecaptureStagingService(
            staging_root,
            SessionStorage(projects_dir=Path(config_root) / "projects"),
        )
        coordinator = SessionBackupCoordinator(service, state_root=Path(state_root))
        try:
            return await coordinator.recover()
        finally:
            await coordinator.aclose()

    @classmethod
    def recover(cls, staging_root: str, state_root: str, config_root: str, ready: Any, start: Any, output: Any) -> None:
        ready.put(True)
        if not start.wait(10):
            raise RuntimeError("natural recovery start barrier timed out")
        output.put(("recovered", asyncio.run(cls.recover_job(staging_root, state_root, config_root))))

    @staticmethod
    def publish(staging_root: str, backup_root: str, state_root: str, ready: Any, start: Any, output: Any) -> None:
        worker = SessionBackupStagingWorker(staging_root, backup_root, coordinator_state_root=Path(state_root))
        ready.put(True)
        if not start.wait(10):
            raise RuntimeError("natural publisher start barrier timed out")
        output.put(("published", worker.run_once()))


@pytest.mark.asyncio
async def test_natural_completion_retry_adopts_receipt_after_other_processes_settle_original_job(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fixture = _NaturalReceiptRetryFixture(monkeypatch, tmp_path)
    context = multiprocessing.get_context("spawn")
    ready, output = context.Queue(), context.Queue()
    start = context.Event()
    processes: list[Any] = []
    try:
        generation, _failed = await asyncio.wait_for(fixture.finalize(), 5)
        job = fixture.control._pending_natural_backup_job
        assert job is not None
        marker = fixture.staging_root / PENDING_JOBS_DIRNAME / (job.job_id + ".json")
        assert marker.exists()
        assert fixture.receipt_failures == 2
        fixture.history.write_text("later-live-content\n", encoding="utf-8")
        fixture.block_receipts = False
        for _ in range(2):
            processes.append(
                context.Process(
                    target=_NaturalRetryProcessActions.recover,
                    args=(
                        str(fixture.staging_root),
                        str(fixture.state_root),
                        str(fixture.config_root),
                        ready,
                        start,
                        output,
                    ),
                )
            )
        processes.append(
            context.Process(
                target=_NaturalRetryProcessActions.publish,
                args=(
                    str(fixture.staging_root),
                    str(fixture.backup_root),
                    str(fixture.state_root),
                    ready,
                    start,
                    output,
                ),
            )
        )
        for process in processes:
            process.start()
        for _ in processes:
            assert await asyncio.to_thread(ready.get, True, 10) is True
        start.set()
        results = [await asyncio.to_thread(output.get, True, 10) for _ in processes]
        for process in processes:
            await asyncio.to_thread(process.join, 10)
            assert not process.is_alive()
            assert process.exitcode == 0
        assert any(kind == "recovered" and count == 1 for kind, count in results)
        assert ("published", 1) in results
        assert not marker.exists()
        assert fixture.control.natural_handoff_receipt() is None

        # The original live controller adopts the other process's exact receipt;
        # this does not claim that a restarted service hydrates old controllers.
        await fixture.control.observe_state()
        await asyncio.wait_for(fixture.drain_control_tasks(), 5)
        natural = fixture.control.natural_handoff_receipt()
        assert natural is not None
        assert natural["pendingJobId"] == job.job_id
        assert natural["completionGeneration"] == generation
        assert natural["executionId"] == job.execution_id
        assert natural["snapshotCommitId"] == job.capture_commit_id
        assert natural["snapshotGeneration"] == 1
        assert fixture.capture_commits == [job.capture_commit_id]
        assert fixture.control.natural_handoff_admits_replacement()
        assert not marker.exists(), "adopting a completed job revived its pending marker"
        shared = fixture.backup_root / "projects" / job.project / "s1" / "session.jsonl"
        assert shared.read_text(encoding="utf-8") == "sealed-natural-boundary\n"
        assert not fixture.staging_root.exists() or not any(fixture.staging_root.iterdir())
    finally:
        start.set()
        for process in processes:
            if process.is_alive():
                process.terminate()
            await asyncio.to_thread(process.join, 5)
        ready.close()
        output.close()
        await fixture.close()


class _PendingReadFailure:
    target: Path | None = None
    original_read_text = Path.read_text

    @staticmethod
    def read_text(path: Path, *args: Any, **kwargs: Any) -> str:
        if path == _PendingReadFailure.target:
            raise OSError("injected pending proof read failure")
        return _PendingReadFailure.original_read_text(path, *args, **kwargs)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["bad_json", "read_io"])
async def test_natural_completion_retry_proof_read_failure_remains_blocked_without_recapture(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, failure: str
) -> None:
    fixture = _NaturalReceiptRetryFixture(monkeypatch, tmp_path)
    try:
        await asyncio.wait_for(fixture.finalize(), 5)
        job = fixture.control._pending_natural_backup_job
        assert job is not None
        marker = fixture.staging_root / PENDING_JOBS_DIRNAME / (job.job_id + ".json")
        if failure == "bad_json":
            marker.write_text("{incomplete-json\n", encoding="utf-8")
        else:
            monkeypatch.setattr(_PendingReadFailure, "target", marker)
            monkeypatch.setattr(Path, "read_text", _PendingReadFailure.read_text)
        unchanged = marker.read_bytes()
        fixture.block_receipts = False
        await fixture.control.observe_state()
        await asyncio.wait_for(fixture.drain_control_tasks(), 5)
        assert fixture.control.snapshot()["backup"]["status"] == "blocked"
        assert fixture.control.natural_handoff_receipt() is None
        assert not fixture.control.release_ready
        assert marker.read_bytes() == unchanged
        assert fixture.capture_commits == [job.capture_commit_id]
    finally:
        await fixture.close()
