import json
import os
import stat
from pathlib import Path

import pytest
import yaml

from iac_code.mcp.config import approve_project_mcp_server, load_mcp_configs, write_mcp_server_config
from iac_code.mcp.types import MCPConfigError, MCPConfigScope


def test_load_mcp_configs_merges_sources_by_precedence_and_deduplicates(monkeypatch, tmp_path: Path) -> None:
    config_dir = tmp_path / "config"
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".iac-code").mkdir()
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(config_dir))

    _write_yaml(
        config_dir / "settings.yml",
        {
            "mcpServers": {
                "shared": {"command": "user-cmd"},
                "same-user": {"command": "same", "args": ["server"]},
            }
        },
    )
    project_file = repo / ".mcp.json"
    _write_json(
        project_file,
        {
            "mcpServers": {
                "shared": {"command": "project-cmd"},
                "project-only": {"command": "${PROJECT_CMD:-project-server}"},
            }
        },
    )
    approve_project_mcp_server("shared", project_file=project_file, workspace_root=repo)
    approve_project_mcp_server("project-only", project_file=project_file, workspace_root=repo)
    _write_yaml(
        repo / ".iac-code" / "settings.local.yml",
        {
            "mcpServers": {
                "shared": {"command": "local-cmd"},
                "same-local": {"command": "same", "args": ["server"]},
            }
        },
    )

    result = load_mcp_configs(
        cwd=repo,
        workspace_root=repo,
        session_configs={
            "shared": {"command": "session-cmd"},
            "session-only": {"type": "http", "url": "https://example.com/mcp"},
        },
        env={},
    )

    by_name = result.by_name()
    assert by_name["shared"].scope is MCPConfigScope.SESSION
    assert by_name["shared"].config.command == "session-cmd"
    assert by_name["project-only"].config.command == "project-server"
    assert by_name["same-local"].scope is MCPConfigScope.LOCAL
    assert "same-user" not in by_name
    assert any(warning.code == "duplicate_config" and "same-user" in warning.message for warning in result.warnings)


def test_project_discovery_searches_root_to_leaf_and_stops_at_workspace_root(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    workspace_root = tmp_path / "repo"
    nested = workspace_root / "services" / "api"
    nested.mkdir(parents=True)
    outside_file = tmp_path / ".mcp.json"
    root_file = workspace_root / ".mcp.json"
    child_file = workspace_root / "services" / ".mcp.json"

    _write_json(outside_file, {"mcpServers": {"outside": {"command": "outside"}}})
    _write_json(
        root_file,
        {"mcpServers": {"shared": {"command": "root"}, "root-only": {"command": "root-only"}}},
    )
    _write_json(
        child_file,
        {"mcpServers": {"shared": {"command": "child"}, "child-only": {"command": "child-only"}}},
    )
    for name, path in [
        ("shared", root_file),
        ("root-only", root_file),
        ("shared", child_file),
        ("child-only", child_file),
    ]:
        approve_project_mcp_server(name, project_file=path, workspace_root=workspace_root)

    result = load_mcp_configs(cwd=nested, workspace_root=workspace_root, env={})

    by_name = result.by_name()
    assert set(by_name) == {"shared", "root-only", "child-only"}
    assert by_name["shared"].config.command == "child"
    assert "outside" not in by_name


def test_pending_project_servers_are_reported_until_approved(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    repo = tmp_path / "repo"
    repo.mkdir()
    project_file = repo / ".mcp.json"
    _write_json(project_file, {"mcpServers": {"pending": {"command": "uvx"}}})

    result = load_mcp_configs(cwd=repo, workspace_root=repo, env={})

    assert result.servers == []
    assert [pending.name for pending in result.pending] == ["pending"]

    approve_project_mcp_server("pending", project_file=project_file, workspace_root=repo)
    approved = load_mcp_configs(cwd=repo, workspace_root=repo, env={})

    assert [server.name for server in approved.servers] == ["pending"]
    assert "approved" not in project_file.read_text(encoding="utf-8")
    assert (tmp_path / "config" / "mcp" / "project-approvals.json").exists()


def test_project_approval_is_invalidated_when_config_changes(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    repo = tmp_path / "repo"
    repo.mkdir()
    project_file = repo / ".mcp.json"
    _write_json(project_file, {"mcpServers": {"server": {"command": "uvx", "args": ["safe"]}}})
    approve_project_mcp_server("server", project_file=project_file, workspace_root=repo)
    assert [server.name for server in load_mcp_configs(cwd=repo, workspace_root=repo, env={}).servers] == ["server"]

    _write_json(project_file, {"mcpServers": {"server": {"command": "uvx", "args": ["changed"]}}})
    changed = load_mcp_configs(cwd=repo, workspace_root=repo, env={})

    assert changed.servers == []
    assert [pending.name for pending in changed.pending] == ["server"]


def test_content_signature_distinguishes_env_headers_and_oauth(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    _write_yaml(
        tmp_path / "config" / "settings.yml",
        {
            "mcpServers": {
                "first": {"command": "uvx", "env": {"TENANT": "a"}},
                "second": {"command": "uvx", "env": {"TENANT": "b"}},
                "remote-a": {"type": "http", "url": "https://example.com/mcp", "headers": {"X-Org": "a"}},
                "remote-b": {"type": "http", "url": "https://example.com/mcp", "headers": {"X-Org": "b"}},
            }
        },
    )

    result = load_mcp_configs(cwd=tmp_path, workspace_root=tmp_path, env={})

    assert set(result.by_name()) == {"first", "second", "remote-a", "remote-b"}


def test_invalid_server_config_skips_only_that_server(monkeypatch, tmp_path: Path) -> None:
    config_dir = tmp_path / "config"
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(config_dir))
    _write_yaml(
        config_dir / "settings.yml",
        {
            "mcpServers": {
                "good": {"command": "uvx"},
                "bad": {"type": "ws", "url": "wss://example.com"},
            }
        },
    )

    result = load_mcp_configs(cwd=tmp_path, workspace_root=tmp_path, env={})

    assert [server.name for server in result.servers] == ["good"]
    assert any(warning.server_name == "bad" and warning.code == "invalid_config" for warning in result.warnings)


def test_missing_env_reference_skips_server_instead_of_passing_placeholder(monkeypatch, tmp_path: Path) -> None:
    config_dir = tmp_path / "config"
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(config_dir))
    _write_yaml(
        config_dir / "settings.yml",
        {"mcpServers": {"missing": {"command": "${MISSING_MCP_COMMAND}"}, "good": {"command": "uvx"}}},
    )

    result = load_mcp_configs(cwd=tmp_path, workspace_root=tmp_path, env={})

    assert [server.name for server in result.servers] == ["good"]
    assert any(warning.server_name == "missing" and warning.code == "missing_env" for warning in result.warnings)


def test_write_mcp_server_config_targets_scope_files_and_validates_before_write(monkeypatch, tmp_path: Path) -> None:
    config_dir = tmp_path / "config"
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(config_dir))

    write_mcp_server_config("user-server", {"command": "uvx"}, scope=MCPConfigScope.USER, cwd=repo)
    write_mcp_server_config("local-server", {"command": "python"}, scope=MCPConfigScope.LOCAL, cwd=repo)
    write_mcp_server_config(
        "project-server",
        {"type": "sse", "url": "https://example.com/sse"},
        scope=MCPConfigScope.PROJECT,
        cwd=repo,
    )

    assert _read_yaml(config_dir / "settings.yml")["mcpServers"]["user-server"]["command"] == "uvx"
    assert _read_yaml(repo / ".iac-code" / "settings.local.yml")["mcpServers"]["local-server"]["command"] == "python"
    assert json.loads((repo / ".mcp.json").read_text(encoding="utf-8"))["mcpServers"]["project-server"] == {
        "type": "sse",
        "url": "https://example.com/sse",
    }

    with pytest.raises(MCPConfigError):
        write_mcp_server_config("bad", {"type": "ws", "url": "wss://example.com"}, scope=MCPConfigScope.USER, cwd=repo)
    assert "bad" not in _read_yaml(config_dir / "settings.yml")["mcpServers"]

    with pytest.raises(MCPConfigError):
        write_mcp_server_config("bad name", {"command": "uvx"}, scope=MCPConfigScope.USER, cwd=repo)

    with pytest.raises(MCPConfigError):
        write_mcp_server_config("mcp__reserved", {"command": "uvx"}, scope=MCPConfigScope.USER, cwd=repo)


def test_write_mcp_server_config_preserves_private_permissions(monkeypatch, tmp_path: Path) -> None:
    config_dir = tmp_path / "config"
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(config_dir))

    path = write_mcp_server_config("user-server", {"command": "uvx"}, scope=MCPConfigScope.USER, cwd=repo)

    assert path.exists()
    assert path.parent.exists()
    if os.name != "nt":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700


def test_session_injected_invalid_config_is_reported_without_blocking_valid_servers(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))

    result = load_mcp_configs(
        cwd=tmp_path,
        workspace_root=tmp_path,
        session_configs={
            "good": {"type": "http", "url": "https://example.com/mcp"},
            "bad": {"type": "ws", "url": "wss://example.com/mcp"},
        },
        env={},
    )

    assert [server.name for server in result.servers] == ["good"]
    assert any(warning.server_name == "bad" and warning.code == "invalid_config" for warning in result.warnings)


def _write_yaml(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(value), encoding="utf-8")


def _read_yaml(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")
