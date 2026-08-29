"""Tests for streaming summary extraction and candidate selection rollback behavior."""

from __future__ import annotations

from io import StringIO

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
    @staticmethod
    def _render_to_text(renderable) -> str:
        from rich.console import Console

        output = StringIO()
        Console(file=output, force_terminal=True, no_color=True, width=120).print(renderable)
        return output.getvalue()

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

    def test_draft_diagram_keeps_optimization_status_until_optimized_views_arrive(self):
        from rich.console import Console

        from iac_code.ui.components.candidate_selection import CandidateSelectionRenderer

        renderer = CandidateSelectionRenderer(console=Console(force_terminal=True, width=120))
        renderer.add_diagram(
            "方案A",
            "graph TD; Draft-->B",
            diagram_stage="draft",
            views=[
                {
                    "id": "overview",
                    "title": "架构概览",
                    "mermaid_source": "graph TD; Draft-->B",
                }
            ],
        )

        rendered = self._render_to_text(renderer.render())

        assert "Optimizing architecture diagram" in rendered
        tab = renderer._tabs[0]
        assert tab.mermaid_source == "graph TD; Draft-->B"
        assert tab.diagram_stage == "draft"
        assert [view.title for view in tab.diagram_views] == ["架构概览"]

        renderer.add_diagram(
            "方案A",
            "graph TD; Optimized-->B",
            diagram_stage="optimized",
            views=[
                {
                    "id": "overview",
                    "title": "架构概览",
                    "mermaid_source": "graph TD; Optimized-->B",
                },
                {
                    "id": "detail_app",
                    "title": "应用详情",
                    "mermaid_source": "graph TD; App-->B",
                },
            ],
        )

        rendered_after_update = self._render_to_text(renderer.render())

        assert "Optimizing architecture diagram" not in rendered_after_update
        assert "架构概览" in rendered_after_update
        assert "应用详情" in rendered_after_update
        assert renderer._tabs[0].diagram_stage == "optimized"

    def test_bracket_keys_switch_diagram_views_inside_selected_candidate(self):
        from rich.console import Console

        from iac_code.ui.components.candidate_selection import CandidateSelectionRenderer
        from iac_code.ui.core.key_event import KeyEvent

        renderer = CandidateSelectionRenderer(console=Console(force_terminal=True, width=120))
        renderer.add_diagram(
            "方案A",
            "graph TD; Overview-->B",
            views=[
                {
                    "id": "overview",
                    "title": "架构概览",
                    "mermaid_source": "graph TD; Overview-->B",
                },
                {
                    "id": "detail_app",
                    "title": "应用详情",
                    "mermaid_source": "graph TD; App-->B",
                },
            ],
        )

        assert renderer._tabs[0].selected_diagram_view_index == 0
        assert renderer.handle_key(KeyEvent(key="]", char="]")) is True
        assert renderer._tabs[0].selected_diagram_view_index == 1
        assert "App" in self._render_to_text(renderer.render())
        assert renderer.handle_key(KeyEvent(key="[", char="[")) is True
        assert renderer._tabs[0].selected_diagram_view_index == 0
