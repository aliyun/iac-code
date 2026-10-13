from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Callable

import pytest

from iac_code.a2a.backup import PENDING_JOBS_DIRNAME, SessionBackupCoordinator, SessionBackupHandoffError
from iac_code.services.session_backup import BackupReason
from iac_code.services.session_backup_staging import SessionBackupStagingWorker, StagedSessionBackupService
from iac_code.services.session_metadata import SESSION_LAYOUT_VERSION_V2, SessionMetadata, write_session_metadata
from iac_code.services.session_storage import SessionStorage


class _DirectoryScanBoundary:
    """Run another owner's completed mutation immediately before directory enumeration."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, target: Path, action: Callable[[], None]) -> None:
        self.target = target
        self.action = action
        self.entered = False
        self.original_scandir = os.scandir
        self.original_listdir = os.listdir
        monkeypatch.setattr(os, "scandir", self.scandir)
        monkeypatch.setattr(os, "listdir", self.listdir)
        accessor = getattr(Path, "_accessor", None)
        if accessor is not None:  # Python 3.10 captures these functions during pathlib import.
            monkeypatch.setattr(accessor, "scandir", self.scandir)
            monkeypatch.setattr(accessor, "listdir", self.listdir)

    def enter(self, path: Any) -> None:
        if not self.entered and isinstance(path, (str, os.PathLike)) and Path(path) == self.target:
            self.entered = True
            self.action()

    def scandir(self, path: Any):
        self.enter(path)
        return self.original_scandir(path)

    def listdir(self, path: Any):
        self.enter(path)
        return self.original_listdir(path)


class _CapturedJobFixture:
    def __init__(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        self.staging = tmp_path / "staging"
        self.shared = tmp_path / "shared"
        self.state = tmp_path / "state"
        monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
        monkeypatch.setenv("IAC_CODE_CONFIG_BACKUP_DIR", str(self.shared))
        storage = SessionStorage(projects_dir=tmp_path / "config" / "projects")
        self.session = storage.session_dir("/repo", "s1")
        write_session_metadata(
            self.session,
            SessionMetadata(session_id="s1", cwd="/repo", layout_version=SESSION_LAYOUT_VERSION_V2),
        )
        (self.session / "session.jsonl").write_text("original-boundary\n", encoding="utf-8")
        self.service = StagedSessionBackupService(self.staging, storage, retry_delays=())
        self.service.initialize_session("/repo", "s1")
        self.coordinator = SessionBackupCoordinator(self.service, state_root=self.state)
        job = self.coordinator._persist_pending_job(
            "/repo", "s1", "ctx-1", "exec-1", "terminal", BackupReason.TERMINAL.value, None, {}, False, None
        )
        assert job is not None
        self.job, result = self.coordinator._capture_local_job(job, 0, False)
        assert result is not None and result.staged_committed
        self.marker = self.staging / PENDING_JOBS_DIRNAME / (job.job_id + ".json")
        self.worker = SessionBackupStagingWorker(self.staging, self.shared, coordinator_state_root=self.state)

    def assert_exact_receipt(self) -> None:
        receipt = self.coordinator._read_receipt_locked(self.job)
        assert receipt is not None
        assert receipt["backupGeneration"] == self.job.staged_generation
        assert receipt["commitId"] == self.job.capture_commit_id
        assert not self.marker.exists()


@pytest.mark.asyncio
async def test_recovery_tolerates_publisher_pruning_snapshot_directory_during_scan(monkeypatch, tmp_path) -> None:
    fixture = _CapturedJobFixture(monkeypatch, tmp_path)
    # Keep the empty project directory until recovery starts enumerating it,
    # then let the publisher's final pruning step remove it at that boundary.
    monkeypatch.setattr(fixture.worker, "_prune_empty_staging_dirs", lambda: None)
    assert fixture.worker.run_once() == 1
    project_dir = fixture.staging / "projects" / fixture.job.project
    assert project_dir.is_dir() and not any(project_dir.iterdir())
    boundary = _DirectoryScanBoundary(monkeypatch, project_dir, project_dir.rmdir)
    (fixture.session / "session.jsonl").write_text("later-live-content\n", encoding="utf-8")
    monkeypatch.setattr(fixture.service, "backup_session", lambda *_args, **_kwargs: pytest.fail("recaptured boundary"))
    try:
        assert await fixture.coordinator.recover() == 1
        assert boundary.entered
        fixture.assert_exact_receipt()
        shared = fixture.shared / "projects" / fixture.job.project / "s1" / "session.jsonl"
        assert shared.read_text(encoding="utf-8") == "original-boundary\n"
    finally:
        await fixture.coordinator.aclose()


@pytest.mark.asyncio
async def test_recovery_tolerates_other_owner_removing_pending_directory_during_scan(monkeypatch, tmp_path) -> None:
    fixture = _CapturedJobFixture(monkeypatch, tmp_path)
    boundary = _DirectoryScanBoundary(
        monkeypatch,
        fixture.staging / PENDING_JOBS_DIRNAME,
        lambda: fixture.coordinator._commit_receipt_and_clear(
            fixture.job, fixture.job.staged_generation, fixture.job.staged_commit_id
        ),
    )
    try:
        assert await fixture.coordinator.recover() == 0
        assert boundary.entered
        fixture.assert_exact_receipt()
    finally:
        await fixture.coordinator.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("directory", ["pending", "snapshot"])
@pytest.mark.parametrize("error_type", [PermissionError, OSError])
async def test_recovery_scan_io_error_keeps_job_without_claiming_completion(
    monkeypatch, tmp_path, directory, error_type
) -> None:
    fixture = _CapturedJobFixture(monkeypatch, tmp_path)
    original = fixture.marker.read_bytes()
    target = (
        fixture.staging / PENDING_JOBS_DIRNAME
        if directory == "pending"
        else fixture.staging / "projects" / fixture.job.project
    )

    def fail_scan() -> None:
        raise error_type("injected scan IO failure")

    boundary = _DirectoryScanBoundary(monkeypatch, target, fail_scan)
    expected_error = SessionBackupHandoffError if directory == "pending" else error_type
    try:
        with pytest.raises(expected_error, match="injected scan IO failure|pending jobs could not be loaded"):
            await fixture.coordinator.recover()
        assert boundary.entered
        assert fixture.marker.read_bytes() == original
        assert fixture.coordinator._read_receipt_locked(fixture.job) is None
        assert fixture.worker.scan_snapshots()
    finally:
        await fixture.coordinator.aclose()
