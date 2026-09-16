"""A2A structured input-required coordination for cloud resource selection."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from a2a.types import Message
from a2a.utils.errors import InvalidParamsError
from google.protobuf.json_format import MessageToDict

from iac_code.resource_selector.profiles import PROFILE_HASH, get_profile
from iac_code.resource_selector.validation import validate_answer_value
from iac_code.services.session_layout import ensure_session_owned_dir
from iac_code.services.session_storage import SessionStorage
from iac_code.types.stream_events import CloudResourceSelectionEvent
from iac_code.utils.file_security import ensure_private_file
from iac_code.utils.state_io import atomic_write_json, cross_process_file_lock

RESOURCE_SELECTION_SCHEMA_VERSION = 1
_INPUT_ID = re.compile(r"^resource-[0-9a-f]{32}$")


@dataclass(frozen=True)
class ResourceSelectionResponse:
    task_id: str
    context_id: str
    input_id: str
    tool_use_id: str
    status: str
    selector_id: str | None = None
    value: str | None = None
    label: str | None = None
    source: dict[str, Any] | None = None
    options_empty: bool | None = None

    def tool_response(self, *, expected_selector_id: str) -> dict[str, Any]:
        if self.status == "canceled":
            result: dict[str, Any] = {
                "status": "canceled",
                "input_id": self.input_id,
                "selector_id": expected_selector_id,
            }
            if self.options_empty is not None:
                result["options_empty"] = self.options_empty
            return result
        return {
            "status": "selected",
            "input_id": self.input_id,
            "selector_id": self.selector_id,
            "value": self.value,
            "label": self.label or self.value,
        }

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "schemaVersion": RESOURCE_SELECTION_SCHEMA_VERSION,
            "kind": "cloud_resource_selection",
            "status": self.status,
            "requestTaskId": self.task_id,
            "contextId": self.context_id,
            "inputId": self.input_id,
            "toolUseId": self.tool_use_id,
        }
        if self.status == "selected":
            assert self.selector_id is not None and self.value is not None
            result.update(selectorId=self.selector_id, value=self.value, label=self.label or self.value)
            if self.source is not None:
                result["source"] = self.source
        elif self.options_empty is not None:
            result["optionsEmpty"] = self.options_empty
        return result


@dataclass
class PendingResourceSelection:
    task_id: str
    context_id: str
    session_id: str
    cwd: str
    event: CloudResourceSelectionEvent
    store: ResourceSelectionCheckpointStore
    state: str = "pending"
    response: ResourceSelectionResponse | None = None
    continuation: Any | None = field(default=None, repr=False)
    resume_from_checkpoint: bool = field(default=False, repr=False)
    claim_lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)

    def envelope(self) -> dict[str, Any]:
        return resource_selection_input_envelope(
            self.event,
            task_id=self.task_id,
            context_id=self.context_id,
        )


class ResourceSelectionCheckpointStore:
    """Atomic, session-owned durable records with no expiry semantics."""

    def __init__(self, cwd: str, session_id: str, *, storage: SessionStorage | None = None) -> None:
        storage = storage or SessionStorage()
        session_dir = storage.v2_session_dir(cwd, session_id)
        if session_dir is None:
            session_dir = storage.ensure_v2_session_dir_for_new_session(cwd, session_id)
        if session_dir is None:
            raise ValueError("resource selections require a version 2 session directory")
        self._session_dir = Path(session_dir)
        self._dir = ensure_session_owned_dir(self._session_dir, self._session_dir / "a2a" / "resource-selections")
        self._lock = self._dir / ".lock"

    def create(self, record: Mapping[str, Any]) -> dict[str, Any]:
        candidate = dict(record)
        input_id = _validated_input_id(candidate.get("inputId"))
        path = self._path(input_id)
        with cross_process_file_lock(self._lock):
            if path.exists():
                existing = self._read(path)
                if existing == candidate:
                    return candidate
                raise ValueError("resource selection already exists")
            atomic_write_json(path, candidate, durable=True)
            ensure_private_file(path)
        return candidate

    def load(self, input_id: str) -> dict[str, Any] | None:
        path = self._path(_validated_input_id(input_id))
        with cross_process_file_lock(self._lock):
            return self._read(path)

    def claim(self, response: ResourceSelectionResponse) -> tuple[dict[str, Any], bool]:
        path = self._path(_validated_input_id(response.input_id))
        response_dict = response.to_dict()
        with cross_process_file_lock(self._lock):
            record = self._read(path)
            if record is None:
                raise InvalidParamsError("resource_selection_resume_invalid: pending input not found")
            if record.get("taskId") != response.task_id or record.get("contextId") != response.context_id:
                raise InvalidParamsError("resource_selection_resume_invalid: task context mismatch")
            existing = record.get("response")
            if record.get("state") in {"claimed", "resolved"}:
                if existing == response_dict:
                    return record, True
                raise InvalidParamsError("resource_selection_resume_invalid: answer conflicts with prior answer")
            if record.get("state") != "pending":
                raise InvalidParamsError("resource_selection_resume_invalid: pending input is unavailable")
            record["state"] = "claimed"
            record["response"] = response_dict
            atomic_write_json(path, record, durable=True)
            ensure_private_file(path)
            return record, False

    def resolve(self, input_id: str) -> None:
        path = self._path(_validated_input_id(input_id))
        with cross_process_file_lock(self._lock):
            record = self._read(path)
            if record is None:
                return
            record["state"] = "resolved"
            atomic_write_json(path, record, durable=True)
            ensure_private_file(path)

    def _path(self, input_id: str) -> Path:
        return self._dir / (input_id + ".json")

    @staticmethod
    def _read(path: Path) -> dict[str, Any] | None:
        if not path.exists():
            return None
        import json

        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise InvalidParamsError("resource_selection_resume_invalid: checkpoint is unreadable") from exc
        return value if isinstance(value, dict) else None


class ResourceSelectionInputRegistry:
    def __init__(self) -> None:
        self._pending: dict[tuple[str, str], PendingResourceSelection] = {}
        self._lock = asyncio.Lock()

    async def register(self, pending: PendingResourceSelection) -> None:
        key = (pending.task_id, pending.event.input_id)
        async with self._lock:
            if key in self._pending:
                raise InvalidParamsError("resource_selection_resume_invalid: input already pending")
            self._pending[key] = pending

    async def pending_for_response(self, response: ResourceSelectionResponse) -> PendingResourceSelection | None:
        async with self._lock:
            return self._pending.get((response.task_id, response.input_id))

    async def answer(
        self,
        pending: PendingResourceSelection,
        response: ResourceSelectionResponse,
        *,
        before_delivery: Callable[[], Awaitable[None]] | None = None,
    ) -> tuple[bool, bool]:
        _validate_response_against_pending(response, pending)
        async with pending.claim_lock:
            record, replayed = pending.store.claim(response)
            if replayed:
                return True, True
            pending.state = "claimed"
            pending.response = response
            if before_delivery is not None:
                await before_delivery()
            tool_response = response.tool_response(expected_selector_id=pending.event.selector_id)
            future = pending.event.response_future
            if not pending.resume_from_checkpoint and future is not None and not future.done():
                future.set_result(tool_response)
            return bool(record), False

    async def claim_continuation(self, pending: PendingResourceSelection) -> Any | None:
        async with pending.claim_lock:
            continuation = pending.continuation
            pending.continuation = None
            return continuation

    async def complete(self, pending: PendingResourceSelection) -> None:
        pending.store.resolve(pending.event.input_id)
        async with self._lock:
            self._pending.pop((pending.task_id, pending.event.input_id), None)

    async def has_pending_task(self, task_id: str) -> bool:
        async with self._lock:
            return any(key[0] == task_id for key in self._pending)

    async def cancel_task(self, task_id: str) -> int:
        """Discard every live wait owned by an explicitly terminated task."""

        async with self._lock:
            pending = [item for key, item in self._pending.items() if key[0] == task_id]
            for item in pending:
                self._pending.pop((item.task_id, item.event.input_id), None)
        for item in pending:
            item.continuation = None
            item.store.resolve(item.event.input_id)
        return len(pending)


def resource_selection_input_envelope(
    event: CloudResourceSelectionEvent,
    *,
    task_id: str,
    context_id: str,
) -> dict[str, Any]:
    return {
        "schemaVersion": RESOURCE_SELECTION_SCHEMA_VERSION,
        "kind": "cloud_resource_selection",
        "requestTaskId": task_id,
        "contextId": context_id,
        "inputId": event.input_id,
        "toolUseId": event.tool_use_id,
        "prompt": event.question,
        "required": True,
        "selector": {
            "id": event.selector_id,
            "associationProperty": event.association_property,
            "outputKind": event.output_kind,
            "associationPropertyMetadata": event.association_property_metadata,
            "source": event.source,
            "profileHash": event.profile_hash,
        },
    }


def checkpoint_record(pending: PendingResourceSelection) -> dict[str, Any]:
    frame = pending.event.continuation_frame
    if not isinstance(frame, dict):
        raise ValueError("resource selection continuation is unavailable")
    return {
        "schemaVersion": RESOURCE_SELECTION_SCHEMA_VERSION,
        "kind": "cloud_resource_selection",
        "state": "pending",
        "taskId": pending.task_id,
        "contextId": pending.context_id,
        "sessionId": pending.session_id,
        "inputId": pending.event.input_id,
        "toolUseId": pending.event.tool_use_id,
        "profileHash": pending.event.profile_hash,
        "selector": pending.envelope()["selector"],
        "prompt": pending.event.question,
        "continuationFrame": frame,
    }


def parse_resource_selection_response(message: Message | None) -> ResourceSelectionResponse | None:
    if message is None:
        return None
    metadata: Any = getattr(message, "metadata", None)
    if metadata is not None and hasattr(metadata, "DESCRIPTOR"):
        metadata = MessageToDict(metadata, preserving_proto_field_name=False)
    if not isinstance(metadata, Mapping):
        return None
    iac_code = metadata.get("iac_code")
    payload = iac_code.get("inputResponse") if isinstance(iac_code, Mapping) else None
    if not isinstance(payload, Mapping) or payload.get("kind") != "cloud_resource_selection":
        return None
    if payload.get("schemaVersion") != RESOURCE_SELECTION_SCHEMA_VERSION:
        raise InvalidParamsError("resource_selection_resume_invalid: unsupported schema version")
    status = payload.get("status")
    if status not in {"selected", "canceled"}:
        raise InvalidParamsError("resource_selection_resume_invalid: status is invalid")
    task_id = _required_string(payload, "requestTaskId")
    context_id = _required_string(payload, "contextId")
    input_id = _validated_input_id(payload.get("inputId"))
    tool_use_id = _required_string(payload, "toolUseId")
    if status == "canceled":
        options_empty = payload.get("optionsEmpty")
        if options_empty is not None and not isinstance(options_empty, bool):
            raise InvalidParamsError("resource_selection_resume_invalid: optionsEmpty is invalid")
        return ResourceSelectionResponse(
            task_id,
            context_id,
            input_id,
            tool_use_id,
            status,
            options_empty=options_empty,
        )
    selector_id = _required_string(payload, "selectorId")
    value = _required_string(payload, "value")
    label = payload.get("label")
    if label is not None and (not isinstance(label, str) or len(label) > 1024):
        raise InvalidParamsError("resource_selection_resume_invalid: label is invalid")
    source = payload.get("source")
    if source is not None and not isinstance(source, dict):
        raise InvalidParamsError("resource_selection_resume_invalid: source is invalid")
    return ResourceSelectionResponse(
        task_id,
        context_id,
        input_id,
        tool_use_id,
        status,
        selector_id,
        value,
        label,
        source,
    )


def _validate_response_against_pending(
    response: ResourceSelectionResponse,
    pending: PendingResourceSelection,
) -> None:
    event = pending.event
    if (
        response.task_id != pending.task_id
        or response.context_id != pending.context_id
        or response.tool_use_id != event.tool_use_id
        or response.input_id != event.input_id
    ):
        raise InvalidParamsError("resource_selection_resume_invalid: correlation mismatch")
    if response.status == "canceled":
        return
    if response.selector_id != event.selector_id:
        raise InvalidParamsError("resource_selection_resume_invalid: selector mismatch")
    profile = get_profile(event.selector_id)
    if profile is None or not profile.enabled or event.profile_hash != PROFILE_HASH:
        raise InvalidParamsError("resource_selection_resume_invalid: selector profile mismatch")
    if validate_answer_value(profile, response.value, metadata=event.association_property_metadata):
        raise InvalidParamsError("resource_selection_resume_invalid: selector value is invalid")
    if profile.source_selector_id and response.source != event.source:
        raise InvalidParamsError("resource_selection_resume_invalid: source mismatch")
    if not profile.source_selector_id and response.source is not None:
        raise InvalidParamsError("resource_selection_resume_invalid: source mismatch")


def pending_from_record(
    *,
    record: Mapping[str, Any],
    cwd: str,
    store: ResourceSelectionCheckpointStore,
) -> PendingResourceSelection:
    selector = record.get("selector")
    frame = record.get("continuationFrame")
    if not isinstance(selector, Mapping) or not isinstance(frame, dict):
        raise InvalidParamsError("resource_selection_resume_invalid: checkpoint contract is invalid")
    event = CloudResourceSelectionEvent(
        tool_use_id=_required_string(record, "toolUseId"),
        input_id=_validated_input_id(record.get("inputId")),
        question=_required_string(record, "prompt"),
        selector_id=_required_string(selector, "id"),
        association_property=_required_string(selector, "associationProperty"),
        output_kind=_required_string(selector, "outputKind"),
        association_property_metadata=dict(selector.get("associationPropertyMetadata") or {}),
        source=dict(selector["source"]) if isinstance(selector.get("source"), Mapping) else None,
        profile_hash=_required_string(record, "profileHash"),
        continuation_frame=frame,
    )
    return PendingResourceSelection(
        task_id=_required_string(record, "taskId"),
        context_id=_required_string(record, "contextId"),
        session_id=_required_string(record, "sessionId"),
        cwd=cwd,
        event=event,
        store=store,
        state=str(record.get("state") or "pending"),
    )


def _required_string(value: Mapping[str, Any], key: str) -> str:
    item = value.get(key)
    if not isinstance(item, str) or not item or len(item) > 1024:
        raise InvalidParamsError("resource_selection_resume_invalid: {} is invalid".format(key))
    return item


def _validated_input_id(value: object) -> str:
    if not isinstance(value, str) or _INPUT_ID.fullmatch(value) is None:
        raise InvalidParamsError("resource_selection_resume_invalid: inputId is invalid")
    return value
