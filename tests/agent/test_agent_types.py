from iac_code.agent.agent_types import (
    AgentDefinition,
    filter_tools,
    get_agent_definition,
    get_builtin_agents,
)
from iac_code.tools.base import Tool, ToolRegistry, ToolResult


class FakeTool(Tool):
    def __init__(self, tool_name: str, read_only: bool = True):
        self._name = tool_name
        self._read_only = read_only

    @property
    def name(self):
        return self._name

    @property
    def description(self):
        return f"Fake {self._name}"

    @property
    def input_schema(self):
        return {"type": "object", "properties": {}}

    async def execute(self, *, tool_input, context):
        return ToolResult.success("ok")

    def is_read_only(self, input=None):
        return self._read_only


class TestAgentDefinition:
    def test_is_tool_allowed_wildcard(self):
        defn = AgentDefinition(
            agent_type="test",
            when_to_use="test",
            tools=["*"],
            disallowed_tools=["agent"],
        )
        assert defn.is_tool_allowed("read_file") is True
        assert defn.is_tool_allowed("agent") is False

    def test_is_tool_allowed_whitelist(self):
        defn = AgentDefinition(
            agent_type="test",
            when_to_use="test",
            tools=["read_file", "grep"],
        )
        assert defn.is_tool_allowed("read_file") is True
        assert defn.is_tool_allowed("write_file") is False

    def test_disallowed_overrides_whitelist(self):
        defn = AgentDefinition(
            agent_type="test",
            when_to_use="test",
            tools=["*"],
            disallowed_tools=["bash"],
        )
        assert defn.is_tool_allowed("bash") is False


class TestFilterTools:
    def test_wildcard_with_blacklist(self):
        registry = ToolRegistry()
        registry.register(FakeTool("read_file"))
        registry.register(FakeTool("write_file", read_only=False))
        registry.register(FakeTool("agent", read_only=False))

        defn = AgentDefinition(
            agent_type="general-purpose",
            when_to_use="test",
            tools=["*"],
            disallowed_tools=["agent"],
        )
        filtered = filter_tools(registry, defn)
        names = [t.name for t in filtered.list_tools()]
        assert "read_file" in names
        assert "write_file" in names
        assert "agent" not in names

    def test_explicit_whitelist(self):
        registry = ToolRegistry()
        registry.register(FakeTool("read_file"))
        registry.register(FakeTool("grep"))
        registry.register(FakeTool("write_file", read_only=False))

        defn = AgentDefinition(
            agent_type="explore",
            when_to_use="test",
            tools=["read_file", "grep"],
        )
        filtered = filter_tools(registry, defn)
        names = [t.name for t in filtered.list_tools()]
        assert "read_file" in names
        assert "grep" in names
        assert "write_file" not in names


class TestBuiltinAgents:
    def test_three_builtin_agents(self):
        agents = get_builtin_agents()
        assert len(agents) == 3

    def test_general_purpose_exists(self):
        defn = get_agent_definition("general-purpose")
        assert defn is not None
        assert defn.is_tool_allowed("read_file") is True
        assert defn.is_tool_allowed("agent") is False

    def test_explore_is_read_only(self):
        defn = get_agent_definition("explore")
        assert defn is not None
        assert defn.is_tool_allowed("read_file") is True
        assert defn.is_tool_allowed("write_file") is False

    def test_plan_no_bash(self):
        defn = get_agent_definition("plan")
        assert defn is not None
        assert defn.is_tool_allowed("bash") is False
        assert defn.is_tool_allowed("read_file") is True

    def test_unknown_type_returns_none(self):
        assert get_agent_definition("nonexistent") is None
