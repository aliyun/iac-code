import asyncio

import pytest
from a2a.auth.user import User
from a2a.server.context import ServerCallContext
from a2a.types import Artifact, ListTasksRequest, Part, Task, TaskState, TaskStatus
from a2a.utils.errors import InvalidParamsError
from google.protobuf.timestamp_pb2 import Timestamp

from iac_code.a2a.metrics import NoOpA2AMetrics
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
