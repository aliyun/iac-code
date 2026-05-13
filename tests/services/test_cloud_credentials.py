import json
import os
from unittest.mock import patch

from iac_code.services.cloud_credentials import CloudCredentials
from iac_code.services.providers.aliyun import AliyunCredential


class TestCloudCredentialsHasProvider:
    def test_has_provider_aliyun_true_with_env_vars(self):
        env = {
            "ALIBABA_CLOUD_ACCESS_KEY_ID": "test_id",
            "ALIBABA_CLOUD_ACCESS_KEY_SECRET": "test_secret",
        }
        with patch.dict(os.environ, env, clear=False):
            cc = CloudCredentials()
            assert cc.has_provider("aliyun") is True

    def test_has_provider_aliyun_true_with_config_file(self, tmp_path):
        config_file = tmp_path / "config.json"
        config = {
            "current": "default",
            "profiles": [
                {
                    "name": "default",
                    "mode": "AK",
                    "access_key_id": "file_id",
                    "access_key_secret": "file_secret",
                    "region_id": "cn-hangzhou",
                }
            ],
        }
        config_file.write_text(json.dumps(config))

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ALIBABA_CLOUD_ACCESS_KEY_ID", None)
            os.environ.pop("ALIBABA_CLOUD_ACCESS_KEY_SECRET", None)
            os.environ.pop("ALIBABA_CLOUD_REGION_ID", None)
            cc = CloudCredentials(aliyun_config_path=str(config_file))
            assert cc.has_provider("aliyun") is True

    def test_has_provider_aliyun_false_when_unconfigured(self, tmp_path):
        config_file = tmp_path / "nonexistent.json"
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ALIBABA_CLOUD_ACCESS_KEY_ID", None)
            os.environ.pop("ALIBABA_CLOUD_ACCESS_KEY_SECRET", None)
            os.environ.pop("ALIBABA_CLOUD_REGION_ID", None)
            cc = CloudCredentials(aliyun_config_path=str(config_file))
            assert cc.has_provider("aliyun") is False

    def test_has_provider_unknown_returns_false(self):
        cc = CloudCredentials()
        assert cc.has_provider("aws") is False
        assert cc.has_provider("gcp") is False
        assert cc.has_provider("") is False


class TestCloudCredentialsGetProvider:
    def test_get_provider_aliyun_returns_credential_with_env_vars(self):
        env = {
            "ALIBABA_CLOUD_ACCESS_KEY_ID": "env_id",
            "ALIBABA_CLOUD_ACCESS_KEY_SECRET": "env_secret",
        }
        with patch.dict(os.environ, env, clear=False):
            os.environ.pop("ALIBABA_CLOUD_REGION_ID", None)
            cc = CloudCredentials()
            cred = cc.get_provider("aliyun")
        assert cred is not None
        assert isinstance(cred, AliyunCredential)
        assert cred.access_key_id == "env_id"
        assert cred.access_key_secret == "env_secret"

    def test_get_provider_aliyun_returns_credential_with_config_file(self, tmp_path):
        config_file = tmp_path / "config.json"
        config = {
            "current": "default",
            "profiles": [
                {
                    "name": "default",
                    "mode": "AK",
                    "access_key_id": "file_id",
                    "access_key_secret": "file_secret",
                    "region_id": "cn-shenzhen",
                }
            ],
        }
        config_file.write_text(json.dumps(config))

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ALIBABA_CLOUD_ACCESS_KEY_ID", None)
            os.environ.pop("ALIBABA_CLOUD_ACCESS_KEY_SECRET", None)
            os.environ.pop("ALIBABA_CLOUD_REGION_ID", None)
            cc = CloudCredentials(aliyun_config_path=str(config_file))
            cred = cc.get_provider("aliyun")

        assert cred is not None
        assert isinstance(cred, AliyunCredential)
        assert cred.access_key_id == "file_id"
        assert cred.region_id == "cn-shenzhen"

    def test_get_provider_aliyun_returns_none_when_unconfigured(self, tmp_path):
        config_file = tmp_path / "nonexistent.json"
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ALIBABA_CLOUD_ACCESS_KEY_ID", None)
            os.environ.pop("ALIBABA_CLOUD_ACCESS_KEY_SECRET", None)
            os.environ.pop("ALIBABA_CLOUD_REGION_ID", None)
            cc = CloudCredentials(aliyun_config_path=str(config_file))
            cred = cc.get_provider("aliyun")
        assert cred is None

    def test_get_provider_unknown_returns_none(self):
        cc = CloudCredentials()
        assert cc.get_provider("aws") is None
        assert cc.get_provider("gcp") is None
        assert cc.get_provider("") is None


class TestCloudCredentialsListProviders:
    def test_list_providers_includes_aliyun_when_configured(self):
        env = {
            "ALIBABA_CLOUD_ACCESS_KEY_ID": "test_id",
            "ALIBABA_CLOUD_ACCESS_KEY_SECRET": "test_secret",
        }
        with patch.dict(os.environ, env, clear=False):
            cc = CloudCredentials()
            providers = cc.list_providers()
        assert "aliyun" in providers
        assert isinstance(providers, list)

    def test_list_providers_excludes_aliyun_when_unconfigured(self, tmp_path):
        config_file = tmp_path / "nonexistent.json"
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ALIBABA_CLOUD_ACCESS_KEY_ID", None)
            os.environ.pop("ALIBABA_CLOUD_ACCESS_KEY_SECRET", None)
            os.environ.pop("ALIBABA_CLOUD_REGION_ID", None)
            cc = CloudCredentials(aliyun_config_path=str(config_file))
            providers = cc.list_providers()
        assert "aliyun" not in providers
        assert providers == []

    def test_list_providers_returns_empty_list_when_no_providers(self, tmp_path):
        config_file = tmp_path / "nonexistent.json"
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ALIBABA_CLOUD_ACCESS_KEY_ID", None)
            os.environ.pop("ALIBABA_CLOUD_ACCESS_KEY_SECRET", None)
            os.environ.pop("ALIBABA_CLOUD_REGION_ID", None)
            cc = CloudCredentials(aliyun_config_path=str(config_file))
            result = cc.list_providers()
        assert result == []
