"""Tests for InputHistory."""

import json
import sys

import pytest

from iac_code.ui.core.input_history import InputHistory


class TestInputHistory:
    def test_append_and_search(self, tmp_path):
        history_file = str(tmp_path / "history.txt")
        h = InputHistory(history_file)
        h.append("hello world")
        h.append("foo bar")
        results = h.search("foo")
        assert "foo bar" in results
        results2 = h.search("hello")
        assert "hello world" in results2

    def test_search_most_recent_first(self, tmp_path):
        history_file = str(tmp_path / "history.txt")
        h = InputHistory(history_file)
        h.append("hello old")
        h.append("hello new")
        results = h.search("hello")
        assert results[0] == "hello new"
        assert results[1] == "hello old"

    def test_dedup_consecutive(self, tmp_path):
        history_file = str(tmp_path / "history.txt")
        h = InputHistory(history_file)
        h.append("same command")
        h.append("same command")
        results = h.search("same")
        assert results.count("same command") == 1

    def test_dedup_non_consecutive_keeps_both(self, tmp_path):
        history_file = str(tmp_path / "history.txt")
        h = InputHistory(history_file)
        h.append("cmd a")
        h.append("cmd b")
        h.append("cmd a")
        results = h.search("cmd")
        assert len(results) == 3

    def test_persistence(self, tmp_path):
        history_file = str(tmp_path / "history.txt")
        h1 = InputHistory(history_file)
        h1.append("persistent entry")
        h2 = InputHistory(history_file)
        results = h2.search("persistent")
        assert "persistent entry" in results

    def test_empty_search_returns_all(self, tmp_path):
        history_file = str(tmp_path / "history.txt")
        h = InputHistory(history_file)
        h.append("alpha")
        h.append("beta")
        results = h.search("")
        assert "alpha" in results
        assert "beta" in results

    def test_entries_returns_oldest_first_copy(self, tmp_path):
        history_file = str(tmp_path / "history.txt")
        h = InputHistory(history_file)
        h.append("first")
        h.append("second")

        entries = h.entries()
        entries.append("mutated")

        assert entries == ["first", "second", "mutated"]
        assert h.entries() == ["first", "second"]

    def test_search_no_match(self, tmp_path):
        history_file = str(tmp_path / "history.txt")
        h = InputHistory(history_file)
        h.append("hello")
        results = h.search("xyz")
        assert results == []

    def test_navigate_older(self, tmp_path):
        history_file = str(tmp_path / "history.txt")
        h = InputHistory(history_file)
        h.append("first")
        h.append("second")
        h.append("third")
        # navigate(-1) = older, returns most recent first
        result1 = h.navigate(-1)
        assert result1 == "third"
        result2 = h.navigate(-1)
        assert result2 == "second"
        result3 = h.navigate(-1)
        assert result3 == "first"

    def test_navigate_newer(self, tmp_path):
        history_file = str(tmp_path / "history.txt")
        h = InputHistory(history_file)
        h.append("first")
        h.append("second")
        h.append("third")
        # Go back 3 steps
        h.navigate(-1)
        h.navigate(-1)
        h.navigate(-1)
        # Now go forward (newer)
        result = h.navigate(1)
        assert result == "second"
        result2 = h.navigate(1)
        assert result2 == "third"

    def test_navigate_to_end_restores_input(self, tmp_path):
        history_file = str(tmp_path / "history.txt")
        h = InputHistory(history_file)
        h.append("entry1")
        # Navigate back once (saves current_input), then forward past newest → None
        h.navigate(-1, current_input="my current text")
        result = h.navigate(1)
        assert result is None

    def test_navigate_saves_current_input(self, tmp_path):
        history_file = str(tmp_path / "history.txt")
        h = InputHistory(history_file)
        h.append("entry1")
        # First call saves current_input
        h.navigate(-1, current_input="saved input")
        # Navigate forward past newest → None, meaning restore original input
        result = h.navigate(1)
        assert result is None
        # Saved input is accessible
        assert h._saved_input == "saved input"

    def test_empty_history_navigate(self, tmp_path):
        history_file = str(tmp_path / "history.txt")
        h = InputHistory(history_file)
        result = h.navigate(-1)
        assert result is None

    def test_navigate_older_at_oldest_stays(self, tmp_path):
        history_file = str(tmp_path / "history.txt")
        h = InputHistory(history_file)
        h.append("only entry")
        h.navigate(-1)
        # Try to go even older — stays at oldest
        result = h.navigate(-1)
        assert result == "only entry"

    def test_initial_state_not_navigating(self, tmp_path):
        history_file = str(tmp_path / "history.txt")
        h = InputHistory(history_file)
        assert h._nav_index == -1

    def test_file_created_on_first_append(self, tmp_path):
        history_file = str(tmp_path / "history.txt")
        h = InputHistory(history_file)
        h.append("test")
        assert (tmp_path / "history.txt").exists()

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX modes are not meaningful on Windows")
    def test_history_file_is_owner_only(self, tmp_path):
        history_file = tmp_path / "history.txt"
        h = InputHistory(str(history_file))

        h.append("test")

        assert oct(history_file.stat().st_mode & 0o777) == "0o600"

    def test_empty_entry_not_appended(self, tmp_path):
        history_file = str(tmp_path / "history.txt")
        h = InputHistory(history_file)
        h.append("")
        results = h.search("")
        assert results == []

    def test_append_resets_nav_index_on_duplicate(self, tmp_path):
        """Regression: appending a duplicate must reset navigation state."""
        history_file = str(tmp_path / "history.txt")
        h = InputHistory(history_file)
        h.append("first")
        h.append("second")
        # Navigate back
        h.navigate(-1)  # nav_index = 1 (points to "second")
        assert h._nav_index == 1
        # Submit "second" again (duplicate of last entry)
        h.append("second")
        # nav_index must be reset even though entry was a duplicate
        assert h._nav_index == -1

    def test_append_persist_false_not_saved_to_disk(self, tmp_path):
        """Session-only entries are in memory but not on disk."""
        history_file = str(tmp_path / "history.txt")
        h = InputHistory(history_file)
        h.append("persisted")
        h.append("/auth", persist=False)
        # In memory: both visible
        results = h.search("/auth")
        assert "/auth" in results
        # On disk: only "persisted" survives reload
        h2 = InputHistory(history_file)
        results2 = h2.search("/auth")
        assert "/auth" not in results2
        results3 = h2.search("persisted")
        assert "persisted" in results3

    def test_reset_navigation(self, tmp_path):
        history_file = str(tmp_path / "history.txt")
        h = InputHistory(history_file)
        h.append("entry")
        h.navigate(-1)
        assert h._nav_index != -1
        h.reset_navigation()
        assert h._nav_index == -1
        assert h._saved_input == ""

    def test_navigate_after_session_only_append(self, tmp_path):
        """Session-only entries are navigable in the current session."""
        history_file = str(tmp_path / "history.txt")
        h = InputHistory(history_file)
        h.append("first")
        h.append("/auth login", persist=False)
        # Navigate back should show session-only entry first
        result = h.navigate(-1)
        assert result == "/auth login"
        result2 = h.navigate(-1)
        assert result2 == "first"

    def test_multiline_entry_persists_as_single_jsonl_entry(self, tmp_path):
        history_file = tmp_path / "history.txt"
        entry = "line1\nline2"

        h1 = InputHistory(str(history_file))
        h1.append(entry)

        raw_lines = history_file.read_text(encoding="utf-8").splitlines()
        assert len(raw_lines) == 1
        assert json.loads(raw_lines[0]) == {
            "format": "iac-code-input-history-v1",
            "text": entry,
        }

        h2 = InputHistory(str(history_file))
        assert h2.search("line1") == [entry]
        assert h2.navigate(-1) == entry

    def test_multiline_entry_dedupes_consecutive_duplicates(self, tmp_path):
        history_file = str(tmp_path / "history.txt")
        entry = "line1\nline2"
        h = InputHistory(history_file)

        h.append(entry)
        h.append(entry)

        assert h.search("line1") == [entry]

    def test_multiline_entry_keeps_non_consecutive_duplicates(self, tmp_path):
        history_file = str(tmp_path / "history.txt")
        entry = "line1\nline2"
        h = InputHistory(history_file)

        h.append(entry)
        h.append("other")
        h.append(entry)

        assert h.search("line1") == [entry, entry]

    def test_multiline_session_only_entry_not_saved_to_disk(self, tmp_path):
        history_file = str(tmp_path / "history.txt")
        entry = "line1\nline2"
        h = InputHistory(history_file)

        h.append("persisted")
        h.append(entry, persist=False)

        assert h.navigate(-1) == entry
        h2 = InputHistory(history_file)
        assert h2.search("line1") == []
        assert h2.search("persisted") == ["persisted"]

    def test_legacy_plain_line_history_still_loads(self, tmp_path):
        history_file = tmp_path / "history.txt"
        history_file.write_text("old one\nold two\n", encoding="utf-8")

        h = InputHistory(str(history_file))

        assert h.search("old") == ["old two", "old one"]

    def test_malformed_jsonl_line_loads_as_legacy_entry(self, tmp_path):
        history_file = tmp_path / "history.txt"
        history_file.write_text(
            '{"format": "iac-code-input-history-v1", "text": "ok"}\n{"text": 123}\n[1, 2]\n{broken\n',
            encoding="utf-8",
        )

        h = InputHistory(str(history_file))

        assert h.search("") == ["{broken", "[1, 2]", '{"text": 123}', "ok"]

    def test_legacy_json_text_line_stays_literal(self, tmp_path):
        history_file = tmp_path / "history.txt"
        legacy_entry = '{"text": "keep literal"}'
        history_file.write_text(legacy_entry + "\n", encoding="utf-8")

        h = InputHistory(str(history_file))

        assert h.search("") == [legacy_entry]
