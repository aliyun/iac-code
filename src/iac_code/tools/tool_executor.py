"""Concurrent tool execution engine with read/write partitioning and input validation."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from iac_code.services.telemetry import add_metric, log_event, start_span
from iac_code.services.telemetry.config import should_capture_content_on_span
from iac_code.services.telemetry.content_serializer import serialize_tool_arguments, serialize_tool_result
from iac_code.services.telemetry.names import Events, GenAiAttr, GenAiOperationName, GenAiSpanKind, Metrics, Spans
from iac_code.services.telemetry.sanitize import sanitize_error_message, sanitize_tool_name
from iac_code.tools.base import ToolContext, ToolResult

if TYPE_CHECKING:
    from iac_code.tools.base import ToolRegistry


@dataclass
class ToolCallRequest:
    id: str
    name: str
    input: dict
    event_queue: asyncio.Queue | None = None


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

    def partition(self, calls: list[ToolCallRequest]) -> tuple[list[ToolCallRequest], list[ToolCallRequest]]:
        """Partition calls into concurrent (read-only) and serial (write) batches."""
        concurrent, serial = [], []
        for call in calls:
            tool = self._registry.get(call.name)
            if tool and tool.is_concurrency_safe(call.input):
                concurrent.append(call)
            else:
                serial.append(call)
        return concurrent, serial

    async def _validate_and_execute(self, call: ToolCallRequest, context: ToolContext) -> ToolResult:
        """Validate input then execute. Returns error ToolResult on validation failure."""
        tool = self._registry.get(call.name)
        if not tool:
            return ToolResult.error(f"Unknown tool: {call.name}")

        # Input validation
        valid, error = tool.validate_input(call.input)
        if not valid:
            return ToolResult.error(
                f"Invalid input for tool '{call.name}': {error}. "
                f"Please provide all required parameters as defined in the tool schema."
            )

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
            tool_use_id=call.id,
        )

        timeout = tool.timeout if tool.timeout is not None else self._tool_timeout

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
            span_attrs[GenAiAttr.TOOL_CALL_ARGUMENTS] = serialize_tool_arguments(call.input)

        try:
            with start_span(span_name, span_attrs) as span:
                result = await asyncio.wait_for(
                    tool.execute(tool_input=call.input, context=context),
                    timeout=timeout,
                )
                duration_ms = int((time.monotonic() - started) * 1000)
                if should_capture_content_on_span():
                    span.set_attribute(GenAiAttr.TOOL_CALL_RESULT, serialize_tool_result(result))
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
            return ToolResult.error(f"Tool '{call.name}' timed out after {timeout}s")
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
            return ToolResult.error(f"Tool '{call.name}' failed: {e}")

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

        1. Partition into concurrent (read-only) and serial (write) batches
        2. Execute concurrent batch in parallel (up to max_concurrency)
        3. Execute serial batch sequentially
        4. Return results in original request order
        """
        concurrent, serial = self.partition(calls)
        concurrent_results = await self._execute_concurrent(concurrent, context)
        serial_results = await self._execute_serial(serial, context)
        result_map = {call_id: result for call_id, result in concurrent_results + serial_results}
        return [result_map[call.id] for call in calls]
