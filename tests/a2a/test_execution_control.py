from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path

import pytest

from iac_code.a2a.backup import run_sync_fenced, run_sync_fenced_with_cancel_completion
from iac_code.a2a.execution_control import (
    ExecutionControlConflictError,
    ExecutionController,
    ExecutionControlNotFoundError,
    ExecutionControlService,
    bind_execution_control,
    execution_activity,
    execution_checkpoint,
    execution_non_advancing_wait,
    reset_execution_control,
    run_with_execution_budget,
)
from iac_code.services.session_backup import BackupReason, BackupResult
from iac_code.services.session_storage import SessionStorage


async def _wait_for_phase(control: ExecutionController, phase: str, *, timeout: float = 5) -> None:
    async def wait() -> None:
        while control.phase != phase:
            await asyncio.sleep(0.005)

    await asyncio.wait_for(wait(), timeout=timeout)


async def _wait_for_condition(predicate, *, timeout: float = 5) -> None:
    async def wait() -> None:
        while not predicate():
            await asyncio.sleep(0.005)

    await asyncio.wait_for(wait(), timeout=timeout)


def _controller(tmp_path: Path, *, backup_service=None) -> ExecutionController:
    return ExecutionController(
        context_id="ctx-1",
        task_id="task-1",
        owner="owner-1",
        cwd=str(tmp_path),
        server_instance_id="instance-1",
        persistence_path=tmp_path / "control.json",
        backup_service=backup_service,
        execution_id="exec-1",
    )


@pytest.mark.asyncio
async def test_resume_during_long_tool_does_not_wait_for_tool(tmp_path: Path) -> None:
    control = _controller(tmp_path)
    tool_started = asyncio.Event()
    release_tool = asyncio.Event()

    async def worker() -> None:
        token = bind_execution_control(control)
        current = asyncio.current_task()
        assert current is not None
        await control.attach_task(current)
        try:
            await execution_checkpoint()
            async with execution_activity("tool_batch"):
                tool_started.set()
                await release_tool.wait()
            await execution_checkpoint()
        finally:
            await control.detach_task(current, execution_status="input-required")
            reset_execution_control(token)

    worker_task = asyncio.create_task(worker())
    await tool_started.wait()
    pause = await control.pause(
        task_id="task-1",
        expected_execution_id="exec-1",
        request_id="request-pause",
        connection_epoch=1,
        reason="client_disconnected",
        reconnect_timeout_seconds=10,
    )
    assert pause["phase"] == "pausing"
    assert {item["kind"] for item in pause["blockers"]} == {"execution", "tool_batch"}

    resumed = await control.resume(
        execution_id="exec-1",
        pause_id=pause["pauseId"],
        request_id="request-resume",
        connection_epoch=2,
    )
    assert resumed["phase"] == "resuming"
    await _wait_for_phase(control, "running")
    assert not worker_task.done()

    release_tool.set()
    await worker_task
    await control.close()


@pytest.mark.asyncio
async def test_resume_is_ordered_after_inflight_paused_commit(tmp_path: Path, monkeypatch) -> None:
    control = _controller(tmp_path)
    paused_write_started = threading.Event()
    allow_paused_write = threading.Event()
    from iac_code.a2a import execution_control as module

    original_write = module.atomic_write_json

    def delayed_write(path, value) -> None:
        if value["phase"] == "paused":
            paused_write_started.set()
            assert allow_paused_write.wait(timeout=2)
        original_write(path, value)

    monkeypatch.setattr(module, "atomic_write_json", delayed_write)
    pause = await control.pause(
        task_id="task-1",
        expected_execution_id="exec-1",
        request_id="request-pause",
        connection_epoch=1,
        reason="client_disconnected",
        reconnect_timeout_seconds=10,
    )
    assert await asyncio.to_thread(paused_write_started.wait, 2)

    resumed = await control.resume(
        execution_id="exec-1",
        pause_id=pause["pauseId"],
        request_id="request-resume",
        connection_epoch=2,
    )
    assert resumed["phase"] == "resuming"
    with pytest.raises(ExecutionControlConflictError, match="resuming"):
        await control.pause(
            task_id="task-1",
            expected_execution_id="exec-1",
            request_id="new-pause-while-resuming",
            connection_epoch=3,
            reason="client_disconnected",
            reconnect_timeout_seconds=10,
        )
    allow_paused_write.set()
    await _wait_for_phase(control, "running")
    await _wait_for_condition(
        lambda: json.loads((tmp_path / "control.json").read_text(encoding="utf-8"))["phase"] == "running"
    )
    persisted = json.loads((tmp_path / "control.json").read_text(encoding="utf-8"))
    assert persisted["revision"] == control.revision
    assert persisted["phase"] == "running"
    await control.close()


@pytest.mark.asyncio
async def test_external_operation_commit_cannot_strand_resume_barrier(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    control = _controller(tmp_path)
    control.bind_session("session-1")
    running_write_started = threading.Event()
    allow_running_write = threading.Event()
    from iac_code.a2a import execution_control as module

    original_write = module.atomic_write_json

    def delayed_write(path, value) -> None:
        if value.get("phase") == "running" and not running_write_started.is_set():
            running_write_started.set()
            assert allow_running_write.wait(timeout=2)
        original_write(path, value)

    monkeypatch.setattr(module, "atomic_write_json", delayed_write)
    pause = await control.pause(
        task_id="task-1",
        expected_execution_id="exec-1",
        request_id="request-pause",
        connection_epoch=1,
        reason="client_disconnected",
        reconnect_timeout_seconds=10,
    )
    await _wait_for_phase(control, "paused")
    await control.resume(
        execution_id="exec-1",
        pause_id=pause["pauseId"],
        request_id="request-resume",
        connection_epoch=2,
    )
    assert await asyncio.to_thread(running_write_started.wait, 2)
    resume_revision = control.revision

    operation_commit = asyncio.create_task(
        control.record_external_operation(
            product="ros",
            action="CreateStack",
            outcome="accepted",
            resource_type="stack",
            resource_id="stack-late",
            region_id="cn-hangzhou",
        )
    )
    await _wait_for_condition(lambda: control.revision > resume_revision)
    allow_running_write.set()
    await operation_commit
    await _wait_for_phase(control, "running")
    await _wait_for_condition(
        lambda: json.loads((tmp_path / "control.json").read_text(encoding="utf-8"))["phase"] == "running"
    )
    assert control.snapshot()["externalOperations"][0]["resourceId"] == "stack-late"
    await control.close()


@pytest.mark.asyncio
async def test_immediate_resume_cannot_make_unscheduled_pause_commit_write_new_revision(
    tmp_path: Path,
    monkeypatch,
) -> None:
    control = _controller(tmp_path)
    from iac_code.a2a import execution_control as module

    writes: list[tuple[int, str]] = []
    original_write = module.atomic_write_json

    def record_write(path, value) -> None:
        writes.append((value["revision"], value["phase"]))
        original_write(path, value)

    monkeypatch.setattr(module, "atomic_write_json", record_write)
    pause = await control.pause(
        task_id="task-1",
        expected_execution_id="exec-1",
        request_id="request-pause",
        connection_epoch=1,
        reason="client_disconnected",
        reconnect_timeout_seconds=10,
    )
    await control.resume(
        execution_id="exec-1",
        pause_id=pause["pauseId"],
        request_id="request-resume",
        connection_epoch=2,
    )
    await _wait_for_phase(control, "running")
    await _wait_for_condition(lambda: control.persisted_revision == control.revision)

    persisted = json.loads((tmp_path / "control.json").read_text(encoding="utf-8"))
    assert persisted["phase"] == "running"
    assert (control.revision, "paused") not in writes
    await control.close()


@pytest.mark.asyncio
async def test_canceling_commit_wait_does_not_release_writer_lock_before_thread_finishes(
    tmp_path: Path,
    monkeypatch,
) -> None:
    control = _controller(tmp_path)
    paused_write_started = threading.Event()
    allow_paused_write = threading.Event()
    from iac_code.a2a import execution_control as module

    original_write = module.atomic_write_json

    def delayed_write(path, value) -> None:
        if value["phase"] == "paused":
            paused_write_started.set()
            assert allow_paused_write.wait(timeout=2)
        original_write(path, value)

    monkeypatch.setattr(module, "atomic_write_json", delayed_write)
    pause = await control.pause(
        task_id="task-1",
        expected_execution_id="exec-1",
        request_id="request-pause",
        connection_epoch=1,
        reason="client_disconnected",
        reconnect_timeout_seconds=10,
    )
    assert await asyncio.to_thread(paused_write_started.wait, 2)
    pause_commit = next(task for task in control._background_tasks if "pause-commit" in task.get_name())
    pause_commit.cancel()

    await control.resume(
        execution_id="exec-1",
        pause_id=pause["pauseId"],
        request_id="request-resume",
        connection_epoch=2,
    )
    await asyncio.sleep(0.02)
    assert control.phase == "resuming"

    allow_paused_write.set()
    await _wait_for_phase(control, "running")
    await _wait_for_condition(lambda: control.persisted_revision == control.revision)
    assert json.loads((tmp_path / "control.json").read_text(encoding="utf-8"))["phase"] == "running"
    await control.close()


@pytest.mark.asyncio
async def test_participant_leaving_safe_wait_invalidates_inflight_pause_commit(tmp_path: Path, monkeypatch) -> None:
    control = _controller(tmp_path)
    paused_write_started = threading.Event()
    allow_paused_write = threading.Event()
    waiting = asyncio.Event()
    leave_wait = asyncio.Event()
    processing = asyncio.Event()
    finish_processing = asyncio.Event()
    from iac_code.a2a import execution_control as module

    original_write = module.atomic_write_json

    def delayed_write(path, value) -> None:
        if value["phase"] == "paused" and not paused_write_started.is_set():
            paused_write_started.set()
            assert allow_paused_write.wait(timeout=2)
        original_write(path, value)

    monkeypatch.setattr(module, "atomic_write_json", delayed_write)

    async def worker() -> None:
        token = bind_execution_control(control)
        current = asyncio.current_task()
        assert current is not None
        await control.attach_task(current)
        try:
            async with execution_non_advancing_wait():
                waiting.set()
                await leave_wait.wait()
            processing.set()
            await finish_processing.wait()
            await execution_checkpoint()
        finally:
            await control.detach_task(current, execution_status="input-required")
            reset_execution_control(token)

    worker_task = asyncio.create_task(worker())
    await waiting.wait()
    pause = await control.pause(
        task_id="task-1",
        expected_execution_id="exec-1",
        request_id="request-pause",
        connection_epoch=1,
        reason="client_disconnected",
        reconnect_timeout_seconds=10,
    )
    assert await asyncio.to_thread(paused_write_started.wait, 2)

    leave_wait.set()
    await processing.wait()
    assert control.phase == "pausing"
    allow_paused_write.set()
    await _wait_for_condition(
        lambda: json.loads((tmp_path / "control.json").read_text(encoding="utf-8"))["phase"] == "pausing"
    )

    finish_processing.set()
    await _wait_for_phase(control, "paused")
    await control.resume(
        execution_id="exec-1",
        pause_id=pause["pauseId"],
        request_id="request-resume",
        connection_epoch=2,
    )
    await worker_task
    await control.close()


@pytest.mark.asyncio
async def test_descendant_pause_does_not_consume_ancestor_tool_budget(tmp_path: Path) -> None:
    control = _controller(tmp_path)
    descendant_at_checkpoint = asyncio.Event()

    async def descendant() -> str:
        descendant_at_checkpoint.set()
        await execution_checkpoint()
        await asyncio.sleep(0.01)
        return "same-task-finished"

    async def worker() -> str:
        token = bind_execution_control(control)
        current = asyncio.current_task()
        assert current is not None
        await control.attach_task(current)
        try:
            await execution_checkpoint()
            async with execution_activity("tool_batch"):
                async with execution_activity("tool", check_gate=False):
                    return await run_with_execution_budget(descendant(), timeout=0.05)
        finally:
            await control.detach_task(current, execution_status="input-required")
            reset_execution_control(token)

    worker_task = asyncio.create_task(worker())
    await descendant_at_checkpoint.wait()
    pause = await control.pause(
        task_id="task-1",
        expected_execution_id="exec-1",
        request_id="request-pause",
        connection_epoch=1,
        reason="client_disconnected",
        reconnect_timeout_seconds=10,
    )
    await _wait_for_phase(control, "paused")
    await asyncio.sleep(0.1)
    await control.resume(
        execution_id="exec-1",
        pause_id=pause["pauseId"],
        request_id="request-resume",
        connection_epoch=2,
    )
    assert await worker_task == "same-task-finished"
    await control.close()


@pytest.mark.asyncio
async def test_unblocked_parallel_descendant_keeps_ancestor_budget_running(tmp_path: Path) -> None:
    control = _controller(tmp_path)
    child_at_checkpoint = asyncio.Event()
    sibling_started = asyncio.Event()

    async def paused_child() -> None:
        child_at_checkpoint.set()
        await execution_checkpoint()

    async def running_sibling() -> None:
        async with execution_activity("tool", check_gate=False):
            sibling_started.set()
            await asyncio.Event().wait()

    async def worker() -> None:
        token = bind_execution_control(control)
        current = asyncio.current_task()
        assert current is not None
        await control.attach_task(current)
        try:
            async with execution_activity("tool_batch"):
                async with execution_activity("tool", check_gate=False):
                    await run_with_execution_budget(
                        asyncio.gather(paused_child(), running_sibling()),
                        timeout=0.05,
                    )
        finally:
            await control.detach_task(current, execution_status="canceled")
            reset_execution_control(token)

    worker_task = asyncio.create_task(worker())
    await child_at_checkpoint.wait()
    await sibling_started.wait()
    pause = await control.pause(
        task_id="task-1",
        expected_execution_id="exec-1",
        request_id="request-pause",
        connection_epoch=1,
        reason="client_disconnected",
        reconnect_timeout_seconds=10,
    )
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(worker_task, timeout=0.5)

    await control.resume(
        execution_id="exec-1",
        pause_id=pause["pauseId"],
        request_id="request-resume",
        connection_epoch=2,
    )
    await _wait_for_phase(control, "running")
    await control.close()


@pytest.mark.asyncio
async def test_existing_business_wait_is_a_pause_safe_point(tmp_path: Path) -> None:
    control = _controller(tmp_path)
    waiting = asyncio.Event()
    release = asyncio.Event()

    async def worker() -> None:
        token = bind_execution_control(control)
        current = asyncio.current_task()
        assert current is not None
        await control.attach_task(current)
        try:
            async with execution_non_advancing_wait():
                waiting.set()
                await release.wait()
        finally:
            await control.detach_task(current, execution_status="input-required")
            reset_execution_control(token)

    task = asyncio.create_task(worker())
    await waiting.wait()
    pause = await control.pause(
        task_id="task-1",
        expected_execution_id="exec-1",
        request_id="request-pause",
        connection_epoch=1,
        reason="client_disconnected",
        reconnect_timeout_seconds=10,
    )
    await _wait_for_phase(control, "paused")
    await control.resume(
        execution_id="exec-1",
        pause_id=pause["pauseId"],
        request_id="request-resume",
        connection_epoch=2,
    )
    await _wait_for_phase(control, "running")
    release.set()
    await task
    await control.close()


@pytest.mark.asyncio
async def test_child_checkpoint_does_not_mark_its_running_parent_safe(tmp_path: Path) -> None:
    control = _controller(tmp_path)
    child_ready = asyncio.Event()
    let_child_checkpoint = asyncio.Event()
    parent_processing_done = asyncio.Event()

    async def child() -> None:
        child_ready.set()
        await let_child_checkpoint.wait()
        await execution_checkpoint()

    async def parent() -> None:
        token = bind_execution_control(control)
        current = asyncio.current_task()
        assert current is not None
        await control.attach_task(current)
        child_task = asyncio.create_task(child())
        try:
            await child_ready.wait()
            await parent_processing_done.wait()
            await execution_checkpoint()
            await child_task
        finally:
            if not child_task.done():
                child_task.cancel()
                await asyncio.gather(child_task, return_exceptions=True)
            await control.detach_task(current, execution_status="input-required")
            reset_execution_control(token)

    parent_task = asyncio.create_task(parent())
    await child_ready.wait()
    pause = await control.pause(
        task_id="task-1",
        expected_execution_id="exec-1",
        request_id="request-pause",
        connection_epoch=1,
        reason="client_disconnected",
        reconnect_timeout_seconds=10,
    )
    let_child_checkpoint.set()
    await _wait_for_condition(lambda: any(participant.safe for participant in control._participants.values()))
    await asyncio.sleep(0.02)
    assert control.phase == "pausing"

    parent_processing_done.set()
    await _wait_for_phase(control, "paused")
    await control.resume(
        execution_id="exec-1",
        pause_id=pause["pauseId"],
        request_id="request-resume",
        connection_epoch=2,
    )
    await parent_task
    await control.close()


@pytest.mark.asyncio
async def test_terminate_cancels_local_execution_and_waits_for_shared_backup(tmp_path: Path) -> None:
    class BackupService:
        def __init__(self) -> None:
            self.reasons: list[BackupReason] = []

        def backup_session(self, cwd, session_id, *, reason, critical) -> BackupResult:
            self.reasons.append(reason)
            return BackupResult(
                enabled=True,
                generation=3,
                commit_id="commit-3",
                shared_committed=True,
            )

    backup = BackupService()
    control = _controller(tmp_path, backup_service=backup)
    control.bind_session("session-1")
    started = asyncio.Event()

    async def worker() -> None:
        token = bind_execution_control(control)
        current = asyncio.current_task()
        assert current is not None
        await control.attach_task(current)
        try:
            async with execution_activity("tool_batch"):
                started.set()
                await asyncio.Event().wait()
        finally:
            await control.detach_task(current, execution_status="canceled")
            reset_execution_control(token)

    worker_task = asyncio.create_task(worker())
    await started.wait()
    pause = await control.pause(
        task_id="task-1",
        expected_execution_id="exec-1",
        request_id="request-pause",
        connection_epoch=1,
        reason="client_disconnected",
        reconnect_timeout_seconds=10,
    )
    state = await control.terminate(
        execution_id="exec-1",
        request_id="request-terminate",
        connection_epoch=2,
        reason="disconnect_timeout",
        pause_id=pause["pauseId"],
    )
    assert state["phase"] == "terminating"
    await _wait_for_phase(control, "terminated")
    await _wait_for_condition(lambda: control.release_ready)
    assert worker_task.cancelled()
    assert backup.reasons == [BackupReason.DISCONNECT_TIMEOUT]
    assert control.snapshot()["backup"]["status"] == "shared_committed"
    await control.close()


@pytest.mark.asyncio
async def test_terminate_does_not_finish_while_managed_sync_work_is_still_running(tmp_path: Path) -> None:
    control = _controller(tmp_path)
    sync_started = threading.Event()
    release_sync = threading.Event()

    def blocking_sync_call() -> None:
        sync_started.set()
        release_sync.wait(timeout=2)

    async def worker() -> None:
        token = bind_execution_control(control)
        current = asyncio.current_task()
        assert current is not None
        await control.attach_task(current)
        try:
            async with execution_activity("tool_batch"):
                await run_sync_fenced(blocking_sync_call)
        finally:
            await control.detach_task(current, execution_status="canceled")
            reset_execution_control(token)

    worker_task = asyncio.create_task(worker())
    assert await asyncio.to_thread(sync_started.wait, 2)
    state = await control.terminate(
        execution_id="exec-1",
        request_id="request-terminate",
        connection_epoch=1,
        reason="explicit_terminate",
    )
    assert state["phase"] == "terminating"
    await asyncio.sleep(0.02)
    assert control.phase == "terminating"
    assert control.release_ready is False

    release_sync.set()
    await _wait_for_phase(control, "terminated")
    await _wait_for_condition(lambda: control.release_ready)
    assert worker_task.done()
    await control.close()


@pytest.mark.asyncio
async def test_budget_wrapper_drains_fenced_tool_before_termination_finishes(tmp_path: Path) -> None:
    control = _controller(tmp_path)
    sync_started = threading.Event()
    release_sync = threading.Event()

    def blocking_sync_call() -> None:
        sync_started.set()
        release_sync.wait(timeout=2)

    async def worker() -> None:
        token = bind_execution_control(control)
        current = asyncio.current_task()
        assert current is not None
        await control.attach_task(current)
        try:
            async with execution_activity("tool_batch"):
                async with execution_activity("tool", check_gate=False):
                    await run_with_execution_budget(run_sync_fenced(blocking_sync_call), timeout=30)
        finally:
            await control.detach_task(current, execution_status="canceled")
            reset_execution_control(token)

    worker_task = asyncio.create_task(worker())
    assert await asyncio.to_thread(sync_started.wait, 2)
    await control.terminate(
        execution_id="exec-1",
        request_id="request-terminate",
        connection_epoch=1,
        reason="explicit_terminate",
    )
    await asyncio.sleep(0.02)
    assert control.phase == "terminating"
    assert control.release_ready is False
    assert worker_task.done() is False

    release_sync.set()
    await _wait_for_condition(lambda: control.release_ready)
    assert worker_task.done()
    await control.close()


@pytest.mark.asyncio
async def test_terminate_persists_sync_write_result_that_arrives_during_cancellation(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    control = _controller(tmp_path)
    control.bind_session("session-1")
    sync_started = threading.Event()
    release_sync = threading.Event()

    def create_stack() -> str:
        sync_started.set()
        release_sync.wait(timeout=2)
        return "stack-created-during-cancel"

    async def record_result(result: str | None, error: BaseException | None) -> None:
        await control.record_external_operation(
            product="ros",
            action="CreateStack",
            outcome="accepted" if error is None else "unknown",
            resource_type="stack",
            resource_id=result,
            region_id="cn-hangzhou",
            tool_use_id="tool-1",
        )

    async def worker() -> None:
        token = bind_execution_control(control)
        current = asyncio.current_task()
        assert current is not None
        await control.attach_task(current)
        try:
            async with execution_activity("tool_batch"):
                await run_sync_fenced_with_cancel_completion(create_stack, record_result)
        finally:
            await control.detach_task(current, execution_status="canceled")
            reset_execution_control(token)

    worker_task = asyncio.create_task(worker())
    assert await asyncio.to_thread(sync_started.wait, 2)
    await control.terminate(
        execution_id="exec-1",
        request_id="request-terminate",
        connection_epoch=1,
        reason="explicit_terminate",
    )
    await asyncio.sleep(0.02)
    assert control.phase == "terminating"

    release_sync.set()
    await _wait_for_condition(lambda: control.release_ready)
    assert worker_task.cancelled()
    assert control.snapshot()["externalOperations"] == [
        {
            "product": "ros",
            "action": "CreateStack",
            "outcome": "accepted",
            "resourceType": "stack",
            "resourceId": "stack-created-during-cancel",
            "regionId": "cn-hangzhou",
            "toolUseId": "tool-1",
        }
    ]
    operation_path = SessionStorage().session_dir(str(tmp_path), "session-1") / "a2a" / "external-operations.json"
    document = json.loads(operation_path.read_text(encoding="utf-8"))
    assert document["operations"][0]["resourceId"] == "stack-created-during-cancel"
    await control.close()


@pytest.mark.asyncio
async def test_terminate_cancels_registered_background_participant_in_passive_wait(tmp_path: Path) -> None:
    control = _controller(tmp_path)
    waiting = asyncio.Event()

    async def background_participant() -> None:
        token = bind_execution_control(control)
        try:
            async with execution_non_advancing_wait():
                waiting.set()
                await asyncio.Event().wait()
        finally:
            reset_execution_control(token)

    participant = asyncio.create_task(background_participant())
    await waiting.wait()
    await control.terminate(
        execution_id="exec-1",
        request_id="request-terminate",
        connection_epoch=1,
        reason="explicit_terminate",
    )
    await _wait_for_phase(control, "terminated")
    await _wait_for_condition(lambda: control.release_ready)
    assert participant.cancelled()
    await control.close()


@pytest.mark.asyncio
async def test_staged_backup_does_not_release_until_shared_retry_succeeds(tmp_path: Path) -> None:
    class StagedBackupService:
        def __init__(self) -> None:
            self.wait_calls = 0

        def backup_session(self, cwd, session_id, *, reason, critical) -> BackupResult:
            return BackupResult(
                enabled=True,
                generation=7,
                commit_id="commit-7",
                staged_committed=True,
                shared_committed=False,
            )

        def wait_until_shared_committed(self, cwd, session_id, *, generation, commit_id) -> BackupResult:
            self.wait_calls += 1
            return BackupResult(
                enabled=True,
                generation=generation,
                commit_id=commit_id,
                succeeded=self.wait_calls > 1,
                staged_committed=True,
                shared_committed=self.wait_calls > 1,
                error=None if self.wait_calls > 1 else "shared unavailable",
            )

    backup = StagedBackupService()
    control = _controller(tmp_path, backup_service=backup)
    control.bind_session("session-1")
    request = {
        "execution_id": "exec-1",
        "request_id": "request-terminate",
        "connection_epoch": 1,
        "reason": "explicit_terminate",
    }

    await control.terminate(**request)
    await _wait_for_condition(lambda: control.backup["status"] == "blocked")
    assert control.phase == "terminated"
    assert control.release_ready is False

    await control.terminate(**request)
    await _wait_for_condition(lambda: control.release_ready)
    assert control.backup["status"] == "shared_committed"
    assert backup.wait_calls == 2
    await control.close()


@pytest.mark.asyncio
async def test_external_terminate_retries_backup_after_automatic_deadline(tmp_path: Path) -> None:
    class RetryBackupService:
        def __init__(self) -> None:
            self.calls = 0

        def backup_session(self, cwd, session_id, *, reason, critical) -> BackupResult:
            self.calls += 1
            return BackupResult(
                enabled=True,
                generation=self.calls,
                commit_id=f"commit-{self.calls}",
                succeeded=self.calls > 1,
                shared_committed=self.calls > 1,
                error=None if self.calls > 1 else "shared unavailable",
            )

    backup = RetryBackupService()
    control = _controller(tmp_path, backup_service=backup)
    control.bind_session("session-1")
    pause = await control.pause(
        task_id="task-1",
        expected_execution_id="exec-1",
        request_id="request-pause",
        connection_epoch=1,
        reason="client_disconnected",
        reconnect_timeout_seconds=10,
    )
    generation = control._pause_generation

    await control._deadline(generation, pause["pauseId"], 0)
    assert control.phase == "terminated"
    assert control.backup["status"] == "blocked"
    assert backup.calls == 1

    await control.terminate(
        execution_id="exec-1",
        request_id="request-terminate-retry",
        connection_epoch=2,
        reason="disconnect_timeout",
        pause_id=pause["pauseId"],
    )
    await _wait_for_condition(lambda: control.release_ready)
    assert backup.calls == 2
    assert control.backup["status"] == "shared_committed"
    await control.close()


@pytest.mark.asyncio
async def test_late_disconnect_timeout_cannot_terminate_resumed_execution(tmp_path: Path) -> None:
    control = _controller(tmp_path)
    pause = await control.pause(
        task_id="task-1",
        expected_execution_id="exec-1",
        request_id="request-pause",
        connection_epoch=1,
        reason="client_disconnected",
        reconnect_timeout_seconds=10,
    )
    await control.resume(
        execution_id="exec-1",
        pause_id=pause["pauseId"],
        request_id="request-resume",
        connection_epoch=1,
    )
    await _wait_for_phase(control, "running")

    with pytest.raises(ExecutionControlConflictError, match="no longer identifies"):
        await control.terminate(
            execution_id="exec-1",
            request_id="late-timeout",
            connection_epoch=1,
            reason="disconnect_timeout",
            pause_id=pause["pauseId"],
        )
    assert control.phase == "running"
    await control.close()


@pytest.mark.asyncio
async def test_terminate_retry_can_recommit_successful_backup_state_without_rerunning_backup(
    tmp_path: Path,
    monkeypatch,
) -> None:
    control = _controller(tmp_path)
    from iac_code.a2a import execution_control as module

    failed_once = False
    original_write = module.atomic_write_json

    def fail_first_backup_state(path, value) -> None:
        nonlocal failed_once
        if not failed_once and value["phase"] == "terminated" and value["backup"]["status"] == "disabled":
            failed_once = True
            raise OSError("disk temporarily unavailable")
        original_write(path, value)

    monkeypatch.setattr(module, "atomic_write_json", fail_first_backup_state)
    request = {
        "execution_id": "exec-1",
        "request_id": "request-terminate",
        "connection_epoch": 1,
        "reason": "explicit_terminate",
    }

    await control.terminate(**request)
    await _wait_for_condition(lambda: control.snapshot()["commitError"] == "state_commit_failed")
    assert control.release_ready is False

    await control.terminate(**request)
    await _wait_for_condition(lambda: control.release_ready)
    persisted = json.loads((tmp_path / "control.json").read_text(encoding="utf-8"))
    assert persisted["releaseReady"] is True
    assert persisted["commitError"] is None
    await control.close()


@pytest.mark.asyncio
async def test_idempotency_and_stale_connection_epoch(tmp_path: Path) -> None:
    control = _controller(tmp_path)
    pause = await control.pause(
        task_id="task-1",
        expected_execution_id="exec-1",
        request_id="request-pause",
        connection_epoch=5,
        reason="client_disconnected",
        reconnect_timeout_seconds=10,
    )
    duplicate = await control.pause(
        task_id="task-1",
        expected_execution_id="exec-1",
        request_id="request-pause",
        connection_epoch=5,
        reason="client_disconnected",
        reconnect_timeout_seconds=10,
    )
    assert duplicate["pauseId"] == pause["pauseId"]

    with pytest.raises(ExecutionControlConflictError, match="different content"):
        await control.pause(
            task_id="task-1",
            expected_execution_id="exec-1",
            request_id="request-pause",
            connection_epoch=5,
            reason="different",
            reconnect_timeout_seconds=10,
        )
    with pytest.raises(ExecutionControlConflictError, match="stale"):
        await control.resume(
            execution_id="exec-1",
            pause_id=pause["pauseId"],
            request_id="request-resume",
            connection_epoch=4,
        )
    pause_generation = control._pause_generation
    await control.resume(
        execution_id="exec-1",
        pause_id=pause["pauseId"],
        request_id="request-resume-current",
        connection_epoch=6,
    )
    await _wait_for_phase(control, "running")
    await control._deadline(pause_generation, pause["pauseId"], 0)
    assert control.phase == "running"
    await control.close()


@pytest.mark.asyncio
async def test_new_normal_turn_gets_new_execution_identity_but_pipeline_continuation_reuses_it(tmp_path: Path) -> None:
    service = ExecutionControlService(persistence_root=tmp_path, backup_service=None)

    first = await service.begin_execution(context_id="ctx-1", task_id="task-1", owner="owner-1", cwd=str(tmp_path))
    current = asyncio.current_task()
    assert current is not None
    pause = await first.pause(
        task_id="task-1",
        expected_execution_id=first.execution_id,
        request_id="pause-old-turn",
        connection_epoch=1,
        reason="client_disconnected",
        reconnect_timeout_seconds=10,
    )
    await first.resume(
        execution_id=first.execution_id,
        pause_id=pause["pauseId"],
        request_id="resume-old-turn",
        connection_epoch=2,
    )
    await _wait_for_phase(first, "running")
    await first.detach_task(current, execution_status="input-required")

    pipeline_continuation = await service.begin_execution(
        context_id="ctx-1",
        task_id="task-1",
        owner="owner-1",
        cwd=str(tmp_path),
        continue_input_required=True,
    )
    assert pipeline_continuation is first
    await pipeline_continuation.detach_task(current, execution_status="input-required")

    next_normal_turn = await service.begin_execution(
        context_id="ctx-1",
        task_id="task-1",
        owner="owner-1",
        cwd=str(tmp_path),
    )
    assert next_normal_turn is not first
    assert next_normal_turn.execution_id != first.execution_id
    assert all(task.done() for task in first._background_tasks)
    await next_normal_turn.detach_task(current, execution_status="input-required")
    await service.close()


@pytest.mark.asyncio
async def test_new_normal_turn_retains_and_controls_previous_turn_background_agent(tmp_path: Path) -> None:
    service = ExecutionControlService(persistence_root=tmp_path, backup_service=None)
    control = await service.begin_execution(
        context_id="ctx-1",
        task_id="task-1",
        owner="owner-1",
        cwd=str(tmp_path),
    )
    token = bind_execution_control(control)
    current = asyncio.current_task()
    assert current is not None
    background_started = asyncio.Event()

    async def background_agent() -> None:
        async with execution_activity("background_agent"):
            background_started.set()
            await asyncio.Event().wait()

    background = asyncio.create_task(background_agent())
    await background_started.wait()
    old_execution_id = control.execution_id
    await control.detach_task(current, execution_status="input-required")
    assert service.has_active_work() is True

    next_turn = await service.begin_execution(
        context_id="ctx-1",
        task_id="task-2",
        owner="owner-1",
        cwd=str(tmp_path),
    )
    assert next_turn is control
    assert next_turn.execution_id != old_execution_id
    assert next_turn.task_id == "task-2"
    assert background.done() is False

    await next_turn.detach_task(current, execution_status="input-required")
    await next_turn.terminate(
        execution_id=next_turn.execution_id,
        request_id="terminate-next-turn",
        connection_epoch=1,
        reason="explicit_terminate",
    )
    await _wait_for_condition(lambda: next_turn.release_ready)
    assert background.cancelled()
    reset_execution_control(token)
    await service.close()


@pytest.mark.asyncio
async def test_paused_context_rejects_new_task_without_replacing_background_agent(tmp_path: Path) -> None:
    service = ExecutionControlService(persistence_root=tmp_path, backup_service=None)
    control = await service.begin_execution(
        context_id="ctx-1",
        task_id="task-1",
        owner="owner-1",
        cwd=str(tmp_path),
    )
    token = bind_execution_control(control)
    current = asyncio.current_task()
    assert current is not None
    background_started = asyncio.Event()
    enter_checkpoint = asyncio.Event()
    background_resumed = asyncio.Event()
    release_background = asyncio.Event()

    async def background_agent() -> None:
        async with execution_activity("background_agent"):
            background_started.set()
            await enter_checkpoint.wait()
            await execution_checkpoint()
            background_resumed.set()
            await release_background.wait()

    background = asyncio.create_task(background_agent())
    await background_started.wait()
    await control.detach_task(current, execution_status="normal-turn-ended")
    pause = await control.pause(
        task_id="task-1",
        expected_execution_id=control.execution_id,
        request_id="pause-background",
        connection_epoch=1,
        reason="client_disconnected",
        reconnect_timeout_seconds=30,
    )
    enter_checkpoint.set()
    await _wait_for_phase(control, "paused")

    with pytest.raises(ExecutionControlConflictError):
        await service.begin_execution(
            context_id="ctx-1",
            task_id="task-2",
            owner="owner-1",
            cwd=str(tmp_path),
        )
    assert service.get_for_context("ctx-1") is control
    assert background.done() is False

    await control.resume(
        execution_id=control.execution_id,
        pause_id=pause["pauseId"],
        request_id="resume-background",
        connection_epoch=2,
    )
    await asyncio.wait_for(background_resumed.wait(), timeout=2)
    release_background.set()
    await background
    reset_execution_control(token)
    await service.close()


@pytest.mark.asyncio
async def test_finished_tool_hands_safety_back_to_parent_until_result_checkpoint(tmp_path: Path) -> None:
    control = _controller(tmp_path)
    tool_started = asyncio.Event()
    release_tool = asyncio.Event()
    result_recorded = asyncio.Event()
    allow_result_checkpoint = asyncio.Event()

    async def tool_batch() -> str:
        async with execution_activity("tool_batch", handoff_to_parent=True):
            async with execution_non_advancing_wait():
                async with execution_activity("tool", check_gate=False, handoff_to_parent=True):
                    tool_started.set()
                    await release_tool.wait()
                    return "durable-result"

    async def worker() -> None:
        token = bind_execution_control(control)
        current = asyncio.current_task()
        assert current is not None
        await control.attach_task(current)
        try:
            child = asyncio.create_task(tool_batch())
            async with execution_non_advancing_wait():
                assert await child == "durable-result"
            result_recorded.set()
            await allow_result_checkpoint.wait()
            await execution_checkpoint()
        finally:
            await control.detach_task(current, execution_status="input-required")
            reset_execution_control(token)

    worker_task = asyncio.create_task(worker())
    await tool_started.wait()
    pause = await control.pause(
        task_id="task-1",
        expected_execution_id="exec-1",
        request_id="pause-tool-result",
        connection_epoch=1,
        reason="client_disconnected",
        reconnect_timeout_seconds=30,
    )
    assert control.phase == "pausing"
    release_tool.set()
    await asyncio.wait_for(result_recorded.wait(), timeout=2)
    await asyncio.sleep(0.02)
    assert control.phase == "pausing"

    allow_result_checkpoint.set()
    await _wait_for_phase(control, "paused")
    await control.resume(
        execution_id="exec-1",
        pause_id=pause["pauseId"],
        request_id="resume-tool-result",
        connection_epoch=2,
    )
    await worker_task
    await control.close()


def test_backup_blocked_terminated_execution_still_counts_as_active_work(tmp_path: Path) -> None:
    service = ExecutionControlService(persistence_root=tmp_path, backup_service=None)
    control = _controller(tmp_path)
    service._controls["ctx-1"] = control

    control.phase = "terminated"
    control.release_ready = False
    assert service.has_active_work() is True

    control.release_ready = True
    assert service.has_active_work() is False


@pytest.mark.asyncio
async def test_new_execution_cannot_replace_another_owner_control(tmp_path: Path) -> None:
    service = ExecutionControlService(persistence_root=tmp_path, backup_service=None)
    first = await service.begin_execution(context_id="ctx-1", task_id="task-1", owner="owner-1", cwd=str(tmp_path))
    current = asyncio.current_task()
    assert current is not None
    await first.detach_task(current, execution_status="input-required")

    with pytest.raises(ExecutionControlNotFoundError):
        await service.begin_execution(
            context_id="ctx-1",
            task_id="different-task",
            owner="owner-2",
            cwd=str(tmp_path),
        )
    assert service.get_for_context("ctx-1") is first
    await service.close()


@pytest.mark.asyncio
async def test_termination_cleanup_failure_is_retried_before_backup_release(tmp_path: Path, monkeypatch) -> None:
    calls = 0

    async def cleanup(context_id: str, task_id: str, reason: str) -> str:
        nonlocal calls
        calls += 1
        assert (context_id, task_id, reason) == ("ctx-1", "task-1", "explicit_terminate")
        if calls == 1:
            raise RuntimeError("cleanup temporarily failed")
        return "canceled"

    control = ExecutionController(
        context_id="ctx-1",
        task_id="task-1",
        owner="owner-1",
        cwd=str(tmp_path),
        server_instance_id="instance-1",
        persistence_path=tmp_path / "control.json",
        backup_service=None,
        termination_cleanup=cleanup,
        execution_id="exec-1",
    )
    blocked_committed = asyncio.Event()
    allow_cleanup_failure_to_return = asyncio.Event()
    commit_blocked = control._commit_blocked_backup_state

    async def gated_commit_blocked(message: str, error: BaseException, **kwargs) -> None:
        await commit_blocked(message, error, **kwargs)
        blocked_committed.set()
        await allow_cleanup_failure_to_return.wait()

    monkeypatch.setattr(control, "_commit_blocked_backup_state", gated_commit_blocked)
    request = {
        "execution_id": "exec-1",
        "request_id": "terminate-request",
        "connection_epoch": 1,
        "reason": "explicit_terminate",
    }
    await control.terminate(**request)
    await asyncio.wait_for(blocked_committed.wait(), 3)
    assert control.phase == "terminated" and control.backup["status"] == "blocked"
    assert control.release_ready is False

    await control.terminate(**request)
    allow_cleanup_failure_to_return.set()
    await _wait_for_condition(lambda: control.release_ready)
    assert calls == 2
    assert control.execution_status == "canceled"
    assert control.backup["status"] == "disabled"
    await control.close()


@pytest.mark.asyncio
async def test_termination_cleanup_rechecks_state_after_active_worker_drains(tmp_path: Path) -> None:
    worker_state = "working"
    observed_states: list[str] = []

    async def cleanup(_context_id: str, _task_id: str, _reason: str) -> str | None:
        observed_states.append(worker_state)
        return "canceled" if worker_state == "canceled" else None

    control = ExecutionController(
        context_id="ctx-1",
        task_id="task-1",
        owner="owner-1",
        cwd=str(tmp_path),
        server_instance_id="instance-1",
        persistence_path=tmp_path / "control.json",
        backup_service=None,
        termination_cleanup=cleanup,
        execution_id="exec-1",
    )

    async def worker() -> None:
        nonlocal worker_state
        current = asyncio.current_task()
        assert current is not None
        await control.attach_task(current)
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            worker_state = "canceled"
            raise
        finally:
            await control.detach_task(current, execution_status=worker_state)

    active = asyncio.create_task(worker())
    await _wait_for_condition(control.has_managed_work)
    await control.terminate(
        execution_id="exec-1",
        request_id="terminate-active-worker",
        connection_epoch=1,
        reason="explicit_terminate",
    )
    await _wait_for_condition(lambda: control.release_ready)

    assert active.cancelled()
    assert observed_states == ["working", "canceled"]
    assert control.execution_status == "canceled"
    await control.close()
