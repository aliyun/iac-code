"""Agent definition model, built-in agent types, and tool filtering."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from iac_code.i18n import _

if TYPE_CHECKING:
    from iac_code.tools.base import ToolRegistry


@dataclass
class AgentDefinition:
    """Structured definition of an agent type."""

    agent_type: str
    when_to_use: str
    tools: list[str] | None = field(default_factory=lambda: ["*"])
    disallowed_tools: list[str] | None = field(default_factory=list)
    max_turns: int = 50
    model: str = "inherit"

    @property
    def allows_all_tools(self) -> bool:
        return self.tools is not None and "*" in self.tools

    def is_tool_allowed(self, tool_name: str) -> bool:
        if self.disallowed_tools and tool_name in self.disallowed_tools:
            return False
        if self.allows_all_tools:
            return True
        if self.tools:
            return tool_name in self.tools
        return False


def filter_tools(registry: "ToolRegistry", agent_def: AgentDefinition) -> "ToolRegistry":
    """Create a new ToolRegistry containing only tools allowed by the agent definition."""
    from iac_code.tools.base import ToolRegistry

    filtered = ToolRegistry()
    for tool in registry.list_tools():
        if agent_def.is_tool_allowed(tool.name):
            filtered.register(tool)
    return filtered


def get_builtin_agents() -> list[AgentDefinition]:
    return [
        AgentDefinition(
            agent_type="general-purpose",
            when_to_use=_(
                "Use for complex, multi-step tasks that require research, code changes, "
                "or coordinating multiple operations."
            ),
            tools=["*"],
            disallowed_tools=["agent"],
            max_turns=100,
        ),
        AgentDefinition(
            agent_type="explore",
            when_to_use=_(
                "Use to quickly find files, search code, or answer questions about the codebase. "
                "Read-only; cannot modify files."
            ),
            tools=["read_file", "glob", "grep", "list_files", "bash"],
            disallowed_tools=["write_file", "edit_file", "agent"],
            max_turns=30,
        ),
        AgentDefinition(
            agent_type="plan",
            when_to_use=_(
                "Use to plan implementation strategy, review architecture, or design solutions. "
                "Read-only, no execution."
            ),
            tools=["read_file", "glob", "grep", "list_files"],
            disallowed_tools=["bash", "write_file", "edit_file", "agent"],
            max_turns=20,
        ),
    ]


def get_agent_definition(agent_type: str) -> AgentDefinition | None:
    for defn in get_builtin_agents():
        if defn.agent_type == agent_type:
            return defn
    return None
