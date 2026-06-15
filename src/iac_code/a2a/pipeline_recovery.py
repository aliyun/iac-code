from __future__ import annotations

import math
from typing import Any

from a2a.server.context import ServerCallContext

from iac_code.a2a.pipeline_journal import A2APipelineJournal
from iac_code.a2a.pipeline_paths import existing_a2a_pipeline_dir_for_session
from iac_code.a2a.pipeline_snapshot import (
    SNAPSHOT_SCHEMA_VERSION,
    A2APipelineSnapshotStore,
    reduce_pipeline_events,
    sanitize_pipeline_artifact_uris,
)
from iac_code.i18n import _


class A2APipelineRecoveryService:
    def __init__(self, *, task_store: Any) -> None:
        self._task_store = task_store

    async def get_state(
        self,
        *,
        context_id: str | None = None,
        task_id: str | None = None,
        after_sequence: int | None = None,
        call_context: ServerCallContext | None = None,
    ) -> dict[str, Any]:
        if task_id is not None:
            task = await self._task_store.get(task_id, context=call_context)
            if task is None:
                raise ValueError(_("A2A pipeline state not found"))
            if context_id is not None and context_id != task.context_id:
                raise ValueError(_("A2A task/context mismatch"))
            context_id = task.context_id
        elif context_id is None:
            raise ValueError(_("contextId or taskId is required"))

        context = await self._task_store.get_context_record(context_id)
        pipeline_dir = existing_a2a_pipeline_dir_for_session(cwd=context.cwd, session_id=context.session_id)
        journal = A2APipelineJournal(pipeline_dir)
        snapshot_store = A2APipelineSnapshotStore(pipeline_dir)
        snapshot = snapshot_store.load()
        events = journal.read_all_repairing_tail()
        recovery_task_id = task_id
        if recovery_task_id is None:
            context_events = _events_for_task(events, task_id=None, context_id=context_id)
            snapshot_task_id = None
            if isinstance(snapshot, dict) and _snapshot_matches_context(snapshot, context_id=context_id):
                snapshot_task_id = snapshot.get("taskId")
                snapshot_task_id = snapshot_task_id if isinstance(snapshot_task_id, str) else None
            journal_task_id = _latest_task_id(context_events)
            recovery_task_id = _select_context_recovery_task_id(
                snapshot=snapshot,
                snapshot_task_id=snapshot_task_id,
                journal_task_id=journal_task_id,
                context_events=context_events,
            )
            if call_context is not None:
                if recovery_task_id is None:
                    raise ValueError(_("A2A pipeline state not found"))
                await self._verify_task_owner(recovery_task_id, context_id=context_id, call_context=call_context)
            replay_events = (
                _events_for_task(events, task_id=recovery_task_id, context_id=context_id)
                if recovery_task_id is not None
                else context_events
            )
        else:
            replay_events = _events_for_task(events, task_id=recovery_task_id, context_id=context_id)

        if (
            snapshot is not None
            and _snapshot_schema_is_stale(snapshot)
            and _snapshot_seen_events_are_within_replay(snapshot, replay_events)
        ):
            snapshot = reduce_pipeline_events(replay_events)
            if task_id is None:
                snapshot_store.save(snapshot)
                snapshot = snapshot_store.load() or snapshot

        if snapshot is None:
            if not replay_events:
                raise ValueError(_("A2A pipeline state not found"))
            snapshot = reduce_pipeline_events(replay_events)
            if task_id is None:
                snapshot_store.save(snapshot)
                snapshot = snapshot_store.load() or snapshot
        elif recovery_task_id is not None and (
            not _snapshot_matches(
                snapshot,
                task_id=recovery_task_id,
                context_id=context_id,
            )
            or not _snapshot_seen_events_are_within_context_task(
                snapshot,
                _events_for_task(events, task_id=None, context_id=context_id),
                task_id=recovery_task_id,
            )
        ):
            if not replay_events:
                raise ValueError(_("A2A pipeline state not found"))
            snapshot = reduce_pipeline_events(replay_events)
            if not _snapshot_matches(snapshot, task_id=recovery_task_id, context_id=context_id):
                raise ValueError(_("A2A pipeline state not found"))
            if task_id is None:
                snapshot_store.save(snapshot)
                snapshot = snapshot_store.load() or snapshot
        elif task_id is None and not _snapshot_seen_events_are_within_replay(snapshot, replay_events):
            if not _snapshot_matches_context(snapshot, context_id=context_id):
                raise ValueError(_("A2A pipeline state not found"))
            if recovery_task_id is not None and not _snapshot_seen_events_are_within_context_task(
                snapshot,
                _events_for_task(events, task_id=None, context_id=context_id),
                task_id=recovery_task_id,
            ):
                if not replay_events:
                    raise ValueError(_("A2A pipeline state not found"))
                snapshot = reduce_pipeline_events(replay_events)
                if not _snapshot_matches(snapshot, task_id=recovery_task_id, context_id=context_id):
                    raise ValueError(_("A2A pipeline state not found"))
                snapshot_store.save(snapshot)
                snapshot = snapshot_store.load() or snapshot
        elif task_id is None and not _snapshot_matches_context(snapshot, context_id=context_id):
            if not replay_events:
                raise ValueError(_("A2A pipeline state not found"))
            snapshot = reduce_pipeline_events(replay_events)
            if not _snapshot_matches_context(snapshot, context_id=context_id):
                raise ValueError(_("A2A pipeline state not found"))
            snapshot_store.save(snapshot)
            snapshot = snapshot_store.load() or snapshot

        if task_id is not None and not _snapshot_matches(snapshot, task_id=task_id, context_id=context_id):
            raise ValueError(_("A2A pipeline state not found"))
        if (
            task_id is None
            and recovery_task_id is not None
            and not _snapshot_matches(
                snapshot,
                task_id=recovery_task_id,
                context_id=context_id,
            )
        ):
            raise ValueError(_("A2A pipeline state not found"))
        if task_id is None and not _snapshot_matches_context(snapshot, context_id=context_id):
            raise ValueError(_("A2A pipeline state not found"))

        replay_after = after_sequence if after_sequence is not None else _int_value(snapshot.get("lastSequence"), 0)
        events_after_replay = [event for event in replay_events if _int_value(event.get("sequence"), 0) > replay_after]
        return {
            "snapshot": _json_compatible(sanitize_pipeline_artifact_uris(snapshot)),
            "events": _json_compatible(sanitize_pipeline_artifact_uris(events_after_replay)),
        }

    async def _verify_task_owner(
        self,
        task_id: str,
        *,
        context_id: str,
        call_context: ServerCallContext,
    ) -> None:
        task = await self._task_store.get(task_id, context=call_context)
        if task is None or task.context_id != context_id:
            raise ValueError(_("A2A pipeline state not found"))


def _int_value(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _json_compatible(value: Any) -> Any:
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {key: _json_compatible(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_compatible(item) for item in value]
    return value


def _events_for_task(
    events: list[dict[str, Any]],
    *,
    task_id: str | None,
    context_id: str,
) -> list[dict[str, Any]]:
    context_events = [event for event in events if event.get("contextId") == context_id]
    if task_id is None:
        return context_events
    return [event for event in context_events if event.get("taskId") == task_id]


def _snapshot_matches(snapshot: dict[str, Any], *, task_id: str, context_id: str) -> bool:
    return snapshot.get("taskId") == task_id and snapshot.get("contextId") == context_id


def _snapshot_matches_context(snapshot: dict[str, Any], *, context_id: str) -> bool:
    return snapshot.get("contextId") == context_id


def _snapshot_schema_is_stale(snapshot: dict[str, Any]) -> bool:
    return snapshot.get("schemaVersion") != SNAPSHOT_SCHEMA_VERSION


def _latest_task_id(events: list[dict[str, Any]]) -> str | None:
    task_events = [event for event in events if isinstance(event.get("taskId"), str)]
    if not task_events:
        return None
    latest_event = max(task_events, key=lambda event: _int_value(event.get("sequence"), 0))
    task_id = latest_event.get("taskId")
    return task_id if isinstance(task_id, str) else None


def _select_context_recovery_task_id(
    *,
    snapshot: dict[str, Any] | None,
    snapshot_task_id: str | None,
    journal_task_id: str | None,
    context_events: list[dict[str, Any]],
) -> str | None:
    if snapshot_task_id is None or not isinstance(snapshot, dict):
        return journal_task_id
    if journal_task_id is None:
        return snapshot_task_id

    snapshot_sequence = _int_value(snapshot.get("lastSequence"), 0)
    latest_journal_sequence = max((_int_value(event.get("sequence"), 0) for event in context_events), default=0)
    snapshot_task_events = _events_for_task(
        context_events,
        task_id=snapshot_task_id,
        context_id=str(snapshot.get("contextId")),
    )
    if snapshot_sequence >= latest_journal_sequence:
        if _snapshot_seen_events_are_within_context_task(snapshot, context_events, task_id=snapshot_task_id):
            return snapshot_task_id
        return journal_task_id
    later_task_ids = {
        event.get("taskId")
        for event in context_events
        if _int_value(event.get("sequence"), 0) > snapshot_sequence and isinstance(event.get("taskId"), str)
    }
    if later_task_ids and later_task_ids != {snapshot_task_id}:
        return journal_task_id
    if not _snapshot_seen_events_are_within_replay(snapshot, snapshot_task_events):
        return snapshot_task_id
    return journal_task_id


def _snapshot_seen_events_are_within_replay(snapshot: dict[str, Any], replay_events: list[dict[str, Any]]) -> bool:
    seen_event_ids = snapshot.get("seenEventIds")
    if not isinstance(seen_event_ids, list):
        return True
    replay_event_ids = {event.get("eventId") for event in replay_events if isinstance(event.get("eventId"), str)}
    return all(not isinstance(event_id, str) or event_id in replay_event_ids for event_id in seen_event_ids)


def _snapshot_seen_events_are_within_context_task(
    snapshot: dict[str, Any],
    context_events: list[dict[str, Any]],
    *,
    task_id: str,
) -> bool:
    seen_event_ids = snapshot.get("seenEventIds")
    if not isinstance(seen_event_ids, list):
        return True
    event_task_ids = {
        event.get("eventId"): event.get("taskId") for event in context_events if isinstance(event.get("eventId"), str)
    }
    return all(
        not isinstance(event_id, str) or event_id not in event_task_ids or event_task_ids[event_id] == task_id
        for event_id in seen_event_ids
    )
