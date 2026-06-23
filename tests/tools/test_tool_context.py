from iac_code.tools.base import ToolContext


def test_tool_context_positional_tool_use_id_compatibility() -> None:
    context = ToolContext("/tmp/project", None, "toolu-1")

    assert context.cwd == "/tmp/project"
    assert context.event_queue is None
    assert context.tool_use_id == "toolu-1"
