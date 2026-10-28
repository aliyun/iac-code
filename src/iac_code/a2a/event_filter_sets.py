"""Named outbound event exclusions; canonical events and local sinks stay complete.

Consumer audit (2026-10-28): ROS frontend 9fa1dc5c, ros-ai-agent 1b81e15e,
and alibabacloud-ros-agent ca98848a. See the local docs/sse_event_matrix.html.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from a2a.types import Role, SendMessageRequest, Task, TaskArtifactUpdateEvent, TaskState, TaskStatusUpdateEvent
from a2a.utils.errors import InvalidParamsError
from google.protobuf.json_format import MessageToDict, ParseDict

NONE = "NONE"
USAGE = "USAGE"
SKILL = "SKILL"
EVENT_FILTER_SET_METADATA_KEY = "eventFilterSet"

# Gateway dependencies cannot be removed by any current or future filter set.
# Protocol states/IDs, messages and root recovery metadata survive separately.
# ros-ai-agent/client/a2a_event.py:is_input_required and iac_code_session_id.
GATEWAY_REQUIRED_PIPELINE_EVENTS = frozenset({"input_required"})
GATEWAY_REQUIRED_METADATA_KEYS = frozenset({"iacCodeSessionId"})
IDENTITY_METADATA_KEYS = frozenset({"iacCodeSessionId", "pipelineName", "pipelineRunId", "scope"})


@dataclass(frozen=True)
class A2AEventFilterSet:
    name: str
    # Names match ordinary metadata keys, pipeline eventType, or tool.<status>.
    # Only listed events are removed; unknown and future signals are retained.
    events: frozenset[str] = frozenset()


EVENT_FILTER_SETS = {
    NONE: A2AEventFilterSet(NONE),
    USAGE: A2AEventFilterSet(USAGE, frozenset({"usage", "context_usage"})),
    SKILL: A2AEventFilterSet(
        SKILL,
        frozenset({"usage", "context_usage", "thinking", "thinking_delta", "tool.input_delta"}),
    ),
}


def _metadata_mapping(metadata: Any) -> Mapping[str, Any]:
    if metadata is not None and hasattr(metadata, "DESCRIPTOR"):
        return MessageToDict(metadata, preserving_proto_field_name=False)
    if isinstance(metadata, Mapping):
        return metadata
    return {}


def _filter_set_value(metadata: Any) -> Any:
    iac_code = _metadata_mapping(metadata).get("iac_code")
    if not isinstance(iac_code, Mapping):
        return None
    return iac_code.get(EVENT_FILTER_SET_METADATA_KEY, iac_code.get("event_filter_set"))


def resolve_event_filter_set(metadata: Any = None) -> A2AEventFilterSet:
    value = _filter_set_value(metadata)
    if value is None:
        return EVENT_FILTER_SETS[NONE]
    if not isinstance(value, str) or value not in EVENT_FILTER_SETS:
        raise InvalidParamsError("Unsupported A2A event filter set.")
    return EVENT_FILTER_SETS[value]


def message_event_filter_set(params: Any) -> A2AEventFilterSet:
    """Honor request metadata first, falling back to the message's metadata."""

    metadata = getattr(params, "metadata", None)
    if _filter_set_value(metadata) is None:
        metadata = getattr(getattr(params, "message", None), "metadata", None)
    return resolve_event_filter_set(metadata)


def filter_set_message_request(params: SendMessageRequest, filter_set: A2AEventFilterSet) -> SendMessageRequest:
    """Record a request-level selection in message history for re-subscription."""

    if _filter_set_value(getattr(params, "metadata", None)) is None:
        return params
    result = SendMessageRequest()
    result.CopyFrom(params)
    metadata = MessageToDict(result.message.metadata, preserving_proto_field_name=False)
    iac_code = metadata.get("iac_code")
    if not isinstance(iac_code, dict):
        iac_code = {}
        metadata["iac_code"] = iac_code
    iac_code.pop("event_filter_set", None)
    iac_code[EVENT_FILTER_SET_METADATA_KEY] = filter_set.name
    result.message.metadata.Clear()
    ParseDict(metadata, result.message.metadata)
    return result


def task_event_filter_set(task: Task) -> A2AEventFilterSet:
    """SubscribeToTask inherits the latest user request's filter set.

    Read canonical user history rather than mutating the shared Task or keeping
    a process-only registry. An omitted selector means NONE, including after a
    previously filtered request. Message streams (including cold recovery)
    select their own request-scoped view independently of this history.
    """

    for message in reversed(task.history):
        if message.role == Role.ROLE_USER:
            return resolve_event_filter_set(message.metadata)
    return resolve_event_filter_set(task.metadata)


class A2AEventDeliveryFilter:
    """Remove named events after task persistence, immediately before delivery."""

    def __init__(self, filter_set: A2AEventFilterSet) -> None:
        self.filter_set = filter_set
        self._first_event = True
        self._delivered_session_ids: set[str] = set()

    def project(self, event: Any) -> Any | None:
        if not self.filter_set.events:
            return event
        if isinstance(event, TaskArtifactUpdateEvent):
            self._first_event = False
            return event
        if not isinstance(event, (Task, TaskStatusUpdateEvent)):
            # Permission ACK Messages and future SDK control carriers survive.
            self._first_event = False
            return event

        payload = MessageToDict(event, preserving_proto_field_name=False)
        metadata = payload.get("metadata")
        original_iac = metadata.get("iac_code") if isinstance(metadata, dict) else None
        session_id = original_iac.get("iacCodeSessionId") if isinstance(original_iac, dict) else None
        new_session_binding = (
            isinstance(session_id, str) and bool(session_id) and session_id not in self._delivered_session_ids
        )
        suppressed = False
        if isinstance(original_iac, dict):
            filtered = self._filter_iac_metadata(original_iac)
            suppressed = filtered != original_iac
            if filtered:
                metadata["iac_code"] = filtered
            else:
                metadata.pop("iac_code", None)
            if original_iac.get("mcpWarning") and "mcpWarning" not in filtered:
                payload.get("status", {}).pop("message", None)

        if (
            isinstance(event, TaskStatusUpdateEvent)
            and suppressed
            and not self._first_event
            and not new_session_binding
            and self._is_empty_working_update(payload)
        ):
            return None

        self._first_event = False
        if isinstance(session_id, str) and session_id:
            self._delivered_session_ids.add(session_id)
        result = type(event)()
        ParseDict(payload, result)
        return result

    def _filter_iac_metadata(self, metadata: dict[str, Any]) -> dict[str, Any]:
        excluded = self.filter_set.events
        result = {
            key: value
            for key, value in metadata.items()
            if key not in excluded or key in GATEWAY_REQUIRED_METADATA_KEYS
        }
        tool = result.get("tool")
        if isinstance(tool, dict) and f"tool.{tool.get('status')}" in excluded:
            result.pop("tool", None)
        permission = metadata.get("permission")
        if isinstance(permission, dict) and permission.get("pending") is True:
            result["permission"] = permission
        pipeline = metadata.get("pipeline")
        if isinstance(pipeline, dict):
            if self._retain_pipeline_event(pipeline):
                result["pipeline"] = pipeline
            else:
                result.pop("pipeline", None)
        batch = metadata.get("pipelineBatch")
        if isinstance(batch, dict) and isinstance(batch.get("events"), list):
            events = [event for event in batch["events"] if self._retain_pipeline_event(event)]
            if events:
                result["pipelineBatch"] = {**batch, "events": events}
            else:
                result.pop("pipelineBatch", None)
        return result

    def _retain_pipeline_event(self, event: Any) -> bool:
        if not isinstance(event, dict):
            return True
        event_type = event.get("eventType")
        if not isinstance(event_type, str) or event_type in GATEWAY_REQUIRED_PIPELINE_EVENTS:
            return True
        return event_type not in self.filter_set.events

    @staticmethod
    def _is_empty_working_update(payload: dict[str, Any]) -> bool:
        status = payload.get("status", {})
        if status.get("state") != TaskState.Name(TaskState.TASK_STATE_WORKING) or status.get("message"):
            return False
        metadata = payload.get("metadata", {})
        if any(key != "iac_code" for key in metadata) or status.get("metadata"):
            return False
        iac_code = metadata.get("iac_code", {})
        # New identity bindings and every recovery watermark survive.
        return not any(key not in IDENTITY_METADATA_KEYS for key in iac_code)
