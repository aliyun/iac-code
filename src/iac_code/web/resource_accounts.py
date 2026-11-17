"""Non-secret credential context, loaded only after an explicit UI action."""

from typing import Any

from iac_code.tools.cloud.aliyun.resource_reference_extractors import credential_reference_context
from iac_code.tools.cloud.resource_references import locator_text


def clean_resource_account_context(value: Any) -> dict[str, str]:
    if not isinstance(value, dict) or not locator_text(value.get("account_scope")):
        return {}
    result = {"account_scope": value["account_scope"]}
    if value.get("console_partition") in {"aliyun", "alibabacloud"}:
        result["console_partition"] = value["console_partition"]
    return result


def load_configured_resource_context() -> dict[str, str]:
    """Read local configuration, never refresh OAuth, resolve STS or call IMDS.

    The opaque context does not prove a cloud account ID or resource existence.
    Secrets, credential paths, account numbers and default regions stay local.
    """
    from iac_code.services.cloud_credentials import CloudCredentials

    try:
        credential = CloudCredentials().get_provider("aliyun")
        return clean_resource_account_context(credential_reference_context(credential))
    except Exception:
        return {}
