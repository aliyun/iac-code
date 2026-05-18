from starlette.testclient import TestClient

from iac_code.a2a.app import create_app
from iac_code.types.stream_events import TextDeltaEvent

from .fakes import FakeAgentLoop, FakeRuntime


def test_rest_message_send_uses_official_route(monkeypatch, tmp_path) -> None:
    loop = FakeAgentLoop([TextDeltaEvent(text="ok")])
    runtime = FakeRuntime(agent_loop=loop, session_id="session-1")
    monkeypatch.setattr("iac_code.a2a.executor.create_agent_runtime", lambda options: runtime)

    app = create_app(host="127.0.0.1", port=41242, token=None, model="qwen3.6-plus")
    client = TestClient(app)

    response = client.post(
        "/message:send",
        headers={"A2A-Version": "1.0"},
        json={
            "message": {
                "messageId": "msg-1",
                "role": "ROLE_USER",
                "parts": [{"text": "hello"}],
                "metadata": {"iac_code": {"cwd": str(tmp_path)}},
            },
            "configuration": {"acceptedOutputModes": ["text/plain"]},
        },
    )

    assert response.status_code == 200
    assert response.json()["task"]["status"]["state"] == "TASK_STATE_INPUT_REQUIRED"
    assert loop.prompts == ["hello"]


def test_rest_extended_agent_card_route_returns_card() -> None:
    app = create_app(host="127.0.0.1", port=41242, token=None, model="qwen3.6-plus")
    client = TestClient(app)

    response = client.get("/extendedAgentCard", headers={"A2A-Version": "1.0"})

    assert response.status_code == 200
    assert response.json()["name"] == "iac-code"
