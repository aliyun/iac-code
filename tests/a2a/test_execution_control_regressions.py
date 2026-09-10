"""Production-path regressions for execution control, cancellation, and slow storage."""

import asyncio
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from a2a.types import Task, TaskState, TaskStatus
from starlette.testclient import TestClient

from iac_code.a2a.app import create_app
from iac_code.a2a.execution_control import (
    ExecutionController,
    ExecutionControlService,
    bind_execution_control,
    execution_activity,
    reset_execution_control,
)
from iac_code.a2a.executor import IacCodeA2AExecutor
from iac_code.a2a.persistence import A2APersistenceStore
from iac_code.a2a.pipeline_executor import _cancel_task_safely, _drive_stream_events
from iac_code.a2a.task_store import A2ATaskStore
from iac_code.agent.agent_loop import AgentLoop
from iac_code.services.session_backup import BackupReason, SessionBackupService
from iac_code.services.session_backup_staging import SessionBackupStagingWorker, StagedSessionBackupService
from iac_code.services.session_storage import SessionStorage
from iac_code.tools.base import ToolContext, ToolRegistry
from iac_code.tools.cloud.aliyun.ros_stack import RosStack
from iac_code.tools.cloud.aliyun.ros_stack_instances import RosStackInstances
from iac_code.types.stream_events import MessageEndEvent, TextDeltaEvent, Usage

from .fakes import FakeAgentLoop, FakeEventQueue, FakeRequestContext, FakeRuntime


@pytest.fixture(autouse=True)
def isolate_backup_environment(monkeypatch):
    monkeypatch.delenv("IAC_CODE_CONFIG_BACKUP_DIR", raising=False)
    monkeypatch.delenv("IAC_CODE_CONFIG_BACKUP_TMP_DIR", raising=False)


async def wait_until(predicate, timeout=5):
    async def wait():
        while not predicate():
            await asyncio.sleep(0.005)

    await asyncio.wait_for(wait(), timeout)


def controller(tmp_path, backup=None):
    return ExecutionController(
        context_id="ctx-1",
        task_id="task-1",
        owner="",
        cwd=str(tmp_path),
        server_instance_id="test",
        persistence_path=tmp_path / "control.json",
        backup_service=backup,
    )


@pytest.mark.asyncio
async def test_slow_termination_commit_keeps_event_loop_and_other_contexts_available(tmp_path, monkeypatch):
    store = A2ATaskStore(persistence=A2APersistenceStore(tmp_path / "a2a"))
    await store.get_or_create_context(context_id="ctx-1", cwd=str(tmp_path), runtime_factory=lambda _: object())
    record = await store.get_or_create_task(task_id="task-1", context_id="ctx-1")
    record.state = "input-required"
    started, release, finished = threading.Event(), threading.Event(), threading.Event()
    original = store._persist_terminated_task_snapshots_strict

    def blocked_write(*args):
        started.set()
        try:
            assert release.wait(3)
            original(*args)
        finally:
            finished.set()

    monkeypatch.setattr(store, "_persist_terminated_task_snapshots_strict", blocked_write)
    commit = asyncio.create_task(store.cancel_inactive_input_required_task(task_id="task-1", context_id="ctx-1"))
    try:
        assert await asyncio.to_thread(started.wait, 2)
        await asyncio.wait_for(store.get_or_create_task(task_id="task-2", context_id="ctx-2"), 1)
        assert not finished.is_set()
        assert not commit.done()
    finally:
        release.set()
        await commit
    assert store._persistence.load_task("task-1").state == "canceled"
    await store.stop_cleanup_loop()


@pytest.mark.asyncio
@pytest.mark.parametrize("staged", [False, True])
@pytest.mark.parametrize("mode", ["normal", "pipeline"])
async def test_terminate_waits_for_bootstrap_and_cleanup_then_backs_up(tmp_path, monkeypatch, staged, mode):
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("IAC_CODE_MODE", mode)
    shared = tmp_path / "shared"
    monkeypatch.setenv("IAC_CODE_CONFIG_BACKUP_DIR", str(shared))
    backup = StagedSessionBackupService(tmp_path / "staging") if staged else SessionBackupService()
    service = ExecutionControlService(persistence_root=tmp_path / "a2a", backup_service=backup)
    store = A2ATaskStore(backup_service=backup)
    executor = IacCodeA2AExecutor(
        task_store=store, model="test", backup_service=backup, execution_control_service=service
    )
    started, release = threading.Event(), threading.Event()
    closing, close_release, closed = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def close():
        closing.set()
        await close_release.wait()
        closed.set()

    def factory(options):
        started.set()
        assert release.wait(5)
        return FakeRuntime(agent_loop=FakeAgentLoop([]), session_id=options.session_id, aclose=close)

    monkeypatch.setattr("iac_code.a2a.executor.create_agent_runtime", factory)
    monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_agent_runtime", factory)
    worker = SessionBackupStagingWorker(tmp_path / "staging", shared)

    async def publish():
        while True:
            await asyncio.to_thread(worker.run_once)
            await asyncio.sleep(0.01)

    publisher = asyncio.create_task(publish()) if staged else None
    execution = asyncio.create_task(
        executor.execute(FakeRequestContext(metadata={"iac_code": {"cwd": str(tmp_path)}}), FakeEventQueue())
    )
    try:
        assert await asyncio.to_thread(started.wait, 2)
        control = service.get_for_context("ctx-1")
        assert control.session_id is not None
        await control.terminate(
            execution_id=control.execution_id, request_id="terminate", connection_epoch=1, reason="explicit_terminate"
        )
        await asyncio.sleep(0.03)
        assert control.phase == "terminating"
        assert not control.release_ready
        assert not execution.done()
        release.set()
        await asyncio.wait_for(closing.wait(), 2)
        assert not control.release_ready
        execution.cancel()  # Repeated cancellation must not abandon runtime cleanup.
        await asyncio.sleep(0.01)
        assert not execution.done()
        close_release.set()
        with suppress(asyncio.CancelledError):
            await execution
        await wait_until(lambda: control.release_ready)
        assert closed.is_set()
        assert control.backup["status"] == "shared_committed"
        snapshots = list(shared.rglob("a2a/task.json"))
        assert len(snapshots) == 1
        assert json.loads(snapshots[0].read_text(encoding="utf-8"))["state"] == "canceled"
    finally:
        release.set()
        close_release.set()
        await asyncio.gather(execution, return_exceptions=True)
        await service.close()
        if publisher:
            publisher.cancel()
            await asyncio.gather(publisher, return_exceptions=True)
        await store.stop_cleanup_loop()


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["CreateStack", "UpdateStack", "ContinueCreateStack", "DeleteStack"])
async def test_ros_successful_submission_survives_polling_termination_and_backup(tmp_path, monkeypatch, action):
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    shared = tmp_path / "shared"
    monkeypatch.setenv("IAC_CODE_CONFIG_BACKUP_DIR", str(shared))
    backup = SessionBackupService()
    SessionStorage().ensure_v2_session_dir_for_new_session(str(tmp_path), "session-1")
    backup.initialize_session(str(tmp_path), "session-1")
    control = controller(tmp_path, backup)
    control.bind_session("session-1")
    tool = RosStack(allow_pipeline_deployment_actions=True)
    tool.poll_interval = 3600
    calls = []

    def submit(_request):
        calls.append(action)
        return SimpleNamespace(body=SimpleNamespace(stack_id="stack-accepted"))

    client = SimpleNamespace(
        create_stack=submit, update_stack=submit, continue_create_stack=submit, delete_stack=submit
    )
    monkeypatch.setattr(tool, "_get_client", lambda _: client)
    monkeypatch.setattr(tool, "_log_event_best_effort", lambda *a, **kw: None)
    monkeypatch.setattr(tool, "_add_metric_best_effort", lambda *a, **kw: None)
    monkeypatch.setattr("iac_code.tools.cloud.aliyun.api_hooks.run_hooks", lambda *a, **kw: None)
    polling = asyncio.Event()
    original_wait = tool.wait_for_stack_operation

    async def wait(*args, **kwargs):
        polling.set()
        return await original_wait(*args, **kwargs)

    monkeypatch.setattr(tool, "wait_for_stack_operation", wait)

    async def execute():
        token = bind_execution_control(control)
        current = asyncio.current_task()
        await control.attach_task(current)
        try:
            await tool.execute(
                tool_input={
                    "action": action,
                    "region_id": "cn-hangzhou",
                    "params": {
                        "StackName": "test",
                        "StackId": "stack-accepted",
                        "TemplateBody": '{"ROSTemplateFormatVersion":"2015-09-01","Resources":{}}',
                    },
                },
                context=ToolContext(cwd=str(tmp_path), tool_use_id="call-1", event_queue=asyncio.Queue()),
            )
        finally:
            await control.detach_task(current, execution_status="canceled")
            reset_execution_control(token)

    execution = asyncio.create_task(execute())
    try:
        await asyncio.wait_for(polling.wait(), 3)
        await control.terminate(
            execution_id=control.execution_id, request_id="terminate", connection_epoch=1, reason="explicit_terminate"
        )
        await wait_until(lambda: control.release_ready)
        assert execution.cancelled()
        assert calls == [action]
        assert control.external_operations[0]["resourceId"] == "stack-accepted"
        paths = list(shared.rglob("external-operations.json"))
        assert len(paths) == 1
        assert json.loads(paths[0].read_text(encoding="utf-8"))["operations"] == control.external_operations
    finally:
        execution.cancel()
        await asyncio.gather(execution, return_exceptions=True)
        await control.close()


@pytest.mark.asyncio
async def test_activity_finalization_in_another_context_releases_without_error(tmp_path):
    control = controller(tmp_path)

    async def stream():
        async with execution_activity("llm"):
            yield "first"

    async def start():
        token = bind_execution_control(control)
        try:
            events = stream()
            assert await anext(events) == "first"
            return events
        finally:
            reset_execution_control(token)

    events = await asyncio.create_task(start())
    try:
        assert control.has_managed_work()
        await events.aclose()
        assert not control.has_managed_work()
    finally:
        await control.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("continuation", [False, True])
async def test_legacy_stream_cancel_releases_llm_activity_in_consuming_context(tmp_path, continuation):
    closed = asyncio.Event()

    class Provider:
        def get_model_name(self):
            return "test"

        async def stream(self, **kwargs):
            try:
                yield TextDeltaEvent(text="first")
                yield MessageEndEvent(stop_reason="stop", usage=Usage())
            finally:
                closed.set()

    agent = AgentLoop(provider_manager=Provider(), system_prompt="test", tool_registry=ToolRegistry())
    control = controller(tmp_path)
    service = ExecutionControlService(persistence_root=None, backup_service=None)
    service._controls["ctx-1"] = control
    store = A2ATaskStore()
    store.set_execution_control_provider(service.snapshot_for_context, service.has_active_work)
    requests = asyncio.Queue()

    async def drive():
        token = bind_execution_control(control)
        current = asyncio.current_task()
        await control.attach_task(current)
        try:
            stream = agent.continue_streaming() if continuation else agent.run_streaming("hello")
            await _drive_stream_events(stream, requests)
        finally:
            await control.detach_task(current, execution_status="canceled")
            reset_execution_control(token)

    driver = asyncio.create_task(drive())
    try:
        while True:
            completion = asyncio.get_running_loop().create_future()
            await requests.put(completion)
            event = await asyncio.wait_for(completion, 2)
            if isinstance(event, TextDeltaEvent):
                break
        await _cancel_task_safely(driver)
        assert driver.cancelled()
        assert closed.is_set()
        assert not control.has_managed_work()
        assert not await store.has_active_work()
    finally:
        await _cancel_task_safely(driver)
        await service.close()
        await store.stop_cleanup_loop()


@pytest.mark.asyncio
async def test_normal_executor_cancel_during_publication_closes_real_agent_stream(tmp_path, monkeypatch):
    from iac_code.a2a import executor as module

    closed, publishing = asyncio.Event(), asyncio.Event()

    class Provider:
        def get_model_name(self):
            return "test"

        async def stream(self, **kwargs):
            try:
                yield TextDeltaEvent(text="first")
                yield MessageEndEvent(stop_reason="stop", usage=Usage())
            finally:
                closed.set()

    def factory(options):
        agent = AgentLoop(
            provider_manager=Provider(),
            system_prompt="test",
            tool_registry=ToolRegistry(),
            cwd=options.cwd,
            session_id=options.session_id,
        )
        return FakeRuntime(agent_loop=agent, session_id=options.session_id)

    original_publish = module.publish_stream_event

    async def publish(*args, **kwargs):
        if isinstance(kwargs.get("event"), TextDeltaEvent):
            publishing.set()
            await asyncio.Event().wait()
        return await original_publish(*args, **kwargs)

    monkeypatch.setattr(module, "create_agent_runtime", factory)
    monkeypatch.setattr(module, "publish_stream_event", publish)
    service = ExecutionControlService(persistence_root=None, backup_service=None)
    store = A2ATaskStore()
    store.set_execution_control_provider(service.snapshot_for_context, service.has_active_work)
    executor = IacCodeA2AExecutor(task_store=store, model="test", execution_control_service=service)
    execution = asyncio.create_task(
        executor.execute(FakeRequestContext(metadata={"iac_code": {"cwd": str(tmp_path)}}), FakeEventQueue())
    )
    try:
        await asyncio.wait_for(publishing.wait(), 2)
        # The existing cancel route, without pause/resume/terminate.
        assert await store.cancel_task("task-1")
        await execution
        assert closed.is_set()
        assert not service.get_for_context("ctx-1").has_managed_work()
        assert not await store.has_active_work()
    finally:
        execution.cancel()
        await asyncio.gather(execution, return_exceptions=True)
        await service.close()
        await store.stop_cleanup_loop()


@pytest.mark.asyncio
async def test_ros_result_commit_drains_repeated_cancellation(tmp_path, monkeypatch):
    from iac_code.a2a import execution_control as module

    control = controller(tmp_path)
    control.bind_session("session-1")
    started, release = threading.Event(), threading.Event()
    original_write = module.atomic_write_json

    def blocked_write(path, value):
        if path.name == "external-operations.json":
            started.set()
            assert release.wait(3)
        original_write(path, value)

    monkeypatch.setattr(module, "atomic_write_json", blocked_write)

    async def record():
        token = bind_execution_control(control)
        try:
            await RosStack()._record_operation_result(
                "CreateStack",
                {},
                "cn-hangzhou",
                ToolContext(tool_use_id="call-1"),
                SimpleNamespace(body=SimpleNamespace(stack_id="stack-accepted")),
                None,
            )
        finally:
            reset_execution_control(token)

    task = asyncio.create_task(record())
    try:
        assert await asyncio.to_thread(started.wait, 2)
        for _ in range(2):
            task.cancel()
            await asyncio.sleep(0.01)
            assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        path = SessionStorage().session_dir(str(tmp_path), "session-1") / "a2a" / "external-operations.json"
        assert json.loads(path.read_text(encoding="utf-8"))["operations"][0]["resourceId"] == "stack-accepted"
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        await control.close()


@pytest.mark.asyncio
async def test_slow_rollover_only_serializes_its_own_context(tmp_path, monkeypatch):
    from iac_code.a2a import execution_control as module

    service = ExecutionControlService(persistence_root=tmp_path, backup_service=None)
    first = await service.begin_execution(context_id="ctx-1", task_id="task-1", owner="", cwd=str(tmp_path))
    background = asyncio.create_task(asyncio.Event().wait())
    first.register_spawned_task(background, kind="background_agent")
    await first.detach_task(asyncio.current_task(), execution_status="normal-turn-ended")
    started, release = threading.Event(), threading.Event()
    original_write = module.atomic_write_json

    def blocked_write(path, value):
        if value.get("contextId") == "ctx-1":
            started.set()
            assert release.wait(3)
        original_write(path, value)

    monkeypatch.setattr(module, "atomic_write_json", blocked_write)

    async def begin(context, task):
        result = await service.begin_execution(context_id=context, task_id=task, owner="", cwd=str(tmp_path))
        await result.detach_task(asyncio.current_task(), execution_status="normal-turn-ended")
        return result

    rollover = asyncio.create_task(begin("ctx-1", "task-2"))
    same_context = None
    try:
        assert await asyncio.to_thread(started.wait, 2)
        same_context = asyncio.create_task(begin("ctx-1", "task-3"))
        other = await asyncio.wait_for(begin("ctx-2", "task-other"), 1)
        assert other is not first
        assert not rollover.done()
        assert not same_context.done()
    finally:
        release.set()
        await rollover
        if same_context:
            await same_context
        background.cancel()
        await asyncio.gather(background, return_exceptions=True)
        await service.close()


async def publish_staged_backups(worker):
    while True:
        await asyncio.to_thread(worker.run_once)
        await asyncio.sleep(0.01)


@pytest.mark.asyncio
@pytest.mark.parametrize("fail_write", [False, True])
async def test_permission_termination_context_write_is_off_loop_and_gates_release(tmp_path, monkeypatch, fail_write):
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    shared = tmp_path / "shared"
    monkeypatch.setenv("IAC_CODE_CONFIG_BACKUP_DIR", str(shared))
    backup = StagedSessionBackupService(tmp_path / "staging")
    service = ExecutionControlService(persistence_root=tmp_path / "a2a", backup_service=backup)
    store = A2ATaskStore(persistence=A2APersistenceStore(tmp_path / "a2a"), backup_service=backup)
    executor = IacCodeA2AExecutor(
        task_store=store, model="test", backup_service=backup, execution_control_service=service
    )
    closed = asyncio.Event()

    async def close():
        closed.set()

    ctx = await store.get_or_create_context(
        context_id="ctx-1", cwd=str(tmp_path), runtime_factory=lambda _: SimpleNamespace(aclose=close)
    )
    record = await store.get_or_create_task(task_id="task-1", context_id="ctx-1")
    record.state = "input-required"
    control = controller(tmp_path, backup)
    control.bind_session(ctx.session_id)
    control._termination_cleanup = executor._terminate_detached_execution
    service._controls["ctx-1"] = control
    store.set_execution_control_provider(service.snapshot_for_context, service.has_active_work)

    async def cancel_permission(_task_id):
        # A real pending permission's suspend callback also discards runtime,
        # before _terminate_detached_execution can do its own cleanup.
        await store.discard_context_runtime("ctx-1")

    executor._permission_input_registry = SimpleNamespace(
        has_pending_task=AsyncMock(return_value=True), cancel_task=cancel_permission
    )
    started, release = threading.Event(), threading.Event()
    original_write = store._persistence.save_context
    loop_thread = threading.get_ident()
    writes = []

    def gated_write(snapshot):
        if snapshot.context_id == "ctx-1":
            writes.append((threading.get_ident() == loop_thread, store._mutation_lock.locked()))
            started.set()
            assert release.wait(5)
            if fail_write:
                raise OSError("injected context write failure")
        original_write(snapshot)

    monkeypatch.setattr(store._persistence, "save_context", gated_write)
    publisher = asyncio.create_task(publish_staged_backups(SessionBackupStagingWorker(tmp_path / "staging", shared)))
    try:
        await control.terminate(
            execution_id=control.execution_id, request_id="terminate", connection_epoch=1, reason="explicit_terminate"
        )
        assert await asyncio.to_thread(started.wait, 2)
        assert closed.is_set()
        assert control.phase == "terminating" and not control.release_ready
        await asyncio.wait_for(
            store.get_or_create_context(context_id="ctx-2", cwd=str(tmp_path), runtime_factory=lambda _: object()), 1
        )
        assert writes == [(False, False)]
        release.set()
        if fail_write:
            await wait_until(lambda: control.backup["status"] == "blocked")
            assert not control.release_ready
            fail_write = False
            await control.terminate(
                execution_id=control.execution_id, request_id="retry", connection_epoch=2, reason="explicit_terminate"
            )
        await wait_until(lambda: control.release_ready)
        assert all(on_loop is False and locked is False for on_loop, locked in writes)
        assert store._persistence.load_task("task-1").state == "canceled"
        assert store._persistence.load_context("ctx-1").active_task_id is None
        snapshot = next(shared.rglob("a2a/task.json"))
        assert json.loads(snapshot.read_text(encoding="utf-8"))["state"] == "canceled"
    finally:
        release.set()
        await service.close()
        publisher.cancel()
        await asyncio.gather(publisher, return_exceptions=True)
        await store.stop_cleanup_loop()


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["CreateStackInstances", "UpdateStackInstances", "DeleteStackInstances"])
@pytest.mark.parametrize("inflight", [False, True])
async def test_stack_instances_id_survives_termination_and_staged_backup(tmp_path, monkeypatch, action, inflight):
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    shared = tmp_path / "shared"
    monkeypatch.setenv("IAC_CODE_CONFIG_BACKUP_DIR", str(shared))
    backup = StagedSessionBackupService(tmp_path / "staging")
    SessionStorage().ensure_v2_session_dir_for_new_session(str(tmp_path), "session-1")
    backup.initialize_session(str(tmp_path), "session-1")
    control = controller(tmp_path, backup)
    control.bind_session("session-1")
    tool = RosStackInstances()
    monkeypatch.setattr(RosStackInstances, "poll_interval", 3600)
    started, release = threading.Event(), threading.Event()
    calls = []

    def submit(_request):
        calls.append(action)
        started.set()
        assert release.wait(5)
        return SimpleNamespace(body=SimpleNamespace(operation_id="operation-accepted"))

    client = SimpleNamespace(
        create_stack_instances=submit, update_stack_instances=submit, delete_stack_instances=submit
    )
    monkeypatch.setattr(tool, "_get_client", lambda _: client)
    if not inflight:
        release.set()

    async def execute():
        token = bind_execution_control(control)
        current = asyncio.current_task()
        await control.attach_task(current)
        try:
            await tool.execute(
                tool_input={"action": action, "region_id": "cn-hangzhou", "params": {"StackGroupName": "test"}},
                context=ToolContext(cwd=str(tmp_path), tool_use_id="call-1"),
            )
        finally:
            await control.detach_task(current, execution_status="canceled")
            reset_execution_control(token)

    execution = asyncio.create_task(execute())
    publisher = asyncio.create_task(publish_staged_backups(SessionBackupStagingWorker(tmp_path / "staging", shared)))
    try:
        assert await asyncio.to_thread(started.wait, 2)
        if not inflight:
            await wait_until(lambda: bool(control.external_operations))
        await control.terminate(
            execution_id=control.execution_id, request_id="terminate", connection_epoch=1, reason="explicit_terminate"
        )
        if inflight:
            await asyncio.sleep(0.03)
            assert control.phase == "terminating" and not control.release_ready
            assert not execution.done()
        release.set()
        await wait_until(lambda: control.release_ready)
        assert execution.cancelled()
        assert calls == [action]
        assert control.external_operations == [
            {
                "product": "ros",
                "action": action,
                "outcome": "accepted",
                "resourceType": "stack-group-operation",
                "resourceId": "operation-accepted",
                "regionId": "cn-hangzhou",
                "toolUseId": "call-1",
            }
        ]
        snapshot = next(shared.rglob("external-operations.json"))
        assert json.loads(snapshot.read_text(encoding="utf-8"))["operations"] == control.external_operations
    finally:
        release.set()
        execution.cancel()
        await asyncio.gather(execution, return_exceptions=True)
        await control.close()
        publisher.cancel()
        await asyncio.gather(publisher, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("staged", [False, True])
@pytest.mark.parametrize("termination", ["explicit", "timeout", "legacy"])
async def test_finished_normal_turn_keeps_result_during_slow_backup(tmp_path, monkeypatch, staged, termination):
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("IAC_CODE_MODE", "normal")
    shared = tmp_path / "shared"
    monkeypatch.setenv("IAC_CODE_CONFIG_BACKUP_DIR", str(shared))
    backup = StagedSessionBackupService(tmp_path / "staging") if staged else SessionBackupService()
    service = ExecutionControlService(persistence_root=tmp_path / "a2a", backup_service=backup)
    store = A2ATaskStore(backup_service=backup)
    executor = IacCodeA2AExecutor(
        task_store=store, model="test", backup_service=backup, execution_control_service=service
    )
    monkeypatch.setattr(
        "iac_code.a2a.executor.create_agent_runtime",
        lambda options: FakeRuntime(
            agent_loop=FakeAgentLoop([TextDeltaEvent(text="finished result")]), session_id=options.session_id
        ),
    )
    started, release = threading.Event(), threading.Event()
    original_backup = backup.backup_session

    def gated_backup(*args, **kwargs):
        if kwargs.get("reason") == BackupReason.NORMAL_TURN_END:
            started.set()
            assert release.wait(5)
        return original_backup(*args, **kwargs)

    monkeypatch.setattr(backup, "backup_session", gated_backup)
    publisher = (
        asyncio.create_task(publish_staged_backups(SessionBackupStagingWorker(tmp_path / "staging", shared)))
        if staged
        else None
    )
    queue = FakeEventQueue()
    execution = asyncio.create_task(
        executor.execute(FakeRequestContext(metadata={"iac_code": {"cwd": str(tmp_path)}}), queue)
    )
    try:
        assert await asyncio.to_thread(started.wait, 2)
        record = await store.get_task_record("task-1")
        assert record.state == "input-required"
        control = service.get_for_context("ctx-1")
        if termination == "legacy":
            assert await store.cancel_task("task-1")
        elif termination == "explicit":
            await control.terminate(
                execution_id=control.execution_id,
                request_id="terminate",
                connection_epoch=1,
                reason="explicit_terminate",
            )
        else:
            await control.pause(
                task_id="task-1",
                expected_execution_id=control.execution_id,
                request_id="pause",
                connection_epoch=1,
                reason="client_disconnected",
                reconnect_timeout_seconds=1,
            )
            await wait_until(lambda: control.phase == "terminating")
        assert not control.release_ready
        release.set()
        await asyncio.wait_for(execution, 3)
        expected = "canceled" if termination == "legacy" else "input-required"
        if termination != "legacy":
            await wait_until(lambda: control.release_ready)
            assert control.execution_status == expected
        record = await store.get_task_record("task-1")
        assert record.state == expected
        assert record.output_text == ["finished result"]
        # Legacy cancel only stages its noncritical backup; it has no
        # execution-control releaseReady barrier for shared publication.
        await wait_until(
            lambda: any(
                json.loads(snapshot.read_text(encoding="utf-8"))["state"] == expected
                for snapshot in shared.rglob("a2a/task.json")
            )
        )
    finally:
        release.set()
        execution.cancel()
        await asyncio.gather(execution, return_exceptions=True)
        await service.close()
        if publisher:
            publisher.cancel()
            await asyncio.gather(publisher, return_exceptions=True)
        await store.stop_cleanup_loop()


@pytest.mark.parametrize("change", ["none", "rollover", "replacement"])
def test_recovery_revalidates_execution_after_history_read(tmp_path, monkeypatch, change):
    app = create_app(host="127.0.0.1", port=41242, token=None, model="test", persistence_dir=tmp_path / "a2a")
    components = app.state.a2a_components
    service, store = components.execution_control_service, components.task_store
    started, release = threading.Event(), threading.Event()

    def load(*_args):
        started.set()
        assert release.wait(5)
        return []

    monkeypatch.setattr(SessionStorage, "load", load)

    async def begin(task_id):
        await store.get_or_create_context(context_id="ctx-1", cwd=str(tmp_path), runtime_factory=lambda _: object())
        record = await store.get_or_create_task(task_id=task_id, context_id="ctx-1")
        record.state = "input-required"
        record.output_text = [task_id]
        await store.save(
            Task(id=task_id, context_id="ctx-1", status=TaskStatus(state=TaskState.TASK_STATE_INPUT_REQUIRED))
        )
        control = await service.begin_execution(context_id="ctx-1", task_id=task_id, owner="", cwd=str(tmp_path))
        if task_id == "task-1" and change == "rollover":
            control.register_spawned_task(asyncio.create_task(asyncio.Event().wait()), kind="background_agent")
        await control.detach_task(asyncio.current_task(), execution_status="normal-turn-ended")
        return control.execution_id

    with TestClient(app) as client, ThreadPoolExecutor(max_workers=1) as pool:
        old_execution_id = client.portal.call(begin, "task-1")
        request = pool.submit(
            client.get, "/iac-code/session/recovery", params={"contextId": "ctx-1", "executionId": old_execution_id}
        )
        try:
            assert started.wait(2)
            if change != "none":
                assert client.portal.call(begin, "task-2") != old_execution_id
            release.set()
            response = request.result(timeout=3)
            if change != "none":
                assert response.status_code == 409
            else:
                assert response.status_code == 200
                data = response.json()
                assert data["executionId"] == data["executionControl"]["executionId"] == old_execution_id
                assert data["taskId"] == data["task"]["id"] == "task-1"
        finally:
            release.set()
            request.result(timeout=3)


@pytest.mark.parametrize("timeout", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_pause_timeout_leaves_http_control_usable(tmp_path, timeout):
    app = create_app(host="127.0.0.1", port=41242, token=None, model="test", persistence_dir=tmp_path / "a2a")
    control = controller(tmp_path)
    app.state.a2a_components.execution_control_service._controls["ctx-1"] = control
    before = control.snapshot()
    payload = {
        "contextId": "ctx-1",
        "taskId": "task-1",
        "expectedExecutionId": control.execution_id,
        "requestId": "pause",
        "connectionEpoch": 1,
        "reason": "client_disconnected",
        "reconnectTimeoutSeconds": timeout,
    }
    with TestClient(app) as client:
        invalid = client.post(
            "/iac-code/execution/pause", content=json.dumps(payload), headers={"Content-Type": "application/json"}
        )
        assert invalid.status_code == 400
        assert control.snapshot() == before
        assert client.get("/iac-code/execution/state?contextId=ctx-1").status_code == 200
        payload["reconnectTimeoutSeconds"] = 30
        assert client.post("/iac-code/execution/pause", json=payload).status_code in {200, 202}
        assert client.post(
            "/iac-code/execution/resume",
            json={
                "contextId": "ctx-1",
                "executionId": control.execution_id,
                "pauseId": control.pause_id,
                "requestId": "resume",
                "connectionEpoch": 2,
            },
        ).status_code in {200, 202}
        assert client.post(
            "/iac-code/execution/terminate",
            json={
                "contextId": "ctx-1",
                "expectedExecutionId": control.execution_id,
                "requestId": "terminate",
                "connectionEpoch": 3,
            },
        ).status_code in {200, 202}
