from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path

import pytest

from iac_code.a2a.backup import (
    PENDING_JOBS_DIRNAME,
    SessionBackupCoordinator,
    SessionBackupHandoffError,
    backup_session_async,
)
from iac_code.services.session_backup import BackupReason, BackupResult
from iac_code.services.session_backup_staging import StagedSessionBackupService
from iac_code.services.session_metadata import SESSION_LAYOUT_VERSION_V2, SessionMetadata, write_session_metadata
from iac_code.services.session_storage import SessionStorage


class RecordingStagedBackupService:
    def __init__(self) -> None:
        self.wait_calls = 0

    def backup_session(self, *_args, **_kwargs) -> BackupResult:
        return BackupResult(
            enabled=True,
            destination=Path("/tmp/staging/projects/project/session_v1"),
            generation=1,
            commit_id="commit-1",
            staged_committed=True,
            shared_committed=False,
        )

    def wait_for_shared_commit(
        self,
        result: BackupResult,
        *,
        timeout: float | None = None,
    ) -> BackupResult:
        del result, timeout
        self.wait_calls += 1
        raise AssertionError("A2A terminal backup must not wait for shared publication")


@pytest.mark.asyncio
async def test_terminal_backup_returns_after_local_staging_without_shared_wait() -> None:
    service = RecordingStagedBackupService()

    result = await backup_session_async(
        service,
        "/repo",
        "session",
        reason=BackupReason.TERMINAL,
        critical=True,
    )

    assert result.staged_committed is True
    assert result.shared_committed is False
    assert service.wait_calls == 0


@pytest.mark.asyncio
async def test_non_terminal_backup_keeps_existing_local_staging_behavior() -> None:
    service = RecordingStagedBackupService()

    result = await backup_session_async(
        service,
        "/repo",
        "session",
        reason=BackupReason.NORMAL_TURN_END,
        critical=False,
    )

    assert result.staged_committed is True
    assert result.shared_committed is False
    assert service.wait_calls == 0


async def _wait_for(predicate, *, timeout: float = 5.0) -> None:
    async def wait() -> None:
        while not predicate():
            await asyncio.sleep(0.005)

    await asyncio.wait_for(wait(), timeout)


def _staged_coordinator(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> tuple[SessionBackupCoordinator, StagedSessionBackupService, Path, Path, Path]:
    config_root = tmp_path / "config"
    staging_root = tmp_path / "staging"
    backup_root = tmp_path / "backup"
    state_root = tmp_path / "state"
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
    coordinator = SessionBackupCoordinator(service, state_root=state_root, retry_delays=())
    return coordinator, service, session_dir, staging_root, state_root


async def _register(coordinator: SessionBackupCoordinator, *, execution_id: str = "exec-1"):
    return await coordinator.register_boundary(
        cwd="/repo",
        session_id="s1",
        context_id="ctx-1",
        execution_id=execution_id,
        boundary="natural_completion",
        reason=BackupReason.TERMINAL,
    )


@pytest.mark.asyncio
async def test_register_boundary_persists_a_pending_marker_before_any_snapshot_exists(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    coordinator, service, session_dir, staging_root, state_root = _staged_coordinator(monkeypatch, tmp_path)
    (session_dir / "session.jsonl").write_text("v1\n", encoding="utf-8")
    started, release = threading.Event(), threading.Event()
    original = service.backup_session

    def gated(*args, **kwargs):
        started.set()
        assert release.wait(5)
        return original(*args, **kwargs)

    monkeypatch.setattr(service, "backup_session", gated)
    try:
        handoff = await _register(coordinator)

        assert handoff.job_id is not None
        assert handoff.backup_disabled is False
        assert handoff.business_revision == 1
        marker = staging_root / PENDING_JOBS_DIRNAME / "{}.json".format(handoff.job_id)
        document = json.loads(marker.read_text(encoding="utf-8"))
        assert document["sessionId"] == "s1"
        assert document["boundary"] == "natural_completion"
        assert document["businessRevision"] == 1
        assert document["fence"] == 1
        # The tmp probe must already see the to-do marker while no snapshot exists.
        assert sorted(path.name for path in staging_root.iterdir()) == [PENDING_JOBS_DIRNAME]
        assert await asyncio.to_thread(started.wait, 5)

        release.set()
        await _wait_for(lambda: not marker.exists())
        snapshot = staging_root / "projects" / session_dir.parent.name / "s1_v1"
        # The marker only disappears once the sealed snapshot keeps tmp non-empty.
        assert snapshot.is_dir()
        assert not (staging_root / PENDING_JOBS_DIRNAME).exists()
        receipt = json.loads(
            (state_root / "session-backup-coordinator" / session_dir.parent.name / "s1" / "receipts")
            .joinpath("{}.json".format(handoff.job_id))
            .read_text(encoding="utf-8")
        )
        assert receipt["coveredThroughBusinessRevision"] == 1
        assert receipt["backupGeneration"] == 1
    finally:
        release.set()
        await coordinator.aclose()


@pytest.mark.asyncio
async def test_failed_capture_keeps_its_marker_until_a_later_snapshot_covers_it(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    coordinator, service, session_dir, staging_root, state_root = _staged_coordinator(monkeypatch, tmp_path)
    (session_dir / "session.jsonl").write_text("v1\n", encoding="utf-8")
    original = service.backup_session
    failed: list[bool] = []

    def failing_first(*args, **kwargs):
        if not failed:
            failed.append(True)
            raise OSError("injected staging failure")
        return original(*args, **kwargs)

    monkeypatch.setattr(service, "backup_session", failing_first)
    try:
        first = await _register(coordinator)
        first_marker = staging_root / PENDING_JOBS_DIRNAME / "{}.json".format(first.job_id)
        await _wait_for(lambda: json.loads(first_marker.read_text(encoding="utf-8"))["error"] is not None)
        assert json.loads(first_marker.read_text(encoding="utf-8"))["attempts"] == 1

        # A released session lock lets the next boundary capture a fresh snapshot.
        await asyncio.wait_for(
            coordinator.wait_for_local_snapshot_quiescence(cwd="/repo", session_id="s1", timeout=5.0),
            5,
        )
        (session_dir / "session.jsonl").write_text("v2\n", encoding="utf-8")
        second = await _register(coordinator)
        assert second.business_revision == 2
        second_marker = staging_root / PENDING_JOBS_DIRNAME / "{}.json".format(second.job_id)
        await _wait_for(lambda: not second_marker.exists())

        snapshot = staging_root / "projects" / session_dir.parent.name / "s1_v1"
        assert (snapshot / "session.jsonl").read_text(encoding="utf-8") == "v2\n"
        # The accumulated snapshot covers the earlier revision, so both markers go away.
        assert not first_marker.exists()
        assert not (staging_root / PENDING_JOBS_DIRNAME).exists()
        receipt = json.loads(
            (state_root / "session-backup-coordinator" / session_dir.parent.name / "s1" / "receipts")
            .joinpath("{}.json".format(second.job_id))
            .read_text(encoding="utf-8")
        )
        assert receipt["coveredThroughBusinessRevision"] == 2
    finally:
        await coordinator.aclose()


@pytest.mark.asyncio
async def test_recover_rearms_a_pending_marker_left_by_a_previous_process(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    coordinator, service, session_dir, staging_root, _state_root = _staged_coordinator(monkeypatch, tmp_path)
    (session_dir / "session.jsonl").write_text("v1\n", encoding="utf-8")
    crashed = True
    original = service.backup_session

    def crash_before_copy(*args, **kwargs):
        if crashed:
            raise OSError("previous process died before copying")
        return original(*args, **kwargs)

    monkeypatch.setattr(service, "backup_session", crash_before_copy)
    handoff = await _register(coordinator)
    marker = staging_root / PENDING_JOBS_DIRNAME / "{}.json".format(handoff.job_id)
    await _wait_for(lambda: json.loads(marker.read_text(encoding="utf-8"))["error"] is not None)
    await coordinator.aclose()
    crashed = False

    successor = SessionBackupCoordinator(service, state_root=tmp_path / "state", retry_delays=())
    try:
        assert await successor.recover() == 1
        await _wait_for(lambda: not marker.exists())
        assert (staging_root / "projects" / session_dir.parent.name / "s1_v1").is_dir()
        assert await successor.recover() == 0
    finally:
        await successor.aclose()


@pytest.mark.asyncio
async def test_register_boundary_rejects_a_closed_coordinator_instead_of_faking_a_handoff(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    coordinator, _service, _session_dir, _staging_root, _state_root = _staged_coordinator(monkeypatch, tmp_path)
    await coordinator.aclose()

    with pytest.raises(SessionBackupHandoffError):
        await _register(coordinator)


@pytest.mark.asyncio
async def test_wait_for_local_snapshot_quiescence_does_not_block_on_an_inflight_capture(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    coordinator, service, session_dir, _staging_root, _state_root = _staged_coordinator(monkeypatch, tmp_path)
    (session_dir / "session.jsonl").write_text("v1\n", encoding="utf-8")
    started, release = threading.Event(), threading.Event()
    original = service.backup_session

    def gated(*args, **kwargs):
        started.set()
        assert release.wait(5)
        return original(*args, **kwargs)

    monkeypatch.setattr(service, "backup_session", gated)
    try:
        await _register(coordinator)
        # The local capture is now stuck inside backup_session and will not finish.
        assert await asyncio.to_thread(started.wait, 5)
        # The next turn must observe generation-fenced staging and start immediately;
        # it must never block on the in-flight (potentially slow) local capture.
        await asyncio.wait_for(
            coordinator.wait_for_local_snapshot_quiescence(cwd="/repo", session_id="s1"),
            timeout=1,
        )
    finally:
        release.set()
        await coordinator.aclose()


@pytest.mark.asyncio
async def test_disabled_coordinator_reports_a_backup_disabled_handoff(tmp_path: Path) -> None:
    coordinator = SessionBackupCoordinator(None, state_root=tmp_path / "state")

    handoff = await _register(coordinator)

    assert coordinator.enabled is False
    assert handoff.backup_disabled is True
    assert handoff.job_id is None
