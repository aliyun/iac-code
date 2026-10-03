"""Extract bounded failure facts from local live artifacts without exporting bodies."""

from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path
from typing import Any

import yaml

KNOWN_CODES = (
    "EntityNotExist.Stack", "NotFound.Stack", "StackNotFound", "ActionInProgress",
    "Forbidden", "Forbidden.RAM", "InvalidAccessKeyId.NotFound", "SecurityTokenExpired",
    "Throttling", "Throttling.User", "InvalidParameter", "DeleteFailed", "DependencyViolation",
)
KNOWN_WAITS = {
    "candidate selection visible", "candidate selection controls ready", "candidate selection input ready",
    "live candidate selection controls ready", "pipeline fully completed", "first stack create started",
    "Step 1 ask before restart", "Step 1 ask restored", "Step 2 parameter ask before restart",
    "Step 2 parameter ask restored", "deployment confirmation before restart",
    "deployment confirmation restored", "restored deployment confirmation selector ready",
    "restored Step 2 answer acknowledgement",
}


def _known_wait(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    if value in KNOWN_WAITS:
        return value
    for pattern, label in (
        (r"Step 2 parameter ask #[1-4] input ready", "Step 2 parameter question input ready"),
        (r"deployment confirmation selector ready #[1-9][0-9]?", "deployment confirmation selector ready"),
    ):
        if re.fullmatch(pattern, value):
            return label
    return None


def collect_live_diagnostics(root: Path, summary: dict[str, Any]) -> dict[str, Any]:
    from scripts.a2a.debugger import _extract_pipeline_envelopes

    facts: dict[str, Any] = {}
    abort = summary.get("abort_reason") or summary.get("error") or ""
    if isinstance(abort, str):
        wait = _known_wait(abort.rsplit("timed out waiting for ", 1)[-1])
        if wait:
            facts["failed_wait"] = wait
        for kind in ("TimeoutError", "RuntimeError", "ValueError", "PermissionError", "ConnectionError"):
            if abort.startswith(kind + ":"):
                facts["abort_type"] = kind
        for label, marker in (
            ("selection_not_accepted", "candidate selection input was not accepted"),
            ("no_output", "no terminal output"), ("unexpected_input", "unexpected input while waiting"),
            ("wait_deadline", "timed out waiting"),
        ):
            if marker in abort.lower():
                facts["abort_category"] = label

    cleanup = root / "cleanup-result.json"
    if cleanup.is_file():
        try:
            value = json.loads(cleanup.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            value = {}
        if isinstance(value, dict):
            for source, target in (
                ("resources", "cleanup_resource_count"), ("deletedStackIds", "cleanup_deleted_count"),
            ):
                if isinstance(value.get(source), list):
                    facts[target] = min(len(value[source]), 10000)
            categories: Counter[str] = Counter()
            for failure in value.get("failures", []) if isinstance(value.get("failures"), list) else []:
                text = str(failure).lower()
                category = next((label for label, marker in (
                    ("discovery_failed", "discovery failed"), ("ownership_unproven", "ownership could not be proven"),
                    ("delete_subprocess_failed", "cleanup subprocess exited"), ("timeout", "timeout"),
                ) if marker in text), "other")
                categories[category] += 1
            facts["cleanup_failure_categories"] = dict(categories)
            resources = value.get("resources")
            manifests = list(root.rglob("owned-stack-names.json"))
            if isinstance(resources, list) and len(manifests) == 1:
                try:
                    names = json.loads(manifests[0].read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    names = None
                if isinstance(names, list) and all(isinstance(name, str) for name in names):
                    facts["cleanup_missing_name_count"] = min(sum(
                        isinstance(item, dict) and not item.get("stackName") for item in resources
                    ), 10000)
                    facts["cleanup_unexpected_name_count"] = min(sum(
                        isinstance(item, dict) and bool(item.get("stackName")) and item["stackName"] not in names
                        for item in resources
                    ), 10000)
    codes: set[str] = set()
    for log in root.rglob("cleanup-*.log"):
        text = log.read_text(encoding="utf-8", errors="replace")
        codes.update(code for code in KNOWN_CODES if code in text)
    if codes:
        facts["cleanup_known_codes"] = sorted(codes)

    counts: Counter[str] = Counter()
    marker_present = False
    for path in (*root.glob("*.events.jsonl"), root / "events.jsonl", root / "repl-events.jsonl"):
        if not path.is_file():
            continue
        with path.open(encoding="utf-8", errors="replace") as stream:
            for line in stream:
                marker_present |= "candidate_step_started" in line
                try:
                    value = json.loads(line)
                except ValueError:
                    continue
                if isinstance(value, dict) and value.get("type") == "expect" and value.get("passed") is False:
                    wait = _known_wait(value.get("description"))
                    if wait:
                        facts["failed_wait"] = wait
                for envelope in _extract_pipeline_envelopes(value):
                    kind = envelope.get("eventType")
                    if kind in {"candidate_step_started", "step_started", "input_received"}:
                        counts[kind] += 1
                    data = envelope.get("data")
                    if (
                        kind == "input_received" and isinstance(data, dict)
                        and data.get("kind") == "deployment_confirmation"
                    ):
                        action = data.get("action")
                        action = action if action in {"confirm", "cancel", "adjust", "reselect"} else "free_text"
                        counts["confirmation_" + action] += 1
                        if data.get("has_images") is True:
                            counts["confirmation_image"] += 1
    facts["a2a_event_counts"] = {key: min(value, 10000) for key, value in sorted(counts.items())}
    facts["candidate_marker_without_event"] = marker_present and not counts["candidate_step_started"]

    for meta in root.rglob("pipeline/meta.yaml"):
        try:
            state = yaml.safe_load(meta.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError):
            continue
        if not isinstance(state, dict):
            continue
        step = state.get("current_step")
        if step in {
            "solution_planning_and_selection", "materialize_selected_candidate", "deploying", "confirm_and_select",
        }:
            facts["pending_step"] = step
        execution = state.get("execution")
        if isinstance(execution, dict):
            kind = execution.get("pending_input_kind")
            if kind in {"ask_user_question", "candidate_selection", "deployment_confirmation"}:
                facts["pending_input_kind"] = kind
            elif not kind:
                facts["pending_input_kind"] = "none"
            question = execution.get("pending_ask_user_question_input")
            if isinstance(question, dict):
                facts["pending_question_answered"] = isinstance(question.get("answer"), dict)
    return facts
