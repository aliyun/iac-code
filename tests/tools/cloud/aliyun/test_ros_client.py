import pytest
from alibabacloud_ros20190910.client import Client as RosClient

from iac_code.services.providers.aliyun import AliyunCredential
from iac_code.tools.cloud.aliyun.ros_client import RosClientFactory


@pytest.fixture
def credential():
    return AliyunCredential(
        access_key_id="test-key-id",
        access_key_secret="test-key-secret",
        region_id="cn-hangzhou",
    )


def test_create_client_from_credential(credential):
    client = RosClientFactory.create(credential)
    assert client is not None
    assert isinstance(client, RosClient)


def test_create_client_uses_override_region(credential):
    client = RosClientFactory.create(credential, region_id="cn-beijing")
    assert client is not None
    assert isinstance(client, RosClient)


def test_create_client_without_credentials_raises():
    with pytest.raises(ValueError, match="credentials"):
        RosClientFactory.create(None)


class TestRosClientFactoryModes:
    def test_none_credential_raises(self):
        from iac_code.tools.cloud.aliyun.ros_client import RosClientFactory

        with pytest.raises(ValueError, match="not configured"):
            RosClientFactory.create(None, region_id="cn-hangzhou")

    def test_no_region_raises(self):
        from iac_code.services.providers.aliyun import AliyunCredential
        from iac_code.tools.cloud.aliyun.ros_client import RosClientFactory

        cred = AliyunCredential(
            mode="AK",
            access_key_id="ak",
            access_key_secret="sk",
            region_id="",
        )
        with pytest.raises(ValueError, match="Region not configured"):
            RosClientFactory.create(cred, region_id="")

    def test_sts_token_mode_builds_config(self):
        from iac_code.services.providers.aliyun import AliyunCredential
        from iac_code.tools.cloud.aliyun.ros_client import RosClientFactory

        cred = AliyunCredential(
            mode="StsToken",
            access_key_id="ak",
            access_key_secret="sk",
            sts_token="tok",
            region_id="cn-hangzhou",
        )
        config = RosClientFactory._build_config(cred, "cn-hangzhou")
        assert config.access_key_id == "ak"
        assert config.security_token == "tok"
        assert config.region_id == "cn-hangzhou"
        assert config.user_agent and config.user_agent.startswith("iac-code/")

    def test_oauth_mode_builds_sts_config(self):
        from iac_code.services.providers.aliyun import AliyunCredential
        from iac_code.tools.cloud.aliyun.ros_client import RosClientFactory

        cred = AliyunCredential(
            mode="OAuth",
            access_key_id="tmp-ak",
            access_key_secret="tmp-sk",
            sts_token="tmp-sts",
            region_id="cn-hangzhou",
        )
        config = RosClientFactory._build_config(cred, "cn-hangzhou")
        assert config.access_key_id == "tmp-ak"
        assert config.access_key_secret == "tmp-sk"
        assert config.security_token == "tmp-sts"
        assert config.region_id == "cn-hangzhou"
        assert config.user_agent and config.user_agent.startswith("iac-code/")

    def test_create_refreshes_oauth_before_building_client(self):
        from unittest.mock import patch

        from iac_code.services.providers.aliyun import AliyunCredential
        from iac_code.tools.cloud.aliyun import ros_client

        oauth_cred = AliyunCredential(
            mode="OAuth",
            access_key_id="tmp-ak",
            access_key_secret="tmp-sk",
            sts_token="tmp-sts",
            region_id="cn-hangzhou",
            oauth_access_token="access-token",
            oauth_refresh_token="refresh-token",
        )
        refreshed = AliyunCredential(
            mode="OAuth",
            access_key_id="new-ak",
            access_key_secret="new-sk",
            sts_token="new-sts",
            region_id="cn-hangzhou",
            oauth_access_token="access-token",
            oauth_refresh_token="refresh-token",
        )

        with (
            patch.object(ros_client.AliyunCredentials, "refresh_oauth_if_needed", return_value=refreshed) as refresh,
            patch.object(ros_client, "RosClient") as client_cls,
        ):
            ros_client.RosClientFactory.create(oauth_cred)

        refresh.assert_called_once_with(oauth_cred)
        config = client_cls.call_args.args[0]
        assert config.access_key_id == "new-ak"
        assert config.access_key_secret == "new-sk"
        assert config.security_token == "new-sts"

    def test_ram_role_arn_mode_builds_config(self):
        from iac_code.services.providers.aliyun import AliyunCredential
        from iac_code.tools.cloud.aliyun.ros_client import RosClientFactory

        cred = AliyunCredential(
            mode="RamRoleArn",
            access_key_id="ak",
            access_key_secret="sk",
            ram_role_arn="acs:ram::123:role/x",
            ram_session_name="s1",
            region_id="cn-hangzhou",
        )
        config = RosClientFactory._build_config(cred, "cn-hangzhou")
        # RamRoleArn mode uses credential client, not direct AK/SK
        assert config.region_id == "cn-hangzhou"
        assert config.credential is not None
        assert config.user_agent and config.user_agent.startswith("iac-code/")

    def test_ram_role_arn_default_session_name(self):
        from iac_code.services.providers.aliyun import AliyunCredential
        from iac_code.tools.cloud.aliyun.ros_client import RosClientFactory

        cred = AliyunCredential(
            mode="RamRoleArn",
            access_key_id="ak",
            access_key_secret="sk",
            ram_role_arn="acs:ram::123:role/x",
            ram_session_name=None,
            region_id="cn-hangzhou",
        )
        # Should not raise; default session name applied internally
        _ = RosClientFactory._build_config(cred, "cn-hangzhou")
