from __future__ import annotations

from iac_code.tools.cloud.aliyun.hooks.ros_identifiers import check_identifiers
from iac_code.tools.cloud.aliyun.ros_validation.action_policy import validate_action_request
from iac_code.tools.cloud.aliyun.ros_validation.identifier_policy import (
    IDENTIFIER_POLICIES,
    validate_identifier_request,
    validate_template_id_shape,
)
from iac_code.tools.cloud.aliyun.ros_validation.model import Severity


def test_get_template_is_covered_by_the_identifier_policy_layer() -> None:
    policy = IDENTIFIER_POLICIES["GetTemplate"]
    assert policy.identifier_fields == frozenset({"ChangeSetId", "StackGroupName", "StackId", "TemplateId"})


def test_get_template_without_any_identifier_is_blocked_locally() -> None:
    diagnostics = validate_identifier_request("GetTemplate", {})
    assert [item.code for item in diagnostics] == ["ROS1201"]
    diagnostic = diagnostics[0]
    assert diagnostic.severity == Severity.ERROR
    assert "TemplateId" in diagnostic.detail
    assert "StackId" in diagnostic.detail
    assert diagnostic.suggestion is not None
    assert "page context" in diagnostic.suggestion
    assert "displayed template name" in diagnostic.suggestion


def test_get_template_with_exactly_one_identifier_passes() -> None:
    assert validate_identifier_request("GetTemplate", {"StackId": "stack-1"}) == []


def test_get_template_with_conflicting_identifiers_is_blocked_locally() -> None:
    diagnostics = validate_identifier_request(
        "GetTemplate",
        {"StackId": "stack-1", "TemplateId": "5ecd1e10-b0e9-4389-a565-e4c48b1c1234"},
    )
    assert [item.code for item in diagnostics] == ["ROS1201"]
    assert diagnostics[0].severity == Severity.ERROR


def test_template_display_name_used_as_template_id_is_blocked_locally() -> None:
    diagnostics = validate_template_id_shape("GetTemplate", {"TemplateId": "My Web Stack"})
    assert [item.code for item in diagnostics] == ["ROS1201"]
    diagnostic = diagnostics[0]
    assert diagnostic.severity == Severity.ERROR
    assert diagnostic.suggestion is not None
    assert "ListTemplates" in diagnostic.suggestion

    non_ascii = validate_template_id_shape("GetTemplate", {"TemplateId": "网站模板"})
    assert [item.severity for item in non_ascii] == [Severity.ERROR]


def test_opaque_template_id_values_are_accepted() -> None:
    assert validate_template_id_shape("GetTemplate", {"TemplateId": "5ecd1e10-b0e9-4389-a565-e4c48b1c1234"}) == []
    assert validate_template_id_shape("GetTemplate", {"TemplateId": "template-id"}) == []


def test_identifier_hook_blocks_get_template_and_stays_silent_when_valid() -> None:
    outcome = check_identifiers("ros", "GetTemplate", {})
    assert outcome is not None
    assert outcome.blocking_result is not None
    assert outcome.blocking_result.is_error

    assert check_identifiers("ros", "GetTemplate", {"StackId": "stack-1"}) is None


def test_get_template_estimate_cost_without_source_reports_actionable_suggestion() -> None:
    _, diagnostics, _active = validate_action_request("GetTemplateEstimateCost", {})
    errors = [item for item in diagnostics if item.severity == Severity.ERROR]
    assert [item.code for item in errors] == ["ROS1201"]
    suggestion = errors[0].suggestion
    assert suggestion is not None
    assert "TemplateURL" in suggestion
    assert "TemplateScratchId" in suggestion
    assert "displayed template name" in suggestion


def test_template_body_actions_also_reject_a_display_name_template_id() -> None:
    from iac_code.tools.cloud.aliyun.hooks.ros_validate import check_template

    outcome = check_template("ros", "GetTemplateEstimateCost", {"TemplateId": "My Web Stack"})
    assert outcome is not None
    assert outcome.blocking_result is not None
    assert any(
        item["code"] == "ROS1201" and "display name" in item["summary"]
        for item in outcome.report.to_dict()["diagnostics"]
    )
