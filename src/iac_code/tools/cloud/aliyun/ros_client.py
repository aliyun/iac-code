from typing import Any

from alibabacloud_ros20190910.client import Client as RosClient
from alibabacloud_tea_openapi import models as open_api_models

from iac_code.services.providers.aliyun import AliyunCredential, AliyunCredentials
from iac_code.services.providers.aliyun_oauth import AliyunOAuthError
from iac_code.tools.cloud.aliyun.ecs_credential_errors import ecs_credential_error_code
from iac_code.tools.cloud.aliyun.public_errors import public_aliyun_error
from iac_code.tools.cloud.aliyun.user_agent import build_user_agent


def public_ecs_credential_message(error: BaseException, *, action: str, region: str) -> str | None:
    """Render a credential-runtime ECS failure, or `None` for unrelated failures.

    The shared helper recognizes both the adapter's own `ValueError` and the SDK envelope
    the ROS client raises when signing fails, and nothing else, so an unrelated failure
    from a ROS path is never reported to the user as an ECS credential problem.
    """
    code = ecs_credential_error_code(error)
    if code is None:
        return None
    return public_aliyun_error(code, product="ros", action=action, region_id=region)


class RosClientFactory:
    @staticmethod
    def create(credential: AliyunCredential | None, region_id: str = "") -> RosClient:
        if credential is None:
            raise ValueError(
                "Alibaba Cloud credentials not configured. "
                "Run 'iac-code auth' and select 'Cloud Provider' to configure."
            )

        if credential.mode == "OAuth":
            try:
                credential = AliyunCredentials.refresh_oauth_if_needed(credential)
            except AliyunOAuthError as exc:
                raise ValueError(str(exc)) from exc

        effective_region = region_id or credential.region_id
        if not effective_region:
            raise ValueError("Region not configured. Run 'iac-code auth' and configure the region for Alibaba Cloud.")
        config = RosClientFactory._build_config(credential, effective_region)
        return RosClient(config)

    @staticmethod
    def _build_config(credential: AliyunCredential, region_id: str) -> open_api_models.Config:
        mode = credential.mode
        user_agent = build_user_agent()

        if mode in {"StsToken", "OAuth"}:
            return open_api_models.Config(
                access_key_id=credential.access_key_id,
                access_key_secret=credential.access_key_secret,
                security_token=credential.sts_token,
                region_id=region_id,
                user_agent=user_agent,
            )

        if mode in {"RamRoleArn", "EcsRamRole"}:
            from iac_code.services.providers.aliyun_credentials_runtime import aliyun_credential_runtime

            # The runtime always returns a client for these two dynamic modes; the SDK
            # client type is only available through a lazy import, hence the Any binding.
            dynamic_client: Any = aliyun_credential_runtime().sdk_client(credential)
            return open_api_models.Config(
                credential=dynamic_client,
                region_id=region_id,
                user_agent=user_agent,
            )

        # Default: AK mode
        return open_api_models.Config(
            access_key_id=credential.access_key_id,
            access_key_secret=credential.access_key_secret,
            region_id=region_id,
            user_agent=user_agent,
        )
