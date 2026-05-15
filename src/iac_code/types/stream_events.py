"""Fine-grained streaming event types for Provider -> AgentLoop -> Renderer pipeline.

Replaces the old coarse-grained events (TextChunkEvent, ThinkingEvent, etc.).
These events flow from Provider through AgentLoop to Renderer unchanged.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Literal, Union


@dataclass
class Usage:
    """Token usage from an API response."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens + self.cache_creation_input_tokens + self.cache_read_input_tokens


# -- Provider-originated events ------------------------------------------------


@dataclass
class MessageStartEvent:
    """A new assistant message has started."""

    message_id: str
    type: Literal["message_start"] = "message_start"


@dataclass
class TextDeltaEvent:
    """Incremental text content from the model."""

    text: str
    type: Literal["text_delta"] = "text_delta"


@dataclass
class ThinkingDeltaEvent:
    """Incremental thinking/reasoning content."""

    text: str
    type: Literal["thinking_delta"] = "thinking_delta"


@dataclass
class ToolUseStartEvent:
    """A tool call has started -- name is known, input not yet complete."""

    tool_use_id: str
    name: str
    type: Literal["tool_use_start"] = "tool_use_start"


@dataclass
class ToolInputDeltaEvent:
    """Incremental JSON input for a tool call."""

    tool_use_id: str
    partial_json: str
    type: Literal["tool_input_delta"] = "tool_input_delta"


@dataclass
class ToolUseEndEvent:
    """Tool call input is complete."""

    tool_use_id: str
    name: str
    input: dict[str, Any]
    type: Literal["tool_use_end"] = "tool_use_end"


@dataclass
class MessageEndEvent:
    """The assistant message is complete."""

    stop_reason: str
    usage: Usage
    type: Literal["message_end"] = "message_end"


@dataclass
class TombstoneEvent:
    """Mark a previously-yielded message as orphaned (should be removed from UI/transcript)."""

    message_id: str
    type: Literal["tombstone"] = "tombstone"


@dataclass
class ErrorEvent:
    """An error occurred during streaming."""

    error: str
    is_retryable: bool
    type: Literal["error"] = "error"


# -- AgentLoop-originated events (consumed by Renderer) ------------------------


@dataclass
class ToolResultEvent:
    """A tool has finished executing -- result available."""

    tool_use_id: str
    tool_name: str
    result: str
    is_error: bool = False
    type: Literal["tool_result"] = "tool_result"


@dataclass
class PermissionRequestEvent:
    """Tool execution requires user permission."""

    tool_name: str
    tool_input: dict[str, Any]
    tool_use_id: str
    response_future: asyncio.Future[bool] | None = field(default=None)
    type: Literal["permission_request"] = "permission_request"


@dataclass
class CompactionEvent:
    """Context auto-compaction occurred."""

    original_tokens: int = 0
    compacted_tokens: int = 0
    type: Literal["compaction"] = "compaction"


@dataclass
class TaskNotificationEvent:
    """A background agent task has completed/failed/stopped."""

    task_id: str
    description: str
    status: str  # "completed" | "failed" | "stopped"
    result: str | None = None
    error: str | None = None
    type: Literal["task_notification"] = "task_notification"


@dataclass
class SubAgentToolEvent:
    """A sub-agent's internal tool activity — forwarded to parent Renderer."""

    parent_tool_use_id: str  # The parent AgentTool's tool_use_id
    child_tool_name: str  # Tool name the sub-agent called
    child_tool_input: dict  # Tool input params
    is_done: bool = False  # Whether this child tool finished
    is_error: bool = False
    type: Literal["subagent_tool"] = "subagent_tool"


@dataclass
class StackProgressEvent:
    """Real-time progress from a stack lifecycle operation."""

    stack_id: str
    stack_name: str
    status: str
    progress_percentage: float
    resources: list[dict[str, Any]]
    elapsed_seconds: int
    type: Literal["stack_progress"] = "stack_progress"


@dataclass
class StackInstancesProgressEvent:
    """Real-time progress from a StackGroup instances operation."""

    stack_group_name: str
    operation_id: str
    status: str
    progress_percentage: int
    instances: list[dict[str, Any]]
    elapsed_seconds: int
    type: Literal["stack_instances_progress"] = "stack_instances_progress"


@dataclass
class PlanStep:
    """A single step in an agent plan."""

    content: str
    status: Literal["pending", "in_progress", "completed"] = "pending"
    priority: Literal["high", "medium", "low"] = "medium"


@dataclass
class PlanEvent:
    """Agent plan creation or update."""

    steps: list[PlanStep]
    type: Literal["plan"] = "plan"


StreamEvent = Union[
    MessageStartEvent,
    TextDeltaEvent,
    ThinkingDeltaEvent,
    ToolUseStartEvent,
    ToolInputDeltaEvent,
    ToolUseEndEvent,
    MessageEndEvent,
    TombstoneEvent,
    ErrorEvent,
    ToolResultEvent,
    PermissionRequestEvent,
    CompactionEvent,
    TaskNotificationEvent,
    SubAgentToolEvent,
    StackProgressEvent,
    StackInstancesProgressEvent,
    PlanEvent,
]
