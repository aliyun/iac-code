"""HTTP+SSE transport for ACP server.

Bridges HTTP POST/GET/DELETE requests to ``acp.run_agent()`` via in-memory
asyncio pipes using a *pipe-bridge* pattern::

    HTTP Client
          |
    Starlette ASGI App
          |
          +-- POST /acp -----> StreamReader (requests) -----> acp.run_agent(server)
          |                                                          |
          +-- GET  /acp <----- StreamReader (responses) <----- (responses/notifications)
          |
          +-- DELETE /acp ---> close connection
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
import logging
import os
import uuid
from enum import Enum
from typing import Any

from starlette.applications import Starlette
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from iac_code.acp.server import ACPServer

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Transport type enum
# ---------------------------------------------------------------------------


class TransportType(str, Enum):
    """Supported ACP transport types."""

    STDIO = "stdio"
    HTTP = "http"


# ---------------------------------------------------------------------------
# In-memory transport: StreamWriter that feeds into a StreamReader
# ---------------------------------------------------------------------------


class _MemoryTransport(asyncio.Transport):
    """A custom :class:`asyncio.Transport` that feeds written bytes into an
    :class:`asyncio.StreamReader`, enabling creation of an in-memory
    :class:`asyncio.StreamWriter` / :class:`asyncio.StreamReader` pair.
    """

    def __init__(self, reader: asyncio.StreamReader) -> None:
        super().__init__()
        self._reader = reader
        self._closing = False

    def write(self, data: bytes | bytearray | memoryview) -> None:
        if not self._closing:
            # ``StreamReader.feed_data`` requires an immutable bytes-like
            # buffer, so coerce bytearray / memoryview into ``bytes``.
            self._reader.feed_data(bytes(data))

    def is_closing(self) -> bool:
        return self._closing

    def close(self) -> None:
        self._closing = True
        self._reader.feed_eof()

    def get_extra_info(self, name: str, default: Any = None) -> Any:
        return default


def _create_memory_stream_pair() -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    """Create an in-memory (StreamReader, StreamWriter) pair.

    Data written to the returned *StreamWriter* can be read from the returned
    *StreamReader*.
    """
    reader = asyncio.StreamReader()
    transport = _MemoryTransport(reader)
    protocol = asyncio.StreamReaderProtocol(asyncio.StreamReader())
    writer = asyncio.StreamWriter(transport, protocol, reader, asyncio.get_running_loop())
    return reader, writer


# ---------------------------------------------------------------------------
# HTTPConnectionBridge
# ---------------------------------------------------------------------------

_SSE_QUEUE_MAX_SIZE = 1024


class HTTPConnectionBridge:
    """Bridges a single HTTP client connection to ``acp.run_agent()``
    via in-memory asyncio stream pairs.

    Lifecycle:
    1. ``start()`` — creates pipes and launches ``run_agent`` in a background
       task plus an output-reader task.
    2. ``send_message(msg)`` — feeds a JSON-RPC message (from HTTP POST) into
       the request pipe so the agent can process it.
    3. The output-reader task routes agent responses/notifications to either
       ``_init_response`` (for the synchronous *initialize* round-trip) or
       ``_sse_queue`` (for SSE streaming).
    4. ``close()`` — cancels background tasks and releases resources.
    """

    def __init__(self) -> None:
        self.connection_id: str = str(uuid.uuid4())
        self.server: ACPServer = ACPServer()

        # Pipe: HTTP POST -> run_agent  (requests)
        # request_reader is the output_stream param for run_agent
        self._request_reader: asyncio.StreamReader | None = None

        # Pipe: run_agent -> SSE  (responses / notifications)
        # response_reader is read by _read_output; response_writer is
        # the input_stream param for run_agent
        self._response_reader: asyncio.StreamReader | None = None
        self._response_writer: asyncio.StreamWriter | None = None

        self._agent_task: asyncio.Task[None] | None = None
        self._output_task: asyncio.Task[None] | None = None
        self._sse_queue: asyncio.Queue[str | None] = asyncio.Queue(maxsize=_SSE_QUEUE_MAX_SIZE)
        self._initialized: asyncio.Event = asyncio.Event()
        self._init_response: str | None = None
        self._closed: bool = False
        self._pending_close_tasks: set[asyncio.Task[None]] = set()

    # -- lifecycle -----------------------------------------------------------

    async def start(self) -> None:
        """Create pipes and start ``run_agent`` in a background task."""
        # Pipe for requests: HTTP POST writes -> agent reads
        self._request_reader = asyncio.StreamReader()

        # Pipe for responses: agent writes -> SSE/init reader reads
        self._response_reader, self._response_writer = _create_memory_stream_pair()

        # Start the agent background task
        self._agent_task = asyncio.create_task(self._run_agent(), name=f"acp-agent-{self.connection_id[:8]}")

        # Start the output reader
        self._output_task = asyncio.create_task(self._read_output(), name=f"acp-output-{self.connection_id[:8]}")

    async def send_message(self, message: str) -> None:
        """Feed a JSON-RPC message (from HTTP POST) into the request pipe."""
        if self._request_reader is None:
            raise RuntimeError("Connection not started")
        # ACP SDK uses newline-delimited JSON framing
        data = message.strip() + "\n"
        self._request_reader.feed_data(data.encode("utf-8"))

    async def close(self) -> None:
        """Shut down the connection and release all resources."""
        if self._closed:
            return
        self._closed = True

        # Signal SSE stream to close
        await self._sse_queue.put(None)

        # Close the request pipe so run_agent's readline() returns empty
        if self._request_reader is not None:
            self._request_reader.feed_eof()

        # Cancel background tasks
        for task in (self._agent_task, self._output_task):
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

        # Close the response writer
        if self._response_writer is not None:
            self._response_writer.close()

        # Cancel any pending close tasks (avoid recursive await of self)
        for t in list(self._pending_close_tasks):
            t.cancel()
        for t in list(self._pending_close_tasks):
            with contextlib.suppress(asyncio.CancelledError):
                await t
        self._pending_close_tasks.clear()

        # Shut down the ACP server (cleanup loop etc.)
        await self.server.shutdown()
        logger.info("Connection %s closed", self.connection_id[:8])

    # -- internal ------------------------------------------------------------

    async def _run_agent(self) -> None:
        """Run ``acp.run_agent`` with bridged streams."""
        import acp

        try:
            await acp.run_agent(
                self.server,
                # input_stream (StreamWriter): agent writes responses here
                input_stream=self._response_writer,
                # output_stream (StreamReader): agent reads requests from here
                output_stream=self._request_reader,
                use_unstable_protocol=True,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("run_agent failed for connection %s", self.connection_id[:8])
        finally:
            # Ensure SSE is notified even on unexpected exit
            if not self._closed:
                try:
                    self._sse_queue.put_nowait(None)
                except asyncio.QueueFull:
                    pass

    async def _read_output(self) -> None:
        """Read agent output pipe and route messages to SSE queue or init response."""
        if self._response_reader is None:
            return
        fatal_error = False
        try:
            while not self._closed:
                line = await self._response_reader.readline()
                if not line:
                    break
                message = line.decode("utf-8").strip()
                if not message:
                    continue

                # The first response is the initialize reply — deliver it
                # synchronously so the POST handler can return it.
                if not self._initialized.is_set():
                    self._init_response = message
                    self._initialized.set()
                else:
                    try:
                        # Apply backpressure: block until space is available
                        # (up to a timeout) rather than silently discarding.
                        await asyncio.wait_for(self._sse_queue.put(message), timeout=30.0)
                    except asyncio.TimeoutError:
                        # Client is not consuming events within a reasonable
                        # window — send an error notification and tear down.
                        logger.error(
                            "SSE queue full for connection %s: client not consuming events, closing",
                            self.connection_id[:8],
                        )
                        error_event = json.dumps(
                            {
                                "jsonrpc": "2.0",
                                "method": "notifications/cancelled",
                                "params": {
                                    "reason": "SSE queue full: client is not consuming events",
                                },
                            }
                        )
                        # Make room by discarding the oldest pending event so
                        # the error notification reaches the client.
                        try:
                            self._sse_queue.get_nowait()
                        except asyncio.QueueEmpty:
                            pass
                        try:
                            self._sse_queue.put_nowait(error_event)
                        except asyncio.QueueFull:
                            pass
                        # Break out of the read loop — the finally block will
                        # send the sentinel and trigger connection teardown.
                        fatal_error = True
                        break
        except asyncio.CancelledError:
            raise
        except Exception:
            # An unrecoverable error occurred while reading from the agent.
            # Continuing would leak the connection and leave clients waiting
            # forever, so tear it down and let the SSE side observe EOF.
            logger.exception("Output reader failed for connection %s", self.connection_id[:8])
            fatal_error = True
        finally:
            if not self._closed:
                try:
                    self._sse_queue.put_nowait(None)
                except asyncio.QueueFull:
                    pass
            if fatal_error and not self._closed:
                # Drop this connection from the global pool as well so a new
                # initialize is required to recover. close() is idempotent.
                _connections.pop(self.connection_id, None)
                # Schedule close so we don't await self from inside our own task.
                task = asyncio.create_task(
                    self.close(),
                    name=f"acp-close-after-error-{self.connection_id[:8]}",
                )
                self._pending_close_tasks.add(task)
                task.add_done_callback(self._pending_close_tasks.discard)


# ---------------------------------------------------------------------------
# Connection pool
# ---------------------------------------------------------------------------

_connections: dict[str, HTTPConnectionBridge] = {}


# ---------------------------------------------------------------------------
# Starlette route handlers
# ---------------------------------------------------------------------------

_ACP_CONN_HEADER = "Acp-Connection-Id"
_INIT_TIMEOUT = 30.0


async def _handle_post(request: Request) -> Response:
    """Handle ``POST /acp`` — JSON-RPC requests from the client."""
    body = await request.body()
    message = body.decode("utf-8")

    try:
        parsed: dict[str, Any] = json.loads(message)
    except json.JSONDecodeError:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    method = parsed.get("method", "")
    conn_id = request.headers.get(_ACP_CONN_HEADER.lower())

    if method == "initialize":
        # Create a new connection bridge
        bridge = HTTPConnectionBridge()
        await bridge.start()
        await bridge.send_message(message)

        try:
            await asyncio.wait_for(bridge._initialized.wait(), timeout=_INIT_TIMEOUT)
        except asyncio.TimeoutError:
            await bridge.close()
            return JSONResponse({"error": "Initialize timeout"}, status_code=504)

        _connections[bridge.connection_id] = bridge
        logger.info("New HTTP connection %s", bridge.connection_id[:8])

        init_response = bridge._init_response
        if init_response is None:
            await bridge.close()
            return JSONResponse({"error": "Initialize produced no response"}, status_code=502)

        return JSONResponse(
            content=json.loads(init_response),
            headers={_ACP_CONN_HEADER: bridge.connection_id},
        )

    # Non-initialize requests require an existing connection
    if not conn_id or conn_id not in _connections:
        return JSONResponse(
            {"error": "Connection not found. Send 'initialize' first or provide a valid Acp-Connection-Id header."},
            status_code=400,
        )

    bridge = _connections[conn_id]
    await bridge.send_message(message)

    # Return 202 Accepted — the real response arrives over the SSE stream
    return Response(status_code=202)


async def _handle_get(request: Request) -> Response:
    """Handle ``GET /acp`` — SSE stream for server-initiated messages."""
    conn_id = request.headers.get(_ACP_CONN_HEADER.lower())
    if not conn_id or conn_id not in _connections:
        return JSONResponse(
            {"error": "Connection not found. Provide a valid Acp-Connection-Id header."},
            status_code=400,
        )

    bridge = _connections[conn_id]

    async def event_stream():  # type: ignore[return]
        event_id = 0
        try:
            while True:
                message = await bridge._sse_queue.get()
                if message is None:
                    break
                event_id += 1
                yield f"event: message\ndata: {message}\nid: {event_id}\nretry: 5000\n\n"
        finally:
            logger.debug("SSE stream closed for connection %s", conn_id[:8])

    from starlette.responses import StreamingResponse

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


async def _handle_delete(request: Request) -> Response:
    """Handle ``DELETE /acp`` — close a connection."""
    conn_id = request.headers.get(_ACP_CONN_HEADER.lower())
    if conn_id and conn_id in _connections:
        bridge = _connections.pop(conn_id)
        await bridge.close()
    return Response(status_code=200)


async def _handle_health(request: Request) -> Response:
    """Handle ``GET /health`` — simple health check endpoint."""
    return JSONResponse({"status": "healthy"})


# ---------------------------------------------------------------------------
# Bearer-token authentication middleware
# ---------------------------------------------------------------------------


class _BearerTokenMiddleware(BaseHTTPMiddleware):
    """Reject requests when ``IACCODE_ACP_HTTP_TOKEN`` is set and the
    ``Authorization: Bearer <token>`` header doesn't match.
    """

    async def dispatch(self, request: Request, call_next):  # type: ignore[override]
        token = os.environ.get("IACCODE_ACP_HTTP_TOKEN")
        if token:
            auth = request.headers.get("authorization", "")
            # Constant-time comparison to mitigate timing attacks.
            if not auth.startswith("Bearer ") or not hmac.compare_digest(auth[7:], token):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
        return await call_next(request)


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------


async def _cleanup_connections() -> None:
    """Close every open connection and shut down their servers."""
    for bridge in list(_connections.values()):
        await bridge.close()
    _connections.clear()
    logger.info("All HTTP connections cleaned up during shutdown")


def create_app() -> Starlette:
    """Create a Starlette ASGI application for the HTTP+SSE transport."""
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _lifespan(app: Starlette):
        yield
        await _cleanup_connections()

    routes = [
        Route("/acp", _handle_post, methods=["POST"]),
        Route("/acp", _handle_get, methods=["GET"]),
        Route("/acp", _handle_delete, methods=["DELETE"]),
        Route("/health", _handle_health, methods=["GET"]),
    ]

    app = Starlette(routes=routes, lifespan=_lifespan)
    app.add_middleware(_BearerTokenMiddleware)
    return app
