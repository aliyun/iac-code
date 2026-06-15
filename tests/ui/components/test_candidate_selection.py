"""Tests for CandidateSelectionRenderer component."""

from io import StringIO

from rich.console import Console

from iac_code.ui.components.candidate_selection import CandidateSelectionRenderer
from iac_code.ui.core.key_event import KeyEvent


def key(k):
    return KeyEvent(key=k, char=k, ctrl=False)


def _make_renderer():
    console = Console(file=StringIO(), force_terminal=False, width=120)
    r = CandidateSelectionRenderer(console=console)
    return r, console


class TestCandidateSelectionRendererTabManagement:
    def test_add_diagram_creates_tab(self):
        r, _ = _make_renderer()
        r.add_diagram("方案1", "graph TD\n  A-->B")
        assert r.tab_count == 1
        assert r.selected_index == 0

    def test_add_detail_creates_tab(self):
        r, _ = _make_renderer()
        r.add_detail("tu_1", "方案1", "摘要", [{"name": "ECS", "spec": "1C2G", "monthly_cost": "¥50"}], "¥50/月")
        assert r.tab_count == 1

    def test_add_diagram_and_detail_same_candidate(self):
        r, _ = _make_renderer()
        r.add_diagram("方案1", "graph TD")
        r.add_detail("tu_1", "方案1", "摘要", [], "¥0")
        assert r.tab_count == 1

    def test_add_detail_two_tool_use_ids_same_candidate_preserves_both(self):
        """U-I14: two ``show_candidate_detail`` invocations with the same
        ``candidate_name`` but different ``tool_use_id`` must each be preserved
        in the renderer accumulator instead of clobbering the previous entry."""
        r, _ = _make_renderer()
        r.add_detail(
            "tu_a",
            "方案1",
            "first version",
            [{"name": "ECS", "spec": "1C2G", "monthly_cost": "¥50"}],
            "¥50/月",
        )
        r.add_detail(
            "tu_b",
            "方案1",
            "second version",
            [{"name": "ECS", "spec": "2C4G", "monthly_cost": "¥100"}],
            "¥100/月",
        )
        # Still a single visible tab because both share candidate_name.
        assert r.tab_count == 1
        tab = r._tabs[0]
        # Both invocations are preserved keyed by tool_use_id.
        assert set(tab.details_by_tool_use_id.keys()) == {"tu_a", "tu_b"}
        assert tab.details_by_tool_use_id["tu_a"].summary == "first version"
        assert tab.details_by_tool_use_id["tu_a"].total_monthly_cost == "¥50/月"
        assert tab.details_by_tool_use_id["tu_b"].summary == "second version"
        assert tab.details_by_tool_use_id["tu_b"].total_monthly_cost == "¥100/月"
        # Convenience fields hold the most recently received entry.
        assert tab.summary == "second version"
        assert tab.total_monthly_cost == "¥100/月"

    def test_add_multiple_candidates(self):
        r, _ = _make_renderer()
        r.add_diagram("方案1", "graph TD")
        r.add_diagram("方案2", "graph TD")
        assert r.tab_count == 2

    def test_duplicate_candidate_names_with_indexes_stay_distinct(self):
        r, _ = _make_renderer()
        r.add_diagram("Same", "graph TD\nA-->B", candidate_index=0)
        r.add_diagram("Same", "graph TD\nC-->D", candidate_index=1)

        assert r.tab_count == 2
        assert r.selected_index == 0
        assert r.confirm_selection().selected_candidate_index == 0
        r.handle_key(key("right"))
        assert r.confirm_selection().selected_candidate_index == 1

    def test_seed_candidates_creates_placeholders_for_missing_tool_events(self):
        r, console = _make_renderer()
        r.seed_candidates(
            [
                {"name": "Plan A", "summary": "ready", "candidate_index": 0},
                {"name": "Plan B", "summary": "missing display", "candidate_index": 1},
            ]
        )
        r.add_diagram("Plan A", "graph TD\nA-->B", candidate_index=0)
        r.add_detail("tu_a", "Plan A", "ready", [], "¥0/月", candidate_index=0)
        r.enter_selection_mode()

        console.print(r.render())
        output = console.file.getvalue()

        assert r.tab_count == 2
        assert "Plan B" in output
        assert "Plan B missing architecture diagram/details" in output
        r.handle_key(key("right"))
        assert r.confirm_selection().selected_candidate_index == 1

    def test_streaming_summary_merges_into_later_indexed_detail(self):
        r, _ = _make_renderer()
        r.update_streaming_summary("Same", "partial")
        r.add_detail("tu_same", "Same", "full", [], "¥0/月", candidate_index=0)

        assert r.tab_count == 1
        selection = r.confirm_selection()
        assert selection.selected_candidate_name == "Same"
        assert selection.selected_candidate_index == 0


class TestCandidateSelectionRendererKeyHandling:
    def test_switch_right(self):
        r, _ = _make_renderer()
        r.add_diagram("方案1", "graph TD")
        r.add_diagram("方案2", "graph TD")
        r.handle_key(key("right"))
        assert r.selected_index == 1

    def test_switch_left(self):
        r, _ = _make_renderer()
        r.add_diagram("方案1", "graph TD")
        r.add_diagram("方案2", "graph TD")
        r.handle_key(key("right"))
        r.handle_key(key("left"))
        assert r.selected_index == 0

    def test_no_wrap_right(self):
        r, _ = _make_renderer()
        r.add_diagram("方案1", "graph TD")
        r.handle_key(key("right"))
        assert r.selected_index == 0

    def test_number_key_jump(self):
        r, _ = _make_renderer()
        r.add_diagram("方案1", "graph TD")
        r.add_diagram("方案2", "graph TD")
        r.add_diagram("方案3", "graph TD")
        r.handle_key(key("2"))
        assert r.selected_index == 1

    def test_scroll_down(self):
        r, _ = _make_renderer()
        r.add_diagram("方案1", "graph TD")
        r.handle_key(key("down"))
        assert r._scroll_offset == 3

    def test_scroll_up(self):
        r, _ = _make_renderer()
        r.add_diagram("方案1", "graph TD")
        r._scroll_offset = 6
        r.handle_key(key("up"))
        assert r._scroll_offset == 3

    def test_scroll_up_clamps_to_zero(self):
        r, _ = _make_renderer()
        r.add_diagram("方案1", "graph TD")
        r._scroll_offset = 1
        r.handle_key(key("up"))
        assert r._scroll_offset == 0

    def test_tab_switch_resets_scroll(self):
        r, _ = _make_renderer()
        r.add_diagram("方案1", "graph TD")
        r.add_diagram("方案2", "graph TD")
        r._scroll_offset = 10
        r.handle_key(key("right"))
        assert r._scroll_offset == 0

    def test_unhandled_key_returns_false(self):
        r, _ = _make_renderer()
        r.add_diagram("方案1", "graph TD")
        assert r.handle_key(key("x")) is False


class TestCandidateSelectionRendererRendering:
    def test_render_returns_renderable(self):
        r, _ = _make_renderer()
        r.add_diagram("方案1", "graph TD\n  A-->B")
        r.add_detail("tu_1", "方案1", "简单方案", [{"name": "ECS", "spec": "1C2G", "monthly_cost": "¥50"}], "¥50/月")
        result = r.render()
        assert result is not None

    def test_render_contains_tab_title(self):
        r, console = _make_renderer()
        r.add_diagram("方案1", "graph TD")
        result = r.render()
        console.print(result)
        output = console.file.getvalue()
        assert "方案1" in output

    def test_render_contains_cost_table(self):
        r, console = _make_renderer()
        r.add_detail("tu_1", "方案1", "desc", [{"name": "ECS", "spec": "1C2G", "monthly_cost": "¥50"}], "¥50/月")
        result = r.render()
        console.print(result)
        output = console.file.getvalue()
        assert "ECS" in output
        assert "¥50" in output

    def test_render_placeholder_when_diagram_missing(self):
        r, console = _make_renderer()
        r.add_detail("tu_1", "方案1", "desc", [], "¥0")
        result = r.render()
        console.print(result)
        output = console.file.getvalue()
        assert "Loading" in output

    def test_render_hint_with_multiple_tabs(self):
        r, console = _make_renderer()
        r.add_diagram("方案1", "graph TD")
        r.add_diagram("方案2", "graph TD")
        result = r.render()
        console.print(result)
        output = console.file.getvalue()
        assert "←" in output or "切换" in output

    def test_completeness_warning_mentions_missing_diagram_and_detail(self):
        r, console = _make_renderer()
        r.add_diagram("方案A", "graph TD", candidate_index=0)
        r.add_detail("tu_b", "方案B", "summary", [], "¥0/月", candidate_index=1)
        r.enter_selection_mode()

        console.print(r.render())
        output = console.file.getvalue()

        assert "missing" in output
        assert "方案A" in output
        assert "方案B" in output
        assert "details" in output
        assert "architecture diagram" in output

    def test_duplicate_candidate_names_get_distinct_display_labels(self):
        r, console = _make_renderer()
        r.add_diagram("Same", "graph TD\nA-->B", candidate_index=0)
        r.add_diagram("Same", "graph TD\nC-->D", candidate_index=1)

        console.print(r.render())
        output = console.file.getvalue()

        assert "Same #1" in output
        assert "Same #2" in output


class TestCandidateSelectionRendererSelection:
    def test_enter_selection_mode(self):
        r, _ = _make_renderer()
        r.add_diagram("方案1", "graph TD")
        r.enter_selection_mode()
        assert r.is_selecting is True

    def test_confirm_selection(self):
        r, _ = _make_renderer()
        r.add_diagram("方案1", "graph TD")
        r.add_diagram("方案2", "graph TD")
        r.handle_key(key("right"))
        r.enter_selection_mode()
        selection = r.confirm_selection()
        assert selection.selected_candidate_name == "方案2"
        assert selection.selected_candidate_index is None


class TestRenderDiagramFallback:
    """Regression: termaid render failures must log a warning (U-C3)."""

    def test_render_diagram_logs_warning_on_termaid_failure(self, monkeypatch):
        # Use a fake module so the test runs even when the optional `termaid`
        # extra is not installed (it is gated on python_version >= '3.11').
        import sys
        import types

        from loguru import logger

        r, _ = _make_renderer()

        def broken_render(_):
            raise ValueError("invalid mermaid syntax")

        fake_termaid = types.SimpleNamespace(render_rich=broken_render)
        monkeypatch.setitem(sys.modules, "termaid", fake_termaid)
        records: list[str] = []
        handler_id = logger.add(lambda msg: records.append(str(msg)), level="WARNING")
        try:
            result = r._render_diagram("graph TD\n A-->B")
        finally:
            logger.remove(handler_id)

        # Falls back without raising
        assert result is not None
        # Non-ImportError is logged with enough detail to debug
        assert any("termaid render failed" in rec for rec in records), (
            f"Expected 'termaid render failed' warning, got: {records}"
        )

    def test_render_diagram_silent_when_termaid_missing(self, monkeypatch):
        import sys

        from loguru import logger

        r, _ = _make_renderer()
        # Make `from termaid import render_rich` raise ImportError
        monkeypatch.setitem(sys.modules, "termaid", None)
        records: list[str] = []
        handler_id = logger.add(lambda msg: records.append(str(msg)), level="WARNING")
        try:
            result = r._render_diagram("graph TD\n A-->B")
        finally:
            logger.remove(handler_id)

        assert result is not None
        # ImportError is silent — termaid is an optional dep, no warning needed
        assert not any("termaid render failed" in rec for rec in records)
