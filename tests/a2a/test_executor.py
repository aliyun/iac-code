import asyncio
import logging
from pathlib import Path

import pytest
from a2a.types import TaskStatusUpdateEvent
from a2a.utils.errors import InvalidParamsError
from google.protobuf.json_format import MessageToDict

from iac_code.a2a.executor import IacCodeA2AExecutor
from iac_code.a2a.exposure import A2AExposureType
from iac_code.a2a.metrics import NoOpA2AMetrics
from iac_code.a2a.persistence import A2AContextSnapshot, A2APersistenceStore, A2ATaskSnapshot
from iac_code.a2a.pipeline_journal import A2APipelineJournal
from iac_code.a2a.pipeline_paths import a2a_pipeline_dir_for_session
from iac_code.a2a.task_store import A2ATaskStore
from iac_code.agent.message import ImageBlock
from iac_code.pipeline.engine.user_input import PipelineUserInput
from iac_code.types.stream_events import PermissionRequestEvent, TextDeltaEvent, ToolResultEvent

from .fakes import FakeAgentLoop, FakeEventQueue, FakeRequestContext, FakeRuntime, pending_future


def dump(event):
    return MessageToDict(event, preserving_proto_field_name=False)


def _image_only_pipeline_input() -> PipelineUserInput:
    return PipelineUserInput(
        content=[ImageBlock(media_type="image/png", data="aGVsbG8=")],
        display_text="[Image input]",
        has_images=True,
    )


@pytest.fixture(autouse=True)
def default_normal_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("IAC_CODE_MODE", raising=False)


@pytest.mark.asyncio
async def test_executor_runs_prompt_and_finishes_input_required(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    loop = FakeAgentLoop([TextDeltaEvent(text="hi")])
    runtime = FakeRuntime(agent_loop=loop, session_id="session-1")

    monkeypatch.setattr("iac_code.a2a.executor.create_agent_runtime", lambda options: runtime)

    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    executor = IacCodeA2AExecutor(task_store=store, model="qwen3.6-plus")
    queue = FakeEventQueue()
    context = FakeRequestContext(metadata={"iac_code": {"cwd": str(tmp_path)}})

    await executor.execute(context, queue)

    assert loop.prompts == ["hello"]
    states = [dump(event)["status"]["state"] for event in queue.events if isinstance(event, TaskStatusUpdateEvent)]
    assert states[0] == "TASK_STATE_SUBMITTED"
    assert "TASK_STATE_WORKING" in states
    assert states[-1] == "TASK_STATE_INPUT_REQUIRED"
    record = await store.get_or_create_task(task_id="task-1", context_id="ctx-1")
    assert "".join(record.output_text) == "hi"


@pytest.mark.asyncio
async def test_executor_passes_artifact_store_to_stream_event_publisher(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    artifact_store = object()
    seen_artifact_stores: list[object | None] = []
    seen_auto_approve_permissions: list[bool] = []
    seen_exposure_types: list[frozenset[A2AExposureType]] = []

    async def spy_publish_stream_event(
        event_queue,
        *,
        task_id,
        context_id,
        event,
        artifact_store=None,
        permission_resolver=None,
        auto_approve_permissions=False,
        exposure_types=None,
    ):
        seen_artifact_stores.append(artifact_store)
        seen_auto_approve_permissions.append(auto_approve_permissions)
        seen_exposure_types.append(exposure_types)
        return None

    loop = FakeAgentLoop(
        [
            ToolResultEvent(
                tool_use_id="tool-1",
                tool_name="write_file",
                result={"artifact": {"filename": "out.txt", "content": "hello", "mediaType": "text/plain"}},
                is_error=False,
            )
        ]
    )
    runtime = FakeRuntime(agent_loop=loop, session_id="session-1")
    monkeypatch.setattr("iac_code.a2a.executor.create_agent_runtime", lambda options: runtime)
    monkeypatch.setattr("iac_code.a2a.executor.publish_stream_event", spy_publish_stream_event)

    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    executor = IacCodeA2AExecutor(
        task_store=store,
        model="qwen3.6-plus",
        artifact_store=artifact_store,
        thinking_exposure_types=[A2AExposureType.RAW_THINKING, A2AExposureType.TOOL_TRACE],
    )

    await executor.execute(FakeRequestContext(metadata={"iac_code": {"cwd": str(tmp_path)}}), FakeEventQueue())

    assert seen_artifact_stores == [artifact_store]
    assert seen_auto_approve_permissions == [False]
    assert seen_exposure_types == [frozenset({A2AExposureType.RAW_THINKING, A2AExposureType.TOOL_TRACE})]


@pytest.mark.asyncio
async def test_executor_auto_approves_permissions_when_configured(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    future = pending_future()
    loop = FakeAgentLoop(
        [
            PermissionRequestEvent(
                tool_name="bash",
                tool_input={"cmd": "pwd"},
                tool_use_id="tool-1",
                response_future=future,
            )
        ]
    )
    runtime = FakeRuntime(agent_loop=loop, session_id="session-1")
    monkeypatch.setattr("iac_code.a2a.executor.create_agent_runtime", lambda options: runtime)

    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    executor = IacCodeA2AExecutor(
        task_store=store,
        model="qwen3.6-plus",
        auto_approve_permissions=True,
    )
    queue = FakeEventQueue()

    await executor.execute(FakeRequestContext(metadata={"iac_code": {"cwd": str(tmp_path)}}), queue)

    assert future.result() is True
    permission_events = [
        dump(event)["metadata"]["iac_code"]["permission"]
        for event in queue.events
        if "permission" in dump(event).get("metadata", {}).get("iac_code", {})
    ]
    assert permission_events[0]["autoApproved"] is True


@pytest.mark.asyncio
async def test_executor_persists_terminal_task_state_and_output(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    loop = FakeAgentLoop([TextDeltaEvent(text="persisted output")])
    runtime = FakeRuntime(agent_loop=loop, session_id="session-1")
    persistence = A2APersistenceStore(tmp_path / "state")
    monkeypatch.setattr("iac_code.a2a.executor.create_agent_runtime", lambda options: runtime)

    store = A2ATaskStore(metrics=NoOpA2AMetrics(), persistence=persistence)
    executor = IacCodeA2AExecutor(task_store=store, model="qwen3.6-plus")

    await executor.execute(FakeRequestContext(metadata={"iac_code": {"cwd": str(tmp_path)}}), FakeEventQueue())

    snapshot = persistence.load_task("task-1")
    assert snapshot is not None
    assert snapshot.state == "input-required"
    assert snapshot.output_text == ["persisted output"]


@pytest.mark.asyncio
async def test_executor_persists_working_state_for_interrupted_restoration(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    started = asyncio.Event()

    class SlowLoop:
        async def run_streaming(self, prompt: str):
            started.set()
            await asyncio.sleep(5)
            yield TextDeltaEvent(text="never")

    runtime = FakeRuntime(agent_loop=SlowLoop(), session_id="session-1")
    persistence = A2APersistenceStore(tmp_path / "state")
    monkeypatch.setattr("iac_code.a2a.executor.create_agent_runtime", lambda options: runtime)

    store = A2ATaskStore(metrics=NoOpA2AMetrics(), persistence=persistence)
    executor = IacCodeA2AExecutor(task_store=store, model="qwen3.6-plus")
    context = FakeRequestContext(metadata={"iac_code": {"cwd": str(tmp_path)}})
    queue = FakeEventQueue()
    running = asyncio.create_task(executor.execute(context, queue))
    await asyncio.wait_for(started.wait(), timeout=5.0)

    task_snapshot = persistence.load_task("task-1")
    context_snapshot = persistence.load_context("ctx-1")
    assert task_snapshot is not None
    assert task_snapshot.state == "working"
    assert context_snapshot is not None
    assert context_snapshot.active_task_id == "task-1"

    await executor.cancel(context, queue)
    await running


@pytest.mark.asyncio
async def test_executor_notifies_push_for_terminal_success(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    class SpyPushNotifier:
        def __init__(self) -> None:
            self.calls: list[dict[str, str]] = []

        async def notify_task_state(self, **kwargs) -> bool:
            self.calls.append(kwargs)
            return True

    loop = FakeAgentLoop([TextDeltaEvent(text="hi")])
    runtime = FakeRuntime(agent_loop=loop, session_id="session-1")
    notifier = SpyPushNotifier()
    monkeypatch.setattr("iac_code.a2a.executor.create_agent_runtime", lambda options: runtime)

    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    executor = IacCodeA2AExecutor(task_store=store, model="qwen3.6-plus", push_notifier=notifier)

    await executor.execute(FakeRequestContext(metadata={"iac_code": {"cwd": str(tmp_path)}}), FakeEventQueue())

    assert notifier.calls == [{"task_id": "task-1", "context_id": "ctx-1", "state": "input-required"}]


@pytest.mark.asyncio
async def test_executor_logs_and_swallows_push_failures(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    class FailingPushNotifier:
        async def notify_task_state(self, **kwargs) -> bool:
            raise RuntimeError("push endpoint down")

    class ExplodingLoop:
        async def run_streaming(self, prompt: str):
            raise RuntimeError("internal failure")
            yield TextDeltaEvent(text="never")

    runtime = FakeRuntime(agent_loop=ExplodingLoop(), session_id="session-1")
    monkeypatch.setattr("iac_code.a2a.executor.create_agent_runtime", lambda options: runtime)
    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    executor = IacCodeA2AExecutor(task_store=store, model="qwen3.6-plus", push_notifier=FailingPushNotifier())

    await executor.execute(FakeRequestContext(metadata={"iac_code": {"cwd": str(tmp_path)}}), FakeEventQueue())

    assert "A2A push notification failed" in caplog.text


@pytest.mark.asyncio
async def test_executor_creates_missing_workspace(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    runtime = FakeRuntime(agent_loop=FakeAgentLoop([TextDeltaEvent(text="hi")]), session_id="session-1")
    monkeypatch.setattr("iac_code.a2a.executor.create_agent_runtime", lambda options: runtime)

    missing = tmp_path / "missing"
    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    executor = IacCodeA2AExecutor(task_store=store, model="qwen3.6-plus")
    queue = FakeEventQueue()
    context = FakeRequestContext(metadata={"iac_code": {"cwd": str(missing)}})

    await executor.execute(context, queue)

    assert missing.is_dir()
    final_state = dump(queue.events[-1])["status"]["state"]
    assert final_state != "TASK_STATE_FAILED"


@pytest.mark.asyncio
async def test_executor_uses_metadata_cwd_when_process_cwd_is_deleted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    runtime = FakeRuntime(agent_loop=FakeAgentLoop([TextDeltaEvent(text="hi")]), session_id="session-1")
    monkeypatch.setattr("iac_code.a2a.executor.create_agent_runtime", lambda options: runtime)
    monkeypatch.setenv("IACCODE_A2A_ALLOWED_CWDS", str(tmp_path))

    def deleted_process_cwd() -> str:
        raise FileNotFoundError("[Errno 2] No such file or directory")

    monkeypatch.setattr("iac_code.a2a.executor.os.getcwd", deleted_process_cwd)

    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    executor = IacCodeA2AExecutor(task_store=store, model="qwen3.6-plus")
    queue = FakeEventQueue()

    await executor.execute(FakeRequestContext(metadata={"iac_code": {"cwd": str(tmp_path)}}), queue)

    assert runtime.agent_loop.prompts == ["hello"]
    final_state = dump(queue.events[-1])["status"]["state"]
    assert final_state != "TASK_STATE_FAILED"


def test_resolve_cwd_returns_logical_metadata_path_for_symlinked_workspace(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    physical_root = tmp_path / "mount-root"
    physical_root.mkdir()
    logical_root = tmp_path / "workspace"
    logical_root.symlink_to(physical_root, target_is_directory=True)
    logical_cwd = logical_root / "ctx-1"
    monkeypatch.setenv("IACCODE_A2A_ALLOWED_CWDS", str(logical_root))

    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    executor = IacCodeA2AExecutor(task_store=store, model="qwen3.6-plus")

    cwd = executor._resolve_cwd({"iac_code": {"cwd": str(logical_cwd)}})

    assert cwd == str(logical_cwd)
    assert logical_cwd.is_dir()
    assert logical_cwd.resolve() == physical_root / "ctx-1"


@pytest.mark.asyncio
async def test_executor_rejects_workspace_path_pointing_at_file(tmp_path: Path) -> None:
    file_path = tmp_path / "not-a-dir"
    file_path.write_text("blocker", encoding="utf-8")
    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    executor = IacCodeA2AExecutor(task_store=store, model="qwen3.6-plus")
    queue = FakeEventQueue()
    context = FakeRequestContext(metadata={"iac_code": {"cwd": str(file_path)}})

    await executor.execute(context, queue)

    dumped = dump(queue.events[-1])
    assert dumped["status"]["state"] == "TASK_STATE_FAILED"
    assert "workspace" in dumped["status"]["message"]["parts"][0]["text"].lower()


@pytest.mark.asyncio
async def test_executor_rejects_workspace_outside_allowed_roots(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    allowed = tmp_path / "allowed"
    outside = tmp_path / "outside"
    allowed.mkdir()
    outside.mkdir()
    monkeypatch.setenv("IACCODE_A2A_ALLOWED_CWDS", str(allowed))
    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    executor = IacCodeA2AExecutor(task_store=store, model="qwen3.6-plus")
    queue = FakeEventQueue()
    context = FakeRequestContext(metadata={"iac_code": {"cwd": str(outside)}})

    await executor.execute(context, queue)

    dumped = dump(queue.events[-1])
    assert dumped["status"]["state"] == "TASK_STATE_FAILED"
    assert "workspace" in dumped["status"]["message"]["parts"][0]["text"].lower()


@pytest.mark.asyncio
async def test_executor_reports_invalid_task_id() -> None:
    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    executor = IacCodeA2AExecutor(task_store=store, model="qwen3.6-plus")
    queue = FakeEventQueue()
    context = FakeRequestContext(task_id="../bad")

    await executor.execute(context, queue)

    dumped = dump(queue.events[-1])
    assert dumped["status"]["state"] == "TASK_STATE_FAILED"
    assert dumped["status"]["message"]["parts"][0]["text"] == "Invalid A2A id"


@pytest.mark.asyncio
async def test_executor_rejects_empty_prompt_before_creating_runtime(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def fail_if_called(options):
        raise AssertionError("runtime should not be created for empty prompt")

    monkeypatch.setattr("iac_code.a2a.executor.create_agent_runtime", fail_if_called)

    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    executor = IacCodeA2AExecutor(task_store=store, model="qwen3.6-plus")
    queue = FakeEventQueue()
    context = FakeRequestContext(text="   ", metadata={"iac_code": {"cwd": str(tmp_path)}})

    await executor.execute(context, queue)

    dumped = dump(queue.events[-1])
    assert dumped["status"]["state"] == "TASK_STATE_FAILED"
    assert dumped["status"]["message"]["parts"][0]["text"] == "A2A server currently accepts text input only."


@pytest.mark.asyncio
async def test_executor_delegates_pipeline_mode_after_validation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("IAC_CODE_MODE", "pipeline")
    calls = []

    class SpyPipelineExecutor:
        def __init__(self, **kwargs):
            calls.append(("init", kwargs))

        async def execute(self, *, context, event_queue, task, task_id, context_id, cwd, pipeline_input):
            calls.append(
                (
                    "execute",
                    {
                        "task_id": task_id,
                        "context_id": context_id,
                        "cwd": cwd,
                        "pipeline_input": pipeline_input,
                    },
                )
            )

    monkeypatch.setattr("iac_code.a2a.executor.IacCodeA2APipelineExecutor", SpyPipelineExecutor)

    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    executor = IacCodeA2AExecutor(task_store=store, model="qwen3.6-plus")

    await executor.execute(FakeRequestContext(metadata={"iac_code": {"cwd": str(tmp_path)}}), FakeEventQueue())

    assert calls[-1] == (
        "execute",
        {
            "task_id": "task-1",
            "context_id": "ctx-1",
            "cwd": str(tmp_path),
            "pipeline_input": PipelineUserInput(content="hello", display_text="hello", has_images=False),
        },
    )


@pytest.mark.asyncio
async def test_executor_hydrates_running_pipeline_task_id_from_sidecar(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("IAC_CODE_MODE", "pipeline")
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    persistence = A2APersistenceStore(tmp_path / "a2a-state")
    persistence.save_context(A2AContextSnapshot(context_id="ctx-1", session_id="session-1", cwd=str(tmp_path)))
    persistence.save_task(A2ATaskSnapshot(task_id="task-1", context_id="ctx-1", state="working"))
    journal = A2APipelineJournal(a2a_pipeline_dir_for_session(cwd=str(tmp_path), session_id="session-1"))
    journal.append(
        {
            "schemaVersion": "1.0",
            "eventId": "evt-running",
            "sequence": 1,
            "eventType": "step_started",
            "scope": "step",
            "pipelineRunId": "ctx-1",
            "pipelineName": "selling",
            "contextId": "ctx-1",
            "taskId": "task-1",
            "status": "working",
            "data": {},
        }
    )
    calls = []

    class SpyPipelineExecutor:
        def __init__(self, **kwargs):
            calls.append(("init", kwargs))

        async def execute(self, *, context, event_queue, task, task_id, context_id, cwd, pipeline_input):
            calls.append(
                (
                    "execute",
                    {
                        "task_id": task_id,
                        "context_id": context_id,
                        "cwd": cwd,
                        "pipeline_input": pipeline_input,
                    },
                )
            )

    monkeypatch.setattr("iac_code.a2a.executor.IacCodeA2APipelineExecutor", SpyPipelineExecutor)

    store = A2ATaskStore(metrics=NoOpA2AMetrics(), persistence=persistence)
    executor = IacCodeA2AExecutor(task_store=store, model="qwen3.6-plus")

    await executor.execute(
        FakeRequestContext(
            task_id="",
            context_id="ctx-1",
            text="继续",
            metadata={"iac_code": {"cwd": str(tmp_path)}},
        ),
        FakeEventQueue(),
    )

    assert calls[-1] == (
        "execute",
        {
            "task_id": "task-1",
            "context_id": "ctx-1",
            "cwd": str(tmp_path),
            "pipeline_input": PipelineUserInput(content="继续", display_text="继续", has_images=False),
        },
    )


@pytest.mark.asyncio
async def test_pipeline_mode_accepts_image_only_input(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("IAC_CODE_MODE", "pipeline")
    pipeline_input = _image_only_pipeline_input()
    calls = []

    class CapturingPipelineExecutor:
        def __init__(self, **kwargs):
            pass

        async def execute(self, **kwargs):
            calls.append(kwargs)

    monkeypatch.setattr("iac_code.a2a.executor.IacCodeA2APipelineExecutor", CapturingPipelineExecutor)
    monkeypatch.setattr(
        IacCodeA2AExecutor,
        "_pipeline_input_from_context",
        lambda self, context, *, cwd: pipeline_input,
    )
    monkeypatch.setattr("iac_code.a2a.executor.is_model_multimodal", lambda *args, **kwargs: True)

    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    executor = IacCodeA2AExecutor(task_store=store, model="qwen3.6-plus")
    queue = FakeEventQueue()

    await executor.execute(FakeRequestContext(metadata={"iac_code": {"cwd": str(tmp_path)}}), queue)

    assert calls
    assert calls[0]["pipeline_input"] == pipeline_input
    states = [dump(event)["status"]["state"] for event in queue.events if isinstance(event, TaskStatusUpdateEvent)]
    assert "TASK_STATE_FAILED" not in states


@pytest.mark.asyncio
async def test_pipeline_mode_image_input_checks_provider_context(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("IAC_CODE_MODE", "pipeline")
    monkeypatch.setattr(
        IacCodeA2AExecutor,
        "_pipeline_input_from_context",
        lambda self, context, *, cwd: _image_only_pipeline_input(),
    )
    seen = {}

    def fake_is_model_multimodal(model, *, provider_key=None, base_url=None, api_key=None):
        seen.update(
            {
                "model": model,
                "provider_key": provider_key,
                "base_url": base_url,
                "api_key": api_key,
            }
        )
        return False

    monkeypatch.setattr("iac_code.a2a.executor.get_active_provider_key", lambda: "openai_compatible")
    monkeypatch.setattr(
        "iac_code.a2a.executor.get_provider_config",
        lambda provider_key: {"keyName": provider_key, "apiBase": "https://example.test/v1"},
    )
    monkeypatch.setattr(
        "iac_code.a2a.executor.load_credentials",
        lambda model=None: {"openai_compatible": "test-key"},
    )
    monkeypatch.setattr("iac_code.a2a.executor.is_model_multimodal", fake_is_model_multimodal)

    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    executor = IacCodeA2AExecutor(task_store=store, model="custom-vl")

    queue = FakeEventQueue()
    with pytest.raises(InvalidParamsError, match="Current model custom-vl does not support image input"):
        await executor.execute(
            FakeRequestContext(metadata={"iac_code": {"cwd": str(tmp_path)}}),
            queue,
        )

    assert seen == {
        "model": "custom-vl",
        "provider_key": "openai_compatible",
        "base_url": "https://example.test/v1",
        "api_key": "test-key",
    }
    assert not [event for event in queue.events if isinstance(event, TaskStatusUpdateEvent)]
    with pytest.raises(ValueError, match="A2A task not found"):
        await store.get_task_record("task-1")


@pytest.mark.asyncio
async def test_executor_empty_prompt_takes_precedence_over_pipeline_mode(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("IAC_CODE_MODE", "pipeline")

    def fail_if_called(options):  # noqa: ARG001
        raise AssertionError("runtime should not be created for empty prompt")

    monkeypatch.setattr("iac_code.a2a.executor.create_agent_runtime", fail_if_called)

    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    executor = IacCodeA2AExecutor(task_store=store, model="qwen3.6-plus")
    queue = FakeEventQueue()
    context = FakeRequestContext(text="   ", metadata={"iac_code": {"cwd": str(tmp_path)}})

    with pytest.raises(InvalidParamsError, match="A2A server received empty input"):
        await executor.execute(context, queue)
    assert not [event for event in queue.events if isinstance(event, TaskStatusUpdateEvent)]
    with pytest.raises(ValueError, match="A2A task not found"):
        await store.get_task_record("task-1")


@pytest.mark.asyncio
async def test_executor_workspace_errors_take_precedence_over_pipeline_mode(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("IAC_CODE_MODE", "pipeline")

    def fail_if_called(options):  # noqa: ARG001
        raise AssertionError("runtime should not be created for invalid workspace")

    monkeypatch.setattr("iac_code.a2a.executor.create_agent_runtime", fail_if_called)

    file_path = tmp_path / "not-a-dir"
    file_path.write_text("blocker", encoding="utf-8")
    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    executor = IacCodeA2AExecutor(task_store=store, model="qwen3.6-plus")
    queue = FakeEventQueue()
    context = FakeRequestContext(metadata={"iac_code": {"cwd": str(file_path)}})

    await executor.execute(context, queue)

    dumped = dump(queue.events[-1])
    assert dumped["status"]["state"] == "TASK_STATE_FAILED"
    assert "workspace" in dumped["status"]["message"]["parts"][0]["text"].lower()


@pytest.mark.asyncio
async def test_executor_runs_normal_mode_when_iac_code_mode_is_normal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("IAC_CODE_MODE", "normal")
    loop = FakeAgentLoop([TextDeltaEvent(text="normal")])
    runtime = FakeRuntime(agent_loop=loop, session_id="session-1")
    monkeypatch.setattr("iac_code.a2a.executor.create_agent_runtime", lambda options: runtime)

    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    executor = IacCodeA2AExecutor(task_store=store, model="qwen3.6-plus")
    queue = FakeEventQueue()

    await executor.execute(FakeRequestContext(metadata={"iac_code": {"cwd": str(tmp_path)}}), queue)

    assert loop.prompts == ["hello"]
    dumped = dump(queue.events[-1])
    assert dumped["status"]["state"] == "TASK_STATE_INPUT_REQUIRED"


@pytest.mark.asyncio
async def test_cancel_bypasses_context_lock(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    started = asyncio.Event()

    class SlowLoop:
        async def run_streaming(self, prompt: str):
            started.set()
            await asyncio.sleep(5)
            yield TextDeltaEvent(text="never")

    runtime = FakeRuntime(agent_loop=SlowLoop(), session_id="session-1")
    monkeypatch.setattr("iac_code.a2a.executor.create_agent_runtime", lambda options: runtime)

    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    executor = IacCodeA2AExecutor(task_store=store, model="qwen3.6-plus")
    context = FakeRequestContext(metadata={"iac_code": {"cwd": str(tmp_path)}})
    queue = FakeEventQueue()
    running = asyncio.create_task(executor.execute(context, queue))
    await asyncio.wait_for(started.wait(), timeout=5.0)

    await executor.cancel(context, queue)
    await running

    assert dump(queue.events[-1])["status"]["state"] == "TASK_STATE_CANCELED"


@pytest.mark.asyncio
async def test_same_context_concurrent_message_is_rejected(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    started = asyncio.Event()

    class SlowLoop:
        async def run_streaming(self, prompt: str):
            started.set()
            await asyncio.sleep(5)
            yield TextDeltaEvent(text="never")

    runtime = FakeRuntime(agent_loop=SlowLoop(), session_id="session-1")
    monkeypatch.setattr("iac_code.a2a.executor.create_agent_runtime", lambda options: runtime)

    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    executor = IacCodeA2AExecutor(task_store=store, model="qwen3.6-plus")
    first = FakeRequestContext(task_id="task-1", context_id="ctx-1", metadata={"iac_code": {"cwd": str(tmp_path)}})
    second = FakeRequestContext(task_id="task-2", context_id="ctx-1", metadata={"iac_code": {"cwd": str(tmp_path)}})
    first_queue = FakeEventQueue()
    second_queue = FakeEventQueue()
    running = asyncio.create_task(executor.execute(first, first_queue))
    await asyncio.wait_for(started.wait(), timeout=5.0)

    await executor.execute(second, second_queue)
    await executor.cancel(first, first_queue)
    await running

    dumped = dump(second_queue.events[-1])
    assert dumped["status"]["state"] == "TASK_STATE_FAILED"
    assert "already working" in dumped["status"]["message"]["parts"][0]["text"]


@pytest.mark.asyncio
async def test_same_context_lock_race_fails_fast(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    class ContendedLock:
        def __init__(self) -> None:
            self.acquire_requested = asyncio.Event()
            self.acquire_waiter = asyncio.get_running_loop().create_future()

        def acquire(self) -> asyncio.Future[bool]:
            self.acquire_requested.set()
            return self.acquire_waiter

        def release(self) -> None:
            raise AssertionError("release should not be called when acquire times out")

    runtime = FakeRuntime(agent_loop=FakeAgentLoop([TextDeltaEvent(text="never")]), session_id="session-1")
    monkeypatch.setattr("iac_code.a2a.executor.create_agent_runtime", lambda options: runtime)
    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    ctx = await store.get_or_create_context(
        context_id="ctx-1",
        cwd=str(tmp_path),
        runtime_factory=lambda sid: runtime,
    )
    lock = ContendedLock()
    ctx.lock = lock

    async def deterministic_timeout(awaitable, timeout):
        assert awaitable is lock.acquire_waiter
        assert timeout == 1
        raise TimeoutError

    monkeypatch.setattr("iac_code.a2a.executor.asyncio.wait_for", deterministic_timeout)
    executor = IacCodeA2AExecutor(task_store=store, model="qwen3.6-plus")
    queue = FakeEventQueue()

    await executor.execute(
        FakeRequestContext(
            task_id="task-2",
            context_id="ctx-1",
            metadata={"iac_code": {"cwd": str(tmp_path)}},
        ),
        queue,
    )

    assert lock.acquire_requested.is_set()
    dumped = dump(queue.events[-1])
    assert dumped["status"]["state"] == "TASK_STATE_FAILED"
    assert "already working" in dumped["status"]["message"]["parts"][0]["text"]


@pytest.mark.asyncio
async def test_independent_contexts_execute_concurrently(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    prompts: list[str] = []

    class FastLoop:
        async def run_streaming(self, prompt: str):
            prompts.append(prompt)
            await asyncio.sleep(0)
            yield TextDeltaEvent(text=prompt)

    monkeypatch.setattr(
        "iac_code.a2a.executor.create_agent_runtime",
        lambda options: FakeRuntime(agent_loop=FastLoop(), session_id=options.session_id),
    )

    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    executor = IacCodeA2AExecutor(task_store=store, model="qwen3.6-plus")
    await asyncio.gather(
        executor.execute(
            FakeRequestContext(
                task_id="task-1", context_id="ctx-1", text="one", metadata={"iac_code": {"cwd": str(tmp_path)}}
            ),
            FakeEventQueue(),
        ),
        executor.execute(
            FakeRequestContext(
                task_id="task-2", context_id="ctx-2", text="two", metadata={"iac_code": {"cwd": str(tmp_path)}}
            ),
            FakeEventQueue(),
        ),
    )

    assert sorted(prompts) == ["one", "two"]


@pytest.mark.asyncio
async def test_executor_overrides_telemetry_session_id_per_context(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Per-context conversation session_id must surface in telemetry while
    run_streaming is executing, instead of the process-level bootstrap id."""
    from iac_code.services.telemetry import bootstrap_telemetry, get_session_id, set_client
    from iac_code.services.telemetry.identity import SESSION_ID_PREFIX

    monkeypatch.setenv("DISABLE_TELEMETRY", "1")
    monkeypatch.setenv("HOME", str(tmp_path))
    set_client(None)
    bootstrap_telemetry(session_id="a2a-server-process")
    try:
        process_level = get_session_id()
        assert process_level == f"{SESSION_ID_PREFIX}a2a-server-process"

        observed: dict[str, str] = {}

        class ObservingLoop:
            def __init__(self, label: str) -> None:
                self._label = label

            async def run_streaming(self, prompt: str):
                observed[self._label] = get_session_id()
                yield TextDeltaEvent(text="ok")

        def factory(options):
            return FakeRuntime(
                agent_loop=ObservingLoop(label=options.session_id),
                session_id=options.session_id,
            )

        monkeypatch.setattr("iac_code.a2a.executor.create_agent_runtime", factory)

        store = A2ATaskStore(metrics=NoOpA2AMetrics())
        executor = IacCodeA2AExecutor(task_store=store, model="qwen3.6-plus")

        await executor.execute(
            FakeRequestContext(task_id="task-1", context_id="ctx-1", metadata={"iac_code": {"cwd": str(tmp_path)}}),
            FakeEventQueue(),
        )
        await executor.execute(
            FakeRequestContext(task_id="task-2", context_id="ctx-2", metadata={"iac_code": {"cwd": str(tmp_path)}}),
            FakeEventQueue(),
        )

        session_one = store._contexts["ctx-1"].session_id
        session_two = store._contexts["ctx-2"].session_id
        assert session_one != session_two
        assert observed[session_one] == f"{SESSION_ID_PREFIX}{session_one}"
        assert observed[session_two] == f"{SESSION_ID_PREFIX}{session_two}"
        # And the per-context override does not leak back to the parent scope.
        assert get_session_id() == process_level
    finally:
        set_client(None)


@pytest.mark.asyncio
async def test_executor_resumes_messages_after_restart(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from iac_code.a2a.persistence import A2APersistenceStore
    from iac_code.agent.message import Message
    from iac_code.services.session_storage import SessionStorage

    config_dir = tmp_path / "config"
    config_dir.mkdir()
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(config_dir))

    cwd = tmp_path / "ws"
    cwd.mkdir()

    seen_resume: list[object | None] = []

    def fake_factory(options):
        seen_resume.append(options.resume_messages)
        return FakeRuntime(
            agent_loop=FakeAgentLoop([TextDeltaEvent(text="ok")]),
            session_id=options.session_id,
        )

    monkeypatch.setattr("iac_code.a2a.executor.create_agent_runtime", fake_factory)

    persistence = A2APersistenceStore(tmp_path / "a2a")

    store_one = A2ATaskStore(metrics=NoOpA2AMetrics(), persistence=persistence)
    executor_one = IacCodeA2AExecutor(task_store=store_one, model="qwen3.6-plus")
    ctx_one = FakeRequestContext(
        task_id="task-1",
        context_id="ctx-shared",
        text="hi-1",
        metadata={"iac_code": {"cwd": str(cwd)}},
    )
    await executor_one.execute(ctx_one, FakeEventQueue())
    session_id = store_one._contexts["ctx-shared"].session_id

    SessionStorage().append(str(cwd), session_id, Message(role="user", content="prior turn"))

    store_two = A2ATaskStore(metrics=NoOpA2AMetrics(), persistence=persistence)
    executor_two = IacCodeA2AExecutor(task_store=store_two, model="qwen3.6-plus")
    ctx_two = FakeRequestContext(
        task_id="task-2",
        context_id="ctx-shared",
        text="hi-2",
        metadata={"iac_code": {"cwd": str(cwd)}},
    )
    await executor_two.execute(ctx_two, FakeEventQueue())

    assert store_two._contexts["ctx-shared"].session_id == session_id
    assert seen_resume[0] is None
    assert seen_resume[1] is not None
    assert any(getattr(m, "content", "") == "prior turn" for m in seen_resume[1])


@pytest.mark.asyncio
async def test_pipeline_handoff_context_routes_followup_to_normal_after_restart(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from iac_code.a2a.pipeline_paths import a2a_pipeline_dir_for_session
    from iac_code.a2a.pipeline_snapshot import A2APipelineSnapshotStore
    from iac_code.agent.message import Message
    from iac_code.services.session_storage import SessionStorage

    monkeypatch.setenv("IAC_CODE_MODE", "pipeline")
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(config_dir))

    cwd = tmp_path / "ws"
    cwd.mkdir()
    session_id = "session-handoff"
    context_id = "ctx-handoff"
    persistence = A2APersistenceStore(tmp_path / "a2a")
    persistence.save_context(A2AContextSnapshot(context_id=context_id, session_id=session_id, cwd=str(cwd)))
    A2APipelineSnapshotStore(a2a_pipeline_dir_for_session(cwd=str(cwd), session_id=session_id)).save(
        {
            "normalHandoff": {
                "action": "switch_to_normal",
                "targetMode": "normal",
                "summary": "[Pipeline Handoff Context]\nPipeline: selling",
            }
        }
    )
    SessionStorage().append(str(cwd), session_id, Message(role="user", content="[Pipeline Handoff Context]"))

    loop = FakeAgentLoop([TextDeltaEvent(text="normal-ok")])
    seen_resume: list[object | None] = []

    def fake_factory(options):
        seen_resume.append(options.resume_messages)
        return FakeRuntime(agent_loop=loop, session_id=options.session_id)

    class FailingPipelineExecutor:
        def __init__(self, **kwargs) -> None:
            pass

        async def execute(self, **kwargs) -> None:
            raise AssertionError("pipeline executor should not be used after normal handoff")

    monkeypatch.setattr("iac_code.a2a.executor.create_agent_runtime", fake_factory)
    monkeypatch.setattr("iac_code.a2a.executor.IacCodeA2APipelineExecutor", FailingPipelineExecutor)

    store = A2ATaskStore(metrics=NoOpA2AMetrics(), persistence=persistence)
    executor = IacCodeA2AExecutor(task_store=store, model="qwen3.6-plus")
    context = FakeRequestContext(
        task_id="task-followup",
        context_id=context_id,
        text="继续解释一下",
        metadata={"iac_code": {"cwd": str(cwd)}},
    )
    await executor.execute(context, FakeEventQueue())

    assert loop.prompts == ["继续解释一下"]
    assert store._contexts[context_id].session_id == session_id
    assert seen_resume and seen_resume[0] is not None
    assert any(getattr(message, "content", "") == "[Pipeline Handoff Context]" for message in seen_resume[0])


@pytest.mark.asyncio
async def test_pipeline_handoff_image_request_uses_normal_manifest_prompt(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from a2a.types import Message, Part, Role

    from iac_code.a2a.pipeline_paths import a2a_pipeline_dir_for_session
    from iac_code.a2a.pipeline_snapshot import A2APipelineSnapshotStore
    from iac_code.agent.message import Message as AgentMessage
    from iac_code.services.session_storage import SessionStorage

    monkeypatch.setenv("IAC_CODE_MODE", "pipeline")
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(config_dir))

    cwd = tmp_path / "ws"
    cwd.mkdir()
    session_id = "session-handoff"
    context_id = "ctx-handoff"
    persistence = A2APersistenceStore(tmp_path / "a2a")
    persistence.save_context(A2AContextSnapshot(context_id=context_id, session_id=session_id, cwd=str(cwd)))
    A2APipelineSnapshotStore(a2a_pipeline_dir_for_session(cwd=str(cwd), session_id=session_id)).save(
        {"normalHandoff": {"action": "switch_to_normal", "targetMode": "normal", "summary": "handoff"}}
    )
    SessionStorage().append(str(cwd), session_id, AgentMessage(role="user", content="handoff"))

    def fail_pipeline_input(*args, **kwargs):
        raise AssertionError("normal handoff must not build PipelineUserInput")

    monkeypatch.setattr(IacCodeA2AExecutor, "_pipeline_input_from_context", fail_pipeline_input)
    loop = FakeAgentLoop([TextDeltaEvent(text="normal-ok")])
    monkeypatch.setattr(
        "iac_code.a2a.executor.create_agent_runtime",
        lambda options: FakeRuntime(agent_loop=loop, session_id=options.session_id),
    )

    class FailingPipelineExecutor:
        def __init__(self, **kwargs) -> None:
            pass

        async def execute(self, **kwargs) -> None:
            raise AssertionError("pipeline executor should not be used after normal handoff")

    monkeypatch.setattr("iac_code.a2a.executor.IacCodeA2APipelineExecutor", FailingPipelineExecutor)

    context = FakeRequestContext(
        task_id="task-followup",
        context_id=context_id,
        text="",
        metadata={"iac_code": {"cwd": str(cwd)}},
    )
    context.message = Message(
        role=Role.ROLE_USER,
        parts=[Part(raw=b"\x89PNG\r\n\x1a\nimage", media_type="image/png", filename="diagram.png")],
        message_id="msg-1",
    )

    executor = IacCodeA2AExecutor(
        task_store=A2ATaskStore(metrics=NoOpA2AMetrics(), persistence=persistence),
        model="qwen3.6-plus",
    )
    await executor.execute(
        context,
        FakeEventQueue(),
    )

    assert loop.prompts
    assert "A2A multimodal attachment:" in loop.prompts[0]
    assert "mediaType=image/png" in loop.prompts[0]
    assert "[Image input]" not in loop.prompts[0]


@pytest.mark.asyncio
async def test_pipeline_handoff_context_is_backfilled_from_snapshot_when_session_missing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from iac_code.a2a.pipeline_paths import a2a_pipeline_dir_for_session
    from iac_code.a2a.pipeline_snapshot import A2APipelineSnapshotStore
    from iac_code.services.session_storage import SessionStorage

    monkeypatch.setenv("IAC_CODE_MODE", "pipeline")
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(config_dir))

    cwd = tmp_path / "ws"
    cwd.mkdir()
    session_id = "session-handoff"
    context_id = "ctx-handoff"
    summary = "[Pipeline Handoff Context]\nPipeline: selling"
    persistence = A2APersistenceStore(tmp_path / "a2a")
    persistence.save_context(A2AContextSnapshot(context_id=context_id, session_id=session_id, cwd=str(cwd)))
    A2APipelineSnapshotStore(a2a_pipeline_dir_for_session(cwd=str(cwd), session_id=session_id)).save(
        {
            "normalHandoff": {
                "action": "switch_to_normal",
                "targetMode": "normal",
                "summary": summary,
            }
        }
    )

    loop = FakeAgentLoop([TextDeltaEvent(text="normal-ok")])
    seen_resume: list[object | None] = []

    def fake_factory(options):
        seen_resume.append(options.resume_messages)
        return FakeRuntime(agent_loop=loop, session_id=options.session_id)

    class FailingPipelineExecutor:
        def __init__(self, **kwargs) -> None:
            pass

        async def execute(self, **kwargs) -> None:
            raise AssertionError("pipeline executor should not be used after normal handoff")

    monkeypatch.setattr("iac_code.a2a.executor.create_agent_runtime", fake_factory)
    monkeypatch.setattr("iac_code.a2a.executor.IacCodeA2APipelineExecutor", FailingPipelineExecutor)

    store = A2ATaskStore(metrics=NoOpA2AMetrics(), persistence=persistence)
    executor = IacCodeA2AExecutor(task_store=store, model="qwen3.6-plus")
    await executor.execute(
        FakeRequestContext(
            task_id="task-followup",
            context_id=context_id,
            text="继续解释一下",
            metadata={"iac_code": {"cwd": str(cwd)}},
        ),
        FakeEventQueue(),
    )

    assert loop.prompts == ["继续解释一下"]
    assert seen_resume and seen_resume[0] is not None
    assert any(getattr(message, "content", "") == summary for message in seen_resume[0])
    loaded = SessionStorage().load(str(cwd), session_id)
    assert loaded is not None
    assert any(getattr(message, "content", "") == summary for message in loaded)


@pytest.mark.asyncio
async def test_pipeline_handoff_context_routes_and_backfills_public_summary_from_journal_when_snapshot_corrupt(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from iac_code.a2a.pipeline_paths import a2a_pipeline_dir_for_session
    from iac_code.pipeline.engine.cleanup import CleanupLedger, CleanupResource
    from iac_code.services.session_storage import SessionStorage

    monkeypatch.setenv("IAC_CODE_MODE", "pipeline")
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(config_dir))

    cwd = tmp_path / "ws"
    cwd.mkdir()
    session_id = "session-handoff"
    context_id = "ctx-handoff"
    summary = "[Pipeline Handoff Context]\nPipeline: selling"
    cleanup_prompt = "cleanup prompt for stack-123"
    persistence = A2APersistenceStore(tmp_path / "a2a")
    persistence.save_context(A2AContextSnapshot(context_id=context_id, session_id=session_id, cwd=str(cwd)))
    pipeline_dir = a2a_pipeline_dir_for_session(cwd=str(cwd), session_id=session_id)
    pipeline_dir.mkdir(parents=True, exist_ok=True)
    (pipeline_dir / "a2a-snapshot.json").write_text("{broken", encoding="utf-8")
    A2APipelineJournal(pipeline_dir).append(
        {
            "schemaVersion": "1.0",
            "eventId": "evt-handoff",
            "sequence": 1,
            "createdAt": "2026-01-01T00:00:00Z",
            "eventType": "pipeline_handoff_ready",
            "scope": "pipeline",
            "pipelineRunId": context_id,
            "taskId": "task-pipeline",
            "contextId": context_id,
            "pipelineName": "selling",
            "status": "completed",
            "data": {
                "action": "switch_to_normal",
                "targetMode": "normal",
                "summary": summary,
                "cleanup": {
                    "status": "pending",
                    "resourceCount": 1,
                    "prompt": cleanup_prompt,
                    "resources": [{"resourceId": "stack-123", "regionId": "cn-hangzhou"}],
                },
            },
        }
    )
    ledger = CleanupLedger(SessionStorage().session_dir(str(cwd), session_id) / "pipeline" / "cleanup.yaml")
    ledger.mark_cleanup_required(
        [
            CleanupResource(
                provider="ros",
                resource_type="stack",
                resource_id="stack-123",
                region_id="cn-hangzhou",
                cleanup_status="completed",
                progress_status="DELETE_COMPLETE",
            )
        ],
        source_step_id="deploying",
        reason="rollback",
    )

    loop = FakeAgentLoop([TextDeltaEvent(text="normal-ok")])
    seen_resume: list[object | None] = []

    def fake_factory(options):
        seen_resume.append(options.resume_messages)
        return FakeRuntime(agent_loop=loop, session_id=options.session_id)

    class FailingPipelineExecutor:
        def __init__(self, **kwargs) -> None:
            pass

        async def execute(self, **kwargs) -> None:
            raise AssertionError("pipeline executor should not be used after normal handoff")

    monkeypatch.setattr("iac_code.a2a.executor.create_agent_runtime", fake_factory)
    monkeypatch.setattr("iac_code.a2a.executor.IacCodeA2APipelineExecutor", FailingPipelineExecutor)

    store = A2ATaskStore(metrics=NoOpA2AMetrics(), persistence=persistence)
    executor = IacCodeA2AExecutor(task_store=store, model="qwen3.6-plus")
    await executor.execute(
        FakeRequestContext(
            task_id="task-followup",
            context_id=context_id,
            text="继续解释一下",
            metadata={"iac_code": {"cwd": str(cwd)}},
        ),
        FakeEventQueue(),
    )

    assert loop.prompts == ["继续解释一下"]
    assert seen_resume and seen_resume[0] is not None
    assert any(getattr(message, "content", "") == summary for message in seen_resume[0])
    assert not any(getattr(message, "content", "") == cleanup_prompt for message in seen_resume[0])
    loaded = SessionStorage().load(str(cwd), session_id)
    assert loaded is not None
    assert any(getattr(message, "content", "") == summary for message in loaded)
    assert not any(getattr(message, "content", "") == cleanup_prompt for message in loaded)


@pytest.mark.asyncio
async def test_auth_error_is_sanitized(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def raise_auth_error(options):
        raise ValueError("provider not configured: secret internal detail")

    monkeypatch.setattr("iac_code.a2a.executor.create_agent_runtime", raise_auth_error)

    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    executor = IacCodeA2AExecutor(task_store=store, model="qwen3.6-plus")
    queue = FakeEventQueue()
    context = FakeRequestContext(metadata={"iac_code": {"cwd": str(tmp_path)}})

    await executor.execute(context, queue)

    dumped = dump(queue.events[-1])
    assert dumped["status"]["state"] == "TASK_STATE_FAILED"
    assert (
        dumped["status"]["message"]["parts"][0]["text"]
        == "Authentication required. Please configure your API credentials."
    )


@pytest.mark.asyncio
async def test_retryable_executor_error_returns_input_required(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    class TimeoutLoop:
        async def run_streaming(self, prompt: str):
            raise TimeoutError("upstream timed out")
            yield TextDeltaEvent(text="never")

    runtime = FakeRuntime(agent_loop=TimeoutLoop(), session_id="session-1")
    monkeypatch.setattr("iac_code.a2a.executor.create_agent_runtime", lambda options: runtime)

    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    executor = IacCodeA2AExecutor(task_store=store, model="qwen3.6-plus")
    queue = FakeEventQueue()
    context = FakeRequestContext(metadata={"iac_code": {"cwd": str(tmp_path)}})

    await executor.execute(context, queue)

    dumped = dump(queue.events[-1])
    assert dumped["status"]["state"] == "TASK_STATE_INPUT_REQUIRED"
    assert dumped["status"]["message"]["parts"][0]["text"] == "A temporary error occurred. Please retry."


@pytest.mark.asyncio
async def test_retryable_setup_error_returns_input_required(tmp_path: Path) -> None:
    class TimeoutTaskStore(A2ATaskStore):
        async def ensure_task_not_expired(self, task_id: str) -> None:
            raise TimeoutError("task store timed out")

    store = TimeoutTaskStore(metrics=NoOpA2AMetrics())
    executor = IacCodeA2AExecutor(task_store=store, model="qwen3.6-plus")
    queue = FakeEventQueue()
    context = FakeRequestContext(metadata={"iac_code": {"cwd": str(tmp_path)}})

    await executor.execute(context, queue)

    dumped = dump(queue.events[-1])
    assert dumped["status"]["state"] == "TASK_STATE_INPUT_REQUIRED"
    assert dumped["status"]["message"]["parts"][0]["text"] == "A temporary error occurred. Please retry."


@pytest.mark.asyncio
async def test_setup_failure_logs_traceback_with_task_context(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    class FailingTaskStore(A2ATaskStore):
        async def ensure_task_not_expired(self, task_id: str) -> None:
            raise FileNotFoundError(2, "No such file or directory")

    caplog.set_level(logging.ERROR, logger="iac_code.a2a.executor")

    store = FailingTaskStore(metrics=NoOpA2AMetrics())
    executor = IacCodeA2AExecutor(task_store=store, model="qwen3.6-plus")
    queue = FakeEventQueue()
    context = FakeRequestContext(metadata={"iac_code": {"cwd": str(tmp_path)}})

    await executor.execute(context, queue)

    dumped = dump(queue.events[-1])
    assert dumped["status"]["state"] == "TASK_STATE_FAILED"
    assert dumped["status"]["message"]["parts"][0]["text"] == "[Errno 2] No such file or directory"
    assert "A2A executor setup failed" in caplog.text
    assert "task_id=task-1" in caplog.text
    assert "context_id=ctx-1" in caplog.text
    assert "Traceback (most recent call last)" in caplog.text
    assert "FileNotFoundError: [Errno 2] No such file or directory" in caplog.text


@pytest.mark.asyncio
async def test_runtime_creation_failure_logs_traceback_with_task_context(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    def raise_missing_dependency(options):
        raise FileNotFoundError(2, "No such file or directory")

    caplog.set_level(logging.ERROR, logger="iac_code.a2a.executor")
    monkeypatch.setattr("iac_code.a2a.executor.create_agent_runtime", raise_missing_dependency)

    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    executor = IacCodeA2AExecutor(task_store=store, model="qwen3.6-plus")
    queue = FakeEventQueue()
    context = FakeRequestContext(metadata={"iac_code": {"cwd": str(tmp_path)}})

    await executor.execute(context, queue)

    dumped = dump(queue.events[-1])
    assert dumped["status"]["state"] == "TASK_STATE_FAILED"
    assert dumped["status"]["message"]["parts"][0]["text"].startswith("FileNotFoundError:")
    assert "A2A executor runtime setup failed" in caplog.text
    assert "task_id=task-1" in caplog.text
    assert "context_id=ctx-1" in caplog.text
    assert "Traceback (most recent call last)" in caplog.text
    assert "FileNotFoundError: [Errno 2] No such file or directory" in caplog.text


@pytest.mark.asyncio
async def test_streaming_failure_logs_traceback_with_task_context(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    class ExplodingLoop:
        async def run_streaming(self, prompt: str):
            raise FileNotFoundError(2, "No such file or directory")
            yield TextDeltaEvent(text="never")

    caplog.set_level(logging.ERROR, logger="iac_code.a2a.executor")
    runtime = FakeRuntime(agent_loop=ExplodingLoop(), session_id="session-1")
    monkeypatch.setattr("iac_code.a2a.executor.create_agent_runtime", lambda options: runtime)

    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    executor = IacCodeA2AExecutor(task_store=store, model="qwen3.6-plus")
    queue = FakeEventQueue()
    context = FakeRequestContext(metadata={"iac_code": {"cwd": str(tmp_path)}})

    await executor.execute(context, queue)

    dumped = dump(queue.events[-1])
    assert dumped["status"]["state"] == "TASK_STATE_FAILED"
    assert dumped["status"]["message"]["parts"][0]["text"].startswith("FileNotFoundError:")
    assert "A2A executor streaming failed" in caplog.text
    assert "task_id=task-1" in caplog.text
    assert "context_id=ctx-1" in caplog.text
    assert "Traceback (most recent call last)" in caplog.text
    assert "FileNotFoundError: [Errno 2] No such file or directory" in caplog.text


@pytest.mark.asyncio
async def test_unexpected_error_surfaces_type_and_message(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    class ExplodingLoop:
        async def run_streaming(self, prompt: str):
            raise RuntimeError("Authorization: Bearer sk-live at /Users/alice/.iac-code/settings.yml")
            yield TextDeltaEvent(text="never")

    runtime = FakeRuntime(agent_loop=ExplodingLoop(), session_id="session-1")
    monkeypatch.setattr("iac_code.a2a.executor.create_agent_runtime", lambda options: runtime)

    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    executor = IacCodeA2AExecutor(task_store=store, model="qwen3.6-plus")
    queue = FakeEventQueue()
    context = FakeRequestContext(metadata={"iac_code": {"cwd": str(tmp_path)}})

    await executor.execute(context, queue)

    dumped = dump(queue.events[-1])
    assert dumped["status"]["state"] == "TASK_STATE_FAILED"
    text = dumped["status"]["message"]["parts"][0]["text"]
    assert text.startswith("RuntimeError:")
    assert "sk-live" not in text
    assert "/Users/alice" not in text


@pytest.mark.asyncio
async def test_auth_error_still_uses_friendly_hint(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    class AuthFailingLoop:
        async def run_streaming(self, prompt: str):
            raise ValueError("please configure your provider via /auth")
            yield TextDeltaEvent(text="never")

    runtime = FakeRuntime(agent_loop=AuthFailingLoop(), session_id="session-1")
    monkeypatch.setattr("iac_code.a2a.executor.create_agent_runtime", lambda options: runtime)

    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    executor = IacCodeA2AExecutor(task_store=store, model="qwen3.6-plus")
    queue = FakeEventQueue()
    context = FakeRequestContext(metadata={"iac_code": {"cwd": str(tmp_path)}})

    await executor.execute(context, queue)

    dumped = dump(queue.events[-1])
    assert dumped["status"]["state"] == "TASK_STATE_FAILED"
    assert (
        dumped["status"]["message"]["parts"][0]["text"]
        == "Authentication required. Please configure your API credentials."
    )


@pytest.mark.asyncio
async def test_executor_flushes_telemetry_after_task(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    flush_calls: list[int] = []

    def fake_flush() -> None:
        flush_calls.append(1)

    monkeypatch.setattr("iac_code.services.telemetry.flush_telemetry", fake_flush)

    loop = FakeAgentLoop([TextDeltaEvent(text="hi")])
    runtime = FakeRuntime(agent_loop=loop, session_id="session-flush")
    monkeypatch.setattr("iac_code.a2a.executor.create_agent_runtime", lambda options: runtime)

    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    executor = IacCodeA2AExecutor(task_store=store, model="qwen3.6-plus")
    await executor.execute(
        FakeRequestContext(metadata={"iac_code": {"cwd": str(tmp_path)}}),
        FakeEventQueue(),
    )

    assert flush_calls == [1]


@pytest.mark.asyncio
async def test_executor_flushes_telemetry_even_on_failure(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    flush_calls: list[int] = []

    def fake_flush() -> None:
        flush_calls.append(1)

    monkeypatch.setattr("iac_code.services.telemetry.flush_telemetry", fake_flush)

    class ExplodingLoop:
        async def run_streaming(self, prompt):  # noqa: ARG002
            raise RuntimeError("boom")
            yield  # pragma: no cover - generator marker

    runtime = FakeRuntime(agent_loop=ExplodingLoop(), session_id="session-flush-fail")
    monkeypatch.setattr("iac_code.a2a.executor.create_agent_runtime", lambda options: runtime)

    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    executor = IacCodeA2AExecutor(task_store=store, model="qwen3.6-plus")
    await executor.execute(
        FakeRequestContext(metadata={"iac_code": {"cwd": str(tmp_path)}}),
        FakeEventQueue(),
    )

    assert flush_calls == [1]


@pytest.mark.asyncio
async def test_executor_swallows_flush_errors(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def boom() -> None:
        raise RuntimeError("flush exporter network down")

    monkeypatch.setattr("iac_code.services.telemetry.flush_telemetry", boom)

    loop = FakeAgentLoop([TextDeltaEvent(text="hi")])
    runtime = FakeRuntime(agent_loop=loop, session_id="session-flush-error")
    monkeypatch.setattr("iac_code.a2a.executor.create_agent_runtime", lambda options: runtime)

    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    executor = IacCodeA2AExecutor(task_store=store, model="qwen3.6-plus")

    # Flush failure must not break task completion.
    await executor.execute(
        FakeRequestContext(metadata={"iac_code": {"cwd": str(tmp_path)}}),
        FakeEventQueue(),
    )


class TestResolveUserId:
    def _make_executor(self) -> IacCodeA2AExecutor:
        store = A2ATaskStore(metrics=NoOpA2AMetrics())
        return IacCodeA2AExecutor(task_store=store, model="qwen3.6-plus")

    def test_extracts_user_id_from_iac_code_metadata(self) -> None:
        executor = self._make_executor()
        result = executor._resolve_user_id({"iac_code": {"user_id": "custom-user-123"}})
        assert result == "custom-user-123"

    def test_returns_none_when_no_metadata(self) -> None:
        executor = self._make_executor()
        assert executor._resolve_user_id(None) is None

    def test_returns_none_when_empty_metadata(self) -> None:
        executor = self._make_executor()
        assert executor._resolve_user_id({}) is None

    def test_returns_none_when_no_iac_code_key(self) -> None:
        executor = self._make_executor()
        assert executor._resolve_user_id({"other": "value"}) is None

    def test_returns_none_when_no_user_id_key(self) -> None:
        executor = self._make_executor()
        assert executor._resolve_user_id({"iac_code": {"cwd": "/tmp"}}) is None

    def test_returns_none_for_empty_string(self) -> None:
        executor = self._make_executor()
        assert executor._resolve_user_id({"iac_code": {"user_id": ""}}) is None

    def test_returns_none_for_whitespace_only(self) -> None:
        executor = self._make_executor()
        assert executor._resolve_user_id({"iac_code": {"user_id": "   "}}) is None

    def test_strips_whitespace(self) -> None:
        executor = self._make_executor()
        result = executor._resolve_user_id({"iac_code": {"user_id": "  user-abc  "}})
        assert result == "user-abc"

    def test_passes_through_non_prefixed_value(self) -> None:
        executor = self._make_executor()
        result = executor._resolve_user_id({"iac_code": {"user_id": "raw-value"}})
        assert result == "raw-value"

    def test_returns_none_for_non_string_value(self) -> None:
        executor = self._make_executor()
        assert executor._resolve_user_id({"iac_code": {"user_id": 12345}}) is None


class TestResolveAliyunCredential:
    def _make_executor(self) -> IacCodeA2AExecutor:
        store = A2ATaskStore(metrics=NoOpA2AMetrics())
        return IacCodeA2AExecutor(task_store=store, model="qwen3.6-plus")

    def test_extracts_aliyun_credential_from_iac_code_metadata(self) -> None:
        executor = self._make_executor()

        result = executor._resolve_aliyun_credential(
            {
                "iac_code": {
                    "alibaba_cloud_access_key_id": "client-id",
                    "alibaba_cloud_access_key_secret": "client-secret",
                    "alibaba_cloud_region_id": "cn-beijing",
                    "alibaba_cloud_security_token": "client-sts",
                }
            }
        )

        assert result is not None
        assert result.mode == "StsToken"
        assert result.access_key_id == "client-id"
        assert result.access_key_secret == "client-secret"
        assert result.region_id == "cn-beijing"
        assert result.sts_token == "client-sts"

    def test_uses_default_region_when_metadata_region_is_missing(self) -> None:
        executor = self._make_executor()

        result = executor._resolve_aliyun_credential(
            {
                "iac_code": {
                    "alibaba_cloud_access_key_id": "client-id",
                    "alibaba_cloud_access_key_secret": "client-secret",
                }
            }
        )

        assert result is not None
        assert result.region_id == "cn-hangzhou"
        assert result.mode == "AK"

    def test_returns_none_for_incomplete_aliyun_metadata(self) -> None:
        executor = self._make_executor()

        result = executor._resolve_aliyun_credential(
            {
                "iac_code": {
                    "alibaba_cloud_access_key_id": "client-id",
                    "alibaba_cloud_region_id": "cn-beijing",
                }
            }
        )

        assert result is None


@pytest.mark.asyncio
async def test_executor_applies_user_id_to_telemetry(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from iac_code.services.telemetry.identity import _user_id_override

    captured_user_ids: list[str | None] = []

    original_run_streaming = FakeAgentLoop.run_streaming

    async def capturing_run_streaming(self, prompt):
        captured_user_ids.append(_user_id_override.get())
        async for event in original_run_streaming(self, prompt):
            yield event

    monkeypatch.setattr(FakeAgentLoop, "run_streaming", capturing_run_streaming)

    loop = FakeAgentLoop([TextDeltaEvent(text="ok")])
    runtime = FakeRuntime(agent_loop=loop, session_id="sess-uid")
    monkeypatch.setattr("iac_code.a2a.executor.create_agent_runtime", lambda options: runtime)

    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    executor = IacCodeA2AExecutor(task_store=store, model="qwen3.6-plus")
    queue = FakeEventQueue()
    context = FakeRequestContext(metadata={"iac_code": {"cwd": str(tmp_path), "user_id": "client-user-xyz"}})

    await executor.execute(context, queue)

    assert captured_user_ids == ["client-user-xyz"]


@pytest.mark.asyncio
async def test_executor_no_user_id_override_when_not_specified(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from iac_code.services.telemetry.identity import _user_id_override

    captured_user_ids: list[str | None] = []

    original_run_streaming = FakeAgentLoop.run_streaming

    async def capturing_run_streaming(self, prompt):
        captured_user_ids.append(_user_id_override.get())
        async for event in original_run_streaming(self, prompt):
            yield event

    monkeypatch.setattr(FakeAgentLoop, "run_streaming", capturing_run_streaming)

    loop = FakeAgentLoop([TextDeltaEvent(text="ok")])
    runtime = FakeRuntime(agent_loop=loop, session_id="sess-no-uid")
    monkeypatch.setattr("iac_code.a2a.executor.create_agent_runtime", lambda options: runtime)

    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    executor = IacCodeA2AExecutor(task_store=store, model="qwen3.6-plus")
    queue = FakeEventQueue()
    context = FakeRequestContext(metadata={"iac_code": {"cwd": str(tmp_path)}})

    await executor.execute(context, queue)

    assert captured_user_ids == [None]


@pytest.mark.asyncio
async def test_executor_uses_metadata_iac_code_model_when_creating_runtime(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    seen_models: list[str] = []

    def factory(options):
        seen_models.append(options.model)
        return FakeRuntime(
            agent_loop=FakeAgentLoop([TextDeltaEvent(text="ok")]),
            session_id=options.session_id,
        )

    monkeypatch.setattr("iac_code.a2a.executor.create_agent_runtime", factory)

    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    executor = IacCodeA2AExecutor(task_store=store, model="server-default-model")
    context = FakeRequestContext(metadata={"iac_code": {"cwd": str(tmp_path), "iac_code_model": "metadata-model"}})

    await executor.execute(context, FakeEventQueue())

    assert seen_models == ["metadata-model"]


@pytest.mark.asyncio
async def test_executor_reconfigures_cached_runtime_iac_code_model_per_call(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class FakeProviderManager:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def reconfigure(self, model, credentials, provider_key_override=None, base_url_override=None):
            self.calls.append(model)

    provider_manager = FakeProviderManager()
    runtime = FakeRuntime(
        agent_loop=FakeAgentLoop([TextDeltaEvent(text="ok")]),
        session_id="session-1",
        provider_manager=provider_manager,
    )
    monkeypatch.setattr("iac_code.a2a.executor.create_agent_runtime", lambda options: runtime)

    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    executor = IacCodeA2AExecutor(task_store=store, model="server-default-model")

    await executor.execute(
        FakeRequestContext(
            context_id="ctx-1",
            metadata={"iac_code": {"cwd": str(tmp_path), "iac_code_model": "metadata-model"}},
        ),
        FakeEventQueue(),
    )
    await executor.execute(
        FakeRequestContext(context_id="ctx-1", task_id="task-2", metadata={"iac_code": {"cwd": str(tmp_path)}}),
        FakeEventQueue(),
    )

    assert provider_manager.calls == ["metadata-model", "server-default-model"]


@pytest.mark.asyncio
async def test_executor_applies_aliyun_metadata_to_task_credentials(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from iac_code.services.providers.aliyun import AliyunCredentials

    captured_access_key_ids: list[str | None] = []

    original_run_streaming = FakeAgentLoop.run_streaming

    async def capturing_run_streaming(self, prompt):
        cred = AliyunCredentials.load()
        captured_access_key_ids.append(cred.access_key_id if cred else None)
        async for event in original_run_streaming(self, prompt):
            yield event

    monkeypatch.setattr(FakeAgentLoop, "run_streaming", capturing_run_streaming)

    env = {
        "ALIBABA_CLOUD_ACCESS_KEY_ID": "env-id",
        "ALIBABA_CLOUD_ACCESS_KEY_SECRET": "env-secret",
        "ALIBABA_CLOUD_REGION_ID": "cn-shanghai",
    }
    loop = FakeAgentLoop([TextDeltaEvent(text="ok")])
    runtime = FakeRuntime(agent_loop=loop, session_id="sess-aliyun")
    monkeypatch.setattr("iac_code.a2a.executor.create_agent_runtime", lambda options: runtime)

    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    executor = IacCodeA2AExecutor(task_store=store, model="qwen3.6-plus")
    context = FakeRequestContext(
        metadata={
            "iac_code": {
                "cwd": str(tmp_path),
                "alibaba_cloud_access_key_id": "client-id",
                "alibaba_cloud_access_key_secret": "client-secret",
                "alibaba_cloud_region_id": "cn-beijing",
            }
        }
    )

    monkeypatch.setattr("iac_code.services.providers.aliyun.AliyunCredentials._load_from_iac_code_config", lambda: None)
    with monkeypatch.context() as m:
        for key, value in env.items():
            m.setenv(key, value)
        await executor.execute(context, FakeEventQueue())
        after = AliyunCredentials.load()

    assert captured_access_key_ids == ["client-id"]
    assert after is not None
    assert after.access_key_id == "env-id"


@pytest.mark.asyncio
async def test_executor_applies_aliyun_metadata_while_creating_runtime(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from iac_code.services.providers.aliyun import AliyunCredentials

    captured_access_key_ids: list[str | None] = []

    def factory(options):
        cred = AliyunCredentials.load()
        captured_access_key_ids.append(cred.access_key_id if cred else None)
        return FakeRuntime(
            agent_loop=FakeAgentLoop([TextDeltaEvent(text="ok")]),
            session_id=options.session_id,
        )

    monkeypatch.setattr("iac_code.a2a.executor.create_agent_runtime", factory)
    monkeypatch.setattr("iac_code.services.providers.aliyun.AliyunCredentials._load_from_iac_code_config", lambda: None)

    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    executor = IacCodeA2AExecutor(task_store=store, model="qwen3.6-plus")
    context = FakeRequestContext(
        metadata={
            "iac_code": {
                "cwd": str(tmp_path),
                "alibaba_cloud_access_key_id": "client-id",
                "alibaba_cloud_access_key_secret": "client-secret",
                "alibaba_cloud_region_id": "cn-beijing",
            }
        }
    )

    await executor.execute(context, FakeEventQueue())

    assert captured_access_key_ids == ["client-id"]


@pytest.mark.asyncio
async def test_executor_refreshes_cloud_tools_with_aliyun_metadata_for_reused_context(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    seen_access_key_ids: list[str | None] = []
    runtime = FakeRuntime(
        agent_loop=FakeAgentLoop([TextDeltaEvent(text="ok")]),
        session_id="session-1",
        tool_registry=object(),
    )

    def fake_register_cloud_tools(registry, credentials):
        assert registry is runtime.tool_registry
        credential = credentials.get_provider("aliyun")
        seen_access_key_ids.append(credential.access_key_id if credential else None)

    monkeypatch.setattr("iac_code.tools.cloud.registry.register_cloud_tools", fake_register_cloud_tools)
    monkeypatch.setattr("iac_code.a2a.executor.create_agent_runtime", lambda options: runtime)
    monkeypatch.setattr("iac_code.services.providers.aliyun.AliyunCredentials._load_from_iac_code_config", lambda: None)

    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    await store.get_or_create_context(
        context_id="ctx-1",
        cwd=str(tmp_path.resolve()),
        runtime_factory=lambda session_id: runtime,
    )
    executor = IacCodeA2AExecutor(task_store=store, model="qwen3.6-plus")
    context = FakeRequestContext(
        context_id="ctx-1",
        metadata={
            "iac_code": {
                "cwd": str(tmp_path),
                "alibaba_cloud_access_key_id": "client-id",
                "alibaba_cloud_access_key_secret": "client-secret",
                "alibaba_cloud_region_id": "cn-beijing",
            }
        },
    )

    await executor.execute(context, FakeEventQueue())

    assert seen_access_key_ids == ["client-id"]
