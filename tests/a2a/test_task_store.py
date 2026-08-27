import asyncio
import json
import shutil
import threading
import time
from collections.abc import Callable
from types import SimpleNamespace

import pytest
from a2a.auth.user import User
from a2a.server.context import ServerCallContext
from a2a.types import Artifact, ListTasksRequest, Part, Task, TaskState, TaskStatus
from a2a.utils.errors import InvalidParamsError
from google.protobuf.timestamp_pb2 import Timestamp

from iac_code.a2a.metrics import NoOpA2AMetrics
from iac_code.a2a.persistence import A2AContextSnapshot, A2APersistenceStore, A2ATaskSnapshot
from iac_code.a2a.task_store import A2ATaskStore
from iac_code.services.session_backup_state import SessionBackupState
from iac_code.services.session_layout import UnsupportedSessionLayoutError
from iac_code.services.session_storage import SessionStorage


def _symlink_or_skip(target, link, *, target_is_directory=False) -> None:
    try:
        link.symlink_to(target, target_is_directory=target_is_directory)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"symlink creation unsupported: {exc}")


class FailingPersistence:
    def __init__(self) -> None:
        self.fail = True

    def save_task(self, snapshot) -> None:
        if self.fail:
            raise OSError("disk full")

    def save_context(self, snapshot) -> None:
        if self.fail:
            raise OSError("disk full")


class CountingTaskPersistence(A2APersistenceStore):
    def __init__(self, root) -> None:
        super().__init__(root)
        self.task_save_count = 0

    def save_task(self, snapshot) -> None:
        self.task_save_count += 1
        super().save_task(snapshot)


class FlakyTaskPersistence(CountingTaskPersistence):
    def __init__(self, root) -> None:
        super().__init__(root)
        self.fail_next_task_save = True

    def save_task(self, snapshot) -> None:
        if self.fail_next_task_save:
            self.task_save_count += 1
            self.fail_next_task_save = False
            raise OSError("temporary write failure")
        super().save_task(snapshot)


class NamedUser(User):
    def __init__(self, user_name: str) -> None:
        self._user_name = user_name

    @property
    def is_authenticated(self) -> bool:
        return True

    @property
    def user_name(self) -> str:
        return self._user_name


def call_context(user_name: str) -> ServerCallContext:
    return ServerCallContext(user=NamedUser(user_name))


def timestamp(seconds: int) -> Timestamp:
    value = Timestamp()
    value.FromSeconds(seconds)
    return value


def timestamp_with_nanos(seconds: int, nanos: int) -> Timestamp:
    value = Timestamp(seconds=seconds, nanos=nanos)
    return value


async def wait_until(condition: Callable[[], bool], *, timeout: float = 1.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return
        await asyncio.sleep(0.01)
    assert condition()


def sdk_task(
    task_id: str,
    *,
    context_id: str = "ctx-1",
    state: int = TaskState.TASK_STATE_SUBMITTED,
    updated_at: int = 1,
    with_artifact: bool = False,
) -> Task:
    task = Task(
        id=task_id,
        context_id=context_id,
        status=TaskStatus(state=TaskState.Name(state), timestamp=timestamp(updated_at)),
    )
    if with_artifact:
        task.artifacts.append(Artifact(artifact_id=f"artifact-{task_id}", parts=[Part(text="artifact")]))
    return task


@pytest.mark.asyncio
async def test_context_reuses_runtime_until_evicted() -> None:
    store = A2ATaskStore(metrics=NoOpA2AMetrics(), idle_timeout_seconds=60, cleanup_interval_seconds=300)
    context = await store.get_or_create_context(context_id="ctx-1", cwd="/tmp", runtime_factory=lambda sid: f"rt-{sid}")
    again = await store.get_or_create_context(context_id="ctx-1", cwd="/tmp", runtime_factory=lambda sid: f"new-{sid}")

    assert again is context
    assert again.runtime == context.runtime


@pytest.mark.asyncio
async def test_context_telemetry_channel_binding_persists_and_can_be_updated(monkeypatch, tmp_path) -> None:
    config_dir = tmp_path / "config"
    cwd = tmp_path / "workspace"
    cwd.mkdir()
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(config_dir))
    persistence = A2APersistenceStore(config_dir / "a2a")
    store = A2ATaskStore(metrics=NoOpA2AMetrics(), persistence=persistence)

    assert await store.resolve_context_telemetry_channel("ctx-1", "  skill  ") == "skill"
    context = await store.get_or_create_context(
        context_id="ctx-1",
        cwd=str(cwd),
        runtime_factory=lambda _session_id: object(),
    )

    assert context.telemetry_channel == "skill"
    assert await store.resolve_context_telemetry_channel("ctx-1", None) == "skill"
    assert persistence.load_context("ctx-1").telemetry_channel == "skill"

    assert await store.resolve_context_telemetry_channel("ctx-1", "web-a2a") == "web-a2a"
    assert persistence.load_context("ctx-1").telemetry_channel == "web-a2a"

    restored_store = A2ATaskStore(metrics=NoOpA2AMetrics(), persistence=persistence)
    assert await restored_store.resolve_context_telemetry_channel("ctx-1", None) == "web-a2a"
    restored = await restored_store.get_or_create_context(
        context_id="ctx-1",
        cwd=str(cwd),
        runtime_factory=lambda _session_id: object(),
    )
    assert restored.telemetry_channel == "web-a2a"


@pytest.mark.asyncio
async def test_new_a2a_session_initializes_backup_generation_zero(monkeypatch, tmp_path) -> None:
    config_dir = tmp_path / "config"
    backup_root = tmp_path / "backup"
    cwd = tmp_path / "workspace"
    cwd.mkdir()
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("IAC_CODE_CONFIG_BACKUP_DIR", str(backup_root))
    store = A2ATaskStore(metrics=NoOpA2AMetrics())

    context = await store.get_or_create_context(
        context_id="ctx-1", cwd=str(cwd), runtime_factory=lambda _session_id: object()
    )

    state_path = SessionStorage().session_dir(str(cwd), context.session_id) / ".backup-state.json"
    state = SessionBackupState.from_dict(json.loads(state_path.read_text(encoding="utf-8")))
    assert state.session_id == context.session_id
    assert state.generation == 0


def test_reconciliation_lock_is_stable_per_context() -> None:
    store = A2ATaskStore(metrics=NoOpA2AMetrics())

    assert store.reconciliation_lock("ctx-1") is store.reconciliation_lock("ctx-1")
    assert store.reconciliation_lock("ctx-1") is not store.reconciliation_lock("ctx-2")


@pytest.mark.asyncio
async def test_context_execution_start_waits_for_reconciliation_and_blocks_new_reconcile() -> None:
    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    reconciliation_lock = store.reconciliation_lock("ctx-1")
    await reconciliation_lock.acquire()
    execution_started = asyncio.Event()
    release_execution = asyncio.Event()

    async def hold_execution_start() -> None:
        token = await store.begin_context_execution("ctx-1")
        execution_started.set()
        await release_execution.wait()
        await store.end_context_execution("ctx-1", token)

    start_task = asyncio.create_task(hold_execution_start())
    await asyncio.sleep(0)
    assert start_task.done() is False

    reconciliation_lock.release()
    await execution_started.wait()
    assert await store.context_reconciliation_is_blocked("ctx-1") is True
    with pytest.raises(ValueError, match="A2A context not found"):
        await store.ensure_context_reconciliation_safe("ctx-1")

    release_execution.set()
    await start_task
    assert await store.context_reconciliation_is_blocked("ctx-1") is False


@pytest.mark.asyncio
async def test_reconciliation_waiter_does_not_block_active_pipeline_followup() -> None:
    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    active_record = await store.get_or_create_task(task_id="task-active", context_id="ctx-1")
    release_active = asyncio.Event()

    async def hold_active_execution() -> None:
        await release_active.wait()

    active_owner = asyncio.create_task(hold_active_execution())
    active_record.active_task = active_owner
    ordinary = asyncio.create_task(
        store.begin_context_execution_after_reconciliation(
            "ctx-1",
            lambda: asyncio.sleep(0),
            wait_timeout=1,
        )
    )
    await asyncio.sleep(0)

    fast_followup = asyncio.create_task(store.begin_context_execution_if_task_active("ctx-1", "task-active"))
    reservation = await asyncio.wait_for(fast_followup, timeout=0.1)

    assert reservation is not None
    fast_token, reserved_owner = reservation
    assert reserved_owner is active_owner
    await store.end_context_execution("ctx-1", fast_token)
    release_active.set()
    await active_owner
    ordinary_token, _result = await ordinary
    await store.end_context_execution("ctx-1", ordinary_token)


@pytest.mark.asyncio
async def test_cancelled_reconciliation_waiter_releases_cleanup_protection() -> None:
    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    active_record = await store.get_or_create_task(task_id="task-active", context_id="ctx-1")
    release_active = asyncio.Event()

    async def hold_active_execution() -> None:
        await release_active.wait()

    active_owner = asyncio.create_task(hold_active_execution())
    active_record.active_task = active_owner
    waiter = asyncio.create_task(
        store.begin_context_execution_after_reconciliation(
            "ctx-1",
            lambda: asyncio.sleep(0),
            wait_timeout=None,
        )
    )
    await asyncio.sleep(0)
    assert store._context_reconciliation_waiters.get("ctx-1")

    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    await asyncio.sleep(0)

    assert not store._context_reconciliation_waiters.get("ctx-1")
    release_active.set()
    await active_owner


@pytest.mark.asyncio
async def test_context_execution_start_is_released_when_owner_is_cancelled() -> None:
    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    execution_started = asyncio.Event()

    async def hold_execution_start() -> None:
        await store.begin_context_execution("ctx-1")
        execution_started.set()
        await asyncio.Event().wait()

    owner_task = asyncio.create_task(hold_execution_start())
    await execution_started.wait()
    assert await store.context_reconciliation_is_blocked("ctx-1") is True

    owner_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await owner_task
    await asyncio.sleep(0)

    assert await store.context_reconciliation_is_blocked("ctx-1") is False


@pytest.mark.asyncio
async def test_cleanup_preserves_context_during_reconciliation_and_execution_start() -> None:
    store = A2ATaskStore(metrics=NoOpA2AMetrics(), idle_timeout_seconds=0, cleanup_interval_seconds=300)
    context = await store.get_or_create_context(
        context_id="ctx-1",
        cwd="/tmp",
        runtime_factory=lambda _session_id: object(),
    )
    token = await store.begin_context_execution(context.context_id)

    await store.cleanup_once(now_offset_seconds=1)

    assert (await store.get_context_record(context.context_id)).session_id == context.session_id
    await store.end_context_execution(context.context_id, token)

    reconciliation_lock = store.reconciliation_lock(context.context_id)
    await reconciliation_lock.acquire()
    try:
        await store.cleanup_once(now_offset_seconds=1)
        assert (await store.get_context_record(context.context_id)).session_id == context.session_id
    finally:
        reconciliation_lock.release()


@pytest.mark.asyncio
async def test_context_runtime_path_directories_include_permission_and_tool_context_roots(tmp_path) -> None:
    permission_context = SimpleNamespace(
        additional_directories=[str(tmp_path / "additional")],
        trusted_read_directories=[str(tmp_path / "trusted")],
        relative_read_directories=[str(tmp_path / "relative")],
        strict_read_directories=[str(tmp_path / "application-root")],
    )
    agent_loop = SimpleNamespace(
        _permission_context_getter=None,
        _permission_context=permission_context,
        _tool_context_trusted_read_directories=[str(tmp_path / "tool-trusted")],
        _tool_context_relative_read_directories=[str(tmp_path / "tool-relative")],
    )
    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    context = await store.get_or_create_context(
        context_id="ctx-runtime-roots",
        cwd=str(tmp_path),
        runtime_factory=lambda _session_id: SimpleNamespace(agent_loop=agent_loop),
    )

    assert await store.get_context_runtime_path_directories(context.context_id) == (
        [str(tmp_path / "additional")],
        [
            str(tmp_path / "trusted"),
            str(tmp_path / "application-root"),
            str(tmp_path / "tool-trusted"),
        ],
        [str(tmp_path / "relative"), str(tmp_path / "tool-relative")],
    )


@pytest.mark.asyncio
async def test_refresh_context_from_session_closes_runtime_and_applies_proven_handoff(monkeypatch, tmp_path) -> None:
    config_dir = tmp_path / "config"
    cwd = tmp_path / "workspace"
    cwd.mkdir()
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(config_dir))
    persistence = A2APersistenceStore(config_dir / "a2a")
    store = A2ATaskStore(metrics=NoOpA2AMetrics(), persistence=persistence)
    runtime = SimpleNamespace(closed=False)

    async def close_runtime() -> None:
        runtime.closed = True

    runtime.aclose = close_runtime
    context = await store.get_or_create_context(
        context_id="ctx-1", cwd=str(cwd), runtime_factory=lambda _session_id: runtime
    )
    session_dir = SessionStorage().session_dir(str(cwd), context.session_id)
    context_path = session_dir / "a2a" / "context.json"
    restored = A2AContextSnapshot(
        context_id="ctx-1",
        session_id=context.session_id,
        cwd=str(cwd),
        active_task_id="restored-task",
    )
    context_path.write_text(json.dumps(restored.__dict__), encoding="utf-8")

    refreshed = await store.refresh_context_from_session(
        context_id="ctx-1",
        cwd=str(cwd),
        session_id=context.session_id,
        clear_active_task_for_proven_handoff=False,
    )

    assert runtime.closed is True
    assert refreshed.active_task_id == "restored-task"
    assert store._contexts["ctx-1"].runtime is None

    cleared = await store.refresh_context_from_session(
        context_id="ctx-1",
        cwd=str(cwd),
        session_id=context.session_id,
        clear_active_task_for_proven_handoff=True,
    )

    assert cleared.active_task_id is None
    assert json.loads(context_path.read_text(encoding="utf-8"))["active_task_id"] is None
    assert persistence.load_context("ctx-1").active_task_id is None


@pytest.mark.asyncio
async def test_refresh_context_from_session_rejects_in_flight_task_without_closing_runtime(
    monkeypatch, tmp_path
) -> None:
    config_dir = tmp_path / "config"
    cwd = tmp_path / "workspace"
    cwd.mkdir()
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(config_dir))
    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    runtime = SimpleNamespace(closed=False)

    async def close_runtime() -> None:
        runtime.closed = True

    runtime.aclose = close_runtime
    context = await store.get_or_create_context(
        context_id="ctx-1", cwd=str(cwd), runtime_factory=lambda _session_id: runtime
    )
    task = await store.get_or_create_task(task_id="task-1", context_id="ctx-1")
    task.active_task = asyncio.create_task(asyncio.sleep(60))
    context.active_task_id = task.task_id
    store.mirror_context(context)

    try:
        with pytest.raises(ValueError, match="A2A context not found"):
            await store.refresh_context_from_session(
                context_id="ctx-1",
                cwd=str(cwd),
                session_id=context.session_id,
                clear_active_task_for_proven_handoff=True,
            )
    finally:
        task.active_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task.active_task

    assert runtime.closed is False
    assert store._contexts["ctx-1"].active_task_id == "task-1"


def test_mirror_session_task_treats_session_path_resolution_as_best_effort(monkeypatch: pytest.MonkeyPatch) -> None:
    store = A2ATaskStore(metrics=NoOpA2AMetrics(), idle_timeout_seconds=60, cleanup_interval_seconds=300)
    warning_calls = []

    def fail_session_paths(context_id: str):
        raise UnsupportedSessionLayoutError(
            "future layout at /mnt/oss/customer-bucket/projects/sensitive-session-id/metadata.json"
        )

    monkeypatch.setattr(store, "_session_paths_for_task_context", fail_session_paths)
    monkeypatch.setattr("iac_code.a2a.task_store.logger.warning", lambda *args, **kwargs: warning_calls.append(args))

    store._mirror_session_task(A2ATaskSnapshot(task_id="task-1", context_id="ctx-1", state="working"))

    assert warning_calls
    logged = " ".join(str(arg) for arg in warning_calls[0])
    assert "UnsupportedSessionLayoutError" in logged
    assert "/mnt/oss" not in logged
    assert "customer-bucket" not in logged
    assert "sensitive-session-id" not in logged


def test_load_context_snapshot_failure_logs_sanitized_warning(monkeypatch: pytest.MonkeyPatch) -> None:
    class ExplodingPersistence:
        def load_context(self, context_id: str):
            raise OSError(f"failed for {context_id} at /tmp/secret/path")

    warning_calls = []
    store = A2ATaskStore(metrics=NoOpA2AMetrics(), idle_timeout_seconds=60, persistence=ExplodingPersistence())

    monkeypatch.setattr("iac_code.a2a.task_store.logger.warning", lambda *args, **kwargs: warning_calls.append(args))
    monkeypatch.setattr(
        "iac_code.a2a.task_store.logger.exception",
        lambda *args, **kwargs: pytest.fail("context snapshot load should not log traceback"),
    )

    assert store._load_context_snapshot("ctx-secret") is None

    assert warning_calls
    template, error_type = warning_calls[0]
    assert "context %s" not in template
    assert error_type == "OSError"


@pytest.mark.asyncio
async def test_context_runtime_factory_runs_outside_mutation_lock() -> None:
    store = A2ATaskStore(metrics=NoOpA2AMetrics(), idle_timeout_seconds=60, cleanup_interval_seconds=300)
    started = threading.Event()
    release = threading.Event()

    def slow_runtime_factory(session_id: str):
        started.set()
        release.wait(timeout=2)
        return f"rt-{session_id}"

    context_task = asyncio.create_task(
        store.get_or_create_context(context_id="ctx-1", cwd="/tmp", runtime_factory=slow_runtime_factory)
    )

    try:
        assert await asyncio.wait_for(asyncio.to_thread(started.wait, 1), timeout=1)
        await asyncio.wait_for(store.save(sdk_task("task-while-runtime-starts")), timeout=1)
        assert not context_task.done()
    finally:
        release.set()

    context = await asyncio.wait_for(context_task, timeout=1)
    assert context.runtime == f"rt-{context.session_id}"


@pytest.mark.asyncio
async def test_cancelled_context_runtime_creation_does_not_poison_follow_up() -> None:
    store = A2ATaskStore(metrics=NoOpA2AMetrics(), idle_timeout_seconds=60, cleanup_interval_seconds=300)
    release = threading.Event()
    call_index = 0
    runtimes = []

    def runtime_factory(session_id: str):
        nonlocal call_index
        call_index += 1
        index = call_index
        release.wait(timeout=2)
        runtime = SimpleNamespace(index=index, session_id=session_id, closed=False)

        async def aclose() -> None:
            runtime.closed = True

        runtime.aclose = aclose
        runtimes.append(runtime)
        return runtime

    first = asyncio.create_task(
        store.get_or_create_context(context_id="ctx-cancel", cwd="/tmp", runtime_factory=runtime_factory)
    )
    await asyncio.sleep(0.01)
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first

    second = asyncio.create_task(
        store.get_or_create_context(context_id="ctx-cancel", cwd="/tmp", runtime_factory=runtime_factory)
    )
    release.set()
    context = await asyncio.wait_for(second, timeout=1)

    def cancelled_runtime_closed() -> bool:
        runtime = next((runtime for runtime in runtimes if runtime.index == 1), None)
        return runtime is not None and runtime.closed is True

    await wait_until(cancelled_runtime_closed)

    assert context.runtime.index == 2
    assert context.runtime.closed is False


@pytest.mark.asyncio
async def test_stop_cleanup_loop_discards_pending_context_runtime() -> None:
    store = A2ATaskStore(metrics=NoOpA2AMetrics(), idle_timeout_seconds=60, cleanup_interval_seconds=300)
    release = threading.Event()
    runtimes = []

    def runtime_factory(session_id: str):
        release.wait(timeout=2)
        runtime = SimpleNamespace(session_id=session_id, close_count=0)

        async def aclose() -> None:
            runtime.close_count += 1

        runtime.aclose = aclose
        runtimes.append(runtime)
        return runtime

    pending = asyncio.create_task(
        store.get_or_create_context(context_id="ctx-stop", cwd="/tmp", runtime_factory=runtime_factory)
    )
    await asyncio.sleep(0.01)

    await store.stop_cleanup_loop()
    release.set()

    with pytest.raises(ValueError, match="not found"):
        await asyncio.wait_for(pending, timeout=1)
    assert runtimes[0].close_count == 1


@pytest.mark.asyncio
async def test_context_rejects_workspace_change() -> None:
    store = A2ATaskStore(metrics=NoOpA2AMetrics(), idle_timeout_seconds=60, cleanup_interval_seconds=300)
    await store.get_or_create_context(context_id="ctx-1", cwd="/tmp/one", runtime_factory=lambda sid: object())

    with pytest.raises(ValueError, match="different workspace"):
        await store.get_or_create_context(context_id="ctx-1", cwd="/tmp/two", runtime_factory=lambda sid: object())


@pytest.mark.asyncio
async def test_expired_task_rejects_follow_up() -> None:
    store = A2ATaskStore(metrics=NoOpA2AMetrics(), idle_timeout_seconds=0, cleanup_interval_seconds=300)
    await store.get_or_create_context(context_id="ctx-1", cwd="/tmp", runtime_factory=lambda sid: object())
    await store.get_or_create_task(task_id="task-1", context_id="ctx-1")
    await store.cleanup_once(now_offset_seconds=1)

    with pytest.raises(ValueError, match="expired"):
        await store.ensure_task_not_expired("task-1")


@pytest.mark.asyncio
async def test_cleanup_disconnects_mcp_manager_on_evicted_runtime() -> None:
    store = A2ATaskStore(metrics=NoOpA2AMetrics(), idle_timeout_seconds=0, cleanup_interval_seconds=300)
    manager = SimpleNamespace(disconnected=False)

    async def disconnect_all() -> None:
        manager.disconnected = True

    manager.disconnect_all = disconnect_all
    runtime = SimpleNamespace(mcp_manager=manager)
    await store.get_or_create_context(context_id="ctx-1", cwd="/tmp", runtime_factory=lambda sid: runtime)

    await store.cleanup_once(now_offset_seconds=1)

    assert manager.disconnected is True


@pytest.mark.asyncio
async def test_cleanup_disconnects_nested_pipeline_agent_runtime_mcp_manager() -> None:
    store = A2ATaskStore(metrics=NoOpA2AMetrics(), idle_timeout_seconds=0, cleanup_interval_seconds=300)
    manager = SimpleNamespace(disconnected=False)

    async def disconnect_all() -> None:
        manager.disconnected = True

    manager.disconnect_all = disconnect_all
    runtime = SimpleNamespace(agent_runtime=SimpleNamespace(mcp_manager=manager))
    await store.get_or_create_context(context_id="ctx-1", cwd="/tmp", runtime_factory=lambda sid: runtime)

    await store.cleanup_once(now_offset_seconds=1)

    assert manager.disconnected is True


@pytest.mark.asyncio
async def test_cleanup_removes_expired_sdk_tasks_after_tombstone_window() -> None:
    store = A2ATaskStore(metrics=NoOpA2AMetrics(), idle_timeout_seconds=0, cleanup_interval_seconds=300)
    await store.get_or_create_context(context_id="ctx-1", cwd="/tmp", runtime_factory=lambda sid: object())
    await store.get_or_create_task(task_id="task-1", context_id="ctx-1")
    await store.save(Task(id="task-1", context_id="ctx-1", status=TaskStatus(state="TASK_STATE_SUBMITTED")))

    await store.cleanup_once(now_offset_seconds=1)
    assert await store.get("task-1") is not None

    await store.cleanup_once(now_offset_seconds=302)
    assert await store.get("task-1") is None


@pytest.mark.asyncio
async def test_cancel_active_task_does_not_need_context_lock() -> None:
    store = A2ATaskStore(metrics=NoOpA2AMetrics(), idle_timeout_seconds=60, cleanup_interval_seconds=300)
    context = await store.get_or_create_context(context_id="ctx-1", cwd="/tmp", runtime_factory=lambda sid: object())
    task = await store.get_or_create_task(task_id="task-1", context_id="ctx-1")

    async def sleeper() -> None:
        await asyncio.sleep(5)

    active = asyncio.create_task(sleeper())
    task.active_task = active
    async with context.lock:
        assert await store.cancel_task("task-1") is True

    await asyncio.sleep(0)
    assert active.cancelled() or active.done()


@pytest.mark.asyncio
async def test_cancel_task_and_wait_reports_unfinished_task_after_timeout() -> None:
    store = A2ATaskStore(
        metrics=NoOpA2AMetrics(),
        idle_timeout_seconds=60,
        cleanup_interval_seconds=300,
    )
    task = await store.get_or_create_task(task_id="task-1", context_id="ctx-1")
    release = asyncio.Event()

    async def ignore_cancel_until_released() -> None:
        try:
            await release.wait()
        except asyncio.CancelledError:
            await release.wait()

    active = asyncio.create_task(ignore_cancel_until_released())
    task.active_task = active
    await asyncio.sleep(0)

    assert await store.cancel_task_and_wait("task-1", timeout=0.01) is False
    assert active.done() is False

    release.set()
    await active


@pytest.mark.asyncio
async def test_cancel_task_and_wait_clears_inactive_context_fence(
    tmp_path,
) -> None:
    store = A2ATaskStore(
        metrics=NoOpA2AMetrics(),
        idle_timeout_seconds=60,
        cleanup_interval_seconds=300,
    )
    context = await store.get_or_create_context(
        context_id="ctx-1",
        cwd=str(tmp_path),
        runtime_factory=lambda sid: object(),
    )
    task = await store.get_or_create_task(task_id="task-1", context_id="ctx-1")
    context.active_task_id = task.task_id
    store.mirror_context(context)

    async def run_until_cancelled() -> None:
        await asyncio.sleep(60)

    active = asyncio.create_task(run_until_cancelled())
    task.active_task = active
    await asyncio.sleep(0)

    assert await store.cancel_task_and_wait(task.task_id, timeout=1) is True
    assert task.active_task is None
    assert (await store.get_context_record(context.context_id)).active_task_id is None


@pytest.mark.asyncio
async def test_cancel_task_and_wait_preserves_live_replacement_owner(
    tmp_path,
) -> None:
    store = A2ATaskStore(
        metrics=NoOpA2AMetrics(),
        idle_timeout_seconds=60,
        cleanup_interval_seconds=300,
    )
    context = await store.get_or_create_context(
        context_id="ctx-1",
        cwd=str(tmp_path),
        runtime_factory=lambda sid: object(),
    )
    task = await store.get_or_create_task(task_id="task-1", context_id="ctx-1")
    context.active_task_id = task.task_id
    store.mirror_context(context)
    replacement_release = asyncio.Event()

    async def replacement_owner() -> None:
        await replacement_release.wait()

    replacement = asyncio.create_task(replacement_owner())

    async def replace_owner_when_cancelled() -> None:
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            task.active_task = replacement

    active = asyncio.create_task(replace_owner_when_cancelled())
    task.active_task = active
    await asyncio.sleep(0)

    assert await store.cancel_task_and_wait(task.task_id, timeout=1) is False
    assert task.active_task is replacement
    assert (await store.get_context_record(context.context_id)).active_task_id == task.task_id

    replacement_release.set()
    await replacement


@pytest.mark.asyncio
async def test_task_status_access_waits_for_mutation_lock() -> None:
    store = A2ATaskStore(metrics=NoOpA2AMetrics(), idle_timeout_seconds=60, cleanup_interval_seconds=300)
    task = await store.get_or_create_task(task_id="task-1", context_id="ctx-1")

    async def sleeper() -> None:
        await asyncio.sleep(5)

    active = asyncio.create_task(sleeper())
    task.active_task = active

    async with store._mutation_lock:
        active_check = asyncio.create_task(store.is_task_active("task-1"))
        await asyncio.sleep(0)
        assert active_check.done() is False

    assert await active_check is True

    async with store._mutation_lock:
        cancel_attempt = asyncio.create_task(store.cancel_task("task-1"))
        await asyncio.sleep(0)
        assert cancel_attempt.done() is False

    assert await cancel_attempt is True
    await asyncio.sleep(0)
    assert active.cancelled() or active.done()


@pytest.mark.asyncio
async def test_task_id_cannot_move_between_contexts() -> None:
    store = A2ATaskStore(metrics=NoOpA2AMetrics(), idle_timeout_seconds=60, cleanup_interval_seconds=300)
    await store.get_or_create_task(task_id="task-1", context_id="ctx-a")

    with pytest.raises(ValueError, match="different context"):
        await store.get_or_create_task(task_id="task-1", context_id="ctx-b")


@pytest.mark.asyncio
async def test_get_or_create_task_rejects_persisted_context_mismatch_after_restart(tmp_path) -> None:
    persistence = A2APersistenceStore(tmp_path)
    persistence.save_task(A2ATaskSnapshot(task_id="task-1", context_id="ctx-a", state="working"))
    store = A2ATaskStore(metrics=NoOpA2AMetrics(), persistence=persistence)

    with pytest.raises(ValueError, match="different context"):
        await store.get_or_create_task(task_id="task-1", context_id="ctx-b")

    snapshot = persistence.load_task("task-1")
    assert snapshot is not None
    assert snapshot.context_id == "ctx-a"
    assert snapshot.state == "working"


@pytest.mark.asyncio
async def test_get_or_create_task_restores_persisted_interrupted_state_after_restart(tmp_path) -> None:
    persistence = A2APersistenceStore(tmp_path)
    persistence.save_task(A2ATaskSnapshot(task_id="task-1", context_id="ctx-1", state="working"))
    store = A2ATaskStore(metrics=NoOpA2AMetrics(), persistence=persistence)

    task = await store.get_or_create_task(task_id="task-1", context_id="ctx-1")

    assert task.context_id == "ctx-1"
    assert task.state == "interrupted"
    assert persistence.load_task("task-1").state == "interrupted"


@pytest.mark.asyncio
async def test_get_or_create_task_can_load_recoverable_persisted_task_without_interrupting(tmp_path) -> None:
    persistence = A2APersistenceStore(tmp_path)
    persistence.save_task(A2ATaskSnapshot(task_id="task-1", context_id="ctx-1", state="working"))
    store = A2ATaskStore(metrics=NoOpA2AMetrics(), persistence=persistence)

    task = await store.get_or_create_task(task_id="task-1", context_id="ctx-1", restore_interrupted=False)

    assert task.context_id == "ctx-1"
    assert task.state == "working"
    assert persistence.load_task("task-1").state == "working"


@pytest.mark.asyncio
async def test_get_returns_persisted_task_after_restart(tmp_path) -> None:
    persistence = A2APersistenceStore(tmp_path)
    persistence.save_task(A2ATaskSnapshot(task_id="task-1", context_id="ctx-1", state="input-required"))
    store = A2ATaskStore(metrics=NoOpA2AMetrics(), persistence=persistence)

    task = await store.get("task-1")

    assert task is not None
    assert task.id == "task-1"
    assert task.context_id == "ctx-1"
    assert task.status.state == TaskState.TASK_STATE_INPUT_REQUIRED


@pytest.mark.asyncio
async def test_get_does_not_mutate_running_persisted_task_after_restart(tmp_path) -> None:
    persistence = A2APersistenceStore(tmp_path)
    persistence.save_task(A2ATaskSnapshot(task_id="task-1", context_id="ctx-1", state="working"))
    store = A2ATaskStore(metrics=NoOpA2AMetrics(), persistence=persistence)

    task = await store.get("task-1")

    assert task is not None
    assert task.status.state == TaskState.TASK_STATE_WORKING
    assert persistence.load_task("task-1").state == "working"


@pytest.mark.asyncio
async def test_get_returns_persisted_task_for_matching_authenticated_owner_after_restart(tmp_path) -> None:
    persistence = A2APersistenceStore(tmp_path)
    persistence.save_task(A2ATaskSnapshot(task_id="task-1", context_id="ctx-1", state="input-required", owner="alice"))
    store = A2ATaskStore(metrics=NoOpA2AMetrics(), persistence=persistence)

    alice_task = await store.get("task-1", context=call_context("alice"))
    bob_task = await store.get("task-1", context=call_context("bob"))

    assert alice_task is not None
    assert alice_task.id == "task-1"
    assert alice_task.context_id == "ctx-1"
    assert bob_task is None


@pytest.mark.asyncio
async def test_authenticated_get_and_list_hide_legacy_ownerless_persisted_task_after_restart(tmp_path) -> None:
    persistence = A2APersistenceStore(tmp_path)
    persistence.save_task(A2ATaskSnapshot(task_id="task-1", context_id="ctx-1", state="input-required"))
    store = A2ATaskStore(metrics=NoOpA2AMetrics(), persistence=persistence)

    task = await store.get("task-1", context=call_context("alice"))
    response = await store.list(ListTasksRequest(context_id="ctx-1"), context=call_context("alice"))

    assert task is None
    assert response.tasks == []


@pytest.mark.asyncio
async def test_get_or_create_task_claims_legacy_ownerless_snapshot_for_authenticated_owner(tmp_path) -> None:
    persistence = A2APersistenceStore(tmp_path)
    persistence.save_task(A2ATaskSnapshot(task_id="task-1", context_id="ctx-1", state="input-required"))
    store = A2ATaskStore(metrics=NoOpA2AMetrics(), persistence=persistence)

    record = await store.get_or_create_task(task_id="task-1", context_id="ctx-1", owner="alice")

    assert record.owner == "alice"
    assert persistence.load_task("task-1").owner == "alice"
    assert await store.get("task-1", context=call_context("bob")) is None


@pytest.mark.asyncio
async def test_cleanup_does_not_evict_in_flight_context() -> None:
    store = A2ATaskStore(metrics=NoOpA2AMetrics(), idle_timeout_seconds=0, cleanup_interval_seconds=300)
    context = await store.get_or_create_context(context_id="ctx-1", cwd="/tmp", runtime_factory=lambda sid: object())
    context.active_task_id = "task-1"

    await store.cleanup_once(now_offset_seconds=1)

    same = await store.get_or_create_context(context_id="ctx-1", cwd="/tmp", runtime_factory=lambda sid: object())
    assert same is context


@pytest.mark.asyncio
async def test_list_filters_by_context_with_index() -> None:
    store = A2ATaskStore(metrics=NoOpA2AMetrics(), idle_timeout_seconds=60, cleanup_interval_seconds=300)
    await store.save(Task(id="task-1", context_id="ctx-a", status=TaskStatus(state="TASK_STATE_SUBMITTED")))
    await store.save(Task(id="task-2", context_id="ctx-b", status=TaskStatus(state="TASK_STATE_SUBMITTED")))

    response = await store.list(ListTasksRequest(context_id="ctx-a"))

    assert [task.id for task in response.tasks] == ["task-1"]


@pytest.mark.asyncio
async def test_list_includes_persisted_tasks_for_matching_authenticated_owner_after_restart(tmp_path) -> None:
    persistence = A2APersistenceStore(tmp_path)
    persistence.save_task(
        A2ATaskSnapshot(task_id="alice-task", context_id="ctx-a", state="input-required", owner="alice")
    )
    persistence.save_task(A2ATaskSnapshot(task_id="bob-task", context_id="ctx-b", state="input-required", owner="bob"))
    store = A2ATaskStore(metrics=NoOpA2AMetrics(), persistence=persistence)

    alice = await store.list(ListTasksRequest(), context=call_context("alice"))
    bob = await store.list(ListTasksRequest(), context=call_context("bob"))

    assert [task.id for task in alice.tasks] == ["alice-task"]
    assert [task.id for task in bob.tasks] == ["bob-task"]


@pytest.mark.asyncio
async def test_list_does_not_mutate_running_persisted_task_after_restart(tmp_path) -> None:
    persistence = A2APersistenceStore(tmp_path)
    persistence.save_task(A2ATaskSnapshot(task_id="task-1", context_id="ctx-1", state="working"))
    store = A2ATaskStore(metrics=NoOpA2AMetrics(), persistence=persistence)

    response = await store.list(ListTasksRequest())

    assert [task.id for task in response.tasks] == ["task-1"]
    assert response.tasks[0].status.state == TaskState.TASK_STATE_WORKING
    assert persistence.load_task("task-1").state == "working"


@pytest.mark.asyncio
async def test_list_filters_persisted_tasks_by_context_after_restart(tmp_path) -> None:
    persistence = A2APersistenceStore(tmp_path)
    persistence.save_task(A2ATaskSnapshot(task_id="task-a", context_id="ctx-a", state="input-required"))
    persistence.save_task(A2ATaskSnapshot(task_id="task-b", context_id="ctx-b", state="input-required"))
    store = A2ATaskStore(metrics=NoOpA2AMetrics(), persistence=persistence)

    response = await store.list(ListTasksRequest(context_id="ctx-a"))

    assert [task.id for task in response.tasks] == ["task-a"]


@pytest.mark.asyncio
async def test_list_sorts_and_filters_persisted_tasks_by_updated_at_after_restart(tmp_path) -> None:
    persistence = A2APersistenceStore(tmp_path)
    persistence.save_task(
        A2ATaskSnapshot(task_id="task-old", context_id="ctx-1", state="input-required", updated_at=10)
    )
    persistence.save_task(
        A2ATaskSnapshot(task_id="task-new", context_id="ctx-1", state="input-required", updated_at=30)
    )
    persistence.save_task(
        A2ATaskSnapshot(task_id="task-mid", context_id="ctx-1", state="input-required", updated_at=20)
    )
    store = A2ATaskStore(metrics=NoOpA2AMetrics(), persistence=persistence)
    after = timestamp(20)

    first = await store.list(
        ListTasksRequest(
            status=TaskState.TASK_STATE_INPUT_REQUIRED,
            status_timestamp_after=after,
            page_size=1,
        )
    )
    second = await store.list(
        ListTasksRequest(
            status=TaskState.TASK_STATE_INPUT_REQUIRED,
            status_timestamp_after=after,
            page_size=1,
            page_token=first.next_page_token,
        )
    )

    assert [task.id for task in first.tasks] == ["task-new"]
    assert first.next_page_token
    assert [task.id for task in second.tasks] == ["task-mid"]
    assert second.next_page_token == ""


@pytest.mark.asyncio
async def test_list_preserves_running_sdk_task_timestamp_after_restart(tmp_path) -> None:
    persistence = A2APersistenceStore(tmp_path)
    writer = A2ATaskStore(metrics=NoOpA2AMetrics(), persistence=persistence)
    await writer.save(sdk_task("task-old", state=TaskState.TASK_STATE_WORKING, updated_at=10))
    await writer.save(sdk_task("task-new", state=TaskState.TASK_STATE_WORKING, updated_at=30))
    reader = A2ATaskStore(metrics=NoOpA2AMetrics(), persistence=persistence)

    response = await reader.list(
        ListTasksRequest(
            status=TaskState.TASK_STATE_WORKING,
            status_timestamp_after=timestamp(20),
        )
    )

    assert [task.id for task in response.tasks] == ["task-new"]
    assert response.tasks[0].status.timestamp.seconds == 30
    assert persistence.load_task("task-new").state == "working"


@pytest.mark.asyncio
async def test_get_task_record_does_not_mutate_running_persisted_task_after_restart(tmp_path) -> None:
    persistence = A2APersistenceStore(tmp_path)
    persistence.save_task(A2ATaskSnapshot(task_id="task-1", context_id="ctx-1", state="working"))
    store = A2ATaskStore(metrics=NoOpA2AMetrics(), persistence=persistence)

    record = await store.get_task_record("task-1")

    assert record.state == "working"
    assert persistence.load_task("task-1").state == "working"


@pytest.mark.asyncio
async def test_list_compares_persisted_fractional_timestamps_numerically(tmp_path) -> None:
    persistence = A2APersistenceStore(tmp_path)
    persistence.save_task(
        A2ATaskSnapshot(task_id="task-exact", context_id="ctx-1", state="input-required", updated_at=20)
    )
    persistence.save_task(
        A2ATaskSnapshot(task_id="task-half", context_id="ctx-1", state="input-required", updated_at=20.5)
    )
    store = A2ATaskStore(metrics=NoOpA2AMetrics(), persistence=persistence)

    response = await store.list(
        ListTasksRequest(
            status=TaskState.TASK_STATE_INPUT_REQUIRED,
            status_timestamp_after=timestamp(20),
        )
    )

    assert [task.id for task in response.tasks] == ["task-half", "task-exact"]
    assert response.tasks[0].status.timestamp == timestamp_with_nanos(20, 500_000_000)


@pytest.mark.asyncio
async def test_list_filters_status_sorts_desc_and_paginates_with_cursor() -> None:
    store = A2ATaskStore(metrics=NoOpA2AMetrics(), idle_timeout_seconds=60, cleanup_interval_seconds=300)
    await store.save(sdk_task("task-old", state=TaskState.TASK_STATE_WORKING, updated_at=10))
    await store.save(sdk_task("task-new", state=TaskState.TASK_STATE_WORKING, updated_at=30))
    await store.save(sdk_task("task-failed", state=TaskState.TASK_STATE_FAILED, updated_at=40))
    await store.save(sdk_task("task-mid", state=TaskState.TASK_STATE_WORKING, updated_at=20))

    first = await store.list(ListTasksRequest(status=TaskState.TASK_STATE_WORKING, page_size=2))

    assert [task.id for task in first.tasks] == ["task-new", "task-mid"]
    assert first.page_size == 2
    assert first.total_size == 3
    assert first.next_page_token

    second = await store.list(
        ListTasksRequest(status=TaskState.TASK_STATE_WORKING, page_size=2, page_token=first.next_page_token)
    )

    assert [task.id for task in second.tasks] == ["task-old"]
    assert second.next_page_token == ""


@pytest.mark.asyncio
async def test_list_rejects_invalid_page_token() -> None:
    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    await store.save(sdk_task("task-1"))

    with pytest.raises(InvalidParamsError, match="Invalid page token"):
        await store.list(ListTasksRequest(page_token="bWlzc2luZw=="))


@pytest.mark.asyncio
async def test_list_omits_artifacts_by_default_and_keeps_internal_task_unchanged() -> None:
    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    await store.save(sdk_task("task-1", with_artifact=True))

    response = await store.list(ListTasksRequest())

    assert len(response.tasks[0].artifacts) == 0
    assert len((await store.get("task-1")).artifacts) == 1


@pytest.mark.asyncio
async def test_list_includes_artifacts_when_requested() -> None:
    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    await store.save(sdk_task("task-1", with_artifact=True))

    response = await store.list(ListTasksRequest(include_artifacts=True))

    assert response.tasks[0].artifacts[0].artifact_id == "artifact-task-1"


@pytest.mark.asyncio
async def test_task_store_scopes_sdk_tasks_by_authenticated_user() -> None:
    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    await store.save(sdk_task("alice-task"), context=call_context("alice"))
    await store.save(sdk_task("bob-task"), context=call_context("bob"))

    alice = await store.list(ListTasksRequest(), context=call_context("alice"))
    bob = await store.list(ListTasksRequest(), context=call_context("bob"))

    assert [task.id for task in alice.tasks] == ["alice-task"]
    assert [task.id for task in bob.tasks] == ["bob-task"]
    assert await store.get("bob-task", context=call_context("alice")) is None


@pytest.mark.asyncio
async def test_save_persists_sdk_task_owner_even_before_executor_record_exists(tmp_path) -> None:
    persistence = A2APersistenceStore(tmp_path)
    store = A2ATaskStore(metrics=NoOpA2AMetrics(), persistence=persistence)

    await store.save(sdk_task("task-1", context_id="ctx-1"), context=call_context("alice"))

    snapshot = persistence.load_task("task-1")
    assert snapshot is not None
    assert snapshot.context_id == "ctx-1"
    assert snapshot.owner == "alice"


@pytest.mark.asyncio
async def test_save_updates_existing_executor_record_state_in_persistence(tmp_path) -> None:
    persistence = A2APersistenceStore(tmp_path)
    store = A2ATaskStore(metrics=NoOpA2AMetrics(), persistence=persistence)
    record = await store.get_or_create_task(task_id="task-1", context_id="ctx-1")

    await store.save(sdk_task("task-1", context_id="ctx-1", state=TaskState.TASK_STATE_COMPLETED))

    snapshot = persistence.load_task("task-1")
    assert record.state == "completed"
    assert snapshot is not None
    assert snapshot.state == "completed"


@pytest.mark.asyncio
async def test_save_skips_only_shared_task_snapshot_when_sdk_state_is_unchanged(monkeypatch, tmp_path) -> None:
    config_dir = tmp_path / "config"
    cwd = tmp_path / "workspace"
    cwd.mkdir()
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(config_dir))
    persistence = CountingTaskPersistence(config_dir / "a2a")
    store = A2ATaskStore(metrics=NoOpA2AMetrics(), persistence=persistence)
    context = await store.get_or_create_context(context_id="ctx-1", cwd=str(cwd), runtime_factory=lambda sid: object())
    await store.get_or_create_task(task_id="task-1", context_id="ctx-1")

    await store.save(sdk_task("task-1", context_id="ctx-1", state=TaskState.TASK_STATE_WORKING, updated_at=1))
    await store.save(sdk_task("task-1", context_id="ctx-1", state=TaskState.TASK_STATE_WORKING, updated_at=2))

    live_task = await store.get("task-1")
    persisted_task = persistence.load_task("task-1")
    session_task_path = SessionStorage().session_dir(str(cwd), context.session_id) / "a2a" / "task.json"
    session_task = json.loads(session_task_path.read_text(encoding="utf-8"))
    assert persistence.task_save_count == 2
    assert live_task is not None
    assert live_task.status.timestamp == timestamp(2)
    assert persisted_task is not None
    assert persisted_task.state == "working"
    assert persisted_task.updated_at == 1
    assert session_task["state"] == "working"
    assert session_task["updated_at"] == 2


@pytest.mark.asyncio
async def test_save_persists_sdk_state_transitions_and_explicit_task_mirrors(tmp_path) -> None:
    persistence = CountingTaskPersistence(tmp_path / "a2a")
    store = A2ATaskStore(metrics=NoOpA2AMetrics(), persistence=persistence)
    record = await store.get_or_create_task(task_id="task-1", context_id="ctx-1")

    await store.save(sdk_task("task-1", context_id="ctx-1", state=TaskState.TASK_STATE_WORKING, updated_at=1))
    await store.save(sdk_task("task-1", context_id="ctx-1", state=TaskState.TASK_STATE_WORKING, updated_at=2))
    record.output_text.append("delivered")
    store.mirror_task(record)
    await store.save(sdk_task("task-1", context_id="ctx-1", state=TaskState.TASK_STATE_COMPLETED, updated_at=3))

    snapshot = persistence.load_task("task-1")
    assert persistence.task_save_count == 4
    assert snapshot is not None
    assert snapshot.state == "completed"
    assert snapshot.output_text == ["delivered"]
    assert snapshot.updated_at == 3


@pytest.mark.asyncio
async def test_save_retries_failed_shared_task_snapshot_when_sdk_state_is_unchanged(tmp_path) -> None:
    persistence = FlakyTaskPersistence(tmp_path / "a2a")
    store = A2ATaskStore(metrics=NoOpA2AMetrics(), persistence=persistence)
    await store.get_or_create_task(task_id="task-1", context_id="ctx-1")

    await store.save(sdk_task("task-1", context_id="ctx-1", updated_at=2))

    snapshot = persistence.load_task("task-1")
    assert persistence.task_save_count == 2
    assert snapshot is not None
    assert snapshot.state == "submitted"
    assert snapshot.updated_at == 2


@pytest.mark.asyncio
async def test_save_updates_session_task_snapshot_after_restart_without_contexts(monkeypatch, tmp_path) -> None:
    config_dir = tmp_path / "config"
    cwd = tmp_path / "workspace"
    cwd.mkdir()
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(config_dir))
    persistence = A2APersistenceStore(config_dir / "a2a")
    writer = A2ATaskStore(metrics=NoOpA2AMetrics(), persistence=persistence)
    context = await writer.get_or_create_context(context_id="ctx-1", cwd=str(cwd), runtime_factory=lambda sid: object())
    await writer.get_or_create_task(task_id="task-1", context_id="ctx-1")
    session_task_path = SessionStorage().session_dir(str(cwd), context.session_id) / "a2a" / "task.json"
    assert json.loads(session_task_path.read_text(encoding="utf-8"))["state"] == "submitted"

    restarted = A2ATaskStore(metrics=NoOpA2AMetrics(), persistence=persistence)
    await restarted.save(sdk_task("task-1", context_id="ctx-1", state=TaskState.TASK_STATE_CANCELED, updated_at=2))

    session_task = json.loads(session_task_path.read_text(encoding="utf-8"))
    assert session_task["state"] == "canceled"
    assert persistence.load_task("task-1").state == "canceled"


@pytest.mark.asyncio
async def test_session_snapshots_are_written_without_global_persistence(monkeypatch, tmp_path) -> None:
    config_dir = tmp_path / "config"
    cwd = tmp_path / "workspace"
    cwd.mkdir()
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(config_dir))
    store = A2ATaskStore(metrics=NoOpA2AMetrics(), persistence=None)

    context = await store.get_or_create_context(context_id="ctx-1", cwd=str(cwd), runtime_factory=lambda sid: object())
    await store.get_or_create_task(task_id="task-1", context_id="ctx-1")

    session_dir = SessionStorage().session_dir(str(cwd), context.session_id)
    session_context = json.loads((session_dir / "a2a" / "context.json").read_text(encoding="utf-8"))
    session_task = json.loads((session_dir / "a2a" / "task.json").read_text(encoding="utf-8"))
    assert session_context["context_id"] == "ctx-1"
    assert session_context["session_id"] == context.session_id
    assert session_task["task_id"] == "task-1"
    assert session_task["context_id"] == "ctx-1"
    assert not (config_dir / "a2a" / "tasks" / "task-1.json").exists()


@pytest.mark.asyncio
async def test_session_snapshots_refuse_symlinked_a2a_dir(monkeypatch, tmp_path) -> None:
    config_dir = tmp_path / "config"
    cwd = tmp_path / "workspace"
    cwd.mkdir()
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(config_dir))
    store = A2ATaskStore(metrics=NoOpA2AMetrics(), persistence=None)

    context = await store.get_or_create_context(context_id="ctx-1", cwd=str(cwd), runtime_factory=lambda sid: object())
    task = await store.get_or_create_task(task_id="task-1", context_id="ctx-1")
    session_dir = SessionStorage().session_dir(str(cwd), context.session_id)
    shutil.rmtree(session_dir / "a2a")
    outside = tmp_path / "outside-a2a"
    outside.mkdir()
    _symlink_or_skip(outside, session_dir / "a2a", target_is_directory=True)
    task.state = "working"

    store.mirror_task(task)
    store.mirror_context(context)

    assert not (outside / "task.json").exists()
    assert not (outside / "context.json").exists()


@pytest.mark.asyncio
async def test_mirror_task_updates_internal_record_timestamp(tmp_path) -> None:
    persistence = A2APersistenceStore(tmp_path)
    store = A2ATaskStore(metrics=NoOpA2AMetrics(), persistence=persistence)
    record = await store.get_or_create_task(task_id="task-1", context_id="ctx-1")
    record.updated_at = 10
    record.state = "completed"

    store.mirror_task(record)

    snapshot = persistence.load_task("task-1")
    assert snapshot is not None
    assert snapshot.state == "completed"
    assert snapshot.updated_at > 10


@pytest.mark.asyncio
async def test_task_store_mirrors_task_and_context_to_persistence(tmp_path) -> None:
    from iac_code.a2a.persistence import A2APersistenceStore

    persistence = A2APersistenceStore(tmp_path)
    store = A2ATaskStore(metrics=NoOpA2AMetrics(), persistence=persistence)

    context = await store.get_or_create_context(context_id="ctx-1", cwd="/tmp", runtime_factory=lambda sid: object())
    task = await store.get_or_create_task(task_id="task-1", context_id="ctx-1")

    assert persistence.load_context("ctx-1").session_id == context.session_id
    assert persistence.load_task("task-1").context_id == task.context_id


@pytest.mark.asyncio
async def test_get_or_create_context_restores_persisted_session_id(tmp_path) -> None:
    from iac_code.a2a.persistence import A2APersistenceStore

    persistence = A2APersistenceStore(tmp_path)
    store_one = A2ATaskStore(metrics=NoOpA2AMetrics(), persistence=persistence)
    original = await store_one.get_or_create_context(
        context_id="ctx-1", cwd="/tmp", runtime_factory=lambda sid: f"rt-{sid}"
    )

    store_two = A2ATaskStore(metrics=NoOpA2AMetrics(), persistence=persistence)
    restored = await store_two.get_or_create_context(
        context_id="ctx-1", cwd="/tmp", runtime_factory=lambda sid: f"rt-{sid}"
    )

    assert restored.session_id == original.session_id
    assert restored.runtime == f"rt-{original.session_id}"


@pytest.mark.asyncio
async def test_get_or_create_context_persisted_cwd_mismatch_raises(tmp_path) -> None:
    from iac_code.a2a.persistence import A2APersistenceStore

    persistence = A2APersistenceStore(tmp_path)
    store_one = A2ATaskStore(metrics=NoOpA2AMetrics(), persistence=persistence)
    await store_one.get_or_create_context(context_id="ctx-1", cwd="/tmp/one", runtime_factory=lambda sid: object())

    store_two = A2ATaskStore(metrics=NoOpA2AMetrics(), persistence=persistence)
    with pytest.raises(ValueError, match="different workspace"):
        await store_two.get_or_create_context(context_id="ctx-1", cwd="/tmp/two", runtime_factory=lambda sid: object())


@pytest.mark.asyncio
async def test_task_store_persistence_failure_does_not_abort_task_creation() -> None:
    store = A2ATaskStore(metrics=NoOpA2AMetrics(), persistence=FailingPersistence())

    task = await store.get_or_create_task(task_id="task-1", context_id="ctx-1")

    assert task.task_id == "task-1"


@pytest.mark.asyncio
async def test_has_active_work_tracks_running_tasks() -> None:
    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    await store.get_or_create_context(context_id="ctx-1", cwd="/tmp", runtime_factory=lambda sid: object())
    task = await store.get_or_create_task(task_id="task-1", context_id="ctx-1")
    release = asyncio.Event()
    active_task = asyncio.create_task(release.wait())
    task.active_task = active_task

    assert await store.has_active_work() is True
    release.set()
    await active_task
    assert await store.has_active_work() is False


@pytest.mark.asyncio
async def test_cleanup_loop_survives_cleanup_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    store = A2ATaskStore(metrics=NoOpA2AMetrics(), cleanup_interval_seconds=0.01)
    calls = 0

    async def flaky_cleanup_once() -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("cleanup failed")

    monkeypatch.setattr(store, "cleanup_once", flaky_cleanup_once)

    await store.start_cleanup_loop()
    await asyncio.sleep(0.15)
    await store.stop_cleanup_loop()

    assert calls >= 2
