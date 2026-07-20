"""Policy for classifying live Aliyun API validation results."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

API_NOT_SUITABLE_STATUS = "验证评估不适合本次测试"

LiveValidationStatus = Literal[
    "未测试",
    "通过（HTTP 200）",
    "通过（服务端有效错误）",
    "验证评估不适合本次测试",
    "失败",
]
LiveValidationCategory = Literal[
    "http_200",
    "service_reached",
    "retry_later",
    "retry_or_switch_api",
    "fix_input",
    "metadata_or_endpoint",
    "unknown_non_success",
]
SemanticValidationConfidence = Literal["high", "medium", "low"]
SemanticValidationCategory = str

_HARD_INPUT_PREFIXES = (
    "missing",
    "invalidformat",
    "malformed",
)
_HARD_INPUT_MARKERS = (
    "invalidheader",
    "missingparameter",
    "missparam",
    "missingrequiredparameter",
    "requiredparametermissing",
    "parametermissing",
    "errormissing",
    "malformed",
    "postbodyinvalid",
)
_HARD_INPUT_VALUE_PREFIXES = (
    "illegalargument",
    "illegalparam",
    "illegalparameter",
    "illegalvalue",
    "invalidargumentformat",
    "invalidargumenttype",
    "invalidargumentvalue",
    "invalidparamvalue",
    "invalidparameterformat",
    "invalidparametermalformed",
    "invalidparametersyntax",
    "invalidparametertype",
    "invalidparametervalue",
    "invalidtype",
    "invalidvalue",
)
_HARD_INPUT_VALUE_MARKERS = (
    "invalidarraylength",
    "invalidlength",
    "invalidlistlength",
    "invalidrange",
    "invalidsize",
    "illegalparamvalue",
    "illegalvalue",
    "parameterempty",
    "sizelimit",
)
_SEMANTIC_JUDGMENT_MARKERS = (
    "accessdenied",
    "disabled",
    "doesnotexist",
    "forbidden",
    "nosuch",
    "notauthorized",
    "notexist",
    "notfound",
    "notopened",
    "notpurchase",
    "organization",
    "permission",
    "precondition",
    "resourcenot",
    "tenant",
    "unsubscript",
    "workspace",
)
_RATE_LIMIT_CODES = {
    "requestlimitexceeded",
    "toomanyrequests",
}
_RATE_LIMIT_PREFIXES = (
    "throttling",
    "requestlimit",
)
_SERVER_ERROR_CODES = {
    "internalerror",
    "internalservererror",
    "remoteservererror",
    "servererror",
    "serviceunavailable",
    "systemerror",
}
_SERVER_ERROR_PREFIXES = (
    "internalerror",
    "internalservererror",
    "remoteservererror",
    "servererror",
    "serviceunavailable",
    "systemerror",
)
_PRODUCT_ACTIVATION_ERROR_MARKERS = (
    "disabled",
    "notactivate",
    "notactivated",
    "notopened",
    "notpurchase",
    "unsubscript",
)


@dataclass(frozen=True)
class LiveValidationDecision:
    counts_as_product_version_success: bool
    status: LiveValidationStatus
    category: LiveValidationCategory
    reason: str
    error_code: str | None = None


@dataclass(frozen=True)
class SemanticValidationJudgment:
    counts_as_product_version_success: bool
    status: LiveValidationStatus
    category: SemanticValidationCategory
    confidence: SemanticValidationConfidence
    reason: str
    source: str = "llm"


def classify_live_validation_response(
    *,
    status: int,
    body: Any | None = None,
    semantic_judgment: SemanticValidationJudgment | None = None,
) -> LiveValidationDecision:
    error_code = _extract_error_code(body)
    normalized_code = _normalize_error_code(error_code)
    normalized_text = _normalize_error_code(_extract_error_text(body))
    if status == 200:
        return LiveValidationDecision(True, "通过（HTTP 200）", "http_200", "HTTP 200", error_code)
    if 501 <= status <= 599:
        return LiveValidationDecision(
            False,
            API_NOT_SUITABLE_STATUS,
            "retry_or_switch_api",
            "server error; retry first, then switch to another safe API",
            error_code,
        )
    if _server_error(normalized_code) or _server_error(normalized_text):
        return LiveValidationDecision(
            False,
            API_NOT_SUITABLE_STATUS,
            "retry_or_switch_api",
            "official service or gateway error; retry first, then switch to another safe API",
            error_code,
        )
    if status == 429:
        return LiveValidationDecision(
            False,
            API_NOT_SUITABLE_STATUS,
            "retry_later",
            "rate limited; retry with backoff",
            error_code,
        )
    if _rate_limit_error(normalized_code) or _rate_limit_error(normalized_text):
        return LiveValidationDecision(
            False,
            API_NOT_SUITABLE_STATUS,
            "retry_later",
            "rate limited; retry with backoff",
            error_code,
        )
    if status == 405 and (not normalized_code or _http_method_contract_error(normalized_code, normalized_text)):
        return LiveValidationDecision(
            False,
            "失败",
            "metadata_or_endpoint",
            "HTTP method contract did not match the target service",
            error_code,
        )
    if status == 415:
        return LiveValidationDecision(
            False,
            "失败",
            "metadata_or_endpoint",
            "request media type contract did not match the target service",
            error_code,
        )
    metadata_code_error = _metadata_or_endpoint_error(normalized_code, normalized_text=normalized_text)
    metadata_message_error = _metadata_or_endpoint_message_error(normalized_text, normalized_code=normalized_code)
    if metadata_code_error or metadata_message_error:
        return LiveValidationDecision(
            False,
            "失败",
            "metadata_or_endpoint",
            "API route, version, endpoint, HTTP method, or signing metadata did not reach the target service",
            error_code,
        )
    if _input_error(normalized_code) or _input_message_error(
        normalized_text,
        semantic_code=_semantic_judgment_error(normalized_code),
    ):
        return LiveValidationDecision(
            False,
            API_NOT_SUITABLE_STATUS,
            "fix_input",
            "request input should be corrected",
            error_code,
        )
    if not (400 <= status <= 499 or status == 500):
        return LiveValidationDecision(
            False,
            API_NOT_SUITABLE_STATUS,
            "unknown_non_success",
            "non-200 non-error response does not count as HTTP 200 or service-reached error evidence",
            error_code,
        )
    if _semantic_judgment_accepts_service_reached(
        semantic_judgment,
        normalized_code=normalized_code,
        normalized_text=normalized_text,
    ):
        assert semantic_judgment is not None
        return LiveValidationDecision(
            True,
            "通过（服务端有效错误）",
            "service_reached",
            semantic_judgment.reason,
            error_code,
        )
    return LiveValidationDecision(
        False,
        API_NOT_SUITABLE_STATUS,
        "unknown_non_success",
        "non-200 response requires structured semantic judgment unless it is a hard failure",
        error_code,
    )


def _extract_error_code(body: Any | None) -> str | None:
    if not isinstance(body, Mapping):
        return None
    for key in ("Code", "code", "errorCode", "ErrorCode", "error"):
        value = body.get(key)
        if isinstance(value, str) and value:
            return value
    error = body.get("Error") or body.get("error")
    if isinstance(error, Mapping):
        for key in ("Code", "code", "errorCode", "ErrorCode"):
            value = error.get(key)
            if isinstance(value, str) and value:
                return value
    return None


def _extract_error_text(body: Any | None) -> str | None:
    if not isinstance(body, Mapping):
        return None
    texts: list[str] = []
    for key in ("Message", "message", "Description", "description", "error_description"):
        value = body.get(key)
        if isinstance(value, str) and value.strip():
            texts.append(value.strip())
    error = body.get("Error") or body.get("error")
    if isinstance(error, Mapping):
        for key in ("Message", "message", "Description", "description"):
            value = error.get(key)
            if isinstance(value, str) and value.strip():
                texts.append(value.strip())
    return " ".join(texts) if texts else None


def _normalize_error_code(error_code: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", "", error_code.casefold()) if error_code else ""


def _semantic_judgment_accepts_service_reached(
    judgment: SemanticValidationJudgment | None,
    *,
    normalized_code: str = "",
    normalized_text: str = "",
) -> bool:
    del normalized_code, normalized_text
    if judgment is None:
        return False
    return (
        judgment.counts_as_product_version_success
        and judgment.status == "通过（服务端有效错误）"
        and judgment.category.startswith("service_reached_")
        and judgment.confidence == "high"
        and bool(judgment.reason.strip())
    )


def _metadata_or_endpoint_error(normalized_code: str, *, normalized_text: str = "") -> bool:
    if not normalized_code:
        return False
    if normalized_code == "invalidproduct" and _product_activation_context(normalized_text):
        return False
    if normalized_code in {
        "invalidapi",
        "invalidapinotfound",
        "invalidapinotexist",
        "apinotfound",
        "invalidaction",
        "invalidproduct",
        "invalidproductnotfound",
        "invalidproductnotexist",
        "invalidversion",
        "incompletesignature",
        "invalidtimestamp",
        "invalidcredential",
        "invalidcredentials",
        "invalidregionid",
        "invalidregionnotfound",
        "nosuchregion",
        "notsupportedregion",
        "regionidnotsupported",
        "regionnotsupported",
        "requesttimetooskewed",
        "unsupportedregion",
        "wrongendpoint",
        "invalidoperationnotsupportedendpoint",
        "notsupportedendpoint",
        "missingaccesskeyid",
        "missingauthenticationtoken",
        "missingcredential",
        "missingsecuritytoken",
        "missingsignature",
        "methodnotallowed",
        "signaturenotvalid",
        "signaturedoesnotmatch",
        "signatureinvalid",
        "signnotvalid",
        "httpmethodnotallowed",
        "unsupportedmethod",
        "unsupportedhttpmethod",
        "unsupportedmediatype",
        "invalidaccesskeyidnotfound",
        "invalidsecuritytokenexpired",
        "securitytokenexpired",
        "expiredsecuritytoken",
    }:
        return True
    route_prefixes = (
        "invalidaction",
        "invalidversion",
        "invalidendpoint",
        "invalidregion",
        "invalidsignature",
        "invalidaccesskey",
        "invalidcredential",
        "invalidsecuritytoken",
        "expiredsecuritytoken",
        "missingaccesskey",
        "missingcredential",
        "missingsignature",
        "signature",
    )
    return normalized_code.startswith(route_prefixes)


def _metadata_or_endpoint_message_error(normalized_text: str, *, normalized_code: str = "") -> bool:
    if not normalized_text:
        return False
    if normalized_text == "invalidproduct" and _product_activation_context(normalized_code):
        return False
    if normalized_text == "invalidproduct":
        return True
    markers = (
        "invalidapinotfound",
        "invalidapinotexist",
        "apinotfound",
        "invalidaction",
        "invalidproductnotfound",
        "invalidproductnotexist",
        "invalidversion",
        "invalidregionnotfound",
        "nosuchregion",
        "notsupportedregion",
        "regionidnotsupported",
        "regionnotsupported",
        "wrongendpoint",
        "unsupportedregion",
        "invalidendpoint",
        "notsupportedendpoint",
        "incompletesignature",
        "invalidtimestamp",
        "requesttimetooskewed",
        "missingaccesskeyid",
        "missingauthenticationtoken",
        "missingcredential",
        "missingsecuritytoken",
        "missingsignature",
        "methodnotallowed",
        "signaturenotvalid",
        "signaturedoesnotmatch",
        "signatureinvalid",
        "signnotvalid",
        "httpmethodnotallowed",
        "unsupportedmethod",
        "unsupportedhttpmethod",
        "unsupportedmediatype",
        "invalidaccesskey",
        "invalidsecuritytoken",
        "securitytokenexpired",
        "expiredsecuritytoken",
    )
    return any(marker in normalized_text for marker in markers)


def _http_method_contract_error(normalized_code: str, normalized_text: str) -> bool:
    method_markers = (
        "methodnotallowed",
        "httpmethodnotallowed",
        "unsupportedmethod",
        "unsupportedhttpmethod",
    )
    return any(marker in normalized_code or marker in normalized_text for marker in method_markers)


def _product_activation_context(*normalized_values: str) -> bool:
    return any(marker in value for value in normalized_values for marker in _PRODUCT_ACTIVATION_ERROR_MARKERS)


def _rate_limit_error(normalized_code: str) -> bool:
    if not normalized_code:
        return False
    return normalized_code in _RATE_LIMIT_CODES or normalized_code.startswith(_RATE_LIMIT_PREFIXES)


def _server_error(normalized_code: str) -> bool:
    if not normalized_code:
        return False
    return normalized_code in _SERVER_ERROR_CODES or normalized_code.startswith(_SERVER_ERROR_PREFIXES)


def _input_error(normalized_code: str) -> bool:
    if not normalized_code:
        return False
    if (
        normalized_code.startswith(_HARD_INPUT_PREFIXES)
        or any(marker in normalized_code for marker in _HARD_INPUT_MARKERS)
        or normalized_code.startswith(_HARD_INPUT_VALUE_PREFIXES)
        or any(marker in normalized_code for marker in _HARD_INPUT_VALUE_MARKERS)
    ):
        return True
    if any(marker in normalized_code for marker in _SEMANTIC_JUDGMENT_MARKERS):
        return False
    return False


def _semantic_judgment_error(normalized_code: str) -> bool:
    return bool(normalized_code) and any(marker in normalized_code for marker in _SEMANTIC_JUDGMENT_MARKERS)


def _input_message_error(normalized_text: str, *, semantic_code: bool = False) -> bool:
    if not normalized_text:
        return False
    markers = (
        "mandatory",
        "missingparameter",
        "parametermissing",
        "requiredparameter",
        "parameterrequired",
        "ismissing",
        "notprovided",
        "mustprovide",
        "shouldnotbeempty",
        "cannotbeempty",
        "malformed",
        "invalidformat",
        "formatinvalid",
        "invalidtype",
        "typeinvalid",
        "invalidvalue",
        "valueinvalid",
        "illegalparam",
        "illegalparameter",
        "illegalvalue",
    )
    if any(marker in normalized_text for marker in markers):
        return True
    if "parameter" not in normalized_text:
        return False
    if semantic_code and _semantic_message_error(normalized_text):
        return False
    parameter_markers = ("required", "missing", "mandatory", "format", "type", "value", "invalid", "illegal")
    return any(marker in normalized_text for marker in parameter_markers)


def _semantic_message_error(normalized_text: str) -> bool:
    markers = (
        "doesnotexist",
        "nosuch",
        "notexist",
        "notfound",
        "notopened",
        "notpurchase",
        "organization",
        "permission",
        "precondition",
        "resourcenot",
        "tenant",
        "unsubscript",
        "workspace",
    )
    return any(marker in normalized_text for marker in markers)
