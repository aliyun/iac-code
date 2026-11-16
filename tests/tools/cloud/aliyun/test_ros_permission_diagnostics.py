"""Offline regressions for the ROS VPC Forbidden.RAM failure observed after rollback."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import MagicMock, patch

import pytest
from Tea.exceptions import TeaException

from iac_code.services.providers.aliyun import AliyunCredential, use_aliyun_credential
from iac_code.tools.base import ToolContext
from iac_code.tools.cloud.aliyun.ros_stack import RosStack

TEMPLATE = {
    "ROSTemplateFormatVersion": "2015-09-01",
    "Resources": {"Vpc": {"Type": "ALIYUN::ECS::VPC", "Properties": {"CidrBlock": "10.254.0.0/16"}}},
}
RESOURCE_REASON = (
    "ResponseException: resources.Vpc: User not authorized to operate on the specified resource, "
    "or this API doesn't support RAM. Code: Forbidden.RAM RequestId: test-resource-request-id"
)
STACK_REASON = f"Resource CREATE failed: {RESOURCE_REASON}"
REQUIRED_ACTIONS = ["vpc:AssociateVpcCidrBlock", "vpc:CreateVpc", "vpc:DeleteVpc", "vpc:DescribeVpcs"]


def permission_failure_client() -> MagicMock:
    client = MagicMock()
    client.get_stack.return_value.body.to_map.return_value = {
        "StackId": "stack-test",
        "StackName": "test-vpc",
        "Status": "CREATE_ROLLBACK_COMPLETE",
        "StatusReason": STACK_REASON,
    }
    # The actual resource has already been rolled back and no longer has its error.
    client.list_stack_resources.return_value.body.to_map.return_value = {
        "Resources": [{"LogicalResourceId": "Vpc", "ResourceType": "ALIYUN::ECS::VPC", "Status": "INIT_COMPLETE"}]
    }
    client.list_stack_events_with_options.return_value.body.to_map.return_value = {
        "StackEvents": [
            {
                "LogicalResourceId": "test-vpc",
                "ResourceType": "ALIYUN::ROS::Stack",
                "Status": "CREATE_FAILED",
                "StatusReason": STACK_REASON,
            },
            {
                "LogicalResourceId": "Vpc",
                "ResourceType": "ALIYUN::ECS::VPC",
                "Status": "CREATE_FAILED",
                "StatusReason": RESOURCE_REASON,
            },
            {
                "LogicalResourceId": "OldResource",
                "ResourceType": "ALIYUN::ECS::Instance",
                "Status": "CREATE_FAILED",
                "StatusReason": "Forbidden.RAM RequestId: old-request-id",
            },
        ]
    }
    client.generate_template_policy_with_options.return_value.body.to_map.return_value = {
        "RequestId": "test-policy-request-id",
        "Policy": {"Version": "1", "Statement": [{"Effect": "Allow", "Action": REQUIRED_ACTIONS, "Resource": "*"}]},
    }
    client.get_template_with_options.return_value.body.to_map.return_value = {"TemplateBody": json.dumps(TEMPLATE)}
    return client


async def wait_for_failure(client: MagicMock, action: str = "CreateStack", params: dict | None = None):
    tool = RosStack()
    tool.poll_interval = 0
    # No local credentials or real network; all reads use this same effective client.
    with patch.object(tool, "_get_client", return_value=client):
        return await tool.wait_for_stack_operation(
            action,
            {"TemplateBody": TEMPLATE} if params is None else params,
            "cn-hangzhou",
            "stack-test",
            ToolContext(),
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["CreateStack", "ContinueCreateStack", "UpdateStack", "DeleteStack"])
async def test_vpc_rollback_exposes_template_requirements_without_claiming_missing_actions(action: str) -> None:
    client = permission_failure_client()
    terminal = {"UpdateStack": "ROLLBACK_COMPLETE", "DeleteStack": "DELETE_FAILED"}.get(
        action, "CREATE_ROLLBACK_COMPLETE"
    )
    client.get_stack.return_value.body.to_map.return_value["Status"] = terminal
    result = await wait_for_failure(client, action)

    data = json.loads(result.content)
    assert result.is_error
    assert data["status_reason"] == STACK_REASON
    assert data["permission_diagnostics"]["error_request_id"] == "test-resource-request-id"
    assert data["permission_diagnostics"]["denied_action_status"] == "unknown"
    policy = data["permission_diagnostics"]["template_policy"]
    assert policy["required_actions"] == REQUIRED_ACTIONS
    assert policy["meaning"] == "template_requirements_not_confirmed_missing_permissions"
    assert policy["source"] == "ros:GenerateTemplatePolicy"
    assert policy["request_id"] == "test-policy-request-id"
    assert "ecs:CreateVpc" not in result.content
    assert data["failed_resources"] == [
        {
            "name": "Vpc",
            "resource_type": "ALIYUN::ECS::VPC",
            "status": "CREATE_FAILED",
            "status_reason": RESOURCE_REASON,
            "source": "ros:ListStackEvents",
        }
    ]
    request, runtime = client.generate_template_policy_with_options.call_args.args
    assert json.loads(request.template_body) == TEMPLATE
    assert request.operation_types == ["CreateStack" if action == "ContinueCreateStack" else action]
    assert runtime.autoretry is False
    assert runtime.connect_timeout == runtime.read_timeout == 2000
    client.get_template_with_options.assert_not_called()


@pytest.mark.asyncio
async def test_failed_resource_is_not_duplicated_when_rollback_is_disabled() -> None:
    client = permission_failure_client()
    client.get_stack.return_value.body.to_map.return_value["Status"] = "CREATE_FAILED"
    client.list_stack_resources.return_value.body.to_map.return_value["Resources"][0].update(
        Status="CREATE_FAILED", StatusReason=RESOURCE_REASON
    )
    result = await wait_for_failure(client)
    resources = json.loads(result.content)["failed_resources"]
    assert len(resources) == 1
    assert resources[0]["name"] == "Vpc"
    assert resources[0]["status_reason"] == RESOURCE_REASON


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure", [PermissionError("query denied"), TimeoutError("query timeout"), ValueError("bad data")]
)
async def test_optional_queries_cannot_mask_original_permission_failure(failure: Exception) -> None:
    client = permission_failure_client()
    client.list_stack_events_with_options.side_effect = failure
    client.generate_template_policy_with_options.side_effect = failure
    result = await wait_for_failure(client)
    data = json.loads(result.content)
    assert result.is_error
    assert data["status_reason"] == STACK_REASON
    assert data["permission_diagnostics"]["denied_action_status"] == "unknown"
    assert data["permission_diagnostics"]["template_policy"]["status"] == "unavailable"
    assert "exact denied action is unknown" in data["permission_diagnostics"]["explanation"]
    assert "required_actions" not in data["permission_diagnostics"]["template_policy"]
    assert str(failure) not in result.content
    assert "ecs:CreateVpc" not in result.content


@pytest.mark.asyncio
async def test_resumed_operation_reads_existing_template_with_same_client() -> None:
    client = permission_failure_client()
    result = await wait_for_failure(client, params={"StackId": "stack-test"})
    assert json.loads(result.content)["permission_diagnostics"]["template_policy"]["status"] == "available"
    request = client.get_template_with_options.call_args.args[0]
    assert request.stack_id == "stack-test"
    assert request.region_id == "cn-hangzhou"
    assert request.template_stage == "Original"


@pytest.mark.asyncio
async def test_diagnostic_reads_use_a2a_request_credential(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path))
    credential = AliyunCredential(
        mode="StsToken",
        access_key_id="fake-ak",
        access_key_secret="fake-secret",
        sts_token="fake-token",
        region_id="cn-hangzhou",
    )
    client = permission_failure_client()
    tool = RosStack()
    tool.poll_interval = 0
    with (
        use_aliyun_credential(credential),
        patch("iac_code.tools.cloud.aliyun.ros_stack.RosClientFactory.create", return_value=client) as create_client,
    ):
        result = await tool.wait_for_stack_operation(
            "CreateStack", {"TemplateBody": TEMPLATE}, "cn-hangzhou", "stack-test", ToolContext()
        )
    assert json.loads(result.content)["permission_diagnostics"]["template_policy"]["status"] == "available"
    assert create_client.call_count == 3  # status, resources, then optional diagnostics
    assert all(call.args[0] is credential for call in create_client.call_args_list)


@pytest.mark.asyncio
@pytest.mark.parametrize("status,reason", [("CREATE_COMPLETE", ""), ("CREATE_ROLLBACK_COMPLETE", "QuotaExceeded")])
async def test_success_and_non_permission_failures_do_not_query_policies(status: str, reason: str) -> None:
    client = permission_failure_client()
    client.get_stack.return_value.body.to_map.return_value.update(Status=status, StatusReason=reason)
    result = await wait_for_failure(client)
    assert "permission_diagnostics" not in json.loads(result.content)
    client.list_stack_events_with_options.assert_not_called()
    client.generate_template_policy_with_options.assert_not_called()


@pytest.mark.asyncio
async def test_diagnostic_cancellation_is_propagated() -> None:
    client = permission_failure_client()
    client.list_stack_events_with_options.side_effect = asyncio.CancelledError
    with pytest.raises(asyncio.CancelledError):
        await wait_for_failure(client)
    client.generate_template_policy_with_options.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("location", ["data", "accessDeniedDetail"])
async def test_create_stack_preserves_explicit_denied_action_without_private_diagnostics(location: str) -> None:
    detail = {
        "AuthAction": "vpc:CreateVpc",
        "NoPermissionType": "ExplicitDeny",
        "AuthPrincipalOwnerId": "private-owner",
        "EncodedDiagnosticMessage": "private-diagnostic",
    }
    error = TeaException(
        {
            "code": "Forbidden.RAM",
            "message": "not authorized",
            location: {"AccessDeniedDetail": detail} if location == "data" else detail,
        }
    )
    client = MagicMock()
    client.create_stack.side_effect = error
    tool = RosStack()
    with (
        patch.object(tool, "_get_client", return_value=client),
        patch("iac_code.tools.cloud.aliyun.api_hooks.run_hooks", return_value=None),
    ):
        result = await tool.execute(
            tool_input={
                "action": "CreateStack",
                "region_id": "cn-hangzhou",
                "params": {"StackName": "test", "TemplateBody": TEMPLATE},
            },
            context=ToolContext(),
        )
    assert result.is_error
    assert "Forbidden.RAM" in result.content
    assert "not authorized" in result.content
    assert '"AuthAction": "vpc:CreateVpc"' in result.content
    assert "private" not in result.content
