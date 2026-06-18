from __future__ import annotations

import copy
import json
import logging
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from iac_code.a2a.artifacts import (
    sanitize_public_artifact_data,
    sanitize_public_tool_output_data,
)
from iac_code.a2a.pipeline_journal import to_json_safe

SNAPSHOT_SCHEMA_VERSION = "1.1"
logger = logging.getLogger(__name__)

_TERMINAL_STATUS_BY_EVENT_TYPE = {
    "pipeline_completed": "completed",
    "pipeline_failed": "failed",
    "pipeline_canceled": "canceled",
}


class A2APipelineSnapshotStore:
    def __init__(self, pipeline_dir: str | Path) -> None:
        self.pipeline_dir = Path(pipeline_dir)
        self.path = self.pipeline_dir / "a2a-snapshot.json"

    def save(self, snapshot: dict[str, Any]) -> bool:
        previous = self.load()
        next_snapshot = copy.deepcopy(snapshot)
        next_snapshot["snapshotVersion"] = _snapshot_version(previous) + 1
        next_snapshot = to_json_safe(next_snapshot)
        if not isinstance(next_snapshot, dict):
            logger.warning("Skipping invalid A2A pipeline snapshot for %s", self.path)
            return False

        self.pipeline_dir.mkdir(parents=True, exist_ok=True)
        tmp_path = self.path.with_name(f"{self.path.name}.{uuid.uuid4().hex}.tmp")
        try:
            with tmp_path.open("w", encoding="utf-8") as handle:
                json.dump(next_snapshot, handle, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            tmp_path.replace(self.path)
            return True
        except (OSError, TypeError, ValueError):
            logger.warning("Failed to persist A2A pipeline snapshot to %s", self.path, exc_info=True)
            return False
        finally:
            if tmp_path.exists():
                tmp_path.unlink()

    def load(self) -> dict[str, Any] | None:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (UnicodeDecodeError, json.JSONDecodeError, OSError):
            logger.warning("Failed to load A2A pipeline snapshot from %s", self.path, exc_info=True)
            return None
        if not isinstance(value, dict):
            logger.warning(
                "Invalid A2A pipeline snapshot in %s: expected object, got %s",
                self.path,
                type(value).__name__,
            )
            return None
        return value


def reduce_pipeline_events(
    events: list[dict[str, Any]],
    existing_snapshot: dict[str, Any] | None = None,
) -> dict[str, Any]:
    reducer = _PipelineSnapshotReducer(existing_snapshot)
    reducer.reduce(events)
    return reducer.snapshot()


def sanitize_pipeline_artifact_uris(value: Any) -> Any:
    if isinstance(value, list):
        return [sanitize_pipeline_artifact_uris(item) for item in value]
    if not isinstance(value, dict):
        return value

    sanitized = copy.deepcopy(value)
    if sanitized.get("eventType") == "artifact_created":
        _sanitize_artifact_field(sanitized, "artifact")
        _sanitize_artifact_field(sanitized, "data")
    if sanitized.get("eventType") == "tool_result":
        _drop_legacy_tool_result_artifact_uri(_dict_or_none(sanitized.get("data")))

    display = _dict_or_none(sanitized.get("display"))
    if display is not None:
        artifacts = display.get("artifacts")
        if isinstance(artifacts, list):
            for artifact in artifacts:
                _drop_legacy_artifact_uri(_dict_or_none(artifact))
        tool_results = display.get("toolResults")
        if isinstance(tool_results, list):
            for tool_result in tool_results:
                _drop_legacy_tool_result_artifact_uri(_dict_or_none(tool_result))
    return sanitized


class _PipelineSnapshotReducer:
    def __init__(self, existing_snapshot: dict[str, Any] | None = None) -> None:
        self._snapshot = _snapshot_from_existing(existing_snapshot)
        self._seen_event_ids: set[str] = set()
        self._steps_by_run_id: dict[str, dict[str, Any]] = {}
        self._candidates_by_run_id: dict[str, dict[str, Any]] = {}
        self._candidate_parent_step_run_ids: dict[str, str] = {}
        self._candidate_steps_by_run_id: dict[str, dict[str, Any]] = {}
        self._messages_by_scope_run_id: dict[tuple[str, str], dict[str, Any]] = {}
        self._candidate_detail_indexes: dict[str, int] = {}
        self._diagram_indexes: dict[str, int] = {}
        self._artifact_indexes: dict[str, int] = {}
        self._permission_indexes: dict[str, int] = {}
        self._tool_result_indexes: dict[str, int] = {}
        self._input_history_keys: set[str] = set()
        self._interrupt_history_keys: set[str] = set()
        self._rollback_keys: set[str] = set()
        self._candidate_restart_keys: set[str] = set()
        self._handoff_history_keys: set[str] = set()
        self._stack_history_keys: set[str] = set()
        self._skip_sequences_through = 0
        self._hydrate_existing_snapshot(existing_snapshot)

    def reduce(self, events: list[dict[str, Any]]) -> None:
        for event in sorted(events, key=_sequence_value):
            if not isinstance(event, dict):
                continue
            event_id = _string_or_none(event.get("eventId"))
            if event_id is not None and event_id in self._seen_event_ids:
                continue
            if self._is_legacy_replay_event(event):
                continue
            if event_id is not None:
                self._seen_event_ids.add(event_id)
            self._apply(event)

    def snapshot(self) -> dict[str, Any]:
        snapshot = copy.deepcopy(self._snapshot)
        snapshot["steps"] = sorted(snapshot["steps"], key=_item_sort_key)
        for step in snapshot["steps"]:
            step["candidates"] = sorted(step.get("candidates", []), key=_item_sort_key)
            for candidate in step["candidates"]:
                candidate["steps"] = sorted(candidate.get("steps", []), key=_item_sort_key)
        snapshot["seenEventIds"] = sorted(self._seen_event_ids)
        return snapshot

    def _hydrate_existing_snapshot(self, existing_snapshot: dict[str, Any] | None) -> None:
        if existing_snapshot is None:
            return

        seen_event_ids = existing_snapshot.get("seenEventIds")
        has_explicit_seen_event_ids = isinstance(seen_event_ids, list)
        if has_explicit_seen_event_ids:
            self._seen_event_ids.update(value for value in seen_event_ids if isinstance(value, str) and value)
        else:
            self._skip_sequences_through = _sequence_number(self._snapshot.get("lastSequence"))

        self._hydrate_steps()
        self._hydrate_messages()
        self._hydrate_display_indexes("candidateDetails", self._candidate_detail_indexes, ("detailId", "id"))
        self._hydrate_display_indexes("diagrams", self._diagram_indexes, ("diagramId", "id"))
        self._hydrate_display_indexes("artifacts", self._artifact_indexes, ("artifactId", "artifact_id", "id"))
        self._hydrate_display_indexes("permissions", self._permission_indexes, ("permissionId", "toolUseId", "id"))
        self._hydrate_display_indexes("toolResults", self._tool_result_indexes, ("toolUseId", "resultId", "id"))
        self._hydrate_control_history("inputHistory", self._input_history_keys)
        self._hydrate_control_history("interruptHistory", self._interrupt_history_keys)
        self._hydrate_rollbacks()
        self._hydrate_candidate_restarts()
        self._hydrate_control_history("handoffHistory", self._handoff_history_keys)
        self._hydrate_stack_history()

    def _hydrate_steps(self) -> None:
        valid_steps: list[dict[str, Any]] = []
        seen_step_run_ids: set[str] = set()
        for step in self._snapshot["steps"]:
            if not isinstance(step, dict):
                continue
            step["candidates"] = _dict_list(step.get("candidates"))
            step_run_id = _string_or_none(step.get("runId"))
            if step_run_id is None or step_run_id in seen_step_run_ids:
                continue
            seen_step_run_ids.add(step_run_id)
            valid_steps.append(step)
            self._steps_by_run_id[step_run_id] = step

            valid_candidates: list[dict[str, Any]] = []
            seen_candidate_run_ids: set[str] = set()
            for candidate in step["candidates"]:
                candidate["steps"] = _dict_list(candidate.get("steps"))
                candidate_run_id = _string_or_none(candidate.get("runId"))
                if candidate_run_id is None or candidate_run_id in seen_candidate_run_ids:
                    continue
                seen_candidate_run_ids.add(candidate_run_id)
                valid_candidates.append(candidate)
                self._candidates_by_run_id[candidate_run_id] = candidate
                self._candidate_parent_step_run_ids[candidate_run_id] = step_run_id

                valid_candidate_steps: list[dict[str, Any]] = []
                seen_candidate_step_run_ids: set[str] = set()
                for candidate_step in candidate["steps"]:
                    candidate_step_run_id = _string_or_none(candidate_step.get("runId"))
                    if candidate_step_run_id is None or candidate_step_run_id in seen_candidate_step_run_ids:
                        continue
                    seen_candidate_step_run_ids.add(candidate_step_run_id)
                    valid_candidate_steps.append(candidate_step)
                    self._candidate_steps_by_run_id[candidate_step_run_id] = candidate_step
                candidate["steps"] = valid_candidate_steps
            step["candidates"] = valid_candidates
        self._snapshot["steps"] = valid_steps
        self._sanitize_active_candidate_run_ids()

    def _hydrate_messages(self) -> None:
        valid_messages: list[dict[str, Any]] = []
        seen_message_keys: set[tuple[str, str]] = set()
        for message in self._snapshot["display"]["messages"]:
            if not isinstance(message, dict):
                continue
            event_id = _string_or_none(message.get("eventId"))
            if event_id is not None:
                self._seen_event_ids.add(event_id)
            scope = _string_or_none(message.get("scope")) or "pipeline"
            run_id = _string_or_none(message.get("runId"))
            if run_id is not None:
                key = (scope, run_id)
                if key in seen_message_keys:
                    continue
                seen_message_keys.add(key)
                message["scope"] = scope
                message["runId"] = run_id
                if not isinstance(message.get("text"), str):
                    message["text"] = ""
                self._messages_by_scope_run_id[key] = message
                valid_messages.append(message)
        self._snapshot["display"]["messages"] = valid_messages

    def _hydrate_display_indexes(
        self,
        display_key: str,
        indexes: dict[str, int],
        id_keys: tuple[str, ...],
    ) -> None:
        unique_items: list[dict[str, Any]] = []
        seen_item_ids: set[str] = set()
        for item in self._snapshot["display"][display_key]:
            if not isinstance(item, dict):
                continue
            event_id = _string_or_none(item.get("eventId"))
            if event_id is not None:
                self._seen_event_ids.add(event_id)
            item_id = _first_string_value(item, (*id_keys, "eventId"))
            if item_id is not None:
                if item_id in seen_item_ids:
                    continue
                seen_item_ids.add(item_id)
                indexes[item_id] = len(unique_items)
            unique_items.append(item)
        self._snapshot["display"][display_key] = unique_items

    def _hydrate_rollbacks(self) -> None:
        unique_rollbacks: list[dict[str, Any]] = []
        for rollback in self._snapshot["control"]["rollbackHistory"]:
            if not isinstance(rollback, dict):
                continue
            event_id = _string_or_none(rollback.get("eventId"))
            key = event_id or str(_sequence_value(rollback))
            if key in self._rollback_keys:
                if event_id is not None:
                    self._seen_event_ids.add(event_id)
                continue
            self._rollback_keys.add(key)
            unique_rollbacks.append(rollback)
            if event_id is not None:
                self._seen_event_ids.add(event_id)
        self._snapshot["control"]["rollbackHistory"] = unique_rollbacks

    def _hydrate_candidate_restarts(self) -> None:
        unique_restarts: list[dict[str, Any]] = []
        for restart in self._snapshot["control"]["candidateRestarts"]:
            if not isinstance(restart, dict):
                continue
            event_id = _string_or_none(restart.get("eventId"))
            key = event_id or str(_sequence_value(restart))
            if key in self._candidate_restart_keys:
                if event_id is not None:
                    self._seen_event_ids.add(event_id)
                continue
            self._candidate_restart_keys.add(key)
            unique_restarts.append(restart)
            if event_id is not None:
                self._seen_event_ids.add(event_id)
        self._snapshot["control"]["candidateRestarts"] = unique_restarts

    def _hydrate_control_history(self, control_key: str, known_keys: set[str]) -> None:
        unique_items: list[dict[str, Any]] = []
        for item in self._snapshot["control"][control_key]:
            if not isinstance(item, dict):
                continue
            event_id = _string_or_none(item.get("eventId"))
            key = event_id or str(_sequence_value(item))
            if key in known_keys:
                if event_id is not None:
                    self._seen_event_ids.add(event_id)
                continue
            known_keys.add(key)
            unique_items.append(item)
            if event_id is not None:
                self._seen_event_ids.add(event_id)
        self._snapshot["control"][control_key] = unique_items

    def _hydrate_stack_history(self) -> None:
        stacks = self._snapshot["stacks"]
        unique_history: list[dict[str, Any]] = []
        for item in stacks["history"]:
            if not isinstance(item, dict):
                continue
            event_id = _string_or_none(item.get("eventId"))
            key = event_id or str(_sequence_value(item))
            if key in self._stack_history_keys:
                if event_id is not None:
                    self._seen_event_ids.add(event_id)
                continue
            self._stack_history_keys.add(key)
            unique_history.append(item)
            if event_id is not None:
                self._seen_event_ids.add(event_id)
        stacks["history"] = unique_history

    def _is_legacy_replay_event(self, event: dict[str, Any]) -> bool:
        return self._skip_sequences_through > 0 and _sequence_value(event) <= self._skip_sequences_through

    def _sanitize_active_candidate_run_ids(self) -> None:
        active_candidate_run_ids = self._snapshot["control"].get("activeCandidateRunIds")
        valid_run_ids: list[str] = []
        seen_run_ids: set[str] = set()
        for run_id in active_candidate_run_ids if isinstance(active_candidate_run_ids, list) else []:
            if not isinstance(run_id, str) or not run_id or run_id in seen_run_ids:
                continue
            if run_id not in self._candidates_by_run_id:
                continue
            seen_run_ids.add(run_id)
            valid_run_ids.append(run_id)
        self._snapshot["control"]["activeCandidateRunIds"] = valid_run_ids

    def _apply(self, event: dict[str, Any]) -> None:
        event_type = _string_or_none(event.get("eventType")) or ""
        self._snapshot["lastSequence"] = max(self._snapshot["lastSequence"], _sequence_value(event))
        self._merge_pipeline_identity(event)

        data = _dict_or_empty(event.get("data"))
        if event_type == "pipeline_started":
            self._apply_pipeline_started(data)
        elif event_type == "pipeline_handoff_ready":
            handoff = _normal_handoff(event)
            self._snapshot["normalHandoff"] = handoff
            self._append_control_history("handoffHistory", self._handoff_history_keys, handoff)

        step = self._upsert_step(event.get("step"), event)
        candidate = self._upsert_candidate(step, event.get("candidate"), event)
        self._upsert_candidate_step(candidate, event.get("candidateStep"), event)

        if event_type in {"input_required", "input_received"}:
            self._append_control_history(
                "inputHistory",
                self._input_history_keys,
                _interaction_history_entry(event),
            )
        if event_type in {"interrupt_received", "interrupt_classified"} or event.get("scope") == "interrupt":
            self._append_control_history(
                "interruptHistory",
                self._interrupt_history_keys,
                _interaction_history_entry(event),
            )

        if event_type == "text_delta":
            self._apply_text_delta(event)
        elif event_type == "candidate_detail_shown":
            self._upsert_display_item("candidateDetails", self._candidate_detail_indexes, event, "detailId")
        elif event_type == "diagram_shown":
            self._upsert_display_item("diagrams", self._diagram_indexes, event, "diagramId")
        elif event_type == "artifact_created":
            self._upsert_artifact_item(event)
        elif event_type == "permission_requested":
            self._upsert_permission_item(event)
        elif event_type == "tool_result":
            self._upsert_tool_result_item(event)
        elif event_type == "stack_current_changed":
            self._apply_stack_current_changed(event)
        elif event_type == "rollback_completed":
            self._append_rollback(event)
        elif event_type == "candidate_restart_requested":
            self._append_candidate_restart(event)
        elif event_type == "input_required":
            self._snapshot["pendingInput"] = self._pending_input(event)
            self._snapshot["status"] = "waiting_input"
        elif event_type == "input_received":
            self._snapshot["pendingInput"] = None
            self._snapshot["status"] = "working"

        terminal_status = _TERMINAL_STATUS_BY_EVENT_TYPE.get(event_type)
        if terminal_status is not None:
            self._snapshot["status"] = terminal_status
            self._snapshot["pendingInput"] = None
            self._snapshot["control"]["activeCandidateRunIds"] = []
        elif event_type not in {"input_required", "input_received"} and not (
            event_type == "pipeline_handoff_ready" and self._snapshot["status"] in {"completed", "failed", "canceled"}
        ):
            self._apply_event_status(event)

    def _merge_pipeline_identity(self, event: dict[str, Any]) -> None:
        for key in ("pipelineRunId", "taskId", "contextId", "pipelineName"):
            value = event.get(key)
            if value is not None:
                self._snapshot[key] = value

    def _apply_pipeline_started(self, data: dict[str, Any]) -> None:
        self._snapshot["normalHandoff"] = None
        control = self._snapshot["control"]
        if "totalSteps" in data:
            control["totalSteps"] = data["totalSteps"]
        if isinstance(data.get("stepIds"), list):
            control["stepIds"] = copy.deepcopy(data["stepIds"])
        if isinstance(data.get("stepNames"), list):
            control["stepNames"] = copy.deepcopy(data["stepNames"])

    def _apply_event_status(self, event: dict[str, Any]) -> None:
        event_status = _normalized_status(event.get("status"))
        if event_status is not None:
            self._snapshot["status"] = event_status

    def _upsert_step(self, coordinate_value: Any, event: dict[str, Any]) -> dict[str, Any] | None:
        coordinate = _dict_or_none(coordinate_value)
        if coordinate is None:
            return None

        run_id = _string_or_none(coordinate.get("runId"))
        if run_id is None:
            return None

        step = self._steps_by_run_id.get(run_id)
        if step is None:
            step = {
                "runId": run_id,
                "id": _string_or_none(coordinate.get("id")) or run_id,
                "attempt": _int_or_none(coordinate.get("attempt")) or 1,
                "status": "working",
                "candidates": [],
            }
            self._steps_by_run_id[run_id] = step
            self._snapshot["steps"].append(step)

        _merge_coordinate(step, coordinate)
        self._apply_step_lifecycle(step, event)
        return step

    def _apply_step_lifecycle(self, step: dict[str, Any], event: dict[str, Any]) -> None:
        event_type = event.get("eventType")
        created_at = _string_or_none(event.get("createdAt"))
        if event_type == "step_started":
            step["status"] = "working"
            _set_time(step, "startedAt", created_at)
        elif event_type == "step_completed":
            step["status"] = "completed"
            _set_time(step, "completedAt", created_at)
            _merge_completion_data(step, event)
        elif event_type == "step_failed":
            step["status"] = "failed"
            _set_time(step, "failedAt", created_at)
            _merge_completion_data(step, event)
        elif event_type == "input_required":
            step["status"] = "waiting_input"
        elif event_type == "input_received" and step.get("status") == "waiting_input":
            step["inputReceived"] = copy.deepcopy(_dict_or_empty(event.get("data")))
            if _event_kind(event) in {"ask_user_question", "pipeline_pause_confirmation"}:
                step["status"] = "working"
            else:
                step["status"] = "completed"
                _set_time(step, "completedAt", created_at)

    def _upsert_candidate(
        self,
        step: dict[str, Any] | None,
        coordinate_value: Any,
        event: dict[str, Any],
    ) -> dict[str, Any] | None:
        coordinate = _dict_or_none(coordinate_value)
        if coordinate is None:
            return None

        run_id = _string_or_none(coordinate.get("runId"))
        if run_id is None:
            return None

        if step is None:
            parent_run_id = self._candidate_parent_step_run_ids.get(run_id)
            step = self._steps_by_run_id.get(parent_run_id or "")
        if step is None:
            return None

        candidate = self._candidates_by_run_id.get(run_id)
        if candidate is None:
            candidate = {
                "runId": run_id,
                "id": _string_or_none(coordinate.get("id")) or run_id,
                "attempt": _int_or_none(coordinate.get("attempt")) or 1,
                "status": "working",
                "steps": [],
            }
            self._candidates_by_run_id[run_id] = candidate

        if candidate not in step["candidates"]:
            step["candidates"].append(candidate)
        self._candidate_parent_step_run_ids[run_id] = step["runId"]

        _merge_coordinate(candidate, coordinate)
        self._apply_candidate_lifecycle(candidate, event)
        return candidate

    def _apply_candidate_lifecycle(self, candidate: dict[str, Any], event: dict[str, Any]) -> None:
        event_type = event.get("eventType")
        created_at = _string_or_none(event.get("createdAt"))
        run_id = candidate["runId"]
        if event_type == "candidate_started":
            candidate["status"] = "working"
            _set_time(candidate, "startedAt", created_at)
            self._remove_active_candidate_attempts(candidate)
            _append_unique(self._snapshot["control"]["activeCandidateRunIds"], run_id)
        elif event_type == "candidate_completed":
            candidate["status"] = "completed"
            _set_time(candidate, "completedAt", created_at)
            _merge_completion_data(candidate, event)
            _remove_value(self._snapshot["control"]["activeCandidateRunIds"], run_id)
        elif event_type == "candidate_failed":
            candidate["status"] = "failed"
            _set_time(candidate, "failedAt", created_at)
            _merge_completion_data(candidate, event)
            _remove_value(self._snapshot["control"]["activeCandidateRunIds"], run_id)
        elif event_type == "candidate_restart_requested":
            candidate["status"] = "restarting"
            _set_time(candidate, "restartingAt", created_at)
            _remove_value(self._snapshot["control"]["activeCandidateRunIds"], run_id)

    def _remove_active_candidate_attempts(self, candidate: dict[str, Any]) -> None:
        candidate_id = _string_or_none(candidate.get("id"))
        candidate_index = _int_or_none(candidate.get("index"))
        current_run_id = _string_or_none(candidate.get("runId"))
        active_run_ids = list(self._snapshot["control"].get("activeCandidateRunIds") or [])
        for run_id in active_run_ids:
            if run_id == current_run_id:
                continue
            active_candidate = self._candidates_by_run_id.get(run_id)
            if active_candidate is None:
                continue
            same_id = candidate_id is not None and candidate_id == _string_or_none(active_candidate.get("id"))
            same_index = candidate_index is not None and candidate_index == _int_or_none(active_candidate.get("index"))
            if same_id and same_index:
                _remove_value(self._snapshot["control"]["activeCandidateRunIds"], run_id)

    def _upsert_candidate_step(
        self,
        candidate: dict[str, Any] | None,
        coordinate_value: Any,
        event: dict[str, Any],
    ) -> dict[str, Any] | None:
        coordinate = _dict_or_none(coordinate_value)
        if coordinate is None or candidate is None:
            return None

        run_id = _string_or_none(coordinate.get("runId"))
        if run_id is None:
            return None

        candidate_step = self._candidate_steps_by_run_id.get(run_id)
        if candidate_step is None:
            candidate_step = {
                "runId": run_id,
                "id": _string_or_none(coordinate.get("id")) or run_id,
                "attempt": _int_or_none(coordinate.get("attempt")) or 1,
                "status": "working",
            }
            self._candidate_steps_by_run_id[run_id] = candidate_step

        if candidate_step not in candidate["steps"]:
            candidate["steps"].append(candidate_step)

        _merge_coordinate(candidate_step, coordinate)
        self._apply_candidate_step_lifecycle(candidate_step, event)
        return candidate_step

    def _apply_candidate_step_lifecycle(self, candidate_step: dict[str, Any], event: dict[str, Any]) -> None:
        event_type = event.get("eventType")
        created_at = _string_or_none(event.get("createdAt"))
        if event_type == "candidate_step_started":
            candidate_step["status"] = "working"
            _set_time(candidate_step, "startedAt", created_at)
        elif event_type == "candidate_step_completed":
            candidate_step["status"] = "completed"
            _set_time(candidate_step, "completedAt", created_at)
            _merge_completion_data(candidate_step, event)
        elif event_type == "candidate_step_failed":
            candidate_step["status"] = "failed"
            _set_time(candidate_step, "failedAt", created_at)
            _merge_completion_data(candidate_step, event)

    def _apply_text_delta(self, event: dict[str, Any]) -> None:
        text = _dict_or_empty(event.get("data")).get("text")
        if not isinstance(text, str):
            return

        scope = _string_or_none(event.get("scope")) or "pipeline"
        run_id = _scope_run_id(event)
        key = (scope, run_id)
        message = self._messages_by_scope_run_id.get(key)
        if message is None:
            message = {
                "id": f"message-{scope}-{run_id}",
                "scope": scope,
                "runId": run_id,
                "text": "",
                "createdAt": _string_or_none(event.get("createdAt")),
            }
            _merge_event_coordinates(message, event)
            self._messages_by_scope_run_id[key] = message
            self._snapshot["display"]["messages"].append(message)

        if not isinstance(message.get("text"), str):
            message["text"] = ""
        message["text"] += text
        message["updatedAt"] = _string_or_none(event.get("createdAt"))

    def _upsert_display_item(
        self,
        display_key: str,
        indexes: dict[str, int],
        event: dict[str, Any],
        preferred_id_key: str,
    ) -> None:
        data = _dict_or_empty(event.get("data"))
        item_id = _display_item_id(data, event, preferred_id_key)
        item = copy.deepcopy(data)
        item.setdefault(preferred_id_key, item_id)
        item["id"] = item_id
        item["scope"] = _string_or_none(event.get("scope")) or "pipeline"
        item["runId"] = _scope_run_id(event)
        item["createdAt"] = _string_or_none(event.get("createdAt"))
        item["eventId"] = _string_or_none(event.get("eventId"))
        _merge_event_coordinates(item, event)

        items = self._snapshot["display"][display_key]
        index = indexes.get(item_id)
        if index is None:
            indexes[item_id] = len(items)
            items.append(item)
        else:
            existing_created_at = items[index].get("createdAt")
            item["createdAt"] = existing_created_at or item["createdAt"]
            items[index] = item

    def _upsert_artifact_item(self, event: dict[str, Any]) -> None:
        data = _dict_or_empty(event.get("data"))
        artifact = _dict_or_none(event.get("artifact"))
        item = copy.deepcopy(data)
        if artifact is not None:
            item.update(copy.deepcopy(artifact))

        item_id = _artifact_item_id(artifact, data, event)
        item["artifactId"] = item_id
        item["id"] = item_id
        _drop_legacy_artifact_uri(item)
        item["scope"] = _string_or_none(event.get("scope")) or "pipeline"
        item["runId"] = _scope_run_id(event)
        item["sequence"] = _sequence_value(event)
        item["createdAt"] = _string_or_none(event.get("createdAt"))
        item["eventId"] = _string_or_none(event.get("eventId"))
        _merge_event_coordinates(item, event)

        items = self._snapshot["display"]["artifacts"]
        index = self._artifact_indexes.get(item_id)
        if index is None:
            self._artifact_indexes[item_id] = len(items)
            items.append(item)
        else:
            existing = copy.deepcopy(items[index])
            existing_created_at = existing.get("createdAt")
            existing.update(item)
            existing["createdAt"] = existing_created_at or item["createdAt"]
            items[index] = existing

    def _upsert_permission_item(self, event: dict[str, Any]) -> None:
        permission = _dict_or_none(event.get("permission"))
        if permission is None:
            permission = _dict_or_empty(event.get("data"))
        item = copy.deepcopy(permission)
        data = _dict_or_empty(event.get("data"))
        for key in ("toolName", "toolUseId"):
            if key not in item and key in data:
                item[key] = copy.deepcopy(data[key])
        self._upsert_display_record("permissions", self._permission_indexes, event, item, "permissionId")

    def _upsert_tool_result_item(self, event: dict[str, Any]) -> None:
        item = copy.deepcopy(_dict_or_empty(event.get("data")))
        _drop_legacy_tool_result_artifact_uri(item)
        self._upsert_display_record("toolResults", self._tool_result_indexes, event, item, "toolUseId")

    def _apply_stack_current_changed(self, event: dict[str, Any]) -> None:
        data = copy.deepcopy(_dict_or_empty(event.get("data")))
        stack_id = _string_or_none(data.get("stackId"))
        if stack_id is None:
            return

        item = data
        item["id"] = stack_id
        item["stackId"] = stack_id
        item["scope"] = _string_or_none(event.get("scope")) or "stack"
        item["runId"] = _scope_run_id(event)
        item["sequence"] = _sequence_value(event)
        item["createdAt"] = _string_or_none(event.get("createdAt"))
        item["eventId"] = _string_or_none(event.get("eventId"))
        _merge_event_coordinates(item, event)

        stacks = self._snapshot["stacks"]
        existing = copy.deepcopy(stacks["byId"].get(stack_id) or {})
        existing.update(item)
        stacks["byId"][stack_id] = existing

        key = _string_or_none(event.get("eventId")) or str(_sequence_value(event))
        if key not in self._stack_history_keys:
            self._stack_history_keys.add(key)
            stacks["history"].append(copy.deepcopy(item))

        if item.get("current") is False or item.get("cleared") is True:
            current = _dict_or_none(stacks.get("current"))
            if current is None or current.get("stackId") == stack_id:
                stacks["current"] = None
        else:
            stacks["current"] = copy.deepcopy(existing)

    def _upsert_display_record(
        self,
        display_key: str,
        indexes: dict[str, int],
        event: dict[str, Any],
        item: dict[str, Any],
        preferred_id_key: str,
    ) -> None:
        item_id = _display_item_id(item, event, preferred_id_key)
        item.setdefault(preferred_id_key, item_id)
        item["id"] = item_id
        item["scope"] = _string_or_none(event.get("scope")) or "pipeline"
        item["runId"] = _scope_run_id(event)
        item["sequence"] = _sequence_value(event)
        item["createdAt"] = _string_or_none(event.get("createdAt"))
        item["eventId"] = _string_or_none(event.get("eventId"))
        _merge_event_coordinates(item, event)

        items = self._snapshot["display"][display_key]
        index = indexes.get(item_id)
        if index is None:
            indexes[item_id] = len(items)
            items.append(item)
        else:
            existing_created_at = items[index].get("createdAt")
            item["createdAt"] = existing_created_at or item["createdAt"]
            items[index] = item

    def _append_rollback(self, event: dict[str, Any]) -> None:
        key = _string_or_none(event.get("eventId")) or str(_sequence_value(event))
        if key in self._rollback_keys:
            return
        self._rollback_keys.add(key)

        entry = {
            "eventId": _string_or_none(event.get("eventId")),
            "sequence": _sequence_value(event),
            "createdAt": _string_or_none(event.get("createdAt")),
            "data": copy.deepcopy(_dict_or_empty(event.get("data"))),
        }
        _merge_event_coordinates(entry, event)
        self._snapshot["control"]["rollbackHistory"].append(entry)

    def _append_candidate_restart(self, event: dict[str, Any]) -> None:
        key = _string_or_none(event.get("eventId")) or str(_sequence_value(event))
        if key in self._candidate_restart_keys:
            return
        self._candidate_restart_keys.add(key)

        data = copy.deepcopy(_dict_or_empty(event.get("data")))
        entry = {
            "eventId": _string_or_none(event.get("eventId")),
            "sequence": _sequence_value(event),
            "createdAt": _string_or_none(event.get("createdAt")),
            "candidateScope": data.get("candidateScope"),
            "targetCandidateStepId": data.get("targetCandidateStepId"),
            "nextCandidateAttempt": data.get("nextCandidateAttempt"),
            "reason": data.get("reason"),
            "data": data,
        }
        _merge_event_coordinates(entry, event)
        self._snapshot["control"]["candidateRestarts"].append(entry)

    def _append_control_history(
        self,
        control_key: str,
        known_keys: set[str],
        entry: dict[str, Any],
    ) -> None:
        key = _string_or_none(entry.get("eventId")) or str(_sequence_value(entry))
        if key in known_keys:
            return
        known_keys.add(key)
        self._snapshot["control"][control_key].append(entry)

    def _pending_input(self, event: dict[str, Any]) -> dict[str, Any]:
        input_value = _dict_or_none(event.get("input"))
        pending = copy.deepcopy(input_value if input_value is not None else _dict_or_empty(event.get("data")))
        pending["scope"] = _string_or_none(event.get("scope")) or "pipeline"
        pending["runId"] = _scope_run_id(event)
        pending["createdAt"] = _string_or_none(event.get("createdAt"))
        pending["eventId"] = _string_or_none(event.get("eventId"))
        _merge_event_coordinates(pending, event)
        return pending


def _normal_handoff(event: dict[str, Any]) -> dict[str, Any]:
    data = copy.deepcopy(_dict_or_empty(event.get("data")))
    handoff = {
        "eventType": _string_or_none(event.get("eventType")),
        "eventId": _string_or_none(event.get("eventId")),
        "sequence": _sequence_value(event),
        "createdAt": _string_or_none(event.get("createdAt")),
        "status": _string_or_none(event.get("status")),
        "action": data.get("action"),
        "targetMode": data.get("targetMode"),
        "outcome": data.get("outcome"),
        "summary": data.get("summary"),
        "data": data,
    }
    _merge_event_coordinates(handoff, event)
    return handoff


def _interaction_history_entry(event: dict[str, Any]) -> dict[str, Any]:
    data = copy.deepcopy(_dict_or_empty(event.get("data")))
    input_value = copy.deepcopy(_dict_or_empty(event.get("input")))
    entry = {
        "eventType": _string_or_none(event.get("eventType")),
        "eventId": _string_or_none(event.get("eventId")),
        "sequence": _sequence_value(event),
        "createdAt": _string_or_none(event.get("createdAt")),
        "scope": _string_or_none(event.get("scope")) or "pipeline",
        "status": _string_or_none(event.get("status")),
        "runId": _scope_run_id(event),
        "data": data,
    }
    if input_value:
        entry["input"] = input_value

    source = {**data, **input_value}
    for key in (
        "kind",
        "inputId",
        "toolUseId",
        "prompt",
        "question",
        "required",
        "allowFreeText",
        "freeTextPrompt",
        "options",
        "selectedValue",
        "selectedIndex",
        "selectedOption",
        "selectedId",
        "selectedLabel",
        "answerTextLength",
        "userInputLength",
        "freeTextLength",
        "messageLength",
        "action",
        "targetStepId",
        "candidateScope",
        "rollbackScope",
        "toStepId",
        "reason",
    ):
        if key in source:
            entry[key] = copy.deepcopy(source[key])

    _merge_event_coordinates(entry, event)
    return entry


def _empty_snapshot() -> dict[str, Any]:
    return {
        "schemaVersion": SNAPSHOT_SCHEMA_VERSION,
        "snapshotVersion": 0,
        "pipelineRunId": None,
        "taskId": None,
        "contextId": None,
        "pipelineName": None,
        "status": "working",
        "lastSequence": 0,
        "generatedAt": _utc_now(),
        "steps": [],
        "display": {
            "messages": [],
            "diagrams": [],
            "candidateDetails": [],
            "artifacts": [],
            "permissions": [],
            "toolResults": [],
        },
        "stacks": {
            "current": None,
            "byId": {},
            "history": [],
        },
        "normalHandoff": None,
        "pendingInput": None,
        "control": {
            "activeCandidateRunIds": [],
            "inputHistory": [],
            "interruptHistory": [],
            "rollbackHistory": [],
            "candidateRestarts": [],
            "handoffHistory": [],
        },
        "seenEventIds": [],
    }


def _snapshot_from_existing(existing_snapshot: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(existing_snapshot, dict):
        return _empty_snapshot()

    snapshot = copy.deepcopy(existing_snapshot)
    defaults = _empty_snapshot()
    for key, value in defaults.items():
        snapshot.setdefault(key, copy.deepcopy(value))

    steps = snapshot.get("steps")
    snapshot["steps"] = _dict_list(steps)

    display = snapshot.get("display")
    if not isinstance(display, dict):
        display = {}
    snapshot["display"] = {
        key: _dict_list(display.get(key))
        for key in ("messages", "diagrams", "candidateDetails", "artifacts", "permissions", "toolResults")
    }

    stacks = snapshot.get("stacks")
    if not isinstance(stacks, dict):
        stacks = {}
    by_id = stacks.get("byId")
    snapshot["stacks"] = {
        "current": copy.deepcopy(stacks.get("current")) if isinstance(stacks.get("current"), dict) else None,
        "byId": {str(key): copy.deepcopy(value) for key, value in by_id.items() if isinstance(value, dict)}
        if isinstance(by_id, dict)
        else {},
        "history": _dict_list(stacks.get("history")),
    }
    snapshot["normalHandoff"] = (
        copy.deepcopy(snapshot.get("normalHandoff")) if isinstance(snapshot.get("normalHandoff"), dict) else None
    )

    control = snapshot.get("control")
    if not isinstance(control, dict):
        control = {}
    snapshot["control"] = copy.deepcopy(control)
    for key in (
        "activeCandidateRunIds",
        "inputHistory",
        "interruptHistory",
        "rollbackHistory",
        "candidateRestarts",
        "handoffHistory",
    ):
        value = snapshot["control"].get(key)
        snapshot["control"][key] = copy.deepcopy(value) if isinstance(value, list) else []

    seen_event_ids = snapshot.get("seenEventIds")
    snapshot["seenEventIds"] = (
        sorted(value for value in seen_event_ids if isinstance(value, str) and value)
        if isinstance(seen_event_ids, list)
        else []
    )
    snapshot["lastSequence"] = _sequence_number(snapshot.get("lastSequence"))
    return snapshot


def _merge_coordinate(target: dict[str, Any], coordinate: dict[str, Any]) -> None:
    for key, value in coordinate.items():
        if value is not None:
            target[key] = copy.deepcopy(value)


def _merge_event_coordinates(target: dict[str, Any], event: dict[str, Any]) -> None:
    for key in ("step", "candidate", "candidateStep"):
        value = _dict_or_none(event.get(key))
        if value is not None:
            target[key] = copy.deepcopy(value)


def _event_kind(event: dict[str, Any]) -> str | None:
    input_value = _dict_or_none(event.get("input"))
    if input_value is not None:
        return _string_or_none(input_value.get("kind"))
    data = _dict_or_empty(event.get("data"))
    return _string_or_none(data.get("kind"))


def _display_item_id(data: dict[str, Any], event: dict[str, Any], preferred_id_key: str) -> str:
    for key in (preferred_id_key, "id", "toolUseId"):
        value = _string_or_none(data.get(key))
        if value is not None:
            return value
    event_id = _string_or_none(event.get("eventId"))
    if event_id is not None:
        return event_id
    return f"{preferred_id_key}-{_sequence_value(event)}"


def _first_string_value(data: dict[str, Any], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        value = _string_or_none(data.get(key))
        if value is not None:
            return value
    return None


def _artifact_item_id(artifact: dict[str, Any] | None, data: dict[str, Any], event: dict[str, Any]) -> str:
    for source in (artifact or {}, data):
        for key in ("artifactId", "artifact_id", "id"):
            value = _string_or_none(source.get(key))
            if value is not None:
                return value
    event_id = _string_or_none(event.get("eventId"))
    if event_id is not None:
        return event_id
    return f"artifact-{_sequence_value(event)}"


def _drop_legacy_artifact_uri(data: dict[str, Any] | None) -> None:
    if data is None:
        return
    sanitized = sanitize_public_artifact_data(data)
    data.clear()
    data.update(sanitized)


def _sanitize_artifact_field(data: dict[str, Any], key: str) -> None:
    if key not in data:
        return
    value = data[key]
    if isinstance(value, (dict, list, tuple)):
        data[key] = sanitize_public_artifact_data(value)
    else:
        data.pop(key, None)


def _drop_legacy_tool_result_artifact_uri(data: dict[str, Any] | None) -> None:
    if data is None:
        return
    if "result" in data:
        data["result"] = sanitize_public_tool_output_data(data["result"])


def _scope_run_id(event: dict[str, Any]) -> str:
    scope = _string_or_none(event.get("scope"))
    if scope == "candidate_step":
        candidate_step = _dict_or_none(event.get("candidateStep"))
        run_id = _run_id(candidate_step)
        if run_id is not None:
            return run_id
    if scope == "candidate":
        candidate = _dict_or_none(event.get("candidate"))
        run_id = _run_id(candidate)
        if run_id is not None:
            return run_id
    if scope in {"step", "input"}:
        step = _dict_or_none(event.get("step"))
        run_id = _run_id(step)
        if run_id is not None:
            return run_id

    for coordinate_key in ("candidateStep", "candidate", "step"):
        run_id = _run_id(_dict_or_none(event.get(coordinate_key)))
        if run_id is not None:
            return run_id

    return _string_or_none(event.get("pipelineRunId")) or "pipeline"


def _run_id(coordinate: dict[str, Any] | None) -> str | None:
    if coordinate is None:
        return None
    return _string_or_none(coordinate.get("runId"))


def _sequence_value(event: Any) -> int:
    if not isinstance(event, dict):
        return 0

    return _sequence_number(event.get("sequence", 0))


def _sequence_number(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return 0


def _snapshot_version(snapshot: dict[str, Any] | None) -> int:
    if snapshot is None:
        return 0
    return _int_or_none(snapshot.get("snapshotVersion")) or 0


def _item_sort_key(item: dict[str, Any]) -> tuple[int, int, str]:
    index = _int_or_none(item.get("index"))
    attempt = _int_or_none(item.get("attempt")) or 0
    run_id = _string_or_none(item.get("runId")) or ""
    if index is None:
        return (1, 0, run_id)
    return (0, index, f"{attempt}:{run_id}")


def _normalized_status(value: Any) -> str | None:
    status = _string_or_none(value)
    if status == "input_required":
        return "waiting_input"
    return status


def _dict_or_none(value: Any) -> dict[str, Any] | None:
    return value if isinstance(value, dict) else None


def _dict_or_empty(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _dict_list(value: Any) -> list[dict[str, Any]]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _string_or_none(value: Any) -> str | None:
    if isinstance(value, str) and value:
        return value
    return None


def _int_or_none(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return None
    return None


def _set_time(target: dict[str, Any], key: str, value: str | None) -> None:
    if value is not None:
        target[key] = value


def _merge_completion_data(target: dict[str, Any], event: dict[str, Any]) -> None:
    data = _dict_or_empty(event.get("data"))
    for key in (
        "conclusionField",
        "conclusion",
        "conclusions",
        "durationS",
        "earlyExit",
        "failed",
        "errorSummary",
        "errorDetails",
    ):
        if key in data:
            target[key] = copy.deepcopy(data[key])


def _append_unique(values: list[Any], value: Any) -> None:
    if value not in values:
        values.append(value)


def _remove_value(values: list[Any], value: Any) -> None:
    while value in values:
        values.remove(value)


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


__all__ = ["A2APipelineSnapshotStore", "SNAPSHOT_SCHEMA_VERSION", "reduce_pipeline_events"]
