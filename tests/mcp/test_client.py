from __future__ import annotations

import asyncio
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from iac_code.mcp.client import MCPClientAdapter
from iac_code.mcp.errors import MCPConnectionError
from iac_code.mcp.oauth import oauth_storage_key
from iac_code.mcp.types import MCPConfigScope, MCPServerConfig


@pytest.mark.asyncio
async def test_stdio_adapter_initializes_sdk_session_with_roots(monkeypatch, tmp_path: Path) -> None:
    seen: dict[str, Any] = {}

    @asynccontextmanager
    async def fake_stdio_client(params, errlog=None):
        seen["stdio_params"] = params
        seen["errlog"] = errlog
        yield object(), object()

    class FakeClientSession:
        def __init__(self, read_stream, write_stream, list_roots_callback=None):
            seen["read_stream"] = read_stream
            seen["write_stream"] = write_stream
            seen["list_roots_callback"] = list_roots_callback
            self.closed = False

        async def __aenter__(self):
            seen["session"] = self
            return self

        async def __aexit__(self, exc_type, exc_val, exc_tb):
            self.closed = True

        async def initialize(self):
            seen["initialized"] = True

        async def list_tools(self):
            return [{"name": "plan"}]

    import mcp.client.session as session_module
    import mcp.client.stdio as stdio_module

    monkeypatch.setattr(stdio_module, "stdio_client", fake_stdio_client)
    monkeypatch.setattr(session_module, "ClientSession", FakeClientSession)

    adapter = MCPClientAdapter(
        MCPServerConfig.from_mapping(
            "local",
            {
                "command": "uvx",
                "args": ["server"],
                "env": {"API_KEY": "fake"},
            },
        ),
        roots=[tmp_path / "repo"],
    )

    await adapter.connect()

    assert seen["stdio_params"].command == "uvx"
    assert seen["stdio_params"].args == ["server"]
    assert seen["stdio_params"].env["API_KEY"] == "fake"
    assert "PATH" in seen["stdio_params"].env
    assert seen["errlog"] is not None
    assert seen["initialized"] is True
    assert await adapter.list_tools() == [{"name": "plan"}]
    roots = await seen["list_roots_callback"](None)
    assert len(roots.roots) == 1
    assert str(roots.roots[0].uri).startswith("file://")

    await adapter.close()
    assert seen["session"].closed is True


@pytest.mark.asyncio
async def test_stdio_adapter_does_not_inherit_secret_process_environment(monkeypatch) -> None:
    seen: dict[str, Any] = {}
    monkeypatch.setenv("IAC_CODE_API_KEY", "real-secret")
    monkeypatch.setenv("ALIBABA_CLOUD_ACCESS_KEY_SECRET", "cloud-secret")
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.example:8080")
    monkeypatch.setenv("HTTP_PROXY", "http://user:pass@proxy.example:8080")
    monkeypatch.setenv("SSL_CERT_FILE", "/tmp/cacert.pem")

    @asynccontextmanager
    async def fake_stdio_client(params, errlog=None):
        seen["stdio_params"] = params
        yield object(), object()

    class FakeClientSession:
        def __init__(self, read_stream, write_stream, list_roots_callback=None):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc_val, exc_tb):
            return None

        async def initialize(self):
            return None

    import mcp.client.session as session_module
    import mcp.client.stdio as stdio_module

    monkeypatch.setattr(stdio_module, "stdio_client", fake_stdio_client)
    monkeypatch.setattr(session_module, "ClientSession", FakeClientSession)

    adapter = MCPClientAdapter(
        MCPServerConfig.from_mapping(
            "local",
            {
                "command": "uvx",
                "env": {"API_TOKEN": "explicit-token"},
            },
        ),
    )

    await adapter.connect()

    env = seen["stdio_params"].env
    assert env["PATH"] == "/usr/bin"
    assert env["HTTPS_PROXY"] == "http://proxy.example:8080"
    assert env["SSL_CERT_FILE"] == "/tmp/cacert.pem"
    assert env["API_TOKEN"] == "explicit-token"
    assert "HTTP_PROXY" not in env
    assert "IAC_CODE_API_KEY" not in env
    assert "ALIBABA_CLOUD_ACCESS_KEY_SECRET" not in env

    await adapter.close()


@pytest.mark.asyncio
async def test_timed_out_session_operation_does_not_stop_worker(monkeypatch) -> None:
    release = threading.Event()

    @asynccontextmanager
    async def fake_stdio_client(params, errlog=None):
        yield object(), object()

    class FakeClientSession:
        def __init__(self, read_stream, write_stream, list_roots_callback=None):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc_val, exc_tb):
            return None

        async def initialize(self):
            return None

        async def list_resources(self):
            await asyncio.to_thread(release.wait, 2)
            return [{"uri": "resource://slow"}]

        async def list_tools(self):
            return [{"name": "plan"}]

    import mcp.client.session as session_module
    import mcp.client.stdio as stdio_module

    monkeypatch.setattr(stdio_module, "stdio_client", fake_stdio_client)
    monkeypatch.setattr(session_module, "ClientSession", FakeClientSession)

    adapter = MCPClientAdapter(MCPServerConfig.from_mapping("local", {"command": "uvx"}))
    await adapter.connect()
    try:
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(adapter.list_resources(), timeout=0.01)
        release.set()
        await asyncio.sleep(0.05)

        assert await adapter.list_tools() == [{"name": "plan"}]
    finally:
        await adapter.close()


def test_stdio_adapter_operations_survive_event_loop_boundary(monkeypatch) -> None:
    seen: dict[str, Any] = {}

    @asynccontextmanager
    async def fake_stdio_client(params, errlog=None):
        yield object(), object()

    class FakeClientSession:
        def __init__(self, read_stream, write_stream, list_roots_callback=None):
            self.closed = False

        async def __aenter__(self):
            seen["session"] = self
            return self

        async def __aexit__(self, exc_type, exc_val, exc_tb):
            self.closed = True

        async def initialize(self):
            return None

        async def list_tools(self):
            return [{"name": "plan"}]

    import mcp.client.session as session_module
    import mcp.client.stdio as stdio_module

    monkeypatch.setattr(stdio_module, "stdio_client", fake_stdio_client)
    monkeypatch.setattr(session_module, "ClientSession", FakeClientSession)

    adapter = MCPClientAdapter(MCPServerConfig.from_mapping("local", {"command": "uvx"}))

    asyncio.run(adapter.connect())
    try:
        assert asyncio.run(asyncio.wait_for(adapter.list_tools(), timeout=1.0)) == [{"name": "plan"}]
    finally:
        asyncio.run(adapter.close())

    assert seen["session"].closed is True


@pytest.mark.asyncio
async def test_adapter_forwards_list_changed_notifications(monkeypatch) -> None:
    changed: list[str] = []
    seen: dict[str, Any] = {}

    @asynccontextmanager
    async def fake_stdio_client(params, errlog=None):
        yield object(), object()

    class FakeClientSession:
        def __init__(self, read_stream, write_stream, list_roots_callback=None):
            self.original_notifications = 0

        async def __aenter__(self):
            seen["session"] = self
            return self

        async def __aexit__(self, exc_type, exc_val, exc_tb):
            return None

        async def initialize(self):
            return None

        async def _received_notification(self, notification):
            self.original_notifications += 1

    import mcp.client.session as session_module
    import mcp.client.stdio as stdio_module

    monkeypatch.setattr(stdio_module, "stdio_client", fake_stdio_client)
    monkeypatch.setattr(session_module, "ClientSession", FakeClientSession)

    adapter = MCPClientAdapter(
        MCPServerConfig.from_mapping("local", {"command": "uvx"}),
        list_changed_callback=lambda capability: changed.append(capability),
    )
    await adapter.connect()

    await seen["session"]._received_notification(
        SimpleNamespace(root=SimpleNamespace(method="notifications/tools/list_changed"))
    )

    assert seen["session"].original_notifications == 1
    assert changed == ["tools"]


@pytest.mark.asyncio
async def test_stdio_adapter_captures_bounded_stderr_on_connect_failure(monkeypatch) -> None:
    @asynccontextmanager
    async def fake_stdio_client(params, errlog=None):
        assert errlog is not None
        errlog.write("debug line\n")
        errlog.write("fatal line\n")
        yield object(), object()

    class FakeClientSession:
        def __init__(self, read_stream, write_stream, list_roots_callback=None):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc_val, exc_tb):
            return None

        async def initialize(self):
            raise RuntimeError("init failed")

    import mcp.client.session as session_module
    import mcp.client.stdio as stdio_module

    monkeypatch.setattr(stdio_module, "stdio_client", fake_stdio_client)
    monkeypatch.setattr(session_module, "ClientSession", FakeClientSession)

    adapter = MCPClientAdapter(MCPServerConfig.from_mapping("local", {"command": "uvx"}))

    with pytest.raises(MCPConnectionError, match="fatal line"):
        await adapter.connect()


@pytest.mark.asyncio
async def test_stdio_adapter_close_cleans_up_worker_after_connect_timeout(monkeypatch) -> None:
    initialized = threading.Event()
    closed = threading.Event()

    @asynccontextmanager
    async def fake_stdio_client(params, errlog=None):
        yield object(), object()

    class FakeClientSession:
        def __init__(self, read_stream, write_stream, list_roots_callback=None):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc_val, exc_tb):
            closed.set()

        async def initialize(self):
            initialized.set()
            await asyncio.Event().wait()

    import mcp.client.session as session_module
    import mcp.client.stdio as stdio_module

    monkeypatch.setattr(stdio_module, "stdio_client", fake_stdio_client)
    monkeypatch.setattr(session_module, "ClientSession", FakeClientSession)

    adapter = MCPClientAdapter(MCPServerConfig.from_mapping("local", {"command": "uvx"}))

    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(adapter.connect(), timeout=0.1)

    assert initialized.wait(timeout=1)
    await adapter.close()
    assert closed.wait(timeout=1)


@pytest.mark.asyncio
async def test_http_adapter_injects_stored_oauth_bearer_token(monkeypatch) -> None:
    seen: dict[str, Any] = {}
    config = MCPServerConfig.from_mapping(
        "remote",
        {
            "type": "http",
            "url": "https://example.com/mcp",
            "headers": {"X-Org": "iac"},
            "oauth": {"clientId": "client-id"},
        },
    )
    storage = FakeSecretStorage()
    storage.set_secret(oauth_storage_key(config, "access_token", scope=MCPConfigScope.USER), "stored-token")

    @asynccontextmanager
    async def fake_streamablehttp_client(url, headers=None):
        seen["url"] = url
        seen["headers"] = headers
        yield object(), object(), None

    class FakeClientSession:
        def __init__(self, read_stream, write_stream, list_roots_callback=None):
            self.closed = False

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc_val, exc_tb):
            self.closed = True

        async def initialize(self):
            return None

    import mcp.client.session as session_module
    import mcp.client.streamable_http as http_module

    monkeypatch.setattr(http_module, "streamablehttp_client", fake_streamablehttp_client)
    monkeypatch.setattr(session_module, "ClientSession", FakeClientSession)

    adapter = MCPClientAdapter(config, scope=MCPConfigScope.USER, secret_storage=storage)
    await adapter.connect()

    assert seen["url"] == "https://example.com/mcp"
    assert seen["headers"] == {"X-Org": "iac", "Authorization": "Bearer stored-token"}

    await adapter.close()


class FakeSecretStorage:
    def __init__(self) -> None:
        self._values: dict[str, str] = {}

    def set_secret(self, key: str, value: str) -> None:
        self._values[key] = value

    def get_secret(self, key: str) -> str | None:
        return self._values.get(key)

    def delete_secret(self, key: str) -> None:
        self._values.pop(key, None)
