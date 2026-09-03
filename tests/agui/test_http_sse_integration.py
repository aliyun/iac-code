from __future__ import annotations

import asyncio
import json
import socket
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterator

import httpx
import pytest
import uvicorn

from iac_code.a2a.app import create_app as create_a2a_app
from iac_code.a2a.executor import IacCodeA2AExecutor
from iac_code.agui.app import create_app as create_agui_app
from iac_code.pipeline.engine.events import PipelineEvent, PipelineEventType
from iac_code.types.stream_events import PermissionRequestEvent, SubPipelineStreamEvent, TextDeltaEvent


class _ToolRegistry:
    def register(self, tool) -> None:
        del tool

    def unregister(self, tool_name: str) -> None:
        del tool_name


class _ParallelPermissionPipeline:
    pipeline_name = "selling"
    sidecar_status = None
    handoff_enabled = False

    def __init__(self, session_dir: Path) -> None:
        self.session = SimpleNamespace(session_dir=session_dir)
        self.other_candidate_continued = threading.Event()
        self.permission_resolved = threading.Event()
        self.permission_future: asyncio.Future[bool] | None = None

    async def run(self, prompt: str):
        del prompt
        yield PipelineEvent(
            type=PipelineEventType.PIPELINE_STARTED,
            step_id=None,
            timestamp=time.time(),
            data={"total_steps": 2, "step_names": ["candidate-a", "candidate-b"]},
        )
        self.permission_future = asyncio.get_running_loop().create_future()
        yield SubPipelineStreamEvent(
            sub_pipeline_id="candidate-a",
            candidate_index=0,
            inner=PermissionRequestEvent(
                tool_name="bash",
                tool_input={"cmd": "pwd"},
                tool_use_id="tool-a",
                response_future=self.permission_future,
            ),
        )
        yield SubPipelineStreamEvent(
            sub_pipeline_id="candidate-b",
            candidate_index=1,
            inner=TextDeltaEvent(text="candidate B continued while A waited"),
        )
        self.other_candidate_continued.set()
        approved = await self.permission_future
        self.permission_resolved.set()
        yield SubPipelineStreamEvent(
            sub_pipeline_id="candidate-a",
            candidate_index=0,
            inner=TextDeltaEvent(text=f"candidate A resumed: {approved}"),
        )
        yield PipelineEvent(
            type=PipelineEventType.PIPELINE_COMPLETED,
            step_id=None,
            timestamp=time.time(),
            data={"total_steps": 2},
        )

    def clear_sidecar(self) -> None:
        self.sidecar_status = None

    def should_switch_to_normal(self, data: dict[str, Any]) -> bool:
        del data
        return False


@contextmanager
def _serve(app: Any) -> Iterator[str]:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error", access_log=False, lifespan="on")
    )
    thread = threading.Thread(target=server.run, name=f"test-uvicorn-{port}", daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and thread.is_alive() and time.monotonic() < deadline:
        time.sleep(0.01)
    if not server.started:
        server.should_exit = True
        thread.join(timeout=5)
        raise RuntimeError("test Uvicorn server did not start")
    try:
        yield f"http://127.0.0.1:{port}/"
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        if thread.is_alive():
            server.force_exit = True
            thread.join(timeout=5)


def _read_agui(url: str, payload: dict[str, Any]) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    with httpx.Client(timeout=20) as client:
        with client.stream("POST", url, json=payload, headers={"Accept": "text/event-stream"}) as response:
            response.raise_for_status()
            for line in response.iter_lines():
                if line.startswith("data: "):
                    events.append(json.loads(line.removeprefix("data: ")))
    return events


def _run_payload(workspace: Path, *, run_id: str, resume: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {
        "threadId": "thread-http-sse",
        "runId": run_id,
        "state": {},
        "messages": [] if resume else [{"id": "message-1", "role": "user", "content": "run pipeline"}],
        "tools": [],
        "context": [],
        "forwardedProps": {
            "iacCode": {
                "schemaVersion": 1,
                "rosInvocationId": "ros-invocation-http-sse",
                "cwd": str(workspace),
            }
        },
        **({"resume": resume} if resume is not None else {}),
    }


def _get_task(a2a_url: str, task_id: str) -> dict[str, Any]:
    response = httpx.post(
        a2a_url,
        headers={"A2A-Version": "1.0"},
        json={"jsonrpc": "2.0", "id": "get-task", "method": "GetTask", "params": {"id": task_id}},
        timeout=10,
    )
    response.raise_for_status()
    return response.json()["result"]


def test_real_http_sse_sub_pipeline_interrupt_survives_agui_restart_and_snapshot_catches_up(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state_dir = tmp_path / "migratable-agui-state"
    monkeypatch.setenv("IAC_CODE_MODE", "pipeline")
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("IACCODE_A2A_ALLOWED_CWDS", str(workspace))
    monkeypatch.setenv("IAC_CODE_AGUI_ALLOWED_CWDS", str(workspace))
    monkeypatch.setattr("iac_code.a2a.input_required.emit_permission_boundary_audit", lambda *_a, **_k: True)
    monkeypatch.setattr("iac_code.a2a.pipeline_stream.emit_permission_boundary_audit", lambda *_a, **_k: True)

    pipeline = _ParallelPermissionPipeline(tmp_path / "pipeline-sidecar")
    runtime = SimpleNamespace(provider_manager=object(), tool_registry=_ToolRegistry())
    monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_agent_runtime", lambda _options: runtime)

    def create_test_pipeline(*_args, **kwargs):
        pipeline.session.session_dir = (
            kwargs["session_storage"].session_dir(
                kwargs["cwd"],
                kwargs["session_id"],
            )
            / "pipeline"
        )
        return pipeline

    monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_pipeline", create_test_pipeline)
    cancel_calls: list[str | None] = []
    original_cancel = IacCodeA2AExecutor.cancel

    async def record_cancel(self, context, event_queue):
        cancel_calls.append(context.task_id)
        await original_cancel(self, context, event_queue)

    monkeypatch.setattr(IacCodeA2AExecutor, "cancel", record_cancel)
    a2a_app = create_a2a_app(
        host="127.0.0.1",
        port=0,
        token=None,
        model="deterministic-test-model",
        persistence_dir=tmp_path / "a2a-state",
    )

    with _serve(a2a_app) as a2a_url:
        first_agui = create_agui_app(a2a_url=a2a_url, state_dir=state_dir)
        with _serve(first_agui) as first_agui_url:
            first_events = _read_agui(first_agui_url, _run_payload(workspace, run_id="run-1"))

        first_terminal = first_events[-1]
        assert first_terminal["type"] == "RUN_FINISHED"
        assert first_terminal["outcome"]["type"] == "interrupt"
        assert len(first_terminal["outcome"]["interrupts"]) == 1
        interrupt_id = first_terminal["outcome"]["interrupts"][0]["id"]
        session = next(event["value"] for event in first_events if event.get("name") == "iac-code.session.v1")
        task_id = session["taskId"]

        assert pipeline.other_candidate_continued.wait(timeout=5)
        task_during_disconnect = _get_task(a2a_url, task_id)
        assert task_during_disconnect["status"]["state"] not in {
            "TASK_STATE_FAILED",
            "TASK_STATE_CANCELED",
        }
        assert cancel_calls == []
        persisted = json.loads(
            (state_dir / "threads" / "thread-http-sse.json").read_text(encoding="utf-8")
        )
        assert persisted["execution"]["pending"][interrupt_id]["sideband"] is True

        second_agui = create_agui_app(a2a_url=a2a_url, state_dir=state_dir)
        with _serve(second_agui) as second_agui_url:
            second_events = _read_agui(
                second_agui_url,
                _run_payload(
                    workspace,
                    run_id="run-2",
                    resume=[
                        {
                            "interruptId": interrupt_id,
                            "status": "resolved",
                            "payload": {"decision": "allow_once"},
                        }
                    ],
                ),
            )

        assert pipeline.permission_resolved.wait(timeout=5)
        assert pipeline.permission_future is not None and pipeline.permission_future.result() is True
        assert any(event["type"] == "ACTIVITY_SNAPSHOT" for event in second_events)
        assert any(
            event["type"] == "TEXT_MESSAGE_CONTENT" and "candidate B continued while A waited" in event["delta"]
            for event in second_events
        )
        assert "candidate B continued while A waited" in json.dumps(second_events, ensure_ascii=False)
        assert second_events[-1]["type"] == "RUN_FINISHED"
        assert second_events[-1]["outcome"] == {"type": "success"}
        assert _get_task(a2a_url, task_id)["status"]["state"] == "TASK_STATE_COMPLETED"
        assert cancel_calls == []
        assert (
            sum(event.get("outcome", {}).get("type") == "interrupt" for event in [*first_events, *second_events]) == 1
        )
