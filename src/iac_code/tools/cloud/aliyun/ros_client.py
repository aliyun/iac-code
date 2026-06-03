from alibabacloud_ros20190910.client import Client as RosClient
from alibabacloud_tea_openapi import models as open_api_models

from iac_code.services.providers.aliyun import AliyunCredential, AliyunCredentials
from iac_code.services.providers.aliyun_oauth import AliyunOAuthError
from iac_code.tools.cloud.aliyun.user_agent import build_user_agent


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

        if mode == "RamRoleArn":
            from alibabacloud_credentials import models as credential_models
            from alibabacloud_credentials.client import Client as CredentialClient

            cred_config = credential_models.Config(
                type="ram_role_arn",
                access_key_id=credential.access_key_id,
                access_key_secret=credential.access_key_secret,
                role_arn=credential.ram_role_arn,
                role_session_name=credential.ram_session_name or "iac-code-session",
            )
            cred_client = CredentialClient(cred_config)
            return open_api_models.Config(
                credential=cred_client,
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
