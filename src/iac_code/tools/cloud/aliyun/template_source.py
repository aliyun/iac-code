"""ROS template source parameter helpers."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from iac_code.i18n import _
from iac_code.tools.path_safety import check_read_path, resolve_read_path
from iac_code.types.permissions import PermissionResult

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

PIPELINE_DEDICATED_ROS_DEPLOYMENT_ACTIONS = frozenset(
    {
        "createstack",
        "continuecreatestack",
        "deletestack",
        "updatestack",
    }
)


def is_remote_template_url(template_url: str) -> bool:
    """Return True when TemplateURL points to a remote URL rather than a local file."""
    return template_url.lower().startswith(REMOTE_TEMPLATE_URL_PREFIXES)


def is_local_template_url(template_url: str) -> bool:
    """Return True when TemplateURL should be resolved as a local file path."""
    return not is_remote_template_url(template_url)


def _read_dirs_from_context(context: Any, name: str) -> list[str]:
    value = getattr(context, name, [])
    return list(value) if isinstance(value, list) else []


def check_local_template_url_read_permission(
    template_url: str,
    context: Any,
    *,
    cwd: str | None = None,
    additional_directories: list[str] | None = None,
    trusted_read_directories: list[str] | None = None,
    relative_read_directories: list[str] | None = None,
    strict_read_directories: list[str] | None = None,
    read_path_violation_behavior: Literal["ask", "deny"] | None = None,
) -> PermissionResult | None:
    """Return a path permission result for local TemplateURL reads, or None when allowed."""
    if not template_url or not is_local_template_url(template_url):
        return None

    decision = check_read_path(
        template_url,
        cwd=cwd or getattr(context, "cwd", "") or ".",
        additional_directories=additional_directories
        if additional_directories is not None
        else _read_dirs_from_context(context, "additional_directories"),
        trusted_read_directories=trusted_read_directories
        if trusted_read_directories is not None
        else _read_dirs_from_context(context, "trusted_read_directories"),
        relative_read_directories=relative_read_directories
        if relative_read_directories is not None
        else _read_dirs_from_context(context, "relative_read_directories"),
        strict_read_directories=strict_read_directories
        if strict_read_directories is not None
        else _read_dirs_from_context(context, "strict_read_directories"),
        read_path_violation_behavior=read_path_violation_behavior
        or getattr(context, "read_path_violation_behavior", "ask"),
    )
    if decision.behavior == "allow":
        return None
    return decision.to_permission_result()


def resolve_template_url_for_api(
    template_url: str,
    context: Any,
    *,
    cwd: str | None = None,
    relative_read_directories: list[str] | None = None,
) -> str:
    """Resolve local TemplateURL paths before passing them to SDK/API callers."""
    if not is_local_template_url(template_url):
        return template_url
    return resolve_read_path(
        template_url,
        cwd or getattr(context, "cwd", "") or ".",
        relative_read_directories=relative_read_directories
        if relative_read_directories is not None
        else _read_dirs_from_context(context, "relative_read_directories"),
    )


def read_local_template_url(
    template_url: str,
    context: Any,
    *,
    cwd: str | None = None,
    relative_read_directories: list[str] | None = None,
) -> str:
    """Read a local TemplateURL after resolving it like other read tools."""
    resolved = resolve_template_url_for_api(
        template_url,
        context,
        cwd=cwd,
        relative_read_directories=relative_read_directories,
    )
    return Path(resolved).read_text(encoding="utf-8")


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


def reject_pipeline_dedicated_ros_deployment_action(action: str, *, pipeline_mode: bool) -> str | None:
    """Return an error when pipeline callers use raw ROS deployment APIs."""
    if not pipeline_mode:
        return None
    if action.lower() not in PIPELINE_DEDICATED_ROS_DEPLOYMENT_ACTIONS:
        return None
    return _(
        "ROS pipeline calls for {action} must use the dedicated ros_deploy tool instead of aliyun_api. "
        "Do not call the raw ROS deployment API directly."
    ).format(action=action)


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
