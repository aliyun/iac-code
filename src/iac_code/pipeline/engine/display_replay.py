"""Semantic display transcript for pipeline UI replay."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from loguru import logger

DISPLAY_REPLAY_VERSION = 1
DISPLAY_TRANSCRIPT_FILENAME = "display.jsonl"

TERMINAL_ATTEMPT_STATUSES = {"completed", "failed", "rolled_back", "interrupted"}


@dataclass
class DisplayToolUse:
    name: str
    tool_use_id: str = ""


@dataclass
class DisplayCandidate:
    name: str
    candidate_index: int | None = None
    mermaid_source: str = ""
    summary: str = ""
    cost_items: list[dict[str, Any]] = field(default_factory=list)
    total_monthly_cost: str = ""


@dataclass
class DisplayCandidateSelection:
    state: str = "none"
    prompt: str = ""
    options: list[dict[str, Any]] = field(default_factory=list)
    selected_name: str = ""
    selected_index: int | None = None
    candidates: dict[int | str, DisplayCandidate] = field(default_factory=dict)


@dataclass
class DisplaySubStepAttempt:
    step_id: str
    attempt_no: int
    attempt_id: str = ""
    transcript_id: str = ""
    status: str = "running"
    step_index: int | None = None
    error: str = ""


@dataclass
class DisplaySubPipeline:
    sub_pipeline_id: str
    candidate_index: int | None = None
    candidate_name: str = ""
    sub_pipeline_name: str = ""
    total_steps: int = 0
    status: str = "running"
    steps: list[DisplaySubStepAttempt] = field(default_factory=list)
    error: str = ""


@dataclass
class DisplayAttempt:
    step_id: str
    attempt_no: int
    attempt_id: str = ""
    transcript_id: str = ""
    index: int | None = None
    total: int | None = None
    status: str = "running"
    step_type: str = ""
    ui_mode: str = ""
    summary: str = ""
    rollback_to: str = ""
    rollback_reason: str = ""
    error: str = ""
    tools: list[DisplayToolUse] = field(default_factory=list)
    sub_pipelines: dict[str, DisplaySubPipeline] = field(default_factory=dict)
    candidate_selection: DisplayCandidateSelection = field(default_factory=DisplayCandidateSelection)


@dataclass
class DisplayReplayModel:
    pipeline_name: str = "Pipeline"
    attempts: list[DisplayAttempt] = field(default_factory=list)
    interrupted: bool = False
    completed: bool = False
    failed: bool = False
    duration_s: float | None = None


class PipelineDisplayRecorder:
    """Append-only JSONL writer for semantic pipeline display events."""

    def __init__(self, path: str | Path | None) -> None:
        self.path = Path(path) if path is not None else None

    @property
    def enabled(self) -> bool:
        return self.path is not None

    def record(
        self,
        event_type: str,
        *,
        step_id: str | None = None,
        pipeline_name: str | None = None,
        payload: dict[str, Any] | None = None,
        timestamp: float | None = None,
    ) -> None:
        if self.path is None:
            return
        event: dict[str, Any] = {
            "version": DISPLAY_REPLAY_VERSION,
            "type": event_type,
            "timestamp": timestamp if timestamp is not None else time.time(),
        }
        if step_id is not None:
            event["step_id"] = step_id
        if pipeline_name is not None:
            event["pipeline_name"] = pipeline_name
        if payload:
            event["payload"] = payload
        self._append(event)

    def record_pipeline_event(self, event: Any) -> None:
        event_type = getattr(getattr(event, "type", ""), "value", getattr(event, "type", ""))
        data = getattr(event, "data", {}) or {}
        pipeline_name = data.get("pipeline_type") if event_type == "pipeline_started" else None
        self.record(
            str(event_type),
            step_id=getattr(event, "step_id", None),
            pipeline_name=pipeline_name,
            payload=dict(data) if isinstance(data, dict) else {},
            timestamp=getattr(event, "timestamp", None),
        )

    def record_tool_use(self, event: Any, *, step_id: str | None = None, sub_pipeline_id: str | None = None) -> None:
        payload = {
            "name": getattr(event, "name", ""),
            "tool_use_id": getattr(event, "tool_use_id", ""),
        }
        if sub_pipeline_id:
            payload["sub_pipeline_id"] = sub_pipeline_id
        self.record("tool_used", step_id=step_id, payload=payload)

    def record_candidate_diagram(self, event: Any, *, step_id: str | None = None) -> None:
        self.record(
            "candidate_diagram",
            step_id=step_id,
            payload={
                "candidate_name": getattr(event, "candidate_name", ""),
                "candidate_index": getattr(event, "candidate_index", None),
                "mermaid_source": getattr(event, "mermaid_source", ""),
            },
        )

    def record_candidate_detail(self, event: Any, *, step_id: str | None = None) -> None:
        self.record(
            "candidate_detail",
            step_id=step_id,
            payload={
                "candidate_name": getattr(event, "candidate_name", ""),
                "candidate_index": getattr(event, "candidate_index", None),
                "summary": getattr(event, "summary", ""),
                "cost_items": getattr(event, "cost_items", []),
                "total_monthly_cost": getattr(event, "total_monthly_cost", ""),
            },
        )

    def record_candidate_selected(
        self,
        *,
        step_id: str | None,
        candidate_name: str,
        candidate_index: int | None,
    ) -> None:
        self.record(
            "candidate_selected",
            step_id=step_id,
            payload={"candidate_name": candidate_name, "candidate_index": candidate_index},
        )

    def record_user_aborted(self) -> None:
        self.record("pipeline_user_aborted")

    def _append(self, event: dict[str, Any]) -> None:
        try:
            assert self.path is not None
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")
        except Exception as exc:
            logger.warning("Failed to append pipeline display event: {}", exc)


def load_display_events(path: str | Path | None) -> list[dict[str, Any]]:
    if path is None:
        return []
    display_path = Path(path)
    if not display_path.exists():
        return []
    events: list[dict[str, Any]] = []
    try:
        lines = display_path.read_text(encoding="utf-8").splitlines()
    except Exception as exc:
        logger.warning("Failed to load pipeline display transcript: {}", exc)
        return []
    for line_number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            logger.warning("Skipping invalid pipeline display event at {}:{}", display_path, line_number)
            continue
        if isinstance(parsed, dict):
            events.append(parsed)
    return events


class PipelineDisplayReducer:
    """Build a static replay model from semantic display events."""

    def reduce(
        self,
        events: list[dict[str, Any]],
        attempts_metadata: dict[str, Any] | None = None,
    ) -> DisplayReplayModel:
        model = DisplayReplayModel()
        attempt_counts: dict[str, int] = {}
        metadata_index = self._attempt_metadata_index(attempts_metadata)
        active_attempt: DisplayAttempt | None = None
        pipeline_started_at: float | None = None

        for event in events:
            event_type = str(event.get("type", ""))
            payload = self._payload(event)
            step_id = self._step_id(event, payload)

            if event_type == "pipeline_started":
                pipeline_started_at = self._optional_float(event.get("timestamp"))
                model.pipeline_name = str(
                    event.get("pipeline_name") or payload.get("pipeline_type") or model.pipeline_name
                )
                continue

            if event_type == "step_started" and step_id:
                attempt_counts[step_id] = attempt_counts.get(step_id, 0) + 1
                metadata = self._next_parent_attempt_metadata(metadata_index, step_id)
                active_attempt = DisplayAttempt(
                    step_id=step_id,
                    attempt_no=attempt_counts[step_id],
                    attempt_id=str(
                        payload.get("active_attempt_id")
                        or payload.get("attempt_id")
                        or metadata.get("attempt_id")
                        or ""
                    ),
                    transcript_id=str(payload.get("transcript_id") or metadata.get("transcript_id") or ""),
                    index=self._optional_int(payload.get("index")),
                    total=self._optional_int(payload.get("total")),
                    step_type=str(payload.get("step_type") or ""),
                    ui_mode=str(payload.get("ui_mode") or ""),
                )
                if active_attempt.ui_mode == "candidate_selection":
                    active_attempt.candidate_selection.state = "preparing"
                model.attempts.append(active_attempt)
                continue

            attempt = self._find_attempt(model, step_id) or active_attempt

            if event_type == "step_completed" and attempt is not None:
                attempt.status = "completed"
                if "candidates_count" in payload:
                    attempt.summary = f"{payload['candidates_count']} candidates"
                if attempt.candidate_selection.state != "none":
                    attempt.candidate_selection.state = "completed"
                if active_attempt is attempt:
                    active_attempt = None
                continue

            if event_type == "step_failed" and attempt is not None:
                attempt.status = "failed"
                attempt.error = str(payload.get("error") or "")
                if active_attempt is attempt:
                    active_attempt = None
                continue

            if event_type == "rollback_triggered":
                from_step = str(payload.get("from_step") or step_id or "")
                rollback_attempt = self._find_attempt(model, from_step) or attempt
                if rollback_attempt is not None:
                    rollback_attempt.status = "rolled_back"
                    rollback_attempt.rollback_to = str(payload.get("to_step") or "")
                    rollback_attempt.rollback_reason = str(payload.get("reason") or "")
                if active_attempt is rollback_attempt:
                    active_attempt = None
                continue

            if event_type == "tool_used" and attempt is not None:
                name = str(payload.get("name") or "")
                if name:
                    attempt.tools.append(DisplayToolUse(name=name, tool_use_id=str(payload.get("tool_use_id") or "")))
                continue

            if event_type == "user_input_required" and attempt is not None:
                attempt.status = "waiting_input"
                self._mark_candidate_selection_waiting(attempt, payload)
                continue

            if event_type == "candidate_selection_ready" and attempt is not None:
                self._mark_candidate_selection_waiting(attempt, payload)
                continue

            if event_type == "candidate_diagram" and attempt is not None:
                candidate = self._candidate(attempt, payload)
                candidate.mermaid_source = str(payload.get("mermaid_source") or "")
                if attempt.candidate_selection.state == "none":
                    attempt.candidate_selection.state = "preparing"
                continue

            if event_type == "candidate_detail" and attempt is not None:
                candidate = self._candidate(attempt, payload)
                candidate.summary = str(payload.get("summary") or "")
                cost_items = payload.get("cost_items")
                candidate.cost_items = cost_items if isinstance(cost_items, list) else []
                candidate.total_monthly_cost = str(payload.get("total_monthly_cost") or "")
                if attempt.candidate_selection.state == "none":
                    attempt.candidate_selection.state = "preparing"
                continue

            if event_type == "candidate_selected" and attempt is not None:
                attempt.candidate_selection.state = "selected"
                attempt.candidate_selection.selected_name = str(payload.get("candidate_name") or "")
                attempt.candidate_selection.selected_index = self._optional_int(payload.get("candidate_index"))
                continue

            if event_type == "sub_pipeline_started" and attempt is not None:
                sub_id = str(payload.get("sub_pipeline_id") or "")
                if sub_id:
                    attempt.sub_pipelines[sub_id] = DisplaySubPipeline(
                        sub_pipeline_id=sub_id,
                        candidate_index=self._optional_int(payload.get("candidate_index")),
                        candidate_name=str(payload.get("candidate_name") or ""),
                        sub_pipeline_name=str(payload.get("sub_pipeline_name") or ""),
                        total_steps=int(payload.get("total_steps") or 0),
                    )
                continue

            if event_type == "sub_step_started" and attempt is not None:
                sub = self._sub_pipeline(attempt, payload)
                if sub is not None:
                    sub.status = "running"
                    metadata = self._next_sub_step_attempt_metadata(metadata_index, attempt.step_id, payload)
                    self._append_sub_step_attempt(sub, payload, step_id, metadata)
                continue

            if event_type == "sub_step_completed" and attempt is not None:
                sub = self._sub_pipeline(attempt, payload)
                completed_step = str(payload.get("step_id") or step_id or "")
                if sub is not None and completed_step:
                    sub_step = self._find_sub_step_attempt(sub, completed_step)
                    if sub_step is None:
                        metadata = self._next_sub_step_attempt_metadata(metadata_index, attempt.step_id, payload)
                        sub_step = self._append_sub_step_attempt(sub, payload, completed_step, metadata)
                    sub_step.status = "completed"
                continue

            if event_type == "sub_step_failed" and attempt is not None:
                sub = self._sub_pipeline(attempt, payload)
                failed_step = str(payload.get("step_id") or step_id or "")
                if sub is not None and failed_step:
                    sub_step = self._find_sub_step_attempt(sub, failed_step)
                    if sub_step is None:
                        metadata = self._next_sub_step_attempt_metadata(metadata_index, attempt.step_id, payload)
                        sub_step = self._append_sub_step_attempt(sub, payload, failed_step, metadata)
                    sub_step.status = "failed"
                    sub_step.error = str(payload.get("error") or "")
                continue

            if event_type == "sub_pipeline_completed" and attempt is not None:
                sub = self._sub_pipeline(attempt, payload)
                if sub is not None:
                    sub.status = "failed" if payload.get("failed") else "completed"
                    sub.error = str(payload.get("error") or payload.get("error_summary") or "")
                    if sub.status == "failed":
                        self._mark_running_sub_step_status(sub, "failed", sub.error)
                continue

            if event_type == "pipeline_user_aborted":
                model.interrupted = True
                if active_attempt is not None and active_attempt.status not in TERMINAL_ATTEMPT_STATUSES:
                    active_attempt.status = "interrupted"
                    self._mark_running_sub_pipelines_interrupted(active_attempt)
                continue

            if event_type == "pipeline_completed":
                model.completed = not bool(payload.get("failed"))
                model.failed = bool(payload.get("failed"))
                completed_at = self._optional_float(event.get("timestamp"))
                if pipeline_started_at is not None and completed_at is not None and completed_at >= pipeline_started_at:
                    model.duration_s = completed_at - pipeline_started_at
                if active_attempt is not None and active_attempt.status not in TERMINAL_ATTEMPT_STATUSES:
                    active_attempt.status = "completed" if model.completed else "failed"
                active_attempt = None

        return model

    @staticmethod
    def _payload(event: dict[str, Any]) -> dict[str, Any]:
        payload = event.get("payload")
        return payload if isinstance(payload, dict) else {}

    @staticmethod
    def _step_id(event: dict[str, Any], payload: dict[str, Any]) -> str:
        return str(event.get("step_id") or payload.get("step_id") or "")

    @staticmethod
    def _optional_int(value: Any) -> int | None:
        if value is None:
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _optional_float(value: Any) -> float | None:
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _find_attempt(model: DisplayReplayModel, step_id: str | None) -> DisplayAttempt | None:
        if not step_id:
            return None
        for attempt in reversed(model.attempts):
            if attempt.step_id == step_id:
                return attempt
        return None

    @staticmethod
    def _candidate_key(payload: dict[str, Any]) -> int | str:
        candidate_index = payload.get("candidate_index")
        if candidate_index is not None:
            try:
                return int(candidate_index)
            except (TypeError, ValueError):
                pass
        return str(payload.get("candidate_name") or "")

    def _candidate(self, attempt: DisplayAttempt, payload: dict[str, Any]) -> DisplayCandidate:
        key = self._candidate_key(payload)
        candidate = attempt.candidate_selection.candidates.get(key)
        if candidate is None:
            candidate = DisplayCandidate(
                name=str(payload.get("candidate_name") or ""),
                candidate_index=self._optional_int(payload.get("candidate_index")),
            )
            attempt.candidate_selection.candidates[key] = candidate
        return candidate

    @staticmethod
    def _mark_candidate_selection_waiting(attempt: DisplayAttempt, payload: dict[str, Any]) -> None:
        attempt.candidate_selection.state = "waiting"
        attempt.candidate_selection.prompt = str(payload.get("prompt") or "")
        options = payload.get("options")
        attempt.candidate_selection.options = options if isinstance(options, list) else []

    @staticmethod
    def _sub_pipeline(attempt: DisplayAttempt, payload: dict[str, Any]) -> DisplaySubPipeline | None:
        sub_id = str(payload.get("sub_pipeline_id") or "")
        if not sub_id:
            return None
        sub = attempt.sub_pipelines.get(sub_id)
        if sub is None:
            sub = DisplaySubPipeline(sub_pipeline_id=sub_id)
            attempt.sub_pipelines[sub_id] = sub
        return sub

    def _append_sub_step_attempt(
        self,
        sub: DisplaySubPipeline,
        payload: dict[str, Any],
        fallback_step_id: str,
        metadata: dict[str, Any] | None = None,
    ) -> DisplaySubStepAttempt:
        metadata = metadata or {}
        step_id = str(payload.get("step_id") or fallback_step_id or "")
        attempt_no = sum(1 for step in sub.steps if step.step_id == step_id) + 1
        sub_step = DisplaySubStepAttempt(
            step_id=step_id,
            attempt_no=attempt_no,
            attempt_id=str(
                payload.get("active_attempt_id") or payload.get("attempt_id") or metadata.get("attempt_id") or ""
            ),
            transcript_id=str(payload.get("transcript_id") or metadata.get("transcript_id") or ""),
            step_index=self._optional_int(payload.get("step_index")),
        )
        sub.steps.append(sub_step)
        return sub_step

    @staticmethod
    def _find_sub_step_attempt(sub: DisplaySubPipeline, step_id: str) -> DisplaySubStepAttempt | None:
        for sub_step in reversed(sub.steps):
            if sub_step.step_id == step_id and sub_step.status == "running":
                return sub_step
        for sub_step in reversed(sub.steps):
            if sub_step.step_id == step_id:
                return sub_step
        return None

    @staticmethod
    def _mark_running_sub_step_status(sub: DisplaySubPipeline, status: str, error: str = "") -> None:
        for sub_step in reversed(sub.steps):
            if sub_step.status == "running":
                sub_step.status = status
                if error:
                    sub_step.error = error
                return

    def _mark_running_sub_pipelines_interrupted(self, attempt: DisplayAttempt) -> None:
        for sub in attempt.sub_pipelines.values():
            if sub.status == "running":
                self._mark_running_sub_step_status(sub, "interrupted")

    def _attempt_metadata_index(self, attempts_metadata: dict[str, Any] | None) -> dict[str, Any]:
        parent_by_step: dict[str, list[dict[str, Any]]] = {}
        sub_by_key: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
        if not isinstance(attempts_metadata, dict):
            return {
                "parent_by_step": parent_by_step,
                "sub_by_key": sub_by_key,
                "parent_cursors": {},
                "sub_cursors": {},
            }
        items = attempts_metadata.get("items")
        if not isinstance(items, dict):
            return {
                "parent_by_step": parent_by_step,
                "sub_by_key": sub_by_key,
                "parent_cursors": {},
                "sub_cursors": {},
            }
        for attempt in sorted(
            (value for value in items.values() if isinstance(value, dict)),
            key=self._attempt_sort_key,
        ):
            scope = attempt.get("scope")
            if scope == "parent":
                step_id = str(attempt.get("step_id") or "")
                if step_id:
                    parent_by_step.setdefault(step_id, []).append(attempt)
            elif scope == "sub_step":
                parent_step_id = str(attempt.get("parent_step_id") or "")
                sub_pipeline_id = str(attempt.get("sub_pipeline_id") or "")
                sub_step_id = str(attempt.get("sub_step_id") or "")
                if parent_step_id and sub_pipeline_id and sub_step_id:
                    sub_by_key.setdefault((parent_step_id, sub_pipeline_id, sub_step_id), []).append(attempt)
        return {
            "parent_by_step": parent_by_step,
            "sub_by_key": sub_by_key,
            "parent_cursors": {},
            "sub_cursors": {},
        }

    @staticmethod
    def _attempt_sort_key(attempt: dict[str, Any]) -> tuple[int, str]:
        raw = str(attempt.get("attempt_id") or attempt.get("transcript_id") or "")
        suffix = raw.rsplit("_", 1)[-1]
        try:
            return int(suffix), raw
        except ValueError:
            return 0, raw

    @staticmethod
    def _next_parent_attempt_metadata(metadata_index: dict[str, Any], step_id: str) -> dict[str, Any]:
        by_step = metadata_index.get("parent_by_step", {})
        cursors = metadata_index.get("parent_cursors", {})
        attempts = by_step.get(step_id, []) if isinstance(by_step, dict) else []
        cursor = cursors.get(step_id, 0) if isinstance(cursors, dict) else 0
        if not isinstance(attempts, list) or cursor >= len(attempts):
            return {}
        if isinstance(cursors, dict):
            cursors[step_id] = cursor + 1
        attempt = attempts[cursor]
        return attempt if isinstance(attempt, dict) else {}

    @staticmethod
    def _next_sub_step_attempt_metadata(
        metadata_index: dict[str, Any],
        parent_step_id: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        sub_pipeline_id = str(payload.get("sub_pipeline_id") or "")
        sub_step_id = str(payload.get("step_id") or "")
        key = (parent_step_id, sub_pipeline_id, sub_step_id)
        by_key = metadata_index.get("sub_by_key", {})
        cursors = metadata_index.get("sub_cursors", {})
        attempts = by_key.get(key, []) if isinstance(by_key, dict) else []
        cursor = cursors.get(key, 0) if isinstance(cursors, dict) else 0
        if not isinstance(attempts, list) or cursor >= len(attempts):
            return {}
        if isinstance(cursors, dict):
            cursors[key] = cursor + 1
        attempt = attempts[cursor]
        return attempt if isinstance(attempt, dict) else {}
