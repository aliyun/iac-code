"""Tests for GlobalSearch dialog."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from rich.panel import Panel

from iac_code.ui.components.fuzzy_picker import PickerItem
from iac_code.ui.dialogs.global_search import GlobalSearch


class TestGlobalSearch:
    def test_create(self, tmp_path):
        """Constructor should work without error."""
        gs = GlobalSearch(
            root_dir=str(tmp_path),
            on_select=lambda result: None,
            on_cancel=lambda: None,
        )
        assert gs is not None

    def test_search_finds_hello(self, tmp_path):
        """Searching 'hello' in a sample tree should return items matching main.py."""
        (tmp_path / "main.py").write_text("print('hello world')\nx = 1\n")
        (tmp_path / "other.py").write_text("y = 2\n")

        gs = GlobalSearch(
            root_dir=str(tmp_path),
            on_select=lambda result: None,
            on_cancel=lambda: None,
        )
        items = gs._search("hello")
        assert len(items) >= 1
        # At least one item should reference main.py
        assert any("main.py" in item.display for item in items)

    def test_search_empty_query_returns_empty(self, tmp_path):
        """Empty query should return no items."""
        (tmp_path / "main.py").write_text("hello")
        gs = GlobalSearch(
            root_dir=str(tmp_path),
            on_select=lambda result: None,
            on_cancel=lambda: None,
        )
        items = gs._search("")
        assert items == []

    def test_parse_results(self, tmp_path):
        """_parse_results should correctly parse grep/rg output format."""
        gs = GlobalSearch(
            root_dir=str(tmp_path),
            on_select=lambda result: None,
            on_cancel=lambda: None,
        )
        main_py = str(tmp_path / "main.py")
        utils_py = str(tmp_path / "utils.py")
        fake_output = f"{main_py}:3:hello world\n{utils_py}:10:hello again\n"
        items = gs._parse_results(fake_output)
        assert len(items) == 2
        # Check keys have file:lineno format
        assert items[0].key == f"{main_py}:3"
        assert items[1].key == f"{utils_py}:10"
        # Metadata should be dict with file_path and lineno
        assert items[0].metadata["lineno"] == 3
        assert items[1].metadata["lineno"] == 10

    def test_search_no_match(self, tmp_path):
        """Searching for a non-existent string should return empty list."""
        (tmp_path / "main.py").write_text("print('hello')\n")
        gs = GlobalSearch(
            root_dir=str(tmp_path),
            on_select=lambda result: None,
            on_cancel=lambda: None,
        )
        items = gs._search("zzz_unlikely_string_xyz_999")
        assert items == []

    # ------------------------------------------------------------------
    # run() method — lines 43-69
    # ------------------------------------------------------------------

    def test_run_returns_key_on_select(self, tmp_path):
        """run() should return the key of the selected item via on_select callback."""
        selected_results: list[str] = []

        def on_select(result: str) -> None:
            selected_results.append(result)

        gs = GlobalSearch(
            root_dir=str(tmp_path),
            on_select=on_select,
            on_cancel=lambda: None,
        )

        fake_item = PickerItem(
            key=f"{tmp_path}/main.py:5",
            display="main.py:5  hello",
            metadata={"file_path": str(tmp_path / "main.py"), "lineno": 5, "text": "hello"},
        )

        with patch("iac_code.ui.dialogs.global_search.FuzzyPicker") as mock_fuzzy_picker:
            mock_picker_instance = MagicMock()

            captured_on_select = {}

            def capture_init(*args, **kwargs):
                captured_on_select["fn"] = kwargs["on_select"]
                return mock_picker_instance

            mock_fuzzy_picker.side_effect = capture_init

            def picker_run():
                # simulate picking an item
                captured_on_select["fn"](fake_item)

            mock_picker_instance.run.side_effect = picker_run

            result = gs.run()

        assert result == f"{tmp_path}/main.py:5"
        assert len(selected_results) == 1
        assert "main.py:5" in selected_results[0]

    def test_run_returns_none_on_cancel(self, tmp_path):
        """run() should return None when the picker is cancelled."""
        cancelled: list[bool] = []

        def on_cancel() -> None:
            cancelled.append(True)

        gs = GlobalSearch(
            root_dir=str(tmp_path),
            on_select=lambda r: None,
            on_cancel=on_cancel,
        )

        with patch("iac_code.ui.dialogs.global_search.FuzzyPicker") as mock_fuzzy_picker:
            mock_picker_instance = MagicMock()
            captured_on_cancel = {}

            def capture_init(*args, **kwargs):
                captured_on_cancel["fn"] = kwargs["on_cancel"]
                return mock_picker_instance

            mock_fuzzy_picker.side_effect = capture_init

            def picker_run():
                # simulate cancellation
                captured_on_cancel["fn"]()

            mock_picker_instance.run.side_effect = picker_run

            result = gs.run()

        assert result is None
        assert cancelled == [True]

    def test_run_passes_correct_picker_kwargs(self, tmp_path):
        """run() should create FuzzyPicker with the expected keyword arguments."""
        gs = GlobalSearch(
            root_dir=str(tmp_path),
            on_select=lambda r: None,
            on_cancel=lambda: None,
            keybinding_manager=object(),
        )

        with patch("iac_code.ui.dialogs.global_search.FuzzyPicker") as mock_fuzzy_picker:
            mock_picker_instance = MagicMock()
            mock_fuzzy_picker.return_value = mock_picker_instance
            mock_picker_instance.run.return_value = None

            gs.run()

        _, kwargs = mock_fuzzy_picker.call_args
        assert kwargs["items"].__self__ is gs
        assert kwargs["items"].__func__ is gs._search.__func__
        assert kwargs["render_preview"].__self__ is gs
        assert kwargs["render_preview"].__func__ is gs._render_preview.__func__
        assert kwargs["debounce_ms"] == 300
        assert kwargs["keybinding_manager"] is gs._km

    # ------------------------------------------------------------------
    # _search() exception path — lines 82-83
    # ------------------------------------------------------------------

    def test_search_returns_empty_on_exception(self, tmp_path):
        """_search() should return [] when _run_search raises any exception."""
        gs = GlobalSearch(
            root_dir=str(tmp_path),
            on_select=lambda r: None,
            on_cancel=lambda: None,
        )
        with patch.object(gs, "_run_search", side_effect=RuntimeError("boom")):
            items = gs._search("hello")
        assert items == []

    # ------------------------------------------------------------------
    # _run_search() grep fallback — line 100
    # ------------------------------------------------------------------

    def test_run_search_uses_grep_when_rg_unavailable(self, tmp_path):
        """_run_search should use grep when rg is not on PATH."""
        (tmp_path / "sample.txt").write_text("grep me please\n")

        gs = GlobalSearch(
            root_dir=str(tmp_path),
            on_select=lambda r: None,
            on_cancel=lambda: None,
        )

        # Force shutil.which("rg") to return None so grep branch is taken
        with patch("iac_code.ui.dialogs.global_search.shutil.which", return_value=None):
            output = gs._run_search("grep me")

        assert "grep me" in output

    # ------------------------------------------------------------------
    # _parse_results() edge cases — lines 125, 128, 132
    # ------------------------------------------------------------------

    def test_parse_results_skips_short_lines(self, tmp_path):
        """Lines with fewer than 3 colon-separated parts should be skipped."""
        gs = GlobalSearch(
            root_dir=str(tmp_path),
            on_select=lambda r: None,
            on_cancel=lambda: None,
        )
        # Only two parts — no second colon
        output = "just_a_line_without_enough_colons\n"
        items = gs._parse_results(output)
        assert items == []

    def test_parse_results_skips_non_digit_lineno(self, tmp_path):
        """Lines where the second field is not a digit should be skipped."""
        gs = GlobalSearch(
            root_dir=str(tmp_path),
            on_select=lambda r: None,
            on_cancel=lambda: None,
        )
        main_py = str(tmp_path / "main.py")
        output = f"{main_py}:abc:hello world\n"
        items = gs._parse_results(output)
        assert items == []

    def test_parse_results_skips_duplicate_keys(self, tmp_path):
        """Duplicate file:lineno entries should produce only one PickerItem."""
        gs = GlobalSearch(
            root_dir=str(tmp_path),
            on_select=lambda r: None,
            on_cancel=lambda: None,
        )
        main_py = str(tmp_path / "main.py")
        dup_line = f"{main_py}:3:hello world\n"
        output = dup_line + dup_line  # exact duplicate
        items = gs._parse_results(output)
        assert len(items) == 1

    # ------------------------------------------------------------------
    # _render_preview() — lines 151-178
    # ------------------------------------------------------------------

    def test_render_preview_returns_panel(self, tmp_path):
        """_render_preview should return a Panel containing a Syntax object."""
        py_file = tmp_path / "hello.py"
        py_file.write_text("line1\nline2\nline3\nhello\nline5\nline6\nline7\n")

        gs = GlobalSearch(
            root_dir=str(tmp_path),
            on_select=lambda r: None,
            on_cancel=lambda: None,
        )

        item = PickerItem(
            key=f"{py_file}:4",
            display="hello.py:4  hello",
            metadata={"file_path": str(py_file), "lineno": 4, "text": "hello"},
        )
        panel = gs._render_preview(item)

        assert isinstance(panel, Panel)

    def test_render_preview_panel_title_contains_path_and_lineno(self, tmp_path):
        """Panel title should include relative path and line number."""
        py_file = tmp_path / "src" / "main.py"
        py_file.parent.mkdir()
        py_file.write_text("a\nb\nc\nhello\ne\n")

        gs = GlobalSearch(
            root_dir=str(tmp_path),
            on_select=lambda r: None,
            on_cancel=lambda: None,
        )

        item = PickerItem(
            key=f"{py_file}:4",
            display="src/main.py:4  hello",
            metadata={"file_path": str(py_file), "lineno": 4, "text": "hello"},
        )
        panel = gs._render_preview(item)

        assert isinstance(panel, Panel)
        # The panel title should contain the relative path and line number
        assert "main.py" in str(panel.title)
        assert "4" in str(panel.title)

    def test_render_preview_handles_non_dict_metadata(self, tmp_path):
        """_render_preview should return an empty panel when metadata is not a dict."""
        gs = GlobalSearch(
            root_dir=str(tmp_path),
            on_select=lambda r: None,
            on_cancel=lambda: None,
        )
        item = PickerItem(
            key="some:1",
            display="some:1  text",
            metadata="not a dict",
        )
        panel = gs._render_preview(item)
        assert isinstance(panel, Panel)

    def test_render_preview_handles_missing_file(self, tmp_path):
        """_render_preview should return a panel with empty content for missing files."""
        gs = GlobalSearch(
            root_dir=str(tmp_path),
            on_select=lambda r: None,
            on_cancel=lambda: None,
        )
        item = PickerItem(
            key=f"{tmp_path}/nonexistent.py:1",
            display="nonexistent.py:1  hello",
            metadata={
                "file_path": str(tmp_path / "nonexistent.py"),
                "lineno": 1,
                "text": "hello",
            },
        )
        panel = gs._render_preview(item)
        assert isinstance(panel, Panel)

    def test_render_preview_no_extension(self, tmp_path):
        """_render_preview should handle files without an extension gracefully."""
        no_ext_file = tmp_path / "Makefile"
        no_ext_file.write_text("all:\n\techo done\n")

        gs = GlobalSearch(
            root_dir=str(tmp_path),
            on_select=lambda r: None,
            on_cancel=lambda: None,
        )
        item = PickerItem(
            key=f"{no_ext_file}:1",
            display="Makefile:1  all:",
            metadata={"file_path": str(no_ext_file), "lineno": 1, "text": "all:"},
        )
        panel = gs._render_preview(item)
        assert isinstance(panel, Panel)

    def test_render_preview_lineno_near_start(self, tmp_path):
        """_render_preview should clip start line to 1 when lineno < 6."""
        py_file = tmp_path / "short.py"
        py_file.write_text("a\nb\nhello\n")

        gs = GlobalSearch(
            root_dir=str(tmp_path),
            on_select=lambda r: None,
            on_cancel=lambda: None,
        )
        item = PickerItem(
            key=f"{py_file}:1",
            display="short.py:1  a",
            metadata={"file_path": str(py_file), "lineno": 1, "text": "a"},
        )
        # Should not raise even though lineno-5 would be negative
        panel = gs._render_preview(item)
        assert isinstance(panel, Panel)
