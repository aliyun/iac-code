"""Tests for ShellHistoryProvider."""

from __future__ import annotations

import pytest

from iac_code.ui.suggestions import shell_history_provider as shell_history_module
from iac_code.ui.suggestions.shell_history_provider import (
    ShellHistoryProvider,
    _detect_history_path,
    _read_history,
)
from iac_code.ui.suggestions.types import CompletionToken


def make_token(text: str) -> CompletionToken:
    return CompletionToken(text=text, start=0, end=len(text), trigger="!")


@pytest.fixture
def provider_with_history(tmp_path) -> ShellHistoryProvider:
    """Create a ShellHistoryProvider with a known history file."""
    history_file = tmp_path / ".bash_history"
    history_file.write_text(
        "git status\n"
        "git commit -m 'first'\n"
        "ls -la\n"
        "git push origin main\n"
        "python -m pytest\n"
        "git status\n"  # duplicate — should be deduped, most recent first
        "docker build .\n",
        encoding="utf-8",
    )
    provider = ShellHistoryProvider()
    provider._history_path = str(history_file)
    return provider


class TestShellHistoryProvider:
    def test_trigger(self):
        provider = ShellHistoryProvider()
        assert provider.trigger == "!"

    def test_match(self, provider_with_history):
        """!git matches all git commands."""
        token = make_token("!git")
        items = provider_with_history.provide(token)
        assert len(items) > 0
        for item in items:
            assert "git" in item.display_text.lower()

    def test_dedup(self, provider_with_history):
        """Duplicate entries are removed."""
        token = make_token("!git status")
        items = provider_with_history.provide(token)
        displays = [item.display_text for item in items]
        assert len(displays) == len(set(displays))

    def test_most_recent_first(self, provider_with_history):
        """Most recent matching entry appears first."""
        token = make_token("!git")
        items = provider_with_history.provide(token)
        assert len(items) > 0
        # The history file has "git status" at line 1 and again at line 6 (most recent)
        # After dedup (most recent first), "git status" should appear before the original
        assert items[0].display_text == "git status"

    def test_no_match(self, provider_with_history):
        """Query matching nothing → empty results."""
        token = make_token("!xyzxyz_nonexistent")
        items = provider_with_history.provide(token)
        assert items == []

    def test_source_and_icon(self, provider_with_history):
        """All items have source='shell' and icon='↑'."""
        token = make_token("!")
        items = provider_with_history.provide(token)
        assert len(items) > 0
        for item in items:
            assert item.source == "shell"
            assert item.icon == "↑"

    def test_empty_query_returns_all(self, provider_with_history):
        """!-only token returns all history entries (deduped)."""
        token = make_token("!")
        items = provider_with_history.provide(token)
        # 7 lines, 1 duplicate → 6 unique entries
        assert len(items) == 6

    def test_no_history_path(self):
        """Provider with no history file returns empty list."""
        provider = ShellHistoryProvider()
        provider._history_path = None
        token = make_token("!git")
        items = provider.provide(token)
        assert items == []

    def test_completion_prefix(self, provider_with_history):
        """Completion text starts with '!'."""
        token = make_token("!git")
        items = provider_with_history.provide(token)
        assert len(items) > 0
        for item in items:
            assert item.completion.startswith("!")

    def test_zsh_extended_format(self, tmp_path):
        """Handles zsh extended_history format."""
        history_file = tmp_path / ".zsh_history"
        history_file.write_text(
            ": 1700000000:0;git status\n: 1700000001:0;ls -la\n: 1700000002:0;git diff\n",
            encoding="utf-8",
        )
        provider = ShellHistoryProvider()
        provider._history_path = str(history_file)
        token = make_token("!git")
        items = provider.provide(token)
        assert len(items) > 0
        displays = {item.display_text for item in items}
        assert "git status" in displays
        assert "git diff" in displays

    def test_reuses_cached_history_when_file_unchanged(self, tmp_path, monkeypatch):
        history_file = tmp_path / ".bash_history"
        history_file.write_text("git status\ngit diff\n", encoding="utf-8")
        calls: list[str] = []

        def fake_read_history(path: str) -> list[str]:
            calls.append(path)
            return ["git status", "git diff"]

        monkeypatch.setattr(shell_history_module, "_read_history", fake_read_history)
        provider = ShellHistoryProvider()
        provider._history_path = str(history_file)

        first = provider.provide(make_token("!git"))
        second = provider.provide(make_token("!git s"))

        assert [item.display_text for item in first] == ["git diff", "git status"]
        assert [item.display_text for item in second] == ["git status"]
        assert calls == [str(history_file)]

    def test_refreshes_cached_history_when_file_changes(self, tmp_path, monkeypatch):
        history_file = tmp_path / ".bash_history"
        history_file.write_text("git status\n", encoding="utf-8")
        reads = [["git status"], ["git status", "git push"]]

        def fake_read_history(path: str) -> list[str]:
            return reads.pop(0)

        monkeypatch.setattr(shell_history_module, "_read_history", fake_read_history)
        provider = ShellHistoryProvider()
        provider._history_path = str(history_file)

        assert [item.display_text for item in provider.provide(make_token("!git"))] == ["git status"]
        history_file.write_text("git status\ngit push\n", encoding="utf-8")

        assert [item.display_text for item in provider.provide(make_token("!git"))] == ["git push", "git status"]

    def test_limits_returned_history_suggestions(self, tmp_path, monkeypatch):
        history_file = tmp_path / ".bash_history"
        history_file.write_text("\n".join(f"git command {i}" for i in range(10)), encoding="utf-8")
        monkeypatch.setattr(
            shell_history_module,
            "_read_history",
            lambda path: [f"git command {i}" for i in range(10)],
        )
        provider = ShellHistoryProvider(max_suggestions=3)
        provider._history_path = str(history_file)

        items = provider.provide(make_token("!git"))

        assert [item.display_text for item in items] == ["git command 9", "git command 8", "git command 7"]


class TestShellHistoryHelpers:
    def test_detect_history_path_prefers_zsh_from_shell_env(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SHELL", "/bin/zsh")
        monkeypatch.setattr("os.path.expanduser", lambda path: str(tmp_path))
        history = tmp_path / ".zsh_history"
        history.write_text("echo hi\n", encoding="utf-8")

        assert _detect_history_path() == str(history)

    def test_detect_history_path_fallbacks_when_shell_unknown(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SHELL", "/bin/fish")
        monkeypatch.setattr("os.path.expanduser", lambda path: str(tmp_path))
        bash_history = tmp_path / ".bash_history"
        bash_history.write_text("echo hi\n", encoding="utf-8")

        assert _detect_history_path() == str(bash_history)

    def test_read_history_returns_empty_on_os_error(self, tmp_path):
        assert _read_history(str(tmp_path / "missing_history")) == []

    def test_read_history_skips_blank_and_malformed_zsh_lines(self, tmp_path):
        history_file = tmp_path / ".zsh_history"
        history_file.write_bytes(b": 1700000000:0;\nplain\n\n: 1700000001:0;git status\n")

        assert _read_history(str(history_file)) == ["plain", "git status"]

    def test_provide_accepts_token_without_bang_prefix(self, provider_with_history):
        token = CompletionToken(text="git", start=0, end=3, trigger="!")
        items = provider_with_history.provide(token)

        assert items
        assert all("git" in item.display_text.lower() for item in items)
