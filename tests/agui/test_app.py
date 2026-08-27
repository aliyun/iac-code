from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest
from starlette.testclient import TestClient

from iac_code.a2a.app import create_app as create_a2a_app
from iac_code.a2a.client import A2AClientResponse
from iac_code.agui.adapter import AguiA2AAdapter, ThreadBinding
from iac_code.agui.app import create_app
from iac_code.agui.events import A2AEventMapper, a2a_state
from iac_code.types.stream_events import TextDeltaEvent
from tests.a2a.fakes import FakeAgentLoop, FakeRuntime


class FakeA2AClient:
    def __init__(self, *, interrupt: bool = False, input_value: dict[str, Any] | None = None) -> None:
        self.interrupt = interrupt
        self.input_value = input_value
        self.sent_parts: list[dict[str, Any]] = []
        self.resumed_prompts: list[tuple[str, str | None]] = []
        self.stream_contexts: list[str] = []
        self.stream_options: list[dict[str, Any]] = []
        self.cancelled: list[str] = []
        self.restored_sessions: list[tuple[str, str]] = []
        self.resume_preflight_calls: list[str] = []
        self.session_available = True
        self.closed = False

    def stream_message_parts(self, _url, _parts, *, context_id, **kwargs):
        self.stream_contexts.append(context_id)
        self.stream_options.append(kwargs)

        async def events():
            if kwargs.get("task_id") is not None:
                self.sent_parts.extend(_parts)
                yield _text_event(context_id=context_id, text="resumed")
                yield _event(context_id=context_id, state="TASK_STATE_INPUT_REQUIRED")
                return
            yield _event(context_id=context_id)
            if self.interrupt:
                yield _permission_event(context_id=context_id)
                return
            if self.input_value is not None:
                value = dict(self.input_value)
                value["contextId"] = context_id
                value["requestTaskId"] = "task-1"
                tool_use_id = value.get("toolUseId")
                if isinstance(tool_use_id, str) and tool_use_id:
                    yield _tool_event(
                        context_id=context_id,
                        value={"status": "started", "toolUseId": tool_use_id, "name": "ask_user_question"},
                    )
                    yield _tool_event(
                        context_id=context_id,
                        value={
                            "status": "input_complete",
                            "toolUseId": tool_use_id,
                            "name": "ask_user_question",
                            "toolInput": {"prompt": value.get("prompt")},
                        },
                    )
                yield _input_event(context_id=context_id, value=value)
                return
            yield _text_event(context_id=context_id, text="hello")
            yield _tool_event(
                context_id=context_id,
                value={"status": "started", "toolUseId": "tool-1", "name": "bash"},
            )
            yield _tool_event(
                context_id=context_id,
                value={
                    "status": "input_complete",
                    "toolUseId": "tool-1",
                    "name": "bash",
                    "toolInput": {"command": "pwd"},
                },
            )
            yield _event(context_id=context_id, state="TASK_STATE_INPUT_REQUIRED")

        return events()

    def stream_message(self, _url, prompt, *, context_id, task_id=None, **_kwargs):
        self.resumed_prompts.append((prompt, task_id))

        async def events():
            yield _text_event(context_id=context_id, text="resumed")
            yield _event(context_id=context_id, state="TASK_STATE_INPUT_REQUIRED")

        return events()

    async def send_message_parts(self, _url, parts, **_kwargs):
        self.sent_parts.extend(parts)
        return object()

    async def get_task(self, _url, _task_id, *, history_length=None):
        del history_length
        self.resume_preflight_calls.append("get_task")
        if self.interrupt:
            return _permission_event(context_id=self.context_id)
        if self.input_value is not None:
            value = dict(self.input_value)
            value["contextId"] = self.context_id
            value["requestTaskId"] = "task-1"
            return _input_event(context_id=self.context_id, value=value)
        return _event(context_id=self.context_id)

    async def get_pipeline_state(self, _url, *, task_id, after_sequence=None):
        del task_id, after_sequence
        self.resume_preflight_calls.append("get_pipeline_state")
        return None

    async def ensure_session_restored(self, _url, *, cwd, session_id):
        self.restored_sessions.append((cwd, session_id))
        self.resume_preflight_calls.append("ensure_session_restored")
        return self.session_available

    def subscribe_task(self, _url, _task_id):
        async def events():
            yield _text_event(context_id=self.context_id, text="resumed")
            yield _event(context_id=self.context_id, state="TASK_STATE_INPUT_REQUIRED")

        return events()

    async def cancel_task(self, _url, task_id):
        self.cancelled.append(task_id)
        return {}

    async def aclose(self):
        self.closed = True

    @property
    def context_id(self) -> str:
        return getattr(self, "_context_id", "")

    @context_id.setter
    def context_id(self, value: str) -> None:
        self._context_id = value


def _event(*, context_id: str, state: str = "TASK_STATE_WORKING") -> dict[str, Any]:
    return {
        "result": {
            "taskId": "task-1",
            "contextId": context_id,
            "status": {"state": state},
            "metadata": {"iac_code": {"iacCodeSessionId": "session-1"}},
        }
    }


def _text_event(*, context_id: str, text: str) -> dict[str, Any]:
    return {
        "result": {
            "taskId": "task-1",
            "contextId": context_id,
            "status": {
                "state": "TASK_STATE_WORKING",
                "message": {
                    "messageId": "assistant-1",
                    "role": "ROLE_AGENT",
                    "parts": [{"text": text}],
                },
            },
        }
    }


def _tool_event(*, context_id: str, value: dict[str, Any]) -> dict[str, Any]:
    event = _event(context_id=context_id)
    event["result"]["metadata"] = {"iac_code": {"tool": value}}
    return event


def _permission_event(*, context_id: str) -> dict[str, Any]:
    event = _event(context_id=context_id, state="TASK_STATE_INPUT_REQUIRED")
    event["result"]["metadata"] = {
        "iac_code": {
            "input": {
                "schemaVersion": 1,
                "kind": "permission",
                "requestTaskId": "task-1",
                "contextId": context_id,
                "inputId": "permission-1",
                "toolUseId": "tool-1",
                "toolName": "bash",
                "title": "Run a local shell command",
                "purpose": "Execute a command for this task.",
                "effect": "local_execution",
                "target": "the current workspace",
                "isReadOnly": False,
                "prompt": "Run a local shell command. Allow once?",
                "safeSummary": "bash: pwd",
                "options": [{"id": "allow_once", "label": "Allow once"}, {"id": "deny", "label": "Deny"}],
                "required": True,
            }
        }
    }
    return event


def _input_event(*, context_id: str, value: dict[str, Any]) -> dict[str, Any]:
    event = _event(context_id=context_id, state="TASK_STATE_INPUT_REQUIRED")
    event["result"]["metadata"] = {"iac_code": {"input": value}}
    return event


def _payload(tmp_path, *, run_id: str = "run-1", resume: list[dict[str, Any]] | None = None):
    return {
        "threadId": "thread-1",
        "runId": run_id,
        "state": {},
        "messages": [] if resume else [{"id": "message-1", "role": "user", "content": "hello"}],
        "tools": [],
        "context": [],
        "forwardedProps": {
            "iacCode": {
                "schemaVersion": 1,
                "rosInvocationId": "invocation-1",
                "cwd": str(tmp_path),
            }
        },
        **({"resume": resume} if resume is not None else {}),
    }


def _events(response: httpx.Response) -> list[dict[str, Any]]:
    return [json.loads(line.removeprefix("data: ")) for line in response.text.splitlines() if line.startswith("data: ")]


@pytest.fixture(autouse=True)
def _isolated_agui_state(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("IAC_CODE_AGUI_STATE_DIR", str(tmp_path / "agui-state"))


@pytest.mark.asyncio
async def test_normal_run_is_translated_from_a2a_to_standard_agui(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("IAC_CODE_AGUI_ALLOWED_CWDS", str(tmp_path))
    fake = FakeA2AClient()
    adapter = AguiA2AAdapter(a2a_url="http://a2a/", client=fake)
    app = create_app(adapter=adapter)

    payload = _payload(tmp_path)
    payload["forwardedProps"]["iacCode"]["runMode"] = "pipeline"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/", json=payload)

    events = _events(response)
    assert response.status_code == 200
    assert events[0]["type"] == "RUN_STARTED"
    assert events[-1]["type"] == "RUN_FINISHED"
    assert events[-1]["outcome"] == {"type": "success"}
    assert "TEXT_MESSAGE_CONTENT" in [event["type"] for event in events]
    assert "TOOL_CALL_ARGS" in [event["type"] for event in events]
    assert fake.stream_options[0]["iac_code_metadata"]["run_mode"] == "pipeline"


@pytest.mark.asyncio
async def test_pipeline_steps_are_balanced_across_interrupt_and_resume_runs(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("IAC_CODE_AGUI_ALLOWED_CWDS", str(tmp_path))

    class PipelineInterruptClient(FakeA2AClient):
        def stream_message_parts(self, _url, parts, *, context_id, **kwargs):
            self.context_id = context_id
            self.stream_contexts.append(context_id)
            self.stream_options.append(kwargs)

            async def events():
                if kwargs.get("task_id") is not None:
                    self.sent_parts.extend(parts)
                    yield _event(context_id=context_id, state="TASK_STATE_INPUT_REQUIRED")
                    return
                yield _event(context_id=context_id)
                event = _event(context_id=context_id)
                permission = _permission_event(context_id=context_id)["result"]["metadata"]["iac_code"]["input"]
                event["result"]["metadata"]["iac_code"].update(
                    {
                        "pipelineBatch": {
                            "events": [
                                {
                                    "eventId": "parent-start",
                                    "eventType": "step_started",
                                    "sequence": 1,
                                    "step": {"id": "evaluate_candidates"},
                                },
                                {
                                    "eventId": "candidate-start",
                                    "eventType": "candidate_step_started",
                                    "sequence": 2,
                                    "candidate": {"runId": "candidate-0"},
                                    "candidateStep": {"id": "template_generating"},
                                },
                            ]
                        },
                        "input": permission,
                    }
                )
                yield event

            return events()

        async def get_task(self, _url, _task_id, *, history_length=None):
            del history_length
            return _permission_event(context_id=self.context_id)

        async def get_pipeline_state(self, _url, *, task_id, after_sequence=None):
            del task_id
            assert after_sequence in {2, 4}
            return {
                "snapshot": {"pipelineRunId": "pipeline-1", "lastSequence": 4},
                "events": [
                    {
                        "eventId": "candidate-complete",
                        "eventType": "candidate_step_completed",
                        "sequence": 3,
                        "candidate": {"runId": "candidate-0"},
                        "candidateStep": {"id": "template_generating"},
                    },
                    {
                        "eventId": "parent-complete",
                        "eventType": "step_completed",
                        "sequence": 4,
                        "step": {"id": "evaluate_candidates"},
                    },
                ],
            }

    def assert_balanced(events: list[dict[str, Any]]) -> None:
        active: set[str] = set()
        for event in events:
            if event["type"] == "STEP_STARTED":
                assert event["stepName"] not in active
                active.add(event["stepName"])
            elif event["type"] == "STEP_FINISHED":
                assert event["stepName"] in active
                active.remove(event["stepName"])
            elif event["type"] == "RUN_FINISHED":
                assert not active

    fake = PipelineInterruptClient()
    adapter = AguiA2AAdapter(a2a_url="http://a2a/", client=fake)
    app = create_app(adapter=adapter)
    initial = _payload(tmp_path)
    initial["forwardedProps"]["iacCode"]["runMode"] = "pipeline"
    resume = _payload(
        tmp_path,
        run_id="run-2",
        resume=[
            {
                "interruptId": "permission-1",
                "status": "resolved",
                "payload": {"decision": "allow_once"},
            }
        ],
    )
    resume["forwardedProps"]["iacCode"]["runMode"] = "pipeline"

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        first = _events(await client.post("/", json=initial))
        second = _events(await client.post("/", json=resume))

    assert first[-1]["outcome"]["type"] == "interrupt"
    assert second[-1]["outcome"] == {"type": "success"}
    assert_balanced(first)
    assert_balanced(second)
    assert adapter._threads["thread-1"].pipeline_open_steps == set()


@pytest.mark.asyncio
async def test_permission_resume_is_sent_to_same_a2a_task_then_resubscribed(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("IAC_CODE_AGUI_ALLOWED_CWDS", str(tmp_path))
    fake = FakeA2AClient(interrupt=True)
    adapter = AguiA2AAdapter(a2a_url="http://a2a/", client=fake)
    app = create_app(adapter=adapter)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        first = await client.post("/", json=_payload(tmp_path))
        fake.context_id = adapter._threads["thread-1"].context_id
        second = await client.post(
            "/",
            json=_payload(
                tmp_path,
                run_id="run-2",
                resume=[
                    {
                        "interruptId": "permission-1",
                        "status": "resolved",
                        "payload": {"decision": "allow_once"},
                    }
                ],
            ),
        )

    first_events = _events(first)
    second_events = _events(second)
    assert first_events[-1]["outcome"]["type"] == "interrupt"
    assert first_events[-1]["outcome"]["interrupts"][0]["message"] == "Run a local shell command. Allow once?"
    assert fake.sent_parts[0]["data"]["decision"] == "allow_once"
    assert fake.sent_parts[0]["data"]["requestTaskId"] == "task-1"
    assert second_events[-1]["outcome"] == {"type": "success"}


@pytest.mark.asyncio
async def test_resume_does_not_replay_the_last_a2a_status_message(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("IAC_CODE_AGUI_ALLOWED_CWDS", str(tmp_path))

    class SnapshotTextClient(FakeA2AClient):
        def stream_message_parts(self, _url, parts, *, context_id, **kwargs):
            self.context_id = context_id

            async def events():
                if kwargs.get("task_id") is not None:
                    self.sent_parts.extend(parts)
                    yield _text_event(context_id=context_id, text="before interruptafter resume")
                    yield _event(context_id=context_id, state="TASK_STATE_INPUT_REQUIRED")
                    return
                yield _event(context_id=context_id)
                yield _text_event(context_id=context_id, text="before interrupt")
                yield _permission_event(context_id=context_id)

            return events()

        async def get_task(self, _url, _task_id, *, history_length=None):
            del history_length
            return _permission_event(context_id=self.context_id)

    fake = SnapshotTextClient()
    state_dir = tmp_path / "state"
    first_adapter = AguiA2AAdapter(a2a_url="http://a2a/", client=fake, state_dir=state_dir)
    first_app = create_app(adapter=first_adapter)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=first_app), base_url="http://test") as client:
        first = _events(await client.post("/", json=_payload(tmp_path)))
    await first_adapter.aclose()

    second_adapter = AguiA2AAdapter(a2a_url="http://a2a/", client=fake, state_dir=state_dir)
    second_app = create_app(adapter=second_adapter)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=second_app), base_url="http://test") as client:
        second = _events(
            await client.post(
                "/",
                json=_payload(
                    tmp_path,
                    run_id="run-2",
                    resume=[
                        {
                            "interruptId": "permission-1",
                            "status": "resolved",
                            "payload": {"decision": "allow_once"},
                        }
                    ],
                ),
            )
        )
    await second_adapter.aclose()

    first_text = [event["delta"] for event in first if event["type"] == "TEXT_MESSAGE_CONTENT"]
    second_text = [event["delta"] for event in second if event["type"] == "TEXT_MESSAGE_CONTENT"]
    assert first_text == ["before interrupt"]
    assert second_text == ["after resume"]


@pytest.mark.asyncio
async def test_invalid_permission_resume_can_be_corrected_without_accepting_execution(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("IAC_CODE_AGUI_ALLOWED_CWDS", str(tmp_path))
    fake = FakeA2AClient(interrupt=True)
    adapter = AguiA2AAdapter(a2a_url="http://a2a/", client=fake)
    app = create_app(adapter=adapter)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        await client.post("/", json=_payload(tmp_path))
        fake.context_id = adapter._threads["thread-1"].context_id
        invalid = await client.post(
            "/",
            json=_payload(
                tmp_path,
                run_id="run-invalid",
                resume=[
                    {
                        "interruptId": "permission-1",
                        "status": "resolved",
                        "payload": {"decision": "allow"},
                    }
                ],
            ),
        )
        invalid_events = _events(invalid)
        assert [event["type"] for event in invalid_events] == ["RUN_STARTED", "RUN_ERROR"]
        assert invalid_events[-1]["code"] == "RESUME_PAYLOAD_INVALID"
        assert set(adapter._threads["thread-1"].pending) == {"permission-1"}
        corrected = await client.post(
            "/",
            json=_payload(
                tmp_path,
                run_id="run-corrected",
                resume=[
                    {
                        "interruptId": "permission-1",
                        "status": "resolved",
                        "payload": {"decision": "allow_once"},
                    }
                ],
            ),
        )

    assert adapter._threads["thread-1"].pending == {}
    assert _events(corrected)[-1]["outcome"] == {"type": "success"}
    assert fake.sent_parts[-1]["data"]["decision"] == "allow_once"


@pytest.mark.asyncio
async def test_permission_resume_without_an_a2a_event_keeps_interrupt_for_retry(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("IAC_CODE_AGUI_ALLOWED_CWDS", str(tmp_path))

    class RetryablePermissionClient(FakeA2AClient):
        fail_resume = True

        def stream_message_parts(self, url, parts, *, context_id, **kwargs):
            if kwargs.get("task_id") is None or not self.fail_resume:
                return super().stream_message_parts(url, parts, context_id=context_id, **kwargs)

            async def events():
                if False:
                    yield {}

            return events()

    fake = RetryablePermissionClient(interrupt=True)
    adapter = AguiA2AAdapter(a2a_url="http://a2a/", client=fake)
    app = create_app(adapter=adapter)
    response = {
        "interruptId": "permission-1",
        "status": "resolved",
        "payload": {"decision": "allow_once"},
    }

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        await client.post("/", json=_payload(tmp_path))
        fake.context_id = adapter._threads["thread-1"].context_id
        failed = await client.post("/", json=_payload(tmp_path, run_id="run-failed", resume=[response]))
        assert [event["type"] for event in _events(failed)] == ["RUN_STARTED", "RUN_ERROR"]
        assert _events(failed)[-1]["code"] == "A2A_UNAVAILABLE"
        assert set(adapter._threads["thread-1"].pending) == {"permission-1"}

        fake.fail_resume = False
        retried = await client.post("/", json=_payload(tmp_path, run_id="run-retried", resume=[response]))

    assert _events(retried)[-1]["outcome"] == {"type": "success"}
    assert any(event.get("name") == "iac-code.session.v1" for event in _events(retried))


@pytest.mark.asyncio
async def test_permission_resume_jsonrpc_error_is_not_accepted(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("IAC_CODE_AGUI_ALLOWED_CWDS", str(tmp_path))

    class RejectedPermissionClient(FakeA2AClient):
        def stream_message_parts(self, url, parts, *, context_id, **kwargs):
            if kwargs.get("task_id") is None:
                return super().stream_message_parts(url, parts, context_id=context_id, **kwargs)

            async def events():
                yield {"jsonrpc": "2.0", "id": "resume", "error": {"code": -32602, "message": "rejected"}}

            return events()

    fake = RejectedPermissionClient(interrupt=True)
    adapter = AguiA2AAdapter(a2a_url="http://a2a/", client=fake)
    app = create_app(adapter=adapter)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        await client.post("/", json=_payload(tmp_path))
        fake.context_id = adapter._threads["thread-1"].context_id
        failed = await client.post(
            "/",
            json=_payload(
                tmp_path,
                run_id="run-failed",
                resume=[
                    {
                        "interruptId": "permission-1",
                        "status": "resolved",
                        "payload": {"decision": "deny"},
                    }
                ],
            ),
        )

    events = _events(failed)
    assert [event["type"] for event in events] == ["RUN_STARTED", "RUN_ERROR"]
    assert events[-1]["code"] == "A2A_UNAVAILABLE"
    assert set(adapter._threads["thread-1"].pending) == {"permission-1"}


@pytest.mark.asyncio
async def test_top_pipeline_permission_resume_uses_streaming_resume(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("IAC_CODE_AGUI_ALLOWED_CWDS", str(tmp_path))
    fake = FakeA2AClient(interrupt=True)
    adapter = AguiA2AAdapter(a2a_url="http://a2a/", client=fake)
    app = create_app(adapter=adapter)
    initial = _payload(tmp_path)
    initial["forwardedProps"]["iacCode"]["runMode"] = "pipeline"
    resume = _payload(
        tmp_path,
        run_id="run-2",
        resume=[
            {
                "interruptId": "permission-1",
                "status": "resolved",
                "payload": {"decision": "allow_once"},
            }
        ],
    )
    resume["forwardedProps"]["iacCode"]["runMode"] = "pipeline"

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        await client.post("/", json=initial)
        fake.context_id = adapter._threads["thread-1"].context_id
        response = await client.post("/", json=resume)

    assert _events(response)[-1]["outcome"] == {"type": "success"}
    assert fake.sent_parts[-1]["data"]["decision"] == "allow_once"
    assert len(fake.stream_contexts) == 2


@pytest.mark.asyncio
async def test_sub_pipeline_permission_resume_uses_sideband_send_then_resubscribes(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("IAC_CODE_AGUI_ALLOWED_CWDS", str(tmp_path))

    class SubPipelinePermissionClient(FakeA2AClient):
        def stream_message_parts(self, _url, _parts, *, context_id, **kwargs):
            if kwargs.get("task_id") is not None:
                return super().stream_message_parts(_url, _parts, context_id=context_id, **kwargs)
            self.stream_contexts.append(context_id)
            self.stream_options.append(kwargs)

            async def events():
                yield _event(context_id=context_id)
                event = _event(context_id=context_id)
                permission = _permission_event(context_id=context_id)["result"]["metadata"]["iac_code"]["input"]
                event["result"]["metadata"] = {"iac_code": {"pendingPermissions": [permission]}}
                yield event

            return events()

        async def get_task(self, _url, _task_id, *, history_length=None):
            del history_length
            event = _event(context_id=self.context_id)
            permission = _permission_event(context_id=self.context_id)["result"]["metadata"]["iac_code"]["input"]
            event["result"]["metadata"] = {"iac_code": {"pendingPermissions": [permission]}}
            return event

    fake = SubPipelinePermissionClient()
    adapter = AguiA2AAdapter(a2a_url="http://a2a/", client=fake)
    app = create_app(adapter=adapter)
    initial = _payload(tmp_path)
    initial["forwardedProps"]["iacCode"]["runMode"] = "pipeline"
    resume = _payload(
        tmp_path,
        run_id="run-2",
        resume=[
            {
                "interruptId": "permission-1",
                "status": "resolved",
                "payload": {"decision": "allow_once"},
            }
        ],
    )
    resume["forwardedProps"]["iacCode"]["runMode"] = "pipeline"

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        await client.post("/", json=initial)
        fake.context_id = adapter._threads["thread-1"].context_id
        response = await client.post("/", json=resume)

    assert _events(response)[-1]["outcome"] == {"type": "success"}
    assert fake.sent_parts[-1]["data"]["decision"] == "allow_once"
    assert len(fake.stream_contexts) == 1


@pytest.mark.asyncio
async def test_sub_pipeline_jsonrpc_error_keeps_permission_pending(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("IAC_CODE_AGUI_ALLOWED_CWDS", str(tmp_path))

    class RejectedSidebandClient(FakeA2AClient):
        def stream_message_parts(self, _url, _parts, *, context_id, **kwargs):
            del kwargs

            async def events():
                yield _event(context_id=context_id)
                event = _event(context_id=context_id)
                permission = _permission_event(context_id=context_id)["result"]["metadata"]["iac_code"]["input"]
                event["result"]["metadata"] = {"iac_code": {"pendingPermissions": [permission]}}
                yield event

            return events()

        async def get_task(self, _url, _task_id, *, history_length=None):
            del history_length
            event = _event(context_id=self.context_id)
            permission = _permission_event(context_id=self.context_id)["result"]["metadata"]["iac_code"]["input"]
            event["result"]["metadata"] = {"iac_code": {"pendingPermissions": [permission]}}
            return event

        async def send_message_parts(self, _url, _parts, **_kwargs):
            return A2AClientResponse(
                payload={"jsonrpc": "2.0", "id": "resume", "error": {"code": -32602, "message": "rejected"}}
            )

    fake = RejectedSidebandClient()
    adapter = AguiA2AAdapter(a2a_url="http://a2a/", client=fake)
    app = create_app(adapter=adapter)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        await client.post("/", json=_payload(tmp_path))
        fake.context_id = adapter._threads["thread-1"].context_id
        failed = await client.post(
            "/",
            json=_payload(
                tmp_path,
                run_id="run-failed",
                resume=[
                    {
                        "interruptId": "permission-1",
                        "status": "resolved",
                        "payload": {"decision": "deny"},
                    }
                ],
            ),
        )

    events = _events(failed)
    assert [event["type"] for event in events] == ["RUN_STARTED", "RUN_ERROR"]
    assert events[-1]["code"] == "A2A_UNAVAILABLE"
    assert set(adapter._threads["thread-1"].pending) == {"permission-1"}


def test_pending_permission_is_upgraded_when_it_later_appears_as_sideband(tmp_path) -> None:
    adapter = AguiA2AAdapter(a2a_url="http://a2a/", client=FakeA2AClient())
    binding = ThreadBinding(
        thread_id="thread-1",
        context_id="context-1",
        cwd=str(tmp_path),
        user_id=None,
        ros_invocation_id="invocation-1",
        task_id="task-1",
    )
    permission = _permission_event(context_id="context-1")["result"]["metadata"]["iac_code"]["input"]

    adapter._merge_pending(binding, [permission], replace=False, sideband_ids=set())
    assert binding.pending["permission-1"].sideband is False

    adapter._merge_pending(binding, [permission], replace=False, sideband_ids={"permission-1"})
    assert binding.pending["permission-1"].sideband is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "subscription_events",
    [
        [],
        [{"jsonrpc": "2.0", "error": {"code": -32602, "message": "Task is already completed"}}],
    ],
)
async def test_sub_pipeline_subscribe_failure_refetches_completed_task(
    tmp_path,
    monkeypatch,
    subscription_events,
) -> None:
    monkeypatch.setenv("IAC_CODE_AGUI_ALLOWED_CWDS", str(tmp_path))

    class CompletedBetweenGetAndSubscribeClient(FakeA2AClient):
        def __init__(self) -> None:
            super().__init__()
            self.get_task_calls = 0

        def stream_message_parts(self, _url, _parts, *, context_id, **kwargs):
            if kwargs.get("task_id") is not None:
                return super().stream_message_parts(_url, _parts, context_id=context_id, **kwargs)
            self.stream_contexts.append(context_id)
            self.stream_options.append(kwargs)

            async def events():
                yield _event(context_id=context_id)
                event = _event(context_id=context_id)
                permission = _permission_event(context_id=context_id)["result"]["metadata"]["iac_code"]["input"]
                event["result"]["metadata"] = {"iac_code": {"pendingPermissions": [permission]}}
                yield event

            return events()

        async def get_task(self, _url, _task_id, *, history_length=None):
            del history_length
            self.get_task_calls += 1
            if self.get_task_calls <= 2:
                event = _event(context_id=self.context_id)
                permission = _permission_event(context_id=self.context_id)["result"]["metadata"]["iac_code"]["input"]
                event["result"]["metadata"] = {"iac_code": {"pendingPermissions": [permission]}}
                return event
            return _event(context_id=self.context_id, state="TASK_STATE_COMPLETED")

        def subscribe_task(self, _url, _task_id):
            async def events():
                for event in subscription_events:
                    yield event

            return events()

    fake = CompletedBetweenGetAndSubscribeClient()
    adapter = AguiA2AAdapter(a2a_url="http://a2a/", client=fake)
    app = create_app(adapter=adapter)
    initial = _payload(tmp_path)
    initial["forwardedProps"]["iacCode"]["runMode"] = "pipeline"
    resume = _payload(
        tmp_path,
        run_id="run-2",
        resume=[
            {
                "interruptId": "permission-1",
                "status": "resolved",
                "payload": {"decision": "allow_once"},
            }
        ],
    )
    resume["forwardedProps"]["iacCode"]["runMode"] = "pipeline"

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        await client.post("/", json=initial)
        fake.context_id = adapter._threads["thread-1"].context_id
        response = await client.post("/", json=resume)

    assert _events(response)[-1]["outcome"] == {"type": "success"}
    assert fake.get_task_calls == 3


@pytest.mark.asyncio
async def test_question_selection_resume_is_sent_to_same_a2a_task(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("IAC_CODE_AGUI_ALLOWED_CWDS", str(tmp_path))
    fake = FakeA2AClient(
        input_value={
            "schemaVersion": 1,
            "kind": "ask_user_question",
            "requestTaskId": "task-1",
            "contextId": "unused-by-adapter",
            "inputId": "question-1",
            "toolUseId": "ask-1",
            "prompt": "Choose a plan",
            "options": [
                {"id": "plan-a", "label": "Plan A"},
                {"id": "plan-b", "label": "Plan B"},
            ],
            "allowFreeText": True,
            "required": True,
        }
    )
    adapter = AguiA2AAdapter(a2a_url="http://a2a/", client=fake)
    app = create_app(adapter=adapter)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        first = await client.post("/", json=_payload(tmp_path))
        second = await client.post(
            "/",
            json=_payload(
                tmp_path,
                run_id="run-2",
                resume=[
                    {
                        "interruptId": "question-1",
                        "status": "resolved",
                        "payload": {"selectedId": "plan-b"},
                    }
                ],
            ),
        )

    assert _events(first)[-1]["outcome"]["type"] == "interrupt"
    assert fake.resumed_prompts == [("Plan B", "task-1")]
    second_events = _events(second)
    assert sum(
        event.get("type") == "TOOL_CALL_RESULT" and event.get("toolCallId") == "ask-1"
        for event in second_events
    ) == 1
    assert second_events[-1]["outcome"] == {"type": "success"}


@pytest.mark.asyncio
async def test_question_resume_failure_keeps_answer_pending_for_retry(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("IAC_CODE_AGUI_ALLOWED_CWDS", str(tmp_path))

    class RetryableQuestionClient(FakeA2AClient):
        fail_resume = True

        def stream_message(self, url, prompt, *, context_id, task_id=None, **kwargs):
            if not self.fail_resume:
                return super().stream_message(url, prompt, context_id=context_id, task_id=task_id, **kwargs)

            async def events():
                raise RuntimeError("injected failure before the first A2A event")
                yield {}

            return events()

    fake = RetryableQuestionClient(
        input_value={
            "schemaVersion": 1,
            "kind": "ask_user_question",
            "requestTaskId": "task-1",
            "contextId": "unused-by-adapter",
            "inputId": "question-1",
            "toolUseId": "ask-1",
            "prompt": "Choose a plan",
            "options": [{"id": "plan-a", "label": "Plan A"}],
            "allowFreeText": True,
            "required": True,
        }
    )
    adapter = AguiA2AAdapter(a2a_url="http://a2a/", client=fake)
    app = create_app(adapter=adapter)
    response = {
        "interruptId": "question-1",
        "status": "resolved",
        "payload": {"selectedId": "plan-a"},
    }

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        await client.post("/", json=_payload(tmp_path))
        fake.context_id = adapter._threads["thread-1"].context_id
        failed = await client.post("/", json=_payload(tmp_path, run_id="run-failed", resume=[response]))
        assert [event["type"] for event in _events(failed)] == ["RUN_STARTED", "RUN_ERROR"]
        assert set(adapter._threads["thread-1"].pending) == {"question-1"}

        fake.fail_resume = False
        retried = await client.post("/", json=_payload(tmp_path, run_id="run-retried", resume=[response]))

    assert adapter._threads["thread-1"].pending == {}
    assert _events(retried)[-1]["outcome"] == {"type": "success"}


@pytest.mark.asyncio
async def test_cancel_extension_forwards_to_a2a_task(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("IAC_CODE_AGUI_ALLOWED_CWDS", str(tmp_path))
    fake = FakeA2AClient(interrupt=True)
    adapter = AguiA2AAdapter(a2a_url="http://a2a/", client=fake)
    app = create_app(adapter=adapter)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        first = await client.post("/", json=_payload(tmp_path))
        session = next(event for event in _events(first) if event.get("name") == "iac-code.session.v1")
        response = await client.post(
            "/extensions/iac-code/v1/executions/{}/cancel".format(session["value"]["executionId"]),
            json={"threadId": "thread-1", "rosInvocationId": "invocation-1"},
        )

    assert response.status_code == 200
    assert response.json()["status"] == "cancelled"
    assert fake.cancelled == ["task-1"]


@pytest.mark.asyncio
async def test_ordinary_new_turn_reuses_a2a_context_but_rotates_execution(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("IAC_CODE_AGUI_ALLOWED_CWDS", str(tmp_path))
    fake = FakeA2AClient()
    adapter = AguiA2AAdapter(a2a_url="http://a2a/", client=fake)
    app = create_app(adapter=adapter)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        first = await client.post("/", json=_payload(tmp_path))
        second = await client.post("/", json=_payload(tmp_path, run_id="run-2"))

    first_session = next(event for event in _events(first) if event.get("name") == "iac-code.session.v1")
    second_session = next(event for event in _events(second) if event.get("name") == "iac-code.session.v1")
    assert fake.stream_contexts[0] == fake.stream_contexts[1]
    assert first_session["value"]["contextId"] == second_session["value"]["contextId"]
    assert first_session["value"]["executionId"] != second_session["value"]["executionId"]


@pytest.mark.asyncio
async def test_request_runtime_overrides_are_forwarded_only_as_a2a_metadata(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("IAC_CODE_AGUI_ALLOWED_CWDS", str(tmp_path))
    fake = FakeA2AClient()
    adapter = AguiA2AAdapter(a2a_url="http://a2a/", client=fake)
    app = create_app(adapter=adapter)
    payload = _payload(tmp_path)
    payload["forwardedProps"]["iacCode"].update(
        {
            "model": "qwen-test",
            "llmApiKey": "fake-provider-key",
            "thinking": {"enabled": True, "effort": "low", "budget": 1024},
            "alibabaCloud": {
                "accessKeyId": "fake-access-key",
                "accessKeySecret": "fake-access-secret",
                "securityToken": "fake-sts-token",
                "regionId": "cn-hangzhou",
            },
        }
    )

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/", json=payload)

    assert response.status_code == 200
    options = fake.stream_options[0]
    assert options["model"] == "qwen-test"
    assert options["iac_code_api_key"] == "fake-provider-key"
    assert options["thinking_enabled"] is True
    assert options["thinking_effort"] == "low"
    assert options["thinking_budget"] == 1024
    assert options["iac_code_metadata"] == {
        "cleanupOnly": False,
        "rosInvocationId": "invocation-1",
        "preferredLanguage": "en",
        "alibaba_cloud_access_key_id": "fake-access-key",
        "alibaba_cloud_access_key_secret": "fake-access-secret",
        "alibaba_cloud_security_token": "fake-sts-token",
        "alibaba_cloud_region_id": "cn-hangzhou",
    }


@pytest.mark.asyncio
async def test_heartbeat_remains_sse_comment_and_not_agui_event(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("IAC_CODE_AGUI_ALLOWED_CWDS", str(tmp_path))
    monkeypatch.setattr("iac_code.agui.app._HEARTBEAT_SECONDS", 0.01)

    class SlowA2AClient(FakeA2AClient):
        def stream_message_parts(self, _url, _parts, *, context_id, **kwargs):
            del kwargs

            async def events():
                await asyncio.sleep(0.035)
                yield _event(context_id=context_id)
                yield _event(context_id=context_id, state="TASK_STATE_INPUT_REQUIRED")

            return events()

    adapter = AguiA2AAdapter(a2a_url="http://a2a/", client=SlowA2AClient())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(adapter=adapter)), base_url="http://test"
    ) as client:
        response = await client.post("/", json=_payload(tmp_path))

    assert ": heartbeat\n\n" in response.text
    assert all(event.get("object") != "heartbeat" for event in _events(response))


def test_mapper_consumes_real_local_a2a_wire_contract(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("IAC_CODE_AGUI_ALLOWED_CWDS", str(tmp_path))
    monkeypatch.setenv("IACCODE_A2A_ALLOWED_CWDS", str(tmp_path))
    runtime = FakeRuntime(
        agent_loop=FakeAgentLoop([TextDeltaEvent(text="from-real-a2a-wire")]),
        session_id="session-1",
    )
    monkeypatch.setattr("iac_code.a2a.executor.create_agent_runtime", lambda _options: runtime)
    a2a_app = create_a2a_app(host="127.0.0.1", port=41242, token=None, model="qwen-test")

    with TestClient(a2a_app) as client:
        with client.stream(
            "POST",
            "/",
            headers={"A2A-Version": "1.0"},
            json={
                "jsonrpc": "2.0",
                "id": "request-1",
                "method": "SendStreamingMessage",
                "params": {
                    "message": {
                        "messageId": "message-1",
                        "contextId": "context-1",
                        "role": "ROLE_USER",
                        "parts": [{"text": "hello"}],
                        "metadata": {"iac_code": {"cwd": str(tmp_path)}},
                    },
                    "configuration": {"acceptedOutputModes": ["text/plain"]},
                },
            },
        ) as response:
            raw_events = [
                json.loads(line.removeprefix("data: "))
                for line in response.iter_lines()
                if line.startswith("data: ")
            ]

    mapper = A2AEventMapper(thread_id="thread-1", run_id="run-1")
    mapped = [mapped_event for event in raw_events for mapped_event in mapper.map(event)]
    assert response.status_code == 200
    assert any(getattr(event, "delta", None) == "from-real-a2a-wire" for event in mapped), raw_events
    assert a2a_state(raw_events[-1]) == "input-required"


@pytest.mark.asyncio
async def test_http_errors_use_payload_or_accept_language(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("IAC_CODE_AGUI_ALLOWED_CWDS", str(tmp_path))
    monkeypatch.setattr(
        "iac_code.agui.app.translate_message",
        lambda message, *, language: f"{language}:{message}",
    )
    app = create_app(adapter=AguiA2AAdapter(a2a_url="http://a2a/", client=FakeA2AClient()), auth_token="secret")
    payload = _payload(tmp_path)
    payload["forwardedProps"]["iacCode"]["preferredLanguage"] = "zh-CN"
    payload.pop("threadId")

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        unauthorized = await client.post(
            "/",
            headers={"Accept-Language": "ja-JP, en;q=0.5"},
            json={},
        )
        weighted = await client.post(
            "/",
            headers={"Accept-Language": "zh;q=0, en;q=0.2, ja;q=1"},
            json={},
        )
        invalid = await client.post(
            "/",
            headers={"Authorization": "Bearer secret"},
            json=payload,
        )

    assert unauthorized.json()["error"]["message"] == "ja:A valid bearer token is required."
    assert weighted.json()["error"]["message"] == "ja:A valid bearer token is required."
    assert invalid.json()["error"]["message"] == "zh:Invalid AG-UI RunAgentInput envelope."


@pytest.mark.asyncio
async def test_run_errors_use_request_language_without_global_locale(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("IAC_CODE_AGUI_ALLOWED_CWDS", str(tmp_path))
    monkeypatch.setattr(
        "iac_code.agui.errors.translate_message",
        lambda message, *, language: f"{language}:{message}",
    )

    class FailingA2AClient(FakeA2AClient):
        def stream_message_parts(self, _url, _parts, *, context_id, **kwargs):
            del context_id, kwargs

            async def events():
                raise RuntimeError("injected failure")
                yield {}

            return events()

    payload = _payload(tmp_path)
    payload["forwardedProps"]["iacCode"]["preferredLanguage"] = "zh-CN"
    app = create_app(adapter=AguiA2AAdapter(a2a_url="http://a2a/", client=FailingA2AClient()))

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/", json=payload)

    assert _events(response)[-1]["message"] == "zh:The local A2A execution service is unavailable."


@pytest.mark.asyncio
async def test_accept_language_reaches_stream_errors_and_a2a_metadata(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("IAC_CODE_AGUI_ALLOWED_CWDS", str(tmp_path))
    monkeypatch.setattr(
        "iac_code.agui.errors.translate_message",
        lambda message, *, language: f"{language}:{message}",
    )

    class FailingA2AClient(FakeA2AClient):
        def stream_message_parts(self, _url, _parts, *, context_id, **kwargs):
            self.stream_contexts.append(context_id)
            self.stream_options.append(kwargs)

            async def events():
                raise RuntimeError("injected failure")
                yield {}

            return events()

    fake = FailingA2AClient()
    payload = _payload(tmp_path)
    app = create_app(adapter=AguiA2AAdapter(a2a_url="http://a2a/", client=fake))

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/",
            headers={"Accept-Language": "en;q=0.2, zh-CN;q=1"},
            json=payload,
        )

    assert _events(response)[-1]["message"] == "zh:The local A2A execution service is unavailable."
    assert fake.stream_options[0]["iac_code_metadata"]["preferredLanguage"] == "zh"


@pytest.mark.asyncio
async def test_idle_monitor_requests_server_shutdown_without_killing_process() -> None:
    from iac_code.agui.app import _monitor_idle

    shutdown_requested: list[bool] = []
    adapter = FakeA2AClient()
    adapter.is_idle = True
    adapter.last_activity = asyncio.get_running_loop().time() - 10

    await _monitor_idle(adapter, 0.01, lambda: shutdown_requested.append(True))

    assert shutdown_requested == [True]
