from __future__ import annotations

from collections import defaultdict
from typing import Any

from scripts.observability.local_observe.records import Record

RAW_KEYS = ("gen_ai.input.messages", "gen_ai.system_instructions", "gen_ai.output.messages", "prompt", "user_prompt")


def _result(label: str, status: str, message: str, evidence_ids: list[str] | None = None) -> dict[str, Any]:
    return {
        "label": label,
        "status": status,
        "message": message,
        "evidence_ids": evidence_ids or [],
    }


def evaluate_assertions(records: list[Record], *, expected_raw_content: str = "off") -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    results.extend(_raw_content_assertions(records, expected_raw_content=expected_raw_content))
    results.extend(_step_attempt_assertions(records))
    return results


def _raw_content_assertions(records: list[Record], *, expected_raw_content: str) -> list[dict[str, Any]]:
    present: dict[str, list[str]] = defaultdict(list)
    for record in records:
        attrs = record.get("attributes", {})
        for key in RAW_KEYS:
            if key in attrs and attrs[key]:
                present[key].append(record["id"])

    if expected_raw_content == "on":
        return [
            _result(
                f"{key} present",
                "pass" if present.get(key) else "fail",
                f"{key} was {'received' if present.get(key) else 'not received'}",
                present.get(key, []),
            )
            for key in ("gen_ai.input.messages", "gen_ai.system_instructions")
        ]

    return [
        _result(
            f"{key} absent",
            "fail" if present.get(key) else "pass",
            f"{key} {'leaked in raw telemetry' if present.get(key) else 'was not present'}",
            present.get(key, []),
        )
        for key in RAW_KEYS
    ]


def _step_attempt_assertions(records: list[Record]) -> list[dict[str, Any]]:
    results = [_step_attempt_presence_assertion(records)]
    attempts_by_step: dict[str, set[int]] = defaultdict(set)
    for record in records:
        attrs = record.get("attributes", {})
        step_id = attrs.get("step_id")
        attempt = attrs.get("step_attempt")
        if not step_id:
            continue
        if type(attempt) is int:
            attempts_by_step[str(step_id)].add(attempt)
        elif isinstance(attempt, str) and attempt.isdigit():
            attempts_by_step[str(step_id)].add(int(attempt))
    repeated = {step_id: attempts for step_id, attempts in attempts_by_step.items() if len(attempts) > 1}
    results.append(
        _result(
            "Repeated step attempts are distinguishable",
            "pass" if repeated else "warn",
            "Repeated attempts found" if repeated else "No repeated step attempts observed yet",
        )
    )
    return results


def _step_attempt_presence_assertion(records: list[Record]) -> dict[str, Any]:
    pipeline_records = [record for record in records if _is_pipeline_step_record(record)]
    missing = [
        record["id"]
        for record in pipeline_records
        if not _has_valid_attempt(record.get("attributes", {}).get("step_attempt"))
    ]
    if missing:
        return _result(
            "Pipeline step_attempt is present",
            "fail",
            "Some pipeline step records are missing a numeric step_attempt",
            missing,
        )
    if not pipeline_records:
        return _result(
            "Pipeline step_attempt is present",
            "warn",
            "No pipeline step records observed yet",
        )
    return _result(
        "Pipeline step_attempt is present",
        "pass",
        "All observed pipeline step records include numeric step_attempt",
    )


def _is_pipeline_step_record(record: Record) -> bool:
    attrs = record.get("attributes", {})
    return bool(attrs.get("step_id")) and str(record.get("name", "")).startswith("iac.pipeline.")


def _has_valid_attempt(value: Any) -> bool:
    return type(value) is int or (isinstance(value, str) and value.isdigit())
