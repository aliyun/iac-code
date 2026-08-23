"""Extraction and translation-boundary coverage for Aliyun public errors."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from babel.messages.pofile import read_po

from iac_code.services.providers.aliyun_credentials_runtime import (
    ECS_CREDENTIAL_ERROR_CODES,
    ECS_IMDSV2_REQUIRED,
    ECS_METADATA_DISABLED,
    ECS_METADATA_UNREACHABLE,
    ECS_RAM_ROLE_NOT_FOUND,
    ECS_RAM_ROLE_REFRESH_FAILED,
    ECS_RAM_ROLE_RESPONSE_INVALID,
)
from iac_code.tools.cloud.aliyun.api_contract import ApiContractError
from iac_code.tools.cloud.aliyun.public_errors import public_aliyun_error


def test_aliyun_public_error_templates_are_directly_extractable(tmp_path: Path) -> None:
    project_root = Path(__file__).resolve().parents[4]
    output = tmp_path / "aliyun-public-errors.pot"
    subprocess.run(
        [
            "uv",
            "run",
            "pybabel",
            "extract",
            "-F",
            "babel.cfg",
            "--add-location=file",
            "-o",
            str(output),
            "src/iac_code/tools/cloud/aliyun/public_errors.py",
        ],
        cwd=project_root,
        check=True,
        capture_output=True,
        timeout=60,
    )

    with output.open(encoding="utf-8") as stream:
        msgids = {message.id for message in read_po(stream) if isinstance(message.id, str) and message.id}

    required = {
        "Alibaba Cloud API action must contain only letters, numbers, underscores, or hyphens.",
        "Alibaba Cloud API authorization expired or changed. Run {operation} again to approve the current contract.",
        "Alibaba Cloud API execution is unavailable for {operation}. Retry from an active runtime.",
        "Alibaba Cloud API {operation} cannot modify cloud resources from a pipeline step. "
        "Use a pipeline-specific tool for this operation.",
        "Alibaba Cloud API input is invalid for {operation}. Check product, action, version, region, and parameters.",
        "Alibaba Cloud returned the target response, but its headers are too large to display safely. "
        "Verify the cloud resource state before retrying.",
        "Alibaba Cloud returned an unsupported response body for {operation}. "
        "Verify the cloud resource state before retrying.",
        "Alibaba Cloud API metadata for {operation} is temporarily unavailable; try again later.",
        "Alibaba Cloud endpoint cache could not be updated for {operation} in {region}. "
        "Check local configuration storage and retry.",
        "Alibaba Cloud API parameters are invalid for {operation}.",
        "Alibaba Cloud API path parameters are invalid or incomplete for {operation}.",
        "Alibaba Cloud API protocol overrides are invalid for {operation}. "
        "Remove style, method, or pathname overrides and retry.",
        "Alibaba Cloud API {operation} body is invalid. Provide a valid JSON or form request body.",
        "Alibaba Cloud API {operation} body_file exceeds the 32 MiB limit. Use a smaller regular file.",
        "Alibaba Cloud API {operation} body_file is invalid. Use a readable regular file within the allowed size.",
        "Alibaba Cloud API {operation} content_type does not match the request body. Use a compatible media type.",
        "Alibaba Cloud API {operation} content_type is invalid. Use a valid media type such as application/json.",
        "Alibaba Cloud API {operation} could not be prepared safely. Check the request and try again.",
        "Alibaba Cloud API {operation} cannot be executed from its current metadata. "
        "Choose another API version or action.",
        "Alibaba Cloud API {operation} metadata uses a schema this runtime cannot execute. "
        "Choose another API version or action.",
        "Alibaba Cloud API {operation} uses a protocol shape this runtime cannot execute. "
        "Choose another API version or action.",
        "Alibaba Cloud OSS API {operation} is not supported by this runtime. "
        "Choose a supported OSS action or use another client.",
        "Alibaba Cloud API {operation} parameter {parameter} expects a scalar header value without line breaks "
        "but received {actual}.",
        "Alibaba Cloud API {operation} parameter {parameter} expects a valid header name "
        "but received a {actual} with invalid syntax.",
        "Alibaba Cloud API {operation} parameter {parameter} expects a valid DNS host-label string "
        "but received {actual} with invalid syntax.",
        "Alibaba Cloud API {operation} parameter {parameter} targets a reserved authentication header. "
        "Remove the parameter and retry.",
        "Alibaba Cloud API {operation} returned HTTP {status}. "
        "Check the request and cloud permissions before retrying.",
        "Alibaba Cloud API {operation} returned HTTP {status} with error code {code}. "
        "Check the request and cloud permissions before retrying.",
        "Alibaba Cloud API {operation} received a retryable service response, but the retry deadline expired. "
        "Retry the read-only request later.",
        "Alibaba Cloud API {operation} read-only request did not complete before the retry deadline. "
        "Check network and service health, then retry.",
        "Alibaba Cloud API {operation} timed out after the request may have been sent. "
        "Check cloud state before retrying to avoid duplicate changes.",
        "Alibaba Cloud API {operation} timed out before the request was sent. Retry the operation.",
        "Alibaba Cloud API {operation} returned an invalid response after the request was sent. "
        "Check cloud state before retrying to avoid duplicate changes.",
        "Alibaba Cloud API {operation} could not connect before the request was sent. "
        "Check network and endpoint access, then retry.",
        "Alibaba Cloud API {operation} may have been sent before the connection failed. "
        "Check cloud state before retrying to avoid duplicate changes.",
        "Alibaba Cloud API {operation} response exceeded max_response_bytes. "
        "Increase the limit or narrow the request and retry.",
        "Alibaba Cloud API {operation} error response exceeded the 1 MiB safety limit. "
        "Check Alibaba Cloud logs before retrying.",
        "Alibaba Cloud API {operation} does not accept body_file. Use the body source declared by the API contract.",
        "Alibaba Cloud API {operation} has content_type without a request body. Remove content_type or provide a body.",
        "the requested product",
        "the requested action",
        "the requested region",
        "the requested parameter",
        "Alibaba Cloud product must contain only letters, numbers, underscores, or hyphens.",
        "Alibaba Cloud product {product} was not found. Check the product code and try again.",
        " Suggested product codes: {suggestions}.",
        "Alibaba Cloud region must contain only lowercase letters, numbers, or hyphens.",
        "Alibaba Cloud API version contains unsupported characters.",
        "Alibaba Cloud API {operation} is missing required parameters.",
        "Alibaba Cloud API {operation} parameter {parameter} is not an allowed value.",
        "Alibaba Cloud API {operation} parameter {parameter} is not an allowed value. Allowed values: {allowed}.",
        "Alibaba Cloud API {operation} parameter {parameter} expects {expected} but received {actual}.",
        "Alibaba Cloud API {operation} requires parameters {parameters}.",
        "Alibaba Cloud API {operation} requires body_file for its binary request body.",
        "Alibaba Cloud API {operation} parameter {parameter} is reserved for request signing and cannot be set.",
        "Alibaba Cloud API {operation} is missing path parameter {parameter}.",
        "Alibaba Cloud API {operation} is missing path parameters {parameters}.",
        "Alibaba Cloud API metadata for {operation} returned an incompatible response. "
        "Check the API identifiers or try again later.",
        "Alibaba Cloud API {operation} max_response_bytes must be between 1 and 16777216.",
        "Alibaba Cloud API {operation} detail must be one of: summary, full.",
        "Alibaba Cloud API {operation} path parameter {parameter} expects {expected} but received {actual}.",
        "Alibaba Cloud API {operation} received multiple body sources. "
        "Provide only one of body, body_file, or body parameters.",
        "Alibaba Cloud API {operation} received multiple template sources. Provide only one.",
        "Alibaba Cloud API {operation} requires a body source compatible with its API contract.",
        "Alibaba Cloud API {operation} requires parameter {parameter}.",
        "Alibaba Cloud API {operation} template file is invalid. Use a readable regular template file.",
        "Alibaba Cloud API {operation} template validation failed. Check the template syntax and resource definitions.",
        "Alibaba Cloud API {operation} was not found. Check the product, version, and action.",
        "Alibaba Cloud API {product}/{version}/{action} was not found. Check the product, version, and action.",
        "Alibaba Cloud authentication is unsupported for {operation}. "
        "Check the API contract or choose an API that supports AccessKey authentication.",
        "Alibaba Cloud credentials are unavailable. Configure credentials and retry {operation}.",
        "Alibaba Cloud host parameters are invalid for {operation} in {region}.",
        "Alibaba Cloud signing is unsupported for {operation}. Check the API contract.",
        "No valid Alibaba Cloud API version is available for {product}; provide an explicit version.",
        "No trusted Alibaba Cloud endpoint is available for {operation} in {region}. "
        "Check the region or endpoint configuration.",
        "Alibaba Cloud API authorization expired or changed. Run {operation} again to approve the current contract.",
        "Alibaba Cloud ECS instance metadata service is unreachable, so {operation} cannot be signed. "
        "Confirm this process runs on an ECS instance with a bound RAM role.",
        "Alibaba Cloud ECS instance metadata credentials are disabled, so {operation} cannot be signed. "
        "Check the ALIBABA_CLOUD_ECS_METADATA_DISABLED environment variable.",
        "Alibaba Cloud ECS metadata token (IMDSv2) could not be obtained while IMDSv1 is disabled, "
        "so {operation} cannot be signed. Check the instance metadata settings and network.",
        "No matching Alibaba Cloud ECS instance RAM role was found, so {operation} cannot be signed. "
        "Check the instance RAM role and the configured ECS RAM role name.",
        "Alibaba Cloud ECS instance metadata returned incomplete RAM role credentials, "
        "so {operation} cannot be signed. Retry and check the ECS metadata service.",
        "Alibaba Cloud ECS instance RAM role credentials could not be refreshed before they expired, "
        "so {operation} cannot be signed. Check ECS metadata availability.",
    }
    assert msgids == required


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        (
            "retryable_status",
            "Alibaba Cloud API Ecs/DescribeInstances received a retryable service response, but the retry deadline "
            "expired. Retry the read-only request later.",
        ),
        (
            "read_timeout",
            "Alibaba Cloud API Ecs/DescribeInstances read-only request did not complete before the retry deadline. "
            "Check network and service health, then retry.",
        ),
    ],
)
def test_retry_exhaustion_errors_are_actionable_for_read_only_calls(code: str, expected: str) -> None:
    assert public_aliyun_error(code, product="Ecs", action="DescribeInstances") == expected


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        (
            "body_file_too_large",
            "Alibaba Cloud API Oss/PutObject body_file exceeds the 32 MiB limit. Use a smaller regular file.",
        ),
        (
            "content_type_without_body",
            "Alibaba Cloud API Oss/PutObject has content_type without a request body. "
            "Remove content_type or provide a body.",
        ),
        (
            "invalid_max_response_bytes",
            "Alibaba Cloud API Oss/PutObject max_response_bytes must be between 1 and 16777216.",
        ),
    ],
)
def test_public_errors_are_actionable_for_locally_correctable_inputs(code: str, expected: str) -> None:
    assert public_aliyun_error(code, product="Oss", action="PutObject") == expected


def test_location_cache_failure_uses_endpoint_context_and_remediation() -> None:
    assert public_aliyun_error(
        "location_cache_write_failed",
        product="FC",
        action="GetFunction",
        region_id="cn-hangzhou",
    ) == (
        "Alibaba Cloud endpoint cache could not be updated for FC/GetFunction in cn-hangzhou. "
        "Check local configuration storage and retry."
    )


def test_pipeline_write_forbidden_error_requires_pipeline_specific_tool() -> None:
    assert public_aliyun_error(
        "aliyun_pipeline_write_forbidden",
        product="ros",
        action="UpdateTemplate",
    ) == (
        "Alibaba Cloud API ros/UpdateTemplate cannot modify cloud resources from a pipeline step. "
        "Use a pipeline-specific tool for this operation."
    )


def test_missing_required_parameter_error_reports_every_safe_name_without_values() -> None:
    error = ApiContractError("missing_required_parameters:InstanceId,RegionId")

    message = public_aliyun_error(error, product="Ecs", action="DescribeInstances")

    assert message == "Alibaba Cloud API Ecs/DescribeInstances requires parameters InstanceId,RegionId."
    assert "business-value" not in message


@pytest.mark.parametrize(
    "parameter_names",
    [
        ",".join(f"Parameter{index}" for index in range(17)),
        ",".join(f"Parameter{index}{'x' * 110}" for index in range(5)),
    ],
)
def test_missing_required_parameter_error_bounds_public_parameter_list(parameter_names: str) -> None:
    error = ApiContractError(f"missing_required_parameters:{parameter_names}")

    message = public_aliyun_error(error, product="Ecs", action="DescribeInstances")

    assert message == "Alibaba Cloud API Ecs/DescribeInstances is missing required parameters."
    assert parameter_names not in message


def test_wildcard_parameter_name_is_preserved_as_safe_public_context() -> None:
    error = ApiContractError(
        "invalid_parameter_type:x-oss-meta-*",
        parameter="x-oss-meta-*",
        expected_type="object",
        actual_type="string",
    )

    message = public_aliyun_error(error, product="Oss", action="PutObject")

    assert message == ("Alibaba Cloud API Oss/PutObject parameter x-oss-meta-* expects object but received string.")


def test_product_not_found_suggestions_are_sanitized_and_bounded() -> None:
    error = ApiContractError(
        "product_not_found",
        suggestions=("Dysmsapi", "unsafe/value", "Dyvmsapi", "Ecs", "Rds"),
    )

    message = public_aliyun_error(error, product="dy0msapi", action="DescribeSomething")

    assert message == (
        "Alibaba Cloud product dy0msapi was not found. Check the product code and try again. "
        "Suggested product codes: Dysmsapi, Dyvmsapi, Ecs."
    )
    assert "unsafe/value" not in message
    assert "Rds" not in message


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        (
            ECS_METADATA_UNREACHABLE,
            "Alibaba Cloud ECS instance metadata service is unreachable, so Ros/CreateStack cannot be signed. "
            "Confirm this process runs on an ECS instance with a bound RAM role.",
        ),
        (
            ECS_METADATA_DISABLED,
            "Alibaba Cloud ECS instance metadata credentials are disabled, so Ros/CreateStack cannot be signed. "
            "Check the ALIBABA_CLOUD_ECS_METADATA_DISABLED environment variable.",
        ),
        (
            ECS_IMDSV2_REQUIRED,
            "Alibaba Cloud ECS metadata token (IMDSv2) could not be obtained while IMDSv1 is disabled, so "
            "Ros/CreateStack cannot be signed. Check the instance metadata settings and network.",
        ),
        (
            ECS_RAM_ROLE_NOT_FOUND,
            "No matching Alibaba Cloud ECS instance RAM role was found, so Ros/CreateStack cannot be signed. "
            "Check the instance RAM role and the configured ECS RAM role name.",
        ),
        (
            ECS_RAM_ROLE_RESPONSE_INVALID,
            "Alibaba Cloud ECS instance metadata returned incomplete RAM role credentials, so Ros/CreateStack "
            "cannot be signed. Retry and check the ECS metadata service.",
        ),
        (
            ECS_RAM_ROLE_REFRESH_FAILED,
            "Alibaba Cloud ECS instance RAM role credentials could not be refreshed before they expired, so "
            "Ros/CreateStack cannot be signed. Check ECS metadata availability.",
        ),
    ],
)
def test_ecs_credential_errors_are_actionable(code: str, expected: str) -> None:
    assert public_aliyun_error(code, product="Ros", action="CreateStack", region_id="cn-hangzhou") == expected


def test_every_ecs_credential_code_has_a_dedicated_public_message() -> None:
    messages = {
        code: public_aliyun_error(code, product="Ros", action="CreateStack") for code in ECS_CREDENTIAL_ERROR_CODES
    }

    # No code may fall through to a generic message, and no two codes may collide.
    assert len(set(messages.values())) == len(ECS_CREDENTIAL_ERROR_CODES)
    for code, message in messages.items():
        assert code not in message
        assert "Ros/CreateStack" in message
        assert message.endswith(".")


def test_ecs_credential_errors_never_echo_metadata_content() -> None:
    """A metadata-bearing upstream failure must not reach the user through this mapping."""
    sdk_error = ValueError(
        "Failed to get RAM session credentials from ECS metadata service. "
        "HttpCode=404, url=http://100.100.100.200/latest/meta-data/ram/security-credentials/secret-role-name, "
        'ResponseBody={"AccessKeyId":"STS.leaked","AccessKeySecret":"leaked-secret","SecurityToken":"leaked-token"}'
    )

    message = public_aliyun_error(ECS_RAM_ROLE_NOT_FOUND, product="Ros", action="CreateStack")

    for secret in ("100.100.100.200", "secret-role-name", "STS.leaked", "leaked-secret", "leaked-token", "404"):
        assert secret not in message
    # Sanity check: the redaction holds because the code, not the upstream text, drives the message.
    assert str(sdk_error) not in message
