import json
import os
import stat
from pathlib import Path

import pytest
import yaml

from iac_code.mcp import config as mcp_config
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


def test_project_headers_helper_is_pending_until_approved_and_keeps_source_dir(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    repo = tmp_path / "repo"
    service = repo / "services" / "api"
    service.mkdir(parents=True)
    project_file = service / ".mcp.json"
    _write_json(
        project_file,
        {
            "mcpServers": {
                "remote": {
                    "type": "http",
                    "url": "https://example.com/mcp",
                    "headersHelper": "python ./scripts/mcp_headers.py",
                }
            }
        },
    )

    pending = load_mcp_configs(cwd=service, workspace_root=repo, env={})

    assert pending.servers == []
    assert [server.name for server in pending.pending] == ["remote"]

    approve_project_mcp_server("remote", project_file=project_file, workspace_root=repo)
    approved = load_mcp_configs(cwd=service, workspace_root=repo, env={})

    assert [server.name for server in approved.servers] == ["remote"]
    assert approved.servers[0].config.headers_helper == "python ./scripts/mcp_headers.py"
    assert approved.servers[0].config.source_dir == str(service)
    assert "headersHelper" in project_file.read_text(encoding="utf-8")
    assert "X-Org" not in project_file.read_text(encoding="utf-8")


def test_project_headers_helper_approval_is_invalidated_when_helper_changes(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    repo = tmp_path / "repo"
    repo.mkdir()
    project_file = repo / ".mcp.json"
    _write_json(
        project_file,
        {
            "mcpServers": {
                "remote": {
                    "type": "http",
                    "url": "https://example.com/mcp",
                    "headersHelper": "python ./scripts/mcp_headers.py",
                }
            }
        },
    )
    approve_project_mcp_server("remote", project_file=project_file, workspace_root=repo)
    assert [server.name for server in load_mcp_configs(cwd=repo, workspace_root=repo, env={}).servers] == ["remote"]

    _write_json(
        project_file,
        {
            "mcpServers": {
                "remote": {
                    "type": "http",
                    "url": "https://example.com/mcp",
                    "headersHelper": "python ./scripts/changed_headers.py",
                }
            }
        },
    )
    changed = load_mcp_configs(cwd=repo, workspace_root=repo, env={})

    assert changed.servers == []
    assert [pending.name for pending in changed.pending] == ["remote"]


def test_project_headers_helper_plaintext_secret_is_invalid_config(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_json(
        repo / ".mcp.json",
        {
            "mcpServers": {
                "remote": {
                    "type": "http",
                    "url": "https://example.com/mcp",
                    "headersHelper": "python ./scripts/mcp_headers.py --token plain-secret",
                }
            }
        },
    )

    loaded = load_mcp_configs(cwd=repo, workspace_root=repo, env={})

    assert loaded.servers == []
    assert loaded.pending == []
    assert len(loaded.warnings) == 1
    assert loaded.warnings[0].code == "invalid_config"
    assert loaded.warnings[0].server_name == "remote"
    assert "headersHelper" in loaded.warnings[0].message
    assert "plain-secret" not in loaded.warnings[0].message


def test_project_secret_like_env_default_under_non_sensitive_key_is_invalid_config(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_json(
        repo / ".mcp.json",
        {
            "mcpServers": {
                "remote": {
                    "command": "uvx",
                    "env": {"TEAM": "${SAFE_TEAM:-api_key=plain-secret}"},
                }
            }
        },
    )

    loaded = load_mcp_configs(cwd=repo, workspace_root=repo, env={})

    assert loaded.servers == []
    assert loaded.pending == []
    assert len(loaded.warnings) == 1
    assert loaded.warnings[0].code == "invalid_config"
    assert loaded.warnings[0].server_name == "remote"
    assert "TEAM" in loaded.warnings[0].message
    assert "plain-secret" not in loaded.warnings[0].message


def test_user_headers_helper_env_reference_loads_without_expanding_secret(monkeypatch, tmp_path: Path) -> None:
    config_dir = tmp_path / "config"
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(config_dir))
    _write_yaml(
        config_dir / "settings.yml",
        {
            "mcpServers": {
                "remote": {
                    "type": "http",
                    "url": "https://example.com/mcp",
                    "headersHelper": "python ./headers.py --token ${MCP_TOKEN} 'Authorization: Bearer ${MCP_TOKEN}'",
                }
            }
        },
    )

    loaded = load_mcp_configs(cwd=repo, env={"MCP_TOKEN": "runtime-secret"})

    assert loaded.warnings == []
    assert [server.name for server in loaded.servers] == ["remote"]
    assert (
        loaded.servers[0].config.headers_helper
        == "python ./headers.py --token ${MCP_TOKEN} 'Authorization: Bearer ${MCP_TOKEN}'"
    )
    assert "runtime-secret" not in loaded.servers[0].config.content_signature()


def test_disabled_project_server_state_is_private_and_invalidated_by_config_change(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    repo = tmp_path / "repo"
    repo.mkdir()
    project_file = repo / ".mcp.json"
    _write_json(project_file, {"mcpServers": {"yuque": {"command": "uvx", "args": ["mcp-yuque"]}}})
    approve_project_mcp_server("yuque", project_file=project_file, workspace_root=repo)
    original_project_config = project_file.read_text(encoding="utf-8")

    disabled = mcp_config.disable_mcp_server("yuque", scope=MCPConfigScope.PROJECT, cwd=repo)

    assert disabled.disabled is True
    assert project_file.read_text(encoding="utf-8") == original_project_config
    loaded = load_mcp_configs(cwd=repo, workspace_root=repo, env={}, include_pending_project=True)
    assert loaded.by_name()["yuque"].disabled is True
    state_path = tmp_path / "config" / "mcp" / "server-states.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["disabled"]

    _write_json(project_file, {"mcpServers": {"yuque": {"command": "uvx", "args": ["mcp-yuque-v2"]}}})
    approve_project_mcp_server("yuque", project_file=project_file, workspace_root=repo)
    changed = load_mcp_configs(cwd=repo, workspace_root=repo, env={}, include_pending_project=True)

    assert changed.by_name()["yuque"].disabled is False


@pytest.mark.parametrize(
    ("original_config", "changed_config"),
    [
        (
            {
                "type": "http",
                "url": "https://example.com/mcp",
                "headers": {"Authorization": "Bearer ${TOKEN_A}"},
            },
            {
                "type": "http",
                "url": "https://example.com/mcp",
                "headers": {"Authorization": "Bearer ${TOKEN_B}"},
            },
        ),
        (
            {"command": "uvx", "env": {"API_TOKEN": "${TOKEN_A}"}},
            {"command": "uvx", "env": {"API_TOKEN": "${TOKEN_B}"}},
        ),
        (
            {
                "type": "http",
                "url": "https://example.com/mcp",
                "oauth": {"clientSecretEnv": "TOKEN_A"},
            },
            {
                "type": "http",
                "url": "https://example.com/mcp",
                "oauth": {"clientSecretEnv": "TOKEN_B"},
            },
        ),
    ],
)
def test_disabled_state_is_invalidated_by_sensitive_config_value_change(
    monkeypatch,
    tmp_path: Path,
    original_config: dict,
    changed_config: dict,
) -> None:
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    repo = tmp_path / "repo"
    repo.mkdir()
    env = {"TOKEN_A": "old-token", "TOKEN_B": "new-token"}
    _write_yaml(tmp_path / "config" / "settings.yml", {"mcpServers": {"remote": original_config}})

    disabled = mcp_config.disable_mcp_server("remote", scope=MCPConfigScope.USER, cwd=repo)
    original = load_mcp_configs(cwd=repo, workspace_root=repo, env=env)

    assert disabled.disabled is True
    assert original.by_name()["remote"].disabled is True

    _write_yaml(tmp_path / "config" / "settings.yml", {"mcpServers": {"remote": changed_config}})
    changed = load_mcp_configs(cwd=repo, workspace_root=repo, env=env)

    assert changed.by_name()["remote"].disabled is False


def test_disabled_state_key_distinguishes_persisted_scopes(monkeypatch, tmp_path: Path) -> None:
    config_dir = tmp_path / "config"
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(config_dir))
    _write_yaml(config_dir / "settings.yml", {"mcpServers": {"shared": {"command": "user-cmd"}}})
    _write_yaml(repo / ".iac-code" / "settings.local.yml", {"mcpServers": {"shared": {"command": "local-cmd"}}})

    disabled = mcp_config.disable_mcp_server("shared", scope=MCPConfigScope.USER, cwd=repo)
    local = mcp_config.load_exact_mcp_config("shared", scope=MCPConfigScope.LOCAL, cwd=repo)
    user = mcp_config.load_exact_mcp_config("shared", scope=MCPConfigScope.USER, cwd=repo)

    assert disabled.disabled is True
    assert user.servers[0].disabled is True
    assert local.servers[0].disabled is False


def test_disabled_state_tolerates_malformed_state_file_and_writes_private_file(
    monkeypatch,
    tmp_path: Path,
) -> None:
    config_dir = tmp_path / "config"
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(config_dir))
    _write_yaml(config_dir / "settings.yml", {"mcpServers": {"yuque": {"command": "uvx"}}})
    state_path = config_dir / "mcp" / "server-states.json"
    state_path.parent.mkdir(parents=True)
    state_path.write_text("{not-json", encoding="utf-8")

    disabled = mcp_config.disable_mcp_server("yuque", scope=MCPConfigScope.USER, cwd=repo)
    loaded = load_mcp_configs(cwd=repo, workspace_root=repo, env={})

    assert disabled.disabled is True
    assert loaded.by_name()["yuque"].disabled is True
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["disabled"]
    if os.name != "nt":
        assert stat.S_IMODE(state_path.stat().st_mode) == 0o600
        assert stat.S_IMODE(state_path.parent.stat().st_mode) == 0o700


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
                "bad": {"type": "tcp", "url": "tcp://example.com"},
            }
        },
    )

    result = load_mcp_configs(cwd=tmp_path, workspace_root=tmp_path, env={})

    assert [server.name for server in result.servers] == ["good"]
    assert any(warning.server_name == "bad" and warning.code == "invalid_config" for warning in result.warnings)


def test_malformed_websocket_url_reports_invalid_config_warning(monkeypatch, tmp_path: Path) -> None:
    config_dir = tmp_path / "config"
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(config_dir))
    _write_yaml(
        config_dir / "settings.yml",
        {
            "mcpServers": {
                "good": {"command": "uvx"},
                "bad-ws": {"type": "ws", "url": "ws://[::1/mcp"},
            }
        },
    )

    result = load_mcp_configs(cwd=tmp_path, workspace_root=tmp_path, env={})

    assert [server.name for server in result.servers] == ["good"]
    assert any(warning.server_name == "bad-ws" and warning.code == "invalid_config" for warning in result.warnings)


def test_load_exact_mcp_config_reports_invalid_config_for_malformed_value(monkeypatch, tmp_path: Path) -> None:
    config_dir = tmp_path / "config"
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(config_dir))
    _write_yaml(config_dir / "settings.yml", {"mcpServers": {"bad": "not-an-object"}})

    result = mcp_config.load_exact_mcp_config("bad", scope=MCPConfigScope.USER, cwd=tmp_path)

    assert result.servers == []
    assert result.pending == []
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
        write_mcp_server_config("bad", {"type": "tcp", "url": "tcp://example.com"}, scope=MCPConfigScope.USER, cwd=repo)
    assert "bad" not in _read_yaml(config_dir / "settings.yml")["mcpServers"]

    with pytest.raises(MCPConfigError):
        write_mcp_server_config("bad name", {"command": "uvx"}, scope=MCPConfigScope.USER, cwd=repo)

    with pytest.raises(MCPConfigError):
        write_mcp_server_config("mcp__reserved", {"command": "uvx"}, scope=MCPConfigScope.USER, cwd=repo)


def test_find_persisted_mcp_server_matches_reports_unique_and_ambiguous_scopes(
    monkeypatch,
    tmp_path: Path,
) -> None:
    config_dir = tmp_path / "config"
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(config_dir))
    _write_yaml(
        config_dir / "settings.yml",
        {
            "mcpServers": {
                "remote": {"type": "http", "url": "https://example.com/mcp"},
                "shared": {"command": "user-cmd"},
            }
        },
    )
    project_file = repo / ".mcp.json"
    _write_json(project_file, {"mcpServers": {"project-only": {"command": "project-cmd"}}})
    _write_yaml(repo / ".iac-code" / "settings.local.yml", {"mcpServers": {"shared": {"command": "local-cmd"}}})

    remote = mcp_config.find_persisted_mcp_server_matches("remote", cwd=repo)
    shared = mcp_config.find_persisted_mcp_server_matches("shared", cwd=repo)
    project_only = mcp_config.find_persisted_mcp_server_matches("project-only", cwd=repo)

    assert [(match.scope, match.config["url"]) for match in remote] == [
        (MCPConfigScope.USER, "https://example.com/mcp")
    ]
    assert [match.scope for match in shared] == [MCPConfigScope.LOCAL, MCPConfigScope.USER]
    assert [(match.scope, match.source_path) for match in project_only] == [(MCPConfigScope.PROJECT, project_file)]


def test_find_persisted_mcp_server_matches_uses_nearest_project_file_from_child_cwd(
    monkeypatch,
    tmp_path: Path,
) -> None:
    config_dir = tmp_path / "config"
    repo = tmp_path / "repo"
    nested = repo / "services" / "api"
    nested.mkdir(parents=True)
    (repo / ".git").mkdir()
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(config_dir))
    _write_yaml(config_dir / "settings.yml", {"mcpServers": {"shared": {"command": "user-cmd"}}})
    _write_yaml(repo / ".iac-code" / "settings.local.yml", {"mcpServers": {"shared": {"command": "local-cmd"}}})
    _write_json(repo / "services" / ".mcp.json", {"mcpServers": {"shared": {"command": "child-project-cmd"}}})

    matches = mcp_config.find_persisted_mcp_server_matches("shared", cwd=nested)

    assert [match.scope for match in matches] == [
        MCPConfigScope.LOCAL,
        MCPConfigScope.PROJECT,
        MCPConfigScope.USER,
    ]
    assert matches[1].source_path == repo / "services" / ".mcp.json"
    assert matches[1].config == {"command": "child-project-cmd"}


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


def test_explicit_source_path_must_belong_to_scope(monkeypatch, tmp_path: Path) -> None:
    config_dir = tmp_path / "config"
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(config_dir))
    outside = tmp_path / "outside.yml"
    _write_yaml(outside, {"mcpServers": {"remote": {"type": "http", "url": "https://example.com/mcp"}}})
    before = outside.read_text(encoding="utf-8")

    with pytest.raises(MCPConfigError, match="source path"):
        mcp_config.read_mcp_server_config("remote", scope=MCPConfigScope.USER, cwd=repo, source_path=outside)
    with pytest.raises(MCPConfigError, match="source path"):
        mcp_config.disable_mcp_server("remote", scope=MCPConfigScope.USER, cwd=repo, source_path=outside)
    with pytest.raises(MCPConfigError, match="source path"):
        mcp_config.remove_mcp_server_config("remote", scope=MCPConfigScope.USER, cwd=repo, source_path=outside)

    assert outside.read_text(encoding="utf-8") == before


def test_explicit_project_source_path_allows_nested_workspace_mcp_file(monkeypatch, tmp_path: Path) -> None:
    config_dir = tmp_path / "config"
    repo = tmp_path / "repo"
    service = repo / "services" / "api"
    service.mkdir(parents=True)
    (repo / ".git").mkdir()
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(config_dir))
    project_file = repo / "services" / ".mcp.json"
    _write_json(project_file, {"mcpServers": {"remote": {"command": "uvx"}}})

    config = mcp_config.read_mcp_server_config(
        "remote",
        scope=MCPConfigScope.PROJECT,
        cwd=service,
        source_path=project_file,
    )

    assert config == {"command": "uvx"}


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
            "bad": {"type": "tcp", "url": "tcp://example.com/mcp"},
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
