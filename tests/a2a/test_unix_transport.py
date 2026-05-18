import asyncio
import contextlib

import pytest

from iac_code.a2a.transports.dispatcher import create_runtime_components
from iac_code.a2a.transports.unix import UnixA2AClient, UnixA2AServer, validate_socket_path
from iac_code.types.stream_events import TextDeltaEvent

from .fakes import FakeAgentLoop, FakeRuntime


def test_validate_socket_path_requires_existing_parent(tmp_path) -> None:
    socket_path = tmp_path / "iac-code.sock"

    assert validate_socket_path(str(socket_path)) == socket_path

    with pytest.raises(ValueError, match="Unix socket parent does not exist"):
        validate_socket_path(str(tmp_path / "missing" / "iac-code.sock"))


async def wait_for_socket(socket_path, timeout: float = 1.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not socket_path.exists():
        if asyncio.get_running_loop().time() >= deadline:
            raise TimeoutError(f"Timed out waiting for Unix socket: {socket_path}")
        await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_unix_server_and_client_handle_unary_request(monkeypatch, tmp_path) -> None:
    loop = FakeAgentLoop([TextDeltaEvent(text="unix ok")])
    runtime = FakeRuntime(agent_loop=loop, session_id="session-1")
    monkeypatch.setattr("iac_code.a2a.executor.create_agent_runtime", lambda options: runtime)
    monkeypatch.chdir(tmp_path)
    socket_path = tmp_path / "iac-code.sock"
    socket_name = "iac-code.sock"
    components = create_runtime_components(model="qwen3.6-plus", host="127.0.0.1", port=41242)
    server = UnixA2AServer(components=components, socket_path=socket_name)
    serve_task = asyncio.create_task(server.serve())

    try:
        await wait_for_socket(socket_path)
        client = UnixA2AClient(socket_path=socket_name)
        try:
            response = await asyncio.wait_for(
                client.send(
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
                ),
                timeout=1,
            )
        finally:
            await client.aclose()

        assert response["id"] == "1"
        assert response["result"]["status"]["state"] == "input-required"
        assert loop.prompts == ["hello"]
    finally:
        await server.aclose()
        serve_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await serve_task

    assert not socket_path.exists()
