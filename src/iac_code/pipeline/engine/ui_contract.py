"""Shared string contracts for pipeline terminal UI integration."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from enum import Enum
from typing import Any

_CANDIDATE_INDEX_PATTERNS = (
    re.compile(r"(?:方案|候选|candidate|plan)\s*#?\s*(\d+)", re.IGNORECASE),
    re.compile(r"^(?:我)?(?:要|选|选择)\s*(\d+)$"),
)


class PipelineStepType(str, Enum):
    """Pipeline step types that affect execution or terminal rendering."""

    NORMAL = "normal"
    PARALLEL_SUB_PIPELINE = "parallel_sub_pipeline"


class PipelineUiMode(str, Enum):
    """Pipeline UI modes consumed by terminal renderers."""

    CANDIDATE_SELECTION = "candidate_selection"


@dataclass(frozen=True)
class SelectedCandidate:
    """Structured payload returned by the candidate selection UI."""

    selected_candidate_name: str
    selected_candidate_index: int | None = None


def encode_selected_candidate(candidate_name: str, candidate_index: int | None) -> str:
    return json.dumps(
        {
            "selected_candidate_name": candidate_name,
            "selected_candidate_index": candidate_index,
        },
        ensure_ascii=False,
    )


def parse_selected_candidate(value: Any) -> SelectedCandidate | None:
    if isinstance(value, dict):
        name = value.get("selected_candidate_name")
        index = value.get("selected_candidate_index")
    elif isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return None
        try:
            decoded = json.loads(stripped)
        except json.JSONDecodeError:
            parsed_index = _parse_candidate_index_hint(stripped)
            if parsed_index is not None:
                return SelectedCandidate(selected_candidate_name="", selected_candidate_index=parsed_index)
            return SelectedCandidate(selected_candidate_name=stripped, selected_candidate_index=None)
        if not isinstance(decoded, dict):
            return None
        name = decoded.get("selected_candidate_name")
        index = decoded.get("selected_candidate_index")
    else:
        return None

    if index is not None and not isinstance(index, int):
        return None
    if isinstance(name, str) and name.strip():
        candidate_name = name.strip()
    elif index is not None:
        candidate_name = ""
    else:
        return None
    return SelectedCandidate(selected_candidate_name=candidate_name, selected_candidate_index=index)


def _parse_candidate_index_hint(value: str) -> int | None:
    for pattern in _CANDIDATE_INDEX_PATTERNS:
        match = pattern.search(value)
        if match is None:
            continue
        try:
            return int(match.group(1))
        except ValueError:
            return None
    return None
