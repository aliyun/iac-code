"""Real HTTP/SDK/AgentLoop coverage for prompts and replies at permission waits."""

import asyncio
import json
import multiprocessing
import os
import socket
import time
from contextlib import ExitStack
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


def _serve(
    root: str,
    port: int,
    *,
    protocol: str | None = None,
    model_url: str | None = None,
    config_dir: str | None = None,
    persistence_dir: str | None = None,
    backup_dir: str | None = None,
    observation_dir: str | None = None,
    resident_timeout: float = 300,
    tool_count: int = 3,
    fault: str | None = None,
    host: str = "127.0.0.1",
    pipeline_dir: str | None = None,
    artifact_mode: bool = False,
) -> None:
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
    os.environ["IAC_CODE_CONFIG_DIR"] = config_dir or str(root_path / "config")
    os.environ["IAC_CODE_CONFIG_BACKUP_DIR"] = backup_dir or str(root_path / "backup")
    os.environ["IAC_CODE_MODE"] = "pipeline" if pipeline_dir else "normal"
    if pipeline_dir:
        os.environ["IAC_CODE_PIPELINE_NAME"] = "permission-engine-fixture"
    os.environ["IACCODE_A2A_ALLOWED_CWDS"] = str(root_path)
    os.environ.pop("IAC_CODE_CONFIG_BACKUP_TMP_DIR", None)
    observation_path = Path(observation_dir) if observation_dir else root_path
    observation_path.mkdir(parents=True, exist_ok=True)
    inputs = observation_path / "model-inputs.jsonl"

    async def close_runtime():
        if fault == "runtime-close":
            import asyncio

            (observation_path / "runtime-close-started").touch()
            while not (observation_path / "release-runtime-close").exists():
                await asyncio.sleep(0.02)

    class RecordingTool(WriteTool):
        async def execute(self, *, tool_input, context):
            with (observation_path / "executions.jsonl").open("a", encoding="utf-8") as output:
                output.write(json.dumps(tool_input) + "\n")
            if fault == "tool-block":
                import asyncio

                (observation_path / "tool-started").touch()
                while not (observation_path / "release-tool").exists():
                    await asyncio.sleep(0.02)
            return ToolResult.success("executed")

    class Provider:
        def get_model_name(self):
            return "test"

        async def stream(self, messages, system, tools=None, max_tokens=8192):
            with inputs.open("a", encoding="utf-8") as output:
                output.write(json.dumps([asdict(message) for message in messages]) + "\n")
            yield MessageStartEvent(message_id="model-message")
            is_step = any(tool.name == "complete_step" for tool in tools or [])
            has_results = any(
                block.type == "tool_result"
                for message in messages
                if isinstance(message.content, list)
                for block in message.content
            )
            if (
                artifact_mode
                and isinstance(messages[-1].content, str)
                and messages[-1].content in {"create template", "edit template"}
            ):
                creating = messages[-1].content == "create template"
                name = "write_file" if creating else "edit_file"
                arguments = {"path": "template.yaml"}
                if creating:
                    arguments["content"] = "ROSTemplateFormatVersion: '2015-09-01'\nDescription: old\n"
                else:
                    arguments.update(old_string="Description: old", new_string="Description: updated template content")
                yield ToolUseStartEvent(tool_use_id="artifact-tool", name=name)
                yield ToolUseEndEvent(tool_use_id="artifact-tool", name=name, input=arguments)
                yield MessageEndEvent(stop_reason="tool_use", usage=Usage())
            elif is_step and has_results:
                yield ToolUseStartEvent(tool_use_id="complete-step", name="complete_step")
                yield ToolUseEndEvent(
                    tool_use_id="complete-step", name="complete_step", input={"conclusion": {"status": "ready"}}
                )
                yield MessageEndEvent(stop_reason="tool_use", usage=Usage())
            elif messages[-1].content == "original" or is_step:
                for index in range(tool_count):
                    tool_id = f"tool-{index + 1}"
                    yield ToolUseStartEvent(tool_use_id=tool_id, name="write_test")
                    yield ToolUseEndEvent(tool_use_id=tool_id, name="write_test", input={"value": tool_id})
                yield MessageEndEvent(stop_reason="tool_use", usage=Usage())
            else:
                text = "handled new prompt" if isinstance(messages[-1].content, str) else "completed approved batch"
                yield TextDeltaEvent(text=text)
                yield MessageEndEvent(stop_reason="end_turn", usage=Usage())

    def make_provider():
        if protocol is None:
            return Provider()
        from iac_code.providers.anthropic_provider import AnthropicProvider
        from iac_code.providers.openai_provider import OpenAIProvider
        from iac_code.providers.responses_provider import ResponsesProvider

        provider_type = {"chat": OpenAIProvider, "responses": ResponsesProvider, "anthropic": AnthropicProvider}[
            protocol
        ]
        return provider_type(model="fixture", api_key="fixture-key", base_url=model_url, thinking_enabled=False)

    def runtime(options):
        from iac_code.tools.base import ToolRegistry

        registry = ToolRegistry()
        if artifact_mode:
            from iac_code.tools.edit_file import EditFileTool
            from iac_code.tools.write_file import WriteFileTool

            registry.register(WriteFileTool())
            registry.register(EditFileTool())
        else:
            registry.register(RecordingTool())
        provider = make_provider()
        return SimpleNamespace(
            session_id=options.session_id,
            provider_manager=provider,
            tool_registry=registry,
            aclose=close_runtime,
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

    with ExitStack() as patches:
        patches.enter_context(patch("iac_code.a2a.executor.create_agent_runtime", runtime))
        if fault == "runtime-close":
            from iac_code.a2a.task_store import A2ATaskStore

            close_for_handoff = A2ATaskStore.discard_context_runtime_for_input_handoff

            async def observed_close(self, context_id):
                try:
                    await close_for_handoff(self, context_id)
                except asyncio.TimeoutError:
                    (observation_path / "runtime-close-wait-expired").touch()
                    raise

            patches.enter_context(patch("iac_code.a2a.task_store._RUNTIME_CLOSE_TIMEOUT_SECONDS", 0.1))
            patches.enter_context(
                patch.object(A2ATaskStore, "discard_context_runtime_for_input_handoff", observed_close)
            )
        if pipeline_dir:
            pipelines = {"permission-engine-fixture": Path(pipeline_dir)}
            patches.enter_context(patch("iac_code.pipeline.discover_pipelines", return_value=pipelines))
            patches.enter_context(patch("iac_code.a2a.pipeline_executor.discover_pipelines", return_value=pipelines))
            patches.enter_context(patch("iac_code.a2a.pipeline_executor.create_agent_runtime", runtime))
        patches.enter_context(patch("tiktoken.get_encoding", side_effect=RuntimeError("offline test")))
        if fault == "backup-failure":
            from iac_code.services.session_backup import SessionBackupService

            original_backup = SessionBackupService.backup_recoverable_input_handoff

            def gated_backup(self, *args, **kwargs):
                if not (observation_path / "release-backup").exists():
                    (observation_path / "backup-failed").touch()
                    raise OSError("injected shared backup write failure")
                return original_backup(self, *args, **kwargs)

            patches.enter_context(patch.object(SessionBackupService, "backup_recoverable_input_handoff", gated_backup))
        if fault == "crash-before-commit":
            from iac_code.a2a.permission_handoff import SuspendedPermissionHandoff

            def crash_before_commit(*_args):
                (observation_path / "crash-before-commit").touch()
                os._exit(77)

            patches.enter_context(patch.object(SuspendedPermissionHandoff, "_publish_committed", crash_before_commit))
        app = create_app(
            host=host,
            port=port,
            token=None,
            model="test",
            auto_approve_permissions=artifact_mode,
            persistence_dir=Path(persistence_dir) if persistence_dir else root_path / "a2a",
            artifact_dir=root_path / "a2a/artifacts" if artifact_mode else None,
            permission_wait={"resident_timeout_seconds": resident_timeout, "timeout_grace_seconds": 0},
        )
        from iac_code.a2a.execution_owner import ExecutionOwnerIdentity

        Path(os.environ["IAC_CODE_CONFIG_DIR"]).mkdir(parents=True, exist_ok=True)
        (Path(os.environ["IAC_CODE_CONFIG_DIR"]) / "fixture-owner.json").write_text(
            json.dumps(ExecutionOwnerIdentity.capture().model_dump(mode="json", by_alias=True)), encoding="utf-8"
        )
        uvicorn.run(app, host=host, port=port, log_level="error")


class Server:
    def __init__(self, root: Path, **options):
        self.root = root
        self.options = options
        self.process = None
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            self.port = sock.getsockname()[1]
        self.client = httpx.Client(base_url=f"http://127.0.0.1:{self.port}", trust_env=False, timeout=15)

    def start(self):
        self.process = multiprocessing.get_context("spawn").Process(
            target=_serve, args=(str(self.root), self.port), kwargs=self.options
        )
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


def _reply(permission, decision="allow_once"):
    return "IAC_CODE_PERMISSION: " + json.dumps(
        {
            **{
                key: permission[key]
                for key in ("schemaVersion", "kind", "requestTaskId", "contextId", "inputId", "toolUseId")
            },
            "decision": decision,
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
@pytest.mark.parametrize(
    ("restart", "middle_decision"),
    [(False, "allow_once"), (True, "allow_once"), (False, "deny"), (True, "deny")],
    ids=["same-process", "after-restart", "mixed-deny-same-process", "mixed-deny-after-restart"],
)
def test_permission_confirmation_continues_batch_and_accepts_next_prompt(
    tmp_path, monkeypatch, restart, middle_decision
):
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

        with server.stream(_reply(second, middle_decision), task_id=second["requestTaskId"]) as response:
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
        expected_tools = ["tool-1", "tool-3"] if middle_decision == "deny" else ["tool-1", "tool-2", "tool-3"]
        assert sorted(item["value"] for item in executions) == expected_tools
        inputs_path = tmp_path / "model-inputs.jsonl"
        inputs = [json.loads(line) for line in inputs_path.read_text(encoding="utf-8").splitlines()]
        assert len(inputs) == 2
        results = inputs[-1][-1]["content"]
        result_ids = [result["tool_use_id"] for result in results]
        assert sorted(result_ids) == ["tool-1", "tool-2", "tool-3"]
        if middle_decision == "allow_once":
            assert result_ids == ["tool-1", "tool-2", "tool-3"]
        for result in results:
            denied = middle_decision == "deny" and result["tool_use_id"] == "tool-2"
            assert result["is_error"] is denied
            assert "user explicitly denied" in result["content"].lower() if denied else result["content"] == "executed"

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
        assert len(executions_path.read_text(encoding="utf-8").splitlines()) == len(expected_tools)
        assert len(inputs_path.read_text(encoding="utf-8").splitlines()) == 3

        # Repeated confirmations must acknowledge/reject the old receipt,
        # without executing the already completed batch a second time.
        for cold in (False, True):
            if cold:
                server.stop()
                server.start()
            for permission, decision in ((first, "allow_once"), (second, middle_decision), (third, "allow_once")):
                server.send(_reply(permission, decision), task_id=permission["requestTaskId"])
            assert len(executions_path.read_text(encoding="utf-8").splitlines()) == len(expected_tools)
            assert len(inputs_path.read_text(encoding="utf-8").splitlines()) == 3
        saved = SessionStorage().load(ctx.cwd, ctx.session_id)
        assert [message.get_text() for message in saved].count("new prompt") == 1
    finally:
        server.stop()
        server.client.close()
