"""ROS template source parameter helpers."""

from __future__ import annotations

from typing import Any

from iac_code.i18n import _


def reject_template_body_param(params: dict[str, Any], *, pipeline_mode: bool) -> str | None:
    """Return an error message when a caller provides TemplateBody directly."""
    if not pipeline_mode or "TemplateBody" not in params:
        return None
    return _(
        "ROS template calls must use TemplateURL instead of TemplateBody. "
        "Save the template to a file and pass params.TemplateURL, for example a local file path or OSS/HTTP URL."
    )
