from iac_code.types.stream_events import (
    TOOL_RENDER_METADATA_KEY,
    MCPProgressEvent,
    ThinkingDeltaEvent,
    ToolResultEvent,
    ToolUseEndEvent,
    ToolUseStartEvent,
)
from iac_code.ui.stream_accumulator import StreamAccumulator


def test_tool_result_with_unknown_id_does_not_fallback_by_name() -> None:
    acc = StreamAccumulator()
    acc.process(ToolUseStartEvent(tool_use_id="tool-a", name="read_file"))

    acc.process(ToolResultEvent(tool_use_id="stale-id", tool_name="read_file", result="wrong"))

    assert acc.tool_records["tool-a"].done is False


def test_tool_end_replaces_partial_start_name() -> None:
    acc = StreamAccumulator()
    acc.process(ToolUseStartEvent(tool_use_id="tool-a", name="read_"))

    acc.process(ToolUseEndEvent(tool_use_id="tool-a", name="read_file", input={"path": "main.py"}))

    assert acc.tool_records["tool-a"].tool_name == "read_file"
    assert acc.tool_records["tool-a"].tool_input == {"path": "main.py"}


def test_metadata_only_thinking_does_not_start_ui_thinking_state() -> None:
    acc = StreamAccumulator()

    action = acc.process(ThinkingDeltaEvent(text="", provider_metadata={"provider": "gemini"}))

    assert action == "none"
    assert acc._thinking_start_time is None
    assert acc.thinking_buffer == ""


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


def test_tool_use_start_records_render_metadata() -> None:
    acc = StreamAccumulator()
    metadata = {TOOL_RENDER_METADATA_KEY: {"display_name": "InfraGuard scan"}}

    acc.process(ToolUseStartEvent(tool_use_id="tool-a", name="infraguard_scan", metadata=metadata))

    assert acc.tool_records["tool-a"].metadata == metadata
    assert not hasattr(acc.tool_records["tool-a"], "renderer_tool")


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


def test_mcp_progress_without_tool_use_id_matches_public_tool_name() -> None:
    acc = StreamAccumulator()
    acc.process(ToolUseStartEvent(tool_use_id="tool-a", name="mcp__yuque_space__search_docs_8d3f"))

    action = acc.process(
        MCPProgressEvent(
            server_name="yuque space",
            tool_name="search/docs",
            public_name="mcp__yuque_space__search_docs_8d3f",
            progress=1,
            total=2,
            message="api_key=sk-live-secret /Users/alice/.iac-code/settings.yml",
        )
    )

    assert action == "tool_update"
    assert (
        acc.tool_records["tool-a"].progress_renderable == "MCP yuque space:search/docs: 1/2: api_key=[REDACTED] [PATH]"
    )
