from __future__ import annotations

import asyncio
import logging
import os
import uuid
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import Any, TypeAlias

import httpx
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.types import Message, Role, Task, TaskState, TaskStatus, TaskStatusUpdateEvent
from google.protobuf.json_format import MessageToDict

from iac_code.a2a.events import make_text_part, publish_stream_event
from iac_code.a2a.metrics import A2AMetrics, NoOpA2AMetrics
from iac_code.a2a.parts import allowed_cwd_roots, is_relative_to, parts_to_prompt
from iac_code.a2a.task_store import A2ATaskStore
from iac_code.a2a.types import (
    TASK_STATE_CANCELED,
    TASK_STATE_FAILED,
    TASK_STATE_INPUT_REQUIRED,
    TASK_STATE_WORKING,
)
from iac_code.services.agent_factory import AgentFactoryOptions, create_agent_runtime
from iac_code.services.session_storage import SessionStorage
from iac_code.services.telemetry import use_session_id

logger = logging.getLogger(__name__)
_CONTEXT_LOCK_ACQUIRE_TIMEOUT_SECONDS = 1
_ERROR_TEXT_MAX_CHARS = 1000


def _format_exception(exc: BaseException) -> str:
    message = str(exc)
    if not message:
        return type(exc).__name__
    return f"{type(exc).__name__}: {message[:_ERROR_TEXT_MAX_CHARS]}"


A2APermissionResolver: TypeAlias = Callable[[Any], "bool | Awaitable[bool]"]


def _allowed_cwd_roots() -> list[Path]:
    return allowed_cwd_roots()


def _is_relative_to(path: Path, root: Path) -> bool:
    return is_relative_to(path, root)


class IacCodeA2AExecutor(AgentExecutor):
    def __init__(
        self,
        *,
        task_store: A2ATaskStore,
        model: str,
        metrics: A2AMetrics | None = None,
        artifact_store: Any | None = None,
        push_notifier: Any | None = None,
        permission_resolver: A2APermissionResolver | None = None,
        auto_approve_permissions: bool = False,
    ) -> None:
        self._task_store = task_store
        self._model = model
        self._metrics = metrics or NoOpA2AMetrics()
        self._artifact_store = artifact_store
        self._push_notifier = push_notifier
        self._permission_resolver = permission_resolver
        self._auto_approve_permissions = auto_approve_permissions

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        task_id = context.task_id or "task-" + uuid.uuid4().hex[:12]
        context_id = context.context_id or "ctx-" + uuid.uuid4().hex[:12]
        task = None
        try:
            task = await self._task_store.get_or_create_task(task_id=task_id, context_id=context_id)
            if not isinstance(getattr(context, "current_task", None), Task):
                await self._publish_initial_task(event_queue, task_id=task_id, context_id=context_id, context=context)
            await self._task_store.ensure_task_not_expired(task.task_id)
            metadata = getattr(context, "metadata", None) or getattr(
                getattr(context, "message", None), "metadata", None
            )
            cwd = self._resolve_cwd(metadata)
            prompt = self._prompt_from_context(context, cwd=cwd)
        except Exception as exc:
            if _is_retryable_executor_error(exc):
                await self._publish_status(
                    event_queue,
                    task_id=task_id,
                    context_id=context_id,
                    state=TaskState.TASK_STATE_INPUT_REQUIRED,
                    text="A temporary error occurred. Please retry.",
                )
                if task is not None:
                    task.state = TASK_STATE_INPUT_REQUIRED
                    self._task_store.mirror_task(task)
                    await self._notify_terminal_task(task_id=task.task_id, context_id=task.context_id, state=task.state)
                self._metrics.record_executor_error()
                return
            await self._publish_status(
                event_queue,
                task_id=task_id,
                context_id=context_id,
                state=TaskState.TASK_STATE_FAILED,
                text=str(exc),
            )
            if task is not None:
                task.state = TASK_STATE_FAILED
                self._task_store.mirror_task(task)
                await self._notify_terminal_task(task_id=task.task_id, context_id=task.context_id, state=task.state)
            self._metrics.record_task_failed()
            return

        if not prompt.strip():
            task.state = TASK_STATE_FAILED
            await self._publish_status(
                event_queue,
                task_id=task_id,
                context_id=context_id,
                state=TaskState.TASK_STATE_FAILED,
                text="A2A server currently accepts text input only.",
            )
            self._task_store.mirror_task(task)
            await self._notify_terminal_task(task_id=task.task_id, context_id=task.context_id, state=task.state)
            self._metrics.record_task_failed()
            return

        def runtime_factory(session_id: str) -> Any:
            session_storage = SessionStorage()
            resume_messages = None
            if session_storage.exists(cwd, session_id):
                loaded = session_storage.load(cwd, session_id)
                resume_messages = SessionStorage.repair_interrupted(loaded) if loaded else None
            return create_agent_runtime(
                AgentFactoryOptions(
                    model=self._model,
                    session_id=session_id,
                    cwd=cwd,
                    resume_messages=resume_messages,
                )
            )

        try:
            ctx = await self._task_store.get_or_create_context(
                context_id=context_id,
                cwd=cwd,
                runtime_factory=runtime_factory,
            )
        except Exception as exc:
            await self._publish_status(
                event_queue,
                task_id=task_id,
                context_id=context_id,
                state=TaskState.TASK_STATE_FAILED,
                text=self._sanitize_error(exc),
            )
            task.state = TASK_STATE_FAILED
            self._task_store.mirror_task(task)
            await self._notify_terminal_task(task_id=task.task_id, context_id=task.context_id, state=task.state)
            self._metrics.record_executor_error()
            self._metrics.record_task_failed()
            return

        if ctx.lock is None:
            ctx.lock = asyncio.Lock()
        if ctx.active_task_id is not None:
            task.state = TASK_STATE_FAILED
            await self._publish_status(
                event_queue,
                task_id=task_id,
                context_id=context_id,
                state=TaskState.TASK_STATE_FAILED,
                text="Task is already working.",
            )
            self._task_store.mirror_task(task)
            await self._notify_terminal_task(task_id=task.task_id, context_id=task.context_id, state=task.state)
            self._metrics.record_task_failed()
            return

        lock = ctx.lock
        try:
            await asyncio.wait_for(lock.acquire(), timeout=_CONTEXT_LOCK_ACQUIRE_TIMEOUT_SECONDS)
        except TimeoutError:
            task.state = TASK_STATE_FAILED
            await self._publish_status(
                event_queue,
                task_id=task_id,
                context_id=context_id,
                state=TaskState.TASK_STATE_FAILED,
                text="Task is already working.",
            )
            self._task_store.mirror_task(task)
            await self._notify_terminal_task(task_id=task.task_id, context_id=task.context_id, state=task.state)
            self._metrics.record_task_failed()
            return

        try:
            ctx.active_task_id = task.task_id
            task.state = TASK_STATE_WORKING
            task.active_task = asyncio.current_task()
            self._task_store.mirror_task(task)
            self._task_store.mirror_context(ctx)
            try:
                runtime = ctx.runtime
                if runtime is None:
                    raise RuntimeError("A2A context runtime missing")
                await self._publish_status(
                    event_queue,
                    task_id=task_id,
                    context_id=context_id,
                    state=TaskState.TASK_STATE_SUBMITTED,
                )
                await self._publish_status(
                    event_queue,
                    task_id=task_id,
                    context_id=context_id,
                    state=TaskState.TASK_STATE_WORKING,
                )
                with use_session_id(ctx.session_id):
                    async for event in runtime.agent_loop.run_streaming(prompt):
                        text_chunk = await publish_stream_event(
                            event_queue,
                            task_id=task_id,
                            context_id=context_id,
                            event=event,
                            artifact_store=self._artifact_store,
                            permission_resolver=self._permission_resolver,
                            auto_approve_permissions=self._auto_approve_permissions,
                        )
                        if text_chunk:
                            task.output_text.append(text_chunk)
                task.state = TASK_STATE_INPUT_REQUIRED
                await self._publish_status(
                    event_queue,
                    task_id=task_id,
                    context_id=context_id,
                    state=TaskState.TASK_STATE_INPUT_REQUIRED,
                )
                self._task_store.mirror_task(task)
                await self._notify_terminal_task(task_id=task.task_id, context_id=task.context_id, state=task.state)
                self._metrics.record_turn_completed()
            except asyncio.CancelledError:
                task.state = TASK_STATE_CANCELED
                await self._publish_status(
                    event_queue,
                    task_id=task_id,
                    context_id=context_id,
                    state=TaskState.TASK_STATE_CANCELED,
                    text="Task canceled.",
                )
                self._task_store.mirror_task(task)
                await self._notify_terminal_task(task_id=task.task_id, context_id=task.context_id, state=task.state)
                self._metrics.record_task_canceled()
            except Exception as exc:
                if _is_retryable_executor_error(exc):
                    task.state = TASK_STATE_INPUT_REQUIRED
                    await self._publish_status(
                        event_queue,
                        task_id=task_id,
                        context_id=context_id,
                        state=TaskState.TASK_STATE_INPUT_REQUIRED,
                        text="A temporary error occurred. Please retry.",
                    )
                    self._task_store.mirror_task(task)
                    await self._notify_terminal_task(task_id=task.task_id, context_id=task.context_id, state=task.state)
                    self._metrics.record_executor_error()
                else:
                    task.state = TASK_STATE_FAILED
                    await self._publish_status(
                        event_queue,
                        task_id=task_id,
                        context_id=context_id,
                        state=TaskState.TASK_STATE_FAILED,
                        text=self._sanitize_error(exc),
                    )
                    self._task_store.mirror_task(task)
                    await self._notify_terminal_task(task_id=task.task_id, context_id=task.context_id, state=task.state)
                    self._metrics.record_executor_error()
                    self._metrics.record_task_failed()
            finally:
                task.active_task = None
                ctx.active_task_id = None
                ctx.touch()
                task.touch()
                self._task_store.mirror_context(ctx)
                # Force-flush telemetry between tasks. The a2a server may run in
                # an ephemeral sandbox that's destroyed immediately after the
                # response is delivered, before the natural batch interval or
                # process-exit graceful_shutdown can run. Synchronous flush is
                # offloaded to a worker thread so the event loop is not blocked.
                from iac_code.services.telemetry import flush_telemetry

                try:
                    await asyncio.to_thread(flush_telemetry)
                except Exception:
                    logger.debug("flush_telemetry after task failed", exc_info=True)
        finally:
            lock.release()

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        task_id = context.task_id
        context_id = context.context_id or "unknown"
        if task_id and await self._task_store.cancel_task(task_id):
            await self._publish_status(
                event_queue,
                task_id=task_id,
                context_id=context_id,
                state=TaskState.TASK_STATE_CANCELED,
                text="Task cancellation requested.",
            )
            self._metrics.record_task_canceled()
            return
        if task_id:
            await self._publish_status(
                event_queue,
                task_id=task_id,
                context_id=context_id,
                state=TaskState.TASK_STATE_FAILED,
                text="Task not running.",
            )

    def _resolve_cwd(self, metadata: Any | None) -> str:
        cwd = os.getcwd()
        if metadata is not None and hasattr(metadata, "DESCRIPTOR"):
            metadata = MessageToDict(metadata, preserving_proto_field_name=False)
        if metadata:
            raw_iac_meta = metadata.get("iac_code") if isinstance(metadata, Mapping) else None
            if isinstance(raw_iac_meta, Mapping):
                raw_cwd = raw_iac_meta.get("cwd")
                if isinstance(raw_cwd, str):
                    cwd = raw_cwd
        if not isinstance(cwd, str) or not Path(cwd).is_absolute():
            raise ValueError("Invalid A2A workspace metadata.")
        resolved_cwd = Path(cwd).resolve()
        if not any(_is_relative_to(resolved_cwd, root) for root in _allowed_cwd_roots()):
            raise ValueError("Invalid A2A workspace metadata.")
        if resolved_cwd.exists():
            if not resolved_cwd.is_dir():
                raise ValueError("Invalid A2A workspace metadata.")
        else:
            resolved_cwd.mkdir(parents=True, exist_ok=True)
        return str(resolved_cwd)

    def _prompt_from_context(self, context: RequestContext, *, cwd: str) -> str:
        message = getattr(context, "message", None)
        if not isinstance(message, Message):
            return context.get_user_input()
        return parts_to_prompt(message.parts, cwd=cwd)

    def _sanitize_error(self, exc: Exception) -> str:
        if isinstance(exc, ValueError):
            msg = str(exc).lower()
            if "provider" in msg or "configure" in msg or "/auth" in msg:
                return "Authentication required. Please configure your API credentials."
        if type(exc).__name__ == "AuthenticationError":
            return "Authentication required. Please configure your API credentials."
        status = getattr(exc, "status_code", None) or getattr(exc, "status", None)
        if status == 401:
            return "Authentication required. Please configure your API credentials."
        logger.exception("Unhandled A2A executor error")
        return _format_exception(exc)

    async def _publish_status(
        self,
        event_queue: EventQueue,
        *,
        task_id: str,
        context_id: str,
        state: int,
        text: str | None = None,
    ) -> None:
        message = None
        if text:
            message = Message(
                message_id=f"{task_id}-{state}",
                task_id=task_id,
                context_id=context_id,
                role=Role.ROLE_AGENT,
                parts=[make_text_part(text)],
            )
        status = TaskStatus(state=TaskState.Name(state), message=message)
        status.timestamp.GetCurrentTime()
        await event_queue.enqueue_event(TaskStatusUpdateEvent(task_id=task_id, context_id=context_id, status=status))

    async def _publish_initial_task(
        self,
        event_queue: EventQueue,
        *,
        task_id: str,
        context_id: str,
        context: RequestContext,
    ) -> None:
        task = Task(
            id=task_id,
            context_id=context_id,
            status=TaskStatus(state=TaskState.Name(TaskState.TASK_STATE_SUBMITTED)),
        )
        message = getattr(context, "message", None)
        if isinstance(message, Message):
            task.history.append(message)
        await event_queue.enqueue_event(task)

    async def _notify_terminal_task(self, *, task_id: str, context_id: str, state: str) -> None:
        if self._push_notifier is None:
            return
        try:
            await self._push_notifier.notify_task_state(task_id=task_id, context_id=context_id, state=state)
        except Exception:
            logger.warning("A2A push notification failed", exc_info=True)


def _is_retryable_executor_error(exc: Exception) -> bool:
    return isinstance(exc, (TimeoutError, httpx.TimeoutException, httpx.TransportError, ConnectionError))
