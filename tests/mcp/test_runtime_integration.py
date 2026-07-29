from __future__ import annotations

import asyncio
import base64
import sys
import threading
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from iac_code.commands.registry import CommandRegistry, LocalCommand, PromptCommand
from iac_code.mcp.errors import MCPConnectionError
from iac_code.mcp.manager import MCPManager
from iac_code.mcp.oauth import OAuthMetadata, OAuthPendingFlow
from iac_code.mcp.storage import MCPSecretStorage
from iac_code.mcp.types import (
    MCPConnectionMetadata,
    MCPConnectionState,
    MCPPromptRecord,
    MCPResourceRecord,
    MCPServerConfig,
    MCPToolRecord,
)
from iac_code.services.agent_factory import (
    AgentFactoryOptions,
    AgentRuntime,
    _mcp_auth_flow_factory,
    _sync_mcp_command_registry,
    create_agent_runtime,
)
from iac_code.services.permissions.pipeline import check_tool_permission
from iac_code.skills.frontmatter import SkillFrontmatter
from iac_code.skills.skill_definition import SkillDefinition
from iac_code.tools.base import ToolContext
from iac_code.types.stream_events import MessageEndEvent, Usage


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


def test_create_runtime_skips_malformed_mcp_skill_without_blocking_valid_tools_or_skills(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    manager = MixedSkillFakeMCPManager(
        tools=[
            MCPToolRecord(
                server_name="ros",
                tool_name="plan",
                public_name="mcp__ros__plan",
                input_schema={"type": "object"},
            )
        ],
        resources=[
            MCPResourceRecord(server_name="ros", uri="skill://ros/bad", name="bad"),
            MCPResourceRecord(server_name="ros", uri="skill://ros/good", name="good"),
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

    assert runtime.tool_registry.get("mcp__ros__plan") is not None
    assert runtime.command_registry.get("mcp__ros__bad") is None
    good = runtime.command_registry.get("mcp__ros__good")
    assert good is not None
    assert getattr(good, "skill", None) is not None
    warnings = runtime.mcp_config_warnings or []
    assert [(warning.server_name, warning.code) for warning in warnings] == [("ros", "skill_read_failed")]
    assert "mcp__ros__bad" in warnings[0].message


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "capability,state",
    [("auth", MCPConnectionState.NEEDS_AUTH), ("connection", MCPConnectionState.FAILED)],
)
async def test_runtime_mcp_auth_or_connection_change_removes_stale_tools_and_commands(
    monkeypatch,
    tmp_path: Path,
    capability: str,
    state: MCPConnectionState,
) -> None:
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
        prompts=[MCPPromptRecord(server_name="ros", prompt_name="review", public_name="mcp__ros__review")],
    )
    runtime = create_agent_runtime(
        AgentFactoryOptions(
            model="qwen3.7-max",
            session_id="session-1",
            cwd=str(tmp_path),
            mcp_configs=[{"name": "ros", "type": "http", "url": "https://example.com/mcp"}],
            mcp_manager_factory=lambda configs, roots: manager,
        )
    )
    assert runtime.tool_registry.get("mcp__ros__plan") is not None
    assert runtime.command_registry.get("mcp__ros__review") is not None
    assert runtime.command_registry.get("mcp__ros__vpc") is not None

    manager._tools = []
    manager._resources = []
    manager._prompts = []
    manager._states["ros"] = state
    manager._errors["ros"] = "authentication required" if state is MCPConnectionState.NEEDS_AUTH else "session expired"
    for listener in manager.listeners:
        await listener("ros", capability)

    assert runtime.tool_registry.get("mcp__ros__plan") is None
    assert runtime.command_registry.get("mcp__ros__review") is None
    assert runtime.command_registry.get("mcp__ros__vpc") is None
    if state is MCPConnectionState.NEEDS_AUTH:
        assert runtime.tool_registry.get("mcp__ros__authenticate") is not None


@pytest.mark.asyncio
async def test_runtime_mcp_command_conflict_does_not_unregister_builtin_on_next_sync() -> None:
    registry = CommandRegistry()
    builtin_help = LocalCommand(name="help", description="Help")
    registry.register(builtin_help)
    manager = FakeMCPManager(
        prompts=[
            MCPPromptRecord(
                server_name="remote",
                prompt_name="help",
                public_name="help",
                description="Remote help",
            )
        ]
    )

    registered, warnings = await _sync_mcp_command_registry(registry, manager, set())
    registered, second_warnings = await _sync_mcp_command_registry(registry, manager, registered)

    assert registry.get("help") is builtin_help
    assert registered == set()
    assert [warning.code for warning in warnings] == ["command_conflict"]
    assert [warning.code for warning in second_warnings] == ["command_conflict"]


@pytest.mark.asyncio
async def test_runtime_mcp_command_sync_removes_stale_untracked_mcp_commands() -> None:
    registry = CommandRegistry()
    builtin_help = LocalCommand(name="help", description="Help")
    registry.register(builtin_help)
    local_skill = _prompt_command("local_review", file_path="/tmp/local-review/SKILL.md")
    registry.register(local_skill)
    registry.register(_prompt_command("mcp__ros__review", file_path="mcp://ros/prompt/review"))
    registry.register(_prompt_command("mcp__ros__vpc", file_path="mcp://ros/skill://ros/vpc"))
    manager = FakeMCPManager(prompts=[], resources=[])

    registered, warnings = await _sync_mcp_command_registry(registry, manager, set())

    assert warnings == []
    assert registered == set()
    assert registry.get("mcp__ros__review") is None
    assert registry.get("mcp__ros__vpc") is None
    assert registry.get("help") is builtin_help
    assert registry.get("local_review") is local_skill


@pytest.mark.asyncio
async def test_runtime_starts_failed_mcp_reconnect_on_agent_loop(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    clients = [
        RuntimeFakeMCPClient(connect_error=MCPConnectionError("connect failed")),
        RuntimeFakeMCPClient(tools=[{"name": "search", "inputSchema": {"type": "object"}}]),
    ]

    def factory(configs, roots):
        return MCPManager(
            configs,
            roots=roots,
            client_factory=lambda config: clients.pop(0),
            max_reconnect_attempts=1,
        )

    runtime = create_agent_runtime(
        AgentFactoryOptions(
            model="qwen3.7-max",
            session_id="session-1",
            cwd=str(tmp_path),
            mcp_configs=[{"name": "remote", "type": "http", "url": "https://example.com/mcp"}],
            mcp_manager_factory=factory,
        )
    )
    assert runtime.mcp_manager is not None
    assert runtime.mcp_manager.connection_state("remote") is MCPConnectionState.FAILED
    assert runtime.tool_registry.get("mcp__remote__search") is None
    runtime.mcp_manager.connection("remote").reconnect_next_attempt_at = 0.0

    async def fake_stream(*args, **kwargs):
        yield MessageEndEvent(stop_reason="stop", usage=Usage())

    runtime.agent_loop._provider_manager.stream = fake_stream

    async for _event in runtime.agent_loop.run_streaming("hello"):
        pass
    for _ in range(20):
        if runtime.tool_registry.get("mcp__remote__search") is not None:
            break
        await asyncio.sleep(0.01)

    assert runtime.mcp_manager.connection_state("remote") is MCPConnectionState.CONNECTED
    assert runtime.tool_registry.get("mcp__remote__search") is not None
    await runtime.aclose()


@pytest.mark.asyncio
async def test_runtime_mcp_permission_metadata_includes_names_and_destructive_read_only_annotations(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    manager = FakeMCPManager(
        tools=[
            MCPToolRecord(
                server_name="yuque.internal",
                tool_name="search-docs",
                public_name="mcp__yuque_internal__search_docs",
                original_server_name="yuque",
                original_tool_name="search",
                input_schema={"type": "object"},
                annotations={"readOnlyHint": False, "destructiveHint": True},
            )
        ]
    )

    runtime = create_agent_runtime(
        AgentFactoryOptions(
            model="qwen3.7-max",
            session_id="session-1",
            cwd=str(tmp_path),
            mcp_configs=[{"name": "yuque", "command": "uvx"}],
            mcp_manager_factory=lambda configs, roots: manager,
        )
    )

    tool = runtime.tool_registry.get("mcp__yuque_internal__search_docs")
    permission = await check_tool_permission(tool, {"query": "ros"}, runtime.agent_loop._permission_context)

    assert tool.user_facing_name({"query": "ros"}) == "MCP yuque:search"
    assert permission.behavior == "ask"
    assert permission.message == "Allow MCP yuque:search?"
    assert permission.audit is not None
    assert permission.audit.is_read_only is False
    assert permission.audit.operation == {
        "publicName": "mcp__yuque_internal__search_docs",
        "originalServerName": "yuque",
        "originalToolName": "search",
        "isReadOnly": False,
        "isDestructive": True,
    }


def test_create_runtime_includes_mcp_server_instructions_in_agent_prompt(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    manager = FakeMCPManager(
        metadata={
            "ros": MCPConnectionMetadata(
                state=MCPConnectionState.CONNECTED,
                server_name="ros",
                server_info={"name": "aliyun-ros-mcp", "version": "1.2.3"},
                instructions="Prefer MCP-provided ROS templates when the user asks for Alibaba Cloud IaC.",
            )
        }
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

    prompt = runtime.agent_loop.system_prompt
    assert "# MCP Server Instructions" in prompt
    assert "## ros (aliyun-ros-mcp 1.2.3)" in prompt
    assert "Prefer MCP-provided ROS templates" in prompt
    skills_index = prompt.find("# Available Skills")
    if skills_index != -1:
        assert prompt.index("# MCP Server Instructions") < skills_index


def test_create_runtime_places_project_instructions_before_mcp_server_instructions(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    (tmp_path / "AGENTS.md").write_text("Project runtime instruction\n", encoding="utf-8")
    manager = FakeMCPManager(
        metadata={
            "ros": MCPConnectionMetadata(
                state=MCPConnectionState.CONNECTED,
                server_name="ros",
                instructions="MCP runtime instruction.",
            )
        }
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

    prompt = runtime.agent_loop.system_prompt
    assert prompt.index("Project runtime instruction") < prompt.index("# MCP Server Instructions")


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
async def test_runtime_mcp_tools_changed_uses_session_dir_for_binary_output(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    manager = FakeMCPManager()

    runtime = create_agent_runtime(
        AgentFactoryOptions(
            model="qwen3.7-max",
            session_id="session-1",
            cwd=str(tmp_path),
            mcp_configs=[{"name": "ros", "command": "uvx"}],
            mcp_manager_factory=lambda configs, roots: manager,
        )
    )

    manager._tools = [
        MCPToolRecord(
            server_name="ros",
            tool_name="render",
            public_name="mcp__ros__render",
            input_schema={"type": "object"},
        )
    ]

    await manager.listeners[0]("ros", "tools")

    tool = runtime.tool_registry.get("mcp__ros__render")
    result = await tool.execute(tool_input={}, context=ToolContext())

    session_dir = runtime.agent_loop._session_storage.session_dir(str(tmp_path), "session-1")
    artifact_path = result.metadata["mcp"]["artifacts"][0]["path"]
    assert artifact_path.startswith(str(session_dir / "tool-results" / "mcp" / "ros" / "render"))
    assert not (tmp_path / "config" / "tool-results" / "session-1").exists()


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


@pytest.mark.asyncio
async def test_runtime_auth_tool_tracks_real_pending_oauth_flow_until_runtime_close(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("IAC_CODE_MCP_DISABLE_KEYRING", "1")
    closed = threading.Event()
    manager = FakeMCPManager(states={"remote": MCPConnectionState.NEEDS_AUTH})

    class BlockingCallback:
        def wait_for_code(self, timeout_seconds: float) -> str:
            _ = timeout_seconds
            closed.wait(timeout=5)
            raise RuntimeError("flow closed")

        def close(self) -> None:
            closed.set()

    def real_pending_flow(
        config: MCPServerConfig,
        *,
        storage: MCPSecretStorage,
        scope: str | None = None,
        **kwargs: object,
    ) -> OAuthPendingFlow:
        _ = kwargs
        return OAuthPendingFlow(
            config=config,
            storage=storage,
            metadata=OAuthMetadata(
                issuer="https://auth.example",
                authorization_endpoint="https://auth.example/authorize",
                token_endpoint="https://auth.example/token",
            ),
            callback=BlockingCallback(),
            redirect_uri="http://127.0.0.1/callback",
            authorization_url="https://auth.example/authorize",
            verifier="verifier",
            scope=scope,
            timeout_seconds=0.1,
        )

    monkeypatch.setattr("iac_code.mcp.oauth.start_oauth_loopback_flow", real_pending_flow)
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

    await runtime.aclose()

    assert closed.is_set()
    assert not runtime._mcp_auth_tasks
    assert not runtime._mcp_auth_flows
    assert manager.disconnected is True


@pytest.mark.asyncio
async def test_runtime_auth_tool_requests_required_scope_without_explicit_oauth(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    calls: dict[str, Any] = {}
    manager = FakeMCPManager(
        states={"remote": MCPConnectionState.NEEDS_AUTH},
        required_scopes={"remote": ["write:stack"]},
        required_resource_metadata_urls={"remote": "https://resource.example/.well-known/oauth-protected-resource/mcp"},
    )

    class FakePendingFlow:
        authorization_url = "https://auth.example/authorize?scope=write%3Astack"
        browser_opened = False

        def wait(self):
            calls["waited"] = True

    def fake_flow(config, *, storage, scope, required_scopes=None, resource_metadata_url=None, **kwargs):
        calls["server"] = config.name
        calls["has_oauth_config"] = config.oauth is not None
        calls["scope"] = scope
        calls["required_scopes"] = required_scopes
        calls["resource_metadata_url"] = resource_metadata_url
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
                }
            ],
            mcp_manager_factory=lambda configs, roots: manager,
        )
    )

    tool = runtime.tool_registry.get("mcp__remote__authenticate")
    assert tool is not None
    result = await tool.execute(tool_input={}, context=ToolContext())

    assert result.is_error is False
    assert "https://auth.example/authorize?scope=write%3Astack" in result.content
    assert calls["server"] == "remote"
    assert calls["has_oauth_config"] is False
    assert calls["scope"] == "session:session-1"
    assert calls["required_scopes"] == ["write:stack"]
    assert calls["resource_metadata_url"] == "https://resource.example/.well-known/oauth-protected-resource/mcp"


@pytest.mark.asyncio
async def test_runtime_auth_tool_rejects_unsafe_resource_metadata_url(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    calls: dict[str, Any] = {}
    manager = FakeMCPManager(
        states={"remote": MCPConnectionState.NEEDS_AUTH},
        required_scopes={"remote": ["write:stack"]},
        required_resource_metadata_urls={"remote": "http://127.0.0.1:8000/.well-known/oauth-protected-resource/mcp"},
    )

    class FakePendingFlow:
        authorization_url = "https://auth.example/authorize?scope=write%3Astack"
        browser_opened = False

        def wait(self):
            calls["waited"] = True

    def fake_flow(config, *, storage, scope, required_scopes=None, resource_metadata_url=None, **kwargs):
        calls["resource_metadata_url"] = resource_metadata_url
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
                }
            ],
            mcp_manager_factory=lambda configs, roots: manager,
        )
    )

    tool = runtime.tool_registry.get("mcp__remote__authenticate")
    assert tool is not None
    result = await tool.execute(tool_input={}, context=ToolContext())

    assert result.is_error is False
    assert calls["resource_metadata_url"] is None


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
    assert any(warning.code == "pending_approval" for warning in (runtime.mcp_config_warnings or []))
    assert [config.name for config in (runtime.mcp_pending_configs or [])] == ["pending"]


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

    warnings = [warning for warning in (runtime.mcp_config_warnings or []) if warning.code == "connection_failed"]
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
async def test_create_runtime_registers_mcp_elicitation_handler(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    manager = FakeMCPManager()
    requests: list[tuple[str, dict[str, Any]]] = []

    async def handler(server_name: str, params: Mapping[str, Any]) -> Mapping[str, Any]:
        requests.append((server_name, dict(params)))
        return {"action": "accept"}

    create_agent_runtime(
        AgentFactoryOptions(
            model="qwen3.7-max",
            session_id="session-1",
            cwd=str(tmp_path),
            mcp_configs=[{"name": "ros", "command": "uvx"}],
            mcp_manager_factory=lambda configs, roots: manager,
            mcp_elicitation_handler=handler,
        )
    )

    result = await manager.request_elicitation("ros", {"mode": "url", "message": "Authorize"})

    assert result == {"action": "accept"}
    assert requests == [("ros", {"mode": "url", "message": "Authorize"})]


@pytest.mark.asyncio
async def test_headless_mcp_elicitation_handler_cancels() -> None:
    from iac_code.cli.headless import _headless_mcp_elicitation_handler

    assert await _headless_mcp_elicitation_handler("ros", {"mode": "url"}) == {"action": "cancel"}


@pytest.mark.asyncio
async def test_repl_mcp_elicitation_cancels_without_printing_when_non_interactive(monkeypatch) -> None:
    from iac_code.ui.repl import InlineREPL

    class FailingConsole:
        def print(self, *args, **kwargs):
            raise AssertionError("non-interactive elicitation should not print")

    repl = InlineREPL.__new__(InlineREPL)
    repl.console = FailingConsole()
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: False)

    result = await repl._request_mcp_elicitation(
        "ros",
        {
            "mode": "url",
            "message": "Authorization: Bearer sk-live-secret",
            "url": "https://auth.example/authorize?AccessKeySecret=secret",
        },
    )

    assert result == {"action": "cancel"}


@pytest.mark.asyncio
async def test_repl_mcp_elicitation_preserves_and_bounds_interactive_text(monkeypatch) -> None:
    from iac_code.ui.repl import InlineREPL

    class CapturingConsole:
        def __init__(self) -> None:
            self.printed: list[str] = []
            self.prompts: list[str] = []

        def print(self, value, *args, **kwargs):
            self.printed.append(getattr(value, "plain", str(value)))

        def input(self, prompt: str) -> str:
            self.prompts.append(prompt)
            return ""

    console = CapturingConsole()
    repl = InlineREPL.__new__(InlineREPL)
    repl.console = console
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)

    result = await repl._request_mcp_elicitation(
        "ros",
        {
            "mode": "url",
            "message": "Open this URL with token=super-secret-token " + ("x" * 3000),
            "url": "https://auth.example/authorize?AccessKeySecret=super-secret-token&state=ok" + ("y" * 3000),
        },
    )

    printed = "\n".join(console.printed)
    assert result == {"action": "accept"}
    assert "super-secret-token" in printed
    assert "[REDACTED]" not in printed
    assert all(len(line) <= 1100 for line in console.printed)
    assert console.prompts


@pytest.mark.asyncio
async def test_repl_mcp_elicitation_suspends_streaming_input_and_uses_prompt_input(monkeypatch) -> None:
    from iac_code.ui.repl import InlineREPL

    class FakeRenderer:
        def __init__(self) -> None:
            self.suspended = False

        async def run_with_streaming_input_suspended(self, callback):
            self.suspended = True
            return await callback()

    class ConsoleWithoutInput:
        def __init__(self, renderer: FakeRenderer) -> None:
            self.renderer = renderer
            self.printed: list[str] = []

        def print(self, value, *args, **kwargs):
            assert self.renderer.suspended, "MCP elicitation text must render while streaming input is suspended"
            self.printed.append(getattr(value, "plain", str(value)))

        def input(self, prompt: str) -> str:
            raise AssertionError("MCP elicitation must not race streaming input via console.input")

    class FakePromptInput:
        def __init__(self) -> None:
            self.calls: list[tuple[str, bool]] = []

        async def get_input(self, prompt: str, *, transient: bool = False) -> str:
            self.calls.append((prompt, transient))
            return "accept"

    renderer = FakeRenderer()
    repl = InlineREPL.__new__(InlineREPL)
    repl.console = ConsoleWithoutInput(renderer)
    repl._prompt_input = FakePromptInput()
    repl.renderer = renderer
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)

    result = await repl._request_mcp_elicitation("ros", {"mode": "confirm", "message": "Approve?"})

    assert result == {"action": "accept"}
    assert repl.renderer.suspended is True
    assert repl._prompt_input.calls == [
        ("Type 'accept' to accept, 'decline' to decline, or 'cancel' to cancel: ", True)
    ]


@pytest.mark.asyncio
async def test_repl_mcp_elicitation_suspends_streaming_input_once_for_entire_form(monkeypatch) -> None:
    from iac_code.ui.repl import InlineREPL

    class FakeRenderer:
        def __init__(self) -> None:
            self.suspended = False
            self.calls = 0

        async def run_with_streaming_input_suspended(self, callback):
            self.calls += 1
            assert not self.suspended
            self.suspended = True
            try:
                return await callback()
            finally:
                self.suspended = False

    class ConsoleWithoutInput:
        def __init__(self, renderer: FakeRenderer) -> None:
            self.renderer = renderer
            self.printed: list[str] = []

        def print(self, value, *args, **kwargs):
            assert self.renderer.suspended, "MCP form text must render while streaming input is suspended"
            self.printed.append(getattr(value, "plain", str(value)))

        def input(self, prompt: str) -> str:
            raise AssertionError("MCP form elicitation must use prompt input when available")

    class FakePromptInput:
        def __init__(self, renderer: FakeRenderer) -> None:
            self.renderer = renderer
            self.answers = iter(["cn-hangzhou", "yes"])

        async def get_input(self, prompt: str, *, transient: bool = False) -> str:
            assert self.renderer.suspended, "MCP form fields must be read while streaming input is suspended"
            assert transient is True
            return next(self.answers)

    renderer = FakeRenderer()
    repl = InlineREPL.__new__(InlineREPL)
    repl.console = ConsoleWithoutInput(renderer)
    repl._prompt_input = FakePromptInput(renderer)
    repl.renderer = renderer
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)

    result = await repl._request_mcp_elicitation(
        "ros",
        {
            "message": "Choose deployment settings",
            "requestedSchema": {
                "type": "object",
                "required": ["region"],
                "properties": {
                    "region": {"type": "string"},
                    "dryRun": {"type": "boolean"},
                },
            },
        },
    )

    assert result == {"action": "accept", "content": {"region": "cn-hangzhou", "dryRun": True}}
    assert renderer.calls == 1


@pytest.mark.asyncio
async def test_repl_mcp_elicitation_collects_schema_aware_form_content(monkeypatch) -> None:
    from iac_code.ui.repl import InlineREPL

    class CapturingConsole:
        def __init__(self) -> None:
            self.printed: list[str] = []
            self.prompts: list[str] = []
            self.answers = iter(["cn-hangzhou", "yes", "release note"])

        def print(self, value, *args, **kwargs):
            self.printed.append(getattr(value, "plain", str(value)))

        def input(self, prompt: str) -> str:
            self.prompts.append(prompt)
            return next(self.answers)

    console = CapturingConsole()
    repl = InlineREPL.__new__(InlineREPL)
    repl.console = console
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)

    result = await repl._request_mcp_elicitation(
        "ros",
        {
            "message": "Choose deployment settings",
            "requestedSchema": {
                "type": "object",
                "required": ["region"],
                "properties": {
                    "region": {"type": "string", "enum": ["cn-hangzhou", "cn-beijing"]},
                    "dryRun": {"type": "boolean"},
                    "note": {"type": "string"},
                },
            },
        },
    )

    assert result == {
        "action": "accept",
        "content": {"region": "cn-hangzhou", "dryRun": True, "note": "release note"},
    }
    assert any("region" in prompt for prompt in console.prompts)
    assert any("dryRun" in prompt for prompt in console.prompts)


@pytest.mark.asyncio
async def test_repl_mcp_elicitation_reprompts_invalid_enum_value(monkeypatch) -> None:
    from iac_code.ui.repl import InlineREPL

    class CapturingConsole:
        def __init__(self) -> None:
            self.printed: list[str] = []
            self.prompts: list[str] = []
            self.answers = iter(["cn-shanghai", "cn-beijing"])

        def print(self, value, *args, **kwargs):
            self.printed.append(getattr(value, "plain", str(value)))

        def input(self, prompt: str) -> str:
            self.prompts.append(prompt)
            return next(self.answers)

    console = CapturingConsole()
    repl = InlineREPL.__new__(InlineREPL)
    repl.console = console
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)

    result = await repl._request_mcp_elicitation(
        "ros",
        {
            "message": "Choose deployment settings",
            "requestedSchema": {
                "type": "object",
                "required": ["region"],
                "properties": {
                    "region": {"type": "string", "enum": ["cn-hangzhou", "cn-beijing"]},
                },
            },
        },
    )

    assert result == {"action": "accept", "content": {"region": "cn-beijing"}}
    assert len([prompt for prompt in console.prompts if "region" in prompt]) == 2
    assert any("Invalid value" in line for line in console.printed)


@pytest.mark.asyncio
async def test_repl_mcp_elicitation_boolean_no_is_field_value_not_decline(monkeypatch) -> None:
    from iac_code.ui.repl import InlineREPL

    class CapturingConsole:
        def __init__(self) -> None:
            self.printed: list[str] = []
            self.prompts: list[str] = []
            self.answers = iter(["no"])

        def print(self, value, *args, **kwargs):
            self.printed.append(getattr(value, "plain", str(value)))

        def input(self, prompt: str) -> str:
            self.prompts.append(prompt)
            return next(self.answers)

    console = CapturingConsole()
    repl = InlineREPL.__new__(InlineREPL)
    repl.console = console
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)

    result = await repl._request_mcp_elicitation(
        "ros",
        {
            "message": "Choose deployment settings",
            "requestedSchema": {
                "type": "object",
                "required": ["dryRun"],
                "properties": {
                    "dryRun": {"type": "boolean"},
                },
            },
        },
    )

    assert result == {"action": "accept", "content": {"dryRun": False}}


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

    warnings = [warning for warning in (runtime.mcp_config_warnings or []) if warning.code == "prompts_failed"]
    assert len(warnings) == 1
    assert warnings[0].server_name == "ros"
    assert "secret" not in warnings[0].message
    assert "[REDACTED]" in warnings[0].message


@pytest.mark.asyncio
async def test_runtime_mcp_status_metadata_includes_list_changed_refresh_diagnostics(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    manager = FakeMCPManager(
        tools=[MCPToolRecord(server_name="ros", tool_name="plan", public_name="mcp__ros__plan")],
        states={"ros": MCPConnectionState.CONNECTED},
        refresh_metadata={
            "ros": {
                "kind": "tools",
                "refreshed_at": 123.456,
                "failure_reason": "tools failed with access_token=super-secret-token",
            }
        },
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

    assert runtime.mcp_manager is not None
    metadata = runtime.mcp_manager.status_metadata()

    assert metadata is not None
    assert metadata["servers"][0]["latestRefreshKind"] == "tools"
    assert metadata["servers"][0]["latestRefreshAt"] == 123.456
    assert metadata["servers"][0]["latestRefreshFailureReason"] == "tools failed with access_token=[REDACTED]"
    assert "super-secret-token" not in str(metadata)


def _prompt_command(name: str, *, file_path: str) -> PromptCommand:
    skill = SkillDefinition(
        name=name,
        description=name,
        frontmatter=SkillFrontmatter(description=name, when_to_use=name),
        content="",
        file_path=file_path,
    )
    return PromptCommand(name=name, description=name, skill=skill)


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
        required_scopes: dict[str, list[str]] | None = None,
        required_resource_metadata_urls: dict[str, str] | None = None,
        metadata: dict[str, MCPConnectionMetadata] | None = None,
        refresh_metadata: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        self._tools = tools or []
        self._resources = resources or []
        self._prompts = prompts or []
        self._states = states or {}
        self._errors = errors or {}
        self.capability_errors = capability_errors or {}
        self._required_scopes = required_scopes or {}
        self._required_resource_metadata_urls = required_resource_metadata_urls or {}
        self._metadata = metadata or {}
        self._refresh_metadata = refresh_metadata or {}
        self.connected = False
        self.disconnected = False
        self.reconnected: list[str] = []
        self.listeners: list[Any] = []
        self._elicitation_handler: Any = None

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
        records: list[Any] = [
            type(
                "Connection",
                (),
                {
                    "name": name,
                    "state": state,
                    "error": self._errors.get(name),
                    "capability_errors": self.capability_errors.get(name, {}),
                    "metadata": self._metadata.get(name),
                    "latest_refresh_kind": self._refresh_metadata.get(name, {}).get("kind"),
                    "latest_refresh_at": self._refresh_metadata.get(name, {}).get("refreshed_at"),
                    "latest_refresh_failure_reason": self._refresh_metadata.get(name, {}).get("failure_reason"),
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
                            "metadata": self._metadata.get(name),
                            "latest_refresh_kind": self._refresh_metadata.get(name, {}).get("kind"),
                            "latest_refresh_at": self._refresh_metadata.get(name, {}).get("refreshed_at"),
                            "latest_refresh_failure_reason": self._refresh_metadata.get(name, {}).get("failure_reason"),
                        },
                    )()
                )
        for name, metadata in self._metadata.items():
            if name not in self._states and name not in self.capability_errors:
                records.append(
                    type(
                        "Connection",
                        (),
                        {
                            "name": name,
                            "state": metadata.state,
                            "error": None,
                            "capability_errors": {},
                            "metadata": metadata,
                            "latest_refresh_kind": self._refresh_metadata.get(name, {}).get("kind"),
                            "latest_refresh_at": self._refresh_metadata.get(name, {}).get("refreshed_at"),
                            "latest_refresh_failure_reason": self._refresh_metadata.get(name, {}).get("failure_reason"),
                        },
                    )()
                )
        return records

    def status_metadata(self) -> dict[str, Any] | None:
        from iac_code.mcp.manager import mcp_status_metadata

        return mcp_status_metadata(self)

    def needs_auth_servers(self) -> list[str]:
        return [name for name, state in self._states.items() if state is MCPConnectionState.NEEDS_AUTH]

    def required_auth_scopes(self, server_name: str) -> list[str]:
        return self._required_scopes.get(server_name, [])

    def required_auth_resource_metadata_url(self, server_name: str) -> str | None:
        return self._required_resource_metadata_urls.get(server_name)

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

    def set_elicitation_handler(self, handler) -> None:
        self._elicitation_handler = handler

    async def request_elicitation(self, server_name: str, params: Mapping[str, Any]) -> Mapping[str, Any]:
        if self._elicitation_handler is None:
            return {"action": "cancel"}
        result = self._elicitation_handler(server_name, params)
        if asyncio.iscoroutine(result):
            result = await result
        return result if isinstance(result, Mapping) else {"action": "cancel"}

    async def call_tool(self, *args, **kwargs):
        return {
            "content": [
                {
                    "type": "image",
                    "data": base64.b64encode(b"runtime-png").decode("ascii"),
                    "mimeType": "image/png",
                }
            ]
        }

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


class MixedSkillFakeMCPManager(FakeMCPManager):
    async def read_resource(self, uri: str, server_name: str | None = None):
        if uri.endswith("/bad"):
            text = "---\ndescription: Bad guidance\narguments: 1\n---\n# Bad"
        else:
            text = "---\ndescription: Good guidance\n---\n# Good"
        return (
            server_name or "ros",
            {
                "contents": [
                    {
                        "uri": uri,
                        "mimeType": "text/markdown",
                        "text": text,
                    }
                ]
            },
        )


class RuntimeFakeMCPClient:
    def __init__(
        self,
        *,
        connect_error: Exception | None = None,
        tools: list[dict[str, Any]] | None = None,
    ) -> None:
        self._connect_error = connect_error
        self._tools = tools or []
        self.metadata: MCPConnectionMetadata | None = None
        self.closed = False

    async def connect(self) -> None:
        if self._connect_error is not None:
            raise self._connect_error
        self.metadata = MCPConnectionMetadata(state=MCPConnectionState.CONNECTED, server_name="remote")

    async def close(self) -> None:
        self.closed = True

    async def list_tools(self):
        return {"tools": self._tools}

    async def list_resources(self):
        return {"resources": []}

    async def read_resource(self, uri: str):
        return {"contents": []}

    async def list_prompts(self):
        return {"prompts": []}

    async def get_prompt(self, name: str, arguments: dict[str, str] | None = None):
        return {"messages": []}

    async def call_tool(self, name: str, arguments: dict[str, Any] | None = None, **kwargs):
        return {"content": [{"type": "text", "text": "ok"}]}
