from __future__ import annotations

import json
from collections import defaultdict
from typing import Any

from scripts.observability.local_observe.records import Record

PIPELINE_STEP_SPAN_NAMES = {"iac.pipeline.step", "iac.pipeline.sub_pipeline", "iac.pipeline.sub_step"}
RUN_EVIDENCE_GROUPS = (
    ("pipeline_lifecycle", "Pipeline lifecycle"),
    ("normal_chat_after_pipeline", "Normal chat after pipeline"),
    ("other_session_evidence", "Other session evidence"),
)
NORMAL_CHAT_SPAN_NAMES = {"enter_ai_application_system", "react step"}


def _attempt(value: Any) -> int:
    if type(value) is int:
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return 1


def build_pipeline_model(records: list[Record]) -> dict[str, Any]:
    records = _compact_metric_records(records)
    records_by_id = {str(record.get("id")): record for record in records}
    children_by_parent: dict[str, list[Record]] = defaultdict(list)
    for record in records:
        parent = record.get("parent_span_id")
        if parent:
            children_by_parent[parent].append(record)
    pipeline_descendant_ids = _pipeline_descendant_record_ids(records, children_by_parent)

    sub_step_instances = _sub_step_instance_index(records)
    step_records = [
        record
        for record in records
        if record.get("kind") in {"span", "log", "metric"} and _step_instance_id(record, sub_step_instances) is not None
    ]
    sessions_by_step = _sessions_by_step(step_records, sub_step_instances)
    unscoped_metric_ids = {
        record["id"]
        for record in records
        if _is_unscoped_pipeline_metric(record, sessions_by_step, sub_step_instances)
    }
    unscoped_metrics = [_metric_summary(record) for record in records if record["id"] in unscoped_metric_ids]
    step_records = [record for record in step_records if record["id"] not in unscoped_metric_ids]

    runs: dict[tuple[str, str], dict[str, Any]] = {}
    for record in records:
        if record["id"] in unscoped_metric_ids:
            continue
        attrs = record.get("attributes", {})
        pipeline_name = attrs.get("pipeline_name")
        session_id = _record_session_id(record, sessions_by_step, sub_step_instances)
        if not pipeline_name:
            continue
        key = (str(pipeline_name or "unknown"), str(session_id or "unknown"))
        runs.setdefault(
            key,
            {
                "pipeline_name": key[0],
                "session_id": key[1],
                "record_ids": [],
                "evidence_records": [],
                "steps": [],
            },
        )["record_ids"].append(record["id"])

    grouped_steps: dict[tuple[str, str, str, int], list[Record]] = defaultdict(list)
    for record in step_records:
        attrs = record.get("attributes", {})
        pipeline_name = str(attrs.get("pipeline_name") or "unknown")
        session_id = str(_record_session_id(record, sessions_by_step, sub_step_instances) or "unknown")
        step_instance_id = _step_instance_id(record, sub_step_instances)
        if step_instance_id is None:
            continue
        step_attempt = _attempt(attrs.get("step_attempt", 1))
        grouped_steps[(pipeline_name, session_id, step_instance_id, step_attempt)].append(record)

    step_record_ids = {str(record.get("id")) for items in grouped_steps.values() for record in items}
    step_key_by_span_id = _step_key_by_span_id(grouped_steps, children_by_parent)
    for record in records:
        record_id = str(record.get("id"))
        if record_id in step_record_ids or record_id in unscoped_metric_ids:
            continue
        if not _is_span_associated_step_record(record):
            continue
        step_key = step_key_by_span_id.get(str(record.get("span_id") or ""))
        if step_key is None:
            continue
        grouped_steps[step_key].append(record)
        step_record_ids.add(record_id)

    for (pipeline_name, session_id, step_instance_id, step_attempt), items in grouped_steps.items():
        run = runs.setdefault(
            (pipeline_name, session_id),
            {
                "pipeline_name": pipeline_name,
                "session_id": session_id,
                "record_ids": [],
                "evidence_records": [],
                "steps": [],
            },
        )
        step_span = next((item for item in items if _is_pipeline_step_span(item)), None)
        rounds = _agent_rounds(step_span, children_by_parent) if step_span else []
        step_attrs = _step_attrs(step_span or items[0])
        run["steps"].append(
            {
                "step_id": step_attrs["step_id"],
                "step_instance_id": step_instance_id,
                "step_attempt": step_attempt,
                "parent_step_id": step_attrs["parent_step_id"],
                "sub_pipeline_id": step_attrs["sub_pipeline_id"],
                "sub_step_id": step_attrs["sub_step_id"],
                "record_ids": [item["id"] for item in items],
                "evidence_records": [_evidence_record(item) for item in items],
                "agent_rounds": rounds,
            }
        )

    _attach_session_evidence(records, runs, pipeline_descendant_ids)

    for run in runs.values():
        run["steps"].sort(key=lambda item: (item["step_id"], item["sub_pipeline_id"] or "", item["step_attempt"]))
        step_record_ids = {record_id for step in run["steps"] for record_id in step.get("record_ids", [])}
        run["evidence_records"] = [
            _evidence_record(records_by_id[str(record_id)])
            for record_id in run["record_ids"]
            if record_id not in step_record_ids and str(record_id) in records_by_id
        ]
        run["evidence_groups"] = _run_evidence_groups(run["evidence_records"])
    return {"runs": list(runs.values()), "unmatched_count": len(records), "unscoped_metrics": unscoped_metrics}


def _evidence_record(record: Record) -> dict[str, Any]:
    return {
        "id": record.get("id", ""),
        "kind": record.get("kind", ""),
        "name": record.get("name", ""),
        "timestamp_unix_nano": record.get("timestamp_unix_nano", 0),
        "attributes": record.get("attributes", {}),
        "span_id": record.get("span_id", ""),
        "parent_span_id": record.get("parent_span_id", ""),
        "trace_id": record.get("trace_id", ""),
        "value": record.get("value"),
    }


def _run_evidence_groups(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    normal_trace_ids = {
        str(record.get("trace_id"))
        for record in records
        if record.get("trace_id") and _is_normal_chat_anchor(record)
    }
    normal_span_ids = {
        str(record.get("span_id"))
        for record in records
        if record.get("span_id") and _is_normal_chat_anchor(record)
    }
    grouped: dict[str, list[dict[str, Any]]] = {group_id: [] for group_id, _title in RUN_EVIDENCE_GROUPS}
    for record in records:
        if _is_pipeline_lifecycle_record(record):
            grouped["pipeline_lifecycle"].append(record)
        elif _is_normal_chat_evidence(record, normal_trace_ids, normal_span_ids):
            grouped["normal_chat_after_pipeline"].append(record)
        else:
            grouped["other_session_evidence"].append(record)
    return [
        {"id": group_id, "title": title, "records": grouped[group_id]}
        for group_id, title in RUN_EVIDENCE_GROUPS
    ]


def _is_pipeline_lifecycle_record(record: Record) -> bool:
    attrs = record.get("attributes", {})
    name = str(record.get("name") or "")
    if not attrs.get("pipeline_name"):
        return False
    if _has_step_scope(attrs):
        return False
    return name == "iac.pipeline.run" or name.startswith("iac.pipeline.")


def _has_step_scope(attrs: dict[str, Any]) -> bool:
    return any(
        attrs.get(key) is not None
        for key in (
            "step_id",
            "step_index",
            "step_attempt",
            "parent_step_id",
            "sub_pipeline_id",
            "sub_step_id",
            "sub_step_index",
            "candidate_index",
        )
    )


def _is_normal_chat_evidence(
    record: Record,
    normal_trace_ids: set[str],
    normal_span_ids: set[str],
) -> bool:
    attrs = record.get("attributes", {})
    if attrs.get("pipeline_name") or record.get("kind") not in {"span", "log"}:
        return False
    if _is_normal_chat_anchor(record):
        return True
    trace_id = record.get("trace_id")
    span_id = record.get("span_id")
    return bool((trace_id and str(trace_id) in normal_trace_ids) or (span_id and str(span_id) in normal_span_ids))


def _is_normal_chat_anchor(record: Record) -> bool:
    attrs = record.get("attributes", {})
    name = str(record.get("name") or "")
    return (
        name in NORMAL_CHAT_SPAN_NAMES
        or name.startswith("chat ")
        or name.startswith("execute_tool ")
        or any(str(key).startswith("gen_ai.") for key in attrs)
    )


def _attach_session_evidence(
    records: list[Record],
    runs: dict[tuple[str, str], dict[str, Any]],
    pipeline_descendant_ids: set[str],
) -> None:
    run_keys_by_session: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for key, run in runs.items():
        session_id = run.get("session_id")
        if session_id and session_id != "unknown":
            run_keys_by_session[str(session_id)].append(key)

    unique_run_by_session = {
        session_id: keys[0] for session_id, keys in run_keys_by_session.items() if len(keys) == 1
    }
    attached_ids_by_run = {key: set(run.get("record_ids", [])) for key, run in runs.items()}
    for record in records:
        record_id = str(record.get("id"))
        if record_id in pipeline_descendant_ids:
            continue
        if not _is_session_evidence_record(record):
            continue
        session_id = _telemetry_session_id(record)
        if session_id is None:
            continue
        key = unique_run_by_session.get(session_id)
        if key is None or record_id in attached_ids_by_run[key]:
            continue
        runs[key]["record_ids"].append(record.get("id"))
        attached_ids_by_run[key].add(record_id)


def _is_session_evidence_record(record: Record) -> bool:
    attrs = record.get("attributes", {})
    if attrs.get("pipeline_name"):
        return False
    return record.get("kind") in {"span", "log"} and _telemetry_session_id(record) is not None


def _pipeline_descendant_record_ids(
    records: list[Record],
    children_by_parent: dict[str, list[Record]],
) -> set[str]:
    descendants: set[str] = set()
    for record in records:
        if not _is_pipeline_step_span(record):
            continue
        for child in _descendants_within_step(record.get("span_id", ""), children_by_parent):
            descendants.add(str(child.get("id")))
    return descendants


def _step_key_by_span_id(
    grouped_steps: dict[tuple[str, str, str, int], list[Record]],
    children_by_parent: dict[str, list[Record]],
) -> dict[str, tuple[str, str, str, int]]:
    out: dict[str, tuple[str, str, str, int]] = {}
    for step_key, items in grouped_steps.items():
        step_span = next((item for item in items if _is_pipeline_step_span(item)), None)
        if step_span is None:
            continue
        step_span_id = step_span.get("span_id")
        if not step_span_id:
            continue
        out[str(step_span_id)] = step_key
        for child in _descendants_within_step(str(step_span_id), children_by_parent):
            child_span_id = child.get("span_id")
            if child_span_id:
                out[str(child_span_id)] = step_key
    return out


def _is_span_associated_step_record(record: Record) -> bool:
    return record.get("kind") == "log" and bool(record.get("span_id"))


def _compact_metric_records(records: list[Record]) -> list[Record]:
    latest_by_key: dict[tuple[str, str, str, str, str, str], tuple[tuple[float, float, int], str]] = {}
    for index, record in enumerate(records):
        if not _is_compactable_metric(record):
            continue
        key = _metric_identity(record)
        order_key = _metric_order_key(record, index)
        current = latest_by_key.get(key)
        if current is None or order_key >= current[0]:
            latest_by_key[key] = (order_key, str(record.get("id")))
    latest_metric_ids = {record_id for _, record_id in latest_by_key.values()}
    return [
        record
        for record in records
        if not _is_compactable_metric(record) or str(record.get("id")) in latest_metric_ids
    ]


def _is_compactable_metric(record: Record) -> bool:
    return record.get("kind") == "metric" and str(record.get("name", "")).startswith("iac.pipeline.")


def _metric_identity(record: Record) -> tuple[str, str, str, str, str, str]:
    return (
        str(record.get("name") or ""),
        str(record.get("metric_type") or ""),
        str(record.get("aggregation_temporality") or ""),
        _stable_json(record.get("resource", {})),
        _stable_json(record.get("scope", {})),
        _stable_json(record.get("attributes", {})),
    )


def _metric_order_key(record: Record, index: int) -> tuple[float, float, int]:
    return (_number(record.get("timestamp_unix_nano")), _number(record.get("received_at")), index)


def _stable_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, default=str, separators=(",", ":"))


def _number(value: Any) -> float:
    if isinstance(value, int | float):
        return float(value)
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return 0.0


def _is_unscoped_pipeline_metric(
    record: Record,
    sessions_by_step: dict[tuple[str, str, int], set[str]],
    sub_step_instances: dict[tuple[str, str, str, str], str],
) -> bool:
    if record.get("kind") != "metric" or not str(record.get("name", "")).startswith("iac.pipeline."):
        return False
    attrs = record.get("attributes", {})
    if not attrs.get("pipeline_name"):
        return False
    return not _record_session_id(record, sessions_by_step, sub_step_instances)


def _metric_summary(record: Record) -> dict[str, Any]:
    return {
        "record_id": record["id"],
        "name": record.get("name"),
        "value": record.get("value"),
        "attributes": record.get("attributes", {}),
    }


def _is_pipeline_step_span(record: Record) -> bool:
    return record.get("kind") == "span" and record.get("name") in PIPELINE_STEP_SPAN_NAMES


def _step_id(record: Record) -> str | None:
    attrs = record.get("attributes", {})
    for key in ("step_id", "sub_step_id", "sub_pipeline_id", "parent_step_id"):
        value = attrs.get(key)
        if value is not None:
            return str(value)
    return None


def _step_instance_id(record: Record, sub_step_instances: dict[tuple[str, str, str, str], str]) -> str | None:
    attrs = record.get("attributes", {})
    sub_step_id = attrs.get("sub_step_id")
    sub_pipeline_id = attrs.get("sub_pipeline_id")
    if sub_step_id is not None:
        if sub_pipeline_id is not None:
            return f"{sub_pipeline_id}/{sub_step_id}"
        key = _sub_step_lookup_key(attrs)
        if key is not None and key in sub_step_instances:
            return sub_step_instances[key]
        if not _is_pipeline_step_span(record):
            return None
        return str(sub_step_id)
    step_id = _step_id(record)
    return str(step_id) if step_id is not None else None


def _step_attrs(record: Record) -> dict[str, str | None]:
    attrs = record.get("attributes", {})
    return {
        "step_id": _step_id(record) or "unknown",
        "parent_step_id": _str_or_none(attrs.get("parent_step_id")),
        "sub_pipeline_id": _str_or_none(attrs.get("sub_pipeline_id")),
        "sub_step_id": _str_or_none(attrs.get("sub_step_id")),
    }


def _str_or_none(value: Any) -> str | None:
    return str(value) if value is not None else None


def _sub_step_instance_index(records: list[Record]) -> dict[tuple[str, str, str, str], str]:
    candidates: dict[tuple[str, str, str, str], set[str]] = defaultdict(set)
    for record in records:
        attrs = record.get("attributes", {})
        sub_pipeline_id = attrs.get("sub_pipeline_id")
        sub_step_id = attrs.get("sub_step_id")
        key = _sub_step_lookup_key(attrs)
        if not _is_pipeline_step_span(record) or key is None or sub_pipeline_id is None or sub_step_id is None:
            continue
        candidates[key].add(f"{sub_pipeline_id}/{sub_step_id}")
    return {key: next(iter(values)) for key, values in candidates.items() if len(values) == 1}


def _sub_step_lookup_key(attrs: dict[str, Any]) -> tuple[str, str, str, str] | None:
    pipeline_name = attrs.get("pipeline_name")
    parent_step_id = attrs.get("parent_step_id")
    sub_step_id = attrs.get("sub_step_id")
    candidate_index = attrs.get("candidate_index")
    if pipeline_name is None or parent_step_id is None or sub_step_id is None or candidate_index is None:
        return None
    return (str(pipeline_name), str(parent_step_id), str(sub_step_id), str(candidate_index))


def _sessions_by_step(
    records: list[Record], sub_step_instances: dict[tuple[str, str, str, str], str]
) -> dict[tuple[str, str, int], set[str]]:
    out: dict[tuple[str, str, int], set[str]] = defaultdict(set)
    for record in records:
        attrs = record.get("attributes", {})
        pipeline_name = attrs.get("pipeline_name")
        session_id = attrs.get("session_id") or _resource_session_id(record)
        step_instance_id = _step_instance_id(record, sub_step_instances)
        if not pipeline_name or not session_id or step_instance_id is None:
            continue
        out[(str(pipeline_name), step_instance_id, _attempt(attrs.get("step_attempt", 1)))].add(str(session_id))
    return out


def _infer_session_id(
    record: Record,
    sessions_by_step: dict[tuple[str, str, int], set[str]],
    sub_step_instances: dict[tuple[str, str, str, str], str],
) -> str | None:
    attrs = record.get("attributes", {})
    pipeline_name = attrs.get("pipeline_name")
    step_instance_id = _step_instance_id(record, sub_step_instances)
    if not pipeline_name or step_instance_id is None:
        return None
    key = (str(pipeline_name), step_instance_id, _attempt(attrs.get("step_attempt", 1)))
    matches = sessions_by_step.get(key, set())
    if len(matches) != 1:
        return None
    return next(iter(matches))


def _record_session_id(
    record: Record,
    sessions_by_step: dict[tuple[str, str, int], set[str]],
    sub_step_instances: dict[tuple[str, str, str, str], str],
) -> str | None:
    attrs = record.get("attributes", {})
    value = attrs.get("session_id") or _telemetry_session_id(record)
    if value is not None:
        return str(value)
    return _infer_session_id(record, sessions_by_step, sub_step_instances)


def _resource_session_id(record: Record) -> str | None:
    value = record.get("resource", {}).get("session.id")
    if value is None:
        return None
    return _strip_session_prefix(value)


def _telemetry_session_id(record: Record) -> str | None:
    attrs = record.get("attributes", {})
    value = record.get("resource", {}).get("session.id") or attrs.get("gen_ai.session.id")
    if value is None:
        return None
    return _strip_session_prefix(value)


def _strip_session_prefix(value: Any) -> str:
    text = str(value)
    prefix = "iac_sess_"
    if text.startswith(prefix):
        return text[len(prefix) :]
    return text


def _agent_rounds(step_span: Record, children_by_parent: dict[str, list[Record]]) -> list[dict[str, Any]]:
    out = []
    for child in _descendants_within_step(step_span.get("span_id", ""), children_by_parent):
        if child.get("name") != "react step":
            continue
        round_value = _attempt(child.get("attributes", {}).get("gen_ai.react.round", 1))
        out.append(
            {
                "round": round_value,
                "record_id": child["id"],
                "span_id": child.get("span_id", ""),
                "attributes": child.get("attributes", {}),
                "children": [
                    {
                        "record_id": grandchild["id"],
                        "span_id": grandchild.get("span_id", ""),
                        "name": grandchild.get("name"),
                        "kind": grandchild.get("kind"),
                        "attributes": grandchild.get("attributes", {}),
                    }
                    for grandchild in children_by_parent.get(child.get("span_id", ""), [])
                ],
            }
        )
    return sorted(out, key=lambda item: item["round"])


def _descendants_within_step(parent_id: str, children_by_parent: dict[str, list[Record]]) -> list[Record]:
    out: list[Record] = []
    stack = list(children_by_parent.get(parent_id, []))
    while stack:
        record = stack.pop(0)
        out.append(record)
        if _is_pipeline_step_span(record):
            continue
        stack.extend(children_by_parent.get(record.get("span_id", ""), []))
    return out
