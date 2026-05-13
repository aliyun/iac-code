from __future__ import annotations

import acp
import pytest

from iac_code.acp.server import _PROVIDER_ENV_VARS, ACPServer
from iac_code.acp.version import CURRENT_VERSION, negotiate_version
from iac_code.types.stream_events import MessageEndEvent, TextDeltaEvent, Usage


class FakeConn:
    def __init__(self) -> None:
        self.updates: list[tuple[str, object]] = []

    async def session_update(self, session_id: str, update: object, **kwargs: object) -> None:
        self.updates.append((session_id, update))


class FakeLoop:
    async def run_streaming(self, prompt: str):
        yield TextDeltaEvent(text=f"echo: {prompt}")
        yield MessageEndEvent(stop_reason="stop", usage=Usage())


class FakeRuntime:
    def __init__(self, session_id: str = "lifecycle-s1") -> None:
        self.session_id = session_id
        self.agent_loop = FakeLoop()
        self.tool_registry = None


def _patch_server(monkeypatch: pytest.MonkeyPatch, runtime: FakeRuntime | None = None) -> None:
    monkeypatch.setattr("iac_code.acp.server.load_saved_model", lambda: "fake-model")
    monkeypatch.setattr(
        "iac_code.acp.server.create_agent_runtime",
        lambda options: runtime or FakeRuntime(),
    )
    monkeypatch.setattr(
        "iac_code.acp.server.replace_bash_with_acp_terminal",
        lambda *args, **kwargs: None,
    )


@pytest.mark.asyncio
async def test_protocol_lifecycle_full_flow(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """P0: Full handshake flow initialize -> new_session -> prompt -> cancel -> list_sessions."""
    _patch_server(monkeypatch)
    # list_sessions uses get_projects_dir/get_project_dir from iac_code.utils.project_paths
    monkeypatch.setattr("iac_code.utils.project_paths.get_config_dir", lambda: tmp_path)
    (tmp_path / "projects" / "-tmp").mkdir(parents=True)

    server = ACPServer()
    conn = FakeConn()

    # 1. on_connect
    server.on_connect(conn)
    assert server.conn is conn

    # 2. initialize
    init_resp = await server.initialize(
        protocol_version=1,
        client_capabilities=acp.schema.ClientCapabilities(),
    )
    assert isinstance(init_resp, acp.InitializeResponse)
    assert init_resp.protocol_version == 1

    # 3. new_session
    new_resp = await server.new_session(cwd="/tmp")
    assert new_resp.session_id is not None
    assert new_resp.session_id in server.sessions

    # 4. prompt
    prompt_resp = await server.prompt(
        session_id=new_resp.session_id,
        prompt=[acp.schema.TextContentBlock(type="text", text="hello")],
    )
    assert prompt_resp.stop_reason == "end_turn"

    # 5. cancel (no active task, should not raise)
    await server.cancel(session_id=new_resp.session_id)

    # 6. list_sessions
    list_resp = await server.list_sessions()
    assert isinstance(list_resp, acp.schema.ListSessionsResponse)


@pytest.mark.asyncio
async def test_initialize_called_twice_is_idempotent() -> None:
    """P1: Repeated initialization is idempotent."""
    server = ACPServer()

    resp1 = await server.initialize(
        protocol_version=1,
        client_capabilities=acp.schema.ClientCapabilities(),
    )
    resp2 = await server.initialize(
        protocol_version=1,
        client_capabilities=acp.schema.ClientCapabilities(),
    )

    assert resp1.protocol_version == resp2.protocol_version
    assert resp1.agent_info == resp2.agent_info


@pytest.mark.asyncio
async def test_new_session_without_connection_raises_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """P1: Calling new_session without initialization (conn is None)."""
    _patch_server(monkeypatch)
    server = ACPServer()
    assert server.conn is None

    with pytest.raises(acp.RequestError):
        await server.new_session(cwd="/tmp")


@pytest.mark.asyncio
async def test_cancel_without_connection_raises_error() -> None:
    """P1: Calling cancel without initialization (conn is None, no session)."""
    server = ACPServer()
    assert server.conn is None

    with pytest.raises(acp.RequestError):
        await server.cancel(session_id="nonexistent")


# ---------------------------------------------------------------------------
# Smoke test
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_acp_server_new_session_then_prompt(monkeypatch) -> None:
    server = ACPServer()
    conn = FakeConn()
    server.on_connect(conn)

    monkeypatch.setattr(
        "iac_code.acp.server.create_agent_runtime",
        lambda options: FakeRuntime(session_id="s1"),
    )

    await server.initialize(protocol_version=1, client_capabilities=acp.schema.ClientCapabilities())
    created = await server.new_session(cwd="/tmp", mcp_servers=[])
    response = await server.prompt(
        session_id=created.session_id,
        prompt=[acp.schema.TextContentBlock(type="text", text="hello")],
    )

    assert response.stop_reason == "end_turn"
    # First update is the available_commands_update pushed during new_session
    assert conn.updates[0][0] == "s1"
    assert conn.updates[0][1].session_update == "available_commands_update"
    # Subsequent updates are agent_message_chunk from the prompt
    assert conn.updates[1][0] == "s1"
    assert conn.updates[1][1].session_update == "agent_message_chunk"


# ---------------------------------------------------------------------------
# Server initialize tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_initialize_advertises_only_implemented_initial_capabilities() -> None:
    server = ACPServer()

    response = await server.initialize(
        protocol_version=1,
        client_capabilities=acp.schema.ClientCapabilities(),
        client_info=acp.schema.Implementation(name="test-client", version="1.0"),
    )

    assert response.protocol_version == 1
    assert response.agent_capabilities.load_session is True
    assert response.agent_capabilities.prompt_capabilities.embedded_context is True
    assert response.agent_capabilities.prompt_capabilities.image is False
    assert response.agent_capabilities.mcp_capabilities.http is False
    # auth_methods should declare supported provider env-var authentication
    assert len(response.auth_methods) > 0
    for method in response.auth_methods:
        assert method.type == "env_var"
        assert method.id
        assert method.name
        assert len(method.vars) == 1
        assert method.vars[0].secret is True


@pytest.mark.asyncio
async def test_auth_methods_include_all_supported_provider_env_vars() -> None:
    """Scenario 11: auth_methods must include env vars for all supported providers."""
    server = ACPServer()
    response = await server.initialize(
        protocol_version=1,
        client_capabilities=acp.schema.ClientCapabilities(),
        client_info=acp.schema.Implementation(name="test-client", version="1.0"),
    )

    env_var_names = set()
    for method in response.auth_methods:
        for var in method.vars:
            env_var_names.add(var.name)

    # Must include at least these well-known provider env vars
    expected_env_vars = {env_name for env_name, _, _ in _PROVIDER_ENV_VARS}
    assert expected_env_vars.issubset(env_var_names), f"Missing env vars: {expected_env_vars - env_var_names}"
    # Specifically check known providers
    assert "DASHSCOPE_API_KEY" in env_var_names
    assert "OPENAI_API_KEY" in env_var_names
    assert "ANTHROPIC_API_KEY" in env_var_names
    assert "DEEPSEEK_API_KEY" in env_var_names


# ---------------------------------------------------------------------------
# Version negotiation tests
# ---------------------------------------------------------------------------


def test_negotiate_exact_supported_version() -> None:
    assert negotiate_version(1) == CURRENT_VERSION


def test_negotiate_lower_version_returns_current_for_client_decision() -> None:
    assert negotiate_version(0) == CURRENT_VERSION


def test_negotiate_higher_version_uses_highest_supported() -> None:
    assert negotiate_version(99) == CURRENT_VERSION


def test_negotiate_version_very_high() -> None:
    """Extremely high version still returns highest supported."""
    result = negotiate_version(99999)
    assert result == CURRENT_VERSION


def test_negotiated_version_contains_sdk_version() -> None:
    """Verify ACPVersionSpec includes correct sdk_version."""
    result = negotiate_version(1)
    assert result.sdk_version == "0.9.0"
    assert result.protocol_version == 1


def test_negotiate_negative_version() -> None:
    """Negative version treated like below minimum."""
    result = negotiate_version(-1)
    assert result == CURRENT_VERSION
