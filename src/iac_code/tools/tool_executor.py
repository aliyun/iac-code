"""Concurrent tool execution engine with read/write partitioning and input validation."""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import TYPE_CHECKING

from iac_code.i18n import _
from iac_code.services.telemetry import add_metric, log_event, start_span
from iac_code.services.telemetry.config import should_capture_content_on_span
from iac_code.services.telemetry.content_serializer import serialize_tool_arguments, serialize_tool_result
from iac_code.services.telemetry.names import Events, GenAiAttr, GenAiOperationName, GenAiSpanKind, Metrics, Spans
from iac_code.services.telemetry.sanitize import sanitize_error_message, sanitize_tool_name
from iac_code.tools.base import ToolContext, ToolResult
from iac_code.types.permissions import ExecutionClass, InvocationBinding

if TYPE_CHECKING:
    from iac_code.tools.base import ToolRegistry

_MAX_REJECTED_INPUT_DIGESTS = 64


def _input_digest(tool_input: dict) -> str | None:
    """Stable digest of a tool input, or None when it cannot be serialized."""
    try:
        encoded = json.dumps(
            tool_input,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
            default=repr,
        ).encode("utf-8")
    except (TypeError, ValueError):
        return None
    return hashlib.sha256(encoded).hexdigest()


@dataclass
class ToolCallRequest:
    id: str
    name: str
    input: dict
    event_queue: asyncio.Queue | None = None
    invocation_binding: InvocationBinding | None = None
    snapshot_id: str | None = None
    security_digest: str | None = None
    execution_class: ExecutionClass | None = None


class ToolExecutor:
    def __init__(
        self,
        registry: "ToolRegistry",
        max_concurrency: int = 10,
        tool_timeout: float = 120.0,
    ):
        self._registry = registry
        self._max_concurrency = max_concurrency
        self._tool_timeout = tool_timeout
        # Fingerprints of tool inputs already rejected by input validation, so a
        # model that resubmits byte-identical invalid arguments gets an escalated
        # error instead of the same text again. Bounded per executor.
        self._rejected_input_digests: OrderedDict[tuple[str, str], None] = OrderedDict()

    def _record_rejected_input(self, tool_name: str, tool_input: dict) -> bool:
        """Remember a rejected input. Returns True if this exact input was rejected before."""
        digest = _input_digest(tool_input)
        if digest is None:
            return False
        key = (tool_name, digest)
        if key in self._rejected_input_digests:
            self._rejected_input_digests.move_to_end(key)
            return True
        self._rejected_input_digests[key] = None
        while len(self._rejected_input_digests) > _MAX_REJECTED_INPUT_DIGESTS:
            self._rejected_input_digests.popitem(last=False)
        return False

    @staticmethod
    def _repeated_rejection_prefix(tool_name: str) -> str:
        return (
            _(
                "These exact '{tool_name}' arguments were already rejected by input validation. "
                "Resubmitting them will keep failing. Read the error below, then call '{tool_name}' "
                "again with corrected arguments that differ from the rejected ones."
            ).format(tool_name=tool_name)
            + "\n"
        )

    def partition(self, calls: list[ToolCallRequest]) -> tuple[list[ToolCallRequest], list[ToolCallRequest]]:
        """Partition calls into concurrent (read-only) and serial (write) batches."""
        concurrent, serial = [], []
        for call in calls:
            if self._is_concurrent(call):
                concurrent.append(call)
            else:
                serial.append(call)
        return concurrent, serial

    def _is_concurrent(self, call: ToolCallRequest) -> bool:
        tool = self._registry.get(call.name)
        if tool is None:
            return False
        if getattr(tool, "requires_runtime_execution_class", False):
            return call.execution_class == "concurrent"
        return tool.is_concurrency_safe(call.input)

    async def _validate_and_execute(self, call: ToolCallRequest, context: ToolContext) -> ToolResult:
        """Validate input then execute. Returns error ToolResult on validation failure."""
        tool = self._registry.get(call.name)
        if not tool:
            return ToolResult.error(_("Unknown tool: {tool_name}").format(tool_name=call.name))

        # Input validation
        valid, error = tool.validate_input(call.input)
        if not valid:
            repeated = self._record_rejected_input(call.name, call.input)
            tool_error = tool.validation_error_result(call.input)
            if tool_error is not None:
                if repeated:
                    return ToolResult.error(self._repeated_rejection_prefix(call.name) + tool_error.content)
                return tool_error
            message = _(
                "Invalid input for tool '{tool_name}': {error}. "
                "Please provide all required parameters as defined in the tool schema."
            ).format(tool_name=call.name, error=error)
            if repeated:
                message = self._repeated_rejection_prefix(call.name) + message
            return ToolResult.error(message)

        # Pass event_queue from call to context for tools that emit progress events.
        # Always derive a per-call ToolContext so that ``tool_use_id`` (U-I14) is
        # populated for the executing tool — needed so emitted events can be
        # attributed to the specific tool invocation that produced them.
        context = ToolContext(
            cwd=context.cwd,
            event_queue=call.event_queue if call.event_queue is not None else context.event_queue,
            additional_directories=list(context.additional_directories),
            trusted_read_directories=list(context.trusted_read_directories),
            relative_read_directories=list(context.relative_read_directories),
            strict_read_directories=list(context.strict_read_directories),
            read_path_violation_behavior=context.read_path_violation_behavior,
            tool_use_id=call.id,
            pipeline_mode=context.pipeline_mode,
            env_overrides=dict(context.env_overrides),
            telemetry_attributes=dict(context.telemetry_attributes),
            permission_context=context.permission_context,
            invocation_binding=call.invocation_binding,
            snapshot_id=call.snapshot_id,
            security_digest=call.security_digest,
            execution_class=call.execution_class,
            ros_preflight_outcome=None,
            trusted_ros_account_context=context.trusted_ros_account_context,
        )

        timeout = tool.execution_timeout(call.input)
        if timeout is None:
            timeout = self._tool_timeout

        # Telemetry instrumentation
        tool_name = sanitize_tool_name(call.name)
        started = time.monotonic()

        span_name = f"{Spans.TOOL_EXECUTE} {tool_name}"
        span_attrs: dict = {
            GenAiAttr.SPAN_KIND: GenAiSpanKind.TOOL,
            GenAiAttr.OPERATION_NAME: GenAiOperationName.EXECUTE_TOOL,
            GenAiAttr.TOOL_NAME: tool_name,
            GenAiAttr.TOOL_TYPE: "function",
            GenAiAttr.TOOL_CALL_ID: call.id,
        }
        if tool.description:
            span_attrs[GenAiAttr.TOOL_DESCRIPTION] = tool.description
        if should_capture_content_on_span():
            span_attrs[GenAiAttr.TOOL_CALL_ARGUMENTS] = serialize_tool_arguments(call.input, tool_name=call.name)

        try:
            with start_span(span_name, span_attrs) as span:
                result = await asyncio.wait_for(
                    tool.execute(tool_input=call.input, context=context),
                    timeout=timeout,
                )
                duration_ms = int((time.monotonic() - started) * 1000)
                if should_capture_content_on_span():
                    span.set_attribute(GenAiAttr.TOOL_CALL_RESULT, serialize_tool_result(result, tool_name=call.name))
                log_event(Events.TOOL_USE_SUCCEEDED, {"tool_name": tool_name, "duration_ms": duration_ms})
                add_metric(Metrics.TOOL_USE_COUNT, 1, {"tool_name": tool_name, "outcome": "success"})
                return result
        except asyncio.TimeoutError:
            log_event(
                Events.TOOL_USE_FAILED,
                {
                    "tool_name": tool_name,
                    "error_type": "TimeoutError",
                    "error_message": sanitize_error_message(f"Timeout after {timeout}s"),
                },
            )
            add_metric(Metrics.TOOL_USE_COUNT, 1, {"tool_name": tool_name, "outcome": "error"})
            timeout_error = tool.timeout_error_result_with_context(call.input, timeout, context)
            if timeout_error is not None:
                return self._attach_ros_preflight(timeout_error, context)
            return self._attach_ros_preflight(
                ToolResult.error(
                    _("Tool '{tool_name}' timed out after {timeout}s").format(tool_name=call.name, timeout=timeout)
                ),
                context,
            )
        except Exception as e:
            log_event(
                Events.TOOL_USE_FAILED,
                {
                    "tool_name": tool_name,
                    "error_type": type(e).__name__,
                    "error_message": sanitize_error_message(str(e)),
                },
            )
            add_metric(Metrics.TOOL_USE_COUNT, 1, {"tool_name": tool_name, "outcome": "error"})
            return self._attach_ros_preflight(
                ToolResult.error(_("Tool '{tool_name}' failed: {error}").format(tool_name=call.name, error=e)),
                context,
            )

    @staticmethod
    def _attach_ros_preflight(result: ToolResult, context: ToolContext) -> ToolResult:
        outcome = context.ros_preflight_outcome
        if outcome is None:
            return result
        from iac_code.tools.cloud.aliyun.ros_validation.outcome import attach_ros_validation

        return attach_ros_validation(result, outcome)

    async def _execute_concurrent(
        self, calls: list[ToolCallRequest], context: ToolContext
    ) -> list[tuple[str, ToolResult]]:
        if not calls:
            return []
        sem = asyncio.Semaphore(self._max_concurrency)

        async def run(call: ToolCallRequest) -> tuple[str, ToolResult]:
            async with sem:
                result = await self._validate_and_execute(call, context)
                return call.id, result

        tasks = [asyncio.create_task(run(c)) for c in calls]
        return list(await asyncio.gather(*tasks))

    async def _execute_serial(self, calls: list[ToolCallRequest], context: ToolContext) -> list[tuple[str, ToolResult]]:
        results = []
        for call in calls:
            result = await self._validate_and_execute(call, context)
            results.append((call.id, result))
        return results

    async def execute_batch(self, calls: list[ToolCallRequest], context: ToolContext) -> list[ToolResult]:
        """Execute tool calls with read/write partitioning.

        Consecutive concurrency-safe read-only calls run in parallel. Calls that
        are not concurrency-safe form ordering barriers: earlier reads finish
        before the write, and later reads start after it.
        """
        result_map: dict[str, ToolResult] = {}
        concurrent_batch: list[ToolCallRequest] = []

        async def flush_concurrent_batch() -> None:
            nonlocal concurrent_batch
            if not concurrent_batch:
                return
            for call_id, result in await self._execute_concurrent(concurrent_batch, context):
                result_map[call_id] = result
            concurrent_batch = []

        for call in calls:
            if self._is_concurrent(call):
                concurrent_batch.append(call)
                continue
            await flush_concurrent_batch()
            result = await self._validate_and_execute(call, context)
            result_map[call.id] = result

        await flush_concurrent_batch()
        return [result_map[call.id] for call in calls]
