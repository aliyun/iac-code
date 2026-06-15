"""Tests for FileProvider."""

from __future__ import annotations

import pytest

from iac_code.ui.suggestions.file_provider import FileProvider
from iac_code.ui.suggestions.types import CompletionToken


def make_token(text: str) -> CompletionToken:
    return CompletionToken(text=text, start=0, end=len(text), trigger="@")


@pytest.fixture
def sample_tree(tmp_path):
    """Create a sample directory tree for testing."""
    # Normal files
    (tmp_path / "main.py").write_text("# main", encoding="utf-8")
    (tmp_path / "config.yaml").write_text("key: value", encoding="utf-8")
    src = tmp_path / "src"
    src.mkdir()
    (src / "app.py").write_text("# app", encoding="utf-8")
    (src / "utils.py").write_text("# utils", encoding="utf-8")
    ui = src / "ui"
    ui.mkdir()
    (ui / "input.py").write_text("# input", encoding="utf-8")

    # Excluded dirs
    git = tmp_path / ".git"
    git.mkdir()
    (git / "config").write_text("git config", encoding="utf-8")

    pycache = tmp_path / "__pycache__"
    pycache.mkdir()
    (pycache / "main.cpython-312.pyc").write_text("bytecode", encoding="utf-8")

    # egg-info excluded dir
    egg = tmp_path / "mypackage.egg-info"
    egg.mkdir()
    (egg / "PKG-INFO").write_text("pkg info", encoding="utf-8")

    return tmp_path


@pytest.fixture
def provider(sample_tree) -> FileProvider:
    return FileProvider(str(sample_tree))


class TestFileProvider:
    def test_trigger(self, provider):
        assert provider.trigger == "@"

    def test_basic_match(self, provider):
        """@app matches src/app.py."""
        token = make_token("@app")
        items = provider.provide(token)
        assert len(items) > 0
        paths = [item.display_text for item in items]
        assert any("app.py" in p for p in paths)

    def test_excludes_git(self, provider):
        """Files under .git should not appear."""
        token = make_token("@")
        items = provider.provide(token)
        paths = [item.display_text for item in items]
        assert not any(".git" in p for p in paths)

    def test_excludes_pycache(self, provider):
        """Files under __pycache__ should not appear."""
        token = make_token("@")
        items = provider.provide(token)
        paths = [item.display_text for item in items]
        assert not any("__pycache__" in p for p in paths)

    def test_excludes_egg_info(self, provider):
        """Files under *.egg-info should not appear."""
        token = make_token("@")
        items = provider.provide(token)
        paths = [item.display_text for item in items]
        assert not any("egg-info" in p for p in paths)

    def test_source_and_icon(self, provider):
        """All items have source='file' and icon='+'."""
        token = make_token("@")
        items = provider.provide(token)
        assert len(items) > 0
        for item in items:
            assert item.source == "file"
            assert item.icon == "+"

    def test_no_match(self, provider):
        """Query matching nothing → empty results."""
        token = make_token("@xyzxyzxyz123")
        items = provider.provide(token)
        assert items == []

    def test_id_format(self, provider):
        """Items have id starting with 'file:'."""
        token = make_token("@main")
        items = provider.provide(token)
        assert len(items) > 0
        for item in items:
            assert item.id.startswith("file:")

    def test_completion_prefix(self, provider):
        """Completion text starts with '@'."""
        token = make_token("@main")
        items = provider.provide(token)
        assert len(items) > 0
        for item in items:
            assert item.completion.startswith("@")

    def test_empty_query_returns_all(self, provider):
        """@-only query returns all indexed files."""
        token = make_token("@")
        items = provider.provide(token)
        # Should return at least the 5 normal files (main.py, config.yaml, src/app.py, src/utils.py, src/ui/input.py)
        assert len(items) >= 5
