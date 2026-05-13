"""Tests for InputHistory."""

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

    def test_empty_entry_not_appended(self, tmp_path):
        history_file = str(tmp_path / "history.txt")
        h = InputHistory(history_file)
        h.append("")
        results = h.search("")
        assert results == []
