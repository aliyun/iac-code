"""Connection holds and termination across durable permission recovery."""

import asyncio
import json
import threading
from datetime import timedelta

import pytest
from a2a.types import Message as A2AMessage
from a2a.types import Part, Role, TaskState

from iac_code.a2a.execution_control import ExecutionController, execution_checkpoint
from iac_code.a2a.executor import IacCodeA2AExecutor
from iac_code.a2a.input_required import PERMISSION_QUERY_PREFIX
from iac_code.agent.message import Message
from iac_code.services.permission_wait import (
    PermissionWaitCheckpointStore,
    PermissionWaitCoordinator,
    PermissionWaitPolicy,
    build_permission_checkpoint,
    format_utc,
    utc_now,
)
from iac_code.services.session_backup import BackupReason, SessionBackupService
from iac_code.services.session_backup_staging import SessionBackupStagingWorker, StagedSessionBackupService
from iac_code.services.session_storage import SessionStorage
from iac_code.types.stream_events import PermissionRequestEvent, PermissionWaitOutcome, TextDeltaEvent

from .fakes import FakeEventQueue, FakeRequestContext, FakeRuntime
from .test_execution_control_regressions import publish_staged_backups, wait_until
from .test_execution_control_review_regressions import make_executor


@pytest.fixture(autouse=True)
def isolated_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("IAC_CODE_MODE", "normal")
    monkeypatch.delenv("IAC_CODE_CONFIG_BACKUP_DIR", raising=False)
    monkeypatch.delenv("IAC_CODE_CONFIG_BACKUP_TMP_DIR", raising=False)


def continuation_frame():
    return {
        "assistantMessageRef": "session.jsonl:0",
        "assistantMessageDigest": "a" * 64,
        "orderedToolUseIds": ["tool-1"],
        "currentIndex": 0,
        "decisions": [{"toolUseId": "tool-1", "state": "pending", "source": None, "deniedResult": None}],
    }


def permission_reply(tmp_path, *, input_id="input-1"):
    response = {
        "schemaVersion": 1,
        "kind": "permission",
        "requestTaskId": "task-1",
        "contextId": "ctx-1",
        "inputId": input_id,
        "toolUseId": "tool-1",
        "decision": "allow_once",
    }
    request = FakeRequestContext(metadata={"iac_code": {"cwd": str(tmp_path)}})
    request.message = A2AMessage(
        message_id="reply",
        context_id="ctx-1",
        role=Role.ROLE_USER,
        parts=[Part(text=PERMISSION_QUERY_PREFIX + " " + json.dumps(response))],
    )
    return request


def checkpoint_record(session_id, *, policy=None, permission_class="normal"):
    frame = continuation_frame()
    if permission_class == "pipeline":
        frame["assistantMessageRef"] = "pipeline/transcripts/transcript-1/session.jsonl:0"
    return build_permission_checkpoint(
        session_id=session_id,
        task_id="task-1",
        context_id="ctx-1",
        input_id="input-1",
        tool_use_id="tool-1",
        tool_name="bash",
        tool_input={"cmd": "pwd"},
        permission_class=permission_class,
        continuation_frame=frame,
        policy=policy or PermissionWaitPolicy(),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("staged", [False, True])
@pytest.mark.parametrize(
    ("finished", "fail_checkpoint", "close_gate"),
    [(False, False, False), (False, True, False), (True, False, False), (True, False, True)],
)
async def test_persisted_recovery_termination_seals_checkpoint_and_backup(
    tmp_path, monkeypatch, staged, finished, fail_checkpoint, close_gate
):
    shared = tmp_path / "shared"
    monkeypatch.setenv("IAC_CODE_CONFIG_BACKUP_DIR", str(shared))
    backup = StagedSessionBackupService(tmp_path / "staging") if staged else SessionBackupService()
    executor, store, service = make_executor(tmp_path, backup)
    ctx = await store.get_or_create_context(
        context_id="ctx-1", cwd=str(tmp_path), runtime_factory=lambda sid: FakeRuntime(session_id=sid)
    )
    SessionStorage().append(str(tmp_path), ctx.session_id, Message(role="user", content="test permission"))
    task = await store.get_or_create_task(task_id="task-1", context_id="ctx-1")
    task.state = "input-required"
    store.mirror_task(task)
    checkpoints = PermissionWaitCheckpointStore(str(tmp_path), ctx.session_id)
    checkpoint = checkpoints.create(checkpoint_record(ctx.session_id))
    boundary_id = checkpoint["boundaryId"]
    checkpoints.mark_suspended(boundary_id)
    claimed, _ = checkpoints.claim_decision(boundary_id, value="allow_once", source="user")
    claim_id = claimed["decision"]["claimId"]
    checkpoints.run_claim_audit_once(boundary_id, claim_id=claim_id, audit=lambda _: True)
    checkpoints.mark_claim_backed_up(boundary_id, claim_id=claim_id)
    control = await service.begin_execution(context_id="ctx-1", task_id="task-1", owner="", cwd=str(tmp_path))
    control.bind_session(ctx.session_id)
    await control.detach_task(asyncio.current_task(), execution_status="input-required")
    started = asyncio.Event()
    backup_started, release_backup = threading.Event(), threading.Event()
    closed = []
    close_started, release_close = asyncio.Event(), asyncio.Event()
    original_cancel_restore = PermissionWaitCheckpointStore.cancel_restore
    failed = False

    def flaky_cancel_restore(self, *args, **kwargs):
        nonlocal failed
        if fail_checkpoint and not failed:
            failed = True
            raise OSError("simulated checkpoint write failure")
        return original_cancel_restore(self, *args, **kwargs)

    monkeypatch.setattr(PermissionWaitCheckpointStore, "cancel_restore", flaky_cancel_restore)

    class RecoveryLoop:
        async def resume_permission_boundary(self, _checkpoint):
            started.set()
            if not finished:
                await asyncio.Event().wait()
            yield TextDeltaEvent(text="recovered result")

    async def close():
        close_started.set()
        if close_gate:
            await release_close.wait()
        closed.append(True)

    monkeypatch.setattr(
        "iac_code.a2a.executor.create_agent_runtime",
        lambda options: FakeRuntime(
            session_id=options.session_id,
            agent_loop=RecoveryLoop(),
            aclose=close,
        ),
    )
    original_backup = backup.backup_session

    def gated_backup(*args, **kwargs):
        if kwargs.get("reason") == BackupReason.NORMAL_TURN_END:
            backup_started.set()
            assert release_backup.wait(5)
        return original_backup(*args, **kwargs)

    monkeypatch.setattr(backup, "backup_session", gated_backup)
    publisher = (
        asyncio.create_task(publish_staged_backups(SessionBackupStagingWorker(tmp_path / "staging", shared)))
        if staged
        else None
    )
    queue = FakeEventQueue()
    recovery = asyncio.create_task(executor.execute(permission_reply(tmp_path), queue))
    try:
        await asyncio.wait_for(started.wait(), 3)
        assert task.active_task is recovery
        assert ctx.active_task_id == "task-1"
        if close_gate:
            await asyncio.wait_for(close_started.wait(), 3)
        elif finished:
            assert await asyncio.to_thread(backup_started.wait, 3)
        else:
            assert task.state == control.execution_status == "working"
        await control.terminate(
            execution_id=control.execution_id, request_id="terminate", connection_epoch=1, reason="explicit_terminate"
        )
        assert not control.release_ready
        release_backup.set()
        release_close.set()
        if fail_checkpoint:
            await wait_until(lambda: control.backup["status"] == "blocked")
            assert not control.release_ready
            assert checkpoints.load(boundary_id)["phase"] == "RESTORING"
            await control.terminate(
                execution_id=control.execution_id, request_id="retry", connection_epoch=2, reason="explicit_terminate"
            )
        await wait_until(lambda: control.release_ready)
        await recovery
        expected = "input-required" if finished else "canceled"
        assert task.state == control.execution_status == expected
        assert task.active_task is None and ctx.active_task_id is None
        assert closed == [True]
        assert queue.events[-1].status.state == (
            TaskState.TASK_STATE_INPUT_REQUIRED if finished else TaskState.TASK_STATE_CANCELED
        )
        assert store._persistence.load_task("task-1").state == expected
        shared_task = json.loads(next(shared.rglob("a2a/task.json")).read_text(encoding="utf-8"))
        assert shared_task["state"] == expected
        final_checkpoint = checkpoints.load(boundary_id)
        assert final_checkpoint["phase"] == ("RESOLVED" if finished else "CANCELED")
        assert final_checkpoint["decision"]["claimId"] == claim_id
        shared_checkpoint = next(shared.rglob(boundary_id + ".json"))
        assert json.loads(shared_checkpoint.read_text(encoding="utf-8"))["phase"] == final_checkpoint["phase"]
    finally:
        release_backup.set()
        release_close.set()
        recovery.cancel()
        await asyncio.gather(recovery, return_exceptions=True)
        await service.close()
        if publisher is not None:
            publisher.cancel()
            await asyncio.gather(publisher, return_exceptions=True)
        await store.stop_cleanup_loop()


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["resume", "terminate", "approve_resume"])
async def test_normal_permission_resident_timer_keeps_paused_runtime(tmp_path, monkeypatch, action):
    backup = SessionBackupService()
    _, store, service = make_executor(tmp_path, backup)
    executor = IacCodeA2AExecutor(
        task_store=store,
        model="test",
        backup_service=backup,
        execution_control_service=service,
        permission_wait_policy=PermissionWaitPolicy(resident_timeout_seconds=0.2, timeout_grace_seconds=0),
    )
    future = asyncio.get_running_loop().create_future()
    ran = asyncio.Event()
    closed = []

    class PermissionLoop:
        async def run_streaming(self, _prompt):
            yield PermissionRequestEvent(
                tool_name="bash",
                tool_input={"cmd": "pwd"},
                tool_use_id="tool-1",
                response_future=future,
                continuation_frame=continuation_frame(),
            )
            await execution_checkpoint()
            ran.set()
            yield TextDeltaEvent(text="approved")

    async def close():
        closed.append(True)

    monkeypatch.setattr(
        "iac_code.a2a.executor.create_agent_runtime",
        lambda options: FakeRuntime(
            session_id=options.session_id,
            agent_loop=PermissionLoop(),
            aclose=close,
        ),
    )
    monkeypatch.setattr("iac_code.a2a.input_required.emit_permission_boundary_audit", lambda *args, **kwargs: True)
    response_task = None
    try:
        await executor.execute(FakeRequestContext(metadata={"iac_code": {"cwd": str(tmp_path)}}), FakeEventQueue())
        pending = next(iter(executor._permission_input_registry._pending.values()))
        control = service.get_for_context("ctx-1")
        paused = await control.pause(
            task_id="task-1",
            expected_execution_id=control.execution_id,
            request_id="pause",
            connection_epoch=1,
            reason="disconnect",
            reconnect_timeout_seconds=60,
        )
        await wait_until(lambda: control.phase == "paused")
        await asyncio.sleep(0.3)
        assert not closed and not future.done() and pending.continuation is not None
        assert control.phase == "paused"
        if action == "terminate":
            await control.terminate(
                execution_id=control.execution_id,
                request_id="terminate",
                connection_epoch=2,
                reason="explicit_terminate",
            )
            await wait_until(lambda: control.release_ready)
            assert closed == [True] and not ran.is_set()
            assert not executor._permission_wait_coordinator.has_live_owners()
            return
        if action == "approve_resume":
            response_task = asyncio.create_task(
                executor.execute(
                    permission_reply(tmp_path, input_id=pending.input_id),
                    FakeEventQueue(),
                )
            )
            await wait_until(future.done)
            assert not ran.is_set() and not closed
        await control.resume(
            execution_id=control.execution_id,
            pause_id=paused["pauseId"],
            request_id="resume",
            connection_epoch=2,
        )
        if response_task is not None:
            await asyncio.wait_for(response_task, 3)
            assert ran.is_set() and not closed
        else:
            await wait_until(lambda: bool(closed))
            assert closed == [True] and not ran.is_set()
    finally:
        if response_task is not None:
            response_task.cancel()
            await asyncio.gather(response_task, return_exceptions=True)
        await service.close()
        await store.stop_cleanup_loop()


@pytest.mark.asyncio
@pytest.mark.parametrize("permission_class", ["normal", "pipeline"])
@pytest.mark.parametrize("terminate", [False, True])
async def test_permission_cleanup_claim_blocks_pause_until_slow_write_finishes(
    tmp_path, monkeypatch, permission_class, terminate
):
    storage = SessionStorage()
    storage.ensure_v2_session_dir_for_new_session(str(tmp_path), "session-1")
    checkpoints = PermissionWaitCheckpointStore(str(tmp_path), "session-1")
    policy = PermissionWaitPolicy(timeout_grace_seconds=0)
    record = checkpoint_record("session-1", policy=policy, permission_class=permission_class)
    record["residentDeadlineAt"] = format_utc(utc_now() - timedelta(seconds=1))
    record = checkpoints.create(record)
    control = ExecutionController(
        context_id="ctx-1",
        task_id="task-1",
        owner="",
        cwd=str(tmp_path),
        server_instance_id="test",
        persistence_path=None,
        backup_service=None,
    )
    coordinator = PermissionWaitCoordinator(policy)
    future = asyncio.get_running_loop().create_future()
    cleanup_calls = []

    async def on_suspend():
        cleanup_calls.append(True)
        coordinator.unregister_live(record["boundaryId"])

    coordinator.register_live(
        record=record,
        store=checkpoints,
        future=future,
        on_suspend=on_suspend,
        run_suspension=control.run_permission_suspension,
        suspension_allowed=control.permission_suspension_allowed,
    )
    started, release = threading.Event(), threading.Event()
    original_reconcile = checkpoints.reconcile_deadline

    def gated_reconcile(*args, **kwargs):
        started.set()
        assert release.wait(5)
        return original_reconcile(*args, **kwargs)

    monkeypatch.setattr(checkpoints, "reconcile_deadline", gated_reconcile)
    suspension = asyncio.create_task(coordinator.suspend_now(record["boundaryId"]))
    try:
        assert await asyncio.to_thread(started.wait, 3)
        await asyncio.wait_for(
            control.pause(
                task_id="task-1",
                expected_execution_id=control.execution_id,
                request_id="pause",
                connection_epoch=1,
                reason="disconnect",
                reconnect_timeout_seconds=60,
            ),
            0.5,
        )
        assert control.phase == "pausing" and not future.done()
        if terminate:
            await control.terminate(
                execution_id=control.execution_id,
                request_id="terminate",
                connection_epoch=2,
                reason="explicit_terminate",
            )
            # Let the termination driver cancel the waiter while its worker is still gated.
            await asyncio.sleep(0)
            assert not control.release_ready
        release.set()
        if terminate:
            result = await asyncio.gather(suspension, return_exceptions=True)
            assert isinstance(result[0], asyncio.CancelledError)
            await wait_until(lambda: control.release_ready)
        else:
            assert await suspension
            await wait_until(lambda: control.phase == "paused")
        assert future.result() is PermissionWaitOutcome.SUSPEND
        assert cleanup_calls == [True]
        assert not control.has_managed_work()
    finally:
        release.set()
        suspension.cancel()
        await asyncio.gather(suspension, return_exceptions=True)
        coordinator.unregister_live(record["boundaryId"])
        await control.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("permission_class", ["normal", "pipeline"])
async def test_permission_resident_timer_waits_for_hold_before_signaling_suspend(tmp_path, permission_class):
    storage = SessionStorage()
    storage.ensure_v2_session_dir_for_new_session(str(tmp_path), "session-1")
    checkpoints = PermissionWaitCheckpointStore(str(tmp_path), "session-1")
    policy = PermissionWaitPolicy(resident_timeout_seconds=0.05, timeout_grace_seconds=0)
    record = checkpoints.create(checkpoint_record("session-1", policy=policy, permission_class=permission_class))
    control = ExecutionController(
        context_id="ctx-1",
        task_id="task-1",
        owner="",
        cwd=str(tmp_path),
        server_instance_id="test",
        persistence_path=None,
        backup_service=None,
    )
    coordinator = PermissionWaitCoordinator(policy)
    future = asyncio.get_running_loop().create_future()
    closed = []

    async def on_suspend():
        closed.append(True)
        coordinator.unregister_live(record["boundaryId"])

    coordinator.register_live(
        record=record,
        store=checkpoints,
        future=future,
        on_suspend=on_suspend,
        run_suspension=control.run_permission_suspension,
        suspension_allowed=control.permission_suspension_allowed,
    )
    try:
        paused = await control.pause(
            task_id="task-1",
            expected_execution_id=control.execution_id,
            request_id="pause",
            connection_epoch=1,
            reason="disconnect",
            reconnect_timeout_seconds=60,
        )
        await wait_until(lambda: control.phase == "paused")
        await asyncio.sleep(0.1)
        assert not future.done() and not closed
        assert checkpoints.load(record["boundaryId"])["phase"] == "WAITING"
        await control.resume(
            execution_id=control.execution_id,
            pause_id=paused["pauseId"],
            request_id="resume",
            connection_epoch=2,
        )
        assert await asyncio.wait_for(future, 3) is PermissionWaitOutcome.SUSPEND
        await wait_until(lambda: not control.has_managed_work())
        assert closed == [True]
        assert not coordinator.has_live_owners()
    finally:
        coordinator.unregister_live(record["boundaryId"])
        await control.close()


def test_execution_termination_seals_only_its_claimed_restore(tmp_path):
    storage = SessionStorage()
    storage.ensure_v2_session_dir_for_new_session(str(tmp_path), "session-1")
    checkpoints = PermissionWaitCheckpointStore(str(tmp_path), "session-1")
    record = checkpoints.create(checkpoint_record("session-1"))
    boundary_id = record["boundaryId"]
    checkpoints.mark_suspended(boundary_id)
    claimed, _ = checkpoints.claim_decision(boundary_id, value="allow_once", source="user")
    claim_id = claimed["decision"]["claimId"]
    with pytest.raises(ValueError, match="not restoring"):
        checkpoints.cancel_restore(boundary_id, claim_id=claim_id)
    checkpoints.begin_restore(boundary_id)
    with pytest.raises(ValueError, match="already claimed"):
        checkpoints.cancel(boundary_id)
    with pytest.raises(ValueError, match="claim changed"):
        checkpoints.cancel_restore(boundary_id, claim_id="another-claim")
    canceled = checkpoints.cancel_restore(boundary_id, claim_id=claim_id)
    assert canceled["phase"] == "CANCELED"
    assert canceled["decision"] == claimed["decision"]
    assert checkpoints.cancel_restore(boundary_id, claim_id=claim_id) == canceled
    with pytest.raises(ValueError, match="not recoverable"):
        checkpoints.begin_restore(boundary_id)
