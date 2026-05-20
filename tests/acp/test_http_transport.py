"""Tests for HTTP+SSE transport (tests/acp/test_http_transport.py).

Covers: basic protocol, SSE streaming, connection management, and bearer-token auth.
Uses ``httpx.AsyncClient`` + ``ASGITransport`` — no real HTTP server started.
"""

from __future__ import annotations

import asyncio
import json
import os
from unittest.mock import patch

import httpx
import pytest

from iac_code.acp.http_sse import (
    HTTPConnectionBridge,
    _cleanup_connections,
    _connections,
    _create_memory_stream_pair,
    _MemoryTransport,
    create_app,
)

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

_INIT_REQUEST = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {"capabilities": {}},
}

_INIT_RESPONSE = {
    "jsonrpc": "2.0",
    "id": 1,
    "result": {"capabilities": {}, "serverInfo": {"name": "iac-code"}},
}

_SESSION_NEW_REQUEST = {
    "jsonrpc": "2.0",
    "id": 2,
    "method": "session/new",
    "params": {},
}


def _make_fake_run_agent():
    """Return a coroutine that mimics ``acp.run_agent``.

    It reads JSON-RPC messages from *output_stream* (the request pipe) and
    writes matching responses to *input_stream* (the response pipe).
    Only the ``initialize`` method is actually echoed back; other messages
    are answered with a simple result so they appear on the SSE queue.
    """

    async def _fake_run_agent(server, *, input_stream=None, output_stream=None, **_kw):
        """Minimal agent stub that reads requests and writes responses."""
        reader = output_stream  # StreamReader — requests from HTTP POST
        writer = input_stream  # StreamWriter — responses to SSE / init

        while True:
            line = await reader.readline()
            if not line:
                break
            text = line.decode("utf-8").strip()
            if not text:
                continue
            try:
                msg = json.loads(text)
            except json.JSONDecodeError:
                continue

            method = msg.get("method", "")
            msg_id = msg.get("id")

            if method == "initialize":
                resp = json.dumps(_INIT_RESPONSE)
            else:
                resp = json.dumps({"jsonrpc": "2.0", "id": msg_id, "result": {"echo": method}})

            writer.write((resp + "\n").encode("utf-8"))
            await writer.drain()

    return _fake_run_agent


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clear_connections():
    """Ensure the global connection pool is empty before/after each test."""
    _connections.clear()
    yield
    # Close any remaining bridges synchronously-ish
    for bridge in list(_connections.values()):
        bridge._closed = True
    _connections.clear()


@pytest.fixture()
def app():
    """Create a fresh Starlette app with ``acp.run_agent`` mocked."""
    with patch("acp.run_agent", side_effect=_make_fake_run_agent()):
        yield create_app()


@pytest.fixture()
def raw_app():
    """Create a Starlette app *without* mocking — for unit-level bridge tests."""
    return create_app()


async def _async_client(app):
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://testserver")


async def _initialize(client: httpx.AsyncClient, extra_headers: dict | None = None):
    """Send an initialize request and return (response, connection_id)."""
    headers = {"Content-Type": "application/json"}
    if extra_headers:
        headers.update(extra_headers)
    resp = await client.post("/acp", json=_INIT_REQUEST, headers=headers)
    conn_id = resp.headers.get("acp-connection-id")
    return resp, conn_id


# ---------------------------------------------------------------------------
# A. Basic protocol scenarios
# ---------------------------------------------------------------------------


class TestBasicProtocol:
    """A1-A4: initialize, non-init POST, invalid JSON, missing connection id."""

    @pytest.mark.asyncio
    async def test_initialize_returns_200_with_connection_id(self, app):
        """A1: POST initialize -> 200 + Acp-Connection-Id + valid JSON-RPC."""
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
            resp, conn_id = await _initialize(client)

        assert resp.status_code == 200
        assert conn_id is not None and len(conn_id) > 0
        body = resp.json()
        assert "result" in body
        assert body.get("jsonrpc") == "2.0"

    @pytest.mark.asyncio
    async def test_non_initialize_returns_202(self, app):
        """A2: After init, a non-initialize POST returns 202 Accepted."""
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
            _, conn_id = await _initialize(client)
            resp = await client.post(
                "/acp",
                json=_SESSION_NEW_REQUEST,
                headers={"Acp-Connection-Id": conn_id},
            )

        assert resp.status_code == 202

    @pytest.mark.asyncio
    async def test_invalid_json_returns_400(self, app):
        """A3: POST with invalid JSON body returns 400."""
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
            resp = await client.post("/acp", content=b"not-json{{{", headers={"Content-Type": "application/json"})

        assert resp.status_code == 400
        assert "error" in resp.json()

    @pytest.mark.asyncio
    async def test_missing_connection_id_returns_400(self, app):
        """A4: Non-initialize POST without Acp-Connection-Id returns 400."""
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
            resp = await client.post("/acp", json=_SESSION_NEW_REQUEST)

        assert resp.status_code == 400
        body = resp.json()
        assert "error" in body


# ---------------------------------------------------------------------------
# B. SSE stream scenarios
# ---------------------------------------------------------------------------


class TestSSEStream:
    """B5-B6: SSE event reception and format."""

    @pytest.mark.asyncio
    async def test_sse_stream_receives_events(self, app):
        """B5: SSE stream delivers JSON-RPC responses for non-init messages."""
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
            _, conn_id = await _initialize(client)

            # Send a non-init request so the agent writes a response to SSE queue
            await client.post(
                "/acp",
                json=_SESSION_NEW_REQUEST,
                headers={"Acp-Connection-Id": conn_id},
            )

            # Give the fake agent a moment to process
            await asyncio.sleep(0.15)

            # Signal the SSE stream to end *before* opening it so it drains
            bridge = _connections[conn_id]
            await bridge._sse_queue.put(None)

            resp = await client.get("/acp", headers={"Acp-Connection-Id": conn_id})

        full = resp.text
        assert "event: message" in full
        assert "data:" in full
        assert "id:" in full

    @pytest.mark.asyncio
    async def test_sse_event_format(self, app):
        """B6: Each SSE event contains event/data/id/retry fields."""
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
            _, conn_id = await _initialize(client)

            await client.post(
                "/acp",
                json=_SESSION_NEW_REQUEST,
                headers={"Acp-Connection-Id": conn_id},
            )
            await asyncio.sleep(0.15)

            bridge = _connections[conn_id]
            await bridge._sse_queue.put(None)

            resp = await client.get("/acp", headers={"Acp-Connection-Id": conn_id})

        full = resp.text
        assert "event: message" in full
        assert "data: " in full
        assert "id: " in full
        assert "retry: 5000" in full


# ---------------------------------------------------------------------------
# C. Connection management scenarios
# ---------------------------------------------------------------------------


class TestConnectionManagement:
    """C7-C9: DELETE, resource release, multi-client isolation."""

    @pytest.mark.asyncio
    async def test_delete_closes_connection(self, app):
        """C7: DELETE /acp closes connection; reuse returns error."""
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
            _, conn_id = await _initialize(client)
            assert conn_id in _connections

            del_resp = await client.request("DELETE", "/acp", headers={"Acp-Connection-Id": conn_id})
            assert del_resp.status_code == 200

            # Same connection_id should now be rejected
            resp = await client.post(
                "/acp",
                json=_SESSION_NEW_REQUEST,
                headers={"Acp-Connection-Id": conn_id},
            )
            assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_resource_release_after_delete(self, app):
        """C8: After DELETE the connection is removed from the pool."""
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
            _, conn_id = await _initialize(client)
            assert conn_id in _connections

            await client.request("DELETE", "/acp", headers={"Acp-Connection-Id": conn_id})
            assert conn_id not in _connections

    @pytest.mark.asyncio
    async def test_multi_client_isolation(self, app):
        """C9: Two independent connections get different ids and don't interfere."""
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
            _, conn1 = await _initialize(client)
            _, conn2 = await _initialize(client)

            assert conn1 != conn2
            assert conn1 in _connections
            assert conn2 in _connections

            # Delete one — the other should still work
            await client.request("DELETE", "/acp", headers={"Acp-Connection-Id": conn1})
            assert conn1 not in _connections
            assert conn2 in _connections

            # conn2 still accepts requests
            resp = await client.post(
                "/acp",
                json=_SESSION_NEW_REQUEST,
                headers={"Acp-Connection-Id": conn2},
            )
            assert resp.status_code == 202


# ---------------------------------------------------------------------------
# D. Authentication scenarios
# ---------------------------------------------------------------------------


class TestAuthentication:
    """D10-D12: Bearer token auth success, failure, open access."""

    @pytest.mark.asyncio
    async def test_bearer_token_auth_success(self, app):
        """D10: Correct bearer token allows access."""
        with patch.dict(os.environ, {"IACCODE_ACP_HTTP_TOKEN": "test-secret-token"}):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://testserver"
            ) as client:
                resp, conn_id = await _initialize(client, extra_headers={"Authorization": "Bearer test-secret-token"})

            assert resp.status_code == 200
            assert conn_id is not None

    @pytest.mark.asyncio
    async def test_bearer_token_auth_failure_wrong_token(self, app):
        """D11a: Wrong bearer token returns 401."""
        with patch.dict(os.environ, {"IACCODE_ACP_HTTP_TOKEN": "test-secret-token"}):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://testserver"
            ) as client:
                resp = await client.post(
                    "/acp",
                    json=_INIT_REQUEST,
                    headers={"Authorization": "Bearer wrong-token"},
                )

            assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_bearer_token_auth_failure_no_header(self, app):
        """D11b: Missing Authorization header returns 401 when token is set."""
        with patch.dict(os.environ, {"IACCODE_ACP_HTTP_TOKEN": "test-secret-token"}):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://testserver"
            ) as client:
                resp = await client.post("/acp", json=_INIT_REQUEST)

            assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_open_access_when_no_token_set(self, app):
        """D12: When IACCODE_ACP_HTTP_TOKEN is not set, requests pass through."""
        with patch.dict(os.environ, {}, clear=False):
            # Ensure the env var is absent
            os.environ.pop("IACCODE_ACP_HTTP_TOKEN", None)
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://testserver"
            ) as client:
                resp, conn_id = await _initialize(client)

            assert resp.status_code == 200
            assert conn_id is not None


# ---------------------------------------------------------------------------
# E. _MemoryTransport unit tests
# ---------------------------------------------------------------------------


class TestMemoryTransport:
    """E13-E16: _MemoryTransport write, is_closing, close, get_extra_info."""

    @pytest.mark.asyncio
    async def test_write_feeds_data_to_reader(self):
        """E13: write() feeds bytes into the reader."""
        reader = asyncio.StreamReader()
        transport = _MemoryTransport(reader)
        transport.write(b"hello")
        # The data should be buffered in the reader
        assert reader._buffer == b"hello"  # noqa: SLF001

    @pytest.mark.asyncio
    async def test_write_ignored_when_closing(self):
        """E14: write() is a no-op after close()."""
        reader = asyncio.StreamReader()
        transport = _MemoryTransport(reader)
        transport.close()
        transport.write(b"should-be-ignored")
        # Buffer should be empty (only EOF fed)
        assert b"should-be-ignored" not in reader._buffer  # noqa: SLF001

    @pytest.mark.asyncio
    async def test_is_closing_reflects_state(self):
        """E15: is_closing() returns False initially, True after close()."""
        reader = asyncio.StreamReader()
        transport = _MemoryTransport(reader)
        assert transport.is_closing() is False
        transport.close()
        assert transport.is_closing() is True

    @pytest.mark.asyncio
    async def test_get_extra_info_returns_default(self):
        """E16: get_extra_info() always returns the provided default."""
        reader = asyncio.StreamReader()
        transport = _MemoryTransport(reader)
        assert transport.get_extra_info("peername") is None
        assert transport.get_extra_info("peername", "fallback") == "fallback"


# ---------------------------------------------------------------------------
# F. _create_memory_stream_pair
# ---------------------------------------------------------------------------


class TestMemoryStreamPair:
    """F17: Integration test for the memory stream pair factory."""

    @pytest.mark.asyncio
    async def test_round_trip(self):
        """F17: Data written to StreamWriter is readable from StreamReader."""
        reader, writer = _create_memory_stream_pair()
        writer.write(b"line1\n")
        await writer.drain()
        writer.close()
        line = await reader.readline()
        assert line == b"line1\n"


# ---------------------------------------------------------------------------
# G. HTTPConnectionBridge unit tests
# ---------------------------------------------------------------------------


class TestHTTPConnectionBridge:
    """G18-G24: Bridge lifecycle, send_message, close, _read_output edge cases."""

    @pytest.mark.asyncio
    async def test_send_message_before_start_raises(self):
        """G18: send_message() before start() raises RuntimeError."""
        bridge = HTTPConnectionBridge()
        with pytest.raises(RuntimeError, match="Connection not started"):
            await bridge.send_message('{"method": "test"}')

    @pytest.mark.asyncio
    async def test_close_is_idempotent(self):
        """G19: Calling close() twice does not error."""
        bridge = HTTPConnectionBridge()
        with patch.object(bridge.server, "shutdown", return_value=None) as mock_shutdown:
            bridge._closed = True
            await bridge.close()
            # server.shutdown should NOT be called on the second close
            mock_shutdown.assert_not_called()

    @pytest.mark.asyncio
    async def test_close_cleans_up_resources(self):
        """G20: close() cancels tasks, feeds EOF, closes writer, shuts down server."""
        bridge = HTTPConnectionBridge()
        with (
            patch("acp.run_agent", side_effect=_make_fake_run_agent()),
            patch.object(bridge.server, "shutdown", return_value=None) as mock_shutdown,
        ):
            await bridge.start()
            # Ensure tasks are running
            assert bridge._agent_task is not None
            assert bridge._output_task is not None

            await bridge.close()

            assert bridge._closed is True
            mock_shutdown.assert_awaited_once()
            # Tasks should be cancelled/done
            assert bridge._agent_task.done()
            assert bridge._output_task.done()

    @pytest.mark.asyncio
    async def test_read_output_returns_when_reader_is_none(self):
        """G21: _read_output exits immediately if _response_reader is None."""
        bridge = HTTPConnectionBridge()
        # _response_reader is None by default; should return without error
        await bridge._read_output()

    @pytest.mark.asyncio
    async def test_read_output_routes_init_then_sse(self):
        """G22: First message goes to _init_response; subsequent to _sse_queue."""
        bridge = HTTPConnectionBridge()
        bridge._response_reader = asyncio.StreamReader()

        # Feed two messages then EOF
        init_msg = '{"jsonrpc":"2.0","id":1,"result":{}}\n'
        sse_msg = '{"jsonrpc":"2.0","id":2,"result":{"data":"hello"}}\n'
        bridge._response_reader.feed_data(init_msg.encode())
        bridge._response_reader.feed_data(sse_msg.encode())
        bridge._response_reader.feed_eof()

        await bridge._read_output()

        assert bridge._initialized.is_set()
        assert bridge._init_response == init_msg.strip()
        # SSE queue should have the second message + None sentinel
        msg = await asyncio.wait_for(bridge._sse_queue.get(), timeout=1.0)
        assert msg == sse_msg.strip()

    @pytest.mark.asyncio
    async def test_read_output_skips_blank_lines(self):
        """G23: _read_output skips blank lines between messages."""
        bridge = HTTPConnectionBridge()
        bridge._response_reader = asyncio.StreamReader()

        bridge._response_reader.feed_data(b"\n\n")
        bridge._response_reader.feed_data(b'{"id":1}\n')
        bridge._response_reader.feed_eof()

        await bridge._read_output()

        assert bridge._init_response == '{"id":1}'

    @pytest.mark.asyncio
    async def test_read_output_handles_exception(self):
        """G24: Exception in _read_output is caught and SSE sentinel is sent."""
        bridge = HTTPConnectionBridge()
        reader = asyncio.StreamReader()
        bridge._response_reader = reader

        # Make readline raise an unexpected error
        async def _boom():
            raise ValueError("boom")

        reader.readline = _boom  # type: ignore[assignment]

        await bridge._read_output()  # should not raise

        # Sentinel should be enqueued
        msg = await asyncio.wait_for(bridge._sse_queue.get(), timeout=1.0)
        assert msg is None

    @pytest.mark.asyncio
    async def test_run_agent_exception_sends_sentinel(self):
        """G25: Exception in _run_agent sends None sentinel to SSE queue."""
        bridge = HTTPConnectionBridge()
        bridge._response_writer = asyncio.StreamReader()  # dummy, won't be used
        bridge._request_reader = asyncio.StreamReader()

        with patch("acp.run_agent", side_effect=RuntimeError("agent crashed")):
            await bridge._run_agent()

        msg = await asyncio.wait_for(bridge._sse_queue.get(), timeout=1.0)
        assert msg is None


# ---------------------------------------------------------------------------
# H. HTTP route edge cases
# ---------------------------------------------------------------------------


class TestHTTPRouteEdgeCases:
    """H26-H30: GET/DELETE with missing connection, cleanup, create_app."""

    @pytest.mark.asyncio
    async def test_get_without_connection_id_returns_400(self, app):
        """H26: GET /acp without Acp-Connection-Id returns 400."""
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
            resp = await client.get("/acp")
        assert resp.status_code == 400
        assert "error" in resp.json()

    @pytest.mark.asyncio
    async def test_get_with_invalid_connection_id_returns_400(self, app):
        """H27: GET /acp with unknown Acp-Connection-Id returns 400."""
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
            resp = await client.get("/acp", headers={"Acp-Connection-Id": "nonexistent"})
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_delete_without_connection_returns_200(self, app):
        """H28: DELETE /acp with no matching connection still returns 200."""
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
            resp = await client.request("DELETE", "/acp")
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_delete_with_unknown_id_returns_200(self, app):
        """H29: DELETE /acp with unknown id returns 200 (no-op)."""
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
            resp = await client.request("DELETE", "/acp", headers={"Acp-Connection-Id": "unknown"})
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_cleanup_connections_closes_all(self):
        """H30: _cleanup_connections closes every bridge and clears the pool."""
        bridge1 = HTTPConnectionBridge()
        bridge2 = HTTPConnectionBridge()
        with (
            patch.object(bridge1.server, "shutdown", return_value=None),
            patch.object(bridge2.server, "shutdown", return_value=None),
        ):
            _connections["a"] = bridge1
            _connections["b"] = bridge2
            await _cleanup_connections()
            assert len(_connections) == 0
            assert bridge1._closed is True
            assert bridge2._closed is True

    def test_create_app_returns_starlette_app(self):
        """H31: create_app() returns a Starlette instance with routes."""
        from starlette.applications import Starlette

        app = create_app()
        assert isinstance(app, Starlette)

    @pytest.mark.asyncio
    async def test_initialize_timeout_returns_504(self, raw_app):
        """H32: If initialize times out, POST returns 504."""

        # Patch run_agent to do nothing (never set _initialized)
        async def _stuck_agent(server, *, input_stream=None, output_stream=None, **_kw):
            await asyncio.sleep(999)

        with (
            patch("acp.run_agent", side_effect=_stuck_agent),
            patch("iac_code.acp.http_sse._INIT_TIMEOUT", 0.1),
        ):
            app = create_app()
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://testserver"
            ) as client:
                resp = await client.post("/acp", json=_INIT_REQUEST)

        assert resp.status_code == 504
        assert "timeout" in resp.json()["error"].lower()

    @pytest.mark.asyncio
    async def test_lifespan_cleanup(self):
        """H33: Starlette lifespan cleanup closes all connections."""
        with patch("acp.run_agent", side_effect=_make_fake_run_agent()):
            app = create_app()

            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://testserver"
            ) as client:
                _, conn_id = await _initialize(client)
                assert conn_id in _connections

            # Simulate lifespan shutdown by calling the cleanup
            await _cleanup_connections()
            assert len(_connections) == 0


# ---------------------------------------------------------------------------
# I. Bearer-token auth uses constant-time comparison + behaves correctly
# ---------------------------------------------------------------------------


class TestBearerTokenAuth:
    """I34-I37: Bearer-token middleware accepts the right token, rejects others,
    and uses ``hmac.compare_digest`` so comparison is constant-time."""

    @pytest.mark.asyncio
    async def test_missing_authorization_header_is_rejected(self, monkeypatch):
        """I34: With token configured, a request without Authorization is 401."""
        monkeypatch.setenv("IACCODE_ACP_HTTP_TOKEN", "s3cret")
        app = create_app()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
            resp = await client.get("/acp")
        assert resp.status_code == 401
        assert resp.json() == {"error": "Unauthorized"}

    @pytest.mark.asyncio
    async def test_wrong_token_is_rejected(self, monkeypatch):
        """I35: A Bearer header with a non-matching token is 401."""
        monkeypatch.setenv("IACCODE_ACP_HTTP_TOKEN", "s3cret")
        app = create_app()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
            resp = await client.get("/acp", headers={"Authorization": "Bearer nope"})
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_correct_token_passes_auth_layer(self, monkeypatch):
        """I36: Correct Bearer token passes auth (downstream may still 400, not 401)."""
        monkeypatch.setenv("IACCODE_ACP_HTTP_TOKEN", "s3cret")
        app = create_app()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
            resp = await client.get("/acp", headers={"Authorization": "Bearer s3cret"})
        # Auth passed; route handler then complains about missing connection id.
        assert resp.status_code == 400

    def test_middleware_uses_constant_time_comparison(self):
        """I37: ``_BearerTokenMiddleware`` must call ``hmac.compare_digest``.

        Detecting actual timing differences in a unit test is unreliable, so we
        instead assert the implementation uses the constant-time helper from
        the stdlib. This catches regressions to plain ``==`` comparisons.
        """
        import inspect

        from iac_code.acp import http_sse

        source = inspect.getsource(http_sse._BearerTokenMiddleware)
        assert "hmac.compare_digest" in source, (
            "Bearer token comparison must use hmac.compare_digest to mitigate timing attacks"
        )


# ---------------------------------------------------------------------------
# J. _read_output fatal errors tear down the connection
# ---------------------------------------------------------------------------


class TestReadOutputFatalError:
    """J38: When ``_read_output`` hits an unexpected exception, the bridge is
    closed and removed from the global connection pool so clients aren't left
    waiting forever for events that will never arrive.
    """

    @pytest.mark.asyncio
    async def test_fatal_error_removes_connection_and_closes_bridge(self):
        bridge = HTTPConnectionBridge()
        reader = asyncio.StreamReader()
        bridge._response_reader = reader

        async def _boom():
            raise ValueError("boom")

        reader.readline = _boom  # type: ignore[assignment]

        # Register the bridge in the global pool as if initialize() had run.
        _connections[bridge.connection_id] = bridge

        try:
            with patch.object(bridge.server, "shutdown", return_value=None):
                await bridge._read_output()  # should not raise
                # Sentinel must be enqueued so any waiting SSE consumer wakes up
                msg = await asyncio.wait_for(bridge._sse_queue.get(), timeout=1.0)
                assert msg is None

                # The connection should be removed from the global pool so a
                # subsequent request observes "connection not found" instead
                # of dialing a half-dead bridge.
                assert bridge.connection_id not in _connections

                # And close() should run as a follow-up task.
                # Give the loop one tick for ``asyncio.create_task`` to run.
                for _ in range(10):
                    if bridge._closed:
                        break
                    await asyncio.sleep(0.01)
                assert bridge._closed is True
        finally:
            _connections.pop(bridge.connection_id, None)


# ---------------------------------------------------------------------------
# Health endpoint
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_health_endpoint_returns_healthy():
    """GET /health returns 200 with {"status": "healthy"}."""
    app = create_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/health")
        assert resp.status_code == 200
        assert resp.json() == {"status": "healthy"}


@pytest.mark.asyncio
async def test_health_endpoint_with_auth_token():
    """GET /health is subject to bearer token middleware when configured."""
    app = create_app()
    transport = httpx.ASGITransport(app=app)
    with patch.dict(os.environ, {"IACCODE_ACP_HTTP_TOKEN": "secret-token"}):
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            # Without token — rejected by middleware
            resp = await client.get("/health")
            assert resp.status_code == 401

            # With valid token — succeeds
            resp = await client.get("/health", headers={"Authorization": "Bearer secret-token"})
            assert resp.status_code == 200
            assert resp.json() == {"status": "healthy"}
