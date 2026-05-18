import json

import pytest

from iac_code.a2a.transports.base import A2ATransportDependencyError
from iac_code.a2a.transports.dispatcher import create_runtime_components
from iac_code.a2a.transports.redis_streams import (
    RedisStreamsA2AClient,
    RedisStreamsA2AServer,
    RedisStreamsMessage,
    parse_redis_entry,
    require_redis,
)
from iac_code.types.stream_events import TextDeltaEvent

from .fakes import FakeAgentLoop, FakeRuntime


class FakeRedis:
    def __init__(self) -> None:
        self.streams = {}
        self.closed = False
        self.acked: list[tuple[str, str, str]] = []
        self.created_groups: list[tuple[str, str, str, bool]] = []
        self.read_groups: list[tuple[str, str, dict[str, str], int, int]] = []

    async def xadd(self, stream, fields):
        self.streams.setdefault(stream, []).append(fields)
        return "1-0"

    async def xread(self, streams, count=1, block=0):
        stream = next(iter(streams))
        items = self.streams.get(stream, [])
        if not items:
            return []
        return [(stream, [("1-0", items.pop(0))])]

    async def aclose(self):
        self.closed = True

    async def xgroup_create(self, name, groupname, id="0-0", mkstream=False):
        self.created_groups.append((name, groupname, id, mkstream))

    async def xreadgroup(self, groupname, consumername, streams, count=1, block=0):
        self.read_groups.append((groupname, consumername, streams, count, block))
        stream = next(iter(streams))
        items = self.streams.get(stream, [])
        if not items:
            return []
        return [(stream, [("1-0", items.pop(0))])]

    async def xack(self, stream, group, entry_id):
        self.acked.append((stream, group, entry_id))
        return 1


def test_parse_redis_entry_decodes_payload() -> None:
    message = parse_redis_entry(
        "1-0",
        {"correlation_id": "corr-1", "payload": json.dumps({"jsonrpc": "2.0", "id": "1"}), "final": "true"},
    )

    assert message == RedisStreamsMessage(
        entry_id="1-0",
        correlation_id="corr-1",
        payload={"jsonrpc": "2.0", "id": "1"},
        final=True,
    )


def test_require_redis_reports_missing_dependency(monkeypatch) -> None:
    def fail_redis_import(name):
        if name == "redis.asyncio":
            raise ModuleNotFoundError(name)
        raise AssertionError(name)

    monkeypatch.setattr("iac_code.a2a.transports.redis_streams.import_module", fail_redis_import)

    with pytest.raises(A2ATransportDependencyError, match="iac-code\\[a2a-redis\\]"):
        require_redis()


@pytest.mark.asyncio
async def test_redis_client_sends_and_reads_response() -> None:
    redis = FakeRedis()
    client = RedisStreamsA2AClient(
        redis=redis,
        request_stream="requests",
        response_stream="responses",
        timeout_seconds=1,
    )
    await redis.xadd(
        "responses",
        {
            "correlation_id": "corr-fixed",
            "payload": json.dumps({"jsonrpc": "2.0", "id": "1", "result": {"ok": True}}),
            "final": "true",
        },
    )

    response = await client.send({"jsonrpc": "2.0", "id": "1", "method": "message/send"}, correlation_id="corr-fixed")

    assert response["result"]["ok"] is True
    assert redis.streams["requests"][0]["correlation_id"] == "corr-fixed"


@pytest.mark.asyncio
async def test_redis_server_processes_one_unary_request(monkeypatch, tmp_path) -> None:
    redis = FakeRedis()
    loop = FakeAgentLoop([TextDeltaEvent(text="redis ok")])
    runtime = FakeRuntime(agent_loop=loop, session_id="session-1")
    monkeypatch.setattr("iac_code.a2a.executor.create_agent_runtime", lambda options: runtime)
    components = create_runtime_components(model="qwen3.6-plus", host="127.0.0.1", port=41242)
    server = RedisStreamsA2AServer(
        redis=redis,
        components=components,
        request_stream="requests",
        response_stream="responses",
        consumer_group="iac-code",
    )
    await redis.xadd(
        "requests",
        {
            "correlation_id": "corr-server",
            "reply_stream": "responses",
            "payload": json.dumps(
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
        },
    )

    assert await server.serve_once() is True

    response = parse_redis_entry("1-0", redis.streams["responses"][0])
    assert response.correlation_id == "corr-server"
    assert response.final is True
    assert response.payload["result"]["status"]["state"] == "input-required"
    assert redis.acked == [("requests", "iac-code", "1-0")]
    await server.aclose()


@pytest.mark.asyncio
async def test_redis_server_creates_group_and_reads_with_consumer_group(monkeypatch, tmp_path) -> None:
    redis = FakeRedis()
    loop = FakeAgentLoop([TextDeltaEvent(text="redis ok")])
    runtime = FakeRuntime(agent_loop=loop, session_id="session-1")
    monkeypatch.setattr("iac_code.a2a.executor.create_agent_runtime", lambda options: runtime)
    components = create_runtime_components(model="qwen3.6-plus", host="127.0.0.1", port=41242)
    server = RedisStreamsA2AServer(
        redis=redis,
        components=components,
        request_stream="requests",
        response_stream="responses",
        consumer_group="iac-code",
        consumer_name="worker-1",
    )
    await redis.xadd(
        "requests",
        {
            "correlation_id": "corr-server",
            "reply_stream": "responses",
            "payload": json.dumps(
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
        },
    )

    assert await server.serve_once() is True

    assert redis.created_groups == [("requests", "iac-code", "0-0", True)]
    assert redis.read_groups == [("iac-code", "worker-1", {"requests": ">"}, 1, 100)]
    assert redis.acked == [("requests", "iac-code", "1-0")]
    await server.aclose()


@pytest.mark.asyncio
async def test_redis_server_acks_request_when_dispatch_fails() -> None:
    class FailingDispatcher:
        async def dispatch(self, payload):
            raise RuntimeError("dispatch failed")

    redis = FakeRedis()
    components = create_runtime_components(model="qwen3.6-plus", host="127.0.0.1", port=41242)
    server = RedisStreamsA2AServer(
        redis=redis,
        components=components,
        request_stream="requests",
        response_stream="responses",
        consumer_group="iac-code",
    )
    server._dispatcher = FailingDispatcher()
    fields = {
        "correlation_id": "corr-server",
        "reply_stream": "responses",
        "payload": json.dumps({"jsonrpc": "2.0", "id": "1", "method": "message/send"}),
    }

    with pytest.raises(RuntimeError, match="dispatch failed"):
        await server._process_entry("9-0", fields)

    assert redis.acked == [("requests", "iac-code", "9-0")]
    await server.aclose()


@pytest.mark.asyncio
async def test_redis_client_reconnects_after_read_error() -> None:
    class FailingRedis(FakeRedis):
        async def xread(self, streams, count=1, block=0):
            raise RuntimeError("connection lost")

    replacement = FakeRedis()
    await replacement.xadd(
        "responses",
        {
            "correlation_id": "corr-fixed",
            "payload": json.dumps({"jsonrpc": "2.0", "id": "1", "result": {"ok": True}}),
            "final": "true",
        },
    )
    created = []

    async def redis_factory():
        created.append(replacement)
        return replacement

    client = RedisStreamsA2AClient(
        redis=FailingRedis(),
        request_stream="requests",
        response_stream="responses",
        timeout_seconds=1,
        redis_factory=redis_factory,
    )

    response = await client.send({"jsonrpc": "2.0", "id": "1", "method": "message/send"}, correlation_id="corr-fixed")

    assert response["result"]["ok"] is True
    assert created == [replacement]


@pytest.mark.asyncio
async def test_redis_client_bounds_xread_with_wait_for(monkeypatch: pytest.MonkeyPatch) -> None:
    redis = FakeRedis()
    await redis.xadd(
        "responses",
        {
            "correlation_id": "corr-fixed",
            "payload": json.dumps({"jsonrpc": "2.0", "id": "1", "result": {"ok": True}}),
            "final": "true",
        },
    )
    wait_for_timeouts = []

    async def fake_wait_for(awaitable, timeout):
        wait_for_timeouts.append(timeout)
        return await awaitable

    monkeypatch.setattr("iac_code.a2a.transports.redis_streams.asyncio.wait_for", fake_wait_for)
    client = RedisStreamsA2AClient(
        redis=redis,
        request_stream="requests",
        response_stream="responses",
        timeout_seconds=1,
    )

    response = await client.send({"jsonrpc": "2.0", "id": "1", "method": "message/send"}, correlation_id="corr-fixed")

    assert response["result"]["ok"] is True
    assert wait_for_timeouts
    assert all(0 < timeout <= 1 for timeout in wait_for_timeouts)
