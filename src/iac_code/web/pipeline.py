"""Web adapters for A2A pipeline recovery state."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

_MAX_AFTER_SEQUENCE_DIGITS = 20


class PipelineStateRequestError(ValueError):
    """Raised when a web pipeline state request is malformed."""


class PipelineCandidateSelectionRequestError(ValueError):
    """Raised when a web candidate selection request is malformed."""


class PipelineStateNotFoundError(Exception):
    """Raised when A2A recovery cannot produce public pipeline state."""


@dataclass(frozen=True)
class PipelineCandidateSelection:
    """Validated candidate selection ready for the pipeline runner input contract."""

    session_id: str
    candidate_name: str
    candidate_index: int | None
    parameter_overrides: dict[str, Any]
    encoded_input: str

    def metadata_payload(self, *, context_id: str | None = None, task_id: str | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "mode": "pipeline",
            "candidateName": self.candidate_name,
            "candidateIndex": self.candidate_index,
            "parameterOverrides": dict(self.parameter_overrides),
        }
        if context_id is not None:
            payload["contextId"] = context_id
        if task_id is not None:
            payload["taskId"] = task_id
        return payload


class PipelineRecoveryService(Protocol):
    async def get_state(
        self,
        *,
        context_id: str | None = None,
        task_id: str | None = None,
        after_sequence: int | None = None,
    ) -> dict[str, Any]: ...


def parse_after_sequence(value: str | None) -> int | None:
    """Parse the A2A-compatible afterSequence query value."""
    if value is None or value == "":
        return None
    if len(value) > _MAX_AFTER_SEQUENCE_DIGITS:
        raise PipelineStateRequestError("afterSequence must be a non-negative integer")
    if value.isascii() and value.isdecimal():
        try:
            return int(value)
        except ValueError:
            pass
    raise PipelineStateRequestError("afterSequence must be a non-negative integer")


def parse_candidate_selection_body(data: Mapping[str, Any]) -> PipelineCandidateSelection:
    """Validate and encode a web candidate selection request."""
    from iac_code.pipeline.engine.ui_contract import encode_selected_candidate

    session_id = _required_string(data, "sessionId")
    candidate_name = _optional_string(data, "candidateName") or ""
    candidate_index = _optional_candidate_index(data, "candidateIndex")
    parameter_overrides = _optional_parameter_overrides(data, "parameterOverrides")
    if not candidate_name.strip() and candidate_index is None:
        raise PipelineCandidateSelectionRequestError("candidateName or candidateIndex is required")
    candidate_name = candidate_name.strip()
    return PipelineCandidateSelection(
        session_id=session_id,
        candidate_name=candidate_name,
        candidate_index=candidate_index,
        parameter_overrides=parameter_overrides,
        encoded_input=encode_selected_candidate(candidate_name, candidate_index, parameter_overrides),
    )


def create_a2a_pipeline_recovery_service() -> PipelineRecoveryService:
    """Build the production recovery service over local A2A persistence."""
    from iac_code.a2a.metrics import NoOpA2AMetrics
    from iac_code.a2a.persistence import A2APersistenceStore
    from iac_code.a2a.pipeline_recovery import A2APipelineRecoveryService
    from iac_code.a2a.task_store import A2ATaskStore
    from iac_code.config import get_config_dir

    persistence = A2APersistenceStore(get_config_dir() / "a2a")
    task_store = A2ATaskStore(metrics=NoOpA2AMetrics(), persistence=persistence)
    return A2APipelineRecoveryService(task_store=task_store)


async def pipeline_state_from_query(
    query_params: Mapping[str, str],
    *,
    recovery_service: PipelineRecoveryService | None = None,
) -> dict[str, Any]:
    """Resolve public A2A pipeline state from web query parameters."""
    context_id = _non_empty_query_value(query_params, "contextId")
    task_id = _non_empty_query_value(query_params, "taskId")
    if context_id is None and task_id is None:
        raise PipelineStateRequestError("contextId or taskId is required")
    context_id = _validated_protocol_id(context_id, "contextId")
    task_id = _validated_protocol_id(task_id, "taskId")
    after_sequence = parse_after_sequence(query_params.get("afterSequence"))
    service = recovery_service or create_a2a_pipeline_recovery_service()
    try:
        return await service.get_state(
            context_id=context_id,
            task_id=task_id,
            after_sequence=after_sequence,
        )
    except ValueError as exc:
        raise PipelineStateNotFoundError("pipeline state not found") from exc


def _non_empty_query_value(query_params: Mapping[str, str], key: str) -> str | None:
    value = query_params.get(key)
    return value or None


def _required_string(data: Mapping[str, Any], key: str) -> str:
    if key not in data:
        raise PipelineCandidateSelectionRequestError(f"{key} is required")
    value = data[key]
    if not isinstance(value, str) or not value.strip():
        raise PipelineCandidateSelectionRequestError(f"{key} must be a string")
    return value


def _optional_string(data: Mapping[str, Any], key: str) -> str | None:
    if key not in data or data[key] is None:
        return None
    value = data[key]
    if not isinstance(value, str):
        raise PipelineCandidateSelectionRequestError(f"{key} must be a string")
    return value


def _optional_candidate_index(data: Mapping[str, Any], key: str) -> int | None:
    if key not in data or data[key] is None:
        return None
    value = data[key]
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise PipelineCandidateSelectionRequestError(f"{key} must be a non-negative integer")
    return value


def _optional_parameter_overrides(data: Mapping[str, Any], key: str) -> dict[str, Any]:
    if key not in data:
        return {}
    value = data[key]
    if not isinstance(value, Mapping):
        raise PipelineCandidateSelectionRequestError(f"{key} must be an object")
    overrides: dict[str, Any] = {}
    for override_key, override_value in value.items():
        if not isinstance(override_key, str) or not override_key.strip():
            raise PipelineCandidateSelectionRequestError(f"{key} must use string keys")
        overrides[override_key.strip()] = override_value
    return overrides


def _validated_protocol_id(value: str | None, field: str) -> str | None:
    if value is None:
        return None
    from iac_code.a2a.types import validate_protocol_id

    try:
        return validate_protocol_id(value)
    except ValueError as exc:
        raise PipelineStateRequestError(f"{field} is invalid") from exc
