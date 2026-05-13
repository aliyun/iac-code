"""Tests for HistorySearch dialog."""

from __future__ import annotations

from iac_code.ui.dialogs.history_search import HistorySearch


class TestHistorySearch:
    def test_create(self):
        """Constructor should work without error."""
        hs = HistorySearch(
            messages=[],
            on_select=lambda text: None,
            on_cancel=lambda: None,
        )
        assert hs is not None

    def test_builds_picker_items(self):
        """3 messages (2 user + 1 assistant) should produce 2 items, most recent first."""
        messages = [
            {"role": "user", "content": "First user message"},
            {"role": "assistant", "content": "Assistant reply"},
            {"role": "user", "content": "Second user message"},
        ]
        hs = HistorySearch(
            messages=messages,
            on_select=lambda text: None,
            on_cancel=lambda: None,
        )
        items = hs._build_items()
        assert len(items) == 2
        # Most recent first
        assert items[0].display == "Second user message"
        assert items[1].display == "First user message"

    def test_filter_text_is_full_content(self):
        """filter_text should be the full message content."""
        long_content = "A" * 200
        messages = [{"role": "user", "content": long_content}]
        hs = HistorySearch(
            messages=messages,
            on_select=lambda text: None,
            on_cancel=lambda: None,
        )
        items = hs._build_items()
        assert len(items) == 1
        assert items[0].display == long_content[:80]
        assert items[0].filter_text == long_content
        assert items[0].metadata == long_content

    def test_empty_messages(self):
        """Empty message list should produce no items."""
        hs = HistorySearch(
            messages=[],
            on_select=lambda text: None,
            on_cancel=lambda: None,
        )
        items = hs._build_items()
        assert items == []

    def test_only_assistant_messages(self):
        """Assistant-only messages should produce no items."""
        messages = [
            {"role": "assistant", "content": "Hello"},
            {"role": "assistant", "content": "World"},
        ]
        hs = HistorySearch(
            messages=messages,
            on_select=lambda text: None,
            on_cancel=lambda: None,
        )
        items = hs._build_items()
        assert items == []

    # ------------------------------------------------------------------
    # Tests for structured (list) content blocks (lines 78-79)
    # ------------------------------------------------------------------

    def test_build_items_with_list_content(self):
        """Content as a list of dicts with 'text' key should be joined."""
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Hello"},
                    {"type": "text", "text": "World"},
                ],
            }
        ]
        hs = HistorySearch(
            messages=messages,
            on_select=lambda text: None,
            on_cancel=lambda: None,
        )
        items = hs._build_items()
        assert len(items) == 1
        assert items[0].metadata == "Hello World"
        assert items[0].filter_text == "Hello World"

    def test_build_items_with_list_content_non_dict_blocks(self):
        """Content list with non-dict elements should be stringified."""
        messages = [
            {
                "role": "user",
                "content": ["plain string", {"type": "text", "text": "structured"}],
            }
        ]
        hs = HistorySearch(
            messages=messages,
            on_select=lambda text: None,
            on_cancel=lambda: None,
        )
        items = hs._build_items()
        assert len(items) == 1
        assert "plain string" in items[0].metadata
        assert "structured" in items[0].metadata

    def test_build_items_with_list_content_missing_text_key(self):
        """Dict block without 'text' key should contribute empty string."""
        messages = [
            {
                "role": "user",
                "content": [{"type": "image"}, {"type": "text", "text": "caption"}],
            }
        ]
        hs = HistorySearch(
            messages=messages,
            on_select=lambda text: None,
            on_cancel=lambda: None,
        )
        items = hs._build_items()
        assert len(items) == 1
        # image block contributes "" so joined result starts with space or just the caption
        assert "caption" in items[0].metadata

    # ------------------------------------------------------------------
    # Tests for _render_preview (lines 93-96)
    # ------------------------------------------------------------------

    def test_render_preview_returns_panel(self):
        """_render_preview should return a rich Panel containing the message text."""
        from rich.panel import Panel

        from iac_code.ui.components.fuzzy_picker import PickerItem

        hs = HistorySearch(
            messages=[],
            on_select=lambda text: None,
            on_cancel=lambda: None,
        )
        item = PickerItem(key="k", display="short", metadata="Full message content here")
        panel = hs._render_preview(item)
        assert isinstance(panel, Panel)

    def test_render_preview_content_matches_metadata(self):
        """The panel renderable should contain the item's metadata text."""
        from rich.text import Text

        from iac_code.ui.components.fuzzy_picker import PickerItem

        hs = HistorySearch(
            messages=[],
            on_select=lambda text: None,
            on_cancel=lambda: None,
        )
        item = PickerItem(key="k", display="short", metadata="Unique preview content")
        panel = hs._render_preview(item)
        # The panel's renderable is a Text object wrapping the metadata
        assert isinstance(panel.renderable, Text)
        assert str(panel.renderable) == "Unique preview content"

    # ------------------------------------------------------------------
    # Tests for run() method (lines 38-63)
    # ------------------------------------------------------------------

    def test_run_returns_selected_content(self):
        """run() should return the selected message content when a selection is made."""
        from unittest.mock import patch

        selected: list[str] = []

        def capture_select(text: str) -> None:
            selected.append(text)

        messages = [{"role": "user", "content": "hello from history"}]
        hs = HistorySearch(
            messages=messages,
            on_select=capture_select,
            on_cancel=lambda: None,
        )

        # Simulate FuzzyPicker.run() calling on_select with the first item
        def fake_picker_run(self_picker: object) -> None:
            # self_picker._on_select is the closure defined inside hs.run()
            item = hs._build_items()[0]
            self_picker._on_select(item)  # type: ignore[attr-defined]

        with patch(
            "iac_code.ui.dialogs.history_search.FuzzyPicker.run",
            fake_picker_run,
        ):
            result = hs.run()

        assert result == "hello from history"
        assert selected == ["hello from history"]

    def test_run_returns_none_on_cancel(self):
        """run() should return None when the picker is cancelled."""
        from unittest.mock import patch

        cancelled: list[bool] = []

        def capture_cancel() -> None:
            cancelled.append(True)

        messages = [{"role": "user", "content": "some message"}]
        hs = HistorySearch(
            messages=messages,
            on_select=lambda text: None,
            on_cancel=capture_cancel,
        )

        def fake_picker_run(self_picker: object) -> None:
            self_picker._on_cancel()  # type: ignore[attr-defined]

        with patch(
            "iac_code.ui.dialogs.history_search.FuzzyPicker.run",
            fake_picker_run,
        ):
            result = hs.run()

        assert result is None
        assert cancelled == [True]

    def test_run_passes_keybinding_manager(self):
        """run() should forward the keybinding_manager to FuzzyPicker."""
        from unittest.mock import MagicMock, patch

        km = MagicMock()
        captured_km: list[object] = []

        class CapturePicker:
            def __init__(self, **kwargs: object) -> None:
                captured_km.append(kwargs.get("keybinding_manager"))
                self._on_cancel = kwargs["on_cancel"]

            def run(self) -> None:
                self._on_cancel()

        with patch("iac_code.ui.dialogs.history_search.FuzzyPicker", CapturePicker):
            hs = HistorySearch(
                messages=[],
                on_select=lambda text: None,
                on_cancel=lambda: None,
                keybinding_manager=km,
            )
            hs.run()

        assert captured_km[0] is km
