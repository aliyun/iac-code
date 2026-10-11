from __future__ import annotations

import asyncio
import json
import shutil
import threading
from pathlib import Path

import pytest

from iac_code.a2a.backup import (
    PENDING_JOBS_DIRNAME,
    SessionBackupCoordinator,
    SessionBackupHandoffError,
    SessionBackupJob,
    backup_session_async,
)
from iac_code.a2a.input_required import PermissionInputRegistry
from iac_code.a2a.persistence import A2APersistenceStore, A2ATaskSnapshot
from iac_code.a2a.task_store import A2ATaskStore
from iac_code.services.permission_wait import (
    PermissionWaitCheckpointStore,
    PermissionWaitPolicy,
    build_permission_checkpoint,
)
from iac_code.services.session_backup import BackupReason, BackupResult, SessionBackupBlocked
from iac_code.services.session_backup_staging import SessionBackupStagingWorker, StagedSessionBackupService
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


@pytest.mark.asyncio
async def test_noncritical_staged_backup_failure_is_not_reported_as_success(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    coordinator, service, session_dir, staging_root, _state_root = _staged_coordinator(monkeypatch, tmp_path)
    (session_dir / "session.jsonl").write_text("boundary-v1\n", encoding="utf-8")

    def fail_local_copy(*_args, **_kwargs):
        raise OSError("injected local staging copy failure")

    monkeypatch.setattr(service, "_mirror", fail_local_copy)
    try:
        with pytest.raises(SessionBackupBlocked):
            await backup_session_async(
                service,
                "/repo",
                "s1",
                reason=BackupReason.NORMAL_TURN_END,
                critical=False,
            )
        assert not list((staging_root / "projects").glob("**/s1_v*"))
    finally:
        await coordinator.aclose()


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
async def test_exact_local_state_proof_does_not_wait_for_shared_snapshot_publication(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    coordinator, service, session_dir, _staging_root, _state_root = _staged_coordinator(monkeypatch, tmp_path)
    (session_dir / "session.jsonl").write_text("v1\n", encoding="utf-8")
    snapshot = service.backup_session("/repo", "s1", reason=BackupReason.TERMINAL, critical=True)
    assert snapshot.destination is not None and snapshot.commit_id is not None
    state = service._read_state(snapshot.destination, session_id="s1", shared=True)
    assert state is not None
    backup_root = service._backup_root()
    assert backup_root is not None
    project = session_dir.parent.name
    shared = backup_root / "projects" / project / "s1"
    shared.mkdir(parents=True)
    job = SessionBackupJob(
        job_id="job-1",
        project=project,
        session_id="s1",
        cwd="/repo",
        context_id="ctx-1",
        execution_id="exec-1",
        boundary="natural_completion",
        reason=BackupReason.TERMINAL.value,
        business_revision=1,
        fence=1,
        capture_commit_id=snapshot.commit_id,
    )
    try:
        with service._shared_session_lock(backup_root, project=project, session_id="s1"):
            shutil.rmtree(snapshot.destination)
            lookup = asyncio.create_task(asyncio.to_thread(coordinator._find_committed_capture, job))
            captured = await asyncio.wait_for(lookup, 0.5)
            assert captured is not None and captured.commit_id == snapshot.commit_id
            service._write_state(shared, state)
        captured = await asyncio.wait_for(lookup, 5)
        assert captured is not None
        assert captured.commit_id == snapshot.commit_id
        assert captured.staged_committed is True
    finally:
        await coordinator.aclose()


@pytest.mark.asyncio
async def test_register_boundary_returns_only_after_local_snapshot_is_committed(
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
        registration = asyncio.create_task(_register(coordinator))
        assert await asyncio.to_thread(started.wait, 5)
        assert registration.done() is False

        pending_root = staging_root / PENDING_JOBS_DIRNAME
        await _wait_for(lambda: pending_root.is_dir() and any(pending_root.iterdir()))
        marker = next(pending_root.iterdir())
        document = json.loads(marker.read_text(encoding="utf-8"))
        assert document["sessionId"] == "s1"
        assert document["boundary"] == "natural_completion"
        assert document["businessRevision"] == 1
        assert document["fence"] == 1

        release.set()
        handoff = await asyncio.wait_for(registration, 5)
        assert handoff.job_id is not None
        assert handoff.backup_disabled is False
        assert handoff.business_revision == 1
        assert handoff.staged_committed is True
        assert handoff.snapshot_generation == 1
        assert handoff.snapshot_commit_id is not None
        snapshot = staging_root / "projects" / session_dir.parent.name / "s1_v1"
        assert snapshot.is_dir()
        assert (snapshot / "session.jsonl").read_text(encoding="utf-8") == "v1\n"
        assert not marker.exists()
        assert not (staging_root / PENDING_JOBS_DIRNAME).exists()
        receipt = json.loads(
            (state_root / "session-backup-coordinator" / session_dir.parent.name / "s1" / "receipts")
            .joinpath("{}.json".format(handoff.job_id))
            .read_text(encoding="utf-8")
        )
        assert receipt["coveredThroughBusinessRevision"] == 1
        assert receipt["backupGeneration"] == 1
        assert handoff.snapshot_commit_id == receipt["commitId"]
    finally:
        release.set()
        await coordinator.aclose()


@pytest.mark.asyncio
async def test_failed_capture_keeps_its_marker_without_an_exact_ack(
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
        with pytest.raises(SessionBackupHandoffError, match="could not be committed locally"):
            await _register(coordinator)
        first_marker = next((staging_root / PENDING_JOBS_DIRNAME).glob("*.json"))
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
        # A newer revision cannot ACK the failed old capture.
        assert first_marker.exists()
        assert (staging_root / PENDING_JOBS_DIRNAME).exists()
        receipt = json.loads(
            (state_root / "session-backup-coordinator" / session_dir.parent.name / "s1" / "receipts")
            .joinpath("{}.json".format(second.job_id))
            .read_text(encoding="utf-8")
        )
        assert receipt["coveredThroughBusinessRevision"] == 2
    finally:
        await coordinator.aclose()


@pytest.mark.asyncio
async def test_failed_boundary_does_not_retry_against_a_later_live_session(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    coordinator, service, session_dir, _staging_root, _state_root = _staged_coordinator(monkeypatch, tmp_path)
    (session_dir / "session.jsonl").write_text("boundary-v1\n", encoding="utf-8")
    calls = 0

    def fail_capture(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        raise OSError("injected local capture failure")

    monkeypatch.setattr(service, "backup_session", fail_capture)
    try:
        with pytest.raises(SessionBackupHandoffError, match="could not be committed locally"):
            await _register(coordinator)
        (session_dir / "session.jsonl").write_text("later-turn\n", encoding="utf-8")
        await asyncio.sleep(0.05)
        assert calls == 1
    finally:
        await coordinator.aclose()


@pytest.mark.asyncio
async def test_recover_does_not_recapture_a_failed_boundary_from_changed_live_state(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    coordinator, service, session_dir, staging_root, state_root = _staged_coordinator(monkeypatch, tmp_path)
    (session_dir / "session.jsonl").write_text("sealed-boundary\n", encoding="utf-8")

    def fail_capture(*_args, **_kwargs):
        raise OSError("injected capture failure")

    monkeypatch.setattr(service, "backup_session", fail_capture)
    with pytest.raises(SessionBackupHandoffError, match="could not be committed locally"):
        await _register(coordinator)
    marker = next((staging_root / PENDING_JOBS_DIRNAME).glob("*.json"))
    document = json.loads(marker.read_text(encoding="utf-8"))
    assert document["captureStarted"] is True
    assert document["attempts"] == 1
    await coordinator.aclose()
    (session_dir / "session.jsonl").write_text("later-live-content\n", encoding="utf-8")

    calls = 0

    def reject_recapture(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        raise AssertionError("failed boundary must not reread the changed live session")

    monkeypatch.setattr(service, "backup_session", reject_recapture)
    successor = SessionBackupCoordinator(service, state_root=state_root, retry_delays=())
    try:
        with pytest.raises(SessionBackupHandoffError, match="no matching durable lineage proof"):
            await successor.recover()
        assert calls == 0
        assert marker.exists()
    finally:
        await successor.aclose()


@pytest.mark.asyncio
async def test_recover_uses_published_capture_identity_after_stage_record_crash(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    coordinator, service, session_dir, staging_root, state_root = _staged_coordinator(monkeypatch, tmp_path)
    backup_root = tmp_path / "backup"
    (session_dir / "session.jsonl").write_text("sealed-boundary\n", encoding="utf-8")
    original_write = coordinator._write_pending_job

    def crash_before_stage_record(job) -> None:
        if job.staged_generation is not None:
            raise OSError("injected crash before durable stage record")
        original_write(job)

    monkeypatch.setattr(coordinator, "_write_pending_job", crash_before_stage_record)
    try:
        with pytest.raises(SessionBackupHandoffError, match="staged callback"):
            await _register(coordinator)
    finally:
        await coordinator.aclose()

    marker = next((staging_root / PENDING_JOBS_DIRNAME).glob("*.json"))
    pending = json.loads(marker.read_text(encoding="utf-8"))
    capture_commit_id = pending["captureCommitId"]
    assert capture_commit_id

    worker = SessionBackupStagingWorker(staging_root, backup_root)
    assert worker.run_once() == 1
    assert not list((staging_root / "projects").glob("**/s1_v*"))
    shared_state = json.loads(
        (backup_root / "projects" / session_dir.parent.name / "s1" / ".backup-state.json").read_text(encoding="utf-8")
    )
    assert shared_state["commit_id"] == capture_commit_id

    (session_dir / "session.jsonl").write_text("later-live-content\n", encoding="utf-8")
    calls = 0

    def reject_recapture(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        raise AssertionError("sealed boundary must not be recaptured from live session")

    monkeypatch.setattr(service, "backup_session", reject_recapture)
    successor = SessionBackupCoordinator(service, state_root=state_root, retry_delays=())
    try:
        assert await successor.recover() == 1
        assert calls == 0
        assert not marker.exists()
    finally:
        await successor.aclose()


@pytest.mark.asyncio
async def test_recover_replays_a_durable_staged_callback_without_recapture(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    coordinator, service, session_dir, staging_root, state_root = _staged_coordinator(monkeypatch, tmp_path)
    (session_dir / "session.jsonl").write_text("sealed-boundary\n", encoding="utf-8")
    callback_calls: list[tuple[int | None, str | None]] = []

    def fail_once(generation: int | None, commit_id: str | None) -> None:
        callback_calls.append((generation, commit_id))
        if len(callback_calls) == 1:
            raise OSError("injected callback crash")

    with pytest.raises(SessionBackupHandoffError, match="staged callback"):
        await coordinator.register_boundary(
            cwd="/repo",
            session_id="s1",
            context_id="ctx-1",
            execution_id="exec-1",
            boundary="permission_publication",
            reason=BackupReason.INPUT_REQUIRED,
            on_staged=fail_once,
            staged_action={"kind": "test_callback_v1", "boundaryId": "boundary-1"},
        )
    marker = next((staging_root / PENDING_JOBS_DIRNAME).glob("*.json"))
    document = json.loads(marker.read_text(encoding="utf-8"))
    assert document["stagedGeneration"] == 1
    assert document["stagedCommitId"]
    assert document["callbackCompleted"] is False

    calls = 0
    original = service.backup_session

    def recording_backup(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(service, "backup_session", recording_backup)
    try:
        assert await coordinator.recover() == 1
        assert calls == 0
        assert len(callback_calls) == 2
        assert not marker.exists()
    finally:
        await coordinator.aclose()


@pytest.mark.asyncio
async def test_restart_resolves_a_typed_staged_callback_without_recapture(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    coordinator, service, session_dir, staging_root, state_root = _staged_coordinator(monkeypatch, tmp_path)
    (session_dir / "session.jsonl").write_text("sealed-boundary\n", encoding="utf-8")

    def crash_callback(_generation: int | None, _commit_id: str | None) -> None:
        raise OSError("injected process exit before callback")

    with pytest.raises(SessionBackupHandoffError, match="staged callback"):
        await coordinator.register_boundary(
            cwd="/repo",
            session_id="s1",
            context_id="ctx-1",
            execution_id="exec-1",
            boundary="permission_publication",
            reason=BackupReason.INPUT_REQUIRED,
            on_staged=crash_callback,
            staged_action={"kind": "test_callback_v1", "boundaryId": "boundary-1"},
        )
    marker = next((staging_root / PENDING_JOBS_DIRNAME).glob("*.json"))
    await coordinator.aclose()

    calls = 0

    def reject_recapture(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        raise AssertionError("durable staged callback must not recapture live session")

    monkeypatch.setattr(service, "backup_session", reject_recapture)
    resolved: list[tuple[dict[str, object], int, str]] = []

    async def resolve(action, generation: int, commit_id: str) -> None:
        resolved.append((dict(action), generation, commit_id))

    successor = SessionBackupCoordinator(
        service,
        state_root=state_root,
        retry_delays=(),
        staged_action_resolver=resolve,
    )
    try:
        assert await successor.recover() == 1
        assert calls == 0
        assert resolved == [
            (
                {"kind": "test_callback_v1", "boundaryId": "boundary-1"},
                1,
                resolved[0][2],
            )
        ]
        assert resolved[0][2]
        assert not marker.exists()
    finally:
        await successor.aclose()


@pytest.mark.asyncio
async def test_restart_fails_closed_when_a_staged_callback_has_no_recovery_identity(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    coordinator, service, session_dir, staging_root, state_root = _staged_coordinator(monkeypatch, tmp_path)
    (session_dir / "session.jsonl").write_text("sealed-boundary\n", encoding="utf-8")

    def crash_callback(_generation: int | None, _commit_id: str | None) -> None:
        raise OSError("injected process exit before callback")

    with pytest.raises(SessionBackupHandoffError, match="staged callback"):
        await coordinator.register_boundary(
            cwd="/repo",
            session_id="s1",
            context_id="ctx-1",
            execution_id="exec-1",
            boundary="permission_publication",
            reason=BackupReason.INPUT_REQUIRED,
            on_staged=crash_callback,
        )
    marker = next((staging_root / PENDING_JOBS_DIRNAME).glob("*.json"))
    await coordinator.aclose()

    calls = 0

    def reject_recapture(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        raise AssertionError("unrecoverable callback must not recapture live session")

    monkeypatch.setattr(service, "backup_session", reject_recapture)
    successor = SessionBackupCoordinator(service, state_root=state_root, retry_delays=())
    try:
        with pytest.raises(SessionBackupHandoffError, match="recovery identity"):
            await successor.recover()
        assert calls == 0
        assert marker.exists()
    finally:
        await successor.aclose()


def test_staged_backup_uses_a_preallocated_capture_commit_id(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    coordinator, service, session_dir, _staging_root, _state_root = _staged_coordinator(monkeypatch, tmp_path)
    (session_dir / "session.jsonl").write_text("boundary\n", encoding="utf-8")
    try:
        result = service.backup_session(
            "/repo",
            "s1",
            reason=BackupReason.TERMINAL,
            critical=False,
            operation_commit_id="capture-identity-1",
        )
        assert result.commit_id == "capture-identity-1"
    finally:
        asyncio.run(coordinator.aclose())


@pytest.mark.asyncio
async def test_permission_staged_action_resolver_is_generation_fenced_and_does_not_revive_consumed_boundary(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _coordinator, _service, _session_dir, _staging_root, _state_root = _staged_coordinator(monkeypatch, tmp_path)
    store = PermissionWaitCheckpointStore("/repo", "s1")
    record = store.create(
        build_permission_checkpoint(
            session_id="s1",
            task_id="task-1",
            context_id="ctx-1",
            input_id="input-1",
            tool_use_id="tool-1",
            tool_name="aliyun_api",
            tool_input={"action": "CreateStack"},
            permission_class="normal",
            continuation_frame={
                "assistantMessageRef": "session.jsonl:0",
                "assistantMessageDigest": "a" * 64,
                "orderedToolUseIds": ["tool-1"],
                "currentIndex": 0,
                "decisions": [{"toolUseId": "tool-1", "state": "pending", "source": None, "deniedResult": None}],
            },
            policy=PermissionWaitPolicy(),
        )
    )
    action = {
        "kind": "permission_generation_v1",
        "cwd": "/repo",
        "sessionId": "s1",
        "boundaryId": record["boundaryId"],
        "checkpointGeneration": record["generation"],
        "taskId": "task-1",
        "contextId": "ctx-1",
    }
    recorded: list[tuple[str, int]] = []

    async def record_generation(task_id: str, generation: int) -> None:
        recorded.append((task_id, generation))

    registry = PermissionInputRegistry()
    registry.set_staged_task_generation_recorder(record_generation)
    await registry.resolve_staged_backup_action(action, 7, "commit-7")
    assert recorded == [("task-1", 7)]

    store.cancel(record["boundaryId"], expected_generation=record["generation"])
    with pytest.raises(ValueError, match="no longer active"):
        await registry.resolve_staged_backup_action(action, 7, "commit-7")
    assert recorded == [("task-1", 7)]


@pytest.mark.asyncio
async def test_cold_start_recovers_permission_stage_into_persisted_task_without_live_recapture(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    coordinator, service, session_dir, staging_root, state_root = _staged_coordinator(monkeypatch, tmp_path)
    (session_dir / "session.jsonl").write_text("sealed-permission\n", encoding="utf-8")
    checkpoint_store = PermissionWaitCheckpointStore("/repo", "s1")
    checkpoint = checkpoint_store.create(
        build_permission_checkpoint(
            session_id="s1",
            task_id="task-1",
            context_id="ctx-1",
            input_id="input-1",
            tool_use_id="tool-1",
            tool_name="aliyun_api",
            tool_input={"action": "CreateStack"},
            permission_class="normal",
            continuation_frame={
                "assistantMessageRef": "session.jsonl:0",
                "assistantMessageDigest": "a" * 64,
                "orderedToolUseIds": ["tool-1"],
                "currentIndex": 0,
                "decisions": [{"toolUseId": "tool-1", "state": "pending", "source": None, "deniedResult": None}],
            },
            policy=PermissionWaitPolicy(),
        )
    )
    action = {
        "kind": "permission_generation_v1",
        "cwd": "/repo",
        "sessionId": "s1",
        "boundaryId": checkpoint["boundaryId"],
        "checkpointGeneration": checkpoint["generation"],
        "taskId": "task-1",
        "contextId": "ctx-1",
    }
    persistence = A2APersistenceStore(state_root)
    persistence.save_task(
        A2ATaskSnapshot(
            task_id="task-1",
            context_id="ctx-1",
            state="input-required",
        )
    )

    def crash_callback(_generation: int | None, _commit_id: str | None) -> None:
        raise OSError("injected process exit before permission callback")

    with pytest.raises(SessionBackupHandoffError, match="staged callback"):
        await coordinator.register_boundary(
            cwd="/repo",
            session_id="s1",
            context_id="ctx-1",
            execution_id="task-1",
            boundary="permission_publication",
            reason=BackupReason.INPUT_REQUIRED,
            on_staged=crash_callback,
            staged_action=action,
        )
    marker = next((staging_root / PENDING_JOBS_DIRNAME).glob("*.json"))
    await coordinator.aclose()

    calls = 0

    def reject_recapture(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        raise AssertionError("cold staged recovery must not recapture live permission state")

    monkeypatch.setattr(service, "backup_session", reject_recapture)
    cold_task_store = A2ATaskStore(persistence=persistence, backup_service=service)
    cold_registry = PermissionInputRegistry()
    cold_registry.set_staged_task_generation_recorder(cold_task_store.recover_expected_permission_backup_generation)
    successor = SessionBackupCoordinator(
        service,
        state_root=state_root,
        retry_delays=(),
        staged_action_resolver=cold_registry.resolve_staged_backup_action,
    )
    try:
        assert await successor.recover() == 1
        assert calls == 0
        assert not marker.exists()
        restored = persistence.load_task("task-1")
        assert restored is not None
        assert restored.expected_permission_backup_generation == 1
    finally:
        await successor.aclose()


@pytest.mark.asyncio
async def test_recover_rejects_a_corrupt_durable_pending_job(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    coordinator, _service, _session_dir, staging_root, _state_root = _staged_coordinator(monkeypatch, tmp_path)
    pending_root = staging_root / PENDING_JOBS_DIRNAME
    pending_root.mkdir(parents=True)
    (pending_root / "job-corrupt.json").write_text("{not-json", encoding="utf-8")

    try:
        with pytest.raises(SessionBackupHandoffError, match="pending jobs could not be loaded"):
            await coordinator.recover()
    finally:
        await coordinator.aclose()


@pytest.mark.asyncio
async def test_recover_rejects_a_pending_directory_enumeration_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    coordinator, _service, _session_dir, staging_root, _state_root = _staged_coordinator(monkeypatch, tmp_path)
    pending_root = staging_root / PENDING_JOBS_DIRNAME
    pending_root.mkdir(parents=True)
    original_glob = Path.glob

    def fail_pending_enumeration(path: Path, pattern: str):
        if path == pending_root and pattern == "*.json":
            raise OSError("injected pending directory enumeration failure")
        return original_glob(path, pattern)

    monkeypatch.setattr(Path, "glob", fail_pending_enumeration)
    try:
        with pytest.raises(SessionBackupHandoffError, match="pending jobs could not be loaded"):
            await coordinator.recover()
    finally:
        await coordinator.aclose()


@pytest.mark.asyncio
async def test_recover_does_not_recapture_a_job_with_a_durable_local_receipt(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    coordinator, service, session_dir, staging_root, state_root = _staged_coordinator(monkeypatch, tmp_path)
    (session_dir / "session.jsonl").write_text("boundary-v1\n", encoding="utf-8")
    monkeypatch.setattr(coordinator, "_remove_pending_marker", lambda _path: None)
    handoff = await _register(coordinator)
    marker = staging_root / PENDING_JOBS_DIRNAME / "{}.json".format(handoff.job_id)
    assert marker.is_file()
    await coordinator.aclose()

    calls = 0
    original = service.backup_session

    def recording_backup(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(service, "backup_session", recording_backup)
    successor = SessionBackupCoordinator(service, state_root=state_root, retry_delays=())
    try:
        assert await successor.recover() == 0
        assert calls == 0
        assert marker.exists() is False
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
async def test_register_boundary_rejects_a_stale_staged_callback_and_keeps_its_job(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    coordinator, _service, session_dir, staging_root, _state_root = _staged_coordinator(monkeypatch, tmp_path)
    (session_dir / "session.jsonl").write_text("v1\n", encoding="utf-8")

    def stale_callback(_generation: int | None, _commit_id: str | None) -> None:
        raise ValueError("permission generation changed")

    try:
        with pytest.raises(SessionBackupHandoffError, match="staged callback"):
            await coordinator.register_boundary(
                cwd="/repo",
                session_id="s1",
                context_id="ctx-1",
                execution_id="exec-1",
                boundary="permission_publication",
                reason=BackupReason.INPUT_REQUIRED,
                on_staged=stale_callback,
            )
        assert len(list((staging_root / PENDING_JOBS_DIRNAME).glob("*.json"))) == 1
    finally:
        await coordinator.aclose()


@pytest.mark.asyncio
async def test_register_boundary_rejects_an_unpersisted_local_receipt(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    coordinator, _service, session_dir, staging_root, _state_root = _staged_coordinator(monkeypatch, tmp_path)
    (session_dir / "session.jsonl").write_text("v1\n", encoding="utf-8")
    monkeypatch.setattr(
        coordinator,
        "_commit_receipt_and_clear",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("receipt fsync failed")),
    )
    try:
        with pytest.raises(SessionBackupHandoffError, match="could not be committed"):
            await _register(coordinator)
        assert len(list((staging_root / PENDING_JOBS_DIRNAME).glob("*.json"))) == 1
    finally:
        await coordinator.aclose()


@pytest.mark.asyncio
async def test_wait_for_local_snapshot_quiescence_waits_for_an_inflight_capture(
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
        registration = asyncio.create_task(_register(coordinator))
        # The local capture is now stuck inside backup_session and will not finish.
        assert await asyncio.to_thread(started.wait, 5)
        quiescence = asyncio.create_task(coordinator.wait_for_local_snapshot_quiescence(cwd="/repo", session_id="s1"))
        await asyncio.sleep(0.05)
        assert quiescence.done() is False
        release.set()
        await asyncio.wait_for(registration, 5)
        await asyncio.wait_for(quiescence, 5)
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


@pytest.mark.asyncio
async def test_fresh_capture_finishes_while_old_publisher_holds_shared_lock(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    coordinator, service, session_dir, staging_root, state_root = _staged_coordinator(monkeypatch, tmp_path)
    (session_dir / "session.jsonl").write_text("old\n", encoding="utf-8")
    await _register(coordinator)
    worker = SessionBackupStagingWorker(staging_root, tmp_path / "backup")
    entered = threading.Event()
    release = threading.Event()
    from iac_code.services.session_backup import SessionBackupService

    original = SessionBackupService._mirror

    def blocked_mirror(self, source, destination, *args, **kwargs):
        if Path(destination).is_relative_to(tmp_path / "backup"):
            entered.set()
            assert release.wait(5)
        return original(self, source, destination, *args, **kwargs)

    monkeypatch.setattr(SessionBackupService, "_mirror", blocked_mirror)
    snapshot = worker.scan_snapshots()[0]
    publishing = asyncio.create_task(asyncio.to_thread(worker.publish_snapshot, snapshot))
    try:
        assert await asyncio.to_thread(entered.wait, 3)
        (session_dir / "session.jsonl").write_text("new\n", encoding="utf-8")
        capture = asyncio.create_task(_register(coordinator, execution_id="exec-2"))
        done, _ = await asyncio.wait({capture}, timeout=0.5)
        assert capture in done, "fresh local capture waited for shared publication"
        assert capture.result().staged_committed
        assert not publishing.done()
    finally:
        release.set()
        await publishing
        await coordinator.aclose()


def test_pending_updates_preserve_staged_ack_and_do_not_revive_completed_job(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from dataclasses import replace

    coordinator, _service, _session_dir, staging_root, _state_root = _staged_coordinator(monkeypatch, tmp_path)
    job = coordinator._persist_pending_job("/repo", "s1", "ctx", "exec", "terminal", "terminal", None, {}, False, None)
    assert job is not None
    staged = replace(job, capture_started=True, staged_generation=1, staged_commit_id=job.capture_commit_id)
    coordinator._write_pending_job(staged)
    coordinator._write_pending_job(replace(job, capture_started=True))
    marker = staging_root / PENDING_JOBS_DIRNAME / f"{job.job_id}.json"
    assert json.loads(marker.read_text(encoding="utf-8"))["stagedGeneration"] == 1
    coordinator._commit_receipt_and_clear(staged, 1, staged.staged_commit_id)
    coordinator._write_pending_job(job)
    assert not marker.exists()


def test_new_receipt_keeps_older_job_without_exact_staged_ack(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from dataclasses import replace

    coordinator, _service, _session_dir, staging_root, _state_root = _staged_coordinator(monkeypatch, tmp_path)
    old = coordinator._persist_pending_job("/repo", "s1", "ctx", "exec", "terminal", "terminal", None, {}, False, None)
    new = coordinator._persist_pending_job("/repo", "s1", "ctx", "exec2", "terminal", "terminal", None, {}, False, None)
    assert old is not None and new is not None
    staged = replace(new, capture_started=True, staged_generation=2, staged_commit_id=new.capture_commit_id)
    coordinator._write_pending_job(staged)
    coordinator._commit_receipt_and_clear(staged, 2, staged.staged_commit_id)
    assert (staging_root / PENDING_JOBS_DIRNAME / f"{old.job_id}.json").exists()


@pytest.mark.asyncio
async def test_publisher_retains_snapshot_until_crashed_capture_has_exact_ack(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    coordinator, service, session_dir, staging_root, state_root = _staged_coordinator(monkeypatch, tmp_path)
    (session_dir / "session.jsonl").write_text("sealed\n", encoding="utf-8")
    original_write = coordinator._write_pending_job

    def crash_before_ack(job):
        if job.staged_generation is not None:
            raise OSError("crashed before staged ACK")
        return original_write(job)

    monkeypatch.setattr(coordinator, "_write_pending_job", crash_before_ack)
    with pytest.raises(SessionBackupHandoffError):
        await _register(coordinator)
    await coordinator.aclose()
    worker = SessionBackupStagingWorker(staging_root, tmp_path / "backup")
    worker.coordinator_state_root = state_root
    assert worker.run_once() == 1
    snapshot = staging_root / "projects" / session_dir.parent.name / "s1_v1"
    assert snapshot.exists(), "publisher deleted the only immutable recovery proof before staged ACK"
    (session_dir / "session.jsonl").write_text("later\n", encoding="utf-8")
    successor = SessionBackupCoordinator(service, state_root=state_root)
    monkeypatch.setattr(service, "backup_session", lambda *_a, **_k: pytest.fail("recaptured old live data"))
    try:
        with service._shared_session_lock(tmp_path / "backup", project=session_dir.parent.name, session_id="s1"):
            recovery = asyncio.create_task(successor.recover())
            done, _ = await asyncio.wait({recovery}, timeout=0.5)
            assert recovery in done, "exact immutable proof waited for shared lock"
            assert recovery.result() == 1
        assert worker.run_once() == 1
        assert not snapshot.exists()
    finally:
        await successor.aclose()


class BackupProcessActions:
    @staticmethod
    def recover(staging_root, state_root, config_root, ready, start, output):
        service = StagedSessionBackupService(staging_root, SessionStorage(projects_dir=Path(config_root) / "projects"))
        coordinator = SessionBackupCoordinator(service, state_root=Path(state_root))
        ready.put(True)
        if not start.wait(10):
            raise RuntimeError("recovery start barrier timed out")
        try:
            output.put(asyncio.run(coordinator.recover()))
        except Exception as exc:
            output.put(repr(exc))

    @staticmethod
    def publish(staging_root, backup_root, state_root, snapshot, ready, start, output):
        worker = SessionBackupStagingWorker(staging_root, backup_root, coordinator_state_root=state_root)
        ready.put(True)
        if not start.wait(10):
            raise RuntimeError("publisher start barrier timed out")
        try:
            worker.publish_snapshot(snapshot)
            output.put("published")
        except Exception as exc:
            output.put(repr(exc))


@pytest.mark.asyncio
async def test_two_process_recovery_and_publishers_keep_exact_snapshot_identity(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import multiprocessing

    coordinator, service, session_dir, staging_root, state_root = _staged_coordinator(monkeypatch, tmp_path)
    (session_dir / "session.jsonl").write_text("sealed\n", encoding="utf-8")
    original = coordinator._write_pending_job

    def crash_stage(job):
        if job.staged_generation is not None:
            raise OSError("crash before ACK")
        original(job)

    monkeypatch.setattr(coordinator, "_write_pending_job", crash_stage)
    with pytest.raises(SessionBackupHandoffError):
        await _register(coordinator)
    await coordinator.aclose()
    (session_dir / "session.jsonl").write_text("later\n", encoding="utf-8")
    # Erase the mutable fast proof; only the immutable snapshot can recover.
    local_state = service._read_state(session_dir, session_id="s1")
    assert local_state is not None
    service._write_state(
        session_dir,
        local_state.committed_next(commit_id="later-commit", reason="terminal", writer_id="later", proofs={}),
    )
    context = multiprocessing.get_context("spawn")
    for action in ("recover", "publish"):
        ready, output = context.Queue(), context.Queue()
        start = context.Event()
        worker = SessionBackupStagingWorker(staging_root, tmp_path / "backup", coordinator_state_root=state_root)
        snapshots = worker.scan_snapshots()
        assert len(snapshots) == 1
        if action == "recover":
            target = BackupProcessActions.recover
            arguments = (str(staging_root), str(state_root), str(tmp_path / "config"), ready, start, output)
        else:
            target = BackupProcessActions.publish
            arguments = (
                str(staging_root),
                str(tmp_path / "backup"),
                str(state_root),
                snapshots[0],
                ready,
                start,
                output,
            )
        processes = [context.Process(target=target, args=arguments) for _ in range(2)]
        try:
            for process in processes:
                process.start()
            for _ in processes:
                assert await asyncio.to_thread(ready.get, True, 15) is True
            start.set()
            results = [await asyncio.to_thread(output.get, True, 15) for _ in processes]
            if action == "recover":
                assert all(isinstance(value, int) for value in results), results
                assert sum(results) >= 1
                assert (snapshots[0].path / "session.jsonl").read_text(encoding="utf-8") == "sealed\n"
            else:
                assert results == ["published", "published"]
                assert not snapshots[0].path.exists()
                destination = tmp_path / "backup" / "projects" / session_dir.parent.name / "s1"
                assert (destination / "session.jsonl").read_text(encoding="utf-8") == "sealed\n"
        finally:
            start.set()
            for process in processes:
                await asyncio.to_thread(process.join, 10)
                if process.is_alive():
                    process.terminate()
                    await asyncio.to_thread(process.join, 5)
                assert process.exitcode == 0
            ready.close()
            output.close()


@pytest.mark.asyncio
async def test_permission_resolving_stop_drains_writer_without_waiting_for_old_shared_copy(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from iac_code.a2a.execution_control import ExecutionController
    from iac_code.a2a.input_required import PermissionResponse
    from iac_code.services.permission_wait import PermissionWaitCoordinator
    from iac_code.services.session_backup import SessionBackupService
    from iac_code.types.stream_events import PermissionRequestEvent

    coordinator, service, session_dir, staging_root, state_root = _staged_coordinator(monkeypatch, tmp_path)
    (session_dir / "session.jsonl").write_text("permission\n", encoding="utf-8")
    registry = PermissionInputRegistry()
    registry.set_permission_wait_coordinator(PermissionWaitCoordinator(PermissionWaitPolicy()))
    registry.set_backup_coordinator(coordinator)
    future = asyncio.get_running_loop().create_future()
    request = PermissionRequestEvent(
        tool_name="bash", tool_input={"cmd": "pwd"}, tool_use_id="tool-1", response_future=future
    )
    pending = await registry.register(request, task_id="task-1", context_id="ctx-1", scope="normal")
    store = PermissionWaitCheckpointStore("/repo", "s1")
    record = store.create(
        build_permission_checkpoint(
            session_id="s1",
            task_id="task-1",
            context_id="ctx-1",
            input_id=pending.input_id,
            tool_use_id="tool-1",
            tool_name="bash",
            tool_input=request.tool_input,
            permission_class="normal",
            continuation_frame={
                "assistantMessageRef": "session.jsonl:0",
                "assistantMessageDigest": "a" * 64,
                "orderedToolUseIds": ["tool-1"],
                "currentIndex": 0,
                "decisions": [{"toolUseId": "tool-1", "state": "pending", "source": None, "deniedResult": None}],
            },
            policy=PermissionWaitPolicy(),
        )
    )
    pending.boundary_id = record["boundaryId"]
    pending.checkpoint_store = store
    pending.backup_cwd, pending.backup_session_id, pending.backup_service = "/repo", "s1", service
    registry.activate_durable_boundary(pending, record)
    monkeypatch.setattr("iac_code.a2a.input_required.emit_permission_boundary_audit", lambda *_a, **_k: True)
    await _register(coordinator)
    worker = SessionBackupStagingWorker(staging_root, tmp_path / "backup", coordinator_state_root=state_root)
    copy_started, release_copy = threading.Event(), threading.Event()
    original_mirror = SessionBackupService._mirror

    def mirror(self, source, destination, *args, **kwargs):
        if Path(destination).is_relative_to(tmp_path / "backup"):
            copy_started.set()
            assert release_copy.wait(10)
        return original_mirror(self, source, destination, *args, **kwargs)

    monkeypatch.setattr(SessionBackupService, "_mirror", mirror)
    publishing = asyncio.create_task(asyncio.to_thread(worker.publish_snapshot, worker.scan_snapshots()[0]))
    claimed, allow_capture, cleanup_started = asyncio.Event(), asyncio.Event(), asyncio.Event()
    writer_started, writer_exiting, release_writer = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def before_claim(_pending, _record):
        claimed.set()
        await allow_capture.wait()

    pending.before_claim_backup = before_claim

    async def cleanup(_context, task, _reason):
        cleanup_started.set()
        await registry.cancel_task(task)
        return "canceled"

    control = ExecutionController(
        context_id="ctx-1",
        task_id="task-1",
        owner="owner-1",
        cwd="/repo",
        server_instance_id="instance-1",
        persistence_path=state_root / "control.json",
        backup_service=service,
        backup_coordinator=coordinator,
        termination_cleanup=cleanup,
        execution_id="exec-1",
    )
    control.bind_session("s1")

    async def writer():
        current = asyncio.current_task()
        await control.attach_task(current)
        writer_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            writer_exiting.set()
            await release_writer.wait()
            await control.detach_task(current, execution_status="canceled")

    writing = asyncio.create_task(writer())
    answering = None
    try:
        assert await asyncio.to_thread(copy_started.wait, 3)
        await writer_started.wait()
        answering = asyncio.create_task(
            registry.answer(
                PermissionResponse(
                    task_id="task-1",
                    context_id="ctx-1",
                    request_task_id="task-1",
                    input_id=pending.input_id,
                    tool_use_id="tool-1",
                    decision="deny",
                )
            )
        )
        await asyncio.wait_for(claimed.wait(), 2)
        assert not future.done()
        await control.terminate(execution_id="exec-1", request_id="stop-1", connection_epoch=1, reason="stop")
        await asyncio.wait_for(cleanup_started.wait(), 2)
        assert not control.release_ready
        allow_capture.set()
        done, _ = await asyncio.wait({answering}, timeout=0.5)
        assert answering in done, "permission capture waited for publisher's shared lock"
        assert answering.result() is False
        assert store.load(record["boundaryId"])["decision"]["status"] == "applied"
        await asyncio.wait_for(writer_exiting.wait(), 2)
        assert not control.release_ready, "live writer falsely reported drained"
        release_writer.set()
        await _wait_for(lambda: control.release_ready)
        assert control.snapshot()["backup"]["status"] == "staged_committed"
        assert control.phase == "terminated"
        assert not publishing.done()
    finally:
        allow_capture.set()
        release_writer.set()
        release_copy.set()
        await publishing
        if answering is not None:
            await asyncio.gather(answering, return_exceptions=True)
        await asyncio.gather(writing, return_exceptions=True)
        await control.close()
        await coordinator.aclose()


@pytest.mark.asyncio
async def test_snapshot_rename_crash_keeps_proof_before_local_state_and_job_ack(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    coordinator, service, session_dir, staging_root, state_root = _staged_coordinator(monkeypatch, tmp_path)
    (session_dir / "session.jsonl").write_text("immutable\n", encoding="utf-8")
    original = service._write_state

    def crash_local_state(path, state):
        if path == session_dir and state.generation > 0 and state.status == "succeeded":
            raise OSError("crash after snapshot rename")
        original(path, state)

    monkeypatch.setattr(service, "_write_state", crash_local_state)
    with pytest.raises(SessionBackupHandoffError):
        await _register(coordinator)
    await coordinator.aclose()
    monkeypatch.setattr(service, "_write_state", original)
    snapshot = staging_root / "projects" / session_dir.parent.name / "s1_v1"
    assert snapshot.exists()
    assert service._read_state(session_dir, session_id="s1").status == "failed"
    worker = SessionBackupStagingWorker(staging_root, tmp_path / "backup", coordinator_state_root=state_root)
    assert worker.run_once() == 1
    assert snapshot.exists()
    (session_dir / "session.jsonl").write_text("later-live\n", encoding="utf-8")
    monkeypatch.setattr(service, "backup_session", lambda *_a, **_k: pytest.fail("recaptured old boundary"))
    successor = SessionBackupCoordinator(service, state_root=state_root)
    try:
        assert await successor.recover() == 1
        recovered_state = service._read_state(session_dir, session_id="s1")
        assert recovered_state.status == "succeeded", "recovery left the old failed attempt marker"
        assert worker.run_once() == 1
        assert not snapshot.exists()
        warm = service.reconcile_session("/repo", "s1")
        assert warm.action == "current"
        assert warm.state.commit_id == recovered_state.commit_id
        monkeypatch.setattr(service, "backup_session", StagedSessionBackupService.backup_session.__get__(service))
        fresh = await _register(successor, execution_id="next-exec")
        assert fresh.snapshot_generation == recovered_state.generation + 1
        assert fresh.snapshot_commit_id != recovered_state.commit_id
    finally:
        await successor.aclose()


def test_publisher_keeps_unknown_coordinator_snapshot_without_pending_or_receipt(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _coordinator, service, session_dir, staging_root, state_root = _staged_coordinator(monkeypatch, tmp_path)
    (session_dir / "session.jsonl").write_text("unknown-owner\n", encoding="utf-8")
    result = service.backup_session(
        "/repo",
        "s1",
        reason=BackupReason.TERMINAL,
        critical=True,
        operation_commit_id="00000000-0000-4000-8000-000000000001",
    )
    worker = SessionBackupStagingWorker(staging_root, tmp_path / "backup", coordinator_state_root=state_root)
    assert worker.run_once() == 0
    assert result.destination.exists()


@pytest.mark.asyncio
async def test_historical_missing_local_proof_recovers_shared_only_during_startup(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from dataclasses import replace

    coordinator, service, session_dir, staging_root, state_root = _staged_coordinator(monkeypatch, tmp_path)
    job = coordinator._persist_pending_job("/repo", "s1", "ctx", "exec", "terminal", "terminal", None, {}, False, None)
    assert job is not None
    coordinator._write_pending_job(replace(job, capture_started=True))
    result = service.backup_session(
        "/repo", "s1", reason=BackupReason.TERMINAL, critical=True, operation_commit_id=job.capture_commit_id
    )
    SessionBackupStagingWorker(staging_root, tmp_path / "backup").run_once()
    # Historical publisher deleted snapshot and later local state no longer proves this commit.
    state = service._read_state(session_dir, session_id="s1")
    service._write_state(
        session_dir, state.committed_next(commit_id="later", reason="terminal", writer_id="later", proofs={})
    )
    assert coordinator._find_committed_capture(job) is None
    monkeypatch.setattr(service, "backup_session", lambda *_a, **_k: pytest.fail("must not recapture old live content"))
    successor = SessionBackupCoordinator(service, state_root=state_root)
    try:
        assert await successor.recover() == 1
        receipt = successor._read_receipt_locked(job)
        assert receipt["commitId"] == result.commit_id
    finally:
        await successor.aclose()


@pytest.mark.asyncio
async def test_disabled_capture_with_callback_completes_without_running_staged_callback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    coordinator, _service, _session_dir, staging_root, _state_root = _staged_coordinator(monkeypatch, tmp_path)
    monkeypatch.delenv("IAC_CODE_CONFIG_BACKUP_DIR")
    called = []
    try:
        result = await coordinator.register_boundary(
            cwd="/repo",
            session_id="s1",
            context_id="ctx",
            execution_id="exec",
            boundary="permission",
            reason=BackupReason.INPUT_REQUIRED,
            on_staged=lambda *_a: called.append(True),
        )
        assert result.backup_disabled
        assert not called
        assert not list((staging_root / PENDING_JOBS_DIRNAME).glob("*.json"))
    finally:
        await coordinator.aclose()


@pytest.mark.asyncio
async def test_historical_shared_recovery_does_not_hold_fresh_capture_ownership(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from dataclasses import replace

    coordinator, service, session_dir, staging_root, state_root = _staged_coordinator(monkeypatch, tmp_path)
    job = coordinator._persist_pending_job("/repo", "s1", "ctx", "exec", "terminal", "terminal", None, {}, False, None)
    assert job is not None
    coordinator._write_pending_job(replace(job, capture_started=True))
    service.backup_session(
        "/repo", "s1", reason=BackupReason.TERMINAL, critical=True, operation_commit_id=job.capture_commit_id
    )
    SessionBackupStagingWorker(staging_root, tmp_path / "backup").run_once()
    state = service._read_state(session_dir, session_id="s1")
    service._write_state(
        session_dir, state.committed_next(commit_id="later", reason="terminal", writer_id="later", proofs={})
    )
    entering_shared = threading.Event()
    original = coordinator._find_shared_capture

    def lookup(old_job):
        entering_shared.set()
        return original(old_job)

    monkeypatch.setattr(coordinator, "_find_shared_capture", lookup)
    fresh = SessionBackupCoordinator(service, state_root=state_root)
    recovery = None
    capture = None
    try:
        with service._shared_session_lock(tmp_path / "backup", project=session_dir.parent.name, session_id="s1"):
            recovery = asyncio.create_task(coordinator.recover())
            assert await asyncio.to_thread(entering_shared.wait, 2)
            capture = asyncio.create_task(_register(fresh, execution_id="new-exec"))
            done, _ = await asyncio.wait({capture}, timeout=0.5)
            assert capture in done, "historical recovery held capture ownership while waiting for shared"
            assert capture.result().staged_committed
        assert await recovery == 1
    finally:
        if recovery is not None:
            await asyncio.gather(recovery, return_exceptions=True)
        if capture is not None:
            await asyncio.gather(capture, return_exceptions=True)
        await coordinator.aclose()
        await fresh.aclose()


@pytest.mark.asyncio
async def test_ordinary_snapshot_does_not_block_later_coordinator_publication(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    coordinator, service, session_dir, staging_root, state_root = _staged_coordinator(monkeypatch, tmp_path)
    (session_dir / "session.jsonl").write_text("ordinary\n", encoding="utf-8")
    service.backup_session("/repo", "s1", reason=BackupReason.NORMAL_TURN_END, critical=True)
    (session_dir / "session.jsonl").write_text("managed\n", encoding="utf-8")
    handoff = await _register(coordinator)
    worker = SessionBackupStagingWorker(staging_root, tmp_path / "backup", coordinator_state_root=state_root)
    try:
        assert worker.run_once() == 2
        assert not worker.scan_snapshots()
        shared = service._read_state(
            tmp_path / "backup" / "projects" / session_dir.parent.name / "s1", session_id="s1", shared=True
        )
        assert shared.generation == handoff.snapshot_generation
        assert shared.commit_id == handoff.snapshot_commit_id
    finally:
        await coordinator.aclose()


@pytest.mark.asyncio
async def test_four_process_recovery_receipt_and_snapshot_retirement_race(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import multiprocessing

    coordinator, _service, session_dir, staging_root, state_root = _staged_coordinator(monkeypatch, tmp_path)
    (session_dir / "session.jsonl").write_text("fenced-original\n", encoding="utf-8")
    original = coordinator._write_pending_job

    def crash_ack(job):
        if job.staged_generation is not None:
            raise OSError("crash before ACK")
        original(job)

    monkeypatch.setattr(coordinator, "_write_pending_job", crash_ack)
    with pytest.raises(SessionBackupHandoffError):
        await _register(coordinator)
    await coordinator.aclose()
    worker = SessionBackupStagingWorker(staging_root, tmp_path / "backup", coordinator_state_root=state_root)
    snapshot = worker.scan_snapshots()[0]
    snapshot_state = worker._service._read_state(snapshot.path, session_id="s1", shared=True)
    marker = next((staging_root / PENDING_JOBS_DIRNAME).glob("*.json"))
    job = SessionBackupJob.from_document(json.loads(marker.read_text(encoding="utf-8")))
    assert job is not None
    (session_dir / "session.jsonl").write_text("newer-live\n", encoding="utf-8")
    context = multiprocessing.get_context("spawn")
    ready, output = context.Queue(), context.Queue()
    start = context.Event()
    processes = [
        context.Process(
            target=BackupProcessActions.recover,
            args=(str(staging_root), str(state_root), str(tmp_path / "config"), ready, start, output),
        )
        for _ in range(2)
    ]
    processes += [
        context.Process(
            target=BackupProcessActions.publish,
            args=(str(staging_root), str(tmp_path / "backup"), str(state_root), snapshot, ready, start, output),
        )
        for _ in range(2)
    ]
    try:
        for process in processes:
            process.start()
        for _ in processes:
            assert await asyncio.to_thread(ready.get, True, 15)
        start.set()
        results = [await asyncio.to_thread(output.get, True, 15) for _ in processes]
        assert results.count("published") == 2, results
        assert sum(value for value in results if isinstance(value, int)) >= 1, results
        assert all(value == "published" or isinstance(value, int) for value in results), results
        worker.run_once()
        assert not snapshot.path.exists()
        destination = tmp_path / "backup" / "projects" / session_dir.parent.name / "s1"
        assert (destination / "session.jsonl").read_text(encoding="utf-8") == "fenced-original\n"
        assert not list((staging_root / PENDING_JOBS_DIRNAME).glob("*.json"))
        receipt = coordinator._read_receipt_locked(job)
        assert receipt["jobId"] == job.job_id
        assert receipt["backupGeneration"] == snapshot_state.generation
        assert receipt["commitId"] == snapshot_state.commit_id
    finally:
        start.set()
        for process in processes:
            await asyncio.to_thread(process.join, 10)
            if process.is_alive():
                process.terminate()
                await asyncio.to_thread(process.join, 5)
            assert process.exitcode == 0
        ready.close()
        output.close()


@pytest.mark.asyncio
async def test_historical_shared_capture_repairs_same_failed_attempt_without_recapturing_live(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from dataclasses import replace

    coordinator, service, session_dir, staging_root, state_root = _staged_coordinator(monkeypatch, tmp_path)
    base = service._read_state(session_dir, session_id="s1")
    (session_dir / "session.jsonl").write_text("sealed-shared\n", encoding="utf-8")
    job = coordinator._persist_pending_job("/repo", "s1", "ctx", "exec", "terminal", "terminal", None, {}, False, None)
    assert job is not None
    coordinator._write_pending_job(replace(job, capture_started=True))
    captured = service.backup_session(
        "/repo", "s1", reason=BackupReason.TERMINAL, critical=True, operation_commit_id=job.capture_commit_id
    )
    SessionBackupStagingWorker(staging_root, tmp_path / "backup").run_once()
    assert not captured.destination.exists()
    service._write_state(
        session_dir,
        base.failed_attempt(
            reason="terminal",
            writer_id="old-process",
            attempt_commit_id=job.capture_commit_id,
            attempted_proofs={},
            error="crash after rename",
            attempt=1,
            retry_count=0,
            exhausted=True,
        ),
    )
    (session_dir / "session.jsonl").write_text("later-live\n", encoding="utf-8")
    original_backup = service.backup_session
    original_read = service._read_state
    shared = tmp_path / "backup" / "projects" / session_dir.parent.name / "s1"
    shared_reads = []

    def read_state(path, *args, **kwargs):
        if path == shared:
            shared_reads.append(path)
            assert len(shared_reads) == 1, "shared proof was re-read while owning local capture"
        return original_read(path, *args, **kwargs)

    monkeypatch.setattr(service, "_read_state", read_state)
    monkeypatch.setattr(service, "backup_session", lambda *_a, **_k: pytest.fail("recaptured later live as old commit"))
    successor = SessionBackupCoordinator(service, state_root=state_root)
    try:
        assert await successor.recover() == 1
        local = service._read_state(session_dir, session_id="s1")
        assert local.status == "succeeded", "historical shared recovery left old failed attempt"
        assert local.commit_id == captured.commit_id
        warm = service.reconcile_session("/repo", "s1")
        assert warm.action == "current"
        assert warm.state.commit_id == captured.commit_id
        assert len(shared_reads) == 1
        monkeypatch.setattr(service, "backup_session", original_backup)
        following = await _register(successor, execution_id="following-exec")
        assert following.snapshot_generation == captured.generation + 1
        assert following.snapshot_commit_id != captured.commit_id
        assert (shared / "session.jsonl").read_text(encoding="utf-8") == "sealed-shared\n"
    finally:
        await successor.aclose()


@pytest.mark.parametrize("later_state", ["succeeded", "different_failed_attempt"])
def test_shared_metadata_adoption_preserves_later_local_owner(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, later_state: str
) -> None:
    _coordinator, service, session_dir, _staging_root, _state_root = _staged_coordinator(monkeypatch, tmp_path)
    base = service._read_state(session_dir, session_id="s1")
    captured = service.backup_session(
        "/repo", "s1", reason=BackupReason.TERMINAL, critical=True, operation_commit_id="old-commit"
    )
    marker = service._read_state(captured.destination, session_id="s1", shared=True)
    if later_state == "succeeded":
        expected = marker.committed_next(commit_id="later-commit", reason="terminal", writer_id="later", proofs={})
    else:
        expected = base.failed_attempt(
            reason="terminal",
            writer_id="later",
            attempt_commit_id="different-commit",
            attempted_proofs={},
            error="different boundary failed",
            attempt=1,
            retry_count=0,
            exhausted=True,
        )
    service._write_state(session_dir, expected)
    service.adopt_coordinator_capture("/repo", "s1", captured, committed_state=marker)
    assert service._read_state(session_dir, session_id="s1") == expected


class _NaturalCompletionProducerBarriers:
    """Pause real capture and control commit without manufacturing their proofs."""

    def __init__(self, service, control) -> None:
        self.capture_started = threading.Event()
        self.release_capture = threading.Event()
        self.commit_started = asyncio.Event()
        self.release_commit = asyncio.Event()
        self._capture = service.backup_session
        self._persist = control._persist_snapshot

    def capture(self, *args, **kwargs):
        self.capture_started.set()
        assert self.release_capture.wait(10)
        return self._capture(*args, **kwargs)

    async def persist(self, snapshot):
        if snapshot.get("naturalHandoff") is not None:
            self.commit_started.set()
            await asyncio.wait_for(self.release_commit.wait(), 10)
        await self._persist(snapshot)

    def release(self) -> None:
        self.release_capture.set()
        self.release_commit.set()


@pytest.mark.asyncio
async def test_real_natural_completion_pending_shapes_become_ready_before_shared_publication(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from iac_code.a2a.execution_control import ExecutionController
    from iac_code.services.session_backup import SessionBackupService

    coordinator, service, session_dir, staging_root, state_root = _staged_coordinator(monkeypatch, tmp_path)
    (session_dir / "session.jsonl").write_text("natural-boundary\n", encoding="utf-8")
    control = ExecutionController(
        context_id="ctx-1",
        task_id="task-1",
        owner="owner-1",
        cwd="/repo",
        server_instance_id="instance-1",
        persistence_path=state_root / "control.json",
        backup_service=service,
        backup_coordinator=coordinator,
        execution_id="exec-1",
    )
    control.bind_session("s1")
    gates = _NaturalCompletionProducerBarriers(service, control)
    monkeypatch.setattr(service, "backup_session", gates.capture)
    monkeypatch.setattr(control, "_persist_snapshot", gates.persist)
    publication_started, release_publication = threading.Event(), threading.Event()
    original_mirror = SessionBackupService._mirror

    def shared_mirror(instance, source, destination, *args, **kwargs):
        if Path(destination).is_relative_to(tmp_path / "backup"):
            publication_started.set()
            assert release_publication.wait(10)
        return original_mirror(instance, source, destination, *args, **kwargs)

    monkeypatch.setattr(SessionBackupService, "_mirror", shared_mirror)
    current = asyncio.current_task()
    assert current is not None
    await control.attach_task(current)
    generation = await control.detach_task(current, execution_status="completed", natural_completion=True)
    assert generation is not None
    finalization = asyncio.create_task(
        control.finalize_natural_completion(task_id="task-1", completion_generation=generation)
    )
    publishing = None
    try:
        assert await asyncio.to_thread(gates.capture_started.wait, 5)
        pending = control.protocol_snapshot(await control.observe_state())
        assert pending["contextId"] == "ctx-1"
        assert pending["taskId"] == "task-1"
        assert pending["executionId"] == "exec-1"
        assert pending["phase"] == "terminated"
        assert pending["terminationReason"] == "natural_completion"
        assert pending["backup"] == {"status": "pending"}
        assert pending["naturalHandoff"] is None
        assert pending["commitError"] is None
        assert pending["releaseReady"] is False
        assert pending["blockers"] == []
        assert not finalization.done()

        gates.release_capture.set()
        await asyncio.wait_for(gates.commit_started.wait(), 5)
        committing = control.protocol_snapshot(await control.observe_state())
        backup = committing["backup"]
        assert committing["contextId"] == pending["contextId"]
        assert committing["taskId"] == pending["taskId"]
        assert committing["executionId"] == pending["executionId"]
        assert committing["phase"] == "terminated"
        assert committing["terminationReason"] == "natural_completion"
        assert committing["naturalHandoff"] is None
        assert committing["releaseReady"] is False
        assert committing["commitError"] is None
        assert committing["blockers"] == []
        assert committing["revision"] > committing["persistedRevision"]
        assert backup["status"] == "staged_committed"
        assert backup["businessRevision"] == 1
        assert backup["generation"] == 1
        assert backup["jobId"] and backup["commitId"]
        receipt_path = (
            state_root / "session-backup-coordinator" / session_dir.parent.name / "s1" / "receipts"
        ) / "{}.json".format(backup["jobId"])
        local_receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        assert local_receipt["commitId"] == backup["commitId"]
        assert local_receipt["backupGeneration"] == backup["generation"]
        assert local_receipt["coveredThroughBusinessRevision"] == backup["businessRevision"]
        assert not (staging_root / PENDING_JOBS_DIRNAME / "{}.json".format(backup["jobId"])).exists()

        worker = SessionBackupStagingWorker(staging_root, tmp_path / "backup", coordinator_state_root=state_root)
        snapshots = worker.scan_snapshots()
        assert len(snapshots) == 1
        assert (snapshots[0].path / "session.jsonl").read_text(encoding="utf-8") == "natural-boundary\n"
        publishing = asyncio.create_task(asyncio.to_thread(worker.publish_snapshot, snapshots[0]))
        assert await asyncio.to_thread(publication_started.wait, 5)
        gates.release_commit.set()
        completed = await asyncio.wait_for(finalization, 5)
        ready = completed["naturalHandoff"]
        assert ready == control.natural_handoff_receipt()
        assert ready["contextId"] == pending["contextId"]
        assert ready["taskId"] == pending["taskId"]
        assert ready["executionId"] == pending["executionId"]
        assert ready["completionGeneration"] == generation
        assert ready["businessDrained"] is True
        assert ready["stagedCommitted"] is True
        assert ready["snapshotGeneration"] == backup["generation"]
        assert ready["snapshotCommitId"] == backup["commitId"]
        assert ready["businessRevision"] == backup["businessRevision"]
        assert ready["pendingJobId"] == backup["jobId"]
        assert not publishing.done(), "natural completion waited for shared publication"
        assert (snapshots[0].path / "session.jsonl").is_file()
        release_publication.set()
        await asyncio.wait_for(publishing, 5)
        shared = tmp_path / "backup" / "projects" / session_dir.parent.name / "s1" / "session.jsonl"
        assert shared.read_text(encoding="utf-8") == "natural-boundary\n"
    finally:
        gates.release()
        release_publication.set()
        await control.close()
        await asyncio.gather(finalization, return_exceptions=True)
        if publishing is not None:
            await asyncio.gather(publishing, return_exceptions=True)
        await coordinator.aclose()
