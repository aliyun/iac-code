"""Translate public A2A wire events into standard AG-UI events."""

from __future__ import annotations

import hashlib
import json
import math
import time
import uuid
from collections.abc import Iterable, Mapping
from datetime import datetime, timedelta, timezone
from typing import Any, cast

from ag_ui.core import (
    ActivitySnapshotEvent,
    CustomEvent,
    Interrupt,
    ReasoningEndEvent,
    ReasoningMessageContentEvent,
    ReasoningMessageEndEvent,
    ReasoningMessageStartEvent,
    ReasoningStartEvent,
    StepFinishedEvent,
    StepStartedEvent,
    TextMessageContentEvent,
    TextMessageEndEvent,
    TextMessageStartEvent,
    TokenUsage,
    ToolCallArgsEvent,
    ToolCallEndEvent,
    ToolCallResultEvent,
    ToolCallStartEvent,
)

from iac_code.agui.errors import normalize_agui_language
from iac_code.i18n import translate_message

# Keep the external AG-UI stream focused on Pipeline information that has no
# equivalent standard AG-UI event and is useful to a client.  The A2A stream
# remains full fidelity; this allowlist only controls its AG-UI projection.
_AGUI_PIPELINE_CUSTOM_EVENT_TYPES = frozenset(
    {
        "backup_blocked",
        "candidate_completed",
        "candidate_detail_shown",
        "candidate_failed",
        "candidate_interrupted",
        "candidate_restart_requested",
        "candidate_selected",
        "candidate_started",
        "candidate_step_failed",
        "cleanup_completed",
        "cleanup_failed",
        "cleanup_progress",
        "cleanup_started",
        "context_compacted",
        "context_compaction_failed",
        "context_compaction_started",
        "diagram_shown",
        "fields_marked_stale",
        "mcp_status",
        "pipeline_completed",
        "pipeline_error",
        "pipeline_resumed",
        "pipeline_started",
        "pipeline_warning",
        "rollback_completed",
        "rollback_triggered",
        "stack_current_changed",
        "stack_instances_progress",
        "stack_progress",
        "step_failed",
        "sub_pipeline_completed",
        "sub_pipeline_started",
        "sub_step_failed",
        "tool_progress",
    }
)


def timestamp_ms(timestamp: float | None = None) -> int:
    return int((time.time() if timestamp is None else timestamp) * 1000)


def normalize_a2a_state(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return value.strip().lower().removeprefix("task_state_").replace("_", "-")


def a2a_result(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        return {}
    result = payload.get("result")
    if isinstance(result, Mapping):
        for key in ("task", "statusUpdate", "artifactUpdate", "message"):
            nested = result.get(key)
            if isinstance(nested, Mapping):
                return dict(nested)
        return dict(result)
    return dict(payload)


def a2a_task_id(payload: Any) -> str | None:
    result = a2a_result(payload)
    for key in ("taskId", "id"):
        value = result.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def a2a_context_id(payload: Any) -> str | None:
    result = a2a_result(payload)
    value = result.get("contextId")
    return value if isinstance(value, str) and value else None


def a2a_state(payload: Any) -> str:
    result = a2a_result(payload)
    status = result.get("status")
    if not isinstance(status, Mapping):
        return ""
    return normalize_a2a_state(status.get("state"))


def a2a_iac_code_metadata(payload: Any) -> dict[str, Any]:
    result = a2a_result(payload)
    metadata = result.get("metadata")
    if not isinstance(metadata, Mapping):
        return {}
    value = metadata.get("iac_code")
    return dict(value) if isinstance(value, Mapping) else {}


def a2a_iac_code_session_id(payload: Any) -> str | None:
    metadata = a2a_iac_code_metadata(payload)
    candidates: list[Any] = [metadata.get("iacCodeSessionId")]
    pipeline = metadata.get("pipeline")
    if isinstance(pipeline, Mapping):
        candidates.append(pipeline.get("iacCodeSessionId"))
    pipeline_batch = metadata.get("pipelineBatch")
    if isinstance(pipeline_batch, Mapping) and isinstance(pipeline_batch.get("events"), list):
        candidates.extend(
            event.get("iacCodeSessionId") for event in pipeline_batch["events"] if isinstance(event, Mapping)
        )
    return next((value for value in candidates if isinstance(value, str) and value), None)


def a2a_input(payload: Any) -> dict[str, Any] | None:
    values = a2a_inputs(payload)
    return values[0] if values else None


def a2a_inputs(payload: Any) -> list[dict[str, Any]]:
    """Return direct and task-snapshot input projections without duplicates."""
    metadata = a2a_iac_code_metadata(payload)
    candidates: list[Any] = [metadata.get("input")]
    pending_permissions = metadata.get("pendingPermissions")
    if isinstance(pending_permissions, list):
        candidates.extend(pending_permissions)
    output: list[dict[str, Any]] = []
    seen: set[str] = set()
    for candidate in candidates:
        if not isinstance(candidate, Mapping) or candidate.get("required") is not True:
            continue
        value = dict(candidate)
        input_id = str(value.get("inputId") or "")
        dedupe_key = input_id or json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        output.append(value)
    return output


def a2a_sideband_input_ids(payload: Any) -> set[str]:
    """Return permission ids exposed as concurrent Pipeline waits."""

    metadata = a2a_iac_code_metadata(payload)
    output: set[str] = set()
    direct = metadata.get("input")
    if isinstance(direct, Mapping) and (
        direct.get("scope") == "candidate" or isinstance(direct.get("subPipelineId"), str)
    ):
        input_id = direct.get("inputId")
        if isinstance(input_id, str) and input_id:
            output.add(input_id)
    pending_permissions = metadata.get("pendingPermissions")
    if not isinstance(pending_permissions, list):
        return output
    output.update(
        str(value["inputId"])
        for value in pending_permissions
        if isinstance(value, Mapping) and isinstance(value.get("inputId"), str) and value["inputId"]
    )
    return output


def interrupt_from_a2a(value: Mapping[str, Any], *, ttl_seconds: int) -> Interrupt:
    kind = str(value.get("kind") or "input_required")
    language = normalize_agui_language(value.get("language"))
    input_id = str(value.get("inputId") or f"input-{uuid.uuid4().hex}")
    tool_use_id = _string(value.get("toolUseId"))
    raw_options = value.get("options")
    options = _standard_options(
        raw_options if isinstance(raw_options, list) else [],
        pipeline=kind == "candidate_selection",
    )
    if kind == "permission":
        schema = {
            "type": "object",
            "properties": {"decision": {"type": "string", "enum": ["allow_once", "deny"]}},
            "required": ["decision"],
            "additionalProperties": False,
        }
        message = str(
            value.get("prompt") or value.get("title") or translate_message("Permission required", language=language)
        )
        reason = "tool_call"
    else:
        allow_free_text = bool(value.get("allowFreeText")) or not options
        schema = _selection_schema(options, allow_free_text=allow_free_text)
        message = _selection_message(
            str(value.get("prompt") or translate_message("Input required", language=language)),
            options,
        )
        reason = "input_required"
    return Interrupt(
        id=input_id,
        reason=reason,
        message=message,
        tool_call_id=tool_use_id,
        response_schema=schema,
        expires_at=(datetime.now(timezone.utc) + timedelta(seconds=max(1, ttl_seconds)))
        .isoformat()
        .replace("+00:00", "Z"),
        metadata={"schemaVersion": 1, **dict(value), "standardOptions": options},
    )


class A2AEventMapper:
    """Stateful A2A-wire mapper that keeps AG-UI spans balanced."""

    def __init__(
        self,
        *,
        thread_id: str,
        run_id: str,
        open_pipeline_steps: set[str] | None = None,
        text_snapshot_digests: set[str] | None = None,
    ) -> None:
        self.thread_id = thread_id
        self.run_id = run_id
        self.current_message_id: str | None = None
        self.open_messages: set[str] = set()
        self.open_reasoning: set[str] = set()
        self.open_tools: set[str] = set()
        self.seen_tool_results: set[str] = set()
        self.usage: list[TokenUsage] = []
        self.seen_pipeline_event_ids: set[str] = set()
        # ``open_pipeline_steps`` is the durable A2A/Pipeline state.  AG-UI
        # STEP spans, however, belong to one RUN_STARTED/RUN_FINISHED pair and
        # cannot remain open across an interrupt.  Keep the two lifecycles
        # separate so an interrupted Pipeline can be resumed without emitting
        # an invalid AG-UI stream.
        self.open_pipeline_steps: set[str] = set(open_pipeline_steps or ())
        self.run_pipeline_steps: dict[str, str] = {}
        self.pipeline_run_id = f"pipeline-{thread_id}"
        self.last_pipeline_sequence = 0
        self.text_emitted = False
        self.text_snapshot_digests: set[str] = set(text_snapshot_digests or ())
        self._message_text_parts: dict[str, list[str]] = {}
        self._resume_prefix_checked: set[str] = set()

    def session_event(
        self,
        *,
        execution_id: str,
        context_id: str,
        task_id: str | None,
        ros_invocation_id: str,
        session_id: str | None,
    ) -> CustomEvent:
        return CustomEvent(
            name="iac-code.session.v1",
            value={
                "schemaVersion": 1,
                "threadId": self.thread_id,
                "aguiRunId": self.run_id,
                "executionId": execution_id,
                "contextId": context_id,
                "taskId": task_id,
                "rosInvocationId": ros_invocation_id,
                "sessionId": session_id,
            },
            timestamp=timestamp_ms(),
        )

    def map(
        self,
        payload: Any,
        *,
        include_pipeline: bool = True,
        include_status_text: bool = True,
    ) -> list[Any]:
        result = a2a_result(payload)
        iac_code = a2a_iac_code_metadata(payload)
        output: list[Any] = []

        if include_pipeline:
            pipeline_batch = iac_code.get("pipelineBatch")
            if isinstance(pipeline_batch, Mapping) and isinstance(pipeline_batch.get("events"), list):
                for envelope in pipeline_batch["events"]:
                    if isinstance(envelope, Mapping):
                        output.extend(self._map_pipeline(dict(envelope)))
            pipeline = iac_code.get("pipeline")
            if isinstance(pipeline, Mapping):
                output.extend(self._map_pipeline(dict(pipeline)))

        thinking = iac_code.get("thinking")
        if isinstance(thinking, Mapping):
            output.extend(self._map_thinking(thinking))
        tool = iac_code.get("tool")
        if isinstance(tool, Mapping):
            output.extend(self._map_tool(tool))
        usage = iac_code.get("usage")
        if isinstance(usage, Mapping):
            self._record_usage(usage)

        artifact = result.get("artifact")
        if isinstance(artifact, Mapping):
            output.append(
                CustomEvent(
                    name="iac-code.artifact.v1",
                    value={"schemaVersion": 1, **dict(artifact)},
                    timestamp=timestamp_ms(),
                )
            )

        text = _status_text(result) if include_status_text else ""
        history_message_id: str | None = None
        if include_status_text and not text and not self.text_emitted:
            text, history_message_id = _task_history_agent_text(result)
        assistant_final = iac_code.get("assistantFinal")
        duplicate_final = isinstance(assistant_final, Mapping) and assistant_final.get("complete") is True
        status_message_id = _status_message_id(result) or history_message_id
        if text and not (duplicate_final and self.text_emitted):
            output.extend(self._map_status_text(text, status_message_id))
        return output

    def _map_status_text(self, text: str, message_id: str | None) -> list[Any]:
        """Map live status text while removing a cumulative Resume prefix."""

        replay_key = message_id or self.current_message_id
        if replay_key is None or replay_key in self._resume_prefix_checked:
            return self._map_text(text, message_id)
        self._resume_prefix_checked.add(replay_key)

        prefix_length = _replayed_prefix_length(text, self.text_snapshot_digests)
        if prefix_length == 0:
            return self._map_text(text, message_id)

        # A resumed A2A agent can replay all text produced before an interrupt
        # and append the newly generated suffix in one status message. Keep the
        # full value as the next durable snapshot, but expose only the suffix as
        # an AG-UI delta. The persisted digest is sufficient to locate the old
        # prefix, so adapter state does not need to store conversation text.
        self._message_text_parts[replay_key] = [text]
        suffix = text[prefix_length:]
        if not suffix:
            return []
        return self._map_text(suffix, message_id, record_snapshot=False)

    def _map_text(
        self,
        text: str,
        message_id: str | None,
        *,
        allow_parallel: bool = False,
        record_snapshot: bool = True,
    ) -> list[Any]:
        output = self.close_reasoning()
        resolved_id = message_id or self.current_message_id or f"assistant-{uuid.uuid4().hex}"
        if resolved_id not in self.open_messages:
            if not allow_parallel and self.current_message_id is not None:
                output.extend(self.close_text(self.current_message_id))
            self.open_messages.add(resolved_id)
            output.append(TextMessageStartEvent(message_id=resolved_id, timestamp=timestamp_ms()))
        self.current_message_id = resolved_id
        output.append(TextMessageContentEvent(message_id=resolved_id, delta=text, timestamp=timestamp_ms()))
        if record_snapshot:
            self._message_text_parts.setdefault(resolved_id, []).append(text)
        self.text_emitted = True
        return output

    def _map_thinking(self, value: Mapping[str, Any], message_id: str | None = None) -> list[Any]:
        if value.get("type") != "raw_thinking":
            return []
        text = value.get("text")
        if not isinstance(text, str) or not text:
            return []
        reasoning_id = f"reasoning-{message_id or self.current_message_id or self.run_id}"
        output: list[Any] = []
        if reasoning_id not in self.open_reasoning:
            self.open_reasoning.add(reasoning_id)
            output.extend(
                [
                    ReasoningStartEvent(message_id=reasoning_id, timestamp=timestamp_ms()),
                    ReasoningMessageStartEvent(message_id=reasoning_id, role="reasoning", timestamp=timestamp_ms()),
                ]
            )
        output.append(ReasoningMessageContentEvent(message_id=reasoning_id, delta=text, timestamp=timestamp_ms()))
        return output

    def _map_tool(self, value: Mapping[str, Any]) -> list[Any]:
        status = str(value.get("status") or "")
        tool_id = _string(value.get("toolUseId")) or f"tool-{uuid.uuid4().hex}"
        name = _string(value.get("name")) or "tool"
        output: list[Any] = []
        if status in {"started", "input_complete"} and self.current_message_id is not None:
            self._finalize_text_snapshot(self.current_message_id)
        if status in {"started", "input_complete"} and tool_id not in self.open_tools:
            self.open_tools.add(tool_id)
            output.append(
                ToolCallStartEvent(
                    tool_call_id=tool_id,
                    tool_call_name=name,
                    parent_message_id=self.current_message_id,
                    timestamp=timestamp_ms(),
                )
            )
        if status == "input_complete":
            raw_input = value.get("toolInput")
            if raw_input is None:
                raw_input = {"summary": value.get("inputSummary")}
            output.extend(
                [
                    ToolCallArgsEvent(
                        tool_call_id=tool_id,
                        delta=json.dumps(raw_input, ensure_ascii=False, separators=(",", ":"), sort_keys=True),
                        timestamp=timestamp_ms(),
                    ),
                    ToolCallEndEvent(tool_call_id=tool_id, timestamp=timestamp_ms()),
                ]
            )
            self.open_tools.discard(tool_id)
        elif status in {"completed", "failed"}:
            if tool_id in self.seen_tool_results:
                return output
            self.seen_tool_results.add(tool_id)
            result = value.get("result")
            output.append(
                ToolCallResultEvent(
                    message_id=f"tool-result-{tool_id}",
                    tool_call_id=tool_id,
                    content=_json_text(result),
                    role="tool",
                    timestamp=timestamp_ms(),
                )
            )
        elif status == "progress":
            output.append(
                CustomEvent(
                    name="iac-code.tool-progress.v1",
                    value={"schemaVersion": 1, **dict(value)},
                    timestamp=timestamp_ms(),
                )
            )
        return output

    def map_resolved_tool(self, *, tool_call_id: str, content: Any) -> list[Any]:
        """Close an input-required tool span after A2A accepts its answer."""

        return self._map_tool(
            {
                "status": "completed",
                "toolUseId": tool_call_id,
                "result": content,
            }
        )

    def _map_pipeline(self, envelope: dict[str, Any]) -> list[Any]:
        event_id = _string(envelope.get("eventId"))
        if event_id and event_id in self.seen_pipeline_event_ids:
            return []
        sequence = _integer(envelope.get("sequence")) or 0
        if sequence and sequence <= self.last_pipeline_sequence:
            return []
        if event_id:
            self.seen_pipeline_event_ids.add(event_id)
        event_type = str(envelope.get("eventType") or "pipeline_event")
        self.last_pipeline_sequence = max(self.last_pipeline_sequence, sequence)
        step = envelope.get("candidateStep") or envelope.get("step")
        step_id = _string(step.get("id")) if isinstance(step, Mapping) else None
        output = self._map_pipeline_standard_event(envelope)
        output.extend(self._map_pipeline_step_lifecycle(envelope, event_type=event_type, step_id=step_id))
        custom = _pipeline_custom_event(envelope, event_type=event_type)
        if custom is not None:
            output.append(custom)
        return output

    def map_pipeline_recovery(self, state: Mapping[str, Any]) -> list[Any]:
        """Project a full A2A snapshot plus only post-disconnect incremental events."""

        snapshot = state.get("snapshot")
        if not isinstance(snapshot, Mapping):
            return []
        snapshot_value = dict(snapshot)
        pipeline_run_id = _string(snapshot_value.get("pipelineRunId")) or self.pipeline_run_id
        self.pipeline_run_id = pipeline_run_id
        snapshot_sequence = _integer(snapshot_value.get("lastSequence")) or 0
        output: list[Any] = [
            ActivitySnapshotEvent(
                message_id=f"pipeline:{pipeline_run_id}",
                activity_type="iac-code.pipeline.v1",
                content={"schemaVersion": 1, "pipelineRunId": pipeline_run_id, "snapshot": snapshot_value},
                replace=True,
                timestamp=timestamp_ms(),
            ),
        ]
        raw_events = state.get("events")
        if not isinstance(raw_events, list):
            self.last_pipeline_sequence = max(self.last_pipeline_sequence, snapshot_sequence)
            return output
        for raw_event in raw_events:
            if not isinstance(raw_event, Mapping):
                continue
            envelope = dict(raw_event)
            event_id = _string(envelope.get("eventId"))
            if event_id and event_id in self.seen_pipeline_event_ids:
                continue
            sequence = _integer(envelope.get("sequence")) or 0
            if not event_id and sequence and sequence <= self.last_pipeline_sequence:
                continue
            if event_id:
                self.seen_pipeline_event_ids.add(event_id)
            self.last_pipeline_sequence = max(
                self.last_pipeline_sequence,
                sequence,
            )
            output.extend(self._map_recovery_pipeline_event(envelope))
        self.last_pipeline_sequence = max(self.last_pipeline_sequence, snapshot_sequence)
        return output

    def _map_recovery_pipeline_event(self, envelope: dict[str, Any]) -> list[Any]:
        event_type = str(envelope.get("eventType") or "pipeline_event")
        step = envelope.get("candidateStep") or envelope.get("step")
        step_id = _string(step.get("id")) if isinstance(step, Mapping) else None
        output = self._map_pipeline_standard_event(envelope)
        output.extend(self._map_pipeline_step_lifecycle(envelope, event_type=event_type, step_id=step_id))
        custom = _pipeline_custom_event(envelope, event_type=event_type)
        if custom is not None:
            output.append(custom)
        return output

    def _map_pipeline_step_lifecycle(
        self,
        envelope: Mapping[str, Any],
        *,
        event_type: str,
        step_id: str | None,
    ) -> list[Any]:
        if not step_id:
            return []
        step_key = _pipeline_step_key(envelope, step_id)
        if event_type in {"step_started", "candidate_step_started", "input_received"}:
            self.open_pipeline_steps.add(step_key)
            if step_key in self.run_pipeline_steps:
                return []
            step_name = _pipeline_step_name(step_key, step_id=step_id)
            self.run_pipeline_steps[step_key] = step_name
            return [StepStartedEvent(step_name=step_name, timestamp=timestamp_ms())]
        if event_type in {
            "step_completed",
            "step_failed",
            "candidate_step_completed",
            "candidate_step_failed",
        }:
            if step_key not in self.open_pipeline_steps:
                return []
            self.open_pipeline_steps.remove(step_key)
            step_name = self.run_pipeline_steps.pop(step_key, None)
            if step_name is not None:
                return [StepFinishedEvent(step_name=step_name, timestamp=timestamp_ms())]
            # Recovery can observe a completion for a durable step before the
            # caller explicitly reopens it.  Emit a balanced zero-length span
            # rather than an orphan STEP_FINISHED event.
            step_name = _pipeline_step_name(step_key, step_id=step_id)
            now = timestamp_ms()
            return [
                StepStartedEvent(step_name=step_name, timestamp=now),
                StepFinishedEvent(step_name=step_name, timestamp=now),
            ]
        return []

    def reopen_pipeline_steps(self) -> list[Any]:
        """Open AG-UI spans for durable Pipeline steps in a new run."""

        output: list[Any] = []
        for step_key in sorted(self.open_pipeline_steps):
            if step_key in self.run_pipeline_steps:
                continue
            step_name = _pipeline_step_name(step_key)
            self.run_pipeline_steps[step_key] = step_name
            output.append(StepStartedEvent(step_name=step_name, timestamp=timestamp_ms()))
        return output

    def _map_pipeline_standard_event(self, envelope: Mapping[str, Any]) -> list[Any]:
        event_type = str(envelope.get("eventType") or "")
        data = envelope.get("data")
        if not isinstance(data, Mapping):
            return []
        if event_type == "text_delta":
            text = data.get("text")
            if isinstance(text, str) and text:
                return self._map_text(
                    text,
                    _pipeline_message_id(envelope, self.pipeline_run_id),
                    allow_parallel=True,
                )
        if event_type == "thinking_delta":
            return self._map_thinking(data, _pipeline_message_id(envelope, self.pipeline_run_id))
        if event_type == "tool_started":
            return self._map_tool(
                {
                    "status": "input_complete",
                    "toolUseId": data.get("toolUseId"),
                    "name": data.get("toolName"),
                    "toolInput": data.get("input"),
                }
            )
        if event_type == "tool_result":
            return self._map_tool(
                {
                    "status": "failed" if data.get("isError") is True else "completed",
                    "toolUseId": data.get("toolUseId"),
                    "name": data.get("toolName"),
                    "result": data.get("result"),
                }
            )
        if event_type == "usage":
            self._record_usage(data)
        return []

    def _record_usage(self, usage: Mapping[str, Any]) -> None:
        self.usage.append(
            TokenUsage(
                provider=_string(usage.get("provider")),
                model=_string(usage.get("model")),
                input_tokens=_integer(usage.get("inputTokens")),
                output_tokens=_integer(usage.get("outputTokens")),
                total_tokens=_integer(usage.get("totalTokens")),
                cached_input_tokens=_integer(usage.get("cachedInputTokens")),
            )
        )

    def close_reasoning(self) -> list[Any]:
        output: list[Any] = []
        for reasoning_id in sorted(self.open_reasoning):
            output.extend(
                [
                    ReasoningMessageEndEvent(message_id=reasoning_id, timestamp=timestamp_ms()),
                    ReasoningEndEvent(message_id=reasoning_id, timestamp=timestamp_ms()),
                ]
            )
        self.open_reasoning.clear()
        return output

    def close_text(self, message_id: str | None = None) -> list[Any]:
        if message_id is not None:
            message_ids = [message_id] if message_id in self.open_messages else []
        else:
            message_ids = sorted(self.open_messages)
        if not message_ids:
            return []
        for value in message_ids:
            self._finalize_text_snapshot(value)
        self.open_messages.difference_update(message_ids)
        if self.current_message_id in message_ids:
            self.current_message_id = next(iter(self.open_messages), None)
        return [TextMessageEndEvent(message_id=value, timestamp=timestamp_ms()) for value in message_ids]

    def finalize_text_snapshots(self) -> None:
        for message_id in list(self._message_text_parts):
            self._finalize_text_snapshot(message_id)

    def _finalize_text_snapshot(self, message_id: str) -> None:
        parts = self._message_text_parts.pop(message_id, None)
        if parts:
            self.text_snapshot_digests.add(_text_digest("".join(parts)))

    def close_all(self) -> list[Any]:
        output = self.close_reasoning()
        for tool_id in sorted(self.open_tools):
            output.append(ToolCallEndEvent(tool_call_id=tool_id, timestamp=timestamp_ms()))
        self.open_tools.clear()
        output.extend(self.close_text())
        for step_key in sorted(self.run_pipeline_steps):
            output.append(
                StepFinishedEvent(
                    step_name=self.run_pipeline_steps[step_key],
                    timestamp=timestamp_ms(),
                )
            )
        self.run_pipeline_steps.clear()
        return output


def aggregate_usage(items: Iterable[TokenUsage]) -> list[TokenUsage] | None:
    values = list(items)
    if not values:
        return None
    grouped: dict[tuple[str | None, str | None], list[TokenUsage]] = {}
    for item in values:
        grouped.setdefault((item.provider, item.model), []).append(item)
    return [
        TokenUsage(
            provider=provider,
            model=model,
            input_tokens=sum(item.input_tokens or 0 for item in group),
            output_tokens=sum(item.output_tokens or 0 for item in group),
            total_tokens=sum(item.total_tokens or 0 for item in group),
            cached_input_tokens=sum(item.cached_input_tokens or 0 for item in group),
        )
        for (provider, model), group in grouped.items()
    ]


def _pipeline_custom_event(envelope: dict[str, Any], *, event_type: str) -> CustomEvent | None:
    if event_type not in _AGUI_PIPELINE_CUSTOM_EVENT_TYPES:
        return None
    return CustomEvent(name="iac-code.pipeline.v1", value=envelope, timestamp=timestamp_ms())


def resume_value(input_value: Mapping[str, Any], payload: Any) -> str:
    if not isinstance(payload, Mapping):
        return ""
    free_text = payload.get("freeText") or payload.get("free_text")
    if isinstance(free_text, str) and free_text:
        return free_text
    selected_id = payload.get("selectedId") or payload.get("selected_id")
    if not isinstance(selected_id, str) or not selected_id:
        return ""
    raw_options = input_value.get("options")
    if isinstance(raw_options, list):
        for option in raw_options:
            if not isinstance(option, Mapping):
                continue
            option_id = option.get("id")
            if str(option_id) != selected_id:
                continue
            label = option.get("label") or option.get("name") or option_id
            return str(label)
    return selected_id


def _standard_options(raw_options: list[Any], *, pipeline: bool) -> list[dict[str, str]]:
    del pipeline
    output: list[dict[str, str]] = []
    used_ids: set[str] = set()
    for index, option in enumerate(raw_options):
        if isinstance(option, Mapping):
            option_id = str(option.get("id", option.get("candidate_index", index)))
            title = str(option.get("label") or option.get("name") or option.get("title") or option_id)
        else:
            option_id = str(option)
            title = str(option)
        base_id = option_id or f"option-{index + 1}"
        option_id = base_id
        suffix = 2
        while option_id in used_ids:
            option_id = f"{base_id}-{suffix}"
            suffix += 1
        used_ids.add(option_id)
        output.append({"id": option_id, "title": title})
    return output


def _selection_schema(options: list[dict[str, str]], *, allow_free_text: bool) -> dict[str, Any]:
    branches: list[dict[str, Any]] = [
        {
            "type": "object",
            "properties": {"selectedId": {"type": "string", "const": option["id"], "title": option["title"]}},
            "required": ["selectedId"],
            "additionalProperties": False,
        }
        for option in options
    ]
    if allow_free_text:
        branches.append(
            {
                "type": "object",
                "properties": {"freeText": {"type": "string", "minLength": 1}},
                "required": ["freeText"],
                "additionalProperties": False,
            }
        )
    return {"oneOf": branches}


def _selection_message(prompt: str, options: list[dict[str, str]]) -> str:
    if not options:
        return prompt
    return "{}\n{}".format(prompt, "\n".join(f"- {item['id']}: {item['title']}" for item in options))


def _status_text(result: Mapping[str, Any]) -> str:
    status = result.get("status")
    if not isinstance(status, Mapping):
        return ""
    message = status.get("message")
    if not isinstance(message, Mapping) or message.get("role") not in {"ROLE_AGENT", "agent", None}:
        return ""
    parts = message.get("parts")
    if not isinstance(parts, list):
        return ""
    return "".join(
        str(part.get("text")) for part in parts if isinstance(part, Mapping) and isinstance(part.get("text"), str)
    )


def _status_message_id(result: Mapping[str, Any]) -> str | None:
    status = result.get("status")
    message = status.get("message") if isinstance(status, Mapping) else None
    return _string(message.get("messageId")) if isinstance(message, Mapping) else None


def _task_history_agent_text(result: Mapping[str, Any]) -> tuple[str, str | None]:
    history = result.get("history")
    if not isinstance(history, list):
        return "", None
    trailing: list[Mapping[str, Any]] = []
    for raw_message in reversed(history):
        if not isinstance(raw_message, Mapping):
            break
        message = cast(Mapping[str, Any], raw_message)
        role = message.get("role")
        if role not in {"ROLE_AGENT", "agent"}:
            if trailing:
                break
            continue
        trailing.append(message)
    trailing.reverse()
    pieces: list[str] = []
    message_id: str | None = None
    for message in trailing:
        message_id = _string(message.get("messageId")) or message_id
        parts = message.get("parts")
        if not isinstance(parts, list):
            continue
        pieces.extend(
            str(part.get("text")) for part in parts if isinstance(part, Mapping) and isinstance(part.get("text"), str)
        )
    return "".join(pieces), message_id


def _json_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _text_digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _replayed_prefix_length(value: str, snapshot_digests: set[str]) -> int:
    """Return the longest prefix already emitted before an interrupt."""

    if not value or not snapshot_digests:
        return 0
    digest = hashlib.sha256()
    longest = 0
    for index, character in enumerate(value, start=1):
        digest.update(character.encode("utf-8"))
        if digest.hexdigest() in snapshot_digests:
            longest = index
    return longest


def _string(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _integer(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and math.isfinite(value) and value.is_integer():
        return int(value)
    return None


def _pipeline_step_key(envelope: Mapping[str, Any], step_id: str) -> str:
    candidate = envelope.get("candidate")
    if isinstance(candidate, Mapping):
        candidate_id = (
            _string(candidate.get("id")) or _string(candidate.get("subPipelineId")) or _string(candidate.get("runId"))
        )
        if candidate_id:
            return f"candidate:{candidate_id}:{step_id}"
    return f"step:{step_id}"


def _pipeline_step_name(step_key: str, *, step_id: str | None = None) -> str:
    """Return a RUN-local AG-UI step name unique across parallel candidates."""

    if step_key.startswith("step:"):
        return step_id or step_key.removeprefix("step:")
    # Candidate run ids are stable across start/completion/recovery and make
    # same-named parallel candidate steps legal under AG-UI's step verifier.
    return step_key


def _pipeline_message_id(envelope: Mapping[str, Any], pipeline_run_id: str) -> str:
    scope = _string(envelope.get("scope")) or "pipeline"
    coordinates: list[str] = []
    # A candidate step id (for example ``template_generating``) is shared by
    # every parallel candidate.  Include the candidate run identity first so
    # independent sub-pipeline text streams never collapse into one AG-UI
    # message.
    for key in ("candidate", "candidateStep", "step"):
        coordinate = envelope.get(key)
        if not isinstance(coordinate, Mapping):
            continue
        run_id = _string(coordinate.get("runId")) or _string(coordinate.get("id"))
        if run_id and run_id not in coordinates:
            coordinates.append(run_id)
    if coordinates:
        return f"pipeline-message:{pipeline_run_id}:{scope}:{':'.join(coordinates)}"
    return f"pipeline-message:{pipeline_run_id}:{scope}"
