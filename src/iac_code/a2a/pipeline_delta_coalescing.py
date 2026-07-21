from __future__ import annotations

import copy
from typing import Any

_DELTA_EVENT_TYPES = {"text_delta", "thinking_delta"}
_VOLATILE_ENVELOPE_KEYS = {"eventId", "sequence", "createdAt"}


def coalesce_pipeline_delta_envelopes(envelopes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    coalesced: list[dict[str, Any]] = []
    previous_identity: dict[str, Any] | None = None

    for source in envelopes:
        current = copy.deepcopy(source)
        identity = _coalescing_identity(current)
        if coalesced and identity is not None and identity == previous_identity:
            coalesced[-1]["data"]["text"] += current["data"]["text"]
            coalesced[-1]["sequence"] = current["sequence"]
            continue
        coalesced.append(current)
        previous_identity = identity

    return coalesced


def coalesce_pipeline_delta_envelopes_by_source(envelopes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    source_envelopes: dict[tuple[Any, ...], list[dict[str, Any]]] = {}
    for envelope in envelopes:
        source_envelopes.setdefault(_source_identity(envelope), []).append(envelope)

    coalesced = [
        envelope for source in source_envelopes.values() for envelope in coalesce_pipeline_delta_envelopes(source)
    ]
    return sorted(coalesced, key=lambda envelope: _sequence(envelope))


def _coalescing_identity(envelope: dict[str, Any]) -> dict[str, Any] | None:
    if envelope.get("eventType") not in _DELTA_EVENT_TYPES:
        return None
    data = envelope.get("data")
    if not isinstance(data, dict) or not isinstance(data.get("text"), str):
        return None
    identity = {
        key: copy.deepcopy(value)
        for key, value in envelope.items()
        if key not in _VOLATILE_ENVELOPE_KEYS and key != "data"
    }
    identity["data"] = {key: copy.deepcopy(value) for key, value in data.items() if key != "text"}
    return identity


def _source_identity(envelope: dict[str, Any]) -> tuple[Any, ...]:
    candidate = envelope.get("candidate")
    if not isinstance(candidate, dict):
        return (envelope.get("pipelineRunId"), "default")
    return (
        envelope.get("pipelineRunId"),
        "candidate",
        candidate.get("runId"),
        candidate.get("id"),
        candidate.get("index"),
        candidate.get("attempt"),
    )


def _sequence(envelope: dict[str, Any]) -> int:
    value = envelope.get("sequence")
    return value if isinstance(value, int) else 0
