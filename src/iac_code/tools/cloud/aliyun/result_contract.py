"""Internal contract for OpenMeta Alibaba Cloud tool results."""

from __future__ import annotations

import base64
import json
from collections.abc import Mapping
from typing import Any

from iac_code.i18n import _
from iac_code.tools.cloud.aliyun.acs3_transport import NormalizedApiResponse
from iac_code.tools.cloud.aliyun.api_contract import ApiContractError, BuiltApiRequest, CanonicalWireContract

ALIYUN_HTTP_METADATA_KEY = "aliyun_http"
ALIYUN_BODY_CONTRACT_VERSION = "aliyun_body_v1"
ALIYUN_MIGRATED_RESULT_TOOLS = frozenset(
    {
        "aliyun_api",
        "ros_validate_template",
        "ros_get_template_parameter_constraints",
        "ros_preview_template",
        "ros_estimate_template_cost",
        "ros_stack_group",
        "ros_template",
        "ros_template_scratch",
        "ros_diagnostic",
        "ros_resource_type_registration",
        "ros_tag",
    }
)

_BODY_FORMATS = frozenset({"json", "text", "xml", "binary_base64_json", "headers_only_json", "empty"})
_RESPONSE_MODES = frozenset({"json", "text", "xml", "binary", "headers_only"})
_CONTENT_STATES = frozenset({"inline_final", "externalized_preview"})
_HEADER_COUNT_LIMIT = 64
_HEADER_VALUE_BYTES_LIMIT = 8192
_HEADERS_JSON_BYTES_LIMIT = 32768


def sanitize_aliyun_http_metadata(value: Any, *, include_content_state: bool = True) -> dict[str, Any] | None:
    """Return only the reviewed internal Alibaba Cloud result metadata."""
    if not isinstance(value, Mapping) or value.get("contract_version") != ALIYUN_BODY_CONTRACT_VERSION:
        return None

    output: dict[str, Any] = {"contract_version": ALIYUN_BODY_CONTRACT_VERSION}
    for key in ("product", "version", "action"):
        item = value.get(key)
        if isinstance(item, str):
            output[key] = item

    status = value.get("status")
    if isinstance(status, int) and not isinstance(status, bool) and 100 <= status <= 599:
        output["status"] = status
        expected_status_class = "{}xx".format(status // 100)
        output["status_class"] = expected_status_class

    response_mode = value.get("response_mode")
    if response_mode in _RESPONSE_MODES:
        output["response_mode"] = response_mode
    body_format = value.get("body_format")
    if body_format in _BODY_FORMATS:
        output["body_format"] = body_format

    for key in (
        "headers_present",
        "body_present",
        "content_type_present",
        "size_present",
        "content_encoding_present",
        "headers_nonempty",
    ):
        item = value.get(key)
        if isinstance(item, bool):
            output[key] = item

    header_count = value.get("header_count")
    if isinstance(header_count, int) and not isinstance(header_count, bool) and header_count >= 0:
        output["header_count"] = header_count

    if include_content_state and value.get("content_state") in _CONTENT_STATES:
        output["content_state"] = value["content_state"]
    return output


def aliyun_http_from_metadata(metadata: Any) -> dict[str, Any] | None:
    if not isinstance(metadata, Mapping):
        return None
    return sanitize_aliyun_http_metadata(metadata.get(ALIYUN_HTTP_METADATA_KEY))


def with_aliyun_content_state(metadata: Any, *, externalized: bool) -> dict[str, Any] | None:
    """Copy metadata and add the post-ResultStorage content state when marked."""
    if not isinstance(metadata, Mapping):
        return dict(metadata) if isinstance(metadata, dict) else None
    aliyun_http = sanitize_aliyun_http_metadata(metadata.get(ALIYUN_HTTP_METADATA_KEY), include_content_state=False)
    if aliyun_http is None:
        return dict(metadata)
    aliyun_http["content_state"] = "externalized_preview" if externalized else "inline_final"
    result = dict(metadata)
    result[ALIYUN_HTTP_METADATA_KEY] = aliyun_http
    return result


def serialize_business_result(
    response: NormalizedApiResponse,
    request: BuiltApiRequest,
    contract: CanonicalWireContract,
) -> tuple[str, str]:
    """Project a normalized transport response into the reviewed business result."""
    mode = request.response_policy.mode
    if request.method == "HEAD" or mode == "headers_only":
        return _serialize_headers_only(response.headers), "headers_only_json"
    if response.status == 204:
        return "null", "empty"

    body = response.body
    if contract.transport == "oss_v4_sdk" and mode in {"text", "xml"} and isinstance(body, Mapping):
        return json.dumps(dict(body), ensure_ascii=False, indent=2), "json"
    if mode == "json":
        return json.dumps(body, ensure_ascii=False, indent=2), "json"
    if mode in {"text", "xml"}:
        if not isinstance(body, str):
            raise ApiContractError("aliyun_response_body_invalid")
        return body, mode
    if mode == "binary":
        return _serialize_binary(body), "binary_base64_json"
    raise ApiContractError("aliyun_response_body_invalid")


def build_aliyun_http_metadata(
    response: NormalizedApiResponse,
    request: BuiltApiRequest,
    contract: CanonicalWireContract,
    *,
    body_format: str,
) -> dict[str, Any]:
    headers_object = dict(response.headers)
    value = {
        "contract_version": ALIYUN_BODY_CONTRACT_VERSION,
        "product": contract.product,
        "version": contract.version,
        "action": contract.action,
        "status": response.status,
        "status_class": "{}xx".format(response.status // 100),
        "response_mode": request.response_policy.mode,
        "body_format": body_format,
        "headers_present": True,
        "body_present": True,
        "content_type_present": True,
        "size_present": True,
        "content_encoding_present": response.content_encoding is not None,
        "headers_nonempty": bool(headers_object),
        "header_count": len(headers_object),
    }
    sanitized = sanitize_aliyun_http_metadata(value, include_content_state=False)
    if sanitized is None:  # pragma: no cover - values above are compile-time constrained
        raise AssertionError("invalid aliyun_http metadata")
    return sanitized


def render_aliyun_result(
    tool_input: Mapping[str, Any],
    content: str,
    *,
    is_error: bool,
    aliyun_http: Mapping[str, Any],
    verbose: bool,
) -> str | None:
    """Render one marked result without consulting mutable tool instance state."""
    if is_error:
        return None
    metadata = sanitize_aliyun_http_metadata(aliyun_http)
    if metadata is None:
        return None
    if verbose:
        return content.strip()
    if metadata.get("content_state") == "externalized_preview":
        lines = content.strip().splitlines()
        return _("Received response ({count} lines)").format(count=len(lines))

    parsed = _parse_business_content(content, metadata.get("body_format"))
    if not parsed[0]:
        return _("Received response ({count} lines)").format(count=len(content.strip().splitlines()))
    if parsed[0] and isinstance(parsed[1], Mapping):
        request_id = parsed[1].get("RequestId")
        if request_id:
            return _("Call succeeded (RequestId: {request_id})").format(request_id=request_id)
    return _("Call succeeded")


def _parse_business_content(content: str, body_format: Any) -> tuple[bool, Any]:
    if body_format not in {"json", "binary_base64_json", "headers_only_json", "empty"}:
        return True, content
    try:
        return True, json.loads(content)
    except (TypeError, ValueError):
        return False, None


def _serialize_headers_only(headers: Mapping[str, str]) -> str:
    headers_object = dict(headers)
    if len(headers_object) > _HEADER_COUNT_LIMIT:
        raise ApiContractError("aliyun_response_headers_too_large")
    for value in headers_object.values():
        if len(str(value).encode("utf-8")) > _HEADER_VALUE_BYTES_LIMIT:
            raise ApiContractError("aliyun_response_headers_too_large")
    serialized = json.dumps(headers_object, ensure_ascii=False, indent=2, sort_keys=True)
    if len(serialized.encode("utf-8")) > _HEADERS_JSON_BYTES_LIMIT:
        raise ApiContractError("aliyun_response_headers_too_large")
    return serialized


def _serialize_binary(body: Any) -> str:
    if isinstance(body, bytes | bytearray):
        payload = {"encoding": "base64", "data": base64.b64encode(bytes(body)).decode("ascii")}
    elif isinstance(body, Mapping) and body.get("encoding") == "base64" and isinstance(body.get("data"), str):
        try:
            base64.b64decode(body["data"], validate=True)
        except (ValueError, TypeError) as exc:
            raise ApiContractError("aliyun_response_body_invalid") from exc
        payload = {"encoding": "base64", "data": body["data"]}
    else:
        raise ApiContractError("aliyun_response_body_invalid")
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
