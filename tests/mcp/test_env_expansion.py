from iac_code.mcp.env_expansion import expand_env


def test_expand_env_replaces_variables_and_defaults_inside_strings() -> None:
    expanded, warnings = expand_env(
        {
            "command": "${MCP_COMMAND}",
            "args": ["--region", "${REGION:-cn-hangzhou}", "--label=${ORG}-${ENV:-dev}"],
        },
        env={"MCP_COMMAND": "uvx", "ORG": "iac"},
        source="settings.yml",
        server_name="ros",
    )

    assert expanded == {
        "command": "uvx",
        "args": ["--region", "cn-hangzhou", "--label=iac-dev"],
    }
    assert warnings == []


def test_expand_env_walks_nested_lists_and_dicts_without_touching_non_strings() -> None:
    expanded, warnings = expand_env(
        {
            "headers": {
                "Authorization": "Bearer ${TOKEN}",
                "X-Enabled": True,
            },
            "oauth": {
                "callbackPort": 3118,
                "clientSecretEnv": "${SECRET_ENV_NAME}",
            },
        },
        env={"TOKEN": "secret-token", "SECRET_ENV_NAME": "MCP_SECRET"},
        source=".mcp.json",
        server_name="remote",
    )

    assert expanded == {
        "headers": {
            "Authorization": "Bearer secret-token",
            "X-Enabled": True,
        },
        "oauth": {
            "callbackPort": 3118,
            "clientSecretEnv": "MCP_SECRET",
        },
    }
    assert warnings == []


def test_missing_variables_are_preserved_and_reported() -> None:
    expanded, warnings = expand_env(
        {
            "env": {
                "API_KEY": "${MISSING_API_KEY}",
                "OPTIONAL": "${OPTIONAL:-fallback}",
            },
        },
        env={},
        source="/repo/.mcp.json",
        server_name="aliyun",
    )

    assert expanded == {
        "env": {
            "API_KEY": "${MISSING_API_KEY}",
            "OPTIONAL": "fallback",
        },
    }
    assert len(warnings) == 1
    assert warnings[0].source == "/repo/.mcp.json"
    assert warnings[0].server_name == "aliyun"
    assert warnings[0].code == "missing_env"
    assert "MISSING_API_KEY" in warnings[0].message
