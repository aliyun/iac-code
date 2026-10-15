from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

import pytest

from iac_code.agent.message import Message
from iac_code.services.session_backup import (
    BackupReason,
    SessionBackupBlocked,
    SessionBackupError,
    SessionBackupService,
)
from iac_code.services.session_backup_staging import SessionBackupStagingWorker, StagedSessionBackupService
from iac_code.services.session_metadata import SESSION_LAYOUT_VERSION_V2, SessionMetadata, write_session_metadata
from iac_code.services.session_mutation_guard import session_mutation_guard
from iac_code.services.session_storage import SessionStorage


class _SharedPublicationCase:
    def __init__(self, monkeypatch: pytest.MonkeyPatch, root: Path, staged: bool) -> None:
        self.root = root
        self.backup = root / "backup"
        monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(root / "config"))
        monkeypatch.setenv("IAC_CODE_CONFIG_BACKUP_DIR", str(self.backup))
        self.storage = SessionStorage(projects_dir=root / "config" / "projects")
        self.session = self.storage.session_dir("/repo", "s1")
        write_session_metadata(
            self.session, SessionMetadata(session_id="s1", cwd="/repo", layout_version=SESSION_LAYOUT_VERSION_V2)
        )
        self.service = (
            StagedSessionBackupService(root / "staging", self.storage, retry_delays=())
            if staged
            else SessionBackupService(self.storage, retry_delays=())
        )
        self.service.initialize_session("/repo", "s1")
        self.worker = SessionBackupStagingWorker(root / "staging", self.backup)
        self.staged = staged
        self.shared = self.backup / "projects" / self.session.parent.name / "s1"

    def payload(self, value: str) -> None:
        for name in ("session.jsonl", "first.txt", "second.txt"):
            (self.session / name).write_text(value, encoding="utf-8")

    def publish(self) -> Any:
        result = self.service.backup_session(
            "/repo", "s1", reason=BackupReason.TERMINAL, critical=True, operation_commit_id="operation-2"
        )
        if self.staged:
            return self.worker.run_once()
        return result

    def cold_service(self) -> SessionBackupService:
        return SessionBackupService(SessionStorage(projects_dir=self.root / "cold" / "projects"), retry_delays=())


@pytest.mark.parametrize("staged", [False, True])
@pytest.mark.parametrize("initial_generation", [False, True])
@pytest.mark.parametrize("crash", [False, True])
def test_partial_shared_publication_is_not_restored_and_exact_retry_repairs_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, staged: bool, initial_generation: bool, crash: bool
) -> None:
    case = _SharedPublicationCase(monkeypatch, tmp_path, staged)
    if initial_generation:
        case.payload("generation-1\n")
        case.publish()
    case.payload("generation-2-longer\n")
    original = SessionBackupService._copy_file

    def interrupt_after_one_file(self: SessionBackupService, source: Path, destination: Path) -> None:
        original(self, source, destination)
        if destination.is_relative_to(case.shared):
            if crash:
                raise KeyboardInterrupt("publisher process died")
            raise OSError("shared publication interrupted")

    with monkeypatch.context() as failures:
        failures.setattr(SessionBackupService, "_copy_file", interrupt_after_one_file)
        if crash:
            with pytest.raises(KeyboardInterrupt):
                case.publish()
        elif staged:
            assert case.publish() == 0
        else:
            with pytest.raises(SessionBackupBlocked):
                case.publish()
    with pytest.raises(SessionBackupError, match="publication.*incomplete"):
        case.cold_service().reconcile_session("/repo", "s1")

    if staged:
        assert case.worker.run_once() == 1
    else:
        case.publish()
    cold = case.cold_service()
    restored = cold.reconcile_session("/repo", "s1")
    assert restored.action == "restored"
    assert restored.state is not None and restored.state.generation == (2 if initial_generation else 1)
    destination = cold._session_storage.session_dir("/repo", "s1")
    for name in ("session.jsonl", "first.txt", "second.txt"):
        assert (destination / name).read_text(encoding="utf-8") == "generation-2-longer\n"


def test_direct_shared_publication_holds_restore_lock_until_payload_and_state_commit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    case = _SharedPublicationCase(monkeypatch, tmp_path, False)
    case.payload("generation-1\n")
    case.publish()
    case.payload("generation-2-longer\n")
    payload_written = threading.Event()
    release_writer = threading.Event()
    restore_started = threading.Event()
    restore_finished = threading.Event()
    results: list[Any] = []
    original = case.service._mirror

    def block_after_payload(source: Path, destination: Path) -> Any:
        result = original(source, destination)
        payload_written.set()
        assert release_writer.wait(5)
        return result

    monkeypatch.setattr(case.service, "_mirror", block_after_payload)

    def write() -> None:
        try:
            results.append(case.publish())
        except BaseException as exc:
            results.append(exc)

    def restore() -> None:
        restore_started.set()
        try:
            results.append(case.cold_service().reconcile_session("/repo", "s1"))
        except BaseException as exc:
            results.append(exc)
        finally:
            restore_finished.set()

    writer = threading.Thread(target=write)
    reader = threading.Thread(target=restore)
    writer.start()
    try:
        assert payload_written.wait(5)
        reader.start()
        assert restore_started.wait(5)
        assert not restore_finished.wait(0.1), "cold restore read a publication before its state committed"
    finally:
        release_writer.set()
        writer.join(5)
        if reader.ident is not None:
            reader.join(5)
    assert not writer.is_alive() and not reader.is_alive()
    assert not any(isinstance(result, BaseException) for result in results)
    assert results[-1].state.generation == 2


@pytest.mark.parametrize("staged", [False, True])
def test_committed_shared_state_with_pending_publication_marker_retries_without_new_generation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, staged: bool
) -> None:
    case = _SharedPublicationCase(monkeypatch, tmp_path, staged)
    case.payload("complete-generation-1\n")
    original = SessionBackupService._complete_shared_publication

    def fail_marker_cleanup(self: SessionBackupService, destination: Path, state: Any) -> None:
        raise OSError("publication marker cleanup failed")

    with monkeypatch.context() as failure:
        failure.setattr(SessionBackupService, "_complete_shared_publication", fail_marker_cleanup)
        if staged:
            assert case.publish() == 0
        else:
            with pytest.raises(SessionBackupBlocked):
                case.publish()
    with pytest.raises(SessionBackupError, match="publication.*incomplete"):
        case.cold_service().reconcile_session("/repo", "s1")

    monkeypatch.setattr(SessionBackupService, "_complete_shared_publication", original)
    if staged:
        assert case.worker.run_once() == 1
    else:
        case.publish()
    result = case.cold_service().reconcile_session("/repo", "s1")
    assert result.state is not None and result.state.generation == 1


@pytest.mark.parametrize("staged", [False, True])
def test_incomplete_shared_publication_rejects_another_capture_identity(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, staged: bool
) -> None:
    case = _SharedPublicationCase(monkeypatch, tmp_path, staged)
    case.payload("owned-capture\n")
    original = SessionBackupService._copy_file

    def interrupt(self: SessionBackupService, source: Path, destination: Path) -> None:
        original(self, source, destination)
        if destination == case.shared / "session.jsonl":
            raise OSError("publication interrupted")

    with monkeypatch.context() as failure:
        failure.setattr(SessionBackupService, "_copy_file", interrupt)
        if staged:
            assert case.publish() == 0
        else:
            with pytest.raises(SessionBackupBlocked):
                case.publish()
    snapshot = case.worker.scan_snapshots()[0].path if staged else case.session
    state = case.service._read_state(snapshot, session_id="s1")
    assert state is not None
    other = state.committed_next(
        commit_id="different-capture", reason=BackupReason.TERMINAL.value, writer_id="another-writer", proofs={}
    )
    before = (case.shared / "session.jsonl").read_bytes()
    with pytest.raises(SessionBackupError, match="conflicts with incomplete capture"):
        case.service._publish_shared_snapshot(snapshot, case.shared, other)
    assert (case.shared / "session.jsonl").read_bytes() == before


@pytest.mark.parametrize("initial_generation", [False, True])
def test_direct_reconciliation_repairs_its_exact_interrupted_publication(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, initial_generation: bool
) -> None:
    case = _SharedPublicationCase(monkeypatch, tmp_path, False)
    if initial_generation:
        case.payload("old\n")
        case.publish()
    case.payload("repair-this-capture\n")
    original = SessionBackupService._copy_file

    def interrupt(self: SessionBackupService, source: Path, destination: Path) -> None:
        original(self, source, destination)
        if destination.is_relative_to(case.shared):
            raise OSError("publication interrupted")

    with monkeypatch.context() as failure:
        failure.setattr(SessionBackupService, "_copy_file", interrupt)
        with pytest.raises(SessionBackupBlocked):
            case.publish()
    result = case.service.reconcile_session("/repo", "s1")
    assert result.action == "repaired"
    assert result.state is not None and result.state.generation == (2 if initial_generation else 1)
    assert case.cold_service().reconcile_session("/repo", "s1").action == "restored"


def test_direct_capture_waits_for_complete_guarded_history_and_sidecar_mutation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    case = _SharedPublicationCase(monkeypatch, tmp_path, False)
    case.storage.append("/repo", "s1", Message(role="user", content="before"))
    sidecar = case.session / "compound-state.json"
    sidecar.write_text(json.dumps({"historyMessages": 1}), encoding="utf-8")
    history_updated = threading.Event()
    release_mutation = threading.Event()
    capture_started = threading.Event()
    mirror_started = threading.Event()
    results: list[Any] = []
    original = case.service._mirror

    def observe_mirror(source: Path, destination: Path) -> Any:
        mirror_started.set()
        return original(source, destination)

    monkeypatch.setattr(case.service, "_mirror", observe_mirror)

    def mutate() -> None:
        with session_mutation_guard(case.session):
            case.storage.append("/repo", "s1", Message(role="user", content="after"))
            history_updated.set()
            assert release_mutation.wait(5)
            sidecar.write_text(json.dumps({"historyMessages": 2}), encoding="utf-8")

    def capture() -> None:
        capture_started.set()
        try:
            results.append(case.publish())
        except BaseException as exc:
            results.append(exc)

    writer = threading.Thread(target=mutate)
    backup = threading.Thread(target=capture)
    writer.start()
    try:
        assert history_updated.wait(5)
        backup.start()
        assert capture_started.wait(5)
        assert not mirror_started.wait(0.1), "backup copied an unfinished compound session mutation"
    finally:
        release_mutation.set()
        writer.join(5)
        if backup.ident is not None:
            backup.join(5)
    assert not writer.is_alive() and not backup.is_alive()
    assert len(results) == 1 and not isinstance(results[0], BaseException)
    assert results[0].succeeded
    history = (case.shared / "session.jsonl").read_text(encoding="utf-8").splitlines()
    assert [json.loads(row)["content"] for row in history] == ["before", "after"]
    assert json.loads((case.shared / "compound-state.json").read_text(encoding="utf-8")) == {"historyMessages": 2}
