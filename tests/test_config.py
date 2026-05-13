"""Tests for config.py - YAML-based configuration."""

from __future__ import annotations

from unittest.mock import patch


class TestConfigPaths:
    """Test that config paths use YAML naming convention."""

    def test_get_credentials_path_uses_yaml(self, tmp_path):
        """Credentials path should end with .credentials.yml"""
        with patch("iac_code.config.Path.home", return_value=tmp_path):
            from iac_code.config import get_credentials_path

            path = get_credentials_path()
        assert path.name == ".credentials.yml"

    def test_get_settings_path_uses_yaml(self, tmp_path):
        """Settings path should end with settings.yml (no dot prefix)."""
        with patch("iac_code.config.Path.home", return_value=tmp_path):
            from iac_code.config import get_settings_path

            path = get_settings_path()
        assert path.name == "settings.yml"

    def test_get_history_path(self, tmp_path):
        """History path should be .input_history (dot prefix)."""
        with patch("iac_code.config.Path.home", return_value=tmp_path):
            from iac_code.config import get_history_path

            path = get_history_path()
        assert path.name == ".input_history"

    def test_credentials_path_is_in_config_dir(self, tmp_path):
        """Credentials path should be inside ~/.iac-code/."""
        with patch("iac_code.config.Path.home", return_value=tmp_path):
            from iac_code.config import get_config_dir, get_credentials_path

            path = get_credentials_path()
            config_dir = get_config_dir()
        assert path.parent == config_dir

    def test_settings_path_is_in_config_dir(self, tmp_path):
        """Settings path should be inside ~/.iac-code/."""
        with patch("iac_code.config.Path.home", return_value=tmp_path):
            from iac_code.config import get_config_dir, get_settings_path

            path = get_settings_path()
            config_dir = get_config_dir()
        assert path.parent == config_dir

    def test_history_path_is_in_config_dir(self, tmp_path):
        """History path should be inside ~/.iac-code/."""
        with patch("iac_code.config.Path.home", return_value=tmp_path):
            from iac_code.config import get_config_dir, get_history_path

            path = get_history_path()
            config_dir = get_config_dir()
        assert path.parent == config_dir


class TestLoadSavedModel:
    """Test load_saved_model reads from YAML settings."""

    def test_returns_none_when_no_file(self, tmp_path):
        """Returns None if settings.yml does not exist."""
        with patch("iac_code.config.Path.home", return_value=tmp_path):
            from iac_code.config import load_saved_model

            result = load_saved_model()
        assert result is None

    def test_returns_model_from_yaml(self, tmp_path):
        """Returns model name from active provider's per-provider entry."""
        import yaml

        config_dir = tmp_path / ".iac-code"
        config_dir.mkdir()
        settings = config_dir / "settings.yml"
        settings.write_text(
            yaml.dump(
                {
                    "activeProvider": "bailian",
                    "providers": {"bailian": {"model": "qwen3-max"}},
                }
            )
        )

        with patch("iac_code.config.Path.home", return_value=tmp_path):
            from iac_code.config import load_saved_model

            result = load_saved_model()
        assert result == "qwen3-max"

    def test_returns_none_when_no_active_provider(self, tmp_path):
        """Returns None if activeProvider is missing."""
        import yaml

        config_dir = tmp_path / ".iac-code"
        config_dir.mkdir()
        settings = config_dir / "settings.yml"
        settings.write_text(yaml.dump({"otherKey": "value"}))

        with patch("iac_code.config.Path.home", return_value=tmp_path):
            from iac_code.config import load_saved_model

            result = load_saved_model()
        assert result is None

    def test_returns_none_on_corrupt_file(self, tmp_path):
        """Returns None if the YAML file is malformed."""
        config_dir = tmp_path / ".iac-code"
        config_dir.mkdir()
        settings = config_dir / "settings.yml"
        settings.write_text("not: valid: yaml: [")

        with patch("iac_code.config.Path.home", return_value=tmp_path):
            from iac_code.config import load_saved_model

            result = load_saved_model()
        assert result is None


class TestLoadSavedEffort:
    """Test load_saved_effort reads effort level from YAML settings."""

    def test_returns_none_when_no_file(self, tmp_path):
        """Returns None if settings.yml does not exist."""
        with patch("iac_code.config.Path.home", return_value=tmp_path):
            from iac_code.config import load_saved_effort

            result = load_saved_effort()
        assert result is None

    def test_returns_effort_from_yaml(self, tmp_path):
        """Returns effort value from settings.yml."""
        import yaml

        config_dir = tmp_path / ".iac-code"
        config_dir.mkdir()
        settings = config_dir / "settings.yml"
        settings.write_text(yaml.dump({"effort": "high"}))

        with patch("iac_code.config.Path.home", return_value=tmp_path):
            from iac_code.config import load_saved_effort

            result = load_saved_effort()
        assert result == "high"

    def test_returns_none_when_no_effort_key(self, tmp_path):
        """Returns None if effort key is missing."""
        import yaml

        config_dir = tmp_path / ".iac-code"
        config_dir.mkdir()
        settings = config_dir / "settings.yml"
        settings.write_text(
            yaml.dump(
                {
                    "activeProvider": "bailian",
                    "providers": {"bailian": {"model": "qwen3-max"}},
                }
            )
        )

        with patch("iac_code.config.Path.home", return_value=tmp_path):
            from iac_code.config import load_saved_effort

            result = load_saved_effort()
        assert result is None

    def test_returns_none_on_corrupt_file(self, tmp_path):
        """Returns None if the YAML file is malformed."""
        config_dir = tmp_path / ".iac-code"
        config_dir.mkdir()
        settings = config_dir / "settings.yml"
        settings.write_text("not: valid: yaml: [")

        with patch("iac_code.config.Path.home", return_value=tmp_path):
            from iac_code.config import load_saved_effort

            result = load_saved_effort()
        assert result is None


class TestYamlHelpers:
    """Test _load_yaml and _save_yaml internal helpers."""

    def test_load_yaml_returns_empty_dict_when_file_missing(self, tmp_path):
        """_load_yaml returns {} when path does not exist."""
        from iac_code.config import _load_yaml

        result = _load_yaml(tmp_path / "nonexistent.yml")
        assert result == {}

    def test_load_yaml_reads_existing_file(self, tmp_path):
        """_load_yaml reads and returns YAML content."""
        import yaml

        path = tmp_path / "test.yml"
        path.write_text(yaml.dump({"key": "value", "num": 42}))

        from iac_code.config import _load_yaml

        result = _load_yaml(path)
        assert result == {"key": "value", "num": 42}

    def test_save_yaml_writes_file(self, tmp_path):
        """_save_yaml writes data to a YAML file."""
        import yaml

        path = tmp_path / "test.yml"

        from iac_code.config import _save_yaml

        _save_yaml(path, {"key": "value", "num": 42})

        assert path.exists()
        content = yaml.safe_load(path.read_text())
        assert content == {"key": "value", "num": 42}

    def test_save_yaml_creates_parent_dirs(self, tmp_path):
        """_save_yaml creates parent directories if they don't exist."""
        path = tmp_path / "subdir" / "nested" / "test.yml"

        from iac_code.config import _save_yaml

        _save_yaml(path, {"key": "value"})

        assert path.exists()
