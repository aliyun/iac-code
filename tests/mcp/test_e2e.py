from __future__ import annotations

import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from iac_code.commands.registry import CommandRegistry, PromptCommand
from iac_code.mcp.manager import MCPManager
from iac_code.mcp.oauth import oauth_storage_key
from iac_code.mcp.skills import register_mcp_skill_commands
from iac_code.mcp.storage import MCPSecretStorage
from iac_code.mcp.tools import MCPTool, ReadMCPResourceTool
from iac_code.mcp.types import MCPConfigScope, MCPConnectionState, MCPServerConfig, ScopedMCPServerConfig
from iac_code.skills.skill_definition import SkillContext
from iac_code.tools.base import ToolContext


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
        storage.set_secret(
            oauth_storage_key(scoped.config, "access_token", scope=MCPConfigScope.SESSION),
            "access-token",
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
            stderr = process.stderr.read() if process.stderr else ""
            raise RuntimeError(f"MCP e2e server exited early: {stderr}")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return
        except OSError:
            time.sleep(0.05)
    stderr = process.stderr.read() if process.stderr else ""
    raise TimeoutError(f"MCP e2e server did not listen on port {port}: {stderr}")
