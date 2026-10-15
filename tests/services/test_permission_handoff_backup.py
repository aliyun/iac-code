"""A permission ownership handoff requires its exact shared backup receipt."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from iac_code.services.session_backup import (
    BackupReason,
    SessionBackupBlocked,
    SessionBackupError,
    SessionBackupService,
)
from iac_code.services.session_backup_staging import SessionBackupStagingWorker, StagedSessionBackupService
from iac_code.services.session_metadata import SESSION_LAYOUT_VERSION_V2, SessionMetadata, write_session_metadata
from iac_code.services.session_storage import SessionStorage


class _HandoffBackupCase:
    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, staged: bool) -> None:
        self.backup_root = tmp_path / "shared"
        self.staging_root = tmp_path / "staging"
        monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
        monkeypatch.setenv("IAC_CODE_CONFIG_BACKUP_DIR", str(self.backup_root))
        monkeypatch.delenv("IAC_CODE_CONFIG_BACKUP_TMP_DIR", raising=False)
        storage = SessionStorage(projects_dir=tmp_path / "config" / "projects")
        self.session_dir = storage.session_dir("/repo", "s1")
        write_session_metadata(
            self.session_dir,
            SessionMetadata(session_id="s1", cwd="/repo", layout_version=SESSION_LAYOUT_VERSION_V2),
        )
        (self.session_dir / "session.jsonl").write_text("permission safety point\n", encoding="utf-8")
        self.service = (
            StagedSessionBackupService(self.staging_root, storage, retry_delays=())
            if staged
            else SessionBackupService(storage, retry_delays=())
        )
        self.service.initialize_session("/repo", "s1")
        self.shared_dir = self.backup_root / "projects" / self.session_dir.parent.name / "s1"

    def capture(self):
        return self.service.backup_recoverable_input_handoff("/repo", "s1", operation_commit_id="permission-commit-1")

    def shared_state(self):
        return self.service._read_state(self.shared_dir, session_id="s1", shared=True)


@pytest.mark.parametrize("staged", [False, True])
def test_permission_handoff_returns_exact_shared_receipt(tmp_path, monkeypatch, staged):
    case = _HandoffBackupCase(tmp_path, monkeypatch, staged=staged)
    result = case.capture()
    assert result.enabled and result.succeeded and result.shared_committed
    assert result.generation == 1
    assert result.commit_id == "permission-commit-1"
    assert result.destination == case.shared_dir
    assert case.shared_state().generation == 1
    assert case.shared_state().commit_id == "permission-commit-1"
    assert case.shared_state().reason == BackupReason.INPUT_REQUIRED.value
    assert (case.shared_dir / "session.jsonl").read_text(encoding="utf-8") == "permission safety point\n"


@pytest.mark.parametrize("staged", [False, True])
def test_permission_handoff_shared_write_failure_never_returns_success(tmp_path, monkeypatch, staged):
    case = _HandoffBackupCase(tmp_path, monkeypatch, staged=staged)
    mirror = SessionBackupService._mirror

    def fail_shared_copy(service, source, destination):
        if destination == case.shared_dir:
            mirror(service, source, destination)
            raise OSError("injected handoff shared publication failure")
        return mirror(service, source, destination)

    monkeypatch.setattr(SessionBackupService, "_mirror", fail_shared_copy)
    with pytest.raises(SessionBackupBlocked) as blocked:
        case.capture()
    assert blocked.value.result is not None
    assert not blocked.value.result.succeeded
    assert not blocked.value.result.shared_committed
    assert (case.shared_dir / ".backup-publication.json").is_file()
    assert not (case.shared_dir / ".backup-state.json").exists()
    fresh = SessionBackupService(SessionStorage(projects_dir=tmp_path / "cold" / "projects"), retry_delays=())
    with pytest.raises(SessionBackupError, match="publication.*incomplete"):
        fresh.reconcile_session("/repo", "s1")
    monkeypatch.setattr(SessionBackupService, "_mirror", mirror)
    if staged:
        worker = SessionBackupStagingWorker(case.staging_root, case.backup_root)
        snapshots = worker.scan_snapshots()
        assert len(snapshots) == 1
        assert snapshots[0].generation == 1
        assert worker.run_once() == 1
    else:
        result = case.capture()
        assert result.succeeded and result.shared_committed
        assert result.generation == 1 and result.commit_id == "permission-commit-1"
    assert case.shared_state().generation == 1
    assert case.shared_state().commit_id == "permission-commit-1"
    assert not (case.shared_dir / ".backup-publication.json").exists()
    assert fresh.reconcile_session("/repo", "s1").action == "restored"


@pytest.mark.parametrize("published_state", ["missing", "newer", "different_commit"])
def test_staged_permission_handoff_rejects_unmatched_shared_receipt(tmp_path, monkeypatch, published_state):
    case = _HandoffBackupCase(tmp_path, monkeypatch, staged=True)
    publish = SessionBackupStagingWorker.publish_snapshot

    def publish_unmatched(worker, snapshot):
        if published_state == "missing":
            return
        publish(worker, snapshot)
        state = case.shared_state()
        if published_state == "newer":
            state = state.committed_next(commit_id="later-commit", reason="terminal", writer_id="other-worker")
        else:
            document = state.to_dict()
            document["commit_id"] = "other-commit"
            state = type(state).from_dict(document, shared=True)
        case.service._write_state(case.shared_dir, state)

    monkeypatch.setattr(SessionBackupStagingWorker, "publish_snapshot", publish_unmatched)
    with pytest.raises(SessionBackupError):
        case.capture()


def test_staged_permission_handoff_accepts_exact_receipt_after_concurrent_snapshot_retirement(tmp_path, monkeypatch):
    case = _HandoffBackupCase(tmp_path, monkeypatch, staged=True)
    publish = SessionBackupStagingWorker.publish_snapshot

    def concurrent_publish(worker, snapshot):
        publish(worker, snapshot)
        if snapshot.path.exists():
            shutil.rmtree(snapshot.path)
        # The worker's scanned path can disappear before this attempt publishes.
        publish(worker, snapshot)

    monkeypatch.setattr(SessionBackupStagingWorker, "publish_snapshot", concurrent_publish)
    result = case.capture()
    assert result.shared_committed
    assert result.generation == 1
    assert result.commit_id == "permission-commit-1"
    assert case.shared_state().commit_id == result.commit_id
