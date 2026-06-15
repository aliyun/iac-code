"""Tests for streaming summary extraction and candidate selection rollback behavior."""

from __future__ import annotations

from iac_code.utils.json_utils import extract_json_string_value


class TestExtractJsonStringValue:
    def test_extracts_complete_value(self):
        json_str = '{"candidate_name": "轻量单实例方案", "summary": "该方案使用单台ECS"}'
        assert extract_json_string_value(json_str, "candidate_name") == "轻量单实例方案"
        assert extract_json_string_value(json_str, "summary") == "该方案使用单台ECS"

    def test_partial_value_not_returned_by_default(self):
        json_str = '{"summary": "该方案使用单台'
        assert extract_json_string_value(json_str, "summary") is None

    def test_partial_value_returned_when_allowed(self):
        json_str = '{"summary": "该方案使用单台'
        assert extract_json_string_value(json_str, "summary", allow_partial=True) == "该方案使用单台"

    def test_handles_escaped_quotes(self):
        json_str = r'{"summary": "the \"best\" plan"}'
        assert extract_json_string_value(json_str, "summary") == 'the "best" plan'

    def test_handles_escaped_newlines(self):
        json_str = '{"summary": "line1\\nline2"}'
        assert extract_json_string_value(json_str, "summary") == "line1\nline2"

    def test_key_not_found(self):
        json_str = '{"other_key": "value"}'
        assert extract_json_string_value(json_str, "summary") is None

    def test_empty_string(self):
        assert extract_json_string_value("", "summary") is None

    def test_growing_accumulation(self):
        chunks = [
            '{"candidate_name": "方案A',
            '", "summary": "这',
            "是一个",
            "轻量级方案",
            '"}',
        ]
        accumulated = ""
        results = []
        for chunk in chunks:
            accumulated += chunk
            result = extract_json_string_value(accumulated, "summary", allow_partial=True)
            results.append(result)

        assert results[0] is None  # summary key not started yet
        assert results[1] == "这"
        assert results[2] == "这是一个"
        assert results[3] == "这是一个轻量级方案"
        assert results[4] == "这是一个轻量级方案"

    def test_candidate_name_complete_before_summary(self):
        json_str = '{"candidate_name": "高可用方案", "summary": "该方案使用'
        name = extract_json_string_value(json_str, "candidate_name")
        summary = extract_json_string_value(json_str, "summary", allow_partial=True)
        assert name == "高可用方案"
        assert summary == "该方案使用"

    def test_no_space_after_colon(self):
        json_str = '{"summary":"no space"}'
        assert extract_json_string_value(json_str, "summary") == "no space"

    def test_multiple_spaces_after_colon(self):
        json_str = '{"summary":  "extra spaces"}'
        assert extract_json_string_value(json_str, "summary") == "extra spaces"


class TestCandidateSelectionRendererStreaming:
    def test_update_streaming_summary_creates_tab(self):
        from rich.console import Console

        from iac_code.ui.components.candidate_selection import CandidateSelectionRenderer

        renderer = CandidateSelectionRenderer(console=Console(force_terminal=True))
        renderer.update_streaming_summary("方案A", "该方案")
        assert renderer.tab_count == 1
        tab = renderer._tabs[0]
        assert tab.candidate_name == "方案A"
        assert tab.summary == "该方案"

    def test_streaming_summary_updates_existing_tab(self):
        from rich.console import Console

        from iac_code.ui.components.candidate_selection import CandidateSelectionRenderer

        renderer = CandidateSelectionRenderer(console=Console(force_terminal=True))
        renderer.update_streaming_summary("方案A", "该方案")
        renderer.update_streaming_summary("方案A", "该方案使用单台ECS")
        assert renderer.tab_count == 1
        assert renderer._tabs[0].summary == "该方案使用单台ECS"

    def test_add_detail_overwrites_streaming_summary(self):
        from rich.console import Console

        from iac_code.ui.components.candidate_selection import CandidateSelectionRenderer

        renderer = CandidateSelectionRenderer(console=Console(force_terminal=True))
        renderer.update_streaming_summary("方案A", "partial summary")
        cost_items = [{"name": "ECS", "spec": "2C4G", "monthly_cost": "¥200"}]
        renderer.add_detail("tu_1", "方案A", "final summary", cost_items, "¥200/月")
        tab = renderer._tabs[0]
        assert tab.summary == "final summary"
        assert tab.cost_items == [{"name": "ECS", "spec": "2C4G", "monthly_cost": "¥200"}]
        assert tab.total_monthly_cost == "¥200/月"

    def test_diagram_tab_gets_streaming_summary(self):
        from rich.console import Console

        from iac_code.ui.components.candidate_selection import CandidateSelectionRenderer

        renderer = CandidateSelectionRenderer(console=Console(force_terminal=True))
        renderer.add_diagram("方案A", "graph TD; A-->B")
        renderer.update_streaming_summary("方案A", "streaming text")
        tab = renderer._tabs[0]
        assert tab.mermaid_source == "graph TD; A-->B"
        assert tab.summary == "streaming text"
