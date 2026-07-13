import pytest

from iac_code.mcp.types import (
    MCPConfigError,
    MCPConfigScope,
    MCPConnectionState,
    MCPOAuthConfig,
    MCPPromptRecord,
    MCPResourceRecord,
    MCPServerConfig,
    MCPSkillRecord,
    MCPToolRecord,
    MCPTransport,
    ScopedMCPServerConfig,
)


def test_stdio_config_defaults_to_stdio_when_command_is_present() -> None:
    config = MCPServerConfig.from_mapping(
        "terraform",
        {
            "command": "uvx",
            "args": ["terraform-mcp-server"],
            "env": {"ALIBABA_CLOUD_REGION": "cn-hangzhou"},
        },
    )

    assert config.name == "terraform"
    assert config.transport == MCPTransport.STDIO
    assert config.command == "uvx"
    assert config.args == ("terraform-mcp-server",)
    assert config.env == {"ALIBABA_CLOUD_REGION": "cn-hangzhou"}
    assert config.content_signature().startswith("stdio:")
    assert "terraform-mcp-server" not in config.content_signature()
    changed_env = MCPServerConfig.from_mapping(
        "terraform",
        {
            "command": "uvx",
            "args": ["terraform-mcp-server"],
            "env": {"ALIBABA_CLOUD_REGION": "cn-shanghai"},
        },
    )
    assert changed_env.content_signature() != config.content_signature()


@pytest.mark.parametrize("transport", [MCPTransport.HTTP, MCPTransport.SSE])
def test_remote_config_parses_url_headers_and_oauth(transport: MCPTransport) -> None:
    config = MCPServerConfig.from_mapping(
        "remote",
        {
            "type": transport.value,
            "url": "https://example.com/mcp",
            "headers": {"X-Org": "iac"},
            "headersHelper": "python ./scripts/mcp_headers.py",
            "oauth": {
                "clientId": "client-id",
                "clientSecretEnv": "MCP_CLIENT_SECRET",
                "callbackPort": 3118,
                "authServerMetadataUrl": "https://example.com/.well-known/oauth-authorization-server",
                "clientMetadataUrl": "https://example.com/iac-code/oauth-client.json",
            },
        },
    )

    assert config.transport == transport
    assert config.url == "https://example.com/mcp"
    assert config.headers == {"X-Org": "iac"}
    assert config.headers_helper == "python ./scripts/mcp_headers.py"
    assert config.oauth == MCPOAuthConfig(
        client_id="client-id",
        client_secret_env="MCP_CLIENT_SECRET",
        callback_port=3118,
        auth_server_metadata_url="https://example.com/.well-known/oauth-authorization-server",
        client_metadata_url="https://example.com/iac-code/oauth-client.json",
    )
    assert config.content_signature().startswith("url:")
    assert "https://example.com/mcp" not in config.content_signature()
    changed_header = MCPServerConfig.from_mapping(
        "remote",
        {
            "type": transport.value,
            "url": "https://example.com/mcp",
            "headers": {"X-Org": "other"},
            "headersHelper": "python ./scripts/mcp_headers.py",
        },
    )
    changed_helper = MCPServerConfig.from_mapping(
        "remote",
        {
            "type": transport.value,
            "url": "https://example.com/mcp",
            "headers": {"X-Org": "iac"},
            "headersHelper": "python ./scripts/other_headers.py",
        },
    )
    assert changed_header.content_signature() != config.content_signature()
    assert changed_helper.content_signature() != config.content_signature()


@pytest.mark.parametrize("callback_port", [-1, 65536, 70000])
def test_remote_config_rejects_out_of_range_oauth_callback_port(callback_port: int) -> None:
    with pytest.raises(MCPConfigError, match=r"oauth\.callbackPort.*0 and 65535"):
        MCPServerConfig.from_mapping(
            "remote",
            {
                "type": "http",
                "url": "https://example.com/mcp",
                "oauth": {"callbackPort": callback_port},
            },
        )


@pytest.mark.parametrize("callback_port", [False, True])
def test_remote_config_rejects_boolean_oauth_callback_port(callback_port: bool) -> None:
    with pytest.raises(MCPConfigError, match=r"oauth\.callbackPort must be an integer"):
        MCPServerConfig.from_mapping(
            "remote",
            {
                "type": "http",
                "url": "https://example.com/mcp",
                "oauth": {"callbackPort": callback_port},
            },
        )


def test_remote_config_allows_zero_oauth_callback_port_for_ephemeral_port() -> None:
    config = MCPServerConfig.from_mapping(
        "remote",
        {
            "type": "http",
            "url": "https://example.com/mcp",
            "oauth": {"callbackPort": 0},
        },
    )

    assert config.oauth == MCPOAuthConfig(callback_port=0)


def test_oauth_client_metadata_url_affects_content_signature() -> None:
    config = MCPServerConfig.from_mapping(
        "remote",
        {
            "type": "http",
            "url": "https://example.com/mcp",
            "oauth": {"clientMetadataUrl": "https://example.com/clients/iac-code.json"},
        },
    )
    changed_client_metadata = MCPServerConfig.from_mapping(
        "remote",
        {
            "type": "http",
            "url": "https://example.com/mcp",
            "oauth": {"clientMetadataUrl": "https://example.com/clients/other.json"},
        },
    )

    assert changed_client_metadata.content_signature() != config.content_signature()


def test_websocket_config_parses_url_only() -> None:
    config = MCPServerConfig.from_mapping(
        "realtime",
        {
            "type": "ws",
            "url": "wss://example.com/mcp",
        },
    )

    assert config.transport == MCPTransport.WS
    assert config.url == "wss://example.com/mcp"
    assert config.headers == {}
    assert config.headers_helper is None
    assert config.oauth is None
    assert config.content_signature().startswith("url:")
    assert "wss://example.com/mcp" not in config.content_signature()


def test_websocket_config_accepts_local_ws_url() -> None:
    config = MCPServerConfig.from_mapping(
        "local-ws",
        {
            "type": "ws",
            "url": "ws://127.0.0.1:8765/mcp",
        },
    )

    assert config.transport == MCPTransport.WS
    assert config.url == "ws://127.0.0.1:8765/mcp"


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/mcp",
        "http://example.com/mcp",
        "example.com/mcp",
        "not-a-url",
        "ws://:8765/mcp",
        "ws://[::1/mcp",
    ],
)
def test_websocket_config_rejects_non_websocket_url_schemes(url: str) -> None:
    with pytest.raises(MCPConfigError, match="WebSocket.*ws:// or wss://.*host"):
        MCPServerConfig.from_mapping(
            "realtime",
            {
                "type": "ws",
                "url": url,
            },
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("headers", {"X-Org": "platform"}),
        ("headersHelper", "python ./headers.py"),
        ("oauth", {}),
    ],
)
def test_websocket_config_rejects_unsupported_connection_options(field: str, value: object) -> None:
    with pytest.raises(MCPConfigError, match=rf"WebSocket.*{field}.*not supported"):
        MCPServerConfig.from_mapping(
            "realtime",
            {
                "type": "ws",
                "url": "wss://example.com/mcp",
                field: value,
            },
        )


def test_stdio_config_rejects_headers_helper() -> None:
    with pytest.raises(MCPConfigError, match="headersHelper"):
        MCPServerConfig.from_mapping(
            "local",
            {
                "command": "uvx",
                "headersHelper": "python ./scripts/mcp_headers.py",
            },
        )


def test_remote_config_rejects_headers_helper_plaintext_secret() -> None:
    with pytest.raises(MCPConfigError, match="headersHelper"):
        MCPServerConfig.from_mapping(
            "remote",
            {
                "type": "http",
                "url": "https://example.com/mcp",
                "headersHelper": "python ./headers.py --token plain-secret",
            },
        )


def test_remote_config_rejects_headers_helper_mixed_env_and_plaintext_bearer_secret() -> None:
    with pytest.raises(MCPConfigError, match="headersHelper"):
        MCPServerConfig.from_mapping(
            "remote",
            {
                "type": "http",
                "url": "https://example.com/mcp",
                "headersHelper": "python ./headers.py 'Authorization: Bearer plain-secret' ${SAFE_VAR}",
            },
        )


def test_remote_config_rejects_headers_helper_sensitive_env_default() -> None:
    with pytest.raises(MCPConfigError, match="headersHelper"):
        MCPServerConfig.from_mapping(
            "remote",
            {
                "type": "http",
                "url": "https://example.com/mcp",
                "headersHelper": "python ./headers.py --token ${MCP_TOKEN:-plain-secret}",
            },
        )


def test_remote_config_allows_headers_helper_env_secret_reference() -> None:
    config = MCPServerConfig.from_mapping(
        "remote",
        {
            "type": "http",
            "url": "https://example.com/mcp",
            "headersHelper": "python ./headers.py --token ${MCP_TOKEN} 'Authorization: Bearer ${MCP_TOKEN}'",
        },
    )

    assert config.headers_helper == "python ./headers.py --token ${MCP_TOKEN} 'Authorization: Bearer ${MCP_TOKEN}'"


def test_content_signature_redacts_secret_like_values() -> None:
    config = MCPServerConfig.from_mapping(
        "remote",
        {
            "type": "http",
            "url": "https://example.com/mcp",
            "headers": {"Authorization": "Bearer token-a", "X-Org": "iac"},
            "env": {"API_TOKEN": "token-a", "TENANT": "a"},
            "oauth": {"clientId": "client-id", "clientSecretEnv": "MCP_CLIENT_SECRET_A"},
        },
    )
    changed_secret_values = MCPServerConfig.from_mapping(
        "remote",
        {
            "type": "http",
            "url": "https://example.com/mcp",
            "headers": {"Authorization": "Bearer token-b", "X-Org": "iac"},
            "env": {"API_TOKEN": "token-b", "TENANT": "a"},
            "oauth": {"clientId": "client-id", "clientSecretEnv": "MCP_CLIENT_SECRET_B"},
        },
    )
    changed_public_values = MCPServerConfig.from_mapping(
        "remote",
        {
            "type": "http",
            "url": "https://example.com/mcp",
            "headers": {"Authorization": "Bearer token-a", "X-Org": "other"},
            "env": {"API_TOKEN": "token-a", "TENANT": "b"},
            "oauth": {"clientId": "client-id", "clientSecretEnv": "MCP_CLIENT_SECRET_A"},
        },
    )

    assert changed_secret_values.content_signature() == config.content_signature()
    assert changed_public_values.content_signature() != config.content_signature()


def test_invalid_server_configs_fail_with_actionable_errors() -> None:
    with pytest.raises(MCPConfigError, match="Unsupported MCP transport"):
        MCPServerConfig.from_mapping("tcp", {"type": "tcp", "url": "tcp://example.com"})

    with pytest.raises(MCPConfigError, match="requires a command"):
        MCPServerConfig.from_mapping("stdio", {"type": "stdio"})

    with pytest.raises(MCPConfigError, match="requires a url"):
        MCPServerConfig.from_mapping("remote", {"type": "http"})

    with pytest.raises(MCPConfigError, match="oauth.clientSecret"):
        MCPServerConfig.from_mapping(
            "remote",
            {
                "type": "http",
                "url": "https://example.com/mcp",
                "oauth": {"clientSecret": "do-not-store-plaintext"},
            },
        )


def test_scopes_connection_states_and_precedence_labels_are_stable() -> None:
    assert [scope.value for scope in MCPConfigScope] == ["user", "project", "local", "session", "dynamic"]
    assert MCPConfigScope.SESSION.precedence > MCPConfigScope.LOCAL.precedence
    assert MCPConfigScope.LOCAL.precedence > MCPConfigScope.PROJECT.precedence
    assert MCPConfigScope.PROJECT.precedence > MCPConfigScope.USER.precedence
    assert MCPConfigScope.DYNAMIC.precedence == MCPConfigScope.SESSION.precedence

    assert [state.value for state in MCPConnectionState] == [
        "connected",
        "failed",
        "needs_auth",
        "pending",
        "disabled",
    ]


def test_scoped_config_keeps_source_metadata_and_defaults() -> None:
    server_config = MCPServerConfig.from_mapping("local", {"command": "python", "args": ["server.py"]})
    scoped = ScopedMCPServerConfig(config=server_config, scope=MCPConfigScope.LOCAL, source_path="/repo/.iac-code")

    assert scoped.name == "local"
    assert scoped.transport == MCPTransport.STDIO
    assert scoped.approved is True
    assert scoped.source_path == "/repo/.iac-code"
    assert scoped.warning is None


def test_discovery_records_preserve_server_origin_and_public_names() -> None:
    tool = MCPToolRecord(
        server_name="ros",
        tool_name="generate_template",
        public_name="mcp__ros__generate_template",
        description="Generate ROS templates",
        input_schema={"type": "object"},
        annotations={"readOnlyHint": True},
    )
    resource = MCPResourceRecord(
        server_name="ros",
        uri="skill://ros/vpc",
        name="vpc",
        title="VPC Skill",
        mime_type="text/markdown",
    )
    prompt = MCPPromptRecord(
        server_name="ros",
        prompt_name="review",
        public_name="mcp:ros:review",
        description="Review infrastructure",
        arguments={"template": {"required": True}},
    )
    skill = MCPSkillRecord(
        server_name="ros",
        name="vpc",
        public_name="mcp:ros:vpc",
        resource_uri="skill://ros/vpc",
        description="VPC best practices",
    )

    assert tool.public_name == "mcp__ros__generate_template"
    assert resource.is_skill_resource is True
    assert prompt.arguments["template"]["required"] is True
    assert skill.resource_uri == "skill://ros/vpc"
