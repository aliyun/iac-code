"""Tests for IAC_CODE_CONFIG_DIR env-var override of get_config_dir()."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest


class TestResolveConfigDirFallback:
    def test_unset_falls_back_to_home_dot_iac_code(self, monkeypatch, tmp_path):
        monkeypatch.delenv("IAC_CODE_CONFIG_DIR", raising=False)
        with patch("iac_code.config.Path.home", return_value=tmp_path):
            from iac_code.config import get_config_dir

            result = get_config_dir()
            assert result == tmp_path / ".iac-code"
            assert result.is_dir()

    def test_empty_string_treated_as_unset(self, monkeypatch, tmp_path):
        monkeypatch.setenv("IAC_CODE_CONFIG_DIR", "")
        with patch("iac_code.config.Path.home", return_value=tmp_path):
            from iac_code.config import get_config_dir

            assert get_config_dir() == tmp_path / ".iac-code"

    def test_whitespace_only_treated_as_unset(self, monkeypatch, tmp_path):
        monkeypatch.setenv("IAC_CODE_CONFIG_DIR", "   \t\n")
        with patch("iac_code.config.Path.home", return_value=tmp_path):
            from iac_code.config import get_config_dir

            assert get_config_dir() == tmp_path / ".iac-code"

    def test_absolute_path_used_as_is(self, monkeypatch, tmp_path):
        target = tmp_path / "custom-config"
        monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(target))
        from iac_code.config import get_config_dir

        result = get_config_dir()
        assert result == target.resolve()
        assert result.is_dir()

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX modes are not meaningful on Windows")
    def test_get_config_dir_is_owner_only(self, monkeypatch, tmp_path):
        target = tmp_path / "custom-config"
        monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(target))
        from iac_code.config import get_config_dir

        result = get_config_dir()

        assert oct(result.stat().st_mode & 0o777) == "0o700"


class TestResolveConfigDirExpansion:
    def test_tilde_expansion(self, monkeypatch, tmp_path):
        # Pretend $HOME is tmp_path so ~/work/iac resolves into our sandbox.
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("USERPROFILE", str(tmp_path))
        monkeypatch.setenv("IAC_CODE_CONFIG_DIR", "~/work/iac")
        from iac_code.config import get_config_dir

        result = get_config_dir()
        assert result == (tmp_path / "work" / "iac").resolve()
        assert result.is_dir()

    def test_env_var_expansion(self, monkeypatch, tmp_path):
        base = tmp_path / "base-from-var"
        monkeypatch.setenv("MY_BASE", str(base))
        monkeypatch.setenv("IAC_CODE_CONFIG_DIR", "$MY_BASE/iac")
        from iac_code.config import get_config_dir

        result = get_config_dir()
        assert result == (base / "iac").resolve()
        assert result.is_dir()

    def test_relative_path_resolved_against_cwd(self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("IAC_CODE_CONFIG_DIR", "./rel-iac")
        from iac_code.config import get_config_dir

        result = get_config_dir()
        assert result == (tmp_path / "rel-iac").resolve()
        assert result.is_dir()

    def test_creates_parent_dirs(self, monkeypatch, tmp_path):
        target = tmp_path / "a" / "b" / "c"
        monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(target))
        from iac_code.config import get_config_dir

        result = get_config_dir()
        assert result == target.resolve()
        assert result.is_dir()
        assert (tmp_path / "a" / "b").is_dir()


class TestResolveConfigDirEdgeCases:
    def test_mkdir_failure_propagates(self, monkeypatch, tmp_path):
        # Point at a path that we then make impossible to create by
        # replacing Path.mkdir with a stub that raises.
        target = tmp_path / "no-permission"
        monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(target))

        original_mkdir = Path.mkdir

        def boom(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            if str(self).startswith(str(target)):
                raise PermissionError("simulated")
            return original_mkdir(self, *args, **kwargs)

        monkeypatch.setattr(Path, "mkdir", boom)

        import pytest

        from iac_code.config import get_config_dir

        with pytest.raises(PermissionError):
            get_config_dir()

    def test_env_changes_picked_up_immediately(self, monkeypatch, tmp_path):
        first = tmp_path / "first"
        second = tmp_path / "second"
        monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(first))
        from iac_code.config import get_config_dir

        assert get_config_dir() == first.resolve()

        monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(second))
        assert get_config_dir() == second.resolve()


class TestSubpathsFollowEnv:
    def test_root_file_paths_follow_env(self, monkeypatch, tmp_path):
        target = tmp_path / "custom"
        monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(target))

        from iac_code.config import (
            get_cloud_credentials_path,
            get_credentials_path,
            get_history_path,
            get_settings_path,
        )

        assert get_credentials_path() == target.resolve() / ".credentials.yml"
        assert get_settings_path() == target.resolve() / "settings.yml"
        assert get_cloud_credentials_path() == target.resolve() / ".cloud-credentials.yml"
        assert get_history_path() == target.resolve() / ".input_history"

    def test_projects_dir_follows_env(self, monkeypatch, tmp_path):
        target = tmp_path / "custom"
        monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(target))

        from iac_code.utils.project_paths import get_projects_dir

        assert get_projects_dir() == target.resolve() / "projects"

    def test_image_cache_dir_follows_env(self, monkeypatch, tmp_path):
        target = tmp_path / "custom"
        monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(target))

        from iac_code.utils.image.store import _get_base_dir

        assert _get_base_dir() == target.resolve() / "image-cache"


class TestUserSkillsFollowEnv:
    def test_user_global_skills_loaded_from_env_dir(self, monkeypatch, tmp_path):
        target = tmp_path / "custom-config"
        monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(target))

        # Place a skill under the configured user-global skills dir.
        user_skills = target / "skills"
        skill_dir = user_skills / "my-skill"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text("---\ndescription: Sample skill\n---\nBody.\n", encoding="utf-8")

        # Use a cwd with no project skills so the only USER skill found
        # must come from <env>/skills/.
        project_cwd = tmp_path / "empty-project"
        project_cwd.mkdir()

        from iac_code.skills.discovery import discover_all_skills
        from iac_code.types.skill_source import SkillSource

        skills = discover_all_skills(str(project_cwd))
        names = {s.name: s for s in skills if s.source == SkillSource.USER}
        assert "my-skill" in names
        assert names["my-skill"].description == "Sample skill"
