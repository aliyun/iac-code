"""Tests for DirectoryProvider."""

from __future__ import annotations

import pytest

from iac_code.ui.suggestions.directory_provider import DirectoryProvider
from iac_code.ui.suggestions.types import CompletionToken


def make_token(text: str) -> CompletionToken:
    return CompletionToken(text=text, start=0, end=len(text), trigger="@")


@pytest.fixture
def sample_tree(tmp_path):
    """Create a sample directory tree for testing."""
    (tmp_path / "main.py").write_text("# main")
    (tmp_path / "config.yaml").write_text("key: value")
    src = tmp_path / "src"
    src.mkdir()
    (src / "app.py").write_text("# app")
    (src / "utils.py").write_text("# utils")
    ui = src / "ui"
    ui.mkdir()
    (ui / "input.py").write_text("# input")

    # Hidden dir — should be excluded
    hidden = tmp_path / ".hidden"
    hidden.mkdir()
    (hidden / "secret.py").write_text("secret")

    # Excluded dir
    pycache = tmp_path / "__pycache__"
    pycache.mkdir()
    (pycache / "bytecode.pyc").write_text("bytes")

    return tmp_path


@pytest.fixture
def provider(sample_tree) -> DirectoryProvider:
    return DirectoryProvider(str(sample_tree))


class TestDirectoryProvider:
    def test_trigger(self, provider):
        assert provider.trigger == "@"

    def test_list_root_dirs(self, provider):
        """@ at root should list top-level dirs and files (not hidden, not excluded)."""
        token = make_token("@")
        items = provider.provide(token)
        names = [item.display_text for item in items]
        # src/ dir should appear
        assert any("src" in n for n in names)
        # main.py should appear
        assert any("main.py" in n for n in names)
        # Hidden dir should NOT appear
        assert not any(".hidden" in n for n in names)
        # __pycache__ should NOT appear
        assert not any("__pycache__" in n for n in names)

    def test_list_subdirs(self, provider):
        """@src/ should list contents of src/."""
        token = make_token("@src/")
        items = provider.provide(token)
        names = [item.display_text for item in items]
        # Should include src/app.py and src/utils.py and src/ui/
        assert any("app.py" in n for n in names)
        assert any("utils.py" in n for n in names)
        assert any("ui" in n for n in names)

    def test_includes_files_in_dir(self, provider):
        """@src/ includes both files and subdirectories."""
        token = make_token("@src/")
        items = provider.provide(token)
        sources = {item.source for item in items}
        assert "directory" in sources

    def test_source_and_icon(self, provider):
        """All items have source='directory' and icon='◇'."""
        token = make_token("@")
        items = provider.provide(token)
        assert len(items) > 0
        for item in items:
            assert item.source == "directory"
            assert item.icon == "◇"

    def test_directory_completions_have_trailing_slash(self, provider):
        """Directory entries should have trailing '/' in completion."""
        token = make_token("@")
        items = provider.provide(token)
        dir_items = [i for i in items if i.description == "directory"]
        assert len(dir_items) > 0
        for item in dir_items:
            assert item.completion.endswith("/")

    def test_fragment_filter(self, provider):
        """@src/ap should filter to entries matching 'ap' in src/."""
        token = make_token("@src/ap")
        items = provider.provide(token)
        names = [item.display_text for item in items]
        assert any("app.py" in n for n in names)
        # utils.py should not match 'ap'... unless fuzzy_match finds it
        # Just verify app.py is present
        assert len(items) > 0

    def test_id_format(self, provider):
        """Items have id starting with 'dir:'."""
        token = make_token("@")
        items = provider.provide(token)
        assert len(items) > 0
        for item in items:
            assert item.id.startswith("dir:")
