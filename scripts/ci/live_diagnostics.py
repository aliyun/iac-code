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

COMPLETION_ERROR_PATTERNS = {
    "input_schema": r"completion_input_schema_validation_failed",
    "conclusion_schema": r"conclusion_schema_validation_failed|Schema validation failed|schema 验证失败",
    "retry_exhausted": r"maximum retry count|超过最大重试|exceeding.{0,30}retry",
    "no_conclusion": r"No conclusion extracted|No result",
    "missing_required": r"is a required property|required property|缺少必填",
    "guard_rejected": r"completion guard|complete_step validation failed",
    "natural_handoff_receipt": r"Natural completion did not produce an exact durable handoff receipt",
}


def _evidence_paths(root: Path, pattern: str, runtime_config_dir: Path | None) -> list[Path]:
    paths: dict[Path, Path] = {}
    for base in (root, runtime_config_dir):
        if base is None:
            continue
        for path in base.rglob(pattern):
            paths.setdefault(path.resolve(), path)
            if len(paths) >= 60:
                return list(paths.values())
    return list(paths.values())


def _schema_property_names() -> set[str]:
    """Only names in the repository's public schema can leave the CI host."""
    path = Path(__file__).resolve().parents[2] / 'src/iac_code/pipeline/selling_solution_first/pipeline.yaml'
    pending = [yaml.safe_load(path.read_text(encoding='utf-8'))]
    names: set[str] = set()
    while pending:
        value = pending.pop()
        if isinstance(value, dict):
            properties = value.get('properties')
            if isinstance(properties, dict):
                names.update(k for k in properties if isinstance(k, str))
            pending.extend(value.values())
        elif isinstance(value, list):
            pending.extend(value)
    return names


def _completion_failure_facts(root: Path, runtime_config_dir: Path | None = None) -> dict[str, Any]:
    """Project only fixed failure codes and schema validators, never tool result bodies."""
    codes: Counter[str] = Counter()
    validators: set[str] = set()
    missing_fields: set[str] = set()
    schema_fields: set[str] = set()
    allowed_fields = _schema_property_names()
    failed_calls = 0
    allowed_validators = {"required", "type", "oneOf", "anyOf", "enum", "const", "minItems", "additionalProperties"}
    for path in _evidence_paths(root, "transcripts/*/session.jsonl", runtime_config_dir)[:30]:
        if path.stat().st_size > 20_000_000:
            continue
        calls: set[str] = set()
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            content = row.get("content") if isinstance(row, dict) else None
            for block in content if isinstance(content, list) else []:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "tool_use" and block.get("name") == "complete_step":
                    calls.add(str(block.get("id") or ""))
                if (block.get("type") != "tool_result" or block.get("tool_use_id") not in calls
                    or not block.get("is_error")):
                    continue
                failed_calls += 1
                content = block.get('content') or ''
                if isinstance(content, list):
                    content = '\n'.join(str(x.get('text') or '') for x in content if isinstance(x, dict))
                text = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
                try:
                    decoded = json.loads(text)
                except (TypeError, ValueError):
                    pass
                else:
                    if isinstance(decoded, (dict, list)):
                        text = json.dumps(decoded, ensure_ascii=False)
                for code, pattern in COMPLETION_ERROR_PATTERNS.items():
                    if re.search(pattern, text, re.I):
                        codes[code] += 1
                validators.update(v for v in re.findall(r'"validator"\s*:\s*"([A-Za-z]+)"', text)
                                  if v in allowed_validators)
                # A single validation error is plain jsonschema text, without
                # the structured multi-error envelope. Keep its public field
                # name and validator without copying values or messages.
                required = re.findall(r"'([A-Za-z_][A-Za-z_0-9]*)' is a required property", text)
                if required:
                    validators.add('required')
                    missing_fields.update(set(required).intersection(allowed_fields))
                if 'is not of type' in text:
                    validators.add('type')
                for pointer in re.findall(r'"path"\s*:\s*"([^"]*)"', text):
                    schema_fields.update(set(pointer.split('/')).intersection(allowed_fields))
    facts: dict[str, Any] = {"complete_step_error_count": min(failed_calls, 10000)}
    if codes:
        facts["completion_error_codes"] = dict(codes)
    if validators:
        facts["completion_schema_validators"] = sorted(validators)
    if missing_fields:
        facts['completion_schema_missing_fields'] = sorted(missing_fields)[:20]
    if schema_fields:
        facts['completion_schema_fields'] = sorted(schema_fields)[:20]
    return facts


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


def collect_live_diagnostics(
    root: Path, summary: dict[str, Any], *, runtime_config_dir: Path | None = None,
) -> dict[str, Any]:
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
                    facts['cleanup_unexpected_name_same_case_count'] = min(sum(
                        isinstance(item, dict) and isinstance(item.get('stackName'), str)
                        and item['stackName'] not in names
                        and any(item['stackName'].startswith(name + '-') for name in names)
                        for item in resources
                    ), 10000)
    codes: set[str] = set()
    cleanup_attempts: list[dict[str, str]] = []
    for log in root.rglob("cleanup-*.log"):
        text = log.read_text(encoding="utf-8", errors="replace")
        codes.update(code for code in KNOWN_CODES if code in text)
        for line in text.splitlines():
            try:
                value = json.loads(line)
            except ValueError:
                continue
            item = value.get("cleanupDiagnostic") if isinstance(value, dict) else None
            if not isinstance(item, dict):
                continue
            projected = {}
            for key, allowed in {
                "stage": {"get_stack", "delete_stack"},
                "errorType": {"TimeoutError", "RuntimeError", "ConnectionError", "PermissionError", "SDKError"},
                "code": {*KNOWN_CODES, "unknown"},
                "status": {"CREATE_COMPLETE", "CREATE_IN_PROGRESS", "CREATE_FAILED", "DELETE_IN_PROGRESS",
                           "DELETE_FAILED", "DELETE_COMPLETE", "ROLLBACK_IN_PROGRESS", "ROLLBACK_COMPLETE", "unknown"},
            }.items():
                if isinstance(item.get(key), str) and item[key] in allowed:
                    projected[key] = item[key]
            if projected:
                cleanup_attempts.append(projected)
    if cleanup_attempts:
        facts["cleanup_attempt_diagnostics"] = cleanup_attempts[:16]
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

    for meta in _evidence_paths(root, "pipeline/meta.yaml", runtime_config_dir):
        try:
            state = yaml.safe_load(meta.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError):
            continue
        if not isinstance(state, dict):
            continue
        status = state.get("status")
        if status in {"running", "waiting_input", "completed", "failed", "canceled", "discarded"}:
            facts["pipeline_status"] = status
        handoff = state.get("normal_handoff")
        if isinstance(handoff, dict) and handoff.get("status") in {"pending", "succeeded", "failed"}:
            facts["normal_handoff_status"] = handoff["status"]
        reason = str(state.get("reason") or "")
        reason_codes = [code for code, pattern in COMPLETION_ERROR_PATTERNS.items() if re.search(pattern, reason, re.I)]
        if reason_codes:
            facts["pipeline_reason_codes"] = reason_codes
        step = state.get("current_step")
        if step in {
            "solution_planning_and_selection", "materialize_selected_candidate", "deploying", "confirm_and_select",
            "intent_parsing", "architecture_design", "architecture_detail",
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
    from scripts.e2e_question_driver import (
        NETWORK_DIAGNOSTIC_FILENAME,
        NETWORK_FAILURE_CATEGORIES,
        NETWORK_KNOWN_CODES,
    )

    for path in _evidence_paths(root, NETWORK_DIAGNOSTIC_FILENAME, runtime_config_dir):
        try:
            if path.stat().st_size > 4096:
                continue
            value = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            continue
        if not isinstance(value, dict):
            continue
        category = value.get('network_fixture_failure_category')
        if isinstance(category, str) and category in NETWORK_FAILURE_CATEGORIES:
            facts['network_fixture_failure_category'] = category
        for key, minimum, maximum in (
            ('network_fixture_exit_code', -255, 255), ('network_fixture_scan_retry_count', 0, 2),
        ):
            count = value.get(key)
            if isinstance(count, int) and not isinstance(count, bool) and minimum <= count <= maximum:
                facts[key] = count
        code = value.get('network_fixture_scan_retry_code')
        if isinstance(code, str) and code in NETWORK_KNOWN_CODES:
            facts['network_fixture_scan_retry_code'] = code
        codes = value.get('network_fixture_known_codes')
        if isinstance(codes, list):
            facts['network_fixture_known_codes'] = sorted({
                c for c in codes if isinstance(c, str) and c in NETWORK_KNOWN_CODES})
    facts.update(_completion_failure_facts(root, runtime_config_dir))
    return facts
