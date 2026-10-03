#!/usr/bin/env python3
"""Delete only ROS stacks whose exact, run-scoped names belong to one E2E case."""

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


def _stack_body(client: Any, models: Any, stack_id: str, region: str) -> dict[str, Any] | None:
    try:
        return client.get_stack(models.GetStackRequest(stack_id=stack_id, region_id=region)).body.to_map()
    except Exception as exc:
        message = str(exc).lower()
        if any(marker in message for marker in ("stacknotfound", "notfound.stack", "stack not found")):
            return None
        raise


def _named_stacks(client: Any, models: Any, name: str, region: str, *, prefix: bool = False) -> list[Any]:
    stacks: list[Any] = []
    page = 1
    page_size = 50
    while page <= 20:
        response = client.list_stacks(
            models.ListStacksRequest(region_id=region, stack_name=[name + "*" if prefix else name],
                                    page_number=page, page_size=page_size)
        ).body
        batch = response.stacks or []
        stacks.extend(item for item in batch if (
            str(item.stack_name or "").startswith(name) if prefix else item.stack_name == name
        ))
        if len(batch) < page_size:
            return stacks
        page += 1
    raise RuntimeError("E2E Stack lookup exceeded bounded pagination")


def cleanup_owned_stacks(run_dir: Path, *, timeout: float = 840) -> dict[str, Any]:
    manifest = json.loads((run_dir / "owned-stacks.json").read_text(encoding="utf-8"))
    run_id = manifest.get("runId")
    names = manifest.get("stackNames")
    if not isinstance(run_id, str) or re.fullmatch(r"[0-9a-f]{12}", run_id) is None:
        raise ValueError("invalid E2E ownership manifest")
    if not isinstance(names, list) or not names or any(
        not isinstance(name, str) or not name.startswith("iac-e2e-" + run_id + "-") for name in names
    ):
        raise ValueError("E2E Stack names do not prove run ownership")

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
    for name in names:
        try:
            named_stacks = _named_stacks(client, ros_models, name, region)
        except Exception as exc:
            raise CleanupOperationError("list_stacks", exc) from exc
        for listed in named_stacks:
            stack_id = listed.stack_id
            if not isinstance(stack_id, str) or not stack_id:
                failures.append(name + ": Stack ID missing")
                continue
            while time.monotonic() < deadline:
                try:
                    body = _stack_body(client, ros_models, stack_id, region)
                    if body is None or body.get("Status") == "DELETE_COMPLETE":
                        deleted.append(stack_id)
                        break
                    if body.get("StackName") != name:
                        failures.append(stack_id + ": StackName ownership mismatch")
                        break
                    status = str(body.get("Status") or "")
                    if not status.endswith("_IN_PROGRESS"):
                        try:
                            client.delete_stack(ros_models.DeleteStackRequest(stack_id=stack_id, region_id=region))
                        except Exception as exc:
                            if "actioninprogress" not in str(exc).lower():
                                raise
                    time.sleep(min(5, max(0, deadline - time.monotonic())))
                except Exception as exc:
                    failures.append(stack_id + ": " + type(exc).__name__)
                    break
            else:
                failures.append(stack_id + ": cleanup timeout")
            if stack_id not in deleted:
                remaining.append(stack_id)
    # The model can violate an exact name instruction by adding a suffix.
    # Such resources are not authorized for deletion by this manifest, but
    # an empty exact-name lookup must not report that cleanup succeeded.
    try:
        scoped_stacks = _named_stacks(client, ros_models, "iac-e2e-" + run_id + "-", region, prefix=True)
        for listed in scoped_stacks:
            if listed.stack_name in names:
                continue
            body = _stack_body(client, ros_models, listed.stack_id, region)
            if body is not None and body.get("Status") != "DELETE_COMPLETE":
                failures.append("unexpected run-scoped Stack outside exact ownership manifest")
                remaining.append(listed.stack_id)
    except Exception as exc:
        raise CleanupOperationError("run_scope_audit", exc) from exc
    result = {
        "status": "failed" if failures or remaining else "completed",
        "deletedStackIds": deleted,
        "remainingStackIds": remaining,
        "failures": failures,
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
