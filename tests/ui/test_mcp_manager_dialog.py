from __future__ import annotations

import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import pytest
from rich.console import Console

from iac_code.mcp.oauth import oauth_storage_key
from iac_code.mcp.storage import MCPSecretStorage
from iac_code.mcp.types import MCPConfigScope, MCPServerConfig
from iac_code.ui.core.key_event import KeyEvent
from iac_code.ui.dialogs.mcp_manager import MCPManagerDialog


def test_mcp_manager_dialog_renders_grouped_server_list() -> None:
    dialog = MCPManagerDialog(_context(), metadata_provider=_sample_metadata)

    output = _render_text(dialog.render())

    assert "Manage MCP servers" in output
    assert "2 servers" in output
    assert "Project MCPs" in output
    assert "User MCPs" in output
    assert "ros" in output
    assert "ros · ✓ connected" in output
    assert "remote" in output
    assert "remote · △ needs authentication" in output
    assert "2/1/1" not in output
    assert "iac-code mcp --help for help" in output
    assert "Enter select" in output


def test_mcp_manager_dialog_run_returns_cli_guidance_without_tty(monkeypatch) -> None:
    dialog = MCPManagerDialog(_context(), metadata_provider=_sample_metadata)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: False)

    result = dialog.run()

    assert result == "Interactive MCP manager requires a TTY. Use `iac-code mcp list` or MCP quick commands."


def test_mcp_manager_dialog_disambiguates_same_name_servers_in_list() -> None:
    dialog = MCPManagerDialog(
        _context(),
        metadata_provider=lambda: {
            "servers": [
                {
                    "serverName": "shared",
                    "scope": "project",
                    "sourcePath": "/repo/.mcp.json",
                    "_sourcePath": "/repo/.mcp.json",
                    "transport": "stdio",
                    "state": "connected",
                    "authState": "not-configured",
                },
                {
                    "serverName": "shared",
                    "scope": "project",
                    "sourcePath": "/repo/child/.mcp.json",
                    "_sourcePath": "/repo/child/.mcp.json",
                    "transport": "stdio",
                    "state": "connected",
                    "authState": "not-configured",
                },
            ],
            "warnings": [],
        },
    )

    output = _render_text(dialog.render())

    assert "shared (/repo/.mcp.json) · ✓ connected" in output
    assert "shared (/repo/child/.mcp.json) · ✓ connected" in output


def test_mcp_manager_dialog_uses_public_source_path_for_display_and_private_path_for_actions() -> None:
    calls: list[tuple[str, str | None, str | None]] = []
    dialog = MCPManagerDialog(
        _context(),
        metadata_provider=lambda: {
            "servers": [
                {
                    "serverName": "shared",
                    "scope": "project",
                    "sourcePath": "[PATH]/.mcp.json",
                    "_sourcePath": "/Users/alice/repo/.mcp.json",
                    "transport": "stdio",
                    "state": "connected",
                    "authState": "not-configured",
                    "toolsCount": 0,
                    "resourcesCount": 0,
                    "promptsCount": 0,
                },
                {
                    "serverName": "shared",
                    "scope": "project",
                    "sourcePath": "[PATH]/child/.mcp.json",
                    "_sourcePath": "/Users/alice/repo/child/.mcp.json",
                    "transport": "stdio",
                    "state": "connected",
                    "authState": "not-configured",
                    "toolsCount": 0,
                    "resourcesCount": 0,
                    "promptsCount": 0,
                },
            ],
            "warnings": [],
        },
        actions={
            "disable": lambda name, scope, source_path=None: (
                calls.append((name, scope, source_path)) or "Disabled MCP server 'shared'."
            )
        },
    )

    list_output = _render_text(dialog.render())

    assert "shared ([PATH]/.mcp.json) · ✓ connected" in list_output
    assert "shared ([PATH]/child/.mcp.json) · ✓ connected" in list_output
    assert "/Users/alice" not in list_output
    assert "alice" not in list_output

    dialog.handle_key(KeyEvent("enter", "\n"))
    detail_output = _render_text(dialog.render())

    assert "Config location: project ([PATH]/.mcp.json)" in detail_output
    assert "/Users/alice" not in detail_output
    assert "alice" not in detail_output

    dialog.handle_key(KeyEvent("down", ""))
    dialog.handle_key(KeyEvent("enter", "\n"))

    assert calls == [("shared", "project", "/Users/alice/repo/.mcp.json")]


def test_mcp_manager_dialog_orders_scopes_and_servers_like_claude_panel() -> None:
    dialog = MCPManagerDialog(_context(), metadata_provider=_unsorted_metadata)

    output = _render_text(dialog.render())

    assert output.index("Project MCPs") < output.index("alpha") < output.index("beta")
    assert output.index("beta") < output.index("Local MCPs") < output.index("local")
    assert output.index("local") < output.index("User MCPs") < output.index("zed")
    assert output.index("zed") < output.index("Session MCPs") < output.index("ephemeral")
    assert output.index("ephemeral") < output.index("Built-in MCPs") < output.index("builtin")

    assert dialog.handle_key(KeyEvent("enter", "\n")) is True
    assert "Alpha MCP Server" in _render_text(dialog.render())


def test_mcp_manager_dialog_top_list_does_not_use_select_ctrl_navigation_like_claude_panel() -> None:
    dialog = MCPManagerDialog(_context(), metadata_provider=_sample_metadata)

    dialog.handle_key(KeyEvent("n", "\x0e", ctrl=True))
    dialog.handle_key(KeyEvent("enter", "\n"))
    output = _render_text(dialog.render())

    assert "Ros MCP Server" in output
    assert "Remote MCP Server" not in output


def test_mcp_manager_dialog_server_title_only_capitalizes_first_character_like_claude() -> None:
    dialog = MCPManagerDialog(
        _context(),
        metadata_provider=lambda: _single_server_metadata(
            {
                "serverName": "my-server",
                "scope": "project",
                "transport": "stdio",
                "state": "connected",
                "authState": "not-configured",
                "toolsCount": 0,
                "resourcesCount": 0,
                "promptsCount": 0,
            }
        ),
    )

    dialog.handle_key(KeyEvent("enter", "\n"))
    output = _render_text(dialog.render())

    assert "My-server MCP Server" in output
    assert "My Server MCP Server" not in output


def test_mcp_manager_dialog_navigates_to_server_tools_and_tool_details() -> None:
    dialog = MCPManagerDialog(_context(), metadata_provider=_sample_metadata)

    assert dialog.handle_key(KeyEvent("enter", "\n")) is True
    server_output = _render_text(dialog.render())
    assert "Ros MCP Server" in server_output
    assert "View tools" in server_output
    assert "Config location" in server_output

    assert dialog.handle_key(KeyEvent("enter", "\n")) is True
    tools_output = _render_text(dialog.render())
    assert "Tools for ros" in tools_output
    assert "1 tool" in tools_output
    assert "generate-template" in tools_output
    assert "mcp__ros__generate_template" not in tools_output
    assert "read-only" in tools_output

    assert dialog.handle_key(KeyEvent("enter", "\n")) is True
    detail_output = _render_text(dialog.render())
    assert "generate-template" in detail_output
    assert "[read-only]" in detail_output
    assert "Tool name: generate-template" in detail_output
    assert "Full name: mcp__ros__generate_template" in detail_output
    assert "Public name:" not in detail_output
    assert "Original server:" not in detail_output
    assert "Description:" in detail_output
    assert "Generate ROS template" in detail_output
    assert "Parameters:" in detail_output
    assert "• region (required): string - Region ID" in detail_output
    assert "Annotations:" not in detail_output
    assert "Input schema" not in detail_output


def test_mcp_manager_dialog_navigates_to_resources_and_resource_details() -> None:
    dialog = MCPManagerDialog(
        _context(),
        metadata_provider=lambda: _single_server_metadata(
            {
                "serverName": "docs",
                "scope": "project",
                "transport": "stdio",
                "state": "connected",
                "authState": "not-configured",
                "toolsCount": 0,
                "resourcesCount": 1,
                "promptsCount": 0,
                "resources": [
                    {
                        "uri": "file:///repo/guide.md",
                        "name": "guide",
                        "title": "Project guide",
                        "description": "How to use the project",
                        "mimeType": "text/markdown",
                    }
                ],
            }
        ),
    )

    dialog.handle_key(KeyEvent("enter", "\n"))
    server_output = _render_text(dialog.render())
    assert "View resources" in server_output
    assert "View tools" not in server_output

    dialog.handle_key(KeyEvent("enter", "\n"))
    resources_output = _render_text(dialog.render())
    assert "Resources for docs" in resources_output
    assert "1 resource" in resources_output
    assert "Project guide" in resources_output
    assert "file:///repo/guide.md" in resources_output

    dialog.handle_key(KeyEvent("enter", "\n"))
    detail_output = _render_text(dialog.render())
    assert "Project guide" in detail_output
    assert "URI: file:///repo/guide.md" in detail_output
    assert "MIME type: text/markdown" in detail_output
    assert "Description:" in detail_output
    assert "How to use the project" in detail_output


def test_mcp_manager_dialog_navigates_to_prompts_and_prompt_details() -> None:
    dialog = MCPManagerDialog(
        _context(),
        metadata_provider=lambda: _single_server_metadata(
            {
                "serverName": "writer",
                "scope": "project",
                "transport": "stdio",
                "state": "connected",
                "authState": "not-configured",
                "toolsCount": 0,
                "resourcesCount": 0,
                "promptsCount": 1,
                "prompts": [
                    {
                        "publicName": "mcp__writer__summarize",
                        "originalPromptName": "summarize",
                        "description": "Summarize a document",
                        "arguments": [{"name": "topic", "description": "Topic to summarize", "required": True}],
                    }
                ],
            }
        ),
    )

    dialog.handle_key(KeyEvent("enter", "\n"))
    server_output = _render_text(dialog.render())
    assert "View prompts" in server_output
    assert "View tools" not in server_output

    dialog.handle_key(KeyEvent("enter", "\n"))
    prompts_output = _render_text(dialog.render())
    assert "Prompts for writer" in prompts_output
    assert "1 prompt" in prompts_output
    assert "summarize" in prompts_output

    dialog.handle_key(KeyEvent("enter", "\n"))
    detail_output = _render_text(dialog.render())
    assert "summarize" in detail_output
    assert "Full name: mcp__writer__summarize" in detail_output
    assert "Description:" in detail_output
    assert "Summarize a document" in detail_output
    assert "Arguments:" in detail_output
    assert "• topic (required) - Topic to summarize" in detail_output


def test_mcp_manager_dialog_renders_claude_style_status_text() -> None:
    dialog = MCPManagerDialog(_context(), metadata_provider=_status_metadata)

    output = _render_text(dialog.render())

    assert "connected · ✓ connected" in output
    assert "pending · ○ connecting…" in output
    assert "retrying · ○ reconnecting (1/2)…" in output
    assert "auth · △ needs authentication" in output
    assert "failed · ✖ failed" in output
    assert "disabled · ○ disabled" in output


def test_mcp_manager_dialog_server_detail_pending_status_omits_retry_count_like_claude_panel() -> None:
    dialog = MCPManagerDialog(
        _context(),
        metadata_provider=lambda: _single_server_metadata(
            {
                "serverName": "retrying",
                "scope": "project",
                "transport": "stdio",
                "state": "pending",
                "authState": "not-configured",
                "toolsCount": 0,
                "resourcesCount": 0,
                "promptsCount": 0,
                "retryCount": 1,
                "maxReconnectAttempts": 2,
            }
        ),
    )

    assert "retrying · ○ reconnecting (1/2)…" in _render_text(dialog.render())

    dialog.handle_key(KeyEvent("enter", "\n"))
    output = _render_text(dialog.render())

    assert "Status: ○ connecting…" in output
    assert "Status: ○ reconnecting (1/2)…" not in output


def test_mcp_manager_dialog_failed_list_shows_debug_hint_like_claude_panel(monkeypatch) -> None:
    monkeypatch.setattr("iac_code.ui.dialogs.mcp_manager.is_debug_enabled", lambda: False, raising=False)
    dialog = MCPManagerDialog(
        _context(),
        metadata_provider=lambda: _single_server_metadata(
            {
                "serverName": "broken",
                "scope": "project",
                "transport": "stdio",
                "state": "failed",
                "authState": "not-configured",
                "toolsCount": 0,
                "resourcesCount": 0,
                "promptsCount": 0,
            }
        ),
    )

    output = _render_text(dialog.render())

    assert "Run iac-code --debug to see error logs" in output


def test_mcp_manager_dialog_failed_list_shows_debug_enabled_hint_like_claude_panel(monkeypatch) -> None:
    monkeypatch.setattr("iac_code.ui.dialogs.mcp_manager.is_debug_enabled", lambda: True, raising=False)
    monkeypatch.setattr(
        "iac_code.ui.dialogs.mcp_manager.current_log_file",
        lambda: Path("/tmp/iac-code.log"),
        raising=False,
    )
    dialog = MCPManagerDialog(
        _context(),
        metadata_provider=lambda: _single_server_metadata(
            {
                "serverName": "broken",
                "scope": "project",
                "transport": "stdio",
                "state": "failed",
                "authState": "not-configured",
                "toolsCount": 0,
                "resourcesCount": 0,
                "promptsCount": 0,
            }
        ),
    )

    output = _render_text(dialog.render())

    assert "Debug logging is enabled. Log file:" in output
    assert "iac-code.log" in output
    assert "Run iac-code --debug to see error logs" not in output


def test_mcp_manager_dialog_refresh_key_reloads_metadata() -> None:
    calls = 0

    def metadata() -> dict[str, Any]:
        nonlocal calls
        calls += 1
        state = "pending" if calls == 1 else "connected"
        return _single_server_metadata(
            {
                "serverName": "remote",
                "scope": "user",
                "transport": "http",
                "state": state,
                "authState": "not-configured",
                "toolsCount": 0,
                "resourcesCount": 0,
                "promptsCount": 0,
            }
        )

    dialog = MCPManagerDialog(_context(), metadata_provider=metadata)

    assert "remote · ○ connecting…" in _render_text(dialog.render())
    assert dialog.handle_key(KeyEvent("r", "r")) is True
    output = _render_text(dialog.render())

    assert "remote · ✓ connected" in output
    assert calls == 2


def test_mcp_manager_dialog_renders_config_details_for_stdio_and_remote() -> None:
    stdio_dialog = MCPManagerDialog(
        _context(),
        metadata_provider=lambda: _single_server_metadata(
            {
                "serverName": "stdio",
                "scope": "project",
                "transport": "stdio",
                "state": "connected",
                "authState": "not-configured",
                "toolsCount": 1,
                "resourcesCount": 2,
                "promptsCount": 3,
                "sourcePath": "/repo/.mcp.json",
                "command": "npx",
                "args": ["-y", "mcp-server"],
            }
        ),
    )

    stdio_dialog.handle_key(KeyEvent("enter", "\n"))
    stdio_output = _render_text(stdio_dialog.render())

    assert "Status: ✓ connected" in stdio_output
    assert "Command: npx" in stdio_output
    assert "Args: -y mcp-server" in stdio_output
    assert "Config location: project (/repo/.mcp.json)" in stdio_output
    assert "Transport:" not in stdio_output
    assert "Auth:" not in stdio_output
    assert "Capabilities: tools, resources, prompts" in stdio_output
    assert "Capabilities: 1/2/3" not in stdio_output
    assert "Tools: 1 tool" in stdio_output
    assert "Resources: 2 resources" in stdio_output
    assert "Prompts: 3 prompts" in stdio_output
    assert "Latest failure" not in stdio_output

    remote_dialog = MCPManagerDialog(
        _context(),
        metadata_provider=lambda: _single_server_metadata(
            {
                "serverName": "remote",
                "scope": "user",
                "transport": "http",
                "state": "connected",
                "authState": "configured",
                "toolsCount": 4,
                "resourcesCount": 0,
                "promptsCount": 1,
                "sourcePath": "/home/user/settings.yml",
                "url": "https://mcp.example.test/mcp",
            }
        ),
    )

    remote_dialog.handle_key(KeyEvent("enter", "\n"))
    remote_output = _render_text(remote_dialog.render())

    assert "Status: ✓ connected" in remote_output
    assert "URL: https://mcp.example.test/mcp" in remote_output
    assert "Auth: ✓ authenticated" in remote_output
    assert "Transport:" not in remote_output
    assert "Config location: user (/home/user/settings.yml)" in remote_output
    assert "Capabilities: tools, prompts" in remote_output
    assert "Tools: 4 tools" in remote_output
    assert "Resources: 0 resources" in remote_output
    assert "Prompts: 1 prompt" in remote_output
    assert "Latest failure" not in remote_output


def test_mcp_manager_dialog_renders_structured_config_diagnostics() -> None:
    dialog = MCPManagerDialog(
        _context(),
        metadata_provider=lambda: {
            "servers": [],
            "warnings": [
                {
                    "severity": "warning",
                    "scope": "project",
                    "sourcePath": "/repo/.mcp.json",
                    "serverName": "remote",
                    "path": "mcpServers.remote.headers",
                    "message": "Unknown field.",
                },
                {
                    "severity": "fatal",
                    "scope": "user",
                    "sourcePath": "/home/user/settings.yml",
                    "path": "mcpServers.bad",
                    "message": "Config must be an object.",
                },
            ],
        },
    )

    output = _render_text(dialog.render())

    assert "MCP Config Diagnostics" in output
    assert "For help configuring MCP servers" in output
    assert "Project MCPs" in output
    assert "Location: /repo/.mcp.json" in output
    assert "[Warning] [remote] mcpServers.remote.headers: Unknown field." in output
    assert "User MCPs" in output
    assert "Location: /home/user/settings.yml" in output
    assert "[Error] mcpServers.bad: Config must be an object." in output


def test_mcp_manager_dialog_auth_action_uses_step_up_scopes_and_refreshes_runtime() -> None:
    auth_calls: list[tuple[str, str | None, list[str] | None, str | None]] = []
    refreshed: list[bool] = []

    async def refresh_mcp_integrations() -> None:
        refreshed.append(True)

    live_manager = _FakeLiveMCPManager("connected")
    live_manager.required_auth_scopes = lambda name: ["doc:read"] if name == "remote" else []
    live_manager.required_auth_resource_metadata_url = lambda name: (
        "https://resource.example/.well-known/oauth-protected-resource/mcp" if name == "remote" else None
    )
    repl = SimpleNamespace(
        _mcp_manager=live_manager,
        refresh_mcp_integrations=refresh_mcp_integrations,
    )
    dialog = MCPManagerDialog(
        _context(repl=repl),
        metadata_provider=_sample_metadata,
        actions={
            "auth": lambda name, scope, required_scopes=None, resource_metadata_url=None: (
                auth_calls.append((name, scope, list(required_scopes or []), resource_metadata_url))
                or "Authenticated MCP server 'remote'."
            ),
        },
    )

    dialog.handle_key(KeyEvent("down", ""))
    dialog.handle_key(KeyEvent("enter", "\n"))
    dialog.handle_key(KeyEvent("enter", "\n"))

    assert auth_calls == [
        ("remote", "user", ["doc:read"], "https://resource.example/.well-known/oauth-protected-resource/mcp")
    ]
    assert refreshed == [True]
    assert dialog.result_message == "Authentication successful. Connected to remote."


def test_mcp_manager_dialog_auth_action_uses_metadata_step_up_scopes_without_live_manager() -> None:
    auth_calls: list[tuple[str, str | None, list[str], str | None]] = []
    refreshed: list[bool] = []

    async def refresh_mcp_integrations() -> None:
        refreshed.append(True)

    repl = SimpleNamespace(refresh_mcp_integrations=refresh_mcp_integrations)
    dialog = MCPManagerDialog(
        _context(repl=repl),
        metadata_provider=lambda: _single_server_metadata(
            {
                "serverName": "remote",
                "scope": "user",
                "transport": "http",
                "state": "needs_auth",
                "authState": "needs-auth",
                "requiredAuthScopes": ["doc:write"],
                "authResourceMetadataUrl": "https://resource.example/.well-known/oauth-protected-resource/mcp",
                "toolsCount": 0,
                "resourcesCount": 0,
                "promptsCount": 0,
            }
        ),
        actions={
            "auth": lambda name, scope, required_scopes=None, resource_metadata_url=None: (
                auth_calls.append((name, scope, list(required_scopes or []), resource_metadata_url))
                or "Authenticated MCP server 'remote'."
            ),
        },
    )

    dialog.handle_key(KeyEvent("enter", "\n"))
    dialog.handle_key(KeyEvent("enter", "\n"))

    assert auth_calls == [
        ("remote", "user", ["doc:write"], "https://resource.example/.well-known/oauth-protected-resource/mcp")
    ]
    assert refreshed == [True]
    assert dialog.result_message == (
        "Authentication successful, but server reconnection failed. "
        "You may need to manually restart iac-code for the changes to take effect."
    )


def test_mcp_manager_dialog_auth_ignores_same_name_live_server_from_other_scope() -> None:
    auth_calls: list[tuple[str, str | None, list[str], str | None]] = []
    refreshed: list[bool] = []

    async def refresh_mcp_integrations() -> None:
        refreshed.append(True)

    class FakeScopedLiveManager:
        def connection(self, server_name: str) -> object:
            if server_name != "shared":
                raise KeyError(server_name)
            return SimpleNamespace(
                scoped_config=SimpleNamespace(scope=SimpleNamespace(value="local")),
                state="connected",
            )

        def connection_state(self, server_name: str) -> str:
            assert server_name == "shared"
            return "connected"

        def required_auth_scopes(self, server_name: str) -> list[str]:
            assert server_name == "shared"
            return ["local:write"]

        def required_auth_resource_metadata_url(self, server_name: str) -> str:
            assert server_name == "shared"
            return "https://local.example/.well-known/oauth-protected-resource/mcp"

    def metadata() -> dict[str, Any]:
        return {
            "servers": [
                {
                    "serverName": "shared",
                    "scope": "local",
                    "transport": "http",
                    "state": "connected",
                    "authState": "configured",
                    "toolsCount": 0,
                    "resourcesCount": 0,
                    "promptsCount": 0,
                    "url": "https://local.example/mcp",
                },
                {
                    "serverName": "shared",
                    "scope": "user",
                    "transport": "http",
                    "state": "needs_auth",
                    "authState": "needs-auth",
                    "toolsCount": 0,
                    "resourcesCount": 0,
                    "promptsCount": 0,
                    "url": "https://user.example/mcp",
                },
            ],
            "warnings": [],
        }

    repl = SimpleNamespace(_mcp_manager=FakeScopedLiveManager(), refresh_mcp_integrations=refresh_mcp_integrations)
    dialog = MCPManagerDialog(
        _context(repl=repl),
        metadata_provider=metadata,
        actions={
            "auth": lambda name, scope, required_scopes=None, resource_metadata_url=None: (
                auth_calls.append((name, scope, list(required_scopes or []), resource_metadata_url))
                or "Authenticated MCP server 'shared'."
            ),
        },
    )

    dialog.handle_key(KeyEvent("down", ""))
    dialog.handle_key(KeyEvent("enter", "\n"))
    dialog.handle_key(KeyEvent("enter", "\n"))

    assert auth_calls == [("shared", "user", [], None)]
    assert refreshed == [True]
    assert dialog.result_message == (
        "Authentication successful, but server reconnection failed. "
        "You may need to manually restart iac-code for the changes to take effect."
    )


def test_mcp_manager_dialog_disabled_remote_auth_reports_saved_auth_without_reconnect_failure() -> None:
    pending = _FakePendingOAuthFlow()
    refreshed: list[bool] = []

    async def refresh_mcp_integrations() -> None:
        refreshed.append(True)

    repl = SimpleNamespace(refresh_mcp_integrations=refresh_mcp_integrations)
    dialog = MCPManagerDialog(
        _context(repl=repl),
        metadata_provider=lambda: _single_server_metadata(
            {
                "serverName": "remote",
                "scope": "user",
                "transport": "http",
                "state": "disabled",
                "authState": "needs-auth",
                "toolsCount": 0,
                "resourcesCount": 0,
                "promptsCount": 0,
            }
        ),
        actions={"auth": lambda name, scope, required_scopes=None, resource_metadata_url=None: pending},
    )

    dialog.handle_key(KeyEvent("enter", "\n"))
    dialog.handle_key(KeyEvent("down", ""))
    dialog.handle_key(KeyEvent("enter", "\n"))
    dialog.handle_key(KeyEvent("paste", "http://127.0.0.1/callback?code=ok"))
    dialog.handle_key(KeyEvent("enter", "\n"))
    output = _render_until(dialog, "Authentication successful. Enable remote to connect.")

    assert pending.manual_values == ["http://127.0.0.1/callback?code=ok"]
    assert refreshed == [True]
    assert "server reconnection failed" not in output
    assert "Authentication successful. Enable remote to connect." in output


def test_mcp_manager_dialog_auth_error_stays_in_menu_with_error_prefix_like_claude_panel() -> None:
    def fail_auth(
        name: str,
        scope: str | None,
        required_scopes: list[str] | None = None,
        resource_metadata_url: str | None = None,
    ) -> None:
        _ = name, scope, required_scopes, resource_metadata_url
        raise RuntimeError("boom")

    dialog = MCPManagerDialog(
        _context(),
        metadata_provider=lambda: _single_server_metadata(
            {
                "serverName": "remote",
                "scope": "user",
                "transport": "http",
                "state": "needs_auth",
                "authState": "needs-auth",
                "toolsCount": 0,
                "resourcesCount": 0,
                "promptsCount": 0,
            }
        ),
        actions={"auth": fail_auth},
    )

    dialog.handle_key(KeyEvent("enter", "\n"))
    dialog.handle_key(KeyEvent("enter", "\n"))
    output = _render_text(dialog.render())

    assert "Error: boom" in output
    assert "Actions" in output
    assert dialog._done is False


def test_mcp_manager_dialog_gates_disabled_stdio_actions_like_claude_panel() -> None:
    dialog = MCPManagerDialog(
        _context(),
        metadata_provider=lambda: _single_server_metadata(
            {
                "serverName": "disabled",
                "scope": "user",
                "transport": "stdio",
                "state": "disabled",
                "authState": "not-configured",
                "toolsCount": 1,
                "resourcesCount": 0,
                "promptsCount": 0,
                "tools": [{"publicName": "mcp__disabled__tool", "originalToolName": "tool"}],
            }
        ),
    )

    dialog.handle_key(KeyEvent("enter", "\n"))
    output = _render_text(dialog.render())

    assert "Enable" in output
    assert "Status: ○ disabled" in output
    assert "View tools" not in output
    assert "Authenticate" not in output
    assert "Re-authenticate" not in output
    assert "Clear authentication" not in output
    assert "Reconnect" not in output
    assert "│     Disable" not in output
    assert "Capabilities:" not in output
    assert "Tools:" not in output
    assert "Prompts:" not in output
    assert "Resources:" not in output


def test_mcp_manager_dialog_disabled_remote_keeps_auth_actions_like_claude_panel() -> None:
    dialog = MCPManagerDialog(
        _context(),
        metadata_provider=lambda: _single_server_metadata(
            {
                "serverName": "remote",
                "scope": "user",
                "transport": "http",
                "state": "disabled",
                "authState": "configured",
                "toolsCount": 1,
                "resourcesCount": 0,
                "promptsCount": 0,
                "tools": [{"publicName": "mcp__remote__tool", "originalToolName": "tool"}],
            }
        ),
    )

    dialog.handle_key(KeyEvent("enter", "\n"))
    output = _render_text(dialog.render())

    assert "Enable" in output
    assert "Auth: ✓ authenticated" in output
    assert "Re-authenticate" in output
    assert "Clear authentication" in output
    assert "View tools" not in output
    assert "Reconnect" not in output
    assert "│     Disable" not in output
    assert "Capabilities:" not in output
    assert "Tools:" not in output


def test_mcp_manager_dialog_disabled_remote_keeps_authenticate_action_like_claude_panel() -> None:
    dialog = MCPManagerDialog(
        _context(),
        metadata_provider=lambda: _single_server_metadata(
            {
                "serverName": "remote",
                "scope": "user",
                "transport": "http",
                "state": "disabled",
                "authState": "not-configured",
                "toolsCount": 0,
                "resourcesCount": 0,
                "promptsCount": 0,
                "failureReason": "Required scopes: doc:read",
            }
        ),
    )

    dialog.handle_key(KeyEvent("enter", "\n"))
    output = _render_text(dialog.render())

    assert "Enable" in output
    assert "Auth: ✖ not authenticated" in output
    assert "Authenticate" in output
    assert "Re-authenticate" not in output
    assert "Clear authentication" not in output
    assert "Reconnect" not in output
    assert "│     Disable" not in output
    assert "Error: Required scopes: doc:read" not in output


def test_mcp_manager_dialog_connected_remote_without_configured_auth_does_not_show_reauth_actions() -> None:
    dialog = MCPManagerDialog(
        _context(),
        metadata_provider=lambda: _single_server_metadata(
            {
                "serverName": "remote",
                "scope": "user",
                "transport": "http",
                "state": "connected",
                "authState": "not-configured",
                "toolsCount": 1,
                "resourcesCount": 0,
                "promptsCount": 0,
                "tools": [{"publicName": "mcp__remote__tool", "originalToolName": "tool"}],
            }
        ),
    )

    dialog.handle_key(KeyEvent("enter", "\n"))
    output = _render_text(dialog.render())

    assert "Auth: ✖ not authenticated" in output
    assert "Authenticate" in output
    assert "Re-authenticate" not in output
    assert "Clear authentication" not in output


@pytest.mark.parametrize("scope", ["session", "dynamic"])
def test_mcp_manager_dialog_hides_persisted_actions_for_non_persisted_servers(scope: str) -> None:
    dialog = MCPManagerDialog(
        _context(),
        metadata_provider=lambda: _single_server_metadata(
            {
                "serverName": "ephemeral",
                "scope": scope,
                "transport": "http",
                "state": "connected",
                "authState": "configured",
                "toolsCount": 1,
                "resourcesCount": 0,
                "promptsCount": 0,
                "tools": [{"publicName": "mcp__ephemeral__tool", "originalToolName": "tool"}],
            }
        ),
    )

    dialog.handle_key(KeyEvent("enter", "\n"))
    output = _render_text(dialog.render())

    assert "View tools" in output
    assert "Authenticate" not in output
    assert "Re-authenticate" not in output
    assert "Clear authentication" not in output
    assert "Reconnect" not in output
    assert "Disable" not in output
    assert "Remove" not in output


def test_mcp_manager_dialog_gates_needs_auth_remote_actions() -> None:
    dialog = MCPManagerDialog(
        _context(),
        metadata_provider=lambda: _single_server_metadata(
            {
                "serverName": "remote",
                "scope": "user",
                "transport": "http",
                "state": "needs_auth",
                "authState": "needs-auth",
                "toolsCount": 0,
                "resourcesCount": 0,
                "promptsCount": 0,
                "failureReason": "Required scopes: doc:read",
            }
        ),
    )

    dialog.handle_key(KeyEvent("enter", "\n"))
    output = _render_text(dialog.render())

    assert "Authenticate" in output
    assert "Status: △ needs authentication" in output
    assert "Auth: ✖ not authenticated" in output
    assert "Disable" in output
    assert "Reconnect" not in output
    assert "Clear authentication" not in output
    assert "Capabilities:" not in output
    assert "Tools:" not in output
    assert "Prompts:" not in output
    assert "Resources:" not in output
    assert "Error: Required scopes: doc:read" not in output


def test_mcp_manager_dialog_pending_approval_offers_approve_reject_and_disable_actions() -> None:
    dialog = MCPManagerDialog(
        _context(),
        metadata_provider=lambda: _single_server_metadata(
            {
                "serverName": "project-pending",
                "scope": "project",
                "sourcePath": "/repo/.mcp.json",
                "_sourcePath": "/repo/.mcp.json",
                "transport": "http",
                "state": "pending-approval",
                "authState": "configured",
                "toolsCount": 0,
                "resourcesCount": 0,
                "promptsCount": 0,
                "url": "https://mcp.example.test/mcp",
            }
        ),
    )

    dialog.handle_key(KeyEvent("enter", "\n"))
    output = _render_text(dialog.render())

    assert "Status: ○ pending approval" in output
    assert "Approve" in output
    assert "Reject" in output
    assert "Disable" in output
    assert "Authenticate" not in output
    assert "Re-authenticate" not in output
    assert "Clear authentication" not in output
    assert "Reconnect" not in output


def test_mcp_manager_dialog_pending_approval_approve_action_refreshes_runtime() -> None:
    calls: list[tuple[str, str | None, str | None]] = []
    refreshed: list[bool] = []

    async def refresh_mcp_integrations() -> None:
        refreshed.append(True)

    repl = SimpleNamespace(refresh_mcp_integrations=refresh_mcp_integrations)
    dialog = MCPManagerDialog(
        _context(repl=repl),
        metadata_provider=lambda: _single_server_metadata(
            {
                "serverName": "project-pending",
                "scope": "project",
                "sourcePath": "/repo/.mcp.json",
                "_sourcePath": "/repo/.mcp.json",
                "transport": "http",
                "state": "pending-approval",
                "authState": "configured",
                "toolsCount": 0,
                "resourcesCount": 0,
                "promptsCount": 0,
                "url": "https://mcp.example.test/mcp",
            }
        ),
        actions={
            "approve": lambda name, scope, source_path=None: (
                calls.append((name, scope, source_path)) or "Approved MCP server 'project-pending'."
            )
        },
    )

    dialog.handle_key(KeyEvent("enter", "\n"))
    dialog.handle_key(KeyEvent("enter", "\n"))

    assert calls == [("project-pending", "project", "/repo/.mcp.json")]
    assert refreshed == [True]
    assert dialog.result_message == "Approved MCP server 'project-pending'."


def test_mcp_manager_dialog_pending_approval_reject_action_refreshes_runtime() -> None:
    calls: list[tuple[str, str | None, str | None]] = []
    refreshed: list[bool] = []

    async def refresh_mcp_integrations() -> None:
        refreshed.append(True)

    repl = SimpleNamespace(refresh_mcp_integrations=refresh_mcp_integrations)
    dialog = MCPManagerDialog(
        _context(repl=repl),
        metadata_provider=lambda: _single_server_metadata(
            {
                "serverName": "project-pending",
                "scope": "project",
                "sourcePath": "/repo/.mcp.json",
                "_sourcePath": "/repo/.mcp.json",
                "transport": "http",
                "state": "pending-approval",
                "authState": "configured",
                "toolsCount": 0,
                "resourcesCount": 0,
                "promptsCount": 0,
                "url": "https://mcp.example.test/mcp",
            }
        ),
        actions={
            "reject": lambda name, scope, source_path=None: (
                calls.append((name, scope, source_path)) or "Rejected MCP server 'project-pending'."
            )
        },
    )

    dialog.handle_key(KeyEvent("enter", "\n"))
    dialog.handle_key(KeyEvent("down", ""))
    dialog.handle_key(KeyEvent("enter", "\n"))

    assert calls == [("project-pending", "project", "/repo/.mcp.json")]
    assert refreshed == [True]
    assert dialog.result_message == "Rejected MCP server 'project-pending'."


def test_mcp_manager_dialog_needs_auth_remote_with_auth_state_matches_claude_reauth_actions() -> None:
    dialog = MCPManagerDialog(
        _context(),
        metadata_provider=lambda: _single_server_metadata(
            {
                "serverName": "remote",
                "scope": "user",
                "transport": "http",
                "state": "needs_auth",
                "authState": "configured",
                "toolsCount": 0,
                "resourcesCount": 0,
                "promptsCount": 0,
                "failureReason": "Required scopes: doc:read",
            }
        ),
    )

    dialog.handle_key(KeyEvent("enter", "\n"))
    output = _render_text(dialog.render())

    assert "Status: △ needs authentication" in output
    assert "Auth: ✓ authenticated" in output
    assert "Re-authenticate" in output
    assert "Clear authentication" in output
    assert "Authenticate" not in output
    assert "Reconnect" not in output
    assert "Disable" in output
    assert "Error: Required scopes: doc:read" not in output


def test_mcp_manager_dialog_failed_server_shows_failure_details_like_claude_panel() -> None:
    dialog = MCPManagerDialog(
        _context(),
        metadata_provider=lambda: _single_server_metadata(
            {
                "serverName": "broken",
                "scope": "user",
                "transport": "http",
                "state": "failed",
                "authState": "configured",
                "toolsCount": 1,
                "resourcesCount": 2,
                "promptsCount": 3,
                "failureReason": "connection refused with access_token=super-secret-token",
                "latestFailureReason": "tool call failed with api_key=secret-key",
                "capabilityErrors": {
                    "tools": "tools failed with password=secret-password",
                    "resources": "resources unavailable",
                },
                "latestRefreshFailureReason": "refresh failed with Authorization: Bearer secret-token",
            }
        ),
    )

    dialog.handle_key(KeyEvent("enter", "\n"))
    output = _render_text(dialog.render())

    assert "Failure: connection refused with access_token=[REDACTED]" in output
    assert "Latest failure: tool call failed with api_key=[REDACTED]" in output
    assert "Tools refresh: tools failed with password=[REDACTED]" in output
    assert "Resources refresh: resources unavailable" in output
    assert "Latest refresh failure: refresh failed with [REDACTED]" in output
    assert "super-secret-token" not in output
    assert "secret-key" not in output
    assert "secret-password" not in output
    assert "secret-token" not in output
    assert "Capabilities:" not in output
    assert "Tools:" not in output
    assert "Prompts:" not in output
    assert "Resources:" not in output


def test_mcp_manager_dialog_gates_stdio_actions_without_auth() -> None:
    dialog = MCPManagerDialog(
        _context(),
        metadata_provider=lambda: _single_server_metadata(
            {
                "serverName": "stdio",
                "scope": "project",
                "transport": "stdio",
                "state": "connected",
                "authState": "not-configured",
                "toolsCount": 1,
                "resourcesCount": 0,
                "promptsCount": 0,
                "tools": [{"publicName": "mcp__stdio__tool", "originalToolName": "tool"}],
            }
        ),
    )

    dialog.handle_key(KeyEvent("enter", "\n"))
    output = _render_text(dialog.render())

    assert "View tools" in output
    assert "Reconnect" in output
    assert "Disable" in output
    assert "Authenticate" not in output
    assert "Clear authentication" not in output
    assert "Back" not in output


def test_mcp_manager_dialog_server_menu_shows_numeric_indexes_like_claude_select() -> None:
    dialog = MCPManagerDialog(
        _context(),
        metadata_provider=lambda: _single_server_metadata(
            {
                "serverName": "stdio",
                "scope": "project",
                "transport": "stdio",
                "state": "connected",
                "authState": "not-configured",
                "toolsCount": 1,
                "resourcesCount": 0,
                "promptsCount": 0,
                "tools": [{"publicName": "mcp__stdio__tool", "originalToolName": "tool"}],
            }
        ),
    )

    dialog.handle_key(KeyEvent("enter", "\n"))
    output = _render_text(dialog.render())

    assert "1. View tools" in output
    assert "2. Reconnect" in output
    assert "3. Disable" in output


def test_mcp_manager_dialog_server_menu_supports_select_jk_like_claude_panel() -> None:
    calls: list[tuple[str, str | None]] = []

    dialog = MCPManagerDialog(
        _context(),
        metadata_provider=lambda: _single_server_metadata(
            {
                "serverName": "stdio",
                "scope": "project",
                "transport": "stdio",
                "state": "connected",
                "authState": "not-configured",
                "toolsCount": 1,
                "resourcesCount": 0,
                "promptsCount": 0,
                "tools": [{"publicName": "mcp__stdio__tool", "originalToolName": "tool"}],
            }
        ),
        actions={"reconnect": lambda name, scope: calls.append((name, scope)) or "Reconnected to stdio."},
    )

    dialog.handle_key(KeyEvent("enter", "\n"))
    dialog.handle_key(KeyEvent("j", "j"))
    dialog.handle_key(KeyEvent("enter", "\n"))
    _render_until(dialog, "Reconnected to stdio.")

    assert calls == [("stdio", "project")]


def test_mcp_manager_dialog_server_menu_supports_numeric_select_like_claude_panel() -> None:
    calls: list[tuple[str, str | None]] = []

    dialog = MCPManagerDialog(
        _context(),
        metadata_provider=lambda: _single_server_metadata(
            {
                "serverName": "stdio",
                "scope": "project",
                "transport": "stdio",
                "state": "connected",
                "authState": "not-configured",
                "toolsCount": 1,
                "resourcesCount": 0,
                "promptsCount": 0,
                "tools": [{"publicName": "mcp__stdio__tool", "originalToolName": "tool"}],
            }
        ),
        actions={"disable": lambda name, scope: calls.append((name, scope)) or "Disabled MCP server 'stdio'."},
    )

    dialog.handle_key(KeyEvent("enter", "\n"))
    dialog.handle_key(KeyEvent("3", "3"))

    assert calls == [("stdio", "project")]
    assert "Project MCPs" in _render_text(dialog.render())


def test_mcp_manager_dialog_server_menu_supports_page_keys_like_claude_select() -> None:
    calls: list[tuple[str, str | None]] = []

    dialog = MCPManagerDialog(
        _context(),
        metadata_provider=lambda: _single_server_metadata(
            {
                "serverName": "remote",
                "scope": "user",
                "transport": "http",
                "state": "connected",
                "authState": "configured",
                "toolsCount": 1,
                "resourcesCount": 0,
                "promptsCount": 0,
                "url": "https://mcp.example.test/mcp",
                "tools": [{"publicName": "mcp__remote__tool", "originalToolName": "tool"}],
            }
        ),
        actions={"remove": lambda name, scope: calls.append((name, scope)) or "Removed MCP server 'remote'."},
    )

    dialog.handle_key(KeyEvent("enter", "\n"))
    dialog.handle_key(KeyEvent("pagedown", ""))
    dialog.handle_key(KeyEvent("enter", "\n"))

    assert calls == [("remote", "user")]


def test_mcp_manager_dialog_tools_view_shows_numeric_indexes_like_claude_select() -> None:
    dialog = MCPManagerDialog(
        _context(),
        metadata_provider=lambda: _single_server_metadata(
            {
                "serverName": "stdio",
                "scope": "project",
                "transport": "stdio",
                "state": "connected",
                "authState": "not-configured",
                "toolsCount": 2,
                "resourcesCount": 0,
                "promptsCount": 0,
                "tools": [
                    {"publicName": "mcp__stdio__first", "originalToolName": "first"},
                    {"publicName": "mcp__stdio__second", "originalToolName": "second"},
                ],
            }
        ),
    )

    dialog.handle_key(KeyEvent("enter", "\n"))
    dialog.handle_key(KeyEvent("enter", "\n"))
    output = _render_text(dialog.render())

    assert "1. first" in output
    assert "2. second" in output


def test_mcp_manager_dialog_tools_view_supports_select_jk_and_numeric_like_claude_panel() -> None:
    def metadata() -> dict[str, Any]:
        return _single_server_metadata(
            {
                "serverName": "stdio",
                "scope": "project",
                "transport": "stdio",
                "state": "connected",
                "authState": "not-configured",
                "toolsCount": 2,
                "resourcesCount": 0,
                "promptsCount": 0,
                "tools": [
                    {"publicName": "mcp__stdio__first", "originalToolName": "first"},
                    {"publicName": "mcp__stdio__second", "originalToolName": "second"},
                ],
            }
        )

    dialog = MCPManagerDialog(_context(), metadata_provider=metadata)
    dialog.handle_key(KeyEvent("enter", "\n"))
    dialog.handle_key(KeyEvent("enter", "\n"))
    dialog.handle_key(KeyEvent("j", "j"))
    dialog.handle_key(KeyEvent("enter", "\n"))

    assert "second" in _render_text(dialog.render())

    dialog = MCPManagerDialog(_context(), metadata_provider=metadata)
    dialog.handle_key(KeyEvent("enter", "\n"))
    dialog.handle_key(KeyEvent("enter", "\n"))
    dialog.handle_key(KeyEvent("2", "2"))

    assert "second" in _render_text(dialog.render())


def test_mcp_manager_dialog_tools_view_supports_page_keys_like_claude_select() -> None:
    tools = [{"publicName": f"mcp__stdio__tool_{index}", "originalToolName": f"tool-{index}"} for index in range(1, 8)]
    dialog = MCPManagerDialog(
        _context(),
        metadata_provider=lambda: _single_server_metadata(
            {
                "serverName": "stdio",
                "scope": "project",
                "transport": "stdio",
                "state": "connected",
                "authState": "not-configured",
                "toolsCount": len(tools),
                "resourcesCount": 0,
                "promptsCount": 0,
                "tools": tools,
            }
        ),
    )

    dialog.handle_key(KeyEvent("enter", "\n"))
    dialog.handle_key(KeyEvent("enter", "\n"))
    initial_output = _render_text(dialog.render())
    assert "1. tool-1" in initial_output
    assert "5. tool-5" in initial_output
    assert "5. tool-5  ↓" in initial_output
    assert "↑" not in initial_output
    assert "6. tool-6" not in initial_output

    dialog.handle_key(KeyEvent("pagedown", ""))
    paged_output = _render_text(dialog.render())
    assert "1. tool-1" not in paged_output
    assert "2. tool-2" in paged_output
    assert "2. tool-2  ↑" in paged_output
    assert "6. tool-6" in paged_output
    assert "6. tool-6  ↓" in paged_output
    assert "7. tool-7" not in paged_output

    dialog.handle_key(KeyEvent("enter", "\n"))
    output = _render_text(dialog.render())

    assert "tool-6" in output
    assert "Tool name: tool-6" in output


def test_mcp_manager_dialog_empty_tools_view_matches_claude_copy() -> None:
    dialog = MCPManagerDialog(
        _context(),
        metadata_provider=lambda: _single_server_metadata(
            {
                "serverName": "stdio",
                "scope": "project",
                "transport": "stdio",
                "state": "connected",
                "authState": "not-configured",
                "toolsCount": 0,
                "resourcesCount": 0,
                "promptsCount": 0,
            }
        ),
    )

    dialog.handle_key(KeyEvent("enter", "\n"))
    dialog._view = "tools"
    output = _render_text(dialog.render())

    assert "No tools available" in output
    assert "No tools reported for this MCP server." not in output
    assert "Enter select" in output
    assert "Enter details" not in output


def test_mcp_manager_dialog_tool_detail_hides_raw_schema_without_parameters() -> None:
    dialog = MCPManagerDialog(
        _context(),
        metadata_provider=lambda: _single_server_metadata(
            {
                "serverName": "stdio",
                "scope": "project",
                "transport": "stdio",
                "state": "connected",
                "authState": "not-configured",
                "toolsCount": 1,
                "resourcesCount": 0,
                "promptsCount": 0,
                "tools": [
                    {
                        "publicName": "mcp__stdio__ping",
                        "originalToolName": "ping",
                        "description": "Ping the server",
                        "inputSchema": {"type": "object"},
                    }
                ],
            }
        ),
    )

    dialog.handle_key(KeyEvent("enter", "\n"))
    dialog.handle_key(KeyEvent("enter", "\n"))
    dialog.handle_key(KeyEvent("enter", "\n"))
    output = _render_text(dialog.render())

    assert "Description:" in output
    assert "Parameters:" not in output
    assert "Input schema" not in output
    assert '"type": "object"' not in output


def test_mcp_manager_dialog_sanitizes_terminal_controls_from_capability_views() -> None:
    dialog = MCPManagerDialog(
        _context(),
        metadata_provider=lambda: _single_server_metadata(
            {
                "serverName": "stdio",
                "scope": "project",
                "transport": "stdio",
                "state": "connected",
                "authState": "not-configured",
                "toolsCount": 1,
                "resourcesCount": 1,
                "promptsCount": 1,
                "tools": [
                    {
                        "publicName": "mcp__stdio__tool",
                        "originalToolName": "wipe\x1b[2J\x1b]0;owned\x07tool",
                        "description": "tool desc\x1b[31mred",
                        "inputSchema": {
                            "type": "object",
                            "properties": {
                                "path\x1b[2J": {
                                    "type": "string",
                                    "description": "path desc\x07bell",
                                }
                            },
                            "required": ["path\x1b[2J"],
                        },
                    }
                ],
                "resources": [
                    {
                        "uri": "resource://stdio/doc\x1b[2J",
                        "name": "doc\x1b]2;owned\x1b\\name",
                        "title": "title\x1b]0;owned\x07",
                        "description": "resource desc\x1b[31mred",
                        "mimeType": "text/plain\x07",
                    }
                ],
                "prompts": [
                    {
                        "publicName": "mcp__stdio__prompt",
                        "originalPromptName": "prompt\x9b2Jname",
                        "description": "prompt desc\x1b]0;owned\x07",
                        "arguments": [{"name": "topic\x1b[2J", "description": "topic desc\x9b31m"}],
                    }
                ],
            }
        ),
    )

    dialog.handle_key(KeyEvent("enter", "\n"))
    dialog._view = "tools"
    tools_output = _render_text(dialog.render())
    dialog.handle_key(KeyEvent("enter", "\n"))
    tool_detail_output = _render_text(dialog.render())
    dialog._view = "resources"
    resources_output = _render_text(dialog.render())
    dialog.handle_key(KeyEvent("enter", "\n"))
    resource_detail_output = _render_text(dialog.render())
    dialog._view = "prompts"
    prompts_output = _render_text(dialog.render())
    dialog.handle_key(KeyEvent("enter", "\n"))
    prompt_detail_output = _render_text(dialog.render())

    rendered = "\n".join(
        [
            tools_output,
            tool_detail_output,
            resources_output,
            resource_detail_output,
            prompts_output,
            prompt_detail_output,
        ]
    )
    for control in ("\x1b", "\x07", "\x9b"):
        assert control not in rendered
    assert "wipe" in rendered
    assert "tool" in rendered
    assert "doc" in rendered
    assert "prompt" in rendered
    assert "path (required): string - path descbell" in rendered
    assert "topic - topic desc" in rendered


def test_mcp_manager_dialog_annotations_match_claude_supported_badges() -> None:
    dialog = MCPManagerDialog(
        _context(),
        metadata_provider=lambda: _single_server_metadata(
            {
                "serverName": "stdio",
                "scope": "project",
                "transport": "stdio",
                "state": "connected",
                "authState": "not-configured",
                "toolsCount": 1,
                "resourcesCount": 0,
                "promptsCount": 0,
                "tools": [
                    {
                        "publicName": "mcp__stdio__danger",
                        "originalToolName": "danger",
                        "annotations": {
                            "destructiveHint": True,
                            "idempotentHint": True,
                            "openWorldHint": True,
                        },
                    }
                ],
            }
        ),
    )

    dialog.handle_key(KeyEvent("enter", "\n"))
    dialog.handle_key(KeyEvent("enter", "\n"))
    tools_output = _render_text(dialog.render())

    assert "destructive" in tools_output
    assert "open-world" in tools_output
    assert "idempotent" not in tools_output

    dialog.handle_key(KeyEvent("enter", "\n"))
    detail_output = _render_text(dialog.render())

    assert "[destructive]" in detail_output
    assert "[open-world]" in detail_output
    assert "idempotent" not in detail_output


def test_mcp_manager_dialog_reconnect_uses_live_manager_like_claude_panel() -> None:
    reconnected: list[str] = []

    class FakeLiveMCPManager:
        def connection(self, server_name: str) -> object:
            if server_name != "stdio":
                raise KeyError(server_name)
            return object()

        async def reconnect(self, server_name: str) -> None:
            reconnected.append(server_name)

        def connection_state(self, server_name: str) -> str:
            assert server_name == "stdio"
            return "connected"

    async def refresh_mcp_integrations() -> None:
        raise AssertionError("single-server reconnect should not trigger a full MCP refresh")

    dialog = MCPManagerDialog(
        _context(
            repl=SimpleNamespace(
                _mcp_manager=FakeLiveMCPManager(),
                refresh_mcp_integrations=refresh_mcp_integrations,
            )
        ),
        metadata_provider=lambda: _single_server_metadata(
            {
                "serverName": "stdio",
                "scope": "project",
                "transport": "stdio",
                "state": "connected",
                "authState": "not-configured",
                "toolsCount": 0,
                "resourcesCount": 0,
                "promptsCount": 0,
            }
        ),
    )

    dialog.handle_key(KeyEvent("enter", "\n"))
    dialog.handle_key(KeyEvent("enter", "\n"))
    _render_until(dialog, "Reconnected to stdio.")

    assert reconnected == ["stdio"]
    assert dialog.result_message == "Reconnected to stdio."
    assert dialog._done is True


def test_mcp_manager_dialog_reconnect_same_name_different_scope_uses_scoped_cli_action() -> None:
    cli_reconnected: list[tuple[str | None, str | None]] = []

    class FakeLiveMCPManager:
        def connection(self, server_name: str) -> object:
            if server_name != "shared":
                raise KeyError(server_name)
            return SimpleNamespace(scoped_config=SimpleNamespace(scope=SimpleNamespace(value="local")))

        async def reconnect(self, server_name: str) -> None:
            raise AssertionError("shadowed persisted entry must not reconnect the same-name live server")

    def reconnect_server(*, name: str | None = None, all_servers: bool = False, scope: str | None = None):
        assert all_servers is False
        cli_reconnected.append((name, scope))
        from iac_code.mcp.manager import MCPHealthDiagnostic
        from iac_code.mcp.types import MCPConfigScope, MCPConnectionState, MCPServerConfig, ScopedMCPServerConfig

        return [
            MCPHealthDiagnostic(
                scoped_config=ScopedMCPServerConfig(
                    config=MCPServerConfig.from_mapping("shared", {"command": "uvx"}),
                    scope=MCPConfigScope.USER,
                ),
                status=MCPConnectionState.CONNECTED.value,
                connection_state=MCPConnectionState.CONNECTED.value,
                auth_state="not-configured",
            )
        ]

    import iac_code.ui.dialogs.mcp_manager as dialog_module

    previous = dialog_module.mcp_cli.reconnect_mcp_server
    dialog_module.mcp_cli.reconnect_mcp_server = reconnect_server
    try:
        dialog = MCPManagerDialog(
            _context(repl=SimpleNamespace(_mcp_manager=FakeLiveMCPManager())),
            metadata_provider=lambda: {
                "servers": [
                    {
                        "serverName": "shared",
                        "scope": "local",
                        "transport": "stdio",
                        "state": "connected",
                        "authState": "not-configured",
                        "toolsCount": 0,
                        "resourcesCount": 0,
                        "promptsCount": 0,
                    },
                    {
                        "serverName": "shared",
                        "scope": "user",
                        "transport": "stdio",
                        "state": "connected",
                        "authState": "not-configured",
                        "toolsCount": 0,
                        "resourcesCount": 0,
                        "promptsCount": 0,
                    },
                ],
                "warnings": [],
            },
        )

        dialog.handle_key(KeyEvent("down", ""))
        dialog.handle_key(KeyEvent("enter", "\n"))
        dialog.handle_key(KeyEvent("enter", "\n"))
        _render_until(dialog, "Reconnected to shared.")
    finally:
        dialog_module.mcp_cli.reconnect_mcp_server = previous

    assert cli_reconnected == [("shared", "user")]


def test_mcp_manager_dialog_reconnect_fallback_uses_repl_original_cwd(monkeypatch, tmp_path: Path) -> None:
    project_a = tmp_path / "project-a"
    project_b = tmp_path / "project-b"
    project_a.mkdir()
    project_b.mkdir()
    monkeypatch.chdir(project_b)
    cli_reconnected: list[tuple[str | None, str | None, str | None]] = []

    class FakeLiveMCPManager:
        def connection(self, server_name: str) -> object:
            if server_name != "shared":
                raise KeyError(server_name)
            return SimpleNamespace(scoped_config=SimpleNamespace(scope=SimpleNamespace(value="local")))

        async def reconnect(self, server_name: str) -> None:
            raise AssertionError("shadowed persisted entry must not reconnect the same-name live server")

    def reconnect_server(
        *,
        name: str | None = None,
        all_servers: bool = False,
        scope: str | None = None,
        cwd: str | Path | None = None,
    ):
        assert all_servers is False
        cli_reconnected.append((name, scope, str(cwd) if cwd is not None else None))
        from iac_code.mcp.manager import MCPHealthDiagnostic
        from iac_code.mcp.types import MCPConfigScope, MCPConnectionState, MCPServerConfig, ScopedMCPServerConfig

        return [
            MCPHealthDiagnostic(
                scoped_config=ScopedMCPServerConfig(
                    config=MCPServerConfig.from_mapping("shared", {"command": "uvx"}),
                    scope=MCPConfigScope.USER,
                ),
                status=MCPConnectionState.CONNECTED.value,
                connection_state=MCPConnectionState.CONNECTED.value,
                auth_state="not-configured",
            )
        ]

    import iac_code.ui.dialogs.mcp_manager as dialog_module

    previous = dialog_module.mcp_cli.reconnect_mcp_server
    dialog_module.mcp_cli.reconnect_mcp_server = reconnect_server
    try:
        dialog = MCPManagerDialog(
            _context(repl=SimpleNamespace(_original_cwd=str(project_a), _mcp_manager=FakeLiveMCPManager())),
            metadata_provider=lambda: {
                "servers": [
                    {
                        "serverName": "shared",
                        "scope": "local",
                        "transport": "stdio",
                        "state": "connected",
                        "authState": "not-configured",
                        "toolsCount": 0,
                        "resourcesCount": 0,
                        "promptsCount": 0,
                    },
                    {
                        "serverName": "shared",
                        "scope": "user",
                        "transport": "stdio",
                        "state": "connected",
                        "authState": "not-configured",
                        "toolsCount": 0,
                        "resourcesCount": 0,
                        "promptsCount": 0,
                    },
                ],
                "warnings": [],
            },
        )

        dialog.handle_key(KeyEvent("down", ""))
        dialog.handle_key(KeyEvent("enter", "\n"))
        dialog.handle_key(KeyEvent("enter", "\n"))
        _render_until(dialog, "Reconnected to shared.")
    finally:
        dialog_module.mcp_cli.reconnect_mcp_server = previous

    assert cli_reconnected == [("shared", "user", str(project_a))]


def test_mcp_manager_dialog_relative_source_path_matches_live_manager_with_repl_cwd(
    monkeypatch,
    tmp_path: Path,
) -> None:
    process_cwd = tmp_path / "outside"
    child = tmp_path / "workspace" / "child"
    process_cwd.mkdir()
    child.mkdir(parents=True)
    child_project_file = child / ".mcp.json"
    child_project_file.write_text("{}", encoding="utf-8")
    monkeypatch.chdir(process_cwd)
    reconnected: list[str] = []

    class FakeLiveMCPManager:
        def connection(self, server_name: str) -> object:
            if server_name != "shared":
                raise KeyError(server_name)
            return SimpleNamespace(
                scoped_config=SimpleNamespace(
                    scope=SimpleNamespace(value="project"),
                    source_path=str(child_project_file),
                )
            )

        async def reconnect(self, server_name: str) -> None:
            reconnected.append(server_name)

        def connection_state(self, server_name: str) -> str:
            assert server_name == "shared"
            return "connected"

    import iac_code.ui.dialogs.mcp_manager as dialog_module

    previous = dialog_module.mcp_cli.reconnect_mcp_server
    dialog_module.mcp_cli.reconnect_mcp_server = Mock(
        side_effect=AssertionError("relative source path should match the live manager")
    )
    try:
        dialog = MCPManagerDialog(
            _context(repl=SimpleNamespace(_original_cwd=str(child), _mcp_manager=FakeLiveMCPManager())),
            metadata_provider=lambda: _single_server_metadata(
                {
                    "serverName": "shared",
                    "scope": "project",
                    "transport": "stdio",
                    "state": "connected",
                    "authState": "not-configured",
                    "toolsCount": 0,
                    "resourcesCount": 0,
                    "promptsCount": 0,
                    "sourcePath": ".mcp.json",
                    "_sourcePath": ".mcp.json",
                }
            ),
        )

        dialog.handle_key(KeyEvent("enter", "\n"))
        dialog.handle_key(KeyEvent("enter", "\n"))
        _render_until(dialog, "Reconnected to shared.")
    finally:
        dialog_module.mcp_cli.reconnect_mcp_server = previous

    assert reconnected == ["shared"]


def test_mcp_manager_dialog_distinguishes_same_name_project_servers_by_source_path() -> None:
    calls: list[tuple[str, str | None, str | None]] = []

    def disable(name: str, scope: str | None, *, source_path: str | None = None) -> None:
        calls.append((name, scope, source_path))

    dialog = MCPManagerDialog(
        _context(),
        metadata_provider=lambda: {
            "servers": [
                {
                    "serverName": "shared",
                    "scope": "project",
                    "transport": "stdio",
                    "state": "connected",
                    "authState": "not-configured",
                    "toolsCount": 0,
                    "resourcesCount": 0,
                    "promptsCount": 0,
                    "sourcePath": "/repo/.mcp.json",
                    "_sourcePath": "/repo/.mcp.json",
                    "command": "root-cmd",
                },
                {
                    "serverName": "shared",
                    "scope": "project",
                    "transport": "stdio",
                    "state": "connected",
                    "authState": "not-configured",
                    "toolsCount": 0,
                    "resourcesCount": 0,
                    "promptsCount": 0,
                    "sourcePath": "/repo/services/.mcp.json",
                    "_sourcePath": "/repo/services/.mcp.json",
                    "command": "child-cmd",
                },
            ],
            "warnings": [],
        },
        actions={"disable": disable},
    )

    dialog.handle_key(KeyEvent("down", ""))
    dialog.handle_key(KeyEvent("enter", "\n"))
    output = _render_text(dialog.render())
    assert "child-cmd" in output
    assert "/repo/services/.mcp.json" in output
    assert "root-cmd" not in output

    dialog.handle_key(KeyEvent("down", ""))
    dialog.handle_key(KeyEvent("enter", "\n"))

    assert calls == [("shared", "project", "/repo/services/.mcp.json")]


def test_mcp_manager_dialog_reconnect_reports_claude_result_messages() -> None:
    class FakeLiveMCPManager:
        def connection(self, server_name: str) -> object:
            if server_name != "stdio":
                raise KeyError(server_name)
            return object()

        async def reconnect(self, server_name: str) -> None:
            assert server_name == "stdio"

        def connection_state(self, server_name: str) -> str:
            assert server_name == "stdio"
            return "needs-auth"

    dialog = MCPManagerDialog(
        _context(repl=SimpleNamespace(_mcp_manager=FakeLiveMCPManager())),
        metadata_provider=lambda: _single_server_metadata(
            {
                "serverName": "stdio",
                "scope": "project",
                "transport": "stdio",
                "state": "connected",
                "authState": "not-configured",
                "toolsCount": 0,
                "resourcesCount": 0,
                "promptsCount": 0,
            }
        ),
    )

    dialog.handle_key(KeyEvent("enter", "\n"))
    dialog.handle_key(KeyEvent("enter", "\n"))
    _render_until(dialog, "stdio requires authentication. Use the 'Authenticate' option.")

    assert dialog.result_message == "stdio requires authentication. Use the 'Authenticate' option."
    assert dialog._done is True


def test_mcp_manager_dialog_reconnect_reports_claude_error_message() -> None:
    raw_error = "boom https://alice:super-secret@auth.example/callback PRIVATE_SECRET_MARKER \x1b[31mred " + (
        "x" * 1200
    )

    class FakeLiveMCPManager:
        def connection(self, server_name: str) -> object:
            if server_name != "stdio":
                raise KeyError(server_name)
            return object()

        async def reconnect(self, server_name: str) -> None:
            raise RuntimeError(raw_error)

    dialog = MCPManagerDialog(
        _context(repl=SimpleNamespace(_mcp_manager=FakeLiveMCPManager())),
        metadata_provider=lambda: _single_server_metadata(
            {
                "serverName": "stdio",
                "scope": "project",
                "transport": "stdio",
                "state": "connected",
                "authState": "not-configured",
                "toolsCount": 0,
                "resourcesCount": 0,
                "promptsCount": 0,
            }
        ),
    )

    dialog.handle_key(KeyEvent("enter", "\n"))
    dialog.handle_key(KeyEvent("enter", "\n"))
    _render_until(dialog, "Error reconnecting to stdio: boom")

    assert dialog.result_message is not None
    assert dialog.result_message.startswith("Error reconnecting to stdio: boom")
    assert "super-secret" not in dialog.result_message
    assert "PRIVATE_SECRET_MARKER" not in dialog.result_message
    assert "\x1b" not in dialog.result_message
    assert "[truncated]" in dialog.result_message
    assert len(dialog.result_message) <= len("Error reconnecting to stdio: ") + 1000
    assert dialog._done is True


def test_mcp_manager_dialog_reconnect_renders_busy_view_like_claude_panel() -> None:
    started = threading.Event()
    release = threading.Event()

    def reconnect(name: str, scope: str | None) -> str:
        assert (name, scope) == ("stdio", "project")
        started.set()
        release.wait(timeout=1)
        return "Reconnected to stdio."

    dialog = MCPManagerDialog(
        _context(),
        metadata_provider=lambda: _single_server_metadata(
            {
                "serverName": "stdio",
                "scope": "project",
                "transport": "stdio",
                "state": "connected",
                "authState": "not-configured",
                "toolsCount": 0,
                "resourcesCount": 0,
                "promptsCount": 0,
            }
        ),
        actions={"reconnect": reconnect},
    )

    dialog.handle_key(KeyEvent("enter", "\n"))
    runner = threading.Thread(target=lambda: dialog.handle_key(KeyEvent("enter", "\n")), daemon=True)
    runner.start()
    assert started.wait(timeout=0.5)
    try:
        output = _render_text(dialog.render())
    finally:
        release.set()
        runner.join(timeout=1)

    assert "Reconnecting to stdio" in output
    assert "Restarting MCP server process" in output
    assert "This may take a few moments." in output


def test_mcp_manager_dialog_completes_oauth_inside_dialog_and_refreshes_runtime() -> None:
    refreshed: list[bool] = []
    pending = _FakePendingOAuthFlow()
    repl = SimpleNamespace(_mcp_manager=_FakeLiveMCPManager("connected"))

    async def refresh_mcp_integrations() -> None:
        refreshed.append(True)

    dialog = MCPManagerDialog(
        _context(repl=repl),
        metadata_provider=lambda: _single_server_metadata(
            {
                "serverName": "remote",
                "scope": "user",
                "transport": "http",
                "state": "needs_auth",
                "authState": "needs-auth",
                "toolsCount": 0,
                "resourcesCount": 0,
                "promptsCount": 0,
            }
        ),
        actions={"auth": lambda name, scope, required_scopes=None, resource_metadata_url=None: pending},
    )
    repl.refresh_mcp_integrations = refresh_mcp_integrations

    dialog.handle_key(KeyEvent("enter", "\n"))
    dialog.handle_key(KeyEvent("enter", "\n"))
    auth_output = _render_text(dialog.render())
    assert "Authenticating remote" in auth_output
    assert "https://auth.example/authorize" in auth_output

    dialog.handle_key(KeyEvent("paste", "http://127.0.0.1/callback?code=ok"))
    dialog.handle_key(KeyEvent("enter", "\n"))
    output = _render_until(dialog, "Authentication successful. Connected to remote.")

    assert pending.manual_values == ["http://127.0.0.1/callback?code=ok"]
    assert refreshed == [True]
    assert "Authentication successful. Connected to remote." in output


def test_mcp_manager_dialog_waits_for_oauth_callback_when_browser_did_not_open_like_claude_panel() -> None:
    refreshed: list[bool] = []
    pending = _FakePendingOAuthFlow()
    repl = SimpleNamespace(_mcp_manager=_FakeLiveMCPManager("connected"))

    async def refresh_mcp_integrations() -> None:
        refreshed.append(True)

    dialog = MCPManagerDialog(
        _context(repl=repl),
        metadata_provider=lambda: _single_server_metadata(
            {
                "serverName": "remote",
                "scope": "user",
                "transport": "http",
                "state": "needs_auth",
                "authState": "needs-auth",
                "toolsCount": 0,
                "resourcesCount": 0,
                "promptsCount": 0,
            }
        ),
        actions={"auth": lambda name, scope, required_scopes=None, resource_metadata_url=None: pending},
    )
    repl.refresh_mcp_integrations = refresh_mcp_integrations

    dialog.handle_key(KeyEvent("enter", "\n"))
    dialog.handle_key(KeyEvent("enter", "\n"))
    auth_output = _render_text(dialog.render())
    assert "Authenticating remote" in auth_output
    assert "Browser opened: no" in auth_output

    pending.complete_from_browser()
    output = _render_until(dialog, "Authentication successful. Connected to remote.")

    assert refreshed == [True]
    assert "Authentication successful. Connected to remote." in output


def test_mcp_manager_dialog_oauth_reports_post_auth_reconnect_state_like_claude_panel() -> None:
    pending = _FakePendingOAuthFlow()
    repl = SimpleNamespace(_mcp_manager=_FakeLiveMCPManager("needs-auth"))

    async def refresh_mcp_integrations() -> None:
        repl._mcp_manager = _FakeLiveMCPManager("needs-auth")

    dialog = MCPManagerDialog(
        _context(repl=repl),
        metadata_provider=lambda: _single_server_metadata(
            {
                "serverName": "remote",
                "scope": "user",
                "transport": "http",
                "state": "needs_auth",
                "authState": "needs-auth",
                "toolsCount": 0,
                "resourcesCount": 0,
                "promptsCount": 0,
            }
        ),
        actions={"auth": lambda name, scope, required_scopes=None, resource_metadata_url=None: pending},
    )
    repl.refresh_mcp_integrations = refresh_mcp_integrations

    dialog.handle_key(KeyEvent("enter", "\n"))
    dialog.handle_key(KeyEvent("enter", "\n"))
    dialog.handle_key(KeyEvent("paste", "http://127.0.0.1/callback?code=ok"))
    dialog.handle_key(KeyEvent("enter", "\n"))
    output = _render_until(
        dialog,
        "Authentication successful, but server still requires authentication. "
        "You may need to manually restart iac-code.",
    )

    assert pending.manual_values == ["http://127.0.0.1/callback?code=ok"]
    assert (
        "Authentication successful, but server still requires authentication. "
        "You may need to manually restart iac-code."
    ) in output


def test_mcp_manager_dialog_reauth_reports_reconnected_like_claude_panel() -> None:
    refreshed: list[bool] = []
    pending = _FakePendingOAuthFlow()
    repl = SimpleNamespace(_mcp_manager=_FakeLiveMCPManager("connected"))

    async def refresh_mcp_integrations() -> None:
        refreshed.append(True)

    dialog = MCPManagerDialog(
        _context(repl=repl),
        metadata_provider=lambda: _single_server_metadata(
            {
                "serverName": "remote",
                "scope": "user",
                "transport": "http",
                "state": "connected",
                "authState": "configured",
                "toolsCount": 1,
                "resourcesCount": 0,
                "promptsCount": 0,
                "tools": [{"publicName": "mcp__remote__tool", "originalToolName": "tool"}],
            }
        ),
        actions={"reauth": lambda name, scope, required_scopes=None, resource_metadata_url=None: pending},
    )
    repl.refresh_mcp_integrations = refresh_mcp_integrations

    dialog.handle_key(KeyEvent("enter", "\n"))
    dialog.handle_key(KeyEvent("down", ""))
    dialog.handle_key(KeyEvent("enter", "\n"))
    dialog.handle_key(KeyEvent("paste", "http://127.0.0.1/callback?code=ok"))
    dialog.handle_key(KeyEvent("enter", "\n"))
    output = _render_until(dialog, "Authentication successful. Reconnected to remote.")

    assert pending.manual_values == ["http://127.0.0.1/callback?code=ok"]
    assert refreshed == [True]
    assert "Authentication successful. Reconnected to remote." in output


def test_mcp_manager_dialog_clear_auth_reports_claude_message() -> None:
    events: list[str] = []

    async def close_mcp_manager() -> None:
        events.append("close")

    async def refresh_mcp_integrations() -> None:
        events.append("refresh")

    repl = SimpleNamespace(_close_mcp_manager=close_mcp_manager, refresh_mcp_integrations=refresh_mcp_integrations)
    dialog = MCPManagerDialog(
        _context(repl=repl),
        metadata_provider=lambda: _single_server_metadata(
            {
                "serverName": "remote",
                "scope": "user",
                "transport": "http",
                "state": "connected",
                "authState": "configured",
                "toolsCount": 0,
                "resourcesCount": 0,
                "promptsCount": 0,
            }
        ),
        actions={
            "clear-auth": lambda name, scope: (
                events.append("clear-auth")
                or "Reset stored MCP auth state for 'remote'.\n"
                "Warning: OAuth token revocation failed for MCP server 'remote': [REDACTED]"
            )
        },
    )

    dialog.handle_key(KeyEvent("enter", "\n"))
    dialog.handle_key(KeyEvent("down", ""))
    dialog.handle_key(KeyEvent("enter", "\n"))

    assert dialog.result_message == (
        "Reset stored MCP auth state for 'remote'.\n"
        "Warning: OAuth token revocation failed for MCP server 'remote': [REDACTED]"
    )
    assert events == ["close", "clear-auth", "refresh"]
    assert dialog._done is True


def test_mcp_manager_dialog_clear_auth_closes_live_runtime_before_clearing_token(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("IAC_CODE_MCP_DISABLE_KEYRING", "1")
    settings_path = tmp_path / "config" / "settings.yml"
    settings_path.parent.mkdir(parents=True)
    settings_path.write_text(
        """
mcpServers:
  remote:
    type: http
    url: "https://example.com/mcp"
""".lstrip(),
        encoding="utf-8",
    )
    config = MCPServerConfig.from_mapping("remote", {"type": "http", "url": "https://example.com/mcp"})
    storage = MCPSecretStorage()
    access_key = oauth_storage_key(config, "access_token", scope=MCPConfigScope.USER)
    storage.set_secret(access_key, "live-token")
    events: list[str] = []

    async def close_mcp_manager() -> None:
        assert storage.get_secret(access_key) == "live-token"
        events.append("close")

    async def refresh_mcp_integrations() -> None:
        assert storage.get_secret(access_key) is None
        events.append("refresh")

    repl = SimpleNamespace(
        _original_cwd=str(tmp_path),
        _close_mcp_manager=close_mcp_manager,
        refresh_mcp_integrations=refresh_mcp_integrations,
    )
    dialog = MCPManagerDialog(
        _context(repl=repl),
        metadata_provider=lambda: _single_server_metadata(
            {
                "serverName": "remote",
                "scope": "user",
                "sourcePath": str(settings_path),
                "_sourcePath": str(settings_path),
                "transport": "http",
                "state": "connected",
                "authState": "configured",
                "toolsCount": 0,
                "resourcesCount": 0,
                "promptsCount": 0,
            }
        ),
    )

    dialog.handle_key(KeyEvent("enter", "\n"))
    dialog.handle_key(KeyEvent("down", ""))
    dialog.handle_key(KeyEvent("enter", "\n"))

    assert events == ["close", "refresh"]
    assert storage.get_secret(access_key) is None
    assert str(dialog.result_message).startswith("Reset stored MCP auth state for 'remote'.")
    assert dialog._done is True


def test_mcp_manager_dialog_clear_auth_refreshes_runtime_after_action_failure() -> None:
    events: list[str] = []

    async def close_mcp_manager() -> None:
        events.append("close")

    async def refresh_mcp_integrations() -> None:
        events.append("refresh")

    def clear_auth(name: str, scope: str | None) -> str:
        events.append("clear-auth")
        raise RuntimeError("reset failed")

    repl = SimpleNamespace(_close_mcp_manager=close_mcp_manager, refresh_mcp_integrations=refresh_mcp_integrations)
    dialog = MCPManagerDialog(
        _context(repl=repl),
        metadata_provider=lambda: _single_server_metadata(
            {
                "serverName": "remote",
                "scope": "user",
                "transport": "http",
                "state": "connected",
                "authState": "configured",
                "toolsCount": 0,
                "resourcesCount": 0,
                "promptsCount": 0,
            }
        ),
        actions={"clear-auth": clear_auth},
    )

    dialog.handle_key(KeyEvent("enter", "\n"))
    dialog.handle_key(KeyEvent("down", ""))
    dialog.handle_key(KeyEvent("enter", "\n"))

    assert events == ["close", "clear-auth", "refresh"]
    assert dialog.result_message == "reset failed"
    assert dialog._done is False


def test_mcp_manager_dialog_oauth_c_copies_url_and_edits_callback_input() -> None:
    copied: list[str] = []
    pending = _FakePendingOAuthFlow()
    dialog = MCPManagerDialog(
        _context(),
        metadata_provider=lambda: _single_server_metadata(
            {
                "serverName": "remote",
                "scope": "user",
                "transport": "http",
                "state": "needs_auth",
                "authState": "needs-auth",
                "toolsCount": 0,
                "resourcesCount": 0,
                "promptsCount": 0,
            }
        ),
        actions={"auth": lambda name, scope, required_scopes=None, resource_metadata_url=None: pending},
        clipboard_writer=lambda value: copied.append(value) or True,
    )

    dialog.handle_key(KeyEvent("enter", "\n"))
    dialog.handle_key(KeyEvent("enter", "\n"))
    dialog.handle_key(KeyEvent("c", "c"))

    auth_output = _render_text(dialog.render())
    assert copied == ["https://auth.example/authorize"]
    assert "Copied!" in auth_output
    assert "c copy URL" in auth_output

    dialog.handle_key(KeyEvent("x", "x"))
    assert dialog._auth_flow is not None
    assert dialog._auth_flow.callback_input == "x"
    dialog._auth_flow.callback_input = ""
    dialog._auth_flow.callback_cursor = 0

    dialog.handle_key(KeyEvent("paste", "http://127.0.0.1/callback?code=ok"))
    dialog.handle_key(KeyEvent("left", ""))
    dialog.handle_key(KeyEvent("backspace", ""))
    dialog.handle_key(KeyEvent("o", "o"))
    dialog.handle_key(KeyEvent("enter", "\n"))
    _render_until(dialog, "Authentication successful. Connected to remote.")

    assert pending.manual_values == ["http://127.0.0.1/callback?code=ok"]


def test_mcp_manager_dialog_invalid_manual_oauth_callback_keeps_auth_flow_like_claude_panel() -> None:
    pending = _InvalidManualPendingOAuthFlow()
    repl = SimpleNamespace(_mcp_manager=_FakeLiveMCPManager("connected"))
    dialog = MCPManagerDialog(
        _context(repl=repl),
        metadata_provider=lambda: _single_server_metadata(
            {
                "serverName": "remote",
                "scope": "user",
                "transport": "http",
                "state": "needs_auth",
                "authState": "needs-auth",
                "toolsCount": 0,
                "resourcesCount": 0,
                "promptsCount": 0,
            }
        ),
        actions={"auth": lambda name, scope, required_scopes=None, resource_metadata_url=None: pending},
    )

    dialog.handle_key(KeyEvent("enter", "\n"))
    dialog.handle_key(KeyEvent("enter", "\n"))
    dialog.handle_key(KeyEvent("paste", "https://auth.example/authorize"))
    dialog.handle_key(KeyEvent("enter", "\n"))
    output = _render_until(dialog, "OAuth callback did not include a code.")

    assert "Authenticating remote" in output
    assert "Actions" not in output
    assert pending.closed is False
    assert dialog._done is False

    pending.complete_from_browser()
    output = _render_until(dialog, "Authentication successful. Connected to remote.")

    assert "Authentication successful. Connected to remote." in output


def test_mcp_manager_dialog_oauth_escape_cancels_without_error_like_claude_panel() -> None:
    pending = _FakePendingOAuthFlow()
    dialog = MCPManagerDialog(
        _context(),
        metadata_provider=lambda: _single_server_metadata(
            {
                "serverName": "remote",
                "scope": "user",
                "transport": "http",
                "state": "needs_auth",
                "authState": "needs-auth",
                "toolsCount": 0,
                "resourcesCount": 0,
                "promptsCount": 0,
            }
        ),
        actions={"auth": lambda name, scope, required_scopes=None, resource_metadata_url=None: pending},
    )

    dialog.handle_key(KeyEvent("enter", "\n"))
    dialog.handle_key(KeyEvent("enter", "\n"))
    dialog.handle_key(KeyEvent("escape", ""))
    output = _render_text(dialog.render())

    assert pending.closed is True
    assert "OAuth authorization was cancelled." not in output
    assert dialog.result_message is None
    assert "Actions" in output


def test_mcp_manager_dialog_async_oauth_error_keeps_menu_with_error_prefix_like_claude_panel() -> None:
    pending = _FailingPendingOAuthFlow(RuntimeError("bad callback"))
    dialog = MCPManagerDialog(
        _context(),
        metadata_provider=lambda: _single_server_metadata(
            {
                "serverName": "remote",
                "scope": "user",
                "transport": "http",
                "state": "needs_auth",
                "authState": "needs-auth",
                "toolsCount": 0,
                "resourcesCount": 0,
                "promptsCount": 0,
            }
        ),
        actions={"auth": lambda name, scope, required_scopes=None, resource_metadata_url=None: pending},
    )

    dialog.handle_key(KeyEvent("enter", "\n"))
    dialog.handle_key(KeyEvent("enter", "\n"))
    dialog.handle_key(KeyEvent("paste", "http://127.0.0.1/callback?error=access_denied"))
    dialog.handle_key(KeyEvent("enter", "\n"))
    output = _render_until(dialog, "Error: bad callback")

    assert "Error: bad callback" in output
    assert "Actions" in output
    assert dialog._done is False


def test_mcp_manager_dialog_async_oauth_error_sanitizes_public_message() -> None:
    raw_error = (
        "token exchange failed at https://alice:super-secret@auth.example/callback "
        "PRIVATE_SECRET_MARKER \x1b[2J " + ("x" * 1200)
    )
    pending = _FailingPendingOAuthFlow(RuntimeError(raw_error))
    dialog = MCPManagerDialog(
        _context(),
        metadata_provider=lambda: _single_server_metadata(
            {
                "serverName": "remote",
                "scope": "user",
                "transport": "http",
                "state": "needs_auth",
                "authState": "needs-auth",
                "toolsCount": 0,
                "resourcesCount": 0,
                "promptsCount": 0,
            }
        ),
        actions={"auth": lambda name, scope, required_scopes=None, resource_metadata_url=None: pending},
    )

    dialog.handle_key(KeyEvent("enter", "\n"))
    dialog.handle_key(KeyEvent("enter", "\n"))
    dialog.handle_key(KeyEvent("paste", "http://127.0.0.1/callback?error=access_denied"))
    dialog.handle_key(KeyEvent("enter", "\n"))
    output = _render_until(dialog, "Error:")

    assert "Actions" in output
    assert dialog.result_message is not None
    assert "super-secret" not in dialog.result_message
    assert "PRIVATE_SECRET_MARKER" not in dialog.result_message
    assert "\x1b" not in dialog.result_message
    assert "[truncated]" in dialog.result_message
    assert len(dialog.result_message) <= len("Error: ") + 1000
    assert dialog._done is False


def test_mcp_manager_dialog_toggle_returns_to_server_list_like_claude_panel() -> None:
    calls: list[tuple[str, str | None]] = []
    state = "connected"

    def disable(name: str, scope: str | None) -> str:
        nonlocal state
        calls.append((name, scope))
        state = "disabled"
        return "Disabled MCP server 'stdio'."

    dialog = MCPManagerDialog(
        _context(),
        metadata_provider=lambda: _single_server_metadata(
            {
                "serverName": "stdio",
                "scope": "project",
                "transport": "stdio",
                "state": state,
                "authState": "not-configured",
                "toolsCount": 1,
                "resourcesCount": 0,
                "promptsCount": 0,
                "tools": [{"publicName": "mcp__stdio__tool", "originalToolName": "tool"}],
            }
        ),
        actions={"disable": disable},
    )

    dialog.handle_key(KeyEvent("enter", "\n"))
    dialog.handle_key(KeyEvent("down", ""))
    dialog.handle_key(KeyEvent("down", ""))
    dialog.handle_key(KeyEvent("enter", "\n"))
    output = _render_text(dialog.render())

    assert calls == [("stdio", "project")]
    assert "Project MCPs" in output
    assert "stdio · ○ disabled" in output
    assert "Disabled MCP server 'stdio'." not in output


def test_mcp_manager_dialog_remove_action_finishes_with_remove_message() -> None:
    calls: list[tuple[str, str | None, str | None]] = []
    events: list[str] = []

    async def close_mcp_manager() -> None:
        events.append("close")

    async def refresh_mcp_integrations() -> None:
        events.append("refresh")

    repl = SimpleNamespace(_close_mcp_manager=close_mcp_manager, refresh_mcp_integrations=refresh_mcp_integrations)
    dialog = MCPManagerDialog(
        _context(repl=repl),
        metadata_provider=lambda: _single_server_metadata(
            {
                "serverName": "remote",
                "scope": "user",
                "sourcePath": "/home/user/.iac-code/settings.yml",
                "_sourcePath": "/home/user/.iac-code/settings.yml",
                "transport": "http",
                "state": "connected",
                "authState": "configured",
                "toolsCount": 0,
                "resourcesCount": 0,
                "promptsCount": 0,
            }
        ),
        actions={
            "remove": lambda name, scope, source_path=None: (
                calls.append((name, scope, source_path))
                or events.append("remove")
                or "Removed MCP server 'remote' from /home/user/.iac-code/settings.yml."
            )
        },
    )

    dialog.handle_key(KeyEvent("enter", "\n"))
    output = _render_text(dialog.render())
    assert "Remove" in output

    for _ in range(4):
        dialog.handle_key(KeyEvent("down", ""))
    dialog.handle_key(KeyEvent("enter", "\n"))

    assert calls == [("remote", "user", "/home/user/.iac-code/settings.yml")]
    assert events == ["close", "remove", "refresh"]
    assert dialog.result_message == "Removed MCP server 'remote' from /home/user/.iac-code/settings.yml."
    assert dialog._done is True


def test_mcp_manager_dialog_remove_closes_live_runtime_before_clearing_token(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("IAC_CODE_MCP_DISABLE_KEYRING", "1")
    settings_path = tmp_path / "config" / "settings.yml"
    settings_path.parent.mkdir(parents=True)
    settings_path.write_text(
        """
mcpServers:
  remote:
    type: http
    url: "https://example.com/mcp"
""".lstrip(),
        encoding="utf-8",
    )
    config = MCPServerConfig.from_mapping("remote", {"type": "http", "url": "https://example.com/mcp"})
    storage = MCPSecretStorage()
    access_key = oauth_storage_key(config, "access_token", scope=MCPConfigScope.USER)
    storage.set_secret(access_key, "live-token")
    events: list[str] = []

    async def close_mcp_manager() -> None:
        assert storage.get_secret(access_key) == "live-token"
        events.append("close")

    async def refresh_mcp_integrations() -> None:
        assert storage.get_secret(access_key) is None
        events.append("refresh")

    repl = SimpleNamespace(
        _original_cwd=str(tmp_path),
        _close_mcp_manager=close_mcp_manager,
        refresh_mcp_integrations=refresh_mcp_integrations,
    )
    dialog = MCPManagerDialog(
        _context(repl=repl),
        metadata_provider=lambda: _single_server_metadata(
            {
                "serverName": "remote",
                "scope": "user",
                "sourcePath": str(settings_path),
                "_sourcePath": str(settings_path),
                "transport": "http",
                "state": "connected",
                "authState": "configured",
                "toolsCount": 0,
                "resourcesCount": 0,
                "promptsCount": 0,
            }
        ),
    )

    dialog.handle_key(KeyEvent("enter", "\n"))
    for _ in range(4):
        dialog.handle_key(KeyEvent("down", ""))
    dialog.handle_key(KeyEvent("enter", "\n"))

    assert events == ["close", "refresh"]
    assert storage.get_secret(access_key) is None
    assert "remote" not in settings_path.read_text(encoding="utf-8")
    assert dialog._done is True


def test_mcp_manager_dialog_remove_refreshes_runtime_after_action_failure() -> None:
    events: list[str] = []

    async def close_mcp_manager() -> None:
        events.append("close")

    async def refresh_mcp_integrations() -> None:
        events.append("refresh")

    def remove(name: str, scope: str | None, source_path: str | None = None) -> str:
        events.append("remove")
        raise RuntimeError("remove failed")

    repl = SimpleNamespace(_close_mcp_manager=close_mcp_manager, refresh_mcp_integrations=refresh_mcp_integrations)
    dialog = MCPManagerDialog(
        _context(repl=repl),
        metadata_provider=lambda: _single_server_metadata(
            {
                "serverName": "remote",
                "scope": "user",
                "sourcePath": "/home/user/.iac-code/settings.yml",
                "_sourcePath": "/home/user/.iac-code/settings.yml",
                "transport": "http",
                "state": "connected",
                "authState": "configured",
                "toolsCount": 0,
                "resourcesCount": 0,
                "promptsCount": 0,
            }
        ),
        actions={"remove": remove},
    )

    dialog.handle_key(KeyEvent("enter", "\n"))
    for _ in range(4):
        dialog.handle_key(KeyEvent("down", ""))
    dialog.handle_key(KeyEvent("enter", "\n"))

    assert events == ["close", "remove", "refresh"]
    assert dialog.result_message == "remove failed"
    assert dialog._done is False


def test_mcp_manager_dialog_default_remove_action_deletes_persisted_server(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    settings_path = tmp_path / "config" / "settings.yml"
    settings_path.parent.mkdir(parents=True)
    settings_path.write_text(
        """
mcpServers:
  remote:
    type: http
    url: "https://example.com/mcp"
""".lstrip(),
        encoding="utf-8",
    )
    refreshed: list[bool] = []

    async def refresh_mcp_integrations() -> None:
        refreshed.append(True)

    repl = SimpleNamespace(_original_cwd=str(tmp_path), refresh_mcp_integrations=refresh_mcp_integrations)
    dialog = MCPManagerDialog(
        _context(repl=repl),
        metadata_provider=lambda: _single_server_metadata(
            {
                "serverName": "remote",
                "scope": "user",
                "sourcePath": str(settings_path),
                "_sourcePath": str(settings_path),
                "transport": "http",
                "state": "connected",
                "authState": "not-configured",
                "toolsCount": 0,
                "resourcesCount": 0,
                "promptsCount": 0,
            }
        ),
    )

    dialog.handle_key(KeyEvent("enter", "\n"))
    for _ in range(3):
        dialog.handle_key(KeyEvent("down", ""))
    dialog.handle_key(KeyEvent("enter", "\n"))

    assert "Removed MCP server 'remote'" in str(dialog.result_message)
    assert refreshed == [True]
    assert "remote" not in settings_path.read_text(encoding="utf-8")
    assert dialog._done is True


def test_mcp_manager_dialog_default_remove_action_uses_repl_original_cwd(
    monkeypatch,
    tmp_path: Path,
) -> None:
    project_a = tmp_path / "project-a"
    project_b = tmp_path / "project-b"
    project_a.mkdir()
    project_b.mkdir()
    monkeypatch.chdir(project_b)
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    local_path = project_a / ".iac-code" / "settings.local.yml"
    local_path.parent.mkdir(parents=True)
    local_path.write_text(
        """
mcpServers:
  remote:
    type: http
    url: "https://project-a.example/mcp"
""".lstrip(),
        encoding="utf-8",
    )
    repl = SimpleNamespace(_original_cwd=str(project_a))
    dialog = MCPManagerDialog(
        _context(repl=repl),
        metadata_provider=lambda: _single_server_metadata(
            {
                "serverName": "remote",
                "scope": "local",
                "sourcePath": str(local_path),
                "_sourcePath": str(local_path),
                "transport": "http",
                "state": "connected",
                "authState": "not-configured",
                "toolsCount": 0,
                "resourcesCount": 0,
                "promptsCount": 0,
            }
        ),
    )

    dialog.handle_key(KeyEvent("enter", "\n"))
    for _ in range(3):
        dialog.handle_key(KeyEvent("down", ""))
    dialog.handle_key(KeyEvent("enter", "\n"))

    assert "remote" not in local_path.read_text(encoding="utf-8")
    assert dialog._done is True


def test_mcp_manager_dialog_toggle_failure_completes_with_claude_error_message() -> None:
    def disable(name: str, scope: str | None) -> None:
        assert (name, scope) == ("stdio", "project")
        raise RuntimeError("boom")

    dialog = MCPManagerDialog(
        _context(),
        metadata_provider=lambda: _single_server_metadata(
            {
                "serverName": "stdio",
                "scope": "project",
                "transport": "stdio",
                "state": "connected",
                "authState": "not-configured",
                "toolsCount": 0,
                "resourcesCount": 0,
                "promptsCount": 0,
            }
        ),
        actions={"disable": disable},
    )

    dialog.handle_key(KeyEvent("enter", "\n"))
    dialog.handle_key(KeyEvent("down", ""))
    dialog.handle_key(KeyEvent("enter", "\n"))

    assert dialog.result_message == "Failed to disable MCP server 'stdio': boom"
    assert dialog._done is True


def test_mcp_manager_dialog_empty_state_points_to_cli_help() -> None:
    dialog = MCPManagerDialog(_context(), metadata_provider=lambda: {"servers": [], "warnings": []})

    output = _render_text(dialog.render())

    assert "No MCP servers configured." in output
    assert "iac-code mcp --help" in output


def test_mcp_manager_dialog_empty_message_completes_like_claude_when_no_servers() -> None:
    dialog = MCPManagerDialog(_context(), metadata_provider=lambda: {"servers": [], "warnings": []})

    assert dialog.empty_message_if_no_servers() == "No MCP servers configured. Run `iac-code mcp --help` to learn more."


def test_mcp_manager_dialog_empty_message_keeps_diagnostics_visible() -> None:
    dialog = MCPManagerDialog(
        _context(),
        metadata_provider=lambda: {
            "servers": [],
            "warnings": [{"severity": "warning", "message": "bad config"}],
        },
    )

    assert dialog.empty_message_if_no_servers() is None


def test_mcp_manager_dialog_default_metadata_includes_missing_env_server_row(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.delenv("YUQUE_TOKEN", raising=False)
    settings_path = tmp_path / "config" / "settings.yml"
    settings_path.parent.mkdir(parents=True)
    settings_path.write_text(
        """
mcpServers:
  remote:
    type: http
    url: "https://${YUQUE_TOKEN}.example.test/mcp"
""".lstrip(),
        encoding="utf-8",
    )
    calls: list[tuple[str, str | None]] = []
    repl = SimpleNamespace(_original_cwd=str(tmp_path))
    dialog = MCPManagerDialog(
        _context(repl=repl),
        actions={"disable": lambda name, scope: calls.append((name, scope)) or "Disabled MCP server 'remote'."},
    )

    output = _render_text(dialog.render())

    assert "MCP Config Diagnostics" in output
    assert "remote · ✖ missing-env" in output
    assert "No MCP servers configured." not in output

    dialog.handle_key(KeyEvent("enter", "\n"))
    detail_output = _render_text(dialog.render())

    assert "Remote MCP Server" in detail_output
    assert "Status: ✖ missing-env" in detail_output
    assert "Failure: Environment variable 'YUQUE_TOKEN' is not set" in detail_output
    assert "Disable" in detail_output
    assert "Authenticate" not in detail_output
    assert "Reconnect" not in detail_output

    dialog.handle_key(KeyEvent("enter", "\n"))

    assert calls == [("remote", "user")]


def test_mcp_manager_dialog_default_metadata_redacts_url_userinfo(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    settings_path = tmp_path / "config" / "settings.yml"
    settings_path.parent.mkdir(parents=True)
    settings_path.write_text(
        """
mcpServers:
  remote:
    type: http
    url: "https://user:password@example.com/mcp?access_token=super-url-token&space=public"
""".lstrip(),
        encoding="utf-8",
    )
    repl = SimpleNamespace(_original_cwd=str(tmp_path))
    dialog = MCPManagerDialog(_context(repl=repl))

    dialog.handle_key(KeyEvent("enter", "\n"))
    output = _render_text(dialog.render())

    assert "URL: https://[REDACTED]@example.com/mcp?access_token=[REDACTED]" in output
    assert "super-url-token" not in output
    assert "user:password" not in output


def test_mcp_manager_dialog_default_disable_action_handles_invalid_config_row(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    settings_path = tmp_path / "config" / "settings.yml"
    settings_path.parent.mkdir(parents=True)
    settings_path.write_text(
        """
mcpServers:
  bad:
    type: http
""".lstrip(),
        encoding="utf-8",
    )
    repl = SimpleNamespace(_original_cwd=str(tmp_path))
    dialog = MCPManagerDialog(_context(repl=repl))

    assert "bad · ✖ invalid-config" in _render_text(dialog.render())

    dialog.handle_key(KeyEvent("enter", "\n"))
    detail_output = _render_text(dialog.render())

    assert "Bad MCP Server" in detail_output
    assert "Status: ✖ invalid-config" in detail_output
    assert "Disable" in detail_output

    dialog.handle_key(KeyEvent("enter", "\n"))
    output = _render_text(dialog.render())

    assert "bad · ○ disabled" in output
    assert "invalid-config" not in output
    assert dialog._done is False


def test_mcp_manager_dialog_default_disable_action_handles_scalar_invalid_config_row(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    settings_path = tmp_path / "config" / "settings.yml"
    settings_path.parent.mkdir(parents=True)
    settings_path.write_text(
        """
mcpServers:
  bad: not-an-object
""".lstrip(),
        encoding="utf-8",
    )
    repl = SimpleNamespace(_original_cwd=str(tmp_path))
    dialog = MCPManagerDialog(_context(repl=repl))

    assert "bad · ✖ invalid-config" in _render_text(dialog.render())

    dialog.handle_key(KeyEvent("enter", "\n"))
    dialog.handle_key(KeyEvent("enter", "\n"))
    output = _render_text(dialog.render())

    assert "bad · ○ disabled" in output
    assert "invalid-config" not in output


def _context(repl: Any | None = None) -> Any:
    return SimpleNamespace(console=Console(record=True), repl=repl or SimpleNamespace())


def _render_text(renderable: Any) -> str:
    console = Console(record=True, width=140)
    console.print(renderable)
    return console.export_text()


def _render_until(dialog: MCPManagerDialog, expected: str) -> str:
    output = ""
    for _ in range(20):
        output = _render_text(dialog.render())
        if expected in output:
            return output
        time.sleep(0.01)
    return output


def _sample_metadata() -> dict[str, Any]:
    return {
        "servers": [
            {
                "serverName": "ros",
                "scope": "project",
                "transport": "stdio",
                "state": "connected",
                "authState": "not-configured",
                "toolsCount": 2,
                "resourcesCount": 1,
                "promptsCount": 1,
                "sourcePath": "/repo/.mcp.json",
                "command": "npx",
                "args": ["-y", "@iac-code/ros-mcp"],
                "tools": [
                    {
                        "publicName": "mcp__ros__generate_template",
                        "originalServerName": "ros",
                        "originalToolName": "generate-template",
                        "description": "Generate ROS template",
                        "inputSchema": {
                            "type": "object",
                            "properties": {"region": {"type": "string", "description": "Region ID"}},
                            "required": ["region"],
                        },
                        "annotations": {"readOnlyHint": True},
                    }
                ],
            },
            {
                "serverName": "remote",
                "scope": "user",
                "transport": "http",
                "state": "needs_auth",
                "authState": "needs-auth",
                "toolsCount": 0,
                "resourcesCount": 0,
                "promptsCount": 0,
                "url": "https://mcp.example.test/mcp",
                "failureReason": "Required scopes: doc:read",
            },
        ],
        "warnings": [],
    }


def _status_metadata() -> dict[str, Any]:
    def item(name: str, state: str, *, retry: int | None = None) -> dict[str, Any]:
        server = {
            "serverName": name,
            "scope": "project",
            "transport": "stdio",
            "state": state,
            "authState": "not-configured",
            "toolsCount": 0,
            "resourcesCount": 0,
            "promptsCount": 0,
        }
        if retry is not None:
            server["retryCount"] = retry
            server["maxReconnectAttempts"] = 2
        return server

    return {
        "servers": [
            item("connected", "connected"),
            item("pending", "pending"),
            item("retrying", "pending", retry=1),
            item("auth", "needs_auth"),
            item("failed", "failed"),
            item("disabled", "disabled"),
        ],
        "warnings": [],
    }


def _single_server_metadata(server: dict[str, Any]) -> dict[str, Any]:
    return {"servers": [server], "warnings": []}


def _unsorted_metadata() -> dict[str, Any]:
    def item(name: str, scope: str) -> dict[str, Any]:
        return {
            "serverName": name,
            "scope": scope,
            "transport": "stdio",
            "state": "connected",
            "authState": "not-configured",
            "toolsCount": 0,
            "resourcesCount": 0,
            "promptsCount": 0,
        }

    return {
        "servers": [
            item("zed", "user"),
            item("beta", "project"),
            item("builtin", "dynamic"),
            item("local", "local"),
            item("ephemeral", "session"),
            item("alpha", "project"),
        ],
        "warnings": [],
    }


class _FakeLiveMCPManager:
    def __init__(self, state: str) -> None:
        self._state = state

    def connection(self, server_name: str) -> object:
        if server_name != "remote":
            raise KeyError(server_name)
        return object()

    def connection_state(self, server_name: str) -> str:
        if server_name != "remote":
            raise KeyError(server_name)
        return self._state


class _FakePendingOAuthFlow:
    authorization_url = "https://auth.example/authorize"
    browser_opened = False

    def __init__(self) -> None:
        self.manual_values: list[str] = []
        self.closed = False
        self._done = threading.Event()
        self._error: BaseException | None = None

    def wait(self) -> object:
        self._done.wait(timeout=1)
        if self._error is not None:
            raise self._error
        return object()

    def submit_manually(self, value: str) -> None:
        self.manual_values.append(value)
        self._done.set()

    def complete_manually(self, value: str) -> object:
        self.manual_values.append(value)
        self._done.set()
        return object()

    def complete_from_browser(self) -> None:
        self._done.set()

    def close(self) -> None:
        self.closed = True
        self._error = RuntimeError("OAuth flow closed.")
        self._done.set()


class _FailingPendingOAuthFlow(_FakePendingOAuthFlow):
    def __init__(self, error: BaseException) -> None:
        super().__init__()
        self._error = error

    def complete_manually(self, value: str) -> object:
        self.manual_values.append(value)
        raise self._error


class _InvalidManualPendingOAuthFlow(_FakePendingOAuthFlow):
    def submit_manually(self, value: str) -> None:
        self.manual_values.append(value)
        raise RuntimeError("OAuth callback did not include a code.")

    def complete_manually(self, value: str) -> object:
        self.manual_values.append(value)
        raise RuntimeError("OAuth callback did not include a code.")
