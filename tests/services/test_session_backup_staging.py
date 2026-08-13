import json
import threading
from dataclasses import replace
from pathlib import Path

import pytest

from iac_code.services.session_backup import (
    BACKUP_STATE_FILENAME,
    BackupReason,
    SessionBackupError,
    SessionBackupService,
)
from iac_code.services.session_backup_staging import (
    SessionBackupStagingProcess,
    SessionBackupStagingWorker,
    StagedSessionBackupService,
    StagedSessionSnapshot,
    create_a2a_session_backup_runtime,
    run_staging_worker,
)
from iac_code.services.session_backup_state import (
    NORMAL_HANDOFF_PROOF_KEY,
    BackupPublicationProof,
    SessionBackupState,
)
from iac_code.services.session_metadata import SESSION_LAYOUT_VERSION_V2, SessionMetadata, write_session_metadata
from iac_code.services.session_storage import SessionStorage


def _create_staged_service(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> tuple[StagedSessionBackupService, Path, Path, Path]:
    config_root = tmp_path / "config"
    staging_root = tmp_path / "staging"
    backup_root = tmp_path / "backup"
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(config_root))
    monkeypatch.setenv("IAC_CODE_CONFIG_BACKUP_DIR", str(backup_root))
    storage = SessionStorage(projects_dir=config_root / "projects")
    session_dir = storage.session_dir("/repo", "s1")
    write_session_metadata(
        session_dir,
        SessionMetadata(session_id="s1", cwd="/repo", layout_version=SESSION_LAYOUT_VERSION_V2),
    )
    service = StagedSessionBackupService(staging_root, storage, retry_delays=())
    service.initialize_session("/repo", "s1")
    return service, session_dir, staging_root, backup_root


def _read_state(path: Path, *, shared: bool = False) -> SessionBackupState:
    payload = json.loads((path / BACKUP_STATE_FILENAME).read_text(encoding="utf-8"))
    return SessionBackupState.from_dict(payload, shared=shared)


def test_staged_backup_creates_immutable_versions_without_writing_final_root(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    service, session_dir, staging_root, backup_root = _create_staged_service(monkeypatch, tmp_path)
    (session_dir / "session.jsonl").write_text("v1\n", encoding="utf-8")

    first = service.backup_session("/repo", "s1", reason=BackupReason.NORMAL_TURN_END, critical=True)
    (session_dir / "session.jsonl").write_text("v2\n", encoding="utf-8")
    second = service.backup_session("/repo", "s1", reason=BackupReason.TERMINAL, critical=True)

    project = session_dir.parent.name
    first_snapshot = staging_root / "projects" / project / "s1_v1"
    second_snapshot = staging_root / "projects" / project / "s1_v2"
    assert first.staged_committed is True and first.shared_committed is False
    assert second.staged_committed is True and second.shared_committed is False
    assert (first_snapshot / "session.jsonl").read_text(encoding="utf-8") == "v1\n"
    assert (second_snapshot / "session.jsonl").read_text(encoding="utf-8") == "v2\n"
    assert _read_state(first_snapshot, shared=True).generation == 1
    assert _read_state(second_snapshot, shared=True).generation == 2
    assert not list(staging_root.rglob("*.copying"))
    assert not backup_root.exists()


def test_regular_backup_service_ignores_staging_environment(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config_root = tmp_path / "config"
    backup_root = tmp_path / "backup"
    staging_root = tmp_path / "staging"
    monkeypatch.setenv("IAC_CODE_CONFIG_BACKUP_DIR", str(backup_root))
    monkeypatch.setenv("IAC_CODE_CONFIG_BACKUP_TMP_DIR", str(staging_root))
    storage = SessionStorage(projects_dir=config_root / "projects")
    session_dir = storage.session_dir("/repo", "s1")
    write_session_metadata(
        session_dir,
        SessionMetadata(session_id="s1", cwd="/repo", layout_version=SESSION_LAYOUT_VERSION_V2),
    )
    service = SessionBackupService(storage, retry_delays=())
    service.initialize_session("/repo", "s1")

    result = service.backup_session("/repo", "s1", reason=BackupReason.TERMINAL, critical=True)

    assert result.shared_committed is True and result.staged_committed is False
    assert (backup_root / "projects" / session_dir.parent.name / "s1").is_dir()
    assert not staging_root.exists()


def test_staged_reconcile_adopts_completed_next_snapshot(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    service, session_dir, _staging_root, _backup_root = _create_staged_service(monkeypatch, tmp_path)
    bootstrap = _read_state(session_dir)
    service.backup_session("/repo", "s1", reason=BackupReason.TERMINAL, critical=True)
    service._write_state(session_dir, bootstrap)

    result = service.reconcile_session("/repo", "s1")

    assert result.action == "staged_current"
    assert result.state is not None and result.state.generation == 1
    assert _read_state(session_dir).generation == 1


def test_staged_backup_adopts_snapshot_completed_before_local_state_update(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    service, session_dir, staging_root, _backup_root = _create_staged_service(monkeypatch, tmp_path)
    bootstrap = _read_state(session_dir)
    service.backup_session("/repo", "s1", reason=BackupReason.TERMINAL, critical=True)
    committed = _read_state(staging_root / "projects" / session_dir.parent.name / "s1_v1", shared=True)
    service._write_state(session_dir, bootstrap)

    result = service.backup_session("/repo", "s1", reason=BackupReason.NORMAL_TURN_END, critical=True)

    assert result.commit_id == committed.commit_id
    assert result.generation == 1
    assert _read_state(session_dir).same_lineage(committed)


def test_staged_reconcile_repairs_failed_state_locally(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    service, session_dir, staging_root, backup_root = _create_staged_service(monkeypatch, tmp_path)
    failed = _read_state(session_dir).failed_attempt(
        reason=BackupReason.TERMINAL.value,
        writer_id="writer",
        attempt_commit_id="attempt-1",
        attempted_proofs={},
        error="copy failed",
        attempt=1,
        retry_count=0,
        exhausted=True,
    )
    service._write_state(session_dir, failed)

    result = service.reconcile_session("/repo", "s1")

    assert result.action == "repaired"
    assert _read_state(session_dir).generation == 1
    assert (staging_root / "projects" / session_dir.parent.name / "s1_v1").is_dir()
    assert not backup_root.exists()


def test_staged_backup_repeated_failure_preserves_attempt_reason_and_proofs(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    service, session_dir, _staging_root, _backup_root = _create_staged_service(monkeypatch, tmp_path)
    original_mirror = service._mirror
    proof = BackupPublicationProof("event-1", "pipeline_handoff_ready", 7)

    def fail_mirror(*_args, **_kwargs):
        raise OSError("local copy failed")

    monkeypatch.setattr(service, "_mirror", fail_mirror)
    first = service.backup_session(
        "/repo",
        "s1",
        reason=BackupReason.HANDOFF_READY,
        critical=False,
        publication_proofs={NORMAL_HANDOFF_PROOF_KEY: proof},
    )
    first_failed = _read_state(session_dir)
    second = service.backup_session(
        "/repo",
        "s1",
        reason=BackupReason.NORMAL_TURN_END,
        critical=False,
    )
    second_failed = _read_state(session_dir)

    assert first.succeeded is False and second.succeeded is False
    assert second_failed.attempt_commit_id == first_failed.attempt_commit_id
    assert second_failed.reason == BackupReason.HANDOFF_READY.value
    assert second_failed.attempt_publication_proofs == {NORMAL_HANDOFF_PROOF_KEY: proof}

    monkeypatch.setattr(service, "_mirror", original_mirror)
    succeeded = service.backup_session(
        "/repo",
        "s1",
        reason=BackupReason.NORMAL_TURN_END,
        critical=True,
    )
    committed = _read_state(session_dir)
    assert succeeded.succeeded is True
    assert committed.reason == BackupReason.HANDOFF_READY.value
    assert committed.publication_proofs == {NORMAL_HANDOFF_PROOF_KEY: proof}


def test_staged_backup_proof_conflict_keeps_original_failed_attempt(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    service, session_dir, _staging_root, _backup_root = _create_staged_service(monkeypatch, tmp_path)
    original = BackupPublicationProof("event-old", "pipeline_handoff_ready", 7)
    conflicting = BackupPublicationProof("event-new", "pipeline_handoff_ready", 7)
    failed = _read_state(session_dir).failed_attempt(
        reason=BackupReason.HANDOFF_READY.value,
        writer_id="writer",
        attempt_commit_id="attempt-1",
        attempted_proofs={NORMAL_HANDOFF_PROOF_KEY: original},
        error="copy failed",
        attempt=1,
        retry_count=0,
        exhausted=True,
    )
    service._write_state(session_dir, failed)

    result = service.backup_session(
        "/repo",
        "s1",
        reason=BackupReason.HANDOFF_READY,
        critical=False,
        publication_proofs={NORMAL_HANDOFF_PROOF_KEY: conflicting},
    )

    retained = _read_state(session_dir)
    assert result.succeeded is False
    assert retained.attempt_commit_id == failed.attempt_commit_id
    assert retained.reason == failed.reason
    assert retained.attempt_publication_proofs == {NORMAL_HANDOFF_PROOF_KEY: original}


def test_staged_reconcile_treats_concurrently_pruned_project_as_empty(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    service, session_dir, staging_root, _backup_root = _create_staged_service(monkeypatch, tmp_path)
    project_dir = staging_root / "projects" / session_dir.parent.name
    project_dir.mkdir(parents=True)
    original_iterdir = Path.iterdir

    def remove_before_iterdir(path: Path):
        if path == project_dir:
            path.rmdir()
            raise FileNotFoundError(path)
        return original_iterdir(path)

    monkeypatch.setattr(Path, "iterdir", remove_before_iterdir)

    result = service.reconcile_session("/repo", "s1")

    assert result.action == "current"


def test_staged_reconcile_treats_concurrently_published_snapshot_as_absent(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    service, session_dir, staging_root, _backup_root = _create_staged_service(monkeypatch, tmp_path)
    snapshot = staging_root / "projects" / session_dir.parent.name / "s1_v1"
    snapshot.mkdir(parents=True)
    original_read_state = service._read_state

    def remove_before_read(path: Path, *args, **kwargs):
        if path == snapshot:
            path.rmdir()
        return original_read_state(path, *args, **kwargs)

    monkeypatch.setattr(service, "_read_state", remove_before_read)

    result = service.reconcile_session("/repo", "s1")

    assert result.action == "current"


def test_staging_worker_publishes_versions_in_order_and_empties_staging_root(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    service, session_dir, staging_root, backup_root = _create_staged_service(monkeypatch, tmp_path)
    (session_dir / "session.jsonl").write_text("v1\n", encoding="utf-8")
    service.backup_session("/repo", "s1", reason=BackupReason.NORMAL_TURN_END, critical=True)
    (session_dir / "session.jsonl").write_text("v2\n", encoding="utf-8")
    service.backup_session("/repo", "s1", reason=BackupReason.TERMINAL, critical=True)
    worker = SessionBackupStagingWorker(staging_root, backup_root)
    published: list[int] = []
    original_publish = worker.publish_snapshot

    def record_publish(snapshot):
        published.append(snapshot.generation)
        original_publish(snapshot)

    worker.publish_snapshot = record_publish

    assert worker.run_once() == 2

    final_dir = backup_root / "projects" / session_dir.parent.name / "s1"
    assert published == [1, 2]
    assert (final_dir / "session.jsonl").read_text(encoding="utf-8") == "v2\n"
    assert _read_state(final_dir, shared=True).generation == 2
    assert list(staging_root.iterdir()) == []


def test_staging_worker_does_not_skip_failed_version_for_same_session(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    service, session_dir, staging_root, backup_root = _create_staged_service(monkeypatch, tmp_path)
    service.backup_session("/repo", "s1", reason=BackupReason.NORMAL_TURN_END, critical=True)
    service.backup_session("/repo", "s1", reason=BackupReason.TERMINAL, critical=True)
    worker = SessionBackupStagingWorker(staging_root, backup_root)
    attempted: list[int] = []

    def fail_first(snapshot):
        attempted.append(snapshot.generation)
        raise OSError("OSS unavailable")

    worker.publish_snapshot = fail_first

    assert worker.run_once() == 0
    assert attempted == [1]
    assert sorted(path.name for path in (staging_root / "projects" / session_dir.parent.name).iterdir()) == [
        "s1_v1",
        "s1_v2",
    ]


def test_staging_worker_publishes_different_sessions_concurrently(tmp_path: Path) -> None:
    worker = SessionBackupStagingWorker(
        tmp_path / "staging",
        tmp_path / "backup",
        max_concurrent_sessions=2,
    )
    snapshots = [
        StagedSessionSnapshot(tmp_path / "s1_v1", "project", "s1", 1),
        StagedSessionSnapshot(tmp_path / "s2_v1", "project", "s2", 1),
    ]
    barrier = threading.Barrier(2)
    started: list[str] = []
    started_lock = threading.Lock()

    def publish(snapshot: StagedSessionSnapshot) -> None:
        with started_lock:
            started.append(snapshot.session_id)
        barrier.wait(timeout=2.0)

    worker.scan_snapshots = lambda: snapshots
    worker.publish_snapshot = publish

    assert worker.run_once() == 2
    assert sorted(started) == ["s1", "s2"]


def test_staging_worker_failure_only_blocks_later_versions_of_that_session(tmp_path: Path) -> None:
    worker = SessionBackupStagingWorker(
        tmp_path / "staging",
        tmp_path / "backup",
        max_concurrent_sessions=2,
    )
    snapshots = [
        StagedSessionSnapshot(tmp_path / "s1_v1", "project", "s1", 1),
        StagedSessionSnapshot(tmp_path / "s1_v2", "project", "s1", 2),
        StagedSessionSnapshot(tmp_path / "s2_v1", "project", "s2", 1),
    ]
    attempted: list[tuple[str, int]] = []
    attempted_lock = threading.Lock()

    def publish(snapshot: StagedSessionSnapshot) -> None:
        with attempted_lock:
            attempted.append((snapshot.session_id, snapshot.generation))
        if snapshot.session_id == "s1":
            raise OSError("OSS unavailable")

    worker.scan_snapshots = lambda: snapshots
    worker.publish_snapshot = publish

    assert worker.run_once() == 1
    assert sorted(attempted) == [("s1", 1), ("s2", 1)]


def test_staging_worker_keeps_same_generation_conflict(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    service, session_dir, staging_root, backup_root = _create_staged_service(monkeypatch, tmp_path)
    (session_dir / "session.jsonl").write_text("staged\n", encoding="utf-8")
    service.backup_session("/repo", "s1", reason=BackupReason.TERMINAL, critical=True)
    snapshot = staging_root / "projects" / session_dir.parent.name / "s1_v1"
    final_dir = backup_root / "projects" / session_dir.parent.name / "s1"
    final_dir.mkdir(parents=True)
    (final_dir / "session.jsonl").write_text("existing\n", encoding="utf-8")
    conflict = replace(_read_state(snapshot, shared=True), commit_id="different-commit")
    service._write_state(final_dir, conflict)

    assert SessionBackupStagingWorker(staging_root, backup_root).run_once() == 0
    assert snapshot.is_dir()
    assert (final_dir / "session.jsonl").read_text(encoding="utf-8") == "existing\n"


def test_a2a_runtime_requires_final_root_and_rejects_overlapping_roots(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("IAC_CODE_CONFIG_BACKUP_TMP_DIR", str(tmp_path / "staging"))
    monkeypatch.delenv("IAC_CODE_CONFIG_BACKUP_DIR", raising=False)
    with pytest.raises(SessionBackupError, match="requires"):
        create_a2a_session_backup_runtime()

    monkeypatch.setenv("IAC_CODE_CONFIG_BACKUP_DIR", str(tmp_path / "staging" / "backup"))
    with pytest.raises(SessionBackupError, match="must not overlap"):
        create_a2a_session_backup_runtime()


def test_staging_process_cleans_copying_before_start_and_stops_child(tmp_path: Path) -> None:
    staging_root = tmp_path / "staging"
    copying = staging_root / "projects" / "project" / "s1_v1.copying"
    copying.mkdir(parents=True)
    backup_root = tmp_path / "backup"

    class FakeEvent:
        def __init__(self, *, ready: bool = False) -> None:
            self.was_set = False
            self.ready = ready
            self.wait_timeout: float | None = None

        def set(self) -> None:
            self.was_set = True

        def wait(self, timeout: float) -> bool:
            self.wait_timeout = timeout
            return self.ready

    class FakeProcess:
        pid = 123

        def __init__(self, **kwargs) -> None:
            self.kwargs = kwargs
            self.started = False

        def start(self) -> None:
            self.started = True

        def join(self, timeout: float) -> None:
            assert timeout == 5.0

        def is_alive(self) -> bool:
            return False

    class FakeContext:
        def __init__(self) -> None:
            self.events = [FakeEvent(), FakeEvent(ready=True)]
            self.created_events = list(self.events)
            self.process: FakeProcess | None = None

        def Event(self) -> FakeEvent:  # noqa: N802 - mirrors multiprocessing context API
            return self.events.pop(0)

        def Process(self, **kwargs) -> FakeProcess:  # noqa: N802 - mirrors multiprocessing context API
            self.process = FakeProcess(**kwargs)
            return self.process

    context = FakeContext()
    process = SessionBackupStagingProcess(staging_root, backup_root, process_context=context)

    process.start()
    assert not copying.exists()
    assert context.process is not None and context.process.started is True
    assert context.created_events[1].wait_timeout == 5.0
    process.close()
    assert context.created_events[0].was_set is True


def test_staging_worker_runs_final_scan_after_stop(monkeypatch: pytest.MonkeyPatch) -> None:
    scans: list[int] = []

    class FakeWorker:
        def __init__(self, _staging_root: str, _backup_root: str) -> None:
            pass

        def run_once(self) -> int:
            scans.append(len(scans) + 1)
            return 0

    class FakeStopEvent:
        def is_set(self) -> bool:
            return bool(scans)

        def wait(self, _timeout: float) -> bool:
            return self.is_set()

    class FakeReadyEvent:
        was_set = False

        def set(self) -> None:
            self.was_set = True

    monkeypatch.setattr("iac_code.services.session_backup_staging.SessionBackupStagingWorker", FakeWorker)
    ready_event = FakeReadyEvent()

    run_staging_worker("/staging", "/backup", FakeStopEvent(), ready_event)

    assert ready_event.was_set is True
    assert scans == [1, 2]
