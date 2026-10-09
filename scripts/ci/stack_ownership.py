"""Read accepted CreateStack receipts from a case's private pipeline ledger.

Names are chosen by the application. Neither a name prefix, a Continue/Wait
operation nor a Stack ID merely mentioned in model output authorizes deletion.
The product writes observed_resources only after ROS accepts CreateStack.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Iterable

import yaml


def _load(path: Path) -> dict[str, Any]:
    if path.is_symlink() or path.stat().st_size > 5_000_000:
        raise ValueError("invalid private Stack ownership evidence")
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("invalid private Stack ownership evidence")
    return data


def creation_receipts(pipeline_dirs: Iterable[Path]) -> list[dict[str, str]]:
    """Callers supply session directories belonging to their isolated case only."""
    resources: dict[str, dict[str, str]] = {}
    for directory in pipeline_dirs:
        ledger_path = directory / "cleanup.yaml"
        if not ledger_path.exists():
            continue
        data = _load(ledger_path)
        meta = _load(directory / "meta.yaml")
        attempt_metadata = meta.get("attempts")
        attempts = attempt_metadata.get("items") if isinstance(attempt_metadata, dict) else None
        if not isinstance(attempts, dict):
            raise ValueError("missing pipeline attempt ownership")
        values = data.get("observed_resources", [])
        if not isinstance(values, list):
            raise ValueError("invalid observed Stack ledger")
        for item in values:
            if not isinstance(item, dict):
                raise ValueError("invalid observed Stack ledger")
            if (item.get("provider") != "ros" or item.get("resource_type") != "stack"
                    or item.get("observed_action") != "CreateStack"):
                continue
            fields = ("resource_id", "resource_name", "region_id", "source_step_id", "source_attempt_id")
            metadata = item.get("metadata")
            if (any(not isinstance(item.get(key), str) or not item[key] for key in fields)
                    or not isinstance(metadata, dict) or not metadata.get("tool_use_id")
                    or metadata.get("tool_name") not in {"ros_stack", "ros_deploy", "aliyun_api"}):
                raise ValueError("incomplete accepted Stack creation receipt")
            if re.fullmatch(r"[A-Za-z0-9_-]{6,128}", item["resource_id"]) is None:
                raise ValueError("invalid accepted Stack identity")
            attempt = attempts.get(item["source_attempt_id"])
            if not isinstance(attempt, dict) or attempt.get("step_id") != item["source_step_id"]:
                raise ValueError("Stack creation attempt does not belong to this pipeline")
            resource = {
                "provider": "ros", "resourceType": "stack", "stackId": item["resource_id"],
                "stackName": item["resource_name"], "regionId": item["region_id"],
                "createdByCase": "true", "ownershipSource": "accepted_create_ledger",
            }
            previous = resources.setdefault(resource["stackId"], resource)
            if previous != resource:
                raise ValueError("conflicting Stack creation receipts")
    if len(resources) > 60:
        raise ValueError("Stack creation receipt count exceeds case bound")
    return list(resources.values())


def case_pipeline_dirs(config_dir: Path, cwd: str) -> list[Path]:
    """Resolve project aliases without consulting the agent's local configuration."""
    from iac_code.services.session_storage import SessionStorage

    projects = config_dir / "projects"
    if not projects.exists():
        return []
    storage = SessionStorage(projects_dir=projects)
    result: list[Path] = []
    for project in storage.project_read_dirs(cwd):
        for pattern in ("*/pipeline", "*/a2a/pipeline"):
            for directory in sorted(project.glob(pattern)):
                if directory.resolve().is_relative_to(projects.resolve()):
                    result.append(directory)
                else:
                    raise ValueError("Stack ownership evidence escaped isolated case")
    return result
