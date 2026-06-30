from iac_code.types.stream_events import MCPProgressEvent, ToolResultEvent, ToolUseStartEvent
from iac_code.ui.stream_accumulator import StreamAccumulator


def test_tool_result_with_unknown_id_does_not_fallback_by_name() -> None:
    acc = StreamAccumulator()
    acc.process(ToolUseStartEvent(tool_use_id="tool-a", name="read_file"))

    acc.process(ToolResultEvent(tool_use_id="stale-id", tool_name="read_file", result="wrong"))

    assert acc.tool_records["tool-a"].done is False


def test_orphan_tool_result_fallback_requires_unique_pending_tool_name() -> None:
    acc = StreamAccumulator()
    acc.process(ToolUseStartEvent(tool_use_id="tool-a", name="read_file"))
    acc.process(ToolUseStartEvent(tool_use_id="tool-b", name="read_file"))

    acc.process(ToolResultEvent(tool_use_id="", tool_name="read_file", result="ambiguous"))

    assert acc.tool_records["tool-a"].done is False
    assert acc.tool_records["tool-b"].done is False


def test_orphan_tool_result_fallback_allows_single_pending_tool_name() -> None:
    acc = StreamAccumulator()
    acc.process(ToolUseStartEvent(tool_use_id="tool-a", name="read_file"))

    acc.process(ToolResultEvent(tool_use_id="", tool_name="read_file", result="ok"))

    assert acc.tool_records["tool-a"].done is True
    assert acc.tool_records["tool-a"].result == "ok"


def test_mcp_progress_updates_matching_tool_record() -> None:
    acc = StreamAccumulator()
    acc.process(ToolUseStartEvent(tool_use_id="tool-a", name="mcp__live__echo"))

    action = acc.process(
        MCPProgressEvent(
            server_name="live",
            tool_name="echo",
            progress=1,
            total=2,
            message="halfway",
            tool_use_id="tool-a",
        )
    )

    assert action == "tool_update"
    assert acc.tool_records["tool-a"].progress_renderable == "MCP live:echo: 1/2: halfway"
