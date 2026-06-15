"""Base classes for the tool system."""

from __future__ import annotations

import asyncio
import os
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from iac_code.i18n import _

if TYPE_CHECKING:
    from iac_code.types.permissions import PermissionResult


@dataclass
class ToolContext:
    """Execution context passed to tools."""

    cwd: str = field(default_factory=os.getcwd)
    event_queue: asyncio.Queue | None = None
    # U-I14: tool_use_id of the current tool invocation; lets tools tag emitted
    # events so multiple parallel calls of the same tool can be distinguished
    # downstream (e.g. in the renderer's per-tab accumulator). Populated by the
    # ToolExecutor on each call.
    tool_use_id: str | None = None


@dataclass
class ToolResult:
    """Result returned from tool execution.

    Extended to support context modification:
    - new_messages: Additional messages to inject into conversation context.
    - context_modifier: Callback to modify the execution context
      (e.g., add tool permissions, override model, override effort).
    """

    content: str
    is_error: bool = False
    new_messages: list[dict[str, Any]] = field(default_factory=list)
    context_modifier: Callable[[dict], dict] | None = None
    metadata: dict[str, Any] | None = None

    @staticmethod
    def error(message: str) -> ToolResult:
        """Create an error result."""
        return ToolResult(content=message, is_error=True)

    @staticmethod
    def success(content: str) -> ToolResult:
        """Create a success result."""
        return ToolResult(content=content, is_error=False)


class Tool(ABC):
    """Abstract base class for all tools.

    Important for third-party tool authors: if your tool emits events to the
    UI (StackProgressEvent, DiagramEvent, etc.), you MUST override
    needs_event_queue() to return True. Otherwise the AgentLoop will not
    pass an event_queue and your event emissions will silently noop.
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """The unique name of the tool."""
        ...

    @property
    @abstractmethod
    def description(self) -> str:
        """A description of what the tool does."""
        ...

    @property
    @abstractmethod
    def input_schema(self) -> dict[str, Any]:
        """JSON Schema defining the tool's input parameters."""
        ...

    @abstractmethod
    async def execute(self, *, tool_input: dict[str, Any], context: ToolContext) -> ToolResult:
        """Execute the tool with the given input.

        Args:
            tool_input: The input parameters as defined by input_schema.
            context: The execution context.

        Returns:
            The result of the tool execution.
        """
        ...

    def normalize_input(self, tool_input: dict[str, Any]) -> None:
        """Normalize tool input in-place before validation.

        Override in subclasses to handle parameter aliases.
        """

    def validate_input(self, tool_input: dict[str, Any]) -> tuple[bool, str]:
        """Validate tool_input against input_schema using JSON Schema.

        Returns:
            (True, "") if valid, (False, error_message) if invalid.
        """
        self.normalize_input(tool_input)
        try:
            import jsonschema

            jsonschema.validate(instance=tool_input, schema=self.input_schema)
            return True, ""
        except jsonschema.ValidationError as e:
            return False, str(e.message)

    def to_api_format(self) -> dict[str, Any]:
        """Convert tool definition to LLM API format (legacy OpenAI format)."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.input_schema,
            },
        }

    # --- UI rendering methods ---
    def render_tool_use_message(self, input: dict, *, verbose: bool = False) -> str | None:
        """Return detail text shown in parentheses after the tool name.

        Displayed as ``● ToolName(detail)`` in the UI.  Use this to show
        the key parameters of the tool invocation, e.g. file path, search
        pattern, or URL.
        """
        return None

    def render_tool_result_message(self, output: str, *, is_error: bool = False, verbose: bool = False) -> str | None:
        """Return result summary shown after ``⎿`` prefix.

        In non-verbose mode, return a short one-line summary (e.g.
        "Found 5 files").  In verbose mode, include more detail.
        """
        return None

    def render_tool_use_error_message(self, error: str) -> str | None:
        """Return error text for display."""
        return None

    # --- Display info ---
    def user_facing_name(self, input: dict | None = None) -> str:
        """Short, human-readable tool name shown as the bold label in the UI.

        Should be a concise noun like "Read", "Search", or "Bash".
        Do NOT include parameter details here — use
        ``render_tool_use_message`` for that.  This name is also used in
        permission prompts (e.g. "Allow Read?").
        """
        return self.name

    def get_activity_description(self, input: dict | None = None) -> str | None:
        """Text shown in the spinner while the tool is executing."""
        return None

    def get_tool_use_summary(self, input: dict | None = None) -> str | None:
        """Optional one-line summary of the tool invocation."""
        return None

    def streaming_preview_fields(self) -> list[str]:
        """Top-level string fields to extract from partial input JSON during streaming.

        Returning a non-empty list lets the renderer show the tool header
        with these fields filled in before the full input JSON is parsed.
        Override to opt in; the default empty list means no streaming preview.
        """
        return []

    # --- Permission methods ---
    @property
    def supports_blanket_allow(self) -> bool:
        """Whether this tool supports blanket 'always allow this tool' permission."""
        return True

    @property
    def timeout(self) -> float | None:
        """Per-tool timeout in seconds. None means use the global default."""
        return None

    def is_read_only(self, input: dict | None = None) -> bool:
        """Whether the tool only reads and never modifies state."""
        return False

    def is_concurrency_safe(self, tool_input: dict[str, Any]) -> bool:
        """Whether this tool can safely run concurrently with other tools.
        By default, read-only tools are concurrency safe.
        """
        return self.is_read_only(tool_input)

    def is_destructive(self, input: dict | None = None) -> bool:
        """Whether the tool performs destructive operations."""
        return False

    def needs_event_queue(self) -> bool:
        """Override to return True if this tool emits ToolEmittedEvent subclasses.

        Default False. If True, AgentLoop passes an event_queue kwarg to the
        tool's execute() method. Without it, calls to event_queue.put() are
        unreachable and the UI sees nothing.

        In-tree tools that override this: AgentTool, ROSStackTool,
        ROSStackInstancesTool, ShowDiagramTool, ShowCandidateDetailTool.
        """
        return False

    async def check_permissions(self, input: dict, context=None) -> "PermissionResult":
        """Check permissions"""
        from iac_code.types.permissions import PermissionResult

        if self.is_read_only(input):
            return PermissionResult(behavior="allow")
        return PermissionResult(behavior="ask", message=_("Allow {}?").format(self.user_facing_name(input)))


class ToolRegistry:
    """Registry for managing available tools."""

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        """Register a tool."""
        self._tools[tool.name] = tool

    def unregister(self, name: str) -> None:
        """Unregister a tool if it exists."""
        self._tools.pop(name, None)

    def get(self, name: str) -> Tool | None:
        """Get a tool by name."""
        return self._tools.get(name)

    def list_tools(self) -> list[Tool]:
        """List all registered tools."""
        return list(self._tools.values())

    def to_api_format(self) -> list[dict[str, Any]]:
        """Convert all tools to LLM API format (legacy OpenAI format)."""
        return [tool.to_api_format() for tool in self._tools.values()]

    def clone(self) -> "ToolRegistry":
        """Create an independent copy of this registry.

        Primarily used by the pipeline subsystem when constructing per-step
        tool subsets via clone() + filter()/exclude(). Not typically needed
        in normal mode.
        """
        new_reg = ToolRegistry()
        for tool in self._tools.values():
            new_reg.register(tool)
        return new_reg

    def filter(self, allowed_names: list[str]) -> "ToolRegistry":
        """Return a new registry containing only tools whose name is in `allowed_names`.

        Pipeline subsystem only — see pipeline.yaml `tools.include`.
        """
        new_reg = ToolRegistry()
        for name in allowed_names:
            tool = self._tools.get(name)
            if tool is not None:
                new_reg.register(tool)
        return new_reg

    def exclude(self, names: list[str]) -> "ToolRegistry":
        """Return a new registry with the named tools removed.

        Pipeline subsystem only — see pipeline.yaml `tools.exclude`.
        """
        new_reg = ToolRegistry()
        for name, tool in self._tools.items():
            if name not in names:
                new_reg.register(tool)
        return new_reg

    def register_default_tools(self) -> None:
        """Register all default built-in tools."""
        from iac_code.tools.bash import BashTool
        from iac_code.tools.edit_file import EditFileTool
        from iac_code.tools.glob import GlobTool
        from iac_code.tools.grep import GrepTool
        from iac_code.tools.list_files import ListFilesTool
        from iac_code.tools.read_file import ReadFileTool
        from iac_code.tools.web_fetch import WebFetchTool
        from iac_code.tools.write_file import WriteFileTool

        self.register(ReadFileTool())
        self.register(WriteFileTool())
        self.register(EditFileTool())
        self.register(BashTool())
        self.register(ListFilesTool())
        self.register(GlobTool())
        self.register(GrepTool())
        self.register(WebFetchTool())
