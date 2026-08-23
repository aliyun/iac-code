"""Stable, translated public errors for Alibaba Cloud API tools."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from iac_code.i18n import _
from iac_code.tools.cloud.aliyun.api_contract import ApiContractError
from iac_code.tools.cloud.aliyun.api_identifiers import is_safe_api_version

_SAFE_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_SAFE_PARAMETER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,126}(?:\*)?$")
_SAFE_ALLOWED_VALUE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_ASCII_WHITESPACE = " \t\n\r\f\v"
_TARGET_HTTP_ERROR = re.compile(r"^aliyun_target_http_error:(\d{3})(?::([A-Za-z0-9][A-Za-z0-9_.:-]{0,127}))?$")
_MAX_PUBLIC_PARAMETER_NAMES = 16
_MAX_PUBLIC_PARAMETER_LIST_CHARS = 512
_MAX_PUBLIC_ALLOWED_VALUES = 32
_MAX_PUBLIC_ALLOWED_VALUE_CHARS = 512
_PROTOCOL_UNSUPPORTED_REASONS = frozenset(
    {
        "api_style_unsupported",
        "http_method_unsupported",
        "https_required",
        "parameter_style_unsupported",
        "response_media_type_unsupported",
    }
)
_SCHEMA_UNSUPPORTED_REASONS = frozenset(
    {
        "parameter_schema_reference_unsupported",
        "response_header_metadata_invalid",
        "response_schema_reference_unsupported",
    }
)
_OSS_UNSUPPORTED_REASONS = frozenset(
    {
        "generic_sdk_invocation_forbidden",
        "openmeta_operation_unavailable",
        "presigning_not_supported",
        "sdk_convenience_method",
        "sdk_lifecycle_method",
    }
)


@dataclass(frozen=True)
class AliyunApiIdentity:
    product: str
    action: str
    version: str | None


def normalize_api_identity(tool_input: Mapping[str, Any]) -> AliyunApiIdentity:
    """Validate the identifiers shared by documentation and execution tools."""

    product = tool_input.get("product")
    action = tool_input.get("action")
    version = tool_input.get("version")
    if not isinstance(product, str):
        raise ApiContractError("invalid_product")
    normalized_product = product.strip(_ASCII_WHITESPACE)
    if not normalized_product or _SAFE_IDENTIFIER.fullmatch(normalized_product) is None:
        raise ApiContractError("invalid_product")
    if not isinstance(action, str) or _SAFE_IDENTIFIER.fullmatch(action) is None:
        raise ApiContractError("invalid_action")
    if version is not None and not is_safe_api_version(version):
        raise ApiContractError("invalid_version")
    return AliyunApiIdentity(
        product=product,
        action=action,
        version=version,
    )


def public_aliyun_error(
    error: BaseException | str,
    *,
    product: Any = None,
    version: Any = None,
    action: Any = None,
    region_id: Any = None,
) -> str:
    """Map an internal error to a stable public message without raw values."""

    code = str(error)
    safe_product = _safe_identifier(product, _("the requested product"))
    safe_action = _safe_identifier(action, _("the requested action"))
    safe_version = version if is_safe_api_version(version) else None
    safe_region = _safe_identifier(region_id, _("the requested region"))
    operation = "{}/{}".format(safe_product, safe_action)
    if code == "invalid_product":
        return _("Alibaba Cloud product must contain only letters, numbers, underscores, or hyphens.")
    if code == "invalid_action":
        return _("Alibaba Cloud API action must contain only letters, numbers, underscores, or hyphens.")
    if code == "invalid_version":
        return _("Alibaba Cloud API version contains unsupported characters.")
    if code == "invalid_region_id":
        return _("Alibaba Cloud region must contain only lowercase letters, numbers, or hyphens.")
    if code in {"invalid_style", "invalid_method", "invalid_explicit_override"}:
        return _(
            "Alibaba Cloud API protocol overrides are invalid for {operation}. "
            "Remove style, method, or pathname overrides and retry."
        ).format(operation=operation)
    if code == "invalid_tool_input":
        return _(
            "Alibaba Cloud API input is invalid for {operation}. "
            "Check product, action, version, region, and parameters."
        ).format(operation=operation)
    if code == "aliyun_response_headers_too_large":
        return _(
            "Alibaba Cloud returned the target response, but its headers are too large to display safely. "
            "Verify the cloud resource state before retrying."
        )
    if code == "aliyun_response_body_invalid":
        return _(
            "Alibaba Cloud returned an unsupported response body for {operation}. "
            "Verify the cloud resource state before retrying."
        ).format(operation=operation)
    if code == "invalid_detail":
        return _("Alibaba Cloud API {operation} detail must be one of: summary, full.").format(operation=operation)
    if code == "product_not_found":
        message = _("Alibaba Cloud product {product} was not found. Check the product code and try again.").format(
            product=safe_product
        )
        suggestions = tuple(
            suggestion
            for suggestion in getattr(error, "suggestions", ())
            if isinstance(suggestion, str) and _SAFE_IDENTIFIER.fullmatch(suggestion) is not None
        )[:3]
        if suggestions:
            message += _(" Suggested product codes: {suggestions}.").format(suggestions=", ".join(suggestions))
        return message
    if code == "metadata_not_found":
        if safe_version is not None:
            return _(
                "Alibaba Cloud API {product}/{version}/{action} was not found. Check the product, version, and action."
            ).format(product=safe_product, version=safe_version, action=safe_action)
        return _("Alibaba Cloud API {operation} was not found. Check the product, version, and action.").format(
            operation=operation
        )
    if code == "metadata_unavailable":
        return _("Alibaba Cloud API metadata for {operation} is temporarily unavailable; try again later.").format(
            operation=operation
        )
    if code == "metadata_protocol_error":
        return _(
            "Alibaba Cloud API metadata for {operation} returned an incompatible response. "
            "Check the API identifiers or try again later."
        ).format(operation=operation)
    if code == "invalid_or_missing_version":
        return _("No valid Alibaba Cloud API version is available for {product}; provide an explicit version.").format(
            product=safe_product
        )
    if code.startswith("missing_required_parameters:"):
        parameter = _safe_parameter_list(code.partition(":")[2])
        if parameter == "body_file":
            return _("Alibaba Cloud API {operation} requires body_file for its binary request body.").format(
                operation=operation
            )
        if parameter is not None and "," not in parameter:
            return _("Alibaba Cloud API {operation} requires parameter {parameter}.").format(
                operation=operation,
                parameter=parameter,
            )
        if parameter is not None:
            return _("Alibaba Cloud API {operation} requires parameters {parameters}.").format(
                operation=operation,
                parameters=parameter,
            )
        return _("Alibaba Cloud API {operation} is missing required parameters.").format(operation=operation)
    if code.startswith("invalid_parameter_type:"):
        parameter = _safe_parameter(getattr(error, "parameter", None), _("the requested parameter"))
        expected = _safe_type(getattr(error, "expected_type", None))
        actual = _safe_type(getattr(error, "actual_type", None))
        return _(
            "Alibaba Cloud API {operation} parameter {parameter} expects {expected} but received {actual}."
        ).format(
            operation=operation,
            parameter=parameter,
            expected=expected,
            actual=actual,
        )
    if code.startswith("invalid_parameter_enum:"):
        parameter = _safe_parameter(code.partition(":")[2], _("the requested parameter"))
        allowed = _safe_allowed_values(getattr(error, "suggestions", ()))
        if allowed is not None:
            return _(
                "Alibaba Cloud API {operation} parameter {parameter} is not an allowed value. "
                "Allowed values: {allowed}."
            ).format(operation=operation, parameter=parameter, allowed=allowed)
        return _("Alibaba Cloud API {operation} parameter {parameter} is not an allowed value.").format(
            operation=operation,
            parameter=parameter,
        )
    if code == "unresolved_path_parameter":
        parameter = _safe_parameter_list(getattr(error, "parameter", ""))
        if parameter is not None and "," not in parameter:
            return _("Alibaba Cloud API {operation} is missing path parameter {parameter}.").format(
                operation=operation,
                parameter=parameter,
            )
        if parameter is not None:
            return _("Alibaba Cloud API {operation} is missing path parameters {parameters}.").format(
                operation=operation,
                parameters=parameter,
            )
        return _("Alibaba Cloud API path parameters are invalid or incomplete for {operation}.").format(
            operation=operation
        )
    if code == "invalid_path_parameter":
        parameter = _safe_parameter(getattr(error, "parameter", None), _("the requested parameter"))
        expected = _safe_type(getattr(error, "expected_type", None))
        actual = _safe_type(getattr(error, "actual_type", None))
        return _(
            "Alibaba Cloud API {operation} path parameter {parameter} expects {expected} but received {actual}."
        ).format(operation=operation, parameter=parameter, expected=expected, actual=actual)
    if code == "invalid_pathname":
        return _("Alibaba Cloud API path parameters are invalid or incomplete for {operation}.").format(
            operation=operation
        )
    if code == "invalid_header_value":
        parameter_value = getattr(error, "parameter", None)
        if isinstance(parameter_value, str) and _SAFE_PARAMETER.fullmatch(parameter_value):
            actual = _safe_type(getattr(error, "actual_type", None))
            return _(
                "Alibaba Cloud API {operation} parameter {parameter} expects a scalar header value without line "
                "breaks but received {actual}."
            ).format(operation=operation, parameter=parameter_value, actual=actual)
    if code == "invalid_expanded_header_name":
        parameter_value = getattr(error, "parameter", None)
        if isinstance(parameter_value, str) and _SAFE_PARAMETER.fullmatch(parameter_value):
            actual = _safe_type(getattr(error, "actual_type", None))
            return _(
                "Alibaba Cloud API {operation} parameter {parameter} expects a valid header name "
                "but received a {actual} with invalid syntax."
            ).format(operation=operation, parameter=parameter_value, actual=actual)
    if code == "invalid_host_label":
        parameter_value = getattr(error, "parameter", None)
        if isinstance(parameter_value, str) and _SAFE_PARAMETER.fullmatch(parameter_value):
            actual = _safe_type(getattr(error, "actual_type", None))
            return _(
                "Alibaba Cloud API {operation} parameter {parameter} expects a valid DNS host-label string "
                "but received {actual} with invalid syntax."
            ).format(operation=operation, parameter=parameter_value, actual=actual)
    if code == "reserved_header_forbidden":
        parameter_value = getattr(error, "parameter", None)
        if isinstance(parameter_value, str) and _SAFE_PARAMETER.fullmatch(parameter_value):
            return _(
                "Alibaba Cloud API {operation} parameter {parameter} targets a reserved authentication header. "
                "Remove the parameter and retry."
            ).format(operation=operation, parameter=parameter_value)
    if code in _PROTOCOL_UNSUPPORTED_REASONS:
        return _(
            "Alibaba Cloud API {operation} uses a protocol shape this runtime cannot execute. "
            "Choose another API version or action."
        ).format(operation=operation)
    if code in _SCHEMA_UNSUPPORTED_REASONS:
        return _(
            "Alibaba Cloud API {operation} metadata uses a schema this runtime cannot execute. "
            "Choose another API version or action."
        ).format(operation=operation)
    if _is_oss_unsupported_reason(code):
        return _(
            "Alibaba Cloud OSS API {operation} is not supported by this runtime. "
            "Choose a supported OSS action or use another client."
        ).format(operation=operation)
    if code == "contract_not_executable":
        return _(
            "Alibaba Cloud API {operation} cannot be executed from its current metadata. "
            "Choose another API version or action."
        ).format(operation=operation)
    if code == "location_cache_write_failed":
        return _(
            "Alibaba Cloud endpoint cache could not be updated for {operation} in {region}. "
            "Check local configuration storage and retry."
        ).format(operation=operation, region=safe_region)
    # ECS RAM Role credential failures. These must be registered before the generic
    # credential and fallback branches: `ecs_*` matches neither "credential" in code
    # nor code.startswith("auth_"), so each needs its own actionable message. Only an
    # allowed carrier may reach them, so a code-shaped message on some unrelated
    # exception keeps falling through to the generic text below.
    ecs_code = _ecs_credential_code(error)
    if ecs_code == "ecs_metadata_unreachable":
        return _(
            "Alibaba Cloud ECS instance metadata service is unreachable, so {operation} cannot be signed. "
            "Confirm this process runs on an ECS instance with a bound RAM role."
        ).format(operation=operation)
    if ecs_code == "ecs_metadata_disabled":
        return _(
            "Alibaba Cloud ECS instance metadata credentials are disabled, so {operation} cannot be signed. "
            "Check the ALIBABA_CLOUD_ECS_METADATA_DISABLED environment variable."
        ).format(operation=operation)
    if ecs_code == "ecs_imdsv2_required":
        return _(
            "Alibaba Cloud ECS metadata token (IMDSv2) could not be obtained while IMDSv1 is disabled, "
            "so {operation} cannot be signed. Check the instance metadata settings and network."
        ).format(operation=operation)
    if ecs_code == "ecs_ram_role_not_found":
        return _(
            "No matching Alibaba Cloud ECS instance RAM role was found, so {operation} cannot be signed. "
            "Check the instance RAM role and the configured ECS RAM role name."
        ).format(operation=operation)
    if ecs_code == "ecs_ram_role_response_invalid":
        return _(
            "Alibaba Cloud ECS instance metadata returned incomplete RAM role credentials, "
            "so {operation} cannot be signed. Retry and check the ECS metadata service."
        ).format(operation=operation)
    if ecs_code == "ecs_ram_role_refresh_failed":
        return _(
            "Alibaba Cloud ECS instance RAM role credentials could not be refreshed before they expired, "
            "so {operation} cannot be signed. Check ECS metadata availability."
        ).format(operation=operation)
    if "endpoint" in code:
        return _(
            "No trusted Alibaba Cloud endpoint is available for {operation} in {region}. "
            "Check the region or endpoint configuration."
        ).format(operation=operation, region=safe_region)
    if "host" in code:
        return _("Alibaba Cloud host parameters are invalid for {operation} in {region}.").format(
            operation=operation,
            region=safe_region,
        )
    if code.startswith("security_") or code in {"unsupported_auth_type", "auth_scheme_unsupported"}:
        return _(
            "Alibaba Cloud authentication is unsupported for {operation}. "
            "Check the API contract or choose an API that supports AccessKey authentication."
        ).format(operation=operation)
    if (
        code
        in {
            "aliyun_credential_provider_required",
            "aliyun_credentials_required",
            "authentication_required",
        }
        or "credential" in code
        or code.startswith("auth_")
    ):
        return _("Alibaba Cloud credentials are unavailable. Configure credentials and retry {operation}.").format(
            operation=operation
        )
    if code == "signature_parameter_forbidden":
        parameter = _safe_parameter(getattr(error, "parameter", None), _("the requested parameter"))
        return _(
            "Alibaba Cloud API {operation} parameter {parameter} is reserved for request signing and cannot be set."
        ).format(operation=operation, parameter=parameter)
    if "signature" in code or "signing" in code:
        return _("Alibaba Cloud signing is unsupported for {operation}. Check the API contract.").format(
            operation=operation
        )
    if code == "aliyun_delegated_executor_required":
        return _("Alibaba Cloud API execution is unavailable for {operation}. Retry from an active runtime.").format(
            operation=operation
        )
    if code == "aliyun_pipeline_write_forbidden":
        return _(
            "Alibaba Cloud API {operation} cannot modify cloud resources from a pipeline step. "
            "Use a pipeline-specific tool for this operation."
        ).format(operation=operation)
    if (
        code.startswith("snapshot_")
        or "handoff" in code
        or "invocation_binding" in code
        or code
        in {
            "aliyun_delegated_outer_binding_required",
            "aliyun_invocation_binding_required",
            "aliyun_public_binding_required",
        }
    ):
        return _(
            "Alibaba Cloud API authorization expired or changed. Run {operation} again to approve the current contract."
        ).format(operation=operation)
    if code == "body_file_not_supported":
        return _(
            "Alibaba Cloud API {operation} does not accept body_file. Use the body source declared by the API contract."
        ).format(operation=operation)
    if code == "body_file_too_large":
        return _(
            "Alibaba Cloud API {operation} body_file exceeds the 32 MiB limit. Use a smaller regular file."
        ).format(operation=operation)
    if code == "body_source_mismatch":
        return _("Alibaba Cloud API {operation} requires a body source compatible with its API contract.").format(
            operation=operation
        )
    if code == "conflicting_body_sources":
        return _(
            "Alibaba Cloud API {operation} received multiple body sources. "
            "Provide only one of body, body_file, or body parameters."
        ).format(operation=operation)
    if code == "conflicting_template_sources":
        return _("Alibaba Cloud API {operation} received multiple template sources. Provide only one.").format(
            operation=operation
        )
    if code == "content_type_mismatch":
        return _(
            "Alibaba Cloud API {operation} content_type does not match the request body. Use a compatible media type."
        ).format(operation=operation)
    if code == "content_type_without_body":
        return _(
            "Alibaba Cloud API {operation} has content_type without a request body. "
            "Remove content_type or provide a body."
        ).format(operation=operation)
    if code in {"invalid_body", "invalid_json_body"}:
        return _("Alibaba Cloud API {operation} body is invalid. Provide a valid JSON or form request body.").format(
            operation=operation
        )
    if code == "invalid_body_file":
        return _(
            "Alibaba Cloud API {operation} body_file is invalid. Use a readable regular file within the allowed size."
        ).format(operation=operation)
    if code == "invalid_content_type":
        return _(
            "Alibaba Cloud API {operation} content_type is invalid. Use a valid media type such as application/json."
        ).format(operation=operation)
    if code == "invalid_template_file":
        return _(
            "Alibaba Cloud API {operation} template file is invalid. Use a readable regular template file."
        ).format(operation=operation)
    if code == "hook_validation_failed":
        return _(
            "Alibaba Cloud API {operation} template validation failed. "
            "Check the template syntax and resource definitions."
        ).format(operation=operation)
    if code == "invalid_max_response_bytes":
        return _("Alibaba Cloud API {operation} max_response_bytes must be between 1 and 16777216.").format(
            operation=operation
        )
    target_http = _target_http_context(code)
    if target_http is not None:
        status, target_code = target_http
        if target_code is not None:
            return _(
                "Alibaba Cloud API {operation} returned HTTP {status} with error code {code}. "
                "Check the request and cloud permissions before retrying."
            ).format(operation=operation, status=status, code=target_code)
        return _(
            "Alibaba Cloud API {operation} returned HTTP {status}. "
            "Check the request and cloud permissions before retrying."
        ).format(operation=operation, status=status)
    if code in {"pre_connect_failure", "pool_unavailable", "connect_timeout", "connect_error"}:
        return _(
            "Alibaba Cloud API {operation} could not connect before the request was sent. "
            "Check network and endpoint access, then retry."
        ).format(operation=operation)
    if code == "retryable_status":
        return _(
            "Alibaba Cloud API {operation} received a retryable service response, but the retry deadline expired. "
            "Retry the read-only request later."
        ).format(operation=operation)
    if code in {"read_timeout", "read_error", "protocol_error", "stream_read_error"}:
        return _(
            "Alibaba Cloud API {operation} read-only request did not complete before the retry deadline. "
            "Check network and service health, then retry."
        ).format(operation=operation)
    if code == "unknown_after_cancel":
        return _(
            "Alibaba Cloud API {operation} timed out after the request may have been sent. "
            "Check cloud state before retrying to avoid duplicate changes."
        ).format(operation=operation)
    if code == "pretarget_timeout":
        return _("Alibaba Cloud API {operation} timed out before the request was sent. Retry the operation.").format(
            operation=operation
        )
    if code == "invalid_response":
        return _(
            "Alibaba Cloud API {operation} returned an invalid response after the request was sent. "
            "Check cloud state before retrying to avoid duplicate changes."
        ).format(operation=operation)
    if code in {
        "unknown_after_transport_error",
        "target_transport_failure",
    }:
        return _(
            "Alibaba Cloud API {operation} may have been sent before the connection failed. "
            "Check cloud state before retrying to avoid duplicate changes."
        ).format(operation=operation)
    if code == "response_too_large":
        return _(
            "Alibaba Cloud API {operation} response exceeded max_response_bytes. "
            "Increase the limit or narrow the request and retry."
        ).format(operation=operation)
    if code == "error_response_too_large":
        return _(
            "Alibaba Cloud API {operation} error response exceeded the 1 MiB safety limit. "
            "Check Alibaba Cloud logs before retrying."
        ).format(operation=operation)
    if code.startswith("invalid_parameter_") or code in {"invalid_params", "invalid_unknown_query_container"}:
        return _("Alibaba Cloud API parameters are invalid for {operation}.").format(operation=operation)
    return _("Alibaba Cloud API {operation} could not be prepared safely. Check the request and try again.").format(
        operation=operation
    )


def public_aliyun_unsupported_reasons(
    reasons: tuple[str, ...],
    *,
    product: Any,
    action: Any,
) -> list[str]:
    """Render canonical non-executable reasons without exposing internal codes."""

    messages = [public_aliyun_error(reason, product=product, action=action) for reason in reasons]
    return list(dict.fromkeys(messages))


def _ecs_credential_code(error: BaseException | str) -> str | None:
    """Return the ECS credential code only when `error` is an allowed carrier.

    An explicit stable code string, and the exception carriers the credential runtime
    actually uses (`ValueError`/`ApiContractError` whose message is exactly a stable code,
    or the one Darabonba envelope directly around such a `ValueError`), may reach the
    `ecs_*` branches. A `RuntimeError` or a bare `Exception` with the same message must
    not, even though `str(error)` matches.
    """

    if isinstance(error, str):
        return error
    # Imported here so this module stays free of the credential runtime at import time.
    from iac_code.tools.cloud.aliyun.ecs_credential_errors import ecs_credential_error_code

    return ecs_credential_error_code(error)


def _is_oss_unsupported_reason(code: str) -> bool:
    prefix = code.partition(":")[0]
    return code.startswith("oss_") or prefix in _OSS_UNSUPPORTED_REASONS | {
        "field_mapping_missing",
        "request_body_type_unsupported",
    }


def _target_http_context(code: str) -> tuple[str, str | None] | None:
    match = _TARGET_HTTP_ERROR.fullmatch(code)
    if match is None:
        return None
    status = match.group(1)
    if not 100 <= int(status) <= 599:
        return None
    return status, match.group(2)


def _safe_identifier(value: Any, fallback: str) -> str:
    return value if isinstance(value, str) and _SAFE_IDENTIFIER.fullmatch(value) else fallback


def _safe_parameter(value: Any, fallback: str) -> str:
    return value if isinstance(value, str) and _SAFE_PARAMETER.fullmatch(value) else fallback


def _safe_parameter_list(value: Any) -> str | None:
    if not isinstance(value, str) or len(value) > _MAX_PUBLIC_PARAMETER_LIST_CHARS:
        return None
    names = value.split(",")
    if (
        not names
        or len(names) > _MAX_PUBLIC_PARAMETER_NAMES
        or any(_SAFE_PARAMETER.fullmatch(name) is None for name in names)
    ):
        return None
    return ",".join(names)


def _safe_allowed_values(values: Any) -> str | None:
    """Render a closed value set that came from the local contract, never from tool input."""

    if not isinstance(values, tuple) or not values or len(values) > _MAX_PUBLIC_ALLOWED_VALUES:
        return None
    if any(not isinstance(value, str) or _SAFE_ALLOWED_VALUE.fullmatch(value) is None for value in values):
        return None
    rendered = ", ".join(values)
    return rendered if len(rendered) <= _MAX_PUBLIC_ALLOWED_VALUE_CHARS else None


def _safe_type(value: Any) -> str:
    safe_types = {"string", "integer", "number", "boolean", "array", "object", "null", "scalar"}
    return value if value in safe_types else "unknown"
