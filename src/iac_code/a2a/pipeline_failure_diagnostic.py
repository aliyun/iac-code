from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from typing import ClassVar

from iac_code.a2a.pipeline_continuation import (
    PipelineContinuationConflictError,
    PipelineContinuationCorruptError,
    PipelineContinuationUnsafeError,
)


@dataclass(frozen=True)
class PipelineFailureDiagnostic:
    """Finite operational facts; never retain an exception, message, or frame."""

    exception_type: str = "UNKNOWN"
    guard_reason: str = "UNKNOWN"
    source_module: str = "UNKNOWN"
    source_function: str = "UNKNOWN"
    source_line: int = 0
    task_id: str = "UNKNOWN"
    context_id: str = "UNKNOWN"

    _EXCEPTION_TYPES: ClassVar[tuple[tuple[type[Exception], str], ...]] = (
        (PipelineContinuationUnsafeError, "PipelineContinuationUnsafeError"),
        (PipelineContinuationConflictError, "PipelineContinuationConflictError"),
        (PipelineContinuationCorruptError, "PipelineContinuationCorruptError"),
        (ValueError, "ValueError"),
        (RuntimeError, "RuntimeError"),
        (TypeError, "TypeError"),
        (KeyError, "KeyError"),
        (AttributeError, "AttributeError"),
        (AssertionError, "AssertionError"),
        (OSError, "OSError"),
        (FileNotFoundError, "FileNotFoundError"),
        (PermissionError, "PermissionError"),
        (TimeoutError, "TimeoutError"),
        (ConnectionError, "ConnectionError"),
    )
    _GUARD_MESSAGES: ClassVar[tuple[tuple[str, str], ...]] = (
        ("CONTINUATION_UNSAFE: active_attempt_outcome_unverified", "active_attempt_outcome_unverified"),
        ("CONTINUATION_UNSAFE: successor_execution_outcome_unverified", "successor_execution_outcome_unverified"),
        ("CONTINUATION_UNSAFE: successor_checkpoint_fence_changed", "successor_checkpoint_fence_changed"),
    )
    _ID_PATTERN: ClassVar[re.Pattern[str]] = re.compile(
        r"(?:[0-9a-fA-F]{32}|"
        r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}|"
        r"task-[0-9a-f]{12})"
    )
    _SOURCE_FRAMES: ClassVar[dict[str, tuple[str, frozenset[str]]]] = {
        "iac_code.a2a.pipeline_executor": (
            "pipeline_executor",
            frozenset(
                {
                    "execute",
                    "_select_stream",
                    "_continue_after_interrupt_stream",
                    "_consume_stream_until_restart",
                    "_create_pipeline",
                }
            ),
        ),
        "iac_code.a2a.pipeline_continuation": (
            "pipeline_continuation",
            frozenset({"load", "_load_unlocked", "reserve_successor", "claim_execution", "begin_execution"}),
        ),
        "iac_code.pipeline.engine.pipeline_runner": (
            "pipeline_runner",
            frozenset(
                {
                    "canceled_model_checkpoint_safety",
                    "_model_resume_seed",
                    "handle_user_interrupt",
                    "apply_hard_interrupt",
                    "continue_after_interrupt",
                    "_continue_from_current",
                    "_create_parent_attempt",
                    "_ensure_parent_attempt",
                    "restore_canceled_checkpoint_sync",
                }
            ),
        ),
        "iac_code.pipeline.engine.interrupt": (
            "interrupt",
            frozenset({"judge", "_call_judge_llm", "_build_judge_user_prompt"}),
        ),
        "iac_code.pipeline.engine.transcript_storage": (
            "transcript_storage",
            frozenset({"first_model_request_snapshot", "load"}),
        ),
        "iac_code.pipeline.engine.step_executor": (
            "step_executor",
            frozenset({"execute", "build_agent_loop_context"}),
        ),
        "iac_code.providers.manager": (
            "provider_manager",
            frozenset({"_stream_with_request_lease", "complete"}),
        ),
        "iac_code.agent.agent_loop": (
            "agent_loop",
            frozenset({"_stream_provider", "_stream_provider_with_execution_control"}),
        ),
        "iac_code.pipeline": ("pipeline_factory", frozenset({"create_pipeline"})),
        "iac_code.services.agent_factory": ("agent_factory", frozenset({"create_agent_runtime"})),
    }

    @classmethod
    def from_exception(cls, exc: Exception, *, task_id: object, context_id: object) -> PipelineFailureDiagnostic:
        safe_task_id = cls._safe_id(task_id)
        safe_context_id = cls._safe_id(context_id)
        exception_type = "UNKNOWN"
        for known_type, label in cls._EXCEPTION_TYPES:
            if type(exc) is known_type:
                exception_type = label
                break
        # Preserve the engine's lazy import: a genuine persistence error implies
        # its defining module is already loaded, so no diagnostic import is needed.
        runner = sys.modules.get("iac_code.pipeline.engine.pipeline_runner")
        if runner is not None and type(exc) is vars(runner).get("PipelineStatePersistenceError"):
            exception_type = "PipelineStatePersistenceError"
        source_module, source_function, source_line = cls._source_location(exc)
        return cls(
            exception_type=exception_type,
            guard_reason=cls._guard_reason(exc),
            source_module=source_module,
            source_function=source_function,
            source_line=source_line,
            task_id=safe_task_id,
            context_id=safe_context_id,
        )

    @classmethod
    def _safe_id(cls, value: object) -> str:
        if type(value) is str and len(value) <= 36 and cls._ID_PATTERN.fullmatch(value) is not None:
            return value
        return "UNKNOWN"

    @classmethod
    def _guard_reason(cls, exc: Exception) -> str:
        if type(exc) is not PipelineContinuationUnsafeError:
            return "UNKNOWN"
        args = exc.args
        if type(args) is not tuple or len(args) != 1 or type(args[0]) is not str:
            return "UNKNOWN"
        for message, reason in cls._GUARD_MESSAGES:
            if args[0] == message:
                return reason
        return "UNKNOWN"

    @classmethod
    def _source_location(cls, exc: Exception) -> tuple[str, str, int]:
        location = ("UNKNOWN", "UNKNOWN", 0)
        # Read the built-in descriptor without invoking an unknown exception's
        # overridden attribute access or property.
        traceback = BaseException.__dict__["__traceback__"].__get__(exc)
        for _ in range(32):
            if traceback is None:
                break
            frame = traceback.tb_frame
            module_name = frame.f_globals.get("__name__")
            if type(module_name) is str:
                allowed = cls._SOURCE_FRAMES.get(module_name)
                module = sys.modules.get(module_name)
                # A dynamic frame cannot acquire provenance by copying a known module name.
                if allowed is not None and module is not None and frame.f_globals is vars(module):
                    label, functions = allowed
                    function = frame.f_code.co_name
                    line = traceback.tb_lineno
                    if function in functions and type(line) is int and 1 <= line <= 1_000_000:
                        location = (label, function, line)
            traceback = traceback.tb_next
        return location
