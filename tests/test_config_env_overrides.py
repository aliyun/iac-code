"""Tests for IAC_CODE_* environment variable overrides in config.py."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _reset_warn_state():
    """Reset module-level warn flag between tests so warning behavior is deterministic."""
    import iac_code.config as cfg

    cfg._warned_base_url_ignored = False
    yield
    cfg._warned_base_url_ignored = False


class TestGetEnvOverrides:
    def test_all_unset_returns_none_values(self, monkeypatch):
        for name in ("IAC_CODE_PROVIDER", "IAC_CODE_MODEL", "IAC_CODE_BASE_URL", "IAC_CODE_API_KEY"):
            monkeypatch.delenv(name, raising=False)
        from iac_code.config import _get_env_overrides

        result = _get_env_overrides()
        assert result == {"provider_key": None, "model": None, "api_base": None, "api_key": None}

    def test_empty_strings_treated_as_unset(self, monkeypatch):
        for name in ("IAC_CODE_PROVIDER", "IAC_CODE_MODEL", "IAC_CODE_BASE_URL", "IAC_CODE_API_KEY"):
            monkeypatch.setenv(name, "")
        from iac_code.config import _get_env_overrides

        result = _get_env_overrides()
        assert result == {"provider_key": None, "model": None, "api_base": None, "api_key": None}

    def test_whitespace_only_treated_as_unset(self, monkeypatch):
        monkeypatch.setenv("IAC_CODE_MODEL", "   ")
        monkeypatch.setenv("IAC_CODE_API_KEY", "\t\n")
        from iac_code.config import _get_env_overrides

        result = _get_env_overrides()
        assert result["model"] is None
        assert result["api_key"] is None

    def test_provider_pascal_case_maps_to_key_name(self, monkeypatch):
        monkeypatch.setenv("IAC_CODE_PROVIDER", "OpenAI")
        from iac_code.config import _get_env_overrides

        assert _get_env_overrides()["provider_key"] == "openai"

    def test_provider_lowercase_accepted(self, monkeypatch):
        monkeypatch.setenv("IAC_CODE_PROVIDER", "openai")
        from iac_code.config import _get_env_overrides

        assert _get_env_overrides()["provider_key"] == "openai"

    def test_provider_uppercase_accepted(self, monkeypatch):
        monkeypatch.setenv("IAC_CODE_PROVIDER", "DASHSCOPE")
        from iac_code.config import _get_env_overrides

        assert _get_env_overrides()["provider_key"] == "dashscope"

    def test_provider_mixed_case_accepted(self, monkeypatch):
        monkeypatch.setenv("IAC_CODE_PROVIDER", "oPeNaPiCoMpAtIbLe")
        from iac_code.config import _get_env_overrides

        assert _get_env_overrides()["provider_key"] == "openapi_compatible"

    def test_provider_with_surrounding_whitespace_accepted(self, monkeypatch):
        monkeypatch.setenv("IAC_CODE_PROVIDER", "  DashScope  ")
        from iac_code.config import _get_env_overrides

        assert _get_env_overrides()["provider_key"] == "dashscope"

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("OpenAPI Compatible", "openapi_compatible"),
            ("openapi-compatible", "openapi_compatible"),
            ("openapi_compatible", "openapi_compatible"),
            ("oPeNaPi CoMpAtIbLe", "openapi_compatible"),
            ("DashScope Token Plan", "dashscope_token_plan"),
            ("dashscope-token-plan", "dashscope_token_plan"),
            ("dashscope_token_plan", "dashscope_token_plan"),
        ],
    )
    def test_provider_env_accepts_normalized_display_names_and_keys(self, monkeypatch, value, expected):
        monkeypatch.setenv("IAC_CODE_PROVIDER", value)
        from iac_code.config import _get_env_overrides

        assert _get_env_overrides()["provider_key"] == expected

    def test_provider_name_lookup_rejects_colliding_normalized_aliases(self, monkeypatch):
        from types import SimpleNamespace

        import iac_code.providers.registry as registry
        from iac_code.config import _build_provider_name_to_key

        monkeypatch.setattr(
            registry,
            "PROVIDER_REGISTRY",
            {
                "foo_bar": SimpleNamespace(key="foo_bar", name="Foo Bar"),
                "foobar": SimpleNamespace(key="foobar", name="Foobar"),
            },
        )

        with pytest.raises(ValueError, match="Ambiguous provider alias"):
            _build_provider_name_to_key()

    def test_provider_invalid_raises_with_canonical_names(self, monkeypatch):
        monkeypatch.setenv("IAC_CODE_PROVIDER", "Unknown")
        from iac_code.config import _get_env_overrides

        with pytest.raises(ValueError) as exc:
            _get_env_overrides()
        msg = str(exc.value)
        for canonical in ("Anthropic", "OpenAI", "DashScope", "DeepSeek"):
            assert canonical in msg

    def test_provider_invalid_error_is_translatable(self, monkeypatch):
        import iac_code.config as config

        monkeypatch.setenv("IAC_CODE_PROVIDER", "Unknown")
        monkeypatch.setattr(config, "_", lambda msg: f"TRANSLATED:{msg}", raising=False)

        with pytest.raises(ValueError) as exc:
            config._get_env_overrides()

        assert str(exc.value).startswith("TRANSLATED:Invalid IAC_CODE_PROVIDER value")

    def test_all_four_env_vars_returned(self, monkeypatch):
        monkeypatch.setenv("IAC_CODE_PROVIDER", "DeepSeek")
        monkeypatch.setenv("IAC_CODE_MODEL", "deepseek-v4-pro")
        monkeypatch.setenv("IAC_CODE_BASE_URL", "https://example.com/v1")
        monkeypatch.setenv("IAC_CODE_API_KEY", "sk-xxx")
        from iac_code.config import _get_env_overrides

        assert _get_env_overrides() == {
            "provider_key": "deepseek",
            "model": "deepseek-v4-pro",
            "api_base": "https://example.com/v1",
            "api_key": "sk-xxx",
        }


class TestGetActiveProviderKeyEnv:
    def test_env_overrides_settings(self, monkeypatch, tmp_path):
        from unittest.mock import patch

        config_dir = tmp_path / ".iac-code"
        config_dir.mkdir()
        (config_dir / "settings.yml").write_text("activeProvider: openai\n", encoding="utf-8")

        monkeypatch.setenv("IAC_CODE_PROVIDER", "Anthropic")
        with patch("iac_code.config.Path.home", return_value=tmp_path):
            from iac_code.config import get_active_provider_key

            assert get_active_provider_key() == "anthropic"

    def test_falls_back_to_settings_when_env_unset(self, monkeypatch, tmp_path):
        from unittest.mock import patch

        monkeypatch.delenv("IAC_CODE_PROVIDER", raising=False)
        config_dir = tmp_path / ".iac-code"
        config_dir.mkdir()
        (config_dir / "settings.yml").write_text("activeProvider: openai\n", encoding="utf-8")

        with patch("iac_code.config.Path.home", return_value=tmp_path):
            from iac_code.config import get_active_provider_key

            assert get_active_provider_key() == "openai"

    def test_env_works_without_settings_file(self, monkeypatch, tmp_path):
        from unittest.mock import patch

        monkeypatch.setenv("IAC_CODE_PROVIDER", "DashScope")
        with patch("iac_code.config.Path.home", return_value=tmp_path):
            from iac_code.config import get_active_provider_key

            assert get_active_provider_key() == "dashscope"


class TestGetProviderConfigEnv:
    def _write_settings(self, tmp_path, content: str) -> None:
        config_dir = tmp_path / ".iac-code"
        config_dir.mkdir(exist_ok=True)
        (config_dir / "settings.yml").write_text(content, encoding="utf-8")

    def test_model_env_overlays_active_provider(self, monkeypatch, tmp_path):
        from unittest.mock import patch

        self._write_settings(
            tmp_path,
            "activeProvider: openai\nproviders:\n  openai:\n    model: gpt-5.4\n",
        )
        monkeypatch.setenv("IAC_CODE_MODEL", "gpt-5.5")
        with patch("iac_code.config.Path.home", return_value=tmp_path):
            from iac_code.config import get_provider_config

            assert get_provider_config("openai")["model"] == "gpt-5.5"

    def test_model_env_does_not_leak_to_other_providers(self, monkeypatch, tmp_path):
        from unittest.mock import patch

        self._write_settings(
            tmp_path,
            (
                "activeProvider: openai\n"
                "providers:\n"
                "  openai:\n    model: gpt-5.4\n"
                "  bailian:\n    model: qwen3.6-plus\n"
            ),
        )
        monkeypatch.setenv("IAC_CODE_MODEL", "gpt-5.5")
        with patch("iac_code.config.Path.home", return_value=tmp_path):
            from iac_code.config import get_provider_config

            assert get_provider_config("bailian")["model"] == "qwen3.6-plus"

    def test_base_url_env_overlays_when_active_is_openapi_compatible(self, monkeypatch, tmp_path):
        from unittest.mock import patch

        self._write_settings(
            tmp_path,
            ("activeProvider: openapi_compatible\nproviders:\n  openapi_compatible:\n    apiBase: https://old/v1\n"),
        )
        monkeypatch.setenv("IAC_CODE_BASE_URL", "https://new/v1")
        with patch("iac_code.config.Path.home", return_value=tmp_path):
            from iac_code.config import get_provider_config

            assert get_provider_config("openapi_compatible")["apiBase"] == "https://new/v1"

    def test_base_url_env_ignored_when_active_is_not_openapi_compatible(self, monkeypatch, tmp_path, caplog):
        import logging
        from unittest.mock import patch

        self._write_settings(
            tmp_path,
            "activeProvider: openai\nproviders:\n  openai:\n    model: gpt-5.4\n",
        )
        monkeypatch.setenv("IAC_CODE_BASE_URL", "https://example/v1")
        with patch("iac_code.config.Path.home", return_value=tmp_path):
            from iac_code.config import get_provider_config

            with caplog.at_level(logging.WARNING):
                cfg = get_provider_config("openai")
        assert "apiBase" not in cfg or cfg.get("apiBase") != "https://example/v1"

    def test_env_overlay_does_not_mutate_unset_fields(self, monkeypatch, tmp_path):
        from unittest.mock import patch

        self._write_settings(
            tmp_path,
            ("activeProvider: openai\nproviders:\n  openai:\n    model: gpt-5.4\n    effort: high\n"),
        )
        monkeypatch.delenv("IAC_CODE_MODEL", raising=False)
        with patch("iac_code.config.Path.home", return_value=tmp_path):
            from iac_code.config import get_provider_config

            cfg = get_provider_config("openai")
            assert cfg["model"] == "gpt-5.4"
            assert cfg["effort"] == "high"

    def test_load_saved_model_picks_up_env(self, monkeypatch, tmp_path):
        from unittest.mock import patch

        self._write_settings(
            tmp_path,
            "activeProvider: openai\nproviders:\n  openai:\n    model: gpt-5.4\n",
        )
        monkeypatch.setenv("IAC_CODE_MODEL", "gpt-5.5")
        with patch("iac_code.config.Path.home", return_value=tmp_path):
            from iac_code.config import load_saved_model

            assert load_saved_model() == "gpt-5.5"


class TestLoadCredentials:
    def _write(self, tmp_path, settings: str = "", creds: str = "") -> None:
        config_dir = tmp_path / ".iac-code"
        config_dir.mkdir(exist_ok=True)
        if settings:
            (config_dir / "settings.yml").write_text(settings, encoding="utf-8")
        if creds:
            (config_dir / ".credentials.yml").write_text(creds, encoding="utf-8")

    def test_returns_empty_when_no_files(self, monkeypatch, tmp_path):
        from unittest.mock import patch

        for n in ("IAC_CODE_PROVIDER", "IAC_CODE_API_KEY"):
            monkeypatch.delenv(n, raising=False)
        with patch("iac_code.config.Path.home", return_value=tmp_path):
            from iac_code.config import load_credentials

            creds = load_credentials()
            for key in ("anthropic", "openai", "dashscope", "dashscope_token_plan", "deepseek", "openapi_compatible"):
                assert creds[key] == ""
            assert all(v == "" for v in creds.values())

    def test_loads_all_slots_from_file(self, monkeypatch, tmp_path):
        from unittest.mock import patch

        for n in ("IAC_CODE_PROVIDER", "IAC_CODE_API_KEY"):
            monkeypatch.delenv(n, raising=False)
        self._write(
            tmp_path,
            creds=(
                "anthropic: ak\nopenai: ok\ndashscope: ds\n"
                "dashscope_token_plan: tp\ndeepseek: dk\nopenapi_compatible: oc\n"
            ),
        )
        with patch("iac_code.config.Path.home", return_value=tmp_path):
            from iac_code.config import load_credentials

            creds = load_credentials()
            assert creds["anthropic"] == "ak"
            assert creds["openai"] == "ok"
            assert creds["dashscope"] == "ds"
            assert creds["dashscope_token_plan"] == "tp"
            assert creds["deepseek"] == "dk"
            assert creds["openapi_compatible"] == "oc"

    def test_bailian_legacy_key_falls_back_to_dashscope_slot(self, monkeypatch, tmp_path):
        from unittest.mock import patch

        for n in ("IAC_CODE_PROVIDER", "IAC_CODE_API_KEY"):
            monkeypatch.delenv(n, raising=False)
        self._write(tmp_path, creds="bailian: legacy\n")
        with patch("iac_code.config.Path.home", return_value=tmp_path):
            from iac_code.config import load_credentials

            assert load_credentials()["dashscope"] == "legacy"

    def test_dashscope_key_takes_precedence_over_bailian_legacy(self, monkeypatch, tmp_path):
        from unittest.mock import patch

        for n in ("IAC_CODE_PROVIDER", "IAC_CODE_API_KEY"):
            monkeypatch.delenv(n, raising=False)
        self._write(tmp_path, creds="dashscope: new\nbailian: legacy\n")
        with patch("iac_code.config.Path.home", return_value=tmp_path):
            from iac_code.config import load_credentials

            assert load_credentials()["dashscope"] == "new"

    def test_api_key_env_overrides_active_slot(self, monkeypatch, tmp_path):
        from unittest.mock import patch

        self._write(
            tmp_path,
            settings="activeProvider: openai\n",
            creds="openai: file_key\n",
        )
        monkeypatch.setenv("IAC_CODE_API_KEY", "env_key")
        with patch("iac_code.config.Path.home", return_value=tmp_path):
            from iac_code.config import load_credentials

            assert load_credentials()["openai"] == "env_key"

    def test_api_key_env_routed_to_dashscope_slot_when_active_is_bailian(self, monkeypatch, tmp_path):
        from unittest.mock import patch

        self._write(tmp_path, settings="activeProvider: bailian\n", creds="dashscope: file_ds\n")
        monkeypatch.setenv("IAC_CODE_API_KEY", "env_ds")
        with patch("iac_code.config.Path.home", return_value=tmp_path):
            from iac_code.config import load_credentials

            assert load_credentials()["dashscope"] == "env_ds"

    def test_api_key_env_routed_via_provider_env(self, monkeypatch, tmp_path):
        from unittest.mock import patch

        self._write(tmp_path, settings="", creds="")
        monkeypatch.setenv("IAC_CODE_PROVIDER", "Anthropic")
        monkeypatch.setenv("IAC_CODE_API_KEY", "env_anth")
        with patch("iac_code.config.Path.home", return_value=tmp_path):
            from iac_code.config import load_credentials

            assert load_credentials()["anthropic"] == "env_anth"

    def test_api_key_env_does_not_leak_to_other_slots(self, monkeypatch, tmp_path):
        from unittest.mock import patch

        self._write(
            tmp_path,
            settings="activeProvider: openai\n",
            creds="openai: ok\nanthropic: ak\n",
        )
        monkeypatch.setenv("IAC_CODE_API_KEY", "env_only_for_openai")
        with patch("iac_code.config.Path.home", return_value=tmp_path):
            from iac_code.config import load_credentials

            creds = load_credentials()
            assert creds["openai"] == "env_only_for_openai"
            assert creds["anthropic"] == "ak"

    def test_api_key_env_without_active_provider_is_ignored_when_no_model(self, monkeypatch, tmp_path):
        from unittest.mock import patch

        for n in ("IAC_CODE_PROVIDER",):
            monkeypatch.delenv(n, raising=False)
        self._write(tmp_path, settings="", creds="openai: ok\n")
        monkeypatch.setenv("IAC_CODE_API_KEY", "env_orphan")
        with patch("iac_code.config.Path.home", return_value=tmp_path):
            from iac_code.config import load_credentials

            creds = load_credentials()
            assert creds["openai"] == "ok"
            assert "env_orphan" not in creds.values()

    def test_api_key_env_routed_via_model_prefix_when_no_provider(self, monkeypatch, tmp_path):
        from unittest.mock import patch

        for n in ("IAC_CODE_PROVIDER",):
            monkeypatch.delenv(n, raising=False)
        self._write(tmp_path, settings="", creds="")
        monkeypatch.setenv("IAC_CODE_API_KEY", "sk-from-env")
        with patch("iac_code.config.Path.home", return_value=tmp_path):
            from iac_code.config import load_credentials

            creds = load_credentials(model="claude-sonnet-4-6")
            assert creds["anthropic"] == "sk-from-env"

    def test_api_key_env_model_prefix_does_not_override_explicit_provider(self, monkeypatch, tmp_path):
        from unittest.mock import patch

        self._write(tmp_path, settings="activeProvider: openai\n", creds="")
        monkeypatch.setenv("IAC_CODE_API_KEY", "sk-from-env")
        with patch("iac_code.config.Path.home", return_value=tmp_path):
            from iac_code.config import load_credentials

            creds = load_credentials(model="claude-sonnet-4-6")
            assert creds["openai"] == "sk-from-env"
            assert creds["anthropic"] == ""


class TestDashScopeTokenPlanProviderEnv:
    def test_canonical_name_recognized(self, monkeypatch):
        monkeypatch.setenv("IAC_CODE_PROVIDER", "DashScopeTokenPlan")
        from iac_code.config import _get_env_overrides

        assert _get_env_overrides()["provider_key"] == "dashscope_token_plan"

    def test_canonical_name_case_insensitive(self, monkeypatch):
        monkeypatch.setenv("IAC_CODE_PROVIDER", "dashscopetokenplan")
        from iac_code.config import _get_env_overrides

        assert _get_env_overrides()["provider_key"] == "dashscope_token_plan"

    def test_canonical_name_listed_in_constants(self):
        from iac_code.config import _PROVIDER_CANONICAL_NAMES

        assert "DashScope Token Plan" in _PROVIDER_CANONICAL_NAMES


class TestDashScopeTokenPlanCredSlot:
    @staticmethod
    def _write(tmp_path, settings: str = "", creds: str = ""):
        cfg_dir = tmp_path / ".iac-code"
        cfg_dir.mkdir()
        (cfg_dir / "settings.yml").write_text(settings, encoding="utf-8")
        (cfg_dir / ".credentials.yml").write_text(creds, encoding="utf-8")

    def test_credentials_includes_token_plan_slot(self, monkeypatch, tmp_path):
        from unittest.mock import patch

        for n in ("IAC_CODE_PROVIDER", "IAC_CODE_API_KEY"):
            monkeypatch.delenv(n, raising=False)
        self._write(tmp_path, creds="dashscope_token_plan: tp-key\n")
        with patch("iac_code.config.Path.home", return_value=tmp_path):
            from iac_code.config import load_credentials

            creds = load_credentials()
            assert creds["dashscope_token_plan"] == "tp-key"
            assert creds["dashscope"] == ""

    def test_token_plan_and_dashscope_keys_independent(self, monkeypatch, tmp_path):
        from unittest.mock import patch

        for n in ("IAC_CODE_PROVIDER", "IAC_CODE_API_KEY"):
            monkeypatch.delenv(n, raising=False)
        self._write(
            tmp_path,
            creds="dashscope: ds-key\ndashscope_token_plan: tp-key\n",
        )
        with patch("iac_code.config.Path.home", return_value=tmp_path):
            from iac_code.config import load_credentials

            creds = load_credentials()
            assert creds["dashscope"] == "ds-key"
            assert creds["dashscope_token_plan"] == "tp-key"

    def test_api_key_env_routed_to_token_plan_slot(self, monkeypatch, tmp_path):
        from unittest.mock import patch

        self._write(
            tmp_path,
            settings="activeProvider: dashscope_token_plan\n",
            creds="dashscope_token_plan: file-key\n",
        )
        monkeypatch.setenv("IAC_CODE_API_KEY", "env-key")
        with patch("iac_code.config.Path.home", return_value=tmp_path):
            from iac_code.config import load_credentials

            assert load_credentials()["dashscope_token_plan"] == "env-key"
