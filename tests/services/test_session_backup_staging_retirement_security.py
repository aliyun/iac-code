from __future__ import annotations

import json
import os
import subprocess
from contextlib import contextmanager
from pathlib import Path

import pytest

from iac_code.services.session_backup import BACKUP_STATE_FILENAME, SessionBackupError, SessionBackupService
from iac_code.services.session_backup_staging import (
    _COPYING_OWNER_MARKER_FILENAME,
    SessionBackupStagingWorker,
    _CopyingSnapshotOwnerLock,
)
from iac_code.services.session_backup_state import SessionBackupState


class _RetiredSnapshotFixture:
    def __init__(self, tmp_path: Path) -> None:
        self.staging = tmp_path / "staging"
        self.projects = self.staging / "projects"
        self.project = self.projects / "project-1"
        self.retired_name = ".retired-" + "a" * 32
        self.outside = tmp_path / "outside"
        self.worker = SessionBackupStagingWorker(self.staging, tmp_path / "shared")
        self.state = SessionBackupState.bootstrap("s1", writer_id="writer-1").committed_next(
            commit_id="staged-unmanaged:original", reason="terminal", writer_id="writer-1", proofs={}
        )

    def snapshot(self, parent: Path, *, name: str | None = None) -> Path:
        retired = parent / (name or self.retired_name)
        retired.mkdir(parents=True)
        (retired / BACKUP_STATE_FILENAME).write_text(json.dumps(self.state.to_dict()), encoding="utf-8")
        (retired / "session.jsonl").write_text("retired-boundary\n", encoding="utf-8")
        if retired.name.endswith(".copying"):
            (retired / _COPYING_OWNER_MARKER_FILENAME).write_text('{"version": 1}', encoding="utf-8")
        return retired

    @staticmethod
    def symlink(target: Path, link: Path) -> None:
        link.parent.mkdir(parents=True, exist_ok=True)
        try:
            link.symlink_to(target, target_is_directory=True)
        except (NotImplementedError, OSError) as exc:
            pytest.skip(f"directory symlink unsupported: {exc}")


class _SnapshotPathSwap:
    def __init__(self, fixture: _RetiredSnapshotFixture, source: Path, outside: Path, *, kind: str, phase: str) -> None:
        self.fixture = fixture
        self.source = source
        self.outside = outside
        self.kind = kind
        self.phase = phase
        self.entered = False
        self.original_source = source
        self.original_read = fixture.worker._service._read_state
        self.original_acquire = _CopyingSnapshotOwnerLock.acquire

    def swap(self) -> None:
        if self.entered:
            return
        self.entered = True
        if self.kind == "ancestor":
            original_project = self.fixture.projects / "project-original"
            self.fixture.project.rename(original_project)
            self.original_source = original_project / self.source.name
            self.fixture.symlink(self.fixture.outside, self.fixture.project)
        elif self.kind == "leaf":
            self.original_source = self.source.with_name("copying-original")
            self.source.rename(self.original_source)
            self.fixture.symlink(self.outside, self.source)
        else:
            self.fixture.symlink(self.outside, self.source.with_name("s1_v1"))

    def read_state(self, path: Path, **kwargs):
        state = self.original_read(path, **kwargs)
        if self.phase == "read-state" and path == self.source:
            self.swap()
        elif self.phase == "destination-state" and path == self.source.with_name("s1_v1"):
            self.swap()
        elif (
            self.phase == "shared-commit" and state is not None and path.is_relative_to(self.fixture.worker.backup_root)
        ):
            self.swap()
        return state

    @contextmanager
    def acquire(self, lease: _CopyingSnapshotOwnerLock, *, blocking: bool):
        with self.original_acquire(lease, blocking=blocking) as acquired:
            if acquired:
                self.swap()
            yield acquired


@pytest.mark.parametrize(
    "kind,phase",
    [
        ("ancestor", "owner-lock"),
        ("ancestor", "read-state"),
        ("leaf", "read-state"),
        ("destination", "read-state"),
        ("ancestor", "destination-state"),
    ],
)
def test_copying_recovery_rechecks_paths_after_owned_io(monkeypatch, tmp_path, kind, phase) -> None:
    fixture = _RetiredSnapshotFixture(tmp_path)
    copying = fixture.snapshot(fixture.project, name="s1_v1.copying")
    outside = fixture.snapshot(fixture.outside, name="s1_v1.copying")
    if phase == "destination-state":
        fixture.snapshot(fixture.project, name="s1_v1")
    boundary = _SnapshotPathSwap(fixture, copying, outside, kind=kind, phase=phase)
    monkeypatch.setattr(fixture.worker._service, "_read_state", boundary.read_state)
    if phase == "owner-lock":
        monkeypatch.setattr(
            _CopyingSnapshotOwnerLock, "acquire", lambda lease, **kwargs: boundary.acquire(lease, **kwargs)
        )

    assert fixture.worker.cleanup_incomplete_snapshots() == 0

    assert boundary.entered
    assert boundary.original_source.is_dir()
    assert outside.is_dir()
    assert (outside / _COPYING_OWNER_MARKER_FILENAME).is_file()
    assert (outside / "session.jsonl").read_text(encoding="utf-8") == "retired-boundary\n"
    assert not (fixture.outside / "s1_v1").exists()


@pytest.mark.parametrize("kind", ["ancestor", "leaf"])
def test_snapshot_retirement_rechecks_paths_after_shared_commit(monkeypatch, tmp_path, kind) -> None:
    fixture = _RetiredSnapshotFixture(tmp_path)
    fixture.worker.coordinator_state_root = tmp_path / "state"
    snapshot = fixture.snapshot(fixture.project, name="s1_v1")
    outside = fixture.snapshot(fixture.outside, name="s1_v1")
    boundary = _SnapshotPathSwap(fixture, snapshot, outside, kind=kind, phase="shared-commit")
    monkeypatch.setattr(fixture.worker._service, "_read_state", boundary.read_state)
    scanned = fixture.worker.scan_snapshots()[0]

    with pytest.raises(SessionBackupError, match="ancestry|unsafe"):
        fixture.worker.publish_snapshot(scanned)

    assert boundary.entered
    assert boundary.original_source.is_dir()
    assert outside.is_dir()
    assert (outside / "session.jsonl").read_text(encoding="utf-8") == "retired-boundary\n"
    assert not list(fixture.outside.glob(".retired-*"))


@pytest.mark.parametrize("ancestor", ["projects", "project"])
@pytest.mark.parametrize("shape", ["retired", "copying"])
def test_snapshot_cleanup_preserves_directory_outside_symlinked_ancestry(tmp_path, ancestor, shape) -> None:
    fixture = _RetiredSnapshotFixture(tmp_path)
    outside_parent = fixture.outside / "project-1" if ancestor == "projects" else fixture.outside
    retired = fixture.snapshot(outside_parent, name="s1_v1.copying" if shape == "copying" else None)
    target, link = (fixture.outside, fixture.projects) if ancestor == "projects" else (fixture.outside, fixture.project)
    fixture.symlink(target, link)

    fixture.worker.cleanup_incomplete_snapshots()

    assert retired.is_dir()
    assert (retired / "session.jsonl").read_text(encoding="utf-8") == "retired-boundary\n"
    if shape == "copying":
        assert (retired / _COPYING_OWNER_MARKER_FILENAME).is_file()
        assert not (outside_parent / "s1_v1").exists()


def test_snapshot_removal_rejects_symlinked_parent_even_when_leaf_is_regular(tmp_path) -> None:
    fixture = _RetiredSnapshotFixture(tmp_path)
    retired = fixture.snapshot(fixture.outside)
    fixture.symlink(fixture.outside, fixture.project)

    with pytest.raises(SessionBackupError, match="ancestry|outside"):
        fixture.worker._remove_snapshot(fixture.project / fixture.retired_name)

    assert retired.is_dir()


@pytest.mark.parametrize("shape", ["retired", "copying"])
def test_snapshot_cleanup_skips_reparse_project_on_python_without_is_junction(monkeypatch, tmp_path, shape) -> None:
    fixture = _RetiredSnapshotFixture(tmp_path)
    retired = fixture.snapshot(fixture.project, name="s1_v1.copying" if shape == "copying" else None)
    original = SessionBackupService._is_reparse_point
    monkeypatch.setattr(
        SessionBackupService,
        "_is_reparse_point",
        staticmethod(lambda path: Path(path) == fixture.project or original(path)),
    )

    fixture.worker.cleanup_incomplete_snapshots()

    assert retired.is_dir()
    with pytest.raises(SessionBackupError, match="ancestry"):
        fixture.worker._remove_snapshot(retired)
    assert retired.is_dir()


@pytest.mark.skipif(os.name != "nt", reason="native Windows junction coverage")
@pytest.mark.parametrize("shape", ["retired", "copying"])
def test_snapshot_cleanup_preserves_directory_outside_windows_junction(tmp_path, shape) -> None:
    fixture = _RetiredSnapshotFixture(tmp_path)
    retired = fixture.snapshot(fixture.outside, name="s1_v1.copying" if shape == "copying" else None)
    fixture.projects.mkdir(parents=True)
    result = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(fixture.project), str(fixture.outside)],
        capture_output=True,
        encoding="utf-8",
        check=False,
    )
    if result.returncode:
        pytest.skip("directory junction creation unsupported")

    fixture.worker.cleanup_incomplete_snapshots()

    assert retired.is_dir()
    assert (retired / "session.jsonl").read_text(encoding="utf-8") == "retired-boundary\n"


def test_retired_cleanup_removes_valid_committed_snapshot(tmp_path) -> None:
    fixture = _RetiredSnapshotFixture(tmp_path)
    retired = fixture.snapshot(fixture.project)

    fixture.worker.cleanup_incomplete_snapshots()

    assert not retired.exists()


def test_snapshot_cleanup_recovers_safe_complete_copying_snapshot(tmp_path) -> None:
    fixture = _RetiredSnapshotFixture(tmp_path)
    copying = fixture.snapshot(fixture.project, name="s1_v1.copying")

    assert fixture.worker.cleanup_incomplete_snapshots() == 1

    assert not copying.exists()
    destination = fixture.project / "s1_v1"
    assert destination.is_dir()
    assert not (destination / _COPYING_OWNER_MARKER_FILENAME).exists()
    assert (destination / "session.jsonl").read_text(encoding="utf-8") == "retired-boundary\n"


@pytest.mark.parametrize(
    "operation,directory",
    [("cleanup", "projects"), ("cleanup", "project"), ("scan", "projects"), ("scan", "project"), ("prune", "projects")],
)
def test_snapshot_enumeration_tolerates_other_publisher_pruning_directory(
    monkeypatch, tmp_path, operation, directory
) -> None:
    fixture = _RetiredSnapshotFixture(tmp_path)
    target = fixture.projects if directory == "projects" else fixture.project
    target.mkdir(parents=True)
    original_iterdir = Path.iterdir
    entered = False

    def prune_then_enumerate(path: Path):
        nonlocal entered
        if path == target and not entered:
            entered = True
            target.rmdir()
        return original_iterdir(path)

    monkeypatch.setattr(Path, "iterdir", prune_then_enumerate)
    if operation == "cleanup":
        assert fixture.worker.cleanup_incomplete_snapshots() == 0
    elif operation == "scan":
        assert fixture.worker.scan_snapshots() == []
    else:
        fixture.worker._prune_empty_staging_dirs()
    assert entered
    assert not target.exists()


@pytest.mark.parametrize("error_type", [PermissionError, OSError])
def test_snapshot_enumeration_io_error_preserves_snapshot_and_propagates(monkeypatch, tmp_path, error_type) -> None:
    fixture = _RetiredSnapshotFixture(tmp_path)
    retired = fixture.snapshot(fixture.project)
    original_iterdir = Path.iterdir

    def fail_scan(path: Path):
        if path == fixture.projects:
            raise error_type("injected project enumeration IO failure")
        return original_iterdir(path)

    monkeypatch.setattr(Path, "iterdir", fail_scan)
    with pytest.raises(error_type, match="injected project enumeration IO failure"):
        fixture.worker.cleanup_incomplete_snapshots()
    assert (retired / "session.jsonl").read_text(encoding="utf-8") == "retired-boundary\n"


@pytest.mark.parametrize("metadata", ["missing", "bad-json", "failed", "uncommitted", "invalid-status", "symlink"])
def test_retired_cleanup_retains_snapshot_without_valid_committed_metadata(tmp_path, metadata) -> None:
    fixture = _RetiredSnapshotFixture(tmp_path)
    retired = fixture.snapshot(fixture.project)
    marker = retired / BACKUP_STATE_FILENAME
    if metadata == "missing":
        marker.unlink()
    elif metadata == "bad-json":
        marker.write_text("{incomplete-json", encoding="utf-8")
    elif metadata == "uncommitted":
        marker.write_text(
            json.dumps(SessionBackupState.bootstrap("s1", writer_id="writer-1").to_dict()), encoding="utf-8"
        )
    elif metadata == "invalid-status":
        document = fixture.state.to_dict()
        document["status"] = []
        marker.write_text(json.dumps(document), encoding="utf-8")
    elif metadata == "symlink":
        outside_marker = tmp_path / "outside-state.json"
        outside_marker.write_bytes(marker.read_bytes())
        marker.unlink()
        try:
            marker.symlink_to(outside_marker)
        except (NotImplementedError, OSError) as exc:
            pytest.skip(f"file symlink unsupported: {exc}")
    else:
        failed = fixture.state.failed_attempt(
            reason="terminal",
            writer_id="writer-1",
            attempt_commit_id="failed-attempt",
            attempted_proofs={},
            error="failed capture",
            attempt=1,
            retry_count=0,
            exhausted=True,
        )
        marker.write_text(json.dumps(failed.to_dict()), encoding="utf-8")

    fixture.worker.cleanup_incomplete_snapshots()

    assert retired.is_dir()
    assert (retired / "session.jsonl").read_text(encoding="utf-8") == "retired-boundary\n"
