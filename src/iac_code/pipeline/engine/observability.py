"""Best-effort telemetry adapter for pipeline runtime lifecycle signals."""

from __future__ import annotations

import logging
import re
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from iac_code.services.telemetry import add_metric, get_session_id, get_user_id, log_event, start_span
from iac_code.services.telemetry.config import ContentCaptureMode, get_content_capture_mode
from iac_code.services.telemetry.names import (
    FRAMEWORK_IAC_CODE,
    Events,
    GenAiAttr,
    GenAiOperationName,
    GenAiSpanKind,
    Metrics,
    Spans,
)
from iac_code.utils.public_errors import sanitize_public_text

logger = logging.getLogger(__name__)

_METRIC_ATTR_KEYS = frozenset(
    {
        "all_failed",
        "candidate_index",
        "candidate_count_bucket",
        "error_type",
        "from_step",
        "input_length_bucket",
        "parent_step_id",
        "reason",
        "rollback_scope",
        "status",
        "step_attempt",
        "step_id",
        "step_index",
        "step_type",
        "sub_pipeline_name",
        "sub_step_id",
        "sub_step_index",
        "to_step",
        "total_steps",
        "total_sub_steps",
        "ui_mode",
    }
)

_SENSITIVE_EVENT_KEYS = frozenset(
    {
        "cwd",
        "candidate_name",
        "error_summary",
        "rollback_reason",
        "selected_value",
        "selected_name",
        "prompt",
        "user_prompt",
    }
)

_TEXT_LIMITS = {
    "candidate_name": 200,
    "selected_name": 200,
    "selected_value": 200,
    "cwd": 1000,
    "error_summary": 1000,
    "rollback_reason": 1000,
    "prompt": 1000,
    "user_prompt": 1000,
}

_SECRET_PATTERNS = (
    re.compile(
        r"""(?ix)
        (?<![\w.-])["']?(?:api\s+key|access\s+key\s+(?:id|secret)|access\s+token|secret\s+key)["']?
        \s*[:=]\s*
        (?:"[^"]*"|'[^']*'|[^'"\s,;}]+)
        """
    ),
    re.compile(
        r"""(?ix)
        ["']?[\w.-]*
        (?:access[_-]?key(?:[_-]?(?:id|secret))?|api[_-]?key|token|password|secret)
        [\w.-]*["']?
        \s*[:=]\s*
        (?:"[^"]*"|'[^']*'|[^'"\s,;}]+)
        """
    ),
    re.compile(
        r"""(?ix)
        ["']?authorization["']?
        \s*[:=]\s*
        \{[^{}]*["']?scheme["']?\s*[:=]\s*(?:"bearer"|'bearer'|bearer)[^{}]*["']?credentials["']?\s*[:=]\s*(?:"[^"]*"|'[^']*'|[^'"\s,;}]+)
        """
    ),
    re.compile(
        r"""(?ix)
        ["']?authorization["']?
        \s*[:=]\s*
        \{[^{}]*["']?credentials["']?\s*[:=]\s*(?:"[^"]*"|'[^']*'|[^'"\s,;}]+)[^{}]*["']?scheme["']?\s*[:=]\s*(?:"bearer"|'bearer'|bearer)
        """
    ),
    re.compile(
        r"""(?ix)
        ["']?authorization["']?
        \s*[:=]\s*
        (?:"bearer\s+[^"]*"|'bearer\s+[^']*'|bearer\s+[^,\s;]+)
        """
    ),
    re.compile(r"(?i)\b(AKIA[0-9A-Z]{16}|LTAI[0-9A-Za-z]{12,})\b"),
)


class PipelineObservability:
    """Small adapter that keeps telemetry calls out of pipeline control flow."""

    def __init__(self, *, pipeline_name: str, session_id: str, cwd: str) -> None:
        self.pipeline_name = pipeline_name
        self.session_id = session_id
        self.cwd = cwd
        self.pipeline_started_at: float | None = None
        self._correlation_attrs: dict[str, str] = {}

    @staticmethod
    def now() -> float:
        return time.monotonic()

    @staticmethod
    def duration_ms(started_at: float) -> float:
        return round((time.monotonic() - started_at) * 1000, 3)

    @staticmethod
    def length_bucket(value: Any) -> str:
        text = "" if value is None else str(value)
        length = len(text)
        if length == 0:
            return "0"
        if length <= 50:
            return "1-50"
        if length <= 200:
            return "51-200"
        if length <= 1000:
            return "201-1000"
        return "1001+"

    @staticmethod
    def count_bucket(value: int) -> str:
        if value <= 0:
            return "0"
        if value == 1:
            return "1"
        if value <= 5:
            return "2-5"
        if value <= 10:
            return "6-10"
        return "11+"

    @staticmethod
    def _debug_event_content_enabled() -> bool:
        try:
            mode = get_content_capture_mode()
            if mode in (
                ContentCaptureMode.EVENT_ONLY,
                ContentCaptureMode.SPAN_AND_EVENT,
            ):
                return True
            return False
        except Exception as exc:
            logger.warning(
                "Pipeline telemetry event content-capture check failed: error_type=%s",
                type(exc).__name__,
            )
            return False

    @staticmethod
    def _redact_obvious_secrets(text: str) -> str:
        redacted = sanitize_public_text(text)
        for pattern in _SECRET_PATTERNS:
            redacted = pattern.sub("[REDACTED]", redacted)
        return redacted

    @classmethod
    def _sanitize_non_sensitive_value(cls, value: Any) -> Any:
        if isinstance(value, str):
            return cls._redact_obvious_secrets(value)
        return value

    @classmethod
    def _truncate_event_text(cls, key: str, value: Any) -> str:
        text = cls._redact_obvious_secrets("" if value is None else str(value))
        limit = _TEXT_LIMITS.get(key, 1000)
        if len(text) > limit:
            return text[:limit] + "..."
        return text

    @classmethod
    def _derived_sensitive_attrs(cls, key: str, value: Any) -> dict[str, Any]:
        present = bool(value)
        if key == "cwd":
            return {"cwd_present": present}
        if key == "candidate_name":
            return {
                "candidate_name_present": present,
                "candidate_name_length_bucket": cls.length_bucket(value),
            }
        if key == "error_summary":
            return {"error_summary_present": present}
        if key == "rollback_reason":
            return {"rollback_reason_present": present}
        if key in {"selected_value", "selected_name"}:
            return {"selected_value_present": present}
        if key in {"prompt", "user_prompt"}:
            return {"prompt_present": present}
        return {}

    @classmethod
    def _sanitize_event_attrs(cls, attrs: dict[str, Any]) -> dict[str, Any]:
        include_raw = cls._debug_event_content_enabled()
        sanitized: dict[str, Any] = {}
        for key, value in attrs.items():
            if value is None:
                continue
            if key not in _SENSITIVE_EVENT_KEYS:
                sanitized[key] = cls._sanitize_non_sensitive_value(value)
                continue
            if include_raw:
                sanitized[key] = cls._truncate_event_text(key, value)
            else:
                sanitized.update(cls._derived_sensitive_attrs(key, value))
        return sanitized

    @classmethod
    def _sanitize_local_log_attrs(cls, attrs: dict[str, Any]) -> dict[str, Any]:
        sanitized: dict[str, Any] = {}
        for key, value in attrs.items():
            if value is None:
                continue
            if key in _SENSITIVE_EVENT_KEYS:
                sanitized.update(cls._derived_sensitive_attrs(key, value))
            else:
                sanitized[key] = cls._sanitize_non_sensitive_value(value)
        return sanitized

    @classmethod
    def _sanitize_span_attrs(cls, attrs: dict[str, Any]) -> dict[str, Any]:
        return cls._sanitize_local_log_attrs(attrs)

    def set_correlation(
        self,
        *,
        task_id: str | None = None,
        context_id: str | None = None,
        pipeline_run_id: str | None = None,
    ) -> None:
        self._correlation_attrs = {
            key: value
            for key, value in {
                "task_id": task_id,
                "context_id": context_id,
                "pipeline_run_id": pipeline_run_id,
            }.items()
            if value
        }

    def base_attrs(self, **attrs: Any) -> dict[str, Any]:
        data = {
            "pipeline_name": self.pipeline_name,
            "session_id": self.session_id,
            "cwd": self.cwd,
        }
        data.update(self._correlation_attrs)
        data.update({key: value for key, value in attrs.items() if value is not None})
        return data

    def genai_attrs(
        self,
        *,
        span_kind: str,
        operation_name: str,
        react_round: int | None = None,
    ) -> dict[str, Any]:
        attrs: dict[str, Any] = {
            GenAiAttr.SPAN_KIND: span_kind,
            GenAiAttr.OPERATION_NAME: operation_name,
            GenAiAttr.FRAMEWORK: FRAMEWORK_IAC_CODE,
            GenAiAttr.AGENT_NAME: self.pipeline_name,
        }
        if react_round is not None:
            attrs[GenAiAttr.REACT_ROUND] = react_round
        try:
            attrs[GenAiAttr.SESSION_ID] = get_session_id()
        except Exception as exc:
            logger.warning(
                "Pipeline telemetry session id lookup failed: error_type=%s",
                type(exc).__name__,
            )
        try:
            attrs[GenAiAttr.USER_ID] = get_user_id()
        except Exception as exc:
            logger.warning(
                "Pipeline telemetry user id lookup failed: error_type=%s",
                type(exc).__name__,
            )
        return attrs

    def span_attrs(
        self,
        *,
        span_kind: str,
        operation_name: str,
        react_round: int | None = None,
        **attrs: Any,
    ) -> dict[str, Any]:
        data = self.base_attrs(**attrs)
        data.update(
            self.genai_attrs(
                span_kind=span_kind,
                operation_name=operation_name,
                react_round=react_round,
            )
        )
        return data

    def metric_attrs(self, **attrs: Any) -> dict[str, Any]:
        """Return low-cardinality labels suitable for metrics."""
        data = {"pipeline_name": self.pipeline_name}
        data.update({key: value for key, value in attrs.items() if value is not None and key in _METRIC_ATTR_KEYS})
        return data

    def _event(self, name: str, attrs: dict[str, Any]) -> None:
        try:
            log_event(name, self._sanitize_event_attrs(attrs))
        except Exception as exc:
            try:
                local_log_attrs = self._sanitize_local_log_attrs(attrs)
            except Exception:
                local_log_attrs = {"attrs_sanitized": False, "attr_count": len(attrs)}
            logger.warning(
                "Pipeline telemetry emission failed: event=%s error_type=%s attrs=%s",
                name,
                type(exc).__name__,
                local_log_attrs,
            )

    def _metric(self, name: str, value: int | float, attrs: dict[str, Any]) -> None:
        try:
            add_metric(name, value, attrs)
        except Exception:
            logger.warning(
                "Pipeline telemetry metric failed: metric=%s value=%s attrs=%s",
                name,
                value,
                attrs,
                exc_info=True,
            )

    @contextmanager
    def _span(self, name: str, attrs: dict[str, Any]) -> Iterator[Any]:
        try:
            span_attrs = self._sanitize_span_attrs(attrs)
        except Exception:
            span_attrs = {"attrs_sanitized": False, "attr_count": len(attrs)}
        try:
            span_context = start_span(name, span_attrs)
            span = span_context.__enter__()
        except Exception as exc:
            logger.warning(
                "Pipeline telemetry span failed to start: span=%s error_type=%s attrs=%s",
                name,
                type(exc).__name__,
                span_attrs,
            )
            yield None
            return

        try:
            yield span
        except BaseException:
            exc_info = sys.exc_info()
            try:
                span_context.__exit__(*exc_info)
            except Exception as close_exc:
                logger.warning(
                    "Pipeline telemetry span failed to close: span=%s error_type=%s attrs=%s",
                    name,
                    type(close_exc).__name__,
                    span_attrs,
                )
            raise
        else:
            try:
                span_context.__exit__(None, None, None)
            except Exception as close_exc:
                logger.warning(
                    "Pipeline telemetry span failed to close: span=%s error_type=%s attrs=%s",
                    name,
                    type(close_exc).__name__,
                    span_attrs,
                )

    def pipeline_run_span(self, *, total_steps: int):
        return self._span(
            Spans.PIPELINE_RUN,
            self.span_attrs(
                span_kind=GenAiSpanKind.ENTRY,
                operation_name=GenAiOperationName.ENTER,
                total_steps=total_steps,
            ),
        )

    def step_span(
        self,
        *,
        step_id: str,
        step_index: int,
        total_steps: int,
        step_attempt: int | None = None,
        step_type: str | None = None,
    ):
        return self._span(
            Spans.PIPELINE_STEP,
            self.span_attrs(
                span_kind=GenAiSpanKind.STEP,
                operation_name=GenAiOperationName.REACT,
                react_round=step_index,
                step_id=step_id,
                step_index=step_index,
                step_attempt=step_attempt,
                total_steps=total_steps,
                step_type=step_type,
            ),
        )

    def sub_pipeline_span(self, **attrs: Any):
        return self._span(
            Spans.PIPELINE_SUB_PIPELINE,
            self.span_attrs(
                span_kind=GenAiSpanKind.CHAIN,
                operation_name=GenAiOperationName.INVOKE_AGENT,
                **attrs,
            ),
        )

    def sub_step_span(self, **attrs: Any):
        react_round = attrs.get("sub_step_index")
        if not isinstance(react_round, int):
            react_round = None
        return self._span(
            Spans.PIPELINE_SUB_STEP,
            self.span_attrs(
                span_kind=GenAiSpanKind.STEP,
                operation_name=GenAiOperationName.REACT,
                react_round=react_round,
                **attrs,
            ),
        )

    def pipeline_started(self, *, total_steps: int, step_names: list[str]) -> None:
        self.pipeline_started_at = self.now()
        self._event(Events.PIPELINE_STARTED, self.base_attrs(total_steps=total_steps, step_names=step_names))

    def pipeline_resumed(self, **attrs: Any) -> None:
        self.pipeline_started_at = self.now()
        self._event(Events.PIPELINE_RESUMED, self.base_attrs(**attrs))

    def pipeline_completed(self, *, total_steps: int, failed: bool = False, early_exit: bool = False) -> None:
        attrs = self.base_attrs(total_steps=total_steps, failed=failed, early_exit=early_exit)
        if self.pipeline_started_at is not None:
            duration = self.duration_ms(self.pipeline_started_at)
            attrs["duration_ms"] = duration
            self._metric(
                Metrics.PIPELINE_COMPLETION_TIME,
                duration,
                self.metric_attrs(status="failed" if failed else "completed"),
            )
        self._event(Events.PIPELINE_COMPLETED, attrs)

    def pipeline_user_aborted(
        self,
        *,
        total_steps: int | None = None,
        current_step: str | None = None,
        reason: str | None = None,
    ) -> None:
        attrs = self.base_attrs(total_steps=total_steps, current_step=current_step, reason=reason)
        if self.pipeline_started_at is not None:
            duration = self.duration_ms(self.pipeline_started_at)
            attrs["duration_ms"] = duration
            self._metric(
                Metrics.PIPELINE_COMPLETION_TIME,
                duration,
                self.metric_attrs(status="user_aborted"),
            )
        self._event(Events.PIPELINE_USER_ABORTED, attrs)

    def step_started(self, **attrs: Any) -> None:
        self._event(Events.PIPELINE_STEP_STARTED, self.base_attrs(**attrs))

    def step_completed(self, *, step_id: str, duration_ms: float, **attrs: Any) -> None:
        data = self.base_attrs(step_id=step_id, duration_ms=duration_ms, status="completed", **attrs)
        self._event(Events.PIPELINE_STEP_COMPLETED, data)
        self._metric(
            Metrics.PIPELINE_STEP_DURATION,
            duration_ms,
            self.metric_attrs(step_id=step_id, status="completed", **attrs),
        )

    def step_failed(self, *, step_id: str, duration_ms: float | None = None, **attrs: Any) -> None:
        data = self.base_attrs(step_id=step_id, duration_ms=duration_ms, status="failed", **attrs)
        self._event(Events.PIPELINE_STEP_FAILED, data)
        if duration_ms is not None:
            self._metric(
                Metrics.PIPELINE_STEP_DURATION,
                duration_ms,
                self.metric_attrs(step_id=step_id, status="failed", **attrs),
            )

    def rollback(self, **attrs: Any) -> None:
        self._event(Events.PIPELINE_ROLLBACK, self.base_attrs(**attrs))
        self._metric(Metrics.PIPELINE_ROLLBACK_COUNT, 1, self.metric_attrs(**attrs))

    def hard_interrupt(self, **attrs: Any) -> None:
        self._event(Events.PIPELINE_HARD_INTERRUPT, self.base_attrs(**attrs))

    def sidecar_failed(self, **attrs: Any) -> None:
        self._event(Events.PIPELINE_SIDECAR_FAILED, self.base_attrs(**attrs))

    def sub_pipeline_started(self, **attrs: Any) -> None:
        self._event(Events.PIPELINE_SUB_PIPELINE_STARTED, self.base_attrs(**attrs))

    def sub_pipeline_completed(self, *, duration_ms: float, failed: bool = False, **attrs: Any) -> None:
        event_name = Events.PIPELINE_SUB_PIPELINE_FAILED if failed else Events.PIPELINE_SUB_PIPELINE_COMPLETED
        data = self.base_attrs(duration_ms=duration_ms, failed=failed, **attrs)
        self._event(event_name, data)
        self._metric(
            Metrics.PIPELINE_SUB_PIPELINE_DURATION,
            duration_ms,
            self.metric_attrs(status="failed" if failed else "completed", **attrs),
        )

    def sub_step_started(self, **attrs: Any) -> None:
        self._event(Events.PIPELINE_SUB_STEP_STARTED, self.base_attrs(**attrs))

    def sub_step_completed(self, *, duration_ms: float, failed: bool = False, **attrs: Any) -> None:
        event_name = Events.PIPELINE_SUB_STEP_FAILED if failed else Events.PIPELINE_SUB_STEP_COMPLETED
        data = self.base_attrs(duration_ms=duration_ms, failed=failed, **attrs)
        self._event(event_name, data)
        self._metric(
            Metrics.PIPELINE_SUB_STEP_DURATION,
            duration_ms,
            self.metric_attrs(status="failed" if failed else "completed", **attrs),
        )

    def candidate_cancelled(self, **attrs: Any) -> None:
        self._event(Events.PIPELINE_CANDIDATE_CANCELLED, self.base_attrs(**attrs))
        self._metric(Metrics.PIPELINE_CANDIDATE_CANCELLED_COUNT, 1, self.metric_attrs(**attrs))

    def step_nudged(self, **attrs: Any) -> None:
        self._event(Events.PIPELINE_STEP_NUDGED, self.base_attrs(**attrs))

    def user_input_required(
        self,
        *,
        step_id: str,
        step_index: int,
        step_attempt: int | None = None,
        total_steps: int,
        step_type: str | None = None,
        ui_mode: str | None = None,
        option_count: int | None = None,
        prompt: str | None = None,
    ) -> None:
        self._event(
            Events.PIPELINE_USER_INPUT_REQUIRED,
            self.base_attrs(
                step_id=step_id,
                step_index=step_index,
                step_attempt=step_attempt,
                total_steps=total_steps,
                step_type=step_type,
                ui_mode=ui_mode,
                option_count=option_count,
                prompt=prompt,
            ),
        )

    def user_input_received(
        self,
        *,
        step_id: str,
        step_index: int,
        step_attempt: int | None = None,
        total_steps: int,
        ui_mode: str | None = None,
        user_input: str | None = None,
        wait_duration_ms: float | None = None,
    ) -> None:
        input_length_bucket = self.length_bucket(user_input)
        attrs = self.base_attrs(
            step_id=step_id,
            step_index=step_index,
            step_attempt=step_attempt,
            total_steps=total_steps,
            ui_mode=ui_mode,
            input_length_bucket=input_length_bucket,
            wait_duration_ms=wait_duration_ms,
        )
        self._event(Events.PIPELINE_USER_INPUT_RECEIVED, attrs)
        if wait_duration_ms is not None:
            self._metric(
                Metrics.PIPELINE_USER_INPUT_WAIT_DURATION,
                wait_duration_ms,
                self.metric_attrs(
                    step_id=step_id,
                    step_index=step_index,
                    step_attempt=step_attempt,
                    total_steps=total_steps,
                    ui_mode=ui_mode,
                    input_length_bucket=input_length_bucket,
                ),
            )

    def selection_made(
        self,
        *,
        step_id: str,
        step_attempt: int | None = None,
        ui_mode: str | None = None,
        option_count: int | None = None,
        selected_index: int | None = None,
        selected_value: str | None = None,
    ) -> None:
        self._event(
            Events.PIPELINE_SELECTION_MADE,
            self.base_attrs(
                step_id=step_id,
                step_attempt=step_attempt,
                ui_mode=ui_mode,
                option_count=option_count,
                selected_index=selected_index,
                selected_value=selected_value,
            ),
        )

    def candidates_evaluated(
        self,
        *,
        parent_step_id: str,
        sub_pipeline_name: str,
        candidate_count: int,
        candidate_success_count: int,
        candidate_failed_count: int,
    ) -> None:
        all_failed = candidate_count > 0 and candidate_success_count == 0
        candidate_count_bucket = self.count_bucket(candidate_count)
        attrs = self.base_attrs(
            parent_step_id=parent_step_id,
            sub_pipeline_name=sub_pipeline_name,
            candidate_count=candidate_count,
            candidate_success_count=candidate_success_count,
            candidate_failed_count=candidate_failed_count,
            all_failed=all_failed,
        )
        self._event(Events.PIPELINE_CANDIDATES_EVALUATED, attrs)
        metric_attrs = self.metric_attrs(
            parent_step_id=parent_step_id,
            sub_pipeline_name=sub_pipeline_name,
            candidate_count_bucket=candidate_count_bucket,
            all_failed=all_failed,
        )
        self._metric(Metrics.PIPELINE_CANDIDATE_COUNT, candidate_count, metric_attrs)
        self._metric(Metrics.PIPELINE_CANDIDATE_SUCCESS_COUNT, candidate_success_count, metric_attrs)
        self._metric(Metrics.PIPELINE_CANDIDATE_FAILED_COUNT, candidate_failed_count, metric_attrs)

    def funnel_step(
        self,
        *,
        step_id: str,
        step_index: int,
        step_attempt: int | None = None,
        total_steps: int,
        status: str,
        step_type: str | None = None,
        ui_mode: str | None = None,
        duration_ms: float | None = None,
    ) -> None:
        attrs = self.base_attrs(
            step_id=step_id,
            step_index=step_index,
            step_attempt=step_attempt,
            total_steps=total_steps,
            status=status,
            step_type=step_type,
            ui_mode=ui_mode,
            duration_ms=duration_ms,
        )
        self._event(Events.PIPELINE_FUNNEL_STEP, attrs)
        self._metric(
            Metrics.PIPELINE_FUNNEL_STEP_COUNT,
            1,
            self.metric_attrs(
                step_id=step_id,
                step_index=step_index,
                step_attempt=step_attempt,
                total_steps=total_steps,
                status=status,
                step_type=step_type,
                ui_mode=ui_mode,
            ),
        )
