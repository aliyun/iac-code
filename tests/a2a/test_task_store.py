import asyncio
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
from iac_code.a2a.persistence import A2APersistenceStore, A2ATaskSnapshot
from iac_code.a2a.task_store import A2ATaskStore


class FailingPersistence:
    def __init__(self) -> None:
        self.fail = True

    def save_task(self, snapshot) -> None:
        if self.fail:
            raise OSError("disk full")

    def save_context(self, snapshot) -> None:
        if self.fail:
            raise OSError("disk full")


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
async def test_context_runtime_factory_runs_outside_mutation_lock() -> None:
    store = A2ATaskStore(metrics=NoOpA2AMetrics(), idle_timeout_seconds=60, cleanup_interval_seconds=300)

    def slow_runtime_factory(session_id: str):
        time.sleep(0.2)
        return f"rt-{session_id}"

    start = time.monotonic()
    context_task = asyncio.create_task(
        store.get_or_create_context(context_id="ctx-1", cwd="/tmp", runtime_factory=slow_runtime_factory)
    )
    await asyncio.sleep(0.01)

    await asyncio.wait_for(store.save(sdk_task("task-while-runtime-starts")), timeout=0.1)
    save_elapsed = time.monotonic() - start

    context = await context_task
    assert context.runtime == f"rt-{context.session_id}"
    assert save_elapsed < 0.15


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
