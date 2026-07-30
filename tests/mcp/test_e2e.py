from __future__ import annotations

import asyncio
import contextlib
import json
import socket
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

import pytest
from typer.testing import CliRunner

import iac_code.mcp.cli as mcp_cli
import iac_code.mcp.oauth as oauth_module
from iac_code.cli.main import app
from iac_code.commands.registry import CommandRegistry, PromptCommand
from iac_code.mcp.manager import MCPManager
from iac_code.mcp.oauth import get_oauth_access_token_async, get_oauth_storage_secret, set_oauth_storage_secret
from iac_code.mcp.skills import register_mcp_skill_commands
from iac_code.mcp.storage import MCPSecretStorage
from iac_code.mcp.tools import MCPTool, ReadMCPResourceTool
from iac_code.mcp.types import MCPConfigScope, MCPConnectionState, MCPServerConfig, ScopedMCPServerConfig
from iac_code.services.agent_factory import AgentFactoryOptions, create_agent_runtime
from iac_code.skills.skill_definition import SkillContext
from iac_code.tools.base import ToolContext


@pytest.mark.skipif(
    sys.platform == "win32",
    reason=(
        "The full Yuque-like release gate combines FastMCP OAuth subprocesses, loopback manual auth, and runtime "
        "cleanup; focused Windows MCP, CLI auth, and OAuth storage tests cover those paths without xdist hangs."
    ),
)
@pytest.mark.timeout(120)
def test_yuque_like_release_candidate_gate(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("IAC_CODE_MCP_DISABLE_KEYRING", "1")
    monkeypatch.setattr(mcp_cli, "_MCP_HEALTH_TIMEOUT_SECONDS", 10.0)

    runner = CliRunner()
    server = _start_yuque_like_server(tmp_path)
    try:
        _install_deterministic_manual_oauth(monkeypatch, timeout_seconds=10.0)
        monkeypatch.setattr(oauth_module, "_get_safe_oauth_metadata_json", oauth_module._get_json)
        monkeypatch.setattr(oauth_module, "_is_safe_discovered_oauth_endpoint", lambda *_args, **_kwargs: True)
        monkeypatch.setattr(oauth_module, "_validate_public_oauth_endpoint_url", lambda _url: None)
        monkeypatch.setattr(oauth_module, "_validate_urllib_response_peer", lambda _response: None)

        added = runner.invoke(
            app,
            [
                "mcp",
                "add",
                "--transport",
                "http",
                "yuque",
                server.mcp_url,
                "--scope",
                "user",
            ],
        )
        assert added.exit_code == 0, added.output
        assert "yuque" in added.output

        auth = runner.invoke(
            app,
            ["mcp", "auth", "yuque", "--scope", "user"],
            input="http://127.0.0.1/callback?code=manual-yuque-code&state=yuque-state\n",
        )
        assert auth.exit_code == 0, auth.output
        assert "Browser opened: no" in auth.output
        assert "Authorization URL:" in auth.output
        assert "Authenticated MCP server 'yuque'." in auth.output
        assert len(server.oauth_state.registration_requests) == 1
        assert server.oauth_state.registration_requests[0]["client_name"] == "IaC Code"
        authorization_query = parse_qs(urlparse(_authorization_url_from_output(auth.output)).query)
        assert authorization_query["client_id"] == ["yuque-dcr-client"]
        assert authorization_query["resource"] == [server.expected_resource]
        assert authorization_query["scope"] == ["doc:read"]
        assert authorization_query["state"] == ["yuque-state"]
        assert authorization_query["code_challenge_method"] == ["S256"]
        assert authorization_query["code_challenge"] != [""]
        assert len(server.oauth_state.authorization_code_requests) == 1
        assert server.oauth_state.authorization_code_requests[0]["resource"] == [server.expected_resource]

        yuque_config = MCPServerConfig.from_mapping("yuque", {"type": "http", "url": server.mcp_url})
        storage = MCPSecretStorage()
        assert get_oauth_storage_secret(yuque_config, storage, "access_token", scope=MCPConfigScope.USER) == (
            "yuque-access-1"
        )
        assert get_oauth_storage_secret(yuque_config, storage, "refresh_token", scope=MCPConfigScope.USER) == (
            "yuque-refresh-1"
        )
        assert get_oauth_storage_secret(yuque_config, storage, "client_secret", scope=MCPConfigScope.USER) == (
            "yuque-dcr-secret"
        )

        listed = runner.invoke(app, ["mcp", "list", "--check"])
        assert listed.exit_code == 0, listed.output
        assert listed.output.splitlines() == [
            "name\tscope\ttransport\tapproval_state\tauth_state\tconnection_state\ttools\tresources\tprompts\t"
            "latest_failure\trefresh_kind\trefresh_time\trefresh_failure",
            "yuque\tuser\thttp\tapproved\tconfigured\tconnected\t1\t2\t1\t-\t-\t-\t-",
        ]

        fetched = runner.invoke(app, ["mcp", "get", "yuque", "--scope", "user", "--check"])
        assert fetched.exit_code == 0, fetched.output
        payload = json.loads(fetched.output)
        assert payload["name"] == "yuque"
        assert payload["scope"] == "user"
        assert payload["transport"] == "http"
        assert payload["url"] == server.mcp_url
        assert payload["connection_state"] == "connected"
        assert payload["auth_state"] == "configured"
        assert payload["tools"] == 1
        assert payload["resources"] == 2
        assert payload["prompts"] == 1
        assert payload["latest_failure"] is None
        assert "yuque-access-1" not in fetched.output
        assert "yuque-refresh-1" not in fetched.output
        assert "yuque-dcr-secret" not in fetched.output

        runtime = create_agent_runtime(
            AgentFactoryOptions(
                model="qwen3.7-max",
                session_id="yuque-session",
                cwd=str(tmp_path),
            )
        )
        try:
            asyncio.run(_assert_yuque_runtime_exposure(runtime, tmp_path))
        finally:
            asyncio.run(runtime.aclose())

        cancel_added = runner.invoke(
            app,
            ["mcp", "add", "--transport", "http", "yuque-cancel", server.mcp_url, "--scope", "user"],
        )
        assert cancel_added.exit_code == 0, cancel_added.output
        _install_deterministic_manual_oauth(monkeypatch, timeout_seconds=2.0)
        cancelled = runner.invoke(app, ["mcp", "auth", "yuque-cancel", "--scope", "user"], input="\n")
        assert cancelled.exit_code == 1
        assert "MCP auth failed for 'yuque-cancel'" in cancelled.output
        assert "Timed out waiting for MCP OAuth callback" in cancelled.output
        cancel_config = MCPServerConfig.from_mapping("yuque-cancel", {"type": "http", "url": server.mcp_url})
        _assert_oauth_state_cleared(cancel_config)

        set_oauth_storage_secret(yuque_config, storage, "expires_at", "100", scope=MCPConfigScope.USER)
        before_refresh_count = len(server.oauth_state.refresh_requests)
        first, second = asyncio.run(_concurrent_expired_token_access(yuque_config))
        assert first == "yuque-access-refreshed"
        assert second == "yuque-access-refreshed"
        assert len(server.oauth_state.refresh_requests) == before_refresh_count + 1
        assert server.oauth_state.refresh_requests[-1]["resource"] == [server.expected_resource]

        reset = runner.invoke(app, ["mcp", "reset-auth", "yuque", "--scope", "user"])
        assert reset.exit_code == 0, reset.output
        _assert_oauth_state_cleared(yuque_config)

        for kind in _OAUTH_TEST_STORAGE_KINDS:
            set_oauth_storage_secret(yuque_config, storage, kind, f"leftover-{kind}", scope=MCPConfigScope.USER)
        removed = runner.invoke(app, ["mcp", "remove", "yuque", "--scope", "user"])
        assert removed.exit_code == 0, removed.output
        _assert_oauth_state_cleared(yuque_config)
        missing = runner.invoke(app, ["mcp", "get", "yuque", "--scope", "user"])
        assert missing.exit_code == 1
        assert "not found" in missing.output
    finally:
        server.close()


@pytest.mark.asyncio
async def test_stdio_mcp_server_e2e_tools_resources_prompts_and_skills(tmp_path: Path) -> None:
    script = _write_fastmcp_server(tmp_path)
    manager = MCPManager(
        [_scoped("stdio-e2e", {"command": sys.executable, "args": [str(script), "stdio"]})],
        roots=[tmp_path],
    )

    await manager.connect_all()
    try:
        assert manager.connection_state("stdio-e2e") is MCPConnectionState.CONNECTED
        assert [tool.public_name for tool in manager.list_tools()] == ["mcp__stdio_e2e__echo"]

        tool = MCPTool(manager=manager, record=manager.list_tools()[0], session_id="e2e-session")
        tool_result = await tool.execute(tool_input={"text": "hello"}, context=ToolContext(tool_use_id="tool-1"))
        assert tool_result.is_error is False
        assert "echo:hello" in tool_result.content

        resource_tool = ReadMCPResourceTool(manager=manager, session_id="e2e-session")
        resource_result = await resource_tool.execute(
            tool_input={"server": "stdio-e2e", "uri": "resource://ros/template"},
            context=ToolContext(),
        )
        assert "kind: ros" in resource_result.content

        assert [prompt.public_name for prompt in manager.list_prompts()] == ["mcp__stdio_e2e__review"]

        registry = CommandRegistry()
        warnings = await register_mcp_skill_commands(registry, manager)
        assert warnings == []
        command = registry.get("mcp__stdio_e2e__vpc")
        assert isinstance(command, PromptCommand)
        prompt = await command.skill.get_prompt("", SkillContext(cwd=str(tmp_path)))
        assert "Remote VPC skill" in prompt
    finally:
        await manager.disconnect_all()


@pytest.mark.asyncio
async def test_default_sdk_clients_connect_with_bounded_concurrency_and_disconnect(tmp_path: Path) -> None:
    script = _write_fastmcp_server(tmp_path)
    manager = MCPManager(
        [
            _scoped("stdio-one", {"command": sys.executable, "args": [str(script), "stdio"]}),
            _scoped("stdio-two", {"command": sys.executable, "args": [str(script), "stdio"]}),
        ],
        roots=[tmp_path],
        max_concurrent_connections=2,
    )

    await manager.connect_all()
    try:
        assert manager.connection_state("stdio-one") is MCPConnectionState.CONNECTED
        assert manager.connection_state("stdio-two") is MCPConnectionState.CONNECTED
    finally:
        await manager.disconnect_all()


@pytest.mark.asyncio
async def test_mixed_stdio_and_http_servers_connect_concurrently(tmp_path: Path) -> None:
    script = _write_fastmcp_server(tmp_path)
    port = _free_port()
    process = subprocess.Popen(
        [sys.executable, str(script), "http", str(port)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        _wait_for_port(port, process)
        manager = MCPManager(
            [
                _scoped("local-e2e", {"command": sys.executable, "args": [str(script), "stdio"]}),
                _scoped("remote-e2e", {"type": "http", "url": f"http://127.0.0.1:{port}/mcp"}),
            ],
            roots=[tmp_path],
            max_concurrent_connections=2,
        )

        await manager.connect_all()

        try:
            assert manager.connection_state("local-e2e") is MCPConnectionState.CONNECTED
            assert manager.connection_state("remote-e2e") is MCPConnectionState.CONNECTED
        finally:
            await manager.disconnect_all()
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("transport", "path"),
    [
        ("http", "/mcp"),
        ("sse", "/sse"),
    ],
)
async def test_remote_mcp_server_e2e_http_and_sse(tmp_path: Path, transport: str, path: str) -> None:
    script = _write_fastmcp_server(tmp_path)
    port = _free_port()
    process = subprocess.Popen(
        [sys.executable, str(script), transport, str(port)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        _wait_for_port(port, process)
        manager = MCPManager(
            [_scoped("remote-e2e", {"type": transport, "url": f"http://127.0.0.1:{port}{path}"})],
            roots=[tmp_path],
        )
        await manager.connect_all()
        assert manager.connection_state("remote-e2e") is MCPConnectionState.CONNECTED
        result = await manager.call_tool("remote-e2e", "echo", {"text": transport})
        assert _first_text(result) == f"echo:{transport}"
        await manager.disconnect_all()
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


@pytest.mark.asyncio
async def test_remote_mcp_server_e2e_oauth_bearer_token_connect(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("IAC_CODE_MCP_DISABLE_KEYRING", "1")
    script = _write_auth_fastmcp_server(tmp_path)
    port = _free_port()
    process = subprocess.Popen(
        [sys.executable, str(script), str(port)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        _wait_for_port(port, process)
        scoped = _scoped(
            "auth-remote",
            {
                "type": "http",
                "url": f"http://127.0.0.1:{port}/mcp",
                "oauth": {"clientId": "client-id"},
            },
        )
        storage = MCPSecretStorage()
        set_oauth_storage_secret(
            scoped.config, storage, "access_token", "access-token", scope=MCPConfigScope.SESSION
        )
        manager = MCPManager([scoped], roots=[tmp_path])

        await manager.connect_all()

        assert manager.connection_state("auth-remote") is MCPConnectionState.CONNECTED
        result = await manager.call_tool("auth-remote", "echo", {"text": "oauth"})
        assert _first_text(result) == "echo:oauth"
        await manager.disconnect_all()
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


_OAUTH_TEST_STORAGE_KINDS = (
    "access_token",
    "refresh_token",
    "expires_at",
    "refresh_marker",
    "auth_flow_marker",
    "client_id",
    "client_secret",
    "client_auth_method",
)


@dataclass
class _YuqueOAuthState:
    issuer: str
    expected_resource: str
    registration_requests: list[dict[str, Any]] = field(default_factory=list)
    authorization_requests: list[dict[str, list[str]]] = field(default_factory=list)
    authorization_code_requests: list[dict[str, list[str]]] = field(default_factory=list)
    refresh_requests: list[dict[str, list[str]]] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)


class _YuqueOAuthHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False

    def __init__(self, address: tuple[str, int], state: _YuqueOAuthState) -> None:
        super().__init__(address, _YuqueOAuthHandler)
        self.state = state


class _YuqueOAuthHandler(BaseHTTPRequestHandler):
    server: _YuqueOAuthHTTPServer

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/.well-known/oauth-authorization-server":
            self._send_json(
                {
                    "issuer": self.server.state.issuer,
                    "authorization_endpoint": f"{self.server.state.issuer}/oauth/authorize",
                    "token_endpoint": f"{self.server.state.issuer}/oauth/token",
                    "registration_endpoint": f"{self.server.state.issuer}/oauth/register",
                    "scopes_supported": ["doc:read"],
                    "token_endpoint_auth_methods_supported": ["client_secret_post", "none"],
                }
            )
            return
        if parsed.path == "/oauth/authorize":
            self._handle_authorize(parse_qs(parsed.query))
            return
        self._send_json({"error": "not_found"}, status=404)

    def _handle_authorize(self, query: dict[str, list[str]]) -> None:
        redirect_uri = _single_query_value(query, "redirect_uri")
        if not _is_loopback_callback_uri(redirect_uri):
            self._send_oauth_error("invalid_request", "authorization redirect_uri must be loopback callback")
            return
        if _single_query_value(query, "response_type") != "code":
            self._send_oauth_error("invalid_request", "authorization response_type must be code")
            return
        if _single_query_value(query, "client_id") != "yuque-dcr-client":
            self._send_oauth_error("invalid_client", "authorization client_id must match DCR client")
            return
        if _single_query_value(query, "state") != "yuque-state":
            self._send_oauth_error("invalid_request", "authorization state mismatch")
            return
        if not _single_query_value(query, "code_challenge"):
            self._send_oauth_error("invalid_request", "authorization code_challenge is required")
            return
        if _single_query_value(query, "code_challenge_method") != "S256":
            self._send_oauth_error("invalid_request", "authorization code_challenge_method must be S256")
            return
        if _single_query_value(query, "resource") != self.server.state.expected_resource:
            self._send_oauth_error("invalid_target", "authorization resource mismatch")
            return
        if _single_query_value(query, "scope") != "doc:read":
            self._send_oauth_error("invalid_scope", "authorization scope must be doc:read")
            return

        with self.server.state.lock:
            self.server.state.authorization_requests.append(query)
        location = "{}{}{}".format(
            redirect_uri,
            "&" if "?" in redirect_uri else "?",
            urlencode({"code": "browser-yuque-code", "state": _single_query_value(query, "state")}),
        )
        self.send_response(302)
        self.send_header("Location", location)
        self.end_headers()

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        body = self.rfile.read(int(self.headers.get("Content-Length", "0") or "0"))
        if parsed.path == "/oauth/register":
            self._handle_register(body)
            return
        if parsed.path == "/oauth/token":
            self._handle_token(body)
            return
        self._send_json({"error": "not_found"}, status=404)

    def log_message(self, format: str, *args: Any) -> None:
        _ = (format, args)

    def _handle_register(self, body: bytes) -> None:
        try:
            request = json.loads(body.decode("utf-8")) if body else {}
        except json.JSONDecodeError:
            self._send_oauth_error("invalid_client_metadata", "DCR request body must be JSON")
            return
        if not isinstance(request, dict):
            self._send_oauth_error("invalid_client_metadata", "DCR request body must be an object")
            return
        if not _is_loopback_callback_uri(_first_string(request.get("redirect_uris"))):
            self._send_oauth_error("invalid_client_metadata", "DCR redirect_uris must include loopback callback")
            return
        if not _contains_all(request.get("grant_types"), {"authorization_code", "refresh_token"}):
            self._send_oauth_error(
                "invalid_client_metadata",
                "DCR grant_types must include authorization_code and refresh_token",
            )
            return
        if not _contains_all(request.get("response_types"), {"code"}):
            self._send_oauth_error("invalid_client_metadata", "DCR response_types must include code")
            return
        if request.get("scope") != "doc:read":
            self._send_oauth_error("invalid_client_metadata", "DCR scope must be doc:read")
            return
        if request.get("token_endpoint_auth_method") != "none":
            self._send_oauth_error("invalid_client_metadata", "DCR token auth method must request none")
            return
        if not request.get("client_name"):
            self._send_oauth_error("invalid_client_metadata", "DCR client_name is required")
            return
        with self.server.state.lock:
            self.server.state.registration_requests.append(request)
        self._send_json(
            {
                "client_id": "yuque-dcr-client",
                "client_secret": "yuque-dcr-secret",
                "client_secret_expires_at": 0,
                "token_endpoint_auth_method": "client_secret_post",
            },
            status=201,
        )

    def _handle_token(self, body: bytes) -> None:
        request = parse_qs(body.decode("utf-8"))
        if _single_query_value(request, "client_id") != "yuque-dcr-client":
            self._send_oauth_error("invalid_client", "token client_id must match DCR client")
            return
        if _single_query_value(request, "client_secret") != "yuque-dcr-secret":
            self._send_oauth_error("invalid_client", "token client_secret must match DCR client secret")
            return
        if _single_query_value(request, "resource") != self.server.state.expected_resource:
            self._send_oauth_error("invalid_target", "token resource mismatch")
            return
        grant_type = request.get("grant_type", [""])[0]
        if grant_type == "authorization_code":
            if request.get("code") not in (["manual-yuque-code"], ["browser-yuque-code"]):
                self._send_oauth_error("invalid_grant", "authorization code is not accepted")
                return
            if not _single_query_value(request, "code_verifier"):
                self._send_oauth_error("invalid_request", "authorization-code token request requires code_verifier")
                return
            if not _is_loopback_callback_uri(_single_query_value(request, "redirect_uri")):
                self._send_oauth_error("invalid_request", "authorization-code redirect_uri must be loopback callback")
                return
            with self.server.state.lock:
                self.server.state.authorization_code_requests.append(request)
            self._send_json(
                {
                    "access_token": "yuque-access-1",
                    "refresh_token": "yuque-refresh-1",
                    "expires_in": 3600,
                    "token_type": "Bearer",
                }
            )
            return
        if grant_type == "refresh_token":
            if _single_query_value(request, "refresh_token") != "yuque-refresh-1":
                self._send_oauth_error("invalid_grant", "refresh token must be the issued Yuque refresh token")
                return
            with self.server.state.lock:
                self.server.state.refresh_requests.append(request)
            time.sleep(0.05)
            self._send_json(
                {
                    "access_token": "yuque-access-refreshed",
                    "refresh_token": "yuque-refresh-2",
                    "expires_in": 3600,
                    "token_type": "Bearer",
                }
            )
            return
        self._send_oauth_error("unsupported_grant_type", f"unsupported grant_type {grant_type!r}")

    def _send_oauth_error(self, error: str, description: str, *, status: int = 400) -> None:
        self._send_json({"error": error, "error_description": description}, status=status)

    def _send_json(self, payload: dict[str, Any], *, status: int = 200) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _single_query_value(query: dict[str, list[str]], name: str) -> str:
    return query.get(name, [""])[0]


def _first_string(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        for item in value:
            if isinstance(item, str):
                return item
    return ""


def _contains_all(value: Any, expected: set[str]) -> bool:
    return isinstance(value, list) and expected.issubset({str(item) for item in value})


def _is_loopback_callback_uri(value: str) -> bool:
    parsed = urlparse(value)
    return (
        parsed.scheme == "http"
        and parsed.hostname == "127.0.0.1"
        and parsed.port is not None
        and parsed.path == ("/callback")
    )


@dataclass
class _YuqueLikeServer:
    mcp_url: str
    expected_resource: str
    oauth_state: _YuqueOAuthState
    oauth_server: _YuqueOAuthHTTPServer
    oauth_thread: threading.Thread
    process: subprocess.Popen[str]

    def close(self) -> None:
        _stop_process(self.process)
        self.oauth_server.shutdown()
        self.oauth_server.server_close()
        self.oauth_thread.join(timeout=5)


def _start_yuque_like_server(tmp_path: Path) -> _YuqueLikeServer:
    mcp_port = _free_port()
    mcp_url = f"http://127.0.0.1:{mcp_port}/mcp"
    expected_resource = f"http://127.0.0.1:{mcp_port}/"
    oauth_state = _YuqueOAuthState(issuer="", expected_resource=expected_resource)
    oauth_server = _YuqueOAuthHTTPServer(("127.0.0.1", 0), oauth_state)
    oauth_port = int(oauth_server.server_address[1])
    oauth_state.issuer = f"http://127.0.0.1:{oauth_port}"
    oauth_thread = threading.Thread(target=oauth_server.serve_forever, daemon=True)
    oauth_thread.start()

    script = _write_yuque_like_fastmcp_server(tmp_path)
    process = subprocess.Popen(
        [sys.executable, str(script), str(mcp_port), oauth_state.issuer],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        _wait_for_port(mcp_port, process)
    except Exception:
        _stop_process(process)
        oauth_server.shutdown()
        oauth_server.server_close()
        oauth_thread.join(timeout=5)
        raise
    return _YuqueLikeServer(
        mcp_url=mcp_url,
        expected_resource=expected_resource,
        oauth_state=oauth_state,
        oauth_server=oauth_server,
        oauth_thread=oauth_thread,
        process=process,
    )


def _write_yuque_like_fastmcp_server(tmp_path: Path) -> Path:
    script = tmp_path / "yuque_like_fastmcp_server.py"
    script.write_text(
        """
from __future__ import annotations

import sys
from mcp.server.auth.provider import AccessToken
from mcp.server.auth.settings import AuthSettings
from mcp.server.fastmcp import FastMCP

port = int(sys.argv[1])
issuer = sys.argv[2]
resource = f"http://127.0.0.1:{port}"

class Verifier:
    async def verify_token(self, token: str):
        if token.startswith("yuque-access"):
            return AccessToken(token=token, client_id="yuque-dcr-client", scopes=["doc:read"])
        return None

mcp = FastMCP(
    "yuque-like",
    host="127.0.0.1",
    port=port,
    token_verifier=Verifier(),
    auth=AuthSettings(
        issuer_url=issuer,
        resource_server_url=resource,
        required_scopes=["doc:read"],
    ),
)

@mcp.tool()
def search(query: str) -> str:
    return "yuque-search:" + query

@mcp.resource("resource://yuque/doc", name="doc", mime_type="text/plain")
def doc() -> str:
    return "Yuque doc body"

@mcp.resource("skill://yuque/space", name="space", mime_type="text/markdown")
def skill() -> str:
    return "---\\ndescription: Yuque space skill\\n---\\n# Yuque space skill"

@mcp.prompt()
def review(doc: str = "demo") -> str:
    return "Review Yuque doc " + doc

mcp.run("streamable-http")
""",
        encoding="utf-8",
    )
    return script


def _stop_process(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        with contextlib.suppress(subprocess.TimeoutExpired):
            process.wait(timeout=5)


def _install_deterministic_manual_oauth(monkeypatch: pytest.MonkeyPatch, *, timeout_seconds: float) -> None:
    import mcp.client.auth.oauth2 as sdk_oauth2

    def token_urlsafe(length: int) -> str:
        if length == 32:
            return "yuque-state"
        return f"yuque-token-{length}"

    def start_oauth_loopback_flow(config, *, storage, scope):
        return oauth_module.start_oauth_loopback_flow(
            config,
            storage=storage,
            scope=scope,
            open_browser=lambda _url: False,
            timeout_seconds=timeout_seconds,
        )

    monkeypatch.setattr(sdk_oauth2.secrets, "token_urlsafe", token_urlsafe)
    monkeypatch.setattr(oauth_module, "_open_browser", lambda _url: False)
    monkeypatch.setattr(mcp_cli, "start_oauth_loopback_flow", start_oauth_loopback_flow)


async def _assert_yuque_runtime_exposure(runtime: Any, tmp_path: Path) -> None:
    assert runtime.mcp_manager.connection_state("yuque") is MCPConnectionState.CONNECTED

    tool = runtime.tool_registry.get("mcp__yuque__search")
    assert tool is not None
    tool_result = await tool.execute(tool_input={"query": "ros"}, context=ToolContext(tool_use_id="runtime-tool"))
    assert tool_result.is_error is False
    assert "yuque-search:ros" in tool_result.content

    resource_tool = runtime.tool_registry.get("read_mcp_resource")
    assert resource_tool is not None
    resource_result = await resource_tool.execute(
        tool_input={"server": "yuque", "uri": "resource://yuque/doc"},
        context=ToolContext(tool_use_id="runtime-resource"),
    )
    assert "Yuque doc body" in resource_result.content

    assert runtime.command_registry.get("mcp__yuque__review") is not None
    skill_command = runtime.command_registry.get("mcp__yuque__space")
    assert isinstance(skill_command, PromptCommand)
    prompt = await skill_command.skill.get_prompt("", SkillContext(cwd=str(tmp_path)))
    assert "Yuque space skill" in prompt


async def _concurrent_expired_token_access(config: MCPServerConfig) -> tuple[str | None, str | None]:
    first_storage = MCPSecretStorage()
    second_storage = MCPSecretStorage()
    first, second = await asyncio.gather(
        get_oauth_access_token_async(
            config,
            storage=first_storage,
            scope=MCPConfigScope.USER,
            now=lambda: 200,
            refresh_coordinator=oauth_module.TokenRefreshCoordinator(),
        ),
        get_oauth_access_token_async(
            config,
            storage=second_storage,
            scope=MCPConfigScope.USER,
            now=lambda: 200,
            refresh_coordinator=oauth_module.TokenRefreshCoordinator(),
        ),
    )
    return first, second


def _assert_oauth_state_cleared(config: MCPServerConfig) -> None:
    storage = MCPSecretStorage()
    for kind in _OAUTH_TEST_STORAGE_KINDS:
        assert get_oauth_storage_secret(config, storage, kind, scope=MCPConfigScope.USER) is None, kind


def _authorization_url_from_output(output: str) -> str:
    for line in output.splitlines():
        if line.startswith("Authorization URL: "):
            return line.removeprefix("Authorization URL: ")
    raise AssertionError(f"Authorization URL line not found in output:\n{output}")


def _write_fastmcp_server(tmp_path: Path) -> Path:
    script = tmp_path / "fastmcp_server.py"
    script.write_text(
        """
from __future__ import annotations

import sys
from mcp.server.fastmcp import FastMCP

transport = sys.argv[1]
port = int(sys.argv[2]) if len(sys.argv) > 2 else 0
mcp = FastMCP("iac-code-e2e", host="127.0.0.1", port=port)

@mcp.tool()
def echo(text: str) -> str:
    return "echo:" + text

@mcp.resource("resource://ros/template", name="template", mime_type="text/plain")
def template() -> str:
    return "kind: ros"

@mcp.resource("skill://ros/vpc", name="vpc", mime_type="text/markdown")
def skill() -> str:
    return "---\\ndescription: Remote VPC skill\\n---\\n# Remote VPC skill"

@mcp.prompt()
def review(template: str = "demo") -> str:
    return "Review " + template

if transport == "stdio":
    mcp.run("stdio")
elif transport == "http":
    mcp.run("streamable-http")
elif transport == "sse":
    mcp.run("sse")
else:
    raise SystemExit("unknown transport")
""",
        encoding="utf-8",
    )
    return script


def _write_auth_fastmcp_server(tmp_path: Path) -> Path:
    script = tmp_path / "auth_fastmcp_server.py"
    script.write_text(
        """
from __future__ import annotations

import sys
from mcp.server.auth.provider import AccessToken
from mcp.server.auth.settings import AuthSettings
from mcp.server.fastmcp import FastMCP

port = int(sys.argv[1])

class Verifier:
    async def verify_token(self, token: str):
        if token != "access-token":
            return None
        return AccessToken(token=token, client_id="client-id", scopes=["mcp"])

mcp = FastMCP(
    "iac-code-auth-e2e",
    host="127.0.0.1",
    port=port,
    token_verifier=Verifier(),
    auth=AuthSettings(
        issuer_url=f"http://127.0.0.1:{port}",
        resource_server_url=f"http://127.0.0.1:{port}",
        required_scopes=["mcp"],
    ),
)

@mcp.tool()
def echo(text: str) -> str:
    return "echo:" + text

mcp.run("streamable-http")
""",
        encoding="utf-8",
    )
    return script


def _scoped(name: str, config: dict[str, Any]) -> ScopedMCPServerConfig:
    return ScopedMCPServerConfig(
        config=MCPServerConfig.from_mapping(name, config),
        scope=MCPConfigScope.SESSION,
    )


def _first_text(result: Any) -> str:
    content = getattr(result, "content", None)
    if content is None and isinstance(result, dict):
        content = result.get("content")
    first = content[0]
    if isinstance(first, dict):
        return first["text"]
    return getattr(first, "text")


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_for_port(port: int, process: subprocess.Popen[str]) -> None:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if process.poll() is not None:
            stderr = _read_process_stderr_if_exited(process)
            raise RuntimeError(f"MCP e2e server exited early: {stderr}")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return
        except OSError:
            time.sleep(0.05)
    stderr = _read_process_stderr_if_exited(process)
    detail = stderr if stderr else "stderr unavailable while process is still running"
    raise TimeoutError(f"MCP e2e server did not listen on port {port}: {detail}")


def _read_process_stderr_if_exited(process: subprocess.Popen[str]) -> str:
    if process.poll() is None or process.stderr is None:
        return ""
    return process.stderr.read()
