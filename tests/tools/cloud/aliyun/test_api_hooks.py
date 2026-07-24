"""Tests for api_hooks before_call decorator enhancement."""

from __future__ import annotations

from iac_code.tools.base import ToolContext
from iac_code.tools.cloud.aliyun.api_hooks import _ensure_loaded, _hooks, before_call, run_hooks
from iac_code.tools.cloud.aliyun.ros_validation.model import (
    Category,
    Severity,
    ValidationReport,
    make_diagnostic,
)
from iac_code.tools.cloud.aliyun.ros_validation.outcome import outcome_from_report


class TestBeforeCallList:
    def setup_method(self) -> None:
        _ensure_loaded()
        self._saved = dict(_hooks)
        _hooks.clear()

    def teardown_method(self) -> None:
        _hooks.clear()
        _hooks.update(self._saved)

    def test_single_action_str(self) -> None:
        @before_call("ros", "ValidateTemplate")
        def hook(product, action, params):
            return None

        assert ("ros", "ValidateTemplate") in _hooks
        assert hook in _hooks[("ros", "ValidateTemplate")]

    def test_action_list(self) -> None:
        @before_call("ros", ["CreateStack", "UpdateStack"])
        def hook(product, action, params):
            return None

        assert hook in _hooks[("ros", "CreateStack")]
        assert hook in _hooks[("ros", "UpdateStack")]

    def test_action_list_single_fn_instance(self) -> None:
        @before_call("ros", ["CreateStack", "UpdateStack", "PreviewStack"])
        def hook(product, action, params):
            return None

        assert _hooks[("ros", "CreateStack")][0] is _hooks[("ros", "UpdateStack")][0]
        assert _hooks[("ros", "UpdateStack")][0] is _hooks[("ros", "PreviewStack")][0]

    def test_run_hooks_with_list_registered(self) -> None:
        from iac_code.tools.base import ToolResult

        @before_call("ros", ["ActionA", "ActionB"])
        def hook(product, action, params):
            if params.get("fail"):
                return ToolResult.error("blocked")
            return None

        result = run_hooks("ros", "ActionA", {"fail": True})
        assert result is not None
        assert result.is_error

        result = run_hooks("ros", "ActionB", {})
        assert result is None

        result = run_hooks("ros", "ActionC", {})
        assert result is None

    def test_read_only_hook_chain_cannot_mutate_bound_params(self) -> None:
        @before_call("ros", "ReadOnly")
        def mutating_hook(product, action, params):
            params.pop("Parameters")
            params["Parameters.1.ParameterKey"] = "P"
            return None

        params = {"Parameters": {"P": "v"}}
        assert run_hooks("ros", "ReadOnly", params, read_only=True) is None
        assert params == {"Parameters": {"P": "v"}}

    def test_ros_outcome_is_kept_per_context_and_only_errors_block(self) -> None:
        warning = make_diagnostic(
            code="ROSW",
            severity=Severity.WARNING,
            category=Category.QUALITY,
            summary="warning",
            detail="warning",
        )
        error = make_diagnostic(
            code="ROSE",
            severity=Severity.ERROR,
            category=Category.COMPATIBILITY,
            summary="error",
            detail="error",
        )

        @before_call("ros", "Warn")
        def warning_hook(product, action, params):
            return outcome_from_report(ValidationReport.build([warning]))

        @before_call("ros", "Block")
        def error_hook(product, action, params):
            return outcome_from_report(ValidationReport.build([error]))

        warning_context = ToolContext()
        blocking_context = ToolContext()
        assert run_hooks("ros", "Warn", {}, context=warning_context) is None
        blocking = run_hooks("ros", "Block", {}, context=blocking_context)

        assert warning_context.ros_preflight_outcome.report.diagnostics[0].code == "ROSW"
        assert blocking_context.ros_preflight_outcome.report.diagnostics[0].code == "ROSE"
        assert blocking is not None and blocking.is_error
        assert blocking.metadata["ros_validation"]["diagnostics"][0]["code"] == "ROSE"


def test_real_ros_stage_zero_hook_chain_is_read_only_and_preserves_parameter_semantics() -> None:
    preview_params = {"StackId": "stack", "Parameters": {"P": "v"}}
    assert run_hooks("ros", "PreviewStack", preview_params, context=ToolContext(), read_only=True) is None
    assert preview_params == {"StackId": "stack", "Parameters": {"P": "v"}}

    group_params = {
        "StackArn": "acs:ros:cn-hangzhou:123:stack/demo/id",
        "PermissionModel": "SELF_MANAGED",
        "Parameters": {"P": "v"},
    }
    result = run_hooks("ros", "CreateStackGroup", group_params, context=ToolContext())
    assert result is not None and result.is_error
    assert "cannot provide Parameters when StackArn is used" in result.content
