"""Phase 1.5 MCP configuration conversion and injection scenario tests."""

from __future__ import annotations

import logging
from unittest.mock import MagicMock

import acp
import pytest

from iac_code.acp.mcp import convert_mcp_configs
from iac_code.acp.server import ACPServer
from iac_code.services.session_index import SessionEntry
from iac_code.services.session_resolver import ResolutionStatus, SessionResolution

# ---------------------------------------------------------------------------
# Helper factories
# ---------------------------------------------------------------------------


def _make_stdio_server(
    name: str = "my-mcp",
    command: str = "/usr/bin/node",
    args: list[str] | None = None,
    env: list[acp.schema.EnvVariable] | None = None,
) -> acp.schema.McpServerStdio:
    return acp.schema.McpServerStdio(
        name=name,
        command=command,
        args=args or ["server.js"],
        env=env or [],
    )


def _make_sse_server(
    name: str = "sse-mcp",
    url: str = "http://localhost:8080/sse",
    headers: list[acp.schema.HttpHeader] | None = None,
) -> acp.schema.SseMcpServer:
    return acp.schema.SseMcpServer(
        type="sse",
        name=name,
        url=url,
        headers=headers or [],
    )


def _make_http_server(
    name: str = "http-mcp",
    url: str = "http://localhost:9090/mcp",
    headers: list[acp.schema.HttpHeader] | None = None,
) -> acp.schema.HttpMcpServer:
    return acp.schema.HttpMcpServer(
        type="http",
        name=name,
        url=url,
        headers=headers or [],
    )


# ===========================================================================
# A. MCP configuration conversion scenarios
# ===========================================================================


class TestConvertMcpConfigs:
    """Functional scenario tests for the convert_mcp_configs function."""

    # A-1: stdio type parsed correctly
    def test_stdio_server_parsed(self) -> None:
        server = _make_stdio_server()
        result = convert_mcp_configs([server])

        assert len(result) == 1
        cfg = result[0]
        assert cfg["type"] == "stdio"
        assert cfg["command"] == "/usr/bin/node"
        assert cfg["args"] == ["server.js"]
        assert cfg["name"] == "my-mcp"

    # A-2: SSE type parsed correctly
    def test_sse_server_parsed(self) -> None:
        server = _make_sse_server()
        result = convert_mcp_configs([server])

        assert len(result) == 1
        cfg = result[0]
        assert cfg["type"] == "sse"
        assert cfg["url"] == "http://localhost:8080/sse"
        assert cfg["name"] == "sse-mcp"

    # A-3: HTTP type parsed correctly
    def test_http_server_parsed(self) -> None:
        server = _make_http_server()
        result = convert_mcp_configs([server])

        assert len(result) == 1
        cfg = result[0]
        assert cfg["type"] == "http"
        assert cfg["url"] == "http://localhost:9090/mcp"
        assert cfg["name"] == "http-mcp"

    # A-4: Unknown type skipped with warning
    def test_unknown_type_skipped_with_warning(self, caplog: pytest.LogCaptureFixture) -> None:
        unknown = MagicMock()
        unknown.__class__.__name__ = "WeirdServer"

        with caplog.at_level(logging.WARNING, logger="iac_code.acp.mcp"):
            result = convert_mcp_configs([unknown])

        assert result == []
        assert any("Unsupported MCP server type" in msg for msg in caplog.messages)

    # A-5: Empty list returns empty result
    def test_empty_list_returns_empty(self) -> None:
        assert convert_mcp_configs([]) == []

    # A-6: Mixed MCP server configurations
    def test_mixed_servers(self) -> None:
        servers = [_make_stdio_server(name="s1"), _make_sse_server(name="s2")]
        result = convert_mcp_configs(servers)

        assert len(result) == 2
        assert result[0]["type"] == "stdio"
        assert result[0]["name"] == "s1"
        assert result[1]["type"] == "sse"
        assert result[1]["name"] == "s2"

    # A-7: stdio config preserves all required fields
    def test_stdio_preserves_all_fields(self) -> None:
        env_vars = [
            acp.schema.EnvVariable(name="FOO", value="bar"),
            acp.schema.EnvVariable(name="BAZ", value="qux"),
        ]
        server = _make_stdio_server(
            name="full-mcp",
            command="/bin/my-server",
            args=["--port", "3000"],
            env=env_vars,
        )
        result = convert_mcp_configs([server])

        assert len(result) == 1
        cfg = result[0]
        assert cfg["command"] == "/bin/my-server"
        assert cfg["args"] == ["--port", "3000"]
        assert cfg["env"] == {"FOO": "bar", "BAZ": "qux"}
        assert cfg["name"] == "full-mcp"
        assert cfg["type"] == "stdio"


# ===========================================================================
# B. new_session / resume_session MCP injection scenarios
# ===========================================================================


class FakeConn:
    """Minimal fake ACP client connection for testing."""

    def __init__(self) -> None:
        self.updates: list = []

    async def session_update(self, session_id, update, **kwargs):
        self.updates.append((session_id, update))


class FakeLoop:
    tool_registry = None

    async def run_streaming(self, prompt):
        yield  # pragma: no cover


class FakeRuntime:
    session_id = "test-session"
    agent_loop = FakeLoop()
    tool_registry = None
    mcp_manager = None


def _patch_runtime(monkeypatch):
    """Patch create_agent_runtime to return FakeRuntime."""
    monkeypatch.setattr("iac_code.acp.server.create_agent_runtime", lambda opts: FakeRuntime())


class TestNewSessionMcpInjection:
    """MCP configuration injection scenarios for new_session and resume_session."""

    # B-8: new_session with mcp_servers passes config
    @pytest.mark.asyncio
    async def test_new_session_with_mcp_servers(self, monkeypatch) -> None:
        _patch_runtime(monkeypatch)
        server = ACPServer()
        conn = FakeConn()
        server.on_connect(conn)
        await server.initialize(protocol_version=1, client_capabilities=acp.schema.ClientCapabilities())

        stdio = _make_stdio_server(name="injected")
        resp = await server.new_session(cwd="/tmp", mcp_servers=[stdio])

        session = server.sessions[resp.session_id]
        assert len(session.mcp_configs) == 1
        assert session.mcp_configs[0]["name"] == "injected"
        assert session.mcp_configs[0]["type"] == "stdio"

    # B-9: new_session without mcp_servers creates normally
    @pytest.mark.asyncio
    async def test_new_session_without_mcp_servers(self, monkeypatch) -> None:
        _patch_runtime(monkeypatch)
        server = ACPServer()
        conn = FakeConn()
        server.on_connect(conn)
        await server.initialize(protocol_version=1, client_capabilities=acp.schema.ClientCapabilities())

        resp = await server.new_session(cwd="/tmp")

        session = server.sessions[resp.session_id]
        assert session.mcp_configs == []

    # B-10: resume_session with mcp_servers passes config
    @pytest.mark.asyncio
    async def test_resume_session_with_mcp_servers(self, monkeypatch) -> None:
        _patch_runtime(monkeypatch)
        server = ACPServer()
        conn = FakeConn()
        server.on_connect(conn)

        # Mock SessionStorage to pretend the session exists
        mock_storage_cls = MagicMock()
        mock_storage = mock_storage_cls.return_value
        mock_storage.exists.return_value = True
        mock_storage.load.return_value = []
        # repair_interrupted is a classmethod on the real SessionStorage; in the mock
        # it is accessed via the class, so configure it to return an empty list so
        # that resume_session skips history injection into agent_loop.
        mock_storage_cls.repair_interrupted.return_value = []
        monkeypatch.setattr("iac_code.acp.server.SessionStorage", mock_storage_cls)
        monkeypatch.setattr(
            "iac_code.acp.server.resolve_session_argument",
            lambda index, cwd, arg: SessionResolution(
                status=ResolutionStatus.FOUND,
                entry=SessionEntry(
                    session_id="test-session",
                    cwd="/tmp",
                    project_name="-tmp",
                    git_branch=None,
                    title="test-session",
                    mtime=0.0,
                    size_bytes=0,
                    name=None,
                    auto_title=None,
                    is_legacy=False,
                ),
            ),
        )

        sse = _make_sse_server(name="resumed-sse")
        await server.resume_session(cwd="/tmp", session_id="test-session", mcp_servers=[sse])

        session = server.sessions["test-session"]
        assert len(session.mcp_configs) == 1
        assert session.mcp_configs[0]["name"] == "resumed-sse"
        assert session.mcp_configs[0]["type"] == "sse"

    @pytest.mark.asyncio
    async def test_fork_session_with_mcp_servers(self, monkeypatch) -> None:
        _patch_runtime(monkeypatch)
        server = ACPServer()
        conn = FakeConn()
        server.on_connect(conn)
        await server.initialize(protocol_version=1, client_capabilities=acp.schema.ClientCapabilities())
        source = await server.new_session(cwd="/tmp")

        http = _make_http_server(name="forked-http")
        forked = await server.fork_session(cwd="/tmp", session_id=source.session_id, mcp_servers=[http])

        session = server.sessions[forked.session_id]
        assert len(session.mcp_configs) == 1
        assert session.mcp_configs[0]["name"] == "forked-http"
        assert session.mcp_configs[0]["type"] == "http"


# ===========================================================================
# C. Capability declaration scenarios
# ===========================================================================


class TestMcpCapabilities:
    """mcp_capabilities scenario tests in the initialize response."""

    # C-11: mcp_capabilities reflects actual capabilities (http=False, sse=False)
    @pytest.mark.asyncio
    async def test_mcp_capabilities_reflect_actual(self) -> None:
        server = ACPServer()
        conn = FakeConn()
        server.on_connect(conn)

        resp = await server.initialize(
            protocol_version=1,
            client_capabilities=acp.schema.ClientCapabilities(),
        )

        caps = resp.agent_capabilities.mcp_capabilities
        assert caps is not None
        assert caps.http is True
        assert caps.sse is True

    # C-12: mcp_capabilities field exists and has correct format
    @pytest.mark.asyncio
    async def test_mcp_capabilities_is_valid_object(self) -> None:
        server = ACPServer()
        conn = FakeConn()
        server.on_connect(conn)

        resp = await server.initialize(
            protocol_version=1,
            client_capabilities=acp.schema.ClientCapabilities(),
        )

        caps = resp.agent_capabilities.mcp_capabilities
        assert isinstance(caps, acp.schema.McpCapabilities)


class TestMcpSessionLifecycle:
    """MCP manager lifecycle scenarios."""

    @pytest.mark.asyncio
    async def test_acp_session_close_disconnects_mcp_manager(self) -> None:
        from iac_code.acp.session import ACPSession

        manager = FakeMCPManager()
        session = ACPSession(
            "mcp-session",
            FakeLoop(),
            FakeConn(),
            mcp_manager=manager,
        )

        await session.close()

        assert manager.disconnected is True


class FakeMCPManager:
    def __init__(self) -> None:
        self.disconnected = False

    async def disconnect_all(self) -> None:
        self.disconnected = True
