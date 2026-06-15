from __future__ import annotations

from loguru import logger

from iac_code.types.stream_events import ToolUseEndEvent, ToolUseStartEvent
from iac_code.utils.tool_input_parser import parse_tool_input_events


class TestParseToolInputEvents:
    def test_single_valid_json_yields_one_end_event(self):
        events = list(parse_tool_input_events("toolu_1", "read_file", '{"path":"a.txt"}'))

        assert len(events) == 1
        assert isinstance(events[0], ToolUseEndEvent)
        assert events[0].tool_use_id == "toolu_1"
        assert events[0].name == "read_file"
        assert events[0].input == {"path": "a.txt"}

    def test_concatenated_json_recovers_additional_tool_calls(self):
        events = list(parse_tool_input_events("toolu_1", "read_file", '{"path":"a.txt"}{"path":"b.txt"}'))

        assert len(events) == 3
        assert isinstance(events[0], ToolUseEndEvent)
        assert events[0].tool_use_id == "toolu_1"
        assert events[0].name == "read_file"
        assert events[0].input == {"path": "a.txt"}
        assert isinstance(events[1], ToolUseStartEvent)
        assert events[1].name == "read_file"
        assert isinstance(events[2], ToolUseEndEvent)
        assert events[2].name == "read_file"
        assert events[2].input == {"path": "b.txt"}
        assert events[2].tool_use_id == events[1].tool_use_id

    def test_invalid_json_yields_empty_input_end_event(self):
        events = list(parse_tool_input_events("toolu_1", "read_file", "{invalid"))

        assert len(events) == 1
        assert isinstance(events[0], ToolUseEndEvent)
        assert events[0].name == "read_file"
        assert events[0].input == {}

    def test_empty_json_yields_empty_input_end_event(self):
        events = list(parse_tool_input_events("toolu_1", "read_file", ""))

        assert len(events) == 1
        assert isinstance(events[0], ToolUseEndEvent)
        assert events[0].name == "read_file"
        assert events[0].input == {}

    def test_invalid_json_warning_interpolates_tool_metadata(self):
        messages: list[str] = []
        sink_id = logger.add(lambda message: messages.append(str(message)), level="WARNING")

        try:
            list(parse_tool_input_events("toolu_1", "read_file", '{"path":'))
        finally:
            logger.remove(sink_id)

        log_text = "".join(messages)
        assert "tool_use_id=toolu_1" in log_text
        assert "length=8" in log_text
        assert 'raw={"path":' in log_text
        assert "%s" not in log_text
