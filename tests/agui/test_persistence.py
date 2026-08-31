from __future__ import annotations

import asyncio
import json
import os
import stat
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
import pytest
from ag_ui.core import EventType

from iac_code.agui.adapter import AguiA2AAdapter, _persistent_input
from iac_code.agui.app import create_app
from iac_code.agui.inputs import canonical_digest, parse_run_input
from iac_code.agui.state import AguiStateStoreError, FileAguiThreadStateStore
from tests.agui.test_app import FakeA2AClient, _event, _events, _payload


class SnapshotA2AClient(FakeA2AClient):
    def __init__(self, inputs: list[dict[str, Any]]) -> None:
        super().__init__(interrupt=True)
        self.inputs = inputs
        self.pipeline_state: dict[str, Any] | None = None
        self.pipeline_after_sequences: list[int | None] = []

    async def get_task(self, _url, _task_id, *, history_length=None):
        del history_length
        event = _event(context_id=self.context_id, state="TASK_STATE_WORKING")
        event["result"]["metadata"] = {"iac_code": {"pendingPermissions": self.inputs}}
        return event

    async def get_pipeline_state(self, _url, *, task_id, after_sequence=None):
        del task_id
        self.pipeline_after_sequences.append(after_sequence)
        return self.pipeline_state


class FailNthSaveStore:
    def __init__(self, fail_at: int) -> None:
        self.fail_at = fail_at
        self.calls = 0
        self.load_calls: list[str] = []
        self.values: dict[str, dict[str, Any]] = {}

    def load_thread(self, thread_id: str) -> dict[str, Any] | None:
        self.load_calls.append(thread_id)
        return self.values.get(thread_id)

    def save_thread(self, thread_id: str, state) -> None:
        self.calls += 1
        if self.calls == self.fail_at:
            raise AguiStateStoreError("injected state failure")
        self.values[thread_id] = dict(state)


def _thread_state_path(state_dir: Path, thread_id: str = "thread-1") -> Path:
    return state_dir / "threads" / f"{thread_id}.json"


def _permission(context_id: str, input_id: str, tool_id: str) -> dict[str, Any]:
    return {
        "schemaVersion": 1,
        "kind": "permission",
        "requestTaskId": "task-1",
        "contextId": context_id,
        "inputId": input_id,
        "toolUseId": tool_id,
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


def test_persistent_permission_keeps_scope_and_safe_display_details() -> None:
    value = _permission("ctx-1", "permission-1", "tool-1")
    value.update(
        {
            "scope": "candidate",
            "subPipelineId": "candidate-a",
            "operation": {
                "product": "vpc",
                "action": "CreateVpc",
                "region": "cn-hangzhou",
                "apiCalls": [{"product": "VPC", "action": "CreateVpc", "effect": "change"}],
            },
            "displayParameters": {
                "format": "json",
                "value": {"CidrBlock": "10.0.0.0/16", "Password": {"redacted": True}},
            },
        }
    )

    assert _persistent_input(value) == value


@pytest.mark.parametrize(
    ("scope", "metadata_key", "state"),
    [
        ("normal", "input", "TASK_STATE_INPUT_REQUIRED"),
        ("top-pipeline", "input", "TASK_STATE_INPUT_REQUIRED"),
        ("sub-pipeline", "pendingPermissions", "TASK_STATE_WORKING"),
    ],
)
@pytest.mark.asyncio
async def test_permission_interrupt_shapes_close_without_cancel(
    tmp_path,
    scope: str,
    metadata_key: str,
    state: str,
) -> None:
    class ShapeClient(FakeA2AClient):
        def stream_message_parts(self, _url, _parts, *, context_id, **kwargs):
            del kwargs
            self.context_id = context_id

            async def events():
                yield _event(context_id=context_id)
                event = _event(context_id=context_id, state=state)
                value = _permission(context_id, f"permission-{scope}", f"tool-{scope}")
                event["result"]["metadata"] = {
                    "iac_code": {metadata_key: [value] if metadata_key == "pendingPermissions" else value}
                }
                yield event

            return events()

    fake = ShapeClient()
    adapter = AguiA2AAdapter(a2a_url="http://a2a/", client=fake, state_dir=tmp_path / f"state-{scope}")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(adapter=adapter)), base_url="http://test"
    ) as client:
        response = await client.post("/", json=_payload(tmp_path))

    assert _events(response)[-1]["outcome"]["type"] == "interrupt"
    assert fake.cancelled == []


@pytest.fixture(autouse=True)
def _allowed_cwd(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("IAC_CODE_AGUI_ALLOWED_CWDS", str(tmp_path))


@pytest.mark.asyncio
async def test_interrupt_is_durable_before_terminal_event_and_disconnect_does_not_cancel(tmp_path) -> None:
    fake = FakeA2AClient(interrupt=True)
    adapter = AguiA2AAdapter(a2a_url="http://a2a/", client=fake, state_dir=tmp_path / "state")
    payload = _payload(tmp_path)
    ticket = await adapter.admit(parse_run_input(payload), canonical_digest(payload))
    stream = adapter.stream(ticket)

    terminal = None
    async for event in stream:
        if event.type == EventType.RUN_FINISHED:
            terminal = event
            break

    assert terminal is not None
    assert ticket.completed is True
    assert ticket.paused is True
    state = json.loads(_thread_state_path(tmp_path / "state").read_text(encoding="utf-8"))
    assert list(state["execution"]["pending"]) == ["permission-1"]
    await adapter.disconnect(ticket)
    assert fake.cancelled == []
    await stream.aclose()
    await adapter.aclose()


@pytest.mark.asyncio
async def test_disconnect_before_interrupt_is_durable_cancels_the_a2a_task(tmp_path) -> None:
    fake = FakeA2AClient()
    adapter = AguiA2AAdapter(a2a_url="http://a2a/", client=fake, state_dir=tmp_path / "state")
    payload = _payload(tmp_path)
    ticket = await adapter.admit(parse_run_input(payload), canonical_digest(payload))
    stream = adapter.stream(ticket)

    assert (await anext(stream)).type == EventType.RUN_STARTED
    session_event = await anext(stream)
    assert session_event.name == "iac-code.session.v1"
    assert ticket.binding.task_id == "task-1"

    await adapter.disconnect(ticket)

    assert fake.cancelled == ["task-1"]
    assert ticket.binding.task_id is None
    await stream.aclose()
    await adapter.aclose()


@pytest.mark.asyncio
async def test_pipeline_guidance_preserves_active_execution_across_adapter_restart(
    tmp_path,
) -> None:
    state_dir = tmp_path / "state"
    fake = FakeA2AClient()
    adapter = AguiA2AAdapter(
        a2a_url="http://a2a/",
        client=fake,
        state_dir=state_dir,
    )
    initial_payload = _payload(tmp_path)
    initial_payload["forwardedProps"]["iacCode"]["runMode"] = "pipeline"
    initial_ticket = await adapter.admit(parse_run_input(initial_payload), canonical_digest(initial_payload))
    initial_ticket.binding.task_id = "task-1"
    initial_ticket.binding.iac_code_session_id = "session-1"
    adapter._persist_thread(initial_ticket.binding)

    guidance_payload = _payload(tmp_path, run_id="run-guidance-1")
    guidance_props = guidance_payload["forwardedProps"]["iacCode"]
    guidance_props.update(
        {
            "rosInvocationId": "invocation-guidance-1",
            "runMode": "pipeline",
            "activeGuidance": True,
        }
    )
    guidance_ticket = await adapter.admit(parse_run_input(guidance_payload), canonical_digest(guidance_payload))
    guidance_events = [event async for event in adapter.stream(guidance_ticket)]

    assert [event.type for event in guidance_events] == [
        EventType.RUN_STARTED,
        EventType.RUN_FINISHED,
    ]
    assert guidance_ticket.is_guidance is True
    assert initial_ticket.binding.active_run_id == "run-1"
    assert initial_ticket.binding.task_id == "task-1"
    assert fake.stream_options[-1]["task_id"] == "task-1"
    assert fake.stream_options[-1]["iac_code_metadata"]["rosInvocationId"] == ("invocation-guidance-1")
    assert fake.sent_parts == [{"text": "hello"}]
    assert fake.cancelled == []
    await adapter.aclose()

    restarted = AguiA2AAdapter(
        a2a_url="http://a2a/",
        client=fake,
        state_dir=state_dir,
    )
    guidance_after_restart = _payload(tmp_path, run_id="run-guidance-2")
    restarted_props = guidance_after_restart["forwardedProps"]["iacCode"]
    restarted_props.update(
        {
            "rosInvocationId": "invocation-guidance-2",
            "runMode": "pipeline",
            "activeGuidance": True,
        }
    )
    restarted_ticket = await restarted.admit(
        parse_run_input(guidance_after_restart),
        canonical_digest(guidance_after_restart),
    )

    assert restarted_ticket.binding.active_run_id is None
    assert restarted_ticket.binding.task_id == "task-1"
    restarted_events = [event async for event in restarted.stream(restarted_ticket)]
    assert [event.type for event in restarted_events] == [
        EventType.RUN_STARTED,
        EventType.RUN_FINISHED,
    ]
    assert restarted_ticket.binding.task_id == "task-1"
    assert fake.cancelled == []
    await restarted.aclose()


@pytest.mark.asyncio
async def test_restart_restores_interrupt_resume_and_cancel_identity(tmp_path) -> None:
    state_dir = tmp_path / "state"
    fake = FakeA2AClient(interrupt=True)
    first_adapter = AguiA2AAdapter(a2a_url="http://a2a/", client=fake, state_dir=state_dir)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(adapter=first_adapter)), base_url="http://test"
    ) as client:
        first = await client.post("/", json=_payload(tmp_path))
    session = next(event for event in _events(first) if event.get("name") == "iac-code.session.v1")
    assert session["value"]["sessionId"] == "session-1"
    fake.context_id = session["value"]["contextId"]
    await first_adapter.aclose()
    assert fake.cancelled == []
    fake.resume_preflight_calls.clear()

    second_adapter = AguiA2AAdapter(a2a_url="http://a2a/", client=fake, state_dir=state_dir)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(adapter=second_adapter)), base_url="http://test"
    ) as client:
        resumed = await client.post(
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
    assert _events(resumed)[-1]["outcome"] == {"type": "success"}
    assert fake.restored_sessions[-1] == (str(tmp_path), "session-1")
    assert fake.resume_preflight_calls[:3] == [
        "ensure_session_restored",
        "get_task",
        "get_pipeline_state",
    ]
    resumed_session = next(event for event in _events(resumed) if event.get("name") == "iac-code.session.v1")
    assert resumed_session["value"]["sessionId"] == "session-1"
    assert fake.sent_parts[-1]["data"]["decision"] == "allow_once"
    assert session["value"]["executionId"] in second_adapter._threads["thread-1"].terminal_execution_ids
    assert (
        await second_adapter.cancel(
            session["value"]["executionId"],
            thread_id="thread-1",
            ros_invocation_id="invocation-1",
        )
        == "already_terminal"
    )


@pytest.mark.parametrize(
    ("scope", "metadata_key", "snapshot_state"),
    [
        ("normal", "input", "TASK_STATE_INPUT_REQUIRED"),
        ("top-pipeline", "input", "TASK_STATE_INPUT_REQUIRED"),
        ("sub-pipeline", "pendingPermissions", "TASK_STATE_WORKING"),
    ],
)
@pytest.mark.asyncio
async def test_a2a_restart_summary_without_input_projection_preserves_durable_interrupt(
    tmp_path,
    scope: str,
    metadata_key: str,
    snapshot_state: str,
) -> None:
    class InitialPermissionClient(FakeA2AClient):
        def stream_message_parts(self, _url, _parts, *, context_id, **kwargs):
            assert kwargs.get("task_id") is None

            async def events():
                yield _event(context_id=context_id)
                event = _event(context_id=context_id, state=snapshot_state)
                value = _permission(context_id, f"permission-{scope}", f"tool-{scope}")
                event["result"]["metadata"] = {
                    "iac_code": {metadata_key: [value] if metadata_key == "pendingPermissions" else value}
                }
                yield event

            return events()

    class RestartedSummaryClient(FakeA2AClient):
        async def get_task(self, _url, _task_id, *, history_length=None):
            del history_length
            self.resume_preflight_calls.append("get_task")
            event = _event(context_id=self.context_id, state=snapshot_state)
            event["result"].pop("metadata")
            return event

    state_dir = tmp_path / f"state-{scope}"
    first_adapter = AguiA2AAdapter(
        a2a_url="http://a2a/",
        client=InitialPermissionClient(),
        state_dir=state_dir,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(adapter=first_adapter)), base_url="http://test"
    ) as client:
        first = await client.post("/", json=_payload(tmp_path))
    session = next(event["value"] for event in _events(first) if event.get("name") == "iac-code.session.v1")
    await first_adapter.aclose()

    restarted_a2a = RestartedSummaryClient()
    restarted_a2a.context_id = session["contextId"]
    second_adapter = AguiA2AAdapter(a2a_url="http://a2a/", client=restarted_a2a, state_dir=state_dir)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(adapter=second_adapter)), base_url="http://test"
    ) as client:
        resumed = await client.post(
            "/",
            json=_payload(
                tmp_path,
                run_id="run-2",
                resume=[
                    {
                        "interruptId": f"permission-{scope}",
                        "status": "resolved",
                        "payload": {"decision": "allow_once"},
                    }
                ],
            ),
        )

    assert _events(resumed)[-1]["outcome"] == {"type": "success"}
    assert restarted_a2a.resume_preflight_calls[:3] == [
        "ensure_session_restored",
        "get_task",
        "get_pipeline_state",
    ]
    assert restarted_a2a.sent_parts == [
        {
            "data": {
                "schemaVersion": 1,
                "kind": "permission",
                "requestTaskId": "task-1",
                "inputId": f"permission-{scope}",
                "toolUseId": f"tool-{scope}",
                "decision": "allow_once",
            },
            "mediaType": "application/json",
        }
    ]
    await second_adapter.aclose()


@pytest.mark.asyncio
async def test_explicit_empty_input_projection_remains_authoritative_after_restart(tmp_path) -> None:
    state_dir = tmp_path / "state"
    first_fake = FakeA2AClient(interrupt=True)
    first_adapter = AguiA2AAdapter(a2a_url="http://a2a/", client=first_fake, state_dir=state_dir)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(adapter=first_adapter)), base_url="http://test"
    ) as client:
        first = await client.post("/", json=_payload(tmp_path))
    session = next(event["value"] for event in _events(first) if event.get("name") == "iac-code.session.v1")
    await first_adapter.aclose()

    snapshot = SnapshotA2AClient([])
    snapshot.context_id = session["contextId"]
    second_adapter = AguiA2AAdapter(a2a_url="http://a2a/", client=snapshot, state_dir=state_dir)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(adapter=second_adapter)), base_url="http://test"
    ) as client:
        resumed = await client.post(
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

    assert _events(resumed)[-1]["code"] == "UNKNOWN_INTERRUPT"
    assert snapshot.sent_parts == []
    await second_adapter.aclose()


@pytest.mark.asyncio
async def test_resume_stops_before_get_task_when_session_backup_is_missing(tmp_path) -> None:
    state_dir = tmp_path / "state"
    fake = FakeA2AClient(interrupt=True)
    first_adapter = AguiA2AAdapter(a2a_url="http://a2a/", client=fake, state_dir=state_dir)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(adapter=first_adapter)), base_url="http://test"
    ) as client:
        first = await client.post("/", json=_payload(tmp_path))
    session = next(event for event in _events(first) if event.get("name") == "iac-code.session.v1")
    fake.context_id = session["value"]["contextId"]
    await first_adapter.aclose()
    fake.session_available = False
    fake.resume_preflight_calls.clear()

    second_adapter = AguiA2AAdapter(a2a_url="http://a2a/", client=fake, state_dir=state_dir)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(adapter=second_adapter)), base_url="http://test"
    ) as client:
        resumed = await client.post(
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

    terminal = _events(resumed)[-1]
    assert terminal["type"] == "RUN_ERROR"
    assert terminal["code"] == "EXECUTION_LOST"
    assert terminal["message"] == "The iac-code session to resume is unavailable."
    assert fake.resume_preflight_calls == ["ensure_session_restored"]
    assert fake.sent_parts == []


@pytest.mark.asyncio
async def test_restart_restores_explicit_cancel_route(tmp_path) -> None:
    state_dir = tmp_path / "state"
    fake = FakeA2AClient(interrupt=True)
    first_adapter = AguiA2AAdapter(a2a_url="http://a2a/", client=fake, state_dir=state_dir)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(adapter=first_adapter)), base_url="http://test"
    ) as client:
        first = await client.post("/", json=_payload(tmp_path))
    session = next(event["value"] for event in _events(first) if event.get("name") == "iac-code.session.v1")
    await first_adapter.aclose()

    second_adapter = AguiA2AAdapter(a2a_url="http://a2a/", client=fake, state_dir=state_dir)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(adapter=second_adapter)), base_url="http://test"
    ) as client:
        response = await client.post(
            f"/extensions/iac-code/v1/executions/{session['executionId']}/cancel",
            json={"threadId": "thread-1", "rosInvocationId": "invocation-1"},
        )

    assert response.status_code == 200
    assert response.json()["status"] == "cancelled"
    assert fake.cancelled == ["task-1"]


@pytest.mark.asyncio
async def test_restart_allows_ordinary_new_turn_with_same_context(tmp_path) -> None:
    state_dir = tmp_path / "state"
    fake = FakeA2AClient()
    first_adapter = AguiA2AAdapter(a2a_url="http://a2a/", client=fake, state_dir=state_dir)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(adapter=first_adapter)), base_url="http://test"
    ) as client:
        first = await client.post("/", json=_payload(tmp_path))
    await first_adapter.aclose()

    second_adapter = AguiA2AAdapter(a2a_url="http://a2a/", client=fake, state_dir=state_dir)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(adapter=second_adapter)), base_url="http://test"
    ) as client:
        replayed = await client.post("/", json=_payload(tmp_path))
        second = await client.post("/", json=_payload(tmp_path, run_id="run-2"))

    assert replayed.status_code == 409
    assert replayed.json()["error"]["code"] == "DUPLICATE_RUN_ID"
    first_session = next(event for event in _events(first) if event.get("name") == "iac-code.session.v1")
    second_session = next(event for event in _events(second) if event.get("name") == "iac-code.session.v1")
    assert first_session["value"]["contextId"] == second_session["value"]["contextId"]
    assert first_session["value"]["executionId"] != second_session["value"]["executionId"]


@pytest.mark.asyncio
async def test_resume_reconciles_multiple_current_permissions_before_applying_any_response(tmp_path) -> None:
    state_dir = tmp_path / "state"
    fake = SnapshotA2AClient([])
    first_adapter = AguiA2AAdapter(a2a_url="http://a2a/", client=fake, state_dir=state_dir)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(adapter=first_adapter)), base_url="http://test"
    ) as client:
        first = await client.post("/", json=_payload(tmp_path))
    session = next(event for event in _events(first) if event.get("name") == "iac-code.session.v1")
    fake.context_id = session["value"]["contextId"]
    fake.inputs = [
        _permission(fake.context_id, "permission-1", "tool-1"),
        _permission(fake.context_id, "permission-2", "tool-2"),
    ]
    await first_adapter.aclose()

    second_adapter = AguiA2AAdapter(a2a_url="http://a2a/", client=fake, state_dir=state_dir)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(adapter=second_adapter)), base_url="http://test"
    ) as client:
        response = await client.post(
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
    interrupt = _events(response)[-1]["outcome"]
    assert interrupt["type"] == "interrupt"
    assert {item["id"] for item in interrupt["interrupts"]} == {"permission-1", "permission-2"}
    assert fake.sent_parts == []


@pytest.mark.asyncio
async def test_restart_retry_does_not_reapply_a_permission_accepted_before_partial_failure(tmp_path) -> None:
    class PartialFailureClient(SnapshotA2AClient):
        def __init__(self) -> None:
            super().__init__([])
            self.fail_permission_2 = True

        async def send_message_parts(self, url, parts, **kwargs):
            del url, kwargs
            input_id = parts[0]["data"]["inputId"]
            if input_id == "permission-2" and self.fail_permission_2:
                raise RuntimeError("injected second response failure")
            self.sent_parts.extend(parts)
            return object()

    state_dir = tmp_path / "state"
    fake = PartialFailureClient()
    first_adapter = AguiA2AAdapter(a2a_url="http://a2a/", client=fake, state_dir=state_dir)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(adapter=first_adapter)), base_url="http://test"
    ) as client:
        first = await client.post("/", json=_payload(tmp_path))
        session = next(event["value"] for event in _events(first) if event.get("name") == "iac-code.session.v1")
        fake.context_id = session["contextId"]
        fake.inputs = [
            _permission(fake.context_id, "permission-1", "tool-1"),
            _permission(fake.context_id, "permission-2", "tool-2"),
        ]
        discovered = await client.post(
            "/",
            json=_payload(
                tmp_path,
                run_id="run-2",
                resume=[{"interruptId": "permission-1", "status": "resolved", "payload": {"decision": "deny"}}],
            ),
        )
        failed = await client.post(
            "/",
            json=_payload(
                tmp_path,
                run_id="run-3",
                resume=[
                    {"interruptId": "permission-1", "status": "resolved", "payload": {"decision": "deny"}},
                    {"interruptId": "permission-2", "status": "resolved", "payload": {"decision": "deny"}},
                ],
            ),
        )

    discovered_events = _events(discovered)
    assert {item["id"] for item in discovered_events[-1]["outcome"]["interrupts"]} == {
        "permission-1",
        "permission-2",
    }
    assert all(event.get("name") != "iac-code.session.v1" for event in discovered_events)
    failed_events = _events(failed)
    assert failed_events[-1]["code"] == "A2A_UNAVAILABLE"
    assert all(event.get("name") != "iac-code.session.v1" for event in failed_events)
    assert set(first_adapter._threads["thread-1"].pending) == {"permission-2"}
    assert [part["data"]["inputId"] for part in fake.sent_parts] == ["permission-1"]
    await first_adapter.aclose()

    fake.fail_permission_2 = False
    second_adapter = AguiA2AAdapter(a2a_url="http://a2a/", client=fake, state_dir=state_dir)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(adapter=second_adapter)), base_url="http://test"
    ) as client:
        retried = await client.post(
            "/",
            json=_payload(
                tmp_path,
                run_id="run-4",
                resume=[
                    {"interruptId": "permission-1", "status": "resolved", "payload": {"decision": "deny"}},
                    {"interruptId": "permission-2", "status": "resolved", "payload": {"decision": "deny"}},
                ],
            ),
        )

    assert _events(retried)[-1]["outcome"] == {"type": "success"}
    assert any(event.get("name") == "iac-code.session.v1" for event in _events(retried))
    assert [part["data"]["inputId"] for part in fake.sent_parts] == ["permission-1", "permission-2"]


@pytest.mark.asyncio
async def test_cancelled_permission_resume_reschedules_interrupt_expiry(tmp_path) -> None:
    class BlockingResumeClient(FakeA2AClient):
        def stream_message_parts(self, url, parts, **kwargs):
            if kwargs.get("task_id") is None:
                return super().stream_message_parts(url, parts, **kwargs)

            async def events():
                await asyncio.Event().wait()
                yield  # pragma: no cover - keeps this an async generator

            return events()

    fake = BlockingResumeClient(interrupt=True)
    adapter = AguiA2AAdapter(a2a_url="http://a2a/", client=fake, state_dir=tmp_path / "state")
    initial_payload = _payload(tmp_path)
    initial_ticket = await adapter.admit(parse_run_input(initial_payload), canonical_digest(initial_payload))
    initial_stream = adapter.stream(initial_ticket)
    async for event in initial_stream:
        if event.type == EventType.RUN_FINISHED:
            break
    await initial_stream.aclose()
    fake.context_id = initial_ticket.binding.context_id

    resume_payload = _payload(
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
    resume_ticket = await adapter.admit(parse_run_input(resume_payload), canonical_digest(resume_payload))
    stream = adapter.stream(resume_ticket)
    assert (await anext(stream)).type == EventType.RUN_STARTED

    blocked_read = asyncio.create_task(anext(stream))
    await asyncio.sleep(0.01)
    blocked_read.cancel()
    with pytest.raises(asyncio.CancelledError):
        await blocked_read

    assert resume_ticket.binding.pending
    assert resume_ticket.binding.expiry_task is not None
    assert not resume_ticket.binding.expiry_task.done()
    await stream.aclose()
    await adapter.aclose()


@pytest.mark.asyncio
async def test_resume_uses_persisted_pipeline_cursor_instead_of_get_task_suffix(tmp_path) -> None:
    class PipelineBatchClient(SnapshotA2AClient):
        async def get_task(self, url, task_id, *, history_length=None):
            event = await super().get_task(url, task_id, history_length=history_length)
            event["result"]["metadata"]["iac_code"]["pipelineBatch"] = {
                "events": [
                    {
                        "eventId": "step-7",
                        "eventType": "candidate_step_completed",
                        "sequence": 7,
                        "candidateStep": {"id": "candidate-b"},
                    }
                ]
            }
            return event

    state_dir = tmp_path / "state"
    fake = PipelineBatchClient([])
    first_adapter = AguiA2AAdapter(a2a_url="http://a2a/", client=fake, state_dir=state_dir)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(adapter=first_adapter)), base_url="http://test"
    ) as client:
        first = await client.post("/", json=_payload(tmp_path))
    session = next(event["value"] for event in _events(first) if event.get("name") == "iac-code.session.v1")
    fake.context_id = session["contextId"]
    fake.inputs = [_permission(fake.context_id, "permission-1", "tool-1")]
    fake.pipeline_state = {
        "snapshot": {"schemaVersion": "1.0", "pipelineRunId": "pipeline-1", "lastSequence": 7},
        "events": [],
    }

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(adapter=first_adapter)), base_url="http://test"
    ) as client:
        response = await client.post(
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

    response_events = _events(response)
    assert fake.pipeline_after_sequences[-2:] == [0, 7]
    assert not any(event.get("type") == "STEP_FINISHED" for event in response_events)


@pytest.mark.asyncio
async def test_resume_rejects_incomplete_duplicate_wrong_owner_and_repeated_payloads(tmp_path) -> None:
    state_dir = tmp_path / "state"
    fake = FakeA2AClient(interrupt=True)
    adapter = AguiA2AAdapter(a2a_url="http://a2a/", client=fake, state_dir=state_dir)
    app = create_app(adapter=adapter)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        first = await client.post("/", json=_payload(tmp_path))
        session = next(event["value"] for event in _events(first) if event.get("name") == "iac-code.session.v1")
        fake.context_id = session["contextId"]
        incomplete = await client.post("/", json=_payload(tmp_path, run_id="run-2", resume=[]))
        duplicate = await client.post(
            "/",
            json=_payload(
                tmp_path,
                run_id="run-3",
                resume=[
                    {"interruptId": "permission-1", "status": "resolved", "payload": {"decision": "deny"}},
                    {"interruptId": "permission-1", "status": "resolved", "payload": {"decision": "deny"}},
                ],
            ),
        )
        wrong_owner_payload = _payload(
            tmp_path,
            run_id="run-4",
            resume=[{"interruptId": "permission-1", "status": "resolved", "payload": {"decision": "deny"}}],
        )
        wrong_owner_payload["forwardedProps"]["iacCode"]["rosInvocationId"] = "other-invocation"
        wrong_owner = await client.post("/", json=wrong_owner_payload)
        applied = await client.post(
            "/",
            json=_payload(
                tmp_path,
                run_id="run-5",
                resume=[
                    {
                        "interruptId": "permission-1",
                        "status": "resolved",
                        "payload": {"decision": "allow_once"},
                    }
                ],
            ),
        )
        repeated = await client.post(
            "/",
            json=_payload(
                tmp_path,
                run_id="run-6",
                resume=[
                    {
                        "interruptId": "permission-1",
                        "status": "resolved",
                        "payload": {"decision": "allow_once"},
                    }
                ],
            ),
        )

    assert _events(incomplete)[-1]["code"] == "INCOMPLETE_RESUME"
    assert _events(duplicate)[-1]["code"] == "INCOMPLETE_RESUME"
    assert wrong_owner.status_code == 409
    assert wrong_owner.json()["error"]["code"] == "EXECUTION_LOST"
    assert _events(applied)[-1]["outcome"] == {"type": "success"}
    assert _events(repeated)[-1]["code"] == "RESUME_ALREADY_APPLIED"
    assert len(fake.sent_parts) == 1


@pytest.mark.asyncio
async def test_state_write_failure_cancels_a2a_without_emitting_interrupt(tmp_path) -> None:
    fake = FakeA2AClient(interrupt=True)
    store = FailNthSaveStore(fail_at=3)
    adapter = AguiA2AAdapter(a2a_url="http://a2a/", client=fake, state_store=store)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(adapter=adapter)), base_url="http://test"
    ) as client:
        response = await client.post("/", json=_payload(tmp_path))

    events = _events(response)
    assert events[-1]["type"] == "RUN_ERROR"
    assert events[-1]["code"] == "STATE_PERSISTENCE_FAILED"
    assert all(event.get("outcome", {}).get("type") != "interrupt" for event in events)
    assert fake.cancelled == ["task-1"]


@pytest.mark.asyncio
async def test_restart_keeps_original_absolute_interrupt_expiry(tmp_path) -> None:
    state_dir = tmp_path / "state"
    first_fake = FakeA2AClient(interrupt=True)
    first_adapter = AguiA2AAdapter(a2a_url="http://a2a/", client=first_fake, state_dir=state_dir, interrupt_ttl=1)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(adapter=first_adapter)), base_url="http://test"
    ) as client:
        response = await client.post("/", json=_payload(tmp_path))
    expires_at = datetime.fromisoformat(
        _events(response)[-1]["outcome"]["interrupts"][0]["expiresAt"].replace("Z", "+00:00")
    )
    await asyncio.sleep(0.55)
    await first_adapter.aclose()

    second_fake = FakeA2AClient(interrupt=True)
    second_adapter = AguiA2AAdapter(a2a_url="http://a2a/", client=second_fake, state_dir=state_dir, interrupt_ttl=20)
    await second_adapter.start()
    assert second_adapter._threads == {}
    resume_payload = _payload(
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
    await second_adapter.admit(parse_run_input(resume_payload), canonical_digest(resume_payload))
    remaining = max(0.0, (expires_at - datetime.now(timezone.utc)).total_seconds())

    async def wait_until_cancelled() -> None:
        while not second_fake.cancelled:
            await asyncio.sleep(0.02)

    await asyncio.wait_for(wait_until_cancelled(), timeout=remaining + 3.0)
    assert second_fake.cancelled == ["task-1"]
    state = json.loads(_thread_state_path(state_dir).read_text(encoding="utf-8"))
    assert state["execution"]["taskId"] is None
    assert state["execution"]["pending"] == {}
    await second_adapter.aclose()


@pytest.mark.asyncio
async def test_persisted_state_excludes_request_messages_and_credentials(tmp_path) -> None:
    state_dir = tmp_path / "state"
    fake = FakeA2AClient(interrupt=True)
    adapter = AguiA2AAdapter(a2a_url="http://a2a/", client=fake, state_dir=state_dir)
    payload = _payload(tmp_path)
    payload["messages"][0]["content"] = "private-user-message"
    payload["forwardedProps"]["iacCode"].update(
        {
            "llmApiKey": "llm-secret",
            "alibabaCloud": {
                "accessKeyId": "ak-secret",
                "accessKeySecret": "sk-secret",
                "securityToken": "sts-secret",
            },
        }
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(adapter=adapter)), base_url="http://test"
    ) as client:
        await client.post("/", json=payload)

    thread_path = _thread_state_path(state_dir)
    raw = thread_path.read_text(encoding="utf-8")
    for secret in ("private-user-message", "llm-secret", "ak-secret", "sk-secret", "sts-secret"):
        assert secret not in raw
    if os.name != "nt":
        assert stat.S_IMODE(state_dir.stat().st_mode) == 0o700
        assert stat.S_IMODE((state_dir / "threads").stat().st_mode) == 0o700
        assert stat.S_IMODE(thread_path.stat().st_mode) == 0o600


def test_file_state_store_rejects_unknown_schema(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    store = FileAguiThreadStateStore(state_dir)
    thread_path = store.path_for_thread("thread-1")
    thread_path.parent.mkdir(parents=True)
    thread_path.write_text('{"schemaVersion":999,"threadId":"thread-1"}', encoding="utf-8")

    with pytest.raises(AguiStateStoreError):
        store.load_thread("thread-1")


@pytest.mark.asyncio
async def test_adapter_lazily_loads_only_the_requested_thread(tmp_path: Path) -> None:
    store = FailNthSaveStore(fail_at=100)
    adapter = AguiA2AAdapter(a2a_url="http://a2a/", client=FakeA2AClient(), state_store=store)

    assert store.load_calls == []
    await adapter.start()
    assert store.load_calls == []

    payload = _payload(tmp_path)
    await adapter.admit(parse_run_input(payload), canonical_digest(payload))

    assert store.load_calls == ["thread-1"]
    assert set(store.values) == {"thread-1"}
    await adapter.aclose()


def test_file_state_store_uses_readable_safe_ids_and_reversible_unsafe_ids(tmp_path: Path) -> None:
    store = FileAguiThreadStateStore(tmp_path / "state")
    safe_thread_id = "8473547e-c8ed-4aef-a84c-603a6a8d42da"
    unsafe_thread_id = "../external/thread"
    safe_document = {"schemaVersion": 1, "threadId": safe_thread_id}
    unsafe_document = {"schemaVersion": 1, "threadId": unsafe_thread_id}

    store.save_thread(safe_thread_id, safe_document)
    store.save_thread(unsafe_thread_id, unsafe_document)

    safe_path = store.path_for_thread(safe_thread_id)
    unsafe_path = store.path_for_thread(unsafe_thread_id)
    assert safe_path.name == f"{safe_thread_id}.json"
    assert unsafe_path.parent == store.threads_dir
    assert unsafe_path.name.startswith("aguiid~")
    assert "/" not in unsafe_path.name and "\\" not in unsafe_path.name
    assert store.load_thread(safe_thread_id) == safe_document
    assert store.load_thread(unsafe_thread_id) == unsafe_document


def test_file_state_store_avoids_case_only_filename_collisions(tmp_path: Path) -> None:
    store = FileAguiThreadStateStore(tmp_path / "state")
    upper_thread_id = "Thread-1"
    lower_thread_id = "thread-1"
    upper_document = {"schemaVersion": 1, "threadId": upper_thread_id}
    lower_document = {"schemaVersion": 1, "threadId": lower_thread_id}

    store.save_thread(upper_thread_id, upper_document)
    store.save_thread(lower_thread_id, lower_document)

    upper_path = store.path_for_thread(upper_thread_id)
    lower_path = store.path_for_thread(lower_thread_id)
    assert upper_path.name.casefold() != lower_path.name.casefold()
    assert upper_path.stem.removeprefix("aguiid~") == upper_thread_id.encode("utf-8").hex()
    assert store.load_thread(upper_thread_id) == upper_document
    assert store.load_thread(lower_thread_id) == lower_document


@pytest.mark.parametrize(
    "thread_id",
    [
        "T" * 115,
        "会话" * 40,
    ],
)
def test_file_state_store_bounds_long_encoded_thread_filenames(tmp_path: Path, thread_id: str) -> None:
    store = FileAguiThreadStateStore(tmp_path / "state")
    document = {"schemaVersion": 1, "threadId": thread_id}

    store.save_thread(thread_id, document)

    path = store.path_for_thread(thread_id)
    assert path.parent == store.threads_dir
    assert path.name.startswith("aguihash~")
    assert len(path.name) < 100
    assert store.load_thread(thread_id) == document


def test_file_state_store_keeps_distinct_long_thread_ids_isolated(tmp_path: Path) -> None:
    store = FileAguiThreadStateStore(tmp_path / "state")
    first_thread_id = "T" * 114 + "A"
    second_thread_id = "T" * 114 + "B"
    first_document = {"schemaVersion": 1, "threadId": first_thread_id}
    second_document = {"schemaVersion": 1, "threadId": second_thread_id}

    store.save_thread(first_thread_id, first_document)
    store.save_thread(second_thread_id, second_document)

    assert store.path_for_thread(first_thread_id) != store.path_for_thread(second_thread_id)
    assert store.load_thread(first_thread_id) == first_document
    assert store.load_thread(second_thread_id) == second_document


@pytest.mark.asyncio
async def test_shared_state_dir_keeps_interleaved_threads_isolated(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    first_adapter = AguiA2AAdapter(a2a_url="http://a2a/", client=FakeA2AClient(), state_dir=state_dir)
    second_adapter = AguiA2AAdapter(a2a_url="http://a2a/", client=FakeA2AClient(), state_dir=state_dir)
    first_payload = _payload(tmp_path)
    second_payload = _payload(tmp_path)
    second_payload["threadId"] = "thread-2"
    second_payload["forwardedProps"]["iacCode"]["rosInvocationId"] = "invocation-2"

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(adapter=first_adapter)),
        base_url="http://test",
    ) as first_client, httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(adapter=second_adapter)),
        base_url="http://test",
    ) as second_client:
        first_response = await first_client.post("/", json=first_payload)
        second_response = await second_client.post("/", json=second_payload)

    assert first_response.status_code == 200
    assert second_response.status_code == 200
    assert _thread_state_path(state_dir, "thread-1").is_file()
    assert _thread_state_path(state_dir, "thread-2").is_file()
    assert not (state_dir / "adapter-state.json").exists()
    first_state = json.loads(_thread_state_path(state_dir, "thread-1").read_text(encoding="utf-8"))
    second_state = json.loads(_thread_state_path(state_dir, "thread-2").read_text(encoding="utf-8"))
    assert first_state["threadId"] == "thread-1"
    assert second_state["threadId"] == "thread-2"
    assert first_state["runDigests"] == {"run-1": canonical_digest(first_payload)}
    assert second_state["runDigests"] == {"run-1": canonical_digest(second_payload)}

    restarted_first = AguiA2AAdapter(a2a_url="http://a2a/", client=FakeA2AClient(), state_dir=state_dir)
    restarted_second = AguiA2AAdapter(a2a_url="http://a2a/", client=FakeA2AClient(), state_dir=state_dir)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(adapter=restarted_first)),
        base_url="http://test",
    ) as first_client, httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(adapter=restarted_second)),
        base_url="http://test",
    ) as second_client:
        first_replay = await first_client.post("/", json=first_payload)
        second_replay = await second_client.post("/", json=second_payload)

    assert first_replay.status_code == 409
    assert second_replay.status_code == 409
    assert first_replay.json()["error"]["code"] == "DUPLICATE_RUN_ID"
    assert second_replay.json()["error"]["code"] == "DUPLICATE_RUN_ID"


@pytest.mark.asyncio
async def test_corrupt_thread_state_does_not_block_another_thread(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    first_adapter = AguiA2AAdapter(a2a_url="http://a2a/", client=FakeA2AClient(), state_dir=state_dir)
    second_adapter = AguiA2AAdapter(a2a_url="http://a2a/", client=FakeA2AClient(), state_dir=state_dir)
    first_payload = _payload(tmp_path)
    second_payload = _payload(tmp_path)
    second_payload["threadId"] = "thread-2"
    second_payload["forwardedProps"]["iacCode"]["rosInvocationId"] = "invocation-2"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(adapter=first_adapter)),
        base_url="http://test",
    ) as first_client, httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(adapter=second_adapter)),
        base_url="http://test",
    ) as second_client:
        await first_client.post("/", json=first_payload)
        await second_client.post("/", json=second_payload)

    _thread_state_path(state_dir, "thread-1").write_text("not-json", encoding="utf-8")
    restarted = AguiA2AAdapter(a2a_url="http://a2a/", client=FakeA2AClient(), state_dir=state_dir)
    second_next = _payload(tmp_path, run_id="run-2")
    second_next["threadId"] = "thread-2"
    second_next["forwardedProps"]["iacCode"]["rosInvocationId"] = "invocation-3"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(adapter=restarted)),
        base_url="http://test",
    ) as client:
        healthy = await client.post("/", json=second_next)
        corrupt = await client.post("/", json=first_payload)

    assert healthy.status_code == 200
    assert corrupt.status_code == 503
    assert corrupt.json()["error"]["code"] == "STATE_UNAVAILABLE"
