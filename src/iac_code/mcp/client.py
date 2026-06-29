from __future__ import annotations

import asyncio
import concurrent.futures
import inspect
import os
import threading
from contextlib import AsyncExitStack
from pathlib import Path
from tempfile import TemporaryFile
from typing import Any, Awaitable, Callable, Protocol, cast
from urllib.parse import urlparse

from iac_code.i18n import _
from iac_code.mcp.errors import MCPConnectionError, MCPNeedsAuthError
from iac_code.mcp.oauth import get_oauth_access_token_async
from iac_code.mcp.storage import MCPSecretStorage
from iac_code.mcp.types import MCPConfigScope, MCPServerConfig, MCPTransport


class MCPClientProtocol(Protocol):
    async def connect(self) -> None: ...

    async def close(self) -> None: ...

    async def list_tools(self) -> Any: ...

    async def call_tool(self, name: str, arguments: dict[str, Any] | None = None, **kwargs: Any) -> Any: ...

    async def list_resources(self) -> Any: ...

    async def read_resource(self, uri: str) -> Any: ...

    async def list_prompts(self) -> Any: ...

    async def get_prompt(self, name: str, arguments: dict[str, str] | None = None) -> Any: ...


ListChangedCallback = Callable[[str], Awaitable[None] | None]

_MCP_CLIENT_MODULES_PRELOADED = False
_MCP_CLIENT_MODULES_PRELOAD_LOCK = threading.Lock()


class MCPClientAdapter:
    """Thin MCP Python SDK adapter."""

    def __init__(
        self,
        config: MCPServerConfig,
        *,
        roots: list[Path] | None = None,
        scope: MCPConfigScope | str | None = None,
        secret_storage: MCPSecretStorage | None = None,
        list_changed_callback: ListChangedCallback | None = None,
    ) -> None:
        self.config = config
        self.roots = [Path(root) for root in roots or []]
        self.scope = scope
        self._secret_storage = secret_storage or MCPSecretStorage()
        self._list_changed_callback = list_changed_callback
        self._stack: AsyncExitStack | None = None
        self._session: Any = None
        self.initialize_result: Any = None
        self.stderr_tail: str | None = None
        self._stderr_buffer: _BoundedTextBuffer | None = None
        self._worker_thread: threading.Thread | None = None
        self._worker_task: asyncio.Task[Any] | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._operations: asyncio.Queue[Any] | None = None
        self._close_timeout_seconds = 5.0

    async def connect(self) -> None:
        if self._worker_thread is not None and self._worker_thread.is_alive():
            return
        _ensure_mcp_client_modules_preloaded()
        ready: concurrent.futures.Future[None] = concurrent.futures.Future()
        thread = threading.Thread(
            target=self._run_worker_thread,
            args=(ready,),
            name="iac-code-mcp-{}".format(self.config.name),
            daemon=True,
        )
        self._worker_thread = thread
        thread.start()
        try:
            await asyncio.wrap_future(ready)
        except BaseException:
            if not ready.done():
                ready.cancel()
            raise

    async def close(self) -> None:
        thread = self._worker_thread
        loop = self._loop
        operations = self._operations
        worker_task = self._worker_task
        if thread is None:
            return
        force_cancel = self._session is None
        if loop is not None and operations is not None and thread.is_alive():
            try:
                running_loop = asyncio.get_running_loop()
            except RuntimeError:
                running_loop = None
            if running_loop is loop:
                await operations.put(None)
                if force_cancel and worker_task is not None:
                    worker_task.cancel()
                return
            try:
                await asyncio.wrap_future(asyncio.run_coroutine_threadsafe(operations.put(None), loop))
            except RuntimeError:
                pass
            if force_cancel and worker_task is not None and not worker_task.done():
                try:
                    loop.call_soon_threadsafe(worker_task.cancel)
                except RuntimeError:
                    pass
        if thread is not threading.current_thread():
            await asyncio.to_thread(thread.join, self._close_timeout_seconds)
        if not thread.is_alive() and self._worker_thread is thread:
            self._worker_thread = None

    def _run_worker_thread(self, ready: concurrent.futures.Future[None]) -> None:
        try:
            asyncio.run(self._run_worker(ready))
        except BaseException as exc:  # pragma: no cover - defensive for interpreter/runtime failures.
            if not ready.done():
                ready.set_exception(exc)

    async def _run_worker(self, ready: concurrent.futures.Future[None]) -> None:
        stack = AsyncExitStack()
        operations: asyncio.Queue[Any] = asyncio.Queue()
        self._loop = asyncio.get_running_loop()
        self._operations = operations
        self._worker_task = asyncio.current_task()
        try:
            session = await self._open_session(stack)
            self._stack = stack
            self._session = session
            if not ready.done():
                ready.set_result(None)
            while True:
                item = await operations.get()
                if item is None:
                    break
                operation, future = item
                if future.cancelled():
                    continue
                try:
                    result = await operation(session)
                except Exception as exc:
                    if not future.cancelled():
                        try:
                            future.set_exception(exc)
                        except concurrent.futures.InvalidStateError:
                            pass
                else:
                    if not future.cancelled():
                        try:
                            future.set_result(result)
                        except concurrent.futures.InvalidStateError:
                            pass
        except Exception as exc:
            if self._stderr_buffer is not None:
                self.stderr_tail = self._stderr_buffer.getvalue()
            error = _connection_error(self.config.name, exc, self.stderr_tail)
            if not ready.done():
                ready.set_exception(error)
        finally:
            try:
                await stack.aclose()
            finally:
                if self._stderr_buffer is not None:
                    self.stderr_tail = self._stderr_buffer.getvalue()
                    self._stderr_buffer.close()
                    self._stderr_buffer = None
                self._stack = None
                self._session = None
                self.initialize_result = None
                self._operations = None
                self._loop = None
                self._worker_task = None
                self._worker_thread = None

    async def _put_operation(self, item: Any) -> None:
        operations = self._operations
        if operations is None:
            raise MCPConnectionError(_("MCP server {server!r} is not connected.").format(server=self.config.name))
        await operations.put(item)

    async def _run_session_operation(self, operation: Callable[[Any], Awaitable[Any]]) -> Any:
        loop = self._loop
        operations = self._operations
        if loop is None or operations is None:
            raise MCPConnectionError(_("MCP server {server!r} is not connected.").format(server=self.config.name))

        try:
            running_loop = asyncio.get_running_loop()
        except RuntimeError:
            running_loop = None
        if running_loop is loop:
            session = self._require_session()
            return await operation(session)

        future: concurrent.futures.Future[Any] = concurrent.futures.Future()
        try:
            enqueue = asyncio.run_coroutine_threadsafe(operations.put((operation, future)), loop)
        except RuntimeError as exc:
            message = _("MCP server {server!r} is not connected.").format(server=self.config.name)
            raise MCPConnectionError(message) from exc
        await asyncio.wrap_future(enqueue)
        return await asyncio.wrap_future(future)

    async def _list_roots_callback(self, context: Any) -> Any:
        from mcp import types

        _ = context
        roots = [types.Root(uri=cast(Any, root.resolve().as_uri()), name=root.name or str(root)) for root in self.roots]
        return types.ListRootsResult(roots=roots)

    def _require_session(self) -> Any:
        if self._session is None:
            raise MCPConnectionError(_("MCP server {server!r} is not connected.").format(server=self.config.name))
        return self._session

    async def _remote_headers(self) -> dict[str, str] | None:
        headers = dict(self.config.headers)
        if self.config.oauth is not None:
            token = await get_oauth_access_token_async(self.config, storage=self._secret_storage, scope=self.scope)
            if token:
                headers["Authorization"] = "Bearer {}".format(token)
        return headers or None

    async def list_tools(self) -> Any:
        return await self._run_session_operation(lambda session: session.list_tools())

    async def call_tool(self, name: str, arguments: dict[str, Any] | None = None, **kwargs: Any) -> Any:
        return await self._run_session_operation(lambda session: session.call_tool(name, arguments=arguments, **kwargs))

    async def list_resources(self) -> Any:
        return await self._run_session_operation(lambda session: session.list_resources())

    async def read_resource(self, uri: str) -> Any:
        return await self._run_session_operation(lambda session: session.read_resource(uri))

    async def list_prompts(self) -> Any:
        return await self._run_session_operation(lambda session: session.list_prompts())

    async def get_prompt(self, name: str, arguments: dict[str, str] | None = None) -> Any:
        return await self._run_session_operation(lambda session: session.get_prompt(name, arguments=arguments))

    async def _open_session(self, stack: AsyncExitStack) -> Any:
        read_stream: Any
        write_stream: Any

        if self.config.transport is MCPTransport.STDIO:
            from mcp.client.stdio import StdioServerParameters, stdio_client

            errlog = _BoundedTextBuffer()
            self._stderr_buffer = errlog
            self.stderr_tail = None
            params = StdioServerParameters(
                command=self.config.command or "",
                args=list(self.config.args),
                env=_stdio_env(self.config.env),
            )
            try:
                read_stream, write_stream = await stack.enter_async_context(
                    stdio_client(params, errlog=cast(Any, errlog))
                )
            finally:
                self.stderr_tail = errlog.getvalue()
        elif self.config.transport is MCPTransport.HTTP:
            from mcp.client.streamable_http import streamablehttp_client

            headers = await self._remote_headers()
            read_stream, write_stream, _session_id = await stack.enter_async_context(
                streamablehttp_client(self.config.url or "", headers=headers)
            )
        elif self.config.transport is MCPTransport.SSE:
            from mcp.client.sse import sse_client

            headers = await self._remote_headers()
            read_stream, write_stream = await stack.enter_async_context(
                sse_client(self.config.url or "", headers=headers)
            )
        else:  # pragma: no cover - MCPServerConfig validation prevents this.
            raise MCPConnectionError(
                _("Unsupported MCP transport: {transport}").format(transport=self.config.transport.value)
            )

        from mcp.client.session import ClientSession

        session = ClientSession(read_stream, write_stream, list_roots_callback=self._list_roots_callback)
        if self._list_changed_callback is not None:
            _install_list_changed_handler(session, self._list_changed_callback)
        await stack.enter_async_context(session)
        self.initialize_result = await session.initialize()
        return session


def _ensure_mcp_client_modules_preloaded() -> None:
    global _MCP_CLIENT_MODULES_PRELOADED
    if _MCP_CLIENT_MODULES_PRELOADED:
        return
    with _MCP_CLIENT_MODULES_PRELOAD_LOCK:
        if _MCP_CLIENT_MODULES_PRELOADED:
            return
        import mcp.client.session  # noqa: F401
        import mcp.client.sse  # noqa: F401
        import mcp.client.stdio  # noqa: F401
        import mcp.client.streamable_http  # noqa: F401

        _MCP_CLIENT_MODULES_PRELOADED = True


_STDIO_ENV_ALLOWLIST = {
    "APPDATA",
    "ALL_PROXY",
    "CURL_CA_BUNDLE",
    "COMSPEC",
    "HOME",
    "HTTPS_PROXY",
    "HTTP_PROXY",
    "LANG",
    "LC_ALL",
    "LOCALAPPDATA",
    "NODE_EXTRA_CA_CERTS",
    "NO_PROXY",
    "PATH",
    "PATHEXT",
    "REQUESTS_CA_BUNDLE",
    "SHELL",
    "SSL_CERT_DIR",
    "SSL_CERT_FILE",
    "SYSTEMROOT",
    "TEMP",
    "TMP",
    "TMPDIR",
    "UV_CACHE_DIR",
    "UV_PYTHON_INSTALL_DIR",
    "UV_TOOL_DIR",
    "USER",
    "USERPROFILE",
    "USERNAME",
    "WINDIR",
    "XDG_CACHE_HOME",
    "all_proxy",
    "https_proxy",
    "http_proxy",
    "no_proxy",
}


def _stdio_env(explicit_env: dict[str, str]) -> dict[str, str]:
    env = {
        name: value
        for name in _STDIO_ENV_ALLOWLIST
        if (value := os.environ.get(name)) and _safe_stdio_inherited_env(name, value)
    }
    env.update(explicit_env)
    return env


def _safe_stdio_inherited_env(name: str, value: str) -> bool:
    if name.lower() in {"http_proxy", "https_proxy", "all_proxy"}:
        parsed = urlparse(value)
        return not parsed.username and not parsed.password
    return True


def _looks_like_auth_required(exc: BaseException) -> bool:
    response = getattr(exc, "response", None)
    status_code = getattr(response, "status_code", None)
    if status_code in {401, 403}:
        return True
    text = "{} {}".format(exc.__class__.__name__, str(exc)).lower()
    return any(marker in text for marker in ("401", "unauthorized", "forbidden", "invalid_token", "oauth"))


def _connection_error(server_name: str, exc: BaseException, stderr_tail: str | None = None) -> Exception:
    if isinstance(exc, MCPNeedsAuthError | MCPConnectionError):
        return exc
    if _looks_like_auth_required(exc):
        return MCPNeedsAuthError(_("MCP server {server!r} requires authentication.").format(server=server_name))
    message = str(exc)
    if stderr_tail:
        message = _("{message}\nMCP server stderr:\n{stderr}").format(message=message, stderr=stderr_tail)
    return MCPConnectionError(message)


def _install_list_changed_handler(session: Any, callback: ListChangedCallback) -> None:
    original = session._received_notification

    async def received_notification(notification: Any) -> None:
        await original(notification)
        capability = _list_changed_capability(notification)
        if capability is not None:
            result = callback(capability)
            if inspect.isawaitable(result):
                asyncio.create_task(_await_callback(result))

    session._received_notification = received_notification


async def _await_callback(awaitable: Awaitable[None]) -> None:
    try:
        await awaitable
    except Exception as exc:
        from loguru import logger

        logger.debug("MCP list_changed callback failed: {}", exc)


def _list_changed_capability(notification: Any) -> str | None:
    root = getattr(notification, "root", notification)
    method = getattr(root, "method", None)
    if method == "notifications/tools/list_changed":
        return "tools"
    if method == "notifications/resources/list_changed":
        return "resources"
    if method == "notifications/prompts/list_changed":
        return "prompts"
    class_name = root.__class__.__name__
    if class_name == "ToolListChangedNotification":
        return "tools"
    if class_name == "ResourceListChangedNotification":
        return "resources"
    if class_name == "PromptListChangedNotification":
        return "prompts"
    return None


class _BoundedTextBuffer:
    def __init__(self, *, max_chars: int = 8000) -> None:
        self._max_chars = max_chars
        self._file = TemporaryFile(mode="w+b")

    def write(self, value: str) -> int:
        data = value.encode("utf-8", errors="replace") if isinstance(value, str) else bytes(value)
        return self._file.write(data)

    def flush(self) -> None:
        self._file.flush()

    def fileno(self) -> int:
        return self._file.fileno()

    def getvalue(self) -> str:
        self.flush()
        self._file.seek(0, os.SEEK_END)
        size = self._file.tell()
        self._file.seek(max(0, size - self._max_chars))
        return self._file.read(self._max_chars).decode("utf-8", errors="replace")

    def close(self) -> None:
        self._file.close()
