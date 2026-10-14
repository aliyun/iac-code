"""A review can append its own pixel handoff, never rewrite source evidence."""
from __future__ import annotations

import json
from contextlib import nullcontext
from pathlib import Path
from typing import Any

from .files import WorkflowError, contained, digest


def validate(root: Path, store: Any, context: dict, relative: str, text: str, *, connection=None) -> dict:
    try:
        value = json.loads(text)
    except (TypeError, ValueError) as error:
        raise WorkflowError("review may only append its exact screenshot handoff JSON") from error
    if (context["purpose"] != "review" or context["role"] != "sol"
            or Path(relative).parts[0] != context["resource"]["id"]
            or not relative.endswith(".json") or not isinstance(value, dict)
            or set(value) != {"kind", "task_id", "job_id", "source_job_id", "navigation_verified", "verified_scenes"}
            or value["kind"] != "native_screenshot_review" or value["navigation_verified"] is not True
            or value["task_id"] != context["task_id"] or value["job_id"] != context["job_id"]
            or not context.get("native_cleanup_order")):
        raise WorkflowError("review cannot change evidence; only its exact screenshot handoff is appendable")
    with nullcontext(connection) if connection is not None else store.transaction() as db:
        source = db.execute("SELECT * FROM jobs WHERE task_id=? AND role='glm' ORDER BY created DESC,rowid DESC",
                            (context["task_id"],)).fetchone()
        if not source or source["purpose"] != "continuation" or source["id"] != value["source_job_id"]:
            raise WorkflowError("screenshot review needs this resource's latest continuation")
        expected = []
        for row in db.execute("SELECT * FROM observations WHERE task_id=? AND attempt=? ORDER BY scene",
                              (context["task_id"], context["attempt"])):
            data = json.loads(row["data"])
            if data.get("actual_ui_click") is not True:
                raise WorkflowError("screenshot review requires an actual click")
            images = [data["screenshot"]]
            inspection = data.get("console_inspection") or {}
            images.extend(v["screenshot"] for v in inspection.get("supplemental_captures", []))
            if inspection.get("screenshot") and inspection["screenshot"] not in images:
                images.append(inspection["screenshot"])
            for image in images:
                path = contained(root, image)
                if (not path.is_relative_to(root / "jobs" / source["id"] / "output")
                        or not db.execute("SELECT 1 FROM inspected WHERE job_id=? AND artifact=? AND sha=?",
                                          (context["job_id"], image, digest(path))).fetchone()):
                    raise WorkflowError("every unchanged original and supplemental PNG must be inspected by this Sol")
            expected.append({"scene": row["scene"], "screenshot": data["screenshot"],
                             "sha256": digest(contained(root, data["screenshot"]))})
        if not expected or value["verified_scenes"] != expected:
            raise WorkflowError("screenshot handoff must contain all and only this attempt's actual clicks")
    return value
