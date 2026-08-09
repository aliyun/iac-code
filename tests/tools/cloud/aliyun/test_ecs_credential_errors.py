"""Tests for recognizing credential-runtime ECS failures behind the SDK envelope.

The SDK clients here are real: only the instance metadata provider is faked. Signing
fails before a request is built, so nothing reaches 100.100.100.200 or a cloud endpoint.
"""

from __future__ import annotations

import asyncio

import pytest
from alibabacloud_ros20190910 import models as ros_models
from alibabacloud_tea_openapi import models as open_api_models
from alibabacloud_tea_openapi.client import Client as OpenApiClient
from alibabacloud_tea_util.models import RuntimeOptions
from darabonba.exceptions import UnretryableException
from darabonba.policy.retry import RetryPolicyContext
from Tea.exceptions import UnretryableException as TeaUnretryableException
from Tea.request import TeaRequest

from iac_code.services.providers.aliyun_credentials_runtime import (
    ECS_CREDENTIAL_ERROR_CODES,
    ECS_METADATA_UNREACHABLE,
    ECS_RAM_ROLE_NOT_FOUND,
)
from iac_code.tools.cloud.aliyun.api_contract import ApiContractError
from iac_code.tools.cloud.aliyun.ecs_credential_errors import ecs_credential_error_code
from iac_code.tools.cloud.aliyun.public_errors import public_aliyun_error
from iac_code.tools.cloud.aliyun.ros_client import RosClientFactory, public_ecs_credential_message
from tests.tools.cloud.aliyun._ecs_ram_role_fakes import FakeEcsRuntime


def _envelope(inner: BaseException) -> UnretryableException:
    """Wrap `inner` the way the Darabonba core does when a signing call fails."""
    return UnretryableException(RetryPolicyContext(http_request=TeaRequest(), exception=inner))


@pytest.mark.parametrize("code", sorted(ECS_CREDENTIAL_ERROR_CODES))
def test_every_stable_code_is_recognized_directly_and_inside_the_envelope(code: str) -> None:
    assert ecs_credential_error_code(ValueError(code)) == code
    assert ecs_credential_error_code(_envelope(ValueError(code))) == code


@pytest.mark.parametrize(
    "error",
    [
        ValueError("boom"),
        RuntimeError(ECS_METADATA_UNREACHABLE),
        Exception(ECS_METADATA_UNREACHABLE),
        _envelope(ValueError("boom")),
        _envelope(RuntimeError(ECS_METADATA_UNREACHABLE)),
        _envelope(_envelope(ValueError(ECS_METADATA_UNREACHABLE))),
        TeaUnretryableException(TeaRequest(), ValueError(ECS_METADATA_UNREACHABLE)),
    ],
    ids=[
        "unrelated_value_error",
        "runtime_error_with_a_code_message",
        "exception_with_a_code_message",
        "envelope_around_an_unrelated_value_error",
        "envelope_around_a_runtime_error",
        "envelope_around_an_envelope",
        "tea_base_envelope",
    ],
)
def test_unrelated_failures_are_not_credential_failures(error: BaseException) -> None:
    assert ecs_credential_error_code(error) is None


def test_cause_and_context_chains_are_not_followed() -> None:
    """A code-shaped failure somewhere in the chain does not make this a credential error."""
    error = RuntimeError("request failed")
    error.__cause__ = ValueError(ECS_METADATA_UNREACHABLE)
    context_only = RuntimeError("request failed")
    context_only.__context__ = ValueError(ECS_METADATA_UNREACHABLE)

    assert ecs_credential_error_code(error) is None
    assert ecs_credential_error_code(context_only) is None


def _openapi_client(fake_ecs_runtime: FakeEcsRuntime) -> OpenApiClient:
    from iac_code.services.providers.aliyun_credentials_runtime import aliyun_credential_runtime

    return OpenApiClient(
        open_api_models.Config(
            credential=aliyun_credential_runtime().sdk_client(fake_ecs_runtime.credential()),
            region_id="cn-shanghai",
            endpoint="ecs.cn-shanghai.aliyuncs.com",
        )
    )


def _openapi_call_arguments() -> tuple[open_api_models.Params, open_api_models.OpenApiRequest, RuntimeOptions]:
    params = open_api_models.Params(
        action="DescribeRegions",
        version="2014-05-26",
        protocol="HTTPS",
        pathname="/",
        method="POST",
        auth_type="AK",
        style="RPC",
        body_type="json",
        req_body_type="json",
    )
    return params, open_api_models.OpenApiRequest(query={}), RuntimeOptions(autoretry=False, max_attempts=1)


def test_the_openapi_sdk_wraps_the_credential_failure(fake_ecs_runtime: FakeEcsRuntime) -> None:
    fake_ecs_runtime.fail_with(ValueError(ECS_METADATA_UNREACHABLE))
    client = _openapi_client(fake_ecs_runtime)
    params, request, runtime = _openapi_call_arguments()

    with pytest.raises(BaseException) as raised:
        client.call_api(params, request, runtime)

    # The exact shape the reviewers reproduced: the stable code is only reachable through
    # the envelope's own inner exception.
    assert type(raised.value) is UnretryableException
    assert not isinstance(raised.value, ValueError)
    assert ecs_credential_error_code(raised.value) == ECS_METADATA_UNREACHABLE


def test_the_async_openapi_sdk_wraps_the_credential_failure(fake_ecs_runtime: FakeEcsRuntime) -> None:
    fake_ecs_runtime.fail_with(ValueError(ECS_RAM_ROLE_NOT_FOUND))
    client = _openapi_client(fake_ecs_runtime)
    params, request, runtime = _openapi_call_arguments()

    async def scenario() -> BaseException:
        with pytest.raises(BaseException) as raised:
            await client.call_api_async(params, request, runtime)
        return raised.value

    error = asyncio.run(scenario())

    assert type(error) is UnretryableException
    assert ecs_credential_error_code(error) == ECS_RAM_ROLE_NOT_FOUND


@pytest.mark.parametrize(
    ("sdk_call", "action"),
    [
        (
            lambda client: client.get_stack(
                ros_models.GetStackRequest(stack_id="stack-1", region_id="cn-shanghai")
            ),
            "GetStack",
        ),
        (
            lambda client: client.list_stack_instances(
                ros_models.ListStackInstancesRequest(stack_group_name="demo", region_id="cn-shanghai")
            ),
            "ListStackInstances",
        ),
        (
            lambda client: client.create_stack_instances(
                ros_models.CreateStackInstancesRequest(stack_group_name="demo", region_id="cn-shanghai")
            ),
            "CreateStackInstances",
        ),
    ],
)
def test_ros_sdk_credential_failures_render_public_text(
    fake_ecs_runtime: FakeEcsRuntime, sdk_call, action: str
) -> None:
    fake_ecs_runtime.fail_with(ValueError(ECS_METADATA_UNREACHABLE))
    client = RosClientFactory.create(fake_ecs_runtime.credential(), region_id="cn-shanghai")

    with pytest.raises(BaseException) as raised:
        sdk_call(client)

    assert type(raised.value) is UnretryableException
    message = public_ecs_credential_message(raised.value, action=action, region="cn-shanghai")
    assert message is not None
    assert "instance metadata service is unreachable" in message
    assert action in message
    assert ECS_METADATA_UNREACHABLE not in message


def _public_endpoint_message(error: BaseException | str) -> str:
    """Render `error` the way endpoint discovery does at its final boundary."""
    return public_aliyun_error(error, product="ecs", action="DescribeRegions", region_id="cn-shanghai")


@pytest.mark.parametrize(
    "error",
    [
        ECS_METADATA_UNREACHABLE,
        ValueError(ECS_METADATA_UNREACHABLE),
        ApiContractError(ECS_METADATA_UNREACHABLE),
        _envelope(ValueError(ECS_METADATA_UNREACHABLE)),
    ],
    ids=["explicit_code_string", "direct_value_error", "api_contract_error", "envelope_around_a_value_error"],
)
def test_endpoint_discovery_failures_still_render_public_text(error: BaseException | str) -> None:
    """Endpoint discovery hands the failure straight to the public renderer.

    The renderer applies the same carrier allowlist as the other boundaries, so an
    explicit code and every carrier the credential runtime actually uses reach the ECS
    text. Pin it so a future change there cannot start leaking the raw code.
    """
    message = _public_endpoint_message(error)

    assert "instance metadata service is unreachable" in message
    assert ECS_METADATA_UNREACHABLE not in message


@pytest.mark.parametrize(
    "error",
    [
        RuntimeError(ECS_METADATA_UNREACHABLE),
        Exception(ECS_METADATA_UNREACHABLE),
        _envelope(RuntimeError(ECS_METADATA_UNREACHABLE)),
    ],
    ids=["runtime_error", "bare_exception", "envelope_around_a_runtime_error"],
)
def test_the_public_renderer_ignores_code_shaped_messages_on_other_carriers(error: BaseException) -> None:
    """An unrelated failure must not be reported as an ECS metadata problem."""
    message = _public_endpoint_message(error)

    assert message == (
        "Alibaba Cloud API ecs/DescribeRegions could not be prepared safely. Check the request and try again."
    )
    assert "instance metadata" not in message
    assert ECS_METADATA_UNREACHABLE not in message


def test_unrelated_ros_sdk_failures_stay_unmapped() -> None:
    assert public_ecs_credential_message(RuntimeError("boom"), action="GetStack", region="cn-shanghai") is None
    assert (
        public_ecs_credential_message(
            _envelope(RuntimeError(ECS_METADATA_UNREACHABLE)), action="GetStack", region="cn-shanghai"
        )
        is None
    )
