"""Tests for DynamicSkillTracker path matching with backslashes."""

from __future__ import annotations

from iac_code.skills.discovery import DynamicSkillTracker


def test_matches_forward_slash_pattern():
    """Standard forward-slash paths match forward-slash patterns."""
    assert DynamicSkillTracker._matches_any_pattern("src/iac_code/tools/bash_tool.py", ["src/iac_code/tools/*.py"])


def test_matches_backslash_path_against_forward_slash_pattern():
    """Windows backslash paths should match forward-slash patterns."""
    assert DynamicSkillTracker._matches_any_pattern("src\\iac_code\\tools\\bash_tool.py", ["src/iac_code/tools/*.py"])


def test_matches_backslash_pattern_against_forward_slash_path():
    """Backslash patterns should match forward-slash paths."""
    assert DynamicSkillTracker._matches_any_pattern("src/iac_code/tools/bash_tool.py", ["src\\iac_code\\tools\\*.py"])


def test_no_match_returns_false():
    """Non-matching paths return False."""
    assert not DynamicSkillTracker._matches_any_pattern("src\\other\\file.py", ["src/iac_code/tools/*.py"])
