"""Scoped OPS environmental pixels; never a formal scene or cloud authority."""

from pathlib import Path
from typing import Any

from .files import WorkflowError, contained, digest, read_json


def validate(root: Path, task_id: str, image: dict[str, Any], diagnostic: dict[str, Any]) -> None:
    image_path = contained(root, image["path"])
    source_path = contained(root, image["source_evidence"])
    relative = image_path.relative_to(root).parts
    if (
        len(relative) != 3 or relative[0] != "inputs" or not relative[1].startswith("ops-domestic-")
        or source_path.parent != image_path.parent or source_path.name != "observation.json"
        or diagnostic.get("kind") != "ops_current_console_environment_diagnostic"
        or diagnostic.get("approved") is not True or diagnostic.get("task_id") != task_id
        or diagnostic.get("evidence_role") != "current_console_environment"
        or diagnostic.get("eligible_for_verdict") is not False
        or diagnostic.get("captured_by") != "ops_not_original_glm"
        or not diagnostic.get("limitations")
        or diagnostic.get("host") != "home.console.aliyun.com"
    ):
        raise WorkflowError("environment pixels require exact task OPS diagnostic-only binding")
    source = read_json(source_path)
    if (
        source.get("kind") not in {"ops_domestic_account_menu_snapshot", "ops_rendered_domestic_login_snapshot"}
        or source.get("host") != diagnostic["host"]
        or not source.get("target") or source["target"] != diagnostic.get("target")
        or source.get("screenshot") != image["path"]
        or source.get("screenshot_sha256") != image["sha256"]
        or digest(source_path) != image["source_sha256"]
        or source.get("cloud_api_calls_performed") is not False
        or source.get("resource_link_clicks_performed") is not False
        or source.get("pass_added") is not False
    ):
        raise WorkflowError("environment original capture provenance changed or claims formal execution")
