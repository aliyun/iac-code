"""Phase 1.5 MCP configuration conversion and injection scenario tests."""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import acp
import pytest

from iac_code.acp.http_sse import _create_memory_stream_pair
from iac_code.acp.mcp import convert_mcp_configs
from iac_code.acp.runner import run_iac_code_acp_agent
from iac_code.acp.server import ACPServer, _minimal_mcp_status_for_acp
from iac_code.commands.registry import CommandRegistry, PromptCommand
from iac_code.mcp.errors import MCPConnectionError, MCPNeedsAuthError
from iac_code.mcp.manager import MCPManager
from iac_code.mcp.types import (
    MCPConfigScope,
    MCPConfigWarning,
    MCPConnectionMetadata,
    MCPConnectionState,
    MCPPromptRecord,
    MCPServerConfig,
    MCPToolRecord,
    ScopedMCPServerConfig,
)
from iac_code.services.agent_factory import _append_new_mcp_connection_warnings, _session_mcp_configs
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

    def test_raw_dict_unknown_type_preserved_for_internal_validation(self) -> None:
        unknown = {
            "type": "tcp",
            "name": "future-mcp",
            "url": "tcp://localhost:9999",
        }

        result = convert_mcp_configs([unknown])

        assert result == [unknown]

    def test_raw_dict_mixed_servers_preserve_unknown_and_normalize_supported(self) -> None:
        servers = [
            {
                "type": "tcp",
                "name": "future-mcp",
                "url": "tcp://localhost:9999",
            },
            {
                "type": "http",
                "name": "http-mcp",
                "url": "http://localhost:9090/mcp",
                "headers": [{"name": "X-Test", "value": "ok"}],
            },
        ]

        result = convert_mcp_configs(servers)

        assert result == [
            {
                "type": "tcp",
                "name": "future-mcp",
                "url": "tcp://localhost:9999",
            },
            {
                "type": "http",
                "url": "http://localhost:9090/mcp",
                "headers": {"X-Test": "ok"},
                "name": "http-mcp",
            },
        ]

    def test_raw_dict_mapping_headers_and_env_are_preserved(self) -> None:
        result = convert_mcp_configs(
            [
                {
                    "type": "http",
                    "name": "http-mcp",
                    "url": "http://localhost:9090/mcp",
                    "headers": {"X-Test": "ok"},
                    "env": {"SHOULD_STAY": "yes"},
                }
            ]
        )

        assert result == [
            {
                "type": "http",
                "name": "http-mcp",
                "url": "http://localhost:9090/mcp",
                "headers": {"X-Test": "ok"},
                "env": {"SHOULD_STAY": "yes"},
            }
        ]

    def test_raw_dict_ws_server_preserved_for_internal_runtime(self) -> None:
        result = convert_mcp_configs(
            [
                {
                    "type": "ws",
                    "name": "ws-mcp",
                    "url": "ws://localhost:9090/mcp",
                }
            ]
        )

        assert result == [
            {
                "type": "ws",
                "url": "ws://localhost:9090/mcp",
                "name": "ws-mcp",
            }
        ]

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


class ClosableFakeRuntime:
    session_id = "test-session"
    agent_loop = FakeLoop()
    tool_registry = None
    command_registry = CommandRegistry()
    mcp_config_warnings: list = []

    def __init__(self, manager=None) -> None:
        self.mcp_manager = manager
        self.closed = False

    async def aclose(self) -> None:
        self.closed = True


class FailingSessionUpdateConn(FakeConn):
    async def session_update(self, session_id, update, **kwargs):
        await super().session_update(session_id, update, **kwargs)
        raise BrokenPipeError("client disconnected")


class LiveFailingMCPClient:
    metadata = None

    def __init__(self) -> None:
        self.closed = False

    async def connect(self) -> None:
        return None

    async def close(self) -> None:
        self.closed = True

    async def list_tools(self) -> list[dict[str, object]]:
        return [{"name": "search", "inputSchema": {"type": "object"}}]

    async def list_resources(self) -> list[dict[str, object]]:
        return []

    async def list_prompts(self) -> list[dict[str, object]]:
        return []

    async def call_tool(self, name: str, arguments: dict[str, object] | None = None, **kwargs: object) -> dict:
        _ = name, arguments, kwargs
        raise MCPConnectionError("transport closed while streaming tool response")


class LiveNeedsAuthMCPClient(LiveFailingMCPClient):
    async def call_tool(self, name: str, arguments: dict[str, object] | None = None, **kwargs: object) -> dict:
        _ = name, arguments, kwargs
        raise MCPNeedsAuthError("MCP server 'remote' requires authentication")


class LiveConnectNeedsAuthMCPClient(LiveFailingMCPClient):
    async def connect(self) -> None:
        raise MCPNeedsAuthError("MCP server 'remote' requires authentication")


def _patch_runtime(monkeypatch):
    """Patch create_agent_runtime to return FakeRuntime."""
    monkeypatch.setattr("iac_code.acp.server.create_agent_runtime", lambda opts: FakeRuntime())


def _mcp_warnings_from_real_loader(cwd: str, configs: list[dict[str, object]]) -> list[MCPConfigWarning]:
    from iac_code.mcp.config import load_mcp_configs

    cwd_path = Path(cwd)
    result = load_mcp_configs(
        cwd=cwd_path,
        workspace_root=cwd_path,
        session_configs=_session_mcp_configs(configs),
        include_pending_project=False,
    )
    return result.warnings


def _session_update_frame_size(session_id: str, update: object) -> int:
    notification = acp.schema.SessionNotification(session_id=session_id, update=update)
    payload = {
        "jsonrpc": "2.0",
        "method": "session/update",
        "params": notification.model_dump(by_alias=True, exclude_none=True, exclude_defaults=True),
    }
    return len((json.dumps(payload, separators=(",", ":")) + "\n").encode("utf-8"))


async def _write_rpc(reader: asyncio.StreamReader, payload: dict[str, object]) -> None:
    reader.feed_data((json.dumps(payload) + "\n").encode("utf-8"))


async def _read_rpc_until_id(
    response_reader: asyncio.StreamReader,
    message_id: int,
    *,
    skipped: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    while True:
        line = await asyncio.wait_for(response_reader.readline(), timeout=2.0)
        assert line, f"ACP stream closed before response id={message_id}"
        payload = json.loads(line)
        if payload.get("id") == message_id:
            return payload
        if skipped is not None:
            skipped.append(payload)


def _large_mcp_status_record() -> SimpleNamespace:
    long_text = "x" * 1000
    tools = [
        MCPToolRecord(
            server_name="large",
            tool_name=f"tool_{index}",
            public_name=f"mcp__large__tool_{index}",
            description=long_text,
            input_schema={
                "type": "object",
                "properties": {
                    f"field_{index}_{field_index}": {
                        "type": "string",
                        "description": long_text,
                        "api_key": "should-not-leak-acp-secret",
                    }
                    for field_index in range(4)
                },
            },
            annotations={"title": long_text},
        )
        for index in range(128)
    ]
    prompts = [
        MCPPromptRecord(
            server_name="large",
            prompt_name=f"prompt_{index}",
            public_name=f"mcp__large__prompt_{index}",
            description=long_text,
            arguments={
                "template": {
                    "description": long_text,
                    "client_secret": "should-not-leak-acp-secret",
                }
            },
        )
        for index in range(12)
    ]
    return SimpleNamespace(
        name="large",
        state=MCPConnectionState.CONNECTED,
        error=None,
        capability_errors={},
        tools=tools,
        resources=[],
        prompts=prompts,
        retry_count=0,
        metadata=MCPConnectionMetadata(
            state=MCPConnectionState.CONNECTED,
            server_name="large",
            protocol_version="2025-06-18",
        ),
    )


def test_minimal_mcp_status_for_acp_preserves_protocol_version() -> None:
    status = {
        "servers": [
            {
                "serverName": "large",
                "state": "connected",
                "protocolVersion": "2025-06-18",
                "toolsCount": 128,
                "tools": [{"inputSchema": {"api_key": "should-not-leak-acp-secret"}}],
            }
        ],
        "warnings": [],
    }

    minimal = _minimal_mcp_status_for_acp(status)

    assert minimal["servers"][0]["protocolVersion"] == "2025-06-18"
    assert "inputSchema" not in repr(minimal)
    assert "should-not-leak-acp-secret" not in repr(minimal)


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

    @pytest.mark.asyncio
    async def test_jsonrpc_new_session_skips_unknown_raw_mcp_server_type(
        self,
        monkeypatch,
        tmp_path,
    ) -> None:
        captured_configs: list[dict[str, object]] = []

        def _fake_runtime(options):
            captured_configs.extend(options.mcp_configs or [])
            runtime = ClosableFakeRuntime()
            runtime.mcp_config_warnings = _mcp_warnings_from_real_loader(options.cwd, options.mcp_configs or [])
            return runtime

        monkeypatch.setattr("iac_code.acp.server.create_agent_runtime", _fake_runtime)
        server = ACPServer()
        request_reader = asyncio.StreamReader()
        response_reader, response_writer = _create_memory_stream_pair()
        agent_task = asyncio.create_task(
            run_iac_code_acp_agent(
                server,
                input_stream=response_writer,
                output_stream=request_reader,
                use_unstable_protocol=True,
            )
        )
        try:
            await _write_rpc(
                request_reader,
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {"protocolVersion": 1, "clientCapabilities": {}},
                },
            )
            initialize_response = await _read_rpc_until_id(response_reader, 1)
            assert "result" in initialize_response

            await _write_rpc(
                request_reader,
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "session/new",
                    "params": {
                        "cwd": str(tmp_path),
                        "mcpServers": [
                            {
                                "type": "tcp",
                                "name": "future-mcp",
                                "url": "tcp://localhost:9999",
                            }
                        ],
                    },
                },
            )
            skipped_messages: list[dict[str, object]] = []
            new_session_response = await _read_rpc_until_id(response_reader, 2, skipped=skipped_messages)

            assert "error" not in new_session_response
            assert new_session_response["result"]["sessionId"] == "test-session"
            assert captured_configs == [
                {
                    "type": "tcp",
                    "name": "future-mcp",
                    "url": "tcp://localhost:9999",
                }
            ]
            warning_metadata = [
                message["params"]["update"]["_meta"]["iac_code"]["mcpWarning"]
                for message in skipped_messages
                if message.get("method") == "session/update"
                and "mcpWarning" in message.get("params", {}).get("update", {}).get("_meta", {}).get("iac_code", {})
            ]
            assert warning_metadata
            warning_meta = warning_metadata[0]
            assert warning_meta["serverName"] == "future-mcp"
            assert warning_meta["code"] == "invalid_config"
            assert "Unsupported MCP transport" in warning_meta["message"]
        finally:
            request_reader.feed_eof()
            await asyncio.wait_for(agent_task, timeout=2.0)
            await server.shutdown()

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

    @pytest.mark.asyncio
    async def test_new_session_pushes_redacted_mcp_warning_and_status_metadata(self, monkeypatch) -> None:
        private_marker = "IAC_PRIVATE_COMMAND_ARG_MARKER_36_ACP"
        scoped_config = ScopedMCPServerConfig(
            config=MCPServerConfig.from_mapping("broken", {"command": "node", "args": ["server.js", private_marker]}),
            scope=MCPConfigScope.USER,
        )
        manager = SimpleNamespace(
            list_connections=lambda: [
                SimpleNamespace(
                    name="broken",
                    scoped_config=scoped_config,
                    state=MCPConnectionState.FAILED,
                    error="Authorization: Bearer super-secret-token",
                    capability_errors={},
                    tools=[],
                    resources=[],
                    prompts=[],
                    retry_count=1,
                    metadata=None,
                )
            ]
        )
        runtime = SimpleNamespace(
            session_id="test-session",
            agent_loop=FakeLoop(),
            tool_registry=None,
            mcp_manager=manager,
            mcp_config_warnings=[
                SimpleNamespace(
                    server_name="broken",
                    code="connection_failed",
                    message="MCP failed with Authorization: Bearer super-secret-token",
                )
            ],
        )
        monkeypatch.setattr("iac_code.acp.server.create_agent_runtime", lambda opts: runtime)
        server = ACPServer()
        conn = FakeConn()
        server.on_connect(conn)
        await server.initialize(protocol_version=1, client_capabilities=acp.schema.ClientCapabilities())

        await server.new_session(cwd="/tmp")

        metas = [
            update.model_dump(by_alias=True).get("_meta") or {}
            for _session_id, update in conn.updates
            if hasattr(update, "model_dump")
        ]
        warning_meta = next(
            meta["iac_code"]["mcpWarning"] for meta in metas if "mcpWarning" in meta.get("iac_code", {})
        )
        status_meta = next(meta["iac_code"]["mcpStatus"] for meta in metas if "mcpStatus" in meta.get("iac_code", {}))
        assert warning_meta["serverName"] == "broken"
        assert warning_meta["code"] == "connection_failed"
        assert status_meta["servers"][0]["serverName"] == "broken"
        assert status_meta["servers"][0]["state"] == "failed"
        assert "super-secret-token" not in repr(warning_meta)
        assert "super-secret-token" not in repr(status_meta)
        assert private_marker not in repr(status_meta)

    @pytest.mark.asyncio
    async def test_new_session_includes_pending_project_servers_in_mcp_status(self, monkeypatch) -> None:
        pending_config = ScopedMCPServerConfig(
            config=MCPServerConfig.from_mapping("pending", {"command": "npx", "args": ["-y", "server"]}),
            scope=MCPConfigScope.PROJECT,
            source_path="/Users/alice/repo/.mcp.json",
            approved=False,
        )
        runtime = SimpleNamespace(
            session_id="test-session",
            agent_loop=FakeLoop(),
            tool_registry=None,
            mcp_manager=None,
            mcp_config_warnings=[
                MCPConfigWarning(
                    source="/Users/alice/repo/.mcp.json",
                    server_name="pending",
                    code="pending_approval",
                    message="Project MCP server 'pending' is pending approval.",
                )
            ],
            mcp_pending_configs=[pending_config],
        )
        monkeypatch.setattr("iac_code.acp.server.create_agent_runtime", lambda opts: runtime)
        server = ACPServer()
        conn = FakeConn()
        server.on_connect(conn)
        await server.initialize(protocol_version=1, client_capabilities=acp.schema.ClientCapabilities())

        await server.new_session(cwd="/tmp")

        metas = [
            update.model_dump(by_alias=True).get("_meta") or {}
            for _session_id, update in conn.updates
            if hasattr(update, "model_dump")
        ]
        status_meta = next(meta["iac_code"]["mcpStatus"] for meta in metas if "mcpStatus" in meta.get("iac_code", {}))
        pending_server = status_meta["servers"][0]
        assert pending_server["serverName"] == "pending"
        assert pending_server["state"] == "pending-approval"
        assert pending_server["scope"] == "project"
        assert pending_server["command"] == "npx"
        assert "/Users/alice" not in pending_server["sourcePath"]

    @pytest.mark.asyncio
    async def test_large_mcp_status_update_fits_default_acp_reader_limit(self, monkeypatch) -> None:
        manager = SimpleNamespace(list_connections=lambda: [_large_mcp_status_record()])
        runtime = SimpleNamespace(
            session_id="test-session",
            agent_loop=FakeLoop(),
            tool_registry=None,
            mcp_manager=manager,
            mcp_config_warnings=[],
        )
        monkeypatch.setattr("iac_code.acp.server.create_agent_runtime", lambda opts: runtime)
        server = ACPServer()
        conn = FakeConn()
        server.on_connect(conn)
        await server.initialize(protocol_version=1, client_capabilities=acp.schema.ClientCapabilities())

        await server.new_session(cwd="/tmp")

        status_update = next(
            update
            for _session_id, update in conn.updates
            if (
                hasattr(update, "model_dump")
                and "mcpStatus" in (update.model_dump(by_alias=True).get("_meta") or {}).get("iac_code", {})
            )
        )
        status_meta = status_update.model_dump(by_alias=True)["_meta"]["iac_code"]["mcpStatus"]
        assert _session_update_frame_size("test-session", status_update) < 64 * 1024
        assert status_meta["truncated"] is True
        assert status_meta["truncationReason"] == "acp-frame-size-limit"
        assert status_meta["servers"][0]["serverName"] == "large"
        assert status_meta["servers"][0]["state"] == "connected"
        assert status_meta["servers"][0]["protocolVersion"] == "2025-06-18"
        assert status_meta["servers"][0]["truncated"] is True
        assert status_meta["servers"][0]["toolsCount"] == 128
        assert status_meta["servers"][0]["promptsCount"] == 12
        assert "should-not-leak-acp-secret" not in repr(status_meta)
        assert "inputSchema" not in repr(status_meta)

    @pytest.mark.asyncio
    async def test_new_session_rolls_back_runtime_when_mcp_status_push_fails(self, monkeypatch) -> None:
        manager = SimpleNamespace(
            list_connections=lambda: [
                SimpleNamespace(
                    name="remote",
                    state=MCPConnectionState.CONNECTED,
                    error=None,
                    capability_errors={},
                    tools=[],
                    resources=[],
                    prompts=[],
                    retry_count=0,
                    metadata=None,
                )
            ]
        )
        runtime = ClosableFakeRuntime(manager=manager)
        monkeypatch.setattr("iac_code.acp.server.create_agent_runtime", lambda opts: runtime)
        server = ACPServer()
        conn = FailingSessionUpdateConn()
        server.on_connect(conn)
        await server.initialize(protocol_version=1, client_capabilities=acp.schema.ClientCapabilities())

        with pytest.raises(BrokenPipeError):
            await server.new_session(cwd="/tmp")

        assert runtime.closed is True
        assert server.sessions == {}

    @pytest.mark.asyncio
    async def test_fork_session_rolls_back_new_runtime_when_mcp_status_push_fails(self, monkeypatch) -> None:
        manager = SimpleNamespace(
            list_connections=lambda: [
                SimpleNamespace(
                    name="remote",
                    state=MCPConnectionState.CONNECTED,
                    error=None,
                    capability_errors={},
                    tools=[],
                    resources=[],
                    prompts=[],
                    retry_count=0,
                    metadata=None,
                )
            ]
        )
        fork_runtime = ClosableFakeRuntime(manager=manager)
        runtimes = [FakeRuntime(), fork_runtime]
        monkeypatch.setattr("iac_code.acp.server.create_agent_runtime", lambda opts: runtimes.pop(0))
        server = ACPServer()
        server.on_connect(FakeConn())
        await server.initialize(protocol_version=1, client_capabilities=acp.schema.ClientCapabilities())
        source = await server.new_session(cwd="/tmp")

        server.on_connect(FailingSessionUpdateConn())
        with pytest.raises(BrokenPipeError):
            await server.fork_session(cwd="/tmp", session_id=source.session_id)

        assert fork_runtime.closed is True
        assert list(server.sessions) == [source.session_id]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("capability", ["auth", "connection"])
    async def test_mcp_auth_or_connection_change_pushes_available_commands(self, monkeypatch, capability) -> None:
        listeners = []
        registry = CommandRegistry()
        registry.register(PromptCommand(name="mcp__remote__review", description="Review with MCP"))
        runtime = SimpleNamespace(
            session_id="test-session",
            agent_loop=FakeLoop(),
            tool_registry=None,
            command_registry=registry,
            mcp_manager=SimpleNamespace(list_connections=lambda: []),
            mcp_config_warnings=[],
            add_mcp_change_listener=lambda listener: listeners.append(listener),
        )
        monkeypatch.setattr("iac_code.acp.server.create_agent_runtime", lambda opts: runtime)
        server = ACPServer()
        conn = FakeConn()
        server.on_connect(conn)
        await server.initialize(protocol_version=1, client_capabilities=acp.schema.ClientCapabilities())

        response = await server.new_session(cwd="/tmp")
        conn.updates.clear()
        await listeners[0]("remote", capability)

        command_updates = [
            update
            for _session_id, update in conn.updates
            if getattr(update, "session_update", None) == "available_commands_update"
        ]
        assert command_updates
        assert any(command.name == "mcp__remote__review" for command in command_updates[-1].available_commands)
        assert response.session_id == "test-session"

    @pytest.mark.asyncio
    async def test_active_resume_repushes_mcp_status_metadata(self, monkeypatch) -> None:
        manager = SimpleNamespace(
            list_connections=lambda: [
                SimpleNamespace(
                    name="remote",
                    state=MCPConnectionState.CONNECTED,
                    error=None,
                    capability_errors={},
                    tools=[],
                    resources=[],
                    prompts=[],
                    retry_count=0,
                    metadata=None,
                )
            ]
        )
        runtime = SimpleNamespace(
            session_id="test-session",
            agent_loop=FakeLoop(),
            tool_registry=None,
            mcp_manager=manager,
            mcp_config_warnings=[],
        )
        monkeypatch.setattr("iac_code.acp.server.create_agent_runtime", lambda opts: runtime)
        server = ACPServer()
        conn = FakeConn()
        server.on_connect(conn)
        await server.initialize(protocol_version=1, client_capabilities=acp.schema.ClientCapabilities())

        response = await server.new_session(cwd="/tmp")
        conn.updates.clear()

        await server.resume_session(cwd="/tmp", session_id=response.session_id)

        metas = [
            update.model_dump(by_alias=True).get("_meta") or {}
            for _session_id, update in conn.updates
            if hasattr(update, "model_dump")
        ]
        status_meta = next(meta["iac_code"]["mcpStatus"] for meta in metas if "mcpStatus" in meta.get("iac_code", {}))
        assert status_meta["servers"][0]["serverName"] == "remote"
        assert status_meta["servers"][0]["state"] == "connected"

    @pytest.mark.asyncio
    async def test_resolved_active_resume_repushes_mcp_status_metadata(self, monkeypatch) -> None:
        manager = SimpleNamespace(
            list_connections=lambda: [
                SimpleNamespace(
                    name="remote",
                    state=MCPConnectionState.CONNECTED,
                    error=None,
                    capability_errors={},
                    tools=[],
                    resources=[],
                    prompts=[],
                    retry_count=0,
                    metadata=None,
                )
            ]
        )
        runtime = SimpleNamespace(
            session_id="test-session",
            agent_loop=FakeLoop(),
            tool_registry=None,
            mcp_manager=manager,
            mcp_config_warnings=[],
        )
        monkeypatch.setattr("iac_code.acp.server.create_agent_runtime", lambda opts: runtime)
        server = ACPServer()
        conn = FakeConn()
        server.on_connect(conn)
        await server.initialize(protocol_version=1, client_capabilities=acp.schema.ClientCapabilities())
        response = await server.new_session(cwd="/tmp")
        conn.updates.clear()
        monkeypatch.setattr(
            "iac_code.acp.server.resolve_session_argument",
            lambda index, cwd, arg: SessionResolution(
                status=ResolutionStatus.FOUND,
                entry=SessionEntry(
                    session_id=response.session_id,
                    cwd="/tmp",
                    project_name="-tmp",
                    git_branch=None,
                    title="Deploy prod",
                    mtime=0.0,
                    size_bytes=0,
                    name="deploy-prod",
                    auto_title=None,
                    is_legacy=False,
                ),
            ),
        )

        await server.resume_session(cwd="/tmp", session_id="deploy-prod")

        metas = [
            update.model_dump(by_alias=True).get("_meta") or {}
            for _session_id, update in conn.updates
            if hasattr(update, "model_dump")
        ]
        status_meta = next(meta["iac_code"]["mcpStatus"] for meta in metas if "mcpStatus" in meta.get("iac_code", {}))
        assert status_meta["servers"][0]["serverName"] == "remote"
        assert status_meta["servers"][0]["state"] == "connected"

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

    @pytest.mark.asyncio
    async def test_live_mcp_connection_failure_pushes_failed_status_metadata(self) -> None:
        client = LiveFailingMCPClient()
        scoped = ScopedMCPServerConfig(
            config=MCPServerConfig.from_mapping("remote", {"type": "http", "url": "https://example.com/mcp"}),
            scope=MCPConfigScope.SESSION,
        )
        manager = MCPManager([scoped], client_factory=lambda config: client)
        await manager.connect_all()

        class Runtime:
            session_id = "test-session"
            agent_loop = FakeLoop()
            tool_registry = None
            command_registry = CommandRegistry()
            mcp_pending_configs: list = []

            def __init__(self) -> None:
                self.mcp_manager = manager
                self.mcp_config_warnings: list[MCPConfigWarning] = []
                self._listeners = []
                manager.add_change_listener(self._on_mcp_changed)

            def add_mcp_change_listener(self, listener) -> None:
                self._listeners.append(listener)

            async def _on_mcp_changed(self, server_name: str, capability: str) -> None:
                _append_new_mcp_connection_warnings(self.mcp_config_warnings, self.mcp_manager)
                for listener in list(self._listeners):
                    result = listener(server_name, capability)
                    if asyncio.iscoroutine(result):
                        await result

        runtime = Runtime()
        conn = FakeConn()
        server = ACPServer()
        server.conn = conn
        session = server._create_acp_session_from_runtime(runtime=runtime, mcp_configs=[])
        server.sessions[session.id] = session

        with pytest.raises(MCPConnectionError):
            await manager.call_tool("remote", "search", {})

        status_metas = [
            meta["iac_code"]["mcpStatus"]
            for _session_id, update in conn.updates
            if hasattr(update, "model_dump")
            and "mcpStatus" in (meta := (update.model_dump(by_alias=True).get("_meta") or {})).get("iac_code", {})
        ]
        assert status_metas
        assert status_metas[-1]["servers"][0]["state"] == "failed"
        assert status_metas[-1]["servers"][0]["latestFailureReason"] == "transport closed while streaming tool response"

    @pytest.mark.asyncio
    async def test_live_mcp_needs_auth_pushes_needs_auth_status_metadata(self) -> None:
        client = LiveNeedsAuthMCPClient()
        scoped = ScopedMCPServerConfig(
            config=MCPServerConfig.from_mapping("remote", {"type": "http", "url": "https://example.com/mcp"}),
            scope=MCPConfigScope.SESSION,
        )
        manager = MCPManager([scoped], client_factory=lambda config: client)
        await manager.connect_all()

        class Runtime:
            session_id = "test-session"
            agent_loop = FakeLoop()
            tool_registry = None
            command_registry = CommandRegistry()
            mcp_pending_configs: list = []

            def __init__(self) -> None:
                self.mcp_manager = manager
                self.mcp_config_warnings: list[MCPConfigWarning] = []
                self._listeners = []
                manager.add_change_listener(self._on_mcp_changed)

            def add_mcp_change_listener(self, listener) -> None:
                self._listeners.append(listener)

            async def _on_mcp_changed(self, server_name: str, capability: str) -> None:
                _append_new_mcp_connection_warnings(self.mcp_config_warnings, self.mcp_manager)
                for listener in list(self._listeners):
                    result = listener(server_name, capability)
                    if asyncio.iscoroutine(result):
                        await result

        runtime = Runtime()
        conn = FakeConn()
        server = ACPServer()
        server.conn = conn
        session = server._create_acp_session_from_runtime(runtime=runtime, mcp_configs=[])
        server.sessions[session.id] = session

        with pytest.raises(MCPNeedsAuthError):
            await manager.call_tool("remote", "search", {})

        status_metas = [
            meta["iac_code"]["mcpStatus"]
            for _session_id, update in conn.updates
            if hasattr(update, "model_dump")
            and "mcpStatus" in (meta := (update.model_dump(by_alias=True).get("_meta") or {})).get("iac_code", {})
        ]
        assert status_metas
        assert status_metas[-1]["servers"][0]["state"] == "needs-auth"
        assert status_metas[-1]["servers"][0]["authState"] == "needs-auth"

    @pytest.mark.asyncio
    async def test_live_mcp_connect_all_needs_auth_transition_pushes_needs_auth_status_metadata(self) -> None:
        clients = [LiveFailingMCPClient(), LiveConnectNeedsAuthMCPClient()]
        scoped = ScopedMCPServerConfig(
            config=MCPServerConfig.from_mapping("remote", {"type": "http", "url": "https://example.com/mcp"}),
            scope=MCPConfigScope.SESSION,
        )
        manager = MCPManager([scoped], client_factory=lambda config: clients.pop(0))
        await manager.connect_all()

        class Runtime:
            session_id = "test-session"
            agent_loop = FakeLoop()
            tool_registry = None
            command_registry = CommandRegistry()
            mcp_pending_configs: list = []

            def __init__(self) -> None:
                self.mcp_manager = manager
                self.mcp_config_warnings: list[MCPConfigWarning] = []
                self._listeners = []
                manager.add_change_listener(self._on_mcp_changed)

            def add_mcp_change_listener(self, listener) -> None:
                self._listeners.append(listener)

            async def _on_mcp_changed(self, server_name: str, capability: str) -> None:
                _append_new_mcp_connection_warnings(self.mcp_config_warnings, self.mcp_manager)
                for listener in list(self._listeners):
                    result = listener(server_name, capability)
                    if asyncio.iscoroutine(result):
                        await result

        runtime = Runtime()
        conn = FakeConn()
        server = ACPServer()
        server.conn = conn
        session = server._create_acp_session_from_runtime(runtime=runtime, mcp_configs=[])
        server.sessions[session.id] = session

        await manager.connect_all()

        status_metas = [
            meta["iac_code"]["mcpStatus"]
            for _session_id, update in conn.updates
            if hasattr(update, "model_dump")
            and "mcpStatus" in (meta := (update.model_dump(by_alias=True).get("_meta") or {})).get("iac_code", {})
        ]
        assert status_metas
        assert status_metas[-1]["servers"][0]["state"] == "needs-auth"
        assert status_metas[-1]["servers"][0]["authState"] == "needs-auth"


class FakeMCPManager:
    def __init__(self) -> None:
        self.disconnected = False

    async def disconnect_all(self) -> None:
        self.disconnected = True
