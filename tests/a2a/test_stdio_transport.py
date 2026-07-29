import asyncio
import contextlib
from unittest.mock import AsyncMock

import pytest

from iac_code.a2a.transports.dispatcher import A2ARuntimeComponents, create_runtime_components
from iac_code.a2a.transports.stdio import (
    StdioA2AClient,
    StdioA2AServer,
    decode_frame,
    encode_frame,
    is_streaming_request,
)


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


async def close_stdio_server(
    client_writer: MemoryWriter,
    server_task: asyncio.Task[None],
    components: A2ARuntimeComponents,
) -> None:
    client_writer.close()
    try:
        await asyncio.wait_for(server_task, timeout=1)
    except asyncio.TimeoutError:
        server_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await server_task
        raise
    finally:
        await components.aclose()


def test_encode_decode_frame_round_trip() -> None:
    payload = {"jsonrpc": "2.0", "id": "1", "result": {"ok": True}}

    assert decode_frame(encode_frame(payload)) == payload


def test_send_streaming_message_is_streaming_request() -> None:
    assert is_streaming_request({"method": "SendStreamingMessage"}) is True


@pytest.mark.asyncio
async def test_stdio_server_handles_unary_request() -> None:
    client_to_server, client_writer = make_stream_pair()
    server_to_client, server_writer = make_stream_pair()
    components = create_runtime_components(model="qwen3.6-plus", host="127.0.0.1", port=41242)
    server = StdioA2AServer(components=components, reader=client_to_server, writer=server_writer)
    expected_response = {
        "jsonrpc": "2.0",
        "id": "1",
        "result": {"status": {"state": "input-required"}},
    }
    server._dispatcher.dispatch = AsyncMock(return_value=expected_response)  # type: ignore[method-assign]
    task = asyncio.create_task(server.serve())
    request = {
        "jsonrpc": "2.0",
        "id": "1",
        "method": "message/send",
        "params": {
            "message": {
                "messageId": "msg-1",
                "role": "user",
                "parts": [{"kind": "text", "text": "hello"}],
            },
            "configuration": {"acceptedOutputModes": ["text/plain"]},
        },
    }

    try:
        client_writer.write(encode_frame(request))
        response = decode_frame(await asyncio.wait_for(server_to_client.readline(), timeout=1))
    finally:
        await close_stdio_server(client_writer, task, components)

    assert response["id"] == "1"
    assert response["result"]["status"]["state"] == "input-required"
    server._dispatcher.dispatch.assert_awaited_once_with(request)


@pytest.mark.asyncio
async def test_stdio_server_returns_raw_outer_dispatch_errors_without_safe_mode(monkeypatch) -> None:
    monkeypatch.delenv("IAC_CODE_A2A_SAFE_MODE", raising=False)

    client_to_server, client_writer = make_stream_pair()
    server_to_client, server_writer = make_stream_pair()
    components = create_runtime_components(model="qwen3.6-plus", host="127.0.0.1", port=41242)
    server = StdioA2AServer(components=components, reader=client_to_server, writer=server_writer)
    server._dispatcher.dispatch = AsyncMock(  # type: ignore[method-assign]
        side_effect=RuntimeError(
            "dispatch failed at /Users/alice/.iac-code/a2a.sock; Authorization: Bearer sk-stdiosecret123"
        )
    )
    task = asyncio.create_task(server.serve())

    try:
        client_writer.write(encode_frame({"jsonrpc": "2.0", "id": "1", "method": "ping"}))

        response = decode_frame(await asyncio.wait_for(server_to_client.readline(), timeout=1))
    finally:
        await close_stdio_server(client_writer, task, components)
    assert response["id"] == "1"
    error = response["error"]
    assert error["code"] == -32603
    assert "dispatch failed" in error["message"]
    assert "sk-stdiosecret123" in error["message"]
    assert "Authorization: Bearer" in error["message"]
    assert "/Users/alice" in error["message"]


@pytest.mark.asyncio
async def test_stdio_server_projects_error_from_new_request_cwd_in_safe_mode(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("IAC_CODE_A2A_SAFE_MODE", "1")
    client_to_server, client_writer = make_stream_pair()
    server_to_client, server_writer = make_stream_pair()
    components = create_runtime_components(model="qwen3.6-plus", host="127.0.0.1", port=41242)
    server = StdioA2AServer(components=components, reader=client_to_server, writer=server_writer)
    server_path = tmp_path / "private" / "stdio.sock"
    server._dispatcher.dispatch = AsyncMock(  # type: ignore[method-assign]
        side_effect=RuntimeError(f"dispatch failed at {server_path}; token=real-secret")
    )
    task = asyncio.create_task(server.serve())
    request = {
        "jsonrpc": "2.0",
        "id": "1",
        "method": "ping",
        "params": {"message": {"metadata": {"iac_code": {"cwd": str(tmp_path)}}}},
    }

    try:
        client_writer.write(encode_frame(request))
        response = decode_frame(await asyncio.wait_for(server_to_client.readline(), timeout=1))
    finally:
        await close_stdio_server(client_writer, task, components)

    assert "[PATH]" in response["error"]["message"]
    assert str(tmp_path) not in response["error"]["message"]
    assert "real-secret" in response["error"]["message"]


@pytest.mark.asyncio
async def test_stdio_streaming_request_emits_final_frame_and_client_finishes() -> None:
    client_to_server, client_writer = make_stream_pair()
    server_to_client, server_writer = make_stream_pair()
    components = create_runtime_components(model="qwen3.6-plus", host="127.0.0.1", port=41242)
    server = StdioA2AServer(components=components, reader=client_to_server, writer=server_writer)

    async def dispatch_stream(payload):
        yield {"jsonrpc": "2.0", "id": payload["id"], "result": {"status": {"state": "completed"}}}

    server._dispatcher.dispatch_stream = dispatch_stream  # type: ignore[method-assign]
    server_task = asyncio.create_task(server.serve())
    client = StdioA2AClient(reader=server_to_client, writer=client_writer)

    async def collect():
        return [
            event
            async for event in client.stream({"jsonrpc": "2.0", "id": "stream-1", "method": "SendStreamingMessage"})
        ]

    try:
        events = await asyncio.wait_for(collect(), timeout=1)
    finally:
        await close_stdio_server(client_writer, server_task, components)

    assert events == [
        {"jsonrpc": "2.0", "id": "stream-1", "result": {"status": {"state": "completed"}}},
        {"jsonrpc": "2.0", "id": "stream-1", "final": True},
    ]


@pytest.mark.asyncio
async def test_stdio_client_sends_request_and_reads_response() -> None:
    request_reader, request_writer = make_stream_pair()
    response_reader, response_writer = make_stream_pair()
    client = StdioA2AClient(reader=response_reader, writer=request_writer)

    pending = asyncio.create_task(client.send({"jsonrpc": "2.0", "id": "1", "method": "ping"}))
    request = decode_frame(await asyncio.wait_for(request_reader.readline(), timeout=1))
    response_writer.write(encode_frame({"jsonrpc": "2.0", "id": request["id"], "result": {"pong": True}}))

    assert await pending == {"jsonrpc": "2.0", "id": "1", "result": {"pong": True}}


class TestOpenStdioStreamsWindows:
    """Windows path uses a daemon thread + sync writer wrapper."""

    @pytest.mark.asyncio
    async def test_sync_writer_writes_and_flushes(self, monkeypatch):
        """_SyncStdoutWriter writes bytes to the underlying buffer and flushes."""
        from io import BytesIO

        from iac_code.a2a.transports.stdio import _SyncStdoutWriter

        class FlushTracker(BytesIO):
            def __init__(self):
                super().__init__()
                self.flush_count = 0

            def flush(self):
                self.flush_count += 1
                super().flush()

        buffer = FlushTracker()
        writer = _SyncStdoutWriter(buffer)
        writer.write(b'{"hello":"world"}\n')
        await writer.drain()
        assert buffer.getvalue() == b'{"hello":"world"}\n'
        assert buffer.flush_count == 1

        writer.close()
        await writer.wait_closed()
        writer.write(b"ignored")
        assert buffer.getvalue() == b'{"hello":"world"}\n'

    @pytest.mark.asyncio
    async def test_windows_branch_uses_thread_reader(self, monkeypatch):
        """When sys.platform == 'win32', open_stdio_streams reads stdin via
        a daemon thread and returns a _SyncStdoutWriter for stdout."""
        from io import BytesIO

        import iac_code.a2a.transports.stdio as stdio_mod

        fake_stdin = BytesIO(b'{"hello":"world"}\n')
        fake_stdout = BytesIO()

        class FakeSys:
            platform = "win32"

            class Stdin:
                buffer = fake_stdin

            stdin = Stdin

            class Stdout:
                buffer = fake_stdout

            stdout = Stdout

        monkeypatch.setattr(stdio_mod, "sys", FakeSys)

        reader, writer = await stdio_mod.open_stdio_streams()

        line = await asyncio.wait_for(reader.readline(), timeout=2.0)
        assert line == b'{"hello":"world"}\n'

        from iac_code.a2a.transports.stdio import _SyncStdoutWriter

        assert isinstance(writer, _SyncStdoutWriter)

        writer.write(b"out\n")
        await writer.drain()
        assert fake_stdout.getvalue() == b"out\n"

    @pytest.mark.asyncio
    async def test_stdin_thread_handles_closed_loop(self, monkeypatch):
        """When the event loop is closed before stdin EOF, the daemon thread
        must exit silently instead of propagating RuntimeError."""
        import threading
        from io import BytesIO

        import iac_code.a2a.transports.stdio as stdio_mod

        class BlockingStdin(BytesIO):
            def __init__(self):
                super().__init__()
                self._event = threading.Event()

            def read(self, n=-1):
                self._event.wait()
                return b""

            def unblock(self):
                self._event.set()

        fake_stdin = BlockingStdin()
        fake_stdout = BytesIO()

        class FakeSys:
            platform = "win32"

            class Stdin:
                buffer = fake_stdin

            stdin = Stdin

            class Stdout:
                buffer = fake_stdout

            stdout = Stdout

        monkeypatch.setattr(stdio_mod, "sys", FakeSys)

        threads_before = set(threading.enumerate())
        reader, writer = await stdio_mod.open_stdio_streams()
        stdin_thread = (set(threading.enumerate()) - threads_before).pop()

        loop = asyncio.get_running_loop()
        original_cstp = loop.call_soon_threadsafe
        try:
            loop.call_soon_threadsafe = lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("Event loop is closed"))
            fake_stdin.unblock()
            stdin_thread.join(timeout=2.0)
            assert not stdin_thread.is_alive(), "stdin thread should have exited cleanly"
        finally:
            loop.call_soon_threadsafe = original_cstp
