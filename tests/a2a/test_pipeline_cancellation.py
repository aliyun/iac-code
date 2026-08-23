from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from iac_code.a2a.executor import IacCodeA2AExecutor
from iac_code.a2a.metrics import NoOpA2AMetrics
from iac_code.a2a.pipeline_cancellation import (
    CANCEL_REASON_EXECUTOR_ERROR,
    CANCEL_REASON_RESOURCE_LIMIT,
    CANCEL_REASON_UNKNOWN,
    CANCEL_REASON_UPSTREAM_TIMEOUT,
    CANCEL_REASON_USER_INITIATED,
    CANCEL_TRIGGER_SCHEDULER,
    CANCEL_TRIGGER_SYSTEM,
    CANCEL_TRIGGER_USER,
    PipelineCancellation,
    cancellation_event_data,
    pipeline_cancellation,
    resolve_pipeline_cancellation,
    unknown_pipeline_cancellation,
)
from iac_code.a2a.pipeline_journal import A2APipelineJournal
from iac_code.a2a.pipeline_snapshot import A2APipelineSnapshotStore, reduce_pipeline_events
from iac_code.a2a.task_store import A2ATaskStore
from tests.a2a.fakes import FakeEventQueue, FakeRequestContext


def test_user_initiated_cancellation_defaults_trigger_to_user() -> None:
    cancellation = pipeline_cancellation(CANCEL_REASON_USER_INITIATED)

    assert cancellation.reason_code == CANCEL_REASON_USER_INITIATED
    assert cancellation.trigger_source == CANCEL_TRIGGER_USER


def test_system_reason_codes_default_to_expected_trigger_sources() -> None:
    assert pipeline_cancellation(CANCEL_REASON_UPSTREAM_TIMEOUT).trigger_source == CANCEL_TRIGGER_SCHEDULER
    assert pipeline_cancellation(CANCEL_REASON_EXECUTOR_ERROR).trigger_source == CANCEL_TRIGGER_SYSTEM
    assert pipeline_cancellation(CANCEL_REASON_RESOURCE_LIMIT).trigger_source == CANCEL_TRIGGER_SYSTEM


def test_unknown_reason_code_falls_back_to_unknown_system() -> None:
    cancellation = pipeline_cancellation("not-a-real-code", trigger_source="not-a-real-source")

    assert cancellation.reason_code == CANCEL_REASON_UNKNOWN
    assert cancellation.trigger_source == CANCEL_TRIGGER_SYSTEM
    assert unknown_pipeline_cancellation() == PipelineCancellation()


def test_explicit_trigger_source_overrides_the_default() -> None:
    cancellation = pipeline_cancellation(CANCEL_REASON_USER_INITIATED, trigger_source=CANCEL_TRIGGER_SCHEDULER)

    assert cancellation.trigger_source == CANCEL_TRIGGER_SCHEDULER


def test_event_data_preserves_legacy_fields_and_adds_attribution() -> None:
    data = cancellation_event_data(
        pipeline_cancellation(CANCEL_REASON_USER_INITIATED, detail="a2a cancelTask request"),
        base={"source": "executor", "reason": "Task canceled."},
    )

    assert data == {
        "source": "executor",
        "reason": "Task canceled.",
        "reasonCode": CANCEL_REASON_USER_INITIATED,
        "triggerSource": CANCEL_TRIGGER_USER,
        "detail": "a2a cancelTask request",
    }


def test_event_data_omits_detail_when_absent() -> None:
    data = cancellation_event_data(pipeline_cancellation(CANCEL_REASON_UPSTREAM_TIMEOUT))

    assert data == {
        "reasonCode": CANCEL_REASON_UPSTREAM_TIMEOUT,
        "triggerSource": CANCEL_TRIGGER_SCHEDULER,
    }


def test_event_data_from_missing_attribution_is_unknown_system() -> None:
    data = cancellation_event_data(None, base={"source": "executor"})

    assert data == {
        "source": "executor",
        "reasonCode": CANCEL_REASON_UNKNOWN,
        "triggerSource": CANCEL_TRIGGER_SYSTEM,
    }


def test_resolve_normalizes_a_tampered_attribution() -> None:
    resolved = resolve_pipeline_cancellation(PipelineCancellation(reason_code="bogus", trigger_source="bogus"))

    assert resolved.reason_code == CANCEL_REASON_UNKNOWN
    assert resolved.trigger_source == CANCEL_TRIGGER_SYSTEM


def test_detail_is_truncated_and_blank_detail_is_dropped() -> None:
    assert pipeline_cancellation(CANCEL_REASON_EXECUTOR_ERROR, detail="   ").detail is None
    long_detail = pipeline_cancellation(CANCEL_REASON_EXECUTOR_ERROR, detail="x" * 500).detail
    assert long_detail is not None
    assert len(long_detail) == 200


def _canceled_envelope(sequence: int, data: dict[str, object]) -> dict[str, object]:
    return {
        "schemaVersion": "1.0",
        "extensionUri": "urn:iac-code:a2a:pipeline-events:v1",
        "eventId": f"evt-{sequence}",
        "sequence": sequence,
        "createdAt": "2026-08-21T10:00:00Z",
        "eventType": "pipeline_canceled",
        "scope": "pipeline",
        "pipelineRunId": "ctx-1",
        "taskId": "task-1",
        "contextId": "ctx-1",
        "pipelineName": "selling",
        "status": "canceled",
        "data": data,
    }


def test_snapshot_exposes_cancel_attribution_for_grouping() -> None:
    user_snapshot = reduce_pipeline_events(
        [
            _canceled_envelope(
                1,
                {
                    "source": "a2a_cancel",
                    "reason": "Task canceled while waiting for input.",
                    "reasonCode": CANCEL_REASON_USER_INITIATED,
                    "triggerSource": CANCEL_TRIGGER_USER,
                },
            )
        ]
    )
    system_snapshot = reduce_pipeline_events(
        [
            _canceled_envelope(
                1,
                {
                    "source": "executor",
                    "reason": "Task canceled.",
                    "reasonCode": CANCEL_REASON_UPSTREAM_TIMEOUT,
                    "triggerSource": CANCEL_TRIGGER_SCHEDULER,
                },
            )
        ]
    )

    assert user_snapshot["status"] == "canceled"
    assert user_snapshot["cancellation"] == {
        "reasonCode": CANCEL_REASON_USER_INITIATED,
        "triggerSource": CANCEL_TRIGGER_USER,
    }
    assert system_snapshot["cancellation"] == {
        "reasonCode": CANCEL_REASON_UPSTREAM_TIMEOUT,
        "triggerSource": CANCEL_TRIGGER_SCHEDULER,
    }
    assert user_snapshot["cancellation"] != system_snapshot["cancellation"]


def test_snapshot_cancellation_is_none_for_legacy_generic_cancel() -> None:
    snapshot = reduce_pipeline_events([_canceled_envelope(1, {"source": "executor", "reason": "Task canceled."})])

    assert snapshot["status"] == "canceled"
    assert snapshot["cancellation"] is None


def test_pending_terminal_lifts_cancel_attribution() -> None:
    envelope = _canceled_envelope(
        1,
        {
            "source": "executor",
            "reason": "Task canceled.",
            "reasonCode": CANCEL_REASON_EXECUTOR_ERROR,
            "triggerSource": CANCEL_TRIGGER_SYSTEM,
            "detail": "executor drain failed",
        },
    )
    envelope["visibility"] = "pending_backup"

    snapshot = reduce_pipeline_events([envelope])

    assert snapshot["pendingTerminal"]["cancellation"] == {
        "reasonCode": CANCEL_REASON_EXECUTOR_ERROR,
        "triggerSource": CANCEL_TRIGGER_SYSTEM,
        "detail": "executor drain failed",
    }


def test_waiting_input_cancel_writes_attribution_into_the_journal(tmp_path: Path) -> None:
    from iac_code.a2a.pipeline_executor import (
        WaitingInputCancelResult,
        cancel_waiting_input_task_from_sidecar,
    )
    from iac_code.a2a.pipeline_paths import a2a_pipeline_dir_for_session

    cwd = tmp_path / "workspace"
    session_id = "session-ctx-1"
    context_id = "ctx-1"
    pipeline_dir = a2a_pipeline_dir_for_session(cwd=str(cwd), session_id=session_id)
    pending = {
        "schemaVersion": "1.0",
        "extensionUri": "urn:iac-code:a2a:pipeline-events:v1",
        "eventId": "evt-selection",
        "sequence": 1,
        "createdAt": "2026-08-21T10:00:00Z",
        "eventType": "input_required",
        "scope": "step",
        "pipelineRunId": context_id,
        "taskId": "task-1",
        "contextId": context_id,
        "pipelineName": "selling",
        "status": "input_required",
        "step": {"runId": "step-confirm_and_select-1", "id": "confirm_and_select", "attempt": 1},
        "input": {
            "inputId": "input-confirm_and_select-1",
            "kind": "candidate_selection",
            "prompt": "choose",
            "options": [{"name": "A", "candidate_index": 0}],
        },
    }
    A2APipelineJournal(pipeline_dir).append(pending)
    A2APipelineSnapshotStore(pipeline_dir).save(reduce_pipeline_events([pending]))

    result = cancel_waiting_input_task_from_sidecar(
        cwd=str(cwd),
        session_id=session_id,
        context_id=context_id,
        task_id="task-1",
        reason="user canceled",
        cancellation=pipeline_cancellation(
            CANCEL_REASON_USER_INITIATED,
            trigger_source=CANCEL_TRIGGER_USER,
            detail="a2a cancelTask on waiting-input task",
        ),
    )

    assert result == WaitingInputCancelResult.CANCELED
    canceled = next(
        event for event in A2APipelineJournal(pipeline_dir).read_all() if event["eventType"] == "pipeline_canceled"
    )
    assert canceled["data"]["source"] == "a2a_cancel"
    assert canceled["data"]["reason"] == "user canceled"
    assert canceled["data"]["reasonCode"] == CANCEL_REASON_USER_INITIATED
    assert canceled["data"]["triggerSource"] == CANCEL_TRIGGER_USER
    assert canceled["data"]["detail"] == "a2a cancelTask on waiting-input task"


def test_waiting_input_cancel_without_attribution_falls_back_to_unknown(tmp_path: Path) -> None:
    from iac_code.a2a.pipeline_executor import (
        WaitingInputCancelResult,
        cancel_waiting_input_task_from_sidecar,
    )
    from iac_code.a2a.pipeline_paths import a2a_pipeline_dir_for_session

    cwd = tmp_path / "workspace"
    session_id = "session-ctx-2"
    context_id = "ctx-2"
    pipeline_dir = a2a_pipeline_dir_for_session(cwd=str(cwd), session_id=session_id)
    pending = {
        "schemaVersion": "1.0",
        "extensionUri": "urn:iac-code:a2a:pipeline-events:v1",
        "eventId": "evt-selection",
        "sequence": 1,
        "createdAt": "2026-08-21T10:00:00Z",
        "eventType": "input_required",
        "scope": "step",
        "pipelineRunId": context_id,
        "taskId": "task-2",
        "contextId": context_id,
        "pipelineName": "selling",
        "status": "input_required",
        "step": {"runId": "step-confirm_and_select-1", "id": "confirm_and_select", "attempt": 1},
        "input": {
            "inputId": "input-confirm_and_select-1",
            "kind": "candidate_selection",
            "prompt": "choose",
            "options": [{"name": "A", "candidate_index": 0}],
        },
    }
    A2APipelineJournal(pipeline_dir).append(pending)
    A2APipelineSnapshotStore(pipeline_dir).save(reduce_pipeline_events([pending]))

    result = cancel_waiting_input_task_from_sidecar(
        cwd=str(cwd),
        session_id=session_id,
        context_id=context_id,
        task_id="task-2",
        reason="canceled",
    )

    assert result == WaitingInputCancelResult.CANCELED
    canceled = next(
        event for event in A2APipelineJournal(pipeline_dir).read_all() if event["eventType"] == "pipeline_canceled"
    )
    assert canceled["data"]["reasonCode"] == CANCEL_REASON_UNKNOWN
    assert canceled["data"]["triggerSource"] == CANCEL_TRIGGER_SYSTEM
    assert "detail" not in canceled["data"]


@pytest.mark.asyncio
async def test_active_run_cancel_records_user_attribution_from_task_store(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A2A ``cancelTask`` on a running pipeline must be attributed to the user."""

    journal_events = await _run_and_cancel_pipeline(
        monkeypatch,
        tmp_path,
        cancellation=pipeline_cancellation(
            CANCEL_REASON_USER_INITIATED,
            trigger_source=CANCEL_TRIGGER_USER,
            detail="a2a cancelTask request",
        ),
    )

    canceled = next(event for event in journal_events if event["eventType"] == "pipeline_canceled")
    assert canceled["data"]["source"] == "executor"
    assert canceled["data"]["reasonCode"] == CANCEL_REASON_USER_INITIATED
    assert canceled["data"]["triggerSource"] == CANCEL_TRIGGER_USER
    assert canceled["data"]["detail"] == "a2a cancelTask request"


@pytest.mark.asyncio
async def test_active_run_cancel_records_upstream_timeout_attribution(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A scheduler-driven timeout cancel stays distinguishable from a user stop."""

    journal_events = await _run_and_cancel_pipeline(
        monkeypatch,
        tmp_path,
        cancellation=pipeline_cancellation(CANCEL_REASON_UPSTREAM_TIMEOUT),
    )

    canceled = next(event for event in journal_events if event["eventType"] == "pipeline_canceled")
    assert canceled["data"]["reasonCode"] == CANCEL_REASON_UPSTREAM_TIMEOUT
    assert canceled["data"]["triggerSource"] == CANCEL_TRIGGER_SCHEDULER


@pytest.mark.asyncio
async def test_active_run_cancel_without_attribution_stays_unknown(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """An undeclared cancel path keeps the legacy payload plus ``unknown``/``system``."""

    journal_events = await _run_and_cancel_pipeline(monkeypatch, tmp_path, cancellation=None)

    canceled = next(event for event in journal_events if event["eventType"] == "pipeline_canceled")
    assert canceled["data"]["source"] == "executor"
    assert canceled["data"]["reasonCode"] == CANCEL_REASON_UNKNOWN
    assert canceled["data"]["triggerSource"] == CANCEL_TRIGGER_SYSTEM
    assert "detail" not in canceled["data"]


class _CancelingPipeline:
    """Minimal pipeline whose stream raises ``CancelledError`` like a real cancel."""

    def __init__(self, *, session_dir: Path) -> None:
        self.pipeline_name = "selling"
        self.sidecar_status = None
        self.sidecar_restore_result = None
        self.session = SimpleNamespace(session_dir=session_dir)

    async def run(self, prompt: str):
        raise asyncio.CancelledError()
        yield None  # pragma: no cover - keeps ``run`` an async generator

    def clear_sidecar(self) -> None:
        self.sidecar_status = None

    def should_switch_to_normal(self, data: dict) -> bool:
        return True

    def build_normal_handoff_summary(self, data: dict) -> str:
        return "[Pipeline Handoff Context]\nOutcome: canceled"


async def _run_and_cancel_pipeline(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    cancellation: PipelineCancellation | None,
) -> list[dict]:
    """Drive one pipeline run through a cancel and return the journal events."""

    monkeypatch.setenv("IAC_CODE_MODE", "pipeline")
    session_dir = tmp_path / "sidecar"
    pipeline = _CancelingPipeline(session_dir=session_dir)
    monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_pipeline", lambda *a, **k: pipeline)
    monkeypatch.setattr(
        "iac_code.a2a.pipeline_executor.create_agent_runtime",
        lambda options: SimpleNamespace(provider_manager=object(), tool_registry=_NoopToolRegistry()),
    )

    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    task = await store.get_or_create_task(task_id="task-1", context_id="ctx-1")
    task.cancellation = cancellation
    executor = IacCodeA2AExecutor(task_store=store, model="qwen3.6-plus", metrics=NoOpA2AMetrics())

    await executor.execute(
        FakeRequestContext(metadata={"iac_code": {"cwd": str(tmp_path)}}),
        FakeEventQueue(),
    )

    return A2APipelineJournal(session_dir).read_all()


class _NoopToolRegistry:
    def register(self, tool) -> None:
        pass

    def unregister(self, tool_name: str) -> None:
        pass
