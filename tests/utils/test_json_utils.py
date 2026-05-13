from __future__ import annotations

from iac_code.utils.json_utils import parse_concatenated_json, safe_parse_json


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
