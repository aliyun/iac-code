from __future__ import annotations

import json

from loguru import logger

from iac_code.utils.json_utils import (
    describe_json_error,
    extract_json_int_value,
    extract_partial_string_fields,
    parse_concatenated_json,
    parse_json_tolerant,
    safe_parse_json,
)


class TestDescribeJsonError:
    def test_points_at_the_offset_and_shows_control_characters(self):
        raw = '{"note":"line one\nline two"}'
        try:
            json.loads(raw)
            raise AssertionError("expected a decode error")
        except json.JSONDecodeError as exc:
            pos = exc.pos
            detail = describe_json_error(raw, exc)

        assert "Invalid control character" in detail
        assert f"length={len(raw)}" in detail
        assert f"around_pos={pos}" in detail
        # repr keeps the newline visible instead of reflowing the log line.
        assert "\\n" in detail
        assert "\n" not in detail

    def test_falls_back_to_head_and_tail_without_a_position(self):
        detail = describe_json_error("abc", ValueError("boom"))

        assert "error=ValueError: boom" in detail
        assert "head='abc'" in detail
        assert "tail='abc'" in detail


class TestParseJsonTolerant:
    def test_returns_the_value_for_valid_json(self):
        assert parse_json_tolerant('{"a": 1}') == ({"a": 1}, None)

    def test_recovers_a_literal_control_character_inside_a_string(self):
        messages: list[str] = []
        sink_id = logger.add(lambda message: messages.append(str(message)), level="WARNING")
        try:
            value, error = parse_json_tolerant('{"note":"line one\nline two"}')
        finally:
            logger.remove(sink_id)

        # Strict json.loads throws away an otherwise perfectly good object here.
        assert value == {"note": "line one\nline two"}
        assert error is None
        assert "strict=False" in "".join(messages)

    def test_truncated_json_still_fails_with_a_described_defect(self):
        value, error = parse_json_tolerant('{"a": 1')

        assert value is None
        assert error is not None
        assert "length=7" in error
        assert "Expecting" in error

    def test_reports_empty_input(self):
        assert parse_json_tolerant("") == (None, "empty input")


class TestSafeParseJson:
    def test_returns_none_for_none_and_empty(self):
        assert safe_parse_json(None) is None
        assert safe_parse_json("") is None

    def test_parses_valid_json(self):
        assert safe_parse_json('{"a": 1, "b": 2}') == {"a": 1, "b": 2}

    def test_returns_none_for_invalid_json(self):
        assert safe_parse_json("{invalid") is None


class TestParseConcatenatedJson:
    def test_parses_multiple_objects(self):
        raw = '{"a":1}{"b":2}\n{"c":3}'
        assert parse_concatenated_json(raw) == [{"a": 1}, {"b": 2}, {"c": 3}]

    def test_skips_non_dict_objects(self):
        raw = '{"a":1}["x"]{"b":2}'
        assert parse_concatenated_json(raw) == [{"a": 1}, {"b": 2}]

    def test_returns_empty_list_when_nothing_parseable(self):
        assert parse_concatenated_json("not-json") == []

    def test_stops_after_invalid_tail(self):
        raw = '{"a":1} trailing'
        assert parse_concatenated_json(raw) == [{"a": 1}]


class TestExtractJsonIntValue:
    def test_extracts_integer_when_delimited_by_comma(self):
        assert extract_json_int_value('{"candidate_index": 10, "summary": "x"', "candidate_index") == 10

    def test_extracts_integer_when_delimited_by_object_end(self):
        assert extract_json_int_value('{"candidate_index": 10}', "candidate_index") == 10

    def test_extracts_integer_when_delimited_by_whitespace(self):
        assert extract_json_int_value('{"candidate_index": 10 ', "candidate_index") == 10

    def test_does_not_extract_unfinished_digit_prefix(self):
        assert extract_json_int_value('{"candidate_index": 1', "candidate_index") is None


class TestExtractPartialStringFields:
    def test_returns_empty_for_empty_input(self):
        assert extract_partial_string_fields("", {"path"}) == {}
        assert extract_partial_string_fields('{"path": "a.py"}', set()) == {}

    def test_extracts_single_closed_field(self):
        assert extract_partial_string_fields('{"path": "src/a.py"', {"path"}) == {"path": "src/a.py"}

    def test_skips_field_whose_value_is_not_yet_closed(self):
        # Closing quote of the path's value not present yet
        assert extract_partial_string_fields('{"path": "src/a.p', {"path"}) == {}

    def test_extracts_only_requested_fields(self):
        raw = '{"path": "a.py", "command": "ls"'
        assert extract_partial_string_fields(raw, {"path"}) == {"path": "a.py"}

    def test_extracts_multiple_completed_fields(self):
        raw = '{"path": "a.py", "mode": "r"'
        assert extract_partial_string_fields(raw, {"path", "mode"}) == {"path": "a.py", "mode": "r"}

    def test_decodes_json_escape_sequences(self):
        # Newline escape inside the string
        raw = '{"path": "a\\nb.py"'
        assert extract_partial_string_fields(raw, {"path"}) == {"path": "a\nb.py"}

    def test_decodes_escaped_quote(self):
        raw = '{"path": "a\\"b.py"'
        assert extract_partial_string_fields(raw, {"path"}) == {"path": 'a"b.py'}

    def test_returns_first_occurrence_on_duplicate_key(self):
        raw = '{"path": "first.py", "path": "second.py"'
        assert extract_partial_string_fields(raw, {"path"}) == {"path": "first.py"}

    def test_ignores_field_not_in_set(self):
        raw = '{"command": "ls"'
        assert extract_partial_string_fields(raw, {"path"}) == {}
