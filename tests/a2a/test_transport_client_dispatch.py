import pytest

from iac_code.a2a.client import A2AClient


class FakeTransportClient:
    def __init__(self) -> None:
        self.sent = []

    async def send(self, payload):
        self.sent.append(payload)
        return {"jsonrpc": "2.0", "id": payload["id"], "result": {"text": "ok"}}

    async def stream(self, payload):
        self.sent.append(payload)
        yield {"jsonrpc": "2.0", "id": payload["id"], "result": {"status": {"state": "working"}}}
        yield {"jsonrpc": "2.0", "id": payload["id"], "result": {"status": {"state": "done"}}, "final": True}

    async def aclose(self):
        return None


@pytest.mark.asyncio
async def test_a2a_client_uses_registered_non_http_transport() -> None:
    fake = FakeTransportClient()
    captured = {}

    def factory(options):
        captured["binding"] = options.binding
        return fake

    client = A2AClient(transport_client_factory=factory)

    response = await client.send_message("unix:///tmp/iac-code.sock", "hello", cwd="/tmp/work")

    assert response.text == "ok"
    assert fake.sent[0]["method"] == "SendMessage"
    assert captured["binding"].url == "unix:///tmp/iac-code.sock"
    assert captured["binding"].transport == "unix"


@pytest.mark.asyncio
async def test_a2a_client_streams_registered_non_http_transport() -> None:
    fake = FakeTransportClient()
    client = A2AClient(transport_client_factory=lambda options: fake)

    events = [event async for event in client.stream_message("ws://127.0.0.1:41243/a2a", "hello", cwd="/tmp/work")]

    assert events[-1]["final"] is True
    assert fake.sent[0]["method"] == "SendStreamingMessage"
