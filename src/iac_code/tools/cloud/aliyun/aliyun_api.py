"""Generic Alibaba Cloud API tool using OpenAPI SDK."""

from __future__ import annotations

import fnmatch
import json
import logging
import re
import time
from pathlib import Path
from typing import Any

import yaml
from alibabacloud_tea_openapi import models as open_api_models
from alibabacloud_tea_openapi.client import Client as OpenApiClient
from darabonba.runtime import RuntimeOptions

from iac_code.i18n import _
from iac_code.services.cloud_credentials import CloudCredentials
from iac_code.services.permissions.audit import fingerprint_text
from iac_code.services.permissions.rule_scope import scope_for_rule_source
from iac_code.services.providers.aliyun import AliyunCredential, AliyunCredentials
from iac_code.services.providers.aliyun_oauth import AliyunOAuthError
from iac_code.services.telemetry import add_metric, log_event
from iac_code.services.telemetry.names import Events, Metrics
from iac_code.services.telemetry.sanitize import sanitize_error_message
from iac_code.tools.base import ToolContext, ToolResult
from iac_code.tools.cloud.aliyun.template_source import reject_template_body_param
from iac_code.tools.cloud.aliyun.user_agent import build_user_agent
from iac_code.tools.cloud.base_api import BaseCloudApi
from iac_code.types.permissions import (
    PermissionAuditMetadata,
    PermissionDecisionReason,
    PermissionResult,
    PermissionRuleValue,
    ToolPermissionContext,
)
from iac_code.types.stream_events import ResourceObservedEvent

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
_SAFE_API_VERSION = re.compile(r"^\d{4}-\d{2}-\d{2}$")

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


def _string_value(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


_SAFE_RULE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_SAFE_WILDCARD_SEGMENT = re.compile(r"^[A-Za-z0-9_*-]{1,128}$")
_RULE_SOURCE_ORDER = {
    "session": 5,
    "cli_arg": 4,
    "local_settings": 3,
    "project_settings": 2,
    "user_settings": 1,
}


def _canonical_product(product: str) -> str:
    return _PRODUCT_CANONICAL.get(product.lower(), product)


def _safe_exact_identifier(value: str) -> bool:
    return bool(_SAFE_RULE_ID.fullmatch(value))


def _add_telemetry_identifier(
    metadata: dict[str, Any],
    key: str,
    fingerprint_key: str,
    value: str,
    *,
    uppercase: bool = False,
) -> None:
    if not value:
        return
    if _safe_exact_identifier(value):
        metadata[key] = value.upper() if uppercase else value
    else:
        metadata[fingerprint_key] = fingerprint_text(value)


def _aliyun_api_telemetry_identifiers(product: str, action: str, region: str) -> dict[str, Any]:
    metadata: dict[str, Any] = {}
    _add_telemetry_identifier(
        metadata,
        "api_service",
        "api_service_fingerprint",
        product,
        uppercase=True,
    )
    _add_telemetry_identifier(metadata, "api_name", "api_name_fingerprint", action)
    _add_telemetry_identifier(metadata, "region", "region_fingerprint", region)
    return metadata


def _aliyun_api_metric_attrs(product: str, outcome: str) -> dict[str, str]:
    api_service = product.upper() if product and _safe_exact_identifier(product) else "unsafe"
    return {"api_service": api_service, "outcome": outcome}


def _aliyun_api_version_telemetry(version: str) -> dict[str, str]:
    if _SAFE_API_VERSION.fullmatch(version):
        return {"api_version": version}
    return {"api_version_fingerprint": fingerprint_text(version)}


def _scrub_unsafe_identifier_text(value: str | None, *identifiers: str) -> str | None:
    if value is None:
        return None
    sanitized = sanitize_error_message(value)
    if sanitized is None:
        return None
    for identifier in identifiers:
        if not identifier or _safe_exact_identifier(identifier):
            continue
        sanitized = re.sub(re.escape(identifier), fingerprint_text(identifier), sanitized, flags=re.IGNORECASE)
    return sanitized


def _parse_aliyun_rule(rule: str) -> tuple[str, str] | None:
    prefix = "aliyun_api("
    if not rule.startswith(prefix) or not rule.endswith(")"):
        return None
    inner = rule[len(prefix) : -1]
    if inner.count(":") != 1:
        return None
    product_pattern, action_pattern = inner.split(":", 1)
    if not (_SAFE_WILDCARD_SEGMENT.fullmatch(product_pattern) and _SAFE_WILDCARD_SEGMENT.fullmatch(action_pattern)):
        return None
    return product_pattern, action_pattern


def _literal_count(pattern: str) -> int:
    return len(pattern.replace("*", ""))


def _side_specificity(pattern: str, value: str) -> tuple[int, int]:
    if pattern.lower() == value.lower():
        return (3, len(pattern))
    if pattern == "*":
        return (1, 0)
    return (2, _literal_count(pattern))


def _safe_operation_identifiers(input: dict) -> tuple[str, str] | None:
    product = _string_value(input.get("product"))
    action = _string_value(input.get("action"))
    if product is None or action is None:
        return None
    canonical_product = _canonical_product(product)
    if not (_safe_exact_identifier(canonical_product) and _safe_exact_identifier(action)):
        return None
    return canonical_product, action


def _is_roa_style(input: dict) -> bool:
    style = _string_value(input.get("style"))
    return style is not None and style.upper() == "ROA"


def _is_roa_read_only_request(input: dict) -> bool:
    method = _string_value(input.get("method"))
    if method is None or method.upper() != "GET":
        return False
    return "body" not in input or input.get("body") is None


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

    @property
    def supports_blanket_allow(self) -> bool:
        return False

    def _get_default_region(self) -> str:
        credentials = CloudCredentials()
        cred = credentials.get_provider("aliyun")
        return cred.region_id if cred else ""

    def is_read_only(self, input: dict | None = None) -> bool:
        if input is None:
            return False
        action = _string_value(input.get("action"))
        if action is None or not _safe_exact_identifier(action):
            return False
        product = _string_value(input.get("product")) or ""
        if product:
            operation = _safe_operation_identifiers(input)
            if operation is None:
                return False
            product, action = operation
        if _is_roa_style(input) and not _is_roa_read_only_request(input):
            return False
        if product.lower() == "ros" and action.lower() == "previewstack":
            return True
        return super().is_read_only({"action": action})

    def _operation_metadata(self, input: dict) -> dict[str, object]:
        product = _string_value(input.get("product"))
        action = _string_value(input.get("action"))
        region = _string_value(input.get("region_id"))
        operation: dict[str, object] = {}
        if product is not None:
            canonical_product = _canonical_product(product)
            if _safe_exact_identifier(canonical_product):
                operation["product"] = canonical_product
            else:
                operation["product_fingerprint"] = fingerprint_text(product)
        if action is not None and _safe_exact_identifier(action):
            operation["action"] = action
        elif action is not None:
            operation["action_fingerprint"] = fingerprint_text(action)
        if region is not None and _safe_exact_identifier(region):
            operation["region"] = region
        elif region is not None:
            operation["region_fingerprint"] = fingerprint_text(region)
        return operation

    def _audit(
        self,
        input: dict,
        *,
        scope: str,
        rule_source: str | None = None,
        rule: str | None = None,
        reason: PermissionDecisionReason | None = None,
        is_read_only: bool | None = None,
    ) -> PermissionAuditMetadata:
        return PermissionAuditMetadata(
            scope=scope,
            source="permission_pipeline",
            rule_source=rule_source,
            rule=rule,
            reason_type=reason.type if reason else None,
            reason_detail=reason.detail if reason else None,
            is_read_only=is_read_only,
            operation=self._operation_metadata(input),
        )

    def _supports_persistent_allow(self, input: dict, *, is_read_only: bool) -> bool:
        return True

    def _suggestion(self, input: dict, *, is_read_only: bool = False) -> list[PermissionRuleValue] | None:
        if not self._supports_persistent_allow(input, is_read_only=is_read_only):
            return None
        product = _string_value(input.get("product"))
        action = _string_value(input.get("action"))
        if product is None or action is None:
            return None
        product = _canonical_product(product)
        if not (_safe_exact_identifier(product) and _safe_exact_identifier(action)):
            return None
        return [PermissionRuleValue(tool_name=self.name, rule_content="{}:{}".format(product, action))]

    def _matching_rule(
        self,
        input: dict,
        rules_by_source: dict[str, list[str]],
        *,
        require_exact: bool = False,
    ) -> tuple[str, str] | None:
        operation = _safe_operation_identifiers(input)
        if operation is None:
            return None
        canonical_product, action = operation
        best: tuple[tuple[tuple[int, int], tuple[int, int], int, int], str, str] | None = None

        for source, rules in rules_by_source.items():
            for index, rule in enumerate(rules):
                parsed = _parse_aliyun_rule(rule)
                if parsed is None:
                    continue
                product_pattern, action_pattern = parsed
                if not fnmatch.fnmatchcase(canonical_product.lower(), product_pattern.lower()):
                    continue
                if not fnmatch.fnmatchcase(action.lower(), action_pattern.lower()):
                    continue
                if require_exact and (
                    product_pattern.lower() != canonical_product.lower() or action_pattern.lower() != action.lower()
                ):
                    continue
                score = (
                    _side_specificity(product_pattern, canonical_product),
                    _side_specificity(action_pattern, action),
                    _RULE_SOURCE_ORDER.get(source, 0),
                    index,
                )
                rule_content = "{}:{}".format(product_pattern, action_pattern)
                if best is None or score > best[0]:
                    best = (score, source, rule_content)

        if best is None:
            return None
        return best[1], best[2]

    async def check_permissions(self, input: dict, context=None) -> PermissionResult:
        if not isinstance(context, ToolPermissionContext):
            context = ToolPermissionContext(cwd=context.get("cwd", "") if isinstance(context, dict) else "")

        is_read_only = _safe_operation_identifiers(input) is not None and self.is_read_only(input)
        supports_persistent_allow = self._supports_persistent_allow(input, is_read_only=is_read_only)
        for behavior, rules_by_source in (
            ("deny", context.deny_rules),
            ("ask", context.ask_rules),
            ("allow", context.allow_rules),
        ):
            if behavior == "allow" and not supports_persistent_allow:
                continue
            match = self._matching_rule(input, rules_by_source, require_exact=behavior == "allow" and not is_read_only)
            if match is None:
                continue
            rule_source, rule = match
            detail = _("matched {behavior} rule: {rule}").format(behavior=behavior, rule=rule)
            reason = PermissionDecisionReason(type="rule", detail=detail)
            return PermissionResult(
                behavior=behavior,
                message=detail,
                reason=reason,
                audit=self._audit(
                    input,
                    scope=scope_for_rule_source(rule_source),
                    rule_source=rule_source,
                    rule=rule,
                    reason=reason,
                    is_read_only=is_read_only,
                ),
            )

        if is_read_only:
            reason = PermissionDecisionReason(type="read_only", detail="read-only Aliyun API action")
            return PermissionResult(
                behavior="allow",
                reason=reason,
                audit=self._audit(input, scope="read_only", reason=reason, is_read_only=True),
            )

        reason = PermissionDecisionReason(
            type="untrusted_write",
            detail="Aliyun API action may modify cloud resources",
        )
        return PermissionResult(
            behavior="ask",
            message=_("Allow {}?").format(self.user_facing_name(input)),
            reason=reason,
            suggestions=self._suggestion(input, is_read_only=is_read_only),
            audit=self._audit(input, scope="once", reason=reason, is_read_only=False),
        )

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

        if mode in {"StsToken", "OAuth"}:
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
            if error := reject_template_body_param(params, pipeline_mode=context.pipeline_mode):
                return ToolResult.error(error)
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

        if credential.mode == "OAuth":
            try:
                credential = AliyunCredentials.refresh_oauth_if_needed(credential)
            except AliyunOAuthError as exc:
                return ToolResult.error(str(exc))

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
        telemetry_identifiers = _aliyun_api_telemetry_identifiers(product, action, region)
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
                    **telemetry_identifiers,
                    **_aliyun_api_version_telemetry(version),
                    "outcome": outcome,
                    "duration_ms": duration_ms,
                    "http_status": http_status,
                },
            )
            add_metric(Metrics.ALIYUN_API_CALLED_COUNT, 1, _aliyun_api_metric_attrs(product, outcome))
            add_metric(Metrics.ALIYUN_API_CALLED_DURATION, duration_ms)

            # Special case: ROS ValidateTemplate
            if api_service == "ROS" and action == "ValidateTemplate":
                _emit_validate_template_event(body, duration_ms)

            if context.event_queue is not None and product == "ros" and action == "CreateStack":
                stack_id = _string_value(body.get("StackId")) if isinstance(body, dict) else None
                if stack_id:
                    await context.event_queue.put(
                        ResourceObservedEvent(
                            provider="ros",
                            resource_type="stack",
                            resource_id=stack_id,
                            resource_name=str(params.get("StackName") or params.get("stack_name") or ""),
                            region_id=region,
                            action=action,
                            tool_name=self.name,
                            tool_use_id=context.tool_use_id,
                        )
                    )

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
                    **telemetry_identifiers,
                    **_aliyun_api_version_telemetry(version),
                    "outcome": outcome,
                    "duration_ms": duration_ms,
                    "http_status": http_status,
                    "error_code": _scrub_unsafe_identifier_text(error_code, product, action, region, version),
                    "error_message": _scrub_unsafe_identifier_text(error_message, product, action, region, version),
                },
            )
            add_metric(Metrics.ALIYUN_API_CALLED_COUNT, 1, _aliyun_api_metric_attrs(product, outcome))
            add_metric(Metrics.ALIYUN_API_CALLED_DURATION, duration_ms)

            return ToolResult.error(self._clean_error_message(error_str))
