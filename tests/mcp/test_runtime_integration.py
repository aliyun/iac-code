from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from typing import Any

import pytest

from iac_code.mcp.types import MCPConnectionState, MCPPromptRecord, MCPResourceRecord, MCPToolRecord
from iac_code.services.agent_factory import (
    AgentFactoryOptions,
    AgentRuntime,
    _mcp_auth_flow_factory,
    create_agent_runtime,
)
from iac_code.tools.base import ToolContext


def test_create_runtime_registers_discovered_mcp_tools_and_resource_tools(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    manager = FakeMCPManager(
        tools=[
            MCPToolRecord(
                server_name="ros",
                tool_name="plan",
                public_name="mcp__ros__plan",
                input_schema={"type": "object"},
            )
        ],
        resources=[MCPResourceRecord(server_name="ros", uri="skill://ros/vpc", name="vpc")],
        prompts=[
            MCPPromptRecord(
                server_name="ros",
                prompt_name="review",
                public_name="mcp__ros__review",
                arguments={},
            )
        ],
    )

    runtime = create_agent_runtime(
        AgentFactoryOptions(
            model="qwen3.7-max",
            session_id="session-1",
            cwd=str(tmp_path),
            mcp_configs=[{"name": "ros", "command": "uvx"}],
            mcp_manager_factory=lambda configs, roots: manager,
        )
    )

    assert manager.connected is True
    assert runtime.mcp_manager is manager
    assert runtime.tool_registry.get("mcp__ros__plan") is not None
    assert runtime.tool_registry.get("list_mcp_resources") is not None
    assert runtime.tool_registry.get("read_mcp_resource") is not None
    assert runtime.command_registry.get("mcp__ros__review") is not None
    assert runtime.command_registry.get("mcp__ros__vpc") is not None


@pytest.mark.asyncio
async def test_agent_runtime_aclose_disconnects_mcp_manager(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    manager = FakeMCPManager(tools=[MCPToolRecord(server_name="ros", tool_name="plan", public_name="mcp__ros__plan")])

    runtime = create_agent_runtime(
        AgentFactoryOptions(
            model="qwen3.7-max",
            session_id="session-1",
            cwd=str(tmp_path),
            mcp_configs=[{"name": "ros", "command": "uvx"}],
            mcp_manager_factory=lambda configs, roots: manager,
        )
    )

    await runtime.aclose()

    assert manager.disconnected is True


@pytest.mark.asyncio
async def test_agent_runtime_aclose_cancels_pending_mcp_auth_flow(monkeypatch) -> None:
    closed = threading.Event()

    class FakeCallback:
        def close(self) -> None:
            closed.set()

    class FakeFlow:
        authorization_url = "https://auth.example/authorize"
        browser_opened = False

        def __init__(self) -> None:
            self.callback = FakeCallback()

        def wait(self) -> None:
            closed.wait(timeout=5)
            raise RuntimeError("flow closed")

    class FakeManager:
        disconnected = False

        async def reconnect(self, server_name: str) -> None:
            raise AssertionError("closed auth flow should not reconnect")

        async def disconnect_all(self) -> None:
            self.disconnected = True

    import iac_code.mcp.oauth as oauth_module

    fake_flow = FakeFlow()
    monkeypatch.setattr(oauth_module, "start_oauth_loopback_flow", lambda *args, **kwargs: fake_flow)
    auth_tasks: set[asyncio.Task[Any]] = set()
    auth_flows: set[Any] = set()
    manager = FakeManager()
    auth_flow = _mcp_auth_flow_factory(
        {"live": type("Scoped", (), {"config": object(), "scope": "user"})()},
        manager,
        auth_tasks=auth_tasks,
        auth_flows=auth_flows,
    )

    message = await auth_flow("live")
    assert "https://auth.example/authorize" in message
    assert fake_flow in auth_flows
    assert len(auth_tasks) == 1

    runtime = AgentRuntime(
        agent_loop=object(),
        session_id="session-1",
        tool_registry=object(),
        provider_manager=object(),
        command_registry=object(),
        task_manager=object(),
        memory_manager=object(),
        legacy_memory_manager=object(),
        mcp_manager=manager,
        _mcp_auth_tasks=auth_tasks,
        _mcp_auth_flows=auth_flows,
    )

    await runtime.aclose()

    assert closed.is_set()
    assert not auth_tasks
    assert not auth_flows
    assert manager.disconnected is True


@pytest.mark.asyncio
async def test_runtime_mcp_list_changed_refreshes_registered_tools(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    manager = FakeMCPManager(
        tools=[
            MCPToolRecord(
                server_name="ros",
                tool_name="plan",
                public_name="mcp__ros__plan",
                input_schema={"type": "object"},
            )
        ]
    )

    runtime = create_agent_runtime(
        AgentFactoryOptions(
            model="qwen3.7-max",
            session_id="session-1",
            cwd=str(tmp_path),
            mcp_configs=[{"name": "ros", "command": "uvx"}],
            mcp_manager_factory=lambda configs, roots: manager,
        )
    )

    assert runtime.tool_registry.get("mcp__ros__plan") is not None
    manager._tools = [
        MCPToolRecord(
            server_name="ros",
            tool_name="apply",
            public_name="mcp__ros__apply",
            input_schema={"type": "object"},
        )
    ]

    await manager.listeners[0]("ros", "tools")

    assert runtime.tool_registry.get("mcp__ros__plan") is None
    assert runtime.tool_registry.get("mcp__ros__apply") is not None


@pytest.mark.asyncio
async def test_runtime_mcp_resources_changed_unregisters_resource_tools_when_empty(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    manager = FakeMCPManager(
        resources=[MCPResourceRecord(server_name="ros", uri="resource://template", name="template")]
    )

    runtime = create_agent_runtime(
        AgentFactoryOptions(
            model="qwen3.7-max",
            session_id="session-1",
            cwd=str(tmp_path),
            mcp_configs=[{"name": "ros", "command": "uvx"}],
            mcp_manager_factory=lambda configs, roots: manager,
        )
    )

    assert runtime.tool_registry.get("list_mcp_resources") is not None
    assert runtime.tool_registry.get("read_mcp_resource") is not None
    manager._resources = []

    await manager.listeners[0]("ros", "resources")

    assert runtime.tool_registry.get("list_mcp_resources") is None
    assert runtime.tool_registry.get("read_mcp_resource") is None


@pytest.mark.asyncio
async def test_runtime_mcp_prompts_changed_refreshes_prompt_commands(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    manager = FakeMCPManager(
        prompts=[MCPPromptRecord(server_name="ros", prompt_name="review", public_name="mcp__ros__review")]
    )

    runtime = create_agent_runtime(
        AgentFactoryOptions(
            model="qwen3.7-max",
            session_id="session-1",
            cwd=str(tmp_path),
            mcp_configs=[{"name": "ros", "command": "uvx"}],
            mcp_manager_factory=lambda configs, roots: manager,
        )
    )

    assert runtime.command_registry.get("mcp__ros__review") is not None
    manager._prompts = [MCPPromptRecord(server_name="ros", prompt_name="deploy", public_name="mcp__ros__deploy")]

    await manager.listeners[0]("ros", "prompts")

    assert runtime.command_registry.get("mcp__ros__review") is None
    assert runtime.command_registry.get("mcp__ros__deploy") is not None
    assert "mcp__ros__review" not in runtime.agent_loop.system_prompt
    assert "mcp__ros__deploy" in runtime.agent_loop.system_prompt
    mcp_auto_trigger_names = [
        command.name for command in runtime.agent_loop._auto_trigger_skills if command.name.startswith("mcp__")
    ]
    assert mcp_auto_trigger_names == ["mcp__ros__deploy"]


@pytest.mark.asyncio
async def test_runtime_mcp_resources_changed_refreshes_skill_commands(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    manager = FakeMCPManager(resources=[MCPResourceRecord(server_name="ros", uri="skill://ros/vpc", name="vpc")])

    runtime = create_agent_runtime(
        AgentFactoryOptions(
            model="qwen3.7-max",
            session_id="session-1",
            cwd=str(tmp_path),
            mcp_configs=[{"name": "ros", "command": "uvx"}],
            mcp_manager_factory=lambda configs, roots: manager,
        )
    )

    assert runtime.command_registry.get("mcp__ros__vpc") is not None
    manager._resources = []

    await manager.listeners[0]("ros", "resources")

    assert runtime.command_registry.get("mcp__ros__vpc") is None


def test_create_runtime_registers_auth_tool_for_needs_auth_server(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    manager = FakeMCPManager(states={"remote": MCPConnectionState.NEEDS_AUTH})

    runtime = create_agent_runtime(
        AgentFactoryOptions(
            model="qwen3.7-max",
            session_id="session-1",
            cwd=str(tmp_path),
            mcp_configs=[
                {
                    "name": "remote",
                    "type": "http",
                    "url": "https://example.com/mcp",
                    "oauth": {"clientId": "client-id"},
                }
            ],
            mcp_manager_factory=lambda configs, roots: manager,
        )
    )

    assert runtime.tool_registry.get("mcp__remote__authenticate") is not None


@pytest.mark.asyncio
async def test_runtime_auth_tool_runs_oauth_flow_and_reconnects(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    calls: dict[str, Any] = {}
    manager = FakeMCPManager(states={"remote": MCPConnectionState.NEEDS_AUTH})

    class FakePendingFlow:
        authorization_url = "https://auth.example/authorize"
        browser_opened = False

        def wait(self):
            calls["waited"] = True

    def fake_flow(config, *, storage, scope, **kwargs):
        calls["server"] = config.name
        calls["scope"] = scope
        return FakePendingFlow()

    monkeypatch.setattr("iac_code.mcp.oauth.start_oauth_loopback_flow", fake_flow)

    runtime = create_agent_runtime(
        AgentFactoryOptions(
            model="qwen3.7-max",
            session_id="session-1",
            cwd=str(tmp_path),
            mcp_configs=[
                {
                    "name": "remote",
                    "type": "http",
                    "url": "https://example.com/mcp",
                    "oauth": {"clientId": "client-id"},
                }
            ],
            mcp_manager_factory=lambda configs, roots: manager,
        )
    )

    tool = runtime.tool_registry.get("mcp__remote__authenticate")
    assert tool is not None
    result = await tool.execute(tool_input={}, context=ToolContext())

    assert result.is_error is False
    assert "https://auth.example/authorize" in result.content
    assert calls["server"] == "remote"
    assert calls["scope"] == "session:session-1"

    for _ in range(20):
        if manager.reconnected:
            break
        await asyncio.sleep(0.01)

    assert calls["waited"] is True
    assert manager.reconnected == ["remote"]
    assert manager.connection_state("remote") is MCPConnectionState.CONNECTED
    assert runtime.tool_registry.get("mcp__remote__authenticate") is None
    assert runtime.tool_registry.get("mcp__remote__search") is not None


def test_create_runtime_skips_unapproved_project_mcp_configs(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    (tmp_path / ".mcp.json").write_text('{"mcpServers": {"pending": {"command": "uvx"}}}', encoding="utf-8")
    called = False

    def factory(configs, roots):
        nonlocal called
        called = True
        return FakeMCPManager()

    runtime = create_agent_runtime(
        AgentFactoryOptions(
            model="qwen3.7-max",
            session_id="session-1",
            cwd=str(tmp_path),
            mcp_manager_factory=factory,
        )
    )

    assert called is False
    assert runtime.mcp_manager is None
    assert runtime.tool_registry.get("mcp__pending__anything") is None
    assert any(warning.code == "pending_approval" for warning in runtime.mcp_config_warnings)


def test_create_runtime_exposes_mcp_connection_failures_as_warnings(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    manager = FakeMCPManager(
        states={"broken": MCPConnectionState.FAILED},
        errors={"broken": "Authorization: Bearer secret"},
    )

    runtime = create_agent_runtime(
        AgentFactoryOptions(
            model="qwen3.7-max",
            session_id="session-1",
            cwd=str(tmp_path),
            mcp_configs=[{"name": "broken", "command": "uvx"}],
            mcp_manager_factory=lambda configs, roots: manager,
        )
    )

    warnings = [warning for warning in runtime.mcp_config_warnings if warning.code == "connection_failed"]
    assert len(warnings) == 1
    assert warnings[0].server_name == "broken"
    assert "secret" not in warnings[0].message
    assert "[REDACTED]" in warnings[0].message


def test_create_runtime_disconnects_mcp_manager_when_setup_fails_after_connect(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    manager = FakeMCPManager(tools=[MCPToolRecord(server_name="ros", tool_name="plan", public_name="mcp__ros__plan")])

    def fail_load_permission_context(*args, **kwargs):
        raise RuntimeError("permission setup failed")

    monkeypatch.setattr("iac_code.services.permissions.loader.load_permission_context", fail_load_permission_context)

    with pytest.raises(RuntimeError, match="permission setup failed"):
        create_agent_runtime(
            AgentFactoryOptions(
                model="qwen3.7-max",
                session_id="session-1",
                cwd=str(tmp_path),
                mcp_configs=[{"name": "ros", "command": "uvx"}],
                mcp_manager_factory=lambda configs, roots: manager,
            )
        )

    assert manager.connected is True
    assert manager.disconnected is True


@pytest.mark.asyncio
async def test_runtime_mcp_list_changed_appends_discovery_warnings(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    manager = FakeMCPManager(prompts=[MCPPromptRecord(server_name="ros", prompt_name="review", public_name="review")])

    runtime = create_agent_runtime(
        AgentFactoryOptions(
            model="qwen3.7-max",
            session_id="session-1",
            cwd=str(tmp_path),
            mcp_configs=[{"name": "ros", "command": "uvx"}],
            mcp_manager_factory=lambda configs, roots: manager,
        )
    )

    manager.capability_errors = {"ros": {"prompts": "Authorization: Bearer secret"}}

    await manager.listeners[0]("ros", "prompts")

    warnings = [warning for warning in runtime.mcp_config_warnings if warning.code == "prompts_failed"]
    assert len(warnings) == 1
    assert warnings[0].server_name == "ros"
    assert "secret" not in warnings[0].message
    assert "[REDACTED]" in warnings[0].message


class FakeMCPManager:
    def __init__(
        self,
        *,
        tools: list[MCPToolRecord] | None = None,
        resources: list[MCPResourceRecord] | None = None,
        prompts: list[MCPPromptRecord] | None = None,
        states: dict[str, MCPConnectionState] | None = None,
        errors: dict[str, str] | None = None,
        capability_errors: dict[str, dict[str, str]] | None = None,
    ) -> None:
        self._tools = tools or []
        self._resources = resources or []
        self._prompts = prompts or []
        self._states = states or {}
        self._errors = errors or {}
        self.capability_errors = capability_errors or {}
        self.connected = False
        self.disconnected = False
        self.reconnected: list[str] = []
        self.listeners: list[Any] = []

    async def connect_all(self) -> None:
        self.connected = True

    async def disconnect_all(self) -> None:
        self.disconnected = True

    def list_tools(self) -> list[MCPToolRecord]:
        return self._tools

    def list_resources(self) -> list[MCPResourceRecord]:
        return self._resources

    def list_prompts(self) -> list[Any]:
        return self._prompts

    def connection_state(self, server_name: str) -> MCPConnectionState:
        return self._states.get(server_name, MCPConnectionState.CONNECTED)

    def list_connections(self) -> list[Any]:
        records = [
            type(
                "Connection",
                (),
                {
                    "name": name,
                    "state": state,
                    "error": self._errors.get(name),
                    "capability_errors": self.capability_errors.get(name, {}),
                },
            )()
            for name, state in self._states.items()
        ]
        for name, errors in self.capability_errors.items():
            if name not in self._states:
                records.append(
                    type(
                        "Connection",
                        (),
                        {
                            "name": name,
                            "state": MCPConnectionState.CONNECTED,
                            "error": None,
                            "capability_errors": errors,
                        },
                    )()
                )
        return records

    def needs_auth_servers(self) -> list[str]:
        return [name for name, state in self._states.items() if state is MCPConnectionState.NEEDS_AUTH]

    async def reconnect(self, server_name: str) -> None:
        self.reconnected.append(server_name)
        self._states[server_name] = MCPConnectionState.CONNECTED
        self._tools = [
            MCPToolRecord(
                server_name=server_name,
                tool_name="search",
                public_name="mcp__remote__search",
                input_schema={"type": "object"},
            )
        ]
        for listener in self.listeners:
            await listener(server_name, "tools")

    def add_change_listener(self, listener) -> None:
        self.listeners.append(listener)

    async def read_resource(self, uri: str, server_name: str | None = None):
        return (
            server_name or "ros",
            {
                "contents": [
                    {
                        "uri": uri,
                        "mimeType": "text/markdown",
                        "text": "---\ndescription: VPC guidance\n---\n# VPC",
                    }
                ]
            },
        )
