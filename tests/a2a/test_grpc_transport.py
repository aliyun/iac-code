import asyncio
import json
from types import SimpleNamespace

import pytest
from a2a.types import a2a_pb2
from google.protobuf.json_format import MessageToDict, ParseDict

from iac_code.a2a.transports.base import A2ATransportDependencyError
from iac_code.a2a.transports.dispatcher import create_runtime_components
from iac_code.a2a.transports.grpc import _GRPC_REQUEST_DATA, GrpcA2AServer, _projecting_grpc_handler, require_grpc
from iac_code.a2a.transports.grpc_jsonrpc import GrpcA2AClient, JsonRpcEnvelope, _from_envelope, _JsonRpcServicer


class FakeGrpcStub:
    def __init__(self) -> None:
        self.requests = []

    async def Send(self, envelope: JsonRpcEnvelope) -> JsonRpcEnvelope:  # noqa: N802
        self.requests.append(envelope)
        return JsonRpcEnvelope(payload=b'{"jsonrpc":"2.0","id":"1","result":{"ok":true}}')

    async def Stream(self, envelope: JsonRpcEnvelope):  # noqa: N802
        self.requests.append(envelope)
        yield JsonRpcEnvelope(payload=b'{"jsonrpc":"2.0","id":"1","result":{"state":"working"}}')
        yield JsonRpcEnvelope(payload=b'{"jsonrpc":"2.0","id":"1","result":{"state":"done"},"final":true}')


def test_require_grpc_reports_missing_dependency(monkeypatch) -> None:
    real_import = __import__

    def fail_grpc_import(name, *args, **kwargs):
        if name == "grpc":
            raise ImportError(name)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", fail_grpc_import)

    with pytest.raises(A2ATransportDependencyError, match="iac-code\\[a2a-grpc\\]"):
        require_grpc()


@pytest.mark.asyncio
async def test_grpc_client_sends_json_payload() -> None:
    client = GrpcA2AClient(stub=FakeGrpcStub())

    response = await client.send({"jsonrpc": "2.0", "id": "1", "method": "message/send"})

    assert response["result"]["ok"] is True


@pytest.mark.asyncio
async def test_grpc_client_streams_json_payloads() -> None:
    client = GrpcA2AClient(stub=FakeGrpcStub())

    events = [event async for event in client.stream({"jsonrpc": "2.0", "id": "1", "method": "message/stream"})]

    assert events[0]["result"]["state"] == "working"
    assert events[-1]["final"] is True


def test_grpc_server_requires_host_and_port() -> None:
    with pytest.raises(ValueError, match="gRPC host and port"):
        GrpcA2AServer(components=None, host="", port=0)


def test_grpc_server_allows_ephemeral_zero_port() -> None:
    GrpcA2AServer(components=None, host="127.0.0.1", port=0)


@pytest.mark.asyncio
async def test_grpc_stream_swallows_client_disconnect() -> None:
    class DisconnectingDispatcher:
        async def dispatch_stream(self, payload):
            raise asyncio.CancelledError()
            yield payload

    class CancelledContext:
        def cancelled(self) -> bool:
            return True

    servicer = _JsonRpcServicer.__new__(_JsonRpcServicer)
    servicer._dispatcher = DisconnectingDispatcher()

    events = [
        event async for event in servicer.Stream(JsonRpcEnvelope(payload=b'{"jsonrpc":"2.0"}'), CancelledContext())
    ]

    assert events == []


@pytest.mark.asyncio
async def test_grpc_jsonrpc_stream_emits_final_envelope_after_dispatch_completes() -> None:
    class CompletingDispatcher:
        async def dispatch_stream(self, payload):
            yield {"jsonrpc": "2.0", "id": payload["id"], "result": {"state": "working"}}

    class OpenContext:
        def cancelled(self) -> bool:
            return False

    servicer = _JsonRpcServicer.__new__(_JsonRpcServicer)
    servicer._dispatcher = CompletingDispatcher()

    envelopes = [
        envelope
        async for envelope in servicer.Stream(
            JsonRpcEnvelope(payload=b'{"jsonrpc":"2.0","id":"stream-1"}'),
            OpenContext(),
        )
    ]

    assert len(envelopes) == 2
    assert envelopes[0].final is False
    assert envelopes[1].final is True


@pytest.mark.asyncio
async def test_grpc_jsonrpc_send_returns_public_error_for_dispatch_failure() -> None:
    class FailingDispatcher:
        async def dispatch(self, payload):
            raise RuntimeError("boom with DB_PASSWORD=hunter2 at /Users/alice/.iac-code/settings.yml")

    servicer = _JsonRpcServicer.__new__(_JsonRpcServicer)
    servicer._dispatcher = FailingDispatcher()

    response = await servicer.Send(JsonRpcEnvelope(payload=b'{"jsonrpc":"2.0","id":"send-1"}'), object())
    payload = _from_envelope(response)

    assert payload["id"] == "send-1"
    assert payload["error"]["code"] == -32603
    assert "hunter2" in payload["error"]["message"]
    assert "/Users/alice" in payload["error"]["message"]
    assert payload["error"]["data"]["error_id"]


@pytest.mark.asyncio
async def test_grpc_jsonrpc_projects_error_from_new_request_cwd_in_safe_mode(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("IAC_CODE_A2A_SAFE_MODE", "yes")
    server_path = tmp_path / "private" / "grpc.sock"

    class FailingDispatcher:
        async def dispatch(self, payload):
            raise RuntimeError(f"boom token=real-secret at {server_path}")

    servicer = _JsonRpcServicer.__new__(_JsonRpcServicer)
    servicer._dispatcher = FailingDispatcher()
    request = {
        "jsonrpc": "2.0",
        "id": "send-1",
        "params": {"message": {"metadata": {"iac_code": {"cwd": str(tmp_path)}}}},
    }

    response = await servicer.Send(JsonRpcEnvelope(payload=json.dumps(request).encode()), object())
    message = _from_envelope(response)["error"]["message"]

    assert "[PATH]" in message
    assert str(tmp_path) not in message
    assert "real-secret" in message


@pytest.mark.asyncio
async def test_official_grpc_projects_success_and_error_from_new_request_cwd(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("IAC_CODE_A2A_SAFE_MODE", "1")
    server_path = str(tmp_path / "private" / "result.json")

    class BaseHandler:
        def __init__(self, handler) -> None:
            self.handler = handler
            self.aborted_error = None
            self.error_to_abort = None

        async def _handle_unary(self, request, context, handler_func, default_response):
            if self.error_to_abort is not None:
                await self.abort_context(self.error_to_abort, context)
            return default_response

        async def abort_context(self, error, context) -> None:
            self.aborted_error = error

    class WireError:
        def __init__(self, *, message, data=None) -> None:
            self.message = message
            self.data = data

    components = create_runtime_components(model="qwen3.6-plus", host="127.0.0.1", port=41242)
    handler = _projecting_grpc_handler(BaseHandler, components)
    request = a2a_pb2.SendMessageRequest()
    ParseDict({"iac_code": {"cwd": str(tmp_path)}}, request.metadata)
    response = a2a_pb2.SendMessageResponse()
    ParseDict({"path": server_path, "password": "real-secret"}, response.message.metadata)

    projected = await handler._handle_unary(request, object(), None, response)
    handler.error_to_abort = WireError(message=f"failed token=real-secret at {server_path}", data={"path": server_path})
    await handler._handle_unary(request, object(), None, a2a_pb2.SendMessageResponse())

    assert MessageToDict(projected.message.metadata) == {"path": "[PATH]", "password": "real-secret"}
    assert handler.aborted_error.message == "failed token=real-secret at [PATH]"
    assert handler.aborted_error.data == {"path": "[PATH]"}
    await components.aclose()


@pytest.mark.asyncio
async def test_official_grpc_task_scoped_error_uses_bare_request_id_roots(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("IAC_CODE_A2A_SAFE_MODE", "true")
    workspace = tmp_path / "task-workspace"
    server_path = str(workspace / "private" / "result.json")

    class TaskStore:
        async def get_task_record(self, task_id):
            assert task_id == "task-1"
            return SimpleNamespace(context_id="context-1")

        async def get_context_record(self, context_id):
            assert context_id == "context-1"
            return SimpleNamespace(cwd=str(workspace), session_id="session-1")

    class BaseHandler:
        def __init__(self, handler) -> None:
            self.aborted_error = None

        async def abort_context(self, error, context) -> None:
            self.aborted_error = error

    class WireError:
        def __init__(self, *, message, data=None) -> None:
            self.message = message
            self.data = data

    components = SimpleNamespace(task_store=TaskStore(), handler=object())
    handler = _projecting_grpc_handler(BaseHandler, components)
    request = a2a_pb2.GetTaskRequest(id="task-1")
    token = _GRPC_REQUEST_DATA.set(MessageToDict(request))
    try:
        await handler.abort_context(WireError(message=f"failed at {server_path}"), object())
    finally:
        _GRPC_REQUEST_DATA.reset(token)

    assert handler.aborted_error.message == "failed at [PATH]"


@pytest.mark.asyncio
async def test_grpc_jsonrpc_stream_returns_final_public_error_for_dispatch_failure() -> None:
    class FailingStreamDispatcher:
        async def dispatch_stream(self, payload):
            yield {"jsonrpc": "2.0", "id": payload["id"], "result": {"state": "working"}}
            raise RuntimeError("stream failed with api_key=sk-live at /Users/alice/.iac-code/settings.yml")

    class OpenContext:
        def cancelled(self) -> bool:
            return False

    servicer = _JsonRpcServicer.__new__(_JsonRpcServicer)
    servicer._dispatcher = FailingStreamDispatcher()

    envelopes = [
        envelope
        async for envelope in servicer.Stream(
            JsonRpcEnvelope(payload=b'{"jsonrpc":"2.0","id":"stream-1"}'),
            OpenContext(),
        )
    ]
    payloads = [json.loads(envelope.payload.decode("utf-8")) for envelope in envelopes]

    assert envelopes[0].final is False
    assert envelopes[1].final is True
    assert payloads[1]["id"] == "stream-1"
    assert payloads[1]["error"]["code"] == -32603
    assert "sk-live" in payloads[1]["error"]["message"]
    assert "/Users/alice" in payloads[1]["error"]["message"]
    assert payloads[1]["error"]["data"]["error_id"]


@pytest.mark.asyncio
async def test_official_grpc_server_registers_a2a_service(monkeypatch, tmp_path) -> None:
    registered: dict[str, object] = {}

    class FakeServer:
        def add_insecure_port(self, address: str) -> None:
            registered["address"] = address

        async def start(self) -> None:
            registered["started"] = True

        async def wait_for_termination(self) -> None:
            registered["waited"] = True

        async def stop(self, grace: int) -> None:
            registered["stopped"] = grace

    class FakeAio:
        @staticmethod
        def server() -> FakeServer:
            return FakeServer()

    class FakeGrpcModule:
        aio = FakeAio

    def fake_register(servicer, server) -> None:
        registered["servicer_type"] = type(servicer).__name__
        registered["server"] = server

    monkeypatch.setattr("iac_code.a2a.transports.grpc.require_grpc", lambda: FakeGrpcModule)
    monkeypatch.setattr("a2a.types.a2a_pb2_grpc.add_A2AServiceServicer_to_server", fake_register)

    components = create_runtime_components(
        model="qwen3.6-plus",
        host="127.0.0.1",
        port=41242,
        persistence_dir=tmp_path / "state",
        artifact_dir=tmp_path / "artifacts",
    )
    server = GrpcA2AServer(components=components, host="127.0.0.1", port=41243)

    await server.serve()
    await server.aclose()

    assert registered["address"] == "127.0.0.1:41243"
    assert registered["servicer_type"] == "ProjectingGrpcHandler"
    assert registered["started"] is True
    assert registered["waited"] is True
