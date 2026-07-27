"""Tests for the web MCP settings adapter and its HTTP routes.

These exercise the web console's MCP management surface, which reuses the REPL /
CLI MCP service layer. All state is isolated with ``tmp_path`` and a fake
``IAC_CODE_CONFIG_DIR``; no real network, cloud, or LLM calls are made.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import yaml

from iac_code.mcp.types import MCPPromptRecord
from iac_code.web import mcp_settings
from iac_code.web.mcp_settings import MCPWebError


@pytest.fixture()
def isolated_config(monkeypatch, tmp_path: Path) -> Path:
    """Point config storage at a throwaway dir and return a workspace root."""

    config_dir = tmp_path / "config"
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(config_dir))
    repo = tmp_path / "repo"
    repo.mkdir()
    return repo


def _read_user_settings(monkeypatch_env_dir: Path) -> dict:
    settings = monkeypatch_env_dir / "settings.yml"
    if not settings.exists():
        return {}
    return yaml.safe_load(settings.read_text(encoding="utf-8")) or {}


# ---------------------------------------------------------------------------
# Scope parsing
# ---------------------------------------------------------------------------


def test_parse_scope_rejects_unknown_value() -> None:
    with pytest.raises(MCPWebError) as excinfo:
        mcp_settings._parse_scope("galaxy")
    assert excinfo.value.status_code == 400


def test_parse_scope_rejects_non_persistable_scope() -> None:
    with pytest.raises(MCPWebError):
        mcp_settings._parse_scope("session")


# ---------------------------------------------------------------------------
# Building configs from form fields
# ---------------------------------------------------------------------------


def test_build_config_from_fields_stdio() -> None:
    config = mcp_settings._build_config_from_fields(
        {"transport": "stdio", "command": "npx", "args": ["-y", "server"], "env": {"FOO": "bar"}}
    )
    assert config == {"command": "npx", "args": ["-y", "server"], "env": {"FOO": "bar"}}


def test_build_config_from_fields_stdio_requires_command() -> None:
    with pytest.raises(MCPWebError):
        mcp_settings._build_config_from_fields({"transport": "stdio", "command": "  "})


@pytest.mark.parametrize("transport", ["http", "sse"])
def test_build_config_from_fields_remote(transport: str) -> None:
    config = mcp_settings._build_config_from_fields(
        {"transport": transport, "url": "https://example.com/mcp", "headers": {"X-Env": "${TOKEN}"}}
    )
    assert config == {"type": transport, "url": "https://example.com/mcp", "headers": {"X-Env": "${TOKEN}"}}


def test_build_config_from_fields_ws_drops_headers() -> None:
    config = mcp_settings._build_config_from_fields({"transport": "ws", "url": "ws://example.com/mcp"})
    assert config == {"type": "ws", "url": "ws://example.com/mcp"}


def test_build_config_from_fields_oauth() -> None:
    config = mcp_settings._build_config_from_fields(
        {
            "transport": "http",
            "url": "https://example.com/mcp",
            "oauth": {
                "clientId": "abc",
                "clientSecretEnv": "MY_SECRET",
                "callbackPort": "8765",
                "authServerMetadataUrl": "https://example.com/.well-known",
            },
        }
    )
    assert config["oauth"] == {
        "clientId": "abc",
        "clientSecretEnv": "MY_SECRET",
        "callbackPort": 8765,
        "authServerMetadataUrl": "https://example.com/.well-known",
    }


def test_build_config_from_fields_oauth_port_must_be_int() -> None:
    with pytest.raises(MCPWebError):
        mcp_settings._build_config_from_fields(
            {"transport": "http", "url": "https://example.com/mcp", "oauth": {"callbackPort": "not-a-port"}}
        )


def test_build_config_from_fields_invalid_transport() -> None:
    with pytest.raises(MCPWebError):
        mcp_settings._build_config_from_fields({"transport": "carrier-pigeon", "url": "x"})


# ---------------------------------------------------------------------------
# Add / list / update / remove
# ---------------------------------------------------------------------------


def test_add_and_list_stdio_server(isolated_config: Path, monkeypatch, tmp_path: Path) -> None:
    result = mcp_settings.add_mcp_server(
        isolated_config,
        name="local-fs",
        fields={"transport": "stdio", "command": "npx", "args": ["server"]},
        scope="user",
    )
    assert result["name"] == "local-fs"
    assert result["scope"] == "user"

    listing = mcp_settings.list_mcp_servers(isolated_config)
    servers = {server["name"]: server for server in listing["servers"]}
    assert "local-fs" in servers
    entry = servers["local-fs"]
    assert entry["transport"] == "stdio"
    assert entry["scope"] == "user"
    assert entry["disabled"] is False
    assert entry["editable_config"]["command"] == "npx"


def test_add_duplicate_server_conflicts(isolated_config: Path) -> None:
    mcp_settings.add_mcp_server(
        isolated_config,
        name="dup",
        fields={"transport": "stdio", "command": "npx"},
        scope="user",
    )
    with pytest.raises(MCPWebError) as excinfo:
        mcp_settings.add_mcp_server(
            isolated_config,
            name="dup",
            fields={"transport": "stdio", "command": "npx"},
            scope="user",
        )
    assert excinfo.value.status_code == 409


def test_add_rejects_plaintext_secret(isolated_config: Path) -> None:
    with pytest.raises(MCPWebError):
        mcp_settings.add_mcp_server(
            isolated_config,
            name="leaky",
            fields={
                "transport": "http",
                "url": "https://example.com/mcp",
                "headers": {"Authorization": "Bearer sk-abcdefgh12345"},
            },
            scope="user",
        )


def test_add_server_json(isolated_config: Path) -> None:
    result = mcp_settings.add_mcp_server_json(
        isolated_config,
        name="json-srv",
        config={"command": "uvx", "args": ["thing"]},
        scope="user",
    )
    assert result["name"] == "json-srv"
    listing = mcp_settings.list_mcp_servers(isolated_config)
    assert any(server["name"] == "json-srv" for server in listing["servers"])


def test_add_server_json_requires_object(isolated_config: Path) -> None:
    with pytest.raises(MCPWebError):
        mcp_settings.add_mcp_server_json(isolated_config, name="bad", config=["not", "object"], scope="user")


def test_update_missing_server_is_404(isolated_config: Path) -> None:
    with pytest.raises(MCPWebError) as excinfo:
        mcp_settings.update_mcp_server(
            isolated_config,
            name="ghost",
            fields={"transport": "stdio", "command": "npx"},
            scope="user",
        )
    assert excinfo.value.status_code == 404


def test_update_server_replaces_config(isolated_config: Path) -> None:
    mcp_settings.add_mcp_server(
        isolated_config,
        name="edit-me",
        fields={"transport": "stdio", "command": "old"},
        scope="user",
    )
    mcp_settings.update_mcp_server(
        isolated_config,
        name="edit-me",
        fields={"transport": "stdio", "command": "new", "args": ["--flag"]},
        scope="user",
    )
    listing = mcp_settings.list_mcp_servers(isolated_config)
    entry = next(server for server in listing["servers"] if server["name"] == "edit-me")
    assert entry["editable_config"]["command"] == "new"
    assert entry["editable_config"]["args"] == ["--flag"]


def test_update_server_config_takes_precedence_over_fields(isolated_config: Path) -> None:
    mcp_settings.add_mcp_server(
        isolated_config,
        name="edit-json",
        fields={"transport": "stdio", "command": "old"},
        scope="user",
    )
    mcp_settings.update_mcp_server(
        isolated_config,
        name="edit-json",
        fields={"transport": "stdio", "command": "ignored"},
        config={"command": "from-json"},
        scope="user",
    )
    listing = mcp_settings.list_mcp_servers(isolated_config)
    entry = next(server for server in listing["servers"] if server["name"] == "edit-json")
    assert entry["editable_config"]["command"] == "from-json"


def test_remove_server(isolated_config: Path) -> None:
    mcp_settings.add_mcp_server(
        isolated_config,
        name="temp",
        fields={"transport": "stdio", "command": "npx"},
        scope="user",
    )
    mcp_settings.remove_mcp_server(isolated_config, name="temp", scope="user")
    listing = mcp_settings.list_mcp_servers(isolated_config)
    assert all(server["name"] != "temp" for server in listing["servers"])


def test_enable_disable_roundtrip(isolated_config: Path) -> None:
    mcp_settings.add_mcp_server(
        isolated_config,
        name="toggler",
        fields={"transport": "stdio", "command": "npx"},
        scope="user",
    )
    mcp_settings.set_mcp_enabled(isolated_config, name="toggler", disabled=True, scope="user")
    listing = mcp_settings.list_mcp_servers(isolated_config)
    entry = next(server for server in listing["servers"] if server["name"] == "toggler")
    assert entry["disabled"] is True

    mcp_settings.set_mcp_enabled(isolated_config, name="toggler", disabled=False, scope="user")
    listing = mcp_settings.list_mcp_servers(isolated_config)
    entry = next(server for server in listing["servers"] if server["name"] == "toggler")
    assert entry["disabled"] is False


# ---------------------------------------------------------------------------
# Project approval
# ---------------------------------------------------------------------------


def test_project_server_pending_then_approved(isolated_config: Path) -> None:
    project_file = isolated_config / ".mcp.json"
    project_file.write_text(
        json.dumps({"mcpServers": {"proj-srv": {"command": "npx", "args": ["proj"]}}}),
        encoding="utf-8",
    )

    listing = mcp_settings.list_mcp_servers(isolated_config)
    entry = next(server for server in listing["servers"] if server["name"] == "proj-srv")
    assert entry["approval_state"] == "pending-approval"

    mcp_settings.approve_mcp_server(isolated_config, name="proj-srv")
    listing = mcp_settings.list_mcp_servers(isolated_config)
    entry = next(server for server in listing["servers"] if server["name"] == "proj-srv")
    assert entry["approval_state"] == "approved"


def test_approve_missing_project_server_errors(isolated_config: Path) -> None:
    with pytest.raises(MCPWebError):
        mcp_settings.approve_mcp_server(isolated_config, name="nope")


# ---------------------------------------------------------------------------
# OAuth flow registry
# ---------------------------------------------------------------------------


class _FakePending:
    def __init__(self) -> None:
        self.authorization_url = "https://auth.example.com/authorize?x=1"
        self.redirect_uri = "http://127.0.0.1:1234/callback"
        self.timeout_seconds = 120
        self.completed_with: str | None = None
        self.cancelled = False

    def wait(self) -> bool:
        return True

    def complete_manually(self, url: str) -> None:
        self.completed_with = url


def test_oauth_flow_start_wait_and_complete(monkeypatch, isolated_config: Path) -> None:
    pending = _FakePending()
    monkeypatch.setattr(mcp_settings, "start_mcp_oauth_flow", lambda *a, **k: pending)

    started = mcp_settings.start_mcp_auth(isolated_config, name="remote", scope="user")
    assert started["authorization_url"] == pending.authorization_url
    flow_id = started["flow_id"]

    completed = mcp_settings.wait_mcp_auth(flow_id)
    assert completed["completed"] is True
    # The flow is popped after wait; a second wait is a 404.
    with pytest.raises(MCPWebError) as excinfo:
        mcp_settings.wait_mcp_auth(flow_id)
    assert excinfo.value.status_code == 404


def test_oauth_flow_manual_completion(monkeypatch, isolated_config: Path) -> None:
    pending = _FakePending()
    monkeypatch.setattr(mcp_settings, "start_mcp_oauth_flow", lambda *a, **k: pending)

    flow_id = mcp_settings.start_mcp_auth(isolated_config, name="remote", scope="user")["flow_id"]
    result = mcp_settings.complete_mcp_auth(flow_id, "http://127.0.0.1:1234/callback?code=xyz")
    assert result["completed"] is True
    assert pending.completed_with == "http://127.0.0.1:1234/callback?code=xyz"


def test_oauth_flow_cancel(monkeypatch, isolated_config: Path) -> None:
    pending = _FakePending()
    monkeypatch.setattr(mcp_settings, "start_mcp_oauth_flow", lambda *a, **k: pending)
    monkeypatch.setattr(mcp_settings, "cancel_pending_mcp_oauth_flow", lambda flow: None)

    flow_id = mcp_settings.start_mcp_auth(isolated_config, name="remote", scope="user")["flow_id"]
    result = mcp_settings.cancel_mcp_auth(flow_id)
    assert result["cancelled"] is True
    with pytest.raises(MCPWebError):
        mcp_settings.cancel_mcp_auth(flow_id)


def test_wait_unknown_flow_is_404() -> None:
    with pytest.raises(MCPWebError) as excinfo:
        mcp_settings.wait_mcp_auth("does-not-exist")
    assert excinfo.value.status_code == 404


# ---------------------------------------------------------------------------
# HTTP routes (wiring smoke test)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mcp_routes_add_list_and_remove(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    from iac_code.web.app import create_app
    from iac_code.web.session_manager import WebSessionManager

    manager = WebSessionManager(projects_dir=tmp_path / "projects", cwd=tmp_path)
    session = manager.create_session(session_id="mcp-routes")
    app = create_app(session_manager=manager)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        add = await client.post(
            "/api/mcp/servers",
            json={
                "sessionId": session.session_id,
                "name": "route-srv",
                "scope": "user",
                "fields": {"transport": "stdio", "command": "npx", "args": ["server"]},
            },
        )
        assert add.status_code == 200, add.text
        assert add.json()["name"] == "route-srv"

        listing = await client.get("/api/mcp/servers", params={"sessionId": session.session_id})
        assert listing.status_code == 200
        names = [server["name"] for server in listing.json()["servers"]]
        assert "route-srv" in names

        removed = await client.delete(
            "/api/mcp/servers/route-srv",
            params={"sessionId": session.session_id, "scope": "user"},
        )
        assert removed.status_code == 200

        listing = await client.get("/api/mcp/servers", params={"sessionId": session.session_id})
        names = [server["name"] for server in listing.json()["servers"]]
        assert "route-srv" not in names


@pytest.mark.asyncio
async def test_mcp_route_duplicate_returns_409(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    from iac_code.web.app import create_app
    from iac_code.web.session_manager import WebSessionManager

    manager = WebSessionManager(projects_dir=tmp_path / "projects", cwd=tmp_path)
    session = manager.create_session(session_id="mcp-conflict")
    app = create_app(session_manager=manager)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        body = {
            "sessionId": session.session_id,
            "name": "dup-route",
            "scope": "user",
            "fields": {"transport": "stdio", "command": "npx"},
        }
        first = await client.post("/api/mcp/servers", json=body)
        assert first.status_code == 200, first.text
        second = await client.post("/api/mcp/servers", json=body)
        assert second.status_code == 409


# ---------------------------------------------------------------------------
# Prompt argument normalisation / JSON-serialisability
# ---------------------------------------------------------------------------


def test_normalize_prompt_arguments_from_sdk_objects() -> None:
    """A list of pydantic-like ``PromptArgument`` objects becomes plain dicts."""

    arguments = [
        SimpleNamespace(name="topic", description="subject", required=True),
        SimpleNamespace(name="tone", description=None, required=False),
    ]
    result = mcp_settings._normalize_prompt_arguments(arguments)
    assert result == [
        {"name": "topic", "description": "subject", "required": True},
        {"name": "tone", "description": None, "required": False},
    ]
    json.dumps(result)  # must not raise


def test_normalize_prompt_arguments_from_dict_mapping() -> None:
    """A ``name -> schema`` mapping is flattened, tolerating bare-string schemas."""

    arguments = {
        "topic": SimpleNamespace(description="subject", required=True),
        "flat": "just-a-string",
    }
    result = mcp_settings._normalize_prompt_arguments(arguments)
    assert {"name": "topic", "description": "subject", "required": True} in result
    assert {"name": "flat", "description": None, "required": False} in result
    json.dumps(result)


def test_normalize_prompt_arguments_from_plain_dicts() -> None:
    """Plain dict arguments pass through; nameless entries are dropped."""

    arguments = [
        {"name": "topic", "description": "d", "required": True},
        {"description": "no-name-here"},
    ]
    result = mcp_settings._normalize_prompt_arguments(arguments)
    assert result == [{"name": "topic", "description": "d", "required": True}]


def test_normalize_prompt_arguments_empty_and_none() -> None:
    assert mcp_settings._normalize_prompt_arguments(None) == []
    assert mcp_settings._normalize_prompt_arguments([]) == []
    assert mcp_settings._normalize_prompt_arguments({}) == []


def test_prompt_payload_is_json_serialisable() -> None:
    """Regression: a prompt carrying SDK argument objects must serialise cleanly.

    Previously ``_prompt_payload`` emitted raw ``PromptArgument`` objects, which
    made ``/api/mcp/capabilities`` raise ``TypeError: ... not JSON serializable``.
    """

    prompt = MCPPromptRecord(
        server_name="probe",
        prompt_name="greeting",
        public_name="greeting",
        description="A greeting",
        arguments=[SimpleNamespace(name="name", description="who", required=True)],
    )
    payload = mcp_settings._prompt_payload(prompt)
    assert payload["name"] == "greeting"
    assert payload["description"] == "A greeting"
    assert payload["arguments"] == [{"name": "name", "description": "who", "required": True}]
    json.dumps(payload)  # must not raise


# ---------------------------------------------------------------------------
# Capability snapshot (captured before disconnect wipes the record)
# ---------------------------------------------------------------------------


def test_connect_and_fetch_snapshots_before_disconnect(monkeypatch) -> None:
    """Capabilities must be captured while the connection is live.

    ``MCPManager.disconnect_all`` rebinds the record's tool/resource/prompt lists
    to empty; ``_connect_and_fetch`` disconnects in a ``finally`` before returning,
    so a naive implementation would hand back an already-wiped record.
    """

    scope_sentinel = object()  # identity is compared with ``is``
    scoped_config = SimpleNamespace(
        name="probe", scope=scope_sentinel, disabled=False, approved=True
    )
    record = SimpleNamespace(
        name="probe",
        scoped_config=SimpleNamespace(scope=scope_sentinel),
        tools=[
            SimpleNamespace(
                tool_name="echo", description="Echo back", input_schema={}, annotations={}
            )
        ],
        resources=[],
        prompts=[
            SimpleNamespace(
                prompt_name="greeting",
                description="Greet",
                arguments=[SimpleNamespace(name="name", description="who", required=True)],
            )
        ],
        capability_errors={},
    )

    class FakeManager:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def connect_all(self) -> None:
            pass

        def list_connections(self):
            return [record]

        async def disconnect_all(self) -> None:
            # Mirror the real manager: rebind capability lists to empty.
            record.tools = []
            record.resources = []
            record.prompts = []

    monkeypatch.setattr(mcp_settings, "MCPManager", FakeManager)
    monkeypatch.setattr(
        mcp_settings,
        "health_diagnostic_for_record",
        lambda rec: SimpleNamespace(
            connection_state="connected", auth_state="not-configured", failure_reason=None
        ),
    )

    snapshot = mcp_settings._connect_and_fetch(scoped_config, Path("/tmp/workspace"))

    assert snapshot is not None
    assert [tool["name"] for tool in snapshot.tools] == ["echo"]
    assert [prompt["name"] for prompt in snapshot.prompts] == ["greeting"]
    assert snapshot.prompts[0]["arguments"] == [
        {"name": "name", "description": "who", "required": True}
    ]
    assert snapshot.diagnostic.connection_state == "connected"
    # The live record really was wiped by disconnect_all — proving we snapshotted first.
    assert record.tools == []
    # And the whole snapshot survives JSON serialisation.
    json.dumps(
        {"tools": snapshot.tools, "resources": snapshot.resources, "prompts": snapshot.prompts}
    )


def test_connect_and_fetch_skips_disabled_server(monkeypatch) -> None:
    """A disabled or unapproved server returns None without connecting."""

    def _boom(*args, **kwargs):
        raise AssertionError("MCPManager must not be constructed for a disabled server")

    monkeypatch.setattr(mcp_settings, "MCPManager", _boom)
    scoped_config = SimpleNamespace(
        name="probe", scope=object(), disabled=True, approved=True
    )
    assert mcp_settings._connect_and_fetch(scoped_config, Path("/tmp/workspace")) is None
