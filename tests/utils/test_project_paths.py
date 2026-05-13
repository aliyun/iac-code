"""Tests for the project-path sanitizer and helpers."""

from __future__ import annotations

from iac_code.utils.project_paths import (
    MAX_SANITIZED_LENGTH,
    sanitize_path,
)


class TestSanitizePath:
    def test_replaces_non_alnum_with_dash(self):
        assert sanitize_path("/Users/x/proj") == "-Users-x-proj"
        assert sanitize_path("/tmp/my proj.git") == "-tmp-my-proj-git"

    def test_preserves_alphanumerics(self):
        assert sanitize_path("abc123") == "abc123"

    def test_long_path_gets_hash_suffix(self):
        original = "x" * (MAX_SANITIZED_LENGTH + 50)
        result = sanitize_path(original)
        # Length should be MAX_SANITIZED_LENGTH + dash + hash
        assert len(result) > MAX_SANITIZED_LENGTH
        assert result.startswith("x" * MAX_SANITIZED_LENGTH)
        assert "-" in result[MAX_SANITIZED_LENGTH:]

    def test_long_paths_are_unique_per_input(self):
        a = "a" * (MAX_SANITIZED_LENGTH + 1)
        b = "b" * (MAX_SANITIZED_LENGTH + 1)
        assert sanitize_path(a) != sanitize_path(b)

    def test_unicode_replaced(self):
        # Chinese characters are non-ASCII non-alnum → replaced with dashes
        assert sanitize_path("/projects/中文/repo") == "-projects----repo"

    def test_empty_string(self):
        assert sanitize_path("") == ""
