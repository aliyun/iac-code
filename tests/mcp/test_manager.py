from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest

from iac_code.mcp import manager as manager_module
from iac_code.mcp.errors import MCPConnectionError, MCPElicitationUnsupportedError, MCPNeedsAuthError
from iac_code.mcp.manager import MCPManager
from iac_code.mcp.types import MCPConfigScope, MCPConnectionState, MCPServerConfig, ScopedMCPServerConfig
from iac_code.services.agent_factory import _mcp_connection_warnings


@pytest.mark.asyncio
async def test_connect_all_discovers_tools_and_isolates_failed_servers() -> None:
    good = _scoped("good", {"command": "uvx"})
    bad = _scoped("bad", {"command": "bad"})
    clients = {
        "good": FakeClient(tools=[{"name": "plan", "description": "Plan", "inputSchema": {"type": "object"}}]),
        "bad": FakeClient(fail_connect=True),
    }
    manager = MCPManager([good, bad], client_factory=lambda config: clients[config.name])

    await manager.connect_all()

    assert manager.connection_state("good") is MCPConnectionState.CONNECTED
    assert manager.connection_state("bad") is MCPConnectionState.FAILED
    assert manager.connection("bad").error
    assert [tool.public_name for tool in manager.list_tools()] == ["mcp__good__plan"]
    assert manager.list_tools()[0].input_schema == {"type": "object"}


@pytest.mark.asyncio
async def test_connection_failure_logs_redacted_diagnostic(monkeypatch: pytest.MonkeyPatch) -> None:
    scoped = _scoped("broken", {"command": "bad"})
    client = FakeClient(
        connect_error=MCPConnectionError(
            "failed at /Users/alice/.iac-code/settings.yml; Authorization: Bearer sk-live-secret; api_key=plain-secret"
        )
    )
    manager = MCPManager([scoped], client_factory=lambda config: client)
    warning = Mock()
    monkeypatch.setattr(manager_module, "logger", Mock(warning=warning), raising=False)

    await manager.connect_all()

    assert manager.connection_state("broken") is MCPConnectionState.FAILED
    assert warning.call_count == 1
    logged = " ".join(str(part) for call in warning.call_args_list for part in (*call.args, call.kwargs))
    assert "broken" in logged
    assert "connection failed" in logged
    assert "sk-live-secret" not in logged
    assert "plain-secret" not in logged
    assert "Authorization: Bearer" not in logged


@pytest.mark.asyncio
async def test_discovery_failure_for_optional_capability_does_not_fail_tools_only_server() -> None:
    scoped = _scoped("tools-only", {"command": "uvx"})
    client = FakeClient(
        tools=[{"name": "plan", "description": "Plan", "inputSchema": {"type": "object"}}],
        fail_resources=True,
        fail_prompts=True,
    )
    manager = MCPManager([scoped], client_factory=lambda config: client)

    await manager.connect_all()

    assert manager.connection_state("tools-only") is MCPConnectionState.CONNECTED
    assert [tool.public_name for tool in manager.list_tools()] == ["mcp__tools_only__plan"]
    assert "resources" in manager.connection("tools-only").capability_errors
    assert "prompts" in manager.connection("tools-only").capability_errors


@pytest.mark.asyncio
async def test_discovery_timeout_records_capability_warning_without_hanging() -> None:
    scoped = _scoped("slow", {"command": "uvx"})
    client = FakeClient(
        tools=[{"name": "plan", "description": "Plan", "inputSchema": {"type": "object"}}],
        resources_delay=0.2,
    )
    manager = MCPManager(
        [scoped],
        client_factory=lambda config: client,
        connect_timeout_seconds=0.01,
    )

    await asyncio.wait_for(manager.connect_all(), timeout=1)

    assert manager.connection_state("slow") is MCPConnectionState.CONNECTED
    assert [tool.public_name for tool in manager.list_tools()] == ["mcp__slow__plan"]
    assert "resources" in manager.connection("slow").capability_errors
    warnings = _mcp_connection_warnings(manager)
    assert any(warning.code == "resources_failed" and warning.server_name == "slow" for warning in warnings)


@pytest.mark.asyncio
async def test_one_resource_discovery_failure_does_not_hide_other_server_resources() -> None:
    failing = _scoped("failing", {"command": "uvx"})
    good = _scoped("good", {"command": "uvx"})
    clients = {
        "failing": FakeClient(fail_resources=True),
        "good": FakeClient(resources=[{"uri": "resource://good/template", "name": "template"}]),
    }
    manager = MCPManager([failing, good], client_factory=lambda config: clients[config.name])

    await manager.connect_all()

    assert manager.connection_state("failing") is MCPConnectionState.CONNECTED
    assert manager.connection_state("good") is MCPConnectionState.CONNECTED
    assert [resource.uri for resource in manager.list_resources()] == ["resource://good/template"]


@pytest.mark.asyncio
async def test_handle_list_changed_refreshes_discovery_cache() -> None:
    scoped = _scoped("ros", {"command": "uvx"})
    client = FakeClient(tools=[{"name": "first", "inputSchema": {"type": "object"}}])
    manager = MCPManager([scoped], client_factory=lambda config: client)
    await manager.connect_all()

    client.tools = [{"name": "second", "description": "Second", "inputSchema": {"type": "object"}}]
    await manager.handle_list_changed("ros", capability="tools")

    assert [tool.tool_name for tool in manager.list_tools()] == ["second"]
    assert client.list_tools_calls == 2


@pytest.mark.asyncio
async def test_handle_resources_list_changed_refreshes_resource_cache() -> None:
    scoped = _scoped("ros", {"command": "uvx"})
    client = FakeClient(resources=[{"uri": "resource://first", "name": "first"}])
    manager = MCPManager([scoped], client_factory=lambda config: client)
    await manager.connect_all()

    client.resources = [{"uri": "resource://second", "name": "second"}]
    await manager.handle_list_changed("ros", capability="resources")

    assert [resource.uri for resource in manager.list_resources()] == ["resource://second"]
    assert client.list_resources_calls == 2


@pytest.mark.asyncio
async def test_handle_prompts_list_changed_refreshes_prompt_cache() -> None:
    scoped = _scoped("ros", {"command": "uvx"})
    client = FakeClient(prompts=[{"name": "first", "description": "First"}])
    manager = MCPManager([scoped], client_factory=lambda config: client)
    await manager.connect_all()

    client.prompts = [{"name": "second", "description": "Second"}]
    await manager.handle_list_changed("ros", capability="prompts")

    assert [prompt.prompt_name for prompt in manager.list_prompts()] == ["second"]
    assert client.list_prompts_calls == 2


@pytest.mark.asyncio
async def test_manager_exposes_roots_and_rejects_elicitation(tmp_path: Path) -> None:
    manager = MCPManager([], roots=[tmp_path / "repo", tmp_path / "shared"])

    assert await manager.list_roots() == [
        (tmp_path / "repo").resolve().as_uri(),
        (tmp_path / "shared").resolve().as_uri(),
    ]
    with pytest.raises(MCPElicitationUnsupportedError):
        await manager.request_elicitation("server", {"message": "Need input"})


@pytest.mark.asyncio
async def test_disconnect_all_closes_connected_clients() -> None:
    scoped = _scoped("ros", {"command": "uvx"})
    client = FakeClient()
    manager = MCPManager([scoped], client_factory=lambda config: client)
    await manager.connect_all()

    await manager.disconnect_all()

    assert client.closed is True
    assert manager.connection_state("ros") is MCPConnectionState.DISABLED


@pytest.mark.asyncio
async def test_disconnect_all_continues_after_client_close_failure() -> None:
    first = _scoped("first", {"command": "uvx"})
    second = _scoped("second", {"command": "uvx"})
    clients = {
        "first": FakeClient(close_error=RuntimeError("close failed")),
        "second": FakeClient(),
    }
    manager = MCPManager([first, second], client_factory=lambda config: clients[config.name])
    await manager.connect_all()

    await manager.disconnect_all()

    assert clients["first"].closed is True
    assert clients["second"].closed is True
    assert manager.connection_state("first") is MCPConnectionState.DISABLED
    assert manager.connection_state("second") is MCPConnectionState.DISABLED


@pytest.mark.asyncio
async def test_remote_reconnect_is_bounded() -> None:
    scoped = _scoped("remote", {"type": "http", "url": "https://example.com/mcp"})
    attempts = 0

    def factory(config):
        nonlocal attempts
        attempts += 1
        return FakeClient(fail_connect=True)

    manager = MCPManager([scoped], client_factory=factory, max_reconnect_attempts=1)

    await manager.connect_all()
    await manager.reconnect_failed("remote")
    await manager.reconnect_failed("remote")

    assert attempts == 2
    assert manager.connection("remote").retry_count == 1
    assert manager.connection_state("remote") is MCPConnectionState.FAILED


@pytest.mark.asyncio
async def test_successful_reconnect_refreshes_discovery_and_notifies_listeners() -> None:
    scoped = _scoped("remote", {"type": "http", "url": "https://example.com/mcp"})
    clients = [
        FakeClient(fail_connect=True),
        FakeClient(
            tools=[{"name": "search", "inputSchema": {"type": "object"}}],
            resources=[{"uri": "resource://template", "name": "template"}],
            prompts=[{"name": "review"}],
        ),
    ]
    notifications: list[tuple[str, str]] = []

    def factory(config):
        return clients.pop(0)

    manager = MCPManager([scoped], client_factory=factory, max_reconnect_attempts=1)
    manager.add_change_listener(lambda server, capability: notifications.append((server, capability)))

    await manager.connect_all()
    await manager.reconnect_failed("remote")

    assert manager.connection_state("remote") is MCPConnectionState.CONNECTED
    assert [tool.tool_name for tool in manager.list_tools()] == ["search"]
    assert [resource.uri for resource in manager.list_resources()] == ["resource://template"]
    assert [prompt.prompt_name for prompt in manager.list_prompts()] == ["review"]
    assert notifications == [
        ("remote", "tools"),
        ("remote", "resources"),
        ("remote", "prompts"),
    ]


@pytest.mark.asyncio
async def test_remote_401_moves_connection_to_needs_auth() -> None:
    scoped = _scoped("remote", {"type": "http", "url": "https://example.com/mcp", "oauth": {"clientId": "id"}})
    manager = MCPManager(
        [scoped],
        client_factory=lambda config: FakeClient(needs_auth=True),
    )

    await manager.connect_all()

    assert manager.connection_state("remote") is MCPConnectionState.NEEDS_AUTH
    assert "authentication" in (manager.connection("remote").error or "")


@pytest.mark.asyncio
async def test_needs_auth_cache_skips_repeated_connect_attempt_until_reconnect() -> None:
    scoped = _scoped("remote", {"type": "http", "url": "https://example.com/mcp", "oauth": {"clientId": "id"}})
    attempts = 0

    def factory(config):
        nonlocal attempts
        attempts += 1
        return FakeClient(needs_auth=True)

    manager = MCPManager([scoped], client_factory=factory)

    await manager.connect_all()
    await manager.connect_all()

    assert attempts == 1
    assert manager.connection_state("remote") is MCPConnectionState.NEEDS_AUTH

    await manager.reconnect("remote")

    assert attempts == 2


@pytest.mark.asyncio
async def test_public_tool_names_are_made_unique_when_normalization_collides() -> None:
    first = _scoped("a-b", {"command": "uvx"})
    second = _scoped("a_b", {"command": "uvx"})
    clients = {
        "a-b": FakeClient(tools=[{"name": "search", "inputSchema": {"type": "object"}}]),
        "a_b": FakeClient(tools=[{"name": "search", "inputSchema": {"type": "object"}}]),
    }
    manager = MCPManager([first, second], client_factory=lambda config: clients[config.name])

    await manager.connect_all()

    public_names = [tool.public_name for tool in manager.list_tools()]
    assert len(public_names) == 2
    assert len(set(public_names)) == 2
    assert all(name.startswith("mcp__a_b__search_") for name in public_names)


@pytest.mark.asyncio
async def test_public_prompt_names_are_made_unique_when_normalization_collides() -> None:
    first = _scoped("a-b", {"command": "uvx"})
    second = _scoped("a_b", {"command": "uvx"})
    clients = {
        "a-b": FakeClient(prompts=[{"name": "review"}]),
        "a_b": FakeClient(prompts=[{"name": "review"}]),
    }
    manager = MCPManager([first, second], client_factory=lambda config: clients[config.name])

    await manager.connect_all()

    public_names = [prompt.public_name for prompt in manager.list_prompts()]
    assert len(public_names) == 2
    assert len(set(public_names)) == 2
    assert all(name.startswith("mcp__a_b__review_") for name in public_names)


@pytest.mark.asyncio
async def test_public_skill_resource_names_are_made_unique_when_normalization_collides() -> None:
    first = _scoped("a-b", {"command": "uvx"})
    second = _scoped("a_b", {"command": "uvx"})
    clients = {
        "a-b": FakeClient(resources=[{"uri": "skill://a-b/vpc", "name": "vpc"}]),
        "a_b": FakeClient(resources=[{"uri": "skill://a_b/vpc", "name": "vpc"}]),
    }
    manager = MCPManager([first, second], client_factory=lambda config: clients[config.name])

    await manager.connect_all()

    public_names = [resource.public_name for resource in manager.list_resources() if resource.is_skill_resource]
    assert len(public_names) == 2
    assert len(set(public_names)) == 2
    assert all(name is not None and name.startswith("mcp__a_b__vpc_") for name in public_names)


@pytest.mark.asyncio
async def test_prompt_and_skill_resource_public_names_share_one_command_namespace() -> None:
    scoped = _scoped("live", {"command": "uvx"})
    client = FakeClient(
        prompts=[{"name": "review"}],
        resources=[{"uri": "skill://live/review", "name": "review"}],
    )
    manager = MCPManager([scoped], client_factory=lambda config: client)

    await manager.connect_all()

    prompt_names = [prompt.public_name for prompt in manager.list_prompts()]
    skill_names = [resource.public_name for resource in manager.list_resources() if resource.is_skill_resource]
    assert len(set(prompt_names + skill_names)) == 2
    assert all(name.startswith("mcp__live__review_") for name in prompt_names + skill_names if name is not None)


@pytest.mark.asyncio
async def test_connect_all_uses_bounded_concurrency() -> None:
    running = 0
    peak = 0

    def factory(config):
        async def on_connect() -> None:
            nonlocal running, peak
            running += 1
            peak = max(peak, running)
            await asyncio.sleep(0.02)
            running -= 1

        return FakeClient(tools=[{"name": "plan", "inputSchema": {"type": "object"}}], on_connect=on_connect)

    manager = MCPManager(
        [_scoped("one", {"command": "uvx"}), _scoped("two", {"command": "uvx"}), _scoped("three", {"command": "uvx"})],
        client_factory=factory,
        max_concurrent_connections=2,
    )

    await manager.connect_all()

    assert peak == 2


def _scoped(name: str, config: dict[str, Any]) -> ScopedMCPServerConfig:
    return ScopedMCPServerConfig(
        config=MCPServerConfig.from_mapping(name, config),
        scope=MCPConfigScope.SESSION,
    )


class FakeClient:
    def __init__(
        self,
        *,
        tools: list[dict[str, Any]] | None = None,
        resources: list[dict[str, Any]] | None = None,
        prompts: list[dict[str, Any]] | None = None,
        fail_connect: bool = False,
        needs_auth: bool = False,
        fail_resources: bool = False,
        fail_prompts: bool = False,
        resources_delay: float = 0,
        close_error: Exception | None = None,
        connect_error: Exception | None = None,
        on_connect: Any = None,
    ) -> None:
        self.tools = tools or []
        self.resources = resources or []
        self.prompts = prompts or []
        self.fail_connect = fail_connect
        self.needs_auth = needs_auth
        self.fail_resources = fail_resources
        self.fail_prompts = fail_prompts
        self.resources_delay = resources_delay
        self.close_error = close_error
        self.connect_error = connect_error
        self.on_connect = on_connect
        self.closed = False
        self.list_tools_calls = 0
        self.list_resources_calls = 0
        self.list_prompts_calls = 0

    async def connect(self) -> None:
        if self.on_connect is not None:
            await self.on_connect()
        if self.needs_auth:
            raise MCPNeedsAuthError("authentication required")
        if self.connect_error is not None:
            raise self.connect_error
        if self.fail_connect:
            raise MCPConnectionError("connect failed")

    async def close(self) -> None:
        self.closed = True
        if self.close_error is not None:
            raise self.close_error

    async def list_tools(self) -> list[dict[str, Any]]:
        self.list_tools_calls += 1
        return self.tools

    async def call_tool(self, name: str, arguments: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
        return {"content": [{"type": "text", "text": name}], "arguments": arguments or {}}

    async def list_resources(self) -> list[dict[str, Any]]:
        self.list_resources_calls += 1
        if self.resources_delay:
            await asyncio.sleep(self.resources_delay)
        if self.fail_resources:
            raise MCPConnectionError("resources unsupported")
        return self.resources

    async def read_resource(self, uri: str) -> dict[str, Any]:
        return {"contents": [{"uri": uri, "text": "resource"}]}

    async def list_prompts(self) -> list[dict[str, Any]]:
        self.list_prompts_calls += 1
        if self.fail_prompts:
            raise MCPConnectionError("prompts unsupported")
        return self.prompts

    async def get_prompt(self, name: str, arguments: dict[str, str] | None = None) -> dict[str, Any]:
        return {"description": name, "messages": [], "arguments": arguments or {}}
