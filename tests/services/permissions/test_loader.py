"""Tests for permission settings loader."""

from __future__ import annotations

import pytest
import yaml

from iac_code.services.permissions.loader import load_permission_context, load_settings_permissions
from iac_code.types.permissions import PermissionMode


class TestLoadSettingsPermissions:
    def test_load_from_yaml(self, tmp_path):
        f = tmp_path / "settings.yml"
        f.write_text(
            yaml.dump(
                {
                    "permissions": {
                        "mode": "default",
                        "allow": ["bash(git *)"],
                        "deny": ["bash(rm -rf /)"],
                        "ask": ["bash(docker *)"],
                        "additional_directories": ["/shared"],
                    }
                }
            ),
            encoding="utf-8",
        )
        result = load_settings_permissions(f, "user_settings")
        assert "bash(git *)" in result["allow"]
        assert "bash(rm -rf /)" in result["deny"]
        assert result["mode"] == "default"

    def test_missing_file(self, tmp_path):
        result = load_settings_permissions(tmp_path / "nope.yml", "user_settings")
        assert result["allow"] == []
        assert result["deny"] == []

    def test_no_permissions_section(self, tmp_path):
        f = tmp_path / "settings.yml"
        f.write_text(yaml.dump({"model": "gpt-4"}), encoding="utf-8")
        result = load_settings_permissions(f, "user_settings")
        assert result["allow"] == []


class TestLoadPermissionContext:
    def test_basic_load(self, tmp_path, monkeypatch):
        global_settings = tmp_path / ".iac-code" / "settings.yml"
        global_settings.parent.mkdir(parents=True)
        global_settings.write_text(yaml.dump({"permissions": {"allow": ["bash(git *)"]}}), encoding="utf-8")
        monkeypatch.setattr("iac_code.services.permissions.loader._get_global_settings_path", lambda: global_settings)
        ctx = load_permission_context(str(tmp_path))
        assert ctx.mode == PermissionMode.DEFAULT
        assert "bash(git *)" in ctx.allow_rules.get("user_settings", [])

    def test_cli_overrides(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "iac_code.services.permissions.loader._get_global_settings_path", lambda: tmp_path / "nonexistent.yml"
        )
        ctx = load_permission_context(
            str(tmp_path),
            cli_allowed=["bash(npm test)"],
            cli_disallowed=["bash(rm *)"],
            cli_mode="bypass_permissions",
        )
        assert "bash(npm test)" in ctx.allow_rules.get("cli_arg", [])
        assert "bash(rm *)" in ctx.deny_rules.get("cli_arg", [])
        assert ctx.mode == PermissionMode.BYPASS_PERMISSIONS

    def test_parse_cli_permission_mode_rejects_invalid_value(self):
        from iac_code.services.permissions.loader import parse_cli_permission_mode

        with pytest.raises(ValueError, match="Invalid --permission-mode 'nonsense'"):
            parse_cli_permission_mode("nonsense")

    def test_parse_cli_permission_mode_error_is_translatable(self, monkeypatch):
        import iac_code.services.permissions.loader as loader

        monkeypatch.setattr(loader, "_", lambda msg: f"TRANSLATED:{msg}", raising=False)

        with pytest.raises(ValueError) as exc:
            loader.parse_cli_permission_mode("nonsense")

        assert str(exc.value).startswith("TRANSLATED:Invalid --permission-mode")

    def test_load_permission_context_rejects_invalid_cli_mode(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "iac_code.services.permissions.loader._get_global_settings_path", lambda: tmp_path / "nonexistent.yml"
        )

        with pytest.raises(ValueError, match="Invalid --permission-mode 'nonsense'"):
            load_permission_context(str(tmp_path), cli_mode="nonsense")

    def test_project_settings_merge(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "iac_code.services.permissions.loader._get_global_settings_path", lambda: tmp_path / "nonexistent.yml"
        )
        project_dir = tmp_path / ".iac-code"
        project_dir.mkdir()
        (project_dir / "settings.yml").write_text(
            yaml.dump({"permissions": {"deny": ["bash(curl *)"]}}), encoding="utf-8"
        )
        ctx = load_permission_context(str(tmp_path))
        assert "bash(curl *)" in ctx.deny_rules.get("project_settings", [])


def test_load_permission_context_initializes_trusted_read_directories(tmp_path, monkeypatch):
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))

    from iac_code.services.permissions.loader import load_permission_context

    ctx = load_permission_context(str(tmp_path))

    assert ctx.trusted_read_directories == []
