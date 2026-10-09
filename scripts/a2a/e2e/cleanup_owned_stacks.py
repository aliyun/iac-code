#!/usr/bin/env python3
"""Delete only ROS stacks with an accepted creation receipt in this E2E case."""

from __future__ import annotations

import argparse
import json
import re
import time
from pathlib import Path
from typing import Any


class CleanupOperationError(RuntimeError):
    """Expose a fixed cleanup stage without leaking the cloud error message."""

    def __init__(self, stage: str, cause: Exception) -> None:
        self.stage = stage
        self.cause_type = type(cause).__name__
        code = getattr(cause, "code", None)
        self.sdk_code = code if isinstance(code, str) and re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]{0,79}", code) else ""
        super().__init__(f"{stage}: {self.cause_type}")


def _record_cleanup_failure(run_dir: Path, stage: str, exc: Exception) -> None:
    known_codes = {
        "EntityNotExist.Stack", "NotFound.Stack", "StackNotFound", "ActionInProgress",
        "Forbidden", "Forbidden.RAM", "InvalidAccessKeyId.NotFound", "SecurityTokenExpired",
        "Throttling", "Throttling.User", "InvalidParameter", "DeleteFailed", "DependencyViolation",
    }
    code = getattr(exc, "code", None)
    diagnostic = {"stage": stage, "errorType": "SDKError",
                  "code": code if isinstance(code, str) and code in known_codes else "unknown"}
    with (run_dir / "cleanup-cloud.log").open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({"cleanupDiagnostic": diagnostic}) + "\n")


def _stack_body(client: Any, models: Any, stack_id: str, region: str) -> dict[str, Any] | None:
    try:
        return client.get_stack(models.GetStackRequest(stack_id=stack_id, region_id=region)).body.to_map()
    except Exception as exc:
        code = getattr(exc, "code", None)
        missing_codes = {"EntityNotExist.Stack", "NotFound.Stack", "StackNotFound"}
        if isinstance(code, str):
            if code in missing_codes:
                return None
        elif any(re.search(r"\b" + re.escape(marker) + r"\b", str(exc), re.I) for marker in missing_codes):
            return None
        raise


def cleanup_owned_stacks(run_dir: Path, *, timeout: float = 840) -> dict[str, Any]:
    from iac_code.services.session_storage import SessionStorage
    from scripts.ci.stack_ownership import creation_receipts

    manifest = json.loads((run_dir / "owned-stacks.json").read_text(encoding="utf-8"))
    if not isinstance(manifest.get("configDir"), str) or not isinstance(manifest.get("cwd"), str):
        raise ValueError("invalid E2E ownership manifest")
    storage = SessionStorage(projects_dir=Path(manifest["configDir"]) / "projects")
    directories: list[Path] = []
    for context_path in sorted((run_dir / "a2a-persistence" / "contexts").glob("*.json")):
        context = json.loads(context_path.read_text(encoding="utf-8"))
        session_id = context.get("session_id")
        if (not isinstance(session_id, str) or re.fullmatch(r"[A-Za-z0-9_-]{1,128}", session_id) is None
                or context.get("cwd") != manifest["cwd"]):
            raise ValueError("A2A context does not prove this case's session ownership")
        session = storage.session_dir(manifest["cwd"], session_id)
        if not session.resolve().is_relative_to((Path(manifest["configDir"]) / "projects").resolve()):
            raise ValueError("Stack ownership evidence escaped isolated case")
        directories.extend([session / "pipeline", session / "a2a" / "pipeline"])
    resources = creation_receipts(directories)
    from alibabacloud_ros20190910 import models as ros_models

    from iac_code.services.cloud_credentials import CloudCredentials
    from iac_code.tools.cloud.aliyun.ros_client import RosClientFactory

    try:
        credential = CloudCredentials().get_provider("aliyun")
    except Exception as exc:
        raise CleanupOperationError("credential_lookup", exc) from exc
    if credential is None:
        raise RuntimeError("Aliyun credential is unavailable for E2E teardown")
    region = str(manifest.get("regionId") or credential.region_id)
    try:
        client = RosClientFactory.create(credential, region)
    except Exception as exc:
        raise CleanupOperationError("client_create", exc) from exc
    deadline = time.monotonic() + timeout
    deleted: list[str] = []
    remaining: list[str] = []
    failures: list[str] = []
    for resource in resources:
        stack_id, name, region = resource["stackId"], resource["stackName"], resource["regionId"]
        client = RosClientFactory.create(credential, region)
        delete_submitted = False
        while time.monotonic() < deadline:
            stage = "get_stack"
            try:
                body = _stack_body(client, ros_models, stack_id, region)
                if body is None or body.get("Status") == "DELETE_COMPLETE":
                    deleted.append(stack_id)
                    break
                if body.get("StackName") != name or body.get("ParentStackId") or body.get("ServiceManaged"):
                    failures.append(stack_id + ": Stack identity differs from accepted creation receipt")
                    break
                status = str(body.get("Status") or "")
                if status == "DELETE_FAILED" and delete_submitted:
                    failures.append(stack_id + ": accepted deletion failed")
                    break
                if not status.endswith("_IN_PROGRESS") and not delete_submitted:
                    stage = "delete_stack"
                    try:
                        client.delete_stack(ros_models.DeleteStackRequest(stack_id=stack_id, region_id=region))
                        delete_submitted = True
                    except Exception as exc:
                        if getattr(exc, "code", "") in {"EntityNotExist.Stack", "NotFound.Stack", "StackNotFound"}:
                            deleted.append(stack_id)
                            break
                        if getattr(exc, "code", "") != "ActionInProgress":
                            raise
                time.sleep(min(5, max(0, deadline - time.monotonic())))
            except Exception as exc:
                _record_cleanup_failure(run_dir, stage, exc)
                failures.append(stack_id + ": " + type(exc).__name__)
                break
        else:
            failures.append(stack_id + ": cleanup timeout")
        if stack_id not in deleted:
            remaining.append(stack_id)
    # Real pipeline Stack events can reveal an unproven leak. Read those exact
    # IDs without granting deletion authority to observations alone.
    from scripts.a2a.debugger import _extract_pipeline_envelopes

    observed_ids: set[str] = set()
    for path in sorted(run_dir.glob("*.events.jsonl"))[:60]:
        if path.stat().st_size > 20_000_000:
            raise RuntimeError("observed Stack audit evidence exceeds bounded size")
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            for envelope in _extract_pipeline_envelopes(row):
                data = envelope.get("data")
                if envelope.get("eventType") != "stack_current_changed" or not isinstance(data, dict):
                    continue
                stack_id = data.get("stackId")
                if isinstance(stack_id, str) and stack_id:
                    observed_ids.add(stack_id)
    if len(observed_ids) > 60:
        raise RuntimeError("observed Stack audit exceeds bounded count")
    try:
        for stack_id in sorted(observed_ids - set(deleted) - set(remaining)):
            body = _stack_body(client, ros_models, stack_id, region)
            if body is not None and body.get("Status") != "DELETE_COMPLETE":
                failures.append("observed Stack has no accepted creation receipt or cleanup incomplete")
                remaining.append(stack_id)
    except Exception as exc:
        raise CleanupOperationError("observed_stack_audit", exc) from exc
    result = {
        "status": "failed" if failures or remaining else "completed",
        "deletedStackIds": deleted,
        "remainingStackIds": remaining,
        "failures": failures,
        "resources": resources,
    }
    (run_dir / "cleanup-result.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--timeout", type=float, default=840)
    args = parser.parse_args()
    result = cleanup_owned_stacks(args.run_dir, timeout=args.timeout)
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["status"] == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
