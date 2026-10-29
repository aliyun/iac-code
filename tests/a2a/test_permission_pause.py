"""A physical pause requires the current permission's existing handoff proof."""

import asyncio
import json
import multiprocessing
import subprocess
import sys
import threading
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

from iac_code.a2a.execution_owner import ExecutionOwnerIdentity
from iac_code.a2a.input_handoff import HandoffBackupReceipt, RecoverableInputHandoffProof
from iac_code.a2a.permission_pause import PendingPermissionPauseInspector, PendingPermissionPauseReferences
from iac_code.utils.state_io import atomic_write_json


class PauseFixture:
    def __init__(self, tmp_path, monkeypatch):
        self.identity = ExecutionOwnerIdentity(
            pid=123, runtimeIncarnation="test-boot", pidNamespace="test-namespace", processStartIdentity="1"
        )
        monkeypatch.setattr(ExecutionOwnerIdentity, "capture", classmethod(lambda cls, pid=None: self.identity))
        monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
        self.persistence = tmp_path / "authority"
        self.references = PendingPermissionPauseReferences(self.persistence)
        self.references.initialize()
        self.checkpoint_path = tmp_path / "session" / "permission-waits" / "pwb_boundary.json"
        self.control_path = self.persistence / "execution-control" / "context.json"
        self.record = {
            "schemaVersion": 1,
            "boundaryId": "pwb_boundary",
            "contextId": "context",
            "taskId": "task",
            "sessionId": "session",
            "permissionClass": "normal",
            "phase": "WAITING",
            "generation": 1,
        }
        self.control = {
            "contextId": "context",
            "taskId": "task",
            "sessionId": "session",
            "executionId": "execution",
            "owner": "owner",
            "ownerGeneration": 1,
            "ownerIdentity": self.identity.as_document(),
            "revision": 2,
            "persistedRevision": 2,
            "phase": "running",
            "executionStatus": "working",
            "streamAvailable": True,
            "blockers": [],
            "subprocessToolTrackingVersion": 1,
            "activeSubprocessTools": 0,
            "inputHandoffReady": False,
            "inputHandoff": None,
            "backup": {"status": "not_requested"},
            "releaseReady": False,
        }
        self.write()
        self.register()
        self.inspector = PendingPermissionPauseInspector(self.persistence)

    def write(self):
        atomic_write_json(self.checkpoint_path, self.record)
        atomic_write_json(self.control_path, self.control)

    def register(self):
        self.references.register(
            checkpoint_path=self.checkpoint_path,
            cwd=str(self.checkpoint_path.parent.parent),
            record=self.record,
            control=self.control,
        )

    def committed(self):
        commit = "c62a3752-ec3c-4442-9be8-2f3f31397c25"
        self.record.update(phase="SUSPENDED", generation=3)
        proof = RecoverableInputHandoffProof(
            contextId="context",
            taskId="task",
            sessionId="session",
            executionId="execution",
            boundaryId="pwb_boundary",
            boundaryGeneration=3,
            controlRevision=3,
            backupCommitId=commit,
            phase="COMMITTED",
            backupReceipt=HandoffBackupReceipt(
                generation=4,
                commitId=commit,
                sharedCommitted=True,
                stagedCommitted=True,
            ),
        )
        self.control.update(
            revision=5,
            persistedRevision=5,
            executionStatus="input-required",
            streamAvailable=False,
            inputHandoffReady=True,
            inputHandoff=proof.as_document(),
        )
        self.write()


@pytest.fixture
def pause(tmp_path, monkeypatch):
    return PauseFixture(tmp_path, monkeypatch)


def test_missing_initialization_is_error_without_creating_files(tmp_path):
    root = tmp_path / "authority"
    inspector = PendingPermissionPauseInspector(root)
    assert inspector.inspect()["status"] == "error"
    assert not root.exists()


def test_initialized_empty_runtime_has_no_applicable_permissions(tmp_path):
    references = PendingPermissionPauseReferences(tmp_path / "authority")
    references.initialize()
    assert PendingPermissionPauseInspector(tmp_path / "authority").inspect()["status"] == "not_applicable"


def test_backup_or_suspended_checkpoint_does_not_authorize_pause(pause):
    for phase in ("WAITING", "SUSPENDING", "SUSPENDED"):
        pause.record["phase"] = phase
        pause.control["backup"] = {"status": "shared_committed"}
        pause.write()
        assert pause.inspector.inspect()["status"] == "pending"


def test_exact_normal_handoff_allows_pause_without_modifying_files(pause):
    pause.committed()
    root = pause.persistence.parent
    before = {path: path.read_bytes() for path in root.rglob("*") if path.is_file()}
    assert pause.inspector.inspect()["status"] == "ready"
    assert {path: path.read_bytes() for path in before} == before
    assert set(path for path in root.rglob("*") if path.is_file()) == set(before)


@pytest.mark.parametrize(
    "field,value", [("boundaryId", "pwb_previous"), ("boundaryGeneration", 2), ("phase", "PREPARING")]
)
def test_stale_or_preparing_handoff_cannot_authorize_current_boundary(pause, field, value):
    pause.committed()
    pause.control["inputHandoff"][field] = value
    pause.write()
    assert pause.inspector.inspect()["status"] == "pending"


def test_generic_pipeline_handoff_does_not_require_normal_proof(pause):
    pause.record["permissionClass"] = "pipeline"
    pause.references.root.joinpath("pwb_boundary.json").unlink()
    pause.register()
    pause.control.update(
        revision=3,
        persistedRevision=3,
        executionStatus="input-required",
        streamAvailable=False,
        inputHandoffReady=True,
    )
    pause.write()
    assert pause.inspector.inspect()["status"] == "ready"


def test_new_boundary_does_not_inherit_generic_handoff_revision(pause):
    pause.control.update(executionStatus="input-required", streamAvailable=False, inputHandoffReady=True)
    pause.write()
    assert pause.inspector.inspect()["status"] == "pending"


@pytest.mark.parametrize("backup", ["shared_committed", "staged_committed", "disabled"])
def test_canceled_permission_uses_original_terminal_release_contract(pause, backup):
    pause.record.update(phase="CANCELED", generation=2)
    pause.control.update(phase="terminating", revision=3, persistedRevision=3)
    pause.write()
    assert pause.inspector.inspect()["status"] == "pending"
    pause.control.update(phase="terminated", releaseReady=True, streamAvailable=False, backup={"status": backup})
    pause.write()
    assert pause.inspector.inspect()["status"] == "ready"


def test_blocked_backup_is_not_a_pause_proof(pause):
    pause.record.update(phase="CANCELED", generation=2)
    pause.control.update(phase="terminated", revision=3, persistedRevision=3, backup={"status": "blocked"})
    pause.write()
    assert pause.inspector.inspect()["status"] == "pending"


@pytest.mark.parametrize("phase", ["running", "terminating", "terminated"])
def test_canceled_boundary_is_not_released_by_a_replacement_control(pause, phase):
    pause.committed()
    pause.record.update(phase="CANCELED", generation=4)
    pause.control.update(
        executionId="replacement",
        taskId="new-task",
        phase=phase,
        releaseReady=True,
        backup={"status": "shared_committed"},
    )
    pause.control["ownerIdentity"] = {**pause.control["ownerIdentity"], "pid": 456}
    pause.write()
    assert pause.inspector.inspect()["status"] == "pending"


def test_resolved_predecessor_cannot_hide_a_pending_successor(pause):
    pause.committed()
    previous_path = pause.checkpoint_path
    previous = dict(pause.record, phase="RESOLVED", ack={"nextBoundaryId": "pwb_next"})
    pause.record.update(boundaryId="pwb_next", phase="WAITING", generation=1)
    pause.checkpoint_path = previous_path.with_name("pwb_next.json")
    pause.register()
    pause.write()
    atomic_write_json(previous_path, previous)
    assert pause.inspector.inspect()["status"] == "pending"
    pause.record["phase"] = "RESOLVED"
    pause.write()
    assert pause.inspector.inspect()["status"] == "not_applicable"


@pytest.mark.parametrize("missing", [False, True])
def test_current_checkpoint_corruption_or_loss_fails_closed(pause, missing):
    if missing:
        pause.checkpoint_path.unlink()
    else:
        pause.checkpoint_path.write_text("broken", encoding="utf-8")
    assert pause.inspector.inspect()["status"] == "error"


def test_unreferenced_history_does_not_block_current_runtime(pause):
    pause.committed()
    pause.checkpoint_path.with_name("pwb_history.json").write_text("broken", encoding="utf-8")
    assert pause.inspector.inspect()["status"] == "ready"


def test_all_worker_references_are_checked(pause):
    pause.committed()
    pending_record = dict(pause.record, boundaryId="pwb_other", phase="WAITING", generation=1)
    pending_path = pause.checkpoint_path.with_name("pwb_other.json")
    atomic_write_json(pending_path, pending_record)
    pause.references.register(
        checkpoint_path=pending_path, cwd="/workspace", record=pending_record, control=pause.control
    )
    assert pause.inspector.inspect()["status"] == "pending"


def test_generation_change_during_inspection_retries(pause, monkeypatch):
    pause.committed()
    original = Path.read_bytes
    reads = 0

    def read(path):
        nonlocal reads
        data = original(path)
        if path == pause.checkpoint_path:
            reads += 1
            if reads == 1:
                pause.record["generation"] += 1
                atomic_write_json(path, pause.record)
        return data

    monkeypatch.setattr(Path, "read_bytes", read)
    assert pause.inspector.inspect()["status"] == "pending"


def test_duplicate_reference_is_idempotent_but_conflicting_identity_rejected(pause):
    pause.register()
    changed = deepcopy(pause.control)
    changed["executionId"] = "other"
    with pytest.raises(ValueError):
        pause.references.register(
            checkpoint_path=pause.checkpoint_path, cwd="/other", record=pause.record, control=changed
        )


def test_runtime_namespace_mismatch_fails_closed(pause):
    path = pause.references.root / "pwb_boundary.json"
    reference = json.loads(path.read_text(encoding="utf-8"))
    reference["ownerIdentity"]["pidNamespace"] = "other"
    atomic_write_json(path, reference)
    assert pause.inspector.inspect()["status"] == "error"


@pytest.mark.parametrize("platform", ["linux", "darwin", "win32"])
def test_unconfigured_a2a_does_not_enable_pause_references(tmp_path, monkeypatch, platform):
    native_platform = sys.platform
    monkeypatch.setattr("iac_code.a2a.permission_pause.sys", SimpleNamespace(platform=platform))
    monkeypatch.delenv(PendingPermissionPauseReferences.ENABLED_ENV, raising=False)
    monkeypatch.delenv("IAC_CODE_CONFIG_BACKUP_TMP_DIR", raising=False)
    monkeypatch.setenv("A2A_PERSISTENCE_DIR", str(tmp_path))
    assert PendingPermissionPauseReferences.enabled(tmp_path) is None
    assert PendingPermissionPauseReferences.enabled(None) is None
    assert sys.platform == native_platform


def test_only_matching_linux_sandbox_configuration_auto_enables(pause, monkeypatch):
    monkeypatch.delenv(PendingPermissionPauseReferences.ENABLED_ENV, raising=False)
    monkeypatch.setattr("iac_code.a2a.permission_pause.sys", SimpleNamespace(platform="linux"))
    monkeypatch.setenv("A2A_PERSISTENCE_DIR", str(pause.persistence))
    monkeypatch.setenv("IAC_CODE_CONFIG_BACKUP_TMP_DIR", "/local-staging")
    pause.identity = pause.identity.model_copy(update={"runtime_incarnation": "linux-boot:test"})
    assert PendingPermissionPauseReferences.enabled(pause.persistence) is not None
    assert PendingPermissionPauseReferences.enabled(pause.persistence / "other") is None
    monkeypatch.setenv(PendingPermissionPauseReferences.ENABLED_ENV, "0")
    assert PendingPermissionPauseReferences.enabled(pause.persistence) is None


def test_explicit_enable_requires_identity_and_persistence(tmp_path, monkeypatch):
    monkeypatch.setenv(PendingPermissionPauseReferences.ENABLED_ENV, "1")
    with pytest.raises(ValueError, match="persistence_unavailable"):
        PendingPermissionPauseReferences.enabled(None)
    monkeypatch.setattr(ExecutionOwnerIdentity, "capture", classmethod(lambda cls: ExecutionOwnerIdentity(pid=123)))
    with pytest.raises(ValueError, match="runtime_identity_unavailable"):
        PendingPermissionPauseReferences.enabled(tmp_path)


@pytest.mark.asyncio
async def test_actual_normal_suspension_advances_from_pending_to_ready(tmp_path, monkeypatch):
    from tests.a2a.test_normal_permission_cold_handoff import _NormalPermissionHandoff

    monkeypatch.setenv(PendingPermissionPauseReferences.ENABLED_ENV, "1")
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("IAC_CODE_CONFIG_BACKUP_DIR", str(tmp_path / "shared"))
    monkeypatch.delenv("IAC_CODE_CONFIG_BACKUP_TMP_DIR", raising=False)
    fixture = _NormalPermissionHandoff(tmp_path, monkeypatch)
    try:
        await fixture.start()
        inspector = PendingPermissionPauseInspector(tmp_path / "a2a")
        assert inspector.inspect()["status"] == "pending"
        await fixture.suspend()
        assert inspector.inspect()["status"] == "ready"
        assert await fixture.reserve() is not None
    finally:
        await fixture.close()


@pytest.mark.integration
@pytest.mark.timeout(120)
def test_actual_pipeline_suspension_produces_usable_pause_proof(tmp_path, monkeypatch):
    import time

    from tests.a2a.test_permission_pipeline_engine_e2e import PIPELINE
    from tests.a2a.test_permission_prompt_e2e import Server, _permission, _reply

    monkeypatch.setenv(PendingPermissionPauseReferences.ENABLED_ENV, "1")
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    (tmp_path / "workspace").mkdir()
    server = Server(tmp_path, pipeline_dir=str(PIPELINE), tool_count=1, resident_timeout=0.1)
    try:
        server.start()
        permission = _permission(server.send("original"))
        inspector = PendingPermissionPauseInspector(tmp_path / "a2a")
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            result = inspector.inspect()
            if result["status"] == "ready":
                break
            assert result["status"] == "pending", result
            time.sleep(0.05)
        else:
            control = json.loads((tmp_path / "a2a/execution-control/context-1.json").read_text(encoding="utf-8"))
            pytest.fail(f"Pipeline handoff did not settle: {result}, control={control}")
        server.stop()
        server.start()
        events = server.send(_reply(permission), task_id=permission["requestTaskId"])
        assert "pipeline_handoff_ready" in json.dumps(events), events
        assert not any("error" in event for event in events), events
    finally:
        server.stop()
        server.client.close()


class _ReferenceWriter:
    """Independent worker with its own PID, sharing the Pod's immutable index."""

    @staticmethod
    def run(persistence, checkpoint, record, start):
        references = PendingPermissionPauseReferences(Path(persistence))
        start.wait(20)
        references.initialize()
        references.register(
            checkpoint_path=Path(checkpoint),
            cwd=str(Path(checkpoint).parent.parent),
            record=record,
            control={
                "contextId": record["contextId"],
                "taskId": record["taskId"],
                "executionId": "worker-execution",
                "revision": 0,
                "owner": "",
                "ownerIdentity": ExecutionOwnerIdentity.capture().as_document(),
            },
        )


@pytest.mark.timeout(60)
def test_workers_publish_to_one_runtime_index_and_cli_only_reads(tmp_path, monkeypatch):
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    persistence = tmp_path / "a2a"
    context = multiprocessing.get_context("spawn")
    start = context.Event()
    workers = []
    for index in range(2):
        record = {
            "schemaVersion": 1,
            "boundaryId": f"pwb_{index}",
            "contextId": f"context-{index}",
            "taskId": f"task-{index}",
            "sessionId": f"session-{index}",
            "permissionClass": "normal" if index == 0 else "pipeline",
            "generation": 1,
            "phase": "RESOLVED",
        }
        checkpoint = tmp_path / f"project-{index}" / "permission-waits" / f"pwb_{index}.json"
        atomic_write_json(checkpoint, record)
        worker = context.Process(target=_ReferenceWriter.run, args=(str(persistence), str(checkpoint), record, start))
        worker.start()
        workers.append(worker)
    try:
        start.set()
        for worker in workers:
            worker.join(30)
            assert worker.exitcode == 0
        references = PendingPermissionPauseReferences(persistence)
        assert len(references.paths()) == 2
        documents = [references.read_document(path) for path in references.paths()]
        assert len({document["ownerIdentity"]["pid"] for document in documents}) == 2
        before = {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
        # The service's initialized manifest is authoritative even if this
        # separately launched probe has no enabling environment variable.
        monkeypatch.setenv(PendingPermissionPauseReferences.ENABLED_ENV, "0")
        command = [sys.executable, "-m", "iac_code.a2a.permission_pause", "--persistence-root", str(persistence)]
        result = subprocess.run(command, capture_output=True, text=True, timeout=30)
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout) == {
            "schemaVersion": 1,
            "status": "not_applicable",
            "reason": "verified",
            "checked": 2,
        }
        assert {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()} == before
        missing = subprocess.run(
            command[:-1] + [str(persistence / "wrong")], capture_output=True, text=True, timeout=30
        )
        assert missing.returncode == 2
        assert json.loads(missing.stdout)["status"] == "error"
        assert not (persistence / "wrong").exists()
    finally:
        start.set()
        for worker in workers:
            if worker.is_alive():
                worker.kill()
                worker.join(10)
            worker.close()


class _RegistrationGate:
    def __init__(self, references, *, fail=False):
        self.register = references.register
        self.started = threading.Event()
        self.release = threading.Event()
        self.finished = threading.Event()
        self.fail = fail
        self.checkpoint = None
        if fail:
            self.release.set()

    def write(self, **kwargs):
        self.checkpoint = kwargs["checkpoint_path"]
        self.started.set()
        try:
            assert self.release.wait(20), "test did not release reference write"
            if self.fail:
                raise OSError("reference write failed")
            self.register(**kwargs)
        finally:
            self.finished.set()


@pytest.mark.asyncio
@pytest.mark.parametrize("fail", [False, True], ids=["cancel-slow-write", "failed-write"])
async def test_unregistered_permission_is_not_published_and_cancellation_does_not_join_write(
    tmp_path, monkeypatch, fail
):
    from iac_code.a2a.events import publish_interactive_permission_boundary
    from iac_code.a2a.execution_control import ExecutionController, bind_execution_control, reset_execution_control
    from iac_code.a2a.input_required import PermissionInputRegistry
    from iac_code.services.permission_wait import PermissionWaitCoordinator, PermissionWaitPolicy
    from iac_code.services.session_storage import SessionStorage
    from iac_code.types.stream_events import PermissionRequestEvent
    from tests.a2a.fakes import FakeEventQueue
    from tests.a2a.test_execution_control_regressions import wait_until
    from tests.a2a.test_permission_execution_control import continuation_frame

    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setattr("iac_code.a2a.input_required.emit_permission_boundary_audit", lambda *_a, **_kw: True)
    references = PendingPermissionPauseReferences(tmp_path / "a2a")
    references.initialize()
    gate = _RegistrationGate(references, fail=fail)
    monkeypatch.setattr(references, "register", gate.write)
    registry = PermissionInputRegistry()
    registry.set_permission_wait_coordinator(PermissionWaitCoordinator(PermissionWaitPolicy()))
    SessionStorage().ensure_v2_session_dir_for_new_session(str(tmp_path), "session")
    control = ExecutionController(
        context_id="context",
        task_id="task",
        owner="",
        cwd=str(tmp_path),
        server_instance_id="server",
        persistence_path=tmp_path / "a2a/execution-control/context.json",
        backup_service=None,
        permission_pause_references=references,
    )
    queue = FakeEventQueue()
    request = PermissionRequestEvent(
        tool_name="bash",
        tool_input={"cmd": "pwd"},
        tool_use_id="tool-1",
        response_future=asyncio.get_running_loop().create_future(),
        continuation_frame=continuation_frame(),
    )
    token = bind_execution_control(control)
    publication = asyncio.create_task(
        publish_interactive_permission_boundary(
            queue,
            permission_event=request,
            permission_input_registry=registry,
            task_id="task",
            context_id="context",
            iac_code_session_id="session",
            permission_wait_cwd=str(tmp_path),
            permission_wait_backup_service=object(),
            wait_for_response=False,
        )
    )
    reset_execution_control(token)
    try:
        await wait_until(gate.started.is_set)
        if fail:
            with pytest.raises(OSError, match="reference write failed"):
                await publication
        else:
            publication.cancel()
            with pytest.raises(asyncio.CancelledError):
                await publication
            assert not gate.finished.is_set(), "Cancellation must not join the new local writer"
        assert queue.events == []
        assert gate.checkpoint is not None
        checkpoint = json.loads(gate.checkpoint.read_text(encoding="utf-8"))
        assert checkpoint["phase"] == "CANCELED", "Existing publication-failure cleanup must retain its checkpoint"
        gate.release.set()
        await wait_until(gate.finished.is_set)
        assert json.loads(gate.checkpoint.read_text(encoding="utf-8")) == checkpoint, (
            "A late reference cannot reactivate the boundary"
        )
        assert request.response_future.result() is False
    finally:
        gate.release.set()
        await asyncio.gather(publication, return_exceptions=True)
        await wait_until(gate.finished.is_set)


@pytest.mark.integration
@pytest.mark.timeout(120)
@pytest.mark.parametrize("pipeline", [False, True], ids=["normal", "pipeline"])
@pytest.mark.parametrize("fault", ["runtime-close", "backup-failure"])
def test_sse_ends_while_existing_handoff_cleanup_is_blocked(tmp_path, monkeypatch, pipeline, fault):
    from tests.a2a.test_permission_handoff_process_e2e import wait_for
    from tests.a2a.test_permission_pipeline_engine_e2e import PIPELINE
    from tests.a2a.test_permission_prompt_e2e import Server, _permission

    monkeypatch.setenv(PendingPermissionPauseReferences.ENABLED_ENV, "1")
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    (tmp_path / "workspace").mkdir()
    server = Server(
        tmp_path, pipeline_dir=str(PIPELINE) if pipeline else None, tool_count=1, resident_timeout=0.1, fault=fault
    )
    try:
        server.start()
        # send() consumes the stream through EOF, before releasing either gate.
        assert _permission(server.send("original"))
        marker = "runtime-close-started" if fault == "runtime-close" else "backup-failed"
        wait_for(lambda: (tmp_path / marker).exists())
        inspector = PendingPermissionPauseInspector(tmp_path / "a2a")
        assert inspector.inspect()["status"] == "pending"
        assert not (tmp_path / "executions.jsonl").exists()
    finally:
        (tmp_path / "release-runtime-close").touch()
        (tmp_path / "release-backup").touch()
        server.stop()
        server.client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("suspended", [False, True], ids=["resident", "committed"])
@pytest.mark.parametrize("corrupt_probe", [False, True], ids=["probe-pending", "probe-error"])
async def test_stop_keeps_original_acceptance_and_terminal_release(tmp_path, monkeypatch, suspended, corrupt_probe):
    from tests.a2a.test_normal_permission_cold_handoff import _NormalPermissionHandoff
    from tests.a2a.test_permission_handoff_control_races import _TerminationCleanupGate

    monkeypatch.setenv(PendingPermissionPauseReferences.ENABLED_ENV, "1")
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("IAC_CODE_CONFIG_BACKUP_DIR", str(tmp_path / "shared"))
    monkeypatch.delenv("IAC_CODE_CONFIG_BACKUP_TMP_DIR", raising=False)
    fixture = _NormalPermissionHandoff(tmp_path, monkeypatch)
    gate = _TerminationCleanupGate()
    try:
        await fixture.start()
        if suspended:
            await fixture.suspend()
        inspector = PendingPermissionPauseInspector(tmp_path / "a2a")
        if corrupt_probe:
            path = PendingPermissionPauseReferences(tmp_path / "a2a").paths()[0]
            path.write_text("broken", encoding="utf-8")
        fixture.control._termination_cleanup = gate.cleanup
        fixture.control._termination_cleanup_complete = False
        # Acceptance returns while the original asynchronous cleanup is blocked.
        await fixture.control.terminate(
            execution_id=fixture.control.execution_id, request_id="stop", connection_epoch=1, reason="user_canceled"
        )
        await asyncio.wait_for(gate.entered.wait(), timeout=5)
        assert not gate.release.is_set()
        assert inspector.inspect()["status"] == ("error" if corrupt_probe else "pending")
        gate.release.set()
        await fixture.drain()
        assert fixture.control.release_ready
        assert fixture.control.snapshot()["commitError"] is None
        assert inspector.inspect()["status"] == ("error" if corrupt_probe else "ready")
    finally:
        gate.release.set()
        await fixture.close()


@pytest.mark.parametrize("field,value", [("persistedRevision", True), ("sessionId", "other-session")])
def test_malformed_or_mismatched_control_cannot_authorize_pause(pause, field, value):
    pause.committed()
    pause.control[field] = value
    pause.write()
    assert pause.inspector.inspect()["status"] == "pending"


@pytest.mark.asyncio
@pytest.mark.parametrize("other_worker", [False, True], ids=["same-worker", "another-service"])
@pytest.mark.parametrize("fail_retirement", [False, True], ids=["retired", "retirement-io-error"])
async def test_stop_reentry_and_natural_finish_do_not_pin_canceled_history(
    tmp_path, monkeypatch, other_worker, fail_retirement
):
    from tests.a2a.test_normal_permission_cold_handoff import _NormalPermissionHandoff

    monkeypatch.setenv(PendingPermissionPauseReferences.ENABLED_ENV, "1")
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("IAC_CODE_CONFIG_BACKUP_DIR", str(tmp_path / "shared"))
    monkeypatch.delenv("IAC_CODE_CONFIG_BACKUP_TMP_DIR", raising=False)
    fixture = _NormalPermissionHandoff(tmp_path, monkeypatch)
    try:
        await fixture.start()
        await fixture.control.terminate(
            execution_id=fixture.control.execution_id, request_id="stop", connection_epoch=1, reason="user_canceled"
        )
        await fixture.drain()
        assert fixture.control.release_ready
        assert fixture.pending.checkpoint_store.load(fixture.pending.boundary_id)["phase"] == "CANCELED"
        inspector = PendingPermissionPauseInspector(tmp_path / "a2a")
        assert inspector.inspect()["status"] == "ready"
        references = PendingPermissionPauseReferences(tmp_path / "a2a")
        reference_path = references.paths()[0]
        if fail_retirement:
            unlink = Path.unlink

            def fail_reference_unlink(path, *args, **kwargs):
                if path == reference_path:
                    raise OSError("reference retirement failed")
                return unlink(path, *args, **kwargs)

            monkeypatch.setattr(Path, "unlink", fail_reference_unlink)
        service = fixture.recovering if other_worker else fixture.service
        successor = await service.begin_execution(context_id="ctx-1", task_id="next-task", owner="", cwd=str(tmp_path))
        assert successor.execution_id != fixture.control.execution_id
        successor.bind_session(fixture.control.session_id)
        generation = await successor.detach_task(
            asyncio.current_task(), execution_status="completed", natural_completion=True
        )
        assert generation is not None
        await service.finalize_natural_completion(
            context_id="ctx-1", task_id="next-task", owner="", completion_generation=generation
        )
        while successor._background_tasks:
            await asyncio.gather(*tuple(successor._background_tasks))
        assert successor.release_ready
        assert inspector.inspect()["status"] == ("pending" if fail_retirement else "not_applicable")
        assert reference_path.exists() is fail_retirement
    finally:
        await fixture.close()


@pytest.mark.parametrize("phase", ["running", "terminating", "terminated"])
def test_retirement_cannot_use_cancellation_or_replacement_as_release_evidence(pause, phase):
    pause.record.update(phase="CANCELED", generation=2)
    pause.control.update(phase=phase, revision=3, persistedRevision=3, streamAvailable=False)
    pause.write()
    pause.references.retire_released(pause.control)
    assert len(pause.references.paths()) == 1
    # A different execution, even terminal, is never the old release proof.
    replacement = dict(pause.control, executionId="next-execution", releaseReady=True, backup={"status": "disabled"})
    pause.references.retire_released(replacement)
    assert len(pause.references.paths()) == 1


class _RetirementLockFailure:
    def __init__(self, cause):
        self.cause = cause

    def __enter__(self):
        raise RuntimeError("could not acquire reference file lock") from self.cause

    def __exit__(self, *_args):
        return False


@pytest.mark.asyncio
@pytest.mark.parametrize("recover", [False, True], ids=["begin", "recover-activation"])
async def test_reference_lock_io_failure_does_not_block_execution_admission(tmp_path, monkeypatch, recover):
    from tests.a2a.test_normal_permission_cold_handoff import _NormalPermissionHandoff

    monkeypatch.setenv(PendingPermissionPauseReferences.ENABLED_ENV, "1")
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("IAC_CODE_CONFIG_BACKUP_DIR", str(tmp_path / "shared"))
    monkeypatch.delenv("IAC_CODE_CONFIG_BACKUP_TMP_DIR", raising=False)
    fixture = _NormalPermissionHandoff(tmp_path, monkeypatch)
    try:
        await fixture.start()
        if recover:
            await fixture.suspend()
            admission = await fixture.reserve()
            assert admission is not None
        else:
            await fixture.control.terminate(
                execution_id=fixture.control.execution_id, request_id="stop", connection_epoch=1, reason="user_canceled"
            )
            await fixture.drain()
            assert fixture.control.release_ready
        references = PendingPermissionPauseReferences(tmp_path / "a2a")
        reference_path = references.paths()[0]
        reference_before = reference_path.read_bytes()
        checkpoint_path = (
            fixture.pending.checkpoint_store.paths.permission_waits_dir / f"{fixture.pending.boundary_id}.json"
        )
        checkpoint_before = checkpoint_path.read_bytes()
        with monkeypatch.context() as fault:
            fault.setattr(
                "iac_code.a2a.permission_pause.cross_process_file_lock",
                lambda _path: _RetirementLockFailure(OSError("reference lock unavailable")),
            )
            if recover:
                successor = await fixture.activate(admission)
            else:
                successor = await fixture.recovering.begin_execution(
                    context_id="ctx-1", task_id="next-task", owner="", cwd=str(tmp_path)
                )
        assert successor.execution_id != fixture.control.execution_id
        assert fixture.recovering.get_for_context("ctx-1") is successor
        persisted = json.loads((tmp_path / "a2a/execution-control/ctx-1.json").read_bytes())
        assert persisted["executionId"] == successor.execution_id
        assert persisted["persistedRevision"] == persisted["revision"]
        assert reference_path.read_bytes() == reference_before
        assert checkpoint_path.read_bytes() == checkpoint_before
        assert PendingPermissionPauseInspector(tmp_path / "a2a").inspect()["status"] == "pending"
    finally:
        await fixture.close()


@pytest.mark.parametrize("source", ["lock", "inspection"])
def test_retirement_does_not_hide_unrelated_runtime_errors(pause, monkeypatch, source):
    pause.record.update(phase="CANCELED", generation=2)
    pause.control.update(
        phase="terminated",
        revision=3,
        persistedRevision=3,
        streamAvailable=False,
        releaseReady=True,
        backup={"status": "disabled"},
    )
    pause.write()
    reference_path = pause.references.paths()[0]
    reference_before = reference_path.read_bytes()
    if source == "lock":
        monkeypatch.setattr(
            "iac_code.a2a.permission_pause.cross_process_file_lock", lambda _path: _RetirementLockFailure(None)
        )
    else:

        def fail_inspection(*_args):
            raise RuntimeError("inspection failed") from OSError("inspection I/O failure")

        monkeypatch.setattr(PendingPermissionPauseInspector, "_inspect_boundary", fail_inspection)
    with pytest.raises(RuntimeError, match="reference file lock|inspection failed"):
        pause.references.retire_released(pause.control)
    assert reference_path.read_bytes() == reference_before


@pytest.mark.parametrize("permission_class", ["normal", "pipeline"])
@pytest.mark.skipif(sys.platform == "win32", reason="CSI mount rebinding requires POSIX symlinks")
def test_prewarmed_index_survives_csi_claim_symlink_rebinding(pause, monkeypatch, permission_class):
    logical_mount = pause.persistence.parent / "csi-mount"
    logical_persistence = logical_mount / "a2a"
    logical_persistence.mkdir(parents=True)
    monkeypatch.setattr("iac_code.a2a.permission_pause.sys", SimpleNamespace(platform="linux"))
    monkeypatch.delenv(PendingPermissionPauseReferences.ENABLED_ENV, raising=False)
    monkeypatch.setenv("A2A_PERSISTENCE_DIR", str(logical_persistence))
    monkeypatch.setenv("IAC_CODE_CONFIG_BACKUP_TMP_DIR", str(pause.persistence.parent / "staging"))
    pause.identity = pause.identity.model_copy(update={"runtime_incarnation": "linux-boot:test"})
    pause.control["ownerIdentity"] = pause.identity.as_document()
    warm_references = PendingPermissionPauseReferences.enabled(logical_persistence)
    assert warm_references is not None
    manifest_before = (warm_references.root / "manifest.json").read_bytes()

    logical_mount.rename(logical_mount.with_name("prewarm-mount"))
    claimed_mount = logical_mount.with_name("claimed-mount")
    claimed_mount.mkdir()
    logical_mount.symlink_to(claimed_mount, target_is_directory=True)

    inspector = PendingPermissionPauseInspector(logical_persistence)
    assert inspector.inspect()["status"] == "not_applicable"
    claimed_references = PendingPermissionPauseReferences.enabled(logical_persistence)
    assert claimed_references is not None
    assert claimed_references.root == warm_references.root
    assert claimed_references.persistence_root == logical_persistence
    assert (warm_references.root / "manifest.json").read_bytes() == manifest_before
    # A physically equivalent alias is not the configured logical authority.
    assert PendingPermissionPauseReferences.enabled(claimed_mount / "a2a") is None

    pause.record["permissionClass"] = permission_class
    pause.control_path = logical_persistence / "execution-control" / "context.json"
    pause.write()
    warm_references.register(
        checkpoint_path=pause.checkpoint_path,
        cwd=str(pause.checkpoint_path.parent.parent),
        record=pause.record,
        control=pause.control,
    )
    assert inspector.inspect()["status"] == "pending"
    pause.committed()
    assert inspector.inspect()["status"] == "ready"
    assert (claimed_mount / "a2a/execution-control/context.json").is_file()
