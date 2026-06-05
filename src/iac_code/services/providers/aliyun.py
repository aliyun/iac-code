import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from iac_code.config import _load_yaml, _save_yaml, get_cloud_credentials_path
from iac_code.i18n import _

DEFAULT_REGION = "cn-hangzhou"
DEFAULT_ALIYUN_CLI_CONFIG_PATH = os.path.expanduser("~/.aliyun/config.json")

# Credential modes matching aliyun CLI
CREDENTIAL_MODES = ["AK", "StsToken", "RamRoleArn", "OAuth"]

# Fields definition for each credential mode
# Each field: (name, label, sensitive)
MODE_FIELDS: dict[str, list[tuple[str, str, bool]]] = {
    "AK": [
        ("access_key_id", "AccessKey ID", True),
        ("access_key_secret", "AccessKey Secret", True),
    ],
    "StsToken": [
        ("access_key_id", "AccessKey ID", True),
        ("access_key_secret", "AccessKey Secret", True),
        ("sts_token", "STS Token", True),
    ],
    "RamRoleArn": [
        ("access_key_id", "AccessKey ID", True),
        ("access_key_secret", "AccessKey Secret", True),
        ("ram_role_arn", "RAM Role ARN", False),
        ("ram_session_name", "Session Name", False),
    ],
    "OAuth": [
        ("oauth_site_type", "OAuth Site Type", False),
        ("oauth_access_token", "OAuth Access Token", True),
        ("oauth_refresh_token", "OAuth Refresh Token", True),
        ("oauth_access_token_expire", "OAuth Access Token Expire", False),
        ("oauth_refresh_token_expire", "OAuth Refresh Token Expire", False),
        ("access_key_id", "AccessKey ID", True),
        ("access_key_secret", "AccessKey Secret", True),
        ("sts_token", "STS Token", True),
        ("sts_expiration", "STS Expiration", False),
    ],
}

# Display names for credential modes (English, translatable via i18n)
MODE_DISPLAY_NAMES: dict[str, str] = {
    "AK": "AccessKey",
    "StsToken": "STS Token",
    "RamRoleArn": "RAM Role",
    "OAuth": "OAuth Login (Browser)",
}


@dataclass
class AliyunCredential:
    mode: str = "AK"
    access_key_id: str = ""
    access_key_secret: str = ""
    region_id: str = field(default=DEFAULT_REGION)
    sts_token: str = ""
    sts_expiration: int = 0
    ram_role_arn: str = ""
    ram_session_name: str = ""
    oauth_site_type: str = ""
    oauth_access_token: str = ""
    oauth_refresh_token: str = ""
    oauth_access_token_expire: int = 0
    oauth_refresh_token_expire: int = 0


def mask_sensitive(value: str) -> str:
    """Mask a sensitive value with '*' characters of the same length."""
    if not value:
        return ""
    return "*" * len(value)


class AliyunCredentials:
    @staticmethod
    def load(config_path: str | None = None) -> AliyunCredential | None:
        """Load credentials with priority: env vars > iac-code config > aliyun CLI config."""
        # Try environment variables first
        access_key_id = os.environ.get("ALIBABA_CLOUD_ACCESS_KEY_ID")
        access_key_secret = os.environ.get("ALIBABA_CLOUD_ACCESS_KEY_SECRET")
        if access_key_id and access_key_secret:
            region_id = os.environ.get("ALIBABA_CLOUD_REGION_ID")
            if not region_id:
                # Env vars don't specify region — walk the file fallback chain:
                # iac-code config → aliyun CLI config → DEFAULT_REGION.
                iac_cred = AliyunCredentials._load_from_iac_code_config()
                if iac_cred and iac_cred.region_id:
                    region_id = iac_cred.region_id
                else:
                    cli_cred = AliyunCredentials._load_from_aliyun_cli(config_path)
                    region_id = (cli_cred.region_id if cli_cred else None) or DEFAULT_REGION
            sts_token = os.environ.get("ALIBABA_CLOUD_SECURITY_TOKEN", "")
            mode = "StsToken" if sts_token else "AK"
            return AliyunCredential(
                mode=mode,
                access_key_id=access_key_id,
                access_key_secret=access_key_secret,
                region_id=region_id,
                sts_token=sts_token,
            )

        # Try iac-code config
        if config_path is None:
            cred = AliyunCredentials._load_from_iac_code_config()
            if cred is not None:
                return cred

        # Fall back to aliyun CLI config
        return AliyunCredentials._load_from_aliyun_cli(config_path)

    @staticmethod
    def refresh_oauth_if_needed(
        credential: AliyunCredential,
        *,
        oauth_client: Any | None = None,
        now: int | None = None,
    ) -> AliyunCredential:
        """Refresh OAuth-backed STS credentials before Alibaba Cloud API use."""
        from iac_code.services.providers.aliyun_oauth import (
            ACCESS_TOKEN_SKEW_SECONDS,
            STS_SKEW_SECONDS,
            AliyunOAuthClient,
            AliyunOAuthReloginRequired,
            get_oauth_site,
            is_epoch_expired,
        )

        if credential.mode != "OAuth":
            return credential

        current = int(time.time()) if now is None else now
        has_sts = bool(credential.access_key_id and credential.access_key_secret and credential.sts_token)
        if has_sts and not is_epoch_expired(credential.sts_expiration, current, STS_SKEW_SECONDS):
            return credential

        if not credential.oauth_site_type:
            raise AliyunOAuthReloginRequired(_("Alibaba Cloud OAuth site is missing."))

        owns_client = oauth_client is None
        client = AliyunOAuthClient(get_oauth_site(credential.oauth_site_type)) if oauth_client is None else oauth_client

        try:
            if is_epoch_expired(credential.oauth_access_token_expire, current, ACCESS_TOKEN_SKEW_SECONDS):
                if not credential.oauth_refresh_token:
                    raise AliyunOAuthReloginRequired(_("Alibaba Cloud OAuth refresh token is missing."))
                token = client.refresh_access_token(credential.oauth_refresh_token, now=current)
                credential.oauth_access_token = token.access_token
                credential.oauth_refresh_token = token.refresh_token
                credential.oauth_access_token_expire = token.access_token_expire
                credential.oauth_refresh_token_expire = token.refresh_token_expire

            if not credential.oauth_access_token:
                raise AliyunOAuthReloginRequired(_("Alibaba Cloud OAuth access token is missing."))

            sts = client.exchange_access_token_for_sts(credential.oauth_access_token)
            credential.access_key_id = sts.access_key_id
            credential.access_key_secret = sts.access_key_secret
            credential.sts_token = sts.sts_token
            credential.sts_expiration = sts.sts_expiration

            AliyunCredentials.save(credential)
            return credential
        finally:
            if owns_client:
                client.close()

    @staticmethod
    def _load_from_iac_code_config() -> AliyunCredential | None:
        """Load credentials from ~/.iac-code/.cloud-credentials.yml."""
        cloud_creds = _load_yaml(get_cloud_credentials_path())
        aliyun_data = cloud_creds.get("aliyun")
        if not aliyun_data or not isinstance(aliyun_data, dict):
            return None

        mode = aliyun_data.get("mode", "AK")
        if mode not in CREDENTIAL_MODES:
            return None

        return AliyunCredential(
            mode=mode,
            access_key_id=aliyun_data.get("access_key_id", ""),
            access_key_secret=aliyun_data.get("access_key_secret", ""),
            region_id=aliyun_data.get("region_id", DEFAULT_REGION),
            sts_token=aliyun_data.get("sts_token", ""),
            sts_expiration=int(aliyun_data.get("sts_expiration") or 0),
            ram_role_arn=aliyun_data.get("ram_role_arn", ""),
            ram_session_name=aliyun_data.get("ram_session_name", ""),
            oauth_site_type=aliyun_data.get("oauth_site_type", ""),
            oauth_access_token=aliyun_data.get("oauth_access_token", ""),
            oauth_refresh_token=aliyun_data.get("oauth_refresh_token", ""),
            oauth_access_token_expire=int(aliyun_data.get("oauth_access_token_expire") or 0),
            oauth_refresh_token_expire=int(aliyun_data.get("oauth_refresh_token_expire") or 0),
        )

    @staticmethod
    def _load_from_aliyun_cli(config_path: str | None = None) -> AliyunCredential | None:
        """Load credentials from aliyun CLI config file (~/.aliyun/config.json)."""
        path = Path(config_path) if config_path else Path(DEFAULT_ALIYUN_CLI_CONFIG_PATH)
        if not path.exists():
            return None

        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None

        if not isinstance(data, dict):
            return None

        raw_profiles = data.get("profiles", [])
        if not isinstance(raw_profiles, list):
            return None

        profiles = {
            profile["name"]: profile
            for profile in raw_profiles
            if isinstance(profile, dict) and isinstance(profile.get("name"), str)
        }
        profile = profiles.get("default")
        if not profile:
            return None

        mode = profile.get("mode", "AK")
        try:
            sts_expiration = int(profile.get("sts_expiration") or 0)
            oauth_access_token_expire = int(profile.get("oauth_access_token_expire") or 0)
            oauth_refresh_token_expire = int(profile.get("oauth_refresh_token_expire") or 0)
        except (TypeError, ValueError):
            return None

        return AliyunCredential(
            mode=mode,
            access_key_id=profile.get("access_key_id", ""),
            access_key_secret=profile.get("access_key_secret", ""),
            region_id=profile.get("region_id", DEFAULT_REGION),
            sts_token=profile.get("sts_token", ""),
            sts_expiration=sts_expiration,
            ram_role_arn=profile.get("ram_role_arn", ""),
            ram_session_name=profile.get("ram_session_name", ""),
            oauth_site_type=profile.get("oauth_site_type", ""),
            oauth_access_token=profile.get("oauth_access_token", ""),
            oauth_refresh_token=profile.get("oauth_refresh_token", ""),
            oauth_access_token_expire=oauth_access_token_expire,
            oauth_refresh_token_expire=oauth_refresh_token_expire,
        )

    @staticmethod
    def load_from_aliyun_cli(config_path: str | None = None) -> AliyunCredential | None:
        """Public method to load credentials from aliyun CLI config only.

        Used by auth UI to display existing aliyun CLI config with masking.
        """
        return AliyunCredentials._load_from_aliyun_cli(config_path)

    @staticmethod
    def save(
        credential: AliyunCredential,
        config_path: str | None = None,
    ) -> None:
        """Save credentials to ~/.iac-code/.cloud-credentials.yml."""
        if config_path is not None:
            # For testing: save to specified path in aliyun CLI format
            AliyunCredentials._save_to_aliyun_cli_format(credential, config_path)
            return

        path = get_cloud_credentials_path()
        cloud_creds = _load_yaml(path)

        aliyun_data: dict[str, Any] = {
            "mode": credential.mode,
            "region_id": credential.region_id,
        }

        # Save fields relevant to the credential mode
        mode_fields = MODE_FIELDS.get(credential.mode, [])
        for field_name, _label, _sensitive in mode_fields:
            value = getattr(credential, field_name, "")
            if value in ("", None):
                continue
            if (
                field_name
                in {
                    "sts_expiration",
                    "oauth_access_token_expire",
                    "oauth_refresh_token_expire",
                }
                and value == 0
            ):
                continue
            aliyun_data[field_name] = value

        cloud_creds["aliyun"] = aliyun_data
        _save_yaml(path, cloud_creds)

    @staticmethod
    def _save_to_aliyun_cli_format(credential: AliyunCredential, config_path: str) -> None:
        """Save credentials in aliyun CLI JSON format (for testing)."""
        from typing import cast

        path = Path(config_path)
        path.parent.mkdir(parents=True, exist_ok=True)

        data: dict[str, object] = {"current": "default", "profiles": []}
        if path.exists():
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    data = loaded
            except (json.JSONDecodeError, OSError):
                pass

        updated_profile: dict[str, Any] = {
            "name": "default",
            "mode": credential.mode,
            "access_key_id": credential.access_key_id,
            "access_key_secret": credential.access_key_secret,
            "region_id": credential.region_id,
            "sts_token": credential.sts_token,
            "sts_expiration": credential.sts_expiration,
            "ram_role_arn": credential.ram_role_arn,
            "ram_session_name": credential.ram_session_name,
            "oauth_site_type": credential.oauth_site_type,
            "oauth_access_token": credential.oauth_access_token,
            "oauth_refresh_token": credential.oauth_refresh_token,
            "oauth_access_token_expire": credential.oauth_access_token_expire,
            "oauth_refresh_token_expire": credential.oauth_refresh_token_expire,
        }

        raw_profiles = data.get("profiles")
        profiles: list[dict[str, Any]] = (
            cast(list[dict[str, Any]], raw_profiles) if isinstance(raw_profiles, list) else []
        )

        for i, profile in enumerate(profiles):
            if isinstance(profile, dict) and profile.get("name") == "default":
                profiles[i] = updated_profile
                break
        else:
            profiles.append(updated_profile)

        data["profiles"] = profiles
        if "current" not in data:
            data["current"] = "default"

        path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")

    @staticmethod
    def is_configured(config_path: str | None = None) -> bool:
        """Check if credentials are available."""
        return AliyunCredentials.load(config_path=config_path) is not None
