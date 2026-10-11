import asyncio
import hashlib
import json
import multiprocessing
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import httpx
import pytest
import yaml
from a2a.utils.errors import InvalidParamsError
from openai import AsyncOpenAI

from iac_code.a2a.execution_control import (
    ExecutionController,
    ExecutionControlService,
    bind_execution_control,
    reset_execution_control,
)
from iac_code.a2a.executor import IacCodeA2AExecutor
from iac_code.a2a.metrics import NoOpA2AMetrics
from iac_code.a2a.persistence import A2AContextSnapshot, A2APersistenceStore
from iac_code.a2a.pipeline_continuation import (
    PipelineCheckpointIdentity,
    PipelineContinuationConflictError,
    PipelineContinuationCorruptError,
    PipelineContinuationStore,
    PipelineContinuationUnsafeError,
    PipelineModelReleaseProof,
    checkpoint_identity,
)
from iac_code.a2a.pipeline_executor import IacCodeA2APipelineExecutor, successor_task_id_from_sidecar
from iac_code.a2a.pipeline_journal import A2APipelineJournal
from iac_code.a2a.pipeline_paths import a2a_pipeline_dir_for_session
from iac_code.a2a.pipeline_snapshot import A2APipelineSnapshotStore, reduce_pipeline_events
from iac_code.a2a.task_store import A2ATaskStore
from iac_code.agent.agent_loop import AgentLoop
from iac_code.agent.message import Message as AgentMessage
from iac_code.pipeline.engine.pipeline_runner import PipelineRunner
from iac_code.providers.manager import ProviderManager
from iac_code.providers.openai_provider import OpenAIProvider
from iac_code.providers.retry import RetryConfig
from iac_code.services.session_backup_staging import SessionBackupStagingWorker, StagedSessionBackupService
from iac_code.services.session_metadata import SESSION_LAYOUT_VERSION_V2, SessionMetadata, write_session_metadata
from iac_code.services.session_storage import SessionStorage
from iac_code.tools.base import ToolRegistry
from iac_code.types.stream_events import MessageEndEvent, ToolUseStartEvent, Usage

from .fakes import FakeEventQueue, FakeRequestContext


class _SnapshotClock:
    def __init__(self, monkeypatch):
        self.value = "2026-10-10T00:00:00Z"
        monkeypatch.setattr("iac_code.a2a.pipeline_snapshot._utc_now", self.now)

    def now(self):
        return self.value

    def advance(self):
        self.value = "2026-10-10T00:00:01Z"


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


@pytest.mark.parametrize("damage", [None, "revision-bool", "generation-string", "external-operation", "unknown-field"])
def test_model_release_proof_is_durable_strict_and_reservation_compares_full_value(tmp_path, damage):
    store = _store(tmp_path / "session" / "a2a" / "pipeline")
    proof = PipelineModelReleaseProof(
        context_id="ctx-1",
        task_id="task-old",
        execution_id="execution-old",
        revision=11,
        backup_generation=7,
        backup_commit_id="commit-7",
        backup_status="staged_committed",
        termination_reason="stream_terminal_cleanup",
    )
    args = dict(
        context_id="ctx-1",
        predecessor_task_id="task-old",
        invocation_id="request-1",
        checkpoint=_checkpoint(),
        cancellation_execution_id="execution-old",
        cancellation_revision=11,
        cancellation_backup_generation=7,
        cancellation_backup_commit_id="commit-7",
        model_release_proof=proof,
    )
    reserved = store.reserve_successor(**args)
    assert store.load().model_release_proof == proof
    assert store.reserve_successor(**args) == reserved
    with pytest.raises(PipelineContinuationConflictError, match="cancellation proof changed"):
        store.reserve_successor(
            **{**args, "model_release_proof": replace(proof, termination_reason="explicit_terminate")}
        )
    with pytest.raises(PipelineContinuationConflictError, match="cancellation proof changed"):
        store.reserve_successor(**{**args, "model_release_proof": None})
    if damage is not None:
        path = tmp_path / "session" / "a2a" / "pipeline" / "continuation-intent.json"
        raw = json.loads(path.read_text(encoding="utf-8"))
        frozen = raw["modelReleaseProof"]
        if damage == "revision-bool":
            raw["cancellationRevision"] = True
        elif damage == "generation-string":
            frozen["backupGeneration"] = "7"
        elif damage == "external-operation":
            frozen["modelBoundary"]["externalOperations"] = [{"outcome": "unknown"}]
        else:
            frozen["modelBoundary"]["unknown"] = True
        path.write_text(json.dumps(raw), encoding="utf-8")
        with pytest.raises(PipelineContinuationCorruptError):
            store.load()


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


def test_canceled_sidecar_reserves_same_successor_on_retry(tmp_path, monkeypatch) -> None:
    clock = _SnapshotClock(monkeypatch)
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
    clock.advance()
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
@pytest.mark.parametrize("change", ["current-step", "updated-at", "execution", "terminal-event"])
async def test_claimed_checkpoint_change_is_durably_unsafe_without_fresh_fallback(tmp_path, change) -> None:
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
    meta = checkpoint_payload["pipelineMeta"].copy()
    if change == "current-step":
        meta["current_step"] = "changed"
    elif change == "updated-at":
        meta["updated_at"] = "2026-10-10T00:00:01Z"
    elif change == "execution":
        meta["execution"] = {"kind": "normal", "active_attempt_id": "other-attempt"}
    else:
        A2APipelineJournal(pipeline_dir).append(
            {**event, "sequence": 2, "eventId": "evt-next-canceled", "data": {"reason": "changed"}}
        )
    meta_path.write_text(yaml.safe_dump(meta), encoding="utf-8")
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


@pytest.mark.parametrize("phase", ["reserved", "claimed", "unsafe"])
def test_legacy_generated_at_digest_never_resets_existing_intent(tmp_path, monkeypatch, phase):
    _SnapshotClock(monkeypatch)
    cwd = tmp_path / "workspace"
    pipeline_dir, event, payload = _canceled_sidecar(cwd, "session-1")
    legacy_bytes = json.dumps(
        {"snapshot": payload, "terminalEvent": event}, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    checkpoint = PipelineCheckpointIdentity(
        version=1,
        digest=hashlib.sha256(legacy_bytes).hexdigest(),
        sequence=event["sequence"],
        event_id=event["eventId"],
    )
    store = _store(pipeline_dir)
    intent = store.reserve_successor(
        context_id="ctx-1",
        predecessor_task_id="task-old",
        invocation_id="request-1",
        checkpoint=checkpoint,
        cancellation_execution_id="execution-old",
        cancellation_revision=11,
        cancellation_backup_generation=7,
        cancellation_backup_commit_id="commit-7",
    )
    if phase in ("claimed", "unsafe"):
        store.claim_execution(
            successor_task_id=intent.successor_task_id,
            invocation_id=intent.invocation_id,
            checkpoint=checkpoint,
            expected_fence=intent.fence,
        )
    if phase == "unsafe":
        store.mark_unsafe(
            successor_task_id=intent.successor_task_id,
            checkpoint=checkpoint,
            expected_fence=intent.fence,
            reason="prior-safety-failure",
        )
    saved = (pipeline_dir / "continuation-intent.json").read_bytes()
    with pytest.raises(PipelineContinuationConflictError, match="canceled checkpoint changed"):
        successor_task_id_from_sidecar(
            cwd=str(cwd),
            session_id="session-1",
            context_id="ctx-1",
            canceled_task_id="task-old",
            invocation_id="request-1",
            cancellation_proof={
                "executionId": "execution-old",
                "revision": 11,
                "backupGeneration": 7,
                "backupCommitId": "commit-7",
            },
        )
    assert (pipeline_dir / "continuation-intent.json").read_bytes() == saved
    assert store.load().phase == phase


@pytest.mark.parametrize("section", ["envelope", "pipelineMeta", "terminalEvent"])
def test_checkpoint_display_clock_exclusion_preserves_other_generated_at_fields(tmp_path, section):
    _, event, payload = _canceled_sidecar(tmp_path / "workspace", "session-1")
    original = json.loads(json.dumps(payload))
    identity = checkpoint_identity(payload, event)
    assert payload == original
    payload["a2a"]["generatedAt"] = "a different display time"
    assert checkpoint_identity(payload, event) == identity
    assert original["a2a"]["generatedAt"] != payload["a2a"]["generatedAt"]
    if section == "envelope":
        payload["generatedAt"] = "authoritative unknown extension"
    elif section == "pipelineMeta":
        payload["pipelineMeta"]["generatedAt"] = "authoritative unknown extension"
    else:
        event["generatedAt"] = "authoritative terminal extension"
    assert checkpoint_identity(payload, event) != identity


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
async def test_canceled_owner_handed_off_to_normal_does_not_demand_release_proof(tmp_path) -> None:
    """A canceled Pipeline that already handed off to normal is not a continuation.

    The selling pipeline completes with ``on_complete: switch_to_normal``, so after a
    cancel the durable snapshot carries a committed ``normalHandoff``.  The next turn
    is ordinary normal chat: it must not be routed through the canceled owner and
    rejected for a release proof that a naturally finalized owner never publishes.
    """

    cwd = tmp_path / "workspace"
    pipeline_dir, canceled_event, _ = _canceled_sidecar(cwd, "session-1")
    handoff_event = {
        "schemaVersion": "1.0",
        "eventId": "evt-handoff",
        "sequence": 2,
        "eventType": "pipeline_handoff_ready",
        "pipelineRunId": "ctx-1",
        "taskId": "task-old",
        "contextId": "ctx-1",
        "scope": "pipeline",
        "status": "canceled",
        "data": {
            "action": "switch_to_normal",
            "targetMode": "normal",
            "outcome": "canceled",
            "summary": "[Pipeline Handoff Context]\nOutcome: canceled",
        },
    }
    journal = A2APipelineJournal(pipeline_dir)
    journal.append(handoff_event)
    A2APipelineSnapshotStore(pipeline_dir).save(reduce_pipeline_events([canceled_event, handoff_event]))

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

    snapshot = A2APipelineSnapshotStore(pipeline_dir).load()
    assert snapshot is not None
    assert snapshot["normalHandoff"]["action"] == "switch_to_normal"
    assert (await task_store.canceled_task_release_proof(context_id="ctx-1", task_id="task-old")) is None

    resolved = await executor.resolve_omitted_pipeline_task_id(
        context_id="ctx-1",
        cwd=str(cwd),
        invocation_id="request-2",
    )

    assert resolved is None
    assert not (pipeline_dir / "continuation-intent.json").exists()


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


@pytest.mark.asyncio
async def test_stream_cleanup_stop_reentry_reserves_one_successor_with_latest_staged_backup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cwd = tmp_path / "workspace"
    cwd.mkdir()
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("IAC_CODE_CONFIG_BACKUP_DIR", str(tmp_path / "shared"))
    pipeline_dir, event, checkpoint_payload = _canceled_sidecar(cwd, "session-1")
    storage = SessionStorage()
    session_dir = storage.session_dir(str(cwd), "session-1")
    write_session_metadata(
        session_dir,
        SessionMetadata(session_id="session-1", cwd=str(cwd), layout_version=SESSION_LAYOUT_VERSION_V2),
    )
    latest_payload = session_dir / "latest.txt"
    latest_payload.write_text("latest-before-stop", encoding="utf-8")
    service = StagedSessionBackupService(tmp_path / "staging", storage, retry_delays=())
    service.initialize_session(str(cwd), "session-1")
    persistence = A2APersistenceStore(tmp_path / "a2a")
    persistence.save_context(A2AContextSnapshot(context_id="ctx-1", session_id="session-1", cwd=str(cwd)))
    store = A2ATaskStore(metrics=NoOpA2AMetrics(), persistence=persistence)
    await store.get_or_create_context(context_id="ctx-1", cwd=str(cwd), runtime_factory=lambda _sid: object())
    owner = await store.get_or_create_task(task_id="task-old", context_id="ctx-1")
    owner.state = "canceled"
    store.mirror_task(owner)
    control = ExecutionController(
        context_id="ctx-1",
        task_id="task-old",
        owner="owner-1",
        cwd=str(cwd),
        server_instance_id="instance-1",
        execution_id="exec-old",
        persistence_path=persistence.root / "execution-control" / "ctx-1.json",
        backup_service=service,
    )
    control.bind_session("session-1")
    try:
        await control.terminate(
            execution_id="exec-old",
            request_id="stop-1",
            connection_epoch=1,
            reason="stream_terminal_cleanup",
        )
        for _ in range(500):
            if control.release_ready:
                break
            await asyncio.sleep(0.01)
        assert control.release_ready
        executor = object.__new__(IacCodeA2AExecutor)
        executor._task_store = store
        resolved = await executor.resolve_omitted_pipeline_task_id(
            context_id="ctx-1",
            cwd=str(cwd),
            invocation_id="reentry-1",
        )
        assert resolved is not None and resolved != "task-old"
        assert (
            await executor.resolve_omitted_pipeline_task_id(
                context_id="ctx-1",
                cwd=str(cwd),
                invocation_id="reentry-1",
            )
            == resolved
        )
        with pytest.raises(PipelineContinuationConflictError):
            await executor.resolve_omitted_pipeline_task_id(
                context_id="ctx-1",
                cwd=str(cwd),
                invocation_id="different-reentry",
            )
        intent = _store(pipeline_dir).load()
        assert intent is not None
        assert intent.successor_task_id == resolved
        assert intent.checkpoint == checkpoint_identity(checkpoint_payload, event)
        assert intent.cancellation_execution_id == "exec-old"
        assert intent.cancellation_revision == control.persisted_revision
        assert intent.cancellation_backup_generation == 1
        assert intent.cancellation_backup_commit_id == control.backup["commitId"]
        worker = SessionBackupStagingWorker(tmp_path / "staging", tmp_path / "shared")
        (snapshot,) = worker.scan_snapshots()
        assert (snapshot.path / "latest.txt").read_text(encoding="utf-8") == "latest-before-stop"
        latest_payload.write_text("later-live-must-not-replace-stop-capture", encoding="utf-8")
        assert worker.run_once() == 1
        shared_session = tmp_path / "shared" / "projects" / session_dir.parent.name / "session-1"
        assert (shared_session / "latest.txt").read_text(encoding="utf-8") == "latest-before-stop"
        assert worker.scan_snapshots() == []
    finally:
        await control.close()


_MODEL_INITIAL_REQUEST = (
    "项目标记 pipeline-preserved-旧需求。VPC 10.246.0.0/16，VSwitch 10.246.1.0/24 和 10.246.2.0/24。"
)
_MODEL_REENTRY_REQUEST = "继续刚才被中断的方案，复述原标记和 CIDR，完成候选方案供我选择。"
_MODEL_FOLLOWUP_REQUEST = "请继续核对方案中的网络范围。"


class _WaitingFirstModel:
    """Stop at the real AgentLoop's first model request, before any tool result."""

    def __init__(self, *, fault=None) -> None:
        self.entered = asyncio.Event()
        self.calls = 0
        self.fault = fault
        self.requests = []
        self.before_judge_response = None

    async def stream(self, **_kwargs):
        self.calls += 1
        self.entered.set()
        await asyncio.Event().wait()
        yield MessageEndEvent(stop_reason="stop", usage=Usage())

    async def handle(self, _request):
        self.requests.append(json.loads(_request.content))
        self.calls += 1
        if self.fault in ("request-retry", "fallback-complete", "sdk-retry") and self.calls == 1:
            return httpx.Response(500, json={"error": {"message": "offline inference failure", "type": "server_error"}})
        if self.fault == "fallback-complete":
            self.entered.set()
            await asyncio.Event().wait()
        if not json.loads(_request.content).get("stream", False):
            if self.before_judge_response is not None:
                self.before_judge_response()
            verdict = {
                "action": "supplement" if self.fault == "supplement" else "continue",
                "reason": "continue planning",
            }
            if self.fault in ("hard-interrupt-current", "hard-interrupt-earlier"):
                verdict.update(
                    action="hard_interrupt",
                    rollback_target=(
                        "prepare_plan" if self.fault == "hard-interrupt-earlier" else "solution_planning_and_selection"
                    ),
                    rollback_context=_MODEL_REENTRY_REQUEST,
                )
            return httpx.Response(
                200,
                json={
                    "id": "judge-1",
                    "object": "chat.completion",
                    "model": "gpt-4o",
                    "choices": [
                        {
                            "index": 0,
                            "message": {
                                "role": "assistant",
                                "content": json.dumps(verdict),
                            },
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                },
            )
        if self.fault == "hard-interrupt-earlier" and self.calls == 1:
            return httpx.Response(
                200, headers={"content-type": "text/event-stream"}, stream=_CompletedPreparationModelBody()
            )
        if self.fault == "partial-tool":
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=_PartialToolModelBody())
        self.entered.set()
        await asyncio.Event().wait()
        return httpx.Response(200)


class _CompletedPreparationModelBody(httpx.AsyncByteStream):
    """Complete the real earlier step, then stop the next step's first model call."""

    async def __aiter__(self):
        chunk = {
            "id": "prepare-1",
            "object": "chat.completion.chunk",
            "model": "gpt-4o",
            "choices": [
                {
                    "index": 0,
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "prepare-complete",
                                "type": "function",
                                "function": {
                                    "name": "complete_step",
                                    "arguments": json.dumps({"conclusion": {"prepared": "completed"}}),
                                },
                            }
                        ]
                    },
                    "finish_reason": None,
                }
            ],
        }
        yield f"data: {json.dumps(chunk)}\n\n".encode()
        chunk["choices"][0]["delta"] = {}
        chunk["choices"][0]["finish_reason"] = "tool_calls"
        yield f"data: {json.dumps(chunk)}\n\n".encode()
        yield b"data: [DONE]\n\n"


class _PartialToolModelBody(httpx.AsyncByteStream):
    async def __aiter__(self):
        chunk = {
            "id": "msg-1",
            "object": "chat.completion.chunk",
            "model": "gpt-4o",
            "choices": [
                {
                    "index": 0,
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "tool-1",
                                "type": "function",
                                "function": {"name": "bash", "arguments": ""},
                            }
                        ]
                    },
                    "finish_reason": None,
                },
            ],
        }
        yield f"data: {json.dumps(chunk)}\n\n".encode()
        chunk["choices"][0]["delta"] = {"tool_calls": [{"index": 0, "function": {"arguments": "{"}}]}
        yield f"data: {json.dumps(chunk)}\n\n".encode()
        await asyncio.Event().wait()


class _CustomModelManager(ProviderManager):
    pass


class _CustomModelAdapter(OpenAIProvider):
    pass


class _ActiveModelStopCase:
    def __init__(self, runner, control, store, owner, pipeline_dir, model) -> None:
        self.runner = runner
        self.control = control
        self.store = store
        self.owner = owner
        self.pipeline_dir = pipeline_dir
        self.model = model
        self.successor_runner = None
        self.successor_clients = []

    async def consume(self) -> None:
        token = bind_execution_control(self.control)
        current = asyncio.current_task()
        assert current is not None
        await self.control.attach_task(current)
        try:
            async for _event in self.runner.run(_MODEL_INITIAL_REQUEST):
                if isinstance(_event, ToolUseStartEvent) and self.model.fault == "partial-tool":
                    self.model.entered.set()
        except asyncio.CancelledError:
            # The production executor's execution-control cancellation path
            # persists the runner checkpoint and then pipeline_canceled.
            self.runner.mark_execution_terminated("stream_terminal_cleanup")
            event = {
                "schemaVersion": "1.0",
                "eventId": "evt-canceled",
                "sequence": 1,
                "eventType": "pipeline_canceled",
                "pipelineRunId": "ctx-1",
                "taskId": "task-old",
                "contextId": "ctx-1",
                "status": "canceled",
                "data": {"source": "execution_control", "reason": "stream_terminal_cleanup"},
            }
            A2APipelineJournal(self.pipeline_dir).append(event)
            A2APipelineSnapshotStore(self.pipeline_dir).save(reduce_pipeline_events([event]))
            self.owner.state = "canceled"
            self.store.mirror_task(self.owner)
            raise
        finally:
            await self.control.detach_task(current, execution_status="canceled")
            reset_execution_control(token)

    async def cleanup(self, context_id, task_id, _reason):
        if await self.store.commit_inactive_execution_task(task_id=task_id, context_id=context_id, cancel_wait=True):
            return "canceled"
        return None

    async def consume_successor(self, stream):
        async for _event in stream:
            pass

    @staticmethod
    async def consume_model_loop(loop, manager):
        lease = manager.begin_request("Plan a solution", [])
        async for _event in loop._stream_provider(
            request_manager=manager,
            lease=lease,
            messages=[AgentMessage(role="user", content="Plan a solution")],
            system=lease.system_prompt,
            tools=[],
        ):
            pass

    def create_successor(self, _executor, **_kwargs):
        process_context = multiprocessing.get_context("spawn")
        queue = process_context.Queue()
        receiver = process_context.Process(
            target=self.inspect_recovered_worker,
            args=(str(self.runner._pipeline_dir), self.runner._cwd, str(self.store._persistence.root), queue),
        )
        receiver.start()
        receiver.join(10)
        assert receiver.exitcode == 0
        assert queue.get(timeout=2) == (True, "released_first_model_request")
        queue.close()
        queue.join_thread()
        provider = ProviderManager(
            "gpt-4o",
            {"openai": "offline-test"},
            provider_key_override="openai",
            ignore_llm_source=True,
            provider_config_override={},
            retry_config=RetryConfig(max_retries=0),
        )
        self.successor_clients.append(provider._provider._client)
        sdk = AsyncOpenAI(
            api_key="offline-test",
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(self.model.handle)),
            max_retries=0,
        )
        self.successor_clients.append(sdk)
        provider._provider._client = sdk
        self.successor_runner = PipelineRunner(
            self.runner._pipeline_dir,
            provider,
            ToolRegistry(),
            SessionStorage(),
            "session-1",
            cwd=self.runner._cwd,
            resume_from_sidecar=True,
            backup_service=self.runner._backup_service,
            surface="a2a",
        )
        assert self.successor_runner is not self.runner
        assert (
            self.successor_runner._step_executor._provider_manager is not self.runner._step_executor._provider_manager
        )
        return self.successor_runner

    @staticmethod
    def inspect_current_worker(config, cwd, queue):
        asyncio.run(_ActiveModelStopCase._inspect_current_worker(config, cwd, queue))

    @staticmethod
    async def _inspect_current_worker(config, cwd, queue):
        runner = PipelineRunner(
            Path(config), MagicMock(), ToolRegistry(), SessionStorage(), "session-1", cwd=cwd, resume_from_sidecar=True
        )
        contexts = runner.get_prompt_contexts()
        assert len(contexts) == 1
        restored_messages = [(message.role, message.content) for message in contexts[0].messages]
        assert runner._model_resume_seed() is None
        assert [message.content for message in runner.get_prompt_contexts()[0].messages] == [
            content for _role, content in restored_messages
        ]
        model = _WaitingFirstModel()
        provider = ProviderManager(
            "gpt-4o",
            {"openai": "offline-test"},
            provider_key_override="openai",
            ignore_llm_source=True,
            provider_config_override={},
            retry_config=RetryConfig(max_retries=0),
        )
        await provider._provider._client.close()
        sdk = AsyncOpenAI(
            api_key="offline-test",
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(model.handle)),
            max_retries=0,
        )
        provider._provider._client = sdk
        # Exercise the real next AgentLoop model context without starting a
        # competing pipeline owner or writing the live parent's transcript.
        loop = AgentLoop(
            provider, contexts[0].system_prompt, ToolRegistry(), resume_messages=contexts[0].messages, cwd=cwd
        )

        async def consume():
            async for _event in loop.run_streaming(_MODEL_FOLLOWUP_REQUEST):
                pass

        active = asyncio.create_task(consume())
        try:
            await asyncio.wait_for(model.entered.wait(), timeout=5)
            queue.put(
                (
                    restored_messages,
                    [(row["role"], row["content"]) for row in model.requests[-1]["messages"] if row["role"] == "user"],
                )
            )
        finally:
            active.cancel()
            await asyncio.gather(active, return_exceptions=True)
            await sdk.close()

    @staticmethod
    def inspect_recovered_worker(config, cwd, persistence_root, queue):
        asyncio.run(_ActiveModelStopCase._inspect_recovered_worker(config, cwd, persistence_root, queue))

    @staticmethod
    async def _inspect_recovered_worker(config, cwd, persistence_root, queue):
        # The receiver has no old runner or process-local model flags. Its new
        # provider is deliberately custom; only the durable old evidence counts.
        runner = PipelineRunner(
            Path(config), MagicMock(), ToolRegistry(), SessionStorage(), "session-1", cwd=cwd, resume_from_sidecar=True
        )
        restored = runner.restore_canceled_checkpoint_sync()
        assert restored.ok and not runner.canceled_checkpoint_safety().safe
        store = A2ATaskStore(metrics=NoOpA2AMetrics(), persistence=A2APersistenceStore(Path(persistence_root)))
        await store.get_or_create_context(context_id="ctx-1", cwd=cwd, runtime_factory=lambda _sid: object())
        # A new owner already replaced execution-control. The receiver must use
        # the immutable intent proof, never today's control file or provider.
        assert await store.canceled_task_release_proof(context_id="ctx-1", task_id="task-old") is None
        intent = _store(a2a_pipeline_dir_for_session(cwd=cwd, session_id="session-1")).load()
        safety = runner.canceled_model_checkpoint_safety(intent, intent.model_release_proof.to_dict())
        queue.put((safety.safe, safety.reason))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "standard_provider,fault",
    [
        (False, None),
        (True, None),
        (True, "checkpoint-clock"),
        (True, "sdk-retry"),
        (True, "supplement"),
        (True, "seed-file-change"),
        (True, "seed-identity-change"),
        (True, "hard-interrupt-current"),
        (True, "hard-interrupt-earlier"),
        *[
            (True, fault)
            for fault in (
                "on-enter",
                "on-exit",
                "manager-subclass",
                "adapter-subclass",
                "partial-tool",
                "request-retry",
                "fallback-complete",
                "missing-transcript",
                "corrupt-transcript",
                "partial-transcript",
                "tool-transcript",
                "wrong-transcript-id",
                "child-attempt",
                "parallel-execution",
                "external-operation",
                "release-not-ready",
                "commit-error",
                "revision-changed",
                "generation-boolean",
                "commit-changed",
                "shared-missing-identity",
                "legacy-intent",
                "obsolete-backup-marker",
            )
        ],
    ],
)
async def test_first_model_stop_reentry_continues_real_canceled_runner(
    tmp_path, monkeypatch, standard_provider, fault
) -> None:
    """Reproduce PRE's resumed -> failed successor after a fully proved Stop."""
    clock = _SnapshotClock(monkeypatch) if fault == "checkpoint-clock" else None
    cwd = tmp_path / "workspace"
    cwd.mkdir()
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("IAC_CODE_CONFIG_BACKUP_DIR", str(tmp_path / "shared"))
    config = tmp_path / "pipeline-config"
    config.mkdir()
    (config / "model.md").write_text("Plan the solution. Do not deploy resources.", encoding="utf-8")
    steps = [
        {
            "id": "solution_planning_and_selection",
            "conclusion_field": "plan",
            "forward": None,
            "prompt": "model.md",
        }
    ]
    dependencies = {"plan": []}
    if fault == "hard-interrupt-earlier":
        steps.insert(
            0,
            {
                "id": "prepare_plan",
                "conclusion_field": "preparation",
                "forward": "solution_planning_and_selection",
                "prompt": "model.md",
            },
        )
        dependencies = {"preparation": [], "plan": ["preparation"]}
    (config / "pipeline.yaml").write_text(
        yaml.safe_dump(
            {
                "name": "model-stop",
                "context_dependencies": dependencies,
                "steps": steps,
            }
        ),
        encoding="utf-8",
    )
    storage = SessionStorage()
    storage.ensure_v2_session_dir_for_new_session(str(cwd), "session-1")
    service = StagedSessionBackupService(tmp_path / "staging", storage, retry_delays=())
    service.initialize_session(str(cwd), "session-1")
    model = _WaitingFirstModel(fault=fault)
    provider = MagicMock()
    provider.get_model_name.return_value = "test-model"
    provider.stream = model.stream
    http_client = httpx.AsyncClient(transport=httpx.MockTransport(model.handle))
    sdk = AsyncOpenAI(api_key="offline-test", http_client=http_client, max_retries=1 if fault == "sdk-retry" else 0)
    if standard_provider:
        manager_type = _CustomModelManager if fault == "manager-subclass" else ProviderManager
        provider = manager_type(
            "gpt-4o",
            {"openai": "offline-test"},
            provider_key_override="openai",
            ignore_llm_source=True,
            provider_config_override={},
            retry_config=RetryConfig(
                max_retries=1 if fault == "request-retry" else 0, base_delay=0.01, jitter_factor=0
            ),
        )
        await provider._provider._client.close()
        provider._provider._client = sdk
        if fault == "adapter-subclass":
            provider._provider.__class__ = _CustomModelAdapter
    runner = PipelineRunner(
        config, provider, ToolRegistry(), storage, "session-1", cwd=str(cwd), backup_service=service, surface="a2a"
    )
    if fault in ("on-enter", "on-exit"):
        setattr(runner.state_machine.current_step, fault.replace("-", "_"), lambda *_args: None)
    persistence = A2APersistenceStore(tmp_path / "a2a")
    persistence.save_context(A2AContextSnapshot(context_id="ctx-1", session_id="session-1", cwd=str(cwd)))
    store = A2ATaskStore(metrics=NoOpA2AMetrics(), persistence=persistence)
    context = await store.get_or_create_context(context_id="ctx-1", cwd=str(cwd), runtime_factory=lambda _sid: object())
    owner = await store.get_or_create_task(task_id="task-old", context_id="ctx-1")
    pipeline_dir = a2a_pipeline_dir_for_session(cwd=str(cwd), session_id="session-1")
    case = _ActiveModelStopCase(runner, None, store, owner, pipeline_dir, model)
    control = ExecutionController(
        context_id="ctx-1",
        task_id="task-old",
        owner="owner-1",
        cwd=str(cwd),
        server_instance_id="instance-1",
        execution_id="exec-old",
        execution_mode="pipeline",
        persistence_path=persistence.root / "execution-control" / "ctx-1.json",
        backup_service=service,
        termination_cleanup=case.cleanup,
    )
    control.bind_session("session-1")
    case.control = control
    active = asyncio.create_task(case.consume())
    owner.active_task = active
    context.active_task_id = owner.task_id
    resumed = None
    control_service = None
    try:
        await asyncio.wait_for(model.entered.wait(), timeout=3)
        old_attempt_id = "att_0002" if fault == "hard-interrupt-earlier" else "att_0001"
        assert runner._execution["active_attempt_id"] == old_attempt_id
        old_transcript_id = f"transcript_{old_attempt_id}"
        transcript = runner._transcript_storage.session_path(str(cwd), old_transcript_id)
        initial_requests = model.calls
        stopped_model_users = (
            [row["content"] for row in model.requests[-1]["messages"] if row["role"] == "user"]
            if standard_provider
            else None
        )
        if fault == "hard-interrupt-earlier":
            assert runner._attempts["items"]["att_0001"]["status"] == "completed"
            assert runner.state_machine.current_step.step_id == "solution_planning_and_selection"
        if fault == "missing-transcript":
            transcript.unlink()
        elif fault == "corrupt-transcript":
            transcript.write_text("{bad}\n", encoding="utf-8")
        elif fault == "partial-transcript":
            transcript.write_text(transcript.read_text(encoding="utf-8").rstrip("\n"), encoding="utf-8")
        elif fault == "tool-transcript":
            with transcript.open("a") as stream:
                stream.write(
                    json.dumps(
                        {
                            "role": "assistant",
                            "content": [{"type": "tool_use", "id": "t1", "name": "bash", "input": {}}],
                        }
                    )
                    + "\n"
                )
        elif fault == "wrong-transcript-id":
            row = json.loads(transcript.read_text(encoding="utf-8"))
            row["session_id"] = "other-transcript"
            transcript.write_text(json.dumps(row) + "\n", encoding="utf-8")
        elif fault == "child-attempt":
            runner._attempts["items"]["att_0002"] = {"attempt_id": "att_0002", "scope": "sub_step", "status": "failed"}
        elif fault == "parallel-execution":
            runner._execution["kind"] = "parallel_sub_pipeline"
        elif fault == "external-operation":
            await control.record_external_operation(product="ros", action="CreateStack", outcome="unknown")
        await control.terminate(
            execution_id="exec-old", request_id="stop-1", connection_epoch=1, reason="stream_terminal_cleanup"
        )
        for _ in range(500):
            if control.release_ready:
                break
            await asyncio.sleep(0.01)
        assert control.release_ready and active.cancelled()
        proof = await store.canceled_task_release_proof(context_id="ctx-1", task_id="task-old")
        assert proof is not None and control.backup["status"] == "staged_committed"
        assert proof["backupCommitId"] == control.backup["commitId"]
        assert bool(control.snapshot()["externalOperations"]) == (fault == "external-operation")
        restored = runner.restore_canceled_checkpoint_sync()
        assert restored.ok and restored.status == "canceled"
        assert restored.execution["active_attempt_id"] == old_attempt_id
        assert restored.attempts["items"][old_attempt_id]["status"] == "failed"
        snapshots = SessionBackupStagingWorker(tmp_path / "staging", tmp_path / "shared").scan_snapshots()
        capture = next(snapshot for snapshot in snapshots if snapshot.generation == proof["backupGeneration"])
        captured_state = service._read_state(capture.path, session_id="session-1")
        assert captured_state.commit_id == proof["backupCommitId"]
        captured_meta = yaml.safe_load((capture.path / "pipeline" / "meta.yaml").read_text(encoding="utf-8"))
        assert captured_meta["status"] == "canceled"
        assert captured_meta["attempts"] == restored.attempts
        if fault in ("hard-interrupt-current", "hard-interrupt-earlier"):
            session_dir = storage.v2_session_dir(str(cwd), "session-1")
            assert session_dir is not None
            captured_transcript = capture.path / transcript.relative_to(session_dir)
            old_transcript_bytes = transcript.read_bytes()
            assert captured_transcript.read_bytes() == old_transcript_bytes
        executor = object.__new__(IacCodeA2AExecutor)
        executor._task_store = store
        successor = await executor.resolve_omitted_pipeline_task_id(
            context_id="ctx-1", cwd=str(cwd), invocation_id="reentry-1"
        )
        intent = _store(pipeline_dir).load()
        assert intent is not None and successor == intent.successor_task_id
        if clock is not None:
            clock.advance()
        positive = standard_provider and fault in (
            None,
            "checkpoint-clock",
            "sdk-retry",
            "supplement",
            "seed-file-change",
            "hard-interrupt-current",
            "hard-interrupt-earlier",
        )
        if positive or fault == "seed-identity-change":
            if fault == "seed-file-change":
                model.before_judge_response = lambda: runner._transcript_storage.save(
                    str(cwd), "transcript_att_0001", [AgentMessage(role="user", content="unverified replacement")]
                )
            elif fault == "seed-identity-change":
                model.before_judge_response = lambda: case.successor_runner._execution.update(
                    transcript_id="transcript-other"
                )
            # Preserve the complete public ordering: task allocation -> new
            # ExecutionControlService owner -> PipelineExecutor -> real runner.
            context.runtime = SimpleNamespace(provider_manager=MagicMock(), tool_registry=ToolRegistry())
            # Import/reload tests may replace the class bound by executor.py;
            # patch its current factory rather than our collected class alias.
            monkeypatch.setattr(
                "iac_code.a2a.executor.IacCodeA2APipelineExecutor._create_pipeline",
                lambda executor, **kwargs: case.create_successor(executor, **kwargs),
            )
            control_service = ExecutionControlService(persistence_root=persistence.root, backup_service=service)
            public_executor = IacCodeA2AExecutor(
                task_store=store, model="gpt-4o", backup_service=service, execution_control_service=control_service
            )
            request = FakeRequestContext(
                task_id=successor,
                context_id="ctx-1",
                text=_MODEL_REENTRY_REQUEST,
                metadata={"iac_code": {"cwd": str(cwd), "run_mode": "pipeline"}},
            )
            request.message.message_id = "reentry-1"
            model.entered.clear()
            event_queue = FakeEventQueue()
            resumed = asyncio.create_task(public_executor.execute(request, event_queue))
            if fault == "seed-identity-change":
                await asyncio.wait_for(resumed, timeout=10)
                runner = case.successor_runner
                assert model.calls == 2
                assert runner._execution["active_attempt_id"] == "att_0001"
                assert runner._canceled_model_resume_seed is None
                assert any("Canceled model resume checkpoint changed" in str(event) for event in event_queue.events)
                return
            model_started = asyncio.create_task(model.entered.wait())
            try:
                done, _pending = await asyncio.wait(
                    (resumed, model_started), timeout=10, return_when=asyncio.FIRST_COMPLETED
                )
                assert model_started in done, (
                    _store(pipeline_dir).load().phase,
                    _store(pipeline_dir).load().unsafe_reason,
                    event_queue.events,
                )
            finally:
                model_started.cancel()
                await asyncio.gather(model_started, return_exceptions=True)
            new_control = control_service.get_for_context("ctx-1")
            runner = case.successor_runner
            assert new_control.task_id == successor and new_control.execution_id != "exec-old"
            assert await store.canceled_task_release_proof(context_id="ctx-1", task_id="task-old") is None
            assert model.calls == initial_requests + 2
            judge_requests = [payload for payload in model.requests if not payload.get("stream")]
            business_requests = [payload for payload in model.requests if payload.get("stream")]
            assert len(judge_requests) == 1
            judge_users = [row["content"] for row in judge_requests[0]["messages"] if row["role"] == "user"]
            assert judge_users[:-1] == stopped_model_users
            assert _MODEL_REENTRY_REQUEST in judge_users[-1]
            resumed_users = [row["content"] for row in business_requests[-1]["messages"] if row["role"] == "user"]
            hard_interrupt = fault in ("hard-interrupt-current", "hard-interrupt-earlier")
            expected_users = (
                [_MODEL_REENTRY_REQUEST] if hard_interrupt else [_MODEL_INITIAL_REQUEST, _MODEL_REENTRY_REQUEST]
            )
            assert resumed_users == expected_users
            assert runner._attempts["items"][old_attempt_id]["status"] == ("discarded" if hard_interrupt else "failed")
            if hard_interrupt:
                assert transcript.read_bytes() == old_transcript_bytes
                assert captured_transcript.read_bytes() == old_transcript_bytes
                assert captured_meta["attempts"]["items"][old_attempt_id]["status"] == "failed"
                assert runner.state_machine.current_step.step_id == (
                    "prepare_plan" if fault == "hard-interrupt-earlier" else "solution_planning_and_selection"
                )
            assert _store(pipeline_dir).load().claim_id is not None
            current_attempt_id = "att_0003" if fault == "hard-interrupt-earlier" else "att_0002"
            current_messages = runner._transcript_storage.load(str(cwd), f"transcript_{current_attempt_id}")
            assert [message.content for message in current_messages] == resumed_users
            assert runner._model_resume_seed() is None
            process_context = multiprocessing.get_context("spawn")
            queue = process_context.Queue()
            receiver = process_context.Process(
                target=_ActiveModelStopCase.inspect_current_worker,
                args=(str(config), str(cwd), queue),
            )
            receiver.start()
            await asyncio.to_thread(receiver.join, 10)
            assert receiver.exitcode == 0
            assert queue.get(timeout=2) == (
                [("user", content) for content in expected_users],
                [("user", content) for content in [*expected_users, _MODEL_FOLLOWUP_REQUEST]],
            )
            queue.close()
            queue.join_thread()
            assert runner._execution["active_attempt_id"] == current_attempt_id
            assert runner.sidecar_status == "running" and _store(pipeline_dir).load().phase == "running"
            return
        _store(pipeline_dir).claim_execution(
            successor_task_id=successor,
            invocation_id="reentry-1",
            checkpoint=intent.checkpoint,
            expected_fence=intent.fence,
        )
        if fault in (
            "release-not-ready",
            "commit-error",
            "revision-changed",
            "generation-boolean",
            "commit-changed",
            "shared-missing-identity",
        ):
            path = pipeline_dir / "continuation-intent.json"
            saved = json.loads(path.read_text(encoding="utf-8"))
            boundary = saved["modelReleaseProof"]["modelBoundary"]
            if fault == "release-not-ready":
                boundary["releaseReady"] = False
            elif fault == "commit-error":
                boundary["commitError"] = "unproved checkpoint"
            elif fault == "revision-changed":
                saved["cancellationRevision"] += 1
            elif fault == "generation-boolean":
                saved["modelReleaseProof"]["backupGeneration"] = True
            elif fault == "commit-changed":
                saved["modelReleaseProof"]["backupCommitId"] = "different-commit"
            else:
                boundary["backupStatus"] = "shared_committed"
                del saved["modelReleaseProof"]["backupGeneration"]
            path.write_text(json.dumps(saved), encoding="utf-8")
            with pytest.raises(PipelineContinuationCorruptError):
                _store(pipeline_dir).load()
            return
        if fault == "legacy-intent":
            path = pipeline_dir / "continuation-intent.json"
            saved = json.loads(path.read_text(encoding="utf-8"))
            del saved["modelReleaseProof"]
            path.write_text(json.dumps(saved), encoding="utf-8")
        elif fault == "obsolete-backup-marker":
            path = storage.session_dir(str(cwd), "session-1") / ".backup-state.json"
            saved = json.loads(path.read_text(encoding="utf-8"))
            saved.update(generation=2, parent_generation=1, commit_id="newer-commit")
            path.write_text(json.dumps(saved), encoding="utf-8")
        print(
            json.dumps(
                {
                    "releaseReady": True,
                    "backup": control.backup,
                    "execution": restored.execution,
                    "attempts": restored.attempts,
                    "externalOperations": control.snapshot()["externalOperations"],
                },
                sort_keys=True,
            )
        )
        selector = object.__new__(IacCodeA2APipelineExecutor)
        selector._task_store = store
        selection = selector._select_stream(
            runner,
            "continue planning",
            pipeline_input=SimpleNamespace(
                display_text="continue planning", content="continue planning", has_images=False
            ),
            publisher=SimpleNamespace(
                journal=A2APipelineJournal(pipeline_dir), snapshot_store=A2APipelineSnapshotStore(pipeline_dir)
            ),
            task_id=successor,
            context_id="ctx-1",
            fresh_pipeline_factory=lambda: None,
        )
        with pytest.raises(PipelineContinuationUnsafeError, match="active_attempt_outcome_unverified"):
            await selection
    finally:
        if not active.done():
            active.cancel()
        await asyncio.gather(active, return_exceptions=True)
        if resumed is not None:
            resumed.cancel()
            await asyncio.gather(resumed, return_exceptions=True)
        await control.close()
        if control_service is not None:
            await control_service.close()
        for client in case.successor_clients:
            await client.close()
        await sdk.close()


class _FalseyModelExtension:
    def __init__(self) -> None:
        self.calls = 0

    def __bool__(self) -> bool:
        return False

    def __call__(self):
        self.calls += 1
        return "refreshed prompt"

    def get_stats_snapshot(self):
        return {}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "extension_name", [None, "system_prompt_refresher", "background_task_starter", "memory_recall_service"]
)
async def test_model_only_provenance_rejects_unknown_falsey_extensions_and_stale_requests(
    tmp_path, monkeypatch, extension_name
):
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    model = _WaitingFirstModel()
    manager = ProviderManager(
        "gpt-4o",
        {"openai": "offline-test"},
        provider_key_override="openai",
        ignore_llm_source=True,
        provider_config_override={},
        retry_config=RetryConfig(max_retries=0),
    )
    await manager._provider._client.close()
    sdk = AsyncOpenAI(
        api_key="offline-test", http_client=httpx.AsyncClient(transport=httpx.MockTransport(model.handle))
    )
    manager._provider._client = sdk
    extension = _FalseyModelExtension()
    loop = AgentLoop(
        provider_manager=manager,
        system_prompt="plan",
        tool_registry=ToolRegistry(),
        cwd=str(tmp_path),
        **({extension_name: extension} if extension_name else {}),
    )
    if extension_name == "system_prompt_refresher":
        loop._refresh_system_prompt()
        assert extension.calls == 1
    elif extension_name == "background_task_starter":
        await loop._start_background_tasks()
        assert extension.calls == 1
    try:
        for request_number in (1, 2):
            model.entered.clear()
            active = asyncio.create_task(_ActiveModelStopCase.consume_model_loop(loop, manager))
            try:
                await asyncio.wait_for(model.entered.wait(), timeout=3)
            finally:
                active.cancel()
                await asyncio.gather(active, return_exceptions=True)
            if extension_name is None and request_number == 1:
                assert loop.canceled_first_model_request == "iac_code.providers.openai_provider.OpenAIProvider"
            else:
                assert loop.canceled_first_model_request is None
    finally:
        await sdk.close()
