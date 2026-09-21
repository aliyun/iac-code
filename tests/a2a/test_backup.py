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
