"""Long-running stream-json process mode for local SDK subprocess clients."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
import uuid
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import IO, Any
from urllib.parse import quote

from loguru import logger

from iac_code.cli.process_events import ProcessErrorMapper, ProcessEventSerializer, ProcessSerializedEvent
from iac_code.cli.process_protocol import (
    ProcessFrameParser,
    ProcessFrameValidationError,
    ProcessInputMessage,
    SDKControlRequest,
    SDKControlResponse,
    SDKErrorPayload,
    SDKProcessRuntimeError,
    SDKUpdateEnvironmentVariables,
    SDKUserMessage,
)
from iac_code.types.stream_events import (
    ErrorEvent,
    MessageEndEvent,
    PermissionRequestEvent,
    SubPipelineStreamEvent,
    TextDeltaEvent,
    Usage,
)
from iac_code.utils.project_paths import get_session_path

EXIT_OK = 0
EXIT_ERROR = 1


@dataclass(frozen=True)
class ProcessModeOptions:
    model: str
    cwd: str
    run_mode: str = "normal"
    max_turns: int = 100
    cli_allowed_tools: list[str] | None = None
    cli_disallowed_tools: list[str] | None = None
    cli_permission_mode: str | None = None


class ProcessTransport:
    """Line-oriented stdin/stdout transport for process mode."""

    def __init__(self, input_stream: IO[str] | None = None, output_stream: IO[str] | None = None) -> None:
        self._input_stream = input_stream or sys.stdin
        self._output_stream = output_stream or sys.stdout
        self._write_lock = asyncio.Lock()

    async def readline(self) -> str:
        return await asyncio.to_thread(self._input_stream.readline)

    async def write_frame(self, frame: dict[str, Any]) -> None:
        line = json.dumps(frame, ensure_ascii=False, default=str)
        async with self._write_lock:
            self._output_stream.write(line)
            self._output_stream.write("\n")
            self._output_stream.flush()


class ProcessSessionLock:
    """Cross-process advisory lock for a single cwd/session_id pair."""

    def __init__(self, *, cwd: str, session_id: str) -> None:
        session_path = get_session_path(cwd, session_id)
        self._lock_path = session_path.with_name(f".{session_path.name}.process.lock")
        self._lock_file: IO[bytes] | None = None

    def acquire(self, *, blocking: bool = False) -> bool:
        if self._lock_file is not None:
            return True
        self._lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_file = self._lock_path.open("a+b")
        try:
            if sys.platform == "win32":
                import msvcrt

                lock_file.seek(0)
                mode = msvcrt.LK_LOCK if blocking else msvcrt.LK_NBLCK
                msvcrt.locking(lock_file.fileno(), mode, 1)
            else:
                import fcntl

                flags = fcntl.LOCK_EX if blocking else fcntl.LOCK_EX | fcntl.LOCK_NB
                fcntl.flock(lock_file.fileno(), flags)
        except OSError:
            lock_file.close()
            return False
        self._lock_file = lock_file
        return True

    def release(self) -> None:
        lock_file = self._lock_file
        self._lock_file = None
        if lock_file is None:
            return
        try:
            if sys.platform == "win32":
                import msvcrt

                lock_file.seek(0)
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        finally:
            lock_file.close()

    def __enter__(self) -> "ProcessSessionLock":
        if not self.acquire(blocking=False):
            raise RuntimeError("session_busy")
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.release()


class ProcessRuntimeController:
    """Create iac-code runtimes and execute user turns."""

    def __init__(self, options: ProcessModeOptions) -> None:
        self._options = options
        self.model = options.model
        self._cwd = options.cwd
        self.session_id: str | None = None

    async def initialize(self, frame: SDKControlRequest) -> dict[str, Any]:
        model = frame.payload.get("model")
        if isinstance(model, str) and model:
            self.model = model
        cwd = frame.payload.get("cwd")
        if isinstance(cwd, str) and cwd:
            self._cwd = cwd
        return {
            "protocol_version": "1.0",
            "capabilities": [
                "user",
                "interrupt",
                "set_model",
                "end_session",
                "close",
                "keep_alive",
                "update_environment_variables",
            ],
            "commands": [],
            "agents": [],
            "output_style": "default",
            "available_output_styles": ["default"],
            "models": [
                {
                    "value": self.model,
                    "displayName": self.model,
                    "description": "Current iac-code model",
                }
            ],
            "account": {},
            "cwd": self._cwd,
            "pid": os.getpid(),
        }

    def set_model(self, model: str) -> None:
        self.model = model

    async def run_turn(self, frame: SDKUserMessage):
        from iac_code.services.agent_factory import AgentFactoryOptions, create_agent_runtime

        cwd = frame.cwd or self._cwd or os.getcwd()
        session_lock = ProcessSessionLock(cwd=cwd, session_id=frame.session_id) if frame.session_id else None
        if session_lock is not None and not session_lock.acquire(blocking=False):
            raise SessionBusyError("session is busy")

        runtime = None
        try:
            runtime = create_agent_runtime(
                AgentFactoryOptions(
                    model=self.model,
                    session_id=frame.session_id,
                    cwd=cwd,
                    max_turns=self._options.max_turns,
                    cli_allowed_tools=self._options.cli_allowed_tools,
                    cli_disallowed_tools=self._options.cli_disallowed_tools,
                    cli_permission_mode=self._options.cli_permission_mode,
                )
            )
            self.session_id = runtime.session_id
            async for event in runtime.agent_loop.run_streaming(frame.text):
                permission_event = _permission_request_event(event)
                if permission_event is not None:
                    _auto_answer_permission(permission_event)
                    continue
                yield event
        finally:
            if session_lock is not None:
                session_lock.release()
            close = getattr(runtime, "aclose", None)
            if close is not None:
                try:
                    await close()
                except Exception:
                    logger.debug("Process mode runtime close failed", exc_info=True)

    async def aclose(self) -> None:
        return None


class SessionBusyError(RuntimeError):
    """Raised when another process holds a session lock."""


@dataclass
class ProcessTurnHandle:
    request_id: str
    session_id: str | None
    task: asyncio.Task

    def cancel(self) -> None:
        if not self.task.done():
            self.task.cancel()


@dataclass(frozen=True)
class ProcessResultPatch:
    """Controller-supplied updates for the final result frame."""

    stop_reason: str | None = None
    subtype: str | None = None
    is_error: bool | None = None
    result: str | None = None
    errors: list[str] = field(default_factory=list)
    extra_fields: dict[str, Any] = field(default_factory=dict)


class ProcessTurnResult:
    """Collect Claude-style result fields while a turn streams."""

    def __init__(self) -> None:
        self._text_chunks: list[str] = []
        self._usage = Usage()
        self._stop_reason: str | None = None
        self._errors: list[str] = []
        self._is_error = False
        self._subtype = "success"
        self._result_override: str | None = None
        self._extra_fields: dict[str, Any] = {}

    def observe(self, event: Any, error_mapper: ProcessErrorMapper) -> None:
        if isinstance(event, ProcessResultPatch):
            if event.stop_reason is not None:
                self._stop_reason = event.stop_reason
            if event.subtype is not None:
                self._subtype = event.subtype
            if event.is_error is not None:
                self._is_error = event.is_error
            if event.result is not None:
                self._result_override = event.result
            self._errors.extend(event.errors)
            self._extra_fields.update(event.extra_fields)
            return
        if isinstance(event, TextDeltaEvent):
            self._text_chunks.append(event.text)
            return
        if isinstance(event, MessageEndEvent):
            self._usage = event.usage
            self._stop_reason = event.stop_reason
            return
        if isinstance(event, ErrorEvent):
            payload = error_mapper.from_event(event)
            self.mark_error(payload.message)

    def mark_error(self, message: str, *, stop_reason: str | None = None) -> None:
        self._is_error = True
        self._subtype = "error_during_execution"
        self._errors.append(message)
        if stop_reason is not None:
            self._stop_reason = stop_reason

    def as_frame(self, *, request_id: str, session_id: str, duration_ms: int) -> dict[str, Any]:
        base: dict[str, Any] = {
            "type": "result",
            "request_id": request_id,
            "subtype": self._subtype,
            "duration_ms": duration_ms,
            "duration_api_ms": 0,
            "is_error": self._is_error,
            "num_turns": 1,
            "stop_reason": self._stop_reason,
            "total_cost_usd": 0.0,
            "usage": {
                "input_tokens": self._usage.input_tokens,
                "output_tokens": self._usage.output_tokens,
                "cache_creation_input_tokens": self._usage.cache_creation_input_tokens,
                "cache_read_input_tokens": self._usage.cache_read_input_tokens,
            },
            "modelUsage": {},
            "permission_denials": [],
            "uuid": str(uuid.uuid4()),
            "session_id": session_id,
        }
        if self._is_error:
            base["errors"] = self._errors
        else:
            base["result"] = self._result_override if self._result_override is not None else "".join(self._text_chunks)
        base.update(self._extra_fields)
        return base


@dataclass(frozen=True)
class PipelineProcessCreateRequest:
    context_id: str
    task_id: str
    iac_code_session_id: str
    cwd: str
    model: str
    resume_from_sidecar: bool
    agent_runtime: Any | None = None


@dataclass(frozen=True)
class PipelineProcessContextSnapshot:
    context_id: str
    task_id: str
    iac_code_session_id: str
    cwd: str
    sidecar_status: str | None = None
    active_task_id: str | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PipelineProcessContextSnapshot":
        return cls(
            context_id=str(data["contextId"]),
            task_id=str(data["taskId"]),
            iac_code_session_id=str(data["iacCodeSessionId"]),
            cwd=str(data["cwd"]),
            sidecar_status=data.get("sidecarStatus") if isinstance(data.get("sidecarStatus"), str) else None,
            active_task_id=data.get("activeTaskId") if isinstance(data.get("activeTaskId"), str) else None,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "contextId": self.context_id,
            "taskId": self.task_id,
            "iacCodeSessionId": self.iac_code_session_id,
            "cwd": self.cwd,
            "sidecarStatus": self.sidecar_status,
            "activeTaskId": self.active_task_id,
        }


class PipelineProcessContextLock:
    """Cross-process advisory lock for one pipeline context."""

    def __init__(self, lock_path: Path) -> None:
        self._lock_path = lock_path
        self._lock_file: IO[bytes] | None = None

    def acquire(self, *, blocking: bool = True) -> bool:
        if self._lock_file is not None:
            return True
        self._lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_file = self._lock_path.open("a+b")
        try:
            if sys.platform == "win32":
                import msvcrt

                lock_file.seek(0)
                mode = msvcrt.LK_LOCK if blocking else msvcrt.LK_NBLCK
                msvcrt.locking(lock_file.fileno(), mode, 1)
            else:
                import fcntl

                flags = fcntl.LOCK_EX if blocking else fcntl.LOCK_EX | fcntl.LOCK_NB
                fcntl.flock(lock_file.fileno(), flags)
        except OSError:
            lock_file.close()
            return False
        self._lock_file = lock_file
        return True

    def release(self) -> None:
        lock_file = self._lock_file
        self._lock_file = None
        if lock_file is None:
            return
        try:
            if sys.platform == "win32":
                import msvcrt

                lock_file.seek(0)
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        finally:
            lock_file.close()

    def __enter__(self) -> "PipelineProcessContextLock":
        self.acquire(blocking=True)
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.release()


class PipelineProcessContextStore:
    """Small process-mode store for pipeline context/task recovery."""

    def __init__(self, root: Path | None = None) -> None:
        self._root = root

    @property
    def root(self) -> Path:
        if self._root is not None:
            return self._root
        from iac_code.config import get_config_dir

        return get_config_dir() / "process-pipeline" / "contexts"

    def lock(self, context_id: str) -> PipelineProcessContextLock:
        return PipelineProcessContextLock(self._path_for(context_id).with_suffix(".lock"))

    def load(self, context_id: str) -> PipelineProcessContextSnapshot | None:
        path = self._path_for(context_id)
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            logger.exception("Failed to load process pipeline context %s", context_id)
            return None
        if not isinstance(data, dict):
            return None
        try:
            return PipelineProcessContextSnapshot.from_dict(data)
        except (KeyError, TypeError, ValueError):
            logger.exception("Invalid process pipeline context snapshot %s", context_id)
            return None

    def save(self, snapshot: PipelineProcessContextSnapshot) -> None:
        path = self._path_for(snapshot.context_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(snapshot.as_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)

    def _path_for(self, context_id: str) -> Path:
        return self.root / f"{quote(context_id, safe='')}.json"


@dataclass
class PipelineProcessTurnState:
    status: str = "completed"
    sidecar_status: str | None = None
    stop_reason: str = "end_turn"
    is_error: bool = False

    def observe(self, event: Any) -> None:
        from iac_code.pipeline.engine.events import PipelineEvent, PipelineEventType

        if not isinstance(event, PipelineEvent):
            return
        if event.type == PipelineEventType.USER_INPUT_REQUIRED:
            self.status = "input_required"
            self.sidecar_status = "waiting_input"
            self.stop_reason = "input_required"
            return
        if event.type == PipelineEventType.BACKUP_BLOCKED:
            self.status = "input_required"
            self.sidecar_status = "backup_blocked"
            self.stop_reason = "input_required"
            return
        if event.type == PipelineEventType.PIPELINE_COMPLETED:
            failed = event.data.get("failed") is True if isinstance(event.data, dict) else False
            self.status = "failed" if failed else "completed"
            self.sidecar_status = self.status
            self.stop_reason = "error" if failed else "end_turn"
            self.is_error = failed
            return
        if event.type == PipelineEventType.PIPELINE_ERROR:
            self.status = "failed"
            self.sidecar_status = "failed"
            self.stop_reason = "error"
            self.is_error = True
            return
        if event.type == PipelineEventType.INTERRUPTED:
            self.status = "canceled"
            self.sidecar_status = "canceled"
            self.stop_reason = "cancelled"
            self.is_error = True


class PipelineProcessRuntimeController:
    """Execute pipeline mode through the SDK process protocol."""

    def __init__(
        self,
        options: ProcessModeOptions,
        *,
        agent_runtime_factory: Any | None = None,
        pipeline_factory: Any | None = None,
        context_store: PipelineProcessContextStore | None = None,
        aliyun_delegated_executor_factory: Any | None = None,
    ) -> None:
        self._options = options
        self.model = options.model
        self._cwd = options.cwd
        self._agent_runtime_factory = agent_runtime_factory or self._default_agent_runtime_factory
        self._pipeline_factory = pipeline_factory or self._default_pipeline_factory
        self._context_store = context_store or PipelineProcessContextStore()
        self._aliyun_delegated_executor_factory = aliyun_delegated_executor_factory
        self._agent_runtime: Any | None = None
        self._pipeline: Any | None = None
        self._translator: Any | None = None
        self._context_id: str | None = None
        self._task_id: str | None = None
        self.session_id: str | None = None

    async def initialize(self, frame: SDKControlRequest) -> dict[str, Any]:
        model = frame.payload.get("model")
        if isinstance(model, str) and model:
            self.model = model
        cwd = frame.payload.get("cwd")
        if isinstance(cwd, str) and cwd:
            self._cwd = cwd
        return {
            "protocol_version": "1.0",
            "capabilities": [
                "user",
                "interrupt",
                "set_model",
                "end_session",
                "close",
                "keep_alive",
                "update_environment_variables",
                "pipeline",
                "pipeline_resume",
                "pipeline_recoverable_task",
                "pipeline_cancel",
            ],
            "commands": [],
            "agents": [],
            "output_style": "default",
            "available_output_styles": ["default"],
            "models": [
                {
                    "value": self.model,
                    "displayName": self.model,
                    "description": "Current iac-code model",
                }
            ],
            "account": {},
            "cwd": self._cwd,
            "pid": os.getpid(),
        }

    def set_model(self, model: str) -> None:
        self.model = model

    async def run_turn(self, frame: SDKUserMessage):
        from iac_code.pipeline.engine.user_input import normalize_pipeline_user_input

        cwd = frame.cwd or self._cwd or os.getcwd()
        metadata = _pipeline_metadata(frame.metadata)
        requested_context_id = _first_metadata_string(metadata, "contextId", "context_id")
        requested_task_id = _first_metadata_string(metadata, "taskId", "task_id")
        requested_iac_session_id = _first_metadata_string(metadata, "iacCodeSessionId", "iac_code_session_id")
        context_id = requested_context_id or self._context_id or str(uuid.uuid4())

        with self._context_store.lock(context_id):
            snapshot = self._context_store.load(context_id)
            self._validate_pipeline_context(snapshot, cwd=cwd, task_id=requested_task_id)
            task_id = requested_task_id or (snapshot.task_id if snapshot and snapshot.active_task_id is None else None)
            if task_id is None:
                if snapshot is not None and snapshot.active_task_id:
                    self._raise_recoverable_task(snapshot)
                task_id = str(uuid.uuid4())
            iac_code_session_id = (
                requested_iac_session_id
                or (snapshot.iac_code_session_id if snapshot is not None else None)
                or frame.session_id
                or str(uuid.uuid4())[:8]
            )
            running_snapshot = PipelineProcessContextSnapshot(
                context_id=context_id,
                task_id=task_id,
                iac_code_session_id=iac_code_session_id,
                cwd=cwd,
                sidecar_status="running",
                active_task_id=task_id,
            )
            self._context_store.save(running_snapshot)

        request = PipelineProcessCreateRequest(
            context_id=context_id,
            task_id=task_id,
            iac_code_session_id=iac_code_session_id,
            cwd=cwd,
            model=self.model,
            resume_from_sidecar=snapshot is not None,
        )
        pipeline = await self._ensure_pipeline(request)
        pipeline_input = normalize_pipeline_user_input(frame.text)
        stream = self._pipeline_stream(pipeline, pipeline_input, snapshot)
        state = PipelineProcessTurnState()

        async for event in stream:
            state.observe(event)
            for payload in self._translate_pipeline_event(event, request):
                yield ProcessSerializedEvent({"type": "pipeline_event", **payload})
            permission_event = _permission_request_event(event)
            if permission_event is not None:
                _auto_answer_permission(permission_event)

        sidecar_status = getattr(pipeline, "sidecar_status", None) or state.sidecar_status or state.status
        state.sidecar_status = sidecar_status
        if sidecar_status in {"waiting_input", "backup_blocked"}:
            state.status = "input_required"
            state.stop_reason = "input_required"
        await self._persist_pipeline_result(request, state)
        self._context_id = context_id
        self._task_id = task_id
        self.session_id = iac_code_session_id
        yield ProcessResultPatch(
            stop_reason=state.stop_reason,
            subtype="error_during_execution" if state.is_error else "success",
            is_error=state.is_error,
            result="",
            extra_fields={
                "pipeline": {
                    "mode": "pipeline",
                    "name": self._pipeline_name(),
                    "contextId": context_id,
                    "taskId": task_id,
                    "iacCodeSessionId": iac_code_session_id,
                    "status": state.status,
                    "sidecarStatus": state.sidecar_status,
                }
            },
        )

    async def aclose(self) -> None:
        runtime = self._agent_runtime
        self._agent_runtime = None
        self._pipeline = None
        self._translator = None
        await self._close_agent_runtime(runtime)

    @staticmethod
    async def _close_agent_runtime(runtime: Any | None) -> None:
        close = getattr(runtime, "aclose", None)
        if close is None:
            return
        try:
            result = close()
            if asyncio.iscoroutine(result):
                await result
        except Exception:
            logger.debug("Pipeline process runtime close failed", exc_info=True)

    def _validate_pipeline_context(
        self, snapshot: PipelineProcessContextSnapshot | None, *, cwd: str, task_id: str | None
    ) -> None:
        if snapshot is None:
            return
        if snapshot.cwd != cwd:
            raise SDKProcessRuntimeError(
                SDKErrorPayload(
                    code="pipeline_context_mismatch",
                    message="Pipeline context belongs to a different cwd.",
                    retryable=False,
                    data={"contextId": snapshot.context_id, "cwd": snapshot.cwd},
                )
            )
        if snapshot.active_task_id is not None and task_id != snapshot.active_task_id:
            self._raise_recoverable_task(snapshot)

    def _raise_recoverable_task(self, snapshot: PipelineProcessContextSnapshot) -> None:
        raise SDKProcessRuntimeError(
            SDKErrorPayload(
                code="pipeline_task_required",
                message="Pipeline context already has a recoverable task.",
                retryable=True,
                data={
                    "contextId": snapshot.context_id,
                    "recoverableTaskId": snapshot.active_task_id or snapshot.task_id,
                    "sidecarStatus": snapshot.sidecar_status,
                },
            )
        )

    async def _ensure_pipeline(self, request: PipelineProcessCreateRequest) -> Any:
        if self._pipeline is not None and self._context_id == request.context_id and self._task_id == request.task_id:
            return self._pipeline

        runtime = self._agent_runtime_factory(request)
        try:
            request = replace(request, agent_runtime=runtime)
            pipeline = self._pipeline_factory(request)
            translator = self._create_translator(request)
        except BaseException:
            await self._close_agent_runtime(runtime)
            raise

        previous_runtime = self._agent_runtime
        self._agent_runtime = runtime
        self._pipeline = pipeline
        self._translator = translator
        self._context_id = request.context_id
        self._task_id = request.task_id
        self.session_id = request.iac_code_session_id
        if previous_runtime is not runtime:
            await self._close_agent_runtime(previous_runtime)
        return pipeline

    def _pipeline_stream(self, pipeline: Any, pipeline_input: Any, snapshot: PipelineProcessContextSnapshot | None):
        sidecar_status = getattr(pipeline, "sidecar_status", None) or (snapshot.sidecar_status if snapshot else None)
        if sidecar_status in {"waiting_input", "backup_blocked"}:
            if sidecar_status == "backup_blocked" and hasattr(pipeline, "continue_from_sidecar"):
                return pipeline.continue_from_sidecar(pipeline_input)
            return pipeline.resume(pipeline_input)
        return pipeline.run(pipeline_input)

    def _translate_pipeline_event(self, event: Any, request: PipelineProcessCreateRequest) -> list[dict[str, Any]]:
        if self._translator is None:
            self._translator = self._create_translator(request)
        return self._translator.translate(event)

    def _create_translator(self, request: PipelineProcessCreateRequest) -> Any:
        from iac_code.a2a.pipeline_events import PipelineA2AContext, PipelineEventTranslator

        return PipelineEventTranslator(
            PipelineA2AContext(
                pipeline_run_id=request.context_id,
                task_id=request.task_id,
                context_id=request.context_id,
                pipeline_name=self._pipeline_name(),
                iac_code_session_id=request.iac_code_session_id,
            )
        )

    async def _persist_pipeline_result(
        self, request: PipelineProcessCreateRequest, state: PipelineProcessTurnState
    ) -> None:
        active_task_id = request.task_id if state.status in {"input_required", "working"} else None
        with self._context_store.lock(request.context_id):
            self._context_store.save(
                PipelineProcessContextSnapshot(
                    context_id=request.context_id,
                    task_id=request.task_id,
                    iac_code_session_id=request.iac_code_session_id,
                    cwd=request.cwd,
                    sidecar_status=state.sidecar_status,
                    active_task_id=active_task_id,
                )
            )

    def _default_agent_runtime_factory(self, request: PipelineProcessCreateRequest) -> Any:
        from iac_code.services.agent_factory import AgentFactoryOptions, create_agent_runtime

        return create_agent_runtime(
            AgentFactoryOptions(
                model=request.model,
                session_id=request.iac_code_session_id,
                cwd=request.cwd,
                max_turns=self._options.max_turns,
                cli_allowed_tools=self._options.cli_allowed_tools,
                cli_disallowed_tools=self._options.cli_disallowed_tools,
                cli_permission_mode=self._options.cli_permission_mode,
            )
        )

    def _default_pipeline_factory(self, request: PipelineProcessCreateRequest) -> Any:
        from iac_code.pipeline import create_pipeline
        from iac_code.services.session_backup import SessionBackupService
        from iac_code.services.session_storage import SessionStorage

        runtime = request.agent_runtime
        if runtime is None:
            raise RuntimeError("pipeline process requires an agent runtime")
        agent_loop = getattr(runtime, "agent_loop", None)
        session_storage = getattr(agent_loop, "_session_storage", None)
        if session_storage is None:
            session_storage = SessionStorage()
        command_registry = getattr(runtime, "command_registry", None)
        skills = command_registry.get_model_invocable_skills() if command_registry is not None else None
        permission_context = getattr(agent_loop, "permission_context", None)
        delegated_factory = self._aliyun_delegated_executor_factory
        if delegated_factory is None:
            services = getattr(runtime, "aliyun_services", None)
            delegated_factory = getattr(services, "delegated_executor_factory", None)
        return create_pipeline(
            self._pipeline_name(),
            provider_manager=runtime.provider_manager,
            base_tool_registry=runtime.tool_registry,
            session_storage=session_storage,
            session_id=request.iac_code_session_id,
            cwd=request.cwd,
            permission_context_getter=lambda: permission_context,
            auto_trigger_skills=skills,
            resume_from_sidecar=request.resume_from_sidecar,
            surface="process",
            backup_service=SessionBackupService(session_storage=session_storage),
            mcp_manager=getattr(runtime, "mcp_manager", None),
            mcp_config_warnings=getattr(runtime, "mcp_config_warnings", None),
            aliyun_delegated_executor_factory=delegated_factory,
        )

    def _pipeline_name(self) -> str:
        from iac_code.pipeline.config import get_pipeline_name

        return get_pipeline_name()


class ProcessRuntimeControllerFactory:
    """Select the process runtime controller for the configured run mode."""

    @staticmethod
    def create(options: ProcessModeOptions) -> Any:
        if options.run_mode == "pipeline":
            return PipelineProcessRuntimeController(options)
        return ProcessRuntimeController(options)


class ProcessQuery:
    """Bidirectional control protocol manager for process mode."""

    def __init__(
        self,
        *,
        transport: ProcessTransport,
        runtime_controller: Any,
        parser: ProcessFrameParser | None = None,
        serializer: ProcessEventSerializer | None = None,
        error_mapper: ProcessErrorMapper | None = None,
    ) -> None:
        self._transport = transport
        self._runtime_controller = runtime_controller
        self._parser = parser or ProcessFrameParser()
        self._serializer = serializer or ProcessEventSerializer()
        self._error_mapper = error_mapper or ProcessErrorMapper()
        self._initialized = False
        self._active_turn: ProcessTurnHandle | None = None
        self._turn_counter = 0
        self._exit_code = EXIT_OK
        self._stop_requested = False

    async def run(self) -> int:
        try:
            while not self._stop_requested:
                line = await self._transport.readline()
                if line == "":
                    break
                try:
                    frame = self._parser.parse_line(line)
                except ProcessFrameValidationError as exc:
                    await self._write_error(self._error_mapper.from_exception(exc), exc.request_id)
                    self._exit_code = EXIT_ERROR
                    continue
                if frame is None:
                    continue
                await self._handle_frame(frame)
            if self._active_turn is not None:
                await self._wait_active_turn()
        finally:
            close = getattr(self._runtime_controller, "aclose", None)
            if close is not None:
                await close()
        return self._exit_code

    async def _handle_frame(self, frame: ProcessInputMessage) -> None:
        if isinstance(frame, SDKControlRequest):
            await self._handle_control(frame)
            return
        if isinstance(frame, SDKControlResponse):
            return
        if isinstance(frame, SDKUpdateEnvironmentVariables):
            os.environ.update(frame.variables)
            return
        await self._handle_user(frame)

    async def _handle_control(self, frame: SDKControlRequest) -> None:
        subtype = frame.subtype
        if subtype == "initialize":
            if self._initialized:
                await self._write_control_error(
                    frame.request_id,
                    "already_initialized",
                    "process mode is already initialized",
                )
                self._exit_code = EXIT_ERROR
                return
            response = await self._runtime_controller.initialize(frame)
            self._initialized = True
            await self._write_control_success(frame.request_id, response)
            return
        if subtype in {"close", "end_session"}:
            if self._active_turn is not None:
                self._active_turn.cancel()
                await self._wait_active_turn()
            await self._write_control_success(frame.request_id, {"ok": True})
            self._stop_requested = True
            return
        if not self._initialized:
            await self._write_control_error(
                frame.request_id,
                "not_initialized",
                "initialize is required before control requests",
            )
            self._exit_code = EXIT_ERROR
            return
        if subtype == "interrupt":
            if self._active_turn is not None:
                self._active_turn.cancel()
            await self._write_control_success(frame.request_id, {"ok": True})
            return
        if subtype == "set_model":
            model = frame.payload.get("model")
            if not isinstance(model, str) or not model:
                await self._write_control_error(frame.request_id, "invalid_frame", "set_model requires model")
                self._exit_code = EXIT_ERROR
                return
            self._runtime_controller.set_model(model)
            await self._write_control_success(frame.request_id, {"ok": True})
            return
        await self._write_control_error(
            frame.request_id,
            "unsupported_control",
            f"Unsupported control subtype: {subtype}",
        )
        self._exit_code = EXIT_ERROR

    async def _handle_user(self, frame: SDKUserMessage) -> None:
        if not self._initialized:
            await self._write_error(
                SDKErrorPayload(
                    code="not_initialized",
                    message="initialize is required before user messages",
                    retryable=False,
                ),
                frame.request_id,
            )
            self._exit_code = EXIT_ERROR
            return
        if self._active_turn is not None and not self._active_turn.task.done():
            await self._write_error(
                SDKErrorPayload(code="turn_active", message="another turn is already active", retryable=True),
                frame.request_id,
            )
            return
        request_id = frame.request_id or self._next_turn_request_id()
        turn_started = asyncio.Event()
        task = asyncio.create_task(self._run_turn(frame, request_id, turn_started))
        self._active_turn = ProcessTurnHandle(request_id=request_id, session_id=frame.session_id, task=task)
        await turn_started.wait()

    async def _run_turn(self, frame: SDKUserMessage, request_id: str, turn_started: asyncio.Event) -> None:
        started = time.monotonic()
        result = ProcessTurnResult()
        turn_started.set()
        try:
            async for event in self._runtime_controller.run_turn(frame):
                result.observe(event, self._error_mapper)
                if isinstance(event, ProcessResultPatch):
                    continue
                await self._write_stream_event(request_id, frame.session_id, event)
        except asyncio.CancelledError as exc:
            result.mark_error(self._error_mapper.from_exception(exc).message, stop_reason="cancelled")
        except SessionBusyError:
            result.mark_error("session is busy")
            await self._write_error(
                SDKErrorPayload(code="session_busy", message="session is busy", retryable=True),
                request_id,
            )
        except SDKProcessRuntimeError as exc:
            payload = self._error_mapper.from_exception(exc)
            result.mark_error(payload.message)
            await self._write_error(payload, request_id)
        except Exception as exc:
            result.mark_error(self._error_mapper.from_exception(exc).message)
        finally:
            session_id = frame.session_id or getattr(self._runtime_controller, "session_id", None) or ""
            await self._transport.write_frame(
                result.as_frame(
                    request_id=request_id,
                    session_id=session_id,
                    duration_ms=int((time.monotonic() - started) * 1000),
                )
            )
            if self._active_turn is not None and self._active_turn.request_id == request_id:
                self._active_turn = None

    async def _wait_active_turn(self) -> None:
        handle = self._active_turn
        if handle is None:
            return
        try:
            await handle.task
        except asyncio.CancelledError:
            pass

    def _next_turn_request_id(self) -> str:
        self._turn_counter += 1
        return f"turn-{self._turn_counter}"

    async def _write_stream_event(self, request_id: str, session_id: str | None, event: Any) -> None:
        await self._transport.write_frame(
            {
                "type": "stream_event",
                "request_id": request_id,
                "session_id": session_id or getattr(self._runtime_controller, "session_id", None) or "",
                "parent_tool_use_id": None,
                "uuid": str(uuid.uuid4()),
                "event": self._serializer.serialize(event),
            }
        )

    async def _write_error(self, payload: SDKErrorPayload, request_id: str | None) -> None:
        await self._transport.write_frame({"type": "error", "request_id": request_id, "error": payload.as_dict()})

    async def _write_control_success(self, request_id: str, response: dict[str, Any]) -> None:
        await self._transport.write_frame(
            {
                "type": "control_response",
                "response": {
                    "subtype": "success",
                    "request_id": request_id,
                    "response": response,
                },
            }
        )

    async def _write_control_error(self, request_id: str, code: str, message: str) -> None:
        await self._transport.write_frame(
            {
                "type": "control_response",
                "response": {
                    "subtype": "error",
                    "request_id": request_id,
                    "code": code,
                    "error": message,
                },
            }
        )


class ProcessModeRunner:
    """Top-level CLI runner for stream-json process mode."""

    def __init__(
        self,
        options: ProcessModeOptions,
        *,
        input_stream: IO[str] | None = None,
        output_stream: IO[str] | None = None,
        runtime_controller: Any | None = None,
    ) -> None:
        self._options = options
        self._transport = ProcessTransport(input_stream=input_stream, output_stream=output_stream)
        self._runtime_controller = runtime_controller or ProcessRuntimeControllerFactory.create(options)

    async def run(self) -> int:
        query = ProcessQuery(transport=self._transport, runtime_controller=self._runtime_controller)
        return await query.run()


def _pipeline_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    iac_code = metadata.get("iac_code")
    return iac_code if isinstance(iac_code, dict) else {}


def _first_metadata_string(metadata: dict[str, Any], *names: str) -> str | None:
    for name in names:
        value = metadata.get(name)
        if isinstance(value, str) and value:
            return value
    return None


def _permission_request_event(event: Any) -> PermissionRequestEvent | None:
    if isinstance(event, PermissionRequestEvent):
        return event
    if isinstance(event, SubPipelineStreamEvent):
        return _permission_request_event(event.inner)
    return None


def _auto_answer_permission(event: PermissionRequestEvent) -> None:
    if event.response_future is None or event.response_future.done():
        return
    from iac_code.services.permissions.audit import (
        emit_auto_permission_audit,
        is_aliyun_api_non_read_only_permission_event,
    )

    approved = not is_aliyun_api_non_read_only_permission_event(event)
    audit_ok = emit_auto_permission_audit(
        event,
        decision="allow" if approved else "deny",
        scope="auto_approve" if approved else "auto_deny",
        source="process_mode_auto_approve" if approved else "process_mode_auto_deny",
    )
    if approved and not audit_ok:
        approved = False
    event.response_future.set_result(approved)
