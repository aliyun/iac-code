"""AG-UI run coordination implemented only in terms of the public A2A client."""

from __future__ import annotations

import asyncio
import contextlib
import copy
import hashlib
import logging
import time
import uuid
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ag_ui.core import (
    Interrupt,
    RunAgentInput,
    RunErrorEvent,
    RunFinishedEvent,
    RunFinishedInterruptOutcome,
    RunFinishedSuccessOutcome,
    RunStartedEvent,
    TokenUsage,
)
from jsonschema import ValidationError as JsonSchemaValidationError
from jsonschema import validate as validate_json_schema

from iac_code.a2a.client import A2AClient
from iac_code.agui.errors import AdmissionError, AguiError, normalize_agui_language, translate_agui_error
from iac_code.agui.events import (
    A2AEventMapper,
    a2a_context_id,
    a2a_iac_code_metadata,
    a2a_iac_code_session_id,
    a2a_inputs,
    a2a_sideband_input_ids,
    a2a_state,
    a2a_task_id,
    aggregate_usage,
    interrupt_from_a2a,
    resume_value,
    timestamp_ms,
)
from iac_code.agui.inputs import (
    IacCodeForwardedProps,
    canonical_digest,
    latest_user_message,
    parse_forwarded_props,
    resolve_cwd,
    validate_tools,
)
from iac_code.agui.state import (
    AGUI_STATE_SCHEMA_VERSION,
    AguiStateStore,
    AguiStateStoreError,
    FileAguiThreadStateStore,
)

logger = logging.getLogger(__name__)
_FAILED_STATES = frozenset({"auth-required", "failed", "rejected"})
_SUCCESS_STATES = frozenset({"completed", "input-required"})


@dataclass
class PendingInput:
    value: dict[str, Any]
    interrupt: Interrupt
    sideband: bool = False


@dataclass
class ThreadBinding:
    thread_id: str
    context_id: str
    cwd: str
    user_id: str | None
    ros_invocation_id: str
    iac_code_session_id: str | None = None
    execution_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    task_id: str | None = None
    pending: dict[str, PendingInput] = field(default_factory=dict)
    pipeline_sequence: int = 0
    pipeline_open_steps: set[str] = field(default_factory=set)
    text_snapshot_digests: set[str] = field(default_factory=set)
    run_digests: dict[str, str] = field(default_factory=dict)
    applied_resume_digests: dict[tuple[str, str], str] = field(default_factory=dict)
    terminal_execution_ids: set[str] = field(default_factory=set)
    active_run_id: str | None = None
    expiry_task: asyncio.Task[None] | None = None


@dataclass
class RunTicket:
    run_input: RunAgentInput
    binding: ThreadBinding
    request_digest: str
    is_resume: bool
    preferred_language: str
    local_task: asyncio.Task[Any] | None = None
    completed: bool = False
    paused: bool = False


@dataclass(frozen=True)
class ResumePreparation:
    events: list[Any]
    needs_interrupt: bool


@dataclass
class ResumeAcceptance:
    accepted: bool = False


@dataclass(frozen=True)
class ResumeApplication:
    stream: Any
    acceptance: ResumeAcceptance
    resolved_tools: list[tuple[str, str]]
    sideband_recovery_after: int | None


class AguiA2AAdapter:
    """Map AG-UI lifecycle calls onto one local A2A execution kernel."""

    def __init__(
        self,
        *,
        a2a_url: str,
        client: Any | None = None,
        interrupt_ttl: int = 540,
        state_store: AguiStateStore | None = None,
        state_dir: str | Path | None = None,
    ) -> None:
        if state_store is not None and state_dir is not None:
            raise ValueError("state_store and state_dir are mutually exclusive")
        self.a2a_url = a2a_url
        self.client = client or A2AClient()
        self.interrupt_ttl = max(1, interrupt_ttl)
        self._state_store = state_store or FileAguiThreadStateStore(state_dir)
        self._lock = asyncio.Lock()
        self._threads: dict[str, ThreadBinding] = {}
        self._executions: dict[str, ThreadBinding] = {}
        self._started = False
        self.last_activity = time.monotonic()

    @property
    def is_idle(self) -> bool:
        return not any(binding.active_run_id or binding.pending for binding in self._threads.values())

    async def admit(
        self,
        run_input: RunAgentInput,
        request_digest: str,
        *,
        preferred_language: str | None = None,
    ) -> RunTicket:
        await self.start()
        async with self._lock:
            props = parse_forwarded_props(run_input.forwarded_props).iac_code
            cwd = resolve_cwd(props.cwd)
            validate_tools(run_input)
            try:
                binding = self._load_thread(run_input.thread_id)
            except AguiStateStoreError as exc:
                raise AdmissionError(
                    "STATE_UNAVAILABLE",
                    "The AG-UI adapter state is unavailable.",
                    status_code=503,
                ) from exc
            created = binding is None
            previous_execution: tuple[str, str | None, str, int, set[str], set[str], bool, bool] | None = None
            if binding is None:
                if run_input.resume is not None:
                    raise AdmissionError("EXECUTION_LOST", "The execution to resume is no longer available.")
                binding = ThreadBinding(
                    thread_id=run_input.thread_id,
                    context_id=_context_id(run_input.thread_id),
                    cwd=cwd,
                    user_id=props.user_id,
                    ros_invocation_id=props.ros_invocation_id,
                )
                self._threads[run_input.thread_id] = binding
                self._executions[binding.execution_id] = binding
            previous_digest = binding.run_digests.get(run_input.run_id)
            if previous_digest is not None:
                code = "DUPLICATE_RUN_ID" if previous_digest == request_digest else "RUN_ID_CONFLICT"
                raise AdmissionError(code, "The AG-UI run id has already been used.")
            if binding.active_run_id is not None:
                raise AdmissionError("THREAD_BUSY", "The AG-UI thread already has an active run.")
            if binding.cwd != cwd or binding.user_id != props.user_id:
                raise AdmissionError(
                    "THREAD_BINDING_CONFLICT",
                    "The AG-UI thread is already bound to another workspace or caller.",
                )
            if run_input.resume is not None:
                if props.ros_invocation_id != binding.ros_invocation_id:
                    raise AdmissionError("EXECUTION_LOST", "The resume request does not match the interrupted run.")
            elif binding.pending:
                raise AdmissionError("RESUME_REQUIRED", "The AG-UI thread is waiting for interrupt responses.")
            elif not created:
                previous_execution = (
                    binding.execution_id,
                    binding.task_id,
                    binding.ros_invocation_id,
                    binding.pipeline_sequence,
                    set(binding.pipeline_open_steps),
                    set(binding.text_snapshot_digests),
                    self._executions.get(binding.execution_id) is binding,
                    binding.execution_id in binding.terminal_execution_ids,
                )
                self._rotate_execution(binding, ros_invocation_id=props.ros_invocation_id)

            binding.active_run_id = run_input.run_id
            binding.run_digests[run_input.run_id] = request_digest
            try:
                self._persist_thread(binding)
            except AguiStateStoreError as exc:
                binding.run_digests.pop(run_input.run_id, None)
                binding.active_run_id = None
                if created:
                    self._threads.pop(binding.thread_id, None)
                    self._executions.pop(binding.execution_id, None)
                elif previous_execution is not None:
                    self._executions.pop(binding.execution_id, None)
                    (
                        old_execution_id,
                        old_task_id,
                        old_invocation_id,
                        old_pipeline_sequence,
                        old_open_steps,
                        old_text_snapshot_digests,
                        was_active,
                        was_terminal,
                    ) = previous_execution
                    binding.execution_id = old_execution_id
                    binding.task_id = old_task_id
                    binding.ros_invocation_id = old_invocation_id
                    binding.pipeline_sequence = old_pipeline_sequence
                    binding.pipeline_open_steps = old_open_steps
                    binding.text_snapshot_digests = old_text_snapshot_digests
                    if was_active:
                        self._executions[old_execution_id] = binding
                    if was_terminal:
                        binding.terminal_execution_ids.add(old_execution_id)
                    else:
                        binding.terminal_execution_ids.discard(old_execution_id)
                raise AdmissionError(
                    "STATE_UNAVAILABLE",
                    "The AG-UI adapter state is unavailable.",
                    status_code=503,
                ) from exc
            self.last_activity = asyncio.get_running_loop().time()
            return RunTicket(
                run_input=run_input,
                binding=binding,
                request_digest=request_digest,
                is_resume=run_input.resume is not None,
                preferred_language=normalize_agui_language(
                    props.preferred_language,
                    fallback=normalize_agui_language(preferred_language),
                ),
            )

    async def start(self) -> None:
        if self._started:
            return
        async with self._lock:
            if self._started:
                return
            self._started = True

    async def stream(self, ticket: RunTicket) -> AsyncIterator[Any]:
        run_input = ticket.run_input
        mapper = A2AEventMapper(
            thread_id=run_input.thread_id,
            run_id=run_input.run_id,
            open_pipeline_steps=ticket.binding.pipeline_open_steps,
            text_snapshot_digests=ticket.binding.text_snapshot_digests,
        )
        yield RunStartedEvent(
            thread_id=run_input.thread_id,
            run_id=run_input.run_id,
            parent_run_id=run_input.parent_run_id,
            input=None,
            timestamp=timestamp_ms(),
        )
        for reopened in mapper.reopen_pipeline_steps():
            yield reopened
        ticket.local_task = asyncio.current_task()
        try:
            props = parse_forwarded_props(run_input.forwarded_props)
            session_emitted = False
            terminal_state = ""
            resolved_tools: list[tuple[str, str]] = []
            sideband_recovery_after: int | None = None
            resume_application: ResumeApplication | None = None
            if ticket.is_resume:
                preparation = await self._prepare_resume(ticket, mapper)
                for event in preparation.events:
                    yield event
                if preparation.needs_interrupt:
                    finished = await self._commit_interrupt(ticket, mapper)
                    for closing in mapper.close_all():
                        yield closing
                    yield finished
                    return
                resume_application = await self._apply_resume(ticket, props.iac_code)
                stream = resume_application.stream
                resolved_tools = resume_application.resolved_tools
                sideband_recovery_after = resume_application.sideband_recovery_after
                if resume_application.acceptance.accepted:
                    yield mapper.session_event(
                        execution_id=ticket.binding.execution_id,
                        context_id=ticket.binding.context_id,
                        task_id=ticket.binding.task_id,
                        ros_invocation_id=ticket.binding.ros_invocation_id,
                        session_id=ticket.binding.iac_code_session_id,
                    )
                    session_emitted = True
            else:
                stream = self._new_a2a_stream(ticket, props.iac_code)
            try:
                async for event in stream:
                    binding = ticket.binding
                    task_id = a2a_task_id(event)
                    if task_id and task_id != binding.task_id:
                        await self._remember_task(ticket, task_id)
                    context_id = a2a_context_id(event)
                    if context_id and context_id != binding.context_id:
                        raise AguiError("A2A_PROTOCOL_ERROR", "The A2A context identity changed unexpectedly.")
                    session_id = a2a_iac_code_session_id(event)
                    if session_id:
                        await self._remember_session_id(ticket, session_id)
                    resume_accepted = resume_application is not None and resume_application.acceptance.accepted
                    if not session_emitted and (
                        resume_accepted
                        or resume_application is None
                        and binding.task_id
                        and binding.iac_code_session_id
                    ):
                        yield mapper.session_event(
                            execution_id=binding.execution_id,
                            context_id=binding.context_id,
                            task_id=binding.task_id,
                            ros_invocation_id=binding.ros_invocation_id,
                            session_id=binding.iac_code_session_id,
                        )
                        session_emitted = True
                    if resolved_tools:
                        for tool_call_id, content in resolved_tools:
                            for resolved in mapper.map_resolved_tool(
                                tool_call_id=tool_call_id,
                                content=content,
                            ):
                                yield resolved
                        resolved_tools.clear()
                    mapped_events = mapper.map(event)
                    binding.pipeline_sequence = max(binding.pipeline_sequence, mapper.last_pipeline_sequence)
                    binding.pipeline_open_steps = set(mapper.open_pipeline_steps)
                    binding.text_snapshot_digests = set(mapper.text_snapshot_digests)
                    for mapped in mapped_events:
                        yield mapped
                    pending_values = [
                        value
                        for value in a2a_inputs(event)
                        if (binding.execution_id, str(value.get("inputId") or ""))
                        not in binding.applied_resume_digests
                    ]
                    if pending_values:
                        if sideband_recovery_after is not None:
                            for recovered in await self._recover_pipeline(
                                binding,
                                mapper,
                                after_sequence=sideband_recovery_after,
                            ):
                                yield recovered
                            binding.pipeline_sequence = max(
                                binding.pipeline_sequence,
                                mapper.last_pipeline_sequence,
                            )
                            binding.pipeline_open_steps = set(mapper.open_pipeline_steps)
                            sideband_recovery_after = None
                        self._merge_pending(
                            binding,
                            pending_values,
                            replace=False,
                            sideband_ids=a2a_sideband_input_ids(event),
                        )
                        finished = await self._commit_interrupt(ticket, mapper)
                        for closing in mapper.close_all():
                            yield closing
                        yield finished
                        return
                    state = a2a_state(event)
                    if state:
                        terminal_state = state
                    if state in _FAILED_STATES or state in {"canceled", "completed"}:
                        break
                    if state == "input-required" and not pending_values:
                        break
            finally:
                close_stream = getattr(stream, "aclose", None)
                if close_stream is not None:
                    await close_stream()

            if sideband_recovery_after is not None:
                for recovered in await self._recover_pipeline(
                    ticket.binding,
                    mapper,
                    after_sequence=sideband_recovery_after,
                ):
                    yield recovered
                ticket.binding.pipeline_sequence = max(
                    ticket.binding.pipeline_sequence,
                    mapper.last_pipeline_sequence,
                )
                ticket.binding.pipeline_open_steps = set(mapper.open_pipeline_steps)

            if not session_emitted:
                yield mapper.session_event(
                    execution_id=ticket.binding.execution_id,
                    context_id=ticket.binding.context_id,
                    task_id=ticket.binding.task_id,
                    ros_invocation_id=ticket.binding.ros_invocation_id,
                    session_id=ticket.binding.iac_code_session_id,
                )
            for closing in mapper.close_all():
                yield closing
            if terminal_state in _SUCCESS_STATES:
                ticket.completed = True
                yield RunFinishedEvent(
                    thread_id=run_input.thread_id,
                    run_id=run_input.run_id,
                    outcome=RunFinishedSuccessOutcome(),
                    usage=aggregate_usage(mapper.usage),
                    timestamp=timestamp_ms(),
                )
            elif terminal_state == "canceled":
                ticket.completed = True
                yield _run_error(
                    run_input,
                    "CANCELLED",
                    "The execution was cancelled.",
                    language=ticket.preferred_language,
                    usage=aggregate_usage(mapper.usage),
                )
            else:
                ticket.completed = True
                yield _run_error(
                    run_input,
                    "A2A_EXECUTION_FAILED",
                    "The A2A execution failed.",
                    language=ticket.preferred_language,
                    usage=aggregate_usage(mapper.usage),
                )
        except AguiError as exc:
            ticket.completed = True
            yield _run_error(
                run_input,
                exc.code,
                exc.message,
                language=ticket.preferred_language,
                usage=aggregate_usage(mapper.usage),
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("AG-UI A2A adapter run failed", extra={"run_id": run_input.run_id})
            await self._cancel_unrecoverable(ticket)
            ticket.completed = True
            yield _run_error(
                run_input,
                "A2A_UNAVAILABLE",
                "The local A2A execution service is unavailable.",
                language=ticket.preferred_language,
                usage=aggregate_usage(mapper.usage),
            )
        finally:
            if ticket.completed and not ticket.binding.pending:
                self._mark_execution_terminal(ticket.binding)
                self._persist_thread_best_effort(ticket.binding)
            await self._release_run(ticket)

    def _new_a2a_stream(self, ticket: RunTicket, props: IacCodeForwardedProps) -> Any:
        user_message = latest_user_message(ticket.run_input)
        if user_message is None:
            raise AguiError("INVALID_INPUT", "A new run requires a user message.")
        message_id, parts = user_message
        return self.client.stream_message_parts(
            self.a2a_url,
            parts,
            cwd=ticket.binding.cwd,
            context_id=ticket.binding.context_id,
            message_id=message_id,
            **_a2a_request_options(props, preferred_language=ticket.preferred_language),
        )

    async def _apply_resume(
        self,
        ticket: RunTicket,
        props: IacCodeForwardedProps,
    ) -> ResumeApplication:
        binding = ticket.binding
        if binding.task_id is None or not binding.pending:
            raise AguiError("EXECUTION_LOST", "The A2A task to resume is unavailable.")
        resolutions = self._validate_resume(ticket)
        if binding.expiry_task is not None:
            binding.expiry_task.cancel()
            binding.expiry_task = None

        prompt: str | None = None
        resolved_tools: list[tuple[str, str]] = []
        permission_responses: list[tuple[PendingInput, dict[str, Any], str]] = []
        prompt_responses: list[tuple[PendingInput, str]] = []
        try:
            for pending, entry, normalized_payload in resolutions:
                digest = canonical_digest({"status": str(entry.status), "payload": entry.payload})
                if pending.value.get("kind") == "permission":
                    decision = "deny" if str(entry.status) == "cancelled" else str(normalized_payload["decision"])
                    payload = {
                        "schemaVersion": 1,
                        "kind": "permission",
                        "requestTaskId": binding.task_id,
                        "inputId": pending.interrupt.id,
                        "toolUseId": str(pending.value.get("toolUseId")),
                        "decision": decision,
                    }
                    permission_responses.append((pending, payload, digest))
                    continue
                elif str(entry.status) == "cancelled":
                    await self.client.cancel_task(self.a2a_url, binding.task_id)
                    binding.pending.clear()
                    self._mark_execution_terminal(binding)
                    self._persist_thread(binding)
                    raise AguiError("CANCELLED", "The execution was cancelled by the interrupt response.")
                else:
                    prompt = resume_value(pending.value, normalized_payload)
                    if not prompt:
                        raise AguiError(
                            "RESUME_PAYLOAD_INVALID",
                            "The interrupt response does not contain an answer.",
                        )
                    tool_use_id = pending.value.get("toolUseId")
                    if isinstance(tool_use_id, str) and tool_use_id:
                        resolved_tools.append((tool_use_id, prompt))
                    prompt_responses.append((pending, digest))
        except BaseException:
            if binding.pending:
                self._schedule_expiry(binding)
            raise

        acceptance = ResumeAcceptance()
        if prompt is not None:
            stream = self.client.stream_message(
                self.a2a_url,
                prompt,
                cwd=binding.cwd,
                context_id=binding.context_id,
                task_id=binding.task_id,
                message_id=f"agui-resume-{ticket.run_input.run_id}",
                **_a2a_request_options(props, preferred_language=ticket.preferred_language),
            )
            return ResumeApplication(
                stream=self._stream_prompt_response(binding, stream, prompt_responses, acceptance),
                acceptance=acceptance,
                resolved_tools=resolved_tools,
                sideband_recovery_after=None,
            )
        if permission_responses:
            recovery_after = binding.pipeline_sequence if binding.pipeline_sequence > 0 else None
            if all(pending.sideband for pending, _payload, _digest in permission_responses):
                await self._send_pipeline_permission_responses(
                    binding,
                    props,
                    permission_responses,
                    preferred_language=ticket.preferred_language,
                )
                acceptance.accepted = True
                stream = self._stream_after_sideband(binding)
            else:
                stream = self._stream_permission_responses(ticket, props, permission_responses, acceptance)
            return ResumeApplication(
                stream=stream,
                acceptance=acceptance,
                resolved_tools=[],
                sideband_recovery_after=recovery_after,
            )
        return ResumeApplication(
            stream=self.client.subscribe_task(self.a2a_url, binding.task_id),
            acceptance=acceptance,
            resolved_tools=[],
            sideband_recovery_after=None,
        )

    async def _stream_prompt_response(
        self,
        binding: ThreadBinding,
        stream: Any,
        responses: list[tuple[PendingInput, str]],
        acceptance: ResumeAcceptance,
    ) -> AsyncIterator[dict[str, Any]]:
        accepted = False
        try:
            async for event in stream:
                self._validate_resume_acceptance_event(binding, event)
                if not accepted:
                    await self._commit_accepted_inputs(binding, responses)
                    acceptance.accepted = True
                    accepted = True
                yield event
        except BaseException:
            if binding.pending:
                self._schedule_expiry(binding)
            raise
        finally:
            close_stream = getattr(stream, "aclose", None)
            if close_stream is not None:
                await close_stream()
        if not accepted:
            if binding.pending:
                self._schedule_expiry(binding)
            raise AguiError("A2A_UNAVAILABLE", "The A2A interrupt response was not accepted.")

    async def _commit_accepted_inputs(
        self,
        binding: ThreadBinding,
        responses: list[tuple[PendingInput, str]],
    ) -> None:
        for pending, digest in responses:
            binding.applied_resume_digests[(binding.execution_id, pending.interrupt.id)] = digest
            binding.pending.pop(pending.interrupt.id, None)
        try:
            self._persist_thread(binding)
        except AguiStateStoreError as exc:
            await self._cancel_for_state_failure(binding)
            raise AguiError(
                "STATE_PERSISTENCE_FAILED",
                "The accepted interrupt response could not be committed.",
            ) from exc

    def _validate_resume_acceptance_event(self, binding: ThreadBinding, event: Any) -> None:
        _raise_for_a2a_error(event)
        task_id = a2a_task_id(event)
        if task_id and task_id != binding.task_id:
            raise AguiError("A2A_PROTOCOL_ERROR", "The A2A task identity changed unexpectedly.")
        context_id = a2a_context_id(event)
        if context_id and context_id != binding.context_id:
            raise AguiError("A2A_PROTOCOL_ERROR", "The A2A context identity changed unexpectedly.")

    async def _recover_pipeline(
        self,
        binding: ThreadBinding,
        mapper: A2AEventMapper,
        *,
        after_sequence: int,
    ) -> list[Any]:
        if binding.task_id is None:
            return []
        get_pipeline_state = getattr(self.client, "get_pipeline_state", None)
        if get_pipeline_state is None:
            return []
        pipeline_state = await get_pipeline_state(
            self.a2a_url,
            task_id=binding.task_id,
            after_sequence=after_sequence,
        )
        if not isinstance(pipeline_state, Mapping):
            return []
        return mapper.map_pipeline_recovery(pipeline_state)

    async def _send_pipeline_permission_responses(
        self,
        binding: ThreadBinding,
        props: IacCodeForwardedProps,
        responses: list[tuple[PendingInput, dict[str, Any], str]],
        *,
        preferred_language: str,
    ) -> None:
        assert binding.task_id is not None
        try:
            for pending, payload, digest in responses:
                response = await self.client.send_message_parts(
                    self.a2a_url,
                    [{"data": payload, "mediaType": "application/json"}],
                    cwd=binding.cwd,
                    context_id=binding.context_id,
                    task_id=binding.task_id,
                    message_id=f"agui-resume-{pending.interrupt.id}",
                    **_a2a_request_options(props, preferred_language=preferred_language),
                )
                _raise_for_a2a_error(response)
                await self._commit_accepted_inputs(binding, [(pending, digest)])
        except BaseException:
            if binding.pending:
                self._schedule_expiry(binding)
            raise

    async def _stream_permission_responses(
        self,
        ticket: RunTicket,
        props: IacCodeForwardedProps,
        responses: list[tuple[PendingInput, dict[str, Any], str]],
        acceptance: ResumeAcceptance,
    ) -> AsyncIterator[dict[str, Any]]:
        binding = ticket.binding
        assert binding.task_id is not None
        terminal_seen = False
        try:
            for index, (pending, payload, digest) in enumerate(responses):
                stream = self.client.stream_message_parts(
                    self.a2a_url,
                    [{"data": payload, "mediaType": "application/json"}],
                    cwd=binding.cwd,
                    context_id=binding.context_id,
                    task_id=binding.task_id,
                    message_id=f"agui-resume-{ticket.run_input.run_id}-{pending.interrupt.id}",
                    **_a2a_request_options(props, preferred_language=ticket.preferred_language),
                )
                accepted = False
                try:
                    async for event in stream:
                        self._validate_resume_acceptance_event(binding, event)
                        if not accepted:
                            await self._commit_accepted_inputs(binding, [(pending, digest)])
                            if index == len(responses) - 1:
                                acceptance.accepted = True
                            accepted = True
                        if a2a_state(event) in _FAILED_STATES | _SUCCESS_STATES | {"canceled"}:
                            terminal_seen = True
                        yield event
                finally:
                    close_stream = getattr(stream, "aclose", None)
                    if close_stream is not None:
                        await close_stream()
                if not accepted:
                    raise AguiError("A2A_UNAVAILABLE", "The A2A permission response was not accepted.")
            if not terminal_seen:
                stream = self.client.subscribe_task(self.a2a_url, binding.task_id)
                try:
                    async for event in stream:
                        yield event
                finally:
                    close_stream = getattr(stream, "aclose", None)
                    if close_stream is not None:
                        await close_stream()
        except BaseException:
            if binding.pending:
                self._schedule_expiry(binding)
            raise

    async def _stream_after_sideband(self, binding: ThreadBinding) -> AsyncIterator[dict[str, Any]]:
        """Close the send/subscribe race with one authoritative task snapshot."""

        assert binding.task_id is not None
        task = await self.client.get_task(self.a2a_url, binding.task_id, history_length=100)
        yield task
        if a2a_state(task) in _FAILED_STATES | _SUCCESS_STATES | {"canceled"}:
            return
        stream = self.client.subscribe_task(self.a2a_url, binding.task_id)
        try:
            async for event in stream:
                if isinstance(event, Mapping) and isinstance(event.get("error"), Mapping):
                    refreshed = await self.client.get_task(self.a2a_url, binding.task_id, history_length=100)
                    if a2a_state(refreshed) not in _FAILED_STATES | _SUCCESS_STATES | {"canceled"}:
                        raise RuntimeError("A2A task subscription returned an error before terminal state")
                    yield refreshed
                    return
                yield event
                if a2a_state(event) in _FAILED_STATES | _SUCCESS_STATES | {"canceled"}:
                    return
            refreshed = await self.client.get_task(self.a2a_url, binding.task_id, history_length=100)
            if a2a_state(refreshed) not in _FAILED_STATES | _SUCCESS_STATES | {"canceled"}:
                raise RuntimeError("A2A task subscription ended before terminal state")
            yield refreshed
        except Exception:
            # The task can become terminal between GetTask and SubscribeToTask.
            # Only suppress the subscribe failure when a second authoritative
            # snapshot proves that this exact task completed normally.
            refreshed = await self.client.get_task(self.a2a_url, binding.task_id, history_length=100)
            if a2a_state(refreshed) not in _FAILED_STATES | _SUCCESS_STATES | {"canceled"}:
                raise
            yield refreshed
        finally:
            close_stream = getattr(stream, "aclose", None)
            if close_stream is not None:
                await close_stream()

    async def _prepare_resume(
        self,
        ticket: RunTicket,
        mapper: A2AEventMapper,
    ) -> ResumePreparation:
        binding = ticket.binding
        if binding.task_id is None or not binding.pending:
            entries = ticket.run_input.resume or []
            if entries and all(
                binding.applied_resume_digests.get((binding.execution_id, entry.interrupt_id))
                == canonical_digest({"status": str(entry.status), "payload": entry.payload})
                for entry in entries
            ):
                raise AguiError("RESUME_ALREADY_APPLIED", "The interrupt response has already been applied.")
            raise AguiError("EXECUTION_LOST", "The A2A task to resume is unavailable.")

        ensure_session_restored = getattr(self.client, "ensure_session_restored", None)
        if binding.iac_code_session_id is not None and callable(ensure_session_restored):
            session_ready = await ensure_session_restored(
                self.a2a_url,
                cwd=binding.cwd,
                session_id=binding.iac_code_session_id,
            )
            if not session_ready:
                raise AguiError("EXECUTION_LOST", "The iac-code session to resume is unavailable.")

        task = await self.client.get_task(self.a2a_url, binding.task_id, history_length=100)
        task_id = a2a_task_id(task)
        context_id = a2a_context_id(task)
        if task_id != binding.task_id or (context_id and context_id != binding.context_id):
            raise AguiError("A2A_PROTOCOL_ERROR", "The A2A task identity does not match the interrupted run.")
        session_id = a2a_iac_code_session_id(task)
        if session_id:
            await self._remember_session_id(ticket, session_id)

        # A task snapshot can contain only a bounded suffix of Pipeline events.
        # Mapping that suffix before the authoritative sequence-based recovery can
        # both duplicate old events and advance past an omitted tool start.
        # GetTask returns the last status message as a snapshot.  That text was
        # already delivered before the interrupt; replaying it on every AG-UI
        # resume appends duplicate content to the same message id.
        events = mapper.map(task, include_pipeline=False, include_status_text=False)
        recovery_after = max(binding.pipeline_sequence, mapper.last_pipeline_sequence)
        get_pipeline_state = getattr(self.client, "get_pipeline_state", None)
        if get_pipeline_state is not None:
            pipeline_state = await get_pipeline_state(
                self.a2a_url,
                task_id=binding.task_id,
                after_sequence=recovery_after,
            )
            if isinstance(pipeline_state, Mapping):
                events.extend(mapper.map_pipeline_recovery(pipeline_state))
        binding.pipeline_sequence = max(binding.pipeline_sequence, mapper.last_pipeline_sequence)
        binding.pipeline_open_steps = set(mapper.open_pipeline_steps)

        known_pending_ids = set(binding.pending)
        current_inputs = a2a_inputs(task)
        task_metadata = a2a_iac_code_metadata(task)
        # A task restored after an A2A process restart is only a durable
        # summary and does not contain the original input metadata.  Absence of
        # that projection is not evidence that the adapter's durable interrupt
        # disappeared: the following permission response is what lets A2A load
        # its permission checkpoint.  Explicit projections (including an empty
        # one) remain authoritative, as do terminal task states.
        replace_pending = (
            "input" in task_metadata
            or "pendingPermissions" in task_metadata
            or a2a_state(task) in _FAILED_STATES | {"canceled", "completed"}
        )
        self._merge_pending(
            binding,
            current_inputs,
            replace=replace_pending,
            sideband_ids=a2a_sideband_input_ids(task),
        )
        entries = ticket.run_input.resume or []
        entry_ids = [entry.interrupt_id for entry in entries]
        if len(entry_ids) != len(set(entry_ids)):
            raise AguiError("INCOMPLETE_RESUME", "The resume contains duplicate interrupt ids.")
        entries_by_id = {entry.interrupt_id: entry for entry in entries}
        for interrupt_id in tuple(binding.pending):
            applied = binding.applied_resume_digests.get((binding.execution_id, interrupt_id))
            if applied is None:
                continue
            entry = entries_by_id.get(interrupt_id)
            if entry is not None:
                digest = canonical_digest({"status": str(entry.status), "payload": entry.payload})
                if digest != applied:
                    raise AguiError("RESUME_ALREADY_APPLIED", "The interrupt response has already been applied.")
            # A successful A2A SendMessage is authoritative for adapter
            # idempotency even if GetTask briefly returns a stale permission.
            binding.pending.pop(interrupt_id, None)
        if binding.pending and self._pending_expired(binding):
            await self._expire(binding, binding.execution_id)
            raise AguiError("EXECUTION_EXPIRED", "The interrupted execution has expired.")
        try:
            self._persist_thread(binding)
        except AguiStateStoreError as exc:
            await self._cancel_for_state_failure(binding)
            raise AguiError(
                "STATE_PERSISTENCE_FAILED",
                "The interrupted execution state could not be committed.",
            ) from exc

        supplied_ids = set(entry_ids)
        pending_ids = set(binding.pending)
        for entry in entries:
            if entry.interrupt_id in pending_ids:
                continue
            digest = canonical_digest({"status": str(entry.status), "payload": entry.payload})
            applied = binding.applied_resume_digests.get((binding.execution_id, entry.interrupt_id))
            if applied is not None and applied == digest:
                continue
            if applied is not None:
                raise AguiError("RESUME_ALREADY_APPLIED", "The interrupt response has already been applied.")
            raise AguiError("UNKNOWN_INTERRUPT", "The resume references an unknown interrupt.")
        missing_ids = pending_ids - supplied_ids
        if missing_ids & known_pending_ids:
            raise AguiError("INCOMPLETE_RESUME", "The resume must resolve every pending interrupt exactly once.")
        needs_interrupt = bool(missing_ids)
        if not needs_interrupt:
            # Validate every response before stream() emits the private session
            # event that tells ROS the parked execution accepted this resume.
            # A schema error must leave both the AG-UI pending inputs and the
            # ROS input-required barrier available for a corrected retry.
            self._validate_resume(ticket)
        return ResumePreparation(events=events, needs_interrupt=needs_interrupt)

    async def _remember_task(self, ticket: RunTicket, task_id: str) -> None:
        binding = ticket.binding
        if binding.task_id is not None and binding.task_id != task_id:
            raise AguiError("A2A_PROTOCOL_ERROR", "The A2A task identity changed unexpectedly.")
        binding.task_id = task_id
        try:
            self._persist_thread(binding)
        except AguiStateStoreError as exc:
            await self._cancel_for_state_failure(binding)
            raise AguiError(
                "STATE_PERSISTENCE_FAILED",
                "The execution mapping could not be committed.",
            ) from exc

    async def _remember_session_id(self, ticket: RunTicket, session_id: str) -> None:
        binding = ticket.binding
        if binding.iac_code_session_id is not None:
            if binding.iac_code_session_id != session_id:
                raise AguiError("A2A_PROTOCOL_ERROR", "The iac-code session identity changed unexpectedly.")
            return
        binding.iac_code_session_id = session_id
        try:
            self._persist_thread(binding)
        except AguiStateStoreError as exc:
            await self._cancel_for_state_failure(binding)
            raise AguiError(
                "STATE_PERSISTENCE_FAILED",
                "The execution session mapping could not be committed.",
            ) from exc

    async def _commit_interrupt(self, ticket: RunTicket, mapper: A2AEventMapper) -> RunFinishedEvent:
        binding = ticket.binding
        mapper.finalize_text_snapshots()
        binding.pipeline_sequence = max(binding.pipeline_sequence, mapper.last_pipeline_sequence)
        binding.text_snapshot_digests = set(mapper.text_snapshot_digests)
        try:
            self._persist_thread(binding)
        except AguiStateStoreError as exc:
            await self._cancel_for_state_failure(binding)
            raise AguiError(
                "STATE_PERSISTENCE_FAILED",
                "The interrupted execution state could not be committed.",
            ) from exc
        self._schedule_expiry(binding)
        # This durable/paused marker must precede yielding any terminal event.  The
        # ASGI response may be closed immediately after the client reads it.
        ticket.paused = True
        ticket.completed = True
        return RunFinishedEvent(
            thread_id=ticket.run_input.thread_id,
            run_id=ticket.run_input.run_id,
            outcome=RunFinishedInterruptOutcome(interrupts=[item.interrupt for item in binding.pending.values()]),
            usage=aggregate_usage(mapper.usage),
            timestamp=timestamp_ms(),
        )

    def _merge_pending(
        self,
        binding: ThreadBinding,
        values: list[dict[str, Any]],
        *,
        replace: bool,
        sideband_ids: set[str] | None = None,
    ) -> None:
        sideband_ids = sideband_ids or set()
        previous = binding.pending
        merged: dict[str, PendingInput] = {} if replace else dict(previous)
        for value in values:
            request_task_id = value.get("requestTaskId")
            context_id = value.get("contextId")
            if request_task_id and request_task_id != binding.task_id:
                raise AguiError("A2A_PROTOCOL_ERROR", "An interrupt belongs to another A2A task.")
            if context_id and context_id != binding.context_id:
                raise AguiError("A2A_PROTOCOL_ERROR", "An interrupt belongs to another A2A context.")
            input_id = str(value.get("inputId") or "")
            existing = previous.get(input_id)
            if existing is not None:
                if input_id in sideband_ids:
                    existing.sideband = True
                merged[input_id] = existing
                continue
            pending = PendingInput(
                value=_persistent_input(value),
                interrupt=interrupt_from_a2a(_persistent_input(value), ttl_seconds=self.interrupt_ttl),
                sideband=input_id in sideband_ids,
            )
            merged[pending.interrupt.id] = pending
        binding.pending = merged

    def _validate_resume(self, ticket: RunTicket) -> list[tuple[PendingInput, Any, Any]]:
        entries = ticket.run_input.resume or []
        entry_ids = [entry.interrupt_id for entry in entries]
        if len(entry_ids) != len(set(entry_ids)):
            raise AguiError("INCOMPLETE_RESUME", "The resume contains duplicate interrupt ids.")
        binding = ticket.binding
        supplied_ids = set(entry_ids)
        pending_ids = set(binding.pending)
        if pending_ids - supplied_ids:
            raise AguiError("INCOMPLETE_RESUME", "The resume must resolve every pending interrupt exactly once.")
        output: list[tuple[PendingInput, Any, Any]] = []
        for entry in entries:
            if entry.interrupt_id not in binding.pending:
                continue
            pending = binding.pending[entry.interrupt_id]
            payload = entry.payload
            if str(entry.status) == "cancelled" and pending.value.get("kind") == "permission":
                payload = {"decision": "deny"}
            elif str(entry.status) != "cancelled" and payload is None:
                raise AguiError("RESUME_PAYLOAD_INVALID", "A resolved interrupt requires a payload.")
            if str(entry.status) != "cancelled" or pending.value.get("kind") == "permission":
                try:
                    validate_json_schema(instance=payload, schema=pending.interrupt.response_schema)
                except JsonSchemaValidationError as exc:
                    raise AguiError("RESUME_PAYLOAD_INVALID", "The interrupt response payload is invalid.") from exc
            output.append((pending, entry, payload))
        if not output:
            raise AguiError("RESUME_ALREADY_APPLIED", "The interrupt response has already been applied.")
        return output

    async def disconnect(self, ticket: RunTicket) -> None:
        if ticket.completed or ticket.paused or ticket.binding.pending:
            return
        await self._cancel_unrecoverable(ticket)
        ticket.completed = True
        await self._release_run(ticket)

    async def cancel(self, execution_id: str, *, thread_id: str, ros_invocation_id: str) -> str:
        await self.start()
        async with self._lock:
            binding = self._load_thread(thread_id)
        if binding is None:
            return "not_found"
        binding = self._executions.get(execution_id)
        if binding is None:
            thread = self._threads.get(thread_id)
            return "already_terminal" if thread and execution_id in thread.terminal_execution_ids else "not_found"
        if binding.thread_id != thread_id or binding.ros_invocation_id != ros_invocation_id:
            return "not_found"
        if binding.task_id:
            await self.client.cancel_task(self.a2a_url, binding.task_id)
        binding.pending.clear()
        if binding.expiry_task is not None:
            binding.expiry_task.cancel()
            binding.expiry_task = None
        self._mark_execution_terminal(binding)
        self._persist_thread(binding)
        return "cancelled"

    async def aclose(self) -> None:
        for binding in self._threads.values():
            if binding.expiry_task is not None:
                binding.expiry_task.cancel()
                binding.expiry_task = None
        close = getattr(self.client, "aclose", None)
        if close is not None:
            await close()

    async def _release_run(self, ticket: RunTicket) -> None:
        async with self._lock:
            if ticket.binding.active_run_id == ticket.run_input.run_id:
                ticket.binding.active_run_id = None
            ticket.local_task = None
            self.last_activity = asyncio.get_running_loop().time()

    def _rotate_execution(self, binding: ThreadBinding, *, ros_invocation_id: str) -> None:
        if binding.task_id is not None or binding.execution_id in self._executions:
            self._executions.pop(binding.execution_id, None)
            binding.terminal_execution_ids.add(binding.execution_id)
        binding.execution_id = str(uuid.uuid4())
        binding.task_id = None
        binding.ros_invocation_id = ros_invocation_id
        binding.pipeline_sequence = 0
        binding.pipeline_open_steps.clear()
        binding.text_snapshot_digests.clear()
        self._executions[binding.execution_id] = binding

    def _mark_execution_terminal(self, binding: ThreadBinding) -> None:
        if self._executions.get(binding.execution_id) is binding:
            self._executions.pop(binding.execution_id, None)
        binding.terminal_execution_ids.add(binding.execution_id)
        binding.task_id = None

    def _schedule_expiry(self, binding: ThreadBinding) -> None:
        if not binding.pending:
            return
        if binding.expiry_task is not None:
            binding.expiry_task.cancel()
        binding.expiry_task = asyncio.create_task(
            self._expire(binding, binding.execution_id),
            name=f"agui-a2a-expiry-{binding.execution_id}",
        )

    async def _expire(self, binding: ThreadBinding, execution_id: str) -> None:
        try:
            expires_at = _pending_expires_at(binding)
            if expires_at is None:
                return
            delay = max(0.0, (expires_at - datetime.now(timezone.utc)).total_seconds())
            await asyncio.sleep(delay)
            if binding.execution_id != execution_id or not binding.pending:
                return
            if binding.task_id:
                with contextlib.suppress(Exception):
                    await self.client.cancel_task(self.a2a_url, binding.task_id)
            binding.pending.clear()
            self._mark_execution_terminal(binding)
            self._persist_thread_best_effort(binding)
        except asyncio.CancelledError:
            return
        finally:
            if binding.expiry_task is asyncio.current_task():
                binding.expiry_task = None

    async def _cancel_unrecoverable(self, ticket: RunTicket) -> None:
        binding = ticket.binding
        if binding.pending:
            return
        if binding.task_id:
            with contextlib.suppress(Exception):
                await self.client.cancel_task(self.a2a_url, binding.task_id)
        self._mark_execution_terminal(binding)
        self._persist_thread_best_effort(binding)

    async def _cancel_for_state_failure(self, binding: ThreadBinding) -> None:
        if binding.task_id:
            with contextlib.suppress(Exception):
                await self.client.cancel_task(self.a2a_url, binding.task_id)
        binding.pending.clear()
        if binding.expiry_task is not None:
            binding.expiry_task.cancel()
            binding.expiry_task = None
        self._mark_execution_terminal(binding)
        self._persist_thread_best_effort(binding)

    def _pending_expired(self, binding: ThreadBinding) -> bool:
        expires_at = _pending_expires_at(binding)
        return expires_at is not None and expires_at <= datetime.now(timezone.utc)

    def _persist_thread(self, binding: ThreadBinding) -> None:
        self._state_store.save_thread(binding.thread_id, self._thread_state_document(binding))

    def _persist_thread_best_effort(self, binding: ThreadBinding) -> None:
        try:
            self._persist_thread(binding)
        except AguiStateStoreError:
            logger.warning("Failed to persist AG-UI thread state", exc_info=True)

    def _thread_state_document(self, binding: ThreadBinding) -> dict[str, Any]:
        pending = {
            input_id: {
                "value": _persistent_input(item.value),
                "expiresAt": item.interrupt.expires_at,
                "sideband": item.sideband,
            }
            for input_id, item in binding.pending.items()
        }
        return {
            "schemaVersion": AGUI_STATE_SCHEMA_VERSION,
            "threadId": binding.thread_id,
            "contextId": binding.context_id,
            "cwd": binding.cwd,
            "userId": binding.user_id,
            "iacCodeSessionId": binding.iac_code_session_id,
            "execution": {
                "executionId": binding.execution_id,
                "rosInvocationId": binding.ros_invocation_id,
                "taskId": binding.task_id,
                "pipelineSequence": binding.pipeline_sequence,
                "pipelineOpenSteps": sorted(binding.pipeline_open_steps),
                "textSnapshotDigests": sorted(binding.text_snapshot_digests),
                "pending": pending,
            },
            "runDigests": dict(binding.run_digests),
            "appliedResumeDigests": [
                {"executionId": execution_id, "interruptId": interrupt_id, "digest": digest}
                for (execution_id, interrupt_id), digest in sorted(binding.applied_resume_digests.items())
            ],
            "terminalExecutionIds": sorted(binding.terminal_execution_ids),
        }

    def _load_thread(self, thread_id: str) -> ThreadBinding | None:
        binding = self._threads.get(thread_id)
        if binding is not None:
            return binding
        document = self._state_store.load_thread(thread_id)
        if document is None:
            return None
        binding = self._restore_thread_state(document, expected_thread_id=thread_id)
        self._threads[thread_id] = binding
        if binding.execution_id not in binding.terminal_execution_ids and binding.task_id:
            self._executions[binding.execution_id] = binding
        if binding.pending:
            self._schedule_expiry(binding)
        return binding

    def _restore_thread_state(
        self,
        document: Mapping[str, Any],
        *,
        expected_thread_id: str,
    ) -> ThreadBinding:
        try:
            thread_id = _required_state_string(document, "threadId")
            if thread_id != expected_thread_id:
                raise ValueError("thread state key mismatch")
            raw_execution = document.get("execution")
            run_digests = document.get("runDigests")
            applied_digests = document.get("appliedResumeDigests")
            terminal_ids = document.get("terminalExecutionIds")
            if not isinstance(raw_execution, Mapping):
                raise ValueError("execution state is invalid")
            if not isinstance(run_digests, Mapping) or not isinstance(applied_digests, list):
                raise ValueError("digest state is invalid")
            if not isinstance(terminal_ids, list):
                raise ValueError("terminal execution state is invalid")
            restored_terminal = {value for value in terminal_ids if isinstance(value, str) and value}
            user_id = document.get("userId")
            task_id = raw_execution.get("taskId")
            iac_code_session_id = document.get("iacCodeSessionId")
            if user_id is not None and not isinstance(user_id, str):
                raise ValueError("userId is invalid")
            if task_id is not None and not isinstance(task_id, str):
                raise ValueError("taskId is invalid")
            if iac_code_session_id is not None and not isinstance(iac_code_session_id, str):
                raise ValueError("iacCodeSessionId is invalid")
            sequence = raw_execution.get("pipelineSequence", 0)
            if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence < 0:
                raise ValueError("pipelineSequence is invalid")
            raw_open_steps = raw_execution.get("pipelineOpenSteps", [])
            if not isinstance(raw_open_steps, list) or not all(
                isinstance(value, str) and value for value in raw_open_steps
            ):
                raise ValueError("pipelineOpenSteps is invalid")
            raw_text_digests = raw_execution.get("textSnapshotDigests", [])
            if not isinstance(raw_text_digests, list) or not all(
                isinstance(value, str)
                and len(value) == 64
                and all(character in "0123456789abcdef" for character in value)
                for value in raw_text_digests
            ):
                raise ValueError("textSnapshotDigests is invalid")
            pending_value = raw_execution.get("pending", {})
            if not isinstance(pending_value, Mapping):
                raise ValueError("pending state is invalid")
            pending: dict[str, PendingInput] = {}
            for input_id, raw_pending in pending_value.items():
                if not isinstance(input_id, str) or not isinstance(raw_pending, Mapping):
                    raise ValueError("pending entry is invalid")
                value = raw_pending.get("value")
                expires_at = raw_pending.get("expiresAt")
                sideband = raw_pending.get("sideband", False)
                if (
                    not isinstance(value, Mapping)
                    or not isinstance(expires_at, str)
                    or not isinstance(sideband, bool)
                ):
                    raise ValueError("pending entry is invalid")
                safe_value = _persistent_input(value)
                if safe_value.get("inputId") != input_id:
                    raise ValueError("pending input identity mismatch")
                _parse_expires_at(expires_at)
                interrupt = interrupt_from_a2a(safe_value, ttl_seconds=1).model_copy(
                    update={"expires_at": expires_at}
                )
                pending[input_id] = PendingInput(value=safe_value, interrupt=interrupt, sideband=sideband)
            restored_run_digests = {
                key: value
                for key, value in run_digests.items()
                if isinstance(key, str) and key and isinstance(value, str) and value
            }
            restored_applied: dict[tuple[str, str], str] = {}
            for item in applied_digests:
                if not isinstance(item, Mapping):
                    raise ValueError("applied resume digest is invalid")
                execution_id = _required_state_string(item, "executionId")
                interrupt_id = _required_state_string(item, "interruptId")
                digest = _required_state_string(item, "digest")
                restored_applied[(execution_id, interrupt_id)] = digest
            return ThreadBinding(
                thread_id=thread_id,
                context_id=_required_state_string(document, "contextId"),
                cwd=_required_state_string(document, "cwd"),
                user_id=user_id,
                ros_invocation_id=_required_state_string(raw_execution, "rosInvocationId"),
                iac_code_session_id=iac_code_session_id,
                execution_id=_required_state_string(raw_execution, "executionId"),
                task_id=task_id,
                pending=pending,
                pipeline_sequence=sequence,
                pipeline_open_steps={value for value in raw_open_steps if isinstance(value, str)},
                text_snapshot_digests={value for value in raw_text_digests if isinstance(value, str)},
                run_digests=restored_run_digests,
                applied_resume_digests=restored_applied,
                terminal_execution_ids=restored_terminal,
            )
        except Exception as exc:
            raise AguiStateStoreError("Invalid AG-UI thread state contents.") from exc


_PERSISTED_INPUT_FIELDS = frozenset(
    {
        "schemaVersion",
        "kind",
        "requestTaskId",
        "contextId",
        "inputId",
        "toolUseId",
        "toolName",
        "title",
        "purpose",
        "effect",
        "target",
        "isReadOnly",
        "prompt",
        "safeSummary",
        "options",
        "language",
        "deploymentSummary",
        "scope",
        "subPipelineId",
        "operation",
        "displayParameters",
        "allowFreeText",
        "freeTextPrompt",
        "required",
    }
)


def _persistent_input(value: Mapping[str, Any]) -> dict[str, Any]:
    """Keep only the public A2A input projection, never request/tool secrets."""

    return {key: copy.deepcopy(item) for key, item in value.items() if key in _PERSISTED_INPUT_FIELDS}


def _required_state_string(value: Mapping[str, Any], key: str) -> str:
    result = value.get(key)
    if not isinstance(result, str) or not result:
        raise ValueError(f"{key} is required")
    return result


def _parse_expires_at(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _pending_expires_at(binding: ThreadBinding) -> datetime | None:
    values = [
        _parse_expires_at(item.interrupt.expires_at)
        for item in binding.pending.values()
        if isinstance(item.interrupt.expires_at, str) and item.interrupt.expires_at
    ]
    return min(values) if values else None


def _raise_for_a2a_error(response: Any) -> None:
    payload = getattr(response, "payload", response)
    if isinstance(payload, Mapping) and isinstance(payload.get("error"), Mapping):
        raise AguiError("A2A_UNAVAILABLE", "The local A2A execution service rejected the interrupt response.")


def _a2a_request_options(
    props: IacCodeForwardedProps,
    *,
    preferred_language: str | None = None,
) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        "user_id": props.user_id,
        "channel": props.channel,
        "preferredLanguage": preferred_language or props.preferred_language,
        "candidatePresentation": props.candidate_presentation,
        "run_mode": props.run_mode,
        "pipeline_name": props.pipeline_name,
        "cleanupOnly": props.cleanup_only,
        "rosInvocationId": props.ros_invocation_id,
    }
    cloud = props.alibaba_cloud
    if cloud is not None:
        metadata.update(
            {
                "alibaba_cloud_access_key_id": cloud.access_key_id,
                "alibaba_cloud_access_key_secret": cloud.access_key_secret,
                "alibaba_cloud_security_token": cloud.security_token,
                "alibaba_cloud_region_id": cloud.region_id,
            }
        )
    metadata = {key: value for key, value in metadata.items() if value is not None}
    thinking = props.thinking
    return {
        "model": props.model,
        "iac_code_api_key": props.llm_api_key,
        "thinking_enabled": thinking.enabled if thinking is not None else None,
        "thinking_effort": thinking.effort if thinking is not None else None,
        "thinking_budget": thinking.budget if thinking is not None else None,
        "iac_code_metadata": metadata,
    }


def _context_id(thread_id: str) -> str:
    digest = hashlib.sha256(thread_id.encode("utf-8")).hexdigest()
    return f"agui-{digest[:40]}"


def _run_error(
    run_input: RunAgentInput,
    code: str,
    message: str,
    *,
    language: str,
    usage: list[TokenUsage] | None = None,
) -> RunErrorEvent:
    return RunErrorEvent.model_validate(
        {
            "threadId": run_input.thread_id,
            "runId": run_input.run_id,
            "code": code,
            "message": translate_agui_error(message, language=language),
            "usage": usage,
            "timestamp": timestamp_ms(),
        }
    )
