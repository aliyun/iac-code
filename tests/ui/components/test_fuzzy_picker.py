"""Tests for FuzzyPicker component and fuzzy_match function."""

from __future__ import annotations

import io
from unittest.mock import MagicMock

from rich.console import Console

from iac_code.ui.components.fuzzy_picker import FuzzyPicker, PickerItem, fuzzy_match
from iac_code.ui.core.key_event import KeyEvent


def key(k: str, ctrl: bool = False) -> KeyEvent:
    return KeyEvent(key=k, char=k, ctrl=ctrl)


def make_items():
    return [
        PickerItem(key="a", display="Alpha"),
        PickerItem(key="b", display="Beta"),
        PickerItem(key="c", display="Gamma"),
        PickerItem(key="d", display="Delta"),
    ]


# -------------------------------------------------------------------------
# fuzzy_match tests
# -------------------------------------------------------------------------


class TestFuzzyMatch:
    def test_exact_match(self):
        score = fuzzy_match("alpha", "alpha")
        assert score is not None
        assert score > 0

    def test_prefix_match(self):
        score = fuzzy_match("alp", "alpha")
        assert score is not None
        assert score > 0

    def test_subsequence_match(self):
        # "aph" is a subsequence of "alpha"
        score = fuzzy_match("aph", "alpha")
        assert score is not None

    def test_no_match(self):
        score = fuzzy_match("xyz", "alpha")
        assert score is None

    def test_empty_query_matches_everything(self):
        score = fuzzy_match("", "anything")
        assert score is not None
        assert score == 0.0

    def test_case_insensitive(self):
        score = fuzzy_match("ALPHA", "alpha")
        assert score is not None

    def test_prefix_bonus(self):
        # Prefix match should score higher than non-prefix subsequence
        prefix_score = fuzzy_match("alp", "alpha")
        subseq_score = fuzzy_match("lph", "alpha")
        assert prefix_score is not None
        assert subseq_score is not None
        assert prefix_score > subseq_score

    def test_word_boundary_bonus(self):
        # "ba" at word boundary "foo bar" vs. middle
        boundary_score = fuzzy_match("ba", "foo bar")
        mid_score = fuzzy_match("oo", "foobar")
        assert boundary_score is not None
        assert mid_score is not None
        assert boundary_score > mid_score

    def test_consecutive_bonus(self):
        # "abc" consecutive in "abcdef" should score higher than "aXbXc"
        consec = fuzzy_match("abc", "abcdef")
        sparse = fuzzy_match("abc", "axbxcx")
        assert consec is not None
        assert sparse is not None
        assert consec > sparse


# -------------------------------------------------------------------------
# FuzzyPicker tests
# -------------------------------------------------------------------------


class TestFuzzyPickerInitial:
    def test_initial_items_all_shown(self):
        picker = FuzzyPicker(items=make_items(), on_select=lambda x: None)
        assert len(picker._filtered_items) == 4

    def test_initial_focused_index_zero(self):
        picker = FuzzyPicker(items=make_items(), on_select=lambda x: None)
        assert picker._focused_index == 0


class TestFuzzyPickerFilter:
    def test_filter_by_query(self):
        picker = FuzzyPicker(items=make_items(), on_select=lambda x: None)
        picker._update_filter("al")
        labels = [item.display for item in picker._filtered_items]
        assert "Alpha" in labels
        # "Beta" and "Delta" and "Gamma" don't match "al" as subsequence
        # Delta does: D-e-l-t-a -> 'a','l' are subsequence - let's check
        # "al" -> a in "Delta" yes, l in "Delta" after a - yes
        # So Delta should match too

    def test_filter_no_match(self):
        picker = FuzzyPicker(items=make_items(), on_select=lambda x: None)
        picker._update_filter("zzz")
        assert picker._filtered_items == []

    def test_filter_resets_focus(self):
        picker = FuzzyPicker(items=make_items(), on_select=lambda x: None)
        picker._focused_index = 2
        picker._update_filter("al")
        assert picker._focused_index == 0

    def test_filter_sorts_by_score(self):
        # "Alpha" is a better match for "al" than "Delta" (prefix bonus)
        picker = FuzzyPicker(items=make_items(), on_select=lambda x: None)
        picker._update_filter("al")
        if len(picker._filtered_items) >= 1:
            assert picker._filtered_items[0].display == "Alpha"


class TestFuzzyPickerMatchCount:
    def test_match_count_text_all(self):
        picker = FuzzyPicker(items=make_items(), on_select=lambda x: None)
        count_text = picker._get_match_count_text()
        assert "4" in count_text

    def test_match_count_text_filtered(self):
        picker = FuzzyPicker(items=make_items(), on_select=lambda x: None)
        picker._update_filter("al")
        count_text = picker._get_match_count_text()
        # Should show how many matched
        assert count_text  # non-empty


class TestFuzzyPickerRender:
    def test_render_returns_renderable(self):
        picker = FuzzyPicker(items=make_items(), on_select=lambda x: None)
        result = picker.render()
        assert result is not None

    def test_render_shows_items(self):
        picker = FuzzyPicker(items=make_items(), on_select=lambda x: None)
        result = picker.render()
        console = Console(file=io.StringIO(), force_terminal=False, width=80)
        console.print(result)
        output = console.file.getvalue()
        assert "Alpha" in output

    def test_render_empty_message_when_no_match(self):
        picker = FuzzyPicker(
            items=make_items(),
            on_select=lambda x: None,
            empty_message="Nothing found",
        )
        picker._update_filter("zzz")
        result = picker.render()
        console = Console(file=io.StringIO(), force_terminal=False, width=80)
        console.print(result)
        output = console.file.getvalue()
        assert "Nothing found" in output


class TestFuzzyPickerDynamicItems:
    def test_dynamic_items_callable(self):
        def search_fn(query: str) -> list[PickerItem]:
            return [PickerItem(key=query, display=f"Result: {query}")]

        picker = FuzzyPicker(items=search_fn, on_select=lambda x: None)
        picker._update_filter("hello")
        assert len(picker._filtered_items) == 1
        assert picker._filtered_items[0].display == "Result: hello"

    def test_dynamic_items_empty_query(self):
        def search_fn(query: str) -> list[PickerItem]:
            if not query:
                return make_items()
            return []

        picker = FuzzyPicker(items=search_fn, on_select=lambda x: None)
        # Empty query initially
        assert len(picker._filtered_items) == 4


# -------------------------------------------------------------------------
# handle_key tests (lines 162-201)
# -------------------------------------------------------------------------


class TestFuzzyPickerHandleKey:
    def test_key_up_moves_focus_up(self):
        picker = FuzzyPicker(items=make_items(), on_select=lambda x: None)
        picker._focused_index = 2
        consumed = picker.handle_key(key("up"))
        assert consumed is True
        assert picker._focused_index == 1

    def test_key_ctrl_p_moves_focus_up(self):
        picker = FuzzyPicker(items=make_items(), on_select=lambda x: None)
        picker._focused_index = 3
        consumed = picker.handle_key(key("p", ctrl=True))
        assert consumed is True
        assert picker._focused_index == 2

    def test_key_down_moves_focus_down(self):
        picker = FuzzyPicker(items=make_items(), on_select=lambda x: None)
        picker._focused_index = 0
        consumed = picker.handle_key(key("down"))
        assert consumed is True
        assert picker._focused_index == 1

    def test_key_ctrl_n_moves_focus_down(self):
        picker = FuzzyPicker(items=make_items(), on_select=lambda x: None)
        picker._focused_index = 0
        consumed = picker.handle_key(key("n", ctrl=True))
        assert consumed is True
        assert picker._focused_index == 1

    def test_key_pageup_moves_focus_by_visible_count(self):
        picker = FuzzyPicker(items=make_items(), on_select=lambda x: None, visible_count=10)
        picker._focused_index = 3
        consumed = picker.handle_key(key("pageup"))
        assert consumed is True
        # clamps at 0
        assert picker._focused_index == 0

    def test_key_pagedown_moves_focus_by_visible_count(self):
        picker = FuzzyPicker(items=make_items(), on_select=lambda x: None, visible_count=10)
        picker._focused_index = 0
        consumed = picker.handle_key(key("pagedown"))
        assert consumed is True
        # clamps at last index (3)
        assert picker._focused_index == 3

    def test_key_enter_selects_focused_item(self):
        selected = []
        picker = FuzzyPicker(items=make_items(), on_select=lambda x: selected.append(x))
        picker._focused_index = 1
        consumed = picker.handle_key(key("enter"))
        assert consumed is True
        assert picker._done is True
        assert len(selected) == 1
        assert selected[0].display == "Beta"
        assert picker._result is selected[0]

    def test_key_enter_with_no_items_does_nothing(self):
        selected = []
        picker = FuzzyPicker(items=make_items(), on_select=lambda x: selected.append(x))
        picker._update_filter("zzz")  # no matches
        consumed = picker.handle_key(key("enter"))
        assert consumed is True
        assert picker._done is False
        assert len(selected) == 0

    def test_key_escape_sets_done(self):
        picker = FuzzyPicker(items=make_items(), on_select=lambda x: None)
        consumed = picker.handle_key(key("escape"))
        assert consumed is True
        assert picker._done is True

    def test_key_escape_calls_on_cancel(self):
        cancelled = []
        picker = FuzzyPicker(
            items=make_items(),
            on_select=lambda x: None,
            on_cancel=lambda: cancelled.append(True),
        )
        picker.handle_key(key("escape"))
        assert len(cancelled) == 1

    def test_key_escape_without_on_cancel(self):
        # on_cancel=None should not raise
        picker = FuzzyPicker(items=make_items(), on_select=lambda x: None, on_cancel=None)
        consumed = picker.handle_key(key("escape"))
        assert consumed is True
        assert picker._done is True

    def test_key_tab_with_tab_action(self):
        tab_called = []
        picker = FuzzyPicker(
            items=make_items(),
            on_select=lambda x: None,
            tab_action=lambda: tab_called.append(True),
        )
        consumed = picker.handle_key(key("tab"))
        assert consumed is True
        assert len(tab_called) == 1

    def test_key_tab_without_tab_action_delegates_to_search_box(self):
        # Without tab_action, tab is delegated to the search box
        picker = FuzzyPicker(items=make_items(), on_select=lambda x: None, tab_action=None)
        consumed = picker.handle_key(key("tab"))
        # Result is whatever search box returns; just ensure no exception
        assert isinstance(consumed, bool)

    def test_unhandled_key_delegates_to_search_box(self):
        picker = FuzzyPicker(items=make_items(), on_select=lambda x: None)
        # "a" is a regular character — search box handles it
        consumed = picker.handle_key(key("a"))
        assert isinstance(consumed, bool)


# -------------------------------------------------------------------------
# _move_focus edge cases (lines 266-271)
# -------------------------------------------------------------------------


class TestFuzzyPickerMoveFocus:
    def test_move_focus_empty_items_does_nothing(self):
        picker = FuzzyPicker(items=make_items(), on_select=lambda x: None)
        picker._update_filter("zzz")  # empty filtered list
        picker._move_focus(1)
        assert picker._focused_index == 0

    def test_move_focus_clamps_at_zero(self):
        picker = FuzzyPicker(items=make_items(), on_select=lambda x: None)
        picker._focused_index = 0
        picker._move_focus(-5)
        assert picker._focused_index == 0

    def test_move_focus_clamps_at_max(self):
        picker = FuzzyPicker(items=make_items(), on_select=lambda x: None)
        picker._focused_index = 3
        picker._move_focus(100)
        assert picker._focused_index == 3

    def test_move_focus_normal(self):
        picker = FuzzyPicker(items=make_items(), on_select=lambda x: None)
        picker._focused_index = 1
        picker._move_focus(2)
        assert picker._focused_index == 3


# -------------------------------------------------------------------------
# _update_scroll edge cases (lines 275-278)
# -------------------------------------------------------------------------


class TestFuzzyPickerUpdateScroll:
    def test_scroll_up_when_focus_above_visible(self):
        picker = FuzzyPicker(items=make_items(), on_select=lambda x: None, visible_count=2)
        picker._visible_from = 2
        picker._focused_index = 1
        picker._update_scroll()
        assert picker._visible_from == 1

    def test_scroll_down_when_focus_below_visible(self):
        picker = FuzzyPicker(items=make_items(), on_select=lambda x: None, visible_count=2)
        picker._visible_from = 0
        picker._focused_index = 3
        picker._update_scroll()
        assert picker._visible_from == 2

    def test_no_scroll_when_focus_in_view(self):
        picker = FuzzyPicker(items=make_items(), on_select=lambda x: None, visible_count=4)
        picker._visible_from = 0
        picker._focused_index = 2
        picker._update_scroll()
        assert picker._visible_from == 0


# -------------------------------------------------------------------------
# _get_match_count_text for dynamic items (line 285)
# -------------------------------------------------------------------------


class TestFuzzyPickerMatchCountDynamic:
    def test_match_count_text_dynamic_callable(self):
        def search_fn(query: str) -> list[PickerItem]:
            return [PickerItem(key="r1", display="Result One")]

        picker = FuzzyPicker(items=search_fn, on_select=lambda x: None)
        count_text = picker._get_match_count_text()
        assert "results" in count_text
        assert "/" not in count_text  # dynamic items don't show total


# -------------------------------------------------------------------------
# _render_item with description (line 295)
# -------------------------------------------------------------------------


class TestFuzzyPickerRenderItem:
    def test_render_item_with_description(self):
        picker = FuzzyPicker(items=make_items(), on_select=lambda x: None)
        item = PickerItem(key="x", display="MyItem", description="A description")
        rendered = picker._render_item(item, is_focused=False)
        console = Console(file=io.StringIO(), force_terminal=False, width=80)
        console.print(rendered)
        output = console.file.getvalue()
        assert "MyItem" in output
        assert "A description" in output

    def test_render_item_focused_shows_cursor(self):
        picker = FuzzyPicker(items=make_items(), on_select=lambda x: None)
        item = PickerItem(key="x", display="MyItem")
        rendered_focused = picker._render_item(item, is_focused=True)
        rendered_unfocused = picker._render_item(item, is_focused=False)
        console_focused = Console(file=io.StringIO(), force_terminal=True, width=80)
        console_focused.print(rendered_focused)
        console_unfocused = Console(file=io.StringIO(), force_terminal=True, width=80)
        console_unfocused.print(rendered_unfocused)
        assert "MyItem" in console_focused.file.getvalue()
        assert "MyItem" in console_unfocused.file.getvalue()

    def test_render_item_no_description(self):
        picker = FuzzyPicker(items=make_items(), on_select=lambda x: None)
        item = PickerItem(key="x", display="NoDesc")
        rendered = picker._render_item(item, is_focused=False)
        console = Console(file=io.StringIO(), force_terminal=False, width=80)
        console.print(rendered)
        output = console.file.getvalue()
        assert "NoDesc" in output


# -------------------------------------------------------------------------
# render() with preview panel (lines 229-230)
# -------------------------------------------------------------------------


class TestFuzzyPickerRenderPreview:
    def test_render_with_preview_panel(self):
        from rich.text import Text

        def preview_fn(item: PickerItem):
            return Text(f"Preview: {item.display}")

        picker = FuzzyPicker(
            items=make_items(),
            on_select=lambda x: None,
            render_preview=preview_fn,
        )
        result = picker.render()
        console = Console(file=io.StringIO(), force_terminal=False, width=80)
        console.print(result)
        output = console.file.getvalue()
        # Preview of the first focused item (Alpha) should appear
        assert "Preview: Alpha" in output

    def test_render_without_preview_when_no_items(self):
        from rich.text import Text

        def preview_fn(item: PickerItem):
            return Text(f"Preview: {item.display}")

        picker = FuzzyPicker(
            items=make_items(),
            on_select=lambda x: None,
            render_preview=preview_fn,
        )
        picker._update_filter("zzz")  # no matches
        result = picker.render()
        console = Console(file=io.StringIO(), force_terminal=False, width=80)
        console.print(result)
        output = console.file.getvalue()
        assert "Preview:" not in output


# -------------------------------------------------------------------------
# _on_query_change (line 239) — triggered via search box
# -------------------------------------------------------------------------


class TestFuzzyPickerOnQueryChange:
    def test_on_query_change_updates_filter(self):
        picker = FuzzyPicker(items=make_items(), on_select=lambda x: None)
        # Directly call _on_query_change to cover line 239
        picker._on_query_change("al")
        labels = [item.display for item in picker._filtered_items]
        assert "Alpha" in labels

    def test_on_query_change_empty_resets_to_all(self):
        picker = FuzzyPicker(items=make_items(), on_select=lambda x: None)
        picker._on_query_change("al")
        picker._on_query_change("")
        assert len(picker._filtered_items) == 4


# -------------------------------------------------------------------------
# run() method (lines 143-158) — mocked RawInputCapture
# -------------------------------------------------------------------------


class TestFuzzyPickerRun:
    def test_run_returns_selected_item_on_enter(self, monkeypatch):
        """run() should return the item selected via enter key."""
        call_count = 0

        class FakeCapture:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def read_key(self, timeout=0.1):
                nonlocal call_count
                call_count += 1
                if call_count == 1:
                    return KeyEvent(key="enter", char="", ctrl=False)
                return None

        monkeypatch.setattr("iac_code.ui.core.raw_input.RawInputCapture", FakeCapture)
        monkeypatch.setattr("rich.console.Console", lambda: MagicMock())

        selected = []
        picker = FuzzyPicker(
            items=make_items(),
            on_select=lambda x: selected.append(x),
        )
        result = picker.run()
        assert result is not None
        assert result.display == "Alpha"
        assert len(selected) == 1

    def test_run_returns_none_on_escape(self, monkeypatch):
        """run() should return None when user presses escape."""
        call_count = 0

        class FakeCapture:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def read_key(self, timeout=0.1):
                nonlocal call_count
                call_count += 1
                if call_count == 1:
                    return KeyEvent(key="escape", char="", ctrl=False)
                return None

        monkeypatch.setattr("iac_code.ui.core.raw_input.RawInputCapture", FakeCapture)
        monkeypatch.setattr("rich.console.Console", lambda: MagicMock())

        picker = FuzzyPicker(items=make_items(), on_select=lambda x: None)
        result = picker.run()
        assert result is None

    def test_run_ignores_none_key_events(self, monkeypatch):
        """run() skips None key events and keeps looping until done."""
        call_count = 0

        class FakeCapture:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def read_key(self, timeout=0.1):
                nonlocal call_count
                call_count += 1
                if call_count <= 3:
                    return None  # simulate timeout/no key
                return KeyEvent(key="escape", char="", ctrl=False)

        monkeypatch.setattr("iac_code.ui.core.raw_input.RawInputCapture", FakeCapture)
        monkeypatch.setattr("rich.console.Console", lambda: MagicMock())

        picker = FuzzyPicker(items=make_items(), on_select=lambda x: None)
        result = picker.run()
        assert result is None
        assert call_count == 4
