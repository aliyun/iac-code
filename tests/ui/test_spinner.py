"""Tests for ShimmerSpinner and helper functions in spinner.py."""

from __future__ import annotations

import time

from rich.text import Text

from iac_code.ui.spinner import (
    COMPLETION_VERBS,
    SPINNER_VERBS,
    ShimmerSpinner,
    _format_elapsed,
    random_completion_verb,
    random_spinner_verb,
)

# ---------------------------------------------------------------------------
# _format_elapsed
# ---------------------------------------------------------------------------


class TestFormatElapsed:
    def test_seconds_less_than_60(self):
        assert _format_elapsed(0) == "0s"
        assert _format_elapsed(1) == "1s"
        assert _format_elapsed(59) == "59s"
        assert _format_elapsed(59.9) == "60s"  # rounds to 60s

    def test_exactly_60_seconds(self):
        result = _format_elapsed(60)
        assert result == "1m 0s"

    def test_minutes_and_seconds(self):
        assert _format_elapsed(65) == "1m 5s"
        assert _format_elapsed(120) == "2m 0s"
        assert _format_elapsed(125) == "2m 5s"
        assert _format_elapsed(3661) == "61m 1s"

    def test_format_contains_m_and_s_for_minutes(self):
        result = _format_elapsed(90)
        assert "m" in result
        assert "s" in result

    def test_format_contains_only_s_for_short_duration(self):
        result = _format_elapsed(30)
        assert "m" not in result
        assert result.endswith("s")


# ---------------------------------------------------------------------------
# random_spinner_verb
# ---------------------------------------------------------------------------


class TestRandomSpinnerVerb:
    def test_returns_string(self):
        result = random_spinner_verb()
        assert isinstance(result, str)

    def test_returns_non_empty(self):
        result = random_spinner_verb()
        assert len(result) > 0

    def test_returns_one_of_spinner_verbs(self, monkeypatch):
        monkeypatch.setattr("random.choice", lambda seq: seq[0])
        result = random_spinner_verb()
        # With identity translation, the result should equal the chosen verb
        assert result == SPINNER_VERBS[0]

    def test_monkeypatched_choice_is_deterministic(self, monkeypatch):
        monkeypatch.setattr("random.choice", lambda seq: seq[-1])
        result = random_spinner_verb()
        assert result == SPINNER_VERBS[-1]


# ---------------------------------------------------------------------------
# random_completion_verb
# ---------------------------------------------------------------------------


class TestRandomCompletionVerb:
    def test_returns_string(self):
        result = random_completion_verb()
        assert isinstance(result, str)

    def test_returns_non_empty(self):
        result = random_completion_verb()
        assert len(result) > 0

    def test_returns_one_of_completion_verbs(self, monkeypatch):
        monkeypatch.setattr("random.choice", lambda seq: seq[0])
        result = random_completion_verb()
        assert result == COMPLETION_VERBS[0]

    def test_monkeypatched_choice_is_deterministic(self, monkeypatch):
        monkeypatch.setattr("random.choice", lambda seq: seq[-1])
        result = random_completion_verb()
        assert result == COMPLETION_VERBS[-1]


# ---------------------------------------------------------------------------
# ShimmerSpinner.__init__
# ---------------------------------------------------------------------------


class TestShimmerSpinnerInit:
    def test_default_status_is_non_empty(self):
        spinner = ShimmerSpinner()
        assert isinstance(spinner._status, str)
        assert len(spinner._status) > 0

    def test_custom_status_is_stored(self):
        spinner = ShimmerSpinner(status="Deploying...")
        assert spinner._status == "Deploying..."

    def test_start_time_is_set(self):
        before = time.monotonic()
        spinner = ShimmerSpinner()
        after = time.monotonic()
        assert before <= spinner._start_time <= after

    def test_none_status_uses_random_verb(self, monkeypatch):
        monkeypatch.setattr("random.choice", lambda seq: seq[0])
        spinner = ShimmerSpinner(status=None)
        # status should start with a verb and end with "..."
        assert spinner._status.endswith("...")


# ---------------------------------------------------------------------------
# ShimmerSpinner.elapsed
# ---------------------------------------------------------------------------


class TestShimmerSpinnerElapsed:
    def test_elapsed_is_non_negative(self):
        spinner = ShimmerSpinner()
        assert spinner.elapsed >= 0.0

    def test_elapsed_increases_monotonically(self):
        spinner = ShimmerSpinner()
        first = spinner.elapsed
        # Brief busy-wait to allow clock to advance
        deadline = time.monotonic() + 0.05
        while time.monotonic() < deadline:
            pass
        second = spinner.elapsed
        assert second >= first

    def test_elapsed_returns_float(self):
        spinner = ShimmerSpinner()
        assert isinstance(spinner.elapsed, float)


# ---------------------------------------------------------------------------
# ShimmerSpinner.render
# ---------------------------------------------------------------------------


class TestShimmerSpinnerRender:
    def test_render_returns_text(self):
        spinner = ShimmerSpinner(status="Testing")
        result = spinner.render()
        assert isinstance(result, Text)

    def test_render_contains_status(self):
        spinner = ShimmerSpinner(status="Testing")
        result = spinner.render()
        assert "Testing" in result.plain

    def test_render_contains_elapsed_in_parens(self):
        spinner = ShimmerSpinner(status="Testing")
        result = spinner.render()
        plain = result.plain
        # Elapsed time is wrapped in parentheses: "(Xs)" or "(Xm Ys)"
        assert "(" in plain
        assert ")" in plain
        assert "s" in plain

    def test_render_contains_spinner_char(self):
        from iac_code.ui.spinner import SPINNER_DOTS

        spinner = ShimmerSpinner(status="Testing")
        result = spinner.render()
        plain = result.plain
        assert any(dot in plain for dot in SPINNER_DOTS)

    def test_render_updates_with_elapsed(self):
        spinner = ShimmerSpinner(status="Testing")
        result = spinner.render()
        assert isinstance(result, Text)
        plain = result.plain
        # Should contain a valid elapsed string like "(0s)" or "(1s)"
        assert "s)" in plain


# ---------------------------------------------------------------------------
# ShimmerSpinner.update_status
# ---------------------------------------------------------------------------


class TestShimmerSpinnerUpdateStatus:
    def test_update_status_changes_internal_status(self):
        spinner = ShimmerSpinner(status="Old")
        spinner.update_status("New")
        assert spinner._status == "New"

    def test_render_reflects_updated_status(self):
        spinner = ShimmerSpinner(status="Old")
        spinner.update_status("New Status")
        result = spinner.render()
        assert "New Status" in result.plain
        assert "Old" not in result.plain

    def test_update_status_does_not_reset_start_time(self):
        spinner = ShimmerSpinner(status="Old")
        start_time = spinner._start_time
        spinner.update_status("New")
        assert spinner._start_time == start_time
