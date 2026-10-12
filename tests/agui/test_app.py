from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest
from starlette.testclient import TestClient

from iac_code.a2a.app import create_app as create_a2a_app
from iac_code.a2a.client import A2AClientResponse, A2ASessionBackupNotReadyError
from iac_code.a2a.resource_selector import RESOURCE_SELECTION_QUERY_PREFIX
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
        self.restored_sessions: list[tuple[str, str, str | None]] = []
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

    async def ensure_session_restored(self, _url, *, cwd, session_id, task_id=None):
        self.restored_sessions.append((cwd, session_id, task_id))
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


def _resource_selector_input(*, selector_id: str = "vpc.vpc") -> dict[str, Any]:
    return {
        "schemaVersion": 1,
        "kind": "cloud_resource_selection",
        "inputId": "resource-" + "a" * 32,
        "toolUseId": "select-1",
        "prompt": "Choose a VPC",
        "required": True,
        "selector": {
            "id": selector_id,
            "associationProperty": "ALIYUN::VPC::VPC::VPCId",
            "outputKind": "resource_id",
            "associationPropertyMetadata": {},
            "source": None,
        },
    }


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
async def test_permission_resume_maps_backup_sync_error_to_retryable_agui_error(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("IAC_CODE_AGUI_ALLOWED_CWDS", str(tmp_path))

    class SyncingPermissionClient(FakeA2AClient):
        def stream_message_parts(self, url, parts, *, context_id, **kwargs):
            if kwargs.get("task_id") is None:
                return super().stream_message_parts(url, parts, context_id=context_id, **kwargs)

            async def events():
                yield {
                    "jsonrpc": "2.0",
                    "id": "resume",
                    "error": {
                        "code": -32602,
                        "message": "Session backup is still synchronizing. Retry after 3 seconds.",
                        "data": {"code": "SESSION_BACKUP_NOT_READY", "retryable": True},
                    },
                }

            return events()

    fake = SyncingPermissionClient(interrupt=True)
    adapter = AguiA2AAdapter(a2a_url="http://a2a/", client=fake)
    app = create_app(adapter=adapter)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        await client.post("/", json=_payload(tmp_path))
        fake.context_id = adapter._threads["thread-1"].context_id
        response = await client.post(
            "/",
            json=_payload(
                tmp_path,
                run_id="run-syncing",
                resume=[
                    {
                        "interruptId": "permission-1",
                        "status": "resolved",
                        "payload": {"decision": "allow_once"},
                    }
                ],
            ),
        )

    terminal = _events(response)[-1]
    assert terminal["type"] == "RUN_ERROR"
    assert terminal["code"] == "SESSION_BACKUP_NOT_READY"
    assert terminal["message"] == "Session backup is still synchronizing. Retry after 3 seconds."
    assert set(adapter._threads["thread-1"].pending) == {"permission-1"}


@pytest.mark.asyncio
async def test_permission_resume_maps_preflight_backup_sync_error_without_sending_response(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("IAC_CODE_AGUI_ALLOWED_CWDS", str(tmp_path))

    class SyncingPreflightClient(FakeA2AClient):
        async def ensure_session_restored(self, _url, *, cwd, session_id, task_id=None):
            self.restored_sessions.append((cwd, session_id, task_id))
            self.resume_preflight_calls.append("ensure_session_restored")
            raise A2ASessionBackupNotReadyError("Session backup is still synchronizing. Retry after 3 seconds.")

    fake = SyncingPreflightClient(interrupt=True)
    adapter = AguiA2AAdapter(a2a_url="http://a2a/", client=fake)
    app = create_app(adapter=adapter)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        await client.post("/", json=_payload(tmp_path))
        fake.context_id = adapter._threads["thread-1"].context_id
        response = await client.post(
            "/",
            json=_payload(
                tmp_path,
                run_id="run-syncing-preflight",
                resume=[
                    {
                        "interruptId": "permission-1",
                        "status": "resolved",
                        "payload": {"decision": "allow_once"},
                    }
                ],
            ),
        )

    terminal = _events(response)[-1]
    assert terminal["type"] == "RUN_ERROR"
    assert terminal["code"] == "SESSION_BACKUP_NOT_READY"
    assert terminal["message"] == "Session backup is still synchronizing. Retry after 3 seconds."
    assert fake.restored_sessions[-1] == (str(tmp_path), "session-1", "task-1")
    assert fake.sent_parts == []
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
    assert (
        sum(event.get("type") == "TOOL_CALL_RESULT" and event.get("toolCallId") == "ask-1" for event in second_events)
        == 1
    )
    assert second_events[-1]["outcome"] == {"type": "success"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "answer", "expected"),
    [
        (
            "resolved",
            {"value": "vpc-123456", "label": "test-vpc"},
            {"status": "selected", "value": "vpc-123456", "label": "test-vpc"},
        ),
        ("resolved", {"freeText": "vpc-987654"}, {"status": "selected", "value": "vpc-987654", "label": "vpc-987654"}),
        ("cancelled", {"optionsEmpty": True}, {"status": "canceled", "optionsEmpty": True}),
    ],
)
async def test_resource_selector_resume_uses_structured_a2a_contract(
    tmp_path, monkeypatch, status, answer, expected
) -> None:
    monkeypatch.setenv("IAC_CODE_AGUI_ALLOWED_CWDS", str(tmp_path))
    fake = FakeA2AClient(input_value=_resource_selector_input())
    adapter = AguiA2AAdapter(a2a_url="http://a2a/", client=fake, state_dir=tmp_path / "state")
    app = create_app(adapter=adapter)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        first = _events(await client.post("/", json=_payload(tmp_path)))
        interrupt = first[-1]["outcome"]["interrupts"][0]
        assert interrupt["metadata"]["selector"]["id"] == "vpc.vpc"
        assert interrupt["responseSchema"]["oneOf"]
        fake.context_id = adapter._threads["thread-1"].context_id
        second = _events(
            await client.post(
                "/",
                json=_payload(
                    tmp_path,
                    run_id="run-2",
                    resume=[{"interruptId": interrupt["id"], "status": status, "payload": answer}],
                ),
            )
        )
    assert second[-1]["outcome"] == {"type": "success"}
    assert fake.cancelled == []
    prompt, task_id = fake.resumed_prompts[0]
    assert task_id == "task-1"
    assert prompt.startswith(RESOURCE_SELECTION_QUERY_PREFIX)
    response = json.loads(prompt.removeprefix(RESOURCE_SELECTION_QUERY_PREFIX))
    assert response.items() >= expected.items()
    assert response["requestTaskId"] == "task-1"
    assert response["contextId"] == fake.context_id
    assert response["inputId"] == interrupt["id"]
    assert response["toolUseId"] == "select-1"
    if status == "resolved":
        assert response["selectorId"] == "vpc.vpc"
        assert sum(event.get("type") == "TOOL_CALL_RESULT" for event in second) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("continuation", ["input", "completed", "failed", "eof"])
@pytest.mark.parametrize("send_boundary", ["working", "empty-input", "consumed-input"])
async def test_pipeline_selector_resume_observes_native_continuation_after_send_stream_eof(
    tmp_path,
    monkeypatch,
    continuation,
    send_boundary,
):
    monkeypatch.setenv("IAC_CODE_AGUI_ALLOWED_CWDS", str(tmp_path))

    class ContinuingClient(FakeA2AClient):
        accepted = False
        subscribe_calls = 0

        def stream_message(self, _url, prompt, *, context_id, task_id=None, **kwargs):
            self.resumed_prompts.append((prompt, task_id))

            async def events():
                self.accepted = True
                yield _text_event(context_id=context_id, text="Returning to candidate selection")
                if send_boundary == "consumed-input":
                    yield _input_event(context_id=context_id, value=_resource_selector_input())
                else:
                    state = "TASK_STATE_WORKING" if send_boundary == "working" else "TASK_STATE_INPUT_REQUIRED"
                    yield _event(context_id=context_id, state=state)

            return events()

        async def get_task(self, _url, _task_id, *, history_length=None):
            if not self.accepted:
                return await super().get_task(_url, _task_id, history_length=history_length)
            # The task store still has the prior wait state, but the consumed
            # selector is gone and the pipeline is publishing its next wait.
            return _event(context_id=self.context_id, state="TASK_STATE_INPUT_REQUIRED")

        def subscribe_task(self, _url, _task_id):
            self.subscribe_calls += 1

            async def events():
                if continuation == "eof":
                    return
                if continuation in {"completed", "failed"}:
                    yield _event(context_id=self.context_id, state="TASK_STATE_" + continuation.upper())
                    return
                yield _event(context_id=self.context_id, state="TASK_STATE_WORKING")
                yield _input_event(
                    context_id=self.context_id,
                    value={
                        "schemaVersion": 1,
                        "kind": "candidate_selection",
                        "required": True,
                        "requestTaskId": "task-1",
                        "contextId": self.context_id,
                        "inputId": "next-selection",
                        "prompt": "Choose the replanned candidate",
                        "options": [{"id": "0", "label": "Candidate A"}],
                    },
                )

            return events()

    fake = ContinuingClient(input_value=_resource_selector_input())
    adapter = AguiA2AAdapter(a2a_url="http://a2a/", client=fake, state_dir=tmp_path / "state")

    def payload(run_id, resume=None):
        value = _payload(tmp_path, run_id=run_id, resume=resume)
        value["forwardedProps"]["iacCode"].update(runMode="pipeline", pipelineName="selling_solution_first")
        return value

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(adapter=adapter)), base_url="http://test"
    ) as client:
        initial = _events(await client.post("/", json=payload("initial")))
        interrupt = initial[-1]["outcome"]["interrupts"][0]
        fake.context_id = adapter._threads["thread-1"].context_id
        resumed = _events(
            await client.post(
                "/",
                json=payload(
                    "resumed",
                    [
                        {
                            "interruptId": interrupt["id"],
                            "status": "cancelled",
                            "payload": {"optionsEmpty": False},
                        }
                    ],
                ),
            )
        )
    if continuation == "input":
        assert resumed[-1]["type"] == "RUN_FINISHED"
        assert resumed[-1]["outcome"]["type"] == "interrupt"
        assert resumed[-1]["outcome"]["interrupts"][0]["id"] == "next-selection"
        assert set(adapter._threads["thread-1"].pending) == {"next-selection"}
    elif continuation == "completed":
        assert resumed[-1]["type"] == "RUN_FINISHED" and resumed[-1]["outcome"]["type"] == "success"
    else:
        assert resumed[-1]["type"] == "RUN_ERROR"
        assert resumed[-1]["code"] == ("A2A_EXECUTION_FAILED" if continuation == "failed" else "A2A_UNAVAILABLE")
    assert fake.subscribe_calls == 1 and len(fake.resumed_prompts) == 1
    assert fake.cancelled == (["task-1"] if continuation == "eof" else [])


@pytest.mark.asyncio
async def test_resource_selector_invalid_answer_is_retryable_after_adapter_restart(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("IAC_CODE_AGUI_ALLOWED_CWDS", str(tmp_path))
    fake = FakeA2AClient(input_value=_resource_selector_input())
    state_dir = tmp_path / "state"
    first_adapter = AguiA2AAdapter(a2a_url="http://a2a/", client=fake, state_dir=state_dir)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(adapter=first_adapter)), base_url="http://test"
    ) as client:
        first = _events(await client.post("/", json=_payload(tmp_path)))
    interrupt_id = first[-1]["outcome"]["interrupts"][0]["id"]
    fake.context_id = first_adapter._threads["thread-1"].context_id
    second_adapter = AguiA2AAdapter(a2a_url="http://a2a/", client=fake, state_dir=state_dir)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(adapter=second_adapter)), base_url="http://test"
    ) as client:
        invalid = _events(
            await client.post(
                "/",
                json=_payload(
                    tmp_path,
                    run_id="run-invalid",
                    resume=[{"interruptId": interrupt_id, "status": "resolved", "payload": {"value": "bad value"}}],
                ),
            )
        )
        assert invalid[-1]["type"] == "RUN_ERROR"
        assert invalid[-1]["code"] == "RESUME_PAYLOAD_INVALID"
        assert fake.resumed_prompts == []
        corrected = _events(
            await client.post(
                "/",
                json=_payload(
                    tmp_path,
                    run_id="run-corrected",
                    resume=[{"interruptId": interrupt_id, "status": "resolved", "payload": {"value": "vpc-123456"}}],
                ),
            )
        )
    assert corrected[-1].get("outcome") == {"type": "success"}, corrected
    assert second_adapter._threads["thread-1"].pending == {}


@pytest.mark.asyncio
async def test_derived_resource_selector_preserves_authoritative_source_on_resume(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("IAC_CODE_AGUI_ALLOWED_CWDS", str(tmp_path))
    input_value = _resource_selector_input(selector_id="redis.connection_url")
    source = {
        "selector_id": "redis.instance",
        "value": "r-test0001",
        "association_property_metadata": {"RegionId": "cn-hangzhou"},
    }
    input_value["selector"].update(
        {
            "associationProperty": "ALIYUN::Redis::Instance::ConnectionURL",
            "outputKind": "endpoint",
            "associationPropertyMetadata": {"RegionId": "cn-hangzhou", "InstanceId": "r-test0001"},
            "source": source,
        }
    )
    fake = FakeA2AClient(input_value=input_value)
    adapter = AguiA2AAdapter(a2a_url="http://a2a/", client=fake)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(adapter=adapter)), base_url="http://test"
    ) as client:
        first = _events(await client.post("/", json=_payload(tmp_path)))
        fake.context_id = adapter._threads["thread-1"].context_id
        second = _events(
            await client.post(
                "/",
                json=_payload(
                    tmp_path,
                    run_id="run-2",
                    resume=[
                        {
                            "interruptId": first[-1]["outcome"]["interrupts"][0]["id"],
                            "status": "resolved",
                            "payload": {"value": "redis://example.com:6379"},
                        }
                    ],
                ),
            )
        )
    assert second[-1]["outcome"] == {"type": "success"}
    response = json.loads(fake.resumed_prompts[0][0].removeprefix(RESOURCE_SELECTION_QUERY_PREFIX))
    assert response["selectorId"] == "redis.connection_url"
    assert response["source"] == source


@pytest.mark.asyncio
async def test_resource_selector_rejected_a2a_response_keeps_interrupt_for_retry(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("IAC_CODE_AGUI_ALLOWED_CWDS", str(tmp_path))

    class RetryableResourceSelectionClient(FakeA2AClient):
        reject_resume = True

        def stream_message(self, url, prompt, *, context_id, task_id=None, **kwargs):
            if not self.reject_resume:
                return super().stream_message(url, prompt, context_id=context_id, task_id=task_id, **kwargs)

            async def rejected():
                yield {"jsonrpc": "2.0", "id": "resume", "error": {"code": -32602, "message": "rejected"}}

            return rejected()

    fake = RetryableResourceSelectionClient(input_value=_resource_selector_input())
    adapter = AguiA2AAdapter(a2a_url="http://a2a/", client=fake)
    response = {
        "interruptId": _resource_selector_input()["inputId"],
        "status": "resolved",
        "payload": {"value": "vpc-123456"},
    }
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(adapter=adapter)), base_url="http://test"
    ) as client:
        await client.post("/", json=_payload(tmp_path))
        fake.context_id = adapter._threads["thread-1"].context_id
        failed = _events(await client.post("/", json=_payload(tmp_path, run_id="run-failed", resume=[response])))
        assert failed[-1]["code"] == "A2A_UNAVAILABLE"
        assert set(adapter._threads["thread-1"].pending) == {response["interruptId"]}
        assert fake.cancelled == []
        fake.reject_resume = False
        retried = _events(await client.post("/", json=_payload(tmp_path, run_id="run-retried", resume=[response])))
    assert retried[-1]["outcome"] == {"type": "success"}
    assert len(fake.resumed_prompts) == 1
    assert fake.resumed_prompts[0][1] == "task-1"
    assert fake.resumed_prompts[0][0].startswith(RESOURCE_SELECTION_QUERY_PREFIX)
    assert adapter._threads["thread-1"].pending == {}


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
            "llmHeaders": {
                "Authorization": "Bearer fake-caller-token",
                "X-Caller-Session": "session-1",
            },
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
        "llm_headers": {
            "Authorization": "Bearer fake-caller-token",
            "X-Caller-Session": "session-1",
        },
        "alibaba_cloud_access_key_id": "fake-access-key",
        "alibaba_cloud_access_key_secret": "fake-access-secret",
        "alibaba_cloud_security_token": "fake-sts-token",
        "alibaba_cloud_region_id": "cn-hangzhou",
    }


@pytest.mark.asyncio
async def test_empty_llm_headers_are_forwarded_to_clear_a2a_context_binding(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("IAC_CODE_AGUI_ALLOWED_CWDS", str(tmp_path))
    fake = FakeA2AClient()
    adapter = AguiA2AAdapter(a2a_url="http://a2a/", client=fake)
    app = create_app(adapter=adapter)
    payload = _payload(tmp_path)
    payload["forwardedProps"]["iacCode"]["llmHeaders"] = {}

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/", json=payload)

    assert response.status_code == 200
    assert fake.stream_options[0]["iac_code_metadata"]["llm_headers"] == {}


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
                json.loads(line.removeprefix("data: ")) for line in response.iter_lines() if line.startswith("data: ")
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


class ConfirmationPublicationClient(FakeA2AClient):
    """Deliver real publisher frames through the public AG-UI resume boundary."""

    def __init__(self, tmp_path, *, stale_after_resume=False, confirm_only_cancel=False, confirmation_options=None):
        super().__init__()
        self.tmp_path = tmp_path
        self.stale_after_resume = stale_after_resume
        self.confirm_only_cancel = confirm_only_cancel
        self.confirmation_options = confirmation_options
        self.current = None
        self.publisher = None
        self.queue = None
        self.phase = "candidate"
        self.old_input_id = None
        self.confirmation_input_id = None
        self.subscribe_calls = 0
        self.permission_registry = None
        self.permission_future = None
        self.pending_permission = None

    async def _publish_wait(self, kind):
        from google.protobuf.json_format import MessageToDict

        from iac_code.a2a.input_required import PermissionInputRegistry
        from iac_code.a2a.pipeline_events import PipelineA2AContext, PipelineEventTranslator
        from iac_code.a2a.pipeline_journal import A2APipelineJournal
        from iac_code.a2a.pipeline_snapshot import A2APipelineSnapshotStore
        from iac_code.a2a.pipeline_stream import PipelineA2AEventPublisher
        from iac_code.pipeline.engine.events import PipelineEvent, PipelineEventType
        from tests.a2a.fakes import FakeEventQueue

        if self.publisher is None:
            self.queue = FakeEventQueue()
            self.permission_registry = PermissionInputRegistry()
            context = PipelineA2AContext(
                pipeline_run_id="pipeline-1",
                task_id="task-1",
                context_id=self.context_id,
                pipeline_name="selling_solution_first",
                iac_code_session_id="session-1",
                parent_step_order=["solution_planning_and_selection", "materialize_selected_candidate"],
            )
            directory = self.tmp_path / "publisher"
            self.publisher = PipelineA2AEventPublisher(
                event_queue=self.queue,
                translator=PipelineEventTranslator(context),
                journal=A2APipelineJournal(directory),
                snapshot_store=A2APipelineSnapshotStore(directory),
                permission_input_registry=self.permission_registry,
            )
        options = [{"id": "plan-a", "label": "Plan A"}]
        step_id = "solution_planning_and_selection"
        if kind == "deployment_confirmation":
            options = [{"name": "Cancel the prepared plan", "action": "cancel"}]
            if not self.confirm_only_cancel:
                options = [
                    {"name": "Confirm the prepared plan", "action": "confirm"},
                    {"name": "Choose again", "action": "reselect"},
                    *options,
                ]
            if self.confirmation_options is not None:
                options = self.confirmation_options
            step_id = "materialize_selected_candidate"
        await self.publisher.publish(
            PipelineEvent(
                type=PipelineEventType.USER_INPUT_REQUIRED,
                step_id=step_id,
                timestamp=1717821600.0 + len(self.queue.events),
                data={"kind": kind, "prompt": "Choose the next action", "options": options},
            )
        )
        self.current = {"result": MessageToDict(self.queue.events[-1], preserving_proto_field_name=False)}
        projection = self.current["result"]["metadata"]["iac_code"].get("input")
        if kind == "candidate_selection":
            self.old_input_id = projection["inputId"]
        elif projection is not None:
            self.confirmation_input_id = projection["inputId"]
        return self.current

    def _prepare_permission(self, pending):
        assert pending.task_id == "task-1"
        assert pending.context_id == self.context_id
        self.pending_permission = pending

    def stream_message_parts(self, url, parts, *, context_id, **kwargs):
        if kwargs.get("task_id") is not None:
            self.sent_parts.extend(parts)
            assert parts[0]["data"]["kind"] == "permission"
            assert parts[0]["data"]["decision"] == "deny"

            async def permission_answer():
                from iac_code.a2a.input_required import PermissionResponse

                value = parts[0]["data"]
                approved = await self.permission_registry.answer(
                    PermissionResponse(
                        task_id="task-1",
                        context_id=context_id,
                        request_task_id=value["requestTaskId"],
                        input_id=value["inputId"],
                        tool_use_id=value["toolUseId"],
                        decision=value["decision"],
                    )
                )
                assert approved is False
                assert self.permission_future.result() is False
                await self.permission_registry.complete(self.pending_permission)
                self.phase = "completed"
                self.current = _event(context_id=context_id, state="TASK_STATE_COMPLETED")
                yield self.current

            return permission_answer()
        self.context_id = context_id

        async def initial():
            yield await self._publish_wait("candidate_selection")

        return initial()

    def stream_message(self, url, prompt, *, context_id, task_id=None, **kwargs):
        self.resumed_prompts.append((prompt, task_id))

        async def answer():
            if self.phase == "candidate":
                assert prompt == "Plan A"
                self.phase = "selecting"
                yield _event(context_id=context_id)
                # The accepted response stream can end at the old wait snapshot.
                yield self.current
                return
            from iac_code.pipeline.engine.ui_contract import parse_deployment_confirmation

            response = parse_deployment_confirmation(prompt)
            assert response is not None
            yield _event(context_id=context_id)
            if response.action == "confirm":
                self.phase = "permission"
                from google.protobuf.json_format import MessageToDict

                from iac_code.types.stream_events import PermissionRequestEvent

                self.permission_future = asyncio.get_running_loop().create_future()
                published = await self.publisher.publish(
                    PermissionRequestEvent(
                        tool_name="aliyun_api",
                        tool_input={"product": "ros", "action": "CreateStack"},
                        tool_use_id="deploy-1",
                        response_future=self.permission_future,
                    ),
                    prepare_detached_permission=self._prepare_permission,
                )
                assert published is self.pending_permission
                assert not self.permission_future.done()
                self.current = {"result": MessageToDict(self.queue.events[-1], preserving_proto_field_name=False)}
                yield self.current
            else:
                self.phase = "completed"
                self.current = _event(context_id=context_id, state="TASK_STATE_COMPLETED")
                yield self.current

        return answer()

    async def get_task(self, url, task_id, *, history_length=None):
        assert task_id == "task-1"
        return self.current

    def subscribe_task(self, url, task_id):
        assert task_id == "task-1"
        self.subscribe_calls += 1

        async def subscription():
            if self.phase == "selecting" and not self.stale_after_resume:
                await self._publish_wait("deployment_confirmation")
                self.phase = "confirmation"
            # Exercise the actual EOF/GetTask repair, not a fabricated terminal.
            yield _event(context_id=self.context_id)

        return subscription()


@pytest.mark.asyncio
async def test_candidate_resume_ends_at_new_confirmation_and_confirm_still_requires_permission(tmp_path, monkeypatch):
    from iac_code.agui.inputs import canonical_digest
    from iac_code.pipeline.engine.ui_contract import parse_deployment_confirmation

    monkeypatch.setenv("IAC_CODE_AGUI_ALLOWED_CWDS", str(tmp_path))
    fake = ConfirmationPublicationClient(tmp_path)
    adapter = AguiA2AAdapter(a2a_url="http://a2a/", client=fake)
    payload = _payload(tmp_path)
    payload["forwardedProps"]["iacCode"]["runMode"] = "pipeline"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(adapter=adapter)), base_url="http://test"
    ) as client:
        first = _events(await client.post("/", json=payload))
        old_id = first[-1]["outcome"]["interrupts"][0]["id"]
        binding = adapter._threads["thread-1"]
        execution_id = binding.execution_id
        select = _payload(
            tmp_path,
            run_id="select",
            resume=[
                {
                    "interruptId": old_id,
                    "status": "resolved",
                    "payload": {"selectedId": "plan-a"},
                }
            ],
        )
        select["forwardedProps"]["iacCode"]["runMode"] = "pipeline"
        selected = _events(await client.post("/", json=select))
        assert selected[-1]["type"] == "RUN_FINISHED"
        assert selected[-1]["outcome"]["type"] == "interrupt"
        new_input = selected[-1]["outcome"]["interrupts"][0]
        assert new_input["metadata"]["kind"] == "deployment_confirmation"
        assert new_input["id"] == fake.confirmation_input_id
        assert new_input["id"] != old_id
        assert set(binding.pending) == {new_input["id"]}
        assert binding.execution_id == execution_id
        assert binding.task_id == "task-1"
        assert binding.applied_resume_digests[(execution_id, old_id)] == canonical_digest(
            {"status": "resolved", "payload": {"selectedId": "plan-a"}}
        )
        assert fake.resumed_prompts == [("Plan A", "task-1")]
        assert fake.subscribe_calls == 1
        confirm = _payload(
            tmp_path,
            run_id="confirm",
            resume=[
                {
                    "interruptId": new_input["id"],
                    "status": "resolved",
                    "payload": {"action": "confirm", "parameter_overrides": {"Name": "retained"}},
                }
            ],
        )
        confirm["forwardedProps"]["iacCode"]["runMode"] = "pipeline"
        confirmation = _events(await client.post("/", json=confirm))
        assert parse_deployment_confirmation(fake.resumed_prompts[-1][0]).parameter_overrides == {"Name": "retained"}
        permission = confirmation[-1]["outcome"]["interrupts"][0]
        assert permission["metadata"]["kind"] == "permission"
        assert fake.sent_parts == []
        assert not fake.permission_future.done()
        deny = _payload(
            tmp_path,
            run_id="deny",
            resume=[
                {
                    "interruptId": permission["id"],
                    "status": "resolved",
                    "payload": {"decision": "deny"},
                }
            ],
        )
        deny["forwardedProps"]["iacCode"]["runMode"] = "pipeline"
        denied = _events(await client.post("/", json=deny))
    assert denied[-1]["outcome"] == {"type": "success"}
    assert fake.sent_parts[0]["data"]["decision"] == "deny"
    assert fake.permission_future.result() is False
    assert fake.pending_permission.state == "completed"
    assert fake.cancelled == []


@pytest.mark.asyncio
async def test_candidate_resume_cannot_treat_only_old_applied_input_as_new_boundary(tmp_path, monkeypatch):
    monkeypatch.setenv("IAC_CODE_AGUI_ALLOWED_CWDS", str(tmp_path))
    fake = ConfirmationPublicationClient(tmp_path, stale_after_resume=True)
    adapter = AguiA2AAdapter(a2a_url="http://a2a/", client=fake)
    initial = _payload(tmp_path)
    initial["forwardedProps"]["iacCode"]["runMode"] = "pipeline"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(adapter=adapter)), base_url="http://test"
    ) as client:
        first = _events(await client.post("/", json=initial))
        old_id = first[-1]["outcome"]["interrupts"][0]["id"]
        reply = _payload(
            tmp_path,
            run_id="select",
            resume=[
                {
                    "interruptId": old_id,
                    "status": "resolved",
                    "payload": {"selectedId": "plan-a"},
                }
            ],
        )
        reply["forwardedProps"]["iacCode"]["runMode"] = "pipeline"
        events = _events(await client.post("/", json=reply))
    assert events[-1]["type"] == "RUN_ERROR"
    assert events[-1]["code"] == "A2A_UNAVAILABLE"
    assert not any(event["type"] == "RUN_FINISHED" for event in events)
    assert fake.resumed_prompts == [("Plan A", "task-1")]
    assert adapter._threads["thread-1"].pending == {}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad_payload",
    [
        {"action": "confirm"},
        {"action": "unknown"},
        {"action": "cancel", "extra": "ignored by parser but rejected by wire schema"},
        {"action": "cancel", "parameter_overrides": []},
        {"selectedId": "missing"},
        {"freeText": "confirm"},
    ],
)
async def test_confirmation_invalid_wire_payload_keeps_pending_and_sends_nothing(tmp_path, monkeypatch, bad_payload):
    monkeypatch.setenv("IAC_CODE_AGUI_ALLOWED_CWDS", str(tmp_path))
    fake = ConfirmationPublicationClient(tmp_path, confirm_only_cancel=True)
    adapter = AguiA2AAdapter(a2a_url="http://a2a/", client=fake)
    initial = _payload(tmp_path)
    initial["forwardedProps"]["iacCode"]["runMode"] = "pipeline"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(adapter=adapter)), base_url="http://test"
    ) as client:
        first = _events(await client.post("/", json=initial))
        old_id = first[-1]["outcome"]["interrupts"][0]["id"]
        select = _payload(
            tmp_path,
            run_id="select",
            resume=[
                {
                    "interruptId": old_id,
                    "status": "resolved",
                    "payload": {"selectedId": "plan-a"},
                }
            ],
        )
        select["forwardedProps"]["iacCode"]["runMode"] = "pipeline"
        selected = _events(await client.post("/", json=select))
        new_id = selected[-1]["outcome"]["interrupts"][0]["id"]
        invalid = _payload(
            tmp_path,
            run_id="invalid",
            resume=[
                {
                    "interruptId": new_id,
                    "status": "resolved",
                    "payload": bad_payload,
                }
            ],
        )
        invalid["forwardedProps"]["iacCode"]["runMode"] = "pipeline"
        events = _events(await client.post("/", json=invalid))
    assert events[-1]["code"] == "RESUME_PAYLOAD_INVALID"
    assert fake.resumed_prompts == [("Plan A", "task-1")]
    assert set(adapter._threads["thread-1"].pending) == {new_id}
    assert fake.sent_parts == []


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["confirm", "cancel", "reselect"])
@pytest.mark.parametrize("response_kind", ["selectedId", "action"])
async def test_public_confirmation_resume_sends_action_not_localized_label(
    tmp_path, monkeypatch, action, response_kind
):
    from iac_code.pipeline.engine.ui_contract import parse_deployment_confirmation

    monkeypatch.setenv("IAC_CODE_AGUI_ALLOWED_CWDS", str(tmp_path))
    fake = ConfirmationPublicationClient(tmp_path)
    adapter = AguiA2AAdapter(a2a_url="http://a2a/", client=fake)
    initial = _payload(tmp_path)
    initial["forwardedProps"]["iacCode"]["runMode"] = "pipeline"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(adapter=adapter)), base_url="http://test"
    ) as client:
        first = _events(await client.post("/", json=initial))
        old_id = first[-1]["outcome"]["interrupts"][0]["id"]
        select = _payload(
            tmp_path,
            run_id="select",
            resume=[
                {
                    "interruptId": old_id,
                    "status": "resolved",
                    "payload": {"selectedId": "plan-a"},
                }
            ],
        )
        select["forwardedProps"]["iacCode"]["runMode"] = "pipeline"
        selected = _events(await client.post("/", json=select))
        interrupt = selected[-1]["outcome"]["interrupts"][0]
        selected_id = next(option["id"] for option in interrupt["metadata"]["options"] if option["action"] == action)
        answer = {
            response_kind: selected_id if response_kind == "selectedId" else action,
            "parameter_overrides": {"Name": "retained"},
        }
        resume = _payload(
            tmp_path,
            run_id="confirmation",
            resume=[
                {
                    "interruptId": interrupt["id"],
                    "status": "resolved",
                    "payload": answer,
                }
            ],
        )
        resume["forwardedProps"]["iacCode"]["runMode"] = "pipeline"
        events = _events(await client.post("/", json=resume))
    reply = parse_deployment_confirmation(fake.resumed_prompts[-1][0])
    assert reply is not None
    assert reply.action == action
    assert reply.parameter_overrides == {"Name": "retained"}
    assert len(fake.resumed_prompts) == 2
    assert fake.sent_parts == []
    assert fake.cancelled == []
    if action == "confirm":
        assert events[-1]["outcome"]["type"] == "interrupt"
        assert events[-1]["outcome"]["interrupts"][0]["metadata"]["kind"] == "permission"
    else:
        assert events[-1]["outcome"] == {"type": "success"}


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", [False, True])
async def test_old_candidate_response_replay_cannot_consume_confirmation(tmp_path, monkeypatch, changed):
    monkeypatch.setenv("IAC_CODE_AGUI_ALLOWED_CWDS", str(tmp_path))
    fake = ConfirmationPublicationClient(tmp_path)
    adapter = AguiA2AAdapter(a2a_url="http://a2a/", client=fake)
    initial = _payload(tmp_path)
    initial["forwardedProps"]["iacCode"]["runMode"] = "pipeline"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(adapter=adapter)), base_url="http://test"
    ) as client:
        first = _events(await client.post("/", json=initial))
        old_id = first[-1]["outcome"]["interrupts"][0]["id"]
        select = _payload(
            tmp_path,
            run_id="select",
            resume=[
                {
                    "interruptId": old_id,
                    "status": "resolved",
                    "payload": {"selectedId": "plan-a"},
                }
            ],
        )
        select["forwardedProps"]["iacCode"]["runMode"] = "pipeline"
        selected = _events(await client.post("/", json=select))
        new_id = selected[-1]["outcome"]["interrupts"][0]["id"]
        binding = adapter._threads["thread-1"]
        expected_execution = binding.execution_id
        old_proofs = dict(binding.applied_resume_digests)
        replay = _payload(
            tmp_path,
            run_id="replay",
            resume=[
                {
                    "interruptId": old_id,
                    "status": "resolved",
                    "payload": {"selectedId": "plan-b" if changed else "plan-a"},
                }
            ],
        )
        replay["forwardedProps"]["iacCode"]["runMode"] = "pipeline"
        events = _events(await client.post("/", json=replay))
    assert fake.resumed_prompts == [("Plan A", "task-1")]
    binding = adapter._threads["thread-1"]
    assert set(binding.pending) == {new_id}
    assert binding.execution_id == expected_execution
    assert binding.task_id == "task-1"
    assert binding.applied_resume_digests == old_proofs
    restored = AguiA2AAdapter(a2a_url="http://a2a/", client=fake)._load_thread("thread-1")
    assert restored is not None
    assert set(restored.pending) == {new_id}
    assert restored.execution_id == expected_execution
    assert restored.task_id == "task-1"
    assert restored.applied_resume_digests == old_proofs
    assert events[-1]["type"] == "RUN_ERROR"
    if changed:
        assert events[-1]["code"] == "RESUME_ALREADY_APPLIED"
    else:
        # The request omits the still-known confirmation, even though its old
        # candidate response is an exact idempotent replay.
        assert events[-1]["code"] == "INCOMPLETE_RESUME"


@pytest.mark.asyncio
@pytest.mark.parametrize("ids", [[""], ["same", "same"], ["x" * 200 + "a", "x" * 200 + "b"]])
async def test_public_confirmation_corrupt_option_identity_never_becomes_a_wait_boundary(tmp_path, monkeypatch, ids):
    monkeypatch.setenv("IAC_CODE_AGUI_ALLOWED_CWDS", str(tmp_path))
    fake = ConfirmationPublicationClient(
        tmp_path, confirmation_options=[{"id": value, "name": "Cancel", "action": "cancel"} for value in ids]
    )
    adapter = AguiA2AAdapter(a2a_url="http://a2a/", client=fake)
    initial = _payload(tmp_path)
    initial["forwardedProps"]["iacCode"]["runMode"] = "pipeline"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(adapter=adapter)), base_url="http://test"
    ) as client:
        first = _events(await client.post("/", json=initial))
        old_id = first[-1]["outcome"]["interrupts"][0]["id"]
        select = _payload(
            tmp_path,
            run_id="select",
            resume=[
                {
                    "interruptId": old_id,
                    "status": "resolved",
                    "payload": {"selectedId": "plan-a"},
                }
            ],
        )
        select["forwardedProps"]["iacCode"]["runMode"] = "pipeline"
        events = _events(await client.post("/", json=select))
    assert events[-1]["type"] == "RUN_ERROR"
    assert events[-1]["code"] == "A2A_UNAVAILABLE"
    assert fake.resumed_prompts == [("Plan A", "task-1")]
    assert fake.confirmation_input_id is None
    assert fake.sent_parts == []


@pytest.mark.asyncio
async def test_confirmation_restart_preserves_action_binding_and_resumes_exact_task_once(tmp_path, monkeypatch):
    from iac_code.pipeline.engine.ui_contract import parse_deployment_confirmation

    monkeypatch.setenv("IAC_CODE_AGUI_ALLOWED_CWDS", str(tmp_path))
    fake = ConfirmationPublicationClient(tmp_path)
    first_adapter = AguiA2AAdapter(a2a_url="http://a2a/", client=fake)
    initial = _payload(tmp_path)
    initial["forwardedProps"]["iacCode"]["runMode"] = "pipeline"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(adapter=first_adapter)), base_url="http://test"
    ) as client:
        first = _events(await client.post("/", json=initial))
        old_id = first[-1]["outcome"]["interrupts"][0]["id"]
        select = _payload(
            tmp_path,
            run_id="select",
            resume=[
                {
                    "interruptId": old_id,
                    "status": "resolved",
                    "payload": {"selectedId": "plan-a"},
                }
            ],
        )
        select["forwardedProps"]["iacCode"]["runMode"] = "pipeline"
        selected = _events(await client.post("/", json=select))
        interrupt = selected[-1]["outcome"]["interrupts"][0]
    expected_execution = first_adapter._threads["thread-1"].execution_id
    cancel_id = next(o["id"] for o in interrupt["metadata"]["options"] if o["action"] == "cancel")
    second_adapter = AguiA2AAdapter(a2a_url="http://a2a/", client=fake)
    resume = _payload(
        tmp_path,
        run_id="after-restart",
        resume=[
            {
                "interruptId": interrupt["id"],
                "status": "resolved",
                "payload": {"selectedId": cancel_id},
            }
        ],
    )
    resume["forwardedProps"]["iacCode"]["runMode"] = "pipeline"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(adapter=second_adapter)), base_url="http://test"
    ) as client:
        events = _events(await client.post("/", json=resume))
    assert events[-1]["outcome"] == {"type": "success"}
    assert second_adapter._threads["thread-1"].execution_id == expected_execution
    assert fake.resumed_prompts[-1][1] == "task-1"
    session = [
        event["value"] for event in events if event["type"] == "CUSTOM" and event["name"] == "iac-code.session.v1"
    ]
    assert len(session) == 1
    assert session[0]["taskId"] == "task-1"
    assert session[0]["executionId"] == expected_execution
    binding = second_adapter._threads["thread-1"]
    assert binding.task_id is None
    assert binding.pending == {}
    assert expected_execution in binding.terminal_execution_ids
    restored = AguiA2AAdapter(a2a_url="http://a2a/", client=fake)._load_thread("thread-1")
    assert restored is not None
    assert restored.execution_id == expected_execution
    assert restored.task_id is None
    assert restored.pending == {}
    assert expected_execution in restored.terminal_execution_ids
    assert restored.applied_resume_digests == binding.applied_resume_digests
    assert parse_deployment_confirmation(fake.resumed_prompts[-1][0]).action == "cancel"
    assert len(fake.resumed_prompts) == 2
    assert fake.sent_parts == []
