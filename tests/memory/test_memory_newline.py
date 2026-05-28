"""Tests for Unix newline handling in memory files."""

from __future__ import annotations

from pathlib import Path

from iac_code.memory.memory_manager import MemoryManager


def test_save_uses_unix_newlines(tmp_path: Path):
    """Memory files always use LF, never CRLF."""
    mgr = MemoryManager(str(tmp_path))
    mgr.save("test-mem", "line1\nline2\nline3", memory_type="user", description="test")

    raw = (tmp_path / "test-mem.md").read_bytes()
    assert b"\r\n" not in raw
    assert b"\n" in raw


def test_index_uses_unix_newlines(tmp_path: Path):
    """Index file also uses LF newlines."""
    mgr = MemoryManager(str(tmp_path))
    mgr.save("mem-a", "content a", memory_type="user", description="first")
    mgr.save("mem-b", "content b", memory_type="feedback", description="second")

    raw = (tmp_path / "MEMORY.md").read_bytes()
    assert b"\r\n" not in raw
    assert b"\n" in raw
