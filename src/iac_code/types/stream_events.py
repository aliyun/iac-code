"""Fine-grained streaming event types for Provider -> AgentLoop -> Renderer pipeline.

Replaces the old coarse-grained events (TextChunkEvent, ThinkingEvent, etc.).
These events flow from Provider through AgentLoop to Renderer unchanged.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Literal, Union

TOOL_RENDER_METADATA_KEY = "_iac_code_tool_render"
TOOL_RENDER_DISPLAY_NAME_KEY = "display_name"
TOOL_RENDER_RESULT_COMPACT_KEY = "result_compact"
TOOL_RENDER_RESULT_VERBOSE_KEY = "result_verbose"
TOOL_RENDER_VERBOSE_RESULT_IN_TRANSCRIPT_KEY = "render_verbose_result_in_transcript"


@dataclass
class Usage:
    """Provider token usage with enough metadata for normalized reporting."""

    provider: str | None = None
    model: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0
    input_tokens_include_cache: bool = True
    reported: bool = False

    @property
    def usage_reported(self) -> bool:
        return self.reported or any(
            (
                self.input_tokens,
                self.output_tokens,
                self.cache_creation_input_tokens,
                self.cache_read_input_tokens,
            )
        )

    @property
    def total_input_tokens(self) -> int:
        if self.input_tokens_include_cache:
            return self.input_tokens
        return self.input_tokens + self.cache_creation_input_tokens + self.cache_read_input_tokens

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens + self.cache_creation_input_tokens + self.cache_read_input_tokens

    @property
    def normalized_total_tokens(self) -> int:
        """Total usage normalized across inclusive and separate cache counters."""
        return self.total_input_tokens + self.output_tokens

    @property
    def standard_input_tokens(self) -> int:
        if not self.input_tokens_include_cache:
            return self.input_tokens
        return max(0, self.input_tokens - self.cache_creation_input_tokens - self.cache_read_input_tokens)

    @property
    def cache_hit_rate(self) -> float:
        total_input_tokens = self.total_input_tokens
        if total_input_tokens <= 0:
            return 0.0
        return min(1.0, self.cache_read_input_tokens / total_input_tokens)


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
    block_index: int = field(default=0, kw_only=True)
    block_type: Literal["thinking", "redacted_thinking"] = field(default="thinking", kw_only=True)
    provider_metadata: dict[str, Any] | None = field(default=None, kw_only=True)

    @property
    def is_metadata_only(self) -> bool:
        """Whether this event carries internal provider state without display text."""
        return not self.text and bool(self.provider_metadata)


@dataclass
class ToolUseStartEvent:
    """A tool call has started -- name is known, input not yet complete."""

    tool_use_id: str
    name: str
    metadata: dict[str, Any] | None = None
    type: Literal["tool_use_start"] = "tool_use_start"
    provider_metadata: dict[str, Any] | None = field(default=None, kw_only=True)


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
    provider_metadata: dict[str, Any] | None = field(default=None, kw_only=True)
    # Set when the provider could not parse the model's raw arguments. ``input``
    # is then ``{}`` and the tool must NOT run: the agent loop turns this into
    # the tool result so the model sees the real defect instead of a schema
    # error about arguments it did send.
    input_error: str | None = field(default=None, kw_only=True)


@dataclass
class MessageEndEvent:
    """The assistant message is complete."""

    stop_reason: str
    usage: Usage
    type: Literal["message_end"] = "message_end"


@dataclass
class ContextUsageEvent:
    """Cumulative context-window usage snapshot for the loop that just finished a
    model round-trip. Emitted after ``MessageEndEvent`` so the web UI can show a
    live per-(pipeline-step) context-usage ring. ``usage`` is the dict returned by
    ``ContextManager.get_usage()`` (snake_case keys)."""

    usage: dict[str, Any]
    type: Literal["context_usage"] = "context_usage"


@dataclass
class TombstoneEvent:
    """Mark a previously-yielded message as orphaned (should be removed from UI/transcript)."""

    message_id: str
    affected_tool_use_ids: list[str] = field(default_factory=list)
    type: Literal["tombstone"] = "tombstone"


@dataclass
class ErrorEvent:
    """An error occurred during streaming."""

    error: str
    is_retryable: bool
    error_id: str | None = None
    type: Literal["error"] = "error"


# -- AgentLoop-originated events (consumed by Renderer) ------------------------


@dataclass
class ToolResultEvent:
    """A tool has finished executing -- result available."""

    tool_use_id: str
    tool_name: str
    result: str
    is_error: bool = False
    metadata: dict[str, Any] | None = None
    public_path_roots: list[dict[str, str]] | None = None
    type: Literal["tool_result"] = "tool_result"


class PermissionWaitOutcome(str, Enum):
    """Internal outcomes that must not be projected as a user decision."""

    SUSPEND = "suspend"
    AUTOMATIC_DENY = "automatic_deny"


class PermissionWaitSuspended(RuntimeError):  # noqa: N818 - domain event, not an error outcome
    """The live permission owner was durably suspended without a decision."""

    def __init__(self, boundary_id: str | None = None) -> None:
        self.boundary_id = boundary_id
        super().__init__("permission wait suspended")


@dataclass
class PermissionRequestEvent:
    """Tool execution requires user permission."""

    tool_name: str
    tool_input: dict[str, Any]
    tool_use_id: str
    response_future: asyncio.Future[bool | PermissionWaitOutcome] | None = field(default=None)
    permission_result: Any | None = field(default=None)
    audit_context: Any | None = field(default=None, repr=False, compare=False)
    resolution_owner_managed: bool = field(default=False, repr=False, compare=False)
    continuation_frame: dict[str, Any] | None = field(default=None, repr=False, compare=False)
    boundary_id: str | None = field(default=None, repr=False, compare=False)
    permission_wait_class: Literal["normal", "pipeline", "sub_pipeline"] | None = field(
        default=None,
        repr=False,
        compare=False,
    )
    permission_wait_coordinates: dict[str, Any] | None = field(default=None, repr=False, compare=False)
    permission_decision_audited: bool = field(default=False, repr=False, compare=False)
    type: Literal["permission_request"] = "permission_request"


@dataclass
class CompactionEvent:
    """Context auto-compaction lifecycle signal.

    ``phase`` distinguishes the pre-compaction "started" marker (shown as the
    running indicator in the UI), the successful "finished" result that carries
    token counts, and a terminal "failed" result that stops the indicator
    without claiming a compaction occurred.
    """

    original_tokens: int = 0
    compacted_tokens: int = 0
    summary: str = ""
    phase: Literal["started", "finished", "failed"] = "finished"
    reason: str = ""
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
class QueuedInputSubmittedEvent:
    """A user prompt queued during streaming was submitted mid-turn."""

    text: str
    message_id: str | None = None
    type: Literal["queued_input_submitted"] = "queued_input_submitted"


@dataclass
class SubAgentToolEvent:
    """A sub-agent's internal tool activity — forwarded to parent Renderer."""

    parent_tool_use_id: str  # The parent AgentTool's tool_use_id
    child_tool_name: str  # Tool name the sub-agent called
    child_tool_input: dict  # Tool input params
    is_done: bool = False  # Whether this child tool finished
    is_error: bool = False
    type: Literal["subagent_tool"] = "subagent_tool"


class ToolEmittedEvent:
    """Marker base class for events emitted by tool execution.

    Subclasses (StackProgressEvent, StackInstancesProgressEvent, DiagramEvent,
    CandidateDetailEvent) inherit from this so AgentLoop can dispatch
    tool-emitted events to the event_queue polymorphically via
    `isinstance(item, ToolEmittedEvent)` checks (see agent_loop.py).

    Do not remove — this class is intentionally minimal.
    """

    pass


@dataclass
class ResourceObservedEvent(ToolEmittedEvent):
    """A cloud resource id became known before the lifecycle tool completed."""

    provider: str
    resource_type: str
    resource_id: str
    resource_name: str = ""
    region_id: str = ""
    action: str = ""
    tool_name: str = ""
    tool_use_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    type: Literal["resource_observed"] = "resource_observed"


@dataclass
class StackOperationStartedEvent(ToolEmittedEvent):
    """A stack lifecycle operation has started, before its first poll.

    Web-only t0 signal so the output panel shows ``*_IN_PROGRESS`` immediately for
    non-create actions (delete/update/continue), instead of waiting for the first
    poll (~POLL_INTERVAL). Deliberately separate from ResourceObservedEvent: the a2a
    translator (which turns ResourceObservedEvent into ``stack_current_changed``) does
    not recognize this type and ignores it, so current-stack semantics stay untouched.
    """

    provider: str
    stack_id: str
    stack_name: str = ""
    region_id: str = ""
    action: str = ""
    tool_name: str = ""
    tool_use_id: str | None = None
    type: Literal["stack_operation_started"] = "stack_operation_started"


@dataclass
class StackProgressEvent(ToolEmittedEvent):
    """Real-time progress from a stack lifecycle operation."""

    stack_id: str
    stack_name: str
    status: str
    progress_percentage: float
    resources: list[dict[str, Any]]
    elapsed_seconds: int
    # 栈所在 region。轮询返回的 resources 只有 name/type/status,不含 region 字段,
    # web 桥接无法从中推断 region;这里带上权威 region,使 live overlay 的去重键
    # `region::name` 与服务端派生栈一致,建栈期不再出现同名双栈。
    region_id: str = ""
    # 让 web 前端把进度关联到发起该栈操作的工具卡(与 ResourceObservedEvent/MCPProgressEvent 一致)。
    tool_use_id: str | None = None
    type: Literal["stack_progress"] = "stack_progress"


@dataclass
class StackInstancesProgressEvent(ToolEmittedEvent):
    """Real-time progress from a StackGroup instances operation."""

    stack_group_name: str
    operation_id: str
    status: str
    progress_percentage: int
    instances: list[dict[str, Any]]
    elapsed_seconds: int
    # 让 web 前端把进度关联到发起该栈组操作的工具卡(与 ResourceObservedEvent/MCPProgressEvent 一致)。
    tool_use_id: str | None = None
    type: Literal["stack_instances_progress"] = "stack_instances_progress"


@dataclass
class MCPProgressEvent(ToolEmittedEvent):
    """Real-time progress emitted by an MCP tool call."""

    server_name: str
    tool_name: str
    progress: float | None = None
    total: float | None = None
    message: str | None = None
    tool_use_id: str | None = None
    public_name: str | None = None
    type: Literal["mcp_progress"] = "mcp_progress"


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


@dataclass
class SubPipelineStreamEvent:
    """Wraps a StreamEvent to route it to a specific sub-pipeline candidate's tab."""

    sub_pipeline_id: str
    candidate_index: int
    inner: "StreamEvent"
    type: Literal["sub_pipeline_stream"] = "sub_pipeline_stream"


@dataclass
class DiagramEvent(ToolEmittedEvent):
    """Architecture diagram for rendering by the frontend."""

    candidate_name: str
    template_content: str
    mermaid_source: str
    candidate_index: int | None = None
    architecture_context: dict[str, Any] | None = None
    diagram_stage: Literal["draft", "optimized"] = "optimized"
    views: list[dict[str, str]] = field(default_factory=list)
    candidate_set_id: str | None = None
    detail_stage: Literal["outline", "detail"] | None = None
    type: Literal["diagram"] = "diagram"


@dataclass
class CandidateDetailEvent(ToolEmittedEvent):
    """Structured candidate detail for rendering in the selection UI."""

    tool_use_id: str  # U-I14: distinguish multiple tool calls in same parallel step
    candidate_name: str
    summary: str
    cost_items: list[dict]
    total_monthly_cost: str
    candidate_index: int | None = None
    candidate_set_id: str | None = None
    detail_stage: Literal["outline", "detail"] | None = None
    key_tradeoff: str | None = None
    type: Literal["candidate_detail"] = "candidate_detail"


@dataclass
class AskUserQuestionEvent(ToolEmittedEvent):
    """A tool-emitted prompt that asks the user to choose an option or type details."""

    tool_use_id: str
    question: str
    options: list[dict[str, Any]]
    allow_free_text: bool = True
    free_text_prompt: str = ""
    response_future: asyncio.Future[dict[str, str] | None] | None = field(default=None)
    type: Literal["ask_user_question"] = "ask_user_question"


StreamEvent = Union[
    MessageStartEvent,
    TextDeltaEvent,
    ThinkingDeltaEvent,
    ToolUseStartEvent,
    ToolInputDeltaEvent,
    ToolUseEndEvent,
    MessageEndEvent,
    ContextUsageEvent,
    TombstoneEvent,
    ErrorEvent,
    ToolResultEvent,
    PermissionRequestEvent,
    CompactionEvent,
    TaskNotificationEvent,
    QueuedInputSubmittedEvent,
    SubAgentToolEvent,
    ResourceObservedEvent,
    StackOperationStartedEvent,
    StackProgressEvent,
    StackInstancesProgressEvent,
    MCPProgressEvent,
    PlanEvent,
    SubPipelineStreamEvent,
    DiagramEvent,
    CandidateDetailEvent,
    AskUserQuestionEvent,
]
