from __future__ import annotations

import json

import pytest

from iac_code.a2a.metrics import NoOpA2AMetrics
from iac_code.a2a.persistence import A2AContextSnapshot, A2APersistenceStore, A2ATaskSnapshot
from iac_code.a2a.pipeline_journal import A2APipelineJournal, A2APipelineJournalReadError
from iac_code.a2a.pipeline_recovery import A2APipelineRecoveryService
from iac_code.a2a.pipeline_snapshot import SNAPSHOT_SCHEMA_VERSION, A2APipelineSnapshotStore, reduce_pipeline_events
from iac_code.a2a.task_store import A2ATaskStore
from iac_code.services.session_storage import SessionStorage


def _event(sequence: int, event_id: str) -> dict:
    return {
        "schemaVersion": "1.0",
        "eventId": event_id,
        "sequence": sequence,
        "eventType": "pipeline_started",
        "scope": "pipeline",
        "pipelineRunId": "ctx-1",
        "taskId": "task-1",
        "contextId": "ctx-1",
        "pipelineName": "selling",
        "status": "working",
        "data": {},
    }


def _event_for_task(sequence: int, event_id: str, *, task_id: str, context_id: str = "ctx-1") -> dict:
    event = _event(sequence, event_id)
    event["taskId"] = task_id
    event["contextId"] = context_id
    event["pipelineRunId"] = context_id
    return event


@pytest.mark.asyncio
async def test_recovery_returns_snapshot_and_replay_events(tmp_path) -> None:
    persistence = A2APersistenceStore(tmp_path / "a2a")
    store = A2ATaskStore(metrics=NoOpA2AMetrics(), persistence=persistence)
    await store.get_or_create_context(
        context_id="ctx-1",
        cwd=str(tmp_path),
        runtime_factory=lambda session_id: object(),
    )
    context = await store.get_context_record("ctx-1")
    pipeline_dir = SessionStorage().session_dir(str(tmp_path), context.session_id) / "pipeline"
    journal = A2APipelineJournal(pipeline_dir)
    journal.append(_event(1, "evt-1"))
    journal.append(_event(2, "evt-2"))
    A2APipelineSnapshotStore(pipeline_dir).save(reduce_pipeline_events([_event(1, "evt-1")]))

    service = A2APipelineRecoveryService(task_store=store)
    state = await service.get_state(context_id="ctx-1", after_sequence=1)

    assert state["snapshot"]["lastSequence"] == 1
    assert [event["eventId"] for event in state["events"]] == ["evt-2"]


@pytest.mark.asyncio
async def test_recovery_keeps_pipeline_warning_visible_after_snapshot_sequence(tmp_path) -> None:
    persistence = A2APersistenceStore(tmp_path / "a2a")
    store = A2ATaskStore(metrics=NoOpA2AMetrics(), persistence=persistence)
    await store.get_or_create_context(
        context_id="ctx-1",
        cwd=str(tmp_path),
        runtime_factory=lambda session_id: object(),
    )
    context = await store.get_context_record("ctx-1")
    pipeline_dir = SessionStorage().session_dir(str(tmp_path), context.session_id) / "pipeline"
    started = _event(1, "evt-1")
    warning = _event(2, "evt-warning")
    warning["eventType"] = "pipeline_warning"
    warning["data"] = {
        "reason": "cleanup_tracking_unavailable",
        "operation": "record_observed",
        "ledger_path": "/Users/alice/.iac-code/projects/demo/cleanup.yaml",
        "load_error": "while parsing /Users/alice/.iac-code/projects/demo/cleanup.yaml",
    }
    journal = A2APipelineJournal(pipeline_dir)
    journal.append(started)
    journal.append(warning)
    A2APipelineSnapshotStore(pipeline_dir).save(reduce_pipeline_events([started, warning]))

    service = A2APipelineRecoveryService(task_store=store)
    state = await service.get_state(context_id="ctx-1")

    assert state["events"] == []
    assert state["snapshot"]["lastSequence"] == 2
    assert state["snapshot"]["control"]["warningHistory"][0]["eventId"] == "evt-warning"
    assert state["snapshot"]["control"]["warningHistory"][0]["data"]["reason"] == "cleanup_tracking_unavailable"
    assert "ledger_path" not in state["snapshot"]["control"]["warningHistory"][0]["data"]
    assert "load_error" not in state["snapshot"]["control"]["warningHistory"][0]["data"]

    replay_state = await service.get_state(context_id="ctx-1", after_sequence=1)

    assert replay_state["events"][0]["eventType"] == "pipeline_warning"
    assert replay_state["events"][0]["data"]["reason"] == "cleanup_tracking_unavailable"
    assert "ledger_path" not in replay_state["events"][0]["data"]
    assert "load_error" not in replay_state["events"][0]["data"]


@pytest.mark.asyncio
async def test_recovery_sanitizes_legacy_artifact_file_uris_from_snapshot_and_replay(tmp_path) -> None:
    persistence = A2APersistenceStore(tmp_path / "a2a")
    store = A2ATaskStore(metrics=NoOpA2AMetrics(), persistence=persistence)
    await store.get_or_create_context(
        context_id="ctx-1",
        cwd=str(tmp_path),
        runtime_factory=lambda session_id: object(),
    )
    context = await store.get_context_record("ctx-1")
    pipeline_dir = SessionStorage().session_dir(str(tmp_path), context.session_id) / "pipeline"
    legacy_uri = "file:///Users/alice/.iac-code/projects/demo/template.yaml"
    legacy_windows_path = r"C:\Users\alice\.iac-code\projects\demo\template.yaml"
    artifact = _event(1, "evt-artifact")
    artifact["eventType"] = "artifact_created"
    legacy_artifact = {
        "artifactId": "artifact-1",
        "filename": legacy_windows_path,
        "uri": [legacy_uri],
        "encodedOwnerUrl": "iac-code-artifact://C%3A%5CUsers%5Calice%5C.iac-code%5Cprojects%5Cdemo/template.yaml",
        "source": legacy_uri,
        "metadata": {"uri": [legacy_uri], "byteSize": 10},
        "parts": [{"url": [legacy_uri], "metadata": {"uri": [legacy_uri]}}],
    }
    artifact["artifact"] = legacy_artifact.copy()
    artifact["data"] = legacy_artifact.copy()
    tool_result = _event(2, "evt-tool")
    tool_result["eventType"] = "tool_result"
    tool_result["data"] = {
        "toolName": "write_file",
        "toolUseId": "toolu-1",
        "result": {
            "artifact": [
                legacy_uri,
                {
                    "filename": legacy_windows_path,
                    "uri": [legacy_uri],
                    "encodedOwnerUrl": (
                        "iac-code-artifact://C%3A%5CUsers%5Calice%5C.iac-code%5Cprojects%5Cdemo/template.yaml"
                    ),
                    "source": legacy_uri,
                    "metadata": {"uri": [legacy_uri]},
                    "parts": [legacy_uri, {"url": [legacy_uri], "metadata": {"uri": [legacy_uri]}}],
                },
            ]
        },
    }
    artifact_list = _event(3, "evt-artifact-list")
    artifact_list["eventType"] = "artifact_created"
    artifact_list["artifact"] = [legacy_uri, {"uri": [legacy_uri], "filename": legacy_windows_path}]
    artifact_list["data"] = [legacy_uri]
    journal = A2APipelineJournal(pipeline_dir)
    journal.append(artifact)
    journal.append(tool_result)
    journal.append(artifact_list)
    legacy_snapshot = reduce_pipeline_events([artifact, tool_result, artifact_list])
    legacy_snapshot["display"]["artifacts"][0]["uri"] = legacy_uri
    legacy_snapshot["display"]["artifacts"][0]["parts"] = [{"url": legacy_uri, "metadata": {"uri": legacy_uri}}]
    legacy_snapshot["display"]["toolResults"][0]["result"]["artifact"][1]["uri"] = legacy_uri
    legacy_snapshot["display"]["toolResults"][0]["result"]["artifact"][1]["parts"] = [
        {"url": legacy_uri, "metadata": {"uri": legacy_uri}}
    ]
    A2APipelineSnapshotStore(pipeline_dir).save(legacy_snapshot)

    service = A2APipelineRecoveryService(task_store=store)
    state = await service.get_state(context_id="ctx-1", after_sequence=0)

    assert state["snapshot"]["display"]["artifacts"][0]["artifactId"] == "artifact-1"
    assert state["snapshot"]["display"]["artifacts"][0]["filename"] == "template.yaml"
    assert "uri" not in state["snapshot"]["display"]["artifacts"][0]
    assert "encodedOwnerUrl" not in state["snapshot"]["display"]["artifacts"][0]
    assert state["snapshot"]["display"]["toolResults"][0]["result"]["artifact"][0] == "[PATH]"
    replay_artifact = state["snapshot"]["display"]["toolResults"][0]["result"]["artifact"][1]
    assert "uri" not in replay_artifact
    assert replay_artifact["filename"] == "template.yaml"
    assert replay_artifact["source"] == "[PATH]"
    assert "url" not in state["snapshot"]["display"]["artifacts"][0]["parts"][0]
    assert "url" not in replay_artifact["parts"][0]
    assert "uri" not in state["events"][0]["artifact"]
    assert "encodedOwnerUrl" not in state["events"][0]["artifact"]
    assert state["events"][0]["artifact"]["filename"] == "template.yaml"
    assert "uri" not in state["events"][0]["data"]
    assert state["events"][1]["data"]["result"]["artifact"][0] == "[PATH]"
    assert "uri" not in state["events"][1]["data"]["result"]["artifact"][1]
    assert state["events"][2]["artifact"][0] == "[PATH]"
    assert "uri" not in state["events"][2]["artifact"][1]
    assert state["events"][2]["data"][0] == "[PATH]"
    rendered = json.dumps(state, ensure_ascii=False)
    assert "file://" not in rendered
    assert ".iac-code" not in rendered


@pytest.mark.asyncio
async def test_recovery_rebuilds_stale_schema_snapshot_from_journal(tmp_path) -> None:
    persistence = A2APersistenceStore(tmp_path / "a2a")
    store = A2ATaskStore(metrics=NoOpA2AMetrics(), persistence=persistence)
    await store.get_or_create_context(
        context_id="ctx-1",
        cwd=str(tmp_path),
        runtime_factory=lambda session_id: object(),
    )
    context = await store.get_context_record("ctx-1")
    pipeline_dir = SessionStorage().session_dir(str(tmp_path), context.session_id) / "pipeline"
    completed = _event(1, "evt-completed")
    completed["eventType"] = "step_completed"
    completed["scope"] = "step"
    completed["step"] = {"runId": "step-intent-1", "id": "intent", "index": 1, "total": 1, "attempt": 1}
    completed["data"] = {"conclusionField": "intent", "conclusion": {"summary": "deploy nginx"}}
    journal = A2APipelineJournal(pipeline_dir)
    journal.append(completed)
    stale_snapshot = reduce_pipeline_events([completed])
    stale_snapshot["schemaVersion"] = "1.0"
    stale_snapshot["steps"][0].pop("conclusion", None)
    stale_snapshot["steps"][0].pop("conclusionField", None)
    snapshot_store = A2APipelineSnapshotStore(pipeline_dir)
    snapshot_store.save(stale_snapshot)

    service = A2APipelineRecoveryService(task_store=store)
    state = await service.get_state(context_id="ctx-1")

    assert state["snapshot"]["schemaVersion"] == SNAPSHOT_SCHEMA_VERSION
    assert state["snapshot"]["steps"][0]["conclusionField"] == "intent"
    assert state["snapshot"]["steps"][0]["conclusion"] == {"summary": "deploy nginx"}
    assert snapshot_store.load()["schemaVersion"] == SNAPSHOT_SCHEMA_VERSION


@pytest.mark.asyncio
async def test_recovery_rejects_unrepairable_middle_journal_corruption_without_saving_snapshot(tmp_path) -> None:
    persistence = A2APersistenceStore(tmp_path / "a2a")
    persistence.save_context(A2AContextSnapshot(context_id="ctx-1", session_id="session-1", cwd=str(tmp_path)))
    pipeline_dir = SessionStorage().session_dir(str(tmp_path), "session-1") / "pipeline"
    first_event = _event(1, "evt-1")
    second_event = _event(3, "evt-3")
    journal = A2APipelineJournal(pipeline_dir)
    journal.append(first_event)
    journal.path.write_text(
        journal.path.read_text(encoding="utf-8")
        + "not-json\n"
        + json.dumps(second_event, ensure_ascii=False, separators=(",", ":"))
        + "\n",
        encoding="utf-8",
    )
    snapshot_store = A2APipelineSnapshotStore(pipeline_dir)
    store = A2ATaskStore(metrics=NoOpA2AMetrics(), persistence=persistence)
    service = A2APipelineRecoveryService(task_store=store)

    with pytest.raises(A2APipelineJournalReadError):
        await service.get_state(context_id="ctx-1")
    assert snapshot_store.load() is None


@pytest.mark.asyncio
async def test_recovery_resolves_state_from_task_id(tmp_path) -> None:
    persistence = A2APersistenceStore(tmp_path / "a2a")
    persistence.save_task(A2ATaskSnapshot(task_id="task-1", context_id="ctx-1", state="completed"))
    persistence.save_context(A2AContextSnapshot(context_id="ctx-1", session_id="session-1", cwd=str(tmp_path)))
    pipeline_dir = SessionStorage().session_dir(str(tmp_path), "session-1") / "pipeline"
    journal = A2APipelineJournal(pipeline_dir)
    journal.append(_event(1, "evt-1"))
    A2APipelineSnapshotStore(pipeline_dir).save(reduce_pipeline_events([_event(1, "evt-1")]))
    store = A2ATaskStore(metrics=NoOpA2AMetrics(), persistence=persistence)

    service = A2APipelineRecoveryService(task_store=store)
    state = await service.get_state(task_id="task-1")

    assert state["snapshot"]["contextId"] == "ctx-1"
    assert state["snapshot"]["taskId"] == "task-1"


@pytest.mark.asyncio
async def test_recovery_rejects_task_id_when_pipeline_state_belongs_to_different_task(tmp_path) -> None:
    persistence = A2APersistenceStore(tmp_path / "a2a")
    persistence.save_task(A2ATaskSnapshot(task_id="task-2", context_id="ctx-1", state="completed"))
    persistence.save_context(A2AContextSnapshot(context_id="ctx-1", session_id="session-1", cwd=str(tmp_path)))
    pipeline_dir = SessionStorage().session_dir(str(tmp_path), "session-1") / "pipeline"
    event = _event_for_task(1, "evt-1", task_id="task-1")
    A2APipelineJournal(pipeline_dir).append(event)
    A2APipelineSnapshotStore(pipeline_dir).save(reduce_pipeline_events([event]))
    store = A2ATaskStore(metrics=NoOpA2AMetrics(), persistence=persistence)
    service = A2APipelineRecoveryService(task_store=store)

    with pytest.raises(ValueError, match="A2A pipeline state not found"):
        await service.get_state(task_id="task-2")


@pytest.mark.asyncio
async def test_recovery_resolves_cleanup_snapshot_from_normal_delivery_task_id(tmp_path) -> None:
    persistence = A2APersistenceStore(tmp_path / "a2a")
    persistence.save_task(A2ATaskSnapshot(task_id="task-pipeline", context_id="ctx-1", state="completed"))
    persistence.save_task(A2ATaskSnapshot(task_id="task-normal", context_id="ctx-1", state="input-required"))
    persistence.save_context(A2AContextSnapshot(context_id="ctx-1", session_id="session-1", cwd=str(tmp_path)))
    pipeline_dir = SessionStorage().session_dir(str(tmp_path), "session-1") / "pipeline"
    raw_error = (
        "DeleteStack failed AccessKeySecret=super-secret token=sk-live-1234567890 "
        "at /Users/alice/.iac-code/projects/session/pipeline/cleanup.yaml"
    )
    pipeline_started = _event_for_task(1, "evt-pipeline-started", task_id="task-pipeline")
    cleanup_started = _event_for_task(2, "evt-cleanup-started", task_id="task-pipeline")
    cleanup_started.update(
        {
            "eventType": "cleanup_started",
            "scope": "cleanup",
            "deliveryTaskId": "task-normal",
            "data": {
                "status": "started",
                "resourceCount": 1,
                "prompt": "hidden cleanup prompt for stack-123",
                "ledgerPath": "/Users/alice/.iac-code/projects/session/pipeline/cleanup.yaml",
                "provider": "ros",
                "resourceType": "stack",
                "resourceId": "stack-123",
                "regionId": "cn-hangzhou",
                "cleanupStatus": "started",
                "progressStatus": "DELETE_STARTED",
                "lastError": raw_error,
            },
        }
    )
    journal = A2APipelineJournal(pipeline_dir)
    journal.append(pipeline_started)
    journal.append(cleanup_started)
    A2APipelineSnapshotStore(pipeline_dir).save(reduce_pipeline_events([pipeline_started, cleanup_started]))
    store = A2ATaskStore(metrics=NoOpA2AMetrics(), persistence=persistence)
    service = A2APipelineRecoveryService(task_store=store)

    state = await service.get_state(task_id="task-normal", after_sequence=0)

    assert state["snapshot"]["taskId"] == "task-pipeline"
    assert state["snapshot"]["cleanup"]["status"] == "started"
    assert state["snapshot"]["cleanup"]["resources"][0]["resourceId"] == "stack-123"
    assert "prompt" not in state["snapshot"]["cleanup"]
    assert "ledgerPath" not in state["snapshot"]["cleanup"]
    assert "prompt" not in state["snapshot"]["cleanup"]["history"][0]["data"]
    assert "ledgerPath" not in state["snapshot"]["cleanup"]["history"][0]["data"]
    assert raw_error not in state["snapshot"]["cleanup"]["history"][0]["data"]["lastError"]
    assert [event["eventId"] for event in state["events"]] == ["evt-cleanup-started"]
    assert "prompt" not in state["events"][0]["data"]
    assert "ledgerPath" not in state["events"][0]["data"]
    assert raw_error not in state["events"][0]["data"]["lastError"]
    rendered = json.dumps(state, ensure_ascii=False)
    assert "super-secret" not in rendered
    assert "sk-live-1234567890" not in rendered
    assert "/Users/alice" not in rendered


@pytest.mark.asyncio
async def test_recovery_by_delivery_task_catches_up_stale_pipeline_snapshot(tmp_path) -> None:
    persistence = A2APersistenceStore(tmp_path / "a2a")
    persistence.save_task(A2ATaskSnapshot(task_id="task-pipeline", context_id="ctx-1", state="completed"))
    persistence.save_task(A2ATaskSnapshot(task_id="task-normal", context_id="ctx-1", state="input-required"))
    persistence.save_context(A2AContextSnapshot(context_id="ctx-1", session_id="session-1", cwd=str(tmp_path)))
    pipeline_dir = SessionStorage().session_dir(str(tmp_path), "session-1") / "pipeline"
    pipeline_started = _event_for_task(1, "evt-pipeline-started", task_id="task-pipeline")
    cleanup_started = _event_for_task(2, "evt-cleanup-started", task_id="task-pipeline")
    cleanup_started.update(
        {
            "eventType": "cleanup_started",
            "scope": "cleanup",
            "deliveryTaskId": "task-normal",
            "data": {
                "status": "started",
                "resourceCount": 1,
                "provider": "ros",
                "resourceType": "stack",
                "resourceId": "stack-123",
                "regionId": "cn-hangzhou",
                "cleanupStatus": "started",
                "progressStatus": "DELETE_STARTED",
            },
        }
    )
    journal = A2APipelineJournal(pipeline_dir)
    journal.append(pipeline_started)
    journal.append(cleanup_started)
    A2APipelineSnapshotStore(pipeline_dir).save(reduce_pipeline_events([pipeline_started]))
    store = A2ATaskStore(metrics=NoOpA2AMetrics(), persistence=persistence)
    service = A2APipelineRecoveryService(task_store=store)

    state = await service.get_state(task_id="task-normal")

    assert state["snapshot"]["lastSequence"] == 2
    assert state["snapshot"]["cleanup"]["status"] == "started"
    assert state["snapshot"]["cleanup"]["resources"][0]["resourceId"] == "stack-123"
    assert "prompt" not in state["snapshot"]["cleanup"]
    assert "ledgerPath" not in state["snapshot"]["cleanup"]


@pytest.mark.asyncio
async def test_recovery_rejects_context_id_that_does_not_match_task_id(tmp_path) -> None:
    persistence = A2APersistenceStore(tmp_path / "a2a")
    persistence.save_task(A2ATaskSnapshot(task_id="task-1", context_id="ctx-1", state="completed"))
    persistence.save_context(A2AContextSnapshot(context_id="ctx-2", session_id="session-2", cwd=str(tmp_path)))
    pipeline_dir = SessionStorage().session_dir(str(tmp_path), "session-2") / "pipeline"
    event = _event_for_task(1, "evt-1", task_id="task-2", context_id="ctx-2")
    A2APipelineJournal(pipeline_dir).append(event)
    A2APipelineSnapshotStore(pipeline_dir).save(reduce_pipeline_events([event]))
    store = A2ATaskStore(metrics=NoOpA2AMetrics(), persistence=persistence)
    service = A2APipelineRecoveryService(task_store=store)

    with pytest.raises(ValueError, match="A2A task/context mismatch"):
        await service.get_state(context_id="ctx-2", task_id="task-1")


@pytest.mark.asyncio
async def test_task_id_recovery_does_not_write_filtered_snapshot_to_shared_context(tmp_path) -> None:
    persistence = A2APersistenceStore(tmp_path / "a2a")
    persistence.save_task(A2ATaskSnapshot(task_id="task-old", context_id="ctx-1", state="completed"))
    persistence.save_task(A2ATaskSnapshot(task_id="task-new", context_id="ctx-1", state="completed"))
    persistence.save_context(A2AContextSnapshot(context_id="ctx-1", session_id="session-1", cwd=str(tmp_path)))
    pipeline_dir = SessionStorage().session_dir(str(tmp_path), "session-1") / "pipeline"
    journal = A2APipelineJournal(pipeline_dir)
    journal.append(_event_for_task(1, "evt-old", task_id="task-old"))
    journal.append(_event_for_task(2, "evt-new", task_id="task-new"))
    snapshot_store = A2APipelineSnapshotStore(pipeline_dir)
    store = A2ATaskStore(metrics=NoOpA2AMetrics(), persistence=persistence)
    service = A2APipelineRecoveryService(task_store=store)

    old_state = await service.get_state(task_id="task-old")

    assert old_state["snapshot"]["taskId"] == "task-old"
    assert snapshot_store.load() is None

    context_state = await service.get_state(context_id="ctx-1")
    assert context_state["snapshot"]["taskId"] == "task-new"
    assert snapshot_store.load()["taskId"] == "task-new"


@pytest.mark.asyncio
async def test_task_id_recovery_rebuilds_mixed_snapshot_for_requested_task(tmp_path) -> None:
    persistence = A2APersistenceStore(tmp_path / "a2a")
    persistence.save_task(A2ATaskSnapshot(task_id="task-new", context_id="ctx-1", state="completed"))
    persistence.save_context(A2AContextSnapshot(context_id="ctx-1", session_id="session-1", cwd=str(tmp_path)))
    pipeline_dir = SessionStorage().session_dir(str(tmp_path), "session-1") / "pipeline"
    old_event = _event_for_task(1, "evt-old", task_id="task-old")
    old_event["eventType"] = "text_delta"
    old_event["data"] = {"text": "old"}
    new_event = _event_for_task(2, "evt-new", task_id="task-new")
    new_event["eventType"] = "text_delta"
    new_event["data"] = {"text": "new"}
    journal = A2APipelineJournal(pipeline_dir)
    journal.append(old_event)
    journal.append(new_event)
    A2APipelineSnapshotStore(pipeline_dir).save(reduce_pipeline_events([old_event, new_event]))
    store = A2ATaskStore(metrics=NoOpA2AMetrics(), persistence=persistence)
    service = A2APipelineRecoveryService(task_store=store)

    state = await service.get_state(task_id="task-new")

    assert state["snapshot"]["taskId"] == "task-new"
    assert [message["text"] for message in state["snapshot"]["display"]["messages"]] == ["new"]


@pytest.mark.asyncio
async def test_context_recovery_uses_latest_task_without_merging_previous_task_ui(tmp_path) -> None:
    persistence = A2APersistenceStore(tmp_path / "a2a")
    persistence.save_task(A2ATaskSnapshot(task_id="task-old", context_id="ctx-1", state="completed"))
    persistence.save_task(A2ATaskSnapshot(task_id="task-new", context_id="ctx-1", state="completed"))
    persistence.save_context(A2AContextSnapshot(context_id="ctx-1", session_id="session-1", cwd=str(tmp_path)))
    pipeline_dir = SessionStorage().session_dir(str(tmp_path), "session-1") / "pipeline"
    old_event = _event_for_task(1, "evt-old", task_id="task-old")
    old_event["eventType"] = "text_delta"
    old_event["data"] = {"text": "old"}
    new_event = _event_for_task(2, "evt-new", task_id="task-new")
    new_event["eventType"] = "text_delta"
    new_event["data"] = {"text": "new"}
    journal = A2APipelineJournal(pipeline_dir)
    journal.append(old_event)
    journal.append(new_event)
    snapshot_store = A2APipelineSnapshotStore(pipeline_dir)
    snapshot_store.save(reduce_pipeline_events([old_event, new_event]))
    store = A2ATaskStore(metrics=NoOpA2AMetrics(), persistence=persistence)
    service = A2APipelineRecoveryService(task_store=store)

    state = await service.get_state(context_id="ctx-1")

    assert state["snapshot"]["taskId"] == "task-new"
    assert [message["text"] for message in state["snapshot"]["display"]["messages"]] == ["new"]


@pytest.mark.asyncio
async def test_context_recovery_returns_matching_snapshot_when_journal_is_missing_snapshot_events(tmp_path) -> None:
    persistence = A2APersistenceStore(tmp_path / "a2a")
    persistence.save_context(A2AContextSnapshot(context_id="ctx-1", session_id="session-1", cwd=str(tmp_path)))
    pipeline_dir = SessionStorage().session_dir(str(tmp_path), "session-1") / "pipeline"
    snapshot_event = _event_for_task(5, "evt-snapshot-only", task_id="task-1")
    snapshot_event["eventType"] = "text_delta"
    snapshot_event["data"] = {"text": "snapshot text"}
    snapshot_store = A2APipelineSnapshotStore(pipeline_dir)
    snapshot_store.save(reduce_pipeline_events([snapshot_event]))
    store = A2ATaskStore(metrics=NoOpA2AMetrics(), persistence=persistence)
    service = A2APipelineRecoveryService(task_store=store)

    state = await service.get_state(context_id="ctx-1")

    assert state["snapshot"]["taskId"] == "task-1"
    assert state["snapshot"]["lastSequence"] == 5
    assert state["snapshot"]["display"]["messages"][0]["text"] == "snapshot text"
    assert state["events"] == []


@pytest.mark.asyncio
async def test_context_recovery_keeps_snapshot_when_same_task_journal_is_incomplete(tmp_path) -> None:
    persistence = A2APersistenceStore(tmp_path / "a2a")
    persistence.save_context(A2AContextSnapshot(context_id="ctx-1", session_id="session-1", cwd=str(tmp_path)))
    pipeline_dir = SessionStorage().session_dir(str(tmp_path), "session-1") / "pipeline"
    first_event = _event_for_task(1, "evt-first", task_id="task-1")
    first_event["eventType"] = "text_delta"
    first_event["data"] = {"text": "first"}
    snapshot_only_event = _event_for_task(5, "evt-snapshot-only", task_id="task-1")
    snapshot_only_event["eventType"] = "text_delta"
    snapshot_only_event["data"] = {"text": " snapshot"}
    journal = A2APipelineJournal(pipeline_dir)
    journal.append(first_event)
    snapshot_store = A2APipelineSnapshotStore(pipeline_dir)
    snapshot_store.save(reduce_pipeline_events([first_event, snapshot_only_event]))
    store = A2ATaskStore(metrics=NoOpA2AMetrics(), persistence=persistence)
    service = A2APipelineRecoveryService(task_store=store)

    state = await service.get_state(context_id="ctx-1")

    assert state["snapshot"]["lastSequence"] == 5
    assert state["snapshot"]["display"]["messages"][0]["text"] == "first snapshot"
    assert snapshot_store.load()["lastSequence"] == 5
    assert state["events"] == []


@pytest.mark.asyncio
async def test_context_recovery_prefers_newer_snapshot_task_over_older_journal_task(tmp_path) -> None:
    persistence = A2APersistenceStore(tmp_path / "a2a")
    persistence.save_context(A2AContextSnapshot(context_id="ctx-1", session_id="session-1", cwd=str(tmp_path)))
    pipeline_dir = SessionStorage().session_dir(str(tmp_path), "session-1") / "pipeline"
    old_event = _event_for_task(3, "evt-old", task_id="task-old")
    old_event["eventType"] = "text_delta"
    old_event["data"] = {"text": "old"}
    new_snapshot_event = _event_for_task(8, "evt-new-snapshot", task_id="task-new")
    new_snapshot_event["eventType"] = "text_delta"
    new_snapshot_event["data"] = {"text": "new"}
    journal = A2APipelineJournal(pipeline_dir)
    journal.append(old_event)
    snapshot_store = A2APipelineSnapshotStore(pipeline_dir)
    snapshot_store.save(reduce_pipeline_events([new_snapshot_event]))
    store = A2ATaskStore(metrics=NoOpA2AMetrics(), persistence=persistence)
    service = A2APipelineRecoveryService(task_store=store)

    state = await service.get_state(context_id="ctx-1")

    assert state["snapshot"]["taskId"] == "task-new"
    assert state["snapshot"]["lastSequence"] == 8
    assert state["snapshot"]["display"]["messages"][0]["text"] == "new"
    assert state["events"] == []


@pytest.mark.asyncio
async def test_context_recovery_prefers_newer_journal_task_over_older_incomplete_snapshot(tmp_path) -> None:
    persistence = A2APersistenceStore(tmp_path / "a2a")
    persistence.save_context(A2AContextSnapshot(context_id="ctx-1", session_id="session-1", cwd=str(tmp_path)))
    pipeline_dir = SessionStorage().session_dir(str(tmp_path), "session-1") / "pipeline"
    old_first = _event_for_task(1, "evt-old-first", task_id="task-old")
    old_first["eventType"] = "text_delta"
    old_first["data"] = {"text": "old"}
    old_snapshot_only = _event_for_task(5, "evt-old-snapshot-only", task_id="task-old")
    old_snapshot_only["eventType"] = "text_delta"
    old_snapshot_only["data"] = {"text": " snapshot"}
    new_event = _event_for_task(6, "evt-new", task_id="task-new")
    new_event["eventType"] = "text_delta"
    new_event["data"] = {"text": "new"}
    journal = A2APipelineJournal(pipeline_dir)
    journal.append(old_first)
    journal.append(new_event)
    snapshot_store = A2APipelineSnapshotStore(pipeline_dir)
    snapshot_store.save(reduce_pipeline_events([old_first, old_snapshot_only]))
    store = A2ATaskStore(metrics=NoOpA2AMetrics(), persistence=persistence)
    service = A2APipelineRecoveryService(task_store=store)

    state = await service.get_state(context_id="ctx-1")

    assert state["snapshot"]["taskId"] == "task-new"
    assert state["snapshot"]["lastSequence"] == 6
    assert state["snapshot"]["display"]["messages"][0]["text"] == "new"


@pytest.mark.asyncio
async def test_recovery_rejects_task_events_from_different_context(tmp_path) -> None:
    persistence = A2APersistenceStore(tmp_path / "a2a")
    persistence.save_task(A2ATaskSnapshot(task_id="task-1", context_id="ctx-1", state="completed"))
    persistence.save_context(A2AContextSnapshot(context_id="ctx-1", session_id="session-1", cwd=str(tmp_path)))
    pipeline_dir = SessionStorage().session_dir(str(tmp_path), "session-1") / "pipeline"
    event = _event_for_task(1, "evt-1", task_id="task-1", context_id="ctx-2")
    A2APipelineJournal(pipeline_dir).append(event)
    store = A2ATaskStore(metrics=NoOpA2AMetrics(), persistence=persistence)
    service = A2APipelineRecoveryService(task_store=store)

    with pytest.raises(ValueError, match="A2A pipeline state not found"):
        await service.get_state(task_id="task-1")


@pytest.mark.asyncio
async def test_recovery_returns_not_found_when_context_has_no_pipeline_state(tmp_path) -> None:
    persistence = A2APersistenceStore(tmp_path / "a2a")
    persistence.save_context(A2AContextSnapshot(context_id="ctx-empty", session_id="session-empty", cwd=str(tmp_path)))
    store = A2ATaskStore(metrics=NoOpA2AMetrics(), persistence=persistence)
    service = A2APipelineRecoveryService(task_store=store)

    with pytest.raises(ValueError, match="A2A pipeline state not found"):
        await service.get_state(context_id="ctx-empty")


@pytest.mark.asyncio
async def test_context_recovery_ignores_pipeline_events_from_other_context(tmp_path) -> None:
    persistence = A2APersistenceStore(tmp_path / "a2a")
    persistence.save_context(A2AContextSnapshot(context_id="ctx-empty", session_id="session-1", cwd=str(tmp_path)))
    pipeline_dir = SessionStorage().session_dir(str(tmp_path), "session-1") / "pipeline"
    event = _event_for_task(1, "evt-other", task_id="task-other", context_id="ctx-other")
    A2APipelineJournal(pipeline_dir).append(event)
    snapshot_store = A2APipelineSnapshotStore(pipeline_dir)
    store = A2ATaskStore(metrics=NoOpA2AMetrics(), persistence=persistence)
    service = A2APipelineRecoveryService(task_store=store)

    with pytest.raises(ValueError, match="A2A pipeline state not found"):
        await service.get_state(context_id="ctx-empty")
    assert snapshot_store.load() is None


@pytest.mark.asyncio
async def test_context_recovery_rebuilds_wrong_context_snapshot_from_matching_journal_events(tmp_path) -> None:
    persistence = A2APersistenceStore(tmp_path / "a2a")
    persistence.save_context(A2AContextSnapshot(context_id="ctx-1", session_id="session-1", cwd=str(tmp_path)))
    pipeline_dir = SessionStorage().session_dir(str(tmp_path), "session-1") / "pipeline"
    correct_event = _event_for_task(2, "evt-correct", task_id="task-1", context_id="ctx-1")
    wrong_event = _event_for_task(1, "evt-wrong", task_id="task-other", context_id="ctx-other")
    journal = A2APipelineJournal(pipeline_dir)
    journal.append(correct_event)
    snapshot_store = A2APipelineSnapshotStore(pipeline_dir)
    snapshot_store.save(reduce_pipeline_events([wrong_event]))
    store = A2ATaskStore(metrics=NoOpA2AMetrics(), persistence=persistence)
    service = A2APipelineRecoveryService(task_store=store)

    state = await service.get_state(context_id="ctx-1")

    assert state["snapshot"]["contextId"] == "ctx-1"
    assert state["snapshot"]["taskId"] == "task-1"
    assert state["snapshot"]["lastSequence"] == 2
    assert snapshot_store.load()["contextId"] == "ctx-1"
