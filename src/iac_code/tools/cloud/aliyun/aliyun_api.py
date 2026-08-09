"""Generic Alibaba Cloud API tool using OpenAPI SDK."""

from __future__ import annotations

import asyncio
import copy
import fnmatch
import inspect
import ipaddress
import json
import logging
import os
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal

import yaml
from alibabacloud_tea_openapi import models as open_api_models
from alibabacloud_tea_openapi.client import Client as OpenApiClient
from darabonba.runtime import RuntimeOptions

from iac_code.i18n import _
from iac_code.services.cloud_credentials import CloudCredentials
from iac_code.services.permissions.audit import fingerprint_text
from iac_code.services.permissions.rule_scope import scope_for_rule_source
from iac_code.services.providers.aliyun import DEFAULT_REGION, AliyunCredential, AliyunCredentials
from iac_code.services.providers.aliyun_oauth import AliyunOAuthError
from iac_code.services.telemetry import add_metric, get_session_id, get_user_id, log_event, start_span
from iac_code.services.telemetry.names import (
    ALIYUN_API_TARGET_OUTCOMES,
    AliyunApiAttr,
    Events,
    GenAiAttr,
    GenAiOperationName,
    GenAiSpanKind,
    Metrics,
    Spans,
)
from iac_code.tools.base import ToolContext, ToolResult
from iac_code.tools.cloud.aliyun.api_contract import (
    ApiCallShape,
    ApiContractError,
    CanonicalWireContract,
    _read_body_file,
    _validate_pathname,
    validate_content_type,
)
from iac_code.tools.cloud.aliyun.api_identifiers import SAFE_API_VERSION
from iac_code.tools.cloud.aliyun.contract_store import (
    AuthorizedReadPath,
    ResolvedContractError,
    ResolvedContractRecovery,
    ResolvedContractStore,
    canonical_input_sha256,
)
from iac_code.tools.cloud.aliyun.ecs_credential_errors import ecs_credential_error_code
from iac_code.tools.cloud.aliyun.public_errors import normalize_api_identity, public_aliyun_error
from iac_code.tools.cloud.aliyun.result_contract import (
    ALIYUN_HTTP_METADATA_KEY,
    build_aliyun_http_metadata,
    serialize_business_result,
)
from iac_code.tools.cloud.aliyun.retry_policy import RetryBudget, TransportFailure
from iac_code.tools.cloud.aliyun.runtime import (
    _valid_delegated_binding,
    emit_aliyun_api_called,
    emit_aliyun_api_contract_error,
    emit_aliyun_endpoint_resolution,
)
from iac_code.tools.cloud.aliyun.template_source import (
    check_local_template_url_read_permission,
    is_local_template_url,
    read_local_template_url,
    reject_pipeline_dedicated_ros_deployment_action,
    reject_pipeline_dedicated_ros_template_action,
    reject_pipeline_template_source_params,
)
from iac_code.tools.cloud.aliyun.user_agent import build_user_agent
from iac_code.tools.cloud.base_api import BaseCloudApi
from iac_code.tools.path_safety import check_read_path_with_resolution
from iac_code.types.permissions import (
    MAX_PERMISSION_AUDIT_ITEMS,
    ExecutionClass,
    InvocationBinding,
    PermissionAuditMetadata,
    PermissionDecisionReason,
    PermissionResult,
    PermissionRuleValue,
    ToolPermissionContext,
)
from iac_code.types.stream_events import ResourceObservedEvent

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _RuntimeFileAuthorization:
    source: Literal["template", "body_file"]
    resolved_path: str
    permission: PermissionResult | None


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

# The pre-runtime adapter remains available only for isolated regression tests.
_LEGACY_ENDPOINTS_FILE = Path(__file__).parent / "data" / "endpoints" / "legacy.yml"


def _load_legacy_endpoints() -> dict[str, Any]:
    data = yaml.safe_load(_LEGACY_ENDPOINTS_FILE.read_text(encoding="utf-8")) or {}
    # Convert region lists to sets for O(1) lookup
    for config in data.values():
        for key in ("regional", "central"):
            section = config.get(key)
            if section and "regions" in section:
                section["regions"] = set(section["regions"])
    return data


_VERSION_MAP_LOWER: dict[str, str] = {k.lower(): v for k, v in VERSION_MAP.items()}
_PRODUCT_CANONICAL: dict[str, str] = {k.lower(): k for k in VERSION_MAP}
_SAFE_RUNTIME_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_SAFE_RUNTIME_PRODUCT_INPUT = re.compile(r"^[ \t\n\r\f\v]*[A-Za-z0-9][A-Za-z0-9_-]{0,127}[ \t\n\r\f\v]*$")
_SAFE_RUNTIME_REGION = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
_SAFE_RUNTIME_HOST_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_RUNTIME_INPUT_FIELDS = frozenset(
    {
        "product",
        "action",
        "version",
        "params",
        "region_id",
        "endpoint",
        "style",
        "method",
        "pathname",
        "body",
        "body_file",
        "content_type",
        "max_response_bytes",
    }
)
_DEFAULT_RUNTIME_RESPONSE_BYTES = 8 * 1024 * 1024
_MAX_RUNTIME_RESPONSE_BYTES = 16 * 1024 * 1024
LOCAL_TEMPLATE_PATH_FIELD = "_iac_code_local_template_path"


class _LocalTemplateBodySentinel:
    def __deepcopy__(self, memo: dict[int, Any]) -> _LocalTemplateBodySentinel:
        return self


LOCAL_TEMPLATE_BODY_SENTINEL = _LocalTemplateBodySentinel()

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
_SAFE_REQUEST_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/=-]{0,255}$")
_SAFE_WILDCARD_SEGMENT = re.compile(r"^[A-Za-z0-9_*-]{1,128}$")
_REQUEST_ID_BODY_KEYS = ("requestid",)
_REQUEST_ID_HEADER_KEYS = (
    "requestid",
    "xacsrequestid",
    "xaliyunrequestid",
    "xlogrequestid",
    "xossrequestid",
    "xmnsrequestid",
    "xrequestid",
)
_ERROR_CODE_BODY_KEYS = ("errorcode", "code", "error")
_ERROR_CODE_HEADER_KEYS = (
    "errorcode",
    "xacserrorcode",
    "xaliyunerrorcode",
    "xlogerrorcode",
    "xosserrorcode",
    "xmnserrorcode",
    "xerrorcode",
)
_HTTP_STATUS_KEYS = ("httpstatuscode", "statuscode", "httpstatus", "httpcode", "status")
_RESPONSE_HEADERS_KEYS = ("responseheaders", "iacresponseheaders", "headers")
_RULE_SOURCE_ORDER = {
    "session": 5,
    "cli_arg": 4,
    "local_settings": 3,
    "project_settings": 2,
    "user_settings": 1,
}


def _canonical_product(product: str) -> str:
    return _PRODUCT_CANONICAL.get(product.lower(), product)


def _canonical_public_product(product: str) -> str:
    canonical = _PRODUCT_CANONICAL.get(product.lower())
    if canonical is None:
        return product
    return "IaCService" if canonical.casefold() == "iacservice" else canonical.title()


def _safe_exact_identifier(value: str) -> bool:
    return bool(_SAFE_RULE_ID.fullmatch(value))


def _aliyun_api_metric_attrs(
    product: str,
    outcome: str,
    target_outcome: str | None = None,
) -> dict[str, str]:
    if not product or not _safe_exact_identifier(product):
        api_service = "unsafe"
    else:
        canonical = _PRODUCT_CANONICAL.get(product.casefold())
        api_service = canonical.upper() if canonical is not None else "other"
    detailed = target_outcome or ("success" if outcome == "success" else "target_transport_failure")
    if detailed not in ALIYUN_API_TARGET_OUTCOMES:
        detailed = "target_transport_failure"
    return {"api_service": api_service, "outcome": outcome, "target_outcome": detailed}


def _aliyun_api_span_attrs(
    contract: CanonicalWireContract,
    shape: ApiCallShape,
    context: ToolContext,
) -> dict[str, Any]:
    attrs: dict[str, Any] = {
        GenAiAttr.SPAN_KIND: GenAiSpanKind.TOOL,
        GenAiAttr.OPERATION_NAME: GenAiOperationName.EXECUTE_TOOL,
        GenAiAttr.TOOL_NAME: "aliyun_api",
        AliyunApiAttr.SERVICE: _aliyun_api_metric_attrs(contract.product, "success")["api_service"],
        AliyunApiAttr.PRODUCT: contract.product,
        AliyunApiAttr.ACTION: contract.action,
        AliyunApiAttr.VERSION: contract.version,
        AliyunApiAttr.REGION: shape.region_id,
        AliyunApiAttr.HTTP_METHOD: contract.method,
    }
    if context.tool_use_id:
        attrs[GenAiAttr.TOOL_CALL_ID] = context.tool_use_id
    for key, getter in (
        (GenAiAttr.SESSION_ID, get_session_id),
        (GenAiAttr.USER_ID, get_user_id),
    ):
        try:
            attrs[key] = getter()
        except Exception:
            logger.debug("Failed to resolve %s for Aliyun API telemetry", key, exc_info=True)
    return attrs


def _safe_request_id(value: Any) -> str | None:
    return value if isinstance(value, str) and _SAFE_REQUEST_ID.fullmatch(value) else None


def _safe_error_code(value: Any) -> str | None:
    if type(value) is int:
        value = str(value)
    return value if isinstance(value, str) and _TARGET_ERROR_CODE.fullmatch(value) else None


def _normalized_telemetry_key(value: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value).casefold())


def _mapping_value(mapping: Mapping[str, Any], normalized_keys: tuple[str, ...]) -> Any | None:
    normalized_items = tuple((_normalized_telemetry_key(key), value) for key, value in mapping.items())
    for normalized_key in normalized_keys:
        for key, value in normalized_items:
            if value is not None and key == normalized_key:
                return value
    return None


def _object_value(value: Any, normalized_keys: tuple[str, ...], attribute_names: tuple[str, ...]) -> Any | None:
    fields = vars(value) if hasattr(value, "__dict__") else {}
    candidate = _mapping_value(fields, normalized_keys)
    if candidate is not None:
        return candidate
    for attribute_name in attribute_names:
        try:
            candidate = getattr(value, attribute_name)
        except Exception:
            continue
        if candidate is not None:
            return candidate
    return None


def _safe_http_status(value: Any) -> int | None:
    if isinstance(value, str) and value.isdecimal():
        value = int(value)
    return value if type(value) is int and 100 <= value <= 599 else None


def _response_request_id(response: Any) -> str | None:
    if isinstance(response.body, Mapping):
        request_id = _safe_request_id(_mapping_value(response.body, _REQUEST_ID_BODY_KEYS))
        if request_id is not None:
            return request_id
    if isinstance(response.headers, Mapping):
        return _safe_request_id(_mapping_value(response.headers, _REQUEST_ID_HEADER_KEYS))
    return None


def _response_error_code(response: Any) -> str | None:
    if not isinstance(response.body, Mapping):
        body_code = None
    else:
        body_code = _safe_error_code(_mapping_value(response.body, _ERROR_CODE_BODY_KEYS))
    if body_code is not None:
        return body_code
    if isinstance(response.headers, Mapping):
        return _safe_error_code(_mapping_value(response.headers, _ERROR_CODE_HEADER_KEYS))
    return None


def _exception_telemetry_attrs(error: BaseException) -> dict[str, Any]:
    attrs: dict[str, Any] = {}
    status = _safe_http_status(
        _object_value(
            error,
            _HTTP_STATUS_KEYS,
            ("status_code", "statusCode", "http_status_code", "httpStatusCode", "http_code", "status"),
        )
    )
    if status is not None:
        attrs[AliyunApiAttr.HTTP_STATUS_CODE] = status
    request_id = _safe_request_id(_object_value(error, _REQUEST_ID_BODY_KEYS, ("request_id", "requestId", "RequestId")))
    error_code = _safe_error_code(
        _object_value(error, _ERROR_CODE_BODY_KEYS, ("error_code", "errorCode", "ErrorCode", "code"))
    )
    data = getattr(error, "data", None)
    if isinstance(data, Mapping):
        request_id = request_id or _safe_request_id(_mapping_value(data, _REQUEST_ID_BODY_KEYS))
        error_code = error_code or _safe_error_code(_mapping_value(data, _ERROR_CODE_BODY_KEYS))
    headers = _object_value(error, _RESPONSE_HEADERS_KEYS, ("response_headers", "headers", "_iac_response_headers"))
    if isinstance(headers, Mapping):
        request_id = request_id or _safe_request_id(_mapping_value(headers, _REQUEST_ID_HEADER_KEYS))
        error_code = error_code or _safe_error_code(_mapping_value(headers, _ERROR_CODE_HEADER_KEYS))
    if request_id is not None:
        attrs[AliyunApiAttr.REQUEST_ID] = request_id
    if error_code is not None:
        attrs[AliyunApiAttr.ERROR_CODE] = error_code
    return attrs


def _record_aliyun_api_span_result(
    span: Any,
    *,
    target_outcome: str,
    response: Any | None,
    error: BaseException | None,
) -> None:
    attrs: dict[str, Any] = {
        AliyunApiAttr.OUTCOME: "success" if target_outcome == "success" else "failure",
        AliyunApiAttr.TARGET_OUTCOME: target_outcome,
    }
    if response is not None:
        attrs[AliyunApiAttr.HTTP_STATUS_CODE] = int(response.status)
        request_id = _response_request_id(response)
        if request_id is not None:
            attrs[AliyunApiAttr.REQUEST_ID] = request_id
        if not 200 <= response.status < 300:
            error_code = _response_error_code(response)
            if error_code is not None:
                attrs[AliyunApiAttr.ERROR_CODE] = error_code
    elif error is not None:
        attrs.update(_exception_telemetry_attrs(error))
    try:
        for key, value in attrs.items():
            span.set_attribute(key, value)
    except Exception:
        logger.warning("Failed to attach Aliyun API span result attributes", exc_info=True)


_TARGET_ERROR_CODE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_TARGET_ERROR_DETAIL_LIMIT = 512
_TARGET_ERROR_DETAIL_FIELDS = (
    ("Message", ("Message", "message")),
    ("Description", ("Description", "description", "error_description")),
)
_TARGET_ERROR_DETAIL_SENSITIVE_RE = re.compile(
    r"(?<![A-Za-z0-9_])[\"`]?"
    r"(?:AccessKeyId|AccessKeySecret|SecurityToken|access_key_id|access_key_secret|security_token|"
    r"x-acs-security-token|access_token|oauth_access_token|refresh_token|sts_token|Authorization|Cookie|"
    r"Set-Cookie|token|secret|password|signature)"
    r"[\"`]?\s*[:=]\s*[\"`]?(?:Bearer\s+)?[^;；，,。\s\"`]+",
    re.IGNORECASE,
)
_TARGET_ERROR_DETAIL_RESOURCE_RE = re.compile(
    r"(?<![A-Za-z0-9_])[\"`]?"
    r"(?:InstanceId|instanceId|WorkspaceId|workspaceId|AppKey|appKey|AgentKey|agentKey|TaskId|taskId|"
    r"JobId|jobId|OrderId|orderId|ProjectId|projectId|ClusterId|clusterId|ResourceArn|resourceArn)"
    r"[\"`]?\s*[:=]\s*[\"`]?(?!<运行前|<脱敏)[A-Za-z0-9][A-Za-z0-9._:/@=-]{5,}",
    re.IGNORECASE,
)
_TARGET_ERROR_DETAIL_REQUEST_ID_RE = re.compile(
    r"(?<![A-Za-z0-9_])[\"`]?"
    r"(?:RequestId|requestId|request[ _-]id|x-acs-request-id)"
    r"[\"`]?\s*[:=]\s*[\"`]?[A-Za-z0-9-]{16,}",
    re.IGNORECASE,
)
_TARGET_ERROR_DETAIL_BEARER_RE = re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{20,}\b", re.IGNORECASE)


def _target_http_error_message(response: Any) -> str:
    code = None
    if isinstance(response.body, Mapping):
        candidate = response.body.get("Code", response.body.get("code", response.body.get("error")))
        if isinstance(candidate, str) and _TARGET_ERROR_CODE.fullmatch(candidate):
            code = candidate
    suffix = f":{code}" if code is not None else ""
    return f"aliyun_target_http_error:{int(response.status)}{suffix}"


def _redact_target_error_detail(value: str) -> str:
    redacted = _TARGET_ERROR_DETAIL_REQUEST_ID_RE.sub("<redacted>", value)
    redacted = _TARGET_ERROR_DETAIL_SENSITIVE_RE.sub("<redacted>", redacted)
    redacted = _TARGET_ERROR_DETAIL_BEARER_RE.sub("Bearer <redacted>", redacted)
    redacted = _TARGET_ERROR_DETAIL_RESOURCE_RE.sub("<redacted>", redacted)
    redacted = re.sub(r"\s+", " ", redacted).strip()
    return redacted[:_TARGET_ERROR_DETAIL_LIMIT].rstrip()


def _target_http_error_detail(response: Any) -> str | None:
    if not isinstance(response.body, Mapping):
        return None
    details: list[str] = []
    for public_name, keys in _TARGET_ERROR_DETAIL_FIELDS:
        for key in keys:
            value = response.body.get(key)
            if isinstance(value, str) and value.strip():
                redacted = _redact_target_error_detail(value)
                if redacted:
                    details.append(f"{public_name}: {redacted}")
                break
    return "; ".join(details) or None


def _target_failure_outcome(error: Exception) -> str:
    if isinstance(error, TransportFailure) and error.outcome in ALIYUN_API_TARGET_OUTCOMES:
        return error.outcome
    if str(error) in ALIYUN_API_TARGET_OUTCOMES:
        return str(error)
    return "target_transport_failure"


def _target_failure_message(error: Exception) -> str:
    if isinstance(error, TransportFailure) and error.outcome in ALIYUN_API_TARGET_OUTCOMES:
        return error.outcome
    code = str(error)
    if code in ALIYUN_API_TARGET_OUTCOMES | {"response_too_large", "error_response_too_large"}:
        return code
    if (credential_code := ecs_credential_error_code(error)) is not None:
        # Credential resolution fails before the request is signed; keep the exact
        # stable code so public_aliyun_error() can render its actionable message
        # instead of the generic "may have been sent" transport text.
        return credential_code
    return "aliyun_target_transport_error"


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


def _normalize_runtime_input(
    input: Mapping[str, Any],
    *,
    allow_internal_shape: bool = False,
    allow_arbitrary_json_body: bool = False,
) -> dict[str, Any]:
    allowed_fields = _RUNTIME_INPUT_FIELDS | ({LOCAL_TEMPLATE_PATH_FIELD} if allow_internal_shape else set())
    if any(not isinstance(name, str) or name not in allowed_fields for name in input):
        raise ApiContractError("invalid_tool_input")
    try:
        normalized = copy.deepcopy(dict(input))
    except (TypeError, ValueError) as error:
        raise ApiContractError("invalid_tool_input") from error
    identity = normalize_api_identity(normalized)
    product = identity.product
    normalized["product"] = identity.product
    region_id = normalized.get("region_id")
    if region_id is None:
        normalized["region_id"] = ""
    elif not isinstance(region_id, str) or _SAFE_RUNTIME_REGION.fullmatch(region_id) is None:
        raise ApiContractError("invalid_region_id")
    endpoint = normalized.get("endpoint")
    if endpoint is not None:
        _validate_runtime_endpoint(endpoint)
    params = normalized.get("params", {})
    if params is None:
        params = {}
    if not isinstance(params, Mapping):
        raise ApiContractError(
            "invalid_parameter_type:params",
            parameter="params",
            expected_type="object",
            actual_type=_runtime_json_type_name(params),
        )
    normalized["params"] = copy.deepcopy(dict(params))
    if not _runtime_json_shape_is_valid(normalized["params"], allow_sentinel=allow_internal_shape):
        raise ApiContractError("invalid_params")
    canonical_product = _PRODUCT_CANONICAL.get(product.lower(), product)
    if canonical_product == "ros":
        from iac_code.tools.cloud.aliyun.hooks.ros_parameters import normalize_ros_parameters

        normalize_ros_parameters(str(normalized.get("action") or ""), normalized["params"])
    template_url = normalized["params"].get("TemplateURL")
    if canonical_product == "ros" and isinstance(template_url, str) and is_local_template_url(template_url):
        if "TemplateBody" in normalized["params"]:
            raise ApiContractError("conflicting_template_sources")
        normalized[LOCAL_TEMPLATE_PATH_FIELD] = template_url
        normalized["params"].pop("TemplateURL")
        normalized["params"]["TemplateBody"] = LOCAL_TEMPLATE_BODY_SENTINEL
    if "body" in normalized and "body_file" in normalized:
        raise ApiContractError("conflicting_body_sources")
    if "body" in normalized:
        if not _runtime_json_shape_is_valid(normalized["body"]):
            raise ApiContractError("invalid_body")
        if not allow_arbitrary_json_body and not isinstance(normalized["body"], Mapping):
            raise ApiContractError("invalid_body")
    if "body_file" in normalized and (not isinstance(normalized["body_file"], str) or not normalized["body_file"]):
        raise ApiContractError("invalid_body_file")
    style = normalized.get("style")
    if style is not None:
        if not isinstance(style, str) or style.upper() not in {"RPC", "ROA"}:
            raise ApiContractError("invalid_style")
        normalized["style"] = style.upper()
    method = normalized.get("method")
    if method is not None:
        if not isinstance(method, str) or method.upper() not in {
            "GET",
            "POST",
            "PUT",
            "DELETE",
            "PATCH",
            "HEAD",
            "OPTIONS",
        }:
            raise ApiContractError("invalid_method")
        normalized["method"] = method.upper()
    if "pathname" in normalized:
        _validate_pathname(normalized["pathname"])
    content_type = normalized.get("content_type")
    if content_type is not None:
        normalized["content_type"] = validate_content_type(content_type)
    max_response_bytes = normalized.get("max_response_bytes")
    if max_response_bytes is not None and (
        not isinstance(max_response_bytes, int)
        or isinstance(max_response_bytes, bool)
        or not 0 < max_response_bytes <= _MAX_RUNTIME_RESPONSE_BYTES
    ):
        raise ApiContractError("invalid_max_response_bytes")
    return normalized


def _validate_runtime_endpoint(endpoint: Any) -> None:
    if (
        not isinstance(endpoint, str)
        or endpoint != endpoint.casefold()
        or len(endpoint) > 253
        or endpoint.endswith(".")
    ):
        raise ApiContractError("invalid_endpoint")
    labels = endpoint.split(".")
    if len(labels) < 2 or any(_SAFE_RUNTIME_HOST_LABEL.fullmatch(label) is None for label in labels):
        raise ApiContractError("invalid_endpoint")
    try:
        ipaddress.ip_address(endpoint)
    except ValueError:
        return
    raise ApiContractError("invalid_endpoint")


def _with_canonical_runtime_product(input: Mapping[str, Any], product: str) -> dict[str, Any]:
    normalized = dict(input)
    normalized["product"] = product
    params = normalized.get("params")
    if product.casefold() != "ros" or not isinstance(params, Mapping):
        return normalized
    canonical_params = dict(params)
    from iac_code.tools.cloud.aliyun.hooks.ros_parameters import normalize_ros_parameters

    normalize_ros_parameters(str(normalized.get("action") or ""), canonical_params)
    normalized["params"] = canonical_params
    template_url = canonical_params.get("TemplateURL")
    if not isinstance(template_url, str) or not is_local_template_url(template_url):
        return normalized
    if "TemplateBody" in canonical_params:
        raise ApiContractError("conflicting_template_sources")
    canonical_params.pop("TemplateURL")
    canonical_params["TemplateBody"] = LOCAL_TEMPLATE_BODY_SENTINEL
    normalized["params"] = canonical_params
    normalized[LOCAL_TEMPLATE_PATH_FIELD] = template_url
    return normalized


def _runtime_json_shape_is_valid(value: Any, *, allow_sentinel: bool = False) -> bool:
    if value is LOCAL_TEMPLATE_BODY_SENTINEL:
        return allow_sentinel
    if value is None or isinstance(value, str | bool | int):
        return True
    if isinstance(value, float):
        return value == value and value not in {float("inf"), float("-inf")}
    if isinstance(value, Mapping):
        return all(
            isinstance(name, str) and _runtime_json_shape_is_valid(item, allow_sentinel=allow_sentinel)
            for name, item in value.items()
        )
    if isinstance(value, list | tuple):
        return all(_runtime_json_shape_is_valid(item, allow_sentinel=allow_sentinel) for item in value)
    return False


def _runtime_json_type_name(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list | tuple):
        return "array"
    if isinstance(value, Mapping):
        return "object"
    return "unknown"


def _path_is_lexically_under(path: str, root: str) -> bool:
    path_norm = os.path.normcase(os.path.abspath(path))
    root_norm = os.path.normcase(os.path.abspath(root))
    try:
        return os.path.commonpath((path_norm, root_norm)) == root_norm
    except ValueError:
        return False


def _materialization_root(root: str) -> tuple[str, str]:
    logical = os.path.abspath(os.path.expanduser(root or "."))
    return logical, os.path.realpath(logical)


def _absolute_materialization_path(path: str, roots: list[str]) -> Path:
    absolute = os.path.abspath(path)
    for root in roots:
        logical_root, real_root = _materialization_root(root)
        if not _path_is_lexically_under(absolute, logical_root):
            continue
        relative = os.path.relpath(absolute, logical_root)
        mapped = real_root if relative == "." else os.path.join(real_root, relative)
        return Path(mapped)
    return Path(absolute)


def _relative_materialization_candidates(path: str, roots: list[str]) -> list[Path]:
    candidates: list[Path] = []
    for root in roots:
        _logical_root, real_root = _materialization_root(root)
        candidates.append(Path(os.path.abspath(os.path.join(real_root, path))))
    return candidates


def _runtime_materialization_path(path: str, context: ToolContext) -> Path:
    expanded = os.path.expanduser(path)
    roots = [context.cwd or ".", *context.relative_read_directories]
    if os.path.isabs(expanded):
        return _absolute_materialization_path(expanded, roots)
    candidates = _relative_materialization_candidates(expanded, roots)
    return next((candidate for candidate in candidates if os.path.lexists(candidate)), candidates[0])


def _authorized_materialization_path(
    source: Literal["template", "body_file"],
    authorized_read_paths: tuple[AuthorizedReadPath, ...],
) -> Path:
    """Return the physical path bound to the one-shot authorization snapshot."""
    matches = [entry.path for entry in authorized_read_paths if entry.source == source]
    if len(matches) != 1 or not os.path.isabs(matches[0]):
        raise ApiContractError("snapshot_read_path_mismatch")
    return Path(matches[0])


def _runtime_contract_error_stage(error: ApiContractError) -> str | None:
    code = str(error).casefold()
    if "product" in code:
        return "product"
    if "version" in code:
        return "version"
    if "security" in code or "auth_type" in code:
        return "security"
    if "media" in code or "content_type" in code:
        return "media_type"
    if "signature" in code:
        return "signature"
    if "transport" in code:
        return "transport"
    if "oss" in code or "catalog" in code:
        return "oss_catalog"
    if "endpoint" in code:
        return "endpoint"
    if "host" in code:
        return "host"
    if "action" in code or "metadata" in code or "api" in code:
        return "api"
    if any(token in code for token in ("parameter", "params", "pathname", "region", "body", "template", "input")):
        return "parameter"
    return None


def _runtime_call_shape(
    tool_input: Mapping[str, Any],
    *,
    contract: CanonicalWireContract | None = None,
) -> ApiCallShape:
    params = tool_input.get("params", {})
    if not isinstance(params, Mapping):
        raise ApiContractError("invalid_params")
    locations: dict[str, list[str]] = {}
    metadata = {parameter.name: parameter for parameter in contract.parameters} if contract is not None else {}
    for name in params:
        parameter = metadata.get(str(name))
        location = parameter.location if parameter is not None else "unknown"
        locations.setdefault(location, []).append(str(name))
    if contract is not None and tool_input.get("region_id"):
        for parameter in contract.parameters:
            if (
                parameter.location == "query"
                and parameter.name.casefold() == "regionid"
                and parameter.name not in params
            ):
                locations.setdefault("query", []).append(parameter.name)
    body_sources: list[Literal["body", "body_file", "params_body", "formdata"]] = []
    if "body" in tool_input:
        body_sources.append("body")
    if "body_file" in tool_input:
        body_sources.append("body_file")
    if contract is not None:
        occupied = {str(name) for name in params}
        if any(parameter.name in occupied and parameter.location == "body" for parameter in contract.parameters):
            body_sources.append("params_body")
        if any(parameter.name in occupied and parameter.location == "formData" for parameter in contract.parameters):
            body_sources.append("formdata")
    if len(body_sources) > 1:
        raise ApiContractError("conflicting_body_sources")
    body_source = body_sources[0] if body_sources else "none"
    explicit_overrides = tuple(field_name for field_name in ("style", "method", "pathname") if field_name in tool_input)
    max_response_bytes = tool_input.get("max_response_bytes", _DEFAULT_RUNTIME_RESPONSE_BYTES)
    if not isinstance(max_response_bytes, int) or isinstance(max_response_bytes, bool):
        raise ApiContractError("invalid_max_response_bytes")
    return ApiCallShape(
        product=contract.product if contract is not None else str(tool_input["product"]),
        version=str(tool_input["version"]) if tool_input.get("version") is not None else None,
        action=str(tool_input["action"]),
        region_id=str(tool_input["region_id"]),
        explicit_overrides=explicit_overrides,
        parameter_names_by_location=MappingProxyType(
            {key: tuple(sorted(values)) for key, values in sorted(locations.items())}
        ),
        body_source=body_source,
        endpoint=str(tool_input["endpoint"]) if tool_input.get("endpoint") is not None else None,
        style=str(tool_input["style"]) if tool_input.get("style") is not None else None,
        method=str(tool_input["method"]) if tool_input.get("method") is not None else None,
        pathname=str(tool_input["pathname"]) if tool_input.get("pathname") is not None else None,
        content_type=str(tool_input["content_type"]) if tool_input.get("content_type") is not None else None,
        max_response_bytes=max_response_bytes,
    )


def _runtime_pipeline_guard(tool_input: Mapping[str, Any], *, pipeline_mode: bool) -> str | None:
    product = str(tool_input.get("product", ""))
    product = _PRODUCT_CANONICAL.get(product.lower(), product)
    if product != "ros":
        return None
    action = str(tool_input.get("action", ""))
    params = tool_input.get("params", {})
    if not isinstance(params, dict):
        return "invalid_params"
    return (
        reject_pipeline_dedicated_ros_template_action(action, pipeline_mode=pipeline_mode)
        or reject_pipeline_dedicated_ros_deployment_action(action, pipeline_mode=pipeline_mode)
        or reject_pipeline_template_source_params(action, params, pipeline_mode=pipeline_mode)
    )


def _runtime_is_read_only(
    contract: CanonicalWireContract,
    shape: ApiCallShape,
    metadata_contract: CanonicalWireContract,
) -> bool:
    overrides_match = all(
        _normalized_override_value(name, getattr(shape, name))
        == _normalized_override_value(name, getattr(metadata_contract, name))
        for name in shape.explicit_overrides
    )
    body_matches = _runtime_body_matches_contract(shape.body_source, contract.request_body_type)
    if not overrides_match or not body_matches:
        return False
    if contract.product.casefold() == "ros" and contract.action.casefold() == "previewstack":
        if contract.metadata_source == "explicit_fallback":
            return (
                contract.style == "RPC"
                and contract.method == "POST"
                and contract.pathname == "/"
                and not shape.explicit_overrides
                and shape.body_source == "none"
            )
        return contract.metadata_source in {"fresh", "cache", "stale_cache"}
    if contract.metadata_source == "explicit_fallback":
        if contract.operation_type is not None:
            return False
        if contract.style == "RPC":
            return contract.action.startswith(("Get", "List", "Describe", "Query", "Validate"))
        return contract.style == "ROA" and contract.method in {"GET", "HEAD"} and shape.body_source == "none"
    if contract.metadata_source not in {"fresh", "cache", "stale_cache"} or contract.operation_type != "read":
        return False
    if contract.style == "RPC":
        return True
    return contract.style == "ROA" and contract.method in {"GET", "HEAD", "OPTIONS"} and shape.body_source == "none"


def _normalized_override_value(name: str, value: Any) -> Any:
    if isinstance(value, str) and name in {"style", "method"}:
        return value.upper()
    return value


def _runtime_body_matches_contract(body_source: str, request_body_type: str) -> bool:
    if body_source == "none":
        return True
    return {
        "body": "json",
        "body_file": "byte",
        "params_body": "json",
        "formdata": "formData",
    }.get(body_source) == request_body_type


def _dedupe_permission_reasons(reasons: list[PermissionDecisionReason]) -> list[PermissionDecisionReason]:
    result: list[PermissionDecisionReason] = []
    seen: set[tuple[str, str]] = set()
    for reason in reasons:
        key = (reason.type, reason.detail)
        if key not in seen:
            result.append(reason)
            seen.add(key)
    return result


class AliyunApi(BaseCloudApi):
    """Generic Alibaba Cloud API tool.

    Can call any Aliyun product API through the common OpenAPI SDK.
    """

    def validation_error_result(self, tool_input: dict[str, Any]) -> ToolResult | None:
        try:
            _normalize_runtime_input(tool_input, allow_arbitrary_json_body=True)
        except ApiContractError as error:
            return ToolResult.error(
                public_aliyun_error(
                    error,
                    product=tool_input.get("product"),
                    version=tool_input.get("version"),
                    action=tool_input.get("action"),
                    region_id=tool_input.get("region_id"),
                )
            )
        return ToolResult.error(
            public_aliyun_error(
                "invalid_tool_input",
                product=tool_input.get("product"),
                version=tool_input.get("version"),
                action=tool_input.get("action"),
                region_id=tool_input.get("region_id"),
            )
        )

    def timeout_error_result(self, tool_input: dict[str, Any], timeout: float) -> ToolResult | None:
        del timeout
        return ToolResult.error(
            public_aliyun_error(
                "unknown_after_cancel",
                product=tool_input.get("product"),
                version=tool_input.get("version"),
                action=tool_input.get("action"),
                region_id=tool_input.get("region_id"),
            )
        )

    def timeout_error_result_with_context(
        self,
        tool_input: dict[str, Any],
        timeout: float,
        context: ToolContext,
    ) -> ToolResult | None:
        del timeout
        return ToolResult.error(
            public_aliyun_error(
                "unknown_after_cancel" if context.request_started else "pretarget_timeout",
                product=tool_input.get("product"),
                version=tool_input.get("version"),
                action=tool_input.get("action"),
                region_id=tool_input.get("region_id"),
            )
        )

    def __init__(
        self,
        services: Any | None = None,
        *,
        _isolated_legacy_test: bool = False,
    ) -> None:
        self._runtime_services = services
        self._isolated_legacy_test = _isolated_legacy_test
        self._legacy_endpoints = _load_legacy_endpoints() if _isolated_legacy_test else {}
        self._legacy_endpoints_canonical = {key.lower(): key for key in self._legacy_endpoints}

    @classmethod
    def isolated_for_tests(cls) -> AliyunApi:
        """Build the pre-runtime adapter only for isolated legacy regression tests."""

        return cls(_isolated_legacy_test=True)

    @property
    def requires_runtime_execution_class(self) -> bool:
        return self._runtime_services is not None

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
        if self._runtime_services is not None:
            provider = getattr(self._runtime_services, "default_region_provider", None)
            if callable(provider):
                region = provider()
                return region if isinstance(region, str) and region else DEFAULT_REGION
            return DEFAULT_REGION
        credentials = CloudCredentials()
        cred = credentials.get_provider("aliyun")
        return cred.region_id if cred else ""

    def prepare_invocation_input(self, tool_input: Mapping[str, Any]) -> dict[str, Any]:
        """Bind the configured local region before invocation hashing and permission resolution."""

        try:
            prepared = copy.deepcopy(dict(tool_input))
        except (TypeError, ValueError) as error:
            raise ApiContractError("invalid_tool_input") from error
        if self._runtime_services is not None and "region_id" not in prepared:
            try:
                region = self._get_default_region()
            except Exception:
                region = DEFAULT_REGION
            prepared["region_id"] = region if isinstance(region, str) and region else DEFAULT_REGION
        return prepared

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

    def _operation_metadata(
        self,
        input: dict,
        *,
        contract: CanonicalWireContract | None = None,
    ) -> dict[str, object]:
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
        if contract is not None:
            operation.update(
                {
                    "api_version": contract.version,
                    "api_style": contract.style,
                    "http_method": contract.method,
                    "operation_type": contract.operation_type,
                    "metadata_source": contract.metadata_source,
                }
            )
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
        contract: CanonicalWireContract | None = None,
    ) -> PermissionAuditMetadata:
        return PermissionAuditMetadata(
            scope=scope,
            source="permission_pipeline",
            rule_source=rule_source,
            rule=rule,
            reason_type=reason.type if reason else None,
            reason_detail=reason.detail if reason else None,
            is_read_only=is_read_only,
            operation=self._operation_metadata(input, contract=contract),
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
        if self._runtime_services is not None:
            return await self._check_runtime_permissions(self.prepare_invocation_input(input), context)
        if not isinstance(context, ToolPermissionContext):
            context = ToolPermissionContext(cwd=context.get("cwd", "") if isinstance(context, dict) else "")

        if path_result := self._check_local_template_url_read_permission(input, context):
            return path_result

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

    async def check_shape_permissions(
        self,
        shape: Mapping[str, Any],
        context: ToolPermissionContext,
    ) -> PermissionResult:
        """Check a model-side delegated shape against its outer invocation binding."""

        if self._runtime_services is None:
            return PermissionResult(behavior="deny", message=public_aliyun_error("aliyun_runtime_services_required"))
        return await self._check_runtime_permissions(
            self.prepare_invocation_input(shape),
            context,
            allow_internal_shape=True,
        )

    async def _check_runtime_permissions(
        self,
        input: Mapping[str, Any],
        context: Any,
        *,
        allow_internal_shape: bool = False,
    ) -> PermissionResult:
        runtime = self._runtime_services
        if runtime is None:
            return PermissionResult(behavior="deny", message=public_aliyun_error("aliyun_runtime_services_required"))
        if not isinstance(context, ToolPermissionContext) or context.invocation_binding is None:
            return PermissionResult(
                behavior="deny",
                message=public_aliyun_error(
                    "aliyun_invocation_binding_required",
                    product=input.get("product"),
                    version=input.get("version"),
                    action=input.get("action"),
                    region_id=input.get("region_id"),
                ),
            )
        if not allow_internal_shape and (
            not isinstance(context.invocation_binding, InvocationBinding)
            or context.invocation_binding.tool_name != self.name
            or context.invocation_binding.canonical_input_sha256 != canonical_input_sha256(input)
        ):
            return PermissionResult(
                behavior="deny",
                message=public_aliyun_error(
                    "aliyun_public_binding_required",
                    product=input.get("product"),
                    version=input.get("version"),
                    action=input.get("action"),
                    region_id=input.get("region_id"),
                ),
            )

        stages = getattr(runtime, "permission_stage_observer", None)

        def observe(stage: str) -> None:
            if callable(stages):
                stages(stage)

        observe("local_input")
        try:
            model_schema_valid = True
            if not allow_internal_shape:
                model_schema_valid = self.validate_input(dict(input))[0]
            normalized = _normalize_runtime_input(
                input,
                allow_internal_shape=allow_internal_shape,
                allow_arbitrary_json_body=True,
            )
            if not model_schema_valid:
                raise ApiContractError("invalid_tool_input")
            initial_shape = _runtime_call_shape(normalized)
        except ApiContractError as error:
            return PermissionResult(
                behavior="deny",
                message=public_aliyun_error(
                    error,
                    product=input.get("product"),
                    version=input.get("version"),
                    action=input.get("action"),
                    region_id=input.get("region_id"),
                ),
            )

        observe("pipeline_guard")
        if error := _runtime_pipeline_guard(normalized, pipeline_mode=context.pipeline_mode):
            return PermissionResult(behavior="deny", message=error)

        pending_reasons: list[PermissionDecisionReason] = []
        observe("file_permission")
        file_authorizations = self._runtime_file_authorizations(normalized, context)
        for authorization in file_authorizations:
            path_result = authorization.permission
            if path_result is None:
                continue
            if path_result.behavior == "deny":
                return path_result
            if path_result.behavior == "ask" and path_result.reason is not None:
                pending_reasons.append(path_result.reason)

        observe("local_rules")
        deny_match = self._matching_rule(normalized, context.deny_rules)
        if deny_match is not None:
            source, rule = deny_match
            detail = _("matched deny rule: {rule}").format(rule=rule)
            reason = PermissionDecisionReason(type="rule", detail=detail)
            return PermissionResult(
                behavior="deny",
                message=detail,
                reason=reason,
                reasons=[reason],
                audit=self._audit(
                    normalized,
                    scope=scope_for_rule_source(source),
                    rule_source=source,
                    rule=rule,
                    reason=reason,
                    is_read_only=False,
                ),
            )
        ask_source: str | None = None
        ask_rule: str | None = None
        ask_match = self._matching_rule(normalized, context.ask_rules)
        if ask_match is not None:
            ask_source, ask_rule = ask_match
            pending_reasons.append(
                PermissionDecisionReason(
                    type="rule",
                    detail=_("matched ask rule(s): {}").format(ask_rule),
                )
            )

        observe("openmeta")
        try:
            contract = await runtime.contract_resolver.resolve(initial_shape, allow_fallback=True)
            metadata_contract = contract
            if initial_shape.explicit_overrides and contract.metadata_source != "explicit_fallback":
                metadata_contract = await runtime.contract_resolver.resolve(
                    replace(initial_shape, explicit_overrides=()),
                    allow_fallback=True,
                )
        except (ApiContractError, ValueError) as error:
            return PermissionResult(
                behavior="deny",
                message=public_aliyun_error(
                    error,
                    product=(
                        getattr(error, "product", None) or _canonical_public_product(str(normalized.get("product", "")))
                    ),
                    version=normalized.get("version"),
                    action=normalized.get("action"),
                    region_id=normalized.get("region_id"),
                ),
            )

        if str(normalized.get("product", "")) != contract.product:
            try:
                canonical_normalized = _with_canonical_runtime_product(normalized, contract.product)
            except ApiContractError as error:
                return PermissionResult(
                    behavior="deny",
                    message=public_aliyun_error(
                        error,
                        product=contract.product,
                        version=contract.version,
                        action=contract.action,
                        region_id=normalized.get("region_id"),
                    ),
                )
            if error := _runtime_pipeline_guard(canonical_normalized, pipeline_mode=context.pipeline_mode):
                return PermissionResult(behavior="deny", message=error)
            file_authorizations = self._runtime_file_authorizations(canonical_normalized, context)
            for authorization in file_authorizations:
                path_result = authorization.permission
                if path_result is None:
                    continue
                if path_result.behavior == "deny":
                    return path_result
                if path_result.behavior == "ask" and path_result.reason is not None:
                    pending_reasons.append(path_result.reason)
            canonical_deny = self._matching_rule(canonical_normalized, context.deny_rules)
            if canonical_deny is not None:
                source, rule = canonical_deny
                detail = _("matched deny rule: {rule}").format(rule=rule)
                reason = PermissionDecisionReason(type="rule", detail=detail)
                return PermissionResult(
                    behavior="deny",
                    message=detail,
                    reason=reason,
                    reasons=[reason],
                    audit=self._audit(
                        canonical_normalized,
                        scope=scope_for_rule_source(source),
                        rule_source=source,
                        rule=rule,
                        reason=reason,
                        is_read_only=False,
                        contract=contract,
                    ),
                )
            canonical_ask = self._matching_rule(canonical_normalized, context.ask_rules)
            if canonical_ask is not None:
                ask_source, ask_rule = canonical_ask
                pending_reasons.append(
                    PermissionDecisionReason(
                        type="rule",
                        detail=_("matched ask rule(s): {}").format(ask_rule),
                    )
                )
            normalized = canonical_normalized

        final_shape = _runtime_call_shape(normalized, contract=contract)
        if not contract.executable:
            reason = contract.unsupported_reasons[0] if contract.unsupported_reasons else "contract_not_executable"
            return PermissionResult(
                behavior="deny",
                message=public_aliyun_error(
                    reason,
                    product=contract.product,
                    version=contract.version,
                    action=contract.action,
                    region_id=normalized.get("region_id"),
                ),
            )

        is_read_only = _runtime_is_read_only(
            contract,
            final_shape,
            metadata_contract,
        )
        execution_class: ExecutionClass = "concurrent" if is_read_only else "serial"
        allow_match = self._matching_rule(
            normalized,
            context.allow_rules,
            require_exact=not is_read_only,
        )
        if not is_read_only and allow_match is None:
            pending_reasons.append(
                PermissionDecisionReason(
                    type="untrusted_write",
                    detail="Aliyun API action may modify cloud resources",
                )
            )
        reasons = _dedupe_permission_reasons(pending_reasons)
        audit_items: list[tuple[PermissionDecisionReason, PermissionAuditMetadata]] = []
        for audit_reason in reasons[:MAX_PERMISSION_AUDIT_ITEMS]:
            rule_reason = audit_reason.type == "rule" and ask_source is not None and ask_rule is not None
            sanitized_reason = PermissionDecisionReason(type=audit_reason.type, detail=audit_reason.type)
            audit_items.append(
                (
                    audit_reason,
                    self._audit(
                        normalized,
                        scope=scope_for_rule_source(ask_source) if rule_reason else "once",
                        rule_source=ask_source if rule_reason else None,
                        rule=ask_rule if rule_reason else None,
                        reason=sanitized_reason,
                        is_read_only=is_read_only,
                        contract=contract,
                    ),
                )
            )
        digest = contract.security_digest(final_shape)
        store: ResolvedContractStore = runtime.contract_store
        try:
            snapshot_id = store.create(
                binding=context.invocation_binding,
                contract=contract,
                security_digest=digest,
                execution_class=execution_class,
                authorized_read_paths=tuple(
                    AuthorizedReadPath(authorization.source, authorization.resolved_path)
                    for authorization in file_authorizations
                ),
            )
        except (ResolvedContractError, RuntimeError) as error:
            return PermissionResult(
                behavior="deny",
                message=public_aliyun_error(
                    error,
                    product=contract.product,
                    version=contract.version,
                    action=contract.action,
                    region_id=normalized.get("region_id"),
                ),
            )

        if reasons:
            primary = next((reason for reason in reversed(reasons) if reason.type == "untrusted_write"), reasons[-1])
            primary_audit = next(audit for reason, audit in audit_items if reason is primary)
            return PermissionResult(
                behavior="ask",
                message=_("Allow {}?").format(self.user_facing_name(normalized)),
                reason=primary,
                reasons=reasons,
                suggestions=self._suggestion(normalized, is_read_only=is_read_only),
                audit=primary_audit,
                audit_items=tuple(audit for _, audit in audit_items),
                invocation_binding=context.invocation_binding,
                snapshot_id=snapshot_id,
                security_digest=digest,
                execution_class=execution_class,
            )

        reason = PermissionDecisionReason(type="read_only", detail="read-only Aliyun API action")
        scope = "read_only"
        rule_source = None
        rule = None
        if allow_match is not None:
            rule_source, rule = allow_match
            scope = scope_for_rule_source(rule_source)
            reason = PermissionDecisionReason(type="rule", detail="matched allow rule: {}".format(rule))
        audit = self._audit(
            normalized,
            scope=scope,
            rule_source=rule_source,
            rule=rule,
            reason=reason,
            is_read_only=is_read_only,
            contract=contract,
        )
        return PermissionResult(
            behavior="allow",
            reason=reason,
            reasons=[reason],
            audit=audit,
            audit_items=(audit,),
            invocation_binding=context.invocation_binding,
            snapshot_id=snapshot_id,
            security_digest=digest,
            execution_class=execution_class,
        )

    def _runtime_file_permission_results(
        self,
        input: dict[str, Any],
        context: ToolPermissionContext | ToolContext,
    ) -> list[PermissionResult]:
        return [
            authorization.permission
            for authorization in self._runtime_file_authorizations(input, context)
            if authorization.permission is not None
        ]

    def _runtime_file_authorizations(
        self,
        input: dict[str, Any],
        context: ToolPermissionContext | ToolContext,
    ) -> list[_RuntimeFileAuthorization]:
        authorizations: list[_RuntimeFileAuthorization] = []
        template_url = self._local_template_url(input)
        if template_url is not None:
            authorizations.append(self._authorize_runtime_file("template", template_url, context))
        body_file = input.get("body_file")
        if isinstance(body_file, str) and body_file:
            authorizations.append(self._authorize_runtime_file("body_file", body_file, context))
        return authorizations

    @staticmethod
    def _authorize_runtime_file(
        source: Literal["template", "body_file"],
        path: str,
        context: ToolPermissionContext | ToolContext,
    ) -> _RuntimeFileAuthorization:
        decision, resolution = check_read_path_with_resolution(
            path,
            cwd=context.cwd or ".",
            additional_directories=list(context.additional_directories),
            trusted_read_directories=list(context.trusted_read_directories),
            relative_read_directories=list(context.relative_read_directories),
            strict_read_directories=list(context.strict_read_directories),
            read_path_violation_behavior=context.read_path_violation_behavior,
        )
        permission = None if decision.behavior == "allow" else decision.to_permission_result()
        return _RuntimeFileAuthorization(source, resolution.path, permission)

    @staticmethod
    def _local_template_url(input: dict[str, Any]) -> str | None:
        product = input.get("product", "")
        product = _PRODUCT_CANONICAL.get(str(product).lower(), product)
        if product != "ros":
            return None
        params = input.get("params") or {}
        if not isinstance(params, dict):
            return None
        template_url = input.get(LOCAL_TEMPLATE_PATH_FIELD, params.get("TemplateURL", ""))
        if not isinstance(template_url, str) or not template_url or not is_local_template_url(template_url):
            return None
        return template_url

    def _check_local_template_url_read_permission(
        self,
        input: dict[str, Any],
        context: ToolPermissionContext | ToolContext,
    ) -> PermissionResult | None:
        template_url = self._local_template_url(input)
        if template_url is None:
            return None
        return check_local_template_url_read_permission(template_url, context)

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
                    "pattern": _SAFE_RUNTIME_PRODUCT_INPUT.pattern,
                    "description": "The Aliyun product code (e.g. 'ros', 'ecs', 'rds', 'vpc').",
                },
                "action": {
                    "type": "string",
                    "pattern": _SAFE_RUNTIME_IDENTIFIER.pattern,
                    "description": "The API action to call.",
                },
                "version": {
                    "type": "string",
                    "pattern": SAFE_API_VERSION.pattern,
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
                "endpoint": {
                    "type": "string",
                    "minLength": 3,
                    "maxLength": 253,
                    "description": (
                        "API endpoint hostname without scheme or path. "
                        "默认会自动获取，通常不需要传；仅在自动解析失败或需要指定已知 endpoint 时设置。"
                    ),
                },
                "style": {
                    "type": "string",
                    "enum": ["RPC", "ROA"],
                    "description": "API style. Defaults to 'RPC'. Use 'ROA' for RESTful APIs (e.g. CS, CR, FC).",
                },
                "method": {
                    "type": "string",
                    "enum": ["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"],
                    "description": "HTTP method. Defaults to 'POST'. Only needed for ROA APIs.",
                },
                "pathname": {
                    "type": "string",
                    "description": "Request path. Defaults to '/'. Only needed for ROA APIs (e.g. '/clusters').",
                },
                "body": {
                    "description": "Arbitrary JSON request body for APIs that declare one.",
                },
                "body_file": {
                    "type": "string",
                    "minLength": 1,
                    "description": "Local file containing a binary request body.",
                },
                "content_type": {
                    "type": "string",
                    "minLength": 1,
                    "description": "Explicit request media type.",
                },
                "max_response_bytes": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": _MAX_RUNTIME_RESPONSE_BYTES,
                    "description": "Maximum accepted response size in bytes.",
                },
            },
            "required": ["product", "action"],
            "additionalProperties": False,
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

    def _get_endpoint(self, product: str, region_id: str = "") -> str | None:
        """Resolve an endpoint for the isolated pre-runtime regression adapter."""
        config = self._legacy_endpoints.get(product)
        if config is None:
            canonical = self._legacy_endpoints_canonical.get(product.lower())
            if canonical:
                config = self._legacy_endpoints[canonical]
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

        if mode in {"RamRoleArn", "EcsRamRole"}:
            from iac_code.services.providers.aliyun_credentials_runtime import aliyun_credential_runtime

            # The runtime always returns a client for these two dynamic modes; the SDK
            # client type is only available through a lazy import, hence the Any binding.
            dynamic_client: Any = aliyun_credential_runtime().sdk_client(credential)
            return open_api_models.Config(
                credential=dynamic_client,
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
        context.ros_preflight_outcome = None
        runtime_input = tool_input
        if self._runtime_services is not None:
            runtime_input = self.prepare_invocation_input(tool_input)
            if error := self._public_preflight_handoff_error(runtime_input, context):
                self._reject_runtime_handoff(context)
                return ToolResult.error(
                    public_aliyun_error(
                        error,
                        product=runtime_input.get("product"),
                        version=runtime_input.get("version"),
                        action=runtime_input.get("action"),
                        region_id=runtime_input.get("region_id"),
                    )
                )
        product = str(runtime_input.get("product") or "").casefold()
        params = runtime_input.get("params")
        ros_preflight_completed = False
        if (
            not context.pipeline_mode
            and product in {"ros", "resourceorchestrationservice"}
            and isinstance(params, dict)
        ):
            # Source cardinality and already-materialized TemplateBody are
            # stage-zero checks: validate them before region/credential setup.
            from iac_code.tools.cloud.aliyun.api_hooks import run_hooks

            hook_result = run_hooks(
                "ros",
                str(tool_input.get("action") or ""),
                params,
                context=context,
                read_only=True,
            )
            if hook_result is not None:
                if self._runtime_services is not None:
                    self._reject_runtime_handoff(context)
                return hook_result
            template_url = params.get("TemplateURL")
            local_template_materialization_pending = (
                isinstance(template_url, str) and bool(template_url) and is_local_template_url(template_url)
            )
            ros_preflight_completed = bool(
                context.ros_preflight_outcome is not None and not local_template_materialization_pending
            )
        if self._runtime_services is not None:
            return await self._execute_runtime(
                api_input=runtime_input,
                binding_input=runtime_input,
                context=context,
                trust_path="public",
                ros_preflight_completed=ros_preflight_completed,
            )
        if self._isolated_legacy_test:
            result = await self._execute_legacy(
                tool_input=tool_input,
                context=context,
                ros_preflight_completed=ros_preflight_completed,
            )
            return self._attach_ros_preflight(result, context)
        result = ToolResult.error(
            public_aliyun_error(
                "aliyun_runtime_services_required",
                product=tool_input.get("product"),
                version=tool_input.get("version"),
                action=tool_input.get("action"),
                region_id=tool_input.get("region_id"),
            )
        )
        return self._attach_ros_preflight(result, context)

    def _public_preflight_handoff_error(
        self,
        binding_input: Mapping[str, Any],
        context: ToolContext,
    ) -> ApiContractError | None:
        """Validate a public handoff before a stage-zero early return can bypass it."""

        binding = context.invocation_binding
        if binding is None or context.snapshot_id is None or context.security_digest is None:
            return ApiContractError("aliyun_runtime_handoff_required")
        if binding.canonical_input_sha256 != canonical_input_sha256(binding_input):
            return ApiContractError("aliyun_invocation_binding_mismatch")
        if context.tool_use_id is not None and binding.tool_use_id != context.tool_use_id:
            return ApiContractError("aliyun_invocation_binding_mismatch")
        if binding.tool_name != self.name:
            return ApiContractError("aliyun_public_binding_required")
        return None

    async def execute_delegated(
        self,
        shape: Mapping[str, Any],
        tool_input: Mapping[str, Any],
        context: ToolContext,
    ) -> ToolResult:
        context.ros_preflight_outcome = None
        if self._runtime_services is None:
            return ToolResult.error(
                public_aliyun_error(
                    "aliyun_runtime_services_required",
                    product=shape.get("product"),
                    version=shape.get("version"),
                    action=shape.get("action"),
                    region_id=shape.get("region_id"),
                )
            )
        action = shape.get("action")
        if not isinstance(action, str) or not _valid_delegated_binding(
            tool_input,
            context.invocation_binding,
            action=action,
        ):
            self._invalidate_runtime_handoff(context)
            return ToolResult.error(
                public_aliyun_error(
                    "aliyun_delegated_outer_binding_required",
                    product=shape.get("product"),
                    version=shape.get("version"),
                    action=shape.get("action"),
                    region_id=shape.get("region_id"),
                )
            )
        from iac_code.tools.cloud.aliyun.ros_template_tools import validate_delegated_tool_input

        if not validate_delegated_tool_input(tool_input, action=action):
            self._invalidate_runtime_handoff(context)
            return ToolResult.error(
                public_aliyun_error(
                    "invalid_tool_input",
                    product=shape.get("product"),
                    version=shape.get("version"),
                    action=shape.get("action"),
                    region_id=shape.get("region_id"),
                )
            )
        return await self._execute_runtime(
            api_input=self.prepare_invocation_input(shape),
            binding_input=tool_input,
            context=context,
            trust_path="delegated",
        )

    def _invalidate_runtime_handoff(self, context: ToolContext) -> None:
        runtime = self._runtime_services
        if runtime is not None and isinstance(context.snapshot_id, str):
            runtime.contract_store.cancel(context.snapshot_id)

    def _reject_runtime_handoff(self, context: ToolContext) -> None:
        runtime = self._runtime_services
        if runtime is not None and isinstance(context.snapshot_id, str):
            runtime.contract_store.reject(context.snapshot_id)

    async def execute_internal(
        self,
        tool_input: Mapping[str, Any],
        context: ToolContext,
    ) -> ToolResult:
        del tool_input, context
        return ToolResult.error("aliyun_internal_capability_required")

    async def _execute_internal_trusted(
        self,
        tool_input: Mapping[str, Any],
        context: ToolContext,
    ) -> ToolResult:
        context.ros_preflight_outcome = None
        if self._runtime_services is None:
            return ToolResult.error("aliyun_runtime_services_required")
        return await self._execute_runtime(
            api_input=self.prepare_invocation_input(tool_input),
            binding_input=None,
            context=context,
            trust_path="internal",
        )

    @staticmethod
    def _attach_ros_preflight(result: ToolResult, context: ToolContext) -> ToolResult:
        from iac_code.tools.cloud.aliyun.ros_validation.outcome import attach_ros_validation

        return attach_ros_validation(result, context.ros_preflight_outcome)

    async def _execute_runtime(
        self,
        *,
        api_input: Mapping[str, Any],
        binding_input: Mapping[str, Any] | None,
        context: ToolContext,
        trust_path: str,
        ros_preflight_completed: bool = False,
    ) -> ToolResult:
        result = await self._execute_runtime_unattached(
            api_input=api_input,
            binding_input=binding_input,
            context=context,
            trust_path=trust_path,
            ros_preflight_completed=ros_preflight_completed,
        )
        return self._attach_ros_preflight(result, context)

    async def _execute_runtime_unattached(
        self,
        *,
        api_input: Mapping[str, Any],
        binding_input: Mapping[str, Any] | None,
        context: ToolContext,
        trust_path: str,
        ros_preflight_completed: bool = False,
    ) -> ToolResult:
        runtime = self._runtime_services
        if runtime is None:
            return ToolResult.error(
                public_aliyun_error(
                    "aliyun_runtime_services_required",
                    product=api_input.get("product"),
                    version=api_input.get("version"),
                    action=api_input.get("action"),
                    region_id=api_input.get("region_id"),
                )
            )
        context.request_started = False
        observer = getattr(runtime, "execution_stage_observer", None)

        def observe(stage: str) -> None:
            if callable(observer):
                observer(stage)

        recovery_claim: ResolvedContractRecovery | None = None
        contract: CanonicalWireContract | None = None
        authorized_read_paths: tuple[AuthorizedReadPath, ...] = ()
        try:
            observe("normalize_trust")
            if trust_path == "internal":
                if any(
                    value is not None
                    for value in (
                        context.invocation_binding,
                        context.snapshot_id,
                        context.security_digest,
                        context.execution_class,
                    )
                ):
                    raise ApiContractError("aliyun_internal_handoff_forbidden")
            else:
                if context.invocation_binding is None or context.snapshot_id is None or context.security_digest is None:
                    raise ApiContractError("aliyun_runtime_handoff_required")
                if binding_input is None:
                    raise ApiContractError("aliyun_runtime_handoff_required")
                binding = context.invocation_binding
                if binding.canonical_input_sha256 != canonical_input_sha256(binding_input):
                    raise ApiContractError("aliyun_invocation_binding_mismatch")
                if context.tool_use_id is not None and binding.tool_use_id != context.tool_use_id:
                    raise ApiContractError("aliyun_invocation_binding_mismatch")
                if trust_path == "public" and binding.tool_name != self.name:
                    raise ApiContractError("aliyun_public_binding_required")
            model_schema_valid = True
            if trust_path == "public":
                model_schema_valid = self.validate_input(dict(api_input))[0]
            normalized = _normalize_runtime_input(
                api_input,
                allow_internal_shape=trust_path == "delegated",
                allow_arbitrary_json_body=True,
            )
            if not model_schema_valid:
                raise ApiContractError("invalid_tool_input")
            initial_shape = _runtime_call_shape(normalized)

            observe("local_authorization")
            if error := _runtime_pipeline_guard(normalized, pipeline_mode=context.pipeline_mode):
                raise ApiContractError(error)
            if trust_path == "internal":
                for result in self._runtime_file_permission_results(normalized, context):
                    if result.behavior in {"ask", "deny"}:
                        raise ApiContractError(result.message or "aliyun_file_permission_required")

            observe("contract")
            recovery_metadata_contract: CanonicalWireContract | None = None
            if trust_path == "internal":
                contract = await runtime.contract_resolver.resolve(initial_shape, allow_fallback=True)
            else:
                assert context.invocation_binding is not None
                assert context.snapshot_id is not None
                assert context.security_digest is not None
                handoff = runtime.contract_store.consume(
                    snapshot_id=context.snapshot_id,
                    binding=context.invocation_binding,
                    security_digest=context.security_digest,
                )
                authorized_read_paths = handoff.authorized_read_paths
                if isinstance(handoff, ResolvedContractRecovery):
                    recovery_claim = handoff
                    contract = await runtime.contract_resolver.resolve(
                        initial_shape,
                        allow_fallback=True,
                    )
                    recovery_metadata_contract = contract
                    if initial_shape.explicit_overrides and contract.metadata_source != "explicit_fallback":
                        recovery_metadata_contract = await runtime.contract_resolver.resolve(
                            replace(initial_shape, explicit_overrides=()),
                            allow_fallback=True,
                        )
                else:
                    contract = handoff.contract
                    if context.execution_class is not None and handoff.execution_class != context.execution_class:
                        raise ApiContractError("snapshot_execution_class_mismatch")
            if str(normalized.get("product", "")) != contract.product:
                canonical_normalized = _with_canonical_runtime_product(normalized, contract.product)
                if error := _runtime_pipeline_guard(canonical_normalized, pipeline_mode=context.pipeline_mode):
                    raise ApiContractError(error)
                if trust_path == "internal":
                    for result in self._runtime_file_permission_results(canonical_normalized, context):
                        if result.behavior in {"ask", "deny"}:
                            raise ApiContractError(result.message or "aliyun_file_permission_required")
                normalized = canonical_normalized
            final_shape = _runtime_call_shape(normalized, contract=contract)
            digest = contract.security_digest(final_shape)
            if trust_path != "internal" and digest != context.security_digest:
                raise ApiContractError("snapshot_digest_mismatch")
            if recovery_metadata_contract is not None:
                expected_execution_class: ExecutionClass = (
                    "concurrent"
                    if _runtime_is_read_only(
                        contract,
                        final_shape,
                        recovery_metadata_contract,
                    )
                    else "serial"
                )
                if context.execution_class != expected_execution_class:
                    raise ApiContractError("snapshot_execution_class_mismatch")
            if not contract.executable:
                reason = contract.unsupported_reasons[0] if contract.unsupported_reasons else "contract_not_executable"
                raise ApiContractError(reason)
            if recovery_claim is not None:
                assert context.invocation_binding is not None
                assert context.snapshot_id is not None
                runtime.contract_store.complete_recovery(
                    snapshot_id=context.snapshot_id,
                    claim_id=recovery_claim.claim_id,
                    binding=context.invocation_binding,
                    security_digest=digest,
                    execution_class=expected_execution_class,
                )
                recovery_claim = None

            observe("materialize")
            materialized = copy.deepcopy(normalized)
            params = materialized.get("params", {})
            if not isinstance(params, dict):
                raise ApiContractError("invalid_params")
            template_path = materialized.pop(LOCAL_TEMPLATE_PATH_FIELD, None)
            if params.get("TemplateBody") is LOCAL_TEMPLATE_BODY_SENTINEL:
                if not isinstance(template_path, str) or not template_path:
                    raise ApiContractError("invalid_template_file")
                resolved_template = (
                    _runtime_materialization_path(template_path, context)
                    if trust_path == "internal"
                    else _authorized_materialization_path("template", authorized_read_paths)
                )
                try:
                    template_bytes = await asyncio.to_thread(_read_body_file, resolved_template)
                    params["TemplateBody"] = template_bytes.decode("utf-8")
                except (OSError, UnicodeError) as error:
                    from iac_code.tools.cloud.aliyun.hooks.ros_validate import local_template_source_error

                    outcome = local_template_source_error(error)
                    context.ros_preflight_outcome = outcome
                    assert outcome.blocking_result is not None
                    return outcome.blocking_result
            body_file = materialized.get("body_file")
            if isinstance(body_file, str):
                resolved_body_file = (
                    _runtime_materialization_path(body_file, context)
                    if trust_path == "internal"
                    else _authorized_materialization_path("body_file", authorized_read_paths)
                )
                materialized["body_file"] = await asyncio.to_thread(_read_body_file, resolved_body_file)
            region_id = materialized.get("region_id")
            if isinstance(region_id, str) and region_id:
                for parameter in contract.parameters:
                    if (
                        parameter.location == "query"
                        and parameter.name.casefold() == "regionid"
                        and parameter.name not in params
                    ):
                        params[parameter.name] = region_id
                        break

            if not ros_preflight_completed:
                observe("hooks")
                from iac_code.tools.cloud.aliyun.api_hooks import run_hooks

                hook_result = run_hooks(contract.product.casefold(), contract.action, params, context=context)
                if hook_result is not None:
                    return hook_result
            post_hook_shape = _runtime_call_shape(materialized, contract=contract)
            if post_hook_shape.security_view() != final_shape.security_view():
                raise ApiContractError("hook_call_shape_changed")

            observe("request_builder")
            explicit_endpoint = materialized.pop("endpoint", None)
            request = await runtime.request_builder.build(contract, materialized)

            observe("credential")
            credential = None
            if contract.auth_type != "Anonymous":
                credential_provider = getattr(runtime, "credential_provider", None)
                if not callable(credential_provider):
                    raise ApiContractError("aliyun_credential_provider_required")
                credential = credential_provider()
                if inspect.isawaitable(credential):
                    credential = await credential
                if credential is None:
                    raise ApiContractError("aliyun_credentials_required")

            observe("endpoint")
            try:
                endpoint = await runtime.endpoint_resolver.resolve(
                    contract,
                    final_shape.region_id,
                    credential,
                    host_values=request.host_values,
                    explicit_endpoint=explicit_endpoint,
                )
                final_host = runtime.host_binding_resolver.bind(
                    contract,
                    endpoint.endpoint,
                    endpoint.host_template,
                    request.host_values,
                )
            except Exception as error:
                emit_aliyun_endpoint_resolution("error")
                if not isinstance(error, ApiContractError):
                    emit_aliyun_api_contract_error("host" if "host" in str(error).casefold() else "endpoint")
                raise
            emit_aliyun_endpoint_resolution(endpoint.source)
            endpoint = replace(
                endpoint,
                expected_host=final_host,
                region_id=final_shape.region_id,
            )

            observe("transport")
            prepared_transport = runtime.transport_router.prepare(
                contract=contract,
                request=request,
                endpoint=endpoint,
                credential=credential,
                context=context,
            )

            observe("target")
            context.request_started = True
            target_started = time.monotonic()
            budget_factory = getattr(runtime, "retry_budget_factory", None)
            budget = (
                budget_factory()
                if callable(budget_factory)
                else RetryBudget(
                    deadline=time.monotonic() + (self.timeout or 120.0),
                    random=getattr(runtime, "random", lambda: 0.0),
                )
            )
            response = None
            target_outcome = "target_transport_failure"
            target_error_message: str | None = None
            target_error_detail: str | None = None
            target_error: BaseException | None = None
            with start_span(
                Spans.ALIYUN_API_CALL,
                _aliyun_api_span_attrs(contract, final_shape, context),
            ) as api_span:
                try:
                    response = await prepared_transport.execute(budget=budget)
                    if not 200 <= response.status < 300:
                        target_outcome = "http_error"
                        target_error_message = _target_http_error_message(response)
                        target_error_detail = _target_http_error_detail(response)
                    else:
                        target_outcome = "success"
                except asyncio.CancelledError as error:
                    target_outcome = "unknown_after_cancel"
                    target_error = error
                    raise
                except Exception as error:
                    target_outcome = _target_failure_outcome(error)
                    target_error_message = _target_failure_message(error)
                    target_error = error
                finally:
                    duration_ms = max(0, int((time.monotonic() - target_started) * 1000))
                    _record_aliyun_api_span_result(
                        api_span,
                        target_outcome=target_outcome,
                        response=response,
                        error=target_error,
                    )
                    emit_aliyun_api_called(
                        metadata_source=contract.metadata_source,
                        api_style=contract.style,
                        http_method=contract.method,
                        transport=contract.transport,
                        signature_scheme=contract.signature_scheme,
                        endpoint_source=endpoint.source,
                        host_template_applied=endpoint.host_template is not None,
                        contract_override_used=bool(final_shape.explicit_overrides),
                        openmeta_cache_status=contract.openmeta_cache_status,
                        outcome=target_outcome,
                    )
                    legacy_outcome = "success" if target_outcome == "success" else "failure"
                    log_event(Events.ALIYUN_API_LEGACY_CALLED, {"outcome": legacy_outcome})
                    metric_attributes = _aliyun_api_metric_attrs(
                        contract.product,
                        legacy_outcome,
                        target_outcome,
                    )
                    add_metric(
                        Metrics.ALIYUN_API_CALLED_COUNT,
                        1,
                        metric_attributes,
                    )
                    add_metric(Metrics.ALIYUN_API_CALLED_DURATION, duration_ms, metric_attributes)
                    outcome_observer = getattr(runtime, "target_outcome_observer", None)
                    if callable(outcome_observer):
                        outcome_observer({"outcome": target_outcome, "duration_ms": duration_ms})
            if target_error_message is not None:
                self._last_action = ""
                self._last_result = None
                public_error = public_aliyun_error(
                    target_error_message,
                    product=contract.product,
                    version=contract.version,
                    action=contract.action,
                    region_id=final_shape.region_id,
                )
                if target_error_detail is not None:
                    public_error = f"{public_error} Response: {target_error_detail}"
                return ToolResult.error(public_error)
            assert response is not None
            business_content, body_format = serialize_business_result(response, request, contract)
            aliyun_http = build_aliyun_http_metadata(
                response,
                request,
                contract,
                body_format=body_format,
            )
            if contract.product.casefold() == "ros" and contract.action == "ValidateTemplate":
                _emit_validate_template_event(response.body, duration_ms)
            if (
                context.event_queue is not None
                and contract.product.casefold() == "ros"
                and contract.action == "CreateStack"
            ):
                stack_id = _string_value(response.body.get("StackId")) if isinstance(response.body, dict) else None
                if stack_id:
                    await context.event_queue.put(
                        ResourceObservedEvent(
                            provider="ros",
                            resource_type="stack",
                            resource_id=stack_id,
                            resource_name=str(params.get("StackName") or params.get("stack_name") or ""),
                            region_id=final_shape.region_id,
                            action=contract.action,
                            tool_name=self.name,
                            tool_use_id=context.tool_use_id,
                        )
                    )
            self._last_action = contract.action
            self._last_result = response.body
            return ToolResult(
                content=business_content,
                metadata={ALIYUN_HTTP_METADATA_KEY: aliyun_http},
            )
        except asyncio.CancelledError:
            if trust_path != "internal":
                if recovery_claim is not None and context.snapshot_id is not None:
                    runtime.contract_store.cancel_recovery(context.snapshot_id, recovery_claim.claim_id)
                else:
                    self._invalidate_runtime_handoff(context)
            raise
        except Exception as error:
            if trust_path != "internal":
                if recovery_claim is not None and context.snapshot_id is not None:
                    runtime.contract_store.reject_recovery(context.snapshot_id, recovery_claim.claim_id)
                else:
                    self._reject_runtime_handoff(context)
            if isinstance(error, ApiContractError):
                stage = _runtime_contract_error_stage(error)
                if stage is not None:
                    emit_aliyun_api_contract_error(stage)
            self._last_action = ""
            self._last_result = None
            return ToolResult.error(
                public_aliyun_error(
                    error,
                    product=(
                        contract.product
                        if contract is not None
                        else getattr(error, "product", None) or api_input.get("product")
                    ),
                    version=contract.version if contract is not None else api_input.get("version"),
                    action=contract.action if contract is not None else api_input.get("action"),
                    region_id=api_input.get("region_id"),
                )
            )

    async def _execute_legacy(
        self,
        *,
        tool_input: dict[str, Any],
        context: ToolContext,
        ros_preflight_completed: bool = False,
    ) -> ToolResult:
        product = tool_input.get("product", "")
        product = _PRODUCT_CANONICAL.get(product.lower(), product)
        action = tool_input.get("action", "")
        params = tool_input.get("params") or {}
        region = self._resolve_region(tool_input)

        # ROS: TemplateURL as local file path → read into TemplateBody
        if product == "ros":
            if error := reject_pipeline_dedicated_ros_template_action(action, pipeline_mode=context.pipeline_mode):
                return ToolResult.error(error)
            if error := reject_pipeline_dedicated_ros_deployment_action(action, pipeline_mode=context.pipeline_mode):
                return ToolResult.error(error)
            if error := reject_pipeline_template_source_params(action, params, pipeline_mode=context.pipeline_mode):
                return ToolResult.error(error)
            template_url = params.get("TemplateURL", "")
            if isinstance(template_url, str) and template_url and is_local_template_url(template_url):
                if path_result := check_local_template_url_read_permission(template_url, context):
                    if path_result.behavior == "deny":
                        return ToolResult.error(path_result.message)
                try:
                    params["TemplateBody"] = read_local_template_url(template_url, context)
                except (OSError, UnicodeError) as error:
                    from iac_code.tools.cloud.aliyun.hooks.ros_validate import local_template_source_error

                    outcome = local_template_source_error(error)
                    context.ros_preflight_outcome = outcome
                    assert outcome.blocking_result is not None
                    return outcome.blocking_result
                del params["TemplateURL"]

        # Pre-call hooks (e.g. resource type validation)
        if not ros_preflight_completed:
            from iac_code.tools.cloud.aliyun.api_hooks import run_hooks

            if hook_result := run_hooks(product, action, params, context=context):
                return hook_result

        if product == "ros":
            from iac_code.tools.cloud.aliyun.hooks.ros_parameters import normalize_ros_parameters

            normalize_ros_parameters(action, params)

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

        endpoint = self._get_endpoint(product, region)
        if not endpoint:
            # Location-service discovery makes a blocking OpenAPI call; keep it off
            # the shared event loop (web agent turns, SSE, and HTTP handlers run on it).
            endpoint = await asyncio.to_thread(self._discover_endpoint, product, region, credential)
        if not endpoint:
            endpoint = self._get_endpoint_fallback(product, region)
        try:
            config = self._build_config(credential, endpoint, region)
        except ValueError as error:
            # Only the credential runtime's stable ECS codes may be reinterpreted here;
            # any other ValueError keeps its existing handling.
            code = ecs_credential_error_code(error)
            if code is None:
                raise
            return ToolResult.error(
                public_aliyun_error(code, product=product, version=version, action=action, region_id=region)
            )
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
        outcome = "success"

        try:
            # The OpenAPI call is blocking network I/O; offload it so it never
            # starves the shared event loop. Telemetry/event emission stay on-loop.
            result = await asyncio.to_thread(client.call_api, api_params, request, runtime)
            body = result.get("body", result)

            self._last_action = action
            self._last_result = body

            duration_ms = int((time.monotonic() - started) * 1000)
            log_event(
                Events.ALIYUN_API_LEGACY_CALLED,
                {"outcome": outcome},
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

            log_event(
                Events.ALIYUN_API_LEGACY_CALLED,
                {"outcome": outcome},
            )
            add_metric(Metrics.ALIYUN_API_CALLED_COUNT, 1, _aliyun_api_metric_attrs(product, outcome))
            add_metric(Metrics.ALIYUN_API_CALLED_DURATION, duration_ms)

            if (code := ecs_credential_error_code(e)) is not None:
                # The dynamic credential is fetched while signing, so an IMDS failure
                # surfaces here — wrapped in the SDK envelope when `call_api` raised it;
                # render it through the public error mapping.
                return ToolResult.error(
                    public_aliyun_error(code, product=product, version=version, action=action, region_id=region)
                )
            return ToolResult.error(self._clean_error_message(error_str))
