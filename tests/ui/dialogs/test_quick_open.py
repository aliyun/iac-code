"""Tests for QuickOpen dialog."""

from __future__ import annotations

import os

from iac_code.ui.dialogs.quick_open import QuickOpen


class TestQuickOpen:
    def test_create(self, tmp_path):
        """Constructor should work without error."""
        qo = QuickOpen(
            root_dir=str(tmp_path),
            on_select=lambda path: None,
            on_cancel=lambda: None,
        )
        assert qo is not None

    def test_builds_items(self, tmp_path):
        """Sample tree should produce items containing expected files."""
        # Create a small file tree
        (tmp_path / "main.py").write_text("print('hello')", encoding="utf-8")
        (tmp_path / "utils.py").write_text("def helper(): pass", encoding="utf-8")
        sub = tmp_path / "subdir"
        sub.mkdir()
        (sub / "module.py").write_text("x = 1", encoding="utf-8")

        qo = QuickOpen(
            root_dir=str(tmp_path),
            on_select=lambda path: None,
            on_cancel=lambda: None,
        )
        items = qo._build_items()
        rel_paths = {item.display for item in items}

        assert "main.py" in rel_paths
        assert "utils.py" in rel_paths
        assert os.path.join("subdir", "module.py") in rel_paths

    def test_excludes_hidden_dirs(self, tmp_path):
        """Excluded dirs like .git and __pycache__ should not appear."""
        (tmp_path / "main.py").write_text("x", encoding="utf-8")
        git_dir = tmp_path / ".git"
        git_dir.mkdir()
        (git_dir / "config").write_text("bare = false", encoding="utf-8")
        cache_dir = tmp_path / "__pycache__"
        cache_dir.mkdir()
        (cache_dir / "main.cpython-312.pyc").write_bytes(b"")

        qo = QuickOpen(
            root_dir=str(tmp_path),
            on_select=lambda path: None,
            on_cancel=lambda: None,
        )
        items = qo._build_items()
        rel_paths = {item.display for item in items}

        assert "main.py" in rel_paths
        assert not any(".git" in p for p in rel_paths)
        assert not any("__pycache__" in p for p in rel_paths)

    def test_metadata_is_absolute_path(self, tmp_path):
        """Item metadata should be the absolute path."""
        (tmp_path / "hello.txt").write_text("hi", encoding="utf-8")
        qo = QuickOpen(
            root_dir=str(tmp_path),
            on_select=lambda path: None,
            on_cancel=lambda: None,
        )
        items = qo._build_items()
        assert len(items) == 1
        assert os.path.isabs(items[0].metadata)
        assert items[0].metadata == str(tmp_path / "hello.txt")

    # ------------------------------------------------------------------
    # run() tests — mock FuzzyPicker to exercise select / cancel paths
    # ------------------------------------------------------------------

    def test_run_returns_path_on_select(self, tmp_path, monkeypatch):
        """run() should return the absolute path when a file is selected."""
        (tmp_path / "foo.py").write_text("x = 1", encoding="utf-8")

        selected_refs: list[str] = []

        def fake_on_select(ref: str) -> None:
            selected_refs.append(ref)

        qo = QuickOpen(
            root_dir=str(tmp_path),
            on_select=fake_on_select,
            on_cancel=lambda: None,
        )

        # Capture the _on_select callback passed to FuzzyPicker and call it
        # immediately so run() returns without needing a real terminal.
        import iac_code.ui.dialogs.quick_open as mod

        class FakePicker:
            def __init__(self, items, on_select, on_cancel, **kwargs):
                self._items = items
                self._on_select = on_select
                self._on_cancel = on_cancel

            def run(self):
                # Simulate selecting the first item
                item = self._items[0]
                self._on_select(item)

        monkeypatch.setattr(mod, "FuzzyPicker", FakePicker)

        result = qo.run()

        abs_path = str(tmp_path / "foo.py")
        assert result == abs_path
        # on_select should have been called with "@<rel_path>"
        assert selected_refs == ["@foo.py"]

    def test_run_returns_none_on_cancel(self, tmp_path, monkeypatch):
        """run() should return None and invoke on_cancel when cancelled."""
        (tmp_path / "bar.py").write_text("y = 2", encoding="utf-8")

        cancel_called: list[bool] = []

        def fake_on_cancel() -> None:
            cancel_called.append(True)

        qo = QuickOpen(
            root_dir=str(tmp_path),
            on_select=lambda ref: None,
            on_cancel=fake_on_cancel,
        )

        import iac_code.ui.dialogs.quick_open as mod

        class FakePicker:
            def __init__(self, items, on_select, on_cancel, **kwargs):
                self._on_cancel = on_cancel

            def run(self):
                # Simulate cancellation
                self._on_cancel()

        monkeypatch.setattr(mod, "FuzzyPicker", FakePicker)

        result = qo.run()

        assert result is None
        assert cancel_called == [True]

    def test_run_empty_dir_returns_none_on_cancel(self, tmp_path, monkeypatch):
        """run() with no files should return None when cancelled."""
        qo = QuickOpen(
            root_dir=str(tmp_path),
            on_select=lambda ref: None,
            on_cancel=lambda: None,
        )

        import iac_code.ui.dialogs.quick_open as mod

        class FakePicker:
            def __init__(self, items, on_select, on_cancel, **kwargs):
                self._on_cancel = on_cancel

            def run(self):
                self._on_cancel()

        monkeypatch.setattr(mod, "FuzzyPicker", FakePicker)

        result = qo.run()
        assert result is None

    # ------------------------------------------------------------------
    # _render_preview() tests
    # ------------------------------------------------------------------

    def test_render_preview_reads_file(self, tmp_path):
        """_render_preview should return a Panel with file content."""
        from rich.panel import Panel

        from iac_code.ui.components.fuzzy_picker import PickerItem

        content = "line1\nline2\nline3\n"
        f = tmp_path / "sample.py"
        f.write_text(content, encoding="utf-8")

        qo = QuickOpen(
            root_dir=str(tmp_path),
            on_select=lambda ref: None,
            on_cancel=lambda: None,
        )

        item = PickerItem(key="file:sample.py", display="sample.py", metadata=str(f))
        panel = qo._render_preview(item)

        assert isinstance(panel, Panel)

    def test_render_preview_truncates_at_20_lines(self, tmp_path):
        """_render_preview should only read up to the first 20 lines."""
        from rich.panel import Panel

        from iac_code.ui.components.fuzzy_picker import PickerItem

        lines = [f"line{i}\n" for i in range(30)]
        f = tmp_path / "big.txt"
        f.write_text("".join(lines), encoding="utf-8")

        qo = QuickOpen(
            root_dir=str(tmp_path),
            on_select=lambda ref: None,
            on_cancel=lambda: None,
        )

        item = PickerItem(key="file:big.txt", display="big.txt", metadata=str(f))
        panel = qo._render_preview(item)

        assert isinstance(panel, Panel)
        # The panel title should be the display name
        assert panel.title == "big.txt"

    def test_render_preview_missing_file(self, tmp_path):
        """_render_preview should return a Panel even if the file cannot be read."""
        from rich.panel import Panel

        from iac_code.ui.components.fuzzy_picker import PickerItem

        missing = str(tmp_path / "nonexistent.py")

        qo = QuickOpen(
            root_dir=str(tmp_path),
            on_select=lambda ref: None,
            on_cancel=lambda: None,
        )

        item = PickerItem(key="file:nonexistent.py", display="nonexistent.py", metadata=missing)
        panel = qo._render_preview(item)

        assert isinstance(panel, Panel)

    def test_render_preview_no_extension(self, tmp_path):
        """_render_preview should handle files without an extension (uses 'text')."""
        from rich.panel import Panel

        from iac_code.ui.components.fuzzy_picker import PickerItem

        f = tmp_path / "Makefile"
        f.write_text("all:\n\techo done\n", encoding="utf-8")

        qo = QuickOpen(
            root_dir=str(tmp_path),
            on_select=lambda ref: None,
            on_cancel=lambda: None,
        )

        item = PickerItem(key="file:Makefile", display="Makefile", metadata=str(f))
        panel = qo._render_preview(item)

        assert isinstance(panel, Panel)
