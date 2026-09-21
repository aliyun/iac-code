import asyncio
import multiprocessing
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from a2a.utils.errors import InvalidParamsError

from iac_code.a2a.executor import IacCodeA2AExecutor
from iac_code.a2a.metrics import NoOpA2AMetrics
from iac_code.a2a.persistence import A2AContextSnapshot, A2APersistenceStore
from iac_code.a2a.pipeline_continuation import (
    PipelineCheckpointIdentity,
    PipelineContinuationConflictError,
    PipelineContinuationCorruptError,
    PipelineContinuationStore,
    PipelineContinuationUnsafeError,
    checkpoint_identity,
)
from iac_code.a2a.pipeline_executor import IacCodeA2APipelineExecutor, successor_task_id_from_sidecar
from iac_code.a2a.pipeline_journal import A2APipelineJournal
from iac_code.a2a.pipeline_paths import a2a_pipeline_dir_for_session
from iac_code.a2a.pipeline_snapshot import A2APipelineSnapshotStore, reduce_pipeline_events
from iac_code.a2a.task_store import A2ATaskStore
from iac_code.services.session_storage import SessionStorage


@pytest.fixture(autouse=True)
def _freeze_snapshot_generated_at(monkeypatch: pytest.MonkeyPatch) -> None:
    # reduce_pipeline_events stamps a wall-clock second into generatedAt and
    # checkpoint_identity digests the whole snapshot. A re-reduction on the far
    # side of a second boundary (slow CI runners) would change the digest and
    # fail-close the continuation, so keep the stamp deterministic.
    monkeypatch.setattr("iac_code.a2a.pipeline_snapshot._utc_now", lambda: "2026-01-01T00:00:00Z")


def _checkpoint() -> PipelineCheckpointIdentity:
    return PipelineCheckpointIdentity(version=1, digest="a" * 64, sequence=7, event_id="evt-7")


def _reserve(store: PipelineContinuationStore, invocation_id: str = "request-1"):
    return store.reserve_successor(
        context_id="ctx-1",
        predecessor_task_id="task-old",
        invocation_id=invocation_id,
        checkpoint=_checkpoint(),
        cancellation_execution_id="execution-old",
        cancellation_revision=11,
        cancellation_backup_generation=7,
        cancellation_backup_commit_id="commit-7",
    )


def _store(pipeline_dir: Path) -> PipelineContinuationStore:
    return PipelineContinuationStore(pipeline_dir, session_dir=pipeline_dir.parent.parent)


def _claim_worker(pipeline_dir: str, successor_task_id: str, queue) -> None:
    try:
        _store(Path(pipeline_dir)).claim_execution(
            successor_task_id=successor_task_id,
            invocation_id="request-1",
            checkpoint=_checkpoint(),
            expected_fence=1,
        )
    except PipelineContinuationConflictError:
        queue.put("rejected")
    else:
        queue.put("executed")


def test_successor_intent_is_unique_across_store_instances(tmp_path) -> None:
    stores = [_store(tmp_path / "session" / "a2a" / "pipeline") for _ in range(8)]
    with ThreadPoolExecutor(max_workers=len(stores)) as executor:
        successor_ids = set(executor.map(lambda store: _reserve(store).successor_task_id, stores))

    assert len(successor_ids) == 1
    intent = stores[0].load()
    assert intent is not None
    assert intent.invocation_id == "request-1"
    assert intent.checkpoint == _checkpoint()
    assert intent.cancellation_revision == 11


def test_corrupt_intent_fails_closed_instead_of_allocating_another_successor(tmp_path) -> None:
    pipeline_dir = tmp_path / "session" / "a2a" / "pipeline"
    pipeline_dir.mkdir(parents=True)
    (pipeline_dir / "continuation-intent.json").write_text("{truncated", encoding="utf-8")

    with pytest.raises(PipelineContinuationCorruptError):
        _reserve(_store(pipeline_dir))


def test_checkpoint_or_cancellation_proof_change_fails_closed(tmp_path) -> None:
    store = _store(tmp_path / "session" / "a2a" / "pipeline")
    _reserve(store)

    with pytest.raises(PipelineContinuationConflictError):
        store.reserve_successor(
            context_id="ctx-1",
            predecessor_task_id="task-old",
            invocation_id="request-retry",
            checkpoint=PipelineCheckpointIdentity(version=1, digest="b" * 64, sequence=8, event_id="evt-8"),
            cancellation_execution_id="execution-old",
            cancellation_revision=11,
            cancellation_backup_generation=7,
            cancellation_backup_commit_id="commit-7",
        )


def test_multiple_processes_claim_successor_execution_exactly_once(tmp_path) -> None:
    pipeline_dir = tmp_path / "session" / "a2a" / "pipeline"
    intent = _reserve(_store(pipeline_dir))
    context = multiprocessing.get_context("spawn")
    queue = context.Queue()
    workers = [
        context.Process(target=_claim_worker, args=(str(pipeline_dir), intent.successor_task_id, queue))
        for _ in range(4)
    ]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=10)
        assert worker.exitcode == 0

    outcomes = [queue.get(timeout=2) for _ in workers]
    assert outcomes.count("executed") == 1
    assert outcomes.count("rejected") == 3


def test_claim_rejects_stale_fence_without_consuming_intent(tmp_path) -> None:
    store = _store(tmp_path / "session" / "a2a" / "pipeline")
    intent = _reserve(store)

    with pytest.raises(PipelineContinuationConflictError):
        store.claim_execution(
            successor_task_id=intent.successor_task_id,
            invocation_id=intent.invocation_id,
            checkpoint=intent.checkpoint,
            expected_fence=intent.fence + 1,
        )

    assert store.load() == intent


def test_interrupted_intent_write_fails_closed_on_retry(tmp_path, monkeypatch) -> None:
    pipeline_dir = tmp_path / "session" / "a2a" / "pipeline"
    store = _store(pipeline_dir)

    def interrupted_write(path, _payload, *, durable):
        assert durable is True
        path.write_text("{partial", encoding="utf-8")
        raise OSError("simulated fsync failure")

    monkeypatch.setattr("iac_code.a2a.pipeline_continuation.atomic_write_json", interrupted_write)
    with pytest.raises(OSError, match="fsync failure"):
        _reserve(store)
    with pytest.raises(PipelineContinuationCorruptError):
        _reserve(store)


def _canceled_sidecar(cwd: Path, session_id: str) -> tuple[Path, dict, dict]:
    pipeline_dir = a2a_pipeline_dir_for_session(cwd=str(cwd), session_id=session_id)
    event = {
        "schemaVersion": "1.0",
        "eventId": "evt-canceled",
        "sequence": 1,
        "eventType": "pipeline_canceled",
        "pipelineRunId": "ctx-1",
        "taskId": "task-old",
        "contextId": "ctx-1",
        "status": "canceled",
        "data": {},
    }
    journal = A2APipelineJournal(pipeline_dir)
    journal.append(event)
    snapshot = reduce_pipeline_events([event])
    A2APipelineSnapshotStore(pipeline_dir).save(snapshot)
    sidecar_dir = SessionStorage().session_dir(str(cwd), session_id) / "pipeline"
    sidecar_dir.mkdir(parents=True, exist_ok=True)
    meta = {"status": "canceled", "current_step": "deploy"}
    (sidecar_dir / "meta.yaml").write_text(yaml.safe_dump(meta), encoding="utf-8")
    return pipeline_dir, event, {"a2a": snapshot, "pipelineMeta": meta}


def test_canceled_sidecar_reserves_same_successor_on_retry(tmp_path) -> None:
    cwd = tmp_path / "workspace"
    pipeline_dir, _, _ = _canceled_sidecar(cwd, "session-1")
    kwargs = {
        "cwd": str(cwd),
        "session_id": "session-1",
        "context_id": "ctx-1",
        "canceled_task_id": "task-old",
        "invocation_id": "request-1",
        "cancellation_proof": {"executionId": "execution-old", "revision": 11},
    }

    first = successor_task_id_from_sidecar(**kwargs)
    second = successor_task_id_from_sidecar(**kwargs)

    assert first is not None
    assert second == first
    assert _store(pipeline_dir).load().invocation_id == "request-1"

    with pytest.raises(PipelineContinuationConflictError):
        successor_task_id_from_sidecar(**{**kwargs, "invocation_id": "different-request"})


@pytest.mark.asyncio
async def test_claimed_successor_continues_exact_canceled_checkpoint(tmp_path) -> None:
    cwd = tmp_path / "workspace"
    pipeline_dir, event, checkpoint_payload = _canceled_sidecar(cwd, "session-1")
    store = _store(pipeline_dir)
    intent = store.reserve_successor(
        context_id="ctx-1",
        predecessor_task_id="task-old",
        invocation_id="request-1",
        checkpoint=checkpoint_identity(checkpoint_payload, event),
        cancellation_execution_id="execution-old",
        cancellation_revision=11,
        cancellation_backup_generation=7,
        cancellation_backup_commit_id="commit-7",
    )
    store.claim_execution(
        successor_task_id=intent.successor_task_id,
        invocation_id=intent.invocation_id,
        checkpoint=intent.checkpoint,
        expected_fence=intent.fence,
    )

    class Pipeline:
        sidecar_status = "canceled"

        def __init__(self) -> None:
            self.inputs = []

        def restore_canceled_checkpoint_sync(self):
            return SimpleNamespace(ok=True, status="canceled")

        def canceled_checkpoint_safety(self):
            return SimpleNamespace(safe=True, reason="step_boundary")

        async def continue_from_sidecar(self, user_input=None):
            self.inputs.append(user_input)
            yield "continued"

    pipeline = Pipeline()
    executor = object.__new__(IacCodeA2APipelineExecutor)
    journal = A2APipelineJournal(pipeline_dir)
    select_kwargs = {
        "pipeline_input": SimpleNamespace(display_text="next request", content="next request", has_images=False),
        "publisher": SimpleNamespace(journal=journal, snapshot_store=A2APipelineSnapshotStore(pipeline_dir)),
        "task_id": intent.successor_task_id,
        "context_id": "ctx-1",
        "fresh_pipeline_factory": lambda: (_ for _ in ()).throw(AssertionError("must not start fresh")),
    }
    abandoned_before_iteration = await executor._select_stream(
        pipeline,
        "next request",
        **select_kwargs,
    )
    assert store.load().phase == "claimed"
    selected = await executor._select_stream(pipeline, "next request", **select_kwargs)
    claimed = store.load()
    store.begin_execution(
        successor_task_id=intent.successor_task_id,
        claim_id=claimed.claim_id,
        expected_fence=intent.fence,
    )

    assert [item async for item in selected.stream] == ["continued"]
    await abandoned_before_iteration.stream.aclose()
    assert store.load().phase == "running"
    assert pipeline.inputs == ["next request"]
    with pytest.raises(PipelineContinuationUnsafeError, match="successor_execution_outcome_unverified"):
        await executor._select_stream(pipeline, "retry after running", **select_kwargs)
    assert store.load().phase == "unsafe"


@pytest.mark.asyncio
async def test_unverified_active_attempt_is_durably_unsafe_and_never_continues(tmp_path) -> None:
    cwd = tmp_path / "workspace"
    pipeline_dir, event, checkpoint_payload = _canceled_sidecar(cwd, "session-1")
    store = _store(pipeline_dir)
    intent = store.reserve_successor(
        context_id="ctx-1",
        predecessor_task_id="task-old",
        invocation_id="request-1",
        checkpoint=checkpoint_identity(checkpoint_payload, event),
        cancellation_execution_id="execution-old",
        cancellation_revision=11,
        cancellation_backup_generation=7,
        cancellation_backup_commit_id="commit-7",
    )
    store.claim_execution(
        successor_task_id=intent.successor_task_id,
        invocation_id=intent.invocation_id,
        checkpoint=intent.checkpoint,
        expected_fence=intent.fence,
    )

    class UnsafePipeline:
        sidecar_status = "canceled"

        def __init__(self) -> None:
            self.continued = False

        def restore_canceled_checkpoint_sync(self):
            return SimpleNamespace(ok=True, status="canceled")

        def canceled_checkpoint_safety(self):
            return SimpleNamespace(safe=False, reason="active_attempt_outcome_unverified")

        async def continue_from_sidecar(self, user_input=None):
            self.continued = True
            yield "must-not-run"

    pipeline = UnsafePipeline()
    executor = object.__new__(IacCodeA2APipelineExecutor)
    publisher = SimpleNamespace(
        journal=A2APipelineJournal(pipeline_dir),
        snapshot_store=A2APipelineSnapshotStore(pipeline_dir),
    )
    kwargs = {
        "pipeline_input": SimpleNamespace(display_text="next request", content="next request", has_images=False),
        "publisher": publisher,
        "task_id": intent.successor_task_id,
        "context_id": "ctx-1",
        "fresh_pipeline_factory": lambda: (_ for _ in ()).throw(AssertionError("must not start fresh")),
    }

    with pytest.raises(PipelineContinuationUnsafeError, match="CONTINUATION_UNSAFE"):
        await executor._select_stream(pipeline, "next request", **kwargs)
    with pytest.raises(PipelineContinuationUnsafeError, match="CONTINUATION_UNSAFE"):
        await executor._select_stream(pipeline, "retry", **kwargs)

    persisted = store.load()
    assert persisted is not None
    assert persisted.phase == "unsafe"
    assert persisted.unsafe_reason == "active_attempt_outcome_unverified"
    assert pipeline.continued is False


@pytest.mark.asyncio
async def test_unsafe_intent_still_resolves_same_successor_after_failed_owner_projection(tmp_path) -> None:
    cwd = tmp_path / "workspace"
    pipeline_dir, event, checkpoint_payload = _canceled_sidecar(cwd, "session-1")
    store = _store(pipeline_dir)
    intent = store.reserve_successor(
        context_id="ctx-1",
        predecessor_task_id="task-old",
        invocation_id="request-1",
        checkpoint=checkpoint_identity(checkpoint_payload, event),
        cancellation_execution_id="execution-old",
        cancellation_revision=11,
        cancellation_backup_generation=7,
        cancellation_backup_commit_id="commit-7",
    )
    store.claim_execution(
        successor_task_id=intent.successor_task_id,
        invocation_id=intent.invocation_id,
        checkpoint=intent.checkpoint,
        expected_fence=intent.fence,
    )
    store.mark_unsafe(
        successor_task_id=intent.successor_task_id,
        checkpoint=intent.checkpoint,
        expected_fence=intent.fence,
        reason="active_attempt_outcome_unverified",
    )
    A2APipelineJournal(pipeline_dir).append(
        {
            "schemaVersion": "1.0",
            "eventId": "evt-successor-failed",
            "sequence": 2,
            "eventType": "pipeline_failed",
            "pipelineRunId": "ctx-1",
            "taskId": intent.successor_task_id,
            "contextId": "ctx-1",
            "status": "failed",
            "data": {},
        }
    )
    persistence = A2APersistenceStore(tmp_path / "a2a-state")
    persistence.save_context(A2AContextSnapshot(context_id="ctx-1", session_id="session-1", cwd=str(cwd)))
    task_store = A2ATaskStore(metrics=NoOpA2AMetrics(), persistence=persistence)
    await task_store.get_or_create_context(context_id="ctx-1", cwd=str(cwd), runtime_factory=lambda _sid: object())
    executor = object.__new__(IacCodeA2AExecutor)
    executor._task_store = task_store

    resolved = await executor.resolve_omitted_pipeline_task_id(
        context_id="ctx-1",
        cwd=str(cwd),
        invocation_id="request-2",
    )

    assert resolved == intent.successor_task_id


@pytest.mark.asyncio
async def test_reserved_successor_rejects_different_omitted_request_invocation(tmp_path) -> None:
    cwd = tmp_path / "workspace"
    pipeline_dir, event, checkpoint_payload = _canceled_sidecar(cwd, "session-1")
    store = _store(pipeline_dir)
    store.reserve_successor(
        context_id="ctx-1",
        predecessor_task_id="task-old",
        invocation_id="request-a",
        checkpoint=checkpoint_identity(checkpoint_payload, event),
        cancellation_execution_id="execution-old",
        cancellation_revision=11,
        cancellation_backup_generation=7,
        cancellation_backup_commit_id="commit-7",
    )
    persistence = A2APersistenceStore(tmp_path / "a2a-state")
    persistence.save_context(A2AContextSnapshot(context_id="ctx-1", session_id="session-1", cwd=str(cwd)))
    task_store = A2ATaskStore(metrics=NoOpA2AMetrics(), persistence=persistence)
    await task_store.get_or_create_context(context_id="ctx-1", cwd=str(cwd), runtime_factory=lambda _sid: object())
    executor = object.__new__(IacCodeA2AExecutor)
    executor._task_store = task_store

    with pytest.raises(PipelineContinuationConflictError, match="different invocation"):
        await executor.resolve_omitted_pipeline_task_id(
            context_id="ctx-1",
            cwd=str(cwd),
            invocation_id="request-b",
        )


@pytest.mark.asyncio
async def test_claimed_checkpoint_change_is_durably_unsafe_without_fresh_fallback(tmp_path) -> None:
    cwd = tmp_path / "workspace"
    pipeline_dir, event, checkpoint_payload = _canceled_sidecar(cwd, "session-1")
    store = _store(pipeline_dir)
    intent = store.reserve_successor(
        context_id="ctx-1",
        predecessor_task_id="task-old",
        invocation_id="request-1",
        checkpoint=checkpoint_identity(checkpoint_payload, event),
        cancellation_execution_id="execution-old",
        cancellation_revision=11,
        cancellation_backup_generation=7,
        cancellation_backup_commit_id="commit-7",
    )
    store.claim_execution(
        successor_task_id=intent.successor_task_id,
        invocation_id=intent.invocation_id,
        checkpoint=intent.checkpoint,
        expected_fence=intent.fence,
    )
    meta_path = SessionStorage().session_dir(str(cwd), "session-1") / "pipeline" / "meta.yaml"
    meta_path.write_text(yaml.safe_dump({"status": "canceled", "current_step": "changed"}), encoding="utf-8")
    pipeline = SimpleNamespace(sidecar_status="canceled")
    executor = object.__new__(IacCodeA2APipelineExecutor)

    with pytest.raises(PipelineContinuationUnsafeError, match="successor_checkpoint_fence_changed"):
        await executor._select_stream(
            pipeline,
            "retry",
            pipeline_input=SimpleNamespace(display_text="retry", content="retry", has_images=False),
            publisher=SimpleNamespace(
                journal=A2APipelineJournal(pipeline_dir),
                snapshot_store=A2APipelineSnapshotStore(pipeline_dir),
            ),
            task_id=intent.successor_task_id,
            context_id="ctx-1",
            fresh_pipeline_factory=lambda: (_ for _ in ()).throw(AssertionError("must not start fresh")),
        )

    assert store.load().phase == "unsafe"


def test_settled_successor_releases_next_predecessor_with_higher_fence(tmp_path) -> None:
    store = _store(tmp_path / "session" / "a2a" / "pipeline")
    first = _reserve(store)
    store.claim_execution(
        successor_task_id=first.successor_task_id,
        invocation_id=first.invocation_id,
        checkpoint=first.checkpoint,
        expected_fence=first.fence,
    )
    claimed = store.load()
    store.begin_execution(
        successor_task_id=first.successor_task_id,
        claim_id=claimed.claim_id,
        expected_fence=first.fence,
    )
    store.settle(successor_task_id=first.successor_task_id, expected_fence=first.fence)

    second = store.reserve_successor(
        context_id="ctx-1",
        predecessor_task_id="task-next-canceled",
        invocation_id="request-2",
        checkpoint=PipelineCheckpointIdentity(version=1, digest="b" * 64, sequence=8, event_id="evt-8"),
        cancellation_execution_id="execution-next",
        cancellation_revision=12,
        cancellation_backup_generation=8,
        cancellation_backup_commit_id="commit-8",
    )

    assert second.predecessor_task_id == "task-next-canceled"
    assert second.fence == first.fence + 1


@pytest.mark.asyncio
async def test_running_successor_reenters_at_matching_durable_waiting_input_boundary(tmp_path) -> None:
    cwd = tmp_path / "workspace"
    pipeline_dir, canceled_event, checkpoint_payload = _canceled_sidecar(cwd, "session-1")
    store = _store(pipeline_dir)
    intent = store.reserve_successor(
        context_id="ctx-1",
        predecessor_task_id="task-old",
        invocation_id="request-a",
        checkpoint=checkpoint_identity(checkpoint_payload, canceled_event),
        cancellation_execution_id="execution-old",
        cancellation_revision=11,
        cancellation_backup_generation=7,
        cancellation_backup_commit_id="commit-7",
    )
    claimed = store.claim_execution(
        successor_task_id=intent.successor_task_id,
        invocation_id=intent.invocation_id,
        checkpoint=intent.checkpoint,
        expected_fence=intent.fence,
    )
    store.begin_execution(
        successor_task_id=intent.successor_task_id,
        claim_id=claimed.claim_id,
        expected_fence=intent.fence,
    )
    waiting_event = {
        "schemaVersion": "1.0",
        "eventId": "evt-waiting",
        "sequence": 2,
        "eventType": "input_required",
        "pipelineRunId": "ctx-1",
        "taskId": intent.successor_task_id,
        "contextId": "ctx-1",
        "status": "waiting_input",
        "data": {},
    }
    journal = A2APipelineJournal(pipeline_dir)
    journal.append(waiting_event)
    A2APipelineSnapshotStore(pipeline_dir).save(reduce_pipeline_events([canceled_event, waiting_event]))

    class WaitingPipeline:
        sidecar_status = "waiting_input"
        sidecar_restore_failed = False

        async def resume(self, user_input):
            yield user_input

    executor = object.__new__(IacCodeA2APipelineExecutor)
    selected = await executor._select_stream(
        WaitingPipeline(),
        "answer",
        pipeline_input=SimpleNamespace(display_text="answer", content="answer", has_images=False),
        publisher=SimpleNamespace(journal=journal, snapshot_store=A2APipelineSnapshotStore(pipeline_dir)),
        task_id=intent.successor_task_id,
        context_id="ctx-1",
        fresh_pipeline_factory=lambda: (_ for _ in ()).throw(AssertionError("must not start fresh")),
    )

    assert [item async for item in selected.stream] == ["answer"]
    assert store.load().phase == "waiting_input"


@pytest.mark.asyncio
async def test_quiescent_canceled_owner_without_proof_fails_closed(tmp_path) -> None:
    cwd = tmp_path / "workspace"
    _canceled_sidecar(cwd, "session-1")
    persistence = A2APersistenceStore(tmp_path / "a2a-state")
    persistence.save_context(A2AContextSnapshot(context_id="ctx-1", session_id="session-1", cwd=str(cwd)))
    task_store = A2ATaskStore(metrics=NoOpA2AMetrics(), persistence=persistence)
    await task_store.get_or_create_context(context_id="ctx-1", cwd=str(cwd), runtime_factory=lambda _sid: object())
    owner = await task_store.get_or_create_task(task_id="task-old", context_id="ctx-1")
    owner.state = "canceled"
    owner.active_task = None
    task_store.mirror_task(owner)
    executor = object.__new__(IacCodeA2AExecutor)
    executor._task_store = task_store

    pipeline_dir = a2a_pipeline_dir_for_session(cwd=str(cwd), session_id="session-1")
    intent_path = pipeline_dir / "continuation-intent.json"
    assert not intent_path.exists()
    assert (await task_store.canceled_task_release_proof(context_id="ctx-1", task_id="task-old")) is None

    with pytest.raises(InvalidParamsError, match="cancellation release proof is unavailable"):
        await executor.resolve_omitted_pipeline_task_id(
            context_id="ctx-1",
            cwd=str(cwd),
            invocation_id="request-2",
        )

    assert not intent_path.exists()


@pytest.mark.asyncio
async def test_live_canceled_writer_without_proof_still_fails_finalizing(tmp_path) -> None:
    cwd = tmp_path / "workspace"
    _canceled_sidecar(cwd, "session-1")
    persistence = A2APersistenceStore(tmp_path / "a2a-state")
    persistence.save_context(A2AContextSnapshot(context_id="ctx-1", session_id="session-1", cwd=str(cwd)))
    task_store = A2ATaskStore(metrics=NoOpA2AMetrics(), persistence=persistence)
    await task_store.get_or_create_context(context_id="ctx-1", cwd=str(cwd), runtime_factory=lambda _sid: object())
    owner = await task_store.get_or_create_task(task_id="task-old", context_id="ctx-1")
    owner.state = "canceled"
    release = asyncio.Event()
    active_writer = asyncio.create_task(release.wait())
    owner.active_task = active_writer
    task_store.mirror_task(owner)
    executor = object.__new__(IacCodeA2AExecutor)
    executor._task_store = task_store

    pipeline_dir = a2a_pipeline_dir_for_session(cwd=str(cwd), session_id="session-1")
    assert not (pipeline_dir / "continuation-intent.json").exists()
    assert (await task_store.canceled_task_release_proof(context_id="ctx-1", task_id="task-old")) is None

    try:
        with pytest.raises(InvalidParamsError, match="still finalizing"):
            await executor.resolve_omitted_pipeline_task_id(
                context_id="ctx-1",
                cwd=str(cwd),
                invocation_id="request-2",
            )
    finally:
        release.set()
        await active_writer
