import json
import shlex
import sys
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PureWindowsPath
from urllib.parse import parse_qs, urlencode, urlparse

import pytest
import typer
import yaml
from typer.testing import CliRunner

import iac_code.mcp.cli as mcp_cli
import iac_code.mcp.oauth as oauth_module
from iac_code.cli.main import app
from iac_code.mcp.config import (
    MCPConfigLoadResult,
    MCPPersistedServerMatch,
    approve_project_mcp_server,
    load_exact_mcp_config,
    load_mcp_configs,
)
from iac_code.mcp.manager import MCPConnectionRecord
from iac_code.mcp.oauth import oauth_storage_key, remember_oauth_storage_signature
from iac_code.mcp.storage import MCPSecretStorage
from iac_code.mcp.types import (
    MCPConfigScope,
    MCPConnectionMetadata,
    MCPConnectionState,
    MCPPromptRecord,
    MCPResourceRecord,
    MCPServerConfig,
    MCPToolRecord,
    ScopedMCPServerConfig,
)


@pytest.fixture(autouse=True)
def _disable_post_add_health_checks_by_default(monkeypatch) -> None:
    monkeypatch.setenv("IAC_CODE_MCP_ADD_HEALTH_CHECK", "0")


def test_mcp_add_list_get_and_remove_local_server(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    runner = CliRunner()

    result = runner.invoke(
        app,
        [
            "mcp",
            "add",
            "local-server",
            "--command",
            "uvx",
            "--arg",
            "server",
            "--env",
            "FOO=bar",
            "--scope",
            "local",
        ],
    )

    assert result.exit_code == 0, result.output
    local_settings = yaml.safe_load((tmp_path / ".iac-code" / "settings.local.yml").read_text(encoding="utf-8"))
    assert local_settings["mcpServers"]["local-server"] == {
        "command": "uvx",
        "args": ["server"],
        "env": {"FOO": "bar"},
    }

    listed = runner.invoke(app, ["mcp", "list", "--config-only"])
    assert listed.exit_code == 0
    assert "local-server" in listed.output
    assert "stdio" in listed.output

    fetched = runner.invoke(app, ["mcp", "get", "local-server", "--scope", "local", "--config-only"])
    assert fetched.exit_code == 0
    assert '"command": "uvx"' in fetched.output

    removed = runner.invoke(app, ["mcp", "remove", "local-server", "--scope", "local"])
    assert removed.exit_code == 0
    assert "Removed" in removed.output
    local_settings = yaml.safe_load((tmp_path / ".iac-code" / "settings.local.yml").read_text(encoding="utf-8"))
    assert "local-server" not in local_settings["mcpServers"]


def test_mcp_add_transport_http_positional_url_writes_user_config(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    runner = CliRunner()

    result = runner.invoke(
        app,
        [
            "mcp",
            "add",
            "--transport",
            "http",
            "yuque",
            "https://mcp.example.com/yuque/mcp",
            "--scope",
            "user",
        ],
    )

    assert result.exit_code == 0, result.output
    settings = yaml.safe_load((tmp_path / "config" / "settings.yml").read_text(encoding="utf-8"))
    assert settings["mcpServers"]["yuque"] == {
        "type": "http",
        "url": "https://mcp.example.com/yuque/mcp",
    }
    assert "to user config" in result.output
    assert "iac-code mcp get yuque --scope user --check" in result.output
    assert "iac-code mcp auth" not in result.output


@pytest.mark.parametrize("transport", ["http", "sse", "ws"])
def test_mcp_add_remote_rejects_unknown_option_like_url(monkeypatch, tmp_path: Path, transport: str) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))

    result = CliRunner().invoke(app, ["mcp", "add", "remote", "--type", transport, "--bogus"])

    assert result.exit_code != 0
    assert "Unknown MCP option '--bogus'" in result.output
    assert not (tmp_path / "config" / "settings.yml").exists()


def test_mcp_add_transport_sse_positional_url_writes_user_config(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    runner = CliRunner()

    result = runner.invoke(
        app,
        [
            "mcp",
            "add",
            "--transport",
            "sse",
            "events",
            "https://example.com/sse",
            "--scope",
            "user",
        ],
    )

    assert result.exit_code == 0, result.output
    settings = yaml.safe_load((tmp_path / "config" / "settings.yml").read_text(encoding="utf-8"))
    assert settings["mcpServers"]["events"] == {
        "type": "sse",
        "url": "https://example.com/sse",
    }
    assert "to user config" in result.output
    assert "iac-code mcp get events --scope user --check" in result.output
    assert "iac-code mcp auth" not in result.output


def test_mcp_add_transport_ws_positional_url_writes_user_config_with_health_hint(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    runner = CliRunner()

    result = runner.invoke(
        app,
        [
            "mcp",
            "add",
            "--transport",
            "ws",
            "realtime",
            "wss://example.com/mcp",
            "--scope",
            "user",
        ],
    )

    assert result.exit_code == 0, result.output
    settings = yaml.safe_load((tmp_path / "config" / "settings.yml").read_text(encoding="utf-8"))
    assert settings["mcpServers"]["realtime"] == {
        "type": "ws",
        "url": "wss://example.com/mcp",
    }
    assert "to user config" in result.output
    assert "iac-code mcp get realtime --scope user --check" in result.output
    assert "iac-code mcp auth" not in result.output


def test_mcp_add_help_lists_ws_transport() -> None:
    runner = CliRunner()

    result = runner.invoke(app, ["mcp", "add", "--help"], env={"COLUMNS": "160"}, terminal_width=160)

    assert result.exit_code == 0, result.output
    click_app = typer.main.get_command(app)
    add_command = click_app.commands["mcp"].commands["add"]
    transport_option = next(param for param in add_command.params if "--transport" in param.opts)
    assert transport_option.help == "Transport type: stdio, http, sse, ws."


def test_mcp_add_positional_stdio_command_passthrough_after_double_dash(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    runner = CliRunner()

    result = runner.invoke(
        app,
        [
            "mcp",
            "add",
            "yuque-remote",
            "--scope",
            "user",
            "--",
            "npx",
            "mcp-remote",
            "https://mcp.example.com/yuque/mcp",
        ],
    )

    assert result.exit_code == 0, result.output
    settings = yaml.safe_load((tmp_path / "config" / "settings.yml").read_text(encoding="utf-8"))
    assert settings["mcpServers"]["yuque-remote"] == {
        "command": "npx",
        "args": ["mcp-remote", "https://mcp.example.com/yuque/mcp"],
    }
    assert "to user config" in result.output
    assert "iac-code mcp auth" not in result.output


def test_mcp_add_positional_stdio_command_passthrough_preserves_command_flags(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    runner = CliRunner()

    result = runner.invoke(
        app,
        [
            "mcp",
            "add",
            "server",
            "--scope",
            "user",
            "--",
            "npx",
            "--yes",
            "mcp-server",
        ],
    )

    assert result.exit_code == 0, result.output
    settings = yaml.safe_load((tmp_path / "config" / "settings.yml").read_text(encoding="utf-8"))
    assert settings["mcpServers"]["server"] == {
        "command": "npx",
        "args": ["--yes", "mcp-server"],
    }


def test_mcp_add_unknown_option_typo_before_command_fails_without_writing(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    runner = CliRunner()

    result = runner.invoke(
        app,
        [
            "mcp",
            "add",
            "typo",
            "--trasnport",
            "http",
            "https://example.com/mcp",
            "--scope",
            "user",
        ],
    )

    assert result.exit_code != 0
    assert "Unknown MCP option" in result.output
    assert "--trasnport" in result.output
    assert not (tmp_path / "config" / "settings.yml").exists()


def test_mcp_add_command_rejects_unknown_option_typo_without_writing(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    runner = CliRunner()

    result = runner.invoke(
        app,
        [
            "mcp",
            "add",
            "typo",
            "--command",
            "uvx",
            "--trasnport",
            "http",
            "--scope",
            "user",
        ],
    )

    assert result.exit_code != 0
    assert "Unknown MCP option" in result.output
    assert "--trasnport" in result.output
    assert not (tmp_path / "config" / "settings.yml").exists()


def test_mcp_add_positional_url_without_transport_warns_and_stores_stdio(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    runner = CliRunner()

    result = runner.invoke(
        app,
        [
            "mcp",
            "add",
            "remote-ish",
            "https://example.com/mcp",
            "--scope",
            "user",
        ],
    )

    assert result.exit_code == 0, result.output
    settings = yaml.safe_load((tmp_path / "config" / "settings.yml").read_text(encoding="utf-8"))
    assert settings["mcpServers"]["remote-ish"] == {"command": "https://example.com/mcp"}
    assert "Warning" in result.output
    assert "--transport http" in result.output
    assert "--transport ws" in result.output


def test_mcp_add_url_like_endpoint_without_transport_warns_and_stores_stdio(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    runner = CliRunner()

    for index, endpoint in enumerate(("localhost:3000/mcp", "example.com/mcp", "tools.example/sse")):
        result = runner.invoke(
            app,
            [
                "mcp",
                "add",
                f"remote-ish-{index}",
                endpoint,
                "--scope",
                "user",
            ],
        )

        assert result.exit_code == 0, result.output
        settings = yaml.safe_load((tmp_path / "config" / "settings.yml").read_text(encoding="utf-8"))
        assert settings["mcpServers"][f"remote-ish-{index}"] == {"command": endpoint}
        assert "Warning" in result.output
        assert "--transport http" in result.output
        assert "--transport sse" in result.output


def test_mcp_add_header_equals_format_normalizes_to_headers(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    runner = CliRunner()

    result = runner.invoke(
        app,
        [
            "mcp",
            "add",
            "--transport",
            "http",
            "remote",
            "https://example.com/mcp",
            "--header",
            "X-Org=platform",
            "--scope",
            "user",
        ],
    )

    assert result.exit_code == 0, result.output
    settings = yaml.safe_load((tmp_path / "config" / "settings.yml").read_text(encoding="utf-8"))
    assert settings["mcpServers"]["remote"]["headers"] == {"X-Org": "platform"}


def test_mcp_add_header_colon_format_normalizes_to_headers(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    runner = CliRunner()

    result = runner.invoke(
        app,
        [
            "mcp",
            "add",
            "--type",
            "http",
            "--url",
            "https://example.com/mcp",
            "remote",
            "--header",
            "X-Org: platform",
            "--scope",
            "user",
        ],
    )

    assert result.exit_code == 0, result.output
    settings = yaml.safe_load((tmp_path / "config" / "settings.yml").read_text(encoding="utf-8"))
    assert settings["mcpServers"]["remote"]["headers"] == {"X-Org": "platform"}


def test_mcp_add_header_colon_value_with_equals_preserves_header_name(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    runner = CliRunner()

    result = runner.invoke(
        app,
        [
            "mcp",
            "add",
            "--type",
            "http",
            "--url",
            "https://example.com/mcp",
            "remote",
            "--header",
            "X-Thing: a=b",
            "--scope",
            "user",
        ],
    )

    assert result.exit_code == 0, result.output
    settings = yaml.safe_load((tmp_path / "config" / "settings.yml").read_text(encoding="utf-8"))
    assert settings["mcpServers"]["remote"]["headers"] == {"X-Thing": "a=b"}


def test_mcp_add_header_colon_rejects_plaintext_secret(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    runner = CliRunner()

    result = runner.invoke(
        app,
        [
            "mcp",
            "add",
            "--transport",
            "http",
            "remote",
            "https://example.com/mcp",
            "--header",
            "Authorization: Bearer token",
            "--scope",
            "user",
        ],
    )

    assert result.exit_code != 0
    assert "environment variable reference" in result.output
    assert "Bearer token" not in result.output
    assert not (tmp_path / "config" / "settings.yml").exists()


def test_mcp_add_header_colon_secret_with_equals_rejects_without_leaking_value(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    runner = CliRunner()

    result = runner.invoke(
        app,
        [
            "mcp",
            "add",
            "--transport",
            "http",
            "remote",
            "https://example.com/mcp",
            "--header",
            "Authorization: Bearer abc=",
            "--scope",
            "user",
        ],
    )

    assert result.exit_code != 0
    assert "environment variable reference" in result.output
    assert "Bearer abc" not in result.output
    assert "abc" not in result.output
    assert not (tmp_path / "config" / "settings.yml").exists()


def test_mcp_add_rejects_plaintext_cookie_header_without_leaking_value(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    runner = CliRunner()

    result = runner.invoke(
        app,
        [
            "mcp",
            "add",
            "--transport",
            "http",
            "remote",
            "https://example.com/mcp",
            "--header",
            "Cookie: session=plain-secret",
            "--scope",
            "user",
        ],
    )

    assert result.exit_code != 0
    assert "environment variable reference" in result.output
    assert "plain-secret" not in result.output
    assert not (tmp_path / "config" / "settings.yml").exists()


def test_mcp_add_rejects_secret_like_header_value_under_non_sensitive_key(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    runner = CliRunner()

    result = runner.invoke(
        app,
        [
            "mcp",
            "add",
            "--transport",
            "http",
            "remote",
            "https://example.com/mcp",
            "--header",
            "X-Trace: Bearer plain-token",
            "--scope",
            "user",
        ],
    )

    assert result.exit_code != 0
    assert "environment variable reference" in result.output
    assert "plain-token" not in result.output
    assert not (tmp_path / "config" / "settings.yml").exists()


def test_mcp_add_rejects_secret_like_env_value_under_non_sensitive_key(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    runner = CliRunner()

    result = runner.invoke(
        app,
        [
            "mcp",
            "add",
            "local",
            "--command",
            "uvx",
            "--env",
            "TRACE=Bearer plain-token",
            "--scope",
            "user",
        ],
    )

    assert result.exit_code != 0
    assert "environment variable reference" in result.output
    assert "plain-token" not in result.output
    assert not (tmp_path / "config" / "settings.yml").exists()


def test_mcp_add_header_colon_accepts_env_reference_and_get_redacts_it(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    runner = CliRunner()

    result = runner.invoke(
        app,
        [
            "mcp",
            "add",
            "--transport",
            "http",
            "remote",
            "https://example.com/mcp",
            "--header",
            "Authorization: Bearer ${YUQUE_MCP_TOKEN}",
            "--scope",
            "user",
        ],
    )

    assert result.exit_code == 0, result.output
    settings = yaml.safe_load((tmp_path / "config" / "settings.yml").read_text(encoding="utf-8"))
    assert settings["mcpServers"]["remote"]["headers"] == {"Authorization": "Bearer ${YUQUE_MCP_TOKEN}"}

    fetched = runner.invoke(app, ["mcp", "get", "remote", "--scope", "user", "--config-only"])

    assert fetched.exit_code == 0, fetched.output
    assert "YUQUE_MCP_TOKEN" not in fetched.output
    assert '"Authorization": "[redacted]"' in fetched.output


def test_mcp_add_remote_oauth_success_shows_auth_guidance(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    runner = CliRunner()

    result = runner.invoke(
        app,
        [
            "mcp",
            "add",
            "remote",
            "--type",
            "http",
            "--url",
            "https://example.com/mcp",
            "--client-id",
            "client-id",
            "--scope",
            "user",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "iac-code mcp get remote --scope user --check" in result.output
    assert "iac-code mcp auth remote --scope user" in result.output


def test_mcp_add_remote_needs_auth_health_check_shows_auth_guidance(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("IAC_CODE_MCP_ADD_HEALTH_CHECK", "1")
    seen: dict[str, object] = {}

    class NeedsAuthHealthManager:
        def __init__(self, checked: list[ScopedMCPServerConfig]) -> None:
            seen["configs"] = checked
            self.connected = False
            self.disconnected = False

        async def connect_all(self) -> None:
            self.connected = True
            seen["connected"] = True

        async def disconnect_all(self) -> None:
            self.disconnected = True
            seen["disconnected"] = True

        def list_connections(self) -> list[MCPConnectionRecord]:
            checked = seen["configs"]
            assert isinstance(checked, list)
            return [
                MCPConnectionRecord(
                    scoped_config=checked[0],
                    state=MCPConnectionState.NEEDS_AUTH,
                    error="authentication required",
                )
            ]

    monkeypatch.setattr(
        mcp_cli,
        "_create_health_check_manager",
        lambda checked, *, roots: NeedsAuthHealthManager(checked),
    )

    result = CliRunner().invoke(
        app,
        [
            "mcp",
            "add",
            "--transport",
            "http",
            "remote",
            "https://example.com/mcp",
            "--scope",
            "user",
        ],
    )

    assert result.exit_code == 0, result.output
    assert seen["connected"] is True
    assert seen["disconnected"] is True
    assert "iac-code mcp get remote --scope user --check" in result.output
    assert "iac-code mcp auth remote --scope user" in result.output


def test_mcp_add_remote_health_check_failure_keeps_success_without_leaking(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("IAC_CODE_MCP_ADD_HEALTH_CHECK", "1")

    class FailingHealthManager:
        async def connect_all(self) -> None:
            raise RuntimeError("connect failed with access_token=super-secret-token")

        async def disconnect_all(self) -> None:
            return None

        def list_connections(self) -> list[MCPConnectionRecord]:
            return []

    monkeypatch.setattr(mcp_cli, "_create_health_check_manager", lambda checked, *, roots: FailingHealthManager())

    result = CliRunner().invoke(
        app,
        [
            "mcp",
            "add",
            "--transport",
            "http",
            "remote",
            "https://example.com/mcp",
            "--scope",
            "user",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "Added MCP server 'remote' to user config." in result.output
    assert "iac-code mcp get remote --scope user --check" in result.output
    assert "iac-code mcp auth remote --scope user" not in result.output
    assert "super-secret-token" not in result.output
    assert "access_token" not in result.output
    assert "Traceback" not in result.output


def test_mcp_add_stdio_does_not_run_post_add_health_check(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("IAC_CODE_MCP_ADD_HEALTH_CHECK", "1")

    def fail_if_checked(*args, **kwargs):
        raise AssertionError("stdio add must not run health checks")

    monkeypatch.setattr(mcp_cli, "_create_health_check_manager", fail_if_checked)

    result = CliRunner().invoke(
        app,
        [
            "mcp",
            "add",
            "local-server",
            "--command",
            "uvx",
            "--scope",
            "user",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "iac-code mcp get" not in result.output
    assert "iac-code mcp auth" not in result.output


def test_mcp_add_remote_with_explicit_oauth_does_not_run_post_add_health_check(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("IAC_CODE_MCP_ADD_HEALTH_CHECK", "1")

    def fail_if_checked(*args, **kwargs):
        raise AssertionError("explicit OAuth add already has auth guidance")

    monkeypatch.setattr(mcp_cli, "_create_health_check_manager", fail_if_checked)

    result = CliRunner().invoke(
        app,
        [
            "mcp",
            "add",
            "--transport",
            "http",
            "remote",
            "https://example.com/mcp",
            "--client-id",
            "client-id",
            "--scope",
            "user",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "iac-code mcp get remote --scope user --check" in result.output
    assert "iac-code mcp auth remote --scope user" in result.output


def test_mcp_add_ws_does_not_run_post_add_health_check(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("IAC_CODE_MCP_ADD_HEALTH_CHECK", "1")

    def fail_if_checked(*args, **kwargs):
        raise AssertionError("ws add must not run OAuth health guidance checks")

    monkeypatch.setattr(mcp_cli, "_create_health_check_manager", fail_if_checked)

    result = CliRunner().invoke(
        app,
        [
            "mcp",
            "add",
            "--transport",
            "ws",
            "remote",
            "ws://example.com/mcp",
            "--scope",
            "user",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "iac-code mcp get remote --scope user --check" in result.output
    assert "iac-code mcp auth remote --scope user" not in result.output


def test_mcp_add_json_validates_and_does_not_write_plaintext_client_secret(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    runner = CliRunner()

    result = runner.invoke(
        app,
        [
            "mcp",
            "add-json",
            "remote",
            json.dumps(
                {
                    "type": "http",
                    "url": "https://example.com/mcp",
                    "oauth": {"clientId": "client-id", "clientSecretEnv": "MCP_SECRET"},
                }
            ),
            "--scope",
            "user",
        ],
    )

    assert result.exit_code == 0, result.output
    settings = yaml.safe_load((tmp_path / "config" / "settings.yml").read_text(encoding="utf-8"))
    assert settings["mcpServers"]["remote"]["oauth"] == {
        "clientId": "client-id",
        "clientSecretEnv": "MCP_SECRET",
    }

    bad = runner.invoke(
        app,
        [
            "mcp",
            "add-json",
            "bad",
            json.dumps(
                {
                    "type": "http",
                    "url": "https://example.com/mcp",
                    "oauth": {"clientSecret": "super-secret"},
                }
            ),
            "--scope",
            "user",
        ],
    )

    assert bad.exit_code != 0
    assert "oauth.clientSecret" in bad.output
    assert "super-secret" not in (tmp_path / "config" / "settings.yml").read_text(encoding="utf-8")


def test_mcp_add_json_rejects_unsupported_transport_without_writing(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    runner = CliRunner()

    result = runner.invoke(
        app,
        [
            "mcp",
            "add-json",
            "websocket",
            json.dumps({"type": "tcp", "url": "tcp://example.com/mcp"}),
            "--scope",
            "user",
        ],
    )

    assert result.exit_code != 0
    assert "Unsupported MCP transport" in result.output
    assert not (tmp_path / "config" / "settings.yml").exists()


def test_mcp_add_json_rejects_out_of_range_oauth_callback_port_without_writing(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    runner = CliRunner()

    result = runner.invoke(
        app,
        [
            "mcp",
            "add-json",
            "remote",
            json.dumps(
                {
                    "type": "http",
                    "url": "https://example.com/mcp",
                    "oauth": {"callbackPort": 70000},
                }
            ),
            "--scope",
            "user",
        ],
    )

    assert result.exit_code != 0
    assert "oauth.callbackPort" in result.output
    assert "0 and 65535" in result.output
    assert not (tmp_path / "config" / "settings.yml").exists()


def test_mcp_get_redacts_secret_like_values(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    runner = CliRunner()
    settings_path = tmp_path / "config" / "settings.yml"
    settings_path.parent.mkdir(parents=True)
    settings_path.write_text(
        yaml.safe_dump(
            {
                "mcpServers": {
                    "remote": {
                        "type": "http",
                        "url": "https://example.com/mcp",
                        "headers": {"Authorization": "Bearer token", "X-Org": "org"},
                        "env": {"API_TOKEN": "secret"},
                    }
                }
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )

    fetched = runner.invoke(app, ["mcp", "get", "remote", "--scope", "user", "--config-only"])

    assert fetched.exit_code == 0, fetched.output
    assert "Bearer token" not in fetched.output
    assert '"Authorization": "[redacted]"' in fetched.output
    assert '"X-Org": "org"' in fetched.output


def test_mcp_get_redacts_secret_like_plain_output_values(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    runner = CliRunner()
    settings_path = tmp_path / "config" / "settings.yml"
    settings_path.parent.mkdir(parents=True)
    settings_path.write_text(
        yaml.safe_dump(
            {
                "mcpServers": {
                    "remote": {
                        "type": "http",
                        "url": "https://example.com/mcp",
                        "headers": {
                            "Cookie": "session=plain-secret",
                            "X-Trace": "Bearer trace-token",
                            "X-Org": "platform",
                        },
                        "env": {"MCP_SESSION": "session=env-secret", "PLAIN": "visible"},
                    }
                }
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )

    fetched = runner.invoke(app, ["mcp", "get", "remote", "--scope", "user", "--config-only"])

    assert fetched.exit_code == 0, fetched.output
    assert "plain-secret" not in fetched.output
    assert "trace-token" not in fetched.output
    assert "env-secret" not in fetched.output
    assert '"Cookie": "[redacted]"' in fetched.output
    assert '"X-Trace": "[redacted]"' in fetched.output
    assert '"MCP_SESSION": "[redacted]"' in fetched.output
    assert '"X-Org": "platform"' in fetched.output
    assert '"PLAIN": "visible"' in fetched.output


def test_mcp_get_redacts_headers_helper_command(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    settings_path = tmp_path / "config" / "settings.yml"
    settings_path.parent.mkdir(parents=True)
    settings_path.write_text(
        yaml.safe_dump(
            {
                "mcpServers": {
                    "remote": {
                        "type": "http",
                        "url": "https://example.com/mcp",
                        "headersHelper": "python helper.py --token plain-secret",
                    }
                }
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )

    fetched = CliRunner().invoke(app, ["mcp", "get", "remote", "--scope", "user", "--config-only"])

    assert fetched.exit_code == 0, fetched.output
    assert "plain-secret" not in fetched.output
    assert '"headersHelper": "[redacted]"' in fetched.output


def test_mcp_add_json_rejects_headers_helper_plaintext_secret(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    config = {
        "type": "http",
        "url": "https://example.com/mcp",
        "headersHelper": "python helper.py --client-secret plain-secret",
    }

    result = CliRunner().invoke(app, ["mcp", "add-json", "remote", json.dumps(config), "--scope", "user"])

    assert result.exit_code != 0
    assert "headersHelper" in result.output
    assert "plain-secret" not in result.output
    assert not (tmp_path / "config" / "settings.yml").exists()


def test_mcp_remove_deletes_malformed_persisted_entry(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    settings_path = tmp_path / "config" / "settings.yml"
    settings_path.parent.mkdir(parents=True)
    settings_path.write_text(yaml.safe_dump({"mcpServers": {"bad": "not-an-object"}}), encoding="utf-8")

    removed = CliRunner().invoke(app, ["mcp", "remove", "bad", "--scope", "user"])

    assert removed.exit_code == 0, removed.output
    assert "Removed MCP server 'bad'" in removed.output
    settings = yaml.safe_load(settings_path.read_text(encoding="utf-8"))
    assert "bad" not in settings["mcpServers"]


def test_mcp_remove_clears_disabled_state_for_readded_same_config(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    runner = CliRunner()
    add_args = [
        "mcp",
        "add",
        "remote",
        "--type",
        "http",
        "--url",
        "https://example.com/mcp",
        "--scope",
        "user",
    ]

    added = runner.invoke(app, add_args)
    disabled = runner.invoke(app, ["mcp", "disable", "remote", "--scope", "user"])
    removed = runner.invoke(app, ["mcp", "remove", "remote", "--scope", "user"])
    readded = runner.invoke(app, add_args)

    assert added.exit_code == 0, added.output
    assert disabled.exit_code == 0, disabled.output
    assert removed.exit_code == 0, removed.output
    assert readded.exit_code == 0, readded.output
    load_result = load_mcp_configs(cwd=tmp_path, env={})
    assert len(load_result.servers) == 1
    assert load_result.servers[0].name == "remote"
    assert load_result.servers[0].disabled is False


def test_mcp_disable_missing_env_server_and_loading_disabled_server_does_not_warn(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    settings_path = tmp_path / "config" / "settings.yml"
    settings_path.parent.mkdir(parents=True)
    settings_path.write_text(
        yaml.safe_dump(
            {
                "mcpServers": {
                    "remote": {
                        "type": "http",
                        "url": "https://example.com/mcp",
                        "headers": {"Authorization": "Bearer ${MISSING_TOKEN}"},
                    }
                }
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )

    disabled = CliRunner().invoke(app, ["mcp", "disable", "remote", "--scope", "user"])

    assert disabled.exit_code == 0, disabled.output
    assert "Disabled MCP server 'remote'." in disabled.output
    load_result = load_mcp_configs(cwd=tmp_path, env={}, include_pending_project=True)
    assert [server.name for server in load_result.servers] == ["remote"]
    assert load_result.servers[0].disabled is True
    assert [warning.code for warning in load_result.warnings] == []


def test_mcp_disable_invalid_config_server_and_loading_disabled_server_does_not_warn(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    settings_path = tmp_path / "config" / "settings.yml"
    settings_path.parent.mkdir(parents=True)
    settings_path.write_text(
        yaml.safe_dump({"mcpServers": {"bad": {"type": "http"}}}, sort_keys=True),
        encoding="utf-8",
    )

    disabled = CliRunner().invoke(app, ["mcp", "disable", "bad", "--scope", "user"])

    assert disabled.exit_code == 0, disabled.output
    assert "Disabled MCP server 'bad'." in disabled.output
    load_result = load_mcp_configs(cwd=tmp_path, env={}, include_pending_project=True)
    assert [server.name for server in load_result.servers] == ["bad"]
    assert load_result.servers[0].disabled is True
    assert load_result.servers[0].transport.value == "http"
    assert [warning.code for warning in load_result.warnings] == []


def test_mcp_disable_scalar_invalid_config_server_and_loading_disabled_server_does_not_warn(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    settings_path = tmp_path / "config" / "settings.yml"
    settings_path.parent.mkdir(parents=True)
    settings_path.write_text(yaml.safe_dump({"mcpServers": {"bad": "not-an-object"}}, sort_keys=True), encoding="utf-8")

    disabled = CliRunner().invoke(app, ["mcp", "disable", "bad", "--scope", "user"])

    assert disabled.exit_code == 0, disabled.output
    assert "Disabled MCP server 'bad'." in disabled.output
    load_result = load_mcp_configs(cwd=tmp_path, env={}, include_pending_project=True)
    assert [server.name for server in load_result.servers] == ["bad"]
    assert load_result.servers[0].disabled is True
    assert [warning.code for warning in load_result.warnings] == []


def test_mcp_enable_scalar_invalid_config_server_removes_disabled_state(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    settings_path = tmp_path / "config" / "settings.yml"
    settings_path.parent.mkdir(parents=True)
    settings_path.write_text(yaml.safe_dump({"mcpServers": {"bad": "not-an-object"}}, sort_keys=True), encoding="utf-8")
    runner = CliRunner()

    disabled = runner.invoke(app, ["mcp", "disable", "bad", "--scope", "user"])
    enabled = runner.invoke(app, ["mcp", "enable", "bad", "--scope", "user"])

    assert disabled.exit_code == 0, disabled.output
    assert enabled.exit_code == 0, enabled.output
    assert "Enabled MCP server 'bad'." in enabled.output
    load_result = load_mcp_configs(cwd=tmp_path, env={}, include_pending_project=True)
    assert load_result.servers == []
    assert [(warning.server_name, warning.code) for warning in load_result.warnings] == [("bad", "invalid_config")]


def test_mcp_list_check_reports_health_states_without_real_connections(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    connected = _scoped_mcp_config("connected", {"type": "http", "url": "https://connected.example/mcp"})
    needs_auth = _scoped_mcp_config("needs-auth", {"type": "http", "url": "https://auth.example/mcp"})
    failed = _scoped_mcp_config("failed", {"command": "broken"})
    skipped = _scoped_mcp_config("skipped", {"command": "unchecked"})
    pending = _scoped_mcp_config("pending", {"command": "uvx"}, scope=MCPConfigScope.PROJECT, approved=False)
    configs = [connected, needs_auth, failed, pending, skipped]
    manager = _FakeHealthManager(
        [
            _record_with_refresh(
                MCPConnectionRecord(
                    scoped_config=connected,
                    state=MCPConnectionState.CONNECTED,
                    tools=[
                        MCPToolRecord(
                            server_name="connected",
                            tool_name="plan",
                            public_name="mcp__connected__plan",
                        )
                    ],
                    resources=[MCPResourceRecord(server_name="connected", uri="resource://connected/template")],
                    prompts=[MCPPromptRecord(server_name="connected", prompt_name="review", public_name="review")],
                ),
                kind="tools",
                refreshed_at=123.456,
                failure_reason=None,
            ),
            MCPConnectionRecord(
                scoped_config=needs_auth,
                state=MCPConnectionState.NEEDS_AUTH,
                error="authentication required",
                auth_error="invalid_token",
            ),
            _record_with_refresh(
                MCPConnectionRecord(
                    scoped_config=failed,
                    state=MCPConnectionState.FAILED,
                    error="connect failed with access_token=super-secret-token",
                ),
                kind="resources",
                refreshed_at=124.0,
                failure_reason="resource refresh failed with access_token=super-secret-token",
            ),
        ]
    )

    monkeypatch.setattr(
        mcp_cli,
        "load_mcp_configs",
        lambda **_: MCPConfigLoadResult(servers=configs, warnings=[], pending=[pending]),
    )
    monkeypatch.setattr(mcp_cli, "_create_health_check_manager", lambda checked, *, roots: manager)

    result = CliRunner().invoke(app, ["mcp", "list", "--check"])

    assert result.exit_code == 0, result.output
    assert manager.connected is True
    assert manager.disconnected is True
    assert result.output.splitlines() == [
        (
            "name\tscope\ttransport\tapproval_state\tauth_state\tconnection_state\ttools\tresources\tprompts\t"
            "latest_failure\trefresh_kind\trefresh_time\trefresh_failure"
        ),
        "connected\tuser\thttp\tapproved\tnot-configured\tconnected\t1\t1\t1\t-\ttools\t123.456\t-",
        ("needs-auth\tuser\thttp\tapproved\tneeds-auth\tneeds-auth\t0\t0\t0\tauthentication required\t-\t-\t-"),
        (
            "failed\tuser\tstdio\tapproved\tnot-configured\tfailed\t0\t0\t0\t"
            "connect failed with access_token=[REDACTED]\t"
            "resources\t124.0\tresource refresh failed with access_token=[REDACTED]"
        ),
        (
            "pending\tproject\tstdio\tpending-approval\tnot-configured\tpending-approval\t-\t-\t-\t"
            "Project MCP server pending approval.\t-\t-\t-"
        ),
        "skipped\tuser\tstdio\tapproved\tnot-configured\tskipped\t-\t-\t-\t-\t-\t-\t-",
    ]


def test_mcp_list_check_keeps_same_name_scope_diagnostics_separate(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    user = _scoped_mcp_config(
        "shared",
        {"type": "http", "url": "https://user.example/mcp"},
        scope=MCPConfigScope.USER,
    )
    local = _scoped_mcp_config(
        "shared",
        {"type": "http", "url": "https://local.example/mcp"},
        scope=MCPConfigScope.LOCAL,
    )

    class SameNameHealthManager:
        async def connect_all(self) -> None:
            return None

        def list_connections(self) -> list[MCPConnectionRecord]:
            return [
                MCPConnectionRecord(scoped_config=user, state=MCPConnectionState.NEEDS_AUTH, error="user auth"),
                MCPConnectionRecord(scoped_config=local, state=MCPConnectionState.CONNECTED),
            ]

        async def disconnect_all(self) -> None:
            return None

    monkeypatch.setattr(
        mcp_cli,
        "load_mcp_configs",
        lambda **_: MCPConfigLoadResult(servers=[user, local], warnings=[], pending=[]),
    )
    monkeypatch.setattr(mcp_cli, "_create_health_check_manager", lambda checked, *, roots: SameNameHealthManager())

    result = CliRunner().invoke(app, ["mcp", "list", "--check"])

    assert result.exit_code == 0, result.output
    assert "shared\tuser\thttp\tapproved\tneeds-auth\tneeds-auth" in result.output
    assert "shared\tlocal\thttp\tapproved\tnot-configured\tconnected" in result.output


def test_mcp_list_plain_does_not_create_health_manager(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    server = _scoped_mcp_config("fast", {"command": "uvx"})

    monkeypatch.setattr(
        mcp_cli,
        "load_mcp_configs",
        lambda **_: MCPConfigLoadResult(servers=[server], warnings=[], pending=[]),
    )

    def fail_if_checked(*args, **kwargs):
        raise AssertionError("plain mcp list must not create a health manager")

    monkeypatch.setattr(mcp_cli, "_create_health_check_manager", fail_if_checked)

    result = CliRunner().invoke(app, ["mcp", "list"])

    assert result.exit_code == 0, result.output
    assert result.output == "fast\tuser\tstdio\tapproved\n"


def test_mcp_list_config_only_does_not_create_health_manager(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    server = _scoped_mcp_config("fast", {"command": "uvx"})

    monkeypatch.setattr(
        mcp_cli,
        "load_mcp_configs",
        lambda **_: MCPConfigLoadResult(servers=[server], warnings=[], pending=[]),
    )

    def fail_if_checked(*args, **kwargs):
        raise AssertionError("mcp list --config-only must not create a health manager")

    monkeypatch.setattr(mcp_cli, "_create_health_check_manager", fail_if_checked)

    result = CliRunner().invoke(app, ["mcp", "list", "--config-only"])

    assert result.exit_code == 0, result.output
    assert result.output == "fast\tuser\tstdio\tapproved\n"


def test_mcp_list_config_only_lists_missing_env_persisted_server(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    settings_path = tmp_path / "config" / "settings.yml"
    settings_path.parent.mkdir(parents=True)
    settings_path.write_text(
        yaml.safe_dump(
            {
                "mcpServers": {
                    "remote": {
                        "type": "http",
                        "url": "https://example.com/mcp",
                        "headers": {"Authorization": "Bearer ${MISSING_TOKEN}"},
                    }
                }
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )

    def fail_if_checked(*args, **kwargs):
        raise AssertionError("mcp list --config-only must not create a health manager")

    monkeypatch.setattr(mcp_cli, "_create_health_check_manager", fail_if_checked)

    result = CliRunner().invoke(app, ["mcp", "list", "--config-only"])

    assert result.exit_code == 0, result.output
    assert result.output == "remote\tuser\thttp\tmissing-env\n"


def test_mcp_list_check_lists_missing_env_persisted_server_without_connecting(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    settings_path = tmp_path / "config" / "settings.yml"
    settings_path.parent.mkdir(parents=True)
    settings_path.write_text(
        yaml.safe_dump(
            {
                "mcpServers": {
                    "remote": {
                        "type": "http",
                        "url": "https://example.com/mcp",
                        "headers": {"Authorization": "Bearer ${MISSING_TOKEN}"},
                    }
                }
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )

    def fail_if_checked(*args, **kwargs):
        raise AssertionError("missing-env MCP config must not create a health manager")

    monkeypatch.setattr(mcp_cli, "_create_health_check_manager", fail_if_checked)

    result = CliRunner().invoke(app, ["mcp", "list", "--check"])

    assert result.exit_code == 0, result.output
    assert (
        "remote\tuser\thttp\tapproved\tnot-configured\tmissing-env\t-\t-\t-\t"
        "Environment variable 'MISSING_TOKEN' is not set"
    ) in result.output


def test_mcp_list_check_lists_invalid_persisted_server_without_connecting(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    settings_path = tmp_path / "config" / "settings.yml"
    settings_path.parent.mkdir(parents=True)
    settings_path.write_text(
        yaml.safe_dump({"mcpServers": {"broken": {"type": "bogus", "url": "https://example.com/mcp"}}}),
        encoding="utf-8",
    )

    def fail_if_checked(*args, **kwargs):
        raise AssertionError("invalid MCP config must not create a health manager")

    monkeypatch.setattr(mcp_cli, "_create_health_check_manager", fail_if_checked)

    result = CliRunner().invoke(app, ["mcp", "list", "--check"])

    assert result.exit_code == 0, result.output
    assert "broken\tuser\tstdio\tapproved\tnot-configured\tinvalid-config\t-\t-\t-\t" in result.output
    assert "Unsupported MCP transport 'bogus'" in result.output


def test_mcp_list_check_batches_distinct_persisted_server_names(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    settings_path = tmp_path / "config" / "settings.yml"
    settings_path.parent.mkdir(parents=True)
    settings_path.write_text(
        yaml.safe_dump(
            {
                "mcpServers": {
                    "alpha": {"command": "alpha-cmd"},
                    "beta": {"command": "beta-cmd"},
                }
            }
        ),
        encoding="utf-8",
    )
    checked_batches: list[list[str]] = []

    def create_manager(configs: list[ScopedMCPServerConfig], *, roots: list[Path]) -> _FakeHealthManager:
        _ = roots
        checked_batches.append([config.name for config in configs])
        return _FakeHealthManager(
            [MCPConnectionRecord(scoped_config=config, state=MCPConnectionState.CONNECTED) for config in configs]
        )

    monkeypatch.setattr(mcp_cli, "_create_health_check_manager", create_manager)

    result = CliRunner().invoke(app, ["mcp", "list", "--check"])

    assert result.exit_code == 0, result.output
    assert checked_batches == [["alpha", "beta"]]
    assert "alpha\tuser\tstdio\tapproved\tnot-configured\tconnected\t0\t0\t0\t-" in result.output
    assert "beta\tuser\tstdio\tapproved\tnot-configured\tconnected\t0\t0\t0\t-" in result.output


def test_mcp_list_check_checks_same_name_persisted_servers_individually(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    settings_path = tmp_path / "config" / "settings.yml"
    local_path = tmp_path / ".iac-code" / "settings.local.yml"
    settings_path.parent.mkdir(parents=True)
    local_path.parent.mkdir(parents=True)
    settings_path.write_text(yaml.safe_dump({"mcpServers": {"shared": {"command": "user-cmd"}}}), encoding="utf-8")
    local_path.write_text(yaml.safe_dump({"mcpServers": {"shared": {"command": "local-cmd"}}}), encoding="utf-8")
    seen_commands: list[str] = []

    def create_manager(configs, *, roots):
        [config] = configs
        seen_commands.append(str(config.config.command))
        return _FakeHealthManager(
            [
                MCPConnectionRecord(
                    scoped_config=config,
                    state=MCPConnectionState.CONNECTED,
                )
            ]
        )

    monkeypatch.setattr(mcp_cli, "_create_health_check_manager", create_manager)

    result = CliRunner().invoke(app, ["mcp", "list", "--check"])

    assert result.exit_code == 0, result.output
    assert seen_commands == ["user-cmd", "local-cmd"]
    assert "shared\tuser\tstdio\tapproved\tnot-configured\tconnected\t0\t0\t0\t-" in result.output
    assert "shared\tlocal\tstdio\tapproved\tnot-configured\tconnected\t0\t0\t0\t-" in result.output


def test_mcp_list_check_skips_lower_precedence_duplicate_signature(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    settings_path = tmp_path / "config" / "settings.yml"
    local_path = tmp_path / ".iac-code" / "settings.local.yml"
    settings_path.parent.mkdir(parents=True)
    local_path.parent.mkdir(parents=True)
    config = {"command": "shared-cmd", "args": ["serve"]}
    settings_path.write_text(yaml.safe_dump({"mcpServers": {"shared": config}}), encoding="utf-8")
    local_path.write_text(yaml.safe_dump({"mcpServers": {"shared": config}}), encoding="utf-8")
    checked: list[tuple[str, MCPConfigScope]] = []

    def create_manager(configs: list[ScopedMCPServerConfig], *, roots: list[Path]) -> _FakeHealthManager:
        _ = roots
        checked.extend((config.name, config.scope) for config in configs)
        return _FakeHealthManager(
            [MCPConnectionRecord(scoped_config=config, state=MCPConnectionState.CONNECTED) for config in configs]
        )

    monkeypatch.setattr(mcp_cli, "_create_health_check_manager", create_manager)

    result = CliRunner().invoke(app, ["mcp", "list", "--check"])

    assert result.exit_code == 0, result.output
    assert checked == [("shared", MCPConfigScope.LOCAL)]
    assert "shared\tuser\tstdio\tapproved\tnot-configured\tskipped\t-\t-\t-\t-" in result.output
    assert "shared\tlocal\tstdio\tapproved\tnot-configured\tconnected\t0\t0\t0\t-" in result.output


def test_mcp_list_rejects_check_and_config_only_even_when_config_empty(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))

    result = CliRunner().invoke(app, ["mcp", "list", "--check", "--config-only"])

    assert result.exit_code == 1
    assert "Use either --check or --config-only" in result.output


def test_mcp_get_plain_prints_redacted_config_without_health_manager(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("YUQUE_TOKEN", "token-value")
    settings_path = tmp_path / "config" / "settings.yml"
    settings_path.parent.mkdir(parents=True)
    settings_path.write_text(
        yaml.safe_dump(
            {
                "mcpServers": {
                    "yuque": {
                        "type": "http",
                        "url": "http://user:password@[::1]",
                        "headers": {"Authorization": "Bearer ${YUQUE_TOKEN}", "X-Org": "platform"},
                        "env": {"API_TOKEN": "secret-token"},
                        "oauth": {"clientId": "client-id", "clientSecretEnv": "YUQUE_CLIENT_SECRET"},
                    }
                }
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )

    def fail_if_checked(*args, **kwargs):
        raise AssertionError("plain mcp get must not create a health manager")

    monkeypatch.setattr(mcp_cli, "_create_health_check_manager", fail_if_checked)

    result = CliRunner().invoke(app, ["mcp", "get", "yuque", "--scope", "user"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["type"] == "http"
    assert payload["url"] == "http://[REDACTED]@[::1]"
    assert payload["headers"] == {"Authorization": "[redacted]", "X-Org": "platform"}
    assert payload["env"] == {"API_TOKEN": "[redacted]"}
    assert payload["oauth"] == {"clientId": "[redacted]", "clientSecretEnv": "[redacted]"}
    assert "YUQUE_TOKEN" not in result.output
    assert "token-value" not in result.output
    assert "client-id" not in result.output
    assert "secret-token" not in result.output
    assert "super-url-token" not in result.output
    assert "user:password" not in result.output


def test_mcp_get_check_prints_redacted_config_and_connection_diagnostics(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("YUQUE_TOKEN", "token-value")
    settings_path = tmp_path / "config" / "settings.yml"
    settings_path.parent.mkdir(parents=True)
    settings_path.write_text(
        yaml.safe_dump(
            {
                "mcpServers": {
                    "yuque": {
                        "type": "http",
                        "url": "https://user:password@example.com/mcp?access_token=super-url-token&space=public",
                        "headers": {"Authorization": "Bearer ${YUQUE_TOKEN}", "X-Org": "platform"},
                        "env": {"API_TOKEN": "secret-token"},
                        "oauth": {"clientId": "client-id", "clientSecretEnv": "YUQUE_CLIENT_SECRET"},
                    }
                }
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    diagnostic_config = {
        "type": "http",
        "url": "https://user:password@example.com/mcp?access_token=super-url-token&space=public",
        "oauth": {"clientId": "client-id", "clientSecretEnv": "YUQUE_CLIENT_SECRET"},
    }
    manager = _FakeHealthManager(
        [
            _record_with_refresh(
                MCPConnectionRecord(
                    scoped_config=_scoped_mcp_config("yuque", diagnostic_config),
                    state=MCPConnectionState.CONNECTED,
                    error="prompts failed with access_token=super-secret-token",
                    metadata=MCPConnectionMetadata(
                        state=MCPConnectionState.CONNECTED,
                        server_name="yuque",
                        protocol_version="2025-06-18",
                    ),
                    tools=[
                        MCPToolRecord(server_name="yuque", tool_name="search", public_name="mcp__yuque__search"),
                        MCPToolRecord(server_name="yuque", tool_name="read", public_name="mcp__yuque__read"),
                    ],
                    resources=[MCPResourceRecord(server_name="yuque", uri="resource://yuque/doc")],
                    prompts=[MCPPromptRecord(server_name="yuque", prompt_name="review", public_name="review")],
                ),
                kind="prompts",
                refreshed_at=123.456,
                failure_reason="prompts failed with access_token=super-secret-token",
            )
        ]
    )
    monkeypatch.setattr(mcp_cli, "_create_health_check_manager", lambda checked, *, roots: manager)
    diagnostic_mcp_config = MCPServerConfig.from_mapping("yuque", diagnostic_config)
    storage = MCPSecretStorage()
    storage.set_secret(
        oauth_storage_key(diagnostic_mcp_config, "client_id", scope=MCPConfigScope.USER),
        "registered-client",
    )
    storage.set_secret(
        oauth_storage_key(diagnostic_mcp_config, "client_secret", scope=MCPConfigScope.USER),
        "registered-secret",
    )
    storage.set_secret(
        oauth_storage_key(diagnostic_mcp_config, "client_auth_method", scope=MCPConfigScope.USER),
        "client_secret_post",
    )

    result = CliRunner().invoke(app, ["mcp", "get", "yuque", "--scope", "user", "--check"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["scope"] == "user"
    assert payload["transport"] == "http"
    assert payload["url"] == "https://[REDACTED]@example.com/mcp?access_token=[REDACTED]"
    assert payload["command"] is None
    assert payload["auth_state"] == "configured"
    assert payload["approval_state"] == "approved"
    assert payload["connection_state"] == "connected"
    assert payload["protocol_version"] == "2025-06-18"
    assert payload["oauth_client_state"] == {
        "oauth_configured": True,
        "configured_client_id": True,
        "stored_client_auth_method": "client_secret_post",
        "stored_client_id": True,
        "stored_client_secret": True,
    }
    assert payload["tools"] == 2
    assert payload["resources"] == 1
    assert payload["prompts"] == 1
    assert payload["latest_failure"] == "prompts failed with access_token=[REDACTED]"
    assert payload["latest_refresh_kind"] == "prompts"
    assert payload["latest_refresh_at"] == 123.456
    assert payload["latest_refresh_failure"] == "prompts failed with access_token=[REDACTED]"
    assert payload["config"]["url"] == "https://[REDACTED]@example.com/mcp?access_token=[REDACTED]"
    assert payload["config"]["headers"] == {"Authorization": "[redacted]", "X-Org": "[redacted]"}
    assert payload["config"]["env"] == {"API_TOKEN": "[redacted]"}
    assert payload["config"]["oauth"] == {"clientId": "[redacted]", "clientSecretEnv": "[redacted]"}
    assert "YUQUE_TOKEN" not in result.output
    assert "token-value" not in result.output
    assert "platform" not in result.output
    assert "client-id" not in result.output
    assert "YUQUE_CLIENT_SECRET" not in result.output
    assert "super-secret-token" not in result.output
    assert "super-url-token" not in result.output
    assert "user:password" not in result.output


def test_mcp_get_check_json_includes_structured_auth_error(monkeypatch, tmp_path: Path) -> None:
    marker = "MCP_REFRESH_EXCEPTION_SECRET_29173"
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    settings_path = tmp_path / "config" / "settings.yml"
    settings_path.parent.mkdir(parents=True)
    settings_path.write_text(
        yaml.safe_dump({"mcpServers": {"remote": {"type": "http", "url": "https://example.com/mcp"}}}),
        encoding="utf-8",
    )
    scoped = _scoped_mcp_config("remote", {"type": "http", "url": "https://example.com/mcp"})
    manager = _FakeHealthManager(
        [
            MCPConnectionRecord(
                scoped_config=scoped,
                state=MCPConnectionState.NEEDS_AUTH,
                error="MCP server 'remote' requires authentication: invalid_grant: {}".format(marker),
                auth_error="invalid_grant",
            )
        ]
    )
    monkeypatch.setattr(mcp_cli, "_create_health_check_manager", lambda checked, *, roots: manager)

    result = CliRunner().invoke(app, ["mcp", "get", "remote", "--scope", "user", "--check"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["auth_state"] == "needs-auth"
    assert payload["connection_state"] == "needs-auth"
    assert payload["auth_error"] == "invalid_grant"
    assert payload["latest_failure"] == "MCP server 'remote' requires authentication: [REDACTED]"
    assert marker not in result.output


def test_mcp_get_check_omits_refresh_failure_when_last_refresh_succeeded(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    settings_path = tmp_path / "config" / "settings.yml"
    settings_path.parent.mkdir(parents=True)
    settings_path.write_text(
        yaml.safe_dump({"mcpServers": {"yuque": {"type": "http", "url": "https://example.com/mcp"}}}),
        encoding="utf-8",
    )
    manager = _FakeHealthManager(
        [
            _record_with_refresh(
                MCPConnectionRecord(
                    scoped_config=_scoped_mcp_config(
                        "yuque",
                        {
                            "type": "http",
                            "url": "https://example.com/mcp",
                        },
                    ),
                    state=MCPConnectionState.CONNECTED,
                    tools=[MCPToolRecord(server_name="yuque", tool_name="search", public_name="mcp__yuque__search")],
                ),
                kind="tools",
                refreshed_at=234.0,
                failure_reason=None,
            )
        ]
    )
    monkeypatch.setattr(mcp_cli, "_create_health_check_manager", lambda checked, *, roots: manager)

    result = CliRunner().invoke(app, ["mcp", "get", "yuque", "--scope", "user", "--check"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["latest_refresh_kind"] == "tools"
    assert payload["latest_refresh_at"] == 234.0
    assert "latest_refresh_failure" not in payload


def test_mcp_get_check_sanitizes_url_query_secrets(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    secret_url = "https://example.com/mcp?access_token=super-secret-token&space=public"
    settings_path = tmp_path / "config" / "settings.yml"
    settings_path.parent.mkdir(parents=True)
    settings_path.write_text(
        yaml.safe_dump({"mcpServers": {"remote": {"type": "http", "url": secret_url}}}, sort_keys=True),
        encoding="utf-8",
    )
    scoped = _scoped_mcp_config("remote", {"type": "http", "url": secret_url})
    manager = _FakeHealthManager([MCPConnectionRecord(scoped_config=scoped, state=MCPConnectionState.CONNECTED)])
    monkeypatch.setattr(mcp_cli, "_create_health_check_manager", lambda checked, *, roots: manager)

    result = CliRunner().invoke(app, ["mcp", "get", "remote", "--scope", "user", "--check"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["url"] == "https://example.com/mcp?access_token=[REDACTED]"
    assert payload["config"]["url"] == "https://example.com/mcp?access_token=[REDACTED]"
    assert "super-secret-token" not in result.output


def test_mcp_get_check_explicit_project_scope_uses_nearest_project_file_from_nested_cwd(
    monkeypatch,
    tmp_path: Path,
) -> None:
    root = tmp_path / "repo"
    nested = root / "services" / "api"
    nested.mkdir(parents=True)
    (root / ".git").mkdir()
    root_project_file = root / ".mcp.json"
    child_project_file = root / "services" / ".mcp.json"
    root_project_file.write_text(
        json.dumps({"mcpServers": {"shared": {"type": "http", "url": "https://root.example/mcp"}}}),
        encoding="utf-8",
    )
    child_project_file.write_text(
        json.dumps({"mcpServers": {"shared": {"type": "http", "url": "https://child.example/mcp"}}}),
        encoding="utf-8",
    )
    monkeypatch.chdir(nested)
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    approve_project_mcp_server("shared", project_file=root_project_file, workspace_root=root)
    approve_project_mcp_server("shared", project_file=child_project_file, workspace_root=root)
    checked_configs: list[ScopedMCPServerConfig] = []

    def fake_health_manager(configs: list[ScopedMCPServerConfig], *, roots: list[Path]) -> _FakeHealthManager:
        _ = roots
        checked_configs.extend(configs)
        records = [MCPConnectionRecord(scoped_config=config, state=MCPConnectionState.CONNECTED) for config in configs]
        return _FakeHealthManager(records)

    monkeypatch.setattr(mcp_cli, "_create_health_check_manager", fake_health_manager)
    runner = CliRunner()

    plain = runner.invoke(app, ["mcp", "get", "shared", "--scope", "project", "--config-only"])
    checked = runner.invoke(app, ["mcp", "get", "shared", "--scope", "project", "--check"])

    assert plain.exit_code == 0, plain.output
    assert '"url": "https://child.example/mcp"' in plain.output
    assert "https://root.example/mcp" not in plain.output
    assert checked.exit_code == 0, checked.output
    payload = json.loads(checked.output)
    assert payload["url"] == "https://child.example/mcp"
    assert payload["config"]["url"] == "https://child.example/mcp"
    assert payload["connection_state"] == "connected"
    assert [config.config.url for config in checked_configs] == ["https://child.example/mcp"]
    assert [config.source_path for config in checked_configs] == [str(child_project_file)]
    assert "https://root.example/mcp" not in checked.output


def test_mcp_disable_enable_explicit_project_scope_uses_nearest_project_file_from_nested_cwd(
    monkeypatch,
    tmp_path: Path,
) -> None:
    root = tmp_path / "repo"
    nested = root / "services" / "api"
    nested.mkdir(parents=True)
    (root / ".git").mkdir()
    root_project_file = root / ".mcp.json"
    child_project_file = root / "services" / ".mcp.json"
    root_project_file.write_text(
        json.dumps({"mcpServers": {"shared": {"command": "root-cmd"}}}),
        encoding="utf-8",
    )
    child_project_file.write_text(
        json.dumps({"mcpServers": {"shared": {"command": "child-cmd"}}}),
        encoding="utf-8",
    )
    monkeypatch.chdir(nested)
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    runner = CliRunner()

    disabled = runner.invoke(app, ["mcp", "disable", "shared", "--scope", "project"])

    assert disabled.exit_code == 0, disabled.output
    root_exact = load_exact_mcp_config(
        "shared",
        scope=MCPConfigScope.PROJECT,
        cwd=nested,
        source_path=root_project_file,
        workspace_root=root,
    )
    child_exact = load_exact_mcp_config(
        "shared",
        scope=MCPConfigScope.PROJECT,
        cwd=nested,
        source_path=child_project_file,
        workspace_root=root,
    )
    assert root_exact.servers[0].disabled is False
    assert child_exact.servers[0].disabled is True

    enabled = runner.invoke(app, ["mcp", "enable", "shared", "--scope", "project"])

    assert enabled.exit_code == 0, enabled.output
    child_enabled = load_exact_mcp_config(
        "shared",
        scope=MCPConfigScope.PROJECT,
        cwd=nested,
        source_path=child_project_file,
        workspace_root=root,
    )
    assert child_enabled.servers[0].disabled is False


def test_mcp_disable_explicit_source_path_targets_matching_project_file(monkeypatch, tmp_path: Path) -> None:
    root = tmp_path / "repo"
    nested = root / "services" / "api"
    nested.mkdir(parents=True)
    (root / ".git").mkdir()
    root_project_file = root / ".mcp.json"
    child_project_file = root / "services" / ".mcp.json"
    root_project_file.write_text(
        json.dumps({"mcpServers": {"shared": {"command": "root-cmd"}}}),
        encoding="utf-8",
    )
    child_project_file.write_text(
        json.dumps({"mcpServers": {"shared": {"command": "child-cmd"}}}),
        encoding="utf-8",
    )
    monkeypatch.chdir(nested)
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))

    disabled = CliRunner().invoke(
        app,
        ["mcp", "disable", "shared", "--scope", "project", "--source-path", str(root_project_file)],
    )

    assert disabled.exit_code == 0, disabled.output
    root_exact = load_exact_mcp_config(
        "shared",
        scope=MCPConfigScope.PROJECT,
        cwd=nested,
        source_path=root_project_file,
        workspace_root=root,
    )
    child_exact = load_exact_mcp_config(
        "shared",
        scope=MCPConfigScope.PROJECT,
        cwd=nested,
        source_path=child_project_file,
        workspace_root=root,
    )
    assert root_exact.servers[0].disabled is True
    assert child_exact.servers[0].disabled is False


def test_mcp_persisted_commands_explicit_source_path_can_target_sibling_project_file(
    monkeypatch,
    tmp_path: Path,
) -> None:
    root = tmp_path / "repo"
    service = root / "services"
    service.mkdir(parents=True)
    (root / ".git").mkdir()
    root_project_file = root / ".mcp.json"
    service_project_file = service / ".mcp.json"
    child_raw_config = {
        "type": "http",
        "url": "https://service.example/mcp",
        "oauth": {"clientId": "service-client"},
    }
    root_project_file.write_text(
        json.dumps({"mcpServers": {"shared": {"command": "root-cmd"}}}),
        encoding="utf-8",
    )
    service_project_file.write_text(
        json.dumps({"mcpServers": {"shared": child_raw_config}}),
        encoding="utf-8",
    )
    monkeypatch.chdir(root)
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    runner = CliRunner()

    disabled = runner.invoke(
        app,
        ["mcp", "disable", "shared", "--scope", "project", "--source-path", "services/.mcp.json"],
    )

    assert disabled.exit_code == 0, disabled.output
    root_exact = load_exact_mcp_config(
        "shared",
        scope=MCPConfigScope.PROJECT,
        cwd=root,
        source_path=root_project_file,
        workspace_root=root,
    )
    service_exact = load_exact_mcp_config(
        "shared",
        scope=MCPConfigScope.PROJECT,
        cwd=root,
        source_path=service_project_file,
        workspace_root=root,
    )
    assert root_exact.servers[0].disabled is False
    assert service_exact.servers[0].disabled is True

    enabled = runner.invoke(
        app,
        ["mcp", "enable", "shared", "--scope", "project", "--source-path", "services/.mcp.json"],
    )

    assert enabled.exit_code == 0, enabled.output
    service_enabled = load_exact_mcp_config(
        "shared",
        scope=MCPConfigScope.PROJECT,
        cwd=root,
        source_path=service_project_file,
        workspace_root=root,
    )
    assert service_enabled.servers[0].disabled is False

    config = MCPServerConfig.from_mapping("shared", child_raw_config)
    storage = MCPSecretStorage()
    oauth_scope = "project:{}".format(service_project_file.as_posix())
    storage.set_secret(oauth_storage_key(config, "access_token", scope=oauth_scope), "service-access")

    reset = runner.invoke(
        app,
        ["mcp", "reset-auth", "shared", "--scope", "project", "--source-path", "services/.mcp.json"],
    )

    assert reset.exit_code == 0, reset.output
    assert storage.get_secret(oauth_storage_key(config, "access_token", scope=oauth_scope)) is None

    removed = runner.invoke(
        app,
        ["mcp", "remove", "shared", "--scope", "project", "--source-path", "services/.mcp.json"],
    )

    assert removed.exit_code == 0, removed.output
    assert (
        load_exact_mcp_config(
            "shared",
            scope=MCPConfigScope.PROJECT,
            cwd=root,
            source_path=service_project_file,
            workspace_root=root,
        ).servers
        == []
    )
    assert (
        load_exact_mcp_config(
            "shared",
            scope=MCPConfigScope.PROJECT,
            cwd=root,
            source_path=root_project_file,
            workspace_root=root,
        )
        .servers[0]
        .config.command
        == "root-cmd"
    )


def test_mcp_ambiguous_project_sources_include_executable_source_path_commands(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root_project_file = root / ".mcp.json"
    child_project_file = root / "services" / ".mcp.json"
    matches = [
        MCPPersistedServerMatch(MCPConfigScope.PROJECT, root_project_file, {"command": "root-cmd"}),
        MCPPersistedServerMatch(MCPConfigScope.PROJECT, child_project_file, {"command": "child-cmd"}),
    ]

    message = mcp_cli._ambiguous_scope_message("shared", command="remove", matches=matches)

    assert f"iac-code mcp remove shared --scope project --source-path {root_project_file}" in message
    assert f"iac-code mcp remove shared --scope project --source-path {child_project_file}" in message
    assert " --scope project  #" not in message


def test_mcp_ambiguous_project_sources_use_windows_friendly_source_path_commands(monkeypatch) -> None:
    monkeypatch.setattr(mcp_cli.sys, "platform", "win32")
    source_path = PureWindowsPath(r"C:\Users\runneradmin\repo\.mcp.json")
    match = MCPPersistedServerMatch(MCPConfigScope.PROJECT, source_path, {"command": "root-cmd"})

    line = mcp_cli._ambiguous_scope_command_line("shared", command="remove", match=match)

    assert line == r"iac-code mcp remove shared --scope project --source-path C:\Users\runneradmin\repo\.mcp.json"


def test_mcp_get_check_explicit_project_scope_respects_pending_approval_when_shadowed_by_local(
    monkeypatch,
    tmp_path: Path,
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    (root / ".git").mkdir()
    root_project_file = root / ".mcp.json"
    root_project_file.write_text(
        json.dumps({"mcpServers": {"shared": {"type": "http", "url": "https://project.example/mcp"}}}),
        encoding="utf-8",
    )
    local_settings = root / ".iac-code" / "settings.local.yml"
    local_settings.parent.mkdir()
    local_settings.write_text(
        yaml.safe_dump(
            {"mcpServers": {"shared": {"type": "http", "url": "https://local.example/mcp"}}},
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    monkeypatch.chdir(root)
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    checked_configs: list[ScopedMCPServerConfig] = []

    def fake_health_manager(configs: list[ScopedMCPServerConfig], *, roots: list[Path]) -> _FakeHealthManager:
        _ = roots
        checked_configs.extend(configs)
        return _FakeHealthManager([])

    monkeypatch.setattr(mcp_cli, "_create_health_check_manager", fake_health_manager)

    result = CliRunner().invoke(app, ["mcp", "get", "shared", "--scope", "project", "--check"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["connection_state"] == "pending-approval"
    assert payload["latest_failure"] == "Project MCP server pending approval."
    assert payload["url"] == "https://project.example/mcp"
    assert checked_configs == []
    assert "https://local.example/mcp" not in result.output


def test_mcp_get_check_explicit_user_scope_expands_env_when_shadowed_by_local(
    monkeypatch,
    tmp_path: Path,
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    (root / ".git").mkdir()
    user_settings = tmp_path / "config" / "settings.yml"
    user_settings.parent.mkdir()
    user_settings.write_text(
        yaml.safe_dump(
            {
                "mcpServers": {
                    "shared": {
                        "type": "http",
                        "url": "https://user.example/mcp",
                        "headers": {"X-Org": "${ORG}"},
                    }
                }
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    local_settings = root / ".iac-code" / "settings.local.yml"
    local_settings.parent.mkdir()
    local_settings.write_text(
        yaml.safe_dump(
            {
                "mcpServers": {
                    "shared": {
                        "type": "http",
                        "url": "https://local.example/mcp",
                        "headers": {"X-Org": "local"},
                    }
                }
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    monkeypatch.chdir(root)
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("ORG", "platform")
    checked_configs: list[ScopedMCPServerConfig] = []

    def fake_health_manager(configs: list[ScopedMCPServerConfig], *, roots: list[Path]) -> _FakeHealthManager:
        _ = roots
        checked_configs.extend(configs)
        records = [MCPConnectionRecord(scoped_config=config, state=MCPConnectionState.CONNECTED) for config in configs]
        return _FakeHealthManager(records)

    monkeypatch.setattr(mcp_cli, "_create_health_check_manager", fake_health_manager)

    result = CliRunner().invoke(app, ["mcp", "get", "shared", "--scope", "user", "--check"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["url"] == "https://user.example/mcp"
    assert checked_configs[0].config.url == "https://user.example/mcp"
    assert checked_configs[0].config.headers == {"X-Org": "platform"}
    assert "${ORG}" not in result.output
    assert "https://local.example/mcp" not in result.output


def test_mcp_get_check_explicit_scope_reports_invalid_config_for_malformed_value(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    settings_path = tmp_path / "config" / "settings.yml"
    settings_path.parent.mkdir()
    settings_path.write_text(yaml.safe_dump({"mcpServers": {"bad": "not-an-object"}}), encoding="utf-8")

    def fail_if_checked(*args: object, **kwargs: object) -> None:
        raise AssertionError("malformed exact config must not be connected")

    monkeypatch.setattr(mcp_cli, "_create_health_check_manager", fail_if_checked)

    result = CliRunner().invoke(app, ["mcp", "get", "bad", "--scope", "user", "--check"])

    assert result.exit_code == 1
    assert "config must be an object" in result.output
    assert "not found" not in result.output


def test_mcp_list_check_uses_bounded_timeouts_and_disconnects_after_manager_failure(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    server = _scoped_mcp_config("remote", {"type": "http", "url": "https://example.com/mcp"})
    captured_kwargs: dict[str, object] = {}
    manager = _FakeHealthManager([], connect_error=RuntimeError("access_token=super-secret-token"))

    monkeypatch.setattr(
        mcp_cli,
        "load_mcp_configs",
        lambda **_: MCPConfigLoadResult(servers=[server], warnings=[], pending=[]),
    )

    def fake_mcp_manager(configs: list[ScopedMCPServerConfig], **kwargs: object) -> _FakeHealthManager:
        assert configs == [server]
        captured_kwargs.update(kwargs)
        return manager

    monkeypatch.setattr(mcp_cli, "MCPManager", fake_mcp_manager)

    result = CliRunner().invoke(app, ["mcp", "list", "--check"])

    assert result.exit_code == 1
    assert manager.connected is True
    assert manager.disconnected is True
    connect_timeout = captured_kwargs["connect_timeout_seconds"]
    operation_timeout = captured_kwargs["operation_timeout_seconds"]
    assert isinstance(connect_timeout, int | float)
    assert isinstance(operation_timeout, int | float)
    assert connect_timeout <= 3
    assert operation_timeout <= 3
    assert "MCP health check failed" in result.output
    assert "super-secret-token" not in result.output


def test_mcp_disable_enable_omitted_scope_persists_disabled_state_and_list_check_skips_connect(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    runner = CliRunner()
    added = runner.invoke(app, ["mcp", "add", "yuque", "--command", "uvx", "--scope", "user"])
    assert added.exit_code == 0, added.output

    disabled = runner.invoke(app, ["mcp", "disable", "yuque"])

    assert disabled.exit_code == 0, disabled.output
    assert "Disabled MCP server 'yuque'." in disabled.output
    assert (tmp_path / "config" / "mcp" / "server-states.json").exists()
    listed = runner.invoke(app, ["mcp", "list", "--config-only"])
    assert listed.exit_code == 0, listed.output
    assert listed.output == "yuque\tuser\tstdio\tdisabled\n"

    checked_configs: list[ScopedMCPServerConfig] = []

    def fake_health_manager(configs: list[ScopedMCPServerConfig], *, roots: list[Path]) -> _FakeHealthManager:
        _ = roots
        checked_configs.extend(configs)
        return _FakeHealthManager([])

    monkeypatch.setattr(mcp_cli, "_create_health_check_manager", fake_health_manager)
    checked = runner.invoke(app, ["mcp", "list", "--check"])

    assert checked.exit_code == 0, checked.output
    assert checked_configs == []
    assert "yuque\tuser\tstdio\tdisabled\tnot-configured\tdisabled\t-\t-\t-\tMCP server disabled." in checked.output

    enabled = runner.invoke(app, ["mcp", "enable", "yuque"])

    assert enabled.exit_code == 0, enabled.output
    assert "Enabled MCP server 'yuque'." in enabled.output
    listed_again = runner.invoke(app, ["mcp", "list", "--config-only"])
    assert listed_again.exit_code == 0, listed_again.output
    assert listed_again.output == "yuque\tuser\tstdio\tapproved\n"


def test_mcp_reconnect_omitted_scope_reconnects_unique_user_server_and_reports_discovery(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    runner = CliRunner()
    added = runner.invoke(
        app,
        ["mcp", "add", "yuque", "--type", "http", "--url", "https://example.com/mcp", "--scope", "user"],
    )
    assert added.exit_code == 0, added.output
    checked_configs: list[ScopedMCPServerConfig] = []

    def fake_health_manager(configs: list[ScopedMCPServerConfig], *, roots: list[Path]) -> _FakeHealthManager:
        _ = roots
        checked_configs.extend(configs)
        return _FakeHealthManager(
            [
                MCPConnectionRecord(
                    scoped_config=configs[0],
                    state=MCPConnectionState.CONNECTED,
                    tools=[MCPToolRecord(server_name="yuque", tool_name="search", public_name="mcp__yuque__search")],
                )
            ]
        )

    monkeypatch.setattr(mcp_cli, "_create_reconnect_manager", fake_health_manager)

    result = runner.invoke(app, ["mcp", "reconnect", "yuque"])

    assert result.exit_code == 0, result.output
    assert [(config.name, config.scope) for config in checked_configs] == [("yuque", MCPConfigScope.USER)]
    assert "yuque\tuser\thttp\tapproved\tnot-configured\tconnected\t1\t0\t0\t-" in result.output


def test_mcp_reconnect_explicit_project_scope_uses_nearest_project_file_from_nested_cwd(
    monkeypatch,
    tmp_path: Path,
) -> None:
    root = tmp_path / "repo"
    nested = root / "services" / "api"
    nested.mkdir(parents=True)
    (root / ".git").mkdir()
    root_project_file = root / ".mcp.json"
    child_project_file = root / "services" / ".mcp.json"
    root_project_file.write_text(
        json.dumps({"mcpServers": {"shared": {"type": "http", "url": "https://root.example/mcp"}}}),
        encoding="utf-8",
    )
    child_project_file.write_text(
        json.dumps({"mcpServers": {"shared": {"type": "http", "url": "https://child.example/mcp"}}}),
        encoding="utf-8",
    )
    monkeypatch.chdir(nested)
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    approve_project_mcp_server("shared", project_file=root_project_file, workspace_root=root)
    approve_project_mcp_server("shared", project_file=child_project_file, workspace_root=root)
    checked_configs: list[ScopedMCPServerConfig] = []

    def fake_health_manager(configs: list[ScopedMCPServerConfig], *, roots: list[Path]) -> _FakeHealthManager:
        _ = roots
        checked_configs.extend(configs)
        return _FakeHealthManager(
            [MCPConnectionRecord(scoped_config=config, state=MCPConnectionState.CONNECTED) for config in configs]
        )

    monkeypatch.setattr(mcp_cli, "_create_reconnect_manager", fake_health_manager)

    result = CliRunner().invoke(app, ["mcp", "reconnect", "shared", "--scope", "project"])

    assert result.exit_code == 0, result.output
    assert [(config.config.url, config.source_path) for config in checked_configs] == [
        ("https://child.example/mcp", str(child_project_file))
    ]
    assert "shared\tproject\thttp\tapproved\tnot-configured\tconnected\t0\t0\t0\t-" in result.output


def test_mcp_reconnect_all_uses_all_persisted_scopes_and_skips_disabled_and_pending(
    monkeypatch,
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    monkeypatch.chdir(repo)
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    runner = CliRunner()
    user = runner.invoke(app, ["mcp", "add", "shared", "--command", "user-cmd", "--scope", "user"])
    local = runner.invoke(app, ["mcp", "add", "shared", "--command", "local-cmd", "--scope", "local"])
    disabled_added = runner.invoke(app, ["mcp", "add", "disabled", "--command", "uvx", "--scope", "user"])
    assert user.exit_code == 0, user.output
    assert local.exit_code == 0, local.output
    assert disabled_added.exit_code == 0, disabled_added.output
    disabled = runner.invoke(app, ["mcp", "disable", "disabled", "--scope", "user"])
    assert disabled.exit_code == 0, disabled.output
    (repo / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"pending": {"command": "project-cmd"}}}),
        encoding="utf-8",
    )
    checked_configs: list[ScopedMCPServerConfig] = []

    def fake_health_manager(configs: list[ScopedMCPServerConfig], *, roots: list[Path]) -> _FakeHealthManager:
        _ = roots
        checked_configs.extend(configs)
        return _FakeHealthManager(
            [MCPConnectionRecord(scoped_config=config, state=MCPConnectionState.CONNECTED) for config in configs]
        )

    monkeypatch.setattr(mcp_cli, "_create_reconnect_manager", fake_health_manager)

    result = runner.invoke(app, ["mcp", "reconnect", "--all"])

    assert result.exit_code == 0, result.output
    assert [(config.name, config.scope) for config in checked_configs] == [
        ("shared", MCPConfigScope.USER),
        ("shared", MCPConfigScope.LOCAL),
    ]
    assert "shared\tuser\tstdio\tapproved\tnot-configured\tconnected\t0\t0\t0\t-" in result.output
    assert "shared\tlocal\tstdio\tapproved\tnot-configured\tconnected\t0\t0\t0\t-" in result.output
    assert "disabled\tuser\tstdio\tdisabled\tnot-configured\tdisabled\t-\t-\t-\tMCP server disabled." in result.output
    assert (
        "pending\tproject\tstdio\tpending-approval\tnot-configured\tpending-approval\t-\t-\t-\t"
        "Project MCP server pending approval."
    ) in result.output


def test_mcp_reconnect_all_rejects_source_path(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    runner = CliRunner()
    checked = False

    def fake_health_manager(configs: list[ScopedMCPServerConfig], *, roots: list[Path]) -> _FakeHealthManager:
        nonlocal checked
        _ = configs, roots
        checked = True
        return _FakeHealthManager([])

    monkeypatch.setattr(mcp_cli, "_create_reconnect_manager", fake_health_manager)

    result = runner.invoke(app, ["mcp", "reconnect", "--all", "--source-path", ".mcp.json"])

    assert result.exit_code == 1, result.output
    assert "--source-path cannot be used with mcp reconnect --all" in result.output
    assert checked is False


def test_mcp_reconnect_disabled_management_commands_ambiguous_omitted_scope_list_disambiguation_commands(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    runner = CliRunner()
    user = runner.invoke(app, ["mcp", "add", "shared", "--command", "user-cmd", "--scope", "user"])
    local = runner.invoke(app, ["mcp", "add", "shared", "--command", "local-cmd", "--scope", "local"])
    assert user.exit_code == 0, user.output
    assert local.exit_code == 0, local.output

    for command in ("reconnect", "disable", "enable"):
        result = runner.invoke(app, ["mcp", command, "shared"])

        assert result.exit_code == 1, result.output
        assert "iac-code mcp {} shared --scope local".format(command) in result.output
        assert "iac-code mcp {} shared --scope user".format(command) in result.output


def test_mcp_persisted_commands_resolve_unique_user_scope_when_omitted(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("IAC_CODE_MCP_DISABLE_KEYRING", "1")
    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "mcp",
            "add",
            "remote",
            "--type",
            "http",
            "--url",
            "https://example.com/mcp",
            "--client-id",
            "client-id",
            "--scope",
            "user",
        ],
    )
    assert result.exit_code == 0, result.output

    fetched = runner.invoke(app, ["mcp", "get", "remote", "--config-only"])

    assert fetched.exit_code == 0, fetched.output
    assert '"url": "https://example.com/mcp"' in fetched.output

    auth_scopes: list[MCPConfigScope | str | None] = []

    def fake_auth(
        config: MCPServerConfig,
        *,
        storage: MCPSecretStorage,
        scope: MCPConfigScope | str | None = None,
        server_name: str,
    ) -> None:
        _ = server_name
        auth_scopes.append(scope)
        storage.set_secret(oauth_storage_key(config, "access_token", scope=scope), "access")

    monkeypatch.setattr(mcp_cli, "_run_cli_oauth_flow", fake_auth)

    authenticated = runner.invoke(app, ["mcp", "auth", "remote"])

    assert authenticated.exit_code == 0, authenticated.output
    assert auth_scopes == [MCPConfigScope.USER]
    settings = yaml.safe_load((tmp_path / "config" / "settings.yml").read_text(encoding="utf-8"))
    config = MCPServerConfig.from_mapping("remote", settings["mcpServers"]["remote"])
    storage = MCPSecretStorage()
    assert storage.get_secret(oauth_storage_key(config, "access_token", scope=MCPConfigScope.USER)) == "access"

    reset = runner.invoke(app, ["mcp", "reset-auth", "remote"])

    assert reset.exit_code == 0, reset.output
    assert storage.get_secret(oauth_storage_key(config, "access_token", scope=MCPConfigScope.USER)) is None

    removed = runner.invoke(app, ["mcp", "remove", "remote"])

    assert removed.exit_code == 0, removed.output
    settings = yaml.safe_load((tmp_path / "config" / "settings.yml").read_text(encoding="utf-8"))
    assert "remote" not in settings["mcpServers"]


def test_mcp_remove_clears_scoped_oauth_state(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("IAC_CODE_MCP_DISABLE_KEYRING", "1")
    runner = CliRunner()
    added = runner.invoke(
        app,
        [
            "mcp",
            "add",
            "yuque",
            "--type",
            "http",
            "--url",
            "https://example.com/mcp",
            "--scope",
            "user",
        ],
    )
    assert added.exit_code == 0, added.output
    config = _user_mcp_config(tmp_path, "yuque")
    storage = MCPSecretStorage()
    _store_oauth_state(config, storage, scope=MCPConfigScope.USER)

    removed = runner.invoke(app, ["mcp", "remove", "yuque", "--scope", "user"])

    assert removed.exit_code == 0, removed.output
    settings = yaml.safe_load((tmp_path / "config" / "settings.yml").read_text(encoding="utf-8"))
    assert "yuque" not in settings["mcpServers"]
    _assert_no_oauth_state(config, storage, scope=MCPConfigScope.USER)


def test_mcp_get_ambiguous_omitted_scope_lists_disambiguation_commands(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    runner = CliRunner()
    user = runner.invoke(app, ["mcp", "add", "shared", "--command", "user-cmd", "--scope", "user"])
    local = runner.invoke(app, ["mcp", "add", "shared", "--command", "local-cmd", "--scope", "local"])
    assert user.exit_code == 0, user.output
    assert local.exit_code == 0, local.output

    result = runner.invoke(app, ["mcp", "get", "shared"])

    assert result.exit_code == 1
    assert "iac-code mcp get shared --scope local" in result.output
    assert "iac-code mcp get shared --scope user" in result.output


def test_mcp_get_source_path_requires_scope_without_traceback(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    (tmp_path / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"remote": {"type": "http", "url": "https://example.com/mcp"}}}),
        encoding="utf-8",
    )

    result = CliRunner().invoke(app, ["mcp", "get", "remote", "--source-path", ".mcp.json"])

    assert result.exit_code == 1, result.output
    assert "--source-path requires --scope" in result.output
    assert "AssertionError" not in result.output
    assert "Traceback" not in result.output


def test_mcp_get_wrong_scope_source_path_reports_actionable_error_without_traceback(
    monkeypatch,
    tmp_path: Path,
) -> None:
    root = tmp_path / "repo"
    local_settings = root / ".iac-code" / "settings.local.yml"
    local_settings.parent.mkdir(parents=True)
    (root / ".git").mkdir()
    local_settings.write_text(
        yaml.safe_dump({"mcpServers": {"remote": {"type": "http", "url": "https://example.com/mcp"}}}),
        encoding="utf-8",
    )
    monkeypatch.chdir(root)
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))

    result = CliRunner().invoke(
        app,
        [
            "mcp",
            "get",
            "remote",
            "--scope",
            "user",
            "--source-path",
            str(local_settings),
            "--config-only",
        ],
    )

    assert result.exit_code == 1, result.output
    assert "does not belong to user scope" in result.output
    assert "Traceback" not in result.output


def test_mcp_get_sibling_project_source_path_reports_actionable_error_without_traceback(
    monkeypatch,
    tmp_path: Path,
) -> None:
    root = tmp_path / "repo"
    sibling = tmp_path / "sibling"
    project_file = sibling / ".mcp.json"
    root.mkdir()
    sibling.mkdir()
    (root / ".git").mkdir()
    project_file.write_text(
        json.dumps({"mcpServers": {"remote": {"type": "http", "url": "https://example.com/mcp"}}}),
        encoding="utf-8",
    )
    monkeypatch.chdir(root)
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))

    result = CliRunner().invoke(
        app,
        [
            "mcp",
            "get",
            "remote",
            "--scope",
            "project",
            "--source-path",
            str(project_file),
            "--config-only",
        ],
    )

    assert result.exit_code == 1, result.output
    assert "does not belong to project scope" in result.output
    assert "Traceback" not in result.output


def test_mcp_auth_reset_remove_ambiguous_omitted_scope_lists_disambiguation_commands(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    runner = CliRunner()
    user = runner.invoke(app, ["mcp", "add", "shared", "--command", "user-cmd", "--scope", "user"])
    local = runner.invoke(app, ["mcp", "add", "shared", "--command", "local-cmd", "--scope", "local"])
    assert user.exit_code == 0, user.output
    assert local.exit_code == 0, local.output

    for command in ("auth", "reset-auth", "remove"):
        result = runner.invoke(app, ["mcp", command, "shared"])

        assert result.exit_code == 1, result.output
        assert "iac-code mcp {} shared --scope local".format(command) in result.output
        assert "iac-code mcp {} shared --scope user".format(command) in result.output


def test_mcp_get_ambiguous_omitted_scope_suggests_nested_project_file(
    monkeypatch,
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    nested = repo / "services" / "api"
    nested.mkdir(parents=True)
    (repo / ".git").mkdir()
    monkeypatch.chdir(nested)
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    child_project_file = repo / "services" / ".mcp.json"
    child_project_file.write_text('{"mcpServers": {"shared": {"command": "child-project-cmd"}}}', encoding="utf-8")
    runner = CliRunner()
    user = runner.invoke(app, ["mcp", "add", "shared", "--command", "user-cmd", "--scope", "user"])
    local = runner.invoke(app, ["mcp", "add", "shared", "--command", "local-cmd", "--scope", "local"])
    assert user.exit_code == 0, user.output
    assert local.exit_code == 0, local.output

    explicit_project = runner.invoke(app, ["mcp", "get", "shared", "--scope", "project", "--config-only"])
    result = runner.invoke(app, ["mcp", "get", "shared"])

    assert explicit_project.exit_code == 0, explicit_project.output
    assert '"command": "child-project-cmd"' in explicit_project.output
    assert result.exit_code == 1
    assert "iac-code mcp get shared --scope local" in result.output
    assert "iac-code mcp get shared --scope user" in result.output
    assert "iac-code mcp get shared --scope project" in result.output


def test_mcp_project_auth_uses_nearest_project_scope_when_child_project_has_same_name(
    monkeypatch,
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    nested = repo / "services" / "api"
    nested.mkdir(parents=True)
    (repo / ".git").mkdir()
    root_project_file = repo / ".mcp.json"
    child_project_file = repo / "services" / ".mcp.json"
    root_raw_config = {
        "type": "http",
        "url": "https://root.example/mcp",
        "oauth": {"clientId": "root-client"},
    }
    root_project_file.write_text(json.dumps({"mcpServers": {"remote": root_raw_config}}), encoding="utf-8")
    child_raw_config = {
        "type": "http",
        "url": "https://child.example/mcp",
        "oauth": {"clientId": "child-client"},
    }
    child_project_file.write_text(json.dumps({"mcpServers": {"remote": child_raw_config}}), encoding="utf-8")
    monkeypatch.chdir(nested)
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("IAC_CODE_MCP_DISABLE_KEYRING", "1")
    runner = CliRunner()
    auth_calls: list[tuple[str, MCPConfigScope | str | None]] = []

    def fake_auth(
        config: MCPServerConfig,
        *,
        storage: MCPSecretStorage,
        scope: MCPConfigScope | str | None = None,
        server_name: str,
    ) -> None:
        _ = server_name
        auth_calls.append((config.url or "", scope))
        storage.set_secret(oauth_storage_key(config, "access_token", scope=scope), "auth-access")

    monkeypatch.setattr(mcp_cli, "_run_cli_oauth_flow", fake_auth)

    authenticated = runner.invoke(app, ["mcp", "auth", "remote"])

    assert authenticated.exit_code == 0, authenticated.output
    expected_scope = "project:{}".format(child_project_file.as_posix())
    child_config = MCPServerConfig.from_mapping("remote", child_raw_config)
    storage = MCPSecretStorage()
    storage.set_secret(oauth_storage_key(child_config, "access_token", scope=expected_scope), "child-access")
    reset = runner.invoke(app, ["mcp", "reset-auth", "remote"])

    assert reset.exit_code == 0, reset.output
    assert auth_calls == [("https://child.example/mcp", expected_scope)]
    assert storage.get_secret(oauth_storage_key(child_config, "access_token", scope=expected_scope)) is None


def test_mcp_project_auth_relative_source_path_uses_canonical_scope_and_reset_matches(
    monkeypatch,
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    monkeypatch.chdir(repo)
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("IAC_CODE_MCP_DISABLE_KEYRING", "1")
    raw_config = {
        "type": "http",
        "url": "https://project.example/mcp",
        "oauth": {"clientId": "project-client"},
    }
    project_file = repo / ".mcp.json"
    project_file.write_text(json.dumps({"mcpServers": {"remote": raw_config}}), encoding="utf-8")
    auth_scopes: list[MCPConfigScope | str | None] = []

    def fake_auth(
        config: MCPServerConfig,
        *,
        storage: MCPSecretStorage,
        scope: MCPConfigScope | str | None = None,
        server_name: str,
    ) -> None:
        _ = server_name
        auth_scopes.append(scope)
        storage.set_secret(oauth_storage_key(config, "access_token", scope=scope), "access")

    monkeypatch.setattr(mcp_cli, "_run_cli_oauth_flow", fake_auth)

    runner = CliRunner()
    authenticated = runner.invoke(
        app,
        ["mcp", "auth", "remote", "--scope", "project", "--source-path", ".mcp.json"],
    )

    expected_scope = "project:{}".format(project_file.as_posix())
    assert authenticated.exit_code == 0, authenticated.output
    assert auth_scopes == [expected_scope]
    config = MCPServerConfig.from_mapping("remote", raw_config)
    storage = MCPSecretStorage()
    assert storage.get_secret(oauth_storage_key(config, "access_token", scope=expected_scope)) == "access"

    reset = runner.invoke(
        app,
        ["mcp", "reset-auth", "remote", "--scope", "project", "--source-path", ".mcp.json"],
    )

    assert reset.exit_code == 0, reset.output
    assert storage.get_secret(oauth_storage_key(config, "access_token", scope=expected_scope)) is None


def test_mcp_add_rejects_plaintext_secret_headers_and_env(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    runner = CliRunner()

    header_result = runner.invoke(
        app,
        [
            "mcp",
            "add",
            "remote",
            "--type",
            "http",
            "--url",
            "https://example.com/mcp",
            "--header",
            "Authorization=Bearer plain-token",
            "--scope",
            "user",
        ],
    )

    assert header_result.exit_code != 0
    assert "environment variable reference" in header_result.output
    assert not (tmp_path / "config" / "settings.yml").exists()

    env_result = runner.invoke(
        app,
        [
            "mcp",
            "add",
            "local",
            "--command",
            "uvx",
            "--env",
            "API_TOKEN=plain-token",
            "--scope",
            "user",
        ],
    )

    assert env_result.exit_code != 0
    assert "environment variable reference" in env_result.output
    assert not (tmp_path / "config" / "settings.yml").exists()

    json_result = runner.invoke(
        app,
        [
            "mcp",
            "add-json",
            "json-remote",
            json.dumps(
                {
                    "type": "http",
                    "url": "https://example.com/mcp",
                    "headers": {"Authorization": "Bearer plain-token"},
                }
            ),
            "--scope",
            "user",
        ],
    )

    assert json_result.exit_code != 0
    assert "environment variable reference" in json_result.output
    assert not (tmp_path / "config" / "settings.yml").exists()


def test_mcp_add_rejects_secret_like_env_default_in_non_sensitive_fields(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    runner = CliRunner()

    env_result = runner.invoke(
        app,
        [
            "mcp",
            "add",
            "local",
            "--command",
            "uvx",
            "--env",
            "TEAM=${SAFE_TEAM:-api_key=plain-secret}",
            "--scope",
            "user",
        ],
    )

    assert env_result.exit_code != 0
    assert "environment variable reference" in env_result.output
    assert not (tmp_path / "config" / "settings.yml").exists()

    json_result = runner.invoke(
        app,
        [
            "mcp",
            "add-json",
            "json-local",
            json.dumps({"command": "uvx", "env": {"TEAM": "${SAFE_TEAM:-api_key=plain-secret}"}}),
            "--scope",
            "user",
        ],
    )

    assert json_result.exit_code != 0
    assert "environment variable reference" in json_result.output
    assert not (tmp_path / "config" / "settings.yml").exists()


def test_mcp_add_stores_direct_client_secret_outside_config(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("IAC_CODE_MCP_DISABLE_KEYRING", "1")
    runner = CliRunner()

    result = runner.invoke(
        app,
        [
            "mcp",
            "add",
            "remote",
            "--type",
            "http",
            "--url",
            "https://example.com/mcp",
            "--client-id",
            "client-id",
            "--client-secret",
            "super-secret",
            "--scope",
            "user",
        ],
    )

    assert result.exit_code == 0, result.output
    settings_text = (tmp_path / "config" / "settings.yml").read_text(encoding="utf-8")
    assert "super-secret" not in settings_text
    config = MCPServerConfig.from_mapping(
        "remote",
        yaml.safe_load(settings_text)["mcpServers"]["remote"],
    )
    assert (
        MCPSecretStorage().get_secret(oauth_storage_key(config, "client_secret", scope=MCPConfigScope.USER))
        == "super-secret"
    )


def test_mcp_add_direct_client_secret_is_available_to_env_expanded_auth(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("IAC_CODE_MCP_DISABLE_KEYRING", "1")
    monkeypatch.setenv("IAC_CODE_MCP_URL", "https://expanded.example/mcp")
    runner = CliRunner()

    result = runner.invoke(
        app,
        [
            "mcp",
            "add",
            "remote",
            "--type",
            "http",
            "--url",
            "${IAC_CODE_MCP_URL}",
            "--client-id",
            "client-id",
            "--client-secret",
            "super-secret",
            "--scope",
            "user",
        ],
    )

    assert result.exit_code == 0, result.output
    load_result = load_exact_mcp_config("remote", scope=MCPConfigScope.USER, cwd=tmp_path)
    assert load_result.servers
    expanded_config = load_result.servers[0].config
    storage = MCPSecretStorage()
    assert (
        storage.get_secret(oauth_storage_key(expanded_config, "client_secret", scope=MCPConfigScope.USER))
        == "super-secret"
    )
    captured: list[str | None] = []

    def run_flow(
        config: MCPServerConfig,
        *,
        storage: MCPSecretStorage,
        scope: MCPConfigScope | str | None,
        server_name: str,
        **kwargs: object,
    ) -> None:
        _ = server_name, kwargs
        captured.append(storage.get_secret(oauth_storage_key(config, "client_secret", scope=scope)))

    monkeypatch.setattr(mcp_cli, "_run_cli_oauth_flow", run_flow)

    auth = runner.invoke(app, ["mcp", "auth", "remote", "--scope", "user"])

    assert auth.exit_code == 0, auth.output
    assert captured == ["super-secret"]


def test_mcp_add_prompts_for_client_secret_when_option_has_no_value(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("IAC_CODE_MCP_DISABLE_KEYRING", "1")
    runner = CliRunner()

    result = runner.invoke(
        app,
        [
            "mcp",
            "add",
            "remote",
            "--type",
            "http",
            "--url",
            "https://example.com/mcp",
            "--client-id",
            "client-id",
            "--scope",
            "user",
            "--client-secret",
        ],
        input="prompted-secret\n",
    )

    assert result.exit_code == 0, result.output
    settings_text = (tmp_path / "config" / "settings.yml").read_text(encoding="utf-8")
    assert "prompted-secret" not in settings_text
    config = MCPServerConfig.from_mapping(
        "remote",
        yaml.safe_load(settings_text)["mcpServers"]["remote"],
    )
    assert (
        MCPSecretStorage().get_secret(oauth_storage_key(config, "client_secret", scope=MCPConfigScope.USER))
        == "prompted-secret"
    )


def test_mcp_add_defaults_to_user_scope_outside_git_project(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    runner = CliRunner()

    result = runner.invoke(app, ["mcp", "add", "global-server", "--command", "uvx"])

    assert result.exit_code == 0, result.output
    assert (tmp_path / "config" / "settings.yml").exists()
    assert not (tmp_path / ".iac-code" / "settings.local.yml").exists()
    assert "to user config" in result.output
    assert "iac-code mcp auth" not in result.output


def test_mcp_add_defaults_to_local_scope_inside_project(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    (tmp_path / ".git").mkdir()
    runner = CliRunner()

    result = runner.invoke(app, ["mcp", "add", "project-server", "--command", "uvx"])

    assert result.exit_code == 0, result.output
    assert (tmp_path / ".iac-code" / "settings.local.yml").exists()
    assert not (tmp_path / "config" / "settings.yml").exists()
    assert "to local config" in result.output
    assert "iac-code mcp auth" not in result.output


def test_mcp_add_warns_for_bare_npx_on_windows(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setattr(sys, "platform", "win32")
    runner = CliRunner()

    result = runner.invoke(app, ["mcp", "add", "node-server", "--command", "npx", "--scope", "user"])

    assert result.exit_code == 0, result.output
    assert "cmd /c npx" in result.output


def test_mcp_project_approval_commands(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    (tmp_path / ".mcp.json").write_text('{"mcpServers": {"pending": {"command": "uvx"}}}', encoding="utf-8")
    runner = CliRunner()

    assert load_mcp_configs(cwd=tmp_path, workspace_root=tmp_path, env={}).servers == []

    approved = runner.invoke(app, ["mcp", "approve", "pending"])
    assert approved.exit_code == 0, approved.output
    assert [server.name for server in load_mcp_configs(cwd=tmp_path, workspace_root=tmp_path, env={}).servers] == [
        "pending"
    ]

    rejected = runner.invoke(app, ["mcp", "reject", "pending"])
    assert rejected.exit_code == 0, rejected.output
    assert load_mcp_configs(cwd=tmp_path, workspace_root=tmp_path, env={}).servers == []

    reset = runner.invoke(app, ["mcp", "reset-project-choices"])
    assert reset.exit_code == 0, reset.output
    assert "Reset" in reset.output


def test_mcp_project_approval_invalid_config_reports_clean_error(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    (tmp_path / ".mcp.json").write_text(
        '{"mcpServers": {"project-live": {"command": ""}}}',
        encoding="utf-8",
    )
    runner = CliRunner()

    for command in ("approve", "reject"):
        result = runner.invoke(app, ["mcp", command, "project-live"])
        assert result.exit_code != 0
        assert "requires a command string" in result.output
        assert "Traceback" not in result.output


def test_mcp_project_approval_from_child_directory_uses_workspace_root(monkeypatch, tmp_path: Path) -> None:
    root = tmp_path / "repo"
    child = root / "nested"
    child.mkdir(parents=True)
    (root / ".git").mkdir()
    (root / ".mcp.json").write_text('{"mcpServers": {"pending": {"command": "uvx"}}}', encoding="utf-8")
    monkeypatch.chdir(child)
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    runner = CliRunner()

    approved = runner.invoke(app, ["mcp", "approve", "pending"])

    assert approved.exit_code == 0, approved.output
    assert [server.name for server in load_mcp_configs(cwd=child, workspace_root=root, env={}).servers] == ["pending"]


def test_mcp_auth_exchanges_loopback_code_and_stores_token(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("IAC_CODE_MCP_DISABLE_KEYRING", "1")
    oauth_server = FakeOAuthServer()
    monkeypatch.setattr(
        oauth_module,
        "discover_oauth_metadata",
        lambda config, resource_metadata_url=None: _fake_oauth_metadata(oauth_server),
    )
    callback_port = _free_port()
    runner = CliRunner()
    runner.invoke(
        app,
        [
            "mcp",
            "add",
            "remote",
            "--type",
            "http",
            "--url",
            "https://resource.example/mcp",
            "--client-id",
            "client-id",
            "--callback-port",
            str(callback_port),
            "--auth-server-metadata-url",
            oauth_server.metadata_url,
            "--scope",
            "user",
        ],
    )

    def open_browser(url: str) -> bool:
        urllib.request.urlopen(url, timeout=5).read()
        return True

    monkeypatch.delenv("BROWSER", raising=False)
    monkeypatch.setitem(sys.modules, "webbrowser", type("FakeWebBrowser", (), {"open": staticmethod(open_browser)}))

    result = runner.invoke(app, ["mcp", "auth", "remote", "--scope", "user"])

    assert result.exit_code == 0, result.output
    assert "access-token" not in result.output
    settings = yaml.safe_load((tmp_path / "config" / "settings.yml").read_text(encoding="utf-8"))
    config = MCPServerConfig.from_mapping("remote", settings["mcpServers"]["remote"])
    storage = MCPSecretStorage()
    assert storage.get_secret(oauth_storage_key(config, "access_token", scope=MCPConfigScope.USER)) == "access-token"
    assert storage.get_secret(oauth_storage_key(config, "refresh_token", scope=MCPConfigScope.USER)) == "refresh-token"
    assert oauth_server.last_token_request["code"] == ["code-1"]
    assert oauth_server.last_token_request["client_id"] == ["client-id"]


def test_mcp_auth_helper_passes_required_step_up_scopes(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("IAC_CODE_MCP_DISABLE_KEYRING", "1")
    runner = CliRunner()
    added = runner.invoke(
        app,
        [
            "mcp",
            "add",
            "remote",
            "--type",
            "http",
            "--url",
            "https://resource.example/mcp",
            "--scope",
            "user",
        ],
    )
    assert added.exit_code == 0, added.output
    required_scope_calls: list[list[str]] = []

    def fake_flow(
        config: MCPServerConfig,
        *,
        storage: MCPSecretStorage,
        scope: MCPConfigScope | str | None = None,
        server_name: str,
        required_scopes: list[str] | tuple[str, ...] | None = None,
    ) -> None:
        _ = server_name
        required_scope_calls.append(list(required_scopes or []))
        storage.set_secret(oauth_storage_key(config, "access_token", scope=scope), "access")

    monkeypatch.setattr(mcp_cli, "_run_cli_oauth_flow", fake_flow)

    result = mcp_cli.authenticate_mcp_server("remote", scope="user", required_scopes=["doc:read"])

    assert result == "Authenticated MCP server 'remote'."
    assert required_scope_calls == [["doc:read"]]


def test_mcp_auth_uses_passed_cwd_when_loading_local_config(monkeypatch, tmp_path: Path) -> None:
    project_a = tmp_path / "project-a"
    project_b = tmp_path / "project-b"
    project_a.mkdir()
    project_b.mkdir()
    for project, url in (
        (project_a, "https://project-a.example/mcp"),
        (project_b, "https://project-b.example/mcp"),
    ):
        settings_path = project / ".iac-code" / "settings.local.yml"
        settings_path.parent.mkdir(parents=True)
        settings_path.write_text(
            yaml.safe_dump({"mcpServers": {"remote": {"type": "http", "url": url}}}, sort_keys=True),
            encoding="utf-8",
        )
    monkeypatch.chdir(project_b)
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    captured_urls: list[str] = []

    def fake_flow(
        config: MCPServerConfig,
        *,
        storage: MCPSecretStorage,
        scope: MCPConfigScope | str | None = None,
        server_name: str,
    ) -> None:
        _ = storage, scope, server_name
        captured_urls.append(config.url or "")

    monkeypatch.setattr(mcp_cli, "_run_cli_oauth_flow", fake_flow)

    result = mcp_cli.authenticate_mcp_server("remote", scope="local", cwd=project_a)

    assert result == "Authenticated MCP server 'remote'."
    assert captured_urls == ["https://project-a.example/mcp"]


def test_mcp_auth_failure_preserves_existing_oauth_state(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    runner = CliRunner()
    added = runner.invoke(
        app,
        [
            "mcp",
            "add",
            "remote",
            "--type",
            "http",
            "--url",
            "https://resource.example/mcp",
            "--scope",
            "user",
        ],
    )
    assert added.exit_code == 0, added.output
    clear_calls: list[str] = []

    def failing_flow(*args, **kwargs) -> None:
        raise RuntimeError("metadata temporarily unavailable")

    monkeypatch.setattr(mcp_cli, "_run_cli_oauth_flow", failing_flow)
    monkeypatch.setattr(
        mcp_cli,
        "clear_oauth_state",
        lambda config, *, storage, scope=None, revoke=True: clear_calls.append(str(scope)),
    )

    with pytest.raises(typer.Exit):
        mcp_cli.authenticate_mcp_server("remote", scope="user")

    assert clear_calls == []


def test_mcp_start_oauth_flow_passes_step_up_resource_metadata_url(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    runner = CliRunner()
    added = runner.invoke(
        app,
        [
            "mcp",
            "add",
            "remote",
            "--type",
            "http",
            "--url",
            "https://resource.example/mcp",
            "--scope",
            "user",
        ],
    )
    assert added.exit_code == 0, added.output
    captured: dict[str, object] = {}

    def fake_start_oauth_loopback_flow(
        config: MCPServerConfig,
        *,
        storage: MCPSecretStorage,
        scope: MCPConfigScope | str | None = None,
        required_scopes: list[str] | tuple[str, ...] | None = None,
        resource_metadata_url: str | None = None,
    ) -> object:
        captured["config"] = config
        captured["storage"] = storage
        captured["scope"] = scope
        captured["required_scopes"] = list(required_scopes or [])
        captured["resource_metadata_url"] = resource_metadata_url
        return object()

    monkeypatch.setattr(mcp_cli, "start_oauth_loopback_flow", fake_start_oauth_loopback_flow)

    pending = mcp_cli.start_mcp_oauth_flow(
        "remote",
        scope="user",
        required_scopes=["doc:read"],
        resource_metadata_url="https://resource.example/.well-known/oauth-protected-resource/mcp",
    )

    assert pending is not None
    assert captured["scope"] == MCPConfigScope.USER
    assert captured["required_scopes"] == ["doc:read"]
    assert captured["resource_metadata_url"] == "https://resource.example/.well-known/oauth-protected-resource/mcp"


def test_mcp_start_oauth_flow_failure_preserves_existing_oauth_state(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    runner = CliRunner()
    added = runner.invoke(
        app,
        [
            "mcp",
            "add",
            "remote",
            "--type",
            "http",
            "--url",
            "https://resource.example/mcp",
            "--scope",
            "user",
        ],
    )
    assert added.exit_code == 0, added.output
    clear_calls: list[str] = []

    def failing_start(*args, **kwargs) -> object:
        raise RuntimeError("loopback unavailable")

    monkeypatch.setattr(mcp_cli, "start_oauth_loopback_flow", failing_start)
    monkeypatch.setattr(
        mcp_cli,
        "clear_oauth_state",
        lambda config, *, storage, scope=None, revoke=True: clear_calls.append(str(scope)),
    )

    with pytest.raises(RuntimeError, match="loopback unavailable"):
        mcp_cli.start_mcp_oauth_flow("remote", scope="user")

    assert clear_calls == []


def test_mcp_pending_oauth_cancel_restores_existing_oauth_state(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("IAC_CODE_MCP_DISABLE_KEYRING", "1")
    runner = CliRunner()
    added = runner.invoke(
        app,
        [
            "mcp",
            "add",
            "remote",
            "--type",
            "http",
            "--url",
            "https://resource.example/mcp",
            "--scope",
            "user",
        ],
    )
    assert added.exit_code == 0, added.output
    config = _user_mcp_config(tmp_path, "remote")
    storage = MCPSecretStorage()
    _store_oauth_state(config, storage, scope=MCPConfigScope.USER)

    class Pending:
        authorization_url = "https://auth.example/authorize"
        browser_opened = False

        def __init__(
            self,
            config: MCPServerConfig,
            storage: MCPSecretStorage,
            scope: MCPConfigScope | str | None,
        ) -> None:
            self.config = config
            self.storage = storage
            self.scope = scope
            self.closed = False

        def close(self) -> None:
            self.closed = True

    def start_flow(
        config: MCPServerConfig,
        *,
        storage: MCPSecretStorage,
        scope: MCPConfigScope | str | None = None,
    ):
        storage.set_secret(oauth_storage_key(config, "access_token", scope=scope), "partial-access")
        storage.set_secret(oauth_storage_key(config, "client_secret", scope=scope), "partial-client-secret")
        return Pending(config, storage, scope)

    monkeypatch.setattr(mcp_cli, "start_oauth_loopback_flow", start_flow)

    pending = mcp_cli.start_mcp_oauth_flow("remote", scope="user")
    mcp_cli.cancel_pending_mcp_oauth_flow(pending)

    assert pending.closed is True
    assert storage.get_secret(oauth_storage_key(config, "access_token", scope=MCPConfigScope.USER)) == "stored-access"
    assert (
        storage.get_secret(oauth_storage_key(config, "client_secret", scope=MCPConfigScope.USER))
        == "registered-client-secret"
    )


def test_mcp_auth_uses_env_expanded_oauth_config(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("IAC_CODE_MCP_CLIENT_METADATA_URL", "https://metadata.example.com/client.json")
    settings_path = tmp_path / "config" / "settings.yml"
    settings_path.parent.mkdir()
    settings_path.write_text(
        yaml.safe_dump(
            {
                "mcpServers": {
                    "remote": {
                        "type": "http",
                        "url": "https://resource.example/mcp",
                        "oauth": {"clientMetadataUrl": "${IAC_CODE_MCP_CLIENT_METADATA_URL}"},
                    }
                }
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    captured: list[str | None] = []

    def run_flow(
        config: MCPServerConfig,
        *,
        storage: MCPSecretStorage,
        scope: MCPConfigScope | str | None,
        server_name: str,
        **kwargs: object,
    ) -> None:
        _ = storage, scope, server_name, kwargs
        captured.append(config.oauth.client_metadata_url if config.oauth else None)

    monkeypatch.setattr(mcp_cli, "_run_cli_oauth_flow", run_flow)

    result = CliRunner().invoke(app, ["mcp", "auth", "remote", "--scope", "user"])

    assert result.exit_code == 0, result.output
    assert captured == ["https://metadata.example.com/client.json"]


def test_mcp_auth_registers_dynamic_client_and_stores_client_secret(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("IAC_CODE_MCP_DISABLE_KEYRING", "1")
    oauth_server = FakeOAuthServer()
    monkeypatch.setattr(
        oauth_module,
        "discover_oauth_metadata",
        lambda config, resource_metadata_url=None: _fake_oauth_metadata(oauth_server),
    )
    callback_port = _free_port()
    runner = CliRunner()
    added = runner.invoke(
        app,
        [
            "mcp",
            "add",
            "remote",
            "--type",
            "http",
            "--url",
            "https://resource.example/mcp",
            "--callback-port",
            str(callback_port),
            "--auth-server-metadata-url",
            oauth_server.metadata_url,
            "--scope",
            "user",
        ],
    )
    assert added.exit_code == 0, added.output

    def open_browser(url: str) -> bool:
        urllib.request.urlopen(url, timeout=5).read()
        return True

    monkeypatch.delenv("BROWSER", raising=False)
    monkeypatch.setitem(sys.modules, "webbrowser", type("FakeWebBrowser", (), {"open": staticmethod(open_browser)}))

    result = runner.invoke(app, ["mcp", "auth", "remote", "--scope", "user"])

    assert result.exit_code == 0, result.output
    assert "registered-client-secret" not in result.output
    settings_text = (tmp_path / "config" / "settings.yml").read_text(encoding="utf-8")
    assert "registered-client-secret" not in settings_text
    settings = yaml.safe_load(settings_text)
    config = MCPServerConfig.from_mapping("remote", settings["mcpServers"]["remote"])
    storage = MCPSecretStorage()
    assert storage.get_secret(oauth_storage_key(config, "client_id", scope=MCPConfigScope.USER)) == "registered-client"
    assert (
        storage.get_secret(oauth_storage_key(config, "client_secret", scope=MCPConfigScope.USER))
        == "registered-client-secret"
    )
    assert oauth_server.last_registration_request is not None
    assert oauth_server.last_registration_request["client_name"] == "IaC Code"
    assert oauth_server.last_registration_request["redirect_uris"] == [
        oauth_server.last_token_request["redirect_uri"][0]
    ]
    assert oauth_server.last_registration_request["token_endpoint_auth_method"] == "none"
    assert oauth_server.last_registration_request["scope"] == "mcp"
    assert oauth_server.last_token_request["client_id"] == ["registered-client"]


def test_mcp_reset_auth_clears_stored_tokens(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("IAC_CODE_MCP_DISABLE_KEYRING", "1")
    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "mcp",
            "add",
            "remote",
            "--type",
            "http",
            "--url",
            "https://example.com/mcp",
            "--client-id",
            "client-id",
            "--scope",
            "user",
        ],
    )
    assert result.exit_code == 0, result.output
    config = _user_mcp_config(tmp_path, "remote")
    storage = MCPSecretStorage()
    _store_oauth_state(config, storage, scope=MCPConfigScope.USER)

    reset = runner.invoke(app, ["mcp", "reset-auth", "remote", "--scope", "user"])

    assert reset.exit_code == 0, reset.output
    settings = yaml.safe_load((tmp_path / "config" / "settings.yml").read_text(encoding="utf-8"))
    assert "remote" in settings["mcpServers"]
    _assert_no_oauth_state(config, storage, scope=MCPConfigScope.USER)


def test_mcp_reset_auth_clears_stored_tokens_after_config_becomes_scalar_invalid(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("IAC_CODE_MCP_DISABLE_KEYRING", "1")
    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "mcp",
            "add",
            "remote",
            "--type",
            "http",
            "--url",
            "https://example.com/mcp",
            "--client-id",
            "client-id",
            "--scope",
            "user",
        ],
    )
    assert result.exit_code == 0, result.output
    config = _user_mcp_config(tmp_path, "remote")
    storage = MCPSecretStorage()
    _store_oauth_state(config, storage, scope=MCPConfigScope.USER)
    settings_path = tmp_path / "config" / "settings.yml"
    settings_path.write_text(yaml.safe_dump({"mcpServers": {"remote": "not-an-object"}}), encoding="utf-8")

    reset = runner.invoke(app, ["mcp", "reset-auth", "remote", "--scope", "user"])

    assert reset.exit_code == 0, reset.output
    assert "Reset stored MCP auth state for 'remote'." in reset.output
    _assert_no_oauth_state(config, storage, scope=MCPConfigScope.USER)


@pytest.mark.timeout(90)
def test_mcp_reset_auth_and_remove_clear_env_expanded_oauth_state(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("IAC_CODE_MCP_DISABLE_KEYRING", "1")
    monkeypatch.setenv("IAC_CODE_MCP_URL", "https://expanded.example/mcp")
    settings_path = tmp_path / "config" / "settings.yml"
    settings_path.parent.mkdir()
    settings_path.write_text(
        yaml.safe_dump(
            {
                "mcpServers": {
                    "remote": {
                        "type": "http",
                        "url": "${IAC_CODE_MCP_URL}",
                        "oauth": {"clientId": "client-id"},
                    }
                }
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    raw_config = _user_mcp_config(tmp_path, "remote")
    load_result = load_exact_mcp_config("remote", scope=MCPConfigScope.USER, cwd=tmp_path)
    assert load_result.servers
    expanded_config = load_result.servers[0].config
    assert expanded_config.url == "https://expanded.example/mcp"
    monkeypatch.setattr(mcp_cli, "revoke_oauth_stored_tokens", lambda *_args, **_kwargs: [])
    storage = MCPSecretStorage()
    _store_oauth_state(raw_config, storage, scope=MCPConfigScope.USER)
    _store_oauth_state(expanded_config, storage, scope=MCPConfigScope.USER)
    runner = CliRunner()
    monkeypatch.delenv("IAC_CODE_MCP_URL")

    reset = runner.invoke(app, ["mcp", "reset-auth", "remote", "--scope", "user"])

    assert reset.exit_code == 0, reset.output
    _assert_no_oauth_state(raw_config, storage, scope=MCPConfigScope.USER)
    _assert_no_oauth_state(expanded_config, storage, scope=MCPConfigScope.USER)

    _store_oauth_state(raw_config, storage, scope=MCPConfigScope.USER)
    _store_oauth_state(expanded_config, storage, scope=MCPConfigScope.USER)
    monkeypatch.setenv("IAC_CODE_MCP_URL", "https://different.example/mcp")
    removed = runner.invoke(app, ["mcp", "remove", "remote", "--scope", "user"])

    assert removed.exit_code == 0, removed.output
    _assert_no_oauth_state(raw_config, storage, scope=MCPConfigScope.USER)
    _assert_no_oauth_state(expanded_config, storage, scope=MCPConfigScope.USER)


def test_mcp_reset_auth_and_remove_emit_revocation_warnings(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("IAC_CODE_MCP_DISABLE_KEYRING", "1")
    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "mcp",
            "add",
            "remote",
            "--type",
            "http",
            "--url",
            "https://example.com/mcp",
            "--client-id",
            "client-id",
            "--scope",
            "user",
        ],
    )
    assert result.exit_code == 0, result.output
    config = _user_mcp_config(tmp_path, "remote")
    storage = MCPSecretStorage()
    _store_oauth_state(config, storage, scope=MCPConfigScope.USER)
    revocations: list[tuple[str, MCPConfigScope | str | None]] = []

    def revoke_oauth_stored_tokens(
        config: MCPServerConfig,
        *,
        storage: MCPSecretStorage,
        scope: MCPConfigScope | str | None = None,
    ) -> list[str]:
        revocations.append((config.url or "", scope))
        return ["OAuth token revocation failed for MCP server 'remote': [REDACTED]"]

    monkeypatch.setattr(mcp_cli, "revoke_oauth_stored_tokens", revoke_oauth_stored_tokens)

    reset = runner.invoke(app, ["mcp", "reset-auth", "remote", "--scope", "user"])

    assert reset.exit_code == 0, reset.output
    assert "Reset stored MCP auth state for 'remote'." in reset.output
    assert "Warning: OAuth token revocation failed for MCP server 'remote': [REDACTED]" in reset.output
    _assert_no_oauth_state(config, storage, scope=MCPConfigScope.USER)

    _store_oauth_state(config, storage, scope=MCPConfigScope.USER)
    removed = runner.invoke(app, ["mcp", "remove", "remote", "--scope", "user"])

    assert removed.exit_code == 0, removed.output
    assert "Removed MCP server 'remote'" in removed.output
    assert "Warning: OAuth token revocation failed for MCP server 'remote': [REDACTED]" in removed.output
    assert revocations == [
        ("https://example.com/mcp", MCPConfigScope.USER),
        ("https://example.com/mcp", MCPConfigScope.USER),
    ]
    _assert_no_oauth_state(config, storage, scope=MCPConfigScope.USER)


def test_mcp_manual_auth_browser_failure_prints_authorization_url(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("IAC_CODE_MCP_DISABLE_KEYRING", "1")
    oauth_server = FakeOAuthServer()
    monkeypatch.setattr(
        oauth_module,
        "discover_oauth_metadata",
        lambda config, resource_metadata_url=None: _fake_oauth_metadata(oauth_server),
    )
    callback_port = _free_port()
    runner = CliRunner()
    added = runner.invoke(
        app,
        [
            "mcp",
            "add",
            "yuque",
            "--type",
            "http",
            "--url",
            "https://resource.example/mcp",
            "--client-id",
            "client-id",
            "--callback-port",
            str(callback_port),
            "--auth-server-metadata-url",
            oauth_server.metadata_url,
            "--scope",
            "user",
        ],
    )
    assert added.exit_code == 0, added.output
    opened_urls: list[str] = []

    def open_browser(url: str) -> bool:
        opened_urls.append(url)
        return False

    def wait_for_code(self, timeout_seconds: float) -> str:
        raise TimeoutError("test callback timeout")

    monkeypatch.delenv("BROWSER", raising=False)
    monkeypatch.setitem(sys.modules, "webbrowser", type("FakeWebBrowser", (), {"open": staticmethod(open_browser)}))
    monkeypatch.setattr(oauth_module._LoopbackCallback, "wait_for_code", wait_for_code)

    result = runner.invoke(app, ["mcp", "auth", "yuque", "--scope", "user"])

    assert result.exit_code == 1
    assert opened_urls
    assert "Browser opened: no" in result.output
    assert opened_urls[0] in result.output
    assert "MCP auth failed for 'yuque'" in result.output
    config = _user_mcp_config(tmp_path, "yuque")
    _assert_no_oauth_state(config, MCPSecretStorage(), scope=MCPConfigScope.USER)


def _failing_browser_command(tmp_path: Path) -> tuple[str, Path]:
    browser = tmp_path / "fail_browser.py"
    log_path = tmp_path / "browser-url.txt"
    browser.write_text(
        "\n".join(
            [
                "from pathlib import Path",
                "import sys",
                "Path(sys.argv[1]).write_text(sys.argv[2], encoding='utf-8')",
                "sys.exit(1)",
            ]
        ),
        encoding="utf-8",
    )
    return (
        "{} {} {} %s".format(
            shlex.quote(sys.executable),
            shlex.quote(str(browser)),
            shlex.quote(str(log_path)),
        ),
        log_path,
    )


def test_mcp_manual_auth_browser_env_failure_prints_authorization_url(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("IAC_CODE_MCP_DISABLE_KEYRING", "1")
    browser_command, browser_log = _failing_browser_command(tmp_path)
    monkeypatch.setenv("BROWSER", browser_command)
    oauth_server = FakeOAuthServer()
    monkeypatch.setattr(
        oauth_module,
        "discover_oauth_metadata",
        lambda config, resource_metadata_url=None: _fake_oauth_metadata(oauth_server),
    )
    callback_port = _free_port()
    runner = CliRunner()
    added = runner.invoke(
        app,
        [
            "mcp",
            "add",
            "yuque",
            "--type",
            "http",
            "--url",
            "https://resource.example/mcp",
            "--client-id",
            "client-id",
            "--callback-port",
            str(callback_port),
            "--auth-server-metadata-url",
            oauth_server.metadata_url,
            "--scope",
            "user",
        ],
    )
    assert added.exit_code == 0, added.output

    def wait_for_code(self, timeout_seconds: float) -> str:
        raise TimeoutError("test callback timeout")

    monkeypatch.setattr(oauth_module._LoopbackCallback, "wait_for_code", wait_for_code)

    result = runner.invoke(app, ["mcp", "auth", "yuque", "--scope", "user"])

    assert result.exit_code == 1
    authorization_url = browser_log.read_text(encoding="utf-8")
    assert "/authorize?" in authorization_url
    assert "Browser opened: no" in result.output
    assert authorization_url in result.output
    assert "MCP auth failed for 'yuque'" in result.output


def test_mcp_manual_auth_accepts_authorization_code_when_browser_cannot_open(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("IAC_CODE_MCP_DISABLE_KEYRING", "1")
    oauth_server = FakeOAuthServer()
    monkeypatch.setattr(
        oauth_module,
        "discover_oauth_metadata",
        lambda config, resource_metadata_url=None: _fake_oauth_metadata(oauth_server),
    )
    callback_port = _free_port()
    runner = CliRunner()
    added = runner.invoke(
        app,
        [
            "mcp",
            "add",
            "yuque",
            "--type",
            "http",
            "--url",
            "https://resource.example/mcp",
            "--client-id",
            "client-id",
            "--callback-port",
            str(callback_port),
            "--auth-server-metadata-url",
            oauth_server.metadata_url,
            "--scope",
            "user",
        ],
    )
    assert added.exit_code == 0, added.output
    opened_urls: list[str] = []

    def open_browser(url: str) -> bool:
        opened_urls.append(url)
        return False

    def wait_for_code(self, timeout_seconds: float) -> str:
        raise TimeoutError("loopback should not be required for manual code input")

    monkeypatch.delenv("BROWSER", raising=False)
    monkeypatch.setitem(sys.modules, "webbrowser", type("FakeWebBrowser", (), {"open": staticmethod(open_browser)}))
    monkeypatch.setattr(oauth_module._LoopbackCallback, "wait_for_code", wait_for_code)

    result = runner.invoke(app, ["mcp", "auth", "yuque", "--scope", "user"], input="manual-code\n")

    assert result.exit_code == 0, result.output
    assert opened_urls
    assert "Paste the callback URL or authorization code" in result.output
    assert opened_urls[0] in result.output
    assert "access-token" not in result.output
    assert oauth_server.last_token_request["code"] == ["manual-code"]
    config = _user_mcp_config(tmp_path, "yuque")
    storage = MCPSecretStorage()
    assert storage.get_secret(oauth_storage_key(config, "access_token", scope=MCPConfigScope.USER)) == "access-token"
    assert storage.get_secret(oauth_storage_key(config, "refresh_token", scope=MCPConfigScope.USER)) == "refresh-token"


def test_mcp_manual_auth_accepts_authorization_code_for_dynamic_client(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("IAC_CODE_MCP_DISABLE_KEYRING", "1")
    oauth_server = FakeOAuthServer()
    monkeypatch.setattr(
        oauth_module,
        "discover_oauth_metadata",
        lambda config, resource_metadata_url=None: _fake_oauth_metadata(oauth_server),
    )
    callback_port = _free_port()
    runner = CliRunner()
    added = runner.invoke(
        app,
        [
            "mcp",
            "add",
            "yuque",
            "--type",
            "http",
            "--url",
            "https://resource.example/mcp",
            "--callback-port",
            str(callback_port),
            "--auth-server-metadata-url",
            oauth_server.metadata_url,
            "--scope",
            "user",
        ],
    )
    assert added.exit_code == 0, added.output

    def open_browser(url: str) -> bool:
        return False

    monkeypatch.delenv("BROWSER", raising=False)
    monkeypatch.setitem(sys.modules, "webbrowser", type("FakeWebBrowser", (), {"open": staticmethod(open_browser)}))

    result = runner.invoke(app, ["mcp", "auth", "yuque", "--scope", "user"], input="manual-code\n")

    assert result.exit_code == 0, result.output
    assert oauth_server.last_registration_request is not None
    assert oauth_server.last_token_request["code"] == ["manual-code"]
    assert oauth_server.last_token_request["client_id"] == ["registered-client"]
    config = _user_mcp_config(tmp_path, "yuque")
    storage = MCPSecretStorage()
    assert storage.get_secret(oauth_storage_key(config, "access_token", scope=MCPConfigScope.USER)) == "access-token"
    assert storage.get_secret(oauth_storage_key(config, "client_id", scope=MCPConfigScope.USER)) == "registered-client"


def test_mcp_manual_auth_timeout_clears_partial_dynamic_client_state(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("IAC_CODE_MCP_DISABLE_KEYRING", "1")
    oauth_server = FakeOAuthServer()
    monkeypatch.setattr(
        oauth_module,
        "discover_oauth_metadata",
        lambda config, resource_metadata_url=None: _fake_oauth_metadata(oauth_server),
    )
    callback_port = _free_port()
    runner = CliRunner()
    added = runner.invoke(
        app,
        [
            "mcp",
            "add",
            "yuque",
            "--type",
            "http",
            "--url",
            "https://resource.example/mcp",
            "--callback-port",
            str(callback_port),
            "--auth-server-metadata-url",
            oauth_server.metadata_url,
            "--scope",
            "user",
        ],
    )
    assert added.exit_code == 0, added.output

    def open_browser(url: str) -> bool:
        return False

    def wait_for_code_and_state(self, timeout_seconds: float) -> tuple[str, str | None]:
        raise TimeoutError("callback timed out")

    monkeypatch.delenv("BROWSER", raising=False)
    monkeypatch.setitem(sys.modules, "webbrowser", type("FakeWebBrowser", (), {"open": staticmethod(open_browser)}))
    monkeypatch.setattr(oauth_module._LoopbackCallback, "wait_for_code_and_state", wait_for_code_and_state)

    result = runner.invoke(app, ["mcp", "auth", "yuque", "--scope", "user"], input="\n")

    assert result.exit_code == 1
    assert "MCP auth failed for 'yuque'" in result.output
    assert "timed out" in result.output
    config = _user_mcp_config(tmp_path, "yuque")
    _assert_no_oauth_state(config, MCPSecretStorage(), scope=MCPConfigScope.USER)


def test_mcp_auth_keyboard_interrupt_during_browser_wait_clears_partial_state(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("IAC_CODE_MCP_DISABLE_KEYRING", "1")
    runner = CliRunner()
    added = runner.invoke(
        app,
        [
            "mcp",
            "add",
            "yuque",
            "--type",
            "http",
            "--url",
            "https://resource.example/mcp",
            "--scope",
            "user",
        ],
    )
    assert added.exit_code == 0, added.output

    class Pending:
        browser_opened = True
        authorization_url = "https://auth.example/authorize"

        def wait(self):
            raise KeyboardInterrupt

    def start_flow(
        config: MCPServerConfig,
        *,
        storage: MCPSecretStorage,
        scope: MCPConfigScope | str | None = None,
    ):
        storage.set_secret(oauth_storage_key(config, "access_token", scope=scope), "partial-access")
        storage.set_secret(oauth_storage_key(config, "client_id", scope=scope), "partial-client")
        return Pending()

    monkeypatch.setattr(mcp_cli, "start_oauth_loopback_flow", start_flow)

    result = runner.invoke(app, ["mcp", "auth", "yuque", "--scope", "user"])

    assert result.exit_code == 1
    assert "Browser opened: yes" in result.output
    assert "MCP auth failed for 'yuque'" in result.output
    assert "cancelled" in result.output
    config = _user_mcp_config(tmp_path, "yuque")
    _assert_no_oauth_state(config, MCPSecretStorage(), scope=MCPConfigScope.USER)


def test_mcp_manual_auth_keyboard_interrupt_after_empty_input_clears_partial_state(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("IAC_CODE_MCP_DISABLE_KEYRING", "1")
    runner = CliRunner()
    added = runner.invoke(
        app,
        [
            "mcp",
            "add",
            "yuque",
            "--type",
            "http",
            "--url",
            "https://resource.example/mcp",
            "--scope",
            "user",
        ],
    )
    assert added.exit_code == 0, added.output

    class Pending:
        browser_opened = False
        authorization_url = "https://auth.example/authorize"

        def wait(self):
            raise KeyboardInterrupt

    def start_flow(
        config: MCPServerConfig,
        *,
        storage: MCPSecretStorage,
        scope: MCPConfigScope | str | None = None,
    ):
        storage.set_secret(oauth_storage_key(config, "access_token", scope=scope), "partial-access")
        storage.set_secret(oauth_storage_key(config, "client_id", scope=scope), "partial-client")
        return Pending()

    monkeypatch.setattr(mcp_cli, "start_oauth_loopback_flow", start_flow)

    result = runner.invoke(app, ["mcp", "auth", "yuque", "--scope", "user"], input="\n")

    assert result.exit_code == 1
    assert "Browser opened: no" in result.output
    assert "Authorization URL: https://auth.example/authorize" in result.output
    assert "MCP auth failed for 'yuque'" in result.output
    assert "cancelled" in result.output
    config = _user_mcp_config(tmp_path, "yuque")
    _assert_no_oauth_state(config, MCPSecretStorage(), scope=MCPConfigScope.USER)


def test_mcp_manual_auth_input_interrupt_closes_pending_flow_and_clears_partial_state(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("IAC_CODE_MCP_DISABLE_KEYRING", "1")
    runner = CliRunner()
    added = runner.invoke(
        app,
        [
            "mcp",
            "add",
            "yuque",
            "--type",
            "http",
            "--url",
            "https://resource.example/mcp",
            "--scope",
            "user",
        ],
    )
    assert added.exit_code == 0, added.output
    closed: list[bool] = []

    class Pending:
        browser_opened = False
        authorization_url = "https://auth.example/authorize"

        def close(self) -> None:
            closed.append(True)

    def start_flow(
        config: MCPServerConfig,
        *,
        storage: MCPSecretStorage,
        scope: MCPConfigScope | str | None = None,
    ):
        storage.set_secret(oauth_storage_key(config, "access_token", scope=scope), "partial-access")
        storage.set_secret(oauth_storage_key(config, "client_id", scope=scope), "partial-client")
        return Pending()

    def interrupted_input() -> str:
        raise KeyboardInterrupt

    monkeypatch.setattr(mcp_cli, "start_oauth_loopback_flow", start_flow)
    monkeypatch.setattr(mcp_cli, "input", interrupted_input, raising=False)

    result = runner.invoke(app, ["mcp", "auth", "yuque", "--scope", "user"])

    assert result.exit_code == 1
    assert "MCP auth failed for 'yuque'" in result.output
    assert "cancelled" in result.output
    assert closed == [True]
    config = _user_mcp_config(tmp_path, "yuque")
    _assert_no_oauth_state(config, MCPSecretStorage(), scope=MCPConfigScope.USER)


def test_mcp_manual_auth_completion_error_closes_pending_flow_and_clears_partial_state(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("IAC_CODE_MCP_DISABLE_KEYRING", "1")
    runner = CliRunner()
    added = runner.invoke(
        app,
        [
            "mcp",
            "add",
            "yuque",
            "--type",
            "http",
            "--url",
            "https://resource.example/mcp",
            "--scope",
            "user",
        ],
    )
    assert added.exit_code == 0, added.output
    closed: list[bool] = []

    class Pending:
        browser_opened = False
        authorization_url = "https://auth.example/authorize"

        def complete_manually(self, callback_or_code: str):
            assert callback_or_code == "http://127.0.0.1/callback?code=abc&state=wrong"
            raise RuntimeError("OAuth callback state did not match.")

        def close(self) -> None:
            closed.append(True)

    def start_flow(
        config: MCPServerConfig,
        *,
        storage: MCPSecretStorage,
        scope: MCPConfigScope | str | None = None,
    ):
        storage.set_secret(oauth_storage_key(config, "access_token", scope=scope), "partial-access")
        storage.set_secret(oauth_storage_key(config, "client_id", scope=scope), "partial-client")
        return Pending()

    monkeypatch.setattr(mcp_cli, "start_oauth_loopback_flow", start_flow)

    result = runner.invoke(
        app,
        ["mcp", "auth", "yuque", "--scope", "user"],
        input="http://127.0.0.1/callback?code=abc&state=wrong\n",
    )

    assert result.exit_code == 1
    assert "MCP auth failed for 'yuque'" in result.output
    assert "OAuth callback state did not match." in result.output
    assert closed == [True]
    config = _user_mcp_config(tmp_path, "yuque")
    _assert_no_oauth_state(config, MCPSecretStorage(), scope=MCPConfigScope.USER)


def test_mcp_manual_auth_completion_error_sanitizes_token_like_message(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("IAC_CODE_MCP_DISABLE_KEYRING", "1")
    runner = CliRunner()
    added = runner.invoke(
        app,
        [
            "mcp",
            "add",
            "yuque",
            "--type",
            "http",
            "--url",
            "https://resource.example/mcp",
            "--scope",
            "user",
        ],
    )
    assert added.exit_code == 0, added.output

    class Pending:
        browser_opened = False
        authorization_url = "https://auth.example/authorize"

        def complete_manually(self, callback_or_code: str):
            assert callback_or_code == "manual-code"
            raise RuntimeError("access_token=super-secret-token")

        def close(self) -> None:
            return None

    def start_flow(
        config: MCPServerConfig,
        *,
        storage: MCPSecretStorage,
        scope: MCPConfigScope | str | None = None,
    ):
        storage.set_secret(oauth_storage_key(config, "access_token", scope=scope), "partial-access")
        return Pending()

    monkeypatch.setattr(mcp_cli, "start_oauth_loopback_flow", start_flow)

    result = runner.invoke(app, ["mcp", "auth", "yuque", "--scope", "user"], input="manual-code\n")

    assert result.exit_code == 1
    assert "MCP auth failed for 'yuque'" in result.output
    assert "super-secret-token" not in result.output
    assert "[REDACTED]" in result.output
    config = _user_mcp_config(tmp_path, "yuque")
    _assert_no_oauth_state(config, MCPSecretStorage(), scope=MCPConfigScope.USER)


def _user_mcp_config(tmp_path: Path, name: str) -> MCPServerConfig:
    settings = yaml.safe_load((tmp_path / "config" / "settings.yml").read_text(encoding="utf-8"))
    return MCPServerConfig.from_mapping(name, settings["mcpServers"][name])


def _store_oauth_state(
    config: MCPServerConfig,
    storage: MCPSecretStorage,
    *,
    scope: MCPConfigScope | str,
) -> None:
    remember_oauth_storage_signature(config, storage=storage, scope=scope)
    for kind, value in {
        "access_token": "stored-access",
        "refresh_token": "stored-refresh",
        "expires_at": "100",
        "refresh_marker": "marker",
        "auth_flow_marker": "flow-marker",
        "client_id": "registered-client",
        "client_secret": "registered-client-secret",
        "client_auth_method": "client_secret_post",
    }.items():
        storage.set_secret(oauth_storage_key(config, kind, scope=scope), value)


def _assert_no_oauth_state(
    config: MCPServerConfig,
    storage: MCPSecretStorage,
    *,
    scope: MCPConfigScope | str,
) -> None:
    for kind in (
        "access_token",
        "refresh_token",
        "expires_at",
        "refresh_marker",
        "auth_flow_marker",
        "client_id",
        "client_secret",
        "client_auth_method",
    ):
        assert storage.get_secret(oauth_storage_key(config, kind, scope=scope)) is None


def _scoped_mcp_config(
    name: str,
    config: dict[str, object],
    *,
    scope: MCPConfigScope = MCPConfigScope.USER,
    approved: bool = True,
) -> ScopedMCPServerConfig:
    return ScopedMCPServerConfig(
        config=MCPServerConfig.from_mapping(name, config),
        scope=scope,
        approved=approved,
    )


class _FakeHealthManager:
    def __init__(
        self,
        records: list[MCPConnectionRecord],
        *,
        connect_error: Exception | None = None,
    ) -> None:
        self._records = records
        self._connect_error = connect_error
        self.connected = False
        self.disconnected = False

    async def connect_all(self) -> None:
        self.connected = True
        if self._connect_error is not None:
            raise self._connect_error

    async def disconnect_all(self) -> None:
        self.disconnected = True

    def list_connections(self) -> list[MCPConnectionRecord]:
        return self._records


def _record_with_refresh(
    record: MCPConnectionRecord,
    *,
    kind: str,
    refreshed_at: float,
    failure_reason: str | None,
) -> MCPConnectionRecord:
    record.latest_refresh_kind = kind
    record.latest_refresh_at = refreshed_at
    record.latest_refresh_failure_reason = failure_reason
    return record


def _fake_oauth_metadata(oauth_server: "FakeOAuthServer") -> oauth_module.OAuthMetadata:
    return oauth_module.OAuthMetadata(
        issuer=oauth_server.base_url,
        authorization_endpoint=oauth_server.base_url + "/authorize",
        token_endpoint=oauth_server.base_url + "/token",
        registration_endpoint=oauth_server.base_url + "/register",
        scopes_supported=["mcp"],
    )


class FakeOAuthServer:
    def __init__(self) -> None:
        self.last_registration_request: dict[str, object] | None = None
        self.last_token_request: dict[str, list[str]] = {}
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        self.base_url = "http://127.0.0.1:{}".format(self._server.server_address[1])
        self.metadata_url = self.base_url + "/.well-known/oauth-authorization-server"

    def _handler(self):
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                parsed = urlparse(self.path)
                if parsed.path == "/.well-known/oauth-authorization-server":
                    self._json(
                        {
                            "issuer": outer.base_url,
                            "authorization_endpoint": outer.base_url + "/authorize",
                            "token_endpoint": outer.base_url + "/token",
                            "registration_endpoint": outer.base_url + "/register",
                            "scopes_supported": ["mcp"],
                        }
                    )
                    return
                if parsed.path == "/authorize":
                    query = parse_qs(parsed.query)
                    redirect_uri = query["redirect_uri"][0]
                    state = query["state"][0]
                    try:
                        callback_url = outer._callback_url(redirect_uri, state)
                    except ValueError:
                        self.send_error(400)
                        return
                    urllib.request.urlopen(callback_url, timeout=5).read()
                    self._json({"ok": True})
                    return
                self.send_error(404)

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(length).decode("utf-8")
                if self.path == "/register":
                    outer.last_registration_request = json.loads(body)
                    self._json(
                        {
                            "client_id": "registered-client",
                            "client_secret": "registered-client-secret",
                        },
                        status=201,
                    )
                    return
                if self.path == "/token":
                    outer.last_token_request = parse_qs(body)
                    self._json(
                        {
                            "access_token": "access-token",
                            "refresh_token": "refresh-token",
                            "expires_in": 3600,
                            "token_type": "Bearer",
                        }
                    )
                    return
                self.send_error(404)

            def log_message(self, format: str, *args: object) -> None:
                return

            def _json(self, payload: dict[str, object], *, status: int = 200) -> None:
                body = json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        return Handler

    def _callback_url(self, redirect_uri: str, state: str) -> str:
        parsed = urlparse(redirect_uri)
        if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost"}:
            raise ValueError
        if parsed.port is None or parsed.path != "/callback":
            raise ValueError
        query = urlencode({"code": "code-1", "state": state})
        return "http://127.0.0.1:{}{}?{}".format(parsed.port, parsed.path, query)


def _free_port() -> int:
    server = ThreadingHTTPServer(("127.0.0.1", 0), BaseHTTPRequestHandler)
    try:
        return int(server.server_address[1])
    finally:
        server.server_close()
