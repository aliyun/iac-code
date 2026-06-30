import json
import sys
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

import yaml
from typer.testing import CliRunner

from iac_code.cli.main import app
from iac_code.mcp.config import load_mcp_configs
from iac_code.mcp.oauth import oauth_storage_key
from iac_code.mcp.storage import MCPSecretStorage
from iac_code.mcp.types import MCPConfigScope, MCPServerConfig


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

    listed = runner.invoke(app, ["mcp", "list"])
    assert listed.exit_code == 0
    assert "local-server" in listed.output
    assert "stdio" in listed.output

    fetched = runner.invoke(app, ["mcp", "get", "local-server", "--scope", "local"])
    assert fetched.exit_code == 0
    assert '"command": "uvx"' in fetched.output

    removed = runner.invoke(app, ["mcp", "remove", "local-server", "--scope", "local"])
    assert removed.exit_code == 0
    assert "Removed" in removed.output
    local_settings = yaml.safe_load((tmp_path / ".iac-code" / "settings.local.yml").read_text(encoding="utf-8"))
    assert "local-server" not in local_settings["mcpServers"]


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
            json.dumps({"type": "ws", "url": "wss://example.com/mcp"}),
            "--scope",
            "user",
        ],
    )

    assert result.exit_code != 0
    assert "Unsupported MCP transport" in result.output
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

    fetched = runner.invoke(app, ["mcp", "get", "remote", "--scope", "user"])

    assert fetched.exit_code == 0, fetched.output
    assert "Bearer token" not in fetched.output
    assert '"Authorization": "[redacted]"' in fetched.output
    assert '"X-Org": "org"' in fetched.output


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


def test_mcp_add_defaults_to_local_scope_inside_project(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    (tmp_path / ".git").mkdir()
    runner = CliRunner()

    result = runner.invoke(app, ["mcp", "add", "project-server", "--command", "uvx"])

    assert result.exit_code == 0, result.output
    assert (tmp_path / ".iac-code" / "settings.local.yml").exists()
    assert not (tmp_path / "config" / "settings.yml").exists()


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
    config = MCPServerConfig.from_mapping(
        "remote",
        yaml.safe_load((tmp_path / "config" / "settings.yml").read_text(encoding="utf-8"))["mcpServers"]["remote"],
    )
    storage = MCPSecretStorage()
    storage.set_secret(oauth_storage_key(config, "access_token", scope=MCPConfigScope.USER), "access")
    storage.set_secret(oauth_storage_key(config, "refresh_token", scope=MCPConfigScope.USER), "refresh")

    reset = runner.invoke(app, ["mcp", "reset-auth", "remote", "--scope", "user"])

    assert reset.exit_code == 0, reset.output
    assert storage.get_secret(oauth_storage_key(config, "access_token", scope=MCPConfigScope.USER)) is None
    assert storage.get_secret(oauth_storage_key(config, "refresh_token", scope=MCPConfigScope.USER)) is None


class FakeOAuthServer:
    def __init__(self) -> None:
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
                            "authorization_endpoint": outer.base_url + "/authorize",
                            "token_endpoint": outer.base_url + "/token",
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
                if self.path != "/token":
                    self.send_error(404)
                    return
                length = int(self.headers.get("Content-Length", "0"))
                outer.last_token_request = parse_qs(self.rfile.read(length).decode("utf-8"))
                self._json(
                    {
                        "access_token": "access-token",
                        "refresh_token": "refresh-token",
                        "expires_in": 3600,
                        "token_type": "Bearer",
                    }
                )

            def log_message(self, format: str, *args: object) -> None:
                return

            def _json(self, payload: dict[str, object]) -> None:
                body = json.dumps(payload).encode("utf-8")
                self.send_response(200)
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
