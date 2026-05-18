import asyncio

import pytest

from iac_code.a2a.transports.dispatcher import create_runtime_components
from iac_code.a2a.transports.stdio import StdioA2AClient, StdioA2AServer, decode_frame, encode_frame
from iac_code.types.stream_events import TextDeltaEvent

from .fakes import FakeAgentLoop, FakeRuntime


class MemoryWriter:
    def __init__(self, reader: asyncio.StreamReader) -> None:
        self.reader = reader
        self.closed = False

    def write(self, data: bytes) -> None:
        self.reader.feed_data(data)

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True
        self.reader.feed_eof()

    async def wait_closed(self) -> None:
        return None


def make_stream_pair() -> tuple[asyncio.StreamReader, MemoryWriter]:
    reader = asyncio.StreamReader()
    return reader, MemoryWriter(reader)


def test_encode_decode_frame_round_trip() -> None:
    payload = {"jsonrpc": "2.0", "id": "1", "result": {"ok": True}}

    assert decode_frame(encode_frame(payload)) == payload


@pytest.mark.asyncio
async def test_stdio_server_handles_unary_request(monkeypatch, tmp_path) -> None:
    loop = FakeAgentLoop([TextDeltaEvent(text="stdio ok")])
    runtime = FakeRuntime(agent_loop=loop, session_id="session-1")
    monkeypatch.setattr("iac_code.a2a.executor.create_agent_runtime", lambda options: runtime)
    client_to_server, client_writer = make_stream_pair()
    server_to_client, server_writer = make_stream_pair()
    components = create_runtime_components(model="qwen3.6-plus", host="127.0.0.1", port=41242)
    server = StdioA2AServer(components=components, reader=client_to_server, writer=server_writer)
    task = asyncio.create_task(server.serve())

    client_writer.write(
        encode_frame(
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

    response = decode_frame(await asyncio.wait_for(server_to_client.readline(), timeout=1))
    assert response["id"] == "1"
    assert response["result"]["status"]["state"] == "input-required"
    client_writer.close()
    await task
    await components.aclose()


@pytest.mark.asyncio
async def test_stdio_client_sends_request_and_reads_response() -> None:
    request_reader, request_writer = make_stream_pair()
    response_reader, response_writer = make_stream_pair()
    client = StdioA2AClient(reader=response_reader, writer=request_writer)

    pending = asyncio.create_task(client.send({"jsonrpc": "2.0", "id": "1", "method": "ping"}))
    request = decode_frame(await asyncio.wait_for(request_reader.readline(), timeout=1))
    response_writer.write(encode_frame({"jsonrpc": "2.0", "id": request["id"], "result": {"pong": True}}))

    assert await pending == {"jsonrpc": "2.0", "id": "1", "result": {"pong": True}}
