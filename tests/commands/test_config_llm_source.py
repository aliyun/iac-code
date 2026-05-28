"""Tests for get_llm_source() priority chain."""

import pytest

from iac_code.config import get_llm_source


@pytest.fixture(autouse=True)
def iac_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    (tmp_path / ".iac-code").mkdir()
    return tmp_path


class TestGetLlmSource:
    def test_env_api_key_returns_env(self, monkeypatch, iac_home):
        """Env var takes highest priority."""
        monkeypatch.setenv("IAC_CODE_API_KEY", "sk-test")
        assert get_llm_source() == "env"

    def test_active_provider_returns_local(self, iac_home):
        """activeProvider present -> 'local' regardless of llm_source."""
        settings_path = iac_home / ".iac-code" / "settings.yml"
        settings_path.write_text("llm_source: qwenpaw\nactiveProvider: dashscope\n")
        assert get_llm_source() == "local"

    def test_no_active_provider_with_llm_source_returns_source(self, iac_home):
        """No activeProvider but llm_source set -> return llm_source value."""
        settings_path = iac_home / ".iac-code" / "settings.yml"
        settings_path.write_text("llm_source: qwenpaw\n")
        assert get_llm_source() == "qwenpaw"

    def test_no_active_provider_no_llm_source_returns_local(self, iac_home):
        """Nothing configured -> 'local'."""
        settings_path = iac_home / ".iac-code" / "settings.yml"
        settings_path.write_text("")
        assert get_llm_source() == "local"

    def test_active_provider_empty_string_treated_as_absent(self, iac_home):
        """Empty-string activeProvider is treated as absent."""
        settings_path = iac_home / ".iac-code" / "settings.yml"
        settings_path.write_text("llm_source: qwenpaw\nactiveProvider: ''\n")
        assert get_llm_source() == "qwenpaw"

    def test_env_overrides_active_provider(self, monkeypatch, iac_home):
        """Env var wins even when activeProvider exists."""
        monkeypatch.setenv("IAC_CODE_API_KEY", "sk-test")
        settings_path = iac_home / ".iac-code" / "settings.yml"
        settings_path.write_text("activeProvider: dashscope\n")
        assert get_llm_source() == "env"

    def test_settings_file_missing_returns_local(self, iac_home):
        """No settings file -> 'local'."""
        settings_path = iac_home / ".iac-code" / "settings.yml"
        if settings_path.exists():
            settings_path.unlink()
        assert get_llm_source() == "local"
