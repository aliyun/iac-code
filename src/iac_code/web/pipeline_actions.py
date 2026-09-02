"""Thin Web adapters over existing A2A pipeline action paths."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from iac_code.i18n import _
from iac_code.pipeline.engine.user_input import PipelineInputContent, normalize_pipeline_user_input
from iac_code.web.events import normalize_event_payload
from iac_code.web.pipeline import PipelineCandidateSelection
from iac_code.web.pipeline_transcript import PipelineTranscriptTranslator
from iac_code.web.runtime import WebModelSelection

# Async sink that receives batches of translated web SSE events for live forwarding.
PipelineEventSink = Callable[[list[dict[str, Any]]], Awaitable[None]]
# Session-bound resolver: given a PermissionRequestEvent, surface the web permission UI
# and block until the user answers, returning the approve/deny bool (Issue 6). Kept as
# ``Any``-typed to avoid importing a2a types here.
PipelinePermissionResolver = Callable[[Any], "Awaitable[bool] | bool"]

logger = logging.getLogger(__name__)


class PipelineActionRunner(Protocol):
    async def start(
        self,
        session: Any,
        message: str,
        image_ids: list[str],
        file_refs: list[str],
        *,
        model_selection: WebModelSelection | None = None,
        event_sink: PipelineEventSink | None = None,
        permission_resolver: PipelinePermissionResolver | None = None,
        envelope_observer: Callable[[Mapping[str, Any]], None] | None = None,
    ) -> "PipelineActionResult": ...

    async def select_candidate(
        self,
        session: Any,
        selection: PipelineCandidateSelection,
        *,
        model_selection: WebModelSelection | None = None,
        event_sink: PipelineEventSink | None = None,
        permission_resolver: PipelinePermissionResolver | None = None,
        envelope_observer: Callable[[Mapping[str, Any]], None] | None = None,
    ) -> "PipelineActionResult": ...

    async def interrupt(
        self,
        session: Any,
        message: str,
        image_ids: list[str],
        file_refs: list[str],
        *,
        model_selection: WebModelSelection | None = None,
        event_sink: PipelineEventSink | None = None,
        permission_resolver: PipelinePermissionResolver | None = None,
    ) -> "PipelineActionResult": ...

    async def resume_permission(
        self,
        session: Any,
        checkpoint: dict[str, Any],
        *,
        model_selection: WebModelSelection | None = None,
        event_sink: PipelineEventSink | None = None,
        permission_resolver: PipelinePermissionResolver | None = None,
    ) -> "PipelineActionResult": ...

    async def rebuild_permission_audit_event(
        self,
        session: Any,
        checkpoint: dict[str, Any],
        recovered: Any,
        *,
        model_selection: WebModelSelection | None = None,
    ) -> Any: ...


@dataclass(frozen=True)
class PipelineActionResult:
    accepted: bool
    status_code: int
    response: dict[str, Any]
    events: list[dict[str, Any]] = field(default_factory=list)
    # 终态枚举(failed/canceled/...)。非 None 表示这次「不接受」是流水线到达终态,
    # 而非普通动作校验失败——此时主转录已有彩色结局行代替底部错误条,故上层不再另发
    # error 事件把英文错误钉在最新一条。普通校验失败(404/400/500)保持 None,照常报错。
    terminal_outcome: str | None = None


class A2APipelineActionRunner:
    """Route Web pipeline actions through the existing A2A pipeline executor."""

    def __init__(self, runtime_owner: Any | None = None) -> None:
        from iac_code.a2a.exposure import A2AExposureType
        from iac_code.a2a.metrics import NoOpA2AMetrics
        from iac_code.a2a.persistence import A2APersistenceStore
        from iac_code.a2a.runtime_registry import A2ARuntimeOwner, get_runtime_owner
        from iac_code.a2a.task_store import A2ATaskStore
        from iac_code.config import DEFAULT_MODEL, get_config_dir, load_saved_model

        persistence_root = get_config_dir() / "a2a"
        persistence = A2APersistenceStore(persistence_root)
        owner = runtime_owner or get_runtime_owner(persistence_root=persistence_root)
        uses_web_global_defaults = owner is None
        if owner is None:
            metrics = NoOpA2AMetrics()
            owner = A2ARuntimeOwner(
                task_store=A2ATaskStore(metrics=metrics, persistence=persistence),
                model=load_saved_model() or DEFAULT_MODEL,
                metrics=metrics,
                persistence_root=persistence_root,
                # The Web console consumes the pre-remote-redaction (loopback) envelope only,
                # so raw thinking never leaves this process. Exposure gating is a *remote*
                # privacy concept; enabling RAW_THINKING here lets publish() keep thinking_delta
                # for the local Web sink so pipeline mode shows 正在思考/思考完成 like normal mode.
                # Only touches this fallback owner (standalone Web); a shared dispatcher owner
                # keeps its own exposure config and remote clients are unaffected.
                thinking_exposure_types=frozenset({A2AExposureType.RAW_THINKING, A2AExposureType.TOOL_TRACE}),
            )
        self._owner = owner
        self._task_store = owner.task_store
        self._uses_web_global_defaults = uses_web_global_defaults

    async def startup(self) -> None:
        """Start background maintenance for the fallback store owned by Web."""
        if self._uses_web_global_defaults:
            await self._task_store.start_cleanup_loop()

    async def shutdown(self) -> None:
        """Release fallback runtimes and maintenance tasks owned by Web."""
        if self._uses_web_global_defaults:
            await self._task_store.stop_cleanup_loop()

    async def start(
        self,
        session: Any,
        message: str,
        image_ids: list[str],
        file_refs: list[str],
        *,
        model_selection: WebModelSelection | None = None,
        event_sink: PipelineEventSink | None = None,
        permission_resolver: PipelinePermissionResolver | None = None,
        envelope_observer: Callable[[Mapping[str, Any]], None] | None = None,
    ) -> PipelineActionResult:
        if not getattr(session, "context_id", None) or not getattr(session, "task_id", None):
            return _action_error(_("pipeline contextId and taskId are required"), status_code=400)
        pipeline_input = _pipeline_user_input_from_web(session, message, image_ids, file_refs)
        return await self._execute(
            session,
            pipeline_input,
            action="started",
            events=[
                {
                    "kind": "pipeline.started",
                    "pipelineName": getattr(session, "pipeline_name", None) or "selling",
                }
            ],
            model_selection=model_selection,
            event_sink=event_sink,
            permission_resolver=permission_resolver,
            envelope_observer=envelope_observer,
        )

    async def select_candidate(
        self,
        session: Any,
        selection: PipelineCandidateSelection,
        *,
        model_selection: WebModelSelection | None = None,
        event_sink: PipelineEventSink | None = None,
        permission_resolver: PipelinePermissionResolver | None = None,
        envelope_observer: Callable[[Mapping[str, Any]], None] | None = None,
    ) -> PipelineActionResult:
        unavailable = await self._unavailable_result(session)
        if unavailable is not None:
            return unavailable
        task_record = await self._task_store.get_task_record(session.task_id)
        if task_record.state != "input-required":
            return _action_error(_("pipeline is not waiting for candidate input"), status_code=409)
        return await self._execute(
            session,
            selection.encoded_input,
            action="candidate_selected",
            events=[
                {
                    "kind": "candidate.selected",
                    "candidateName": selection.candidate_name,
                    "candidateIndex": selection.candidate_index,
                    "parameterOverrides": dict(selection.parameter_overrides),
                }
            ],
            model_selection=model_selection,
            event_sink=event_sink,
            permission_resolver=permission_resolver,
            envelope_observer=envelope_observer,
        )

    async def interrupt(
        self,
        session: Any,
        message: str,
        image_ids: list[str],
        file_refs: list[str],
        *,
        model_selection: WebModelSelection | None = None,
        event_sink: PipelineEventSink | None = None,
        permission_resolver: PipelinePermissionResolver | None = None,
    ) -> PipelineActionResult:
        unavailable = await self._unavailable_result(session, require_active_task=True)
        if unavailable is not None:
            return unavailable
        if not await self._task_store.is_task_active(session.task_id):
            return _action_error(_("pipeline is not active"), status_code=409)
        pipeline_input = _pipeline_user_input_from_web(session, message, image_ids, file_refs)
        return await self._execute(
            session,
            pipeline_input,
            action="interrupt",
            events=[
                {
                    "kind": "pipeline.interrupt.submitted",
                    "pipelineInterrupt": True,
                }
            ],
            model_selection=model_selection,
            event_sink=event_sink,
            permission_resolver=permission_resolver,
        )

    async def resume_permission(
        self,
        session: Any,
        checkpoint: dict[str, Any],
        *,
        model_selection: WebModelSelection | None = None,
        event_sink: PipelineEventSink | None = None,
        permission_resolver: PipelinePermissionResolver | None = None,
    ) -> PipelineActionResult:
        unavailable = await self._unavailable_result(session)
        if unavailable is not None:
            return unavailable
        return await self._execute(
            session,
            "",
            action="permission_recovered",
            events=[{"kind": "permission.recovered"}],
            model_selection=model_selection,
            event_sink=event_sink,
            permission_resolver=permission_resolver,
            permission_checkpoint=checkpoint,
        )

    async def _unavailable_result(
        self,
        session: Any,
        *,
        require_active_task: bool = False,
    ) -> PipelineActionResult | None:
        context_id = getattr(session, "context_id", None)
        task_id = getattr(session, "task_id", None)
        if not context_id or not task_id:
            return _action_error(_("pipeline contextId and taskId are required"), status_code=400)
        try:
            context_record = await self._task_store.get_context_record(context_id)
        except ValueError:
            return _action_error(_("pipeline context not found"), status_code=404)
        if context_record.cwd != session.cwd:
            return _action_error(_("pipeline context belongs to a different workspace"), status_code=409)
        try:
            task_record = await self._task_store.get_task_record(task_id)
        except ValueError:
            return _action_error(_("pipeline task not found"), status_code=404)
        if task_record.context_id != context_id:
            return _action_error(_("pipeline task belongs to a different context"), status_code=409)
        if require_active_task and context_record.active_task_id != task_id:
            return _action_error(_("pipeline is not active"), status_code=409)
        return None

    def _resolve_auto_approve(self, session: Any) -> bool:
        """Map the web session's permission mode onto the a2a auto-approve flag.

        The pipeline executor has no interactive permission channel yet, so a
        missing resolver + ``auto_approve_permissions=False`` denies every tool
        (this is why "完全访问" sessions had all permissions rejected). When the
        session opted into a non-interactive mode (``bypass_permissions`` /
        ``dont_ask``) we approve automatically; otherwise we defer to the
        runtime owner's flag. ``default`` / ``accept_edits`` still require the
        interactive resolver (not wired here) and continue to deny writes.
        """
        if self._owner.auto_approve_permissions:
            return True
        from iac_code.types.permissions import PermissionMode

        mode = getattr(session, "permission_mode", None)
        if isinstance(mode, PermissionMode):
            return mode in (PermissionMode.BYPASS_PERMISSIONS, PermissionMode.DONT_ASK)
        return str(mode or "").strip().lower() in ("bypass_permissions", "dont_ask")

    def _executor_for_session(
        self,
        session: Any,
        *,
        model_selection: WebModelSelection | None,
        permission_resolver: PipelinePermissionResolver | None,
    ) -> Any:
        from iac_code.a2a.pipeline_executor import IacCodeA2APipelineExecutor

        if model_selection is not None:
            session_model = model_selection.model
            provider_key_override = model_selection.provider
            provider_api_key_override = model_selection.provider_api_key
            provider_base_url_override = model_selection.provider_base_url
            provider_config_frozen = model_selection.provider_config_frozen
            provider_config_override = model_selection.provider_config_override
            effort_override = model_selection.effort
        elif getattr(self, "_uses_web_global_defaults", False):
            from iac_code.web.runtime import model_selection_for_session

            selection = model_selection_for_session(session)
            session_model = selection.model
            provider_key_override = selection.provider
            provider_api_key_override = selection.provider_api_key
            provider_base_url_override = selection.provider_base_url
            provider_config_frozen = selection.provider_config_frozen
            provider_config_override = selection.provider_config_override
            effort_override = selection.effort
        else:
            session_model = getattr(session, "model", None) or self._owner.model
            provider_key_override = getattr(session, "provider", None)
            provider_api_key_override = None
            provider_base_url_override = None
            provider_config_frozen = False
            provider_config_override = None
            effort_override = getattr(session, "effort", None)

        auto_approve = self._resolve_auto_approve(session)
        resolver = None if auto_approve else (permission_resolver or self._owner.permission_resolver)
        return IacCodeA2APipelineExecutor(
            task_store=self._task_store,
            model=session_model,
            provider_key_override=provider_key_override,
            provider_api_key_override=provider_api_key_override,
            provider_base_url_override=provider_base_url_override,
            provider_config_frozen=provider_config_frozen,
            provider_config_override=provider_config_override,
            effort_override=effort_override,
            metrics=self._owner.metrics,
            artifact_store=self._owner.artifact_store,
            push_notifier=self._owner.push_notifier,
            permission_resolver=resolver,
            auto_approve_permissions=auto_approve,
            thinking_exposure_types=self._owner.thinking_exposure_types,
            # The session owns the pipeline the user picked in the mode selector;
            # without this the executor would always fall back to the process-wide
            # IAC_CODE_PIPELINE_NAME default and silently run `selling`.
            pipeline_name=_session_pipeline_name(session),
        )

    async def rebuild_permission_audit_event(
        self,
        session: Any,
        checkpoint: dict[str, Any],
        recovered: Any,
        *,
        model_selection: WebModelSelection | None = None,
    ) -> Any:
        executor = self._executor_for_session(
            session,
            model_selection=model_selection,
            permission_resolver=None,
        )
        return await executor.rebuild_permission_audit_event(
            cwd=session.cwd,
            session_id=session.session_id,
            checkpoint=checkpoint,
            recovered=recovered,
        )

    async def _execute(
        self,
        session: Any,
        pipeline_input: str | PipelineInputContent,
        *,
        action: str,
        events: list[dict[str, Any]],
        model_selection: WebModelSelection | None = None,
        event_sink: PipelineEventSink | None = None,
        permission_resolver: PipelinePermissionResolver | None = None,
        envelope_observer: Callable[[Mapping[str, Any]], None] | None = None,
        permission_checkpoint: dict[str, Any] | None = None,
    ) -> PipelineActionResult:
        # Issue 6: the pipeline executor denies every tool when it has no resolver and
        # auto-approve is off. When the session opts into a non-interactive mode we keep
        # the silent auto-approve path (resolver=None + auto_approve=True); otherwise we
        # thread the session-bound web resolver so tool permission prompts surface in the
        # browser and block on the user's answer instead of auto-denying.
        executor = self._executor_for_session(
            session,
            model_selection=model_selection,
            permission_resolver=permission_resolver,
        )
        task = await self._task_store.get_or_create_task(task_id=session.task_id, context_id=session.context_id)
        history_envelopes = await self._load_pipeline_envelope_history(session) if event_sink is not None else []
        event_queue = (
            _ForwardingEventQueue(
                event_sink,
                envelope_observer=envelope_observer,
                history_envelopes=history_envelopes,
            )
            if event_sink is not None
            else _CollectingEventQueue()
        )
        try:
            await executor.execute(
                context=_WebA2AContext(),
                event_queue=event_queue,
                task=task,
                task_id=session.task_id,
                context_id=session.context_id,
                cwd=session.cwd,
                pipeline_input=normalize_pipeline_user_input(pipeline_input),
                permission_checkpoint=permission_checkpoint,
            )
        except Exception as exc:
            return _action_error(str(exc)[:500], status_code=500)
        terminal_result = await self._terminal_result(session, event_queue.events)
        if terminal_result is not None:
            return terminal_result
        result_events = list(events)
        for event in event_queue.events:
            web_event = normalize_a2a_event_for_web(event)
            if web_event is not None:
                result_events.append(web_event)
        snapshot = await load_pipeline_snapshot(context_id=session.context_id, task_id=session.task_id)
        if snapshot is not None:
            result_events.append(
                {
                    "webEventType": "pipeline.snapshot",
                    "snapshot": snapshot,
                    "contextId": session.context_id,
                    "taskId": session.task_id,
                }
            )
        return PipelineActionResult(
            accepted=True,
            status_code=202,
            response={"accepted": True, "action": action, "eventCount": len(event_queue.events)},
            events=result_events,
        )

    async def _load_pipeline_envelope_history(self, session: Any) -> list[dict[str, Any]]:
        """Load prior envelopes so a resumed live translator keeps cumulative state.

        Each Web input invokes the A2A executor separately.  Without hydrating the
        translator, the continuation does not know the paused step's marker, elapsed
        segments, or pending question, so the live UI stays folded/at ``0s`` until a
        reload reconstructs the journal.  Hydration mutates translator state only;
        the historical Web events are deliberately discarded by the queue.
        """
        try:
            from iac_code.a2a.pipeline_journal import A2APipelineJournal
            from iac_code.a2a.pipeline_paths import existing_a2a_pipeline_dir_for_session

            context = await self._task_store.get_context_record(session.context_id)
            pipeline_dir = existing_a2a_pipeline_dir_for_session(
                cwd=context.cwd,
                session_id=context.session_id,
            )
            return A2APipelineJournal(pipeline_dir).read_all_repairing_tail()
        except Exception:
            logger.debug("Unable to hydrate Web pipeline transcript history", exc_info=True)
            return []

    async def _terminal_result(self, session: Any, events: list[Any]) -> PipelineActionResult | None:
        event_result = _terminal_result_from_status_events(events)
        if event_result is not None:
            return event_result
        try:
            task_record = await self._task_store.get_task_record(session.task_id)
        except ValueError:
            return _action_error(_("pipeline task not found"), status_code=404)
        if task_record.state == "failed":
            return _action_error(_("pipeline action failed"), status_code=409, terminal_outcome="failed")
        if task_record.state == "canceled":
            return _action_error(_("pipeline action canceled"), status_code=409, terminal_outcome="canceled")
        return None


class _CollectingEventQueue:
    def __init__(self) -> None:
        self.events: list[Any] = []

    async def enqueue_event(self, event: Any) -> None:
        self.events.append(event)


def extract_pipeline_envelope(event: Any) -> Mapping[str, Any] | None:
    """Pull the fine-grained pipeline envelope embedded in an A2A status event.

    The executor stashes the full envelope under
    ``metadata.iac_code.pipeline`` (see ``a2a/pipeline_stream.py``). Returns the
    envelope mapping when present, else ``None`` (e.g. for task/artifact events).
    """
    data = _a2a_event_dict(event)
    metadata = data.get("metadata")
    if not isinstance(metadata, Mapping):
        return None
    container = metadata.get("iac_code")
    if not isinstance(container, Mapping):
        container = metadata.get("iacCode")
    if not isinstance(container, Mapping):
        return None
    envelope = container.get("pipeline")
    return envelope if isinstance(envelope, Mapping) else None


class _ForwardingEventQueue:
    """Collect A2A events while live-forwarding translated web SSE events.

    Behaves like ``_CollectingEventQueue`` (retains ``events`` for the terminal /
    snapshot pass) but additionally feeds each event through a shared
    :class:`PipelineTranscriptTranslator` and awaits ``sink`` with any resulting
    web events, so the main transcript streams while the pipeline runs.
    """

    def __init__(
        self,
        sink: PipelineEventSink,
        *,
        envelope_observer: Callable[[Mapping[str, Any]], None] | None = None,
        history_envelopes: Sequence[Mapping[str, Any]] | None = None,
    ) -> None:
        self.events: list[Any] = []
        self._sink = sink
        self._translator = PipelineTranscriptTranslator()
        # Prime all stateful folds (markers, durations, pending questions) without
        # replaying historical events to the browser.  Only envelopes produced by
        # this continuation are forwarded below.
        self._translator.translate_all(history_envelopes or [])
        self._envelope_observer = envelope_observer

    async def enqueue_event(self, event: Any) -> None:
        self.events.append(event)

    async def enqueue_local_pipeline_envelope(self, envelope: Mapping[str, Any]) -> None:
        """Forward the pre-remote-redaction envelope only to the loopback Web sink."""
        web_events = self._translator.push(envelope)
        if web_events:
            await self._sink(web_events)
        if self._envelope_observer is not None:
            try:
                self._envelope_observer(envelope)
            except Exception:
                logger.exception("diagram optimization envelope observer failed")


class _WebA2AContext:
    metadata: dict[str, Any] = {}


def create_pipeline_action_runner() -> PipelineActionRunner:
    return A2APipelineActionRunner()


async def load_pipeline_snapshot(*, context_id: str | None, task_id: str | None) -> dict[str, Any] | None:
    """Load the latest public reducer snapshot for an A2A pipeline action."""
    if not context_id and not task_id:
        return None
    try:
        from iac_code.web.pipeline import create_a2a_pipeline_recovery_service

        state = await create_a2a_pipeline_recovery_service().get_state(context_id=context_id, task_id=task_id)
    except Exception:
        return None
    if isinstance(state, dict):
        snapshot = state.get("snapshot")
        if isinstance(snapshot, dict):
            return normalize_event_payload(snapshot)
    return None


def normalize_a2a_event_for_web(event: Any) -> dict[str, Any] | None:
    """Normalize live A2A status/task/artifact events into Web pipeline event payloads."""
    data = _a2a_event_dict(event)
    task_id = _string_value(data.get("taskId") or data.get("task_id") or getattr(event, "task_id", None))
    context_id = _string_value(data.get("contextId") or data.get("context_id") or getattr(event, "context_id", None))
    status = data.get("status") if isinstance(data.get("status"), dict) else None
    artifact = data.get("artifact") if isinstance(data.get("artifact"), dict) else None
    if status is not None or getattr(event, "status", None) is not None:
        state = _state_name(
            (status or {}).get("state")
            if isinstance(status, dict)
            else getattr(getattr(event, "status", None), "state", None)
        )
        payload = {
            "kind": "a2a.task.status",
            "taskId": task_id,
            "contextId": context_id,
            "state": state,
            "status": status or {},
            "message": _status_message_text(event),
        }
        return normalize_event_payload(payload)
    if artifact is not None or getattr(event, "artifact", None) is not None:
        payload = {
            "kind": "a2a.task.artifact",
            "taskId": task_id,
            "contextId": context_id,
            "artifact": artifact or {},
        }
        return normalize_event_payload(payload)
    if data:
        data.setdefault("kind", "a2a.task.event")
        return normalize_event_payload(data)
    return None


def _a2a_event_dict(event: Any) -> dict[str, Any]:
    if isinstance(event, dict):
        return dict(event)
    try:
        from google.protobuf.json_format import MessageToDict

        data = MessageToDict(event, preserving_proto_field_name=False)
        return data if isinstance(data, dict) else {}
    except Exception:
        pass
    payload: dict[str, Any] = {}
    for source_name, target_name in (
        ("task_id", "taskId"),
        ("context_id", "contextId"),
        ("status", "status"),
        ("artifact", "artifact"),
    ):
        value = getattr(event, source_name, None)
        if value is not None:
            payload[target_name] = value
    return payload


def _string_value(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _session_pipeline_name(session: Any) -> str | None:
    """Pipeline override this session asks for, or ``None`` for the process default.

    The stored name reaches us from settings.yml and from the create-session
    payload, so an unknown value is possible (typo, or a session created against
    a build that shipped another pipeline). Since this value now decides which
    pipeline runs, an unchecked bad name would make every pipeline turn fail with
    ``Unknown pipeline`` — fall back to the process default instead.
    """
    name = getattr(session, "pipeline_name", None)
    if not isinstance(name, str) or not name.strip():
        return None
    name = name.strip()
    from iac_code.pipeline import discover_pipelines

    if name not in discover_pipelines():
        logger.warning("Ignoring unknown session pipeline name %r; falling back to the process default", name)
        return None
    return name


def _action_error(message: str, *, status_code: int, terminal_outcome: str | None = None) -> PipelineActionResult:
    return PipelineActionResult(
        accepted=False,
        status_code=status_code,
        response={"accepted": False, "error": {"message": message}},
        terminal_outcome=terminal_outcome,
    )


def _terminal_result_from_status_events(events: list[Any]) -> PipelineActionResult | None:
    for event in reversed(events):
        status = getattr(event, "status", None)
        state = _state_name(getattr(status, "state", None))
        if state == "TASK_STATE_FAILED":
            return _action_error(
                _status_message_text(event) or _("pipeline action failed"), status_code=409, terminal_outcome="failed"
            )
        if state == "TASK_STATE_CANCELED":
            return _action_error(
                _status_message_text(event) or _("pipeline action canceled"),
                status_code=409,
                terminal_outcome="canceled",
            )
    return None


def _state_name(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, int):
        try:
            from a2a.types import TaskState

            return str(TaskState.Name(value))
        except Exception:
            return str(value)
    return str(value or "")


def _status_message_text(event: Any) -> str:
    try:
        from google.protobuf.json_format import MessageToDict

        data = MessageToDict(event, preserving_proto_field_name=False)
    except Exception:
        data = {}
    if isinstance(data, dict):
        text = _first_text_part(data.get("status", {}).get("message", {}).get("parts"))
        if text:
            return text
    status = getattr(event, "status", None)
    message = getattr(status, "message", None)
    parts = getattr(message, "parts", None)
    text = _first_text_part(parts)
    return text if text is not None else ""


def _first_text_part(parts: Any) -> str | None:
    if not isinstance(parts, list) and not hasattr(parts, "__iter__"):
        return None
    for part in parts:
        if isinstance(part, dict):
            text = part.get("text")
            if isinstance(text, str):
                return text
            root = part.get("root")
            if isinstance(root, dict) and isinstance(root.get("text"), str):
                return root["text"]
        text = getattr(part, "text", None)
        if isinstance(text, str):
            return text
        root = getattr(part, "root", None)
        text = getattr(root, "text", None)
        if isinstance(text, str):
            return text
    return None


def _pipeline_user_input_from_web(
    session: Any,
    message: str,
    image_ids: list[str],
    file_refs: list[str],
):
    from iac_code.agent.message import ContentBlock, ImageBlock, TextBlock
    from iac_code.web.files import safe_file_references
    from iac_code.web.images import load_cached_image

    text = message
    references = safe_file_references(file_refs, cwd=session.cwd, must_exist=True)
    if references:
        reference_text = "\n".join("- {}".format(reference) for reference in references)
        text = "{}\n\nReferenced files:\n{}".format(message, reference_text) if message.strip() else reference_text
    if not image_ids:
        return text
    blocks: list[ContentBlock] = [TextBlock(text=text)] if text.strip() else []
    for image_id in image_ids:
        image = load_cached_image(image_id, cwd=session.cwd, session_id=session.session_id)
        blocks.append(ImageBlock(media_type=image.media_type, data=image.base64_data))
    return blocks
