"""Tests for permission cache helpers in state.app_state."""

from __future__ import annotations

from collections import OrderedDict

from iac_code.state.app_state import (
    _PERMISSION_CACHE_MAX_SIZE,
    lookup_permission,
    record_permission,
)


class TestRecordAndLookup:
    def test_record_and_lookup_roundtrip(self):
        cache: OrderedDict = OrderedDict()
        record_permission(cache, "bash", "always_allow")
        assert lookup_permission(cache, "bash") == "always_allow"

    def test_lookup_missing_returns_none(self):
        cache: OrderedDict = OrderedDict()
        assert lookup_permission(cache, "bash") is None

    def test_lookup_moves_entry_to_end(self):
        cache: OrderedDict = OrderedDict()
        record_permission(cache, "a", "always_allow")
        record_permission(cache, "b", "always_allow")
        # Access "a" — it should become most-recent.
        lookup_permission(cache, "a")
        assert list(cache.keys()) == ["b", "a"]

    def test_record_overwrite_moves_to_end(self):
        cache: OrderedDict = OrderedDict()
        record_permission(cache, "a", "always_allow")
        record_permission(cache, "b", "always_allow")
        record_permission(cache, "a", "always_deny")
        assert cache["a"] == "always_deny"
        assert list(cache.keys()) == ["b", "a"]


class TestLruEviction:
    def test_eviction_at_cap(self):
        cache: OrderedDict = OrderedDict()
        for i in range(_PERMISSION_CACHE_MAX_SIZE):
            record_permission(cache, f"tool_{i}", "always_allow")
        assert len(cache) == _PERMISSION_CACHE_MAX_SIZE

        # Writing one more should evict the oldest.
        record_permission(cache, "tool_overflow", "always_allow")
        assert len(cache) == _PERMISSION_CACHE_MAX_SIZE
        assert "tool_0" not in cache
        assert "tool_overflow" in cache


class TestNoneGuards:
    def test_lookup_with_none_returns_none(self):
        assert lookup_permission(None, "bash") is None

    def test_record_with_none_is_noop(self):
        # Must not raise.
        record_permission(None, "bash", "always_allow")
