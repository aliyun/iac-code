from __future__ import annotations

from iac_code.utils.json_utils import extract_partial_string_fields, parse_concatenated_json, safe_parse_json


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
