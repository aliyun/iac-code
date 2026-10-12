"""Upgrade compatibility for immutable snapshots created before coordinator ACKs."""

import json
import uuid
from pathlib import Path

import pytest

from iac_code.a2a.backup import SessionBackupCoordinator
from iac_code.services.session_backup import BACKUP_STATE_FILENAME, BackupReason, SessionBackupService
from iac_code.services.session_backup_staging import SessionBackupStagingWorker, StagedSessionBackupService
from iac_code.services.session_backup_state import SessionBackupState
from iac_code.services.session_metadata import SESSION_LAYOUT_VERSION_V2, SessionMetadata, write_session_metadata
from iac_code.services.session_storage import SessionStorage


class LegacyPublicationCase:
    def __init__(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        config_root = tmp_path / "config"
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
        self.service = StagedSessionBackupService(self.staging_root, storage, retry_delays=())
        self.service.initialize_session("/repo", "s1")
        self.coordinator = SessionBackupCoordinator(self.service, state_root=self.state_root, retry_delays=())
        self.worker = SessionBackupStagingWorker(
            self.staging_root, self.backup_root, coordinator_state_root=self.state_root
        )
        self.shared_dir = self.backup_root / "projects" / self.session_dir.parent.name / "s1"

    async def capture_legacy_then_managed(self) -> None:
        (self.session_dir / "session.jsonl").write_text("legacy-v1\n", encoding="utf-8")
        # Before this PR, ordinary staged backups generated bare UUID commits.
        # Their retained snapshots have neither coordinator jobs nor receipts.
        legacy = self.service.backup_session(
            "/repo",
            "s1",
            reason=BackupReason.NORMAL_TURN_END,
            critical=True,
            operation_commit_id=str(uuid.uuid4()),
        )
        assert legacy.generation == 1 and legacy.destination is not None
        self.legacy_snapshot = legacy.destination
        (self.session_dir / "session.jsonl").write_text("managed-v2\n", encoding="utf-8")
        handoff = await self.coordinator.register_boundary(
            cwd="/repo",
            session_id="s1",
            context_id="ctx-1",
            execution_id="exec-1",
            boundary="natural_completion",
            reason=BackupReason.TERMINAL,
        )
        assert handoff.snapshot_generation == 2
        self.managed_snapshot = next(
            snapshot.path for snapshot in self.worker.scan_snapshots() if snapshot.generation == 2
        )

    def seed_shared_generation(self, generation: int) -> None:
        snapshot = self.legacy_snapshot if generation == 1 else self.managed_snapshot
        self.service._mirror(snapshot, self.shared_dir)
        self.service._write_state(self.shared_dir, self.read_state(snapshot))

    @staticmethod
    def read_state(path: Path) -> SessionBackupState:
        document = json.loads((path / BACKUP_STATE_FILENAME).read_text(encoding="utf-8"))
        return SessionBackupState.from_dict(document, shared=True)

    def assert_managed_published_and_legacy_retained(self) -> None:
        assert self.read_state(self.shared_dir).generation == 2
        assert (self.shared_dir / "session.jsonl").read_text(encoding="utf-8") == "managed-v2\n"
        assert self.legacy_snapshot.is_dir(), "unknown retirement proof must preserve the immutable legacy capture"
        assert (self.legacy_snapshot / "session.jsonl").read_text(encoding="utf-8") == "legacy-v1\n"
        assert not self.managed_snapshot.exists(), "the exact managed ACK permits retiring generation 2"


@pytest.mark.asyncio
@pytest.mark.parametrize("shared_generation", [None, 1, 2])
async def test_legacy_retirement_proof_does_not_block_new_managed_publication(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, shared_generation: int | None
) -> None:
    case = LegacyPublicationCase(monkeypatch, tmp_path)
    try:
        await case.capture_legacy_then_managed()
        if shared_generation is not None:
            case.seed_shared_generation(shared_generation)

        assert case.worker.run_once() == 2
        case.assert_managed_published_and_legacy_retained()

        # The retained older capture must neither rewind shared state nor stall
        # a subsequent publisher scan after generation 2 has been retired.
        assert case.worker.run_once() == 1
        case.assert_managed_published_and_legacy_retained()
    finally:
        await case.coordinator.aclose()


@pytest.mark.asyncio
async def test_legacy_shared_copy_failure_keeps_ordered_retry(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    case = LegacyPublicationCase(monkeypatch, tmp_path)
    attempted: list[str] = []
    mirror = SessionBackupService._mirror

    def fail_legacy_copy_once(service, source, destination):
        attempted.append(source.name)
        if len(attempted) == 1:
            raise OSError("injected shared copy failure")
        return mirror(service, source, destination)

    try:
        await case.capture_legacy_then_managed()
        monkeypatch.setattr(SessionBackupService, "_mirror", fail_legacy_copy_once)

        assert case.worker.run_once() == 0
        assert attempted == ["s1_v1"], "a failed shared copy must block later generations for the same session"
        assert case.legacy_snapshot.exists() and case.managed_snapshot.exists()

        assert case.worker.run_once() == 2
        assert attempted == ["s1_v1", "s1_v1", "s1_v2"]
        case.assert_managed_published_and_legacy_retained()
    finally:
        await case.coordinator.aclose()
