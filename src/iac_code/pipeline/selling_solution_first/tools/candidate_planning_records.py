"""Pipeline-local helpers for the active Step 1 candidate batch.

The latest successful ``show_architecture_plan`` call is the authoritative outline batch.  Rich
detail calls after that record belong to that batch; older calls remain transcript history only.
This module deliberately has no engine-level state or persistence of its own.
"""

from __future__ import annotations

import copy
from typing import Any


class CandidateOutlineBatch:
    """The latest successful lightweight outline batch.

    Keep this as a small ordinary object instead of a dataclass: pipeline-local tool modules are
    loaded with ``exec_module`` under transient names that are not inserted into ``sys.modules``,
    while Python 3.12 dataclasses resolve postponed annotations through that module registry.
    """

    __slots__ = ("candidate_set_id", "sequence", "candidates")

    def __init__(self, *, candidate_set_id: str, sequence: int, candidates: list[dict[str, str]]) -> None:
        self.candidate_set_id = candidate_set_id
        self.sequence = sequence
        self.candidates = candidates


def latest_candidate_outline_batch(records: list[dict[str, Any]]) -> CandidateOutlineBatch | None:
    """Return the latest valid successful outline batch from ordered v2 records."""

    latest: CandidateOutlineBatch | None = None
    for position, record in enumerate(records):
        if not isinstance(record, dict) or record.get("is_error"):
            continue
        if record.get("tool_name") != "show_architecture_plan":
            continue
        tool_input = record.get("input")
        raw_candidates = tool_input.get("candidates") if isinstance(tool_input, dict) else None
        candidates = normalize_outline_candidates(raw_candidates)
        if candidates is None:
            # Ignore records produced by the old per-candidate graph contract.
            continue
        sequence = _record_sequence(record, position)
        recorded_candidate_set_id = record.get("candidate_set_id")
        candidate_set_id = str(recorded_candidate_set_id).strip() if isinstance(recorded_candidate_set_id, str) else ""
        record_id = record.get("record_id")
        if not candidate_set_id:
            candidate_set_id = str(record_id).strip() if isinstance(record_id, str) else ""
        if not candidate_set_id:
            candidate_set_id = f"outline-{sequence}"
        # An identical repeated call is recorded as a successful idempotent observation with the
        # original candidateSetId.  It must not move the active batch boundary forward, otherwise
        # details already produced for that batch would be incorrectly invalidated.
        if latest is not None and latest.candidate_set_id == candidate_set_id and latest.candidates == candidates:
            continue
        latest = CandidateOutlineBatch(
            candidate_set_id=candidate_set_id,
            sequence=sequence,
            candidates=candidates,
        )
    return latest


def normalize_outline_candidates(value: Any) -> list[dict[str, str]] | None:
    if not isinstance(value, list) or not 1 <= len(value) <= 3:
        return None
    normalized: list[dict[str, str]] = []
    names: set[str] = set()
    for item in value:
        if not isinstance(item, dict):
            return None
        candidate: dict[str, str] = {}
        for field in ("candidate_name", "summary", "total_cost", "key_tradeoff"):
            # Old persisted outline records remain readable, but schemas expose only total_cost.
            raw = item.get(field, item.get("total_monthly_cost") if field == "total_cost" else None)
            if not isinstance(raw, str) or not raw.strip():
                return None
            candidate[field] = raw.strip()
        if candidate["candidate_name"] in names:
            return None
        names.add(candidate["candidate_name"])
        normalized.append(candidate)
    return normalized


def _rename_cost_field(value: dict[str, Any], old: str, new: str) -> None:
    if old in value:
        if new not in value:
            value[new] = value[old]
        del value[old]


def normalize_inventory_costs(value: Any) -> Any:
    """Copy legacy resource costs into the period-neutral planning contract."""
    inventory = copy.deepcopy(value)
    if isinstance(inventory, list):
        for resource in inventory:
            if isinstance(resource, dict):
                _rename_cost_field(resource, "rough_monthly_cost", "rough_cost")
    return inventory


def normalize_candidate_costs(value: Any) -> Any:
    """Migrate only known planning fields, preserving values and canonical-key precedence."""
    candidate = copy.deepcopy(value)
    if not isinstance(candidate, dict):
        return candidate
    if "resource_inventory" in candidate:
        candidate["resource_inventory"] = normalize_inventory_costs(candidate["resource_inventory"])
    cost = candidate.get("rough_cost")
    if isinstance(cost, dict):
        _rename_cost_field(cost, "monthly_range", "cost_range")
        items = cost.get("items")
        if isinstance(items, list):
            for item in items:
                if isinstance(item, dict):
                    _rename_cost_field(item, "monthly_cost", "cost")
    return candidate


def normalize_solution_selection_costs(value: Any) -> None:
    """Normalize restored candidates in place without changing context versions or stale flags."""
    if not isinstance(value, dict):
        return
    candidates = value.get("candidates")
    if isinstance(candidates, list):
        value["candidates"] = [normalize_candidate_costs(candidate) for candidate in candidates]
    if "selected_candidate" in value:
        value["selected_candidate"] = normalize_candidate_costs(value["selected_candidate"])


def latest_candidate_detail_records(
    records: list[dict[str, Any]],
    batch: CandidateOutlineBatch,
) -> dict[int, dict[str, Any]]:
    """Return each index's latest detail attempt after the active outline batch.

    Failed attempts intentionally replace earlier successful attempts.  A bad correction must not
    silently fall back to stale detail that the user saw before the correction.
    """

    latest: dict[int, dict[str, Any]] = {}
    for position, record in enumerate(records):
        if not isinstance(record, dict) or record.get("tool_name") != "show_candidate_detail":
            continue
        if _record_sequence(record, position) <= batch.sequence:
            continue
        record_candidate_set_id = record.get("candidate_set_id")
        if (
            isinstance(record_candidate_set_id, str)
            and record_candidate_set_id
            and record_candidate_set_id != batch.candidate_set_id
        ):
            continue
        tool_input = record.get("input")
        index = tool_input.get("candidate_index") if isinstance(tool_input, dict) else None
        if isinstance(index, bool) or not isinstance(index, int) or index < 0:
            continue
        latest[index] = record
    return latest


def first_missing_candidate_detail_index(
    records: list[dict[str, Any]],
    batch: CandidateOutlineBatch,
) -> int | None:
    latest = latest_candidate_detail_records(records, batch)
    for index, outline in enumerate(batch.candidates):
        record = latest.get(index)
        if not detail_record_matches(record, index=index, candidate_name=outline["candidate_name"]):
            return index
    return None


def detail_record_matches(record: Any, *, index: int, candidate_name: str) -> bool:
    if not isinstance(record, dict) or record.get("is_error"):
        return False
    tool_input = record.get("input")
    if not isinstance(tool_input, dict):
        return False
    return tool_input.get("candidate_index") == index and tool_input.get("candidate_name") == candidate_name


def _record_sequence(record: dict[str, Any], position: int) -> int:
    sequence = record.get("sequence")
    return sequence if isinstance(sequence, int) and not isinstance(sequence, bool) else position + 1


__all__ = [
    "CandidateOutlineBatch",
    "detail_record_matches",
    "first_missing_candidate_detail_index",
    "latest_candidate_detail_records",
    "latest_candidate_outline_batch",
    "normalize_outline_candidates",
    "normalize_inventory_costs",
    "normalize_candidate_costs",
    "normalize_solution_selection_costs",
]
