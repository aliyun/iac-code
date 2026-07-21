"""Tests for live Aliyun API validation result classification."""

from __future__ import annotations

from iac_code.tools.cloud.aliyun.validation_policy import SemanticValidationJudgment, classify_live_validation_response


def test_live_validation_accepts_http_200_as_success() -> None:
    decision = classify_live_validation_response(status=200, body={"RequestId": "req-1"})

    assert decision.counts_as_product_version_success is True
    assert decision.status == "通过（HTTP 200）"
    assert decision.category == "http_200"


def test_live_validation_understands_oauth_style_business_error_fields() -> None:
    body = {
        "error": "instance_not_found",
        "error_description": "Instance not found by request.",
    }
    judgment = SemanticValidationJudgment(
        counts_as_product_version_success=True,
        status="通过（服务端有效错误）",
        category="service_reached_resource_state",
        confidence="high",
        reason="目标服务识别了 EIAM 实例路径并返回实例不存在业务语义。",
    )

    undecided = classify_live_validation_response(status=404, body=body)
    accepted = classify_live_validation_response(status=404, body=body, semantic_judgment=judgment)

    assert undecided.error_code == "instance_not_found"
    assert undecided.category == "unknown_non_success"
    assert accepted.counts_as_product_version_success is True
    assert accepted.status == "通过（服务端有效错误）"
    assert accepted.error_code == "instance_not_found"


def test_live_validation_requires_semantic_judgment_for_resource_not_found() -> None:
    undecided = classify_live_validation_response(
        status=400,
        body={"Code": "InstanceId.NotFound", "Message": "The specified InstanceId does not exist."},
    )

    assert undecided.counts_as_product_version_success is False
    assert undecided.status == "验证评估不适合本次测试"
    assert undecided.category == "unknown_non_success"

    decision = classify_live_validation_response(
        status=400,
        body={"Code": "InstanceId.NotFound", "Message": "The specified InstanceId does not exist."},
        semantic_judgment=SemanticValidationJudgment(
            counts_as_product_version_success=True,
            status="通过（服务端有效错误）",
            category="service_reached_resource_scope",
            confidence="high",
            reason="目标服务识别 InstanceId 并返回资源不存在语义。",
        ),
    )

    assert decision.counts_as_product_version_success is True
    assert decision.status == "通过（服务端有效错误）"
    assert decision.category == "service_reached"


def test_live_validation_requires_semantic_judgment_for_workspace_and_organization_not_found() -> None:
    judgment = SemanticValidationJudgment(
        counts_as_product_version_success=True,
        status="通过（服务端有效错误）",
        category="service_reached_business_precondition",
        confidence="high",
        reason="LLM 判断该错误来自目标服务业务前置校验。",
    )

    for code in ("InvalidOrganization.NotFound", "Workspace.NotFound", "WorkspaceNotExist"):
        undecided = classify_live_validation_response(status=400, body={"Code": code})
        accepted = classify_live_validation_response(status=400, body={"Code": code}, semantic_judgment=judgment)

        assert undecided.counts_as_product_version_success is False
        assert undecided.category == "unknown_non_success"
        assert accepted.counts_as_product_version_success is True
        assert accepted.status == "通过（服务端有效错误）"


def test_live_validation_defers_invalid_parameter_resource_semantics_to_judgment() -> None:
    judgment = SemanticValidationJudgment(
        counts_as_product_version_success=True,
        status="通过（服务端有效错误）",
        category="service_reached_resource_scope",
        confidence="high",
        reason="错误码带有 InvalidParameter 前缀，但实际表达目标服务资源不存在。",
    )

    for code in (
        "InvalidParameter.ResourceNotFound",
        "InvalidParameter.InstanceNotFound",
        "InvalidArgument.ResourceNotExist",
    ):
        undecided = classify_live_validation_response(status=400, body={"Code": code})
        accepted = classify_live_validation_response(status=400, body={"Code": code}, semantic_judgment=judgment)

        assert undecided.counts_as_product_version_success is False
        assert undecided.category == "unknown_non_success"
        assert accepted.counts_as_product_version_success is True
        assert accepted.status == "通过（服务端有效错误）"


def test_live_validation_does_not_let_generic_input_text_override_semantic_error_code() -> None:
    judgment = SemanticValidationJudgment(
        counts_as_product_version_success=True,
        status="通过（服务端有效错误）",
        category="service_reached_resource_scope",
        confidence="high",
        reason="错误码明确表达目标服务资源域不存在，泛化参数文案不能覆盖语义裁决。",
    )

    undecided = classify_live_validation_response(
        status=400,
        body={
            "Code": "InvalidParameter.ResourceNotFound",
            "Message": "Invalid parameter ResourceId was not found in the target service.",
        },
    )
    accepted = classify_live_validation_response(
        status=400,
        body={
            "Code": "InvalidParameter.ResourceNotFound",
            "Message": "Invalid parameter ResourceId was not found in the target service.",
        },
        semantic_judgment=judgment,
    )

    assert undecided.counts_as_product_version_success is False
    assert undecided.category == "unknown_non_success"
    assert accepted.counts_as_product_version_success is True
    assert accepted.status == "通过（服务端有效错误）"


def test_live_validation_semantic_error_code_still_rejects_hard_input_message() -> None:
    judgment = SemanticValidationJudgment(
        counts_as_product_version_success=True,
        status="通过（服务端有效错误）",
        category="service_reached_resource_scope",
        confidence="high",
        reason="裁决声称错误码已进入目标服务资源域。",
    )

    for message in (
        "The input parameter InstanceId is mandatory.",
        "The required parameter InstanceId is missing.",
        "Invalid parameter type for InstanceId.",
        "Invalid parameter value for InstanceId.",
        "Malformed parameter InstanceId.",
    ):
        decision = classify_live_validation_response(
            status=400,
            body={"Code": "InvalidParameter.ResourceNotFound", "Message": message},
            semantic_judgment=judgment,
        )

        assert decision.counts_as_product_version_success is False
        assert decision.category == "fix_input"


def test_live_validation_rejects_hard_input_shape_errors_as_input_to_fix() -> None:
    for code in (
        "MissingParameter",
        "MissingworkspaceId",
        "MissingTenantId",
        "MissingOrganizationId",
        "CCAI.ParamNotfound.MissParam",
        "InvalidFormat",
        "InvalidParameter.Type",
        "InvalidParameter.Value",
        "InvalidType",
        "InvalidValue",
        "InvalidParamValue",
        "InvalidArgument.Format",
        "InvalidArgument.Type",
        "InvalidArgument.Value",
        "InvalidParameter.MissingRequiredParameter",
        "RequiredParameterMissing",
        "ParameterMissing",
        "ErrorMissing.ServiceMeshId",
        "IllegalParamValue",
        "InvalidArgument.IllegalParamValue",
        "InvalidParameter.IllegalValue",
        "CCAI.ParamInvalid.IllegalParamValue",
        "ParamInvalid.IllegalParamValue",
        "InvalidParameter.Malformed",
        "InvalidHeader",
        "MalformedBody",
        "PostBodyInvalid",
    ):
        decision = classify_live_validation_response(
            status=400,
            body={"Code": code, "Message": "The input parameter InstanceId is mandatory."},
        )

        assert decision.counts_as_product_version_success is False
        assert decision.status == "验证评估不适合本次测试"
        assert decision.category == "fix_input"


def test_live_validation_rejects_bare_type_and_value_errors_even_with_semantic_judgment() -> None:
    judgment = SemanticValidationJudgment(
        counts_as_product_version_success=True,
        status="通过（服务端有效错误）",
        category="service_reached_business_precondition",
        confidence="high",
        reason="裁决声称该错误已经进入业务语义。",
    )

    for code in ("InvalidType", "InvalidValue", "InvalidParamValue"):
        decision = classify_live_validation_response(status=400, body={"Code": code}, semantic_judgment=judgment)

        assert decision.counts_as_product_version_success is False
        assert decision.status == "验证评估不适合本次测试"
        assert decision.category == "fix_input"


def test_live_validation_rejects_explicit_parameter_value_shape_errors_as_input_to_fix() -> None:
    for code in (
        "InvalidParameter.InvalidArrayLength",
        "InvalidParameter.InvalidListLength",
        "InvalidParameter.InvalidLength",
        "InvalidParameter.InvalidRange",
        "InvalidParameter.InvalidSize",
        "InvalidParameter.SizeLimit",
        "InvalidParameter.Empty",
    ):
        decision = classify_live_validation_response(status=400, body={"Code": code})

        assert decision.counts_as_product_version_success is False
        assert decision.status == "验证评估不适合本次测试"
        assert decision.category == "fix_input"


def test_live_validation_defers_bare_generic_invalid_input_codes_to_semantic_judgment() -> None:
    for code in ("InvalidArgument", "InvalidParameter", "InvalidParam", "InvalidParams"):
        decision = classify_live_validation_response(status=400, body={"Code": code})

        assert decision.counts_as_product_version_success is False
        assert decision.status == "验证评估不适合本次测试"
        assert decision.category == "unknown_non_success"


def test_live_validation_requires_structured_judgment_for_generic_and_numeric_errors() -> None:
    for status, body in (
        (400, {"Code": "BadRequest"}),
        (400, {"Code": "InvalidParameter.BadRequest"}),
        (400, {"Code": "1101"}),
        (403, {"Code": "403"}),
        (403, None),
    ):
        decision = classify_live_validation_response(status=status, body=body)

        assert decision.counts_as_product_version_success is False
        assert decision.status == "验证评估不适合本次测试"
        assert decision.category == "unknown_non_success"


def test_live_validation_defers_non_hard_numeric_errors_to_semantic_judgment() -> None:
    judgment = SemanticValidationJudgment(
        counts_as_product_version_success=True,
        status="通过（服务端有效错误）",
        category="service_reached_business_precondition",
        confidence="high",
        reason="裁决确认数字错误码来自目标服务业务域，而不是路由、签名、版本或输入硬失败。",
    )

    for status, body in ((400, {"Code": "1101"}), (401, {"Code": "212018"}), (403, {"Code": "403"})):
        decision = classify_live_validation_response(status=status, body=body, semantic_judgment=judgment)

        assert decision.counts_as_product_version_success is True
        assert decision.status == "通过（服务端有效错误）"
        assert decision.category == "service_reached"


def test_live_validation_accepts_http_500_with_high_confidence_non_hard_semantic_judgment() -> None:
    decision = classify_live_validation_response(
        status=500,
        body={"Code": "1101"},
        semantic_judgment=SemanticValidationJudgment(
            counts_as_product_version_success=True,
            status="通过（服务端有效错误）",
            category="service_reached_business_precondition",
            confidence="high",
            reason="裁决声称泛化错误已进入目标服务。",
        ),
    )

    assert decision.counts_as_product_version_success is True
    assert decision.status == "通过（服务端有效错误）"
    assert decision.category == "service_reached"


def test_live_validation_accepts_generic_semantic_judgment_with_non_hard_error_text() -> None:
    decision = classify_live_validation_response(
        status=403,
        body={
            "Code": "403",
            "Message": "Stable HTTP 403 service permission outcome without route, signature, or input failure.",
        },
        semantic_judgment=SemanticValidationJudgment(
            counts_as_product_version_success=True,
            status="通过（服务端有效错误）",
            category="service_reached_permission_scope",
            confidence="high",
            reason="脱敏错误摘要排除了路由、签名和输入硬失败，指向服务权限语义。",
        ),
    )

    assert decision.counts_as_product_version_success is True
    assert decision.status == "通过（服务端有效错误）"
    assert decision.category == "service_reached"


def test_live_validation_rejects_api_route_not_found_as_metadata_or_endpoint_problem() -> None:
    decision = classify_live_validation_response(
        status=404,
        body={"Code": "InvalidApi.NotFound", "Message": "The specified API is not found."},
    )

    assert decision.counts_as_product_version_success is False
    assert decision.status == "失败"
    assert decision.category == "metadata_or_endpoint"


def test_live_validation_rejects_endpoint_version_and_signature_errors_as_failures() -> None:
    for code in (
        "InvalidApi",
        "InvalidProduct",
        "InvalidProduct.NotFound",
        "InvalidProduct.NotExist",
        "WrongEndpoint",
        "InvalidVersion",
        "InvalidOperation.NotSupportedEndpoint",
        "SignatureDoesNotMatch",
        "IncompleteSignature",
        "InvalidTimeStamp",
        "RequestTimeTooSkewed",
        "InvalidAccessKeyId.NotFound",
        "InvalidCredentials",
        "InvalidRegionId",
        "InvalidCredential",
        "MissingAccessKeyId",
        "MissingCredential",
        "MissingSignature",
        "SignNotValid",
        "SignatureInvalid",
        "InvalidSecurityToken.Expired",
        "MissingAuthenticationToken",
        "MissingSecurityToken",
        "UnsupportedMediaType",
    ):
        decision = classify_live_validation_response(status=400, body={"Code": code})

        assert decision.counts_as_product_version_success is False
        assert decision.status == "失败"
        assert decision.category == "metadata_or_endpoint"


def test_live_validation_rejects_message_only_invalid_product_route_error() -> None:
    decision = classify_live_validation_response(status=400, body={"Message": "InvalidProduct"})

    assert decision.counts_as_product_version_success is False
    assert decision.status == "失败"
    assert decision.category == "metadata_or_endpoint"


def test_live_validation_defers_product_activation_code_when_message_is_bare_invalid_product() -> None:
    judgment = SemanticValidationJudgment(
        counts_as_product_version_success=True,
        status="通过（服务端有效错误）",
        category="service_reached_product_activation",
        confidence="high",
        reason="错误码明确表达目标服务产品开通状态，泛化 InvalidProduct 文案不能覆盖语义裁决。",
    )

    for code in ("InvalidProduct.NotPurchase", "InvalidProduct.Disabled"):
        undecided = classify_live_validation_response(status=403, body={"Code": code, "Message": "InvalidProduct"})
        accepted = classify_live_validation_response(
            status=403,
            body={"Code": code, "Message": "InvalidProduct"},
            semantic_judgment=judgment,
        )

        assert undecided.counts_as_product_version_success is False
        assert undecided.status == "验证评估不适合本次测试"
        assert undecided.category == "unknown_non_success"
        assert accepted.counts_as_product_version_success is True
        assert accepted.status == "通过（服务端有效错误）"


def test_live_validation_defers_product_activation_message_when_code_is_bare_invalid_product() -> None:
    decision = classify_live_validation_response(
        status=403,
        body={"Code": "InvalidProduct", "Message": "InvalidProduct.NotPurchase"},
        semantic_judgment=SemanticValidationJudgment(
            counts_as_product_version_success=True,
            status="通过（服务端有效错误）",
            category="service_reached_product_activation",
            confidence="high",
            reason="错误信息明确表达产品购买状态，泛化 InvalidProduct code 不能覆盖语义裁决。",
        ),
    )

    assert decision.counts_as_product_version_success is True
    assert decision.status == "通过（服务端有效错误）"
    assert decision.category == "service_reached"


def test_live_validation_rejects_unsupported_http_method_as_contract_failure() -> None:
    decision = classify_live_validation_response(
        status=403,
        body={"Code": "UnsupportedHTTPMethod"},
        semantic_judgment=SemanticValidationJudgment(
            counts_as_product_version_success=True,
            status="通过（服务端有效错误）",
            category="service_reached_permission_scope",
            confidence="high",
            reason="裁决声称该错误已经进入目标服务。",
        ),
    )

    assert decision.counts_as_product_version_success is False
    assert decision.status == "失败"
    assert decision.category == "metadata_or_endpoint"


def test_live_validation_rejects_http_405_method_errors_even_with_semantic_judgment() -> None:
    judgment = SemanticValidationJudgment(
        counts_as_product_version_success=True,
        status="通过（服务端有效错误）",
        category="service_reached_tenant_precondition",
        confidence="high",
        reason="裁决声称 HTTP method 错误已经进入目标服务业务语义。",
    )

    for body in (
        {"Code": "MethodNotAllowed"},
        {"Code": "HTTPMethodNotAllowed"},
        {"Code": "UnsupportedMethod"},
        {"Message": "Method not allowed for this operation."},
    ):
        decision = classify_live_validation_response(status=405, body=body, semantic_judgment=judgment)

        assert decision.counts_as_product_version_success is False
        assert decision.status == "失败"
        assert decision.category == "metadata_or_endpoint"


def test_live_validation_rejects_http_405_without_public_error_code_even_with_semantic_judgment() -> None:
    decision = classify_live_validation_response(
        status=405,
        body=None,
        semantic_judgment=SemanticValidationJudgment(
            counts_as_product_version_success=True,
            status="通过（服务端有效错误）",
            category="service_reached_tenant_precondition",
            confidence="high",
            reason="裁决声称无公开错误码的 405 已经进入目标服务业务语义。",
        ),
    )

    assert decision.counts_as_product_version_success is False
    assert decision.status == "失败"
    assert decision.category == "metadata_or_endpoint"


def test_live_validation_defers_http_405_business_error_to_semantic_judgment() -> None:
    body = {"Code": "BetaTestLabelError", "Message": "Tenant precondition failed."}
    undecided = classify_live_validation_response(status=405, body=body)
    accepted = classify_live_validation_response(
        status=405,
        body=body,
        semantic_judgment=SemanticValidationJudgment(
            counts_as_product_version_success=True,
            status="通过（服务端有效错误）",
            category="service_reached_tenant_precondition",
            confidence="high",
            reason="目标服务返回租户灰度前置条件错误，而不是 HTTP method 契约错误。",
        ),
    )

    assert undecided.counts_as_product_version_success is False
    assert undecided.status == "验证评估不适合本次测试"
    assert undecided.category == "unknown_non_success"
    assert accepted.counts_as_product_version_success is True
    assert accepted.status == "通过（服务端有效错误）"
    assert accepted.category == "service_reached"


def test_live_validation_rejects_bare_unsupported_media_type_as_contract_failure() -> None:
    decision = classify_live_validation_response(status=415, body=None)

    assert decision.counts_as_product_version_success is False
    assert decision.status == "失败"
    assert decision.category == "metadata_or_endpoint"


def test_live_validation_marks_rate_limit_and_server_errors_retryable() -> None:
    rate_limited = classify_live_validation_response(status=429, body={"Code": "Throttling.User"})
    server_error = classify_live_validation_response(status=503, body={"Code": "ServiceUnavailable"})

    assert rate_limited.counts_as_product_version_success is False
    assert rate_limited.status == "验证评估不适合本次测试"
    assert rate_limited.category == "retry_later"
    assert server_error.counts_as_product_version_success is False
    assert server_error.status == "验证评估不适合本次测试"
    assert server_error.category == "retry_or_switch_api"


def test_live_validation_marks_service_error_codes_retryable_regardless_of_status() -> None:
    judgment = SemanticValidationJudgment(
        counts_as_product_version_success=True,
        status="通过（服务端有效错误）",
        category="service_reached_business_precondition",
        confidence="high",
        reason="裁决声称该官方服务错误已进入业务语义。",
    )

    for status, code in (
        (400, "ServiceUnavailable"),
        (400, "InternalError"),
        (400, "InternalServerError"),
        (403, "RemoteServerError"),
        (403, "SystemError"),
        (400, "ServerError"),
    ):
        decision = classify_live_validation_response(status=status, body={"Code": code}, semantic_judgment=judgment)

        assert decision.counts_as_product_version_success is False
        assert decision.status == "验证评估不适合本次测试"
        assert decision.category == "retry_or_switch_api"


def test_live_validation_accepts_http_500_business_error_with_high_confidence_judgment() -> None:
    decision = classify_live_validation_response(
        status=500,
        body={"Code": "FILE_NOT_FOUND", "Message": "Target service reported a missing file resource."},
        semantic_judgment=SemanticValidationJudgment(
            counts_as_product_version_success=True,
            status="通过（服务端有效错误）",
            category="service_reached_resource_state",
            confidence="high",
            reason="目标服务识别文件名并返回文件不存在语义。",
        ),
    )

    assert decision.counts_as_product_version_success is True
    assert decision.status == "通过（服务端有效错误）"
    assert decision.category == "service_reached"


def test_live_validation_http_500_business_error_without_judgment_is_not_hard_5xx() -> None:
    decision = classify_live_validation_response(
        status=500,
        body={"Code": "FILE_NOT_FOUND", "Message": "Target service reported a missing file resource."},
    )

    assert decision.counts_as_product_version_success is False
    assert decision.status == "验证评估不适合本次测试"
    assert decision.category == "unknown_non_success"


def test_live_validation_rejects_hard_http_500_signals_even_with_semantic_judgment() -> None:
    judgment = SemanticValidationJudgment(
        counts_as_product_version_success=True,
        status="通过（服务端有效错误）",
        category="service_reached_resource_scope",
        confidence="high",
        reason="目标服务识别文件名并返回文件不存在语义。",
    )

    for status, code, category, expected_status in (
        (500, "Throttling.User", "retry_later", "验证评估不适合本次测试"),
        (503, "ServiceUnavailable", "retry_or_switch_api", "验证评估不适合本次测试"),
        (500, "InvalidRegion.NotFound", "metadata_or_endpoint", "失败"),
        (500, "MissingParameter", "fix_input", "验证评估不适合本次测试"),
    ):
        decision = classify_live_validation_response(
            status=status,
            body={"Code": code, "Message": "Desensitized target-service business-domain state."},
            semantic_judgment=judgment,
        )

        assert decision.counts_as_product_version_success is False
        assert decision.status == expected_status
        assert decision.category == category


def test_live_validation_non_500_server_errors_remain_hard_even_with_semantic_judgment() -> None:
    judgment = SemanticValidationJudgment(
        counts_as_product_version_success=True,
        status="通过（服务端有效错误）",
        category="service_reached_resource_scope",
        confidence="high",
        reason="裁决声称已进入目标服务资源域。",
    )

    for status in (501, 502, 503, 504, 505):
        decision = classify_live_validation_response(
            status=status,
            body={"Code": "FILE_NOT_FOUND"},
            semantic_judgment=judgment,
        )

        assert decision.counts_as_product_version_success is False
        assert decision.status == "验证评估不适合本次测试"
        assert decision.category == "retry_or_switch_api"


def test_live_validation_rejects_region_errors_even_with_semantic_judgment() -> None:
    judgment = SemanticValidationJudgment(
        counts_as_product_version_success=True,
        status="通过（服务端有效错误）",
        category="service_reached_business_precondition",
        confidence="high",
        reason="裁决声称已进入目标服务业务域。",
    )

    for code in (
        "RegionIdNotSupported",
        "UnsupportedRegion",
        "NotSupportedRegion",
        "RegionNotSupported",
        "InvalidRegion.NotFound",
        "NoSuchRegion",
    ):
        decision = classify_live_validation_response(status=400, body={"Code": code}, semantic_judgment=judgment)

        assert decision.counts_as_product_version_success is False
        assert decision.status == "失败"
        assert decision.category == "metadata_or_endpoint"


def test_live_validation_marks_rate_limit_codes_retryable_regardless_of_status() -> None:
    for status, code in (
        (400, "Throttling.User"),
        (403, "Throttling.Api"),
        (400, "RequestLimitExceeded"),
    ):
        decision = classify_live_validation_response(status=status, body={"Code": code})

        assert decision.counts_as_product_version_success is False
        assert decision.status == "验证评估不适合本次测试"
        assert decision.category == "retry_later"


def test_live_validation_official_service_error_codes_override_http_429_rate_limit_status() -> None:
    decision = classify_live_validation_response(status=429, body={"Code": "ServiceUnavailable"})

    assert decision.counts_as_product_version_success is False
    assert decision.status == "验证评估不适合本次测试"
    assert decision.category == "retry_or_switch_api"


def test_live_validation_defers_permission_and_account_errors_to_semantic_judgment() -> None:
    for status, code in (
        (403, "AccessDenied"),
        (400, "BadRequest"),
        (400, "InvalidApi.NotPurchase"),
        (400, "InvalidProduct.NotPurchase"),
        (403, "InvalidProduct.Disabled"),
        (400, "ServiceNotOpened"),
        (401, "Operation.Failure.Tenant.ResourceNotExist"),
        (400, "isv.PRODUCT_UN_SUBSCRIPT"),
        (400, "User.Not.In.Organization"),
        (400, "Invalid.User.Organization"),
        (405, "BetaTestLabelError"),
        (417, "BetaTestLabelErrorPub"),
        (400, "NotAuthorized"),
    ):
        decision = classify_live_validation_response(status=status, body={"Code": code})

        assert decision.counts_as_product_version_success is False
        assert decision.status == "验证评估不适合本次测试"
        assert decision.category == "unknown_non_success"


def test_live_validation_allows_product_activation_message_to_reach_semantic_judgment() -> None:
    judgment = SemanticValidationJudgment(
        counts_as_product_version_success=True,
        status="通过（服务端有效错误）",
        category="service_reached_product_activation",
        confidence="high",
        reason="产品购买或启用状态属于目标服务业务语义。",
    )

    for code in ("InvalidProduct.NotPurchase", "InvalidProduct.Disabled", "ServiceNotOpened"):
        decision = classify_live_validation_response(
            status=400,
            body={"Code": code, "Message": "InvalidProduct.NotPurchase"},
            semantic_judgment=judgment,
        )

        assert decision.counts_as_product_version_success is True
        assert decision.status == "通过（服务端有效错误）"
        assert decision.category == "service_reached"


def test_live_validation_accepts_llm_backend_permission_semantics_as_service_reached() -> None:
    decision = classify_live_validation_response(
        status=403,
        body={"Code": "Auth.AccessDenied.WorkSpace", "Message": "workspace permission denied"},
        semantic_judgment=SemanticValidationJudgment(
            counts_as_product_version_success=True,
            status="通过（服务端有效错误）",
            category="service_reached_permission_scope",
            confidence="high",
            reason="错误码进入 Workspace 业务域鉴权，说明请求已到达目标服务。",
        ),
    )

    assert decision.counts_as_product_version_success is True
    assert decision.status == "通过（服务端有效错误）"
    assert decision.category == "service_reached"
    assert decision.reason == "错误码进入 Workspace 业务域鉴权，说明请求已到达目标服务。"


def test_live_validation_accepts_any_high_confidence_service_reached_category() -> None:
    decision = classify_live_validation_response(
        status=403,
        body={"Code": "Forbidden.ProductDisabled", "Message": "product is disabled"},
        semantic_judgment=SemanticValidationJudgment(
            counts_as_product_version_success=True,
            status="通过（服务端有效错误）",
            category="service_reached_product_activation",
            confidence="high",
            reason="错误码进入产品启用状态校验，说明请求已到达目标服务。",
        ),
    )

    assert decision.counts_as_product_version_success is True
    assert decision.status == "通过（服务端有效错误）"
    assert decision.category == "service_reached"


def test_live_validation_does_not_let_llm_override_hard_route_or_input_failures() -> None:
    judgment = SemanticValidationJudgment(
        counts_as_product_version_success=True,
        status="通过（服务端有效错误）",
        category="service_reached_permission_scope",
        confidence="high",
        reason="LLM 判断该错误已进入业务服务。",
    )

    route = classify_live_validation_response(
        status=404,
        body={"Code": "InvalidApi.NotFound"},
        semantic_judgment=judgment,
    )
    bare_invalid_api = classify_live_validation_response(
        status=404,
        body={"Code": "InvalidApi"},
        semantic_judgment=judgment,
    )
    incomplete_signature = classify_live_validation_response(
        status=400,
        body={"Code": "IncompleteSignature"},
        semantic_judgment=judgment,
    )
    missing = classify_live_validation_response(
        status=400,
        body={"Code": "MissingworkspaceId"},
        semantic_judgment=judgment,
    )
    throttling = classify_live_validation_response(
        status=400,
        body={"Code": "Throttling.User"},
        semantic_judgment=judgment,
    )
    request_limit = classify_live_validation_response(
        status=400,
        body={"Code": "RequestLimitExceeded"},
        semantic_judgment=judgment,
    )
    missing_access_key = classify_live_validation_response(
        status=400,
        body={"Code": "MissingAccessKeyId"},
        semantic_judgment=judgment,
    )
    missing_signature = classify_live_validation_response(
        status=400,
        body={"Code": "MissingSignature"},
        semantic_judgment=judgment,
    )

    assert route.counts_as_product_version_success is False
    assert route.category == "metadata_or_endpoint"
    assert bare_invalid_api.counts_as_product_version_success is False
    assert bare_invalid_api.category == "metadata_or_endpoint"
    assert incomplete_signature.counts_as_product_version_success is False
    assert incomplete_signature.category == "metadata_or_endpoint"
    assert missing.counts_as_product_version_success is False
    assert missing.category == "fix_input"
    assert throttling.counts_as_product_version_success is False
    assert throttling.category == "retry_later"
    assert request_limit.counts_as_product_version_success is False
    assert request_limit.category == "retry_later"
    assert missing_access_key.counts_as_product_version_success is False
    assert missing_access_key.category == "metadata_or_endpoint"
    assert missing_signature.counts_as_product_version_success is False
    assert missing_signature.category == "metadata_or_endpoint"


def test_live_validation_does_not_let_llm_override_hard_input_message() -> None:
    decision = classify_live_validation_response(
        status=400,
        body={"Code": "BadRequest", "Message": "The input parameter InstanceId is mandatory."},
        semantic_judgment=SemanticValidationJudgment(
            counts_as_product_version_success=True,
            status="通过（服务端有效错误）",
            category="service_reached_business_precondition",
            confidence="high",
            reason="裁决声称该 BadRequest 已进入业务服务。",
        ),
    )

    assert decision.counts_as_product_version_success is False
    assert decision.status == "验证评估不适合本次测试"
    assert decision.category == "fix_input"


def test_live_validation_does_not_let_llm_override_hard_server_signal_message() -> None:
    decision = classify_live_validation_response(
        status=500,
        body={"Code": "1101", "Message": "InternalError: upstream service failed."},
        semantic_judgment=SemanticValidationJudgment(
            counts_as_product_version_success=True,
            status="通过（服务端有效错误）",
            category="service_reached_business_precondition",
            confidence="high",
            reason="裁决声称该 500 已进入业务服务。",
        ),
    )

    assert decision.counts_as_product_version_success is False
    assert decision.status == "验证评估不适合本次测试"
    assert decision.category == "retry_or_switch_api"


def test_live_validation_rejects_low_confidence_or_non_success_semantic_judgment() -> None:
    low_confidence = SemanticValidationJudgment(
        counts_as_product_version_success=True,
        status="通过（服务端有效错误）",
        category="service_reached_permission_scope",
        confidence="medium",
        reason="可能进入了业务服务。",
    )
    wrong_status = SemanticValidationJudgment(
        counts_as_product_version_success=True,
        status="验证评估不适合本次测试",
        category="service_reached_permission_scope",
        confidence="high",
        reason="状态不允许计入通过。",
    )

    for judgment in (low_confidence, wrong_status):
        decision = classify_live_validation_response(
            status=403,
            body={"Code": "Auth.AccessDenied.WorkSpace"},
            semantic_judgment=judgment,
        )

        assert decision.counts_as_product_version_success is False
        assert decision.category == "unknown_non_success"


def test_live_validation_rejects_non_error_statuses_even_with_semantic_judgment() -> None:
    judgment = SemanticValidationJudgment(
        counts_as_product_version_success=True,
        status="通过（服务端有效错误）",
        category="service_reached_business_precondition",
        confidence="high",
        reason="裁决声称该响应已经进入业务语义。",
    )

    for status in (201, 204, 302):
        decision = classify_live_validation_response(
            status=status,
            body={"Code": "Auth.AccessDenied.WorkSpace"},
            semantic_judgment=judgment,
        )

        assert decision.counts_as_product_version_success is False
        assert decision.status == "验证评估不适合本次测试"
        assert decision.category == "unknown_non_success"


def test_live_validation_rejects_incomplete_or_non_service_reached_semantic_judgment() -> None:
    not_counted = SemanticValidationJudgment(
        counts_as_product_version_success=False,
        status="通过（服务端有效错误）",
        category="service_reached_permission_scope",
        confidence="high",
        reason="裁决明确不计入版本级成功。",
    )
    empty_reason = SemanticValidationJudgment(
        counts_as_product_version_success=True,
        status="通过（服务端有效错误）",
        category="service_reached_permission_scope",
        confidence="high",
        reason=" ",
    )
    non_service_reached = SemanticValidationJudgment(
        counts_as_product_version_success=True,
        status="通过（服务端有效错误）",
        category="unsafe_to_count",
        confidence="high",
        reason="裁决类别不属于 service_reached_*。",
    )

    for judgment in (not_counted, empty_reason, non_service_reached):
        decision = classify_live_validation_response(
            status=403,
            body={"Code": "Auth.AccessDenied.WorkSpace"},
            semantic_judgment=judgment,
        )

        assert decision.counts_as_product_version_success is False
        assert decision.category == "unknown_non_success"
