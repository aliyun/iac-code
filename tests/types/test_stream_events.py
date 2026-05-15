from iac_code.types.stream_events import (
    CompactionEvent,
    ErrorEvent,
    MessageEndEvent,
    MessageStartEvent,
    PermissionRequestEvent,
    StreamEvent,
    TaskNotificationEvent,
    TextDeltaEvent,
    ThinkingDeltaEvent,
    TombstoneEvent,
    ToolInputDeltaEvent,
    ToolResultEvent,
    ToolUseEndEvent,
    ToolUseStartEvent,
    Usage,
)


class TestStreamEventTypes:
    def test_text_delta(self):
        event = TextDeltaEvent(text="hello")
        assert event.type == "text_delta"
        assert event.text == "hello"

    def test_thinking_delta(self):
        event = ThinkingDeltaEvent(text="reasoning...")
        assert event.type == "thinking_delta"

    def test_tool_use_start(self):
        event = ToolUseStartEvent(tool_use_id="t1", name="read_file")
        assert event.type == "tool_use_start"
        assert event.name == "read_file"

    def test_tool_input_delta(self):
        event = ToolInputDeltaEvent(tool_use_id="t1", partial_json='{"file')
        assert event.type == "tool_input_delta"

    def test_tool_use_end(self):
        event = ToolUseEndEvent(tool_use_id="t1", name="read_file", input={"file_path": "a.py"})
        assert event.type == "tool_use_end"

    def test_message_end_with_usage(self):
        usage = Usage(input_tokens=100, output_tokens=50)
        event = MessageEndEvent(stop_reason="end_turn", usage=usage)
        assert event.type == "message_end"
        assert event.usage.input_tokens == 100
        assert event.usage.total_tokens == 150

    def test_tombstone(self):
        event = TombstoneEvent(message_id="msg-1")
        assert event.type == "tombstone"

    def test_error(self):
        event = ErrorEvent(error="rate limited", is_retryable=True)
        assert event.type == "error"
        assert event.is_retryable is True

    def test_tool_result(self):
        event = ToolResultEvent(tool_use_id="t1", tool_name="bash", result="ok")
        assert event.type == "tool_result"
        assert event.is_error is False

    def test_permission_request(self):
        event = PermissionRequestEvent(tool_name="bash", tool_input={"command": "rm -rf /"}, tool_use_id="t1")
        assert event.type == "permission_request"
        assert event.response_future is None  # None by default, set by AgentLoop at runtime

    def test_compaction(self):
        event = CompactionEvent(original_tokens=50000, compacted_tokens=5000)
        assert event.type == "compaction"

    def test_task_notification(self):
        event = TaskNotificationEvent(
            task_id="t1", description="Explore code", status="completed", result="Found 3 files"
        )
        assert event.type == "task_notification"

    def test_stream_event_union_covers_all(self):
        events: list[StreamEvent] = [
            MessageStartEvent(message_id="m1"),
            TextDeltaEvent(text="hi"),
            ThinkingDeltaEvent(text="hmm"),
            ToolUseStartEvent(tool_use_id="t1", name="bash"),
            ToolInputDeltaEvent(tool_use_id="t1", partial_json="{}"),
            ToolUseEndEvent(tool_use_id="t1", name="bash", input={}),
            MessageEndEvent(stop_reason="end_turn", usage=Usage()),
            TombstoneEvent(message_id="m1"),
            ErrorEvent(error="err", is_retryable=False),
            ToolResultEvent(tool_use_id="t1", tool_name="bash", result="ok"),
            PermissionRequestEvent(tool_name="bash", tool_input={}, tool_use_id="t1"),
            CompactionEvent(),
            TaskNotificationEvent(task_id="t1", description="x", status="completed"),
        ]
        assert len(events) == 13
