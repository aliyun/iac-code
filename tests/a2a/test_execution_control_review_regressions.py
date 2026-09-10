"""Permission continuations, slow terminal storage, and connection-hold retention."""

import asyncio
import json
import threading
import time
from types import SimpleNamespace

import pytest
from a2a.types import Message as A2AMessage
from a2a.types import Part, Role, Task, TaskState, TaskStatus

from iac_code.a2a.execution_control import (
    ExecutionController,
    ExecutionControlService,
    bind_execution_control,
    reset_execution_control,
)
from iac_code.a2a.executor import IacCodeA2AExecutor
from iac_code.a2a.input_required import PERMISSION_QUERY_PREFIX
from iac_code.a2a.persistence import A2APersistenceStore
from iac_code.a2a.task_store import A2ATaskStore
from iac_code.agent.agent_loop import AgentLoop
from iac_code.agent.message import Message, ToolUseBlock
from iac_code.services.permission_wait import canonical_digest
from iac_code.services.session_backup import BackupReason, SessionBackupService
from iac_code.services.session_backup_staging import SessionBackupStagingWorker, StagedSessionBackupService
from iac_code.tools.base import ToolRegistry
from iac_code.types.stream_events import MessageEndEvent, PermissionRequestEvent, TextDeltaEvent, Usage
from tests.agent.test_agent_loop_permissions import WriteTool

from .fakes import FakeEventQueue, FakeRequestContext, FakeRuntime
from .test_execution_control_regressions import publish_staged_backups, wait_until


@pytest.fixture(autouse=True)
def isolated_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("IAC_CODE_MODE", "normal")
    monkeypatch.delenv("IAC_CODE_CONFIG_BACKUP_DIR", raising=False)
    monkeypatch.delenv("IAC_CODE_CONFIG_BACKUP_TMP_DIR", raising=False)


def make_executor(tmp_path, backup):
    store = A2ATaskStore(persistence=A2APersistenceStore(tmp_path / "a2a"), backup_service=backup)
    service = ExecutionControlService(persistence_root=tmp_path / "a2a", backup_service=backup)
    store.set_execution_control_provider(service.snapshot_for_context, service.has_active_work)
    executor = IacCodeA2AExecutor(
        task_store=store, model="test", backup_service=backup, execution_control_service=service
    )
    return executor, store, service


@pytest.mark.asyncio
@pytest.mark.parametrize("staged", [False, True])
@pytest.mark.parametrize("finished", [False, True])
async def test_permission_continuation_termination_commits_actual_result(tmp_path, monkeypatch, staged, finished):
    shared = tmp_path / "shared"
    monkeypatch.setenv("IAC_CODE_CONFIG_BACKUP_DIR", str(shared))
    backup = StagedSessionBackupService(tmp_path / "staging") if staged else SessionBackupService()
    executor, store, service = make_executor(tmp_path, backup)
    started = asyncio.Event()
    backup_started, release_backup = threading.Event(), threading.Event()
    future = asyncio.get_running_loop().create_future()

    class PermissionLoop:
        async def run_streaming(self, _prompt):
            yield PermissionRequestEvent(
                tool_name="bash",
                tool_input={"cmd": "pwd"},
                tool_use_id="tool-1",
                response_future=future,
                continuation_frame={
                    "assistantMessageRef": "session.jsonl:0",
                    "assistantMessageDigest": "a" * 64,
                    "orderedToolUseIds": ["tool-1"],
                    "currentIndex": 0,
                    "decisions": [{"toolUseId": "tool-1", "state": "pending", "source": None, "deniedResult": None}],
                },
            )
            started.set()
            if not finished:
                await asyncio.Event().wait()
            yield TextDeltaEvent(text="finished continuation")

    monkeypatch.setattr(
        "iac_code.a2a.executor.create_agent_runtime",
        lambda options: FakeRuntime(agent_loop=PermissionLoop(), session_id=options.session_id),
    )
    monkeypatch.setattr("iac_code.a2a.input_required.emit_permission_boundary_audit", lambda *_args, **_kwargs: True)
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
    continuation = None
    try:
        metadata = {"iac_code": {"cwd": str(tmp_path)}}
        await executor.execute(FakeRequestContext(metadata=metadata), FakeEventQueue())
        pending = next(iter(executor._permission_input_registry._pending.values()))
        response = {
            "schemaVersion": 1,
            "kind": "permission",
            "requestTaskId": pending.task_id,
            "contextId": pending.context_id,
            "inputId": pending.input_id,
            "toolUseId": "tool-1",
            "decision": "allow_once",
        }
        context = FakeRequestContext(metadata=metadata)
        context.message = A2AMessage(
            message_id="reply",
            context_id=pending.context_id,
            role=Role.ROLE_USER,
            parts=[Part(text=PERMISSION_QUERY_PREFIX + " " + json.dumps(response))],
        )
        queue = FakeEventQueue()
        continuation = asyncio.create_task(executor.execute(context, queue))
        await asyncio.wait_for(started.wait(), 3)
        if finished:
            assert await asyncio.to_thread(backup_started.wait, 3)
        control = service.get_for_context("ctx-1")
        await control.terminate(
            execution_id=control.execution_id, request_id="terminate", connection_epoch=1, reason="explicit_terminate"
        )
        release_backup.set()
        await wait_until(lambda: control.release_ready)
        await continuation
        expected = "input-required" if finished else "canceled"
        assert control.execution_status == expected
        assert (await store.get_task_record("task-1")).state == expected
        assert store._persistence.load_task("task-1").state == expected
        assert json.loads(next(shared.rglob("a2a/task.json")).read_text(encoding="utf-8"))["state"] == expected
        assert store._persistence.load_context("ctx-1").active_task_id is None
        assert queue.events[-1].status.state == (
            TaskState.TASK_STATE_INPUT_REQUIRED if finished else TaskState.TASK_STATE_CANCELED
        )
    finally:
        release_backup.set()
        if continuation is not None:
            continuation.cancel()
            await asyncio.gather(continuation, return_exceptions=True)
        await service.close()
        if publisher is not None:
            publisher.cancel()
            await asyncio.gather(publisher, return_exceptions=True)
        await store.stop_cleanup_loop()


@pytest.mark.asyncio
async def test_recovered_permission_batch_waits_for_connection_resume(tmp_path):
    control = ExecutionController(
        context_id="ctx",
        task_id="task",
        owner="",
        cwd=str(tmp_path),
        server_instance_id="test",
        persistence_path=None,
        backup_service=None,
    )
    paused = await control.pause(
        task_id="task",
        expected_execution_id=control.execution_id,
        request_id="pause",
        connection_epoch=1,
        reason="disconnect",
        reconnect_timeout_seconds=60,
    )
    await wait_until(lambda: control.phase == "paused")
    executions = []

    class RecordingTool(WriteTool):
        async def execute(self, **kwargs):
            executions.append(control.phase)
            return await super().execute(**kwargs)

    class Provider:
        def get_model_name(self):
            return "fake"

        async def stream(self, *_args, **_kwargs):
            yield MessageEndEvent(stop_reason="end_turn", usage=Usage())

    registry = ToolRegistry()
    registry.register(RecordingTool())
    assistant = Message(role="assistant", content=[ToolUseBlock(id="tool-1", name="write_test", input={"value": "ok"})])
    loop = AgentLoop(
        provider_manager=Provider(),
        system_prompt="test",
        tool_registry=registry,
        max_turns=1,
        resume_messages=[assistant],
        cwd=str(tmp_path),
    )
    checkpoint = {
        "toolUseId": "tool-1",
        "payloadDigest": canonical_digest({"name": "write_test", "input": {"value": "ok"}}),
        "decision": {"status": "claimed", "value": "allow_once", "claimId": "claim-1"},
        "continuationFrame": {
            "assistantMessageRef": "session.jsonl:0",
            "assistantMessageDigest": canonical_digest([block.model_dump(mode="json") for block in assistant.content]),
            "orderedToolUseIds": ["tool-1"],
            "currentIndex": 0,
            "decisions": [{"toolUseId": "tool-1", "state": "pending", "source": None, "deniedResult": None}],
        },
    }
    attached = asyncio.Event()

    async def consume():
        token = bind_execution_control(control)
        await control.attach_task(asyncio.current_task(), mark_working=False)
        attached.set()
        try:
            async for _event in loop.resume_permission_boundary(checkpoint):
                pass
        finally:
            await control.detach_task(asyncio.current_task(), execution_status="input-required")
            reset_execution_control(token)

    task = asyncio.create_task(consume())
    try:
        await attached.wait()
        await asyncio.sleep(0.1)
        assert executions == []
        assert not task.done()
        await control.resume(
            execution_id=control.execution_id, pause_id=paused["pauseId"], request_id="resume", connection_epoch=2
        )
        await asyncio.wait_for(task, 3)
        assert executions == ["running"]
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await control.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("fail_write", [False, True])
async def test_active_termination_commits_off_loop_and_blocks_release_on_failure(tmp_path, monkeypatch, fail_write):
    monkeypatch.setenv("IAC_CODE_CONFIG_BACKUP_DIR", str(tmp_path / "shared"))
    backup = StagedSessionBackupService(tmp_path / "staging")
    executor, store, service = make_executor(tmp_path, backup)
    running = asyncio.Event()

    class Loop:
        async def run_streaming(self, _prompt):
            running.set()
            await asyncio.Event().wait()
            yield TextDeltaEvent(text="unreachable")

    monkeypatch.setattr(
        "iac_code.a2a.executor.create_agent_runtime",
        lambda options: FakeRuntime(agent_loop=Loop(), session_id=options.session_id),
    )
    original = store._persistence.save_task
    started, release = threading.Event(), threading.Event()
    loop_thread = threading.get_ident()
    writes = []

    def gated_save(snapshot):
        control = service.get_for_context(snapshot.context_id)
        if snapshot.task_id == "task-1" and control and control.phase in {"terminating", "terminated"}:
            writes.append((threading.get_ident() == loop_thread, store._mutation_lock.locked()))
            started.set()
            assert release.wait(5)
            if fail_write:
                raise OSError("injected terminal storage failure")
        original(snapshot)

    monkeypatch.setattr(store._persistence, "save_task", gated_save)
    publisher = asyncio.create_task(
        publish_staged_backups(SessionBackupStagingWorker(tmp_path / "staging", tmp_path / "shared"))
    )
    task = asyncio.create_task(
        executor.execute(FakeRequestContext(metadata={"iac_code": {"cwd": str(tmp_path)}}), FakeEventQueue())
    )
    try:
        await asyncio.wait_for(running.wait(), 3)
        control = service.get_for_context("ctx-1")
        await control.terminate(
            execution_id=control.execution_id, request_id="terminate", connection_epoch=1, reason="explicit_terminate"
        )
        assert await asyncio.to_thread(started.wait, 3)
        assert writes == [(False, False)]
        assert not control.release_ready
        await asyncio.wait_for(
            store.get_or_create_context(context_id="ctx-2", cwd=str(tmp_path), runtime_factory=lambda _: object()), 1
        )
        # A previously queued SDK event may arrive while the final write is in flight.
        await store.save(Task(id="task-1", context_id="ctx-1", status=TaskStatus(state=TaskState.TASK_STATE_WORKING)))
        assert (await store.get_task_record("task-1")).state == "canceled"
        release.set()
        if fail_write:
            await wait_until(lambda: control.backup["status"] == "blocked")
            assert not control.release_ready
            fail_write = False
            await control.terminate(
                execution_id=control.execution_id, request_id="retry", connection_epoch=2, reason="explicit_terminate"
            )
        await wait_until(lambda: control.release_ready)
        assert all(write == (False, False) for write in writes)
        assert store._persistence.load_task("task-1").state == "canceled"
        assert (await store.get("task-1")).status.state == TaskState.TASK_STATE_CANCELED
        assert (
            json.loads(next((tmp_path / "shared").rglob("a2a/task.json")).read_text(encoding="utf-8"))["state"]
            == "canceled"
        )
    finally:
        release.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await service.close()
        publisher.cancel()
        await asyncio.gather(publisher, return_exceptions=True)
        await store.stop_cleanup_loop()


@pytest.mark.asyncio
async def test_pause_retains_only_its_context_and_resume_restarts_idle_interval(tmp_path):
    executor, store, service = make_executor(tmp_path, SessionBackupService())
    closed = []

    async def close():
        closed.append(True)

    ctx = await store.get_or_create_context(
        context_id="ctx-1", cwd=str(tmp_path), runtime_factory=lambda _: SimpleNamespace(aclose=close)
    )
    other = await store.get_or_create_context(context_id="ctx-2", cwd=str(tmp_path), runtime_factory=lambda _: object())
    record = await store.get_or_create_task(task_id="task-1", context_id="ctx-1")
    record.state = "input-required"
    control = await service.begin_execution(context_id="ctx-1", task_id="task-1", owner="", cwd=str(tmp_path))
    await control.detach_task(asyncio.current_task(), execution_status="input-required")
    ctx.last_active = other.last_active = time.monotonic() - 3601
    try:
        paused = await control.pause(
            task_id="task-1",
            expected_execution_id=control.execution_id,
            request_id="pause",
            connection_epoch=1,
            reason="disconnect",
            reconnect_timeout_seconds=300,
        )
        await wait_until(lambda: control.phase == "paused")
        await store.cleanup_once()
        assert closed == []
        assert not record.expired
        assert "ctx-1" in store._contexts and "ctx-2" not in store._contexts
        await control.resume(
            execution_id=control.execution_id, pause_id=paused["pauseId"], request_id="resume", connection_epoch=2
        )
        await wait_until(lambda: control.phase == "running")
        await store.cleanup_once()
        assert "ctx-1" in store._contexts
        assert time.monotonic() - ctx.last_active < 2
        await store.cleanup_once(now_offset_seconds=3601)
        assert closed == [True]
        assert record.expired
    finally:
        await service.close()
        await store.stop_cleanup_loop()


@pytest.mark.asyncio
@pytest.mark.parametrize("resume_during_cleanup", [False, True])
async def test_cleanup_rechecks_hold_after_closing_another_runtime(tmp_path, resume_during_cleanup):
    _executor, store, service = make_executor(tmp_path, SessionBackupService())
    closing, release = asyncio.Event(), asyncio.Event()

    async def slow_close():
        closing.set()
        await release.wait()

    first = await store.get_or_create_context(
        context_id="ctx-first", cwd=str(tmp_path), runtime_factory=lambda _: SimpleNamespace(aclose=slow_close)
    )
    held = await store.get_or_create_context(
        context_id="ctx-held", cwd=str(tmp_path), runtime_factory=lambda _: object()
    )
    control = await service.begin_execution(context_id="ctx-held", task_id="task", owner="", cwd=str(tmp_path))
    await control.detach_task(asyncio.current_task(), execution_status="input-required")
    first.last_active = held.last_active = time.monotonic() - 3601
    cleanup = asyncio.create_task(store.cleanup_once())
    try:
        await asyncio.wait_for(closing.wait(), 1)
        paused = await control.pause(
            task_id="task",
            expected_execution_id=control.execution_id,
            request_id="pause",
            connection_epoch=1,
            reason="disconnect",
            reconnect_timeout_seconds=300,
        )
        await wait_until(lambda: control.phase == "paused")
        if resume_during_cleanup:
            await control.resume(
                execution_id=control.execution_id, pause_id=paused["pauseId"], request_id="resume", connection_epoch=2
            )
            await wait_until(lambda: control.phase == "running")
        release.set()
        await asyncio.wait_for(cleanup, 1)
        assert store._contexts.get("ctx-held") is held
        assert "ctx-first" not in store._contexts
    finally:
        release.set()
        await cleanup
        await service.close()
        await store.stop_cleanup_loop()
