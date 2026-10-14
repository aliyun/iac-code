"""Real HTTP/SDK/AgentLoop coverage for prompts and replies at permission waits."""

import json
import multiprocessing
import os
import socket
import time
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import pytest

from iac_code.a2a.persistence import A2APersistenceStore
from iac_code.services.permission_wait import PermissionWaitCheckpointStore
from iac_code.services.session_storage import SessionStorage

# Each case starts/restarts real servers and sends several permission replies.
# Keep one overall watchdog without treating their combined IO as a 30s SLA.
pytestmark = pytest.mark.timeout(120)


def _serve(root: str, port: int) -> None:
    """Only the model and tool are fake; restart kills the actual server process."""
    import uvicorn

    from iac_code.a2a.app import create_app
    from iac_code.agent.agent_loop import AgentLoop
    from iac_code.tools.base import ToolResult
    from iac_code.types.stream_events import (
        MessageEndEvent,
        MessageStartEvent,
        TextDeltaEvent,
        ToolUseEndEvent,
        ToolUseStartEvent,
        Usage,
    )
    from tests.agent.test_agent_loop_permissions import WriteTool

    root_path = Path(root)
    os.environ["IAC_CODE_CONFIG_DIR"] = str(root_path / "config")
    os.environ["IAC_CODE_CONFIG_BACKUP_DIR"] = str(root_path / "backup")
    os.environ["IAC_CODE_MODE"] = "normal"
    os.environ["IACCODE_A2A_ALLOWED_CWDS"] = str(root_path)
    os.environ.pop("IAC_CODE_CONFIG_BACKUP_TMP_DIR", None)
    inputs = root_path / "model-inputs.jsonl"

    class RecordingTool(WriteTool):
        async def execute(self, *, tool_input, context):
            with (root_path / "executions.jsonl").open("a", encoding="utf-8") as output:
                output.write(json.dumps(tool_input) + "\n")
            return ToolResult.success("executed")

    class Provider:
        def get_model_name(self):
            return "test"

        async def stream(self, messages, system, tools=None):
            with inputs.open("a", encoding="utf-8") as output:
                output.write(json.dumps([asdict(message) for message in messages]) + "\n")
            yield MessageStartEvent(message_id="model-message")
            if messages[-1].content == "original":
                for index in range(3):
                    tool_id = f"tool-{index + 1}"
                    yield ToolUseStartEvent(tool_use_id=tool_id, name="write_test")
                    yield ToolUseEndEvent(tool_use_id=tool_id, name="write_test", input={"value": tool_id})
                yield MessageEndEvent(stop_reason="tool_use", usage=Usage())
            else:
                text = "handled new prompt" if isinstance(messages[-1].content, str) else "completed approved batch"
                yield TextDeltaEvent(text=text)
                yield MessageEndEvent(stop_reason="end_turn", usage=Usage())

    def runtime(options):
        from iac_code.tools.base import ToolRegistry

        registry = ToolRegistry()
        registry.register(RecordingTool())
        provider = Provider()
        return SimpleNamespace(
            session_id=options.session_id,
            provider_manager=provider,
            tool_registry=registry,
            agent_loop=AgentLoop(
                provider_manager=provider,
                system_prompt="test",
                tool_registry=registry,
                session_storage=SessionStorage(),
                session_id=options.session_id,
                cwd=options.cwd,
                resume_messages=options.resume_messages,
                max_turns=3,
            ),
        )

    with (
        patch("iac_code.a2a.executor.create_agent_runtime", runtime),
        patch("tiktoken.get_encoding", side_effect=RuntimeError("offline test")),
    ):
        app = create_app(
            host="127.0.0.1",
            port=port,
            token=None,
            model="test",
            persistence_dir=root_path / "a2a",
            permission_wait={"resident_timeout_seconds": 300},
        )
        uvicorn.run(app, host="127.0.0.1", port=port, log_level="error")


class Server:
    def __init__(self, root: Path):
        self.root = root
        self.process = None
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            self.port = sock.getsockname()[1]
        self.client = httpx.Client(base_url=f"http://127.0.0.1:{self.port}", trust_env=False, timeout=15)

    def start(self):
        self.process = multiprocessing.get_context("spawn").Process(target=_serve, args=(str(self.root), self.port))
        self.process.start()
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            assert self.process.is_alive(), "A2A server exited before startup"
            try:
                if self.client.get("/health").status_code == 200:
                    return
            except httpx.TransportError:
                pass
            time.sleep(0.02)
        raise AssertionError("A2A server startup timed out")

    def stop(self):
        if self.process is not None:
            self.process.kill()
            self.process.join(timeout=10)
            assert not self.process.is_alive()
            self.process.close()
            self.process = None

    def stream(self, text, *, task_id=None):
        message = {
            "messageId": f"message-{time.monotonic_ns()}",
            "contextId": "context-1",
            "role": "ROLE_USER",
            "parts": [{"text": text}],
            "metadata": {"iac_code": {"cwd": str(self.root / "workspace")}},
        }
        if task_id is not None:
            message["taskId"] = task_id
        return self.client.stream(
            "POST",
            "/",
            headers={"A2A-Version": "1.0"},
            json={"jsonrpc": "2.0", "id": "test", "method": "SendStreamingMessage", "params": {"message": message}},
        )

    @staticmethod
    def events(response):
        response.raise_for_status()
        return (
            json.loads(line[5:].strip() if line.startswith("data:") else line)
            for line in response.iter_lines()
            if line.startswith("data:") or line.startswith("{")
        )

    def send(self, text, *, task_id=None):
        with self.stream(text, task_id=task_id) as response:
            return list(self.events(response))


def _permission(events, *, required=True):
    for event in events:
        result = event.get("result", {})
        update = result.get("statusUpdate") or result.get("task") or result
        permission = update.get("metadata", {}).get("iac_code", {}).get("input", {})
        if permission.get("kind") == "permission":
            return permission
    if required:
        raise AssertionError(f"No permission event: {events}")
    return None


def _reply(permission):
    return "IAC_CODE_PERMISSION: " + json.dumps(
        {
            **{
                key: permission[key]
                for key in ("schemaVersion", "kind", "requestTaskId", "contextId", "inputId", "toolUseId")
            },
            "decision": "allow_once",
        }
    )


@pytest.mark.integration
@pytest.mark.parametrize("restart", [False, True], ids=["same-process", "after-restart"])
def test_new_prompt_cancels_unexecuted_permission_batch(tmp_path, monkeypatch, restart):
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    (tmp_path / "workspace").mkdir()
    server = Server(tmp_path)
    try:
        server.start()
        first = _permission(server.send("original"))
        second = _permission(server.send(_reply(first)))
        assert first["toolUseId"] == "tool-1"
        assert second["toolUseId"] == "tool-2"
        assert not (tmp_path / "executions.jsonl").exists()
        if restart:
            server.stop()
            server.start()

        events = server.send("new prompt")
        assert not any("error" in event for event in events), events
        assert "handled new prompt" in json.dumps(events)
        assert "Current execution is active in another process" not in json.dumps(events)
        assert not (tmp_path / "executions.jsonl").exists()
        inputs = [
            json.loads(line) for line in (tmp_path / "model-inputs.jsonl").read_text(encoding="utf-8").splitlines()
        ]
        assert len(inputs) == 2
        messages = inputs[-1]
        assert messages[-1]["role"] == "user" and messages[-1]["content"] == "new prompt"
        results = messages[-2]["content"]
        assert messages[-2]["role"] == "user"
        assert [result["tool_use_id"] for result in results] == ["tool-1", "tool-2", "tool-3"]
        assert all(result["is_error"] and "canceled before execution" in result["content"] for result in results)
        assert [block["tool_use_id"] for block in messages[-3]["content"] if block["type"] == "tool_use"] == [
            "tool-1",
            "tool-2",
            "tool-3",
        ]

        persistence = A2APersistenceStore(tmp_path / "a2a")
        assert persistence.load_task(second["requestTaskId"]).state == "canceled"
        ctx = persistence.load_context("context-1")
        checkpoints = PermissionWaitCheckpointStore(ctx.cwd, ctx.session_id)
        assert checkpoints.find_by_input_id(second["inputId"])["phase"] == "CANCELED"
        saved = SessionStorage().load(ctx.cwd, ctx.session_id)
        assert [message.get_text() for message in saved].count("new prompt") == 1
        assert (
            len(
                [
                    block
                    for message in saved
                    if isinstance(message.content, list)
                    for block in message.content
                    if block.type == "tool_result"
                ]
            )
            == 3
        )

        # Exercise delayed clicks both before and after another real restart.
        for cold in (False, True):
            if cold:
                server.stop()
                server.start()
            for permission in (first, second):
                server.send(_reply(permission), task_id=permission["requestTaskId"])
            assert not (tmp_path / "executions.jsonl").exists()
            assert len((tmp_path / "model-inputs.jsonl").read_text(encoding="utf-8").splitlines()) == 2
            assert checkpoints.find_by_input_id(second["inputId"])["phase"] == "CANCELED"

        # A later batch can reuse tool IDs; cancellation must target its live
        # boundary rather than a receipt from the previous batch.
        later = _permission(server.send("original"))
        assert later["requestTaskId"] != second["requestTaskId"]
        assert "handled new prompt" in json.dumps(server.send("new prompt"))
        assert checkpoints.find_by_input_id(later["inputId"])["phase"] == "CANCELED"
        assert not (tmp_path / "executions.jsonl").exists()
    finally:
        server.stop()
        server.client.close()


@pytest.mark.integration
@pytest.mark.parametrize("restart", [False, True], ids=["same-process", "after-restart"])
def test_permission_confirmation_continues_batch_and_accepts_next_prompt(tmp_path, monkeypatch, restart):
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    (tmp_path / "workspace").mkdir()
    server = Server(tmp_path)
    try:
        server.start()
        first = _permission(server.send("original"))
        second = _permission(server.send(_reply(first), task_id=first["requestTaskId"]))
        assert first["toolUseId"] == "tool-1"
        assert second["toolUseId"] == "tool-2"
        assert not (tmp_path / "executions.jsonl").exists()
        if restart:
            server.stop()
            server.start()

        with server.stream(_reply(second), task_id=second["requestTaskId"]) as response:
            stream = server.events(response)
            third = None
            for event in stream:
                third = _permission([event], required=False)
                if third is not None:
                    break
            assert third is not None
            assert third["toolUseId"] == "tool-3"
            assert first["requestTaskId"] == second["requestTaskId"] == third["requestTaskId"]
            assert not (tmp_path / "executions.jsonl").exists()
            assert len((tmp_path / "model-inputs.jsonl").read_text(encoding="utf-8").splitlines()) == 1

            # Cold recovery keeps this SSE stream open while the third
            # permission is answered through a separate HTTP request.
            events = server.send(_reply(third), task_id=third["requestTaskId"])
            events.extend(stream)
        assert not any("error" in event for event in events), events
        assert "completed approved batch" in json.dumps(events)
        assert any(
            event.get("result", {})
            .get("statusUpdate", {})
            .get("metadata", {})
            .get("iac_code", {})
            .get("assistantFinal", {})
            .get("complete")
            for event in events
        ), events
        executions_path = tmp_path / "executions.jsonl"
        executions = [json.loads(line) for line in executions_path.read_text(encoding="utf-8").splitlines()]
        assert sorted(item["value"] for item in executions) == ["tool-1", "tool-2", "tool-3"]
        inputs_path = tmp_path / "model-inputs.jsonl"
        inputs = [json.loads(line) for line in inputs_path.read_text(encoding="utf-8").splitlines()]
        assert len(inputs) == 2
        results = inputs[-1][-1]["content"]
        assert [result["tool_use_id"] for result in results] == ["tool-1", "tool-2", "tool-3"]
        assert all(not result["is_error"] and result["content"] == "executed" for result in results)

        persistence = A2APersistenceStore(tmp_path / "a2a")
        assert persistence.load_task(third["requestTaskId"]).state == "input-required"
        ctx = persistence.load_context("context-1")
        checkpoints = PermissionWaitCheckpointStore(ctx.cwd, ctx.session_id)
        for permission in (first, second):
            assert checkpoints.find_by_input_id(permission["inputId"])["phase"] == "RESOLVED"
        # Main currently leaves a cold recovery's successor in WAITING after
        # applying its decision. Check approval and replay safety separately.
        assert checkpoints.find_by_input_id(third["inputId"])["decision"]["status"] == "applied"

        # Check the next ordinary message before replaying any old permission
        # reply, so a stale click cannot explain a failure to release execution.
        next_events = server.send("new prompt")
        next_output = json.dumps(next_events)
        assert not any("error" in event for event in next_events), next_events
        assert "Current execution is active in another process" not in next_output, next_events
        assert "handled new prompt" in next_output, next_events
        assert len(executions_path.read_text(encoding="utf-8").splitlines()) == 3
        assert len(inputs_path.read_text(encoding="utf-8").splitlines()) == 3

        # Repeated confirmations must acknowledge/reject the old receipt,
        # without executing the already completed batch a second time.
        for permission in (second, third):
            server.send(_reply(permission), task_id=permission["requestTaskId"])
        assert len(executions_path.read_text(encoding="utf-8").splitlines()) == 3
        assert len(inputs_path.read_text(encoding="utf-8").splitlines()) == 3
    finally:
        server.stop()
        server.client.close()
