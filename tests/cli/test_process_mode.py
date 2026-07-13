from __future__ import annotations

import asyncio
import io
import json
import os
import queue
from collections.abc import AsyncIterator
from types import SimpleNamespace

import pytest

from iac_code.cli.process_mode import (
    PipelineProcessContextSnapshot,
    PipelineProcessContextStore,
    PipelineProcessCreateRequest,
    PipelineProcessRuntimeController,
    ProcessModeOptions,
    ProcessModeRunner,
    ProcessRuntimeController,
    ProcessSessionLock,
)
from iac_code.cli.process_protocol import SDKControlRequest, SDKUserMessage
from iac_code.pipeline.engine.events import PipelineEvent, PipelineEventType
from iac_code.types.stream_events import MessageEndEvent, TextDeltaEvent, Usage


class FakeRuntimeController:
    def __init__(self, *, block_turn: bool = False) -> None:
        self.model = "model-a"
        self.session_id = "generated-session"
        self.turns: list[tuple[str, str]] = []
        self.closed = False
        self.block_turn = block_turn
        self.turn_started = asyncio.Event()
        self.release_turn = asyncio.Event()

    async def initialize(self, frame) -> dict:
        if frame.payload.get("model"):
            self.model = frame.payload["model"]
        return {"protocol_version": "1.0", "capabilities": ["user", "interrupt", "set_model", "end_session", "close"]}

    def set_model(self, model: str) -> None:
        self.model = model

    async def run_turn(self, frame) -> AsyncIterator[object]:
        self.turns.append((self.model, frame.text))
        self.turn_started.set()
        if self.block_turn:
            await self.release_turn.wait()
        yield TextDeltaEvent(text=f"echo:{frame.text}")
        yield MessageEndEvent(stop_reason="end_turn", usage=Usage(input_tokens=1, output_tokens=1))

    async def aclose(self) -> None:
        self.closed = True


class FakePipelineRuntime:
    provider_manager = object()
    tool_registry = object()
    command_registry = object()

    async def aclose(self) -> None:
        return None


class FakePipeline:
    def __init__(self) -> None:
        self.sidecar_status: str | None = None
        self.inputs: list[tuple[str, str]] = []

    async def run(self, user_input):
        self.inputs.append(("run", user_input.display_text))
        yield PipelineEvent(
            type=PipelineEventType.PIPELINE_STARTED,
            step_id=None,
            timestamp=1.0,
            data={"input": user_input.display_text},
        )
        self.sidecar_status = "waiting_input"
        yield PipelineEvent(
            type=PipelineEventType.USER_INPUT_REQUIRED,
            step_id=None,
            timestamp=2.0,
            data={"prompt": "continue?"},
        )

    async def resume(self, user_input):
        self.inputs.append(("resume", user_input.display_text))
        self.sidecar_status = "running"
        yield PipelineEvent(
            type=PipelineEventType.USER_INPUT_RECEIVED,
            step_id=None,
            timestamp=3.0,
            data={"input": user_input.display_text},
        )
        self.sidecar_status = "completed"
        yield PipelineEvent(
            type=PipelineEventType.PIPELINE_COMPLETED,
            step_id=None,
            timestamp=4.0,
            data={},
        )


def test_default_pipeline_factory_passes_mcp_runtime_state(monkeypatch, tmp_path) -> None:
    captured: dict[str, object] = {}
    pipeline = object()
    mcp_manager = object()
    warnings = [object()]
    session_storage = object()
    permission_context = object()

    def fake_create_pipeline(name, **kwargs):
        captured["name"] = name
        captured.update(kwargs)
        return pipeline

    monkeypatch.setattr("iac_code.pipeline.create_pipeline", fake_create_pipeline)
    runtime = SimpleNamespace(
        provider_manager=object(),
        tool_registry=object(),
        command_registry=SimpleNamespace(get_model_invocable_skills=lambda: ["skill"]),
        agent_loop=SimpleNamespace(_session_storage=session_storage, permission_context=permission_context),
        mcp_manager=mcp_manager,
        mcp_config_warnings=warnings,
    )
    controller = PipelineProcessRuntimeController(
        ProcessModeOptions(model="model-a", cwd=str(tmp_path), run_mode="pipeline")
    )

    result = controller._default_pipeline_factory(
        PipelineProcessCreateRequest(
            context_id="ctx-1",
            task_id="task-1",
            iac_code_session_id="session-1",
            cwd=str(tmp_path),
            model="model-a",
            resume_from_sidecar=True,
            agent_runtime=runtime,
        )
    )

    assert result is pipeline
    assert captured["session_storage"] is session_storage
    assert captured["permission_context_getter"]() is permission_context
    assert captured["auto_trigger_skills"] == ["skill"]
    assert captured["surface"] == "process"
    assert captured["mcp_manager"] is mcp_manager
    assert captured["mcp_config_warnings"] is warnings


def _load_output(stream: io.StringIO) -> list[dict]:
    return [json.loads(line) for line in stream.getvalue().splitlines() if line.strip()]


class _BlockingInputStream:
    def __init__(self) -> None:
        self._lines: queue.Queue[str] = queue.Queue()

    def push_json(self, frame: dict) -> None:
        self._lines.put(json.dumps(frame) + "\n")

    def close(self) -> None:
        self._lines.put("")

    def readline(self) -> str:
        return self._lines.get()


async def _wait_for_output(stream: io.StringIO, predicate, *, timeout: float = 2.0) -> list[dict]:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        lines = _load_output(stream)
        if any(predicate(line) for line in lines):
            return lines
        await asyncio.sleep(0.01)
    lines = _load_output(stream)
    raise AssertionError(f"timed out waiting for process output; got {lines!r}")


@pytest.mark.asyncio
async def test_pipeline_process_runner_starts_waits_and_resumes(tmp_path) -> None:
    pipeline = FakePipeline()
    controller = PipelineProcessRuntimeController(
        ProcessModeOptions(model="model-a", cwd=str(tmp_path), run_mode="pipeline"),
        agent_runtime_factory=lambda request: FakePipelineRuntime(),
        pipeline_factory=lambda request: pipeline,
    )
    metadata = {
        "iac_code": {
            "contextId": "ctx-1",
            "taskId": "task-1",
            "iacCodeSessionId": "iac-session-1",
        }
    }
    stdin = _BlockingInputStream()
    stdout = io.StringIO()

    runner_task = asyncio.create_task(
        ProcessModeRunner(
            ProcessModeOptions(model="model-a", cwd=str(tmp_path), run_mode="pipeline"),
            input_stream=stdin,
            output_stream=stdout,
            runtime_controller=controller,
        ).run()
    )

    stdin.push_json({"type": "control_request", "request_id": "req-init", "request": {"subtype": "initialize"}})
    await _wait_for_output(stdout, lambda line: line.get("type") == "control_response")

    stdin.push_json(
        {
            "type": "user",
            "request_id": "req-start",
            "session_id": "sdk-session-1",
            "metadata": metadata,
            "message": {"role": "user", "content": "start"},
        }
    )
    await _wait_for_output(
        stdout,
        lambda line: line.get("type") == "result" and line.get("request_id") == "req-start",
    )

    stdin.push_json(
        {
            "type": "user",
            "request_id": "req-resume",
            "session_id": "sdk-session-1",
            "metadata": metadata,
            "message": {"role": "user", "content": "continue"},
        }
    )
    await _wait_for_output(
        stdout,
        lambda line: line.get("type") == "result" and line.get("request_id") == "req-resume",
    )
    stdin.close()

    exit_code = await asyncio.wait_for(runner_task, timeout=2)

    assert exit_code == 0
    assert pipeline.inputs == [("run", "start"), ("resume", "continue")]
    lines = _load_output(stdout)
    init = lines[0]
    assert init["response"]["subtype"] == "success"
    assert "pipeline_resume" in init["response"]["response"]["capabilities"]

    stream_events = [line["event"] for line in lines if line["type"] == "stream_event"]
    assert [event["eventType"] for event in stream_events] == [
        "pipeline_started",
        "input_required",
        "input_received",
        "pipeline_completed",
    ]
    assert all(event["type"] == "pipeline_event" for event in stream_events)
    assert all(event["contextId"] == "ctx-1" for event in stream_events)
    assert all(event["taskId"] == "task-1" for event in stream_events)

    results = [line for line in lines if line["type"] == "result"]
    assert results[0]["request_id"] == "req-start"
    assert results[0]["stop_reason"] == "input_required"
    assert results[0]["pipeline"]["status"] == "input_required"
    assert results[0]["pipeline"]["sidecarStatus"] == "waiting_input"
    assert results[1]["request_id"] == "req-resume"
    assert results[1]["stop_reason"] == "end_turn"
    assert results[1]["pipeline"]["status"] == "completed"
    assert results[1]["pipeline"]["sidecarStatus"] == "completed"


@pytest.mark.asyncio
async def test_pipeline_process_runner_returns_recoverable_task_when_task_id_is_missing(tmp_path) -> None:
    context_store = PipelineProcessContextStore(tmp_path / "contexts")
    context_store.save(
        PipelineProcessContextSnapshot(
            context_id="ctx-1",
            task_id="task-1",
            iac_code_session_id="iac-session-1",
            cwd=str(tmp_path),
            sidecar_status="waiting_input",
            active_task_id="task-1",
        )
    )
    controller = PipelineProcessRuntimeController(
        ProcessModeOptions(model="model-a", cwd=str(tmp_path), run_mode="pipeline"),
        agent_runtime_factory=lambda request: FakePipelineRuntime(),
        pipeline_factory=lambda request: FakePipeline(),
        context_store=context_store,
    )
    stdin = io.StringIO(
        "\n".join(
            [
                json.dumps({"type": "control_request", "request_id": "req-init", "request": {"subtype": "initialize"}}),
                json.dumps(
                    {
                        "type": "user",
                        "request_id": "req-resume",
                        "metadata": {"iac_code": {"contextId": "ctx-1"}},
                        "message": {"role": "user", "content": "continue"},
                    }
                ),
            ]
        )
        + "\n"
    )
    stdout = io.StringIO()

    exit_code = await ProcessModeRunner(
        ProcessModeOptions(model="model-a", cwd=str(tmp_path), run_mode="pipeline"),
        input_stream=stdin,
        output_stream=stdout,
        runtime_controller=controller,
    ).run()

    assert exit_code == 0
    lines = _load_output(stdout)
    error = next(line for line in lines if line["type"] == "error")
    assert error["request_id"] == "req-resume"
    assert error["error"]["code"] == "pipeline_task_required"
    assert error["error"]["retryable"] is True
    assert error["error"]["data"] == {
        "contextId": "ctx-1",
        "recoverableTaskId": "task-1",
        "sidecarStatus": "waiting_input",
    }


@pytest.mark.asyncio
async def test_process_runner_handles_initialize_user_and_eof(tmp_path) -> None:
    controller = FakeRuntimeController()
    stdin = io.StringIO(
        "\n".join(
            [
                json.dumps(
                    {
                        "type": "control_request",
                        "request_id": "req-init",
                        "request": {"subtype": "initialize", "cwd": str(tmp_path), "model": "model-a"},
                    }
                ),
                json.dumps({"type": "user", "session_id": "session-1", "message": {"role": "user", "content": "hi"}}),
            ]
        )
        + "\n"
    )
    stdout = io.StringIO()

    exit_code = await ProcessModeRunner(
        ProcessModeOptions(model="model-a", cwd=str(tmp_path)),
        input_stream=stdin,
        output_stream=stdout,
        runtime_controller=controller,
    ).run()

    assert exit_code == 0
    assert controller.turns == [("model-a", "hi")]
    lines = _load_output(stdout)
    assert lines[0]["type"] == "control_response"
    assert lines[0]["response"]["subtype"] == "success"
    assert lines[0]["response"]["request_id"] == "req-init"
    assert lines[1]["type"] == "stream_event"
    assert lines[1]["session_id"] == "session-1"
    assert lines[1]["event"] == {"type": "text_delta", "text": "echo:hi"}
    assert lines[2]["type"] == "stream_event"
    assert lines[2]["event"]["type"] == "message_end"
    assert lines[3]["type"] == "result"
    assert lines[3]["subtype"] == "success"
    assert lines[3]["is_error"] is False
    assert lines[3]["result"] == "echo:hi"
    assert lines[3]["stop_reason"] == "end_turn"
    assert lines[3]["usage"]["input_tokens"] == 1
    assert lines[3]["duration_api_ms"] == 0
    assert lines[3]["num_turns"] == 1
    assert lines[3]["modelUsage"] == {}


@pytest.mark.asyncio
async def test_process_runner_rejects_user_before_initialize(tmp_path) -> None:
    controller = FakeRuntimeController()
    stdin = io.StringIO(json.dumps({"type": "user", "message": {"role": "user", "content": "hi"}}) + "\n")
    stdout = io.StringIO()

    exit_code = await ProcessModeRunner(
        ProcessModeOptions(model="model-a", cwd=str(tmp_path)),
        input_stream=stdin,
        output_stream=stdout,
        runtime_controller=controller,
    ).run()

    assert exit_code == 1
    lines = _load_output(stdout)
    assert lines == [
        {
            "type": "error",
            "request_id": None,
            "error": {
                "code": "not_initialized",
                "message": "initialize is required before user messages",
                "retryable": False,
            },
        }
    ]


@pytest.mark.asyncio
async def test_process_runner_reports_turn_active_and_close_cancels_turn(tmp_path) -> None:
    controller = FakeRuntimeController(block_turn=True)
    stdin = io.StringIO(
        "\n".join(
            [
                json.dumps({"type": "control_request", "request_id": "req-init", "request": {"subtype": "initialize"}}),
                json.dumps(
                    {"type": "user", "session_id": "session-1", "message": {"role": "user", "content": "first"}}
                ),
                json.dumps(
                    {"type": "user", "session_id": "session-1", "message": {"role": "user", "content": "second"}}
                ),
                json.dumps(
                    {"type": "control_request", "request_id": "req-close", "request": {"subtype": "end_session"}}
                ),
            ]
        )
        + "\n"
    )
    stdout = io.StringIO()

    exit_code = await ProcessModeRunner(
        ProcessModeOptions(model="model-a", cwd=str(tmp_path)),
        input_stream=stdin,
        output_stream=stdout,
        runtime_controller=controller,
    ).run()

    assert exit_code == 0
    lines = _load_output(stdout)
    assert any(line["type"] == "error" and line["error"]["code"] == "turn_active" for line in lines)
    canceled = [line for line in lines if line["type"] == "result" and line["subtype"] == "error_during_execution"]
    assert len(canceled) == 1
    assert canceled[0]["is_error"] is True
    assert canceled[0]["stop_reason"] == "cancelled"
    assert any(
        line["type"] == "control_response"
        and line["response"]["request_id"] == "req-close"
        and line["response"]["subtype"] == "success"
        for line in lines
    )


@pytest.mark.asyncio
async def test_process_runner_interrupt_cancels_active_turn(tmp_path) -> None:
    controller = FakeRuntimeController(block_turn=True)
    stdin = io.StringIO(
        "\n".join(
            [
                json.dumps({"type": "control_request", "request_id": "req-init", "request": {"subtype": "initialize"}}),
                json.dumps(
                    {"type": "user", "session_id": "session-1", "message": {"role": "user", "content": "first"}}
                ),
                json.dumps(
                    {
                        "type": "control_request",
                        "request_id": "req-interrupt",
                        "request": {"subtype": "interrupt"},
                    }
                ),
            ]
        )
        + "\n"
    )
    stdout = io.StringIO()

    exit_code = await ProcessModeRunner(
        ProcessModeOptions(model="model-a", cwd=str(tmp_path)),
        input_stream=stdin,
        output_stream=stdout,
        runtime_controller=controller,
    ).run()

    assert exit_code == 0
    lines = _load_output(stdout)
    assert any(
        line["type"] == "control_response"
        and line["response"]["request_id"] == "req-interrupt"
        and line["response"]["subtype"] == "success"
        for line in lines
    )
    assert any(line["type"] == "result" and line["subtype"] == "error_during_execution" for line in lines)


@pytest.mark.asyncio
async def test_process_runner_set_model_affects_next_turn(tmp_path) -> None:
    controller = FakeRuntimeController()
    stdin = io.StringIO(
        "\n".join(
            [
                json.dumps({"type": "control_request", "request_id": "req-init", "request": {"subtype": "initialize"}}),
                json.dumps(
                    {
                        "type": "control_request",
                        "request_id": "req-model",
                        "request": {"subtype": "set_model", "model": "model-b"},
                    }
                ),
                json.dumps(
                    {"type": "user", "session_id": "session-1", "message": {"role": "user", "content": "after"}}
                ),
            ]
        )
        + "\n"
    )
    stdout = io.StringIO()

    exit_code = await ProcessModeRunner(
        ProcessModeOptions(model="model-a", cwd=str(tmp_path)),
        input_stream=stdin,
        output_stream=stdout,
        runtime_controller=controller,
    ).run()

    assert exit_code == 0
    assert controller.turns == [("model-b", "after")]
    lines = _load_output(stdout)
    assert any(
        line["type"] == "control_response"
        and line["response"]["request_id"] == "req-model"
        and line["response"]["subtype"] == "success"
        for line in lines
    )


@pytest.mark.asyncio
async def test_runtime_controller_uses_initialize_cwd_as_default(monkeypatch, tmp_path) -> None:
    init_cwd = tmp_path / "init"
    init_cwd.mkdir()
    captured_cwds: list[str] = []

    class FakeAgentLoop:
        async def run_streaming(self, prompt: str) -> AsyncIterator[object]:
            yield TextDeltaEvent(text=prompt)
            yield MessageEndEvent(stop_reason="end_turn", usage=Usage())

    class FakeRuntime:
        session_id = "generated-session"
        agent_loop = FakeAgentLoop()

    def fake_create_agent_runtime(options) -> FakeRuntime:
        captured_cwds.append(options.cwd)
        return FakeRuntime()

    monkeypatch.setattr("iac_code.services.agent_factory.create_agent_runtime", fake_create_agent_runtime)
    controller = ProcessRuntimeController(ProcessModeOptions(model="model-a", cwd=str(tmp_path)))

    response = await controller.initialize(
        SDKControlRequest(
            request_id="req-init",
            subtype="initialize",
            payload={"subtype": "initialize", "cwd": str(init_cwd)},
        )
    )
    events = [
        event async for event in controller.run_turn(SDKUserMessage(request_id=None, session_id=None, text="hello"))
    ]

    assert response["cwd"] == str(init_cwd)
    assert captured_cwds == [str(init_cwd)]
    assert isinstance(events[0], TextDeltaEvent)


@pytest.mark.asyncio
async def test_runtime_controller_user_cwd_overrides_initialize_cwd(monkeypatch, tmp_path) -> None:
    init_cwd = tmp_path / "init"
    message_cwd = tmp_path / "message"
    init_cwd.mkdir()
    message_cwd.mkdir()
    captured_cwds: list[str] = []

    class FakeAgentLoop:
        async def run_streaming(self, prompt: str) -> AsyncIterator[object]:
            yield TextDeltaEvent(text=prompt)
            yield MessageEndEvent(stop_reason="end_turn", usage=Usage())

    class FakeRuntime:
        session_id = "generated-session"
        agent_loop = FakeAgentLoop()

    def fake_create_agent_runtime(options) -> FakeRuntime:
        captured_cwds.append(options.cwd)
        return FakeRuntime()

    monkeypatch.setattr("iac_code.services.agent_factory.create_agent_runtime", fake_create_agent_runtime)
    controller = ProcessRuntimeController(ProcessModeOptions(model="model-a", cwd=str(tmp_path)))

    await controller.initialize(
        SDKControlRequest(
            request_id="req-init",
            subtype="initialize",
            payload={"subtype": "initialize", "cwd": str(init_cwd)},
        )
    )
    _ = [
        event
        async for event in controller.run_turn(
            SDKUserMessage(request_id=None, session_id=None, text="hello", cwd=str(message_cwd))
        )
    ]

    assert captured_cwds == [str(message_cwd)]


def test_process_session_lock_rejects_concurrent_holder(tmp_path) -> None:
    first = ProcessSessionLock(cwd=str(tmp_path), session_id="session-1")
    second = ProcessSessionLock(cwd=str(tmp_path), session_id="session-1")

    assert first.acquire(blocking=False) is True
    try:
        assert second.acquire(blocking=False) is False
    finally:
        first.release()

    assert second.acquire(blocking=False) is True
    second.release()


@pytest.mark.asyncio
async def test_process_runner_applies_environment_updates(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("IAC_CODE_PROCESS_TEST_ENV", raising=False)
    controller = FakeRuntimeController()
    stdin = io.StringIO(
        "\n".join(
            [
                json.dumps({"type": "update_environment_variables", "variables": {"IAC_CODE_PROCESS_TEST_ENV": "1"}}),
                json.dumps({"type": "control_request", "request_id": "req-init", "request": {"subtype": "initialize"}}),
            ]
        )
        + "\n"
    )
    stdout = io.StringIO()

    exit_code = await ProcessModeRunner(
        ProcessModeOptions(model="model-a", cwd=str(tmp_path)),
        input_stream=stdin,
        output_stream=stdout,
        runtime_controller=controller,
    ).run()

    assert exit_code == 0
    assert os.environ["IAC_CODE_PROCESS_TEST_ENV"] == "1"
    lines = _load_output(stdout)
    assert len(lines) == 1
    assert lines[0]["type"] == "control_response"


@pytest.mark.asyncio
async def test_process_runner_ignores_control_response_frames(tmp_path) -> None:
    controller = FakeRuntimeController()
    stdin = io.StringIO(
        "\n".join(
            [
                json.dumps(
                    {
                        "type": "control_response",
                        "response": {"subtype": "success", "request_id": "permission-1", "response": {"ok": True}},
                    }
                ),
                json.dumps({"type": "control_request", "request_id": "req-init", "request": {"subtype": "initialize"}}),
            ]
        )
        + "\n"
    )
    stdout = io.StringIO()

    exit_code = await ProcessModeRunner(
        ProcessModeOptions(model="model-a", cwd=str(tmp_path)),
        input_stream=stdin,
        output_stream=stdout,
        runtime_controller=controller,
    ).run()

    assert exit_code == 0
    lines = _load_output(stdout)
    assert len(lines) == 1
    assert lines[0]["response"]["request_id"] == "req-init"
