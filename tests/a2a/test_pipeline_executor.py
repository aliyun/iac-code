from __future__ import annotations

import asyncio
import contextlib
import json
import shutil
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from a2a.types import TaskStatusUpdateEvent
from google.protobuf.json_format import MessageToDict

from iac_code.a2a.executor import IacCodeA2AExecutor
from iac_code.a2a.metrics import NoOpA2AMetrics
from iac_code.a2a.pipeline_journal import A2APipelineJournal
from iac_code.a2a.pipeline_snapshot import A2APipelineSnapshotStore, reduce_pipeline_events
from iac_code.a2a.task_store import A2ATaskStore
from iac_code.agent.message import ImageBlock
from iac_code.pipeline.engine.cleanup import CleanupLedger, CleanupResource
from iac_code.pipeline.engine.events import PipelineEvent, PipelineEventType
from iac_code.pipeline.engine.interrupt import InterruptVerdict
from iac_code.pipeline.engine.user_input import PipelineUserInput
from iac_code.types.stream_events import AskUserQuestionEvent, TextDeltaEvent

from .fakes import FakeEventQueue, FakeRequestContext

RETRY_TEXT = "A temporary error occurred. Please retry."
AUTH_TEXT = "Authentication required. Configure credentials and retry."


def dump(event):
    return MessageToDict(event, preserving_proto_field_name=False)


def image_interrupt_input() -> PipelineUserInput:
    return PipelineUserInput(
        content=[ImageBlock(media_type="image/png", data="aGVsbG8=")],
        display_text="[Image input]",
        has_images=True,
    )


def _display_text(value):
    return value.display_text if isinstance(value, PipelineUserInput) else value


class FakePipeline:
    def __init__(self, events, *, session_dir: Path) -> None:
        self.events = events
        self.run_prompts: list[str] = []
        self.resume_prompts: list[str] = []
        self.continue_inputs: list[str | None] = []
        self.continue_calls = 0
        self.pipeline_name = "selling"
        self.sidecar_status = None
        self.sidecar_restore_result = None
        self.clear_sidecar_calls = 0
        self.session = SimpleNamespace(session_dir=session_dir)
        self.handoff_enabled = False
        self.handoff_summary = "handoff summary"

    async def run(self, prompt: str):
        self.run_prompts.append(_display_text(prompt))
        for event in self.events:
            if isinstance(event, BaseException):
                raise event
            yield event

    async def resume(self, prompt: str):
        self.resume_prompts.append(_display_text(prompt))
        for event in self.events:
            if isinstance(event, BaseException):
                raise event
            yield event

    def continue_from_sidecar(self, user_input: str | None = None):
        self.continue_calls += 1
        self.continue_inputs.append(_display_text(user_input))
        return self.run(user_input or "continued")

    def clear_sidecar(self) -> None:
        self.clear_sidecar_calls += 1
        self.sidecar_status = None

    def should_switch_to_normal(self, data: dict) -> bool:
        return self.handoff_enabled

    def build_normal_handoff_summary(self, data: dict) -> str:
        return self.handoff_summary


class CloseableEventStream:
    def __init__(self, events, *, wait_until_closed: bool = True) -> None:
        self.events = list(events)
        self.wait_until_closed = wait_until_closed
        self.started = asyncio.Event()
        self.closed_event = asyncio.Event()
        self.closed = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        self.started.set()
        if self.events:
            await asyncio.sleep(0)
            return self.events.pop(0)
        if self.wait_until_closed:
            await self.closed_event.wait()
        raise StopAsyncIteration

    async def aclose(self) -> None:
        self.closed = True
        self.closed_event.set()


def _fake_runtime():
    return SimpleNamespace(provider_manager=object(), tool_registry=object())


def _status_events(queue: FakeEventQueue) -> list[dict]:
    return [dump(event) for event in queue.events if isinstance(event, TaskStatusUpdateEvent)]


async def _wait_for_output_text(task, expected: str) -> None:
    for _ in range(100):
        if "".join(task.output_text) == expected:
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"Expected output text {expected!r}, got {''.join(task.output_text)!r}")


async def _wait_for_pipeline_event(queue: FakeEventQueue, expected_event_type: str) -> None:
    for _ in range(100):
        for event in queue.events:
            if not isinstance(event, TaskStatusUpdateEvent):
                continue
            metadata = dump(event).get("metadata", {}).get("iac_code", {})
            pipeline = metadata.get("pipeline", {})
            if pipeline.get("eventType") == expected_event_type:
                return
        await asyncio.sleep(0.01)
    event_types = [
        dump(event).get("metadata", {}).get("iac_code", {}).get("pipeline", {}).get("eventType")
        for event in queue.events
        if isinstance(event, TaskStatusUpdateEvent)
    ]
    raise AssertionError(f"Expected pipeline event {expected_event_type!r}, got {event_types!r}")


def _pending_coro_names() -> list[str]:
    current = asyncio.current_task()
    return [
        getattr(task.get_coro(), "__qualname__", repr(task.get_coro()))
        for task in asyncio.all_tasks()
        if task is not current and not task.done()
    ]


@pytest.mark.asyncio
async def test_executor_runs_pipeline_mode(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("IAC_CODE_MODE", "pipeline")
    fake_pipeline = FakePipeline(
        [
            PipelineEvent(
                type=PipelineEventType.PIPELINE_STARTED,
                step_id=None,
                timestamp=1717821600.0,
                data={"total_steps": 1, "step_names": ["intent_parsing"]},
            ),
            TextDeltaEvent(text="pipeline output"),
            PipelineEvent(
                type=PipelineEventType.PIPELINE_COMPLETED,
                step_id=None,
                timestamp=1717821601.0,
                data={"total_steps": 1},
            ),
        ],
        session_dir=tmp_path / "sidecar",
    )
    monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_pipeline", lambda *args, **kwargs: fake_pipeline)
    monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_agent_runtime", lambda options: _fake_runtime())

    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    executor = IacCodeA2AExecutor(task_store=store, model="qwen3.6-plus")
    queue = FakeEventQueue()

    await executor.execute(FakeRequestContext(metadata={"iac_code": {"cwd": str(tmp_path)}}), queue)

    assert fake_pipeline.run_prompts == ["hello"]
    states = [dump(event)["status"]["state"] for event in queue.events if isinstance(event, TaskStatusUpdateEvent)]
    assert "TASK_STATE_WORKING" in states
    assert states[-1] == "TASK_STATE_COMPLETED"
    event_types = [
        dump(event)["metadata"]["iac_code"]["pipeline"]["eventType"]
        for event in queue.events
        if isinstance(event, TaskStatusUpdateEvent)
        and "pipeline" in dump(event).get("metadata", {}).get("iac_code", {})
    ]
    assert event_types == ["pipeline_started", "text_delta", "pipeline_completed"]
    record = await store.get_or_create_task(task_id="task-1", context_id="ctx-1")
    assert record.state == "completed"
    assert "".join(record.output_text) == "pipeline output"


@pytest.mark.asyncio
async def test_executor_publishes_normal_handoff_ready_after_completed_pipeline(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("IAC_CODE_MODE", "pipeline")
    session_dir = tmp_path / "sidecar"
    fake_pipeline = FakePipeline(
        [
            PipelineEvent(
                type=PipelineEventType.PIPELINE_COMPLETED,
                step_id=None,
                timestamp=1717821601.0,
                data={"total_steps": 1},
            ),
        ],
        session_dir=session_dir,
    )
    fake_pipeline.handoff_enabled = True
    fake_pipeline.handoff_summary = "[Pipeline Handoff Context]\nPipeline: selling"

    def fake_create_pipeline(*args, **kwargs):
        fake_pipeline._session_storage = kwargs["session_storage"]
        fake_pipeline._session_id = kwargs["session_id"]
        fake_pipeline._cwd = kwargs["cwd"]
        return fake_pipeline

    monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_pipeline", fake_create_pipeline)
    monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_agent_runtime", lambda options: _fake_runtime())

    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    executor = IacCodeA2AExecutor(task_store=store, model="qwen3.6-plus")
    queue = FakeEventQueue()

    await executor.execute(FakeRequestContext(metadata={"iac_code": {"cwd": str(tmp_path)}}), queue)

    pipeline_events = [
        dump(event)["metadata"]["iac_code"]["pipeline"]
        for event in queue.events
        if isinstance(event, TaskStatusUpdateEvent)
        and "pipeline" in dump(event).get("metadata", {}).get("iac_code", {})
    ]
    assert [event["eventType"] for event in pipeline_events] == ["pipeline_completed", "pipeline_handoff_ready"]
    handoff = pipeline_events[1]
    assert handoff["status"] == "completed"
    assert handoff["data"] == {
        "action": "switch_to_normal",
        "targetMode": "normal",
        "outcome": "completed",
        "summary": "[Pipeline Handoff Context]\nPipeline: selling",
    }

    snapshot = A2APipelineSnapshotStore(session_dir).load()
    assert snapshot is not None
    assert snapshot["normalHandoff"]["action"] == "switch_to_normal"
    assert snapshot["normalHandoff"]["targetMode"] == "normal"
    assert snapshot["normalHandoff"]["summary"] == "[Pipeline Handoff Context]\nPipeline: selling"
    from iac_code.services.session_storage import SessionStorage

    session_id = store._contexts["ctx-1"].session_id
    messages = SessionStorage().load(str(tmp_path), session_id)
    assert messages[-1].role == "user"
    assert messages[-1].content == "[Pipeline Handoff Context]\nPipeline: selling"


@pytest.mark.asyncio
async def test_executor_publishes_normal_handoff_ready_with_cleanup_resources(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("IAC_CODE_MODE", "pipeline")
    session_dir = tmp_path / "sidecar"
    ledger = CleanupLedger(session_dir / "cleanup.yaml")
    ledger.mark_cleanup_required(
        [
            CleanupResource(
                provider="ros",
                resource_type="stack",
                resource_id="stack-123",
                resource_name="selling-stack",
                region_id="cn-hangzhou",
                source_step_id="deploying",
            )
        ],
        source_step_id="deploying",
        reason="rollback from deploying",
    )
    fake_pipeline = FakePipeline(
        [
            PipelineEvent(
                type=PipelineEventType.PIPELINE_COMPLETED,
                step_id=None,
                timestamp=1717821601.0,
                data={"total_steps": 1},
            ),
        ],
        session_dir=session_dir,
    )
    fake_pipeline.handoff_enabled = True
    fake_pipeline.handoff_summary = "[Pipeline Handoff Context]\nPipeline: selling"
    fake_pipeline.cleanup_ledger = lambda: ledger

    def fake_create_pipeline(*args, **kwargs):
        fake_pipeline._session_storage = kwargs["session_storage"]
        fake_pipeline._session_id = kwargs["session_id"]
        fake_pipeline._cwd = kwargs["cwd"]
        return fake_pipeline

    monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_pipeline", fake_create_pipeline)
    monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_agent_runtime", lambda options: _fake_runtime())

    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    executor = IacCodeA2AExecutor(task_store=store, model="qwen3.6-plus")
    queue = FakeEventQueue()

    await executor.execute(FakeRequestContext(metadata={"iac_code": {"cwd": str(tmp_path)}}), queue)

    pipeline_events = [
        dump(event)["metadata"]["iac_code"]["pipeline"]
        for event in queue.events
        if isinstance(event, TaskStatusUpdateEvent)
        and "pipeline" in dump(event).get("metadata", {}).get("iac_code", {})
    ]
    handoff = pipeline_events[-1]
    cleanup = handoff["data"]["cleanup"]
    assert cleanup["status"] == "pending"
    assert cleanup["resourceCount"] == 1
    assert cleanup["statusMessage"] == "检测到 1 个回滚残留资源，开始清理流程。"
    assert "prompt" not in cleanup
    assert "ledgerPath" not in cleanup
    assert cleanup["resources"] == [
        {
            "provider": "ros",
            "resourceType": "stack",
            "resourceId": "stack-123",
            "resourceName": "selling-stack",
            "regionId": "cn-hangzhou",
            "sourceStepId": "deploying",
            "cleanupStatus": "pending",
            "progressStatus": None,
            "lastError": None,
        }
    ]

    snapshot = A2APipelineSnapshotStore(session_dir).load()
    assert snapshot is not None
    assert snapshot["cleanup"]["status"] == "pending"
    assert snapshot["cleanup"]["resourceCount"] == 1
    assert snapshot["normalHandoff"]["data"]["cleanup"]["resourceCount"] == 1
    assert "prompt" not in snapshot["cleanup"]
    assert "ledgerPath" not in snapshot["cleanup"]
    assert "prompt" not in snapshot["normalHandoff"]["data"]["cleanup"]
    assert "ledgerPath" not in snapshot["normalHandoff"]["data"]["cleanup"]


@pytest.mark.asyncio
async def test_executor_sets_pipeline_telemetry_correlation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("IAC_CODE_MODE", "pipeline")
    fake_pipeline = FakePipeline(
        [
            PipelineEvent(
                type=PipelineEventType.PIPELINE_COMPLETED,
                step_id=None,
                timestamp=1717821601.0,
                data={"total_steps": 1},
            ),
        ],
        session_dir=tmp_path / "sidecar",
    )
    fake_pipeline.set_telemetry_correlation = MagicMock()
    create_pipeline_kwargs = {}

    def fake_create_pipeline(*args, **kwargs):
        create_pipeline_kwargs.update(kwargs)
        fake_pipeline._session_storage = kwargs["session_storage"]
        fake_pipeline._session_id = kwargs["session_id"]
        fake_pipeline._cwd = kwargs["cwd"]
        return fake_pipeline

    monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_pipeline", fake_create_pipeline)
    monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_agent_runtime", lambda options: _fake_runtime())

    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    executor = IacCodeA2AExecutor(task_store=store, model="qwen3.6-plus")

    await executor.execute(
        FakeRequestContext(task_id="task-1", context_id="ctx-1", metadata={"iac_code": {"cwd": str(tmp_path)}}),
        FakeEventQueue(),
    )

    fake_pipeline.set_telemetry_correlation.assert_called_once_with(
        task_id="task-1",
        context_id="ctx-1",
        pipeline_run_id="ctx-1",
    )
    assert create_pipeline_kwargs["surface"] == "a2a"


@pytest.mark.asyncio
async def test_executor_publishes_normal_handoff_ready_after_failed_pipeline_when_policy_allows(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("IAC_CODE_MODE", "pipeline")
    session_dir = tmp_path / "sidecar"
    fake_pipeline = FakePipeline(
        [
            PipelineEvent(
                type=PipelineEventType.PIPELINE_COMPLETED,
                step_id=None,
                timestamp=1717821601.0,
                data={"total_steps": 1, "failed": True},
            ),
        ],
        session_dir=session_dir,
    )
    fake_pipeline.handoff_enabled = True
    fake_pipeline.handoff_summary = "[Pipeline Handoff Context]\nOutcome: failed"

    def fake_create_pipeline(*args, **kwargs):
        fake_pipeline._session_storage = kwargs["session_storage"]
        fake_pipeline._session_id = kwargs["session_id"]
        fake_pipeline._cwd = kwargs["cwd"]
        return fake_pipeline

    monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_pipeline", fake_create_pipeline)
    monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_agent_runtime", lambda options: _fake_runtime())

    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    executor = IacCodeA2AExecutor(task_store=store, model="qwen3.6-plus")
    queue = FakeEventQueue()

    await executor.execute(FakeRequestContext(metadata={"iac_code": {"cwd": str(tmp_path)}}), queue)

    pipeline_events = [
        dump(event)["metadata"]["iac_code"]["pipeline"]
        for event in queue.events
        if isinstance(event, TaskStatusUpdateEvent)
        and "pipeline" in dump(event).get("metadata", {}).get("iac_code", {})
    ]
    assert [event["eventType"] for event in pipeline_events] == ["pipeline_failed", "pipeline_handoff_ready"]
    handoff = pipeline_events[1]
    assert handoff["status"] == "failed"
    assert handoff["data"]["outcome"] == "failed"
    assert handoff["data"]["summary"] == "[Pipeline Handoff Context]\nOutcome: failed"

    snapshot = A2APipelineSnapshotStore(session_dir).load()
    assert snapshot is not None
    assert snapshot["status"] == "failed"
    assert snapshot["normalHandoff"]["outcome"] == "failed"


@pytest.mark.asyncio
async def test_executor_candidate_started_includes_steps_from_loaded_sub_pipeline(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("IAC_CODE_MODE", "pipeline")
    fake_pipeline = FakePipeline(
        [
            PipelineEvent(
                type=PipelineEventType.SUB_PIPELINE_STARTED,
                step_id=None,
                timestamp=1717821600.0,
                data={
                    "parent_step_id": "evaluate_candidates",
                    "sub_pipeline_id": "evaluate_candidate_candidate_0",
                    "sub_pipeline_name": "evaluate_candidate",
                    "candidate_index": 0,
                    "candidate_name": "轻量应用服务器方案",
                    "total_steps": 2,
                },
            ),
            PipelineEvent(
                type=PipelineEventType.PIPELINE_COMPLETED,
                step_id=None,
                timestamp=1717821601.0,
                data={},
            ),
        ],
        session_dir=tmp_path / "sidecar",
    )
    fake_pipeline._loaded = SimpleNamespace(
        steps=[
            SimpleNamespace(step_id="intent_parsing"),
            SimpleNamespace(
                step_id="evaluate_candidates",
                step_type="parallel_sub_pipeline",
                sub_pipeline_name="evaluate_candidate",
            ),
            SimpleNamespace(step_id="confirm_and_select"),
        ],
        sub_pipelines={
            "evaluate_candidate": SimpleNamespace(
                steps=[
                    SimpleNamespace(step_id="template_generating"),
                    SimpleNamespace(step_id="cost_estimating"),
                ],
            )
        },
    )
    monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_pipeline", lambda *args, **kwargs: fake_pipeline)
    monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_agent_runtime", lambda options: _fake_runtime())

    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    executor = IacCodeA2AExecutor(task_store=store, model="qwen3.6-plus")
    queue = FakeEventQueue()

    await executor.execute(FakeRequestContext(metadata={"iac_code": {"cwd": str(tmp_path)}}), queue)

    candidate_started = next(
        dump(event)["metadata"]["iac_code"]["pipeline"]
        for event in queue.events
        if isinstance(event, TaskStatusUpdateEvent)
        and dump(event).get("metadata", {}).get("iac_code", {}).get("pipeline", {}).get("eventType")
        == "candidate_started"
    )
    assert [step["id"] for step in candidate_started["candidate"]["steps"]] == [
        "template_generating",
        "cost_estimating",
    ]


@pytest.mark.asyncio
async def test_executor_hydrates_translator_step_attempts_before_resuming_waiting_input(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("IAC_CODE_MODE", "pipeline")
    session_dir = tmp_path / "sidecar"
    waiting_event = {
        "schemaVersion": "1.0",
        "extensionUri": "urn:iac-code:a2a:pipeline-events:v1",
        "eventId": "evt-waiting",
        "sequence": 42,
        "createdAt": "2026-06-11T06:15:55Z",
        "eventType": "input_required",
        "scope": "step",
        "pipelineRunId": "ctx-1",
        "taskId": "task-1",
        "contextId": "ctx-1",
        "pipelineName": "selling",
        "status": "input_required",
        "step": {"id": "confirm_and_select", "runId": "step-confirm_and_select-2", "attempt": 2},
        "data": {"prompt": "请选择要部署的方案："},
    }
    journal = A2APipelineJournal(session_dir)
    journal.append(waiting_event)
    A2APipelineSnapshotStore(session_dir).save(reduce_pipeline_events([waiting_event]))
    fake_pipeline = FakePipeline(
        [
            PipelineEvent(
                type=PipelineEventType.USER_INPUT_RECEIVED,
                step_id="confirm_and_select",
                timestamp=1717821601.0,
                data={"selected_value": "已有VPC下新建VSwitch"},
            ),
            PipelineEvent(
                type=PipelineEventType.PIPELINE_COMPLETED,
                step_id=None,
                timestamp=1717821602.0,
                data={"total_steps": 5},
            ),
        ],
        session_dir=session_dir,
    )
    fake_pipeline.sidecar_status = "waiting_input"
    monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_pipeline", lambda *args, **kwargs: fake_pipeline)
    monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_agent_runtime", lambda options: _fake_runtime())

    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    executor = IacCodeA2AExecutor(task_store=store, model="qwen3.6-plus")
    queue = FakeEventQueue()

    await executor.execute(
        FakeRequestContext(
            task_id="task-1",
            context_id="ctx-1",
            text="已有VPC下新建VSwitch",
            metadata={"iac_code": {"cwd": str(tmp_path)}},
        ),
        queue,
    )

    input_received = next(
        dump(event)["metadata"]["iac_code"]["pipeline"]
        for event in queue.events
        if isinstance(event, TaskStatusUpdateEvent)
        and dump(event).get("metadata", {}).get("iac_code", {}).get("pipeline", {}).get("eventType") == "input_received"
    )
    assert fake_pipeline.resume_prompts == ["已有VPC下新建VSwitch"]
    assert input_received["step"]["runId"] == "step-confirm_and_select-2"
    assert input_received["step"]["attempt"] == 2


@pytest.mark.asyncio
async def test_executor_returns_input_required_for_retryable_stream_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("IAC_CODE_MODE", "pipeline")
    fake_pipeline = FakePipeline([TimeoutError("upstream timed out")], session_dir=tmp_path / "sidecar")
    monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_pipeline", lambda *args, **kwargs: fake_pipeline)
    monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_agent_runtime", lambda options: _fake_runtime())

    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    executor = IacCodeA2AExecutor(task_store=store, model="qwen3.6-plus")
    queue = FakeEventQueue()

    await executor.execute(FakeRequestContext(metadata={"iac_code": {"cwd": str(tmp_path)}}), queue)

    final_status = _status_events(queue)[-1]["status"]
    assert final_status["state"] == "TASK_STATE_INPUT_REQUIRED"
    assert final_status["message"]["parts"][0]["text"] == RETRY_TEXT
    record = await store.get_or_create_task(task_id="task-1", context_id="ctx-1")
    assert record.state == "input-required"


@pytest.mark.asyncio
async def test_executor_returns_input_required_for_retryable_pipeline_creation_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("IAC_CODE_MODE", "pipeline")

    def raise_timeout(*args, **kwargs):
        raise TimeoutError("pipeline setup timed out")

    monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_pipeline", raise_timeout)
    monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_agent_runtime", lambda options: _fake_runtime())

    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    executor = IacCodeA2AExecutor(task_store=store, model="qwen3.6-plus")
    queue = FakeEventQueue()

    await executor.execute(FakeRequestContext(metadata={"iac_code": {"cwd": str(tmp_path)}}), queue)

    final_status = _status_events(queue)[-1]["status"]
    assert final_status["state"] == "TASK_STATE_INPUT_REQUIRED"
    assert final_status["message"]["parts"][0]["text"] == RETRY_TEXT
    record = await store.get_or_create_task(task_id="task-1", context_id="ctx-1")
    assert record.state == "input-required"


@pytest.mark.asyncio
async def test_executor_returns_input_required_for_retryable_runtime_creation_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("IAC_CODE_MODE", "pipeline")

    def raise_timeout(options):
        raise TimeoutError("runtime setup timed out")

    monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_agent_runtime", raise_timeout)

    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    executor = IacCodeA2AExecutor(task_store=store, model="qwen3.6-plus")
    queue = FakeEventQueue()

    await executor.execute(FakeRequestContext(metadata={"iac_code": {"cwd": str(tmp_path)}}), queue)

    final_status = _status_events(queue)[-1]["status"]
    assert final_status["state"] == "TASK_STATE_INPUT_REQUIRED"
    assert final_status["message"]["parts"][0]["text"] == RETRY_TEXT
    record = await store.get_or_create_task(task_id="task-1", context_id="ctx-1")
    assert record.state == "input-required"


@pytest.mark.asyncio
async def test_executor_sanitizes_auth_looking_pipeline_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("IAC_CODE_MODE", "pipeline")
    fake_pipeline = FakePipeline(
        [ValueError("missing API key: secret-internal-detail")],
        session_dir=tmp_path / "sidecar",
    )
    monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_pipeline", lambda *args, **kwargs: fake_pipeline)
    monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_agent_runtime", lambda options: _fake_runtime())

    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    executor = IacCodeA2AExecutor(task_store=store, model="qwen3.6-plus")
    queue = FakeEventQueue()

    await executor.execute(FakeRequestContext(metadata={"iac_code": {"cwd": str(tmp_path)}}), queue)

    final_status = _status_events(queue)[-1]["status"]
    assert final_status["state"] == "TASK_STATE_FAILED"
    assert final_status["message"]["parts"][0]["text"] == AUTH_TEXT


@pytest.mark.asyncio
async def test_executor_persists_pipeline_failed_event_for_nonretryable_stream_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("IAC_CODE_MODE", "pipeline")
    session_dir = tmp_path / "sidecar"
    fake_pipeline = FakePipeline(
        [ValueError("planner crashed INTERNAL_TOKEN=tok-live /tmp/iac-code/work.py")],
        session_dir=session_dir,
    )
    monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_pipeline", lambda *args, **kwargs: fake_pipeline)
    monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_agent_runtime", lambda options: _fake_runtime())

    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    executor = IacCodeA2AExecutor(task_store=store, model="qwen3.6-plus")
    queue = FakeEventQueue()

    await executor.execute(FakeRequestContext(metadata={"iac_code": {"cwd": str(tmp_path)}}), queue)

    events = A2APipelineJournal(session_dir).read_all()
    assert events[-1]["eventType"] == "pipeline_failed"
    assert events[-1]["status"] == "failed"
    assert events[-1]["data"]["errorSummary"] == "ValueError: planner crashed INTERNAL_TOKEN=[REDACTED] [PATH]"
    assert events[-1]["data"]["errorDetails"]["type"] == "ValueError"
    assert events[-1]["data"]["errorDetails"]["errorId"]
    assert events[-1]["data"]["errorDetails"]["traceback"] == "Stack trace omitted from public event; see error_id."
    assert "tok-live" not in json.dumps(events[-1])
    assert "/tmp/iac-code" not in json.dumps(events[-1])
    snapshot = A2APipelineSnapshotStore(session_dir).load()
    assert snapshot is not None
    assert snapshot["status"] == "failed"
    assert _status_events(queue)[-1]["status"]["state"] == "TASK_STATE_FAILED"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("sidecar_status", "expected_state", "expected_snapshot_status", "expected_event_type"),
    [
        ("completed", "TASK_STATE_COMPLETED", "completed", "pipeline_completed"),
        ("failed", "TASK_STATE_FAILED", "failed", "pipeline_failed"),
        ("user_aborted", "TASK_STATE_CANCELED", "canceled", "pipeline_canceled"),
        ("canceled", "TASK_STATE_CANCELED", "canceled", "pipeline_canceled"),
    ],
)
async def test_executor_preserves_terminal_sidecar_recovery_state_on_followup(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    sidecar_status: str,
    expected_state: str,
    expected_snapshot_status: str,
    expected_event_type: str,
) -> None:
    monkeypatch.setenv("IAC_CODE_MODE", "pipeline")
    session_dir = tmp_path / "sidecar"
    A2APipelineJournal(session_dir).append(
        {
            "schemaVersion": "1.0",
            "extensionUri": "urn:iac-code:a2a:pipeline-events:v1",
            "eventId": "evt-terminal",
            "sequence": 1,
            "createdAt": "2026-06-08T10:00:00Z",
            "eventType": expected_event_type,
            "scope": "pipeline",
            "pipelineRunId": "ctx-1",
            "taskId": "task-1",
            "contextId": "ctx-1",
            "pipelineName": "selling",
            "status": expected_snapshot_status,
            "data": {"sidecarStatus": sidecar_status},
        }
    )
    fake_pipeline = FakePipeline([], session_dir=session_dir)
    fake_pipeline.sidecar_status = sidecar_status
    monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_pipeline", lambda *args, **kwargs: fake_pipeline)
    monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_agent_runtime", lambda options: _fake_runtime())

    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    executor = IacCodeA2AExecutor(task_store=store, model="qwen3.6-plus")
    queue = FakeEventQueue()

    await executor.execute(
        FakeRequestContext(
            task_id="task-1",
            context_id="ctx-1",
            text="new request",
            metadata={"iac_code": {"cwd": str(tmp_path)}},
        ),
        queue,
    )

    assert fake_pipeline.clear_sidecar_calls == 0
    assert fake_pipeline.run_prompts == []
    assert (session_dir / "a2a-events.jsonl").exists()
    assert _status_events(queue)[-1]["status"]["state"] == expected_state
    snapshot = A2APipelineSnapshotStore(session_dir).load()
    assert snapshot is not None
    assert snapshot["status"] == expected_snapshot_status
    last_event = A2APipelineJournal(session_dir).read_all()[-1]
    assert last_event["eventType"] == expected_event_type
    assert last_event["taskId"] == "task-1"
    record = await store.get_or_create_task(task_id="task-1", context_id="ctx-1")
    assert record.state in {"completed", "failed", "canceled"}


@pytest.mark.asyncio
async def test_executor_clears_previous_task_terminal_sidecar_and_runs_new_task(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("IAC_CODE_MODE", "pipeline")
    session_dir = tmp_path / "sidecar"
    journal = A2APipelineJournal(session_dir)
    previous_terminal = {
        "schemaVersion": "1.0",
        "extensionUri": "urn:iac-code:a2a:pipeline-events:v1",
        "eventId": "evt-old-terminal",
        "sequence": 1,
        "createdAt": "2026-06-08T10:00:00Z",
        "eventType": "pipeline_completed",
        "scope": "pipeline",
        "pipelineRunId": "ctx-1",
        "taskId": "task-old",
        "contextId": "ctx-1",
        "pipelineName": "selling",
        "status": "completed",
        "data": {"sidecarStatus": "completed"},
    }
    journal.append(previous_terminal)
    A2APipelineSnapshotStore(session_dir).save(
        {
            "schemaVersion": "1.0",
            "snapshotVersion": 1,
            "pipelineRunId": "ctx-1",
            "taskId": "task-old",
            "contextId": "ctx-1",
            "pipelineName": "selling",
            "status": "completed",
            "lastSequence": 1,
            "steps": [],
            "display": {"messages": [], "diagrams": [], "candidateDetails": [], "artifacts": []},
            "pendingInput": None,
            "control": {"activeCandidateRunIds": [], "rollbackHistory": [], "candidateRestarts": []},
            "seenEventIds": ["evt-old-terminal"],
        }
    )
    fake_pipeline = FakePipeline(
        [
            TextDeltaEvent(text="new output"),
            PipelineEvent(type=PipelineEventType.PIPELINE_COMPLETED, step_id=None, timestamp=1717821601.0, data={}),
        ],
        session_dir=session_dir,
    )
    fake_pipeline.sidecar_status = "completed"
    monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_pipeline", lambda *args, **kwargs: fake_pipeline)
    monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_agent_runtime", lambda options: _fake_runtime())

    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    executor = IacCodeA2AExecutor(task_store=store, model="qwen3.6-plus")
    queue = FakeEventQueue()

    await executor.execute(
        FakeRequestContext(
            task_id="task-new",
            context_id="ctx-1",
            text="new request",
            metadata={"iac_code": {"cwd": str(tmp_path)}},
        ),
        queue,
    )

    assert fake_pipeline.clear_sidecar_calls == 1
    assert fake_pipeline.run_prompts == ["new request"]
    event_types = [event["eventType"] for event in journal.read_all()]
    assert event_types == ["pipeline_completed", "text_delta", "pipeline_completed"]
    assert journal.read_all()[-1]["taskId"] == "task-new"
    assert _status_events(queue)[-1]["status"]["state"] == "TASK_STATE_COMPLETED"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("sidecar_status", "event_type", "event_status"),
    [
        ("waiting_input", "input_required", "waiting_input"),
        ("running", "pipeline_started", "working"),
        ("completed", "pipeline_completed", "completed"),
    ],
)
async def test_executor_replaces_restored_pipeline_when_sidecar_owner_mismatches(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    sidecar_status: str,
    event_type: str,
    event_status: str,
) -> None:
    monkeypatch.setenv("IAC_CODE_MODE", "pipeline")
    session_dir = tmp_path / "sidecar"
    old_event = {
        "schemaVersion": "1.0",
        "extensionUri": "urn:iac-code:a2a:pipeline-events:v1",
        "eventId": "evt-old-sidecar",
        "sequence": 1,
        "createdAt": "2026-06-08T10:00:00Z",
        "eventType": event_type,
        "scope": "pipeline",
        "pipelineRunId": "ctx-1",
        "taskId": "task-old",
        "contextId": "ctx-1",
        "pipelineName": "selling",
        "status": event_status,
        "data": {"prompt": "old choice"} if event_type == "input_required" else {},
    }
    A2APipelineJournal(session_dir).append(old_event)
    A2APipelineSnapshotStore(session_dir).save(reduce_pipeline_events([old_event]))

    class RestoredMemoryPipeline(FakePipeline):
        async def run(self, prompt: str):
            self.run_prompts.append(prompt)
            yield TextDeltaEvent(text="stale restored output")
            yield PipelineEvent(
                type=PipelineEventType.PIPELINE_COMPLETED,
                step_id=None,
                timestamp=1717821601.0,
                data={},
            )

    restored_pipeline = RestoredMemoryPipeline([], session_dir=session_dir)
    restored_pipeline.sidecar_status = sidecar_status
    fresh_pipeline = FakePipeline(
        [
            TextDeltaEvent(text="fresh output"),
            PipelineEvent(type=PipelineEventType.PIPELINE_COMPLETED, step_id=None, timestamp=1717821601.0, data={}),
        ],
        session_dir=session_dir,
    )
    create_resume_flags: list[bool | None] = []

    def fake_create_pipeline(*args, **kwargs):
        create_resume_flags.append(kwargs.get("resume_from_sidecar"))
        return restored_pipeline if len(create_resume_flags) == 1 else fresh_pipeline

    monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_pipeline", fake_create_pipeline)
    monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_agent_runtime", lambda options: _fake_runtime())

    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    executor = IacCodeA2AExecutor(task_store=store, model="qwen3.6-plus")

    await executor.execute(
        FakeRequestContext(
            task_id="task-new",
            context_id="ctx-1",
            text="new request",
            metadata={"iac_code": {"cwd": str(tmp_path)}},
        ),
        FakeEventQueue(),
    )

    assert create_resume_flags == [True, False]
    assert restored_pipeline.clear_sidecar_calls == 1
    assert restored_pipeline.run_prompts == []
    assert fresh_pipeline.run_prompts == ["new request"]
    record = await store.get_task_record("task-new")
    assert "".join(record.output_text) == "fresh output"


@pytest.mark.asyncio
async def test_executor_keeps_a2a_metadata_when_mismatch_clears_pipeline_sidecar(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("IAC_CODE_MODE", "pipeline")
    session_root = tmp_path / "session"
    sidecar_dir = session_root / "pipeline"
    a2a_dir = session_root / "a2a" / "pipeline"
    sidecar_dir.mkdir(parents=True)
    old_event = {
        "schemaVersion": "1.0",
        "extensionUri": "urn:iac-code:a2a:pipeline-events:v1",
        "eventId": "evt-old-sidecar",
        "sequence": 1,
        "createdAt": "2026-06-08T10:00:00Z",
        "eventType": "pipeline_completed",
        "scope": "pipeline",
        "pipelineRunId": "ctx-1",
        "taskId": "task-old",
        "contextId": "ctx-1",
        "pipelineName": "selling",
        "status": "completed",
        "data": {"sidecarStatus": "completed"},
    }
    A2APipelineJournal(a2a_dir).append(old_event)
    A2APipelineSnapshotStore(a2a_dir).save(reduce_pipeline_events([old_event]))

    class DeletingSidecarPipeline(FakePipeline):
        def clear_sidecar(self) -> None:
            super().clear_sidecar()
            shutil.rmtree(self.session.session_dir, ignore_errors=True)

    restored_pipeline = DeletingSidecarPipeline([], session_dir=sidecar_dir)
    restored_pipeline.sidecar_status = "completed"
    fresh_pipeline = FakePipeline(
        [
            TextDeltaEvent(text="fresh output"),
            PipelineEvent(type=PipelineEventType.PIPELINE_COMPLETED, step_id=None, timestamp=1717821601.0, data={}),
        ],
        session_dir=sidecar_dir,
    )
    create_resume_flags: list[bool | None] = []

    def fake_create_pipeline(*args, **kwargs):
        create_resume_flags.append(kwargs.get("resume_from_sidecar"))
        return restored_pipeline if len(create_resume_flags) == 1 else fresh_pipeline

    monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_pipeline", fake_create_pipeline)
    monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_agent_runtime", lambda options: _fake_runtime())

    executor = IacCodeA2AExecutor(task_store=A2ATaskStore(metrics=NoOpA2AMetrics()), model="qwen3.6-plus")

    await executor.execute(
        FakeRequestContext(
            task_id="task-new",
            context_id="ctx-1",
            text="new request",
            metadata={"iac_code": {"cwd": str(tmp_path)}},
        ),
        FakeEventQueue(),
    )

    assert create_resume_flags == [True, False]
    assert restored_pipeline.clear_sidecar_calls == 1
    events = A2APipelineJournal(a2a_dir).read_all()
    assert [event["taskId"] for event in events] == ["task-old", "task-new", "task-new"]
    assert not (sidecar_dir / "a2a-events.jsonl").exists()


@pytest.mark.asyncio
async def test_executor_does_not_duplicate_existing_terminal_recovery_event_when_snapshot_missing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("IAC_CODE_MODE", "pipeline")
    session_dir = tmp_path / "sidecar"
    journal = A2APipelineJournal(session_dir)
    journal.append(
        {
            "schemaVersion": "1.0",
            "extensionUri": "urn:iac-code:a2a:pipeline-events:v1",
            "eventId": "evt-terminal",
            "sequence": 1,
            "createdAt": "2026-06-08T10:00:00Z",
            "eventType": "pipeline_failed",
            "scope": "pipeline",
            "pipelineRunId": "ctx-1",
            "taskId": "task-1",
            "contextId": "ctx-1",
            "pipelineName": "selling",
            "status": "failed",
            "data": {"sidecarStatus": "failed", "recovered": True},
        }
    )
    fake_pipeline = FakePipeline([], session_dir=session_dir)
    fake_pipeline.sidecar_status = "failed"
    monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_pipeline", lambda *args, **kwargs: fake_pipeline)
    monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_agent_runtime", lambda options: _fake_runtime())

    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    executor = IacCodeA2AExecutor(task_store=store, model="qwen3.6-plus")
    queue = FakeEventQueue()

    await executor.execute(
        FakeRequestContext(
            task_id="task-1",
            context_id="ctx-1",
            text="new request",
            metadata={"iac_code": {"cwd": str(tmp_path)}},
        ),
        queue,
    )

    terminal_events = [event for event in journal.read_all() if event["eventType"] == "pipeline_failed"]
    assert len(terminal_events) == 1
    snapshot = A2APipelineSnapshotStore(session_dir).load()
    assert snapshot is not None
    assert snapshot["status"] == "failed"
    assert _status_events(queue)[-1]["status"]["state"] == "TASK_STATE_FAILED"


@pytest.mark.asyncio
async def test_executor_does_not_publish_conflicting_terminal_sidecar_recovery_event(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("IAC_CODE_MODE", "pipeline")
    session_dir = tmp_path / "sidecar"
    failed_event = {
        "schemaVersion": "1.0",
        "extensionUri": "urn:iac-code:a2a:pipeline-events:v1",
        "eventId": "evt-terminal",
        "sequence": 1,
        "createdAt": "2026-06-08T10:00:00Z",
        "eventType": "pipeline_failed",
        "scope": "pipeline",
        "pipelineRunId": "ctx-1",
        "taskId": "task-1",
        "contextId": "ctx-1",
        "pipelineName": "selling",
        "status": "failed",
        "data": {"source": "executor"},
    }
    journal = A2APipelineJournal(session_dir)
    journal.append(failed_event)
    A2APipelineSnapshotStore(session_dir).save(reduce_pipeline_events([failed_event]))
    fake_pipeline = FakePipeline([], session_dir=session_dir)
    fake_pipeline.sidecar_status = "completed"
    monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_pipeline", lambda *args, **kwargs: fake_pipeline)
    monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_agent_runtime", lambda options: _fake_runtime())

    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    executor = IacCodeA2AExecutor(task_store=store, model="qwen3.6-plus")
    queue = FakeEventQueue()

    await executor.execute(
        FakeRequestContext(
            task_id="task-1",
            context_id="ctx-1",
            text="new request",
            metadata={"iac_code": {"cwd": str(tmp_path)}},
        ),
        queue,
    )

    events = journal.read_all()
    assert [event["eventType"] for event in events] == ["pipeline_failed"]
    snapshot = A2APipelineSnapshotStore(session_dir).load()
    assert snapshot is not None
    assert snapshot["status"] == "failed"
    assert _status_events(queue)[-1]["status"]["state"] == "TASK_STATE_FAILED"


@pytest.mark.asyncio
async def test_executor_rebuilds_stale_snapshot_from_existing_terminal_recovery_event(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("IAC_CODE_MODE", "pipeline")
    session_dir = tmp_path / "sidecar"
    journal = A2APipelineJournal(session_dir)
    working_event = {
        "schemaVersion": "1.0",
        "extensionUri": "urn:iac-code:a2a:pipeline-events:v1",
        "eventId": "evt-working",
        "sequence": 1,
        "createdAt": "2026-06-08T10:00:00Z",
        "eventType": "pipeline_started",
        "scope": "pipeline",
        "pipelineRunId": "ctx-1",
        "taskId": "task-1",
        "contextId": "ctx-1",
        "pipelineName": "selling",
        "status": "working",
        "data": {},
    }
    terminal_event = dict(working_event)
    terminal_event.update(
        {
            "eventId": "evt-terminal",
            "sequence": 2,
            "eventType": "pipeline_failed",
            "status": "failed",
            "data": {"sidecarStatus": "failed", "recovered": True},
        }
    )
    other_context_terminal_event = dict(working_event)
    other_context_terminal_event.update(
        {
            "eventId": "evt-other-terminal",
            "sequence": 99,
            "eventType": "pipeline_completed",
            "pipelineRunId": "ctx-other",
            "taskId": "task-other",
            "contextId": "ctx-other",
            "status": "completed",
            "data": {"sidecarStatus": "completed", "recovered": True},
        }
    )
    journal.append(working_event)
    journal.append(terminal_event)
    journal.append(other_context_terminal_event)
    A2APipelineSnapshotStore(session_dir).save(
        {
            "schemaVersion": "1.0",
            "snapshotVersion": 1,
            "pipelineRunId": "ctx-1",
            "taskId": "task-1",
            "contextId": "ctx-1",
            "pipelineName": "selling",
            "status": "working",
            "lastSequence": 1,
            "steps": [],
            "display": {"messages": [], "diagrams": [], "candidateDetails": [], "artifacts": []},
            "pendingInput": None,
            "control": {"activeCandidateRunIds": [], "rollbackHistory": [], "candidateRestarts": []},
            "seenEventIds": ["evt-working"],
        }
    )
    fake_pipeline = FakePipeline([], session_dir=session_dir)
    fake_pipeline.sidecar_status = "failed"
    monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_pipeline", lambda *args, **kwargs: fake_pipeline)
    monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_agent_runtime", lambda options: _fake_runtime())

    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    executor = IacCodeA2AExecutor(task_store=store, model="qwen3.6-plus")
    queue = FakeEventQueue()

    await executor.execute(
        FakeRequestContext(
            task_id="task-1",
            context_id="ctx-1",
            text="new request",
            metadata={"iac_code": {"cwd": str(tmp_path)}},
        ),
        queue,
    )

    terminal_events = [event for event in journal.read_all() if event["eventType"] == "pipeline_failed"]
    assert len(terminal_events) == 1
    snapshot = A2APipelineSnapshotStore(session_dir).load()
    assert snapshot is not None
    assert snapshot["status"] == "failed"
    assert snapshot["lastSequence"] == 2
    assert snapshot["taskId"] == "task-1"
    assert snapshot["contextId"] == "ctx-1"
    assert _status_events(queue)[-1]["status"]["state"] == "TASK_STATE_FAILED"


@pytest.mark.asyncio
async def test_executor_does_not_rebuild_terminal_snapshot_from_unrepairable_journal(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("IAC_CODE_MODE", "pipeline")
    session_dir = tmp_path / "sidecar"
    working_event = {
        "schemaVersion": "1.0",
        "extensionUri": "urn:iac-code:a2a:pipeline-events:v1",
        "eventId": "evt-working",
        "sequence": 1,
        "createdAt": "2026-06-08T10:00:00Z",
        "eventType": "pipeline_started",
        "scope": "pipeline",
        "pipelineRunId": "ctx-1",
        "taskId": "task-1",
        "contextId": "ctx-1",
        "pipelineName": "selling",
        "status": "working",
        "data": {},
    }
    terminal_event = dict(working_event)
    terminal_event.update(
        {
            "eventId": "evt-terminal",
            "sequence": 3,
            "eventType": "pipeline_failed",
            "status": "failed",
            "data": {"sidecarStatus": "failed", "recovered": True},
        }
    )
    journal = A2APipelineJournal(session_dir)
    journal.append(working_event)
    journal.path.write_text(
        journal.path.read_text(encoding="utf-8")
        + "not-json\n"
        + json.dumps(terminal_event, ensure_ascii=False, separators=(",", ":"))
        + "\n",
        encoding="utf-8",
    )
    A2APipelineSnapshotStore(session_dir).save(reduce_pipeline_events([working_event]))

    class TerminalAfterRunPipeline(FakePipeline):
        async def run(self, prompt: str):
            self.run_prompts.append(prompt)
            self.sidecar_status = "failed"
            if False:
                yield None

    fake_pipeline = TerminalAfterRunPipeline([], session_dir=session_dir)
    monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_pipeline", lambda *args, **kwargs: fake_pipeline)
    monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_agent_runtime", lambda options: _fake_runtime())

    executor = IacCodeA2AExecutor(task_store=A2ATaskStore(metrics=NoOpA2AMetrics()), model="qwen3.6-plus")
    queue = FakeEventQueue()

    await executor.execute(
        FakeRequestContext(
            task_id="task-1",
            context_id="ctx-1",
            text="resume",
            metadata={"iac_code": {"cwd": str(tmp_path)}},
        ),
        queue,
    )

    snapshot = A2APipelineSnapshotStore(session_dir).load()
    assert snapshot is not None
    assert snapshot["status"] == "working"
    assert snapshot["lastSequence"] == 1
    assert _status_events(queue)[-1]["status"]["state"] == "TASK_STATE_FAILED"


@pytest.mark.asyncio
async def test_executor_repairs_same_task_terminal_sidecar_with_nonterminal_snapshot_without_rerun(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("IAC_CODE_MODE", "pipeline")
    session_dir = tmp_path / "sidecar"
    working_event = {
        "schemaVersion": "1.0",
        "extensionUri": "urn:iac-code:a2a:pipeline-events:v1",
        "eventId": "evt-working",
        "sequence": 1,
        "createdAt": "2026-06-08T10:00:00Z",
        "eventType": "pipeline_started",
        "scope": "pipeline",
        "pipelineRunId": "ctx-1",
        "taskId": "task-1",
        "contextId": "ctx-1",
        "pipelineName": "selling",
        "status": "working",
        "data": {},
    }
    journal = A2APipelineJournal(session_dir)
    journal.append(working_event)
    A2APipelineSnapshotStore(session_dir).save(reduce_pipeline_events([working_event]))
    fake_pipeline = FakePipeline(
        [
            TextDeltaEvent(text="should not rerun"),
            PipelineEvent(type=PipelineEventType.PIPELINE_COMPLETED, step_id=None, timestamp=1717821601.0, data={}),
        ],
        session_dir=session_dir,
    )
    fake_pipeline.sidecar_status = "completed"
    monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_pipeline", lambda *args, **kwargs: fake_pipeline)
    monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_agent_runtime", lambda options: _fake_runtime())

    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    executor = IacCodeA2AExecutor(task_store=store, model="qwen3.6-plus")
    queue = FakeEventQueue()

    await executor.execute(
        FakeRequestContext(
            task_id="task-1",
            context_id="ctx-1",
            text="new request",
            metadata={"iac_code": {"cwd": str(tmp_path)}},
        ),
        queue,
    )

    assert fake_pipeline.clear_sidecar_calls == 0
    assert fake_pipeline.run_prompts == []
    events = journal.read_all()
    assert [event["eventType"] for event in events] == ["pipeline_started", "pipeline_completed"]
    assert events[-1]["data"]["recovered"] is True
    snapshot = A2APipelineSnapshotStore(session_dir).load()
    assert snapshot is not None
    assert snapshot["status"] == "completed"
    assert _status_events(queue)[-1]["status"]["state"] == "TASK_STATE_COMPLETED"


@pytest.mark.asyncio
async def test_executor_repairs_terminal_sidecar_after_partial_nonterminal_stream(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("IAC_CODE_MODE", "pipeline")
    session_dir = tmp_path / "sidecar"

    class PartialTerminalPipeline(FakePipeline):
        async def run(self, prompt: str):
            self.run_prompts.append(prompt)
            yield TextDeltaEvent(text="partial output")
            self.sidecar_status = "failed"

    fake_pipeline = PartialTerminalPipeline([], session_dir=session_dir)
    monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_pipeline", lambda *args, **kwargs: fake_pipeline)
    monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_agent_runtime", lambda options: _fake_runtime())

    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    executor = IacCodeA2AExecutor(task_store=store, model="qwen3.6-plus")
    queue = FakeEventQueue()

    await executor.execute(FakeRequestContext(metadata={"iac_code": {"cwd": str(tmp_path)}}), queue)

    event_types = [event["eventType"] for event in A2APipelineJournal(session_dir).read_all()]
    assert event_types == ["text_delta", "pipeline_failed"]
    snapshot = A2APipelineSnapshotStore(session_dir).load()
    assert snapshot is not None
    assert snapshot["status"] == "failed"
    assert _status_events(queue)[-1]["status"]["state"] == "TASK_STATE_FAILED"


@pytest.mark.asyncio
async def test_executor_routes_waiting_sidecar_prompt_to_resume(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("IAC_CODE_MODE", "pipeline")
    fake_pipeline = FakePipeline(
        [
            PipelineEvent(
                type=PipelineEventType.PIPELINE_COMPLETED,
                step_id=None,
                timestamp=1717821601.0,
                data={},
            )
        ],
        session_dir=tmp_path / "sidecar",
    )
    fake_pipeline.sidecar_status = "waiting_input"
    input_event = {
        "schemaVersion": "1.0",
        "extensionUri": "urn:iac-code:a2a:pipeline-events:v1",
        "eventId": "evt-input",
        "sequence": 1,
        "createdAt": "2026-06-08T10:00:00Z",
        "eventType": "input_required",
        "scope": "pipeline",
        "pipelineRunId": "ctx-1",
        "taskId": "task-1",
        "contextId": "ctx-1",
        "pipelineName": "selling",
        "status": "waiting_input",
        "data": {"prompt": "choose"},
    }
    A2APipelineJournal(tmp_path / "sidecar").append(input_event)
    A2APipelineSnapshotStore(tmp_path / "sidecar").save(reduce_pipeline_events([input_event]))
    monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_pipeline", lambda *args, **kwargs: fake_pipeline)
    monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_agent_runtime", lambda options: _fake_runtime())

    executor = IacCodeA2AExecutor(task_store=A2ATaskStore(metrics=NoOpA2AMetrics()), model="qwen3.6-plus")

    await executor.execute(
        FakeRequestContext(text="selected", metadata={"iac_code": {"cwd": str(tmp_path)}}),
        FakeEventQueue(),
    )

    assert fake_pipeline.resume_prompts == ["selected"]
    assert fake_pipeline.run_prompts == []


@pytest.mark.asyncio
async def test_executor_does_not_resume_waiting_sidecar_when_restore_failed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from iac_code.pipeline.engine.session import RestoreResult

    monkeypatch.setenv("IAC_CODE_MODE", "pipeline")
    session_dir = tmp_path / "sidecar"
    fake_pipeline = FakePipeline(
        [
            PipelineEvent(
                type=PipelineEventType.PIPELINE_COMPLETED,
                step_id=None,
                timestamp=1717821601.0,
                data={},
            )
        ],
        session_dir=session_dir,
    )
    fake_pipeline.sidecar_status = "waiting_input"
    fake_pipeline.sidecar_restore_result = RestoreResult(ok=False, status="waiting_input", reason="invalid_context")
    input_event = {
        "schemaVersion": "1.0",
        "extensionUri": "urn:iac-code:a2a:pipeline-events:v1",
        "eventId": "evt-input",
        "sequence": 1,
        "createdAt": "2026-06-08T10:00:00Z",
        "eventType": "input_required",
        "scope": "pipeline",
        "pipelineRunId": "ctx-1",
        "taskId": "task-1",
        "contextId": "ctx-1",
        "pipelineName": "selling",
        "status": "waiting_input",
        "data": {"prompt": "choose"},
    }
    A2APipelineJournal(session_dir).append(input_event)
    A2APipelineSnapshotStore(session_dir).save(reduce_pipeline_events([input_event]))
    monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_pipeline", lambda *args, **kwargs: fake_pipeline)
    monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_agent_runtime", lambda options: _fake_runtime())

    executor = IacCodeA2AExecutor(task_store=A2ATaskStore(metrics=NoOpA2AMetrics()), model="qwen3.6-plus")
    queue = FakeEventQueue()

    await executor.execute(
        FakeRequestContext(text="selected", metadata={"iac_code": {"cwd": str(tmp_path)}}),
        queue,
    )

    assert fake_pipeline.resume_prompts == []
    assert fake_pipeline.run_prompts == []
    assert _status_events(queue)[-1]["status"]["state"] == "TASK_STATE_FAILED"


@pytest.mark.asyncio
async def test_executor_resumes_matching_waiting_sidecar_when_journal_has_partial_tail(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("IAC_CODE_MODE", "pipeline")
    session_dir = tmp_path / "sidecar"
    fake_pipeline = FakePipeline(
        [
            PipelineEvent(
                type=PipelineEventType.PIPELINE_COMPLETED,
                step_id=None,
                timestamp=1717821601.0,
                data={},
            )
        ],
        session_dir=session_dir,
    )
    fake_pipeline.sidecar_status = "waiting_input"
    input_event = {
        "schemaVersion": "1.0",
        "extensionUri": "urn:iac-code:a2a:pipeline-events:v1",
        "eventId": "evt-input",
        "sequence": 1,
        "createdAt": "2026-06-08T10:00:00Z",
        "eventType": "input_required",
        "scope": "pipeline",
        "pipelineRunId": "ctx-1",
        "taskId": "task-1",
        "contextId": "ctx-1",
        "pipelineName": "selling",
        "status": "waiting_input",
        "data": {"prompt": "choose"},
    }
    journal = A2APipelineJournal(session_dir)
    journal.append(input_event)
    journal.path.write_text(journal.path.read_text(encoding="utf-8") + '{"eventId":"evt-partial"', encoding="utf-8")
    A2APipelineSnapshotStore(session_dir).save(reduce_pipeline_events([input_event]))
    monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_pipeline", lambda *args, **kwargs: fake_pipeline)
    monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_agent_runtime", lambda options: _fake_runtime())

    executor = IacCodeA2AExecutor(task_store=A2ATaskStore(metrics=NoOpA2AMetrics()), model="qwen3.6-plus")

    await executor.execute(
        FakeRequestContext(text="selected", metadata={"iac_code": {"cwd": str(tmp_path)}}),
        FakeEventQueue(),
    )

    assert fake_pipeline.clear_sidecar_calls == 0
    assert fake_pipeline.resume_prompts == ["selected"]
    assert fake_pipeline.run_prompts == []


@pytest.mark.asyncio
async def test_executor_does_not_trust_snapshot_owner_when_journal_has_middle_corruption(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("IAC_CODE_MODE", "pipeline")
    session_dir = tmp_path / "sidecar"
    fake_pipeline = FakePipeline(
        [
            PipelineEvent(
                type=PipelineEventType.PIPELINE_COMPLETED,
                step_id=None,
                timestamp=1717821601.0,
                data={},
            )
        ],
        session_dir=session_dir,
    )
    fake_pipeline.sidecar_status = "waiting_input"
    old_input = {
        "schemaVersion": "1.0",
        "extensionUri": "urn:iac-code:a2a:pipeline-events:v1",
        "eventId": "evt-old-input",
        "sequence": 1,
        "createdAt": "2026-06-08T10:00:00Z",
        "eventType": "input_required",
        "scope": "pipeline",
        "pipelineRunId": "ctx-1",
        "taskId": "task-old",
        "contextId": "ctx-1",
        "pipelineName": "selling",
        "status": "waiting_input",
        "data": {"prompt": "old choice"},
    }
    new_event = dict(old_input)
    new_event.update(
        {
            "eventId": "evt-new",
            "sequence": 3,
            "taskId": "task-new",
            "status": "working",
            "eventType": "pipeline_started",
            "data": {},
        }
    )
    journal = A2APipelineJournal(session_dir)
    journal.append(old_input)
    journal.path.write_text(
        journal.path.read_text(encoding="utf-8")
        + "not-json\n"
        + json.dumps(new_event, ensure_ascii=False, separators=(",", ":"))
        + "\n",
        encoding="utf-8",
    )
    A2APipelineSnapshotStore(session_dir).save(reduce_pipeline_events([old_input]))
    monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_pipeline", lambda *args, **kwargs: fake_pipeline)
    monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_agent_runtime", lambda options: _fake_runtime())

    executor = IacCodeA2AExecutor(task_store=A2ATaskStore(metrics=NoOpA2AMetrics()), model="qwen3.6-plus")
    queue = FakeEventQueue()

    await executor.execute(
        FakeRequestContext(
            task_id="task-old",
            context_id="ctx-1",
            text="old followup",
            metadata={"iac_code": {"cwd": str(tmp_path)}},
        ),
        queue,
    )

    assert fake_pipeline.clear_sidecar_calls == 0
    assert fake_pipeline.resume_prompts == []
    assert fake_pipeline.run_prompts == []
    assert _status_events(queue)[-1]["status"]["state"] == "TASK_STATE_FAILED"


@pytest.mark.asyncio
@pytest.mark.parametrize("sidecar_status", ["waiting_input", "running"])
@pytest.mark.parametrize(
    ("terminal_event_type", "terminal_status", "expected_state"),
    [
        ("pipeline_completed", "completed", "TASK_STATE_COMPLETED"),
        ("pipeline_failed", "failed", "TASK_STATE_FAILED"),
        ("pipeline_canceled", "canceled", "TASK_STATE_CANCELED"),
    ],
)
async def test_executor_does_not_resume_nonterminal_sidecar_when_a2a_state_is_terminal(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    sidecar_status: str,
    terminal_event_type: str,
    terminal_status: str,
    expected_state: str,
) -> None:
    monkeypatch.setenv("IAC_CODE_MODE", "pipeline")
    session_dir = tmp_path / "sidecar"
    terminal_event = {
        "schemaVersion": "1.0",
        "extensionUri": "urn:iac-code:a2a:pipeline-events:v1",
        "eventId": "evt-terminal",
        "sequence": 1,
        "createdAt": "2026-06-08T10:00:00Z",
        "eventType": terminal_event_type,
        "scope": "pipeline",
        "pipelineRunId": "ctx-1",
        "taskId": "task-1",
        "contextId": "ctx-1",
        "pipelineName": "selling",
        "status": terminal_status,
        "data": {},
    }
    A2APipelineJournal(session_dir).append(terminal_event)
    A2APipelineSnapshotStore(session_dir).save(reduce_pipeline_events([terminal_event]))
    fake_pipeline = FakePipeline(
        [
            TextDeltaEvent(text="fresh output"),
            PipelineEvent(type=PipelineEventType.PIPELINE_COMPLETED, step_id=None, timestamp=1717821601.0, data={}),
        ],
        session_dir=session_dir,
    )
    fake_pipeline.sidecar_status = sidecar_status
    monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_pipeline", lambda *args, **kwargs: fake_pipeline)
    monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_agent_runtime", lambda options: _fake_runtime())

    executor = IacCodeA2AExecutor(task_store=A2ATaskStore(metrics=NoOpA2AMetrics()), model="qwen3.6-plus")
    queue = FakeEventQueue()

    await executor.execute(
        FakeRequestContext(
            task_id="task-1",
            context_id="ctx-1",
            text="retry",
            metadata={"iac_code": {"cwd": str(tmp_path)}},
        ),
        queue,
    )

    assert fake_pipeline.clear_sidecar_calls == 0
    assert fake_pipeline.resume_prompts == []
    assert fake_pipeline.continue_calls == 0
    assert fake_pipeline.run_prompts == []
    assert _status_events(queue)[-1]["status"]["state"] == expected_state
    events = A2APipelineJournal(session_dir).read_all()
    assert [event["eventType"] for event in events] == [terminal_event_type]
    snapshot = A2APipelineSnapshotStore(session_dir).load()
    assert snapshot is not None
    assert snapshot["status"] == terminal_status


@pytest.mark.asyncio
async def test_executor_clears_previous_task_waiting_sidecar_and_runs_new_task(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("IAC_CODE_MODE", "pipeline")
    session_dir = tmp_path / "sidecar"
    old_input = {
        "schemaVersion": "1.0",
        "extensionUri": "urn:iac-code:a2a:pipeline-events:v1",
        "eventId": "evt-old-input",
        "sequence": 1,
        "createdAt": "2026-06-08T10:00:00Z",
        "eventType": "input_required",
        "scope": "pipeline",
        "pipelineRunId": "ctx-1",
        "taskId": "task-old",
        "contextId": "ctx-1",
        "pipelineName": "selling",
        "status": "waiting_input",
        "data": {"prompt": "old choice"},
    }
    A2APipelineJournal(session_dir).append(old_input)
    A2APipelineSnapshotStore(session_dir).save(reduce_pipeline_events([old_input]))
    fake_pipeline = FakePipeline(
        [
            TextDeltaEvent(text="new output"),
            PipelineEvent(type=PipelineEventType.PIPELINE_COMPLETED, step_id=None, timestamp=1717821601.0, data={}),
        ],
        session_dir=session_dir,
    )
    fake_pipeline.sidecar_status = "waiting_input"
    monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_pipeline", lambda *args, **kwargs: fake_pipeline)
    monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_agent_runtime", lambda options: _fake_runtime())

    executor = IacCodeA2AExecutor(task_store=A2ATaskStore(metrics=NoOpA2AMetrics()), model="qwen3.6-plus")

    await executor.execute(
        FakeRequestContext(
            task_id="task-new",
            context_id="ctx-1",
            text="new request",
            metadata={"iac_code": {"cwd": str(tmp_path)}},
        ),
        FakeEventQueue(),
    )

    assert fake_pipeline.clear_sidecar_calls == 1
    assert fake_pipeline.resume_prompts == []
    assert fake_pipeline.run_prompts == ["new request"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("sidecar_status", "event_type", "event_status"),
    [
        ("waiting_input", "input_required", "waiting_input"),
        ("running", "pipeline_started", "working"),
    ],
)
async def test_executor_does_not_attach_current_sidecar_to_historical_task(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    sidecar_status: str,
    event_type: str,
    event_status: str,
) -> None:
    monkeypatch.setenv("IAC_CODE_MODE", "pipeline")
    session_dir = tmp_path / "sidecar"
    old_event = {
        "schemaVersion": "1.0",
        "extensionUri": "urn:iac-code:a2a:pipeline-events:v1",
        "eventId": "evt-old",
        "sequence": 1,
        "createdAt": "2026-06-08T10:00:00Z",
        "eventType": event_type,
        "scope": "pipeline",
        "pipelineRunId": "ctx-1",
        "taskId": "task-old",
        "contextId": "ctx-1",
        "pipelineName": "selling",
        "status": event_status,
        "data": {"prompt": "old choice"} if event_type == "input_required" else {},
    }
    current_event = dict(old_event)
    current_event.update(
        {
            "eventId": "evt-current",
            "sequence": 2,
            "taskId": "task-current",
            "status": event_status,
            "data": {"prompt": "current choice"} if event_type == "input_required" else {},
        }
    )
    journal = A2APipelineJournal(session_dir)
    journal.append(old_event)
    journal.append(current_event)
    A2APipelineSnapshotStore(session_dir).save(reduce_pipeline_events([old_event, current_event]))
    fake_pipeline = FakePipeline(
        [
            TextDeltaEvent(text="old followup output"),
            PipelineEvent(type=PipelineEventType.PIPELINE_COMPLETED, step_id=None, timestamp=1717821601.0, data={}),
        ],
        session_dir=session_dir,
    )
    fake_pipeline.sidecar_status = sidecar_status
    monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_pipeline", lambda *args, **kwargs: fake_pipeline)
    monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_agent_runtime", lambda options: _fake_runtime())

    executor = IacCodeA2AExecutor(task_store=A2ATaskStore(metrics=NoOpA2AMetrics()), model="qwen3.6-plus")

    await executor.execute(
        FakeRequestContext(
            task_id="task-old",
            context_id="ctx-1",
            text="old followup",
            metadata={"iac_code": {"cwd": str(tmp_path)}},
        ),
        FakeEventQueue(),
    )

    assert fake_pipeline.clear_sidecar_calls == 1
    assert fake_pipeline.resume_prompts == []
    assert fake_pipeline.continue_calls == 0
    assert fake_pipeline.run_prompts == ["old followup"]
    assert journal.read_all()[-1]["taskId"] == "task-old"


@pytest.mark.asyncio
async def test_executor_routes_running_sidecar_to_continue(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("IAC_CODE_MODE", "pipeline")
    fake_pipeline = FakePipeline(
        [
            PipelineEvent(
                type=PipelineEventType.PIPELINE_COMPLETED,
                step_id=None,
                timestamp=1717821601.0,
                data={},
            )
        ],
        session_dir=tmp_path / "sidecar",
    )
    fake_pipeline.sidecar_status = "running"
    running_event = {
        "schemaVersion": "1.0",
        "extensionUri": "urn:iac-code:a2a:pipeline-events:v1",
        "eventId": "evt-running",
        "sequence": 1,
        "createdAt": "2026-06-08T10:00:00Z",
        "eventType": "pipeline_started",
        "scope": "pipeline",
        "pipelineRunId": "ctx-1",
        "taskId": "task-1",
        "contextId": "ctx-1",
        "pipelineName": "selling",
        "status": "working",
        "data": {},
    }
    A2APipelineJournal(tmp_path / "sidecar").append(running_event)
    A2APipelineSnapshotStore(tmp_path / "sidecar").save(reduce_pipeline_events([running_event]))
    monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_pipeline", lambda *args, **kwargs: fake_pipeline)
    monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_agent_runtime", lambda options: _fake_runtime())

    executor = IacCodeA2AExecutor(task_store=A2ATaskStore(metrics=NoOpA2AMetrics()), model="qwen3.6-plus")

    await executor.execute(
        FakeRequestContext(text="not fresh input", metadata={"iac_code": {"cwd": str(tmp_path)}}),
        FakeEventQueue(),
    )

    assert fake_pipeline.continue_calls == 1
    assert fake_pipeline.continue_inputs == ["not fresh input"]
    assert fake_pipeline.run_prompts == ["not fresh input"]


@pytest.mark.asyncio
async def test_executor_preserves_running_sidecar_pause_as_input_required(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("IAC_CODE_MODE", "pipeline")
    pause_event = PipelineEvent(
        type=PipelineEventType.USER_INPUT_REQUIRED,
        step_id="deploying",
        timestamp=1717821601.0,
        data={
            "kind": "pipeline_pause_confirmation",
            "prompt": "Pipeline paused.",
            "reason": "judge failed: timeout after 90.0s",
            "paused": True,
            "options": [],
        },
    )
    fake_pipeline = FakePipeline([pause_event], session_dir=tmp_path / "sidecar")
    fake_pipeline.sidecar_status = "running"
    running_event = {
        "schemaVersion": "1.0",
        "extensionUri": "urn:iac-code:a2a:pipeline-events:v1",
        "eventId": "evt-running",
        "sequence": 1,
        "createdAt": "2026-06-08T10:00:00Z",
        "eventType": "pipeline_started",
        "scope": "pipeline",
        "pipelineRunId": "ctx-1",
        "taskId": "task-1",
        "contextId": "ctx-1",
        "pipelineName": "selling",
        "status": "working",
        "data": {},
    }
    A2APipelineJournal(tmp_path / "sidecar").append(running_event)
    A2APipelineSnapshotStore(tmp_path / "sidecar").save(reduce_pipeline_events([running_event]))
    monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_pipeline", lambda *args, **kwargs: fake_pipeline)
    monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_agent_runtime", lambda options: _fake_runtime())

    queue = FakeEventQueue()
    executor = IacCodeA2AExecutor(task_store=A2ATaskStore(metrics=NoOpA2AMetrics()), model="qwen3.6-plus")

    await executor.execute(
        FakeRequestContext(text="stop deploying", metadata={"iac_code": {"cwd": str(tmp_path)}}),
        queue,
    )

    assert fake_pipeline.continue_calls == 1
    events = A2APipelineJournal(tmp_path / "sidecar").read_all()
    assert events[-1]["eventType"] == "input_required"
    assert events[-1]["status"] == "input_required"
    assert events[-1]["data"]["kind"] == "pipeline_pause_confirmation"
    assert "timeout" in events[-1]["data"]["reason"]
    statuses = _status_events(queue)
    assert statuses[-1]["status"]["state"] == "TASK_STATE_INPUT_REQUIRED"


@pytest.mark.asyncio
async def test_executor_routes_waiting_input_pause_confirmation_through_interrupt_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("IAC_CODE_MODE", "pipeline")
    session_dir = tmp_path / "sidecar"
    pending = {
        "schemaVersion": "1.0",
        "extensionUri": "urn:iac-code:a2a:pipeline-events:v1",
        "eventId": "evt-pause",
        "sequence": 1,
        "createdAt": "2026-06-08T10:00:00Z",
        "eventType": "input_required",
        "scope": "step",
        "pipelineRunId": "ctx-1",
        "taskId": "task-1",
        "contextId": "ctx-1",
        "pipelineName": "selling",
        "status": "input_required",
        "step": {"runId": "step-deploying-1", "id": "deploying", "attempt": 1},
        "data": {
            "kind": "pipeline_pause_confirmation",
            "prompt": "Pipeline paused.",
            "reason": "judge failed: timeout",
            "paused": True,
            "options": [],
        },
    }
    A2APipelineJournal(session_dir).append(pending)
    A2APipelineSnapshotStore(session_dir).save(reduce_pipeline_events([pending]))
    fake_pipeline = FakePipeline(
        [
            PipelineEvent(
                type=PipelineEventType.PIPELINE_COMPLETED,
                step_id=None,
                timestamp=1717821601.0,
                data={},
            )
        ],
        session_dir=session_dir,
    )
    fake_pipeline.sidecar_status = "waiting_input"
    monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_pipeline", lambda *args, **kwargs: fake_pipeline)
    monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_agent_runtime", lambda options: _fake_runtime())

    executor = IacCodeA2AExecutor(task_store=A2ATaskStore(metrics=NoOpA2AMetrics()), model="qwen3.6-plus")
    await executor.execute(
        FakeRequestContext(text="rollback to design", metadata={"iac_code": {"cwd": str(tmp_path)}}),
        FakeEventQueue(),
    )

    assert fake_pipeline.continue_inputs == ["rollback to design"]
    assert fake_pipeline.resume_prompts == []


@pytest.mark.asyncio
async def test_executor_clears_previous_task_running_sidecar_and_runs_new_task(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("IAC_CODE_MODE", "pipeline")
    session_dir = tmp_path / "sidecar"
    old_running = {
        "schemaVersion": "1.0",
        "extensionUri": "urn:iac-code:a2a:pipeline-events:v1",
        "eventId": "evt-old-running",
        "sequence": 1,
        "createdAt": "2026-06-08T10:00:00Z",
        "eventType": "pipeline_started",
        "scope": "pipeline",
        "pipelineRunId": "ctx-1",
        "taskId": "task-old",
        "contextId": "ctx-1",
        "pipelineName": "selling",
        "status": "working",
        "data": {},
    }
    A2APipelineJournal(session_dir).append(old_running)
    A2APipelineSnapshotStore(session_dir).save(reduce_pipeline_events([old_running]))
    fake_pipeline = FakePipeline(
        [
            TextDeltaEvent(text="new output"),
            PipelineEvent(type=PipelineEventType.PIPELINE_COMPLETED, step_id=None, timestamp=1717821601.0, data={}),
        ],
        session_dir=session_dir,
    )
    fake_pipeline.sidecar_status = "running"
    monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_pipeline", lambda *args, **kwargs: fake_pipeline)
    monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_agent_runtime", lambda options: _fake_runtime())

    executor = IacCodeA2AExecutor(task_store=A2ATaskStore(metrics=NoOpA2AMetrics()), model="qwen3.6-plus")

    await executor.execute(
        FakeRequestContext(
            task_id="task-new",
            context_id="ctx-1",
            text="new request",
            metadata={"iac_code": {"cwd": str(tmp_path)}},
        ),
        FakeEventQueue(),
    )

    assert fake_pipeline.clear_sidecar_calls == 1
    assert fake_pipeline.continue_calls == 0
    assert fake_pipeline.run_prompts == ["new request"]


@pytest.mark.asyncio
async def test_pipeline_executor_routes_second_prompt_as_interrupt(tmp_path: Path) -> None:
    from iac_code.a2a.pipeline_events import PipelineA2AContext, PipelineEventTranslator
    from iac_code.a2a.pipeline_executor import A2APipelineRuntime, IacCodeA2APipelineExecutor
    from iac_code.a2a.pipeline_journal import A2APipelineJournal
    from iac_code.a2a.pipeline_snapshot import A2APipelineSnapshotStore
    from iac_code.a2a.pipeline_stream import PipelineA2AEventPublisher

    class InterruptiblePipeline(FakePipeline):
        def __init__(self, *, session_dir: Path) -> None:
            super().__init__([TextDeltaEvent(text="running")], session_dir=session_dir)
            self.interrupts: list[str] = []

        async def handle_user_interrupt(self, message: str) -> SimpleNamespace:
            self.interrupts.append(message)
            return SimpleNamespace(
                action="supplement",
                reason="added context",
                rollback_target=None,
                candidate_scope=None,
            )

    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    task = await store.get_or_create_task(task_id="task-1", context_id="ctx-1")
    ctx = await store.get_or_create_context(
        context_id="ctx-1",
        cwd=str(tmp_path),
        runtime_factory=lambda _session_id: _fake_runtime(),
    )
    ctx.active_task_id = "task-1"

    queue = FakeEventQueue()
    pipeline = InterruptiblePipeline(session_dir=tmp_path / "sidecar")
    publisher = PipelineA2AEventPublisher(
        event_queue=queue,
        translator=PipelineEventTranslator(
            PipelineA2AContext(
                pipeline_run_id="ctx-1",
                task_id="task-1",
                context_id="ctx-1",
                pipeline_name="selling",
            )
        ),
        journal=A2APipelineJournal(tmp_path / "pipeline"),
        snapshot_store=A2APipelineSnapshotStore(tmp_path / "pipeline"),
    )
    ctx.runtime = A2APipelineRuntime(agent_runtime=_fake_runtime(), pipeline=pipeline, publisher=publisher)
    store.mirror_context(ctx)

    executor = IacCodeA2APipelineExecutor(
        task_store=store,
        model="qwen3.6-plus",
        metrics=NoOpA2AMetrics(),
        artifact_store=None,
        push_notifier=None,
        permission_resolver=None,
        auto_approve_permissions=False,
        thinking_exposure_types=None,
    )

    await executor.execute(
        context=FakeRequestContext(
            text="please change cpu",
            metadata={"iac_code": {"cwd": str(tmp_path)}},
        ),
        event_queue=queue,
        task=task,
        task_id="task-1",
        context_id="ctx-1",
        cwd=str(tmp_path),
        prompt="please change cpu",
    )

    assert pipeline.interrupts == ["please change cpu"]
    event_types = [event["eventType"] for event in publisher.journal.read_all()]
    assert event_types == ["interrupt_received", "interrupt_classified"]


@pytest.mark.asyncio
async def test_pipeline_executor_publishes_input_required_for_live_paused_interrupt(tmp_path: Path) -> None:
    from iac_code.a2a.pipeline_events import PipelineA2AContext, PipelineEventTranslator
    from iac_code.a2a.pipeline_executor import A2APipelineRuntime, IacCodeA2APipelineExecutor
    from iac_code.a2a.pipeline_journal import A2APipelineJournal
    from iac_code.a2a.pipeline_snapshot import A2APipelineSnapshotStore
    from iac_code.a2a.pipeline_stream import PipelineA2AEventPublisher

    class PausingPipeline(FakePipeline):
        def __init__(self, *, session_dir: Path) -> None:
            super().__init__([TextDeltaEvent(text="running")], session_dir=session_dir)
            self.interrupts: list[str] = []
            self.saved_pause_verdicts: list[SimpleNamespace] = []

        async def handle_user_interrupt(self, message: str) -> SimpleNamespace:
            self.interrupts.append(message)
            return SimpleNamespace(
                action="continue",
                reason="judge failed: timeout",
                rollback_target=None,
                candidate_scope=None,
                paused=True,
            )

        async def save_interrupt_pause(self, verdict: SimpleNamespace) -> PipelineEvent:
            self.saved_pause_verdicts.append(verdict)
            return PipelineEvent(
                type=PipelineEventType.USER_INPUT_REQUIRED,
                step_id="deploying",
                timestamp=1717821601.0,
                data={
                    "kind": "pipeline_pause_confirmation",
                    "prompt": "Pipeline paused.",
                    "reason": verdict.reason,
                    "paused": True,
                    "options": [],
                },
            )

    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    task = await store.get_or_create_task(task_id="task-1", context_id="ctx-1")
    task.state = "working"
    task.active_task = asyncio.current_task()
    ctx = await store.get_or_create_context(
        context_id="ctx-1",
        cwd=str(tmp_path),
        runtime_factory=lambda _session_id: _fake_runtime(),
    )
    ctx.active_task_id = "task-1"

    queue = FakeEventQueue()
    pipeline = PausingPipeline(session_dir=tmp_path / "sidecar")
    publisher = PipelineA2AEventPublisher(
        event_queue=queue,
        translator=PipelineEventTranslator(
            PipelineA2AContext(
                pipeline_run_id="ctx-1",
                task_id="task-1",
                context_id="ctx-1",
                pipeline_name="selling",
            )
        ),
        journal=A2APipelineJournal(tmp_path / "pipeline"),
        snapshot_store=A2APipelineSnapshotStore(tmp_path / "pipeline"),
    )
    ctx.runtime = A2APipelineRuntime(agent_runtime=_fake_runtime(), pipeline=pipeline, publisher=publisher)
    store.mirror_task(task)
    store.mirror_context(ctx)

    executor = IacCodeA2APipelineExecutor(
        task_store=store,
        model="qwen3.6-plus",
        metrics=NoOpA2AMetrics(),
        artifact_store=None,
        push_notifier=None,
        permission_resolver=None,
        auto_approve_permissions=False,
        thinking_exposure_types=None,
    )

    await executor.execute(
        context=FakeRequestContext(
            text="please stop",
            metadata={"iac_code": {"cwd": str(tmp_path)}},
        ),
        event_queue=queue,
        task=task,
        task_id="task-1",
        context_id="ctx-1",
        cwd=str(tmp_path),
        prompt="please stop",
    )

    assert pipeline.interrupts == ["please stop"]
    assert len(pipeline.saved_pause_verdicts) == 1
    event_types = [event["eventType"] for event in publisher.journal.read_all()]
    assert event_types == ["interrupt_received", "interrupt_classified", "input_required"]
    assert publisher.snapshot_store.load()["pendingInput"]["kind"] == "pipeline_pause_confirmation"
    assert task.state == "input-required"


@pytest.mark.asyncio
async def test_live_paused_interrupt_releases_active_task_and_next_reply_clears_pending_input(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("IAC_CODE_MODE", "pipeline")

    class PausingPipeline(FakePipeline):
        def __init__(self, *, session_dir: Path) -> None:
            super().__init__([], session_dir=session_dir)
            self.primary_stream = CloseableEventStream([TextDeltaEvent(text="before pause")])
            self.resume_stream = CloseableEventStream(
                [
                    PipelineEvent(
                        type=PipelineEventType.USER_INPUT_RECEIVED,
                        step_id="deploying",
                        timestamp=1717821602.0,
                        data={"kind": "pipeline_pause_confirmation", "user_input_length": 8},
                    ),
                    PipelineEvent(
                        type=PipelineEventType.PIPELINE_COMPLETED,
                        step_id=None,
                        timestamp=1717821603.0,
                        data={},
                    ),
                ],
                wait_until_closed=False,
            )
            self.interrupts: list[str] = []
            self.saved_pause_verdicts: list[SimpleNamespace] = []

        def run(self, prompt: str):
            self.run_prompts.append(prompt)
            return self.primary_stream

        def continue_from_sidecar(self, user_input: str | None = None):
            self.continue_calls += 1
            self.continue_inputs.append(user_input)
            self.sidecar_status = "running"
            return self.resume_stream

        async def handle_user_interrupt(self, message: str) -> SimpleNamespace:
            self.interrupts.append(message)
            return SimpleNamespace(
                action="continue",
                reason="judge failed: timeout",
                rollback_target=None,
                candidate_scope=None,
                paused=True,
            )

        async def save_interrupt_pause(self, verdict: SimpleNamespace) -> PipelineEvent:
            self.saved_pause_verdicts.append(verdict)
            self.sidecar_status = "waiting_input"
            return PipelineEvent(
                type=PipelineEventType.USER_INPUT_REQUIRED,
                step_id="deploying",
                timestamp=1717821601.0,
                data={
                    "kind": "pipeline_pause_confirmation",
                    "prompt": "Pipeline paused.",
                    "reason": verdict.reason,
                    "paused": True,
                    "options": [],
                },
            )

    pipeline = PausingPipeline(session_dir=tmp_path / "sidecar")
    monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_pipeline", lambda *args, **kwargs: pipeline)
    monkeypatch.setattr("iac_code.a2a.executor.create_agent_runtime", lambda options: _fake_runtime())
    monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_agent_runtime", lambda options: _fake_runtime())

    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    executor = IacCodeA2AExecutor(task_store=store, model="qwen3.6-plus")
    queue = FakeEventQueue()
    first = asyncio.create_task(
        executor.execute(
            FakeRequestContext(task_id="task-1", context_id="ctx-1", metadata={"iac_code": {"cwd": str(tmp_path)}}),
            queue,
        )
    )
    await _wait_for_output_text(await store.get_or_create_task(task_id="task-1", context_id="ctx-1"), "before pause")

    await executor.execute(
        FakeRequestContext(
            task_id="task-1",
            context_id="ctx-1",
            text="please pause",
            metadata={"iac_code": {"cwd": str(tmp_path)}},
        ),
        queue,
    )
    await asyncio.wait_for(first, timeout=1)
    ctx = await store.get_or_create_context(context_id="ctx-1", cwd=str(tmp_path), runtime_factory=lambda _sid: None)
    assert ctx.active_task_id is None
    assert pipeline.primary_stream.closed is True
    assert pipeline.sidecar_status == "waiting_input"
    assert pipeline.interrupts == ["please pause"]
    assert (
        A2APipelineSnapshotStore(tmp_path / "sidecar").load()["pendingInput"]["kind"] == "pipeline_pause_confirmation"
    )

    await executor.execute(
        FakeRequestContext(
            task_id="task-1",
            context_id="ctx-1",
            text="continue",
            metadata={"iac_code": {"cwd": str(tmp_path)}},
        ),
        queue,
    )

    events = A2APipelineJournal(tmp_path / "sidecar").read_all()
    assert [event["eventType"] for event in events][-2:] == ["input_received", "pipeline_completed"]
    assert A2APipelineSnapshotStore(tmp_path / "sidecar").load()["pendingInput"] is None
    assert pipeline.continue_inputs == ["continue"]


@pytest.mark.asyncio
async def test_active_pause_continuation_keeps_active_owner_until_continuation_finishes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("IAC_CODE_MODE", "pipeline")

    class BlockingCloseStream(CloseableEventStream):
        def __init__(self, events) -> None:
            super().__init__(events)
            self.allow_close = asyncio.Event()

        async def aclose(self) -> None:
            self.closed = True
            self.closed_event.set()
            await self.allow_close.wait()

    class PausingPipeline(FakePipeline):
        def __init__(self, *, session_dir: Path) -> None:
            super().__init__([], session_dir=session_dir)
            self.primary_stream = BlockingCloseStream([TextDeltaEvent(text="before pause")])
            self.continuation_started = asyncio.Event()
            self.finish_continuation = asyncio.Event()

        def run(self, prompt: str):
            return self.primary_stream

        async def handle_user_interrupt(self, message: str) -> SimpleNamespace:
            return SimpleNamespace(action="continue", reason="pause requested", paused=True)

        async def save_interrupt_pause(self, verdict: SimpleNamespace) -> PipelineEvent:
            self.sidecar_status = "waiting_input"
            return PipelineEvent(
                type=PipelineEventType.USER_INPUT_REQUIRED,
                step_id="deploying",
                timestamp=1717821601.0,
                data={
                    "kind": "pipeline_pause_confirmation",
                    "prompt": "Pipeline paused.",
                    "reason": verdict.reason,
                    "paused": True,
                    "options": [],
                },
            )

        def continue_from_sidecar(self, user_input: str | None = None):
            self.continue_calls += 1
            self.continue_inputs.append(user_input)
            self.sidecar_status = "running"

            async def stream():
                self.continuation_started.set()
                yield PipelineEvent(
                    type=PipelineEventType.USER_INPUT_RECEIVED,
                    step_id="deploying",
                    timestamp=1717821602.0,
                    data={"kind": "pipeline_pause_confirmation", "user_input_length": len(user_input or "")},
                )
                await self.finish_continuation.wait()
                yield PipelineEvent(
                    type=PipelineEventType.PIPELINE_COMPLETED,
                    step_id=None,
                    timestamp=1717821603.0,
                    data={},
                )

            return stream()

    pipeline = PausingPipeline(session_dir=tmp_path / "sidecar")
    monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_pipeline", lambda *args, **kwargs: pipeline)
    monkeypatch.setattr("iac_code.a2a.executor.create_agent_runtime", lambda options: _fake_runtime())
    monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_agent_runtime", lambda options: _fake_runtime())

    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    executor = IacCodeA2AExecutor(task_store=store, model="qwen3.6-plus")
    queue = FakeEventQueue()
    first = asyncio.create_task(
        executor.execute(
            FakeRequestContext(task_id="task-1", context_id="ctx-1", metadata={"iac_code": {"cwd": str(tmp_path)}}),
            queue,
        )
    )
    active_task = await store.get_or_create_task(task_id="task-1", context_id="ctx-1")
    await _wait_for_output_text(active_task, "before pause")

    await executor.execute(
        FakeRequestContext(
            task_id="task-1",
            context_id="ctx-1",
            text="please pause",
            metadata={"iac_code": {"cwd": str(tmp_path)}},
        ),
        queue,
    )

    continuation = asyncio.create_task(
        executor.execute(
            FakeRequestContext(
                task_id="task-1",
                context_id="ctx-1",
                text="continue",
                metadata={"iac_code": {"cwd": str(tmp_path)}},
            ),
            queue,
        )
    )
    try:
        await asyncio.wait_for(pipeline.continuation_started.wait(), timeout=1)
        pipeline.primary_stream.allow_close.set()
        await asyncio.wait_for(first, timeout=1)

        ctx = await store.get_or_create_context(
            context_id="ctx-1", cwd=str(tmp_path), runtime_factory=lambda _sid: None
        )
        active_task = await store.get_or_create_task(task_id="task-1", context_id="ctx-1")
        assert ctx.active_task_id == "task-1"
        assert active_task.active_task is continuation

        pipeline.finish_continuation.set()
        await asyncio.wait_for(continuation, timeout=1)
        assert ctx.active_task_id is None
        assert active_task.active_task is None
    finally:
        pipeline.primary_stream.allow_close.set()
        pipeline.finish_continuation.set()
        for runner in (first, continuation):
            if not runner.done():
                runner.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await runner


@pytest.mark.asyncio
async def test_active_task_route_continues_pending_pause_confirmation(tmp_path: Path) -> None:
    from iac_code.a2a.pipeline_events import PipelineA2AContext, PipelineEventTranslator
    from iac_code.a2a.pipeline_executor import A2APipelineRuntime, IacCodeA2APipelineExecutor
    from iac_code.a2a.pipeline_stream import PipelineA2AEventPublisher

    class PausedPipeline(FakePipeline):
        def __init__(self, *, session_dir: Path) -> None:
            super().__init__([], session_dir=session_dir)
            self.sidecar_status = "waiting_input"
            self.interrupts: list[str] = []

        async def handle_user_interrupt(self, message: str) -> SimpleNamespace:
            self.interrupts.append(message)
            return SimpleNamespace(action="continue", reason="should not run")

        def continue_from_sidecar(self, user_input: str | None = None):
            self.continue_calls += 1
            self.continue_inputs.append(user_input)

            async def stream():
                yield PipelineEvent(
                    type=PipelineEventType.USER_INPUT_RECEIVED,
                    step_id="deploying",
                    timestamp=1717821602.0,
                    data={"kind": "pipeline_pause_confirmation", "user_input_length": len(user_input or "")},
                )
                yield PipelineEvent(
                    type=PipelineEventType.PIPELINE_COMPLETED,
                    step_id=None,
                    timestamp=1717821603.0,
                    data={},
                )

            return stream()

    sidecar_dir = tmp_path / "sidecar"
    pending = {
        "schemaVersion": "1.0",
        "extensionUri": "urn:iac-code:a2a:pipeline-events:v1",
        "eventId": "evt-pause",
        "sequence": 1,
        "createdAt": "2026-06-08T10:00:00Z",
        "eventType": "input_required",
        "scope": "pipeline",
        "pipelineRunId": "ctx-1",
        "taskId": "task-1",
        "contextId": "ctx-1",
        "pipelineName": "selling",
        "status": "input_required",
        "data": {
            "kind": "pipeline_pause_confirmation",
            "prompt": "Pipeline paused.",
            "reason": "judge failed: timeout",
            "paused": True,
            "options": [],
        },
    }
    A2APipelineJournal(sidecar_dir).append(pending)
    A2APipelineSnapshotStore(sidecar_dir).save(reduce_pipeline_events([pending]))
    pipeline = PausedPipeline(session_dir=sidecar_dir)
    queue = FakeEventQueue()
    publisher = PipelineA2AEventPublisher(
        event_queue=queue,
        translator=PipelineEventTranslator(
            PipelineA2AContext(
                pipeline_run_id="ctx-1",
                task_id="task-1",
                context_id="ctx-1",
                pipeline_name="selling",
            )
        ),
        journal=A2APipelineJournal(sidecar_dir),
        snapshot_store=A2APipelineSnapshotStore(sidecar_dir),
    )
    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    task = await store.get_or_create_task(task_id="task-1", context_id="ctx-1")
    task.state = "input-required"
    task.active_task = asyncio.current_task()
    ctx = await store.get_or_create_context(
        context_id="ctx-1",
        cwd=str(tmp_path),
        runtime_factory=lambda _session_id: _fake_runtime(),
    )
    ctx.active_task_id = "task-1"
    ctx.runtime = A2APipelineRuntime(agent_runtime=_fake_runtime(), pipeline=pipeline, publisher=publisher)
    store.mirror_task(task)
    store.mirror_context(ctx)

    executor = IacCodeA2APipelineExecutor(
        task_store=store,
        model="qwen3.6-plus",
        metrics=NoOpA2AMetrics(),
        artifact_store=None,
        push_notifier=None,
        permission_resolver=None,
        auto_approve_permissions=False,
        thinking_exposure_types=None,
    )

    await executor.execute(
        context=FakeRequestContext(text="continue", metadata={"iac_code": {"cwd": str(tmp_path)}}),
        event_queue=queue,
        task=task,
        task_id="task-1",
        context_id="ctx-1",
        cwd=str(tmp_path),
        prompt="continue",
    )

    assert pipeline.interrupts == []
    assert pipeline.continue_inputs == ["continue"]
    events = A2APipelineJournal(sidecar_dir).read_all()
    assert [event["eventType"] for event in events][-2:] == ["input_received", "pipeline_completed"]
    assert A2APipelineSnapshotStore(sidecar_dir).load()["pendingInput"] is None


@pytest.mark.asyncio
async def test_active_pause_confirmation_failure_marks_task_and_pipeline_failed(tmp_path: Path) -> None:
    from iac_code.a2a.pipeline_events import PipelineA2AContext, PipelineEventTranslator
    from iac_code.a2a.pipeline_executor import A2APipelineRuntime, IacCodeA2APipelineExecutor
    from iac_code.a2a.pipeline_stream import PipelineA2AEventPublisher

    class FailingPausedPipeline(FakePipeline):
        def __init__(self, *, session_dir: Path) -> None:
            super().__init__([], session_dir=session_dir)
            self.sidecar_status = "waiting_input"

        def continue_from_sidecar(self, user_input: str | None = None):
            self.continue_calls += 1
            self.continue_inputs.append(user_input)

            async def stream():
                raise RuntimeError("pause continuation failed token=secret-value")
                if False:
                    yield

            return stream()

    sidecar_dir = tmp_path / "sidecar"
    pending = {
        "schemaVersion": "1.0",
        "extensionUri": "urn:iac-code:a2a:pipeline-events:v1",
        "eventId": "evt-pause",
        "sequence": 1,
        "createdAt": "2026-06-08T10:00:00Z",
        "eventType": "input_required",
        "scope": "pipeline",
        "pipelineRunId": "ctx-1",
        "taskId": "task-1",
        "contextId": "ctx-1",
        "pipelineName": "selling",
        "status": "input_required",
        "data": {"kind": "pipeline_pause_confirmation", "prompt": "Pipeline paused."},
    }
    A2APipelineJournal(sidecar_dir).append(pending)
    A2APipelineSnapshotStore(sidecar_dir).save(reduce_pipeline_events([pending]))
    queue = FakeEventQueue()
    publisher = PipelineA2AEventPublisher(
        event_queue=queue,
        translator=PipelineEventTranslator(
            PipelineA2AContext(
                pipeline_run_id="ctx-1",
                task_id="task-1",
                context_id="ctx-1",
                pipeline_name="selling",
            )
        ),
        journal=A2APipelineJournal(sidecar_dir),
        snapshot_store=A2APipelineSnapshotStore(sidecar_dir),
    )
    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    task = await store.get_or_create_task(task_id="task-1", context_id="ctx-1")
    task.state = "input-required"
    ctx = await store.get_or_create_context(
        context_id="ctx-1",
        cwd=str(tmp_path),
        runtime_factory=lambda _session_id: _fake_runtime(),
    )
    ctx.active_task_id = "task-1"
    ctx.runtime = A2APipelineRuntime(
        agent_runtime=_fake_runtime(),
        pipeline=FailingPausedPipeline(session_dir=sidecar_dir),
        publisher=publisher,
    )
    store.mirror_task(task)
    store.mirror_context(ctx)
    executor = IacCodeA2APipelineExecutor(
        task_store=store,
        model="qwen3.6-plus",
        metrics=NoOpA2AMetrics(),
        artifact_store=None,
        push_notifier=None,
        permission_resolver=None,
        auto_approve_permissions=False,
        thinking_exposure_types=None,
    )

    await executor.execute(
        context=FakeRequestContext(text="continue", metadata={"iac_code": {"cwd": str(tmp_path)}}),
        event_queue=queue,
        task=task,
        task_id="task-1",
        context_id="ctx-1",
        cwd=str(tmp_path),
        prompt="continue",
    )

    assert task.state == "failed"
    journal_events = A2APipelineJournal(sidecar_dir).read_all()
    assert journal_events[-1]["eventType"] == "pipeline_failed"
    snapshot = A2APipelineSnapshotStore(sidecar_dir).load()
    assert snapshot["status"] == "failed"
    assert snapshot["pendingInput"] is None
    assert any(
        dump(event).get("metadata", {}).get("iac_code", {}).get("pipeline", {}).get("eventType") == "pipeline_failed"
        for event in queue.events
    )


@pytest.mark.asyncio
async def test_pipeline_executor_publishes_interrupt_received_before_slow_judge(tmp_path: Path) -> None:
    from iac_code.a2a.pipeline_events import PipelineA2AContext, PipelineEventTranslator
    from iac_code.a2a.pipeline_executor import A2APipelineRuntime, IacCodeA2APipelineExecutor
    from iac_code.a2a.pipeline_journal import A2APipelineJournal
    from iac_code.a2a.pipeline_snapshot import A2APipelineSnapshotStore
    from iac_code.a2a.pipeline_stream import PipelineA2AEventPublisher

    class SlowInterruptPipeline(FakePipeline):
        def __init__(self, *, session_dir: Path) -> None:
            super().__init__([], session_dir=session_dir)
            self.judge_started = asyncio.Event()
            self.finish_judge = asyncio.Event()

        async def handle_user_interrupt(self, message: str) -> SimpleNamespace:
            self.judge_started.set()
            await self.finish_judge.wait()
            return SimpleNamespace(
                action="continue",
                reason="not relevant",
                rollback_target=None,
                candidate_scope=None,
                supplement_target=None,
            )

    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    task = await store.get_or_create_task(task_id="task-1", context_id="ctx-1")
    task.state = "working"
    task.active_task = asyncio.current_task()
    ctx = await store.get_or_create_context(
        context_id="ctx-1",
        cwd=str(tmp_path),
        runtime_factory=lambda _session_id: _fake_runtime(),
    )
    ctx.active_task_id = "task-1"
    pipeline = SlowInterruptPipeline(session_dir=tmp_path / "sidecar")
    publisher = PipelineA2AEventPublisher(
        event_queue=FakeEventQueue(),
        translator=PipelineEventTranslator(
            PipelineA2AContext(
                pipeline_run_id="ctx-1",
                task_id="task-1",
                context_id="ctx-1",
                pipeline_name="selling",
            )
        ),
        journal=A2APipelineJournal(tmp_path / "pipeline"),
        snapshot_store=A2APipelineSnapshotStore(tmp_path / "pipeline"),
    )
    ctx.runtime = A2APipelineRuntime(agent_runtime=_fake_runtime(), pipeline=pipeline, publisher=publisher)
    store.mirror_task(task)
    store.mirror_context(ctx)

    executor = IacCodeA2APipelineExecutor(
        task_store=store,
        model="qwen3.6-plus",
        metrics=NoOpA2AMetrics(),
        artifact_store=None,
        push_notifier=None,
        permission_resolver=None,
        auto_approve_permissions=False,
        thinking_exposure_types=None,
    )
    interrupt_task = asyncio.create_task(
        executor.execute(
            context=FakeRequestContext(text="hello", metadata={"iac_code": {"cwd": str(tmp_path)}}),
            event_queue=FakeEventQueue(),
            task=task,
            task_id="task-1",
            context_id="ctx-1",
            cwd=str(tmp_path),
            prompt="hello",
        )
    )
    try:
        await asyncio.wait_for(pipeline.judge_started.wait(), timeout=1)
        assert [event["eventType"] for event in publisher.journal.read_all()] == ["interrupt_received"]
    finally:
        pipeline.finish_judge.set()
        await asyncio.wait_for(interrupt_task, timeout=1)

    assert [event["eventType"] for event in publisher.journal.read_all()] == [
        "interrupt_received",
        "interrupt_classified",
    ]


@pytest.mark.asyncio
async def test_pipeline_executor_stops_at_ask_user_question_without_holding_active_task(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from iac_code.a2a.pipeline_executor import IacCodeA2APipelineExecutor

    class AskingPipeline(FakePipeline):
        def __init__(self, *, session_dir: Path) -> None:
            super().__init__([], session_dir=session_dir)
            self.question_ready = asyncio.Event()
            self.answers: list[dict[str, str] | None] = []
            self.future: asyncio.Future[dict[str, str] | None] | None = None
            self.closed = asyncio.Event()

        async def run(self, prompt: str):
            self.run_prompts.append(prompt)
            self.future = asyncio.get_running_loop().create_future()
            self.question_ready.set()
            answer = None
            try:
                yield AskUserQuestionEvent(
                    tool_use_id="ask-1",
                    question="请选择部署目标",
                    options=[
                        {"id": "nginx", "label": "Nginx 网站"},
                        {"id": "ecs", "label": "ECS 应用"},
                    ],
                    allow_free_text=True,
                    free_text_prompt="也可以直接描述目标",
                    response_future=self.future,
                )
                answer = await self.future
                self.answers.append(answer)
                yield TextDeltaEvent(text=answer["selected_id"] if answer else "cancelled")
                yield PipelineEvent(
                    type=PipelineEventType.PIPELINE_COMPLETED,
                    step_id=None,
                    timestamp=1717821602.0,
                    data={},
                )
            finally:
                self.closed.set()
                if answer is None and self.future is not None and not self.future.done():
                    self.future.set_result(None)

    pipeline = AskingPipeline(session_dir=tmp_path / "sidecar")
    monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_pipeline", lambda *args, **kwargs: pipeline)
    monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_agent_runtime", lambda options: _fake_runtime())

    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    active_task = await store.get_or_create_task(task_id="task-1", context_id="ctx-1")
    executor = IacCodeA2APipelineExecutor(
        task_store=store,
        model="qwen3.6-plus",
        metrics=NoOpA2AMetrics(),
        artifact_store=None,
        push_notifier=None,
        permission_resolver=None,
        auto_approve_permissions=False,
        thinking_exposure_types=None,
    )
    queue = FakeEventQueue()
    runner = asyncio.create_task(
        executor.execute(
            context=FakeRequestContext(metadata={"iac_code": {"cwd": str(tmp_path)}}),
            event_queue=queue,
            task=active_task,
            task_id="task-1",
            context_id="ctx-1",
            cwd=str(tmp_path),
            prompt="帮我部署网站",
        )
    )
    await asyncio.wait_for(pipeline.question_ready.wait(), timeout=1)
    await _wait_for_pipeline_event(queue, "input_required")

    await asyncio.wait_for(runner, timeout=1)
    await asyncio.wait_for(pipeline.closed.wait(), timeout=1)

    ctx = await store.get_or_create_context(
        context_id="ctx-1",
        cwd=str(tmp_path),
        runtime_factory=lambda _session_id: _fake_runtime(),
    )
    assert ctx.active_task_id is None
    assert active_task.state == "input-required"
    assert pipeline.answers == []
    assert "".join(active_task.output_text) == ""
    event_types = [
        dump(event)["metadata"]["iac_code"]["pipeline"]["eventType"]
        for event in queue.events
        if isinstance(event, TaskStatusUpdateEvent)
        and "pipeline" in dump(event).get("metadata", {}).get("iac_code", {})
    ]
    assert event_types == ["input_required"]


def test_ask_user_question_answer_accepts_one_based_option_index() -> None:
    from iac_code.a2a.pipeline_executor import _ask_user_question_answer_from_prompt

    answer = _ask_user_question_answer_from_prompt(
        AskUserQuestionEvent(
            tool_use_id="ask-1",
            question="请选择部署目标",
            options=[
                {"id": "nginx", "label": "Nginx 网站"},
                {"id": "ecs", "label": "ECS 应用"},
            ],
        ),
        "1",
    )

    assert answer == {"selected_id": "nginx", "selected_label": "Nginx 网站", "free_text": ""}


@pytest.mark.asyncio
async def test_pipeline_executor_does_not_resolve_pending_question_when_input_received_publish_fails(
    tmp_path: Path,
) -> None:
    from iac_code.a2a.pipeline_events import PipelineA2AContext, PipelineEventTranslator
    from iac_code.a2a.pipeline_executor import (
        A2APipelineRuntime,
        IacCodeA2APipelineExecutor,
        _PendingAskUserQuestion,
    )
    from iac_code.a2a.pipeline_journal import A2APipelineJournal
    from iac_code.a2a.pipeline_snapshot import A2APipelineSnapshotStore
    from iac_code.a2a.pipeline_stream import PipelineA2AEventPublisher

    future: asyncio.Future[dict[str, str] | None] = asyncio.get_running_loop().create_future()
    question = AskUserQuestionEvent(
        tool_use_id="ask-1",
        question="请选择部署目标",
        options=[{"id": "nginx", "label": "Nginx 网站"}],
        response_future=future,
    )
    publisher = PipelineA2AEventPublisher(
        event_queue=FakeEventQueue(),
        translator=PipelineEventTranslator(
            PipelineA2AContext(
                pipeline_run_id="ctx-1",
                task_id="task-1",
                context_id="ctx-1",
                pipeline_name="selling",
            )
        ),
        journal=A2APipelineJournal(tmp_path / "pipeline"),
        snapshot_store=A2APipelineSnapshotStore(tmp_path / "pipeline"),
    )
    publisher.publish_manual = AsyncMock(return_value=None)  # type: ignore[method-assign]
    runtime = A2APipelineRuntime(agent_runtime=_fake_runtime(), publisher=publisher)
    runtime.pending_question = _PendingAskUserQuestion(
        event=question,
        envelope={
            "eventType": "input_required",
            "scope": "step",
            "input": {"inputId": "ask-ask-1"},
            "step": {"runId": "step-intent_parsing-1", "id": "intent_parsing"},
        },
    )
    executor = IacCodeA2APipelineExecutor(
        task_store=A2ATaskStore(metrics=NoOpA2AMetrics()),
        model="qwen3.6-plus",
        metrics=NoOpA2AMetrics(),
        artifact_store=None,
        push_notifier=None,
        permission_resolver=None,
        auto_approve_permissions=False,
        thinking_exposure_types=None,
    )

    routed = await executor._route_pending_question_answer(runtime, "Nginx 网站")

    assert routed == "not_routed"
    assert future.done() is False
    assert runtime.pending_question is not None


@pytest.mark.asyncio
async def test_active_task_route_does_not_treat_finished_pending_question_as_interrupt(
    tmp_path: Path,
) -> None:
    from iac_code.a2a.pipeline_events import PipelineA2AContext, PipelineEventTranslator
    from iac_code.a2a.pipeline_executor import (
        A2APipelineRuntime,
        IacCodeA2APipelineExecutor,
        _PendingAskUserQuestion,
    )
    from iac_code.a2a.pipeline_journal import A2APipelineJournal
    from iac_code.a2a.pipeline_snapshot import A2APipelineSnapshotStore
    from iac_code.a2a.pipeline_stream import PipelineA2AEventPublisher

    class InterruptRecordingPipeline(FakePipeline):
        def __init__(self, *, session_dir: Path) -> None:
            super().__init__([], session_dir=session_dir)
            self.interrupts: list[str] = []

        async def handle_user_interrupt(self, message: str) -> SimpleNamespace:
            self.interrupts.append(message)
            return SimpleNamespace(action="supplement", reason="wrong route")

    future: asyncio.Future[dict[str, str] | None] = asyncio.get_running_loop().create_future()
    future.set_result(None)
    question = AskUserQuestionEvent(
        tool_use_id="ask-1",
        question="请选择部署目标",
        options=[{"id": "nginx", "label": "Nginx 网站"}],
        response_future=future,
    )
    publisher = PipelineA2AEventPublisher(
        event_queue=FakeEventQueue(),
        translator=PipelineEventTranslator(
            PipelineA2AContext(
                pipeline_run_id="ctx-1",
                task_id="task-1",
                context_id="ctx-1",
                pipeline_name="selling",
            )
        ),
        journal=A2APipelineJournal(tmp_path / "pipeline"),
        snapshot_store=A2APipelineSnapshotStore(tmp_path / "pipeline"),
    )
    runtime = A2APipelineRuntime(
        agent_runtime=_fake_runtime(),
        pipeline=InterruptRecordingPipeline(session_dir=tmp_path / "sidecar"),
        publisher=publisher,
    )
    runtime.pending_question = _PendingAskUserQuestion(
        event=question,
        envelope={
            "eventType": "input_required",
            "scope": "step",
            "input": {"inputId": "ask-ask-1"},
            "step": {"runId": "step-intent_parsing-1", "id": "intent_parsing"},
        },
    )
    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    task = await store.get_or_create_task(task_id="task-1", context_id="ctx-1")
    ctx = await store.get_or_create_context(
        context_id="ctx-1",
        cwd=str(tmp_path),
        runtime_factory=lambda _session_id: _fake_runtime(),
    )
    ctx.runtime = runtime
    ctx.active_task_id = "task-1"
    executor = IacCodeA2APipelineExecutor(
        task_store=store,
        model="qwen3.6-plus",
        metrics=NoOpA2AMetrics(),
        artifact_store=None,
        push_notifier=None,
        permission_resolver=None,
        auto_approve_permissions=False,
        thinking_exposure_types=None,
    )

    routed = await executor._route_active_pipeline_interrupt(
        FakeEventQueue(),
        task=task,
        ctx=ctx,
        task_id="task-1",
        context_id="ctx-1",
        cwd=str(tmp_path),
        pipeline_input="Nginx 网站",
        preserve_task_record=True,
    )

    assert routed is True
    assert runtime.pipeline.interrupts == []


@pytest.mark.asyncio
async def test_active_task_route_answers_pending_question_without_marking_input_required(
    tmp_path: Path,
) -> None:
    from iac_code.a2a.pipeline_events import PipelineA2AContext, PipelineEventTranslator
    from iac_code.a2a.pipeline_executor import (
        A2APipelineRuntime,
        IacCodeA2APipelineExecutor,
        _PendingAskUserQuestion,
    )
    from iac_code.a2a.pipeline_journal import A2APipelineJournal
    from iac_code.a2a.pipeline_snapshot import A2APipelineSnapshotStore
    from iac_code.a2a.pipeline_stream import PipelineA2AEventPublisher

    class InterruptRecordingPipeline(FakePipeline):
        def __init__(self, *, session_dir: Path) -> None:
            super().__init__([], session_dir=session_dir)
            self.interrupts: list[str] = []

        async def handle_user_interrupt(self, message: str) -> SimpleNamespace:
            self.interrupts.append(message)
            return SimpleNamespace(action="supplement", reason="wrong route")

    future: asyncio.Future[dict[str, str] | None] = asyncio.get_running_loop().create_future()
    question = AskUserQuestionEvent(
        tool_use_id="ask-1",
        question="请选择部署目标",
        options=[{"id": "nginx", "label": "Nginx 网站"}],
        response_future=future,
    )
    publisher = PipelineA2AEventPublisher(
        event_queue=FakeEventQueue(),
        translator=PipelineEventTranslator(
            PipelineA2AContext(
                pipeline_run_id="ctx-1",
                task_id="task-1",
                context_id="ctx-1",
                pipeline_name="selling",
            )
        ),
        journal=A2APipelineJournal(tmp_path / "pipeline"),
        snapshot_store=A2APipelineSnapshotStore(tmp_path / "pipeline"),
    )
    runtime = A2APipelineRuntime(
        agent_runtime=_fake_runtime(),
        pipeline=InterruptRecordingPipeline(session_dir=tmp_path / "sidecar"),
        publisher=publisher,
    )
    runtime.pending_question = _PendingAskUserQuestion(
        event=question,
        envelope={
            "eventType": "input_required",
            "scope": "step",
            "input": {"inputId": "ask-ask-1"},
            "step": {"runId": "step-intent_parsing-1", "id": "intent_parsing"},
        },
    )
    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    task = await store.get_or_create_task(task_id="task-1", context_id="ctx-1")
    task.state = "input-required"
    ctx = await store.get_or_create_context(
        context_id="ctx-1",
        cwd=str(tmp_path),
        runtime_factory=lambda _session_id: _fake_runtime(),
    )
    ctx.runtime = runtime
    ctx.active_task_id = "task-1"
    executor = IacCodeA2APipelineExecutor(
        task_store=store,
        model="qwen3.6-plus",
        metrics=NoOpA2AMetrics(),
        artifact_store=None,
        push_notifier=None,
        permission_resolver=None,
        auto_approve_permissions=False,
        thinking_exposure_types=None,
    )

    routed = await executor._route_active_pipeline_interrupt(
        FakeEventQueue(),
        task=task,
        ctx=ctx,
        task_id="task-1",
        context_id="ctx-1",
        cwd=str(tmp_path),
        pipeline_input="Nginx 网站",
        preserve_task_record=True,
    )

    assert routed is True
    assert runtime.pipeline.interrupts == []
    assert future.result()["selected_id"] == "nginx"
    assert runtime.pending_question is None
    assert task.state == "working"


@pytest.mark.asyncio
async def test_executor_routes_running_sidecar_pending_ask_to_ask_resume(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("IAC_CODE_MODE", "pipeline")

    class AskResumePipeline(FakePipeline):
        def __init__(self, *, session_dir: Path) -> None:
            super().__init__(
                [
                    TextDeltaEvent(text="nginx selected"),
                    PipelineEvent(
                        type=PipelineEventType.PIPELINE_COMPLETED,
                        step_id=None,
                        timestamp=1717821601.0,
                        data={},
                    ),
                ],
                session_dir=session_dir,
            )
            self.ask_answers: list[dict[str, str]] = []
            self.pending_inputs: list[dict[str, object] | None] = []

        async def resume_ask_user_question(
            self,
            answer: dict[str, str],
            *,
            tool_use_id: str,
            pending_input: dict[str, object] | None = None,
        ):
            self.ask_answers.append(answer)
            self.pending_inputs.append(pending_input)
            assert tool_use_id == "ask-1"
            for event in self.events:
                yield event

    session_dir = tmp_path / "sidecar"
    pending = {
        "schemaVersion": "1.0",
        "extensionUri": "urn:iac-code:a2a:pipeline-events:v1",
        "eventId": "evt-ask",
        "sequence": 1,
        "createdAt": "2026-06-08T10:00:00Z",
        "eventType": "input_required",
        "scope": "candidate_step",
        "pipelineRunId": "ctx-1",
        "taskId": "task-1",
        "contextId": "ctx-1",
        "pipelineName": "selling",
        "status": "input_required",
        "step": {"runId": "step-intent_parsing-1", "id": "intent_parsing", "attempt": 1},
        "candidate": {"runId": "candidate-evaluate_candidate-0-1", "id": "evaluate_candidate", "index": 0},
        "candidateStep": {
            "runId": "candidate-evaluate_candidate-0-1-template_generating-1",
            "id": "template_generating",
        },
        "data": {"kind": "ask_user_question", "toolUseId": "ask-1"},
        "input": {
            "inputId": "ask-ask-1",
            "kind": "ask_user_question",
            "toolUseId": "ask-1",
            "question": "请选择部署目标",
            "options": [{"id": "nginx", "label": "Nginx 网站"}],
            "allowFreeText": True,
        },
    }
    A2APipelineJournal(session_dir).append(pending)
    A2APipelineSnapshotStore(session_dir).save(reduce_pipeline_events([pending]))
    fake_pipeline = AskResumePipeline(session_dir=session_dir)
    fake_pipeline.sidecar_status = "running"
    monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_pipeline", lambda *args, **kwargs: fake_pipeline)
    monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_agent_runtime", lambda options: _fake_runtime())

    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    executor = IacCodeA2AExecutor(task_store=store, model="qwen3.6-plus")
    queue = FakeEventQueue()

    await executor.execute(
        FakeRequestContext(
            text="Nginx 网站",
            metadata={"iac_code": {"cwd": str(tmp_path)}},
        ),
        queue,
    )

    assert fake_pipeline.continue_calls == 0
    assert fake_pipeline.ask_answers == [{"selected_id": "nginx", "selected_label": "Nginx 网站", "free_text": ""}]
    assert fake_pipeline.pending_inputs[0]["candidate"] == {
        "runId": "candidate-evaluate_candidate-0-1",
        "id": "evaluate_candidate",
        "index": 0,
    }
    assert fake_pipeline.pending_inputs[0]["candidateStep"] == {
        "runId": "candidate-evaluate_candidate-0-1-template_generating-1",
        "id": "template_generating",
    }
    task_record = await store.get_or_create_task(task_id="task-1", context_id="ctx-1")
    assert "nginx selected" in "".join(task_record.output_text)
    assert "input_received" in [event["eventType"] for event in A2APipelineJournal(session_dir).read_all()]


@pytest.mark.asyncio
async def test_executor_routes_waiting_input_sidecar_pending_ask_to_ask_resume(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("IAC_CODE_MODE", "pipeline")

    class AskResumePipeline(FakePipeline):
        def __init__(self, *, session_dir: Path) -> None:
            super().__init__(
                [
                    TextDeltaEvent(text="nginx selected"),
                    PipelineEvent(
                        type=PipelineEventType.PIPELINE_COMPLETED,
                        step_id=None,
                        timestamp=1717821601.0,
                        data={},
                    ),
                ],
                session_dir=session_dir,
            )
            self.ask_answers: list[dict[str, str]] = []

        async def resume_ask_user_question(self, answer: dict[str, str], *, tool_use_id: str):
            self.ask_answers.append(answer)
            assert tool_use_id == "ask-1"
            for event in self.events:
                yield event

    session_dir = tmp_path / "sidecar"
    pending = {
        "schemaVersion": "1.0",
        "extensionUri": "urn:iac-code:a2a:pipeline-events:v1",
        "eventId": "evt-ask",
        "sequence": 1,
        "createdAt": "2026-06-08T10:00:00Z",
        "eventType": "input_required",
        "scope": "step",
        "pipelineRunId": "ctx-1",
        "taskId": "task-1",
        "contextId": "ctx-1",
        "pipelineName": "selling",
        "status": "input_required",
        "step": {"runId": "step-intent_parsing-1", "id": "intent_parsing", "attempt": 1},
        "data": {"kind": "ask_user_question", "toolUseId": "ask-1"},
        "input": {
            "inputId": "ask-ask-1",
            "kind": "ask_user_question",
            "toolUseId": "ask-1",
            "question": "请选择部署目标",
            "options": [{"id": "nginx", "label": "Nginx 网站"}],
            "allowFreeText": True,
        },
    }
    A2APipelineJournal(session_dir).append(pending)
    A2APipelineSnapshotStore(session_dir).save(reduce_pipeline_events([pending]))
    fake_pipeline = AskResumePipeline(session_dir=session_dir)
    fake_pipeline.sidecar_status = "waiting_input"
    monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_pipeline", lambda *args, **kwargs: fake_pipeline)
    monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_agent_runtime", lambda options: _fake_runtime())

    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    executor = IacCodeA2AExecutor(task_store=store, model="qwen3.6-plus")
    queue = FakeEventQueue()

    await executor.execute(
        FakeRequestContext(
            text="Nginx 网站",
            metadata={"iac_code": {"cwd": str(tmp_path)}},
        ),
        queue,
    )

    assert fake_pipeline.resume_prompts == []
    assert fake_pipeline.ask_answers == [{"selected_id": "nginx", "selected_label": "Nginx 网站", "free_text": ""}]
    task_record = await store.get_or_create_task(task_id="task-1", context_id="ctx-1")
    assert "nginx selected" in "".join(task_record.output_text)
    assert "input_received" in [event["eventType"] for event in A2APipelineJournal(session_dir).read_all()]


@pytest.mark.asyncio
async def test_executor_routes_waiting_input_sidecar_by_context_when_task_id_is_missing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("IAC_CODE_MODE", "pipeline")

    class AskResumePipeline(FakePipeline):
        def __init__(self, *, session_dir: Path) -> None:
            super().__init__(
                [
                    TextDeltaEvent(text="nginx selected"),
                    PipelineEvent(
                        type=PipelineEventType.PIPELINE_COMPLETED,
                        step_id=None,
                        timestamp=1717821601.0,
                        data={},
                    ),
                ],
                session_dir=session_dir,
            )
            self.ask_answers: list[dict[str, str]] = []

        async def resume_ask_user_question(self, answer: dict[str, str], *, tool_use_id: str):
            self.ask_answers.append(answer)
            assert tool_use_id == "ask-1"
            for event in self.events:
                yield event

    from iac_code.a2a.persistence import A2AContextSnapshot, A2APersistenceStore
    from iac_code.a2a.pipeline_paths import a2a_pipeline_dir_for_session

    persistence = A2APersistenceStore(tmp_path / "a2a")
    session_id = "session-ctx-1"
    persistence.save_context(A2AContextSnapshot(context_id="ctx-1", session_id=session_id, cwd=str(tmp_path)))
    session_dir = a2a_pipeline_dir_for_session(cwd=str(tmp_path), session_id=session_id)
    pending = {
        "schemaVersion": "1.0",
        "extensionUri": "urn:iac-code:a2a:pipeline-events:v1",
        "eventId": "evt-ask",
        "sequence": 1,
        "createdAt": "2026-06-08T10:00:00Z",
        "eventType": "input_required",
        "scope": "step",
        "pipelineRunId": "ctx-1",
        "taskId": "task-1",
        "contextId": "ctx-1",
        "pipelineName": "selling",
        "status": "input_required",
        "step": {"runId": "step-intent_parsing-1", "id": "intent_parsing", "attempt": 1},
        "data": {"kind": "ask_user_question", "toolUseId": "ask-1"},
        "input": {
            "inputId": "ask-ask-1",
            "kind": "ask_user_question",
            "toolUseId": "ask-1",
            "question": "请选择部署目标",
            "options": [{"id": "nginx", "label": "Nginx 网站"}],
            "allowFreeText": True,
        },
    }
    A2APipelineJournal(session_dir).append(pending)
    A2APipelineSnapshotStore(session_dir).save(reduce_pipeline_events([pending]))
    fake_pipeline = AskResumePipeline(session_dir=session_dir)
    fake_pipeline.session = SimpleNamespace()
    fake_pipeline.sidecar_status = "waiting_input"
    monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_pipeline", lambda *args, **kwargs: fake_pipeline)
    monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_agent_runtime", lambda options: _fake_runtime())

    store = A2ATaskStore(metrics=NoOpA2AMetrics(), persistence=persistence)
    executor = IacCodeA2AExecutor(task_store=store, model="qwen3.6-plus")

    await executor.execute(
        FakeRequestContext(
            task_id=None,
            context_id="ctx-1",
            text="Nginx 网站",
            metadata={"iac_code": {"cwd": str(tmp_path)}},
        ),
        FakeEventQueue(),
    )

    assert fake_pipeline.clear_sidecar_calls == 0
    assert fake_pipeline.resume_prompts == []
    assert fake_pipeline.ask_answers == [{"selected_id": "nginx", "selected_label": "Nginx 网站", "free_text": ""}]
    task_record = await store.get_or_create_task(task_id="task-1", context_id="ctx-1")
    assert "nginx selected" in "".join(task_record.output_text)


def test_waiting_input_task_id_from_sidecar_accepts_candidate_selection(tmp_path: Path) -> None:
    from iac_code.a2a.pipeline_executor import waiting_input_task_id_from_sidecar
    from iac_code.a2a.pipeline_paths import a2a_pipeline_dir_for_session

    cwd = tmp_path / "workspace"
    session_id = "session-ctx-1"
    context_id = "ctx-1"
    session_dir = a2a_pipeline_dir_for_session(cwd=str(cwd), session_id=session_id)
    pending = {
        "schemaVersion": "1.0",
        "extensionUri": "urn:iac-code:a2a:pipeline-events:v1",
        "eventId": "evt-selection",
        "sequence": 1,
        "createdAt": "2026-06-08T10:00:00Z",
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
            "prompt": "请选择方案",
            "options": [{"name": "方案A", "candidate_index": 0}],
        },
    }
    A2APipelineJournal(session_dir).append(pending)
    A2APipelineSnapshotStore(session_dir).save(reduce_pipeline_events([pending]))

    assert waiting_input_task_id_from_sidecar(cwd=str(cwd), session_id=session_id, context_id=context_id) == "task-1"


@pytest.mark.asyncio
async def test_executor_recovers_pending_ask_from_journal_when_snapshot_is_missing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("IAC_CODE_MODE", "pipeline")

    class AskResumePipeline(FakePipeline):
        def __init__(self, *, session_dir: Path) -> None:
            super().__init__(
                [
                    TextDeltaEvent(text="nginx selected"),
                    PipelineEvent(
                        type=PipelineEventType.PIPELINE_COMPLETED,
                        step_id=None,
                        timestamp=1717821601.0,
                        data={},
                    ),
                ],
                session_dir=session_dir,
            )
            self.ask_answers: list[dict[str, str]] = []

        async def resume_ask_user_question(self, answer: dict[str, str], *, tool_use_id: str):
            self.ask_answers.append(answer)
            assert tool_use_id == "ask-1"
            for event in self.events:
                yield event

    session_dir = tmp_path / "sidecar"
    pending = {
        "schemaVersion": "1.0",
        "extensionUri": "urn:iac-code:a2a:pipeline-events:v1",
        "eventId": "evt-ask",
        "sequence": 1,
        "createdAt": "2026-06-08T10:00:00Z",
        "eventType": "input_required",
        "scope": "step",
        "pipelineRunId": "ctx-1",
        "taskId": "task-1",
        "contextId": "ctx-1",
        "pipelineName": "selling",
        "status": "input_required",
        "step": {"runId": "step-intent_parsing-1", "id": "intent_parsing", "attempt": 1},
        "data": {"kind": "ask_user_question", "toolUseId": "ask-1"},
        "input": {
            "inputId": "ask-ask-1",
            "kind": "ask_user_question",
            "toolUseId": "ask-1",
            "question": "请选择部署目标",
            "options": [{"id": "nginx", "label": "Nginx 网站"}],
            "allowFreeText": True,
        },
    }
    A2APipelineJournal(session_dir).append(pending)
    fake_pipeline = AskResumePipeline(session_dir=session_dir)
    fake_pipeline.sidecar_status = "waiting_input"
    monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_pipeline", lambda *args, **kwargs: fake_pipeline)
    monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_agent_runtime", lambda options: _fake_runtime())

    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    executor = IacCodeA2AExecutor(task_store=store, model="qwen3.6-plus")

    await executor.execute(
        FakeRequestContext(
            task_id="task-1",
            context_id="ctx-1",
            text="Nginx 网站",
            metadata={"iac_code": {"cwd": str(tmp_path)}},
        ),
        FakeEventQueue(),
    )

    assert fake_pipeline.resume_prompts == []
    assert fake_pipeline.ask_answers == [{"selected_id": "nginx", "selected_label": "Nginx 网站", "free_text": ""}]


@pytest.mark.asyncio
async def test_executor_ignores_stale_snapshot_pending_ask_after_journal_input_received(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from iac_code.a2a.pipeline_executor import IacCodeA2APipelineExecutor

    monkeypatch.setenv("IAC_CODE_MODE", "pipeline")

    class RunningPipeline(FakePipeline):
        def __init__(self, *, session_dir: Path) -> None:
            super().__init__(
                [
                    TextDeltaEvent(text="continued"),
                    PipelineEvent(
                        type=PipelineEventType.PIPELINE_COMPLETED,
                        step_id=None,
                        timestamp=1717821601.0,
                        data={},
                    ),
                ],
                session_dir=session_dir,
            )
            self.ask_answers: list[dict[str, str]] = []

        async def resume_ask_user_question(self, answer: dict[str, str], *, tool_use_id: str):
            self.ask_answers.append(answer)
            raise AssertionError("stale pending ask should not be replayed")

    session_dir = tmp_path / "sidecar"
    pending = {
        "schemaVersion": "1.0",
        "extensionUri": "urn:iac-code:a2a:pipeline-events:v1",
        "eventId": "evt-ask",
        "sequence": 1,
        "createdAt": "2026-06-08T10:00:00Z",
        "eventType": "input_required",
        "scope": "step",
        "pipelineRunId": "ctx-1",
        "taskId": "task-1",
        "contextId": "ctx-1",
        "pipelineName": "selling",
        "status": "input_required",
        "step": {"runId": "step-intent_parsing-1", "id": "intent_parsing", "attempt": 1},
        "data": {"kind": "ask_user_question", "toolUseId": "ask-1"},
        "input": {
            "inputId": "ask-ask-1",
            "kind": "ask_user_question",
            "toolUseId": "ask-1",
            "question": "请选择部署目标",
            "options": [{"id": "nginx", "label": "Nginx 网站"}],
            "allowFreeText": True,
        },
    }
    received = {
        **pending,
        "eventId": "evt-answer",
        "sequence": 2,
        "eventType": "input_received",
        "status": "working",
        "data": {"kind": "ask_user_question", "toolUseId": "ask-1"},
        "input": None,
    }
    A2APipelineSnapshotStore(session_dir).save(reduce_pipeline_events([pending]))
    journal = A2APipelineJournal(session_dir)
    journal.append(pending)
    journal.append(received)
    fake_pipeline = RunningPipeline(session_dir=session_dir)
    fake_pipeline.sidecar_status = "running"
    monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_pipeline", lambda *args, **kwargs: fake_pipeline)
    monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_agent_runtime", lambda options: _fake_runtime())

    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    executor = IacCodeA2APipelineExecutor(
        task_store=store,
        model="qwen3.6-plus",
        metrics=NoOpA2AMetrics(),
        artifact_store=None,
        push_notifier=None,
        permission_resolver=None,
        auto_approve_permissions=False,
        thinking_exposure_types=None,
    )

    await executor.execute(
        context=FakeRequestContext(
            task_id="task-1",
            context_id="ctx-1",
            text="Nginx 网站",
            metadata={"iac_code": {"cwd": str(tmp_path)}},
        ),
        event_queue=FakeEventQueue(),
        task=await store.get_or_create_task(task_id="task-1", context_id="ctx-1"),
        task_id="task-1",
        context_id="ctx-1",
        cwd=str(tmp_path),
        prompt="Nginx 网站",
    )

    assert fake_pipeline.ask_answers == []
    assert fake_pipeline.continue_inputs == ["Nginx 网站"]


@pytest.mark.asyncio
async def test_pipeline_executor_rejects_new_task_while_context_has_active_pipeline(tmp_path: Path) -> None:
    from iac_code.a2a.pipeline_events import PipelineA2AContext, PipelineEventTranslator
    from iac_code.a2a.pipeline_executor import A2APipelineRuntime, IacCodeA2APipelineExecutor
    from iac_code.a2a.pipeline_journal import A2APipelineJournal
    from iac_code.a2a.pipeline_snapshot import A2APipelineSnapshotStore
    from iac_code.a2a.pipeline_stream import PipelineA2AEventPublisher

    class InterruptiblePipeline(FakePipeline):
        def __init__(self, *, session_dir: Path) -> None:
            super().__init__([], session_dir=session_dir)
            self.interrupts: list[str] = []

        async def handle_user_interrupt(self, message: str) -> SimpleNamespace:
            self.interrupts.append(message)
            return SimpleNamespace(
                action="supplement",
                reason="added context",
                rollback_target=None,
                candidate_scope=None,
            )

    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    active_task = await store.get_or_create_task(task_id="active-task", context_id="ctx-1")
    active_task.state = "working"
    active_task.active_task = asyncio.current_task()
    new_task = await store.get_or_create_task(task_id="new-task", context_id="ctx-1")
    ctx = await store.get_or_create_context(
        context_id="ctx-1",
        cwd=str(tmp_path),
        runtime_factory=lambda _session_id: _fake_runtime(),
    )
    ctx.active_task_id = "active-task"
    active_task_handle = active_task.active_task

    queue = FakeEventQueue()
    pipeline = InterruptiblePipeline(session_dir=tmp_path / "sidecar")
    publisher = PipelineA2AEventPublisher(
        event_queue=queue,
        translator=PipelineEventTranslator(
            PipelineA2AContext(
                pipeline_run_id="ctx-1",
                task_id="active-task",
                context_id="ctx-1",
                pipeline_name="selling",
            )
        ),
        journal=A2APipelineJournal(tmp_path / "pipeline"),
        snapshot_store=A2APipelineSnapshotStore(tmp_path / "pipeline"),
    )
    ctx.runtime = A2APipelineRuntime(agent_runtime=_fake_runtime(), pipeline=pipeline, publisher=publisher)
    store.mirror_task(active_task)
    store.mirror_context(ctx)

    executor = IacCodeA2APipelineExecutor(
        task_store=store,
        model="qwen3.6-plus",
        metrics=NoOpA2AMetrics(),
        artifact_store=None,
        push_notifier=None,
        permission_resolver=None,
        auto_approve_permissions=False,
        thinking_exposure_types=None,
    )

    await executor.execute(
        context=FakeRequestContext(
            task_id="new-task",
            context_id="ctx-1",
            text="please change cpu",
            metadata={"iac_code": {"cwd": str(tmp_path)}},
        ),
        event_queue=queue,
        task=new_task,
        task_id="new-task",
        context_id="ctx-1",
        cwd=str(tmp_path),
        prompt="please change cpu",
    )

    assert pipeline.interrupts == []
    assert publisher.journal.read_all() == []
    final_status = _status_events(queue)[-1]["status"]
    assert final_status["state"] == "TASK_STATE_FAILED"
    assert final_status["message"]["parts"][0]["text"] == "Task is already working."
    assert new_task.state == "failed"
    assert ctx.active_task_id == "active-task"
    assert active_task.active_task is active_task_handle
    assert active_task.state == "working"


@pytest.mark.asyncio
async def test_parent_hard_interrupt_closes_active_stream_and_restarts(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from iac_code.a2a.pipeline_executor import IacCodeA2APipelineExecutor

    class RestartablePipeline(FakePipeline):
        def __init__(self, *, session_dir: Path) -> None:
            super().__init__([], session_dir=session_dir)
            self.primary_stream = CloseableEventStream([TextDeltaEvent(text="before interrupt")])
            self.restart_stream = CloseableEventStream(
                [
                    TextDeltaEvent(text="after interrupt"),
                    PipelineEvent(
                        type=PipelineEventType.PIPELINE_COMPLETED,
                        step_id=None,
                        timestamp=1717821602.0,
                        data={},
                    ),
                ],
                wait_until_closed=False,
            )
            self.interrupts: list[str] = []
            self.applied_verdicts: list[SimpleNamespace] = []
            self.continue_after_interrupt_calls = 0

        def run(self, prompt: str):
            self.run_prompts.append(prompt)
            return self.primary_stream

        def continue_after_interrupt(self):
            self.continue_after_interrupt_calls += 1
            return self.restart_stream

        async def handle_user_interrupt(self, message: str) -> SimpleNamespace:
            self.interrupts.append(message)
            return SimpleNamespace(
                action="hard_interrupt",
                reason="changed parent plan",
                rollback_target="architecture_planning",
                candidate_scope=None,
            )

        def apply_hard_interrupt(self, verdict: SimpleNamespace) -> bool:
            self.applied_verdicts.append(verdict)
            return True

    pipeline = RestartablePipeline(session_dir=tmp_path / "sidecar")
    monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_pipeline", lambda *args, **kwargs: pipeline)
    monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_agent_runtime", lambda options: _fake_runtime())

    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    active_task = await store.get_or_create_task(task_id="task-1", context_id="ctx-1")
    executor = IacCodeA2APipelineExecutor(
        task_store=store,
        model="qwen3.6-plus",
        metrics=NoOpA2AMetrics(),
        artifact_store=None,
        push_notifier=None,
        permission_resolver=None,
        auto_approve_permissions=False,
        thinking_exposure_types=None,
    )
    active_queue = FakeEventQueue()
    runner = asyncio.create_task(
        executor.execute(
            context=FakeRequestContext(metadata={"iac_code": {"cwd": str(tmp_path)}}),
            event_queue=active_queue,
            task=active_task,
            task_id="task-1",
            context_id="ctx-1",
            cwd=str(tmp_path),
            prompt="build ecs",
        )
    )
    await asyncio.wait_for(pipeline.primary_stream.started.wait(), timeout=1)
    await _wait_for_output_text(active_task, "before interrupt")

    try:
        await executor.execute(
            context=FakeRequestContext(
                text="please change cpu",
                metadata={"iac_code": {"cwd": str(tmp_path)}},
            ),
            event_queue=FakeEventQueue(),
            task=active_task,
            task_id="task-1",
            context_id="ctx-1",
            cwd=str(tmp_path),
            prompt="please change cpu",
        )

        await asyncio.wait_for(pipeline.primary_stream.closed_event.wait(), timeout=1)
        assert pipeline.primary_stream.closed is True
        await asyncio.wait_for(runner, timeout=1)
        assert pipeline.continue_after_interrupt_calls == 1
        assert "".join(active_task.output_text) == "before interruptafter interrupt"
    finally:
        if not pipeline.primary_stream.closed:
            await pipeline.primary_stream.aclose()
        if not runner.done():
            runner.cancel()
            try:
                await runner
            except asyncio.CancelledError:
                pass


@pytest.mark.asyncio
async def test_parent_hard_interrupt_cancels_blocked_async_generator_before_restart(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from iac_code.a2a.pipeline_executor import IacCodeA2APipelineExecutor

    class BlockedGeneratorPipeline(FakePipeline):
        def __init__(self, *, session_dir: Path) -> None:
            super().__init__([], session_dir=session_dir)
            self.generator_blocked = asyncio.Event()
            self.primary_cancelled = asyncio.Event()
            self.primary_closed = asyncio.Event()
            self.release_stale = asyncio.Event()
            self.continue_after_interrupt_calls = 0
            self.interrupts: list[str] = []

        async def run(self, prompt: str):
            self.run_prompts.append(prompt)
            yield TextDeltaEvent(text="before interrupt")
            self.generator_blocked.set()
            try:
                await self.release_stale.wait()
            except asyncio.CancelledError:
                self.primary_cancelled.set()
                raise
            finally:
                self.primary_closed.set()
            yield TextDeltaEvent(text="stale")

        def continue_after_interrupt(self):
            self.continue_after_interrupt_calls += 1
            return self._restart_stream()

        async def _restart_stream(self):
            yield TextDeltaEvent(text="after interrupt")
            yield PipelineEvent(
                type=PipelineEventType.PIPELINE_COMPLETED,
                step_id=None,
                timestamp=1717821602.0,
                data={},
            )

        async def handle_user_interrupt(self, message: str) -> SimpleNamespace:
            self.interrupts.append(message)
            return SimpleNamespace(
                action="hard_interrupt",
                reason="changed parent plan",
                rollback_target="architecture_planning",
                candidate_scope=None,
            )

        def apply_hard_interrupt(self, verdict: SimpleNamespace) -> bool:
            return True

    pipeline = BlockedGeneratorPipeline(session_dir=tmp_path / "sidecar")
    monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_pipeline", lambda *args, **kwargs: pipeline)
    monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_agent_runtime", lambda options: _fake_runtime())

    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    active_task = await store.get_or_create_task(task_id="task-1", context_id="ctx-1")
    executor = IacCodeA2APipelineExecutor(
        task_store=store,
        model="qwen3.6-plus",
        metrics=NoOpA2AMetrics(),
        artifact_store=None,
        push_notifier=None,
        permission_resolver=None,
        auto_approve_permissions=False,
        thinking_exposure_types=None,
    )
    runner = asyncio.create_task(
        executor.execute(
            context=FakeRequestContext(metadata={"iac_code": {"cwd": str(tmp_path)}}),
            event_queue=FakeEventQueue(),
            task=active_task,
            task_id="task-1",
            context_id="ctx-1",
            cwd=str(tmp_path),
            prompt="build ecs",
        )
    )
    await asyncio.wait_for(pipeline.generator_blocked.wait(), timeout=1)

    try:
        await executor.execute(
            context=FakeRequestContext(
                text="please change cpu",
                metadata={"iac_code": {"cwd": str(tmp_path)}},
            ),
            event_queue=FakeEventQueue(),
            task=active_task,
            task_id="task-1",
            context_id="ctx-1",
            cwd=str(tmp_path),
            prompt="please change cpu",
        )

        await asyncio.wait_for(pipeline.primary_cancelled.wait(), timeout=1)
        await asyncio.wait_for(pipeline.primary_closed.wait(), timeout=1)
        await asyncio.wait_for(runner, timeout=1)
        assert pipeline.continue_after_interrupt_calls == 1
        assert "".join(active_task.output_text) == "before interruptafter interrupt"
    finally:
        pipeline.release_stale.set()
        if not runner.done():
            runner.cancel()
            try:
                await runner
            except asyncio.CancelledError:
                pass


@pytest.mark.asyncio
async def test_canceled_pipeline_run_closes_blocked_stream_without_child_task_leak(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from iac_code.a2a.pipeline_executor import IacCodeA2APipelineExecutor

    class CancellablePipeline(FakePipeline):
        def __init__(self, *, session_dir: Path) -> None:
            super().__init__([], session_dir=session_dir)
            self.generator_blocked = asyncio.Event()
            self.primary_cancelled = asyncio.Event()
            self.primary_closed = asyncio.Event()
            self.release_stale = asyncio.Event()

        async def run(self, prompt: str):
            self.run_prompts.append(prompt)
            self.generator_blocked.set()
            try:
                await self.release_stale.wait()
            except asyncio.CancelledError:
                self.primary_cancelled.set()
                raise
            finally:
                self.primary_closed.set()
            yield TextDeltaEvent(text="stale")

    pipeline = CancellablePipeline(session_dir=tmp_path / "sidecar")
    pipeline.handoff_enabled = True
    pipeline.handoff_summary = "[Pipeline Handoff Context]\nOutcome: canceled"
    monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_pipeline", lambda *args, **kwargs: pipeline)
    monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_agent_runtime", lambda options: _fake_runtime())

    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    active_task = await store.get_or_create_task(task_id="task-1", context_id="ctx-1")
    executor = IacCodeA2APipelineExecutor(
        task_store=store,
        model="qwen3.6-plus",
        metrics=NoOpA2AMetrics(),
        artifact_store=None,
        push_notifier=None,
        permission_resolver=None,
        auto_approve_permissions=False,
        thinking_exposure_types=None,
    )
    runner = asyncio.create_task(
        executor.execute(
            context=FakeRequestContext(metadata={"iac_code": {"cwd": str(tmp_path)}}),
            event_queue=FakeEventQueue(),
            task=active_task,
            task_id="task-1",
            context_id="ctx-1",
            cwd=str(tmp_path),
            prompt="build ecs",
        )
    )
    await asyncio.wait_for(pipeline.generator_blocked.wait(), timeout=1)

    try:
        runner.cancel()
        await asyncio.wait_for(runner, timeout=1)
        await asyncio.sleep(0)

        pending_coro_names = _pending_coro_names()
        assert "_next_stream_event" not in pending_coro_names
        assert "Event.wait" not in pending_coro_names
        assert pipeline.primary_cancelled.is_set()
        assert pipeline.primary_closed.is_set()
        events = A2APipelineJournal(tmp_path / "sidecar").read_all()
        assert [event["eventType"] for event in events[-2:]] == ["pipeline_canceled", "pipeline_handoff_ready"]
        assert events[-2]["status"] == "canceled"
        assert events[-1]["status"] == "canceled"
        assert events[-1]["data"]["outcome"] == "canceled"
        assert events[-1]["data"]["summary"] == "[Pipeline Handoff Context]\nOutcome: canceled"
        snapshot = A2APipelineSnapshotStore(tmp_path / "sidecar").load()
        assert snapshot is not None
        assert snapshot["status"] == "canceled"
        assert snapshot["normalHandoff"]["outcome"] == "canceled"
    finally:
        pipeline.release_stale.set()
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_candidate_hard_interrupt_does_not_close_or_restart_parent_stream(tmp_path: Path) -> None:
    from iac_code.a2a.pipeline_events import PipelineA2AContext, PipelineEventTranslator
    from iac_code.a2a.pipeline_executor import A2APipelineRuntime, IacCodeA2APipelineExecutor
    from iac_code.a2a.pipeline_journal import A2APipelineJournal
    from iac_code.a2a.pipeline_snapshot import A2APipelineSnapshotStore
    from iac_code.a2a.pipeline_stream import PipelineA2AEventPublisher

    class CandidateInterruptPipeline(FakePipeline):
        async def handle_user_interrupt(self, message: str) -> SimpleNamespace:
            return SimpleNamespace(
                action="hard_interrupt",
                reason=message,
                rollback_target="template_generating",
                candidate_scope="candidate-0",
            )

        def apply_hard_interrupt(self, verdict: SimpleNamespace) -> bool:
            return False

    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    task = await store.get_or_create_task(task_id="task-1", context_id="ctx-1")
    ctx = await store.get_or_create_context(
        context_id="ctx-1",
        cwd=str(tmp_path),
        runtime_factory=lambda _session_id: _fake_runtime(),
    )
    ctx.active_task_id = "task-1"

    queue = FakeEventQueue()
    pipeline = CandidateInterruptPipeline([], session_dir=tmp_path / "sidecar")
    stream = CloseableEventStream([])
    publisher = PipelineA2AEventPublisher(
        event_queue=queue,
        translator=PipelineEventTranslator(
            PipelineA2AContext(
                pipeline_run_id="ctx-1",
                task_id="task-1",
                context_id="ctx-1",
                pipeline_name="selling",
            )
        ),
        journal=A2APipelineJournal(tmp_path / "pipeline"),
        snapshot_store=A2APipelineSnapshotStore(tmp_path / "pipeline"),
    )
    runtime = A2APipelineRuntime(agent_runtime=_fake_runtime(), pipeline=pipeline, publisher=publisher)
    runtime.current_stream = stream
    runtime.restart_after_interrupt = False
    ctx.runtime = runtime

    executor = IacCodeA2APipelineExecutor(
        task_store=store,
        model="qwen3.6-plus",
        metrics=NoOpA2AMetrics(),
        artifact_store=None,
        push_notifier=None,
        permission_resolver=None,
        auto_approve_permissions=False,
        thinking_exposure_types=None,
    )

    await executor.execute(
        context=FakeRequestContext(text="candidate change", metadata={"iac_code": {"cwd": str(tmp_path)}}),
        event_queue=queue,
        task=task,
        task_id="task-1",
        context_id="ctx-1",
        cwd=str(tmp_path),
        prompt="candidate change",
    )

    assert stream.closed is False
    assert runtime.restart_after_interrupt is False


@pytest.mark.asyncio
async def test_hard_interrupt_failure_marks_active_task_failed_without_restart(tmp_path: Path) -> None:
    from iac_code.a2a.pipeline_events import PipelineA2AContext, PipelineEventTranslator
    from iac_code.a2a.pipeline_executor import A2APipelineRuntime, IacCodeA2APipelineExecutor
    from iac_code.a2a.pipeline_journal import A2APipelineJournal
    from iac_code.a2a.pipeline_snapshot import A2APipelineSnapshotStore
    from iac_code.a2a.pipeline_stream import PipelineA2AEventPublisher

    class FailedInterruptPipeline(FakePipeline):
        def __init__(self, *args, **kwargs) -> None:
            super().__init__(*args, **kwargs)
            self.resume_calls = 0

        async def handle_user_interrupt(self, message: str) -> SimpleNamespace:
            return SimpleNamespace(
                action="hard_interrupt",
                reason=message,
                rollback_target="missing",
                candidate_scope=None,
            )

        def apply_hard_interrupt(self, verdict: SimpleNamespace) -> bool:
            self.sidecar_status = "failed"
            return False

        def resume_agent_loops(self) -> None:
            self.resume_calls += 1

    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    task = await store.get_or_create_task(task_id="task-1", context_id="ctx-1")
    ctx = await store.get_or_create_context(
        context_id="ctx-1",
        cwd=str(tmp_path),
        runtime_factory=lambda _session_id: _fake_runtime(),
    )
    ctx.active_task_id = "task-1"

    queue = FakeEventQueue()
    pipeline = FailedInterruptPipeline([], session_dir=tmp_path / "sidecar")
    publisher = PipelineA2AEventPublisher(
        event_queue=queue,
        translator=PipelineEventTranslator(
            PipelineA2AContext(
                pipeline_run_id="ctx-1",
                task_id="task-1",
                context_id="ctx-1",
                pipeline_name="selling",
            )
        ),
        journal=A2APipelineJournal(tmp_path / "pipeline"),
        snapshot_store=A2APipelineSnapshotStore(tmp_path / "pipeline"),
    )
    runtime = A2APipelineRuntime(agent_runtime=_fake_runtime(), pipeline=pipeline, publisher=publisher)
    runtime.current_stream = CloseableEventStream([])
    ctx.runtime = runtime

    executor = IacCodeA2APipelineExecutor(
        task_store=store,
        model="qwen3.6-plus",
        metrics=NoOpA2AMetrics(),
        artifact_store=None,
        push_notifier=None,
        permission_resolver=None,
        auto_approve_permissions=False,
        thinking_exposure_types=None,
    )

    await executor.execute(
        context=FakeRequestContext(text="change architecture", metadata={"iac_code": {"cwd": str(tmp_path)}}),
        event_queue=queue,
        task=task,
        task_id="task-1",
        context_id="ctx-1",
        cwd=str(tmp_path),
        prompt="change architecture",
    )

    assert task.state == "failed"
    assert runtime.restart_after_interrupt is False
    assert runtime.pause_after_interrupt is True
    assert runtime.restart_requested.is_set() is True
    assert pipeline.resume_calls == 0


@pytest.mark.asyncio
async def test_escalated_candidate_interrupt_publishes_parent_rollback(tmp_path: Path) -> None:
    from iac_code.a2a.pipeline_events import PipelineA2AContext, PipelineEventTranslator
    from iac_code.a2a.pipeline_executor import A2APipelineRuntime, IacCodeA2APipelineExecutor
    from iac_code.a2a.pipeline_journal import A2APipelineJournal
    from iac_code.a2a.pipeline_snapshot import A2APipelineSnapshotStore
    from iac_code.a2a.pipeline_stream import PipelineA2AEventPublisher

    class EscalatingInterruptPipeline(FakePipeline):
        async def handle_user_interrupt(self, message: str) -> SimpleNamespace:
            return SimpleNamespace(
                action="hard_interrupt",
                reason=message,
                rollback_target="architecture_planning",
                candidate_scope="all",
            )

        def apply_hard_interrupt(self, verdict: SimpleNamespace) -> bool:
            return True

    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    task = await store.get_or_create_task(task_id="task-1", context_id="ctx-1")
    ctx = await store.get_or_create_context(
        context_id="ctx-1",
        cwd=str(tmp_path),
        runtime_factory=lambda _session_id: _fake_runtime(),
    )
    ctx.active_task_id = "task-1"

    queue = FakeEventQueue()
    pipeline = EscalatingInterruptPipeline([], session_dir=tmp_path / "sidecar")
    publisher = PipelineA2AEventPublisher(
        event_queue=queue,
        translator=PipelineEventTranslator(
            PipelineA2AContext(
                pipeline_run_id="ctx-1",
                task_id="task-1",
                context_id="ctx-1",
                pipeline_name="selling",
            )
        ),
        journal=A2APipelineJournal(tmp_path / "pipeline"),
        snapshot_store=A2APipelineSnapshotStore(tmp_path / "pipeline"),
    )
    runtime = A2APipelineRuntime(agent_runtime=_fake_runtime(), pipeline=pipeline, publisher=publisher)
    ctx.runtime = runtime

    executor = IacCodeA2APipelineExecutor(
        task_store=store,
        model="qwen3.6-plus",
        metrics=NoOpA2AMetrics(),
        artifact_store=None,
        push_notifier=None,
        permission_resolver=None,
        auto_approve_permissions=False,
        thinking_exposure_types=None,
    )

    await executor.execute(
        context=FakeRequestContext(text="escalate candidate", metadata={"iac_code": {"cwd": str(tmp_path)}}),
        event_queue=queue,
        task=task,
        task_id="task-1",
        context_id="ctx-1",
        cwd=str(tmp_path),
        prompt="escalate candidate",
    )

    events = publisher.journal.read_all()
    event_types = [event["eventType"] for event in events]
    assert "candidate_restart_requested" not in event_types
    rollback = next(event for event in events if event["eventType"] == "rollback_completed")
    assert rollback["data"]["rollbackScope"] == "parent"
    assert rollback["step"]["id"] == "architecture_planning"
    assert rollback["step"]["runId"] == "step-architecture_planning-2"
    assert rollback["step"]["attempt"] == 2
    assert runtime.restart_after_interrupt is True
    assert runtime.restart_requested.is_set() is True


@pytest.mark.asyncio
async def test_same_task_interrupt_handler_failure_preserves_active_record(tmp_path: Path) -> None:
    from iac_code.a2a.pipeline_events import PipelineA2AContext, PipelineEventTranslator
    from iac_code.a2a.pipeline_executor import A2APipelineRuntime, IacCodeA2APipelineExecutor
    from iac_code.a2a.pipeline_journal import A2APipelineJournal
    from iac_code.a2a.pipeline_snapshot import A2APipelineSnapshotStore
    from iac_code.a2a.pipeline_stream import PipelineA2AEventPublisher

    class FailingInterruptPipeline(FakePipeline):
        async def handle_user_interrupt(self, message: str) -> SimpleNamespace:
            raise ValueError(f"cannot judge {message}")

    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    task = await store.get_or_create_task(task_id="task-1", context_id="ctx-1")
    task.state = "working"
    task.active_task = asyncio.current_task()
    ctx = await store.get_or_create_context(
        context_id="ctx-1",
        cwd=str(tmp_path),
        runtime_factory=lambda _session_id: _fake_runtime(),
    )
    ctx.active_task_id = "task-1"
    active_task = task.active_task

    queue = FakeEventQueue()
    pipeline = FailingInterruptPipeline([], session_dir=tmp_path / "sidecar")
    publisher = PipelineA2AEventPublisher(
        event_queue=queue,
        translator=PipelineEventTranslator(
            PipelineA2AContext(
                pipeline_run_id="ctx-1",
                task_id="task-1",
                context_id="ctx-1",
                pipeline_name="selling",
            )
        ),
        journal=A2APipelineJournal(tmp_path / "pipeline"),
        snapshot_store=A2APipelineSnapshotStore(tmp_path / "pipeline"),
    )
    ctx.runtime = A2APipelineRuntime(agent_runtime=_fake_runtime(), pipeline=pipeline, publisher=publisher)

    executor = IacCodeA2APipelineExecutor(
        task_store=store,
        model="qwen3.6-plus",
        metrics=NoOpA2AMetrics(),
        artifact_store=None,
        push_notifier=None,
        permission_resolver=None,
        auto_approve_permissions=False,
        thinking_exposure_types=None,
    )

    await executor.execute(
        context=FakeRequestContext(text="bad interrupt", metadata={"iac_code": {"cwd": str(tmp_path)}}),
        event_queue=queue,
        task=task,
        task_id="task-1",
        context_id="ctx-1",
        cwd=str(tmp_path),
        prompt="bad interrupt",
    )

    assert ctx.active_task_id == "task-1"
    assert task.active_task is active_task
    assert task.state == "working"
    assert _status_events(queue)[-1]["status"]["state"] == "TASK_STATE_FAILED"


@pytest.mark.asyncio
async def test_same_task_non_interruptible_active_context_preserves_active_record(tmp_path: Path) -> None:
    from iac_code.a2a.pipeline_executor import A2APipelineRuntime, IacCodeA2APipelineExecutor

    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    task = await store.get_or_create_task(task_id="task-1", context_id="ctx-1")
    task.state = "working"
    task.active_task = asyncio.current_task()
    ctx = await store.get_or_create_context(
        context_id="ctx-1",
        cwd=str(tmp_path),
        runtime_factory=lambda _session_id: _fake_runtime(),
    )
    ctx.active_task_id = "task-1"
    ctx.runtime = A2APipelineRuntime(
        agent_runtime=_fake_runtime(),
        pipeline=FakePipeline([], session_dir=tmp_path / "sidecar"),
        publisher=None,
    )
    active_task = task.active_task

    executor = IacCodeA2APipelineExecutor(
        task_store=store,
        model="qwen3.6-plus",
        metrics=NoOpA2AMetrics(),
        artifact_store=None,
        push_notifier=None,
        permission_resolver=None,
        auto_approve_permissions=False,
        thinking_exposure_types=None,
    )
    queue = FakeEventQueue()

    await executor.execute(
        context=FakeRequestContext(text="second", metadata={"iac_code": {"cwd": str(tmp_path)}}),
        event_queue=queue,
        task=task,
        task_id="task-1",
        context_id="ctx-1",
        cwd=str(tmp_path),
        prompt="second",
    )

    assert ctx.active_task_id == "task-1"
    assert task.active_task is active_task
    assert task.state == "working"
    final_status = _status_events(queue)[-1]["status"]
    assert final_status["state"] == "TASK_STATE_FAILED"
    assert final_status["message"]["parts"][0]["text"] == "Task is already working."


@pytest.mark.asyncio
async def test_active_pipeline_interrupt_receives_structured_image_input(tmp_path: Path) -> None:
    from iac_code.a2a.pipeline_executor import IacCodeA2APipelineExecutor

    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    task = await store.get_or_create_task(task_id="task-1", context_id="ctx-1")
    task.active_task = asyncio.current_task()
    ctx = await store.get_or_create_context(
        context_id="ctx-1",
        cwd=str(tmp_path),
        runtime_factory=lambda session_id: _fake_runtime(),
    )
    ctx.active_task_id = "task-1"
    received = []

    class InterruptPipeline(FakePipeline):
        async def handle_user_interrupt(self, message):
            received.append(message)
            return InterruptVerdict(action="continue", reason="keep going")

        def pause_agent_loops(self) -> None:
            pass

        def resume_agent_loops(self) -> None:
            pass

    pipeline = InterruptPipeline([], session_dir=tmp_path / "pipeline")
    publisher = SimpleNamespace(
        publish_interrupt_received=AsyncMock(),
        publish_interrupt=AsyncMock(),
        journal=A2APipelineJournal(tmp_path / "pipeline"),
        snapshot_store=A2APipelineSnapshotStore(tmp_path / "pipeline"),
    )
    ctx.runtime = SimpleNamespace(
        agent_runtime=_fake_runtime(),
        pipeline=pipeline,
        publisher=publisher,
        current_stream=None,
        restart_after_interrupt=False,
        pause_after_interrupt=False,
        restart_requested=asyncio.Event(),
    )
    store.mirror_context(ctx)
    pipeline_input = image_interrupt_input()

    executor = IacCodeA2APipelineExecutor(
        task_store=store,
        model="qwen3.6-plus",
        metrics=NoOpA2AMetrics(),
        artifact_store=None,
        push_notifier=None,
        permission_resolver=None,
        auto_approve_permissions=False,
        thinking_exposure_types=None,
    )
    await executor.execute(
        context=FakeRequestContext(metadata={"iac_code": {"cwd": str(tmp_path)}}),
        event_queue=FakeEventQueue(),
        task=task,
        task_id="task-1",
        context_id="ctx-1",
        cwd=str(tmp_path),
        pipeline_input=pipeline_input,
    )

    assert received == [pipeline_input]
    publisher.publish_interrupt_received.assert_awaited_once_with(prompt="[Image input]")


@pytest.mark.asyncio
async def test_active_pending_question_answer_preserves_image_input(tmp_path: Path) -> None:
    from iac_code.a2a.pipeline_executor import IacCodeA2APipelineExecutor, _PendingAskUserQuestion

    future = asyncio.get_running_loop().create_future()
    injected = []

    class Pipeline:
        def inject_pending_question_supplement(self, message, *, envelope):
            injected.append((message, envelope))

    runtime = SimpleNamespace(
        pending_question=_PendingAskUserQuestion(
            event=AskUserQuestionEvent(
                tool_use_id="toolu_1",
                question="Upload diagram",
                options=[],
                response_future=future,
            ),
            envelope={"scope": "pipeline", "inputId": "ask-toolu_1"},
        ),
        pipeline=Pipeline(),
        publisher=SimpleNamespace(
            publish_manual=AsyncMock(return_value=object()),
        ),
    )
    pipeline_input = image_interrupt_input()
    executor = IacCodeA2APipelineExecutor(
        task_store=A2ATaskStore(metrics=NoOpA2AMetrics()),
        model="qwen3.6-plus",
        metrics=NoOpA2AMetrics(),
        artifact_store=None,
        push_notifier=None,
        permission_resolver=None,
        auto_approve_permissions=False,
        thinking_exposure_types=None,
    )

    result = await executor._route_pending_question_answer(runtime, pipeline_input)

    assert result == "answered"
    answer = future.result()
    assert answer == {"selected_id": "", "selected_label": "", "free_text": "[Image input]"}
    assert injected == [(pipeline_input.content, {"scope": "pipeline", "inputId": "ask-toolu_1"})]


@pytest.mark.asyncio
async def test_active_pending_question_image_injection_failure_is_not_marked_answered(tmp_path: Path) -> None:
    from iac_code.a2a.pipeline_executor import IacCodeA2APipelineExecutor, _PendingAskUserQuestion

    future = asyncio.get_running_loop().create_future()

    class Pipeline:
        def inject_pending_question_supplement(self, message, *, envelope):
            return False

    runtime = SimpleNamespace(
        pending_question=_PendingAskUserQuestion(
            event=AskUserQuestionEvent(
                tool_use_id="toolu_1",
                question="Upload diagram",
                options=[],
                response_future=future,
            ),
            envelope={"scope": "pipeline", "inputId": "ask-toolu_1"},
        ),
        pipeline=Pipeline(),
        publisher=SimpleNamespace(
            publish_manual=AsyncMock(return_value=object()),
        ),
    )
    pipeline_input = image_interrupt_input()
    executor = IacCodeA2APipelineExecutor(
        task_store=A2ATaskStore(metrics=NoOpA2AMetrics()),
        model="qwen3.6-plus",
        metrics=NoOpA2AMetrics(),
        artifact_store=None,
        push_notifier=None,
        permission_resolver=None,
        auto_approve_permissions=False,
        thinking_exposure_types=None,
    )

    with pytest.raises(RuntimeError, match="image supplement could not be delivered"):
        await executor._route_pending_question_answer(runtime, pipeline_input)

    assert future.done() is False
    assert runtime.pending_question is not None


@pytest.mark.asyncio
async def test_active_pending_question_image_injection_failure_restores_snapshot_pending_input(tmp_path: Path) -> None:
    from iac_code.a2a.pipeline_events import PipelineA2AContext, PipelineEventTranslator
    from iac_code.a2a.pipeline_executor import IacCodeA2APipelineExecutor, _PendingAskUserQuestion
    from iac_code.a2a.pipeline_journal import A2APipelineJournal
    from iac_code.a2a.pipeline_snapshot import A2APipelineSnapshotStore
    from iac_code.a2a.pipeline_stream import PipelineA2AEventPublisher

    future = asyncio.get_running_loop().create_future()

    class Pipeline:
        def inject_pending_question_supplement(self, message, *, envelope):
            return False

    publisher = PipelineA2AEventPublisher(
        event_queue=FakeEventQueue(),
        translator=PipelineEventTranslator(
            PipelineA2AContext(
                pipeline_run_id="ctx-1",
                task_id="task-1",
                context_id="ctx-1",
                pipeline_name="selling",
            )
        ),
        journal=A2APipelineJournal(tmp_path / "pipeline"),
        snapshot_store=A2APipelineSnapshotStore(tmp_path / "pipeline"),
    )
    await publisher.publish_manual(
        "input_required",
        "pipeline",
        status="input_required",
        data={
            "kind": "ask_user_question",
            "inputId": "ask-toolu_1",
            "toolUseId": "toolu_1",
            "question": "Upload diagram",
            "prompt": "Upload diagram",
            "options": [],
            "required": True,
        },
    )
    assert publisher.snapshot_store.load()["pendingInput"]["inputId"] == "ask-toolu_1"

    runtime = SimpleNamespace(
        pending_question=_PendingAskUserQuestion(
            event=AskUserQuestionEvent(
                tool_use_id="toolu_1",
                question="Upload diagram",
                options=[],
                response_future=future,
            ),
            envelope={"scope": "pipeline", "inputId": "ask-toolu_1"},
        ),
        pipeline=Pipeline(),
        publisher=publisher,
    )
    executor = IacCodeA2APipelineExecutor(
        task_store=A2ATaskStore(metrics=NoOpA2AMetrics()),
        model="qwen3.6-plus",
        metrics=NoOpA2AMetrics(),
        artifact_store=None,
        push_notifier=None,
        permission_resolver=None,
        auto_approve_permissions=False,
        thinking_exposure_types=None,
    )

    with pytest.raises(RuntimeError, match="image supplement could not be delivered"):
        await executor._route_pending_question_answer(runtime, image_interrupt_input())

    snapshot = publisher.snapshot_store.load()
    assert snapshot["status"] == "waiting_input"
    assert snapshot["pendingInput"]["inputId"] == "ask-toolu_1"
    assert future.done() is False
    assert runtime.pending_question is not None


@pytest.mark.asyncio
async def test_execute_reports_active_pending_question_image_injection_failure(tmp_path: Path) -> None:
    from iac_code.a2a.pipeline_events import PipelineA2AContext, PipelineEventTranslator
    from iac_code.a2a.pipeline_executor import (
        A2APipelineRuntime,
        IacCodeA2APipelineExecutor,
        _PendingAskUserQuestion,
    )
    from iac_code.a2a.pipeline_journal import A2APipelineJournal
    from iac_code.a2a.pipeline_snapshot import A2APipelineSnapshotStore
    from iac_code.a2a.pipeline_stream import PipelineA2AEventPublisher

    future = asyncio.get_running_loop().create_future()

    class Pipeline:
        def inject_pending_question_supplement(self, message, *, envelope):
            return False

    queue = FakeEventQueue()
    publisher = PipelineA2AEventPublisher(
        event_queue=queue,
        translator=PipelineEventTranslator(
            PipelineA2AContext(
                pipeline_run_id="ctx-1",
                task_id="task-1",
                context_id="ctx-1",
                pipeline_name="selling",
            )
        ),
        journal=A2APipelineJournal(tmp_path / "pipeline"),
        snapshot_store=A2APipelineSnapshotStore(tmp_path / "pipeline"),
    )
    publisher.publish_manual = AsyncMock(return_value=object())  # type: ignore[method-assign]
    runtime = A2APipelineRuntime(agent_runtime=_fake_runtime(), pipeline=Pipeline(), publisher=publisher)
    runtime.pending_question = _PendingAskUserQuestion(
        event=AskUserQuestionEvent(
            tool_use_id="toolu_1",
            question="Upload diagram",
            options=[],
            response_future=future,
        ),
        envelope={"scope": "pipeline", "inputId": "ask-toolu_1"},
    )
    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    task = await store.get_or_create_task(task_id="task-1", context_id="ctx-1")
    task.state = "input-required"
    ctx = await store.get_or_create_context(
        context_id="ctx-1",
        cwd=str(tmp_path),
        runtime_factory=lambda _session_id: _fake_runtime(),
    )
    ctx.runtime = runtime
    ctx.active_task_id = "task-1"
    executor = IacCodeA2APipelineExecutor(
        task_store=store,
        model="qwen3.6-plus",
        metrics=NoOpA2AMetrics(),
        artifact_store=None,
        push_notifier=None,
        permission_resolver=None,
        auto_approve_permissions=False,
        thinking_exposure_types=None,
    )

    await executor.execute(
        context=FakeRequestContext(task_id="task-1", context_id="ctx-1"),
        event_queue=queue,
        task=task,
        task_id="task-1",
        context_id="ctx-1",
        cwd=str(tmp_path),
        pipeline_input=image_interrupt_input(),
    )

    states = [dump(event)["status"]["state"] for event in queue.events if isinstance(event, TaskStatusUpdateEvent)]
    assert "TASK_STATE_FAILED" in states
    assert future.done() is False
    assert runtime.pending_question is not None


@pytest.mark.asyncio
async def test_pending_ask_user_question_resume_preserves_image_input(tmp_path: Path) -> None:
    from iac_code.a2a.pipeline_executor import _resume_pending_ask_user_question_stream

    pipeline_input = image_interrupt_input()
    received = {}

    class AskPipeline(FakePipeline):
        sidecar_status = "waiting_input"

        async def resume_ask_user_question(self, answer, **kwargs):
            received["answer"] = answer
            received["supplemental_input"] = kwargs.get("supplemental_input")
            yield PipelineEvent(
                type=PipelineEventType.PIPELINE_COMPLETED,
                step_id="ask",
                timestamp=0.0,
                data={"total_steps": 1},
            )

    pending_input = {
        "kind": "ask_user_question",
        "toolUseId": "toolu_1",
        "inputId": "ask-toolu_1",
    }
    pipeline = AskPipeline([], session_dir=tmp_path / "pipeline")
    publisher = SimpleNamespace(
        snapshot_store=SimpleNamespace(load=lambda: {"status": "waiting_input"}),
        publish_manual=AsyncMock(return_value=object()),
    )

    stream = _resume_pending_ask_user_question_stream(
        pipeline=pipeline,
        publisher=publisher,
        pending_input=pending_input,
        prompt="[Image input]",
        pipeline_input=pipeline_input,
    )
    events = [event async for event in stream]

    assert events
    assert received["supplemental_input"] == pipeline_input
