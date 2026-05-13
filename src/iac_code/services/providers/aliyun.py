import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from iac_code.config import _load_yaml, _save_yaml, get_cloud_credentials_path

DEFAULT_REGION = "cn-hangzhou"
DEFAULT_ALIYUN_CLI_CONFIG_PATH = os.path.expanduser("~/.aliyun/config.json")

# Credential modes matching aliyun CLI
CREDENTIAL_MODES = ["AK", "StsToken", "RamRoleArn"]

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
}

# Display names for credential modes (English, translatable via i18n)
MODE_DISPLAY_NAMES: dict[str, str] = {
    "AK": "AccessKey",
    "StsToken": "STS Token",
    "RamRoleArn": "RAM Role",
}


@dataclass
class AliyunCredential:
    mode: str = "AK"
    access_key_id: str = ""
    access_key_secret: str = ""
    region_id: str = field(default=DEFAULT_REGION)
    sts_token: str = ""
    ram_role_arn: str = ""
    ram_session_name: str = ""


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
            ram_role_arn=aliyun_data.get("ram_role_arn", ""),
            ram_session_name=aliyun_data.get("ram_session_name", ""),
        )

    @staticmethod
    def _load_from_aliyun_cli(config_path: str | None = None) -> AliyunCredential | None:
        """Load credentials from aliyun CLI config file (~/.aliyun/config.json)."""
        path = Path(config_path) if config_path else Path(DEFAULT_ALIYUN_CLI_CONFIG_PATH)
        if not path.exists():
            return None

        try:
            data = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            return None

        profiles = {p["name"]: p for p in data.get("profiles", [])}
        profile = profiles.get("default")
        if not profile:
            return None

        mode = profile.get("mode", "AK")
        return AliyunCredential(
            mode=mode,
            access_key_id=profile.get("access_key_id", ""),
            access_key_secret=profile.get("access_key_secret", ""),
            region_id=profile.get("region_id", DEFAULT_REGION),
            sts_token=profile.get("sts_token", ""),
            ram_role_arn=profile.get("ram_role_arn", ""),
            ram_session_name=profile.get("ram_session_name", ""),
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
            aliyun_data[field_name] = getattr(credential, field_name, "")

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
                loaded = json.loads(path.read_text())
                if isinstance(loaded, dict):
                    data = loaded
            except (json.JSONDecodeError, OSError):
                pass

        updated_profile: dict[str, str] = {
            "name": "default",
            "mode": credential.mode,
            "access_key_id": credential.access_key_id,
            "access_key_secret": credential.access_key_secret,
            "region_id": credential.region_id,
            "sts_token": credential.sts_token,
            "ram_role_arn": credential.ram_role_arn,
            "ram_session_name": credential.ram_session_name,
        }

        raw_profiles = data.get("profiles")
        profiles: list[dict[str, str]] = (
            cast(list[dict[str, str]], raw_profiles) if isinstance(raw_profiles, list) else []
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

        path.write_text(json.dumps(data, indent=2, ensure_ascii=False))

    @staticmethod
    def is_configured(config_path: str | None = None) -> bool:
        """Check if credentials are available."""
        return AliyunCredentials.load(config_path=config_path) is not None
