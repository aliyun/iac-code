import json
import os
from unittest.mock import patch

import pytest
import yaml

from iac_code.services.providers.aliyun import (
    AliyunCredential,
    AliyunCredentials,
    mask_sensitive,
)
from iac_code.services.providers.aliyun_oauth import AliyunOAuthReloginRequired, OAuthStsCredentials, OAuthToken


class TestAliyunCredential:
    def test_dataclass_fields(self):
        cred = AliyunCredential(
            access_key_id="test_id",
            access_key_secret="test_secret",
            region_id="cn-beijing",
        )
        assert cred.access_key_id == "test_id"
        assert cred.access_key_secret == "test_secret"
        assert cred.region_id == "cn-beijing"
        assert cred.mode == "AK"

    def test_default_region(self):
        cred = AliyunCredential(
            access_key_id="id",
            access_key_secret="secret",
        )
        assert cred.region_id == "cn-hangzhou"

    def test_default_mode(self):
        cred = AliyunCredential()
        assert cred.mode == "AK"
        assert cred.access_key_id == ""
        assert cred.access_key_secret == ""

    def test_sts_token_mode(self):
        cred = AliyunCredential(
            mode="StsToken",
            access_key_id="id",
            access_key_secret="secret",
            sts_token="token123",
        )
        assert cred.mode == "StsToken"
        assert cred.sts_token == "token123"

    def test_ram_role_arn_mode(self):
        cred = AliyunCredential(
            mode="RamRoleArn",
            access_key_id="id",
            access_key_secret="secret",
            ram_role_arn="acs:ram::123:role/test",
            ram_session_name="session1",
        )
        assert cred.mode == "RamRoleArn"
        assert cred.ram_role_arn == "acs:ram::123:role/test"
        assert cred.ram_session_name == "session1"

    def test_oauth_mode_fields(self):
        cred = AliyunCredential(
            mode="OAuth",
            access_key_id="tmp-ak",
            access_key_secret="tmp-sk",
            sts_token="tmp-sts",
            sts_expiration=1798794000,
            oauth_site_type="CN",
            oauth_access_token="oauth-access",
            oauth_refresh_token="oauth-refresh",
            oauth_access_token_expire=1798790400,
            oauth_refresh_token_expire=1801382400,
            region_id="cn-hangzhou",
        )

        assert cred.mode == "OAuth"
        assert cred.access_key_id == "tmp-ak"
        assert cred.access_key_secret == "tmp-sk"
        assert cred.sts_token == "tmp-sts"
        assert cred.sts_expiration == 1798794000
        assert cred.oauth_site_type == "CN"
        assert cred.oauth_access_token == "oauth-access"
        assert cred.oauth_refresh_token == "oauth-refresh"
        assert cred.oauth_access_token_expire == 1798790400
        assert cred.oauth_refresh_token_expire == 1801382400
        assert cred.region_id == "cn-hangzhou"


class TestMaskSensitive:
    def test_mask_normal_string(self):
        assert mask_sensitive("LTAI5tAbcDefGhi") == "*" * 15

    def test_mask_empty_string(self):
        assert mask_sensitive("") == ""

    def test_mask_preserves_length(self):
        value = "my_secret_key_12345"
        masked = mask_sensitive(value)
        assert len(masked) == len(value)
        assert masked == "*" * len(value)


class TestAliyunCredentialsLoadFromEnv:
    def test_load_from_env_vars_defaults_to_cn_hangzhou(self):
        env = {
            "ALIBABA_CLOUD_ACCESS_KEY_ID": "env_id",
            "ALIBABA_CLOUD_ACCESS_KEY_SECRET": "env_secret",
        }
        with (
            patch.dict(os.environ, env, clear=False),
            patch.object(AliyunCredentials, "_load_from_iac_code_config", return_value=None),
            patch.object(AliyunCredentials, "_load_from_aliyun_cli", return_value=None),
        ):
            os.environ.pop("ALIBABA_CLOUD_REGION_ID", None)
            os.environ.pop("ALIBABA_CLOUD_SECURITY_TOKEN", None)
            cred = AliyunCredentials.load()
        assert cred is not None
        assert cred.access_key_id == "env_id"
        assert cred.access_key_secret == "env_secret"
        assert cred.region_id == "cn-hangzhou"
        assert cred.mode == "AK"

    def test_load_from_env_vars_falls_back_to_cli_region_when_no_iac_code_config(self, tmp_path):
        """Env has AK/SK but no REGION_ID; iac-code config absent → CLI config region wins."""
        cli_config = tmp_path / "config.json"
        cli_config.write_text(
            json.dumps(
                {
                    "current": "default",
                    "profiles": [
                        {
                            "name": "default",
                            "mode": "AK",
                            "access_key_id": "file_id",
                            "access_key_secret": "file_secret",
                            "region_id": "cn-beijing",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        env = {
            "ALIBABA_CLOUD_ACCESS_KEY_ID": "env_id",
            "ALIBABA_CLOUD_ACCESS_KEY_SECRET": "env_secret",
        }
        with (
            patch.dict(os.environ, env, clear=False),
            patch.object(AliyunCredentials, "_load_from_iac_code_config", return_value=None),
        ):
            os.environ.pop("ALIBABA_CLOUD_REGION_ID", None)
            os.environ.pop("ALIBABA_CLOUD_SECURITY_TOKEN", None)
            cred = AliyunCredentials.load(config_path=str(cli_config))
        assert cred is not None
        assert cred.access_key_id == "env_id"
        assert cred.region_id == "cn-beijing"

    def test_load_from_env_vars_iac_code_region_wins_over_cli_region(self, tmp_path):
        """Both file sources present — iac-code config region takes precedence over CLI config."""
        cli_config = tmp_path / "config.json"
        cli_config.write_text(
            json.dumps(
                {
                    "current": "default",
                    "profiles": [
                        {
                            "name": "default",
                            "mode": "AK",
                            "access_key_id": "cli_id",
                            "access_key_secret": "cli_secret",
                            "region_id": "cn-shenzhen",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        cloud_creds_file = tmp_path / ".cloud-credentials.yml"
        cloud_creds_file.write_text(
            yaml.dump(
                {
                    "aliyun": {
                        "mode": "AK",
                        "access_key_id": "iac_id",
                        "access_key_secret": "iac_secret",
                        "region_id": "cn-beijing",
                    }
                }
            ),
            encoding="utf-8",
        )
        env = {
            "ALIBABA_CLOUD_ACCESS_KEY_ID": "env_id",
            "ALIBABA_CLOUD_ACCESS_KEY_SECRET": "env_secret",
        }
        with (
            patch.dict(os.environ, env, clear=False),
            patch("iac_code.services.providers.aliyun.get_cloud_credentials_path", return_value=cloud_creds_file),
        ):
            os.environ.pop("ALIBABA_CLOUD_REGION_ID", None)
            os.environ.pop("ALIBABA_CLOUD_SECURITY_TOKEN", None)
            cred = AliyunCredentials.load(config_path=str(cli_config))
        assert cred is not None
        assert cred.region_id == "cn-beijing"

    def test_load_from_env_vars_uses_iac_code_region(self, tmp_path):
        """Env vars have AK but no region — should use region from iac-code config."""
        cloud_creds_file = tmp_path / ".cloud-credentials.yml"
        data = {
            "aliyun": {
                "mode": "AK",
                "access_key_id": "iac_id",
                "access_key_secret": "iac_secret",
                "region_id": "cn-beijing",
            }
        }
        cloud_creds_file.write_text(yaml.dump(data), encoding="utf-8")

        env = {
            "ALIBABA_CLOUD_ACCESS_KEY_ID": "env_id",
            "ALIBABA_CLOUD_ACCESS_KEY_SECRET": "env_secret",
        }
        with (
            patch.dict(os.environ, env, clear=False),
            patch("iac_code.services.providers.aliyun.get_cloud_credentials_path", return_value=cloud_creds_file),
        ):
            os.environ.pop("ALIBABA_CLOUD_REGION_ID", None)
            os.environ.pop("ALIBABA_CLOUD_SECURITY_TOKEN", None)
            cred = AliyunCredentials.load()
        assert cred is not None
        assert cred.access_key_id == "env_id"
        assert cred.region_id == "cn-beijing"

    def test_load_from_env_vars_with_region(self):
        env = {
            "ALIBABA_CLOUD_ACCESS_KEY_ID": "env_id",
            "ALIBABA_CLOUD_ACCESS_KEY_SECRET": "env_secret",
            "ALIBABA_CLOUD_REGION_ID": "cn-shanghai",
        }
        with patch.dict(os.environ, env, clear=False):
            os.environ.pop("ALIBABA_CLOUD_SECURITY_TOKEN", None)
            cred = AliyunCredentials.load()
        assert cred is not None
        assert cred.region_id == "cn-shanghai"

    def test_load_from_env_vars_with_sts_token(self):
        env = {
            "ALIBABA_CLOUD_ACCESS_KEY_ID": "env_id",
            "ALIBABA_CLOUD_ACCESS_KEY_SECRET": "env_secret",
            "ALIBABA_CLOUD_SECURITY_TOKEN": "sts_token_123",
        }
        with (
            patch.dict(os.environ, env, clear=False),
            patch.object(AliyunCredentials, "_load_from_iac_code_config", return_value=None),
        ):
            os.environ.pop("ALIBABA_CLOUD_REGION_ID", None)
            cred = AliyunCredentials.load()
        assert cred is not None
        assert cred.mode == "StsToken"
        assert cred.sts_token == "sts_token_123"

    def test_env_vars_take_priority_over_config_file(self, tmp_path):
        config_file = tmp_path / "config.json"
        config = {
            "current": "default",
            "profiles": [
                {
                    "name": "default",
                    "mode": "AK",
                    "access_key_id": "file_id",
                    "access_key_secret": "file_secret",
                    "region_id": "cn-beijing",
                }
            ],
        }
        config_file.write_text(json.dumps(config), encoding="utf-8")

        env = {
            "ALIBABA_CLOUD_ACCESS_KEY_ID": "env_id",
            "ALIBABA_CLOUD_ACCESS_KEY_SECRET": "env_secret",
        }
        with patch.dict(os.environ, env, clear=False):
            os.environ.pop("ALIBABA_CLOUD_REGION_ID", None)
            os.environ.pop("ALIBABA_CLOUD_SECURITY_TOKEN", None)
            cred = AliyunCredentials.load(config_path=str(config_file))
        assert cred.access_key_id == "env_id"
        assert cred.access_key_secret == "env_secret"


class TestAliyunCredentialsLoadFromAliyunCli:
    def test_load_from_config_file(self, tmp_path):
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
        config_file.write_text(json.dumps(config), encoding="utf-8")

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ALIBABA_CLOUD_ACCESS_KEY_ID", None)
            os.environ.pop("ALIBABA_CLOUD_ACCESS_KEY_SECRET", None)
            os.environ.pop("ALIBABA_CLOUD_REGION_ID", None)
            cred = AliyunCredentials.load(config_path=str(config_file))

        assert cred is not None
        assert cred.access_key_id == "file_id"
        assert cred.access_key_secret == "file_secret"
        assert cred.region_id == "cn-shenzhen"
        assert cred.mode == "AK"

    def test_load_oauth_from_aliyun_cli_default_profile(self, tmp_path):
        config_file = tmp_path / "config.json"
        config = {
            "current": "default",
            "profiles": [
                {
                    "name": "default",
                    "mode": "OAuth",
                    "access_key_id": "tmp-ak",
                    "access_key_secret": "tmp-sk",
                    "sts_token": "tmp-sts",
                    "sts_expiration": 1798794000,
                    "oauth_site_type": "CN",
                    "oauth_access_token": "oauth-access",
                    "oauth_refresh_token": "oauth-refresh",
                    "oauth_access_token_expire": 1798790400,
                    "oauth_refresh_token_expire": 1801382400,
                    "region_id": "cn-hangzhou",
                }
            ],
        }
        config_file.write_text(json.dumps(config), encoding="utf-8")

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ALIBABA_CLOUD_ACCESS_KEY_ID", None)
            os.environ.pop("ALIBABA_CLOUD_ACCESS_KEY_SECRET", None)
            os.environ.pop("ALIBABA_CLOUD_REGION_ID", None)
            cred = AliyunCredentials.load(config_path=str(config_file))

        assert cred is not None
        assert cred.mode == "OAuth"
        assert cred.access_key_id == "tmp-ak"
        assert cred.access_key_secret == "tmp-sk"
        assert cred.sts_token == "tmp-sts"
        assert cred.sts_expiration == 1798794000
        assert cred.oauth_site_type == "CN"
        assert cred.oauth_access_token == "oauth-access"
        assert cred.oauth_refresh_token == "oauth-refresh"
        assert cred.oauth_access_token_expire == 1798790400
        assert cred.oauth_refresh_token_expire == 1801382400
        assert cred.region_id == "cn-hangzhou"

    def test_load_ram_role_arn_from_config_file(self, tmp_path):
        config_file = tmp_path / "config.json"
        config = {
            "current": "default",
            "profiles": [
                {
                    "name": "default",
                    "mode": "RamRoleArn",
                    "access_key_id": "file_id",
                    "access_key_secret": "file_secret",
                    "ram_role_arn": "acs:ram::123:role/test",
                    "ram_session_name": "session1",
                    "region_id": "cn-beijing",
                }
            ],
        }
        config_file.write_text(json.dumps(config), encoding="utf-8")

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ALIBABA_CLOUD_ACCESS_KEY_ID", None)
            os.environ.pop("ALIBABA_CLOUD_ACCESS_KEY_SECRET", None)
            cred = AliyunCredentials.load(config_path=str(config_file))

        assert cred is not None
        assert cred.mode == "RamRoleArn"
        assert cred.ram_role_arn == "acs:ram::123:role/test"
        assert cred.ram_session_name == "session1"

    def test_load_returns_none_when_no_config_file(self, tmp_path):
        config_file = tmp_path / "nonexistent.json"
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ALIBABA_CLOUD_ACCESS_KEY_ID", None)
            os.environ.pop("ALIBABA_CLOUD_ACCESS_KEY_SECRET", None)
            os.environ.pop("ALIBABA_CLOUD_REGION_ID", None)
            cred = AliyunCredentials.load(config_path=str(config_file))
        assert cred is None

    def test_load_returns_none_when_unconfigured(self, tmp_path):
        config_file = tmp_path / "config.json"
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ALIBABA_CLOUD_ACCESS_KEY_ID", None)
            os.environ.pop("ALIBABA_CLOUD_ACCESS_KEY_SECRET", None)
            os.environ.pop("ALIBABA_CLOUD_REGION_ID", None)
            cred = AliyunCredentials.load(config_path=str(config_file))
        assert cred is None

    def test_load_uses_default_profile(self, tmp_path):
        config_file = tmp_path / "config.json"
        config = {
            "current": "default",
            "profiles": [
                {
                    "name": "other",
                    "mode": "AK",
                    "access_key_id": "other_id",
                    "access_key_secret": "other_secret",
                    "region_id": "cn-beijing",
                },
                {
                    "name": "default",
                    "mode": "AK",
                    "access_key_id": "default_id",
                    "access_key_secret": "default_secret",
                    "region_id": "cn-hangzhou",
                },
            ],
        }
        config_file.write_text(json.dumps(config), encoding="utf-8")

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ALIBABA_CLOUD_ACCESS_KEY_ID", None)
            os.environ.pop("ALIBABA_CLOUD_ACCESS_KEY_SECRET", None)
            os.environ.pop("ALIBABA_CLOUD_REGION_ID", None)
            cred = AliyunCredentials.load(config_path=str(config_file))

        assert cred.access_key_id == "default_id"

    def test_load_from_aliyun_cli_public(self, tmp_path):
        config_file = tmp_path / "config.json"
        config = {
            "current": "default",
            "profiles": [
                {
                    "name": "default",
                    "mode": "RamRoleArn",
                    "access_key_id": "id",
                    "access_key_secret": "secret",
                    "ram_role_arn": "acs:ram::123:role/test",
                    "ram_session_name": "session1",
                    "region_id": "cn-hangzhou",
                }
            ],
        }
        config_file.write_text(json.dumps(config), encoding="utf-8")

        cred = AliyunCredentials.load_from_aliyun_cli(config_path=str(config_file))
        assert cred is not None
        assert cred.mode == "RamRoleArn"
        assert cred.ram_role_arn == "acs:ram::123:role/test"

    def test_load_from_aliyun_cli_skips_malformed_profiles_before_default(self, tmp_path):
        config_file = tmp_path / "config.json"
        config = {
            "current": "default",
            "profiles": [
                {"mode": "AK", "access_key_id": "missing-name"},
                "not-a-profile",
                None,
                {
                    "name": "default",
                    "mode": "AK",
                    "access_key_id": "default-id",
                    "access_key_secret": "default-secret",
                    "region_id": "cn-beijing",
                },
            ],
        }
        config_file.write_text(json.dumps(config), encoding="utf-8")

        cred = AliyunCredentials.load_from_aliyun_cli(config_path=str(config_file))

        assert cred is not None
        assert cred.access_key_id == "default-id"
        assert cred.access_key_secret == "default-secret"
        assert cred.region_id == "cn-beijing"

    @pytest.mark.parametrize(
        "profiles",
        [
            [{"mode": "AK", "access_key_id": "missing-name"}],
            ["not-a-profile"],
            {"default": {"mode": "AK", "access_key_id": "not-a-list"}},
        ],
    )
    def test_load_from_aliyun_cli_returns_none_without_valid_default_profile(self, tmp_path, profiles):
        config_file = tmp_path / "config.json"
        config_file.write_text(json.dumps({"current": "default", "profiles": profiles}), encoding="utf-8")

        cred = AliyunCredentials.load_from_aliyun_cli(config_path=str(config_file))

        assert cred is None

    @pytest.mark.parametrize(
        "field_name",
        [
            "sts_expiration",
            "oauth_access_token_expire",
            "oauth_refresh_token_expire",
        ],
    )
    def test_load_from_aliyun_cli_returns_none_for_malformed_numeric_default_profile(self, tmp_path, field_name):
        config_file = tmp_path / "config.json"
        profile = {
            "name": "default",
            "mode": "OAuth",
            "access_key_id": "id",
            "access_key_secret": "secret",
            "region_id": "cn-hangzhou",
            field_name: "not-an-int",
        }
        config_file.write_text(json.dumps({"current": "default", "profiles": [profile]}), encoding="utf-8")

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ALIBABA_CLOUD_ACCESS_KEY_ID", None)
            os.environ.pop("ALIBABA_CLOUD_ACCESS_KEY_SECRET", None)
            os.environ.pop("ALIBABA_CLOUD_REGION_ID", None)
            os.environ.pop("ALIBABA_CLOUD_SECURITY_TOKEN", None)

            assert AliyunCredentials.load_from_aliyun_cli(config_path=str(config_file)) is None
            assert AliyunCredentials.load(config_path=str(config_file)) is None


class TestAliyunCredentialsLoadFromIacCode:
    def test_load_from_iac_code_config(self, tmp_path):
        cloud_creds_file = tmp_path / ".cloud-credentials.yml"
        data = {
            "aliyun": {
                "mode": "AK",
                "access_key_id": "iac_id",
                "access_key_secret": "iac_secret",
                "region_id": "cn-beijing",
            }
        }
        cloud_creds_file.write_text(yaml.dump(data), encoding="utf-8")

        with patch("iac_code.services.providers.aliyun.get_cloud_credentials_path", return_value=cloud_creds_file):
            cred = AliyunCredentials._load_from_iac_code_config()

        assert cred is not None
        assert cred.access_key_id == "iac_id"
        assert cred.access_key_secret == "iac_secret"
        assert cred.region_id == "cn-beijing"

    def test_load_oauth_from_iac_code_config(self, tmp_path):
        cloud_creds_file = tmp_path / ".cloud-credentials.yml"
        data = {
            "aliyun": {
                "mode": "OAuth",
                "region_id": "cn-hangzhou",
                "oauth_site_type": "CN",
                "oauth_access_token": "oauth-access",
                "oauth_refresh_token": "oauth-refresh",
                "oauth_access_token_expire": 1798790400,
                "oauth_refresh_token_expire": 1801382400,
                "access_key_id": "tmp-ak",
                "access_key_secret": "tmp-sk",
                "sts_token": "tmp-sts",
                "sts_expiration": 1798794000,
            }
        }
        cloud_creds_file.write_text(yaml.dump(data), encoding="utf-8")

        with patch("iac_code.services.providers.aliyun.get_cloud_credentials_path", return_value=cloud_creds_file):
            cred = AliyunCredentials._load_from_iac_code_config()

        assert cred is not None
        assert cred.mode == "OAuth"
        assert cred.region_id == "cn-hangzhou"
        assert cred.oauth_site_type == "CN"
        assert cred.oauth_access_token == "oauth-access"
        assert cred.oauth_refresh_token == "oauth-refresh"
        assert cred.oauth_access_token_expire == 1798790400
        assert cred.oauth_refresh_token_expire == 1801382400
        assert cred.access_key_id == "tmp-ak"
        assert cred.access_key_secret == "tmp-sk"
        assert cred.sts_token == "tmp-sts"
        assert cred.sts_expiration == 1798794000

    def test_load_from_iac_code_returns_none_when_no_file(self, tmp_path):
        cloud_creds_file = tmp_path / ".cloud-credentials.yml"

        with patch("iac_code.services.providers.aliyun.get_cloud_credentials_path", return_value=cloud_creds_file):
            cred = AliyunCredentials._load_from_iac_code_config()

        assert cred is None

    def test_load_from_iac_code_ram_role_arn(self, tmp_path):
        cloud_creds_file = tmp_path / ".cloud-credentials.yml"
        data = {
            "aliyun": {
                "mode": "RamRoleArn",
                "access_key_id": "iac_id",
                "access_key_secret": "iac_secret",
                "ram_role_arn": "acs:ram::456:role/dev",
                "ram_session_name": "dev-session",
                "region_id": "cn-shanghai",
            }
        }
        cloud_creds_file.write_text(yaml.dump(data), encoding="utf-8")

        with patch("iac_code.services.providers.aliyun.get_cloud_credentials_path", return_value=cloud_creds_file):
            cred = AliyunCredentials._load_from_iac_code_config()

        assert cred is not None
        assert cred.mode == "RamRoleArn"
        assert cred.ram_role_arn == "acs:ram::456:role/dev"

    def test_iac_code_config_takes_priority_over_aliyun_cli(self, tmp_path):
        # Set up iac-code config
        cloud_creds_file = tmp_path / ".cloud-credentials.yml"
        iac_data = {
            "aliyun": {
                "mode": "AK",
                "access_key_id": "iac_id",
                "access_key_secret": "iac_secret",
                "region_id": "cn-beijing",
            }
        }
        cloud_creds_file.write_text(yaml.dump(iac_data), encoding="utf-8")

        # Set up aliyun CLI config
        cli_config_file = tmp_path / "config.json"
        cli_config = {
            "current": "default",
            "profiles": [
                {
                    "name": "default",
                    "mode": "AK",
                    "access_key_id": "cli_id",
                    "access_key_secret": "cli_secret",
                    "region_id": "cn-hangzhou",
                }
            ],
        }
        cli_config_file.write_text(json.dumps(cli_config), encoding="utf-8")

        with (
            patch.dict(os.environ, {}, clear=False),
            patch("iac_code.services.providers.aliyun.get_cloud_credentials_path", return_value=cloud_creds_file),
            patch(
                "iac_code.services.providers.aliyun.DEFAULT_ALIYUN_CLI_CONFIG_PATH",
                str(cli_config_file),
            ),
        ):
            os.environ.pop("ALIBABA_CLOUD_ACCESS_KEY_ID", None)
            os.environ.pop("ALIBABA_CLOUD_ACCESS_KEY_SECRET", None)
            # load() with no config_path uses iac-code config then aliyun CLI
            cred = AliyunCredentials.load()

        assert cred is not None
        assert cred.access_key_id == "iac_id"


class TestAliyunCredentialsSave:
    def test_save_to_iac_code_config(self, tmp_path):
        cloud_creds_file = tmp_path / ".cloud-credentials.yml"

        with patch("iac_code.services.providers.aliyun.get_cloud_credentials_path", return_value=cloud_creds_file):
            cred = AliyunCredential(
                mode="AK",
                access_key_id="new_id",
                access_key_secret="new_secret",
                region_id="cn-hangzhou",
            )
            AliyunCredentials.save(cred)

        assert cloud_creds_file.exists()
        data = yaml.safe_load(cloud_creds_file.read_text(encoding="utf-8"))
        assert data["aliyun"]["mode"] == "AK"
        assert data["aliyun"]["access_key_id"] == "new_id"
        assert data["aliyun"]["access_key_secret"] == "new_secret"
        assert data["aliyun"]["region_id"] == "cn-hangzhou"

    def test_save_ram_role_arn_to_iac_code_config(self, tmp_path):
        cloud_creds_file = tmp_path / ".cloud-credentials.yml"

        with patch("iac_code.services.providers.aliyun.get_cloud_credentials_path", return_value=cloud_creds_file):
            cred = AliyunCredential(
                mode="RamRoleArn",
                access_key_id="id",
                access_key_secret="secret",
                ram_role_arn="acs:ram::123:role/test",
                ram_session_name="session1",
                region_id="cn-beijing",
            )
            AliyunCredentials.save(cred)

        data = yaml.safe_load(cloud_creds_file.read_text(encoding="utf-8"))
        assert data["aliyun"]["mode"] == "RamRoleArn"
        assert data["aliyun"]["ram_role_arn"] == "acs:ram::123:role/test"
        assert data["aliyun"]["ram_session_name"] == "session1"

    def test_save_oauth_to_iac_code_config(self, tmp_path):
        cloud_creds_file = tmp_path / ".cloud-credentials.yml"

        with patch("iac_code.services.providers.aliyun.get_cloud_credentials_path", return_value=cloud_creds_file):
            cred = AliyunCredential(
                mode="OAuth",
                region_id="cn-hangzhou",
                oauth_site_type="INTL",
                oauth_access_token="oauth-access",
                oauth_refresh_token="oauth-refresh",
                oauth_access_token_expire=1798790400,
                oauth_refresh_token_expire=1801382400,
                access_key_id="tmp-ak",
                access_key_secret="tmp-sk",
                sts_token="tmp-sts",
                sts_expiration=1798794000,
            )
            AliyunCredentials.save(cred)

        data = yaml.safe_load(cloud_creds_file.read_text(encoding="utf-8"))
        assert data["aliyun"] == {
            "mode": "OAuth",
            "region_id": "cn-hangzhou",
            "oauth_site_type": "INTL",
            "oauth_access_token": "oauth-access",
            "oauth_refresh_token": "oauth-refresh",
            "oauth_access_token_expire": 1798790400,
            "oauth_refresh_token_expire": 1801382400,
            "access_key_id": "tmp-ak",
            "access_key_secret": "tmp-sk",
            "sts_token": "tmp-sts",
            "sts_expiration": 1798794000,
        }

    def test_save_to_aliyun_cli_format(self, tmp_path):
        """Test save with config_path (aliyun CLI format, for testing)."""
        config_file = tmp_path / "config.json"
        cred = AliyunCredential(
            mode="AK",
            access_key_id="new_id",
            access_key_secret="new_secret",
            region_id="cn-hangzhou",
        )
        AliyunCredentials.save(cred, config_path=str(config_file))

        assert config_file.exists()
        data = json.loads(config_file.read_text(encoding="utf-8"))
        assert data["current"] == "default"
        profiles = {p["name"]: p for p in data["profiles"]}
        assert "default" in profiles
        assert profiles["default"]["access_key_id"] == "new_id"
        assert profiles["default"]["access_key_secret"] == "new_secret"
        assert profiles["default"]["region_id"] == "cn-hangzhou"
        assert profiles["default"]["mode"] == "AK"

    def test_save_updates_existing_aliyun_cli_format(self, tmp_path):
        config_file = tmp_path / "config.json"
        existing_config = {
            "current": "default",
            "profiles": [
                {
                    "name": "default",
                    "mode": "AK",
                    "access_key_id": "old_id",
                    "access_key_secret": "old_secret",
                    "region_id": "cn-hangzhou",
                },
                {
                    "name": "staging",
                    "mode": "AK",
                    "access_key_id": "staging_id",
                    "access_key_secret": "staging_secret",
                    "region_id": "cn-shanghai",
                },
            ],
        }
        config_file.write_text(json.dumps(existing_config), encoding="utf-8")

        cred = AliyunCredential(
            access_key_id="updated_id",
            access_key_secret="updated_secret",
            region_id="cn-beijing",
        )
        AliyunCredentials.save(cred, config_path=str(config_file))

        data = json.loads(config_file.read_text(encoding="utf-8"))
        profiles = {p["name"]: p for p in data["profiles"]}

        # Default profile updated
        assert profiles["default"]["access_key_id"] == "updated_id"
        assert profiles["default"]["access_key_secret"] == "updated_secret"
        assert profiles["default"]["region_id"] == "cn-beijing"

        # Staging profile preserved
        assert "staging" in profiles
        assert profiles["staging"]["access_key_id"] == "staging_id"

    def test_save_creates_parent_dirs(self, tmp_path):
        config_file = tmp_path / "nested" / "dir" / "config.json"
        cred = AliyunCredential(
            access_key_id="id",
            access_key_secret="secret",
            region_id="cn-hangzhou",
        )
        AliyunCredentials.save(cred, config_path=str(config_file))
        assert config_file.exists()

    def test_save_does_not_write_to_aliyun_cli_config(self, tmp_path):
        """Verify that save() without config_path writes to iac-code, not aliyun CLI."""
        cloud_creds_file = tmp_path / ".cloud-credentials.yml"
        aliyun_cli_file = tmp_path / "config.json"

        with patch("iac_code.services.providers.aliyun.get_cloud_credentials_path", return_value=cloud_creds_file):
            cred = AliyunCredential(
                access_key_id="id",
                access_key_secret="secret",
                region_id="cn-hangzhou",
            )
            AliyunCredentials.save(cred)

        assert cloud_creds_file.exists()
        assert not aliyun_cli_file.exists()


class TestAliyunCredentialsOAuthRefresh:
    def test_refresh_oauth_uses_unexpired_sts_without_network(self, monkeypatch):
        cred = AliyunCredential(
            mode="OAuth",
            oauth_site_type="CN",
            oauth_access_token="access",
            oauth_refresh_token="refresh",
            oauth_access_token_expire=2000,
            access_key_id="tmp-ak",
            access_key_secret="tmp-sk",
            sts_token="tmp-sts",
            sts_expiration=1900,
        )

        class FailingClient:
            def refresh_access_token(self, refresh_token, *, now=None):
                raise AssertionError("refresh_access_token should not be called")

            def exchange_access_token_for_sts(self, access_token):
                raise AssertionError("exchange_access_token_for_sts should not be called")

        monkeypatch.setattr(
            AliyunCredentials,
            "save",
            lambda credential: (_ for _ in ()).throw(AssertionError("save should not be called")),
        )

        refreshed = AliyunCredentials.refresh_oauth_if_needed(cred, oauth_client=FailingClient(), now=1000)

        assert refreshed is cred
        assert cred.access_key_id == "tmp-ak"
        assert cred.access_key_secret == "tmp-sk"
        assert cred.sts_token == "tmp-sts"
        assert cred.sts_expiration == 1900

    def test_refresh_oauth_exchanges_expired_sts_with_current_access_token(self, monkeypatch):
        cred = AliyunCredential(
            mode="OAuth",
            oauth_site_type="CN",
            oauth_access_token="access",
            oauth_refresh_token="refresh",
            oauth_access_token_expire=2000,
            access_key_id="old-ak",
            access_key_secret="old-sk",
            sts_token="old-sts",
            sts_expiration=900,
        )
        saved: list[AliyunCredential] = []

        class FakeClient:
            def exchange_access_token_for_sts(self, access_token):
                assert access_token == "access"
                return OAuthStsCredentials("new-ak", "new-sk", "new-sts", 2500)

        monkeypatch.setattr(AliyunCredentials, "save", saved.append)

        refreshed = AliyunCredentials.refresh_oauth_if_needed(cred, oauth_client=FakeClient(), now=1000)

        assert refreshed is cred
        assert saved == [cred]
        assert cred.access_key_id == "new-ak"
        assert cred.access_key_secret == "new-sk"
        assert cred.sts_token == "new-sts"
        assert cred.sts_expiration == 2500

    def test_refresh_oauth_refreshes_access_token_before_exchange(self, monkeypatch):
        cred = AliyunCredential(
            mode="OAuth",
            oauth_site_type="CN",
            oauth_access_token="old-access",
            oauth_refresh_token="old-refresh",
            oauth_access_token_expire=900,
            access_key_id="old-ak",
            access_key_secret="old-sk",
            sts_token="old-sts",
            sts_expiration=900,
        )
        saved: list[AliyunCredential] = []

        class FakeClient:
            def refresh_access_token(self, refresh_token, *, now=None):
                assert refresh_token == "old-refresh"
                assert now == 1000
                return OAuthToken("new-access", "new-refresh", 4600, 0)

            def exchange_access_token_for_sts(self, access_token):
                assert access_token == "new-access"
                return OAuthStsCredentials("new-ak", "new-sk", "new-sts", 2500)

        monkeypatch.setattr(AliyunCredentials, "save", saved.append)

        refreshed = AliyunCredentials.refresh_oauth_if_needed(cred, oauth_client=FakeClient(), now=1000)

        assert refreshed is cred
        assert saved == [cred]
        assert cred.oauth_access_token == "new-access"
        assert cred.oauth_refresh_token == "new-refresh"
        assert cred.oauth_access_token_expire == 4600
        assert cred.oauth_refresh_token_expire == 0
        assert cred.access_key_id == "new-ak"
        assert cred.access_key_secret == "new-sk"
        assert cred.sts_token == "new-sts"
        assert cred.sts_expiration == 2500

    def test_refresh_oauth_requires_relogin_when_refresh_token_missing(self):
        cred = AliyunCredential(
            mode="OAuth",
            oauth_site_type="CN",
            oauth_access_token="old-access",
            oauth_access_token_expire=900,
            access_key_id="old-ak",
            access_key_secret="old-sk",
            sts_token="old-sts",
            sts_expiration=900,
        )

        with pytest.raises(AliyunOAuthReloginRequired, match="/auth"):
            AliyunCredentials.refresh_oauth_if_needed(cred, oauth_client=object(), now=1000)

    def test_refresh_oauth_closes_internally_created_client(self, monkeypatch):
        cred = AliyunCredential(
            mode="OAuth",
            oauth_site_type="CN",
            oauth_access_token="access",
            oauth_refresh_token="refresh",
            oauth_access_token_expire=2000,
            sts_expiration=900,
        )
        clients = []

        class FakeClient:
            def __init__(self, site):
                self.site = site
                self.closed = False
                clients.append(self)

            def exchange_access_token_for_sts(self, access_token):
                assert access_token == "access"
                return OAuthStsCredentials("new-ak", "new-sk", "new-sts", 2500)

            def close(self):
                self.closed = True

        monkeypatch.setattr("iac_code.services.providers.aliyun_oauth.AliyunOAuthClient", FakeClient)
        monkeypatch.setattr(AliyunCredentials, "save", lambda credential: None)

        AliyunCredentials.refresh_oauth_if_needed(cred, now=1000)

        assert len(clients) == 1
        assert clients[0].closed is True

    def test_refresh_oauth_leaves_supplied_client_open(self, monkeypatch):
        cred = AliyunCredential(
            mode="OAuth",
            oauth_site_type="CN",
            oauth_access_token="access",
            oauth_refresh_token="refresh",
            oauth_access_token_expire=2000,
            sts_expiration=900,
        )

        class FakeClient:
            closed = False

            def exchange_access_token_for_sts(self, access_token):
                return OAuthStsCredentials("new-ak", "new-sk", "new-sts", 2500)

            def close(self):
                self.closed = True

        client = FakeClient()
        monkeypatch.setattr(AliyunCredentials, "save", lambda credential: None)

        AliyunCredentials.refresh_oauth_if_needed(cred, oauth_client=client, now=1000)

        assert client.closed is False

    def test_refresh_oauth_uses_falsey_supplied_client_without_closing_it(self, monkeypatch):
        cred = AliyunCredential(
            mode="OAuth",
            oauth_site_type="CN",
            oauth_access_token="access",
            oauth_refresh_token="refresh",
            oauth_access_token_expire=2000,
            sts_expiration=900,
        )
        saved: list[AliyunCredential] = []

        class FalseyClient:
            closed = False
            exchanged = False

            def __bool__(self):
                return False

            def exchange_access_token_for_sts(self, access_token):
                assert access_token == "access"
                self.exchanged = True
                return OAuthStsCredentials("new-ak", "new-sk", "new-sts", 2500)

            def close(self):
                self.closed = True

        def fail_internal_client(site):
            raise AssertionError("internal OAuth client should not be constructed")

        client = FalseyClient()
        monkeypatch.setattr("iac_code.services.providers.aliyun_oauth.AliyunOAuthClient", fail_internal_client)
        monkeypatch.setattr(AliyunCredentials, "save", saved.append)

        refreshed = AliyunCredentials.refresh_oauth_if_needed(cred, oauth_client=client, now=1000)

        assert refreshed is cred
        assert saved == [cred]
        assert client.exchanged is True
        assert client.closed is False

    def test_refresh_oauth_returns_non_oauth_credentials_without_network(self, monkeypatch):
        cred = AliyunCredential(
            mode="AK",
            access_key_id="ak",
            access_key_secret="sk",
        )

        class FailingClient:
            def refresh_access_token(self, refresh_token, *, now=None):
                raise AssertionError("refresh_access_token should not be called")

            def exchange_access_token_for_sts(self, access_token):
                raise AssertionError("exchange_access_token_for_sts should not be called")

        monkeypatch.setattr(
            AliyunCredentials,
            "save",
            lambda credential: (_ for _ in ()).throw(AssertionError("save should not be called")),
        )

        refreshed = AliyunCredentials.refresh_oauth_if_needed(cred, oauth_client=FailingClient(), now=1000)

        assert refreshed is cred
        assert cred.access_key_id == "ak"
        assert cred.access_key_secret == "sk"

    def test_refresh_oauth_requires_relogin_when_oauth_site_type_missing(self):
        cred = AliyunCredential(
            mode="OAuth",
            oauth_access_token="access",
            oauth_refresh_token="refresh",
            oauth_access_token_expire=2000,
            access_key_id="old-ak",
            access_key_secret="old-sk",
            sts_token="old-sts",
            sts_expiration=900,
        )

        with pytest.raises(AliyunOAuthReloginRequired, match="/auth"):
            AliyunCredentials.refresh_oauth_if_needed(cred, oauth_client=object(), now=1000)


class TestAliyunCredentialsIsConfigured:
    def test_is_configured_true_with_env_vars(self):
        env = {
            "ALIBABA_CLOUD_ACCESS_KEY_ID": "id",
            "ALIBABA_CLOUD_ACCESS_KEY_SECRET": "secret",
        }
        with patch.dict(os.environ, env, clear=False):
            assert AliyunCredentials.is_configured() is True

    def test_is_configured_true_with_config_file(self, tmp_path):
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
        config_file.write_text(json.dumps(config), encoding="utf-8")

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ALIBABA_CLOUD_ACCESS_KEY_ID", None)
            os.environ.pop("ALIBABA_CLOUD_ACCESS_KEY_SECRET", None)
            os.environ.pop("ALIBABA_CLOUD_REGION_ID", None)
            result = AliyunCredentials.is_configured(config_path=str(config_file))
        assert result is True

    def test_is_configured_false_when_unconfigured(self, tmp_path):
        config_file = tmp_path / "nonexistent.json"
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ALIBABA_CLOUD_ACCESS_KEY_ID", None)
            os.environ.pop("ALIBABA_CLOUD_ACCESS_KEY_SECRET", None)
            os.environ.pop("ALIBABA_CLOUD_REGION_ID", None)
            result = AliyunCredentials.is_configured(config_path=str(config_file))
        assert result is False
