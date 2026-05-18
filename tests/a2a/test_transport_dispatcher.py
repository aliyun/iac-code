import pytest

from iac_code.a2a.transports.dispatcher import A2AJsonRpcDispatcher, A2ARuntimeComponents, create_runtime_components
from iac_code.types.stream_events import TextDeltaEvent

from .fakes import FakeAgentLoop, FakeRuntime


@pytest.mark.asyncio
async def test_dispatcher_handles_unary_v03_message(monkeypatch, tmp_path) -> None:
    loop = FakeAgentLoop([TextDeltaEvent(text="hello from dispatcher")])
    runtime = FakeRuntime(agent_loop=loop, session_id="session-1")
    monkeypatch.setattr("iac_code.a2a.executor.create_agent_runtime", lambda options: runtime)
    components = create_runtime_components(model="qwen3.6-plus", host="127.0.0.1", port=41242)
    dispatcher = A2AJsonRpcDispatcher(components)

    response = await dispatcher.dispatch(
        {
            "jsonrpc": "2.0",
            "id": "1",
            "method": "message/send",
            "params": {
                "message": {
                    "messageId": "msg-1",
                    "role": "user",
                    "parts": [{"kind": "text", "text": "hello"}],
                    "metadata": {"iac_code": {"cwd": str(tmp_path)}},
                },
                "configuration": {"acceptedOutputModes": ["text/plain"]},
            },
        }
    )

    assert response["id"] == "1"
    assert response["result"]["status"]["state"] == "input-required"
    assert loop.prompts == ["hello"]
    await components.aclose()


@pytest.mark.asyncio
async def test_dispatcher_stream_yields_events(monkeypatch, tmp_path) -> None:
    loop = FakeAgentLoop([TextDeltaEvent(text="streamed")])
    runtime = FakeRuntime(agent_loop=loop, session_id="session-1")
    monkeypatch.setattr("iac_code.a2a.executor.create_agent_runtime", lambda options: runtime)
    components = create_runtime_components(model="qwen3.6-plus", host="127.0.0.1", port=41242)
    dispatcher = A2AJsonRpcDispatcher(components)

    events = [
        event
        async for event in dispatcher.dispatch_stream(
            {
                "jsonrpc": "2.0",
                "id": "2",
                "method": "message/stream",
                "params": {
                    "message": {
                        "messageId": "msg-2",
                        "role": "user",
                        "parts": [{"kind": "text", "text": "hello"}],
                        "metadata": {"iac_code": {"cwd": str(tmp_path)}},
                    },
                    "configuration": {"acceptedOutputModes": ["text/plain"]},
                },
            }
        )
    ]

    assert any(event["result"]["status"]["state"] == "working" for event in events)
    assert events[-1]["result"]["status"]["state"] == "input-required"
    await components.aclose()


def test_create_runtime_components_returns_shared_objects() -> None:
    components = create_runtime_components(model="qwen3.6-plus", host="127.0.0.1", port=41242)

    assert isinstance(components, A2ARuntimeComponents)
    assert components.handler is not None
    assert components.task_store is not None


@pytest.mark.asyncio
async def test_dispatcher_reuses_http_client(monkeypatch) -> None:
    created = 0

    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self):
            return {"jsonrpc": "2.0", "id": "1", "result": {"ok": True}}

    class FakeHTTPClient:
        def __init__(self, **kwargs) -> None:
            nonlocal created
            created += 1

        async def post(self, *args, **kwargs):
            return FakeResponse()

        async def aclose(self) -> None:
            return None

    monkeypatch.setattr("iac_code.a2a.transports.dispatcher.httpx.AsyncClient", FakeHTTPClient)
    dispatcher = A2AJsonRpcDispatcher(create_runtime_components(model="qwen3.6-plus", host="127.0.0.1", port=41242))

    await dispatcher.dispatch({"jsonrpc": "2.0", "id": "1", "method": "message/send"})
    await dispatcher.dispatch({"jsonrpc": "2.0", "id": "2", "method": "message/send"})
    await dispatcher.aclose()

    assert created == 1
