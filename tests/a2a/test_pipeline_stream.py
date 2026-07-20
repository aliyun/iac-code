from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from a2a.types import TaskArtifactUpdateEvent, TaskStatusUpdateEvent
from google.protobuf.json_format import MessageToDict

import iac_code.a2a.pipeline_stream as pipeline_stream
from iac_code.a2a.artifacts import A2AArtifactStore
from iac_code.a2a.exposure import A2AExposureType
from iac_code.a2a.pipeline_events import PipelineA2AContext, PipelineEventTranslator
from iac_code.a2a.pipeline_journal import A2APipelineJournal
from iac_code.a2a.pipeline_performance import A2A_EXTREME_PERFORMANCE_ENV
from iac_code.a2a.pipeline_snapshot import A2APipelineSnapshotStore
from iac_code.a2a.pipeline_stream import (
    PipelineA2AEventPublisher,
    PipelineA2APersistenceError,
    is_recovery_semantic_event,
)
from iac_code.a2a.pipeline_transport_delivery import (
    PipelineTransportDeliveryClosedError,
    acknowledge_pipeline_transport_delivery,
    bind_pipeline_transport_delivery_tracker,
    close_pipeline_transport_delivery_tracker,
    create_pipeline_transport_delivery_tracker,
    pipeline_transport_delivery_required,
    pipeline_transport_delivery_tracking,
)
from iac_code.pipeline.engine.events import PipelineEvent, PipelineEventType
from iac_code.services.permissions.audit import fingerprint_text
from iac_code.types.permissions import PermissionAuditMetadata
from iac_code.types.stream_events import (
    AskUserQuestionEvent,
    CandidateDetailEvent,
    MCPProgressEvent,
    PermissionRequestEvent,
    SubPipelineStreamEvent,
    TextDeltaEvent,
    ThinkingDeltaEvent,
    ToolResultEvent,
    ToolUseEndEvent,
)

from .fakes import FakeEventQueue


def dump(event: Any) -> dict[str, Any]:
    return MessageToDict(event, preserving_proto_field_name=False)


def _has_truncated_object(value: object) -> bool:
    if isinstance(value, dict):
        if value.get("type") == "object" and value.get("truncated") is True:
            return True
        return any(_has_truncated_object(child) for child in value.values())
    return False


def _publisher(
    tmp_path: Path,
    *,
    artifact_store: A2AArtifactStore | None = None,
    exposure_types: object | None = None,
    a2a_artifacts_by_step_id: dict[str, list[dict[str, str]]] | None = None,
) -> tuple[PipelineA2AEventPublisher, FakeEventQueue]:
    queue = FakeEventQueue()
    context = PipelineA2AContext(
        pipeline_run_id="run-1",
        task_id="task-1",
        context_id="ctx-1",
        pipeline_name="selling",
        parent_step_order=["evaluate_candidates", "confirm_and_select"],
        candidate_step_order=["template_generating"],
        a2a_artifacts_by_step_id=a2a_artifacts_by_step_id or {},
    )
    pipeline_dir = tmp_path / "pipeline"
    publisher = PipelineA2AEventPublisher(
        event_queue=queue,
        translator=PipelineEventTranslator(context),
        journal=A2APipelineJournal(pipeline_dir),
        snapshot_store=A2APipelineSnapshotStore(pipeline_dir),
        artifact_store=artifact_store,
        exposure_types=exposure_types,
    )
    return publisher, queue


def _envelope(event_type: str, status: str = "working") -> dict[str, Any]:
    return {
        "schemaVersion": "1.0",
        "extensionUri": "urn:iac-code:a2a:pipeline-events:v1",
        "eventId": f"evt-{event_type}",
        "sequence": 1,
        "createdAt": "2026-06-08T10:00:00Z",
        "eventType": event_type,
        "scope": "pipeline",
        "pipelineRunId": "run-1",
        "taskId": "task-1",
        "contextId": "ctx-1",
        "pipelineName": "selling",
        "status": status,
        "data": {},
    }


def _encoded_envelope_size(envelope: dict[str, Any]) -> int:
    return len(json.dumps(envelope, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def _encoded_pipeline_batch_size(envelopes: list[dict[str, Any]]) -> int:
    return len(
        json.dumps(
            {
                "schemaVersion": "1.0",
                "extensionUri": "urn:iac-code:a2a:pipeline-events:v1",
                "events": envelopes,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    )


def test_pipeline_warning_is_recovery_semantic() -> None:
    assert is_recovery_semantic_event(_envelope("pipeline_warning")) is True


def test_unknown_working_step_event_is_recovery_semantic() -> None:
    envelope = _envelope("custom_step_progress")
    envelope["scope"] = "step"

    assert is_recovery_semantic_event(envelope) is True


def test_mcp_status_is_metadata_only_not_recovery_semantic() -> None:
    envelope = _envelope("mcp_status")

    assert is_recovery_semantic_event(envelope) is False


@pytest.mark.asyncio
async def test_publish_text_writes_pipeline_metadata_without_duplicate_status_message(tmp_path: Path) -> None:
    publisher, queue = _publisher(tmp_path)

    returned = await publisher.publish(TextDeltaEvent(text="hello"))

    assert returned == "hello"
    assert isinstance(queue.events[0], TaskStatusUpdateEvent)
    dumped = dump(queue.events[0])
    assert dumped["status"]["state"] == "TASK_STATE_WORKING"
    assert "message" not in dumped["status"]
    envelope = dumped["metadata"]["iac_code"]["pipeline"]
    assert envelope["eventType"] == "text_delta"
    assert envelope["data"]["text"] == "hello"
    assert publisher.journal.read_all()[0]["data"]["text"] == "hello"
    snapshot = publisher.snapshot_store.load()
    assert snapshot is not None
    assert snapshot["display"]["messages"][0]["text"] == "hello"


@pytest.mark.asyncio
async def test_publish_mcp_progress_preserves_canonical_metadata(tmp_path: Path) -> None:
    publisher, queue = _publisher(tmp_path)

    await publisher.publish(
        MCPProgressEvent(
            server_name="live",
            tool_name="echo",
            public_name="mcp__live__echo_8d3f",
            progress=1,
            total=2,
            message="halfway",
            tool_use_id="tool-1",
        )
    )

    envelope = dump(queue.events[0])["metadata"]["iac_code"]["pipeline"]
    assert envelope["eventType"] == "tool_progress"
    assert envelope["data"]["mcpProgress"] == {
        "status": "progress",
        "toolUseId": "tool-1",
        "publicName": "mcp__live__echo_8d3f",
        "originalServerName": "live",
        "originalToolName": "echo",
        "progress": 1,
        "total": 2,
        "message": "halfway",
    }


@pytest.mark.asyncio
async def test_test_fault_injection_fires_after_snapshot_save(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    publisher, queue = _publisher(tmp_path)
    monkeypatch.setenv("IAC_CODE_TEST_FAULT_INJECTION", "1")
    monkeypatch.setenv("IAC_CODE_TEST_FAULT_INJECTION_MODE", "raise")
    monkeypatch.setenv("IAC_CODE_TEST_CRASH_AT", "after_a2a_pipeline_snapshot_saved")

    with pytest.raises(RuntimeError, match="after_a2a_pipeline_snapshot_saved"):
        await publisher.publish_manual("pipeline_started", "pipeline")

    snapshot = publisher.snapshot_store.load()
    assert snapshot is not None
    assert snapshot["status"] == "working"
    assert queue.events == []


@pytest.mark.asyncio
async def test_publish_serializes_delivery_while_before_enqueue_waits(tmp_path: Path) -> None:
    publisher, queue = _publisher(tmp_path)
    hook_started = asyncio.Event()
    release_hook = asyncio.Event()

    async def before_enqueue(envelope: dict[str, Any]) -> bool:
        if envelope.get("eventType") == "pipeline_started":
            hook_started.set()
            await release_hook.wait()
        return True

    publisher.before_enqueue = before_enqueue
    first = asyncio.create_task(publisher.publish_manual("pipeline_started", "pipeline", status="working"))
    await asyncio.wait_for(hook_started.wait(), timeout=1)
    second = asyncio.create_task(publisher.publish_manual("pipeline_warning", "pipeline", status="working"))
    await asyncio.sleep(0.05)

    assert queue.events == []
    assert not second.done()

    release_hook.set()
    await asyncio.wait_for(asyncio.gather(first, second), timeout=1)

    pipeline_events = [dump(event)["metadata"]["iac_code"]["pipeline"] for event in queue.events]
    assert [event["eventType"] for event in pipeline_events] == ["pipeline_started", "pipeline_warning"]


@pytest.mark.asyncio
async def test_publish_rebuilds_missing_snapshot_from_journal_history(tmp_path: Path) -> None:
    publisher, _queue = _publisher(tmp_path)
    await publisher.publish(TextDeltaEvent(text="hello"))
    publisher.snapshot_store.path.unlink()

    await publisher.publish(TextDeltaEvent(text=" world"))

    assert [event["data"]["text"] for event in publisher.journal.read_all()] == ["hello", " world"]
    snapshot = publisher.snapshot_store.load()
    assert snapshot is not None
    assert snapshot["display"]["messages"][0]["text"] == "hello world"


@pytest.mark.asyncio
async def test_publish_rebuilds_stale_schema_snapshot_from_journal_history(tmp_path: Path) -> None:
    publisher, _queue = _publisher(tmp_path)
    await publisher.publish(TextDeltaEvent(text="old"))
    stale_snapshot = publisher.snapshot_store.load()
    assert stale_snapshot is not None
    stale_snapshot["schemaVersion"] = "1.0"
    stale_snapshot["display"]["messages"] = []
    publisher.snapshot_store.save(stale_snapshot)

    await publisher.publish(TextDeltaEvent(text=" new"))

    snapshot = publisher.snapshot_store.load()
    assert snapshot is not None
    assert snapshot["schemaVersion"] == "1.1"
    assert snapshot["display"]["messages"][0]["text"] == "old new"


@pytest.mark.asyncio
async def test_publish_empty_text_delta_is_skipped(tmp_path: Path) -> None:
    publisher, queue = _publisher(tmp_path)

    returned = await publisher.publish(TextDeltaEvent(text=""))

    assert returned is None
    assert queue.events == []
    assert publisher.journal.read_all() == []
    assert publisher.snapshot_store.load() is None


@pytest.mark.asyncio
async def test_publish_thinking_delta_requires_raw_thinking_exposure(tmp_path: Path) -> None:
    publisher, queue = _publisher(tmp_path, exposure_types=[])

    returned = await publisher.publish(ThinkingDeltaEvent(text="hidden reasoning"))

    assert returned is None
    assert queue.events == []
    assert publisher.journal.read_all() == []
    assert publisher.snapshot_store.load() is None


@pytest.mark.asyncio
async def test_publish_thinking_delta_writes_pipeline_metadata_when_exposed(tmp_path: Path) -> None:
    publisher, queue = _publisher(tmp_path, exposure_types=[A2AExposureType.RAW_THINKING])

    returned = await publisher.publish(ThinkingDeltaEvent(text="visible reasoning"))

    assert returned is None
    assert isinstance(queue.events[0], TaskStatusUpdateEvent)
    dumped = dump(queue.events[0])
    assert dumped["status"]["state"] == "TASK_STATE_WORKING"
    assert "message" not in dumped["status"]
    envelope = dumped["metadata"]["iac_code"]["pipeline"]
    assert envelope["eventType"] == "thinking_delta"
    assert envelope["data"] == {"type": "raw_thinking", "text": "visible reasoning"}
    assert publisher.journal.read_all()[0]["data"] == {"type": "raw_thinking", "text": "visible reasoning"}


@pytest.mark.asyncio
async def test_publish_batch_persists_each_delta_but_enqueues_once(tmp_path: Path) -> None:
    publisher, queue = _publisher(tmp_path)

    await publisher.publish_batch([TextDeltaEvent(text="a"), TextDeltaEvent(text="b")])

    payload = dump(queue.events[0])["metadata"]["iac_code"]["pipelineBatch"]
    assert payload["schemaVersion"] == "1.0"
    assert payload["extensionUri"] == "urn:iac-code:a2a:pipeline-events:v1"
    assert [event["data"]["text"] for event in payload["events"]] == ["a", "b"]
    assert [event["data"]["text"] for event in publisher.journal.read_all()] == ["a", "b"]
    assert [event["sequence"] for event in publisher.journal.read_all()] == [1, 2]
    snapshot = publisher.snapshot_store.load()
    assert snapshot is not None
    assert snapshot["display"]["messages"][0]["text"] == "ab"


@pytest.mark.asyncio
async def test_publish_batch_mixes_adjacent_exposed_thinking_and_text_deltas(tmp_path: Path) -> None:
    publisher, queue = _publisher(tmp_path, exposure_types=[A2AExposureType.RAW_THINKING])

    await publisher.publish_batch([ThinkingDeltaEvent(text="reasoning"), TextDeltaEvent(text="answer")])

    envelopes = dump(queue.events[0])["metadata"]["iac_code"]["pipelineBatch"]["events"]
    assert [envelope["eventType"] for envelope in envelopes] == ["thinking_delta", "text_delta"]
    assert [envelope["sequence"] for envelope in envelopes] == [1, 2]


@pytest.mark.asyncio
async def test_publish_batch_extreme_mode_defers_delta_sidecar_persistence_until_semantic_flush(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(A2A_EXTREME_PERFORMANCE_ENV, raising=False)
    publisher, queue = _publisher(tmp_path)

    await publisher.publish_batch([TextDeltaEvent(text="a"), TextDeltaEvent(text="b")])

    assert len(queue.events) == 1
    assert publisher.journal.read_all() == []
    assert publisher.snapshot_store.load() is None

    await publisher.publish_manual("pipeline_warning", "pipeline", data={"message": "flush"})

    assert [event["eventType"] for event in publisher.journal.read_all()] == [
        "text_delta",
        "text_delta",
        "pipeline_warning",
    ]
    snapshot = publisher.snapshot_store.load()
    assert snapshot is not None
    assert snapshot["display"]["messages"][0]["text"] == "ab"


@pytest.mark.asyncio
async def test_publish_batch_holds_delivery_lock_while_before_enqueue_waits(tmp_path: Path) -> None:
    publisher, queue = _publisher(tmp_path)
    hook_started = asyncio.Event()
    release_hook = asyncio.Event()

    async def before_enqueue(envelope: dict[str, Any]) -> bool:
        if envelope.get("data", {}).get("text") == "a":
            hook_started.set()
            await release_hook.wait()
        return True

    publisher.before_enqueue = before_enqueue
    batch = asyncio.create_task(publisher.publish_batch([TextDeltaEvent(text="a"), TextDeltaEvent(text="b")]))
    await asyncio.wait_for(hook_started.wait(), timeout=1)
    concurrent = asyncio.create_task(publisher.publish(TextDeltaEvent(text="c")))
    await asyncio.sleep(0.05)

    assert queue.events == []
    assert not concurrent.done()

    release_hook.set()
    await asyncio.wait_for(asyncio.gather(batch, concurrent), timeout=1)

    first = dump(queue.events[0])["metadata"]["iac_code"]["pipelineBatch"]["events"]
    second = dump(queue.events[1])["metadata"]["iac_code"]["pipeline"]
    assert [envelope["data"]["text"] for envelope in first] == ["a", "b"]
    assert second["data"]["text"] == "c"


@pytest.mark.asyncio
async def test_publish_batch_translates_nested_sub_pipeline_delta_events(tmp_path: Path) -> None:
    publisher, queue = _publisher(tmp_path)
    event = SubPipelineStreamEvent(
        sub_pipeline_id="outer",
        candidate_index=1,
        inner=SubPipelineStreamEvent(
            sub_pipeline_id="inner",
            candidate_index=0,
            inner=TextDeltaEvent(text="hello"),
        ),
    )

    await publisher.publish_batch([event, TextDeltaEvent(text=" world")])

    events = dump(queue.events[0])["metadata"]["iac_code"]["pipelineBatch"]["events"]
    assert events[0]["candidate"]["id"] == "inner"
    assert [event["data"]["text"] for event in publisher.journal.read_all()] == ["hello", " world"]
    snapshot = publisher.snapshot_store.load()
    assert snapshot is not None
    assert [message["text"] for message in snapshot["display"]["messages"]] == ["hello", " world"]


@pytest.mark.asyncio
async def test_publish_batch_filters_hidden_thinking_deltas(tmp_path: Path) -> None:
    publisher, queue = _publisher(tmp_path, exposure_types=[])

    await publisher.publish_batch([ThinkingDeltaEvent(text="hidden"), TextDeltaEvent(text="visible")])

    envelope = dump(queue.events[0])["metadata"]["iac_code"]["pipeline"]
    assert envelope["data"]["text"] == "visible"
    assert [event["data"]["text"] for event in publisher.journal.read_all()] == ["visible"]


@pytest.mark.asyncio
async def test_publish_batch_uses_legacy_metadata_for_one_persisted_envelope(tmp_path: Path) -> None:
    publisher, queue = _publisher(tmp_path)

    await publisher.publish_batch([TextDeltaEvent(text="only")])

    metadata = dump(queue.events[0])["metadata"]["iac_code"]
    assert metadata["pipeline"]["data"]["text"] == "only"
    assert "pipelineBatch" not in metadata


@pytest.mark.asyncio
async def test_publish_batch_splits_persisted_envelopes_by_count_and_encoded_size(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    publisher, queue = _publisher(tmp_path)
    monkeypatch.setattr(pipeline_stream, "OUTBOUND_HARD_MAX_BATCH_EVENTS", 2)
    monkeypatch.setattr(pipeline_stream, "OUTBOUND_HARD_MAX_BATCH_BYTES", 10_000)

    await publisher.publish_batch([TextDeltaEvent(text="a"), TextDeltaEvent(text="b"), TextDeltaEvent(text="c")])

    count_chunks = [dump(event)["metadata"]["iac_code"] for event in queue.events]
    assert [envelope["data"]["text"] for envelope in count_chunks[0]["pipelineBatch"]["events"]] == ["a", "b"]
    assert count_chunks[1]["pipeline"]["data"]["text"] == "c"

    publisher, queue = _publisher(tmp_path / "bytes")
    first = await publisher.persist_envelope(publisher.translator.translate(TextDeltaEvent(text="a"))[0])
    assert first is not None
    monkeypatch.setattr(pipeline_stream, "OUTBOUND_HARD_MAX_BATCH_EVENTS", 64)
    monkeypatch.setattr(pipeline_stream, "OUTBOUND_HARD_MAX_BATCH_BYTES", _encoded_envelope_size(first) * 2 - 1)

    frame_count = await publisher.enqueue_persisted_batch(
        await publisher.persist_batch_events([TextDeltaEvent(text="b"), TextDeltaEvent(text="c")])
    )

    assert [dump(event)["metadata"]["iac_code"]["pipeline"]["data"]["text"] for event in queue.events] == ["b", "c"]
    assert frame_count == 2


def test_envelope_batch_size_is_computed_once_per_envelope(monkeypatch: pytest.MonkeyPatch) -> None:
    envelopes = [_envelope(f"event-{index}") for index in range(130)]
    original = pipeline_stream._encoded_pipeline_envelope_size
    calls = 0

    def count_size(envelope: dict[str, Any]) -> int:
        nonlocal calls
        calls += 1
        return original(envelope)

    monkeypatch.setattr(pipeline_stream, "_encoded_pipeline_envelope_size", count_size)
    monkeypatch.setattr(pipeline_stream, "OUTBOUND_HARD_MAX_BATCH_EVENTS", 64)

    batches = list(pipeline_stream._envelope_batches(envelopes))

    assert [len(batch) for batch in batches] == [64, 64, 2]
    assert calls == len(envelopes)


@pytest.mark.asyncio
async def test_publish_batch_counts_pipeline_batch_wrapper_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    publisher, queue = _publisher(tmp_path)
    first = publisher.translator.translate(TextDeltaEvent(text="a"))[0]
    second = publisher.translator.translate(TextDeltaEvent(text="b"))[0]
    envelope_bytes = _encoded_envelope_size(first) + _encoded_envelope_size(second)
    assert _encoded_pipeline_batch_size([first, second]) > envelope_bytes
    monkeypatch.setattr(pipeline_stream, "OUTBOUND_HARD_MAX_BATCH_EVENTS", 64)
    monkeypatch.setattr(pipeline_stream, "OUTBOUND_HARD_MAX_BATCH_BYTES", envelope_bytes)

    await publisher.publish_batch([TextDeltaEvent(text="a"), TextDeltaEvent(text="b")])

    assert [dump(event)["metadata"]["iac_code"]["pipeline"]["data"]["text"] for event in queue.events] == ["a", "b"]


@pytest.mark.asyncio
async def test_publish_batch_uses_legacy_delivery_for_one_oversized_envelope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    publisher, queue = _publisher(tmp_path)
    monkeypatch.setattr(pipeline_stream, "OUTBOUND_HARD_MAX_BATCH_BYTES", 1)

    await publisher.publish_batch([TextDeltaEvent(text="oversized")])

    metadata = dump(queue.events[0])["metadata"]["iac_code"]
    assert metadata["pipeline"]["data"]["text"] == "oversized"
    assert "pipelineBatch" not in metadata


@pytest.mark.asyncio
async def test_publish_top_level_candidate_detail_updates_metadata_and_snapshot(tmp_path: Path) -> None:
    publisher, queue = _publisher(tmp_path)
    await publisher.publish(
        PipelineEvent(
            type=PipelineEventType.STEP_STARTED,
            step_id="confirm_and_select",
            timestamp=1717821600.0,
            data={"index": 2, "total": 2},
        )
    )

    await publisher.publish(
        CandidateDetailEvent(
            tool_use_id="toolu-detail",
            candidate_name="low cost",
            summary="single ecs",
            cost_items=[{"name": "ecs", "monthly_cost": "CNY 60"}],
            total_monthly_cost="CNY 60",
        )
    )

    envelope = dump(queue.events[-1])["metadata"]["iac_code"]["pipeline"]
    assert envelope["eventType"] == "candidate_detail_shown"
    assert envelope["scope"] == "step"
    assert envelope["step"]["id"] == "confirm_and_select"
    assert envelope["data"]["detail"]["candidateName"] == "low cost"
    snapshot = publisher.snapshot_store.load()
    assert snapshot is not None
    assert snapshot["display"]["candidateDetails"][0]["detail"]["costItems"] == [
        {"name": "ecs", "monthly_cost": "CNY 60"}
    ]
    assert snapshot["display"]["candidateDetails"][0]["step"]["id"] == "confirm_and_select"


@pytest.mark.asyncio
async def test_publish_sub_pipeline_permission_resolves_inner_future_and_publishes_metadata(tmp_path: Path) -> None:
    publisher, queue = _publisher(tmp_path)
    await publisher.publish(
        PipelineEvent(
            type=PipelineEventType.SUB_PIPELINE_STARTED,
            step_id=None,
            timestamp=1717821600.0,
            data={
                "sub_pipeline_id": "eval-abcd",
                "candidate_index": 0,
                "candidate_name": "candidate",
                "parent_step_id": "evaluate_candidates",
                "total_steps": 1,
            },
        )
    )
    future: asyncio.Future[bool] = asyncio.get_running_loop().create_future()

    returned = await publisher.publish(
        SubPipelineStreamEvent(
            sub_pipeline_id="eval-abcd",
            candidate_index=0,
            inner=PermissionRequestEvent(
                tool_name="bash",
                tool_input={"cmd": "pwd"},
                tool_use_id="toolu-1",
                response_future=future,
            ),
        ),
        auto_approve_permissions=True,
    )

    assert returned is None
    assert future.result() is True
    dumped = dump(queue.events[-1])
    assert dumped["status"]["state"] == "TASK_STATE_WORKING"
    permission = dumped["metadata"]["iac_code"]["pipeline"]["permission"]
    assert permission["permissionId"] == "perm-toolu-1"
    assert permission["toolName"] == "bash"
    assert permission["toolUseId"] == "toolu-1"
    assert permission["safeSummary"] == "bash permission request (fields: cmd)"
    assert permission["toolInput"] == {"cmd": {"type": "str", "length": 3, "fingerprint": fingerprint_text("pwd")}}
    assert permission["approved"] is True
    assert permission["decision"] == "allow_once"
    assert publisher.journal.read_all()[-1]["permission"]["approved"] is True


@pytest.mark.asyncio
async def test_publish_nested_sub_pipeline_permission_resolves_inner_future(tmp_path: Path) -> None:
    publisher, queue = _publisher(tmp_path)
    await publisher.publish(
        PipelineEvent(
            type=PipelineEventType.SUB_PIPELINE_STARTED,
            step_id=None,
            timestamp=1717821600.0,
            data={
                "sub_pipeline_id": "eval-inner",
                "candidate_index": 0,
                "candidate_name": "candidate",
                "parent_step_id": "evaluate_candidates",
                "total_steps": 1,
            },
        )
    )
    future: asyncio.Future[bool] = asyncio.get_running_loop().create_future()

    returned = await publisher.publish(
        SubPipelineStreamEvent(
            sub_pipeline_id="eval-outer",
            candidate_index=1,
            inner=SubPipelineStreamEvent(
                sub_pipeline_id="eval-inner",
                candidate_index=0,
                inner=PermissionRequestEvent(
                    tool_name="aliyun_api",
                    tool_input={"product": "ros", "action": "CreateStack"},
                    tool_use_id="toolu-nested",
                    response_future=future,
                    audit_context={
                        "metadata": PermissionAuditMetadata(
                            scope="once",
                            source="permission_pipeline",
                            reason_type="untrusted_write",
                            reason_detail="untrusted Aliyun write",
                            is_read_only=False,
                            operation={"product": "ros", "action": "CreateStack", "is_read_only": False},
                        )
                    },
                ),
            ),
        ),
        auto_approve_permissions=False,
    )

    assert returned is None
    assert future.result() is False
    permission = dump(queue.events[-1])["metadata"]["iac_code"]["pipeline"]["permission"]
    assert permission["toolName"] == "aliyun_api"
    assert permission["toolUseId"] == "toolu-nested"
    assert permission["inputSummary"]["tool_name"] == "aliyun_api"
    assert permission["approved"] is False


@pytest.mark.asyncio
async def test_publish_direct_permission_resolver_overrides_auto_approve(tmp_path: Path) -> None:
    publisher, queue = _publisher(tmp_path)
    future: asyncio.Future[bool] = asyncio.get_running_loop().create_future()

    returned = await publisher.publish(
        PermissionRequestEvent(
            tool_name="bash",
            tool_input={"cmd": "pwd"},
            tool_use_id="toolu-direct",
            response_future=future,
        ),
        permission_resolver=lambda _request: False,
        auto_approve_permissions=True,
    )

    assert returned is None
    assert future.result() is False
    permission = dump(queue.events[0])["metadata"]["iac_code"]["pipeline"]["permission"]
    assert permission["approved"] is False
    assert permission["decision"] == "deny"
    assert permission["toolUseId"] == "toolu-direct"


@pytest.mark.asyncio
async def test_prepared_permission_resolves_only_after_transport_consumes_frame(tmp_path: Path) -> None:
    publisher, queue = _publisher(tmp_path)
    future: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
    event = PermissionRequestEvent(
        tool_name="bash",
        tool_input={"cmd": "pwd"},
        tool_use_id="toolu-prepared",
        response_future=future,
    )

    approved = await publisher.resolve_permission_event(event, auto_approve_permissions=True)
    prepared = await publisher.prepare_permission_event(event, approved=approved, resolver_used=False)
    with pipeline_transport_delivery_tracking():
        delivery = asyncio.create_task(publisher.enqueue_prepared_permission(prepared))
        while not queue.events:
            await asyncio.sleep(0)

        assert future.done() is False
        assert delivery.done() is False
        await asyncio.sleep(0.12)
        assert delivery.done() is False
        acknowledge_pipeline_transport_delivery(queue.events[0])
        delivered = await delivery
    publisher.complete_prepared_permission(prepared, delivered=delivered)

    assert future.result() is True
    permission = dump(queue.events[0])["metadata"]["iac_code"]["pipeline"]["permission"]
    assert permission["toolUseId"] == "toolu-prepared"


@pytest.mark.asyncio
async def test_direct_publication_waits_for_transport_inside_outbound_callback_scope(tmp_path: Path) -> None:
    publisher, queue = _publisher(tmp_path)

    with pipeline_transport_delivery_tracking(), pipeline_transport_delivery_required():
        delivery = asyncio.create_task(publisher.publish(TextDeltaEvent(text="callback")))
        while not queue.events:
            await asyncio.sleep(0)

        assert delivery.done() is False
        acknowledge_pipeline_transport_delivery(queue.events[0])
        assert await delivery == "callback"


@pytest.mark.asyncio
async def test_inherited_closed_transport_tracker_rejects_later_publication(tmp_path: Path) -> None:
    publisher, queue = _publisher(tmp_path)
    tracker = create_pipeline_transport_delivery_tracker()
    release = asyncio.Event()

    async def publish_later() -> str | None:
        await release.wait()
        with pipeline_transport_delivery_required():
            return await publisher.publish(TextDeltaEvent(text="after-disconnect"))

    with bind_pipeline_transport_delivery_tracker(tracker):
        publication = asyncio.create_task(publish_later())
    close_pipeline_transport_delivery_tracker(tracker)
    release.set()

    with pytest.raises(PipelineTransportDeliveryClosedError):
        await publication
    assert queue.events == []


@pytest.mark.asyncio
async def test_publish_direct_auto_approve_denies_untrusted_aliyun_write(tmp_path: Path) -> None:
    publisher, queue = _publisher(tmp_path)
    future: asyncio.Future[bool] = asyncio.get_running_loop().create_future()

    returned = await publisher.publish(
        PermissionRequestEvent(
            tool_name="aliyun_api",
            tool_input={"product": "ros", "action": "CreateStack"},
            tool_use_id="toolu-aliyun-write",
            response_future=future,
            audit_context={
                "metadata": PermissionAuditMetadata(
                    scope="once",
                    source="permission_pipeline",
                    reason_type="untrusted_write",
                    reason_detail="untrusted Aliyun write",
                    is_read_only=False,
                    operation={"product": "ros", "action": "CreateStack", "is_read_only": False},
                )
            },
        ),
        auto_approve_permissions=True,
    )

    assert returned is None
    assert future.result() is False
    permission = dump(queue.events[0])["metadata"]["iac_code"]["pipeline"]["permission"]
    assert permission["approved"] is False
    assert permission["decision"] == "deny"
    assert permission["toolUseId"] == "toolu-aliyun-write"


@pytest.mark.asyncio
async def test_publish_permission_denies_future_when_pipeline_metadata_is_not_persisted(tmp_path: Path) -> None:
    publisher, queue = _publisher(tmp_path)
    future: asyncio.Future[bool] = asyncio.get_running_loop().create_future()

    def fail_read_all() -> list[dict[str, Any]]:
        raise OSError("read failed")

    publisher.journal.read_all_repairing_tail = fail_read_all  # type: ignore[method-assign]

    returned = await publisher.publish(
        PermissionRequestEvent(
            tool_name="bash",
            tool_input={"cmd": "deploy"},
            tool_use_id="toolu-denied",
            response_future=future,
        ),
        auto_approve_permissions=True,
    )

    assert returned is None
    assert future.result() is False
    assert queue.events == []
    assert publisher.snapshot_store.load() is None


@pytest.mark.asyncio
async def test_publish_permission_denies_future_when_permission_metadata_is_not_durable(tmp_path: Path) -> None:
    publisher, queue = _publisher(tmp_path)
    future: asyncio.Future[bool] = asyncio.get_running_loop().create_future()

    def fail_append(_event: dict[str, Any], durable: bool = False) -> None:
        raise OSError("append failed")

    def fail_save(_snapshot: dict[str, Any]) -> bool:
        return False

    publisher.journal.append = fail_append  # type: ignore[method-assign]
    publisher.snapshot_store.save = fail_save  # type: ignore[method-assign]

    returned = await publisher.publish(
        PermissionRequestEvent(
            tool_name="bash",
            tool_input={"cmd": "deploy"},
            tool_use_id="toolu-not-durable",
            response_future=future,
        ),
        auto_approve_permissions=True,
    )

    assert returned is None
    assert future.result() is False
    assert queue.events == []


@pytest.mark.asyncio
async def test_recovery_semantic_event_is_not_enqueued_when_metadata_persistence_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    publisher, queue = _publisher(tmp_path)

    def fail_append(_event: dict[str, Any], durable: bool = False) -> None:
        raise OSError("journal locked")

    monkeypatch.setattr(publisher.journal, "append", fail_append)
    monkeypatch.setattr(publisher.snapshot_store, "save", lambda _snapshot: False)

    result = await publisher.publish_manual("pipeline_started", "pipeline")

    assert result is None
    assert queue.events == []


@pytest.mark.asyncio
async def test_text_delta_can_be_enqueued_when_only_durable_metadata_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    publisher, queue = _publisher(tmp_path)

    def fail_append(event: dict[str, Any], durable: bool = False) -> None:
        if durable:
            raise OSError("journal locked")
        A2APipelineJournal.append(publisher.journal, event)

    monkeypatch.setattr(publisher.journal, "append", fail_append)
    monkeypatch.setattr(publisher.snapshot_store, "save", lambda _snapshot: False)

    returned = await publisher.publish(TextDeltaEvent(text="hello"))

    assert returned == "hello"
    assert len(queue.events) == 1


@pytest.mark.asyncio
async def test_manual_recovery_event_routes_durable_metadata_without_explicit_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    publisher, _queue = _publisher(tmp_path)
    durable_flags: list[bool] = []

    def record_append(event: dict[str, Any], durable: bool = False) -> None:
        durable_flags.append(durable)
        A2APipelineJournal.append(publisher.journal, event)

    monkeypatch.setattr(publisher.journal, "append", record_append)

    await publisher.publish_manual("pipeline_started", "pipeline")

    assert durable_flags == [True]


@pytest.mark.asyncio
async def test_translated_recovery_event_routes_durable_metadata_without_explicit_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    publisher, _queue = _publisher(tmp_path)
    durable_flags: list[bool] = []

    def record_append(event: dict[str, Any], durable: bool = False) -> None:
        durable_flags.append(durable)
        A2APipelineJournal.append(publisher.journal, event)

    monkeypatch.setattr(publisher.journal, "append", record_append)

    await publisher.publish(
        PipelineEvent(
            type=PipelineEventType.STEP_STARTED,
            step_id="confirm_and_select",
            timestamp=1717821600.0,
            data={"index": 1, "total": 2},
        )
    )

    assert durable_flags == [True]


@pytest.mark.asyncio
async def test_publish_permission_redacts_and_truncates_tool_input_in_status_metadata_and_journal(
    tmp_path: Path,
) -> None:
    publisher, queue = _publisher(tmp_path)
    nested: object = "leaf"
    for _ in range(80):
        nested = {"next": nested}

    await publisher.publish(
        PermissionRequestEvent(
            tool_name="bash",
            tool_input={
                "cmd": "x" * 5000,
                "apiKey": "secret-value",
                "Signature": "signature-secret",
                "nested": nested,
            },
            tool_use_id="toolu-large",
        ),
        auto_approve_permissions=True,
    )

    status_tool_input = dump(queue.events[0])["metadata"]["iac_code"]["pipeline"]["permission"]["toolInput"]
    journal_tool_input = publisher.journal.read_all()[0]["permission"]["toolInput"]
    for tool_input in (status_tool_input, journal_tool_input):
        assert tool_input["cmd"] == {
            "type": "str",
            "length": 5000,
            "fingerprint": fingerprint_text("x" * 5000),
        }
        assert tool_input[fingerprint_text("apiKey")] == {"redacted": True}
        assert tool_input[fingerprint_text("Signature")] == {"redacted": True}
        assert "apiKey" not in str(tool_input)
        assert "Signature" not in str(tool_input)
        assert _has_truncated_object(tool_input[fingerprint_text("nested")])
    assert "secret-value" not in str(dump(queue.events[0]))
    assert "signature-secret" not in str(dump(queue.events[0]))
    assert "secret-value" not in str(publisher.journal.read_all()[0])
    assert "signature-secret" not in str(publisher.journal.read_all()[0])


@pytest.mark.asyncio
async def test_publish_permission_omits_tool_input_when_tool_trace_disabled(tmp_path: Path) -> None:
    publisher, queue = _publisher(tmp_path, exposure_types=[])

    await publisher.publish(
        PermissionRequestEvent(
            tool_name="bash",
            tool_input={"cmd": "pwd", "token": "secret-value"},
            tool_use_id="toolu-private",
        ),
        auto_approve_permissions=True,
    )

    permission = dump(queue.events[0])["metadata"]["iac_code"]["pipeline"]["permission"]
    assert permission["safeSummary"] == "bash permission request (fields: [redacted], cmd)"
    assert "toolInput" not in permission
    assert "secret-value" not in str(publisher.journal.read_all()[0])


@pytest.mark.asyncio
async def test_publish_tool_result_externalizes_artifact_and_updates_snapshot(tmp_path: Path) -> None:
    store = A2AArtifactStore(tmp_path / "artifacts")
    publisher, queue = _publisher(tmp_path, artifact_store=store, exposure_types=[A2AExposureType.TOOL_TRACE])

    await publisher.publish(
        ToolResultEvent(
            tool_use_id="toolu-write",
            tool_name="write_file",
            result={"artifact": {"filename": "template.yaml", "mediaType": "text/yaml", "content": "ROSTemplate"}},
        )
    )

    assert isinstance(queue.events[0], TaskArtifactUpdateEvent)
    artifact_update = dump(queue.events[0])
    assert artifact_update["artifact"]["name"] == "template.yaml"
    assert artifact_update["artifact"]["parts"][0]["url"].startswith("iac-code-artifact://")
    status = dump(queue.events[1])["metadata"]["iac_code"]["pipeline"]
    assert status["eventType"] == "artifact_created"
    assert status["artifact"]["filename"] == "template.yaml"
    assert "content" not in str(status)
    journal_event = publisher.journal.read_all()[0]
    assert journal_event["eventType"] == "artifact_created"
    assert journal_event["artifact"]["artifactId"] == artifact_update["artifact"]["artifactId"]
    snapshot = publisher.snapshot_store.load()
    assert snapshot is not None
    assert snapshot["display"]["artifacts"][0]["artifactId"] == artifact_update["artifact"]["artifactId"]
    for value in (artifact_update, status, journal_event, snapshot):
        rendered = str(value)
        assert "file://" not in rendered
        assert str(tmp_path) not in rendered


@pytest.mark.asyncio
async def test_batch_persistence_externalizes_tool_artifact_without_losing_mixed_events(tmp_path: Path) -> None:
    store = A2AArtifactStore(tmp_path / "artifacts")
    publisher, queue = _publisher(tmp_path, artifact_store=store, exposure_types=[A2AExposureType.TOOL_TRACE])
    persisted = await publisher.persist_batch_events(
        [
            TextDeltaEvent(text="before"),
            ToolResultEvent(
                tool_use_id="toolu-write",
                tool_name="write_file",
                result={"artifact": {"filename": "template.yaml", "content": "ROSTemplate"}},
            ),
            TextDeltaEvent(text="after"),
        ]
    )

    await publisher.enqueue_persisted_batch(persisted)

    assert any(isinstance(event, TaskArtifactUpdateEvent) for event in queue.events)
    status_envelopes = [
        envelope
        for event in queue.events
        if isinstance(event, TaskStatusUpdateEvent)
        for envelope in (
            dump(event)["metadata"]["iac_code"]["pipelineBatch"]["events"]
            if "pipelineBatch" in dump(event)["metadata"]["iac_code"]
            else [dump(event)["metadata"]["iac_code"]["pipeline"]]
        )
    ]
    assert [envelope["eventType"] for envelope in status_envelopes] == [
        "text_delta",
        "artifact_created",
        "text_delta",
    ]


@pytest.mark.asyncio
async def test_batch_persistence_failure_is_explicit_instead_of_dropping_event(tmp_path: Path) -> None:
    publisher, queue = _publisher(tmp_path)

    def fail_read_all() -> list[dict[str, Any]]:
        raise OSError("read failed")

    publisher.journal.read_all_repairing_tail = fail_read_all  # type: ignore[method-assign]

    with pytest.raises(PipelineA2APersistenceError):
        await publisher.persist_batch_events([TextDeltaEvent(text="must-not-drop")])

    assert queue.events == []
    assert "ROSTemplate" not in str(queue.events)


@pytest.mark.asyncio
async def test_publish_tool_result_uri_only_artifact_drops_legacy_file_uri(tmp_path: Path) -> None:
    publisher, queue = _publisher(tmp_path, exposure_types=[A2AExposureType.TOOL_TRACE])

    await publisher.publish(
        ToolResultEvent(
            tool_use_id="toolu-write",
            tool_name="write_file",
            result={
                "artifact": {
                    "filename": "template.yaml",
                    "uri": r"file://C:\Users\alice\.iac-code\projects\demo\template.yaml",
                    "publicUrl": r"iac-code-artifact://artifact-1/C:\Users\alice\.iac-code\projects\demo\template.yaml",
                    "encodedOwnerUrl": (
                        "iac-code-artifact://C%3A%5CUsers%5Calice%5C.iac-code%5Cprojects%5Cdemo/template.yaml"
                    ),
                    "backupUri": [r"file://C:\Users\alice\.iac-code\projects\demo\template.yaml"],
                    "source": r"file://C:\Users\alice\.iac-code\projects\demo\template.yaml",
                    "metadata": {
                        "uri": [r"file://C:\Users\alice\.iac-code\projects\demo\template.yaml"],
                        "byteSize": 10,
                    },
                    "parts": [
                        {
                            "url": r"file://C:\Users\alice\.iac-code\projects\demo\template.yaml",
                            "metadata": {"uri": r"file://C:\Users\alice\.iac-code\projects\demo\template.yaml"},
                        }
                    ],
                }
            },
        )
    )

    status = dump(queue.events[0])["metadata"]["iac_code"]["pipeline"]
    journal_event = publisher.journal.read_all()[0]
    snapshot = publisher.snapshot_store.load()
    for artifact in (
        status["data"]["result"]["artifact"],
        journal_event["data"]["result"]["artifact"],
        snapshot["display"]["toolResults"][0]["result"]["artifact"],
    ):
        assert artifact["filename"] == "template.yaml"
        assert artifact["metadata"] == {"byteSize": 10}
        assert "uri" not in artifact
        assert "publicUrl" not in artifact
        assert "encodedOwnerUrl" not in artifact
        assert "backupUri" not in artifact
        assert artifact["source"] == "[PATH]"
        assert "url" not in artifact["parts"][0]
        assert "uri" not in artifact["parts"][0]["metadata"]
    rendered = str((status, journal_event, snapshot))
    assert "file://" not in rendered
    assert "Users" not in rendered
    assert ".iac-code" not in rendered


@pytest.mark.asyncio
async def test_publish_pipeline_write_file_result_does_not_infer_artifact_from_tool_input(tmp_path: Path) -> None:
    store = A2AArtifactStore(tmp_path / "artifacts")
    publisher, queue = _publisher(tmp_path, artifact_store=store, exposure_types=[A2AExposureType.TOOL_TRACE])

    await publisher.publish(
        ToolUseEndEvent(
            tool_use_id="toolu-write",
            name="write_file",
            input={"path": str(tmp_path / "template.yaml"), "content": "ROSTemplate"},
        )
    )
    await publisher.publish(
        ToolResultEvent(
            tool_use_id="toolu-write",
            tool_name="write_file",
            result="Successfully wrote 1 lines to template.yaml",
        )
    )

    assert len(queue.events) == 1
    assert isinstance(queue.events[0], TaskStatusUpdateEvent)
    status = dump(queue.events[0])["metadata"]["iac_code"]["pipeline"]
    assert status["eventType"] == "tool_result"
    assert status["scope"] == "pipeline"
    assert status["data"]["toolUseId"] == "toolu-write"
    snapshot = publisher.snapshot_store.load()
    assert snapshot is not None
    assert snapshot["display"]["artifacts"] == []
    assert snapshot["display"]["toolResults"][0]["toolUseId"] == "toolu-write"


@pytest.mark.asyncio
async def test_publish_candidate_write_file_result_does_not_infer_artifact_from_tool_input(tmp_path: Path) -> None:
    store = A2AArtifactStore(tmp_path / "artifacts")
    publisher, queue = _publisher(tmp_path, artifact_store=store, exposure_types=[A2AExposureType.TOOL_TRACE])

    await publisher.publish(
        PipelineEvent(
            type=PipelineEventType.SUB_PIPELINE_STARTED,
            step_id=None,
            timestamp=1717821600.0,
            data={
                "sub_pipeline_id": "eval-abcd",
                "candidate_index": 0,
                "candidate_name": "candidate",
                "parent_step_id": "evaluate_candidates",
                "total_steps": 1,
            },
        )
    )
    await publisher.publish(
        PipelineEvent(
            type=PipelineEventType.SUB_STEP_STARTED,
            step_id="template_generating",
            timestamp=1717821601.0,
            data={
                "sub_pipeline_id": "eval-abcd",
                "candidate_index": 0,
                "step_id": "template_generating",
                "step_index": 0,
                "total_steps": 1,
            },
        )
    )
    await publisher.publish(
        SubPipelineStreamEvent(
            sub_pipeline_id="eval-abcd",
            candidate_index=0,
            inner=ToolUseEndEvent(
                tool_use_id="toolu-write",
                name="write_file",
                input={"path": str(tmp_path / "template.yaml"), "content": "ROSTemplate"},
            ),
        )
    )
    await publisher.publish(
        SubPipelineStreamEvent(
            sub_pipeline_id="eval-abcd",
            candidate_index=0,
            inner=ToolResultEvent(
                tool_use_id="toolu-write",
                tool_name="write_file",
                result="Successfully wrote 1 lines to template.yaml",
            ),
        )
    )

    assert not any(isinstance(event, TaskArtifactUpdateEvent) for event in queue.events)
    status = next(
        dump(event)["metadata"]["iac_code"]["pipeline"]
        for event in queue.events
        if dump(event).get("metadata", {}).get("iac_code", {}).get("pipeline", {}).get("eventType") == "tool_result"
    )
    assert status["scope"] == "candidate_step"
    assert status["candidateStep"]["id"] == "template_generating"
    assert status["data"]["toolUseId"] == "toolu-write"
    snapshot = publisher.snapshot_store.load()
    assert snapshot is not None
    assert snapshot["display"]["artifacts"] == []


@pytest.mark.asyncio
async def test_publish_candidate_step_completed_externalizes_configured_conclusion_artifact(tmp_path: Path) -> None:
    store = A2AArtifactStore(tmp_path / "artifacts")
    publisher, queue = _publisher(
        tmp_path,
        artifact_store=store,
        exposure_types=[A2AExposureType.TOOL_TRACE],
        a2a_artifacts_by_step_id={
            "template_generating": [
                {
                    "path": "conclusion.file_path",
                    "content": "conclusion.template",
                    "media_type": "auto",
                }
            ]
        },
    )

    await publisher.publish(
        PipelineEvent(
            type=PipelineEventType.SUB_PIPELINE_STARTED,
            step_id=None,
            timestamp=1717821600.0,
            data={
                "sub_pipeline_id": "eval-abcd",
                "candidate_index": 0,
                "candidate_name": "candidate",
                "parent_step_id": "evaluate_candidates",
                "total_steps": 1,
            },
        )
    )
    await publisher.publish(
        PipelineEvent(
            type=PipelineEventType.SUB_STEP_COMPLETED,
            step_id="template_generating",
            timestamp=1717821601.0,
            data={
                "sub_pipeline_id": "eval-abcd",
                "candidate_index": 0,
                "step_id": "template_generating",
                "step_index": 0,
                "total_steps": 1,
                "conclusion": {
                    "file_path": "templates/1-vpc-vswitch.yml",
                    "template": "ROSTemplateFormatVersion: '2015-09-01'\nResources: {}\n",
                },
            },
        )
    )

    artifact_event = next(event for event in queue.events if isinstance(event, TaskArtifactUpdateEvent))
    artifact_update = dump(artifact_event)
    assert artifact_update["artifact"]["name"] == "1-vpc-vswitch.yml"
    status_events = [
        dump(event)["metadata"]["iac_code"]["pipeline"]
        for event in queue.events
        if dump(event).get("metadata", {}).get("iac_code", {}).get("pipeline", {}).get("eventType")
    ]
    assert [event["eventType"] for event in status_events[-2:]] == [
        "candidate_step_completed",
        "artifact_created",
    ]
    artifact_status = status_events[-1]
    assert artifact_status["scope"] == "candidate_step"
    assert artifact_status["candidateStep"]["id"] == "template_generating"
    assert artifact_status["data"]["filename"] == "1-vpc-vswitch.yml"
    assert artifact_status["data"]["mediaType"] == "text/yaml"
    assert "ROSTemplateFormatVersion" not in str(artifact_status)
    snapshot = publisher.snapshot_store.load()
    assert snapshot is not None
    assert snapshot["display"]["artifacts"][0]["filename"] == "1-vpc-vswitch.yml"
    assert snapshot["display"]["artifacts"][0]["mediaType"] == "text/yaml"


@pytest.mark.asyncio
async def test_publish_externalized_conclusion_artifact_preserves_final_metadata(tmp_path: Path) -> None:
    store = A2AArtifactStore(tmp_path / "artifacts")
    raw_superseded_path = str(tmp_path / "generated" / "template.yaml")
    supersedes_key = fingerprint_text(raw_superseded_path)
    publisher, queue = _publisher(
        tmp_path,
        artifact_store=store,
        exposure_types=[A2AExposureType.TOOL_TRACE],
        a2a_artifacts_by_step_id={
            "template_generating": [
                {
                    "path": "conclusion.file_path",
                    "content": "conclusion.template",
                    "media_type": "auto",
                    "role": "final",
                    "supersedes_path": "conclusion.original_file_path",
                }
            ]
        },
    )

    await publisher.publish(
        PipelineEvent(
            type=PipelineEventType.SUB_PIPELINE_STARTED,
            step_id=None,
            timestamp=1717821600.0,
            data={
                "sub_pipeline_id": "eval-abcd",
                "candidate_index": 0,
                "candidate_name": "candidate",
                "parent_step_id": "evaluate_candidates",
                "total_steps": 1,
            },
        )
    )
    await publisher.publish(
        PipelineEvent(
            type=PipelineEventType.SUB_STEP_COMPLETED,
            step_id="template_generating",
            timestamp=1717821601.0,
            data={
                "sub_pipeline_id": "eval-abcd",
                "candidate_index": 0,
                "step_id": "template_generating",
                "step_index": 0,
                "total_steps": 1,
                "conclusion": {
                    "file_path": "templates/reviewed-template.yaml",
                    "original_file_path": raw_superseded_path,
                    "template": "ROSTemplateFormatVersion: '2015-09-01'\nResources: {}\n",
                },
            },
        )
    )

    status_events = [
        dump(event)["metadata"]["iac_code"]["pipeline"]
        for event in queue.events
        if dump(event).get("metadata", {}).get("iac_code", {}).get("pipeline", {}).get("eventType")
    ]
    artifact_status = status_events[-1]
    assert artifact_status["eventType"] == "artifact_created"
    assert artifact_status["artifact"]["role"] == "final"
    assert artifact_status["artifact"]["supersedesPath"] == "[PATH]"
    assert artifact_status["artifact"]["supersedesKey"] == supersedes_key
    assert artifact_status["data"]["role"] == "final"
    assert artifact_status["data"]["supersedesPath"] == "[PATH]"
    assert artifact_status["data"]["supersedesKey"] == supersedes_key

    journal_event = publisher.journal.read_all()[-1]
    assert journal_event["artifact"]["role"] == "final"
    assert journal_event["artifact"]["supersedesPath"] == "[PATH]"
    assert journal_event["artifact"]["supersedesKey"] == supersedes_key
    assert journal_event["data"]["role"] == "final"
    assert journal_event["data"]["supersedesPath"] == "[PATH]"
    assert journal_event["data"]["supersedesKey"] == supersedes_key

    snapshot = publisher.snapshot_store.load()
    assert snapshot is not None
    artifact = snapshot["display"]["artifacts"][0]
    assert artifact["role"] == "final"
    assert artifact["supersedesPath"] == "[PATH]"
    assert artifact["supersedesKey"] == supersedes_key
    artifact_metadata = (
        artifact_status["artifact"],
        artifact_status["data"],
        journal_event["artifact"],
        journal_event["data"],
        artifact,
    )
    assert raw_superseded_path not in str(artifact_metadata)


@pytest.mark.asyncio
async def test_publish_artifact_created_omits_tool_metadata_when_tool_trace_disabled(tmp_path: Path) -> None:
    store = A2AArtifactStore(tmp_path / "artifacts")
    publisher, queue = _publisher(tmp_path, artifact_store=store, exposure_types=[])

    await publisher.publish(
        ToolResultEvent(
            tool_use_id="toolu-private",
            tool_name="write_file",
            result={"artifact": {"filename": "template.yaml", "mediaType": "text/yaml", "content": "ROSTemplate"}},
            is_error=True,
        )
    )

    assert isinstance(queue.events[0], TaskArtifactUpdateEvent)
    status = dump(queue.events[1])["metadata"]["iac_code"]["pipeline"]
    assert status["eventType"] == "artifact_created"
    assert status["data"] == {
        "artifactId": status["artifact"]["artifactId"],
        "filename": "template.yaml",
        "mediaType": "text/yaml",
        "byteSize": status["artifact"]["byteSize"],
        "sha256": status["artifact"]["sha256"],
        "uri": status["artifact"]["uri"],
    }
    journal_event = publisher.journal.read_all()[0]
    snapshot = publisher.snapshot_store.load()
    assert snapshot is not None
    for value in (status, journal_event, snapshot):
        rendered = str(value)
        assert "write_file" not in rendered
        assert "toolu-private" not in rendered
        assert "isError" not in rendered


@pytest.mark.asyncio
async def test_publish_does_not_emit_artifact_update_when_sequence_high_water_is_unreadable(tmp_path: Path) -> None:
    store = A2AArtifactStore(tmp_path / "artifacts")
    publisher, queue = _publisher(tmp_path, artifact_store=store, exposure_types=[A2AExposureType.TOOL_TRACE])

    def fail_read_all() -> list[dict[str, Any]]:
        raise OSError("read failed")

    publisher.journal.read_all_repairing_tail = fail_read_all  # type: ignore[method-assign]

    await publisher.publish(
        ToolResultEvent(
            tool_use_id="toolu-write",
            tool_name="write_file",
            result={"artifact": {"filename": "template.yaml", "mediaType": "text/yaml", "content": "ROSTemplate"}},
        )
    )

    assert queue.events == []
    assert publisher.snapshot_store.load() is None


@pytest.mark.asyncio
async def test_publish_does_not_emit_artifact_update_when_artifact_metadata_is_not_durable(tmp_path: Path) -> None:
    store = A2AArtifactStore(tmp_path / "artifacts")
    publisher, queue = _publisher(tmp_path, artifact_store=store, exposure_types=[A2AExposureType.TOOL_TRACE])

    def fail_append(_event: dict[str, Any], durable: bool = False) -> None:
        raise OSError("append failed")

    def fail_save(_snapshot: dict[str, Any]) -> None:
        raise OSError("snapshot failed")

    publisher.journal.append = fail_append  # type: ignore[method-assign]
    publisher.snapshot_store.save = fail_save  # type: ignore[method-assign]

    await publisher.publish(
        ToolResultEvent(
            tool_use_id="toolu-write",
            tool_name="write_file",
            result={"artifact": {"filename": "template.yaml", "mediaType": "text/yaml", "content": "ROSTemplate"}},
        )
    )

    assert queue.events == []


@pytest.mark.asyncio
async def test_publish_candidate_restart_has_stable_coordinates_and_snapshot_control(tmp_path: Path) -> None:
    publisher, _queue = _publisher(tmp_path)
    await publisher.publish(
        PipelineEvent(
            type=PipelineEventType.STEP_STARTED,
            step_id="evaluate_candidates",
            timestamp=1717821600.0,
            data={"index": 1, "total": 1},
        )
    )
    await publisher.publish(
        PipelineEvent(
            type=PipelineEventType.SUB_PIPELINE_STARTED,
            step_id=None,
            timestamp=1717821601.0,
            data={
                "sub_pipeline_id": "eval-abcd",
                "candidate_index": 0,
                "candidate_name": "low cost",
                "parent_step_id": "evaluate_candidates",
                "total_steps": 1,
            },
        )
    )

    await publisher.publish_interrupt(
        prompt="make it cheaper",
        verdict=SimpleNamespace(
            action="hard_interrupt",
            reason="user changed price target",
            rollback_target="template_generating",
            candidate_scope="candidate:0",
        ),
        parent_rollback=False,
    )

    restart = publisher.journal.read_all()[-1]
    assert restart["eventType"] == "candidate_restart_requested"
    assert restart["step"]["runId"] == "step-evaluate_candidates-1"
    assert restart["candidate"]["runId"] == "candidate-eval-abcd-0-1"
    assert restart["data"]["targetCandidateStepId"] == "template_generating"
    assert restart["data"]["nextCandidateAttempt"] == 2
    snapshot = publisher.snapshot_store.load()
    assert snapshot is not None
    candidate = snapshot["steps"][0]["candidates"][0]
    assert candidate["status"] == "restarting"
    assert snapshot["control"]["candidateRestarts"][0]["nextCandidateAttempt"] == 2


@pytest.mark.asyncio
async def test_publish_candidate_restart_targets_latest_matching_candidate_only(tmp_path: Path) -> None:
    publisher, _queue = _publisher(tmp_path)
    await publisher.publish(
        PipelineEvent(
            type=PipelineEventType.STEP_STARTED,
            step_id="evaluate_candidates",
            timestamp=1717821600.0,
            data={"index": 1, "total": 1},
        )
    )
    await publisher.publish(
        PipelineEvent(
            type=PipelineEventType.SUB_PIPELINE_STARTED,
            step_id=None,
            timestamp=1717821601.0,
            data={
                "sub_pipeline_id": "eval-old",
                "candidate_index": 0,
                "candidate_name": "old",
                "parent_step_id": "evaluate_candidates",
            },
        )
    )
    await publisher.publish(
        PipelineEvent(
            type=PipelineEventType.SUB_PIPELINE_COMPLETED,
            step_id=None,
            timestamp=1717821602.0,
            data={"sub_pipeline_id": "eval-old", "candidate_index": 0, "failed": False},
        )
    )
    await publisher.publish_interrupt(
        prompt="redo candidates",
        verdict=SimpleNamespace(
            action="hard_interrupt",
            reason="parent retry",
            rollback_target="evaluate_candidates",
            candidate_scope=None,
        ),
        parent_rollback=True,
    )
    await publisher.publish(
        PipelineEvent(
            type=PipelineEventType.SUB_PIPELINE_STARTED,
            step_id=None,
            timestamp=1717821603.0,
            data={
                "sub_pipeline_id": "eval-new",
                "candidate_index": 0,
                "candidate_name": "new",
                "parent_step_id": "evaluate_candidates",
            },
        )
    )

    await publisher.publish_interrupt(
        prompt="only restart current candidate",
        verdict=SimpleNamespace(
            action="hard_interrupt",
            reason="candidate retry",
            rollback_target="template_generating",
            candidate_scope="candidate:0",
        ),
        parent_rollback=False,
    )

    restart_events = [
        event for event in publisher.journal.read_all() if event["eventType"] == "candidate_restart_requested"
    ]
    assert len(restart_events) == 1
    assert restart_events[0]["candidate"]["runId"] == "candidate-eval-new-0-1"
    snapshot = publisher.snapshot_store.load()
    assert snapshot is not None
    candidates = {candidate["runId"]: candidate for step in snapshot["steps"] for candidate in step["candidates"]}
    assert candidates["candidate-eval-old-0-1"]["status"] == "completed"
    assert candidates["candidate-eval-new-0-1"]["status"] == "restarting"


@pytest.mark.asyncio
async def test_publish_candidate_failure_keeps_a2a_task_working(tmp_path: Path) -> None:
    publisher, queue = _publisher(tmp_path)
    await publisher.publish(
        PipelineEvent(
            type=PipelineEventType.STEP_STARTED,
            step_id="evaluate_candidates",
            timestamp=1717821600.0,
            data={"index": 1, "total": 1},
        )
    )
    await publisher.publish(
        PipelineEvent(
            type=PipelineEventType.SUB_PIPELINE_STARTED,
            step_id=None,
            timestamp=1717821601.0,
            data={
                "sub_pipeline_id": "eval-failed",
                "candidate_index": 0,
                "candidate_name": "failed",
                "parent_step_id": "evaluate_candidates",
            },
        )
    )

    await publisher.publish(
        PipelineEvent(
            type=PipelineEventType.SUB_PIPELINE_COMPLETED,
            step_id=None,
            timestamp=1717821602.0,
            data={"sub_pipeline_id": "eval-failed", "candidate_index": 0, "failed": True},
        )
    )

    assert dump(queue.events[-1])["status"]["state"] == "TASK_STATE_WORKING"
    snapshot = publisher.snapshot_store.load()
    assert snapshot is not None
    assert snapshot["status"] == "working"
    assert snapshot["steps"][0]["candidates"][0]["status"] == "failed"


@pytest.mark.asyncio
async def test_publish_continues_when_pipeline_persistence_fails(tmp_path: Path) -> None:
    publisher, queue = _publisher(tmp_path)

    def fail_append(_event: dict[str, Any], durable: bool = False) -> None:
        raise OSError("disk full")

    publisher.journal.append = fail_append  # type: ignore[method-assign]

    returned = await publisher.publish(TextDeltaEvent(text="still streams"))

    assert returned == "still streams"
    assert dump(queue.events[0])["metadata"]["iac_code"]["pipeline"]["data"]["text"] == "still streams"


@pytest.mark.asyncio
async def test_publish_append_failure_warning_does_not_include_journal_path(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    publisher, queue = _publisher(tmp_path)
    monkeypatch.setattr(publisher, "_ensure_monotonic_sequence", lambda _envelope: None)
    journal_path = tmp_path / "pipeline" / "a2a-events.jsonl"
    journal_path.parent.mkdir(parents=True, exist_ok=True)
    existing_event = _envelope("text_delta")
    existing_event["sequence"] = 1
    existing_event["data"] = {"text": "old"}
    after_corrupt_event = _envelope("text_delta")
    after_corrupt_event["eventId"] = "evt-after-corrupt"
    after_corrupt_event["sequence"] = 2
    after_corrupt_event["data"] = {"text": "after"}
    journal_path.write_text(
        json.dumps(existing_event, separators=(",", ":"))
        + "\n"
        + '{"eventId":"evt-corrupt"\n'
        + json.dumps(after_corrupt_event, separators=(",", ":"))
        + "\n",
        encoding="utf-8",
    )

    with caplog.at_level(logging.WARNING):
        returned = await publisher.publish(TextDeltaEvent(text="still streams"))

    assert returned == "still streams"
    assert dump(queue.events[0])["metadata"]["iac_code"]["pipeline"]["data"]["text"] == "still streams"
    messages = [record.getMessage() for record in caplog.records]
    assert any(
        "Failed to append A2A pipeline journal event error_type=A2APipelineJournalReadError" in message
        for message in messages
    )
    assert str(journal_path) not in "\n".join(messages)
    assert all(record.exc_info is None for record in caplog.records)


@pytest.mark.asyncio
async def test_publish_skips_pipeline_metadata_when_journal_read_fails_for_sequence_high_water(tmp_path: Path) -> None:
    publisher, queue = _publisher(tmp_path)

    def fail_read_all() -> list[dict[str, Any]]:
        raise OSError("read failed")

    publisher.journal.read_all_repairing_tail = fail_read_all  # type: ignore[method-assign]

    returned = await publisher.publish(TextDeltaEvent(text="still streams"))

    assert returned == "still streams"
    assert queue.events == []
    journal_path = tmp_path / "pipeline" / "a2a-events.jsonl"
    assert not journal_path.exists()


@pytest.mark.asyncio
async def test_publish_repairs_partial_tail_before_persisting_pipeline_metadata(tmp_path: Path) -> None:
    publisher, queue = _publisher(tmp_path)
    journal_path = tmp_path / "pipeline" / "a2a-events.jsonl"
    journal_path.parent.mkdir(parents=True, exist_ok=True)
    valid_event = _envelope("text_delta")
    valid_event["sequence"] = 1
    valid_event["data"] = {"text": "old"}
    journal_content = json.dumps(valid_event, separators=(",", ":")) + "\n"
    journal_content += '{"eventId":"evt-partial","sequence":2'
    journal_path.write_text(journal_content, encoding="utf-8")

    returned = await publisher.publish(TextDeltaEvent(text="still streams"))

    assert returned == "still streams"
    assert len(queue.events) == 1
    events = publisher.journal.read_all_strict()
    assert [event["sequence"] for event in events] == [1, 2]
    assert [event["data"]["text"] for event in events] == ["old", "still streams"]
    assert (tmp_path / "pipeline" / "a2a-events.jsonl.corrupt").read_text(encoding="utf-8") == (
        '{"eventId":"evt-partial","sequence":2\n'
    )
    snapshot = publisher.snapshot_store.load()
    assert snapshot is not None
    assert snapshot["display"]["messages"][0]["text"] == "oldstill streams"


@pytest.mark.asyncio
async def test_publish_does_not_advance_stale_snapshot_when_catch_up_read_fails(tmp_path: Path) -> None:
    publisher, _queue = _publisher(tmp_path)
    await publisher.publish(TextDeltaEvent(text="old"))
    stale_snapshot = publisher.snapshot_store.load()
    assert stale_snapshot is not None
    manual_event = _envelope("text_delta")
    manual_event["eventId"] = "evt-manual"
    manual_event["sequence"] = 2
    manual_event["data"] = {"text": "manual"}
    publisher.journal.append(manual_event)
    publisher.snapshot_store.save(stale_snapshot)
    fresh_publisher, _fresh_queue = _publisher(tmp_path)

    def fail_read_all() -> list[dict[str, Any]]:
        raise OSError("read failed")

    fresh_publisher.journal.read_all_repairing_tail = fail_read_all  # type: ignore[method-assign]

    await fresh_publisher.publish(TextDeltaEvent(text="new"))

    snapshot = fresh_publisher.snapshot_store.load()
    assert snapshot is not None
    assert snapshot["lastSequence"] == 1
    assert snapshot["display"]["messages"][0]["text"] == "old"


@pytest.mark.asyncio
async def test_publish_rebuilds_missing_snapshot_with_current_event_when_journal_append_fails(tmp_path: Path) -> None:
    publisher, _queue = _publisher(tmp_path)
    await publisher.publish(TextDeltaEvent(text="old"))
    publisher.snapshot_store.path.unlink()

    def fail_append(_event: dict[str, Any], durable: bool = False) -> None:
        raise OSError("disk full")

    publisher.journal.append = fail_append  # type: ignore[method-assign]

    await publisher.publish(TextDeltaEvent(text=" new"))

    snapshot = publisher.snapshot_store.load()
    assert snapshot is not None
    assert snapshot["display"]["messages"][0]["text"] == "old new"


@pytest.mark.asyncio
async def test_publish_manual_can_require_journal_metadata(tmp_path: Path) -> None:
    publisher, queue = _publisher(tmp_path)

    def fail_append(_event: dict[str, Any], durable: bool = False) -> None:
        raise OSError("journal locked")

    publisher.journal.append = fail_append  # type: ignore[method-assign]

    result = await publisher.publish_manual(
        "backup_blocked",
        "pipeline",
        status="input_required",
        data={"reason": "terminal"},
        require_durable_metadata=True,
        require_journal_metadata=True,
    )

    assert result is None
    assert queue.events == []
    assert publisher.snapshot_store.load() is not None


@pytest.mark.asyncio
async def test_publish_sequence_uses_journal_when_snapshot_is_stale(tmp_path: Path) -> None:
    publisher, _queue = _publisher(tmp_path)
    await publisher.publish(TextDeltaEvent(text="old"))
    stale_snapshot = publisher.snapshot_store.load()
    assert stale_snapshot is not None
    manual_event = _envelope("text_delta")
    manual_event["eventId"] = "evt-manual"
    manual_event["sequence"] = 2
    manual_event["data"] = {"text": "manual"}
    publisher.journal.append(manual_event)
    publisher.snapshot_store.save(stale_snapshot)
    fresh_publisher, _fresh_queue = _publisher(tmp_path)

    await fresh_publisher.publish(TextDeltaEvent(text="new"))

    events = fresh_publisher.journal.read_all()
    assert [event["sequence"] for event in events] == [1, 2, 3]
    assert events[-1]["data"]["text"] == "new"
    snapshot = fresh_publisher.snapshot_store.load()
    assert snapshot is not None
    assert snapshot["lastSequence"] == 3
    assert snapshot["display"]["messages"][0]["text"] == "oldmanualnew"


@pytest.mark.asyncio
async def test_publish_permission_does_not_overwrite_completed_future(tmp_path: Path) -> None:
    publisher, queue = _publisher(tmp_path)
    future: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
    future.set_result(True)

    await publisher.publish(
        PermissionRequestEvent(
            tool_name="bash",
            tool_input={"cmd": "pwd"},
            tool_use_id="toolu-done",
            response_future=future,
        ),
        auto_approve_permissions=False,
    )

    assert future.result() is True
    permission = dump(queue.events[0])["metadata"]["iac_code"]["pipeline"]["permission"]
    assert permission["approved"] is False
    assert permission["decision"] == "deny"


@pytest.mark.asyncio
async def test_publish_input_required_maps_to_a2a_input_required(tmp_path: Path) -> None:
    publisher, queue = _publisher(tmp_path)

    await publisher.publish(
        PipelineEvent(
            type=PipelineEventType.USER_INPUT_REQUIRED,
            step_id="confirm_and_select",
            timestamp=1717821600.0,
            data={"prompt": "Choose one", "kind": "choice"},
        )
    )

    dumped = dump(queue.events[0])
    assert dumped["status"]["state"] == "TASK_STATE_INPUT_REQUIRED"
    assert dumped["metadata"]["iac_code"]["pipeline"]["eventType"] == "input_required"
    snapshot = publisher.snapshot_store.load()
    assert snapshot is not None
    assert snapshot["status"] == "waiting_input"


@pytest.mark.asyncio
async def test_publish_mcp_status_does_not_clear_pending_input_snapshot(tmp_path: Path) -> None:
    publisher, _queue = _publisher(tmp_path)
    await publisher.publish(
        PipelineEvent(
            type=PipelineEventType.USER_INPUT_REQUIRED,
            step_id="confirm_and_select",
            timestamp=1717821600.0,
            data={"prompt": "Choose one", "kind": "choice"},
        )
    )
    before = publisher.snapshot_store.load()
    assert before is not None
    assert before["status"] == "waiting_input"
    assert before["pendingInput"] is not None

    await publisher.publish(
        PipelineEvent(
            type=PipelineEventType.MCP_STATUS,
            step_id=None,
            timestamp=1717821601.0,
            data={
                "kind": "mcp_status",
                "mcp_status": {"servers": [{"serverName": "remote", "state": "connected"}], "warnings": []},
            },
        )
    )

    after = publisher.snapshot_store.load()
    assert after is not None
    assert after["status"] == "waiting_input"
    assert after["pendingInput"] == before["pendingInput"]


@pytest.mark.asyncio
async def test_publish_ask_user_question_maps_to_input_required_snapshot(tmp_path: Path) -> None:
    publisher, queue = _publisher(tmp_path)
    await publisher.publish(
        PipelineEvent(
            type=PipelineEventType.STEP_STARTED,
            step_id="intent_parsing",
            timestamp=1717821599.0,
            data={"index": 1, "total": 2},
        )
    )
    future: asyncio.Future[dict[str, str] | None] = asyncio.get_running_loop().create_future()

    await publisher.publish(
        AskUserQuestionEvent(
            tool_use_id="ask-1",
            question="请选择部署目标",
            options=[{"id": "nginx", "label": "Nginx 网站"}],
            allow_free_text=True,
            free_text_prompt="也可以直接描述你的目标",
            response_future=future,
        )
    )

    dumped = dump(queue.events[-1])
    envelope = dumped["metadata"]["iac_code"]["pipeline"]
    assert dumped["status"]["state"] == "TASK_STATE_INPUT_REQUIRED"
    assert envelope["eventType"] == "input_required"
    assert envelope["scope"] == "step"
    assert envelope["step"]["id"] == "intent_parsing"
    assert envelope["input"] == {
        "inputId": "ask-ask-1",
        "kind": "ask_user_question",
        "toolUseId": "ask-1",
        "question": "请选择部署目标",
        "prompt": "请选择部署目标",
        "required": True,
        "options": [{"id": "nginx", "label": "Nginx 网站"}],
        "allowFreeText": True,
        "freeTextPrompt": "也可以直接描述你的目标",
    }
    assert not future.done()

    snapshot = publisher.snapshot_store.load()
    assert snapshot is not None
    assert snapshot["status"] == "waiting_input"
    assert snapshot["pendingInput"]["kind"] == "ask_user_question"
    assert snapshot["pendingInput"]["toolUseId"] == "ask-1"
    assert snapshot["pendingInput"]["question"] == "请选择部署目标"


@pytest.mark.asyncio
async def test_pipeline_input_received_is_not_enqueued_when_metadata_persistence_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    publisher, queue = _publisher(tmp_path)

    def fail_append(_event: dict[str, Any], durable: bool = False) -> None:
        raise OSError("journal locked")

    monkeypatch.setattr(publisher.journal, "append", fail_append)
    monkeypatch.setattr(publisher.snapshot_store, "save", lambda _snapshot: False)

    result = await publisher.publish_manual("input_received", "pipeline")

    assert result is None
    assert queue.events == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failed", "expected_state"),
    [
        (False, "TASK_STATE_COMPLETED"),
        (True, "TASK_STATE_FAILED"),
    ],
)
async def test_publish_pipeline_completed_maps_terminal_states(
    tmp_path: Path,
    failed: bool,
    expected_state: str,
) -> None:
    publisher, queue = _publisher(tmp_path)

    await publisher.publish(
        PipelineEvent(
            type=PipelineEventType.PIPELINE_COMPLETED,
            step_id=None,
            timestamp=1717821600.0,
            data={"failed": failed},
        )
    )

    assert dump(queue.events[0])["status"]["state"] == expected_state


@pytest.mark.asyncio
async def test_publish_pipeline_canceled_envelope_maps_to_canceled_state(tmp_path: Path) -> None:
    class StaticTranslator:
        def translate(self, _event: Any) -> list[dict[str, Any]]:
            return [_envelope("pipeline_canceled", status="canceled")]

    queue = FakeEventQueue()
    pipeline_dir = tmp_path / "pipeline"
    publisher = PipelineA2AEventPublisher(
        event_queue=queue,
        translator=StaticTranslator(),  # type: ignore[arg-type]
        journal=A2APipelineJournal(pipeline_dir),
        snapshot_store=A2APipelineSnapshotStore(pipeline_dir),
    )

    await publisher.publish(object())

    assert dump(queue.events[0])["status"]["state"] == "TASK_STATE_CANCELED"


@pytest.mark.asyncio
async def test_publish_parent_hard_interrupt_rolls_forward_target_step_attempt(tmp_path: Path) -> None:
    publisher, _queue = _publisher(tmp_path)

    await publisher.publish_interrupt(
        prompt="change the architecture",
        verdict=SimpleNamespace(
            action="hard_interrupt",
            reason="changed parent plan",
            rollback_target="confirm_and_select",
            candidate_scope=None,
        ),
    )
    await publisher.publish(
        PipelineEvent(
            type=PipelineEventType.STEP_STARTED,
            step_id="confirm_and_select",
            timestamp=1717821602.0,
            data={"index": 2, "total": 2},
        )
    )

    rollback = publisher.journal.read_all()[-2]
    restarted_step = publisher.journal.read_all()[-1]
    assert rollback["eventType"] == "rollback_completed"
    assert rollback["step"]["id"] == "confirm_and_select"
    assert rollback["step"]["runId"] == "step-confirm_and_select-2"
    assert rollback["step"]["attempt"] == 2
    assert restarted_step["eventType"] == "step_started"
    assert restarted_step["step"]["runId"] == "step-confirm_and_select-2"
    assert restarted_step["step"]["attempt"] == 2


@pytest.mark.asyncio
async def test_publish_hard_interrupt_false_parent_without_candidate_does_not_emit_rollback(tmp_path: Path) -> None:
    publisher, _queue = _publisher(tmp_path)

    await publisher.publish_interrupt(
        prompt="invalid rollback",
        verdict=SimpleNamespace(
            action="hard_interrupt",
            reason="fallback rejected",
            rollback_target="confirm_and_select",
            candidate_scope=None,
        ),
        parent_rollback=False,
    )

    event_types = [event["eventType"] for event in publisher.journal.read_all()]
    assert event_types == ["interrupt_received", "interrupt_classified"]
