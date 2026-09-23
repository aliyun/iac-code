from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path

import pytest

from iac_code.a2a import execution_control as execution_control_module
from iac_code.a2a.backup import (
    NATURAL_HANDOFF_VERSION,
    SessionBackupCoordinator,
    SessionBackupHandoff,
    SessionBackupHandoffError,
    run_sync_fenced,
    run_sync_fenced_with_cancel_completion,
)
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
async def test_natural_business_boundary_self_finalizes_without_terminate_request(
    tmp_path: Path,
) -> None:
    control = _controller(tmp_path)
    current = asyncio.current_task()
    assert current is not None
    await control.attach_task(current)

    completion_generation = await control.detach_task(
        current,
        execution_status="input-required",
        natural_completion=True,
    )

    assert completion_generation is not None
    observed = await control.finalize_natural_completion(
        task_id="task-1",
        completion_generation=completion_generation,
    )
    assert observed["phase"] == "terminated"
    assert observed["terminationReason"] == "natural_completion"
    # The business handoff is published without waiting for the release commit.
    handoff = observed["naturalHandoff"]
    assert handoff["version"] == NATURAL_HANDOFF_VERSION
    assert handoff["executionId"] == "exec-1"
    assert handoff["completionGeneration"] == completion_generation
    assert handoff["businessDrained"] is True
    assert handoff["backupDisabled"] is True
    assert handoff["pendingJobId"] is None
    await _wait_for_condition(lambda: control.release_ready)
    snapshot = control.snapshot()
    assert snapshot["phase"] == "terminated"
    assert snapshot["executionStatus"] == "input-required"
    assert snapshot["terminationReason"] == "natural_completion"
    assert snapshot["backup"] == {"status": "disabled"}
    assert snapshot["connectionEpoch"] == -1
    await control.close()


@pytest.mark.asyncio
async def test_new_execution_waits_for_natural_finalized_cleanup(tmp_path: Path) -> None:
    service = ExecutionControlService(persistence_root=None, backup_service=None)
    control = await service.begin_execution(
        context_id="ctx-1",
        task_id="task-1",
        owner="owner-1",
        cwd=str(tmp_path),
    )
    current = asyncio.current_task()
    assert current is not None
    completion_generation = await control.detach_task(
        current,
        execution_status="completed",
        natural_completion=True,
    )
    assert completion_generation is not None

    cleanup_entered = asyncio.Event()
    release_cleanup = asyncio.Event()

    async def finalized_cleanup() -> None:
        cleanup_entered.set()
        await release_cleanup.wait()

    finalizing = asyncio.create_task(
        service.finalize_natural_completion(
            context_id="ctx-1",
            task_id="task-1",
            owner="owner-1",
            completion_generation=completion_generation,
            finalized_cleanup=finalized_cleanup,
        )
    )
    await cleanup_entered.wait()
    begin_attempted = asyncio.Event()

    async def begin_next_execution() -> ExecutionController:
        begin_attempted.set()
        return await service.begin_execution(
            context_id="ctx-1",
            task_id="task-1",
            owner="owner-1",
            cwd=str(tmp_path),
        )

    beginning = asyncio.create_task(begin_next_execution())
    await begin_attempted.wait()
    assert not beginning.done()

    release_cleanup.set()
    await finalizing
    next_control = await beginning
    assert next_control is not control
    await next_control.detach_task(beginning, execution_status="completed")
    await next_control.close()


@pytest.mark.asyncio
async def test_old_response_boundary_cannot_finalize_newer_same_task_turn(tmp_path: Path) -> None:
    control = _controller(tmp_path)
    current = asyncio.current_task()
    assert current is not None

    await control.attach_task(current)
    first_generation = await control.detach_task(
        current,
        execution_status="input-required",
        natural_completion=True,
    )
    await control.attach_task(current)
    second_generation = await control.detach_task(
        current,
        execution_status="input-required",
        natural_completion=True,
    )

    stale = await control.finalize_natural_completion(
        task_id="task-1",
        completion_generation=first_generation,
    )
    observed = await control.observe_state()

    assert first_generation != second_generation
    assert stale["phase"] == "running"
    assert observed["phase"] == "running"

    settled = await control.finalize_natural_completion(
        task_id="task-1",
        completion_generation=second_generation,
    )
    assert settled["phase"] == "terminated"
    assert settled["terminationReason"] == "natural_completion"
    assert settled["naturalHandoff"]["completionGeneration"] == second_generation
    await _wait_for_condition(lambda: control.release_ready)
    await control.close()


@pytest.mark.asyncio
async def test_explicit_termination_preempts_inflight_natural_cleanup(tmp_path: Path) -> None:
    cleanup_started = asyncio.Event()
    release_cleanup = asyncio.Event()
    reasons: list[str] = []

    async def cleanup(_context_id: str, _task_id: str, reason: str) -> str:
        reasons.append(reason)
        if reason == "natural_completion":
            cleanup_started.set()
            await release_cleanup.wait()
        return "input-required" if reason == "natural_completion" else "canceled"

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
    current = asyncio.current_task()
    assert current is not None
    await control.attach_task(current)
    completion_generation = await control.detach_task(
        current,
        execution_status="input-required",
        natural_completion=True,
    )
    natural = asyncio.create_task(
        control.finalize_natural_completion(
            task_id="task-1",
            completion_generation=completion_generation,
        )
    )
    await asyncio.wait_for(cleanup_started.wait(), timeout=1)

    claimed = await control.terminate(
        execution_id="exec-1",
        request_id="request-stop-during-natural",
        connection_epoch=1,
        reason="stop_chat",
    )
    release_cleanup.set()
    await natural
    await _wait_for_condition(lambda: control.release_ready)
    retried = await control.terminate(
        execution_id="exec-1",
        request_id="request-stop-during-natural",
        connection_epoch=1,
        reason="stop_chat",
    )

    assert claimed["phase"] == "terminating"
    assert control.termination_reason == "stop_chat"
    assert control.execution_status == "canceled"
    assert retried["terminationReason"] == "stop_chat"
    assert reasons[0] == "natural_completion"
    assert "stop_chat" in reasons
    await control.close()


@pytest.mark.asyncio
async def test_explicit_termination_claim_wins_before_natural_finalization(tmp_path: Path) -> None:
    control = _controller(tmp_path)
    current = asyncio.current_task()
    assert current is not None
    await control.attach_task(current)
    completion_generation = await control.detach_task(
        current,
        execution_status="input-required",
        natural_completion=True,
    )

    claimed = await control.terminate(
        execution_id="exec-1",
        request_id="request-stop",
        connection_epoch=1,
        reason="stop_chat",
    )
    assert completion_generation is not None
    observed = await control.finalize_natural_completion(
        task_id="task-1",
        completion_generation=completion_generation,
    )

    assert claimed["terminationReason"] == "stop_chat"
    assert observed["terminationReason"] == "stop_chat"
    await _wait_for_condition(lambda: control.release_ready)
    assert control.termination_reason == "stop_chat"
    await control.close()


@pytest.mark.asyncio
async def test_explicit_termination_after_natural_release_publishes_fresh_backup(
    tmp_path: Path,
) -> None:
    calls: list[tuple[BackupReason, bool]] = []

    class BackupService:
        def backup_session(self, _cwd, _session_id, *, reason, critical) -> BackupResult:
            calls.append((reason, critical))
            generation = len(calls)
            return BackupResult(
                enabled=True,
                generation=generation,
                commit_id=f"commit-{generation}",
                shared_committed=True,
            )

    control = _controller(tmp_path, backup_service=BackupService())
    control.bind_session("session-1")
    current = asyncio.current_task()
    assert current is not None
    await control.attach_task(current)
    completion_generation = await control.detach_task(
        current,
        execution_status="input-required",
        natural_completion=True,
    )
    assert completion_generation is not None
    await control.finalize_natural_completion(
        task_id="task-1",
        completion_generation=completion_generation,
    )

    claimed = await control.terminate(
        execution_id="exec-1",
        request_id="request-stop-after-natural",
        connection_epoch=1,
        reason="stop_chat",
    )

    assert claimed["phase"] == "terminating"
    assert claimed["terminationReason"] == "stop_chat"
    assert claimed["releaseReady"] is False
    await _wait_for_condition(lambda: control.release_ready)
    assert calls == [
        (BackupReason.TERMINAL, True),
        (BackupReason.TERMINAL, True),
    ]
    assert control.termination_reason == "stop_chat"
    assert control.backup["generation"] == 2
    await control.close()


@pytest.mark.asyncio
async def test_explicit_termination_fences_inflight_natural_backup(tmp_path: Path) -> None:
    first_backup_started = threading.Event()
    release_first_backup = threading.Event()
    calls: list[str] = []

    class BackupService:
        def backup_session(self, _cwd, _session_id, *, reason, critical) -> BackupResult:
            assert reason is BackupReason.TERMINAL
            assert critical is True
            calls.append(control.termination_reason or "none")
            generation = len(calls)
            if generation == 1:
                first_backup_started.set()
                assert release_first_backup.wait(timeout=2)
            return BackupResult(
                enabled=True,
                generation=generation,
                commit_id=f"commit-{generation}",
                shared_committed=True,
            )

    control = _controller(tmp_path, backup_service=BackupService())
    control.bind_session("session-1")
    current = asyncio.current_task()
    assert current is not None
    await control.attach_task(current)
    completion_generation = await control.detach_task(
        current,
        execution_status="input-required",
        natural_completion=True,
    )
    assert completion_generation is not None
    natural = asyncio.create_task(
        control.finalize_natural_completion(
            task_id="task-1",
            completion_generation=completion_generation,
        )
    )
    assert await asyncio.to_thread(first_backup_started.wait, 5)

    await control.terminate(
        execution_id="exec-1",
        request_id="request-stop-during-backup",
        connection_epoch=1,
        reason="stop_chat",
    )
    await asyncio.sleep(0.01)
    assert calls == ["natural_completion"]

    release_first_backup.set()
    await natural
    await _wait_for_condition(lambda: control.release_ready)

    assert calls == ["natural_completion", "stop_chat"]
    assert control.termination_reason == "stop_chat"
    assert control.backup["generation"] == 2
    await control.close()


@pytest.mark.asyncio
async def test_initial_input_wait_exposes_only_local_continuation_readiness(tmp_path: Path) -> None:
    control = _controller(tmp_path)
    current = asyncio.current_task()
    assert current is not None
    await control.attach_task(current)
    await control.detach_task(current, execution_status="input-required")

    snapshot = control.snapshot()

    assert snapshot["localInputContinuationReady"] is True
    assert snapshot["inputHandoffReady"] is False
    assert "localInputContinuationReady" not in control.protocol_snapshot(snapshot)
    await control.close()


@pytest.mark.asyncio
async def test_natural_business_boundary_requires_critical_shared_backup(tmp_path: Path) -> None:
    calls: list[tuple[BackupReason, bool]] = []

    class BackupService:
        def backup_session(self, _cwd, _session_id, *, reason, critical) -> BackupResult:
            calls.append((reason, critical))
            return BackupResult(
                enabled=True,
                generation=7,
                commit_id="commit-7",
                shared_committed=True,
            )

    control = _controller(tmp_path, backup_service=BackupService())
    control.bind_session("session-1")
    current = asyncio.current_task()
    assert current is not None
    await control.attach_task(current)

    completion_generation = await control.detach_task(
        current,
        execution_status="input-required",
        natural_completion=True,
    )

    assert completion_generation is not None
    await control.finalize_natural_completion(
        task_id="task-1",
        completion_generation=completion_generation,
    )
    await _wait_for_condition(lambda: control.release_ready)
    assert calls == [(BackupReason.TERMINAL, True)]
    assert control.release_ready is True
    assert control.backup["status"] == "shared_committed"
    await control.close()


@pytest.mark.asyncio
async def test_natural_business_boundary_waits_for_disconnect_pause_resume_without_cancel(
    tmp_path: Path,
) -> None:
    control = _controller(tmp_path)
    current = asyncio.current_task()
    assert current is not None
    await control.attach_task(current)
    paused = await control.pause(
        task_id="task-1",
        expected_execution_id="exec-1",
        request_id="request-pause",
        connection_epoch=1,
        reason="client_disconnected",
        reconnect_timeout_seconds=10,
    )
    assert paused["phase"] == "pausing"

    completion_generation = await control.detach_task(
        current,
        execution_status="input-required",
        natural_completion=True,
    )

    assert current.cancelled() is False
    await _wait_for_condition(lambda: control.phase == "paused")
    assert control.termination_reason is None
    assert control.pause_id == paused["pauseId"]
    assert control.release_ready is False
    assert completion_generation is not None
    await control.finalize_natural_completion(
        task_id="task-1",
        completion_generation=completion_generation,
    )

    resumed = await control.resume(
        execution_id="exec-1",
        pause_id=paused["pauseId"],
        request_id="request-resume",
        connection_epoch=2,
    )

    assert resumed["phase"] == "resuming"
    await _wait_for_condition(lambda: control.phase == "running")
    await control.observe_state()
    await _wait_for_condition(lambda: control.release_ready)
    assert control.phase == "terminated"
    assert control.termination_reason == "natural_completion"
    assert control.pause_id is None
    assert control.release_ready is True
    await control.close()


@pytest.mark.asyncio
async def test_natural_completion_state_observation_retries_failed_cleanup(
    tmp_path: Path,
) -> None:
    cleanup_attempts = 0

    async def cleanup(_context_id: str, _task_id: str, _reason: str) -> str:
        nonlocal cleanup_attempts
        cleanup_attempts += 1
        if cleanup_attempts == 1:
            raise OSError("injected cleanup failure")
        return "input-required"

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
    current = asyncio.current_task()
    assert current is not None
    await control.attach_task(current)
    completion_generation = await control.detach_task(
        current,
        execution_status="input-required",
        natural_completion=True,
    )
    assert completion_generation is not None
    await control.finalize_natural_completion(
        task_id="task-1",
        completion_generation=completion_generation,
    )
    await _wait_for_condition(lambda: control.phase == "terminated" and control.backup["status"] == "blocked")

    observed = await control.observe_state()

    assert observed["phase"] == "terminated"
    await _wait_for_condition(lambda: control.release_ready)
    assert cleanup_attempts == 2
    assert control.execution_status == "input-required"
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
async def test_staged_backup_releases_without_waiting_for_shared_publication(tmp_path: Path) -> None:
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
            raise AssertionError("release must not depend on shared publication")

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
    await _wait_for_condition(lambda: control.release_ready)
    assert control.phase == "terminated"
    assert control.backup["status"] == "staged_committed"
    assert control.backup["generation"] == 7
    assert control.backup["commitId"] == "commit-7"
    assert backup.wait_calls == 0
    await control.close()


@pytest.mark.asyncio
async def test_staged_backup_failure_still_blocks_release_until_a_snapshot_lands(tmp_path: Path) -> None:
    class StagedBackupService:
        def __init__(self) -> None:
            self.calls = 0

        def backup_session(self, cwd, session_id, *, reason, critical) -> BackupResult:
            self.calls += 1
            staged = self.calls > 1
            return BackupResult(
                enabled=True,
                generation=7 if staged else None,
                commit_id="commit-7" if staged else None,
                succeeded=staged,
                staged_committed=staged,
                shared_committed=False,
                error=None if staged else "staging unavailable",
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
    assert control.backup["status"] == "staged_committed"
    assert backup.calls == 2
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

    third_normal_turn = await service.begin_execution(
        context_id="ctx-1",
        task_id="task-1",
        owner="owner-1",
        cwd=str(tmp_path),
    )
    assert third_normal_turn is not next_normal_turn
    assert third_normal_turn.execution_id != next_normal_turn.execution_id
    await third_normal_turn.detach_task(current, execution_status="input-required")
    await service.close()


@pytest.mark.asyncio
async def test_pipeline_continuation_replaces_drained_terminated_control_when_backup_is_blocked(
    tmp_path: Path,
) -> None:
    service = ExecutionControlService(persistence_root=tmp_path, backup_service=None)
    first = await service.begin_execution(
        context_id="ctx-1",
        task_id="task-1",
        owner="owner-1",
        cwd=str(tmp_path),
    )
    current = asyncio.current_task()
    assert current is not None
    await first.detach_task(current, execution_status="input-required")
    first.phase = "terminated"
    first.execution_status = "canceled"
    first.backup = {"status": "blocked", "error": "shared backup unavailable"}
    first.release_ready = False
    first.revision += 1
    await first._persist_snapshot(first.snapshot())

    with pytest.raises(ExecutionControlConflictError):
        await service.begin_execution(
            context_id="ctx-1",
            task_id="task-1",
            owner="owner-1",
            cwd=str(tmp_path),
            continue_input_required=True,
        )

    admission = await service.reserve_recoverable_input_continuation(
        context_id="ctx-1",
        task_id="task-1",
        owner="owner-1",
    )
    assert admission is not None
    continuation = await service.begin_execution(
        context_id="ctx-1",
        task_id="task-1",
        owner="owner-1",
        cwd=str(tmp_path),
        continue_input_required=True,
        recoverable_input_admission=admission,
    )

    assert continuation is not first
    assert continuation.execution_id != first.execution_id
    assert all(task.done() for task in first._background_tasks)
    await continuation.detach_task(current, execution_status="input-required")
    await service.close()


@pytest.mark.asyncio
async def test_recoverable_input_admission_rejects_mismatched_task_and_live_background_work(tmp_path: Path) -> None:
    service = ExecutionControlService(persistence_root=tmp_path, backup_service=None)
    control = await service.begin_execution(
        context_id="ctx-1",
        task_id="task-1",
        owner="owner-1",
        cwd=str(tmp_path),
    )
    current = asyncio.current_task()
    assert current is not None
    await control.detach_task(current, execution_status="input-required")
    control.phase = "terminated"
    control.execution_status = "canceled"
    control.backup = {"status": "blocked", "error": "shared backup unavailable"}
    control.release_ready = False

    assert (
        await service.reserve_recoverable_input_continuation(
            context_id="ctx-1",
            task_id="task-other",
            owner="owner-1",
        )
        is None
    )

    background_release = asyncio.Event()
    background = control._spawn(background_release.wait(), "test-recovery-admission")
    assert (
        await service.reserve_recoverable_input_continuation(
            context_id="ctx-1",
            task_id="task-1",
            owner="owner-1",
        )
        is None
    )
    background_release.set()
    await background
    await service.close()


@pytest.mark.asyncio
async def test_recoverable_input_wait_wakes_after_last_background_task_finishes(tmp_path: Path) -> None:
    service = ExecutionControlService(persistence_root=tmp_path, backup_service=None)
    control = await service.begin_execution(
        context_id="ctx-1",
        task_id="task-1",
        owner="owner-1",
        cwd=str(tmp_path),
    )
    current = asyncio.current_task()
    assert current is not None
    await control.detach_task(current, execution_status="input-required")
    control.phase = "terminated"
    control.execution_status = "canceled"
    control.backup = {"status": "blocked", "error": "shared backup unavailable"}
    control.release_ready = False
    background_release = asyncio.Event()
    background = control._spawn(background_release.wait(), "test-recovery-wait")

    waiter = asyncio.create_task(
        service.wait_until_recoverable_input_continuation(
            context_id="ctx-1",
            task_id="task-1",
            timeout=1,
        )
    )
    await asyncio.sleep(0)
    assert not waiter.done()

    background_release.set()
    await background
    await asyncio.wait_for(waiter, timeout=1)
    await service.close()


@pytest.mark.asyncio
async def test_recoverable_input_admission_is_single_owner_across_service_instances(tmp_path: Path) -> None:
    first_service = ExecutionControlService(persistence_root=tmp_path, backup_service=None)
    second_service = ExecutionControlService(persistence_root=tmp_path, backup_service=None)

    admissions = await asyncio.gather(
        first_service.reserve_recoverable_input_continuation(
            context_id="ctx-1",
            task_id="task-1",
            owner="owner-1",
        ),
        second_service.reserve_recoverable_input_continuation(
            context_id="ctx-1",
            task_id="task-1",
            owner="owner-1",
        ),
    )

    assert sum(admission is not None for admission in admissions) == 1
    winner = first_service if admissions[0] is not None else second_service
    loser = second_service if winner is first_service else first_service
    admission = admissions[0] or admissions[1]
    assert admission is not None

    with pytest.raises(ExecutionControlConflictError, match="active in another process"):
        await loser.begin_execution(
            context_id="ctx-1",
            task_id="task-1",
            owner="owner-1",
            cwd=str(tmp_path),
            continue_input_required=True,
        )

    control = await winner.begin_execution(
        context_id="ctx-1",
        task_id="task-1",
        owner="owner-1",
        cwd=str(tmp_path),
        continue_input_required=True,
        recoverable_input_admission=admission,
    )
    assert (
        await loser.reserve_recoverable_input_continuation(
            context_id="ctx-1",
            task_id="task-1",
            owner="owner-1",
        )
        is None
    )
    with pytest.raises(ExecutionControlConflictError, match="active in another process"):
        await loser.begin_execution(
            context_id="ctx-1",
            task_id="task-1",
            owner="owner-1",
            cwd=str(tmp_path),
            continue_input_required=True,
        )
    current = asyncio.current_task()
    assert current is not None
    await control.detach_task(current, execution_status="input-required")
    await first_service.close()
    await second_service.close()


@pytest.mark.asyncio
async def test_stale_local_terminated_control_cannot_replace_new_shared_running_control(tmp_path: Path) -> None:
    stale_service = ExecutionControlService(persistence_root=tmp_path, backup_service=None)
    recovering_service = ExecutionControlService(persistence_root=tmp_path, backup_service=None)
    stale_control = await stale_service.begin_execution(
        context_id="ctx-1",
        task_id="task-1",
        owner="owner-1",
        cwd=str(tmp_path),
    )
    current = asyncio.current_task()
    assert current is not None
    await stale_control.detach_task(current, execution_status="input-required")
    stale_control.phase = "terminated"
    stale_control.execution_status = "canceled"
    stale_control.backup = {"status": "blocked", "error": "shared backup unavailable"}
    stale_control.release_ready = False
    stale_control.revision += 1
    await stale_control._persist_snapshot(stale_control.snapshot())

    admission = await recovering_service.reserve_recoverable_input_continuation(
        context_id="ctx-1",
        task_id="task-1",
        owner="owner-1",
    )
    assert admission is not None
    recovered = await recovering_service.begin_execution(
        context_id="ctx-1",
        task_id="task-1",
        owner="owner-1",
        cwd=str(tmp_path),
        continue_input_required=True,
        recoverable_input_admission=admission,
    )

    assert (
        await stale_service.reserve_recoverable_input_continuation(
            context_id="ctx-1",
            task_id="task-1",
            owner="owner-1",
        )
        is None
    )
    persisted = json.loads((tmp_path / "execution-control" / "ctx-1.json").read_text(encoding="utf-8"))
    assert persisted["phase"] == "running"
    assert persisted["executionId"] == recovered.execution_id
    assert stale_control.execution_id != recovered.execution_id

    await recovered.detach_task(current, execution_status="input-required")
    await stale_service.close()
    await recovering_service.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invalid_release_ready",
    ["false", 1],
    ids=["string-false", "integer-one"],
)
async def test_recovery_activation_rejects_non_boolean_persisted_release_ready(
    tmp_path: Path,
    invalid_release_ready: object,
) -> None:
    source_service = ExecutionControlService(persistence_root=tmp_path, backup_service=None)
    recovering_service = ExecutionControlService(persistence_root=tmp_path, backup_service=None)
    source = await source_service.begin_execution(
        context_id="ctx-1",
        task_id="task-1",
        owner="owner-1",
        cwd=str(tmp_path),
    )
    current = asyncio.current_task()
    assert current is not None
    await source.detach_task(current, execution_status="input-required")
    source.phase = "terminated"
    source.execution_status = "canceled"
    source.backup = {"status": "blocked", "error": "shared backup unavailable"}
    source.release_ready = False
    source.revision += 1
    await source._persist_snapshot(source.snapshot())

    admission = await recovering_service.reserve_recoverable_input_continuation(
        context_id="ctx-1",
        task_id="task-1",
        owner="owner-1",
    )
    assert admission is not None
    control_path = tmp_path / "execution-control" / "ctx-1.json"
    persisted = json.loads(control_path.read_text(encoding="utf-8"))
    persisted["phase"] = "terminated"
    persisted["inputHandoffReady"] = False
    persisted["backup"] = {"status": "pending"}
    persisted["releaseReady"] = invalid_release_ready
    execution_control_module.atomic_write_json(control_path, persisted)

    async def begin_recovery() -> None:
        recovered = await recovering_service.begin_execution(
            context_id="ctx-1",
            task_id="task-1",
            owner="owner-1",
            cwd=str(tmp_path),
            continue_input_required=True,
            recoverable_input_admission=admission,
        )
        recovery_task = asyncio.current_task()
        assert recovery_task is not None
        await recovered.detach_task(recovery_task, execution_status="input-required")

    try:
        with pytest.raises(ExecutionControlConflictError, match="admission is stale"):
            await asyncio.create_task(begin_recovery())
        assert recovering_service.get_for_context("ctx-1") is None
        assert json.loads(control_path.read_text(encoding="utf-8")) == persisted
    finally:
        await source_service.close()
        await recovering_service.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("persisted_input_handoff_ready", "phase", "release_ready", "backup_status", "expected_allowed"),
    [
        pytest.param("false", "terminated", True, "pending", False, id="string-false"),
        pytest.param(1, "terminated", True, "pending", False, id="integer-one"),
        pytest.param(None, "terminated", True, "pending", False, id="missing"),
        pytest.param(True, "running", False, "pending", True, id="boolean-true"),
        pytest.param(False, "terminated", True, "pending", True, id="boolean-false-release-ready"),
        pytest.param(False, "terminated", False, "blocked", True, id="boolean-false-backup-blocked"),
        pytest.param(False, "terminated", "false", "blocked", False, id="blocked-string-false-release-ready"),
        pytest.param(False, "terminated", 1, "blocked", False, id="blocked-integer-one-release-ready"),
        pytest.param(False, "terminated", None, "blocked", False, id="blocked-missing-release-ready"),
    ],
)
async def test_recoverable_input_admission_activation_validates_persisted_input_handoff_ready(
    tmp_path: Path,
    persisted_input_handoff_ready: object,
    phase: str,
    release_ready: object | None,
    backup_status: str,
    expected_allowed: bool,
) -> None:
    source_service = ExecutionControlService(persistence_root=tmp_path, backup_service=None)
    recovering_service = ExecutionControlService(persistence_root=tmp_path, backup_service=None)
    source = await source_service.begin_execution(
        context_id="ctx-1",
        task_id="task-1",
        owner="owner-1",
        cwd=str(tmp_path),
    )
    current = asyncio.current_task()
    assert current is not None
    await source.detach_task(current, execution_status="input-required")
    source.phase = "terminated"
    source.execution_status = "canceled"
    source.backup = {"status": "blocked", "error": "shared backup unavailable"}
    source.release_ready = False
    source.revision += 1
    await source._persist_snapshot(source.snapshot())

    admission = await recovering_service.reserve_recoverable_input_continuation(
        context_id="ctx-1",
        task_id="task-1",
        owner="owner-1",
    )
    assert admission is not None
    control_path = tmp_path / "execution-control" / "ctx-1.json"
    persisted = json.loads(control_path.read_text(encoding="utf-8"))
    persisted["phase"] = phase
    if persisted_input_handoff_ready is None:
        persisted.pop("inputHandoffReady")
    else:
        persisted["inputHandoffReady"] = persisted_input_handoff_ready
    persisted["backup"] = {"status": backup_status}
    if release_ready is None:
        persisted.pop("releaseReady")
    else:
        persisted["releaseReady"] = release_ready
    execution_control_module.atomic_write_json(control_path, persisted)

    async def begin_recovery() -> None:
        recovered = await recovering_service.begin_execution(
            context_id="ctx-1",
            task_id="task-1",
            owner="owner-1",
            cwd=str(tmp_path),
            continue_input_required=True,
            recoverable_input_admission=admission,
        )
        recovery_task = asyncio.current_task()
        assert recovery_task is not None
        await recovered.detach_task(recovery_task, execution_status="input-required")

    try:
        if expected_allowed:
            await asyncio.create_task(begin_recovery())
            assert recovering_service.get_for_context("ctx-1") is not None
        else:
            with pytest.raises(ExecutionControlConflictError, match="admission is stale"):
                await asyncio.create_task(begin_recovery())
            assert recovering_service.get_for_context("ctx-1") is None
            assert json.loads(control_path.read_text(encoding="utf-8")) == persisted
    finally:
        await source_service.close()
        await recovering_service.close()


@pytest.mark.asyncio
async def test_recovered_input_wait_can_handoff_to_another_service_instance(tmp_path: Path) -> None:
    source_service = ExecutionControlService(persistence_root=tmp_path, backup_service=None)
    first_recovery_service = ExecutionControlService(persistence_root=tmp_path, backup_service=None)
    next_recovery_service = ExecutionControlService(persistence_root=tmp_path, backup_service=None)
    source = await source_service.begin_execution(
        context_id="ctx-1",
        task_id="task-1",
        owner="owner-1",
        cwd=str(tmp_path),
    )
    current = asyncio.current_task()
    assert current is not None
    await source.detach_task(current, execution_status="input-required")
    source.phase = "terminated"
    source.execution_status = "canceled"
    source.backup = {"status": "blocked", "error": "shared backup unavailable"}
    source.release_ready = False
    source.revision += 1
    await source._persist_snapshot(source.snapshot())

    first_admission = await first_recovery_service.reserve_recoverable_input_continuation(
        context_id="ctx-1",
        task_id="task-1",
        owner="owner-1",
    )
    assert first_admission is not None
    first_recovery = await first_recovery_service.begin_execution(
        context_id="ctx-1",
        task_id="task-1",
        owner="owner-1",
        cwd=str(tmp_path),
        continue_input_required=True,
        recoverable_input_admission=first_admission,
    )
    await first_recovery.detach_task(current, execution_status="input-required")
    handoff_snapshot = json.loads((tmp_path / "execution-control" / "ctx-1.json").read_text(encoding="utf-8"))
    assert handoff_snapshot["inputHandoffReady"] is True
    assert handoff_snapshot["executionStatus"] == "input-required"
    assert handoff_snapshot["streamAvailable"] is False

    next_admission = await next_recovery_service.reserve_recoverable_input_continuation(
        context_id="ctx-1",
        task_id="task-1",
        owner="owner-1",
    )
    assert next_admission is not None
    next_recovery = await next_recovery_service.begin_execution(
        context_id="ctx-1",
        task_id="task-1",
        owner="owner-1",
        cwd=str(tmp_path),
        continue_input_required=True,
        recoverable_input_admission=next_admission,
    )
    assert next_recovery.execution_id != first_recovery.execution_id
    persisted = json.loads((tmp_path / "execution-control" / "ctx-1.json").read_text(encoding="utf-8"))
    assert persisted["phase"] == "running"
    assert persisted["executionId"] == next_recovery.execution_id
    assert persisted["inputHandoffReady"] is False

    await next_recovery.detach_task(current, execution_status="input-required")
    await source_service.close()
    await first_recovery_service.close()
    await next_recovery_service.close()


@pytest.mark.asyncio
async def test_failed_input_handoff_commit_requires_admission_and_retries_on_reserve(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    service = ExecutionControlService(persistence_root=tmp_path, backup_service=None)
    source = await service.begin_execution(
        context_id="ctx-1",
        task_id="task-1",
        owner="owner-1",
        cwd=str(tmp_path),
    )
    current = asyncio.current_task()
    assert current is not None
    await source.detach_task(current, execution_status="input-required")
    source.phase = "terminated"
    source.execution_status = "canceled"
    source.backup = {"status": "blocked", "error": "shared backup unavailable"}
    source.release_ready = False
    source.revision += 1
    await source._persist_snapshot(source.snapshot())
    admission = await service.reserve_recoverable_input_continuation(
        context_id="ctx-1",
        task_id="task-1",
        owner="owner-1",
    )
    assert admission is not None
    recovered = await service.begin_execution(
        context_id="ctx-1",
        task_id="task-1",
        owner="owner-1",
        cwd=str(tmp_path),
        continue_input_required=True,
        recoverable_input_admission=admission,
    )
    original_atomic_write_json = execution_control_module.atomic_write_json

    def fail_input_handoff(path: Path, value: dict) -> None:
        if value.get("inputHandoffReady") is True:
            raise OSError("input handoff write failed")
        original_atomic_write_json(path, value)

    monkeypatch.setattr(execution_control_module, "atomic_write_json", fail_input_handoff)
    with pytest.raises(OSError, match="input handoff write failed"):
        await recovered.detach_task(current, execution_status="input-required")
    assert recovered.input_handoff_ready()
    persisted_path = tmp_path / "execution-control" / "ctx-1.json"
    assert json.loads(persisted_path.read_text(encoding="utf-8"))["inputHandoffReady"] is False

    with pytest.raises(ExecutionControlConflictError, match="active in another process"):
        await service.begin_execution(
            context_id="ctx-1",
            task_id="task-1",
            owner="owner-1",
            cwd=str(tmp_path),
            continue_input_required=True,
        )

    monkeypatch.setattr(execution_control_module, "atomic_write_json", original_atomic_write_json)
    retry_admission = await service.reserve_recoverable_input_continuation(
        context_id="ctx-1",
        task_id="task-1",
        owner="owner-1",
    )
    assert retry_admission is not None
    assert json.loads(persisted_path.read_text(encoding="utf-8"))["inputHandoffReady"] is True
    retried = await service.begin_execution(
        context_id="ctx-1",
        task_id="task-1",
        owner="owner-1",
        cwd=str(tmp_path),
        continue_input_required=True,
        recoverable_input_admission=retry_admission,
    )
    assert retried is not recovered

    await retried.detach_task(current, execution_status="input-required")
    await service.close()


@pytest.mark.asyncio
async def test_in_memory_recovered_input_handoff_still_requires_admission() -> None:
    service = ExecutionControlService(persistence_root=None, backup_service=None)
    source = await service.begin_execution(
        context_id="ctx-1",
        task_id="task-1",
        owner="owner-1",
        cwd="/tmp",
    )
    current = asyncio.current_task()
    assert current is not None
    await source.detach_task(current, execution_status="input-required")
    source.phase = "terminated"
    source.execution_status = "canceled"
    source.backup = {"status": "blocked", "error": "shared backup unavailable"}
    source.release_ready = False
    admission = await service.reserve_recoverable_input_continuation(
        context_id="ctx-1",
        task_id="task-1",
        owner="owner-1",
    )
    assert admission is not None
    recovered = await service.begin_execution(
        context_id="ctx-1",
        task_id="task-1",
        owner="owner-1",
        cwd="/tmp",
        continue_input_required=True,
        recoverable_input_admission=admission,
    )
    await recovered.detach_task(current, execution_status="input-required")
    assert recovered.input_handoff_ready()

    with pytest.raises(ExecutionControlConflictError, match="active in another process"):
        await service.begin_execution(
            context_id="ctx-1",
            task_id="task-1",
            owner="owner-1",
            cwd="/tmp",
            continue_input_required=True,
        )

    retry_admission = await service.reserve_recoverable_input_continuation(
        context_id="ctx-1",
        task_id="task-1",
        owner="owner-1",
    )
    assert retry_admission is not None
    retried = await service.begin_execution(
        context_id="ctx-1",
        task_id="task-1",
        owner="owner-1",
        cwd="/tmp",
        continue_input_required=True,
        recoverable_input_admission=retry_admission,
    )
    assert retried is not recovered

    await retried.detach_task(current, execution_status="input-required")
    await service.close()


@pytest.mark.asyncio
async def test_expired_recovery_admission_does_not_mutate_local_or_shared_control(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    service = ExecutionControlService(persistence_root=tmp_path, backup_service=None)
    original = await service.begin_execution(
        context_id="ctx-1",
        task_id="task-1",
        owner="owner-1",
        cwd=str(tmp_path),
    )
    current = asyncio.current_task()
    assert current is not None
    await original.detach_task(current, execution_status="input-required")
    original.phase = "terminated"
    original.execution_status = "canceled"
    original.backup = {"status": "blocked", "error": "shared backup unavailable"}
    original.release_ready = False
    original.revision += 1
    await original._persist_snapshot(original.snapshot())
    control_path = tmp_path / "execution-control" / "ctx-1.json"
    original_document = control_path.read_text(encoding="utf-8")

    admission = await service.reserve_recoverable_input_continuation(
        context_id="ctx-1",
        task_id="task-1",
        owner="owner-1",
    )
    assert admission is not None
    admission_path = tmp_path / "execution-control" / ".ctx-1.recoverable-input.json"
    expires_at = json.loads(admission_path.read_text(encoding="utf-8"))["expiresAt"]
    monkeypatch.setattr("iac_code.a2a.execution_control.time.time", lambda: expires_at + 1)

    with pytest.raises(ExecutionControlConflictError, match="admission is stale"):
        await service.begin_execution(
            context_id="ctx-1",
            task_id="task-1",
            owner="owner-1",
            cwd=str(tmp_path),
            continue_input_required=True,
            recoverable_input_admission=admission,
        )

    assert service.get_for_context("ctx-1") is original
    assert not original.has_managed_work()
    assert control_path.read_text(encoding="utf-8") == original_document
    await service.release_recoverable_input_continuation(admission)
    await service.close()


@pytest.mark.asyncio
async def test_recovery_activation_persistence_failure_keeps_original_control(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    service = ExecutionControlService(persistence_root=tmp_path, backup_service=None)
    original = await service.begin_execution(
        context_id="ctx-1",
        task_id="task-1",
        owner="owner-1",
        cwd=str(tmp_path),
    )
    current = asyncio.current_task()
    assert current is not None
    await original.detach_task(current, execution_status="input-required")
    original.phase = "terminated"
    original.execution_status = "canceled"
    original.backup = {"status": "blocked", "error": "shared backup unavailable"}
    original.release_ready = False
    original.revision += 1
    await original._persist_snapshot(original.snapshot())
    control_path = tmp_path / "execution-control" / "ctx-1.json"
    original_document = control_path.read_text(encoding="utf-8")
    admission = await service.reserve_recoverable_input_continuation(
        context_id="ctx-1",
        task_id="task-1",
        owner="owner-1",
    )
    assert admission is not None
    original_atomic_write_json = execution_control_module.atomic_write_json

    def fail_running_snapshot(path: Path, value: dict) -> None:
        if path == control_path and value.get("phase") == "running":
            raise OSError("running snapshot write failed")
        original_atomic_write_json(path, value)

    monkeypatch.setattr(execution_control_module, "atomic_write_json", fail_running_snapshot)

    with pytest.raises(OSError, match="running snapshot write failed"):
        await service.begin_execution(
            context_id="ctx-1",
            task_id="task-1",
            owner="owner-1",
            cwd=str(tmp_path),
            continue_input_required=True,
            recoverable_input_admission=admission,
        )

    assert service.get_for_context("ctx-1") is original
    assert not original.has_managed_work()
    assert control_path.read_text(encoding="utf-8") == original_document
    admission_path = tmp_path / "execution-control" / ".ctx-1.recoverable-input.json"
    assert admission_path.exists()
    await service.release_recoverable_input_continuation(admission)
    await service.close()


@pytest.mark.asyncio
async def test_cancelled_recovery_activation_rolls_back_shared_and_local_control(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    service = ExecutionControlService(persistence_root=tmp_path, backup_service=None)
    original = await service.begin_execution(
        context_id="ctx-1",
        task_id="task-1",
        owner="owner-1",
        cwd=str(tmp_path),
    )
    current = asyncio.current_task()
    assert current is not None
    await original.detach_task(current, execution_status="input-required")
    original.phase = "terminated"
    original.execution_status = "canceled"
    original.backup = {"status": "blocked", "error": "shared backup unavailable"}
    original.release_ready = False
    original.revision += 1
    await original._persist_snapshot(original.snapshot())
    control_path = tmp_path / "execution-control" / "ctx-1.json"
    original_document = control_path.read_text(encoding="utf-8")
    admission = await service.reserve_recoverable_input_continuation(
        context_id="ctx-1",
        task_id="task-1",
        owner="owner-1",
    )
    assert admission is not None
    activated = threading.Event()
    allow_return = threading.Event()
    original_activate = service._recoverable_input_admissions.activate

    def activate_then_block(*args, **kwargs):
        result = original_activate(*args, **kwargs)
        activated.set()
        assert allow_return.wait(timeout=5)
        return result

    monkeypatch.setattr(service._recoverable_input_admissions, "activate", activate_then_block)
    begin_task = asyncio.create_task(
        service.begin_execution(
            context_id="ctx-1",
            task_id="task-1",
            owner="owner-1",
            cwd=str(tmp_path),
            continue_input_required=True,
            recoverable_input_admission=admission,
        )
    )
    assert await asyncio.to_thread(activated.wait, 5)
    begin_task.cancel()
    allow_return.set()

    with pytest.raises(asyncio.CancelledError):
        await begin_task

    assert service.get_for_context("ctx-1") is original
    assert control_path.read_text(encoding="utf-8") == original_document
    admission_path = tmp_path / "execution-control" / ".ctx-1.recoverable-input.json"
    assert admission_path.exists()
    await service.release_recoverable_input_continuation(admission)
    await service.close()


@pytest.mark.asyncio
async def test_admission_unlink_failure_keeps_new_control_consistent_and_release_retries(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    service = ExecutionControlService(persistence_root=tmp_path, backup_service=None)
    original = await service.begin_execution(
        context_id="ctx-1",
        task_id="task-1",
        owner="owner-1",
        cwd=str(tmp_path),
    )
    current = asyncio.current_task()
    assert current is not None
    await original.detach_task(current, execution_status="input-required")
    original.phase = "terminated"
    original.execution_status = "canceled"
    original.backup = {"status": "blocked", "error": "shared backup unavailable"}
    original.release_ready = False
    original.revision += 1
    await original._persist_snapshot(original.snapshot())
    admission = await service.reserve_recoverable_input_continuation(
        context_id="ctx-1",
        task_id="task-1",
        owner="owner-1",
    )
    assert admission is not None
    admission_path = tmp_path / "execution-control" / ".ctx-1.recoverable-input.json"
    original_unlink = Path.unlink
    failed_once = False

    def fail_admission_unlink_once(path: Path, *args, **kwargs) -> None:
        nonlocal failed_once
        if path == admission_path and not failed_once:
            failed_once = True
            raise OSError("admission unlink failed")
        original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_admission_unlink_once)
    recovered = await service.begin_execution(
        context_id="ctx-1",
        task_id="task-1",
        owner="owner-1",
        cwd=str(tmp_path),
        continue_input_required=True,
        recoverable_input_admission=admission,
    )

    persisted = json.loads((tmp_path / "execution-control" / "ctx-1.json").read_text(encoding="utf-8"))
    assert service.get_for_context("ctx-1") is recovered
    assert persisted["phase"] == "running"
    assert persisted["executionId"] == recovered.execution_id
    assert admission_path.exists()
    await service.release_recoverable_input_continuation(admission)
    assert not admission_path.exists()

    await recovered.detach_task(current, execution_status="input-required")
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


class _BlockingCoordinatorBackupService:
    """A staged backup service whose directory copy blocks until it is released."""

    def __init__(self, staging_root: Path) -> None:
        self.staging_root = staging_root
        self.started = threading.Event()
        self.release = threading.Event()
        self.copies = 0

    def _source_for_backup(self, cwd: str, session_id: str) -> Path:
        return Path(cwd) / "projects" / "project" / session_id

    def backup_session(
        self,
        _cwd,
        _session_id,
        *,
        reason,
        critical,
        publication_proofs=None,
        operation_commit_id=None,
    ) -> BackupResult:
        del reason, critical, publication_proofs
        self.started.set()
        assert self.release.wait(5)
        self.copies += 1
        return BackupResult(
            enabled=True,
            generation=1,
            commit_id=operation_commit_id,
            staged_committed=True,
            shared_committed=False,
        )


@pytest.mark.asyncio
async def test_natural_completion_hands_off_after_the_local_snapshot_finishes(tmp_path: Path) -> None:
    backup_service = _BlockingCoordinatorBackupService(tmp_path / "staging")
    coordinator = SessionBackupCoordinator(backup_service, state_root=tmp_path / "state", retry_delays=())
    control = ExecutionController(
        context_id="ctx-1",
        task_id="task-1",
        owner="owner-1",
        cwd=str(tmp_path),
        server_instance_id="instance-1",
        persistence_path=tmp_path / "control.json",
        backup_service=backup_service,
        execution_id="exec-1",
        backup_coordinator=coordinator,
    )
    control.bind_session("session-1")
    current = asyncio.current_task()
    assert current is not None
    await control.attach_task(current)
    completion_generation = await control.detach_task(
        current,
        execution_status="input-required",
        natural_completion=True,
    )
    assert completion_generation is not None
    try:
        finalization = asyncio.create_task(
            control.finalize_natural_completion(
                task_id="task-1",
                completion_generation=completion_generation,
            )
        )
        assert await asyncio.to_thread(backup_service.started.wait, 5)
        assert finalization.done() is False
        backup_service.release.set()
        observed = await asyncio.wait_for(finalization, 5)

        assert observed["phase"] == "terminated"
        assert observed["terminationReason"] == "natural_completion"
        handoff = observed["naturalHandoff"]
        assert handoff["version"] == NATURAL_HANDOFF_VERSION
        assert handoff["contextId"] == "ctx-1"
        assert handoff["taskId"] == "task-1"
        assert handoff["executionId"] == "exec-1"
        assert handoff["completionGeneration"] == completion_generation
        assert handoff["ownerGeneration"] == 1
        assert handoff["businessDrained"] is True
        assert handoff["backupDisabled"] is False
        assert handoff["businessRevision"] == 1
        assert handoff["pendingJobId"] is not None
        assert handoff["stagedCommitted"] is True
        assert handoff["snapshotGeneration"] == 1
        assert isinstance(handoff["snapshotCommitId"], str) and handoff["snapshotCommitId"]
        assert observed["backup"] == {
            "status": "staged_committed",
            "jobId": handoff["pendingJobId"],
            "businessRevision": 1,
            "generation": 1,
            "commitId": handoff["snapshotCommitId"],
        }
        assert backup_service.copies == 1
        marker = tmp_path / "staging" / ".pending" / "{}.json".format(handoff["pendingJobId"])
        assert marker.exists() is False
        assert control.natural_handoff_admits_replacement() is True
        await _wait_for_condition(lambda: control.release_ready)
    finally:
        backup_service.release.set()
        await coordinator.aclose()
        await control.close()


@pytest.mark.asyncio
async def test_local_job_persistence_failure_blocks_release_instead_of_faking_a_handoff(tmp_path: Path) -> None:
    class FailingCoordinator:
        enabled = True

        async def register_boundary(self, **_kwargs) -> None:
            raise SessionBackupHandoffError("session backup job could not be persisted: OSError")

    control = ExecutionController(
        context_id="ctx-1",
        task_id="task-1",
        owner="owner-1",
        cwd=str(tmp_path),
        server_instance_id="instance-1",
        persistence_path=tmp_path / "control.json",
        backup_service=None,
        execution_id="exec-1",
        backup_coordinator=FailingCoordinator(),
    )
    control.bind_session("session-1")
    current = asyncio.current_task()
    assert current is not None
    await control.attach_task(current)
    completion_generation = await control.detach_task(
        current,
        execution_status="input-required",
        natural_completion=True,
    )
    assert completion_generation is not None

    observed = await control.finalize_natural_completion(
        task_id="task-1",
        completion_generation=completion_generation,
    )

    assert observed["backup"]["status"] == "blocked"
    assert observed["naturalHandoff"] is None
    assert observed["releaseReady"] is False
    assert control.natural_handoff_admits_replacement() is False
    await _wait_for_condition(lambda: control.backup["status"] == "blocked")
    assert control.release_ready is False
    await control.close()


class _RecordingCoordinator:
    enabled = True

    def __init__(self) -> None:
        self.registrations: list[dict] = []
        self.quiescence_waits: list[str | None] = []

    async def register_boundary(self, **kwargs):
        self.registrations.append(kwargs)
        return SessionBackupHandoff(
            business_revision=len(self.registrations),
            job_id="job-{}".format(len(self.registrations)),
            staged_committed=True,
            snapshot_generation=len(self.registrations),
            snapshot_commit_id="commit-{}".format(len(self.registrations)),
        )

    async def wait_for_local_snapshot_quiescence(self, *, cwd, session_id, timeout=None) -> None:
        del cwd, timeout
        self.quiescence_waits.append(session_id)


async def _prepare_natural_handoff(
    service: ExecutionControlService,
    *,
    session_id: str | None,
) -> tuple[ExecutionController, int]:
    control = await service.begin_execution(
        context_id="ctx-1",
        task_id="task-1",
        owner="owner-1",
        cwd="/repo",
    )
    if session_id is not None:
        control.bind_session(session_id)
    current = asyncio.current_task()
    assert current is not None
    generation = await control.detach_task(
        current,
        execution_status="input-required",
        natural_completion=True,
    )
    assert generation is not None
    return control, generation


async def _persist_natural_handoff_before_release_ready(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Path, dict]:
    coordinator = _RecordingCoordinator()
    service = ExecutionControlService(
        persistence_root=tmp_path,
        backup_service=None,
        backup_coordinator=coordinator,
    )
    control, completion_generation = await _prepare_natural_handoff(service, session_id="session-1")
    release_commit_attempted = threading.Event()
    original_write = execution_control_module.atomic_write_json

    def fail_release_ready_commit(path: Path, value: dict) -> None:
        if value.get("releaseReady") is True:
            release_commit_attempted.set()
            raise OSError("injected release-ready snapshot failure")
        original_write(path, value)

    monkeypatch.setattr(execution_control_module, "atomic_write_json", fail_release_ready_commit)
    try:
        observed = await control.finalize_natural_completion(
            task_id="task-1",
            completion_generation=completion_generation,
        )
        assert observed["naturalHandoff"] is not None
        assert await asyncio.to_thread(release_commit_attempted.wait, 5)
        persisted_path = tmp_path / "execution-control" / "ctx-1.json"
        persisted = json.loads(persisted_path.read_text(encoding="utf-8"))
        assert persisted["naturalHandoff"] == observed["naturalHandoff"]
        assert persisted["persistedRevision"] == persisted["revision"]
        assert persisted["releaseReady"] is False
        assert persisted["phase"] in {"terminating", "terminated"}
        return persisted_path, persisted
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_restart_admits_next_turn_from_persisted_natural_handoff_before_release_ready(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, persisted = await _persist_natural_handoff_before_release_ready(tmp_path, monkeypatch)
    restarted = ExecutionControlService(persistence_root=tmp_path, backup_service=None)
    try:
        replacement = await restarted.begin_execution(
            context_id="ctx-1",
            task_id="task-2",
            owner="owner-1",
            cwd="/repo",
        )
        assert replacement.execution_id != persisted["executionId"]
        current = asyncio.current_task()
        assert current is not None
        await replacement.detach_task(current, execution_status="input-required")
    finally:
        await restarted.close()


@pytest.mark.asyncio
async def test_concurrent_restarts_claim_persisted_natural_handoff_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    persisted_path, previous = await _persist_natural_handoff_before_release_ready(tmp_path, monkeypatch)
    services = [
        ExecutionControlService(persistence_root=tmp_path, backup_service=None),
        ExecutionControlService(persistence_root=tmp_path, backup_service=None),
    ]
    start = asyncio.Event()

    async def begin_replacement(service: ExecutionControlService, task_id: str) -> ExecutionController:
        await start.wait()
        return await service.begin_execution(
            context_id="ctx-1",
            task_id=task_id,
            owner="owner-1",
            cwd="/repo",
        )

    attempts = [
        asyncio.create_task(begin_replacement(service, f"task-{index + 2}")) for index, service in enumerate(services)
    ]
    start.set()
    try:
        results = await asyncio.gather(*attempts, return_exceptions=True)
        successes = [result for result in results if isinstance(result, ExecutionController)]
        conflicts = [result for result in results if isinstance(result, ExecutionControlConflictError)]

        assert len(successes) == 1
        assert len(conflicts) == 1
        winner = successes[0]
        persisted = json.loads(persisted_path.read_text(encoding="utf-8"))
        assert persisted["executionId"] == winner.execution_id
        assert persisted["executionId"] != previous["executionId"]
        assert persisted["serverInstanceId"] == winner.server_instance_id
        assert persisted["phase"] == "running"
    finally:
        await asyncio.gather(*(service.close() for service in services))


@pytest.mark.asyncio
async def test_late_release_ready_snapshot_cannot_overwrite_new_natural_handoff_claim(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old_service = ExecutionControlService(
        persistence_root=tmp_path,
        backup_service=None,
        backup_coordinator=_RecordingCoordinator(),
    )
    old_control, completion_generation = await _prepare_natural_handoff(old_service, session_id="session-1")
    late_write_started = asyncio.Event()
    allow_late_write = asyncio.Event()
    original_run_sync_fenced = execution_control_module.run_sync_fenced

    async def delay_release_ready_write(function, /, *args, **kwargs):
        value = args[-1] if args else None
        if isinstance(value, dict) and value.get("executionId") == old_control.execution_id:
            if value.get("releaseReady") is True:
                late_write_started.set()
                await allow_late_write.wait()
        return await original_run_sync_fenced(function, *args, **kwargs)

    monkeypatch.setattr(execution_control_module, "run_sync_fenced", delay_release_ready_write)
    replacement_service = ExecutionControlService(persistence_root=tmp_path, backup_service=None)
    replacement: ExecutionController | None = None
    persisted_path = tmp_path / "execution-control" / "ctx-1.json"
    try:
        observed = await old_control.finalize_natural_completion(
            task_id="task-1",
            completion_generation=completion_generation,
        )
        assert observed["naturalHandoff"] is not None
        await asyncio.wait_for(late_write_started.wait(), timeout=5)
        old_persisted_revision = old_control.persisted_revision
        assert old_persisted_revision < old_control.revision

        replacement = await replacement_service.begin_execution(
            context_id="ctx-1",
            task_id="task-2",
            owner="owner-1",
            cwd="/repo",
        )
        claimed = json.loads(persisted_path.read_text(encoding="utf-8"))
        assert claimed["executionId"] == replacement.execution_id
        assert claimed["serverInstanceId"] == replacement.server_instance_id
        assert claimed["phase"] == "running"

        allow_late_write.set()
        await _wait_for_condition(lambda: old_control.release_ready)

        persisted = json.loads(persisted_path.read_text(encoding="utf-8"))
        assert persisted["executionId"] == replacement.execution_id
        assert persisted["serverInstanceId"] == replacement.server_instance_id
        assert persisted["phase"] == "running"
        assert old_control.persisted_revision == old_persisted_revision
    finally:
        allow_late_write.set()
        current = asyncio.current_task()
        if replacement is not None and current is not None:
            await replacement.detach_task(current, execution_status="input-required")
        await asyncio.gather(old_service.close(), replacement_service.close())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mutation", "invalid_release_ready"),
    [
        pytest.param("missing", None, id="missing"),
        pytest.param("value", "false", id="string-false"),
        pytest.param("value", 0, id="integer-zero"),
    ],
)
async def test_restart_rejects_invalid_release_ready_with_complete_natural_handoff(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
    invalid_release_ready: object,
) -> None:
    persisted_path, persisted = await _persist_natural_handoff_before_release_ready(tmp_path, monkeypatch)
    if mutation == "missing":
        persisted.pop("releaseReady")
    else:
        persisted["releaseReady"] = invalid_release_ready
    execution_control_module.atomic_write_json(persisted_path, persisted)

    restarted = ExecutionControlService(persistence_root=tmp_path, backup_service=None)

    async def begin_replacement() -> None:
        replacement = await restarted.begin_execution(
            context_id="ctx-1",
            task_id="task-2",
            owner="owner-1",
            cwd="/repo",
        )
        current = asyncio.current_task()
        assert current is not None
        await replacement.detach_task(current, execution_status="input-required")

    try:
        with pytest.raises(ExecutionControlConflictError, match="active in another process"):
            await asyncio.create_task(begin_replacement())
    finally:
        await restarted.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invalid_input_handoff_ready",
    ["true", 1, None],
    ids=["string-true", "integer-one", "missing"],
)
async def test_restart_rejects_invalid_or_missing_persisted_input_handoff_ready(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    invalid_input_handoff_ready: object,
) -> None:
    persisted_path, persisted = await _persist_natural_handoff_before_release_ready(tmp_path, monkeypatch)
    if invalid_input_handoff_ready is None:
        persisted.pop("inputHandoffReady")
    else:
        persisted["inputHandoffReady"] = invalid_input_handoff_ready
    execution_control_module.atomic_write_json(persisted_path, persisted)

    restarted = ExecutionControlService(persistence_root=tmp_path, backup_service=None)

    async def begin_replacement() -> None:
        replacement = await restarted.begin_execution(
            context_id="ctx-1",
            task_id="task-2",
            owner="owner-1",
            cwd="/repo",
        )
        current = asyncio.current_task()
        assert current is not None
        await replacement.detach_task(current, execution_status="input-required")

    try:
        with pytest.raises(ExecutionControlConflictError, match="active in another process"):
            await asyncio.create_task(begin_replacement())
    finally:
        await restarted.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invalid_release_ready",
    ["false", 1],
    ids=["string-false", "integer-one"],
)
async def test_restart_rejects_non_boolean_release_ready_without_valid_handoff(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    invalid_release_ready: object,
) -> None:
    persisted_path, persisted = await _persist_natural_handoff_before_release_ready(tmp_path, monkeypatch)
    persisted["phase"] = "terminated"
    persisted["terminationReason"] = "explicit_terminate"
    persisted["releaseReady"] = invalid_release_ready
    persisted["naturalHandoff"] = None
    execution_control_module.atomic_write_json(persisted_path, persisted)

    restarted = ExecutionControlService(persistence_root=tmp_path, backup_service=None)

    async def begin_replacement() -> None:
        replacement = await restarted.begin_execution(
            context_id="ctx-1",
            task_id="task-2",
            owner="owner-1",
            cwd="/repo",
        )
        current = asyncio.current_task()
        assert current is not None
        await replacement.detach_task(current, execution_status="input-required")

    try:
        with pytest.raises(ExecutionControlConflictError, match="active in another process"):
            await asyncio.create_task(begin_replacement())
    finally:
        await restarted.close()


def _set_canonical_backup_disabled_handoff(persisted: dict) -> dict:
    receipt = persisted["naturalHandoff"]
    assert isinstance(receipt, dict)
    receipt.update(
        backupDisabled=True,
        businessRevision=0,
        pendingJobId=None,
        stagedCommitted=False,
        snapshotGeneration=None,
        snapshotCommitId=None,
    )
    persisted["backup"] = {"status": "disabled"}
    return receipt


def _set_backup_disabled_with_job_handoff(persisted: dict) -> dict:
    receipt = persisted["naturalHandoff"]
    assert isinstance(receipt, dict)
    receipt.update(
        backupDisabled=True,
        businessRevision=1,
        pendingJobId="job-disabled",
        stagedCommitted=False,
        snapshotGeneration=None,
        snapshotCommitId=None,
    )
    persisted["backup"] = {
        "status": "disabled",
        "jobId": "job-disabled",
        "businessRevision": 1,
        "generation": None,
        "commitId": None,
    }
    return receipt


@pytest.mark.asyncio
async def test_restart_admits_backup_disabled_natural_handoff_before_release_ready(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    persisted_path, persisted = await _persist_natural_handoff_before_release_ready(tmp_path, monkeypatch)
    _set_canonical_backup_disabled_handoff(persisted)
    execution_control_module.atomic_write_json(persisted_path, persisted)

    restarted = ExecutionControlService(persistence_root=tmp_path, backup_service=None)
    try:
        replacement = await restarted.begin_execution(
            context_id="ctx-1",
            task_id="task-2",
            owner="owner-1",
            cwd="/repo",
        )
        assert replacement.execution_id != persisted["executionId"]
        current = asyncio.current_task()
        assert current is not None
        await replacement.detach_task(current, execution_status="input-required")
    finally:
        await restarted.close()


@pytest.mark.asyncio
async def test_restart_admits_backup_disabled_handoff_overwritten_by_legacy_committed_backup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A handoff that registered no snapshot job still runs the legacy directory
    # backup, and ``_perform_backup`` then overwrites the ``disabled`` marker with
    # its own committed result.  That durable outcome must admit replacement across
    # a restart, exactly like the marker it replaced.
    persisted_path, persisted = await _persist_natural_handoff_before_release_ready(tmp_path, monkeypatch)
    _set_canonical_backup_disabled_handoff(persisted)
    persisted["backup"] = {"status": "shared_committed", "generation": 3, "commitId": "commit-3", "error": None}
    execution_control_module.atomic_write_json(persisted_path, persisted)

    restarted = ExecutionControlService(persistence_root=tmp_path, backup_service=None)
    try:
        replacement = await restarted.begin_execution(
            context_id="ctx-1",
            task_id="task-2",
            owner="owner-1",
            cwd="/repo",
        )
        assert replacement.execution_id != persisted["executionId"]
        current = asyncio.current_task()
        assert current is not None
        await replacement.detach_task(current, execution_status="input-required")
    finally:
        await restarted.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "legacy_backup",
    [
        pytest.param(
            {"status": "blocked", "generation": None, "commitId": None, "error": "backup failed"},
            id="blocked",
        ),
        pytest.param(
            {"status": "shared_committed", "generation": 3, "commitId": "commit-3", "error": "partial"},
            id="committed-with-error",
        ),
    ],
)
async def test_restart_rejects_backup_disabled_handoff_with_uncommitted_legacy_backup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    legacy_backup: dict,
) -> None:
    persisted_path, persisted = await _persist_natural_handoff_before_release_ready(tmp_path, monkeypatch)
    _set_canonical_backup_disabled_handoff(persisted)
    persisted["backup"] = legacy_backup
    execution_control_module.atomic_write_json(persisted_path, persisted)

    restarted = ExecutionControlService(persistence_root=tmp_path, backup_service=None)

    async def begin_replacement() -> None:
        replacement = await restarted.begin_execution(
            context_id="ctx-1",
            task_id="task-2",
            owner="owner-1",
            cwd="/repo",
        )
        current = asyncio.current_task()
        assert current is not None
        await replacement.detach_task(current, execution_status="input-required")

    try:
        with pytest.raises(ExecutionControlConflictError, match="active in another process"):
            await asyncio.create_task(begin_replacement())
    finally:
        await restarted.close()


@pytest.mark.asyncio
async def test_restart_admits_backup_disabled_natural_handoff_with_registered_job(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    persisted_path, persisted = await _persist_natural_handoff_before_release_ready(tmp_path, monkeypatch)
    _set_backup_disabled_with_job_handoff(persisted)
    execution_control_module.atomic_write_json(persisted_path, persisted)

    restarted = ExecutionControlService(persistence_root=tmp_path, backup_service=None)
    try:
        replacement = await restarted.begin_execution(
            context_id="ctx-1",
            task_id="task-2",
            owner="owner-1",
            cwd="/repo",
        )
        assert replacement.execution_id != persisted["executionId"]
        current = asyncio.current_task()
        assert current is not None
        await replacement.detach_task(current, execution_status="input-required")
    finally:
        await restarted.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("target", "field", "invalid_value"),
    [
        pytest.param("backup", "businessRevision", True, id="document-business-revision-boolean"),
        pytest.param("backup", "businessRevision", 1.0, id="document-business-revision-float"),
        pytest.param("backup", "jobId", "job-other", id="document-job-mismatch"),
        pytest.param("backup", "generation", 1, id="document-generation-residue"),
        pytest.param("backup", "commitId", "commit-residue", id="document-commit-residue"),
        pytest.param("backup", "extra", "residue", id="document-extra-residue"),
        pytest.param("receipt", "pendingJobId", "job-other", id="receipt-job-mismatch"),
        pytest.param("receipt", "businessRevision", 2, id="receipt-revision-mismatch"),
        pytest.param("receipt", "stagedCommitted", True, id="receipt-staged-residue"),
        pytest.param("receipt", "snapshotGeneration", 1, id="receipt-generation-residue"),
        pytest.param("receipt", "snapshotCommitId", "commit-residue", id="receipt-commit-residue"),
    ],
)
async def test_restart_rejects_backup_disabled_handoff_with_invalid_registered_job_shape(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    target: str,
    field: str,
    invalid_value: object,
) -> None:
    persisted_path, persisted = await _persist_natural_handoff_before_release_ready(tmp_path, monkeypatch)
    receipt = _set_backup_disabled_with_job_handoff(persisted)
    document = persisted["backup"]
    assert isinstance(document, dict)
    (document if target == "backup" else receipt)[field] = invalid_value
    execution_control_module.atomic_write_json(persisted_path, persisted)

    restarted = ExecutionControlService(persistence_root=tmp_path, backup_service=None)

    async def begin_replacement() -> None:
        replacement = await restarted.begin_execution(
            context_id="ctx-1",
            task_id="task-2",
            owner="owner-1",
            cwd="/repo",
        )
        current = asyncio.current_task()
        assert current is not None
        await replacement.detach_task(current, execution_status="input-required")

    try:
        with pytest.raises(ExecutionControlConflictError, match="active in another process"):
            await asyncio.create_task(begin_replacement())
    finally:
        await restarted.close()


@pytest.mark.asyncio
async def test_restart_rejects_legacy_pending_backup_with_backup_disabled_handoff(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    persisted_path, persisted = await _persist_natural_handoff_before_release_ready(tmp_path, monkeypatch)
    _set_canonical_backup_disabled_handoff(persisted)
    persisted["backup"] = {"status": "pending"}
    execution_control_module.atomic_write_json(persisted_path, persisted)

    restarted = ExecutionControlService(persistence_root=tmp_path, backup_service=None)

    async def begin_replacement() -> None:
        replacement = await restarted.begin_execution(
            context_id="ctx-1",
            task_id="task-2",
            owner="owner-1",
            cwd="/repo",
        )
        current = asyncio.current_task()
        assert current is not None
        await replacement.detach_task(current, execution_status="input-required")

    try:
        with pytest.raises(ExecutionControlConflictError, match="active in another process"):
            await asyncio.create_task(begin_replacement())
    finally:
        await restarted.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("residual_field", "residual_value"),
    [
        pytest.param("jobId", "job-1", id="job-id"),
        pytest.param("businessRevision", 1, id="business-revision"),
        pytest.param("generation", 1, id="generation"),
        pytest.param("commitId", "commit-1", id="commit-id"),
    ],
)
async def test_restart_rejects_backup_disabled_handoff_with_document_backup_residue(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    residual_field: str,
    residual_value: object,
) -> None:
    persisted_path, persisted = await _persist_natural_handoff_before_release_ready(tmp_path, monkeypatch)
    _set_canonical_backup_disabled_handoff(persisted)
    persisted["backup"][residual_field] = residual_value
    execution_control_module.atomic_write_json(persisted_path, persisted)

    restarted = ExecutionControlService(persistence_root=tmp_path, backup_service=None)

    async def begin_replacement() -> None:
        replacement = await restarted.begin_execution(
            context_id="ctx-1",
            task_id="task-2",
            owner="owner-1",
            cwd="/repo",
        )
        current = asyncio.current_task()
        assert current is not None
        await replacement.detach_task(current, execution_status="input-required")

    try:
        with pytest.raises(ExecutionControlConflictError, match="active in another process"):
            await asyncio.create_task(begin_replacement())
    finally:
        await restarted.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("receipt_field", "invalid_value"),
    [
        ("businessRevision", 1),
        ("businessRevision", False),
        ("businessRevision", "0"),
        ("pendingJobId", "job-1"),
        ("stagedCommitted", True),
        ("stagedCommitted", 0),
        ("snapshotGeneration", 1),
        ("snapshotCommitId", "commit-1"),
    ],
    ids=[
        "business-revision-residue",
        "business-revision-bool",
        "business-revision-string",
        "pending-job-id-residue",
        "staged-committed-residue",
        "staged-committed-integer",
        "snapshot-generation-residue",
        "snapshot-commit-id-residue",
    ],
)
async def test_restart_rejects_backup_disabled_handoff_with_noncanonical_receipt_proof(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    receipt_field: str,
    invalid_value: object,
) -> None:
    persisted_path, persisted = await _persist_natural_handoff_before_release_ready(tmp_path, monkeypatch)
    receipt = _set_canonical_backup_disabled_handoff(persisted)
    receipt[receipt_field] = invalid_value
    execution_control_module.atomic_write_json(persisted_path, persisted)

    restarted = ExecutionControlService(persistence_root=tmp_path, backup_service=None)

    async def begin_replacement() -> None:
        replacement = await restarted.begin_execution(
            context_id="ctx-1",
            task_id="task-2",
            owner="owner-1",
            cwd="/repo",
        )
        current = asyncio.current_task()
        assert current is not None
        await replacement.detach_task(current, execution_status="input-required")

    try:
        with pytest.raises(ExecutionControlConflictError, match="active in another process"):
            await asyncio.create_task(begin_replacement())
    finally:
        await restarted.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("backup_field", "invalid_value"),
    [
        pytest.param("businessRevision", True, id="business-revision-boolean"),
        pytest.param("businessRevision", "1", id="business-revision-string"),
        pytest.param("businessRevision", 0, id="business-revision-zero"),
        pytest.param("generation", True, id="generation-boolean"),
        pytest.param("generation", "1", id="generation-string"),
        pytest.param("generation", 0, id="generation-zero"),
    ],
)
async def test_restart_rejects_staged_handoff_with_invalid_document_backup_integer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    backup_field: str,
    invalid_value: object,
) -> None:
    persisted_path, persisted = await _persist_natural_handoff_before_release_ready(tmp_path, monkeypatch)
    backup = persisted["backup"]
    assert isinstance(backup, dict)
    backup[backup_field] = invalid_value
    execution_control_module.atomic_write_json(persisted_path, persisted)

    restarted = ExecutionControlService(persistence_root=tmp_path, backup_service=None)

    async def begin_replacement() -> None:
        replacement = await restarted.begin_execution(
            context_id="ctx-1",
            task_id="task-2",
            owner="owner-1",
            cwd="/repo",
        )
        current = asyncio.current_task()
        assert current is not None
        await replacement.detach_task(current, execution_status="input-required")

    try:
        with pytest.raises(ExecutionControlConflictError, match="active in another process"):
            await asyncio.create_task(begin_replacement())
    finally:
        await restarted.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("backup_field", "mismatched_value"),
    [
        ("jobId", "job-other"),
        ("businessRevision", 999),
        ("generation", 999),
        ("commitId", "commit-other"),
    ],
    ids=["job-id", "business-revision", "generation", "commit-id"],
)
async def test_restart_rejects_staged_handoff_when_document_backup_proof_mismatches_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    backup_field: str,
    mismatched_value: object,
) -> None:
    persisted_path, persisted = await _persist_natural_handoff_before_release_ready(tmp_path, monkeypatch)
    backup = persisted["backup"]
    assert isinstance(backup, dict)
    backup[backup_field] = mismatched_value
    execution_control_module.atomic_write_json(persisted_path, persisted)

    restarted = ExecutionControlService(persistence_root=tmp_path, backup_service=None)

    async def begin_replacement() -> None:
        replacement = await restarted.begin_execution(
            context_id="ctx-1",
            task_id="task-2",
            owner="owner-1",
            cwd="/repo",
        )
        current = asyncio.current_task()
        assert current is not None
        await replacement.detach_task(current, execution_status="input-required")

    try:
        with pytest.raises(ExecutionControlConflictError, match="active in another process"):
            await asyncio.create_task(begin_replacement())
    finally:
        await restarted.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mutation", "field", "invalid_value"),
    [
        ("receipt_value", "version", "unsupported-natural-handoff"),
        ("receipt_value", "contextId", "ctx-other"),
        ("receipt_value", "taskId", "task-other"),
        ("receipt_value", "executionId", "exec-other"),
        ("missing_receipt_field", "completionGeneration", None),
        ("receipt_value", "completionGeneration", 0),
        ("receipt_value", "completionGeneration", True),
        ("receipt_value", "ownerGeneration", 999),
        ("receipt_value", "ownerGeneration", True),
        ("receipt_value", "businessDrained", False),
        ("missing_receipt_field", "pendingJobId", None),
        ("missing_receipt_field", "businessRevision", None),
        ("missing_receipt_field", "stagedCommitted", None),
        ("missing_receipt_field", "snapshotGeneration", None),
        ("missing_receipt_field", "snapshotCommitId", None),
        ("document_value", "terminationReason", "explicit_terminate"),
    ],
    ids=[
        "wrong-version",
        "wrong-context-id",
        "wrong-task-id",
        "wrong-execution-id",
        "missing-completion-generation",
        "zero-completion-generation",
        "boolean-completion-generation",
        "wrong-owner-generation",
        "boolean-owner-generation",
        "business-not-drained",
        "missing-pending-job-id",
        "missing-business-revision",
        "missing-staged-committed",
        "missing-snapshot-generation",
        "missing-snapshot-commit-id",
        "explicit-termination",
    ],
)
async def test_restart_rejects_invalid_persisted_natural_handoff_before_release_ready(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
    field: str,
    invalid_value: object,
) -> None:
    persisted_path, persisted = await _persist_natural_handoff_before_release_ready(tmp_path, monkeypatch)
    receipt = persisted["naturalHandoff"]
    assert isinstance(receipt, dict)
    if mutation == "receipt_value":
        receipt[field] = invalid_value
    elif mutation == "missing_receipt_field":
        receipt.pop(field)
    else:
        persisted[field] = invalid_value
    execution_control_module.atomic_write_json(persisted_path, persisted)

    restarted = ExecutionControlService(persistence_root=tmp_path, backup_service=None)

    async def begin_replacement() -> None:
        replacement = await restarted.begin_execution(
            context_id="ctx-1",
            task_id="task-2",
            owner="owner-1",
            cwd="/repo",
        )
        current = asyncio.current_task()
        assert current is not None
        await replacement.detach_task(current, execution_status="input-required")

    try:
        with pytest.raises(ExecutionControlConflictError, match="active in another process"):
            await asyncio.create_task(begin_replacement())
    finally:
        await restarted.close()


@pytest.mark.asyncio
async def test_natural_handoff_reentry_waits_for_in_flight_commit_then_admits(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    coordinator = _RecordingCoordinator()
    service = ExecutionControlService(
        persistence_root=tmp_path,
        backup_service=None,
        backup_coordinator=coordinator,
    )
    control, completion_generation = await _prepare_natural_handoff(service, session_id="session-1")
    commit_started = threading.Event()
    allow_commit = threading.Event()
    original_write = execution_control_module.atomic_write_json

    def block_handoff_commit(path: Path, value: dict) -> None:
        if value.get("naturalHandoff") is not None:
            commit_started.set()
            assert allow_commit.wait(5)
        original_write(path, value)

    monkeypatch.setattr(execution_control_module, "atomic_write_json", block_handoff_commit)
    finalizing = asyncio.create_task(
        control.finalize_natural_completion(
            task_id="task-1",
            completion_generation=completion_generation,
        )
    )
    try:
        assert await asyncio.to_thread(commit_started.wait, 5)
        # The handoff is not visible until its snapshot commit is persisted.
        assert control.natural_handoff_receipt() is None
        assert control.snapshot()["naturalHandoff"] is None
        assert control.natural_handoff_admits_replacement() is False

        # A fresh next turn no longer fail-closes while the commit is in flight;
        # it waits for the commit to persist, then is admitted. The wait keeps
        # the safety invariant intact: a next turn never starts from
        # un-persisted state, it only stops surfacing SOURCE_OPEN mid-commit.
        replacing = asyncio.create_task(
            service.begin_execution(
                context_id="ctx-1",
                task_id="task-2",
                owner="owner-1",
                cwd="/repo",
            )
        )
        await asyncio.sleep(0.3)
        assert not replacing.done()

        allow_commit.set()
        await asyncio.wait_for(finalizing, 5)
        replacement = await asyncio.wait_for(replacing, 5)
        assert replacement is not control
        assert replacement.natural_handoff_admits_replacement() is False
        current = asyncio.current_task()
        assert current is not None
        await replacement.detach_task(current, execution_status="input-required")
    finally:
        allow_commit.set()
        await asyncio.gather(finalizing, return_exceptions=True)
        await service.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("backup_enabled", [False, True])
async def test_natural_handoff_snapshot_failure_is_fail_closed_when_backup_is_enabled_or_disabled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    backup_enabled: bool,
) -> None:
    coordinator = _RecordingCoordinator() if backup_enabled else None
    service = ExecutionControlService(
        persistence_root=tmp_path,
        backup_service=None,
        backup_coordinator=coordinator,
    )
    control, completion_generation = await _prepare_natural_handoff(
        service,
        session_id="session-1" if backup_enabled else None,
    )
    original_write = execution_control_module.atomic_write_json

    def fail_handoff_commit(path: Path, value: dict) -> None:
        if value.get("naturalHandoff") is not None:
            raise OSError("injected natural handoff snapshot failure")
        original_write(path, value)

    monkeypatch.setattr(execution_control_module, "atomic_write_json", fail_handoff_commit)
    try:
        observed = await control.finalize_natural_completion(
            task_id="task-1",
            completion_generation=completion_generation,
        )

        assert observed["commitError"] == "state_commit_failed"
        assert observed["releaseReady"] is False
        assert observed["naturalHandoff"] is None
        assert control.natural_handoff_receipt() is None
        assert control.natural_handoff_admits_replacement() is False
        with pytest.raises(ExecutionControlConflictError):
            await service.begin_execution(
                context_id="ctx-1",
                task_id="task-2",
                owner="owner-1",
                cwd="/repo",
            )
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_natural_handoff_retry_publishes_only_the_receipt_that_was_persisted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = ExecutionControlService(persistence_root=tmp_path, backup_service=None)
    control, completion_generation = await _prepare_natural_handoff(service, session_id=None)
    fail_handoff_commit = True
    original_write = execution_control_module.atomic_write_json

    def injected_write(path: Path, value: dict) -> None:
        if fail_handoff_commit and value.get("naturalHandoff") is not None:
            raise OSError("injected natural handoff snapshot failure")
        original_write(path, value)

    monkeypatch.setattr(execution_control_module, "atomic_write_json", injected_write)
    try:
        await control.finalize_natural_completion(
            task_id="task-1",
            completion_generation=completion_generation,
        )
        assert control.natural_handoff_receipt() is None
        assert control.natural_handoff_admits_replacement() is False

        fail_handoff_commit = False
        await control.observe_state()
        await _wait_for_condition(control.natural_handoff_admits_replacement)
        # Admission becomes true after revision 4 is durable. A separate
        # release-ready commit may already be writing revision 5, so compare
        # file and in-memory revisions only after that public commit boundary.
        await _wait_for_condition(lambda: control.release_ready)

        receipt = control.natural_handoff_receipt()
        assert receipt is not None
        persisted_path = tmp_path / "execution-control" / "ctx-1.json"
        persisted = json.loads(persisted_path.read_text(encoding="utf-8"))
        assert persisted["naturalHandoff"] == receipt
        assert persisted["persistedRevision"] == persisted["revision"]
        assert control.snapshot()["persistedRevision"] == persisted["persistedRevision"]

        restarted = ExecutionControlService(persistence_root=tmp_path, backup_service=None)
        try:
            reloaded = restarted.snapshot_for_context("ctx-1")
            assert reloaded is not None
            assert reloaded["naturalHandoff"] == receipt
            assert reloaded["persistedRevision"] == persisted["persistedRevision"]
        finally:
            await restarted.close()
    finally:
        await service.close()


async def _natural_handoff_turn(
    service: ExecutionControlService,
    *,
    task_id: str,
    session_id: str | None = "session-1",
) -> ExecutionController:
    control = await service.begin_execution(
        context_id="ctx-1",
        task_id=task_id,
        owner="owner-1",
        cwd="/repo",
    )
    if session_id is not None:
        control.bind_session(session_id)
    current = asyncio.current_task()
    assert current is not None
    completion_generation = await control.detach_task(
        current,
        execution_status="input-required",
        natural_completion=True,
    )
    assert completion_generation is not None
    await control.finalize_natural_completion(task_id=task_id, completion_generation=completion_generation)
    return control


@pytest.mark.asyncio
async def test_ordinary_next_turn_starts_against_a_retired_natural_handoff_without_release_ready(
    tmp_path: Path,
) -> None:
    coordinator = _RecordingCoordinator()
    service = ExecutionControlService(
        persistence_root=None,
        backup_service=None,
        backup_coordinator=coordinator,
    )
    first = await _natural_handoff_turn(service, task_id="task-1")
    # Keep the release commit pending so admission cannot depend on it.
    first.release_ready = False
    first._release_commit_inflight = True

    assert first.natural_handoff_admits_replacement() is True
    next_turn = await service.begin_execution(
        context_id="ctx-1",
        task_id="task-2",
        owner="owner-1",
        cwd="/repo",
    )

    assert next_turn is not first
    assert next_turn.owner_generation > first.owner_generation
    assert first.release_ready is False
    assert coordinator.quiescence_waits == ["session-1"]
    await next_turn.detach_task(asyncio.current_task(), execution_status="input-required")
    await service.close()


@pytest.mark.asyncio
async def test_paused_or_explicitly_terminating_control_still_rejects_an_ordinary_next_turn(tmp_path: Path) -> None:
    service = ExecutionControlService(persistence_root=None, backup_service=None)
    paused_control = await service.begin_execution(
        context_id="ctx-paused",
        task_id="task-1",
        owner="owner-1",
        cwd=str(tmp_path),
    )
    current = asyncio.current_task()
    assert current is not None
    paused = await paused_control.pause(
        task_id="task-1",
        expected_execution_id=paused_control.execution_id,
        request_id="pause",
        connection_epoch=1,
        reason="client_disconnected",
        reconnect_timeout_seconds=30,
    )
    assert paused["phase"] in {"pausing", "pause_committing", "paused"}

    with pytest.raises(ExecutionControlConflictError):
        await service.begin_execution(
            context_id="ctx-paused",
            task_id="task-2",
            owner="owner-1",
            cwd=str(tmp_path),
        )

    terminating_control = await service.begin_execution(
        context_id="ctx-terminating",
        task_id="task-1",
        owner="owner-1",
        cwd=str(tmp_path),
    )
    await terminating_control.detach_task(current, execution_status="input-required")
    terminating_control.phase = "terminated"
    terminating_control.termination_reason = "explicit_terminate"
    terminating_control.backup = {"status": "blocked", "error": "staging unavailable"}
    terminating_control.release_ready = False

    assert terminating_control.natural_handoff_admits_replacement() is False
    with pytest.raises(ExecutionControlConflictError):
        await service.begin_execution(
            context_id="ctx-terminating",
            task_id="task-2",
            owner="owner-1",
            cwd=str(tmp_path),
        )
    await paused_control.detach_task(current, execution_status="canceled")
    await service.close()


@pytest.mark.asyncio
async def test_retired_natural_handoff_receipt_is_never_answered_with_a_newer_execution(tmp_path: Path) -> None:
    coordinator = _RecordingCoordinator()
    service = ExecutionControlService(
        persistence_root=None,
        backup_service=None,
        backup_coordinator=coordinator,
    )
    first = await _natural_handoff_turn(service, task_id="task-1")
    first_receipt = first.natural_handoff_receipt()
    assert first_receipt is not None
    second = await _natural_handoff_turn(service, task_id="task-2")
    second_receipt = second.natural_handoff_receipt()

    assert second is not first
    assert second_receipt is not None
    assert service.natural_handoff_receipt(first.execution_id) == first_receipt
    assert service.natural_handoff_receipt(second.execution_id) == second_receipt
    assert first_receipt["pendingJobId"] != second_receipt["pendingJobId"]
    assert first_receipt["ownerGeneration"] < second_receipt["ownerGeneration"]
    assert service.natural_handoff_receipt("exec-unknown") is None
    await service.close()


def _persist_dead_owner_claim_remnant(tmp_path: Path, *, revision: int = 0) -> dict:
    """Persist the running claim remnant a killed publisher leaves behind."""
    control_path = tmp_path / "execution-control" / "ctx-1.json"
    control_path.parent.mkdir(parents=True, exist_ok=True)
    document = {
        "contextId": "ctx-1",
        "taskId": "task-1",
        "executionId": "exec-1",
        "owner": "owner-1",
        "ownerGeneration": 1,
        "serverInstanceId": "instance-1",
        "ownerPid": 999999,
        "pauseId": None,
        "pauseReason": None,
        "connectionEpoch": -1,
        "revision": revision,
        "persistedRevision": revision,
        "phase": "running",
        "pauseComplete": False,
        "executionStatus": "working",
        "streamAvailable": True,
        "blockers": [{"kind": "execution", "count": 1}],
        "expiresAt": None,
        "terminationReason": None,
        "commitError": None,
        "backup": {"status": "not_requested"},
        "externalOperations": [],
        "releaseReady": False,
        "naturalHandoff": None,
        "inputHandoffReady": False,
        "localInputContinuationReady": False,
    }
    execution_control_module.atomic_write_json(control_path, document)
    return document


@pytest.mark.asyncio
async def test_recovery_admits_terminated_natural_handoff_before_release_ready(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A killed natural finalization still durably proves its input wait may be recovered.

    The receipt is committed before the release marker, so recovery must accept it
    exactly like replacement does; otherwise the context is permanently unusable.
    """

    persisted_path, persisted = await _persist_natural_handoff_before_release_ready(tmp_path, monkeypatch)
    _set_canonical_backup_disabled_handoff(persisted)
    execution_control_module.atomic_write_json(persisted_path, persisted)

    recovering = ExecutionControlService(persistence_root=tmp_path, backup_service=None)
    try:
        admission = await recovering.reserve_recoverable_input_continuation(
            context_id="ctx-1",
            task_id="task-1",
            owner="owner-1",
        )
        assert admission is not None
        recovered = await recovering.begin_execution(
            context_id="ctx-1",
            task_id="task-1",
            owner="owner-1",
            cwd="/repo",
            continue_input_required=True,
            recoverable_input_admission=admission,
        )
        assert recovered.phase == "running"
        current = asyncio.current_task()
        assert current is not None
        await recovered.detach_task(current, execution_status="input-required")
    finally:
        await recovering.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("revision", [0, 1], ids=["revision-zero", "revision-one"])
async def test_recovery_admits_claim_remnant_whose_owner_process_is_gone(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    revision: int,
) -> None:
    _persist_dead_owner_claim_remnant(tmp_path, revision=revision)
    monkeypatch.setattr(execution_control_module, "_pid_alive", lambda pid: False)

    recovering = ExecutionControlService(persistence_root=tmp_path, backup_service=None)
    try:
        admission = await recovering.reserve_recoverable_input_continuation(
            context_id="ctx-1",
            task_id="task-1",
            owner="owner-1",
        )
        assert admission is not None
        recovered = await recovering.begin_execution(
            context_id="ctx-1",
            task_id="task-1",
            owner="owner-1",
            cwd="/repo",
            continue_input_required=True,
            recoverable_input_admission=admission,
        )
        assert recovered.phase == "running"
        current = asyncio.current_task()
        assert current is not None
        await recovered.detach_task(current, execution_status="input-required")
    finally:
        await recovering.close()


@pytest.mark.asyncio
async def test_recovery_rejects_claim_remnant_whose_owner_process_is_alive(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _persist_dead_owner_claim_remnant(tmp_path)
    monkeypatch.setattr(execution_control_module, "_pid_alive", lambda pid: True)

    recovering = ExecutionControlService(persistence_root=tmp_path, backup_service=None)
    try:
        assert (
            await recovering.reserve_recoverable_input_continuation(
                context_id="ctx-1",
                task_id="task-1",
                owner="owner-1",
            )
            is None
        )
    finally:
        await recovering.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mutation", "value"),
    [
        pytest.param("drop-owner-pid", None, id="missing-owner-pid"),
        pytest.param("revision-mismatch", 1, id="persisted-revision-lagging"),
        pytest.param("phase", "terminating", id="phase-terminating"),
    ],
)
async def test_recovery_rejects_claim_remnant_without_dead_owner_proof(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
    value: object,
) -> None:
    document = _persist_dead_owner_claim_remnant(tmp_path)
    control_path = tmp_path / "execution-control" / "ctx-1.json"
    if mutation == "drop-owner-pid":
        document.pop("ownerPid")
    elif mutation == "revision-mismatch":
        document["revision"] = value
    else:
        document["phase"] = value
    execution_control_module.atomic_write_json(control_path, document)
    monkeypatch.setattr(execution_control_module, "_pid_alive", lambda pid: False)

    recovering = ExecutionControlService(persistence_root=tmp_path, backup_service=None)
    try:
        assert (
            await recovering.reserve_recoverable_input_continuation(
                context_id="ctx-1",
                task_id="task-1",
                owner="owner-1",
            )
            is None
        )
    finally:
        await recovering.close()


@pytest.mark.asyncio
async def test_recovery_rejects_terminated_control_without_release_or_handoff_proof(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    persisted_path, persisted = await _persist_natural_handoff_before_release_ready(tmp_path, monkeypatch)
    persisted["naturalHandoff"] = None
    persisted["terminationReason"] = None
    execution_control_module.atomic_write_json(persisted_path, persisted)

    recovering = ExecutionControlService(persistence_root=tmp_path, backup_service=None)
    try:
        assert (
            await recovering.reserve_recoverable_input_continuation(
                context_id="ctx-1",
                task_id="task-1",
                owner="owner-1",
            )
            is None
        )
    finally:
        await recovering.close()
