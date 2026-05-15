"""Comprehensive tests for ACPEventConverter event type conversions."""

from __future__ import annotations

import acp

from iac_code.acp.convert import ACPEventConverter, _tool_kind, acp_blocks_to_multimodal, acp_blocks_to_prompt_text
from iac_code.acp.state import ToolCallState, TurnState
from iac_code.types.stream_events import (
    CompactionEvent,
    ErrorEvent,
    MessageEndEvent,
    MessageStartEvent,
    PermissionRequestEvent,
    PlanEvent,
    PlanStep,
    StackInstancesProgressEvent,
    StackProgressEvent,
    SubAgentToolEvent,
    TextDeltaEvent,
    ThinkingDeltaEvent,
    ToolInputDeltaEvent,
    ToolResultEvent,
    ToolUseEndEvent,
    ToolUseStartEvent,
    Usage,
)

# ---------------------------------------------------------------------------
# ThinkingDeltaEvent → AgentThoughtChunk
# ---------------------------------------------------------------------------


def test_thinking_delta_event_to_thought_chunk() -> None:
    converter = ACPEventConverter(turn_id="turn-1")
    updates = converter.event_to_updates(ThinkingDeltaEvent(text="Let me think..."))

    assert len(updates) == 1
    assert updates[0].session_update == "agent_thought_chunk"
    assert updates[0].content.text == "Let me think..."


# ---------------------------------------------------------------------------
# ToolInputDeltaEvent accumulates args
# ---------------------------------------------------------------------------


def test_tool_input_delta_event_accumulates_args() -> None:
    converter = ACPEventConverter(turn_id="turn-1")
    # Start a tool
    converter.event_to_updates(ToolUseStartEvent(tool_use_id="t1", name="bash"))

    # Send partial input chunks
    updates1 = converter.event_to_updates(ToolInputDeltaEvent(tool_use_id="t1", partial_json='{"cmd":'))
    updates2 = converter.event_to_updates(ToolInputDeltaEvent(tool_use_id="t1", partial_json=' "ls"}'))

    assert len(updates1) == 1
    assert updates1[0].session_update == "tool_call_update"
    assert updates1[0].status == "pending"
    # First chunk only
    assert updates1[0].content[0].content.text == '{"cmd":'

    assert len(updates2) == 1
    # Accumulated
    assert updates2[0].content[0].content.text == '{"cmd": "ls"}'


# ---------------------------------------------------------------------------
# CompactionEvent → AgentMessageChunk
# ---------------------------------------------------------------------------


def test_compaction_event_to_message_chunk() -> None:
    converter = ACPEventConverter(turn_id="turn-1")
    updates = converter.event_to_updates(CompactionEvent(original_tokens=5000, compacted_tokens=2000))

    assert len(updates) == 1
    assert updates[0].session_update == "agent_message_chunk"
    assert "5000" in updates[0].content.text
    assert "2000" in updates[0].content.text
    assert "compacted" in updates[0].content.text.lower()


# ---------------------------------------------------------------------------
# ErrorEvent → AgentMessageChunk
# ---------------------------------------------------------------------------


def test_error_event_to_message_chunk() -> None:
    converter = ACPEventConverter(turn_id="turn-1")
    updates = converter.event_to_updates(ErrorEvent(error="Rate limit exceeded", is_retryable=True))

    assert len(updates) == 1
    assert updates[0].session_update == "agent_message_chunk"
    assert "[Error]" in updates[0].content.text
    assert "Rate limit exceeded" in updates[0].content.text


# ---------------------------------------------------------------------------
# Tool call full lifecycle
# ---------------------------------------------------------------------------


def test_tool_call_full_lifecycle_events() -> None:
    converter = ACPEventConverter(turn_id="turn-1")

    # 1. ToolUseStart → pending
    start_updates = converter.event_to_updates(ToolUseStartEvent(tool_use_id="t1", name="read_file"))
    assert len(start_updates) == 1
    assert start_updates[0].session_update == "tool_call"
    assert start_updates[0].status == "pending"
    assert start_updates[0].tool_call_id == "turn-1/t1"

    # 2. ToolInputDelta → pending
    input_updates = converter.event_to_updates(ToolInputDeltaEvent(tool_use_id="t1", partial_json='{"path":"x"}'))
    assert len(input_updates) == 1
    assert input_updates[0].status == "pending"

    # 3. ToolUseEnd → in_progress
    end_updates = converter.event_to_updates(ToolUseEndEvent(tool_use_id="t1", name="read_file", input={"path": "x"}))
    assert len(end_updates) == 1
    assert end_updates[0].status == "in_progress"

    # 4. ToolResult → progress (in_progress) + end (completed)
    result_updates = converter.event_to_updates(
        ToolResultEvent(tool_use_id="t1", tool_name="read_file", result="file content")
    )
    assert len(result_updates) == 2
    assert result_updates[0].status == "in_progress"
    assert result_updates[1].status == "completed"


# ---------------------------------------------------------------------------
# Mixed event stream
# ---------------------------------------------------------------------------


def test_mixed_event_stream_preserves_order() -> None:
    converter = ACPEventConverter(turn_id="turn-1")
    events = [
        TextDeltaEvent(text="Hello"),
        ThinkingDeltaEvent(text="thinking..."),
        ToolUseStartEvent(tool_use_id="t1", name="bash"),
        TextDeltaEvent(text=" world"),
        ToolResultEvent(tool_use_id="t1", tool_name="bash", result="done"),
    ]

    all_updates = []
    for event in events:
        all_updates.extend(converter.event_to_updates(event))

    assert len(all_updates) == 6
    assert all_updates[0].session_update == "agent_message_chunk"
    assert all_updates[1].session_update == "agent_thought_chunk"
    assert all_updates[2].session_update == "tool_call"
    assert all_updates[3].session_update == "agent_message_chunk"
    # ToolResult produces progress + end
    assert all_updates[4].session_update == "tool_call_update"
    assert all_updates[5].session_update == "tool_call_update"


# ---------------------------------------------------------------------------
# Multi-turn tool ID isolation
# ---------------------------------------------------------------------------


def test_multi_turn_tool_id_isolation() -> None:
    conv1 = ACPEventConverter(turn_id="turn-aaa")
    conv2 = ACPEventConverter(turn_id="turn-bbb")

    updates1 = conv1.event_to_updates(ToolUseStartEvent(tool_use_id="shared-id", name="bash"))
    updates2 = conv2.event_to_updates(ToolUseStartEvent(tool_use_id="shared-id", name="bash"))

    assert updates1[0].tool_call_id == "turn-aaa/shared-id"
    assert updates2[0].tool_call_id == "turn-bbb/shared-id"
    assert updates1[0].tool_call_id != updates2[0].tool_call_id


# ---------------------------------------------------------------------------
# MessageStartEvent returns empty
# ---------------------------------------------------------------------------


def test_message_start_event_returns_empty() -> None:
    converter = ACPEventConverter(turn_id="turn-1")
    # MessageStartEvent is not in the match-case handled branches that return updates
    # It falls through to the wildcard case
    updates = converter.event_to_updates(MessageStartEvent(message_id="msg-1"))
    assert updates == []


# ---------------------------------------------------------------------------
# MessageEndEvent returns empty
# ---------------------------------------------------------------------------


def test_message_end_event_returns_empty() -> None:
    converter = ACPEventConverter(turn_id="turn-1")
    updates = converter.event_to_updates(MessageEndEvent(stop_reason="end_turn", usage=Usage()))
    assert updates == []


def test_message_end_event_emits_usage_update_when_snapshot_provided() -> None:
    """With a context_snapshot, MessageEndEvent emits an ACP UsageUpdate."""
    converter = ACPEventConverter(
        turn_id="turn-1",
        context_snapshot=lambda: (1234, 200_000),
    )
    updates = converter.event_to_updates(MessageEndEvent(stop_reason="end_turn", usage=Usage()))
    assert len(updates) == 1
    assert updates[0].session_update == "usage_update"
    assert updates[0].used == 1234
    assert updates[0].size == 200_000


def test_message_end_event_skips_usage_update_on_zero_window() -> None:
    """A snapshot reporting size<=0 should not emit UsageUpdate."""
    converter = ACPEventConverter(
        turn_id="turn-1",
        context_snapshot=lambda: (0, 0),
    )
    updates = converter.event_to_updates(MessageEndEvent(stop_reason="end_turn", usage=Usage()))
    assert updates == []


def test_message_end_event_snapshot_exception_is_swallowed() -> None:
    """Snapshot errors must not break the stream — just skip UsageUpdate."""

    def broken_snapshot() -> tuple[int, int]:
        raise RuntimeError("boom")

    converter = ACPEventConverter(turn_id="turn-1", context_snapshot=broken_snapshot)
    updates = converter.event_to_updates(MessageEndEvent(stop_reason="end_turn", usage=Usage()))
    assert updates == []


# ---------------------------------------------------------------------------
# StackProgressEvent returns empty
# ---------------------------------------------------------------------------


def test_stack_progress_event_returns_empty() -> None:
    converter = ACPEventConverter(turn_id="turn-1")
    updates = converter.event_to_updates(
        StackProgressEvent(
            stack_id="stack-1",
            stack_name="test-stack",
            status="CREATE_IN_PROGRESS",
            progress_percentage=50.0,
            resources=[],
            elapsed_seconds=10,
        )
    )
    assert updates == []


# ---------------------------------------------------------------------------
# StackInstancesProgressEvent returns empty
# ---------------------------------------------------------------------------


def test_stack_instances_progress_event_returns_empty() -> None:
    converter = ACPEventConverter(turn_id="turn-1")
    updates = converter.event_to_updates(
        StackInstancesProgressEvent(
            stack_group_name="group-1",
            operation_id="op-1",
            status="RUNNING",
            progress_percentage=30,
            instances=[],
            elapsed_seconds=5,
        )
    )
    assert updates == []


# ---------------------------------------------------------------------------
# PermissionRequestEvent returns empty
# ---------------------------------------------------------------------------


def test_permission_request_event_returns_empty() -> None:
    converter = ACPEventConverter(turn_id="turn-1")
    updates = converter.event_to_updates(
        PermissionRequestEvent(tool_name="bash", tool_input={"cmd": "rm -rf /"}, tool_use_id="t1")
    )
    assert updates == []


# ---------------------------------------------------------------------------
# SubAgentToolEvent returns empty
# ---------------------------------------------------------------------------


def test_subagent_tool_event_returns_empty() -> None:
    converter = ACPEventConverter(turn_id="turn-1")
    updates = converter.event_to_updates(
        SubAgentToolEvent(parent_tool_use_id="p1", child_tool_name="read_file", child_tool_input={"path": "/x"})
    )
    assert updates == []


# ---------------------------------------------------------------------------
# ToolResult success vs failure status
# ---------------------------------------------------------------------------


def test_tool_result_success_vs_failure_status() -> None:
    # Success case
    converter1 = ACPEventConverter(turn_id="turn-1")
    converter1.event_to_updates(ToolUseStartEvent(tool_use_id="t1", name="bash"))
    success = converter1.event_to_updates(
        ToolResultEvent(tool_use_id="t1", tool_name="bash", result="output", is_error=False)
    )
    assert len(success) == 2
    assert success[0].status == "in_progress"  # progress with output
    assert success[1].status == "completed"  # terminal end

    # Failure case
    converter2 = ACPEventConverter(turn_id="turn-2")
    converter2.event_to_updates(ToolUseStartEvent(tool_use_id="t2", name="bash"))
    failure = converter2.event_to_updates(
        ToolResultEvent(tool_use_id="t2", tool_name="bash", result="command not found", is_error=True)
    )
    assert len(failure) == 2
    assert failure[0].status == "in_progress"  # progress with output
    assert failure[1].status == "failed"  # terminal end


# ---------------------------------------------------------------------------
# Unknown event type safely ignored
# ---------------------------------------------------------------------------


def test_unknown_event_type_safely_ignored() -> None:
    converter = ACPEventConverter(turn_id="turn-1")
    # TaskNotificationEvent is not explicitly handled, falls to wildcard
    from iac_code.types.stream_events import TaskNotificationEvent

    updates = converter.event_to_updates(
        TaskNotificationEvent(task_id="task-1", description="done", status="completed")
    )
    assert updates == []


# ---------------------------------------------------------------------------
# Terminal tool result marked as already displayed
# ---------------------------------------------------------------------------


def test_terminal_tool_result_marked_already_displayed() -> None:
    converter = ACPEventConverter(turn_id="turn-1", terminal_tool_names={"bash"})
    converter.event_to_updates(ToolUseStartEvent(tool_use_id="t1", name="bash"))
    updates = converter.event_to_updates(ToolResultEvent(tool_use_id="t1", tool_name="bash", result="output"))
    assert len(updates) == 2
    # Progress carries the meta and output
    assert updates[0].status == "in_progress"
    assert updates[0].field_meta == {"already_displayed": True}
    # End is terminal
    assert updates[1].status == "completed"


# ---------------------------------------------------------------------------
# Non-terminal tool result has no meta
# ---------------------------------------------------------------------------


def test_non_terminal_tool_result_has_no_meta() -> None:
    converter = ACPEventConverter(turn_id="turn-1", terminal_tool_names={"bash"})
    converter.event_to_updates(ToolUseStartEvent(tool_use_id="t1", name="read_file"))
    updates = converter.event_to_updates(
        ToolResultEvent(tool_use_id="t1", tool_name="read_file", result="file content")
    )
    assert len(updates) == 2
    assert updates[0].status == "in_progress"
    assert updates[0].field_meta is None
    assert updates[1].status == "completed"


def test_terminal_tool_names_empty_by_default() -> None:
    converter = ACPEventConverter(turn_id="turn-1")
    converter.event_to_updates(ToolUseStartEvent(tool_use_id="t1", name="bash"))
    updates = converter.event_to_updates(ToolResultEvent(tool_use_id="t1", tool_name="bash", result="output"))
    assert len(updates) == 2
    assert updates[0].field_meta is None
    assert updates[1].status == "completed"


# ---------------------------------------------------------------------------
# Plan event conversion
# ---------------------------------------------------------------------------


def test_plan_event_converts_to_agent_plan_update() -> None:
    converter = ACPEventConverter(turn_id="turn-1")
    steps = [
        PlanStep(content="Analyze requirements", status="completed", priority="high"),
        PlanStep(content="Implement solution", status="in_progress", priority="high"),
        PlanStep(content="Write tests", status="pending", priority="medium"),
    ]
    updates = converter.event_to_updates(PlanEvent(steps=steps))

    assert len(updates) == 1
    plan_update = updates[0]
    assert plan_update.session_update == "plan"
    assert len(plan_update.entries) == 3
    assert plan_update.entries[0].content == "Analyze requirements"
    assert plan_update.entries[0].status == "completed"
    assert plan_update.entries[0].priority == "high"
    assert plan_update.entries[1].status == "in_progress"
    assert plan_update.entries[2].status == "pending"
    assert plan_update.entries[2].priority == "medium"


def test_plan_event_empty_steps() -> None:
    converter = ACPEventConverter(turn_id="turn-1")
    updates = converter.event_to_updates(PlanEvent(steps=[]))

    assert len(updates) == 1
    assert updates[0].session_update == "plan"
    assert updates[0].entries == []


def test_plan_event_default_priority_and_status() -> None:
    converter = ACPEventConverter(turn_id="turn-1")
    steps = [PlanStep(content="Do something")]
    updates = converter.event_to_updates(PlanEvent(steps=steps))

    assert len(updates) == 1
    entry = updates[0].entries[0]
    assert entry.content == "Do something"
    assert entry.status == "pending"
    assert entry.priority == "medium"


# ---------------------------------------------------------------------------
# tool_call_end lifecycle validation
# ---------------------------------------------------------------------------


def test_tool_result_emits_progress_then_end() -> None:
    """ToolResultEvent must produce two updates: progress (with output) + end (terminal status)."""
    converter = ACPEventConverter(turn_id="turn-1")
    converter.event_to_updates(ToolUseStartEvent(tool_use_id="t1", name="bash"))
    updates = converter.event_to_updates(ToolResultEvent(tool_use_id="t1", tool_name="bash", result="output here"))

    assert len(updates) == 2
    # First: progress with the tool output
    assert updates[0].session_update == "tool_call_update"
    assert updates[0].status == "in_progress"
    assert updates[0].content[0].content.text == "output here"
    # Second: terminal end marker
    assert updates[1].session_update == "tool_call_update"
    assert updates[1].status == "completed"
    assert updates[1].content is None  # no duplicate content on end marker


def test_tool_call_complete_lifecycle() -> None:
    """Validate full lifecycle: start -> input -> use_end -> result(progress+end)."""
    converter = ACPEventConverter(turn_id="turn-1")

    # 1. start
    s = converter.event_to_updates(ToolUseStartEvent(tool_use_id="t1", name="read_file"))
    assert s[0].session_update == "tool_call"
    assert s[0].status == "pending"

    # 2. input
    i = converter.event_to_updates(ToolInputDeltaEvent(tool_use_id="t1", partial_json='{"path":"x"}'))
    assert i[0].session_update == "tool_call_update"
    assert i[0].status == "pending"

    # 3. use end
    u = converter.event_to_updates(ToolUseEndEvent(tool_use_id="t1", name="read_file", input={"path": "x"}))
    assert u[0].session_update == "tool_call_update"
    assert u[0].status == "in_progress"

    # 4. result -> progress + end
    r = converter.event_to_updates(ToolResultEvent(tool_use_id="t1", tool_name="read_file", result="content"))
    assert len(r) == 2
    assert r[0].status == "in_progress"
    assert r[1].status == "completed"


# ---------------------------------------------------------------------------
# acp_blocks_to_prompt_text helpers
# ---------------------------------------------------------------------------


def test_acp_text_prompt_becomes_plain_text() -> None:
    text = acp_blocks_to_prompt_text([acp.schema.TextContentBlock(type="text", text="hello")])

    assert text == "hello"


def test_embedded_text_resource_is_included() -> None:
    text = acp_blocks_to_prompt_text(
        [
            acp.schema.EmbeddedResourceContentBlock(
                type="resource",
                resource=acp.schema.TextResourceContents(uri="file:///main.tf", text="resource x {}"),
            )
        ]
    )

    assert "file:///main.tf" in text
    assert "resource x {}" in text


def test_text_delta_converts_to_agent_message_chunk() -> None:
    updates = ACPEventConverter("turn").event_to_updates(TextDeltaEvent(text="hi"))

    assert updates[0].session_update == "agent_message_chunk"


def test_tool_ids_are_prefixed_with_turn_id() -> None:
    converter = ACPEventConverter("turn-1")
    start = converter.event_to_updates(ToolUseStartEvent(tool_use_id="tool-1", name="read_file"))[0]
    result_updates = converter.event_to_updates(
        ToolResultEvent(tool_use_id="tool-1", tool_name="read_file", result="ok")
    )

    assert start.tool_call_id == "turn-1/tool-1"
    # ToolResult now emits progress + end, both share the same tool_call_id
    assert len(result_updates) == 2
    assert result_updates[0].tool_call_id == "turn-1/tool-1"
    assert result_updates[1].tool_call_id == "turn-1/tool-1"


# ---------------------------------------------------------------------------
# ACPEventConverter integration with TurnState
# ---------------------------------------------------------------------------


def test_tool_use_start_triggers_turn_state_start_tool_call() -> None:
    """Scenario 9: ToolUseStartEvent registers a ToolCallState in turn_state."""
    ts = TurnState(turn_id="turn-9")
    converter = ACPEventConverter(turn_id="turn-9", turn_state=ts)

    converter.event_to_updates(ToolUseStartEvent(tool_use_id="tc-1", name="bash"))

    tc = ts.get_tool_call("tc-1")
    assert tc is not None
    assert tc.tool_name == "bash"
    assert tc.tool_call_id == "tc-1"


def test_tool_input_delta_updates_tool_call_state_and_generates_title_in_progress() -> None:
    """Scenario 10: ToolInputDeltaEvent updates ToolCallState, progress has computed title."""
    ts = TurnState(turn_id="turn-10")
    converter = ACPEventConverter(turn_id="turn-10", turn_state=ts)

    # Start the tool call first
    converter.event_to_updates(ToolUseStartEvent(tool_use_id="tc-2", name="read_file"))

    # Send input delta with file_path
    updates = converter.event_to_updates(
        ToolInputDeltaEvent(tool_use_id="tc-2", partial_json='{"file_path": "/tmp/test.tf"}')
    )

    assert len(updates) == 1
    progress = updates[0]
    assert progress.session_update == "tool_call_update"
    assert progress.title is not None
    assert "/tmp/test.tf" in progress.title

    # Verify the underlying ToolCallState was updated
    tc = ts.get_tool_call("tc-2")
    assert tc is not None
    assert tc.accumulated_input == '{"file_path": "/tmp/test.tf"}'


def test_converter_works_without_turn_state_backward_compatible() -> None:
    """Scenario 11: Without turn_state, converter still processes events normally."""
    converter = ACPEventConverter(turn_id="turn-11", turn_state=None)

    # ToolUseStart without turn_state should not error
    start_updates = converter.event_to_updates(ToolUseStartEvent(tool_use_id="tc-3", name="bash"))
    assert len(start_updates) == 1
    assert start_updates[0].session_update == "tool_call"

    # ToolInputDelta without turn_state should still produce an update
    delta_updates = converter.event_to_updates(
        ToolInputDeltaEvent(tool_use_id="tc-3", partial_json='{"command": "ls"}')
    )
    assert len(delta_updates) == 1
    assert delta_updates[0].session_update == "tool_call_update"


# ---------------------------------------------------------------------------
# TurnState and ToolCallState unit tests
# ---------------------------------------------------------------------------


def test_bash_tool_call_title_contains_command_summary() -> None:
    """Scenario 4: ToolCallState for bash computes title with command summary."""
    tc = ToolCallState(tool_call_id="tc-1", tool_name="bash")
    tc.update_input('{"command": "echo hello"}')

    assert "bash" in tc.title
    assert "echo hello" in tc.title


def test_read_file_tool_call_title_contains_file_path() -> None:
    """Scenario 5: ToolCallState for read_file computes title with file path."""
    tc = ToolCallState(tool_call_id="tc-2", tool_name="read_file")
    tc.update_input('{"file_path": "/tmp/main.tf"}')

    assert "read_file" in tc.title
    assert "/tmp/main.tf" in tc.title


def test_tool_call_state_accumulates_streamed_input_deltas() -> None:
    """Scenario 6: Multiple update_input() deltas are correctly concatenated."""
    tc = ToolCallState(tool_call_id="tc-3", tool_name="bash")
    tc.update_input('{"com')
    tc.update_input('mand": ')
    tc.update_input('"ls -la"}')

    assert tc.accumulated_input == '{"command": "ls -la"}'
    assert "ls -la" in tc.title


def test_turn_state_manages_multiple_concurrent_tool_calls() -> None:
    """Scenario 7: TurnState can track multiple independent tool calls."""
    ts = TurnState(turn_id="turn-1")
    tc_a = ts.start_tool_call("tc-a", "bash")
    tc_b = ts.start_tool_call("tc-b", "read_file")

    assert tc_a is not tc_b
    assert ts.get_tool_call("tc-a") is tc_a
    assert ts.get_tool_call("tc-b") is tc_b

    tc_a.update_input('{"command": "pwd"}')
    tc_b.update_input('{"file_path": "/etc/hosts"}')

    assert "pwd" in tc_a.title
    assert "/etc/hosts" in tc_b.title


def test_get_tool_call_returns_none_for_unknown_id() -> None:
    """Scenario 8: get_tool_call with nonexistent ID returns None."""
    ts = TurnState(turn_id="turn-x")
    assert ts.get_tool_call("nonexistent") is None


# ---------------------------------------------------------------------------
# acp_blocks_to_multimodal coverage
# ---------------------------------------------------------------------------


def test_acp_blocks_to_multimodal_embedded_text_resource() -> None:
    """EmbeddedResourceContentBlock with TextResourceContents in multimodal."""
    blocks = [
        acp.schema.EmbeddedResourceContentBlock(
            type="resource",
            resource=acp.schema.TextResourceContents(uri="file:///main.tf", text="resource x {}"),
        )
    ]
    result = acp_blocks_to_multimodal(blocks)
    assert len(result) == 1
    assert result[0]["type"] == "text"
    assert "file:///main.tf" in result[0]["text"]
    assert "resource x {}" in result[0]["text"]


def test_acp_blocks_to_multimodal_resource_content_block() -> None:
    """ResourceContentBlock in multimodal."""
    blocks = [acp.schema.ResourceContentBlock(type="resource_link", uri="file:///test.tf", name="test.tf")]
    result = acp_blocks_to_multimodal(blocks)
    assert len(result) == 1
    assert result[0]["type"] == "text"
    assert "file:///test.tf" in result[0]["text"]
    assert "test.tf" in result[0]["text"]


# ---------------------------------------------------------------------------
# ToolResult with turn_state timing (non-terminal tool)
# ---------------------------------------------------------------------------


def test_tool_result_with_turn_state_timing_non_terminal() -> None:
    """Non-terminal tool with turn_state should have timing meta but no already_displayed."""
    ts = TurnState(turn_id="turn-t")
    converter = ACPEventConverter(turn_id="turn-t", turn_state=ts, terminal_tool_names={"bash"})

    # Start a non-terminal tool
    converter.event_to_updates(ToolUseStartEvent(tool_use_id="t1", name="read_file"))

    # Result for non-terminal tool with turn_state
    updates = converter.event_to_updates(ToolResultEvent(tool_use_id="t1", tool_name="read_file", result="content"))
    assert len(updates) == 2
    # Should have timing meta but NOT already_displayed
    meta = updates[0].field_meta
    assert meta is not None
    assert "timing" in meta
    assert "elapsed_ms" in meta["timing"]
    assert "already_displayed" not in meta


# ---------------------------------------------------------------------------
# ToolCallStart.kind mapping
# ---------------------------------------------------------------------------


def test_tool_kind_mapping_covers_core_builtin_tools() -> None:
    """Every default-registered iac-code tool should map to a concrete kind."""
    assert _tool_kind("read_file") == "read"
    assert _tool_kind("list_files") == "read"
    assert _tool_kind("write_file") == "edit"
    assert _tool_kind("edit_file") == "edit"
    assert _tool_kind("grep") == "search"
    assert _tool_kind("glob") == "search"
    assert _tool_kind("bash") == "execute"
    assert _tool_kind("web_fetch") == "fetch"


def test_tool_kind_mapping_covers_extension_tools() -> None:
    """Memory / task / cloud extension tools should also map sensibly."""
    assert _tool_kind("read_memory") == "read"
    assert _tool_kind("write_memory") == "edit"
    assert _tool_kind("task_list") == "read"
    assert _tool_kind("task_get") == "read"
    assert _tool_kind("task_stop") == "execute"
    assert _tool_kind("ros_stack") == "execute"
    assert _tool_kind("ros_stack_instances") == "execute"
    assert _tool_kind("aliyun_doc_search") == "fetch"


def test_tool_kind_suffix_heuristics_for_dynamic_names() -> None:
    """Cloud provider tools use dynamic ``{provider}_api`` / ``*_doc_search`` names."""
    assert _tool_kind("aliyun_api") == "execute"
    assert _tool_kind("foo_api") == "execute"
    assert _tool_kind("bar_doc_search") == "fetch"


def test_tool_kind_falls_back_to_other_for_unknown_tool() -> None:
    assert _tool_kind("some_completely_new_tool") == "other"
    assert _tool_kind("") == "other"


def test_tool_use_start_event_populates_kind_on_session_update() -> None:
    """ACP session updates emitted from ToolUseStartEvent must carry ``kind``."""
    converter = ACPEventConverter(turn_id="turn-1")

    bash_updates = converter.event_to_updates(ToolUseStartEvent(tool_use_id="t1", name="bash"))
    assert bash_updates[0].kind == "execute"

    read_updates = converter.event_to_updates(ToolUseStartEvent(tool_use_id="t2", name="read_file"))
    assert read_updates[0].kind == "read"

    unknown_updates = converter.event_to_updates(ToolUseStartEvent(tool_use_id="t3", name="mystery"))
    assert unknown_updates[0].kind == "other"
