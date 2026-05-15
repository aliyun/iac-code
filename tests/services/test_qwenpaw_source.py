"""Tests for QwenPaw configuration source."""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from iac_code.services.qwenpaw_source import (
    QwenPawError,
    _decrypt_api_key,
    _map_qwenpaw_provider_id,
    _read_active_model,
    _read_provider_config,
    _resolve_secret_dir,
    load_from_qwenpaw,
)


def _clear_env(monkeypatch):
    monkeypatch.delenv("QWENPAW_SECRET_DIR", raising=False)
    monkeypatch.delenv("COPAW_SECRET_DIR", raising=False)


class TestResolveSecretDir:
    def test_env_var_takes_precedence(self, tmp_path, monkeypatch):
        secret = tmp_path / "my_secret"
        secret.mkdir()
        monkeypatch.setenv("QWENPAW_SECRET_DIR", str(secret))
        assert _resolve_secret_dir() == secret

    def test_copaw_env_fallback(self, tmp_path, monkeypatch):
        _clear_env(monkeypatch)
        secret = tmp_path / "copaw_secret"
        secret.mkdir()
        monkeypatch.setenv("COPAW_SECRET_DIR", str(secret))
        assert _resolve_secret_dir() == secret

    def test_home_qwenpaw_secret(self, tmp_path, monkeypatch):
        _clear_env(monkeypatch)
        monkeypatch.chdir(tmp_path)
        qwenpaw_dir = tmp_path / ".qwenpaw.secret"
        qwenpaw_dir.mkdir()
        with patch("iac_code.services.qwenpaw_source.Path.home", return_value=tmp_path):
            assert _resolve_secret_dir() == qwenpaw_dir

    def test_home_copaw_secret_legacy(self, tmp_path, monkeypatch):
        _clear_env(monkeypatch)
        monkeypatch.chdir(tmp_path)
        copaw_dir = tmp_path / ".copaw.secret"
        copaw_dir.mkdir()
        with patch("iac_code.services.qwenpaw_source.Path.home", return_value=tmp_path):
            assert _resolve_secret_dir() == copaw_dir

    def test_cwd_dot_secret(self, tmp_path, monkeypatch):
        _clear_env(monkeypatch)
        secret = tmp_path / ".secret"
        secret.mkdir()
        monkeypatch.chdir(tmp_path)
        with patch("iac_code.services.qwenpaw_source.Path.home", return_value=tmp_path / "nohome"):
            assert _resolve_secret_dir() == secret

    def test_returns_none_when_no_secret(self, tmp_path, monkeypatch):
        _clear_env(monkeypatch)
        monkeypatch.chdir(tmp_path)
        with patch("iac_code.services.qwenpaw_source.Path.home", return_value=tmp_path / "nohome"):
            assert _resolve_secret_dir() is None


class TestReadActiveModel:
    def test_reads_from_providers_subdir(self, tmp_path):
        providers = tmp_path / "providers"
        providers.mkdir()
        data = {"model": "qwen3.6-plus", "provider_id": "dashscope"}
        (providers / "active_model.json").write_text(json.dumps(data))
        assert _read_active_model(tmp_path) == data

    def test_reads_from_root_fallback(self, tmp_path):
        data = {"model": "qwen3.6-plus", "provider": "dashscope"}
        (tmp_path / "active_model.json").write_text(json.dumps(data))
        assert _read_active_model(tmp_path) == data

    def test_prefers_providers_subdir_over_root(self, tmp_path):
        providers = tmp_path / "providers"
        providers.mkdir()
        subdir_data = {"model": "new-model", "provider_id": "openai"}
        root_data = {"model": "old-model", "provider": "dashscope"}
        (providers / "active_model.json").write_text(json.dumps(subdir_data))
        (tmp_path / "active_model.json").write_text(json.dumps(root_data))
        assert _read_active_model(tmp_path) == subdir_data

    def test_returns_none_when_missing(self, tmp_path):
        assert _read_active_model(tmp_path) is None

    def test_returns_none_for_invalid_json(self, tmp_path):
        providers = tmp_path / "providers"
        providers.mkdir()
        (providers / "active_model.json").write_text("not json")
        (tmp_path / "active_model.json").write_text("also not json")
        assert _read_active_model(tmp_path) is None


class TestReadProviderConfig:
    def test_reads_from_builtin_subdir(self, tmp_path):
        builtin = tmp_path / "providers" / "builtin"
        builtin.mkdir(parents=True)
        data = {"api_key": "ENC:xxx", "base_url": "https://example.com"}
        (builtin / "dashscope.json").write_text(json.dumps(data))
        assert _read_provider_config(tmp_path, "dashscope") == data

    def test_reads_from_custom_subdir(self, tmp_path):
        custom = tmp_path / "providers" / "custom"
        custom.mkdir(parents=True)
        data = {"api_key": "ENC:yyy", "base_url": "https://custom.com"}
        (custom / "my-provider.json").write_text(json.dumps(data))
        assert _read_provider_config(tmp_path, "my-provider") == data

    def test_reads_flat_fallback(self, tmp_path):
        providers_dir = tmp_path / "providers"
        providers_dir.mkdir()
        data = {"api_key": "encrypted_key", "base_url": "https://example.com"}
        (providers_dir / "dashscope.json").write_text(json.dumps(data))
        assert _read_provider_config(tmp_path, "dashscope") == data

    def test_returns_none_when_missing(self, tmp_path):
        assert _read_provider_config(tmp_path, "nonexistent") is None


class TestDecryptApiKey:
    def test_plaintext_passthrough(self):
        assert _decrypt_api_key("sk-plain-key") == "sk-plain-key"

    def test_empty_returns_none(self):
        assert _decrypt_api_key("") is None

    def test_enc_prefix_stripped_before_decrypt(self):
        fake_hex_key = "aa" * 32  # 64-char valid hex → 32 bytes
        with patch("iac_code.services.qwenpaw_source._get_master_key", return_value=fake_hex_key):
            with patch("cryptography.fernet.Fernet") as mock_fernet_cls:
                mock_fernet = mock_fernet_cls.return_value
                mock_fernet.decrypt.return_value = b"decrypted-key"
                result = _decrypt_api_key("ENC:ciphertext123")
                mock_fernet.decrypt.assert_called_once_with(b"ciphertext123")
                assert result == "decrypted-key"


class TestMapQwenPawProviderId:
    def test_maps_known_provider(self):
        assert _map_qwenpaw_provider_id("dashscope") == "dashscope"

    def test_maps_kimi_cn(self):
        assert _map_qwenpaw_provider_id("kimi-cn") == "kimi_cn"

    def test_maps_anthropic(self):
        assert _map_qwenpaw_provider_id("anthropic") == "anthropic"

    def test_returns_none_for_unknown(self):
        assert _map_qwenpaw_provider_id("totally-unknown") is None


class TestLoadFromQwenPaw:
    def test_returns_none_when_no_secret_dir(self, tmp_path, monkeypatch):
        _clear_env(monkeypatch)
        monkeypatch.chdir(tmp_path)
        with patch("iac_code.services.qwenpaw_source.Path.home", return_value=tmp_path / "nohome"):
            assert load_from_qwenpaw() is None

    def test_returns_config_with_provider_id_field(self, tmp_path, monkeypatch):
        """QwenPaw uses 'provider_id' (not 'provider') in active_model.json."""
        secret = tmp_path / ".secret"
        secret.mkdir()
        providers = secret / "providers"
        providers.mkdir()
        active = {"model": "qwen3.6-plus", "provider_id": "dashscope"}
        (providers / "active_model.json").write_text(json.dumps(active))
        builtin = providers / "builtin"
        builtin.mkdir()
        provider_cfg = {"api_key": "", "base_url": "https://custom.example.com/v1"}
        (builtin / "dashscope.json").write_text(json.dumps(provider_cfg))
        monkeypatch.setenv("QWENPAW_SECRET_DIR", str(secret))

        with patch("iac_code.services.qwenpaw_source._decrypt_api_key", return_value=None):
            result = load_from_qwenpaw()

        assert result is not None
        assert result.model == "qwen3.6-plus"
        assert result.provider_key == "dashscope"
        assert result.base_url == "https://custom.example.com/v1"

    def test_returns_config_with_legacy_provider_field(self, tmp_path, monkeypatch):
        """Backward compatibility: 'provider' field still works."""
        secret = tmp_path / ".secret"
        secret.mkdir()
        active = {"model": "qwen3.6-plus", "provider": "dashscope"}
        (secret / "active_model.json").write_text(json.dumps(active))
        providers_dir = secret / "providers"
        providers_dir.mkdir()
        provider_cfg = {"api_key": "", "base_url": "https://custom.example.com/v1"}
        (providers_dir / "dashscope.json").write_text(json.dumps(provider_cfg))
        monkeypatch.setenv("QWENPAW_SECRET_DIR", str(secret))

        with patch("iac_code.services.qwenpaw_source._decrypt_api_key", return_value=None):
            result = load_from_qwenpaw()

        assert result is not None
        assert result.model == "qwen3.6-plus"
        assert result.provider_key == "dashscope"

    def test_raises_on_unknown_provider(self, tmp_path, monkeypatch):
        secret = tmp_path / ".secret"
        secret.mkdir()
        providers = secret / "providers"
        providers.mkdir()
        active = {"model": "some-model", "provider_id": "totally-unknown-provider"}
        (providers / "active_model.json").write_text(json.dumps(active))
        monkeypatch.setenv("QWENPAW_SECRET_DIR", str(secret))
        with pytest.raises(QwenPawError, match="totally-unknown-provider") as exc_info:
            load_from_qwenpaw()
        assert "QwenPaw" in str(exc_info.value)
        assert "settings.yml" in str(exc_info.value)

    def test_uses_registry_base_url_as_fallback(self, tmp_path, monkeypatch):
        secret = tmp_path / ".secret"
        secret.mkdir()
        providers = secret / "providers"
        providers.mkdir()
        active = {"model": "qwen3.6-plus", "provider_id": "dashscope"}
        (providers / "active_model.json").write_text(json.dumps(active))
        monkeypatch.setenv("QWENPAW_SECRET_DIR", str(secret))

        with patch("iac_code.services.qwenpaw_source._decrypt_api_key", return_value=None):
            result = load_from_qwenpaw()

        assert result is not None
        assert result.base_url == "https://dashscope.aliyuncs.com/compatible-mode/v1"
