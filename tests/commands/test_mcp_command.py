from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import pytest
import yaml
from rich.console import Console

import iac_code.commands.mcp as mcp_module
from iac_code.commands import create_default_registry
from iac_code.commands.mcp import mcp_command
from iac_code.commands.registry import LocalCommand
from iac_code.mcp.config import load_all_persisted_mcp_configs


@pytest.mark.asyncio
async def test_default_registry_includes_mcp_command() -> None:
    command = create_default_registry().get("mcp")

    assert isinstance(command, LocalCommand)
    assert command.handler is mcp_command
    assert command.arg_hint == "[enable|disable|reconnect [server-name] [--scope scope] [--source-path path]]"


@pytest.mark.asyncio
async def test_mcp_command_requires_context() -> None:
    result = await mcp_command(context=None, args=[])

    assert result == "MCP command requires a context."


@pytest.mark.asyncio
async def test_mcp_command_opens_interactive_manager_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    opened: list[Any] = []

    class FakeMCPManagerDialog:
        def __init__(self, context: Any) -> None:
            opened.append(context)

        def run(self) -> str:
            return "MCP dialog dismissed"

    monkeypatch.setattr("iac_code.commands.mcp._mcp_manager_dialog_class", lambda: FakeMCPManagerDialog)
    context = SimpleNamespace(console=Console(record=True), repl=Mock())

    result = await mcp_command(context=context, args=[])

    assert result == "MCP dialog dismissed"
    assert opened == [context]


@pytest.mark.asyncio
async def test_mcp_command_completes_with_no_servers_message_like_claude(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeMCPManagerDialog:
        def __init__(self, context: Any) -> None:
            self.context = context

        def empty_message_if_no_servers(self) -> str:
            return "No MCP servers configured. Run `iac-code mcp --help` to learn more."

        def run(self) -> str:
            raise AssertionError("empty MCP state should complete without opening the interactive panel")

    monkeypatch.setattr("iac_code.commands.mcp._mcp_manager_dialog_class", lambda: FakeMCPManagerDialog)
    context = SimpleNamespace(console=Console(record=True), repl=Mock())

    result = await mcp_command(context=context, args=[])

    assert result == "No MCP servers configured. Run `iac-code mcp --help` to learn more."


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "args",
    [
        ["status"],
        ["show", "remote"],
        ["auth", "remote"],
        ["reset-auth", "remote"],
        ["clear-auth", "remote"],
    ],
)
async def test_mcp_command_unhandled_args_open_interactive_manager_like_claude(
    args: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    opened: list[Any] = []

    class FakeMCPManagerDialog:
        def __init__(self, context: Any) -> None:
            opened.append(context)

        def run(self) -> str:
            return "MCP dialog dismissed"

    monkeypatch.setattr("iac_code.commands.mcp._mcp_manager_dialog_class", lambda: FakeMCPManagerDialog)
    context = SimpleNamespace(console=Console(record=True), repl=Mock())

    result = await mcp_command(context=context, args=args)

    assert result == "MCP dialog dismissed"
    assert opened == [context]


@pytest.mark.asyncio
async def test_mcp_command_reconnects_named_server_like_claude(monkeypatch: pytest.MonkeyPatch) -> None:
    reconnected: list[str] = []

    class FakeLiveMCPManager:
        def connection(self, server_name: str) -> object:
            if server_name != "remote":
                raise KeyError(server_name)
            return object()

        async def reconnect(self, server_name: str) -> None:
            reconnected.append(server_name)

        def connection_state(self, server_name: str) -> str:
            assert server_name == "remote"
            return "connected"

    cli_reconnect = Mock(side_effect=AssertionError("slash /mcp reconnect should use the live MCP manager"))
    monkeypatch.setattr(mcp_module.mcp_cli, "reconnect_mcp_server", cli_reconnect)
    context = SimpleNamespace(
        console=Console(record=True),
        repl=SimpleNamespace(_mcp_manager=FakeLiveMCPManager()),
    )

    result = await mcp_command(context=context, args=["reconnect", "remote"])

    assert reconnected == ["remote"]
    assert cli_reconnect.call_count == 0
    assert result == "Successfully reconnected to remote"


@pytest.mark.asyncio
async def test_mcp_command_reconnect_scope_does_not_target_same_name_live_server(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    settings_path = tmp_path / "config" / "settings.yml"
    settings_path.parent.mkdir(parents=True)
    settings_path.write_text(
        yaml.safe_dump({"mcpServers": {"shared": {"type": "http", "url": "https://user.example/mcp"}}}),
        encoding="utf-8",
    )
    cli_calls: list[tuple[str | None, bool, str | None]] = []

    class FakeLiveMCPManager:
        def connection(self, server_name: str) -> object:
            if server_name != "shared":
                raise KeyError(server_name)
            return SimpleNamespace(
                scoped_config=SimpleNamespace(scope=SimpleNamespace(value="local")),
                state="connected",
            )

        async def reconnect(self, server_name: str) -> None:
            raise AssertionError("user-scoped quick reconnect must not reconnect the local live server")

    def reconnect_mcp_server(
        *,
        name: str | None = None,
        all_servers: bool = False,
        scope: str | None = None,
        source_path: str | None = None,
        cwd=None,
    ):
        _ = cwd, source_path
        cli_calls.append((name, all_servers, scope))
        return [SimpleNamespace(name=name, connection_state="connected", status="connected")]

    monkeypatch.setattr(
        mcp_module,
        "mcp_cli",
        SimpleNamespace(reconnect_mcp_server=reconnect_mcp_server),
        raising=False,
    )
    context = SimpleNamespace(
        console=Console(record=True),
        repl=SimpleNamespace(_mcp_manager=FakeLiveMCPManager()),
    )

    result = await mcp_command(context=context, args=["reconnect", "shared", "--scope", "user"])

    assert cli_calls == [("shared", False, "user")]
    assert result == "Successfully reconnected to shared"


@pytest.mark.asyncio
async def test_mcp_command_scoped_reconnect_missing_server_returns_message_without_typer_exit(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    context = SimpleNamespace(console=Console(record=True), repl=SimpleNamespace(_mcp_manager=None))

    result = await mcp_command(context=context, args=["reconnect", "missing", "--scope", "user"])

    assert result == 'MCP server "missing" not found'


@pytest.mark.asyncio
async def test_mcp_command_reconnect_source_path_requires_scope_without_traceback(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    (tmp_path / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"remote": {"type": "http", "url": "https://example.com/mcp"}}}),
        encoding="utf-8",
    )
    context = SimpleNamespace(console=Console(record=True), repl=SimpleNamespace(_mcp_manager=None))

    result = await mcp_command(context=context, args=["reconnect", "remote", "--source-path", ".mcp.json"])

    assert result == "Error: --source-path requires --scope."


@pytest.mark.asyncio
async def test_mcp_command_scoped_reconnect_uses_repl_cwd_for_persisted_lookup(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    process_cwd = tmp_path / "outside"
    repl_cwd = tmp_path / "repo" / "services" / "api"
    process_cwd.mkdir()
    repl_cwd.mkdir(parents=True)
    (tmp_path / "repo" / ".git").mkdir()
    (tmp_path / "repo" / "services" / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"shared": {"type": "http", "url": "https://project.example/mcp"}}}),
        encoding="utf-8",
    )
    monkeypatch.chdir(process_cwd)
    calls: list[tuple[str | None, str | None, str | None]] = []

    def reconnect_mcp_server(
        *,
        name: str | None = None,
        all_servers: bool = False,
        scope: str | None = None,
        cwd=None,
        **kwargs,
    ):
        _ = all_servers, kwargs
        calls.append((name, scope, str(cwd) if cwd is not None else None))
        return [SimpleNamespace(name=name, connection_state="connected", status="connected")]

    monkeypatch.setattr(
        mcp_module,
        "mcp_cli",
        SimpleNamespace(reconnect_mcp_server=reconnect_mcp_server),
        raising=False,
    )
    context = SimpleNamespace(
        console=Console(record=True),
        repl=SimpleNamespace(_mcp_manager=None, _original_cwd=str(repl_cwd)),
    )

    result = await mcp_command(context=context, args=["reconnect", "shared", "--scope", "project"])

    assert calls == [("shared", "project", str(repl_cwd))]
    assert result == "Successfully reconnected to shared"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("state", "expected"),
    [
        ("needs_auth", "remote requires authentication. Use /mcp to authenticate."),
        ("needs-auth", "remote requires authentication. Use /mcp to authenticate."),
        ("failed", "Failed to reconnect to remote"),
        ("pending", "Failed to reconnect to remote"),
        ("disabled", "Failed to reconnect to remote"),
        ("unknown", "Failed to reconnect to remote"),
    ],
)
async def test_mcp_command_reconnect_reports_claude_result_messages(state: str, expected: str) -> None:
    class FakeLiveMCPManager:
        def connection(self, server_name: str) -> object:
            if server_name != "remote":
                raise KeyError(server_name)
            return object()

        async def reconnect(self, server_name: str) -> None:
            assert server_name == "remote"

        def connection_state(self, server_name: str) -> str:
            assert server_name == "remote"
            return state

    context = SimpleNamespace(
        console=Console(record=True),
        repl=SimpleNamespace(_mcp_manager=FakeLiveMCPManager()),
    )

    result = await mcp_command(context=context, args=["reconnect", "remote"])

    assert result == expected


@pytest.mark.asyncio
async def test_mcp_command_reconnect_reports_claude_error_message() -> None:
    class FakeLiveMCPManager:
        def connection(self, server_name: str) -> object:
            if server_name != "remote":
                raise KeyError(server_name)
            return object()

        async def reconnect(self, server_name: str) -> None:
            raise RuntimeError("boom")

    context = SimpleNamespace(
        console=Console(record=True),
        repl=SimpleNamespace(_mcp_manager=FakeLiveMCPManager()),
    )

    result = await mcp_command(context=context, args=["reconnect", "remote"])

    assert result == "Error: boom"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("action", "expected", "message"),
    [
        ("enable", "enable_mcp_server_command", 'MCP server "remote" enabled'),
        ("disable", "disable_mcp_server_command", 'MCP server "remote" disabled'),
    ],
)
async def test_mcp_command_toggles_named_server_like_claude(
    action: str,
    expected: str,
    message: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, str | None]] = []
    refreshed: list[bool] = []

    async def refresh_mcp_integrations() -> None:
        refreshed.append(True)

    monkeypatch.setattr(
        mcp_module,
        "load_all_persisted_mcp_configs",
        lambda **kwargs: SimpleNamespace(
            servers=[SimpleNamespace(name="remote", scope=SimpleNamespace(value="user"), disabled=action == "enable")],
            pending=[],
            warnings=[],
        ),
        raising=False,
    )
    monkeypatch.setattr(
        mcp_module,
        "mcp_cli",
        SimpleNamespace(**{expected: lambda name, scope=None: calls.append((name, scope)) or "ignored"}),
        raising=False,
    )
    context = SimpleNamespace(
        console=Console(record=True),
        repl=SimpleNamespace(refresh_mcp_integrations=refresh_mcp_integrations),
    )

    result = await mcp_command(context=context, args=[action, "remote"])

    assert calls == [("remote", "user")]
    assert refreshed == [True]
    assert result == message


@pytest.mark.asyncio
async def test_mcp_command_named_toggle_accepts_scope_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, str | None]] = []

    monkeypatch.setattr(
        mcp_module,
        "load_all_persisted_mcp_configs",
        lambda **kwargs: SimpleNamespace(
            servers=[
                SimpleNamespace(name="remote", scope=SimpleNamespace(value="user"), disabled=False),
                SimpleNamespace(name="remote", scope=SimpleNamespace(value="project"), disabled=False),
            ],
            pending=[],
            warnings=[],
        ),
        raising=False,
    )
    monkeypatch.setattr(
        mcp_module,
        "mcp_cli",
        SimpleNamespace(disable_mcp_server_command=lambda name, scope=None: calls.append((name, scope)) or "ignored"),
        raising=False,
    )
    context = SimpleNamespace(console=Console(record=True), repl=SimpleNamespace(_original_cwd="/repo"))

    result = await mcp_command(context=context, args=["disable", "remote", "--scope", "user"])

    assert result == 'MCP server "remote" disabled'
    assert calls == [("remote", "user")]


@pytest.mark.asyncio
async def test_mcp_command_named_toggle_uses_context_cwd_for_project_server(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    process_cwd = tmp_path / "process"
    project = tmp_path / "project"
    process_cwd.mkdir()
    project.mkdir()
    monkeypatch.chdir(process_cwd)
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    (project / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"remote": {"type": "http", "url": "https://project.example/mcp"}}}),
        encoding="utf-8",
    )
    refreshed: list[bool] = []

    async def refresh_mcp_integrations() -> None:
        refreshed.append(True)

    context = SimpleNamespace(
        console=Console(record=True),
        repl=SimpleNamespace(_original_cwd=str(project), refresh_mcp_integrations=refresh_mcp_integrations),
    )

    result = await mcp_command(context=context, args=["disable", "remote", "--scope", "project"])

    assert result == 'MCP server "remote" disabled'
    assert refreshed == [True]
    loaded = load_all_persisted_mcp_configs(cwd=project)
    assert [server.name for server in loaded.servers] == ["remote"]
    assert loaded.servers[0].disabled is True


@pytest.mark.asyncio
async def test_mcp_command_project_scope_toggle_uses_nearest_project_source(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    root = tmp_path / "workspace"
    child = root / "child"
    process_cwd = tmp_path / "process"
    child.mkdir(parents=True)
    process_cwd.mkdir()
    (root / ".git").mkdir()
    monkeypatch.chdir(process_cwd)
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    (root / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"shared": {"type": "http", "url": "https://root.example/mcp"}}}),
        encoding="utf-8",
    )
    (child / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"shared": {"type": "http", "url": "https://child.example/mcp"}}}),
        encoding="utf-8",
    )
    refreshed: list[bool] = []
    context = SimpleNamespace(
        console=Console(record=True),
        repl=SimpleNamespace(_original_cwd=str(child), refresh_mcp_integrations=lambda: refreshed.append(True)),
    )

    result = await mcp_command(context=context, args=["disable", "shared", "--scope", "project"])

    assert result == 'MCP server "shared" disabled'
    assert refreshed == [True]
    loaded = load_all_persisted_mcp_configs(cwd=child)
    by_source = {server.source_path: server for server in loaded.servers}
    assert by_source[str(root / ".mcp.json")].disabled is False
    assert by_source[str(child / ".mcp.json")].disabled is True


@pytest.mark.asyncio
async def test_mcp_command_named_toggle_accepts_source_path_for_project_server(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    root = tmp_path / "workspace"
    child = root / "child"
    child.mkdir(parents=True)
    (root / ".git").mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    root_project_file = root / ".mcp.json"
    child_project_file = child / ".mcp.json"
    root_project_file.write_text(
        json.dumps({"mcpServers": {"shared": {"command": "root-cmd"}}}),
        encoding="utf-8",
    )
    child_project_file.write_text(
        json.dumps({"mcpServers": {"shared": {"command": "child-cmd"}}}),
        encoding="utf-8",
    )
    refreshed: list[bool] = []
    context = SimpleNamespace(
        console=Console(record=True),
        repl=SimpleNamespace(_original_cwd=str(child), refresh_mcp_integrations=lambda: refreshed.append(True)),
    )

    result = await mcp_command(
        context=context,
        args=["disable", "shared", "--scope", "project", "--source-path", str(root_project_file)],
    )

    assert result == 'MCP server "shared" disabled'
    assert refreshed == [True]
    loaded = load_all_persisted_mcp_configs(cwd=child)
    by_source = {server.source_path: server for server in loaded.servers}
    assert by_source[str(root_project_file)].disabled is True
    assert by_source[str(child_project_file)].disabled is False


@pytest.mark.asyncio
async def test_mcp_command_named_toggle_resolves_relative_source_path_from_repl_cwd(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    process_cwd = tmp_path / "outside"
    root = tmp_path / "workspace"
    child = root / "child"
    process_cwd.mkdir()
    child.mkdir(parents=True)
    (root / ".git").mkdir()
    child_project_file = child / ".mcp.json"
    child_project_file.write_text(
        json.dumps({"mcpServers": {"shared": {"command": "child-cmd"}}}),
        encoding="utf-8",
    )
    monkeypatch.chdir(process_cwd)
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    refreshed: list[bool] = []
    context = SimpleNamespace(
        console=Console(record=True),
        repl=SimpleNamespace(_original_cwd=str(child), refresh_mcp_integrations=lambda: refreshed.append(True)),
    )

    result = await mcp_command(
        context=context,
        args=["disable", "shared", "--scope", "project", "--source-path", ".mcp.json"],
    )

    assert result == 'MCP server "shared" disabled'
    assert refreshed == [True]
    loaded = load_all_persisted_mcp_configs(cwd=child)
    assert loaded.servers[0].source_path == str(child_project_file)
    assert loaded.servers[0].disabled is True


@pytest.mark.asyncio
async def test_mcp_command_named_toggle_without_scope_returns_disambiguation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    user_settings = tmp_path / "config" / "settings.yml"
    local_settings = tmp_path / ".iac-code" / "settings.local.yml"
    user_settings.parent.mkdir(parents=True)
    local_settings.parent.mkdir(parents=True)
    user_settings.write_text(
        yaml.safe_dump({"mcpServers": {"shared": {"type": "http", "url": "https://user.example/mcp"}}}),
        encoding="utf-8",
    )
    local_settings.write_text(
        yaml.safe_dump({"mcpServers": {"shared": {"type": "http", "url": "https://local.example/mcp"}}}),
        encoding="utf-8",
    )

    context = SimpleNamespace(
        console=Console(record=True),
        repl=SimpleNamespace(_original_cwd=str(tmp_path), refresh_mcp_integrations=lambda: None),
    )

    result = await mcp_command(context=context, args=["disable", "shared"])

    assert "MCP server 'shared' exists in multiple persisted scopes." in result
    assert "iac-code mcp disable shared --scope local" in result
    assert "iac-code mcp disable shared --scope user" in result


@pytest.mark.asyncio
async def test_mcp_command_named_toggle_without_scope_disambiguates_missing_env_entries(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    user_settings = tmp_path / "config" / "settings.yml"
    local_settings = tmp_path / ".iac-code" / "settings.local.yml"
    user_settings.parent.mkdir(parents=True)
    local_settings.parent.mkdir(parents=True)
    user_settings.write_text(
        yaml.safe_dump(
            {
                "mcpServers": {
                    "shared": {
                        "type": "http",
                        "url": "https://user.example/mcp",
                        "headers": {"Authorization": "Bearer ${MISSING_TOKEN}"},
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    local_settings.write_text(
        yaml.safe_dump({"mcpServers": {"shared": {"type": "http", "url": "https://local.example/mcp"}}}),
        encoding="utf-8",
    )
    refreshed: list[bool] = []
    context = SimpleNamespace(
        console=Console(record=True),
        repl=SimpleNamespace(_original_cwd=str(tmp_path), refresh_mcp_integrations=lambda: refreshed.append(True)),
    )

    result = await mcp_command(context=context, args=["disable", "shared"])

    assert "MCP server 'shared' exists in multiple persisted scopes." in result
    assert "iac-code mcp disable shared --scope local" in result
    assert "iac-code mcp disable shared --scope user" in result
    assert refreshed == []
    loaded = load_all_persisted_mcp_configs(cwd=tmp_path)
    assert loaded.servers[0].disabled is False


@pytest.mark.asyncio
async def test_mcp_command_named_toggle_reports_not_found_like_claude(monkeypatch: pytest.MonkeyPatch) -> None:
    refreshed: list[bool] = []

    async def refresh_mcp_integrations() -> None:
        refreshed.append(True)

    monkeypatch.setattr(
        mcp_module,
        "load_all_persisted_mcp_configs",
        lambda **kwargs: SimpleNamespace(
            servers=[SimpleNamespace(name="remote", scope=SimpleNamespace(value="user"), disabled=False)],
            pending=[],
            warnings=[],
        ),
        raising=False,
    )
    monkeypatch.setattr(
        mcp_module,
        "mcp_cli",
        SimpleNamespace(
            enable_mcp_server_command=Mock(side_effect=AssertionError("missing target should not call CLI handler")),
            disable_mcp_server_command=Mock(side_effect=AssertionError("missing target should not call CLI handler")),
        ),
        raising=False,
    )
    context = SimpleNamespace(
        console=Console(record=True),
        repl=SimpleNamespace(_original_cwd="/repo", refresh_mcp_integrations=refresh_mcp_integrations),
    )

    result = await mcp_command(context=context, args=["disable", "missing"])

    assert result == 'MCP server "missing" not found'
    assert refreshed == []


@pytest.mark.asyncio
async def test_mcp_command_named_disable_handles_missing_env_persisted_server(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
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
            }
        ),
        encoding="utf-8",
    )
    refreshed: list[bool] = []

    async def refresh_mcp_integrations() -> None:
        refreshed.append(True)

    context = SimpleNamespace(
        console=Console(record=True),
        repl=SimpleNamespace(_original_cwd=str(tmp_path), refresh_mcp_integrations=refresh_mcp_integrations),
    )

    result = await mcp_command(context=context, args=["disable", "remote"])

    assert result == 'MCP server "remote" disabled'
    assert refreshed == [True]
    loaded = load_all_persisted_mcp_configs(cwd=tmp_path)
    assert [server.name for server in loaded.servers] == ["remote"]
    assert loaded.servers[0].disabled is True
    assert loaded.warnings == []


@pytest.mark.asyncio
async def test_mcp_command_named_disable_handles_scalar_invalid_config_persisted_server(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    settings_path = tmp_path / "config" / "settings.yml"
    settings_path.parent.mkdir(parents=True)
    settings_path.write_text(yaml.safe_dump({"mcpServers": {"bad": "not-an-object"}}), encoding="utf-8")
    refreshed: list[bool] = []

    async def refresh_mcp_integrations() -> None:
        refreshed.append(True)

    context = SimpleNamespace(
        console=Console(record=True),
        repl=SimpleNamespace(_original_cwd=str(tmp_path), refresh_mcp_integrations=refresh_mcp_integrations),
    )

    result = await mcp_command(context=context, args=["disable", "bad", "--scope", "user"])

    assert result == 'MCP server "bad" disabled'
    assert refreshed == [True]
    loaded = load_all_persisted_mcp_configs(cwd=tmp_path)
    assert [server.name for server in loaded.servers] == ["bad"]
    assert loaded.servers[0].disabled is True
    assert loaded.warnings == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("action", "disabled_flags", "expected_calls", "message"),
    [
        ("enable", [True, False, True], [("alpha", "user"), ("gamma", "project")], "Enabled 2 MCP server(s)"),
        ("disable", [True, False, False], [("beta", "local"), ("gamma", "project")], "Disabled 2 MCP server(s)"),
    ],
)
async def test_mcp_command_toggles_all_servers_like_claude(
    action: str,
    disabled_flags: list[bool],
    expected_calls: list[tuple[str, str]],
    message: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    names_and_scopes = [("alpha", "user"), ("beta", "local"), ("gamma", "project")]
    servers = [
        SimpleNamespace(name=name, scope=SimpleNamespace(value=scope), disabled=disabled)
        for (name, scope), disabled in zip(names_and_scopes, disabled_flags, strict=True)
    ]
    calls: list[tuple[str, str | None]] = []

    monkeypatch.setattr(
        mcp_module,
        "load_all_persisted_mcp_configs",
        lambda **kwargs: SimpleNamespace(servers=servers, warnings=[]),
        raising=False,
    )
    monkeypatch.setattr(
        mcp_module,
        "mcp_cli",
        SimpleNamespace(
            enable_mcp_server_command=lambda name, scope=None: calls.append((name, scope)) or "enabled",
            disable_mcp_server_command=lambda name, scope=None: calls.append((name, scope)) or "disabled",
        ),
        raising=False,
    )
    context = SimpleNamespace(console=Console(record=True), repl=SimpleNamespace(_original_cwd="/repo"))

    result = await mcp_command(context=context, args=[action])

    assert calls == expected_calls
    assert result == message


@pytest.mark.asyncio
async def test_mcp_command_disable_all_handles_missing_env_persisted_server(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
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
            }
        ),
        encoding="utf-8",
    )
    refreshed: list[bool] = []

    async def refresh_mcp_integrations() -> None:
        refreshed.append(True)

    context = SimpleNamespace(
        console=Console(record=True),
        repl=SimpleNamespace(_original_cwd=str(tmp_path), refresh_mcp_integrations=refresh_mcp_integrations),
    )

    result = await mcp_command(context=context, args=["disable"])

    assert result == "Disabled 1 MCP server(s)"
    assert refreshed == [True]
    loaded = load_all_persisted_mcp_configs(cwd=tmp_path)
    assert [server.name for server in loaded.servers] == ["remote"]
    assert loaded.servers[0].disabled is True
    assert loaded.warnings == []


@pytest.mark.asyncio
async def test_mcp_command_disable_all_handles_scalar_invalid_config_persisted_server(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    settings_path = tmp_path / "config" / "settings.yml"
    settings_path.parent.mkdir(parents=True)
    settings_path.write_text(yaml.safe_dump({"mcpServers": {"bad": "not-an-object"}}), encoding="utf-8")
    refreshed: list[bool] = []

    async def refresh_mcp_integrations() -> None:
        refreshed.append(True)

    context = SimpleNamespace(
        console=Console(record=True),
        repl=SimpleNamespace(_original_cwd=str(tmp_path), refresh_mcp_integrations=refresh_mcp_integrations),
    )

    result = await mcp_command(context=context, args=["disable"])

    assert result == "Disabled 1 MCP server(s)"
    assert refreshed == [True]
    loaded = load_all_persisted_mcp_configs(cwd=tmp_path)
    assert [server.name for server in loaded.servers] == ["bad"]
    assert loaded.servers[0].disabled is True
    assert loaded.warnings == []


@pytest.mark.asyncio
async def test_mcp_command_disable_all_handles_missing_env_same_name_across_scopes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    user_settings = tmp_path / "config" / "settings.yml"
    local_settings = tmp_path / ".iac-code" / "settings.local.yml"
    user_settings.parent.mkdir(parents=True)
    local_settings.parent.mkdir(parents=True)
    for path, url in (
        (user_settings, "https://user.example/mcp"),
        (local_settings, "https://local.example/mcp"),
    ):
        path.write_text(
            yaml.safe_dump(
                {
                    "mcpServers": {
                        "remote": {
                            "type": "http",
                            "url": url,
                            "headers": {"Authorization": "Bearer ${MISSING_TOKEN}"},
                        }
                    }
                }
            ),
            encoding="utf-8",
        )

    context = SimpleNamespace(
        console=Console(record=True),
        repl=SimpleNamespace(_original_cwd=str(tmp_path), refresh_mcp_integrations=lambda: None),
    )

    result = await mcp_command(context=context, args=["disable"])

    assert result == "Disabled 2 MCP server(s)"
    loaded = load_all_persisted_mcp_configs(cwd=tmp_path)
    assert [server.scope.value for server in loaded.servers] == ["user", "local"]
    assert [server.name for server in loaded.servers] == ["remote", "remote"]
    assert all(server.disabled for server in loaded.servers)
    assert loaded.warnings == []


@pytest.mark.asyncio
async def test_mcp_command_disable_all_handles_missing_env_same_name_nested_project_files(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    root = tmp_path
    child = root / "child"
    child.mkdir()
    (root / ".git").mkdir()
    for path, url in (
        (root / ".mcp.json", "https://root.example/mcp"),
        (child / ".mcp.json", "https://child.example/mcp"),
    ):
        path.write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "remote": {
                            "type": "http",
                            "url": url,
                            "headers": {"Authorization": "Bearer ${MISSING_TOKEN}"},
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
    monkeypatch.chdir(child)

    context = SimpleNamespace(
        console=Console(record=True),
        repl=SimpleNamespace(_original_cwd=str(child), refresh_mcp_integrations=lambda: None),
    )

    result = await mcp_command(context=context, args=["disable"])

    assert result == "Disabled 2 MCP server(s)"
    loaded = load_all_persisted_mcp_configs(cwd=child)
    assert [server.source_path for server in loaded.servers] == [
        str(root / ".mcp.json"),
        str(child / ".mcp.json"),
    ]
    assert all(server.disabled for server in loaded.servers)
    assert loaded.warnings == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("action", "disabled_flags", "message"),
    [
        ("enable", [False, False], "All MCP servers are already enabled"),
        ("disable", [True, True], "All MCP servers are already disabled"),
    ],
)
async def test_mcp_command_toggle_all_noop_message_matches_claude(
    action: str,
    disabled_flags: list[bool],
    message: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    servers = [
        SimpleNamespace(name=name, scope=SimpleNamespace(value="user"), disabled=disabled)
        for name, disabled in zip(["alpha", "beta"], disabled_flags, strict=True)
    ]

    monkeypatch.setattr(
        mcp_module,
        "load_all_persisted_mcp_configs",
        lambda **kwargs: SimpleNamespace(servers=servers, warnings=[]),
        raising=False,
    )
    context = SimpleNamespace(console=Console(record=True), repl=SimpleNamespace(_original_cwd="/repo"))

    result = await mcp_command(context=context, args=[action])

    assert result == message
