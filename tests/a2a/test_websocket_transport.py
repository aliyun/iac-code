import json
from typing import Any

import pytest

from iac_code.a2a.transports.dispatcher import create_runtime_components
from iac_code.a2a.transports.websocket import WebSocketA2AServerApp, websocket_event_frame
from iac_code.types.stream_events import TextDeltaEvent

from .fakes import FakeAgentLoop, FakeRuntime


def test_websocket_event_frame_marks_final() -> None:
    frame = websocket_event_frame({"jsonrpc": "2.0", "id": "1", "result": {"ok": True}}, final=True)

    assert frame == {"id": "1", "payload": {"jsonrpc": "2.0", "id": "1", "result": {"ok": True}}, "final": True}


@pytest.mark.asyncio
async def test_websocket_app_handles_unary_frame(monkeypatch, tmp_path) -> None:
    loop = FakeAgentLoop([TextDeltaEvent(text="ws ok")])
    runtime = FakeRuntime(agent_loop=loop, session_id="session-1")
    monkeypatch.setattr("iac_code.a2a.executor.create_agent_runtime", lambda options: runtime)
    components = create_runtime_components(model="qwen3.6-plus", host="127.0.0.1", port=41242)
    app = WebSocketA2AServerApp(components=components, path="/a2a").create_app()

    from starlette.testclient import TestClient

    with TestClient(app) as client:
        with client.websocket_connect("/a2a") as websocket:
            websocket.send_text(
                json.dumps(
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
            )
            response = websocket.receive_json()

    assert response["final"] is True
    assert response["payload"]["result"]["status"]["state"] == "input-required"
    await components.aclose()


@pytest.mark.asyncio
async def test_websocket_app_reports_invalid_json_frame() -> None:
    components = create_runtime_components(model="qwen3.6-plus", host="127.0.0.1", port=41242)
    app = WebSocketA2AServerApp(components=components, path="/a2a").create_app()

    from starlette.testclient import TestClient

    with TestClient(app) as client:
        with client.websocket_connect("/a2a") as websocket:
            websocket.send_text("{broken")
            response = websocket.receive_json()

    assert response["final"] is True
    assert response["payload"]["error"]["code"] == -32700
    await components.aclose()


@pytest.mark.asyncio
async def test_websocket_app_returns_public_error_for_unary_dispatch_failure(monkeypatch) -> None:
    class FailingDispatcher:
        async def dispatch(self, payload: dict[str, Any]) -> dict[str, Any]:
            raise RuntimeError("boom with DB_PASSWORD=hunter2 at /Users/alice/.iac-code/settings.yml")

        async def aclose(self) -> None:
            pass

    monkeypatch.setattr(
        "iac_code.a2a.transports.websocket.A2AJsonRpcDispatcher", lambda components: FailingDispatcher()
    )
    components = create_runtime_components(model="qwen3.6-plus", host="127.0.0.1", port=41242)
    app = WebSocketA2AServerApp(components=components, path="/a2a").create_app()

    from starlette.testclient import TestClient

    with TestClient(app) as client:
        with client.websocket_connect("/a2a") as websocket:
            websocket.send_text(json.dumps({"jsonrpc": "2.0", "id": "err-1", "method": "message/send"}))
            response = websocket.receive_json()

    assert response["final"] is True
    assert response["payload"]["id"] == "err-1"
    assert response["payload"]["error"]["code"] == -32603
    assert "hunter2" not in response["payload"]["error"]["message"]
    assert "/Users/alice" not in response["payload"]["error"]["message"]
    assert response["payload"]["error"]["data"]["error_id"]
    await components.aclose()


@pytest.mark.asyncio
async def test_websocket_app_returns_final_public_error_for_stream_dispatch_failure(monkeypatch) -> None:
    class FailingStreamDispatcher:
        async def dispatch_stream(self, payload: dict[str, Any]):
            yield {"jsonrpc": "2.0", "id": payload["id"], "result": {"state": "working"}}
            raise RuntimeError("stream boom with api_key=sk-live at /Users/alice/.iac-code/settings.yml")

        async def aclose(self) -> None:
            pass

    monkeypatch.setattr(
        "iac_code.a2a.transports.websocket.A2AJsonRpcDispatcher",
        lambda components: FailingStreamDispatcher(),
    )
    components = create_runtime_components(model="qwen3.6-plus", host="127.0.0.1", port=41242)
    app = WebSocketA2AServerApp(components=components, path="/a2a").create_app()

    from starlette.testclient import TestClient

    with TestClient(app) as client:
        with client.websocket_connect("/a2a") as websocket:
            websocket.send_text(json.dumps({"jsonrpc": "2.0", "id": "stream-1", "method": "SendStreamingMessage"}))
            event = websocket.receive_json()
            response = websocket.receive_json()

    assert event["final"] is False
    assert response["final"] is True
    assert response["payload"]["id"] == "stream-1"
    assert response["payload"]["error"]["code"] == -32603
    assert "sk-live" not in response["payload"]["error"]["message"]
    assert "/Users/alice" not in response["payload"]["error"]["message"]
    assert response["payload"]["error"]["data"]["error_id"]
    await components.aclose()
