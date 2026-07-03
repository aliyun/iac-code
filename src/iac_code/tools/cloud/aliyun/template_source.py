"""ROS template source parameter helpers."""

from __future__ import annotations

from typing import Any

from iac_code.i18n import _

REMOTE_TEMPLATE_URL_PREFIXES = ("http://", "https://", "oss://")

PIPELINE_TEMPLATE_URL_ACTIONS = frozenset(
    {
        "CreateChangeSet",
        "CreateStack",
        "CreateStackGroup",
        "GetTemplateEstimateCost",
        "GetTemplateParameterConstraints",
        "GetTemplateSummary",
        "PreviewStack",
        "UpdateStack",
        "UpdateStackGroup",
        "ValidateTemplate",
    }
)

PIPELINE_DEDICATED_ROS_TEMPLATE_TOOLS = {
    "validatetemplate": "ros_validate_template",
    "gettemplateparameterconstraints": "ros_get_template_parameter_constraints",
    "previewstack": "ros_preview_template",
    "gettemplateestimatecost": "ros_estimate_template_cost",
}


def is_remote_template_url(template_url: str) -> bool:
    """Return True when TemplateURL points to a remote URL rather than a local file."""
    return template_url.lower().startswith(REMOTE_TEMPLATE_URL_PREFIXES)


def reject_pipeline_dedicated_ros_template_action(action: str, *, pipeline_mode: bool) -> str | None:
    """Return an error when pipeline callers use raw ROS template APIs."""
    if not pipeline_mode:
        return None
    tool_name = PIPELINE_DEDICATED_ROS_TEMPLATE_TOOLS.get(action.lower())
    if tool_name is None:
        return None
    return _(
        "ROS pipeline calls for {action} must use the dedicated {tool_name} tool instead of aliyun_api. "
        "Do not call the raw ROS template API directly."
    ).format(action=action, tool_name=tool_name)


def reject_template_body_param(params: dict[str, Any], *, pipeline_mode: bool) -> str | None:
    """Return an error message when a caller provides TemplateBody directly."""
    if not pipeline_mode or "TemplateBody" not in params:
        return None
    return _(
        "ROS template calls must use TemplateURL instead of TemplateBody. "
        "Save the template to a file and pass params.TemplateURL, for example a local file path or OSS/HTTP URL."
    )


def reject_pipeline_template_source_params(action: str, params: dict[str, Any], *, pipeline_mode: bool) -> str | None:
    """Return an error message when pipeline ROS template calls do not use TemplateURL."""
    if not pipeline_mode or action not in PIPELINE_TEMPLATE_URL_ACTIONS:
        return None
    if error := reject_template_body_param(params, pipeline_mode=pipeline_mode):
        return error
    if "TemplateURL" in params:
        return None
    return _(
        "ROS pipeline calls for {action} must pass params.TemplateURL. "
        "Save the template to a file and pass params.TemplateURL, for example a local file path or OSS/HTTP URL."
    ).format(action=action)
