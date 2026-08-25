"""Tests for the ROS stack identifier pre-call hook."""

from __future__ import annotations

import pytest

from iac_code.tools.base import ToolContext
from iac_code.tools.cloud.aliyun.api_hooks import run_hooks
from iac_code.tools.cloud.aliyun.hooks.ros_stack_identifier import (
    STACK_ID_REQUIRED_ACTIONS,
    check_stack_identifier,
)


@pytest.mark.parametrize("action", STACK_ID_REQUIRED_ACTIONS)
def test_stack_name_only_is_blocked_for_every_stack_id_action(action: str) -> None:
    outcome = check_stack_identifier("ros", action, {"StackName": "demo-stack", "RegionId": "cn-hangzhou"})

    assert outcome is not None
    assert outcome.is_error
    diagnostic = outcome.report.diagnostics[0]
    assert diagnostic.code == "ROS1203"
    assert diagnostic.expected == "StackId"
    assert "ListStacks" in (diagnostic.suggestion or "")


def test_missing_both_identifiers_is_blocked_with_a_distinct_reason() -> None:
    outcome = check_stack_identifier("ros", "GetStack", {"RegionId": "cn-hangzhou"})

    assert outcome is not None and outcome.is_error
    assert "no stack identifier" in outcome.report.diagnostics[0].summary


def test_blank_stack_id_counts_as_missing() -> None:
    outcome = check_stack_identifier("ros", "GetStack", {"StackId": "   ", "StackName": "demo-stack"})

    assert outcome is not None and outcome.is_error


def test_present_stack_id_passes() -> None:
    assert check_stack_identifier("ros", "GetStack", {"StackId": "stack-id", "StackName": "demo-stack"}) is None


def test_stage_zero_chain_blocks_get_stack_by_name_without_mutating_params() -> None:
    params = {"StackName": "it-s03-20260728-192023-f2a57221", "RegionId": "cn-hangzhou"}
    context = ToolContext()

    result = run_hooks("ros", "GetStack", params, context=context, read_only=True)

    assert result is not None and result.is_error
    assert "ROS1203" in result.content
    assert "ListStacks" in result.content
    assert params == {"StackName": "it-s03-20260728-192023-f2a57221", "RegionId": "cn-hangzhou"}
    assert context.ros_preflight_outcome is not None


def test_stage_zero_chain_allows_list_stacks_by_name() -> None:
    params = {"StackName": "it-s03-20260728-192023-f2a57221", "RegionId": "cn-hangzhou"}

    assert run_hooks("ros", "ListStacks", params, context=ToolContext(), read_only=True) is None


def test_stage_zero_chain_allows_get_stack_with_stack_id() -> None:
    params = {"StackId": "stack-id", "RegionId": "cn-hangzhou"}

    assert run_hooks("ros", "GetStack", params, context=ToolContext(), read_only=True) is None
