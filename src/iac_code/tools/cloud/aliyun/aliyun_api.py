"""Generic Alibaba Cloud API tool using OpenAPI SDK."""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

import yaml
from alibabacloud_tea_openapi import models as open_api_models
from alibabacloud_tea_openapi.client import Client as OpenApiClient
from darabonba.runtime import RuntimeOptions

from iac_code.i18n import _
from iac_code.services.cloud_credentials import CloudCredentials
from iac_code.services.providers.aliyun import AliyunCredential
from iac_code.services.telemetry import add_metric, log_event
from iac_code.services.telemetry.names import Events, Metrics
from iac_code.services.telemetry.sanitize import sanitize_error_message
from iac_code.tools.base import ToolContext, ToolResult
from iac_code.tools.cloud.aliyun.user_agent import build_user_agent
from iac_code.tools.cloud.base_api import BaseCloudApi

logger = logging.getLogger(__name__)

VERSION_MAP = {
    "ros": "2019-09-10",
    "ecs": "2014-05-26",
    "rds": "2014-08-15",
    "r-kvstore": "2015-01-01",
    "slb": "2014-05-15",
    "alb": "2024-03-27",
    "nlb": "2022-04-30",
    "vpc": "2016-04-28",
    "oss": "2019-05-17",
    "IaCService": "2021-08-06",
}

# Endpoint config loaded from endpoints.yml
_ENDPOINTS_FILE = Path(__file__).parent / "endpoints.yml"


def _load_endpoints() -> dict[str, Any]:
    data = yaml.safe_load(_ENDPOINTS_FILE.read_text(encoding="utf-8")) or {}
    # Convert region lists to sets for O(1) lookup
    for config in data.values():
        for key in ("regional", "central"):
            section = config.get(key)
            if section and "regions" in section:
                section["regions"] = set(section["regions"])
    return data


_ENDPOINTS: dict[str, Any] = _load_endpoints()

# Case-insensitive lookup tables for product codes (built once at module load)
_VERSION_MAP_LOWER: dict[str, str] = {k.lower(): v for k, v in VERSION_MAP.items()}
_PRODUCT_CANONICAL: dict[str, str] = {k.lower(): k for k in VERSION_MAP}
_ENDPOINTS_CANONICAL: dict[str, str] = {k.lower(): k for k in _ENDPOINTS}

# Cache for Location service discovered endpoints
_endpoint_cache: dict[tuple[str, str], str | None] = {}

# Error categories for template validation
_VALIDATE_ERROR_CATEGORIES: dict[str, str] = {
    "InvalidTemplateURL": "invalid_url",
    "InvalidTemplate": "invalid_template",
    "TemplateNotFound": "not_found",
    "AccessDenied": "access_denied",
    "InvalidJSON": "invalid_json",
    "InvalidYAML": "invalid_yaml",
}


def _extract_error_info(error_str: str) -> tuple[str | None, str | None]:
    """Extract error code and message from exception string.

    Aliyun errors typically come in formats like:
    - "InvalidTemplate Response: {...}"
    - "InvalidAction.NotFound: The specified action is not found."
    """
    error_code = None
    error_message = None

    if not error_str:
        return error_code, error_message

    # Try to extract error code (first word before space or colon)
    parts = error_str.split()
    if parts:
        first_part = parts[0].rstrip(":")
        if not first_part.startswith("{"):  # Skip JSON fragments
            error_code = first_part

    # Remove "Response: {...}" suffix to get clean message
    if "Response:" in error_str:
        error_message = error_str.split("Response:")[0].strip()
    else:
        error_message = error_str

    return error_code, error_message


def _emit_validate_template_event(response_body: dict | Any, duration_ms: int) -> None:
    """Emit TEMPLATE_VALIDATED event for ROS ValidateTemplate action.

    Maps response outcome to pass/fail and classifies error if present.
    """
    outcome = "pass"
    error_category = None

    # Check if response contains validation errors
    if isinstance(response_body, dict):
        errors = response_body.get("Errors")
        if errors and len(errors) > 0:
            outcome = "fail"
            # Try to classify the first error
            first_error = errors[0] if isinstance(errors, list) else errors
            if isinstance(first_error, dict):
                error_key = first_error.get("ErrorCode") or first_error.get("Type", "")
                # Look up error category from mapping
                for pattern, category in _VALIDATE_ERROR_CATEGORIES.items():
                    if pattern in error_key:
                        error_category = category
                        break
                if not error_category:
                    error_category = "other"

    log_event(
        Events.TEMPLATE_VALIDATED,
        {
            "outcome": outcome,
            "duration_ms": duration_ms,
            "error_category": error_category,
        },
    )
    add_metric(
        Metrics.TEMPLATE_VALIDATED_COUNT,
        1,
        {"outcome": outcome},
    )


class AliyunApi(BaseCloudApi):
    """Generic Alibaba Cloud API tool.

    Can call any Aliyun product API through the common OpenAPI SDK.
    """

    @property
    def provider_name(self) -> str:
        return "aliyun"

    @property
    def supported_actions(self) -> list[str]:
        return []

    async def call_action(self, action: str, params: dict, region: str) -> dict:
        raise NotImplementedError("AliyunApi uses execute() directly, not call_action()")

    @property
    def description(self) -> str:
        return (
            "Call any Alibaba Cloud product API through the common OpenAPI SDK. "
            "Supports ECS, RDS, Redis, SLB, ALB, VPC, OSS, ROS, and more."
        )

    def user_facing_name(self, input: dict | None = None) -> str:
        return _("Aliyun API")

    def _get_default_region(self) -> str:
        credentials = CloudCredentials()
        cred = credentials.get_provider("aliyun")
        return cred.region_id if cred else ""

    def is_read_only(self, input: dict | None = None) -> bool:
        if input is None:
            return False
        product = input.get("product", "")
        action = input.get("action", "")
        if product == "ros" and action == "PreviewStack":
            return True
        return super().is_read_only(input)

    @property
    def input_schema(self) -> dict[str, Any]:
        region_desc = "The region to call the action in."
        default_region = self._get_default_region()
        if default_region:
            region_desc += f" Defaults to '{default_region}'."
        return {
            "type": "object",
            "properties": {
                "product": {
                    "type": "string",
                    "description": "The Aliyun product code (e.g. 'ros', 'ecs', 'rds', 'vpc').",
                },
                "action": {
                    "type": "string",
                    "description": "The API action to call.",
                },
                "version": {
                    "type": "string",
                    "description": (
                        "API version. Optional for common products: "
                        + ", ".join(f"{k}({v})" for k, v in VERSION_MAP.items())
                        + "."
                    ),
                },
                "params": {
                    "type": "object",
                    "description": "Parameters to pass to the action.",
                },
                "region_id": {
                    "type": "string",
                    "description": region_desc,
                },
                "style": {
                    "type": "string",
                    "enum": ["RPC", "ROA"],
                    "description": "API style. Defaults to 'RPC'. Use 'ROA' for RESTful APIs (e.g. CS, CR, FC).",
                },
                "method": {
                    "type": "string",
                    "enum": ["GET", "POST", "PUT", "DELETE"],
                    "description": "HTTP method. Defaults to 'POST'. Only needed for ROA APIs.",
                },
                "pathname": {
                    "type": "string",
                    "description": "Request path. Defaults to '/'. Only needed for ROA APIs (e.g. '/clusters').",
                },
                "body": {
                    "type": "object",
                    "description": "Request body. Only needed for ROA POST/PUT APIs.",
                },
            },
            "required": ["product", "action"],
        }

    def _resolve_version(self, input: dict) -> str:
        """Resolve the API version from input or built-in map."""
        explicit = input.get("version")
        if explicit:
            return explicit
        product = input.get("product", "")
        if product in VERSION_MAP:
            return VERSION_MAP[product]
        version = _VERSION_MAP_LOWER.get(product.lower())
        if version:
            return version
        raise ValueError(
            f"No built-in version for product '{product}'. Please provide an explicit 'version' parameter."
        )

    @staticmethod
    def _get_endpoint(product: str, region_id: str = "") -> str | None:
        """Resolve endpoint from endpoints.yml. Returns None if not found."""
        config = _ENDPOINTS.get(product)
        if config is None:
            canonical = _ENDPOINTS_CANONICAL.get(product.lower())
            if canonical:
                config = _ENDPOINTS[canonical]
            else:
                return None
        # Global central endpoint (all regions)
        if "endpoint" in config:
            return config["endpoint"]
        if not region_id:
            return None
        # Central override for specific regions
        central = config.get("central")
        if central and region_id in central.get("regions", set()):
            return central["endpoint"]
        # Regionalized: mapping (priority) → pattern + regions
        regional = config.get("regional")
        if regional:
            mapping = regional.get("mapping", {})
            if region_id in mapping:
                return mapping[region_id]
            if region_id in regional.get("regions", set()):
                return regional["pattern"].format(region_id=region_id)
        return None

    def _discover_endpoint(self, product: str, region_id: str, credential: AliyunCredential) -> str | None:
        """Discover endpoint via Location service. Results are cached in memory."""
        if not region_id:
            return None
        cache_key = (product, region_id)
        if cache_key in _endpoint_cache:
            return _endpoint_cache[cache_key]
        try:
            config = self._build_config(credential, "location.aliyuncs.com", region_id)
            client = OpenApiClient(config)
            api_params = open_api_models.Params(
                action="DescribeEndpoints",
                version="2015-06-12",
                protocol="HTTPS",
                pathname="/",
                method="POST",
                auth_type="AK",
                style="RPC",
                body_type="json",
                req_body_type="json",
            )
            request = open_api_models.OpenApiRequest(
                query={"Id": region_id, "ServiceCode": product},
            )
            result = client.call_api(api_params, request, RuntimeOptions())
            body = result.get("body", result)
            for ep in body.get("Endpoints", {}).get("Endpoint", []):
                if ep.get("Type") == "openAPI":
                    endpoint = ep.get("Endpoint", "")
                    if endpoint:
                        _endpoint_cache[cache_key] = endpoint
                        return endpoint
            _endpoint_cache[cache_key] = None
            return None
        except Exception:
            _endpoint_cache[cache_key] = None
            return None

    @staticmethod
    def _get_endpoint_fallback(product: str, region_id: str = "") -> str:
        """Last resort fallback endpoint."""
        if region_id:
            return f"{product}.{region_id}.aliyuncs.com"
        return f"{product}.aliyuncs.com"

    @staticmethod
    def _build_config(credential: AliyunCredential, endpoint: str, region_id: str) -> open_api_models.Config:
        """Build OpenAPI config from credential, endpoint, and region."""
        mode = credential.mode
        user_agent = build_user_agent()

        if mode == "StsToken":
            return open_api_models.Config(
                access_key_id=credential.access_key_id,
                access_key_secret=credential.access_key_secret,
                security_token=credential.sts_token,
                endpoint=endpoint,
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
                endpoint=endpoint,
                region_id=region_id,
                user_agent=user_agent,
            )

        # Default: AK mode
        return open_api_models.Config(
            access_key_id=credential.access_key_id,
            access_key_secret=credential.access_key_secret,
            endpoint=endpoint,
            region_id=region_id,
            user_agent=user_agent,
        )

    @staticmethod
    def _serialize_params(params: dict) -> dict[str, str]:
        """Convert param values for query string."""
        result: dict[str, str] = {}
        for k, v in params.items():
            if isinstance(v, str):
                result[k] = v
            elif isinstance(v, bool):
                result[k] = "true" if v else "false"
            elif isinstance(v, (dict, list)):
                result[k] = json.dumps(v, ensure_ascii=False)
            else:
                result[k] = str(v)
        return result

    def _get_action_display_detail(self, input: dict) -> str:
        product = input.get("product", "")
        region = self._resolve_region(input)
        return f"{product} {region}".strip()

    def _summarize_success_result(self, action: str, result: dict) -> str:
        request_id = result.get("RequestId") if isinstance(result, dict) else None
        if request_id:
            return _("Call succeeded (RequestId: {request_id})").format(request_id=request_id)
        return _("Call succeeded")

    async def execute(self, *, tool_input: dict[str, Any], context: ToolContext) -> ToolResult:
        product = tool_input.get("product", "")
        product = _PRODUCT_CANONICAL.get(product.lower(), product)
        action = tool_input.get("action", "")
        params = tool_input.get("params") or {}
        region = self._resolve_region(tool_input)

        # ROS: TemplateURL as local file path → read into TemplateBody
        if product == "ros":
            template_url = params.get("TemplateURL", "")
            if template_url and not template_url.startswith(("http://", "https://", "oss://")):
                params["TemplateBody"] = Path(template_url).read_text(encoding="utf-8")
                del params["TemplateURL"]

        # Pre-call hooks (e.g. resource type validation)
        from iac_code.tools.cloud.aliyun.api_hooks import run_hooks

        if hook_result := run_hooks(product, action, params):
            return hook_result

        try:
            version = self._resolve_version(tool_input)
        except ValueError as e:
            return ToolResult.error(str(e))

        credentials = CloudCredentials()
        credential = credentials.get_provider("aliyun")
        if credential is None:
            return ToolResult.error(
                "Alibaba Cloud credentials not configured. "
                "Run 'iac-code auth' and select 'Cloud Provider' to configure."
            )

        endpoint = (
            self._get_endpoint(product, region)
            or self._discover_endpoint(product, region, credential)
            or self._get_endpoint_fallback(product, region)
        )
        config = self._build_config(credential, endpoint, region)
        client = OpenApiClient(config)

        style = tool_input.get("style", "RPC")
        method = tool_input.get("method", "POST")
        pathname = tool_input.get("pathname", "/")
        body = tool_input.get("body")

        api_params = open_api_models.Params(
            action=action,
            version=version,
            protocol="HTTPS",
            pathname=pathname,
            method=method,
            auth_type="AK",
            style=style,
            body_type="json",
            req_body_type="json",
        )

        if style == "ROA":
            # ROA: params go to query, body goes to body
            serialized = self._serialize_params(params)
            request = open_api_models.OpenApiRequest(
                query=serialized,
                body=body,
            )
        else:
            # RPC: ensure RegionId is in params
            if region:
                params.setdefault("RegionId", region)
            serialized = self._serialize_params(params)
            request = open_api_models.OpenApiRequest(query=serialized)
        runtime = RuntimeOptions()

        # Prepare telemetry metadata
        api_service = product.upper()
        started = time.monotonic()
        http_status: int | None = None
        error_code: str | None = None
        error_message: str | None = None
        outcome = "success"

        try:
            result = client.call_api(api_params, request, runtime)
            body = result.get("body", result)

            # Try to extract HTTP status from response
            if isinstance(result, dict) and "http_status" in result:
                http_status = result.get("http_status")

            self._last_action = action
            self._last_result = body

            # Emit ALIYUN_API_CALLED event
            duration_ms = int((time.monotonic() - started) * 1000)
            log_event(
                Events.ALIYUN_API_CALLED,
                {
                    "api_service": api_service,
                    "api_name": action,
                    "api_version": version,
                    "region": region,
                    "outcome": outcome,
                    "duration_ms": duration_ms,
                    "http_status": http_status,
                },
            )
            add_metric(Metrics.ALIYUN_API_CALLED_COUNT, 1, {"api_service": api_service, "outcome": outcome})
            add_metric(Metrics.ALIYUN_API_CALLED_DURATION, duration_ms)

            # Special case: ROS ValidateTemplate
            if api_service == "ROS" and action == "ValidateTemplate":
                _emit_validate_template_event(body, duration_ms)

            return ToolResult.success(json.dumps(body, ensure_ascii=False, indent=2))
        except Exception as e:
            self._last_action = ""
            self._last_result = None
            outcome = "failure"
            duration_ms = int((time.monotonic() - started) * 1000)
            error_str = str(e)

            # Try to extract error code and message
            error_code, error_message = _extract_error_info(error_str)

            # Emit ALIYUN_API_CALLED event (with error)
            log_event(
                Events.ALIYUN_API_CALLED,
                {
                    "api_service": api_service,
                    "api_name": action,
                    "api_version": version,
                    "region": region,
                    "outcome": outcome,
                    "duration_ms": duration_ms,
                    "http_status": http_status,
                    "error_code": error_code,
                    "error_message": sanitize_error_message(error_message),
                },
            )
            add_metric(Metrics.ALIYUN_API_CALLED_COUNT, 1, {"api_service": api_service, "outcome": outcome})
            add_metric(Metrics.ALIYUN_API_CALLED_DURATION, duration_ms)

            return ToolResult.error(self._clean_error_message(error_str))
