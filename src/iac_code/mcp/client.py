from __future__ import annotations

import asyncio
import concurrent.futures
import inspect
import json
import os
import re
import shlex
import threading
from collections.abc import Mapping
from contextlib import AsyncExitStack, asynccontextmanager, suppress
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryFile
from typing import Any, Awaitable, Callable, Protocol, cast
from urllib.parse import urlparse

from iac_code.desktop.external_env import (
    create_anyio_process,
    create_external_process_async,
    create_subprocess_exec,
    guarded_command,
    is_guardian_command,
    spawn_env,
)
from iac_code.i18n import _
from iac_code.mcp.errors import MCPConnectionError, MCPNeedsAuthError
from iac_code.mcp.oauth import build_oauth_transport_auth_provider, has_oauth_state, needs_auth_error_from_exception
from iac_code.mcp.redaction import sanitize_mcp_public_text
from iac_code.mcp.storage import MCPSecretStorage
from iac_code.mcp.types import (
    MCPConfigScope,
    MCPConnectionMetadata,
    MCPConnectionState,
    MCPServerConfig,
    MCPTransport,
    normalize_initialize_metadata,
)
from iac_code.utils.public_errors import sanitize_public_text


class MCPClientProtocol(Protocol):
    @property
    def metadata(self) -> MCPConnectionMetadata | None: ...

    async def connect(self) -> None: ...

    async def close(self) -> None: ...

    async def list_tools(self) -> Any: ...

    async def call_tool(self, name: str, arguments: dict[str, Any] | None = None, **kwargs: Any) -> Any: ...

    async def list_resources(self) -> Any: ...

    async def read_resource(self, uri: str) -> Any: ...

    async def list_prompts(self) -> Any: ...

    async def get_prompt(self, name: str, arguments: dict[str, str] | None = None) -> Any: ...


ListChangedCallback = Callable[[str], Awaitable[None] | None]
ElicitationCallback = Callable[[Mapping[str, Any]], Awaitable[Mapping[str, Any]] | Mapping[str, Any]]

_MCP_CLIENT_MODULES_PRELOADED = False
_MCP_CLIENT_MODULES_PRELOAD_LOCK = threading.Lock()
MCP_URL_ELICITATION_REQUIRED_ERROR_CODE = -32042
"""MCP SDK ``types.URL_ELICITATION_REQUIRED`` error code for URL-mode elicitation."""
_MCP_URL_ELICITATION_MAX_RETRIES = 3
_HEADERS_HELPER_TIMEOUT_SECONDS = 5.0
_HEADERS_HELPER_STDOUT_MAX_BYTES = 64 * 1024
_HEADERS_HELPER_STDERR_MAX_BYTES = 4 * 1024
_HEADERS_HELPER_STDERR_DISPLAY_MAX_CHARS = 4000
_HEADERS_HELPER_READ_CHUNK_BYTES = 4096


@asynccontextmanager
async def _stdio_client_with_process_fallback(params: Any, errlog: Any) -> Any:
    import anyio
    import mcp.client.stdio as stdio_module
    from anyio import ClosedResourceError
    from anyio.lowlevel import checkpoint
    from anyio.streams.text import TextReceiveStream
    from mcp import types
    from mcp.shared.message import SessionMessage

    stdio_client = stdio_module.stdio_client
    if getattr(stdio_client, "__module__", "") != stdio_module.__name__:
        async with stdio_client(params, errlog=errlog) as streams:
            yield streams
        return

    read_stream_writer, read_stream = anyio.create_memory_object_stream(0)
    write_stream, write_stream_reader = anyio.create_memory_object_stream(0)
    process: Any
    try:
        command = stdio_module._get_executable_command(params.command)
        default_env = stdio_module.get_default_environment()
        guarded = guarded_command([command, *params.args], kind="mcp")
        child_env = spawn_env({**default_env, **params.env} if params.env is not None else default_env)
        if not is_guardian_command(guarded):
            process = await create_external_process_async(
                stdio_module._create_platform_compatible_process,
                command=guarded[0],
                args=guarded[1:],
                env=child_env,
                errlog=errlog,
                cwd=params.cwd,
                add_creation_flags=False,
            )
        else:
            process = await create_anyio_process(
                guarded,
                env=child_env,
                stderr=errlog,
                cwd=params.cwd,
            )
    except OSError:
        await read_stream.aclose()
        await write_stream.aclose()
        await read_stream_writer.aclose()
        await write_stream_reader.aclose()
        raise

    async def stdout_reader() -> None:
        assert process.stdout, "Opened process is missing stdout"
        try:
            async with read_stream_writer:
                buffer = ""
                async for chunk in TextReceiveStream(
                    process.stdout,
                    encoding=params.encoding,
                    errors=params.encoding_error_handler,
                ):
                    lines = (buffer + chunk).split("\n")
                    buffer = lines.pop()
                    for line in lines:
                        try:
                            message = types.JSONRPCMessage.model_validate_json(line)
                        except Exception as exc:  # pragma: no cover - mirrors SDK parser behavior.
                            await read_stream_writer.send(exc)
                            continue
                        await read_stream_writer.send(SessionMessage(message))
        except ClosedResourceError:  # pragma: no cover
            await checkpoint()

    async def stdin_writer() -> None:
        assert process.stdin, "Opened process is missing stdin"
        try:
            async with write_stream_reader:
                async for session_message in write_stream_reader:
                    payload = session_message.message.model_dump_json(by_alias=True, exclude_none=True)
                    await process.stdin.send(
                        (payload + "\n").encode(
                            encoding=params.encoding,
                            errors=params.encoding_error_handler,
                        )
                    )
        except ClosedResourceError:  # pragma: no cover
            await checkpoint()

    async with anyio.create_task_group() as tg, process:
        tg.start_soon(stdout_reader)
        tg.start_soon(stdin_writer)
        try:
            yield read_stream, write_stream
        finally:
            if process.stdin:
                with suppress(Exception):
                    await process.stdin.aclose()
            try:
                try:
                    with anyio.fail_after(stdio_module.PROCESS_TERMINATION_TIMEOUT):
                        await process.wait()
                except TimeoutError:
                    await _terminate_stdio_process(stdio_module, process)
                except ProcessLookupError:  # pragma: no cover
                    pass
                except Exception:
                    await _terminate_stdio_process(stdio_module, process)
                    raise
            finally:
                await read_stream.aclose()
                await write_stream.aclose()
                await read_stream_writer.aclose()
                await write_stream_reader.aclose()


async def _terminate_stdio_process(stdio_module: Any, process: Any) -> None:
    from iac_code.desktop.external_env import is_guardian_process

    if is_guardian_process(process):
        process.terminate()
        await asyncio.shield(process.wait())
        return
    target = getattr(process, "_iac_code_fallback_process", process)
    terminate_tree = getattr(stdio_module, "_terminate_process_tree", None)
    if callable(terminate_tree):
        with suppress(Exception):
            await terminate_tree(target)
            return

    terminate = getattr(process, "terminate", None)
    if callable(terminate):
        with suppress(Exception):
            terminate()
    wait = getattr(process, "wait", None)
    if callable(wait):
        with suppress(Exception):
            await asyncio.wait_for(wait(), timeout=2.0)
            return
    kill = getattr(process, "kill", None)
    if callable(kill):
        with suppress(Exception):
            kill()


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
        elicitation_callback: ElicitationCallback | None = None,
    ) -> None:
        self.config = config
        self.roots = [Path(root) for root in roots or []]
        self.scope = scope
        self._secret_storage = secret_storage or MCPSecretStorage()
        self._list_changed_callback = list_changed_callback
        self._elicitation_callback = elicitation_callback
        self._stack: AsyncExitStack | None = None
        self._session: Any = None
        self.initialize_result: Any = None
        self.stderr_tail: str | None = None
        self._stderr_buffer: _BoundedTextBuffer | None = None
        self._worker_thread: threading.Thread | None = None
        self._worker_task: asyncio.Task[Any] | None = None
        self._active_operation_task: asyncio.Task[Any] | None = None
        self._active_elicitation_loop: asyncio.AbstractEventLoop | None = None
        self._active_call_tasks: set[asyncio.Task[Any]] = set()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._operations: asyncio.Queue[Any] | None = None
        self._close_timeout_seconds = 5.0
        self._closing = False
        self._metadata = MCPConnectionMetadata(
            state=MCPConnectionState.PENDING,
            server_name=self.config.name,
            config_signature=self.config.content_signature(),
        )

    @property
    def metadata(self) -> MCPConnectionMetadata | None:
        return self._metadata

    async def connect(self) -> None:
        if self._closing:
            raise MCPConnectionError(_("MCP server {server!r} is closing.").format(server=self.config.name))
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
            self._closing = False
            return
        self._closing = True
        force_cancel = self._session is None
        self._cancel_active_call_tasks()
        if loop is not None and operations is not None and thread.is_alive():
            self._cancel_active_operation(loop)
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
                loop.call_soon_threadsafe(operations.put_nowait, None)
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
            self._closing = False
        elif thread.is_alive() and self._worker_thread is thread:
            self._operations = None
            self._loop = None
            self._session = None

    def _run_worker_thread(self, ready: concurrent.futures.Future[None]) -> None:
        try:
            asyncio.run(self._run_worker(ready))
        except BaseException as exc:  # pragma: no cover - defensive for interpreter/runtime failures.
            if not ready.done():
                ready.set_exception(exc)

    async def _run_worker(self, ready: concurrent.futures.Future[None]) -> None:
        stack = AsyncExitStack()
        operations: asyncio.Queue[Any] = asyncio.Queue()
        failure: Exception | None = None
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
                operation, future, callback_loop = item
                if future.cancelled():
                    continue
                try:
                    result = await self._run_operation_task(operation, session, future, callback_loop)
                except asyncio.CancelledError:
                    if _current_task_is_cancelling():
                        raise
                    if not future.cancelled():
                        future.cancel()
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
            failure = exc
        finally:
            try:
                await stack.aclose()
            except Exception as exc:
                if failure is None:
                    failure = exc
            finally:
                if self._stderr_buffer is not None:
                    self.stderr_tail = _public_stderr_tail(self._stderr_buffer.getvalue())
                    self._stderr_buffer.close()
                    self._stderr_buffer = None
                if failure is None and self.stderr_tail:
                    self._metadata = replace(self._metadata, stderr_tail=self.stderr_tail)
                if failure is not None:
                    self._metadata = MCPConnectionMetadata(
                        state=MCPConnectionState.FAILED,
                        server_name=self.config.name,
                        stderr_tail=self.stderr_tail,
                        config_signature=self.config.content_signature(),
                    )
                    error = _connection_error(
                        self.config.name,
                        failure,
                        self.stderr_tail,
                        config=self.config,
                        storage=self._secret_storage,
                        scope=self.scope,
                    )
                    if not ready.done():
                        ready.set_exception(error)
                self._stack = None
                self._session = None
                self.initialize_result = None
                self._operations = None
                self._loop = None
                self._worker_task = None
                self._active_operation_task = None
                self._active_elicitation_loop = None
                self._worker_thread = None
                self._closing = False

    async def _put_operation(self, item: Any) -> None:
        if self._closing:
            raise MCPConnectionError(_("MCP server {server!r} is closing.").format(server=self.config.name))
        operations = self._operations
        if operations is None:
            raise MCPConnectionError(_("MCP server {server!r} is not connected.").format(server=self.config.name))
        await operations.put(item)

    async def _run_session_operation(
        self,
        operation: Callable[[Any], Awaitable[Any]],
        *,
        map_errors: bool = True,
    ) -> Any:
        loop = self._loop
        operations = self._operations
        if self._closing:
            raise MCPConnectionError(_("MCP server {server!r} is closing.").format(server=self.config.name))
        if loop is None or operations is None:
            raise MCPConnectionError(_("MCP server {server!r} is not connected.").format(server=self.config.name))

        try:
            running_loop = asyncio.get_running_loop()
        except RuntimeError:
            running_loop = None
        if running_loop is loop:
            session = self._require_session()
            try:
                return await operation(session)
            except Exception as exc:
                if not map_errors:
                    raise
                mapped = _connection_error(
                    self.config.name,
                    exc,
                    config=self.config,
                    storage=self._secret_storage,
                    scope=self.scope,
                )
                if mapped is exc:
                    raise
                raise mapped from exc

        future: concurrent.futures.Future[Any] = concurrent.futures.Future()
        try:
            enqueue = asyncio.run_coroutine_threadsafe(operations.put((operation, future, running_loop)), loop)
        except RuntimeError as exc:
            message = _("MCP server {server!r} is not connected.").format(server=self.config.name)
            raise MCPConnectionError(message) from exc
        await asyncio.wrap_future(enqueue)
        try:
            return await asyncio.wrap_future(future)
        except asyncio.CancelledError:
            future.cancel()
            raise
        except Exception as exc:
            if not map_errors:
                raise
            mapped = _connection_error(
                self.config.name,
                exc,
                config=self.config,
                storage=self._secret_storage,
                scope=self.scope,
            )
            if mapped is exc:
                raise
            raise mapped from exc

    async def _list_roots_callback(self, context: Any) -> Any:
        from mcp import types

        _ = context
        roots = [types.Root(uri=cast(Any, root.resolve().as_uri()), name=root.name or str(root)) for root in self.roots]
        return types.ListRootsResult(roots=roots)

    def _require_session(self) -> Any:
        if self._session is None:
            raise MCPConnectionError(_("MCP server {server!r} is not connected.").format(server=self.config.name))
        return self._session

    async def _run_operation_task(
        self,
        operation: Callable[[Any], Awaitable[Any]],
        session: Any,
        future: concurrent.futures.Future[Any],
        callback_loop: asyncio.AbstractEventLoop | None,
    ) -> Any:
        async def run_operation() -> Any:
            return await operation(session)

        task = asyncio.create_task(run_operation())
        self._active_operation_task = task
        previous_elicitation_loop = self._active_elicitation_loop
        self._active_elicitation_loop = callback_loop
        loop = asyncio.get_running_loop()

        def cancel_task(done_future: concurrent.futures.Future[Any]) -> None:
            if done_future.cancelled() and not task.done():
                loop.call_soon_threadsafe(task.cancel)

        future.add_done_callback(cancel_task)
        try:
            return await task
        finally:
            if self._active_operation_task is task:
                self._active_operation_task = None
            self._active_elicitation_loop = previous_elicitation_loop

    def _cancel_active_operation(self, loop: asyncio.AbstractEventLoop) -> None:
        task = self._active_operation_task
        if task is None or task.done():
            return
        try:
            loop.call_soon_threadsafe(task.cancel)
        except RuntimeError:
            pass

    def _cancel_active_call_tasks(self) -> None:
        try:
            current_task = asyncio.current_task()
        except RuntimeError:
            current_task = None
        for task in list(self._active_call_tasks):
            if task is current_task or task.done():
                continue
            try:
                task.get_loop().call_soon_threadsafe(task.cancel)
            except RuntimeError:
                pass

    async def _remote_headers(self) -> dict[str, str] | None:
        headers = dict(self.config.headers)
        if self.config.headers_helper:
            headers = _merge_headers_case_insensitive(headers, await _run_headers_helper(self.config, self.roots))
        return headers or None

    async def _remote_auth(self) -> Any:
        if self.config.oauth is None and not has_oauth_state(self.config, self._secret_storage, self.scope):
            return None
        return await asyncio.to_thread(
            build_oauth_transport_auth_provider,
            self.config,
            self._secret_storage,
            self.scope,
        )

    async def list_tools(self) -> Any:
        return await self._run_session_operation(lambda session: session.list_tools())

    async def call_tool(self, name: str, arguments: dict[str, Any] | None = None, **kwargs: Any) -> Any:
        current_task = asyncio.current_task()
        if current_task is not None:
            self._active_call_tasks.add(current_task)
        retries = 0
        try:
            while True:
                try:
                    return await self._run_session_operation(
                        lambda session: session.call_tool(name, arguments=arguments, **kwargs),
                        map_errors=False,
                    )
                except Exception as exc:
                    if (
                        self._elicitation_callback is None
                        or not _is_url_elicitation_required_error(exc)
                        or retries >= _MCP_URL_ELICITATION_MAX_RETRIES
                    ):
                        self._raise_connection_error(exc)
                    elicitations = _url_elicitations_from_error(exc)
                    if not elicitations:
                        self._raise_connection_error(exc)
                    for elicitation in elicitations:
                        result = await self._run_elicitation_callback(elicitation)
                        if str(result.get("action", "cancel")) != "accept":
                            self._raise_connection_error(exc)
                        elicitation_id = elicitation.get("elicitationId")
                        if isinstance(elicitation_id, str) and elicitation_id:
                            await self._complete_url_elicitation(elicitation_id)
                    retries += 1
        finally:
            if current_task is not None:
                self._active_call_tasks.discard(current_task)

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
            from mcp.client.stdio import StdioServerParameters

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
                    _stdio_client_with_process_fallback(params, errlog)
                )
            finally:
                self.stderr_tail = _public_stderr_tail(errlog.getvalue())
        elif self.config.transport is MCPTransport.HTTP:
            from mcp.client.streamable_http import streamablehttp_client

            headers = await self._remote_headers()
            auth = await self._remote_auth()
            read_stream, write_stream, _session_id = await stack.enter_async_context(
                streamablehttp_client(self.config.url or "", headers=headers, auth=auth)
            )
        elif self.config.transport is MCPTransport.SSE:
            from mcp.client.sse import sse_client

            headers = await self._remote_headers()
            auth = await self._remote_auth()
            read_stream, write_stream = await stack.enter_async_context(
                sse_client(self.config.url or "", headers=headers, auth=auth)
            )
        elif self.config.transport is MCPTransport.WS:
            from mcp.client.websocket import websocket_client

            read_stream, write_stream = await stack.enter_async_context(websocket_client(self.config.url or ""))
        else:  # pragma: no cover - MCPServerConfig validation prevents this.
            raise MCPConnectionError(
                _("Unsupported MCP transport: {transport}").format(transport=self.config.transport.value)
            )

        from mcp.client.session import ClientSession

        session_kwargs: dict[str, Any] = {"list_roots_callback": self._list_roots_callback}
        if self._elicitation_callback is not None:
            session_kwargs["elicitation_callback"] = self._request_elicitation_callback
        session = ClientSession(read_stream, write_stream, **session_kwargs)
        if self._list_changed_callback is not None:
            _install_list_changed_handler(session, self._list_changed_callback)
        await stack.enter_async_context(session)
        self.initialize_result = await session.initialize()
        self._metadata = normalize_initialize_metadata(
            self.config.name,
            self.initialize_result,
            stderr_tail=self.stderr_tail,
            config_signature=self.config.content_signature(),
        )
        return session

    async def _request_elicitation_callback(self, context: Any, params: Any) -> Any:
        _ = context
        from mcp import types

        result_dict = await self._run_elicitation_callback(_mapping_from_mcp_value(params))
        return types.ElicitResult.model_validate(result_dict)

    async def _run_elicitation_callback(self, params: Mapping[str, Any]) -> Mapping[str, Any]:
        callback_loop = self._active_elicitation_loop
        if callback_loop is not None and not callback_loop.is_closed():
            try:
                running_loop = asyncio.get_running_loop()
            except RuntimeError:
                running_loop = None
            if running_loop is not callback_loop:
                future = asyncio.run_coroutine_threadsafe(self._invoke_elicitation_callback(params), callback_loop)
                try:
                    return await asyncio.wrap_future(future)
                except asyncio.CancelledError:
                    future.cancel()
                    raise
        return await self._invoke_elicitation_callback(params)

    async def _complete_url_elicitation(self, elicitation_id: str) -> None:
        from mcp import types

        notification = types.ElicitCompleteNotification(
            params=types.ElicitCompleteNotificationParams(elicitationId=elicitation_id)
        )
        await self._run_session_operation(lambda session: session.send_notification(notification))

    async def _invoke_elicitation_callback(self, params: Mapping[str, Any]) -> Mapping[str, Any]:
        callback = self._elicitation_callback
        if callback is None:
            return {"action": "cancel"}
        result = callback(params)
        if inspect.isawaitable(result):
            result = await result
        if not isinstance(result, Mapping):
            return {"action": "cancel"}
        return _dict_from_mapping(result)

    def _raise_connection_error(self, exc: Exception) -> None:
        mapped = _connection_error(
            self.config.name,
            exc,
            config=self.config,
            storage=self._secret_storage,
            scope=self.scope,
        )
        if mapped is exc:
            raise exc
        raise mapped from exc


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
        import mcp.client.websocket  # noqa: F401

        _MCP_CLIENT_MODULES_PRELOADED = True


def _current_task_is_cancelling() -> bool:
    task = asyncio.current_task()
    cancelling = getattr(task, "cancelling", None)
    if callable(cancelling):
        return bool(cancelling())
    return False


def _is_url_elicitation_required_error(exc: BaseException) -> bool:
    error = getattr(exc, "error", None)
    return getattr(error, "code", None) == MCP_URL_ELICITATION_REQUIRED_ERROR_CODE


def _url_elicitations_from_error(exc: BaseException) -> list[Mapping[str, Any]]:
    direct = getattr(exc, "elicitations", None)
    if isinstance(direct, list):
        return _mapped_elicitations(direct)
    error = getattr(exc, "error", None)
    data = getattr(error, "data", None)
    if not isinstance(data, Mapping):
        return []
    raw_elicitations = data.get("elicitations")
    if not isinstance(raw_elicitations, list):
        return []
    return _mapped_elicitations(raw_elicitations)


def _mapped_elicitations(values: list[Any]) -> list[Mapping[str, Any]]:
    elicitations: list[Mapping[str, Any]] = []
    for item in values:
        mapped = _mapping_from_mcp_value(item)
        if mapped:
            elicitations.append(mapped)
    return elicitations


def _dict_from_mapping(value: Mapping[Any, Any]) -> dict[str, Any]:
    return {str(key): item for key, item in value.items()}


def _mapping_from_mcp_value(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        dumped = model_dump(by_alias=True, exclude_none=True)
        return dict(dumped) if isinstance(dumped, Mapping) else {}
    return {}


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
_HEADERS_HELPER_ENV_REFERENCE_RE = re.compile(
    r"\$(?:\{([A-Za-z_][A-Za-z0-9_]*)(?::-[^}]*)?\}|([A-Za-z_][A-Za-z0-9_]*))"
)


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


class _HeadersHelperOutputTooLargeError(Exception):
    def __init__(self, stream_name: str, output: bytes) -> None:
        self.stream_name = stream_name
        self.output = output
        super().__init__(stream_name)


async def _run_headers_helper(config: MCPServerConfig, roots: list[Path]) -> dict[str, str]:
    command = config.headers_helper
    if not command:
        return {}
    try:
        argv = _split_headers_helper_command(command)
    except ValueError as exc:
        raise MCPConnectionError(
            _("MCP headers helper for server {server!r} could not be parsed: {error}").format(
                server=config.name,
                error=str(exc),
            )
        ) from exc
    if not argv:
        raise MCPConnectionError(_("MCP headers helper for server {server!r} is empty.").format(server=config.name))

    cwd = _headers_helper_cwd(config, roots)
    try:
        process = await create_subprocess_exec(
            *guarded_command(argv, kind="mcp"),
            cwd=str(cwd),
            env=spawn_env(_headers_helper_env(command)),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as exc:
        raise MCPConnectionError(
            _("MCP headers helper for server {server!r} failed to start: {error}").format(
                server=config.name,
                error=sanitize_public_text(str(exc)).replace("[REDACTED]", "[redacted]"),
            )
        ) from exc

    try:
        stdout, stderr = await asyncio.wait_for(
            _collect_headers_helper_output(process),
            timeout=_HEADERS_HELPER_TIMEOUT_SECONDS,
        )
    except asyncio.CancelledError:
        await _stop_headers_helper_process(process)
        raise
    except asyncio.TimeoutError as exc:
        await _stop_headers_helper_process(process)
        raise _headers_helper_error(
            config.name,
            _("timed out after {seconds:g} seconds").format(seconds=_HEADERS_HELPER_TIMEOUT_SECONDS),
        ) from exc
    except _HeadersHelperOutputTooLargeError as exc:
        await _stop_headers_helper_process(process)
        raise _headers_helper_error(
            config.name,
            _("{stream} output too large").format(stream=exc.stream_name),
            stderr=exc.output if exc.stream_name == "stderr" else None,
        ) from exc

    if process.returncode != 0:
        raise _headers_helper_error(
            config.name,
            _("exited with status {status}").format(status=process.returncode),
            stderr=stderr,
        )

    try:
        decoded = stdout.decode("utf-8")
        payload = json.loads(decoded)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _headers_helper_error(config.name, _("returned invalid JSON"), stderr=stderr) from exc
    if not isinstance(payload, Mapping):
        raise _headers_helper_error(config.name, _("must return a JSON object"), stderr=stderr)

    headers: dict[str, str] = {}
    for key, value in payload.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise _headers_helper_error(config.name, _("must return string header names and values"), stderr=stderr)
        headers[key] = value
    return headers


def _split_headers_helper_command(command: str, *, platform: str | None = None) -> list[str]:
    if (platform or os.name) != "nt":
        return shlex.split(command, posix=True)
    return _split_windows_command_line(command)


def _split_windows_command_line(command: str) -> list[str]:
    args: list[str] = []
    current: list[str] = []
    quote_char: str | None = None
    had_content = False
    index = 0
    length = len(command)
    while index < length:
        char = command[index]
        if char in {" ", "\t"} and quote_char is None:
            if had_content:
                args.append("".join(current))
                current = []
                had_content = False
            index += 1
            continue
        if char == "\\":
            slash_start = index
            while index < length and command[index] == "\\":
                index += 1
            slash_count = index - slash_start
            if quote_char != "'" and index < length and command[index] == '"':
                current.extend("\\" * (slash_count // 2))
                if slash_count % 2:
                    current.append('"')
                    had_content = True
                    index += 1
                    continue
                quote_char = None if quote_char == '"' else '"'
                had_content = True
                index += 1
                continue
            current.extend("\\" * slash_count)
            had_content = True
            continue
        if char in {'"', "'"}:
            if quote_char is None:
                quote_char = char
            elif quote_char == char:
                quote_char = None
            else:
                current.append(char)
            had_content = True
            index += 1
            continue
        current.append(char)
        had_content = True
        index += 1
    if quote_char is not None:
        raise ValueError("No closing quotation")
    if had_content:
        args.append("".join(current))
    return args


async def _collect_headers_helper_output(process: asyncio.subprocess.Process) -> tuple[bytes, bytes]:
    assert process.stdout is not None
    assert process.stderr is not None
    stdout_task = asyncio.create_task(
        _read_headers_helper_stream(process.stdout, _HEADERS_HELPER_STDOUT_MAX_BYTES, "stdout")
    )
    stderr_task = asyncio.create_task(
        _read_headers_helper_stream(process.stderr, _HEADERS_HELPER_STDERR_MAX_BYTES, "stderr")
    )
    try:
        stdout, stderr = await asyncio.gather(stdout_task, stderr_task)
    finally:
        for task in (stdout_task, stderr_task):
            if not task.done():
                task.cancel()
        await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)
    await process.wait()
    return stdout, stderr


async def _read_headers_helper_stream(
    stream: asyncio.StreamReader,
    max_bytes: int,
    stream_name: str,
) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await stream.read(_HEADERS_HELPER_READ_CHUNK_BYTES)
        if not chunk:
            return b"".join(chunks)
        total += len(chunk)
        if total > max_bytes:
            raise _HeadersHelperOutputTooLargeError(stream_name, b"".join(chunks) + chunk)
        chunks.append(chunk)


async def _stop_headers_helper_process(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    try:
        process.kill()
    except ProcessLookupError:
        return
    await process.wait()


def _headers_helper_cwd(config: MCPServerConfig, roots: list[Path]) -> Path:
    if config.source_dir:
        return Path(config.source_dir).expanduser().resolve()
    if roots:
        return roots[0].expanduser().resolve()
    return Path.cwd()


def _headers_helper_env(command: str | None = None) -> dict[str, str]:
    env = {
        name: value
        for name in _STDIO_ENV_ALLOWLIST
        if (value := os.environ.get(name)) and _safe_stdio_inherited_env(name, value)
    }
    for name in _headers_helper_env_references(command or ""):
        if name in env:
            continue
        value = os.environ.get(name)
        if value is not None and _safe_stdio_inherited_env(name, value):
            env[name] = value
    return env


def _headers_helper_env_references(command: str) -> set[str]:
    return {match.group(1) or match.group(2) for match in _HEADERS_HELPER_ENV_REFERENCE_RE.finditer(command)}


def _headers_helper_error(server_name: str, reason: str, *, stderr: bytes | None = None) -> MCPConnectionError:
    message = _("MCP headers helper for server {server!r} {reason}.").format(server=server_name, reason=reason)
    stderr_text = _headers_helper_stderr(stderr)
    if stderr_text:
        message = _("{message}\nMCP headers helper stderr:\n{stderr}").format(message=message, stderr=stderr_text)
    return MCPConnectionError(message)


def _headers_helper_stderr(stderr: bytes | None) -> str:
    if not stderr:
        return ""
    text = stderr.decode("utf-8", errors="replace")
    if len(text) > _HEADERS_HELPER_STDERR_DISPLAY_MAX_CHARS:
        text = text[-_HEADERS_HELPER_STDERR_DISPLAY_MAX_CHARS:]
    return sanitize_mcp_public_text(text, fallback_summary="").replace("[REDACTED]", "[redacted]")


def _merge_headers_case_insensitive(static: dict[str, str], dynamic: dict[str, str]) -> dict[str, str]:
    merged = dict(static)
    for key, value in dynamic.items():
        normalized = key.lower()
        for existing_key in [candidate for candidate in merged if candidate.lower() == normalized]:
            del merged[existing_key]
        merged[key] = value
    return merged


def _looks_like_auth_required(exc: BaseException) -> bool:
    response = getattr(exc, "response", None)
    status_code = getattr(response, "status_code", None)
    if status_code in {401, 403}:
        return True
    text = "{} {}".format(exc.__class__.__name__, str(exc)).lower()
    return any(marker in text for marker in ("401", "unauthorized", "forbidden", "invalid_token", "oauth"))


def is_mcp_session_expired_error(exc: BaseException) -> bool:
    return bool(getattr(exc, "mcp_session_expired", False)) or _looks_like_session_expired(exc)


def _looks_like_session_expired(exc: BaseException, *, config: MCPServerConfig | None = None) -> bool:
    if config is not None and config.transport not in {MCPTransport.HTTP, MCPTransport.SSE}:
        return False

    error = getattr(exc, "error", None)
    code = getattr(error, "code", None)
    message = str(getattr(error, "message", "") or str(exc))
    text = "{} {}".format(exc.__class__.__name__, message).lower()
    session_marker = any(
        marker in text
        for marker in (
            "session terminated",
            "session expired",
            "session not found",
            "invalid session id",
            "invalid session",
            "mcp-session-id",
        )
    )
    if session_marker and code in {32600, 32603, -32600, -32603, -32000, -32001, -32002}:
        return True

    response = getattr(exc, "response", None)
    status_code = getattr(response, "status_code", None)
    if status_code not in {401, 403, 404, 410}:
        return False
    response_text = "{} {}".format(text, getattr(response, "text", "")).lower()
    strong_marker = any(
        marker in response_text
        for marker in (
            "session terminated",
            "session expired",
            "session not found",
            "invalid session id",
            "invalid session",
            "mcp-session-id",
        )
    )
    if strong_marker:
        return True
    return status_code in {404, 410} and "session" in response_text


def _session_expired_connection_error(server_name: str) -> MCPConnectionError:
    error = MCPConnectionError(
        _("MCP HTTP session expired for server {server!r}; reconnect required.").format(server=server_name)
    )
    setattr(error, "mcp_session_expired", True)
    return error


def _connection_error(
    server_name: str,
    exc: BaseException,
    stderr_tail: str | None = None,
    *,
    config: MCPServerConfig | None = None,
    storage: MCPSecretStorage | None = None,
    scope: MCPConfigScope | str | None = None,
) -> Exception:
    if isinstance(exc, MCPNeedsAuthError | MCPConnectionError):
        return exc
    session_expired = _looks_like_session_expired(exc, config=config)
    if session_expired and not _has_explicit_auth_challenge(exc):
        return _session_expired_connection_error(server_name)
    needs_auth = needs_auth_error_from_exception(server_name, exc, config=config, storage=storage, scope=scope)
    if needs_auth is not None:
        return needs_auth
    if session_expired:
        return _session_expired_connection_error(server_name)
    if _looks_like_auth_required(exc):
        return MCPNeedsAuthError(_("MCP server {server!r} requires authentication.").format(server=server_name))
    message = sanitize_mcp_public_text(str(exc), fallback_summary=exc.__class__.__name__)
    public_stderr_tail = _public_stderr_tail(stderr_tail)
    if public_stderr_tail:
        message = _("{message}\nMCP server stderr:\n{stderr}").format(message=message, stderr=public_stderr_tail)
    return MCPConnectionError(message)


def _has_explicit_auth_challenge(exc: BaseException) -> bool:
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None)
    if headers is None:
        return False
    try:
        return bool(headers.get("WWW-Authenticate"))
    except Exception:
        return False


def _public_stderr_tail(value: str | None) -> str | None:
    if value is None:
        return None
    return sanitize_mcp_public_text(value, fallback_summary="").replace("[REDACTED]", "[redacted]")


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
