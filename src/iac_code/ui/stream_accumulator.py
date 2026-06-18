"""StreamAccumulator — reusable event-to-segment processing pipeline.

Encapsulates the logic of converting StreamEvents into renderable segments
(_Segment, _ToolCallRecord). Used by both the main Renderer.run_streaming_output
and the parallel tab UI to avoid duplicating event handling logic.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from iac_code.types.stream_events import (
    StreamEvent,
    SubAgentToolEvent,
    TextDeltaEvent,
    ThinkingDeltaEvent,
    ToolInputDeltaEvent,
    ToolResultEvent,
    ToolUseEndEvent,
    ToolUseStartEvent,
)


@dataclass
class SubAgentChild:
    """A child tool call made by a sub-agent."""

    tool_name: str
    tool_input: dict
    is_done: bool = False
    is_error: bool = False


@dataclass
class ToolCallRecord:
    """One tool invocation (use + optional result)."""

    tool_name: str
    tool_input: dict
    partial_input: str = ""
    result: str | None = None
    is_error: bool = False
    done: bool = False
    children: list[SubAgentChild] | None = None
    start_time: float = 0.0
    progress_renderable: Any = None


@dataclass
class RenderSegment:
    """One segment of turn output — markdown text, a tool call, or a
    collapsed thinking-summary line."""

    kind: str  # "text" | "tool" | "thinking_summary"
    text: str = ""
    tool: ToolCallRecord | None = None
    elapsed_seconds: float = 0.0


class StreamAccumulator:
    """Accumulates StreamEvents into renderable segments.

    Handles: TextDelta, ThinkingDelta, ToolUseStart, ToolInputDelta,
    ToolUseEnd, ToolResult, SubAgentTool events.

    The caller is responsible for display lifecycle (Live start/stop,
    flushing to scrollback, etc.). This class only manages state.
    """

    def __init__(self) -> None:
        self.segments: list[RenderSegment] = []
        self.text_buffer: str = ""
        self.thinking_buffer: str = ""
        self.tool_records: dict[str, ToolCallRecord] = {}
        self._thinking_start_time: float | None = None

    @property
    def has_pending_tools(self) -> bool:
        return any(s.kind == "tool" and s.tool and not s.tool.done for s in self.segments)

    def process(self, event: StreamEvent) -> str:
        """Process one stream event, updating internal state.

        Returns an action hint:
          - "text": text_buffer updated
          - "thinking": thinking_buffer updated
          - "tool_start": new tool segment added (text was finalized)
          - "tool_update": existing tool record updated
          - "tool_done": tool result received, tool marked done
          - "sub_agent": sub-agent child tool activity
          - "finalize": message ended, text finalized into segment
          - "none": event not handled
        """
        if isinstance(event, TextDeltaEvent):
            self._finalize_thinking()
            self.text_buffer += event.text
            return "text"

        if isinstance(event, ThinkingDeltaEvent):
            if self._thinking_start_time is None:
                self._thinking_start_time = time.monotonic()
            self.thinking_buffer += event.text
            return "thinking"

        if isinstance(event, ToolUseStartEvent):
            self._finalize_thinking()
            if self.text_buffer:
                self.segments.append(RenderSegment(kind="text", text=self.text_buffer))
                self.text_buffer = ""
            rec = ToolCallRecord(
                tool_name=event.name,
                tool_input={},
                start_time=time.monotonic(),
            )
            self.tool_records[event.tool_use_id] = rec
            self.segments.append(RenderSegment(kind="tool", tool=rec))
            return "tool_start"

        if isinstance(event, ToolInputDeltaEvent):
            rec = self.tool_records.get(event.tool_use_id)
            if rec:
                rec.partial_input += event.partial_json
            return "tool_update"

        if isinstance(event, ToolUseEndEvent):
            rec = self.tool_records.get(event.tool_use_id)
            if rec:
                rec.tool_input = event.input
            return "tool_update"

        if isinstance(event, ToolResultEvent):
            rec = self.tool_records.get(event.tool_use_id)
            if rec is None and not event.tool_use_id:
                matches = [r for r in self.tool_records.values() if r.tool_name == event.tool_name and not r.done]
                if len(matches) == 1:
                    rec = matches[0]
            if rec:
                rec.result = event.result
                rec.is_error = event.is_error
                rec.done = True
            return "tool_done"

        if isinstance(event, SubAgentToolEvent):
            rec = self.tool_records.get(event.parent_tool_use_id)
            if rec:
                if rec.children is None:
                    rec.children = []
                if event.is_done:
                    for child in rec.children:
                        if child.tool_name == event.child_tool_name and not child.is_done:
                            child.is_done = True
                            child.is_error = event.is_error
                            break
                else:
                    rec.children.append(
                        SubAgentChild(
                            tool_name=event.child_tool_name,
                            tool_input=event.child_tool_input,
                        )
                    )
            return "sub_agent"

        return "none"

    def finalize_text(self) -> None:
        """Finalize any remaining text_buffer into a segment."""
        self._finalize_thinking()
        if self.text_buffer:
            self.segments.append(RenderSegment(kind="text", text=self.text_buffer))
            self.text_buffer = ""

    def _finalize_thinking(self) -> None:
        if self._thinking_start_time is not None and self.thinking_buffer.strip():
            elapsed = time.monotonic() - self._thinking_start_time
            self.segments.append(RenderSegment(kind="thinking_summary", elapsed_seconds=elapsed))
        self.thinking_buffer = ""
        self._thinking_start_time = None

    def completed_segments(self) -> list[RenderSegment]:
        """Return segments where all tools are done (safe to flush)."""
        return [s for s in self.segments if s.kind == "text" or (s.kind == "tool" and s.tool and s.tool.done)]
